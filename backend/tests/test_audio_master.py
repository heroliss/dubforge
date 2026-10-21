"""完整音轨统一响度的真实FFmpeg回归；所有音频均由测试临时生成。"""
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from dubforge.errors import DubForgeError
from dubforge.pipeline import audio_master as master


def _context(tmp_path, values=None):
    commands = []
    def run(command):
        commands.append(command)
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace')
        return result.returncode, result.stdout, result.stderr
    options = {'assemble.final_loudness_mode': 'uniform', **(values or {})}
    return SimpleNamespace(pdir=tmp_path, cfg=lambda key, default=None: options.get(key, default),
                           run_subprocess=run, check_cancel=lambda: None), commands


def _audio(path, *, amplitude=.03, duration=8, transient=False):
    rate = 48000
    timeline = np.arange(round(rate * duration)) / rate
    samples = amplitude * np.sin(2 * np.pi * 440 * timeline)
    if transient:
        # 短瞬态提供高峰低响素材，单纯静态增益不可能同时满足响度和峰值。
        samples[round(rate*duration/2):round(rate*duration/2)+24] = .95
    sf.write(path, samples, rate, subtype='FLOAT')
    return path


def _prepare(ctx, path, *, duration=8, cut_plan=None):
    return master.prepare_master(ctx, ['ffmpeg', '-y', '-hide_banner', '-nostats', '-i', str(path)],
                                 ['[0:a]anull[mix]'], '[mix]', duration=duration, cut_plan=cut_plan)


def test_different_original_levels_reach_one_target_by_linear_gain(tmp_path):
    results = []
    for index, amplitude in enumerate((.02, .1)):
        ctx, _ = _context(tmp_path / str(index))
        source = _audio(tmp_path / f'source{index}.wav', amplitude=amplitude)
        result = _prepare(ctx, source)
        results.append(result)
        assert result['processing']['actual_method'] == 'linear'
        assert result['processing']['requested_method'] == 'uniform'
        assert result['processing']['target_reached']
        assert abs(result['measurement']['integrated_lufs'] + 16) <= .3
        assert result['measurement']['true_peak_db'] <= -1.5
        data, rate = sf.read(result['path'])
        assert rate == 48000 and data.shape == (48000*8, 2)
    assert abs(results[0]['measurement']['integrated_lufs'] - results[1]['measurement']['integrated_lufs']) < .2
    assert results[0]['processing']['gain_db'] > results[1]['processing']['gain_db'] + 10


def test_high_peak_low_loudness_uses_dynamic_master_and_reaches_target(tmp_path):
    source = _audio(tmp_path / 'transient.wav', transient=True, duration=12)
    ctx, _ = _context(tmp_path)
    result = _prepare(ctx, source, duration=12)
    assert result['processing']['actual_method'] == 'dynamic'
    assert result['processing']['gain_db'] is None
    assert result['processing']['nominal_gain_db'] > 10
    assert abs(result['measurement']['integrated_lufs'] + 16) < .7
    assert result['measurement']['true_peak_db'] <= -1.5
    assert result['processing']['target_reached']


def test_silence_remains_silent_without_fake_loudness_or_boost(tmp_path):
    source = _audio(tmp_path / 'silent.wav', amplitude=0)
    ctx, _ = _context(tmp_path)
    result = _prepare(ctx, source)
    assert result['measurement']['integrated_lufs'] is None
    assert result['processing']['silent']
    assert result['processing']['gain_db'] == 0
    assert not result['processing']['target_reached']
    assert not np.any(sf.read(result['path'])[0])


def test_master_cache_reuses_generated_voice_content_not_random_filename(tmp_path):
    first = _audio(tmp_path / '.tmp_voice_first.wav')
    second = tmp_path / '.tmp_voice_second.wav'
    second.write_bytes(first.read_bytes())
    ctx, commands = _context(tmp_path)
    created = _prepare(ctx, first)
    count = len(commands)
    reused = _prepare(ctx, second)
    assert reused['cache_hit'] and created['cache_key'] == reused['cache_key']
    assert len(commands) == count
    _audio(second, amplitude=.09)
    changed = _prepare(ctx, second)
    assert not changed['cache_hit'] and changed['cache_key'] != reused['cache_key']


def test_cache_invalidates_target_cut_graph_and_corrupt_cached_file(tmp_path):
    source = _audio(tmp_path / 'source.wav')
    ctx, _ = _context(tmp_path)
    initial = _prepare(ctx, source)
    target_ctx, _ = _context(tmp_path, {'assemble.final_loudness_lufs': -18})
    target = _prepare(target_ctx, source)
    assert initial['cache_key'] != target['cache_key']
    cut = dict(source_duration=8., ranges=[dict(start=2., end=4.)])
    edited = _prepare(ctx, source, cut_plan=cut)
    assert edited['duration'] == 6 and edited['cache_key'] != initial['cache_key']
    assert len(sf.read(edited['path'])[0]) == 48000 * 6
    altered = master.prepare_master(ctx, ['ffmpeg', '-y', '-i', str(source)],
        ['[0:a]volume=0.8[mix]'], '[mix]', duration=8)
    assert altered['cache_key'] != initial['cache_key']
    initial['path'].write_bytes(b'incomplete file')
    repaired = _prepare(ctx, source)
    assert not repaired['cache_hit'] and repaired['cache_key'] == initial['cache_key']
    assert sf.info(repaired['path']).frames == 48000*8


