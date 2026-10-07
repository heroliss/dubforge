"""表单思考参数摘要与真实请求一致，且不泄漏任意专属参数。"""
from copy import deepcopy
import json

from dubforge.pipeline.text_llm import prepare_profiles, thinking_profiles
from dubforge.pipeline.step_params import schema_view


def profile(**changes):
    result = dict(name='主模型', enabled=True, model='ark-code-latest',
                base_url='https://ark.cn-beijing.volces.com/api/plan/v3',
                api_key='fixture-private-key', params={'reasoning_effort': 'high',
                    'thinking': {'type': 'enabled', 'secret': 'fixture-private-param'}})
    result.update(changes)
    return result


def test_summary_reports_effective_support_without_mutating_or_exposing_secrets():
    original = profile()
    frozen = deepcopy(original)
    summary = thinking_profiles([original])[0]
    assert summary == dict(name='主模型', enabled=True, supports_low=True, params_valid=True,
                           effort='high', has_thinking=True, thinking_type='enabled')
    assert original == frozen
    assert 'fixture-private' not in json.dumps(summary)
    assert 'base_url' not in summary and 'params' not in summary


def test_custom_model_override_is_used_for_support_detection():
    original = profile(params={'model': 'other-model', 'reasoning_effort': 'high'})
    assert thinking_profiles([original])[0]['supports_low'] is False
    assert prepare_profiles([original])[0] == original
    original = profile(model='other-model', params={'model': 'ark-code-latest', 'reasoning_effort': 'high'})
    assert thinking_profiles([original])[0]['supports_low'] is True
    assert prepare_profiles([original])[0]['params']['reasoning_effort'] == 'low'


def test_custom_values_and_invalid_params_are_redacted_in_summary():
    custom = profile(params={'reasoning_effort': 'fixture-private-value',
                             'thinking': {'type': ['fixture-private-value']}})
    summary = thinking_profiles([custom, profile(params='fixture-private-string')])
    assert summary[0]['effort'] == '自定义值' and summary[0]['thinking_type'] == '自定义值'
    assert not summary[1]['params_valid']
    assert 'fixture-private' not in json.dumps(summary)


def test_both_step_schemas_include_safe_preview_for_all_profiles(isolated_env):
    profiles = [profile(), profile(name='备用', enabled=False, base_url='http://fixture.invalid')]
    schema = schema_view({}, caps={'asr': {'langs': []}, 'tts': {'langs': []}},
                         settings={'llm': {'profiles': profiles}})
    for step in ('split', 'translate'):
        field = next(f for f in schema[step]['fields'] if f['path'] == step + '.thinking_mode')
        assert field['thinking_profiles'] == thinking_profiles(profiles)
        assert '适配模型生效' in field['options'][0]['label']
        assert '自定义 JSON' in field['options'][1]['label']
    assert 'fixture-private' not in json.dumps(schema)


def test_legacy_preview_matches_legacy_request_snapshot(isolated_env):
    # 旧单组配置的请求入口只取基础字段，不发送旧 params，预览必须一致。
    legacy = dict(base_url='http://fixture.invalid', model='legacy', api_key='fixture-key',
                  params={'reasoning_effort': 'high'})
    schema = schema_view({}, caps={'asr': {'langs': []}, 'tts': {'langs': []}}, settings={'llm': legacy})
    field = next(f for f in schema['split']['fields'] if f['path'] == 'split.thinking_mode')
    assert field['thinking_profiles'][0]['effort'] == '服务商默认'
