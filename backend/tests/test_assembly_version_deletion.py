"""版本删除的配置事务、并发门禁和媒体证据保留；所有数据由夹具隔离。"""
import datetime
import hashlib
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.main import app
from dubforge import configuration
from dubforge.domain import crud
from dubforge.domain.models import Artifact
from dubforge.pipeline import assembly_version_deletion as deletion, assembly_versions
from dubforge.pipeline.assembly_models import AssemblyResult
from dubforge.publishing.models import Publication
from dubforge.workflow_models import WorkflowRun


def _definition(identifier):
    return dict(id=identifier, name='版本' + identifier, dubbing=False, burn_subtitles=False,
                include_comments=False, overrides={})


def _save(uid, updates):
    current = configuration.resolve(uid)
    return configuration.save('project:' + uid, dict(revision=current['revision'],
        fingerprint=current['fingerprint'], updates=updates))


def _project(make_project, **values):
    uid = make_project()
    _save(uid, {'assembly.versions': [_definition('a'), _definition('b')], **values})
    return uid


def _request(checked):
    return {key: checked[key] for key in ('version_id', 'revision', 'fingerprint')} | {'confirmed': True}


def _all_files(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}


def _media(uid):
    project = crud.get_project(uid)
    root = crud.project_dir(uid)
    output = root / 'output'
    output.mkdir(exist_ok=True)
    final = output / 'final_version_a.mp4'
    preview = output / ('preview_version_a_' + uuid.uuid4().hex + '.mp4')
    final.write_bytes('保留正式文件'.encode('utf-8'))
    preview.write_bytes('保留试片'.encode('utf-8'))
    for name in ('.partial-baselines', '.update-baselines', '.timeline-media'):
        folder = output / name
        folder.mkdir()
        (folder / ('a' * 64 + '.mp4')).write_bytes('保留基准'.encode('utf-8'))
    for name in ('.partial-plans', '.update-plans', '.timeline-plans'):
        folder = output / name
        folder.mkdir()
        (folder / ('a' * 32 + '.json')).write_text('{"base_version_id":"a"}', encoding='utf-8')
    with crud.session() as session:
        artifact = Artifact(project_id=project.id, step_key='assemble', name=final.name,
            rel_path='output/' + final.name, kind='video', size=final.stat().st_size,
            sha256=hashlib.sha256(final.read_bytes()).hexdigest())
        session.add(artifact); session.flush()
        session.add(Artifact(project_id=project.id, step_key='assemble', name=preview.name,
            rel_path='output/' + preview.name, kind='video', size=preview.stat().st_size))
        record = AssemblyResult(key=uid + ':a', project_uid=uid, version_id='a', artifact_id=artifact.id,
            artifact_hash=artifact.sha256, fingerprint='fixture', snapshot=json.dumps(_definition('a')))
        publication = Publication(project_uid=uid, draft=json.dumps({'parts': [dict(artifact_id=artifact.id)]}),
            snapshot=json.dumps({'parts': [dict(version_id='a', artifact_id=artifact.id, sha256=artifact.sha256)]}),
            receipt='{"bvid":"BV保留记录"}')
        session.add(record); session.add(publication); session.commit(); session.refresh(publication)
        return root, publication.id


def test_delete_keeps_media_success_bindings_and_publication_and_updates_references(make_project):
    uid = _project(make_project, **{'publish.version_ids': ['a', 'b'],
        'publish.part_titles': {'a': '原分P', 'b': '保留分P'}, 'assemble.crf': 20})
    root, publication_id = _media(uid)
    before_files = _all_files(root)
    before = configuration.resolve(uid)
    with crud.session() as session:
        before_artifacts = [row.model_dump() for row in session.exec(select(Artifact)).all()]
        before_publication = session.get(Publication, publication_id).model_dump()
        before_result = session.get(AssemblyResult, uid + ':a').model_dump()
    checked = deletion.plan(uid, {'version_id': 'a'})
    assert checked['can_delete'] and checked['formal_count'] == checked['preview_count'] == 1
    assert checked['id'] == checked['version_id'] == 'a' and '保留' in checked['notice']
    assert configuration.resolve(uid) == before and _all_files(root) == before_files
    response = deletion.delete(uid, _request(checked))
    assert response['deleted_version'] == {'id': 'a', 'name': '版本a'}
    assert [row['id'] for row in response['versions']] == ['b']
    after = configuration.resolve(uid)
    assert after['revision'] == before['revision'] + 1
    assert after['values']['publish.version_ids'] == ['b']
    assert after['values']['publish.part_titles'] == {'b': '保留分P'}
    for key in before['values'].keys() - {'assembly.versions', 'publish.version_ids', 'publish.part_titles'}:
        assert after['values'][key] == before['values'][key]
    with crud.session() as session:
        assert [row.model_dump() for row in session.exec(select(Artifact)).all()] == before_artifacts
        assert session.get(Publication, publication_id).model_dump() == before_publication
        assert session.get(AssemblyResult, uid + ':a').model_dump() == before_result
    for name, value in before_files.items():
        if name != 'manifest.json':
            assert (root / name).read_bytes() == value
    assert _all_files(root).keys() == before_files.keys()
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest


