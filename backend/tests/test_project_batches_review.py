"""混合批次独立验收：保护授权、占用和断点回收，所有平台执行均被隔离。"""
import copy
import json
import threading
import uuid
from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

from dubforge import configuration, project_batches as batches, runtime_edit, workflows
from dubforge.configuration_models import ConfigurationScope
from dubforge.db import get_engine
from dubforge.errors import DubForgeError
from dubforge.project_batch_models import ProjectBatch
from dubforge.publishing.models import Publication
from dubforge.workflow_models import WorkflowRun
from dubforge.workflow_plan import digest
from test_configuration import save
from test_release_batch_targets import targets
from test_release_batches import batch_env
from test_publishing import setup_publish


@pytest.fixture
def review_env(make_project, monkeypatch):
    """只替换真正执行边界，真实使用配置、批次持久化与占用核验。"""
    from dubforge.pipeline import recovery

    def prepared(uid, spec, **options):
        return dict(recipe=copy.deepcopy(spec), steps=[], labels={}, blockers=[], warnings=[],
                    settings={}, version_specs=[], fingerprint=digest(spec))

    monkeypatch.setattr(workflows, 'prepared_plan', prepared)
    monkeypatch.setattr(recovery, 'describe', lambda *args, **kwargs: dict(rows=[]))
    monkeypatch.setattr(workflows, '_plan_dependencies', lambda *args: None)
    batches._closing = False
    launches = []

    class WaitingThread:
        def __init__(self, target, args, **options):
            launches.append((target, args))

        def start(self):
            pass

    monkeypatch.setattr(batches, 'threading', SimpleNamespace(Thread=WaitingThread, Event=threading.Event))

    def project(mode='produce', scope=None):
        uid = make_project()
        values = configuration.resolve(uid)['values']
        save('project:' + uid, {'assembly.target_version_ids': [values['assembly.versions'][0]['id']],
            'run.mode': mode, 'publish.project_target': {'kind': 'new'},
            **({'publish.account_id': scope['account_id'], 'publish.season_id': scope['season_id'],
                'publish.section_id': scope['section_id']} if scope else {})})
        return uid

    yield SimpleNamespace(project=project, launches=launches)
    # WaitingThread 不会执行任何工作，测试只留下可直接清理的隔离租约。
    batches._running.clear()
    batches._leases.clear()
    batches._children.clear()


def create(uids):
    body = dict(project_uids=uids, order_confirmed=True)
    plan = batches.preview(body)
    assert not plan['blockers'], plan['blockers']
    return batches.create({**body, 'request_id': str(uuid.uuid4()),
                           'fingerprint': plan['fingerprint'], 'confirmed': True})


def start(batch):
    plan = batches.recovery_plan(batch['id'])
    assert not plan['blockers'], plan['blockers']
    return batches.start(batch['id'], dict(request_id=str(uuid.uuid4()),
        fingerprint=plan['fingerprint'], confirmed=True, rights_confirmed=True))


def test_grouping_uses_real_account_collection_section_and_saved_authority(review_env):
    scope = dict(account_id='123', season_id=7, section_id=8)
    uids = [review_env.project('publish', scope), review_env.project('publish', scope)]
    uids += [review_env.project(mode, scope) for mode in ('produce', 'draft', 'upload')]
    uids += [review_env.project('publish', {**scope, 'section_id': 9}),
             review_env.project('publish', {**scope, 'account_id': '456'})]
    rows = batches.candidates(uids)['items']
    groups = batches._group_rows(rows)
    collection = [group for group in groups if group['kind'] == 'collection']
    independent = [group for group in groups if group['kind'] == 'independent']
    assert len(collection) == 3
    assert collection[0]['project_uids'] == uids[:2]
    assert [group['project_uids'] for group in independent] == [[uid] for uid in uids[2:5]]
    assert [group['items'][0]['delivery_mode'] for group in independent] == ['produce', 'draft', 'upload']


@pytest.mark.parametrize('mode', ['draft', 'upload', 'publish'])
def test_unknown_receipt_blocks_new_independent_delivery(review_env, mode):
    uid = review_env.project(mode)
    with Session(get_engine()) as session:
        session.add(Publication(project_uid=uid, status='unknown'))
        session.commit()
    plan = batches.preview(dict(project_uids=[uid], order_confirmed=True))
    assert plan['blockers'], '有未知投稿回执时不能新建另一条独立稿件'
    assert any('回执' in reason or '待核对' in reason or '结果' in reason for reason in plan['blockers'])
    assert not review_env.launches


