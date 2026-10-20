"""批次执行只用隔离数据库、虚拟流程和合集，绝不调用真实发布接口。"""
import copy
import datetime
import json
import threading
import time
import uuid

import pytest
from sqlmodel import Session

from dubforge import release_batches as batches, workflows, runtime_edit
from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.publishing import service, studio
from dubforge.publishing.models import Publication
from dubforge.release_batch_models import ReleaseBatch
from dubforge.workflow_models import WorkflowRun
from dubforge.workflow_plan import digest


SCOPE = dict(account_id='123', season_id=1, section_id=2)


def finish(key):
    limit = time.monotonic() + 8
    while key in batches._running and time.monotonic() < limit:
        time.sleep(.01)
    assert key not in batches._running
    return batches.get(key)


@pytest.fixture()
def batch_env(make_project, monkeypatch):
    batches.recover()
    uids = [make_project(title=f'第{i}项') for i in range(1, 4)]
    pubs, started, orders = {}, [], []
    section = dict(id=2, season_id=1, title='默认', type=0)
    episodes = [dict(id=1, aid=1, cid=11, bvid='BV0000000001', title='已有成员', order=0)]

    def snapshot(*, cancel=None, **scope):
        assert scope == SCOPE
        value = dict(section=copy.deepcopy(section), episodes=copy.deepcopy(episodes))
        return {**value, 'fingerprint': digest(value)}

    def plan(uid, spec, **kwargs):
        return dict(recipe=copy.deepcopy(spec), steps=['download'], labels={'download':'获取视频'},
                    fingerprint=digest(dict(spec=spec, params=crud.get_project(uid).params)), blockers=[],
                    settings={}, version_specs=[], warnings=[], configuration='fixture', origins={})

    def launch(key, uid):
        started.append(uid)
        pid, aid = 'pub-' + uid, 100 + uids.index(uid)
        bvid = f'BV{aid:010d}'
        pubs[pid] = dict(id=pid, project_uid=uid, revision=1, status='published', error='', progress=100,
                         receipt=dict(bvid=bvid, aid=aid, collection_joined=True),
                         snapshot=dict(account_id='123', settings={'season_id':1, 'section_id':2}), draft={})
        with Session(get_engine()) as session:
            session.add(Publication(id=pid, project_uid=uid, status='published',
                                    receipt=json.dumps(pubs[pid]['receipt']), snapshot=json.dumps(pubs[pid]['snapshot'])))
            session.commit()
        episodes.append(dict(id=aid, aid=aid, cid=aid+1000, bvid=bvid, title=uid, order=len(episodes)))
        workflows._update(key, status='completed', publication_id=pid, phase='已发布')

    def apply(*, order, snapshot: dict, confirmed, cancel=None, **scope):
        assert confirmed and scope == SCOPE
        orders.append(list(order))
        by_aid = {e['aid']: e for e in episodes}
        expected = [e['aid'] for e in snapshot['episodes'] if e['aid'] not in order] + order
        episodes[:] = [by_aid[aid] for aid in expected]
        return dict(verified=True, changed=True)

    def verify(*, order, snapshot, **scope):
        expected = [e['aid'] for e in snapshot['episodes'] if e['aid'] not in order] + order
        return dict(verified=[e['aid'] for e in episodes] == expected)

    monkeypatch.setattr(workflows, 'prepared_plan', plan)
    monkeypatch.setattr(workflows, '_launch', launch)
    monkeypatch.setattr(service, 'view', lambda key: copy.deepcopy(pubs[key]))
    monkeypatch.setattr(service, 'read_collection_order', snapshot)
    monkeypatch.setattr(service, 'apply_collection_order', apply)
    monkeypatch.setattr(service, 'verify_collection_order', verify)
    def detail(account, action, body, *, cancel=None):
        assert account == '123' and action == 'detail'
        pub = next(p for p in pubs.values() if p['receipt'].get('bvid') == body['bvid'])
        return dict(mid=account, aid=pub['receipt']['aid'], bvid=body['bvid'], state=0)
    monkeypatch.setattr(studio, 'read', detail)
    yield dict(uids=uids, pubs=pubs, started=started, orders=orders, episodes=episodes, launch=launch)
    for key in list(workflows._running):
        workflows._running.pop(key)
        workflows._update(key, status='paused')
    for key in list(batches._running):
        batches.stop(key)
        finish(key)
    assert not batches._leases
    assert not batches._children


def prepared(env, uids=None):
    body = dict(project_uids=uids or env['uids'], scope=SCOPE, order_confirmed=True)
    plan = batches.preview(body)
    assert not plan['blockers'], plan['blockers']
    request = str(uuid.uuid4())
    row = batches.create(dict(body, fingerprint=plan['fingerprint'], confirmed=True, request_id=request))
    return row, body, request


def begin(row):
    candidate = batches.recovery_plan(row['id'])
    return batches.start(row['id'], dict(fingerprint=candidate['fingerprint'], confirmed=True,
                                        rights_confirmed=True, request_id=str(uuid.uuid4())))


def test_preview_read_only_create_not_execute_and_idempotent(batch_env):
    row, body, request = prepared(batch_env)
    assert row['status'] == 'ready' and not batch_env['started']
    assert batches.create(dict(body, fingerprint=row['fingerprint'], confirmed=True, request_id=request))['id'] == row['id']
    assert len(batches.listing(request)['batches']) == 1
    with pytest.raises(DubForgeError, match='不同方案'):
        batches.create(dict(body, fingerprint='changed', confirmed=True, request_id=request))
    with pytest.raises(DubForgeError, match='不同方案'):
        batches.create(dict(body, project_uids=list(reversed(body['project_uids'])), fingerprint=row['fingerprint'], confirmed=True, request_id=request))


