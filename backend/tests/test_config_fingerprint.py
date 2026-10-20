"""配置影响范围、旧缓存兼容与流程快照；不访问生产数据或外部服务。"""
import pytest
import json

from dubforge import config, configuration, workflows
from dubforge.domain import crud
from dubforge.pipeline.config_fingerprint import promote_compatible_fingerprints, snapshot
from dubforge.pipeline import stale_explanation
from dubforge.pipeline.dependencies import Dependencies
from dubforge.runner import compute_fingerprint, fingerprint_payload
from dubforge.workflow_plan import configuration_snapshot


def profiles():
    return [dict(name='文字', base_url='https://example.invalid/a', model='one',
                 api_key='测试密钥', params={'temperature': .2}),
            dict(name='备用', base_url='https://example.invalid/b', model='two', api_key='另一密钥'),
            dict(name='停用', base_url='https://example.invalid/c', model='off', enabled=False)]


def project_setting(project, path, value):
    current = configuration.resolve(project.uid)
    configuration.save('project:' + project.uid, dict(revision=current['revision'],
        fingerprint=current['fingerprint'], updates={path: value}))


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


def test_selected_model_group_changes_keep_completed_content_identity(make_project):
    project = crud.get_project(make_project(params={'step_params': {'translate.llm_profile': '文字'}}))
    values = profiles()
    config.set_key('llm.profiles', values)
    before = compute_fingerprint(project, 'translate')
    values[1]['model'] = 'unrelated'
    config.set_key('llm.profiles', values)
    assert compute_fingerprint(project, 'translate') == before
    values[0]['params']['temperature'] = .9
    config.set_key('llm.profiles', values)
    assert compute_fingerprint(project, 'translate') == before
    changed = compute_fingerprint(project, 'translate')
    values[0]['model'] = 'new-model'
    config.set_key('llm.profiles', values)
    assert compute_fingerprint(project, 'translate') == changed


def test_fallback_order_and_enabled_groups_do_not_invalidate_content(make_project):
    project = crud.get_project(make_project())
    values = profiles()
    config.set_key('llm.profiles', values)
    before = compute_fingerprint(project, 'split')
    config.set_key('llm.profiles', [values[1], values[0], values[2]])
    assert compute_fingerprint(project, 'split') == before
    values[2]['enabled'] = True
    config.set_key('llm.profiles', values)
    assert compute_fingerprint(project, 'split') == before


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
    project_setting(project, inactive, 33)
    assert compute_fingerprint(project, 'split') == before
    active = 'split.target_word_count' if use_llm else 'split.max_split_length'
    project_setting(project, active, 34)
    assert compute_fingerprint(project, 'split') != before
    latest = compute_fingerprint(project, 'split')
    project_setting(project, 'split.max_segment_seconds', 9)
    assert compute_fingerprint(project, 'split') != latest


def test_dynamic_reference_limit_invalidates_only_when_changed_from_default(make_project):
    project = crud.get_project(make_project())
    before = compute_fingerprint(project, 'tts')
    project_setting(project, 'tts.indextts25.dynamic_reference_max_seconds', 10.0)
    assert compute_fingerprint(project, 'tts') != before
    project_setting(project, 'tts.indextts25.dynamic_reference_max_seconds', 5.0)
    assert compute_fingerprint(project, 'tts') == before


def test_legacy_null_split_switch_preserves_content_when_model_changes(make_project):
    project = crud.get_project(make_project())
    config.set_key('split.use_llm', None)
    values = profiles()
    config.set_key('llm.profiles', values)
    before = compute_fingerprint(project, 'split')
    values[0]['model'] = 'changed'
    config.set_key('llm.profiles', values)
    assert compute_fingerprint(project, 'split') == before


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
    # 尚未开始的阶段在拿到运行槽位时读取当前模型设置。
    workflows._check_configuration(run, ['translate'])


def test_old_workflow_snapshot_still_uses_its_confirmed_contract(make_project):
    project = crud.get_project(make_project())
    stored = configuration_snapshot(Dependencies(project), legacy=True)
    run = {'project_uid': project.uid, 'plan': {'configuration_snapshot': stored}}
    workflows._check_configuration(run, ['translate'])
    project_setting(project, 'translate.style_prompt', '保留术语')
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


