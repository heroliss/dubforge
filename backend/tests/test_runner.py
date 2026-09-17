# -*- coding: utf-8 -*-
"""
M1 执行器测试: 状态机全路径
============================
覆盖: 完成产物 / 指纹跳过 / 配置失效重算 / 重跑→下游 stale / 取消 /
模拟失败 / 崩溃恢复 / API E2E (fresh 标记)
"""
import json
import time

import pytest

from conftest import wait_status
from dubforge import config as cfgmod
from dubforge.domain import crud


def test_run_done_and_artifact(make_project, isolated_env):
    uid = make_project()
    runner = _runner()
    r = runner.start_step(uid, "translate")
    assert r["status"] == "started"
    wait_status(uid, "translate", {"done"})

    project = crud.get_project(uid)
    arts = crud.latest_artifacts(project.id, ["translate"])
    assert "translate/translate_result.json" in {f"{a.step_key}/{a.name}"
                                               for a in arts.values()}
    # 产物文件在盘上, 日志非空
    art = next(iter(arts.values()))
    assert (isolated_env / "data" / "projects" / uid / art.rel_path).exists()
    log = (isolated_env / "data" / "projects" / uid / "logs" / "translate.log")
    assert "开始步骤" in log.read_text(encoding="utf-8")
    # manifest 已刷新为 done
    m = json.loads((isolated_env / "data" / "projects" / uid / "manifest.json")
                   .read_text(encoding="utf-8"))
    assert m["steps"][4]["status"] == "done"  # translate 是第 5 步


def test_skip_when_fresh(make_project, isolated_env):
    uid = make_project()
    runner = _runner()
    runner.start_step(uid, "translate")
    wait_status(uid, "translate", {"done"})

    art_path = (isolated_env / "data" / "projects" / uid / "artifacts" / "translate"
                / "translate_result.json")
    mtime1 = art_path.stat().st_mtime

    # 再跑 (不 force) → 跳过, 产物未动
    r = runner.start_step(uid, "translate", force=False)
    assert r["status"] == "skipped"
    assert art_path.stat().st_mtime == mtime1


def test_config_change_invalidates(make_project, isolated_env, monkeypatch):
    uid = make_project()
    runner = _runner()
    runner.start_step(uid, "translate")
    wait_status(uid, "translate", {"done"})

    # translate 的 cfg_keys 含 ("translate",) —— 改其中一项 → 指纹变 → 不再跳过
    monkeypatch.setenv("DUBFORGE_CONFIG", str(isolated_env / "config.yaml"))
    cfgmod.set_key("translate.whole_doc_max_lines", 30)
    r = runner.start_step(uid, "translate", force=False)
    assert r["status"] == "started"
    wait_status(uid, "translate", {"done"})


def test_force_rerun_marks_downstream_stale(make_project, isolated_env):
    uid = make_project()
    runner = _runner()
    # 用桩步骤 split/translate (transcribe 已是需本地源的真实现, 状态机测试不碰)
    for key in ("split", "translate"):
        runner.start_step(uid, key)
        wait_status(uid, key, {"done"})
    runner.start_step(uid, "split", force=True)
    wait_status(uid, "split", {"done"})
    step_t = crud.get_step(crud.get_project(uid).id, "translate")
    assert step_t.status == "stale"
    assert "split" in step_t.error_message

    # stale 后正常重跑 (不 force) 也能执行 (status != done)
    runner.start_step(uid, "translate", force=False)
    wait_status(uid, "translate", {"done"})
    assert crud.get_step(crud.get_project(uid).id, "translate").status == "done"


def test_cancel_mid_run(make_project, isolated_env):
    uid = make_project(params={"delay_step": {"translate": 0.4}})
    runner = _runner()
    runner.start_step(uid, "translate")
    time.sleep(0.25)                      # 让步骤进入第一个分片
    r = runner.cancel_step(uid, "translate")
    assert r["status"] in ("cancelling", "not_running")
    status = wait_status(uid, "translate", {"failed", "done"}, timeout=10)
    step = crud.get_step(crud.get_project(uid).id, "translate")
    if status == "failed":                # 在分片间被取消
        assert step.error_code == "STEP_CANCELLED"
        # 无半成品产物
        arts = crud.latest_artifacts(crud.get_project(uid).id, ["translate"])
        assert not any(a.name.endswith("_result.json") for a in arts.values())


def test_fail_step_records_error(make_project, isolated_env):
    uid = make_project(params={"fail_step": "translate"})
    runner = _runner()
    runner.start_step(uid, "translate")
    wait_status(uid, "translate", {"failed"})
    step = crud.get_step(crud.get_project(uid).id, "translate")
    assert step.error_code == "STEP_FAILED"
    assert "模拟失败" in step.error_message
    assert step.error_suggestion          # 有中文修复建议


def test_crash_recovery(make_project, isolated_env):
    uid = make_project()
    project = crud.get_project(uid)
    # 模拟崩溃残留: step=running + job=running
    step = crud.get_step(project.id, "translate")
    step.status = "running"
    crud.save_step(step)
    job = crud.add_job(project.id, "translate", "x.log")

    runner = _runner()
    result = runner.recover()
    assert result["steps_interrupted"] >= 1
    step2 = crud.get_step(project.id, "translate")
    assert step2.status == "failed"
    assert step2.error_code == "STEP_INTERRUPTED"
    assert "重跑" in step2.error_suggestion


def test_api_e2e(isolated_env):
    """API 全环: 建项目 → 跑步骤 → 轮询 → fresh 标记 → 日志读取"""
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        r = client.post("/api/projects", json={"title": "E2E 项目",
                                               "source_url": "http://e.com/1"})
        assert r.status_code == 200
        uid = r.json()["uid"]

        r = client.post(f"/api/projects/{uid}/steps/split/run",
                        json={"force": False})
        assert r.json()["status"] == "started"
        for _ in range(200):
            steps = client.get(f"/api/projects/{uid}/steps").json()
            if steps[3]["status"] == "done":
                break
            time.sleep(0.05)
        assert steps[3]["status"] == "done"
        assert steps[3]["fresh"] is True          # 指纹一致 → 前端显示"已是最新"

        r = client.get(f"/api/projects/{uid}/steps/split/log")
        assert r.status_code == 200
        assert any("开始步骤" in l for l in r.json()["lines"])

        # 不存在步骤 → 400 带错误码
        r = client.post(f"/api/projects/{uid}/steps/nope/run", json={})
        assert r.status_code == 400


def _runner():
    from dubforge.runner import runner
    return runner
