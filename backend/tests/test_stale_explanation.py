"""脱敏输入基线与需更新理由；全部使用隔离数据与本地伪产物。"""
import copy
import json

import pytest

from conftest import mark_upstream_done
from dubforge import config, configuration
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline import stale_explanation as stale
from dubforge.runner import compute_fingerprint
from dubforge.pipeline import assembly_versions as av
from dubforge.pipeline.assembly_models import AssemblyResult
from dubforge.db import get_engine
from sqlmodel import Session


def save_output(uid, key, name, text):
    root = crud.project_dir(uid)
    path = root / f'artifacts/{key}/{name}'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    crud.add_artifact(crud.get_project(uid).id, key, 'text', name, path, str(path.relative_to(root)))
    return path


@pytest.fixture()
def baseline(make_project):
    uid = make_project()
    mark_upstream_done(uid, 'translate')
    save_output(uid, 'translate', 'translate.txt', '1. 你好\n')
    project = crud.get_project(uid)
    deps = Dependencies(project)
    snapshot = stale.capture(deps, 'translate')
    step = crud.get_step(project.id, 'translate')
    step.status = 'done'; step.input_fingerprint = snapshot['fingerprint']
    step.input_snapshot = json.dumps(snapshot)
    crud.save_step(step)
    return uid, snapshot


def patch(uid, values):
    cfg = configuration.resolve(uid)
    configuration.save('project:' + uid, dict(revision=cfg['revision'], updates=values))


def report(uid, key='translate', stored=None):
    return Dependencies(crud.get_project(uid)).explain(key, stored)


def find(report, key):
    return next(c for c in report['changes'] if c['key'] == key)


def test_exact_safe_parameter_diff_and_current_origin(baseline):
    uid, _ = baseline
    assert report(uid)['fresh'] is True
    patch(uid, {'translate.target_language': '日本語'})
    result = report(uid)
    change = find(result, 'translate.target_language')
    assert result['history_available'] and not result['fresh']
    assert change['before'] == '简体中文' and change['after'] == '日本語' and change['before_known']
    assert change['origin_before'] == dict(kind='execution', label='生成时生效设置')
    assert change['origin_after'] == dict(kind='project', label='本项目')


def test_prompt_text_never_saved_but_changed_digest_is_explained(baseline):
    uid, _ = baseline
    text = '不应暴露的自由提示词\n' + 'sk-secret999999999999999999'
    patch(uid, {'translate.style_prompt': text})
    snapshot = stale.capture(Dependencies(crud.get_project(uid)), 'translate')
    assert text not in json.dumps(snapshot, ensure_ascii=False)
    change = find(report(uid), 'translate.style_prompt')
    assert change['category'] == 'prompt' and change['before_known']
    assert change['before'] != change['after'] and '摘要' in change['after']


def test_prompt_version_change_uses_real_historical_value(baseline, monkeypatch):
    from dubforge.pipeline import text_prompts
    uid, snapshot = baseline
    old = snapshot['entries']['rule.translation_prompt']['display']
    monkeypatch.setattr(text_prompts, 'TRANSLATION_PROMPT_VERSION', 'future-version-fixture')
    change = find(report(uid), 'rule.translation_prompt')
    assert change['category'] == 'prompt' and change['before'] == old
    assert change['after'] == 'future-version-fixture'


def test_missing_history_cannot_invent_old_values_or_specific_prompt_cause(baseline):
    uid, _ = baseline
    step = crud.get_step(crud.get_project(uid).id, 'translate')
    step.input_snapshot = '{}'; step.input_fingerprint = 'unknown-old-fingerprint'
    crud.save_step(step)
    result = report(uid)
    assert not result['history_available'] and not result['fresh']
    assert all(c['before'] is None and not c['before_known'] for c in result['changes'])
    assert all(c['category'] != 'prompt' for c in result['changes'])
    assert '无法还原具体旧值' in result['warnings'][0]


@pytest.mark.parametrize('issue', ['bad-json', 'schema', 'step', 'fingerprint', 'scope'])
def test_invalid_history_is_honestly_unavailable(baseline, issue):
    uid, snapshot = baseline
    snapshot = copy.deepcopy(snapshot)
    if issue == 'bad-json': snapshot = '{invalid'
    else: snapshot[issue] = 'invalid'
    patch(uid, {'translate.target_language': '日本語'})
    assert report(uid, stored=snapshot)['history_available'] is False