def test_unknown_receipt_does_not_block_production_only(review_env):
    uid = review_env.project()
    with Session(get_engine()) as session:
        session.add(Publication(project_uid=uid, status='unknown'))
        session.commit()
    plan = batches.preview(dict(project_uids=[uid], order_confirmed=True))
    assert not plan['blockers'], plan['blockers']
    assert plan['groups'][0]['items'][0]['recipe']['mode'] == 'produce'


def test_parent_reservation_blocks_structure_but_keeps_queued_ordinary_settings_editable(review_env):
    uid = review_env.project()
    batch = start(create([uid]))
    assert review_env.launches
    assert not workflows.view(uid).get('runs'), '尚未启动任何真实子流程'
    report = runtime_edit.editability('project:' + uid)
    assert report['resource_locked']
    save('project:' + uid, {'assemble.crf': 27})
    assert configuration.resolve(uid)['values']['assemble.crf'] == 27
    with pytest.raises(DubForgeError):
        configuration.bind(dict(uids=[uid], template_id='', confirmed=True,
                                fingerprint=configuration.binding_plan([uid], '')['fingerprint']))
    assert batches.get(batch['id'])['status'] == 'running'


@pytest.mark.parametrize('path,value', [
    ('run.mode', 'publish'), ('publish.account_id', '456'),
    ('publish.season_id', 17), ('publish.section_id', 18),
    ('publish.project_target', {'kind': 'new'}),
])
def test_parent_queued_goal_changes_require_stopping_batch(review_env, path, value):
    uid = review_env.project()
    start(create([uid]))
    with pytest.raises(DubForgeError):
        save('project:' + uid, {path: value})


def test_persisted_parent_reservation_blocks_even_before_memory_lease(review_env):
    uid = review_env.project()
    batch = create([uid])
    with Session(get_engine()) as session:
        row = session.get(ProjectBatch, batch['id'])
        groups = json.loads(row.groups)
        groups[0]['status'] = 'running'
        row.groups, row.status = json.dumps(groups), 'running'
        session.add(row)
        session.commit()
    assert not batches._leases
    assert runtime_edit.editability('project:' + uid)['resource_locked']
    with pytest.raises(DubForgeError):
        runtime_edit.check_idle(uid)
    save('project:' + uid, {'assemble.crf': 26})


def test_recovery_discovers_same_workflow_after_child_creation_receipt_was_lost(review_env):
    uid = review_env.project()
    batch = create([uid])
    group = batch['groups'][0]
    with Session(get_engine()) as session:
        child = WorkflowRun(project_uid=uid, request_id=batches._child_request(batch['id'], group['id']),
            status='paused', recipe=json.dumps(group['items'][0]['recipe']),
            plan=json.dumps(group['plan']))
        session.add(child)
        session.commit()
        session.refresh(child)
        child_id = child.id
    monkey_group = batches._discover_child(batch['id'], group)
    assert monkey_group == {'workflow_id': child_id}
    assert batches._child_request(batch['id'], group['id']) == child.request_id


def test_recovered_finished_child_is_reflected_without_relaunch(review_env):
    uid = review_env.project()
    batch = create([uid])
    group = batch['groups'][0]
    with Session(get_engine()) as session:
        child = WorkflowRun(project_uid=uid, request_id=batches._child_request(batch['id'], group['id']),
            status='completed', recipe=json.dumps(group['items'][0]['recipe']),
            plan=json.dumps(group['plan']))
        session.add(child)
        session.commit()
    plan = batches.recovery_plan(batch['id'])
    assert not plan['blockers']
    assert not review_env.launches
    assert plan['groups'][0]['workflow_id']


def test_frozen_production_goal_is_not_promoted_by_later_saved_publish_settings(review_env):
    uid = review_env.project()
    batch = create([uid])
    save('project:' + uid, {'run.mode': 'publish'})
    plan = batches.recovery_plan(batch['id'])
    assert plan['groups'][0]['items'][0]['recipe']['mode'] == 'produce'
    assert plan['groups'][0]['plan']['recipe']['delivery_mode'] == 'produce'


