"""资源保护不模拟生产编码；用小样片验证线程选项与错误保留语义。"""
import errno
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from dubforge.errors import DubForgeError
from dubforge.pipeline import render_resources as resources


def _snapshot(**values):
    return dict(output_directory='D:/output', disk_free_bytes=100 * 1024 ** 3,
                physical_available_bytes=4 * 1024 ** 3, commit_available_bytes=8 * 1024 ** 3,
                commit_limit_bytes=64 * 1024 ** 3, decode_threads=2, filter_threads=2,
                encode_threads=4, **values)


def _with_snapshot(monkeypatch, **values):
    snapshot = _snapshot()
    snapshot.update(values)
    monkeypatch.setattr(resources, 'inspect_resources', lambda path: snapshot)
    logs = []
    return SimpleNamespace(check_cancel=lambda: None, log=logs.append), logs, snapshot


def test_input_filter_and_output_threads_are_bounded_without_changing_quality():
    source = ['ffmpeg', '-y', '-ss', '12', '-i', 'source.mp4', '-i', 'voice.wav',
              '-filter_complex', '[1:a]anull[a]', '-map', '0:v', '-map', '[a]',
              '-c:v', 'libx264', '-crf', '18', '-preset', 'medium', 'out.mp4']
    command = resources.bounded_ffmpeg_command(source)
    inputs = [i for i, item in enumerate(command) if item == '-i']
    assert all(command[index - 2:index] == ['-threads:v', '2'] for index in inputs)
    assert command[-3:] == ['-threads:v', '4', 'out.mp4']
    assert command[command.index('-filter_threads') + 1] == '2'
    assert command[command.index('-filter_complex_threads') + 1] == '2'
    assert command[command.index('-crf') + 1] == '18'
    assert command[command.index('-preset') + 1] == 'medium'
    assert source.count('-threads:v') == 0


def test_existing_lower_filter_limit_stays_lower_and_higher_is_capped():
    command = resources.bounded_ffmpeg_command(['ffmpeg', '-filter_threads', '1',
        '-filter_complex_threads', '20', '-i', 'source.mp4', '-c:v', 'copy', 'out.mp4'])
    assert command[command.index('-filter_threads') + 1] == '1'
    assert command[command.index('-filter_complex_threads') + 1] == '2'
    assert command.count('-filter_threads') == command.count('-filter_complex_threads') == 1


def test_preflight_allows_pressure_but_explains_commit_not_only_physical_memory(monkeypatch, tmp_path):
    ctx, logs, snapshot = _with_snapshot(monkeypatch, commit_available_bytes=3 * 1024 ** 3)
    assert resources.preflight_render(ctx, tmp_path) == snapshot
    assert '系统可提交余量 3.00 GiB' in logs[0]
    assert '建议关闭' in logs[0]


def test_preflight_blocks_known_voice_track_disk_requirement(monkeypatch, tmp_path):
    ctx, _, _ = _with_snapshot(monkeypatch, disk_free_bytes=100 * 1024 ** 2)
    with pytest.raises(DubForgeError) as captured:
        resources.preflight_render(ctx, tmp_path, required_bytes=200 * 1024 ** 2)
    assert captured.value.code == 'ASSEMBLE_DISK_SPACE'
    assert captured.value.detail['disk_free_bytes'] == 100 * 1024 ** 2
    assert '清理' in captured.value.suggestion


def test_preflight_only_blocks_near_exhausted_commit_not_normal_pressure(monkeypatch, tmp_path):
    ctx, _, _ = _with_snapshot(monkeypatch, commit_available_bytes=20 * 1024 ** 2)
    with pytest.raises(DubForgeError, match='可提交内存') as captured:
        resources.preflight_render(ctx, tmp_path)
    assert captured.value.code == 'ASSEMBLE_MEMORY_PRESSURE'
    assert '分页文件' in captured.value.suggestion


def test_unknown_resource_values_do_not_block(monkeypatch, tmp_path):
    ctx, logs, _ = _with_snapshot(monkeypatch, disk_free_bytes=None,
        commit_available_bytes=None, commit_limit_bytes=None, physical_available_bytes=None)
    resources.preflight_render(ctx, tmp_path)
    assert '未知' in logs[0]


