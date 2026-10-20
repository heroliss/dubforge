"""成片剪辑只改变输出时间轴，保存和运行快照彼此隔离。"""
import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from dubforge.pipeline import output_cuts as cuts


@pytest.fixture()
def project(isolated_env, monkeypatch):
    from dubforge.domain import crud
    project = crud.create_project('剪辑测试', source_kind='local', source_url='media/video.mp4')
    (crud.project_dir(project.uid) / 'media/video.mp4').write_bytes(b'isolated source video')
    monkeypatch.setattr(cuts, '_duration', lambda *_: 100.)
    return project


def body(uid, ranges=None, **updates):
    current = cuts.view(uid)
    return {'revision': current['revision'], 'source_fingerprint': current['source_fingerprint'],
            'enabled': True, 'ranges': ranges if ranges is not None else [
                {'id': 'ad', 'start': 10, 'end': 20, 'reason': '广告', 'enabled': True}], **updates}


def deps(project):
    from dubforge.pipeline.dependencies import Dependencies
    return Dependencies(project)


def sample():
    return {'schema': 1, 'source_fingerprint': 'source', 'source_duration': 100.,
            'output_duration': 70., 'ranges': [{'start': 10., 'end': 20.}, {'start': 40., 'end': 60.}]}


def test_save_merge_disabled_and_atomic_document(project):
    from dubforge.domain import crud
    value = cuts.save(project.uid, body(project.uid, [
        {'id': 'a', 'start': 10, 'end': 20, 'reason': '广告'},
        {'id': 'b', 'start': 15, 'end': 25},
        {'id': 'c', 'start': 25, 'end': 30},
        {'id': 'd', 'start': 50, 'end': 60, 'enabled': False},
    ]))
    assert value['revision'] != '0'
    assert value['effective_ranges'] == [{'start': 10., 'end': 30.}]
    assert value['removed_duration'] == 20.
    assert value['output_duration'] == 80.
    assert len(value['ranges']) == 4
    assert value['source_url'] == f'/api/projects/{project.uid}/workbench/video'
    root = crud.project_dir(project.uid)
    assert json.loads((root / 'output_cuts.json').read_text(encoding='utf-8'))['schema'] == 1
    assert not list(root.glob('*.tmp'))


def test_view_exposes_source_frame_rate_for_precise_cut_marking(project, monkeypatch):
    monkeypatch.setattr(cuts, '_frame_rate', lambda *_: 25.0)
    assert cuts.view(project.uid)['frame_rate'] == 25.0


@pytest.mark.parametrize('ranges,code', [
    ([{'id': 'a', 'start': 0, 'end': 100}], 'OUTPUT_CUTS_EMPTY'),
    ([{'id': 'a', 'start': -1, 'end': 20}], 'OUTPUT_CUTS_INVALID'),
    ([{'id': 'a', 'start': 0, 'end': 101}], 'OUTPUT_CUTS_INVALID'),
    ([{'id': 'a', 'start': float('nan'), 'end': 20}], 'OUTPUT_CUTS_INVALID'),
    ([{'id': 'a', 'start': 10 ** 1000, 'end': 20}], 'OUTPUT_CUTS_INVALID'),
    ([{'id': 'a', 'start': True, 'end': 20}], 'OUTPUT_CUTS_INVALID'),
    ([{'id': 'a', 'start': 10, 'end': 10}], 'OUTPUT_CUTS_INVALID'),
    ([{'id': 'a', 'start': 10, 'end': 20}, {'id': 'a', 'start': 30, 'end': 40}], 'OUTPUT_CUTS_INVALID'),
])
def test_invalid_ranges_rejected(project, ranges, code):
    with pytest.raises(cuts.OutputCutsError) as caught:
        cuts.save(project.uid, body(project.uid, ranges))
    assert caught.value.code == code
    assert cuts.view(project.uid)['revision'] == '0'