def test_collection_display_snapshot_keeps_ids_and_does_not_change_plan_identity(batch_env):
    body = dict(project_uids=batch_env['uids'], scope=SCOPE, scope_display={
        'season_title': '旅行合集', 'section_title': '正片'}, order_confirmed=True)
    labeled = batches.preview(body)
    plain = batches.preview(dict(body, scope_display={}))
    assert labeled['scope'] == SCOPE
    assert labeled['scope_display'] == {'season_title': '旅行合集', 'section_title': '正片'}
    assert labeled['fingerprint'] == plain['fingerprint']
    request = str(uuid.uuid4())
    row = batches.create(dict(body, fingerprint=labeled['fingerprint'], confirmed=True, request_id=request))
    assert row['scope'] == SCOPE
    assert row['scope_display'] == labeled['scope_display']


def test_collection_cleanup_candidates_detect_same_source_across_projects_without_writes(batch_env, make_project):
    """合集预检只报告重复成员，按审核状态与创建时间建议保留，不触碰远端。"""
    old_uid = make_project(title='旧来源副本')
    new_uid = make_project(title='新来源副本')
    now = datetime.datetime.now(datetime.timezone.utc)
    old_receipt = dict(aid=701, bvid='BV0000000701', collection_joined=True)
    new_receipt = dict(aid=702, bvid='BV0000000702', collection_joined=True)
    with Session(get_engine()) as session:
        session.add(Publication(id='duplicate-old', project_uid=old_uid, status='published',
                                receipt=json.dumps(old_receipt), snapshot='{}',
                                created_at=now - datetime.timedelta(days=2),
                                updated_at=now - datetime.timedelta(days=2)))
        session.add(Publication(id='duplicate-new', project_uid=new_uid, status='published',
                                receipt=json.dumps(new_receipt), snapshot='{}',
                                created_at=now, updated_at=now))
        session.commit()
    collection = dict(section={'id': 2, 'season_id': 1, 'title': '默认', 'type': 0},
                      episodes=[dict(id=701, aid=701, cid=1, bvid='BV0000000701', title='旧', order=0),
                                dict(id=702, aid=702, cid=2, bvid='BV0000000702', title='新', order=1)])
    groups = batches._collection_cleanup_candidates(collection, {new_uid})
    assert len(groups) == 1
    assert groups[0]['recommended_keep']['publication_id'] == 'duplicate-new'
    assert [item['publication_id'] for item in groups[0]['recommended_remove']] == ['duplicate-old']
    assert 'BV0000000701' in batches._collection_cleanup_warnings(groups)[0]
    # helper is read-only: the local receipt rows and collection snapshot remain intact.
    with Session(get_engine()) as session:
        assert session.get(Publication, 'duplicate-old').status == 'published'


def test_confirmed_project_order_serial_publish_and_final_order(batch_env):
    wanted = list(reversed(batch_env['uids']))
    row, _, _ = prepared(batch_env, wanted)
    begin(row)
    result = finish(row['id'])
    assert result['status'] == 'done', result
    assert batch_env['started'] == wanted
    assert result['cursor'] == 3
    assert all(i['status'] == 'done' for i in result['items'])
    assert all(i['publication']['reuse_existing'] is False for i in result['items'])
    assert batch_env['orders'] == [[102, 101, 100]]
    assert [e['aid'] for e in batch_env['episodes']] == [1, 102, 101, 100]


def test_start_requires_rights_and_replays_without_duplicate(batch_env):
    row, _, _ = prepared(batch_env)
    candidate = batches.recovery_plan(row['id'])
    body = dict(fingerprint=candidate['fingerprint'], confirmed=True, request_id=str(uuid.uuid4()))
    with pytest.raises(DubForgeError, match='权利'):
        batches.start(row['id'], body)
    body['rights_confirmed'] = True
    batches.start(row['id'], body)
    finish(row['id'])
    assert batches.start(row['id'], body)['id'] == row['id']
    assert len(batch_env['started']) == 3
    with pytest.raises(DubForgeError, match='不同方案'):
        batches.start(row['id'], {**body, 'fingerprint':'changed'})


def test_executor_enqueue_failure_releases_batch_leases_and_marks_attention(batch_env, monkeypatch):
    row, _, _ = prepared(batch_env)
    original_submit = batches._pool.submit
    monkeypatch.setattr(batches._pool, 'submit', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('queue closed')))
    candidate = batches.recovery_plan(row['id'])
    with pytest.raises(DubForgeError, match='后台队列'):
        batches.start(row['id'], dict(fingerprint=candidate['fingerprint'], confirmed=True,
                                      rights_confirmed=True, request_id=str(uuid.uuid4())))
    assert batches.get(row['id'])['status'] == 'attention'
    assert not batches._running and not batches._leases and not batches._children
    monkeypatch.setattr(batches._pool, 'submit', original_submit)


def test_persisted_active_batch_blocks_editability_before_memory_lease(batch_env):
    row, _, _ = prepared(batch_env)
    batches._update(row['id'], status='running', phase='第1/3项：正在处理')
    try:
        report = runtime_edit.editability('project:' + batch_env['uids'][-1])
        # ADR-0132 separates field locks from project-level resource locks:
        # queued batch work may still edit unrelated settings, while archive,
        # rename, template binding and task start remain blocked.
        assert not report['locked']
        assert report['resource_locked'] and batch_env['uids'][-1] in report['resource_project_uids']
        assert '顺序发布批次' in report['resource_reason'] and '第 3/3 项' in report['resource_reason']
    finally:
        batches._update(row['id'], status='stopped', phase='已停止')


def test_failure_stops_remaining_and_resume_continues_same_workflow(batch_env, monkeypatch):
    original = batch_env['launch']
    fail_uid = batch_env['uids'][1]
    def failed(key, uid):
        if uid == fail_uid:
            batch_env['started'].append(uid)
            workflows._update(key, status='failed', error='隔离模型失败')
        else:
            original(key, uid)
    monkeypatch.setattr(workflows, '_launch', failed)
    row, _, _ = prepared(batch_env)
    begin(row)
    result = finish(row['id'])
    assert result['status'] == 'attention' and result['cursor'] == 1
    assert batch_env['started'] == batch_env['uids'][:2]
    failed_id = result['items'][1]['workflow_id']
    monkeypatch.setattr(workflows, '_launch', original)
    begin(result)
    resumed = finish(row['id'])
    assert resumed['status'] == 'done', resumed
    assert resumed['items'][1]['workflow_id'] == failed_id
    assert batch_env['started'].count(batch_env['uids'][0]) == 1


