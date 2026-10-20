"""状态页读取不能按句数重复查询产物索引。"""
import pytest
import threading
import time
from fastapi import Response

from dubforge.domain import crud
from dubforge.utils.fs import atomic_write_bytes


@pytest.mark.parametrize('count', [2, 200])
def test_step_status_batches_artifact_queries(make_project, monkeypatch, count):
    from app.api.steps import list_steps
    uid = make_project()
    project = crud.get_project(uid)
    root = crud.project_dir(uid)
    for key, name, content in [
        ('split', 'split.txt', '\n'.join(f'{i}. {i*2:.3f}-{i*2+1:.3f}: hello' for i in range(1, count + 1))),
        ('translate', 'translate.txt', '\n'.join(f'{i}. 你好' for i in range(1, count + 1))),
    ]:
        path = root / 'artifacts' / key / name
        atomic_write_bytes(path, content.encode())
        crud.add_artifact(project.id, key, 'text', name, path, str(path.relative_to(root)))
        step = crud.get_step(project.id, key); step.status = 'done'; crud.save_step(step)
    calls = []
    original = crud.latest_artifacts
    def tracked(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(crud, 'latest_artifacts', tracked)
    view = list_steps(uid, Response())
    assert len(view) == 8
    assert len(calls) <= 10, f'{count} 句触发了 {len(calls)} 次产物查询'
    assert next(s for s in view if s['key'] == 'assemble')['unready_inputs']


@pytest.mark.parametrize('scope', ['global', 'project'])
def test_parameter_schema_uses_one_config_snapshot(make_project, monkeypatch, scope):
    from dubforge import config
    from app.api.settings import step_params
    from app.api.projects import get_project_step_params

    uid = make_project()
    config.set_key('llm.profiles', [{'name': '测试模型'}])
    reads = []
    original = config.load_config

    def tracked():
        reads.append(1)
        return original()

    monkeypatch.setattr(config, 'load_config', tracked)
    result = step_params() if scope == 'global' else get_project_step_params(uid)
    assert result['ok']
    assert len(reads) == 1


def test_project_view_reuses_step_and_directory_snapshot(make_project, monkeypatch):
    from app.api.projects import _project_view

    uid = make_project()
    project = crud.get_project(uid)
    queries = []
    original = crud.get_steps

    def tracked(project_id):
        queries.append(project_id)
        return original(project_id)

    def unexpected_lookup(*args):
        raise AssertionError('已有项目对象，不应重复查询项目目录')

    monkeypatch.setattr(crud, 'get_steps', tracked)
    monkeypatch.setattr(crud, 'project_dir', unexpected_lookup)
    result = _project_view(project)
    assert result['dir_exists']
    assert len(queries) == 1


def test_step_status_single_flight_shares_running_read(monkeypatch):
    """同时打开步骤页/总览时，同一项目只执行一次昂贵状态核对。"""
    import app.api.steps as steps_api

    calls = []
    started = threading.Event()
    release = threading.Event()

    def fake_build(uid, response):
        calls.append(uid)
        started.set()
        assert release.wait(5)
        return [{'key': 'assemble', 'status': 'running'}]

    monkeypatch.setattr(steps_api, '_build_step_status', fake_build)
    results = []

    def read():
        results.append(steps_api.list_steps('single-flight', Response()))

    workers = [threading.Thread(target=read) for _ in range(4)]
    workers[0].start()
    assert started.wait(2)
    for worker in workers[1:]:
        worker.start()
    time.sleep(.05)
    release.set()
    for worker in workers:
        worker.join(5)

    assert calls == ['single-flight']
    assert len(results) == 4
    assert all(result == results[0] for result in results)


def test_step_status_single_flight_propagates_failure(monkeypatch):
    import app.api.steps as steps_api

    started = threading.Event()
    release = threading.Event()

    def fake_build(uid, response):
        started.set()
        assert release.wait(5)
        raise RuntimeError('status fixture failed')

    monkeypatch.setattr(steps_api, '_build_step_status', fake_build)
    errors = []

    def read():
        try:
            steps_api.list_steps('single-flight-error', Response())
        except Exception as exc:  # noqa: BLE001 - verify same domain failure path
            errors.append(exc)

    owner = threading.Thread(target=read)
    follower = threading.Thread(target=read)
    owner.start()
    assert started.wait(2)
    follower.start()
    release.set()
    owner.join(5)
    follower.join(5)

    assert len(errors) == 2
    assert all(isinstance(error, RuntimeError) for error in errors)


def test_running_assemble_status_skips_expensive_freshness_check(make_project, monkeypatch):
    """合成运行中只读即时状态，完整 freshness 留到任务结束后的下一轮。"""
    from app.api.steps import list_steps
    from dubforge.pipeline.dependencies import Dependencies

    uid = make_project()
    project = crud.get_project(uid)
    step = crud.get_step(project.id, 'assemble')
    step.status = 'running'
    crud.save_step(step)
    checked = []
    original = Dependencies.freshness_flags

    def tracked(self, key):
        checked.append(key)
        if key == 'assemble':
            raise AssertionError('运行中的合成不应触发成片 freshness 核对')
        return original(self, key)

    monkeypatch.setattr(Dependencies, 'freshness_flags', tracked)
    view = {row['key']: row for row in list_steps(uid, Response())}
    assert view['assemble']['status'] == 'running'
    assert view['assemble']['fresh'] is False
    assert view['assemble']['freshness_label'] == '正在更新'
    assert 'assemble' not in checked


def test_step_status_single_flight_timeout_uses_stable_domain_error(monkeypatch):
    import app.api.steps as steps_api
    from dubforge.errors import DubForgeError

    started = threading.Event()
    release = threading.Event()

    def fake_build(uid, response):
        started.set()
        assert release.wait(5)
        return []

    monkeypatch.setattr(steps_api, '_build_step_status', fake_build)
    monkeypatch.setattr(steps_api, '_STATUS_SINGLE_FLIGHT_WAIT', .01)
    owner = threading.Thread(target=lambda: steps_api.list_steps('single-flight-timeout', Response()))
    owner.start()
    assert started.wait(2)
    with pytest.raises(DubForgeError, match='步骤状态正在核对') as failure:
        steps_api.list_steps('single-flight-timeout', Response())
    assert failure.value.code == 'STEP_STATUS_BUSY'
    release.set()
    owner.join(5)
