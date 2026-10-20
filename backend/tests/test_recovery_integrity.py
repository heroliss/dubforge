"""恢复起点核对真实文本，正常大媒体预检不做整文件读取。"""
from conftest import mark_upstream_done
from dubforge.domain import crud
from dubforge.pipeline.recovery import RecoveryDependencies
from dubforge.runner import compute_fingerprint


def _seed_text(uid, step, name, text):
    """登记一份内容自洽的文本产物，模拟旧批次留下的成功缓存。"""
    project = crud.get_project(uid)
    root = crud.project_dir(uid)
    path = root / 'artifacts' / step / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    crud.add_artifact(project.id, step, 'text', name, path,
                      str(path.relative_to(root)))
    row = crud.get_step(project.id, step)
    row.status = 'done'
    crud.save_step(row)
    return path


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


def test_tts_alignment_recovery_requeues_translate_when_steps_omit_producer(make_project):
    """旧全流程只登记 tts 时，译文合同破坏也必须从 translate 恢复。"""
    from dubforge.pipeline import recovery

    uid = make_project()
    _seed_text(uid, 'split', 'split.txt',
               '1. 0.000-1.000: hello\n2. 1.100-2.000: world\n')
    _seed_text(uid, 'translate', 'translate.txt', '1. 你好\n')
    project = crud.get_project(uid)
    deps = RecoveryDependencies(project)

    stage, error = deps.dubbing_alignment_error()
    assert stage == 'translate' and error.code == 'TEXT_ALIGNMENT'
    plan = recovery.describe(deps, ['tts'], previous_steps=['tts'], previous_cursor=0)
    rows = {row['key']: row for row in plan['rows']}
    assert rows['split']['action'] == 'reuse'
    assert rows['translate']['action'] in {'create', 'update', 'retry'}
    assert plan['start_step'] == 'translate'
    assert plan['execute_steps'][:2] == ['translate', 'tts']


def test_recovery_propagates_stale_split_to_legacy_dubbed_workflow(make_project):
    """旧流程只登记 split/comments/assemble 时，split 变更必须重建译文和配音。"""
    from dubforge.pipeline import recovery

    uid = make_project()
    _seed_text(uid, 'split', 'split.txt',
               '1. 0.000-1.000: hello\n2. 1.100-2.000: world\n')
    _seed_text(uid, 'translate', 'translate.txt', '1. 你好\n2. 世界\n')
    project = crud.get_project(uid)
    # 模拟旧产物合同：当前 split 需要重建，而译文/TTS 的旧文件仍与旧
    # split 指纹相符；恢复计划必须按依赖传播使下游失效。
    split = crud.get_step(project.id, 'split')
    split.status = 'stale'
    crud.save_step(split)
    # 依赖快照在构造时冻结；模拟外部步骤状态变化后重新读取。
    deps = RecoveryDependencies(crud.get_project(uid), assemble_variants=['dub'])
    result = recovery.describe(deps, ['split', 'comments', 'assemble'],
                               previous_steps=['split', 'comments', 'assemble'])
    execution = result['execute_steps']
    # 本测试未播种更早的媒体/转录产物，计划可以合法地把这些缺失依赖排在
    # split 前；关键合同是 split → translate → tts → comments → assemble。
    assert execution.index('split') < execution.index('translate') < execution.index('tts')
    assert execution.index('tts') < execution.index('comments') < execution.index('assemble')
    rows = {row['key']: row for row in result['rows']}
    assert rows['split']['action'] in {'update', 'retry'}
    assert rows['translate']['action'] in {'update', 'retry', 'create'}
    assert rows['tts']['action'] in {'update', 'create'}


def test_failed_upstream_with_verified_cache_is_not_requeued(make_project, monkeypatch):
    """合成失败时，仍被下游版本证明有效的文字缓存不应被递归重排。"""
    from dubforge.pipeline import recovery

    uid = make_project()
    deps = recovery.RecoveryDependencies(crud.get_project(uid))

    # 模拟已验证的历史缓存：这里刻意让普通 fresh 全部为 False，
    # 只允许恢复专用的“失败但已核验”判定通过。
    monkeypatch.setattr(recovery, 'fresh', lambda _deps, _key: False)
    monkeypatch.setattr(recovery, '_verified_failed_cache',
                        lambda _deps, key: key != 'assemble')
    result = recovery.describe(deps, ['assemble'])

    assert result['execute_steps'] == ['assemble']
    assert all(row['action'] == 'reuse' for row in result['rows'] if row['key'] != 'assemble')


def test_verified_failed_cache_rejects_missing_artifact_digest(make_project, monkeypatch):
    """失败步骤不能仅凭文件存在复用，登记摘要缺失时必须重做。"""
    from dubforge.pipeline import assembly_versions, recovery

    uid = make_project()
    _seed_text(uid, 'split', 'split.txt', '1. 0.000-1.000: hello\n')
    project = crud.get_project(uid)
    step = crud.get_step(project.id, 'split')
    step.status = 'failed'
    crud.save_step(step)
    deps = recovery.RecoveryDependencies(project)
    deps.assembly_versions = [{'id': 'cached'}]
    monkeypatch.setattr(assembly_versions, 'is_current', lambda *_args, **_kwargs: True)

    artifact = deps.arts['split/split.txt']
    artifact.sha256 = ''
    assert not recovery._verified_failed_cache(deps, 'split')
