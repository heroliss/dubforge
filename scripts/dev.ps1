# DubForge 开发环境一键启动 (前后端 + 浏览器)
# 用法: 双击根目录 StartDev.bat, 或 .\scripts\dev.ps1
#       需已跑过: backend/uv sync 与 frontend/npm install
param(
    [int]$BackendPort = 0,      # 0 = 读 config.yaml 的 server.port
    [int]$FrontendPort = 5273,   # Vite 开发端口
    [ValidateRange(1, 3600)][int]$StartupTimeoutSec = 180, # 冷启动/恢复扫描允许更长等待
    [switch]$ReloadBackend,    # 显式开启后端自动重载；日常长任务使用稳定模式
    [switch]$AttachRegistered, # 只接管严格核验的既有登记服务，不清理端口、不启动新进程
    [switch]$ShowServiceWindows, # 调试时显示后端/前端窗口; 默认只保留一个控制台
    [switch]$NoBrowser          # 脚本验证或只需控制台时不自动打开浏览器
)
$ErrorActionPreference = "Stop"
# 已有逐阶段状态输出，关闭 PowerShell 自带进度层，避免重绘中文时出现叠字。
$ProgressPreference = 'SilentlyContinue'
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
. (Join-Path $PSScriptRoot "dev-output.ps1")
. (Join-Path $PSScriptRoot "dev-web.ps1")
$Host.UI.RawUI.WindowTitle = "DubForge Dev Launcher"
Write-DevText "`n  == DubForge 启动 ==" -ForegroundColor Cyan
Write-DevText '  [1/4] 正在读取配置并检查运行环境…' -ForegroundColor DarkGray
$Root = (Resolve-Path "$PSScriptRoot\..").Path

