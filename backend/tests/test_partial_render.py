"""局部渲染复用正式声音策略；测试媒体均在隔离目录生成。"""
import copy
import subprocess
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from dubforge.pipeline import content_redaction, step_assemble


def _context(root, values=None):
    commands = []
    def run(cmd, **kwargs):
        commands.append(list(cmd))
        kwargs.pop('on_stdout', None)
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                                errors='replace', timeout=30, **kwargs)
        return result.returncode, result.stdout, result.stderr
    ctx = SimpleNamespace(pdir=root, task_data={}, cfg=lambda k, d=None: (values or {}).get(k, d),
        upstream=lambda *args: None, check_cancel=lambda: None, check_pause=lambda: None,
        run_subprocess=run, log=lambda *args: None, report_progress=lambda *args: None)
    return ctx, commands


def _media(root):
    source = root / 'source.mp4'
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i',
        'color=c=blue:size=160x96:rate=10:duration=3', '-f', 'lavfi', '-i',
        'sine=frequency=440:sample_rate=48000:duration=3', '-c:v', 'libx264',
        '-threads', '1', '-preset', 'ultrafast', '-c:a', 'aac', str(source)],
        check=True, timeout=30)
    voice = root / 'voice.wav'
    sf.write(voice, np.sin(np.arange(3*32000)*2*np.pi*800/32000).astype('float32')*.05,
             32000, subtype='FLOAT')
    return source, voice


@pytest.fixture
def no_review(monkeypatch):
    monkeypatch.setattr(content_redaction, 'safe_overlay', lambda ctx, plan: plan)
    monkeypatch.setattr(content_redaction, 'audio_plan', lambda *args, **kwargs:
        dict(background=None, filter='', fallback_reason='', ranges=[],
             requested_mode='all_audio', effective_mode='all_audio'))


@pytest.mark.parametrize('dubbing', [False, True])
@pytest.mark.parametrize('mode', ['off', 'normalize', 'uniform'])
def test_pcm_candidate_reuses_audio_processing_without_video_or_aac(tmp_path, no_review, dubbing, mode):
    source, voice = _media(tmp_path)
    ctx, commands = _context(tmp_path, {'assemble.final_loudness_mode': mode,
        'assemble.final_loudness_lufs': -16., 'assemble.final_true_peak_db': -1.5})
    output = tmp_path / 'candidate.wav'
    if dubbing:
        step_assemble._mux(ctx, tmp_path, source, voice, output, 3., audio_only=True)
    else:
        step_assemble._mux_original_sub(ctx, source, output, 3., (0, 20), audio_only=True)
    audio = sf.info(output)
    assert audio.samplerate == 48000 and audio.channels == 2 and audio.subtype == 'FLOAT'
    assert audio.frames == 3*48000
    assert not any('.tmp_' in path.name for path in tmp_path.glob('*.wav'))
    candidate_cmd = commands[-1]
    assert '-vn' in candidate_cmd and 'pcm_f32le' in candidate_cmd
    assert not any(option in candidate_cmd for option in ('-c:v', '-vf', 'aac'))
    assert not hasattr(ctx, '_audio_report')


def test_pcm_candidate_failure_keeps_previous_audio(tmp_path, monkeypatch):
    ctx, _ = _context(tmp_path)
    output = tmp_path / 'candidate.wav'
    output.write_bytes(b'previous candidate')
    ctx.run_subprocess = lambda *a, **kw: (1, '', 'test encoder failed')
    from dubforge.errors import DubForgeError
    with pytest.raises(DubForgeError):
        step_assemble._write_audio_candidate(ctx, ['ffmpeg', '-i', 'source.wav'],
            ['[0:a]anull[mix]'], '[mix]', output, 3.)
    assert output.read_bytes() == b'previous candidate'
    assert not (tmp_path / '.tmp_candidate.wav').exists()


