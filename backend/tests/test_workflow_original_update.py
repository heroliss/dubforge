"""自动制作到原BV更新的编排合同；所有远端与更新交付均使用替身。"""
import copy
import json
import uuid

import pytest
from sqlmodel import Session, select

from dubforge import workflow_plan, workflows
from dubforge.db import get_engine
from dubforge.errors import DubForgeError
from dubforge.domain import crud
from dubforge.publishing import service, updates
from dubforge.publishing.models import Publication
from dubforge.workflow_models import WorkflowRun
from test_workflows import flow, finish
from test_publishing import setup_publish


@pytest.fixture
def updating(flow, monkeypatch):
    from dubforge import workflow_update
    uid, spec, calls, sequence = flow
    with Session(get_engine()) as session:
        source = session.exec(select(Publication).where(Publication.project_uid == uid)).first()
        source.status, source.receipt = 'rejected', json.dumps(dict(bvid='BV1234567890', aid=10))
        session.add(source)
        old = WorkflowRun(project_uid=uid, request_id=str(uuid.uuid4()), publication_id=source.id,
                          status='attention', recipe=json.dumps(spec))
        session.add(old); session.commit()
        source_id = source.id
    state = dict(task=None, deliveries=[], prepares=[])

    def prepare(project_uid, recipe, version_specs, *, online=True, previous=None):
        assert project_uid == uid
        state['prepares'].append(dict(online=online, previous=copy.deepcopy(previous)))
        if not online:
            assert previous is not None, '离线重验必须带已确认的远端绑定'
        return dict(publication_id=source_id, bvid='BV1234567890', title='原稿', account_id='123', account_name='测试账号',
            parts=[dict(old_cid=index + 1, title=version['name'], version_id=version['id'], version_name=version['name'])
                   for index, version in enumerate(version_specs or [])],
            source_revision=1, remote_version='remote-version', remote_parts=[dict(cid=1), dict(cid=2)],
            values={}, chapters=dict(enabled=True), collection=dict(enabled=False), blockers=[], warnings=[],
            settings_display={})

    def task(run):
        return copy.deepcopy(state['task']) if run.get('plan', {}).get('publication_update_id') else None

    def deliver(run, event, progress):
        state['deliveries'].append(copy.deepcopy(run))
        assert workflows._leases.get(uid) == run['id']
        if (run['recipe']['update'].get('replace_videos', True)
                or 'add_parts' in run['recipe']['update'].get('sync_fields', [])):
            assert run['plan']['assembled_versions']
        else:
            assert not run['plan'].get('assembled_versions')
        if state['task'] is None:
            state['task'] = dict(id='update-' + run['id'], bvid='BV1234567890', status='waiting',
                                 phase='等待新视频审核', fingerprint='update-fingerprint', error='',
                                 execution=dict(stages={}), next_check=0)
            run['plan']['publication_update_id'] = state['task']['id']
            workflows._update(run['id'], plan=run['plan'])
        else:
            state['task']['status'] = 'waiting'
        return copy.deepcopy(state['task'])

    monkeypatch.setattr(workflow_update, 'prepare', prepare)
    monkeypatch.setattr(workflow_update, 'get_task', task)
    monkeypatch.setattr(workflow_update, 'deliver', deliver)
    monkeypatch.setattr(service, 'create', lambda *a, **k: pytest.fail('原BV更新不能创建新发布记录'))
    monkeypatch.setattr(service, 'enqueue', lambda *a, **k: pytest.fail('不能进入新投稿或旧稿审核流程'))
    intent = dict(mode='update', production_steps=['assemble'],
                  update=dict(publication_id=source_id, replacements=[]))
    return uid, intent, state, calls, sequence


def launch(updating):
    uid, intent, *_ = updating
    candidate = workflows.preview(uid, intent)
    assert not candidate['blockers'], candidate['blockers']
    request = dict(recipe=candidate['recipe'], confirmed=True, request_id=str(uuid.uuid4()),
                   fingerprint=candidate['fingerprint'])
    row = workflows.start(uid, request)
    return finish(row['id']), request


