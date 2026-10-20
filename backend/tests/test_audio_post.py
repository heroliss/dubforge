import json
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from dubforge.errors import DubForgeError
from dubforge.pipeline import audio_post as post, audio_quality as quality


def line():
    return dict(id=1, text='测试声音', start=0., end=2., source='dub', take=1)


def test_gain_is_bound_to_sentence_slot_and_actual_audio(isolated_env):
    from dubforge.domain import crud
    project = crud.create_project('音量测试', 'https://example.com/video')
    if isinstance(project, str):
        project = crud.get_project(project)
    row = line()
    state = post.line_state(project, row, audio_version='first')
    saved = post.save_line(project, row, dict(gain_db=3., binding=state['binding'], revision=state['revision']),
                           audio_version='first')
    assert saved['effective_gain_db'] == 3 and saved['active']
    changed = post.line_state(project, row, audio_version='second')
    assert changed['stale'] and changed['gain_db'] == 0 and changed['saved_gain_db'] == 3
    assert post.line_state(project, {**row, 'take': 2}, audio_version='first')['stale']
    assert post.line_state(project, {**row, 'text': '新文字'}, audio_version='first')['stale']
    with pytest.raises(DubForgeError, match='已变化'):
        post.save_line(project, row, dict(gain_db=2, binding=state['binding'], revision=state['revision']),
                       audio_version='first')
    assert 'tts_lines' not in json.loads(project.params)


def test_gain_rejects_invalid_and_restores_without_touching_voices(isolated_env):
    from dubforge.domain import crud
    project = crud.create_project('音量恢复', 'https://example.com/video')
    if isinstance(project, str):
        project = crud.get_project(project)
    state = post.line_state(project, line(), audio_version='v1')
    for value in (True, float('nan'), -13, 13):
        with pytest.raises(DubForgeError):
            post.save_line(project, line(), dict(gain_db=value, binding=state['binding'], revision=state['revision']),
                           audio_version='v1')
    saved = post.save_line(project, line(), dict(gain_db=-2., binding=state['binding'], revision=state['revision']),
                           audio_version='v1')
    restored = post.save_line(project, line(), dict(reset=True, binding=saved['binding'], revision=saved['revision']),
                              audio_version='v1')
    assert restored['gain_db'] == 0 and not restored['stale'] and not post.records(project)


def test_leveling_is_bounded_conservative_and_never_boosts_silence():
    t = np.arange(32000) / 32000
    clips = [(0, (a * np.sin(2 * np.pi * 400 * t)).astype('float32')) for a in (.05, .2, .3)]
    clips.append((4, np.zeros(32000, dtype='float32')))
    balanced, levels, gains = post.balance_entries(clips, enabled=True, max_db=3)
    assert gains[0] == 3 and gains[-1] == 0 and levels[-1] is None
    assert all(abs(v) <= 3 for v in gains)
    assert gains[1] == 0
    assert np.max(np.abs(balanced[-1][1])) == 0
    untouched, _, zero = post.balance_entries(clips, enabled=False)
    assert zero == [0, 0, 0, 0]
    np.testing.assert_array_equal(untouched[0][1], clips[0][1])


def test_static_gain_preserves_peak_space_without_dynamic_compression():
    assert quality.static_gain(dict(integrated_lufs=-25, true_peak_db=-15), -16, -1.5)['gain_db'] == 9
    limited = quality.static_gain(dict(integrated_lufs=-25, true_peak_db=-2), -16, -1.5)
    assert limited['gain_db'] == .2 and limited['peak_limited']
    assert quality.static_gain(dict(integrated_lufs=None, true_peak_db=None), -16, -1.5)['silent']


def test_quality_measures_actual_audio_and_report_invalidates_on_replacement(tmp_path):
    rate = 32000
    t = np.arange(rate * 4) / rate
    path = tmp_path / 'voice.wav'
    sf.write(path, (.1 * np.sin(2 * np.pi * 440 * t)).astype('float32'), rate, subtype='FLOAT')
    report = quality.measure(path)
    assert -26 < report['integrated_lufs'] < -20
    assert -21 < report['true_peak_db'] < -19
    quality.write_report(path, report)
    assert quality.read_report(path)['integrated_lufs'] == report['integrated_lufs']
    path.write_bytes(b'replaced')
    assert quality.read_report(path) is None


