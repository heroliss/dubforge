"""批次先制作再重绑同一草稿；旧审查产物绝不能绕过发布边界。"""
import copy
import json
import threading
import uuid

import pytest
from sqlmodel import Session, select

from dubforge import configuration, release_batches as batches, workflows
from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.publishing import service
from dubforge.publishing.models import Publication
from dubforge.runner import runner
from dubforge.release_batch_models import ReleaseBatch
from dubforge.workflow_models import WorkflowRun
from test_configuration import save
from test_publishing import setup_publish
from test_release_batches import batch_env, SCOPE
from test_workflows import flow


def _draft(env, *, review=False):
    uid = env['uids'][0]
    draft = dict(account_id='', parts=[], overrides={'title': '保留的人工标题'}, cover_id='', template_id='')
    if review:
        draft['review_source'] = dict(mode='new_submission', source_bvid='BV1111111111', prepared_parts=[])
        draft['parts'] = [dict(artifact_id=9999, title='已经失效的审查修订')]
    pub = dict(id='chosen-draft', project_uid=uid, revision=1, status='draft', draft=draft,
               snapshot={}, receipt={}, error='', progress=0)
    env['pubs'][pub['id']] = pub
    with Session(get_engine()) as session:
        session.add(Publication(id=pub['id'], project_uid=uid, draft=json.dumps(draft)))
        session.commit()
    return pub


@pytest.mark.parametrize('review', [False, True])
def test_empty_or_stale_review_draft_plans_production_without_old_file_precheck(batch_env, monkeypatch, review):
    pub = _draft(batch_env, review=review)
    monkeypatch.setattr(service, 'plan', lambda *_: (_ for _ in ()).throw(AssertionError('不能提前预检旧成片')))
    body = dict(project_uids=[pub['project_uid']], scope=SCOPE, order_confirmed=True)
    before = copy.deepcopy(pub)
    result = batches.preview(body)
    item = result['items'][0]
    assert not result['blockers']
    assert item['publication_action'] == 'prepare'
    assert item['plan']['steps'] == ['download']
    assert item['plan']['refresh_publication'] == pub['id']
    assert item['recipe']['overrides']['title'] == '保留的人工标题'
    assert item['production']['execute_steps'] == ['download']
    assert pub == before


def test_explicit_versions_and_actions_are_frozen_in_creation_receipt(batch_env):
    uid = batch_env['uids'][0]
    save('project:' + uid, {'publish.version_ids': ['src']})
    candidate = batches.candidates([uid])['items'][0]
    assert candidate['selected_version_ids'] == ['src']
    assert {version['id'] for version in candidate['versions']} == {'src', 'dub'}
    body = dict(project_uids=[uid], scope=SCOPE, order_confirmed=True,
                version_ids={uid: ['dub']}, publication_actions={uid: 'prepare'})
    plan = batches.preview(body)
    request = str(uuid.uuid4())
    exact = dict(body, fingerprint=plan['fingerprint'], request_id=request, confirmed=True)
    row = batches.create(exact)
    assert row['items'][0]['recipe']['version_ids'] == ['dub']
    assert batches.create(exact)['id'] == row['id']
    with pytest.raises(DubForgeError, match='不同方案'):
        batches.create(dict(exact, version_ids={uid: ['src']}))


@pytest.mark.parametrize('selection', [[], ['src', 'src'], ['removed'], 'src'])
def test_invalid_version_choice_does_not_create_or_execute(batch_env, selection):
    uid = batch_env['uids'][0]
    with pytest.raises(DubForgeError):
        batches.preview(dict(project_uids=[uid], scope=SCOPE, order_confirmed=True, version_ids={uid: selection}))
    assert not batch_env['started']
    assert batches.listing()['batches'] == []


