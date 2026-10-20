"""硬件编码与配音的原子预约；不创建或停止真实进程、不发服务探测。"""
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from dubforge import tts_service as service, tts_requests
from dubforge.errors import DubForgeError, ServiceStartError, ServiceStopError


@pytest.fixture
def reserved_service(isolated_env, monkeypatch):
    monkeypatch.setattr(service, '_users', {})
    monkeypatch.setattr(service, '_render_reservations', {})
    monkeypatch.setattr(service, '_managed_process', lambda *_: None)
    monkeypatch.setattr(tts_requests, 'needs_recovery', lambda *_: False)
    monkeypatch.setattr(tts_requests, 'pending', lambda *_: False)
    calls = []
    monkeypatch.setattr(service, 'stop', lambda *a, **kw: calls.append(kw))
    values = {'tts.backend': 'indextts25', 'tts.indextts25.url': 'http://127.0.0.1:7860',
              'runner.auto_tts_vram': True}
    return values, lambda key, default=None: values.get(key, default), calls


def test_idle_managed_stop_uses_existing_policy_and_no_probe(reserved_service, monkeypatch):
    values, get, calls = reserved_service
    monkeypatch.setattr(service, '_managed_process', lambda *_:
        ('indextts25', SimpleNamespace(poll=lambda: None), None))
    with service.reserve_for_render(get):
        assert calls == [{'probe': False}]
        with pytest.raises(DubForgeError) as error:
            with service.use(get): pytest.fail('硬件编码预约期间不可新配音')
        assert error.value.code == 'TTS_SERVICE_RENDER_BUSY'
        monkeypatch.setattr(service, 'status', lambda *_a, **_kw: pytest.fail('start预约检查应在任何探测前'))
        with pytest.raises(ServiceStartError): service.start(get)
    assert service._render_reservations == {}
    with service.use(get): assert service._users
    assert service._users == {}


def test_ordinary_use_wins_race_and_render_cannot_stop_active_service(reserved_service):
    _values, get, calls = reserved_service
    entered, release = threading.Event(), threading.Event()
    def use():
        with service.use(get):
            entered.set(); assert release.wait(3)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(use)
        try:
            assert entered.wait(3)
            with pytest.raises(DubForgeError) as error:
                with service.reserve_for_render(get): pytest.fail('不能拿到活动服务')
            assert error.value.code == 'ASSEMBLE_ENCODER_BUSY' and not calls
        finally:
            release.set()
        future.result()
    assert service._users == {} and service._render_reservations == {}


def test_start_checked_before_reserve_rechecks_before_actual_spawn(reserved_service, monkeypatch):
    _values, get, calls = reserved_service
    reservation = service.reserve_for_render(get)
    def delayed_status(*_a, **_kw):
        reservation.__enter__()
        return dict(online=False, status='offline', can_start=True, message='')
    monkeypatch.setattr(service, 'status', delayed_status)
    monkeypatch.setattr(service, '_launch_settings', lambda *_: ('indextts25', 7860, '127.0.0.1', 'fixture', ['forbidden']))
    try:
        with pytest.raises(ServiceStartError): service.start(get)
    finally:
        reservation.__exit__(None, None, None)
    assert not calls and service._render_reservations == {}


def test_inflight_request_is_protected_even_without_service_use_counter(reserved_service, monkeypatch):
    _values, get, calls = reserved_service
    monkeypatch.setattr(tts_requests, 'pending', lambda *_: True)
    # needs_recovery=False 只表示本进程仍在观察，不是服务已经空闲。
    assert not tts_requests.needs_recovery(get('tts.indextts25.url'))
    with pytest.raises(DubForgeError) as error:
        with service.reserve_for_render(get): pytest.fail('inflight请求不可被自动停止')
    assert error.value.code == 'ASSEMBLE_ENCODER_BUSY'
    assert not calls and not service._render_reservations


@pytest.mark.parametrize('url', ['https://localhost:7860', 'http://127.0.0.1:7860/api'])
@pytest.mark.parametrize('marker', ['pending', 'needs_recovery'])
def test_local_url_without_control_permission_still_protects_unfinished_requests(
        reserved_service, monkeypatch, url, marker):
    values, get, calls = reserved_service
    values['tts.indextts25.url'] = url
    monkeypatch.setattr(tts_requests, marker, lambda *_: True)
    monkeypatch.setattr(service, '_managed_process', lambda *_: pytest.fail('资源识别不能扩大本机控制权限'))
    with pytest.raises(DubForgeError) as error:
        with service.reserve_for_render(get): pytest.fail('本机未核对请求仍在占用硬件')
    assert error.value.code == 'ASSEMBLE_ENCODER_BUSY'
    assert not calls and not service._render_reservations


