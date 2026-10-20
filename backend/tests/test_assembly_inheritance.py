"""合成基础设置显式应用：隔离项目与配置，不生成媒体或调用外部服务。"""
import copy
import json

import pytest
from fastapi.testclient import TestClient

from dubforge import configuration as cfg
from dubforge.configuration_models import ConfigurationScope
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import assembly_inheritance as inheritance, assembly_versions as versions


def _save(key, updates):
    current = cfg.resolve(key=key)
    return cfg.save(key, dict(revision=current['revision'], fingerprint=current['fingerprint'], updates=updates))


def _bind(uid, template):
    plan = cfg.binding_plan([uid], template['key'][7:])
    cfg.bind(dict(uids=[uid], template_id=template['key'][7:], confirmed=True, fingerprint=plan['fingerprint']))


def _stored(uid):
    with crud.session() as session:
        row = session.get(ConfigurationScope, 'project:' + uid)
        return row.model_dump()


def _apply(uid, plan=None):
    return inheritance.apply(uid, dict(fingerprint=(plan or inheritance.plan(uid))['fingerprint'], confirmed=True))


def _step(uid, key, status):
    step = crud.get_step(crud.get_project(uid).id, key)
    step.status = status
    crud.save_step(step)


def test_scope_is_the_public_render_contract_without_runtime_or_other_production_paths():
    from dubforge.pipeline.step_params import RUNTIME_PARAM_PATHS
    selected = inheritance.paths()
    assert 'assembly.versions' in selected
    assert {'assemble.subtitle_min_size', 'assemble.subtitle_wrap_width_percent', 'comments.position',
            'subtitles.min_duration', 'assemble.crf', 'assemble.voice_volume'} <= selected
    assert not selected & RUNTIME_PARAM_PATHS
    assert not any(path.startswith(('tts.', 'asr.', 'split.', 'translate.', 'publish.', 'content_review.')) for path in selected)


def test_global_changes_are_only_applied_explicitly_and_summary_does_not_probe_video(make_project, monkeypatch):
    uid = make_project()
    before = cfg.resolve(uid)
    _save('global', {'assemble.subtitle_min_size': 36, 'assemble.subtitle_source_min_size': 30})
    assert cfg.resolve(uid)['values']['assemble.subtitle_min_size'] == before['values']['assemble.subtitle_min_size']
    stored = _stored(uid)
    from dubforge.pipeline import output_cuts
    monkeypatch.setattr(output_cuts, '_duration', lambda *args: pytest.fail('继承预览不应启动 ffprobe'))
    plan = inheritance.plan(uid)
    assert plan['source'] == {'kind': 'global', 'name': '全局默认'}
    assert plan['changed_count'] == plan['effective_count'] == 2
    assert _stored(uid) == stored
    result = _apply(uid, plan)
    assert result['inheritance']['changed_count'] == 0
    assert result['defaults']['assemble.subtitle_min_size'] == 36
    assert result['defaults']['assemble.subtitle_source_min_size'] == 30
    assert cfg.resolve(uid)['sources']['assemble.subtitle_min_size'] == {'kind': 'global', 'frozen': True}


def test_current_bound_template_is_used_instead_of_switching_to_global(make_project):
    uid = make_project()
    template = cfg.create_template('AI 新闻')
    _save(template['key'], {'assemble.subtitle_color': '#FFE082'})
    _bind(uid, template)
    _save('global', {'assemble.subtitle_color': '#AA1122', 'assemble.subtitle_min_size': 36})
    _save(template['key'], {'assemble.subtitle_color': '#11AA22'})
    plan = inheritance.plan(uid)
    assert plan['source'] == {'kind': 'series', 'name': 'AI 新闻'}
    assert plan['source_name'] == 'AI 新闻'
    _apply(uid, plan)
    result = cfg.resolve(uid)
    assert result['parent'] == template['key']
    assert result['values']['assemble.subtitle_color'] == '#11AA22'
    assert result['values']['assemble.subtitle_min_size'] == 36
    assert result['sources']['assemble.subtitle_color'] == {'kind': 'series', 'name': 'AI 新闻', 'frozen': True}