def test_unknown_receipt_is_never_retried(batch_env, monkeypatch):
    original = batch_env['launch']
    def unknown(key, uid):
        original(key, uid)
        batch_env['pubs']['pub-'+uid]['status'] = 'unknown'
        batch_env['pubs']['pub-'+uid]['receipt'] = {}
    monkeypatch.setattr(workflows, '_launch', unknown)
    row, _, _ = prepared(batch_env)
    begin(row)
    result = finish(row['id'])
    assert result['status'] == 'attention' and len(batch_env['started']) == 1
    candidate = batches.recovery_plan(row['id'])
    assert any('重复投稿' in b or '待核对' in b for b in candidate['blockers'])
    with pytest.raises(DubForgeError):
        begin(result)
    assert len(batch_env['started']) == 1


def test_future_project_leases_block_edit_archive_and_other_batches(batch_env, monkeypatch):
    holding = {}
    def launch(key, uid):
        workflows._running[key] = threading.Event()
        holding.update(key=key, uid=uid)
    monkeypatch.setattr(workflows, '_launch', launch)
    row, _, _ = prepared(batch_env)
    begin(row)
    deadline = time.monotonic()+2
    while not holding and time.monotonic()<deadline:
        time.sleep(.01)
    future = batch_env['uids'][-1]
    report = runtime_edit.editability('project:'+future)
    assert not report['locked']
    assert report['resource_locked'] and future in report['resource_project_uids']
    status = batches.project_status(future)
    assert status['position'] == 3
    assert status['item_status'] == 'queued' and status['lock_scope'] == 'queued'
    assert status['phase'] == '等待前项完成' and status['batch_phase'].startswith('第1/3项')
    with pytest.raises(DubForgeError, match='批次'):
        workflows.check_idle(future)
    from dubforge.project_archive import set_archived
    with pytest.raises(DubForgeError):
        set_archived(future, True)
    another, _, _ = prepared(batch_env, [future])
    with pytest.raises(DubForgeError, match='已有批次'):
        begin(another)
    batches.stop(row['id'])
    workflows._running.pop(holding['key'])
    workflows._update(holding['key'], status='paused')
    assert finish(row['id'])['status'] == 'stopped'
    assert not runtime_edit.editability('project:'+future)['locked']


def test_batch_lease_error_explains_queue_position_and_scope(batch_env, monkeypatch):
    holding = {}
    def launch(key, uid):
        workflows._running[key] = threading.Event()
        holding.update(key=key, uid=uid)
    monkeypatch.setattr(workflows, '_launch', launch)
    row, _, _ = prepared(batch_env)
    begin(row)
    deadline = time.monotonic() + 2
    while not holding and time.monotonic() < deadline:
        time.sleep(.01)
    future = batch_env['uids'][-1]
    with pytest.raises(DubForgeError, match=r'第 3/3 项.*等待前项完成') as exc:
        batches.check_idle(future)
    assert '单独运行步骤、自动流程或修改制作资料' in str(exc.value)
    batches.stop(row['id'])
    workflows._running.pop(holding['key'])
    workflows._update(holding['key'], status='paused')
    assert finish(row['id'])['status'] == 'stopped'


def test_pause_finishes_current_then_explicit_resume(batch_env, monkeypatch):
    original = batch_env['launch']
    def pause_first(key, uid):
        original(key, uid)
        if uid == batch_env['uids'][0]:
            batch = next(iter(batches._running))
            batches.pause(batch)
    monkeypatch.setattr(workflows, '_launch', pause_first)
    row, _, _ = prepared(batch_env)
    begin(row)
    result = finish(row['id'])
    assert result['status'] == 'paused' and result['cursor'] == 1
    assert len(batch_env['started']) == 1
    monkeypatch.setattr(workflows, '_launch', original)
    begin(result)
    assert finish(row['id'])['status'] == 'done'


def test_stale_configuration_and_external_collection_edit_block_start(batch_env):
    row, _, _ = prepared(batch_env)
    old = batches.recovery_plan(row['id'])
    project = crud.get_project(batch_env['uids'][0])
    project.params = json.dumps({'fixture':'changed'})
    with Session(get_engine()) as session:
        session.add(project)
        session.commit()
    with pytest.raises(DubForgeError, match='变化'):
        batches.start(row['id'], dict(confirmed=True, rights_confirmed=True, fingerprint=old['fingerprint'], request_id=str(uuid.uuid4())))
    batch_env['episodes'].append(dict(id=999, aid=999, cid=999, bvid='BV1234567890', title='外部新增', order=1))
    changed = batches.recovery_plan(row['id'])
    assert changed['fingerprint'] != old['fingerprint']
    assert any('重新确认' in w for w in changed['warnings'])
    with pytest.raises(DubForgeError, match='变化'):
        batches.start(row['id'], dict(confirmed=True, rights_confirmed=True, fingerprint=old['fingerprint'], request_id=str(uuid.uuid4())))
    assert not batch_env['started']


def test_reboot_pauses_without_starting_or_automatic_sort(batch_env):
    row, _, _ = prepared(batch_env)
    batches._update(row['id'], status='running', order_started=True)
    batches.recover()
    assert batches.get(row['id'])['status'] == 'paused'
    assert not batch_env['started'] and not batch_env['orders']


