"""扫码状态与保存边界；不访问真实B站账号，不记录任何真实凭据。"""
import threading
import time

import pytest
from sqlmodel import Session

from dubforge.db import get_engine
from dubforge.publishing import service, transport, credentials
from dubforge.publishing.models import PublishAccount


@pytest.fixture()
def login_state(isolated_env, monkeypatch):
    state = dict(status='generating', url='', error='', code='', created=time.time(), cancel=threading.Event())
    monkeypatch.setattr(service, '_logins', {'fixture': state})
    monkeypatch.setattr(credentials, 'protect', lambda value: 'encrypted-fixture')
    return state


def test_login_verified_then_saved(login_state, monkeypatch):
    def remote(operation, **kwargs):
        assert operation == 'login'
        for event, expected in [('qr', 'waiting'), ('scanned', 'scanned'), ('verifying', 'verifying')]:
            kwargs['on_event'](dict(event=event, url='https://passport.bilibili.com/fixture'))
            assert service.login_view('fixture')['status'] == expected
        return dict(id='123', name='测试账号', credential={'fake': 'secret'})
    monkeypatch.setattr(transport, 'call', remote)
    def protect(value):
        assert service.login_view('fixture')['status'] == 'saving'
        return 'encrypted-fixture'
    monkeypatch.setattr(credentials, 'protect', protect)
    service._login('fixture')
    assert service.login_view('fixture') == dict(status='connected', url='', error='', code='')
    assert 'credential' not in service.login_view('fixture')
    with Session(get_engine()) as session:
        assert session.get(PublishAccount, '123').credential == 'encrypted-fixture'
    # 已完成保存后迟到的取消不应把已连接改为取消。
    assert service.login_view('fixture', cancel=True)['status'] == 'connected'


@pytest.mark.parametrize('confirmed,code', [(False, 'PUBLISH_QR_EXPIRED'), (True, 'PUBLISH_VERIFICATION'), (True, 'PUBLISH_TLS')])
def test_login_retains_safe_phase_and_reason(login_state, monkeypatch, confirmed, code):
    def remote(*args, **kwargs):
        if confirmed:
            kwargs['on_event']({'event': 'verifying'})
        raise transport.TransportError('脱敏后的具体原因', code=code)
    monkeypatch.setattr(transport, 'call', remote)
    service._login('fixture')
    view = service.login_view('fixture')
    assert view['code'] == code
    assert view['status'] == ('expired' if code == 'PUBLISH_QR_EXPIRED' else 'failed')
    assert '脱敏后的具体原因' in view['error']
    assert ('手机端已确认' in view['error']) == confirmed
    assert '扫码未完成' not in view['error']


def test_login_unknown_error_does_not_leak_credentials(login_state, monkeypatch):
    def remote(*args, **kwargs):
        raise RuntimeError('SESSDATA=never-expose-this')
    monkeypatch.setattr(transport, 'call', remote)
    service._login('fixture')
    assert 'never-expose' not in str(service.login_view('fixture'))


@pytest.mark.parametrize('fail', [False, True])
def test_login_cancel_wins_over_late_completion_and_error(login_state, monkeypatch, fail):
    def remote(*args, **kwargs):
        service.login_view('fixture', cancel=True)
        kwargs['on_event']({'event': 'qr', 'url': 'https://passport.bilibili.com/late'})
        if fail:
            raise transport.TransportError('已停止')
        return dict(id='123', name='测试账号', credential={'fake': 'secret'})
    monkeypatch.setattr(transport, 'call', remote)
    service._login('fixture')
    assert service.login_view('fixture') == dict(status='cancelled', url='', error='', code='')
    with Session(get_engine()) as session:
        assert session.get(PublishAccount, '123') is None


def test_login_save_failure_is_not_reported_as_scan_failure(login_state, monkeypatch):
    monkeypatch.setattr(transport, 'call', lambda *a, **kw: dict(id='123',name='测试',credential={}))
    def protect(value):
        raise credentials.CredentialError('系统凭据保护失败')
    monkeypatch.setattr(credentials, 'protect', protect)
    service._login('fixture')
    assert service.login_view('fixture')['code'] == 'PUBLISH_CREDENTIAL'
    assert '手机端已确认' in service.login_view('fixture')['error']
    assert '系统凭据保护失败' in service.login_view('fixture')['error']


@pytest.mark.parametrize('status', ['generating', 'waiting', 'scanned', 'verifying', 'saving'])
def test_login_in_progress_prevents_duplicate_session(login_state, status):
    login_state['status'] = status
    assert service.login_start() == {'id': 'fixture'}
    assert len(service._logins) == 1


def test_missing_login_session_has_recoverable_code(login_state):
    with pytest.raises(service.PublishError) as exc:
        service.login_view('missing')
    assert exc.value.code == 'PUBLISH_LOGIN_NOT_FOUND'
