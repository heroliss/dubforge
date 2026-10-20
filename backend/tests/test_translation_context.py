"""背景资料采用显式快照；测试不联网、不调用真实模型、不使用生产数据。"""
import json
import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from dubforge import translation_context as context
from dubforge.domain import crud
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline import stale_explanation as stale
from dubforge.pipeline import step_translate as translate
from dubforge.runner import compute_fingerprint


def source_info(uid, **updates):
    path = crud.project_dir(uid) / 'artifacts/download/video_info.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    info = dict(title_original='Source title', description='Source description', summary_zh='已有摘要',
                source={'uploader': 'Author'}, collected_at='2026-10-07', view_count=2)
    info.update(updates)
    path.write_text(json.dumps(info, ensure_ascii=False), encoding='utf-8')
    return path


def save(uid, **fields):
    view = context.view(uid)
    value = {**view['context'], 'enabled': True, **fields}
    return context.update(uid, {'revision': view['revision'], 'context': value})


def test_read_reuses_cached_source_without_adopting_or_generating_summary(make_project, monkeypatch):
    from dubforge import video_info
    uid = make_project()
    source_info(uid)
    monkeypatch.setattr(video_info, 'chat_json', lambda *a, **k: pytest.fail('不得生成额外摘要'))
    before = compute_fingerprint(crud.get_project(uid), 'translate')
    client = TestClient(app)
    response = client.get(f'/api/projects/{uid}/steps/translate/context')
    assert response.status_code == 200
    view = response.json()
    assert response.headers['cache-control'] == 'no-store'
    assert not view['has_saved'] and not view['context']['enabled']
    assert view['source_fields']['title'] == 'Source title'
    assert view['source_fields']['summary'] == '已有摘要'
    assert context.effective(crud.project_params(crud.get_project(uid))) == {}
    assert compute_fingerprint(crud.get_project(uid), 'translate') == before


def test_stale_summary_is_not_offered_and_malformed_source_is_safe(make_project):
    uid = make_project()
    path = source_info(uid, summary_stale=True)
    assert context.view(uid)['source_fields']['summary'] == ''
    path.write_text('[1,2]', encoding='utf-8')
    assert not context.view(uid)['has_source']
    path.write_text('{bad', encoding='utf-8')
    assert not context.view(uid)['has_source']


