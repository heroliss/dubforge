"""时间冲突修复是普通配音设置；旧任务快照兼容，执行与内容身份分离。"""
import json
import threading
import time

import pytest

from dubforge import configuration, runtime_edit, workflow_plan
from dubforge.domain import crud
from dubforge.pipeline import assembly_versions, dubbing_policy, step_tts, voice_timing
from dubforge.pipeline.config_fingerprint import snapshot
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline.step_params import RUNTIME_PARAM_PATHS
from dubforge.runner import StepContext, compute_fingerprint, runner
from test_automatic_timing_repair import old_conflict
from test_configuration import bind, save
from test_dubbing_preparation import index_prepared, prepared
from test_voice_timing_integration import fake_index, timeline


KEY = 'tts.placement.auto_repair'


def test_regular_configuration_inheritance_is_frozen_and_project_override_wins(make_project):
    save('global', {KEY: False})
    uid = make_project()
    assert configuration.resolve(uid)['values'][KEY] is False
    save('global', {KEY: True})
    assert configuration.resolve(uid)['values'][KEY] is False
    template = configuration.create_template('配音模板')
    save(template['key'], {KEY: True})
    application = configuration.binding_plan([uid], template['key'][7:])
    assert KEY in application['projects'][0]['changes']
    assert not application['projects'][0]['affected_steps']
    bind([uid], template)
    resolved = configuration.resolve(uid)
    assert resolved['values'][KEY] is True
    assert resolved['sources'][KEY]['kind'] == 'series'
    save('project:' + uid, {KEY: False})
    assert configuration.resolve(uid)['sources'][KEY] == {'kind': 'project'}
    assert KEY not in RUNTIME_PARAM_PATHS


def test_new_run_intent_cannot_override_project_setting_with_old_default(old_conflict):
    _, uid, _ = old_conflict
    save('project:' + uid, {KEY: False})
    raw = {'mode': 'fill', 'repair_timing': True}
    current = workflow_plan.recipe({'mode': 'produce', 'dubbing': raw})['dubbing']
    assert 'repair_timing' not in current
    summary = dubbing_policy.preview(crud.get_project(uid), current)
    assert summary['repair'] == 0
    assert summary['repair_settings'] == dict(enabled=False, configured=False, legacy=False)
    # 原任务有明确历史快照时可继续解释当时采用的策略，不改写已保存意图。
    legacy = dubbing_policy.preview(crud.get_project(uid), raw)
    assert legacy['repair_settings'] == dict(enabled=True, configured=False, legacy=True)
    assert legacy['repair'] > 0
    assert dubbing_policy.normalize({'repair_timing': False})['repair_timing'] is False


def test_recovery_discards_old_strategy_but_preserves_operation_and_uses_project_setting(old_conflict):
    from dubforge import chain_recovery
    _, uid, _ = old_conflict
    project = crud.get_project(uid)
    params = crud.project_params(project)
    params.update(_chain=['tts'], _chain_state='held',
                  _chain_dubbing=dict(operation_id='legacy-operation',
                                      policy=dubbing_policy.normalize({'repair_timing': False})))
    project.params = json.dumps(params)
    crud.save_project(project)
    candidate = chain_recovery.plan(uid)
    assert candidate['execution']['dubbing']['operation_id'] == 'legacy-operation'
    assert 'repair_timing' not in candidate['dubbing']['policy']
    assert candidate['dubbing']['repair_settings'] == dict(enabled=True, configured=True, legacy=False)
    assert candidate['dubbing']['repair'] > 0


def test_setting_changes_do_not_invalidate_audio_subtitles_or_assembly(index_prepared):
    _, uid, _ = index_prepared
    project = crud.get_project(uid)
    deps = Dependencies(project)
    line = deps.dubbing_lines()[0]
    before = {key: compute_fingerprint(project, key) for key in ('tts', 'subtitles', 'assemble')}
    audio = step_tts.line_fingerprint(project, line)
    definition = assembly_versions.catalog(uid)['versions'][0]
    version = assembly_versions.resolve(deps, definition)
    output = assembly_versions.fingerprint(deps, version)
    execution = snapshot(deps, 'tts', execution=True)
    save('project:' + uid, {KEY: False})
    project = crud.get_project(uid)
    current = Dependencies(project)
    assert {key: compute_fingerprint(project, key) for key in before} == before
    assert step_tts.line_fingerprint(project, line) == audio
    assert assembly_versions.fingerprint(current, assembly_versions.resolve(current, definition)) == output
    assert snapshot(current, 'tts', execution=True) != execution
    assert 'auto_repair' not in snapshot(current, 'tts')['tts']['placement']


