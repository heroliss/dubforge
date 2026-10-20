"""新顺序发布只编排已保存的项目目标；平台和制作均由夹具隔离。"""
import copy
import json
import uuid

import pytest
from sqlmodel import Session

from dubforge import configuration, release_batches as batches, workflows
from dubforge.errors import DubForgeError
from dubforge.db import get_engine
from dubforge.publishing.models import PublicationUpdate
from dubforge.workflow_models import WorkflowRun
from test_release_batches import batch_env, finish, begin
from test_configuration import save
from test_publication_updates import env
from test_publishing import setup_publish


@pytest.fixture
def targets(batch_env, monkeypatch):
    original = workflows.prepared_plan
    def plan(uid, spec, **options):
        result = original(uid, spec, **options)
        result['settings'] = dict(season_id=1, section_id=2)
        return result
    monkeypatch.setattr(workflows, 'prepared_plan', plan)
    for uid in batch_env['uids']:
        versions = configuration.resolve(uid)['values']['assembly.versions']
        save('project:' + uid, {'run.mode': 'publish', 'publish.account_id': '123',
            'publish.season_id': 1, 'publish.section_id': 2, 'publish.project_target': {'kind': 'new'},
            'assembly.target_version_ids': [versions[0]['id']]})
    return batch_env


def body(env):
    return dict(target_driven=True, project_uids=env['uids'], order_confirmed=True)


def test_new_batch_derives_saved_scope_and_preserves_each_recipe(targets):
    candidate = batches.preview(body(targets))
    assert not candidate['blockers'], candidate['blockers']
    assert candidate['target_driven'] is True
    assert candidate['scope'] == dict(account_id='123', season_id=1, section_id=2)
    assert all(item['recipe']['target_driven'] and item['recipe']['mode'] == 'publish' for item in candidate['items'])
    assert not targets['started'] and not targets['orders']
    rows = batches.candidates(targets['uids'], target_driven=True)
    assert rows['derived_scope'] == candidate['scope']
    assert all(row['project_goal'] and row['target_recipe'] for row in rows['items'])


def test_target_batch_preserves_each_saved_part_order_and_titles(targets):
    expected = {}
    for index, uid in enumerate(targets['uids']):
        ids = [version['id'] for version in configuration.resolve(uid)['values']['assembly.versions']][:2]
        selected = ids if index % 2 else list(reversed(ids))
        expected[uid] = selected
        save('project:' + uid, {'publish.version_ids': selected,
                               'publish.part_titles': {key: f'分P-{key}' for key in ids}})
    candidate = batches.preview(body(targets))
    assert not candidate['blockers'], candidate['blockers']
    for item in candidate['items']:
        selected = expected[item['project_uid']]
        assert item['recipe']['version_ids'] == item['selected_version_ids'] == selected
        assert [item['recipe']['part_titles'][key] for key in selected] == [f'分P-{key}' for key in selected]
    uid = targets['uids'][0]
    save('project:' + uid, {'publish.version_ids': list(reversed(expected[uid]))})
    assert batches.preview(body(targets))['fingerprint'] != candidate['fingerprint']
    with pytest.raises(DubForgeError):
        batches.create({**body(targets), 'request_id': str(uuid.uuid4()),
                        'fingerprint': candidate['fingerprint'], 'confirmed': True})
    assert not targets['started'] and not targets['orders']


@pytest.mark.parametrize('mode', ['produce', 'draft', 'upload'])
def test_batch_never_promotes_non_submission_projects(targets, mode):
    save('project:' + targets['uids'][1], {'run.mode': mode})
    candidate = batches.preview(body(targets))
    assert any('不会自动开启投稿' in issue for issue in candidate['blockers'])
    assert candidate['items'][1]['recipe']['mode'] == mode
    assert not targets['started']


def test_batch_requires_saved_same_scope_and_rejects_temporary_overrides(targets):
    save('project:' + targets['uids'][1], {'publish.section_id': 9})
    candidate = batches.preview(body(targets))
    assert any('发布步骤统一' in issue for issue in candidate['blockers'])
    for key, value in [('scope', candidate['scope']), ('version_ids', {}), ('dubbing', {}), ('publication_ids', {})]:
        with pytest.raises(DubForgeError, match='不再覆盖'):
            batches.preview({**body(targets), key: value})


def test_target_batch_create_idempotency_and_runs_saved_goals_in_order(targets):
    request = body(targets)
    candidate = batches.preview(request)
    creation = {**request, 'request_id': str(uuid.uuid4()), 'fingerprint': candidate['fingerprint'], 'confirmed': True}
    row = batches.create(creation)
    assert batches.create(creation)['id'] == row['id']
    assert row['plan']['target_driven'] is True
    begin(row)
    result = finish(row['id'])
    assert result['status'] == 'done', result['error']
    assert targets['started'] == targets['uids']
    assert len(targets['orders']) == 1


def test_saved_config_change_invalidates_create_confirmation(targets):
    request = body(targets)
    candidate = batches.preview(request)
    save('project:' + targets['uids'][0], {'run.mode': 'upload'})
    with pytest.raises(DubForgeError) as error:
        batches.create({**request, 'request_id': str(uuid.uuid4()), 'confirmed': True,
                        'fingerprint': candidate['fingerprint']})
    assert error.value.code == 'RELEASE_BATCH_PLAN_CHANGED'
    assert not targets['started']


