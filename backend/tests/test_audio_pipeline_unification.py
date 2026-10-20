"""用真实FFmpeg验证统一响度、同段原片对照与裁剪试片共同消费完整音轨。"""
import json
import subprocess
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from dubforge.pipeline import audio_quality, content_redaction, step_assemble


def context(root, monkeypatch, **values):
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        kwargs.pop('on_stdout', None)
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                errors='replace', check=False, **kwargs)
        return result.returncode, result.stdout, result.stderr
    ctx = SimpleNamespace(pdir=root, cfg=lambda key, default=None: values.get(key, default),
        upstream=lambda *args: None, run_subprocess=run, report_progress=lambda *args: None,
        check_cancel=lambda: None, log=lambda *args: None, task_data={})
    monkeypatch.setattr(content_redaction, 'safe_overlay', lambda ctx, plan: plan)
    monkeypatch.setattr(content_redaction, 'audio_plan', lambda *args, **kwargs: dict(background=None,
        filter='', fallback_reason='', ranges=[], requested_mode='all_audio', effective_mode='all_audio'))
    return ctx, commands


def video(root, amplitude=.1, duration=8):
    root.mkdir(parents=True, exist_ok=True)
    samples = np.arange(round(48000 * duration)) / 48000
    volume = amplitude(samples) if callable(amplitude) else amplitude
    source = root / 'source.wav'
    sf.write(source, volume * np.sin(2 * np.pi * 440 * samples), 48000, subtype='FLOAT')
    target = root / 'source.mp4'
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-threads', '1', '-f', 'lavfi', '-i',
        f'color=c=black:s=64x64:r=10:d={duration}', '-i', str(source), '-c:v', 'libx264',
        '-preset', 'ultrafast', '-c:a', 'aac', '-b:a', '192k', str(target)], check=True)
    return target


@pytest.mark.parametrize('amplitude', [.02, .6])
def test_quiet_and_loud_sources_use_same_output_target_not_source_loudness(tmp_path, monkeypatch, amplitude):
    source = video(tmp_path, amplitude)
    ctx, commands = context(tmp_path, monkeypatch, **{'assemble.final_loudness_mode': 'uniform',
                                                    'assemble.final_loudness_lufs': -18})
    target = tmp_path / 'full.mp4'
    step_assemble._mux_original_sub(ctx, source, target, 8, (0, 99), burn=False)
    report = audio_quality.read_report(target)
    assert abs(report['integrated_lufs'] + 18) < .7
    assert report['true_peak_db'] <= -1.5
    assert report['assessment']['target_status'] == 'reached'
    assert report['source_comparison']['status'] == 'ready'
    assert report['scope']['kind'] == 'full'
    delta = report['comparison']['integrated_delta_lu']
    assert delta > 10 if amplitude == .02 else delta < -5
    assert sum('-c:a' in cmd and cmd[cmd.index('-c:a') + 1] == 'aac' for cmd in commands) == 1


def test_uniform_cut_preview_reuses_whole_processed_track_and_compares_same_kept_segments(tmp_path, monkeypatch):
    source = video(tmp_path, lambda t: np.where(t < 4, .02, np.where(t < 8, .4, .07)), 12)
    ctx, commands = context(tmp_path, monkeypatch, **{'assemble.final_loudness_mode': 'uniform'})
    cuts = dict(schema=1, source_duration=12., output_duration=9., ranges=[dict(start=4., end=7.)])
    ctx.task_data['output_cuts'] = cuts
    full, preview = tmp_path / 'full.mp4', tmp_path / 'preview.mp4'
    step_assemble._mux_original_sub(ctx, source, full, 12, (0, 99), burn=False)
    initial_passes = sum('loudnorm=' in ' '.join(cmd) for cmd in commands)
    step_assemble._mux_original_sub(ctx, source, preview, 7, (0, 99), start=2, burn=False)
    assert sum('loudnorm=' in ' '.join(cmd) for cmd in commands) == initial_passes
    complete, region = audio_quality.read_report(full), audio_quality.read_report(preview)
    assert complete['full_output_measurement'] == region['full_output_measurement']
    assert complete['processing'] == region['processing']
    assert region['target_reached'] is None and region['assessment']['target_status'] == 'preview'
    assert region['assessment']['target_delta_lu'] is None and not region['assessment']['lra_reliable']
    assert [(s['start'], s['end']) for s in region['scope']['source_segments']] == [(2., 4.), (7., 9.)]
    assert region['scope']['output_duration'] == 4
    assert region['source_comparison']['status'] == 'ready'
    def audio(path, start=None):
        cmd = ['ffmpeg', '-v', 'error']
        if start is not None:
            cmd += ['-ss', str(start)]
        cmd += ['-i', str(path), '-t', '4', '-vn', '-ar', '48000', '-ac', '1', '-f', 'f32le', 'pipe:1']
        return np.frombuffer(subprocess.run(cmd, check=True, capture_output=True).stdout, '<f4')
    a, b = audio(full, 2), audio(preview)
    length = min(len(a), len(b))
    # AAC片段边界可有少量差别；去掉边缘后同一时间的主体必须一致。
    assert length >= 48000 * 3.9
    assert np.corrcoef(a[4800:length - 4800], b[4800:length - 4800])[0, 1] > .98


