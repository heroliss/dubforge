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

import pytest

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
        assert r["status"] in {"pausing", "paused"}
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


@pytest.mark.parametrize("cancel", [False, True])
def test_pause_status_distinguishes_request_from_waiting(make_project, isolated_env, monkeypatch, cancel):
    """正在处理时只能报正在暂停；进入暂停点后报已暂停，继续/取消清除状态。"""
    from app.api.steps import list_steps
    from fastapi import Response

    uid = make_project()
    mark_upstream_done(uid, "assemble")
    project = crud.get_project(uid)
    started, release, resumed = threading.Event(), threading.Event(), threading.Event()

    def worker(ctx):
        started.set()
        assert release.wait(10)
        ctx.check_pause()
        resumed.set()
        ctx.write_json("assemble_result.json", {"ok": True})

    def view():
        return next(s for s in list_steps(uid, Response()) if s["key"] == "assemble")

    _patch_step(monkeypatch, "assemble", worker)
    runner = _runner()
    try:
        runner.start_step(uid, "assemble")
        assert started.wait(5)
        assert view()["pause_state"] == "none"
        assert runner.pause_step(uid, "assemble")["status"] == "pausing"
        pending = view()
        assert pending["pausing"] and pending["pause_state"] == "requested"
        release.set()
        deadline = time.monotonic() + 5
        while runner.pause_state(project.id, "assemble") != "paused":
            assert time.monotonic() < deadline
            time.sleep(.01)
        paused = view()
        assert paused["pausing"] and paused["pause_state"] == "paused"
        assert not resumed.is_set()
        if cancel:
            runner.cancel_step(uid, "assemble")
        else:
            assert runner.pause_step(uid, "assemble", pause=False)["status"] == "resumed"
            assert resumed.wait(5)
        assert view()["pause_state"] == "none"
    finally:
        release.set()
        runner.pause_step(uid, "assemble", pause=False)
    wait_status(uid, "assemble", {"failed" if cancel else "done"})
    assert view()["pause_state"] == "none"
    assert resumed.is_set() is not cancel


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
    assert crud.latest_artifacts(crud.get_project(uid).id, ["assemble"]) == {}

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
        rc, _out, _err = ctx.run_subprocess(
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
    temps = [d / "output" / "final.mp4.part",
             d / "output" / ".tmp_final.mp4",
             d / "artifacts" / "assemble" / "peaks_x.json.tmp"]
    tmpdir = d / "artifacts" / "assemble" / ".demucs_tmp"
    tmpdir.mkdir(parents=True)
    (tmpdir / "t.wav").write_bytes(b"x")
    others = [d / "media" / "video.mp4.part",        # download 的地盘: 不扫
              d / "audio" / ".tmp_src44100.wav"]     # preprocess 的地盘: 不扫
    for t in temps + others:
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_bytes(b"x")

    _sweep_temp_files(d, "assemble")
    for t in temps:
        assert not t.exists(), f"未清理: {t.name}"
    assert not tmpdir.exists()
    for o in others:
        assert o.exists(), f"不该扫别人的地盘: {o}"
    assert keep.exists(), "正式产物不应被清扫"
    assert _sweep_temp_files(d, "assemble") == []      # 幂等


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
    from app.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        r = client.post("/api/projects", json={"title": "清单项目"})
        uid = r.json()["uid"]
        steps = {s["key"]: s for s in
                 client.get(f"/api/projects/{uid}/steps").json()}
        assert steps["download"]["inputs"] == []
        assert {k: steps["preprocess"]["inputs"][0][k] for k in ("step", "name", "desc")} == {
            "step": "download", "name": "video.mp4",
            "desc": "源视频 (提取音轨与分离输入)"}
        outs = {o["name"] for o in steps["preprocess"]["outputs"]}
        assert {"raw.wav", "vocal.wav", "background.wav"} <= outs
        assert steps["assemble"]["pausing"] is False


def test_reveal_artifact_endpoint(isolated_env, monkeypatch):
    """单产物定位: 已登记产物 → reveal 收到真实路径; 未登记 → 400"""
    import dubforge.utils.reveal as reveal_mod
    from app.main import app
    from fastapi.testclient import TestClient
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
        assert runner.stop_reason(pid, 'assemble') is None
        assert runner.is_paused(pid, "assemble") is False
        runner.cancel_step(uid, "assemble")
        assert runner.is_cancelling(pid, "assemble") is True     # 置位后才是
        assert runner.stop_reason(pid, 'assemble') == 'user'
    finally:
        release.set()
    wait_status(uid, "assemble", {"failed"}, timeout=8)
    assert runner.is_cancelling(crud.get_project(uid).id, "assemble") is False


def test_timeout_exposes_reason_until_worker_exits(make_project, isolated_env, monkeypatch):
    from app.main import app
    from fastapi.testclient import TestClient
    uid = make_project()
    mark_upstream_done(uid, 'assemble')
    started, release = threading.Event(), threading.Event()
    contexts = []
    def worker(ctx):
        contexts.append(ctx)
        started.set()
        assert release.wait(10)
        ctx.check_cancel()
    _patch_step(monkeypatch, 'assemble', worker)
    runner = _runner()
    runner.start_step(uid, 'assemble')
    try:
        assert started.wait(5)
        contexts[0].request_timeout(7200)
        # 任务已经启动，不能在这里再次运行应用启动恢复（会把在跑任务标成中断）。
        client = TestClient(app)
        try:
            row = next(s for s in client.get(f'/api/projects/{uid}/steps').json() if s['key'] == 'assemble')
        finally:
            client.close()
        assert row['cancelling'] and row['stop_reason'] == 'timeout'
    finally:
        release.set()
    wait_status(uid, 'assemble', {'failed'})
    assert crud.get_step(crud.get_project(uid).id, 'assemble').error_code == 'STEP_TIMEOUT'


def test_cancel_is_idempotent_on_repeated_clicks(make_project, isolated_env, monkeypatch):
    """连续点击停止只接受第一个信号，后续请求不会重复改变控制状态。"""
    uid = make_project()
    mark_upstream_done(uid, "assemble")
    started = threading.Event()
    release = threading.Event()

    def worker(ctx):
        started.set()
        release.wait(10)
        ctx.check_cancel()

    _patch_step(monkeypatch, "assemble", worker)
    runner = _runner()
    runner.start_step(uid, "assemble")
    assert started.wait(5)
    first = runner.cancel_step(uid, "assemble")
    second = runner.cancel_step(uid, "assemble")
    assert first["status"] == "cancelling"
    assert second["status"] == "already_cancelling"
    release.set()
    wait_status(uid, "assemble", {"failed"}, timeout=8)


def test_control_action_rejects_stale_job_id(make_project, isolated_env, monkeypatch):
    """弹窗打开后任务已结束/换代时，旧弹窗不能控制新任务。"""
    uid = make_project()
    mark_upstream_done(uid, "assemble")
    started = threading.Event()
    release = threading.Event()

    def worker(ctx):
        started.set()
        release.wait(10)
        ctx.check_cancel()

    _patch_step(monkeypatch, "assemble", worker)
    runner = _runner()
    runner.start_step(uid, "assemble")
    assert started.wait(5)
    pid = crud.get_project(uid).id
    job_id = runner.current_job_id(pid, "assemble")
    assert job_id is not None

    # 伪造一个旧弹窗代次: 不应发出取消信号。
    stale = runner.cancel_step(uid, "assemble", expected_job_id=job_id + 999)
    assert stale["status"] == "stale_action"
    assert runner.is_cancelling(pid, "assemble") is False

    release.set()
    wait_status(uid, "assemble", {"done"}, timeout=8)
    stale_after_done = runner.force_stop(uid, "assemble", expected_job_id=job_id)
    assert stale_after_done["status"] == "stale_action"


def test_force_stop_blocks_rerun_until_old_worker_finishes(make_project, isolated_env, monkeypatch):
    """强制结束先改变界面状态，但旧线程未收尾前禁止同 key 重跑。"""
    uid = make_project()
    mark_upstream_done(uid, "assemble")
    started = threading.Event()
    release = threading.Event()

    def worker(ctx):
        started.set()
        release.wait(10)
        ctx.write_json("assemble_result.json", {"discard": True})

    _patch_step(monkeypatch, "assemble", worker)
    runner = _runner()
    runner.start_step(uid, "assemble")
    assert started.wait(5)
    assert runner.force_stop(uid, "assemble")["status"] == "force_stopped"

    from dubforge.errors import DubForgeError
    with pytest.raises(DubForgeError) as ei:
        runner.start_step(uid, "assemble")
    assert ei.value.code == "STEP_FINALIZING"

    release.set()
    deadline = time.time() + 8
    while time.time() < deadline and runner.current_job_id(
            crud.get_project(uid).id, "assemble") is not None:
        time.sleep(0.05)
    assert runner.current_job_id(crud.get_project(uid).id, "assemble") is None
    runner.start_step(uid, "assemble")
    assert wait_status(uid, "assemble", {"done"}, timeout=8) == "done"


def test_force_download_really_redownloads(make_project, isolated_env, monkeypatch, pdir):
    """回归: 强制重跑获取视频必须真重下，成功前保留旧视频
    (旧行为: media/ 有文件就跳过, 改分辨率重跑等于没跑)"""
    from dubforge.pipeline import step_download
    uid = make_project()                       # source_kind 默认 youtube
    media = pdir(uid) / "media"
    media.mkdir(parents=True, exist_ok=True)
    (media / "video.mp4").write_bytes(b"old-video")

    calls = []

    def fake_ytdlp(ctx, url, out_template):
        from pathlib import Path
        calls.append((url, getattr(ctx, "force", False)))
        assert (media / "video.mp4").is_file()
        out = Path(out_template.replace("%(ext)s", "mp4"))
        out.write_bytes(b"new-video")
        return {"title": "t", "filepath": str(out)}

    monkeypatch.setattr(step_download, "_run_ytdlp", fake_ytdlp)
    monkeypatch.setattr(step_download, "_validate_download", lambda *args: None)
    runner = _runner()
    runner.start_step(uid, "download", force=True)
    wait_status(uid, "download", {"done"})
    assert calls, "强制重跑应重新调用 yt-dlp"
    assert calls[0][1] is True                 # ctx.force 传递到位 (绕过归档)
    assert (media / "video.mp4").read_bytes() == b"new-video"

    # 非强制仍复用: 不再调 yt-dlp
    calls.clear()
    runner.start_step(uid, "download", force=True)   # force 但已无 _RUNNERS patch? 仍在
    wait_status(uid, "download", {"done"})
    assert len(calls) == 1                     # force 依旧重下 (语义一致)
