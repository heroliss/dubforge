"""本次配音策略覆盖真实 Runner 和文件版本；模型只使用隔离替身。"""
import json
from threading import Event
from uuid import uuid4

import pytest

from conftest import wait_status
from test_dubbing_preparation import prepared, index_prepared
from test_dubbing_tasks import finished, until
from test_configuration import save
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import dubbing_edit, dubbing_policy, line_settings
from dubforge.pipeline.dependencies import Dependencies
from dubforge.runner import runner
from dubforge.utils.fs import atomic_write_bytes


@pytest.fixture(autouse=True)
def no_real_service_coordination(monkeypatch):
    # 领域策略测试只验证任务、参数与文件；不探测、启动或停止真实配音服务。
    monkeypatch.setattr(runner, '_prepare_gpu', lambda *_: None)


def project(uid):
    return crud.get_project(uid)


def generate(uid, mode='all', *, operation=None, **options):
    result = runner.start_step(uid, 'tts', dubbing={'mode': mode, **options},
                               dubbing_operation_id=operation or uuid4().hex)
    if result['status'] == 'started':
        wait_status(uid, 'tts', {'done'})
    return result


def params(uid, **updates):
    row = project(uid)
    values = crud.project_params(row)
    values.update(updates)
    row.params = json.dumps(values, ensure_ascii=False)
    crud.save_project(row)


@pytest.mark.parametrize('raw', [False, [], {'extra': True}, {'mode': []}, {'target': 1},
    {'line_settings': False}, {'include_manual_sources': 1},
    {'target': 'slot', 'slot': True}, {'target': 'slot', 'slot': 0},
    {'target': 'slot', 'slot': 10001}, {'slot': 1}])
def test_invalid_policy_is_rejected_without_touching_project(raw):
    with pytest.raises(DubForgeError) as failure:
        dubbing_policy.normalize(raw)
    assert failure.value.code == 'DUB_POLICY_INVALID'


def test_fill_canonicalizes_hidden_redo_fields():
    assert dubbing_policy.normalize(dict(mode='fill', target='slot', slot=9,
        line_settings='project', include_manual_sources=True)) == dubbing_policy.DEFAULT_POLICY


def test_fill_reuses_old_voice_after_settings_change_and_fills_only_missing(prepared):
    uid, _, calls = prepared
    runner.start_dubbing(uid, [1]); wait_status(uid, 'tts', {'done'})
    old = dubbing_edit.take_path(crud.project_dir(uid), 1).read_bytes()
    save('project:' + uid, {'tts.gptsovits.character': '新的项目声音'})
    summary = dubbing_policy.preview(project(uid))
    assert (summary['generate'], summary['reuse'], summary['settings_changed']) == (1, 1, 1)
    generate(uid, 'fill')
    assert calls == ['你好', '世界']
    assert dubbing_edit.take_path(crud.project_dir(uid), 1).read_bytes() == old
    assert dubbing_policy.preview(project(uid))['generate'] == 0


def test_changed_respects_line_tuning_and_project_only_is_temporary(index_prepared):
    client, uid, received = index_prepared
    key = 'tts.indextts25.duration_factor'
    assert client.put(f'/api/projects/{uid}/workbench/lines/1/params', json={'updates': {key: .8}}).status_code == 200
    generate(uid)
    first_line = Dependencies(project(uid)).dubbing_lines()[0]
    original_tuning = line_settings.overrides(project(uid), first_line)
    save('project:' + uid, {key: 1.3})
    summary = dubbing_policy.preview(project(uid), {'mode': 'changed'})
    assert summary['generate'] == summary['settings_changed'] == 1
    generate(uid, 'changed')
    selected = Dependencies(project(uid)).production_lines()
    assert [row['take'] for row in selected] == [1, 1]
    assert [row['duration_factor'] for row in received] == [.8, 1.1, 1.3]
    generate(uid, line_settings='project')
    assert [row['duration_factor'] for row in received[-2:]] == [1.3, 1.3]
    assert line_settings.overrides(project(uid), first_line) == original_tuning
    assert [row['take'] for row in Dependencies(project(uid)).production_lines()] == [1, 1]
    take = dubbing_edit.read_take(crud.project_dir(uid), first_line, 1)
    assert take['line_settings_mode'] == 'project' and take['operation_id']
    assert take['identity'] == line_settings.identity(first_line)
    assert take['version'] == dubbing_edit.file_version(dubbing_edit.take_path(crud.project_dir(uid), 1, 1))
    client.close()


