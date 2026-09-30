"""发布边界回归：全部隔离数据、伪账号与伪网络，绝不实际上传或投稿。"""
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.publishing import credentials, service, transport
from dubforge.publishing.models import Publication, PublishAccount


@pytest.fixture()
def setup_publish(make_project, monkeypatch):
    uid = make_project(title='河内美食')
    root = crud.project_dir(uid)
    artifacts = []
    for version in ('dub_hardsub', 'src_hardsub'):
        path = root / f'artifacts/assemble/final_{version}_20260927_120000_123456.mp4'
        path.write_bytes(b'fake-video-' + version.encode())
        artifacts.append(crud.add_artifact(crud.get_project(uid).id, 'assemble', 'video', path.name,
                                          path, str(path.relative_to(root))))
    with Session(get_engine()) as session:
        session.add(PublishAccount(id='123', name='隔离测试账号', credential='encrypted-fixture'))
        session.commit()
    monkeypatch.setattr(credentials, 'unprotect', lambda value: {'fixture': True})
    monkeypatch.setattr(service, '_probe', lambda path: 100)
    monkeypatch.setattr(transport, 'available', lambda: True)
    calls = []
    def fake_call(operation, payload=None, **kwargs):
        calls.append((operation, payload))
        if operation == 'upload':
            return dict(cid=payload['artifact_id'] + 100, filename='remote-' + str(payload['artifact_id']), title=payload['title'])
        if operation == 'cover':
            return dict(url='https://i0.hdslb.com/test.jpg')
        if operation == 'metadata':
            return dict(categories=[dict(id=21)], human_categories=[dict(id=1001, name='旅行')], declarations=[dict(value=1, label='含AI生成内容')],
                        seasons=[dict(id=1, title='越南', sections=[dict(id=2, title='默认')])])
        if operation == 'submit':
            return dict(bvid='BV1234567890', aid=222)
        if operation == 'status':
            return dict(aid=222, bvid='BV1234567890', state=0, state_desc='已通过审核',
                        videos=[dict(cid=a.id + 100) for a in artifacts])
        if operation == 'collection':
            return dict(joined=True)
        raise AssertionError(operation)
    monkeypatch.setattr(transport, 'call', fake_call)
    cover = service.store_cover(b'\x89PNG\r\n\x1a\nfixture')['cover_id']
    row = service.create(uid)
    body = dict(row['draft'], revision=row['revision'], account_id='123', cover_id=cover,
                parts=[dict(artifact_id=a.id, title=f'版本{i}') for i, a in enumerate(artifacts)],
                overrides=dict(title='河内街头美食', tid=21, human_type2=1001, copyright=3, tags=['旅行'], declaration_id=1,
                               season_id=1, section_id=2))
    row = service.save(row['id'], body)
    yield uid, row, artifacts, calls
    deadline = time.monotonic() + 5
    while service._running and time.monotonic() < deadline:
        time.sleep(.01)
    assert not service._running


def finish(key):
    deadline = time.monotonic() + 5
    while key in service._running and time.monotonic() < deadline:
        time.sleep(.005)
    assert key not in service._running
    return service.view(key)


def upload(row):
    preview = service.plan(row['id'])
    service.enqueue(row['id'], dict(action='upload', revision=row['revision'], fingerprint=preview['fingerprint']))
    return finish(row['id'])


def test_publication_sources_keep_frozen_template_identity_and_restore_inheritance(setup_publish):
    from dubforge import configuration as cfg
    uid, row, _, _ = setup_publish
    template = cfg.create_template('本稿件')
    cfg.save(template['key'], {'revision': template['revision'], 'updates': {'publish.watermark': True}})
    plan = cfg.binding_plan([uid], template['key'][7:])
    cfg.bind(dict(uids=[uid], template_id=template['key'][7:], fingerprint=plan['fingerprint'], confirmed=True))
    row = service.view(row['id'])
    assert row['sources']['watermark'] == {'kind': 'series', 'name': '本稿件'}
    assert row['sources']['title'] == {'kind': 'publication'}
    assert row['sources']['close_comments'] == {'kind': 'global'}
    row = upload(row)
    assert row['snapshot']['base_sources']['watermark'] == {'kind': 'series', 'name': '本稿件'}
    current = cfg.resolve(key=template['key'])
    cfg.save(template['key'], {'revision': current['revision'], 'updates': {}, 'name': '后来改名'})
    assert service.view(row['id'])['sources']['watermark']['name'] == '本稿件'
    row = service.save(row['id'], dict(row['draft'], revision=row['revision'], overrides={**row['draft']['overrides'], 'watermark': False}))
    assert row['sources']['watermark'] == {'kind': 'publication'}
    overrides = dict(row['draft']['overrides'])
    overrides.pop('watermark')
    row = service.save(row['id'], dict(row['draft'], revision=row['revision'], overrides=overrides))
    assert row['settings']['watermark'] is True
    assert row['sources']['watermark'] == {'kind': 'series', 'name': '本稿件'}


