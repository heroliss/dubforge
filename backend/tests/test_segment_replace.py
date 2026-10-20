"""使用小型真实媒体验证局部编码，所有文件均在pytest独立目录。"""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from dubforge.pipeline import segment_replace as sr


pytestmark = pytest.mark.usefixtures('isolated_env')


def ff(*args):
    result = subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-v', 'error', '-y',
                             '-threads', '2', '-filter_threads', '1', *map(str, args)],
                            capture_output=True, encoding='utf-8', timeout=45)
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture(scope='module')
def media(tmp_path_factory):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('FFmpeg不可用')
    root = tmp_path_factory.mktemp('segment-media')
    base = root / 'base.mp4'
    ff('-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=30:duration=6',
       '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=6',
       '-c:v', 'libx264', '-threads', '2', '-preset', 'fast', '-crf', '21',
       '-pix_fmt', 'yuv420p', '-g', '60', '-keyint_min', '60', '-sc_threshold', '0',
       '-bf', '3', '-x264-params', 'open-gop=0:repeat-headers=1', '-c:a', 'aac', '-t', '6', base)
    return base


class Context:
    def __init__(self):
        self.commands = []
        self.stages = []
        self.cancelled = False

    def check_cancel(self):
        if self.cancelled:
            raise RuntimeError('已取消')

    def run_subprocess(self, args, **kwargs):
        self.check_cancel()
        self.commands.append(args)
        result = subprocess.run(args, cwd=kwargs.get('cwd'), capture_output=True,
                                encoding='utf-8', errors='replace', timeout=45)
        callback = kwargs.get('on_stdout')
        if callback:
            for line in result.stdout.splitlines():
                callback(line)
        return result.returncode, result.stdout, result.stderr

    def report_execution(self, code, label, **fields):
        self.stages.append((code, fields))


def renderer(base, *, seen=None, mismatch=False):
    def render(start, end, info, target):
        if seen is not None:
            seen.append((start, end, info, target))
        args = sr.encoding_args(info, crf=21)
        if mismatch:
            args[args.index('-x264-params') + 1] = args[args.index('-x264-params') + 1].replace('bframes=3', 'bframes=0')
        ff('-ss', start / info['fps'], '-i', base, '-an',
           '-vf', 'drawbox=x=10:y=10:w=60:h=40:color=red:t=fill',
           '-frames:v', end - start, *args, '-muxdelay', '0', '-f', 'mpegts', target)
    return render


def frame_hashes(path):
    raw = ff('-i', path, '-map', '0:v:0', '-an', '-f', 'framehash', '-')
    return [row.split(',')[-1].strip() for row in raw.splitlines() if row and not row.startswith('#')]


def audio_hash(path):
    return ff('-i', path, '-map', '0:a:0', '-c:a', 'copy', '-f', 'hash', '-hash', 'sha256', '-')


def test_inspect_reads_encoder_and_packets_without_decoding(media):
    ctx = Context()
    info = sr.inspect_base(media, ctx=ctx)
    assert info['frame_count'] == 180
    assert info['duration'] == 6
    assert info['fps'] == 30
    assert info['keyframe_frames'] == [0, 60, 120]
    assert info['bframes'] == 3
    assert info['has_b_frames'] == 2
    assert info['has_audio'] is True
    assert info['x264_options']['open_gop'] == '0'
    assert all(Path(command[0]).stem == 'ffprobe' for command in ctx.commands)


def test_plan_outward_frames_and_merge(media):
    info = sr.inspect_base(media)
    result = sr.plan_windows(info, [[0.3, 0.4], {'start': 1.9, 'end': 2.1}, [4.7, 4.8]])
    assert result['expanded_ranges'] == [{'start': 0, 'end': 6}]
    assert result['encoded_frames'] == 180
    assert result['requested_frame_ranges'][0] == dict(start_frame=9, end_frame=12, start=0.3, end=0.4)
    assert result['requested_frame_ranges'][1]['end_frame'] == 63


@pytest.mark.parametrize('ranges', [[], None, [(-1, 1)], [(0, 7)], [(1, 1)], [(2, 1)], [(6.0001, 6.0002)],
                                    [(True, 1)], [('0', 1)], [(float('nan'), 1)],
                                    [(0, float('inf'))], [{}], [[0, 1, 2]]])
def test_plan_rejects_invalid_ranges(ranges):
    with pytest.raises(sr.SegmentReplaceError) as error:
        sr.plan_windows(dict(duration=6, fps=30, frame_count=180, keyframe_frames=[0, 60, 120]), ranges)
    assert error.value.code == 'SEGMENT_REPLACE_RANGES'


