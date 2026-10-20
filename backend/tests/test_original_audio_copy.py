"""原声成片真实 FFmpeg 回归：画面处理与无损音轨复制独立判定。"""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from dubforge.errors import DubForgeError
from dubforge.pipeline import content_redaction, step_assemble


def _run(command, **kwargs):
    return subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                          errors='replace', check=True, timeout=60, **kwargs)


def _video(root, codec='aac'):
    source = root / ('source.mkv' if codec == 'flac' else 'source.mp4')
    command = ['ffmpeg', '-v', 'error', '-y', '-threads', '1', '-f', 'lavfi', '-i',
               'testsrc2=s=320x180:r=10:d=2.4']
    if codec:
        command += ['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=2.4']
    command += ['-c:v', 'libx264', '-preset', 'ultrafast']
    if codec:
        command += ['-c:a', codec]
    _run([*command, str(source)])
    return source


def _context(root, monkeypatch, *, audio_filter='', **values):
    subtitle = root / 'trans.srt'
    subtitle.write_text('1\n00:00:00,200 --> 00:00:01,800\nTest subtitle\n', encoding='utf-8')
    options = {'assemble.final_loudness_mode': 'off', 'content_review.enabled': False, **values}
    commands, logs, stages = [], [], []

    def run(command, **kwargs):
        commands.append(command)
        on_stdout = kwargs.pop('on_stdout', None)
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                errors='replace', check=False, timeout=60, **kwargs)
        if on_stdout:
            for line in result.stdout.splitlines():
                on_stdout(line)
        return result.returncode, result.stdout, result.stderr

    ctx = SimpleNamespace(pdir=root, task_data={},
                          cfg=lambda key, default=None: options.get(key, default),
                          upstream=lambda *args: subtitle, run_subprocess=run,
                          check_cancel=lambda: None, report_progress=lambda *args: None,
                          log=logs.append, stages=stages,
                          report_execution=lambda code, label, **kwargs: stages.append((code, label)))
    monkeypatch.setattr(content_redaction, 'safe_overlay', lambda ctx, plan: plan)
    monkeypatch.setattr(content_redaction, 'audio_plan', lambda *args, **kwargs: dict(
        background=None, filter=audio_filter, fallback_reason='', ranges=[],
        requested_mode='all_audio', effective_mode='all_audio'))
    return ctx, commands, logs


def _packets(path, stream='a:0'):
    return json.loads(_run(['ffprobe', '-v', 'error', '-select_streams', stream,
        '-show_packets', '-show_data_hash', 'sha256', '-show_entries',
        'packet=data_hash,pts_time,duration_time', '-of', 'json', str(path)]).stdout)['packets']


def _mux_command(commands):
    return next(command for command in commands if '-c:v' in command)


def _decoded_audio_hash(path):
    return _run(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:a:0',
                 '-c:a', 'pcm_s16le', '-f', 'hash', '-hash', 'sha256', 'pipe:1']).stdout


def test_full_subtitle_render_copies_aac_payload_and_timestamps(isolated_env, tmp_path, monkeypatch):
    source = _video(tmp_path)
    ctx, commands, logs = _context(tmp_path, monkeypatch)
    target = tmp_path / 'subtitles.mp4'
    # 正式合成已拿到完整时长，不应再次探测整片时长。
    monkeypatch.setattr(step_assemble, '_ffprobe_duration',
                        lambda *_: pytest.fail('完整成片应复用传入的时长'))
    step_assemble._mux_original_sub(ctx, source, target, 2.4, (0, 99), burn=True)
    command = _mux_command(commands)
    assert command[command.index('-c:a') + 1] == 'copy'
    assert command[command.index('-c:v') + 1] == 'libx264'
    assert _packets(target) == _packets(source)
    # 同时核对实际解码结果，防止复制保留了包却改变 AAC 起始补偿或播放长度。
    assert _decoded_audio_hash(target) == _decoded_audio_hash(source)
    assert _packets(target, 'v:0') != _packets(source, 'v:0')
    assert any('直接复制 AAC' in message for message in logs)
    assert ('encoding', '编码画面，直接复制原声音轨') in ctx.stages
    assert not list(tmp_path.glob('.subtitle_*'))
    assert not list(tmp_path.glob('.tmp_*'))
    assert ctx._audio_report['has_audio']