@pytest.mark.parametrize('status', ['failed', 'interrupted', 'running', 'queued'])
def test_last_success_history_survives_attempt_fingerprint_with_clear_warning(baseline, status):
    uid, _ = baseline
    patch(uid, {'translate.target_language': '日本語'})
    step = crud.get_step(crud.get_project(uid).id, 'translate')
    step.status = status; step.input_fingerprint = compute_fingerprint(crud.get_project(uid), 'translate')
    crud.save_step(step)
    result = report(uid)
    assert result['history_available'] and find(result, 'translate.target_language')['before'] == '简体中文'
    assert any('上次成功生成' in warning for warning in result['warnings'])


def test_same_path_glossary_bytes_change_and_unavailable_file(baseline, tmp_path):
    uid, _ = baseline
    glossary = tmp_path / 'private-terms.xlsx'; glossary.write_bytes(b'old-terms')
    patch(uid, {'translate.custom_terms_path': str(glossary)})
    deps = Dependencies(crud.get_project(uid)); before = stale.capture(deps, 'translate')
    step = crud.get_step(deps.project.id, 'translate')
    step.input_fingerprint = before['fingerprint']; step.input_snapshot = json.dumps(before); crud.save_step(step)
    glossary.write_bytes(b'new-terms')
    change = find(report(uid), 'glossary.contents')
    assert change['category'] == 'glossary' and change['before'] != change['after']
    assert str(glossary) not in json.dumps(before)
    glossary.unlink()
    change = find(report(uid), 'glossary.contents')
    assert change['after'] == '术语表文件无法读取'


def test_input_content_change_has_text_reason_not_invented_parameter(baseline):
    uid, _ = baseline
    save_output(uid, 'split', 'split.txt', '1. 0.000-1.000: changed source\n')
    result = report(uid)
    assert find(result, 'input.split/split.txt')['category'] == 'text'
    assert not any(c['category'] == 'parameter' for c in result['changes'])


def test_missing_actual_output_is_proven_without_history(baseline):
    uid, _ = baseline
    project = crud.get_project(uid)
    art = crud.latest_artifacts(project.id, ['translate'])['translate/translate.txt']
    (crud.project_dir(uid) / art.rel_path).unlink()
    result = report(uid, stored={})
    assert not result['fresh'] and not result['history_available']
    assert find(result, 'output.translate.txt')['category'] == 'missing'
    assert find(result, 'output.translate.txt')['after'] == '文件缺失'


def test_upstream_status_alone_does_not_invalidate_unchanged_consumer(baseline):
    uid, _ = baseline
    step = crud.get_step(crud.get_project(uid).id, 'split')
    step.status = 'stale'; crud.save_step(step)
    result = report(uid)
    # 只有 split 的实际产物被替换/失效时，translate 才会重新核对输入；
    # 单独改写步骤状态不能证明文件内容变化，也不能误报下游过期。
    assert result['fresh'] is True
    assert not any(c.get('upstream_step') == 'split' for c in result['changes'])


def test_credentials_urls_paths_headers_and_unknown_restored_fields_never_leak(baseline):
    uid, snapshot = baseline
    secret = 'sk-super-secret-fixture000000000'
    config.set_key('llm.profiles', [dict(name='fixture', enabled=True, model='model-a',
        base_url='https://private-user:private-pass@model.invalid/v1?token=private-query', api_key=secret,
        headers={'Authorization': 'Bearer unknown-header-secret'})])
    deps = Dependencies(crud.get_project(uid))
    snap = stale.capture(deps, 'translate')
    serialized = json.dumps(snap)
    for value in [secret, 'private-user', 'private-pass', 'private-query', 'unknown-header-secret', 'api_key', 'headers', 'Authorization', 'https://']:
        assert value not in serialized
    snapshot = copy.deepcopy(snapshot)
    snapshot['entries']['llm.profile.0.headers'] = dict(value='old', display='unknown-header-secret', category='model',
                                                      origin={'kind': 'credential', 'label': secret})
    snapshot['entries']['translate.target_language']['origin'] = {'kind': 'execution', 'label': 'password=hidden-old-secret'}
    patch(uid, {'translate.target_language': '日本語'})
    serialized = json.dumps(report(uid, stored=snapshot), ensure_ascii=False)
    assert 'unknown-header-secret' not in serialized and 'hidden-old-secret' not in serialized and secret not in serialized


