"""媒体 Range 与断连回归；只读取测试临时文件，不接触生产媒体。"""
import io
import threading

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import FileResponse

from app import media


def _scope(headers=(), method='GET'):
    return {'type': 'http', 'method': method, 'headers': list(headers)}


def _client(path):
    app = FastAPI()

    @app.api_route('/media', methods=['GET', 'HEAD'])
    def file():
        return media.MediaFileResponse(path, media_type='video/mp4',
                                       headers={'Cache-Control': 'no-store'})

    return TestClient(app)


@pytest.mark.parametrize('header,expected,content_range', [
    ('bytes=2-5', b'2345', 'bytes 2-5/10'),
    ('bytes=6-', b'6789', 'bytes 6-9/10'),
    ('bytes=-3', b'789', 'bytes 7-9/10'),
    ('bytes=8-999', b'89', 'bytes 8-9/10'),
])
def test_range_seek_and_suffix(tmp_path, header, expected, content_range):
    path = tmp_path / 'video.mp4'
    path.write_bytes(b'0123456789')
    response = _client(path).get('/media', headers={'Range': header})
    assert response.status_code == 206
    assert response.content == expected
    assert response.headers['content-range'] == content_range
    assert response.headers['content-length'] == str(len(expected))
    assert response.headers['accept-ranges'] == 'bytes'
    assert response.headers['cache-control'] == 'no-store'


def test_full_multiple_ranges_and_if_range(tmp_path):
    path = tmp_path / 'video.mp4'
    path.write_bytes(b'0123456789')
    client = _client(path)
    full = client.get('/media')
    assert full.status_code == 200 and full.content == b'0123456789'
    multi = client.get('/media', headers={'Range': 'bytes=0-1,8-9'})
    assert multi.status_code == 206
    assert multi.headers['content-type'].startswith('multipart/byteranges; boundary=')
    assert len(multi.content) == int(multi.headers['content-length'])
    assert b'Content-Range: bytes 0-1/10\r\n\r\n01' in multi.content
    assert b'Content-Range: bytes 8-9/10\r\n\r\n89' in multi.content
    for etag in (full.headers['etag'], full.headers['last-modified']):
        response = client.get('/media', headers={'Range': 'bytes=4-5', 'If-Range': etag})
        assert response.status_code == 206 and response.content == b'45'
    changed = client.get('/media', headers={'Range': 'bytes=4-5', 'If-Range': 'old-version'})
    assert changed.status_code == 200 and changed.content == full.content


def test_multiple_ranges_pin_one_file_version(tmp_path, monkeypatch):
    path = tmp_path / 'video.mp4'
    path.write_bytes(b'0123456789')
    opened = []

    def open_file(_):
        # 原子替换后再次打开会得到新版本；同一次响应必须始终读取旧句柄。
        file = TrackedFile(b'abcdefghij' if opened else b'0123456789')
        opened.append(file)
        return file

    monkeypatch.setattr(media, '_open_file', open_file)
    response = _client(path).get('/media', headers={'Range': 'bytes=0-1,8-9'})
    assert response.status_code == 206
    assert b'Content-Range: bytes 0-1/10\r\n\r\n01' in response.content
    assert b'Content-Range: bytes 8-9/10\r\n\r\n89' in response.content
    assert len(opened) == 1 and opened[0].closed


@pytest.mark.parametrize('header,status', [('bytes=100-', 416), ('bytes=6-2', 400),
                                          ('bytes=', 400), ('seconds=1-2', 400)])
def test_invalid_range_is_bounded(tmp_path, header, status):
    path = tmp_path / 'video.mp4'
    path.write_bytes(b'0123456789')
    response = _client(path).get('/media', headers={'Range': header})
    assert response.status_code == status
    if status == 416:
        assert response.headers['content-range'] == 'bytes */10'


