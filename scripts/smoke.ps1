# DubForge 冒烟自检: 后端 health + 测试 + (可选) 前端构建
$ErrorActionPreference = "Stop"
$Root = Split-Path -PSScriptRoot -Parent

Write-Host "== 1/3 backend pytest ==" -ForegroundColor Cyan
Push-Location "$Root\backend"
uv run pytest -q
Pop-Location

Write-Host "== 2/3 backend health ==" -ForegroundColor Cyan
Push-Location "$Root\backend"
$proc = Start-Process -PassThru uv -ArgumentList @("run", "uvicorn", "app.main:app", "--port", "8611")
try {
    $ok = $false
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 800
        try {
            $r = Invoke-RestMethod "http://127.0.0.1:8611/api/health" -TimeoutSec 2
            if ($r.ok) { $ok = $true; break }
        } catch {}
    }
    if (-not $ok) { throw "health 检查失败" }
    Write-Host "health OK: $($r.app) v$($r.version)" -ForegroundColor Green
} finally {
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
}
Pop-Location

Write-Host "== 3/3 frontend build ==" -ForegroundColor Cyan
Push-Location "$Root\frontend"
npm run build
if ($LASTEXITCODE -ne 0) { throw "前端构建失败" }
Pop-Location

Write-Host "== 冒烟全部通过 ==" -ForegroundColor Green
