"""试片历史只读、冻结快照、回收与原位恢复；不调用真实模型或回收站。"""
import copy
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from dubforge.domain import crud
from dubforge.domain.models import Artifact
from dubforge.pipeline import assembly_preview as previews
from dubforge.pipeline import assembly_versions as versions
from dubforge.pipeline.dependencies import Dependencies
from dubforge.publishing.models import Publication


def spec(version_id='test'):
    return dict(id=version_id, name='测试配音版', dubbing=False, burn_subtitles=False,
                include_comments=False, overrides={})


def video(uid, *, version_id='test', register=True):
    project = crud.get_project(uid)
    root = crud.project_dir(uid)
    path = root / 'output' / f'preview_version_{version_id}_{uuid.uuid4().hex}.mp4'
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b'isolated-preview-' + uuid.uuid4().bytes)
    artifact = crud.add_artifact(project.id, 'assemble', 'video', path.name, path,
        str(path.relative_to(root))) if register else None
    return path, artifact


def selection(item):
    return {key: item[key] for key in ('artifact_id', 'name', 'file_revision')}


def recycle_body(uid, path, *, version_id='test'):
    item = next(row for row in previews.history(uid, version_id)['items'] if row['name'] == path.name)
    return dict(confirmed=True, version_id=version_id, items=[selection(item)])


def test_unindexed_old_files_are_visible_readonly_without_media_probe(make_project, monkeypatch):
    uid = make_project()
    path, _ = video(uid, version_id='id_with-dash', register=False)
    (path.parent / 'final_version_test.mp4').write_bytes(b'formal')
    (path.parent / 'preview_version_test_bad.mp4').write_bytes(b'not-preview')
    monkeypatch.setattr(versions, 'fingerprint', lambda *a, **k: pytest.fail('历史读取不能重算输入'))
    import subprocess
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('历史读取不能解码媒体'))
    before = sorted(row.name for row in path.parent.iterdir())
    result = previews.history(uid, 'id_with-dash')
    assert result['total_count'] == 1 and result['total_bytes'] == path.stat().st_size
    item = result['items'][0]
    assert item['artifact_id'] is None and item['metadata_status'] == 'unknown'
    assert item['version_id'] == 'id_with-dash' and item['snapshot'] is None
    assert item['url'].endswith(path.name + '/raw')
    assert before == sorted(row.name for row in path.parent.iterdir())
    assert not crud.latest_artifacts(crud.get_project(uid).id, ['assemble'])


def test_metadata_records_only_actual_safe_snapshot_and_detects_replaced_file(make_project):
    uid = make_project()
    project = crud.get_project(uid)
    path, _ = video(uid)
    resolved = versions.resolve(Dependencies(project), spec())
    resolved['values']['llm.api_key'] = 'not-for-output'
    ctx = SimpleNamespace(job_id=19, task_data={'overlay_revision': 'confirmed-review'})
    previews.record(ctx, resolved, path, 'actual-input', start=7, end=20)
    item = previews.history(uid)['items'][0]
    assert item['metadata_status'] == 'ready' and item['job_id'] == 19
    snapshot = item['snapshot']
    assert snapshot['version_spec'] == spec() and snapshot['input_fingerprint'] == 'actual-input'
    assert snapshot['plan_fingerprint'] == versions.digest([dict(id='test', name='测试配音版', fingerprint='actual-input')])
    assert snapshot['overlay_revision'] is None, '无评论版本不能因无关评论方案变化而过期'
    assert 'llm.api_key' not in snapshot['effective_values']
    assert item['range'] == dict(start=7, end=20)
    assert not list(path.parent.glob('*.tmp'))
    path.write_bytes(b'changed-later')
    assert previews.history(uid)['items'][0]['metadata_status'] == 'unknown'