def test_finished_parent_does_not_occupy_project_summary_forever(review_env):
    uid = review_env.project()
    batch = create([uid])
    with Session(get_engine()) as session:
        row = session.get(ProjectBatch, batch['id'])
        groups = json.loads(row.groups)
        groups[0]['status'] = 'done'
        row.groups, row.status = json.dumps(groups), 'done'
        session.add(row)
        session.commit()
    assert uid not in batches.project_statuses([uid])


def test_internal_owner_scope_is_required_for_prepared_launch(review_env):
    uid = review_env.project()
    batch = start(create([uid]))
    prepared = batch['groups'][0]['plan']
    with pytest.raises(DubForgeError):
        workflows.start_prepared(uid, prepared, str(uuid.uuid4()), frozen_target=True)


@pytest.mark.parametrize('mode', ['produce', 'draft', 'upload'])
def test_no_submission_batch_can_start_without_claiming_submission_rights(review_env, mode):
    uid = review_env.project(mode)
    batch = create([uid])
    plan = batches.recovery_plan(batch['id'])
    result = batches.start(batch['id'], dict(request_id=str(uuid.uuid4()), confirmed=True,
        rights_confirmed=False, fingerprint=plan['fingerprint']))
    assert result['status'] == 'running'
    assert len(review_env.launches) == 1
    assert result['groups'][0]['items'][0]['recipe']['delivery_mode'] == mode


def test_pause_before_precreated_collection_child_start_prevents_remote_execution(review_env, monkeypatch):
    uid = review_env.project()
    batch = start(create([uid]))
    group = batch['groups'][0]
    batches._save_group(batch['id'], group['id'], kind='collection', child_id='precreated-child')
    remote_starts = []
    child = dict(id='precreated-child', status='ready', phase='待确认', error='', items=group['items'])

    def child_pause(key):
        child['status'] = 'paused'
        return copy.deepcopy(child)

    def child_start(key, body):
        remote_starts.append(key)
        child['status'] = 'done'
        return copy.deepcopy(child)

    monkeypatch.setattr(batches.release_batches, 'get', lambda key: copy.deepcopy(child))
    monkeypatch.setattr(batches.release_batches, 'pause', child_pause)
    monkeypatch.setattr(batches.release_batches, 'start', child_start)
    monkeypatch.setattr(batches.release_batches, 'recovery_plan', lambda key: copy.deepcopy(group['plan']))
    batches.pause(batch['id'], {'group_id': group['id']})
    target, arguments = review_env.launches[0]
    target(*arguments)
    assert not remote_starts, '确认暂停后不能再启动尚未开始的合集子任务'


def test_invalid_old_parent_summary_does_not_break_every_project_card(review_env):
    uid = review_env.project()
    with Session(get_engine()) as session:
        session.add(ProjectBatch(request_id=str(uuid.uuid4()), status='paused', groups='broken JSON'))
        session.commit()
    assert batches.project_statuses([uid]) == {}


def test_frequent_listing_returns_progress_summary_without_frozen_plan_payload(review_env):
    uid = review_env.project()
    batch = create([uid])
    sentinel = '完整冻结配方只在详情页读取，不应每四秒传送'
    with Session(get_engine()) as session:
        row = session.get(ProjectBatch, batch['id'])
        plan, groups = json.loads(row.plan), json.loads(row.groups)
        plan['large_frozen_context'] = sentinel
        groups[0]['plan']['large_frozen_context'] = sentinel
        groups[0]['items'][0]['plan']['large_frozen_context'] = sentinel
        row.plan, row.groups = json.dumps(plan), json.dumps(groups)
        session.add(row)
        session.commit()
    listing = batches.listing()
    assert listing['batches'][0]['id'] == batch['id']
    assert listing['batches'][0]['groups'][0]['items'][0]['project_uid'] == uid
    assert sentinel not in json.dumps(listing, ensure_ascii=False, default=str)
    assert sentinel in json.dumps(batches.get(batch['id']), ensure_ascii=False)


def test_queued_version_display_and_encoding_overrides_remain_editable(review_env):
    uid = review_env.project()
    start(create([uid]))
    versions = configuration.resolve(uid)['values']['assembly.versions']
    versions[0]['name'] = '排队时完善的名称'
    versions[0]['overrides']['assemble.crf'] = 25
    save('project:' + uid, {'assembly.versions': versions})
    actual = configuration.resolve(uid)['values']['assembly.versions'][0]
    assert actual['name'] == '排队时完善的名称'
    assert actual['overrides']['assemble.crf'] == 25


