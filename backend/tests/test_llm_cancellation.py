"""取消静默等待必须关闭连接，不等待下个 token，不重试或切换备用模型。"""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

import httpx
import pytest

from dubforge.backends import llm
from dubforge.backends.llm_transport import CancellableClient
from dubforge.runner import StepCancelled


class SilentStream(httpx.AsyncByteStream):
    def __init__(self, started, cancelled, closed):
        self.started, self.cancelled, self.closed = started, cancelled, closed

    async def __aiter__(self):
        self.started.set()
        try:
            await asyncio.Future()
        finally:
            self.cancelled.set()
        yield b''

    async def aclose(self):
        self.closed.set()


def cancel_run(started, run):
    cancelled = threading.Event()
    errors = []

    def check():
        if cancelled.is_set():
            raise StepCancelled('步骤被用户取消')

    def work():
        try:
            run(check)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    try:
        assert started.wait(3), '请求没有进入预期的网络等待阶段'
        before = time.monotonic()
        cancelled.set()
        worker.join(timeout=2)
        assert not worker.is_alive(), '取消不能等待服务商返回下一个 token'
        assert time.monotonic() - before < 2
        assert len(errors) == 1 and isinstance(errors[0], StepCancelled)
    finally:
        cancelled.set()
        worker.join(timeout=3)


@pytest.mark.parametrize('phase', ['headers', 'sse', 'json', 'stream_fallback', 'params_fallback'])
def test_silent_requests_cancel_without_retry_or_provider_fallback(isolated_env, monkeypatch, phase):
    started, interrupted, closed = (threading.Event() for _ in range(3))
    bodies, events, logs = [], [], []

    async def handle(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1 and phase.endswith('_fallback'):
            field = 'stream' if phase == 'stream_fallback' else 'thinking'
            return httpx.Response(400, json={'error': {'message': f'Unsupported parameter: {field}'}})
        if phase == 'headers':
            started.set()
            try:
                await asyncio.Future()
            finally:
                interrupted.set()
        content_type = 'text/event-stream' if phase == 'sse' else 'application/json'
        return httpx.Response(200, headers={'content-type': content_type},
                              stream=SilentStream(started, interrupted, closed))

    monkeypatch.setattr(llm, '_client', lambda base: CancellableClient(
        base_url=base, transport=httpx.MockTransport(handle), trust_env=False))
    profiles = [dict(name='主模型', base_url='http://fixture.invalid', model='fixture',
                     params={'thinking': {'type': 'enabled'}}),
                dict(name='备用模型', base_url='http://backup.invalid', model='backup')]
    before_threads = {t.ident for t in threading.enumerate() if t.name == 'dubforge-llm-http'}
    cancel_run(started, lambda check: llm.chat_validated('测试', parser=lambda _: pytest.fail('取消不得校验'),
        profile=profiles, cancel_check=check, log=logs.append,
        trace=lambda event, content='', **meta: events.append((event, meta))))
    assert interrupted.is_set()
    if phase != 'headers':
        assert closed.is_set()
    assert len(bodies) == (2 if phase.endswith('_fallback') else 1)
    assert all(body['model'] == 'fixture' for body in bodies)
    assert not any('传输异常' in line for line in logs)
    assert any(event == 'request_cancelled' and meta['server_confirmed'] is False for event, meta in events)
    assert {t.ident for t in threading.enumerate() if t.name == 'dubforge-llm-http'} == before_threads


@pytest.mark.parametrize('headers_sent', [False, True])
def test_real_silent_connection_is_closed_on_cancel(isolated_env, headers_sent):
    """只在规定的 8611 测试端口观察对端 EOF，不调用外部 LLM。"""
    started, disconnected = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', '0')))
            if headers_sent:
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.flush()
            started.set()
            self.connection.settimeout(4)
            try:
                if self.connection.recv(1) == b'':
                    disconnected.set()
            except ConnectionResetError:
                disconnected.set()
            except TimeoutError:
                pass

    try:
        server = ThreadingHTTPServer(('127.0.0.1', 8611), Handler)
    except OSError as exc:
        pytest.skip(f'8611 测试端口已被占用，不影响现有服务：{exc}')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        cancel_run(started, lambda check: llm.chat('测试', cancel_check=check,
            profile=dict(name='本地测试', base_url='http://127.0.0.1:8611', model='fixture')))
        assert disconnected.wait(1), '本地返回取消后，实际网络连接仍未关闭'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_normal_async_stream_and_plain_response_still_work(isolated_env, monkeypatch):
    async def handle(request):
        body = json.loads(request.content)
        if body.get('stream'):
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                content='data: {"choices":[{"delta":{"content":"完成"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n')
        return httpx.Response(200, json={'choices': [{'message': {'content': 'OK'}}]})

    monkeypatch.setattr(llm, '_client', lambda base: CancellableClient(
        base_url=base, transport=httpx.MockTransport(handle), trust_env=False))
    profile = dict(name='测试', base_url='http://fixture.invalid', model='fixture')
    assert llm.chat('测试', profile=profile) == '完成'
    assert llm.test_provider(profile)['ok'] is True
