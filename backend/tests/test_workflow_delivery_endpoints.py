"""交付终点与确认授权：全部使用隔离数据库和伪平台，绝不真实投稿。"""
import json
import uuid

import pytest
from sqlmodel import Session

from dubforge import workflows, workflow_plan, workflow_update
from dubforge.db import get_engine
from dubforge.errors import DubForgeError
from dubforge.publishing import updates, service
from dubforge.workflow_models import WorkflowRun
from test_publishing import setup_publish
from test_publication_updates import env, prepare, start, finish
from test_workflows import flow, configure, finish as finish_workflow, start as start_workflow
from test_configuration import save


def test_update_upload_endpoint_requires_separate_submit_and_reuses_upload(env):
    task = prepare(env, delivery_mode='upload', values={'description': '仅上传后待确认'})
    assert task['delivery_mode'] == 'upload'
    uploaded = start(task)
    assert uploaded['status'] == 'uploaded'
    assert len(env['uploads']) == 1 and not env['writes'] and not env['deliveries']
    assert uploaded['execution']['stages']['edit']['status'] == 'waiting_confirmation'
    # 普通继续只延续上传范围，不能把当前设置或一次重试解释成投稿确认。
    assert start(uploaded, 'retry')['status'] == 'uploaded'
    assert len(env['uploads']) == 1 and not env['writes']
    with pytest.raises(DubForgeError):
        updates.action(task['id'], dict(action='submit', fingerprint=task['fingerprint']))
    result = start(uploaded, 'submit')
    assert result['status'] == 'completed'
    assert len(env['uploads']) == 1 and len(env['writes']) == 1
    assert len(env['deliveries']) == 1


def test_upload_authorization_survives_restart_and_cannot_submit_changed_remote(env):
    task = prepare(env, delivery_mode='upload')
    uploaded = start(task)
    updates.recover()
    assert updates.view(task['id'])['status'] == 'uploaded'
    env['remote']['version'] = 'changed-after-upload'
    result = start(uploaded, 'submit')
    assert result['status'] == 'failed'
    assert result['error_code'] == 'PUBLISH_REMOTE_CHANGED'
    assert len(env['uploads']) == 1 and not env['writes']


def test_prepared_update_draft_cannot_start_upload_or_submit(env):
    task = prepare(env, delivery_mode='draft')
    assert task['status'] == 'prepared' and task['delivery_mode'] == 'draft'
    for operation in ('start', 'retry', 'submit'):
        with pytest.raises(DubForgeError) as failure:
            updates.action(task['id'], dict(action=operation, confirmed=True, fingerprint=task['fingerprint']))
        assert failure.value.code == 'PUBLISH_UPDATE_DELIVERY_LIMIT'
    assert not env['uploads'] and not env['writes']


def test_metadata_only_upload_waits_before_edit_or_followup(env):
    task = updates.plan(env['source']['id'], dict(delivery_mode='upload', values={'description': '待确认简介'}))
    result = start(task)
    assert result['status'] == 'uploaded'
    assert not env['uploads'] and not env['writes'] and not env['deliveries']
    assert start(result, 'submit')['status'] == 'completed'
    assert not env['uploads'] and len(env['writes']) == 1


def test_submit_cannot_skip_upload_endpoint(env):
    task = prepare(env, delivery_mode='upload')
    with pytest.raises(DubForgeError) as failure:
        updates.action(task['id'], dict(action='submit', confirmed=True, fingerprint=task['fingerprint']))
    assert failure.value.code == 'PUBLISH_UPDATE_CONFIRM'
    assert not env['uploads'] and not env['writes']


@pytest.mark.parametrize('add_parts,expected', [(False, ['a']), (True, ['b', 'a'])])
def test_update_delivery_does_not_promote_unpublished_production_targets(add_parts, expected):
    versions = [dict(id=key) for key in ('a', 'b', 'local-only')]
    spec = dict(target_driven=True, version_ids=['b'], production_version_ids=['a', 'b', 'local-only'],
        update=dict(replacements=[dict(old_cid=11, version_id='a')],
                    sync_fields=['add_parts'] if add_parts else []))
    assert [row['id'] for row in workflow_update._definitions('unused', spec, versions)] == expected


def test_target_driven_update_never_guesses_an_empty_replacement_mapping(env, monkeypatch):
    monkeypatch.setattr(workflow_update, '_versions', lambda *args: pytest.fail('不得猜测原分P对应关系'))
    spec = dict(target_driven=True, version_ids=['a'], update=dict(publication_id=env['source']['id'],
        replace_videos=True, replacements=[], sync_fields=[]))
    candidate = workflow_update.prepare(env['uid'], spec, [dict(id='a', name='版本A')])
    assert all(not part['replaced'] for part in candidate['parts'])
    assert candidate['blockers']
    assert not env['uploads'] and not env['writes']


