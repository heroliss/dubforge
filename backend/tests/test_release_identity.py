"""发行包版本与真实 HTTP 元数据同源，测试不启动服务监听。"""
from pathlib import Path
import tomllib
import warnings

from fastapi.testclient import TestClient
import pytest

from dubforge import __version__


def test_package_health_and_openapi_share_the_release_version(isolated_env):
    from app.main import app

    project = tomllib.loads((Path(__file__).parents[1] / 'pyproject.toml').read_text(encoding='utf-8'))
    assert 'version' in project['project']['dynamic']
    assert 'version' not in project['project']
    assert project['tool']['hatch']['version']['path'] == 'dubforge/__init__.py'
    client = TestClient(app)
    assert client.get('/api/health').json()['version'] == __version__
    app.openapi_schema = None
    with warnings.catch_warnings(record=True) as observed:
        warnings.simplefilter('always')
        schema = client.get('/openapi.json').json()
    assert schema['info']['version'] == __version__
    assert not any('Duplicate Operation ID' in str(item.message) for item in observed)
    operations = [value['operationId'] for path in schema['paths'].values()
                  for value in path.values() if isinstance(value, dict) and 'operationId' in value]
    assert len(operations) == len(set(operations))
    paths = {
        '/api/projects/{uid}/steps/{key}/artifacts/{name}/raw',
        '/api/projects/{uid}/workbench/audio/line/{line_id}',
        '/api/projects/{uid}/workbench/audio/bg', '/api/projects/{uid}/workbench/audio/{track}',
        '/api/projects/{uid}/workbench/video', '/api/files/reference-audio/content',
        '/api/projects/{uid}/assembly-versions/preview-history/{name}/raw',
        '/api/projects/{uid}/output-cuts/virtual-preview/{task_id}/raw',
        '/api/projects/{uid}/content-review/preview/{fingerprint}/{mode}',
    }
    assert all('get' in schema['paths'][path] and 'head' not in schema['paths'][path] for path in paths)


def test_original_project_media_head_preserves_ranges_and_missing_status(make_project, monkeypatch):
    from app.main import app
    from app import media
    from dubforge.domain import crud

    uid = make_project()
    audio = crud.project_dir(uid) / 'audio' / 'raw.wav'
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b'0123456789')
    monkeypatch.setattr(media, '_open_file', lambda *args: pytest.fail('HEAD 不应读取媒体流'))
    client = TestClient(app)
    url = f'/api/projects/{uid}/workbench/audio/raw'
    response = client.head(url, headers={'Range': 'bytes=2-5'})
    assert response.status_code == 206 and response.content == b''
    assert response.headers['content-range'] == 'bytes 2-5/10'
    assert response.headers['content-length'] == '4'
    assert response.headers['cache-control'] == 'no-store'
    audio.unlink()
    assert client.head(url).status_code == 404
    assert client.head('/api/projects/missing/workbench/audio/raw').status_code == 404
