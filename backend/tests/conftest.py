"""共享夹具: 每个测试独立的 DATA_ROOT/CONFIG (互不污染真实数据)"""
import time

import pytest


@pytest.fixture()
def isolated_env(tmp_path, monkeypatch):
    # DUBFORGE_DEBUG_DATADIR 设置时把数据放到 pytest tmp 之外 (排查文件消失类问题用)
    import os as _os
    root = _os.environ.get("DUBFORGE_DEBUG_DATADIR") or str(tmp_path / "data")
    monkeypatch.setenv("DUBFORGE_DATA_ROOT", root)
    monkeypatch.setenv("DUBFORGE_CONFIG", str(tmp_path / "config.yaml"))
    from dubforge import db
    db.reset_engine()
    db.init_db()                      # 每个测试独立建表 (不依赖 TestClient lifespan)
    from dubforge import tts_service
    from types import SimpleNamespace
    import subprocess

    def forbid_model_launch(*args, **kwargs):
        raise AssertionError('隔离测试禁止启动真实配音模型；请提供服务或进程替身')

    # 只替换服务模块持有的进程接口，不能改全局 subprocess.Popen：
    # FFmpeg 和测试自建的轻量进程仍可运行；服务专项测试可显式覆盖此替身。
    monkeypatch.setattr(tts_service, 'subprocess', SimpleNamespace(
        **{key: value for key, value in vars(subprocess).items() if key != 'Popen'},
        Popen=forbid_model_launch))
    from dubforge.runner import runner
    from dubforge import comment_studio
    # TestClient 会反复进入应用生命周期；每个隔离测试只在旧任务收尾后重开单例。
    runner.startup()
    comment_studio.startup()
    # done/failed 状态早于 worker 最后的 manifest 和项目状态写入；
    # 等 Future 真正结束后再切换数据库，防止上个测试的收尾污染下个测试。
    pending = []
    submit = runner._pool.submit

    def tracked_submit(*args, **kwargs):
        future = submit(*args, **kwargs)
        pending.append(future)
        return future

    monkeypatch.setattr(runner._pool, "submit", tracked_submit)
    queue_execution = runner._queue_execution

    def tracked_queue(*args, **kwargs):
        # 资源准入前的任务还没有线程池 Future，也必须等它完成才能切库。
        future = queue_execution(*args, **kwargs)
        pending.append(future)
        return future

    monkeypatch.setattr(runner, '_queue_execution', tracked_queue)
    from dubforge.publishing import chapter_dispatch
    chapter_submit = chapter_dispatch._pool.submit

    def tracked_chapter_submit(*args, **kwargs):
        future = chapter_submit(*args, **kwargs)
        pending.append(future)
        return future

    monkeypatch.setattr(chapter_dispatch._pool, 'submit', tracked_chapter_submit)
    from dubforge.publishing import updates as publication_updates
    publication_updates._stop.clear()
    update_submit = publication_updates._pool.submit

    def tracked_update_submit(*args, **kwargs):
        future = update_submit(*args, **kwargs)
        pending.append(future)
        return future

    monkeypatch.setattr(publication_updates._pool, 'submit', tracked_update_submit)
    # runner 是跨测试的单例: 清掉重试计划/槽位/废弃标记等运行态,
    # 否则上一个测试的残留 (project_id 从 1 重新计数) 会污染后续测试。
    def _reset_runner_state():
        with runner._lock:
            for value in runner._retries.values():
                # 兼容测试替身及旧进程留下的简化标记；真实值为
                # (cancel_event, attempts, next_at) 三元组。
                cancel = value[0] if isinstance(value, (tuple, list)) and value else None
                if cancel is not None and hasattr(cancel, 'set'):
                    cancel.set()
            runner._retries.clear()
            runner._held.clear()
            runner._abandoned.clear()
    _reset_runner_state()
    try:
        yield tmp_path
    finally:
        chapter_dispatch.shutdown()
        publication_updates.shutdown()
        try:
            index = 0
            while index < len(pending):   # 续跑链可能在收尾时提交下一步。
                if not pending[index].cancelled():
                    pending[index].result(timeout=30)
                index += 1
        finally:
            _reset_runner_state()
            db.reset_engine()


def _engine_for_test():
    from dubforge.db import get_engine
    return get_engine()


def synthesize(client, url, **kwargs):
    """领域结果测试等待后台任务完成；异步调度测试直接使用 client.post。"""
    from dubforge.domain import crud
    from dubforge.runner import runner
    response = client.post(url, **kwargs)
    if response.status_code == 200 and response.json().get('status') == 'started':
        uid = url.split('/projects/', 1)[1].split('/')[0]
        project = crud.get_project(uid)
        deadline = time.monotonic() + 15
        while runner.current_job_id(project.id, 'tts') is not None:
            if time.monotonic() >= deadline:
                raise TimeoutError('配音后台任务未完成')
            time.sleep(.01)
        assert crud.latest_job(project.id, 'tts').status == 'done'
    return response


