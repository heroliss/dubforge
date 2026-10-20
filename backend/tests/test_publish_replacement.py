"""原BV换片只使用隔离账号和伪上传回执，验证完整清单与未知结果的只读核对。"""
import copy
import json
from types import SimpleNamespace

import pytest
from sqlmodel import Session

from dubforge import db
from dubforge.errors import DubForgeError
from dubforge.publishing import credentials, studio, transport
from dubforge.publishing import service
from dubforge.publishing.models import PublishAccount, PublishChange

BV = 'BV1234567890'
REPLACEMENTS = [dict(old_cid=1, cid=3, filename='new-original-file', title='新版原声')]


@pytest.fixture
def env(isolated_env, monkeypatch):
    with Session(db.get_engine()) as session:
        session.add(PublishAccount(id='123', name='隔离账号', credential='fixture'))
        session.add(PublishAccount(id='456', name='其他账号', credential='fixture'))
        session.commit()
    monkeypatch.setattr(credentials, 'unprotect', lambda _: {'fixture': True})
    detail = dict(bvid=BV, aid=10, mid='123', version='v1', edit_supported=True,
                  settings=dict(title='原标题', description='旧简介', tags=['知识'], dynamic='',
                                copyright=3, source='原视频来源', tid=21, human_type2=1001, declaration_id=1),
                  videos=[dict(cid=1, filename='original-file', title='原声'),
                          dict(cid=2, filename='dub-file', title='配音')])
    calls = []
    state = SimpleNamespace(detail=detail, calls=calls, verified=True, apply=True, failure=None)

    def remote(operation, payload, **kwargs):
        calls.append((operation, copy.deepcopy(payload)))
        if operation == 'studio_detail':
            return copy.deepcopy(detail)
        if operation == 'metadata':
            return dict(declarations=[dict(value=1, label='含AI生成内容')])
        if operation == 'studio_write':
            if state.failure:
                raise state.failure
            if state.apply:
                detail['videos'] = copy.deepcopy(payload['videos'])
                detail['settings'].update(payload['values'])
            return dict(bvid=BV, aid=10, accepted=True, verified=state.verified)
        pytest.fail(f'不允许的网络调用：{operation}')

    monkeypatch.setattr(transport, 'call', remote)
    return state


def prepare(values=None, **kwargs):
    return studio.prepare_replacement('123', BV, copy.deepcopy(REPLACEMENTS), values, **kwargs)


def frozen(key):
    with Session(db.get_engine()) as session:
        return json.loads(session.get(PublishChange, key).payload)


def test_replacement_freezes_complete_order_preserves_unmodified_part_and_hides_upload_ids(env):
    view = prepare({'description': '新简介'}, expected_version='v1')
    assert view['kind'] == 'replace' and view['publication_id'] == ''
    assert 'filename' not in json.dumps(view)
    assert [p['cid'] for p in view['preview']['before']['videos']] == [1, 2]
    assert [p['cid'] for p in view['preview']['after']['videos']] == [3, 2]
    payload = frozen(view['id'])
    assert payload['bound_aid'] == 10 and payload['version'] == 'v1'
    assert payload['videos'] == [dict(cid=3, filename='new-original-file', title='新版原声'),
                                 dict(cid=2, filename='dub-file', title='配音')]
    assert payload['replacements'] == REPLACEMENTS
    assert view['preview']['after']['settings'] == {**env.detail['settings'], 'description': '新简介'}
    assert [op for op, _ in env.calls] == ['studio_detail']


@pytest.mark.parametrize('replacements', [[], {},
    [dict(old_cid=True, cid=3, filename='x', title='新')],
    [dict(old_cid=1, cid=0, filename='x', title='新')],
    [dict(old_cid=1, cid=3, filename='', title='新')],
    [dict(old_cid=1, cid=3, filename='x', title='')],
    [dict(old_cid=1, cid=3, filename='x\n', title='新')],
    [dict(old_cid=1, cid=3, filename='x', title='新', untrusted='x')],
    REPLACEMENTS * 2,
    [REPLACEMENTS[0], dict(old_cid=2, cid=3, filename='different', title='重复CID')],
    [REPLACEMENTS[0], dict(old_cid=2, cid=4, filename='new-original-file', title='重复文件')]])