def publish(row):
    preview = service.plan(row['id'])
    service.enqueue(row['id'], dict(action='publish', confirmed=True, revision=row['revision'],
                                    fingerprint=preview['fingerprint']))
    return finish(row['id'])


def test_preflight_and_draft_copy_need_no_rights_checkbox(setup_publish):
    _, row, _, calls = setup_publish
    assert 'rights_confirmed' not in row['draft']
    assert service.plan(row['id'])['snapshot']['parts']
    copied = service.copy_draft(row['id'])
    assert 'rights_confirmed' not in copied['draft']
    assert copied['status'] == 'draft'
    assert calls == []


def test_existing_unchecked_draft_is_not_blocked_and_uploaded_metadata_can_be_saved(setup_publish):
    _, row, _, calls = setup_publish
    # 历史草稿可能仍带旧字段；它不再决定能否预检或修改资料。
    with Session(get_engine()) as session:
        stored = session.get(Publication, row['id'])
        stored.draft = json.dumps({**row['draft'], 'rights_confirmed': False})
        session.add(stored)
        session.commit()
    assert service.plan(row['id'])['snapshot']['parts']
    assert calls == []
    row = upload(row)
    row = service.save(row['id'], dict(row['draft'], revision=row['revision'],
        overrides={**row['draft']['overrides'], 'title': '修改后的标题'}))
    assert row['status'] == 'uploaded'
    assert row['snapshot']['settings']['title'] == '修改后的标题'
    assert not any(op == 'submit' for op, _ in calls)


@pytest.mark.parametrize('confirmation', [None, False, 1, 'true'])
def test_upload_and_submit_requires_explicit_confirmation(setup_publish, confirmation):
    _, row, _, calls = setup_publish
    preview = service.plan(row['id'])
    with pytest.raises(DubForgeError, match='确认'):
        service.enqueue(row['id'], dict(action='publish', revision=row['revision'],
            fingerprint=preview['fingerprint'], confirmed=confirmation))
    assert service.view(row['id'])['status'] == 'draft'
    assert calls == []


def test_upload_and_submit_preserves_settings_and_never_unlocks_between_stages(setup_publish, monkeypatch):
    _, row, arts, calls = setup_publish
    scheduled = int(time.time()) + 3600
    row = service.save(row['id'], dict(row['draft'], revision=row['revision'], overrides={
        **row['draft']['overrides'], 'visibility': 'self', 'publish_at': scheduled}))
    original = transport.call
    states = []
    def observe(op, payload=None, **kwargs):
        state = service.view(row['id'])
        states.append(state['status'])
        assert state['snapshot']['publish_after_upload'] is True
        assert state['status'] != 'uploaded'
        if op == 'submit':
            assert payload['confirmed'] is True
            assert payload['settings']['visibility'] == 'self'
            assert payload['settings']['publish_at'] == scheduled
            assert [v['cid'] for v in payload['videos']] == [a.id + 100 for a in arts]
        return original(op, payload, **kwargs)
    monkeypatch.setattr(transport, 'call', observe)
    result = publish(row)
    assert result['status'] == 'submitted'
    assert result['receipt']['collection_joined'] is True
    assert [op for op, _ in calls] == ['metadata', 'upload', 'upload', 'cover', 'metadata', 'submit', 'status', 'collection']
    assert 'submitting' in states
    for action in ('publish', 'submit'):
        with pytest.raises(DubForgeError):
            service.enqueue(row['id'], dict(action=action, revision=result['revision'], confirmed=True))
    assert sum(op == 'submit' for op, _ in calls) == 1


