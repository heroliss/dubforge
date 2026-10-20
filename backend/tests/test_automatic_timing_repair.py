"""自动流程复用旧配音时也能修复排布；不连接真实模型或发布平台。"""
import json
import time
from uuid import uuid4

import pytest

from test_configuration import save
from test_dubbing_preparation import index_prepared, prepared
from test_voice_timing_integration import fake_index, timeline, start_selected, report
from dubforge.domain import crud
from dubforge.pipeline import dubbing_edit, dubbing_policy, voice_timing
from dubforge.pipeline.dependencies import Dependencies
from dubforge.runner import runner


@pytest.fixture
def old_conflict(index_prepared, monkeypatch):
    client, uid, _ = index_prepared
    monkeypatch.setattr(runner, '_prepare_gpu', lambda *_: None)
    timeline(uid, [(0, 1), (1, 2)])
    monkeypatch.setattr(voice_timing, 'video_seconds', lambda *_: 2)
    calls = fake_index(monkeypatch, lambda text, factor: .5 if text == '世界' else
                       2 if factor > .7 else 1.3)
    save('project:' + uid, {'tts.placement.enabled': False,
                           'tts.indextts25.duration_retry_factors': ''})
    assert start_selected(client, uid).status == 'done'
    save('project:' + uid, {'tts.placement.enabled': True,
                           'tts.indextts25.duration_retry_factors': '.7, .5'})
    return client, uid, calls


def finish(uid, **policy):
    result = runner.start_step(uid, 'tts', dubbing={'mode': 'fill', **policy},
                               dubbing_operation_id=uuid4().hex)
    assert result['status'] == 'started', result
    deadline = time.monotonic() + 15
    while runner.current_job_id(crud.get_project(uid).id, 'tts') is not None:
        assert time.monotonic() < deadline
        time.sleep(.01)
    return crud.latest_job(crud.get_project(uid).id, 'tts')


def test_existing_conflicts_enter_repair_without_precheck_model_calls(old_conflict):
    from dubforge.workflow_plan import production_checks
    _, uid, calls = old_conflict
    deps = Dependencies(crud.get_project(uid))
    summary = dubbing_policy.preview(deps.project, dependencies=deps)
    assert summary['generate'] == 0 and summary['repair'] > 0
    assert not production_checks(deps, ['tts', 'assemble'], dubbing_summary=summary)
    recovery = {'rows': [], 'execute_steps': ['assemble'], 'reusable_steps': ['tts']}
    dubbing_policy.apply_recovery(summary, recovery)
    assert recovery['start_step'] == 'tts' and recovery['execute_steps'] == ['tts', 'assemble']
    assert calls == [('你好', 1.1), ('世界', 1.1)]


def test_fill_repairs_current_slot_and_preserves_original_sound_and_parameters(old_conflict):
    client, uid, calls = old_conflict
    root = crud.project_dir(uid)
    base = f'/api/projects/{uid}/workbench'
    for line_id in (1, 2):
        assert client.post(base + f'/lines/{line_id}/confirm', json={'confirmed': True}).status_code == 200
    line = Dependencies(crud.get_project(uid)).dubbing_lines()[0]
    old = dubbing_edit.take_path(root, 1, 1).read_bytes()
    other = dubbing_edit.take_path(root, 2, 1).read_bytes()
    marker = dubbing_edit.take_path(root, 1, 1).with_suffix('.input').read_text()
    assert client.put(base + '/lines/1/params', json={'updates': {
        'tts.indextts25.temperature': .33}}).status_code == 200
    before_params = crud.get_project(uid).params
    job = finish(uid)
    assert job.status == 'done', job.error_detail
    assert calls == [('你好', 1.1), ('世界', 1.1), ('你好', .7)]
    assert report(uid)['ready']
    assert crud.get_project(uid).params == before_params
    assert dubbing_edit.take_path(root, 1, 1).with_suffix('.input').read_text() == marker
    assert dubbing_edit.take_path(root, 2, 1).read_bytes() == other
    assert dubbing_edit.take_ids(root, 1) == [1]
    current = dubbing_edit.read_take(root, line, 1)
    assert current['settings']['temperature'] != .33
    state = client.get(base + '?probe=false').json()
    first, second = state['lines']
    assert not first['confirmed'] and second['confirmed']
    history = first['wav']['adjustment_history']
    assert history and len(history['entries']) == 2
    response = client.put(base + '/lines/1/audio-adjustment', json={
        'revision': history['revision'], 'entry_id': history['entries'][0]['id']})
    assert response.status_code == 200, response.text
    assert dubbing_edit.take_path(root, 1, 1).read_bytes() == old
    assert len(calls) == 3