@pytest.mark.parametrize('field,value', [('created_at', 17), ('range', {'start': 4, 'end': 2}), ('range', None)])
def test_damaged_preview_metadata_does_not_break_history(make_project, field, value):
    uid = make_project()
    path, _ = video(uid)
    resolved = versions.resolve(Dependencies(crud.get_project(uid)), spec())
    previews.record(SimpleNamespace(job_id=1, task_data={}), resolved, path, 'input', start=0, end=2)
    sidecar = previews.metadata_path(path)
    data = json.loads(sidecar.read_text(encoding='utf-8')); data[field] = value
    sidecar.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    item = previews.history(uid)['items'][0]
    assert item['exists'] and item['metadata_status'] == 'unknown'


def test_old_job_snapshot_is_legacy_never_given_current_fingerprint(make_project):
    uid = make_project()
    path, artifact = video(uid)
    project = crud.get_project(uid)
    resolved = versions.resolve(Dependencies(project), spec())
    job = crud.add_job(project.id, 'assemble', '', task_data=dict(scope='preview', version_specs=[resolved], start=2, end=12))
    artifact.job_id = job.id
    with crud.session() as session:
        session.add(artifact); session.commit()
    item = previews.history(uid)['items'][0]
    assert item['metadata_status'] == 'legacy'
    assert item['snapshot']['version_spec'] == spec() and item['snapshot']['plan_fingerprint'] is None
    assert item['range'] == dict(start=2, end=12)


def test_latest_preview_is_filtered_but_active_assembly_is_project_wide(make_project):
    uid = make_project()
    project = crud.get_project(uid)
    a = crud.add_job(project.id, 'assemble', '', task_data=dict(scope='preview', version_specs=[spec('a')], start=1, end=4))
    a.status = 'failed'; a.error_detail = '编码内存不足'; crud.update_job(a)
    b = crud.add_job(project.id, 'assemble', '', task_data=dict(scope='preview', version_specs=[spec('b')], start=2, end=5))
    b.status = 'done'; crud.update_job(b)
    full = crud.add_job(project.id, 'assemble', '', task_data=dict(scope='versions', version_specs=[spec('b')]))
    result = previews.history(uid, 'a')
    assert result['latest_task']['job_id'] == a.id
    assert result['latest_task']['error_message'] == '编码内存不足'
    assert result['active_task']['job_id'] == full.id and result['active_task']['scope'] == 'render'
    assert result['active_task']['version_ids'] == ['b']


def test_paged_latest_task_and_malformed_old_job_are_safe(make_project):
    uid = make_project()
    project = crud.get_project(uid)
    old = crud.add_job(project.id, 'assemble', '', task_data=dict(scope='preview', version_specs=[spec('a')], start=1, end=2))
    old.status = 'failed'; old.error_detail = 'Traceback: technical details'
    old.execution = json.dumps(dict(detail='成片编码内存分配失败，旧成品保留', state='failed'))
    crud.update_job(old)
    crud.update_job(old, execution_only=True)
    for _ in range(34):
        row = crud.add_job(project.id, 'assemble', '', task_data=dict(scope='versions', version_specs=None))
        row.status = 'done'; crud.update_job(row)
    result = previews.history(uid, 'a')
    assert result['latest_task']['job_id'] == old.id
    assert result['latest_task']['error_message'] == '成片编码内存分配失败，旧成品保留'
    assert result['active_task'] is None
    assert 'disk_free_bytes' in result['resources']


