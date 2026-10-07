"""守卫覆盖真实异步等待与兼容回退，只有HTTP MockTransport，无外部服务。"""
import asyncio
import json
import threading
import time

import httpx
import pytest

from dubforge.backends import llm
from dubforge.backends.llm_guard import ResponseGuard, ResponseGuardError
from dubforge.backends.llm_transport import CancellableClient


PROFILE = {'name': '隔离主配置', 'model': 'fixture', 'base_url': 'http://fixture.invalid',
           'params': {'thinking': {'type': 'enabled'}}}
BACKUP = {**PROFILE, 'name': '备用配置', 'model': 'backup'}


def stream(text, finish='stop'):
    event = {'choices': [{'index': 0, 'delta': {'content': text}, 'finish_reason': finish}]}
    return httpx.Response(200, headers={'content-type': 'text/event-stream'},
        content='data: ' + json.dumps(event) + '\n\ndata: [DONE]\n')


class SilentStream(httpx.AsyncByteStream):
    def __init__(self, interrupted, closed):
        self.interrupted, self.closed = interrupted, closed

    async def __aiter__(self):
        try:
            await asyncio.Future()
        finally:
            self.interrupted.set()
        yield b''

    async def aclose(self):
        self.closed.set()


@pytest.mark.parametrize('phase', ['headers', 'sse', 'json', 'stream_fallback', 'params_fallback'])
def test_request_deadline_cancels_silent_io_without_format_retry_or_provider_fallback(
        isolated_env, monkeypatch, phase):
    interrupted, closed = threading.Event(), threading.Event()
    calls, guards, traces = [], [], []

    async def handle(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1 and phase.endswith('_fallback'):
            field = 'stream' if phase == 'stream_fallback' else 'thinking'
            return httpx.Response(400, json={'error': {'message': f'Unsupported parameter: {field}'}})
        if phase == 'headers':
            try:
                await asyncio.Future()
            finally:
                interrupted.set()
        content_type = 'text/event-stream' if phase == 'sse' else 'application/json'
        return httpx.Response(200, headers={'content-type': content_type},
                             stream=SilentStream(interrupted, closed))

    def factory():
        guard = ResponseGuard(max_chars=1000, timeout_s=.15)
        guards.append(guard)
        return guard

    monkeypatch.setattr(llm, '_client', lambda base: CancellableClient(base_url=base,
        transport=httpx.MockTransport(handle), trust_env=False))
    before_threads = {thread.ident for thread in threading.enumerate() if thread.name == 'dubforge-llm-http'}
    started = time.monotonic()
    with pytest.raises(ResponseGuardError) as error:
        llm.chat_validated('原文', parser=lambda _: pytest.fail('deadline不得进入格式解析'),
            profile=[PROFILE, BACKUP], response_guard_factory=factory, max_retries=3,
            trace=lambda event, content='', **meta: traces.append((event, meta)))
    assert error.value.code == 'LLM_REQUEST_DEADLINE'
    assert time.monotonic() - started < 2
    assert len(calls) == len(guards) == (2 if phase.endswith('_fallback') else 1)
    assert all(body['model'] == 'fixture' for body in calls)
    assert interrupted.is_set() and (phase == 'headers' or closed.is_set())
    assert any(event == 'request_aborted' and meta['code'] == 'LLM_REQUEST_DEADLINE'
               and meta['server_confirmed'] is False for event, meta in traces)
    assert {thread.ident for thread in threading.enumerate() if thread.name == 'dubforge-llm-http'} == before_threads


def test_guard_contract_retry_is_fresh_and_within_one_validation_budget(isolated_env, monkeypatch):
    calls, guards, parsed, hooks, traces = [], [], [], [], []

    async def handle(request):
        calls.append(json.loads(request.content))
        return stream('MODEL INTERNAL NOTE\n' if len(calls) == 1 else '1. 正文')

    def line_check(line):
        if line and not line.startswith('1. '):
            raise ValueError('需要编号正文')

    def factory():
        guard = ResponseGuard(max_chars=1000, timeout_s=2, line_check=line_check)
        guards.append(guard)
        return guard

    monkeypatch.setattr(llm, '_client', lambda base: CancellableClient(base_url=base,
        transport=httpx.MockTransport(handle), trust_env=False))
    def retry_context(error, attempt, raw):
        hooks.append((error.code, attempt, raw))
        return 'conversation'
    result = llm.chat_validated('完整原文', parser=lambda raw: parsed.append(raw) or raw,
        system='规则', profile=[PROFILE, BACKUP], max_retries=2, response_guard_factory=factory,
        retry_context=retry_context, retry_request=lambda error, attempt: '重置后的完整原文',
        retry_prompt=lambda error, attempt: '只输出正文',
        trace=lambda event, content='', **meta: traces.append((event, meta)))
    assert result == '1. 正文' and parsed == ['1. 正文']
    assert len(calls) == len(guards) == 2 and all(body['model'] == 'fixture' for body in calls)
    assert hooks == [('LLM_OUTPUT_CONTRACT', 2, '')]
    assert [message['role'] for message in calls[1]['messages']] == ['system', 'user']
    assert calls[1]['messages'][-1]['content'] == '重置后的完整原文\n\n只输出正文'
    assert 'MODEL INTERNAL NOTE' not in json.dumps(calls[1])
    aborted = [meta for event, meta in traces if event == 'request_aborted']
    assert len(aborted) == 1 and aborted[0]['detail']['content_excerpt'].startswith('MODEL INTERNAL')


@pytest.mark.parametrize('plain', [True, False])
def test_exhausted_response_budget_keeps_code_and_never_sends_huge_loop_to_retry(isolated_env, monkeypatch, plain):
    calls, traces = [], []
    oversized = 'REPEATED' * 1000

    async def handle(request):
        calls.append(json.loads(request.content))
        if plain:
            return httpx.Response(200, json={'choices': [{'message': {'content': oversized}, 'finish_reason': 'stop'}]})
        return stream(oversized)

    monkeypatch.setattr(llm, '_client', lambda base: CancellableClient(base_url=base,
        transport=httpx.MockTransport(handle), trust_env=False))
    with pytest.raises(ResponseGuardError) as error:
        llm.chat_validated('完整输入', parser=lambda _: pytest.fail('超限候选不得解析'),
            profile=[PROFILE, BACKUP], max_retries=2,
            response_guard_factory=lambda: ResponseGuard(max_chars=10, timeout_s=2),
            trace=lambda event, content='', **meta: traces.append((event, meta)))
    assert error.value.code == 'LLM_OUTPUT_BUDGET'
    assert error.value.detail['validation_attempts'] == 2
    assert error.value.detail['guard_validation_failures'] == 2
    assert len(calls) == 2 and all(body['model'] == 'fixture' for body in calls)
    assert all(oversized not in json.dumps(body) for body in calls)
    assert len(error.value.detail['content_excerpt']) <= 240
    assert len([event for event, _ in traces if event == 'request_aborted']) == 2


def test_transport_retry_gets_a_fresh_guard_after_partial_response(isolated_env, monkeypatch):
    calls, guards = [], []
    closed = threading.Event()

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"AB"}}]}\n\n'
            raise httpx.ReadError('隔离模拟连接中断')
        async def aclose(self): closed.set()

    async def handle(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=BrokenStream())
        return stream('XYZ')

    def factory():
        guard = ResponseGuard(max_chars=4, timeout_s=2)
        guards.append(guard)
        return guard

    monkeypatch.setattr(llm, '_client', lambda base: CancellableClient(base_url=base,
        transport=httpx.MockTransport(handle), trust_env=False))
    monkeypatch.setattr(llm, '_sleep_retry', lambda *args: None)
    assert llm.chat('原文', profile=PROFILE, response_guard_factory=factory) == 'XYZ'
    assert len(calls) == len(guards) == 2 and closed.is_set()
    assert [guard.counts['content'] for guard in guards] == [2, 3]