def test_sort_response_unknown_only_read_reconcile(batch_env, monkeypatch):
    apply = service.apply_collection_order
    def response_lost(**kwargs):
        apply(**kwargs)
        raise service.PublishError('排序回执丢失', code='PUBLISH_COLLECTION_ORDER_UNKNOWN')
    monkeypatch.setattr(service, 'apply_collection_order', response_lost)
    row, _, _ = prepared(batch_env)
    begin(row)
    result = finish(row['id'])
    assert result['status'] == 'attention' and result['order_started']
    begin(result)
    assert finish(row['id'])['status'] == 'done'
    assert len(batch_env['orders']) == 1 and len(batch_env['started']) == 3


def test_listing_contains_only_small_summary(batch_env):
    row, _, _ = prepared(batch_env)
    listed = batches.listing()['batches'][0]
    assert listed['id'] == row['id']
    assert 'plan' not in listed and 'recipe' not in listed['items'][0]
    assert len(json.dumps(listed)) < 4000


def test_candidates_source_date_never_uses_project_import_time(make_project):
    uid = make_project()
    assert batches.candidates([uid])['items'][0]['source_date'] is None
    from dubforge.utils.fs import atomic_write_bytes
    path = crud.project_dir(uid) / 'artifacts/download/video_info.json'
    atomic_write_bytes(path, json.dumps({'source':{'upload_date':'2024-03-09'}}).encode())
    assert batches.candidates([uid])['items'][0]['source_date'] == '2024-03-09'


@pytest.mark.parametrize('value', [[], ['x','x'], [True], ['x']*51, None])
def test_invalid_project_selection_is_domain_error(value):
    with pytest.raises(DubForgeError):
        batches.candidates(value)


def test_prepared_start_cannot_be_called_without_actual_batch_owner(make_project):
    uid = make_project()
    with pytest.raises(DubForgeError, match='身份'):
        workflows.start_prepared(uid, {}, str(uuid.uuid4()))


def test_new_collection_baseline_requires_new_confirmation_then_resumes(batch_env):
    row, _, _ = prepared(batch_env)
    old = batches.recovery_plan(row['id'])
    batch_env['episodes'].append(dict(id=88, aid=88, cid=888, bvid='BV0000000088', title='外部新增', order=1))
    fresh = batches.recovery_plan(row['id'])
    assert fresh['fingerprint'] != old['fingerprint']
    assert any('重新确认' in warning for warning in fresh['warnings'])
    # 预检不会写原计划；确认后才接受新的完整其他成员基线。
    assert batches.get(row['id'])['plan']['collection']['fingerprint'] == old['collection']['fingerprint']
    begin(row)
    result = finish(row['id'])
    assert result['status'] == 'done', result
    assert [e['aid'] for e in batch_env['episodes']] == [1, 88, 100, 101, 102]


@pytest.mark.parametrize('code', ['PUBLISH_COLLECTION_ORDER_CHANGED', 'PUBLISH_COLLECTION_ORDER_INVALID',
                                 'PUBLISH_COLLECTION_ORDER_REJECTED', 'PUBLISH_COLLECTION_ORDER_NOT_SENT'])
def test_definite_sort_rejection_can_reconfirm_but_unknown_cannot(batch_env, monkeypatch, code):
    original = service.apply_collection_order
    def rejected(**kwargs):
        raise service.PublishError('平台明确拒绝排序', code=code)
    monkeypatch.setattr(service, 'apply_collection_order', rejected)
    row, _, _ = prepared(batch_env)
    begin(row)
    result = finish(row['id'])
    assert result['status'] == 'attention' and not result['order_started']
    monkeypatch.setattr(service, 'apply_collection_order', original)
    begin(result)
    assert finish(row['id'])['status'] == 'done'
    assert len(batch_env['started']) == 3


def test_unknown_sort_with_wrong_order_is_blocked_without_any_new_write(batch_env, monkeypatch):
    def unknown(**kwargs):
        batch_env['episodes'].reverse()
        raise service.PublishError('排序结果待核对', code='PUBLISH_COLLECTION_ORDER_UNKNOWN')
    monkeypatch.setattr(service, 'apply_collection_order', unknown)
    row, _, _ = prepared(batch_env)
    begin(row)
    result = finish(row['id'])
    assert result['order_started']
    candidate = batches.recovery_plan(row['id'])
    assert any('不会自动重复' in blocker for blocker in candidate['blockers'])
    with pytest.raises(DubForgeError):
        begin(result)
    assert len(batch_env['started']) == 3 and batch_env['orders'] == []


def test_stop_during_final_launch_precheck_starts_no_child(batch_env, monkeypatch):
    original = workflows.prepared_plan
    def stop_before_launch(uid, spec, **kwargs):
        result = original(uid, spec, **kwargs)
        if batches.owns(uid):
            batches.stop(batches._leases[uid])
        return result
    monkeypatch.setattr(workflows, 'prepared_plan', stop_before_launch)
    row, _, _ = prepared(batch_env)
    begin(row)
    result = finish(row['id'])
    assert result['status'] == 'stopped'
    assert batch_env['started'] == []


def test_stop_during_final_collection_read_does_not_write_order(batch_env, monkeypatch):
    original = service.read_collection_order
    def stop_before_sort(**scope):
        result = original(**scope)
        key = getattr(batches._owner, 'key', None)
        if key and batches.get(key)['phase'] == '核对并整理合集顺序':
            batches.stop(key)
        return result
    monkeypatch.setattr(service, 'read_collection_order', stop_before_sort)
    row, _, _ = prepared(batch_env)
    begin(row)
    result = finish(row['id'])
    assert result['status'] == 'stopped' and not result['order_started']
    assert len(batch_env['started']) == 3 and batch_env['orders'] == []