def test_head_never_opens_media_and_empty_get(tmp_path, monkeypatch):
    path = tmp_path / 'video.mp4'
    path.write_bytes(b'0123456789')
    client = _client(path)
    monkeypatch.setattr(media, '_open_file', lambda _: pytest.fail('HEAD 不应打开媒体'))
    response = client.head('/media', headers={'Range': 'bytes=3-5'})
    assert response.status_code == 206 and not response.content
    assert response.headers['content-range'] == 'bytes 3-5/10'
    assert response.headers['content-length'] == '3'
    multi = client.head('/media', headers={'Range': 'bytes=0-1,8-9'})
    assert multi.status_code == 206 and not multi.content
    path.write_bytes(b'')
    empty = client.get('/media')
    assert empty.status_code == 200 and empty.content == b''
    assert empty.headers['content-length'] == '0'


class TrackedFile(io.BytesIO):
    def __init__(self, content):
        super().__init__(content)
        self.read_sizes = []
        self.bytes_read = 0

    def read(self, size):
        self.read_sizes.append(size)
        chunk = super().read(size)
        self.bytes_read += len(chunk)
        return chunk


@pytest.mark.parametrize('header', [None, 'bytes=0-', 'bytes=0-131071,262144-1048575'])
def test_disconnect_stops_old_seek_and_closes_file(tmp_path, monkeypatch, header):
    content = b'x' * (1024 * 1024)
    path = tmp_path / 'video.mp4'
    path.write_bytes(content)
    tracked = TrackedFile(content)
    monkeypatch.setattr(media, '_open_file', lambda _: tracked)

    async def run():
        disconnected = anyio.Event()
        request_received = False
        body_count = 0

        async def receive():
            nonlocal request_received
            if not request_received:
                request_received = True
                return {'type': 'http.request', 'body': b'', 'more_body': False}
            await disconnected.wait()
            return {'type': 'http.disconnect'}

        async def send(message):
            nonlocal body_count
            if message['type'] == 'http.response.body' and len(message.get('body', b'')) >= 65536:
                body_count += 1
                disconnected.set()
                await anyio.sleep(0)
            # 模拟 Uvicorn：断连后 send 返回，而不是抛异常。

        headers = [] if header is None else [(b'range', header.encode())]
        with anyio.fail_after(2):
            await media.MediaFileResponse(path)(_scope(headers), receive, send)
        assert body_count <= 2
        assert tracked.closed
        assert tracked.bytes_read <= media.MediaFileResponse.chunk_size * 2
        assert media._limiter().borrowed_tokens == 0

    anyio.run(run)


def test_stock_response_reproduces_abandoned_full_read(tmp_path, monkeypatch):
    """确证旧响应忽略断连，在 send 静默返回后仍读取完整文件。"""
    content = b'x' * (1024 * 1024)
    path = tmp_path / 'video.mp4'
    path.write_bytes(content)
    tracked = TrackedFile(content)

    class AsyncFile:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            tracked.close()

        async def read(self, size):
            return tracked.read(size)

    async def open_file(*_, **__):
        return AsyncFile()

    monkeypatch.setattr(anyio, 'open_file', open_file)

    async def run():
        async def receive():
            return {'type': 'http.disconnect'}

        async def send(_):
            pass

        await FileResponse(path)(_scope(), receive, send)

    anyio.run(run)
    assert tracked.bytes_read == len(content)
    assert len(tracked.read_sizes) == 17


def test_send_disconnect_closes_media(tmp_path, monkeypatch):
    path = tmp_path / 'video.mp4'
    path.write_bytes(b'x' * 100000)
    tracked = TrackedFile(path.read_bytes())
    monkeypatch.setattr(media, '_open_file', lambda _: tracked)

    async def run():
        async def receive():
            await anyio.sleep_forever()

        async def send(message):
            if message['type'] == 'http.response.body':
                raise OSError('connection closed')

        await media.MediaFileResponse(path)(_scope(), receive, send)
        assert tracked.closed
        assert media._limiter().borrowed_tokens == 0

    anyio.run(run)


