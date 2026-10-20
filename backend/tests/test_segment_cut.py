"""真实小媒体校验精确删帧、完整 GOP 字节复用与失败保护。"""
from pathlib import Path
import subprocess

import pytest

from dubforge.pipeline import segment_cut as sc, segment_replace as sr
from test_segment_replace import media, Context, ff, frame_hashes


def keeps(start, end, duration=6.):
    return [row for row in [dict(start=0., end=start), dict(start=end, end=duration)] if row['end'] > row['start']]


@pytest.mark.parametrize('start,end', [(2.4, 2.6), (2., 4.), (0., 1.), (5., 6.), (.301, .399),
                                     (61/30, 4.), (2., 118/30)])
def test_smart_cut_matches_frames_and_preserves_copied_gops(media, tmp_path, start, end):
    plan = sc.analyze(media, keeps(start, end))
    assert plan['method'] == 'smart_cut'
    out, ctx = tmp_path / 'cut.mp4', Context()
    result = sc.render(ctx, media, out, plan, crf=21)
    assert result['validated']
    original, changed = frame_hashes(media), frame_hashes(out)
    assert len(changed) == plan['output_frames']
    cursor = 0
    for span in plan['spans']:
        size = span['end_frame'] - span['start_frame']
        if not span['encode']:
            assert changed[cursor:cursor + size] == original[span['start_frame']:span['end_frame']]
        cursor += size
    info = sr.inspect_base(out)
    assert info['duration'] == pytest.approx(plan['output_duration'], abs=1e-6)
    assert info['audio_duration'] == pytest.approx(plan['output_duration'], abs=.03)
    assert sum('-c:a' in command and command[command.index('-c:a') + 1] == 'aac' for command in ctx.commands) == 1
    assert not list(tmp_path.glob('.dubforge-smart-cut-*'))


def test_tiny_seams_borrow_adjacent_gop_before_falling_back(media):
    before = sc.analyze(media, keeps(61/30, 4.))
    assert before['method'] == 'smart_cut'
    assert before['spans'] == [dict(start_frame=0, end_frame=61, encode=True),
                              dict(start_frame=120, end_frame=180, encode=False)]
    after = sc.analyze(media, keeps(2., 118/30))
    assert after['method'] == 'smart_cut'
    assert after['spans'] == [dict(start_frame=0, end_frame=60, encode=False),
                             dict(start_frame=118, end_frame=180, encode=True)]


def test_plan_discards_all_intersecting_frames_and_never_rounds_deletion_inward(media):
    plan = sc.analyze(media, keeps(2.411, 2.589))
    assert [(row['start_frame'], row['end_frame']) for row in plan['keep_ranges']] == [(0, 72), (78, 180)]
    assert plan['output_frames'] == 174
    assert plan['quantization_seconds'] > 0
    assert sc.analyze(media, [dict(start=2.4, end=2.6)])['method'] == 'full_encode'
    short = sc.analyze(media, keeps(1/30, 2.))
    assert short['method'] == 'full_encode'
    assert short['fallback_code'] == 'SEGMENT_CUT_SHORT_REORDER_WINDOW'


def test_failure_or_cancel_does_not_replace_target(media, tmp_path, monkeypatch):
    plan = sc.analyze(media, keeps(2.4, 2.6))
    out = tmp_path / 'existing.mp4'
    out.write_bytes(b'existing')
    def failed(*args):
        raise sr.SegmentReplaceError('编码头不一致', code='SEGMENT_REPLACE_INCOMPATIBLE')
    monkeypatch.setattr(sr, '_compatible', failed)
    with pytest.raises(sr.SegmentReplaceError):
        sc.render(Context(), media, out, plan, crf=21)
    assert out.read_bytes() == b'existing'
    ctx = Context(); ctx.cancelled = True
    with pytest.raises(RuntimeError, match='已取消'):
        sc.render(ctx, media, out, plan, crf=21)
    assert out.read_bytes() == b'existing'
    assert not list(tmp_path.glob('.dubforge-smart-cut-*'))


def test_unsupported_codec_has_explicit_fallback(media, tmp_path):
    path = tmp_path / 'mpeg4.mp4'
    ff('-i', media, '-c:v', 'mpeg4', '-q:v', '3', '-c:a', 'copy', path)
    plan = sc.analyze(path, keeps(2.4, 2.6))
    assert plan['method'] == 'full_encode'
    assert plan['fallback_code'] == 'SEGMENT_REPLACE_UNSUPPORTED'
    assert 'H.264' in plan['fallback_reason']


def test_silent_and_fractional_frame_rate(media, tmp_path):
    silent, out = tmp_path / 'silent.mp4', tmp_path / 'silent-cut.mp4'
    ff('-i', media, '-map', '0:v:0', '-an', '-c:v', 'copy', silent)
    plan = sc.analyze(silent, keeps(2.4, 2.6))
    sc.render(Context(), silent, out, plan, crf=21)
    assert not sr.inspect_base(out)['has_audio']
    fractional, target = tmp_path / 'fractional.mp4', tmp_path / 'fractional-cut.mp4'
    ff('-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=30000/1001:duration=6',
       '-c:v', 'libx264', '-threads', '2', '-crf', '21', '-pix_fmt', 'yuv420p', '-g', '60',
       '-keyint_min', '60', '-sc_threshold', '0', '-bf', '3',
       '-x264-params', 'open-gop=0:repeat-headers=1', fractional)
    info = sr.inspect_base(fractional)
    plan = sc.analyze(fractional, keeps(2.4, 2.6, info['duration']))
    assert plan['method'] == 'smart_cut'
    sc.render(Context(), fractional, target, plan, crf=21)
    assert sr.inspect_base(target)['frame_count'] == plan['output_frames']


