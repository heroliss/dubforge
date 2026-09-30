"""创建合集使用伪账号和伪网络；不在真实B站账号中创建或上传。"""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from dubforge import db
from dubforge.errors import DubForgeError
from dubforge.publishing import credentials, service, transport
from dubforge.publishing.models import PublishAccount, PublishCollectionRequest


@pytest.fixture
def collection_env(isolated_env, monkeypatch):
    with Session(db.get_engine()) as session:
        session.add(PublishAccount(id='123', name='测试账号', credential='fixture'))
        session.add(PublishAccount(id='456', name='另一个账号', credential='fixture'))
        session.commit()
    monkeypatch.setattr(credentials, 'unprotect', lambda value: {'fixture':True})
    cover = service.store_cover(b'\xff\xd8\xfffixture')['cover_id']
    body = dict(request_id='a' * 32, confirmed=True, title='旅行系列', description='合集简介', cover_id=cover)
    calls = []
    def remote(operation, payload, **kwargs):
        calls.append((operation, payload))
        if operation == 'create_collection':
            with Session(db.get_engine()) as session:
                assert session.get(PublishCollectionRequest, body['request_id']).status == 'submitting'
            return dict(season_id=10)
        if operation == 'metadata':
            return dict(seasons=[dict(id=10, title='旅行系列', sections=[dict(id=11, title='正片')])])
        pytest.fail(f'不应调用 {operation}')
    monkeypatch.setattr(transport, 'call', remote)
    return body, calls


def test_create_is_explicit_empty_collection_and_idempotent(collection_env):
    body, calls = collection_env
    result = service.create_collection('123', body)
    assert result['status'] == 'created' and result['season_id'] == 10
    assert service.create_collection('123', body) == result
    # 同内容换请求号，也只复用已有创建回执。
    assert service.create_collection('123', {**body, 'request_id':'b' * 32}) == result
    assert [op for op, _ in calls] == ['create_collection']
    assert set(calls[0][1]) == {'account_id','title','description','cover_path','confirmed'}
    assert service.pending_collections('123') == []


@pytest.mark.parametrize('patch', [dict(confirmed=False), dict(confirmed=1), dict(title=' '),
    dict(title='x' * 51), dict(description='x' * 501), dict(description=None),
    dict(request_id='../bad'), dict(cover_id='../config.yaml'), dict(extra='bad')])
def test_invalid_inputs_never_reach_remote(collection_env, patch):
    body, calls = collection_env
    with pytest.raises(DubForgeError):
        service.create_collection('123', {**body, **patch})
    assert calls == []


def test_request_id_cannot_change_account_or_payload(collection_env):
    body, calls = collection_env
    service.create_collection('123', body)
    with pytest.raises(DubForgeError, match='资料已变化'):
        service.create_collection('456', body)
    with pytest.raises(DubForgeError, match='资料已变化'):
        service.create_collection('123', {**body, 'title':'另一个合集'})
    assert len(calls) == 1


def test_unknown_survives_restart_and_blocks_changed_request(collection_env, monkeypatch):
    body, _ = collection_env
    def timeout(*a, **kw):
        raise transport.TransportError('超时')
    monkeypatch.setattr(transport, 'call', timeout)
    with pytest.raises(DubForgeError) as exc:
        service.create_collection('123', body)
    assert exc.value.code == 'PUBLISH_COLLECTION_UNKNOWN'
    db.reset_engine()
    pending = service.pending_collections('123')
    assert pending[0]['busy'] is False and pending[0]['title'] == body['title']
    monkeypatch.setattr(transport, 'call', lambda *a, **kw: pytest.fail('不能盲重发'))
    with pytest.raises(DubForgeError):
        service.create_collection('123', {**body, 'request_id':'b' * 32, 'title':'换名字也不能绕过'})
    with pytest.raises(DubForgeError):
        service.resolve_collection('456', body['request_id'], {'confirmed':True,'season_id':None})
    with pytest.raises(DubForgeError):
        service.resolve_collection('123', body['request_id'], {'confirmed':False,'season_id':None})
    assert service.resolve_collection('123', body['request_id'], {'confirmed':True,'season_id':None})['status'] == 'failed'
    assert service.pending_collections('123') == []


