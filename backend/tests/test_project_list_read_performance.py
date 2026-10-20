"""项目空间只合并本轮读取，不以过期缓存替代真实制作状态。"""
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import Request, Response

from conftest import mark_upstream_done
from dubforge.domain import crud
from dubforge.pipeline import dubbing_edit, voice_timing
from dubforge.pipeline.dependencies import Dependencies
from dubforge.utils.fs import atomic_write_bytes


def test_context_is_small_and_does_not_read_project_state(isolated_env, monkeypatch):
    from app.api import projects
    from dubforge import config, configuration

    def forbidden(*_args, **_kwargs):
        pytest.fail('工作区身份不应查询项目、解析配置或检查制作产物')

    monkeypatch.setattr(crud, 'list_projects', forbidden)
    monkeypatch.setattr(configuration, 'resolve', forbidden)
    monkeypatch.setattr(config, 'load_config', forbidden)
    response = Response()
    first = asyncio.run(projects.projects_context(response))
    assert response.headers['cache-control'] == 'no-store'
    assert len(first['workspace_id']) == 24
    assert asyncio.run(projects.projects_context(Response())) == first
    config._set_runtime_data_root(isolated_env / 'other-workspace')
    assert asyncio.run(projects.projects_context(Response())) != first
    assert not (isolated_env / 'other-workspace').exists()


def test_list_context_header_matches_context_route(make_project):
    from fastapi.testclient import TestClient
    from app.main import app
    make_project()
    client = TestClient(app)
    context = client.get('/api/projects/context')
    listing = client.get('/api/projects')
    assert context.status_code == listing.status_code == 200
    assert listing.headers['x-dubforge-workspace'] == context.json()['workspace_id']
    assert listing.headers['cache-control'] == 'no-store'


def test_list_shares_only_in_flight_same_workspace_reads(isolated_env, monkeypatch):
    from app.api import projects
    calls, results = [], []
    started, release = threading.Event(), threading.Event()

    def build(archive):
        calls.append(archive)
        started.set()
        assert release.wait(3)
        return [{'uid': 'fixture', 'steps': []}]

    monkeypatch.setattr(projects, '_build_project_list', build)
    threads = [threading.Thread(target=lambda: results.append(projects.list_projects())) for _ in range(4)]
    threads[0].start()
    assert started.wait(2)
    for thread in threads[1:]:
        thread.start()
    time.sleep(.05)
    release.set()
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive()
    assert calls == ['active'] and len(results) == 4
    results[0][0]['steps'].append('changed by caller')
    assert sum(bool(result[0]['steps']) for result in results) == 1
    projects.list_projects()
    assert calls == ['active', 'active']


def test_failed_list_read_is_not_cached(isolated_env, monkeypatch):
    from app.api import projects
    calls = []

    def build(archive):
        calls.append(archive)
        raise ValueError('isolated read failure')

    monkeypatch.setattr(projects, '_build_project_list', build)
    for _ in range(2):
        with pytest.raises(ValueError, match='isolated read failure'):
            projects.list_projects()
    assert len(calls) == 2 and not projects._list_flights


def test_after_mutation_refresh_does_not_join_older_pending_list(isolated_env, monkeypatch):
    from app.api import projects
    started, release = threading.Event(), threading.Event()
    old_results, calls = [], []

    def build(_archive):
        calls.append(1)
        if len(calls) == 1:
            started.set()
            assert release.wait(3)
            return [{'uid': 'deleted-before-refresh'}]
        return [{'uid': 'current-after-mutation'}]

    monkeypatch.setattr(projects, '_build_project_list', build)
    owner = threading.Thread(target=lambda: old_results.append(projects.list_projects()))
    owner.start()
    assert started.wait(2)
    request = Request({'type': 'http', 'headers': [(b'x-dubforge-refresh', b'fresh')]})
    try:
        fresh = projects.list_projects(request=request)
        assert fresh == [{'uid': 'current-after-mutation'}]
        assert len(calls) == 2
        # 原慢请求尚未结束，下一次普通轮询也不能再加入它。
        assert projects.list_projects() == fresh
        assert len(calls) == 3
    finally:
        release.set()
        owner.join(3)
    assert old_results == [[{'uid': 'deleted-before-refresh'}]]
    assert fresh == [{'uid': 'current-after-mutation'}]


