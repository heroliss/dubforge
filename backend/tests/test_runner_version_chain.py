"""版本合成的制作链收尾、任务归属与重启核对；全部使用隔离产物。"""
import json
import threading

import pytest

from conftest import mark_upstream_done, wait_status
from dubforge import config
from dubforge.domain import crud
from dubforge.pipeline import assembly_versions as versions
from dubforge.runner import runner


def save_params(uid, **updates):
    project = crud.get_project(uid)
    params = crud.project_params(project)
    params.update(updates)
    project.params = json.dumps(params, ensure_ascii=False)
    crud.save_project(project)
    return params


def prepared(make_project):
    uid = make_project()
    mark_upstream_done(uid, 'assemble')
    return uid


@pytest.fixture
def no_publication(monkeypatch):
    from dubforge import workflows
    from dubforge.publishing import service, transport

    def forbidden(*args, **kwargs):
        pytest.fail('制作链收尾和恢复不得创建完整流程、发布稿件或调用平台')

    monkeypatch.setattr(workflows, 'start', forbidden)
    monkeypatch.setattr(service, 'create', forbidden)
    monkeypatch.setattr(service, 'enqueue', forbidden)
    monkeypatch.setattr(transport, 'call', forbidden)


def test_normal_chain_version_tail_finishes_and_preserves_other_params(
        make_project, stub_heavy_steps, no_publication):
    stub_heavy_steps('assemble')
    uid = prepared(make_project)
    save_params(uid, _chain=['assemble'], _chain_state='active',
                _chain_resume_receipts={'accepted': {'result': '保留'}}, note='保留用户资料')
    result = runner.start_step(uid, 'assemble')
    assert result['status'] == 'started'
    wait_status(uid, 'assemble', {'done'})
    project = crud.get_project(uid)
    params = crud.project_params(project)
    assert not any(key in params for key in ('_chain', '_chain_state', '_chain_hold',
                                             '_chain_resume_plan', '_chain_job_id'))
    assert params['note'] == '保留用户资料'
    assert params['_chain_resume_receipts'] == {'accepted': {'result': '保留'}}
    task = json.loads(crud.latest_job(project.id, 'assemble').task_data)
    assert task['scope'] == 'versions' and task['chain_binding']['signature']


@pytest.mark.parametrize('change', ['stop', 'replace', 'replace_job', 'hold'])
def test_finished_version_task_does_not_advance_changed_plan(
        make_project, stub_heavy_steps, monkeypatch, no_publication, change):
    from dubforge.pipeline import steps
    stub_heavy_steps('assemble')
    uid = prepared(make_project)
    save_params(uid, _chain=['assemble'], _chain_state='active')
    original = steps._RUNNERS['assemble']
    entered, release = threading.Event(), threading.Event()

    def blocked(ctx):
        entered.set()
        assert release.wait(5)
        original(ctx)

    monkeypatch.setitem(steps._RUNNERS, 'assemble', blocked)
    runner.start_step(uid, 'assemble')
    assert entered.wait(5)
    try:
        if change == 'stop':
            runner._clear_chain(crud.get_project(uid), '隔离测试取消计划')
        elif change == 'replace':
            save_params(uid, _chain_resume_plan={'replacement': '后来确认的计划'})
        elif change == 'replace_job':
            job = crud.latest_job(crud.get_project(uid).id, 'assemble')
            save_params(uid, _chain_job_id=job.id + 1)
        else:
            save_params(uid, _chain_state='held',
                        _chain_hold={'step': 'assemble', 'code': 'STEP_CANCELLED', 'message': '停止计划'})
        expected = crud.project_params(crud.get_project(uid))
    finally:
        release.set()
    wait_status(uid, 'assemble', {'done'})
    assert crud.project_params(crud.get_project(uid)) == expected


def test_normal_chain_version_failure_keeps_plan(make_project, stub_heavy_steps, no_publication):
    stub_heavy_steps('assemble')
    uid = prepared(make_project)
    config.set_key('runner.step_retries.assemble', 0)
    save_params(uid, _chain=['assemble'], _chain_state='active', fail_step='assemble')
    runner.start_step(uid, 'assemble')
    wait_status(uid, 'assemble', {'failed'})
    params = crud.project_params(crud.get_project(uid))
    assert params['_chain'] == ['assemble'] and params['_chain_state'] == 'held'
    assert params['_chain_hold']['code'] == 'STEP_FAILED'


