# DubForge 开发环境一键启动 (前后端 + 浏览器)
# 用法: .\scripts\dev.ps1   (需已跑过: backend/uv sync 与 frontend/npm install)
$ErrorActionPreference = "Stop"
$Root = Split-Path -PSScriptRoot -Parent

Write-Host "== DubForge dev ==" -ForegroundColor Cyan

# 后端 (8600, reload)
$backend = Start-Process -PassThru powershell -ArgumentList @(
    "-NoProfile", "-Command",
    "cd '$Root\backend'; uv run uvicorn app.main:app --port 8600 --reload"
)

# 前端 (5273, 热更新)
$frontend = Start-Process -PassThru powershell -ArgumentList @(
    "-NoProfile", "-Command",
    "cd '$Root\frontend'; npm run dev"
)

Start-Sleep -Seconds 4
Start-Process "http://127.0.0.1:5273"

Write-Host "后端 PID=$($backend.Id)  前端 PID=$($frontend.Id)"
Write-Host "关闭: 逐个窗口 Ctrl+C, 或 Stop-Process"
