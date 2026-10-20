"""AAC 反馈纠偏必须有界、可取消，并且只提交已复测通过的候选。"""
import hashlib
import subprocess
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from dubforge.errors import DubForgeError
from dubforge.pipeline import audio_quality as quality


pytestmark = pytest.mark.usefixtures('isolated_env')


def _context(root, mode='uniform'):
    logs = []
    values = {'assemble.final_loudness_mode': mode, 'assemble.final_loudness_lufs': -14.,
              'assemble.final_true_peak_db': -1.5}
    return SimpleNamespace(pdir=root, cfg=lambda key, default=None: values.get(key, default),
        check_cancel=lambda: None, log=logs.append, report_progress=lambda *args: None), logs


def _report(peak, loudness=-14.):
    return dict(schema=1, true_peak_db=peak, integrated_lufs=loudness,
                loudness_range_lu=3., silence_ranges=[], trailing_silence_start=None, issues=[])


def _fake_encoding(monkeypatch, output, peaks, *, on_gain=None, on_measure=None):
    measurements, calls = [], []
    values = iter(peaks)
    def measure(path, **kwargs):
        measurements.append(path)
        report = _report(next(values))
        if on_measure:
            on_measure(len(measurements))
        return report
    def gain(source, target, value, **kwargs):
        calls.append(dict(source=source, target=target, gain=value, original=source.read_bytes()))
        target.write_bytes(f'纠偏候选{len(calls)}'.encode())
        if on_gain:
            on_gain()
    monkeypatch.setattr(quality, 'measure', measure)
    monkeypatch.setattr(quality, '_gain_file', gain)
    output.write_bytes(b'original encoded candidate')
    return calls, measurements


@pytest.mark.parametrize('mode,gain', [('uniform', None), ('normalize', 2.)])
def test_feedback_can_converge_after_single_correction_was_insufficient(tmp_path, monkeypatch, mode, gain):
    ctx, logs = _context(tmp_path, mode)
    output = tmp_path / 'candidate.mp4'
    calls, measurements = _fake_encoding(monkeypatch, output, [-1.2, -1.3, -1.4, -1.6])
    applied = dict(gain_db=gain, peak_limited=False, actual_method='dynamic' if gain is None else 'linear')
    report = quality.finalize(ctx, output, reference=_report(-4.77, -28.15), applied=applied)
    assert [row['gain'] for row in calls] == pytest.approx([-.6, -1.1, -1.5])
    assert all(row['source'] == output and row['original'] == b'original encoded candidate' for row in calls)
    assert output.read_bytes() == '纠偏候选3'.encode()
    assert report['true_peak_db'] == -1.6 and len(measurements) == 4
    assert report['processing']['gain_db'] == (None if gain is None else pytest.approx(.5))
    assert report['processing']['aac_peak_correction_db'] == pytest.approx(-1.5)
    assert applied['gain_db'] == gain  # 不把编码纠偏写回共享的主音轨处理方案。
    diagnostic = report['processing']['aac_peak_verification']
    assert diagnostic['initial_true_peak_db'] == -1.2
    assert [row['true_peak_db'] for row in diagnostic['attempts']] == [-1.3, -1.4, -1.6]
    assert all(any(f'{peak:.2f} dBTP' in line for line in logs) for peak in [-1.2, -1.3, -1.4, -1.6])
    assert not list(tmp_path.glob('.tmp_peak_*'))


def test_exhausted_feedback_never_replaces_original_and_keeps_numeric_diagnostics(tmp_path, monkeypatch):
    ctx, logs = _context(tmp_path)
    output = tmp_path / 'candidate.mp4'
    calls, _ = _fake_encoding(monkeypatch, output, [-.2, -.1, -.1, -.1])
    with pytest.raises(DubForgeError) as caught:
        quality.finalize(ctx, output, reference=_report(-4.), applied=dict(gain_db=None, peak_limited=False))
    assert caught.value.code == 'AUDIO_TRUE_PEAK_EXCEEDED'
    assert output.read_bytes() == b'original encoded candidate'
    assert len(calls) == 3 and len(caught.value.detail['attempts']) == 3
    assert caught.value.detail['initial_true_peak_db'] == -.2
    assert caught.value.detail['target_true_peak_db'] == -1.5
    assert '实测 -0.10 dBTP' in caught.value.message and '3 次' in caught.value.message
    assert all(any(f'纠偏 {i} 复测' in row for row in logs) for i in range(1, 4))
    assert not list(tmp_path.glob('.tmp_peak_*'))


