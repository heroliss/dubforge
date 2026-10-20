"""项目删除只允许安全回收；全部往返使用隔离目录和假回收站。"""
import json

import pytest
from sqlmodel import select

from dubforge import configuration as cfg
from dubforge.configuration_models import ConfigurationScope
from dubforge.domain import crud
from dubforge.domain.models import Artifact, Job, StepRun
from dubforge.project_archive import set_archived
from dubforge.publishing.models import Publication
from dubforge.utils import recycle


def _bind(uid, template):
    plan = cfg.binding_plan([uid], template['key'][7:])
    cfg.bind(dict(uids=[uid], template_id=template['key'][7:],
                  confirmed=True, fingerprint=plan['fingerprint']))


def _historical_job(project):
    with crud.session() as session:
        row = Job(project_id=project.id, step_key='tts', status='done', log_path='logs/tts.log')
        session.add(row)
        session.commit()


@pytest.mark.parametrize('template_missing', [False, True])
def test_archived_project_can_be_recycled_and_rebuilt_with_all_files(make_project, isolated_env, monkeypatch,
                                                                   template_missing):
    uid = make_project(params={'auto_steps': ['download', 'tts'], 'custom': '原参数'})
    project = crud.get_project(uid)
    directory = crud.project_dir(uid)
    template = cfg.create_template('归档的旅行系列')
    template = cfg.save(template['key'], dict(revision=template['revision'], updates={'assemble.subtitle_color': '#FFCC44'}))
    _bind(uid, template)
    cfg.save('project:' + uid, dict(revision=cfg.view('project:' + uid)['revision'], updates={'assemble.voice_volume': 0}))
    audio = directory / 'audio' / '0001.wav'
    audio.write_bytes(b'completed dubbing')
    crud.add_artifact(project.id, 'tts', 'audio', '0001.wav', audio, 'audio/0001.wav')
    log = directory / 'logs' / 'tts.log'
    log.write_text('已完成的配音日志', encoding='utf-8')
    step = crud.get_step(project.id, 'tts')
    step.status, step.input_fingerprint = 'done', 'completed-tts-fingerprint'
    crud.save_step(step)
    _historical_job(project)
    with crud.session() as session:
        row = Publication(project_uid=uid, status='published', receipt=json.dumps({'bvid': 'BV-existing'}))
        session.add(row)
        session.commit()
        receipt_id = row.id
    archived_at = set_archived(uid, True).archived_at
    # 模板归档只写数据库；删除前必须刷新其最新状态，不能依赖已有清单。
    cfg.archive(template['key'], dict(revision=template['revision'], archived=True))
    project = crud.get_project(uid)
    project.params = json.dumps({'auto_steps': ['download', 'tts'], 'custom': '最近修改的参数'})
    crud.save_project(project)
    before_files = {p.relative_to(directory): p.read_bytes() for p in directory.rglob('*')
                    if p.is_file() and p.name != 'manifest.json'}
    trash = isolated_env / 'fake-recycle' / directory.name
    trash.parent.mkdir()

    def fake_recycle(path):
        assert path.resolve() == directory.resolve()
        assert trash.resolve().is_relative_to(isolated_env.resolve())
        path.rename(trash)

    monkeypatch.setattr(recycle, '_windows_recycle', fake_recycle)
    # 非 Windows 测试也走同一假回收站，不使用系统垃圾目录。
    monkeypatch.setattr(recycle.sys, 'platform', 'win32')
    assert crud.delete_project(uid) is True
    assert crud.get_project(uid) is None and not directory.exists()
    manifest = json.loads((trash / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['params']['custom'] == '最近修改的参数'
    assert manifest['archived_at']
    assert next(layer for layer in manifest['configuration']['layers'] if layer['key'] == template['key'])['archived'] is True
    with crud.session() as session:
        assert session.get(Publication, receipt_id).status == 'published'
        assert not session.exec(select(Job).where(Job.project_id == project.id)).all()
        assert session.get(ConfigurationScope, 'project:' + uid) is None
        if template_missing:
            session.delete(session.get(ConfigurationScope, template['key']))
            session.commit()
    trash.rename(directory)
    result = crud.rebuild_from_dirs()
    assert result['projects'] == 1 and result['artifacts'] == 1
    restored = crud.get_project(uid)
    assert restored.dir_name == directory.name and crud.project_dir(uid) == directory
    assert restored.archived_at == archived_at
    assert crud.project_params(restored)['custom'] == '最近修改的参数'
    assert cfg.view(template['key'])['archived'] is True
    assert cfg.resolve(uid)['values']['assemble.subtitle_color'] == '#FFCC44'
    assert cfg.resolve(uid)['values']['assemble.voice_volume'] == 0
    restored_step = crud.get_step(restored.id, 'tts')
    assert restored_step.status == 'done' and restored_step.input_fingerprint == 'completed-tts-fingerprint'
    restored_audio = crud.latest_artifacts(restored.id, ['tts'])['tts/0001.wav']
    assert restored_audio.sha256 and restored_audio.rel_path == 'audio/0001.wav'
    assert {p.relative_to(directory): p.read_bytes() for p in directory.rglob('*')
            if p.is_file() and p.name != 'manifest.json'} == before_files
    assert crud.rebuild_from_dirs() == {'projects': 0, 'steps': 0, 'artifacts': 0}


def test_recycle_failure_keeps_project_and_all_registration(make_project, monkeypatch):
    uid = make_project()
    project = crud.get_project(uid)
    directory = crud.project_dir(uid)
    audio = directory / 'audio' / '0001.wav'
    audio.write_bytes(b'dubbing')
    crud.add_artifact(project.id, 'tts', 'audio', '0001.wav', audio, 'audio/0001.wav')
    _historical_job(project)
    with crud.session() as session:
        before = {model: [row.model_dump() for row in session.exec(select(model).where(model.project_id == project.id)).all()]
                  for model in (Artifact, Job, StepRun)}

    def unavailable(path):
        raise recycle.RecycleError('隔离测试：回收站不可用')

    monkeypatch.setattr(recycle, '_windows_recycle', unavailable)
    monkeypatch.setattr(recycle.sys, 'platform', 'win32')
    with pytest.raises(recycle.RecycleError):
        crud.delete_project(uid)
    assert crud.get_project(uid).model_dump() == project.model_dump()
    assert audio.read_bytes() == b'dubbing' and (directory / 'manifest.json').is_file()
    with crud.session() as session:
        assert {model: [row.model_dump() for row in session.exec(select(model).where(model.project_id == project.id)).all()]
                for model in (Artifact, Job, StepRun)} == before


def test_manifest_refresh_failure_never_attempts_recycle(make_project, monkeypatch):
    uid = make_project()
    directory = crud.project_dir(uid)
    previous = (directory / 'manifest.json').read_bytes()

    def failed(_uid):
        raise OSError('隔离测试：磁盘写入失败')

    monkeypatch.setattr(crud, 'write_manifest', failed)
    monkeypatch.setattr(recycle, 'recycle_file', lambda _path: pytest.fail('清单失败时不能回收'))
    with pytest.raises(crud.ProjectDeletionError) as error:
        crud.delete_project(uid)
    assert error.value.code == 'PROJECT_DELETE_MANIFEST_FAILED'
    assert crud.get_project(uid) is not None and (directory / 'manifest.json').read_bytes() == previous


def test_unconfirmed_recycle_keeps_project_registration(make_project, monkeypatch):
    uid = make_project()
    directory = crud.project_dir(uid)
    monkeypatch.setattr(recycle, '_windows_recycle', lambda _path: None)
    monkeypatch.setattr(recycle.sys, 'platform', 'win32')
    with pytest.raises(recycle.RecycleError, match='仍在原位置'):
        crud.delete_project(uid)
    assert crud.get_project(uid) is not None and directory.is_dir()


def test_project_delete_preserves_external_original(isolated_env):
    external = isolated_env / 'external-video.mp4'
    external.write_bytes(b'original video')
    project = crud.create_project(title='外部源文件', source_kind='local', source_url=str(external))
    directory = crud.project_dir(project.uid)
    assert crud.delete_project(project.uid) is True
    assert external.read_bytes() == b'original video' and not directory.exists()


@pytest.mark.parametrize('name', ['.', '..', '../outside', 'nested/project', 'nested\\project', 'absolute'])
def test_invalid_project_directory_never_recycles_storage_root_or_external_files(make_project, isolated_env,
                                                                               monkeypatch, name):
    uid = make_project()
    directory = crud.project_dir(uid)
    outside = isolated_env / 'outside'
    outside.mkdir()
    sentinel = outside / 'keep.txt'
    sentinel.write_bytes(b'keep external file')
    project = crud.get_project(uid)
    project.dir_name = str(outside) if name == 'absolute' else name
    crud.save_project(project)
    monkeypatch.setattr(recycle, 'recycle_file', lambda _path: pytest.fail('无效目录不能进入回收流程'))
    with pytest.raises(crud.ProjectDeletionError) as error:
        crud.delete_project(uid)
    assert error.value.code == 'PROJECT_DELETE_PATH_INVALID'
    assert crud.get_project(uid) is not None and directory.is_dir()
    assert sentinel.read_bytes() == b'keep external file'


def test_symlink_project_directory_cannot_recycle_external_directory(make_project, isolated_env, monkeypatch):
    uid = make_project()
    directory = crud.project_dir(uid)
    external = isolated_env / 'external-directory'
    external.mkdir()
    sentinel = external / 'keep.txt'
    sentinel.write_bytes(b'keep')
    link = directory.with_name(directory.name + '_link')
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip('测试环境不支持创建目录符号链接')
    project = crud.get_project(uid)
    project.dir_name = link.name
    crud.save_project(project)
    monkeypatch.setattr(recycle, 'recycle_file', lambda _path: pytest.fail('链接目录不能回收'))
    with pytest.raises(crud.ProjectDeletionError) as error:
        crud.delete_project(uid)
    assert error.value.code == 'PROJECT_DELETE_PATH_INVALID'
    assert crud.get_project(uid) is not None and sentinel.read_bytes() == b'keep'


def test_legacy_manifest_without_template_archive_field_is_compatible(make_project):
    uid = make_project()
    project = crud.get_project(uid)
    template = cfg.create_template('旧模板')
    _bind(uid, template)
    directory = crud.project_dir(uid)
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    for layer in manifest['configuration']['layers']:
        layer.pop('archived', None)
    (directory / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    with crud.session() as session:
        for row in session.exec(select(StepRun).where(StepRun.project_id == project.id)).all():
            session.delete(row)
        session.delete(session.get(ConfigurationScope, 'project:' + uid))
        session.delete(session.get(ConfigurationScope, template['key']))
        session.delete(session.get(type(project), project.id))
        session.commit()
    assert crud.rebuild_from_dirs()['projects'] == 1
    assert cfg.view(template['key'])['archived'] is False
    assert crud.project_dir(uid) == directory
