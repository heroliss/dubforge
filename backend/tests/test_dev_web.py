"""启动器验证网页与代理；浏览器打开失败不得终止已经就绪的服务。"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or not shutil.which("powershell"), reason="Windows 启动器")


def _run(tmp_path, body):
    repo = Path(__file__).resolve().parents[2]
    literal = lambda path: "'" + str(path).replace("'", "''") + "'"
    harness = tmp_path / "dev-web-test.ps1"
    harness.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n"
        f". {literal(repo / 'scripts/dev-output.ps1')}\n"
        f". {literal(repo / 'scripts/dev-web.ps1')}\n"
        f"$LogDir = {literal(tmp_path)}\n" + body,
        encoding="utf-8-sig")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize("page,app,expected", [
    ("DubForge", "DubForge", True),
    ("Other app", "DubForge", False),
    ("DubForge", "Other app", False),
    ("DubForge", "unavailable", False),
])
def test_frontend_needs_correct_page_and_live_api_proxy(tmp_path, page, app, expected):
    body = f"""
function Invoke-WebRequest {{ param($Uri, [switch]$UseBasicParsing, $TimeoutSec)
    return @{{StatusCode=200;Content='{page}'}}
}}
function Invoke-RestMethod {{ param($Uri, $TimeoutSec)
    if ('{app}' -eq 'unavailable') {{ throw 'connection refused' }}
    return @{{ok=$true;app='{app}'}}
}}
[Console]::WriteLine('READY=' + (Test-DevFrontend -Port 15273))
"""
    assert f"READY={expected}" in _run(tmp_path, body)


def test_browser_failure_is_explained_without_throwing_or_stopping_services(tmp_path):
    output = _run(tmp_path, """
function Test-DevFrontend { param($Port) return $true }
function Start-Process { param($FilePath) throw 'default browser unavailable' }
$opened = Open-DevPage -BackendPort 8611 -FrontendPort 15273
[Console]::WriteLine('OPENED=' + $opened)
[Console]::WriteLine('CONTROL_PANEL_CONTINUES')
""")
    assert "OPENED=False" in output and "CONTROL_PANEL_CONTINUES" in output
    assert "服务仍在运行" in output and "http://127.0.0.1:15273/" in output
    assert "保持此控制台运行" in output
    assert "默认浏览器启动失败，服务保留" in (tmp_path / "launcher.log").read_text(encoding="utf-8")


def test_open_does_not_launch_browser_before_proxy_is_ready(tmp_path):
    output = _run(tmp_path, """
function Test-DevFrontend { param($Port) return $false }
function Test-DevBackend { param($Port) return $false }
function Start-Process { throw 'must not open browser' }
[Console]::WriteLine('OPENED=' + (Open-DevPage -BackendPort 8611 -FrontendPort 15273))
""")
    assert "OPENED=False" in output and "尚未就绪" in output


def test_open_prefers_healthy_development_page_without_checking_fallback(tmp_path):
    output = _run(tmp_path, """
function Test-DevFrontend { param($Port) return $true }
function Get-DevBuiltPage { throw 'must not inspect fallback while frontend is ready' }
function Start-Process { param($FilePath) [Console]::WriteLine('BROWSER=' + $FilePath) }
[Console]::WriteLine('OPENED=' + (Open-DevPage -BackendPort 8611 -FrontendPort 15273))
""")
    assert "BROWSER=http://127.0.0.1:15273/" in output
    assert "OPENED=True" in output
    assert "改用后端" not in output


def test_open_uses_built_backend_only_after_checking_html_and_all_entry_assets(tmp_path):
    output = _run(tmp_path, """
