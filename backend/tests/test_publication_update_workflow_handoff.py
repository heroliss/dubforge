"""换片后的审核与章节交给更新任务，旧流程停止轮询且保留上传历史。"""
import copy
import json

import pytest
from sqlmodel import Session

from dubforge import workflows, release_batches
from dubforge.db import get_engine
from dubforge.publishing import chapter_dispatch, service
from dubforge.publishing.models import Publication
from dubforge.workflow_models import WorkflowRun


def publication(status='published'):
    return dict(id='source', project_uid='project', status=status, progress=100, error='', revision=1,
        snapshot=dict(chapter_options=dict(enabled=True, items=[dict(title='开场', start_time=0, end_time=30)])),
        receipt=dict(bvid='BV1234567890', aid=10), execution={},
        chapter_sync=dict(status='superseded', superseded_by_update='new-update', parts=[dict(cid=1, status='failed')],
                          error='原时间轴待处理', execution=dict(phase='chapters.old', label='旧章节等待', state='failed')))


@pytest.mark.parametrize('remote_status', ['published', 'private', 'reviewed', 'submitted', 'rejected', 'collection_pending'])
def test_superseded_is_handoff_terminal_without_claiming_new_video_or_chapters_succeeded(monkeypatch, remote_status):
    value = publication(remote_status)
    original = copy.deepcopy(value)
    saved = []
    monkeypatch.setattr(workflows, '_update', lambda key, **fields: saved.append(fields))
    assert workflows._finish_publication_monitor('flow', value) is True
    assert value == original
    result = saved[-1]
    assert result['status'] == 'completed'
    assert result['phase'] == '原稿已投稿，章节已交由原稿更新任务'
    assert result['next_check'] == 0 and result['check_failures'] == 0 and result['error'] == ''
    assert result['execution']['phase'] == 'publish.update.handoff'
    assert '更新原稿件' in result['execution']['detail']
    assert value['chapter_sync']['parts'][0]['status'] == 'failed'


@pytest.mark.parametrize('remote_status', ['submitted', 'rejected', 'collection_pending'])
def test_old_monitor_stops_querying_after_handoff_and_does_not_touch_frozen_history(isolated_env, monkeypatch, remote_status):
    recipe = json.dumps(dict(mode='publish', chapters_enabled=True))
    plan = json.dumps(dict(steps=['assemble'], frozen_marker='keep-original'))
    old_chapters = json.dumps(dict(status='pending', parts=[dict(cid=1, status='pending')]))
    old_snapshot = json.dumps(dict(parts=[dict(artifact_id=100)], chapter_options=dict(enabled=True)))
    old_receipt = json.dumps(dict(bvid='BV1234567890', aid=10, remote_state=0))
    with Session(get_engine()) as session:
        session.add(Publication(id='source', project_uid='project', status='published',
                                snapshot=old_snapshot, receipt=old_receipt, chapter_sync=old_chapters))
        session.add(WorkflowRun(id='flow', request_id='flow-request', project_uid='project', publication_id='source',
                                status='monitoring', next_check=0, recipe=recipe, plan=plan, check_failures=2,
                                phase='视频已发布，章节处理中'))
        session.commit()
    value = publication(remote_status)
    reads = []
    monkeypatch.setattr(service, 'view', lambda key: reads.append(key) or copy.deepcopy(value))
    monkeypatch.setattr(service, '_running', {})
    monkeypatch.setattr(chapter_dispatch, 'scan', lambda: None)
    monkeypatch.setattr(release_batches, 'owns_publication', lambda key: False)
    monkeypatch.setattr(service, 'enqueue', lambda *args, **kwargs: pytest.fail('旧流程不能继续查询或重新投稿'))
    workflows.monitor_once()
    with Session(get_engine()) as session:
        flow = session.get(WorkflowRun, 'flow')
        source = session.get(Publication, 'source')
        assert flow.status == 'completed' and flow.next_check == 0
        assert flow.recipe == recipe and flow.plan == plan and flow.publication_id == 'source'
        assert source.status == 'published'
        assert source.chapter_sync == old_chapters and source.snapshot == old_snapshot and source.receipt == old_receipt
    count = len(reads)
    workflows.monitor_once()
    assert len(reads) == count
    workflows.chapter_status_changed('source')
    with Session(get_engine()) as session:
        assert session.get(WorkflowRun, 'flow').status == 'completed'


def test_original_pending_chapters_still_wait_and_are_not_accidentally_completed(monkeypatch):
    value = publication()
    value['chapter_sync']['status'] = 'pending'
    saved = []
    monkeypatch.setattr(workflows, '_update', lambda key, **fields: saved.append(fields))
    assert workflows._finish_publication_monitor('flow', value) is True
    assert saved[-1]['phase'] == '视频已发布，章节处理中'
    assert 'status' not in saved[-1] and saved[-1]['next_check'] > 0


def test_unapproved_original_video_without_handoff_keeps_existing_review_behavior(monkeypatch):
    value = publication('submitted')
    value['chapter_sync']['status'] = 'pending'
    monkeypatch.setattr(workflows, '_update', lambda *args, **kwargs: pytest.fail('未经审核且无交接不能标完成'))
    assert workflows._finish_publication_monitor('flow', value) is False
