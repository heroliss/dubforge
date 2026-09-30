"""
M1 执行器测试: 状态机全路径
============================
用仍是桩实现的步骤 (assemble) 做状态机验证 —— 不碰网络/真实上游。
TTS 场景用 mock 后端 (真实现但离线可控)。
覆盖: 完成产物 / 指纹跳过 / 配置失效重算 / 重跑→下游 stale / 取消 /
模拟失败 / 崩溃恢复 / API E2E (fresh 标记)
真实链路 (download→…→subtitles) 的测试在 test_media_chain.py / test_text_chain.py。
"""
import json
import time

from conftest import mark_upstream_done, wait_status
from dubforge.domain import crud


def test_running_context_keeps_config_snapshot(make_project, isolated_env, pdir):
    """运行中改设置只影响下一轮，当前上下文不应换参数或模型。"""
    import threading

    from dubforge import config
    from dubforge.runner import StepContext

    uid = make_project()
    project = crud.get_project(uid)
    config.set_key("split.max_split_length", 22)
    config.set_key("llm.profiles", [{"name": "旧模型", "enabled": True,
                                     "base_url": "http://old", "model": "old"}])
    ctx = StepContext(project, "split", pdir(uid),
                      pdir(uid) / "logs" / "split.log", threading.Event())
    config.set_key("split.max_split_length", 44)
    config.set_key("llm.profiles", [{"name": "新模型", "enabled": True,
                                     "base_url": "http://new", "model": "new"}])
    assert ctx.cfg("split.max_split_length") == 22
    assert ctx.llm_profile_snapshot()[0]["model"] == "old"



def test_run_done_and_artifact(make_project, isolated_env, stub_heavy_steps, pdir):
    stub_heavy_steps("assemble")
    uid = make_project()
    runner = _runner()
    mark_upstream_done(uid, "assemble")
    r = runner.start_step(uid, "assemble")
    assert r["status"] == "started"
    wait_status(uid, "assemble", {"done"})

    project = crud.get_project(uid)
    arts = crud.latest_artifacts(project.id, ["assemble"])
    assert "assemble/assemble_result.json" in {f"{a.step_key}/{a.name}"
                                             for a in arts.values()}
    # 产物文件在盘上, 日志非空
    art = next(iter(arts.values()))
    assert (pdir(uid) / art.rel_path).exists()
    assert crud.get_step(crud.get_project(uid).id, "assemble").progress == 100.0
    log = (pdir(uid) / "logs" / "assemble.log")
    assert "开始步骤" in log.read_text(encoding="utf-8")
    # manifest 已刷新为 done
    m = json.loads((pdir(uid) / "manifest.json")
                   .read_text(encoding="utf-8"))
    assert m["steps"][8]["status"] == "done"   # assemble 是第 9 步


def test_skip_when_fresh(make_project, isolated_env, stub_heavy_steps, pdir):
    stub_heavy_steps("assemble")
    uid = make_project()
    runner = _runner()
    mark_upstream_done(uid, "assemble")
    runner.start_step(uid, "assemble")
    wait_status(uid, "assemble", {"done"})

    art_path = (pdir(uid) / "artifacts" / "assemble"
                / "assemble_result.json")
    mtime1 = art_path.stat().st_mtime

    # 再跑 (不 force) → 跳过, 产物未动
    r = runner.start_step(uid, "assemble", force=False)
    assert r["status"] == "skipped"
    assert art_path.stat().st_mtime == mtime1


def test_missing_artifact_invalidates_fresh(make_project, isolated_env,
                                            stub_heavy_steps, pdir):
    """DB 仍有 done/指纹但文件被手动删除时，下一次运行必须重建。"""
    stub_heavy_steps("assemble")
    uid = make_project()
    mark_upstream_done(uid, "assemble")
    runner = _runner()
    runner.start_step(uid, "assemble")
    wait_status(uid, "assemble", {"done"})
    (pdir(uid) / "artifacts" / "assemble" / "final_version_default.mp4").unlink()
    r = runner.start_step(uid, "assemble")
    assert r["status"] == "started"
    wait_status(uid, "assemble", {"done"})