def test_preflight_checks_cancel_before_any_probe(monkeypatch, tmp_path):
    def cancelled():
        raise DubForgeError('已取消', code='CANCELLED')
    monkeypatch.setattr(resources, 'inspect_resources', lambda path: pytest.fail('取消后不再探测'))
    with pytest.raises(DubForgeError) as captured:
        resources.preflight_render(SimpleNamespace(check_cancel=cancelled), tmp_path)
    assert captured.value.code == 'CANCELLED'


@pytest.mark.parametrize('error,code', [
    ('x264 [error]: malloc of size 39245120 failed', 'ASSEMBLE_MEMORY_PRESSURE'),
    ('Error opening encoder: Cannot allocate memory', 'ASSEMBLE_MEMORY_PRESSURE'),
    (MemoryError(), 'ASSEMBLE_MEMORY_PRESSURE'),
    (OSError(errno.ENOMEM, 'allocation'), 'ASSEMBLE_MEMORY_PRESSURE'),
    ('Error writing trailer: No space left on device', 'ASSEMBLE_DISK_SPACE'),
    (OSError(errno.ENOSPC, 'write'), 'ASSEMBLE_DISK_SPACE'),
    ('Unknown encoder av1_typo', 'REVIEW_RENDER_FFMPEG'),
])
def test_failure_uses_real_evidence_and_keeps_unrelated_error_fallback(error, code):
    failure = resources.ffmpeg_failure(error, fallback_code='REVIEW_RENDER_FFMPEG')
    assert failure.code == code
    if code.startswith('ASSEMBLE_'):
        assert '旧成品保留' in failure.message


def test_snapshot_probes_nearest_existing_output_parent(tmp_path, monkeypatch):
    checked = []
    def usage(path):
        checked.append(path)
        return SimpleNamespace(free=123456)
    monkeypatch.setattr(resources.shutil, 'disk_usage', usage)
    monkeypatch.setattr(resources, '_commit_memory', lambda: (128, 256))
    snapshot = resources.inspect_resources(tmp_path / 'new' / 'output')
    assert checked[0] == tmp_path
    assert snapshot['disk_free_bytes'] == 123456
    assert snapshot['commit_available_bytes'] == 128
    assert snapshot['output_directory'] == str((tmp_path / 'new' / 'output').resolve())
    assert snapshot['system_directory'] and snapshot['temp_directory']


def test_low_system_disk_is_warning_not_output_disk_block(monkeypatch, tmp_path):
    ctx, logs, _ = _with_snapshot(monkeypatch, system_directory='C:/',
        system_disk_free_bytes=130 * 1024 ** 2, temp_directory='C:/Temp', temp_disk_free_bytes=130 * 1024 ** 2)
    resources.preflight_render(ctx, tmp_path)
    assert '合成目录' in logs[0] and '系统盘（C:/）可用 0.13 GiB' in logs[0]
    assert '不据此阻止本次合成' in logs[0]


def test_bounded_command_really_runs_small_mux_without_losing_streams(tmp_path):
    target = tmp_path / 'bounded.mp4'
    command = resources.bounded_ffmpeg_command(['ffmpeg', '-y', '-v', 'error',
        '-f', 'lavfi', '-i', 'color=c=black:s=64x64:r=10:d=0.3',
        '-f', 'lavfi', '-i', 'sine=f=440:d=0.3',
        '-filter_complex', '[0:v]null[v];[1:a]anull[a]', '-map', '[v]', '-map', '[a]',
        '-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-c:a', 'aac', str(target)])
    subprocess.run(command, check=True, capture_output=True, timeout=30)
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type',
        '-of', 'csv=p=0', str(target)], check=True, capture_output=True, text=True, timeout=10)
    assert 'video' in result.stdout and 'audio' in result.stdout


