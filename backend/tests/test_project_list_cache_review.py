"""列表性能缓存不能隐藏 Windows 同大小、恢复时间戳的文件改写。"""
import json
import os
import struct
import threading
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def _freeze_file_stat(monkeypatch, metadata):
    """模拟 Windows 上原地改写前后全部文件属性都相同，不靠时钟精度复现。"""
    saved = metadata.stat()
    stat, fstat = Path.stat, os.fstat

    def path_stat(path, *args, **kwargs):
        current = stat(path, *args, **kwargs)
        return saved if path == metadata else current

    def opened_stat(fd):
        current = fstat(fd)
        return saved if (current.st_dev, current.st_ino) == (saved.st_dev, saved.st_ino) else current

    monkeypatch.setattr(Path, 'stat', path_stat)
    monkeypatch.setattr(os, 'fstat', opened_stat)


@pytest.mark.parametrize('unchanged_stat', [False, True])
def test_take_snapshot_rechecks_same_size_metadata_with_restored_mtime(tmp_path, monkeypatch, unchanged_stat):
    _, deps, row, metadata, recorded = _env(tmp_path)
    if unchanged_stat:
        _freeze_file_stat(monkeypatch, metadata)
    assert dubbing_edit.take_snapshot(tmp_path, row, 1, dependencies=deps) == recorded
    changed = {**recorded, 'identity': 'f' * 64}
    assert changed != recorded
    _rewrite_preserving_mtime(metadata, changed)
    assert dubbing_edit.take_snapshot(tmp_path, row, 1, dependencies=deps) == changed


@pytest.mark.parametrize('unchanged_stat', [False, True])
def test_voice_fingerprint_rechecks_cached_take_metadata_with_restored_mtime(tmp_path, monkeypatch, unchanged_stat):
    _, deps, _, metadata, recorded = _env(tmp_path)
    if unchanged_stat:
        _freeze_file_stat(monkeypatch, metadata)
    monkeypatch.setattr(voice_timing, '_video_input', lambda *args: None)
    assert voice_timing.fingerprint(deps)['voices'][0]['version'] is not None
    _rewrite_preserving_mtime(metadata, {**recorded, 'identity': 'f' * 64})
    assert voice_timing.fingerprint(deps)['voices'][0]['version'] is None


@pytest.mark.parametrize('unchanged_stat', [False, True])
def test_voice_plan_rechecks_cached_take_metadata_with_restored_mtime(tmp_path, monkeypatch, unchanged_stat):
    project, deps, row, metadata, recorded = _env(tmp_path)
    if unchanged_stat:
        _freeze_file_stat(monkeypatch, metadata)
    first = voice_timing.plan(project, tmp_path, [row], deps.cfg,
                              video_duration=20, dependencies=deps)
    assert first['timing']['missing_ids'] == []
    _rewrite_preserving_mtime(metadata, {**recorded, 'identity': 'f' * 64})
    current = voice_timing.plan(project, tmp_path, [row], deps.cfg,
                                video_duration=20, dependencies=deps)
    assert current['timing']['missing_ids'] == [1]
    assert current['timing']['lines'][0]['seconds'] is None


@pytest.mark.parametrize('unchanged_stat', [False, True])
@pytest.mark.parametrize('implicit_source', [False, True])
def test_voice_plan_rechecks_same_size_wav_with_restored_mtime(tmp_path, monkeypatch, unchanged_stat, implicit_source):
    project, deps, row, _, _ = _env(tmp_path)
    if implicit_source:
        row.pop('source')
    first = voice_timing.plan(project, tmp_path, [row], deps.cfg,
                              video_duration=20, dependencies=deps)
    assert first['timing']['lines'][0]['seconds'] == 1.0
    path = dubbing_edit.take_path(tmp_path, 1, 1)
    if unchanged_stat:
        _freeze_file_stat(monkeypatch, path)
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


@pytest.mark.parametrize('content', [b'[]', b'{"identity":', b'\xff'])
def test_invalid_record_does_not_reuse_good_snapshot_or_poison_repair(tmp_path, monkeypatch, content):
    _, deps, row, metadata, recorded = _env(tmp_path)
    _freeze_file_stat(monkeypatch, metadata)
    assert dubbing_edit.take_snapshot(tmp_path, row, 1, dependencies=deps) == recorded
    metadata.write_bytes(content)
    assert dubbing_edit.take_snapshot(tmp_path, row, 1, dependencies=deps) == {}
    assert str(metadata) not in deps._take_snapshots
    metadata.write_text(json.dumps(recorded), encoding='utf-8')
    assert dubbing_edit.take_snapshot(tmp_path, row, 1, dependencies=deps) == recorded


