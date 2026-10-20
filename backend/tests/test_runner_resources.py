"""链内失败自动重试、资源组互斥、项目视图工作流状态 (ADR-0076)。"""
import json
import sys
import threading
import time
import types
from pathlib import Path

import pytest

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


def _preprocess_case(make_project):
    """真实隔离项目与步骤入口，只替换 FFmpeg 抽轨及产物登记边界。"""
    def _touch_out(cmd):
        dst = Path(cmd[-1])
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b'raw')
        return 0
    uid = make_project()
    project = crud.get_project(uid)
    root = crud.project_dir(uid)
    (root / 'media').mkdir(parents=True, exist_ok=True)
    video = root / 'media' / 'video.mp4'
    video.write_bytes(b'x')
    crud.add_artifact(project.id, 'download', 'media', 'video.mp4', video, 'media/video.mp4')
    saved, registered, logs = {}, [], []
    ctx = types.SimpleNamespace(
        project=project, pdir=root,
        log=logs.append,
        run_subprocess=lambda cmd, **k: (_touch_out(cmd), '', '')[0:3],
        report_progress=lambda pct: None,
        check_pause=lambda: None,
        check_cancel=lambda: None,
        upstream=lambda step, name: root / 'media' / 'video.mp4',
        write_json=lambda name, data: saved.update({name: data}),
        register_file=lambda name, *a, **k: registered.append(name),
        cfg=lambda key, default=None: {'separate.model': 'htdemucs'}.get(key, default),
    )
    return ctx, root, saved, registered, logs


def _demucs_hardware(monkeypatch, *, available):
    """显式模拟设备和 OOM 类型，不导入真实 Torch 或初始化 CUDA。"""
    class CudaOOM(RuntimeError):
        pass
    state = dict(available=available, probes=0, components=[])
    def is_available():
        state['probes'] += 1
        return state['available']
    torch = types.ModuleType('torch')
    torch.cuda = types.SimpleNamespace(is_available=is_available, OutOfMemoryError=CudaOOM)
    monkeypatch.setitem(sys.modules, 'torch', torch)
    monkeypatch.setattr('dubforge.environment.require_component', state['components'].append)
    return state, CudaOOM


@pytest.mark.parametrize('available_after_oom', [True, False])
@pytest.mark.parametrize('oom_kind', ['runtime_message', 'typed_oom'])
def test_demucs_oom_falls_back_to_cpu(isolated_env, make_project, monkeypatch, available_after_oom, oom_kind):
    """明确模拟 CUDA 首发；即使 OOM 后设备不可用，仍只归还缓存并回退 CPU 一次。"""
    from dubforge.pipeline import step_preprocess
    hardware, CudaOOM = _demucs_hardware(monkeypatch, available=True)
    events = []
    def fake_demucs(ctx, src, out, *, device=None):
        events.append(device)
        if device == 'cuda':
            hardware['available'] = available_after_oom
            raise (CudaOOM('显存分配失败') if oom_kind == 'typed_oom'
                   else RuntimeError('CUDA error: out of memory'))
        assert device == 'cpu'
        (out / 'vocal.wav').write_bytes(b'v')
        (out / 'background.wav').write_bytes(b'b')
        return out / 'vocal.wav', out / 'background.wav'
    monkeypatch.setattr(step_preprocess, '_run_demucs', fake_demucs)
    monkeypatch.setattr('dubforge.utils.gpu_memory.release_unused', lambda log=None: events.append('release'))
    ctx, root, saved, registered, logs = _preprocess_case(make_project)
    step_preprocess.run(ctx)
    assert events == ['cuda', 'release', 'cpu']
    assert hardware['probes'] == 1 and hardware['components'] == ['separation']
    assert registered == ['raw.wav', 'vocal.wav', 'background.wav']
    assert saved['separate.json']['separated'] is True
    assert (root / 'audio/vocal.wav').read_bytes() == b'v'
    assert (root / 'audio/background.wav').read_bytes() == b'b'
    assert not (root / 'audio/.tmp_src44100.wav').exists()
    assert any('自动降级为 CPU' in message for message in logs)


