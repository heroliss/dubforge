"""更新原因与实际生成输入一致；全部配置、产物、数据库和执行均隔离。"""
import hashlib
import json
from concurrent.futures import Future
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from conftest import mark_upstream_done
from dubforge import config, configuration, db
from dubforge.domain import crud
from dubforge.domain.models import Artifact, Job, Project, StepRun
from dubforge.errors import DubForgeError
from dubforge.pipeline import steps
from dubforge.runner import StepContext, compute_fingerprint, runner
from dubforge.utils.fs import atomic_write_bytes
from test_dubbing_preparation import prepared


@pytest.fixture
def history_env(make_project, monkeypatch):
    config.set_key('runner.auto_tts_vram', False)
    config.set_key('download.resolution', 1080)
    uid = make_project()
    futures, calls = [], []
    original_submit = runner._pool.submit

    def submit(*args, **kwargs):
        future = original_submit(*args, **kwargs)
        futures.append(future)
        return future

    def download(ctx):
        calls.append(ctx.cfg('download.resolution'))
        ctx.write_bytes('video.mp4', b'isolated downloaded video', 'video')

    monkeypatch.setattr(runner._pool, 'submit', submit)
    monkeypatch.setitem(steps._RUNNERS, 'download', download)
    return SimpleNamespace(uid=uid, root=crud.project_dir(uid), futures=futures,
                           calls=calls, download=download)


def _finish(env):
    for future in env.futures:
        future.result(timeout=10)
    return crud.get_step(crud.get_project(env.uid).id, 'download')


def _run(env, *, force=False):
    result = runner.start_step(env.uid, 'download', force=force)
    assert result['status'] == 'started'
    step = _finish(env)
    assert step.status == 'done'
    return step


def _get(uid, key='download'):
    from app.main import app
    response = TestClient(app).get(f'/api/projects/{uid}/freshness', params={'step': key})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['project_uid'] == uid and len(result['steps']) == 1
    return result['steps'][0]


def _change(report, path):
    return next(row for row in report['changes'] if row['key'].endswith(path))


def _rows_and_files(uid):
    """不比较SQLite的WAL物理页，只核对持久化记录与项目文件是否发生写入。"""
    project = crud.get_project(uid)
    with crud.session() as session:
        rows = {model.__name__: [row.model_dump() for row in session.exec(select(model)).all()]
                for model in (Project, StepRun, Artifact, Job)}
    root = crud.project_dir_by_project(project)
    files = {str(path.relative_to(root)): (path.stat().st_mtime_ns,
             hashlib.sha256(path.read_bytes()).hexdigest()) for path in root.rglob('*') if path.is_file()}
    return rows, files, config.config_path().read_bytes()


def test_success_then_config_change_reports_precise_before_after_without_get_writes(history_env):
    step = _run(history_env)
    assert json.loads(step.input_snapshot)
    current = _get(history_env.uid)
    assert current['history_available'] and current['fresh'] and not current['changes']
    config.set_key('download.resolution', 720)
    before = _rows_and_files(history_env.uid)
    changed = _get(history_env.uid)
    change = _change(changed, 'download.resolution')
    assert changed['history_available'] and changed['fresh'] is False
    assert change['before'] == '1080' and change['after'] == '720' and change['before_known']
    assert change['origin_before'] and change['origin_after']
    assert _rows_and_files(history_env.uid) == before
    from app.main import app
    overview = TestClient(app).get(f'/api/projects/{history_env.uid}/freshness').json()
    assert [row['key'] for row in overview['steps']] == ['download']
    assert _rows_and_files(history_env.uid) == before


def test_config_changed_during_execution_keeps_the_consumed_old_baseline(history_env, monkeypatch):
    started, release = Event(), Event()

    def controlled(ctx):
        started.set()
        assert release.wait(10)
        history_env.download(ctx)

    monkeypatch.setitem(steps._RUNNERS, 'download', controlled)
    runner.start_step(history_env.uid, 'download')
    try:
        assert started.wait(5)
        config.set_key('download.resolution', 720)
    finally:
        release.set()
    step = _finish(history_env)
    assert step.status == 'done' and history_env.calls == [1080]
    report = _get(history_env.uid)
    change = _change(report, 'download.resolution')
    assert change['before'] == '1080' and change['after'] == '720' and change['before_known']
    assert report['fresh'] is False


