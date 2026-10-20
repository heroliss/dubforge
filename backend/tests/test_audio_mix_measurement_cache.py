"""真实 FFmpeg 验证完整混音测量只在输入身份变化时重新扫描。"""
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from dubforge.errors import DubForgeError
from dubforge.pipeline import audio_quality as quality


pytestmark = pytest.mark.usefixtures('isolated_env')


def _audio(path, amplitude=.04):
    timeline = np.arange(48000 * 4) / 48000
    sf.write(path, amplitude * np.sin(2 * np.pi * 440 * timeline), 48000, subtype='FLOAT')
    return path


def _context(root):
    commands = []
    values = {'assemble.final_loudness_mode': 'normalize'}
    def run(command):
        commands.append(command)
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace')
        return result.returncode, result.stdout, result.stderr
    ctx = SimpleNamespace(pdir=root, cfg=lambda key, default=None: values.get(key, default),
                          run_subprocess=run, check_cancel=lambda: None)
    return ctx, commands, values


def _measure(ctx, source, *, filters=None, duration=4., cut_plan=None, input_args=None):
    return quality.measure_mix(ctx,
        ['ffmpeg', '-hide_banner', '-nostats', *(input_args or []), '-i', str(source)],
        filters or ['[0:a:0]anull[mix]'], '[mix]', duration=duration, cut_plan=cut_plan)


def test_repeated_previews_and_changed_target_reuse_unprocessed_measurement(tmp_path):
    source = _audio(tmp_path / 'source.wav')
    ctx, commands, values = _context(tmp_path / 'project')
    first = _measure(ctx, source)
    old_gain = quality.final_gain(ctx, first)
    values['assemble.final_loudness_lufs'] = -14.
    second = _measure(ctx, source)
    third = _measure(ctx, source)
    assert len(commands) == 1  # 三次完整测量请求只有首次扫描，目标变化不丢弃原混音测量。
    assert first == second == third
    assert quality.final_gain(ctx, second)['requested_gain_db'] == pytest.approx(old_gain['requested_gain_db'] + 2)
    assert first['integrated_lufs'] is not None


def test_generated_voice_uses_content_identity_and_changed_content_invalidates(tmp_path):
    first = _audio(tmp_path / '.tmp_voice_first.wav')
    second = tmp_path / '.tmp_voice_second.wav'
    second.write_bytes(first.read_bytes())
    ctx, commands, _ = _context(tmp_path / 'project')
    assert _measure(ctx, first) == _measure(ctx, second)
    assert len(commands) == 1
    _audio(second, amplitude=.1)
    assert _measure(ctx, second)['integrated_lufs'] > _measure(ctx, first)['integrated_lufs'] + 7
    assert len(commands) == 2


def test_filter_channels_input_range_cuts_duration_and_algorithm_bind_cache(tmp_path, monkeypatch):
    source = _audio(tmp_path / 'source.wav')
    ctx, commands, _ = _context(tmp_path / 'project')
    _measure(ctx, source)
    quiet = _measure(ctx, source, filters=['[0:a:0]volume=0.5[mix]'])
    assert quiet['integrated_lufs'] < -30
    _measure(ctx, source, filters=['[0:a:0]aformat=channel_layouts=stereo[mix]'])
    _measure(ctx, source, input_args=['-ss', '1', '-t', '2'])
    _measure(ctx, source, cut_plan={'source_duration': 4., 'ranges': [{'start': 1., 'end': 2.}]})
    _measure(ctx, source, duration=3.)
    monkeypatch.setattr(quality, 'MIX_MEASURE_ALGORITHM', quality.MIX_MEASURE_ALGORITHM + '-new')
    _measure(ctx, source)
    assert len(commands) == 7


def test_changed_source_and_damaged_receipt_are_remeasured(tmp_path):
    source = _audio(tmp_path / 'source.wav')
    ctx, commands, _ = _context(tmp_path / 'project')
    _measure(ctx, source)
    receipt, = (ctx.pdir / 'audio' / '.mix-measurements').glob('*.json')
    doc = json.loads(receipt.read_text(encoding='utf-8'))
    doc['measurement'].pop('true_peak_db')
    receipt.write_text(json.dumps(doc), encoding='utf-8')
    _measure(ctx, source)
    assert len(commands) == 2
    _audio(source, amplitude=.08)
    _measure(ctx, source)
    assert len(commands) == 3


