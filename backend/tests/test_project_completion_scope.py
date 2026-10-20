"""本地制作完成按保存目标消费；隔离数据，不调用模型、平台或制作任务。"""
import json
from types import SimpleNamespace

import pytest

from conftest import mark_upstream_done
from dubforge import configuration
from dubforge.domain import crud
from dubforge.pipeline import assembly_versions as versions
from dubforge.pipeline.dependencies import Dependencies, project_status
from dubforge.project_execution import production_completion
from dubforge.runner import compute_fingerprint


def save(uid, updates):
    current = configuration.resolve(uid)
    configuration.save('project:' + uid, dict(revision=current['revision'],
        fingerprint=current['fingerprint'], updates=updates))


def definition(key='main', **values):
    return {**dict(id=key, name=key, dubbing=False, burn_subtitles=True,
                   include_comments=False, overrides={'assemble.subtitle_track': 'source'}), **values}


@pytest.fixture
def completed(make_project):
    uid = make_project()
    save(uid, {'assembly.versions': [definition()], 'assembly.target_version_ids': ['main'],
               'run.mode': 'produce', 'content_review.enabled': False})
    mark_upstream_done(uid, 'assemble')
    project = crud.get_project(uid)
    for key in ('download', 'preprocess', 'transcribe', 'split', 'translate'):
        step = crud.get_step(project.id, key)
        step.input_fingerprint = compute_fingerprint(project, key)
        crud.save_step(step)
    step = crud.get_step(project.id, 'subtitles')
    step.status = 'pending'
    crud.save_step(step)
    deps = Dependencies(project)
    resolved = versions.resolve(deps, definition())
    path = deps.root / 'output/final_version_main.mp4'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'isolated completed video')
    crud.add_artifact(project.id, 'assemble', 'video', path.name, path, str(path.relative_to(deps.root)))
    versions.record_result(SimpleNamespace(project=project), resolved, path.name, versions.fingerprint(deps, resolved))
    step = crud.get_step(project.id, 'assemble')
    step.status = 'done'
    crud.save_step(step)
    return uid


def test_default_goal_does_not_require_optional_subtitle_export_and_view_filters_get_done(completed, monkeypatch):
    from app.api.projects import _project_view
    from dubforge import workflow_plan
    monkeypatch.setattr(workflow_plan, 'plan', lambda *args, **kwargs: pytest.fail('读取完成状态不能运行完整预检'))
    project = crud.get_project(completed)
    before = (project.params, versions.results(completed)['main'].fingerprint)
    scope = production_completion(Dependencies(project))
    assert scope['complete'] and scope['version_ids'] == ['main']
    assert 'subtitles' not in scope['steps'] and 'tts' not in scope['steps']
    assert project_status(project) == 'done'
    view = _project_view(project)
    assert view['status'] == 'done'
    assert next(step for step in view['steps'] if step['key'] == 'subtitles')['status'] == 'pending'
    assert not crud.latest_job(project.id, 'assemble')
    assert (crud.get_project(completed).params, versions.results(completed)['main'].fingerprint) == before


@pytest.mark.parametrize('state,expected', [('pending', 'created'), ('failed', 'failed'), ('queued', 'processing')])
def test_explicit_independent_subtitle_export_is_still_required(completed, state, expected):
    project = crud.get_project(completed)
    params = crud.project_params(project)
    params['auto_steps'] = ['subtitles']
    project.params = json.dumps(params)
    crud.save_project(project)
    step = crud.get_step(project.id, 'subtitles')
    step.status = state
    crud.save_step(step)
    assert production_completion(Dependencies(project))['source'] == 'explicit_steps'
    assert project_status(crud.get_project(completed)) == expected


def test_new_selected_unbuilt_version_cannot_borrow_existing_outputs_fresh(completed):
    save(completed, {'assembly.versions': [definition(), definition('new')],
                     'assembly.target_version_ids': ['main', 'new']})
    deps = Dependencies(crud.get_project(completed))
    assert deps.display_fresh('assemble'), '成品展示仍保留原版本有效，不把新增版本误作旧片失效'
    assert project_status(deps.project, deps) == 'created'
    assert versions.results(completed).keys() == {'main'}


def test_unselected_unbuilt_or_changed_version_does_not_override_current_goal(completed):
    save(completed, {'assembly.versions': [definition(), definition('unused')]})
    project = crud.get_project(completed)
    deps = Dependencies(project)
    assert production_completion(deps)['version_ids'] == ['main']
    assert project_status(project, deps) == 'done'
    # 历史未采用版本的存在不使当前目标增加一个制作合同。
    save(completed, {'assembly.versions': [definition(), definition('unused',
        overrides={'assemble.subtitle_track': 'source', 'assemble.subtitle_color': '#00FF00'})]})
    assert project_status(crud.get_project(completed)) == 'done'


