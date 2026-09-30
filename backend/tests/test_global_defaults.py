"""全局样式继承、参数推荐与快照边界；不使用真实配置或服务。"""
import copy

import pytest
import yaml
from fastapi.testclient import TestClient

from app.main import app
from dubforge import comment_studio as cs, config, workflows
from dubforge.backends.tts_indextts25 import synthesis_profiles
from dubforge.domain import crud
from dubforge.pipeline.step_params import COMMENT_DEFAULT_FIELDS, STEP_PARAM_SCHEMA, validate_numeric_updates


def test_global_schema_covers_project_fields_and_comment_style(make_project):
    uid = make_project()
    api = TestClient(app)
    global_steps = api.get('/api/settings/step_params').json()['steps']
    project_steps = api.get(f'/api/projects/{uid}/step_params').json()['steps']
    for step, fields in STEP_PARAM_SCHEMA.items():
        assert global_steps[step]['fields'] == project_steps[step]['fields']
        assert all(field.path in global_steps[step]['values'] for field in fields)
    assert 'comments' not in project_steps
    assert {field.path.removeprefix('comments.overlay.') for field in COMMENT_DEFAULT_FIELDS} == set(cs.DEFAULT_SETTINGS)


def test_template_and_factory_defaults_match(isolated_env):
    template = yaml.safe_load((config.REPO_ROOT / 'config.example.yaml').read_text(encoding='utf-8'))
    assert template['comments'] == config.DEFAULTS['comments']
    assert template['tts']['indextts25'] == config.DEFAULTS['tts']['indextts25']


def test_global_subtitle_colors_keep_project_overrides(make_project):
    uid = make_project(params={'step_params': {'assemble.subtitle_color': '#F7F3E8'}})
    api = TestClient(app)
    assert api.post('/api/settings', json={'updates': {'assemble.subtitle_color': '#FFD65A',
        'assemble.subtitle_outline_color': '#30213A'}}).status_code == 200
    values = api.get(f'/api/projects/{uid}/step_params').json()['steps']['assemble']['values']
    assert values['assemble.subtitle_color'] == '#F7F3E8'
    assert values['assemble.subtitle_outline_color'] == '#30213A'


def test_comment_and_workflow_use_same_inheritance_without_editing_content(make_project):
    from test_configuration import save
    first, second = make_project(), make_project()
    save('project:'+first, {'comments.color':'#FFFFFF'})
    cs._write(crud.get_project(first), {'overlay': {'settings': {'start': 8}, 'items': []}})
    snapshot = (crud.project_dir(first) / 'comment_studio.json').read_bytes()
    save('global', {'comments.color':'#FFD65A', 'comments.background_opacity':0})
    assert cs.view(second)['defaults']['color'] == '#FFD65A'
    assert workflows.view(second)['recipe']['comment_settings']['color'] == '#FFD65A'
    assert cs.view(first)['overlay']['settings']['color'] == '#FFFFFF'
    assert cs.view(first)['overlay']['settings']['background_opacity'] == 0
    assert workflows.view(first)['recipe']['comment_settings'] == cs.view(first)['overlay']['settings']
    assert (crud.project_dir(first) / 'comment_studio.json').read_bytes() == snapshot
    save('project:'+first, reset=['comments.color'])
    assert cs.view(first)['overlay']['settings']['color'] == '#FFD65A'




@pytest.mark.parametrize('patch', [
    {'color': 'red'}, {'duration': 16}, {'interval': 0}, {'position': []},
    {'font_size': None}, {'outline': 11}, {'background_opacity': 101},
    {'show_author': 'true'}, {'source_first': 1}, {'language_mode': {}}, {'unknown': True},
])
def test_invalid_comment_defaults_do_not_partially_save(isolated_env, patch):
    config.update_config({'assemble.subtitle_size': 48})
    before = config.config_path().read_bytes()
    response = TestClient(app).post('/api/settings', json={'updates': {
        'assemble.subtitle_size': 60, **{f'comments.overlay.{key}': value for key, value in patch.items()}}})
    assert response.status_code == 400, response.text
    assert config.config_path().read_bytes() == before


def test_partial_comment_update_checks_current_other_fields(isolated_env):
    api = TestClient(app)
    assert api.post('/api/settings', json={'updates': {'comments.overlay.interval': 30, 'comments.overlay.duration': 25}}).status_code == 200
    assert api.post('/api/settings', json={'updates': {'comments.overlay.interval': 20}}).status_code == 400
    assert config.get('comments.overlay.interval') == 30


def test_can_repair_manually_invalid_comment_default(isolated_env):
    config.update_config({'comments.overlay.duration': 20})
    response = TestClient(app).post('/api/settings', json={'updates': {'comments.overlay.duration': 6}})
    assert response.status_code == 200
    assert cs.default_settings()['duration'] == 6


def test_indextts_recommendations_are_valid_and_do_not_change_config(isolated_env):
    before = copy.deepcopy(config.load_config())
    balanced, fast = synthesis_profiles()
    for profile in (balanced, fast):
        validate_numeric_updates(profile['values'])
        assert not any(key.endswith(('.lang', '.timbre_mode', '.timbre_audio', '.emo_mode', '.emo_audio', '.url', '.project_dir', '.duration_retry_factors')) for key in profile['values'])
    assert balanced['values']['tts.indextts25.emo_weight'] == .65
    assert balanced['values']['tts.indextts25.duration_factor'] == 1
    assert balanced['values']['tts.indextts25.emo_random'] is False
    assert {key for key in balanced['values'] if balanced['values'][key] != fast['values'][key]} == {'tts.indextts25.num_beams'}
    assert fast['values']['tts.indextts25.num_beams'] == 1
    assert config.load_config() == before
