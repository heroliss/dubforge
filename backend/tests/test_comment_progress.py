"""评论实时状态只依据真实事件，轮询不重复解析整个工作区。"""
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app
from dubforge import comment_diagnostics as cd, comment_studio as cs, video_info
from dubforge.domain import crud
from dubforge.utils.fs import atomic_write_bytes
from test_comment_studio import uid, seed, request_payload, validated_result


def new_diagnostics(uid):
    job = dict(id='progress-fixture', action='filter', status='running', processed=0, total=12,
               cached=0, skipped=0, failed=0, blocked=0, deferred=0)
    return cd.Diagnostics(crud.get_project(uid), job)


def test_request_messages_are_counted_once_and_stage_has_no_prompt_body(uid):
    diag = new_diagnostics(uid)
    diag.trace('request', '不应进入进度的系统提示', request_id='one', role='system', model='fixture')
    started = diag.snapshot()['request_started_at']
    diag.trace('request', '不应进入进度的评论原文', request_id='one', role='user')
    diag.trace('reasoning_delta', '不应进入进度的模型思考', request_id='one')
    state = diag.snapshot()
    assert state['request'] == 1 and state['request_started_at'] == started
    assert state['phase'] == 'thinking' and state['reasoning_chars'] > 0
    assert '不应进入进度' not in json.dumps(state, ensure_ascii=False)
    diag.trace('assistant_delta', '1. 80 | 是 | 很有共鸣', request_id='one')
    assert diag.snapshot()['phase'] == 'output'
    diag.trace('assistant_response', '1. 80 | 是 | 很有共鸣', request_id='one')
    assert diag.snapshot()['phase'] == 'validating'
    diag.trace('validation_error', '尚需补齐编号 2', retry_attempt=1, retry_total=3)
    assert diag.snapshot()['phase'] == 'retrying'
    diag.trace('request', '仅补齐第二条', request_id='two', retry_attempt=2, retry_total=3)
    state = diag.snapshot()
    assert (state['request'], state['attempt'], state['attempts'], state['phase']) == (2, 2, 3, 'waiting')


def test_stream_events_update_memory_immediately_but_persist_at_most_once_per_second(uid, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(cd.time, 'monotonic', lambda: clock[0])
    diag = new_diagnostics(uid)
    writes = []
    write = cd.atomic_write_bytes
    def tracked(path, data, **kwargs):
        if path == diag.status_path:
            writes.append(json.loads(data))
        return write(path, data, **kwargs)
    monkeypatch.setattr(cd, 'atomic_write_bytes', tracked)
    diag.trace('assistant_delta', '甲', request_id='one')
    assert len(writes) == 1  # 真实阶段切换立即持久化。
    for moment in (10.1, 10.2, 10.9):
        clock[0] = moment
        diag.trace('assistant_delta', '乙', request_id='one')
    assert len(writes) == 1 and diag.snapshot()['output_chars'] == 4
    clock[0] = 11.1
    diag.trace('assistant_delta', '丙', request_id='one')
    assert len(writes) == 2 and writes[-1]['output_chars'] == 5


def test_terminal_elapsed_time_is_frozen_and_restored_running_job_is_interrupted(uid):
    ended = datetime.now(timezone.utc) - timedelta(seconds=10)
    job = dict(status='done', started_at=(ended - timedelta(seconds=40)).isoformat(),
               finished_at=ended.isoformat(), updated_at=ended.isoformat())
    assert cd.snapshot(job)['elapsed_seconds'] == 40.0
    diag = new_diagnostics(uid)
    restored = cd.restored_job(crud.get_project(uid), 'filter')
    assert restored['status'] == restored['phase'] == 'interrupted'
    assert restored['phase_label'] == '任务已中断'
    assert diag.snapshot()['status'] == 'running'  # 读取磁盘不改仍在运行的内存对象。


def test_legacy_terminal_job_without_end_time_keeps_elapsed_unknown():
    job = dict(status='done', started_at='2026-10-15T13:00:00+08:00')
    value = cd.snapshot(job)
    assert value['elapsed_seconds'] is None
    assert value['request_elapsed_seconds'] is None
    assert value['inactive_seconds'] is None


def test_latest_job_compares_real_time_across_legacy_local_and_utc_offsets(uid, monkeypatch):
    jobs = {
        'filter': dict(id='older', action='filter', status='done', started_at='2026-10-15T13:00:00+08:00'),
        'translate': dict(id='newer', action='translate', status='done', started_at='2026-10-15T05:10:00+00:00'),
    }
    monkeypatch.setattr(cd, 'read_job', lambda _project, action: dict(jobs[action]))
    monkeypatch.setattr(cs, '_jobs', {})
    monkeypatch.setattr(video_info, 'comment_progress', lambda _: dict(status='idle', busy=False), raising=False)
    assert cd.restored_job(crud.get_project(uid))['id'] == 'newer'
    assert cs.progress(uid)['job']['id'] == 'newer'


def test_lightweight_api_does_not_load_full_workspace_and_revision_covers_all_sources(uid, monkeypatch):
    project = crud.get_project(uid)
    seed(uid, 2)
    diag = new_diagnostics(uid)
    diag.job['status'] = 'done'
    diag.persist()
    monkeypatch.setattr(video_info, 'comment_progress', lambda _: dict(status='idle', busy=False, count=2, limit=50), raising=False)
    original_view = cs.view
    def forbidden(*_args, **_kwargs):
        raise AssertionError('轻量轮询不能读取完整工作区')
    monkeypatch.setattr(cs, 'view', forbidden)
    response = TestClient(app).get(f'/api/projects/{uid}/comments/progress')
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    state = response.json()
    assert state['jobs']['filter']['status'] == 'done'
    assert state['collection']['count'] == 2 and isinstance(state['data_revision'], str)
    assert state['data_revision'] == original_view(uid)['data_revision']
    root = crud.project_dir_by_project(project)
    revision = state['data_revision']
    for relative in ('comment_studio.json', 'artifacts/download/video_info.json', 'imports/bilibili_comments.json'):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, b'{"changed":true}', create_parent=False)
        current = cs._data_revision(project)
        assert current != revision
        revision = current


def test_batch_total_and_size_are_visible_during_actual_request(uid, monkeypatch):
    seed(uid, 12)
    observed = []
    def respond(prompt, **kwargs):
        rows = request_payload(prompt)['comments']
        current = cs._current_job(uid)
        observed.append((current['batch'], current['batches'], current['batch_size']))
        return validated_result({'assessments': [dict(id=row['id'], eligible=True, score=80, reason='有共鸣')
                                                   for row in rows]}, kwargs)
    monkeypatch.setattr(cs, 'chat_validated', respond)
    result = cs.run(uid, 'filter', prompt='观众反馈')
    assert observed == [(1, 2, 10), (2, 2, 2)]
    assert result['job']['phase'] == 'done' and result['job']['finished_at']
