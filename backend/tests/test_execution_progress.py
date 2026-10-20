"""执行过程诊断、收尾边界与资源协调；不调用模型、外部服务或真实媒体。"""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from dubforge import config
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.execution_progress import HISTORY_LIMIT, Recorder, normalize
from dubforge.runner import runner


def test_stage_changes_flush_but_streaming_updates_are_bounded_and_body_is_not_stored():
    clock = [1000.]
    rows = []
    recorder = Recorder(rows.append, clock=lambda: clock[0], owner='step', job_id=12)
    recorder.llm_trace('request', '秘密提示词', request_id='one', profile='模型组')
    recorder.llm_trace('request', '第二条消息', request_id='one')
    for _ in range(100):
        clock[0] += .002
        recorder.llm_trace('reasoning_delta', '内部思考内容')
    assert len(rows) == 2
    assert recorder.snapshot()['metrics']['request'] == 1
    assert recorder.snapshot()['metrics']['reasoning_chars'] == 600
    assert '秘密提示词' not in json.dumps(recorder.snapshot(), ensure_ascii=False)
    assert '内部思考内容' not in json.dumps(recorder.snapshot(), ensure_ascii=False)
    assert recorder.snapshot()['job_id'] == 12


def test_model_microstages_keep_the_current_verified_work_scope():
    recorder = Recorder()
    recorder.phase('chunk', '翻译第2块', completed=100,total=500,unit='行',metrics={'batch_index':2,'batch_total':5})
    for event, content in [('request',''),('reasoning_delta','内部思考'),('assistant_delta','候选'),('validation_success','')]:
        value = recorder.llm_trace(event,content,request_id='chunk-2',retry_attempt=1,retry_total=3)
        assert (value['completed'],value['total'],value['unit']) == (100,500,'行')
        assert value['metrics']['batch_index'] == 2 and value['max_attempts'] == 3


def test_history_is_bounded_and_terminal_elapsed_time_does_not_keep_growing():
    clock = [0.]
    recorder = Recorder(clock=lambda: clock[0])
    for index in range(50):
        recorder.phase(str(index), f'阶段{index}', completed=index, total=50, unit='句')
        clock[0] += 1
    recorder.finish()
    assert len(recorder.snapshot()['history']) == HISTORY_LIMIT
    assert recorder.snapshot()['elapsed_seconds'] == 50
    clock[0] = 500
    assert recorder.snapshot()['elapsed_seconds'] == 50
    assert recorder.snapshot()['state'] == 'completed'


def test_parent_failure_overrides_old_running_phase_and_invalid_shape_is_safe():
    value = normalize({'state': [], 'history': [None, {'state': []}], 'metrics': {'prompt': 'secret'}},
                      state='interrupted', started_at=10, finished_at=20, now=300)
    assert value['state'] == 'interrupted'
    assert value['elapsed_seconds'] == 10
    assert value['metrics'] == {}
    assert len(value['history']) == 1
    malformed = normalize({'updated_at': 1e100, 'started_at': 1e100, 'completed': 10 ** 1000})
    assert malformed['updated_at'] is None and malformed['completed'] is None


def test_resume_same_phase_keeps_the_closed_attempt_in_history():
    clock = [100.]
    recorder = Recorder(clock=lambda: clock[0])
    recorder.phase('waiting', '等待平台确认', state='waiting')
    clock[0] = 110.
    recorder.finish('paused')
    clock[0] = 120.
    value = recorder.phase('waiting', '等待平台确认', state='waiting')
    assert len(value['history']) == 2
    assert value['history'][0]['state'] == 'paused' and value['history'][0]['finished_at']
    assert value['history'][1]['state'] == 'waiting' and value['history'][1]['finished_at'] is None


@pytest.mark.parametrize('state', ['failed', 'paused', 'interrupted', 'completed'])
def test_explicit_new_attempt_resets_elapsed_but_preserves_previous_attempt_history(state):
    clock = [100.]
    original = Recorder(clock=lambda: clock[0], owner='chapters', run_id='same-task')
    original.phase('chapters.adapt', '整理章节')
    clock[0] = 110.
    previous = original.finish(state, detail='上一次执行记录')
    clock[0] = 20000.
    continued = Recorder(previous=previous, restart=True, clock=lambda: clock[0],
                         owner='chapters', run_id='same-task')
    accepted = continued.phase('chapters.queued', '等待继续章节', state='queued')
    assert accepted['elapsed_seconds'] == 0
    assert accepted['history'][0] == previous['history'][0]
    assert accepted['history'][0]['state'] == state
    assert accepted['run_id'] == previous['run_id']
    clock[0] += 3
    running = continued.phase('chapters.adapt', '整理章节')
    assert running['elapsed_seconds'] == 3
    assert running['finished_at'] is None
    assert previous['elapsed_seconds'] == 10


