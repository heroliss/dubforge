"""语义整理和翻译的请求级思考参数：配置副本、供应商边界和错误诊断。"""
from copy import deepcopy
from inspect import signature

import pytest

from dubforge.errors import StepFailedError
from dubforge.pipeline.text_llm import prepare_profiles


@pytest.fixture(autouse=True)
def isolated_source_environment(isolated_env):
    """测试只使用内存配置，沿用仓库的数据隔离约定。"""


def profile(path='/api/plan/v3', **overrides):
    return dict(name='模型组', base_url='https://ark.cn-beijing.volces.com' + path,
                api_key='fixture-key', model='ark-code-latest', enabled=True,
                params={'reasoning_effort': 'high', 'temperature': .2,
                        'thinking': {'type': 'enabled', 'budget_tokens': 10000}}, **overrides)


def glm_profile(**overrides):
    return dict(name='GLM', base_url='https://open.bigmodel.cn/api/coding/paas/v4',
                api_key='fixture-key', model='glm-5.3-flash', enabled=True,
                params={'reasoning_effort': 'high',
                        'thinking': {'type': 'enabled', 'clear_thinking': False}}, **overrides)


@pytest.mark.parametrize('path', ['/api/plan/v3', '/api/plan/v3/', '/api/coding/v3', '/api/coding/v3/'])
def test_default_low_mode_lowers_effort_for_verified_ark_plan_endpoints(path):
    original = profile(path)
    frozen = deepcopy(original)
    logs = []
    result = prepare_profiles([original], log=logs.append)
    assert result[0]['params'] == {'temperature': .2, 'reasoning_effort': 'low'}
    assert original == frozen
    assert result[0]['api_key'] == original['api_key']
    assert '降低思考强度' in logs[0] and '关闭' not in logs[0]


def test_low_snapshot_does_not_mutate_nested_globals_or_other_callers():
    original = profile()
    original['params']['nested'] = {'labels': ['固定']}
    original['extra'] = {'flags': ['全局']}
    frozen = deepcopy(original)
    result = prepare_profiles([original])
    result[0]['params']['nested']['labels'].append('本地')
    result[0]['extra']['flags'].append('本地')
    result[0]['params']['reasoning_effort'] = 'high'
    assert original == frozen
    assert prepare_profiles([original])[0]['params']['reasoning_effort'] == 'low'
    assert 'thinking' not in prepare_profiles([original])[0]['params']


def test_follow_profile_preserves_all_values_without_logging_a_parameter_override():
    original = profile()
    logs = []
    result = prepare_profiles([original], 'follow_profile', log=logs.append)
    assert result == [original]
    assert result[0] is not original
    assert result[0]['params'] is not original['params']
    assert logs == []


@pytest.mark.parametrize('endpoint,model', [
    ('https://ark.cn-beijing.volces.com/api/v3', 'ark-code-latest'),
    ('https://ark.cn-beijing.volces.com/api/plan/v3', 'glm-4.7'),
    ('https://ark.cn-beijing.volces.com/api/plan/v3', 'ARK-CODE-LATEST'),
    ('https://ark.cn-beijing.volces.com/api/plan/v3/chat/completions', 'ark-code-latest'),
    ('https://ark.cn-beijing.volces.com/API/PLAN/V3', 'ark-code-latest'),
    ('https://ark.cn-beijing.volces.com:8443/api/plan/v3', 'ark-code-latest'),
    ('http://ark.cn-beijing.volces.com/api/plan/v3', 'ark-code-latest'),
    ('https://proxy.example/api/plan/v3', 'ark-code-latest'),
    ('https://ark.cn-beijing.volces.com.example/api/plan/v3', 'ark-code-latest'),
    ('https://ark.cn-beijing.volces.com@proxy.example/api/plan/v3', 'ark-code-latest'),
    ('https://proxy.example/?next=https://ark.cn-beijing.volces.com/api/plan/v3', 'ark-code-latest'),
    ('https://[invalid/api/plan/v3', 'ark-code-latest'),
])
def test_unverified_provider_model_and_route_keep_original_params_with_soft_hint(endpoint, model):
    original = profile()
    original.update(base_url=endpoint, model=model)
    logs = []
    result = prepare_profiles([original], log=logs.append)
    assert result == [original]
    assert result[0]['params'] is not original['params']
    assert len(logs) == 1
    assert '未确认支持' in logs[0] and '仅由提示词' in logs[0]
    assert original['api_key'] not in logs[0]
    assert endpoint not in logs[0]


