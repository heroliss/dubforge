"""离线审查下载流描述：实际模板投影、未知格式兜底和诊断数据边界。"""
import json

import pytest
from yt_dlp import YoutubeDL

from dubforge.execution_progress import Recorder
from dubforge.pipeline import step_download as download


pytestmark = pytest.mark.usefixtures('isolated_env')


def _template():
    class Settings:
        def cfg(self, key):
            return {}

    cmd = download._ytdlp_cmd(Settings(), 'https://example.test/video', 'video.%(ext)s')
    return next(cmd[index + 1].removeprefix('before_dl:DUBFORGE_PLAN:')
                for index, arg in enumerate(cmd[:-1]) if arg == '--print'
                and cmd[index + 1].startswith('before_dl:DUBFORGE_PLAN:'))


@pytest.mark.parametrize('info', [
    {'format_id': 'single-new-id', 'vcodec': 'avc1.640028', 'acodec': 'mp4a.40.2',
     'width': 1920, 'height': 1080, 'filesize': 100},
    {'requested_formats': [
        {'format_id': 'video-new-id', 'vcodec': 'av01.0.12M.08', 'acodec': 'none', 'filesize': 80},
        {'format_id': 'audio-new-id', 'vcodec': 'none', 'acodec': 'opus', 'filesize': 20}]},
])
def test_real_ytdlp_template_handles_separate_and_single_streams_without_private_fields(info):
    private = {'url': 'https://media.test/stream?signature=PRIVATE',
               'http_headers': {'Authorization': 'Bearer PRIVATE'},
               'cookies': 'PRIVATE'}
    info = {**info, **private}
    for row in info.get('requested_formats', []):
        row.update(private)
    ydl = YoutubeDL({'quiet': True, 'cachedir': False}, auto_init=False)
    payload = ydl.evaluate_outtmpl(_template(), info)
    assert 'PRIVATE' not in payload
    plan = download._stream_plan(payload)
    assert [row['format_id'] for row in plan] == [
        row['format_id'] for row in info.get('requested_formats', [info])]
    assert all(set(row) <= set(download._STREAM_META_FIELDS) for row in plan)


@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf'), 'nan',
                                   'inf', -1, True, {}, [], 10 ** 1000])
def test_invalid_optional_stream_values_do_not_abort_or_invent_quality(value):
    info = {field: value for field in ('width', 'height', 'fps', 'abr')}
    info.update(vcodec='av01.0.12M.08', acodec='none')
    assert download._stream_description(info) == 'AV1 视频流'
    if not isinstance(value, str):
        assert download._stream_description({**info, 'vcodec': value, 'acodec': value}) is None


def test_stream_changes_clear_previous_label_and_diagnostics_never_hold_private_source_metadata(monkeypatch):
    class Context:
        now = 0

        def __init__(self):
            self.events = []
            self.logs = []
            self.recorder = Recorder(clock=lambda: self.now)

        def active_seconds(self):
            return self.now

        def set_eta_provider(self, provider, *, scope='step'):
            pass

        def set_progress_provider(self, provider):
            pass

        def report_progress(self, value):
            pass

        def log(self, message):
            self.logs.append(message)

        def report_execution(self, code, label, **fields):
            metrics = {key: value for key, value in fields.items()
                       if key not in {'detail', 'state', 'completed', 'total', 'unit'}}
            self.events.append(self.recorder.phase(code, label, metrics=metrics))

        def run_subprocess(self, cmd, *, on_stdout, on_stderr):
            plan = [
                {'format_id': 'new-video', 'filesize': 80, 'vcodec': 'vp09.00.51.08',
                 'acodec': 'none', 'width': 1920, 'height': 1080},
                {'format_id': 'new-audio', 'filesize': 20, 'vcodec': 'none',
                 'acodec': 'opus', 'abr': 128}]
            for row in plan:
                row.update(url='https://media.test/path?signature=PRIVATE',
                           http_headers={'Authorization': 'Bearer PRIVATE'})
            on_stdout('DUBFORGE_PLAN:' + json.dumps(plan))
            for key, done, total in [('new-video', 80, 80), ('new-audio', 20, 20),
                                     ('unknown-new-id', 1, 1)]:
                self.now += 1
                on_stderr('DUBFORGE_BYTES:' + json.dumps(key) + '\t' + json.dumps(
                    {'downloaded_bytes': done, 'total_bytes': total}))
            return 0, 'D:/video.mp4\n测试视频\n', ''

    monkeypatch.setattr(download, '_ytdlp_cmd', lambda *args: [])
    ctx = Context()
    assert download._run_ytdlp(ctx, 'https://example.test/video', 'unused')['filepath'] == 'D:/video.mp4'
    snapshots = [row['metrics'] for row in ctx.events if row['phase'] == 'downloading']
    assert [row['current_stream'] for row in snapshots] == [
        '1080p · VP9 视频流', 'Opus 音频流 · 128 kbps', None]
    assert snapshots[-1]['current_format'] == 'unknown-new-id'
    assert 'PRIVATE' not in json.dumps(ctx.events + ctx.logs)
