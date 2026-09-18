# -*- coding: utf-8 -*-
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
    import dubforge.db as db
    db.reset_engine()
    db.init_db()                      # 每个测试独立建表 (不依赖 TestClient lifespan)
    yield tmp_path
    db.reset_engine()


def _engine_for_test():
    from dubforge.db import get_engine
    return get_engine()


def wait_status(uid: str, step_key: str, statuses: set[str], timeout: float = 15.0) -> str:
    """轮询等待步骤到达终态 (执行器在后台线程)"""
    from dubforge.domain import crud
    deadline = time.time() + timeout
    while time.time() < deadline:
        project = crud.get_project(uid)
        step = crud.get_step(project.id, step_key)
        if step.status in statuses:
            return step.status
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