def test_source_identity_requires_explicit_rebind(project):
    from dubforge.domain import crud
    cuts.save(project.uid, body(project.uid))
    path = crud.project_dir(project.uid) / 'media/video.mp4'
    path.write_bytes(b'a different replacement video')
    changed = cuts.view(project.uid)
    assert changed['source_changed'] is True
    assert changed['effective_ranges'] == []
    with pytest.raises(cuts.OutputCutsError, match='源视频已变化'):
        cuts.snapshot(deps(project))
    with pytest.raises(cuts.OutputCutsError, match='明确确认'):
        cuts.save(project.uid, body(project.uid))
    rebound = cuts.save(project.uid, body(project.uid, rebind_source=True))
    assert rebound['source_changed'] is False
    assert cuts.snapshot(deps(project))['ranges'] == [{'start': 10., 'end': 20.}]


def test_optimistic_concurrency_has_one_winner(project):
    request = body(project.uid)
    def attempt(_):
        try:
            cuts.save(project.uid, request)
            return 'saved'
        except cuts.OutputCutsError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(attempt, range(2)))
    assert sorted(values) == ['OUTPUT_CUTS_CONFLICT', 'saved']


def test_remarks_do_not_stale_and_frozen_snapshot_keeps_old_plan(project):
    cuts.save(project.uid, body(project.uid))
    frozen = cuts.snapshot(deps(project))
    request = body(project.uid)
    request['ranges'][0]['reason'] = '只修改说明'
    cuts.save(project.uid, request)
    assert cuts.snapshot(deps(project)) == frozen
    request = body(project.uid)
    request['ranges'][0]['end'] = 30
    cuts.save(project.uid, request)
    running = deps(project)
    running._output_cuts_snapshot = copy.deepcopy(frozen)
    assert cuts.snapshot(running) == frozen
    assert cuts.snapshot(deps(project)) != frozen
    cuts.snapshot(running)['ranges'][0]['end'] = 99
    assert running._output_cuts_snapshot == frozen
    running._output_cuts_snapshot = None
    assert cuts.snapshot(running) is None


def test_disabled_plan_is_render_noop(project):
    assert cuts.snapshot(deps(project)) is None
    cuts.save(project.uid, body(project.uid, enabled=False))
    assert cuts.snapshot(deps(project)) is None


def test_archived_projects_reject_save(project):
    from dubforge.project_archive import set_archived, ProjectArchiveError
    request = body(project.uid)
    set_archived(project.uid, True)
    assert cuts.view(project.uid)['duration'] == 100
    with pytest.raises(ProjectArchiveError) as exc:
        cuts.save(project.uid, request)
    assert exc.value.code == 'PROJECT_ARCHIVED'


def test_save_during_running_assemble_does_not_modify_frozen_plan(project):
    from dubforge.domain import crud
    cuts.save(project.uid, body(project.uid))
    frozen = cuts.snapshot(deps(project))
    with crud.session() as session:
        from dubforge.domain.models import StepRun
        from sqlmodel import select
        step = session.exec(select(StepRun).where(StepRun.project_id == project.id,
                                                  StepRun.step_key == 'assemble')).first()
        step.status = 'running'
        session.add(step)
        session.commit()
    request = body(project.uid)
    request['ranges'][0]['end'] = 30
    value = cuts.save(project.uid, request)
    assert value['removed_duration'] == 20.
    assert frozen['ranges'] == [{'start': 10., 'end': 20.}]


def test_missing_video_readonly_state_and_save_error(project):
    from dubforge.domain import crud
    (crud.project_dir(project.uid) / 'media/video.mp4').unlink()
    current = cuts.view(project.uid)
    assert current['duration'] is None and current['warnings']
    assert current['source_url'] is None
    with pytest.raises(cuts.OutputCutsError) as exc:
        cuts.save(project.uid, body(project.uid))
    assert exc.value.code == 'OUTPUT_CUTS_SOURCE_UNAVAILABLE'


