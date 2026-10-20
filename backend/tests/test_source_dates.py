"""发布日期补齐只使用隔离数据与采集替身，不访问平台或启动制作。"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from dubforge import source_dates as dates, video_info
from dubforge.domain import crud
from dubforge.runner import compute_fingerprint


def save(uid, info):
    path = crud.project_dir(uid) / 'artifacts/download/video_info.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(info, ensure_ascii=False), encoding='utf-8')
    return path


@pytest.mark.parametrize('source,expected', [
    ({'upload_date': '2024-03-09', 'release_date': '2024-03-08'}, '2024-03-09'),
    ({'upload_date': '20240309'}, '2024-03-09'),
    ({'upload_date': 'bad', 'release_date': '2024-03-08'}, '2024-03-08'),
    ({'timestamp': datetime(2024, 3, 7, 23, 59, tzinfo=timezone.utc).timestamp()}, '2024-03-07'),
    ({'release_timestamp': datetime(2024, 3, 6, tzinfo=timezone.utc).timestamp()}, '2024-03-06'),
    ({'upload_date': '2024-13-40', 'timestamp': float('nan')}, None),
    ({'timestamp': True, 'release_timestamp': -1}, None),
    ({'timestamp': 0}, None),
    ({'generated_at': '2024-03-09', 'collected_at': '2024-03-09'}, None),
])
def test_read_real_source_fields_only(make_project, monkeypatch, source, expected):
    uid = make_project()
    save(uid, {'source': source})
    monkeypatch.setattr(video_info, '_fetch_source', lambda *a, **k: pytest.fail('读取日期不能联网'))
    assert dates.read(uid) == expected
    assert dates.read('missing-project') is None


def test_cache_skips_network_and_never_uses_project_import_time(make_project, monkeypatch):
    missing, cached = make_project(), make_project()
    save(cached, {'source': {'upload_date': '2024-03-09'}})
    monkeypatch.setattr(video_info, '_fetch_source', lambda *a, **k: pytest.fail('已缓存或预算为零不可联网'))
    assert dates.ensure_many([cached, missing, cached], timeout_seconds=0) == {cached: '2024-03-09', missing: None}


def test_supplement_preserves_existing_content_names_pipeline_and_media(make_project, monkeypatch):
    uid = make_project(title='手动命名保留')
    project = crud.get_project(uid)
    old = {'schema_version': 2, 'title_original': '原标题', 'title_zh': '旧中文名',
        'description': '手工简介', 'summary_zh': '旧概要', 'summary_status': 'generated',
        'summary_fingerprint': 'existing-summary', 'summary_stale': False,
        'comments': {'items': [{'text': '旧评论'}]}, 'content': {'chapters': [{'title': '保留章节'}]},
        'source': {'url': project.source_url, 'view_count': 123, 'uploader': '原作者', 'upload_date': ''}}
    path = save(uid, old)
    video = crud.project_dir(uid) / 'media/video.mp4'
    video.parent.mkdir(exist_ok=True)
    video.write_bytes(b'keep-media')
    before_steps = [(step.step_key, step.status, step.input_fingerprint) for step in crud.get_steps(project.id)]
    fingerprint = compute_fingerprint(project, 'download')
    calls = []
    def fetch(url, settings, **kwargs):
        calls.append(kwargs)
        assert video_info._active[uid] == 'source_date'
        return {'title': '平台改了标题', 'description': '平台新简介', 'view_count': 999, 'upload_date': '20240309'}
    monkeypatch.setattr(video_info, '_fetch_source', fetch)
    monkeypatch.setattr(video_info, 'chat_json', lambda *a, **k: pytest.fail('日期补齐不可调用 AI'))
    assert dates.ensure_many([uid]) == {uid: '2024-03-09'}
    after = json.loads(path.read_text(encoding='utf-8'))
    assert after == {**old, 'source': {**old['source'], 'upload_date': '2024-03-09'}}
    assert 'comment_limit' not in calls[0] or calls[0]['comment_limit'] == 0
    assert crud.get_project(uid).title == '手动命名保留'
    assert compute_fingerprint(crud.get_project(uid), 'download') == fingerprint
    assert [(step.step_key, step.status, step.input_fingerprint) for step in crud.get_steps(project.id)] == before_steps
    assert crud.latest_job(project.id, 'download') is None
    assert video.read_bytes() == b'keep-media'
    assert video_info.view(uid)['busy'] is None
    assert [a.name for a in crud.latest_artifacts(project.id, ['download']).values()] == ['video_info.json']


def test_empty_info_builds_basic_metadata_without_renaming_or_ai(make_project, monkeypatch):
    uid = make_project(title='用户的项目名')
    monkeypatch.setattr(video_info, '_fetch_source', lambda *a, **k: {
        'title': '来源标题', 'description': '来源简介', 'release_date': '20240309'})
    monkeypatch.setattr(video_info, 'chat_json', lambda *a, **k: pytest.fail('日期补齐不可调用 AI'))
    assert dates.ensure_many([uid]) == {uid: '2024-03-09'}
    info = video_info.view(uid)['info']
    assert info['title_original'] == '来源标题'
    assert info['summary_status'] == 'not_generated'
    assert info['source']['upload_date'] == ''
    assert info['source']['release_date'] == '2024-03-09'
    assert crud.get_project(uid).title == '用户的项目名'


@pytest.mark.parametrize('outcome', ['failure', 'undated', 'local', 'busy', 'save_failure'])
def test_failure_local_busy_leave_snapshot_untouched(make_project, monkeypatch, outcome):
    uid = make_project()
    path = save(uid, {'title_original': '原标题', 'source': {}, 'comments': {'items': []}})
    before = path.read_bytes()
    calls = []
    def fetch(*a, **k):
        calls.append(1)
        if outcome == 'failure':
            raise video_info.VideoInfoError('模拟平台失败')
        return {'title': '平台标题', **({} if outcome == 'undated' else {'upload_date': '20240309'})}
    monkeypatch.setattr(video_info, '_fetch_source', fetch)
    if outcome == 'local':
        project = crud.get_project(uid)
        project.source_kind = 'local'
        crud.save_project(project)
    if outcome == 'busy':
        monkeypatch.setitem(video_info._active, uid, 'refresh')
    if outcome == 'save_failure':
        monkeypatch.setattr(crud, 'add_artifact', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('登记失败')))
    assert dates.ensure_many([uid]) == {uid: None}
    assert path.read_bytes() == before
    assert calls == ([] if outcome in {'local', 'busy'} else [1])


def test_global_concurrency_limit_and_same_project_singleflight(make_project, monkeypatch):
    uids = [make_project() for _ in range(6)]
    lock = threading.Lock()
    release = threading.Event()
    started = threading.Event()
    calls, active, peak = [], 0, 0
    def fetch(url, settings, *, cancel_check):
        nonlocal active, peak
        with lock:
            calls.append(url)
            active += 1
            peak = max(peak, active)
        started.set()
        assert release.wait(3)
        with lock:
            active -= 1
        return {'title': '标题', 'upload_date': '20240309'}
    monkeypatch.setattr(video_info, '_fetch_source', fetch)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(dates.ensure_many, uids)
        assert started.wait(2)
        second = executor.submit(dates.ensure_many, list(reversed(uids)))
        # 两个请求均处于未完成提取期间，随后同批释放采集。
        time.sleep(.08)
        release.set()
        expected = dict.fromkeys(uids, '2024-03-09')
        assert first.result(timeout=5) == expected
        assert second.result(timeout=5) == expected
    assert len(calls) == len(uids)
    assert peak <= 3
    assert dates._inflight == {}


def test_deadline_cancels_fetch_and_does_not_save_late_result(make_project, monkeypatch):
    uid = make_project()
    cleaned = threading.Event()
    def fetch(url, settings, *, cancel_check):
        try:
            while True:
                cancel_check()
                time.sleep(.005)
        finally:
            cleaned.set()
    monkeypatch.setattr(video_info, '_fetch_source', fetch)
    started = time.monotonic()
    assert dates.ensure_many([uid], timeout_seconds=.2) == {uid: None}
    assert time.monotonic() - started < .6
    assert cleaned.is_set()
    assert video_info.view(uid)['info'] is None
    assert video_info.view(uid)['busy'] is None


def test_incomplete_platform_response_never_saves_an_invented_date(make_project, monkeypatch):
    uid = make_project()
    monkeypatch.setattr(video_info, '_fetch_source', lambda *a, **k: {
        'title': '标题', 'generated_at': '2024-03-09', 'upload_date': '2024-99-99'})
    assert dates.ensure_many([uid]) == {uid: None}
    assert video_info.view(uid)['info'] is None


def test_unreadable_old_metadata_is_preserved(make_project, monkeypatch):
    uid = make_project()
    path = save(uid, {})
    path.write_bytes(b'{broken-but-retain-original')
    before = path.read_bytes()
    monkeypatch.setattr(video_info, '_fetch_source', lambda *a, **k: pytest.fail('不可解析的旧资料应先保留'))
    assert dates.ensure_many([uid]) == {uid: None}
    assert path.read_bytes() == before
