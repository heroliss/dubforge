"""下载流说明来自真实元数据；隔离网络，验证 yt-dlp 模板和旧进度协议。"""
import json
from types import SimpleNamespace

import pytest
from yt_dlp import YoutubeDL

from dubforge.pipeline import step_download as download


pytestmark = pytest.mark.usefixtures('isolated_env')


@pytest.mark.parametrize(('meta', 'expected'), [
    ({'width': 3840, 'height': 2160, 'fps': 60, 'vcodec': 'av01.0.12M.08', 'acodec': 'none'},
     '4K · 60 fps · AV1 视频流'),
    ({'width': 7680, 'height': 4320, 'fps': 59.94, 'vcodec': 'av01', 'acodec': 'none'},
     '8K · 59.94 fps · AV1 视频流'),
    ({'width': 1920, 'height': 1080, 'vcodec': 'vp9', 'acodec': 'none'}, '1080p · VP9 视频流'),
    ({'width': 1080, 'height': 1920, 'vcodec': 'avc1.640028', 'acodec': 'none'},
     '1080×1920 · H.264 视频流'),
    ({'width': 1080, 'height': 1080, 'vcodec': 'vp09.00.51.08', 'acodec': 'none'},
     '1080×1080 · VP9 视频流'),
    ({'height': 2160, 'vcodec': 'av01', 'acodec': 'none'}, '高 2160 像素 · AV1 视频流'),
    ({'width': 3000, 'height': 2160, 'vcodec': 'av01', 'acodec': 'none'}, '2160p · AV1 视频流'),
    ({'vcodec': 'none', 'acodec': 'opus', 'abr': 128}, 'Opus 音频流 · 128 kbps'),
    ({'vcodec': 'avc1.64001f', 'acodec': 'mp4a.40.2'}, 'H.264 + AAC 音视频流'),
    ({'vcodec': 'hvc1.1.6.L120', 'acodec': 'none'}, 'HEVC 视频流'),
    ({'vcodec': 'future_codec.2', 'acodec': 'none'}, 'future_codec.2 视频流'),
    ({'format_id': '401'}, None),
    ({'format_id': '616', 'width': 1920, 'height': 1080, 'format_note': 'Premium'}, None),
    ({'width': 1920, 'height': 1080, 'ext': 'mp4'}, None),
    ({'vcodec': 'none', 'acodec': 'none'}, None),
    ({'vcodec': 'unknown', 'acodec': 'N/A'}, None),
    ({'vcodec': 'https://signed.example/media?token=secret', 'acodec': {'bad': 'value'}}, None),
])
def test_stream_description_uses_real_metadata(meta, expected):
    assert download._stream_description(meta) == expected


@pytest.mark.parametrize('bad', [None, '', 'nan', float('nan'), float('inf'), -1, 0, True, [], {}])
def test_invalid_optional_numbers_are_omitted(bad):
    assert download._stream_description({
        'vcodec': 'avc1', 'acodec': 'mp4a', 'width': bad, 'height': bad,
        'fps': bad, 'abr': bad,
    }) == 'H.264 + AAC 音视频流'


def test_out_of_range_or_fractional_dimensions_do_not_create_false_resolution():
    assert download._stream_description({
        'width': 1920.5, 'height': 1080.5, 'fps': 1001,
        'vcodec': 'vp9', 'acodec': 'none',
    }) == 'VP9 视频流'
    assert download._stream_description({
        'width': 10 ** 1000, 'height': 10 ** 1000, 'fps': 10 ** 1000,
        'vcodec': 'vp9', 'acodec': 'none',
    }) == 'VP9 视频流'


def test_format_id_and_untrusted_notes_never_determine_quality():
    base = {'width': 1280, 'height': 720, 'vcodec': 'avc1', 'acodec': 'none'}
    assert download._stream_description({**base, 'format_id': '401', 'format_note': 'Premium HDR 4K'}) == (
        download._stream_description({**base, 'format_id': '616'})
    ) == '720p · H.264 视频流'


def _templates():
    ctx = SimpleNamespace(cfg=lambda section: {})
    cmd = download._ytdlp_cmd(ctx, 'https://example.com/video', 'video.%(ext)s')
    plan = next(cmd[index + 1].removeprefix('before_dl:')
                for index, arg in enumerate(cmd[:-1])
                if arg == '--print' and cmd[index + 1].startswith('before_dl:'))
    progress = cmd[cmd.index('--progress-template') + 1].removeprefix('download:')
    return plan, progress


