"""PowerShell 所有权恢复用拦截器验证，不启动或结束任何真实服务。"""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from dubforge import dev_owner


REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.platform != 'win32' or not shutil.which('powershell'), reason='Windows 控制台')


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def run_ps(tmp_path, body):
    path = tmp_path / 'owner-test.ps1'
    path.write_text("$ErrorActionPreference='Stop'\n[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)\n" + body, encoding='utf-8-sig')
    result = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(path)],
                            capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize('reused', [False, True])
def test_console_restores_valid_replacement_and_never_adopts_reused_backend(tmp_path, reused):
    registry = tmp_path / 'data/dev-logs/8611-15273/backend.process.json'
    registry.parent.mkdir(parents=True)
    registry.write_text('{}')
    value = run_ps(tmp_path, f"""
. {quote(REPO / 'scripts/dev-console.ps1')}
$Root={quote(tmp_path)}
$script:reads=0; $script:starts=0; $script:stops=@()
function Write-DevText {{param($Text,$ForegroundColor)}}
function Start-Sleep {{param($Milliseconds)}}
function Invoke-DevOwner {{param($Arguments); $script:reads++; return [pscustomobject]@{{pid=51;created=10;generation='new';role='backend';backend_port=8611;frontend_port=15273}}}}
function Get-Process {{param($Id,$ErrorAction); return [pscustomobject]@{{Id=$Id;HasExited=$false;StartTime=[DateTimeOffset]::FromUnixTimeSeconds(10).LocalDateTime}}}}
function Get-NetTCPConnection {{param($State,$LocalPort,$ErrorAction); return [pscustomobject]@{{OwningProcess=52}}}}
function Stop-DevProcessTree {{param($Process,$Name); $script:stops+=$Process.Id}}
function Start-DevService {{$script:starts++; throw '测试禁止启动'}}
$state=@{{BackendProcess=[pscustomobject]@{{Id=1;HasExited=$true}};BackendPort=8611;FrontendPort=15273;BackendReused=${str(reused).lower()}}}
$restored=Get-DevManagedProcess -State $state -Name backend
Stop-DevConsoleTarget -State $state -Target backend
Start-DevConsoleTarget -State $state -Target backend
@{{reads=$script:reads;starts=$script:starts;stops=$script:stops;restored=if($restored){{$restored.Id}}else{{$null}}}}|ConvertTo-Json -Compress
""")
    assert value['starts'] == 0
    if reused:
        assert value['reads'] == 0 and value['restored'] is None and value['stops'] == []
    else:
        assert value['restored'] == 51 and value['stops'] == [51]


def test_identity_error_blocks_new_start_even_before_port_binding(tmp_path):
    registry = tmp_path / 'data/dev-logs/8611-15273/backend.process.json'
    registry.parent.mkdir(parents=True)
    registry.write_text('{}')
    value = run_ps(tmp_path, f"""
. {quote(REPO / 'scripts/dev-console.ps1')}
$Root={quote(tmp_path)}
$script:starts=0
function Write-DevText {{param($Text,$ForegroundColor)}}
function Invoke-DevOwner {{param($Arguments); throw '工作区身份不匹配'}}
function Start-DevService {{$script:starts++; throw '禁止启动'}}
function Get-NetTCPConnection {{throw '身份失败时不应依靠端口猜归属'}}
$state=@{{BackendProcess=$null;BackendPort=8611;FrontendPort=15273;BackendReused=$false}}
Start-DevConsoleTarget -State $state -Target backend
@{{starts=$script:starts;error=$state.backendOwnerError}}|ConvertTo-Json -Compress
""")
    assert value['starts'] == 0 and '不匹配' in value['error']