@pytest.mark.parametrize('changed', ['settings', 'producer_settings', 'file_missing', 'source_changed'])
def test_target_settings_upstream_settings_and_missing_or_changed_inputs_never_count_done(completed, changed):
    project = crud.get_project(completed)
    if changed == 'settings':
        save(completed, {'assemble.subtitle_color': '#00FF00'})
    elif changed == 'producer_settings':
        save(completed, {'asr.model': 'small'})
    elif changed == 'file_missing':
        artifact = versions.result_artifact(Dependencies(project), definition())
        (crud.project_dir(completed) / artifact.rel_path).unlink()
    else:
        path = Dependencies(project).path('split', 'split.txt')
        path.write_text(path.read_text(encoding='utf-8').replace('hello', 'world'), encoding='utf-8')
    assert project_status(crud.get_project(completed)) == 'created'


@pytest.mark.parametrize('key,state,expected', [
    ('tts', 'failed', 'done'), ('subtitles', 'failed', 'done'),
    ('split', 'failed', 'failed'), ('assemble', 'failed', 'failed'),
    ('subtitles', 'running', 'processing'), ('tts', 'queued', 'processing'),
])
def test_only_required_failures_block_completion_but_any_active_work_remains_processing(completed, key, state, expected):
    project = crud.get_project(completed)
    step = crud.get_step(project.id, key)
    step.status = state
    crud.save_step(step)
    assert project_status(crud.get_project(completed)) == expected


@pytest.mark.parametrize('old_encoding_status', ['pending', 'failed'])
def test_source_direct_goal_uses_real_source_contract_without_assembly_result(make_project, old_encoding_status):
    uid = make_project()
    save(uid, {'assembly.versions': [definition(burn_subtitles=False)],
               'assembly.target_version_ids': ['main'], 'run.mode': 'produce', 'content_review.enabled': False})
    mark_upstream_done(uid, 'preprocess')
    project = crud.get_project(uid)
    for key, state in [('assemble', old_encoding_status), ('tts', 'failed'), ('subtitles', 'failed')]:
        step = crud.get_step(project.id, key)
        step.status = state
        crud.save_step(step)
    assert project_status(project) == 'done'
    assert 'assemble' not in production_completion(Dependencies(project))['steps']
    assert not versions.results(uid) and crud.get_step(project.id, 'assemble').status == old_encoding_status
    Dependencies(project).path('download', 'video.mp4').unlink()
    assert project_status(crud.get_project(uid)) == 'created'


def test_mixed_source_and_rendered_targets_still_require_successful_encoding(completed):
    save(completed, {'assembly.versions': [definition(), definition('source', burn_subtitles=False)],
                     'assembly.target_version_ids': ['main', 'source']})
    project = crud.get_project(completed)
    assert project_status(project) == 'done'
    step = crud.get_step(project.id, 'assemble')
    step.status = 'failed'
    crud.save_step(step)
    assert project_status(crud.get_project(completed)) == 'failed'


def test_metadata_only_update_keeps_saved_local_production_goal(completed):
    from dubforge.publishing.models import Publication
    with crud.session() as session:
        publication = Publication(project_uid=completed, status='published', receipt='{"bvid":"BVfixture"}')
        session.add(publication)
        session.commit()
        session.refresh(publication)
        identifier = publication.id
    save(completed, {'run.mode': 'publish', 'publish.project_target': dict(kind='existing',
        publication_id=identifier, replace_videos=False, replacements=[], sync_fields=['chapters'])})
    assert project_status(crud.get_project(completed)) == 'done'
    save(completed, {'assemble.subtitle_color': '#00FF00'})
    assert project_status(crud.get_project(completed)) == 'created'


def test_publishing_consumed_extra_version_is_required_without_affecting_produce_mode(completed):
    save(completed, {'assembly.versions': [definition(), definition('remote-extra')],
                     'publish.version_ids': ['remote-extra'], 'run.mode': 'publish'})
    assert project_status(crud.get_project(completed)) == 'created'
    save(completed, {'run.mode': 'produce'})
    assert project_status(crud.get_project(completed)) == 'done'


def test_unresolved_local_target_is_not_completed_by_publication_or_empty_set(make_project):
    uid = make_project()
    assert production_completion(Dependencies(crud.get_project(uid)))['known'] is False
    assert project_status(crud.get_project(uid)) == 'created'