def test_frame_ranges_preserve_fractional_boundary_coverage():
    from dubforge.pipeline.partial_render import _exact_frames
    assert _exact_frames([{'start': 1.201, 'end': 1.799}], 10) == [(12, 18)]
    assert _exact_frames([{'start': 1.2, 'end': 1.8}], 10) == [(12, 18)]


@pytest.mark.parametrize('window', [(0, 30), (20, 30)])
def test_extended_window_uses_clean_source_only_inside_exact_ranges(tmp_path, window):
    from dubforge.pipeline import partial_render, segment_replace
    base, source = tmp_path / 'base.mp4', tmp_path / 'clean.mp4'
    for path, color in ((base, 'red'), (source, 'blue')):
        subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i',
            f'color=c={color}:size=160x96:rate=10:duration=3', '-c:v', 'libx264',
            '-threads', '2', '-preset', 'medium', '-x264-params',
            'keyint=20:min-keyint=20:scenecut=0:open-gop=0', '-pix_fmt', 'yuv420p', str(path)],
            check=True, timeout=30)
    info = segment_replace.inspect_base(base)
    ranges = [{'start': 1.2, 'end': 1.8}, {'start': 2.2, 'end': 2.4}]
    ctx, _ = _context(tmp_path)
    ctx.version = dict(burn_subtitles=False, comment_overlay=None)
    target = tmp_path / 'patched.ts'
    partial_render.render_segment(ctx, base, source, ranges, *window, info, target)
    decoded = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(target), '-an',
        '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'], capture_output=True, check=True, timeout=30).stdout
    frames = np.frombuffer(decoded, np.uint8).reshape(-1, 96, 160, 3)
    assert len(frames) == window[1] - window[0]
    for index, frame in enumerate(frames, window[0]):
        rgb = frame[48, 80]
        inside = 12 <= index < 18 or 22 <= index < 24
        assert (rgb[2] > 200 and rgb[0] < 20) if inside else (rgb[0] > 200 and rgb[2] < 20)


@pytest.mark.parametrize('replace_audio', [False, True])
def test_partial_run_prepares_full_audio_at_most_once_for_many_ranges(tmp_path, monkeypatch, replace_audio):
    from dubforge.pipeline import assembly_partial, partial_render, segment_replace, audio_quality
    ranges = [{'start': .5, 'end': 1.}, {'start': 2., 'end': 2.5}]
    plan = dict(id='test', base_path=str(tmp_path/'base.mp4'), base_sha256='a'*64,
                ranges=ranges, replace_audio=replace_audio, base_spec={'has_audio': True})
    monkeypatch.setattr(assembly_partial, 'prepare', lambda *args: plan)
    calls = []
    def mix(ctx, pdir, total, **kwargs):
        calls.append(('mix', total))
        path = pdir/'voice.wav'
        path.write_bytes(b'voice')
        return path
    def mux(ctx, pdir, source, voice, output, total, **kwargs):
        calls.append(('candidate', total, kwargs))
        output.write_bytes(b'candidate')
    def replace(ctx, base, output, given, render, *, audio_candidate):
        calls.append(('replace', given, audio_candidate is not None))
        output.write_bytes(b'new composition')
        return {'encode_seconds': 2.}
    monkeypatch.setattr(step_assemble, '_mix_timeline', mix)
    monkeypatch.setattr(step_assemble, '_mux', mux)
    monkeypatch.setattr(segment_replace, 'replace', replace)
    monkeypatch.setattr(audio_quality, 'measure', lambda *args, **kwargs:
        dict(integrated_lufs=-18., true_peak_db=-3., loudness_range_lu=2., issues=[]))
    ctx, _ = _context(tmp_path)
    ctx.version = {'dubbing': True}
    partial_render.run(ctx, tmp_path/'source.mp4', tmp_path/'out.mp4', 3.)
    assert sum(row[0] == 'mix' for row in calls) == int(replace_audio)
    assert sum(row[0] == 'candidate' for row in calls) == int(replace_audio)
    assert calls[-1] == ('replace', ranges, replace_audio)
    if replace_audio:
        assert calls[1][2] == {'audio_only': True}
        assert not (tmp_path/'voice.wav').exists()
    assert ctx._audio_report['partial_replacement']['replace_audio'] == replace_audio