def test_automatic_repair_is_finite_and_retains_best_audio_on_exhaustion(old_conflict, monkeypatch):
    _, uid, calls = old_conflict
    calls = fake_index(monkeypatch, lambda text, factor: .5 if text == '世界' else
                       1.8 if factor == .7 else 1.9)
    job = finish(uid)
    assert job.status == 'failed'
    assert calls == [('你好', .7), ('你好', .5)]
    assert report(uid)['unresolved_ids'] == [1]
    saved = json.loads(job.task_data)
    assert saved['lines'][0]['timing_factors'] == [.7, .5]
    # 重新检查不会把耗尽的因子视作尚未执行，造成无限自动请求。
    summary = dubbing_policy.preview(crud.get_project(uid))
    assert summary['repair'] == 0 and summary['repair_blocked']


def test_disabling_repair_keeps_precheck_explicit_and_audio_unchanged(old_conflict):
    from dubforge.workflow_plan import production_checks
    _, uid, calls = old_conflict
    deps = Dependencies(crud.get_project(uid))
    summary = dubbing_policy.preview(deps.project, {'repair_timing': False}, dependencies=deps)
    assert summary['repair'] == 0 and summary['generate'] == 0
    assert production_checks(deps, ['assemble'], dubbing_summary=summary)
    assert len(calls) == 2


def test_repair_uses_old_reference_even_when_new_project_reference_is_missing(old_conflict):
    _, uid, calls = old_conflict
    save('project:' + uid, {'tts.indextts25.timbre_audio': 'Z:/missing-new-voice.wav',
                           'tts.indextts25.emo_mode': 'dynamic'})
    summary = dubbing_policy.preview(crud.get_project(uid))
    assert summary['repair'] == 1 and not summary['requirements']
    job = finish(uid)
    assert job.status == 'done', job.error_detail
    assert calls[-1] == ('你好', .7)


def test_legacy_enabled_snapshot_controls_actual_retry_factor_when_new_setting_is_off(old_conflict):
    _, uid, calls = old_conflict
    save('project:' + uid, {'tts.placement.auto_repair': False})
    job = finish(uid, repair_timing=True)
    assert job.status == 'done', job.error_detail
    assert calls[-1] == ('你好', .7)
    assert report(uid)['ready']


def test_transient_failed_factor_can_retry_on_resume_in_same_operation(old_conflict, monkeypatch):
    from dubforge.backends import tts_indextts25
    from dubforge.errors import DubForgeError
    _, uid, calls = old_conflict
    original = tts_indextts25.synth
    failed = []
    def unavailable(*args, **kwargs):
        failed.append(kwargs['duration_factor'])
        raise DubForgeError('测试服务暂时不可用', code='TTS_REQUEST_FAILED')
    operation = uuid4().hex
    monkeypatch.setattr(tts_indextts25, 'synth', unavailable)
    runner.start_step(uid, 'tts', dubbing={'mode': 'fill'}, dubbing_operation_id=operation)
    deadline = time.monotonic() + 15
    while runner.current_job_id(crud.get_project(uid).id, 'tts') is not None:
        assert time.monotonic() < deadline
        time.sleep(.01)
    assert crud.latest_job(crud.get_project(uid).id, 'tts').status == 'failed'
    monkeypatch.setattr(tts_indextts25, 'synth', original)
    runner.start_step(uid, 'tts', dubbing={'mode': 'fill'}, dubbing_operation_id=operation)
    deadline = time.monotonic() + 15
    while runner.current_job_id(crud.get_project(uid).id, 'tts') is not None:
        assert time.monotonic() < deadline
        time.sleep(.01)
    assert crud.latest_job(crud.get_project(uid).id, 'tts').status == 'done'
    assert failed == [.7] and calls[-1] == ('你好', .7)
