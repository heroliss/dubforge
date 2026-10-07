"""语义软目标与本地字符断句配置互不替换；配置、数据库和API均隔离。"""
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.main import app
from dubforge import config, configuration
from dubforge.errors import DubForgeError


def _save(key, updates=None, reset=None):
    current = configuration.resolve(key=key)
    return configuration.save(key, dict(revision=current['revision'], fingerprint=current['fingerprint'],
                                        updates=updates or {}, reset=reset or []))


def test_semantic_defaults_and_example_have_same_soft_targets(isolated_env):
    settings = config.load_config()
    assert settings['split']['target_word_count'] == 10
    assert settings['split']['max_segment_seconds'] == 7.0
    assert settings['split']['max_split_length'] == 22
    example_path = Path(__file__).resolve().parents[2] / 'config.example.yaml'
    example = yaml.safe_load(example_path.read_text(encoding='utf-8'))
    for key in ['target_word_count', 'max_segment_seconds', 'max_split_length']:
        assert example['split'][key] == config.DEFAULTS['split'][key]


@pytest.mark.parametrize('use_llm', [True, False])
def test_legacy_character_and_duration_values_are_not_converted(make_project, use_llm):
    uid = make_project()
    config.update_config({'split.use_llm': use_llm, 'split.max_split_length': 20,
                          'split.max_segment_seconds': 8.0})
    before = config.config_path().read_bytes()
    values = configuration.resolve(uid)['values']
    assert values['split.use_llm'] is use_llm
    assert values['split.max_split_length'] == 20
    assert values['split.max_segment_seconds'] == 8.0
    assert values['split.target_word_count'] == 10
    assert config.config_path().read_bytes() == before, '读取默认值不能暗中迁移配置文件'
    assert 'target_word_count' not in yaml.safe_load(before)['split']


def test_project_api_exposes_distinct_word_and_character_fields(make_project):
    uid = make_project()
    response = TestClient(app).get(f'/api/projects/{uid}/step_params')
    assert response.status_code == 200
    split = response.json()['steps']['split']
    fields = {field['path']: field for field in split['fields']}
    words, characters = fields['split.target_word_count'], fields['split.max_split_length']
    assert words['label'] == '语义目标词数（约）'
    assert words['depends_on'] == [['split.use_llm', True]]
    assert characters['depends_on'] == [['split.use_llm', False]]
    assert characters['label'] == '本地单句长度（字符）'
    assert '自然语义词' in words['help'] and '机械拆分' in words['help']
    assert '仅关闭 LLM' in characters['help']
    assert '软参考' in fields['split.max_segment_seconds']['help']
    assert split['values']['split.target_word_count'] == 10
    assert split['values']['split.max_split_length'] == 22
    assert split['values']['split.max_segment_seconds'] == 7.0
    view = configuration.schema()['split']
    assert {field['path']: field['depends_on'] for field in view['fields']}['split.target_word_count'] == words['depends_on']


def test_word_target_uses_existing_template_inheritance_and_sparse_project_overrides(make_project):
    uid = make_project()
    template = configuration.create_template('语义节奏')
    _save(template['key'], {'split.target_word_count': 12, 'split.max_split_length': 20})
    plan = configuration.binding_plan([uid], template['key'][7:])
    configuration.bind(dict(uids=[uid], template_id=template['key'][7:], fingerprint=plan['fingerprint'], confirmed=True))
    inherited = configuration.resolve(uid)
    assert inherited['values']['split.target_word_count'] == 12
    assert inherited['sources']['split.target_word_count']['kind'] == 'series'
    _save('project:' + uid, {'split.target_word_count': 10})
    own = configuration.resolve(uid)
    assert own['values']['split.target_word_count'] == 10
    assert own['values']['split.max_split_length'] == 20
    assert own['overrides'] == {'split.target_word_count': 10}
    _save('project:' + uid, reset=['split.target_word_count'])
    assert configuration.resolve(uid)['values']['split.target_word_count'] == 12


@pytest.mark.parametrize('value', [0, 81, True, 2.5, '10', None])
def test_word_target_rejects_invalid_types_and_ranges_without_saving(make_project, value):
    uid = make_project()
    before = configuration.resolve(uid)
    with pytest.raises(DubForgeError):
        _save('project:' + uid, {'split.target_word_count': value})
    assert configuration.resolve(uid) == before


@pytest.mark.parametrize('value', [1, 10, 80])
def test_word_target_accepts_integer_soft_targets(make_project, value):
    uid = make_project()
    result = _save('project:' + uid, {'split.target_word_count': value})
    assert result['values']['split.target_word_count'] == value
    assert result['values']['split.max_split_length'] == 22
