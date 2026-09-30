"""成品回收与原位恢复：所有文件在隔离目录，回收站和COM调用均为模拟。"""
import sys
import threading
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.pipeline import output_files
from dubforge.publishing import service
from dubforge.publishing.models import Publication
from dubforge.runner import runner
from dubforge.utils import recycle as recycle_utils

WINDOWS_RECYCLE = recycle_utils._windows_recycle
NAME = 'final_dub_20260927_120000_123456.mp4'


@pytest.fixture()
def output(make_project):
    uid = make_project()
    project, root = crud.get_project(uid), crud.project_dir(uid)
    path = root / 'output' / NAME
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b'isolated-video')
    artifact = crud.add_artifact(project.id, 'assemble', 'video', NAME, path, str(path.relative_to(root)))
    body = dict(artifact_id=artifact.id, file_revision=output_files.snapshot(artifact, root)['file_revision'], confirmed=True)
    return uid, artifact, path, body


def test_recycle_restore_and_refresh_keep_identity_and_draft(output, tmp_path, monkeypatch):
    uid, artifact, path, body = output
    before_steps = [(s.step_key, s.status, s.input_fingerprint) for s in crud.get_steps(artifact.project_id)]
    draft = service.create(uid)
    service.save(draft['id'], dict(draft['draft'], revision=1, parts=[dict(artifact_id=artifact.id, title='配音版')]))
    trash = tmp_path / 'simulated-recycle.mp4'
    monkeypatch.setattr(output_files, 'recycle_file', lambda p: p.rename(trash))
    from app.main import app
    with TestClient(app) as client:
        result = client.post(f'/api/projects/{uid}/steps/assemble/artifacts/{NAME}/recycle', json=body)
        assert result.status_code == 200 and result.json()['status'] == 'recycled'
        def state():
            steps = client.get(f'/api/projects/{uid}/steps').json()
            return next(s for s in steps if s['key'] == 'assemble')['artifacts'][0]
        assert state()['exists'] is False
        assert service.candidates(uid)['files'][0]['exists'] is False
        assert service.view(draft['id'])['draft']['parts'][0]['artifact_id'] == artifact.id
        trash.rename(path)
        assert state()['exists'] is True and state()['id'] == artifact.id
        assert service.candidates(uid)['files'][0]['exists'] is True
    assert before_steps == [(s.step_key, s.status, s.input_fingerprint) for s in crud.get_steps(artifact.project_id)]


def test_external_missing_and_returned_file_are_readonly(output, tmp_path):
    uid, artifact, path, body = output
    moved = tmp_path / 'moved.mp4'
    path.rename(moved)
    assert output_files.snapshot(artifact, crud.project_dir(uid))['exists'] is False
    assert output_files.recycle(uid, NAME, body)['status'] == 'missing'
    moved.rename(path)
    assert output_files.snapshot(artifact, crud.project_dir(uid))['exists'] is True
    assert crud.latest_artifacts(artifact.project_id)['assemble/' + NAME].id == artifact.id


@pytest.mark.parametrize('change', ['id', 'confirmation', 'revision', 'disk'])
def test_stale_or_unconfirmed_delete_does_not_touch_file(output, monkeypatch, change):
    uid, artifact, path, body = output
    monkeypatch.setattr(output_files, 'recycle_file', lambda p: pytest.fail('无效请求不能删除'))
    if change == 'id':
        body['artifact_id'] += 1
    elif change == 'confirmation':
        body['confirmed'] = False
    elif change == 'revision':
        body['file_revision'] = ''
    else:
        path.write_bytes(b'new-replaced-video')
    with pytest.raises(output_files.OutputFileError):
        output_files.recycle(uid, NAME, body)
    assert path.is_file()


@pytest.mark.parametrize('status', ['queued', 'running', 'finalizing'])
def test_assembly_in_use_is_protected(output, monkeypatch, status):
    uid, artifact, path, body = output
    if status == 'finalizing':
        monkeypatch.setattr(runner, 'current_job_id', lambda *a: 77)
    else:
        step = crud.get_step(artifact.project_id, 'assemble')
        step.status = status
        crud.save_step(step)
    with pytest.raises(Exception) as caught:
        output_files.recycle(uid, NAME, body)
    assert caught.value.code == 'OUTPUT_FILE_BUSY' and path.exists()


@pytest.mark.parametrize('status,finishing', [('uploading', False), ('queued', False), ('paused', True)])
def test_publication_in_use_is_protected(output, status, finishing):
    uid, _, path, body = output
    with Session(get_engine()) as session:
        row = Publication(project_uid=uid, status=status)
        session.add(row); session.commit(); session.refresh(row)
        key = row.id
    if finishing:
        service._running[key] = threading.Event()
    try:
        with pytest.raises(service.PublishError) as caught:
            output_files.recycle(uid, NAME, body)
        assert caught.value.code == 'OUTPUT_FILE_BUSY' and path.exists()
    finally:
        service._running.pop(key, None)