def test_all_preserves_manual_original_and_silence_until_explicitly_included(prepared):
    uid, _, calls = prepared
    lines = Dependencies(project(uid)).dubbing_lines()
    dubbing_edit.save_selection(project(uid), lines[0], 'original', None)
    dubbing_edit.save_selection(project(uid), lines[1], 'dub', None)
    summary = dubbing_policy.preview(project(uid), {'mode': 'all'})
    assert (summary['generate'], summary['original'], summary['silent']) == (0, 1, 1)
    assert generate(uid)['status'] == 'skipped'
    generate(uid, include_manual_sources=True)
    assert calls == ['你好', '世界']
    assert all(row['source'] == 'dub' for row in Dependencies(project(uid)).production_lines())


@pytest.mark.parametrize('target', ['new', 'current', 'slot'])
@pytest.mark.parametrize('legacy', [False, True])
def test_resume_keeps_destination_and_completed_audio_even_with_new_configuration(prepared, monkeypatch, target, legacy):
    from dubforge.backends import tts_gptsovits
    uid, _, calls = prepared
    generate(uid)
    root = crud.project_dir(uid)
    original = tts_gptsovits.synth
    def interrupted(*args, **kwargs):
        if args[1] == '世界':
            raise DubForgeError('隔离服务中断', code='TTS_REQUEST_FAILED')
        return original(*args, **kwargs)
    monkeypatch.setattr(tts_gptsovits, 'synth', interrupted)
    operation = uuid4().hex
    policy = dict(mode='all', target=target, **({'slot': 1} if target == 'slot' else {}))
    if legacy:
        policy['repair_timing'] = False
    runner.start_step(uid, 'tts', dubbing=policy, dubbing_operation_id=operation)
    assert finished(project(uid)).status == 'failed'
    take = 2 if target == 'new' else 1
    first = dubbing_edit.take_path(root, 1, take).read_bytes()
    # 旧意图恢复确认后改用项目配置；生成轮次与槽位不能因移除旧策略字段重置。
    policy = dubbing_policy.current(policy)
    save('project:' + uid, {'tts.gptsovits.character': '中断后改声音'})
    summary = dubbing_policy.preview(project(uid), policy, operation_id=operation)
    assert summary['resumed'] and summary['completed'] == 1 and summary['generate'] == 1
    assert summary['new_slot'] == (2 if target == 'new' else None)
    monkeypatch.setattr(tts_gptsovits, 'synth', original)
    # 外层 force 不得让本轮已完成的句子再次覆盖。
    runner.start_step(uid, 'tts', force=True, dubbing=policy, dubbing_operation_id=operation)
    wait_status(uid, 'tts', {'done'})
    assert calls == ['你好', '世界', '你好', '世界']
    assert dubbing_edit.take_path(root, 1, take).read_bytes() == first
    assert [row['take'] for row in Dependencies(project(uid)).production_lines()] == [take, take]


def test_policy_change_in_same_workflow_creates_new_round(prepared):
    uid, _, _ = prepared
    operation = uuid4().hex
    generate(uid, operation=operation)
    summary = dubbing_policy.preview(project(uid), {'mode': 'all', 'line_settings': 'project'}, operation_id=operation)
    assert summary['resume_reset'] and not summary['resumed'] and summary['new_slot'] is None
    generate(uid, operation=operation, line_settings='project')
    assert [row['take'] for row in Dependencies(project(uid)).production_lines()] == [1, 1]


def test_text_identity_change_does_not_reuse_previous_round(prepared):
    uid, _, calls = prepared
    operation = uuid4().hex
    generate(uid, operation=operation)
    artifact = crud.latest_artifacts(project(uid).id, ['translate'])['translate/translate.txt']
    path = crud.project_dir(uid) / artifact.rel_path
    atomic_write_bytes(path, '1. 新的译文\n2. 世界\n'.encode())
    crud.add_artifact(project(uid).id, 'translate', 'text', 'translate.txt', path, artifact.rel_path)
    summary = dubbing_policy.preview(project(uid), {'mode': 'all'}, operation_id=operation)
    assert summary['resume_reset'] and not summary['resumed'] and summary['new_slot'] is None
    generate(uid, operation=operation)
    assert calls[-2:] == ['新的译文', '世界']
    assert dubbing_edit.take_ids(crud.project_dir(uid), 1) == [1]


