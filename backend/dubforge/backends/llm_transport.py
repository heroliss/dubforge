"""同步流水线使用的可取消 HTTP 客户端；取消网络协程并关闭本地连接。"""
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
import threading

import httpx


class CancellableClient:
    """保持流水线的同步调用方式，网络等待在独立事件循环中执行。"""

    def __init__(self, **options):
        self.options = options
        self.cancel_check = None
        self._loop = None
        self._thread = None
        self._client = None

    def __enter__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever,
                                        name='dubforge-llm-http', daemon=True)
        self._thread.start()

        async def open_client():
            self._client = httpx.AsyncClient(**self.options)
            await self._client.__aenter__()

        try:
            self._wait(open_client())
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def _wait(self, coroutine, *, cancellable=True):
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        try:
            while True:
                if cancellable and self.cancel_check:
                    self.cancel_check()
                try:
                    return future.result(timeout=.1 if cancellable else 5)
                except FutureTimeout:
                    # 网络超时也使用 TimeoutError；完成的 Future 必须原样抛出。
                    if future.done() or not cancellable:
                        raise
        except BaseException:
            future.cancel()
            raise

    def post(self, url, **options):
        return self._wait(self._client.post(url, **options))

    @contextmanager
    def stream(self, method, url, **options):
        request = self._client.build_request(method, url, **options)
        response = self._wait(self._client.send(request, stream=True))
        try:
            yield _Response(self, response)
        finally:
            self._wait(response.aclose(), cancellable=False)

    def __exit__(self, exc_type, exc, tb):
        async def close_client():
            # 等取消落到协程中后再关闭连接，防止留下一条仍在读取的后台请求。
            pending = [task for task in asyncio.all_tasks()
                       if task is not asyncio.current_task() and not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if self._client is not None:
                await self._client.aclose()

        try:
            self._wait(close_client(), cancellable=False)
        except Exception:
            if exc_type is None:
                raise
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
            if not self._thread.is_alive():
                self._loop.close()


class _Response:
    """只适配调用方使用的同步读取方法，其他响应属性交给 httpx。"""

    def __init__(self, client, response):
        self._client, self._response = client, response

    def __getattr__(self, name):
        return getattr(self._response, name)

    def read(self):
        return self._client._wait(self._response.aread())

    def iter_lines(self):
        iterator = self._response.aiter_lines()
        while True:
            try:
                line = self._client._wait(anext(iterator))
            except StopAsyncIteration:
                return
            yield line