def test_upload_and_submit_rechecks_deadline_after_upload(setup_publish, monkeypatch):
    _, row, _, calls = setup_publish
    clock = [time.time()]
    row = service.save(row['id'], dict(row['draft'], revision=row['revision'], overrides={
        **row['draft']['overrides'], 'publish_at': int(clock[0]) + 3600}))
    monkeypatch.setattr(service.time, 'time', lambda: clock[0])
    original = transport.call
    def slow_upload(op, payload=None, **kwargs):
        result = original(op, payload, **kwargs)
        if op == 'cover':
            clock[0] += 3600
        return result
    monkeypatch.setattr(transport, 'call', slow_upload)
    result = publish(row)
    assert result['status'] == 'uploaded'
    assert '不会自动改成立即发布' in result['error']
    assert not any(op == 'submit' for op, _ in calls)
    # 文件不用重传，允许用户修正时间后重新确认。
    saved = service.save(row['id'], dict(result['draft'], revision=result['revision'], overrides={
        **result['draft']['overrides'], 'publish_at': None}))
    assert saved['snapshot']['settings']['publish_at'] is None


def test_upload_and_submit_pause_before_submission_keeps_files_without_posting(setup_publish, monkeypatch):
    _, row, _, calls = setup_publish
    original = transport.call
    reads = 0
    def pause_before_submit(op, payload=None, **kwargs):
        nonlocal reads
        result = original(op, payload, **kwargs)
        if op == 'metadata':
            reads += 1
            if reads == 2:
                service.pause(row['id'])
        return result
    monkeypatch.setattr(transport, 'call', pause_before_submit)
    result = publish(row)
    assert result['status'] == 'uploaded'
    assert '已停止投稿' in result['error']
    assert not any(op == 'submit' for op, _ in calls)


def test_failed_combined_upload_can_resume_without_automatic_submission(setup_publish, monkeypatch):
    _, row, _, calls = setup_publish
    original = transport.call
    attempts = 0
    def interrupted(op, payload=None, **kwargs):
        nonlocal attempts
        if op == 'upload':
            attempts += 1
            if attempts == 2:
                raise transport.TransportError('模拟断网')
        return original(op, payload, **kwargs)
    monkeypatch.setattr(transport, 'call', interrupted)
    result = publish(row)
    assert result['status'] == 'failed'
    assert not any(op == 'submit' for op, _ in calls)
    result = upload(result)
    assert result['status'] == 'uploaded'
    assert result['snapshot']['publish_after_upload'] is False
    assert sum(op == 'upload' for op, _ in calls) == 2
    assert not any(op == 'submit' for op, _ in calls)


def test_combined_submission_timeout_is_unknown_and_cannot_repeat(setup_publish, monkeypatch):
    _, row, _, calls = setup_publish
    original = transport.call
    def timeout(op, payload=None, **kwargs):
        result = original(op, payload, **kwargs)
        if op == 'submit':
            raise transport.TransportError('投稿回执超时')
        return result
    monkeypatch.setattr(transport, 'call', timeout)
    result = publish(row)
    assert result['status'] == 'unknown'
    for action in ('publish', 'upload', 'submit'):
        with pytest.raises(DubForgeError):
            service.enqueue(row['id'], dict(action=action, confirmed=True, revision=result['revision']))
    with pytest.raises(DubForgeError):
        service.copy_draft(row['id'])
    assert sum(op == 'submit' for op, _ in calls) == 1


@pytest.mark.parametrize('ai_part', [True, False])
def test_ai_declaration_is_user_choice_and_never_forced_before_transfer(setup_publish, monkeypatch, ai_part):
    _, row, arts, calls = setup_publish
    parts = row['draft']['parts'] if ai_part else [row['draft']['parts'][1]]
    row = service.save(row['id'], dict(row['draft'], revision=row['revision'], parts=parts,
        overrides={**row['draft']['overrides'], 'declaration_id': -1}))
    original = transport.call
    def no_ai_statement(op, payload=None, **kwargs):
        result = original(op, payload, **kwargs)
        if op == 'metadata':
            result['declarations'].append(dict(value=-1, label='内容无需标注'))
        return result
    monkeypatch.setattr(transport, 'call', no_ai_statement)
    result = publish(row)
    assert result['status'] == 'submitted'
    assert [p['artifact_id'] for op, p in calls if op == 'upload'] == [a.id for a in (arts if ai_part else arts[1:])]
    assert next(p['settings']['declaration_id'] for op, p in calls if op == 'submit') == -1
    assert result['draft']['overrides']['declaration_id'] == -1


