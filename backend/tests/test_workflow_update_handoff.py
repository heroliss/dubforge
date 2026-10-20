"""自动重合成到原稿的交接合同：隔离项目/账号，禁止真实平台调用。"""
import copy
import json
import threading
from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

from dubforge import workflows, workflow_update
from dubforge.db import get_engine
from dubforge.errors import DubForgeError
from dubforge.publishing import service as pub, updates
from dubforge.publishing.models import PublicationUpdate
from dubforge.workflow_models import WorkflowRun
from test_publishing import setup_publish
from test_publication_updates import env, finish


@pytest.fixture
def target(env, monkeypatch):
    from dubforge import configuration
    from dubforge.pipeline import assembly_versions
    from dubforge.publishing import project_copy
    uid = env['uid']
    definitions = [dict(id='source-v', name='原声'), dict(id='dub-v', name='配音')]
    records = {d['id']: SimpleNamespace(artifact_id=a.id, artifact_hash=a.sha256, snapshot='{}')
               for d, a in zip(definitions, env['artifacts'])}
    monkeypatch.setattr(assembly_versions, 'results', lambda uid: records)
    values = {'publish.chapters_enabled': True, 'publish.chapters_translate': True,
        'publish.chapters_permanent': False, 'publish.chapters_instruction': '标题精炼',
        'publish.season_id': 1, 'publish.section_id': 2}
    resolve = configuration.resolve
    monkeypatch.setattr(configuration, 'resolve', lambda *args, **kwargs:
        {**resolve(*args, **kwargs), 'values': {**resolve(*args, **kwargs)['values'], **values}})
    monkeypatch.setattr(assembly_versions, 'artifact_metadata', lambda uid: {})
    monkeypatch.setattr(project_copy, 'preview', lambda uid: dict(
        values=dict(title='新的标题', description='新的简介', tags=[]), configured=['publish.description']))
    monkeypatch.setattr(project_copy, 'source_content', lambda project: dict(
        chapters=[], thumbnail='', source_published_date=None, content={}))
    spec = dict(mode='update', update=dict(publication_id=env['source']['id'], replacements=[]))
    return dict(**env, spec=spec, definitions=definitions, records=records, values=values)


def test_preflight_maps_legacy_snapshot_by_exact_artifact_and_hash_without_writing(target, monkeypatch):
    first = workflow_update.prepare(target['uid'], target['spec'], target['definitions'])
    assert [p['version_id'] for p in first['parts']] == ['source-v', 'dub-v']
    assert not first['blockers']
    assert first['values'] == {'description': '新的简介'}
    assert first['settings']['title'] == target['remote']['settings']['title']
    assert first['chapters']['enabled'] and not first['chapters']['permanent']
    assert first['chapters']['instruction'] == '标题精炼'
    assert first['collection']['enabled']
    assert 'filename' not in json.dumps(first)
    with Session(get_engine()) as session:
        assert not session.exec(select(PublicationUpdate)).all()
    assert not target['uploads'] and not target['writes']
    monkeypatch.setattr(updates, '_remote', lambda source: pytest.fail('离线重算不能访问B站'))
    monkeypatch.setattr(pub, 'metadata', lambda account: pytest.fail('离线重算不能访问B站'))
    second = workflow_update.prepare(target['uid'], target['spec'], target['definitions'],
                                     online=False, previous=first)
    assert second == first


def test_missing_provenance_blocks_guessing_but_accepts_explicit_part_selection(target):
    for row in target['records'].values():
        row.artifact_hash = 'different'
    data = workflow_update.prepare(target['uid'], target['spec'], target['definitions'])
    assert data['blockers'] and not any(p['version_id'] for p in data['parts'])
    target['spec']['update']['replacements'] = [dict(old_cid=part['cid'], version_id=d['id'])
        for part, d in zip(target['remote']['videos'], target['definitions'])]
    assert not workflow_update.prepare(target['uid'], target['spec'], target['definitions'])['blockers']


