"""快捷启动只模拟进程，不加载用户的 GPU 模型。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from dubforge import config, tts_service
from dubforge.backends import tts_gptsovits
from dubforge.errors import ServiceStartError, ServiceStopError


@pytest.fixture()
def launcher(isolated_env, monkeypatch, tmp_path):
    monkeypatch.setattr(tts_service, "_processes", {})
    monkeypatch.setattr(tts_service, "_users", {})
    monkeypatch.setattr(tts_service, "_record_process", lambda *args: None)
    for module in tts_service._BACKENDS.values():
        monkeypatch.setattr(module, "is_online", lambda url: False)
    monkeypatch.setattr(tts_service.socket, "create_connection", Mock(side_effect=ConnectionRefusedError))
    process = Mock()
    process.pid = 23456
    process.poll.return_value = None
    spawn = Mock(return_value=process)
    monkeypatch.setattr(tts_service.subprocess, "Popen", spawn)
    root = tmp_path / "服务 目录"
    root.mkdir()
    for relative in ("webui.py", ".venv/Scripts/python.exe", "api_v2.py", "runtime/python.exe"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    values = {"tts.backend": "indextts25", "tts.indextts25.url": "http://localhost:17860/",
              "tts.indextts25.project_dir": str(root), "tts.gptsovits.project_dir": str(root),
              "tts.gptsovits.url": "http://127.0.0.1:19880/"}
    return values, spawn, process, root


def test_start_once_across_concurrent_requests_then_ready(launcher, monkeypatch):
    values, spawn, process, root = launcher
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(lambda _: tts_service.start(values.get), range(4)))
    assert all(reply["status"] == "starting" for reply in replies)
    spawn.assert_called_once()
    command = spawn.call_args.args[0]
    assert command == [str(root / ".venv/Scripts/python.exe"), "-u",
                       str(Path(tts_service.tts_indextts25.__file__).with_name('indextts_runner.py')), "webui.py",
                       "--host", "127.0.0.1", "--port", "17860"]
    assert spawn.call_args.kwargs["cwd"] == root
    assert Path(replies[0]["log_path"]).is_file()
    assert not spawn.call_args.kwargs.get("shell")
    monkeypatch.setattr(tts_service.tts_indextts25, "is_online", lambda url: True)
    assert tts_service.status(values.get)["status"] == "online"
    assert tts_service.start(values.get)["online"]
    assert spawn.call_count == 1


def test_online_managed_service_keeps_stop_and_log_controls(launcher, monkeypatch):
    values, _, process, _ = launcher
    started = tts_service.start(values.get)
    monkeypatch.setattr(tts_service.tts_indextts25, "is_online", lambda url: True)
    state = tts_service.status(values.get)
    assert state["managed"] and state["can_stop"]
    assert state["pid"] == process.pid and state["log_path"] == started["log_path"]


@pytest.mark.parametrize('bridge', [False, True])
def test_managed_old_launch_is_distinguished_from_iteration_progress(launcher, monkeypatch, bridge):
    values, _, process, _ = launcher
    tts_service.start(values.get)
    process.args = ['python', 'indextts_runner.py', 'webui.py'] if bridge else ['python', 'webui.py']
    monkeypatch.setattr(tts_service.tts_indextts25, 'is_online', lambda url: True)
    assert tts_service.status(values.get)['progress_detail'] is bridge


def test_stop_during_startup_and_restart(launcher, monkeypatch):
    values, spawn, process, _ = launcher
    tts_service.start(values.get)
    kill = Mock(side_effect=lambda p: setattr(p.poll, "return_value", 0))
    monkeypatch.setattr(tts_service, "_terminate_tree", kill)
    stopped = tts_service.stop(values.get)
    assert stopped["status"] == "offline" and stopped["can_start"]
    assert not stopped["can_stop"] and not stopped["managed"]
    assert "已停止配音服务" in tts_service.read_log(values.get)["text"]
    kill.assert_called_once_with(process)
    tts_service.start(values.get)
    assert spawn.call_count == 2


def test_stop_refuses_external_remote_or_other_backend(launcher, monkeypatch):
    values, _, _, _ = launcher
    kill = Mock()
    monkeypatch.setattr(tts_service, "_terminate_tree", kill)
    with pytest.raises(ServiceStopError, match="不是由 DubForge"):
        tts_service.stop(values.get)
    tts_service.start(values.get)
    values["tts.backend"] = "gptsovits"
    values["tts.gptsovits.url"] = values["tts.indextts25.url"]
    with pytest.raises(ServiceStopError, match="不是由 DubForge"):
        tts_service.stop(values.get)
    values["tts.gptsovits.url"] = "http://192.168.1.2:9880"
    with pytest.raises(ServiceStopError, match="服务所在机器"):
        tts_service.stop(values.get)
    kill.assert_not_called()


def test_busy_service_protected_across_projects_and_cleanup_on_error(launcher, monkeypatch):
    values, _, _, _ = launcher
    tts_service.start(values.get)
    alias = {**values, "tts.indextts25.url": "http://127.0.0.1:17860"}
    kill = Mock()
    monkeypatch.setattr(tts_service, "_terminate_tree", kill)
    with pytest.raises(RuntimeError):
        with tts_service.use(alias.get):
            assert not tts_service.status(values.get)["can_stop"]
            with pytest.raises(ServiceStopError, match="正在配音"):
                tts_service.stop(values.get)
            raise RuntimeError("模拟合成失败")
    assert tts_service.status(values.get)["can_stop"]
    kill.assert_not_called()


def test_log_api_returns_only_bounded_tail_and_stop_uses_project_settings(launcher, make_project, monkeypatch):
    from app.main import app
    values, _, process, _ = launcher
    uid = make_project(params={"step_params": values})
    client = TestClient(app)
    base = f"/api/projects/{uid}/workbench/service"
    start = client.post(base + "/start").json()
    Path(start["log_path"]).write_text("a" * 100000 + "最新日志", encoding="utf-8")
    log = client.get(base + "/log").json()
    assert log["truncated"] and log["text"].endswith("最新日志")
    assert len(log["text"].encode()) <= 64 * 1024
    monkeypatch.setattr(tts_service, "_terminate_tree", lambda p: setattr(p.poll, "return_value", 0))
    assert client.post(base + "/stop").json()["status"] == "offline"
    assert client.post(base + "/stop").json()["code"] == "SERVICE_STOP_FAILED"
    assert client.get("/api/projects/missing/workbench/service/log").json()["code"] == "PROJECT_NOT_FOUND"


@pytest.mark.parametrize("mismatch", ["created", "command", "executable"])
def test_restart_never_adopts_reused_pid_or_changed_command(isolated_env, monkeypatch, mismatch):
    import json
    monkeypatch.setattr(tts_service, "_processes", {})
    live = Mock(pid=12345)
    live.create_time.return_value = 100.0
    live.cmdline.return_value = ["python", "webui.py"]
    live.exe.return_value = "python"
    monkeypatch.setattr(tts_service.psutil, "Process", lambda pid: live)
    record = {"backend": "indextts25", "pid": 12345, "created": 100.0,
              "command": ["python", "webui.py"], "executable": "python"}
    record[mismatch] = "已改变"
    path = tts_service._record_path(17860)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    assert tts_service._managed_process(17860) is None


def test_real_process_tree_can_be_stopped_after_manager_restart(isolated_env, monkeypatch, tmp_path):
    """仅启动自建的休眠进程，验证 Windows Python 子进程随服务一起退出。"""
    import subprocess
    import sys
    import time
    import psutil
    monkeypatch.setattr(tts_service, "_processes", {})
    monkeypatch.setattr(tts_service.tts_indextts25, "is_online", lambda url: False)
    script = tmp_path / "fake_service.py"
    ready = tmp_path / "ready"
    script.write_text("import subprocess, sys, time\nfrom pathlib import Path\n"
                      "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                      "ready = Path(sys.argv[1]); temp = ready.with_suffix('.tmp')\n"
                      "temp.write_text(str(child.pid)); temp.replace(ready)\ntime.sleep(60)\n", encoding="utf-8")
    process = subprocess.Popen([sys.executable, str(script), str(ready)],
                               creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    child = None
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        child = psutil.Process(int(ready.read_text()))
        tts_service._record_process("indextts25", 17860, process)
        restored = tts_service._managed_process(17860)
        assert restored[1].pid == process.pid and restored[1].poll() is None
        values = {"tts.backend": "indextts25", "tts.indextts25.url": "http://localhost:17860",
                  "tts.indextts25.project_dir": str(tmp_path)}
        stopped = tts_service.stop(values.get)
        assert not stopped["managed"] and not child.is_running()
        process.wait(timeout=2)
        assert not tts_service._record_path(17860).exists()
    finally:
        if child and child.is_running():
            child.kill()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)


def test_existing_service_is_reused_without_installation(launcher, monkeypatch):
    values, spawn, _, _ = launcher
    values["tts.indextts25.project_dir"] = "missing"
    monkeypatch.setattr(tts_service.tts_indextts25, "is_online", lambda url: True)
    assert tts_service.start(values.get)["online"]
    spawn.assert_not_called()


def test_text_emotion_starts_with_required_model(launcher):
    values, spawn, _, _ = launcher
    values["tts.indextts25.emo_mode"] = "text"
    tts_service.start(values.get)
    assert spawn.call_args.args[0][-1] == "--qwen_emo"


@pytest.mark.parametrize("url", ["http://192.168.1.2:7860", "https://localhost:7860",
                                    "http://localhost:7860/proxy", "http://localhost:0", "invalid"])
def test_nonlocal_or_invalid_address_cannot_launch(launcher, url):
    values, spawn, _, _ = launcher
    values["tts.indextts25.url"] = url
    assert not tts_service.status(values.get)["can_start"]
    with pytest.raises(ServiceStartError):
        tts_service.start(values.get)
    spawn.assert_not_called()


def test_missing_installation_or_runtime_explained(launcher):
    values, spawn, _, root = launcher
    (root / ".venv/Scripts/python.exe").unlink()
    assert "Python" in tts_service.status(values.get)["message"]
    values["tts.indextts25.project_dir"] = str(root / "不存在")
    assert "安装目录" in tts_service.status(values.get)["message"]
    spawn.assert_not_called()


def test_exited_process_reports_log_and_allows_retry(launcher):
    values, spawn, process, _ = launcher
    started = tts_service.start(values.get)
    process.poll.return_value = 1
    result = tts_service.status(values.get)
    assert result["status"] == "failed" and result["can_start"]
    assert result["log_path"] == started["log_path"]
    assert "退出码 1" in result["message"]
    tts_service.start(values.get)
    assert spawn.call_count == 2


def test_spawn_error_is_domain_error(launcher):
    values, spawn, _, _ = launcher
    spawn.side_effect = PermissionError("拒绝访问")
    with pytest.raises(ServiceStartError, match="拒绝访问") as exc:
        tts_service.start(values.get)
    assert exc.value.detail["log_path"]


def test_occupied_port_does_not_spawn_another_model(launcher, monkeypatch):
    values, spawn, _, _ = launcher
    from unittest.mock import MagicMock
    monkeypatch.setattr(tts_service.socket, "create_connection", MagicMock())
    with pytest.raises(ServiceStartError, match="已被占用"):
        tts_service.start(values.get)
    spawn.assert_not_called()


def test_autodiscovery_and_ambiguous_installations(launcher, tmp_path, monkeypatch):
    values, _, _, root = launcher
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "DubForge")
    found = root.rename(tmp_path / "index-tts")
    values["tts.indextts25.project_dir"] = ""
    assert tts_service._launch_settings(values.get)[3] == found
    values["tts.backend"] = "gptsovits"
    values["tts.gptsovits.project_dir"] = ""
    found.rename(tmp_path / "GPT-SoVITS-v2")
    assert tts_service.status(values.get)["can_start"]
    another = tmp_path / "VideoLingo-Workspace/GPT-SoVITS-v3/api_v2.py"
    another.parent.mkdir(parents=True)
    another.touch()
    assert "多个" in tts_service.status(values.get)["message"]


def test_api_uses_saved_project_backend_and_port(launcher, make_project):
    values, spawn, _, root = launcher
    from app.main import app
    values["tts.backend"] = "gptsovits"
    uid = make_project(params={"step_params": values})
    client = TestClient(app)
    reply = client.post(f"/api/projects/{uid}/workbench/service/start")
    assert reply.status_code == 200
    assert reply.json()["backend"] == "gptsovits"
    assert spawn.call_args.args[0] == [str(root / "runtime/python.exe"), "-u", "api_v2.py",
                                      "-a", "127.0.0.1", "-p", "19880"]
    assert client.get(f"/api/projects/{uid}/workbench/service").json()["status"] == "starting"
    assert client.post("/api/projects/missing/workbench/service/start").json()["code"] == "PROJECT_NOT_FOUND"
    assert spawn.call_count == 1


def test_installation_path_does_not_invalidate_audio(launcher, make_project):
    from dubforge.domain import crud
    from dubforge.pipeline.step_tts import line_fingerprint
    from dubforge.runner import compute_fingerprint
    values, _, _, _ = launcher
    uid = make_project(params={"step_params": values})
    project = crud.get_project(uid)
    line = {"id": 1, "start": 0, "end": 1, "text": "你好"}
    step_before = compute_fingerprint(project, "tts")
    line_before = line_fingerprint(project, line)
    values["tts.indextts25.project_dir"] = "另一个目录"
    values["tts.gptsovits.project_dir"] = "另一个目录"
    project.params = __import__("json").dumps({"step_params": values})
    assert compute_fingerprint(project, "tts") == step_before
    assert line_fingerprint(project, line) == line_before


@pytest.mark.parametrize("code,body,expected", [
    (400, {"message": "ref_audio_path is required"}, True),
    (400, {"message": "text is required"}, True),
    (400, {"message": "unrelated error"}, False),
    (500, {}, False), (405, {}, True),
])
def test_gptsovits_readiness_accepts_its_validation_response(monkeypatch, code, body, expected):
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(code, json=body)))
    monkeypatch.setattr(tts_gptsovits.httpx, "Client", lambda **kwargs: client)
    assert tts_gptsovits.is_online("http://localhost:9880") is expected