def test_final_audio_normalization_keeps_video_duration_and_verifies_actual_aac(tmp_path, monkeypatch):
    import subprocess
    path = tmp_path / 'test.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-threads', '1', '-f', 'lavfi', '-i',
                    'color=c=black:s=64x64:r=10:d=4', '-f', 'lavfi', '-i',
                    'sine=frequency=440:sample_rate=48000:duration=4', '-c:v', 'libx264',
                    '-c:a', 'aac', '-af', 'volume=0.1', '-shortest', str(path)], check=True)
    values = {'assemble.final_loudness_mode': 'normalize'}
    def run(command, **kwargs):
        kwargs.pop('on_stdout', None)
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace', **kwargs)
        return result.returncode, result.stdout, result.stderr
    ctx = SimpleNamespace(cfg=lambda key, default=None: values.get(key, default),
                          run_subprocess=run, report_progress=lambda value: None, log=lambda value: None,
                          task_data={})
    from dubforge.pipeline import step_assemble, content_redaction
    monkeypatch.setattr(content_redaction, 'safe_overlay', lambda ctx, plan: plan)
    monkeypatch.setattr(content_redaction, 'audio_plan', lambda *a, **k: dict(background=None, filter='',
        fallback_reason='', ranges=[], requested_mode='all_audio', effective_mode='all_audio'))
    out = tmp_path / 'normalized.mp4'
    step_assemble._mux_original_sub(ctx, path, out, 4., (0, 99), burn=False)
    result = quality.read_report(out)
    assert result['target_reached']
    assert abs(result['integrated_lufs'] + 16) < .7
    assert result['true_peak_db'] <= -1.45
    assert result['processing']['gain_db'] > 0
    duration = float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
                     'format=duration', '-of', 'csv=p=0', str(out)]))
    assert abs(duration - 4) < .1


def test_silent_audio_has_no_fake_lufs_or_unbounded_boost(tmp_path):
    path = tmp_path / 'silence.wav'
    sf.write(path, np.zeros(96000, dtype='float32'), 32000)
    result = quality.measure(path)
    assert result['integrated_lufs'] is None
    assert result['true_peak_db'] is None
    assert any(row['kind'] == 'silent_audio' for row in result['issues'])


def test_bandlimited_resampling_rejects_out_of_band_signal(tmp_path):
    from dubforge.utils.audio import load_mono_32k
    rate = 48000
    t = np.arange(rate) / rate
    path = tmp_path / 'high.wav'
    sf.write(path, .5 * np.sin(2 * np.pi * 22000 * t), rate, subtype='FLOAT')
    actual = load_mono_32k(path)
    assert abs(len(actual) - 32000) <= 1
    assert np.sqrt(np.mean(actual[100:-100] ** 2)) < .001


def test_32k_stereo_keeps_average_fast_path_and_rejects_nonfinite_samples(tmp_path, monkeypatch):
    from dubforge.utils import audio
    path = tmp_path / 'stereo.wav'
    sf.write(path, np.column_stack([np.full(100, .25), np.full(100, .75)]), 32000, subtype='FLOAT')
    monkeypatch.setattr(audio.subprocess, 'run', lambda *a, **k: pytest.fail('32k 快速路径不应启动 FFmpeg'))
    actual = audio.load_mono_32k(path)
    np.testing.assert_allclose(actual, .5)
    assert actual.flags.writeable
    sf.write(path, np.array([0., float('nan'), 0.]), 32000, subtype='FLOAT')
    with pytest.raises(DubForgeError) as caught:
        audio.load_mono_32k(path)
    assert caught.value.code == 'AUDIO_DATA_INVALID'