function Test-DevFrontend { param($Port) return $false }
function Test-DevBackend { param($Port) return ($Port -eq 8611) }
function Invoke-WebRequest { param($Uri, $Method, [switch]$UseBasicParsing, $TimeoutSec)
    [Console]::WriteLine('CHECK=' + $Uri)
    if ($Uri -eq 'http://127.0.0.1:8611/') {
        return @{StatusCode=200;Content='<html><title>DubForge · 工作台</title><script type="module" src="/assets/index.js"></script><link rel="stylesheet" href="/assets/index.css"></html>'}
    }
    if ($Method -ne 'Head') { throw 'asset probe must not download the full bundle' }
    if ($Uri -eq 'http://127.0.0.1:8611/assets/index.js') {
        return @{StatusCode=200;Headers=@{'Content-Type'='application/javascript'}}
    }
    if ($Uri -eq 'http://127.0.0.1:8611/assets/index.css') {
        return @{StatusCode=200;Headers=@{'Content-Type'='text/css; charset=utf-8'}}
    }
    throw 'unexpected resource'
}
function Start-Process { param($FilePath) [Console]::WriteLine('BROWSER=' + $FilePath) }
[Console]::WriteLine('OPENED=' + (Open-DevPage -BackendPort 8611 -FrontendPort 15273))
""")
    assert "CHECK=http://127.0.0.1:8611/assets/index.js" in output
    assert "CHECK=http://127.0.0.1:8611/assets/index.css" in output
    assert "BROWSER=http://127.0.0.1:8611/" in output and "OPENED=True" in output
    assert "已验证并改用后端托管" in output and "制作任务保持运行" in output
    assert "使用已验证的后端构建页面" in (tmp_path / "launcher.log").read_text(encoding="utf-8")


@pytest.mark.parametrize("scenario", ["api-only", "no-assets", "missing-js", "html-instead-js", "missing-css"])
def test_api_health_or_invalid_built_assets_never_open_a_broken_fallback(tmp_path, scenario):
    output = _run(tmp_path, """
