"""源日期只来自平台资料；文案生成与既有稿件相互隔离。"""
from types import SimpleNamespace
import json

import pytest

from dubforge.publishing import copywriting, service, source_credit


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


def test_attribution_components_can_be_selected_independently():
    info = {'source': {'uploader': '作者', 'release_date': '2026-09-01'}}
    project = SimpleNamespace(source_kind='url', source_url='https://example.com/video')
    assert source_credit.attribution(
        project, info, include_author=False, include_published_date=True, include_source_url=False,
    ) == '原视频发布时间：2026-09-01'
    assert source_credit.attribution(
        project, info, include_author=True, include_published_date=False, include_source_url=False,
    ) == '原作者：作者'
    assert source_credit.attribution(
        project, info, include_author=False, include_published_date=False, include_source_url=True,
    ) == '原视频：https://example.com/video'


@pytest.mark.parametrize(('kind', 'url'), [
    ('local', 'D:/private/video.mp4'), ('url', 'https://user:password@example.com/v'),
    ('url', 'https://[invalid'), ('url', 'file:///D:/private/video.mp4'),
])
def test_credit_never_includes_local_path_or_credentials(kind, url):
    assert source_credit.attribution(SimpleNamespace(source_kind=kind, source_url=url), {}) == ''


def test_description_links_are_extracted_without_sponsor_or_source_url():
    description = '''项目文档：https://docs.example.test/guide（请先阅读）
GitHub: https://github.com/example/demo.
赞助与优惠码：https://sponsor.example.test/?code=abc
原视频：https://www.youtube.com/watch?v=source'''
    links = source_credit.extract_description_links(
        description, exclude_url='https://www.youtube.com/watch?v=source')
    assert [row['url'] for row in links] == [
        'https://docs.example.test/guide', 'https://github.com/example/demo']
    assert links[0]['label'] == '项目文档'
    assert links[0]['category'] == 'resource'
    sponsored = source_credit.extract_description_links(description, include_sponsor=True)
    assert next(row for row in sponsored if 'sponsor.example' in row['url'])['sponsor'] is True
    assert next(row for row in sponsored if 'sponsor.example' in row['url'])['category'] == 'sponsor'


def test_description_links_append_is_idempotent_and_does_not_accept_credentials():
    links = [{'url': 'https://docs.example.test/guide', 'label': '文档'},
             {'url': 'https://user:password@example.test/private', 'label': '私有'}]
    first = source_credit.append_source_links('简介', links)
    assert first == '简介\n\n相关链接：\n- 文档: https://docs.example.test/guide'
    assert source_credit.append_source_links(first, links) == first


def test_copy_candidate_respects_link_and_sponsor_options(isolated_env, monkeypatch):
    project = SimpleNamespace(uid='p1', dir_name='p1', title='测试', source_kind='url',
                              source_url='https://video.example.test/watch/1')
    info = {'title_original': '原视频', 'description':
            '文档：https://docs.example.test/guide\n赞助：https://sponsor.example.test/code',
            'source': {'uploader': '原作者', 'upload_date': '2026-01-02'}, 'content': {}, 'summary_stale': True}
    monkeypatch.setattr(copywriting.video_info, 'view', lambda _: {'info': info})
    # 此用例只校验链接筛选，不应初始化真实项目依赖或查询其他测试的数据库。
    monkeypatch.setattr(copywriting, 'Dependencies', lambda *_: SimpleNamespace(fresh=lambda *_: False))
    result = {'title': '标题', 'description': '正文\n外链：https://evil.example.test/x\n赞助合作内容', 'tags': ['测试'], 'dynamic': ''}
    monkeypatch.setattr(copywriting.llm, 'chat_json', lambda *_a, **kw: kw['validator'](result.copy()))
    candidate = copywriting.generate(project, {}, '', {'include_source_links': True, 'include_sponsor_links': False})
    assert 'https://docs.example.test/guide' in candidate['values']['description']
    assert 'sponsor.example' not in candidate['values']['description']
    assert 'evil.example' not in candidate['values']['description'] and '赞助合作' not in candidate['values']['description']
    assert candidate['evidence']['source_links'][0]['sponsor'] is False
    assert '赞助' not in json.dumps(candidate['evidence'], ensure_ascii=False)
    candidate = copywriting.generate(project, {}, '', {'include_source_links': False, 'include_sponsor_links': True})
    assert 'docs.example' not in candidate['values']['description']

    candidate = copywriting.generate(project, {}, '', {
        'include_source_links': False, 'include_sponsor_links': False,
        'include_original_author': False, 'include_original_published_date': False,
        'include_original_video_url': False,
    })
    assert not any(line.startswith(('原作者：', '原视频发布时间：', '原视频：'))
                   for line in candidate['values']['description'].splitlines())


def test_link_categories_are_independently_filterable():
    description = '\n'.join([
        '源码 https://github.com/example/demo',
        '作者 https://x.com/example',
        '其他 https://example.test/anything',
        '赞助 https://patreon.com/example',
    ])
    rows = source_credit.extract_description_links(description, include_sponsor=True)
    assert {row['category'] for row in rows} == {'resource', 'social', 'unknown', 'sponsor'}
    options = copywriting.normalize_options({'include_resource_links': False, 'include_social_links': False,
                                              'include_unknown_links': True, 'include_sponsor_links': False})
    assert options == {'include_resource_links': False, 'include_social_links': False,
                       'include_unknown_links': True, 'include_sponsor_links': False}


def test_copy_options_support_independent_attribution_switches():
    options = copywriting.normalize_options({
        'include_original_author': False,
        'include_original_published_date': True,
        'include_original_video_url': False,
    })
    assert options['include_original_author'] is False
    assert options['include_original_published_date'] is True
    assert options['include_original_video_url'] is False
    # 短别名用于手动 API 调用时仍统一到 canonical key。
    aliases = copywriting.normalize_options({'include_author': False, 'include_published_date': False,
                                              'include_source_url': True})
    assert aliases['include_original_author'] is False
    assert aliases['include_original_published_date'] is False
    assert aliases['include_original_video_url'] is True


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
