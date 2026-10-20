"""真实剪辑尾帧回归：保留 CFR/VFR 时间点、B 帧及完整声音终点。"""
import json
import struct
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


def _mp4_video_timing(path):
    """直接核验 MP4 的解码、展示及 edit list 尾端，不依赖 ffprobe 是否暴露包时长。"""
    data = path.read_bytes()

    def children(start, end):
        result = []
        while start + 8 <= end:
            size, name = struct.unpack_from('>I4s', data, start)
            header = 8
            if size == 1:
                size = struct.unpack_from('>Q', data, start + 8)[0]
                header = 16
            elif size == 0:
                size = end - start
            assert size >= header and start + size <= end
            result.append((name, start + header, start + size))
            start += size
        return result

    def child(rows, name):
        return next((left, right) for tag, left, right in rows if tag == name)

    def timing(point):
        offset = point + (20 if data[point] else 12)
        scale = struct.unpack_from('>I', data, offset)[0]
        ticks = struct.unpack_from('>Q' if data[point] else '>I', data, offset + 4)[0]
        return scale, ticks

    moov = children(*child(children(0, len(data)), b'moov'))
    movie_scale, _ = timing(child(moov, b'mvhd')[0])
    for tag, left, right in moov:
        if tag != b'trak':
            continue
        trak = children(left, right)
        mdia = children(*child(trak, b'mdia'))
        handler = child(mdia, b'hdlr')[0]
        if data[handler + 8:handler + 12] != b'vide':
            continue
        scale, duration = timing(child(mdia, b'mdhd')[0])
        media = children(*child(mdia, b'minf'))
        table = children(*child(media, b'stbl'))
        stts = child(table, b'stts')[0]
        count = struct.unpack_from('>I', data, stts + 4)[0]
        durations = [delta for index in range(count)
                     for length, delta in [struct.unpack_from('>II', data, stts + 8 + index * 8)]
                     for _ in range(length)]
        offsets = [0] * len(durations)
        if any(name == b'ctts' for name, _, _ in table):
            ctts = child(table, b'ctts')[0]
            count = struct.unpack_from('>I', data, ctts + 4)[0]
            offsets = [offset for index in range(count)
                       for length, offset in [struct.unpack_from('>Ii' if data[ctts] else '>II',
                                                                  data, ctts + 8 + index * 8)]
                       for _ in range(length)]
        assert len(offsets) == len(durations)
        edts = children(*child(trak, b'edts'))
        elst = child(edts, b'elst')[0]
        assert struct.unpack_from('>I', data, elst + 4)[0] == 1
        edit_duration, media_time, rate_i, rate_f = struct.unpack_from(
            '>Qqhh' if data[elst] else '>Iihh', data, elst + 8)
        assert (rate_i, rate_f) == (1, 0)
        cursor, endpoint = 0, 0
        for delta, offset in zip(durations, offsets):
            endpoint = max(endpoint, cursor + offset + delta)
            cursor += delta
        return dict(header=duration / scale, decode=cursor / scale,
                    presentation=(endpoint - media_time) / scale,
                    edit=edit_duration / movie_scale, frames=len(durations))
    pytest.fail('成片缺少视频轨')


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


def _check_cut_mux(tmp_path, monkeypatch, kind, dubbed):
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
    # mdhd 可能采用解码总时长，仍须单独验证展示尾端，不能让 edit list 静默截短。
    timing = _mp4_video_timing(target)
    assert timing['frames'] == len(expected)
    for endpoint in ('header', 'decode', 'presentation'):
        assert timing[endpoint] == pytest.approx(plan['output_duration'], abs=2 * tick)
    assert timing['edit'] == pytest.approx(plan['output_duration'], abs=.0011)
    # 解码经过真实最终 MP4 的 edit list，不只数封装中的 packet。
    _run(['ffmpeg', '-v', 'error', '-i', str(target), '-f', 'null', '-'])


@pytest.mark.parametrize('kind', ['cfr', 'ntsc', 'vfr'])
@pytest.mark.parametrize('dubbed', [False, True], ids=['original', 'dubbed'])
def test_cut_mux_preserves_all_frames_pts_and_audio_end(isolated_env, tmp_path, monkeypatch, kind, dubbed):
    _check_cut_mux(tmp_path, monkeypatch, kind, dubbed)


@pytest.mark.parametrize('dubbed', [False, True], ids=['original', 'dubbed'])
def test_cut_mux_completes_endpoint_despite_nonzero_packet_duration(isolated_env, tmp_path, monkeypatch, dubbed):
    # 部分 FFmpeg/编码器会给 concat 输出填入非零包时长；它不能代表计划中的尾端。
    # 注入真实 bitstream filter，让旧版 FFmpeg 也能覆盖这一条件，而非依赖当前安装版本。
    timing_options = output_cuts.mux_timing_options

    def nonzero_packet_duration(duration):
        options = timing_options(duration)
        options[1] = r'setts=pts=PTS:dts=DTS:duration=max(DURATION\,round(0.05/TB)),' + options[1]
        return options

    monkeypatch.setattr(output_cuts, 'mux_timing_options', nonzero_packet_duration)
    _check_cut_mux(tmp_path, monkeypatch, 'vfr', dubbed)


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
