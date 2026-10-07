"""真实项目配置与登记产物的恢复边界，不调用模型、编码或发布。"""
import json

import pytest

from conftest import mark_upstream_done
from dubforge import chain_recovery, config, configuration
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import assembly_versions
from dubforge.pipeline.recovery import RecoveryDependencies
from dubforge.workflow_plan import configuration_stamp


def prepared_chain(make_project, *, burn=False, overrides=None, steps=None):
    uid = make_project()
    mark_upstream_done(uid, 'subtitles')
    current = configuration.resolve(uid)
    configuration.save('project:' + uid, dict(revision=current['revision'], updates={
        'content_review.enabled': False,
        'assembly.versions': [dict(id='source', name='原声版本', dubbing=False,
            burn_subtitles=burn, include_comments=False,
            overrides={'assemble.subtitle_track': 'source', **(overrides or {})})]}))
    project = crud.get_project(uid)
    params = crud.project_params(project)
    steps = steps or ['assemble']
    params.update(_chain=steps, _chain_state='held',
                  _chain_hold=dict(step=steps[0], code='STEP_INTERRUPTED', message='隔离恢复验证'))
    project.params = json.dumps(params)
    crud.save_project(project)
    return uid, chain_recovery.plan(uid)


def current_version_fingerprint(uid):
    deps = RecoveryDependencies(crud.get_project(uid))
    version = assembly_versions.resolve(deps, assembly_versions.catalog(uid)['versions'][0])
    return assembly_versions.fingerprint(deps, version)


def test_inactive_global_render_settings_do_not_hold_plain_source_version(make_project):
    uid, plan = prepared_chain(make_project)
    original = current_version_fingerprint(uid)
    for key, value in [('assemble.crf', 19), ('assemble.voice_volume', .4),
                       ('assemble.bgm_volume', .3), ('assemble.subtitle_font_size', 39)]:
        config.set_key(key, value)
    assert current_version_fingerprint(uid) == original
    chain_recovery.check_execution(uid, plan['execution'], ['assemble'])


def test_version_override_does_not_follow_unconsumed_global_setting(make_project):
    uid, plan = prepared_chain(make_project, burn=True, overrides={'assemble.crf': 19})
    original = current_version_fingerprint(uid)
    config.set_key('assemble.crf', 30)
    assert current_version_fingerprint(uid) == original
    chain_recovery.check_execution(uid, plan['execution'], ['assemble'])


def test_consumed_global_render_setting_requires_new_confirmation(make_project):
    uid, plan = prepared_chain(make_project, burn=True)
    original = current_version_fingerprint(uid)
    config.set_key('assemble.crf', 19)
    assert current_version_fingerprint(uid) != original
    with pytest.raises(DubForgeError) as error:
        chain_recovery.check_execution(uid, plan['execution'], ['assemble'])
    assert error.value.code == 'CHAIN_PLAN_CHANGED'


def test_independent_subtitle_export_keeps_its_own_configuration_boundary(make_project):
    uid, plan = prepared_chain(make_project, steps=['subtitles', 'assemble'])
    original = current_version_fingerprint(uid)
    config.set_key('subtitles.min_duration', 1.3)
    # 原片版本不读取字幕导出，但显式选用的独立导出仍消费此参数。
    assert current_version_fingerprint(uid) == original
    with pytest.raises(DubForgeError) as error:
        chain_recovery.check_execution(uid, plan['execution'], ['subtitles', 'assemble'])
    assert error.value.code == 'CHAIN_PLAN_CHANGED'


def test_legacy_execution_snapshot_keeps_its_confirmed_settings_contract(make_project):
    uid, _plan = prepared_chain(make_project)
    deps = RecoveryDependencies(crud.get_project(uid))
    legacy = dict(configuration_stamps={'assemble': configuration_stamp(deps, steps=['assemble'])},
        version_definitions_digest=assembly_versions.digest(assembly_versions.catalog(uid)['versions']))
    config.set_key('assemble.crf', 19)
    with pytest.raises(DubForgeError) as error:
        chain_recovery.check_execution(uid, legacy, ['assemble'])
    assert error.value.code == 'CHAIN_PLAN_CHANGED'