def test_media_read_limit_does_not_consume_json_thread_capacity(tmp_path, monkeypatch):
    path = tmp_path / 'video.mp4'
    path.write_bytes(b'x')
    release = threading.Event()
    started = 0
    maximum = 0
    lock = threading.Lock()
    opened = []

    class SlowFile(TrackedFile):
        def read(self, size):
            nonlocal started, maximum
            with lock:
                started += 1
                maximum = max(maximum, started)
            try:
                assert release.wait(3), '隔离测试未释放媒体读流'
                return super().read(size)
            finally:
                with lock:
                    started -= 1

    def open_file(_):
        file = SlowFile(b'x')
        opened.append(file)
        return file

    monkeypatch.setattr(media, '_open_file', open_file)

    async def run():
        default = anyio.to_thread.current_default_thread_limiter()
        original = default.total_tokens
        default.total_tokens = 1
        try:
            async def receive():
                await anyio.sleep_forever()

            async def send(_):
                pass

            async with anyio.create_task_group() as group:
                for _ in range(6):
                    group.start_soon(media.MediaFileResponse(path, stat_result=path.stat()),
                                     _scope(), receive, send)
                with anyio.fail_after(2):
                    while maximum < 4:
                        await anyio.sleep(.005)
                    # 模拟健康/状态同步路由；媒体已占满专用额度时仍能执行。
                    assert await anyio.to_thread.run_sync(lambda: '健康') == '健康'
                release.set()
            assert maximum == 4
            assert all(file.closed for file in opened)
            assert media._limiter().borrowed_tokens == 0
        finally:
            release.set()
            default.total_tokens = original

    anyio.run(run)


def test_media_routes_range_head_and_cheap_lookup(isolated_env, monkeypatch):
    from app.main import app
    from dubforge.domain import crud
    from dubforge.domain.models import Artifact
    from dubforge.pipeline.dependencies import Dependencies
    from dubforge import runner

    project = crud.create_project(title='隔离媒体', source_kind='local')
    root = crud.project_dir(project.uid)
    video = root / 'media' / 'video.mp4'
    audio = root / 'audio' / 'background.wav'
    output = root / 'output' / 'final_test.mp4'
    for path in (video, audio, output):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'0123456789')
    with crud.session() as session:
        session.add(Artifact(project_id=project.id, step_key='assemble', name=output.name,
                             rel_path='output/final_test.mp4', size=10, sha256='saved-hash'))
        session.commit()
    monkeypatch.setattr(Dependencies, '__init__', lambda *_: pytest.fail('媒体请求不应构造制作依赖'))
    monkeypatch.setattr(runner, 'compute_fingerprint', lambda *_: pytest.fail('媒体请求不应计算制作指纹'))
    client = TestClient(app)
    urls = [f'/api/projects/{project.uid}/steps/assemble/artifacts/{output.name}/raw',
            f'/api/projects/{project.uid}/workbench/video',
            f'/api/projects/{project.uid}/workbench/audio/bg']
    for url in urls:
        response = client.get(url, headers={'Range': 'bytes=5-7'})
        assert response.status_code == 206 and response.content == b'567'
        assert response.headers['content-range'] == 'bytes 5-7/10'
        assert response.headers['cache-control'] == 'no-store'
        head = client.head(url)
        assert head.status_code == 200 and not head.content
        assert head.headers['content-length'] == '10'


def test_artifact_path_whitelist_unchanged(isolated_env, tmp_path):
    from app.main import app
    from dubforge.domain import crud
    from dubforge.domain.models import Artifact

    project = crud.create_project(title='产物白名单')
    outside = tmp_path / 'external.mp4'
    outside.write_bytes(b'original')
    with crud.session() as session:
        for name, path in [('external.mp4', str(outside)), ('escape.mp4', '../../external.mp4')]:
            session.add(Artifact(project_id=project.id, step_key='assemble', name=name,
                                 rel_path=path, size=8))
        session.commit()
    client = TestClient(app)
    base = f'/api/projects/{project.uid}/steps/assemble/artifacts'
    external = client.get(f'{base}/external.mp4/raw', headers={'Range': 'bytes=0-2'})
    assert external.status_code == 206 and external.content == b'ori'
    escape = client.get(f'{base}/escape.mp4/raw')
    assert escape.status_code == 400 and escape.json()['code'] == 'ARTIFACT_PATH_INVALID'
    absent = client.get(f'{base}/unregistered.mp4/raw')
    assert absent.status_code == 400 and absent.json()['code'] == 'ARTIFACT_NOT_FOUND'
