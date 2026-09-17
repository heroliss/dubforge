# -*- coding: utf-8 -*-
"""
M1 执行器测试: 状态机全路径
============================
用仍是桩实现的步骤 (dubtask/tts/assemble) 做状态机验证 —— 不碰网络/真实上游。
覆盖: 完成产物 / 指纹跳过 / 配置失效重算 / 重跑→下游 stale / 取消 /
模拟失败 / 崩溃恢复 / API E2E (fresh 标记)
真实链路 (download→…→subtitles) 的测试在 test_media_chain.py / test_text_chain.py。
"""
import json
import time

from conftest import wait_status
from dubforge.domain import crud


def test_run_done_and_artifact(make_project, isolated_env):
    uid = make_project()
    runner = _runner()
    r = runner.start_step(uid, "dubtask")
    assert r["status"] == "started"
    wait_status(uid, "dubtask", {"done"})

    project = crud.get_project(uid)
    arts = crud.latest_artifacts(project.id, ["dubtask"])
    assert "dubtask/dubtask_result.json" in {f"{a.step_key}/{a.name}"
                                             for a in arts.values()}
    # 产物文件在盘上, 日志非空
    art = next(iter(arts.values()))
    assert (isolated_env / "data" / "projects" / uid / art.rel_path).exists()
    log = (isolated_env / "data" / "projects" / uid / "logs" / "dubtask.log")
    assert "开始步骤" in log.read_text(encoding="utf-8")
    # manifest 已刷新为 done
    m = json.loads((isolated_env / "data" / "projects" / uid / "manifest.json")
                   .read_text(encoding="utf-8"))
    assert m["steps"][6]["status"] == "done"   # dubtask 是第 7 步


def test_skip_when_fresh(make_project, isolated_env):
    uid = make_project()
    runner = _runner()
    runner.start_step(uid, "dubtask")
    wait_status(uid, "dubtask", {"done"})

    art_path = (isolated_env / "data" / "projects" / uid / "artifacts" / "dubtask"
                / "dubtask_result.json")
    mtime1 = art_path.stat().st_mtime

    # 再跑 (不 force) → 跳过, 产物未动
    r = runner.start_step(uid, "dubtask", force=False)
    assert r["status"] == "skipped"
    assert art_path.stat().st_mtime == mtime1


def test_config_change_invalidates(make_project, isolated_env, monkeypatch):
    uid = make_project()
    runner = _runner()
    runner.start_step(uid, "dubtask")
    wait_status(uid, "dubtask", {"done"})

    # dubtask 的 cfg_keys=("tts",) —— 改其中一项 → 指纹变 → 不再跳过
    monkeypatch.setenv("DUBFORGE_CONFIG", str(isolated_env / "config.yaml"))
    from dubforge import config as cfgmod
    cfgmod.set_key("tts.backend", "gptsovits")
    r = runner.start_step(uid, "dubtask", force=False)
    assert r["status"] == "started"
    wait_status(uid, "dubtask", {"done"})


def test_force_rerun_marks_downstream_stale(make_project, isolated_env):
    uid = make_project()
    runner = _runner()
    for key in ("tts", "assemble"):
        runner.start_step(uid, key)
        wait_status(uid, key, {"done"})

    # force 重跑 tts → 下游 assemble 置 stale
    runner.start_step(uid, "tts", force=True)
    wait_status(uid, "tts", {"done"})
    step_a = crud.get_step(crud.get_project(uid).id, "assemble")
    assert step_a.status == "stale"
    assert "tts" in step_a.error_message

    # stale 后正常重跑 (不 force) 也能执行 (status != done)
    runner.start_step(uid, "assemble", force=False)
    wait_status(uid, "assemble", {"done"})
    assert crud.get_step(crud.get_project(uid).id, "assemble").status == "done"


def test_cancel_mid_run(make_project, isolated_env):
    uid = make_project(params={"delay_step": {"tts": 0.4}})
    runner = _runner()
    runner.start_step(uid, "tts")
    time.sleep(0.25)                      # 让步骤进入第一个分片
    r = runner.cancel_step(uid, "tts")
    assert r["status"] in ("cancelling", "not_running")
    status = wait_status(uid, "tts", {"failed", "done"}, timeout=10)
    step = crud.get_step(crud.get_project(uid).id, "tts")
    if status == "failed":                # 在分片间被取消
        assert step.error_code == "STEP_CANCELLED"
        # 无半成品产物
        arts = crud.latest_artifacts(crud.get_project(uid).id, ["tts"])
        assert not any(a.name.endswith("_result.json") for a in arts.values())


def test_fail_step_records_error(make_project, isolated_env):
    uid = make_project(params={"fail_step": "assemble"})
    runner = _runner()
    runner.start_step(uid, "assemble")
    wait_status(uid, "assemble", {"failed"})
    step = crud.get_step(crud.get_project(uid).id, "assemble")
    assert step.error_code == "STEP_FAILED"
    assert "模拟失败" in step.error_message
    assert step.error_suggestion          # 有中文修复建议


def test_crash_recovery(make_project, isolated_env):
    uid = make_project()
    project = crud.get_project(uid)
    # 模拟崩溃残留: step=running + job=running
    step = crud.get_step(project.id, "tts")
    step.status = "running"
    crud.save_step(step)
    job = crud.add_job(project.id, "tts", "x.log")

    runner = _runner()
    result = runner.recover()
    assert result["steps_interrupted"] >= 1
    step2 = crud.get_step(project.id, "tts")
    assert step2.status == "failed"
    assert step2.error_code == "STEP_INTERRUPTED"
    assert "重跑" in step2.error_suggestion


def test_api_e2e(isolated_env):
    """API 全环: 建项目 → 跑桩步骤 → 轮询 → fresh 标记 → 日志读取"""
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        r = client.post("/api/projects", json={"title": "E2E 项目",
                                               "source_url": "http://e.com/1"})
        assert r.status_code == 200
        uid = r.json()["uid"]

        r = client.post(f"/api/projects/{uid}/steps/tts/run",
                        json={"force": False})
        assert r.json()["status"] == "started"
        for _ in range(200):
            steps = client.get(f"/api/projects/{uid}/steps").json()
            if steps[7]["status"] == "done":
                break
            time.sleep(0.05)
        assert steps[7]["status"] == "done"
        assert steps[7]["fresh"] is True          # 指纹一致 → 前端显示"已是最新"

        r = client.get(f"/api/projects/{uid}/steps/tts/log")
        assert r.status_code == 200
        assert any("开始步骤" in l for l in r.json()["lines"])

        # 不存在步骤 → 400 带错误码
        r = client.post(f"/api/projects/{uid}/steps/nope/run", json={})
        assert r.status_code == 400


def _runner():
    from dubforge.runner import runner
    return runner