def test_malformed_replacement_never_builds_a_plan_or_writes(env, replacements):
    with pytest.raises(DubForgeError):
        studio.prepare_replacement('123', BV, replacements)
    assert not studio.changes('123')
    assert not any(op == 'studio_write' for op, _ in env.calls)


@pytest.mark.parametrize('change', [dict(old_cid=99), dict(cid=2), dict(cid=1), dict(filename='dub-file')])
def test_replacement_rejects_missing_old_part_and_remote_file_or_cid_conflict(env, change):
    with pytest.raises(DubForgeError) as exc:
        studio.prepare_replacement('123', BV, [{**REPLACEMENTS[0], **change}])
    assert exc.value.code == 'PUBLISH_REMOTE_CHANGED'
    assert not studio.changes('123')


@pytest.mark.parametrize('change', [dict(mid='456'), dict(bvid='BVabcdefghij'), dict(version=''),
    dict(aid=0), dict(videos=[]), dict(videos=[dict(cid=1, filename='', title='原声')]),
    dict(videos=[dict(cid=1, filename='one', title='原声'), dict(cid=1, filename='two', title='配音')])])
def test_missing_remote_identity_and_incomplete_parts_fail_closed(env, change):
    env.detail.update(change)
    with pytest.raises(DubForgeError):
        prepare()
    assert not studio.changes('123')


def test_expected_version_and_special_archive_block_before_storing(env):
    with pytest.raises(DubForgeError) as exc:
        prepare(expected_version='old-version')
    assert exc.value.code == 'PUBLISH_REMOTE_CHANGED'
    env.detail['edit_supported'] = False
    with pytest.raises(DubForgeError) as exc:
        prepare()
    assert exc.value.code == 'PUBLISH_EDIT_UNSUPPORTED'
    assert not studio.changes('123')


@pytest.mark.parametrize('values', [dict(copyright=0), dict(season_id=4), dict(visibility='public'),
    dict(title=''), dict(tags=[]), dict(declaration_id=99)])
def test_replacement_cannot_change_unrelated_or_invalid_settings(env, values):
    with pytest.raises(DubForgeError):
        prepare(values)
    assert not any(op == 'studio_write' for op, _ in env.calls)


def test_no_public_prepare_endpoint_can_supply_arbitrary_uploaded_videos(env):
    with pytest.raises(DubForgeError):
        studio.prepare('123', dict(bvid=BV, kind='replace', videos=REPLACEMENTS))
    assert not studio.changes('123')


def test_verified_replacement_keeps_original_bv_and_commit_is_idempotent(env):
    view = prepare({'description': '新简介'})
    result = studio.commit('123', view['id'], {'confirmed': True})
    assert result['status'] == 'done'
    assert result['result'] == dict(bvid=BV, aid=10, accepted=True, verified=True)
    assert studio.commit('123', view['id'], {'confirmed': True})['status'] == 'done'
    writes = [p for op, p in env.calls if op == 'studio_write']
    assert len(writes) == 1 and writes[0]['kind'] == 'replace'
    assert [p['cid'] for p in env.detail['videos']] == [3, 2]


def test_accepted_without_readback_stays_unknown_and_reconcile_checks_filename_not_just_cid(env):
    view = prepare({'description': '新简介'})
    env.verified = False
    with pytest.raises(DubForgeError) as exc:
        studio.commit('123', view['id'], {'confirmed': True})
    assert exc.value.code == 'PUBLISH_WRITE_UNKNOWN'
    assert studio.changes('123')[0]['status'] == 'unknown'
    with pytest.raises(DubForgeError):
        prepare()
    with pytest.raises(DubForgeError):
        studio.commit('123', view['id'], {'confirmed': True})
    env.detail['videos'][0]['filename'] = 'same-cid-wrong-file'
    assert studio.reconcile('123', view['id'], {})['status'] == 'unknown'
    env.detail['videos'][0]['filename'] = 'new-original-file'
    assert studio.reconcile('123', view['id'], {})['status'] == 'done'
    assert len([1 for op, _ in env.calls if op == 'studio_write']) == 1


