# DubForge 开发环境一键启动 (前后端 + 浏览器)
# 用法: 双击根目录 StartDev.bat, 或 .\scripts\dev.ps1
#       需已跑过: backend/uv sync 与 frontend/npm install
$ErrorActionPreference = "Stop"
$Host.UI.RawUI.WindowTitle = "DubForge Dev Launcher"
$Root = (Resolve-Path "$PSScriptRoot\..").Path

Write-Host ""
Write-Host "  == DubForge dev ==" -ForegroundColor Cyan

# ---- 前置检查 ----
if (-not (Test-Path "$Root\backend\.venv")) {
    Write-Host "  [错误] 未找到 backend\.venv —— 请先执行: cd backend; uv sync" -ForegroundColor Red
    Read-Host "  回车退出"; exit 1
}
if (-not (Test-Path "$Root\frontend\node_modules")) {
    Write-Host "  [错误] 未找到 frontend\node_modules —— 请先执行: cd frontend; npm install" -ForegroundColor Red
    Read-Host "  回车退出"; exit 1
}

# ---- 单实例检测: 8600 已被占用时给出选择, 避免双实例互相顶掉 ----
function Get-PortPids($p) {
    $pids = @()
    foreach ($line in (netstat -ano | Select-String ":$p\s")) {
        $m = [regex]::Match($line, 'LISTENING\s+(\d+)')
        if ($m.Success) { $pids += $m.Groups[1].Value }
    }
    return $pids | Select-Object -Unique
}

$existing = Get-PortPids 8600
if ($existing) {
    Write-Host "  [提示] 检测到 8600 端口已被占用 (PID: $($existing -join ', ')) —— 已有 DubForge 实例在运行。" -ForegroundColor Yellow
    Write-Host "    [1] 直接连接现有实例 (推荐 —— 不重启, 保留运行中的任务)"
    Write-Host "    [2] 结束现有实例并重新启动"
    Write-Host "    [3] 退出"
    $ans = Read-Host "  选择 [1/2/3, 回车=1]"
    if ($ans -eq "3") { exit 0 }
    if ($ans -eq "2") {
        foreach ($epid in $existing) { taskkill /PID $epid /T /F 2>$null | Out-Null }
        Start-Sleep -Seconds 1
        if (Get-PortPids 8600) {
            Write-Host "  [错误] 旧实例无法结束 (文件占用?), 请手动关闭后重试。" -ForegroundColor Red
            Read-Host "  回车退出"; exit 1
        }
        Write-Host "  旧实例已结束, 开始重新启动..." -ForegroundColor Green
    } else {
        # 复用现有后端: 只起前端
        $frontend = Start-Process -PassThru powershell -ArgumentList @(
            "-NoProfile", "-Command",
            "`$Host.UI.RawUI.WindowTitle='DubForge Frontend'; cd '$Root\frontend'; npm run dev"
        )
        $okF = $false
        for ($i = 0; $i -lt 50; $i++) {
            Start-Sleep -Milliseconds 800
            foreach ($line in (netstat -ano | Select-String ":5273\s")) {
                if ($line -match 'LISTENING') { $okF = $true; break }
            }
            if ($okF) { break }
        }
        if ($okF) {
            Write-Host "  [OK] 前端 http://127.0.0.1:5273 (后端复用 8600 现有实例)" -ForegroundColor Green
            Start-Process "http://127.0.0.1:5273"
        } else {
            Write-Host "  [失败] 前端未就绪" -ForegroundColor Red
        }
        Write-Host ""
        Read-Host "  回车停止前端"
        if ($frontend -and -not $frontend.HasExited) { taskkill /PID $frontend.Id /T /F 2>$null | Out-Null }
        exit 0
    }
}

# ---- 后端 (8600, reload) ----
$backend = Start-Process -PassThru powershell -ArgumentList @(
    "-NoProfile", "-Command",
    "`$Host.UI.RawUI.WindowTitle='DubForge Backend'; cd '$Root\backend'; uv run uvicorn app.main:app --port 8600 --reload"
)

# ---- 前端 (5273, 热更新) ----
$frontend = Start-Process -PassThru powershell -ArgumentList @(
    "-NoProfile", "-Command",
    "`$Host.UI.RawUI.WindowTitle='DubForge Frontend'; cd '$Root\frontend'; npm run dev"
)

# ---- 等待两个端口就绪 (最多 40 秒), 都通了才开浏览器 ----
function Test-Port($p) {
    try { (New-Object Net.Sockets.TcpClient).ConnectAsync("127.0.0.1", $p).Wait(800) -and (New-Object Net.Sockets.TcpClient("127.0.0.1", $p)).Connected } catch { $false }
}
$okB = $false; $okF = $false
for ($i = 0; $i -lt 50; $i++) {
    Start-Sleep -Milliseconds 800
    if (-not $okB) { $okB = Test-Port 8600 }
    if (-not $okF) { $okF = Test-Port 5273 }
    if ($okB -and $okF) { break }
}
if ($okB) { Write-Host "  [OK] 后端 http://127.0.0.1:8600" -ForegroundColor Green } else { Write-Host "  [失败] 后端 8600 未就绪 (看 Backend 窗口报错)" -ForegroundColor Red }
if ($okF) { Write-Host "  [OK] 前端 http://127.0.0.1:5273" -ForegroundColor Green } else { Write-Host "  [失败] 前端 5273 未就绪 (看 Frontend 窗口报错)" -ForegroundColor Red }
if ($okB -and $okF) { Start-Process "http://127.0.0.1:5273" }

Write-Host ""
Write-Host "  后端 PID=$($backend.Id)  前端 PID=$($frontend.Id)"
Write-Host "  停止: 关闭这两个 PowerShell 窗口 (或在本窗口按回车直接杀掉它们)"
Read-Host "  回车停止前后端"
foreach ($p in @($backend, $frontend)) {
    if ($p -and -not $p.HasExited) { taskkill /PID $p.Id /T /F 2>$null | Out-Null }
}
Write-Host "  已停止。" -ForegroundColor Green