def test_ai_notice_acknowledgement_is_bound_to_snapshot_and_survives_upload(setup_publish):
    _, row, _, calls = setup_publish
    preview = service.plan(row['id'])
    assert preview['ai_declaration_acknowledged'] is False
    service.enqueue(row['id'], dict(action='upload', revision=preview['revision'],
        fingerprint=preview['fingerprint'], acknowledge_ai_notice=True))
    result = finish(row['id'])
    assert result['status'] == 'uploaded'
    assert service.plan(row['id'])['ai_declaration_acknowledged'] is True
    assert not any(op == 'submit' for op, _ in calls)
    # 查询、重启以及原样保存不重复打扰；实际资料变化后旧确认自然失效。
    service.recover()
    result = service.save(row['id'], dict(result['draft'], revision=result['revision']))
    assert service.plan(row['id'])['ai_declaration_acknowledged'] is True
    result = service.save(row['id'], dict(result['draft'], revision=result['revision'], overrides={
        **result['draft']['overrides'], 'description': '更新的简介'}))
    assert service.plan(row['id'])['ai_declaration_acknowledged'] is False
    copied = service.copy_draft(row['id'])
    assert not copied['snapshot']


def test_ai_notice_does_not_repeat_when_resuming_same_files(setup_publish, monkeypatch):
    _, row, _, _ = setup_publish
    original = transport.call
    attempts = 0
    def interrupted(op, payload=None, **kwargs):
        nonlocal attempts
        if op == 'upload':
            attempts += 1
            if attempts == 2:
                raise transport.TransportError('测试上传中断')
        return original(op, payload, **kwargs)
    monkeypatch.setattr(transport, 'call', interrupted)
    preview = service.plan(row['id'])
    service.enqueue(row['id'], dict(action='publish', confirmed=True, revision=preview['revision'],
        fingerprint=preview['fingerprint'], acknowledge_ai_notice=True))
    result = finish(row['id'])
    assert result['status'] == 'failed'
    assert service.plan(row['id'])['ai_declaration_acknowledged'] is True
    result = upload(result)
    assert result['status'] == 'uploaded'
    assert service.plan(row['id'])['ai_declaration_acknowledged'] is True


@pytest.mark.parametrize('field,value', [('account_id', '456'), ('settings', {'declaration_id': -1}),
                                       ('parts', [{'artifact_id': 999}]), ('cover_sha256', 'changed')])
def test_ai_notice_scope_changes_with_relevant_material(setup_publish, field, value):
    _, row, _, _ = setup_publish
    snapshot = service.plan(row['id'])['snapshot']
    snapshot['ai_declaration_acknowledgement'] = service._ai_notice_scope(snapshot)
    snapshot['publish_after_upload'] = True
    assert service._plan_result(snapshot, 1)['ai_declaration_acknowledged'] is True
    snapshot[field] = value
    assert service._plan_result(snapshot, 1)['ai_declaration_acknowledged'] is False


def test_submit_ai_notice_acknowledgement_requires_matching_preview(setup_publish):
    _, row, _, calls = setup_publish
    result = upload(row)
    preview = service.plan(row['id'])
    for fingerprint in (None, 'old-snapshot'):
        with pytest.raises(DubForgeError, match='变化'):
            service.enqueue(row['id'], dict(action='submit', revision=result['revision'], confirmed=True,
                acknowledge_ai_notice=True, fingerprint=fingerprint))
    assert service.plan(row['id'])['ai_declaration_acknowledged'] is False
    service.enqueue(row['id'], dict(action='submit', revision=preview['revision'], confirmed=True,
        acknowledge_ai_notice=True, fingerprint=preview['fingerprint']))
    assert finish(row['id'])['status'] == 'submitted'
    assert service.plan(row['id'])['ai_declaration_acknowledged'] is True
    assert sum(op == 'submit' for op, _ in calls) == 1


@pytest.mark.parametrize('value', [1, 'true', None, []])
def test_ai_notice_confirmation_is_typed_not_a_new_declaration_override(setup_publish, value):
    _, row, _, calls = setup_publish
    preview = service.plan(row['id'])
    with pytest.raises(DubForgeError, match='确认参数'):
        service.enqueue(row['id'], dict(action='upload', revision=preview['revision'],
            fingerprint=preview['fingerprint'], acknowledge_ai_notice=value))
    assert service.plan(row['id'])['ai_declaration_acknowledged'] is False
    assert not calls


