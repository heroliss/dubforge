"""封面共享缓存：隔离数据、模拟网络，不请求平台或修改真实稿件。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

from dubforge import platform_cover as covers
from dubforge.domain import crud
from dubforge.publishing import service


@pytest.fixture
def cover_source(make_project, monkeypatch):
    uid = make_project()
    state = {'content': {'thumbnail': 'https://i.ytimg.com/vi/example/hqdefault.jpg'}}
    monkeypatch.setattr(covers.video_info, 'view', lambda key: {'info': state})
    calls = []
    image = b'\xff\xd8\xfftest-cover'

    @contextmanager
    def stream(method, url, **kwargs):
        calls.append((method, url, kwargs))
        assert kwargs['follow_redirects'] is False
        class Response:
            def raise_for_status(self):
                pass
            def iter_bytes(self, **kwargs):
                yield image
        yield Response()

    monkeypatch.setattr(httpx, 'stream', stream)
    return uid, state, calls, image


def test_preview_and_import_share_download_and_preserve_pipeline(cover_source, monkeypatch):
    from app.main import app
    uid, _, calls, image = cover_source
    before = [(s.step_key, s.status, s.input_fingerprint) for s in crud.get_steps(crud.get_project(uid).id)]
    preview = TestClient(app).get(f'/api/projects/{uid}/steps/download/video-info/cover')
    assert preview.status_code == 200 and preview.content == image
    assert preview.headers['content-type'] == 'image/jpeg'
    first = service.platform_cover(uid)
    # 已有内容寻址文件不重复写入，也不再次访问网络。
    monkeypatch.setattr(service, 'atomic_write_bytes', lambda *a: pytest.fail('不应重复写入封面'))
    assert service.platform_cover(uid) == first
    assert len(calls) == 1
    assert service._cover_path(first['cover_id']).read_bytes() == image
    assert before == [(s.step_key, s.status, s.input_fingerprint) for s in crud.get_steps(crud.get_project(uid).id)]


def test_concurrent_preview_and_import_only_download_once(cover_source):
    uid, _, calls, _ = cover_source
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(covers.get, [uid] * 4))
    assert len(set(paths)) == 1 and len(calls) == 1


def test_changed_url_and_missing_cache_download_again(cover_source):
    uid, state, calls, _ = cover_source
    old = covers.get(uid)
    state['content']['thumbnail'] = 'https://i.ytimg.com/vi/other/hqdefault.jpg'
    new = covers.get(uid)
    assert new != old and old.is_file()
    new.unlink()
    assert covers.get(uid).is_file() and len(calls) == 3


@pytest.mark.parametrize('url', ['http://i.ytimg.com/a', 'https://i.ytimg.com.evil.test/a',
                               'https://localhost/a', 'https://i.ytimg.com:8443/a',
                               'https://user:pass@i.ytimg.com/a', 'file:///secret', ''])
def test_cover_rejects_untrusted_urls_before_request(cover_source, url):
    uid, state, calls, _ = cover_source
    state['content']['thumbnail'] = url
    with pytest.raises(covers.PlatformCoverError):
        covers.get(uid)
    assert calls == []


def test_download_failure_does_not_cache_and_can_retry(cover_source, monkeypatch):
    uid, _, calls, _ = cover_source
    original = httpx.stream
    def failed(*a, **kw):
        raise httpx.ConnectError('模拟网络失败')
    monkeypatch.setattr(httpx, 'stream', failed)
    with pytest.raises(covers.PlatformCoverError, match='下载失败'):
        covers.get(uid)
    monkeypatch.setattr(httpx, 'stream', original)
    assert covers.get(uid).is_file() and len(calls) == 1


@pytest.mark.parametrize('kind', ['html', 'oversize'])
def test_bad_response_never_becomes_cached_cover(cover_source, monkeypatch, kind):
    uid, _, _, _ = cover_source
    payload = b'<html>not an image</html>' if kind == 'html' else b'\xff\xd8\xff' + b'x' * covers.MAX_BYTES
    @contextmanager
    def bad(*a, **kw):
        class Response:
            def raise_for_status(self):
                pass
            def iter_bytes(self, **kw):
                yield payload
        yield Response()
    monkeypatch.setattr(httpx, 'stream', bad)
    with pytest.raises(covers.PlatformCoverError):
        covers.get(uid)
    assert not list((covers.config.data_root() / 'cache/platform-covers').glob('*'))


def test_candidates_offer_preview_without_network(cover_source):
    uid, _, calls, _ = cover_source
    candidates = service.candidates(uid)
    assert candidates['platform_cover_available'] is True
    assert candidates['thumbnail_preview'].startswith(f'/api/projects/{uid}/steps/download/video-info/cover?')
    assert calls == []
