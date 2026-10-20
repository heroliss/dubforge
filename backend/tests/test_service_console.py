"""统一控制台与网页共享服务状态；启动器退出保留独立模型进程。"""
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest
from fastapi.testclient import TestClient

from dubforge import config, tts_service
from dubforge.dev_processes import stop_development_process
from dubforge.errors import ServiceStopError


def test_batch_entry_preserves_windows_newlines():
    path = Path(__file__).resolve().parents[2] / 'StartDev.bat'
    content = path.read_bytes()
    assert not content.startswith(b'\xef\xbb\xbf'), 'cmd 入口不能带 UTF-8 BOM'
    assert b'\r\n' in content and b'\n' not in content.replace(b'\r\n', b''), 'BAT 必须保存为 CRLF'
    content.decode('utf-8')


@pytest.mark.skipif(sys.platform != 'win32' or not shutil.which('powershell'), reason='Windows BAT 入口')
@pytest.mark.parametrize('initial_codepage,exit_code', [(936, 0), (65001, 0), (936, 7)])
def test_real_batch_entry_encoding_arguments_and_failure(tmp_path, initial_codepage, exit_code):
    """真正经 cmd 执行 BAT；替身 PS1 避免碰用户服务，同时覆盖中文空格路径。"""
    repo = Path(__file__).resolve().parents[2]
    root = tmp_path / '启动 验证'
    (root / 'scripts').mkdir(parents=True)
    shutil.copyfile(repo / 'StartDev.bat', root / 'StartDev.bat')
    (root / 'scripts/dev.ps1').write_text(
        'param([int]$BackendPort, [switch]$NoBrowser, [int]$FixtureExitCode)\n'
        '[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)\n'
        'Write-Host "中文输出验证：端口=$BackendPort 不开浏览器=$NoBrowser"\n'
        'exit $FixtureExitCode\n', encoding='utf-8-sig')
    wrapper = root / 'run-test.bat'
    wrapper.write_bytes((f'@echo off\r\nchcp {initial_codepage} >nul\r\n'
                         'call "%~dp0StartDev.bat" %*\r\nexit /b %ERRORLEVEL%\r\n').encode('ascii'))
    result = subprocess.run(['cmd.exe', '/d', '/c', str(wrapper), '-BackendPort', '8611',
        '-NoBrowser', '-FixtureExitCode', str(exit_code)], input=b'\r\n', capture_output=True, timeout=15)
    output = result.stdout.decode('utf-8')
    assert result.returncode == exit_code, result.stderr
    assert not result.stderr, result.stderr
    assert '[DubForge] 正在启动控制台，请稍候...' in output
    assert '中文输出验证：端口=8611 不开浏览器=True' in output
    assert ('启动失败，请查看上方错误信息' in output) == bool(exit_code)


def test_catalog_deduplicates_addresses_and_includes_project_overrides(make_project, monkeypatch):
    from app.main import app
    monkeypatch.setattr(tts_service, 'managed_processes', lambda: [])
    for backend in tts_service._BACKENDS.values():
        monkeypatch.setattr(backend, 'is_online', lambda url: False)
    config.set_key('tts.indextts25.url', 'http://localhost:7860/')
    make_project(params={'step_params': {'tts.indextts25.url': 'http://127.0.0.1:7860'}})
    uid = make_project(params={'step_params': {'tts.indextts25.url': 'http://127.0.0.1:17860'}})
    client = TestClient(app)
    states = client.get('/api/services').json()['services']
    assert [s['id'] for s in states] == ['indextts25', 'gptsovits', f'indextts25@{uid}']
    calls = []
    monkeypatch.setattr(tts_service, 'start', lambda cfg: calls.append(cfg('tts.indextts25.url')) or {'ok': True})
    assert client.post(f'/api/services/indextts25@{uid}/start').status_code == 200
    assert calls == ['http://127.0.0.1:17860']
    assert client.post('/api/services/unknown/start').status_code == 400


def test_console_stop_cannot_bypass_web_synthesis_guard(isolated_env, monkeypatch):
    from app.main import app
    monkeypatch.setattr(tts_service, 'managed_processes', lambda: [])
    monkeypatch.setattr(tts_service, '_managed_process', lambda port: ('indextts25', object(), Path('unused')))
    client = TestClient(app)
    with tts_service.use(config.get):
        response = client.post('/api/services/indextts25/stop')
    assert response.status_code == 400 and response.json()['code'] == 'SERVICE_STOP_FAILED'
    assert '正在配音' in response.json()['message']