def test_queued_configuration_is_adopted_then_frozen_for_running_context(make_project):
    uid = make_project()
    project, root = crud.get_project(uid), crud.project_dir(uid)
    context = StepContext(project, 'tts', root, root / 'logs/setting.log',
                          threading.Event(), defer_snapshot=True)
    save('project:' + uid, {KEY: False})
    context.freeze_configuration()
    assert context.cfg(KEY) is False
    save('project:' + uid, {KEY: True})
    assert context.cfg(KEY) is False
    assert dubbing_policy.repair_settings(context.cfg)['enabled'] is False


@pytest.mark.parametrize('step,status,blocked', [('tts', 'queued', False), ('tts', 'running', True),
    ('assemble', 'running', False), ('subtitles', 'running', False), ('translate', 'running', False)])
def test_only_running_tts_locks_the_regular_setting(make_project, step, status, blocked):
    uid = make_project()
    row = crud.get_step(crud.get_project(uid).id, step)
    row.status = status
    crud.save_step(row)
    if blocked:
        with pytest.raises(runtime_edit.RuntimeEditError):
            save('project:' + uid, {KEY: False})
    else:
        save('project:' + uid, {KEY: False})
        assert configuration.resolve(uid)['values'][KEY] is False


def test_actual_queued_run_uses_latest_auto_repair_and_never_shortens_when_disabled(index_prepared, monkeypatch):
    _, uid, _ = index_prepared
    monkeypatch.setattr(runner, '_prepare_gpu', lambda *_: None)
    timeline(uid, [(0, 1), (1, 2)])
    monkeypatch.setattr(voice_timing, 'video_seconds', lambda *_: 2)
    calls = fake_index(monkeypatch, lambda text, factor: 2.)
    save('project:' + uid, {'tts.placement.enabled': True,
        'tts.placement.max_shift_seconds': 0., 'tts.indextts25.duration_retry_factors': '.7,.5'})
    entered, release = threading.Event(), threading.Event()
    acquire = runner.acquire_slot
    def queued(step, cancel):
        entered.set()
        assert release.wait(10)
        return acquire(step, cancel)
    monkeypatch.setattr(runner, 'acquire_slot', queued)
    runner.start_step(uid, 'tts', dubbing={'mode': 'all'})
    try:
        assert entered.wait(5)
        assert crud.get_step(crud.get_project(uid).id, 'tts').status == 'queued'
        save('project:' + uid, {KEY: False})
    finally:
        release.set()
    deadline = time.monotonic() + 15
    while runner.current_job_id(crud.get_project(uid).id, 'tts') is not None:
        assert time.monotonic() < deadline
        time.sleep(.01)
    job = crud.latest_job(crud.get_project(uid).id, 'tts')
    assert job.status == 'failed' and '自动缩时修复已关闭' in job.error_detail
    assert calls == [('你好', 1.1), ('世界', 1.1)]
    assert json.loads(job.task_data)['repair_settings'] == dict(enabled=False, configured=False, legacy=False)


def test_single_line_synthesis_disables_internal_duration_retries_too(index_prepared, monkeypatch):
    _, uid, _ = index_prepared
    calls = fake_index(monkeypatch, lambda text, factor: 4.)
    save('project:' + uid, {KEY: False, 'tts.placement.enabled': False,
                           'tts.indextts25.duration_retry_factors': '.7,.5'})
    project = crud.get_project(uid)
    line = Dependencies(project).dubbing_lines()[0]
    step_tts.synth_line(project, line, crud.project_dir(uid), force=True)
    assert calls == [('你好', 1.1)]
