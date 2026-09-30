"""下载失败恢复：隔离真实目录/网络，覆盖转正、取消、超时提示和归档误跳过。"""
import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from conftest import wait_status
from dubforge.domain import crud
from dubforge.pipeline import step_download as download
from dubforge.pipeline.video_source import find_final_video
from dubforge.runner import StepCancelled, StepContext, _sweep_temp_files, runner


@pytest.fixture
def context(make_project, pdir):
    uid = make_project()
    project = crud.get_project(uid)
    root = pdir(uid)
    return StepContext(project, 'download', root, root / 'logs/download.log', threading.Event())


def seed_old(ctx):
    media = ctx.pdir / 'media'
    media.mkdir(exist_ok=True)
    video = media / 'video.mp4'
    video.write_bytes(b'old-video')
    download._write_meta(media / 'meta.json', {'file': 'video.mp4', 'title': '旧标题'})
    ctx.register_file('video.mp4', video, 'video')
    ctx.write_json('meta.json', {'file': 'video.mp4', 'title': '旧标题'})
    ctx.write_json('video_info.json', {'title_original': '旧标题'})
    return video


def staged_output(template, content=b'new-video', ext='mp4'):
    video = Path(template.replace('%(ext)s', ext))
    video.write_bytes(content)
    return {'filepath': str(video), 'title': '新标题', 'source_meta': {}}


def settle(ctx):
    deadline = time.monotonic() + 5
    while runner.current_job_id(ctx.project.id, 'download') is not None:
        assert time.monotonic() < deadline
        time.sleep(.01)


@pytest.mark.parametrize('force', [False, True])
def test_failed_rebuild_preserves_old_video_metadata_and_registration(context, monkeypatch, force):
    old = seed_old(context)
    original = {p: p.read_bytes() for p in [old, old.parent / 'meta.json',
                context.artifact_dir() / 'meta.json', context.artifact_dir() / 'video_info.json']}
    step = crud.get_step(context.project.id, 'download')
    step.status = 'stale'
    crud.save_step(step)

    def fail(ctx, url, template):
        assert all(p.read_bytes() == data for p, data in original.items())
        staged_output(template, b'incomplete')
        Path(template.replace('%(ext)s', 'f251.webm.part')).write_bytes(b'partial')
        raise download._download_error('ERROR: The read operation timed out. Giving up after 5 retries')

    monkeypatch.setattr(download, '_run_ytdlp', fail)
    runner.start_step(context.project.uid, 'download', force=force)
    wait_status(context.project.uid, 'download', {'failed'})
    settle(context)
    assert all(p.read_bytes() == data for p, data in original.items())
    assert not list(old.parent.glob('.download-*.tmp'))
    arts = crud.latest_artifacts(context.project.id, ['download'])
    assert {'download/video.mp4', 'download/meta.json', 'download/video_info.json'} <= arts.keys()
    failed = crud.get_step(context.project.id, 'download')
    assert failed.error_code == 'DOWNLOAD_FAIL'
    assert '超时' in failed.error_message
    assert '网络或代理' in failed.error_suggestion
    log = context._log_file.read_text(encoding='utf-8')
    assert '已有源视频保持不变' in log
    assert '强制重跑: 清掉旧视频' not in log


@pytest.mark.parametrize('failure', ['cancel', 'invalid', 'replace', 'outside', 'missing', 'partial', 'empty'])
def test_uncommitted_download_never_replaces_old_video(context, monkeypatch, failure):
    old = seed_old(context)
    external = context.pdir / 'not-ours.mp4'
    external.write_bytes(b'external')

    def run(ctx, url, template):
        info = staged_output(template, b'' if failure == 'empty' else b'new-video')
        if failure == 'outside':
            info['filepath'] = str(external)
        elif failure == 'missing':
            Path(info['filepath']).unlink()
            info['filepath'] = ''
        elif failure == 'partial':
            Path(info['filepath']).rename(Path(info['filepath']).with_name('video.f137.mp4'))
            info['filepath'] = ''
        return info

    def validate(ctx, path):
        assert old.read_bytes() == b'old-video'
        if failure == 'invalid':
            raise download.DownloadError('校验失败', code='DOWNLOAD_INVALID')
        if failure == 'cancel':
            ctx._cancel.set()  # 校验结束后、转正前取消，同样不能覆盖。

    monkeypatch.setattr(download, '_run_ytdlp', run)
    if failure != 'empty':
        monkeypatch.setattr(download, '_validate_download', validate)
    real_replace = download.atomic_replace

    def replace(src, dst):
        if Path(dst) == old:
            raise PermissionError('文件被占用')
        real_replace(src, dst)

    if failure == 'replace':
        monkeypatch.setattr(download, 'atomic_replace', replace)
    with pytest.raises((download.DownloadError, StepCancelled)):
        download._download_video(context, 'https://example.com/video', old.parent)
    assert old.read_bytes() == b'old-video'
    assert external.read_bytes() == b'external'
    assert not list(old.parent.glob('.download-*.tmp'))