def test_preview_is_an_exact_slice_of_the_completed_master(tmp_path):
    source = _audio(tmp_path / 'peaky.wav', transient=True, duration=12)
    ctx, _ = _context(tmp_path)
    result = _prepare(ctx, source, duration=12)
    preview = tmp_path / 'preview.flac'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', '2', '-t', '3', '-i', str(result['path']),
        '-vn', '-c:a', 'flac', '-sample_fmt', 's32', str(preview)], check=True)
    complete, rate = sf.read(result['path'])
    actual, rate2 = sf.read(preview)
    assert rate == rate2 == 48000
    np.testing.assert_array_equal(actual, complete[2*rate:5*rate])


def test_concurrent_same_key_only_processes_once(tmp_path):
    source = _audio(tmp_path / 'source.wav')
    ctx, commands = _context(tmp_path)
    barrier = threading.Barrier(2)
    def prepare():
        barrier.wait()
        return _prepare(ctx, source)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(prepare) for _ in range(2)]
        results = [future.result(timeout=30) for future in futures]
    assert {r['cache_hit'] for r in results} == {False, True}
    assert len(commands) == 3  # 两遍处理、一次实际FLAC测量；另一调用只读验证缓存。
    assert results[0]['path'] == results[1]['path']


def _native_cache_lock(handle, *, unlock=False):
    """用真实系统锁持有测试文件，不依赖待验证的缓存锁实现。"""
    handle.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def _held_cache_lock(path, owner):
    if owner == 'thread':
        with path.open('r+b', buffering=0) as handle:
            _native_cache_lock(handle)
            try:
                yield
            finally:
                _native_cache_lock(handle, unlock=True)
        return

    # 独立进程只操作本测试的临时锁文件；就绪后等待父进程发出释放指令。
    ready = path.with_suffix('.ready')
    code = '''
import os, sys
from pathlib import Path
with Path(sys.argv[1]).open('r+b', buffering=0) as handle:
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    Path(sys.argv[2]).write_text('locked', encoding='ascii')
    sys.stdin.readline()
    handle.seek(0)
    if os.name == 'nt':
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle, fcntl.LOCK_UN)
'''
    process = subprocess.Popen([sys.executable, '-c', code, str(path), str(ready)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding='utf-8', errors='replace')
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            if process.poll() is not None:
                raise AssertionError('测试持锁进程提前退出：' + process.stderr.read())
            assert time.monotonic() < deadline, '测试持锁进程未按时就绪'
            time.sleep(.02)
        yield
    finally:
        try:
            _, error = process.communicate('release\n', timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise AssertionError('测试持锁进程未按时退出') from None
        assert process.returncode == 0, error


@pytest.mark.parametrize('initial', [b'', b'0'], ids=['empty', 'legacy-marker'])
@pytest.mark.parametrize('owner', ['thread', 'process'])
@pytest.mark.parametrize('cancel_wait', [False, True], ids=['wait-release', 'cancel-wait'])
def test_cache_lock_waits_without_writing_and_cancellation_is_safe(tmp_path, initial, owner, cancel_wait):
    path = tmp_path / 'master.lock'
    path.write_bytes(initial)
    waiting, entered, cancel = threading.Event(), threading.Event(), threading.Event()
    checks = 0

    def check_cancel():
        nonlocal checks
        checks += 1
        if checks >= 2:
            waiting.set()

    ctx = SimpleNamespace(check_cancel=check_cancel, _audio_cancel=cancel)

    def acquire():
        with master._cache_lock(path, ctx):
            entered.set()
        return True

    with ThreadPoolExecutor(max_workers=1) as pool:
        with _held_cache_lock(path, owner):
            future = pool.submit(acquire)
            deadline = time.monotonic() + 10
            while not waiting.wait(.02):
                if future.done():
                    future.result()  # 原实现会在预写空锁文件时直接抛出 PermissionError。
                    pytest.fail('持锁者尚未释放，等待者却已进入临界区')
                if time.monotonic() >= deadline:
                    cancel.set()
                    pytest.fail('等待者未进入可取消的加锁重试')
            assert not entered.is_set()
            if cancel_wait:
                cancel.set()
                with pytest.raises(DubForgeError) as caught:
                    future.result(timeout=5)
                assert caught.value.code == 'AUDIO_MASTER_CANCELLED'
                assert not entered.is_set()
        if not cancel_wait:
            assert future.result(timeout=5)
            assert entered.is_set()

    assert path.read_bytes() == initial
    cancel.clear()
    with master._cache_lock(path, ctx):
        pass  # 正常结束和取消等待后，后续调用都能继续获取同一把锁。


@pytest.mark.parametrize('initial', [b'', b'0'], ids=['empty', 'legacy-marker'])
def test_cache_lock_releases_after_body_failure(tmp_path, initial):
    path = tmp_path / 'master.lock'
    path.write_bytes(initial)
    ctx = SimpleNamespace(check_cancel=lambda: None)
    with pytest.raises(RuntimeError, match='模拟处理失败'):
        with master._cache_lock(path, ctx):
            raise RuntimeError('模拟处理失败')
    with _held_cache_lock(path, 'thread'):
        pass  # 独立文件句柄能立即取得系统锁，证明异常退出没有遗留占用。
    with master._cache_lock(path, ctx):
        pass
    assert path.read_bytes() == initial


def test_cancel_and_invalid_inputs_do_not_publish_cache(tmp_path):
    source = _audio(tmp_path / 'source.wav')
    ctx, _ = _context(tmp_path)
    cancel = threading.Event()
    cancel.set()
    ctx._audio_cancel = cancel
    with pytest.raises(DubForgeError) as caught:
        _prepare(ctx, source)
    assert caught.value.code == 'AUDIO_MASTER_CANCELLED'
    assert not list(tmp_path.rglob('*.flac'))
    ctx._audio_cancel = None
    for duration in (0, float('nan'), -1, True):
        with pytest.raises(DubForgeError) as caught:
            _prepare(ctx, source, duration=duration)
        assert caught.value.code == 'AUDIO_MASTER_INVALID'


def test_changed_source_during_processing_never_commits_old_fingerprint(tmp_path):
    source = _audio(tmp_path / 'source.wav')
    ctx, _ = _context(tmp_path)
    original = ctx.run_subprocess
    count = 0
    def mutate(command):
        nonlocal count
        result = original(command)
        count += 1
        if count == 2:
            _audio(source, amplitude=.2)
        return result
    ctx.run_subprocess = mutate
    with pytest.raises(DubForgeError) as caught:
        _prepare(ctx, source)
    assert caught.value.code == 'AUDIO_MASTER_INPUT_CHANGED'
    assert not list((tmp_path / 'audio' / '.master-cache').glob('*.flac'))
    assert not list((tmp_path / 'audio' / '.master-cache').glob('*.json'))


def test_reported_linear_gain_is_the_actual_fixed_signal_gain(tmp_path):
    rate = 48000
    timeline = np.arange(rate*8) / rate
    samples = np.column_stack([.04 * np.sin(2*np.pi*440*timeline)] * 2)
    source = tmp_path / 'stereo.wav'
    sf.write(source, samples, rate, subtype='FLOAT')
    ctx, _ = _context(tmp_path)
    result = _prepare(ctx, source)
    output, _ = sf.read(result['path'])
    measured_gain = 20*np.log10(np.sqrt(np.mean(output**2)) / np.sqrt(np.mean(samples**2)))
    assert result['processing']['actual_method'] == 'linear'
    assert measured_gain == pytest.approx(result['processing']['gain_db'], abs=.002)


def test_short_input_is_rejected_before_publishing_wrong_duration(tmp_path):
    source = _audio(tmp_path / 'short.wav', duration=2)
    ctx, _ = _context(tmp_path)
    with pytest.raises(DubForgeError) as caught:
        _prepare(ctx, source, duration=8)
    assert caught.value.code == 'AUDIO_MASTER_OUTPUT_INVALID'
    assert not list((tmp_path / 'audio' / '.master-cache').glob('*.flac'))


def test_failure_in_second_pass_cleans_pending_file_and_can_resume(tmp_path):
    source = _audio(tmp_path / 'source.wav')
    ctx, _ = _context(tmp_path)
    original = ctx.run_subprocess
    calls = 0
    def fail_once(command):
        nonlocal calls
        calls += 1
        if calls == 2:
            return 1, '', '模拟编码失败'
        return original(command)
    ctx.run_subprocess = fail_once
    with pytest.raises(DubForgeError) as caught:
        _prepare(ctx, source)
    assert caught.value.code == 'AUDIO_MASTER_PROCESS_FAILED'
    root = tmp_path / 'audio' / '.master-cache'
    assert not list(root.glob('*.flac')) and not list(root.glob('*.json'))
    ctx.run_subprocess = original
    assert not _prepare(ctx, source)['cache_hit']


def test_float_mix_above_full_scale_is_normalized_before_lossless_encoding(tmp_path):
    source = _audio(tmp_path / 'source.wav', amplitude=.2)
    ctx, _ = _context(tmp_path)
    result = master.prepare_master(ctx, ['ffmpeg', '-y', '-i', str(source)],
        ['[0:a]volume=20[mix]'], '[mix]', duration=8)
    assert result['reference']['true_peak_db'] > 6
    assert result['processing']['gain_db'] < 0
    assert abs(result['measurement']['integrated_lufs'] + 16) < .3
    assert result['measurement']['true_peak_db'] <= -1.5
