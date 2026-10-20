"""账号检查只验证选定身份；隔离凭据、数据库与真实网络。"""
import datetime

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from dubforge.db import get_engine
from dubforge.publishing import credentials, service, transport
from dubforge.publishing.models import Publication, PublishAccount


@pytest.fixture()
def account_check_env(isolated_env, monkeypatch):
    with Session(get_engine()) as session:
        session.add(PublishAccount(id='123', name='本地昵称', credential='encrypted-fixture', revision=4))
        session.commit()
    calls = []
    monkeypatch.setattr(credentials, 'unprotect', lambda value: {'fixture': 'private-fixture'})
    def remote(operation, payload=None, **kwargs):
        assert operation == 'account_check'
        assert payload == {'account_id': '123'}
        assert kwargs == {'credential': {'fixture': 'private-fixture'}, 'timeout': 30}
        calls.append(operation)
        return {'id': '123', 'name': '当前昵称', 'credential': '不得回传', 'extra': '不得回传'}
    monkeypatch.setattr(transport, 'call', remote)
    return calls


def test_check_account_returns_only_verified_identity_and_time_without_persisting(account_check_env, monkeypatch):
    checked = datetime.datetime(2026, 10, 8, 0, 1, 2, tzinfo=datetime.timezone.utc)
    monkeypatch.setattr(service, 'now', lambda: checked)
    assert service.check_account('123') == {
        'status': 'verified', 'id': '123', 'name': '当前昵称', 'checked_at': checked.isoformat()}
    assert account_check_env == ['account_check']
    with Session(get_engine()) as session:
        account = session.get(PublishAccount, '123')
        assert (account.name, account.revision, account.credential) == ('本地昵称', 4, 'encrypted-fixture')
        assert session.exec(select(Publication)).all() == []


@pytest.mark.parametrize('code', ['PUBLISH_LOGIN_EXPIRED', 'PUBLISH_VERIFICATION', 'PUBLISH_TLS', 'PUBLISH_REMOTE_ERROR'])
def test_check_account_retains_error_classification(account_check_env, monkeypatch, code):
    def fail(*args, **kwargs):
        raise transport.TransportError('安全错误提示', code=code)
    monkeypatch.setattr(transport, 'call', fail)
    with pytest.raises(transport.TransportError) as exc:
        service.check_account('123')
    assert exc.value.code == code
    assert exc.value.message == '安全错误提示'


@pytest.mark.parametrize('result,code', [
    (None, 'PUBLISH_ACCOUNT_RESPONSE'),
    ({'id': '123'}, 'PUBLISH_ACCOUNT_RESPONSE'),
    ({'id': 123, 'name': '昵称'}, 'PUBLISH_ACCOUNT_RESPONSE'),
    ({'id': '123', 'name': ''}, 'PUBLISH_ACCOUNT_RESPONSE'),
    ({'id': '123', 'name': '名' * 151}, 'PUBLISH_ACCOUNT_RESPONSE'),
    ({'id': '456', 'name': '其他账号'}, 'PUBLISH_ACCOUNT_MISMATCH'),
])
def test_check_account_rejects_incomplete_or_mismatched_identity(account_check_env, monkeypatch, result, code):
    monkeypatch.setattr(transport, 'call', lambda *a, **kw: result)
    with pytest.raises(service.PublishError) as exc:
        service.check_account('123')
    assert exc.value.code == code


def test_missing_or_unreadable_account_never_starts_worker(account_check_env, monkeypatch):
    with pytest.raises(service.PublishError) as exc:
        service.check_account('missing')
    assert exc.value.code == 'PUBLISH_NOT_FOUND'
    def unreadable(value):
        raise credentials.CredentialError('凭据不可读取，请重新扫码')
    monkeypatch.setattr(credentials, 'unprotect', unreadable)
    with pytest.raises(credentials.CredentialError) as exc:
        service.check_account('123')
    assert exc.value.code == 'PUBLISH_CREDENTIAL'
    assert account_check_env == []


def test_catalog_does_not_check_or_decrypt_saved_account(account_check_env, monkeypatch):
    monkeypatch.setattr(credentials, 'unprotect', lambda value: pytest.fail('本地目录不应读取凭据'))
    result = service.catalog()
    assert result['accounts'] == [{'id': '123', 'name': '本地昵称', 'revision': 4}]
    assert account_check_env == []


def test_account_check_api_is_no_store_and_keeps_safe_error_code(account_check_env, monkeypatch):
    from app.main import app
    client = TestClient(app)
    response = client.post('/api/publishing/accounts/123/check')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    result = response.json()
    assert set(result) == {'status', 'id', 'name', 'checked_at'}
    assert result['status'] == 'verified'
    assert datetime.datetime.fromisoformat(result['checked_at']).utcoffset() == datetime.timedelta(0)
    assert 'credential' not in response.text
    def fail(*args, **kwargs):
        raise transport.TransportError('登录已失效，请重新扫码连接账号', code='PUBLISH_LOGIN_EXPIRED')
    monkeypatch.setattr(transport, 'call', fail)
    response = client.post('/api/publishing/accounts/123/check')
    assert response.status_code == 400
    assert response.json()['code'] == 'PUBLISH_LOGIN_EXPIRED'
    assert '登录已失效' in response.json()['message']
    response = client.post('/api/publishing/accounts/123/check', headers={'Origin': 'https://evil.example'})
    assert response.status_code == 400
    assert response.json()['code'] == 'PUBLISH_ORIGIN'