@pytest.mark.parametrize('registered', [True, False])
def test_recycle_and_restore_retains_index_snapshot_and_reports_no_immediate_space(make_project, monkeypatch, tmp_path, registered):
    uid = make_project()
    old, artifact = video(uid, register=registered)
    latest, _ = video(uid)
    # 文件时间排序在快速测试中也具有明确先后。
    import os
    os.utime(old, (1, 1))
    resolved = versions.resolve(Dependencies(crud.get_project(uid)), spec())
    previews.record(SimpleNamespace(job_id=1, task_data={}), resolved, old, 'old-input', start=0, end=10)
    # 元数据以生成时间排序，显式让新试片生成快照更晚。
    previews.record(SimpleNamespace(job_id=2, task_data={}), resolved, latest, 'latest-input', start=10, end=20)
    body = recycle_body(uid, old)
    size = old.stat().st_size
    trash = tmp_path / 'simulated-recycle.mp4'
    monkeypatch.setattr(previews, 'recycle_file', lambda p: p.rename(trash))
    before_steps = [(row.step_key, row.status, row.input_fingerprint) for row in crud.get_steps(crud.get_project(uid).id)]
    result = previews.recycle(uid, body)
    assert result['recycled_count'] == 1 and result['recycled_bytes'] == size and result['reclaimed_bytes'] == 0
    assert not old.exists() and latest.exists() and previews.metadata_path(old).exists()
    if artifact:
        assert any(row['name'] == old.name and row['exists'] is False for row in result['history']['items'])
    trash.rename(old)
    item = next(row for row in previews.history(uid)['items'] if row['name'] == old.name)
    assert item['exists'] and item['metadata_status'] == 'ready'
    assert before_steps == [(row.step_key, row.status, row.input_fingerprint) for row in crud.get_steps(crud.get_project(uid).id)]


@pytest.mark.parametrize('change', ['revision', 'file', 'artifact', 'confirmation', 'latest', 'keep', 'foreign', 'version'])
def test_confirmation_identity_current_and_version_protection(make_project, monkeypatch, change):
    uid = make_project()
    old, _ = video(uid)
    latest, _ = video(uid)
    body = recycle_body(uid, old)
    if change == 'revision': body['items'][0]['file_revision'] = 'old'
    elif change == 'file': old.write_bytes(b'new-file')
    elif change == 'artifact': body['items'][0]['artifact_id'] += 1
    elif change == 'confirmation': body['confirmed'] = False
    elif change == 'latest': body = recycle_body(uid, latest)
    elif change == 'keep': body['keep_name'] = old.name
    elif change == 'foreign': uid = make_project()
    elif change == 'version': body['version_id'] = 'other'
    monkeypatch.setattr(previews, 'recycle_file', lambda p: pytest.fail('无效请求不能回收'))
    with pytest.raises(previews.PreviewError): previews.recycle(uid, body)
    assert old.exists() and latest.exists()


@pytest.mark.parametrize('target', ['traversal', 'formal', 'source', 'directory', 'symlink'])
def test_only_owned_regular_output_previews_can_be_accessed(make_project, tmp_path, target):
    uid = make_project()
    path, artifact = video(uid)
    name = path.name
    if target == 'traversal': name = '../' + name
    elif target == 'formal': name = 'final_version_test.mp4'; (path.parent / name).write_bytes(b'formal')
    elif target == 'source':
        source = crud.project_dir(uid) / 'media' / name
        path.rename(source)
        artifact.rel_path = str(source)
        with crud.session() as session: session.add(artifact); session.commit()
    elif target == 'directory': path.unlink(); path.mkdir()
    else:
        source = tmp_path / 'protected.mp4'; path.rename(source)
        try: path.symlink_to(source)
        except OSError: pytest.skip('测试环境不支持符号链接')
    with pytest.raises(previews.PreviewError): previews.media_path(uid, name)
    if target in {'source', 'directory', 'symlink'}:
        assert not previews.history(uid)['items']


