"""服务商拒绝、SSE 空回复与真正格式错误必须分层，不访问外网。"""
import json

import httpx
import pytest

from dubforge.backends import llm
from dubforge.errors import StepFailedError

PROFILE = dict(name='fixture', base_url='http://fixture.invalid', model='fixture',
               params={'thinking': {'type': 'enabled'}, 'model': 'override', 'top_p': 0.9})


def transport(monkeypatch, handler):
    calls = []
    def handle(request):
        calls.append(json.loads(request.content))
        return handler(len(calls), calls[-1])
    monkeypatch.setattr(llm, '_client', lambda base: httpx.Client(base_url=base, transport=httpx.MockTransport(handle)))
    return calls


def sse(*events):
    return httpx.Response(200, headers={'content-type': 'text/event-stream'},
        text='\n\n'.join('data: ' + json.dumps(event) for event in events) + '\n\ndata: [DONE]\n')


def test_content_rejection_is_not_unsupported_params_or_format_retry(isolated_env, monkeypatch):
    calls = transport(monkeypatch, lambda *_: httpx.Response(400, json={'error': {
        'code': 'SensitiveContentDetected', 'message': 'The input may contain sensitive information'}}))
    logs, traces = [], []
    with pytest.raises(StepFailedError) as error:
        llm.chat_validated('评论', parser=lambda raw: pytest.fail('服务错误不得进入解析器'), profile=PROFILE,
                           log=logs.append, trace=lambda event, content='', **kw: traces.append((event, kw)))
    assert error.value.code == 'LLM_CONTENT_REJECTED' and len(calls) == 1
    assert not any('参数被拒绝' in line or '剥离' in line for line in logs)
    assert error.value.detail['providers'][0]['detail']['code'] == 'SensitiveContentDetected'
    assert any(event == 'provider_error' and meta['detail']['code'] == 'SensitiveContentDetected' for event, meta in traces)


def test_only_explicitly_unsupported_optional_parameter_removed(isolated_env, monkeypatch):
    calls = transport(monkeypatch, lambda count, _: httpx.Response(400, json={'error': {
        'code': 'unsupported_parameter', 'message': 'Unsupported parameter: thinking'}}) if count == 1 else
        httpx.Response(200, json={'choices': [{'message': {'content': '成功'}, 'finish_reason': 'stop'}]}))
    assert llm.chat('评论', profile=PROFILE) == '成功'
    assert len(calls) == 2 and 'thinking' not in calls[1]
    assert calls[1]['model'] == 'override' and calls[1]['top_p'] == .9 and calls[1]['messages']


def test_generic_400_does_not_strip_params(isolated_env, monkeypatch):
    calls = transport(monkeypatch, lambda *_: httpx.Response(400, json={'error': {'message': 'Invalid request'}}))
    with pytest.raises(StepFailedError) as error:
        llm.chat('评论', profile=PROFILE)
    assert error.value.code == 'LLM_HTTP_400' and len(calls) == 1


def test_stream_unsupported_switches_to_non_stream_once(isolated_env, monkeypatch):
    calls = transport(monkeypatch, lambda count, _: httpx.Response(400, json={'error': {'message': 'stream is not supported'}})
        if count == 1 else httpx.Response(200, json={'choices': [{'message': {'content': '完成'}}]}))
    assert llm.chat('评论', profile={**PROFILE, 'params': {}}) == '完成'
    assert len(calls) == 2 and calls[0]['stream'] and 'stream' not in calls[1]


