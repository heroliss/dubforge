"""显示名称复用已读取元数据；离线预检不联网，也不改业务确认身份。"""
import json

import pytest
from sqlmodel import Session

from dubforge import config, workflow_plan
from dubforge.db import get_engine
from dubforge.publishing import display, service, transport
from dubforge.publishing.models import PublishAccount
from test_publishing import setup_publish
from test_workflows import flow


SETTINGS = dict(human_type2=1029, season_id=20, section_id=31)
METADATA = dict(human_categories=[dict(id=1000, name='科技', children=[dict(id=1029, name='人工智能')])],
                seasons=[dict(id=20, title='AI 每周动态', sections=[dict(id=31, title='新闻')]),
                         dict(id=21, title='其他合集', sections=[dict(id=31, title='另一个小节')])])


def test_names_preserve_account_and_target_binding_and_nested_category(isolated_env):
    fresh = display.resolve('123', 1, 'encrypted', SETTINGS, account_name='主播', metadata=METADATA)
    assert fresh == dict(account_id='123', account_name='主播', human_type2=1029,
                         season_id=20, section_id=31, category_title='科技 / 人工智能',
                         season_title='AI 每周动态', section_title='新闻', source='platform',
                         fetched_at=fresh['fetched_at'])
    assert fresh['fetched_at']
    display.remember('123', 1, 'encrypted', METADATA)
    cached = display.resolve('123', 1, 'encrypted', SETTINGS)
    assert cached['source'] == 'cache' and cached['fetched_at']
    assert cached['section_title'] == '新闻'
    assert display.resolve('123', 1, 'encrypted', {**SETTINGS, 'season_id': 21})['section_title'] == '另一个小节'


@pytest.mark.parametrize('account,revision,credential', [('456', 1, 'encrypted'),
                                                       ('123', 2, 'encrypted'), ('123', 1, 'new-login')])
def test_old_login_revision_and_other_account_cannot_supply_names(isolated_env, account, revision, credential):
    display.remember('123', 1, 'encrypted', METADATA)
    result = display.resolve(account, revision, credential, SETTINGS)
    assert result['source'] == 'none'
    assert not any(result[key] for key in ('category_title', 'season_title', 'section_title'))


def test_cache_does_not_cross_data_root(isolated_env, tmp_path, monkeypatch):
    display.remember('123', 1, 'encrypted', METADATA)
    monkeypatch.setattr(config, 'data_root', lambda: tmp_path / 'other-root')
    assert display.resolve('123', 1, 'encrypted', SETTINGS)['source'] == 'none'


def test_refresh_replaces_names_instead_of_claiming_missing_objects_still_exist(isolated_env):
    display.remember('123', 1, 'encrypted', METADATA)
    display.remember('123', 1, 'encrypted', dict(human_categories=[], seasons=[]))
    result = display.resolve('123', 1, 'encrypted', SETTINGS)
    assert result['source'] == 'cache' and result['season_title'] == result['category_title'] == ''


@pytest.mark.parametrize('contents', ['not-json', '[]', '{"schema": 1, "account_id":"123", "categories": [], "seasons": {}}'])
def test_invalid_display_cache_is_ignored(isolated_env, contents):
    path = display._path('123', 1, 'encrypted')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding='utf-8')
    assert display.resolve('123', 1, 'encrypted', SETTINGS)['source'] == 'none'


def test_display_write_failure_does_not_stop_refresh_or_expose_credentials(isolated_env, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError('test denied')
    monkeypatch.setattr(display, 'atomic_write_bytes', denied)
    value = display.remember('123', 1, 'private-cookie', METADATA)
    assert value['account_id'] == '123'
    assert 'private-cookie' not in json.dumps(value)
    assert display.resolve('123', 1, 'private-cookie', SETTINGS, metadata=METADATA)['season_title'] == 'AI 每周动态'


@pytest.mark.parametrize('metadata', [None, [], {'seasons': {}},
    {'human_categories': [None, {'id': 1, 'name': '科技', 'children': {}}],
     'seasons': [None, {'id': 20, 'title': '可用合集', 'sections': 'broken'}]}])
def test_invalid_metadata_names_do_not_break_display(isolated_env, metadata):
    display.remember('123', 1, 'encrypted', metadata)
    result = display.resolve('123', 1, 'encrypted', SETTINGS, metadata=metadata)
    assert result['account_id'] == '123'
    assert not result['section_title']


def test_malformed_cache_sections_are_ignored(isolated_env):
    path = display._path('123', 1, 'encrypted')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(schema=1, account_id='123', categories={},
        seasons={'20': {'title': ['not-text'], 'sections': []}})), encoding='utf-8')
    value = display.resolve('123', 1, 'encrypted', SETTINGS)
    assert value['season_title'] == value['section_title'] == ''


def test_service_plan_uses_cached_names_outside_snapshot_and_fingerprint(setup_publish):
    uid, row, _, calls = setup_publish
    before = service.plan(row['id'])
    assert before['settings_display']['source'] == 'none'
    service.metadata('123')
    after = service.plan(row['id'])
    assert after['settings_display']['season_title'] == '越南'
    assert after['settings_display']['category_title'] == '旅行'
    assert after['settings_display']['section_title'] == '默认'
    assert after['settings_display']['account_name'] == '隔离测试账号'
    assert after['settings_display']['source'] == 'cache'
    assert after['snapshot'] == before['snapshot']
    assert after['fingerprint'] == before['fingerprint']
    assert sum(operation == 'metadata' for operation, _ in calls) == 1


def test_flow_online_reuses_metadata_once_and_offline_uses_label_cache(flow, monkeypatch):
    uid, spec, calls, _ = flow
    calls.clear()
    online = workflow_plan.plan(uid, spec)
    assert online['settings_display']['source'] == 'platform'
    assert online['settings_display']['season_title'] == '越南'
    assert sum(operation == 'metadata' for operation, _ in calls) == 1
    calls.clear()
    offline = workflow_plan.plan(uid, spec, online=False)
    assert offline['settings_display']['source'] == 'cache'
    assert offline['settings_display']['season_title'] == '越南'
    assert not calls
    assert online['fingerprint'] == offline['fingerprint']
    original = transport.call
    def renamed(operation, *args, **kwargs):
        result = original(operation, *args, **kwargs)
        if operation == 'metadata':
            result['seasons'][0]['title'] = '越南旅行'
        return result
    monkeypatch.setattr(transport, 'call', renamed)
    refreshed = workflow_plan.plan(uid, spec)
    assert refreshed['settings_display']['season_title'] == '越南旅行'
    assert refreshed['fingerprint'] == online['fingerprint']


def test_service_cache_is_invalidated_by_current_account_credentials(setup_publish):
    _, row, _, _ = setup_publish
    service.metadata('123')
    with Session(get_engine()) as session:
        account = session.get(PublishAccount, '123')
        account.credential = 'reconnected-credential'
        session.add(account)
        session.commit()
    result = service.plan(row['id'])['settings_display']
    assert result['source'] == 'none' and not result['season_title']