def test_apply_updates_only_render_baseline_and_preserves_exact_project_patch(make_project):
    uid = make_project()
    _save('project:' + uid, {'assemble.subtitle_color': '#FEDCBA', 'publish.title': '本片已核对标题'})
    _save('global', {'assemble.subtitle_color': '#112233', 'assemble.subtitle_min_size': 36,
                     'download.resolution': 720, 'publish.title': '新全局标题', 'comments.count': 9,
                     'content_review.blocked_terms': '仅全局新增'})
    before, stored = cfg.resolve(uid), _stored(uid)
    root = crud.project_dir(uid)
    cuts = root / 'output_cuts.json'
    cuts.write_text('{"仅证明剪辑方案不被应用设置修改":true}', encoding='utf-8')
    plan = inheritance.plan(uid)
    assert plan['preserved_count'] == 1
    color = next(item for item in plan['changes'] if item['path'] == 'assemble.subtitle_color')
    assert color['overridden'] is True
    assert color['before_source']['frozen'] and color['after_source']['frozen']
    _apply(uid, plan)
    after, saved = cfg.resolve(uid), _stored(uid)
    assert saved['values'] == stored['values']
    assert saved['parent'] == stored['parent']
    assert after['values']['assemble.subtitle_color'] == '#FEDCBA'
    assert after['values']['assemble.subtitle_min_size'] == 36
    for group in ('values', 'origins', 'sources'):
        for path in before['baseline'][group].keys() - inheritance.paths():
            assert after['baseline'][group][path] == before['baseline'][group][path]
    assert set(after['baseline']['configured']) - inheritance.paths() == set(before['baseline']['configured']) - inheritance.paths()
    assert after['values']['publish.title'] == '本片已核对标题'
    assert after['values']['comments.count'] == before['values']['comments.count']
    assert after['values']['content_review.blocked_terms'] == before['values']['content_review.blocked_terms']
    assert cuts.read_text(encoding='utf-8') == '{"仅证明剪辑方案不被应用设置修改":true}'
    assert saved['revision'] == stored['revision'] + 1
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['configuration']['baseline']['values']['assemble.subtitle_min_size'] == 36


def test_whole_project_version_list_and_explicit_equal_overrides_remain_fixed(make_project):
    uid = make_project()
    local = copy.deepcopy(cfg.resolve(uid)['values']['assembly.versions'])
    local[0]['overrides']['assemble.subtitle_size'] = 48
    local[0]['name'] = '本片精调配音'
    versions.save(uid, {'versions': local, 'revision': cfg.resolve(uid)['revision']})
    stored = _stored(uid)
    _save('global', {'assemble.subtitle_size': 56, 'assemble.subtitle_min_size': 36,
                     'assembly.versions': [dict(id='other', name='上级新版', dubbing=False,
                        burn_subtitles=True, include_comments=False, overrides={})]})
    plan = inheritance.plan(uid)
    assert plan['version_list_fixed'] and plan['preserved_version_count'] == len(local)
    list_change = next(item for item in plan['changes'] if item['path'] == 'assembly.versions')
    assert list_change['overridden']
    dub = next(row for row in plan['versions'] if row['id'] == local[0]['id'])
    assert not any(row['path'] == 'assemble.subtitle_size' for row in dub['changes'])
    assert any(row['path'] == 'assemble.subtitle_size' and row['value'] == 48 for row in dub['preserved_overrides'])
    _apply(uid, plan)
    assert cfg.resolve(uid)['values']['assembly.versions'] == local
    assert _stored(uid)['values'] == stored['values']


