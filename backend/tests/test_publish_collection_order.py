"""合集顺序接口只使用完整确认快照，隔离账号、数据库和真实平台请求。"""
import copy
import hashlib
import json
import threading

import pytest
from sqlmodel import Session, select

from dubforge.db import get_engine
from dubforge.publishing import credentials, service, transport
from dubforge.publishing.models import Publication, PublishAccount


def snapshot(aids=(101,)):
    value = dict(section=dict(id=17, season_id=9, title='正片', type=1), episodes=[
        dict(id=aid + 1000, aid=aid, cid=aid + 2000, bvid='', title=f'视频{aid}', order=i + 1)
        for i, aid in enumerate(aids)])
    value['fingerprint'] = hashlib.sha256(json.dumps(value, ensure_ascii=False,
        separators=(',', ':')).encode()).hexdigest()
    return value


@pytest.fixture()
def collection_order_env(isolated_env, monkeypatch):
    with Session(get_engine()) as session:
        session.add(PublishAccount(id='123', name='测试账号', credential='encrypted-fixture'))
        session.commit()
    monkeypatch.setattr(credentials, 'unprotect', lambda value: {'fixture': 'private-fixture'})
    calls = []
    def remote(operation, payload=None, **kwargs):
        assert kwargs['credential'] == {'fixture': 'private-fixture'}
        assert payload['account_id'] == '123'
        assert (payload['season_id'], payload['section_id']) == (9, 17)
        calls.append((operation, copy.deepcopy(payload), kwargs['timeout']))
        if operation == 'collection_order_read':
            return snapshot()
        assert operation == 'collection_order_apply'
        return dict(**snapshot((101, 202, 303)), verified=True, changed=True)
    monkeypatch.setattr(transport, 'call', remote)
    return calls


def test_collection_read_is_complete_safe_and_does_not_create_publication(collection_order_env):
    assert service.read_collection_order('123', 9, 17) == snapshot()
    assert collection_order_env == [('collection_order_read', dict(account_id='123', season_id=9, section_id=17), 60)]
    with Session(get_engine()) as session:
        assert session.exec(select(Publication)).all() == []


def test_collection_read_passes_cancel_to_worker(collection_order_env, monkeypatch):
    stopped = threading.Event()
    calls = []
    def remote(operation, payload, **kwargs):
        assert operation == 'collection_order_read'
        assert kwargs['cancel'] is stopped and kwargs['timeout'] == 60
        calls.append(operation)
        stopped.set()
        raise transport.TransportError('操作已暂停')
    monkeypatch.setattr(transport, 'call', remote)
    with pytest.raises(transport.TransportError, match='操作已暂停'):
        service.read_collection_order('123', 9, 17, cancel=stopped)
    assert calls == ['collection_order_read']


def test_order_apply_freezes_snapshot_and_sends_once(collection_order_env):
    baseline = snapshot()
    result = service.apply_collection_order('123', 9, 17, order=[202, 303], snapshot=baseline, confirmed=True)
    assert result == dict(**snapshot((101, 202, 303)), verified=True, changed=True)
    assert collection_order_env == [('collection_order_apply', dict(account_id='123', season_id=9,
        section_id=17, order=[202, 303], snapshot=baseline, confirmed=True), 120)]
    assert baseline == snapshot()


def test_cancel_before_worker_never_sends_sort(collection_order_env):
    stopped = threading.Event()
    stopped.set()
    with pytest.raises(service.PublishError) as exc:
        service.apply_collection_order('123', 9, 17, order=[202], snapshot=snapshot(), confirmed=True, cancel=stopped)
    assert exc.value.code == 'PUBLISH_COLLECTION_ORDER_NOT_SENT'
    assert collection_order_env == []


def test_cancel_in_worker_keeps_unknown_and_does_not_retry(collection_order_env, monkeypatch):
    stopped = threading.Event()
    calls = []
    def interrupted(operation, payload, **kwargs):
        assert kwargs['cancel'] is stopped
        calls.append(operation)
        stopped.set()
        raise transport.TransportError('操作已暂停')
    monkeypatch.setattr(transport, 'call', interrupted)
    with pytest.raises(service.PublishError) as exc:
        service.apply_collection_order('123', 9, 17, order=[202], snapshot=snapshot(), confirmed=True, cancel=stopped)
    assert exc.value.code == 'PUBLISH_COLLECTION_ORDER_UNKNOWN'
    assert calls == ['collection_order_apply']


@pytest.mark.parametrize('order,confirmed', [([], True), ([202, 202], True), ([True], True),
    ([202.0], True), ([0], True), ([202], False), ([202], 1)])
