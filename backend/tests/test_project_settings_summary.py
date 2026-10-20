"""卡片摘要不能重新解析每个项目、泄漏参数或冒充发布预检。"""
import json

import pytest
from sqlalchemy import event
from sqlmodel import Session

from dubforge import configuration
from dubforge.configuration_models import ConfigurationScope
from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.project_settings_summary import project_summaries
from dubforge.publishing.models import Publication, PublishAccount
from test_configuration import bind, save


def test_summary_uses_applied_baseline_and_keeps_project_overrides(make_project):
    uid = make_project()
    template = configuration.create_template('历史系列')
    save(template['key'], {'run.mode': 'draft', 'translate.style_prompt': '私人提示不应出现在卡片中'})
    bind([uid], template)
    save('project:' + uid, {'run.mode': 'upload'})
    save(template['key'], {'run.mode': 'publish'})
    summary = project_summaries([crud.get_project(uid)])[uid]
    assert summary['known'] and summary['template_name'] == '历史系列'
    assert summary['template_id'] == template['key'][7:]
    assert summary['override_count'] == 1 and summary['delivery_mode'] == 'upload'
    assert summary['target_kind'] == 'new'
    assert summary['target_selection']['source'] == 'first_publish'
    assert '私人提示' not in json.dumps(summary, ensure_ascii=False)


def test_archived_template_does_not_hide_valid_frozen_project_settings(make_project):
    uid = make_project()
    template = configuration.create_template('旧系列')
    bind([uid], template)
    with Session(get_engine()) as session:
        row = session.get(ConfigurationScope, template['key'])
        row.archived = True
        session.add(row); session.commit()
    summary = project_summaries([crud.get_project(uid)])[uid]
    assert summary['known'] and summary['template_archived']
    assert summary['template_name'] == '旧系列'


@pytest.mark.parametrize('field,raw', [('values', '{broken'), ('baseline', '{broken'), ('baseline', '{"schema":99,"values":{}}')])
def test_unreadable_configuration_never_claims_a_default_delivery_target(make_project, field, raw):
    uid = make_project()
    with Session(get_engine()) as session:
        row = session.get(ConfigurationScope, 'project:' + uid)
        setattr(row, field, raw)
        session.add(row); session.commit()
    summary = project_summaries([crud.get_project(uid)])[uid]
    assert not summary['known'] and summary['delivery_mode'] is None
    assert summary['target_kind'] == '' and summary['account_id'] == ''


def test_exact_original_publication_receipt_and_safe_account_name(make_project):
    uid, other = make_project(), make_project()
    with Session(get_engine()) as session:
        session.add(PublishAccount(id='fixture-account', name='测试账号', credential='fixture-secret'))
        session.add(Publication(id='target', project_uid=uid, receipt='{"bvid":"BVExact"}'))
        session.add(Publication(id='other', project_uid=other, receipt='{"bvid":"BVWrong"}'))
        session.commit()
    save('project:' + uid, {'publish.account_id': 'fixture-account', 'publish.project_target': {
        'kind': 'existing', 'publication_id': 'target', 'replace_videos': False, 'replacements': [], 'sync_fields': ['chapters']}})
    summary = project_summaries([crud.get_project(uid)])[uid]
    assert summary['bvid'] == 'BVExact' and summary['account_name'] == '测试账号'
    assert 'fixture-secret' not in json.dumps(summary)
    # 模拟手工恢复的错误目标登记；只读摘要不能借其他项目回执补齐BV。
    with Session(get_engine()) as session:
        row = session.get(ConfigurationScope, 'project:' + uid)
        values = json.loads(row.values)
        values['publish.project_target']['publication_id'] = 'other'
        row.values = json.dumps(values)
        session.add(row); session.commit()
    assert project_summaries([crud.get_project(uid)])[uid]['bvid'] == ''


def test_list_summary_batches_database_reads_without_configuration_resolve(make_project, monkeypatch):
    projects = [crud.get_project(make_project()) for _ in range(8)]
    monkeypatch.setattr(configuration, 'resolve', lambda *_args, **_kwargs: pytest.fail('卡片摘要不应逐项目resolve'))
    calls = []
    engine = get_engine()
    def count(*_args): calls.append(1)
    event.listen(engine, 'before_cursor_execute', count)
    try:
        result = project_summaries(projects)
    finally:
        event.remove(engine, 'before_cursor_execute', count)
    assert len(result) == 8 and len(calls) == 3
    assert project_summaries([]) == {}


@pytest.mark.parametrize('updates', [{'run.mode': ['bad']}, {'run.mode': 'unknown'}])
def test_invalid_restored_delivery_mode_is_unknown_instead_of_breaking_list(make_project, updates):
    uid = make_project()
    with Session(get_engine()) as session:
        row = session.get(ConfigurationScope, 'project:' + uid)
        row.values = json.dumps(updates)
        session.add(row); session.commit()
    assert project_summaries([crud.get_project(uid)])[uid]['known'] is False


def test_invalid_restored_parent_does_not_impersonate_global_default(make_project):
    uid = make_project()
    with Session(get_engine()) as session:
        row = session.get(ConfigurationScope, 'project:' + uid)
        row.parent = 'project:some-other-project'
        session.add(row); session.commit()
    assert project_summaries([crud.get_project(uid)])[uid]['known'] is False