def test_update_recipe_is_per_run_and_validates_exact_mapping():
    assert 'update' not in workflow_plan.recipe({'mode': 'produce'})
    target = dict(publication_id='source', replacements=[dict(old_cid=1, version_id='original')])
    assert workflow_plan.recipe(dict(mode='update', update=target))['update'] == target
    for value in (None, {}, dict(publication_id=''), dict(publication_id='source', replacements=[dict(old_cid=True, version_id='v')]),
                  dict(publication_id='source', replacements=[dict(old_cid=1, version_id='v')] * 2)):
        with pytest.raises(DubForgeError):
            workflow_plan.recipe(dict(mode='update', update=value))
    with pytest.raises(DubForgeError):
        workflow_plan.recipe(dict(mode='publish', update=target))


def test_update_recipe_preserves_explicit_delivery_scope_and_rejects_ambiguous_values():
    target = dict(publication_id='source', replace_videos=False, sync_fields=['title', 'description', 'chapters'])
    result = workflow_plan.recipe(dict(mode='update', update=target, version_ids=[]))
    assert result['update'] == target and result['version_ids'] == []
    for patch in (dict(replace_videos='false'), dict(sync_fields='title'), dict(sync_fields=['title', 'title']),
                  dict(sync_fields=[True])):
        with pytest.raises(DubForgeError):
            workflow_plan.recipe(dict(mode='update', update={**target, **patch}))
    mapped = {**target, 'replacements': [dict(old_cid=1, version_id='source')]}
    assert workflow_plan.recipe(dict(mode='update', update=mapped))['update'] == mapped


def test_metadata_update_has_no_production_or_unrelated_version_checks(updating, monkeypatch):
    from dubforge.pipeline import assembly_versions
    uid, intent, state, _, sequence = updating
    intent['update'].update(replace_videos=False, sync_fields=['title', 'description', 'chapters'])
    intent.pop('production_steps')
    monkeypatch.setattr(assembly_versions, 'selected', lambda *a, **kw: pytest.fail('纯资料更新不读取成片版本'))
    candidate = workflows.preview(uid, intent)
    assert candidate['steps'] == [] and candidate['requested_steps'] == []
    assert not candidate['blockers'] and not candidate['dubbing']['needed']
    assert 'version_specs' not in candidate
    assert candidate['recipe']['update'] == intent['update']
    result, _ = launch(updating)
    assert result['status'] == 'monitoring' and not sequence
    assert len(state['deliveries']) == 1


def test_update_can_start_at_explicit_upstream_step(updating):
    uid, intent, state, _, sequence = updating
    intent['production_steps'] = ['translate']
    candidate = workflows.preview(uid, intent)
    assert candidate['requested_steps'] == ['translate']
    assert 'assemble' in candidate['steps'] and 'assemble' in candidate['added_steps']
    assert 'download' not in candidate['steps']
    result, _ = launch(updating)
    assert result['status'] == 'monitoring' and sequence.index('translate') < sequence.index('assemble')
    assert len(state['deliveries']) == 1


def test_add_parts_requests_outputs_without_enabling_existing_video_replacement(updating):
    uid, intent, *_ = updating
    intent['update'].update(replace_videos=False, sync_fields=['add_parts'])
    intent['production_steps'] = []
    candidate = workflows.preview(uid, intent)
    assert candidate['steps'] == ['assemble']
    assert candidate['version_specs'] and candidate['recipe']['update']['replace_videos'] is False
    result, _ = launch(updating)
    assert result['status'] == 'monitoring' and result['plan']['assembled_versions']


def test_add_parts_keeps_selected_extra_versions_alongside_explicit_replacements(updating):
    uid, intent, *_ = updating
    intent['update'].update(sync_fields=['add_parts'],
        replacements=[dict(old_cid=1, version_id='dub_hardsub')])
    candidate = workflows.preview(uid, intent)
    assert {v['id'] for v in candidate['version_specs']} == {'dub_hardsub', 'src_hardsub'}