@pytest.mark.parametrize('separate', [True, False])
def test_real_installed_ytdlp_template_supports_split_and_single_stream(separate):
    plan_template, progress_template = _templates()
    selected = {
        'format_id': 'custom-video', 'filesize': 800, 'width': 3840, 'height': 2160,
        'fps': 60, 'vcodec': 'av01.0.12M.08', 'acodec': 'none', 'ext': 'mp4',
        'format_note': '2160p', 'url': 'https://media.example/video?signature=secret',
        'http_headers': {'Authorization': 'Bearer private'},
    }
    audio = {'format_id': 'custom-audio', 'filesize': 200, 'vcodec': 'none', 'acodec': 'opus', 'abr': 128}
    single = {**selected, 'format_id': 'combined', 'vcodec': 'avc1', 'acodec': 'mp4a.40.2'}
    info = {'format_id': 'custom-video+custom-audio', 'requested_formats': [selected, audio]} if separate else single
    with YoutubeDL({'quiet': True}) as ydl:
        line = ydl.evaluate_outtmpl(plan_template, info)
        rows = download._stream_plan(line.removeprefix('DUBFORGE_PLAN:'))
        assert [row['format_id'] for row in rows] == (
            ['custom-video', 'custom-audio'] if separate else ['combined'])
        assert 'secret' not in line and 'private' not in line
        assert 'url' not in line and 'http_headers' not in line
        assert all(set(row) <= set(download._STREAM_META_FIELDS) for row in rows)
        if separate:
            assert download._stream_description(rows[0]) == '4K · 60 fps · AV1 视频流'
            assert download._stream_description(rows[1]) == 'Opus 音频流 · 128 kbps'
        else:
            assert download._stream_description(rows[0]) == '4K · 60 fps · H.264 + AAC 音视频流'
        # 使用实际 yt-dlp 模板引擎证明旧字节回调仍是编号 + JSON，不受资料扩展影响。
        progress = ydl.evaluate_outtmpl(progress_template, {
            'info': selected, 'progress': {'status': 'downloading', 'downloaded_bytes': 400, 'total_bytes': 800}})
        identity, values = progress.removeprefix('DUBFORGE_BYTES:').split('\t', 1)
        assert json.loads(identity) == 'custom-video'
        assert json.loads(values)['downloaded_bytes'] == 400
        assert 'url' not in progress and 'secret' not in progress


def test_plan_parser_removes_non_whitelisted_fields_and_ignores_invalid_rows():
    rows = download._stream_plan(json.dumps([
        None, 123, {}, {'format_id': None},
        {'format_id': '251', 'vcodec': 'none', 'acodec': 'opus',
         'url': 'private', 'http_headers': {'Cookie': 'private'}, 'cookies': 'private'},
    ]))
    assert rows == [{'format_id': '251', 'vcodec': 'none', 'acodec': 'opus'}]


@pytest.mark.parametrize('channel', ['stdout', 'stderr'])
def test_progress_matches_current_stream_and_clears_unknown_stream(monkeypatch, channel):
    snapshots, percentages = [], []
    ctx = SimpleNamespace(
        cfg=lambda section: {}, log=lambda text: None, active_seconds=lambda: 10,
        report_progress=percentages.append,
        report_execution=lambda phase, label, **values: snapshots.append({'phase': phase, **values}),
    )
    plan = [
        {'format_id': '401', 'filesize': 800, 'width': 1920, 'height': 1080,
         'vcodec': 'vp9', 'acodec': 'none', 'url': 'signed-secret'},
        {'format_id': '251', 'filesize': 200, 'vcodec': 'none', 'acodec': 'opus', 'abr': 128},
        {'format_id': 'unknown-plan', 'filesize': 50},
    ]

    def run(cmd, *, on_stdout, on_stderr):
        callback = on_stdout if channel == 'stdout' else on_stderr
        callback('DUBFORGE_PLAN:' + json.dumps(plan))
        for identity, done, total in [('401', 400, 800), ('251', 100, 200),
                                      ('unknown-plan', 25, 50), ('unplanned', 5, 10)]:
            callback('DUBFORGE_BYTES:' + json.dumps(identity) + '\t' + json.dumps({
                'status': 'downloading', 'downloaded_bytes': done, 'total_bytes': total}))
        return 0, 'D:/video.mp4\n测试标题\n', ''

    ctx.run_subprocess = run
    result = download._run_ytdlp(ctx, 'https://example.com/video', 'video.%(ext)s')
    records = [row for row in snapshots if row['phase'] == 'downloading']
    assert [row['current_format'] for row in records] == ['401', '251', 'unknown-plan', 'unplanned']
    assert [row['current_stream'] for row in records] == [
        '1080p · VP9 视频流', 'Opus 音频流 · 128 kbps', None, None]
    assert records[1]['completed'] == 500 and records[1]['total'] == 1050
    assert all('current_stream' in row for row in records)  # 显式 None 清空同阶段上一个流的说明。
    assert 'signed-secret' not in json.dumps(snapshots)
    assert result == {'filepath': 'D:/video.mp4', 'title': '测试标题', 'source_meta': {}}
    assert percentages


def test_legacy_plan_and_bytes_work_without_metadata(monkeypatch):
    records = []
    ctx = SimpleNamespace(
        cfg=lambda section: {}, log=lambda text: None, report_progress=lambda pct: None,
        report_execution=lambda phase, label, **values: records.append({'phase': phase, **values}),
    )

    def run(cmd, *, on_stdout, on_stderr):
        on_stdout('DUBFORGE_PLAN:{"format_id":"old","filesize":100}')
        on_stderr('DUBFORGE_BYTES:"old"\t{"downloaded_bytes":50,"total_bytes":100}')
        return 0, 'D:/video.mp4\n旧协议\n', ''

    ctx.run_subprocess = run
    download._run_ytdlp(ctx, 'https://example.com/v', 'video.%(ext)s')
    progress = next(row for row in records if row['phase'] == 'downloading')
    assert progress['completed'] == 50 and progress['total'] == 100
    assert progress['current_format'] == 'old' and progress['current_stream'] is None
