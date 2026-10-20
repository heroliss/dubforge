"""逐句准备复用大输入摘要；缓存局限于依赖快照且不掩盖文件变化。"""
import hashlib
import os

import pytest

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline.dependencies import Dependencies
from test_dependencies import seed


@pytest.fixture()
def prepared_dependencies(make_project):
    uid = make_project(params={'step_params': {
        'tts.backend': 'indextts25', 'tts.indextts25.timbre_mode': 'dynamic',
        'tts.indextts25.emo_mode': 'same', 'separate.enabled': True,
    }})
    seed(uid, 'split', 'split.txt', '1. 0.000-1.000: Hello\n2. 1.500-2.500: World\n')
    seed(uid, 'translate', 'translate.txt', '1. 你好\n2. 世界\n')
    # 超过轻量依赖状态检查的文本全量核验阈值，模拟真正的大原声。
    path = seed(uid, 'preprocess', 'vocal.wav', 'a' * (1024 * 1024 + 8))
    deps = Dependencies(crud.get_project(uid))
    # 此测试只针对严格内容核验；不让 mtime 的快速门禁提前替它报错。
    deps.arts['preprocess/vocal.wav'].created_at = None
    return deps, path


@pytest.fixture()
def vocal_digest_reads(monkeypatch):
    original = hashlib.file_digest
    reads = []

    def tracked(stream, *args, **kwargs):
        if str(stream.name).endswith('vocal.wav'):
            reads.append(stream.name)
        return original(stream, *args, **kwargs)

    monkeypatch.setattr(hashlib, 'file_digest', tracked)
    return reads


def require_lines(deps):
    for line in deps.dubbing_lines(required=True):
        deps.require('workbench', line=line)


def test_repeated_lines_verify_same_large_input_once(prepared_dependencies, vocal_digest_reads):
    deps, _ = prepared_dependencies
    require_lines(deps)
    require_lines(deps)
    assert len(vocal_digest_reads) == 1


@pytest.mark.parametrize('change', ['write', 'write_restore_mtime', 'replace'])
def test_same_size_write_and_atomic_replace_reverify(prepared_dependencies, vocal_digest_reads, change):
    deps, path = prepared_dependencies
    require_lines(deps)
    before = path.stat()
    content = b'b' * before.st_size
    if change == 'replace':
        temporary = path.with_suffix('.replacement')
        temporary.write_bytes(content)
        # 即使大小和 mtime 相同，文件身份不同也不能复用旧摘要。
        os.utime(temporary, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(temporary, path)
    else:
        path.write_bytes(content)
        modified = before.st_mtime_ns if change == 'write_restore_mtime' else before.st_mtime_ns + 1_000_000
        os.utime(path, ns=(before.st_atime_ns, modified))
    with pytest.raises(DubForgeError, match='内容已变化') as exc:
        require_lines(deps)
    assert exc.value.code == 'DEPS_NOT_MET'
    assert len(vocal_digest_reads) == 2
    # 更换登记摘要只影响本次判断，不能把先前“不匹配”作为永久缓存。
    deps.arts['preprocess/vocal.wav'].sha256 = hashlib.sha256(content).hexdigest()
    require_lines(deps)
    assert len(vocal_digest_reads) == 2


def test_new_snapshot_does_not_reuse_previous_digest(prepared_dependencies, vocal_digest_reads):
    deps, _ = prepared_dependencies
    require_lines(deps)
    fresh = Dependencies(deps.project, settings=deps.settings, overrides=deps.overrides)
    require_lines(fresh)
    assert len(vocal_digest_reads) == 2


def test_shallow_variant_snapshot_shares_only_digest_cache(prepared_dependencies, vocal_digest_reads):
    deps, _ = prepared_dependencies
    require_lines(deps)
    variant = deps.with_assembly_variants(['dub'])
    assert variant._input_digests is deps._input_digests
    require_lines(variant)
    assert len(vocal_digest_reads) == 1


def test_expected_digest_is_compared_on_every_require(prepared_dependencies, vocal_digest_reads):
    deps, _ = prepared_dependencies
    require_lines(deps)
    deps.arts['preprocess/vocal.wav'].sha256 = '0' * 64
    with pytest.raises(DubForgeError, match='内容已变化'):
        require_lines(deps)
    assert len(vocal_digest_reads) == 1


def test_change_during_hash_is_not_cached(prepared_dependencies, vocal_digest_reads, monkeypatch):
    deps, path = prepared_dependencies
    original = hashlib.file_digest
    content = b'c' * path.stat().st_size
    changed = False

    def mutate(stream, *args, **kwargs):
        nonlocal changed
        result = original(stream, *args, **kwargs)
        if stream.name == str(path) and not changed:
            changed = True
            before = path.stat()
            path.write_bytes(content)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
        return result

    monkeypatch.setattr(hashlib, 'file_digest', mutate)
    with pytest.raises(DubForgeError, match='核验期间发生变化') as exc:
        require_lines(deps)
    assert exc.value.code == 'DEPS_NOT_MET'
    assert str(path) not in deps._input_digests
    deps.arts['preprocess/vocal.wav'].sha256 = hashlib.sha256(content).hexdigest()
    require_lines(deps)
    assert len(vocal_digest_reads) == 2


def test_change_during_cache_hit_is_rejected(prepared_dependencies, vocal_digest_reads, monkeypatch):
    deps, path = prepared_dependencies
    require_lines(deps)
    original = os.fstat
    changed = False

    def mutate_after_first_handle_stat(descriptor):
        nonlocal changed
        result = original(descriptor)
        if not changed:
            changed = True
            before = path.stat()
            path.write_bytes(b'd' * before.st_size)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
        return result

    monkeypatch.setattr(os, 'fstat', mutate_after_first_handle_stat)
    with pytest.raises(DubForgeError, match='核验期间发生变化'):
        deps._input_digest(path)
    assert str(path) not in deps._input_digests
    assert len(vocal_digest_reads) == 1