def test_explicit_save_preserves_project_settings_and_is_in_manifest(make_project):
    uid = make_project(params={'auto_steps': ['split', 'translate'], 'arbitrary': {'keep': 1}})
    first = context.view(uid)
    body = {'revision': first['revision'], 'context': {**first['context'], 'enabled': True, 'terms': ' yen → 日元 '}}
    response = TestClient(app).put(f'/api/projects/{uid}/steps/translate/context', json=body)
    assert response.status_code == 200
    params = crud.project_params(crud.get_project(uid))
    assert params['auto_steps'] == ['split', 'translate'] and params['arbitrary'] == {'keep': 1}
    assert params[context.PARAM_KEY]['terms'] == 'yen → 日元'
    manifest = json.loads((crud.project_dir(uid) / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['params'][context.PARAM_KEY] == params[context.PARAM_KEY]


def test_save_rejects_old_revision_and_keeps_newer_context(make_project):
    uid = make_project()
    old = context.view(uid)
    save(uid, terms='newer')
    response = TestClient(app).put(f'/api/projects/{uid}/steps/translate/context', json={
        'revision': old['revision'], 'context': {**old['context'], 'terms': 'older'}})
    assert response.status_code == 400
    assert response.json()['code'] == 'TRANSLATION_CONTEXT_CONFLICT'
    assert context.view(uid)['context']['terms'] == 'newer'


@pytest.mark.parametrize('fields', [{'enabled': 'true'}, {'title': 'x' * 301}, {'terms': []},
                                    {'summary': 'bad\x00'}, {'extra': 'not accepted'}])
def test_save_validates_fields_without_silent_truncation(make_project, fields):
    uid = make_project()
    before = context.view(uid)
    with pytest.raises(context.TranslationContextError):
        context.update(uid, {'revision': before['revision'], 'context': {**before['context'], **fields}})
    assert not context.view(uid)['has_saved']


@pytest.mark.parametrize('status', ['running', 'queued', 'stopping'])
def test_background_save_allows_unrelated_step_while_project_busy(make_project, status):
    uid = make_project()
    project = crud.get_project(uid)
    step = crud.get_step(project.id, 'split')
    step.status = status
    crud.save_step(step)
    # 翻译背景只属于 translate。按 ADR-0132，split 运行/排队/停止时不
    # 冻结 translate 的字段，允许用户准备下一步设置；translate 自身
    # running 时的门禁另由路径锁测试覆盖。
    saved = save(uid, summary='可供后续翻译使用')
    assert saved['context']['summary'] == '可供后续翻译使用'
    assert context.view(uid)['has_saved']


def test_context_fingerprints_follow_only_adopted_fields_and_are_frozen(make_project):
    uid = make_project()
    project = crud.get_project(uid)
    original = {key: compute_fingerprint(project, key) for key in ['split', 'translate']}
    save(uid, enabled=False, terms='draft')
    assert compute_fingerprint(crud.get_project(uid), 'translate') == original['translate']
    save(uid, terms='adopted')
    frozen = Dependencies(crud.get_project(uid))
    used = compute_fingerprint(frozen.project, 'translate', dependencies=frozen)
    assert used != original['translate']
    assert compute_fingerprint(crud.get_project(uid), 'split') == original['split']
    source_info(uid, description='new platform metadata')
    assert compute_fingerprint(crud.get_project(uid), 'translate') == used
    save(uid, terms='revised')
    assert compute_fingerprint(crud.get_project(uid), 'translate') != used
    assert compute_fingerprint(frozen.project, 'translate', dependencies=frozen) == used
    save(uid, enabled=False)
    assert compute_fingerprint(crud.get_project(uid), 'translate') == original['translate']


def test_context_change_is_explained_per_field_and_never_exposes_text(make_project):
    uid = make_project()
    secret_text = 'private background words'
    save(uid, summary=secret_text)
    project = crud.get_project(uid)
    before = stale.capture(Dependencies(project), 'translate')
    assert secret_text not in json.dumps(before)
    step = crud.get_step(project.id, 'translate')
    step.status = 'done'; step.input_fingerprint = before['fingerprint']; step.input_snapshot = json.dumps(before)
    crud.save_step(step)
    save(uid, summary='new background words')
    report = Dependencies(crud.get_project(uid)).explain('translate')
    change = next(row for row in report['changes'] if row['key'] == 'translation_context.summary')
    assert change['before_known'] and change['before'] != change['after']
    assert change['label'] == '翻译背景 · 背景摘要'
    assert 'private' not in json.dumps(report)
    # 手工恢复的摘要展示文字也不得泄露。
    before['entries']['translation_context.summary']['display'] = secret_text
    report = Dependencies(crud.get_project(uid)).explain('translate', before)
    assert secret_text not in json.dumps(report)


@pytest.mark.parametrize('legacy,legacy_config', [(False, False), (False, True), (True, False), (True, True)])
def test_adopted_context_invalidates_all_legacy_fingerprints_without_promoting_old_translation(
        make_project, legacy, legacy_config):
    from dubforge.pipeline.config_fingerprint import promote_compatible_fingerprints
    uid = make_project()
    project = crud.get_project(uid)
    old = compute_fingerprint(project, 'translate', legacy=legacy, legacy_config=legacy_config)
    step = crud.get_step(project.id, 'translate')
    step.status = 'done'; step.input_fingerprint = old
    crud.save_step(step)
    assert Dependencies(project).matches_fingerprint('translate')
    save(uid, enabled=False, summary='unadopted draft')
    assert Dependencies(crud.get_project(uid)).matches_fingerprint('translate')
    save(uid, summary='explicitly adopted background')
    project = crud.get_project(uid)
    assert compute_fingerprint(project, 'translate', legacy=legacy, legacy_config=legacy_config) != old
    assert not Dependencies(project).matches_fingerprint('translate')
    promote_compatible_fingerprints()
    assert crud.get_step(project.id, 'translate').input_fingerprint == old
    assert not Dependencies(crud.get_project(uid)).matches_fingerprint('translate')
    # 取消采用后，旧输入确实相同的历史结果仍按原兼容机制复用。
    save(uid, enabled=False)
    assert Dependencies(crud.get_project(uid)).matches_fingerprint('translate')


def test_full_document_translation_consumes_context_once_without_changing_source(isolated_env, tmp_path, monkeypatch):
    from dubforge.backends import llm
    source = tmp_path / 'split.txt'
    source.write_text('1. 0.000-2.000: it costs two yen\n2. 2.000-4.000: it is not ready\n', encoding='utf-8')
    output = tmp_path / 'output'; output.mkdir()
    params = {context.PARAM_KEY: {'enabled': True, 'title': 'Useful title', 'terms': 'yen → 日元'}}
    values = {'translate.max_context_tokens': 200_000, 'translate.target_language': '简体中文'}
    profile = [dict(name='stub', base_url='https://fixture.invalid/v1', api_key='stub', model='stub', params={})]
    from dubforge.utils.fs import atomic_write_bytes
    ctx = SimpleNamespace(force=False, project_params=params, pdir=tmp_path, cfg=lambda key, default=None: values.get(key, default),
        llm_profile_snapshot=lambda _: profile, log=lambda *_: None, upstream=lambda *_: source,
        check_cancel=lambda: None, check_pause=lambda: None, report_progress=lambda *_: None,
        llm_trace=lambda *_a, **_kw: None, artifact_dir=lambda: output, register_file=lambda *_: None,
        write_json=lambda name, data: atomic_write_bytes(output / name, json.dumps(data, ensure_ascii=False).encode('utf-8')))
    calls = []
    def chat(prompt='', **kwargs):
        calls.append((prompt, kwargs))
        assert 'Useful title' in kwargs['system'] and 'yen → 日元' in kwargs['system']
        assert '有冲突以本行原文为准' in kwargs['system']
        assert 'it costs two yen' in prompt and 'it is not ready' in prompt
        assert re.findall(r'^(\d+)\. ', prompt, re.MULTILINE) == ['1', '2']
        return '1. 花费两日元\n2. 尚未就绪'
    monkeypatch.setattr(llm, 'chat', chat)
    before = source.read_bytes()
    translate.run(ctx)
    assert len(calls) == 1 and source.read_bytes() == before
    assert (output / 'translate.txt').read_text(encoding='utf-8') == '1. 花费两日元\n2. 尚未就绪\n'
    old_key = translate._chunk_key(ctx, [{'id': 1, 'text': 'same'}], 'first')
    ctx.project_params = {context.PARAM_KEY: {'enabled': True, 'terms': 'yen → 日币'}}
    assert translate._chunk_key(ctx, [{'id': 1, 'text': 'same'}], 'first') != old_key