def test_fallback_order_and_disabled_groups_are_not_filtered_by_snapshot_preparation():
    first = profile('/api/plan/v3')
    second = profile('/api/coding/v3')
    second.update(name='备用组', enabled=False)
    third = dict(name='其他组', base_url='https://example.test/v1', model='unknown',
                 params={'vendor_options': {'enabled': True}})
    result = prepare_profiles([first, second, third])
    assert [item['name'] for item in result] == ['模型组', '备用组', '其他组']
    assert result[1]['enabled'] is False
    assert result[2] == third
    assert result[2]['params'] is not third['params']


def test_missing_params_are_added_only_to_supported_step_copy():
    original = profile()
    del original['params']
    result = prepare_profiles([original])
    assert result[0]['params'] == {'reasoning_effort': 'low'}
    assert 'params' not in original
    assert 'params' not in prepare_profiles([original], 'follow_profile')[0]


@pytest.mark.parametrize('mode', ['invalid', '', None, 0, {}, []])
def test_invalid_thinking_mode_has_stable_typed_error(mode):
    with pytest.raises(StepFailedError) as error:
        prepare_profiles([profile()], mode)
    assert error.value.code == 'SPLIT_THINKING_MODE'
    assert '低思考强度（low）' in str(error.value)


@pytest.mark.parametrize('params', [None, 'reasoning_effort=high', [], 1, False])
@pytest.mark.parametrize('mode', ['low', 'follow_profile'])
def test_non_object_params_are_not_silently_dropped(params, mode):
    original = profile()
    original['params'] = params
    with pytest.raises(StepFailedError) as error:
        prepare_profiles([original], mode)
    assert error.value.code == 'SPLIT_LLM_PARAMS_INVALID'
    assert str(error.value).startswith('第1个语义整理模型的专属参数必须是对象')


def test_malformed_later_group_does_not_emit_partial_success_logs():
    valid = profile()
    invalid = profile()
    invalid['params'] = 'fixture-secret-marker'
    logs = []
    frozen = deepcopy(valid)
    with pytest.raises(StepFailedError) as error:
        prepare_profiles([valid, invalid], log=logs.append)
    assert error.value.code == 'SPLIT_LLM_PARAMS_INVALID'
    assert 'fixture-secret-marker' not in str(error.value)
    assert valid == frozen and logs == []


@pytest.mark.parametrize('profiles', [None, {}, [None], [1], (profile(),)])
def test_malformed_profiles_have_a_stable_typed_error(profiles):
    with pytest.raises(StepFailedError) as error:
        prepare_profiles(profiles)
    assert error.value.code == 'SPLIT_LLM_PROFILE_INVALID'


def test_empty_profile_chain_remains_empty():
    assert prepare_profiles([]) == []


@pytest.mark.parametrize('thinking', [{'type': 'enabled'}, {'type': 'disabled'}, {'type': 'auto'}])
def test_low_chat_request_removes_any_conflicting_thinking_mode_from_step_copy(thinking):
    original = profile()
    original['params']['thinking'] = thinking
    frozen = deepcopy(original)
    prepared = prepare_profiles([original])
    assert prepared[0]['params']['reasoning_effort'] == 'low'
    assert 'thinking' not in prepared[0]['params']
    assert original == frozen


@pytest.mark.parametrize('path', ['/api/plan/v3', '/api/coding/v3/'])
def test_translate_default_low_mode_uses_low_effort_without_changing_shared_profiles(path):
    original = profile(path)
    original['params']['nested'] = {'labels': ['固定']}
    frozen = deepcopy(original)
    logs = []
    prepared = prepare_profiles([original], task='translate', log=logs.append)
    assert prepared[0]['params']['reasoning_effort'] == 'low'
    assert 'thinking' not in prepared[0]['params']
    prepared[0]['params']['nested']['labels'].append('翻译本地')
    assert original == frozen
    assert logs[0].startswith('翻译模型1：')
    assert '降低思考强度' in logs[0] and '关闭' not in logs[0]
    assert prepare_profiles([original])[0]['params']['reasoning_effort'] == 'low'


def test_translate_follow_profile_keeps_parameters_and_fallback_order():
    originals = [profile(), profile('/api/coding/v3')]
    originals[1].update(name='备用翻译', enabled=False)
    logs = []
    prepared = prepare_profiles(originals, 'follow_profile', task='translate', log=logs.append)
    assert prepared == originals
    assert prepared[0]['params'] is not originals[0]['params']
    assert prepared[1]['enabled'] is False
    assert logs == []


