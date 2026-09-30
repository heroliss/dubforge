# DubForge 开发环境一键启动 (前后端 + 浏览器)
# 用法: 双击根目录 StartDev.bat, 或 .\scripts\dev.ps1
#       需已跑过: backend/uv sync 与 frontend/npm install
param(
    [int]$BackendPort = 0,      # 0 = 读 config.yaml 的 server.port
    [int]$FrontendPort = 5273,   # Vite 开发端口
    [switch]$ShowServiceWindows, # 调试时显示后端/前端窗口; 默认只保留一个控制台
    [switch]$NoBrowser          # 脚本验证或只需控制台时不自动打开浏览器
)
$ErrorActionPreference = "Stop"
# 已有逐阶段状态输出，关闭 PowerShell 自带进度层，避免重绘中文时出现叠字。
$ProgressPreference = 'SilentlyContinue'
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
. (Join-Path $PSScriptRoot "dev-output.ps1")
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
    $envAssignments = foreach ($entry in $Environment.GetEnumerator()) {
        "`$env:$($entry.Key)='$([string]$entry.Value -replace "'", "''")';"
    }
    $quotedCommand = ($Command | ForEach-Object { "'" + ($_ -replace "'", "''") + "'" }) -join ' '
    $commandText = "`$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new(`$false);" + ($envAssignments -join '') + "`$Host.UI.RawUI.WindowTitle='$($Title -replace "'", "''")'; Set-Location -LiteralPath '$($WorkingDirectory -replace "'", "''")'; & " + $quotedCommand
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
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($commandText))
    $args = @("-NoProfile", "-EncodedCommand", $encoded)
    if ($ShowServiceWindows) {
        return Start-Process -PassThru -FilePath powershell -ArgumentList $args
    }
    # 隐藏进程不能继承主控制台输入，否则 PowerShell/服务会争抢按键，主窗口无法输入。
    # Windows PowerShell 会独占重定向输入文件，因此前后端各用自己的空文件。
    # 隐藏服务读到 EOF；调试时显示的独立窗口仍可正常交互。
    $stdin = Join-Path $LogDir "$LogName.stdin.empty"
    [IO.File]::WriteAllText($stdin, '')
    return Start-Process -PassThru -WindowStyle Hidden -FilePath powershell -ArgumentList (@('-NonInteractive') + $args) `
        -RedirectStandardInput $stdin -RedirectStandardOutput $stdout -RedirectStandardError $stderr
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
}

function Show-DevStatus {
    param(
        [System.Diagnostics.Process]$BackendProcess,
        [System.Diagnostics.Process]$FrontendProcess,
        [int]$BackendPort,
        [int]$FrontendPort,
        [switch]$BackendReused
    )
    $backendAlive = if ($BackendProcess) { [bool](Get-Process -Id $BackendProcess.Id -ErrorAction SilentlyContinue) } else { $true }
    $frontendAlive = if ($FrontendProcess) { [bool](Get-Process -Id $FrontendProcess.Id -ErrorAction SilentlyContinue) } else { $false }
    $backendListening = @(Get-NetTCPConnection -State Listen -LocalPort $BackendPort -ErrorAction SilentlyContinue).Count -gt 0
    $frontendListening = @(Get-NetTCPConnection -State Listen -LocalPort $FrontendPort -ErrorAction SilentlyContinue).Count -gt 0
    $backendState = if ($backendAlive -and $backendListening) {
        if ($BackendReused) { "运行中（复用实例）" } else { "运行中" }
    } elseif ($backendAlive) {
        if ($BackendReused) { "复用实例，端口未就绪" } else { "进程存在，端口未就绪" }
    } else { "已退出" }
    $frontendState = if ($frontendAlive -and $frontendListening) { "运行中" } elseif ($frontendAlive) { "进程存在，端口未就绪" } else { "已退出" }
    $backendPid = if ($BackendProcess) { $BackendProcess.Id } else { "外部实例" }
    $frontendPid = if ($FrontendProcess) { $FrontendProcess.Id } else { "未知" }
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
        default { @("backend", "frontend") }
    }
    foreach ($name in $names) {
        Write-DevText "`n  --- $name 最近日志 ---" -ForegroundColor DarkCyan
        foreach ($suffix in @("out", "err")) {
            $path = Join-Path $LogDir "$name.$suffix.log"
            if (Test-Path $path) {
                Write-DevText "  [$suffix] $path" -ForegroundColor DarkGray
                $lines = @(Get-Content -LiteralPath $path -Tail 12 -ErrorAction SilentlyContinue)
                if ($lines.Count -gt 0) { $lines | ForEach-Object { Write-DevText "    $_" } }
                else { Write-DevText "    （暂无内容）" -ForegroundColor DarkGray }
            }
        }
    }
}

