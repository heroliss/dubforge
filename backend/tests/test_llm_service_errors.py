"""明确的账户资源错误应与临时限流分开，不访问真实模型服务。"""
import json

import httpx
import pytest

from dubforge.backends import llm
from dubforge.comment_recovery import Recovery, item_error_codes
from dubforge.errors import StepFailedError


PROFILE = dict(name='账户测试', base_url='http://account.fixture.invalid', model='fixture',
               params={'thinking': {'type': 'enabled'}})
QUOTA = {'code': '1113', 'message': '余额不足或无可用资源包,请充值。'}
SUBSCRIPTION = {'code': 'InvalidSubscription', 'message':
    'Your account does not have a valid CodingPlan subscription, or your subscription has expired.'}


@pytest.fixture(autouse=True)
def isolated_cooldown(monkeypatch):
    monkeypatch.setattr(llm, '_rate_limit_until', {})


def bind_transport(monkeypatch, response_for):
    requests = []

    def handle(request):
        requests.append((request.url.host, json.loads(request.content)))
        return response_for(len(requests), request)

    monkeypatch.setattr(llm, '_client', lambda base: httpx.Client(
        base_url=base, transport=httpx.MockTransport(handle)))
    return requests


@pytest.mark.parametrize('status', [200, 400, 429, 503])
def test_explicit_balance_error_fails_once_without_rate_limit_cooldown(isolated_env, monkeypatch, status):
    requests = bind_transport(monkeypatch, lambda *_: httpx.Response(status, json={'error': QUOTA}))
    traces = []
    monkeypatch.setattr(llm, '_sleep_retry', lambda *_: pytest.fail('余额不足不能反复退避重试'))
    with pytest.raises(StepFailedError) as caught:
        llm.chat_validated('评论', profile=PROFILE,
            parser=lambda _: pytest.fail('账户错误不能进入格式纠错'),
            trace=lambda event, content='', **meta: traces.append((event, meta)))
    error = caught.value
    assert error.code == 'LLM_QUOTA_EXHAUSTED'
    assert '余额不足' in error.message and '限流' not in error.message
    assert '模型与服务' in error.suggestion
    assert len(requests) == 1 and llm._rate_limit_remaining(PROFILE) == 0
    assert error.detail['providers'][0]['detail'] == QUOTA
    assert any(event == 'provider_error' and meta['detail'] == QUOTA for event, meta in traces)


@pytest.mark.parametrize('envelope', [
    {'code': '1113', 'msg': '余额不足'},
    {'Code': 'InvalidSubscription', 'Message': 'Subscription is invalid'},
    {'Error': {'Code': 'InvalidSubscription', 'Message': 'Subscription is invalid'}},
    {'ResponseMetadata': {'Error': {'Code': 'InvalidSubscription', 'Message': 'Subscription is invalid'},
                          'RequestId': 'fixture-request'}},
    {'Response': {'Error': {'Code': 'InvalidSubscription', 'Message': 'Subscription is invalid'}}},
])
def test_provider_error_envelopes_preserve_original_cause(isolated_env, monkeypatch, envelope):
    requests = bind_transport(monkeypatch, lambda *_: httpx.Response(200, json=envelope))
    with pytest.raises(StepFailedError) as caught:
        llm.chat('评论', profile=PROFILE)
    error = caught.value
    expected = 'LLM_QUOTA_EXHAUSTED' if 'msg' in envelope else 'LLM_SUBSCRIPTION_INVALID'
    assert error.code == expected and len(requests) == 1
    provider = error.detail['providers'][0]
    assert provider['detail'] in [envelope, envelope.get('Error'),
        envelope.get('ResponseMetadata', {}).get('Error'), envelope.get('Response', {}).get('Error')]


@pytest.mark.parametrize('detail', [
    {'type': 'insufficient_quota', 'message': 'Please check your plan and billing details.'},
    {'code': 'service_error', 'message': 'Insufficient balance'},
    {'code': 'service_error', 'message': 'Credits are exhausted'},
])
def test_explicit_resource_exhaustion_is_recognized(isolated_env, monkeypatch, detail):
    bind_transport(monkeypatch, lambda *_: httpx.Response(429, json={'error': detail}))
    with pytest.raises(StepFailedError) as caught:
        llm.chat('评论', profile=PROFILE)
    assert caught.value.code == 'LLM_QUOTA_EXHAUSTED'
    assert llm._rate_limit_remaining(PROFILE) == 0