@pytest.mark.parametrize('busy', ['running', 'queued', 'finalizing', 'archived', 'publishing', 'reference'])
def test_recycle_respects_running_archived_and_publication_guards(make_project, monkeypatch, busy):
    uid = make_project()
    old, artifact = video(uid)
    video(uid)
    body = recycle_body(uid, old)
    project = crud.get_project(uid)
    from dubforge.runner import runner
    if busy in {'running', 'queued'}:
        step = crud.get_step(project.id, 'assemble'); step.status = busy; crud.save_step(step)
    elif busy == 'finalizing': monkeypatch.setattr(runner, 'current_job_id', lambda *a: 9)
    elif busy == 'archived':
        import datetime
        project.archived_at = datetime.datetime.now(datetime.timezone.utc)
        with crud.session() as session: session.add(project); session.commit()
    else:
        row = Publication(project_uid=uid, status='uploading' if busy == 'publishing' else 'draft',
            draft=json.dumps(dict(parts=[dict(artifact_id=artifact.id)])))
        with crud.session() as session: session.add(row); session.commit()
    monkeypatch.setattr(previews, 'recycle_file', lambda p: pytest.fail('受保护文件不能回收'))
    with pytest.raises(Exception): previews.recycle(uid, body)
    assert old.exists()


def test_api_history_range_and_cross_origin_recycle(make_project):
    uid = make_project()
    old, _ = video(uid)
    video(uid)
    from app.main import app
    client = TestClient(app)
    response = client.get(f'/api/projects/{uid}/assembly-versions/preview-history?version_id=test')
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    url = next(row for row in response.json()['items'] if row['name'] == old.name)['url']
    media = client.get(url, headers={'range': 'bytes=0-7'})
    assert media.status_code == 206 and media.content == old.read_bytes()[:8]
    response = client.post(f'/api/projects/{uid}/assembly-versions/preview-history/recycle',
        json=recycle_body(uid, old), headers={'Origin': 'https://outside.example'})
    assert response.json()['code'] == 'ASSEMBLY_PREVIEW_ORIGIN' and old.exists()


def test_unindexed_preview_audio_report_is_readonly_and_bound_to_file_identity(make_project, monkeypatch):
    uid = make_project()
    path, _ = video(uid, register=False)
    from dubforge.pipeline import audio_quality
    audio_quality.write_report(path, dict(schema=1, integrated_lufs=-16, true_peak_db=-1.7))
    monkeypatch.setattr(audio_quality, 'measure', lambda *a, **k: pytest.fail('GET不能重新测量声音'))
    from app.main import app
    client = TestClient(app)
    item = client.get(f'/api/projects/{uid}/assembly-versions/preview-history').json()['items'][0]
    response = client.get(item['report_url'])
    assert response.status_code == 200 and response.json()['report']['integrated_lufs'] == -16
    assert not crud.latest_artifacts(crud.get_project(uid).id, ['assemble'])
    path.write_bytes(b'replaced-output')
    assert client.get(item['report_url']).json()['report'] is None


def test_batch_validates_all_before_first_recycle_and_reports_partial_failure(make_project, monkeypatch, tmp_path):
    uid = make_project()
    first, _ = video(uid)
    second, _ = video(uid)
    video(uid)
    rows = previews.history(uid)['items']
    chosen = [selection(next(row for row in rows if row['name'] == path.name)) for path in (first, second)]
    body = dict(confirmed=True, version_id='test', items=copy.deepcopy(chosen))
    body['items'][1]['file_revision'] = 'changed'
    monkeypatch.setattr(previews, 'recycle_file', lambda *a: pytest.fail('全部校验通过之前不能修改文件'))
    with pytest.raises(previews.PreviewError): previews.recycle(uid, body)
    body['items'] = chosen
    from dubforge.utils.recycle import RecycleError
    def recycle(path):
        if path == second: raise RecycleError('视频仍被播放器占用')
        path.rename(tmp_path / 'recycled.mp4')
    monkeypatch.setattr(previews, 'recycle_file', recycle)
    result = previews.recycle(uid, body)
    assert result['status'] == 'partial' and result['recycled_count'] == 1 and result['failed_count'] == 1
    assert result['failures'][0]['name'] == second.name and second.exists() and not first.exists()


