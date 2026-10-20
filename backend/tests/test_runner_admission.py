"""资源准入不占执行线程；使用隔离项目和事件桩，不运行媒体模型。"""
import threading
import time

import pytest

from conftest import mark_upstream_done
from dubforge import config
from dubforge.domain import crud
from dubforge.runner import Runner


@pytest.fixture
def worker(isolated_env, monkeypatch):
    instance = Runner(max_workers=2)
    monkeypatch.setattr(instance, '_prepare_gpu', lambda *a: None)
    try:
        yield instance
    finally:
        assert instance.shutdown(timeout=5) == 0
        assert instance._admitted == 0
        assert not instance._slot_reservations
        assert not any(instance._held.values())


def until(check, timeout=4):
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() >= deadline:
            raise AssertionError('隔离任务未在预期时间完成')
        time.sleep(.01)


def gpu_jobs(worker, make_project, monkeypatch):
    from dubforge.pipeline import steps
    config.update_config({'runner.step_resources': {'preprocess': 'gpu'}, 'runner.resource_concurrency': {'gpu': 1}})
    entered, release = threading.Event(), threading.Event()
    def run(ctx):
        entered.set()
        while not release.wait(.02):
            ctx.check_cancel()
        ctx.write_json('result.json', {'ok': True})
    monkeypatch.setitem(steps._RUNNERS, 'preprocess', run)
    first, second = make_project(), make_project()
    mark_upstream_done(first, 'preprocess')
    mark_upstream_done(second, 'preprocess')
    worker.start_step(first, 'preprocess')
    assert entered.wait(3)
    worker.start_step(second, 'preprocess')
    return first, second, release


def test_gpu_waiters_do_not_occupy_download_worker(worker, make_project, monkeypatch):
    from dubforge.pipeline import steps
    first, second, release = gpu_jobs(worker, make_project, monkeypatch)
    downloaded = threading.Event()
    def download(ctx):
        downloaded.set()
        ctx.write_json('result.json', {'ok': True})
    monkeypatch.setitem(steps._RUNNERS, 'download', download)
    third = make_project()
    worker.start_step(third, 'download')
    assert downloaded.wait(3), 'GPU 等待任务不能挡住独立下载资源'
    assert crud.get_step(crud.get_project(second).id, 'preprocess').status == 'queued'
    assert worker._admitted <= 2
    release.set()
    until(lambda: not worker._futures)
    assert all(crud.get_step(crud.get_project(uid).id, 'preprocess').status == 'done' for uid in (first, second))


def test_legacy_submission_control_reproduces_head_of_line_blocking(worker, make_project, monkeypatch):
    """对照旧提交方式：两个 worker 被 GPU 运行/等待占满后，下载只能等 GPU 释放。"""
    from dubforge.pipeline import steps
    monkeypatch.setattr(worker, '_queue_execution', lambda *args: worker._pool.submit(worker._execute, *args))
    _, _, release = gpu_jobs(worker, make_project, monkeypatch)
    downloaded = threading.Event()
    monkeypatch.setitem(steps._RUNNERS, 'download', lambda ctx: downloaded.set())
    worker.start_step(make_project(), 'download')
    assert not downloaded.wait(.2)
    release.set()
    assert downloaded.wait(3)
    until(lambda: not worker._futures)


@pytest.mark.parametrize('force', [False, True])
def test_pending_cancel_and_force_stop_leave_no_reservation(worker, make_project, monkeypatch, force):
    first, second, release = gpu_jobs(worker, make_project, monkeypatch)
    project = crud.get_project(second)
    action = worker.force_stop if force else worker.cancel_step
    # 保持准入边界，确保强停状态在队列清理前已经完整提交。
    with worker._slot_cond:
        result = action(second, 'preprocess')
    assert result['status'] == ('force_stopped' if force else 'cancelling')
    until(lambda: worker.current_job_id(project.id, 'preprocess') is None)
    step = crud.get_step(project.id, 'preprocess')
    assert step.error_code == ('STEP_FORCE_STOPPED' if force else 'STEP_CANCELLED')
    assert (project.id, 'preprocess') not in worker._abandoned
    assert worker._held.get('gpu') == 1
    release.set()
    until(lambda: not worker._futures)


def test_shutdown_cleans_pending_and_running_then_restart(worker, make_project, monkeypatch):
    from dubforge.pipeline import steps
    gpu_jobs(worker, make_project, monkeypatch)
    assert worker.shutdown(timeout=5) == 0
    assert worker._admitted == 0 and not worker._slot_reservations
    assert not any(worker._held.values())
    worker.startup()
    monkeypatch.setitem(steps._RUNNERS, 'download', lambda ctx: ctx.write_json('result.json', {'ok': True}))
    uid = make_project()
    worker.start_step(uid, 'download')
    until(lambda: not worker._futures)
    assert crud.get_step(crud.get_project(uid).id, 'download').status == 'done'


def test_raising_resource_limit_admits_waiter_without_blocking_start(worker, make_project, monkeypatch):
    first, second, release = gpu_jobs(worker, make_project, monkeypatch)
    config.set_key('runner.resource_concurrency.gpu', 2)
    until(lambda: crud.get_step(crud.get_project(second).id, 'preprocess').status == 'running')
    assert worker._admitted == 2
    release.set()
    until(lambda: not worker._futures)


def test_cancel_before_execute_claims_reservation_releases_it(worker, make_project, monkeypatch):
    from dubforge.pipeline import steps
    called = []
    monkeypatch.setitem(steps._RUNNERS, 'download', lambda ctx: called.append(True))
    execute = worker._execute
    def cancel_before_claim(*args):
        args[5].set()
        return execute(*args)
    monkeypatch.setattr(worker, '_execute', cancel_before_claim)
    uid = make_project()
    worker.start_step(uid, 'download')
    until(lambda: not worker._futures)
    assert not called and not worker._slot_reservations
    assert not any(worker._held.values()) and worker._admitted == 0
    assert crud.get_step(crud.get_project(uid).id, 'download').error_code == 'STEP_CANCELLED'


def test_bad_resource_config_fails_one_job_without_killing_admission(worker, make_project, monkeypatch):
    from dubforge.pipeline import steps
    monkeypatch.setitem(steps._RUNNERS, 'download', lambda ctx: ctx.write_json('result.json', {'ok': True}))
    limit = worker.concurrency_limit
    def invalid(_):
        raise ValueError('invalid fixture')
    monkeypatch.setattr(worker, 'concurrency_limit', invalid)
    uid = make_project()
    worker.start_step(uid, 'download')
    until(lambda: not worker._futures)
    assert crud.get_step(crud.get_project(uid).id, 'download').error_code == 'RUNNER_RESOURCE_INVALID'
    assert worker._admission_thread.is_alive()
    monkeypatch.setattr(worker, 'concurrency_limit', limit)
    next_uid = make_project()
    worker.start_step(next_uid, 'download')
    until(lambda: not worker._futures)
    assert crud.get_step(crud.get_project(next_uid).id, 'download').status == 'done'