def wait_status(uid: str, step_key: str, statuses: set[str], timeout: float = 15.0) -> str:
    """成功还需等待同一步后台收尾；失败/取消保留提前终结的观察能力。"""
    from dubforge.domain import crud
    deadline = time.time() + timeout
    while time.time() < deadline:
        project = crud.get_project(uid)
        step = crud.get_step(project.id, step_key)
        if step.status in statuses:
            if step.status == 'done':
                from dubforge.runner import runner
                if runner.current_job_id(project.id, step_key) is not None:
                    time.sleep(0.05)
                    continue
            return step.status
        if step.status == "failed":
            raise AssertionError(f"步骤失败 {step_key}: {step.error_message}")
        time.sleep(0.05)
    err = f" | {step.error_code}: {step.error_message}" if step.error_code else ""
    import sqlmodel as _sm
    from dubforge.domain.models import Job as _Job
    with _sm.Session(_engine_for_test()) as _s:
        _j = _s.exec(_sm.select(_Job).where(
            _Job.project_id == project.id, _Job.step_key == step_key)).first()
        if _j and _j.error_detail:
            err += "\n  detail: " + _j.error_detail[-400:]
    raise TimeoutError(f"步骤 {step_key} 未在 {timeout}s 内到达 {statuses}, 当前 "
                       f"{step.status}{err}")


@pytest.fixture(autouse=True)
def _no_real_recycle_bin(monkeypatch):
    """回收操作只模拟删除隔离数据目录内的对象，绝不触碰真实系统回收站。"""
    import os as _os
    import shutil as _shutil
    from pathlib import Path as _Path

    def remove_isolated(path):
        value = _os.environ.get('DUBFORGE_DATA_ROOT')
        if not value:
            pytest.fail('回收模拟必须声明隔离数据目录')
        root = _Path(value).resolve()
        target = _Path(path).resolve()
        if target == root or not target.is_relative_to(root):
            pytest.fail('回收模拟目标必须位于隔离数据目录内，且不能是目录根')
        if target.is_dir():
            _shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)

    import send2trash as _st
    monkeypatch.setattr(_st, 'send2trash', remove_isolated)
    from dubforge.utils import recycle
    monkeypatch.setattr(recycle, '_windows_recycle', remove_isolated)



@pytest.fixture()
def stub_heavy_steps(monkeypatch):
    """把指定步骤的运行函数临时替换为桩行为 (状态机测试与真实现解耦)。

    用法: stub_heavy_steps("assemble") 或 stub_heavy_steps("tts", "assemble")。
    桩支持项目参数: fail_step / delay_step (与原桩实现语义一致)。
    """
    import time as _time

    from dubforge.errors import StepFailedError
    from dubforge.pipeline import steps as steps_mod
    from dubforge import tts_service

    # 重型步骤已被替身接管时，资源准备也必须隔离，避免状态机测试
    # 因本机安装了配音工具而启动真实模型。资源管理专项测试自行提供服务替身。
    monkeypatch.setattr(tts_service, 'status', lambda *a, **k: {
        'online': True, 'managed': False, 'can_start': False, 'can_stop': False,
        'status': 'online', 'backend': 'indextts25', 'message': ''})
    monkeypatch.setattr(tts_service, 'start', lambda *a, **k: (_ for _ in ()).throw(
        AssertionError('步骤替身测试禁止启动真实配音服务')))
    monkeypatch.setattr(tts_service, 'stop', lambda *a, **k: (_ for _ in ()).throw(
        AssertionError('步骤替身测试禁止停止真实配音服务')))

    def make_stub(key):
        def run(ctx):
            delays = ctx.project_params.get("delay_step", {})
            delay = float(delays.get(key, 0)) if isinstance(delays, dict) else 0.0
            for i in range(3):
                if delay:
                    _time.sleep(delay)
                ctx.check_cancel()
                ctx.log(f"[stub] 分片 {i + 1}/3")
            if ctx.project_params.get("fail_step") == key:
                raise StepFailedError(f"模拟失败: 步骤 {key} (项目参数 fail_step 触发)")
            ctx.write_json(f"{key}_result.json", {"step": key})
            if key == 'assemble':
                # 合成状态现在校验实际版本文件；桩也须登记对应成品，而不只写诊断 JSON。
                from dubforge.pipeline.assembly_outputs import default_variants
                if ctx.task_data.get('version_specs'):
                    from dubforge.pipeline import assembly_versions as av
                    from dubforge.pipeline.dependencies import Dependencies
                    for version in ctx.task_data['version_specs']:
                        name=f'final_version_{version["id"]}.mp4'
                        ctx.write_bytes(name,b'test video','video')
                        av.record_result(ctx,version,name,av.fingerprint(Dependencies(ctx.project),version))
                else:
                    for variant in ctx.task_data.get('variants') or default_variants(ctx.cfg):
                        ctx.write_bytes(f'final_{variant}_20260928_000000_000001.mp4', b'test video', 'video')
            # 桩仍遵守步骤产物契约，续跑门禁校验真实文件而非前序状态。
            contents = {"split": ("split.txt", "1. 0.000-1.000: hello\n"),
                        "translate": ("translate.txt", "1. 你好\n"),
                        "tts": ("1.wav", "test audio")}
            if key in contents:
                name, text = contents[key]
                path = ctx.pdir / "artifacts" / key / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                ctx.register_file(name, path, "test")
                if key == 'tts':
                    from dubforge.pipeline.dependencies import Dependencies
                    from dubforge.pipeline.dubbing_edit import record_take
                    from dubforge.pipeline.step_tts import line_fingerprint
                    line = Dependencies(ctx.project).dubbing_lines(required=True)[0]
                    record_take(ctx.project, line, ctx.pdir, 1, line_fingerprint(ctx.project, line), {})
        return run

    def _patch(*keys):
        for k in keys:
            monkeypatch.setitem(steps_mod._RUNNERS, k, make_stub(k))
    return _patch


