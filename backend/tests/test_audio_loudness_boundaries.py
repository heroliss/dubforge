"""真峰值边界及两种响度模式的真实 FFmpeg 回归；不读取用户媒体。"""
import subprocess
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from dubforge.errors import DubForgeError
from dubforge.pipeline import assembly_versions, audio_master, audio_quality
from dubforge.pipeline.step_params import STEP_PARAM_SCHEMA, validate_numeric_updates


PEAK_KEY = 'assemble.final_true_peak_db'


@pytest.mark.parametrize('ceiling', [-6., -.5, 0.])
def test_shared_schema_version_and_runtime_accept_supported_peak_range(ceiling):
    field = next(f for f in STEP_PARAM_SCHEMA['assemble'] if f.path == PEAK_KEY)
    assert (field.min, field.max) == (-6, 0)
    validate_numeric_updates({PEAK_KEY: ceiling})
    version = assembly_versions.validate(dict(id='peak', name='峰值边界', dubbing=False,
        burn_subtitles=False, include_comments=False, overrides={PEAK_KEY: ceiling}))
    assert version['overrides'][PEAK_KEY] == ceiling
    assert audio_quality.settings(lambda key, default: ceiling if key == PEAK_KEY else default)['true_peak_db'] == ceiling


@pytest.mark.parametrize('ceiling', [.01, -6.01, True, float('nan'), float('inf')])
def test_invalid_peak_is_rejected_consistently_before_render(tmp_path, ceiling):
    with pytest.raises(DubForgeError):
        validate_numeric_updates({PEAK_KEY: ceiling})
    with pytest.raises(DubForgeError):
        assembly_versions.validate(dict(id='peak', name='峰值边界', dubbing=False,
            burn_subtitles=False, include_comments=False, overrides={PEAK_KEY: ceiling}))
    ctx, commands = _context(tmp_path, 'uniform', ceiling)
    with pytest.raises(DubForgeError) as caught:
        audio_quality.settings(ctx.cfg)
    assert caught.value.code == 'AUDIO_LOUDNESS_INVALID'
    with pytest.raises(DubForgeError) as caught:
        audio_master.prepare_master(ctx, ['ffmpeg'], [], '[mix]', duration=1)
    assert caught.value.code == 'AUDIO_MASTER_INVALID'
    assert commands == [] and not (tmp_path / 'audio').exists()


def test_defaults_remain_conservative():
    from dubforge.config import DEFAULTS
    options = audio_quality.settings(lambda key, default: default)
    assert options['target_lufs'] == DEFAULTS['assemble']['final_loudness_lufs'] == -16.
    assert options['true_peak_db'] == DEFAULTS['assemble']['final_true_peak_db'] == -1.5


def test_static_gain_explains_peak_constraint_and_still_attenuates_lower_target():
    reference = dict(integrated_lufs=-29., true_peak_db=-1.1)
    limited = audio_quality.static_gain(reference, -10., -1.)
    assert limited['requested_gain_db'] == 19.
    assert limited['available_gain_db'] == -.2
    assert limited['gain_db'] == -.2 and limited['peak_limited']
    assert limited['headroom_db'] == .3
    reduced = audio_quality.static_gain(reference, -30., -1.)
    assert reduced['gain_db'] == -1. and not reduced['peak_limited']
    assert reduced['requested_gain_db'] == -1.
    silent = audio_quality.static_gain(dict(integrated_lufs=None, true_peak_db=None), -16., 0.)
    assert silent['gain_db'] == 0. and silent['requested_gain_db'] is None


def _context(root, mode, ceiling, target=-16.):
    values = {'assemble.final_loudness_mode': mode, PEAK_KEY: ceiling,
              'assemble.final_loudness_lufs': target}
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        kwargs.pop('on_stdout', None)
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                errors='replace', check=False, timeout=30, **kwargs)
        return result.returncode, result.stdout, result.stderr
    return SimpleNamespace(pdir=root, cfg=lambda key, default=None: values.get(key, default),
        upstream=lambda *args: None, run_subprocess=run, report_progress=lambda *args: None,
        check_cancel=lambda: None, log=lambda *args: None, task_data={}), commands


