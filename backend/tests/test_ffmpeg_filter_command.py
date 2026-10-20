"""滤镜脚本只在运行 FFmpeg 的上下文中存在，不改变滤镜内容。"""
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from dubforge.errors import DubForgeError
from dubforge.utils.ffmpeg import complex_filter_command


def test_short_graph_stays_inline_and_original_command_unchanged(isolated_env):
    command = ['ffmpeg', '-filter_complex', '[0:v]null[v]', '-map', '[v]', 'out.mp4']
    before = list(command)
    with complex_filter_command(command, isolated_env) as prepared:
        assert prepared == command
        assert prepared is not command
        assert not list(isolated_env.glob('.dubforge-filter-*'))
    assert command == before


def test_long_filter_utf8_no_bom_and_cleanup(isolated_env):
    graph = '[0:v]null[v];' * 2500 + '# 中文内容\n'
    command = ['ffmpeg', '-filter_complex', graph]
    with complex_filter_command(command, isolated_env) as prepared:
        assert prepared[1] == '-/filter_complex'
        path = Path(prepared[2])
        assert path.parent == isolated_env
        assert path.read_bytes() == graph.encode('utf-8')
        assert not path.read_bytes().startswith(b'\xef\xbb\xbf')
        assert len(subprocess.list2cmdline(prepared)) < 1000
        assert command[1:] == ['-filter_complex', graph]
    assert not path.exists()


@pytest.mark.parametrize('error', [RuntimeError('cancelled'), KeyboardInterrupt()])
def test_error_or_cancellation_cleans_script(isolated_env, error):
    command = ['ffmpeg', '-filter_complex', 'x' * 33000]
    with pytest.raises(type(error)):
        with complex_filter_command(command, isolated_env) as prepared:
            path = Path(prepared[2])
            assert path.is_file()
            raise error
    assert not path.exists()


def test_script_creation_failure_is_business_error(isolated_env, monkeypatch):
    def fail(*args, **kwargs):
        raise PermissionError('isolated failure')
    monkeypatch.setattr(tempfile, 'NamedTemporaryFile', fail)
    with pytest.raises(DubForgeError) as caught:
        with complex_filter_command(['ffmpeg', '-filter_complex', 'x' * 33000], isolated_env):
            pytest.fail('写入失败时不应执行命令')
    assert caught.value.code == 'FFMPEG_FILTER_SCRIPT_WRITE_FAILED'
    assert not list(isolated_env.glob('.dubforge-filter-*'))


def test_partial_write_failure_cleans_script(isolated_env, monkeypatch):
    create = tempfile.NamedTemporaryFile
    class FailingFile:
        def __init__(self, *args, **kwargs):
            self.handle = create(*args, **kwargs)
            self.name = self.handle.name
        def __enter__(self):
            self.handle.__enter__()
            return self
        def write(self, value):
            self.handle.write(value[:10])
            raise OSError('isolated disk full')
        def __exit__(self, *args):
            return self.handle.__exit__(*args)
    monkeypatch.setattr(tempfile, 'NamedTemporaryFile', FailingFile)
    with pytest.raises(DubForgeError) as caught:
        with complex_filter_command(['ffmpeg', '-filter_complex', 'x' * 33000], isolated_env):
            pass
    assert caught.value.code == 'FFMPEG_FILTER_SCRIPT_WRITE_FAILED'
    assert not list(isolated_env.glob('.dubforge-filter-*'))


def test_concurrent_commands_use_distinct_script_names(isolated_env):
    from threading import Barrier
    barrier = Barrier(2)
    def prepare(_):
        with complex_filter_command(['ffmpeg', '-filter_complex', 'x' * 33000], isolated_env) as command:
            path = Path(command[2])
            barrier.wait(timeout=10)
            assert path.is_file()
            return path
    with ThreadPoolExecutor(max_workers=2) as pool:
        paths = list(pool.map(prepare, range(2)))
    assert paths[0] != paths[1]
    assert not any(path.exists() for path in paths)


def test_threshold_counts_utf16_units(isolated_env):
    # 非 BMP 字符在 Windows 命令行中占两个 UTF-16 单元。
    with complex_filter_command(['ffmpeg', '-filter_complex', '\U0001f600' * 13000], isolated_env) as command:
        assert command[1] == '-/filter_complex'


@pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='本机未安装 FFmpeg')
def test_real_ffmpeg_200_cuts_long_command_starts_and_finishes(isolated_env):
    from dubforge.pipeline.output_cuts import filter_graph
    ranges = [{'start': i * .1 + .04, 'end': i * .1 + .06} for i in range(200)]
    graph, video, audio, _ = filter_graph({'source_duration': 20., 'ranges': ranges}, '0:v:0', '1:a:0')
    command = ['ffmpeg', '-hide_banner', '-v', 'error', '-f', 'lavfi', '-i',
               'testsrc2=s=64x64:r=60:d=20', '-f', 'lavfi', '-i', 'sine=r=48000:d=20',
               '-filter_complex', graph, '-map', video, '-map', audio, '-f', 'null', '-']
    assert len(subprocess.list2cmdline(command)) > 32767
    with complex_filter_command(command, isolated_env) as prepared:
        path = Path(prepared[prepared.index('-/filter_complex') + 1])
        result = subprocess.run(prepared, capture_output=True, timeout=30, check=False)
        assert result.returncode == 0, result.stderr.decode(errors='replace')
        assert path.is_file()
    assert not path.exists()


@pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='本机未安装 FFmpeg')
def test_real_argument_file_keeps_video_and_audio_output(isolated_env, monkeypatch):
    from dubforge.utils import ffmpeg

    folder = isolated_env / "滤镜 文件 ' 有空格"
    folder.mkdir()
    graph = '[0:v]hflip[v];[1:a]volume=0.5[a]'
    command = ['ffmpeg', '-hide_banner', '-v', 'error', '-threads', '1',
               '-filter_complex_threads', '1', '-f', 'lavfi', '-i',
               'testsrc2=s=64x64:r=10:d=0.4', '-f', 'lavfi', '-i', 'sine=r=8000:d=0.4',
               '-filter_complex', graph, '-map', '[v]', '-map', '[a]',
               '-c:v', 'rawvideo', '-c:a', 'pcm_s16le', '-f', 'framemd5', '-']

    def render(prepared):
        result = subprocess.run(prepared, cwd=folder, capture_output=True, timeout=30, check=False)
        assert result.returncode == 0, result.stderr.decode(errors='replace')
        return result.stdout

    reference = render(command)
    monkeypatch.setattr(ffmpeg, '_SCRIPT_THRESHOLD', 0)
    with complex_filter_command(command, folder) as prepared:
        assert '-filter_complex' not in prepared
        assert '-filter_complex_script' not in prepared
        path = Path(prepared[prepared.index('-/filter_complex') + 1])
        assert path.read_bytes() == graph.encode('utf-8')
        assert render(prepared) == reference
    assert not path.exists()

    unchanged = list(command)
    unchanged[unchanged.index('-filter_complex') + 1] = '[0:v]null[v];[1:a]anull[a]'
    assert render(unchanged) != reference
    rows = [line for line in reference.splitlines() if line and not line.startswith(b'#')]
    assert {line.split(b',', 1)[0].strip() for line in rows} == {b'0', b'1'}