def test_queued_version_output_kind_is_frozen(review_env):
    uid = review_env.project()
    start(create([uid]))
    versions = configuration.resolve(uid)['values']['assembly.versions']
    versions[0]['dubbing'] = not versions[0]['dubbing']
    with pytest.raises(DubForgeError):
        save('project:' + uid, {'assembly.versions': versions})


def test_collection_recovery_keeps_frozen_goals_after_ordinary_parameters_change(targets):
    batch = create(targets['uids'])
    stored = copy.deepcopy(batch['groups'][0]['items'])
    uid = targets['uids'][0]
    save('project:' + uid, {'assemble.crf': 25})
    plan = batches.recovery_plan(batch['id'])
    assert not plan['blockers'], plan['blockers']
    actual = plan['groups'][0]['plan']['items']
    assert [item['recipe']['mode'] for item in actual] == ['publish'] * len(stored)
    assert [item['recipe']['delivery_mode'] for item in actual] == ['publish'] * len(stored)
    assert [item['recipe']['version_ids'] for item in actual] == [item['recipe']['version_ids'] for item in stored]
    assert [item['project_uid'] for item in actual] == targets['uids']
    assert not targets['started'] and not targets['orders']


def test_concurrent_create_reserves_request_before_preflight_and_rejects_changed_selection(review_env, monkeypatch):
    uids = [review_env.project(), review_env.project()]
    body = dict(project_uids=uids, order_confirmed=True)
    plan = batches.preview(body)
    body.update(request_id=str(uuid.uuid4()), confirmed=True, fingerprint=plan['fingerprint'])
    entered, release = threading.Event(), threading.Event()
    results, errors, preflights = [], [], []

    def delayed(_body):
        preflights.append(1)
        entered.set()
        assert release.wait(8)
        return copy.deepcopy(plan)

    def create_in_background():
        try:
            results.append(batches.create(body))
        except Exception as error:
            errors.append(error)

    monkeypatch.setattr(batches, 'preview', delayed)
    worker = threading.Thread(target=create_in_background)
    worker.start()
    try:
        assert entered.wait(5)
        inflight = batches.create(body)
        assert inflight['status'] == 'creating'
        assert batches.listing(body['request_id'])['batches'][0]['id'] == inflight['id']
        assert not batches.listing()['batches'], '未完成的创建回执不是可执行任务'
        with pytest.raises(DubForgeError) as rejected:
            batches.create({**body, 'project_uids': list(reversed(uids))})
        assert rejected.value.code == 'PROJECT_BATCH_CONFLICT'
        with pytest.raises(DubForgeError):
            batches.recovery_plan(inflight['id'])
        assert not review_env.launches
    finally:
        release.set()
        worker.join(8)
    assert not worker.is_alive() and not errors
    assert results[0]['status'] == 'ready'
    assert results[0]['id'] == inflight['id']
    assert len(preflights) == 1
    with Session(get_engine()) as session:
        assert len(session.exec(select(ProjectBatch)).all()) == 1


def test_late_preflight_failure_cannot_replace_interrupted_terminal_receipt(review_env, monkeypatch):
    uid = review_env.project()
    body = dict(project_uids=[uid], order_confirmed=True)
    plan = batches.preview(body)
    body.update(request_id=str(uuid.uuid4()), confirmed=True, fingerprint=plan['fingerprint'])
    entered, release = threading.Event(), threading.Event()
    errors = []

    def delayed_failure(_body):
        entered.set()
        assert release.wait(8)
        raise batches.ProjectBatchError('晚到的预检失败', code='LATE_FIXTURE_FAILURE')

    def create_in_background():
        try:
            batches.create(body)
        except Exception as error:
            errors.append(error)

    monkeypatch.setattr(batches, 'preview', delayed_failure)
    worker = threading.Thread(target=create_in_background)
    worker.start()
    try:
        assert entered.wait(5)
        batches.recover()
        before = batches.listing(body['request_id'])['batches'][0]
        assert before['status'] == 'creation_failed'
        assert before['creation_error']['code'] == 'PROJECT_BATCH_CREATE_INTERRUPTED'
    finally:
        release.set()
        worker.join(8)
    assert not worker.is_alive() and errors
    after = batches.listing(body['request_id'])['batches'][0]
    assert after['creation_error'] == before['creation_error']
    assert batches.create(body)['status'] == 'creation_failed'
    assert not review_env.launches and not batches._leases