def test_explicit_publication_choice_keeps_historical_receipt_and_uses_new_child(batch_env, monkeypatch):
    uid = batch_env['uids'][0]
    old_id, chosen_id = 'historical-'+uid, 'chosen-'+uid
    for pid, status in [(old_id,'rejected'), (chosen_id,'draft')]:
        pub = dict(id=pid, project_uid=uid, revision=1, status=status, progress=0, error='', receipt={},
                   snapshot=dict(account_id='123', settings={'season_id':1, 'section_id':2}), draft={})
        batch_env['pubs'][pid] = pub
        with Session(get_engine()) as session:
            session.add(Publication(id=pid, project_uid=uid, status=status, snapshot=json.dumps(pub['snapshot'])))
            session.commit()
    with Session(get_engine()) as session:
        session.add(WorkflowRun(id='historical-flow', project_uid=uid, request_id=str(uuid.uuid4()), status='attention', publication_id=old_id))
        session.commit()
    monkeypatch.setattr(service, 'plan', lambda key: {'fingerprint':'original-draft-files'})
    candidate = batches.candidates([uid])['items'][0]
    assert len(candidate['publication_choices']) == 2
    assert any('多份' in b for b in candidate['blockers'])
    body = dict(project_uids=[uid], scope=SCOPE, order_confirmed=True, publication_ids={uid:chosen_id})
    preview = batches.preview(body)
    assert not preview['blockers'], preview['blockers']
    assert preview['items'][0]['workflow_id'] == ''
    row = batches.create(dict(body, fingerprint=preview['fingerprint'], confirmed=True, request_id=str(uuid.uuid4())))
    def publish_chosen(key, current_uid):
        assert workflows.get(key)['publication_id'] == chosen_id
        assert key != 'historical-flow'
        batch_env['started'].append(current_uid)
        batch_env['pubs'][chosen_id].update(status='published', receipt=dict(aid=200,bvid='BV1234567890',collection_joined=True))
        batch_env['episodes'].append(dict(id=200,aid=200,cid=2200,bvid='BV1234567890',title='新稿件',order=1))
        workflows._update(key, status='completed', phase='已发布')
    monkeypatch.setattr(workflows, '_launch', publish_chosen)
    begin(row)
    result = finish(row['id'])
    assert result['status'] == 'done', result
    assert result['items'][0]['publication_id'] == chosen_id
    assert workflows.get('historical-flow')['publication_id'] == old_id


def test_publication_choice_from_other_project_is_rejected(batch_env):
    with Session(get_engine()) as session:
        session.add(Publication(id='other-pub', project_uid=batch_env['uids'][1]))
        session.commit()
    with pytest.raises(DubForgeError, match='不属于'):
        batches.preview(dict(project_uids=[batch_env['uids'][0]], scope=SCOPE, order_confirmed=True,
                             publication_ids={batch_env['uids'][0]:'other-pub'}))


def interrupted_publication_batch(env, monkeypatch, *, status='rejected'):
    """真实批次调度停在虚拟退回稿件，保留原流程及其幂等请求标识。"""
    uid = env['uids'][0]
    old_id = 'interrupted-' + uid
    row, _, _ = prepared(env, [uid])

    def interrupted(key, current_uid):
        assert current_uid == uid
        env['started'].append(uid)
        receipt = dict(bvid='BV0000000200', aid=200) if status != 'unknown' else {}
        pub = dict(id=old_id, project_uid=uid, revision=1, status=status, progress=0,
                   error='原稿件需要处理', receipt=receipt,
                   snapshot=dict(account_id='123', settings={'season_id':1, 'section_id':2}), draft={})
        env['pubs'][old_id] = pub
        with Session(get_engine()) as session:
            session.add(Publication(id=old_id, project_uid=uid, status=status,
                                    snapshot=json.dumps(pub['snapshot']), receipt=json.dumps(receipt)))
            session.commit()
        workflows._update(key, status='attention', publication_id=old_id, error='原稿件需要处理')

    monkeypatch.setattr(workflows, '_launch', interrupted)
    begin(row)
    result = finish(row['id'])
    assert result['status'] == 'attention' and result['cursor'] == 0
    assert result['items'][0]['publication_id'] == old_id
    return result, env['pubs'][old_id]


def replacement_publication(env, uid, *, key='replacement-draft', status='draft', bvid=''):
    receipt = dict(bvid=bvid, aid=201) if bvid else {}
    pub = dict(id=key, project_uid=uid, revision=1, status=status, progress=0, error='',
               receipt=receipt,
               snapshot=dict(account_id='123', settings={'season_id':1, 'section_id':2}),
               draft={'overrides':{'title':'用户编辑的新草稿'}})
    env['pubs'][key] = pub
    with Session(get_engine()) as session:
        session.add(Publication(id=key, project_uid=uid, status=status,
                                draft=json.dumps(pub['draft']), receipt=json.dumps(receipt)))
        session.commit()
    return pub


@pytest.mark.parametrize('old_status', ['rejected', 'unknown'])
def test_recovery_explicit_rebind_to_unsubmitted_draft_is_read_only(batch_env, monkeypatch, old_status):
    row, old = interrupted_publication_batch(batch_env, monkeypatch, status=old_status)
    uid = old['project_uid']
    replacement = replacement_publication(batch_env, uid)
    old_flow = workflows.get(row['items'][0]['workflow_id'])
    old_snapshot = copy.deepcopy(old)
    stored_items = copy.deepcopy(row['items'])
    default = batches.recovery_plan(row['id'])
    assert default['blockers'] and default['items'][0]['publication_id'] == old['id']

    plan = batches.recovery_plan(row['id'], publication_ids={uid:replacement['id']})

    assert not plan['blockers'], plan['blockers']
    item = plan['items'][0]
    assert item['publication_id'] == replacement['id']
    assert item['publication_rebound'] is True
    assert item['publication_action'] == 'prepare'
    assert item['plan']['refresh_publication'] == replacement['id']
    assert item['publication']['prepare'] is True
    assert item['recipe']['overrides']['title'] == '用户编辑的新草稿'
    assert plan['fingerprint'] != default['fingerprint']
    assert batches.get(row['id'])['items'] == stored_items
    assert workflows.get(old_flow['id']) == old_flow and old == old_snapshot
    assert batch_env['started'] == [uid] and not batch_env['orders']