def test_unchanged_full_media_copies_both_streams(isolated_env, tmp_path, monkeypatch):
    source = _video(tmp_path)
    ctx, commands, _ = _context(tmp_path, monkeypatch)
    target = tmp_path / 'untouched.mp4'
    step_assemble._mux_original_sub(ctx, source, target, 2.4, (0, 99), burn=False)
    command = _mux_command(commands)
    assert command[command.index('-c:a') + 1] == 'copy'
    assert command[command.index('-c:v') + 1] == 'copy'
    assert _packets(target) == _packets(source)
    assert _packets(target, 'v:0') == _packets(source, 'v:0')
    assert ('encoding', '封装成片，直接复制原声音轨') in ctx.stages


@pytest.mark.parametrize('source_codec', ['flac', 'unknown'])
def test_other_or_unknown_audio_codec_is_converted_to_aac(
        isolated_env, tmp_path, monkeypatch, source_codec):
    source = _video(tmp_path, 'flac' if source_codec == 'flac' else 'aac')
    ctx, commands, logs = _context(tmp_path, monkeypatch)
    if source_codec == 'unknown':
        monkeypatch.setattr(step_assemble, '_first_audio_stream', lambda _: {'index': 1})
    target = tmp_path / 'compatible.mp4'
    step_assemble._mux_original_sub(ctx, source, target, 2.4, (0, 99), burn=False)
    command = _mux_command(commands)
    assert command[command.index('-c:a') + 1] == 'aac'
    codec = json.loads(_run(['ffprobe', '-v', 'error', '-select_streams', 'a:0',
        '-show_entries', 'stream=codec_name', '-of', 'json', str(target)]).stdout)
    assert codec['streams'][0]['codec_name'] == 'aac'
    assert _packets(target) != _packets(source)
    assert any('不是已确认可直接复制' in message for message in logs)


@pytest.mark.parametrize('processing', ['mute', 'normalize', 'uniform', 'cut', 'preview'])
def test_audio_processing_and_precise_preview_keep_aac_encoding(
        isolated_env, tmp_path, monkeypatch, processing):
    source = _video(tmp_path)
    options = ({'assemble.final_loudness_mode': processing}
               if processing in {'normalize', 'uniform'} else {})
    ctx, commands, logs = _context(tmp_path, monkeypatch,
        audio_filter="volume=0:enable='between(t,0.5,1.0)'" if processing == 'mute' else '', **options)
    if processing == 'cut':
        ctx.task_data['output_cuts'] = dict(schema=1, source_duration=2.4, output_duration=1.8,
                                          ranges=[dict(start=.8, end=1.4)])
    target = tmp_path / 'processed.mp4'
    step_assemble._mux_original_sub(ctx, source, target, 1.2 if processing == 'preview' else 2.4,
                                   (0, 99), burn=False, start=.4 if processing == 'preview' else None)
    command = _mux_command(commands)
    assert command[command.index('-c:a') + 1] == 'aac'
    assert _packets(target) != _packets(source)
    assert any('编码为 AAC 192 kb/s' in message for message in logs)
    duration = float(_run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                          '-of', 'csv=p=0', str(target)]).stdout)
    expected = 1.2 if processing == 'preview' else 1.8 if processing == 'cut' else 2.4
    assert abs(duration - expected) < .15


def test_silent_source_stays_without_audio_stream(isolated_env, tmp_path, monkeypatch):
    source = _video(tmp_path, None)
    ctx, commands, _ = _context(tmp_path, monkeypatch)
    target = tmp_path / 'silent.mp4'
    step_assemble._mux_original_sub(ctx, source, target, 2.4, (0, 99), burn=True)
    assert '-c:a' not in _mux_command(commands)
    assert not step_assemble._has_audio_stream(target)
    assert ctx._audio_report['has_audio'] is False


@pytest.mark.parametrize('output', ['invalid', '{}', '[]', '{"streams":"unknown"}'])
def test_failed_audio_probe_never_silently_drops_audio(monkeypatch, output):
    monkeypatch.setattr(step_assemble.subprocess, 'run',
                        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=output))
    with pytest.raises(DubForgeError, match='无法读取源视频音轨') as caught:
        step_assemble._first_audio_stream(Path('source.mp4'))
    assert caught.value.code == 'AUDIO_STREAM_PROBE_FAILED'
