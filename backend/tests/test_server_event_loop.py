"""Windows 服务循环容量回归；不启动服务器，不接触用户端口。"""

import asyncio
import json
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn

from app import event_loop


def test_windows_factory_does_not_follow_reload_selector(monkeypatch):
    expected = object()
    monkeypatch.setattr(event_loop, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(event_loop.asyncio, "ProactorEventLoop", lambda: expected, raising=False)
    monkeypatch.setattr(event_loop.asyncio, "new_event_loop", lambda: pytest.fail("Windows 需明确使用 Proactor"))
    assert event_loop.create_event_loop() is expected


def test_other_platforms_keep_native_event_loop(monkeypatch):
    expected = object()
    monkeypatch.setattr(event_loop, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(event_loop.asyncio, "new_event_loop", lambda: expected)
    assert event_loop.create_event_loop() is expected


@pytest.mark.skipif(sys.platform != "win32", reason="Windows IOCP 套接字容量")
@pytest.mark.parametrize("reload", [False, True])
def test_uvicorn_factory_handles_more_than_512_socket_reads(reload):
    """经过实际 Uvicorn 工厂解析，并发等待 600 个 socket，不受 select 上限影响。"""
    cfg = uvicorn.Config("app.main:app", reload=reload, loop="app.event_loop:create_event_loop")
    factory = cfg.get_loop_factory()
    loop = factory()
    assert isinstance(loop, asyncio.ProactorEventLoop)
    pairs = []

    async def read_all():
        for _ in range(600):
            reader, writer = socket.socketpair()
            reader.setblocking(False)
            writer.setblocking(False)
            pairs.append((reader, writer))
        # 先把全部读取提交到 IOCP，再逐个唤醒，确保同时存在超过 512 个等待者。
        reads = [asyncio.create_task(loop.sock_recv(reader, 1)) for reader, _ in pairs]
        await asyncio.sleep(0)
        for _, writer in pairs:
            await loop.sock_sendall(writer, b"x")
        assert await asyncio.wait_for(asyncio.gather(*reads), 10) == [b"x"] * 600

    try:
        loop.run_until_complete(read_all())
        loop.run_until_complete(loop.shutdown_asyncgens())
    finally:
        for reader, writer in pairs:
            reader.close()
            writer.close()
        loop.close()


@pytest.mark.skipif(sys.platform != "win32" or not shutil.which("powershell"), reason="Windows 启动器")
@pytest.mark.parametrize("reload", [False, True])
def test_development_start_and_restart_use_same_explicit_loop(tmp_path, reload):
    """执行真实控制台启动分派，但用进程替身避免启动或重启用户服务。"""
    repo = Path(__file__).resolve().parents[2]
    ps_path = "'" + str(repo / "scripts/dev-console.ps1").replace("'", "''") + "'"
    harness = tmp_path / "server-start.ps1"
    harness.write_text(f"""
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
. {ps_path}
function Get-NetTCPConnection {{ @() }}
function Start-DevService {{
    param($Title, $WorkingDirectory, $Command, $LogName, $Environment)
    [Console]::WriteLine('COMMAND=' + (ConvertTo-Json -Compress @($Command)))
    return [pscustomobject]@{{ Id=123; HasExited=$false }}
}}
function Wait-DevReady {{ param($Target, $BackendProcess, $FrontendProcess, $BackendPort, $FrontendPort, $BackendReused); return @{{ Backend=$true; Frontend=$true }} }}
$state = @{{ BackendProcess=$null; FrontendProcess=$null; BackendPort=8611; FrontendPort=15273; BackendReused=$false; ReloadBackend=${str(reload).lower()} }}
[Console]::WriteLine('INITIAL=' + (ConvertTo-Json -Compress @(Get-DevBackendCommand -Port 8611 -ReloadBackend:([bool]$state.ReloadBackend))))
Start-DevConsoleTarget -State $state -Target backend
""", encoding="utf-8-sig")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr
    initial = json.loads(next(line.split("=", 1)[1] for line in result.stdout.splitlines() if line.startswith("INITIAL=")))
    restarted = json.loads(next(line.split("=", 1)[1] for line in result.stdout.splitlines() if line.startswith("COMMAND=")))
    assert initial == restarted
    assert initial[initial.index("--loop") + 1] == "app.event_loop:create_event_loop"
    assert ("--reload" in initial) is reload
    assert "--limit-concurrency" not in initial
    assert initial[initial.index("--port") + 1] == "8611"
