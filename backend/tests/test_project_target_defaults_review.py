"""首稿缺省目标独立审查；只用隔离数据库，不启动模型、服务或真实投稿。"""
import datetime
import json
import threading
import uuid

import pytest
from sqlmodel import Session, select

from dubforge import configuration, project_batches, workflow_plan, workflows
from dubforge.configuration_models import ConfigurationScope
from dubforge.db import get_engine
from dubforge.errors import DubForgeError
from dubforge.publishing import transport
from dubforge.publishing.models import Publication, PublicationUpdate
from dubforge.workflow_models import WorkflowRun
from test_release_batch_targets import targets
from test_release_batches import batch_env


def save(uid, updates):
    data = configuration.resolve(uid)
    return configuration.save('project:' + uid, dict(revision=data['revision'],
        fingerprint=data['fingerprint'], updates=updates))


@pytest.fixture
def target_project(make_project, monkeypatch):
    def no_remote(*args, **kwargs):
        pytest.fail('目标解析不能访问真实平台')

    monkeypatch.setattr(transport, 'call', no_remote)
    monkeypatch.setattr(workflow_plan, 'production_checks', lambda *args, **kwargs: [])

    def create(mode='publish'):
        uid = make_project()
        save(uid, {'assembly.versions': [dict(id='source', name='原片', dubbing=False,
            burn_subtitles=False, include_comments=False, overrides={})],
            'assembly.target_version_ids': ['source'], 'run.mode': mode,
            'publish.ai_copy': False, 'content_review.enabled': False})
        return uid

    return create


def publication(uid, *, status='draft', lifecycle='active'):
    now = datetime.datetime.now(datetime.timezone.utc)
    with Session(get_engine()) as session:
        row = Publication(project_uid=uid, status=status,
            draft=json.dumps(dict(account_id='', template_id='', overrides={})),
            archived_at=now if lifecycle == 'archived' else None,
            deleted_at=now if lifecycle == 'deleted' else None)
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def target_issue(spec):
    return any(row['code'] == 'PROJECT_PUBLISH_TARGET_REQUIRED'
               for row in spec['project_goal_issues'])


@pytest.mark.parametrize('mode', ['draft', 'upload', 'publish'])
def test_first_delivery_defaults_new_without_saving_project_override(target_project, mode):
    uid = target_project(mode)
    before = configuration.resolve(uid)
    with Session(get_engine()) as session:
        stored = session.get(ConfigurationScope, 'project:' + uid).values
    spec = configuration.run_spec(uid, target_driven=True)
    assert spec['mode'] == spec['delivery_mode'] == mode
    assert not target_issue(spec)
    after = configuration.resolve(uid)
    assert after['target_selection']['source'] == 'first_publish'
    assert after['values']['publish.project_target'] == {'kind': 'new'}
    assert 'publish.project_target' not in after['overrides']
    assert 'publish.project_target' not in after['configured']
    assert after['revision'] == before['revision']
    with Session(get_engine()) as session:
        assert session.get(ConfigurationScope, 'project:' + uid).values == stored


@pytest.mark.parametrize('status,lifecycle', [
    ('draft', 'active'), ('uploaded', 'active'), ('unknown', 'active'),
    ('published', 'archived'), ('draft', 'deleted'), ('unrecognized', 'deleted'),
])
def test_any_publication_history_disables_implicit_new(target_project, status, lifecycle):
    uid = target_project()
    publication(uid, status=status, lifecycle=lifecycle)
    spec = configuration.run_spec(uid, target_driven=True)
    assert target_issue(spec)
    assert configuration.resolve(uid)['values']['publish.project_target'] is None


@pytest.mark.parametrize('mode', ['draft', 'upload', 'publish'])
def test_explicit_unset_does_not_reenable_automatic_new(target_project, mode):
    uid = target_project(mode)
    save(uid, {'publish.project_target': None})
    data = configuration.resolve(uid)
    assert data['target_selection']['source'] == 'explicit_unset'
    assert data['overrides']['publish.project_target'] is None
    assert target_issue(configuration.run_spec(uid, target_driven=True))