def test_timeout_persists_across_engine_reset_and_user_release_never_replays(env):
    view = prepare()
    env.failure = transport.TransportError('网络中断')
    with pytest.raises(DubForgeError):
        studio.commit('123', view['id'], {'confirmed': True})
    db.reset_engine()
    assert studio.reconcile('123', view['id'], {})['status'] == 'unknown'
    assert studio.reconcile('123', view['id'], {'confirmed_not_applied': True})['status'] == 'failed'
    replacement = prepare()
    assert replacement['id'] != view['id']
    assert len([1 for op, _ in env.calls if op == 'studio_write']) == 1


def test_wrong_aid_or_owner_cannot_resolve_unknown_replacement(env):
    view = prepare()
    env.verified = False
    with pytest.raises(DubForgeError):
        studio.commit('123', view['id'], {'confirmed': True})
    env.detail['aid'] = 20
    assert studio.reconcile('123', view['id'], {})['status'] == 'unknown'
    env.detail['aid'], env.detail['mid'] = 10, '456'
    assert studio.reconcile('123', view['id'], {})['status'] == 'unknown'
    with pytest.raises(DubForgeError):
        studio.reconcile('456', view['id'], {})


def collection_unknown(env, target):
    row = PublishChange(account_id='123', bvid=BV, kind='collection', status='unknown',
                        payload=service._json(dict(kind='collection', bvid=BV)),
                        preview=service._json(dict(before=copy.deepcopy(env.detail), after=dict(collection=target))))
    with Session(db.get_engine()) as session:
        session.add(row); session.commit(); session.refresh(row)
        return row.id


@pytest.mark.parametrize('member', [True, False])
def test_collection_reconcile_requires_exact_section_membership_and_never_sends_edit(env, monkeypatch, member):
    key = collection_unknown(env, dict(id=4, section_id=5, title='合集'))
    env.detail.update(collection=dict(id=4, title='合集'), in_season=True)
    reads = []

    def read_order(account, season_id, section_id):
        reads.append((account, season_id, section_id))
        return dict(section=dict(id=5, season_id=4),
                    episodes=[dict(aid=10 if member else 11, bvid=BV if member else '')])

    monkeypatch.setattr(service, 'read_collection_order', read_order)
    result = studio.reconcile('123', key, {})
    assert result['status'] == ('done' if member else 'unknown')
    if member:
        assert result['result']['verified'] is True
    assert reads == [('123', 4, 5)]
    assert not any(op == 'studio_write' for op, _ in env.calls)


@pytest.mark.parametrize('change', [dict(mid='456'), dict(aid=11), dict(bvid='BVabcdefghij'),
    dict(collection=dict(id=99)), dict(collection=None)])
def test_collection_reconcile_cannot_prove_with_wrong_archive_or_collection(env, monkeypatch, change):
    key = collection_unknown(env, dict(id=4, section_id=5, title='合集'))
    env.detail.update(collection=dict(id=4), in_season=True)
    env.detail.update(change)
    monkeypatch.setattr(service, 'read_collection_order', lambda *args: pytest.fail('身份不符不读取小节'))
    assert studio.reconcile('123', key, {})['status'] == 'unknown'


@pytest.mark.parametrize('collection, membership, expected', [
    (None, False, 'done'), (None, True, 'unknown'), (None, None, 'unknown'),
    ('missing', False, 'unknown'), (dict(id=4), False, 'unknown')])
def test_collection_removal_requires_explicit_readable_null_and_false(env, collection, membership, expected):
    key = collection_unknown(env, None)
    env.detail.pop('collection', None)
    env.detail.update(in_season=membership)
    if collection != 'missing':
        env.detail['collection'] = collection
    result = studio.reconcile('123', key, {})
    assert result['status'] == expected
    assert not any(op == 'studio_write' for op, _ in env.calls)


def test_collection_section_read_failure_preserves_unknown_lock(env, monkeypatch):
    key = collection_unknown(env, dict(id=4, section_id=5, title='合集'))
    env.detail.update(collection=dict(id=4), in_season=True)
    monkeypatch.setattr(service, 'read_collection_order', lambda *args: (_ for _ in ()).throw(
        service.PublishError('无法完整读取合集成员与顺序', code='PUBLISH_COLLECTION_ORDER_RESPONSE')))
    with pytest.raises(DubForgeError):
        studio.reconcile('123', key, {})
    assert studio.changes('123')[0]['status'] == 'unknown'
    assert not any(op == 'studio_write' for op, _ in env.calls)
