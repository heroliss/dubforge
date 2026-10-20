"""评论字号范围及软高度的跨入口配置契约，所有项目、配置和媒体均隔离。"""
import copy
import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.main import app
from dubforge import comment_studio as cs, config, configuration
from dubforge.configuration_models import ConfigurationScope
from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.pipeline import assembly_versions as av
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline.step_params import COMMENT_DEFAULT_FIELDS
from test_comment_studio import uid, plan_body
from test_configuration import save, bind


NEW_FIELDS = {'min_font_size', 'source_min_font_size', 'height_percent'}


def test_legacy_comment_settings_receive_safe_ranges_without_global_changes(isolated_env):
    old = {key: value for key, value in cs.DEFAULT_SETTINGS.items() if key not in NEW_FIELDS}
    old.update(font_size=18, source_font_size=20)
    config.update_config({'comments.overlay.font_size': 60, 'comments.overlay.min_font_size': 40,
                          'comments.overlay.source_font_size': 50, 'comments.overlay.source_min_font_size': 40,
                          'comments.overlay.height_percent': 70})
    result = cs.validate_settings(old)
    assert result['font_size'] == result['min_font_size'] == 18
    assert result['source_font_size'] == 20 and result['source_min_font_size'] == 18
    assert result['height_percent'] == 30
    assert not NEW_FIELDS.intersection(old)


@pytest.mark.parametrize('key,value', [
    ('min_font_size', 17), ('min_font_size', 73), ('min_font_size', None),
    ('source_min_font_size', True), ('source_min_font_size', float('nan')),
    ('height_percent', 0), ('height_percent', 101), ('height_percent', '30'),
    ('height_percent', float('inf')),
])
def test_invalid_ranges_are_rejected(key, value):
    with pytest.raises(cs.CommentError):
        cs.validate_settings({key: value})


@pytest.mark.parametrize('patch', [
    {'font_size': 36, 'min_font_size': 37},
    {'source_font_size': 30, 'source_min_font_size': 31, 'source_style_enabled': True},
])
def test_inverted_font_ranges_are_rejected_with_readable_error(patch):
    with pytest.raises(cs.CommentError, match='最小字号不能大于最大字号'):
        cs.validate_settings(patch)


def test_disabled_original_range_is_preserved_and_checked_when_enabled():
    result = cs.validate_settings({'source_font_size': 20, 'source_min_font_size': 30})
    assert not result['source_style_enabled']
    assert result['source_min_font_size'] == 30 and result['source_font_size'] == 20
    with pytest.raises(cs.CommentError, match='原文最小字号不能大于最大字号'):
        cs.validate_settings({**result, 'source_style_enabled': True})


def test_range_fields_are_available_in_global_project_and_version_schema(make_project):
    uid = make_project()
    global_schema = {field.path: field for field in COMMENT_DEFAULT_FIELDS}
    project_schema = {field['path']: field for field in configuration.schema()['comments']['fields']}
    version_schema = {field['path']: field for field in av.fields()}
    for name in NEW_FIELDS:
        assert 'comments.overlay.' + name in global_schema
        assert 'comments.' + name in project_schema
        assert 'comments.' + name in version_schema
        assert global_schema['comments.overlay.' + name].help
        assert version_schema['comments.' + name]['help']
    values = TestClient(app).get(f'/api/projects/{uid}/comments').json()['defaults']
    assert values['min_font_size'] == values['source_min_font_size'] == 18
    assert values['height_percent'] == 30


def test_new_ranges_inherit_through_template_project_and_version(make_project):
    uid = make_project()
    save('global', {'comments.min_font_size': 20, 'comments.height_percent': 45})
    template = configuration.create_template('字号范围模板')
    save(template['key'], {'comments.min_font_size': 22, 'comments.height_percent': 35})
    bind([uid], template)
    save('project:' + uid, {'comments.min_font_size': 24})
    settings = cs.project_settings(uid)
    assert settings['min_font_size'] == 24 and settings['height_percent'] == 35
    definition = dict(id='range', name='范围测试', dubbing=False, burn_subtitles=False,
                      include_comments=True, overrides={'comments.min_font_size': 26, 'comments.height_percent': 25})
    plan = dict(settings=settings, items=[dict(display_text='完整评论', source_text='Full comment')])
    deps = Dependencies(crud.get_project(uid))
    resolved = av.resolve(deps, definition, comment_plan=plan)
    assert resolved['comment_overlay']['settings']['min_font_size'] == 26
    assert resolved['comment_overlay']['settings']['height_percent'] == 25
    before = av.fingerprint(deps, resolved)
    definition['overrides']['comments.min_font_size'] = 27
    assert av.fingerprint(deps, av.resolve(deps, definition, comment_plan=plan)) != before
    definition['include_comments'] = False
    before = av.fingerprint(deps, av.resolve(deps, definition))
    definition['overrides']['comments.height_percent'] = 50
    assert av.fingerprint(deps, av.resolve(deps, definition)) == before