@pytest.mark.parametrize('detail', [
    {'code': 'rate_limit_exceeded', 'message': 'Requests per minute quota exceeded'},
    {'message': 'Quota exceeded. Please retry after 10 seconds.'},
    'rate limit',
])
def test_temporary_429_keeps_existing_fallback_cooldown(isolated_env, monkeypatch, detail):
    requests = bind_transport(monkeypatch, lambda *_: httpx.Response(429, json={'error': detail}))
    for _ in range(2):
        with pytest.raises(StepFailedError) as caught:
            llm.chat('评论', profile=PROFILE)
        assert caught.value.code == 'LLM_RATE_LIMITED'
    assert len(requests) == 1 and llm._rate_limit_remaining(PROFILE) > 0


def test_subscription_error_is_readable_and_not_treated_as_429(isolated_env, monkeypatch):
    requests = bind_transport(monkeypatch, lambda *_: httpx.Response(429, json={'error': SUBSCRIPTION}))
    with pytest.raises(StepFailedError) as caught:
        llm.chat('评论', profile=PROFILE)
    assert caught.value.code == 'LLM_SUBSCRIPTION_INVALID'
    assert '订阅未开通或已失效' in caught.value.message
    assert 'CodingPlan' not in caught.value.message
    assert caught.value.detail['providers'][0]['detail'] == SUBSCRIPTION
    assert len(requests) == 1 and llm._rate_limit_remaining(PROFILE) == 0


def test_service_error_sse_envelope_uses_account_classification(isolated_env, monkeypatch):
    event = 'data: ' + json.dumps({'error': SUBSCRIPTION}) + '\n\n'
    requests = bind_transport(monkeypatch, lambda *_: httpx.Response(200,
        headers={'content-type': 'text/event-stream'}, text=event))
    with pytest.raises(StepFailedError) as caught:
        llm.chat('评论', profile=PROFILE)
    assert caught.value.code == 'LLM_SUBSCRIPTION_INVALID' and len(requests) == 1


def test_account_error_still_falls_back_to_next_provider(isolated_env, monkeypatch):
    requests = bind_transport(monkeypatch, lambda count, _: httpx.Response(429, json={'error': QUOTA})
        if count == 1 else httpx.Response(200, json={'choices': [{'message': {'content': '完成'}}]}))
    fallback = {**PROFILE, 'name': '可用配置', 'base_url': 'http://working.fixture.invalid'}
    assert llm.chat('评论', profile=[PROFILE, fallback]) == '完成'
    assert len(requests) == 2 and not llm._rate_limit_until


def test_failed_chain_retains_distinct_service_causes_without_format_retries(isolated_env, monkeypatch):
    responses = [(429, {'error': QUOTA}), (403, {'error': SUBSCRIPTION}),
                 (429, {'error': {'code': 'rate_limit_exceeded', 'message': 'Too many requests'}})]
    requests = bind_transport(monkeypatch, lambda count, _: httpx.Response(
        responses[count - 1][0], json=responses[count - 1][1]))
    profiles = [{**PROFILE, 'name': f'配置{index}', 'base_url': f'http://provider{index}.fixture.invalid'}
                for index in range(3)]
    with pytest.raises(StepFailedError) as caught:
        llm.chat_validated('评论', profile=profiles, parser=lambda _: pytest.fail('不能格式纠错'))
    error = caught.value
    assert error.code == 'LLM_PROVIDERS_FAILED' and len(requests) == 3
    assert [provider['code'] for provider in error.detail['providers']] == [
        'LLM_QUOTA_EXHAUSTED', 'LLM_SUBSCRIPTION_INVALID', 'LLM_RATE_LIMITED']
    assert error.detail['providers'][0]['detail'] == QUOTA
    assert error.detail['providers'][1]['detail'] == SUBSCRIPTION
    assert not item_error_codes(error)
    assert [llm._rate_limit_remaining(profile) > 0 for profile in profiles] == [False, False, True]


@pytest.mark.parametrize('code', ['LLM_QUOTA_EXHAUSTED', 'LLM_SUBSCRIPTION_INVALID'])
def test_comment_recovery_cannot_skip_account_failures_as_bad_items(isolated_env, code):
    error = StepFailedError('模型账户需要处理', code=code)
    calls, skipped = [], []

    def execute(group, **_):
        calls.append(group)
        raise error

    recovery = Recovery(policy='continue', check=lambda: None, remaining=lambda group: group,
        execute=execute, failed=lambda *args: skipped.append(args), trace=lambda *args: None)
    with pytest.raises(StepFailedError) as caught:
        recovery.process([1, 2, 3, 4])
    assert caught.value is error and calls == [[1, 2, 3, 4]]
    assert not skipped and recovery.extra == 0


