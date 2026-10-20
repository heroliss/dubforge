"""显式生成复用运行中的逐句队列；模型替身验证真实调度与原子覆盖边界。"""
import json
from threading import Event

import pytest

from conftest import synthesize
from test_dubbing_preparation import index_prepared, prepared
from test_dubbing_tasks import finished, until
from test_live_dubbing import control, put
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import dubbing_edit, dubbing_task, live_dubbing
from dubforge.runner import runner


def enqueue(client, uid, ident, take, job_id, **body):
    return client.post(f'/api/projects/{uid}/workbench/synthesize', json={
        'line_ids': [ident], 'takes': {str(ident): take}, 'force': True,
        'append_to_job_id': job_id, **body})


def test_append_unmodified_sentence_uses_same_job_persisted_queue_and_explicit_slot(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    started, release, calls = control(monkeypatch)
    initial = runner.start_dubbing(uid, [1], takes={'1': 1})
    try:
        assert started[0].wait(10)
        response = enqueue(client, uid, 2, 2, initial['job_id'])
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['status'] == 'queued' and result['job_id'] == initial['job_id']
        assert result['lines'][0]['take'] == 2
        state = client.get(f'/api/projects/{uid}/workbench?probe=false').json()
        assert state['line_config_execution']['generation_accepting']
        assert state['synthesis_slots'] == [1, 2]
        assert state['synthesis_reserved_takes'] == {'1': [1], '2': [2]}
        assert state['synthesis_task']['total'] == 2
        assert result['queue_id'] in state['synthesis_task']['accepted_request_ids']
        assert state['lines'][1]['config_execution']['pending_revision'] == 0
        assert state['line_config_execution']['pending'] == 1
        assert calls == [('你好', 1.1)]
        repeated = enqueue(client, uid, 2, 2, initial['job_id']).json()
        assert repeated['lines'][0]['already_queued']
        release[0].set()
        assert started[1].wait(10)
        assert calls == [('你好', 1.1), ('世界', 1.1)]
        assert live_dubbing.view(crud.get_project(uid), 2)['running_revision'] == 0
    finally:
        for signal in release:
            signal.set()
    assert finished(project).status == 'done'
    final = client.get(f'/api/projects/{uid}/workbench?probe=false').json()
    assert final['synthesis_task']['completed'] == final['synthesis_task']['total'] == 2
    assert final['lines'][1]['selection']['take'] == 2
    assert final['lines'][1]['config_execution']['completed_revision'] == 0
    assert dubbing_edit.take_ids(crud.project_dir(uid), 2) == [2]
    assert crud.latest_job(project.id, 'tts').id == initial['job_id']


def test_main_plan_duplicate_is_idempotent_and_other_slot_is_explicit_conflict(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    started, release, calls = control(monkeypatch)
    initial = runner.start_dubbing(uid, [1, 2], takes={'1': 1, '2': 1})
    try:
        assert started[0].wait(10)
        for ident, status in [(1, 'running'), (2, 'queued')]:
            response = enqueue(client, uid, ident, 1, initial['job_id'])
            assert response.status_code == 200, response.text
            assert response.json()['lines'][0]['already_queued']
            assert response.json()['lines'][0]['status'] == status
            conflict = enqueue(client, uid, ident, 2, initial['job_id'])
            assert conflict.status_code == 400
            assert conflict.json()['code'] == 'TTS_QUEUE_TARGET_CONFLICT'
    finally:
        for signal in release:
            signal.set()
    assert finished(project).status == 'done'
    assert calls == [('你好', 1.1), ('世界', 1.1)]


def test_completed_main_sentence_can_be_generated_again_while_another_runs(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    started, release, calls = control(monkeypatch)
    initial = runner.start_dubbing(uid, [1, 2], takes={'1': 1, '2': 1})
    try:
        assert started[0].wait(10)
        release[0].set()
        assert started[1].wait(10)
        assert enqueue(client, uid, 1, 2, initial['job_id']).status_code == 200
        release[1].set()
        assert started[2].wait(10)
        assert calls == [('你好', 1.1), ('世界', 1.1), ('你好', 1.1)]
    finally:
        for signal in release:
            signal.set()
    assert finished(project).status == 'done'
    assert dubbing_edit.take_ids(crud.project_dir(uid), 1) == [1, 2]


def test_new_slot_then_live_parameter_changes_keep_target_before_and_during_generation(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    started, release, calls = control(monkeypatch)
    initial = runner.start_dubbing(uid, [1], takes={'1': 1})
    try:
        assert started[0].wait(10)
        result = enqueue(client, uid, 2, 3, initial['job_id']).json()
        assert put(client, uid, 2, .8, revision=0).status_code == 200
        release[0].set()
        assert started[1].wait(10)
        execution = live_dubbing.view(crud.get_project(uid), 2)
        assert execution['running_take'] == 3 and execution['request_id'] == result['queue_id']
        assert put(client, uid, 2, .7, revision=1).status_code == 200
        release[1].set()
        assert started[2].wait(10)
        assert calls == [('你好', 1.1), ('世界', .8), ('世界', .7)]
        assert live_dubbing.view(crud.get_project(uid), 2)['running_take'] == 3
    finally:
        for signal in release:
            signal.set()
    assert finished(project).status == 'done'
    assert dubbing_edit.take_ids(crud.project_dir(uid), 2) == [3]
    assert live_dubbing.view(crud.get_project(uid), 2)['completed_revision'] == 2


def test_non_force_append_reuses_valid_audio_without_changing_satisfaction(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    base = f'/api/projects/{uid}/workbench'
    assert synthesize(client, base + '/lines/2/synthesize', json={'take': 1}).status_code == 200
    assert client.post(base + '/lines/2/confirm', json={'confirmed': True}).status_code == 200
    root = crud.project_dir(uid)
    version = dubbing_edit.file_version(dubbing_edit.take_path(root, 2, 1))
    project = crud.get_project(uid)
    started, release, calls = control(monkeypatch)
    initial = runner.start_dubbing(uid, [1], takes={'1': 1})
    try:
        assert started[0].wait(10)
        response = client.post(base + '/lines/2/synthesize', json={
            'take': 1, 'append_to_job_id': initial['job_id']})
        assert response.status_code == 200, response.text
    finally:
        for signal in release:
            signal.set()
    assert finished(project).status == 'done'
    assert calls == [('你好', 1.1)]
    assert dubbing_edit.file_version(dubbing_edit.take_path(root, 2, 1)) == version
    state = client.get(base + '?probe=false').json()
    assert state['lines'][1]['confirmed']
    assert state['synthesis_task']['lines'][1]['status'] == 'skipped'


def test_cancel_interrupts_unmodified_queued_sentence_and_never_starts_another_job(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    started, release, calls = control(monkeypatch)
    initial = runner.start_dubbing(uid, [1], takes={'1': 1})
    try:
        assert started[0].wait(10)
        assert enqueue(client, uid, 2, 2, initial['job_id']).status_code == 200
        runner.cancel_step(uid, 'tts', expected_job_id=initial['job_id'])
        rejected = enqueue(client, uid, 2, 2, initial['job_id'])
        assert rejected.status_code == 400 and rejected.json()['code'] == 'LINE_UPDATE_QUEUE_CLOSED'
    finally:
        for signal in release:
            signal.set()
    assert finished(project).status == 'cancelled'
    assert calls == [('你好', 1.1)]
    assert live_dubbing.view(crud.get_project(uid), 2)['state'] == 'interrupted'
    after = enqueue(client, uid, 2, 2, initial['job_id'])
    assert after.status_code == 400 and after.json()['code'] == 'LINE_UPDATE_QUEUE_CLOSED'
    assert crud.latest_job(project.id, 'tts').id == initial['job_id']


def test_waiting_resource_allows_settings_save_but_does_not_guess_policy_target(index_prepared):
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    runner.acquire_slot('tts')
    try:
        initial = runner.start_step(uid, 'tts', dubbing=dict(mode='all', target='new'))
        state = live_dubbing.view(crud.get_project(uid))
        assert state['accepting'] and not state['generation_accepting']
        assert put(client, uid, 2, .8, revision=0).status_code == 200
        rejected = enqueue(client, uid, 1, 2, initial['job_id'])
        assert rejected.status_code == 400 and rejected.json()['code'] == 'LINE_UPDATE_QUEUE_CLOSED'
        runner.cancel_step(uid, 'tts', expected_job_id=initial['job_id'])
        assert finished(project).status == 'cancelled'
    finally:
        runner.release_slot('tts')


def test_final_layout_rejects_append_and_finished_job_guard_never_starts_new_task(index_prepared, monkeypatch):
    from dubforge.pipeline import step_tts
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    started, release = Event(), Event()
    original = step_tts._repair_voice_timing
    def layout(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(step_tts, '_repair_voice_timing', layout)
    initial = runner.start_dubbing(uid, [1], takes={'1': 1})
    try:
        assert started.wait(10)
        assert not live_dubbing.view(crud.get_project(uid))['generation_accepting']
        response = enqueue(client, uid, 2, 2, initial['job_id'])
        assert response.status_code == 400 and response.json()['code'] == 'LINE_UPDATE_QUEUE_CLOSED'
    finally:
        release.set()
    assert finished(project).status == 'done'
    response = enqueue(client, uid, 2, 2, initial['job_id'])
    assert response.status_code == 400 and response.json()['code'] == 'LINE_UPDATE_QUEUE_CLOSED'
    assert crud.latest_job(project.id, 'tts').id == initial['job_id']


def test_generation_failure_preserves_old_slot_and_fails_requested_batch(index_prepared, monkeypatch):
    from dubforge.backends import tts_indextts25
    client, uid, _ = index_prepared
    base = f'/api/projects/{uid}/workbench'
    assert synthesize(client, base + '/lines/2/synthesize', json={'take': 1}).status_code == 200
    assert client.post(base + '/lines/2/confirm', json={'confirmed': True}).status_code == 200
    project, root = crud.get_project(uid), crud.project_dir(uid)
    previous = dubbing_edit.take_path(root, 2, 1).read_bytes()
    started, release = Event(), Event()
    original = tts_indextts25.synth
    def controlled(*args, **kwargs):
        if args[1] == '你好':
            started.set()
            assert release.wait(10)
        else:
            raise DubForgeError('模型未能合成此句', code='TTS_FAILED')
        return original(*args, **kwargs)
    monkeypatch.setattr(tts_indextts25, 'synth', controlled)
    initial = runner.start_dubbing(uid, [1], takes={'1': 1})
    try:
        assert started.wait(10)
        assert enqueue(client, uid, 2, 1, initial['job_id']).status_code == 200
    finally:
        release.set()
    assert finished(project).status == 'failed'
    assert dubbing_edit.take_path(root, 2, 1).read_bytes() == previous
    state = client.get(base + '?probe=false').json()
    assert state['lines'][1]['confirmed']
    assert state['synthesis_task']['failed'] == 1 and state['synthesis_task']['total'] == 2


def test_unrelated_new_slot_does_not_invalidate_active_confirmation_but_target_reservation_does(index_prepared, monkeypatch):
    from test_dubbing_confirmation import expected
    from dubforge.utils.fs import atomic_write_bytes
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    started, release, calls = control(monkeypatch)
    initial = runner.start_dubbing(uid, [1], takes={'1': 1})
    try:
        assert started[0].wait(10)
        snapshot = client.get(f'/api/projects/{uid}/workbench?probe=false').json()
        # 仅别句的新槽变化不改变第二句的确认内容或目标槽。
        atomic_write_bytes(crud.project_dir(uid) / 'artifacts/tts/1_v4.wav', b'other-slot')
        response = enqueue(client, uid, 2, 2, initial['job_id'], expected=expected(snapshot, ids=(2,)))
        assert response.status_code == 200, response.text
        # 另窗口已预留目标 2，旧目录下的新建操作不能悄悄复用这个槽。
        conflict = enqueue(client, uid, 2, 2, initial['job_id'], expected=expected(snapshot, ids=(2,)))
        assert conflict.status_code == 400 and conflict.json()['code'] == 'TTS_CONFIRMATION_CHANGED'
    finally:
        for signal in release:
            signal.set()
    assert finished(project).status == 'done'


def test_stale_generation_control_reports_interrupted_without_real_task(index_prepared):
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    params = crud.project_params(project)
    params[live_dubbing.KEY] = dict(job_id=999, accepting=True, accepted_request_ids=['old-request'],
        lines={'2': dict(kind='generation', state='queued', target_take=2,
                         requested_revision=0, pending_revision=0)})
    project.params = json.dumps(params)
    crud.save_project(project)
    state = live_dubbing.view(project)
    assert not state['active'] and not state['generation_accepting']
    assert state['lines']['2']['state'] == 'interrupted'
    assert state['pending'] == 0 and not live_dubbing.reserved_takes(project)


@pytest.mark.parametrize('status', ['running', 'failed'])
def test_completed_generation_receipt_does_not_hide_later_timing_status(index_prepared, monkeypatch, status):
    from dubforge.pipeline import step_tts
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    reached, finish_layout = Event(), Event()
    original = step_tts._repair_voice_timing
    def layout(ctx, *args, **kwargs):
        target = next(row for row in ctx.task_data['lines'] if row['id'] == 2)
        target.update(status=status, timing_phase='shortening')
        if status == 'failed':
            target['error'] = '缩时未完成'
        ctx.save_task_data()
        reached.set()
        assert finish_layout.wait(10)
        return original(ctx, *args, **kwargs)
    monkeypatch.setattr(step_tts, '_repair_voice_timing', layout)
    started, release, calls = control(monkeypatch)
    initial = runner.start_dubbing(uid, [1], takes={'1': 1})
    try:
        assert started[0].wait(10)
        result = enqueue(client, uid, 2, 2, initial['job_id']).json()
        for signal in release:
            signal.set()
        assert reached.wait(10)
        current = dubbing_task.view(project.id)
        row = next(row for row in current['lines'] if row['id'] == 2)
        assert row['status'] == status and row['timing_phase'] == 'shortening'
        assert row['request_id'] == result['queue_id']
        if status == 'failed':
            assert row['error'] == '缩时未完成'
    finally:
        for signal in release:
            signal.set()
        finish_layout.set()
    finished(project)


def test_cached_then_forced_generation_clears_cached_receipt(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    base = f'/api/projects/{uid}/workbench'
    assert synthesize(client, base + '/lines/2/synthesize', json={'take': 1}).status_code == 200
    project = crud.get_project(uid)
    cached, allow_next = Event(), Event()
    original = live_dubbing.complete_cached
    def complete(*args, **kwargs):
        original(*args, **kwargs)
        cached.set()
        assert allow_next.wait(10)
    monkeypatch.setattr(live_dubbing, 'complete_cached', complete)
    started, release, calls = control(monkeypatch)
    initial = runner.start_dubbing(uid, [1], takes={'1': 1})
    try:
        assert started[0].wait(10)
        assert enqueue(client, uid, 2, 1, initial['job_id'], force=False).status_code == 200
        release[0].set()
        assert cached.wait(10)
        assert enqueue(client, uid, 2, 1, initial['job_id'], force=True).status_code == 200
        assert not live_dubbing.view(crud.get_project(uid), 2)['cached']
        allow_next.set()
        assert started[1].wait(10)
    finally:
        for signal in release:
            signal.set()
        allow_next.set()
    assert finished(project).status == 'done'
    row = next(row for row in dubbing_task.view(project.id)['lines'] if row['id'] == 2)
    assert row['status'] == 'done'
    assert not live_dubbing.view(crud.get_project(uid), 2)['cached']
    assert calls == [('你好', 1.1), ('世界', 1.1)]


def test_late_append_failure_is_counted_after_final_close_boundary(index_prepared, monkeypatch):
    from dubforge.backends import tts_indextts25
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    closing, allow_close = Event(), Event()
    original_close, original_synth = live_dubbing.close_if_empty, tts_indextts25.synth
    encountered = []
    def close(ctx):
        if not encountered:
            encountered.append(True)
            closing.set()
            assert allow_close.wait(10)
        return original_close(ctx)
    def synth(*args, **kwargs):
        if args[1] == '世界':
            raise DubForgeError('末尾追加合成失败', code='TTS_FAILED')
        return original_synth(*args, **kwargs)
    monkeypatch.setattr(live_dubbing, 'close_if_empty', close)
    monkeypatch.setattr(tts_indextts25, 'synth', synth)
    initial = runner.start_dubbing(uid, [1], takes={'1': 1})
    try:
        assert closing.wait(10)
        response = enqueue(client, uid, 2, 2, initial['job_id'])
        assert response.status_code == 200, response.text
    finally:
        allow_close.set()
    assert finished(project).status == 'failed'
    state = dubbing_task.view(project.id)
    assert state['failed'] == 1 and state['total'] == 2


def test_force_append_upgrades_main_plan_that_would_only_reuse_valid_audio(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    base = f'/api/projects/{uid}/workbench'
    assert synthesize(client, base + '/lines/2/synthesize', json={'take': 1}).status_code == 200
    project = crud.get_project(uid)
    started, release, calls = control(monkeypatch)
    initial = runner.start_dubbing(uid, [1, 2], takes={'1': 1, '2': 1}, force=False)
    try:
        assert started[0].wait(10)
        response = enqueue(client, uid, 2, 1, initial['job_id'])
        assert response.status_code == 200, response.text
        assert not response.json()['lines'][0].get('already_queued')
        release[0].set()
        assert started[1].wait(10)
    finally:
        for signal in release:
            signal.set()
    assert finished(project).status == 'done'
    assert calls == [('你好', 1.1), ('世界', 1.1)]


def test_explicit_other_slot_cannot_replace_pending_parameter_update_target(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    started, release, calls = control(monkeypatch)
    initial = runner.start_dubbing(uid, [1], takes={'1': 1})
    try:
        assert started[0].wait(10)
        assert put(client, uid, 2, .8, revision=0).status_code == 200
        response = enqueue(client, uid, 2, 2, initial['job_id'])
        assert response.status_code == 400 and response.json()['code'] == 'TTS_QUEUE_TARGET_CONFLICT'
    finally:
        for signal in release:
            signal.set()
    assert finished(project).status == 'done'
    assert calls == [('你好', 1.1), ('世界', .8)]
    assert dubbing_edit.take_ids(crud.project_dir(uid), 2) == [1]


def test_append_does_not_clear_chain_or_change_existing_policy_operation(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    started, release, calls = control(monkeypatch)
    cleared = []
    monkeypatch.setattr(runner, '_clear_chain', lambda *args, **kwargs: cleared.append(args))
    initial = runner.start_step(uid, 'tts', dubbing=dict(mode='all', target='new'),
                                dubbing_operation_id='policy-must-survive')
    try:
        assert started[0].wait(10)
        release[0].set()
        assert started[1].wait(10)
        response = enqueue(client, uid, 1, 2, initial['job_id'])
        assert response.status_code == 200, response.text
    finally:
        for signal in release:
            signal.set()
    job = finished(project)
    assert job.status == 'done'
    task = json.loads(job.task_data)
    assert task['scope'] == 'policy' and task['operation_id'] == 'policy-must-survive'
    assert cleared == [] and calls == [('你好', 1.1), ('世界', 1.1), ('你好', 1.1)]


@pytest.mark.parametrize(('pending_line', 'expected_take'), [(1, 3), (2, 2)])
def test_new_slot_counts_only_selected_sentence_pending_reservations(index_prepared, pending_line, expected_take):
    from dubforge.pipeline.dependencies import Dependencies
    from dubforge.utils.fs import atomic_write_bytes
    client, uid, _ = index_prepared
    project = crud.get_project(uid)
    atomic_write_bytes(dubbing_edit.take_path(crud.project_dir(uid), 1, 1), b'previous-slot-1')
    runner.acquire_slot('tts')
    try:
        initial = runner.start_dubbing(uid, [pending_line], takes={str(pending_line): 2})
        fresh = crud.get_project(uid)
        assert crud.get_step(project.id, 'tts').status == 'queued'
        assert live_dubbing.reserved_takes_by_line(fresh) == {pending_line: {2}}
        assert live_dubbing.reserved_takes(fresh, [1]) == ({2} if pending_line == 1 else set())
        state = client.get(f'/api/projects/{uid}/workbench?probe=false').json()
        assert state['synthesis_reserved_takes'] == {str(pending_line): [2]}
        assert state['synthesis_slots'] == [1, 2]
        result = dubbing_task.prepare(fresh, Dependencies(fresh), [1], takes={'1': None})
        assert result['lines'] == [{'id': 1, 'take': expected_take, 'status': 'queued'}]
        runner.cancel_step(uid, 'tts', expected_job_id=initial['job_id'])
        assert finished(project).status == 'cancelled'
        assert not live_dubbing.reserved_takes_by_line(crud.get_project(uid))
    finally:
        runner.release_slot('tts')


def test_finished_or_failed_rows_and_old_control_do_not_reserve_slots(prepared, monkeypatch):
    from types import SimpleNamespace
    uid, project, _ = prepared
    job = SimpleNamespace(id=73)
    monkeypatch.setattr(live_dubbing, '_active', lambda _: (job, True, False))
    monkeypatch.setattr(live_dubbing, '_task', lambda *_: (None, {'lines': [
        {'id': 1, 'take': 2, 'status': 'failed'},
        {'id': 1, 'take': 3, 'status': 'done'},
        {'id': 1, 'take': 4, 'status': 'skipped'},
        {'id': 2, 'take': 1, 'status': 'running'},
    ]}))
    control = {'job_id': job.id, 'lines': {
        '1': {'target_take': 5, 'state': 'failed'},
        '2': {'target_take': 2, 'state': 'queued'},
    }}
    monkeypatch.setattr(live_dubbing, '_control', lambda _: control)
    assert live_dubbing.reserved_takes_by_line(project) == {2: {1, 2}}
    assert live_dubbing.reserved_takes(project, [1]) == set()
    assert live_dubbing.reserved_takes(project) == {1, 2}
    control['job_id'] = job.id - 1
    assert live_dubbing.reserved_takes_by_line(project) == {2: {1}}
    monkeypatch.setattr(live_dubbing, '_active', lambda _: (job, False, False))
    assert live_dubbing.reserved_takes_by_line(project) == {}