def test_queued_fingerprint_and_snapshot_match_actual_context_values(history_env, monkeypatch):
    queued = []

    def defer(function, *args, **kwargs):
        future = Future()
        queued.append((future, function, args, kwargs))
        return future

    monkeypatch.setattr(runner._pool, 'submit', defer)
    runner.start_step(history_env.uid, 'download')
    config.set_key('download.resolution', 720)
    future, function, args, kwargs = queued[0]
    try:
        function(*args, **kwargs)
    finally:
        future.set_result(None)
    assert history_env.calls == [720]
    step = crud.get_step(crud.get_project(history_env.uid).id, 'download')
    assert step.status == 'done'
    assert step.input_fingerprint == compute_fingerprint(crud.get_project(history_env.uid), 'download')
    report = _get(history_env.uid)
    assert report['history_available'] and report['fresh'] and not report['changes']


def test_failed_attempt_does_not_replace_last_successful_input_snapshot(history_env, monkeypatch):
    baseline = _run(history_env).input_snapshot
    config.set_key('download.resolution', 720)

    def fail(ctx):
        raise DubForgeError('隔离模拟下载失败', code='DOWNLOAD_FAIL')

    monkeypatch.setitem(steps._RUNNERS, 'download', fail)
    runner.start_step(history_env.uid, 'download', force=True)
    step = _finish(history_env)
    assert step.status == 'failed' and step.input_snapshot == baseline
    # 最新失败尝试不能使已保留的成功输入变成未知历史。
    report = _get(history_env.uid)
    change = _change(report, 'download.resolution')
    assert report['history_available'] and change['before_known']
    assert (change['before'], change['after']) == ('1080', '720')
    assert report['fresh'] is False
    assert any('上次成功' in warning and '最近一次制作未完成' in warning for warning in report['warnings'])


def test_legacy_skip_keeps_history_unknown_instead_of_inventing_current_baseline(history_env):
    project = crud.get_project(history_env.uid)
    path = history_env.root / 'artifacts/download/video.mp4'
    atomic_write_bytes(path, b'isolated legacy video')
    crud.add_artifact(project.id, 'download', 'video', 'video.mp4', path, str(path.relative_to(history_env.root)))
    step = crud.get_step(project.id, 'download')
    step.status, step.input_fingerprint, step.input_snapshot = 'done', compute_fingerprint(project, 'download'), '{}'
    crud.save_step(step)
    assert runner.start_step(history_env.uid, 'download')['status'] == 'skipped'
    assert crud.get_step(project.id, 'download').input_snapshot == '{}'
    assert history_env.calls == []
    config.set_key('download.resolution', 720)
    before = _rows_and_files(history_env.uid)
    report = _get(history_env.uid)
    assert report['history_available'] is False and report['warnings']
    assert all(not row['before_known'] for row in report['changes'])
    assert _rows_and_files(history_env.uid) == before


def test_successful_input_snapshot_survives_manifest_rebuild(history_env):
    baseline = _run(history_env).input_snapshot
    project = crud.get_project(history_env.uid)
    manifest = json.loads((history_env.root / 'manifest.json').read_text(encoding='utf-8'))
    serialized = next(row for row in manifest['steps'] if row['key'] == 'download')['input_snapshot']
    assert (json.loads(serialized) if isinstance(serialized, str) else serialized) == json.loads(baseline)
    with crud.session() as session:
        for model in (Job, Artifact, StepRun, Project):
            for row in session.exec(select(model)).all():
                session.delete(row)
        session.commit()
    assert crud.get_project(project.uid) is None
    assert crud.rebuild_from_dirs()['projects'] == 1
    restored = crud.get_step(crud.get_project(project.uid).id, 'download')
    assert json.loads(restored.input_snapshot) == json.loads(baseline)
    # 重建会同时恢复配置层；通过项目稀疏覆盖更新有效值，不改被恢复层覆盖的文件默认。
    settings = configuration.resolve(project.uid)
    configuration.save('project:' + project.uid, dict(revision=settings['revision'], updates={'download.resolution': 720}))
    change = _change(_get(project.uid), 'download.resolution')
    assert (change['before'], change['after']) == ('1080', '720') and change['before_known']


def test_proxy_credentials_are_absent_from_saved_snapshot_manifest_and_public_diff(history_env):
    config.set_key('download.proxy', 'http://audit-user:audit-password@example.invalid:7897/?token=audit-token')
    step = _run(history_env)
    config.set_key('download.proxy', 'http://new-user:new-password@example.invalid:7898/?token=new-token')
    report = _get(history_env.uid)
    evidence = step.input_snapshot + json.dumps(report, ensure_ascii=False)
    # manifest当前配置已另有保存格式；这里只核对新增输入历史分支，不混淆现有配置备份。
    manifest = json.loads((history_env.root / 'manifest.json').read_text(encoding='utf-8'))
    evidence += json.dumps(next(row for row in manifest['steps'] if row['key'] == 'download')['input_snapshot'])
    for secret in ('audit-user', 'audit-password', 'audit-token', 'new-user', 'new-password', 'new-token'):
        assert secret not in evidence


