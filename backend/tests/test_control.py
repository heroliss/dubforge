# -*- coding: utf-8 -*-
"""
运行控制测试: 暂停 / 停止 / 强制结束 / 半成品清理
================================================
对应 runner 的控制面改造:
- pause_step / check_pause: 协作式暂停 (循环间阻塞, 取消优先)
- run_subprocess: 子进程注册, 取消即整树终止 (ffmpeg/yt-dlp 可即时停)
- force_stop: 步骤立刻按「已强制结束」呈现; 进程内计算后台收尾后结果丢弃
- _sweep_temp_files: 取消/失败/强制结束后清扫过程临时文件
"""
import sys
import threading
import time

from conftest import mark_upstream_done, wait_status
from dubforge.domain import crud


def _runner():
    from dubforge.runner import runner
    return runner


def _patch_step(monkeypatch, key, fn):
    import dubforge.pipeline.steps as steps_mod
    monkeypatch.setitem(steps_mod._RUNNERS, key, fn)


def test_pause_blocks_and_resumes(make_project, isolated_env, monkeypatch):
    """暂停: 步骤在 check_pause 处阻塞 (计数停止增长), 继续后恢复执行。
    协作式语义: 发出暂停时正在进行的那个单元允许跑完 (计数至多多走一拍)。"""
    uid = make_project()
    mark_upstream_done(uid, "assemble")
    entered = threading.Event()
    stop = threading.Event()
    ticks = [0]

    def worker(ctx):
        entered.set()
        while not stop.is_set():
            ctx.check_pause()          # 暂停点
            ticks[0] += 1
            time.sleep(0.05)
        ctx.write_json("assemble_result.json", {"ticks": ticks[0]})

    _patch_step(monkeypatch, "assemble", worker)
    runner = _runner()
    try:
        runner.start_step(uid, "assemble")
        assert entered.wait(5)
        wait_status(uid, "assemble", {"running"})
        time.sleep(0.3)
        base = ticks[0]
        assert base > 0

        pid = crud.get_project(uid).id
        r = runner.pause_step(uid, "assemble", pause=True)
        assert r["status"] == "paused"
        assert runner.is_paused(pid, "assemble")
        time.sleep(0.8)                # 允许"当前单元"跑完
        frozen = ticks[0]
        assert frozen <= base + 1, "暂停后至多完成当前一个单元"
        time.sleep(0.4)
        assert ticks[0] == frozen, "暂停期间步骤不应继续执行"
        assert crud.get_step(pid, "assemble").status == "running"   # 暂停不改变状态
    finally:
        stop.set()                     # 失败也不能把 worker 永久留在暂停点
        runner.pause_step(uid, "assemble", pause=False)
    wait_status(uid, "assemble", {"done"})


def test_force_stop_immediate_and_discard(make_project, isolated_env, monkeypatch):
    """强制结束: 步骤立刻按已取消呈现; 后台收尾完成后结果丢弃 (不翻回 done)"""
    uid = make_project()
    mark_upstream_done(uid, "assemble")
    started = threading.Event()
    gate = threading.Event()

    def worker(ctx):                    # 模拟 demucs 这类不可腰斩的进程内计算
        started.set()
        gate.wait(10)
        ctx.write_json("assemble_result.json", {"step": "assemble"})  # 收尾写产物

    _patch_step(monkeypatch, "assemble", worker)
    runner = _runner()
    try:
        runner.start_step(uid, "assemble")
        assert started.wait(5)
        pid = crud.get_project(uid).id

        r = runner.force_stop(uid, "assemble")
        assert r["status"] == "force_stopped"
        step = crud.get_step(pid, "assemble")
        assert step.status == "failed"                    # 立刻生效, 不等后台
        assert step.error_code == "STEP_FORCE_STOPPED"
    finally:
        gate.set()                                        # 后台计算"跑完"
    deadline = time.time() + 10
    while time.time() < deadline and runner.is_cancelling(crud.get_project(uid).id,
                                                          "assemble"):
        time.sleep(0.05)
    step = crud.get_step(crud.get_project(uid).id, "assemble")
    assert step.status == "failed"                        # 收尾不覆盖状态
    assert step.error_code == "STEP_FORCE_STOPPED"

    # 强制结束后可直接重跑
    _patch_step(monkeypatch, "assemble",
                lambda ctx: ctx.write_json("assemble_result.json", {"ok": 1}))
    runner.start_step(uid, "assemble")
    wait_status(uid, "assemble", {"done"})


def test_cancel_kills_subprocess(make_project, isolated_env, monkeypatch):
    """取消即终止子进程: run_subprocess 里的外部命令不用等它自然结束"""
    uid = make_project()
    mark_upstream_done(uid, "assemble")
    started = threading.Event()

    def worker(ctx):
        started.set()
        rc, out, err = ctx.run_subprocess(
            [sys.executable, "-c", "import time; time.sleep(30)"])
        ctx.write_json("never.json", {"rc": rc})      # 正常路径不该走到

    _patch_step(monkeypatch, "assemble", worker)
    runner = _runner()
    try:
        runner.start_step(uid, "assemble")
        assert started.wait(5)
    finally:
        runner.cancel_step(uid, "assemble")               # 失败也要终止子进程
    status = wait_status(uid, "assemble", {"failed"}, timeout=8)   # 秒级退出
    assert status == "failed"
    step = crud.get_step(crud.get_project(uid).id, "assemble")
    assert step.error_code == "STEP_CANCELLED"