def test_time_mapping_and_preview_segments():
    assert cuts.keep_segments(sample()) == [
        {'start': 0., 'end': 10., 'output_start': 0., 'output_end': 10.},
        {'start': 20., 'end': 40., 'output_start': 10., 'output_end': 30.},
        {'start': 60., 'end': 100., 'output_start': 30., 'output_end': 70.},
    ]
    assert cuts.keep_segments(sample(), 15, 65) == [
        {'start': 20., 'end': 40., 'output_start': 0., 'output_end': 20.},
        {'start': 60., 'end': 65., 'output_start': 20., 'output_end': 25.},
    ]
    assert cuts.map_time(sample(), 50) == 30
    assert cuts.map_time(sample(), 65) == 35
    assert cuts.split_interval(sample(), 5, 25) == [
        {'start': 5, 'end': 10., 'output_start': 5., 'output_end': 10.},
        {'start': 20., 'end': 25, 'output_start': 10., 'output_end': 15.}]


def test_chapters_drop_deleted_and_map_retained_intervals():
    result = cuts.map_chapters(sample(), [
        {'title': '开场', 'start_time': 0, 'end_time': 10},
        {'title': '广告', 'start_time': 10, 'end_time': 20},
        {'title': '正文', 'start_time': 20, 'end_time': 50},
        {'title': '结尾', 'start_time': 50},
    ])
    assert result == [
        {'title': '开场', 'start_time': 0., 'end_time': 10.},
        {'title': '正文', 'start_time': 10., 'end_time': 30.},
        {'title': '结尾', 'start_time': 30., 'end_time': 70.}]
    assert cuts.map_chapters(sample(), [{'title': '中途', 'start_time': 30}])[0]['start_time'] == 20


def test_filter_graph_trims_after_render_and_uses_local_preview_time():
    graph, video, audio, duration = cuts.filter_graph(sample(), 'burned', 'mixed', start=15, duration=50)
    assert '[burned]split=2[ocvs0][ocvs1]' in graph
    assert '[mixed]asplit=2[ocas0][ocas1]' in graph
    assert 'trim=start=5.000000:end=25.000000' in graph
    assert 'trim=start=45.000000:end=50.000000' in graph
    assert 'concat=n=2:v=1:a=1[ocv][oca]' in graph
    assert (video, audio, duration) == ('[ocv]', '[oca]', 25.)
    graph, video, audio, duration = cuts.filter_graph(sample(), '[burned]', start=20, duration=10)
    assert 'concat' not in graph and 'atrim' not in graph
    assert (video, audio, duration) == ('[ocv0]', None, 10.)
    with pytest.raises(cuts.OutputCutsError) as exc:
        cuts.filter_graph(sample(), 'v', start=45, duration=10)
    assert exc.value.code == 'OUTPUT_CUTS_PREVIEW_EMPTY'


def test_snapshot_detects_corrupt_persisted_ranges(project):
    from dubforge.domain import crud
    root = crud.project_dir(project.uid)
    (root / 'output_cuts.json').write_text(json.dumps({'schema': 1, 'enabled': True, 'ranges': ['bad']}), encoding='utf-8')
    with pytest.raises(cuts.OutputCutsError):
        cuts.snapshot(deps(project))


def test_failed_save_keeps_previous_plan_and_reports_business_error(project, monkeypatch):
    cuts.save(project.uid, body(project.uid))
    previous = cuts.view(project.uid)
    request = body(project.uid)
    request['ranges'][0]['end'] = 30
    def failed_write(*_):
        raise PermissionError('test isolated permission failure')
    monkeypatch.setattr(cuts, 'atomic_write_bytes', failed_write)
    with pytest.raises(cuts.OutputCutsError) as exc:
        cuts.save(project.uid, request)
    assert exc.value.code == 'OUTPUT_CUTS_WRITE_FAILED'
    assert cuts.view(project.uid) == previous


def test_api_roundtrip_and_conflict(project):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    from app.api.output_cuts import router
    from dubforge.errors import DubForgeError
    app = FastAPI()
    app.include_router(router)
    @app.exception_handler(DubForgeError)
    async def handle(_, exc):
        return JSONResponse(status_code=400, content=exc.to_dict())
    client = TestClient(app)
    route = f'/api/projects/{project.uid}/output-cuts'
    assert client.get(route).json()['revision'] == '0'
    request = body(project.uid)
    assert client.put(route, json=request).json()['removed_duration'] == 10.
    response = client.put(route, json=request)
    assert response.status_code == 400
    assert response.json()['code'] == 'OUTPUT_CUTS_CONFLICT'