function Start-DevService {
    param(
        [string]$Title,
        [string]$WorkingDirectory,
        [string[]]$Command,
        [string]$LogName,
        [hashtable]$Environment = @{}
    )
    # 部分前端开发服务器会把 stdin EOF 当作父进程退出。隐藏服务使用空 stdin，
    # 前端用 CI=true 关闭该监听；可见调试窗口保留原有交互行为。
    if ($LogName -eq 'frontend' -and -not $ShowServiceWindows) {
        $Environment = @{} + $Environment
        $Environment.CI = 'true'
    }
    $envAssignments = foreach ($entry in $Environment.GetEnumerator()) {
        "`$env:$($entry.Key)='$([string]$entry.Value -replace "'", "''")';"
    }
    $quotedCommand = ($Command | ForEach-Object { "'" + ($_ -replace "'", "''") + "'" }) -join ' '
    # 子 PowerShell 的模块加载进度默认序列化为 CLIXML，会混入原生 stderr 日志。
    $commandText = "`$ErrorActionPreference='Stop';`$ProgressPreference='SilentlyContinue';`$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new(`$false);" + ($envAssignments -join '') + "`$Host.UI.RawUI.WindowTitle='$($Title -replace "'", "''")'; Set-Location -LiteralPath '$($WorkingDirectory -replace "'", "''")'; & " + $quotedCommand
    $stdout = Join-Path $LogDir "$LogName.out.log"
    $stderr = Join-Path $LogDir "$LogName.err.log"
    foreach ($path in @($stdout, $stderr)) {
        if ((Test-Path -LiteralPath $path) -and (Get-Item -LiteralPath $path).Length -gt 0) {
            $archive = Join-Path $LogDir 'archive'
            New-Item -ItemType Directory -Force -Path $archive | Out-Null
            Move-Item -LiteralPath $path -Destination (Join-Path $archive ("$([DateTime]::Now.ToString('yyyyMMdd-HHmmss-fff'))-$([IO.Path]::GetFileName($path))"))
        }
    }
    Set-Content -Path $stdout -Value "" -Encoding UTF8
    Set-Content -Path $stderr -Value "" -Encoding UTF8
    if ($ShowServiceWindows) {
        $commandText += " *>&1 | ForEach-Object { `$_ | Out-File -Append -Encoding utf8 -LiteralPath '$($stdout -replace "'", "''")'; Write-Host `$_ }"
    }
    # PowerShell 默认不会把 npm/node 的退出码传给包装进程，导致服务异常退出时
    # 控制台只能看到“进程已退出”而看不到原因。显式保留原生退出码，便于 status 诊断。
    $lifecyclePath = Join-Path $LogDir "$LogName.lifecycle.log"
    $escapedLifecyclePath = $lifecyclePath -replace "'", "''"
    $commandText += "; `$dubforgeExitCode = if (`$LASTEXITCODE -is [int]) { `$LASTEXITCODE } else { 0 }; try { [IO.File]::AppendAllText('$escapedLifecyclePath', (([DateTimeOffset]::Now.ToString('o')) + ' pid=' + `$PID + ' exit_code=' + `$dubforgeExitCode + [Environment]::NewLine), [Text.UTF8Encoding]::new(`$false)) } catch {}; exit `$dubforgeExitCode"
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($commandText))
    $args = @("-NoProfile", "-EncodedCommand", $encoded)
    if ($ShowServiceWindows) {
        $process = Start-Process -PassThru -FilePath powershell -ArgumentList $args
    } else {
    # 隐藏进程不能继承主控制台输入，否则 PowerShell/服务会争抢按键，主窗口无法输入。
    # Windows PowerShell 会独占重定向输入文件，因此前后端各用自己的空文件。
    # 隐藏服务读到 EOF；调试时显示的独立窗口仍可正常交互。
    $stdin = Join-Path $LogDir "$LogName.stdin.empty"
    [IO.File]::WriteAllText($stdin, '')
    $process = Start-Process -PassThru -WindowStyle Hidden -FilePath powershell -ArgumentList (@('-NonInteractive') + $args) `
        -RedirectStandardInput $stdin -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    }
    if (Get-Command Register-DevServiceOwner -ErrorAction SilentlyContinue) {
        Register-DevServiceOwner -Process $process -Name $LogName -Command $Command
    }
    return $process
}

function Stop-DevProcessTree {
    param(
        [System.Diagnostics.Process]$Process,
        [string]$Name
    )
    if (-not $Process) { return }
    $current = Get-Process -Id $Process.Id -ErrorAction SilentlyContinue
    if (-not $current) { return }
    Write-DevText "  正在停止 $Name (PID=$($Process.Id))..." -ForegroundColor DarkGray
    # 核验启动时间并保护已登记的配音进程；taskkill /T 会把后台模型一起杀掉。
    $created = ([DateTimeOffset]$Process.StartTime.ToUniversalTime()).ToUnixTimeMilliseconds() / 1000.0
    $createdText = $created.ToString('R', [Globalization.CultureInfo]::InvariantCulture)
    $stopOutput = & (Join-Path $Root 'backend\.venv\Scripts\python.exe') -m dubforge.dev_processes $Process.Id $createdText
    if ($LASTEXITCODE -ne 0) { Write-DevText "  [警告] $Name 未完全关闭：$stopOutput" -ForegroundColor Yellow }
    elseif (Get-Command Unregister-DevServiceOwner -ErrorAction SilentlyContinue) { Unregister-DevServiceOwner -Process $Process }
}

function Show-DevStatus {
    param(
        [System.Diagnostics.Process]$BackendProcess,
        [System.Diagnostics.Process]$FrontendProcess,
        [int]$BackendPort,
        [int]$FrontendPort,
        [switch]$BackendReused
    )
    $backendAlive = if ($BackendProcess) { [bool](Get-Process -Id $BackendProcess.Id -ErrorAction SilentlyContinue) } else { [bool]$BackendReused }
    $frontendAlive = if ($FrontendProcess) { [bool](Get-Process -Id $FrontendProcess.Id -ErrorAction SilentlyContinue) } else { $false }
    $backendListening = @(Get-NetTCPConnection -State Listen -LocalPort $BackendPort -ErrorAction SilentlyContinue).Count -gt 0
    $frontendListening = @(Get-NetTCPConnection -State Listen -LocalPort $FrontendPort -ErrorAction SilentlyContinue).Count -gt 0
    $backendState = if ($backendAlive -and $backendListening) {
        if ($BackendReused) { "运行中（复用实例）" } else { "运行中" }
    } elseif ($backendAlive) {
        if ($BackendReused) { "复用实例，端口未就绪" } else { "进程存在，端口未就绪" }
    } elseif ($backendListening) {
        "未管理进程占用端口"
    } else { "已退出" }
    $frontendState = if ($frontendAlive -and $frontendListening) { "运行中" } elseif ($frontendAlive) { "进程存在，端口未就绪" } elseif ($frontendListening) { "未管理进程占用端口" } else { "已退出" }
    $backendExitCode = $null
    $frontendExitCode = $null
    if ($BackendProcess) { try { if ($BackendProcess.HasExited) { $backendExitCode = $BackendProcess.ExitCode } } catch {} }
    if ($FrontendProcess) { try { if ($FrontendProcess.HasExited) { $frontendExitCode = $FrontendProcess.ExitCode } } catch {} }
    if ($null -ne $backendExitCode) { $backendState += "（退出码=$backendExitCode）" }
    if ($null -ne $frontendExitCode) { $frontendState += "（退出码=$frontendExitCode）" }
    $backendPid = if ($BackendProcess) { $BackendProcess.Id } elseif ($BackendReused) { "外部实例" } else { "—" }
    $frontendPid = if ($FrontendProcess) { $FrontendProcess.Id } else { "—" }
    Write-DevText "  后端  $backendState  http://127.0.0.1:$BackendPort  PID=$backendPid"
    Write-DevText "  前端  $frontendState  http://127.0.0.1:$FrontendPort  PID=$frontendPid"
}

function Show-DevLogs {
    param(
        [string]$LogDir,
        [string]$Target = "all"
    )
    $names = switch ($Target) {
        "backend" { @("backend") }
        "后端" { @("backend") }
        "frontend" { @("frontend") }
        "前端" { @("frontend") }
        "launcher" { @("launcher") }
        "启动器" { @("launcher") }
        default { @("backend", "frontend") }
    }
    foreach ($name in $names) {
        Write-DevText "`n  --- $name 最近日志 ---" -ForegroundColor DarkCyan
        $suffixes = if ($name -eq 'launcher') { @('lifecycle') } else { @('out', 'err') }
        foreach ($suffix in $suffixes) {
            $path = if ($name -eq 'launcher') { Join-Path $LogDir 'launcher.log' } else { Join-Path $LogDir "$name.$suffix.log" }
            if (Test-Path $path) {
                Write-DevText "  [$suffix] $path" -ForegroundColor DarkGray
                $lines = @(Get-Content -LiteralPath $path -Tail 12 -Encoding UTF8 -ErrorAction SilentlyContinue)
                if ($lines.Count -gt 0) { $lines | ForEach-Object { Write-DevText "    $_" } }
                else { Write-DevText "    （暂无内容）" -ForegroundColor DarkGray }
            }
        }
    }
}

function Wait-DevReady {
    param($BackendProcess, $FrontendProcess, [int]$BackendPort, [int]$FrontendPort,
          [switch]$BackendReused, [ValidateSet('all','backend','frontend')][string]$Target = 'all',
          [ValidateRange(1, 3600)][int]$StartupTimeoutSec = 180)
    $checkBackend = $Target -in @('all', 'backend')
    $checkFrontend = $Target -in @('all', 'frontend')
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $okB = -not $checkBackend; $okF = -not $checkFrontend; $lastReport = -3
    $processExited = $false
    Write-DevText '  [4/4] 等待服务响应，首次加载可能稍慢…' -ForegroundColor DarkGray
    while ($clock.Elapsed.TotalSeconds -lt $StartupTimeoutSec) {
        if (-not $okB) {
            $okB = Test-DevBackend -Port $BackendPort
        }
        if (-not $okF) {
            $okF = Test-DevFrontend -Port $FrontendPort -SkipApi:($Target -eq 'frontend')
        }
        if ($okB -and $okF) { break }
        $watchedProcessExited = ($checkBackend -and $BackendProcess -and $BackendProcess.HasExited) -or
            ($checkFrontend -and $FrontendProcess -and $FrontendProcess.HasExited)
        if ($watchedProcessExited) {
            $processExited = $true
            Write-DevText '  [失败] 服务进程提前退出，下面显示最近日志。' -ForegroundColor Red
            break
        }
        if ($clock.Elapsed.TotalSeconds - $lastReport -ge 2) {
            $lastReport = $clock.Elapsed.TotalSeconds
            $backendText = if (-not $checkBackend) { '未检查' } elseif ($okB) { '已就绪' } else { '加载中' }
            $frontendText = if (-not $checkFrontend) { '未检查' } elseif ($okF) { '已就绪' } else { '加载中' }
            Write-DevText "  已等待 $([int]$clock.Elapsed.TotalSeconds) 秒 · 后端 $backendText · 前端 $frontendText" -ForegroundColor DarkGray
        }
        Start-Sleep -Milliseconds 300
    }
    $timedOut = -not ($okB -and $okF) -and -not $processExited
    if ($timedOut) {
        Write-DevText "  [等待超时] $StartupTimeoutSec 秒内尚未就绪，可能仍在加载；服务会继续运行。请保留控制台，用 status 查看状态，稍后输入 open 打开页面。" -ForegroundColor Yellow
    }
    if ($checkBackend -and -not $okB) { Show-DevLogs -LogDir $LogDir -Target backend }
    if ($checkFrontend -and -not $okF) { Show-DevLogs -LogDir $LogDir -Target frontend }
    Write-DevLifecycle "就绪检查：后端=$okB 前端及代理=$okF 端口=$BackendPort/$FrontendPort"
    return @{ Backend = $okB; Frontend = $okF; TimedOut = $timedOut; ProcessExited = $processExited }
}

. (Join-Path $PSScriptRoot "dev-console.ps1")

function Stop-DevServices {
    param(
        [System.Diagnostics.Process]$BackendProcess,
        [System.Diagnostics.Process]$FrontendProcess,
        [int]$BackendPort,
        [int]$FrontendPort,
        [switch]$KeepBackend,
        [hashtable]$State
    )
    # 维护工具可能替换服务包装器；退出与单独 stop 一样，只按完整登记恢复当前一代。
    if ($State) {
        $BackendProcess = Get-DevManagedProcess -State $State -Name backend
        $FrontendProcess = Get-DevManagedProcess -State $State -Name frontend
        $KeepBackend = [bool]$State.BackendReused
    }
    Stop-DevProcessTree -Process $FrontendProcess -Name "前端"
    if (-not $KeepBackend) { Stop-DevProcessTree -Process $BackendProcess -Name "后端" }
    Write-DevLifecycle "开发控制台退出，停止本次服务；保留复用后端=$KeepBackend"
    Start-Sleep -Milliseconds 500
    $remaining = @()
    $backendStillListening = @(Get-NetTCPConnection -State Listen -LocalPort $BackendPort -ErrorAction SilentlyContinue).Count -gt 0
    if (@(Get-NetTCPConnection -State Listen -LocalPort $FrontendPort -ErrorAction SilentlyContinue).Count -gt 0) { $remaining += "前端 $FrontendPort" }
    if (-not $KeepBackend -and $backendStillListening) { $remaining += "后端 $BackendPort" }
    if ($remaining.Count -gt 0) {
        Write-DevText "  [警告] 以下端口仍被占用：$($remaining -join '、')，可能是旧实例，不会误杀其他进程。" -ForegroundColor Yellow
    } elseif ($KeepBackend -and $backendStillListening) {
        Write-DevText "  [完成] 前端端口已释放；按复用模式保留原有后端实例。" -ForegroundColor Green
    } else {
        Write-DevText "  [完成] 前后端端口已释放；项目文件和已完成产物保留。" -ForegroundColor Green
    }
}
if ($BackendPort -le 0) {
    $m = Select-String -Path "$Root\config.yaml" -Pattern '^\s*port:\s*(\d+)' -ErrorAction SilentlyContinue
    $BackendPort = if ($m) { [int]$m.Matches[0].Groups[1].Value } else { 8600 }
}
$LogDir = Join-Path $Root "data\dev-logs\$BackendPort-$FrontendPort"
if ($AttachRegistered) {
    # 全部只读核验成功后才进入控制台；失败不能落入旧的端口清理或启动分支。
    $attached = @{ BackendProcess=$null; FrontendProcess=$null; BackendPort=$BackendPort; FrontendPort=$FrontendPort; BackendReused=$false; ReloadBackend=[bool]$ReloadBackend; StartupTimeoutSec=$StartupTimeoutSec }
    try {
        foreach ($name in @('backend','frontend')) {
            $process = Resolve-DevServiceOwner -State $attached -Name $name -RequireListener
            if (-not $process) { throw "$name 没有完整有效的服务登记。" }
            $attached["$($name)Process"] = $process
        }
    } catch { Write-DevText "  [错误] 无法接管：$_ 未启动、停止或清理任何服务。" -ForegroundColor Red; exit 1 }
    Write-DevText '  已核验并接管登记的前后端；未重启服务。退出此控制台将停止接管的服务，配音服务仍保留。' -ForegroundColor Yellow
    Write-DevLifecycle "明确接管登记服务，控制台 PID=$PID 后端=$($attached.BackendProcess.Id) 前端=$($attached.FrontendProcess.Id)"
    try { Enter-DevControlPanel -State $attached -BackendPort $BackendPort -FrontendPort $FrontendPort -LogDir $LogDir -ShowServiceWindows:$ShowServiceWindows }
    finally { Stop-DevServices -State $attached -BackendPort $BackendPort -FrontendPort $FrontendPort }
    exit 0
}
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Write-DevLifecycle "启动控制台 PID=$PID 后端=$BackendPort 前端=$FrontendPort 后端自动重载=$ReloadBackend 自动打开浏览器=$(-not $NoBrowser)"

# ---- 前置检查 ----
if (-not (Test-Path "$Root\backend\.venv")) {
    Write-DevText "  [错误] 未找到 backend\.venv —— 请先执行: cd backend; uv sync" -ForegroundColor Red
    Read-DevInput "  回车退出"; exit 1
}
if (-not (Test-Path "$Root\frontend\node_modules")) {
    Write-DevText "  [错误] 未找到 frontend\node_modules —— 请先执行: cd frontend; npm install" -ForegroundColor Red
    Read-DevInput "  回车退出"; exit 1
}

Write-DevText "  [2/4] 正在检查已有进程与端口（后端 $BackendPort / 前端 $FrontendPort）…" -ForegroundColor DarkGray

# 配置端口之外如果还留有本项目后端，明确提示当前前端会连接哪一个。
# 只提示不自动结束：另一个实例可能正承载用户任务。
$processSnapshot = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
$listenerSnapshot = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue)
$projectBackendPids = @($processSnapshot |
    Where-Object {
        $_.CommandLine -and
        $_.CommandLine -match [regex]::Escape((Join-Path $Root 'backend')) -and
        $_.CommandLine -match 'uvicorn' -and
        $_.CommandLine -match 'app\.main:app'
    } | Select-Object -ExpandProperty ProcessId)
# Windows 虚拟环境和 --reload 会派生真正监听端口的 Python 子进程。
do {
    $previousCount = $projectBackendPids.Count
    $projectBackendPids = @($projectBackendPids + @($processSnapshot | Where-Object {
        $projectBackendPids -contains $_.ParentProcessId
    } | Select-Object -ExpandProperty ProcessId) | Select-Object -Unique)
} while ($projectBackendPids.Count -gt $previousCount)
$otherBackendListeners = @()
foreach ($connection in $listenerSnapshot) {
    if ($projectBackendPids -contains $connection.OwningProcess -and
        [int]$connection.LocalPort -ne $BackendPort) {
        $otherBackendListeners += $connection
    }
}
if ($otherBackendListeners.Count -gt 0) {
    $ports = ($otherBackendListeners | Select-Object -ExpandProperty LocalPort -Unique) -join ', '
    Write-DevText "  [提示] 检测到本项目其他后端端口: $ports；本次启动/前端代理将使用 $BackendPort。" -ForegroundColor Yellow
    Write-DevText "  [提示] 旧端口不会自动结束，以免中断其中的任务；确认无任务后可手动关闭。" -ForegroundColor DarkGray
}

# 只处理占用本次前端端口的本项目 Vite，不影响其他端口的独立实例。
$frontendOwners = @($listenerSnapshot | Where-Object { $_.LocalPort -eq $FrontendPort } | Select-Object -ExpandProperty OwningProcess)
$frontendProcesses = @($processSnapshot | Where-Object {
    $_.CommandLine -and
    $_.CommandLine -match [regex]::Escape((Join-Path $Root 'frontend')) -and
    $_.CommandLine -match 'vite' -and $frontendOwners -contains $_.ProcessId
})
if ($frontendProcesses.Count -gt 0) {
    Write-DevText "  [提示] 清理 $($frontendProcesses.Count) 个本项目遗留的前端开发进程，避免旧前端占用端口。" -ForegroundColor Yellow
    foreach ($fp in $frontendProcesses) {
        taskkill /PID $fp.ProcessId /T /F 2>$null | Out-Null
    }
    Start-Sleep -Milliseconds 800
}

# ---- 单实例检测: 后端端口已被占用时给出选择, 避免双实例互相顶掉 ----
function Get-PortPids($p) {
    $pids = @()
    foreach ($line in (netstat -ano | Select-String ":$p\s")) {
        $m = [regex]::Match($line, 'LISTENING\s+(\d+)')
        if ($m.Success) { $pids += $m.Groups[1].Value }
    }
    return $pids | Select-Object -Unique
}

$existing = Get-PortPids $BackendPort
if ($existing) {
    # 必须是本仓的后端命令，不能仅凭 python 进程名判断所有权。
    $procNames = ($existing | ForEach-Object {
        (Get-Process -Id $_ -ErrorAction SilentlyContinue).ProcessName
    }) -join ", "
    Write-DevText "  [提示] 端口 $BackendPort 已被占用 (PID: $($existing -join ', ') | 进程: $procNames)" -ForegroundColor Yellow
    if (@($existing | Where-Object { [int]$_ -notin $projectBackendPids }).Count -gt 0) {
        Write-DevText "  [错误] 占用者不是 DubForge (是其他程序), 不能自动处理。" -ForegroundColor Red
        Write-DevText "  解决: 使用其他端口启动 — .\scripts\dev.ps1 -BackendPort 8610" -ForegroundColor Yellow
        Read-DevInput "  回车退出"; exit 1
    }
    Write-DevText "    [1] 直接连接现有实例 (不重启, 保留运行中的任务; 后端代码不会更新)"
    Write-DevText "    [2] 结束现有实例并重新启动 (加载当前代码; 会中断该实例任务)"
    Write-DevText "    [3] 退出"
    $ans = Read-DevInput "  选择 [1/2/3, 回车=1]"
    if ($ans -eq "3") { exit 0 }
    if ($ans -eq "2") {
        foreach ($epid in $existing) { Stop-DevProcessTree -Process (Get-Process -Id $epid -ErrorAction SilentlyContinue) -Name '旧后端' }
        Start-Sleep -Seconds 1
        if (Get-PortPids $BackendPort) {
            Write-DevText "  [错误] 旧实例无法结束, 请手动关闭后重试。" -ForegroundColor Red
            Read-DevInput "  回车退出"; exit 1
        }
        Write-DevText "  旧实例已结束, 开始重新启动..." -ForegroundColor Green
    } else {
        # 复用现有后端: 只起前端。旧后端可能没有 --reload，前端热更新不会让 API 代码更新。
        Write-DevText "  [提示] 将复用已运行的后端 $BackendPort，只启动前端开发服务器。" -ForegroundColor Yellow
        Write-DevText "  [提示] 如果页面仍显示旧参数或旧流程，这是因为后端进程未重新加载当前代码；需要重新启动时请选择 [2]。" -ForegroundColor Yellow
        Write-DevText "  [提示] 服务窗口默认隐藏，后端日志: $LogDir\backend.err.log" -ForegroundColor DarkGray
        Write-DevText '  [3/4] 正在启动前端，后端沿用现有实例…' -ForegroundColor DarkGray
        $frontend = $null
        $devState = @{
            BackendProcess = $null
            FrontendProcess = $null
            BackendPort = $BackendPort
            FrontendPort = $FrontendPort
            BackendReused = $true
            ReloadBackend = [bool]$ReloadBackend
            StartupTimeoutSec = $StartupTimeoutSec
        }
        try {
        $frontend = Start-DevService -Title 'DubForge Frontend' -WorkingDirectory "$Root\frontend" `
            -Command @('npm', 'run', 'dev', '--', '--port', $FrontendPort, '--strictPort') -LogName 'frontend' `
            -Environment @{ DUBFORGE_BACKEND_PORT = $BackendPort; DUBFORGE_FRONTEND_PORT = $FrontendPort }
        $devState.FrontendProcess = $frontend
        $ready = Wait-DevReady -FrontendProcess $frontend -BackendPort $BackendPort -FrontendPort $FrontendPort -BackendReused -StartupTimeoutSec $StartupTimeoutSec
        $okF = $ready.Frontend
        if ($okF) {
            Write-DevText "  [OK] 前端 http://127.0.0.1:$FrontendPort (后端复用 $BackendPort 现有实例)" -ForegroundColor Green
            Write-DevText "  [提示] 当前页面的 API 来自已运行的后端；若要使用最新后端代码，请停止后重新选择 [2]。" -ForegroundColor Yellow
            if (-not $NoBrowser) { [void](Open-DevPage -BackendPort $BackendPort -FrontendPort $FrontendPort) }
        } elseif ($frontend.HasExited) {
            Write-DevText "  [失败] 前端进程已退出，请查看 frontend.err.log。" -ForegroundColor Red
        } else { Write-DevText "  [未就绪] 前端页面及 API 代理仍未通过检查，请用 status / logs frontend 查看。" -ForegroundColor Yellow }
        Write-DevText ""
        Write-DevText "  前端服务日志: $LogDir\frontend.err.log" -ForegroundColor DarkGray
            Enter-DevControlPanel -State $devState `
                -BackendPort $BackendPort -FrontendPort $FrontendPort -LogDir $LogDir `
                -ShowServiceWindows:$ShowServiceWindows -BackendReused
        } finally {
            Stop-DevServices -State $devState `
                -BackendPort $BackendPort -FrontendPort $FrontendPort -KeepBackend
        }
        exit 0
    }
}

# ---- 后端 (配置端口；自动重载需显式开启) ----
$backend = $null; $frontend = $null
$devState = @{
    BackendProcess = $null
    FrontendProcess = $null
    BackendPort = $BackendPort
    FrontendPort = $FrontendPort
    BackendReused = $false
    ReloadBackend = [bool]$ReloadBackend
    StartupTimeoutSec = $StartupTimeoutSec
}
try {
Write-DevText '  [3/4] 正在启动后端与前端，输出写入日志…' -ForegroundColor DarkGray
if ($ReloadBackend) {
    Write-DevText '  后端模式：自动重载；修改后端代码会重启服务及运行中的任务。' -ForegroundColor Yellow
} else {
    Write-DevText '  后端模式：稳定运行；修改后端代码后用 restart backend 载入。' -ForegroundColor DarkGray
}
$backend = Start-DevService -Title 'DubForge Backend' -WorkingDirectory "$Root\backend" `
    -Command (Get-DevBackendCommand -Port $BackendPort -ReloadBackend:$ReloadBackend) -LogName 'backend' -Environment @{ PYTHONUNBUFFERED = '1'; PYTHONIOENCODING = 'utf-8' }
$devState.BackendProcess = $backend

# ---- 前端 (配置端口, 热更新) ----
$frontend = Start-DevService -Title 'DubForge Frontend' -WorkingDirectory "$Root\frontend" `
    -Command @('npm', 'run', 'dev', '--', '--port', $FrontendPort, '--strictPort') -LogName 'frontend' `
    -Environment @{ DUBFORGE_BACKEND_PORT = $BackendPort; DUBFORGE_FRONTEND_PORT = $FrontendPort }
$devState.FrontendProcess = $frontend

# ---- 等待 HTTP 就绪，持续显示状态，失败时带出日志 ----
$ready = Wait-DevReady -BackendProcess $backend -FrontendProcess $frontend -BackendPort $BackendPort -FrontendPort $FrontendPort -StartupTimeoutSec $StartupTimeoutSec
$okB = $ready.Backend; $okF = $ready.Frontend
if ($okB) {
    Write-DevText "  [OK] 后端 http://127.0.0.1:$BackendPort" -ForegroundColor Green
} elseif ($backend.HasExited) {
    Write-DevText "  [失败] 后端 $BackendPort 进程已退出 (查看 $LogDir\backend.err.log)" -ForegroundColor Red
} else { Write-DevText "  [未就绪] 后端 $BackendPort 仍在加载或恢复，请用 status / logs backend 查看。" -ForegroundColor Yellow }
if ($okF) {
    Write-DevText "  [OK] 前端 http://127.0.0.1:$FrontendPort" -ForegroundColor Green
} elseif ($frontend.HasExited) {
    Write-DevText "  [失败] 前端 $FrontendPort 进程已退出 (查看 $LogDir\frontend.err.log)" -ForegroundColor Red
} else { Write-DevText "  [未就绪] 前端 $FrontendPort 页面及 API 代理仍未通过检查，请用 status / logs frontend 查看。" -ForegroundColor Yellow }
if ($okB -and $okF -and -not $NoBrowser) { [void](Open-DevPage -BackendPort $BackendPort -FrontendPort $FrontendPort) }

Write-DevText ""
Write-DevText "  后端 PID=$($backend.Id)  前端 PID=$($frontend.Id)"
if ($ShowServiceWindows) {
    Write-DevText "  已显示后端/前端服务窗口；本窗口是控制面板，服务窗口只用于观察原始输出。"
} else {
    Write-DevText "  服务窗口已隐藏; 日志目录: $LogDir"
    Write-DevText "  查看日志: Get-Content '$LogDir\backend.err.log' -Wait"
    Write-DevText "  调试显示服务窗口: .\scripts\dev.ps1 -ShowServiceWindows"
}
    Enter-DevControlPanel -State $devState `
        -BackendPort $BackendPort -FrontendPort $FrontendPort -LogDir $LogDir -ShowServiceWindows:$ShowServiceWindows
} finally {
    Stop-DevServices -State $devState `
        -BackendPort $BackendPort -FrontendPort $FrontendPort
}
