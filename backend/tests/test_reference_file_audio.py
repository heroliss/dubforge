"""本机参考文件试听：只读取临时音频，不启动服务、不访问生产配置。"""
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import wave

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

from app.api import files
from dubforge.errors import DubForgeError


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('DUBFORGE_DATA_ROOT', str(tmp_path / 'data'))
    monkeypatch.setenv('DUBFORGE_CONFIG', str(tmp_path / 'config.yaml'))
    app = FastAPI()
    app.include_router(files.router)

    @app.exception_handler(DubForgeError)
    async def domain_error(request: Request, exc: DubForgeError):
        return JSONResponse(status_code=400, content=exc.to_dict())

    return TestClient(app)


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / '中文 空格 & + # 参考音色.WAV'
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b'\x01\x00' * 80)
    return path


def _metadata(client, path, **kwargs):
    return client.get('/api/files/reference-audio', params={'path': str(path)}, **kwargs)


def test_reference_file_returns_versioned_url_and_original_bytes(client, audio):
    response = _metadata(client, audio)
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    body = response.json()
    assert body['filename'] == audio.name
    assert body['url'].startswith('/api/files/reference-audio/content?')
    query = parse_qs(urlparse(body['url']).query)
    assert query['path'] == [str(audio.resolve())]
    assert query['v'] == [f'{audio.stat().st_mtime_ns}-{audio.stat().st_size}']
    before = audio.read_bytes()
    streamed = client.get(body['url'])
    assert streamed.status_code == 200
    assert streamed.content == before
    assert streamed.headers['content-type'] == 'audio/wav'
    assert streamed.headers['accept-ranges'] == 'bytes'
    assert streamed.headers['cache-control'] == 'no-store'
    assert streamed.headers['x-content-type-options'] == 'nosniff'
    assert audio.read_bytes() == before


def test_reference_file_url_changes_after_file_is_replaced(client, audio):
    first = _metadata(client, audio).json()['url']
    audio.write_bytes(audio.read_bytes() + b'\x00\x00')
    second = _metadata(client, audio).json()['url']
    assert first != second


@pytest.mark.parametrize('header,start,end', [
    ('bytes=0-3', 0, 4), ('bytes=44-51', 44, 52),
    ('bytes=180-', 180, 204), ('bytes=-8', 196, 204),
])
def test_reference_file_range_seeking(client, audio, header, start, end):
    url = _metadata(client, audio).json()['url']
    response = client.get(url, headers={'Range': header})
    assert response.status_code == 206
    assert response.content == audio.read_bytes()[start:end]
    assert response.headers['content-range'] == f'bytes {start}-{end - 1}/204'
    assert response.headers['content-length'] == str(end - start)


@pytest.mark.parametrize('headers,status,length', [({}, 200, '204'),
    ({'Range': 'bytes=0-3'}, 206, '4')])
def test_reference_file_head_does_not_read_audio(client, audio, monkeypatch, headers, status, length):
    from app import media
    url = _metadata(client, audio).json()['url']
    monkeypatch.setattr(media, '_open_file', lambda *args: pytest.fail('HEAD 不应打开音频流'))
    response = client.head(url, headers=headers)
    assert response.status_code == status
    assert response.content == b''
    assert response.headers['content-length'] == length
    assert response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('value', ['', '  ', 'voice.wav', '../voice.wav',
    'https://example.com/voice.wav', 'file:///C:/voice.wav', 'data:audio/wav,AA',
    '//server/share/voice.wav', r'\\server\share\voice.wav',
    r'\\?\C:\voice.wav', r'\\.\C:\voice.wav', r'\??\C:\voice.wav',
    'C:/voice.wav:secret.wav', 'C:/bad\nname.wav', 'C:/bad\x00name.wav'])
def test_reference_file_rejects_nonlocal_or_invalid_paths(client, value):
    for route in ('/api/files/reference-audio', '/api/files/reference-audio/content'):
        response = client.get(route, params={'path': value})
        assert response.status_code == 400
        assert response.json()['code'] == 'REFERENCE_FILE_INVALID'


def test_reference_file_missing_directory_and_wrong_extension(client, tmp_path):
    directory = tmp_path / 'folder.wav'
    directory.mkdir()
    document = tmp_path / 'private.yaml'
    document.write_text('not audio', encoding='utf-8')
    for path, code in [(tmp_path / 'missing.wav', 'REFERENCE_FILE_NOT_FOUND'),
                       (directory, 'REFERENCE_FILE_INVALID'),
                       (document, 'REFERENCE_FILE_FORMAT')]:
        response = _metadata(client, path)
        assert response.status_code == 400
        assert response.json()['code'] == code
        assert 'not audio' not in response.text


@pytest.mark.parametrize('headers', [
    {'Origin': 'https://untrusted.example'}, {'Origin': 'null'},
    {'Origin': 'https://[invalid'}, {'Sec-Fetch-Site': 'cross-site'},
    {'Origin': 'http://localhost:5273', 'Sec-Fetch-Site': 'cross-site'},
])
def test_reference_file_rejects_cross_site_for_metadata_and_content(client, audio, headers):
    for route in ('/api/files/reference-audio', '/api/files/reference-audio/content'):
        response = client.get(route, params={'path': str(audio)}, headers=headers)
        assert response.status_code == 400
        assert response.json()['code'] == 'FILE_ORIGIN'
    response = client.head('/api/files/reference-audio/content', params={'path': str(audio)}, headers=headers)
    assert response.status_code == 400


@pytest.mark.parametrize('headers', [{}, {'Sec-Fetch-Site': 'same-origin'},
    {'Origin': 'http://testserver'}, {'Origin': 'http://localhost:5273'},
    {'Origin': 'http://127.0.0.1:8601'}])
def test_reference_file_allows_local_page_media_requests(client, audio, headers):
    response = _metadata(client, audio, headers=headers)
    assert response.status_code == 200
    assert client.get(response.json()['url'], headers=headers).status_code == 200


@pytest.mark.parametrize('suffix,mime', [('.mp3', 'audio/mpeg'), ('.flac', 'audio/flac'),
    ('.ogg', 'audio/ogg'), ('.opus', 'audio/ogg'), ('.m4a', 'audio/mp4'),
    ('.aac', 'audio/aac'), ('.wma', 'audio/x-ms-wma'), ('.aif', 'audio/aiff'),
    ('.aiff', 'audio/aiff')])
def test_reference_file_preserves_format_without_transcoding(client, tmp_path, suffix, mime):
    path = tmp_path / ('reference' + suffix)
    path.write_bytes(b'original encoded audio')
    url = _metadata(client, path).json()['url']
    response = client.get(url)
    assert response.content == b'original encoded audio'
    assert response.headers['content-type'] == mime


def test_reference_file_checks_resolved_network_target(audio, monkeypatch):
    monkeypatch.setattr(Path, 'resolve', lambda *args, **kwargs: Path(r'\\server\share\audio.wav'))
    with pytest.raises(DubForgeError) as caught:
        files._reference_audio_path(str(audio))
    assert caught.value.code == 'REFERENCE_FILE_INVALID'


def test_reference_file_permission_error_is_a_readable_domain_error(client, audio, monkeypatch):
    def unreadable(*args, **kwargs):
        raise PermissionError('secret detail')
    monkeypatch.setattr(Path, 'resolve', unreadable)
    response = _metadata(client, audio)
    assert response.status_code == 400
    assert response.json()['code'] == 'REFERENCE_FILE_UNREADABLE'
    assert 'secret detail' not in response.text