@pytest.mark.parametrize('data', [
    {'code': 200, 'message': 'OK', 'choices': [{'message': {'content': '完成'}}]},
    {'code': 123, 'choices': [{'message': {'content': '余额不足与订阅的说明'}}]},
    {'code': 0, 'message': 'success'},
    {'choices': [{'message': {'content': json.dumps({'error': QUOTA})}}]},
])
def test_success_or_output_content_is_not_scanned_for_account_errors(isolated_env, data):
    assert llm._provider_error(data) is None


def test_content_rejection_keeps_existing_category(isolated_env):
    error = llm._provider_error({'error': {'code': 'SensitiveContentDetected', 'message': '敏感内容'}})
    assert error.code == 'LLM_CONTENT_REJECTED'


@pytest.mark.parametrize('message', ['Content Exists Risk', 'Content Exists Risk (request_id: fixture-request)',
                                   'content exists risk: input rejected'])
def test_explicit_content_exists_risk_is_business_rejection_without_format_or_parameter_retry(isolated_env, monkeypatch, message):
    detail = {'type':'invalid_request_error', 'code':'invalid_request_error', 'message':message}
    requests = bind_transport(monkeypatch, lambda *_: httpx.Response(400, json={'error':detail}))
    monkeypatch.setattr(llm, '_sleep_retry', lambda *_: pytest.fail('明确审核拒绝不能原批次退避重发'))
    with pytest.raises(StepFailedError) as caught:
        llm.chat_validated('保持原批评论', profile=PROFILE,
            parser=lambda _: pytest.fail('审核拒绝不能进入格式修复'))
    assert caught.value.code == 'LLM_CONTENT_REJECTED' and len(requests) == 1
    assert caught.value.detail['providers'][0]['detail'] == detail
    assert requests[0][1]['thinking'] == {'type':'enabled'}
    assert item_error_codes(caught.value) == {'LLM_CONTENT_REJECTED'}


def test_content_exists_risk_sse_envelope_uses_same_business_classification(isolated_env, monkeypatch):
    event = 'data: ' + json.dumps({'error':{'type':'invalid_request_error','message':'Content Exists Risk'}}) + '\n\n'
    requests = bind_transport(monkeypatch, lambda *_: httpx.Response(200,
        headers={'content-type':'text/event-stream'},text=event))
    with pytest.raises(StepFailedError) as caught:
        llm.chat('保持原批评论', profile=PROFILE)
    assert caught.value.code == 'LLM_CONTENT_REJECTED' and len(requests) == 1


@pytest.mark.parametrize('status,detail,expected', [
    (400, {'type':'invalid_request_error','message':'Invalid request'}, 'LLM_HTTP_400'),
    (400, {'type':'invalid_request_error','message':'Invalid JSON schema'}, 'LLM_HTTP_400'),
    (400, {'code':'unsupported_parameter','message':'Unsupported parameter: content_exists_risk'}, 'LLM_HTTP_400'),
    (400, {'type':'invalid_request_error','message':'Validation failed: Content Exists Risk is not a parameter'}, 'LLM_HTTP_400'),
    (401, {'code':'invalid_api_key','message':'Invalid authentication credentials'}, 'LLM_HTTP_401'),
    (429, QUOTA, 'LLM_QUOTA_EXHAUSTED'),
])
def test_content_risk_recognition_does_not_absorb_other_service_errors(isolated_env, status, detail, expected):
    error = llm._body_error(json.dumps({'error':detail}),status)
    assert error.code == expected
    assert not item_error_codes(error)


def test_content_risk_with_auth_failure_in_provider_chain_still_stops_batch(isolated_env, monkeypatch):
    replies = [(400, {'type':'invalid_request_error','message':'Content Exists Risk'}),
               (401, {'code':'invalid_api_key','message':'Invalid authentication credentials'})]
    requests = bind_transport(monkeypatch, lambda count,_: httpx.Response(replies[count-1][0],json={'error':replies[count-1][1]}))
    with pytest.raises(StepFailedError) as caught:
        llm.chat_validated('保持原批评论',profile=[PROFILE,{**PROFILE,'name':'另一个配置'}],
            parser=lambda _: pytest.fail('服务错误不得进入格式修复'))
    assert caught.value.code == 'LLM_PROVIDERS_FAILED' and len(requests) == 2
    assert [row['code'] for row in caught.value.detail['providers']] == ['LLM_CONTENT_REJECTED','LLM_AUTH']
    assert not item_error_codes(caught.value)


def test_successful_model_text_is_not_scanned_for_content_risk(isolated_env):
    assert llm._provider_error({'choices':[{'message':{'content':'Content Exists Risk'}}]}) is None
