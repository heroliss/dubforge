"""超时不阻塞界面，也不把仍在计算的服务误当空闲；全程不调用真实模型。"""
import json
import threading
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dubforge import tts_requests
from dubforge.backends import tts_indextts25
from dubforge.runner import StepCancelled, StepTimeout


def test_single_request_has_bounded_wait_and_does_not_fake_server_cancellation(monkeypatch):
    clock = iter([0, 0, 301])
    monkeypatch.setattr(tts_indextts25.time, 'monotonic', lambda: next(clock))
    job = Mock()
    job.status.return_value = SimpleNamespace(progress_data=None)
    job.done.return_value = False
    job.result.side_effect = TimeoutError()
    with pytest.raises(tts_requests.RequestTimeout, match='300 秒'):
        tts_indextts25._result_with_progress(job, lambda value: None)
    job.cancel.assert_not_called()
    assert job.result.call_count == 1


@pytest.mark.parametrize('error', [StepCancelled('主动停止'), StepTimeout('步骤超时')])
def test_wait_checks_stop_even_without_progress(error):
    job = Mock()
    def check():
        raise error
    with pytest.raises(type(error)) as caught:
        tts_indextts25._result_with_progress(job, lambda value: None, check_cancel=check)
    assert caught.value is error
    job.result.assert_not_called()


def test_request_excludes_duplicate_clients_and_localhost_aliases(isolated_env):
    first = tts_requests.Request('http://localhost:7860/')
    try:
        assert not tts_requests.needs_recovery('http://127.0.0.1:7860')
        with pytest.raises(tts_requests.ServiceDraining):
            tts_requests.Request('http://127.0.0.1:7860')
        other = tts_requests.Request('http://localhost:7861')
        other.finish()
    finally:
        first.finish()


def test_orphan_record_survives_backend_restart_and_requires_verified_service_reset(isolated_env):
    url = 'http://127.0.0.1:7860'
    first = tts_requests.Request(url)
    tts_requests._active.pop(first.token)
    assert tts_requests.needs_recovery(url)
    with pytest.raises(tts_requests.ServiceDraining):
        tts_requests.Request(url)
    tts_requests.reset(url)
    second = tts_requests.Request(url)
    first.finish()
    assert json.loads(second.path.read_text())['token'] == second.token
    second.finish()


@pytest.mark.parametrize('record', ['[]', '{}', 'null', 'bad json'])
def test_corrupt_record_does_not_advertise_idle_service(isolated_env, record):
    from dubforge.utils.fs import atomic_write_bytes
    url = 'http://localhost:7860'
    atomic_write_bytes(tts_requests._path(url), record.encode())
    assert tts_requests.needs_recovery(url)
    with pytest.raises(tts_requests.ServiceDraining):
        tts_requests.Request(url)
    tts_requests.reset(url)


def test_detached_request_blocks_until_actual_result_without_registering_audio(isolated_env):
    url = 'http://127.0.0.1:7860'
    request = tts_requests.Request(url)
    job, closed = Future(), threading.Event()
    client = SimpleNamespace(close=closed.set)
    request.detach(job, client, lambda message: None)
    try:
        assert tts_requests.needs_recovery(url)
        with pytest.raises(tts_requests.ServiceDraining):
            tts_requests.Request(url)
        job.set_result('remote-output.wav')
        assert closed.wait(2)
        assert not tts_requests.needs_recovery(url)
        assert not request.path.exists()
    finally:
        if not job.done():
            job.set_result(None)
        closed.wait(2)
        request.finish()


def test_disconnected_observer_does_not_claim_server_stopped(isolated_env):
    url = 'http://127.0.0.1:7860'
    request = tts_requests.Request(url)
    job, closed = Future(), threading.Event()
    request.detach(job, SimpleNamespace(close=closed.set), lambda message: None)
    job.set_exception(ConnectionError('连接中断'))
    assert closed.wait(2)
    assert tts_requests.needs_recovery(url)
    request.finish()


def test_service_status_explains_orphan_and_keeps_manual_restart_available(isolated_env, monkeypatch):
    from dubforge import tts_service
    request = tts_requests.Request('http://localhost:7860')
    tts_requests._active.pop(request.token)
    monkeypatch.setattr(tts_service, '_status', lambda *args, **kwargs: {
        'backend': 'indextts25', 'url': 'http://localhost:7860', 'online': True,
        'status': 'online', 'can_stop': True})
    result = tts_service.status()
    assert result['status'] == 'draining' and result['can_stop']
    assert '可能仍在生成' in result['message']
    request.finish()


def test_adapter_checks_cancel_and_preserves_existing_audio(isolated_env, monkeypatch, tmp_path):
    import gradio_client
    output = tmp_path / 'old.wav'
    output.write_bytes(b'old')
    client = Mock()
    job = client.submit.return_value
    job.status.return_value = SimpleNamespace(progress_data=None)
    job.done.return_value = False
    job.result.side_effect = TimeoutError()
    monkeypatch.setattr(gradio_client, 'Client', Mock(return_value=client))
    monkeypatch.setattr(gradio_client, 'handle_file', lambda path: path)
    monkeypatch.setattr(tts_indextts25, 'is_online', lambda url: True)
    observers = []
    monkeypatch.setattr(tts_requests.threading, 'Thread', lambda **kwargs:
                        SimpleNamespace(start=lambda: observers.append(kwargs['target'])))
    checks = iter([None, None, StepTimeout('步骤超时')])
    def check():
        if error := next(checks):
            raise error
    with pytest.raises(StepTimeout):
        tts_indextts25.synth('http://localhost:7860', '你好', output, 'ZH',
                            timbre_ref=output, check_cancel=check)
    assert output.read_bytes() == b'old'
    assert tts_requests.needs_recovery('http://localhost:7860')
    assert len(observers) == 1
    job.cancel.assert_not_called()
    job.result.side_effect = None
    job.result.return_value = str(output)
    observers[0]()
    assert not tts_requests.needs_recovery('http://localhost:7860')
    client.close.assert_called_once()
