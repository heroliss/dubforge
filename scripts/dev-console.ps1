. (Join-Path $PSScriptRoot "dev-output.ps1")
# 主控制台命令；服务操作调用当前后端，沿用网页的配置和配音占用保护。
. (Join-Path $PSScriptRoot "dev-web.ps1")
. (Join-Path $PSScriptRoot "dev-owner.ps1")
function Get-DevBackendCommand {
    param([int]$Port, [switch]$ReloadBackend)
    # Windows 普通启动与重载均显式使用 Proactor，避免 select 的 512 连接上限。
    $command = @('uv', 'run', 'uvicorn', 'app.main:app', '--port', [string]$Port,
        '--loop', 'app.event_loop:create_event_loop', '--timeout-graceful-shutdown', '5')
    if ($ReloadBackend) { $command += '--reload' }
    return $command
}

function Show-DevHelp {
    Write-DevText '  可用命令：' -ForegroundColor Cyan
    Write-DevText '    status / 状态                 查看前后端状态'
    Write-DevText '    start <backend|frontend|all>  启动前端/后端/两者'
    Write-DevText '    stop <backend|frontend|all>   停止本次启动的前端/后端'
    Write-DevText '    restart <backend|frontend|all> 重启前端/后端/两者'
    Write-DevText '    services                      查看全部配音服务及服务标识'
    Write-DevText '    start <服务标识>              启动，例如 start indextts25'
    Write-DevText '    stop <服务标识>               停止，例如 stop gptsovits'
    Write-DevText '    logs [backend|frontend|launcher|服务标识] 查看最近日志'
    Write-DevText '      launcher 记录启动、端口和正常退出原因' -ForegroundColor DarkGray
    Write-DevText '    watch <backend|frontend|服务标识> 跟随完整日志；Esc / Q 返回'
    Write-DevText '    open / 打开                   重新打开网页'
    Write-DevText '    help / 帮助                   查看本说明'
    Write-DevText '    quit / exit / stop(无参数) / 退出 / 停止  停止本次前后端并退出'
    Write-DevText '    Ctrl+C                        同样退出；配音服务继续运行'
    Write-DevText '  配音服务需单独 stop <服务标识>；复用的后端也会保留。' -ForegroundColor DarkGray
}

function Invoke-DevServiceApi {
    param([int]$BackendPort, [string]$Target = '', [string]$Action = '')
    $url = "http://127.0.0.1:$BackendPort/api/services"
    if ($Target) { $url += '/' + [uri]::EscapeDataString($Target) }
    if ($Action) { $url += '/' + $Action }
    $method = if ($Action -in @('start', 'stop')) { 'Post' } else { 'Get' }
    try { return Invoke-RestMethod -Uri $url -Method $method -TimeoutSec 30 }
    catch {
        $detail = $null
        try { $detail = ($_.ErrorDetails.Message | ConvertFrom-Json).message } catch {}
        if (-not $detail) { $detail = '无法调用服务管理接口；请确认后端已启动并已重启到当前版本。' }
        throw $detail
    }
}

function Show-DevServiceState {
    param($Service, [string]$Name)
    $states = @{ online = '已连接'; offline = '未连接'; starting = '启动中'; failed = '已退出'; unknown = '未检测' }
    $state = $states[$Service.status]
    $owner = if ($Service.managed) { "DubForge 管理，PID=$($Service.pid)" } else { '外部服务或尚未启动' }
    Write-DevText "  $Name  $state  $($Service.url)  [$owner]" -ForegroundColor Cyan
    if ($Service.message) { Write-DevText "    $($Service.message)" }
    if ($Service.log_path) { Write-DevText "    日志：$($Service.log_path)" -ForegroundColor DarkGray }
}