def test_repeated_assembly_reset_preserves_only_strict_version_preview_history(make_project):
    uid = make_project()
    path, artifact = video(uid)
    root = crud.project_dir(uid)
    bad = path.with_name('preview_loose.mp4'); bad.write_bytes(b'old-format')
    crud.add_artifact(artifact.project_id, 'assemble', 'video', bad.name, bad, str(bad.relative_to(root)))
    for _ in range(2): crud.delete_step_artifacts(artifact.project_id, 'assemble')
    rows = crud.latest_artifacts(artifact.project_id, ['assemble'])
    assert rows['assemble/' + path.name].id == artifact.id and path.exists()
    assert 'assemble/' + bad.name not in rows


def test_preview_full_voice_is_cleaned_if_mux_fails_after_mix(make_project, monkeypatch):
    uid = make_project()
    from dubforge.pipeline import version_render, step_assemble, content_redaction
    project, root = crud.get_project(uid), crud.project_dir(uid)
    source = root / 'media' / 'source.mp4'; source.write_bytes(b'isolated-source')
    deps = Dependencies(project)
    resolved = versions.resolve(deps, {**spec(), 'dubbing': True})
    ctx = SimpleNamespace(project=project, pdir=root,
        task_data=dict(scope='preview', start=0, end=2, version_specs=[resolved]),
        dependencies=lambda: deps, check_cancel=lambda: None, check_pause=lambda: None,
        cfg=lambda key, default=None: default, log=lambda *a: None)
    monkeypatch.setattr(version_render, 'resolve_video', lambda *a: source)
    monkeypatch.setattr(step_assemble, '_ffprobe_duration', lambda *a: 10)
    monkeypatch.setattr(step_assemble, '_task_range', lambda *a: (0, 2))
    monkeypatch.setattr(step_assemble, '_require_dubbing', lambda *a: None)
    monkeypatch.setattr(content_redaction, 'plan', lambda *a: dict(ranges=[], blocked_ids=[]))
    monkeypatch.setattr(versions, 'fingerprint', lambda *a: 'actual-input')
    full = root / 'audio' / '.tmp_full_preview.wav'
    voice = root / 'audio' / '.tmp_voice_preview.wav'
    def mix(scoped, *args, **kwargs):
        full.write_bytes(b'full'); voice.write_bytes(b'voice'); scoped._preview_full_voice = full
        return voice
    def failed(*args, **kwargs): raise versions.VersionError('编码前取消')
    monkeypatch.setattr(step_assemble, '_mix_timeline', mix)
    monkeypatch.setattr(step_assemble, '_mux', failed)
    with pytest.raises(versions.VersionError, match='编码前取消'): version_render.run(ctx)
    assert not full.exists() and not voice.exists()


@pytest.mark.parametrize('comments', [True, False])
def test_runner_to_generated_preview_preserves_only_relevant_comment_identity(make_project, monkeypatch, comments):
    """经过真实任务入队、Job快照及版本渲染，不能只在手工ctx上验证旁路元数据。"""
    import time
    from conftest import wait_status
    from test_assemble import _prepare_outputs_project
    from dubforge import comment_studio
    from dubforge.pipeline import step_assemble, content_redaction
    from dubforge.runner import runner
    uid, root = _prepare_outputs_project(make_project, monkeypatch)
    version = {**spec(), 'include_comments': comments, 'burn_subtitles': not comments}
    overlay = dict(settings={**comment_studio.default_settings(), 'start': .2, 'interval': 1, 'duration': 1},
                   items=[dict(id='c1', source_hash='source', text='Hello', display_text='你好', author='A')])
    def confirmed(project_uid, enabled=False, revision=None, variants=()):
        assert project_uid == uid and revision == 'frozen-comment-identity'
        return copy.deepcopy(overlay) if enabled else None
    monkeypatch.setattr(comment_studio, 'for_task', confirmed)
    # 本用例验证编排身份而非FFmpeg；已有真实媒体测试负责渲染画质与测量。
    monkeypatch.setattr(content_redaction, 'safe_overlay', lambda ctx, value: value)
    def mux(ctx, video, output, *args, **kwargs): output.write_bytes(b'generated-from-frozen-job')
    monkeypatch.setattr(step_assemble, '_mux_original_sub', mux)
    result = runner.start_versions(uid, dict(version=version, start=0, end=2,
        overlay_revision='frozen-comment-identity'), preview=True)
    assert result['status'] == 'started'
    wait_status(uid, 'assemble', {'done'}, timeout=15)
    until = time.monotonic() + 10
    while runner.current_job_id(crud.get_project(uid).id, 'assemble') is not None:
        assert time.monotonic() < until
        time.sleep(.01)
    item = previews.history(uid, 'test')['items'][0]
    assert item['metadata_status'] == 'ready' and item['job_id'] == result['job_id']
    assert item['snapshot']['overlay_revision'] == ('frozen-comment-identity' if comments else None)
    assert bool(item['snapshot']['comment_plan']) is comments