@pytest.mark.parametrize('cache_kind', ['valid', 'missing', 'wrong_hash', 'invalid_numbers', 'invalid_schema'])
def test_unchanged_audio_reuses_bound_measurement_and_keeps_original_settings(tmp_path, monkeypatch, cache_kind):
    from dubforge.pipeline import assembly_partial, partial_render, segment_replace, audio_quality
    original = dict(schema=1, has_audio=True, preview=False, integrated_lufs=-14., true_peak_db=-2.,
        loudness_range_lu=3., issues=[], settings=dict(mode='uniform', target_lufs=-14., true_peak_db=-1.5),
        processing={'actual_method': 'dynamic', 'gain_db': None, 'peak_limited': False}, target_reached=True,
        scope={'kind': 'full', 'output_duration': 3.}, file={'name': 'base.mp4', 'size': 1, 'mtime_ns': 1})
    cached = dict(base_sha256='a'*64, report=copy.deepcopy(original))
    if cache_kind == 'missing':
        cached = None
    elif cache_kind == 'wrong_hash':
        cached['base_sha256'] = 'b'*64
    elif cache_kind == 'invalid_numbers':
        cached['report']['true_peak_db'] = float('nan')
    elif cache_kind == 'invalid_schema':
        cached['report']['schema'] = 0
    plan = dict(id='test', base_path=str(tmp_path/'base.mp4'), base_sha256='a'*64,
        ranges=[{'start': .5, 'end': 1.}], replace_audio=False, base_spec={'has_audio': True},
        baseline_audio_report=cached)
    before = copy.deepcopy(cached)
    monkeypatch.setattr(assembly_partial, 'prepare', lambda *args: plan)
    monkeypatch.setattr(segment_replace, 'replace', lambda *args, **kwargs: {'validated': True})
    measurements = []
    def measure(*args, **kwargs):
        measurements.append(1)
        return dict(schema=1, integrated_lufs=-14., true_peak_db=-2., loudness_range_lu=3., issues=[])
    monkeypatch.setattr(audio_quality, 'measure', measure)
    ctx, _ = _context(tmp_path, {'assemble.final_loudness_lufs': -10.})
    ctx.version = {'dubbing': False}
    partial_render.run(ctx, tmp_path/'source.mp4', tmp_path/'out.mp4', 3.)
    reused = cache_kind == 'valid'
    assert len(measurements) == int(not reused)
    assert ctx._audio_report['partial_replacement']['measurement_reused'] is reused
    if reused:
        assert ctx._audio_report['settings'] == original['settings']
        assert ctx._audio_report['scope'] == original['scope']
        assert ctx._audio_report['target_reached'] is True
        assert ctx._audio_report['processing']['actual_method'] == 'dynamic'
        assert ctx._audio_report['measurement_provenance']['base_sha256'] == plan['base_sha256']
        assert cached == before, '复用时不能修改不可变计划内的测量或处理记录'
        assert 'file' not in ctx._audio_report
    else:
        assert ctx._audio_report['settings']['target_lufs'] == -10.