def test_prepared_mapping_survives_new_assembly_registration_on_resume(target):
    first = workflow_update.prepare(target['uid'], target['spec'], target['definitions'])
    for row in target['records'].values():
        row.artifact_id += 100
        row.artifact_hash = 'new-version'
    resumed = workflow_update.prepare(target['uid'], target['spec'], target['definitions'], previous=first)
    assert resumed['parts'] == first['parts'] and not resumed['blockers']
    target['remote']['videos'].reverse()
    with pytest.raises(DubForgeError) as error:
        workflow_update.prepare(target['uid'], target['spec'], target['definitions'], previous=first)
    assert error.value.code == 'PUBLISH_REMOTE_CHANGED'


@pytest.mark.parametrize('kind', ['duplicate_part', 'duplicate_version', 'wrong_cid', 'wrong_version'])
def test_invalid_mapping_never_reaches_upload(target, kind):
    a, b = target['remote']['videos']
    mapping = [dict(old_cid=a['cid'], version_id='source-v'), dict(old_cid=b['cid'], version_id='dub-v')]
    if kind == 'duplicate_part': mapping[1]['old_cid'] = a['cid']
    if kind == 'duplicate_version': mapping[1]['version_id'] = 'source-v'
    if kind == 'wrong_cid': mapping[0]['old_cid'] = 999
    if kind == 'wrong_version': mapping[0]['version_id'] = 'other'
    target['spec']['update']['replacements'] = mapping
    with pytest.raises(DubForgeError):
        workflow_update.prepare(target['uid'], target['spec'], target['definitions'])
    assert not target['writes'] and not target['uploads']


def _run(target):
    plan = workflow_update.prepare(target['uid'], target['spec'], target['definitions'])
    # 当前测试不需要章节，另有真实章节时间轴/新CID测试覆盖下游。
    plan['chapters']['enabled'] = False
    plan['collection'] = dict(enabled=False, season_id=None, section_id=None)
    frozen = dict(update_plan=plan, assembled_versions=[dict(version=d, artifact_id=a.id,
        artifact_sha256=a.sha256) for d, a in zip(target['definitions'], target['artifacts'])])
    row = WorkflowRun(project_uid=target['uid'], request_id='workflow-test',
        publication_id=target['source']['id'], recipe=json.dumps(target['spec']), plan=json.dumps(frozen))
    with Session(get_engine()) as session:
        session.add(row); session.commit(); session.refresh(row)
        run = dict(id=row.id, project_uid=row.project_uid, plan=frozen)
    return run


def test_handoff_persists_one_task_replay_never_uploads_or_submits_again(target):
    run = _run(target)
    workflows._leases[target['uid']] = run['id']
    try:
        with workflows._owned(run['id']):
            task = workflow_update.deliver(run, threading.Event())
            task = finish(task['id'])
            assert task['status'] == 'completed', task
            assert task['bvid'] == target['remote']['bvid']
            assert len(target['uploads']) == 2 and len(target['writes']) == 1
            # 模拟交接关联尚未保存时服务崩溃。固定更新ID仍找回同一个任务。
            run['plan'].pop('publication_update_id')
            again = workflow_update.deliver(run, threading.Event())
            assert again['id'] == task['id'] and again['status'] == 'completed'
            assert len(target['uploads']) == 2 and len(target['writes']) == 1
    finally:
        workflows._leases.pop(target['uid'], None)
    with Session(get_engine()) as session:
        assert len(session.exec(select(PublicationUpdate)).all()) == 1


def test_handoff_checks_actual_artifact_hash_and_remote_change_before_any_upload(target):
    run = _run(target)
    workflows._leases[target['uid']] = run['id']
    try:
        with workflows._owned(run['id']):
            run['plan']['assembled_versions'][0]['artifact_sha256'] = 'wrong'
            with pytest.raises(DubForgeError) as error:
                workflow_update.deliver(run)
            assert error.value.code == 'WORKFLOW_OUTPUT_STALE'
            run['plan']['assembled_versions'][0]['artifact_sha256'] = target['artifacts'][0].sha256
            target['remote']['version'] = 'externally-changed'
            with pytest.raises(DubForgeError) as error:
                workflow_update.deliver(run)
            assert error.value.code == 'PUBLISH_REMOTE_CHANGED'
    finally:
        workflows._leases.pop(target['uid'], None)
    assert not target['uploads'] and not target['writes']


