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
    from dubforge.runner import runner
    # done/failed 状态早于 worker 最后的 manifest 和项目状态写入；
    # 等 Future 真正结束后再切换数据库，防止上个测试的收尾污染下个测试。
    pending = []
    submit = runner._pool.submit

    def tracked_submit(*args, **kwargs):
        future = submit(*args, **kwargs)
        pending.append(future)
        return future

    monkeypatch.setattr(runner._pool, "submit", tracked_submit)
    # runner 是跨测试的单例: 清掉重试计划/槽位/废弃标记等运行态,
    # 否则上一个测试的残留 (project_id 从 1 重新计数) 会污染后续测试。
    def _reset_runner_state():
        with runner._lock:
            for cancel, _attempts, _at in runner._retries.values():
                cancel.set()
            runner._retries.clear()
            runner._held.clear()
            runner._abandoned.clear()
    _reset_runner_state()
    try:
        yield tmp_path
    finally:
        _reset_runner_state()
        try:
            index = 0
            while index < len(pending):   # 续跑链可能在收尾时提交下一步。
                pending[index].result(timeout=30)
                index += 1
        finally:
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
    """轮询等待步骤到达终态 (执行器在后台线程)"""
    from dubforge.domain import crud
    deadline = time.time() + timeout
    while time.time() < deadline:
        project = crud.get_project(uid)
        step = crud.get_step(project.id, step_key)
        if step.status in statuses:
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
    """测试里删除项目不允许碰真实系统回收站 (send2trash 会把隔离环境里的
    临时目录扔进用户回收站) —— 一律替换为普通删除。"""
    import shutil as _shutil

    import send2trash as _st
    monkeypatch.setattr(_st, "send2trash", lambda p: _shutil.rmtree(p, ignore_errors=True))
    # 单文件安全回收有独立入口，测试必须显式模拟，不触碰真实回收站。
    from dubforge.utils import recycle
    monkeypatch.setattr(recycle, '_windows_recycle', lambda p: pytest.fail('测试未声明回收模拟'))



@pytest.fixture()
def stub_heavy_steps(monkeypatch):
    """把指定步骤的运行函数临时替换为桩行为 (状态机测试与真实现解耦)。

    用法: stub_heavy_steps("assemble") 或 stub_heavy_steps("tts", "assemble")。
    桩支持项目参数: fail_step / delay_step (与原桩实现语义一致)。
    """
    import time as _time

    from dubforge.errors import StepFailedError
    from dubforge.pipeline import steps as steps_mod

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
