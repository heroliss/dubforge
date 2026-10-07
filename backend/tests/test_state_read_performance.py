"""状态读取只复用一次请求内的结构；文件和新请求的有效性检查仍然保留。"""
import json
from pathlib import Path
from types import SimpleNamespace

from conftest import mark_upstream_done
from dubforge import config, configuration
from dubforge.domain import crud
from dubforge.pipeline import assembly_versions as av, dubbing_edit
from dubforge.pipeline.dependencies import Dependencies
from dubforge.utils.fs import atomic_write_bytes


def definition():
    return dict(id='read-test', name='读取测试', dubbing=False, burn_subtitles=False,
                include_comments=False, overrides={})


def test_path_resolution_reused_but_same_request_deleted_file_is_not_ready(make_project, monkeypatch):
    uid = make_project()
    mark_upstream_done(uid, 'preprocess')
    deps = Dependencies(crud.get_project(uid))
    original, calls = Path.resolve, []

    def resolve(path, *args, **kwargs):
        if path.name == 'video.mp4':
            calls.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'resolve', resolve)
    path = deps.path('download', 'video.mp4')
    assert path == deps.path('download', 'video.mp4') and len(calls) == 1
    path.unlink()
    assert next(row for row in deps.input_view('preprocess') if row['name'] == 'video.mp4')['state'] == 'deleted'


def test_version_defaults_reuse_request_configuration_but_new_request_reads_changes(make_project, monkeypatch):
    uid = make_project()
    config.set_key('assemble.crf', 22)
    deps = Dependencies(crud.get_project(uid))
    original, calls = configuration.resolve, []

    def resolve(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(configuration, 'resolve', resolve)
    first = av.base_values(deps)
    assert av.base_values(deps) == first and len(calls) == 1
    config.set_key('assemble.crf', 35)
    assert av.base_values(deps)[0]['assemble.crf'] == 22
    assert av.base_values(Dependencies(crud.get_project(uid)))[0]['assemble.crf'] == 35


def test_production_choices_reused_across_versions_but_changed_selection_recomputed(make_project, monkeypatch):
    uid = make_project()
    mark_upstream_done(uid, 'tts')
    project = crud.get_project(uid)
    deps = Dependencies(project)
    original, calls = dubbing_edit.production_lines, []

    def production(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(dubbing_edit, 'production_lines', production)
    lines = deps.production_lines()
    version = av.resolve(deps, definition())
    assert deps.for_version(version).production_lines() == lines and len(calls) == 1
    params = crud.project_params(project)
    params['dubbing_edit'] = {str(lines[0]['id']): dict(identity=dubbing_edit.identity(lines[0]), source='original', take=None)}
    project.params = json.dumps(params)
    assert deps.production_lines()[0]['source'] == 'original' and len(calls) == 2


def test_reused_version_records_keep_strict_hash_checks_and_new_request_registry(make_project, monkeypatch):
    uid = make_project()
    mark_upstream_done(uid, 'assemble')
    project, root = crud.get_project(uid), crud.project_dir(uid)
    deps = Dependencies(project)
    version = av.resolve(deps, definition())
    path = root / 'output/final_version_read-test.mp4'
    atomic_write_bytes(path, b'stable isolated bytes')
    crud.add_artifact(project.id, 'assemble', 'video', path.name, path, str(path.relative_to(root)))
    av.record_result(SimpleNamespace(project=project), version, path.name, av.fingerprint(deps, version))
    deps = Dependencies(project)
    original, calls = av.results, []

    def results(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(av, 'results', results)
    assert av.is_current(deps, version, verify_hash=True)
    assert av.is_current(deps, version, verify_hash=True) and len(calls) == 1
    atomic_write_bytes(path, b'changed stolen bytes!')  # 同大小篡改也必须重新做严格哈希核验。
    assert not av.is_current(deps, version, verify_hash=True)
    assert av.result_artifact(Dependencies(project), version) is not None and len(calls) == 2