def test_bad_measurement_cannot_cause_unbounded_attenuation(tmp_path, monkeypatch):
    ctx, _ = _context(tmp_path)
    output = tmp_path / 'candidate.mp4'
    calls, _ = _fake_encoding(monkeypatch, output, [100., 100.])
    with pytest.raises(DubForgeError) as caught:
        quality.finalize(ctx, output, reference=_report(-4.), applied=dict(gain_db=0., peak_limited=False))
    assert caught.value.code == 'AUDIO_TRUE_PEAK_EXCEEDED'
    assert len(calls) == 1 and calls[0]['gain'] == -24.
    assert output.read_bytes() == b'original encoded candidate'


@pytest.mark.parametrize('checkpoint', ['before', 'after_gain', 'after_measure', 'before_commit'])
def test_cancellation_at_correction_boundaries_retains_original(tmp_path, monkeypatch, checkpoint):
    ctx, _ = _context(tmp_path)
    output = tmp_path / 'candidate.mp4'
    state = dict(gained=False, measured=False, after_checks=0)
    def measured(number):
        if number == 2:
            state['measured'] = True
    calls, _ = _fake_encoding(monkeypatch, output, [-.5, -2.],
        on_gain=lambda: state.update(gained=True), on_measure=measured)
    def check():
        if state['measured']:
            state['after_checks'] += 1
        stop = (checkpoint == 'before'
            or checkpoint == 'after_gain' and state['gained']
            or checkpoint == 'after_measure' and state['measured']
            or checkpoint == 'before_commit' and state['after_checks'] == 2)
        if stop:
            raise DubForgeError('已取消', code='STEP_CANCELLED')
    ctx.check_cancel = check
    with pytest.raises(DubForgeError) as caught:
        quality.finalize(ctx, output, reference=_report(-4.), applied=dict(gain_db=0., peak_limited=False))
    assert caught.value.code == 'STEP_CANCELLED'
    assert len(calls) == (0 if checkpoint == 'before' else 1)
    assert output.read_bytes() == b'original encoded candidate'
    assert not list(tmp_path.glob('.tmp_peak_*'))


@pytest.mark.parametrize('failure', ['encode', 'measure'])
def test_processing_failure_retains_original_and_removes_partial_candidate(tmp_path, monkeypatch, failure):
    ctx, _ = _context(tmp_path)
    output = tmp_path / 'candidate.mp4'
    def fail():
        raise DubForgeError('模拟处理失败', code='AUDIO_PROCESS_FAILED')
    _fake_encoding(monkeypatch, output, [-.5, -2.], on_gain=fail if failure == 'encode' else None,
                   on_measure=lambda number: fail() if failure == 'measure' and number == 2 else None)
    with pytest.raises(DubForgeError) as caught:
        quality.finalize(ctx, output, reference=_report(-4.), applied=dict(gain_db=0., peak_limited=False))
    assert caught.value.code == 'AUDIO_PROCESS_FAILED'
    assert output.read_bytes() == b'original encoded candidate'
    assert not list(tmp_path.glob('.tmp_peak_*'))


@pytest.mark.parametrize('mode,peak,loudness', [('off', 1., -12.), ('uniform', -2., -14.),
                                              ('normalize', -2., -14.), ('uniform', None, None)])
def test_disabled_safe_or_silent_audio_does_not_encode_again(tmp_path, monkeypatch, mode, peak, loudness):
    ctx, logs = _context(tmp_path, mode)
    output = tmp_path / 'candidate.mp4'
    output.write_bytes(b'original')
    monkeypatch.setattr(quality, 'measure', lambda *args, **kwargs: _report(peak, loudness))
    monkeypatch.setattr(quality, '_gain_file', lambda *args, **kwargs: pytest.fail('无需纠偏不应编码'))
    result = quality.finalize(ctx, output, reference=_report(-4.),
                              applied=dict(gain_db=0., peak_limited=False))
    assert 'aac_peak_correction_db' not in result['processing']
    assert output.read_bytes() == b'original'
    if mode == 'off':
        assert result['target_reached'] is None and '处理已关闭' in logs[0]


def test_feedback_does_not_relax_peak_threshold(tmp_path, monkeypatch):
    ctx, _ = _context(tmp_path)
    output = tmp_path / 'candidate.mp4'
    _fake_encoding(monkeypatch, output, [-1.2, -1.449, -1.449, -1.449])
    with pytest.raises(DubForgeError) as caught:
        quality.finalize(ctx, output, reference=_report(-4.), applied=dict(gain_db=0., peak_limited=False))
    assert caught.value.code == 'AUDIO_TRUE_PEAK_EXCEEDED'
    assert output.read_bytes() == b'original encoded candidate'


