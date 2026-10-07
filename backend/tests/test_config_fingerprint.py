"""配置影响范围、旧缓存兼容与流程快照；不访问生产数据或外部服务。"""
import pytest
import json

from dubforge import config, workflows
from dubforge.domain import crud
from dubforge.pipeline.config_fingerprint import promote_compatible_fingerprints
from dubforge.pipeline.dependencies import Dependencies
from dubforge.runner import compute_fingerprint
from dubforge.workflow_plan import configuration_snapshot


def profiles():
    return [dict(name='文字', base_url='https://example.invalid/a', model='one',
                 api_key='测试密钥', params={'temperature': .2}),
            dict(name='备用', base_url='https://example.invalid/b', model='two', api_key='另一密钥'),
            dict(name='停用', base_url='https://example.invalid/c', model='off', enabled=False)]


def test_connection_credentials_timeout_and_disabled_profiles_do_not_invalidate(make_project):
    project = crud.get_project(make_project())
    values = profiles()
    config.set_key('llm.profiles', values)
    before = {step: compute_fingerprint(project, step) for step in ('split', 'translate')}
    values[0]['api_key'] = '更换密钥'
    values[2]['model'] = '停用组修改'
    config.update_config({'llm.profiles': values, 'llm.timeout': 600})
    assert before == {step: compute_fingerprint(project, step) for step in before}
    data = configuration_snapshot(Dependencies(project))
    assert '测试密钥' not in str(data) and '更换密钥' not in str(data)


def test_selected_model_group_ignores_unrelated_groups_but_tracks_model_and_params(make_project):
    project = crud.get_project(make_project(params={'step_params': {'translate.llm_profile': '文字'}}))
    values = profiles()
    config.set_key('llm.profiles', values)
    before = compute_fingerprint(project, 'translate')
    values[1]['model'] = 'unrelated'
    config.set_key('llm.profiles', values)
    assert compute_fingerprint(project, 'translate') == before
    values[0]['params']['temperature'] = .9
    config.set_key('llm.profiles', values)
    assert compute_fingerprint(project, 'translate') != before
    changed = compute_fingerprint(project, 'translate')
    values[0]['model'] = 'new-model'
    config.set_key('llm.profiles', values)
    assert compute_fingerprint(project, 'translate') != changed


def test_fallback_order_and_enabled_groups_still_invalidate(make_project):
    project = crud.get_project(make_project())
    values = profiles()
    config.set_key('llm.profiles', values)
    before = compute_fingerprint(project, 'split')
    config.set_key('llm.profiles', [values[1], values[0], values[2]])
    assert compute_fingerprint(project, 'split') != before
    values[2]['enabled'] = True
    config.set_key('llm.profiles', values)
    assert compute_fingerprint(project, 'split') != before


def test_rule_split_has_no_model_dependency(make_project):
    project = crud.get_project(make_project(params={'step_params': {'split.use_llm': False}}))
    config.set_key('llm.profiles', profiles())
    before = compute_fingerprint(project, 'split')
    config.set_key('llm.profiles', [])
    assert compute_fingerprint(project, 'split') == before


@pytest.mark.parametrize('use_llm', [True, False])
def test_split_tracks_only_the_length_unit_used_by_its_mode(make_project, use_llm):
    project = crud.get_project(make_project(params={'step_params': {'split.use_llm': use_llm}}))
    before = compute_fingerprint(project, 'split')
    inactive = 'split.max_split_length' if use_llm else 'split.target_word_count'
    config.set_key(inactive, 33)
    assert compute_fingerprint(project, 'split') == before
    active = 'split.target_word_count' if use_llm else 'split.max_split_length'
    config.set_key(active, 34)
    assert compute_fingerprint(project, 'split') != before
    latest = compute_fingerprint(project, 'split')
    config.set_key('split.max_segment_seconds', 9)
    assert compute_fingerprint(project, 'split') != latest


def test_legacy_null_split_switch_still_consumes_model(make_project):
    project = crud.get_project(make_project())
    config.set_key('split.use_llm', None)
    values = profiles()
    config.set_key('llm.profiles', values)
    before = compute_fingerprint(project, 'split')
    values[0]['model'] = 'changed'
    config.set_key('llm.profiles', values)
    assert compute_fingerprint(project, 'split') != before


def test_startup_promotes_only_matching_done_results_and_is_idempotent(make_project):
    config.set_key('llm.profiles', profiles())
    first = crud.get_project(make_project())
    second = crud.get_project(make_project())
    row = crud.get_step(first.id, 'translate')
    row.status = 'done'
    row.input_fingerprint = compute_fingerprint(first, 'translate', legacy_config=True)
    crud.save_step(row)
    stale = crud.get_step(second.id, 'translate')
    stale.status, stale.input_fingerprint = 'done', 'a' * 64
    crud.save_step(stale)
    assert promote_compatible_fingerprints() == 1
    assert crud.get_step(first.id, 'translate').input_fingerprint == compute_fingerprint(first, 'translate')
    assert crud.get_step(second.id, 'translate').input_fingerprint == 'a' * 64
    assert promote_compatible_fingerprints() == 0
    before = crud.get_step(first.id, 'translate').input_fingerprint
    values = profiles()
    values[0]['api_key'] = '轮换'
    config.update_config({'llm.profiles': values, 'llm.timeout': 1000})
    assert compute_fingerprint(first, 'translate') == before