@pytest.mark.parametrize('old_status', ['draft', 'submitted', 'published', 'private', 'reviewed'])
def test_recovery_cannot_rebind_a_normal_publication(batch_env, monkeypatch, old_status):
    row, old = interrupted_publication_batch(batch_env, monkeypatch)
    uid = old['project_uid']
    old['status'] = old_status
    if old_status == 'draft':
        old['receipt'] = {}
    with Session(get_engine()) as session:
        record = session.get(Publication, old['id'])
        record.status, record.receipt = old_status, json.dumps(old['receipt'])
        session.add(record)
        session.commit()
    replacement = replacement_publication(batch_env, uid)

    with pytest.raises(DubForgeError, match='只有审核退回或结果不明') as exc:
        batches.recovery_plan(row['id'], publication_ids={uid:replacement['id']})

    assert exc.value.code == 'RELEASE_BATCH_PUBLICATION_INVALID'
    assert batches.get(row['id'])['items'][0]['publication_id'] == old['id']
    assert batch_env['started'] == [uid]


@pytest.mark.parametrize(('status', 'bvid'), [('draft', 'BV0000000201'), ('submitted', ''), ('rejected', '')])
def test_recovery_rebind_target_must_be_editable_unsubmitted_draft(batch_env, monkeypatch, status, bvid):
    row, old = interrupted_publication_batch(batch_env, monkeypatch)
    replacement = replacement_publication(batch_env, old['project_uid'], status=status, bvid=bvid)

    with pytest.raises(DubForgeError, match='换绑目标必须') as exc:
        batches.recovery_plan(row['id'], publication_ids={old['project_uid']:replacement['id']})

    assert exc.value.code == 'RELEASE_BATCH_PUBLICATION_INVALID'
    assert batches.get(row['id'])['items'][0]['publication_id'] == old['id']


def test_recovery_rebind_keeps_new_draft_when_batch_and_old_workflow_differ(batch_env, monkeypatch):
    row, old = interrupted_publication_batch(batch_env, monkeypatch)
    uid = old['project_uid']
    replacement = replacement_publication(batch_env, uid)
    # 模拟启动请求已保存换绑计划、但新流程尚未入队时中断的持久化状态。
    stored = copy.deepcopy(row['items'])
    stored[0]['publication_id'] = replacement['id']
    batches._update(row['id'], items=stored)

    plan = batches.recovery_plan(row['id'], publication_ids={uid:replacement['id']})

    assert not plan['blockers'], plan['blockers']
    assert plan['items'][0]['publication_id'] == replacement['id']
    assert plan['items'][0]['publication_rebound'] is True
    assert plan['items'][0]['plan']['refresh_publication'] == replacement['id']
    assert workflows.get(stored[0]['workflow_id'])['publication_id'] == old['id']


def test_recovery_rebind_uses_new_workflow_request_and_preserves_old_history(batch_env, monkeypatch):
    row, old = interrupted_publication_batch(batch_env, monkeypatch)
    uid = old['project_uid']
    old_flow = workflows.get(row['items'][0]['workflow_id'])
    replacement = replacement_publication(batch_env, uid)
    launches = []

    def publish_replacement(key, current_uid):
        run = workflows.get(key)
        launches.append(run)
        batch_env['started'].append(current_uid)
        assert run['publication_id'] == replacement['id']
        replacement.update(status='published', receipt=dict(aid=201,bvid='BV0000000201',collection_joined=True))
        batch_env['episodes'].append(dict(id=201,aid=201,cid=1201,bvid='BV0000000201',title='新稿件',order=1))
        workflows._update(key, status='completed', phase='已发布')

    monkeypatch.setattr(workflows, '_launch', publish_replacement)
    selection = {uid:replacement['id']}
    plan = batches.recovery_plan(row['id'], publication_ids=selection)
    body = dict(fingerprint=plan['fingerprint'], publication_ids=selection, confirmed=True,
                rights_confirmed=True, request_id=str(uuid.uuid4()))
    batches.start(row['id'], body)
    result = finish(row['id'])

    assert result['status'] == 'done', result
    assert len(launches) == 1 and launches[0]['id'] != old_flow['id']
    assert launches[0]['request_id'] != old_flow['request_id']
    assert result['items'][0]['workflow_id'] == launches[0]['id']
    assert result['items'][0]['publication_id'] == replacement['id']
    assert workflows.get(old_flow['id']) == old_flow
    assert old['status'] == 'rejected' and old['receipt']['bvid'] == 'BV0000000200'
    assert batch_env['orders'] == [[201]]
    # 启动请求重放不会重建新流程或重复投稿；改变稿件映射须新的确认请求。
    assert batches.start(row['id'], body)['status'] == 'done'
    assert len(launches) == 1
    with pytest.raises(DubForgeError, match='不同方案'):
        batches.start(row['id'], dict(body, publication_ids={uid:old['id']}))


def test_api_readonly_candidates_and_origin_guard(batch_env):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    response = client.get('/api/release-batches/candidates', params={'uids':','.join(batch_env['uids'])})
    assert response.status_code == 200 and len(response.json()['items']) == 3
    assert not batch_env['started']
    response = client.post('/api/release-batches/plan', headers={'Origin':'https://invalid.example'},
                           json=dict(project_uids=batch_env['uids'], scope=SCOPE, order_confirmed=True))
    assert response.status_code in {400,403}
    assert client.get('/api/release-batches/candidates', params={'uids':'x'*5051}).status_code == 400