@pytest.mark.parametrize('available_after_error', [False, True])
def test_demucs_cpu_oom_does_not_retry(isolated_env, make_project, monkeypatch, available_after_error):
    """CPU 内存不足不能再次重试 CPU，即使报错后显卡变得可用。"""
    from dubforge.pipeline import step_preprocess
    hardware, _ = _demucs_hardware(monkeypatch, available=False)
    events = []
    error = RuntimeError('out of memory')
    def fail(ctx, src, out, *, device=None):
        events.append(device)
        hardware['available'] = available_after_error
        raise error
    monkeypatch.setattr(step_preprocess, '_run_demucs', fail)
    monkeypatch.setattr('dubforge.utils.gpu_memory.release_unused', lambda log=None: events.append('release'))
    ctx, root, saved, registered, _ = _preprocess_case(make_project)
    with pytest.raises(RuntimeError) as caught:
        step_preprocess.run(ctx)
    assert caught.value is error and events == ['cpu']
    assert hardware['probes'] == 1
    assert registered == ['raw.wav'] and saved == {}
    assert not (root / 'audio/.tmp_src44100.wav').exists()


@pytest.mark.parametrize('cuda_available', [True, False])
def test_demucs_non_oom_does_not_retry(isolated_env, make_project, monkeypatch, cuda_available):
    """非内存错误保留原始异常，在 CPU / CUDA 均不触发回退。"""
    from dubforge.pipeline import step_preprocess
    hardware, _ = _demucs_hardware(monkeypatch, available=cuda_available)
    events = []
    error = ValueError('坏音频')
    def fail(ctx, src, out, *, device=None):
        events.append(device)
        raise error
    monkeypatch.setattr(step_preprocess, '_run_demucs', fail)
    monkeypatch.setattr('dubforge.utils.gpu_memory.release_unused', lambda log=None: events.append('release'))
    ctx, root, saved, registered, _ = _preprocess_case(make_project)
    with pytest.raises(ValueError) as caught:
        step_preprocess.run(ctx)
    assert caught.value is error and events == ['cuda' if cuda_available else 'cpu']
    assert hardware['probes'] == 1
    assert registered == ['raw.wav'] and saved == {}
    assert not (root / 'audio/.tmp_src44100.wav').exists()


def test_demucs_missing_optional_component_precedes_torch_import(isolated_env, make_project, monkeypatch):
    from dubforge.errors import DubForgeError
    from dubforge.pipeline import step_preprocess
    monkeypatch.setitem(sys.modules, 'torch', None)
    error = DubForgeError('尚未安装人声分离组件', code='OPTIONAL_COMPONENT_MISSING')
    def missing(key):
        assert key == 'separation'
        raise error
    monkeypatch.setattr('dubforge.environment.require_component', missing)
    monkeypatch.setattr(step_preprocess, '_run_demucs', lambda *a, **k: pytest.fail('缺少组件时不得开始分离'))
    ctx, root, saved, registered, _ = _preprocess_case(make_project)
    with pytest.raises(DubForgeError) as caught:
        step_preprocess.run(ctx)
    assert caught.value is error and caught.value.code == 'OPTIONAL_COMPONENT_MISSING'
    assert registered == ['raw.wav'] and saved == {}
    assert not (root / 'audio/.tmp_src44100.wav').exists()


@pytest.mark.parametrize('mode', ['disabled', 'cached'])
def test_demucs_disabled_or_cached_does_not_probe_torch(isolated_env, make_project, monkeypatch, mode):
    """关闭分离或复用已有有效音轨时，不新增可选依赖检查及硬件探测。"""
    from dubforge.pipeline import step_preprocess
    monkeypatch.setitem(sys.modules, 'torch', None)
    def forbidden(*args, **kwargs):
        pytest.fail('关闭或复用分离时不得检查组件、加载模型或探测设备')
    monkeypatch.setattr('dubforge.environment.require_component', forbidden)
    monkeypatch.setattr(step_preprocess, '_run_demucs', forbidden)
    ctx, root, saved, registered, _ = _preprocess_case(make_project)
    if mode == 'disabled':
        ctx.cfg = lambda key, default=None: {'separate.enabled': False}.get(key, default)
    else:
        audio = root / 'audio'
        audio.mkdir(exist_ok=True)
        (audio / 'vocal.wav').write_bytes(b'old-vocal')
        (audio / 'background.wav').write_bytes(b'old-background')
    step_preprocess.run(ctx)
    assert saved['separate.json']['separated'] is (mode == 'cached')
    assert registered == (['raw.wav', 'vocal.wav', 'background.wav'] if mode == 'cached' else ['raw.wav'])
    if mode == 'cached':
        assert (root / 'audio/vocal.wav').read_bytes() == b'old-vocal'
        assert (root / 'audio/background.wav').read_bytes() == b'old-background'
    assert not (root / 'audio/.tmp_src44100.wav').exists()