def test_recorder_continuation_keeps_existing_clock_unless_restart_is_explicit():
    previous = dict(state='running', phase='one', started_at=100., updated_at=105.)
    continued = Recorder(previous=previous, clock=lambda: 110.)
    assert continued.phase('two', '下一阶段')['elapsed_seconds'] == 10


@pytest.mark.parametrize('state', ['paused', 'failed', 'unknown', 'running'])
def test_restart_closes_unfinished_history_at_last_activity_without_claiming_success(state):
    original = Recorder(clock=lambda: 100.)
    original.phase('production.tts', '配音中', completed=69, total=305, unit='句')
    previous = normalize(original.snapshot(), state=state, now=20000.)
    accepted = Recorder(previous=previous, restart=True, clock=lambda: 20000.).phase(
        'production.tts', '等待继续配音', state='queued')
    old, current = accepted['history']
    assert old['state'] == ('interrupted' if state == 'running' else state)
    assert old['finished_at'] == old['started_at']
    assert previous['history'][0]['state'] == 'running'
    assert previous['history'][0]['finished_at'] is None, '不能修改调用方持有的旧快照'
    assert current['phase'] == old['phase'] and current['state'] == 'queued'
    assert current['finished_at'] is None
    assert accepted['elapsed_seconds'] == 0
    assert accepted['completed'] is None and accepted['total'] is None and accepted['metrics'] == {}


@pytest.mark.parametrize('start,updated,restarted,expected', [
    (100., 110., 20000., 110.),
    (100., 30000., 20000., 20000.),
    (100., 90., 20000., 100.),
    (100., 110., 90., 100.),
    (100., None, 20000., 100.),
    (None, None, 20000., 20000.),
])
def test_restart_closing_time_is_bounded_and_missing_or_reversed_clocks_are_safe(start, updated, restarted, expected):
    previous = dict(state='paused', phase='production.tts', started_at=start, updated_at=updated,
        history=[dict(phase='production.tts', state='running', started_at=start, finished_at=None)])
    accepted = Recorder(previous=previous, restart=True, clock=lambda: restarted).phase(
        'workflow.queued', '等待执行', state='queued')
    old = accepted['history'][0]
    assert old['state'] == 'paused'
    assert old['finished_at'] == normalize(dict(started_at=expected), now=expected)['started_at']
    assert accepted['elapsed_seconds'] == 0


def test_pending_retry_overrides_previous_failure_without_frozen_old_finish_time():
    source = {'state':'failed','started_at':10,'updated_at':20,'finished_at':20,'label':'等待自动重试'}
    early = normalize(source,state='retrying',finished_at=20,now=30)
    late = normalize(source,state='retrying',finished_at=20,now=40)
    assert early['state'] == 'retrying' and early['finished_at'] is None
    assert late['elapsed_seconds'] > early['elapsed_seconds']
    assert normalize(source,state='waiting',now=40)['state'] == 'waiting'


def test_diagnostic_failure_does_not_fail_the_underlying_operation():
    recorder = Recorder(lambda _: (_ for _ in ()).throw(OSError('磁盘繁忙')))
    assert recorder.phase('save', '保存结果')['phase'] == 'save'
    assert recorder.finish()['state'] == 'completed'


def test_slow_diagnostic_writer_does_not_block_status_reads():
    entered, release = threading.Event(), threading.Event()
    def write(_):
        entered.set()
        assert release.wait(3)
    recorder = Recorder(write)
    worker = threading.Thread(target=lambda: recorder.llm_trace('request', request_id='slow'))
    worker.start()
    try:
        assert entered.wait(1)
        read = threading.Thread(target=recorder.snapshot)
        read.start(); read.join(1)
        assert not read.is_alive(), '诊断落盘不能阻塞页面读取当前状态'
    finally:
        release.set(); worker.join(3)


def test_stable_metrics_whitelist_and_safe_text():
    recorder = Recorder()
    value = recorder.phase('request', '等待', detail='Bearer secret-value',
                           metrics={'api_key': 'secret', 'profile': 'a', 'completed': float('nan')})
    assert value['metrics'] == {'profile': 'a'}
    assert 'secret-value' not in value['detail']