def test_restored_digest_entry_cannot_display_private_prompt_or_path(baseline):
    uid, snapshot = baseline
    snapshot = copy.deepcopy(snapshot)
    snapshot['entries']['translate.style_prompt']['display'] = '私人提示词正文不得显示'
    snapshot['entries']['translate.custom_terms_path']['display'] = 'D:/private/never-show.xlsx'
    patch(uid, {'translate.style_prompt': 'new style'})
    result = report(uid, stored=snapshot)
    serialized = json.dumps(result, ensure_ascii=False)
    assert '私人提示词正文不得显示' not in serialized and 'private' not in serialized
    assert '内容摘要' in find(result, 'translate.style_prompt')['before']


def test_malformed_model_endpoint_does_not_break_history_capture(baseline):
    uid, _ = baseline
    config.set_key('llm.profiles', [dict(name='fixture', enabled=True, model='model-a', base_url='https://example.invalid:bad-port', api_key='never-save')])
    snapshot = stale.capture(Dependencies(crud.get_project(uid)), 'translate')
    assert 'bad-port' not in json.dumps(snapshot) and 'never-save' not in json.dumps(snapshot)


@pytest.mark.parametrize('scope', ['all', 'default'])
def test_regular_scope_aliases_are_saved_as_full(baseline, scope):
    uid, _ = baseline
    assert stale.capture(Dependencies(crud.get_project(uid)), 'translate', scope=scope)['scope'] == 'full'


@pytest.mark.parametrize('scope', ['selected', 'preview'])
def test_partial_production_cannot_claim_full_history(baseline, scope):
    uid, _ = baseline
    snapshot = stale.capture(Dependencies(crud.get_project(uid)), 'translate', scope=scope)
    assert report(uid, stored=snapshot)['history_available'] is False


def test_tts_pending_feedback_does_not_overwrite_step_name(make_project, monkeypatch):
    project = crud.get_project(make_project()); deps = Dependencies(project)
    monkeypatch.setattr(deps, 'fresh', lambda key: False)
    monkeypatch.setattr(deps, 'dubbing_summary', lambda: {'known': True, 'pending': 2, 'pending_ids': [2, 5]})
    feedback = deps.freshness_feedback('tts')
    assert feedback['freshness_label'] == '仍有待配音句' and '只需核对并补齐' in feedback['freshness_reason']
    assert 'label' not in feedback
    deps.steps['tts'].status = 'running'
    assert deps.freshness_feedback('tts') == {}


def test_tts_unknown_input_feedback_is_distinct(make_project, monkeypatch):
    deps = Dependencies(crud.get_project(make_project()))
    monkeypatch.setattr(deps, 'fresh', lambda key: False)
    monkeypatch.setattr(deps, 'dubbing_summary', lambda: {'known': False, 'pending': 0})
    assert deps.freshness_feedback('tts')['freshness_label'] == '待核对配音输入'


def test_no_global_project_or_artifact_write_during_explanation(baseline):
    uid, snapshot = baseline
    patch(uid, {'translate.target_language': '日本語'})
    project = crud.get_project(uid)
    before_cfg = configuration.resolve(uid)
    before_step = crud.get_step(project.id, 'translate').model_dump()
    before_arts = {k: a.model_dump() for k, a in crud.latest_artifacts(project.id, None).items()}
    report(uid)
    assert configuration.resolve(uid) == before_cfg
    assert crud.get_step(project.id, 'translate').model_dump() == before_step
    assert {k: a.model_dump() for k, a in crud.latest_artifacts(project.id, None).items()} == before_arts