def test_persisted_ready_parent_is_not_misreported_failed_when_child_registration_receipt_is_lost(targets, monkeypatch):
    body = dict(project_uids=targets['uids'], order_confirmed=True)
    plan = batches.preview(body)
    body.update(request_id=str(uuid.uuid4()), confirmed=True, fingerprint=plan['fingerprint'])
    original = batches.release_batches.create_prepared
    interrupted = []

    def child_then_lose_receipt(*args, **kwargs):
        child = original(*args, **kwargs)
        if not interrupted:
            interrupted.append(child['id'])
            raise RuntimeError('隔离模拟：子批次已登记但回执丢失')
        return child

    monkeypatch.setattr(batches.release_batches, 'create_prepared', child_then_lose_receipt)
    with pytest.raises(RuntimeError):
        batches.create(body)
    receipt = batches.listing(body['request_id'])['batches'][0]
    assert receipt['status'] == 'ready', '父方案已落盘后不能谎报未创建并允许另建'
    replay = batches.create(body)
    assert replay['id'] == receipt['id']
    assert replay['groups'][0]['child_id'] == interrupted[0]
    assert len(batches.release_batches.listing(batches._child_request(replay['id'], replay['groups'][0]['id']))['batches']) == 1
    assert not targets['started'] and not targets['orders']


@pytest.fixture
def receipt_recovery(setup_publish, monkeypatch):
    """用真实 workflow_plan 和 service.view 构造已投稿、批次尚未记完的窗口。"""
    from dubforge import release_batches
    from dubforge.domain import crud
    from dubforge.publishing import service, studio
    from dubforge.release_batch_models import ReleaseBatch
    from dubforge.runner import compute_fingerprint

    uid, pub, artifacts, calls = setup_publish
    scope = dict(account_id='123', season_id=1, section_id=2)
    source = crud.project_dir(uid) / 'media' / 'video.mp4'
    source.write_bytes(b'isolated-source-video')
    project = crud.get_project(uid)
    crud.add_artifact(project.id, 'download', 'video', source.name, source, str(source.relative_to(crud.project_dir(uid))))
    save('project:' + uid, {'assembly.versions': [dict(id='source', name='原声目标', dubbing=False,
        burn_subtitles=False, include_comments=False, overrides={})], 'assembly.target_version_ids': ['source'],
        'publish.version_ids': ['source'], 'run.mode': 'publish', 'publish.project_target': {'kind': 'new'},
        'publish.account_id': '123', 'publish.season_id': 1, 'publish.section_id': 2,
        'publish.ai_copy': False, 'publish.cover_source': 'custom', 'publish.cover_id': pub['draft']['cover_id'],
        'content_review.enabled': False,
        **{'publish.' + key: value for key, value in pub['draft']['overrides'].items()}})
    step = crud.get_step(project.id, 'download')
    step.status, step.input_fingerprint = 'done', compute_fingerprint(project, 'download')
    crud.save_step(step)
    # 发布快照走真实plan，不用漏字段的手写snapshot遮挡view与恢复合同。
    frozen_publication = service.plan(pub['id'])['snapshot']
    assert frozen_publication['origins'] and frozen_publication['parts']
    with Session(get_engine()) as session:
        session.delete(session.get(Publication, pub['id']))
        session.commit()
    episodes = []
    def collection(**options):
        value = dict(section=dict(id=2, season_id=1, type=0, title='测试小节'), episodes=copy.deepcopy(episodes))
        return {**value, 'fingerprint': digest(value)}
    monkeypatch.setattr(service, 'read_collection_order', collection)
    release_batches.recover()
    candidate = release_batches.preview(dict(project_uids=[uid], target_driven=True, order_confirmed=True), online=False)
    assert not candidate['blockers'], candidate['blockers']
    assert candidate['items'][0]['plan']['configuration_snapshot']['schema'] == 2
    assert candidate['items'][0]['plan']['origins'], '本测试必须穿过真实 workflow_plan'
    with Session(get_engine()) as session:
        child = ReleaseBatch(request_id=str(uuid.uuid4()), scope=json.dumps(scope),
            plan=json.dumps(candidate), items=json.dumps(candidate['items']))
        session.add(child)
        session.commit()
        session.refresh(child)
        child_id = child.id
        publication_id = 'own-receipt-' + uid
        session.add(Publication(id=publication_id, project_uid=uid, status='published',
            draft=json.dumps(pub['draft']), snapshot=json.dumps(frozen_publication),
            receipt=json.dumps(dict(bvid='BV1234567890', aid=222, collection_joined=True))))
        run = WorkflowRun(project_uid=uid, request_id=str(uuid.uuid4()), status='completed',
            publication_id=publication_id, recipe=json.dumps(candidate['items'][0]['recipe']),
            plan=json.dumps(candidate['items'][0]['plan']))
        session.add(run)
        session.commit()
        session.refresh(run)
        run_id = run.id
    release_batches._save_item(child_id, 0, workflow_id=run_id, publication_id='', status='monitoring')
    episodes.append(dict(id=222, aid=222, cid=1000, bvid='BV1234567890', title='本批已投稿', order=0))
    monkeypatch.setattr(studio, 'read', lambda account, action, body, **options:
        dict(mid='123', aid=222, bvid='BV1234567890', state=0))
    monkeypatch.setattr(service, 'verify_collection_order', lambda **options: dict(verified=True))
    monkeypatch.setattr(service, 'apply_collection_order', lambda **options: dict(verified=True, changed=False))
    launches = []
    def forbid_launch(*args, **kwargs):
        launches.append(args)
        raise AssertionError('已有本批BV不能重新启动制作或投稿流程')
    monkeypatch.setattr(workflows, '_launch', forbid_launch)
    yield SimpleNamespace(uid=uid, child_id=child_id, run_id=run_id, publication_id=publication_id,
                          calls=calls, launches=launches, scope=scope, collection=collection)
    from test_release_batches import finish
    for key in list(release_batches._running):
        release_batches.stop(key)
        finish(key)