def _prepared_run(flow, setup_publish, monkeypatch, *, review=False):
    uid, _, _, _ = flow
    original = service.view(setup_publish[1]['id'])
    draft = copy.deepcopy(original['draft'])
    draft['parts'] = [dict(artifact_id=9999, title='旧的无效成片')]
    if review:
        draft['review_source'] = dict(mode='new_submission', source_bvid='BV1111111111', prepared_parts=[])
    service._update(original['id'], draft=json.dumps(draft, ensure_ascii=False))
    row = batches._candidate(uid, original['id'])
    monkeypatch.setattr(service, 'plan', lambda *_: (_ for _ in ()).throw(AssertionError('制作前不能核验旧修订')))
    item = batches._item_plan(row, SCOPE, online=False, collection={'episodes': []})
    assert not item['blockers']
    runner.start_versions(uid, {}, frozen={'versions': item['plan']['version_specs']})
    run = dict(id='isolated-preparation', project_uid=uid, publication_id=original['id'],
               recipe=item['recipe'], plan=item['plan'])
    monkeypatch.setattr(workflows, '_update', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'create', lambda *_a, **_k: (_ for _ in ()).throw(AssertionError('不能另建草稿')))
    return run, original


@pytest.mark.parametrize('review', [False, True])
def test_actual_preparation_rebinds_new_outputs_to_same_draft_and_preserves_review_source(flow, setup_publish, monkeypatch, review):
    run, before = _prepared_run(flow, setup_publish, monkeypatch, review=review)
    result = workflows._prepare_publication(run, threading.Event())
    assert result['id'] == before['id']
    assert result['revision'] == before['revision'] + 1
    assert [part['artifact_id'] for part in result['draft']['parts']] == [artifact.id for artifact in setup_publish[2]]
    assert result['draft']['overrides']['title'] == before['draft']['overrides']['title']
    assert not result['receipt'].get('bvid')
    if review:
        assert result['draft']['review_source']['source_bvid'] == 'BV1111111111'
        assert result['draft']['review_source']['selection_changed'] is True


@pytest.mark.parametrize('refresh_existing', [False, True])
def test_copy_dialogue_and_saved_result_keep_publication_identity_through_settings_loops(
        flow, setup_publish, monkeypatch, refresh_existing):
    """设置名不能覆盖稿件身份；对话、草稿和流程绑定使用同一个固定 ID。"""
    from dubforge.publishing import copywriting
    create = service.create
    run, before = _prepared_run(flow, setup_publish, monkeypatch)
    expected = before['id'] if refresh_existing else 'workflow-' + run['id']
    initial_ids = {row['id'] for row in service.list_publications(run['project_uid'])}
    if not refresh_existing:
        run['plan'].pop('refresh_publication')
        run['publication_id'] = ''
        monkeypatch.setattr(service, 'create', create)
    run['recipe']['ai_copy'] = True
    monkeypatch.setattr(workflows, 'automatic_copy_fields', lambda _origins: ['description'])
    owners, updates = [], []
    def generate(project, current, instruction, *, conversation_owner_id, **_kwargs):
        owners.append(conversation_owner_id)
        return {'values': {**current, 'description': '隔离生成的简介'}}
    monkeypatch.setattr(copywriting, 'generate', generate)
    monkeypatch.setattr(workflows, '_update', lambda _key, **values: updates.append(values))
    result = workflows._prepare_publication(run, threading.Event())
    assert owners == [expected]
    assert result['id'] == expected
    assert result['draft']['overrides']['description'] == '隔离生成的简介'
    assert updates[-1]['publication_id'] == expected
    assert {row['id'] for row in service.list_publications(run['project_uid'])} == initial_ids | {expected}
    if not refresh_existing:
        # 保存后、流程绑定前中断时，固定 ID 仍保证重试只使用同一份新稿件。
        assert workflows._prepare_publication(run, threading.Event())['id'] == expected
        assert owners == [expected, expected]
        assert {row['id'] for row in service.list_publications(run['project_uid'])} == initial_ids | {expected}


def test_new_outputs_must_still_be_current_before_draft_rebinding(flow, setup_publish, monkeypatch):
    run, before = _prepared_run(flow, setup_publish, monkeypatch)
    save('project:' + run['project_uid'], {'assemble.crf': 19})
    with pytest.raises(DubForgeError) as error:
        workflows._prepare_publication(run, threading.Event())
    assert error.value.code == 'WORKFLOW_OUTPUT_STALE'
    assert service.view(before['id'])['draft']['parts'][0]['artifact_id'] == 9999


