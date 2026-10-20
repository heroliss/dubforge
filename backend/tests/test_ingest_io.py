"""本地上传的事件循环与取消边界；只使用隔离数据和 ASGI 内存请求。"""
import asyncio
import threading
from pathlib import Path

import httpx
import pytest
from starlette.requests import ClientDisconnect

from app.api import projects as api
from dubforge import video_info
from dubforge.domain import crud


def _project():
    project = crud.create_project('隔离上传', source_kind='local', source_url='')
    root = crud.project_dir(project.uid)
    source = root / 'media' / 'previous.mp4'
    source.write_bytes(b'previous-complete-video')
    project.source_url = 'media/previous.mp4'
    crud.save_project(project)
    crud.write_manifest(project.uid)
    return project, source


def _block_write(monkeypatch, entered, release):
    original_open = Path.open
    state = {'writing': False, 'closed': False, 'thread_ids': []}
    class Writer:
        def __init__(self, handle):
            self.handle = handle
        def write(self, chunk):
            state['thread_ids'].append(threading.get_ident())
            state['writing'] = True
            entered.set()
            try:
                assert release.wait(5)
                return self.handle.write(chunk)
            finally:
                state['writing'] = False
        def close(self):
            assert not state['writing'], '不能在仍有写入线程时关闭或删除文件'
            state['closed'] = True
            return self.handle.close()
    def open_file(path, mode='r', *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        return Writer(handle) if path.name.startswith('.tmp_ingest_') and mode == 'wb' else handle
    monkeypatch.setattr(Path, 'open', open_file)
    return state


@pytest.mark.parametrize('stage', ['write', 'commit'])
def test_health_responds_while_upload_disk_operation_is_blocked(isolated_env, monkeypatch, stage):
    from app.main import app
    project, previous = _project()
    entered, release = threading.Event(), threading.Event()
    main_thread = threading.get_ident()
    if stage == 'write':
        state = _block_write(monkeypatch, entered, release)
    else:
        original_manifest = crud.write_manifest
        def manifest(uid):
            assert threading.get_ident() != main_thread
            entered.set()
            assert release.wait(5)
            original_manifest(uid)
        monkeypatch.setattr(crud, 'write_manifest', manifest)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://isolated') as client:
            pending = asyncio.create_task(client.post(f'/api/projects/{project.uid}/ingest',
                params={'filename': 'new.mp4'}, content=b'new-complete-video'))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                healthy = await asyncio.wait_for(client.get('/api/health'), 1)
                assert healthy.status_code == 200 and healthy.json()['ok']
                assert not pending.done()
            finally:
                release.set()
            uploaded = await asyncio.wait_for(pending, 5)
            assert uploaded.status_code == 200
    asyncio.run(exercise())
    if stage == 'write':
        assert state['thread_ids'] and main_thread not in state['thread_ids']
    assert project.uid not in video_info._active
    assert not previous.exists()
    assert (previous.parent / 'new.mp4').read_bytes() == b'new-complete-video'
    assert not list(previous.parent.glob('.tmp_ingest_*'))


def test_client_disconnect_preserves_complete_source_and_releases_project(isolated_env):
    project, source = _project()
    class Disconnected:
        async def stream(self):
            yield b'incomplete-new-video'
            raise ClientDisconnect()
    with pytest.raises(ClientDisconnect):
        asyncio.run(api.ingest_video(project.uid, Disconnected(), 'new.mp4'))
    assert source.read_bytes() == b'previous-complete-video'
    assert crud.get_project(project.uid).source_url == 'media/previous.mp4'
    assert project.uid not in video_info._active
    assert not list(source.parent.glob('.tmp_ingest_*'))


def test_task_cancel_waits_for_inflight_write_before_cleanup_and_unlock(isolated_env, monkeypatch):
    project, source = _project()
    entered, release = threading.Event(), threading.Event()
    state = _block_write(monkeypatch, entered, release)
    class Request:
        async def stream(self):
            yield b'new-video'
    async def exercise():
        pending = asyncio.create_task(api.ingest_video(project.uid, Request(), 'new.mp4'))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            pending.cancel()
            await asyncio.sleep(.02)
            assert not pending.done() and project.uid in video_info._active
            assert not state['closed']
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 5)
    asyncio.run(exercise())
    assert state['closed']
    assert source.read_bytes() == b'previous-complete-video'
    assert crud.get_project(project.uid).source_url == 'media/previous.mp4'
    assert project.uid not in video_info._active
    assert not list(source.parent.glob('.tmp_ingest_*'))