def test_candidate_publication_choices_expose_review_source_and_utc_creation(batch_env):
    uid = batch_env['uids'][0]
    created = datetime.datetime(2026, 9, 30, 12, 34, 56)
    with Session(get_engine()) as session:
        for key, source in [('ordinary', None), ('review', {'source_publication_id':'original'}), ('invalid', '并非真实修订来源')]:
            draft = {'overrides':{'title':'相同标题'}}
            if source is not None:
                draft['review_source'] = source
            session.add(Publication(id=key, project_uid=uid, draft=json.dumps(draft), created_at=created))
        session.commit()
    choices = {item['id']: item for item in batches.candidates([uid])['items'][0]['publication_choices']}
    assert choices['ordinary']['is_review_revision'] is False
    assert choices['review']['is_review_revision'] is True
    assert choices['invalid']['is_review_revision'] is False
    assert all(item['created_at'] == '2026-09-30T12:34:56+00:00' for item in choices.values())
    assert not batch_env['started'] and not batch_env['orders']


def existing_publication(env, *, account='123', season=99, section=98, joined=False, member=True, bvid_in_collection=True):
    uid, key, aid, bvid = env['uids'][0], 'existing-bv', 200, 'BV0000000200'
    pub = dict(id=key, project_uid=uid, revision=1, status='published', error='', progress=100,
               receipt=dict(aid=aid, bvid=bvid, collection_joined=joined),
               snapshot=dict(account_id=account, settings=dict(season_id=season, section_id=section, visibility='public')),
               draft={})
    env['pubs'][key] = pub
    with Session(get_engine()) as session:
        session.add(Publication(id=key, project_uid=uid, status=pub['status'],
                                snapshot=json.dumps(pub['snapshot']), receipt=json.dumps(pub['receipt'])))
        session.add(WorkflowRun(id='old-finished-flow', project_uid=uid, request_id=str(uuid.uuid4()),
                               status='completed', publication_id=key, phase='历史已发布'))
        session.commit()
    if member:
        env['episodes'].append(dict(id=aid, aid=aid, cid=aid+1000, bvid=bvid if bvid_in_collection else '',
                                    title='原BV已移入当前目标', order=len(env['episodes'])))
    return pub


@pytest.mark.parametrize('bvid_in_collection', [True, False])
def test_existing_bv_moved_to_target_reused_without_republishing_or_rewriting_snapshot(batch_env, monkeypatch, bvid_in_collection):
    pub = existing_publication(batch_env, bvid_in_collection=bvid_in_collection)
    before = copy.deepcopy(pub)
    row, body, _ = prepared(batch_env, [pub['project_uid']])
    binding = row['items'][0]['publication']
    assert binding['reuse_existing'] is True and binding['collection_joined'] is True
    assert '复用原BV' in row['items'][0]['plan']['recovery']['start_label']
    online = batches.recovery_plan(row['id'])
    offline = batches.recovery_plan(row['id'], online=False, collection=online['collection'])
    assert online['fingerprint'] == offline['fingerprint']
    assert not online['blockers'] and not offline['blockers']
    monkeypatch.setattr(workflows, 'start_prepared', lambda *args, **kwargs: pytest.fail('已有原BV不能重启旧发布流程'))
    monkeypatch.setattr(service, 'enqueue', lambda *args, **kwargs: pytest.fail('已有原BV不能上传、投稿或重入旧合集'))
    begin(row)
    result = finish(row['id'])
    assert result['status'] == 'done', result
    assert result['items'][0]['phase'] == '原BV审核与目标小节已核对'
    assert batch_env['orders'] == [[200]] and not batch_env['started']
    assert pub == before
    with Session(get_engine()) as session:
        record = session.get(Publication, pub['id'])
        assert json.loads(record.snapshot) == before['snapshot']
        assert json.loads(record.receipt) == before['receipt']
    assert workflows.get('old-finished-flow')['status'] == 'completed'


@pytest.mark.parametrize(('account', 'season', 'section', 'reason'), [
    ('456', 1, 2, '原记录账号为 456'), ('123', 99, 2, '原记录合集为 99'), ('123', 1, 98, '原记录小节为 98'),
])
def test_existing_target_mismatch_reports_exact_different_field(batch_env, account, season, section, reason):
    pub = existing_publication(batch_env, account=account, season=season, section=section, member=False)
    plan = batches.preview(dict(project_uids=[pub['project_uid']], scope=SCOPE, order_confirmed=True))
    assert any(reason in b for b in plan['blockers'])
    assert not batch_env['orders'] and not batch_env['started']


def test_account_mismatch_cannot_be_bypassed_by_matching_collection_member(batch_env):
    pub = existing_publication(batch_env, account='456')
    plan = batches.preview(dict(project_uids=[pub['project_uid']], scope=SCOPE, order_confirmed=True))
    assert any('已有BV不能转移到另一个账号' in b for b in plan['blockers'])


@pytest.mark.parametrize('field', ['aid', 'bvid'])
def test_collection_membership_rejects_conflicting_original_bv_identity(batch_env, field):
    pub = existing_publication(batch_env)
    batch_env['episodes'][-1][field] = 201 if field == 'aid' else 'BV0000000201'
    plan = batches.preview(dict(project_uids=[pub['project_uid']], scope=SCOPE, order_confirmed=True))
    assert any('稿件编号与原BV不一致' in b for b in plan['blockers'])


@pytest.mark.parametrize('state', [-2, -16, -1, 999, None, '0', False, [], {}])
def test_existing_bv_requires_current_remote_review_pass_instead_of_historical_published(batch_env, monkeypatch, state):
    pub = existing_publication(batch_env)
    row, _, _ = prepared(batch_env, [pub['project_uid']])
    monkeypatch.setattr(studio, 'read', lambda account, action, body, **kwargs:
                        dict(mid=account, bvid=body['bvid'], aid=200, state=state, state_desc='历史文字可能写已通过'))
    begin(row)
    result = finish(row['id'])
    assert result['status'] == 'attention' and '当前审核' in result['error']
    assert not batch_env['orders'] and not batch_env['started']
    assert pub['status'] == 'published' and not pub['receipt']['collection_joined']


