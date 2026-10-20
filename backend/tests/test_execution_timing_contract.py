"""同一任务的诊断累计计时不因轮询入口、资源准入或上下文移除改变。"""
import datetime
import json
import threading
from types import SimpleNamespace

from fastapi import Response

from dubforge.domain import crud
from dubforge.execution_progress import Recorder
from dubforge.runner import StepContext, runner


def _fixture(make_project, monkeypatch):
    import dubforge.execution_progress as execution_module
    import dubforge.runner as runner_module
    uid = make_project()
    project = crud.get_project(uid)
    root = crud.project_dir(uid)
    (root / 'logs').mkdir(exist_ok=True)
    clock = [1000.]
    monkeypatch.setattr(execution_module.time, 'time', lambda: clock[0])
    monkeypatch.setattr(runner_module.time, 'monotonic', lambda: clock[0])
    job = crud.add_job(project.id, 'tts', str(root / 'logs' / 'tts.log'))
    job.started_at = datetime.datetime.fromtimestamp(clock[0], datetime.timezone.utc)
    crud.update_job(job)
    pause = threading.Event()
    ctx = StepContext(project, 'tts', root, root / 'logs' / 'tts.log', threading.Event(), pause,
                      job_id=job.id, defer_snapshot=True)
    ctx._job = job
    ctx.execution = Recorder(ctx._persist_execution, clock=lambda: clock[0], owner='step', job_id=job.id)
    ctx.report_execution('queue', '等待执行资源', state='queued', resource='gpu')
    step = crud.get_step(project.id, 'tts')
    step.status = 'queued'
    crud.save_step(step)
    key = (project.id, 'tts')
    monkeypatch.setitem(runner._ctxs, key, ctx)
    monkeypatch.setitem(runner._current_jobs, key, job)
    monkeypatch.setitem(runner._pauses, key, pause)
    return uid, project, step, job, ctx, pause, clock


def _views(uid):
    from app.api.projects import list_projects, project_execution_snapshot
    from app.api.steps import list_steps
    project = crud.get_project(uid)
    direct = runner.execution_details(project.id, crud.get_steps(project.id))['tts']
    lightweight = next(row for row in project_execution_snapshot(Response(), uid)['projects'][0]['steps'] if row['key'] == 'tts')
    steps = next(row for row in list_steps(uid, Response()) if row['key'] == 'tts')
    listed = next(row for row in list_projects() if row['uid'] == uid)
    card = next(row for row in listed['steps'] if row['key'] == 'tts')
    assert direct == lightweight['execution'] == steps['execution'] == card['execution']
    return direct, (lightweight, steps, card)


def test_queue_running_cooperative_pause_resume_and_terminal_have_one_execution_clock(make_project, monkeypatch):
    uid, project, step, job, ctx, pause, clock = _fixture(make_project, monkeypatch)
    clock[0] += 2040  # 确认排队34分钟，不运行真实模型或调度线程。
    queued, _ = _views(uid)
    assert queued['elapsed_seconds'] == 2040
    runner._ctxs.pop((project.id, 'tts'))
    queued_without_ctx, _ = _views(uid)
    assert queued_without_ctx == queued
    runner._ctxs[(project.id, 'tts')] = ctx
    runner._begin_running(ctx, step, SimpleNamespace(label='字幕与配音'), 'isolated-input')
    clock[0] += 107
    ctx.report_execution('tts.synthesize', '生成配音', completed=1, total=10, unit='句')
    running, rows = _views(uid)
    assert running['elapsed_seconds'] == 2147
    assert running['started_at'] == queued['started_at']
    assert all(row['elapsed_seconds'] == 107 for row in rows), '顶层步骤耗时仍排除排队'
    assert any(row['phase'] == 'queue' and row['finished_at'] for row in running['history'])
    runner._ctxs.pop((project.id, 'tts'))
    running_without_ctx, _ = _views(uid)
    assert running_without_ctx == running, '同一运行任务从持久化快照读取也必须保留累计口径'
    runner._ctxs[(project.id, 'tts')] = ctx

    observed = []
    is_set = pause.is_set
    def during_pause():
        # 实际调用 check_pause 的暂停点；它只维护活动时钟，不写虚构的Recorder阶段。
        if ctx._pause_started is not None and not observed:
            observed.append(True)
            clock[0] += 300
            value, paused_rows = _views(uid)
            assert value['state'] == 'running'
            assert value['elapsed_seconds'] == 2447
            assert all(row['elapsed_seconds'] == 107 for row in paused_rows)
            pause.clear()
        return is_set()
    monkeypatch.setattr(pause, 'is_set', during_pause)
    pause.set(); ctx.check_pause()
    assert observed and ctx.active_seconds() == 107
    clock[0] += 10
    ctx.report_execution('tts.synthesize', '生成配音', completed=2, total=10, unit='句')
    resumed, rows = _views(uid)
    assert resumed['elapsed_seconds'] == 2457
    assert all(row['elapsed_seconds'] == 117 for row in rows), '恢复后活动耗时也不计入暂停等待'

    clock[0] += 10
    ctx.execution.finish('completed')
    step.status, job.status = 'done', 'done'
    step.finished_at = job.finished_at = datetime.datetime.fromtimestamp(clock[0], datetime.timezone.utc)
    crud.finish_job(job, step)
    with_ctx, _ = _views(uid)
    assert with_ctx['elapsed_seconds'] == 2467
    runner._ctxs.pop((project.id, 'tts'))
    clock[0] += 10000
    without_ctx, _ = _views(uid)
    assert without_ctx == with_ctx, '上下文收尾移除不能把活动耗时突然改回累计，也不能继续增加终态时间'
    assert json.loads(crud.latest_job(project.id, 'tts').execution)['elapsed_seconds'] == 2467


def test_explicit_paused_recorder_freezes_in_all_projections_without_redefining_cooperative_pause(make_project, monkeypatch):
    uid, project, step, job, ctx, _pause, clock = _fixture(make_project, monkeypatch)
    runner._begin_running(ctx, step, SimpleNamespace(label='字幕与配音'), 'isolated-input')
    clock[0] += 12
    ctx.execution.phase('diagnostic.pause', '已明确暂停的诊断记录', state='paused')
    before, _ = _views(uid)
    clock[0] += 300
    after, _ = _views(uid)
    assert before['state'] == after['state'] == 'paused'
    assert before['elapsed_seconds'] == after['elapsed_seconds'] == 12
    runner._ctxs.pop((project.id, 'tts'))
    stored, _ = _views(uid)
    assert stored == after


def test_newer_job_never_uses_elapsed_from_discarded_old_context(make_project, monkeypatch):
    uid, project, step, _job, ctx, _pause, clock = _fixture(make_project, monkeypatch)
    clock[0] += 2000
    step.status = 'running'; crud.save_step(step)
    newer = crud.add_job(project.id, 'tts', '')
    newer.started_at = datetime.datetime.fromtimestamp(clock[0], datetime.timezone.utc)
    newer.execution = json.dumps(Recorder(clock=lambda: clock[0], owner='step', job_id=newer.id).phase('new', '新任务'))
    crud.update_job(newer); crud.update_job(newer, execution_only=True)
    clock[0] += 7
    value = runner.execution_details(project.id, [step])['tts']
    assert ctx.active_seconds() == 2007
    assert value['job_id'] == newer.id and value['elapsed_seconds'] == 7