@pytest.mark.parametrize('entry', ['versions', 'preview', 'outputs', 'single'])
def test_independent_task_does_not_adopt_later_chain(
        make_project, stub_heavy_steps, monkeypatch, no_publication, entry):
    from dubforge.pipeline import steps
    stub_heavy_steps('assemble')
    uid = prepared(make_project)
    original = steps._RUNNERS['assemble']
    entered, release = threading.Event(), threading.Event()

    def blocked(ctx):
        assert not ctx.task_data.get('chain_binding')
        entered.set()
        assert release.wait(5)
        original(ctx)

    monkeypatch.setitem(steps._RUNNERS, 'assemble', blocked)
    if entry == 'single':
        runner.start_step(uid, 'assemble')
    elif entry == 'outputs':
        runner.start_assemble_outputs(uid, outputs=[dict(dubbing=True, burn_subtitles=False,
                                                        include_comments=False)])
    else:
        preview = versions.plan(uid, {'version_ids': ['default']})
        body = {'version_ids': ['default'], 'fingerprint': preview['fingerprint'], 'start': 0, 'end': 1}
        runner.start_versions(uid, body, preview=entry == 'preview')
    assert entered.wait(5)
    try:
        expected = save_params(uid, _chain=['assemble'], _chain_state='active',
                               _chain_resume_plan={'replacement': '新的制作计划'})
    finally:
        release.set()
    wait_status(uid, 'assemble', {'done'})
    assert crud.project_params(crud.get_project(uid)) == expected


def legacy_completed_tail(make_project, stub_heavy_steps, *, held=False):
    stub_heavy_steps('assemble')
    uid = prepared(make_project)
    runner.start_step(uid, 'assemble')
    wait_status(uid, 'assemble', {'done'})
    definition_digest = versions.digest(versions.catalog(uid)['versions'])
    save_params(uid, _chain=['assemble'], _chain_state='held' if held else 'active',
                _chain_resume_plan={'version_definitions_digest': definition_digest},
                _chain_resume_receipts={'accepted': {'result': '保留'}})
    if held:
        save_params(uid, _chain_hold={'step': 'assemble', 'code': 'STEP_INTERRUPTED', 'message': '旧恢复残留'})
    return uid


def forbid_restart_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('启动核对不得派发任何制作任务')
    monkeypatch.setattr(runner, 'start_step', forbidden)
    original = versions.is_current

    def without_large_hash(*args, **kwargs):
        assert not kwargs.get('verify_hash')
        return original(*args, **kwargs)
    monkeypatch.setattr(versions, 'is_current', without_large_hash)


@pytest.mark.parametrize('held', [False, True])
def test_recover_finishes_proven_legacy_version_tail_without_execution(
        make_project, stub_heavy_steps, monkeypatch, no_publication, held):
    uid = legacy_completed_tail(make_project, stub_heavy_steps, held=held)
    project = crud.get_project(uid)
    job_id = crud.latest_job(project.id, 'assemble').id
    artifact_hashes = {a.id: a.sha256 for a in crud.latest_artifacts(project.id, ['assemble']).values()}
    forbid_restart_execution(monkeypatch)
    runner.recover()
    params = crud.project_params(crud.get_project(uid))
    assert '_chain' not in params and '_chain_resume_plan' not in params
    assert params['_chain_resume_receipts'] == {'accepted': {'result': '保留'}}
    assert crud.latest_job(project.id, 'assemble').id == job_id
    assert {a.id: a.sha256 for a in crud.latest_artifacts(project.id, ['assemble']).values()} == artifact_hashes


@pytest.mark.parametrize('problem', ['cancelled', 'failed_hold', 'missing_source', 'changed_settings',
                                    'missing_file', 'changed_size', 'later_failed_job', 'bad_selection',
                                    'bad_time', 'different_versions', 'different_plan', 'hold_list', 'hold_string'])
def test_recover_keeps_unproven_or_user_stopped_version_plan(
        make_project, stub_heavy_steps, monkeypatch, pdir, no_publication, problem):
    uid = legacy_completed_tail(make_project, stub_heavy_steps)
    project = crud.get_project(uid)
    if problem in {'hold_list', 'hold_string'}:
        save_params(uid, _chain_state='held',
                    _chain_hold=['旧损坏记录'] if problem == 'hold_list' else '旧损坏记录')
    elif problem in {'cancelled', 'failed_hold'}:
        save_params(uid, _chain_state='held', _chain_hold={'step': 'assemble',
                    'code': 'STEP_CANCELLED' if problem == 'cancelled' else 'STEP_FAILED', 'message': '原始原因'})
    elif problem == 'missing_source':
        latest = crud.get_project(uid)
        params = crud.project_params(latest)
        params.pop('_chain_resume_plan')
        latest.params = json.dumps(params)
        crud.save_project(latest)
    elif problem == 'changed_settings':
        from dubforge import configuration
        current = configuration.resolve(uid)
        configuration.save('project:' + uid, dict(revision=current['revision'], updates={'assemble.bgm_volume': .25}))
    elif problem in {'missing_file', 'changed_size'}:
        path = pdir(uid) / 'artifacts' / 'assemble' / 'final_version_default.mp4'
        if problem == 'missing_file':
            path.unlink()
        else:
            path.write_bytes(path.read_bytes() + b'changed')
    elif problem == 'later_failed_job':
        job = crud.add_job(project.id, 'assemble', 'isolated.log', task_data={'scope': 'versions'})
        job.status = 'failed'
        crud.update_job(job)
    elif problem in {'bad_selection', 'bad_time'}:
        step = crud.get_step(project.id, 'assemble')
        snapshot = json.loads(step.input_snapshot)
        if problem == 'bad_selection':
            snapshot['selection'] = {'version_ids': ['other']}
        else:
            snapshot['captured_at'] = '2020-01-01T00:00:00+00:00'
        step.input_snapshot = json.dumps(snapshot)
        crud.save_step(step)
    elif problem == 'different_versions':
        from test_configuration import save
        definitions = versions.catalog(uid)['versions']
        definitions[0]['name'] = '后来确认的版本名称'
        save('project:' + uid, {'assembly.versions': definitions})
    else:
        save_params(uid, _chain_resume_plan={'version_definitions_digest': 'different-plan'})
    forbid_restart_execution(monkeypatch)
    runner.recover()
    runner.recover()
    params = crud.project_params(crud.get_project(uid))
    assert params['_chain'] == ['assemble'] and params['_chain_state'] == 'held'
    if problem in {'cancelled', 'failed_hold'}:
        assert params['_chain_hold']['message'] == '原始原因'
        assert params['_chain_hold']['code'] == ('STEP_CANCELLED' if problem == 'cancelled' else 'STEP_FAILED')
    elif problem in {'hold_list', 'hold_string'}:
        assert params['_chain_hold']['code'] == 'CHAIN_INVALID'