@pytest.mark.parametrize('step, path, value', [
    ('split', 'split.llm_profile', '其他模型'),
    ('split', 'split.thinking_mode', 'follow_profile'),
    ('split', 'split.llm_context_tokens', 500000),
    ('translate', 'translate.llm_profile', '其他模型'),
    ('translate', 'translate.thinking_mode', 'follow_profile'),
    ('translate', 'translate.max_context_tokens', 500000),
    ('translate', 'translate.max_window_lines', 50),
])
def test_execution_choices_change_contract_but_not_existing_content(make_project, step, path, value):
    project = crud.get_project(make_project())
    deps = Dependencies(project)
    before = compute_fingerprint(project, step)
    contract = snapshot(deps, step, execution=True)
    project_setting(project, path, value)
    assert compute_fingerprint(project, step) == before
    assert snapshot(Dependencies(project), step, execution=True) != contract
    assert 'llm' not in snapshot(Dependencies(project), step)


def successful_legacy_translation(make_project):
    from conftest import mark_upstream_done
    config.set_key('llm.profiles', profiles())
    uid = make_project()
    mark_upstream_done(uid, 'translate')
    project = crud.get_project(uid)
    root = crud.project_dir(uid)
    path = root / 'artifacts/translate/translate.txt'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('1. 你好\n', encoding='utf-8')
    crud.add_artifact(project.id, 'translate', 'text', path.name, path, str(path.relative_to(root)))
    deps = Dependencies(project)
    baseline = stale_explanation.capture(deps, 'translate')
    baseline['fingerprint'] = compute_fingerprint(project, 'translate', legacy_execution=True)
    baseline['entries'] = stale_explanation._payload_entries(deps, 'translate',
        fingerprint_payload(project, 'translate', legacy_execution=True))
    step = crud.get_step(project.id, 'translate')
    step.status = 'done'
    step.input_fingerprint = baseline['fingerprint']
    step.input_snapshot = json.dumps(baseline, ensure_ascii=False)
    crud.save_step(step)
    return project, baseline


def test_completed_legacy_result_survives_model_change_with_success_evidence(make_project):
    project, baseline = successful_legacy_translation(make_project)
    path = crud.project_dir(project.uid) / 'artifacts/translate/translate.txt'
    before = path.read_bytes()
    values = profiles()
    values[0].update(model='future-model', base_url='https://example.invalid/new')
    values[0]['params'] = {'temperature': .9, 'chat_template_kwargs': {'enable_thinking': False}}
    config.set_key('llm.profiles', values[::-1])
    assert promote_compatible_fingerprints() >= 1
    row = crud.get_step(project.id, 'translate')
    assert row.status == 'done' and row.input_fingerprint == compute_fingerprint(project, 'translate')
    updated = json.loads(row.input_snapshot)
    assert updated['fingerprint'] == row.input_fingerprint
    assert updated['captured_at'] == baseline['captured_at']
    assert not any(key.startswith('llm.profile.') for key in updated['entries'])
    assert path.read_bytes() == before
    assert Dependencies(project).fresh('translate')
    assert promote_compatible_fingerprints() == 0
    project_setting(project, 'translate.target_language', '日本語')
    assert not Dependencies(project).fresh('translate')
    assert Dependencies(project).explain('translate')['history_available']


def test_startup_skips_large_output_reads_when_content_requirements_changed(make_project, monkeypatch):
    from pathlib import Path
    from dubforge.pipeline.recovery import RecoveryDependencies
    project, _ = successful_legacy_translation(make_project)
    root = crud.project_dir(project.uid)
    output = root / 'artifacts/translate/large-output.bin'
    output.write_bytes(b'a' * (1024 * 1024 + 1))
    crud.add_artifact(project.id, 'translate', 'binary', output.name, output, str(output.relative_to(root)))
    old = crud.get_step(project.id, 'translate').input_fingerprint
    project_setting(project, 'translate.style_prompt', '新的翻译需求')
    values = profiles()
    values[0]['model'] = 'other-model'
    config.set_key('llm.profiles', values)
    original_open = Path.open

    def checked_open(path, mode='r', *args, **kwargs):
        if path == output and 'r' in mode:
            pytest.fail('内容要求已经变化，启动兼容迁移不应读取大产物')
        return original_open(path, mode, *args, **kwargs)

    def unexpected_input_check(*args, **kwargs):
        pytest.fail('内容要求已经变化，启动兼容迁移不应核验输入文件')

    monkeypatch.setattr(Path, 'open', checked_open)
    monkeypatch.setattr(RecoveryDependencies, 'file_matches', unexpected_input_check)
    assert promote_compatible_fingerprints() == 0
    assert crud.get_step(project.id, 'translate').input_fingerprint == old