def test_recovery_follows_own_workflow_receipt_when_item_publication_save_was_lost(receipt_recovery):
    from dubforge import release_batches
    from test_release_batches import finish
    env = receipt_recovery
    plan = release_batches.recovery_plan(env.child_id)
    assert not plan['blockers'], plan['blockers']
    item = plan['items'][0]
    assert item['publication_id'] == env.publication_id
    assert item['publication_action'] == 'continue'
    assert not item['publication_rebound']
    release_batches.start(env.child_id, dict(request_id=str(uuid.uuid4()), confirmed=True,
        rights_confirmed=True, fingerprint=plan['fingerprint']))
    result = finish(env.child_id)
    assert result['status'] == 'done', result['error']
    assert result['items'][0]['publication_id'] == env.publication_id
    assert not env.launches
    assert not any(operation in {'upload', 'submit'} for operation, _ in env.calls)


def test_recovery_ignores_a_newer_unrelated_bv_and_keeps_original_bound_workflow(receipt_recovery):
    from dubforge import release_batches
    from dubforge.publishing import service
    env = receipt_recovery
    own = service.view(env.publication_id)
    with Session(get_engine()) as session:
        session.add(Publication(id='unrelated-newer', project_uid=env.uid, status='published',
            draft=json.dumps(own['draft']), snapshot=json.dumps(own['snapshot']),
            receipt=json.dumps(dict(bvid='BV9999999999', aid=999, collection_joined=True))))
        session.commit()
    plan = release_batches.recovery_plan(env.child_id, online=False)
    assert not plan['blockers'], plan['blockers']
    assert plan['items'][0]['publication_id'] == env.publication_id
    assert plan['items'][0]['publication']['bvid'] == 'BV1234567890'
    assert not env.launches


def test_fresh_batch_cannot_adopt_historical_bv_without_saved_own_workflow(receipt_recovery):
    from dubforge import release_batches
    env = receipt_recovery
    # 历史项目未明确新稿时不能接管旧BV；明确选择新稿另有独立授权回归。
    current = configuration.resolve(env.uid)
    configuration.save('project:' + env.uid, dict(revision=current['revision'], fingerprint=current['fingerprint'],
        reset=['publish.project_target']))
    plan = release_batches.preview(dict(project_uids=[env.uid], target_driven=True, order_confirmed=True), online=False)
    assert plan['blockers']
    assert any('已有BV' in issue or '另发新BV' in issue for issue in plan['blockers'])
    assert not env.launches