function Watch-DevLogs {
    param([string[]]$Paths)
    if ([Console]::IsInputRedirected) { throw '日志跟随需要交互控制台；重定向输入时请使用 logs。' }
    $offsets = @{}
    Write-DevText '  正在跟随日志；Esc 或 Q 返回控制台，Ctrl+C 会退出启动器。' -ForegroundColor Green
    while ($true) {
        foreach ($path in $Paths) {
            if (-not (Test-Path -LiteralPath $path)) { continue }
            $stream = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)
            $reader = $null
            try {
                if (-not $offsets.ContainsKey($path)) { $offsets[$path] = [Math]::Max(0, $stream.Length - 16384) }
                if ($offsets[$path] -gt $stream.Length) { $offsets[$path] = 0 }
                [void]$stream.Seek($offsets[$path], [IO.SeekOrigin]::Begin)
                $reader = New-Object IO.StreamReader($stream, [Text.Encoding]::UTF8)
                $text = $reader.ReadToEnd()
                $offsets[$path] = $stream.Position
                if ($text) {
                    Write-DevText "`n  [$([IO.Path]::GetFileName($path))]" -ForegroundColor DarkGray
                    Write-DevText $text -NoNewline
                }
            } finally {
                if ($reader) { $reader.Dispose() } else { $stream.Dispose() }
            }
        }
        if ([Console]::KeyAvailable) {
            $key = [Console]::ReadKey($true)
            if ($key.Key -in @([ConsoleKey]::Escape, [ConsoleKey]::Q)) { Write-DevText ''; return }
        }
        Start-Sleep -Milliseconds 300
    }
}

function Get-DevManagedProcess {
    param([hashtable]$State, [ValidateSet('backend','frontend')][string]$Name)
    $process = $State["$($Name)Process"]
    $State.Remove("$($Name)OwnerError")
    if ($process) {
        try { if (-not $process.HasExited) { return $process } } catch {}
    }
    if (-not $Root -or ($Name -eq 'backend' -and $State.BackendReused)) { return $null }
    try {
        $restored = Resolve-DevServiceOwner -State $State -Name $Name
        if ($restored) {
            $State["$($Name)Process"] = $restored
            Write-DevText "  已恢复 $Name 的登记管理 (PID=$($restored.Id))。" -ForegroundColor DarkGray
        }
        return $restored
    } catch {
        $State["$($Name)OwnerError"] = [string]$_
        Write-DevText "  [警告] $Name 身份核验失败，不接管进程：$_" -ForegroundColor Yellow
        return $null
    }
}