def test_stream_description_and_raw_id_survive_projection_without_stale_labels():
    """同一下载阶段换流时显式清空说明，不能沿用上一条视频流的标签。"""
    recorder = Recorder()
    recorder.phase('downloading', '下载视频与音频', metrics={
        'current_format': '401', 'current_stream': '4K · 60 fps · AV1 视频流',
        'download_url': 'https://private.example/video?token=secret',
    })
    first = recorder.snapshot()['metrics']
    assert first['current_format'] == '401'
    assert first['current_stream'] == '4K · 60 fps · AV1 视频流'
    assert 'download_url' not in first
    recorder.update(metrics={'current_format': 'new-unknown', 'current_stream': None})
    assert recorder.snapshot()['metrics']['current_stream'] is None
    assert recorder.snapshot()['metrics']['current_format'] == 'new-unknown'


def test_long_attempt_does_not_reset_retry_budget(make_project, monkeypatch):
    uid = make_project(params={'_chain': ['translate'], '_chain_state': 'running'})
    project = crud.get_project(uid)
    config.update_config({'runner.step_retries': {'translate': 1}})
    monkeypatch.setitem(runner._retries, (project.id, 'translate'), (threading.Event(), 1, time.monotonic() - 601))
    monkeypatch.setattr(threading, 'Timer', lambda *args, **kwargs: pytest.fail('已用尽额度，不能排新重试'))
    assert runner._schedule_chain_retry(project, 'translate', DubForgeError('失败'), lambda _: None) is False


def test_tts_cold_start_uses_project_endpoint_without_prior_managed_process(monkeypatch):
    from dubforge import tts_service
    values = {'tts.backend': 'indextts25', 'tts.indextts25.url': 'http://127.0.0.1:7998'}
    ctx = SimpleNamespace(cfg=lambda key, default=None: values.get(key, default), log=lambda _: None,
                          check_cancel=lambda: None)
    calls, online = [], [False]
    def status(cfg_get):
        calls.append(('status', cfg_get('tts.indextts25.url')))
        return {'online': online[0], 'can_start': True, 'managed': False}
    def start(cfg_get):
        calls.append(('start', cfg_get('tts.indextts25.url')))
        online[0] = True
    monkeypatch.setattr(tts_service, 'status', status)
    monkeypatch.setattr(tts_service, 'start', start)
    runner._prepare_gpu(ctx, 'tts')
    assert calls == [('status', values['tts.indextts25.url']), ('start', values['tts.indextts25.url']),
                     ('status', values['tts.indextts25.url'])]