@pytest.mark.parametrize('backend,url', [('gptsovits', 'http://localhost:9880'),
    ('indextts25', 'http://127.0.0.1:17860'), ('indextts25', 'https://localhost:7860'),
    ('gptsovits', 'http://127.0.0.1:9880/api')])
def test_any_local_backend_or_port_is_blocked_but_remote_use_is_allowed(reserved_service, monkeypatch, backend, url):
    _values, get, calls = reserved_service
    other = {'tts.backend': backend, f'tts.{backend}.url': url}
    other_get = lambda key, default=None: other.get(key, default)
    remote = {'tts.backend': 'indextts25', 'tts.indextts25.url': 'http://remote.invalid:7860'}
    remote_get = lambda key, default=None: remote.get(key, default)
    with service.reserve_for_render(get):
        with pytest.raises(DubForgeError):
            with service.use(other_get): pytest.fail('不能以换后端/端口绕过GPU预约')
        with pytest.raises(ServiceStartError): service.start(other_get)
        with service.use(remote_get): pass
    with service.use(other_get):
        with pytest.raises(DubForgeError) as error:
            with service.reserve_for_render(get): pytest.fail('不能停止另一本机活跃服务')
        assert error.value.code == 'ASSEMBLE_ENCODER_BUSY'
    assert not calls


def test_remote_tts_configuration_still_reserves_local_encoder_without_stopping_remote(reserved_service):
    _values, local_get, calls = reserved_service
    remote = {'tts.backend': 'indextts25', 'tts.indextts25.url': 'http://remote.invalid:7860'}
    remote_get = lambda key, default=None: remote.get(key, default)
    with service.reserve_for_render(remote_get):
        with service.use(remote_get): pass
        with pytest.raises(DubForgeError):
            with service.use(local_get): pytest.fail('本机NVENC仍须阻止新的本机模型')
    assert not calls and not service._render_reservations


@pytest.mark.parametrize('case', ['policy_false', 'unmanaged', 'remote', 'exited'])
def test_non_owned_remote_or_disabled_policy_never_stops(reserved_service, monkeypatch, case):
    values, get, calls = reserved_service
    if case == 'remote':
        values['tts.indextts25.url'] = 'http://remote.invalid:7860'
        monkeypatch.setattr(service, '_managed_process', lambda *_: pytest.fail('远端不查本机进程'))
    elif case == 'policy_false':
        values['runner.auto_tts_vram'] = False
        monkeypatch.setattr(service, '_managed_process', lambda *_: ('indextts25', SimpleNamespace(poll=lambda: None), None))
    elif case == 'exited':
        monkeypatch.setattr(service, '_managed_process', lambda *_: ('indextts25', SimpleNamespace(poll=lambda: 0), None))
    with service.reserve_for_render(get): pass
    assert not calls and service._render_reservations == {}


@pytest.mark.parametrize('case', ['pending', 'wrong_owner', 'stop_error', 'body_error'])
def test_unknown_busy_identity_and_exceptions_release_reservation_without_stop(reserved_service, monkeypatch, case):
    _values, get, calls = reserved_service
    if case == 'pending':
        monkeypatch.setattr(tts_requests, 'needs_recovery', lambda *_: True)
    elif case == 'wrong_owner':
        monkeypatch.setattr(service, '_managed_process', lambda *_: ('gptsovits', SimpleNamespace(poll=lambda: None), None))
    elif case == 'stop_error':
        monkeypatch.setattr(service, '_managed_process', lambda *_: ('indextts25', SimpleNamespace(poll=lambda: None), None))
        def refuse(*a, **kw): raise ServiceStopError('身份复核失败')
        monkeypatch.setattr(service, 'stop', refuse)
    with pytest.raises(DubForgeError):
        with service.reserve_for_render(get):
            if case == 'body_error': raise DubForgeError('隔离取消', code='CANCELLED')
    assert service._render_reservations == {} and not calls
    with service.use(get): pass


def test_reservation_does_not_hold_lock_and_status_explains_start_unavailable(reserved_service, monkeypatch):
    _values, get, _calls = reserved_service
    monkeypatch.setattr(service, '_status', lambda *_a, **_kw:
        dict(backend='indextts25', url=get('tts.indextts25.url'), can_start=True, online=False, status='offline'))
    with service.reserve_for_render(get):
        result = service.status(get, probe=False)
        assert result['render_busy'] and not result['can_start']
        # 另一线程仍可读状态；预约不把服务管理锁持有到成片结束。
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(lambda: service.status(get, probe=False)['render_busy']).result(timeout=3)
