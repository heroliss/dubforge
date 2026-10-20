"""真实剪辑尾帧回归：保留 CFR/VFR 时间点、B 帧及完整声音终点。"""
import json
from fractions import Fraction

import pytest

from dubforge.pipeline import output_cuts, step_assemble
from test_original_audio_copy import _context, _run, _mux_command


def _probe(path):
    return json.loads(_run(['ffprobe', '-v', 'error', '-count_frames',
        '-show_entries', 'stream=index,codec_type,time_base,duration,nb_frames,nb_read_frames,has_b_frames',
        '-of', 'json', str(path)]).stdout)['streams']


def _packets(path):
    return json.loads(_run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
        '-show_packets', '-show_entries', 'packet=pts_time,dts_time,duration_time,flags',
        '-of', 'json', str(path)]).stdout)['packets']


def _media(root, kind):
    rate = '30000/1001' if kind == 'ntsc' else '20' if kind == 'vfr' else '10'
    total = 6.006 if kind == 'ntsc' else 6.
    left, right = (2.002, 3.003) if kind == 'ntsc' else (2., 3.)
    source = root / 'source.mp4'
    command = ['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
        f'testsrc2=s=160x96:r={rate}:d={total}', '-f', 'lavfi', '-i',
        f'sine=frequency=440:sample_rate=32000:duration={total}']
    if kind == 'vfr':
        # 前半段 10 fps，后半段每 0.15 秒一帧；不能靠 r_frame_rate 猜测 CFR。
        command += ['-vf', "select='if(lt(t,3),not(mod(n,2)),not(mod(n,3)))'", '-fps_mode', 'vfr']
    command += ['-c:v', 'libx264', '-threads', '2', '-c:a', 'aac', str(source)]
    _run(command)
    plan = dict(schema=1, source_duration=total, output_duration=round(total - right + left, 6),
                ranges=[dict(start=left, end=right)])
    return source, total, plan


@pytest.mark.parametrize('kind', ['cfr', 'ntsc', 'vfr'])
@pytest.mark.parametrize('dubbed', [False, True], ids=['original', 'dubbed'])
def test_cut_mux_preserves_all_frames_pts_and_audio_end(isolated_env, tmp_path, monkeypatch, kind, dubbed):
    source, total, plan = _media(tmp_path, kind)
    ctx, commands, _ = _context(tmp_path, monkeypatch)
    ctx.task_data['output_cuts'] = plan
    target = tmp_path / 'output' / 'cut.mp4'
    target.parent.mkdir()
    if dubbed:
        # 此回归专测最后的混音封装；铺轨另有集成测试，不启动真实模型服务。
        monkeypatch.setattr(step_assemble, '_mixed_audio', lambda *_args, **_kwargs: (
            ['ffmpeg', '-v', 'error', '-y', '-i', str(source)], ['[0:a:0]anull[mixed]'], '[mixed]'))
        step_assemble._mux(ctx, tmp_path, source, source, target, total)
    else:
        step_assemble._mux_original_sub(ctx, source, target, total, (0, 99), burn=False)

    command = _mux_command(commands)
    assert '-shortest' not in command
    assert '-r' not in command  # 不能重采样 VFR 以掩盖尾帧问题。
    assert '-bsf:v' in command
    streams = _probe(target)
    video = next(row for row in streams if row['codec_type'] == 'video')
    audio = next(row for row in streams if row['codec_type'] == 'audio')
    actual = sorted(_packets(target), key=lambda row: float(row['pts_time']))
    left, right = plan['ranges'][0]['start'], plan['ranges'][0]['end']
    expected = sorted(float(row['pts_time']) - (right - left if float(row['pts_time']) >= right else 0)
                      for row in _packets(source) if not left <= float(row['pts_time']) < right)
    tick = float(Fraction(video['time_base']))
    assert len(actual) == len(expected)
    assert int(video['nb_frames']) == int(video['nb_read_frames']) == len(expected)
    assert video['has_b_frames'] > 0
    assert all('D' not in row['flags'] for row in actual)
    assert [float(row['pts_time']) for row in actual] == pytest.approx(expected, abs=max(2 * tick, 2e-6))
    assert float(video['duration']) == pytest.approx(plan['output_duration'], abs=2 * tick)
    assert float(audio['duration']) == pytest.approx(plan['output_duration'], abs=.0011)
    # 解码经过真实最终 MP4 的 edit list，不只数封装中的 packet。
    _run(['ffmpeg', '-v', 'error', '-i', str(target), '-f', 'null', '-'])


def test_cut_timing_options_reject_invalid_endpoint():
    for duration in (0., -1., float('nan'), float('inf'), True):
        with pytest.raises(output_cuts.OutputCutsError):
            output_cuts.mux_timing_options(duration)


def test_cut_preview_uses_its_own_retained_endpoint(isolated_env, tmp_path, monkeypatch):
    source, _, plan = _media(tmp_path, 'cfr')
    ctx, commands, _ = _context(tmp_path, monkeypatch)
    ctx.task_data['output_cuts'] = plan
    target = tmp_path / 'preview.mp4'
    step_assemble._mux_original_sub(ctx, source, target, 4.5, (0, 99), burn=False, start=1.)
    video = next(row for row in _probe(target) if row['codec_type'] == 'video')
    assert int(video['nb_read_frames']) == int(video['nb_frames']) == 35
    assert float(video['duration']) == pytest.approx(3.5, abs=float(Fraction(video['time_base'])))
    command = _mux_command(commands)
    assert command[command.index('-t', command.index('-c:v')) + 1] == '3.500000'
    assert all('D' not in row['flags'] for row in _packets(target))


def test_cut_silent_video_keeps_its_last_frame(isolated_env, tmp_path, monkeypatch):
    source = tmp_path / 'silent.mp4'
    _run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=s=160x96:r=10:d=6',
          '-c:v', 'libx264', '-threads', '2', str(source)])
    ctx, _, _ = _context(tmp_path, monkeypatch)
    ctx.task_data['output_cuts'] = dict(schema=1, source_duration=6., output_duration=5.,
                                      ranges=[dict(start=2., end=3.)])
    target = tmp_path / 'silent-cut.mp4'
    step_assemble._mux_original_sub(ctx, source, target, 6., (0, 99), burn=False)
    streams = _probe(target)
    assert len(streams) == 1
    assert streams[0]['codec_type'] == 'video'
    assert int(streams[0]['nb_read_frames']) == int(streams[0]['nb_frames']) == 50
    assert float(streams[0]['duration']) == 5.
    assert all('D' not in row['flags'] for row in _packets(target))


def test_cut_fingerprint_revision_only_affects_effective_plan(monkeypatch):
    assert output_cuts.fingerprint(None) is None
    plan = {'ranges': [{'start': 2., 'end': 3.}]}
    before = output_cuts.fingerprint(plan)
    monkeypatch.setattr(output_cuts, 'RENDER_VERSION', output_cuts.RENDER_VERSION + 1)
    assert output_cuts.fingerprint(plan) != before
    assert output_cuts.fingerprint(None) is None
