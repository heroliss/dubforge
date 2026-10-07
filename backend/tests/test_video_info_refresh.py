"""独立资料刷新回归：外部采集全用桩，项目数据必须由隔离夹具创建。"""
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from dubforge import video_info as vi
from dubforge.runtime_edit import RuntimeEditError
from dubforge.domain import crud
from dubforge.runner import compute_fingerprint, runner


@pytest.fixture
def source():
    return {
        'id': 'abc123', 'title': 'Source title', 'description': '第一段\n第二段 https://example.com',
        'extractor': 'youtube', 'webpage_url': 'https://www.youtube.com/watch?v=abc123',
        'uploader': 'Author', 'channel': 'Channel', 'channel_url': 'https://www.youtube.com/@author',
        'channel_id': 'UC123', 'channel_follower_count': 0, 'channel_is_verified': False,
        'upload_date': '20260102', 'release_date': '20260101', 'duration': 123,
        'view_count': 0, 'like_count': 7, 'comment_count': 1234,
        'tags': ['AI', '教程', 'AI'], 'categories': ['Education'],
        'chapters': [{'title': '介绍', 'start_time': 0, 'end_time': 10},
                     {'title': '实操', 'start_time': 10, 'end_time': 120}],
        'subtitles': {'en': [{'name': 'English', 'ext': 'vtt', 'url': 'https://private.test/?token=secret'}]},
        'automatic_captions': {'zh-Hans': [{'name': '中文', 'ext': 'json3', 'url': 'https://private.test'}]},
        'language': 'en', 'license': 'Creative Commons', 'availability': 'public', 'live_status': 'not_live',
    }


@pytest.fixture
def project(make_project):
    uid = make_project(title='用户自定义名称')
    project = crud.get_project(uid)
    project.source_url = 'https://www.youtube.com/watch?v=abc123'
    crud.save_project(project)
    return project


def save_info(project, info):
    path = crud.project_dir(project.uid) / 'artifacts/download/video_info.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(info, ensure_ascii=False), encoding='utf-8')
    crud.add_artifact(project.id, 'download', 'json', path.name, path, str(path.relative_to(crud.project_dir(project.uid))))
    return path


def test_rich_content_is_normalized_and_secrets_not_persisted(source):
    source.update(view_count=float('inf'), like_count=True, release_date='20261340',
                  uploader_url='javascript:alert(1)', formats=[{'url': 'private-media-url'}],
                  http_headers={'Authorization': 'private-token'}, tags=['AI', 'AI', None, {}])
    source['chapters'] += [None, {'title': '坏章节', 'start_time': float('nan')}, {'title': '负数', 'start_time': -1}]
    info = vi._build_video_info('youtube', source['title'], source['webpage_url'], None, source, summarize=False)
    assert info['source']['view_count'] is None
    assert info['source']['like_count'] is None
    assert info['source']['channel_follower_count'] == 0
    assert info['source']['channel_is_verified'] is False
    assert info['source']['release_date'] == ''
    assert info['source']['uploader_url'] == ''
    assert info['content']['tags'] == ['AI']
    assert len(info['content']['chapters']) == 2
    assert info['content']['captions']['manual'][0] == {'language': 'en', 'name': 'English', 'formats': ['vtt']}
    assert info['description'] == source['description']
    assert 'private' not in json.dumps(info)
    assert info['summary_status'] == 'not_generated'


def test_refresh_api_does_not_download_call_llm_or_change_pipeline(project, source, monkeypatch):
    monkeypatch.setattr(vi, '_fetch_source', lambda *a, **k: source)
    monkeypatch.setattr(vi, 'chat_json', lambda *a, **k: pytest.fail('刷新平台资料不能调用 LLM'))
    root = crud.project_dir(project.uid)
    video = root / 'media/video.mp4'
    video.parent.mkdir(exist_ok=True)
    video.write_bytes(b'keep video')
    crud.add_artifact(project.id, 'download', 'video', 'video.mp4', video, 'media/video.mp4')
    steps_before = [(s.step_key, s.status, s.input_fingerprint) for s in crud.get_steps(project.id)]
    fp = compute_fingerprint(project, 'download')
    api = TestClient(app)
    response = api.post(f'/api/projects/{project.uid}/steps/download/video-info/refresh')
    assert response.status_code == 200, response.text
    info = response.json()['info']
    assert info['content']['chapters'][1]['title'] == '实操'
    assert info['source']['comment_count'] == 1234
    assert 'comments' not in info
    assert vi.view(project.uid)['info'] == info
    assert video.read_bytes() == b'keep video'
    assert compute_fingerprint(project, 'download') == fp
    assert crud.get_project(project.uid).title == '用户自定义名称'
    assert [(s.step_key, s.status, s.input_fingerprint) for s in crud.get_steps(project.id)] == steps_before
    assert crud.latest_job(project.id, 'download') is None
    assert api.get(f'/api/projects/{project.uid}/steps/download/video-info').headers['cache-control'] == 'no-store'