def test_draft_empty_and_no_side_effect(make_project, monkeypatch):
    monkeypatch.setattr(transport, 'call', lambda *a, **k: pytest.fail('草稿不能请求B站'))
    uid = make_project()
    before = [(s.step_key, s.status, s.input_fingerprint) for s in crud.get_steps(crud.get_project(uid).id)]
    row = service.create(uid)
    assert row['draft']['parts'] == []
    service.save(row['id'], dict(row['draft'], revision=1, overrides={'title': '只改标题'}))
    assert before == [(s.step_key, s.status, s.input_fingerprint) for s in crud.get_steps(crud.get_project(uid).id)]


def test_upload_does_not_submit_and_preserves_part_order(setup_publish):
    _, row, arts, calls = setup_publish
    row = service.save(row['id'], dict(row['draft'], revision=row['revision'], parts=list(reversed(row['draft']['parts']))))
    result = upload(row)
    assert result['status'] == 'uploaded'
    assert result['progress'] == 100
    assert [p['artifact_id'] for op, p in calls if op == 'upload'] == [a.id for a in reversed(arts)]
    assert not any(op == 'submit' for op, _ in calls)
    with pytest.raises(DubForgeError):
        service.save(row['id'], dict(row['draft'], revision=row['revision'], parts=[]))


def test_submit_requires_confirmation_and_one_receipt(setup_publish):
    _, row, _, calls = setup_publish
    row = upload(row)
    with pytest.raises(DubForgeError):
        service.enqueue(row['id'], dict(action='submit', revision=row['revision']))
    service.enqueue(row['id'], dict(action='submit', revision=row['revision'], confirmed=True))
    row = finish(row['id'])
    assert row['status'] == 'submitted'
    assert row['receipt']['collection_joined'] is True
    with pytest.raises(DubForgeError):
        service.enqueue(row['id'], dict(action='submit', revision=row['revision'], confirmed=True))
    assert sum(op == 'submit' for op, _ in calls) == 1


def test_empty_foreign_and_duplicate_parts_rejected(setup_publish, make_project):
    _, row, arts, _ = setup_publish
    with pytest.raises(DubForgeError):
        service.save(row['id'], dict(row['draft'], revision=row['revision'], parts=[row['draft']['parts'][0]] * 2))
    other = service.create(make_project())
    with pytest.raises(DubForgeError):
        service.save(other['id'], dict(other['draft'], revision=1, parts=[dict(artifact_id=arts[0].id, title='越界')]))
    row = service.save(row['id'], dict(row['draft'], revision=row['revision'], parts=[]))
    with pytest.raises(DubForgeError, match='至少一个'):
        service.plan(row['id'])


def test_inheritance_false_and_stale_confirmation(setup_publish):
    uid, row, _, _ = setup_publish
    from test_configuration import save
    save('global', {'publish.watermark': True, 'publish.close_comments': True})
    save('project:'+uid, {'publish.watermark':False})
    result = service.view(row['id'])
    assert result['settings']['watermark'] is False
    assert result['origins']['watermark'] == '本项目'
    preview = service.plan(row['id'])
    save('project:'+uid, {'publish.watermark':True})
    with pytest.raises(DubForgeError, match='变化'):
        service.enqueue(row['id'], dict(action='upload', revision=row['revision'], fingerprint=preview['fingerprint']))


def test_freeze_survives_template_change(setup_publish):
    _, row, _, _ = setup_publish
    row = upload(row)
    from test_configuration import save
    save('global', {'publish.visibility':'self'})
    assert service.view(row['id'])['snapshot']['settings']['visibility'] == 'public'


def test_metadata_mutation_never_invalidates_pipeline(setup_publish):
    uid, row, _, _ = setup_publish
    steps = crud.get_steps(crud.get_project(uid).id)
    for step in steps:
        step.status, step.input_fingerprint = 'done', 'stable'
        crud.save_step(step)
    service.save(row['id'], dict(row['draft'], revision=row['revision'], overrides={'title': '新发布标题'}))
    assert all(s.status == 'done' and s.input_fingerprint == 'stable' for s in crud.get_steps(crud.get_project(uid).id))


def test_changed_file_blocked_before_transfer(setup_publish):
    uid, row, arts, calls = setup_publish
    (crud.project_dir(uid) / arts[0].rel_path).write_bytes(b'changed')
    with pytest.raises(DubForgeError, match='改变'):
        service.plan(row['id'])
    assert calls == []