def test_translate_unknown_provider_gets_only_soft_prompt_log():
    original = profile()
    original['base_url'] = 'https://unknown.example/v1'
    logs = []
    prepared = prepare_profiles([original], task='translate', log=logs.append)
    assert prepared == [original]
    assert prepared[0]['params'] is not original['params']
    assert logs[0].startswith('翻译模型1：')
    assert '仅由提示词' in logs[0]
    assert original['api_key'] not in logs[0]
    assert original['base_url'] not in logs[0]


@pytest.mark.parametrize('task,label,prefix', [
    ('split', '语义整理', 'SPLIT'), ('translate', '翻译', 'TRANSLATE'),
])
@pytest.mark.parametrize('case,suffix', [
    ('mode', 'THINKING_MODE'), ('profiles', 'LLM_PROFILE_INVALID'),
    ('params', 'LLM_PARAMS_INVALID'),
])
def test_text_task_maps_typed_errors_to_its_own_step(task, label, prefix, case, suffix):
    originals = [profile()]
    mode = 'low'
    if case == 'mode':
        mode = 'invalid'
    elif case == 'profiles':
        originals = [None]
    else:
        originals[0]['params'] = 'fixture-secret-marker'
    logs = []
    with pytest.raises(StepFailedError) as error:
        prepare_profiles(originals, mode, task=task, log=logs.append)
    assert error.value.code == f'{prefix}_{suffix}'
    assert label in str(error.value)
    assert 'fixture-secret-marker' not in str(error.value)
    assert logs == []


@pytest.mark.parametrize('task', ['invalid', '', None, 0, {}, [], 'fixture-secret-marker'])
def test_invalid_text_task_is_rejected_before_profile_or_mode_processing(task):
    with pytest.raises(StepFailedError) as error:
        prepare_profiles(None, 'invalid', task=task)
    assert error.value.code == 'TEXT_LLM_TASK_INVALID'
    assert 'fixture-secret-marker' not in str(error.value)


@pytest.mark.parametrize('task', ['split', 'translate'])
def test_legacy_fast_alias_matches_canonical_low_and_default(task):
    originals = [profile(), dict(model='unknown', base_url='https://unknown.example/v1',
                                params={'vendor_options': {'enabled': True}})]
    frozen = deepcopy(originals)
    logs = {'default': [], 'low': [], 'fast': []}
    default = prepare_profiles(originals, task=task, log=logs['default'].append)
    low = prepare_profiles(originals, 'low', task=task, log=logs['low'].append)
    legacy = prepare_profiles(originals, 'fast', task=task, log=logs['fast'].append)
    assert signature(prepare_profiles).parameters['mode'].default == 'low'
    assert default == low == legacy
    assert logs['default'] == logs['low'] == logs['fast']
    assert default[0]['params']['reasoning_effort'] == 'low'
    assert 'thinking' not in default[0]['params']
    assert default[1] == originals[1]
    assert originals == frozen


@pytest.mark.parametrize('model', ['glm-5.3', 'glm-5.3-flash', 'glm-5.3-flashx', 'GLM-5.3-Flash'])
@pytest.mark.parametrize('task', ['split', 'translate'])
def test_verified_glm_5_3_low_mode_preserves_enabled_thinking_nested_fields(model, task):
    original = dict(model=model, base_url='https://open.bigmodel.cn/api/paas/v4/',
                    params={'reasoning_effort': 'max', 'thinking': {'type': 'enabled', 'clear_thinking': False},
                            'temperature': 1, 'top_p': .95})
    frozen = deepcopy(original)
    result = prepare_profiles([original], task=task)
    assert result[0]['params']['reasoning_effort'] == 'low'
    assert result[0]['params']['thinking'] == frozen['params']['thinking']
    result[0]['params']['thinking']['clear_thinking'] = True
    assert original == frozen
    assert prepare_profiles([original], 'follow_profile', task=task) == [original]


@pytest.mark.parametrize('endpoint,model', [
    ('http://open.bigmodel.cn/api/paas/v4', 'glm-5.3'),
    ('https://open.bigmodel.cn:8443/api/paas/v4', 'glm-5.3'),
    ('https://open.bigmodel.cn/api/paas/v4/chat/completions', 'glm-5.3-flash'),
    ('https://open.bigmodel.cn/api/paas/v4?token=fixture', 'glm-5.3-flash'),
    ('https://open.bigmodel.cn/api/paas/v4', 'glm-4.7'),
    ('https://open.bigmodel.cn/api/paas/v4', 'glm-5.2'),
    ('https://proxy.example/api/paas/v4', 'glm-5.3-flash'),
])
def test_glm_low_mode_does_not_guess_unverified_model_or_route(endpoint, model):
    original = dict(model=model, base_url=endpoint, params={'reasoning_effort': 'max'})
    assert prepare_profiles([original]) == [original]