def test_success_is_exposed_only_after_cleanup_and_stage_history_survives(make_project, monkeypatch):
    from conftest import wait_status
    from dubforge.pipeline import steps
    import dubforge.runner as runner_module
    uid = make_project()
    entered, release = threading.Event(), threading.Event()
    def execute(ctx):
        ctx.report_execution('download', '下载源视频', completed=8, total=8, unit='字节')
        ctx.report_progress(99)
    original = runner_module._sweep_temp_files
    def cleanup(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setitem(steps._RUNNERS, 'download', execute)
    monkeypatch.setattr(runner_module, '_sweep_temp_files', cleanup)
    runner.start_step(uid, 'download')
    try:
        assert entered.wait(5)
        step = crud.get_step(crud.get_project(uid).id, 'download')
        assert step.status == 'running' and step.progress < 100
        assert runner.progress_eta(step.project_id, 'download')['execution']['label'] == '登记结果与清理临时文件'
    finally:
        release.set()
    wait_status(uid, 'download', {'done'})
    job = crud.latest_job(crud.get_project(uid).id, 'download')
    value = json.loads(job.execution)
    assert value['state'] == 'completed'
    assert any(row['phase'] == 'download' for row in value['history'])
    value = runner.execution_details(crud.get_project(uid).id, crud.get_steps(crud.get_project(uid).id))['download']
    assert value['job_id'] == job.id and value['state'] == 'completed'


def test_project_list_batches_latest_jobs_and_keeps_completed_diagnostics(make_project, monkeypatch):
    from app.api.projects import list_projects
    ids = [make_project(title=f'隔离项目{i}') for i in range(3)]
    newest = {}
    for uid in ids:
        project = crud.get_project(uid)
        crud.add_job(project.id, 'download', '')
        job = crud.add_job(project.id, 'download', '')
        job.execution = json.dumps(Recorder().phase('save', '保存源资料'))
        crud.update_job(job, execution_only=True)
        job.status = 'done'
        crud.update_job(job)
        step = crud.get_step(project.id, 'download')
        step.status = 'done'
        crud.save_step(step)
        newest[uid] = job.id
    calls = []
    original = runner.latest_execution_jobs
    def query(project_ids):
        values = list(project_ids)
        calls.append(values)
        return original(values)
    monkeypatch.setattr(runner, 'latest_execution_jobs', query)
    rows = list_projects()
    assert len(calls) == 1 and len(calls[0]) == 3
    for row in rows:
        value = next(s for s in row['steps'] if s['key'] == 'download')['execution']
        assert value['job_id'] == newest[row['uid']]
        assert value['state'] == 'completed' and value['label'] == '保存源资料'


def test_diagnostic_write_does_not_clobber_task_receipts_or_terminal_state(make_project):
    project = crud.get_project(make_project())
    job = crud.add_job(project.id, 'download', '', task_data={'version': 1})
    old = crud.latest_job(project.id, 'download')
    job.task_data, job.status = '{"version": 2}', 'done'
    crud.update_job(job)
    old.execution = json.dumps(Recorder().phase('cleanup', '清理'))
    crud.update_job(old, execution_only=True)
    saved = crud.latest_job(project.id, 'download')
    assert saved.task_data == '{"version": 2}' and saved.status == 'done'
    saved.error_detail = '诊断说明'
    crud.update_job(saved)
    assert json.loads(crud.latest_job(project.id, 'download').execution)['label'] == '清理'


@pytest.mark.parametrize('force', [True, False])
def test_stop_at_final_success_commit_cannot_split_step_and_job_state(make_project, monkeypatch, force):
    """完成提交与停止并发时只能有一个胜者，不能产出 done 步骤与 cancelled 任务。"""
    from concurrent.futures import ThreadPoolExecutor
    from dubforge.pipeline import steps
    uid = make_project()
    project = crud.get_project(uid)
    committing, release, attempted = threading.Event(), threading.Event(), threading.Event()
    original = crud.finish_job
    def save(job, step=None):
        if step is not None and step.project_id == project.id and step.step_key == 'download' and step.status == 'done':
            committing.set()
            assert release.wait(5)
        return original(job, step)
    monkeypatch.setattr(crud, 'finish_job', save)
    monkeypatch.setitem(steps._RUNNERS, 'download', lambda ctx: ctx.write_json('result.json', {'ok': True}))
    runner.start_step(uid, 'download')
    def stop():
        attempted.set()
        return (runner.force_stop if force else runner.cancel_step)(uid, 'download')
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = None
        try:
            assert committing.wait(5)
            request = pool.submit(stop)
            assert attempted.wait(3)
            # 旧实现未持启动锁：强停可先写 cancelled，再被旧 done 保存覆盖。
            # 新实现共锁时停止会等待；先放行提交，避免测试自身构造锁等待死锁。
            unlocked = runner._start_lock.acquire(blocking=False)
            if unlocked:
                runner._start_lock.release()
                request.result(timeout=3)
        finally:
            release.set()
        request.result(timeout=5)
    for future in list(runner._futures):
        future.result(timeout=5)
    step = crud.get_step(project.id, 'download')
    job = crud.latest_job(project.id, 'download')
    assert (step.status == 'done') == (job.status == 'done'), (step.status, job.status)
    if job.status != 'done':
        assert step.error_code in {'STEP_FORCE_STOPPED', 'STEP_CANCELLED'}


def test_cancel_during_cleanup_after_function_returns_prevents_success_commit(make_project, monkeypatch):
    """函数已产出完整结果，但收尾尚未提交；此时停止仍须冻结链并保留取消终态。"""
    import dubforge.runner as runner_module
    from dubforge.pipeline import steps
    uid = make_project()
    project = crud.get_project(uid)
    returned, cleaning, release = threading.Event(), threading.Event(), threading.Event()
    original = runner_module._sweep_temp_files
    def execute(ctx):
        ctx.write_json('result.json', {'ok': True})
        returned.set()
    def cleanup(*args, **kwargs):
        cleaning.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setitem(steps._RUNNERS, 'download', execute)
    monkeypatch.setattr(runner_module, '_sweep_temp_files', cleanup)
    runner.start_step(uid, 'download')
    try:
        assert returned.wait(3) and cleaning.wait(3)
        result = runner.cancel_step(uid, 'download')
        assert result['status'] == 'cancelling'
        assert crud.get_step(project.id, 'download').status == 'running'
    finally:
        release.set()
    for future in list(runner._futures):
        future.result(timeout=5)
    step, job = crud.get_step(project.id, 'download'), crud.latest_job(project.id, 'download')
    assert step.status == 'failed' and step.error_code == 'STEP_CANCELLED'
    assert job.status == 'cancelled'
    assert json.loads(job.execution)['state'] == 'cancelled'
    assert crud.latest_artifacts(project.id, ['download'])