def test_resume_reuses_only_completed_parts(setup_publish, monkeypatch):
    _, row, _, calls = setup_publish
    original = transport.call
    seen = 0
    def flaky(op, payload=None, **kwargs):
        nonlocal seen
        if op == 'upload':
            seen += 1
            if seen == 2:
                raise transport.TransportError('网络中断')
        return original(op, payload, **kwargs)
    monkeypatch.setattr(transport, 'call', flaky)
    row = upload(row)
    assert row['status'] == 'failed'
    row = upload(row)
    assert row['status'] == 'uploaded'
    assert sum(op == 'upload' for op, _ in calls) == 2


def test_submit_timeout_unknown_cannot_repeat_or_copy(setup_publish, monkeypatch):
    _, row, _, calls = setup_publish
    row = upload(row)
    original = transport.call
    def timeout(op, payload=None, **kwargs):
        if op == 'submit':
            raise transport.TransportError('网络响应丢失')
        return original(op, payload, **kwargs)
    monkeypatch.setattr(transport, 'call', timeout)
    service.enqueue(row['id'], dict(action='submit', revision=row['revision'], confirmed=True))
    assert finish(row['id'])['status'] == 'unknown'
    with pytest.raises(DubForgeError):
        service.enqueue(row['id'], dict(action='submit', revision=row['revision'], confirmed=True))
    with pytest.raises(DubForgeError):
        service.copy_draft(row['id'])
    result = service.attach_receipt(row['id'], dict(bvid='BV1234567890'))
    assert result['status'] == 'submitted'


def test_collection_failure_does_not_repeat_submission(setup_publish, monkeypatch):
    _, row, _, calls = setup_publish
    row = upload(row)
    original = transport.call
    def fail(op, payload=None, **kwargs):
        if op == 'collection':
            raise transport.TransportError('合集等待审核')
        return original(op, payload, **kwargs)
    monkeypatch.setattr(transport, 'call', fail)
    service.enqueue(row['id'], dict(action='submit', revision=row['revision'], confirmed=True))
    row = finish(row['id'])
    assert row['status'] == 'collection_pending' and row['receipt']['bvid']
    assert row['error'].startswith('视频已投稿，加入合集未完成')
    assert '无需重新上传或投稿' in row['error']
    monkeypatch.setattr(transport, 'call', original)
    service.enqueue(row['id'], dict(action='collection', revision=row['revision']))
    assert finish(row['id'])['receipt']['collection_joined']
    assert sum(op == 'submit' for op, _ in calls) == 1


@pytest.mark.parametrize('response', [{}, None, {'joined': False}, {'joined': 1}])
def test_collection_unconfirmed_receipt_does_not_mark_joined(setup_publish, monkeypatch, response):
    _, row, _, calls = setup_publish
    row = upload(row)
    original = transport.call
    monkeypatch.setattr(transport, 'call', lambda op, payload=None, **kw:
        response if op == 'collection' else original(op, payload, **kw))
    service.enqueue(row['id'], dict(action='submit', revision=row['revision'], confirmed=True))
    result = finish(row['id'])
    assert result['status'] == 'collection_pending'
    assert result['receipt']['bvid']
    assert not result['receipt'].get('collection_joined')
    assert '回执未确认' in result['error']
    assert sum(op == 'submit' for op, _ in calls) == 1


def test_collection_platform_reason_survives_and_retry_only_joins(setup_publish, monkeypatch):
    _, row, _, calls = setup_publish
    row = upload(row)
    original = transport.call
    message = '视频已投稿，但加入合集未完成（平台错误码 21001）：合集条件不满足。无需重新上传或投稿。'
    def refused(op, payload=None, **kwargs):
        if op == 'collection':
            raise transport.TransportError(message, code='PUBLISH_COLLECTION_JOIN_REJECTED')
        return original(op, payload, **kwargs)
    monkeypatch.setattr(transport, 'call', refused)
    service.enqueue(row['id'], dict(action='submit', revision=row['revision'], confirmed=True))
    result = finish(row['id'])
    assert result['error'] == message
    assert transport._error_detail(dict(stage='join_add',remote_code=21001,headers={'Cookie':'private'})) == dict(stage='join_add',remote_code=21001)
    calls.clear()
    monkeypatch.setattr(transport, 'call', original)
    service.enqueue(row['id'], dict(action='collection', revision=result['revision']))
    result = finish(row['id'])
    assert [op for op, _ in calls] == ['status', 'collection']
    assert result['receipt']['collection_joined'] is True
    assert result['error'] == ''