def test_delayed_audio_keeps_leading_silence(media, tmp_path):
    import numpy as np
    delayed, out = tmp_path / 'delayed.mp4', tmp_path / 'delayed-cut.mp4'
    ff('-i', media, '-itsoffset', '0.1', '-i', media, '-map', '0:v:0', '-map', '1:a:0',
       '-c', 'copy', delayed)
    plan = sc.analyze(delayed, keeps(2.4, 2.6))
    sc.render(Context(), delayed, out, plan, crf=21)
    decoded = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(out), '-map', '0:a:0', '-ac', '1',
        '-ar', '48000', '-f', 'f32le', '-'], capture_output=True, timeout=30, check=True)
    samples = np.frombuffer(decoded.stdout, dtype='<f4')
    assert np.max(np.abs(samples[:2400])) < .001
    assert np.sqrt(np.mean(samples[9600:14400] ** 2)) > .01


def _pcm_with_audio_graph(media, kept, offset):
    seek = ['-ss', f'{offset / 48000:.12f}'] if offset else []
    command = ['ffmpeg', '-v', 'error', '-threads', '1', '-filter_complex_threads', '1',
        '-i', str(media), *seek, '-i', str(media), '-filter_complex', sc._audio_graph(kept, offset),
        '-map', '[aout]', '-c:a', 'pcm_f32le', '-f', 'f32le', '-']
    return subprocess.run(command, capture_output=True, check=True, timeout=30).stdout


@pytest.mark.parametrize('delay', [0., 1.9])
@pytest.mark.parametrize('audio_rate', [48000, 44100])
def test_audio_seek_matches_original_samples_at_fractional_boundaries(media, tmp_path, delay, audio_rate):
    import numpy as np
    source = media
    if audio_rate != 48000:
        source = tmp_path / 'rate44100.mp4'
        ff('-i', media, '-c:v', 'copy', '-c:a', 'aac', '-ar', str(audio_rate), source)
    if delay:
        delayed = tmp_path / 'late-audio.mp4'
        ff('-i', source, '-itsoffset', str(delay), '-i', source, '-map', '0:v:0', '-map', '1:a:0', '-c', 'copy', delayed)
        source = delayed
    # 选择非整数帧/非整数采样的切点，第二段也不能积累第一段的误差。
    kept = [dict(start=1.711037, end=2.789019), dict(start=3.001013, end=4.123345)]
    offset = sc._audio_seek_offset(Context(), source, kept)
    assert offset > 0 and offset < kept[0]['start'] * 48000 - 47999
    original = np.frombuffer(_pcm_with_audio_graph(source, kept, 0), dtype='<f4')
    optimized = np.frombuffer(_pcm_with_audio_graph(source, kept, offset), dtype='<f4')
    assert len(optimized) == len(original)
    np.testing.assert_allclose(optimized, original, rtol=1e-5, atol=2e-6)
    if delay:
        assert np.max(np.abs(optimized[:int(.15 * 48000) * 2])) < .001


def test_audio_seek_preserves_integer_sample_rounding_and_rejects_unknown_origin(monkeypatch):
    kept = [dict(start=1893.011013, end=1893.543219)]
    monkeypatch.setattr(sr, '_probe', lambda *a: dict(format={'start_time': '0.000000'},
        streams=[dict(codec_type='audio', sample_rate='48000')]))
    offset = sc._audio_seek_offset(None, Path('base.mp4'), kept)
    assert offset == 90816528
    graph = sc._audio_graph(kept, offset)
    assert f'start_sample={round(kept[0]["start"] * 48000) - offset}' in graph
    for origin in ['-0.1', '1.4', 'N/A', None]:
        monkeypatch.setattr(sr, '_probe', lambda *a, origin=origin: dict(format={'start_time': origin}))
        assert sc._audio_seek_offset(None, Path('base.mp4'), kept) == 0


def test_late_window_render_seeks_only_audio_input_and_preserves_duration(media, tmp_path):
    plan = sc.analyze(media, [dict(start=2.311, end=6.)])
    assert plan['method'] == 'smart_cut'
    ctx, target = Context(), tmp_path / 'late-window.mp4'
    result = sc.render(ctx, media, target, plan, crf=21)
    assert result['audio_seek_offset_seconds'] == pytest.approx(4/3)
    command = next(command for command in ctx.commands if '-c:a' in command)
    inputs = [index for index, value in enumerate(command) if value == '-i']
    assert command[inputs[1] - 2] == '-ss'
    assert float(command[inputs[1] - 1]) == pytest.approx(4/3)
    final = sr.inspect_base(target)
    assert final['frame_count'] == plan['output_frames']
    assert final['audio_duration'] == pytest.approx(plan['output_duration'], abs=.03)