def test_new_publication_upload_stops_without_submission(flow):
    uid, spec, calls, _sequence = flow
    row, _body = start_workflow(uid, {**spec, 'mode': 'upload'})
    result = finish_workflow(row['id'])
    assert result['status'] == 'ready', result
    assert result['publication']['status'] == 'uploaded'
    assert not result['publication']['receipt'].get('bvid')
    assert any(operation == 'upload' for operation, _ in calls)
    assert not any(operation in {'submit', 'collection'} for operation, _ in calls)
    publication = service.view(result['publication_id'])
    service.enqueue(publication['id'], dict(action='submit', confirmed=True, revision=publication['revision'],
        fingerprint=service._hash(publication['snapshot'])))
    workflows._wait_publication(publication['id'], None)
    parent = workflows.get(row['id'])
    assert parent['status'] == 'monitoring'
    assert parent['recipe']['mode'] == 'upload'
    assert any(operation == 'submit' for operation, _ in calls)


def test_completed_production_progress_reports_all_steps_done(flow):
    uid, spec, _calls, _sequence = flow
    row, _ = start_workflow(uid, {**spec, 'mode': 'produce'})
    result = finish_workflow(row['id'])
    assert result['status'] == 'completed'
    assert result['execution']['completed'] == len(result['plan']['steps'])
    assert result['execution']['total'] == len(result['plan']['steps'])


@pytest.mark.parametrize('mode', ['produce', 'draft', 'upload'])
def test_resume_keeps_frozen_delivery_even_when_current_project_enables_publish(flow, mode):
    uid, spec, _calls, _sequence = flow
    saved = workflow_plan.recipe({**spec, 'mode': mode, 'production_steps': ['preprocess']})
    with Session(get_engine()) as session:
        row = WorkflowRun(project_uid=uid, request_id=str(uuid.uuid4()), status='paused',
                          recipe=json.dumps(saved), plan='{}')
        session.add(row)
        session.commit()
        session.refresh(row)
        key = row.id
    configure(uid, {**spec, 'mode': 'publish'})
    candidate = workflows._plan(uid, dict(resume_id=key, mode='publish', production_steps=['download', 'tts']),
                                online=False)
    assert candidate['recipe']['mode'] == mode
    assert candidate['recipe']['production_steps'] == ['preprocess']
    assert candidate['recipe']['account_id'] == saved['account_id']


def test_upload_retry_after_partial_failure_keeps_endpoint(env, monkeypatch):
    from dubforge.publishing import transport
    task = updates.plan(env['source']['id'], dict(delivery_mode='upload', replacements=[
        dict(old_cid=1000 + i, artifact_id=artifact.id) for i, artifact in enumerate(env['artifacts'][:2])]))
    original = transport.call
    attempts = []
    def upload(operation, payload=None, **options):
        if operation == 'upload':
            attempts.append(payload['artifact_id'])
            if len(attempts) == 2:
                raise updates.pub.PublishError('隔离上传中断')
        return original(operation, payload, **options)
    monkeypatch.setattr(transport, 'call', upload)
    assert start(task)['status'] == 'failed'
    result = start(task, 'retry')
    assert result['status'] == 'uploaded'
    assert attempts == [env['artifacts'][0].id, env['artifacts'][1].id, env['artifacts'][1].id]
    assert not env['writes'] and not env['deliveries']


@pytest.mark.parametrize('mode,expected', [('draft', 'prepared'), ('upload', 'uploaded'), ('publish', 'completed')])
def test_project_existing_metadata_delivery_uses_saved_endpoint_without_production(env, mode, expected):
    save('project:' + env['uid'], {'run.mode': mode, 'publish.description': '项目保存的更新文案',
        'publish.project_target': dict(kind='existing', publication_id=env['source']['id'],
            replace_videos=False, replacements=[], sync_fields=['description'])})
    candidate = workflows.preview(env['uid'], {'target_driven': True})
    assert not candidate['blockers'], candidate['blockers']
    assert candidate['recipe']['mode'] == 'update'
    assert candidate['recipe']['delivery_mode'] == mode
    assert candidate['steps'] == []
    goal = workflows.status(env['uid'])['project_goal']
    assert goal['production_required'] is False and goal['target_blockers'] == []
    assert goal['target_kind'] == 'existing' and goal['target_bvid'] == env['remote']['bvid']
    row = workflows.start(env['uid'], dict(recipe=candidate['recipe'], fingerprint=candidate['fingerprint'],
        confirmed=True, request_id=str(uuid.uuid4())))
    run = finish_workflow(row['id'])
    task = finish(run['update_id'])
    assert task['status'] == expected, task
    workflows._sync_update_task(run['id'], task)
    assert workflows.get(run['id'])['status'] == ('completed' if mode == 'publish' else 'ready')
    assert not env['uploads']
    assert len(env['writes']) == (1 if mode == 'publish' else 0)
    assert len(env['deliveries']) == (1 if mode == 'publish' else 0)
    if mode == 'upload':
        assert start(task, 'submit')['status'] == 'completed'
        assert workflows.get(run['id'])['status'] == 'monitoring'
        assert workflows.get(run['id'])['recipe']['delivery_mode'] == 'upload'
        workflows._update(run['id'], next_check=0)
        workflows.monitor_once()
        assert workflows.get(run['id'])['status'] == 'completed'
