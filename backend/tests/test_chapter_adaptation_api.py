"""同一章节整理契约服务于项目草稿与已发布稿件，候选不产生平台写入。"""
import json

import pytest
from fastapi.testclient import TestClient

from dubforge.publishing import chapters, transport


@pytest.fixture
def client(make_project, monkeypatch):
    uid = make_project()
    from app.main import app
    def forbidden(*args, **kwargs):
        pytest.fail('本地候选接口不得调用B站')
    monkeypatch.setattr(transport, 'call', forbidden)
    with TestClient(app) as current:
        yield current, uid


@pytest.mark.parametrize('project_route', [False, True])
def test_both_routes_return_whole_grouped_plan_without_platform_request(client, monkeypatch, project_route):
    current, uid = client
    rows = [dict(title=f'Topic {index}', start_time=index * 10, end_time=(index + 1) * 10)
            for index in range(11)]
    def model(prompt, *, validator, **kwargs):
        assert len(json.loads(prompt)['chapters']) == 11
        return validator({'items': [dict(start_index=0, end_index=4, title='主题一'),
                                     dict(start_index=5, end_index=10, title='主题二')],
                          'review': dict(checked=True, summary='已审查标题内容', findings=[])})
    monkeypatch.setattr(chapters.llm, 'chat_json', model)
    route = f'/projects/{uid}/chapters/translate' if project_route else '/chapters/adapt'
    result = current.post('/api/publishing' + route, json=dict(items=rows, duration=110, translate=True))
    assert result.status_code == 200, result.text
    value = result.json()
    assert value['items'] == [dict(title='主题一', start_time=0, end_time=50, source_indices=list(range(5))),
                              dict(title='主题二', start_time=50, end_time=110, source_indices=list(range(5, 11)))]
    assert value['adaptation']['source_count'] == 11
    assert value['adaptation']['output_count'] == 2


def test_original_language_mode_reviews_titles_and_rejects_client_rule_overrides(client, monkeypatch):
    current, _ = client
    def model(prompt, *, validator, **kwargs):
        return validator(dict(items=[dict(start_index=0, end_index=0, title='Intro')],
                              review=dict(checked=True, summary='原语言标题已审查', findings=[])))
    monkeypatch.setattr(chapters.llm, 'chat_json', model)
    rows = [dict(title='Intro', start_time=0, end_time=10)]
    result = current.post('/api/publishing/chapters/adapt', json=dict(items=rows, translate=False))
    assert result.status_code == 200 and result.json()['source'] == 'llm.chapter_adaptation'
    invalid = current.post('/api/publishing/chapters/adapt', json=dict(items=rows, confirmed=True))
    assert invalid.status_code == 400 and invalid.json()['code'] == 'PUBLISH_CHAPTER_INVALID'
    rules = current.post('/api/publishing/chapters/adapt', json=dict(items=rows, rules=dict(limit=20)))
    assert rules.status_code == 400 and rules.json()['code'] == 'PUBLISH_CHAPTER_INVALID'


def test_model_retry_gets_specific_grouping_validation_failure(client, monkeypatch):
    current, _ = client
    requests = []
    def chat(prompt, **kwargs):
        requests.append((prompt, kwargs))
        if len(requests) == 1:
            return json.dumps({'items': [dict(start_index=0, end_index=0, title='仅开场')]})
        return json.dumps({'items': [dict(start_index=0, end_index=1, title='完整主题')],
                           'review': dict(checked=True, summary='标题内容已审查', findings=[])})
    monkeypatch.setattr(chapters.llm, 'chat', chat)
    result = current.post('/api/publishing/chapters/adapt', json=dict(items=[
        dict(title='Intro', start_time=0, end_time=10), dict(title='Main', start_time=10, end_time=30)]))
    assert result.status_code == 200, result.text
    assert len(requests) == 2 and '遗漏末尾' in requests[1][0]
    assert result.json()['items'][0]['source_indices'] == [0, 1]


def test_transport_chapter_diagnostics_keep_only_bounded_numbers():
    result = transport._error_detail(dict(stage='validate', count=21, limit=10, index=1,
        minimum_seconds=5., actual_seconds=2.3, title='不应外泄', secret='不应外泄', headers={'Cookie': '不应外泄'}))
    assert result == dict(stage='validate', count=21, limit=10, index=1, minimum_seconds=5., actual_seconds=2.3)
    assert transport._error_detail(dict(stage='validate', count=True, limit='10', index=-1,
        minimum_seconds=float('nan'), actual_seconds=float('inf'))) == dict(stage='validate')