def test_default_publication_selection_stays_implicit_and_other_selection_stays_unchanged(make_project):
    uid = _project(make_project)
    checked = deletion.plan(uid, {'version_id': 'a'})
    deletion.delete(uid, _request(checked))
    current = configuration.resolve(uid)
    assert current['values']['publish.version_ids'] is None
    assert 'publish.version_ids' not in current['overrides']
    other = _project(make_project, **{'publish.version_ids': ['b']})
    deletion.delete(other, _request(deletion.plan(other, {'version_id': 'a'})))
    assert configuration.resolve(other)['values']['publish.version_ids'] == ['b']


@pytest.mark.parametrize('single_version,single_publication', [(True, False), (False, True)])
def test_last_definition_and_only_explicit_publication_are_blocked_without_changes(make_project,
        single_version, single_publication):
    uid = _project(make_project, **({'publish.version_ids': ['a']} if single_publication else {}))
    if single_version:
        _save(uid, {'assembly.versions': [_definition('a')]})
    before = configuration.resolve(uid)
    files = _all_files(crud.project_dir(uid))
    checked = deletion.plan(uid, {'version_id': 'a'})
    assert checked['can_delete'] is False and checked['blockers']
    with pytest.raises(deletion.VersionDeletionError) as raised:
        deletion.delete(uid, _request(checked))
    assert raised.value.code == 'ASSEMBLY_VERSION_DELETE_BLOCKED'
    assert configuration.resolve(uid) == before and _all_files(crud.project_dir(uid)) == files


@pytest.mark.parametrize('changed', ['revision', 'fingerprint', 'missing_fingerprint', 'unconfirmed'])
def test_stale_or_unconfirmed_requests_never_delete(make_project, changed):
    uid = _project(make_project)
    body = _request(deletion.plan(uid, {'version_id': 'a'}))
    if changed == 'revision':
        body['revision'] -= 1
    elif changed == 'fingerprint':
        body['fingerprint'] = '0' * 64
    elif changed == 'missing_fingerprint':
        body.pop('fingerprint')
    else:
        body['confirmed'] = False
    before = configuration.resolve(uid)
    files = _all_files(crud.project_dir(uid))
    with pytest.raises(deletion.VersionDeletionError):
        deletion.delete(uid, body)
    assert configuration.resolve(uid) == before and _all_files(crud.project_dir(uid)) == files


@pytest.mark.parametrize('busy', ['queued', 'running', 'stopping', 'publication', 'workflow', 'archived'])
def test_structural_lock_blocks_all_project_activity_even_for_other_steps(make_project, busy):
    uid = _project(make_project)
    checked = deletion.plan(uid, {'version_id': 'a'})
    project = crud.get_project(uid)
    if busy in {'queued', 'running', 'stopping'}:
        step = crud.get_step(project.id, 'download')
        step.status = busy
        crud.save_step(step)
    else:
        with crud.session() as session:
            if busy == 'publication':
                session.add(Publication(project_uid=uid, status='uploading'))
            elif busy == 'workflow':
                session.add(WorkflowRun(project_uid=uid, request_id=uuid.uuid4().hex, status='queued'))
            else:
                project.archived_at = datetime.datetime.now(datetime.timezone.utc)
                session.add(project)
            session.commit()
    before = configuration.resolve(uid)
    files = _all_files(crud.project_dir(uid))
    fresh = deletion.plan(uid, {'version_id': 'a'})
    assert fresh['can_delete'] is False and fresh['blockers']
    with pytest.raises(deletion.VersionDeletionError) as raised:
        deletion.delete(uid, _request(checked))
    assert raised.value.code == 'ASSEMBLY_VERSION_DELETE_BLOCKED'
    assert configuration.resolve(uid) == before and _all_files(crud.project_dir(uid)) == files