@pytest.mark.parametrize('code', ['PUBLISH_COLLECTION_NOT_SENT', 'PUBLISH_COLLECTION_REJECTED'])
def test_definite_failure_allows_explicit_retry(collection_env, monkeypatch, code):
    body, calls = collection_env
    original = transport.call
    monkeypatch.setattr(transport, 'call', lambda *a, **kw: (_ for _ in ()).throw(transport.TransportError('已拒绝', code=code)))
    with pytest.raises(DubForgeError) as error:
        service.create_collection('123', body)
    assert error.value.code == code
    monkeypatch.setattr(transport, 'call', original)
    assert service.create_collection('123', body)['season_id'] == 10
    assert len(calls) == 1


def test_collection_diagnostics_survive_api_and_persistence(collection_env, monkeypatch):
    from app.main import app
    body, _ = collection_env
    message = '合集封面上传失败（平台错误码 -400）：图片格式错误。合集创建请求尚未发送。'
    detail = dict(stage='cover',remote_code=-400,http_status=200,headers={'Cookie':'不可传播'})
    def fail(*args, **kwargs):
        raise transport.TransportError(message,code='PUBLISH_COLLECTION_NOT_SENT',detail=detail)
    monkeypatch.setattr(transport,'call',fail)
    result=TestClient(app).post('/api/publishing/accounts/123/collections',json=body)
    assert result.status_code==400 and result.json()['message']==message
    assert result.json()['detail']==dict(stage='cover',remote_code=-400,http_status=200)
    assert 'Cookie' not in result.text and '不可传播' not in result.text
    with Session(db.get_engine()) as session:
        row=session.get(PublishCollectionRequest,body['request_id'])
        assert row.error==message and row.status=='failed'


def test_reconcile_existing_collection_checks_account_list(collection_env, monkeypatch):
    body, calls = collection_env
    original = transport.call
    monkeypatch.setattr(transport, 'call', lambda *a, **kw: {})
    with pytest.raises(DubForgeError):
        service.create_collection('123', body)
    monkeypatch.setattr(transport, 'call', original)
    with pytest.raises(DubForgeError, match='此账号'):
        service.resolve_collection('123', body['request_id'], {'confirmed':True,'season_id':999})
    assert service.resolve_collection('123', body['request_id'], {'confirmed':True,'season_id':10})['status'] == 'created'
    assert service.create_collection('123', body)['season_id'] == 10
    assert [op for op, _ in calls] == ['metadata','metadata']


def test_inflight_duplicate_and_resolution_are_blocked(collection_env, monkeypatch):
    body, _ = collection_env
    entered, release = threading.Event(), threading.Event()
    def slow(*a, **kw):
        entered.set(); assert release.wait(5); return {'season_id':10}
    monkeypatch.setattr(transport, 'call', slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(service.create_collection, '123', body)
        try:
            assert entered.wait(5)
            assert service.pending_collections('123')[0]['busy'] is True
            with pytest.raises(DubForgeError):
                service.create_collection('123', body)
            with pytest.raises(DubForgeError, match='仍在执行'):
                service.resolve_collection('123', body['request_id'], {'confirmed':True,'season_id':None})
        finally:
            release.set()
        assert task.result()['season_id'] == 10


def test_collection_api_rejects_cross_site_and_returns_only_receipt(collection_env):
    from app.main import app
    body, calls = collection_env
    client = TestClient(app)
    path = '/api/publishing/accounts/123/collections'
    assert client.post(path, json=body, headers={'sec-fetch-site':'cross-site'}).status_code >= 400
    assert calls == []
    result = client.post(path, json=body)
    assert result.status_code == 200 and result.json()['season_id'] == 10
    assert 'credential' not in result.text and 'cover_path' not in result.text