@pytest.mark.parametrize('consumer', ['fingerprint', 'plan'])
def test_cache_key_and_consumer_share_metadata_even_if_file_changes_back(tmp_path, monkeypatch, consumer):
    project, deps, row, metadata, recorded = _env(tmp_path)
    _freeze_file_stat(monkeypatch, metadata)
    changed = {**recorded, 'identity': 'f' * 64}
    monkeypatch.setattr(voice_timing, '_video_input', lambda *args: None)
    target = dubbing_edit if consumer == 'fingerprint' else voice_timing
    name = 'playable' if consumer == 'fingerprint' else '_duration'
    original = getattr(target, name)
    calls = []

    def change_while_consuming(*args, **kwargs):
        calls.append(1)
        # key 读取 A 后，消费期间短暂出现 B，再在登记缓存前恢复 A。
        _rewrite_preserving_mtime(metadata, changed)
        try:
            return original(*args, **kwargs)
        finally:
            _rewrite_preserving_mtime(metadata, recorded)

    monkeypatch.setattr(target, name, change_while_consuming)
    if consumer == 'fingerprint':
        first = voice_timing.fingerprint(deps)
        assert first['voices'][0]['version'] is not None
        assert voice_timing.fingerprint(deps) == first
    else:
        first = voice_timing.plan(project, tmp_path, [row], deps.cfg, 20, dependencies=deps)
        assert first['timing']['missing_ids'] == []
        assert first['timing']['lines'][0]['seconds'] == 1.0
        assert voice_timing.plan(project, tmp_path, [row], deps.cfg, 20, dependencies=deps) == first
    assert calls == [1]


def _pcm_container(*, junk=None, tag=1, extra=b''):
    fmt = struct.pack('<HHIIHH', tag, 1, 8000, 16000, 2, 16)
    chunks = b'fmt ' + struct.pack('<I', len(fmt)) + fmt
    if junk is not None:
        chunks += b'JUNK' + struct.pack('<I', junk) + b'\0' * (junk + junk % 2)
    chunks += extra
    data = b'\0\0' * 8000
    body = b'WAVE' + chunks + b'data' + struct.pack('<I', len(data)) + data
    return b'RIFF' + struct.pack('<I', len(body)) + body


@pytest.mark.parametrize('junk', [None, 3, 4044])
def test_wav_cache_identity_reads_only_bounded_header_and_never_pcm(tmp_path, monkeypatch, junk):
    _env(tmp_path)
    path = dubbing_edit.take_path(tmp_path, 1)
    content = _pcm_container(junk=junk)
    path.write_bytes(content)
    payload_start = content.index(b'data') + 8
    original = Path.open
    reads = []

    class HeaderOnly:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, size=-1):
            assert not hasattr(self.stream, 'raw'), '容器头必须直接读取原始 FileIO，禁止缓冲预读 PCM'
            start = self.stream.tell()
            assert size >= 0 and start + size <= min(payload_start, 4096)
            value = self.stream.read(size)
            reads.append((start, len(value)))
            return value

    def opened(item, *args, **kwargs):
        stream = original(item, *args, **kwargs)
        if item == path and kwargs.get('buffering') == 0:
            # 检查原始 FileIO，无 BufferedReader 的底层预读遗漏。
            assert not hasattr(stream, 'raw')
        return HeaderOnly(stream) if item == path else stream

    monkeypatch.setattr(Path, 'open', opened)
    assert voice_timing._audio_read_identity(path) is not None
    assert sum(size for _, size in reads) <= 4096
    assert max(start + size for start, size in reads) == payload_start


@pytest.mark.parametrize('kind', ['short', 'unknown_encoding', 'rf64', 'far_data',
                                  'truncated_container', 'unknown_chunk', 'short_fmt'])