def test_order_apply_requires_valid_explicit_confirmation(collection_order_env, order, confirmed):
    with pytest.raises(service.PublishError) as exc:
        service.apply_collection_order('123', 9, 17, order=order, snapshot=snapshot(), confirmed=confirmed)
    assert exc.value.code == 'PUBLISH_COLLECTION_ORDER_INVALID'
    assert collection_order_env == []


@pytest.mark.parametrize('change', ['hash', 'missing_id', 'duplicate_aid', 'incorrect_order', 'missing_title'])
def test_invalid_snapshot_never_starts_write(collection_order_env, change):
    value = snapshot()
    if change == 'hash': value['fingerprint'] = 'a' * 64
    elif change == 'missing_id': del value['episodes'][0]['id']
    elif change == 'duplicate_aid': value['episodes'].append(value['episodes'][0])
    elif change == 'incorrect_order': value['episodes'][0]['order'] = 2
    else: del value['section']['title']
    with pytest.raises(service.PublishError) as exc:
        service.apply_collection_order('123', 9, 17, order=[202], snapshot=value, confirmed=True)
    assert exc.value.code == 'PUBLISH_COLLECTION_ORDER_RESPONSE'
    assert collection_order_env == []


@pytest.mark.parametrize('code,expected', [('PUBLISH_TRANSPORT', 'PUBLISH_COLLECTION_ORDER_UNKNOWN'),
    ('PUBLISH_REMOTE_ERROR', 'PUBLISH_COLLECTION_ORDER_UNKNOWN'),
    ('PUBLISH_COLLECTION_ORDER_CHANGED', 'PUBLISH_COLLECTION_ORDER_CHANGED'),
    ('PUBLISH_COLLECTION_ORDER_REJECTED', 'PUBLISH_COLLECTION_ORDER_REJECTED'),
    ('PUBLISH_COLLECTION_ORDER_UNKNOWN', 'PUBLISH_COLLECTION_ORDER_UNKNOWN')])
def test_write_error_is_never_retried(collection_order_env, monkeypatch, code, expected):
    calls = []
    def fail(*args, **kwargs):
        calls.append(args[0])
        raise transport.TransportError('安全提示', code=code)
    monkeypatch.setattr(transport, 'call', fail)
    with pytest.raises((service.PublishError, transport.TransportError)) as exc:
        service.apply_collection_order('123', 9, 17, order=[202], snapshot=snapshot(), confirmed=True)
    assert exc.value.code == expected
    assert calls == ['collection_order_apply']


@pytest.mark.parametrize('result', [None, {}, {'verified': False, 'changed': True},
    dict(**snapshot((101, 202)), verified=True, changed=1),
    {**snapshot((101, 202)), 'verified': True, 'changed': True, 'fingerprint': 'a' * 64}])
def test_incomplete_write_receipt_remains_unknown(collection_order_env, monkeypatch, result):
    monkeypatch.setattr(transport, 'call', lambda *a, **k: result)
    with pytest.raises(service.PublishError) as exc:
        service.apply_collection_order('123', 9, 17, order=[202], snapshot=snapshot(), confirmed=True)
    assert exc.value.code == 'PUBLISH_COLLECTION_ORDER_UNKNOWN'


@pytest.mark.parametrize('args', [('123', True, 17), ('123', 9, 0), ('123', 9, 17.0), ('bad', 9, 17)])
def test_bad_scope_does_not_launch_worker(collection_order_env, args):
    with pytest.raises(service.PublishError) as exc:
        service.read_collection_order(*args)
    assert exc.value.code == 'PUBLISH_COLLECTION_ORDER_INVALID'
    assert collection_order_env == []


@pytest.mark.parametrize('verified', [True, False])
def test_unknown_write_verify_only_reads_and_preserves_matched_status(collection_order_env, monkeypatch, verified):
    calls = []
    def remote(operation, payload, **kwargs):
        calls.append((operation, payload))
        assert kwargs['timeout'] == 60
        assert 'confirmed' not in payload
        return dict(**snapshot((101, 202, 303)), verified=verified)
    monkeypatch.setattr(transport, 'call', remote)
    original = snapshot((303, 101, 202))
    assert service.verify_collection_order('123', 9, 17, order=[202, 303], snapshot=original) == dict(
        **snapshot((101, 202, 303)), verified=verified)
    assert calls == [('collection_order_verify', dict(account_id='123', season_id=9, section_id=17,
        order=[202, 303], snapshot=original))]