@pytest.mark.parametrize('field,value', [
    ('silence_ranges', [{'start': 0., 'end': float('nan')}]),
    ('trailing_silence_start', {'bad': 'value'}),
    ('issues', ['bad issue']),
])
def test_malformed_auxiliary_measurement_is_not_reused(tmp_path, field, value):
    source = _audio(tmp_path / 'source.wav')
    ctx, commands, _ = _context(tmp_path / 'project')
    _measure(ctx, source)
    receipt, = (ctx.pdir / 'audio' / '.mix-measurements').glob('*.json')
    doc = json.loads(receipt.read_text(encoding='utf-8'))
    doc['measurement'][field] = value
    receipt.write_text(json.dumps(doc), encoding='utf-8')
    assert _measure(ctx, source)['integrated_lufs'] is not None
    assert len(commands) == 2


def test_successful_silent_measurement_is_reused_without_fabricating_loudness(tmp_path):
    source = _audio(tmp_path / 'silent.wav', amplitude=0.)
    ctx, commands, _ = _context(tmp_path / 'project')
    first = _measure(ctx, source)
    assert first == _measure(ctx, source)
    assert first['integrated_lufs'] is None and first['true_peak_db'] is None
    assert len(commands) == 1


def test_concurrent_same_mix_is_scanned_once(tmp_path):
    source = _audio(tmp_path / 'source.wav')
    ctx, commands, _ = _context(tmp_path / 'project')
    barrier = threading.Barrier(2)
    def run():
        barrier.wait()
        return _measure(ctx, source)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run) for _ in range(2)]
        reports = [future.result(timeout=30) for future in futures]
    assert reports[0] == reports[1] and len(commands) == 1


@pytest.mark.parametrize('failure', ['process', 'cancel', 'source_changed', 'invalid_report'])
def test_failed_cancelled_or_changed_scan_never_becomes_cache(tmp_path, failure):
    source = _audio(tmp_path / 'source.wav')
    ctx, commands, _ = _context(tmp_path / 'project')
    original = ctx.run_subprocess
    marker = threading.Event()
    ctx._audio_cancel = marker
    def run(command):
        if failure == 'process':
            return 1, '', 'test failure'
        if failure == 'invalid_report':
            return 0, '', 'missing measurement'
        result = original(command)
        if failure == 'cancel':
            marker.set()
        if failure == 'source_changed':
            _audio(source, amplitude=.09)
        return result
    ctx.run_subprocess = run
    with pytest.raises(DubForgeError):
        _measure(ctx, source)
    assert not list((ctx.pdir / 'audio' / '.mix-measurements').glob('*.json'))
    marker.clear()
    ctx.run_subprocess = original
    assert _measure(ctx, source)['integrated_lufs'] is not None


def test_cached_mix_still_obeys_cancellation(tmp_path):
    source = _audio(tmp_path / 'source.wav')
    ctx, commands, _ = _context(tmp_path / 'project')
    _measure(ctx, source)
    marker = threading.Event()
    marker.set()
    ctx._audio_cancel = marker
    with pytest.raises(DubForgeError, match='已取消'):
        _measure(ctx, source)
    assert len(commands) == 1


def test_unwritable_optional_cache_keeps_valid_measurement(tmp_path, monkeypatch):
    source = _audio(tmp_path / 'source.wav')
    ctx, commands, _ = _context(tmp_path / 'project')
    def fail(*args):
        raise PermissionError('cache unavailable')
    monkeypatch.setattr(quality, 'atomic_write_bytes', fail)
    assert _measure(ctx, source)['integrated_lufs'] is not None
    assert len(commands) == 1


def test_actual_output_measurement_stays_uncached_and_project_cache_is_local(tmp_path):
    source = _audio(tmp_path / 'source.wav')
    first, commands, _ = _context(tmp_path / 'first')
    second, second_commands, _ = _context(tmp_path / 'second')
    _measure(first, source)
    _measure(second, source)
    quality.measure(source, ctx=first)
    quality.measure(source, ctx=first)
    assert len(commands) == 3 and len(second_commands) == 1