function Wait-DevReady {
    param($BackendProcess, $FrontendProcess, [int]$BackendPort, [int]$FrontendPort, [switch]$BackendReused)
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $okB = [bool]$BackendReused; $okF = $false; $lastReport = -3
    Write-DevText '  [4/4] 等待服务响应，首次加载可能稍慢…' -ForegroundColor DarkGray
    while ($clock.Elapsed.TotalSeconds -lt 40) {
        if (-not $okB) {
            try { $health = Invoke-RestMethod "http://127.0.0.1:$BackendPort/api/health" -TimeoutSec 1; $okB = $health.ok -and $health.app -eq 'DubForge' } catch {}
        }
        if (-not $okF) {
            try { $response = Invoke-WebRequest "http://127.0.0.1:$FrontendPort" -UseBasicParsing -TimeoutSec 1; $okF = $response.StatusCode -eq 200 } catch {}
        }
        if ($okB -and $okF) { break }
        if (($BackendProcess -and $BackendProcess.HasExited) -or ($FrontendProcess -and $FrontendProcess.HasExited)) {
            Write-DevText '  [失败] 服务进程提前退出，下面显示最近日志。' -ForegroundColor Red
            break
        }
        if ($clock.Elapsed.TotalSeconds - $lastReport -ge 2) {
            $lastReport = $clock.Elapsed.TotalSeconds
            $backendText = if ($okB) { '已就绪' } else { '加载中' }
            $frontendText = if ($okF) { '已就绪' } else { '加载中' }
            Write-DevText "  已等待 $([int]$clock.Elapsed.TotalSeconds) 秒 · 后端 $backendText · 前端 $frontendText" -ForegroundColor DarkGray
        }
        Start-Sleep -Milliseconds 300
    }
    if (-not $okB) { Show-DevLogs -LogDir $LogDir -Target backend }
    if (-not $okF) { Show-DevLogs -LogDir $LogDir -Target frontend }
    return @{ Backend = $okB; Frontend = $okF }
}

. (Join-Path $PSScriptRoot "dev-console.ps1")