def test_development_shutdown_preserves_managed_child_tree(isolated_env, monkeypatch, tmp_path):
    monkeypatch.setattr(tts_service, '_processes', {})
    child_script = tmp_path / 'model.py'
    child_script.write_text('import time\ntime.sleep(60)\n', encoding='utf-8')
    ready = tmp_path / 'ready.json'
    parent_script = tmp_path / 'dev.py'
    parent_script.write_text(
        'import subprocess, sys, time, json\nfrom pathlib import Path\n'
        'model = subprocess.Popen([sys.executable, sys.argv[1]])\n'
        'worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])\n'
        'ready = Path(sys.argv[2]); tmp = ready.with_suffix(".tmp")\n'
        'tmp.write_text(json.dumps([model.pid, worker.pid])); tmp.replace(ready)\ntime.sleep(60)\n', encoding='utf-8')
    parent = subprocess.Popen([sys.executable, str(parent_script), str(child_script), str(ready)],
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
    children = []
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        children = [psutil.Process(pid) for pid in json.loads(ready.read_text())]
        model, worker = children
        tts_service._record_process('indextts25', 17860, model)
        created = psutil.Process(parent.pid).create_time()
        with pytest.raises(ServiceStopError, match='身份已变化'):
            stop_development_process(parent.pid, created - 100)
        result = stop_development_process(parent.pid, created)
        assert result['preserved'] >= 1 and model.is_running() and not worker.is_running()
        parent.wait(timeout=3)
    finally:
        for item in children:
            if item.is_running():
                item.kill()
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=3)


@pytest.mark.skipif(sys.platform != 'win32' or not shutil.which('powershell'), reason='Windows PowerShell 控制台')
@pytest.mark.parametrize('service_count', [1, 2])
def test_hidden_services_cannot_read_launcher_input(tmp_path, service_count):
    """后台并存时均读到 EOF，不争抢主控制台输入，也不重写占用中的输入文件。"""
    repo = Path(__file__).resolve().parents[2]
    child_script = tmp_path / 'read_stdin.py'
    child_script.write_text(
        'import json, sys, time\nfrom pathlib import Path\n'
        'print(json.dumps({"is_tty": sys.stdin.isatty(), "input": sys.stdin.read()}), flush=True)\n'
        'Path(sys.argv[1]).touch()\n'
        'deadline = time.monotonic() + 15\n'
        'while not Path("stop").exists() and time.monotonic() < deadline:\n'
        '    time.sleep(.05)\n',
        encoding='utf-8')

    def ps_literal(value):
        return "'" + str(value).replace("'", "''") + "'"

    harness = tmp_path / 'hidden-service-test.ps1'
    harness.write_text(f"""
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
# 只加载启动函数，避免运行开发环境入口、探测或操作用户服务。
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile({ps_literal(repo / 'scripts/dev.ps1')}, [ref]$tokens, [ref]$errors)
if ($errors.Count) {{ throw '启动脚本语法错误' }}
$function = $ast.Find({{param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Start-DevService'}}, $true)
. ([scriptblock]::Create($function.Extent.Text))
. {ps_literal(repo / 'scripts/dev-output.ps1')}
$LogDir = {ps_literal(tmp_path)}
$ShowServiceWindows = $false
$children = @()
try {{
    for ($i = 0; $i -lt {service_count}; $i++) {{
        $ready = Join-Path $LogDir "child$i.ready"
        $child = Start-DevService -Title 'DubForge 输入回归测试' -WorkingDirectory $LogDir -LogName "child$i" `
            -Command @({ps_literal(sys.executable)}, {ps_literal(child_script)}, $ready)
        $children += $child
        $deadline = [DateTime]::UtcNow.AddSeconds(8)
        while (-not (Test-Path -LiteralPath $ready) -and [DateTime]::UtcNow -lt $deadline -and -not $child.HasExited) {{
            Start-Sleep -Milliseconds 50
        }}
        if (-not (Test-Path -LiteralPath $ready)) {{ throw '隐藏服务仍在等待控制台输入' }}
    }}
    foreach ($child in $children) {{
        if ($child.HasExited) {{ throw '测试服务应当同时保持运行' }}
    }}
    $answer = Read-DevInput 'INPUT_TEST'
    [Console]::WriteLine('PARENT_INPUT=<' + $answer + '>')
}} finally {{
    [IO.File]::WriteAllText((Join-Path $LogDir 'stop'), '')
    foreach ($child in $children) {{
        if (-not $child.WaitForExit(3000)) {{ & taskkill /PID $child.Id /T /F | Out-Null }}
    }}
}}
""", encoding='utf-8-sig')
    result = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(harness)],
        input='help\n', capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stderr
    assert 'PARENT_INPUT=<help>' in result.stdout
    for i in range(service_count):
        assert json.loads((tmp_path / f'child{i}.out.log').read_text(encoding='utf-8-sig')) == {
            'is_tty': False, 'input': ''}


