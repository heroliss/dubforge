"""所选/单句配音是真正的步骤任务，断开页面不影响队列和任务控制。"""
import time
from threading import Event

import pytest
from fastapi.testclient import TestClient

from conftest import synthesize
from test_dubbing_preparation import prepared, index_prepared
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import dubbing_task
from dubforge.runner import runner


def until(predicate):
    deadline = time.monotonic() + 10
    while not predicate():
        assert time.monotonic() < deadline, '后台任务未在测试期限内到达预期状态'
        time.sleep(.01)


def finished(project):
    until(lambda: runner.current_job_id(project.id, 'tts') is None)
    return crud.latest_job(project.id, 'tts')


@pytest.mark.parametrize('code', ['TTS_REQUEST_TIMEOUT', 'TTS_SERVICE_DRAINING', 'TTS_REQUEST_FAILED', 'SERVICE_OFFLINE'])
def test_service_level_failure_stops_batch_without_submitting_remaining_sentences(index_prepared, monkeypatch, code):
    from dubforge.backends import tts_indextts25
    client, uid, _ = index_prepared
    calls = []
    def generate(*args, **kwargs):
        calls.append(args[1])
        assert callable(kwargs['check_cancel'])
        assert kwargs['timeout_seconds'] == 300
        raise DubForgeError('服务需要恢复', code=code)
    monkeypatch.setattr(tts_indextts25, 'synth', generate)
    runner.start_dubbing(uid, [1, 2])
    project = crud.get_project(uid)
    assert finished(project).status == 'failed'
    assert calls == ['你好']
    assert crud.get_step(project.id, 'tts').error_code == code


def test_selected_queue_survives_client_close_and_reports_partial_line_progress(index_prepared, monkeypatch):
    from app.main import app
    from dubforge.backends import tts_indextts25
    client, uid, _ = index_prepared
    project, calls = crud.get_project(uid), []
    base = f'/api/projects/{uid}'
    started = [Event(), Event()]
    release = [Event(), Event()]
    original = tts_indextts25.synth

    def controlled(*args, **kwargs):
        calls.append(args[1])
        index = 0 if args[1] == '你好' else 1
        kwargs['progress'](.5 if index == 0 else .25)
        started[index].set()
        assert release[index].wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(tts_indextts25, 'synth', controlled)
    try:
        response = client.post(base + '/workbench/synthesize', json={'line_ids': [2, 1, 1]})
        assert response.status_code == 200
        job_id = response.json()['job_id']
        assert [row['id'] for row in response.json()['lines']] == [1, 2]
        assert started[0].wait(10)
        client.close()  # HTTP 已结束，关闭原页面连接也不会结束线程中的队列。
        client = TestClient(app)
        state = client.get(base + '/workbench?probe=false').json()
        assert state['synthesis_task']['job_id'] == job_id
        assert state['synthesis_task']['active'] and state['active_line'] == 1
        steps = client.get(base + '/steps').json()
        step = next(s for s in steps if s['key'] == 'tts')
        assert step['status'] == 'running' and step['job_id'] == job_id
        assert step['progress'] == pytest.approx(8.2, abs=.1)  # 为最多两次缩时重试保留进度；无需重试时直接完成本句。
        assert client.post(base + '/workbench/synthesize', json={'line_ids': [2]}).status_code == 400
        release[0].set()
        assert started[1].wait(10)
        state = client.get(base + '/workbench?probe=false').json()
        assert state['synthesis_task']['completed'] == 1 and state['active_line'] == 2
        first = state['lines'][0]
        assert first['selection']['source'] == 'dub' and first['wav']['exists']
        audio = client.get(first['wav']['url'])
        assert audio.status_code == 200 and audio.content.startswith(b'RIFF')
        assert not state['lines'][1]['wav']['exists']
        assert crud.get_step(project.id, 'tts').progress == pytest.approx(53.6, abs=.1)
    finally:
        for signal in release:
            signal.set()
        client.close()
    assert finished(project).status == 'done'
    assert calls == ['你好', '世界']
    state = dubbing_task.view(project.id)
    assert state['completed'] == state['total'] == 2 and not state['active']


@pytest.mark.parametrize('cancel', [False, True])
def test_selected_queue_uses_step_pause_resume_and_cancel(prepared, monkeypatch, cancel):
    from dubforge.backends import tts_gptsovits
    uid, project, calls = prepared
    started, release = Event(), Event()
    original = tts_gptsovits.synth

    def controlled(*args, **kwargs):
        if args[1] == '你好':
            started.set()
            assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(tts_gptsovits, 'synth', controlled)
    result = runner.start_dubbing(uid, [1, 2])
    try:
        assert started.wait(10)
        runner.pause_step(uid, 'tts', pause=True, expected_job_id=result['job_id'])
        assert runner.pause_state(project.id, 'tts') == 'requested'
        release.set()
        until(lambda: dubbing_task.view(project.id)['completed'] == 1)
        until(lambda: runner.pause_state(project.id, 'tts') == 'paused')
        assert calls == ['你好']
        assert dubbing_task.view(project.id)['active']
        if cancel:
            runner.cancel_step(uid, 'tts', expected_job_id=result['job_id'])
        else:
            runner.pause_step(uid, 'tts', pause=False, expected_job_id=result['job_id'])
    finally:
        release.set()
    job = finished(project)
    assert job.status == ('cancelled' if cancel else 'done')
    assert calls == (['你好'] if cancel else ['你好', '世界'])
    view = dubbing_task.view(project.id)
    assert not view['active']
    assert runner.pause_state(project.id, 'tts') == 'none'
    assert view['lines'][1]['status'] == ('interrupted' if cancel else 'done')
    assert (crud.project_dir(uid) / 'artifacts/tts/1.wav').is_file()