def test_workflow_boundary_checks_only_remaining_model_groups(make_project):
    project = crud.get_project(make_project(params={'step_params': {
        'split.llm_profile': '文字', 'translate.llm_profile': '备用'}}))
    values = profiles()
    config.set_key('llm.profiles', values)
    stored = configuration_snapshot(Dependencies(project))
    run = {'project_uid': project.uid, 'plan': {'configuration_snapshot': stored}}
    values[0]['model'] = '已经完成的整理模型变化'
    config.set_key('llm.profiles', values)
    workflows._check_configuration(run, ['translate'])
    values[1]['model'] = '需要翻译的模型变化'
    config.set_key('llm.profiles', values)
    with pytest.raises(workflows.WorkflowError, match='重新预检'):
        workflows._check_configuration(run, ['translate'])


def test_old_workflow_snapshot_still_uses_its_confirmed_contract(make_project):
    project = crud.get_project(make_project())
    stored = configuration_snapshot(Dependencies(project), legacy=True)
    run = {'project_uid': project.uid, 'plan': {'configuration_snapshot': stored}}
    workflows._check_configuration(run, ['translate'])
    config.set_key('translate.style_prompt', '保留术语')
    with pytest.raises(workflows.WorkflowError):
        workflows._check_configuration(run, ['translate'])


def test_old_artifact_config_remains_compatible_without_mutation(make_project):
    project = crud.get_project(make_project())
    config.set_key('llm.profiles', profiles())
    row = crud.get_step(project.id, 'translate')
    row.input_fingerprint = compute_fingerprint(project, 'translate', legacy_config=True)
    crud.save_step(row)
    assert Dependencies(project).matches_fingerprint('translate')
    assert crud.get_step(project.id, 'translate').input_fingerprint == row.input_fingerprint


@pytest.mark.parametrize('legacy', [False, True])
def test_local_source_snapshot_survives_json_roundtrip(isolated_env, legacy):
    source = isolated_env / 'local.mp4'
    source.write_bytes('视频夹具'.encode())
    project = crud.create_project('本地视频', str(source), 'local')
    stored = json.loads(json.dumps(configuration_snapshot(Dependencies(project), legacy=legacy)))
    run = {'project_uid': project.uid, 'plan': {'configuration_snapshot': stored}}
    workflows._check_configuration(run, ['translate'])
    source.write_bytes(b'changed-video-content')
    with pytest.raises(workflows.WorkflowError):
        workflows._check_configuration(run, ['translate'])


def test_automatic_comments_follow_global_model_chain(make_project):
    project = crud.get_project(make_project())
    values = profiles()
    config.set_key('llm.profiles', values)
    stored = configuration_snapshot(Dependencies(project))
    run = {'project_uid': project.uid, 'plan': {'configuration_snapshot': stored}}
    values[0]['api_key'] = '轮换密钥'
    config.set_key('llm.profiles', values)
    workflows._check_configuration(run, ['comments'])
    values[0]['model'] = 'new-model'
    config.set_key('llm.profiles', values)
    with pytest.raises(workflows.WorkflowError):
        workflows._check_configuration(run, ['comments'])
    workflows._check_configuration(run, [])


def test_terms_content_changes_at_same_path_invalidate_output_and_boundary(make_project, isolated_env):
    from openpyxl import Workbook
    path = isolated_env / 'terms.xlsx'
    book = Workbook()
    sheet = book.active
    sheet.append(['src', 'dst', '备注'])
    sheet.append(['station', '车站', '交通术语'])
    book.save(path)
    project = crud.get_project(make_project(params={'step_params': {'translate.custom_terms_path': str(path)}}))
    before = compute_fingerprint(project, 'translate')
    stored = configuration_snapshot(Dependencies(project))
    sheet['B2'] = '站点'
    book.save(path)
    assert compute_fingerprint(project, 'translate') != before
    with pytest.raises(workflows.WorkflowError):
        workflows._check_configuration({'project_uid': project.uid,
            'plan': {'configuration_snapshot': stored}}, ['translate'])


def test_old_terms_cache_is_not_promoted_without_content_evidence(make_project, isolated_env):
    path = isolated_env / 'terms.xlsx'
    path.write_bytes(b'legacy-terms-fixture')
    project = crud.get_project(make_project(params={'step_params': {'translate.custom_terms_path': str(path)}}))
    row = crud.get_step(project.id, 'translate')
    row.status = 'done'
    row.input_fingerprint = compute_fingerprint(project, 'translate', legacy_config=True)
    crud.save_step(row)
    assert not Dependencies(project).matches_fingerprint('translate')
    assert promote_compatible_fingerprints() == 0
    assert crud.get_step(project.id, 'translate').input_fingerprint == row.input_fingerprint