def test_versions_scope_compares_actual_selected_version_and_safe_render_parameters(baseline):
    uid, _ = baseline
    definition = dict(id='local-source', name='原声核对版', dubbing=False, burn_subtitles=True,
                      include_comments=False, overrides={})
    patch(uid, {'assembly.versions': [definition], 'content_review.enabled': False})
    deps = Dependencies(crud.get_project(uid)); version = av.resolve(deps, definition)
    path = save_output(uid, 'assemble', 'final_version_local-source.mp4', 'fixture final')
    art = crud.latest_artifacts(deps.project.id, ['assemble'])['assemble/final_version_local-source.mp4']
    with Session(get_engine()) as session:
        session.add(AssemblyResult(key=uid + ':local-source', project_uid=uid, version_id='local-source',
            artifact_id=art.id, artifact_hash=art.sha256, fingerprint=av.fingerprint(deps, version), snapshot=json.dumps(version)))
        session.commit()
    snapshot = stale.capture(deps.with_versions([version]), 'assemble', scope='versions')
    step = crud.get_step(deps.project.id, 'assemble'); step.status = 'done'
    step.input_fingerprint = snapshot['fingerprint']; step.input_snapshot = json.dumps(snapshot); crud.save_step(step)
    assert report(uid, 'assemble')['fresh'] is True
    old_size = version['values']['assemble.subtitle_size']
    patch(uid, {'assemble.subtitle_size': old_size + 2})
    result = report(uid, 'assemble')
    assert result['history_available'] and not result['fresh']
    change = find(result, 'version.local-source.assemble.subtitle_size')
    assert change['before'] == str(old_size) and change['after'] == str(old_size + 2)
    assert change['label'].startswith('原声核对版')


def test_versions_scope_missing_definition_does_not_compare_default_parameters(baseline):
    uid, _ = baseline
    definition = dict(id='local-source', name='原声核对版', dubbing=False, burn_subtitles=True,
                      include_comments=False, overrides={})
    patch(uid, {'assembly.versions': [definition], 'content_review.enabled': False})
    deps = Dependencies(crud.get_project(uid)); version = av.resolve(deps, definition)
    snapshot = stale.capture(deps.with_versions([version]), 'assemble', scope='versions')
    step = crud.get_step(deps.project.id, 'assemble'); step.status = 'done'
    step.input_fingerprint = snapshot['fingerprint']; step.input_snapshot = json.dumps(snapshot); crud.save_step(step)
    patch(uid, {'assembly.versions': [{**definition, 'id': 'replacement'}], 'assemble.subtitle_size': 30})
    result = report(uid, 'assemble')
    assert result['history_available'] is False
    assert all(c['before_known'] is False for c in result['changes'])


def test_outputs_scope_preserves_selected_variants_and_can_compare_old_safe_values(baseline):
    uid, _ = baseline
    patch(uid, {'content_review.enabled': False})
    deps = Dependencies(crud.get_project(uid), assemble_variants=['src_hardsub'])
    snapshot = stale.capture(deps, 'assemble', scope='outputs',
                             fingerprint='outputs:' + compute_fingerprint(deps.project, 'assemble', dependencies=deps))
    step = crud.get_step(deps.project.id, 'assemble'); step.status = 'done'
    step.input_fingerprint = snapshot['fingerprint']; step.input_snapshot = json.dumps(snapshot); crud.save_step(step)
    patch(uid, {'assemble.crf': 19})
    result = report(uid, 'assemble')
    assert snapshot['selection']['variants'] == ['src_hardsub'] and result['history_available']
    assert find(result, 'assemble.crf')['after'] == '19'


def test_missing_auto_comment_plan_and_legacy_commented_outputs_have_incomplete_history(baseline):
    uid, _ = baseline
    definition = dict(id='commented', name='评论版', dubbing=False, burn_subtitles=False,
                      include_comments=True, overrides={})
    patch(uid, {'assembly.versions': [definition]})
    deps = Dependencies(crud.get_project(uid)); version = av.resolve(deps, definition)
    snapshot = stale.capture(deps.with_versions([version]), 'assemble', scope='versions')
    assert snapshot['incomplete_comments'] is True
    step = crud.get_step(deps.project.id, 'assemble'); step.status = 'done'; step.input_fingerprint = snapshot['fingerprint']
    crud.save_step(step)
    result = report(uid, 'assemble', stored=snapshot)
    assert result['history_available'] is False and any('烧录评论' in w for w in result['warnings'])
    snapshot = stale.capture(deps.with_assembly_variants(['src_comments']), 'assemble', scope='outputs')
    assert snapshot['incomplete_comments'] is True


