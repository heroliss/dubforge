"""独立审查日期补齐：只用隔离数据与平台替身，不访问真实平台。"""
import json
import threading

import pytest

from dubforge import source_dates as dates, video_info
from dubforge.domain import crud


@pytest.fixture
def review_project(make_project):
    uid = make_project(title='保留我的项目名称')
    project = crud.get_project(uid)
    project.source_url = 'https://www.youtube.com/watch?v=review-source'
    crud.save_project(project)
    return project


def save_info(project, info):
    root = crud.project_dir(project.uid)
    path = root / 'artifacts/download/video_info.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(info, ensure_ascii=False), encoding='utf-8')
    crud.add_artifact(project.id, 'download', 'json', path.name, path, str(path.relative_to(root)))
    return path


def raw(project, **updates):
    return {'title': '平台新标题', 'webpage_url': project.source_url,
            'extractor': 'youtube', 'upload_date': '20261004', **updates}


def test_review_date_only_merge_preserves_editorial_and_media(review_project, monkeypatch):
    project = review_project
    before = {'title_original': '旧平台标题', 'description': '旧简介',
              'summary_zh': '手工维护的概要', 'summary_stale': False,
              'comments': {'items': [{'id': 'one', 'text': '已翻译的评论'}]},
              'source': {'url': project.source_url, 'uploader': '原作者', 'view_count': 17}}
    path = save_info(project, before)
    media = crud.project_dir(project.uid) / 'media/video.mp4'
    media.parent.mkdir(exist_ok=True)
    media.write_bytes(b'review-kept-media')
    steps = [(item.step_key, item.status, item.input_fingerprint) for item in crud.get_steps(project.id)]
    monkeypatch.setattr(video_info, '_fetch_source', lambda *a, **kw: raw(project))
    monkeypatch.setattr(video_info, 'chat_json', lambda *a, **kw: pytest.fail('日期检查不能调用 AI'))
    assert dates.ensure_many([project.uid])[project.uid] == '2026-10-04'
    expected = {**before, 'source': {**before['source'], 'upload_date': '2026-10-04'}}
    assert json.loads(path.read_text(encoding='utf-8')) == expected
    assert media.read_bytes() == b'review-kept-media'
    assert crud.get_project(project.uid).title == '保留我的项目名称'
    assert [(item.step_key, item.status, item.input_fingerprint) for item in crud.get_steps(project.id)] == steps
    assert crud.latest_job(project.id, 'download') is None
    assert video_info.view(project.uid)['busy'] is None


@pytest.mark.parametrize('state', ['local', 'archived', 'running', 'queued'])
def test_review_active_or_unavailable_project_never_fetches(review_project, monkeypatch, state):
    project = review_project
    if state == 'local':
        project.source_kind, project.source_url = 'local', 'media/video.mp4'
        crud.save_project(project)
    elif state == 'archived':
        from dubforge.project_archive import set_archived
        set_archived(project.uid, True)
    else:
        step = crud.get_step(project.id, 'tts')
        step.status = state
        crud.save_step(step)
    monkeypatch.setattr(video_info, '_fetch_source', lambda *a, **kw: pytest.fail('占用或本地项目不得联网'))
    assert dates.ensure_many([project.uid]) == {project.uid: None}
    assert video_info.view(project.uid)['busy'] is None


def test_review_changed_source_discards_inflight_result(review_project, monkeypatch):
    project = review_project
    path = save_info(project, {'title_original': '旧资料', 'source': {'url': project.source_url}})
    before = path.read_bytes()

    def fetch(*args, **kwargs):
        replacement = crud.get_project(project.uid)
        replacement.source_url = 'https://www.youtube.com/watch?v=different-source'
        crud.save_project(replacement)
        return raw(project)

    monkeypatch.setattr(video_info, '_fetch_source', fetch)
    assert dates.ensure_many([project.uid]) == {project.uid: None}
    assert path.read_bytes() == before
    assert dates.read(project.uid) is None


def test_review_fresh_cache_appearing_inflight_wins(review_project, monkeypatch):
    project = review_project
    path = save_info(project, {'title_original': '起始资料', 'source': {'url': project.source_url}})
    refreshed = {'title_original': '人工刷新后的资料', 'summary_zh': '保留新概要',
                 'source': {'url': project.source_url, 'upload_date': '2020-01-05'}}

    def fetch(*args, **kwargs):
        path.write_text(json.dumps(refreshed, ensure_ascii=False), encoding='utf-8')
        return raw(project)

    monkeypatch.setattr(video_info, '_fetch_source', fetch)
    assert dates.ensure_many([project.uid]) == {project.uid: '2020-01-05'}
    assert json.loads(path.read_text(encoding='utf-8')) == refreshed


def test_review_registry_failure_restores_previous_bytes(review_project, monkeypatch):
    project = review_project
    path = save_info(project, {'title_original': '旧资料', 'source': {'url': project.source_url}})
    before = path.read_bytes()
    monkeypatch.setattr(video_info, '_fetch_source', lambda *a, **kw: raw(project))
    monkeypatch.setattr(crud, 'add_artifact', lambda *a, **kw: (_ for _ in ()).throw(OSError('模拟登记失败')))
    assert dates.ensure_many([project.uid]) == {project.uid: None}
    assert path.read_bytes() == before
    assert video_info.view(project.uid)['busy'] is None


def test_review_timed_out_inflight_does_not_poison_next_inspection(review_project, monkeypatch):
    project = review_project
    started, release = threading.Event(), threading.Event()
    calls = []

    def fetch(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            started.set()
            assert release.wait(5)
        return raw(project)

    monkeypatch.setattr(video_info, '_fetch_source', fetch)
    try:
        assert dates.ensure_many([project.uid], timeout_seconds=.6) == {project.uid: None}
        assert started.is_set()
        with dates._gate:
            pending = list(dates._inflight.values())
    finally:
        release.set()
    for future in pending:
        assert future.result(timeout=5) is None
    assert dates.read(project.uid) is None
    assert dates.ensure_many([project.uid]) == {project.uid: '2026-10-04'}
    assert len(calls) == 2


def test_review_short_url_cache_uses_true_release_date_without_fetch(review_project, monkeypatch):
    project = review_project
    path = save_info(project, {'source': {'url': 'https://youtu.be/review-source',
                                        'release_date': '2026-01-02'}})
    before = path.read_bytes()
    monkeypatch.setattr(video_info, '_fetch_source', lambda *a, **kw: pytest.fail('已有日期不得联网'))
    assert dates.read(project.uid) == '2026-01-02'
    assert dates.ensure_many([project.uid]) == {project.uid: '2026-01-02'}
    assert path.read_bytes() == before
