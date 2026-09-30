"""并发原子写和失败清理，均使用隔离的数据目录。"""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest

from dubforge.domain import crud
from dubforge.utils import fs


def test_concurrent_atomic_writes_have_private_temporary_files(isolated_env, monkeypatch):
    target = isolated_env / "artifact.json"
    ready = Barrier(8)
    temporary_paths = []
    replace = fs.atomic_replace

    def simultaneous_replace(src, dst):
        temporary_paths.append(src)
        ready.wait(timeout=10)
        replace(src, dst)

    monkeypatch.setattr(fs, "atomic_replace", simultaneous_replace)
    payloads = [json.dumps({"writer": i, "content": "测试" * 1000}).encode() for i in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda data: fs.atomic_write_bytes(target, data), payloads))
    assert len(set(temporary_paths)) == 8
    assert target.read_bytes() in payloads
    assert all(not path.exists() for path in temporary_paths)
    assert not list(isolated_env.glob("*.tmp"))


def test_atomic_replace_failure_preserves_file_and_cleans_temporary(isolated_env, monkeypatch):
    target = isolated_env / "artifact.json"
    fs.atomic_write_bytes(target, b"original")

    def fail(*args):
        raise PermissionError("模拟文件占用")

    monkeypatch.setattr(fs, "atomic_replace", fail)
    with pytest.raises(PermissionError):
        fs.atomic_write_bytes(target, b"replacement")
    assert target.read_bytes() == b"original"
    assert not list(isolated_env.glob("*.tmp"))


def test_manifest_writers_serialize_snapshot_and_write(make_project, monkeypatch):
    import time

    uid = make_project()
    ready = Barrier(8)
    guard = Lock()
    active = maximum = 0
    original = crud._write_manifest

    def tracked(project_uid):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(0.01)  # 扩大重叠窗口，验证锁覆盖快照读取到落盘的全过程。
            original(project_uid)
        finally:
            with guard:
                active -= 1

    def write(_):
        ready.wait(timeout=10)
        crud.write_manifest(uid)

    monkeypatch.setattr(crud, "_write_manifest", tracked)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(8)))
    assert maximum == 1
    manifest = json.loads((crud.project_dir(uid) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["uid"] == uid
    assert manifest["title"] == crud.get_project(uid).title


def test_manifest_does_not_recreate_moved_project_directory(make_project, monkeypatch):
    uid = make_project()
    root = crud.project_dir(uid)
    moved = root.with_name(root.name + "_moved")
    original = crud.atomic_write_bytes

    def move_before_write(*args, **kwargs):
        root.rename(moved)
        return original(*args, **kwargs)

    monkeypatch.setattr(crud, "atomic_write_bytes", move_before_write)
    crud.write_manifest(uid)
    assert moved.is_dir()
    assert not root.exists()
