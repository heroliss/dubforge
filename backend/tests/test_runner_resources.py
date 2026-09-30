"""链内失败自动重试、资源组互斥、项目视图工作流状态 (ADR-0076)。"""
import json
import threading
import time

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from conftest import mark_upstream_done, wait_status
from dubforge import config
from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.runner import runner
from dubforge.workflow_models import WorkflowRun


def _jobs(uid: str, step_key: str) -> int:
    project = crud.get_project(uid)
    with Session(get_engine()) as s:
        from dubforge.domain.models import Job
        return len(s.exec(select(Job).where(
            Job.project_id == project.id, Job.step_key == step_key)).all())


def _wait_retry_scheduled(uid: str, step_key: str, timeout: float = 15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        step = crud.get_step(crud.get_project(uid).id, step_key)
        if step.status == 'failed' and '自动重试' in (step.error_suggestion or ''):
            return
        time.sleep(0.02)
    raise TimeoutError('重试未排定')


def _set_params(uid: str, params: dict):
    """合并式更新: 保留 _chain 等运行键, 只改指定字段 (None = 删除)。"""
    project = crud.get_project(uid)
    merged = {**crud.project_params(project), **params}
    merged = {k: v for k, v in merged.items() if v is not None}
    project.params = json.dumps(merged, ensure_ascii=False)
    crud.save_project(project)


def _wait_done(uid: str, step_key: str, timeout: float = 20.0):
    """重试等待期步骤合法地停留在 failed; 只等最终 done, 不把 failed 视为终态。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if crud.get_step(crud.get_project(uid).id, step_key).status == 'done':
            return
        time.sleep(0.05)
    raise TimeoutError(f'{step_key} 未在 {timeout}s 内完成')


def _run_chain(client, uid: str, steps: list):
    r = client.post(f"/api/projects/{uid}/run_chain", json={"steps": steps})
    assert r.status_code == 200, r.json()


def test_chain_failure_auto_retries_then_continues(isolated_env, stub_heavy_steps, make_project):
    """链内失败 → 延迟自动重试; 重试期间修好诱因 → 成功并继续剩余计划。"""
    stub_heavy_steps('split', 'translate')
    config.update_config({'runner.step_retries': {'translate': 1}, 'runner.retry_delay': 1})
    uid = make_project(params={'fail_step': 'translate'})
    mark_upstream_done(uid, 'split')
    with TestClient(__import__('app.main', fromlist=['app']).app) as client:
        _run_chain(client, uid, ['split', 'translate'])
        _wait_retry_scheduled(uid, 'translate')
        assert _jobs(uid, 'translate') == 1
        _set_params(uid, {'fail_step': None})   # 排除失败诱因 (桩实时读项目参数)
        _wait_done(uid, 'translate')
        assert _jobs(uid, 'translate') == 2
        deadline = time.time() + 5
        while time.time() < deadline and crud.project_params(crud.get_project(uid)).get('_chain'):
            time.sleep(0.05)
        assert not crud.project_params(crud.get_project(uid)).get('_chain')


def test_retry_budget_exhausted_holds_chain(isolated_env, stub_heavy_steps, make_project):
    """重试额度用尽 → 计划挂起 (保留人工处理入口), 不无限重试。"""
    stub_heavy_steps('split', 'translate')
    config.update_config({'runner.step_retries': {'translate': 1}, 'runner.retry_delay': 1})
    uid = make_project(params={'fail_step': 'translate'})
    mark_upstream_done(uid, 'split')
    with TestClient(__import__('app.main', fromlist=['app']).app) as client:
        _run_chain(client, uid, ['split', 'translate'])
        _wait_retry_scheduled(uid, 'translate')
        deadline = time.time() + 15
        while time.time() < deadline:
            params = crud.project_params(crud.get_project(uid))
            if params.get('_chain_state') == 'held':
                break
            time.sleep(0.05)
        else:
            raise TimeoutError('额度用尽后未挂起')
        assert _jobs(uid, 'translate') == 2
        assert crud.project_params(crud.get_project(uid))['_chain_hold']['step'] == 'translate'


def test_cancel_pending_retry_keeps_failure(isolated_env, stub_heavy_steps, make_project):
    """等待自动重试期间停止 → 撤销重试计划, 步骤保持失败, 不再自动启动。"""
    stub_heavy_steps('split', 'translate')
    config.update_config({'runner.step_retries': {'translate': 1}, 'runner.retry_delay': 30})
    uid = make_project(params={'fail_step': 'translate'})
    mark_upstream_done(uid, 'split')
    with TestClient(__import__('app.main', fromlist=['app']).app) as client:
        _run_chain(client, uid, ['split', 'translate'])
        _wait_retry_scheduled(uid, 'translate')
        result = runner.cancel_step(uid, 'translate')
        assert result['status'] == 'retry_cancelled'
        time.sleep(1.5)
        assert _jobs(uid, 'translate') == 1
        assert crud.get_step(crud.get_project(uid).id, 'translate').status == 'failed'


def test_gpu_group_serializes_cross_project_steps(isolated_env, monkeypatch, make_project):
    """分属不同项目的 preprocess/transcribe 同占 gpu 组: 同时只能执行一个。"""
    from dubforge.pipeline import steps as steps_mod
    state = {'active': 0, 'max': 0}
    lock = threading.Lock()

    def make(key):
        def run(ctx):
            with lock:
                state['active'] += 1
                state['max'] = max(state['max'], state['active'])
            time.sleep(0.4)
            with lock:
                state['active'] -= 1
            ctx.write_json(f'{key}_result.json', {'step': key})
        return run
    monkeypatch.setitem(steps_mod._RUNNERS, 'preprocess', make('preprocess'))
    monkeypatch.setitem(steps_mod._RUNNERS, 'transcribe', make('transcribe'))
    a, b = make_project(), make_project()
    mark_upstream_done(a, 'preprocess')
    mark_upstream_done(b, 'transcribe')
    assert runner.start_step(a, 'preprocess')['status'] == 'started'
    assert runner.start_step(b, 'transcribe')['status'] == 'started'
    wait_status(a, 'preprocess', {'done'})
    wait_status(b, 'transcribe', {'done'})
    assert state['max'] == 1


def test_project_view_includes_workflow_status(isolated_env, make_project):
    uid = make_project()
    # TestClient 启动会 recover(): running 判暂停, 用非忙碌状态断言透传。
    with Session(get_engine()) as session:
        session.add(WorkflowRun(project_uid=uid, request_id='req-' + uid,
                                status='failed', recipe='{}', plan='{}'))
        session.commit()
    with TestClient(__import__('app.main', fromlist=['app']).app) as client:
        assert client.get(f'/api/projects/{uid}').json()['workflow_status'] == 'failed'
        listing = client.get('/api/projects').json()
        assert next(p for p in listing if p['uid'] == uid)['workflow_status'] == 'failed'


def test_download_concurrency_two_with_success_handoff(isolated_env, monkeypatch, make_project):
    """download 并行上限=2: 第三项目的下载在槽位因「成功完成」交接后立即运行,
    不需要等其他项目失败; 峰值并发恒为 2。"""
    from dubforge.pipeline import steps as steps_mod
    state = {'active': 0, 'max': 0}
    lock = threading.Lock()

    def run(ctx):
        with lock:
            state['active'] += 1
            state['max'] = max(state['max'], state['active'])
        time.sleep(0.3)
        with lock:
            state['active'] -= 1
        ctx.write_json('download_result.json', {'step': 'download'})
    monkeypatch.setitem(steps_mod._RUNNERS, 'download', run)
    uids = [make_project() for _ in range(3)]
    for uid in uids:
        assert runner.start_step(uid, 'download')['status'] == 'started'
    deadline = time.time() + 5
    while time.time() < deadline and state['active'] < 2:
        time.sleep(0.02)
    assert state['active'] == 2                     # 两个并行, 第三个在等槽位
    for uid in uids:
        wait_status(uid, 'download', {'done'})
    assert state['max'] == 2                        # 全程峰值不超过 2
    assert all(crud.get_step(crud.get_project(u).id, 'download').status == 'done' for u in uids)


def test_demucs_oom_falls_back_to_cpu(isolated_env, make_project, monkeypatch):
    """显存不足自动降级 (模块承诺): run() 捕获 CUDA OOM → 归还缓存 → CPU 重跑;
    非 OOM 异常照常穿透。"""
    from dubforge.pipeline import step_preprocess
    attempts = []
    def _touch_out(cmd):
        # ffmpeg 抽轨桩: 模拟产出 raw.wav (命令最后一个是输出路径)。
        from pathlib import Path as _P
        dst = _P(cmd[-1])
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b'raw')
        return 0
    def fake_demucs(ctx, src, out, *, device=None):
        attempts.append(device or 'cuda')
        if attempts[-1] == 'cuda':
            raise RuntimeError('CUDA error: out of memory')
        out.mkdir(parents=True, exist_ok=True)
        (out / 'vocal.wav').write_bytes(b'v')
        (out / 'background.wav').write_bytes(b'b')
        return out / 'vocal.wav', out / 'background.wav'
    monkeypatch.setattr(step_preprocess, '_run_demucs', fake_demucs)
    monkeypatch.setattr('dubforge.utils.gpu_memory.release_unused', lambda log=None: None)
    # 最小 ctx 桩: 只需要 log 与 run() 用到的路径/上游。
    import types
    from pathlib import Path
    uid = make_project()
    project = crud.get_project(uid)
    root = crud.project_dir(uid)
    (root / 'media').mkdir(parents=True, exist_ok=True)
    video = root / 'media' / 'video.mp4'
    video.write_bytes(b'x')
    from dubforge.domain import crud as _crud
    _crud.add_artifact(project.id, 'download', 'media', 'video.mp4', video, 'media/video.mp4')
    ctx = types.SimpleNamespace(
        project=project, pdir=root,
        log=lambda m: None,
        run_subprocess=lambda cmd, **k: (_touch_out(cmd), '', '')[0:3],
        report_progress=lambda pct: None,
        check_pause=lambda: None,
        check_cancel=lambda: None,
        upstream=lambda step, name: root / 'media' / 'video.mp4',
        write_json=lambda name, data: None,
        register_file=lambda *a, **k: None,
        cfg=lambda key, default=None: {'separate.model': 'htdemucs'}.get(key, default),
    )
    step_preprocess.run(ctx)
    assert attempts == ['cuda', 'cpu']
    assert (root / 'audio' / 'vocal.wav').exists()

    # 非 OOM 异常穿透, 不降级 (新项目, 避免首轮登记的产物导致跳过)。
    uid2 = make_project()
    project2 = crud.get_project(uid2)
    root2 = crud.project_dir(uid2)
    (root2 / 'media').mkdir(parents=True, exist_ok=True)
    video2 = root2 / 'media' / 'video.mp4'
    video2.write_bytes(b'x')
    _crud.add_artifact(project2.id, 'download', 'media', 'video.mp4', video2, 'media/video.mp4')
    attempts.clear()
    def boom(ctx, src, out, *, device=None):
        raise ValueError('坏音频')
    monkeypatch.setattr(step_preprocess, '_run_demucs', boom)
    import pytest as _pytest
    with _pytest.raises(ValueError):
        step_preprocess.run(types.SimpleNamespace(
            project=project2, pdir=root2,
            log=lambda m: None,
            run_subprocess=lambda cmd, **k: (_touch_out(cmd), '', '')[0:3],
            report_progress=lambda pct: None,
            check_pause=lambda: None,
            check_cancel=lambda: None,
            upstream=lambda step, name: video2,
            write_json=lambda name, data: None,
            register_file=lambda *a, **k: None,
            cfg=lambda key, default=None: {'separate.model': 'htdemucs'}.get(key, default),
        ))
    assert attempts == []
