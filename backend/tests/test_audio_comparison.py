"""原片声音对照按源保留区间测量，失败不影响有效成品。"""
import json
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from dubforge.errors import DubForgeError
from dubforge.pipeline import audio_comparison as comparison, audio_quality


pytestmark = pytest.mark.usefixtures('isolated_env')


def _report(**values):
    return dict(integrated_lufs=-16., true_peak_db=-1.8, loudness_range_lu=3.5,
        settings=dict(mode='normalize', target_lufs=-16., true_peak_db=-1.5),
        processing=dict(gain_db=2., peak_limited=False), reference=dict(scope='full_output_mix', integrated_lufs=-18.),
        preview=False, has_audio=True, **values)


def _context(tmp_path):
    return SimpleNamespace(pdir=tmp_path / 'project', log=lambda message: None,
        report_progress=lambda progress: None, check_cancel=lambda: None)


@pytest.fixture()
def source_measurement(tmp_path, monkeypatch):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'original')
    calls = dict(probes=0, measurements=0, graph=None)
    def probe(command, ctx):
        calls['probes'] += 1
        return json.dumps(dict(streams=[dict(index=1)])), ''
    def measure(ctx, command, graph, label):
        calls.update(measurements=calls['measurements'] + 1, graph=';'.join(graph), label=label)
        return dict(integrated_lufs=-20., true_peak_db=-3., loudness_range_lu=5.)
    monkeypatch.setattr(audio_quality, '_run', probe)
    monkeypatch.setattr(audio_quality, 'measure_mix', measure)
    return source, calls


def test_comparison_same_segments_and_immutable_reference(tmp_path, source_measurement):
    source, calls = source_measurement
    report = _report()
    cuts = dict(source_duration=100., output_duration=90., ranges=[dict(start=20., end=30.)])
    result = comparison.augment_report(_context(tmp_path), report, source, duration=100., cut_plan=cuts)
    assert result['scope'] == dict(kind='full', source_start=0., source_end=100., output_duration=90.,
        source_segments=[dict(start=0., end=20., output_start=0., output_end=20.),
                         dict(start=30., end=100., output_start=20., output_end=90.)])
    assert 'atrim=start=0.000000:end=20.000000' in calls['graph']
    assert 'atrim=start=30.000000:end=100.000000' in calls['graph']
    assert 'afade=t=out' in calls['graph'] and 'afade=t=in' in calls['graph']
    assert 'volume=' not in calls['graph']
    assert result['source_comparison']['measurement']['integrated_lufs'] == -20.
    assert result['comparison'] == dict(available=True, integrated_delta_lu=4., true_peak_delta_db=1.2,
        loudness_range_delta_lu=-1.5, lra_comparable=True)
    assert result['assessment']['target_status'] == 'reached'
    assert result['assessment']['lra_reliable']
    assert result['reference'] == report['reference']
    assert 'source_comparison' not in report
    assert source.read_bytes() == b'original'


def test_preview_uses_exact_source_window_without_invented_edge_fade(tmp_path, source_measurement):
    source, calls = source_measurement
    report = _report()
    report['integrated_lufs'] = -29.
    report['target_reached'] = False
    result = comparison.augment_report(_context(tmp_path), report, source, start=15., duration=5., full_duration=100.)
    assert result['scope']['kind'] == 'preview'
    assert result['scope']['source_start'] == 15. and result['scope']['source_end'] == 20.
    assert 'atrim=start=15.000000:end=20.000000' in calls['graph']
    assert 'afade' not in calls['graph']
    assert result['assessment']['target_status'] == 'preview'
    assert result['assessment']['target_delta_lu'] is None
    assert not result['assessment']['lra_reliable']
    assert not result['comparison']['lra_comparable']
    assert result['comparison']['loudness_range_delta_lu'] is None
    assert result['target_reached'] is None


def test_explicit_segments_can_represent_prior_cut_review_output(tmp_path, source_measurement):
    source, calls = source_measurement
    segments = [dict(start=0., end=10.), dict(start=25., end=60.)]
    result = comparison.augment_report(_context(tmp_path), _report(), source, duration=45., source_segments=segments)
    assert result['scope']['output_duration'] == 45.
    assert result['scope']['source_end'] == 60.
    assert result['scope']['source_segments'][-1]['output_start'] == 10.
    assert 'atrim=start=25.000000:end=60.000000' in calls['graph']