@pytest.mark.parametrize('corruption', ['none', 'output', 'input'])
def test_startup_compatible_history_still_verifies_complete_outputs_and_inputs(make_project, monkeypatch, corruption):
    from pathlib import Path
    from dubforge.pipeline.recovery import RecoveryDependencies
    project, _ = successful_legacy_translation(make_project)
    root = crud.project_dir(project.uid)
    output = root / 'artifacts/translate/large-output.bin'
    content = b'a' * (1024 * 1024 + 1)
    output.write_bytes(content)
    crud.add_artifact(project.id, 'translate', 'binary', output.name, output, str(output.relative_to(root)))
    old = crud.get_step(project.id, 'translate').input_fingerprint
    if corruption == 'output':
        # 大小不变且只改末尾，必须完整读取摘要才能拒绝迁移。
        output.write_bytes(content[:-1] + b'b')
    elif corruption == 'input':
        source = root / 'artifacts/split/split.txt'
        source.write_bytes(source.read_bytes().replace(b'hello', b'world'))
    values = profiles()
    values[0]['model'] = 'other-model'
    config.set_key('llm.profiles', values)
    reads, input_checks = [], []
    original_open = Path.open
    original_matches = RecoveryDependencies.file_matches

    def checked_open(path, mode='r', *args, **kwargs):
        if path == output and 'r' in mode:
            reads.append(path)
        return original_open(path, mode, *args, **kwargs)

    def checked_matches(deps, producer, name):
        input_checks.append((producer, name))
        return original_matches(deps, producer, name)

    monkeypatch.setattr(Path, 'open', checked_open)
    monkeypatch.setattr(RecoveryDependencies, 'file_matches', checked_matches)
    assert promote_compatible_fingerprints() == (1 if corruption == 'none' else 0)
    assert reads == [output]
    if corruption != 'output':
        assert ('split', 'split.txt') in input_checks
    current = crud.get_step(project.id, 'translate').input_fingerprint
    assert current == (compute_fingerprint(project, 'translate') if corruption == 'none' else old)


@pytest.mark.parametrize('change', ['style', 'terms', 'source', 'missing_output', 'changed_output',
                                    'missing_entry', 'unknown', 'failed', 'running', 'stale'])
def test_model_change_cannot_hide_changed_inputs_or_incomplete_history(make_project, isolated_env, change):
    project, _ = successful_legacy_translation(make_project)
    row = crud.get_step(project.id, 'translate')
    old = row.input_fingerprint
    if change == 'style':
        project_setting(project, 'translate.style_prompt', '新的翻译需求')
    elif change == 'terms':
        terms = isolated_env / 'terms.xlsx'
        terms.write_bytes(b'new-terms')
        project_setting(project, 'translate.custom_terms_path', str(terms))
    elif change == 'source':
        source = crud.project_dir(project.uid) / 'artifacts/split/split.txt'
        source.write_text('1. 0.000-1.000: changed\n', encoding='utf-8')
        crud.add_artifact(project.id, 'split', 'text', source.name, source, str(source.relative_to(crud.project_dir(project.uid))))
    elif change in {'missing_output', 'changed_output'}:
        output = crud.project_dir(project.uid) / 'artifacts/translate/translate.txt'
        if change == 'missing_output':
            output.unlink()
        else:
            output.write_text('1. 更改后的译文\n', encoding='utf-8')
    elif change == 'missing_entry':
        history = json.loads(row.input_snapshot)
        history['entries'].pop('translate.target_language')
        row.input_snapshot = json.dumps(history)
        crud.save_step(row)
    elif change == 'unknown':
        row.input_snapshot = '{}'
        row.input_fingerprint = old = 'f' * 64
        crud.save_step(row)
    else:
        row.status = change
        crud.save_step(row)
    values = profiles()
    values[0]['model'] = 'other-model'
    config.set_key('llm.profiles', values)
    promote_compatible_fingerprints()
    current = crud.get_step(project.id, 'translate')
    assert current.input_fingerprint == old
    assert current.status == row.status


