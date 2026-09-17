# 构建 DubForge 生产形态: 前端 dist + 后端单进程托管
# 用法: .\scripts\build.ps1 ; 之后 cd backend; uv run uvicorn app.main:app --port 8600
$ErrorActionPreference = "Stop"
$Root = Split-Path -PSScriptRoot -Parent

Write-Host "== frontend build ==" -ForegroundColor Cyan
Push-Location "$Root\frontend"
npm run build
if ($LASTEXITCODE -ne 0) { throw "前端构建失败" }
Pop-Location

Write-Host "== backend self-check ==" -ForegroundColor Cyan
Push-Location "$Root\backend"
uv run python -c "from app.main import app; print('app import OK')"
Pop-Location

Write-Host "== 构建完成: uvicorn app.main:app --port 8600 托管 API + dist ==" -ForegroundColor Green