def test_metadata_update_can_independently_run_a_selected_production_step(updating):
    _, intent, state, _, sequence = updating
    intent['update'].update(replace_videos=False, sync_fields=['description'])
    intent['production_steps'] = ['preprocess']
    result, _ = launch(updating)
    assert result['status'] == 'monitoring' and sequence == ['preprocess']
    assert result['plan']['steps'] == ['preprocess'] and len(state['deliveries']) == 1


def test_video_update_default_scope_freezes_project_checkboxes(updating):
    uid, intent, *_ = updating
    project = crud.get_project(uid)
    with Session(get_engine()) as session:
        project.params = json.dumps({**crud.project_params(project), 'auto_steps': ['translate']})
        session.add(project); session.commit()
    intent.pop('production_steps')
    candidate = workflows.preview(uid, intent)
    assert candidate['recipe']['production_steps'] == ['translate']
    assert candidate['requested_steps'] == ['translate']
    assert 'assemble' in candidate['steps'] and 'download' not in candidate['steps']


def test_metadata_update_does_not_require_local_source_file(updating):
    uid, intent, *_ = updating
    intent['production_steps'] = []
    intent['update']['replace_videos'] = False
    project = crud.get_project(uid)
    with Session(get_engine()) as session:
        project.source_kind, project.source_url = 'local', 'missing-local-video.mp4'
        session.add(project); session.commit()
    assert not workflows.preview(uid, intent)['blockers']


def test_update_rebuilds_selected_versions_then_hands_off_original_without_new_submission(updating):
    uid, _intent, state, calls, sequence = updating
    result, request = launch(updating)
    assert result['status'] == 'monitoring', result
    assert result['mode'] == 'update' and result['publication_id'] == result['recipe']['update']['publication_id']
    assert result['update_id'] == state['task']['id']
    assert result['publication']['receipt']['bvid'] == 'BV1234567890'
    assert sequence and len(state['deliveries']) == 1
    assert [v['version']['id'] for v in result['plan']['assembled_versions']] == result['recipe']['version_ids']
    assert workflows.start(uid, request)['id'] == result['id']
    from dubforge import configuration
    assert configuration.resolve(uid)['values']['run.mode'] == 'publish'
    assert len(state['deliveries']) == 1
    assert not any(operation in {'submit', 'upload'} for operation, _ in calls)
    assert any(not call['online'] and call['previous'] for call in state['prepares'])


@pytest.mark.parametrize('status,expected', [('completed', 'completed'), ('failed', 'attention'), ('unknown', 'attention'),
                                             ('waiting', 'monitoring'), ('paused', 'paused')])
def test_monitor_reads_update_task_not_original_rejected_status(updating, status, expected):
    _uid, _, state, _, _ = updating
    result, _ = launch(updating)
    state['task']['status'] = status
    state['task']['phase'] = '本次更新状态'
    workflows._update(result['id'], next_check=0)
    workflows.monitor_once()
    current = workflows.get(result['id'])
    assert current['status'] == expected
    assert current['phase'] == '本次更新状态'


def test_continue_failed_delivery_preserves_production_and_does_not_reprepare_target(updating):
    uid, _, state, _, sequence = updating
    result, _ = launch(updating)
    state['task']['status'] = 'failed'
    workflows._update(result['id'], status='attention')
    before = copy.deepcopy(sequence), len(state['prepares'])
    candidate = workflows.preview(uid, dict(mode='update', resume_id=result['id']))
    assert not candidate['blockers'] and candidate['recovery']['execute_steps'] == []
    resumed = workflows.resume(result['id'], dict(confirmed=True, fingerprint=candidate['fingerprint'],
                                                   recovery_fingerprint=candidate['recovery_fingerprint']))
    assert finish(resumed['id'])['status'] == 'monitoring'
    assert sequence == before[0] and len(state['prepares']) == before[1]
    assert len(state['deliveries']) == 2