@pytest.mark.skipif(sys.platform != 'win32' or not shutil.which('powershell'), reason='Windows PowerShell 控制台')
def test_powershell_help_dispatch_and_quit(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    harness = tmp_path / 'console-test.ps1'
    script = """
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
. '%s'
function Invoke-DevServiceApi {
    param($BackendPort, $Target, $Action)
    Write-Host "API:$Action/$Target"
    $state = [pscustomobject]@{id='indextts25';status='online';managed=$true;pid=123;url='http://127.0.0.1:17860';message='服务已就绪';log_path='test.log'}
    if (-not $Target) { return @{services=@($state)} }
    if ($Action -eq 'log') { return @{path='test.log';text='日志验证内容';truncated=$false} }
    return $state
}
Enter-DevControlPanel -BackendPort 8611 -FrontendPort 15273 -LogDir '%s'
Write-Host 'PANEL_EXITED'
""" % (str(repo / 'scripts/dev-console.ps1').replace("'", "''"), str(tmp_path).replace("'", "''"))
    harness.write_text(script, encoding='utf-8-sig')
    result = subprocess.run(['powershell', '-NoProfile', '-File', str(harness)],
        input='help\nservices\nstart indextts25\nlogs indextts25\nstop indextts25\nquit\n',
        capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stderr
    assert 'quit / exit / stop' in result.stdout
    assert 'API:start/indextts25' in result.stdout and 'API:stop/indextts25' in result.stdout
    assert '日志验证内容' in result.stdout and 'PANEL_EXITED' in result.stdout
    assert '[错误]' not in result.stdout


@pytest.mark.skipif(sys.platform != 'win32' or not shutil.which('powershell'), reason='Windows PowerShell 控制台')
def test_powershell_console_controls_backend_and_frontend_independently(tmp_path):
    """控制台操作应更新本地进程状态，不把前后端命令误发给配音服务 API。"""
    repo = Path(__file__).resolve().parents[2]
    harness = tmp_path / 'console-process-control-test.ps1'
    script = """
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
. '%s'
$script:nextPid = 100
function Get-NetTCPConnection { @() }
function Start-Sleep { param([int]$Milliseconds) }
function Start-DevService {
    param($Title, $WorkingDirectory, $Command, $LogName, $Environment)
    $script:nextPid += 1
    Write-Host ('START:' + $LogName)
    return [pscustomobject]@{ Id=$script:nextPid; HasExited=$false }
}
function Stop-DevProcessTree { param($Process, $Name); Write-Host ('STOP:' + $Name + ':' + $Process.Id) }
function Wait-DevReady { param($Target, $BackendProcess, $FrontendProcess, $BackendPort, $FrontendPort, $BackendReused); return @{ Backend=$true; Frontend=$true } }
function Show-DevStatus { param($BackendProcess, $FrontendProcess, $BackendPort, $FrontendPort, $BackendReused); Write-Host ('STATUS:' + [bool]$BackendProcess + ':' + [bool]$FrontendProcess) }
function Invoke-DevServiceApi { throw '不应调用配音服务 API' }
$state = @{ BackendProcess = [pscustomobject]@{ Id=1; HasExited=$false }; FrontendProcess = $null; BackendPort=8611; FrontendPort=15273; BackendReused=$false }
Enter-DevControlPanel -State $state -BackendPort 8611 -FrontendPort 15273 -LogDir '%s'
Write-Host ('FINAL:' + [bool]$state.BackendProcess + ':' + [bool]$state.FrontendProcess)
""" % (str(repo / 'scripts/dev-console.ps1').replace("'", "''"), str(tmp_path).replace("'", "''"))
    harness.write_text(script, encoding='utf-8-sig')
    result = subprocess.run(
        ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(harness)],
        input='status\nstop frontend\nstart frontend\nrestart backend\nquit\n',
        capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stderr
    assert 'STATUS:True:False' in result.stdout
    assert 'START:frontend' in result.stdout
    assert 'STOP:backend:1' in result.stdout
    assert 'START:backend' in result.stdout
    assert '不应调用配音服务 API' not in result.stdout
    assert '[错误]' not in result.stdout
