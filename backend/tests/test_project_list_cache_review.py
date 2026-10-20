"""列表性能缓存不能隐藏 Windows 同大小、恢复时间戳的文件改写。"""
import json
import os
import struct
import threading
import wave
from types import SimpleNamespace

from dubforge.pipeline import dubbing_edit, voice_timing
from dubforge.pipeline.line_settings import identity


def _rewrite_preserving_mtime(path, value):
    before = path.stat()
    encoded = json.dumps(value).encode('utf-8')
    assert len(encoded) == before.st_size
    # 必须原地改写：原子替换会改变 inode，无法覆盖 Windows 创建时间语义。
    path.write_bytes(encoded)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


def _env(tmp_path):
    row = dict(id=1, text='测试', start=2.0, end=3.0, source='dub', take=1)
    project = SimpleNamespace(uid='review', params='{}', source_kind='link', source_url='')
    get = lambda key, default=None: default
    deps = SimpleNamespace(root=tmp_path, project=project, cfg=get,
                           production_lines=lambda: [row],
                           _take_snapshots={}, _voice_timing_cache={}, version=None)
    path = dubbing_edit.take_path(tmp_path, 1, 1)
    path.parent.mkdir(parents=True)
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b'\0\0' * 8000)
    recorded = dict(identity=identity(row), version=dubbing_edit.file_version(path))
    metadata = path.with_suffix('.take.json')
    metadata.write_text(json.dumps(recorded), encoding='utf-8')
    return project, deps, row, metadata, recorded


def test_take_snapshot_rechecks_same_size_metadata_with_restored_mtime(tmp_path):
    _, deps, row, metadata, recorded = _env(tmp_path)
    assert dubbing_edit.take_snapshot(tmp_path, row, 1, dependencies=deps) == recorded
    changed = {**recorded, 'identity': 'f' * 64}
    assert changed != recorded
    _rewrite_preserving_mtime(metadata, changed)
    assert dubbing_edit.take_snapshot(tmp_path, row, 1, dependencies=deps) == changed


def test_voice_fingerprint_rechecks_cached_take_metadata_with_restored_mtime(tmp_path, monkeypatch):
    _, deps, _, metadata, recorded = _env(tmp_path)
    monkeypatch.setattr(voice_timing, '_video_input', lambda *args: None)
    assert voice_timing.fingerprint(deps)['voices'][0]['version'] is not None
    _rewrite_preserving_mtime(metadata, {**recorded, 'identity': 'f' * 64})
    assert voice_timing.fingerprint(deps)['voices'][0]['version'] is None


def test_voice_plan_rechecks_cached_take_metadata_with_restored_mtime(tmp_path):
    project, deps, row, metadata, recorded = _env(tmp_path)
    first = voice_timing.plan(project, tmp_path, [row], deps.cfg,
                              video_duration=20, dependencies=deps)
    assert first['timing']['missing_ids'] == []
    _rewrite_preserving_mtime(metadata, {**recorded, 'identity': 'f' * 64})
    current = voice_timing.plan(project, tmp_path, [row], deps.cfg,
                                video_duration=20, dependencies=deps)
    assert current['timing']['missing_ids'] == [1]
    assert current['timing']['lines'][0]['seconds'] is None


def test_voice_plan_rechecks_same_size_wav_with_restored_mtime(tmp_path):
    project, deps, row, _, _ = _env(tmp_path)
    first = voice_timing.plan(project, tmp_path, [row], deps.cfg,
                              video_duration=20, dependencies=deps)
    assert first['timing']['lines'][0]['seconds'] == 1.0
    path = dubbing_edit.take_path(tmp_path, 1, 1)
    before = path.stat()
    changed = bytearray(path.read_bytes())
    # 不改变数据长度，只将合法 PCM WAV 的采样率翻倍，实际时长变为半秒。
    struct.pack_into('<I', changed, 24, 16000)
    struct.pack_into('<I', changed, 28, 32000)
    path.write_bytes(changed)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    current = voice_timing.plan(project, tmp_path, [row], deps.cfg,
                                video_duration=20, dependencies=deps)
    assert current['timing']['lines'][0]['seconds'] == 0.5


def test_retired_list_owner_cannot_remove_new_in_flight_read(isolated_env, monkeypatch):
    from fastapi import Request
    from app.api import projects

    old_started, old_release = threading.Event(), threading.Event()
    new_started, new_release = threading.Event(), threading.Event()
    calls, old_result, new_result = [], [], []

    def build(_archive):
        calls.append(1)
        number = len(calls)
        if number == 1:
            old_started.set()
            assert old_release.wait(3)
            return [{'uid': 'old-read'}]
        if number == 3:
            new_started.set()
            assert new_release.wait(3)
        return [{'uid': 'current-read'}]

    monkeypatch.setattr(projects, '_build_project_list', build)
    old = threading.Thread(target=lambda: old_result.append(projects.list_projects()))
    current = threading.Thread(target=lambda: new_result.append(projects.list_projects()))
    old.start()
    try:
        assert old_started.wait(2)
        fresh = Request({'type': 'http', 'headers': [(b'x-dubforge-refresh', b'fresh')]})
        assert projects.list_projects(request=fresh) == [{'uid': 'current-read'}]
        current.start()
        assert new_started.wait(2)
        key = (projects._workspace_id(), 'active')
        active_read = projects._list_flights[key]
        old_release.set()
        old.join(2)
        assert not old.is_alive()
        assert projects._list_flights.get(key) is active_read
    finally:
        old_release.set()
        new_release.set()
        old.join(3)
        if current.ident is not None:
            current.join(3)
    assert old_result == [[{'uid': 'old-read'}]]
    assert new_result == [[{'uid': 'current-read'}]]
    assert not projects._list_flights