def test_explicit_new_remains_new_despite_archived_history(target_project):
    uid = target_project()
    publication(uid, status='published', lifecycle='archived')
    save(uid, {'publish.project_target': {'kind': 'new'}})
    spec = configuration.run_spec(uid, target_driven=True)
    assert not target_issue(spec)
    assert spec['mode'] == 'publish' and spec.get('confirm_new_publication') is True


@pytest.mark.parametrize('kind', ['workflow', 'update'])
def test_orphan_publication_references_still_require_choice(target_project, kind):
    uid = target_project()
    with Session(get_engine()) as session:
        row = (WorkflowRun(project_uid=uid, request_id=str(uuid.uuid4()), status='completed',
                           publication_id='missing-publication') if kind == 'workflow' else
               PublicationUpdate(project_uid=uid, source_publication_id='missing-publication',
                                 account_id='fixture-account', bvid='BVfixture001', status='completed'))
        session.add(row)
        session.commit()
    assert target_issue(configuration.run_spec(uid, target_driven=True))


def test_produce_endpoint_is_never_promoted_by_new_default_or_history(target_project):
    uid = target_project('produce')
    first = configuration.run_spec(uid, target_driven=True)
    publication(uid, status='unknown')
    later = configuration.run_spec(uid, target_driven=True)
    assert first['mode'] == later['mode'] == 'produce'
    assert first['delivery_mode'] == later['delivery_mode'] == 'produce'
    assert not target_issue(first) and not target_issue(later)
    assert not first.get('confirm_new_publication') and not later.get('confirm_new_publication')


def test_template_baseline_does_not_capture_a_first_publish_default(target_project):
    first, second = target_project(), target_project()
    captured = configuration.baseline_from(configuration.resolve(first))
    assert captured['values']['publish.project_target'] is None
    assert 'publish.project_target' not in captured['configured']
    baseline = configuration.resolve(second)['baseline']
    # 恢复历史清单时，即使有人把首稿有效值塞进基础设置，也不能继承它。
    baseline['values']['publish.project_target'] = {'kind': 'new'}
    baseline['configured'].append('publish.project_target')
    publication(second, status='published')
    with Session(get_engine()) as session:
        row = session.get(ConfigurationScope, 'project:' + second)
        row.baseline = json.dumps(baseline)
        session.add(row)
        session.commit()
    assert configuration.resolve(second)['values']['publish.project_target'] is None
    assert target_issue(configuration.run_spec(second, target_driven=True))


@pytest.mark.parametrize('mode', ['draft', 'upload'])
def test_frozen_first_delivery_recipe_survives_later_history_without_promotion(target_project, mode):
    uid = target_project(mode)
    original = workflow_plan.recipe(configuration.run_spec(uid, target_driven=True))
    with Session(get_engine()) as session:
        row = WorkflowRun(project_uid=uid, request_id=str(uuid.uuid4()), status='paused',
                          recipe=json.dumps(original), plan='{}')
        session.add(row)
        session.commit()
        session.refresh(row)
        key = row.id
    publication(uid, status='draft')
    save(uid, {'run.mode': 'publish'})
    assert target_issue(configuration.run_spec(uid, target_driven=True))
    resumed = workflows._plan(uid, dict(resume_id=key, target_driven=True, mode='publish'), online=False)
    assert resumed['recipe']['mode'] == resumed['recipe']['delivery_mode'] == mode
    assert not target_issue(resumed['recipe'])
    assert resumed['recipe']['production_version_ids'] == original['production_version_ids']
    assert resumed['recipe'].get('confirm_new_publication') == original.get('confirm_new_publication')


@pytest.fixture
def local_start_plan(monkeypatch):
    """保留真实目标解析和入队锁，只替换媒体/平台预检与最终执行边界。"""
    def plan(uid, spec, **kwargs):
        recipe = workflow_plan.recipe(spec)
        return dict(recipe=recipe, steps=[], labels={}, version_specs=[], warnings=[],
                    blockers=[row['message'] for row in recipe.get('project_goal_issues', [])],
                    fingerprint=workflow_plan.digest(recipe))
    monkeypatch.setattr(workflows, 'plan', plan)
    launches = []
    monkeypatch.setattr(workflows, '_launch', lambda *args: launches.append(args))
    return launches


