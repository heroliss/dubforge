"""保留字段的受控摘要随请求冻结，未知写入不能忽略保留失败。"""
import copy
import hashlib
import json

import pytest
from sqlmodel import Session

from dubforge.db import get_engine
from dubforge.errors import DubForgeError
from dubforge.publishing import studio, transport
from dubforge.publishing.models import PublishChange
from test_publish_studio import env, BV, uploaded


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def guarded(env):
    env.detail['preservation'] = dict(version=1, no_disturbance=1, parts=[
        dict(cid=1, description_hash=digest('原声P说明')),
        dict(cid=2, description_hash=digest('配音P说明'))])
    return env


def payload(key):
    with Session(get_engine()) as session:
        return json.loads(session.get(PublishChange, key).payload)


def test_metadata_rename_order_freezes_only_hashes_and_normalizes_title(guarded):
    task = studio.prepare('123', dict(bvid=BV, kind='edit', values={'description': '新简介'},
        part_titles={'1': '  更新原声  '}, order=[2, 1]))
    frozen = payload(task['id'])
    assert frozen['part_titles'] == {'1': '更新原声'}
    assert frozen['expected_preservation'] == dict(version=1, no_disturbance=1, parts=[
        guarded.detail['preservation']['parts'][1], guarded.detail['preservation']['parts'][0]])
    assert '原声P说明' not in json.dumps(frozen, ensure_ascii=False)
    assert [part['title'] for part in frozen['videos']] == ['配音', '更新原声']


def test_replace_append_and_reorder_map_descriptions_by_original_cid(guarded):
    task = studio.prepare_replacement('123', BV, [dict(old_cid=1, cid=3, filename='new-source', title='新原声'),
        dict(old_cid=0, cid=4, filename='new-extra', title='新增P')], expected_version='version-1',
        part_titles={'1': '  原声新版  '}, order=[4, 2, 1])
    frozen = payload(task['id'])
    assert frozen['part_titles'] == {'1': '原声新版'}
    assert frozen['expected_preservation'] == dict(version=1, no_disturbance=1, parts=[
        dict(cid=4, description_hash=digest('')), guarded.detail['preservation']['parts'][1],
        dict(cid=3, description_hash=digest('原声P说明'))])


def test_legacy_append_receipt_freezes_empty_new_part_description(guarded):
    key = uploaded(guarded)
    task = studio.prepare('123', dict(bvid=BV, kind='append', publication_id=key))
    expected = payload(task['id'])['expected_preservation']
    assert expected['parts'][-1] == dict(cid=3, description_hash=digest(''))


@pytest.mark.parametrize('mismatch', ['description', 'no_disturbance', 'missing', 'malformed'])
def test_unknown_edit_cannot_clear_without_matching_preservation_evidence(guarded, monkeypatch, mismatch):
    task = studio.prepare('123', dict(bvid=BV, kind='edit', values={'description': '新简介'}, order=[2, 1]))
    expected = payload(task['id'])['expected_preservation']
    writes = []
    def unknown(operation, body, **kwargs):
        if operation == 'studio_write':
            writes.append(body)
            raise transport.TransportError('写后回读保留字段不一致', code='PUBLISH_WRITE_UNKNOWN')
        return guarded.remote(operation, body, **kwargs)
    monkeypatch.setattr(transport, 'call', unknown)
    with pytest.raises(DubForgeError):
        studio.commit('123', task['id'], {'confirmed': True})
    guarded.detail['settings']['description'] = '新简介'
    guarded.detail['videos'].reverse()
    guarded.detail['preservation'] = copy.deepcopy(expected)
    if mismatch == 'description':
        guarded.detail['preservation']['parts'][0]['description_hash'] = digest('说明丢失')
    elif mismatch == 'no_disturbance':
        guarded.detail['preservation']['no_disturbance'] = 0
    elif mismatch == 'missing':
        guarded.detail.pop('preservation')
    else:
        guarded.detail['preservation']['parts'][0]['description_hash'] = 'invalid'
    assert studio.reconcile('123', task['id'], {})['status'] == 'unknown'
    assert len(writes) == 1
    guarded.detail['preservation'] = expected
    result = studio.reconcile('123', task['id'], {})
    assert result['status'] == 'done' and result['result']['verified'] is True
    assert len(writes) == 1


def test_unknown_replace_keeps_preservation_guard_after_new_cids_are_visible(guarded, monkeypatch):
    task = studio.prepare_replacement('123', BV,
        [dict(old_cid=1, cid=3, filename='new-file', title='新原声')], expected_version='version-1')
    frozen = payload(task['id'])
    def unknown(operation, body, **kwargs):
        if operation == 'studio_write':
            raise transport.TransportError('响应丢失')
        return guarded.remote(operation, body, **kwargs)
    monkeypatch.setattr(transport, 'call', unknown)
    with pytest.raises(DubForgeError):
        studio.commit('123', task['id'], {'confirmed': True})
    guarded.detail['videos'] = frozen['videos']
    guarded.detail['preservation'] = copy.deepcopy(frozen['expected_preservation'])
    guarded.detail['preservation']['parts'][0]['description_hash'] = digest('')
    assert studio.reconcile('123', task['id'], {})['status'] == 'unknown'
    guarded.detail['preservation'] = frozen['expected_preservation']
    assert studio.reconcile('123', task['id'], {})['status'] == 'done'


def test_guarded_request_requires_verified_receipt_but_legacy_request_remains_compatible(guarded):
    task = studio.prepare('123', dict(bvid=BV, kind='edit', values={'description': '新简介'}))
    with pytest.raises(DubForgeError) as error:
        studio.commit('123', task['id'], {'confirmed': True})
    assert error.value.code == 'PUBLISH_WRITE_UNKNOWN'
    assert studio.changes('123')[0]['status'] == 'unknown'


@pytest.mark.parametrize('value', [dict(version=1, no_disturbance=1, parts=[]),
    dict(version=1, no_disturbance=True, parts=[]), dict(version=2, no_disturbance=1, parts=[])])
def test_invalid_preservation_response_is_rejected_before_any_write(guarded, value):
    guarded.detail['preservation'] = value
    with pytest.raises(DubForgeError) as error:
        studio.prepare('123', dict(bvid=BV, kind='edit', values={'description': '新简介'}))
    assert error.value.code == 'PUBLISH_RESPONSE_UNSUPPORTED'
    assert not any(operation == 'studio_write' for operation, _ in guarded.calls)


@pytest.mark.parametrize('title', ['bad\nname', '😀' * 41, ' '])
def test_title_validation_is_shared_before_request_freeze(guarded, title):
    with pytest.raises(DubForgeError):
        studio.prepare('123', dict(bvid=BV, kind='edit', values={}, part_titles={'1': title}))
    assert not any(operation == 'studio_write' for operation, _ in guarded.calls)
