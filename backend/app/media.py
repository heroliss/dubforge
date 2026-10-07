"""可取消的媒体分段传输，避免废弃的预览继续读取大文件。"""
from contextlib import asynccontextmanager
from secrets import token_hex

import anyio
from anyio.lowlevel import RunVar
from starlette.datastructures import MutableHeaders
from starlette.requests import ClientDisconnect
from starlette.responses import FileResponse


_read_limiter = RunVar('dubforge_media_read_limiter')


def _limiter():
    # 与同步 JSON 路由的默认线程额度隔离；每个事件循环各自管理，兼容测试客户端。
    try:
        return _read_limiter.get()
    except LookupError:
        limiter = anyio.CapacityLimiter(4)
        _read_limiter.set(limiter)
        return limiter


def _open_file(path):
    return open(path, 'rb')


@asynccontextmanager
async def _media_file(path):
    limiter = _limiter()
    file = await anyio.to_thread.run_sync(_open_file, path, limiter=limiter)
    try:
        yield file, limiter
    finally:
        # 断连取消也必须关闭文件；Windows 上遗留句柄会阻止产物原子替换。
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(file.close, limiter=limiter)


class MediaFileResponse(FileResponse):
    """沿用 Starlette 的 Range/If-Range 校验，补充断连与有限读流。"""

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await super().__call__(scope, receive, send)

        async def checked_send(message):
            try:
                await send(message)
            except OSError as exc:
                # ASGI 2.4 服务端也可能在发送时直接报告断连。
                raise ClientDisconnect() from exc

        async with anyio.create_task_group() as group:
            async def transfer():
                try:
                    await super(MediaFileResponse, self).__call__(scope, receive, checked_send)
                except ClientDisconnect:
                    pass
                finally:
                    group.cancel_scope.cancel()

            async def watch_disconnect():
                while True:
                    message = await receive()
                    if message['type'] == 'http.disconnect':
                        group.cancel_scope.cancel()
                        return

            group.start_soon(watch_disconnect)
            await transfer()

    async def _send_range(self, send, start, end, multipart=False):
        async with _media_file(self.path) as (file, limiter):
            await self._read_range(send, file, limiter, start, end, multipart)

    async def _read_range(self, send, file, limiter, start, end, multipart=False):
        await anyio.to_thread.run_sync(file.seek, start, limiter=limiter)
        while start < end:
            chunk = await anyio.to_thread.run_sync(
                file.read, min(self.chunk_size, end - start), limiter=limiter)
            if not chunk:
                raise RuntimeError('媒体文件在读取期间发生变化，请刷新预览')
            start += len(chunk)
            await send({'type': 'http.response.body', 'body': chunk,
                        'more_body': multipart or start < end})

    async def _handle_simple(self, send, send_header_only, send_pathsend):
        await send({'type': 'http.response.start', 'status': self.status_code,
                    'headers': self.raw_headers})
        if send_header_only or self.headers['content-length'] == '0':
            await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
        elif send_pathsend:
            await send({'type': 'http.response.pathsend', 'path': str(self.path)})
        else:
            await self._send_range(send, 0, int(self.headers['content-length']))

    async def _handle_single_range(self, send, start, end, file_size, send_header_only):
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers['content-range'] = f'bytes {start}-{end - 1}/{file_size}'
        headers['content-length'] = str(end - start)
        await send({'type': 'http.response.start', 'status': 206, 'headers': headers.raw})
        if send_header_only:
            await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
        else:
            await self._send_range(send, start, end)

    async def _handle_multiple_ranges(self, send, ranges, file_size, send_header_only):
        boundary = token_hex(13)
        length, make_header = self.generate_multipart(
            ranges, boundary, file_size, self.headers['content-type'])
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers['content-type'] = f'multipart/byteranges; boundary={boundary}'
        headers['content-length'] = str(length)
        await send({'type': 'http.response.start', 'status': 206, 'headers': headers.raw})
        if not send_header_only:
            # 多段也固定同一文件版本，不能在两段之间重新打开已被替换的同名文件。
            async with _media_file(self.path) as (file, limiter):
                for start, end in ranges:
                    await send({'type': 'http.response.body', 'body': make_header(start, end), 'more_body': True})
                    await self._read_range(send, file, limiter, start, end, multipart=True)
                    await send({'type': 'http.response.body', 'body': b'\r\n', 'more_body': True})
        await send({'type': 'http.response.body',
                    'body': b'' if send_header_only else f'--{boundary}--'.encode('latin-1'),
                    'more_body': False})
