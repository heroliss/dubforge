"""批量故障分类的独立边界回归；不发起真实网络或执行项目。"""
import json

import httpx
import pytest

from dubforge.backends import llm
from dubforge.comment_recovery import Recovery, item_error_codes
from dubforge.errors import StepFailedError


@pytest.mark.parametrize('body', [
    {'error': {'code': 'rate_limit_exceeded', 'message': 'Requests per minute exceeded'}},
    {'error': {'code': 'quota_exceeded', 'message': 'Per-minute quota exceeded; try again later'}},
    {'error': {'message': '请求过于频繁，请稍后重试'}},
])
def test_temporary_429_is_not_account_exhaustion(isolated_env, monkeypatch, body):
    monkeypatch.setattr(llm, '_rate_limit_until', {})
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(429, json=body)

    monkeypatch.setattr(llm, '_client', lambda base: httpx.Client(
        base_url=base, transport=httpx.MockTransport(handle)))
    profile = {'name': '独立限流边界', 'base_url': 'http://fixture.invalid', 'model': 'fixture'}
    with pytest.raises(StepFailedError) as raised:
        llm.chat('边界测试', profile=profile)
    assert raised.value.code == 'LLM_RATE_LIMITED'
    assert len(calls) == 1
    assert '余额不足' not in raised.value.message


@pytest.mark.parametrize('payload', [
    {'choices': [{'message': {'content': '余额不足、InvalidSubscription 是这段视频讨论的主题。'}}]},
    {'output': '余额不足', 'usage': {'code': 1113}},
    {'code': 200, 'message': 'InvalidSubscription'},
    {'code': 0, 'message': '余额不足'},
    {'Response': {'Result': {'Code': 'InvalidSubscription'}}},
])
def test_account_words_in_success_content_are_not_provider_errors(payload):
    assert llm._provider_error(payload) is None


@pytest.mark.parametrize('code', ['LLM_QUOTA_EXHAUSTED', 'LLM_SUBSCRIPTION_INVALID'])
@pytest.mark.parametrize('wrapped', [False, True])
def test_account_errors_never_become_skippable_comment_rejections(code, wrapped):
    if wrapped:
        error = StepFailedError('多个模型服务失败', code='LLM_PROVIDERS_FAILED', detail={
            'providers': [{'code': 'LLM_CONTENT_REJECTED'}, {'code': code}],
        })
    else:
        error = StepFailedError('账户资源需要处理', code=code)
    assert item_error_codes(error) == set()
    completed = []
    calls = []

    def execute(group, *, isolated):
        calls.append((list(group), isolated))
        raise error

    recovery = Recovery(policy='continue', check=lambda: None, remaining=lambda group: group,
        execute=execute, failed=lambda *args: completed.append(args), trace=lambda *args: None)
    with pytest.raises(StepFailedError) as raised:
        recovery.process(['评论甲', '评论乙'])
    assert raised.value is error
    assert calls == [(['评论甲', '评论乙'], False)]
    assert completed == []


@pytest.mark.parametrize('payload, expected', [
    ({'error': {'code': 1113, 'message': '余额不足或无可用资源包,请充值。'}}, 'LLM_QUOTA_EXHAUSTED'),
    ({'ResponseMetadata': {'Error': {'Code': 'InvalidSubscription', 'Message': 'Plan is not active'}}},
        'LLM_SUBSCRIPTION_INVALID'),
])
def test_known_provider_errors_survive_json_envelope_without_losing_detail(payload, expected):
    error = llm._body_error(json.dumps(payload), 429)
    assert error is not None and error.code == expected
    assert isinstance(error.detail, dict)
    assert error.suggestion and '模型与服务' in error.suggestion