def test_unknown_resume_only_reconciles_without_rebuilding_or_retrying(updating, monkeypatch):
    uid, _, state, _, sequence = updating
    result, _ = launch(updating)
    state['task']['status'] = 'unknown'
    workflows._update(result['id'], status='attention')
    before = copy.deepcopy(sequence)
    operations = []

    def action(key, body, *, workflow_id=''):
        operations.append((key, body['action'], workflow_id))
        state['task']['status'] = 'paused'
        return copy.deepcopy(state['task'])

    monkeypatch.setattr(updates, 'action', action)
    candidate = workflows.preview(uid, dict(resume_id=result['id']))
    resumed = workflows.resume(result['id'], dict(confirmed=True, fingerprint=candidate['fingerprint'],
                                                   recovery_fingerprint=candidate['recovery_fingerprint']))
    assert finish(resumed['id'])['status'] == 'paused'
    assert operations == [(state['task']['id'], 'reconcile', result['id'])]
    assert sequence == before and len(state['deliveries']) == 1


def test_resume_rechecks_update_task_state_after_confirmation(updating):
    uid, _, state, _, _ = updating
    result, _ = launch(updating)
    state['task']['status'] = 'failed'
    workflows._update(result['id'], status='attention')
    candidate = workflows.preview(uid, dict(resume_id=result['id']))
    state['task']['status'] = 'unknown'
    with pytest.raises(DubForgeError) as error:
        workflows.resume(result['id'], dict(confirmed=True, fingerprint=candidate['fingerprint'],
                                               recovery_fingerprint=candidate['recovery_fingerprint']))
    assert error.value.code == 'WORKFLOW_PLAN_CHANGED'


def test_stop_pauses_update_and_dashboard_exposes_update_identity(updating, monkeypatch):
    uid, _, state, _, _ = updating
    result, _ = launch(updating)
    operations = []
    monkeypatch.setattr(updates, 'action', lambda key, body: operations.append((key, body['action'])))
    current = workflows.stop(result['id'])
    assert current['post_processing_paused'] is True
    assert operations == [(state['task']['id'], 'pause')]
    dashboard = workflows.status(uid)['run']
    assert dashboard['mode'] == 'update' and dashboard['update_id'] == state['task']['id']
    assert dashboard['publication_update']['id'] == state['task']['id']


@pytest.mark.parametrize('changed', [False, True])
def test_source_direct_version_binds_registered_source_without_reencoding(flow, changed):
    from dubforge.pipeline import assembly_versions
    uid, _, _, _ = flow
    project = crud.get_project(uid)
    deps = workflows.Dependencies(project)
    version = assembly_versions.resolve(deps, dict(id='source-direct', name='原片直用', dubbing=False,
        burn_subtitles=False, include_comments=False, overrides={'assemble.final_loudness_mode': 'off'}))
    artifact = deps.arts['download/video.mp4']
    if changed:
        deps.path('download', 'video.mp4').write_bytes(b'changed-source')
    with Session(get_engine()) as session:
        row = WorkflowRun(project_uid=uid, request_id=str(uuid.uuid4()), recipe=json.dumps(dict(mode='update')))
        session.add(row); session.commit(); session.refresh(row)
        key = row.id
    run = dict(id=key, project_uid=uid, recipe=dict(mode='update'), plan={})
    if changed:
        with pytest.raises(DubForgeError) as error:
            workflows._bind_assembled_versions(run, [version], {})
        assert error.value.code == 'WORKFLOW_OUTPUT_STALE'
    else:
        workflows._bind_assembled_versions(run, [version], {})
        bound = workflows.get(key)['plan']['assembled_versions'][0]
        assert bound['artifact_id'] == artifact.id and bound['artifact_sha256'] == artifact.sha256
