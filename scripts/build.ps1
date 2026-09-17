# DubForge 生产形态构建: 前端 dist + 后端单进程托管
# 用法: .\scripts\build.ps1 ; 之后 cd backend; uv run uvicorn app.main:app --port 8600
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
uv run python -c "from app.main import app; print('app import OK')"
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "后端自检失败" }
Pop-Location

Write-Host ""
Write-Host "== 构建完成 ==" -ForegroundColor Green
Write-Host "启动生产: cd backend; uv run uvicorn app.main:app --port 8600"
Write-Host "(API 与前端页面均由 8600 一个进程托管)"