@pytest.mark.parametrize('mute,enabled,required', [(True, True, True), (False, True, False), (True, False, False)])
def test_preview_context_covers_original_only_audio_redaction(make_project, monkeypatch, mute, enabled, required):
    uid = make_project()
    from test_configuration import save
    from dubforge.pipeline import version_render
    save('project:' + uid, {'content_review.enabled': enabled, 'content_review.mute_audio': mute,
                           'content_review.blocked_ranges': '1-2:人工屏蔽'})
    monkeypatch.setattr(version_render, 'resolve_video', lambda *a: None)
    context = version_render.preview_context(uid)
    deps = Dependencies(crud.get_project(uid))
    resolved = versions.resolve(deps, spec())
    assert context['audio_redaction_required'] is required
    assert context['audio_redaction_error'] == ''
    assert version_render.uses_source_directly(version_render.VersionContext(deps, resolved), resolved) is (not required)


@pytest.mark.parametrize('mute,enabled,required', [(True, True, True), (False, True, False), (True, False, False)])
def test_version_status_uses_actual_source_reuse_rule_for_audio_redaction(make_project, mute, enabled, required):
    uid = make_project()
    from test_configuration import save
    save('project:' + uid, {'assembly.versions': [spec()], 'content_review.enabled': enabled,
        'content_review.mute_audio': mute, 'content_review.blocked_ranges': '1-2:人工屏蔽'})
    result = versions.view(uid)['versions'][0]
    assert result['uses_source_directly'] is (not required)
    assert (result['status'] == '原片直用') is (not required)
    assert result['render_warning'] == ''
    if required:
        assert result['status'] == '尚未生成'


def test_source_reuse_status_shares_text_review_and_history_never_loads_review(make_project, monkeypatch):
    uid = make_project()
    from test_configuration import save
    from dubforge.pipeline import content_redaction
    save('project:' + uid, {'assembly.versions': [spec('a'), spec('b')],
                           'content_review.blocked_ranges': '1-2:人工屏蔽'})
    original = content_redaction.plan
    hits = []
    def plan(ctx):
        hits.append(getattr(ctx, '_content_redaction_plan', None) is not None)
        return original(ctx)
    monkeypatch.setattr(content_redaction, 'plan', plan)
    result = versions.view(uid)
    assert [row['uses_source_directly'] for row in result['versions']] == [False, False]
    assert hits == [False, True], '本次版本读取只筛查一次文字，不能每个原声版本重复读取'
    monkeypatch.setattr(content_redaction, 'plan', lambda *a: pytest.fail('历史轮询不能触发文本筛查'))
    assert previews.history(uid)['items'] == []


def test_non_source_versions_do_not_add_review_work_for_status(make_project, monkeypatch):
    uid = make_project()
    from test_configuration import save
    from dubforge.pipeline import content_redaction
    save('project:' + uid, {'assembly.versions': [{**spec(), 'burn_subtitles': True}]})
    monkeypatch.setattr(content_redaction, 'plan', lambda *a: pytest.fail('非原片直用候选不额外筛查文字'))
    assert versions.view(uid)['versions'][0]['uses_source_directly'] is False
