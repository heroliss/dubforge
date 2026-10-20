# DubForge 生产形态构建: 前端 dist + 后端单进程托管
# 用法: .\scripts\build.ps1；按结尾提示启动，不重新同步已选依赖。
$ErrorActionPreference = "Stop"
$Host.UI.RawUI.WindowTitle = "DubForge Build"
$Root = (Resolve-Path "$PSScriptRoot\..").Path

Write-Host ""
Write-Host "== frontend build ==" -ForegroundColor Cyan
Push-Location "$Root\frontend"
npm run build
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "前端构建失败" }
Pop-Location

Write-Host "== backend self-check ==" -ForegroundColor Cyan
Push-Location "$Root\backend"
uv run --no-sync python -c "from app.main import app; print('app import OK')"
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "后端自检失败" }
Pop-Location

Write-Host ""
Write-Host "== 构建完成 ==" -ForegroundColor Green
$portMatch = Select-String -Path "$Root\config.yaml" -Pattern '^\s*port:\s*(\d+)' -ErrorAction SilentlyContinue
$port = if ($portMatch) { $portMatch.Matches[0].Groups[1].Value } else { '8600' }
Write-Host "启动生产: cd backend; uv run --no-sync uvicorn app.main:app --host 127.0.0.1 --port $port --loop app.event_loop:create_event_loop"
Write-Host "(API 与前端页面均由后端的 $port 端口托管)"