def test_same_size_artifact_mutation_invalidates_skip(make_project, isolated_env,
                                                      stub_heavy_steps, pdir):
    """产物内容被原地替换但大小不变时，执行器仍需通过哈希发现变化。"""
    stub_heavy_steps("assemble")
    uid = make_project()
    mark_upstream_done(uid, "assemble")
    runner = _runner()
    runner.start_step(uid, "assemble")
    wait_status(uid, "assemble", {"done"})
    path = pdir(uid) / "artifacts" / "assemble" / "final_version_default.mp4"
    data = bytearray(path.read_bytes())
    data[1] ^= 1
    path.write_bytes(data)
    r = runner.start_step(uid, "assemble")
    assert r["status"] == "started"
    wait_status(uid, "assemble", {"done"})


def test_failed_retry_forces_rebuild(make_project, isolated_env, monkeypatch):
    """失败步骤即使指纹未变，重试也必须告诉实现替换旧产物。"""
    import dubforge.pipeline.steps as steps_mod
    from dubforge.runner import compute_fingerprint

    seen = []

    def run(ctx):
        seen.append(ctx.rebuild)
        ctx.write_json("assemble_result.json", {"rebuilt": ctx.rebuild})

    monkeypatch.setitem(steps_mod._RUNNERS, "assemble", run)
    uid = make_project()
    mark_upstream_done(uid, "assemble")
    project = crud.get_project(uid)
    step = crud.get_step(project.id, "assemble")
    step.status = "failed"
    step.input_fingerprint = compute_fingerprint(project, "assemble")
    crud.save_step(step)

    _runner().start_step(uid, "assemble")
    wait_status(uid, "assemble", {"done"})
    assert seen == [True]


def test_config_change_invalidates(make_project, isolated_env, monkeypatch, stub_heavy_steps):
    stub_heavy_steps("assemble")
    uid = make_project()
    runner = _runner()
    mark_upstream_done(uid, "assemble")
    runner.start_step(uid, "assemble")
    wait_status(uid, "assemble", {"done"})

    # assemble 的 cfg_keys=("assemble",) —— 改其中一项 → 指纹变 → 不再跳过
    monkeypatch.setenv("DUBFORGE_CONFIG", str(isolated_env / "config.yaml"))
    from dubforge import config as cfgmod
    cfgmod.set_key("assemble.bgm_volume", 0.6)
    r = runner.start_step(uid, "assemble", force=False)
    assert r["status"] == "started"
    wait_status(uid, "assemble", {"done"})


def test_force_rerun_marks_downstream_stale(make_project, isolated_env, stub_heavy_steps):
    """tts → assemble (双双桩化); force 重跑 tts → 下游置 stale"""
    stub_heavy_steps("tts", "assemble")
    uid = make_project()
    from test_configuration import save
    save('project:'+uid,{'assembly.versions':[dict(id='default',name='配音',dubbing=True,burn_subtitles=False,include_comments=False,overrides={})]})
    project = crud.get_project(uid)
    runner = _runner()
    mark_upstream_done(uid, "tts")
    for key in ("tts", "assemble"):
        runner.start_step(uid, key)
        wait_status(uid, key, {"done"})

    # force 重跑 tts → 下游 assemble 置 stale
    runner.start_step(uid, "tts", force=True)
    wait_status(uid, "tts", {"done"})
    step_a = crud.get_step(project.id, "assemble")
    assert step_a.status == "stale"
    assert "tts" in step_a.error_message

    # stale 后正常重跑 (不 force) 也能执行 (status != done)
    runner.start_step(uid, "assemble", force=False)
    wait_status(uid, "assemble", {"done"})
    assert crud.get_step(project.id, "assemble").status == "done"