def test_unproven_wav_header_bypasses_caches_without_ffprobe(tmp_path, monkeypatch, kind):
    project, deps, row, metadata, recorded = _env(tmp_path)
    path = dubbing_edit.take_path(tmp_path, 1)
    if kind == 'short':
        content = b'RIFF'
    elif kind == 'unknown_encoding':
        content = _pcm_container(tag=2)
    elif kind == 'rf64':
        content = b'RF64' + _pcm_container()[4:]
    elif kind == 'far_data':
        content = _pcm_container(junk=4046)
    elif kind == 'truncated_container':
        content = _pcm_container()[:-1]
    elif kind == 'unknown_chunk':
        content = _pcm_container(extra=b'fact\x04\0\0\0\x40\x1f\0\0')
    else:
        content = bytearray(_pcm_container())
        struct.pack_into('<I', content, 16, 14)
    path.write_bytes(content)
    metadata.write_text(json.dumps({**recorded, 'version': dubbing_edit.file_version(path)}), encoding='utf-8')
    counts = {'plan': 0, 'fingerprint': 0}
    build, playable = voice_timing._build_plan, dubbing_edit.playable

    def build_again(*args, **kwargs):
        counts['plan'] += 1
        return build(*args, **kwargs)

    def check_again(*args, **kwargs):
        counts['fingerprint'] += 1
        return playable(*args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail('配音头身份和重复列表读取不能启动 ffprobe')

    monkeypatch.setattr(voice_timing, '_build_plan', build_again)
    monkeypatch.setattr(dubbing_edit, 'playable', check_again)
    monkeypatch.setattr(voice_timing, '_video_input', lambda *args: None)
    monkeypatch.setattr(voice_timing, '_probe_video', forbidden)
    monkeypatch.setattr('subprocess.Popen', forbidden)
    for _ in range(2):
        voice_timing.plan(project, tmp_path, [row], deps.cfg, 20, dependencies=deps)
        voice_timing.fingerprint(deps)
    assert counts == {'plan': 2, 'fingerprint': 2}
    assert not deps._voice_timing_cache


def test_known_unchanged_wav_reuses_plan_duration_and_fingerprint_without_ffprobe(tmp_path, monkeypatch):
    project, deps, row, _, _ = _env(tmp_path)
    durations, checks = [], []
    duration, playable = voice_timing.audio_seconds, dubbing_edit.playable

    def measure(path):
        durations.append(path)
        return duration(path)

    def check(*args, **kwargs):
        checks.append(1)
        return playable(*args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail('未变配音的排布缓存不应启动探测进程')

    monkeypatch.setattr(voice_timing, 'audio_seconds', measure)
    monkeypatch.setattr(dubbing_edit, 'playable', check)
    monkeypatch.setattr(voice_timing, '_video_input', lambda *args: None)
    monkeypatch.setattr('subprocess.Popen', forbidden)
    for _ in range(2):
        assert voice_timing.plan(project, tmp_path, [row], deps.cfg, 20, dependencies=deps)['timing']['missing_ids'] == []
        assert voice_timing.fingerprint(deps)['voices'][0]['version'] is not None
    assert len(durations) == len(checks) == 1


@pytest.mark.parametrize('padded', [False, True])
def test_odd_pcm_data_needs_padding_inside_container(tmp_path, padded):
    path = tmp_path / 'odd.wav'
    fmt = struct.pack('<HHIIHH', 1, 1, 8000, 8000, 1, 8)
    body = b'WAVEfmt ' + struct.pack('<I', len(fmt)) + fmt + b'data\x03\0\0\0abc'
    body += b'\0' if padded else b''
    path.write_bytes(b'RIFF' + struct.pack('<I', len(body)) + body)
    assert (voice_timing._audio_read_identity(path) is not None) is padded


@pytest.mark.parametrize('when', ['before_identity', 'before_header'])
def test_missing_wav_and_disappearance_while_reading_have_distinct_cache_identity(tmp_path, monkeypatch, when):
    _env(tmp_path)
    path = dubbing_edit.take_path(tmp_path, 1)
    if when == 'before_identity':
        path.unlink()
        assert voice_timing._audio_read_identity(path) == ('missing',)
    else:
        original = voice_timing._wav_duration_identity

        def disappear(*args):
            path.unlink()
            return original(*args)

        monkeypatch.setattr(voice_timing, '_wav_duration_identity', disappear)
        assert voice_timing._audio_read_identity(path) is None


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
