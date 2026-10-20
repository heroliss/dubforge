"""关闭边界回归：隔离数据、伪步骤与伪 LLM，不结束真实服务。"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from conftest import mark_upstream_done, wait_status
from dubforge import comment_studio as cs, config
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.runner import Runner
from test_comment_studio import fake_translation, seed


def test_shutdown_cancels_queued_and_inflight_preserving_completed_artifacts(make_project, monkeypatch):
    from dubforge.pipeline import steps

    worker = Runner(max_workers=1)
    entered, release = threading.Event(), threading.Event()
    called = []
    first = make_project(params={'_chain': ['split', 'translate']})
    queued = make_project()
    fresh = make_project()
    for uid in (first, queued, fresh):
        mark_upstream_done(uid, 'split')

    def run(ctx):
        called.append(ctx.project.uid)
        ctx.write_bytes('completed-checkpoint.txt', b'completed')
        entered.set()
        assert release.wait(3)
        ctx.check_cancel()

    monkeypatch.setitem(steps._RUNNERS, 'split', run)
    try:
        worker.start_step(first, 'split')
        assert entered.wait(3)
        worker.start_step(queued, 'split')
        started = time.monotonic()
        worker.request_shutdown()
        assert worker.wait_for_shutdown(.02) == 1
        assert time.monotonic() - started < 1
        with pytest.raises(DubForgeError) as error:
            worker.start_step(fresh, 'split')
        assert error.value.code == 'RUNNER_SHUTTING_DOWN'
        with pytest.raises(DubForgeError) as error:
            worker.startup()
        assert error.value.code == 'RUNNER_FINALIZING'
        queued_project = crud.get_project(queued)
        assert crud.get_step(queued_project.id, 'split').error_code == 'STEP_INTERRUPTED'
        assert crud.latest_job(queued_project.id, 'split').status == 'cancelled'
        release.set()
        assert worker.wait_for_shutdown(3) == 0
        assert called == [first]
        project = crud.get_project(first)
        step = crud.get_step(project.id, 'split')
        assert step.error_code == 'STEP_INTERRUPTED'
        assert crud.latest_job(project.id, 'split').status == 'cancelled'
        assert (crud.project_dir(first) / 'artifacts/split/completed-checkpoint.txt').read_bytes() == b'completed'
        assert crud.project_params(project)['_chain_state'] == 'held'
        assert crud.project_params(project)['_chain_hold']['code'] == 'STEP_INTERRUPTED'
        # 关闭是幂等的；新的应用生命周期可重新开放一个已收尾的执行器。
        assert worker.shutdown(.01) == 0
        worker.startup()
        monkeypatch.setitem(steps._RUNNERS, 'split', lambda ctx: ctx.write_bytes('split.txt', b'1. 0-1: hello'))
        worker.start_step(fresh, 'split')
        wait_status(fresh, 'split', 'done')
    finally:
        release.set()
        worker.shutdown(3)


def test_shutdown_wakes_worker_waiting_for_resource_slot(make_project, monkeypatch):
    from dubforge.pipeline import steps

    config.set_key('runner.step_concurrency.split', 1)
    worker = Runner(max_workers=2)
    entered, release = threading.Event(), threading.Event()
    called = []
    first, waiting = make_project(), make_project()
    for uid in (first, waiting):
        mark_upstream_done(uid, 'split')

    def run(ctx):
        called.append(ctx.project.uid)
        entered.set()
        assert release.wait(3)
        ctx.check_cancel()

    monkeypatch.setitem(steps._RUNNERS, 'split', run)
    try:
        worker.start_step(first, 'split')
        assert entered.wait(3)
        worker.start_step(waiting, 'split')
        worker.request_shutdown()
        release.set()
        assert worker.wait_for_shutdown(3) == 0
        assert called == [first]
        assert crud.get_step(crud.get_project(waiting).id, 'split').error_code == 'STEP_INTERRUPTED'
        assert worker._held.get('split', 0) == 0
    finally:
        release.set()
        worker.shutdown(3)


def test_comment_shutdown_interrupts_request_and_resume_only_missing(make_project, monkeypatch):
    uid = make_project()
    seed(uid, 20)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def translate(prompt, **kwargs):
        calls.append(prompt)
        if len(calls) == 2:
            entered.set()
            assert release.wait(3)
            kwargs['cancel_check']()
        return fake_translation(prompt, **kwargs)

    monkeypatch.setattr(cs, 'chat_validated', translate)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(cs.run, uid, 'translate')
            assert entered.wait(3)
            cs.request_shutdown()
            assert cs.wait_for_shutdown(.02) == 1
            with pytest.raises(cs.CommentError) as error:
                cs.run(make_project(), 'translate')
            assert error.value.code == 'COMMENTS_INTERRUPTED'
            release.set()
            with pytest.raises(cs.CommentError) as error:
                future.result(timeout=3)
            assert error.value.code == 'COMMENTS_INTERRUPTED'
        assert cs.wait_for_shutdown(3) == 0
        state = cs.view(uid)
        assert state['job']['status'] == 'interrupted'
        assert state['job']['processed'] == 10
        assert len([row for row in state['rows'] if row['text_zh']]) == 10
        assert len(calls) == 2
        cs.startup()
        monkeypatch.setattr(cs, 'chat_validated', fake_translation)
        resumed = cs.run(uid, 'translate')
        assert resumed['job']['cached'] == 10
        assert resumed['job']['processed'] == 10
        assert all(row['text_zh'] for row in resumed['rows'])
    finally:
        release.set()
        cs.shutdown(3)
        cs.startup()


def test_comment_closing_between_entry_and_job_registration_cannot_start_llm(make_project, monkeypatch):
    uid = make_project()
    seed(uid)
    view = cs.view
    calls = []

    def close_during_view(*args, **kwargs):
        result = view(*args, **kwargs)
        cs.request_shutdown()
        return result

    monkeypatch.setattr(cs, 'view', close_during_view)
    monkeypatch.setattr(cs, 'chat_validated', lambda *args, **kwargs: calls.append(args))
    try:
        with pytest.raises(cs.CommentError) as error:
            cs.run(uid, 'translate')
        assert error.value.code == 'COMMENTS_INTERRUPTED'
        assert not calls and uid not in cs._cancels
    finally:
        cs.startup()


def test_lifespan_closes_admission_before_waiting_with_shared_budget(isolated_env, monkeypatch):
    from app.main import app
    from dubforge import workflows
    from dubforge.publishing import service
    from dubforge.runner import runner

    events = []
    for obj, names in [(runner, ('startup', 'recover', 'request_shutdown')),
                       (cs, ('startup', 'request_shutdown')),
                       (workflows, ('recover', 'start_monitor', 'shutdown')),
                       (service, ('recover', 'interrupt_all'))]:
        for name in names:
            monkeypatch.setattr(obj, name, lambda *args, label=name, **kwargs: events.append(label))
    budgets = []
    def wait(label, timeout):
        events.append(label)
        budgets.append(timeout)
        return 0
    monkeypatch.setattr(runner, 'wait_for_shutdown', lambda timeout: wait('wait_steps', timeout))
    monkeypatch.setattr(cs, 'wait_for_shutdown', lambda timeout: wait('wait_comments', timeout))
    with TestClient(app) as client:
        assert client.get('/api/health').status_code == 200
    assert events[-6:] == ['request_shutdown', 'request_shutdown', 'shutdown', 'interrupt_all',
                          'wait_steps', 'wait_comments']
    assert 0 <= budgets[1] <= budgets[0] <= 5