def test_api_protocol_is_same_origin_revisioned_and_non_mutating_on_bad_ids(make_project):
    uid = _project(make_project)
    client = TestClient(app)
    url = f'/api/projects/{uid}/assembly-versions'
    before = configuration.resolve(uid)
    assert client.post(url + '/delete-plan', json={'version_id': '../a'}).status_code == 400
    assert client.post(url + '/delete-plan', json={'version_id': 'missing'}).status_code == 400
    assert client.post(url + '/delete-plan', json={'version_id': 'a'},
        headers={'Origin': 'https://another.example'}).status_code == 400
    assert configuration.resolve(uid) == before
    checked = client.post(url + '/delete-plan', json={'version_id': 'a'})
    assert checked.status_code == 200 and checked.headers['cache-control'] == 'no-store'
    response = client.post(url + '/delete', json=_request(checked.json()))
    assert response.status_code == 200, response.text
    assert response.json()['deleted_version']['id'] == 'a'
    assert client.post(url + '/delete', json=_request(checked.json())).json()['code'] == 'ASSEMBLY_VERSION_DELETE_CONFLICT'


@pytest.mark.parametrize('busy', ['publication', 'workflow'])
def test_activity_has_clear_priority_and_released_activity_does_not_validate_old_preview(make_project, busy):
    uid = _project(make_project)
    checked = deletion.plan(uid, {'version_id': 'a'})
    _save(uid, {'publish.part_titles': {'b': '保留版本的新名称'}})
    with crud.session() as session:
        row = (Publication(project_uid=uid, status='uploading') if busy == 'publication' else
               WorkflowRun(project_uid=uid, request_id=uuid.uuid4().hex, status='queued'))
        session.add(row)
        session.commit()
        session.refresh(row)
        identifier = row.id
    before = configuration.resolve(uid)
    files = _all_files(crud.project_dir(uid))
    with pytest.raises(deletion.VersionDeletionError) as active:
        deletion.delete(uid, _request(checked))
    assert active.value.code == 'ASSEMBLY_VERSION_DELETE_BLOCKED'
    assert active.value.suggestion != deletion.VersionDeletionError.suggestion
    assert configuration.resolve(uid) == before and _all_files(crud.project_dir(uid)) == files
    with crud.session() as session:
        terminal = session.get(Publication if busy == 'publication' else WorkflowRun, identifier)
        terminal.status = 'cancelled' if busy == 'publication' else 'stopped'
        session.add(terminal)
        session.commit()
    assert deletion.plan(uid, {'version_id': 'a'})['can_delete'] is True
    with pytest.raises(deletion.VersionDeletionError) as stale:
        deletion.delete(uid, _request(checked))
    assert stale.value.code == 'ASSEMBLY_VERSION_DELETE_CONFLICT'
    assert _all_files(crud.project_dir(uid)) == files


def test_real_partial_child_keeps_immutable_baseline_after_base_definition_deletion(make_project, monkeypatch):
    from dubforge.pipeline import assembly_partial
    from dubforge.pipeline.dependencies import Dependencies
    from dubforge.runner import runner
    from test_assemble import _prepare_outputs_project
    from test_assembly_versions import wait
    uid, root = _prepare_outputs_project(make_project, monkeypatch)
    base = {**_definition('a'), 'burn_subtitles': True}
    assembly_versions.save(uid, dict(revision=assembly_versions.catalog(uid)['revision'], versions=[base]))
    rendering = assembly_versions.plan(uid, {'version_ids': ['a']})
    runner.start_versions(uid, {'version_ids': ['a'], 'fingerprint': rendering['fingerprint']})
    wait(uid)
    body = dict(base_version_id='a', ranges=[dict(start=1., end=1.8)], replace_audio=False)
    child_plan = assembly_partial.plan(uid, body)
    child = assembly_partial.create(uid, {**body, 'fingerprint': child_plan['fingerprint']})['version']
    assembly_versions.save(uid, dict(revision=assembly_versions.catalog(uid)['revision'], versions=[base, child]))
    before_files = _all_files(root)
    record = assembly_versions.results(uid)['a'].model_dump()
    checked = deletion.plan(uid, {'version_id': 'a'})
    assert checked['can_delete'] and checked['dependent_versions'] == [child['name']]
    deletion.delete(uid, _request(checked))
    loaded = assembly_partial.load_plan(Dependencies(crud.get_project(uid)), child, verify_hash=True)
    assert loaded['base_version_id'] == 'a'
    assert assembly_versions.results(uid)['a'].model_dump() == record
    for name, value in before_files.items():
        if name != 'manifest.json':
            assert (root / name).read_bytes() == value