def test_recycle_failure_keeps_file_and_registration(output, monkeypatch):
    uid, artifact, path, body = output
    def failed(p):
        raise recycle_utils.RecycleError('回收站不可用')
    monkeypatch.setattr(output_files, 'recycle_file', failed)
    with pytest.raises(recycle_utils.RecycleError):
        output_files.recycle(uid, NAME, body)
    assert path.exists()
    assert crud.latest_artifacts(artifact.project_id)['assemble/' + NAME].id == artifact.id


@pytest.mark.parametrize('target', ['outside', 'source', 'directory', 'unknown_name', 'preview', 'foreign'])
def test_only_owned_final_regular_files_can_be_recycled(output, tmp_path, make_project, monkeypatch, target):
    uid, artifact, path, body = output
    root = crud.project_dir(uid)
    monkeypatch.setattr(output_files, 'recycle_file', lambda p: pytest.fail('边界外的文件不能回收'))
    if target == 'outside':
        forged = tmp_path / NAME
    elif target == 'source':
        forged = root / 'media' / NAME
    else:
        forged = path
    if target in {'outside', 'source'}:
        forged.write_bytes(b'protected-source')
        artifact.rel_path = str(forged)
        with Session(get_engine()) as session:
            session.add(artifact); session.commit()
    if target == 'directory':
        path.unlink(); path.mkdir()
    name = NAME if target not in {'unknown_name', 'preview'} else ('../' + NAME if target == 'unknown_name' else NAME.replace('final_', 'preview_'))
    if target == 'foreign':
        uid = make_project()
    with pytest.raises(output_files.OutputFileError):
        output_files.recycle(uid, name, body)
    assert forged.exists()


def test_cross_origin_cannot_recycle(output):
    uid, _, path, body = output
    from app.main import app
    with TestClient(app) as client:
        response = client.post(f'/api/projects/{uid}/steps/assemble/artifacts/{NAME}/recycle',
                               json=body, headers={'Origin': 'https://untrusted.example'})
        assert response.json()['code'] == 'OUTPUT_ORIGIN' and path.exists()


def test_symlink_cannot_recycle(output, tmp_path):
    uid, _, path, body = output
    target = tmp_path / 'protected.mp4'
    path.rename(target)
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip('测试环境不支持创建符号链接')
    with pytest.raises(output_files.OutputFileError):
        output_files.recycle(uid, NAME, body)
    assert target.read_bytes() == b'isolated-video'


def test_missing_old_version_does_not_make_replacement_forever_stale(output):
    uid, artifact, path, _ = output
    path.unlink()
    new_path = path.with_name('final_dub_20260928_120000_123456.mp4')
    new_path.write_bytes(b'new-output')
    crud.add_artifact(artifact.project_id, 'assemble', 'video', new_path.name, new_path, str(new_path.relative_to(crud.project_dir(uid))))
    assert crud.step_artifacts_exist(artifact.project_id, 'assemble') is True
    new_path.unlink()
    assert crud.step_artifacts_exist(artifact.project_id, 'assemble') is False


def test_recycle_guard_rejects_permanent_delete():
    recycle_utils.require_recycle(0x80)
    with pytest.raises(recycle_utils.RecycleError):
        recycle_utils.require_recycle(0)


def test_forget_missing_removes_records_only_and_preserves_publication(output, monkeypatch):
    uid, artifact, path, _ = output
    draft = service.create(uid)
    service.save(draft['id'], dict(draft['draft'], revision=1, parts=[dict(artifact_id=artifact.id, title='配音版')]))
    # 同名历史记录也应清掉，不能从旧索引再次冒出来。
    current = crud.add_artifact(artifact.project_id, 'assemble', 'video', NAME, path, artifact.rel_path)
    path.unlink()
    before = [(s.step_key, s.status, s.input_fingerprint) for s in crud.get_steps(artifact.project_id)]
    monkeypatch.setattr(output_files, 'recycle_file', lambda *a: pytest.fail('删除记录不能调用回收站'))
    body = dict(artifact_id=current.id, record_revision=output_files.record_revision(current), confirmed=True)
    from app.main import app
    response = TestClient(app).post(f'/api/projects/{uid}/steps/assemble/artifacts/{NAME}/forget', json=body)
    assert response.status_code == 200 and response.json()['status'] == 'forgotten'
    assert not crud.latest_artifacts(artifact.project_id, ['assemble'])
    assert service.view(draft['id'])['draft']['parts'][0]['artifact_id'] == artifact.id
    assert before == [(s.step_key, s.status, s.input_fingerprint) for s in crud.get_steps(artifact.project_id)]
    assert NAME not in (crud.project_dir(uid) / 'manifest.json').read_text(encoding='utf-8')