def test_cache_reuses_only_same_original_identity_range_and_algorithm(tmp_path, source_measurement, monkeypatch):
    source, calls = source_measurement
    ctx = _context(tmp_path)
    first = comparison.augment_report(ctx, _report(), source, duration=90.)
    assert first['source_comparison']['cache_hit'] is False
    other_mix = _report()
    other_mix['integrated_lufs'] = -14.
    repeated = comparison.augment_report(ctx, other_mix, source, duration=90.)
    assert repeated['source_comparison']['cache_hit'] is True
    assert repeated['comparison']['integrated_delta_lu'] == 6.
    assert calls['measurements'] == calls['probes'] == 1
    comparison.augment_report(ctx, _report(), source, start=5., duration=85.)
    assert calls['measurements'] == 2
    source.write_bytes(b'new original content')
    comparison.augment_report(ctx, _report(), source, duration=90.)
    assert calls['measurements'] == 3
    monkeypatch.setattr(comparison, 'ALGORITHM', comparison.ALGORITHM + '-changed')
    comparison.augment_report(ctx, _report(), source, duration=90.)
    assert calls['measurements'] == 4
    assert not list(ctx.pdir.rglob('*.tmp'))


def test_corrupt_cache_remeasures(tmp_path, source_measurement):
    source, calls = source_measurement
    ctx = _context(tmp_path)
    comparison.augment_report(ctx, _report(), source, duration=90.)
    cache = next((ctx.pdir / 'audio' / '.source_measurements').glob('*.json'))
    cache.write_text('broken', encoding='utf-8')
    result = comparison.augment_report(ctx, _report(), source, duration=90.)
    assert calls['measurements'] == 2
    assert not result['source_comparison']['cache_hit']


def test_structurally_incomplete_cache_remeasures(tmp_path, source_measurement):
    source, calls = source_measurement
    ctx = _context(tmp_path)
    comparison.augment_report(ctx, _report(), source, duration=90.)
    cache = next((ctx.pdir / 'audio' / '.source_measurements').glob('*.json'))
    doc = json.loads(cache.read_text(encoding='utf-8'))
    doc['result']['measurement'].pop('integrated_lufs')
    cache.write_text(json.dumps(doc), encoding='utf-8')
    result = comparison.augment_report(ctx, _report(), source, duration=90.)
    assert calls['measurements'] == 2
    assert result['source_comparison']['measurement']['integrated_lufs'] == -20.


def test_out_of_source_range_is_not_presented_as_valid_comparison(tmp_path, source_measurement):
    source, calls = source_measurement
    result = comparison.augment_report(_context(tmp_path), _report(), source, duration=90., full_duration=80.)
    assert result['source_comparison']['status'] == 'failed'
    assert result['source_comparison']['error_code'] == 'AUDIO_COMPARISON_RANGE_INVALID'
    assert calls['measurements'] == calls['probes'] == 0


def test_cached_read_still_checks_review_cancellation(tmp_path, source_measurement):
    import threading
    source, _ = source_measurement
    ctx = _context(tmp_path)
    comparison.augment_report(ctx, _report(), source, duration=90.)
    ctx._audio_cancel = threading.Event()
    ctx._audio_cancel.set()
    with pytest.raises(DubForgeError) as error:
        comparison.augment_report(ctx, _report(), source, duration=90.)
    assert error.value.code == 'REVIEW_RENDER_CANCELLED'


@pytest.mark.parametrize('case', ['no_audio', 'silent'])
def test_absent_or_silent_source_is_explicit_and_cacheable(tmp_path, source_measurement, monkeypatch, case):
    source, calls = source_measurement
    if case == 'no_audio':
        monkeypatch.setattr(audio_quality, '_run', lambda command, ctx: ('{"streams": []}', ''))
    else:
        monkeypatch.setattr(audio_quality, 'measure_mix', lambda *args: dict.fromkeys(comparison.METRICS))
    result = comparison.augment_report(_context(tmp_path), _report(), source, duration=90.)
    assert result['source_comparison']['status'] == 'unavailable'
    assert result['source_comparison']['reason_code'] == case
    assert not result['comparison']['available']
    repeated = comparison.augment_report(_context(tmp_path), _report(), source, duration=90.)
    assert repeated['source_comparison']['cache_hit']
    assert repeated['assessment']['target_status'] == 'reached'
    assert case != 'no_audio' or calls['measurements'] == 0