def test_inherited_version_specific_values_show_real_source_and_still_explain_preservation(make_project):
    uid = make_project()
    template = cfg.create_template('字幕模板')
    source_list = copy.deepcopy(cfg.resolve(uid)['values']['assembly.versions'])
    source_list[0]['overrides']['assemble.subtitle_size'] = 48
    _save(template['key'], {'assembly.versions': source_list})
    _bind(uid, template)
    _save(template['key'], {'assemble.subtitle_size': 56})
    plan = inheritance.plan(uid)
    assert not plan['version_list_fixed']
    fixed = next(row for row in plan['versions'][0]['preserved_overrides'] if row['path'] == 'assemble.subtitle_size')
    assert fixed['value'] == 48 and fixed['source']['kind'] == 'series'
    assert fixed['source']['frozen'] and fixed['source']['name'] == '字幕模板'
    current = versions.view(uid)
    source = current['versions'][0]['sources']['assemble.subtitle_size']
    assert source['kind'] == 'series' and source['name'] == '字幕模板' and source['frozen']
    assert source['version_name'] == source_list[0]['name']
    _save('project:' + uid, {'assembly.versions': source_list})
    current = versions.view(uid)
    assert current['versions'][0]['sources']['assemble.subtitle_size'] == {'kind': 'version', 'name': source_list[0]['name']}


@pytest.mark.parametrize('mutation', ['source', 'project', 'parent'])
def test_preview_rejects_concurrent_source_project_or_binding_changes_atomically(make_project, mutation):
    uid = make_project()
    _save('global', {'assemble.subtitle_min_size': 36})
    preview = inheritance.plan(uid)
    if mutation == 'source':
        _save('global', {'assemble.subtitle_min_size': 38})
    elif mutation == 'project':
        _save('project:' + uid, {'publish.title': '另一页面的新标题'})
    else:
        _bind(uid, cfg.create_template('另一模板'))
    before = _stored(uid)
    with pytest.raises(DubForgeError) as caught:
        _apply(uid, preview)
    assert caught.value.code == 'CONFIGURATION_CONFLICT'
    assert _stored(uid) == before


def test_unrelated_source_changes_do_not_invalidate_narrow_preview(make_project):
    uid = make_project()
    _save('global', {'assemble.subtitle_min_size': 36})
    preview = inheritance.plan(uid)
    _save('global', {'publish.title': '不属于合成的全局改动'})
    assert inheritance.plan(uid)['fingerprint'] == preview['fingerprint']
    _apply(uid, preview)
    assert cfg.resolve(uid)['values']['publish.title'] == ''


def test_new_list_cannot_silently_remove_existing_publication_selection(make_project):
    uid = make_project()
    _save('project:' + uid, {'publish.version_ids': ['dub']})
    source_list = cfg.resolve(uid)['values']['assembly.versions'][1:]
    _save('global', {'assembly.versions': source_list})
    before = _stored(uid)
    with pytest.raises(DubForgeError, match='发布分P'):
        inheritance.plan(uid)
    summary = versions.view(uid)['inheritance']
    assert summary['error'] and summary['fingerprint'] == ''
    assert _stored(uid) == before


@pytest.mark.parametrize('kind', ['project', 'template'])
def test_archived_project_or_source_cannot_apply(make_project, kind):
    uid = make_project()
    if kind == 'template':
        template = cfg.create_template('已归档来源')
        _bind(uid, template)
        cfg.archive(template['key'], {'revision': cfg.resolve(key=template['key'])['revision'], 'archived': True})
    else:
        from dubforge.project_archive import set_archived
        set_archived(uid, True)
    before = _stored(uid)
    with pytest.raises(DubForgeError):
        _apply(uid)
    assert _stored(uid) == before


@pytest.mark.parametrize('key,status,allowed', [('assemble', 'running', False), ('assemble', 'queued', True),
                                             ('download', 'running', True)])
def test_existing_field_locks_freeze_running_render_but_allow_queued_or_unrelated_work(make_project, key, status, allowed):
    uid = make_project()
    _save('global', {'assemble.subtitle_min_size': 36})
    preview = inheritance.plan(uid)
    _step(uid, key, status)
    before = _stored(uid)
    if allowed:
        _apply(uid, preview)
        assert cfg.resolve(uid)['values']['assemble.subtitle_min_size'] == 36
    else:
        with pytest.raises(DubForgeError) as caught:
            _apply(uid, preview)
        assert caught.value.code == 'CONFIGURATION_BUSY'
        assert _stored(uid) == before


