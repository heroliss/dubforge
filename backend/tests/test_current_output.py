"""当前组合的文件身份、索引和发布互斥回归。"""
import pytest
from sqlmodel import Session, select

from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.domain.models import Artifact
from dubforge.pipeline.assembly_outputs import current_output
from dubforge.publishing import service
from dubforge.publishing.models import Publication


def test_current_output_matches_exact_dimensions(tmp_path):
    names = ['final_dub_20260926_120000_123456.mp4', 'final_dub_20260927_120000_123456.mp4',
             'final_dub_hardsub_20260928_120000_123456.mp4', 'final_dub_comments_20260929_120000_123456.mp4',
             '.tmp_pending_final_dub_20260930_120000_123456.mp4', 'final_dub_unknown.mp4']
    for name in names:
        (tmp_path / name).write_bytes(b'video')
    assert current_output(tmp_path, 'dub').name == names[1]
    assert current_output(tmp_path, 'dub_hardsub').name == names[2]
    assert current_output(tmp_path, 'src_hardsub') is None
    assert len(list(tmp_path.iterdir())) == len(names)


def test_artifact_replacement_preserves_id_without_adding_rows(make_project):
    project = crud.get_project(make_project())
    root = crud.project_dir(project.uid)
    path = root / 'output/final_dub_20260927_120000_123456.mp4'
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b'old')
    original = crud.add_artifact(project.id, 'assemble', 'video', path.name, path, str(path.relative_to(root)))
    path.write_bytes(b'new-video')
    replaced = crud.add_artifact(project.id, 'assemble', 'video', path.name, path, str(path.relative_to(root)), replace_existing=True)
    assert replaced.id == original.id
    assert replaced.sha256 != original.sha256
    assert replaced.size == len(b'new-video')
    with Session(get_engine()) as session:
        assert len(session.exec(select(Artifact).where(Artifact.project_id == project.id)).all()) == 1


def test_active_publication_protects_output(make_project):
    uid = make_project()
    with Session(get_engine()) as session:
        row = Publication(project_uid=uid, status='uploading')
        session.add(row); session.commit(); session.refresh(row)
        key = row.id
    with pytest.raises(service.PublishError) as exc:
        with service.output_replacement(uid):
            pytest.fail('正在上传的文件不能被替换')
    assert exc.value.code == 'ASSEMBLE_PUBLISH_BUSY'
    service._update(key, status='paused')
    with service.output_replacement(uid):
        pass