def test_uniform_dub_pipeline_processes_mix_once_and_keeps_voice_file(tmp_path, monkeypatch):
    source = video(tmp_path)
    voice = tmp_path / 'voice.wav'
    t = np.arange(32000 * 8) / 32000
    sf.write(voice, .015 * np.sin(2 * np.pi * 700 * t), 32000, subtype='FLOAT')
    before = voice.read_bytes()
    ctx, _ = context(tmp_path, monkeypatch, **{'assemble.final_loudness_mode': 'uniform'})
    out = tmp_path / 'output' / 'dub.mp4'
    out.parent.mkdir()
    step_assemble._mux(ctx, tmp_path, source, voice, out, 8.)
    report = audio_quality.read_report(out)
    assert report['settings']['mode'] == 'uniform' and report['target_reached']
    assert report['source_comparison']['status'] == 'ready'
    assert report['processing']['actual_method'] == 'linear'
    assert voice.read_bytes() == before


def test_dynamic_peak_control_survives_actual_aac_encoding_and_report(tmp_path, monkeypatch):
    source = video(tmp_path, lambda t: np.where((t >= 6) & (t < 6.0005), .95, .03), 12)
    ctx, _ = context(tmp_path, monkeypatch, **{'assemble.final_loudness_mode': 'uniform'})
    target = tmp_path / 'dynamic.mp4'
    step_assemble._mux_original_sub(ctx, source, target, 12, (0, 99), burn=False)
    report = audio_quality.read_report(target)
    assert report['processing']['actual_method'] == 'dynamic'
    assert report['processing']['gain_db'] is None
    assert abs(report['integrated_lufs'] + 16) <= .7
    assert report['true_peak_db'] <= -1.5
    assert report['target_reached'] and report['source_comparison']['status'] == 'ready'
    json.dumps(report, allow_nan=False)


def test_uniform_mode_is_valid_in_shared_project_and_version_schema():
    from dubforge.pipeline import assembly_versions
    from dubforge.pipeline.step_params import STEP_PARAM_SCHEMA
    definitions = {field.path: field for field in STEP_PARAM_SCHEMA['assemble']}
    assert ('统一响度（推荐，必要时控制峰值）', 'uniform') in definitions['assemble.final_loudness_mode'].options
    dependency = definitions['assemble.final_loudness_lufs'].depends_on[0]
    assert dependency == ('assemble.final_loudness_mode', ('normalize', 'uniform'))
    assert audio_quality.settings(lambda key, default=None: 'uniform' if key.endswith('_mode') else default)['mode'] == 'uniform'
    value = assembly_versions.validate(dict(id='unified', name='统一响度版', dubbing=False,
        burn_subtitles=False, include_comments=False, overrides={'assemble.final_loudness_mode': 'uniform'}))
    assert value['overrides']['assemble.final_loudness_mode'] == 'uniform'


def test_review_revision_uses_true_source_comparison_and_uniform_audio(make_project):
    from test_review_render import _fixture
    from dubforge.pipeline import review_render
    from dubforge.pipeline.assembly_models import AssemblyResult
    from dubforge.domain import crud
    uid, root, art, _ = _fixture(make_project)
    with crud.session() as session:
        from sqlmodel import select
        row = session.exec(select(AssemblyResult).where(AssemblyResult.artifact_id == art.id)).one()
        snapshot = json.loads(row.snapshot)
        snapshot['values']['assemble.final_loudness_mode'] = 'uniform'
        row.snapshot = json.dumps(snapshot)
        session.add(row); session.commit()
    result = review_render.repair(uid, [art.id])['repaired'][0]
    generated = next(a for a in crud.latest_artifacts(crud.get_project(uid).id, ['assemble']).values()
                     if a.id == result['artifact_id'])
    report = audio_quality.read_report(root / generated.rel_path)
    assert report['settings']['mode'] == 'uniform'
    assert report['source_comparison']['status'] == 'ready'
    assert report['scope']['source_segments'] == [dict(start=0., end=3., output_start=0., output_end=3.)]