def test_non32k_resampler_reads_file_without_python_pcm_stdin_copy(tmp_path, monkeypatch):
    from dubforge.utils import audio
    path = tmp_path / 'original.wav'
    sf.write(path, np.zeros((441, 2)), 44100, subtype='FLOAT')
    captured = {}
    def run(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return SimpleNamespace(returncode=0, stdout=np.zeros(320, dtype='<f4').tobytes(), stderr=b'')
    monkeypatch.setattr(audio.subprocess, 'run', run)
    monkeypatch.setattr(audio.sf, 'read', lambda *a, **k: pytest.fail('异采样率不应在Python全量读原片PCM'))
    actual = audio.load_mono_32k(path)
    assert 'input' not in captured['kwargs']
    assert captured['command'][captured['command'].index('-i') + 1] == str(path)
    assert len(actual) == 320 and actual.flags.writeable


def _render_context(monkeypatch, values, background=None):
    import subprocess
    from dubforge.pipeline import content_redaction
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        kwargs.pop('on_stdout', None)
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace', **kwargs)
        return result.returncode, result.stdout, result.stderr
    ctx = SimpleNamespace(cfg=lambda key, default=None: values.get(key, default),
        upstream=lambda *a: background, run_subprocess=run, report_progress=lambda value: None,
        log=lambda value: None, task_data={})
    monkeypatch.setattr(content_redaction, 'safe_overlay', lambda ctx, plan: plan)
    monkeypatch.setattr(content_redaction, 'audio_plan', lambda *a, **k: dict(background=None, filter='',
        fallback_reason='', ranges=[], requested_mode='all_audio', effective_mode='all_audio'))
    return ctx, commands


def test_full_mix_is_normalized_before_first_aac_so_over_unity_mix_does_not_clip(tmp_path, monkeypatch):
    import subprocess
    from dubforge.pipeline.step_assemble import _mux
    video = tmp_path / 'source.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-threads', '1', '-f', 'lavfi', '-i',
                    'color=c=black:s=64x64:r=10:d=4', '-an', '-c:v', 'libx264', str(video)], check=True)
    t = np.arange(32000 * 4) / 32000
    voice, background = tmp_path / 'voice.wav', tmp_path / 'background.wav'
    for path in (voice, background):
        sf.write(path, .9 * np.sin(2 * np.pi * 440 * t), 32000, subtype='FLOAT')
    ctx, commands = _render_context(monkeypatch, {'assemble.final_loudness_mode': 'normalize',
        'assemble.ducking': False}, background)
    out = tmp_path / 'output' / 'mix.mp4'
    out.parent.mkdir()
    _mux(ctx, tmp_path, video, voice, out, 4.)
    report = quality.read_report(out)
    assert report['reference']['true_peak_db'] > 4
    assert report['true_peak_db'] <= -1.5
    assert report['target_reached']
    # 正常路径仅编码一次 AAC；整片测量以无损滤镜输出到 null。
    assert sum('-c:a' in command and command[command.index('-c:a')+1] == 'aac' for command in commands) == 1


def test_ducking_trigger_does_not_follow_voice_listening_gain_and_zero_disables_it(tmp_path, monkeypatch):
    from dubforge.pipeline.step_assemble import _mixed_audio
    ctx, _ = _render_context(monkeypatch, {'assemble.voice_volume': .2}, tmp_path / 'bg.wav')
    _, graph, _ = _mixed_audio(ctx, tmp_path / 'v.mp4', tmp_path / 'dub.wav', 4.)
    joined = ';'.join(graph)
    assert '[voice_raw]asplit[trigger][speech_raw]' in joined
    assert '[speech_raw]volume=0.2[speech]' in joined
    ctx.cfg = lambda key, default=None: {'assemble.voice_volume': 0}.get(key, default)
    _, graph, _ = _mixed_audio(ctx, tmp_path / 'v.mp4', tmp_path / 'dub.wav', 4.)
    assert 'sidechaincompress' not in ';'.join(graph)


def test_disabled_new_defaults_do_not_change_legacy_configuration_fingerprint():
    from dubforge.pipeline import config_fingerprint
    base = {'assemble': dict(voice_volume=1, bgm_volume=1, loudnorm=True)}
    def deps(values):
        return SimpleNamespace(cfg=lambda key, default=None: values.get(key, default))
    before = config_fingerprint.snapshot(deps(base), 'assemble', keys=('assemble',))
    after = config_fingerprint.snapshot(deps({'assemble': {**base['assemble'],
        'final_loudness_mode': 'off', 'final_loudness_lufs': -16, 'final_true_peak_db': -1.5,
        'sentence_leveling': False, 'sentence_leveling_max_db': 3}}), 'assemble', keys=('assemble',))
    assert before == after