@pytest.mark.parametrize('body', [None, {}, {'confirmed': 1, 'fingerprint': '0' * 64},
    {'confirmed': True, 'fingerprint': []}, {'confirmed': True, 'fingerprint': 'old'},
    {'confirmed': True, 'fingerprint': '0' * 64, 'paths': ['publish.title']},
    {'confirmed': True, 'fingerprint': '0' * 64, 'template_id': 'switch-source'}])
def test_apply_body_cannot_expand_scope_or_replace_source(make_project, body):
    uid = make_project()
    before = _stored(uid)
    with pytest.raises(DubForgeError):
        inheritance.apply(uid, body)
    assert _stored(uid) == before


def test_defaults_plan_get_and_bodyless_post_are_readonly_and_apply_returns_updated_view(make_project):
    from app.main import app
    uid = make_project()
    _save('global', {'assemble.subtitle_min_size': 36})
    client = TestClient(app)
    url = f'/api/projects/{uid}/assembly-versions'
    before = _stored(uid)
    get, post = client.get(url + '/defaults-plan'), client.post(url + '/defaults-plan')
    assert get.status_code == post.status_code == 200
    assert get.json() == post.json()
    assert get.headers['cache-control'] == 'no-store'
    assert _stored(uid) == before
    result = client.post(url + '/apply-defaults', json={'confirmed': True, 'fingerprint': get.json()['fingerprint']})
    assert result.status_code == 200
    assert result.json()['defaults']['assemble.subtitle_min_size'] == 36
    assert result.json()['inheritance']['changed_count'] == 0


def _enable_cuts(uid, monkeypatch):
    from dubforge.pipeline import output_cuts
    monkeypatch.setattr(output_cuts, 'source_context', lambda deps: {'source_fingerprint': 'fixture-source', 'duration': 100.})
    current = output_cuts.view(uid)
    return output_cuts.save(uid, dict(revision=current['revision'], source_fingerprint=current['source_fingerprint'],
        enabled=True, ranges=[dict(id='ad', start=10, end=20, enabled=True, reason='测试广告')]))


@pytest.mark.parametrize('cut', [False, True])
def test_crf_changes_affect_pure_original_version_only_when_cuts_require_encoding(make_project, monkeypatch, cut):
    from dubforge.pipeline.dependencies import Dependencies
    _save('global', {'assemble.crf': 18})
    uid = make_project()
    spec = dict(id='original', name='原声无字幕', dubbing=False, burn_subtitles=False, include_comments=False, overrides={})
    _save('project:' + uid, {'assembly.versions': [spec], 'content_review.enabled': False})
    if cut:
        _enable_cuts(uid, monkeypatch)
    _save('global', {'assemble.crf': 28})
    plan = inheritance.plan(uid)
    crf_changes = [row for row in plan['versions'][0]['changes'] if row['path'] == 'assemble.crf']
    assert bool(crf_changes) is cut
    if cut:
        assert crf_changes[0]['before'] == 18 and crf_changes[0]['after'] == 28
    deps = Dependencies(crud.get_project(uid))
    before = versions.resolve(deps, spec)
    after = copy.deepcopy(before)
    after['values']['assemble.crf'] = 28
    assert (versions.fingerprint(deps, before) != versions.fingerprint(deps, after)) is cut


def test_enabling_cuts_after_preview_requires_new_defaults_comparison(make_project, monkeypatch):
    uid = make_project()
    _save('global', {'assemble.crf': 28})
    preview = inheritance.plan(uid)
    _enable_cuts(uid, monkeypatch)
    before = _stored(uid)
    with pytest.raises(DubForgeError) as caught:
        _apply(uid, preview)
    assert caught.value.code == 'CONFIGURATION_CONFLICT'
    assert _stored(uid) == before


def test_versions_view_reuses_already_read_cut_state_for_inheritance_summary(make_project, monkeypatch):
    from dubforge.pipeline import output_cuts
    uid = make_project()
    _enable_cuts(uid, monkeypatch)
    calls = []
    def context(deps):
        calls.append(deps.project.uid)
        return {'source_fingerprint': 'fixture-source', 'duration': 100.}
    monkeypatch.setattr(output_cuts, 'source_context', context)
    result = versions.view(uid)
    assert result['inheritance']['fingerprint']
    assert calls == [uid]
