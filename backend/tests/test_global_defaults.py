"""全局样式继承、参数推荐与快照边界；不使用真实配置或服务。"""
import copy

import pytest
import yaml
from fastapi.testclient import TestClient

from app.main import app
from dubforge import comment_studio as cs, config, workflows
from dubforge.domain import crud
from dubforge.pipeline.step_params import COMMENT_DEFAULT_FIELDS, STEP_PARAM_SCHEMA


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
    assert values['assemble.subtitle_outline_color'] == '#000000'
    fresh = make_project()
    new_values = api.get(f'/api/projects/{fresh}/step_params').json()['steps']['assemble']['values']
    assert new_values['assemble.subtitle_color'] == '#FFD65A'
    assert new_values['assemble.subtitle_outline_color'] == '#30213A'


def test_comment_and_workflow_use_same_inheritance_without_editing_content(make_project):
    from test_configuration import save
    from dubforge import configuration
    first, second = make_project(), make_project()
    save('project:'+first, {'comments.color':'#FFFFFF'})
    cs._write(crud.get_project(first), {'overlay': {'settings': {'start': 8}, 'items': []}})
    snapshot = (crud.project_dir(first) / 'comment_studio.json').read_bytes()
    save('global', {'comments.color':'#FFD65A', 'comments.background_opacity':0})
    assert cs.view(second)['defaults']['color'] == '#FFFFFF'
    assert cs.view(first)['overlay']['settings']['background_opacity'] == 50
    plan = configuration.binding_plan([first, second], '')
    configuration.bind(dict(uids=[first, second], template_id='', fingerprint=plan['fingerprint'], confirmed=True))
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


def test_settings_expose_parameter_help_without_generation_profiles(isolated_env):
    before = copy.deepcopy(config.load_config())
    api = TestClient(app)
    for path in ('/api/settings/step_params', '/api/configuration/global'):
        response = api.get(path)
        assert response.status_code == 200, response.text
        data = response.json()
        assert 'indextts_profiles' not in data
        section = data.get('steps', data.get('sections'))['tts']
        assert all(field['help'] for field in section['fields'] if field['path'].startswith('tts.indextts25.'))
    assert config.load_config() == before