def test_preview_success_does_not_promote_production_input_snapshot(history_env, monkeypatch):
    uid = history_env.uid
    mark_upstream_done(uid, 'assemble')
    project = crud.get_project(uid)
    step = crud.get_step(project.id, 'assemble')
    baseline = json.dumps({'schema': 1, 'step': 'assemble', 'fingerprint': 'existing-production', 'scope': 'full'})
    step.input_snapshot, step.input_fingerprint = baseline, 'existing-production'
    crud.save_step(step)

    def preview(ctx):
        ctx.write_bytes('preview_isolated.mp4', b'isolated preview media', 'video')

    monkeypatch.setitem(steps._RUNNERS, 'assemble', preview)
    result = runner.start_assemble_preview(uid, 0, 1)
    assert result['status'] == 'started'
    for future in history_env.futures:
        future.result(timeout=10)
    completed = crud.get_step(project.id, 'assemble')
    assert completed.status == 'done' and completed.input_snapshot == baseline
    assert completed.input_fingerprint == 'existing-production'


def test_selected_dubbing_success_does_not_overwrite_whole_step_baseline(prepared, monkeypatch):
    uid, project, _ = prepared
    config.set_key('runner.auto_tts_vram', False)
    baseline = json.dumps({'schema': 1, 'step': 'tts', 'fingerprint': 'previous-whole-batch', 'scope': 'full'})
    step = crud.get_step(project.id, 'tts')
    step.input_snapshot, step.input_fingerprint = baseline, 'previous-whole-batch'
    crud.save_step(step)
    pending = []
    submit = runner._pool.submit

    def track(*args, **kwargs):
        future = submit(*args, **kwargs)
        pending.append(future)
        return future

    monkeypatch.setattr(runner._pool, 'submit', track)
    runner.start_dubbing(uid, [1])
    for future in pending:
        future.result(timeout=10)
    completed = crud.get_step(project.id, 'tts')
    assert completed.status == 'done' and completed.input_snapshot == baseline
    assert completed.input_fingerprint == 'previous-whole-batch'


def _comment_render_inputs(env):
    """构造真实冻结上下文与实际评论规格，不采集评论或调用模型。"""
    from dubforge import comment_studio
    from dubforge.pipeline import assembly_versions as av, content_redaction, version_render
    from dubforge.pipeline.stale_explanation import capture

    config.set_key('assemble.crf', 22)
    mark_upstream_done(env.uid, 'assemble')
    project = crud.get_project(env.uid)
    log = env.root / 'logs/assemble_history.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    ctx = StepContext(project, 'assemble', env.root, log, Event())
    ctx.task_data = {'scope': 'versions', 'comments_auto': True}
    deps = ctx.dependencies()
    definition = dict(id='auto-comments', name='自动评论', dubbing=False,
                      burn_subtitles=False, include_comments=True, overrides={})
    initial = av.resolve(deps, definition)
    before = capture(deps.with_versions([initial]), 'assemble', scope='versions')
    overlay = dict(settings={**comment_studio.DEFAULT_SETTINGS, 'start': 0, 'interval': 1, 'duration': 1},
                   items=[dict(id='unsafe', text='the tragedy of Tiananmen Square',
                               source_text='the tragedy of Tiananmen Square', display_text='天安门广场的悲剧'),
                          dict(id='safe', text='A lovely city with beautiful architecture',
                               source_text='A lovely city with beautiful architecture', display_text='美丽的城市和建筑')])
    unfiltered = av.resolve(deps, definition, comment_plan=overlay)
    final = {**unfiltered, 'comment_overlay': content_redaction.safe_overlay(
        version_render.VersionContext(ctx, unfiltered), unfiltered['comment_overlay'])}
    return ctx, deps, initial, before, unfiltered, final


