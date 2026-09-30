"""状态页读取不能按句数重复查询产物索引。"""
import pytest
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