def test_list_wait_timeout_has_stable_domain_error(isolated_env, monkeypatch):
    from app.api import projects
    from dubforge.errors import DubForgeError
    started, release = threading.Event(), threading.Event()

    def build(_archive):
        started.set()
        assert release.wait(3)
        return []

    monkeypatch.setattr(projects, '_build_project_list', build)
    monkeypatch.setattr(projects, '_LIST_SINGLE_FLIGHT_WAIT', .01)
    owner = threading.Thread(target=projects.list_projects)
    owner.start()
    assert started.wait(2)
    try:
        with pytest.raises(DubForgeError) as error:
            projects.list_projects()
        assert error.value.code == 'PROJECT_LIST_BUSY'
    finally:
        release.set()
        owner.join(3)


def test_list_batches_workflow_status_instead_of_per_card_lookup(make_project, monkeypatch):
    from app.api import projects
    uids = [make_project(), make_project()]
    monkeypatch.setattr(projects, '_workflow_status_of',
                        lambda *_: pytest.fail('项目列表不能逐卡查询工作流'))
    assert {row['uid'] for row in projects.list_projects()} == set(uids)


def test_take_snapshot_reuses_parse_but_reads_changed_or_deleted_record(make_project, monkeypatch):
    uid = make_project()
    mark_upstream_done(uid, 'assemble')
    project, root = crud.get_project(uid), crud.project_dir(uid)
    deps = Dependencies(project)
    row = deps.production_lines()[0]
    original, reads = dubbing_edit.read_take, []

    def read(*args):
        reads.append(1)
        return original(*args)

    monkeypatch.setattr(dubbing_edit, 'read_take', read)
    first = dubbing_edit.take_snapshot(root, row, 1, dependencies=deps)
    first['settings']['changed_by_caller'] = True
    second = dubbing_edit.take_snapshot(root, row, 1, dependencies=deps)
    assert len(reads) == 1 and 'changed_by_caller' not in second['settings']
    path = dubbing_edit.take_path(root, row['id'], 1).with_suffix('.take.json')
    atomic_write_bytes(path, b'{"identity":"changed"}')
    assert dubbing_edit.take_snapshot(root, row, 1, dependencies=deps) == {'identity': 'changed'}
    assert len(reads) == 2
    path.unlink()
    assert dubbing_edit.take_snapshot(root, row, 1, dependencies=deps) == {}
    assert not deps._take_snapshots


def test_voice_fingerprint_shared_across_request_versions_but_file_changes_invalidate(make_project, monkeypatch):
    uid = make_project()
    mark_upstream_done(uid, 'assemble')
    deps = Dependencies(crud.get_project(uid))
    original, calls = dubbing_edit.playable, []

    def playable(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(dubbing_edit, 'playable', playable)
    first = voice_timing.fingerprint(deps)
    assert voice_timing.fingerprint(deps.for_version({'values': {}})) == first
    assert len(calls) == 1
    path = dubbing_edit.take_path(deps.root, 1, 1)
    path.unlink()
    assert voice_timing.fingerprint(deps)['voices'][0]['version'] is None
    assert len(calls) == 2
    voice_timing.fingerprint(Dependencies(deps.project))
    assert len(calls) == 3


def test_layout_reused_only_when_version_settings_and_file_identity_match(make_project, monkeypatch):
    uid = make_project()
    deps = Dependencies(crud.get_project(uid))
    rows = [dict(id=1, text='测试', start=1., end=2., source='dub', take=1, seconds=.5)]
    original, calls = voice_timing._build_plan, []

    def build(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(voice_timing, '_build_plan', build)
    first = voice_timing.plan(deps.project, deps.root, rows, deps.cfg, 10, dependencies=deps)
    voice_timing.plan(deps.project, deps.root, rows, deps.cfg, 10, dependencies=deps)
    assert len(calls) == 1
    first['timing']['lines'].clear()
    assert voice_timing.plan(deps.project, deps.root, rows, deps.cfg, 10, dependencies=deps)['timing']['lines']
    changed = deps.for_version({'values': {'tts.placement.max_shift_seconds': 1.}})
    voice_timing.plan(deps.project, deps.root, rows, changed.cfg, 10, dependencies=changed)
    assert len(calls) == 2
    atomic_write_bytes(dubbing_edit.take_path(deps.root, 1), b'new audio identity')
    voice_timing.plan(deps.project, deps.root, rows, deps.cfg, 10, dependencies=deps)
    assert len(calls) == 3