def test_success_validates_before_atomic_replacement(context, monkeypatch):
    old = seed_old(context)
    seen = []
    monkeypatch.setattr(download, '_run_ytdlp', lambda ctx, url, template: staged_output(template))
    def validate(ctx, path):
        assert old.read_bytes() == b'old-video'
        assert path.read_bytes() == b'new-video'
        seen.append('validated')
    monkeypatch.setattr(download, '_validate_download', validate)
    real_replace = download.atomic_replace
    def replace(src, dst):
        if Path(dst) == old:
            assert seen == ['validated']
            assert Path(src).suffix == '.tmp'
        real_replace(src, dst)
    monkeypatch.setattr(download, 'atomic_replace', replace)
    path, _ = download._download_video(context, 'https://example.com/video', old.parent)
    assert path == old
    assert old.read_bytes() == b'new-video'
    assert not list(old.parent.glob('.download-*.tmp'))


def test_success_updates_format_registration_and_second_run_is_idempotent(context, monkeypatch):
    old = seed_old(context)
    calls = []
    def fetch(ctx, url, template):
        calls.append(url)
        return staged_output(template, ext='webm')
    monkeypatch.setattr(download, '_run_ytdlp', fetch)
    monkeypatch.setattr(download, '_validate_download', lambda *a: None)
    monkeypatch.setattr(download, '_ensure_video_info', lambda *a, **k: {})
    runner.start_step(context.project.uid, 'download', force=True)
    wait_status(context.project.uid, 'download', {'done'})
    settle(context)
    assert not old.exists()
    video = old.with_suffix('.webm')
    assert find_final_video(old.parent) == video
    arts = crud.latest_artifacts(context.project.id, ['download'])
    assert Path(arts['download/video.mp4'].rel_path).name == video.name
    assert json.loads((old.parent / 'meta.json').read_text(encoding='utf-8'))['file'] == video.name
    assert runner.start_step(context.project.uid, 'download')['status'] == 'skipped'
    assert len(calls) == 1