@pytest.mark.parametrize('finish,content,reasoning,code', [
    ('stop', '', '', 'LLM_EMPTY_RESPONSE'),
    ('stop', None, '只有思考', 'LLM_EMPTY_RESPONSE'),
    ('length', '', '预算耗尽', 'LLM_OUTPUT_TRUNCATED'),
    ('length', '只有部分正文', '', 'LLM_OUTPUT_TRUNCATED'),
    ('content_filter', '', '', 'LLM_CONTENT_REJECTED'),
    ('sensitive', '', '', 'LLM_CONTENT_REJECTED'),
])
@pytest.mark.parametrize('streaming', [True, False])
def test_empty_refused_and_truncated_answers_never_enter_parser(isolated_env, monkeypatch, finish, content, reasoning, code, streaming):
    data = dict(content=content, reasoning_content=reasoning)
    response = sse({'choices': [{'delta': data}]}, {'choices': [{'delta': {}, 'finish_reason': finish}]},
                   {'choices': [], 'usage': {'completion_tokens': 200}}) if streaming else httpx.Response(200,
                   json={'choices': [{'message': data, 'finish_reason': finish}]})
    calls = transport(monkeypatch, lambda *_: response)
    traces = []
    with pytest.raises(StepFailedError) as error:
        llm.chat_validated('评论', parser=lambda raw: pytest.fail('不能用思考或空正文修复格式'),
                           profile=PROFILE, trace=lambda event, content='', **meta: traces.append((event, meta)))
    assert error.value.code == code and len(calls) == 1
    detail = error.value.detail['providers'][0]['detail']
    assert detail['finish_reason'] == finish and detail['reasoning_chars'] == len(reasoning)
    assert any(event == 'response_metadata' and meta['finish_reason'] == finish for event, meta in traces)


def test_sse_error_envelope_not_ignored(isolated_env, monkeypatch):
    calls = transport(monkeypatch, lambda *_: sse({'error': {'code': 'provider_overload', 'message': '服务繁忙'}}))
    with pytest.raises(StepFailedError) as error:
        llm.chat('评论', profile=PROFILE)
    assert error.value.code == 'LLM_PROVIDER_ERROR' and len(calls) == 1
    assert '服务繁忙' in error.value.message


def test_failed_chain_retains_distinct_causes_without_format_retry(isolated_env, monkeypatch):
    calls = transport(monkeypatch, lambda count, _: httpx.Response(400, json={'error': {'code': 'SensitiveContentDetected'}})
                      if count == 1 else sse({'choices': [{'delta': {'reasoning_content': '思考'}, 'finish_reason': 'stop'}]}))
    with pytest.raises(StepFailedError) as error:
        llm.chat_validated('评论', parser=lambda raw: pytest.fail(), profile=[PROFILE, {**PROFILE, 'name': '备用'}])
    assert error.value.code == 'LLM_PROVIDERS_FAILED' and len(calls) == 2
    assert [row['code'] for row in error.value.detail['providers']] == ['LLM_CONTENT_REJECTED', 'LLM_EMPTY_RESPONSE']
    assert '内容审核' in error.value.message and '没有最终正文' in error.value.message


def test_missing_choices_is_domain_error(isolated_env, monkeypatch):
    transport(monkeypatch, lambda *_: httpx.Response(200, json={'choices': []}))
    with pytest.raises(StepFailedError) as error:
        llm.chat('评论', profile=PROFILE)
    assert error.value.code == 'LLM_RESPONSE_INVALID'


def test_explicit_refusal_not_used_as_valid_content(isolated_env, monkeypatch):
    transport(monkeypatch, lambda *_: sse({'choices': [{'delta': {'refusal': '无法处理'}, 'finish_reason': 'stop'}]}))
    with pytest.raises(StepFailedError) as error:
        llm.chat('评论', profile=PROFILE)
    assert error.value.code == 'LLM_CONTENT_REJECTED'


@pytest.mark.parametrize('event', [{'choices': 'bad'}, {'choices': ['bad']},
                                  {'choices': [{'delta': 'bad'}]}, {'choices': [{'delta': {'content': {'unexpected': True}}}]}])
def test_malformed_stream_is_domain_error(isolated_env, monkeypatch, event):
    transport(monkeypatch, lambda *_: sse(event))
    with pytest.raises(StepFailedError) as error:
        llm.chat('评论', profile=PROFILE)
    assert error.value.code == 'LLM_RESPONSE_INVALID'


def test_plain_compatibility_response_content_blocks(isolated_env, monkeypatch):
    transport(monkeypatch, lambda *_: httpx.Response(200, json={'choices': [{'message': {
        'content': [{'type': 'text', 'text': '第一段'}, {'type': 'text', 'text': '第二段'}]}}]}))
    assert llm.chat('评论', profile=PROFILE) == '第一段第二段'
