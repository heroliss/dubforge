"""运行范围必须由用户选择决定；计划只读，显式成品不隐式补默认。"""
import json

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.api.projects import RunChain, RunMany, run_plan
from app.api.steps import list_steps, plan_assemble_outputs
from app.main import app
from conftest import wait_status
from dubforge import config
from dubforge.domain import crud
from dubforge.pipeline.dependencies import Dependencies
from dubforge.runner import runner


@pytest.mark.parametrize('variants', [[], ['src'], ['dub', 'unknown'], None, 'dub'])
def test_empty_or_invalid_outputs_never_queue(make_project, variants):
    uid = make_project()
    client = TestClient(app)  # 不进入 lifespan，不启动真实服务或恢复真实项目。
    for action in ('output_plan', 'outputs'):
        response = client.post(f'/api/projects/{uid}/steps/assemble/{action}',
                               json={'variants': variants})
        assert response.status_code == 400
        assert response.json()['code'] == 'ASSEMBLE_VARIANTS'
    project = crud.get_project(uid)
    assert crud.latest_job(project.id, 'assemble') is None
    assert crud.get_step(project.id, 'assemble').status == 'pending'


@pytest.mark.parametrize('burn', [False, True])
def test_explicit_outputs_check_actual_variant_not_default(make_project, burn):
    uid = make_project()
    config.set_key('assemble.burn_subtitles', burn)
    project = crud.get_project(uid)
    for variants, expected_subtitle in [(['dub'], False), (['dub_hardsub'], True),
                                       (['src_hardsub'], True), (['dub', 'src_hardsub'], True)]:
        inputs = Dependencies(project, assemble_variants=variants).inputs('assemble')
        assert any(step == 'subtitles' for step, _, _ in inputs) == expected_subtitle
        if variants == ['src_hardsub']:
            assert [(step, name) for step, name, _ in inputs] == [
                ('download', 'video.mp4'), ('subtitles', 'trans.srt')]
        plan = plan_assemble_outputs(uid, {'variants': variants + variants})
        assert plan['variants'] == variants
        assert crud.latest_job(project.id, 'assemble') is None


def test_original_hardsub_without_tts_does_not_start_other_steps(make_project, monkeypatch):
    from test_assemble import _prepare_outputs_project
    uid, root = _prepare_outputs_project(make_project, monkeypatch)
    project = crud.get_project(uid)
    # 即使混音配置启用背景，原声烧录也不消费分离音轨或配音。
    config.set_key('assemble.bgm_volume', 1)
    params = crud.project_params(project)
    params['_chain'] = ['tts', 'assemble']
    project.params = json.dumps(params)
    crud.save_project(project)
    plan = plan_assemble_outputs(uid, {'variants': ['src_hardsub']})
    assert not plan['blockers']
    runner.start_assemble_outputs(uid, ['src_hardsub'])
    wait_status(uid, 'assemble', {'done'}, timeout=30)
    names = [path.name for path in (root / 'output').glob('final_*.mp4')]
    assert len(names) == 1 and names[0].startswith('final_src_hardsub_')
    assert crud.latest_job(project.id, 'tts') is None
    assert not (root / 'audio/dub_full.wav').exists()
    assert not crud.project_params(crud.get_project(uid)).get('_chain')


def test_required_hardsub_checked_again_before_queue(make_project):
    uid = make_project()
    plan = plan_assemble_outputs(uid, {'variants': ['dub_hardsub']})
    assert any(item['step'] == 'subtitles' for item in plan['blockers'])
    response = TestClient(app).post(f'/api/projects/{uid}/steps/assemble/outputs',
                                   json={'variants': ['dub_hardsub']})
    assert response.status_code == 400
    assert crud.latest_job(crud.get_project(uid).id, 'assemble') is None