@pytest.mark.parametrize('status,expected', [('uploading','paused'), ('submitting','unknown'), ('joining','collection_pending'), ('queued','paused')])
def test_restart_never_resubmits(setup_publish, status, expected):
    _, row, _, calls = setup_publish
    service._update(row['id'], snapshot={**service.plan(row['id'])['snapshot'], 'publish_after_upload': True})
    if status == 'joining':
        service._update(row['id'], receipt=dict(bvid='BV1234567890'))
    service._update(row['id'], status=status)
    service.recover()
    assert service.view(row['id'])['status'] == expected
    assert calls == []


def test_api_hides_credentials_and_rejects_cross_site(setup_publish):
    from app.main import app
    _, row, _, _ = setup_publish
    client = TestClient(app)
    response = client.get('/api/publishing/catalog')
    assert response.status_code == 200
    assert 'encrypted-fixture' not in response.text
    assert 'credential' not in response.text
    assert response.headers['cache-control'] == 'no-store'
    response = client.post(f'/api/publishing/publications/{row["id"]}/actions',
                          json={'action': 'submit'}, headers={'Origin': 'https://evil.example'})
    assert response.status_code == 400


@pytest.mark.parametrize('values', [{'tags':['a'] * 11}, {'watermark':1}, {'unknown':True}, {'title':'x' * 81}, {'tid':-1}, {'visibility':'private'}])
def test_invalid_setting_rejected(values):
    with pytest.raises(DubForgeError):
        service.validate_values(values)


def test_missing_declaration_is_invalid_but_ai_mismatch_is_not_a_blocker(setup_publish):
    _, row, _, _ = setup_publish
    snapshot = service.plan(row['id'])['snapshot']
    with pytest.raises(DubForgeError, match='声明'):
        service._validate_submission(snapshot, dict(declarations=[]))
    service._validate_submission(snapshot, dict(declarations=[dict(value=1, label='内容无需声明')],
        human_categories=[dict(id=1001)], seasons=[dict(id=1, sections=[dict(id=2)])]))


def test_expired_schedule_never_becomes_immediate(setup_publish):
    _, row, _, _ = setup_publish
    snapshot = service.plan(row['id'])['snapshot']
    snapshot['settings']['publish_at'] = int(time.time()) - 10
    with pytest.raises(DubForgeError, match='不会自动'):
        service._validate_submission(snapshot, {})


def test_dpapi_roundtrip_no_plaintext():
    import os
    if os.name != 'nt':
        pytest.skip('Windows DPAPI 专用')
    value = {'fixture': 'unit-test-only-not-a-cookie'}
    encrypted = credentials.protect(value)
    assert value['fixture'] not in encrypted
    assert credentials.unprotect(encrypted) == value


def test_cover_path_traversal(isolated_env):
    with pytest.raises(DubForgeError):
        service._cover_path('../../config.yaml')
    with pytest.raises(DubForgeError):
        service.store_cover(b'<svg></svg>')


def test_worker_probe_no_network(isolated_env):
    if not transport.available():
        pytest.skip('发布工作进程尚未安装')
    assert transport.call('probe')['version'] == '2.15.0'


@pytest.mark.parametrize('patch', [dict(tags=[]), dict(human_type2=0), dict(declaration_id=None),
                                  dict(copyright=2), dict(close_comments=True, selected_comments=True)])
def test_invalid_metadata_before_upload(setup_publish, patch):
    _, row, _, calls = setup_publish
    row = service.save(row['id'], dict(row['draft'], revision=row['revision'], overrides={**row['draft']['overrides'], **patch}))
    with pytest.raises(DubForgeError):
        service.plan(row['id'])
    assert calls == []


def test_upload_capability_failure_returns_editable_draft(setup_publish, monkeypatch):
    _, row, _, calls = setup_publish
    original = transport.call
    monkeypatch.setattr(transport, 'call', lambda op, payload=None, **kw: dict(declarations=[]) if op == 'metadata' else original(op, payload, **kw))
    result = upload(row)
    assert result['status'] == 'draft'
    assert not result['snapshot']
    assert calls == []
    service.save(row['id'], dict(row['draft'], revision=row['revision']))