def test_original_preview_uses_full_mix_gain_even_when_selected_region_is_quieter(tmp_path, monkeypatch):
    import subprocess
    from dubforge.pipeline.step_assemble import _mux_original_sub
    rate = 48000
    t = np.arange(rate * 6) / rate
    samples = np.sin(2 * np.pi * 440 * t) * np.where(t < 3, .02, .4)
    wav, video = tmp_path / 'source.wav', tmp_path / 'source.mp4'
    sf.write(wav, samples, rate, subtype='FLOAT')
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-threads', '1', '-f', 'lavfi', '-i',
        'color=c=black:s=64x64:r=10:d=6', '-i', str(wav), '-c:v', 'libx264', '-c:a', 'aac', str(video)], check=True)
    ctx, _ = _render_context(monkeypatch, {'assemble.final_loudness_mode': 'normalize'})
    full, preview = tmp_path / 'full.mp4', tmp_path / 'preview.mp4'
    _mux_original_sub(ctx, video, full, 6, (0, 99), burn=False)
    _mux_original_sub(ctx, video, preview, 2, (0, 99), start=.5, burn=False)
    full_report, preview_report = quality.read_report(full), quality.read_report(preview)
    assert full_report['processing']['gain_db'] == preview_report['processing']['gain_db']
    assert preview_report['reference'] == full_report['reference']
    assert preview_report['integrated_lufs'] < full_report['integrated_lufs'] - 15
    assert preview_report['target_reached'] is None
    assert preview_report['preview'] is True
    assert full_report['preview'] is False


def test_audio_report_explicit_preview_scope_is_present_even_without_audio():
    ctx = SimpleNamespace(cfg=lambda key, default=None: default)
    assert quality.finalize(ctx, None, has_audio=False, preview=True)['preview'] is True
    assert quality.finalize(ctx, None, has_audio=False)['preview'] is False


def test_post_gain_changes_mixed_balance_without_regenerating_or_editing_wav(isolated_env, tmp_path, monkeypatch):
    from dubforge.domain import crud
    from dubforge.pipeline import step_assemble, step_dubtask, dubbing_edit
    project = crud.create_project('逐句后期验证')
    root = crud.project_dir(project.uid)
    voice_root = root / 'artifacts' / 'tts'
    voice_root.mkdir(parents=True, exist_ok=True)
    rows = [dict(id=i, text=f'句子{i}', start=float(i*2), end=float(i*2+1),
                 source='dub', take=1, explicit=True) for i in (1, 2)]
    t = np.arange(32000) / 32000
    for row in rows:
        sf.write(voice_root / f"{row['id']}.wav", .1 * np.sin(2 * np.pi * 440 * t), 32000)
    original = [(voice_root / f'{i}.wav').read_bytes() for i in (1, 2)]
    version = dubbing_edit.file_version(voice_root / '1.wav')
    state = post.line_state(project, rows[0], audio_version=version)
    post.save_line(project, rows[0], dict(gain_db=6, binding=state['binding'], revision=state['revision']),
                   audio_version=version)
    ctx = SimpleNamespace(project=project, cfg=lambda key, default=None: {'assemble.loudnorm': False}.get(key, default),
        upstream=lambda step, name: voice_root / name, check_cancel=lambda: None, log=lambda value: None)
    monkeypatch.setattr(step_dubtask, 'build_task', lambda ctx: dict(lines=rows))
    monkeypatch.setattr(step_assemble, '_require_dubbing', lambda ctx: None)
    monkeypatch.setattr(dubbing_edit, 'production_lines', lambda *a: rows)
    def place(ctx, pdir, inputs):
        ctx._voice_layout = dict(timing=dict(enabled=False))
        return inputs
    monkeypatch.setattr(step_assemble, '_apply_adaptive_voice_timing', place)
    out = step_assemble._mix_timeline(ctx, root, 7, register=False)
    mixed, rate = sf.read(out)
    a = np.sqrt(np.mean(mixed[round(2.1*rate):round(2.9*rate)] ** 2))
    b = np.sqrt(np.mean(mixed[round(4.1*rate):round(4.9*rate)] ** 2))
    assert 1.98 < a/b < 2.01
    assert [(voice_root / f'{i}.wav').read_bytes() for i in (1, 2)] == original
    assert ctx._audio_line_report[0]['gain_db'] == 6
