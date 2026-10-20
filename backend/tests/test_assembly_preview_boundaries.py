"""试片最小时长边界；任务启动使用替身，不编码或触碰真实项目。"""
from types import SimpleNamespace

import pytest

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline.step_assemble import _task_range
from dubforge.runner import runner


def _start(entry, uid, start, end):
    if entry == 'legacy':
        return runner.start_assemble_preview(uid, start, end)
    candidate = {'versions': [{'id': 'boundary', 'include_comments': False}]}
    return runner.start_versions(uid, {'start': start, 'end': end}, preview=True, frozen=candidate)


@pytest.mark.parametrize('entry', ['legacy', 'versions'])
@pytest.mark.parametrize(('start', 'end'), [(0, .2), (9.8, 10), (119.8, 120), (3601.8, 3602)])
def test_exact_minimum_preview_can_queue_without_floating_point_rejection(
        make_project, monkeypatch, entry, start, end):
    uid = make_project()
    captured = []
    monkeypatch.setattr(runner, '_start_step', lambda *a, **kw: captured.append(kw['task_data']) or {'status': 'started'})
    assert _start(entry, uid, start, end)['status'] == 'started'
    assert len(captured) == 1
    assert (captured[0]['start'], captured[0]['end']) == (start, end)
    assert crud.latest_job(crud.get_project(uid).id, 'assemble') is None


@pytest.mark.parametrize('entry', ['legacy', 'versions'])
@pytest.mark.parametrize(('start', 'end'), [(9.9, 10), (9.800001, 10), (0, .199999), (10, 10), (-.1, .2), (0, float('nan'))])
def test_truly_short_or_invalid_preview_still_cannot_queue(
        make_project, monkeypatch, entry, start, end):
    uid = make_project()
    monkeypatch.setattr(runner, '_start_step', lambda *a, **kw: pytest.fail('无效试片范围不得提交任务'))
    with pytest.raises(DubForgeError):
        _start(entry, uid, start, end)
    assert crud.latest_job(crud.get_project(uid).id, 'assemble') is None


@pytest.mark.parametrize(('start', 'end'), [(0, .2), (9.8, 10), (119.8, 120), (3601.8, 3602)])
def test_render_range_rechecks_same_minimum_with_float_tolerance(isolated_env, start, end):
    ctx = SimpleNamespace(task_data={'scope': 'preview', 'start': start, 'end': end})
    assert _task_range(ctx, end) == (start, end)


@pytest.mark.parametrize(('start', 'end'), [(9.9, 10), (9.800001, 10), (0, .199999)])
def test_render_range_preserves_real_short_clip_rejection(isolated_env, start, end):
    ctx = SimpleNamespace(task_data={'scope': 'preview', 'start': start, 'end': end})
    with pytest.raises(DubForgeError) as error:
        _task_range(ctx, end)
    assert error.value.code == 'ASSEMBLE_PREVIEW_RANGE'