def test_filtered_actual_comment_baseline_compares_same_filtered_current_plan(baseline, monkeypatch):
    from dubforge import comment_studio
    from dubforge.pipeline.content_redaction import safe_overlay
    from dubforge.pipeline.version_render import VersionContext
    uid, _ = baseline
    definition = dict(id='commented', name='评论版', dubbing=False, burn_subtitles=False,
                      include_comments=True, overrides={})
    patch(uid, {'assembly.versions': [definition], 'content_review.blocked_terms': 'blocked-fixture'})
    raw = dict(settings=comment_studio.default_settings(), items=[
        dict(id='safe', text='ordinary trip', display_text='正常旅行', source_hash='safe'),
        dict(id='blocked', text='blocked-fixture', display_text='被排除评论', source_hash='blocked')])
    monkeypatch.setattr(comment_studio, '_read', lambda project: {'overlay': raw})
    deps = Dependencies(crud.get_project(uid)); version = av.resolve(deps, definition, comment_plan=raw)
    version['comment_overlay'] = safe_overlay(VersionContext(deps, version), version['comment_overlay'])
    snapshot = stale.capture(deps.with_versions([version]), 'assemble', scope='versions')
    step = crud.get_step(deps.project.id, 'assemble'); step.status = 'done'; step.input_fingerprint = snapshot['fingerprint']
    crud.save_step(step)
    previous, current_deps = stale._history(Dependencies(crud.get_project(uid)), 'assemble', snapshot)
    current = stale.capture(current_deps, 'assemble', scope='versions')
    assert previous is not None and snapshot['incomplete_comments'] is False
    key = 'version.commented.text.comments'
    assert snapshot['entries'][key]['value'] == current['entries'][key]['value']
    assert [i['id'] for i in current_deps.assembly_versions[0]['comment_overlay']['items']] == ['safe']


@pytest.mark.parametrize('change', ['unchanged', 'safe-text', 'all-blocked'])
def test_regular_version_freshness_matches_actually_filtered_comments_without_breaking_lists(baseline, monkeypatch, change):
    from dubforge import comment_studio
    from dubforge.pipeline.content_redaction import safe_overlay
    uid, _ = baseline
    definition = dict(id='commented', name='评论版', dubbing=False, burn_subtitles=False,
                      include_comments=True, overrides={})
    patch(uid, {'assembly.versions': [definition], 'content_review.blocked_terms': 'blocked-fixture'})
    raw = dict(settings=comment_studio.default_settings(), items=[
        dict(id='safe', text='ordinary trip', display_text='正常旅行', source_hash='safe'),
        dict(id='blocked', text='blocked-fixture', display_text='被排除评论', source_hash='blocked')])
    monkeypatch.setattr(comment_studio, '_read', lambda project: {'overlay': raw})
    deps = Dependencies(crud.get_project(uid))
    source = av.resolve(deps, definition, comment_plan=raw)
    actual = {**source, 'comment_overlay': safe_overlay(deps.for_version(source), source['comment_overlay'])}
    actual_stamp = av.fingerprint(deps, actual)
    assert av.fingerprint(deps, source) == actual_stamp
    assert len(source['comment_overlay']['items']) == 2 and len(raw['items']) == 2
    save_output(uid, 'assemble', 'final_version_commented.mp4', 'isolated encoded safe comments')
    art = crud.latest_artifacts(deps.project.id, ['assemble'])['assemble/final_version_commented.mp4']
    with Session(get_engine()) as session:
        session.add(AssemblyResult(key=uid + ':commented', project_uid=uid, version_id='commented',
            artifact_id=art.id, artifact_hash=art.sha256, fingerprint=actual_stamp, snapshot=json.dumps(actual)))
        session.commit()
    if change == 'safe-text':
        raw['items'][0]['display_text'] = '正常旅行内容发生改变'
    elif change == 'all-blocked':
        raw['items'][0]['text'] = 'blocked-fixture'
    current = Dependencies(crud.get_project(uid))
    resolved = av.resolve(current, definition, comment_plan=raw)
    assert av.is_current(current, resolved) is (change == 'unchanged')
    assert av.assembly_state(current)['fresh'] is (change == 'unchanged')
    row = av.view(uid)['versions'][0]
    assert row['fresh'] is (change == 'unchanged')
    if change == 'all-blocked':
        with pytest.raises(DubForgeError) as error:
            av.fingerprint(current, resolved)
        assert error.value.code == 'COMMENTS_REVIEW_EMPTY'