def test_history_appearing_after_preview_invalidates_first_delivery_start(target_project, local_start_plan):
    uid = target_project()
    preview = workflows._plan(uid, {'target_driven': True}, online=False)
    assert not preview['blockers']
    publication(uid, status='draft')
    with pytest.raises(DubForgeError):
        workflows.start(uid, dict(recipe=preview['recipe'], fingerprint=preview['fingerprint'],
                                 request_id=str(uuid.uuid4()), confirmed=True))
    assert not local_start_plan
    with Session(get_engine()) as session:
        assert not session.exec(select(WorkflowRun).where(WorkflowRun.project_uid == uid)).all()


def test_same_start_request_returns_original_run_after_default_disappears(target_project, local_start_plan):
    uid = target_project('upload')
    preview = workflows._plan(uid, {'target_driven': True}, online=False)
    body = dict(recipe=preview['recipe'], fingerprint=preview['fingerprint'],
                request_id=str(uuid.uuid4()), confirmed=True)
    started = workflows.start(uid, body)
    publication(uid, status='draft')
    assert target_issue(configuration.run_spec(uid, target_driven=True))
    replayed = workflows.start(uid, body)
    assert replayed['id'] == started['id']
    assert replayed['recipe']['mode'] == 'upload'
    assert len(local_start_plan) == 1


def test_bound_draft_resume_cannot_create_another_publication(target_project, local_start_plan):
    uid = target_project('upload')
    frozen = workflow_plan.recipe(configuration.run_spec(uid, target_driven=True))
    publication_id = publication(uid, status='draft')
    with Session(get_engine()) as session:
        row = WorkflowRun(project_uid=uid, request_id=str(uuid.uuid4()), status='paused',
            recipe=json.dumps(frozen), plan='{}', publication_id=publication_id)
        session.add(row)
        session.commit()
        session.refresh(row)
        key = row.id
    with pytest.raises(DubForgeError, match='发布页'):
        workflows.resume(key, dict(confirmed=True, request_id=str(uuid.uuid4()), recipe={'mode': 'publish'}))
    assert not local_start_plan
    with Session(get_engine()) as session:
        rows = session.exec(select(Publication).where(Publication.project_uid == uid)).all()
        assert [row.id for row in rows] == [publication_id]


def test_explicit_saved_new_allows_batch_after_published_history(target_project, local_start_plan):
    uid = target_project()
    key = publication(uid, status='published')
    with Session(get_engine()) as session:
        row = session.get(Publication, key)
        row.receipt = json.dumps({'bvid': 'BVfixture001'})
        session.add(row)
        session.commit()
    save(uid, {'publish.project_target': {'kind': 'new'}})
    checked = project_batches.preview(dict(project_uids=[uid], order_confirmed=True), online=False)
    assert not checked['blockers'], checked['blockers']
    assert checked['groups'][0]['items'][0]['recipe']['mode'] == 'publish'


@pytest.mark.parametrize('target', [{}, {'kind': 'unsupported'}, 'new', [],
                                   {'kind': 'existing', 'publication_id': ''}])
def test_corrupt_saved_target_never_silently_becomes_new(target_project, target):
    uid = target_project()
    with Session(get_engine()) as session:
        row = session.get(ConfigurationScope, 'project:' + uid)
        patch = json.loads(row.values)
        patch['publish.project_target'] = target
        row.values = json.dumps(patch)
        session.add(row)
        session.commit()
    try:
        spec = configuration.run_spec(uid, target_driven=True)
    except DubForgeError:
        return
    assert target_issue(spec), '损坏的显式选择必须要求核对，不能退化成新建稿件'