def test_global_range_cross_validation_is_atomic(isolated_env):
    config.update_config({'comments.overlay.font_size': 36})
    before = config.config_path().read_bytes()
    result = TestClient(app).post('/api/settings', json={'updates': {
        'assemble.subtitle_size': 60, 'comments.overlay.min_font_size': 40}})
    assert result.status_code == 400
    assert config.config_path().read_bytes() == before


def test_old_project_baseline_gets_factory_ranges_not_changed_global_range(make_project):
    uid = make_project()
    with Session(get_engine()) as session:
        row = session.get(ConfigurationScope, 'project:' + uid)
        doc = json.loads(row.baseline)
        for name in NEW_FIELDS:
            for field in ('values', 'origins', 'sources'):
                doc[field].pop('comments.' + name, None)
            doc['configured'] = [key for key in doc['configured'] if key != 'comments.' + name]
        doc['values']['comments.font_size'] = 18
        doc['values']['comments.source_font_size'] = 20
        row.baseline = json.dumps(doc)
        session.add(row); session.commit()
    config.update_config({'comments.overlay.font_size': 60, 'comments.overlay.min_font_size': 40,
                          'comments.overlay.source_font_size': 50, 'comments.overlay.source_min_font_size': 40,
                          'comments.overlay.height_percent': 70})
    result = cs.project_settings(uid)
    assert result['font_size'] == result['min_font_size'] == 18
    assert result['source_font_size'] == 20 and result['source_min_font_size'] == 18
    assert result['height_percent'] == 30
    with Session(get_engine()) as session:
        assert json.loads(session.get(ConfigurationScope, 'project:' + uid).baseline) == doc


def test_legacy_api_preview_and_save_complete_new_ranges_without_models(uid, tmp_path, monkeypatch):
    from dubforge.pipeline import comment_overlay, video_source
    monkeypatch.setattr(cs, 'chat_validated', lambda *args, **kwargs: pytest.fail('不应请求模型'))
    monkeypatch.setattr(video_source, 'resolve_video', lambda *args: tmp_path / 'isolated.mp4')
    monkeypatch.setattr(comment_overlay, 'video_canvas', lambda video: (1920, 1080))
    captured = []
    monkeypatch.setattr(comment_overlay, 'preview_result',
                        lambda video, plan, seconds: (captured.append(plan) or b'PNG', None))
    body = plan_body(uid)
    for key in NEW_FIELDS:
        body['settings'].pop(key)
    original = copy.deepcopy(body)
    api = TestClient(app)
    preview = api.post(f'/api/projects/{uid}/comments/preview', json={**body, 'seconds': 1})
    assert preview.status_code == 200, preview.text
    assert captured[0]['settings']['height_percent'] == 30
    assert captured[0]['settings']['min_font_size'] == 18
    saved = api.put(f'/api/projects/{uid}/comments/overlay', json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()['overlay']['settings']['source_min_font_size'] == 18
    assert body == original


def test_only_active_original_range_changes_render_fingerprint():
    plan = dict(settings=cs.validate_settings({}), items=[dict(display_text='中文', source_text='Original')])
    before = cs.render_fingerprint_plan(plan)
    plan['settings']['source_min_font_size'] = 26
    assert cs.render_fingerprint_plan(plan) == before
    plan['settings'].update(language_mode='bilingual', source_style_enabled=True)
    before = cs.render_fingerprint_plan(plan)
    plan['settings']['source_min_font_size'] = 27
    assert cs.render_fingerprint_plan(plan) != before


def test_missing_new_ranges_and_explicit_defaults_have_the_same_render_fingerprint():
    plan = dict(settings=cs.validate_settings({}), items=[dict(display_text='完整评论')])
    legacy = copy.deepcopy(plan)
    for name in NEW_FIELDS:
        legacy['settings'].pop(name)
    assert cs.render_fingerprint_plan(plan) == cs.render_fingerprint_plan(legacy)
    assert not NEW_FIELDS.intersection(legacy['settings'])


def test_version_font_range_is_checked_against_inherited_settings(make_project):
    uid = make_project()
    versions = configuration.resolve(uid)['values']['assembly.versions']
    versions[0]['overrides']['comments.min_font_size'] = 40
    with pytest.raises(configuration.ConfigurationError, match='最小字号不能大于最大字号'):
        save('project:' + uid, {'assembly.versions': versions})
