"""源日期只来自平台资料；文案生成与既有稿件相互隔离。"""
from types import SimpleNamespace

import pytest

from dubforge.publishing import service, source_credit


@pytest.mark.parametrize(('source', 'expected'), [
    ({'release_date': '2026-09-10', 'upload_date': '2026-09-08'}, '2026-09-10'),
    ({'upload_date': '2026-09-08'}, '2026-09-08'),
    ({'release_date': '2026-02-30', 'upload_date': '2024-02-29'}, '2024-02-29'),
    ({'release_date': None, 'upload_date': '2026-01-02'}, '2026-01-02'),
    ({'upload_date': '2026-02-29'}, ''), ({'upload_date': '2026-W01-1'}, ''),
    ({'upload_date': False}, ''), ({'upload_date': '2026-09-01T12:30:00Z'}, ''),
    ({'timestamp': 1790610000}, ''), ({}, ''),
])
def test_date_is_valid_source_calendar_date(source, expected):
    assert source_credit.published_date({'source': source}) == expected


def test_programmatic_credit_is_preserved_once_with_utf16_limit():
    info = {'source': {'uploader': '原作者\n频道', 'release_date': '2026-09-01'}}
    project = SimpleNamespace(source_kind='url', source_url='https://example.com/v')
    credit = source_credit.attribution(project, info)
    body = '😀' * 1000 + '\n原视频发布时间：1999-01-01\n原作者：错误作者\n原视频：https://incorrect.example/'
    result = source_credit.append_attribution(body, credit)
    assert len(result.encode('utf-16-le')) // 2 <= 2000
    assert result.endswith(credit) and '1999-01-01' not in result and 'incorrect.example' not in result
    assert result.count('原视频发布时间：2026-09-01') == 1 and '原作者：原作者 频道' in result
    assert '\ufffd' not in result
    assert source_credit.append_attribution(result, credit) == result


@pytest.mark.parametrize(('kind', 'url'), [
    ('local', 'D:/private/video.mp4'), ('url', 'https://user:password@example.com/v'),
    ('url', 'https://[invalid'), ('url', 'file:///D:/private/video.mp4'),
])
def test_credit_never_includes_local_path_or_credentials(kind, url):
    assert source_credit.attribution(SimpleNamespace(source_kind=kind, source_url=url), {}) == ''


def test_draft_default_contains_date_but_refresh_never_rewrites_manual_copy(make_project, monkeypatch):
    uid = make_project()
    info = {'source': {'upload_date': '2026-01-02'}, 'summary_zh': '原视频概述'}
    monkeypatch.setattr(service.video_info, 'view', lambda uid: {'info': info})
    monkeypatch.setattr(service.transport, 'call', lambda *a, **k: pytest.fail('日期填充不得调用B站'))
    row = service.create(uid)
    assert service.view(row['id'])['settings']['description'].startswith('原视频概述\n\n')
    assert '原视频发布时间：2026-01-02' in row['draft']['content']['description']
    row = service.save(row['id'], dict(row['draft'], revision=row['revision'], overrides={'description': '手写简介'}))
    before = service.view(row['id'])
    info['source']['upload_date'] = '2026-01-03'
    assert service.candidates(uid)['source_published_date'] == '2026-01-03'
    assert service.view(row['id']) == before
    assert before['settings']['description'] == '手写简介'


def test_default_description_omits_stale_summary_and_keeps_credit(make_project, monkeypatch):
    uid = make_project()
    monkeypatch.setattr(service.video_info, 'view', lambda uid: {'info': {
        'source': {'upload_date': '2026-01-02'}, 'summary_zh': '过期摘要', 'summary_stale': True}})
    data = service.candidates(uid)
    assert '过期摘要' not in data['suggestion']['description']
    assert '原视频发布时间：2026-01-02' in data['suggestion']['description']


def test_default_long_description_reserves_date(make_project, monkeypatch):
    uid = make_project()
    monkeypatch.setattr(service.video_info, 'view', lambda uid: {'info': {
        'source': {'upload_date': '2026-01-02'}, 'summary_zh': '😀' * 2000}})
    description = service.candidates(uid)['suggestion']['description']
    service.validate_values({'description': description})
    assert '原视频发布时间：2026-01-02' in description