def test_mux_out_of_memory_preserves_old_output_and_removes_incomplete_file(tmp_path, monkeypatch):
    from dubforge.pipeline import step_assemble, content_redaction
    monkeypatch.setattr(step_assemble, 'preflight_render', lambda *args, **kwargs: None)
    monkeypatch.setattr(content_redaction, 'safe_overlay', lambda ctx, overlay: overlay)
    monkeypatch.setattr(step_assemble, 'prepare_filter', lambda *args, **kwargs: ('', None))
    monkeypatch.setattr(step_assemble, '_mixed_audio', lambda *args, **kwargs:
        (['ffmpeg', '-y', '-i', 'input.mp4'], ['[0:a]anull[a]'], '[a]'))
    out = tmp_path / 'output' / 'old.mp4'
    out.parent.mkdir()
    out.write_bytes(b'previous good output')
    def failed(command, **kwargs):
        (kwargs['cwd'] / command[-1]).write_bytes(b'partial encoding')
        return 1, '', 'x264 malloc of size 39245120 failed'
    ctx = SimpleNamespace(cfg=lambda key, default=None: default, task_data={},
        run_subprocess=failed, report_progress=lambda *args: None)
    with pytest.raises(DubForgeError) as captured:
        step_assemble._mux(ctx, tmp_path, tmp_path / 'input.mp4', tmp_path / 'voice.wav', out, total=.3)
    assert captured.value.code == 'ASSEMBLE_MEMORY_PRESSURE'
    assert out.read_bytes() == b'previous good output'
    assert not (out.parent / '.tmp_old.mp4').exists()


def test_preview_voice_cleanup_when_copy_or_normalization_fails(tmp_path, monkeypatch):
    from dubforge.pipeline import step_assemble
    preview = tmp_path / 'preview.wav'
    full = tmp_path / '.tmp_full_preview.wav'
    def failed(ctx, root, total, *, _created, **kwargs):
        _created.append(preview)
        preview.write_bytes(b'generated audio')
        _created.append(full)
        full.write_bytes(b'partial copy')
        ctx._preview_full_voice = full
        raise DubForgeError('测量失败', code='AUDIO_PROCESS_FAILED')
    monkeypatch.setattr(step_assemble, '_mix_timeline_impl', failed)
    ctx = SimpleNamespace()
    with pytest.raises(DubForgeError) as captured:
        step_assemble._mix_timeline(ctx, tmp_path, 100, start=0, end=10)
    assert captured.value.code == 'AUDIO_PROCESS_FAILED'
    assert not preview.exists() and not full.exists()
    assert not hasattr(ctx, '_preview_full_voice')


@pytest.mark.parametrize('name,should_remain', [('.tmp_voice_failed.wav', False), ('dub_full.wav', True)])
def test_full_voice_failure_cleans_unreturned_task_temp_but_keeps_named_audio_cache(tmp_path, monkeypatch, name, should_remain):
    from dubforge.pipeline import step_assemble, step_dubtask, dubbing_edit, audio_quality
    monkeypatch.setattr(step_dubtask, 'build_task', lambda ctx: {'lines': []})
    monkeypatch.setattr(step_assemble, '_require_dubbing', lambda ctx: None)
    monkeypatch.setattr(step_assemble, 'preflight_render', lambda *args, **kwargs: None)
    monkeypatch.setattr(dubbing_edit, 'production_lines', lambda *args: [])
    def place(ctx, *args):
        ctx._voice_layout = {'timing': {'enabled': False}}
        return []
    monkeypatch.setattr(step_assemble, '_apply_adaptive_voice_timing', place)
    def failed(path, ctx):
        assert path.is_file(), '先真实写出完整人声，重现函数返回前失败的边界'
        raise DubForgeError('完整人声测量失败', code='AUDIO_MEASUREMENT_FAILED')
    monkeypatch.setattr(audio_quality, 'normalize_voice', failed)
    ctx = SimpleNamespace(project=None, cfg=lambda key, default=None: default,
                          check_cancel=lambda: None, log=lambda value: None)
    with pytest.raises(DubForgeError) as captured:
        step_assemble._mix_timeline(ctx, tmp_path, .3, output_name=name, register=False)
    assert captured.value.code == 'AUDIO_MEASUREMENT_FAILED'
    assert (tmp_path / 'audio' / name).exists() is should_remain
    assert not list((tmp_path / 'audio').glob('*.tmp'))