def test_auto_comment_prepared_snapshot_uses_final_safe_overlay_and_frozen_settings(history_env):
    from dubforge import comment_studio
    from dubforge.pipeline import assembly_versions as av, version_render

    ctx, deps, _, initial, unfiltered, final = _comment_render_inputs(history_env)
    # 自动评论准备期间修改全局参数，成片继续消费本轮固定的22。
    config.set_key('assemble.crf', 35)
    version_render.capture_prepared_inputs(ctx, deps, [final])
    actual = ctx._completed_input_snapshot
    expected = comment_studio.render_fingerprint_plan(final['comment_overlay'])
    digest = hashlib.sha256(json.dumps(expected, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    entry = actual['entries']['version.auto-comments.text.comments']
    assert entry['value'] == digest and '内容摘要' in entry['display']
    assert entry['value'] != initial['entries']['version.auto-comments.text.comments']['value']
    unsafe = comment_studio.render_fingerprint_plan(unfiltered['comment_overlay'])
    assert entry['value'] != hashlib.sha256(json.dumps(unsafe, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    assert [item['id'] for item in final['comment_overlay']['items']] == ['safe']
    assert actual['entries']['version.auto-comments.assemble.crf']['value'] == 22
    assert actual['scope'] == 'versions' and actual['selection']['version_ids'] == ['auto-comments']
    assert actual['fingerprint'] == 'versions:' + av.digest([av.fingerprint(deps, final)])
    serialized = json.dumps(actual, ensure_ascii=False)
    assert 'A lovely city' not in serialized and '天安门广场的悲剧' not in serialized


def test_comment_preview_does_not_create_completed_production_snapshot(history_env):
    from dubforge.pipeline import version_render

    ctx, deps, _, _, _, final = _comment_render_inputs(history_env)
    ctx.task_data['scope'] = 'preview'
    version_render.capture_prepared_inputs(ctx, deps, [final])
    assert not hasattr(ctx, '_completed_input_snapshot')


def test_success_promotes_prepared_comment_snapshot_instead_of_empty_enqueue_snapshot(history_env, monkeypatch):
    from dubforge import comments_auto
    from dubforge.pipeline import assembly_versions as av, version_render

    _, _, initial_spec, initial_snapshot, _, final = _comment_render_inputs(history_env)
    definition = {key: initial_spec[key] for key in ('id', 'name', 'dubbing', 'burn_subtitles', 'include_comments', 'overrides')}
    av.save(history_env.uid, dict(revision=av.catalog(history_env.uid)['revision'], versions=[definition]))
    monkeypatch.setattr(comments_auto, 'confirmed_or_auto', lambda *args: (None, True))

    def encode_without_network(ctx):
        # 保留真实运行器、冻结依赖、摘要捕获和提交，仅将媒体编码替换为隔离字节。
        config.set_key('assemble.crf', 35)
        ctx.task_data['version_specs'] = [final]
        version_render.capture_prepared_inputs(ctx, ctx.dependencies(), [final])
        ctx.write_bytes('final_version_auto-comments.mp4', b'isolated reviewed comments video', 'video')

    monkeypatch.setitem(steps._RUNNERS, 'assemble', encode_without_network)
    result = runner.start_step(history_env.uid, 'assemble')
    assert result['status'] == 'started'
    for future in history_env.futures:
        future.result(timeout=10)
    step = crud.get_step(crud.get_project(history_env.uid).id, 'assemble')
    actual = json.loads(step.input_snapshot)
    assert step.status == 'done' and actual['fingerprint'] == step.input_fingerprint
    assert step.input_fingerprint != initial_snapshot['fingerprint']
    assert actual['entries']['version.auto-comments.text.comments']['value'] != initial_snapshot['entries']['version.auto-comments.text.comments']['value']
    assert actual['entries']['version.auto-comments.assemble.crf']['value'] == 22


def test_old_step_table_upgrade_adds_empty_history_without_rewriting_fingerprint(make_project):
    uid = make_project()
    project = crud.get_project(uid)
    engine = db.get_engine()
    with engine.begin() as connection:
        connection.exec_driver_sql("UPDATE steprun SET status='done', input_fingerprint='historical-fingerprint' "
                                   "WHERE project_id=? AND step_key='download'", (project.id,))
        # 只在隔离数据库复现升级前的真实表结构，不触碰生产库。
        connection.exec_driver_sql('ALTER TABLE steprun DROP COLUMN input_snapshot')
        assert 'input_snapshot' not in {row[1] for row in connection.exec_driver_sql('PRAGMA table_info(steprun)')}
    db._ensure_columns()
    db.init_db()  # 迁移可重复执行；原索引和旧输入指纹继续保留。
    with engine.connect() as connection:
        assert 'input_snapshot' in {row[1] for row in connection.exec_driver_sql('PRAGMA table_info(steprun)')}
        row = connection.exec_driver_sql('SELECT status, input_fingerprint, input_snapshot FROM steprun '
                                         "WHERE project_id=? AND step_key='download'", (project.id,)).one()
        assert tuple(row) == ('done', 'historical-fingerprint', '{}')
    step = crud.get_step(project.id, 'download')
    assert step.input_snapshot == '{}' and step.input_fingerprint == 'historical-fingerprint'