def test_uploaded_metadata_can_change_without_transferring_again(setup_publish):
    _, row, _, calls = setup_publish
    row = upload(row)
    old = row['revision']
    row = service.save(row['id'], dict(row['draft'], revision=old,
                       overrides={**row['draft']['overrides'], 'title': '确认后的新标题'}))
    assert row['snapshot']['settings']['title'] == '确认后的新标题'
    assert row['status'] == 'uploaded'
    with pytest.raises(DubForgeError, match='变化'):
        service.enqueue(row['id'], dict(action='submit', revision=old, confirmed=True))
    service.enqueue(row['id'], dict(action='submit', revision=row['revision'], confirmed=True))
    assert finish(row['id'])['status'] == 'submitted'
    assert sum(op == 'upload' for op, _ in calls) == 2
    assert next(p['settings']['title'] for op, p in calls if op == 'submit') == '确认后的新标题'


def test_private_and_pending_collection_status_remain_distinct(setup_publish):
    _, row, _, _ = setup_publish
    row = upload(row)
    row = service.save(row['id'], dict(row['draft'], revision=row['revision'],
                       overrides={**row['draft']['overrides'], 'visibility': 'self'}))
    service._update(row['id'], receipt=dict(aid=222, bvid='BV1234567890'), status='collection_pending')
    service.enqueue(row['id'], dict(action='check', revision=row['revision']))
    assert finish(row['id'])['status'] == 'collection_pending'
    service._update(row['id'], receipt=dict(aid=222, bvid='BV1234567890', collection_joined=True))
    service.enqueue(row['id'], dict(action='check', revision=row['revision']))
    assert finish(row['id'])['status'] == 'private'


def test_project_removal_uses_public_guard(setup_publish):
    uid, row, _, _ = setup_publish
    service._update(row['id'], status='queued')
    with pytest.raises(DubForgeError, match='发布任务'):
        crud.delete_project(uid)
    assert crud.get_project(uid)


def test_minus_one_declaration_supported_and_old_outputs_have_dubbing_hint(setup_publish):
    uid, _, _, _ = setup_publish
    assert service.validate_values(dict(declaration_id=-1)) == dict(declaration_id=-1)
    path = crud.project_dir(uid) / 'artifacts/assemble/final_20260927_120000.mp4'
    path.write_bytes(b'legacy')
    crud.add_artifact(crud.get_project(uid).id, 'assemble', 'video', path.name, path, str(path.relative_to(crud.project_dir(uid))))
    assert next(f for f in service.candidates(uid)['files'] if f['name'] == path.name)['has_dubbing']


def test_restart_preserves_receipt_and_forbids_reupload(setup_publish):
    _, row, _, _ = setup_publish
    row = upload(row)
    service._update(row['id'], status='queued', receipt=dict(bvid='BV1234567890', aid=222))
    service.recover()
    assert service.view(row['id'])['status'] == 'collection_pending'
    with pytest.raises(DubForgeError, match='回执'):
        service.enqueue(row['id'], dict(action='upload', revision=row['revision']))


def test_shutdown_stops_only_publish_workers():
    event = threading.Event()
    service._running['isolated-shutdown-fixture'] = event
    try:
        service.interrupt_all()
        assert event.is_set()
    finally:
        service._running.pop('isolated-shutdown-fixture')


def test_original_video_selectable_but_unrelated_file_is_not(setup_publish):
    uid, row, _, _ = setup_publish
    root = crud.project_dir(uid)
    (root / 'media').mkdir(exist_ok=True)
    source = root / 'media/video.mp4'
    source.write_bytes(b'original-video')
    project = crud.get_project(uid)
    art = crud.add_artifact(project.id, 'download', 'video', 'video.mp4', source, 'media/video.mp4')
    other = root / 'media/other.mp4'
    other.write_bytes(b'unrelated')
    wrong = crud.add_artifact(project.id, 'download', 'video', 'video.mp4', other, 'media/other.mp4')
    found = service.candidates(uid)['files']
    assert next(f for f in found if f['id'] == art.id)['is_source']
    assert wrong.id not in {f['id'] for f in found}
    row = service.save(row['id'], dict(row['draft'], revision=row['revision'], parts=[dict(artifact_id=art.id,title='原声')]))
    snapshot = service.plan(row['id'])['snapshot']
    assert snapshot['parts'][0]['path'] == str(source)
    assert snapshot['parts'][0]['has_dubbing'] is False