def test_summary_evidence_staleness_and_refresh_preserves_comments(project, source, monkeypatch):
    monkeypatch.setattr(vi, '_fetch_source', lambda *a, **k: source)
    vi.update(project.uid, 'refresh')
    prompts = []
    def chat(prompt, **kwargs):
        prompts.append(json.loads(prompt))
        assert '不可信' in kwargs['system']
        return {'title_zh': '中文标题', 'summary_zh': '有依据的概要'}
    monkeypatch.setattr(vi, 'chat_json', chat)
    summarized = vi.update(project.uid, 'summarize')
    assert prompts[0]['chapters'][1]['title'] == '实操'
    assert summarized['summary_stale'] is False
    source['view_count'] = 999
    assert not vi.update(project.uid, 'refresh')['summary_stale']
    source['description'] = '作者修改后的新简介'
    updated = vi.update(project.uid, 'refresh')
    assert updated['summary_stale'] is True
    assert updated['summary_zh'] == '有依据的概要'
    assert vi.update(project.uid, 'summarize')['summary_stale'] is False


@pytest.mark.parametrize('action', ['refresh', 'summarize', 'comments'])
def test_failure_preserves_old_snapshot(project, source, monkeypatch, action):
    old = vi._build_video_info('youtube', source['title'], source['webpage_url'], None, source, summarize=False)
    old.update(summary_zh='旧概要', summary_status='generated', comments={'items': [{'text': '旧评论'}]})
    path = save_info(project, old)
    before = path.read_bytes()
    def fail(*a, **k):
        raise vi.VideoInfoError('模拟外部服务失败')
    monkeypatch.setattr(vi, '_fetch_source', fail)
    monkeypatch.setattr(vi, 'chat_json', fail)
    with pytest.raises(vi.VideoInfoError):
        vi.update(project.uid, action)
    assert path.read_bytes() == before
    assert vi.view(project.uid)['busy'] is None


def test_comment_limit_dedup_no_replies_and_no_ai(project, source, monkeypatch):
    limits = []
    source['comments'] = [
        {'id': 'one', 'text': '第一条', 'author': '甲', 'like_count': 0, 'parent': 'root', 'is_pinned': True},
        {'id': 'one', 'text': '重复'}, {'id': 'reply', 'text': '不收回复', 'parent': 'one'},
        {'id': 'two', 'text': '第二条', 'author_url': 'javascript:alert(1)', 'author_is_uploader': True},
        {'id': 'three', 'text': '超过条数'},
    ]
    def fetch(*args, **kwargs):
        limits.append(kwargs['comment_limit'])
        return source
    monkeypatch.setattr(vi, '_fetch_source', fetch)
    monkeypatch.setattr(vi, 'chat_json', lambda *a, **k: pytest.fail('评论采集不能调用 LLM'))
    save_info(project, vi._build_video_info('youtube', source['title'], source['webpage_url'], None, source, summarize=False))
    source['comment_count'] = 5  # yt-dlp 评论后处理覆盖了平台总数。
    info = vi.update(project.uid, 'comments', comment_limit=2)
    assert info['source']['comment_count'] == 1234
    assert limits == [2]
    comments = info['comments']
    assert comments['requested_limit'] == comments['count'] == 2
    assert [c['id'] for c in comments['items']] == ['one', 'two']
    assert comments['items'][0]['like_count'] == 0
    assert comments['items'][1]['author_url'] == ''
    assert 'lc=one' in comments['items'][0]['url']
    assert vi.update(project.uid, 'refresh')['comments'] == comments
    assert limits == [2, 0]


@pytest.mark.parametrize('limit', [0, -1, 501, True, None, '20', 1.5])
def test_comment_count_validation(project, limit):
    response = TestClient(app).post(f'/api/projects/{project.uid}/steps/download/video-info/comments', json={'comment_limit': limit})
    assert response.status_code == 400
    assert response.json()['code'] == 'VIDEO_INFO_COMMENT_LIMIT'


