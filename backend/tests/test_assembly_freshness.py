"""已生成组合的展示状态与默认流程的执行跳过必须分开。"""
import hashlib
import json

import pytest
from fastapi import Response

from app.api.steps import list_steps
from dubforge import config
from dubforge.domain import crud
from dubforge.pipeline.dependencies import Dependencies
from dubforge.runner import compute_fingerprint


@pytest.fixture()
def completed(make_project):
    uid = make_project()
    project = crud.get_project(uid)
    root = crud.project_dir(uid)
    variant = 'src_hardsub_comments'
    def artifact(key, name, relative, content=b'fixture'):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        result = crud.add_artifact(project.id, key, 'video', name, path, relative)
        step = crud.get_step(project.id, key)
        step.status = 'done'; crud.save_step(step)
        return result, path
    artifact('download', 'video.mp4', 'media/video.mp4')
    artifact('subtitles', 'trans.srt', 'srt/trans.srt')
    name = f'final_{variant}_20260928_120000_123456.mp4'
    output, path = artifact('assemble', name, f'output/{name}')
    task = {'scope': 'outputs', 'variants': [variant], 'comment_overlay': {'items': [{'text': '评论'}], 'settings': {}}}
    deps = Dependencies(project, assemble_variants=[variant])
    raw = compute_fingerprint(project, 'assemble', dependencies=deps)
    previous = 'comments:' + hashlib.sha256(('outputs:' + raw + json.dumps(task['comment_overlay'], sort_keys=True)).encode()).hexdigest()
    step = crud.get_step(project.id, 'assemble'); step.input_fingerprint = previous; crud.save_step(step)
    job = crud.add_job(project.id, 'assemble', '', task_data=task)
    job.status = 'done'; crud.update_job(job)
    return uid, project, output, path, job, raw


@pytest.mark.parametrize('modern', [False, True])
def test_actual_output_is_fresh_even_when_default_and_other_variants_missing(completed, modern):
    uid, project, _, path, job, raw = completed
    if modern:
        task = json.loads(job.task_data); task['production_fingerprint'] = raw
        job.task_data = json.dumps(task); crud.update_job(job)
    missing = path.with_name(path.name.replace('src_hardsub_comments', 'dub_hardsub'))
    missing.write_bytes(b'old-output')
    crud.add_artifact(project.id, 'assemble', 'video', missing.name, missing, str(missing.relative_to(crud.project_dir(uid))))
    missing.unlink()
    deps = Dependencies(project)
    assert not deps.fresh('assemble')  # 绝不把默认配音方案错误标成可跳过。
    assert deps.display_fresh('assemble')
    view = next(row for row in list_steps(uid, Response()) if row['key'] == 'assemble')
    assert view['fresh'] and not view['default_fresh']
    assert view['freshness_label'] == '已生成方案有效'


@pytest.mark.parametrize('change', ['file_missing', 'file_changed', 'settings', 'subtitle_missing', 'upstream_stale'])
def test_real_changes_still_invalidate_actual_plan(completed, change):
    uid, project, _, path, _, _ = completed
    if change == 'file_missing':
        path.unlink()
    elif change == 'file_changed':
        path.write_bytes(b'changed size')
    elif change == 'settings':
        from test_configuration import save
        save('project:' + uid, {'assemble.crf': 28})
    elif change == 'subtitle_missing':
        (crud.project_dir(uid) / 'srt/trans.srt').unlink()
    else:
        # 仅改变上游登记文件才算真正的上游变化；手工把状态改成 stale
        # 不应再把旧产物级联标成过期。
        subtitle = crud.project_dir(uid) / 'srt/trans.srt'
        subtitle.write_text('changed upstream subtitle', encoding='utf-8')
    state = Dependencies(project).assembly_state()
    assert not state['fresh']
    assert state['reason']
    if change.startswith('file_'):
        assert state['label'] == '成品文件缺失'


def test_global_render_defaults_do_not_invalidate_completed_project(completed):
    _, project, _, _, _, _ = completed
    config.set_key('assemble.crf', 28)
    assert Dependencies(project).display_fresh('assemble')


def test_preview_does_not_replace_formal_plan_and_never_hides_failed_run(completed):
    _, project, _, _, _, _ = completed
    preview = crud.add_job(project.id, 'assemble', '', task_data={'scope': 'preview'})
    preview.status = 'done'; crud.update_job(preview)
    assert Dependencies(project).display_fresh('assemble')
    failed = crud.add_job(project.id, 'assemble', '', task_data={'scope': 'outputs', 'variants': ['src_hardsub']})
    failed.status = 'failed'; crud.update_job(failed)
    assert not Dependencies(project).display_fresh('assemble')


def test_newer_missing_same_variant_does_not_fall_back_to_old_file(completed):
    uid, project, _, path, _, _ = completed
    newer = path.with_name(path.name.replace('120000', '130000'))
    newer.write_bytes(b'newer')
    crud.add_artifact(project.id, 'assemble', 'video', newer.name, newer, str(newer.relative_to(crud.project_dir(uid))))
    newer.unlink()
    assert path.exists()
    assert not Dependencies(project).display_fresh('assemble')