@pytest.fixture()
def make_project(isolated_env):
    """项目工厂: 默认无延迟; 可传 params (如 fail_step / delay_step)"""
    from dubforge.domain import crud

    def _make(params: dict | None = None, title: str = "测试项目") -> str:
        p = crud.create_project(title=title, source_url="http://example.com/v",
                                params=params or {})
        return p.uid

    return _make


@pytest.fixture(name="pdir")
def pdir_fixture(isolated_env):
    """项目目录解析 (语义化目录名感知); 用法: pdir(uid) / "audio" / ..."""
    def _get(uid: str):
        from dubforge.domain import crud
        return crud.project_dir(uid)
    return _get


def mark_upstream_done(uid: str, key: str) -> None:
    """播种隔离测试所需的完整上游文件；真实链已生成的文件保持不变。"""
    from dubforge.domain import crud
    from dubforge.pipeline.registry import STEP_KEYS, STEP_MAP
    from dubforge.utils.lineprotocols import parse_timed_lines
    if key == 'assemble':
        from dubforge import configuration
        current=configuration.resolve(uid)
        if 'assembly.versions' not in current['overrides']:
            configuration.save('project:'+uid,dict(revision=current['revision'],updates={'assembly.versions':[
                dict(id='default',name='测试配音',dubbing=True,burn_subtitles=False,include_comments=False,overrides={})]}))
    import json
    project = crud.get_project(uid)
    root = crud.project_dir(uid)
    arts = crud.latest_artifacts(project.id, None)
    defaults = {
        "src.srt": "1\n00:00:00,000 --> 00:00:01,000\nhello\n",
        "split.txt": "1. 0.000-1.000: hello\n", "words.json": "[]",
        "transcribe.json": '{"duration": 1}', "dubtask.json": '{"lines": [{"id": 1, "text": "你好", "start": 0, "end": 1}]}',
    }
    for k in STEP_KEYS[:STEP_KEYS.index(key)]:
        for name, _ in STEP_MAP[k].outputs:
            if "*" in name:
                continue
            if "<句id>" in name:
                name = "1.wav"
            if f"{k}/{name}" in arts:
                continue
            text = defaults.get(name, "test")
            if name == "translate.txt":
                split_art = crud.latest_artifacts(project.id, ["split"]).get("split/split.txt")
                rows = parse_timed_lines((root / split_art.rel_path).read_text(encoding="utf-8"))
                text = "\n".join(f"{l['id']}. 测试译文{l['id']}" for l in rows) + "\n"
            path = root / STEP_MAP[k].dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text(text, encoding="utf-8")
            crud.add_artifact(project.id, k, "test", name, path, str(path.relative_to(root)))
            if k == 'tts':
                from dubforge.pipeline.dependencies import Dependencies
                from dubforge.pipeline.dubbing_edit import record_take
                from dubforge.pipeline.step_tts import line_fingerprint
                line = Dependencies(project).dubbing_lines(required=True)[0]
                record_take(project, line, root, 1, line_fingerprint(project, line), {})
        st = crud.get_step(project.id, k)
        st.status = "done"
        crud.save_step(st)
