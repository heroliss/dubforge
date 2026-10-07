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

    def slow_plan(_key):
        entered.set()
        assert release.wait(5)
        return preview

    monkeypatch.setattr(service, 'plan', slow_plan)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.enqueue, row['id'], dict(action='upload', revision=row['revision'],
                             fingerprint=preview['fingerprint']))
        try:
            assert entered.wait(5)
            assert runtime_edit.editability('project:' + uid)['locked']
            with pytest.raises(DubForgeError) as caught:
                configuration.save('global', dict(revision=0, updates={'assemble.crf': 24}))
            assert caught.value.code == 'CONFIGURATION_BUSY'
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
