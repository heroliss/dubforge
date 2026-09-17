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