def test_internal_update_requires_real_owner_and_plan_replay_is_idempotent(target):
    body = dict(replacements=[dict(old_cid=target['remote']['videos'][0]['cid'],
                                  artifact_id=target['artifacts'][0].id)])
    with pytest.raises(DubForgeError) as error:
        updates.plan(target['source']['id'], body, workflow_id='forged')
    assert error.value.code == 'WORKFLOW_BUSY'
    workflows._leases[target['uid']] = 'real'
    try:
        with workflows._owned('real'):
            expected = dict(source_revision=updates._source(target['source']['id'])['revision'],
                remote_version=target['remote']['version'], remote_parts=[{k: v for k, v in p.items()
                    if k != 'filename'} for p in target['remote']['videos']])
            task = updates.plan(target['source']['id'], body, workflow_id='real', expected=expected)
            target['remote']['version'] = 'later'
            replay = updates.plan(target['source']['id'], body, workflow_id='real', expected=expected)
            assert replay['id'] == task['id']
            with pytest.raises(DubForgeError):
                updates.plan(target['source']['id'], {**body, 'values': dict(description='不同范围')},
                    workflow_id='real', expected=expected)
            # 普通手动API不获得内部租约豁免。
            with workflows._owned('other'), pytest.raises(DubForgeError):
                updates.action(task['id'], dict(action='start', confirmed=True,
                    fingerprint=task['fingerprint']), workflow_id='real')
    finally:
        workflows._leases.pop(target['uid'], None)


def test_chapter_metadata_changed_during_assembly_blocks_before_upload(target, monkeypatch):
    from dubforge.publishing import project_copy
    source_content = lambda project: dict(chapters=[dict(title='新章节', start_time=0, end_time=30)],
        thumbnail='', source_published_date=None, content={})
    run = _run(target)
    run['plan']['update_plan']['chapters']['enabled'] = True
    monkeypatch.setattr(project_copy, 'source_content', source_content)
    workflows._leases[target['uid']] = run['id']
    try:
        with workflows._owned(run['id']), pytest.raises(DubForgeError) as error:
            workflow_update.deliver(run)
        assert error.value.code == 'PUBLISH_REMOTE_CHANGED'
    finally:
        workflows._leases.pop(target['uid'], None)
    assert not target['uploads'] and not target['writes']


def test_disabled_chapters_do_not_block_update_when_unused_metadata_changes(target, monkeypatch):
    from dubforge.publishing import project_copy
    run = _run(target)
    monkeypatch.setattr(project_copy, 'source_content', lambda project: dict(
        chapters=[dict(title='未选择同步的章节', start_time=0, end_time=30)],
        thumbnail='', source_published_date=None, content={}))
    workflows._leases[target['uid']] = run['id']
    try:
        with workflows._owned(run['id']):
            task = workflow_update.deliver(run)
            result = finish(task['id'])
        assert result['status'] == 'completed'
        assert not result['preview']['chapters']['enabled']
    finally:
        workflows._leases.pop(target['uid'], None)


def test_registered_original_video_can_replace_without_reencoding(target):
    from dubforge.domain import crud
    project = crud.get_project(target['uid'])
    root = crud.project_dir(target['uid'])
    path = root / 'media/video.mp4'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'original-video')
    artifact = crud.add_artifact(project.id, 'download', 'video', 'video.mp4', path, 'media/video.mp4')
    task = updates.plan(target['source']['id'], dict(replacements=[dict(
        old_cid=target['remote']['videos'][0]['cid'], artifact_id=artifact.id)]))
    with Session(get_engine()) as session:
        frozen = json.loads(session.get(PublicationUpdate, task['id']).plan)
    assert frozen['parts'][0]['artifact_id'] == artifact.id
    assert frozen['parts'][0]['has_dubbing'] is False
    assert not target['uploads'] and not target['writes']


def test_auxiliary_source_status_refresh_failure_does_not_undo_verified_update(target, monkeypatch):
    run = _run(target)
    def unavailable(*args, **kwargs):
        raise OSError('隔离状态回读暂不可用')
    monkeypatch.setattr(pub, 'refresh_status', unavailable)
    workflows._leases[target['uid']] = run['id']
    try:
        with workflows._owned(run['id']):
            task = workflow_update.deliver(run)
            result = finish(task['id'])
        assert result['status'] == 'completed'
        assert result['execution']['receipt_sync_error']
    finally:
        workflows._leases.pop(target['uid'], None)
