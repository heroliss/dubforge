"""单句音频快照的定位传输回归，所有文件均来自隔离目录。"""
import os

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.media import MediaSnapshotResponse


def _client(path):
    app = FastAPI()

    @app.api_route('/sample', methods=['GET', 'HEAD'])
    def sample():
        return MediaSnapshotResponse(path, media_type='audio/wav',
                                     headers={'Cache-Control': 'no-store'})

    return TestClient(app)


@pytest.mark.parametrize('header,expected,content_range', [
    ('bytes=2-5', b'2345', 'bytes 2-5/10'),
    ('bytes=6-', b'6789', 'bytes 6-9/10'),
    ('bytes=-3', b'789', 'bytes 7-9/10'),
    ('bytes=8-999', b'89', 'bytes 8-9/10'),
])
def test_snapshot_ranges(tmp_path, header, expected, content_range):
    path = tmp_path / 'sample.wav'
    path.write_bytes(b'0123456789')
    response = _client(path).get('/sample', headers={'Range': header})
    assert response.status_code == 206
    assert response.content == expected
    assert response.headers['content-range'] == content_range
    assert response.headers['content-length'] == str(len(expected))
    assert response.headers['accept-ranges'] == 'bytes'
    assert response.headers['cache-control'] == 'no-store'


def test_snapshot_full_multiple_ranges_and_if_range(tmp_path):
    path = tmp_path / 'sample.wav'
    path.write_bytes(b'0123456789')
    client = _client(path)
    full = client.get('/sample')
    assert full.status_code == 200 and full.content == b'0123456789'
    assert full.headers['content-type'] == 'audio/wav'
    multi = client.get('/sample', headers={'Range': 'bytes=0-1,8-9'})
    assert multi.status_code == 206
    assert multi.headers['content-type'].startswith('multipart/byteranges; boundary=')
    assert len(multi.content) == int(multi.headers['content-length'])
    assert b'Content-Range: bytes 0-1/10\r\n\r\n01' in multi.content
    assert b'Content-Range: bytes 8-9/10\r\n\r\n89' in multi.content
    for validator in (full.headers['etag'], full.headers['last-modified']):
        ranged = client.get('/sample', headers={'Range': 'bytes=4-5', 'If-Range': validator})
        assert ranged.status_code == 206 and ranged.content == b'45'
    changed = client.get('/sample', headers={'Range': 'bytes=4-5', 'If-Range': 'old-version'})
    assert changed.status_code == 200 and changed.content == full.content


@pytest.mark.parametrize('header,status', [('bytes=100-', 416), ('bytes=6-2', 400),
                                          ('bytes=', 400), ('seconds=1-2', 400)])
def test_snapshot_invalid_range(tmp_path, header, status):
    path = tmp_path / 'sample.wav'
    path.write_bytes(b'0123456789')
    response = _client(path).get('/sample', headers={'Range': header})
    assert response.status_code == status
    if status == 416:
        assert response.headers['content-range'] == 'bytes */10'


@pytest.mark.parametrize('header,status,length', [
    (None, 200, '10'), ('bytes=3-5', 206, '3'), ('bytes=0-1,8-9', 206, None),
])
def test_snapshot_head(tmp_path, header, status, length):
    path = tmp_path / 'sample.wav'
    path.write_bytes(b'0123456789')
    response = _client(path).head('/sample', headers={} if header is None else {'Range': header})
    assert response.status_code == status and response.content == b''
    if length is not None:
        assert response.headers['content-length'] == length


@pytest.mark.parametrize('header', [None, 'bytes=2-5', 'bytes=0-1,8-9'])
def test_snapshot_releases_file_before_response_and_pins_version(tmp_path, header):
    path = tmp_path / 'sample.wav'
    path.write_bytes(b'0123456789')
    response = MediaSnapshotResponse(path, media_type='audio/wav')
    messages = []

    async def run():
        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        async def send(message):
            if message['type'] == 'http.response.start':
                replacement = tmp_path / 'sample.wav.tmp'
                replacement.write_bytes(b'abcdefghij')
                # Windows 下打开的句柄会阻止覆盖；此时必须已完全关闭源文件。
                os.replace(replacement, path)
            messages.append(message)

        headers = [] if header is None else [(b'range', header.encode())]
        scope = {'type': 'http', 'method': 'GET', 'headers': headers,
                 'extensions': {'http.response.pathsend': {}}}
        await response(scope, receive, send)

    anyio.run(run)
    content = b''.join(message.get('body', b'') for message in messages)
    assert path.read_bytes() == b'abcdefghij'
    if header is None:
        assert content == b'0123456789'
    elif header == 'bytes=2-5':
        assert content == b'2345'
    else:
        assert b'Content-Range: bytes 0-1/10\r\n\r\n01' in content
        assert b'Content-Range: bytes 8-9/10\r\n\r\n89' in content


def test_line_audio_route_range_head_missing_and_take(isolated_env):
    from app.main import app
    from dubforge.domain import crud
    from dubforge.pipeline import dubbing_edit

    project = crud.create_project(title='隔离单句音频')
    path = dubbing_edit.take_path(crud.project_dir(project.uid), 1, 2)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'0123456789')
    client = TestClient(app)
    url = f'/api/projects/{project.uid}/workbench/audio/line/1?take=2'
    ranged = client.get(url, headers={'Range': 'bytes=5-7'})
    assert ranged.status_code == 206 and ranged.content == b'567'
    assert ranged.headers['content-range'] == 'bytes 5-7/10'
    assert ranged.headers['accept-ranges'] == 'bytes'
    assert ranged.headers['cache-control'] == 'no-store'
    full = client.get(url)
    assert full.status_code == 200 and full.content == b'0123456789'
    head = client.head(url)
    assert head.status_code == 200 and head.content == b''
    assert head.headers['content-length'] == '10'
    invalid = client.get(url, headers={'Range': 'bytes=20-'})
    assert invalid.status_code == 416 and invalid.headers['content-range'] == 'bytes */10'
    assert client.get(f'/api/projects/{project.uid}/workbench/audio/line/1').status_code == 404
    assert client.get('/api/projects/absent/workbench/audio/line/1').status_code == 404
    path.unlink()
    assert client.get(url).status_code == 404


def test_empty_snapshot(tmp_path):
    path = tmp_path / 'sample.wav'
    path.write_bytes(b'')
    response = _client(path).get('/sample')
    assert response.status_code == 200 and response.content == b''
    assert response.headers['content-length'] == '0'