@pytest.mark.parametrize('field', ['mid', 'aid', 'bvid'])
def test_existing_bv_detail_checks_real_owner_aid_and_bvid_before_sort(batch_env, monkeypatch, field):
    pub = existing_publication(batch_env, bvid_in_collection=False)
    row, _, _ = prepared(batch_env, [pub['project_uid']])
    remote = dict(mid='123', aid=200, bvid=pub['receipt']['bvid'], state=0)
    remote[field] = {'mid':'456', 'aid':201, 'bvid':'BV0000000201'}[field]
    monkeypatch.setattr(studio, 'read', lambda *args, **kwargs: remote)
    begin(row)
    result = finish(row['id'])
    assert result['status'] == 'attention' and '原回执不一致' in result['error']
    assert not batch_env['orders'] and not batch_env['started']


def test_existing_bv_stop_during_remote_read_keeps_original_and_does_not_sort(batch_env, monkeypatch):
    pub = existing_publication(batch_env)
    row, _, _ = prepared(batch_env, [pub['project_uid']])
    def stopped(account, action, body, *, cancel=None):
        assert isinstance(cancel, threading.Event)
        batches.stop(row['id'])
        assert cancel.is_set()
        return dict(mid=account, bvid=body['bvid'], aid=200, state=0)
    monkeypatch.setattr(studio, 'read', stopped)
    begin(row)
    result = finish(row['id'])
    assert result['status'] == 'stopped' and not result['order_started']
    assert not batch_env['orders'] and not batch_env['started']
    assert workflows.get('old-finished-flow')['status'] == 'completed'


@pytest.mark.parametrize('legacy_plan', [False, True])
def test_original_bv_joined_after_batch_creation_has_consistent_recovery_binding_and_does_not_republish(batch_env, monkeypatch, legacy_plan):
    pub = existing_publication(batch_env, season=1, section=2, member=False)
    row, _, _ = prepared(batch_env, [pub['project_uid']])
    original = copy.deepcopy(row['plan']['collection'])
    assert not row['items'][0]['publication']['reuse_existing']
    if legacy_plan:
        previous_plan = copy.deepcopy(row['plan'])
        previous_plan.pop('binding_collection')
        batches._update(row['id'], plan=previous_plan)
    batch_env['episodes'].append(dict(id=200, aid=200, cid=1200, bvid=pub['receipt']['bvid'], title='后来加入原BV', order=1))
    online = batches.recovery_plan(row['id'])
    assert online['collection'] == original
    assert online['binding_collection']['fingerprint'] != original['fingerprint']
    assert online['items'][0]['publication']['reuse_existing'] is True
    offline = batches.recovery_plan(row['id'], online=False, collection=online['collection'],
                                    binding_collection=online['binding_collection'])
    assert online['fingerprint'] == offline['fingerprint']
    assert not online['blockers'] and not offline['blockers']
    monkeypatch.setattr(workflows, 'start_prepared', lambda *args, **kwargs: pytest.fail('恢复只能复用原BV'))
    monkeypatch.setattr(service, 'enqueue', lambda *args, **kwargs: pytest.fail('恢复不能上传、投稿或重入旧合集'))
    begin(row)
    result = finish(row['id'])
    assert result['status'] == 'done', result
    assert result['plan']['collection'] == original
    assert batch_env['orders'] == [[200]] and not batch_env['started']


def test_reused_bv_review_is_checked_again_before_final_order_write(batch_env, monkeypatch):
    pub = existing_publication(batch_env)
    row, _, _ = prepared(batch_env, [pub['project_uid']])
    calls = []
    def edited_again(account, action, body, *, cancel=None):
        calls.append(body['bvid'])
        return dict(mid=account, bvid=body['bvid'], aid=200, state=0 if len(calls) == 1 else -1)
    monkeypatch.setattr(studio, 'read', edited_again)
    begin(row)
    result = finish(row['id'])
    assert result['status'] == 'attention' and '当前审核尚未确认通过' in result['error']
    assert result['items'][0]['status'] == 'done'
    assert len(calls) == 2 and not result['order_started'] and not batch_env['orders']


def test_resuming_completed_reused_item_checks_current_review_before_first_order_write(batch_env, monkeypatch):
    pub = existing_publication(batch_env)
    row, _, _ = prepared(batch_env, [pub['project_uid']])
    state = {'value':0}
    def pause_after_first_review(account, action, body, *, cancel=None):
        if state['value'] == 0:
            batches.pause(row['id'])
        return dict(mid=account, bvid=body['bvid'], aid=200, state=state['value'])
    monkeypatch.setattr(studio, 'read', pause_after_first_review)
    begin(row)
    paused = finish(row['id'])
    assert paused['status'] == 'paused' and paused['items'][0]['status'] == 'done'
    assert not batch_env['orders'] and not paused['order_started']
    state['value'] = -1
    begin(paused)
    result = finish(row['id'])
    assert result['status'] == 'attention' and '当前审核尚未确认通过' in result['error']
    assert not result['order_started'] and not batch_env['orders']


def test_unknown_order_recovery_preserves_journal_and_only_verifies_existing_order(batch_env, monkeypatch):
    pub = existing_publication(batch_env)
    row, _, _ = prepared(batch_env, [pub['project_uid']])
    apply = service.apply_collection_order
    def response_lost(**kwargs):
        apply(**kwargs)
        raise service.PublishError('排序响应丢失', code='PUBLISH_COLLECTION_ORDER_UNKNOWN')
    monkeypatch.setattr(service, 'apply_collection_order', response_lost)
    begin(row)
    pending = finish(row['id'])
    assert pending['status'] == 'attention' and pending['order_started']
    original = copy.deepcopy(pending['plan']['collection'])
    monkeypatch.setattr(studio, 'read', lambda *args, **kwargs: pytest.fail('未知排序恢复只核对排序，不重走已完成项'))
    begin(pending)
    result = finish(row['id'])
    assert result['status'] == 'done' and result['plan']['collection'] == original
    assert batch_env['orders'] == [[200]] and not batch_env['started']