@pytest.mark.parametrize('burn', [False, True])
def test_pipeline_plan_discloses_project_default_and_tts_scope(make_project, burn):
    config.set_key('assemble.burn_subtitles', not burn)
    uid = make_project(params={'step_params': {'assemble.burn_subtitles': burn}})
    from test_configuration import save
    expected = '配音 · 烧录字幕' if burn else '配音 · 不烧录字幕'
    save('project:'+uid, {'assembly.versions':[dict(id='a',name=expected,dubbing=True,burn_subtitles=burn,include_comments=False,overrides={})]})
    result = run_plan(uid, RunChain(steps=['tts', 'assemble']))
    expected = '配音 · 烧录字幕' if burn else '配音 · 不烧录字幕'
    assert expected in result['effects'][1]
    assert '生成已保存版本' in result['effects'][1]
    assert '不读取成品卡片临时勾选' in result['effects'][1]
    assert '不读取句子列表的临时勾选' in result['effects'][0]
    status = next(s for s in list_steps(uid, Response()) if s['key'] == 'assemble')
    assert status['cfg']['assemble']['burn_subtitles'] == burn


def test_original_output_does_not_mark_default_dub_complete(make_project, monkeypatch):
    from test_assemble import _prepare_outputs_project
    uid, root = _prepare_outputs_project(make_project, monkeypatch)
    from test_configuration import save
    save('project:'+uid, {'assembly.versions':[dict(id='dub',name='配音版',dubbing=True,burn_subtitles=False,include_comments=False,overrides={})]})
    runner.start_step(uid, 'tts')
    wait_status(uid, 'tts', {'done'})
    runner.start_assemble_outputs(uid, ['src_hardsub'])
    wait_status(uid, 'assemble', {'done'})
    assert not Dependencies(crud.get_project(uid)).fresh('assemble')
    assert Dependencies(crud.get_project(uid)).display_fresh('assemble')
    assert runner.start_step(uid, 'assemble')['status'] == 'started'
    wait_status(uid, 'assemble', {'done'})
    assert (root / 'output/final_version_dub.mp4').is_file()
    # 缺失试片不应影响默认正式成品的展示状态。
    preview = root / 'output/preview_20260928_120000_123456.mp4'
    preview.write_bytes(b'preview')
    project = crud.get_project(uid)
    crud.add_artifact(project.id, 'assemble', 'video', preview.name, preview, str(preview.relative_to(root)))
    preview.unlink()
    assert Dependencies(project).display_fresh('assemble')


def test_batch_execution_keeps_confirmed_steps_snapshot(make_project, monkeypatch):
    from app.api import projects
    uid = make_project(params={'auto_steps': ['download', 'assemble']})
    calls = []

    def record(uid, body):
        calls.append((uid, body.steps))
        return {'started': body.steps[0]}

    monkeypatch.setattr(projects, 'run_chain', record)
    projects.run_many(RunMany(uids=[uid], steps_by_uid={uid: ['download']}))
    assert calls == [(uid, ['download'])]
    calls.clear()
    projects.run_many(RunMany(uids=[uid], steps_by_uid={}))
    assert not calls  # 显式空快照不能回退为项目的完整默认步骤。


@pytest.mark.parametrize('task', [{'scope': 'preview', 'start': 2, 'end': 5},
                                 {'scope': 'outputs', 'variants': ['src_hardsub']}, {}])
def test_step_status_discloses_assembly_task_scope(make_project, task):
    uid = make_project()
    project = crud.get_project(uid)
    crud.add_job(project.id, 'assemble', '', task_data=task)
    result = next(s for s in list_steps(uid, Response()) if s['key'] == 'assemble')
    assert result['assembly_task']['scope'] == task.get('scope', 'default')
    assert result['assembly_task']['variants'] == task.get('variants', [])
    assert '配音' in result['default_output_label']


def test_damaged_old_task_metadata_does_not_break_step_pages(make_project):
    uid = make_project()
    project = crud.get_project(uid)
    job = crud.add_job(project.id, 'assemble', '')
    job.task_data = '{broken'
    crud.update_job(job)
    result = next(s for s in list_steps(uid, Response()) if s['key'] == 'assemble')
    assert result['assembly_task']['scope'] == 'unknown'
