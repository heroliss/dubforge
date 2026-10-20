"""运行中逐句精调的真实调度边界；服务全部替身，不接触真实配音模型。"""
import json
from threading import Event

import pytest

from conftest import synthesize
from test_dubbing_preparation import index_prepared, prepared
from test_dubbing_tasks import finished, until
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import dubbing_edit, live_dubbing
from dubforge.pipeline.dependencies import Dependencies
from dubforge.runner import runner

FACTOR = 'tts.indextts25.duration_factor'


def put(client, uid, ident, factor, **body):
    return client.put(f'/api/projects/{uid}/workbench/lines/{ident}/params',
                      json={'updates': {FACTOR: factor}, **body})


def control(monkeypatch, count=8):
    from dubforge.backends import tts_indextts25
    original = tts_indextts25.synth
    started, release, calls = [Event() for _ in range(count)], [Event() for _ in range(count)], []
    def synth(*args, **kwargs):
        number = len(calls)
        calls.append((args[1], kwargs['duration_factor']))
        started[number].set()
        assert release[number].wait(10), '测试没有释放模型替身'
        return original(*args, **kwargs)
    monkeypatch.setattr(tts_indextts25, 'synth', synth)
    return started, release, calls


def test_running_request_is_frozen_future_updates_merge_and_edit_during_update_runs_latest(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    started, release, calls = control(monkeypatch)
    runner.start_dubbing(uid, [1, 2])
    try:
        assert started[0].wait(10)
        assert put(client, uid, 2, .8, revision=0).json()['revision'] == 1
        assert put(client, uid, 2, .7, revision=1).json()['revision'] == 2
        result = put(client, uid, 1, .9, revision=0)
        assert result.status_code == 200
        assert result.json()['execution']['state'] == 'queued'
        assert calls == [('你好', 1.1)]
        release[0].set()
        assert started[1].wait(10)
        state = client.get(f'/api/projects/{uid}/workbench/lines/2/params').json()
        assert state['execution']['running_revision'] == 2
        assert put(client, uid, 2, .6, revision=2).json()['execution']['pending_revision'] == 3
        assert calls == [('你好', 1.1), ('世界', .7)]
        release[1].set()
        assert started[2].wait(10)
        assert calls[-1] == ('世界', .6)
        release[2].set()
        assert started[3].wait(10)
        assert calls[-1] == ('你好', .9)
    finally:
        for event in release:
            event.set()
    assert finished(project).status == 'done'
    assert len(calls) == 4
    state = client.get(f'/api/projects/{uid}/workbench?probe=false').json()
    assert state['lines'][0]['selection']['take'] == 1
    assert state['lines'][1]['selection']['take'] == 1
    assert state['lines'][1]['config_execution']['completed_revision'] == 3
    assert state['line_config_execution']['pending'] == state['line_config_execution']['running'] == 0
    assert not any(line['wav'].get('settings_changed') for line in state['lines'])
    step = next(row for row in client.get(f'/api/projects/{uid}/steps').json() if row['key'] == 'tts')
    assert step['fresh'] and not step['settings_changed'] and not step['upstream_changed']


def test_update_failure_keeps_old_selected_audio_and_satisfaction_and_continues_original_batch(index_prepared, monkeypatch):
    from dubforge.backends import tts_indextts25
    client, uid, _ = index_prepared
    base = f'/api/projects/{uid}/workbench'
    synthesize(client, base + '/synthesize', json={'line_ids': [1, 2]})
    client.post(base + '/lines/1/confirm', json={'confirmed': True})
    original = tts_indextts25.synth
    start, release = Event(), Event()
    calls = []
    def synth(*args, **kwargs):
        calls.append(args[1])
        if len(calls) == 1:
            start.set()
            assert release.wait(10)
        if args[1] == '你好':
            raise DubForgeError('精调参考不适用', code='TTS_FAILED')
        return original(*args, **kwargs)
    monkeypatch.setattr(tts_indextts25, 'synth', synth)
    project = crud.get_project(uid)
    old = (crud.project_dir(uid) / 'artifacts/tts/1.wav').read_bytes()
    runner.start_dubbing(uid, [2])
    try:
        assert start.wait(10)
        assert put(client, uid, 1, .8).status_code == 200
    finally:
        release.set()
    assert finished(project).status == 'done'
    state = client.get(base + '?probe=false').json()
    assert state['lines'][0]['selection']['take'] == 1
    assert state['lines'][0]['confirmed'] is True
    assert state['lines'][0]['config_execution']['state'] == 'failed'
    assert state['lines'][0]['config_execution']['error'] == '精调参考不适用'
    assert (crud.project_dir(uid) / 'artifacts/tts/1.wav').read_bytes() == old
    assert calls == ['世界', '你好']
    # 新一轮正常生成已经解决本句，不必改项目里的旧运行簿记来清理失败标签。
    monkeypatch.setattr(tts_indextts25, 'synth', original)
    runner.start_dubbing(uid, [1])
    assert finished(project).status == 'done'
    assert client.get(base + '?probe=false').json()['lines'][0]['config_execution']['state'] == 'saved'


def test_only_save_does_not_hot_change_remaining_requests(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    started, release, calls = control(monkeypatch)
    project = crud.get_project(uid)
    runner.start_dubbing(uid, [1, 2])
    try:
        assert started[0].wait(10)
        assert put(client, uid, 2, .8, execution='save').status_code == 200
        release[0].set()
        assert started[1].wait(10)
        assert calls == [('你好', 1.1), ('世界', 1.1)]
    finally:
        for event in release:
            event.set()
    assert finished(project).status == 'done'
    assert client.get(f'/api/projects/{uid}/workbench/lines/2/params').json()['steps']['tts']['values'][FACTOR] == .8
    state = client.get(f'/api/projects/{uid}/workbench?probe=false').json()
    assert state['lines'][1]['wav']['exists'] and state['lines'][1]['wav']['settings_changed']


def test_cancel_drops_pending_work_and_save_never_starts_another_task(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    started, release, calls = control(monkeypatch)
    project = crud.get_project(uid)
    result = runner.start_dubbing(uid, [1, 2])
    try:
        assert started[0].wait(10)
        assert put(client, uid, 2, .8).status_code == 200
        runner.cancel_step(uid, 'tts', expected_job_id=result['job_id'])
        response = put(client, uid, 1, .9)
        assert response.status_code == 400 and response.json()['code'] == 'LINE_UPDATE_QUEUE_CLOSED'
        assert put(client, uid, 1, .9, execution='save').status_code == 200
    finally:
        for event in release:
            event.set()
    assert finished(project).status == 'cancelled'
    assert calls == [('你好', 1.1)]
    assert live_dubbing.view(crud.get_project(uid), 2)['state'] == 'interrupted'
    assert put(client, uid, 1, .95).status_code == 200
    assert crud.latest_job(project.id, 'tts').id == result['job_id']


def test_revision_conflicts_and_idle_save_does_not_create_job(index_prepared):
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    assert put(client, uid, 1, .8, revision=0).json()['revision'] == 1
    result = put(client, uid, 1, .9, revision=0)
    assert result.status_code == 400 and result.json()['code'] == 'LINE_SETTINGS_CONFLICT'
    assert crud.latest_job(project.id, 'tts') is None
    state = client.get(f'/api/projects/{uid}/workbench/lines/1/params').json()
    assert state['steps']['tts']['values'][FACTOR] == .8
    assert state['execution']['state'] == 'saved'


def test_explicit_live_settings_override_project_policy_without_changing_other_sentences(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    assert put(client, uid, 1, .7).status_code == 200
    project = crud.get_project(uid)
    started, release, calls = control(monkeypatch)
    runner.start_step(uid, 'tts', dubbing=dict(mode='all', target='new', line_settings='project'))
    try:
        assert started[0].wait(10)
        assert calls == [('你好', 1.1)]
        assert put(client, uid, 1, .8, revision=1).status_code == 200
        release[0].set()
        assert started[1].wait(10)
        assert calls[-1] == ('你好', .8)
        release[1].set()
        assert started[2].wait(10)
        assert calls[-1] == ('世界', 1.1)
    finally:
        for event in release:
            event.set()
    assert finished(project).status == 'done'
    root = crud.project_dir(uid)
    line = Dependencies(crud.get_project(uid)).dubbing_lines()[0]
    record = dubbing_edit.read_take(root, line, 1)
    assert record['line_settings_mode'] == 'preserve' and record['live_revision'] == 2


def test_final_layout_closes_queue_atomically_but_allows_save_for_next_run(index_prepared, monkeypatch):
    from dubforge.pipeline import step_tts
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    original = step_tts._repair_voice_timing
    start, release = Event(), Event()
    def layout(*args, **kwargs):
        start.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(step_tts, '_repair_voice_timing', layout)
    runner.start_dubbing(uid, [1, 2])
    try:
        assert start.wait(10)
        response = put(client, uid, 1, .8)
        assert response.status_code == 400 and response.json()['code'] == 'LINE_UPDATE_QUEUE_CLOSED'
        assert put(client, uid, 1, .8, execution='save').status_code == 200
    finally:
        release.set()
    assert finished(project).status == 'done'


def test_stale_persisted_queue_is_interrupted_without_live_task(index_prepared):
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    params = crud.project_params(project)
    params[live_dubbing.KEY] = dict(job_id=999, accepting=True,
        lines={'1': dict(state='running', requested_revision=1, running_revision=1, pending_revision=2)})
    project.params = json.dumps(params)
    crud.save_project(project)
    state = live_dubbing.view(project, 1)
    assert state['state'] == 'interrupted' and not state['active'] and not state['accepting']
    assert live_dubbing.view(project)['pending'] == live_dubbing.view(project)['running'] == 0
    assert crud.latest_job(project.id, 'tts') is None


def test_full_slot_inventory_does_not_block_overwriting_selected_take(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    started, release, calls = control(monkeypatch)
    runner.start_dubbing(uid, [1, 2])
    original = dubbing_edit.take_ids
    try:
        assert started[0].wait(10)
        monkeypatch.setattr(dubbing_edit, 'take_ids', lambda root, ident: [10000] if ident == 1 else original(root, ident))
        assert put(client, uid, 1, .8).status_code == 200
        release[0].set()
        assert started[1].wait(10)
        state = client.get(f'/api/projects/{uid}/workbench/lines/1/params').json()['execution']
        assert state['state'] == 'running' and state['running_take'] == 1
        assert calls == [('你好', 1.1), ('你好', .8)]
    finally:
        for event in release:
            event.set()
    assert finished(project).status == 'done'


def test_failed_live_update_can_be_explicitly_retried_without_new_revision_or_new_job(index_prepared, monkeypatch):
    from dubforge.backends import tts_indextts25
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    original = tts_indextts25.synth
    started, release = Event(), Event()
    second_started, second_release = Event(), Event()
    calls = []
    def synth(*args, **kwargs):
        calls.append((args[1], kwargs['duration_factor']))
        if len(calls) == 1:
            started.set()
            assert release.wait(10)
        if len(calls) == 2:
            raise DubForgeError('瞬时失败', code='TTS_FAILED')
        if len(calls) == 3:
            second_started.set()
            assert second_release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(tts_indextts25, 'synth', synth)
    result = runner.start_dubbing(uid, [1, 2])
    try:
        assert started.wait(10)
        assert put(client, uid, 1, .8).json()['revision'] == 1
        release.set()
        assert second_started.wait(10)
        retry = client.put(f'/api/projects/{uid}/workbench/lines/1/params',
                           json={'execution': 'retry', 'revision': 1})
        assert retry.status_code == 200
        assert retry.json()['revision'] == 1 and retry.json()['execution']['state'] == 'queued'
    finally:
        release.set()
        second_release.set()
    assert finished(project).status == 'done'
    assert calls == [('你好', 1.1), ('你好', .8), ('世界', 1.1), ('你好', .8)]
    assert crud.latest_job(project.id, 'tts').id == result['job_id']
    retry = client.put(f'/api/projects/{uid}/workbench/lines/1/params', json={'execution': 'retry', 'revision': 1})
    assert retry.status_code == 400 and retry.json()['code'] == 'LINE_UPDATE_RETRY_UNAVAILABLE'
    assert crud.latest_job(project.id, 'tts').id == result['job_id']


@pytest.mark.parametrize('fault', ['metadata', 'receipt'])
def test_live_update_commit_failures_keep_selection_queue_and_target_consistent(index_prepared, monkeypatch, fault):
    from dubforge.backends import tts_indextts25
    from dubforge.pipeline import step_tts
    from sqlmodel import Session
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    started, release, calls = control(monkeypatch)
    if fault == 'metadata':
        original = step_tts.atomic_write_bytes
        def write(path, data):
            if path.name.startswith('.live-1-') and str(path).endswith('.take.json') and b'"live_revision"' in data:
                raise OSError('模拟精调元数据写入失败')
            return original(path, data)
        monkeypatch.setattr(step_tts, 'atomic_write_bytes', write)
    else:
        original = Session.exec
        injected = []
        def execute(session, statement, *args, **kwargs):
            if (not injected and getattr(statement, 'is_update', False)
                    and statement.table.name == 'job' and 'task_data' in str(statement)
                    and len(calls) == 2):
                injected.append(True)
                raise OSError('模拟任务回执写入失败')
            return original(session, statement, *args, **kwargs)
        monkeypatch.setattr(Session, 'exec', execute)
    runner.start_dubbing(uid, [1, 2])
    try:
        assert started[0].wait(10)
        assert put(client, uid, 1, .8).status_code == 200
        release[0].set()
        assert started[1].wait(10)
        release[1].set()
        assert started[2].wait(10)
        state = client.get(f'/api/projects/{uid}/workbench?probe=false').json()
        assert state['lines'][0]['selection']['take'] == 1
        assert state['lines'][0]['config_execution']['state'] == 'failed'
    finally:
        for event in release:
            event.set()
    job = finished(project)
    assert job.status == 'done'
    targets = json.loads(job.task_data)['lines']
    assert targets[0]['take'] == 1 and not targets[0].get('live_updated')
    assert calls == [('你好', 1.1), ('你好', .8), ('世界', 1.1)]


def test_only_save_message_distinguishes_latest_saved_revision_from_older_queued_request(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    started, release, calls = control(monkeypatch)
    runner.start_dubbing(uid, [1, 2])
    try:
        assert started[0].wait(10)
        assert put(client, uid, 1, .8).status_code == 200
        result = put(client, uid, 1, .9, execution='save')
        assert result.status_code == 200
        execution = result.json()['execution']
        assert execution['revision'] == 2 and execution['requested_revision'] == 1
        assert 'r2' in execution['message'] and '仅保存' in execution['message'] and 'r1' in execution['message']
        release[0].set()
        assert started[1].wait(10)
        assert calls[-1] == ('你好', .8)
    finally:
        for event in release:
            event.set()
    assert finished(project).status == 'done'
    state = client.get(f'/api/projects/{uid}/workbench/lines/1/params').json()['execution']
    assert state['completed_revision'] == 1 and state['revision'] == 2
    assert '尚未应用' in state['message']


@pytest.mark.parametrize('fault', ['metadata', 'receipt', 'commit', 'cancel_after_publish'])
def test_overwrite_failure_restores_old_files_versions_satisfaction_and_artifact_index(index_prepared, monkeypatch, fault):
    from dubforge.backends import tts_indextts25
    from dubforge.pipeline import step_tts
    from dubforge.domain.models import Artifact
    from dubforge.runner import StepContext, StepCancelled
    from sqlmodel import Session
    client, uid, _ = index_prepared
    base = f'/api/projects/{uid}/workbench'
    root = crud.project_dir(uid)
    assert synthesize(client, base + '/lines/1/synthesize', json={'take': 2}).status_code == 200
    assert client.post(base + '/lines/1/confirm', json={'confirmed': True}).status_code == 200
    path = dubbing_edit.take_path(root, 1, 2)
    before = {suffix: (path.with_suffix(suffix).read_bytes(), path.with_suffix(suffix).stat().st_mtime_ns)
              for suffix in ('.wav', '.input', '.take.json')}
    artifact = crud.latest_artifacts(crud.get_project(uid).id, ['tts'])['tts/1_v2.wav']
    started, release, injected = Event(), Event(), []
    original_synth = tts_indextts25.synth
    def synth(*args, **kwargs):
        if args[1] == '世界':
            started.set()
            assert release.wait(10)
        return original_synth(*args, **kwargs)
    monkeypatch.setattr(tts_indextts25, 'synth', synth)
    if fault == 'metadata':
        original = step_tts.atomic_write_bytes
        def write(destination, data):
            if destination.name.startswith('.live-1-') and b'"live_revision"' in data:
                injected.append(True)
                raise OSError('模拟精调元数据失败')
            return original(destination, data)
        monkeypatch.setattr(step_tts, 'atomic_write_bytes', write)
    elif fault == 'receipt':
        original = Session.exec
        def execute(session, statement, *args, **kwargs):
            if (not injected and getattr(statement, 'is_update', False)
                    and statement.table.name == 'job' and 'task_data' in str(statement)
                    and '"live_updated": true' in str(statement.compile().params)):
                injected.append(True)
                raise OSError('模拟回执失败')
            return original(session, statement, *args, **kwargs)
        monkeypatch.setattr(Session, 'exec', execute)
    elif fault == 'commit':
        original = Session.commit
        def commit(session):
            if not injected and any(isinstance(row, Artifact) and row.name == '1_v2.wav' for row in session.new):
                injected.append(True)
                # 证明故障发生在正式文件已替换之后，而不只是候选生成期间。
                assert json.loads(path.with_suffix('.take.json').read_text())['live_revision'] == 1
                raise OSError('模拟发布后数据库提交失败')
            return original(session)
        monkeypatch.setattr(Session, 'commit', commit)
    else:
        original = StepContext.check_cancel
        def check_cancel(ctx):
            current = json.loads(path.with_suffix('.take.json').read_text())
            if not injected and current.get('live_revision') == 1:
                injected.append(True)
                ctx._cancel.set()
                raise StepCancelled('模拟正式发布后取消')
            return original(ctx)
        monkeypatch.setattr(StepContext, 'check_cancel', check_cancel)
    project = crud.get_project(uid)
    runner.start_dubbing(uid, [2])
    try:
        assert started.wait(10)
        assert put(client, uid, 1, .8).status_code == 200
    finally:
        release.set()
    assert finished(project).status == ('cancelled' if fault == 'cancel_after_publish' else 'done')
    assert injected
    assert before == {suffix: (path.with_suffix(suffix).read_bytes(), path.with_suffix(suffix).stat().st_mtime_ns)
                      for suffix in before}
    current = client.get(base + '?probe=false').json()['lines'][0]
    assert current['selection']['take'] == 2 and current['confirmed'] and current['wav']['exists']
    assert dubbing_edit.take_ids(root, 1) == [2]
    assert crud.latest_artifacts(project.id, ['tts'])['tts/1_v2.wav'].id == artifact.id
    assert not list(path.parent.glob('.live-*')) and not list(path.parent.glob('*.rollback-*'))


def test_live_overwrite_success_reuses_slot_and_requires_listening_again(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    base = f'/api/projects/{uid}/workbench'
    assert synthesize(client, base + '/lines/1/synthesize', json={'take': 2}).status_code == 200
    assert client.post(base + '/lines/1/confirm', json={'confirmed': True}).status_code == 200
    project, root = crud.get_project(uid), crud.project_dir(uid)
    before = dubbing_edit.file_version(dubbing_edit.take_path(root, 1, 2))
    started, release, calls = control(monkeypatch)
    runner.start_dubbing(uid, [2])
    try:
        assert started[0].wait(10)
        assert put(client, uid, 1, .8).status_code == 200
    finally:
        for event in release:
            event.set()
    assert finished(project).status == 'done'
    current = client.get(base + '?probe=false').json()['lines'][0]
    assert current['selection']['take'] == 2 and current['wav']['exists'] and not current['confirmed']
    assert dubbing_edit.take_ids(root, 1) == [2]
    assert dubbing_edit.file_version(dubbing_edit.take_path(root, 1, 2)) != before
    assert not list((root / 'artifacts/tts').glob('.live-*'))
    assert not list((root / 'artifacts/tts').glob('*.rollback-*'))