def test_measurement_failure_preserves_output_and_can_be_retried(tmp_path, source_measurement, monkeypatch):
    source, calls = source_measurement
    def fail(*args):
        raise DubForgeError('测量失败', code='AUDIO_PROCESS_FAILED')
    with monkeypatch.context() as patch:
        patch.setattr(audio_quality, 'measure_mix', fail)
        result = comparison.augment_report(_context(tmp_path), _report(), source, duration=90.)
    assert result['source_comparison']['status'] == 'failed'
    assert result['source_comparison']['error_code'] == 'AUDIO_PROCESS_FAILED'
    assert result['integrated_lufs'] == -16.
    assert result['assessment']['target_status'] == 'reached'
    retried = comparison.augment_report(_context(tmp_path), _report(), source, duration=90.)
    assert retried['source_comparison']['status'] == 'ready'
    assert not retried['source_comparison']['cache_hit']
    assert calls['measurements'] == 1


@pytest.mark.parametrize('code', ['STEP_CANCELLED', 'STEP_TIMEOUT', 'STEP_INTERRUPTED', 'REVIEW_RENDER_CANCELLED'])
def test_cancel_and_shutdown_are_never_swallowed(tmp_path, source_measurement, monkeypatch, code):
    source, _ = source_measurement
    def cancel(*args):
        raise DubForgeError('已停止', code=code)
    monkeypatch.setattr(audio_quality, 'measure_mix', cancel)
    with pytest.raises(DubForgeError, match='已停止') as error:
        comparison.augment_report(_context(tmp_path), _report(), source, duration=90.)
    assert error.value.code == code


def test_changed_source_during_scan_is_not_cached(tmp_path, source_measurement, monkeypatch):
    source, _ = source_measurement
    def change(*args):
        source.write_bytes(b'replaced while measuring')
        return dict(integrated_lufs=-20., true_peak_db=-3., loudness_range_lu=5.)
    monkeypatch.setattr(audio_quality, 'measure_mix', change)
    result = comparison.augment_report(_context(tmp_path), _report(), source, duration=90.)
    assert result['source_comparison']['status'] == 'failed'
    assert result['source_comparison']['error_code'] == 'AUDIO_COMPARISON_SOURCE_CHANGED'
    assert not list((_context(tmp_path).pdir / 'audio').rglob('*.json'))


@pytest.mark.parametrize('kind', ['off', 'peak_limited', 'below', 'above', 'unmeasurable'])
def test_target_assessment_describes_actual_output_not_source(tmp_path, source_measurement, kind):
    source, _ = source_measurement
    report = _report()
    if kind == 'off':
        report['settings']['mode'] = 'off'
    elif kind == 'unmeasurable':
        report['integrated_lufs'] = None
    else:
        report['integrated_lufs'] = -12. if kind == 'above' else -25.
        report['processing']['peak_limited'] = kind == 'peak_limited'
    result = comparison.augment_report(_context(tmp_path), report, source, duration=90.)
    assert result['assessment']['target_status'] == kind


def test_real_ffmpeg_measures_quiet_source_preview_and_output_delta(tmp_path):
    rate = 32000
    t = np.arange(rate * 8) / rate
    samples = np.sin(2 * np.pi * 440 * t) * np.where(t < 4, .4, .02)
    source, output = tmp_path / 'original.wav', tmp_path / 'preview.wav'
    sf.write(source, samples, rate, subtype='FLOAT')
    sf.write(output, samples[rate * 4:] * 2, rate, subtype='FLOAT')
    report = {**_report(), **audio_quality.measure(output), 'preview': True}
    result = comparison.augment_report(_context(tmp_path), report, source, start=4., duration=4., full_duration=8.)
    assert result['source_comparison']['status'] == 'ready'
    assert result['source_comparison']['measurement']['integrated_lufs'] < audio_quality.measure(source)['integrated_lufs'] - 15
    assert abs(result['comparison']['integrated_delta_lu'] - 6.02) < .2
    assert abs(result['comparison']['true_peak_delta_db'] - 6.02) < .2
    assert result['assessment']['target_status'] == 'preview'
    assert not result['comparison']['lra_comparable']


def test_real_ffmpeg_source_cut_discards_loud_removed_interval(tmp_path):
    rate = 32000
    t = np.arange(rate * 12) / rate
    samples = np.sin(2 * np.pi * 440 * t) * np.where((t >= 4) & (t < 8), .5, .02)
    source = tmp_path / 'original.wav'
    sf.write(source, samples, rate, subtype='FLOAT')
    cuts = dict(source_duration=12., output_duration=8., ranges=[dict(start=4., end=8.)])
    result = comparison.augment_report(_context(tmp_path), _report(), source, duration=12., cut_plan=cuts)
    assert result['source_comparison']['status'] == 'ready'
    assert result['source_comparison']['measurement']['integrated_lufs'] < audio_quality.measure(source)['integrated_lufs'] - 15
    assert result['scope']['output_duration'] == 8.