def test_stale_target_is_overwrite_but_no_longer_counts_as_satisfied(index_prepared):
    client, uid, _ = index_prepared
    generate(uid)
    assert client.post(f'/api/projects/{uid}/workbench/confirm', json={'line_ids': [1, 2]}).status_code == 200
    path = dubbing_edit.take_path(crud.project_dir(uid), 1, 1)
    atomic_write_bytes(path, path.read_bytes() + b'changed')
    summary = dubbing_policy.preview(project(uid), {'mode': 'all', 'target': 'current'})
    assert summary['overwrite'] == 2 and summary['confirmed'] == 1
    client.close()


def test_queue_reads_latest_saved_parameters_before_freezing(index_prepared):
    client, uid, received = index_prepared
    generate(uid)
    key = 'tts.indextts25.duration_factor'
    save('project:' + uid, {key: 1.3})
    runner.acquire_slot('tts')
    try:
        result = runner.start_step(uid, 'tts', dubbing={'mode': 'changed'})
        assert result['status'] == 'started' and crud.get_step(project(uid).id, 'tts').status == 'queued'
        save('project:' + uid, {key: 1.4})
    finally:
        runner.release_slot('tts')
    wait_status(uid, 'tts', {'done'})
    assert [row['duration_factor'] for row in received[-2:]] == [1.4, 1.4]
    task = json.loads(crud.latest_job(project(uid).id, 'tts').task_data)
    assert task['prepared'] and {row['take'] for row in task['lines']} == {1}
    client.close()


def test_resume_at_slot_limit_keeps_reserved_slot(prepared):
    uid, _, _ = prepared
    operation = uuid4().hex
    generate(uid, operation=operation, target='slot', slot=10000)
    summary = dubbing_policy.preview(project(uid), {'mode': 'all', 'target': 'slot', 'slot': 10000}, operation_id=operation)
    assert summary['resumed'] and summary['generate'] == 0 and summary['completed'] == 2
    with pytest.raises(DubForgeError) as failure:
        dubbing_policy.preview(project(uid), {'mode': 'all', 'target': 'new'})
    assert failure.value.code == 'DUB_TAKE_INVALID'


def test_changed_requirements_follow_project_only_reference_choice(index_prepared):
    client, uid, _ = index_prepared
    assert client.put(f'/api/projects/{uid}/workbench/lines/1/params', json={'updates': {
        'tts.indextts25.timbre_mode': 'dynamic'}}).status_code == 200
    preserve = dubbing_policy.preview(project(uid), {'mode': 'all'})
    assert any(row['name'] == 'vocal.wav' for row in preserve['requirements'])
    assert not dubbing_policy.preview(project(uid), {'mode': 'all', 'line_settings': 'project'})['requirements']
    generate(uid, line_settings='project')
    client.close()


def test_chain_resume_runs_policy_and_accepts_its_normal_selection_write(prepared):
    from dubforge import chain_recovery
    uid, _, calls = prepared
    generate(uid)
    operation = uuid4().hex
    policy = dubbing_policy.normalize({'mode': 'all'})
    params(uid, _chain=['tts'], _chain_state='held',
           _chain_hold={'step': 'tts', 'code': 'STEP_INTERRUPTED'},
           _chain_dubbing={'policy': policy, 'operation_id': operation})
    candidate = chain_recovery.plan(uid)
    assert candidate['dubbing']['generate'] == 2 and candidate['recovery']['start_step'] == 'tts'
    params(uid, _chain_resume_plan=candidate['execution'], _chain_state='active')
    chain_recovery.check_execution(uid, candidate['execution'], ['tts'])
    runner.start_step(uid, 'tts')
    wait_status(uid, 'tts', {'done'})
    assert calls == ['你好', '世界', '你好', '世界']
    assert [row['take'] for row in Dependencies(project(uid)).production_lines()] == [1, 1]
    assert '_chain_dubbing' not in crud.project_params(project(uid))


def test_all_before_upstream_readiness_is_explicitly_deferred(make_project):
    summary = dubbing_policy.preview(project(make_project()), {'mode': 'all'})
    assert summary['deferred'] and summary['generate'] == 0
    recovery = {'rows': [], 'execute_steps': ['translate', 'assemble'], 'reusable_steps': ['tts', 'assemble']}
    dubbing_policy.apply_recovery(summary, recovery)
    assert recovery['execute_steps'] == ['translate', 'tts', 'assemble']
    assert recovery['start_step'] == 'translate'