$scenario = '""" + scenario + """'
function Test-DevFrontend { param($Port) return $false }
function Test-DevBackend { param($Port) return $true }
function Invoke-WebRequest { param($Uri, $Method, [switch]$UseBasicParsing, $TimeoutSec)
    if ($Uri -eq 'http://127.0.0.1:8611/') {
        if ($scenario -eq 'api-only') { return @{StatusCode=200;Content='{"app":"DubForge"}'} }
        if ($scenario -eq 'no-assets') { return @{StatusCode=200;Content='<html><title>DubForge</title></html>'} }
        return @{StatusCode=200;Content='<html><title>DubForge</title><script type="module" src="/assets/index.js"></script><link rel="stylesheet" href="/assets/index.css"></html>'}
    }
    if ($Uri -match '\\.css$') {
        if ($scenario -eq 'missing-css') { throw 'resource missing' }
        return @{StatusCode=200;Headers=@{'Content-Type'='text/css'}}
    }
    if ($scenario -eq 'missing-js') { return @{StatusCode=404;Headers=@{'Content-Type'='application/javascript'}} }
    if ($scenario -eq 'html-instead-js') { return @{StatusCode=200;Headers=@{'Content-Type'='text/html'}} }
    return @{StatusCode=200;Headers=@{'Content-Type'='application/javascript'}}
}
function Start-Process { throw 'must not open broken fallback' }
[Console]::WriteLine('OPENED=' + (Open-DevPage -BackendPort 8611 -FrontendPort 15273))
""")
    assert "OPENED=False" in output
    assert "备用页面也不可用" in output


def test_reused_backend_must_still_pass_health_check(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    path = "'" + str(repo / "scripts/dev.ps1").replace("'", "''") + "'"
    output = _run(tmp_path, f"""
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile({path}, [ref]$tokens, [ref]$errors)
if ($errors.Count) {{ throw '启动脚本语法错误' }}
$function = $ast.Find({{param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Wait-DevReady'}}, $true)
. ([scriptblock]::Create($function.Extent.Text))
function Test-DevBackend {{ param($Port) return $false }}
function Test-DevFrontend {{ param($Port) return $true }}
function Show-DevLogs {{ param($LogDir, $Target) }}
$ready = Wait-DevReady -BackendPort 8611 -FrontendPort 15273 -BackendReused -FrontendProcess @{{HasExited=$true}}
[Console]::WriteLine('BACKEND=' + $ready.Backend)
""")
    assert "BACKEND=False" in output


def _dev_function(name):
    """只加载指定函数，避免执行启动器主入口或触碰用户实例。"""
    repo = Path(__file__).resolve().parents[2]
    path = "'" + str(repo / "scripts/dev.ps1").replace("'", "''") + "'"
    return f"""
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile({path}, [ref]$tokens, [ref]$errors)
if ($errors.Count) {{ throw '启动脚本语法错误' }}
$function = $ast.Find({{param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq '{name}'}}, $true)
if (-not $function) {{ throw '未找到 {name}' }}
. ([scriptblock]::Create($function.Extent.Text))
"""


def test_readiness_allows_slow_startup_with_configured_budget(tmp_path):
    output = _run(tmp_path, _dev_function("Wait-DevReady") + """
$script:loading = [Diagnostics.Stopwatch]::StartNew()
function Test-DevBackend { param($Port) return ($script:loading.Elapsed.TotalSeconds -ge 1.2) }
function Test-DevFrontend { param($Port, [switch]$SkipApi) return ($script:loading.Elapsed.TotalSeconds -ge 1.2) }
function Show-DevLogs { param($LogDir, $Target) }
$alive = @{HasExited=$false}
$ready = Wait-DevReady -BackendPort 8611 -FrontendPort 15273 -BackendProcess $alive -FrontendProcess $alive -StartupTimeoutSec 3
[Console]::WriteLine('RESULT=' + $ready.Backend + '/' + $ready.Frontend + '/' + $ready.TimedOut)
""")
    assert "RESULT=True/True/False" in output
    assert "[失败]" not in output and "等待超时" not in output


def test_readiness_timeout_preserves_loading_processes_and_explains_recheck(tmp_path):
    output = _run(tmp_path, _dev_function("Wait-DevReady") + """
function Test-DevBackend { param($Port) return $false }
function Test-DevFrontend { param($Port, [switch]$SkipApi) return $false }
function Show-DevLogs { param($LogDir, $Target) }
$alive = @{HasExited=$false}
$ready = Wait-DevReady -BackendPort 8611 -FrontendPort 15273 -BackendProcess $alive -FrontendProcess $alive -StartupTimeoutSec 1
[Console]::WriteLine('RESULT=' + $ready.Backend + '/' + $ready.Frontend + '/' + $ready.TimedOut + '/' + $ready.ProcessExited)
""")
    assert "RESULT=False/False/True/False" in output
    assert "[等待超时] 1 秒" in output and "status" in output and "open" in output
    assert "[失败]" not in output


def test_readiness_early_exit_is_failure_instead_of_cold_start_timeout(tmp_path):
    output = _run(tmp_path, _dev_function("Wait-DevReady") + """
function Test-DevBackend { param($Port) return $false }
function Test-DevFrontend { param($Port, [switch]$SkipApi) return $false }
function Show-DevLogs { param($LogDir, $Target) }
$ready = Wait-DevReady -BackendPort 8611 -FrontendPort 15273 -BackendProcess @{HasExited=$true} -FrontendProcess @{HasExited=$false} -StartupTimeoutSec 5
[Console]::WriteLine('RESULT=' + $ready.TimedOut + '/' + $ready.ProcessExited)
""")
    assert "RESULT=False/True" in output
    assert "[失败] 服务进程提前退出" in output and "等待超时" not in output


def test_log_tail_decodes_utf8_without_bom(tmp_path):
    (tmp_path / "backend.err.log").write_text("后端事件循环：ProactorEventLoop\n", encoding="utf-8")
    output = _run(tmp_path, _dev_function("Show-DevLogs") + """
Show-DevLogs -LogDir $LogDir -Target backend
""")
    assert "后端事件循环：ProactorEventLoop" in output


def test_console_restart_preserves_configured_startup_budget(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    console = "'" + str(repo / "scripts/dev-console.ps1").replace("'", "''") + "'"
    output = _run(tmp_path, f". {console}\n" + """
function Get-NetTCPConnection { param($State, $LocalPort, $ErrorAction) return @() }
function Start-DevService { param($Title, $WorkingDirectory, $Command, $LogName, $Environment) return @{Id=1;HasExited=$false} }
function Wait-DevReady { param($StartupTimeoutSec) [Console]::WriteLine('BUDGET=' + $StartupTimeoutSec); return @{Backend=$true;Frontend=$true} }
$Root = $LogDir
$state = @{BackendPort=8611;FrontendPort=15273;BackendReused=$false;StartupTimeoutSec=240}
Start-DevConsoleTarget -State $state -Target backend
""")
    assert "BUDGET=240" in output