def test_fetch_command_is_metadata_only_and_honors_network_settings(source, monkeypatch, tmp_path):
    cookies = tmp_path / 'cookies.txt'
    cookies.write_text('test cookies')
    seen = []
    def run(cmd, **kwargs):
        seen.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(source))
    monkeypatch.setattr(subprocess, 'run', run)
    vi._fetch_source(source['webpage_url'], {'cookies_path': str(cookies), 'proxy': 'http://127.0.0.1:7897'})
    cmd, options = seen[0]
    assert all(flag in cmd for flag in ['--ignore-config', '--simulate', '--skip-download', '--no-playlist', '--no-write-comments'])
    assert '--download-archive' not in cmd and '-o' not in cmd
    assert cmd[cmd.index('--cookies') + 1] == str(cookies)
    assert cmd[cmd.index('--proxy') + 1] == 'http://127.0.0.1:7897'
    assert options['timeout'] == 90
    # 用实际安装的提取器解析选项/模板，不访问网络，防止 CLI 拼写或结构投影不受支持。
    from yt_dlp import YoutubeDL, parse_options
    parsed = parse_options(cmd[3:]).ydl_opts
    assert parsed['simulate'] and parsed['skip_download'] and not parsed['getcomments']
    with YoutubeDL({'quiet': True}) as ydl:
        filtered = json.loads(ydl.evaluate_outtmpl(cmd[cmd.index('--print') + 1], source))
    assert filtered['chapters'] == source['chapters']
    source['comments'] = []
    vi._fetch_source(source['webpage_url'], {}, comment_limit=30)
    comment_cmd = seen[1][0]
    parsed = parse_options(comment_cmd[3:]).ydl_opts
    assert parsed['simulate'] and parsed['skip_download'] and parsed['getcomments']
    assert parsed['extractor_args']['youtube']['comment_sort'] == ['top']
    assert parsed['extractor_args']['youtube']['max_comments'] == ['30', '30', '0', '0']


@pytest.mark.parametrize('kind', ['timeout', 'exit', 'json', 'playlist', 'empty', 'comments_missing'])
def test_fetch_rejects_unusable_platform_response(source, monkeypatch, kind):
    def run(cmd, **kwargs):
        if kind == 'timeout':
            raise subprocess.TimeoutExpired(cmd, 90)
        result = {'json': 'bad json', 'playlist': json.dumps({'_type': 'playlist', 'title': 'List'}),
                  'empty': '{}', 'comments_missing': json.dumps(source)}
        return SimpleNamespace(returncode=1 if kind == 'exit' else 0,
                               stdout=result.get(kind, ''), stderr='secret proxy password')
    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(vi.VideoInfoError) as exc:
        vi._fetch_source(source['webpage_url'], {}, comment_limit=2 if kind == 'comments_missing' else 0)
    assert 'secret' not in str(exc.value.to_dict())


def test_refresh_occupancy_blocks_duplicate_download_and_delete(project, source, monkeypatch):
    started, release = threading.Event(), threading.Event()
    def fetch(*a, **k):
        started.set()
        assert release.wait(5)
        return source
    monkeypatch.setattr(vi, '_fetch_source', fetch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(vi.update, project.uid, 'refresh')
        try:
            assert started.wait(5)
            assert vi.view(project.uid)['busy'] == 'refresh'
            for operation, error, code in [
                    (lambda: vi.update(project.uid, 'refresh'), RuntimeEditError, 'CONFIGURATION_BUSY'),
                    (lambda: runner.start_step(project.uid, 'download', force=True), vi.VideoInfoError, 'VIDEO_INFO_BUSY'),
                    (lambda: crud.delete_project(project.uid), vi.VideoInfoError, 'VIDEO_INFO_BUSY')]:
                with pytest.raises(error) as exc:
                    operation()
                assert exc.value.code == code
        finally:
            release.set()
        assert future.result(timeout=5)['title_original'] == 'Source title'
    assert vi.view(project.uid)['busy'] is None


def test_registry_failure_rolls_back_file(project, source, monkeypatch):
    old = {'title_original': '旧资料'}
    path = save_info(project, old)
    before = path.read_bytes()
    monkeypatch.setattr(vi, '_fetch_source', lambda *a, **k: source)
    def fail(*a, **k):
        raise OSError('模拟登记失败')
    monkeypatch.setattr(crud, 'add_artifact', fail)
    with pytest.raises(vi.VideoInfoError, match='保存失败'):
        vi.update(project.uid, 'refresh')
    assert path.read_bytes() == before
    assert vi.view(project.uid)['busy'] is None


def test_local_and_non_youtube_capabilities(project):
    project.source_kind, project.source_url = 'local', 'media/video.mp4'
    crud.save_project(project)
    state = vi.view(project.uid)
    assert not state['can_refresh'] and not state['can_summarize'] and not state['can_collect_comments']
    with pytest.raises(vi.VideoInfoError):
        vi.update(project.uid, 'refresh')
    project.source_kind, project.source_url = 'link', 'https://www.bilibili.com/video/123'
    crud.save_project(project)
    state = vi.view(project.uid)
    assert state['can_refresh'] and not state['can_collect_comments']


def test_download_in_progress_rejects_refresh(project):
    step = crud.get_step(project.id, 'download')
    step.status = 'queued'
    crud.save_step(step)
    with pytest.raises(RuntimeEditError) as exc:
        vi.update(project.uid, 'refresh')
    assert exc.value.code == 'CONFIGURATION_BUSY'
