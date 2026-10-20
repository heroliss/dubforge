"""开发服务启动生命周期回归测试。

这些测试只抽取 ``Start-DevService`` 函数，不执行 ``scripts/dev.ps1`` 的
主入口，因此不会探测、重启或停止用户正在使用的后端实例。
"""

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

import pytest


REPO = Path(__file__).resolve().parents[2]
DEV_SCRIPT = REPO / "scripts" / "dev.ps1"
DEV_OUTPUT = REPO / "scripts" / "dev-output.ps1"
DEV_WEB = REPO / "scripts" / "dev-web.ps1"
VITE_ENTRY = REPO / "frontend" / "node_modules" / "vite" / "dist" / "node" / "index.js"


def _ps_literal(value: Path | str) -> str:
    """返回可安全嵌入 Windows PowerShell 单引号字符串的字面量。"""

    return "'" + str(value).replace("'", "''") + "'"


def _powershell_available() -> bool:
    return sys.platform == "win32" and shutil.which("powershell") is not None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.skipif(not _powershell_available(), reason="仅在 Windows PowerShell 上运行")
def test_start_dev_service_only_adds_ci_for_hidden_frontend(tmp_path: Path):
    """CI 只属于隐藏前端；后端和调试可见前端保持原环境语义。"""

    # 通过 PowerShell AST 抽取函数，避免执行 dev.ps1 的端口探测和主入口。
    harness = tmp_path / "inspect-start-service.ps1"
    harness.write_text(
        f"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile({_ps_literal(DEV_SCRIPT)}, [ref]$tokens, [ref]$errors)
if ($errors.Count) {{ throw 'dev.ps1 语法错误' }}
$function = $ast.Find({{ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Start-DevService'
}}, $true)
if (-not $function) {{ throw '未找到 Start-DevService' }}
. ([scriptblock]::Create($function.Extent.Text))
. {_ps_literal(DEV_OUTPUT)}
. {_ps_literal(DEV_WEB)}
$LogDir = {_ps_literal(tmp_path)}
$script:captureIndex = 0
$script:capture = @()

# 可见分支不应真的创建窗口；替身只解码 Start-Process 的命令参数。
function Start-Process {{
    param(
        [switch]$PassThru,
        [string]$FilePath,
        [object[]]$ArgumentList,
        [object]$WindowStyle,
        [string]$RedirectStandardInput,
        [string]$RedirectStandardOutput,
        [string]$RedirectStandardError
    )
    $encodedIndex = [array]::IndexOf([object[]]$ArgumentList, '-EncodedCommand')
    if ($encodedIndex -lt 0) {{ throw 'Start-Process 未收到 EncodedCommand' }}
    $decoded = [Text.Encoding]::Unicode.GetString(
        [Convert]::FromBase64String([string]$ArgumentList[$encodedIndex + 1]))
    $path = Join-Path $LogDir ('capture-' + $script:captureIndex + '.txt')
    [IO.File]::WriteAllText($path, $decoded, [Text.UTF8Encoding]::new($false))
    $script:captureIndex++
    return [pscustomobject]@{{ Id = 100 + $script:captureIndex; HasExited = $true }}
}}

# 继承环境中故意放入哨兵值，确保测试的是“仅隐藏前端覆盖为 true”，
# 而不是测试机恰好没有 CI 变量。
$env:CI = 'caller-value'
$ShowServiceWindows = $false
[void](Start-DevService -Title 'backend-test' -WorkingDirectory $LogDir -Command @('cmd', '/c', 'exit', '0') -LogName 'backend')
[void](Start-DevService -Title 'frontend-hidden-test' -WorkingDirectory $LogDir -Command @('cmd', '/c', 'exit', '0') -LogName 'frontend')
$ShowServiceWindows = $true
[void](Start-DevService -Title 'frontend-visible-test' -WorkingDirectory $LogDir -Command @('cmd', '/c', 'exit', '0') -LogName 'frontend')
""",
        encoding="utf-8-sig",
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 0, result.stderr or result.stdout

    captures = sorted(tmp_path.glob("capture-*.txt"))
    assert len(captures) == 3, result.stdout
    backend_text, hidden_frontend_text, visible_frontend_text = (
        path.read_text(encoding="utf-8") for path in captures
    )
    ci_assignment = re.compile(r"\$env:CI\s*=\s*['\"]true['\"]", re.IGNORECASE)
    assert not ci_assignment.search(backend_text)
    assert ci_assignment.search(hidden_frontend_text), hidden_frontend_text
    assert not ci_assignment.search(visible_frontend_text), visible_frontend_text


@pytest.mark.skipif(
    not (_powershell_available() and shutil.which("node") and VITE_ENTRY.exists()),
    reason="需要 Windows PowerShell、Node.js 和已安装的 Vite",
)
def test_hidden_vite_service_survives_empty_stdin(tmp_path: Path):
    """真实 Vite 在空 stdin/EOF 后仍可提供 HTTP，并能由 stop 文件安全退出。"""

    port = _free_port()
    root = tmp_path / "vite-root"
    root.mkdir()
    (root / "index.html").write_text(
        "<!doctype html><html><body>DubForge EOF lifecycle test</body></html>",
        encoding="utf-8",
    )
    ready = tmp_path / "ready.json"
    eof = tmp_path / "stdin-eof"
    stop = tmp_path / "stop"
    child_pid = tmp_path / "child.pid"
    node_script = tmp_path / "vite-eof-test.mjs"
    node_script.write_text(
        """
import { existsSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const { createServer } = await import(pathToFileURL(process.env.TEST_VITE).href);
const server = await createServer({
  root: process.env.TEST_ROOT,
  logLevel: 'error',
  server: { host: '127.0.0.1', port: Number(process.env.TEST_PORT), strictPort: true },
});
await server.listen();
writeFileSync(process.env.TEST_READY, JSON.stringify({ ci: process.env.CI ?? null }));
process.stdin.on('end', () => writeFileSync(process.env.TEST_EOF, ''));
process.stdin.resume();
const timer = setInterval(async () => {
  if (existsSync(process.env.TEST_STOP)) {
    clearInterval(timer);
    await server.close();
    process.exit(0);
  }
}, 25);
""",
        encoding="utf-8",
    )

    harness = tmp_path / "run-vite-service.ps1"
    harness.write_text(
        f"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile({_ps_literal(DEV_SCRIPT)}, [ref]$tokens, [ref]$errors)
if ($errors.Count) {{ throw 'dev.ps1 语法错误' }}
$function = $ast.Find({{ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Start-DevService'
}}, $true)
if (-not $function) {{ throw '未找到 Start-DevService' }}
. ([scriptblock]::Create($function.Extent.Text))
. {_ps_literal(DEV_OUTPUT)}
. {_ps_literal(DEV_WEB)}
$LogDir = {_ps_literal(tmp_path)}
$ShowServiceWindows = $false
$child = Start-DevService -Title 'DubForge Frontend EOF test' -WorkingDirectory {_ps_literal(root)} `
    -Command @({_ps_literal(shutil.which('node') or 'node')}, {_ps_literal(node_script)}) -LogName 'frontend' `
    -Environment @{{ TEST_ROOT = {_ps_literal(root)}; TEST_PORT = {port}; TEST_READY = {_ps_literal(ready)}; `
        TEST_EOF = {_ps_literal(eof)}; TEST_STOP = {_ps_literal(stop)}; TEST_VITE = {_ps_literal(VITE_ENTRY)} }}
[IO.File]::WriteAllText({_ps_literal(child_pid)}, [string]$child.Id)
try {{
    while (-not (Test-Path -LiteralPath {_ps_literal(stop)}) -and -not $child.HasExited) {{
        Start-Sleep -Milliseconds 50
    }}
}} finally {{
    if (-not (Test-Path -LiteralPath {_ps_literal(stop)})) {{ [IO.File]::WriteAllText({_ps_literal(stop)}, '') }}
    if (-not $child.WaitForExit(5000)) {{ & taskkill /PID $child.Id /T /F | Out-Null }}
}}
if ($child.ExitCode -ne 0) {{ exit $child.ExitCode }}
""",
        encoding="utf-8-sig",
    )

    harness_process = subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        deadline = time.monotonic() + 12
        while not ready.exists() and time.monotonic() < deadline:
            if harness_process.poll() is not None:
                break
            time.sleep(0.05)
        assert ready.exists(), "Vite 未就绪\nstdout=" + (harness_process.stdout.read() if harness_process.stdout else "")
        state = json.loads(ready.read_text(encoding="utf-8"))
        assert state["ci"] == "true"

        # 空文件被重定向为 stdin；若 Vite/其包装器收到 EOF，仍应保持监听。
        eof_deadline = time.monotonic() + 8
        while not eof.exists() and time.monotonic() < eof_deadline:
            time.sleep(0.05)
        assert eof.exists(), "测试服务未观察到 stdin EOF"
        response = urlopen(f"http://127.0.0.1:{port}/", timeout=3)
        assert response.status == 200
        assert b"DubForge EOF lifecycle test" in response.read()
        assert harness_process.poll() is None
    finally:
        stop.write_text("", encoding="utf-8")
        try:
            stdout, stderr = harness_process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            harness_process.kill()
            stdout, stderr = harness_process.communicate(timeout=5)
        assert harness_process.returncode == 0, stderr or stdout


@pytest.mark.skipif(not _powershell_available(), reason="仅在 Windows PowerShell 上运行")
def test_start_dev_service_propagates_native_exit_code(tmp_path: Path):
    """后台服务失败时，wrapper 应记录原生子进程退出码。"""

    harness = tmp_path / "native-exit-test.ps1"
    harness.write_text(
        f"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile({_ps_literal(DEV_SCRIPT)}, [ref]$tokens, [ref]$errors)
if ($errors.Count) {{ throw 'dev.ps1 语法错误' }}
$function = $ast.Find({{ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Start-DevService'
}}, $true)
if (-not $function) {{ throw '未找到 Start-DevService' }}
. ([scriptblock]::Create($function.Extent.Text))
. {_ps_literal(DEV_OUTPUT)}
. {_ps_literal(DEV_WEB)}
$LogDir = {_ps_literal(tmp_path)}
$ShowServiceWindows = $false
$child = Start-DevService -Title 'native-exit-test' -WorkingDirectory {_ps_literal(tmp_path)} `
    -Command @({_ps_literal(sys.executable)}, '-c', 'import sys; sys.exit(7)') -LogName 'backend'
try {{
    if (-not $child.WaitForExit(10000)) {{ throw '退出码测试子进程未结束' }}
        $lifecycle = Join-Path {_ps_literal(tmp_path)} 'backend.lifecycle.log'
        if (-not (Test-Path -LiteralPath $lifecycle)) {{ throw '未生成服务生命周期日志' }}
        $record = Get-Content -LiteralPath $lifecycle -Raw
        if ($record -notmatch 'exit_code=7') {{ throw ('wrapper 未记录原生退出码：' + $record) }}
}} finally {{
    if (-not $child.HasExited) {{ & taskkill /PID $child.Id /T /F | Out-Null }}
}}
""",
        encoding="utf-8-sig",
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.skipif(not _powershell_available(), reason="仅在 Windows PowerShell 上运行")
def test_hidden_native_stderr_keeps_chinese_without_progress_clixml(tmp_path: Path):
    """真实隐藏 wrapper 不应把 PowerShell 模块加载进度混入原生错误日志。"""
    native = tmp_path / "native-stderr.py"
    native.write_text(
        "import sys\nsys.stderr.write('后端事件循环：ProactorEventLoop\\n')\n",
        encoding="utf-8",
    )
    harness = tmp_path / "native-stderr-test.ps1"
    harness.write_text(
        f"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile({_ps_literal(DEV_SCRIPT)}, [ref]$tokens, [ref]$errors)
if ($errors.Count) {{ throw 'dev.ps1 语法错误' }}
$function = $ast.Find({{ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Start-DevService'
}}, $true)
. ([scriptblock]::Create($function.Extent.Text))
. {_ps_literal(DEV_OUTPUT)}
. {_ps_literal(DEV_WEB)}
$LogDir = {_ps_literal(tmp_path)}
$ShowServiceWindows = $false
$child = Start-DevService -Title 'native-stderr-test' -WorkingDirectory $LogDir `
    -Command @({_ps_literal(sys.executable)}, {_ps_literal(native)}) -LogName 'backend' `
    -Environment @{{PYTHONIOENCODING='utf-8'}}
try {{
    if (-not $child.WaitForExit(10000)) {{ throw '错误日志测试子进程未结束' }}
}} finally {{
    if (-not $child.HasExited) {{ & taskkill /PID $child.Id /T /F | Out-Null }}
}}
""",
        encoding="utf-8-sig",
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    stderr = (tmp_path / "backend.err.log").read_text(encoding="utf-8-sig")
    assert stderr.strip() == "后端事件循环：ProactorEventLoop"
    assert "CLIXML" not in stderr
