"""发布编辑与制作共用门禁；全部采用隔离账号和伪网络。"""
import pytest
import threading
from concurrent.futures import ThreadPoolExecutor
from sqlmodel import Session

from dubforge import configuration, runtime_edit
from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.publishing import service
from dubforge.publishing.models import Publication
from test_publishing import setup_publish, finish


def set_step_status(project, status):
    step = crud.get_step(project.id, 'split')
    step.status = status
    crud.save_step(step)


@pytest.mark.parametrize('status', ['queued', 'running'])
@pytest.mark.parametrize('operation', ['save', 'copy', 'create', 'upload'])
def test_publication_edits_and_upload_wait_for_project_execution(setup_publish, status, operation):
    uid, row, _, calls = setup_publish
    preview = service.plan(row['id'])
    project = crud.get_project(uid)
    set_step_status(project, status)
    before = service.view(row['id'])
    with pytest.raises(DubForgeError) as caught:
        if operation == 'save':
            service.save(row['id'], dict(row['draft'], revision=row['revision']))
        elif operation == 'copy':
            service.copy_draft(row['id'])
        elif operation == 'create':
            service.create(uid)
        else:
            service.enqueue(row['id'], dict(action='upload', revision=row['revision'],
                                            fingerprint=preview['fingerprint']))
    assert caught.value.code == 'CONFIGURATION_BUSY'
    assert service.view(row['id']) == before
    assert calls == []


def test_publication_can_edit_while_unrelated_project_runs(setup_publish, make_project):
    _, row, _, calls = setup_publish
    other = crud.get_project(make_project())
    set_step_status(other, 'running')
    saved = service.save(row['id'], dict(row['draft'], revision=row['revision'],
                         overrides={**row['draft']['overrides'], 'title': '闲置项目的新标题'}))
    assert saved['settings']['title'] == '闲置项目的新标题'
    assert calls == []


def test_upload_preflight_locks_affected_settings_without_holding_global_gate(setup_publish, make_project, monkeypatch):
    uid, row, _, _ = setup_publish
    preview = service.plan(row['id'])
    other = make_project()
    entered, release = threading.Event(), threading.Event()

    def slow_plan(_key, *, progress=None):
        entered.set()
        assert release.wait(5)
        return preview

    monkeypatch.setattr(service, 'plan', slow_plan)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.enqueue, row['id'], dict(action='upload', revision=row['revision'],
                             fingerprint=preview['fingerprint']))
        try:
            assert entered.wait(5)
            # ADR-0132：上传预检先占用项目级资源，但尚未锁住设置字段；
            # 只有与活动步骤相交的字段写入才由 locked/locked_paths 拦截。
            report = runtime_edit.editability('project:' + uid)
            assert report['locked'] is False
            assert report['resource_locked'] is True
            assert uid in report['resource_project_uids']
            # 预检占用的是发布资源，不是设置字段；无关的制作参数仍可保存。
            saved_global = configuration.save('global', dict(revision=0, updates={'assemble.crf': 24}))
            assert saved_global['values']['assemble.crf'] == 24
            saved = configuration.save('project:' + other, dict(revision=0, updates={'assemble.crf': 24}))
            assert saved['values']['assemble.crf'] == 24
        finally:
            release.set()
        future.result(timeout=5)
    assert finish(row['id'])['status'] == 'uploaded'


def test_failed_preflight_releases_prepare_occupancy(setup_publish):
    uid, row, _, calls = setup_publish
    with pytest.raises(service.PublishError):
        service.enqueue(row['id'], dict(action='upload', revision=row['revision'], fingerprint='changed'))
    assert row['id'] not in service._running
    assert not runtime_edit.editability('project:' + uid)['locked']
    assert calls == []


def test_preflight_does_not_block_other_publication_controls_and_cancel_never_queues(setup_publish, make_project, monkeypatch):
    uid, row, _, calls = setup_publish
    preview = service.plan(row['id'])
    other = service.create(make_project())
    other_cancel = threading.Event()
    entered, release = threading.Event(), threading.Event()
    submitted = []

    def slow_plan(_key, *, progress=None):
        entered.set()
        assert release.wait(5)
        return preview

    monkeypatch.setattr(service, 'plan', slow_plan)
    monkeypatch.setattr(service._pool, 'submit', lambda *args: submitted.append(args))
    with service._lock:
        service._running[other['id']] = other_cancel
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(service.enqueue, row['id'], dict(action='upload', revision=row['revision'],
                                  fingerprint=preview['fingerprint']))
            try:
                assert entered.wait(5)
                def other_controls():
                    service.pause(other['id'])
                    service._update(other['id'], error='其他稿件进度仍可写入')
                pool.submit(other_controls).result(timeout=2)
                assert other_cancel.is_set()
                assert service.view(other['id'])['error'] == '其他稿件进度仍可写入'
                # 同稿件幂等保护、同项目制作占用依然存在。
                with pytest.raises(service.PublishError):
                    service.enqueue(row['id'], dict(action='upload', revision=row['revision'],
                                    fingerprint=preview['fingerprint']))
                with pytest.raises(DubForgeError) as occupied:
                    service.create(uid)
                assert occupied.value.code == 'CONFIGURATION_BUSY'
                assert runtime_edit.editability('project:' + uid)['resource_locked']
                service.pause(row['id'])
            finally:
                release.set()
            with pytest.raises(service.PublishError) as caught:
                pending.result(timeout=5)
            assert caught.value.code == 'PUBLISH_PREPARATION_CANCELLED'
        current = service.view(row['id'])
        assert current['status'] == 'draft' and not current['snapshot']
        assert row['id'] not in service._running
        assert submitted == [] and calls == []
    finally:
        with service._lock:
            service._running.pop(other['id'], None)


def test_queue_failure_restores_retryable_record_and_releases_occupancy(setup_publish, monkeypatch):
    _, row, _, calls = setup_publish
    preview = service.plan(row['id'])
    original_submit = service._pool.submit
    def unavailable(*args, **kwargs):
        raise RuntimeError('隔离队列拒绝')
    monkeypatch.setattr(service._pool, 'submit', unavailable)
    body = dict(action='upload', revision=row['revision'], fingerprint=preview['fingerprint'])
    with pytest.raises(service.PublishError) as caught:
        service.enqueue(row['id'], body)
    assert caught.value.code == 'PUBLISH_QUEUE_UNAVAILABLE'
    current = service.view(row['id'])
    assert current['status'] == 'draft' and not current['snapshot']
    assert row['id'] not in service._running and calls == []
    monkeypatch.setattr(service._pool, 'submit', original_submit)
    service.enqueue(row['id'], body)
    assert finish(row['id'])['status'] == 'uploaded'


def test_platform_subtitle_import_waits_for_publication_upload(setup_publish):
    from dubforge.publishing import imports
    uid, row, _, calls = setup_publish
    staged = imports.stage('123', 'subtitle', {'lines': [{'start': 0, 'end': 1, 'text': '测试字幕'}]})
    with Session(get_engine()) as session:
        publication = session.get(Publication, row['id'])
        publication.status = 'uploading'
        session.add(publication)
        session.commit()
    with pytest.raises(DubForgeError) as caught:
        imports.apply('123', dict(import_key=staged['import_key'], project_uid=uid, confirmed=True))
    assert caught.value.code == 'CONFIGURATION_BUSY'
    assert not (crud.project_dir(uid) / 'imports/bilibili_subtitle.json').exists()
    assert calls == []