@pytest.mark.parametrize('retry_delivery', [False, True])
def test_saved_existing_target_updates_original_before_sequential_item_completes(env, monkeypatch, retry_delivery):
    from dubforge.publishing import service, update_delivery
    from dubforge.workflow_plan import digest
    batches.recover()
    uid = env['uid']
    save('project:' + uid, {'run.mode': 'publish', 'publish.account_id': '123',
        'publish.season_id': 1, 'publish.section_id': 2, 'publish.description': '顺序任务中的原稿更新',
        'publish.project_target': dict(kind='existing', publication_id=env['source']['id'],
                                      replace_videos=False, replacements=[], sync_fields=['description'])})
    collection = dict(section=dict(id=2, season_id=1, type=0, title='测试小节'),
        episodes=[dict(id=222, aid=222, cid=1000, bvid=env['remote']['bvid'], title='原稿', order=0)])
    collection['fingerprint'] = digest(collection)
    monkeypatch.setattr(service, 'read_collection_order', lambda **options: collection)
    monkeypatch.setattr(service, 'verify_collection_order', lambda **options: {'verified': True})
    orders = []
    monkeypatch.setattr(service, 'apply_collection_order', lambda **options: orders.append(options))
    if retry_delivery:
        original_delivery = update_delivery.run
        attempts = []
        def fail_once(snapshot, execution, **options):
            attempts.append(1)
            if len(attempts) == 1:
                execution.update(outcome='failed', error='隔离的章节后处理暂时失败', phase='原稿更新后处理待继续')
                return execution
            return original_delivery(snapshot, execution, **options)
        monkeypatch.setattr(update_delivery, 'run', fail_once)
    request = dict(target_driven=True, project_uids=[uid], order_confirmed=True)
    candidate = batches.preview(request)
    assert not candidate['blockers'], candidate['blockers']
    assert candidate['items'][0]['publication_action'] == 'update_existing'
    assert not env['writes'] and not env['uploads']
    row = batches.create({**request, 'request_id': str(uuid.uuid4()), 'confirmed': True,
                          'fingerprint': candidate['fingerprint']})
    begin(row)
    result = finish(row['id'])
    if retry_delivery:
        assert result['status'] == 'attention'
        assert len(env['writes']) == 1
        begin(result)
        result = finish(row['id'])
    assert result['status'] == 'done', result['error']
    assert result['items'][0]['publication_id'] == env['source']['id']
    assert len(env['writes']) == 1 and not env['uploads']
    assert len(env['deliveries']) == 1


def test_verified_update_may_change_its_episode_cid_and_title_but_never_external_members(env):
    snapshot = dict(aid=222, bvid=env['remote']['bvid'], settings={'title': '本次确认的新标题'})
    with Session(get_engine()) as session:
        task = PublicationUpdate(source_publication_id=env['source']['id'], project_uid=env['uid'],
            account_id='123', bvid=env['remote']['bvid'], status='completed', plan=json.dumps(snapshot),
            fingerprint=batches.digest(snapshot),
            execution=json.dumps(dict(stages={'edit': {'status': 'completed'}}, final_parts=[{'cid': 2001}])))
        session.add(task);session.commit();session.refresh(task)
        run = WorkflowRun(project_uid=env['uid'], request_id=str(uuid.uuid4()), status='completed',
            recipe=json.dumps({'mode': 'update'}), plan=json.dumps({'publication_update_id': task.id}))
        session.add(run);session.commit();session.refresh(run)
        item = dict(status='done', publication_action='update_existing', workflow_id=run.id,
                    publication_id=env['source']['id'], publication={'aid': 222, 'bvid': env['remote']['bvid']})
    baseline = dict(section={'id': 2}, episodes=[
        dict(id=1, aid=222, cid=1000, bvid=env['remote']['bvid'], title='旧标题'),
        dict(id=2, aid=999, cid=9990, bvid='BVOther', title='外部成员')])
    current = copy.deepcopy(baseline)
    current['episodes'][0].update(cid=2001, title='本次确认的新标题')
    batches._check_collection_base(baseline, current, [item])
    assert batches._expected_order(dict(plan={'collection': baseline}, items=[item]), current) == [999, 222]
    for where, key, value in [(0, 'cid', 7777), (0, 'title', '外部修改标题'), (0, 'id', 55), (1, 'title', '外部变更')]:
        wrong = copy.deepcopy(current);wrong['episodes'][where][key] = value
        with pytest.raises(DubForgeError):
            batches._check_collection_base(baseline, wrong, [item])
    with pytest.raises(DubForgeError):
        batches._check_collection_base(baseline, current, [{**item, 'status': 'running'}])


def test_explicit_new_bv_choice_does_not_pick_a_historical_target(env):
    save('project:' + env['uid'], {'run.mode': 'publish', 'publish.account_id': '123',
        'publish.season_id': 1, 'publish.section_id': 2, 'publish.project_target': {'kind': 'new'}})
    row = batches.candidates([env['uid']], target_driven=True)['items'][0]
    assert row['publication_id'] == ''
    assert row['target_recipe']['mode'] == 'publish'
    assert row['target_recipe']['confirm_new_publication'] is True
    assert not any('另发新BV请先使用单项目运行明确确认' in issue for issue in row['blockers'])