def _fake_tts_service(monkeypatch, *, managed=True, online=True, record=None):
    from dubforge import tts_service
    state = {'stopped': 0, 'started': 0}
    def status(cfg_get=config.get, *, probe=True):
        return {'backend': 'indextts25', 'managed': managed, 'online': online,
                'status': 'online' if online else 'offline', 'message': '', 'can_stop': True}
    def stop(cfg_get=config.get):
        state['stopped'] += 1
        if record is not None:
            record.append('stop')
        return {'status': 'offline'}
    def start(cfg_get=config.get):
        state['started'] += 1
        if record is not None:
            record.append('start')
        monkeypatch.setattr(tts_service, 'status', lambda cfg_get=config.get, *, probe=True: status(probe=False) | {'online': True, 'status': 'online'})
        return {'status': 'online'}
    monkeypatch.setattr(tts_service, 'status', status)
    monkeypatch.setattr(tts_service, 'stop', stop)
    monkeypatch.setattr(tts_service, 'start', start)
    return state


def _stub_step(monkeypatch, make_project, key: str):
    """桩掉目标步骤并播种上游, 返回可直接 start 的项目 uid。"""
    from dubforge.pipeline import steps as steps_mod
    def run(ctx):
        ctx.write_json(f'{key}_result.json', {'ok': True})
    monkeypatch.setitem(steps_mod._RUNNERS, key, run)
    uid = make_project()
    mark_upstream_done(uid, key)
    return uid


def test_gpu_prep_stops_managed_tts_service_for_preprocess(isolated_env, make_project, monkeypatch):
    """分离前空闲显存低于阈值 → 自动停止托管配音服务; 显存充足或非托管不动作。"""
    config.update_config({'runner.gpu_free_reserve_mb': 4096})
    record = []
    state = _fake_tts_service(monkeypatch, managed=True, online=True, record=record)
    monkeypatch.setattr('dubforge.utils.gpu_memory.free_mb', lambda: 500)
    uid = _stub_step(monkeypatch, make_project, 'preprocess')
    runner.start_step(uid, 'preprocess')
    wait_status(uid, 'preprocess', {'done'})
    assert state['stopped'] == 1 and record == ['stop']

    # 显存充足: 不停止
    record.clear(); state['stopped'] = 0
    monkeypatch.setattr('dubforge.utils.gpu_memory.free_mb', lambda: 6000)
    monkeypatch.setattr('dubforge.utils.gpu_memory.free_mb', lambda: 6000)
    uid2 = _stub_step(monkeypatch, make_project, 'preprocess')
    runner.start_step(uid2, 'preprocess')
    wait_status(uid2, 'preprocess', {'done'})
    assert state['stopped'] == 0

    # 外部服务: 不代停
    record.clear()
    _fake_tts_service(monkeypatch, managed=False, online=True, record=record)
    monkeypatch.setattr('dubforge.utils.gpu_memory.free_mb', lambda: 500)
    uid3 = _stub_step(monkeypatch, make_project, 'preprocess')
    runner.start_step(uid3, 'preprocess')
    wait_status(uid3, 'preprocess', {'done'})
    assert state['stopped'] == 0


def test_gpu_prep_starts_tts_service_for_tts_step(isolated_env, make_project, monkeypatch):
    """配音步骤前托管服务未在线 → 自动启动并等待就绪; 关闭开关则完全不动。"""
    config.update_config({'runner.tts_start_timeout': 60})
    record = []
    state = _fake_tts_service(monkeypatch, managed=True, online=False, record=record)
    uid = _stub_step(monkeypatch, make_project, 'tts')
    runner.start_step(uid, 'tts')
    wait_status(uid, 'tts', {'done'})
    assert state['started'] == 1 and 'start' in record

    config.update_config({'runner.auto_tts_vram': False})
    _fake_tts_service(monkeypatch, managed=True, online=False, record=record)
    uid2 = _stub_step(monkeypatch, make_project, 'tts')
    runner.start_step(uid2, 'tts')
    wait_status(uid2, 'tts', {'done'})
    assert not any(a == 'start' for a in record[len(record):]) or state['started'] == 1