@pytest.mark.parametrize('role', ['backend', 'frontend'])
@pytest.mark.parametrize('visible', [False, True])
def test_start_helper_registers_owner_and_forget_carries_exact_generation(tmp_path, role, visible):
    logdir = tmp_path / 'data/dev-logs/8611-15273'
    logdir.mkdir(parents=True)
    value = run_ps(tmp_path, f"""
. {quote(REPO / 'scripts/dev-console.ps1')}
$Root={quote(tmp_path)}; $LogDir={quote(logdir)}; $ShowServiceWindows=${str(visible).lower()}
$script:operations=@()
function Write-DevText {{param($Text,$ForegroundColor)}}
function Invoke-DevOwner {{param($Arguments); $script:operations+=,@($Arguments); return [pscustomobject]@{{pid=51;created=10;generation='exact-generation';role='{role}';backend_port=8611;frontend_port=15273}}}}
function Start-Process {{param($ArgumentList); $script:wrapperArgs=$ArgumentList; return [pscustomobject]@{{Id=51;HasExited=$false;StartTime=[DateTimeOffset]::FromUnixTimeSeconds(10).LocalDateTime}}}}
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile({quote(REPO / 'scripts/dev.ps1')},[ref]$tokens,[ref]$errors)
$definition=$ast.Find({{param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Start-DevService'}},$true)
. ([scriptblock]::Create($definition.Extent.Text))
$command=if('{role}' -eq 'backend'){{@('uv','run','uvicorn','app.main:app','--port','8611')}}else{{@('npm','run','dev','--','--port','15273','--strictPort')}}
$process=Start-DevService -Title fixture -WorkingDirectory (Join-Path $Root '{role}') -LogName '{role}' -Command $command
Unregister-DevServiceOwner -Process $process
@{{generation=$process.DubForgeOwner.generation;operations=$script:operations;wrapperArgs=$script:wrapperArgs;command=$command}}|ConvertTo-Json -Compress -Depth 5
""")
    assert value['generation'] == 'exact-generation'
    assert value['operations'][0][:5] == ['register', str(tmp_path), '8611', '15273', role]
    assert value['operations'][0][-len(value['command']):] == value['command']
    assert value['operations'][1][-2:] == ['--generation', 'exact-generation']
    # 用真实启动函数生成的完整编码命令核验协议，避免手写 fixture 掩盖包装器不兼容。
    dev_owner._intent(dict(argv=['powershell', *value['wrapperArgs']], executable='powershell.exe',
                           workspace=str(tmp_path), role=role, backend_port=8611, frontend_port=15273,
                           command=value['command']))


def test_registration_failure_preserves_started_process_for_current_console(tmp_path):
    logdir = tmp_path / 'data/dev-logs/8611-15273'
    logdir.mkdir(parents=True)
    value = run_ps(tmp_path, f"""
. {quote(REPO / 'scripts/dev-console.ps1')}
$Root={quote(tmp_path)}; $LogDir={quote(logdir)}; $ShowServiceWindows=$false
$script:messages=@()
function Write-DevText {{param($Text,$ForegroundColor); $script:messages+=[string]$Text}}
function Invoke-DevOwner {{param($Arguments); throw '登记写入失败'}}
function Start-Process {{return [pscustomobject]@{{Id=51;HasExited=$false;StartTime=[DateTimeOffset]::FromUnixTimeSeconds(10).LocalDateTime}}}}
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile({quote(REPO / 'scripts/dev.ps1')},[ref]$tokens,[ref]$errors)
$definition=$ast.Find({{param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Start-DevService'}},$true)
. ([scriptblock]::Create($definition.Extent.Text))
$process=Start-DevService -Title fixture -WorkingDirectory (Join-Path $Root 'backend') -LogName backend -Command @('uv','run','uvicorn','app.main:app','--port','8611')
@{{pid=$process.Id;messages=$script:messages}}|ConvertTo-Json -Compress
""")
    assert value['pid'] == 51
    assert '身份登记失败' in ''.join(value['messages'])