def test_lost_peak_measurement_after_correction_is_not_verified_silence(tmp_path, monkeypatch):
    ctx, _ = _context(tmp_path)
    output = tmp_path / 'candidate.mp4'
    calls, _ = _fake_encoding(monkeypatch, output, [-.2, None])
    with pytest.raises(DubForgeError) as caught:
        quality.finalize(ctx, output, reference=_report(-4.), applied=dict(gain_db=None, peak_limited=False))
    assert caught.value.code == 'AUDIO_MEASUREMENT_INVALID'
    assert caught.value.detail['initial_true_peak_db'] == -.2
    assert caught.value.detail['attempts'][0]['true_peak_db'] is None
    assert len(calls) == 1 and output.read_bytes() == b'original encoded candidate'
    assert not list(tmp_path.glob('.tmp_peak_*'))


def test_failed_peak_verification_keeps_formal_video_and_report(tmp_path, monkeypatch):
    from dubforge.pipeline import content_redaction, step_assemble
    source = tmp_path / 'synthetic.mp4'
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-threads', '1', '-f', 'lavfi', '-i',
        'color=c=black:s=64x64:r=10:d=2', '-f', 'lavfi', '-i',
        'sine=frequency=997:sample_rate=48000:duration=2', '-c:v', 'libx264',
        '-threads', '1', '-preset', 'ultrafast', '-c:a', 'aac', str(source)], check=True, timeout=30)
    output = tmp_path / 'final.mp4'
    output.write_bytes(b'previous formal video')
    saved_report = quality.report_path(output)
    saved_report.write_bytes(b'previous bound report')
    ctx, _ = _context(tmp_path, 'normalize')
    def run(command, **kwargs):
        kwargs.pop('on_stdout', None)
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                errors='replace', check=False, timeout=30, **kwargs)
        return result.returncode, result.stdout, result.stderr
    ctx.run_subprocess = run
    monkeypatch.setattr(content_redaction, 'safe_overlay', lambda ctx, plan: plan)
    monkeypatch.setattr(content_redaction, 'audio_plan', lambda *args, **kwargs:
        dict(background=None, filter='', fallback_reason='', ranges=[],
             requested_mode='all_audio', effective_mode='all_audio'))
    monkeypatch.setattr(quality, 'measure', lambda *args, **kwargs: _report(-.1))
    def gain(source, target, *args, **kwargs):
        target.write_bytes(b'nonconvergent candidate')
    monkeypatch.setattr(quality, '_gain_file', gain)
    with pytest.raises(DubForgeError) as caught:
        step_assemble._mux_original_sub(ctx, source, output, 2., (0, 99), burn=False)
    assert caught.value.code == 'AUDIO_TRUE_PEAK_EXCEEDED'
    assert output.read_bytes() == b'previous formal video'
    assert saved_report.read_bytes() == b'previous bound report'
    assert not list(tmp_path.glob('.tmp*'))


@pytest.mark.parametrize('mode', ['uniform', 'normalize'])
def test_real_aac_feedback_meets_ceiling_without_reencoding_video(tmp_path, mode):
    rate = 48000
    timeline = np.arange(rate * 4) / rate
    samples = .7 * np.sin(2 * np.pi * 997 * timeline)
    wav, output = tmp_path / 'synthetic.wav', tmp_path / 'candidate.mp4'
    sf.write(wav, np.column_stack([samples, samples]), rate, subtype='FLOAT')
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-threads', '1', '-f', 'lavfi', '-i',
        'color=c=black:s=64x64:r=10:d=4', '-i', str(wav), '-c:v', 'libx264',
        '-threads', '1', '-preset', 'ultrafast', '-c:a', 'aac', '-b:a', '192k',
        '-af', 'volume=3dB', '-metadata', 'title=合成测试', str(output)], check=True, timeout=30)
    def video_hash():
        return subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(output), '-map', '0:v:0',
            '-c', 'copy', '-f', 'hash', '-hash', 'sha256', '-'], timeout=30)
    initial_hash, initial_video = hashlib.sha256(output.read_bytes()).hexdigest(), video_hash()
    before = quality.measure(output)
    assert before['true_peak_db'] > -1.5
    ctx, logs = _context(tmp_path, mode)
    report = quality.finalize(ctx, output, reference=before, applied=dict(gain_db=None if mode == 'uniform' else 0.,
                                                                       peak_limited=False))
    assert report['true_peak_db'] <= -1.45 and quality.measure(output)['true_peak_db'] == report['true_peak_db']
    assert report['processing']['aac_peak_correction_db'] < 0
    assert report['processing']['aac_peak_verification']['attempts']
    assert hashlib.sha256(output.read_bytes()).hexdigest() != initial_hash
    assert video_hash() == initial_video
    assert '纠偏 1 复测' in '\n'.join(logs)
    assert not list(tmp_path.glob('.tmp_peak_*'))