@pytest.mark.parametrize('change', ['present', 'restored', 'directory', 'revision', 'id', 'confirmation', 'running', 'publishing'])
def test_forget_missing_guards(output, change):
    uid, artifact, path, _ = output
    body = dict(artifact_id=artifact.id, record_revision=output_files.record_revision(artifact), confirmed=True)
    if change != 'present':
        path.unlink()
    if change == 'restored':
        path.write_bytes(b'restored')
    if change == 'directory':
        path.mkdir()
    if change == 'revision':
        body['record_revision'] = 'old-record'
    if change == 'id':
        body['artifact_id'] += 1
    if change == 'confirmation':
        body['confirmed'] = False
    if change == 'running':
        step = crud.get_step(artifact.project_id, 'assemble'); step.status = 'running'; crud.save_step(step)
    if change == 'publishing':
        with Session(get_engine()) as session:
            session.add(Publication(project_uid=uid, status='uploading')); session.commit()
    with pytest.raises(Exception) as caught:
        output_files.forget_missing(uid, NAME, body)
    assert caught.value.code in {'OUTPUT_FILE_RESTORED', 'OUTPUT_FILE_CHANGED', 'OUTPUT_FILE_INVALID', 'OUTPUT_FILE_BUSY'}
    assert crud.latest_artifacts(artifact.project_id, ['assemble'])


def test_cross_origin_cannot_forget_missing(output):
    uid, artifact, path, _ = output
    path.unlink()
    from app.main import app
    result = TestClient(app).post(f'/api/projects/{uid}/steps/assemble/artifacts/{NAME}/forget',
        json=dict(artifact_id=artifact.id, record_revision=output_files.record_revision(artifact), confirmed=True),
        headers={'Origin': 'https://untrusted.example'})
    assert result.json()['code'] == 'OUTPUT_ORIGIN'
    assert crud.latest_artifacts(artifact.project_id, ['assemble'])


@pytest.mark.parametrize('action', ['recycle', 'forget'])
def test_file_edit_lock_order_matches_workflow_start(output, monkeypatch, action):
    uid, artifact, path, body = output
    acquired = []
    @contextmanager
    def publication_guard(*args, **kwargs):
        acquired.append('publication')
        yield
    @contextmanager
    def production_guard(*args, **kwargs):
        acquired.append('production')
        yield crud.get_project(uid)
    monkeypatch.setattr(service, 'output_replacement', publication_guard)
    monkeypatch.setattr(runner, 'output_file_edit', production_guard)
    monkeypatch.setattr(output_files, 'recycle_file', lambda *a: None)
    if action == 'forget':
        path.unlink()
        body['record_revision'] = output_files.record_revision(artifact)
        output_files.forget_missing(uid, NAME, body)
    else:
        output_files.recycle(uid, NAME, body)
    assert acquired == ['publication', 'production']


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows COM 适配测试')
@pytest.mark.parametrize('mode', ['recycled', 'permanent', 'aborted'])
def test_windows_recycle_flags_and_abort(monkeypatch, tmp_path, mode):
    import pythoncom
    from win32com.shell import shell
    calls = []
    class Operation:
        def SetOperationFlags(self, flags):
            assert flags & 0x00080000 and flags & 0x00100000 and flags & 0x2000
        def DeleteItem(self, item, sink):
            self.sink = sink
        def PerformOperations(self):
            self.sink.PreDeleteItem(0 if mode == 'permanent' else 0x80, None)
            calls.append('recycle')
            self.sink.newItem = 'mock-trash-item'
        def GetAnyOperationsAborted(self):
            return mode == 'aborted'
    monkeypatch.setattr(pythoncom, 'CoInitialize', lambda: calls.append('init'))
    monkeypatch.setattr(pythoncom, 'CoUninitialize', lambda: calls.append('uninit'))
    monkeypatch.setattr(pythoncom, 'CoCreateInstance', lambda *a: Operation())
    monkeypatch.setattr(pythoncom, 'WrapObject', lambda value, *a: value)
    monkeypatch.setattr(shell, 'SHCreateItemFromParsingName', lambda *a: object())
    if mode == 'recycled':
        WINDOWS_RECYCLE(tmp_path / 'mock-video.mp4')
        assert calls == ['init', 'recycle', 'uninit']
    else:
        with pytest.raises(Exception):
            WINDOWS_RECYCLE(tmp_path / 'mock-video.mp4')
        assert calls[-1] == 'uninit'
        if mode == 'permanent':
            assert 'recycle' not in calls