def test_partial_copy_preserves_all_untouched_gops_and_original_audio(media, tmp_path):
    out = tmp_path / 'updated.mp4'
    seen, ctx = [], Context()
    result = sr.replace(ctx, media, out, [(2.4, 2.6)], renderer(media, seen=seen))
    assert result['encode_seconds'] == 2
    assert result['frame_count'] == 180
    assert result['validated'] is True
    assert result['audio_mode'] == 'copy'
    assert seen[0][:2] == (60, 120)
    assert isinstance(seen[0][3], Path)
    assert seen[0][2]['requested_frame_ranges'] == [dict(start_frame=72, end_frame=78, start=2.4, end=2.6)]
    original, changed = frame_hashes(media), frame_hashes(out)
    assert len(changed) == len(original) == 180
    assert changed[:60] == original[:60]
    assert changed[120:] == original[120:]
    assert changed[72:78] != original[72:78]
    assert audio_hash(out) == audio_hash(media)
    assert not list(tmp_path.glob('.dubforge-segments-*'))
    assert {'checking_segment_base', 'rendering_segments', 'reusing_segments',
            'joining_segments', 'joining_segment_audio', 'validating_segment_output',
            'segment_output_ready'} <= {code for code, _ in ctx.stages}
    json.dumps(result)


def test_callback_failure_preserves_old_output_and_cleans_temporary(media, tmp_path):
    out = tmp_path / 'old.mp4'
    out.write_bytes('原成片'.encode())
    def fail(*args):
        raise RuntimeError('模拟失败')
    with pytest.raises(RuntimeError, match='模拟失败'):
        sr.replace(Context(), media, out, [(2.4, 2.6)], fail)
    assert out.read_bytes() == '原成片'.encode()
    assert not list(tmp_path.glob('.dubforge-segments-*'))


def test_encoder_mismatch_is_not_silently_retimed(media, tmp_path):
    out = tmp_path / 'old.mp4'
    out.write_bytes(b'old')
    with pytest.raises(sr.SegmentReplaceError) as error:
        sr.replace(Context(), media, out, [(2.4, 2.6)], renderer(media, mismatch=True))
    assert error.value.code == 'SEGMENT_REPLACE_INCOMPATIBLE'
    assert out.read_bytes() == b'old'
    assert not list(tmp_path.glob('.dubforge-segments-*'))


def test_cancel_after_render_does_not_replace_output(media, tmp_path):
    ctx, out = Context(), tmp_path / 'old.mp4'
    out.write_bytes(b'old')
    def cancel(start, end, info, target):
        renderer(media)(start, end, info, target)
        ctx.cancelled = True
    with pytest.raises(RuntimeError, match='已取消'):
        sr.replace(ctx, media, out, [(2.4, 2.6)], cancel)
    assert out.read_bytes() == b'old'
    assert not list(tmp_path.glob('.dubforge-segments-*'))


def test_video_without_audio_is_supported(media, tmp_path):
    silent, out = tmp_path / 'silent.mp4', tmp_path / 'updated.mp4'
    ff('-i', media, '-map', '0:v:0', '-c', 'copy', silent)
    assert sr.inspect_base(silent)['has_audio'] is False
    sr.replace(Context(), silent, out, [(2.4, 2.6)], renderer(silent))
    assert sr.inspect_base(out)['has_audio'] is False


def test_candidate_audio_changes_only_requested_content_and_encodes_once(media, tmp_path):
    candidate, out = tmp_path / 'new.wav', tmp_path / 'updated.mp4'
    ff('-f', 'lavfi', '-i', 'sine=frequency=1100:sample_rate=48000:duration=6', '-ac', '2', '-c:a', 'pcm_f32le', candidate)
    ctx = Context()
    report = sr.replace(ctx, media, out, [(2.4, 2.6)], renderer(media), audio_candidate=candidate)
    assert report['audio_mode'] == 'replace_ranges'
    assert sr.inspect_base(out)['audio_duration'] == pytest.approx(6, abs=0.01)
    assert sum('-c:a' in cmd and cmd[cmd.index('-c:a') + 1] == 'aac' for cmd in ctx.commands) == 1
    # 比较频率主峰，验证范围外依然使用440Hz、范围内使用1100Hz。
    import numpy as np
    def dominant(start, end):
        target = tmp_path / 'chunk.wav'
        ff('-ss', start, '-i', out, '-t', end-start, '-ac', '1', '-ar', '48000', '-c:a', 'pcm_f32le', target)
        import soundfile as sf
        values, rate = sf.read(target)
        frequencies = np.fft.rfftfreq(len(values), 1 / rate)
        return frequencies[np.argmax(np.abs(np.fft.rfft(values)))]
    assert dominant(1, 1.2) == pytest.approx(440, abs=10)
    assert dominant(2.43, 2.57) == pytest.approx(1100, abs=12)
    assert dominant(4, 4.2) == pytest.approx(440, abs=10)


def test_reject_short_non_pcm_candidate(media, tmp_path):
    candidate = tmp_path / 'too-short.wav'
    ff('-f', 'lavfi', '-i', 'sine=duration=1', '-c:a', 'pcm_s16le', candidate)
    with pytest.raises(sr.SegmentReplaceError) as error:
        sr.replace(Context(), media, tmp_path / 'new.mp4', [(2.4, 2.6)], renderer(media), audio_candidate=candidate)
    assert error.value.code == 'SEGMENT_REPLACE_AUDIO'


