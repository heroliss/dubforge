"""自动宽度改用可用全宽：旧自动成片过期，显式宽度与确认时间兼容保留。"""
import copy
import json

import pytest

from conftest import mark_upstream_done
from dubforge import comment_studio
from dubforge.domain import crud
from dubforge.pipeline import assembly_versions as versions
from dubforge.pipeline.assembly_models import AssemblyResult
from dubforge.pipeline.dependencies import Dependencies


@pytest.mark.parametrize('legacy', [False, True])
def test_auto_missing_and_explicit_full_width_share_effective_fingerprint(legacy):
    plan = dict(settings=comment_studio.validate_settings({}),
                items=[dict(display_text='完整评论')], confirmed_at='2026-10-16T00:00:00Z')
    before = copy.deepcopy(plan)
    auto = comment_studio.render_fingerprint_plan(plan, legacy=legacy)
    full = copy.deepcopy(plan)
    full['settings']['width_percent'] = 100
    missing = copy.deepcopy(plan)
    missing['settings'].pop('width_percent')
    assert auto == comment_studio.render_fingerprint_plan(full, legacy=legacy)
    assert auto == comment_studio.render_fingerprint_plan(missing, legacy=legacy)
    assert auto['settings']['width_percent'] == 100
    assert auto['render_version'] == 'font-metrics-range-v5'
    assert plan == before


def _historical_result(make_project, monkeypatch, width, *, legacy):
    uid = make_project()
    mark_upstream_done(uid, 'assemble')
    project = crud.get_project(uid)
    spec = dict(id='comments', name='评论版', dubbing=False, burn_subtitles=False,
                include_comments=True, overrides={'comments.width_percent': width})
    overlay = dict(settings=comment_studio.default_settings(),
                   items=[dict(id='c1', source_hash='source', text='Hello',
                               display_text='你好', author='A')],
                   confirmed_at='2026-10-10T05:00:00+00:00')
    resolved = versions.resolve(Dependencies(project), spec, comment_plan=overlay)
    root = crud.project_dir(uid)
    name = 'final_version_comments.mp4'
    path = root / 'output' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'isolated historical completed video')
    artifact = crud.add_artifact(project.id, 'assemble', 'video', name, path, f'output/{name}')
    step = crud.get_step(project.id, 'assemble')
    step.status = 'done'
    crud.save_step(step)
    deps = Dependencies(project)
    current_summary = comment_studio.render_fingerprint_plan

    def historical_summary(plan, *, legacy=False):
        result = current_summary(plan, legacy=legacy)
        if result and not plan['settings'].get('width_percent'):
            # 改动前的 v5 自动宽度摘要会删除 0，实际使用横屏 68% 等窄版规则。
            result['settings'].pop('width_percent', None)
        return result

    with monkeypatch.context() as old:
        old.setattr(comment_studio, 'render_fingerprint_plan', historical_summary)
        old_fingerprint = versions.fingerprint(deps, resolved, legacy=legacy)
    with crud.session() as session:
        session.add(AssemblyResult(key=f'{uid}:comments', project_uid=uid,
                    version_id='comments', artifact_id=artifact.id, artifact_hash=artifact.sha256,
                    fingerprint=old_fingerprint, snapshot=json.dumps(resolved, ensure_ascii=False)))
        session.commit()
    return deps, resolved, old_fingerprint


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('current_width', [0, 100])
def test_old_auto_width_is_stale_even_through_legacy_fallback(make_project, monkeypatch, legacy, current_width):
    deps, current, old_fingerprint = _historical_result(make_project, monkeypatch, 0, legacy=legacy)
    current['comment_overlay']['settings']['width_percent'] = current_width
    current['values']['comments.width_percent'] = current_width
    current['comment_overlay']['confirmed_at'] = '2026-10-16T00:00:00+00:00'
    # 新旧快照按当前规则看起来相同，旧兼容路径仍不能复活实际画幅不同的产物。
    assert old_fingerprint != versions.fingerprint(deps, current)
    assert not versions.is_current(deps, current)


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize(('saved_width', 'current_width'), [(100, 100), (100, 0), (80, 80)])
def test_explicit_width_history_stays_current_including_reconfirmation(make_project, monkeypatch, legacy, saved_width, current_width):
    deps, current, _ = _historical_result(make_project, monkeypatch, saved_width, legacy=legacy)
    current['comment_overlay']['settings']['width_percent'] = current_width
    current['values']['comments.width_percent'] = current_width
    current['comment_overlay']['confirmed_at'] = '2026-10-16T00:00:00+00:00'
    assert versions.is_current(deps, current)


@pytest.mark.parametrize('legacy', [False, True])
def test_explicit_non_full_width_becomes_stale_when_changed_to_auto(make_project, monkeypatch, legacy):
    deps, current, _ = _historical_result(make_project, monkeypatch, 80, legacy=legacy)
    current['comment_overlay']['settings']['width_percent'] = 0
    current['values']['comments.width_percent'] = 0
    assert not versions.is_current(deps, current)