def _video(root, *, peaky):
    rate, duration = 48000, 12
    time = np.arange(rate * duration) / rate
    samples = (.03 if peaky else .3) * np.sin(2 * np.pi * 440 * time)
    if peaky:
        # 短促高峰与低平均能量共存，复现“调高目标但固定增益几乎不变”。
        samples[rate*6:rate*6+24] = .95
    wav, path = root / 'input.wav', root / 'input.mp4'
    sf.write(wav, samples, rate, subtype='FLOAT')
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-threads', '1', '-f', 'lavfi', '-i',
        f'color=c=black:s=64x64:r=10:d={duration}', '-i', str(wav), '-c:v', 'libx264',
        '-threads', '1', '-preset', 'ultrafast', '-c:a', 'aac', '-b:a', '192k', str(path)],
        check=True, timeout=30)
    return path, duration


def _render(tmp_path, monkeypatch, *, ceiling, target=-16., peaky=True):
    from dubforge.pipeline import content_redaction, step_assemble
    monkeypatch.setattr(content_redaction, 'safe_overlay', lambda ctx, plan: plan)
    monkeypatch.setattr(content_redaction, 'audio_plan', lambda *args, **kwargs:
        dict(background=None, filter='', fallback_reason='', ranges=[],
             requested_mode='all_audio', effective_mode='all_audio'))
    source, duration = _video(tmp_path, peaky=peaky)
    results = {}
    for mode in ('normalize', 'uniform'):
        ctx, commands = _context(tmp_path / mode, mode, ceiling, target)
        output = tmp_path / f'{mode}.mp4'
        step_assemble._mux_original_sub(ctx, source, output, duration, (0, 99), burn=False)
        report = audio_quality.read_report(output)
        assert report['settings']['true_peak_db'] == ceiling
        assert report['true_peak_db'] <= ceiling + .05
        assert report['source_comparison']['status'] == 'ready'
        results[mode] = (report, commands)
    return results


@pytest.mark.parametrize('ceiling', [0., -.5])
def test_real_aac_peak_boundary_and_dynamic_vs_static_behavior(tmp_path, monkeypatch, ceiling):
    results = _render(tmp_path, monkeypatch, ceiling=ceiling)
    static, static_commands = results['normalize']
    uniform, _ = results['uniform']
    assert static['processing']['peak_limited'] and not static['target_reached']
    assert static['processing']['requested_gain_db'] > 10
    assert static['processing']['gain_db'] < 3
    assert not any('loudnorm=' in ' '.join(cmd) for cmd in static_commands)
    assert uniform['processing']['actual_method'] == 'dynamic'
    assert uniform['target_reached']
    assert uniform['integrated_lufs'] > static['integrated_lufs'] + 10


@pytest.mark.parametrize('ceiling', [0., -.5])
def test_lower_target_reduces_loudness_in_both_modes_without_compression(tmp_path, monkeypatch, ceiling):
    results = _render(tmp_path, monkeypatch, ceiling=ceiling, target=-30., peaky=False)
    static, _ = results['normalize']
    uniform, _ = results['uniform']
    for report in (static, uniform):
        assert report['target_reached']
        assert abs(report['integrated_lufs'] + 30) < .7
        assert report['processing']['gain_db'] < 0
        assert not report['processing']['peak_limited']
    assert uniform['processing']['actual_method'] == 'linear'


@pytest.mark.parametrize('ceiling', [0., -.5])
def test_actual_aac_overshoot_is_remeasured_and_corrected_at_new_boundaries(tmp_path, ceiling):
    source, _ = _video(tmp_path, peaky=False)
    output = tmp_path / 'hot.mp4'
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-threads', '1', '-i', str(source),
        '-c:v', 'copy', '-af', 'volume=15dB', '-c:a', 'aac', '-b:a', '192k', str(output)],
        check=True, timeout=30)
    before = audio_quality.measure(output)
    assert before['true_peak_db'] > 0
    ctx, _ = _context(tmp_path, 'normalize', ceiling)
    result = audio_quality.finalize(ctx, output, reference=before,
        applied=dict(gain_db=0., peak_limited=False, silent=False))
    assert result['true_peak_db'] <= ceiling + .05
    assert result['processing']['aac_peak_correction_db'] < 0
    assert result['processing']['gain_db'] == result['processing']['aac_peak_correction_db']
    assert audio_quality.measure(output)['true_peak_db'] == result['true_peak_db']
    assert not list(tmp_path.glob('.tmp_peak_*'))