@pytest.mark.parametrize('missing', [False, True])
def test_explicit_attach_verifies_both_before_any_lifecycle_action(tmp_path, missing):
    # 提取实际 AttachRegistered 分支运行；无需执行 dev.ps1 前置输出或任何端口清理。
    value = run_ps(tmp_path, f"""
$Root={quote(tmp_path)}; $BackendPort=8611; $FrontendPort=15273; $LogDir={quote(tmp_path)}
$ReloadBackend=$false; $ShowServiceWindows=$false
$script:checked=@(); $script:entered=0; $script:cleanups=0; $script:messages=@()
function Resolve-DevServiceOwner {{param($State,$Name,[switch]$RequireListener); if(-not $RequireListener){{throw '接管必须核验监听'}}; $script:checked+=$Name; if(${str(missing).lower()} -and $Name -eq 'frontend'){{return $null}}; return [pscustomobject]@{{Id=if($Name -eq 'backend'){{51}}else{{61}}}}}}
function Write-DevText {{param($Text,$ForegroundColor); $script:messages+=[string]$Text}}
function Write-DevLifecycle {{param($Text)}}
function Enter-DevControlPanel {{param($State,$BackendPort,$FrontendPort,$LogDir,$ShowServiceWindows); $script:entered++; if($State.BackendProcess.Id -ne 51 -or $State.FrontendProcess.Id -ne 61){{throw '未保存正确管理引用'}}}}
function Stop-DevServices {{param($State,$BackendPort,$FrontendPort); if($State.BackendProcess.Id -ne 51 -or $State.FrontendProcess.Id -ne 61){{throw '退出未使用接管状态'}}; $script:cleanups++}}
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile({quote(REPO / 'scripts/dev.ps1')},[ref]$tokens,[ref]$errors)
$branch=$ast.Find({{param($node) $node -is [Management.Automation.Language.IfStatementAst] -and $node.Clauses[0].Item1.Extent.Text -eq '$AttachRegistered'}},$true)
if(-not $branch){{throw '未找到实际接管入口'}}
# 仅把进程级 exit 换成局部 return，以便断言失败入口没有进入 finally 清理。
$body=$branch.Clauses[0].Item2.Extent.Text
$body=$body.Substring(1,$body.Length-2) -replace 'exit 1','return' -replace 'exit 0','return'
& ([scriptblock]::Create($body))
@{{checked=$script:checked;entered=$script:entered;cleanups=$script:cleanups;messages=$script:messages}}|ConvertTo-Json -Compress
""")
    assert value['checked'] == ['backend', 'frontend']
    assert value['entered'] == (0 if missing else 1)
    assert value['cleanups'] == (0 if missing else 1)
    if missing:
        assert '未启动、停止或清理任何服务' in ''.join(value['messages'])


def test_attach_branch_precedes_all_launcher_mutations():
    source = (REPO / 'scripts/dev.ps1').read_text(encoding='utf-8')
    attach = source.index('if ($AttachRegistered)')
    assert attach < source.index('New-Item -ItemType Directory -Force -Path $LogDir')
    assert attach < source.index('$processSnapshot =')


@pytest.mark.parametrize('invalid,reused', [(False, False), (True, False), (False, True)])
def test_console_exit_resolves_current_generation_and_never_falls_back_to_stale_reference(tmp_path, invalid, reused):
    value = run_ps(tmp_path, f"""
. {quote(REPO / 'scripts/dev-console.ps1')}
$script:resolved=@(); $script:stopped=@()
function Get-DevManagedProcess {{param($State,$Name); $script:resolved+=$Name; if(($State.BackendReused -or ${str(invalid).lower()}) -and $Name -eq 'backend'){{return $null}}; return [Diagnostics.Process]::GetCurrentProcess()}}
function Stop-DevProcessTree {{param($Process,$Name); if($Process){{$script:stopped+=$Name}}}}
function Get-NetTCPConnection {{param($State,$LocalPort,$ErrorAction); return @()}}
function Start-Sleep {{param($Milliseconds)}}
function Write-DevText {{param($Text,$ForegroundColor)}}
function Write-DevLifecycle {{param($Text)}}
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile({quote(REPO / 'scripts/dev.ps1')},[ref]$tokens,[ref]$errors)
$definition=$ast.Find({{param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Stop-DevServices'}},$true)
. ([scriptblock]::Create($definition.Extent.Text))
$state=@{{BackendReused=${str(reused).lower()};BackendProcess='旧引用不能作为兜底';FrontendProcess='旧引用不能作为兜底'}}
Stop-DevServices -State $state -BackendPort 8611 -FrontendPort 15273
@{{resolved=$script:resolved;stopped=$script:stopped}}|ConvertTo-Json -Compress
""")
    assert value['resolved'] == ['backend', 'frontend']
    assert value['stopped'] == (['前端'] if invalid or reused else ['前端', '后端'])