def test_force_run_keeps_unstarted_downstream_pending(make_project, stub_heavy_steps):
    """新工程上游强制重跑不能把未运行的下游变成“重新运行”。"""
    stub_heavy_steps("download")
    uid = make_project()
    project = crud.get_project(uid)
    failed = crud.get_step(project.id, "translate")
    failed.status, failed.error_code = "failed", "TEST_FAILED"
    crud.save_step(failed)
    _runner().start_step(uid, "download", force=True)
    wait_status(uid, "download", {"done"})
    steps = {s.step_key: s for s in crud.get_steps(project.id)}
    assert all(s.status == "pending" for k, s in steps.items() if k not in {"download", "translate"})
    assert steps["translate"].status == "failed"
    assert steps["translate"].error_code == "TEST_FAILED"


def test_cancel_mid_run(make_project, isolated_env, stub_heavy_steps):
    stub_heavy_steps("assemble")
    uid = make_project(params={"delay_step": {"assemble": 0.4}})
    runner = _runner()
    mark_upstream_done(uid, "assemble")
    runner.start_step(uid, "assemble")
    time.sleep(0.25)                      # 让步骤进入第一个分片
    r = runner.cancel_step(uid, "assemble")
    assert r["status"] in ("cancelling", "not_running")
    status = wait_status(uid, "assemble", {"failed", "done"}, timeout=10)
    step = crud.get_step(crud.get_project(uid).id, "assemble")
    if status == "failed":                # 在分片间被取消
        assert step.error_code == "STEP_CANCELLED"
        # 无半成品产物
        arts = crud.latest_artifacts(crud.get_project(uid).id, ["assemble"])
        assert not any(a.name.endswith("_result.json") for a in arts.values())


def test_step_timeout_marks_failed_and_cleans_up(make_project, isolated_env,
                                                 stub_heavy_steps):
    """步骤超过活动运行兜底后应失败, 而不是一直占住自动链。"""
    from dubforge import config as cfgmod

    stub_heavy_steps("assemble")
    cfgmod.set_key("runner.step_timeout_seconds.assemble", 0.1)
    uid = make_project(params={"delay_step": {"assemble": 0.2}})
    mark_upstream_done(uid, "assemble")
    _runner().start_step(uid, "assemble")
    wait_status(uid, "assemble", {"failed"}, timeout=8)
    step = crud.get_step(crud.get_project(uid).id, "assemble")
    assert step.error_code == "STEP_TIMEOUT"
    log = (crud.project_dir(uid) / "logs" / "assemble.log").read_text(encoding="utf-8")
    assert "超时" in log


def test_fail_step_records_error(make_project, isolated_env, stub_heavy_steps):
    stub_heavy_steps("assemble")
    uid = make_project(params={"fail_step": "assemble"})
    runner = _runner()
    mark_upstream_done(uid, "assemble")
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
    step = crud.get_step(project.id, "assemble")
    step.status = "running"
    crud.save_step(step)
    crud.add_job(project.id, "assemble", "x.log")

    runner = _runner()
    result = runner.recover()
    assert result["steps_interrupted"] >= 1
    step2 = crud.get_step(project.id, "assemble")
    assert step2.status == "failed"
    assert step2.error_code == "STEP_INTERRUPTED"
    assert "重跑" in step2.error_suggestion


def test_api_e2e(isolated_env, stub_heavy_steps):
    """API 全环: 建项目 → 跑桩步骤 → 轮询 → fresh 标记 → 日志读取"""
    stub_heavy_steps("assemble")
    from app.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        r = client.post("/api/projects", json={"title": "E2E 项目",
                                               "source_url": "http://e.com/1"})
        assert r.status_code == 200
        uid = r.json()["uid"]

        mark_upstream_done(uid, "assemble")
        r = client.post(f"/api/projects/{uid}/steps/assemble/run",
                        json={"force": False})
        assert r.json()["status"] == "started"
        for _ in range(200):
            steps = client.get(f"/api/projects/{uid}/steps").json()
            assemble = next(s for s in steps if s["key"] == "assemble")
            # 步骤完成和任务收尾是两个持久化点；完整状态读取须等任务也完成。
            job = crud.latest_job(crud.get_project(uid).id, 'assemble')
            if assemble["status"] == "done" and job.status == 'done':
                break
            time.sleep(0.05)
        assemble = next(s for s in steps if s["key"] == "assemble")
        assert assemble["status"] == "done"
        assemble = next(s for s in client.get(f"/api/projects/{uid}/steps").json() if s['key'] == 'assemble')
        assert assemble["fresh"] is True          # 指纹一致且实际成品存在 → 前端显示"已是最新"

        r = client.get(f"/api/projects/{uid}/steps/assemble/log")
        assert r.status_code == 200
        assert any("开始步骤" in l for l in r.json()["lines"])

        # 不存在步骤 → 400 带错误码
        r = client.post(f"/api/projects/{uid}/steps/nope/run", json={})
        assert r.status_code == 400