def test_completed_old_cursor_returns_to_stale_assembly_and_keeps_valid_upstream_cache(flow, setup_publish, monkeypatch):
    run, _ = _prepared_run(flow, setup_publish, monkeypatch)
    run['cursor'] = len(run['plan']['steps'])
    save('project:' + run['project_uid'], {'assemble.crf': 19})
    candidate = workflows.prepared_plan(run['project_uid'], run['recipe'], online=False)
    recovered, cursor = workflows._recovery_plan(run, candidate)
    assert cursor <= recovered['steps'].index('assemble')
    assert 'assemble' in recovered['recovery']['execute_steps']
    assert any(row['key'] == 'translate' and row['action'] == 'reuse' for row in recovered['recovery']['rows'])


@pytest.mark.parametrize('change', ['revision', 'draft', 'archived_at', 'deleted_at'])
def test_draft_change_between_batch_check_and_start_is_rejected_before_launch(batch_env, monkeypatch, change):
    pub = _draft(batch_env)
    uid = pub['project_uid']
    item = batches.preview(dict(project_uids=[uid], scope=SCOPE, order_confirmed=True))['items'][0]
    if change == 'revision':
        pub['revision'] += 1
    elif change == 'draft':
        pub['draft']['overrides']['title'] = '外部改变'
    else:
        pub[change] = '2026-10-08T12:00:00+00:00'
    launched = []
    monkeypatch.setattr(workflows, '_launch', lambda *args: launched.append(args))
    with Session(get_engine()) as session:
        session.add(ReleaseBatch(id='isolated-owner', request_id=str(uuid.uuid4()), status='running'))
        session.commit()
    batches._leases[uid] = 'isolated-owner'
    batches._running['isolated-owner'] = threading.Event()
    try:
        with batches._owned('isolated-owner'), pytest.raises(DubForgeError) as error:
            workflows.start_prepared(uid, item['plan'], str(uuid.uuid4()), publication_id=pub['id'])
        assert error.value.code == 'WORKFLOW_PLAN_CHANGED'
    finally:
        batches._leases.pop(uid, None)
        batches._running.pop('isolated-owner', None)
    assert not launched
    with Session(get_engine()) as session:
        assert session.exec(select(WorkflowRun).where(WorkflowRun.project_uid == uid)).all() == []


def test_save_completed_but_workflow_update_crashes_can_recover_same_draft(flow, setup_publish, monkeypatch):
    run, before = _prepared_run(flow, setup_publish, monkeypatch, review=True)
    monkeypatch.setattr(workflows, '_update', lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError('隔离模拟提交后中断')))
    with pytest.raises(RuntimeError, match='提交后中断'):
        workflows._prepare_publication(run, threading.Event())
    saved = service.view(before['id'])
    assert saved['revision'] == before['revision'] + 1
    item = batches._item_plan(batches._candidate(run['project_uid'], saved['id']), SCOPE,
                             online=False, collection={'episodes': []})
    assert not item['blockers']
    assert item['publication_id'] == saved['id']
    assert item['production']['execute_steps'] == []
    monkeypatch.setattr(workflows, '_update', lambda *_a, **_k: None)
    resumed = dict(run, recipe=item['recipe'], plan=item['plan'])
    assert workflows._prepare_publication(resumed, threading.Event())['id'] == saved['id']
    assert len(service.list_publications(run['project_uid'])) == 1


def test_upload_failure_recovery_uses_same_uploaded_snapshot_without_repreparing(batch_env, monkeypatch):
    pub = _draft(batch_env)
    uid = pub['project_uid']
    original = batches.preview(dict(project_uids=[uid], scope=SCOPE, order_confirmed=True))['items'][0]
    pub.update(status='failed', snapshot=dict(account_id='123', settings=dict(season_id=1, section_id=2)),
               error='隔离上传失败')
    monkeypatch.setattr(service, 'plan', lambda key: {'fingerprint': 'confirmed-upload-snapshot'})
    restored = batches._item_plan(batches._candidate(uid, pub['id']), SCOPE, saved=original,
                                 online=False, collection={'episodes': []})
    assert not restored['blockers']
    assert restored['publication_id'] == pub['id']
    assert restored['publication_action'] == 'continue'
    assert restored['plan']['steps'] == []
    assert not restored['plan'].get('refresh_publication')
    assert restored['publication']['fingerprint'] == 'confirmed-upload-snapshot'