def test_replaced_audio_always_measures_even_when_base_has_report(tmp_path, monkeypatch):
    from dubforge.pipeline import assembly_partial, partial_render, segment_replace, audio_quality
    plan = dict(id='test', base_path=str(tmp_path/'base.mp4'), base_sha256='a'*64,
        ranges=[{'start': .5, 'end': 1.}], replace_audio=True, base_spec={'has_audio': True},
        baseline_audio_report={'base_sha256': 'a'*64, 'report': dict(schema=1, has_audio=True,
            integrated_lufs=-14., true_peak_db=-2., loudness_range_lu=3., settings={}, processing={}, issues=[])})
    monkeypatch.setattr(assembly_partial, 'prepare', lambda *args: plan)
    monkeypatch.setattr(segment_replace, 'replace', lambda *args, **kwargs: {'validated': True})
    monkeypatch.setattr(step_assemble, '_mux_original_sub', lambda *args, **kwargs: None)
    measured = []
    def measure(*args, **kwargs):
        measured.append(1)
        return dict(schema=1, integrated_lufs=-18., true_peak_db=-3., loudness_range_lu=2., issues=[])
    monkeypatch.setattr(audio_quality, 'measure', measure)
    ctx, _ = _context(tmp_path)
    ctx.version = {'dubbing': False}
    partial_render.run(ctx, tmp_path/'source.mp4', tmp_path/'out.mp4', 3.)
    assert measured == [1]
    assert ctx._audio_report['partial_replacement']['measurement_reused'] is False
    assert ctx._audio_report['integrated_lufs'] == -18.


@pytest.mark.parametrize('replace_audio', [False, True])
def test_partial_version_uses_formal_registration_and_preserves_base(make_project, monkeypatch, replace_audio):
    from dubforge.domain import crud
    from dubforge.pipeline import assembly_partial, assembly_versions as av
    from dubforge.pipeline.dependencies import Dependencies
    from dubforge.runner import runner
    from test_assemble import _prepare_outputs_project
    from test_assembly_versions import wait

    uid, root = _prepare_outputs_project(make_project, monkeypatch)
    base = dict(id='base', name='原声基准', dubbing=False, burn_subtitles=True,
                include_comments=False, overrides={})
    av.save(uid, {'revision': av.catalog(uid)['revision'], 'versions': [base]})
    first = av.plan(uid, {'version_ids': ['base']})
    runner.start_versions(uid, {'version_ids': ['base'], 'fingerprint': first['fingerprint']})
    wait(uid)
    from dubforge.pipeline import audio_quality
    original_measure = audio_quality.measure
    measurements = []
    def measure(*args, **kwargs):
        measurements.append(str(args[0]))
        return original_measure(*args, **kwargs)
    monkeypatch.setattr(audio_quality, 'measure', measure)
    record = av.results(uid)['base']
    base_path = root/'output/final_version_base.mp4'
    import hashlib
    original_hash = hashlib.sha256(base_path.read_bytes()).hexdigest()
    request = dict(base_version_id='base', ranges=[{'start': 1., 'end': 1.8}], replace_audio=replace_audio)
    plan = assembly_partial.plan(uid, request)
    child = assembly_partial.create(uid, {**request, 'fingerprint': plan['fingerprint']})['version']
    child['overrides']['assemble.subtitle_color'] = '#00FF00'
    av.save(uid, {'revision': av.catalog(uid)['revision'], 'versions': [base, child]})
    second = av.plan(uid, {'version_ids': [child['id']]})
    runner.start_versions(uid, {'version_ids': [child['id']], 'fingerprint': second['fingerprint']})
    wait(uid)
    saved = av.results(uid)
    assert saved['base'].artifact_id == record.artifact_id
    assert hashlib.sha256(base_path.read_bytes()).hexdigest() == original_hash
    assert saved[child['id']].artifact_hash != original_hash
    deps = Dependencies(crud.get_project(uid))
    resolved = av.resolve(deps, child)
    assert av.is_current(deps, resolved, verify_hash=True)
    assert len(measurements) == int(replace_audio), '整轨直接复制时应复用基片报告，不能重复解码测量'
    artifact = av.result_artifact(deps, resolved)
    report = audio_quality.read_report(deps.path(artifact.step_key, artifact.name))
    assert report['partial_replacement']['measurement_reused'] is not replace_audio
    assert not list((root/'output').glob('.tmp_pending_*'))