def test_audio_crossfade_is_sample_smooth_and_does_not_change_outside_range(tmp_path):
    import numpy as np
    import soundfile as sf
    old, new, result = (tmp_path / name for name in ('old.wav', 'new.wav', 'smooth.wav'))
    ff('-f', 'lavfi', '-i', 'aevalsrc=0.5:s=48000:d=0.2', '-ac', '2', '-c:a', 'pcm_f32le', old)
    ff('-f', 'lavfi', '-i', 'aevalsrc=-0.5:s=48000:d=0.2', '-ac', '2', '-c:a', 'pcm_f32le', new)
    ff('-i', old, '-i', old, '-i', new, '-filter_complex',
       sr._audio_filter([dict(start=.05, end=.15)], .2), '-map', '[a]', '-c:a', 'pcm_f32le', result)
    values, rate = sf.read(result, dtype='float32')
    original, _ = sf.read(old, dtype='float32')
    candidate, _ = sf.read(new, dtype='float32')
    assert rate == 48000
    assert len(values) == 9600
    assert np.max(np.abs(values[:2400] - original[:2400])) < 1e-6
    assert np.max(np.abs(values[7200:] - original[7200:])) < 1e-6
    assert np.max(np.abs(values[3000:6600] - candidate[3000:6600])) < 1e-6
    assert np.max(np.abs(np.diff(values, axis=0))) < .005


def test_reject_open_gop(tmp_path):
    base = tmp_path / 'open.mp4'
    ff('-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=30:duration=4', '-c:v', 'libx264',
       '-threads', '2', '-preset', 'fast', '-g', '30', '-bf', '3', '-x264-params', 'open-gop=1', base)
    with pytest.raises(sr.SegmentReplaceError) as error:
        sr.inspect_base(base)
    assert error.value.code == 'SEGMENT_REPLACE_UNSUPPORTED'
    assert '完整重制' in error.value.message


def test_nonzero_origin_is_rejected(media, tmp_path):
    base = tmp_path / 'offset.mp4'
    ff('-i', media, '-c', 'copy', '-output_ts_offset', '1', base)
    with pytest.raises(sr.SegmentReplaceError, match='不从零开始'):
        sr.inspect_base(base)


def test_failure_even_when_ffmpeg_returns_zero_on_timestamp_warning():
    class WarningContext(Context):
        def run_subprocess(self, args, **kwargs):
            return 0, '', 'Non-monotonic DTS; changing to 123'
    with pytest.raises(sr.SegmentReplaceError) as error:
        sr._run(WarningContext(), ['ffmpeg'])
    assert error.value.code == 'SEGMENT_REPLACE_TIMESTAMPS'


def test_fractional_fps_keeps_exact_frame_count_and_pts(tmp_path):
    base, out = tmp_path / 'fractional.mp4', tmp_path / 'updated.mp4'
    ff('-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=24000/1001:duration=6.006',
       '-frames:v', '144', '-c:v', 'libx264', '-threads', '2', '-preset', 'fast', '-crf', '21',
       '-pix_fmt', 'yuv420p', '-g', '48', '-keyint_min', '48', '-sc_threshold', '0', '-bf', '3',
       '-x264-params', 'open-gop=0:repeat-headers=1', base)
    info = sr.inspect_base(base)
    assert (info['fps_num'], info['fps_den']) == (24000, 1001)
    report = sr.replace(Context(), base, out, [(2.4, 2.6)], renderer(base))
    assert report['frame_count'] == 144
    final = sr.inspect_base(out)
    assert final['duration'] == pytest.approx(6.006)
    original, changed = frame_hashes(base), frame_hashes(out)
    assert changed[:48] == original[:48]
    assert changed[96:] == original[96:]


def test_vfr_is_rejected(tmp_path):
    base = tmp_path / 'vfr.mp4'
    ff('-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=30:duration=3',
       '-vf', 'select=not(eq(n\\,40))', '-fps_mode', 'vfr', '-c:v', 'libx264',
       '-threads', '2', '-preset', 'fast', '-pix_fmt', 'yuv420p', '-bf', '3',
       '-x264-params', 'open-gop=0', base)
    with pytest.raises(sr.SegmentReplaceError) as error:
        sr.inspect_base(base)
    assert error.value.code == 'SEGMENT_REPLACE_UNSUPPORTED'


def test_inspection_cache_uses_stat_and_returns_independent_values(media):
    info = sr.inspect_base(media)
    info['keyframe_frames'].clear()
    ctx = Context()
    cached = sr.inspect_base(media, ctx=ctx)
    assert cached['keyframe_frames'] == [0, 60, 120]
    assert ctx.commands == []
    ctx.cancelled = True
    with pytest.raises(RuntimeError, match='已取消'):
        sr.inspect_base(media, ctx=ctx)


def test_subframe_range_always_selects_at_least_one_frame():
    info = dict(duration=6, fps=30, frame_count=180, keyframe_frames=[0, 60, 120])
    plan = sr.plan_windows(info, [(1, 1 + 1e-10), (6 - 1e-10, 6)])
    assert plan['requested_frame_ranges'] == [dict(start_frame=30, end_frame=31, start=1, end=31/30),
                                              dict(start_frame=179, end_frame=180, start=179/30, end=6)]