def test_download_timeout_is_visible_through_steps_api(context, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    seed_old(context)
    def fail(ctx, url, template):
        raise download._download_error('ERROR: The read operation timed out. Giving up after 5 retries')
    monkeypatch.setattr(download, '_run_ytdlp', fail)
    runner.start_step(context.project.uid, 'download', force=True)
    wait_status(context.project.uid, 'download', {'failed'})
    settle(context)
    response = TestClient(app).get(f'/api/projects/{context.project.uid}/steps')
    assert response.status_code == 200
    step = next(s for s in response.json() if s['key'] == 'download')
    assert step['error']['code'] == 'DOWNLOAD_FAIL'
    assert '下载超时' in step['error']['message']
    assert '网络或代理' in step['error']['suggestion']


def test_forced_stop_preserves_old_registered_files(context, monkeypatch):
    old = seed_old(context)
    started, release = threading.Event(), threading.Event()
    def run(ctx, url, template):
        staged_output(template)
        started.set()
        assert release.wait(5)
        ctx.check_cancel()
    monkeypatch.setattr(download, '_run_ytdlp', run)
    runner.start_step(context.project.uid, 'download', force=True)
    try:
        assert started.wait(5)
        runner.force_stop(context.project.uid, 'download')
    finally:
        release.set()
    settle(context)
    assert old.read_bytes() == b'old-video'
    assert 'download/video.mp4' in crud.latest_artifacts(context.project.id, ['download'])
    assert not list(old.parent.glob('.download-*.tmp'))


def test_command_uses_native_bounded_retry_and_ignores_global_archive(context):
    from dubforge import config
    from yt_dlp import parse_options
    archive = config.data_root() / 'download_archive.txt'
    archive.write_text('youtube already-downloaded\n', encoding='utf-8')
    url = 'https://example.com/watch?v=already-downloaded'
    cmd = download._ytdlp_cmd(context, url, str(context.pdir / 'video.%(ext)s'))
    assert '--download-archive' not in cmd
    assert '--ignore-config' in cmd
    assert cmd[-2:] == ['--', url]
    # 实际解析本机安装版本的参数，不依赖选项字符串碰巧出现在命令中。
    options = parse_options(cmd[3:]).ydl_opts
    assert options['retries'] == options['fragment_retries'] == 5
    assert options['socket_timeout'] == 30
    assert options['continuedl'] is True
    assert options['skip_unavailable_fragments'] is False
    # YouTube n-challenge: EJS 求解脚本必须显式启用, 否则部分格式缺失、下载易被掐断。
    assert options['remote_components'] == ['ejs:github']
    for kind in ('http', 'fragment'):
        sleeps = options['retry_sleep_functions'][kind]
        assert sleeps(0) == 1 and sleeps(10) == 10
    assert archive.read_text(encoding='utf-8') == 'youtube already-downloaded\n'


@pytest.mark.parametrize(('detail', 'message'), [
    ('ERROR: The read operation timed out. Giving up after 5 retries', '下载超时'),
    ('ERROR: HTTP Error 429: Too Many Requests', '限流'),
    ("ERROR: Sign in to confirm you're not a bot", '登录或验证'),
    ('ERROR: HTTP Error 403: Forbidden', '403'),
    ('ERROR: Unable to connect to proxy: Connection refused', '代理'),
    ('ERROR: [download] Got error: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1032). Giving up after 5 retries', '连接被中断'),
    ('ERROR: Requested format is not available', '下载格式'),
    ('ERROR: No space left on device', '磁盘空间不足'),
    ('ERROR: Unknown download failure', 'yt-dlp 下载失败'),
])
def test_errors_explain_cause_and_preserve_detail(context, monkeypatch, detail, message):
    monkeypatch.setattr(context, 'run_subprocess', lambda *a, **k: (1, '', detail))
    with pytest.raises(download.DownloadError) as error:
        download._run_ytdlp(context, 'https://example.com/video', 'video.%(ext)s')
    assert error.value.code == 'DOWNLOAD_FAIL'
    assert message in error.value.message
    assert detail in error.value.detail
    assert detail in context._log_file.read_text(encoding='utf-8')
    assert context._eta_provider is None


def test_native_retry_messages_are_logged_even_when_download_recovers(context, monkeypatch):
    def run(cmd, *, on_stdout, on_stderr):
        on_stderr('[download] Got error: The read operation timed out. Retrying (1/5)...')
        on_stderr('[download] Sleeping 1.00 seconds ...')
        return 0, 'C:/tmp/video.mp4\n标题\n', ''
    monkeypatch.setattr(context, 'run_subprocess', run)
    assert download._run_ytdlp(context, 'https://example.com/v', 'video.%(ext)s')['title'] == '标题'
    assert 'Retrying (1/5)' in context._log_file.read_text(encoding='utf-8')


def test_cancel_during_downloader_clears_progress_provider(context, monkeypatch):
    context.set_eta_provider(lambda: 30)
    def cancel(*a, **k):
        raise StepCancelled('测试取消')
    monkeypatch.setattr(context, 'run_subprocess', cancel)
    with pytest.raises(StepCancelled):
        download._run_ytdlp(context, 'https://example.com/video', 'video.%(ext)s')
    assert context._eta_provider is None


def test_diagnostic_removes_sensitive_urls_and_progress_payload():
    detail = download._diagnostic_text('ERROR: https://name:password@proxy.test:7897/path?token=secret\n'
                                       'DUBFORGE_META_DESCRIPTION:private-description\n'
                                       'DUBFORGE_BYTES:secret-progress\nERROR: timed out')
    assert 'password' not in detail and 'token=secret' not in detail
    assert 'private-description' not in detail and 'secret-progress' not in detail
    assert 'timed out' in detail


@pytest.mark.parametrize('payload', [
    {'format': {'duration': '1'}, 'streams': []},
    {'format': {'duration': '0'}, 'streams': [{'codec_type': 'video'}]},
    {'format': {'duration': 'nan'}, 'streams': [{'codec_type': 'video'}]},
    {'format': {'duration': '1'}, 'streams': [{'codec_type': 'audio'}]},
    None,
])
def test_probe_rejects_invalid_media(context, monkeypatch, payload):
    video = context.pdir / 'probe.mp4'
    video.write_bytes(b'not-empty')
    monkeypatch.setattr(context, 'run_subprocess', lambda *a, **k: (0, json.dumps(payload), ''))
    with pytest.raises(download.DownloadError, match='无法确认为完整视频'):
        download._validate_download(context, video)


def test_probe_accepts_real_short_local_video(context):
    video = context.pdir / 'sample.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    'testsrc2=size=32x32:rate=2:duration=1', '-c:v', 'libx264', str(video)],
                   check=True, capture_output=True)
    download._validate_download(context, video)


def test_cleanup_and_source_resolution_never_expose_staging_files(context):
    media = context.pdir / 'media'
    media.mkdir(exist_ok=True)
    staging = media / '.download-crashed.tmp'
    staging.mkdir()
    for name in ['video.mp4', 'video.f251.webm', 'video.f401.mp4.part']:
        (staging / name).write_bytes(b'partial')
    (media / 'video.mp4.tmp').write_bytes(b'partial')
    (media / 'video.info.json').write_bytes(b'{}')
    assert find_final_video(media) is None
    removed = _sweep_temp_files(context.pdir, 'download')
    assert not staging.exists()
    assert not (media / 'video.mp4.tmp').exists()
    assert any('.download-crashed.tmp' in name for name in removed)


def test_registered_new_container_wins_if_old_format_cannot_be_removed(context):
    old = seed_old(context)
    newer = old.parent / 'video.webm'
    newer.write_bytes(b'new-video')
    download._write_meta(old.parent / 'meta.json', {'file': newer.name})
    assert find_final_video(old.parent) == newer
    download._write_meta(old.parent / 'meta.json', {'file': '../not-ours.mp4'})
    assert find_final_video(old.parent) == old