def ready_batch(uid):
    body = dict(project_uids=[uid], order_confirmed=True)
    checked = project_batches.preview(body, online=False)
    assert not checked['blockers'], checked['blockers']
    result = project_batches.create({**body, 'request_id': str(uuid.uuid4()),
        'confirmed': True, 'fingerprint': checked['fingerprint']})
    assert result['status'] == 'ready'
    return result


@pytest.mark.parametrize('status', ['draft', 'unknown'])
def test_history_appearing_after_batch_creation_blocks_first_child(target_project, local_start_plan, status):
    uid = target_project()
    batch = ready_batch(uid)
    publication(uid, status=status)
    checked = project_batches.recovery_plan(batch['id'], online=False)
    assert checked['blockers'], '创建好的首稿批次不能忽略确认后新出现的发布历史'
    assert not local_start_plan


@pytest.mark.parametrize('status', ['draft', 'unknown'])
def test_frozen_batch_child_rechecks_history_inside_start_lock(target_project, local_start_plan,
                                                              monkeypatch, status):
    uid = target_project()
    batch = ready_batch(uid)
    group = batch['groups'][0]
    publication(uid, status=status)
    monkeypatch.setattr(project_batches, '_closing', False)
    project_batches._save_group(batch['id'], group['id'], status='running')
    project_batches._running[(batch['id'], group['id'])] = threading.Event()
    project_batches._leases[uid] = batch['id']
    try:
        with project_batches._owned(batch['id']):
            with pytest.raises(DubForgeError):
                workflows.start_prepared(uid, group['plan'],
                    project_batches._child_request(batch['id'], group['id']), frozen_target=True)
        assert not local_start_plan
    finally:
        project_batches._release(batch['id'], group['id'])


def test_batch_recovers_its_original_receipt_after_first_default_disappears(target_project, local_start_plan):
    uid = target_project('upload')
    batch = ready_batch(uid)
    group = batch['groups'][0]
    publication_id = publication(uid, status='submitted')
    with Session(get_engine()) as session:
        pub = session.get(Publication, publication_id)
        pub.receipt = json.dumps({'bvid': 'BVfixture001'})
        session.add(pub)
        row = WorkflowRun(project_uid=uid,
            request_id=project_batches._child_request(batch['id'], group['id']), status='paused',
            recipe=json.dumps(group['plan']['recipe']), plan=json.dumps(group['plan']),
            publication_id=publication_id)
        session.add(row)
        session.commit()
        session.refresh(row)
        workflow_id = row.id
    project_batches._save_group(batch['id'], group['id'], workflow_id=workflow_id)
    assert target_issue(configuration.run_spec(uid, target_driven=True))
    recovered = project_batches.recovery_plan(batch['id'], online=False)
    assert not recovered['blockers'], recovered['blockers']
    restored = recovered['groups'][0]
    assert restored['workflow_id'] == workflow_id
    assert restored['plan']['recovery']['publication_id'] == publication_id
    assert restored['plan']['recipe']['mode'] == 'upload'
    assert not local_start_plan


def test_saved_new_collection_batch_never_adopts_unrelated_old_drafts_on_recovery(targets):
    from dubforge import release_batches
    uid = targets['uids'][0]
    for status in ('draft', 'rejected'):
        key = publication(uid, status=status)
        targets['pubs'][key] = dict(id=key, project_uid=uid, revision=1, status=status,
            error='', progress=0, receipt={}, snapshot={},
            draft=dict(account_id='123', template_id='', overrides={}))
    body = dict(project_uids=[uid], target_driven=True, order_confirmed=True)
    checked = release_batches.preview(body, online=False)
    assert not checked['blockers'], checked['blockers']
    batch = release_batches.create({**body, 'request_id': str(uuid.uuid4()),
        'confirmed': True, 'fingerprint': checked['fingerprint']})
    recovered = release_batches.recovery_plan(batch['id'], online=False)
    assert not recovered['blockers'], recovered['blockers']
    assert recovered['items'][0]['publication_id'] == ''
    assert recovered['items'][0]['publication_action'] == 'prepare'
    assert not targets['started'] and not targets['orders']