def test_failed_line_is_preserved_in_task_and_later_lines_continue(prepared, monkeypatch):
    from app.main import app
    from dubforge.backends import tts_gptsovits
    uid, project, calls = prepared
    client = TestClient(app)
    base = f'/api/projects/{uid}'
    synthesize(client, base + '/workbench/synthesize', json={'line_ids': [1, 2]})
    original = tts_gptsovits.synth

    def controlled(*args, **kwargs):
        if args[1] == '你好':
            raise DubForgeError('测试服务错误', code='TTS_FAILED')
        return original(*args, **kwargs)

    monkeypatch.setattr(tts_gptsovits, 'synth', controlled)
    runner.start_dubbing(uid, [1, 2])
    assert finished(project).status == 'failed'
    assert calls == ['你好', '世界', '世界']
    state = client.get(base + '/workbench?probe=false').json()['synthesis_task']
    assert state['failed'] == state['completed'] == 1
    assert state['lines'][0]['error'] == '测试服务错误'
    assert state['lines'][1]['status'] == 'done'
    step = next(s for s in client.get(base + '/steps').json() if s['key'] == 'tts')
    assert step['status'] == 'failed' and step['fresh']  # 旧版本仍可用，本次失败不能被掩盖。
    assert step['error']['code'] == 'TTS_PARTIAL_FAILED'
    client.close()


@pytest.mark.parametrize('ids', [[], None, [0], [True], [1.5], [99], '1'])
def test_invalid_selection_does_not_create_job(prepared, ids):
    uid, project, calls = prepared
    with pytest.raises(DubForgeError):
        runner.start_dubbing(uid, ids)
    assert crud.latest_job(project.id, 'tts') is None and not calls


def test_existing_database_adds_task_data_column(isolated_env):
    from dubforge import db
    with db.get_engine().begin() as conn:
        conn.exec_driver_sql('ALTER TABLE job DROP COLUMN task_data')
    db.init_db()
    with db.get_engine().connect() as conn:
        assert 'task_data' in {row[1] for row in conn.exec_driver_sql('PRAGMA table_info(job)')}


def test_selected_task_is_visible_and_cancellable_before_gpu_slot_is_available(prepared):
    uid, project, calls = prepared
    runner.acquire_slot('tts')
    try:
        job = runner.start_dubbing(uid, [1, 2])
        view = dubbing_task.view(project.id)
        assert view['active'] and view['total'] == 2
        assert all(row['status'] == 'queued' for row in view['lines'])
        assert crud.get_step(project.id, 'tts').progress == 0
        runner.cancel_step(uid, 'tts', expected_job_id=job['job_id'])
        assert finished(project).status == 'cancelled'
        assert not calls and not dubbing_task.view(project.id)['active']
    finally:
        runner.release_slot('tts')


def test_heartbeat_snapshot_cannot_overwrite_task_results_or_finished_status(prepared):
    uid, project, _ = prepared
    old_snapshot = crud.add_job(project.id, 'tts', '', task_data={'lines': []})
    current = crud.latest_job(project.id, 'tts')
    current.status = 'done'
    current.task_data = '{"lines":[{"id":1,"status":"done"}]}'
    crud.update_job(current)
    crud.update_job(old_snapshot, heartbeat_only=True)
    saved = crud.latest_job(project.id, 'tts')
    assert saved.status == 'done' and saved.task_data == current.task_data


def test_slow_audio_response_does_not_hold_file_open_during_overwrite(prepared):
    import asyncio
    from app.api.workbench import line_audio
    from app.main import app
    from dubforge.utils.fs import atomic_write_bytes

    uid, _, _ = prepared
    with TestClient(app) as client:
        synthesize(client, f'/api/projects/{uid}/workbench/lines/1/synthesize', json={})
    path = crud.project_dir(uid) / 'artifacts/tts/1.wav'
    before = path.read_bytes()
    response = line_audio(uid, 1)
    sent = []

    async def send(message):
        if message['type'] == 'http.response.body':
            # ASGI 正在发送旧音频时即可原子覆盖；Windows 文件锁不能持续到网络发送结束。
            atomic_write_bytes(path, before + b'new')
            sent.append(message['body'])

    async def receive():
        return {'type': 'http.disconnect'}

    asyncio.run(response({'type': 'http', 'method': 'GET', 'headers': []}, receive, send))
    assert b''.join(sent) == before
    assert path.read_bytes() == before + b'new'
