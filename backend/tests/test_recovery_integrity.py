"""恢复起点核对真实文本，正常大媒体预检不做整文件读取。"""
from conftest import mark_upstream_done
from dubforge.domain import crud
from dubforge.pipeline.recovery import RecoveryDependencies
from dubforge.runner import compute_fingerprint


def test_equal_size_source_edit_moves_resume_to_earlier_producer(make_project):
    from dubforge.chain_recovery import plan
    uid = make_project(params={'_chain': ['translate'], '_chain_state': 'held'})
    mark_upstream_done(uid, 'translate')
    project = crud.get_project(uid)
    step = crud.get_step(project.id, 'split')
    step.input_fingerprint = compute_fingerprint(project, 'split')
    crud.save_step(step)
    original = plan(uid)
    assert original['steps'] == ['translate']
    path = RecoveryDependencies(project).path('split', 'split.txt')
    contents = path.read_bytes()
    path.write_bytes(contents.replace(b'hello', b'world'))
    assert len(contents) == path.stat().st_size
    updated = plan(uid)
    assert updated['steps'] == ['split', 'translate']
    assert updated['recovery']['start_step'] == 'split'
    assert updated['fingerprint'] != original['fingerprint']


def test_equal_size_completed_output_is_updated_instead_of_reported_reusable(make_project):
    from dubforge.chain_recovery import plan
    uid = make_project(params={'_chain': ['translate'], '_chain_state': 'held'})
    mark_upstream_done(uid, 'subtitles')
    project = crud.get_project(uid)
    deps = RecoveryDependencies(project)
    assert deps.fresh('translate')
    path = deps.path('translate', 'translate.txt')
    content = path.read_text(encoding='utf8')
    assert '测试译文' in content
    path.write_text(content.replace('测试译文', '更新译文'), encoding='utf8')
    assert path.stat().st_size == deps.arts['translate/translate.txt'].size
    candidate = plan(uid)
    assert candidate['recovery']['start_step'] == 'translate'
    row = next(r for r in candidate['recovery']['rows'] if r['key'] == 'translate')
    assert row['action'] == 'update' and '内容变化' in row['reason']


def test_untouched_large_media_does_not_read_entire_video(make_project, monkeypatch):
    from dubforge.pipeline import recovery
    uid = make_project()
    project = crud.get_project(uid)
    root = crud.project_dir(uid)
    path = root / 'media' / 'video.mp4'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'x' * (1024 * 1024 + 1))
    crud.add_artifact(project.id, 'download', 'video', 'video.mp4', path, 'media/video.mp4')
    monkeypatch.setattr(recovery, '_file_digest', lambda *args: (_ for _ in ()).throw(
        AssertionError('未修改的大视频不应在恢复预检中整文件读取')))
    assert RecoveryDependencies(project).file_matches('download', 'video.mp4')


def test_modified_large_file_is_verified_once_per_file_version(make_project, monkeypatch):
    from dubforge.pipeline import recovery
    uid = make_project()
    project = crud.get_project(uid)
    root = crud.project_dir(uid)
    path = root / 'media' / 'video.mp4'
    path.parent.mkdir(parents=True, exist_ok=True)
    content = b'x' * (1024 * 1024 + 1)
    path.write_bytes(content)
    crud.add_artifact(project.id, 'download', 'video', 'video.mp4', path, 'media/video.mp4')
    path.write_bytes(b'y' + content[1:])
    reads = []
    actual = recovery.hashlib.file_digest
    def digest(stream, algorithm):
        reads.append(stream.name)
        return actual(stream, algorithm)
    monkeypatch.setattr(recovery.hashlib, 'file_digest', digest)
    assert not RecoveryDependencies(project).file_matches('download', 'video.mp4')
    assert not RecoveryDependencies(project).file_matches('download', 'video.mp4')
    assert len(reads) == 1