def test_recover_finishes_matching_owned_task_without_legacy_resume_proof(
        make_project, stub_heavy_steps, monkeypatch, no_publication):
    stub_heavy_steps('assemble')
    uid = prepared(make_project)
    save_params(uid, _chain=['assemble'], _chain_state='active')
    with monkeypatch.context() as context:
        context.setattr(runner, 'continue_chain', lambda *args: None)
        runner.start_step(uid, 'assemble')
        wait_status(uid, 'assemble', {'done'})
    assert crud.project_params(crud.get_project(uid))['_chain_job_id']
    forbid_restart_execution(monkeypatch)
    runner.recover()
    assert '_chain' not in crud.project_params(crud.get_project(uid))


@pytest.mark.parametrize('problem', ['', 'missing_source', 'different_source', 'mute_without_record', 'review_error'])
def test_real_renderer_mixed_source_and_rendered_legacy_recovery(
        make_project, monkeypatch, no_publication, problem):
    """真实版本准备/登记保留原片直用语义；只替换探测和编码，不使用外部服务。"""
    from sqlmodel import select
    from test_configuration import save
    from dubforge.pipeline import content_redaction, step_assemble
    from dubforge.pipeline.assembly_models import AssemblyResult

    uid = prepared(make_project)
    definitions = [
        dict(id='rendered', name='原声字幕版', dubbing=False, burn_subtitles=True,
             include_comments=False, overrides={}),
        dict(id='source', name='原片直用', dubbing=False, burn_subtitles=False,
             include_comments=False, overrides={}),
    ]
    save('project:' + uid, {'assembly.versions': definitions,
                           'content_review.blocked_ranges': '0.2-0.4:隔离静音验证' if problem == 'mute_without_record' else ''})
    monkeypatch.setattr(step_assemble, '_ffprobe_duration', lambda _: 1)
    monkeypatch.setattr(step_assemble, '_mux_original_sub',
                        lambda _ctx, _video, output, *args, **kwargs: output.write_bytes(b'isolated rendered video'))
    runner.start_step(uid, 'assemble')
    wait_status(uid, 'assemble', {'done'})
    assert versions.results(uid).get('rendered')
    # 只测编排的旧编码替身没有音频报告，不能误继承别的版本或阻止登记。
    assert versions.view(uid)['versions'][0]['audio_report'] is None
    if problem == 'mute_without_record':
        # 需静音时渲染器实际生成独立成品；缺失其登记不能退回直用原片。
        assert versions.results(uid).get('source')
        with crud.session() as session:
            record = session.exec(select(AssemblyResult).where(
                AssemblyResult.project_uid == uid, AssemblyResult.version_id == 'source')).one()
            session.delete(record)
            session.commit()
    else:
        assert versions.results(uid).get('source') is None
    save_params(uid, _chain=['assemble'], _chain_state='active',
                _chain_resume_plan={'version_definitions_digest': versions.digest(definitions)})
    project = crud.get_project(uid)
    source_artifact = crud.latest_artifacts(project.id, ['download'])['download/video.mp4']
    source_path = crud.project_dir(uid) / source_artifact.rel_path
    if problem == 'missing_source':
        source_path.unlink()
    elif problem == 'different_source':
        changed = source_path.with_name('another-video.mp4')
        changed.write_bytes(source_path.read_bytes())
        project.source_kind, project.source_url = 'local', str(changed)
        crud.save_project(project)
    elif problem == 'review_error':
        def invalid_review(_ctx):
            raise content_redaction.ContentRedactionError('隔离损坏审查规则')
        monkeypatch.setattr(content_redaction, 'plan', invalid_review)
    job_id = crud.latest_job(project.id, 'assemble').id
    forbid_restart_execution(monkeypatch)
    runner.recover()
    params = crud.project_params(crud.get_project(uid))
    if problem:
        assert params['_chain'] == ['assemble'] and params['_chain_state'] == 'held'
    else:
        assert '_chain' not in params
    assert crud.latest_job(project.id, 'assemble').id == job_id