def test_strict_legacy_migration_does_not_invent_absent_history(make_project):
    project = crud.get_project(make_project())
    row = crud.get_step(project.id, 'translate')
    row.status = 'done'
    row.input_fingerprint = compute_fingerprint(project, 'translate', legacy_execution=True)
    row.input_snapshot = '{}'
    crud.save_step(row)
    assert promote_compatible_fingerprints() == 1
    assert crud.get_step(project.id, 'translate').input_snapshot == '{}'


def test_legacy_split_and_translation_promote_in_dependency_order_without_rewriting_text(make_project):
    project, _ = successful_legacy_translation(make_project)
    deps = Dependencies(project)
    source = deps.path('split', 'split.txt')
    content = source.read_bytes()
    baseline = stale_explanation.capture(deps, 'split')
    baseline['fingerprint'] = compute_fingerprint(project, 'split', legacy_execution=True)
    baseline['entries'] = stale_explanation._payload_entries(deps, 'split',
        fingerprint_payload(project, 'split', legacy_execution=True))
    row = crud.get_step(project.id, 'split')
    row.input_fingerprint = baseline['fingerprint']
    row.input_snapshot = json.dumps(baseline)
    crud.save_step(row)
    values = profiles()
    values[0]['model'] = 'changed-model'
    config.set_key('llm.profiles', values)
    assert promote_compatible_fingerprints() == 2
    current = Dependencies(project)
    assert current.fresh('split') and current.fresh('translate')
    assert source.read_bytes() == content


def test_new_model_keeps_completed_output_identity_but_separates_response_caches(make_project):
    import threading
    from dubforge.runner import StepContext
    from dubforge.pipeline.step_translate import _chunk_key
    from dubforge.pipeline.semantic_cache import cache_key
    config.set_key('llm.profiles', profiles())
    project = crud.get_project(make_project())
    root = crud.project_dir(project.uid)
    first = StepContext(project, 'translate', root, root / 'logs/cache-first.log', threading.Event())
    chunk = [dict(id=1, text='Hello.', start=0.0, end=1.0)]
    output_before = compute_fingerprint(project, 'translate')
    window_before = _chunk_key(first, chunk, 'window-v6')
    semantic_before = cache_key('prompt', first.llm_profile_snapshot(), 'Hello.', 'v1', {})
    values = profiles()
    values[0]['model'] = 'replacement-model'
    values[0]['params']['temperature'] = .8
    config.set_key('llm.profiles', values)
    second = StepContext(project, 'translate', root, root / 'logs/cache-second.log', threading.Event())
    assert compute_fingerprint(project, 'translate') == output_before
    assert _chunk_key(second, chunk, 'window-v6') != window_before
    assert cache_key('prompt', second.llm_profile_snapshot(), 'Hello.', 'v1', {}) != semantic_before
    assert _chunk_key(first, chunk, 'window-v6') == window_before


def test_invalid_new_model_params_do_not_destroy_evidenced_successful_content(make_project):
    project, _ = successful_legacy_translation(make_project)
    values = profiles()
    values[0]['params'] = []
    config.set_key('llm.profiles', values)
    assert promote_compatible_fingerprints() >= 1
    assert Dependencies(project).fresh('translate')


@pytest.mark.parametrize('step,path,value', [
    ('download', 'download.proxy', 'http://127.0.0.1:8888'),
    ('download', 'download.cookies_path', 'cookies-new.txt'),
    ('tts', 'tts.indextts25.url', 'http://127.0.0.1:9999/'),
    ('tts', 'tts.gptsovits.url', 'http://127.0.0.1:9998/'),
])
def test_runtime_connection_change_does_not_invalidate_completed_artifacts(make_project, step, path, value):
    project = crud.get_project(make_project())
    before = compute_fingerprint(project, step)
    contract = snapshot(Dependencies(project), step, execution=True)
    config.set_key(path, value)
    assert compute_fingerprint(project, step) == before
    assert snapshot(Dependencies(project), step, execution=True) != contract