@pytest.mark.parametrize('field', ['stream', 'thinking'])
def test_successful_plain_compatibility_fallback_is_guarded_with_fresh_budget(isolated_env, monkeypatch, field):
    calls, guards = [], []
    async def handle(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(400, json={'error': {'message': f'Unsupported parameter: {field}'}})
        return httpx.Response(200, json={'choices': [{'message': {'content': '成功'}, 'finish_reason': 'stop'}]})
    def factory():
        guard = ResponseGuard(max_chars=4, timeout_s=2)
        guards.append(guard)
        return guard
    monkeypatch.setattr(llm, '_client', lambda base: CancellableClient(base_url=base,
        transport=httpx.MockTransport(handle), trust_env=False))
    assert llm.chat('原文', profile=PROFILE, response_guard_factory=factory) == '成功'
    assert len(calls) == len(guards) == 2
    assert [guard.counts['content'] for guard in guards] == [0, 2]


def test_partial_repair_guard_error_resets_full_contract_before_fresh_retry(isolated_env, monkeypatch):
    calls, required, checked = [], [1, 2], []
    class Missing(ValueError):
        pass
    async def handle(request):
        calls.append(json.loads(request.content))
        return stream(['1. 第一行', 'MODEL INTERNAL NOTE\n', '1. 第一行\n2. 第二行'][len(calls) - 1])
    def line_check(line):
        if line:
            checked.append((tuple(required), line))
            if not any(line.startswith(f'{number}. ') for number in required):
                raise ValueError('需要当前窗口编号')
    def retry_context(error, attempt, raw):
        required[:] = [2] if isinstance(error, Missing) else [1, 2]
        if isinstance(error, ResponseGuardError):
            assert raw == ''
        return 'fresh'
    def parser(raw):
        if '2. ' not in raw:
            raise Missing('缺少第二行')
        return raw
    monkeypatch.setattr(llm, '_client', lambda base: CancellableClient(base_url=base,
        transport=httpx.MockTransport(handle), trust_env=False))
    result = llm.chat_validated('完整两行', parser=parser, profile=PROFILE, max_retries=3,
        response_guard_factory=lambda: ResponseGuard(max_chars=1000, timeout_s=2, line_check=line_check),
        retry_context=retry_context,
        retry_request=lambda error, attempt: '只补第二行' if isinstance(error, Missing) else '重新完整两行')
    assert result == '1. 第一行\n2. 第二行' and len(calls) == 3
    assert '只补第二行' in calls[1]['messages'][-1]['content']
    assert '重新完整两行' in calls[2]['messages'][-1]['content']
    assert [row['role'] for row in calls[2]['messages']] == ['user']
    assert ((2,), 'MODEL INTERNAL NOTE') in checked
    assert ((1, 2), '2. 第二行') in checked


@pytest.mark.parametrize('sequence', [('parser', 'guard'), ('guard', 'parser')])
def test_guard_failure_counter_does_not_count_normal_parser_failures(isolated_env, monkeypatch, sequence):
    calls = []
    def chat(*args, **kwargs):
        phase = sequence[len(calls)]
        calls.append(phase)
        if phase == 'guard':
            raise ResponseGuardError('格式门禁', code='LLM_OUTPUT_CONTRACT', detail={'excerpt': '短证据'})
        return '正常接收但格式不合格'
    def parser(raw):
        raise ValueError('领域格式不合格')
    monkeypatch.setattr(llm, 'chat', chat)
    with pytest.raises(llm.StepFailedError) as error:
        llm.chat_validated('全文', parser=parser, max_retries=2,
                           response_guard_factory=lambda: ResponseGuard(max_chars=100, timeout_s=2))
    assert calls == list(sequence)
    assert error.value.guard_validation_failures == 1
    if sequence[-1] == 'guard':
        assert error.value.code == 'LLM_OUTPUT_CONTRACT'
        assert error.value.detail['guard_validation_failures'] == 1
        assert error.value.detail['validation_attempts'] == 2
    else:
        assert error.value.code == 'LLM_OUTPUT_INVALID'
        assert isinstance(error.value.detail, str)