function Stop-DevServices {
    param(
        [System.Diagnostics.Process]$BackendProcess,
        [System.Diagnostics.Process]$FrontendProcess,
        [int]$BackendPort,
        [int]$FrontendPort,
        [switch]$KeepBackend
    )
    Stop-DevProcessTree -Process $FrontendProcess -Name "前端"
    if (-not $KeepBackend) { Stop-DevProcessTree -Process $BackendProcess -Name "后端" }
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
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

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
        try {
        $frontend = Start-DevService -Title 'DubForge Frontend' -WorkingDirectory "$Root\frontend" `
            -Command @('npm', 'run', 'dev', '--', '--port', $FrontendPort, '--strictPort') -LogName 'frontend' `
            -Environment @{ DUBFORGE_BACKEND_PORT = $BackendPort; DUBFORGE_FRONTEND_PORT = $FrontendPort }
        $ready = Wait-DevReady -FrontendProcess $frontend -BackendPort $BackendPort -FrontendPort $FrontendPort -BackendReused
        $okF = $ready.Frontend
        if ($okF) {
            Write-DevText "  [OK] 前端 http://127.0.0.1:$FrontendPort (后端复用 $BackendPort 现有实例)" -ForegroundColor Green
            Write-DevText "  [提示] 当前页面的 API 来自已运行的后端；若要使用最新后端代码，请停止后重新选择 [2]。" -ForegroundColor Yellow
            if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$FrontendPort" }
        } else {
            Write-DevText "  [失败] 前端未就绪" -ForegroundColor Red
        }
        Write-DevText ""
        Write-DevText "  前端服务日志: $LogDir\frontend.err.log" -ForegroundColor DarkGray
            Enter-DevControlPanel -BackendProcess $null -FrontendProcess $frontend `
                -BackendPort $BackendPort -FrontendPort $FrontendPort -LogDir $LogDir `
                -ShowServiceWindows:$ShowServiceWindows -BackendReused
        } finally {
            Stop-DevServices -BackendProcess $null -FrontendProcess $frontend `
                -BackendPort $BackendPort -FrontendPort $FrontendPort -KeepBackend
        }
        exit 0
    }
}

# ---- 后端 (配置端口, reload) ----
$backend = $null; $frontend = $null
try {
Write-DevText '  [3/4] 正在启动后端与前端，输出写入日志…' -ForegroundColor DarkGray
$backend = Start-DevService -Title 'DubForge Backend' -WorkingDirectory "$Root\backend" `
    -Command @('uv', 'run', 'uvicorn', 'app.main:app', '--port', $BackendPort, '--reload', '--timeout-graceful-shutdown', '5') -LogName 'backend' -Environment @{ PYTHONUNBUFFERED = '1'; PYTHONIOENCODING = 'utf-8' }

# ---- 前端 (配置端口, 热更新) ----
$frontend = Start-DevService -Title 'DubForge Frontend' -WorkingDirectory "$Root\frontend" `
    -Command @('npm', 'run', 'dev', '--', '--port', $FrontendPort, '--strictPort') -LogName 'frontend' `
    -Environment @{ DUBFORGE_BACKEND_PORT = $BackendPort; DUBFORGE_FRONTEND_PORT = $FrontendPort }

# ---- 等待 HTTP 就绪，持续显示状态，失败时带出日志 ----
$ready = Wait-DevReady -BackendProcess $backend -FrontendProcess $frontend -BackendPort $BackendPort -FrontendPort $FrontendPort
$okB = $ready.Backend; $okF = $ready.Frontend
if ($okB) {
    Write-DevText "  [OK] 后端 http://127.0.0.1:$BackendPort" -ForegroundColor Green
} elseif ($ShowServiceWindows) {
    Write-DevText "  [失败] 后端 $BackendPort 未就绪 (看 Backend 窗口报错)" -ForegroundColor Red
} else {
    Write-DevText "  [失败] 后端 $BackendPort 未就绪 (查看 $LogDir\backend.err.log)" -ForegroundColor Red
}
if ($okF) {
    Write-DevText "  [OK] 前端 http://127.0.0.1:$FrontendPort" -ForegroundColor Green
} elseif ($ShowServiceWindows) {
    Write-DevText "  [失败] 前端 $FrontendPort 未就绪 (看 Frontend 窗口报错)" -ForegroundColor Red
} else {
    Write-DevText "  [失败] 前端 $FrontendPort 未就绪 (查看 $LogDir\frontend.err.log)" -ForegroundColor Red
}
if ($okB -and $okF -and -not $NoBrowser) { Start-Process "http://127.0.0.1:$FrontendPort" }

Write-DevText ""
Write-DevText "  后端 PID=$($backend.Id)  前端 PID=$($frontend.Id)"
if ($ShowServiceWindows) {
    Write-DevText "  已显示后端/前端服务窗口；本窗口是控制面板，服务窗口只用于观察原始输出。"
} else {
    Write-DevText "  服务窗口已隐藏; 日志目录: $LogDir"
    Write-DevText "  查看日志: Get-Content '$LogDir\backend.err.log' -Wait"
    Write-DevText "  调试显示服务窗口: .\scripts\dev.ps1 -ShowServiceWindows"
}
    Enter-DevControlPanel -BackendProcess $backend -FrontendProcess $frontend `
        -BackendPort $BackendPort -FrontendPort $FrontendPort -LogDir $LogDir -ShowServiceWindows:$ShowServiceWindows
} finally {
    Stop-DevServices -BackendProcess $backend -FrontendProcess $frontend `
        -BackendPort $BackendPort -FrontendPort $FrontendPort
}