function Start-DevConsoleTarget {
    param([hashtable]$State, [ValidateSet('backend','frontend','all')][string]$Target)
    $targets = if ($Target -eq 'all') { @('backend','frontend') } else { @($Target) }
    foreach ($name in $targets) {
        $existing = Get-DevManagedProcess -State $State -Name $name
        if ($State.ContainsKey("$($name)OwnerError")) {
            Write-DevText "  [错误] 请先核对 $name 的服务登记；未启动新进程。" -ForegroundColor Red
            continue
        }
        if ($existing) {
            Write-DevText "  $name 已在运行 (PID=$($existing.Id))。" -ForegroundColor DarkGray
            continue
        }
        $port = if ($name -eq 'backend') { [int]$State.BackendPort } else { [int]$State.FrontendPort }
        $listener = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
        if ($name -eq 'backend' -and $State.BackendReused -and $listener.Count -gt 0) {
            Write-DevText "  后端 $($State.BackendPort) 是复用的外部实例，控制台不会接管；需要重启请退出后重新启动并选择重启现有实例。" -ForegroundColor Yellow
            continue
        }
        if ($name -eq 'backend' -and $State.BackendReused -and $listener.Count -eq 0) {
            Write-DevText "  复用的后端已不再监听 $port，将重新启动本控制台管理的后端。" -ForegroundColor Yellow
            $State.BackendReused = $false
        }
        if ($listener.Count -gt 0) {
            Write-DevText "  [错误] 端口 $port 已被其他进程占用，未启动 $name；不会强制结束未知进程。" -ForegroundColor Red
            continue
        }
        if ($name -eq 'backend') {
            Write-DevText "  正在启动后端 $port…" -ForegroundColor DarkGray
            $State.BackendProcess = Start-DevService -Title 'DubForge Backend' -WorkingDirectory "$Root\backend" `
                -Command (Get-DevBackendCommand -Port $port -ReloadBackend:([bool]$State.ReloadBackend)) -LogName 'backend' `
                -Environment @{ PYTHONUNBUFFERED = '1'; PYTHONIOENCODING = 'utf-8' }
            $State.BackendReused = $false
        } else {
            Write-DevText "  正在启动前端 $port…" -ForegroundColor DarkGray
            $State.FrontendProcess = Start-DevService -Title 'DubForge Frontend' -WorkingDirectory "$Root\frontend" `
                -Command @('npm', 'run', 'dev', '--', '--port', $port, '--strictPort') -LogName 'frontend' `
                -Environment @{ DUBFORGE_BACKEND_PORT = [int]$State.BackendPort; DUBFORGE_FRONTEND_PORT = $port }
        }
        $startupTimeout = if ($State.ContainsKey('StartupTimeoutSec')) { [int]$State.StartupTimeoutSec } else { 180 }
        $ready = Wait-DevReady -BackendProcess $State.BackendProcess -FrontendProcess $State.FrontendProcess `
            -BackendPort $State.BackendPort -FrontendPort $State.FrontendPort -BackendReused:$State.BackendReused -Target $name -StartupTimeoutSec $startupTimeout
        $ok = if ($name -eq 'backend') { $ready.Backend } else { $ready.Frontend }
        if ($ok) { Write-DevText "  [完成] $name 已就绪。" -ForegroundColor Green }
        elseif ($State["$($name)Process"].HasExited) { Write-DevText "  [失败] $name 进程已退出；请用 logs $name 查看。" -ForegroundColor Red }
        else { Write-DevText "  [未就绪] $name 尚未通过就绪检查，可能仍在加载；请用 status / logs $name 查看，稍后输入 open。" -ForegroundColor Yellow }
    }
}

function Stop-DevConsoleTarget {
    param([hashtable]$State, [ValidateSet('backend','frontend','all')][string]$Target)
    $targets = if ($Target -eq 'all') { @('frontend','backend') } else { @($Target) }
    foreach ($name in $targets) {
        $process = Get-DevManagedProcess -State $State -Name $name
        if (-not $process) {
            if ($name -eq 'backend' -and $State.BackendReused) {
                Write-DevText "  后端是复用的外部实例，未停止；这样可以避免中断其正在运行的项目任务。" -ForegroundColor Yellow
            } else { Write-DevText "  $name 当前没有由本控制台管理的运行进程。" -ForegroundColor DarkGray }
            continue
        }
        Stop-DevProcessTree -Process $process -Name $name
        $State["$($name)Process"] = $null
        if ($name -eq 'backend') { $State.BackendReused = $false }
    }
}

function Invoke-DevConsoleServiceControl {
    param([hashtable]$State, [string]$Action, [string]$Target)
    $aliases = @{
        '后端'='backend'; 'backend'='backend'; 'api'='backend'
        '前端'='frontend'; 'frontend'='frontend'; 'web'='frontend'
        '全部'='all'; 'all'='all'; 'both'='all'
    }
    $key = $Target.Trim().ToLowerInvariant()
    if (-not $aliases.ContainsKey($key)) { throw "目标必须是 backend、frontend 或 all；当前为 $Target。" }
    $normalized = $aliases[$key]
    switch ($Action) {
        'start' { Start-DevConsoleTarget -State $State -Target $normalized }
        'stop' { Stop-DevConsoleTarget -State $State -Target $normalized }
        'restart' {
            Stop-DevConsoleTarget -State $State -Target $normalized
            Start-Sleep -Milliseconds 500
            Start-DevConsoleTarget -State $State -Target $normalized
        }
    }
}

function Enter-DevControlPanel {
    param($BackendProcess, $FrontendProcess, [int]$BackendPort, [int]$FrontendPort,
          [string]$LogDir, [switch]$ShowServiceWindows, [switch]$BackendReused,
          [hashtable]$State)
    if (-not $State) {
        $State = @{
            BackendProcess = $BackendProcess
            FrontendProcess = $FrontendProcess
            BackendPort = $BackendPort
            FrontendPort = $FrontendPort
            BackendReused = [bool]$BackendReused
        }
    } else {
        if (-not $State.ContainsKey('BackendPort')) { $State.BackendPort = $BackendPort }
        if (-not $State.ContainsKey('FrontendPort')) { $State.FrontendPort = $FrontendPort }
        if (-not $State.ContainsKey('BackendReused')) { $State.BackendReused = [bool]$BackendReused }
    }
    if ($BackendPort -le 0) { $BackendPort = [int]$State.BackendPort }
    if ($FrontendPort -le 0) { $FrontendPort = [int]$State.FrontendPort }
    Write-DevText "`n  DubForge 统一控制台" -ForegroundColor Cyan
    Write-DevText '  前后端和配音服务的输出都保留在日志中；隐藏窗口不等于丢弃日志。'
    Write-DevText '  若服务需要交互式安装、登录或输入确认，请先在其原始终端中完成。' -ForegroundColor DarkGray
    Write-DevText '  配音服务：indextts25、gptsovits；项目独立地址可用 services 查看。'
    Show-DevHelp
    while ($true) {
        if ([Console]::IsInputRedirected) {
            $raw = [Console]::ReadLine()
        } else { $raw = Read-DevInput "`n  DubForge>" }
        if ($null -eq $raw) {
            Write-DevText '  控制台输入已结束，正在退出并停止本次前后端。'
            Write-DevLifecycle '控制台输入 EOF，退出'
            return
        }
        $command = $raw.Trim().ToLowerInvariant()
        try {
            switch -Regex ($command) {
                '^(quit|exit|stop|退出|停止)$' {
                    Write-DevLifecycle "收到退出命令：$command"
                    Write-DevText '  正在关闭本次前后端；独立配音服务和复用的后端保持运行。' -ForegroundColor Yellow
                    return
                }
                '^(help|帮助|菜单)$|^$' { Show-DevHelp; continue }
                '^(status|状态)$' {
                    Show-DevStatus -BackendProcess $State.BackendProcess -FrontendProcess $State.FrontendProcess -BackendPort $BackendPort -FrontendPort $FrontendPort -BackendReused:$State.BackendReused
                    continue
                }
                '^(start|stop|restart)\s+(backend|frontend|all|both|api|web|后端|前端|全部)$' {
                    Invoke-DevConsoleServiceControl -State $State -Action $Matches[1] -Target $Matches[2]
                    continue
                }
                '^services$' {
                    Write-DevText '  正在检测配音服务…' -ForegroundColor DarkGray
                    $result = Invoke-DevServiceApi -BackendPort $BackendPort
                    foreach ($item in $result.services) { Show-DevServiceState -Service $item -Name $item.id }
                    continue
                }
                '^(start|stop)\s+(\S+)$' {
                    $action = $Matches[1]; $target = $Matches[2]
                    Write-DevText "  正在处理 $action $target…" -ForegroundColor DarkGray
                    $result = Invoke-DevServiceApi -BackendPort $BackendPort -Target $target -Action $action
                    Show-DevServiceState -Service $result -Name $target
                    if ($result.status -eq 'starting') { Write-DevText "  输入 watch $target 查看加载过程；services 可检查是否就绪。" }
                    continue
                }
                '^(logs?|日志|watch)(\s+(\S+))?$' {
                    $action = $Matches[1]
                    $target = if ($Matches[3]) { $Matches[3] } else { 'all' }
                    if ($target -in @('all', 'backend', 'frontend', 'launcher', '后端', '前端', '启动器')) {
                        if ($action -ne 'watch') { Show-DevLogs -LogDir $LogDir -Target $target; continue }
                        $names = if ($target -in @('backend', '后端')) { @('backend') } elseif ($target -in @('frontend', '前端')) { @('frontend') } elseif ($target -in @('launcher', '启动器')) { @('launcher') } else { @('backend', 'frontend') }
                        $paths = foreach ($name in $names) { if ($name -eq 'launcher') { Join-Path $LogDir 'launcher.log' } else { foreach ($suffix in @('out', 'err')) { Join-Path $LogDir "$name.$suffix.log" } } }
                        Watch-DevLogs -Paths $paths
                    } else {
                        $result = Invoke-DevServiceApi -BackendPort $BackendPort -Target $target -Action 'log'
                        Write-DevText "  日志：$($result.path)" -ForegroundColor DarkGray
                        if ($action -eq 'watch') { Watch-DevLogs -Paths @($result.path) }
                        else {
                            if ($result.truncated) { Write-DevText '  以下显示末尾 64 KB，完整内容保留在上方文件。' -ForegroundColor DarkGray }
                            Write-DevText $result.text
                        }
                    }
                    continue
                }
                '^(open|打开)$' { [void](Open-DevPage -BackendPort $BackendPort -FrontendPort $FrontendPort); continue }
                default { Write-DevText "  未识别命令：$command。输入 help 查看用法。" -ForegroundColor Yellow }
            }
        } catch { Write-DevText "  [错误] $($_.Exception.Message)" -ForegroundColor Red }
    }
}