def _runner():
    from dubforge.runner import runner
    return runner


def test_global_serial_with_queued_status(make_project, isolated_env, monkeypatch):
    """跨项目全局串行 (单线程池保护显存): 占住池时后提交的步骤显示排队中,
    且排队中可取消"""
    import threading

    import dubforge.pipeline.steps as steps_mod
    from dubforge import config as cfgmod
    cfgmod.set_key("runner.step_concurrency", {"download": 1})   # 显式串行
    gate = threading.Event()      # 控制第一个步骤何时放行
    started = threading.Event()

    def blocker(ctx):
        started.set()
        gate.wait(10)

    monkeypatch.setattr(steps_mod, "get_runner", lambda key: blocker)
    uid1, uid2 = make_project(), make_project()
    runner = _runner()
    runner.start_step(uid1, "download")
    assert started.wait(5), "第一个步骤未开始执行"

    # 池被占住 → 第二个项目的步骤进入排队态 (跨项目同样串行)
    previous = crud.get_step(crud.get_project(uid2).id, "download")
    previous.status = "failed"
    previous.error_code = "STEP_CANCELLED"
    previous.error_message = "步骤被用户取消"
    previous.error_suggestion = "上一轮的提示"
    crud.save_step(previous)
    runner.start_step(uid2, "download")
    wait_status(uid2, "download", {"queued"})
    queued = crud.get_step(previous.project_id, "download")
    assert (queued.error_code, queued.error_message, queued.error_suggestion) == ("", "", "")
    pid1 = crud.get_project(uid1).id
    assert crud.get_step(pid1, "download").status == "running"

    # 排队中取消 → 不执行, 标记取消
    runner.cancel_step(uid2, "download")
    gate.set()
    wait_status(uid1, "download", {"done"})
    wait_status(uid2, "download", {"failed"})
    step2 = crud.get_step(crud.get_project(uid2).id, "download")
    assert step2.error_code == "STEP_CANCELLED"


def test_recover_resets_queued(make_project, isolated_env):
    """崩溃恢复: 排队中的步骤没跑过 → 直接回待运行"""
    uid = make_project()
    step = crud.get_step(crud.get_project(uid).id, "download")
    step.status = "queued"
    crud.save_step(step)

    _runner().recover()
    assert crud.get_step(crud.get_project(uid).id, "download").status == "pending"


def test_deps_gate_blocks_pending_upstream(make_project, isolated_env, stub_heavy_steps):
    """依赖门禁: 上游未就绪 → 入口处明确拒绝 (DEPS_NOT_MET), 而不是运行时才报缺产物"""
    from dubforge.errors import DubForgeError
    stub_heavy_steps("assemble")
    uid = make_project()
    try:
        _runner().start_step(uid, "assemble")
        raised = None
    except DubForgeError as e:
        raised = e
    assert raised is not None and raised.code == "DEPS_NOT_MET"
    assert "split.txt" in raised.message and "translate.txt" in raised.message
    assert "dubtask.json" not in raised.message
    # 上游置 done 后可运行
    mark_upstream_done(uid, "assemble")
    r = _runner().start_step(uid, "assemble")
    assert r["status"] == "started"


