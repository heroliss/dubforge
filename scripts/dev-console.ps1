. (Join-Path $PSScriptRoot "dev-output.ps1")
# 主控制台命令；服务操作调用当前后端，沿用网页的配置和配音占用保护。
function Show-DevHelp {
    Write-DevText '  可用命令：' -ForegroundColor Cyan
    Write-DevText '    status / 状态                 查看前后端状态'
    Write-DevText '    services                      查看全部配音服务及服务标识'
    Write-DevText '    start <服务标识>              启动，例如 start indextts25'
    Write-DevText '    stop <服务标识>               停止，例如 stop gptsovits'
    Write-DevText '    logs [backend|frontend|服务标识] 查看最近日志，默认前后端'
    Write-DevText '    watch <backend|frontend|服务标识> 跟随完整日志；Esc / Q 返回'
    Write-DevText '    open / 打开                   重新打开网页'
    Write-DevText '    help / 帮助                   查看本说明'
    Write-DevText '    quit / exit / stop / 退出 / 停止  停止本次前后端并退出'
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

function Enter-DevControlPanel {
    param($BackendProcess, $FrontendProcess, [int]$BackendPort, [int]$FrontendPort,
          [string]$LogDir, [switch]$ShowServiceWindows, [switch]$BackendReused)
    Write-DevText "`n  DubForge 统一控制台" -ForegroundColor Cyan
    Write-DevText '  前后端和配音服务的输出都保留在日志中；隐藏窗口不等于丢弃日志。'
    Write-DevText '  若服务需要交互式安装、登录或输入确认，请先在其原始终端中完成。' -ForegroundColor DarkGray
    Write-DevText '  配音服务：indextts25、gptsovits；项目独立地址可用 services 查看。'
    Show-DevHelp
    while ($true) {
        if ([Console]::IsInputRedirected) {
            $raw = [Console]::ReadLine()
            if ($null -eq $raw) { Write-DevText '  输入流已结束，正在退出。'; return }
        } else { $raw = Read-DevInput "`n  DubForge>" }
        $command = $raw.Trim().ToLowerInvariant()
        try {
            switch -Regex ($command) {
                '^(quit|exit|stop|退出|停止)$' {
                    Write-DevText '  正在关闭本次前后端；独立配音服务和复用的后端保持运行。' -ForegroundColor Yellow
                    return
                }
                '^(help|帮助|菜单)$|^$' { Show-DevHelp; continue }
                '^(status|状态)$' {
                    Show-DevStatus -BackendProcess $BackendProcess -FrontendProcess $FrontendProcess -BackendPort $BackendPort -FrontendPort $FrontendPort -BackendReused:$BackendReused
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
                    if ($target -in @('all', 'backend', 'frontend', '后端', '前端')) {
                        if ($action -ne 'watch') { Show-DevLogs -LogDir $LogDir -Target $target; continue }
                        $names = if ($target -in @('backend', '后端')) { @('backend') } elseif ($target -in @('frontend', '前端')) { @('frontend') } else { @('backend', 'frontend') }
                        $paths = foreach ($name in $names) { foreach ($suffix in @('out', 'err')) { Join-Path $LogDir "$name.$suffix.log" } }
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
                '^(open|打开)$' { Start-Process "http://127.0.0.1:$FrontendPort"; continue }
                default { Write-DevText "  未识别命令：$command。输入 help 查看用法。" -ForegroundColor Yellow }
            }
        } catch { Write-DevText "  [错误] $($_.Exception.Message)" -ForegroundColor Red }
    }
}