def test_sweep_temp_files(make_project, isolated_env, pdir):
    """清扫: 取消/失败/结束后删除过程临时文件, 不动正式产物"""
    from dubforge.runner import _sweep_temp_files
    uid = make_project()
    d = pdir(uid)
    keep = d / "artifacts" / "assemble" / "assemble_result.json"
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_text("{}", encoding="utf-8")
    temps = [d / "media" / "video.mp4.part",
             d / "audio" / ".tmp_src44100.wav",
             d / "artifacts" / "assemble" / "peaks_x.json.tmp",
             d / "media" / "video.webm.ytdl"]
    tmpdir = d / "audio" / ".demucs_tmp"
    tmpdir.mkdir(parents=True)
    for t in temps:
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_bytes(b"x")

    removed = _sweep_temp_files(d)
    names = set(removed)
    for t in temps:
        assert not t.exists(), f"未清理: {t.name}"
        assert str(t.relative_to(d)) in names
    assert not tmpdir.exists()
    assert keep.exists(), "正式产物不应被清扫"
    assert _sweep_temp_files(d) == []                  # 幂等


def test_sweep_runs_after_cancel(make_project, isolated_env, monkeypatch, pdir):
    """E2E: 步骤取消 → 执行器收尾时自动清扫临时文件"""
    uid = make_project()
    mark_upstream_done(uid, "assemble")
    started = threading.Event()

    def worker(ctx):
        started.set()
        (ctx.pdir / "artifacts" / "assemble").mkdir(parents=True, exist_ok=True)
        (ctx.pdir / "artifacts" / "assemble" / "half.json.tmp").write_bytes(b"x")
        for _ in range(50):
            ctx.check_cancel()
            time.sleep(0.1)

    _patch_step(monkeypatch, "assemble", worker)
    runner = _runner()
    runner.start_step(uid, "assemble")
    assert started.wait(5)
    runner.cancel_step(uid, "assemble")
    wait_status(uid, "assemble", {"failed"}, timeout=8)
    assert not (pdir(uid) / "artifacts" / "assemble" / "half.json.tmp").exists()


def test_steps_api_inputs_outputs_pausing(isolated_env):
    """清单 API: 每步返回上游文件/计划产物/暂停标记"""
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        r = client.post("/api/projects", json={"title": "清单项目"})
        uid = r.json()["uid"]
        steps = {s["key"]: s for s in
                 client.get(f"/api/projects/{uid}/steps").json()}
        assert steps["download"]["inputs"] == []
        assert steps["preprocess"]["inputs"][0] == {
            "step": "download", "name": "video.mp4",
            "desc": "源视频 (提取音轨与分离输入)"}
        outs = {o["name"] for o in steps["preprocess"]["outputs"]}
        assert {"raw.wav", "vocal.wav", "background.wav"} <= outs
        assert steps["assemble"]["pausing"] is False


def test_reveal_artifact_endpoint(isolated_env, monkeypatch):
    """单产物定位: 已登记产物 → reveal 收到真实路径; 未登记 → 400"""
    from fastapi.testclient import TestClient
    from app.main import app
    import dubforge.utils.reveal as reveal_mod
    calls: list[str] = []
    monkeypatch.setattr(reveal_mod, "reveal_in_explorer", lambda p: calls.append(p))

    with TestClient(app) as client:
        r = client.post("/api/projects", json={"title": "定位项目"})
        uid = r.json()["uid"]
        project = crud.get_project(uid)
        from dubforge.domain import crud as c2
        pdir = c2.project_dir(uid)
        f = pdir / "media" / "video.mp4"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
        c2.add_artifact(project.id, "download", "video", "video.mp4",
                        f, str(f.relative_to(pdir)))

        r = client.post(f"/api/projects/{uid}/steps/download/artifacts/video.mp4/reveal")
        assert r.status_code == 200
        assert calls and calls[0].endswith("video.mp4")

        r = client.post(f"/api/projects/{uid}/steps/download/artifacts/nope/reveal")
        assert r.status_code == 400
        assert r.json()["code"] == "ARTIFACT_NOT_FOUND"


def test_cancelling_flag_only_after_cancel(make_project, isolated_env, monkeypatch):
    """回归: 运行中 cancelling 必须为 False, 点了停止才为 True
    (曾把「取消事件已注册」误当「已置位」, 导致一运行就显示取消中)"""
    uid = make_project()
    mark_upstream_done(uid, "assemble")
    started = threading.Event()
    release = threading.Event()

    def worker(ctx):
        started.set()
        release.wait(10)
        ctx.check_cancel()                   # 唤醒后响应停止 → failed

    _patch_step(monkeypatch, "assemble", worker)
    runner = _runner()
    try:
        runner.start_step(uid, "assemble")
        assert started.wait(5)
        pid = crud.get_project(uid).id
        assert runner.is_cancelling(pid, "assemble") is False    # 运行中 ≠ 取消中
        assert runner.is_paused(pid, "assemble") is False
        runner.cancel_step(uid, "assemble")
        assert runner.is_cancelling(pid, "assemble") is True     # 置位后才是
    finally:
        release.set()
    wait_status(uid, "assemble", {"failed"}, timeout=8)
    assert runner.is_cancelling(crud.get_project(uid).id, "assemble") is False