def test_eta_is_independent_of_percent_and_refreshes_without_progress(make_project, isolated_env, pdir, monkeypatch):
    """百分比只反映工作量；倒计时无须再次上报进度就会更新。"""
    import threading
    from dubforge.domain.models import StepRun
    from dubforge.runner import StepContext

    uid = make_project()
    project = crud.get_project(uid)
    crud.save_step(StepRun(project_id=project.id, step_key="split", status="running"))
    ctx = StepContext(project, "split", pdir(uid), pdir(uid) / "logs" / "split.log", threading.Event())
    clock = [0.0]
    ctx.active_seconds = lambda: clock[0]
    ctx.report_eta(600)
    ctx.report_progress(60)
    assert crud.get_step(project.id, "split").eta_seconds == 600
    clock[0] = 100
    assert ctx.eta_seconds() == 500
    ctx.report_progress(60)
    assert crud.get_step(project.id, "split").eta_seconds == 500
    clock[0] = 700
    assert ctx.eta_seconds() == 0
    # API 读实时估算，不能继续返回数据库里旧的 500 秒。
    from app.api.steps import list_steps
    from fastapi import Response
    from dubforge.runner import runner
    monkeypatch.setitem(runner._ctxs, (project.id, "split"), ctx)
    visible = next(s for s in list_steps(uid, Response()) if s["key"] == "split")
    assert visible["eta_seconds"] == 0
    ctx.report_progress(30)
    assert crud.get_step(project.id, "split").progress == 60


def test_progress_without_measurements_has_no_eta(make_project, isolated_env, pdir):
    import threading
    from dubforge.domain.models import StepRun
    from dubforge.runner import StepContext

    uid = make_project()
    project = crud.get_project(uid)
    crud.save_step(StepRun(project_id=project.id, step_key="assemble"))
    ctx = StepContext(project, "assemble", pdir(uid), pdir(uid) / "logs" / "assemble.log", threading.Event())
    ctx._run_started -= 100
    ctx.report_progress(70)
    assert crud.get_step(project.id, "assemble").eta_seconds == 0


def test_llm_estimate_refreshes_on_step_and_project_apis_without_callbacks(make_project, pdir, monkeypatch):
    """无流式回调时两种页面都持续推进；实际下载进度不受时间估算影响。"""
    import threading

    from app.api.projects import _project_view
    from app.api.steps import list_steps
    from dubforge.domain.models import StepRun
    from dubforge.progress import LlmProgress
    from dubforge.runner import StepContext, runner
    from fastapi import Response

    uid = make_project()
    project = crud.get_project(uid)
    crud.save_step(StepRun(project_id=project.id, step_key="split", status="running"))
    ctx = StepContext(project, "split", pdir(uid), pdir(uid) / "logs" / "split.log", threading.Event())
    clock = [0.0]
    ctx.active_seconds = lambda: clock[0]
    progress = LlmProgress(ctx, [100], step="split", profile={"model": "test"})
    progress.begin(0)
    monkeypatch.setitem(runner._ctxs, (project.id, "split"), ctx)
    clock[0] = 90
    step = next(s for s in list_steps(uid, Response()) if s["key"] == "split")
    listed = next(s for s in _project_view(project)["steps"] if s["key"] == "split")
    assert step["progress"] == listed["progress"] > 1
    assert step["eta_seconds"] == listed["eta_seconds"] == 510
    assert step["progress_estimated"] and listed["progress_estimated"]
    assert crud.get_step(project.id, "split").progress == 1  # 轮询不写假工作量到数据库。
    clock[0] = 1200
    overdue = next(s for s in list_steps(uid, Response()) if s["key"] == "split")
    assert overdue["progress"] > step["progress"]
    assert overdue["estimate_overdue"] and overdue["eta_seconds"] == 0
    assert runner.progress_eta(project.id, "download")["progress_estimated"] is False


def test_active_clock_freezes_during_actual_pause(make_project, isolated_env, pdir):
    import threading
    import time
    import pytest
    from dubforge.runner import StepContext

    uid = make_project()
    ctx = StepContext(crud.get_project(uid), "split", pdir(uid), pdir(uid) / "logs" / "split.log", threading.Event())
    now = time.monotonic()
    ctx._run_started, ctx._paused_seconds = now - 100, 20
    ctx._pause_started = now - 30
    assert ctx.active_seconds() == pytest.approx(50, abs=0.1)
