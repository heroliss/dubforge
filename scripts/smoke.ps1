# DubForge 冒烟自检: 后端测试 + health + 前端构建
# 用法: .\scripts\smoke.ps1
$ErrorActionPreference = "Stop"
$Host.UI.RawUI.WindowTitle = "DubForge Smoke"
$Root = (Resolve-Path "$PSScriptRoot\..").Path

Write-Host ""
Write-Host "== 1/3 backend pytest ==" -ForegroundColor Cyan
Push-Location "$Root\backend"
uv run pytest -q
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "pytest 失败" }
Pop-Location

Write-Host "== 2/3 backend health ==" -ForegroundColor Cyan
Push-Location "$Root\backend"
$proc = Start-Process -PassThru uv -ArgumentList @("run", "uvicorn", "app.main:app", "--port", "8611")
try {
    $r = $null
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 800
        try {
            $r = Invoke-RestMethod "http://127.0.0.1:8611/api/health" -TimeoutSec 2
            if ($r.ok) { break }
        } catch {}
    }
    if (-not ($r -and $r.ok)) { throw "health 检查失败" }
    Write-Host "health OK: $($r.app) v$($r.version)" -ForegroundColor Green
} finally {
    # uv 会派生 python 子进程, 按进程树整棵杀
    taskkill /PID $proc.Id /T /F 2>$null | Out-Null
}
Pop-Location

Write-Host "== 3/3 frontend build ==" -ForegroundColor Cyan
Push-Location "$Root\frontend"
npm run build
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "前端构建失败" }
Pop-Location

Write-Host ""
Write-Host "== 冒烟全部通过 ==" -ForegroundColor Green
