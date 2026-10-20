# 完整冒烟：隔离 pytest、固定 8611 健康检查、前端构建；不接触用户实例。
param(
    [switch]$SkipTests,
    [switch]$SkipFrontendBuild,
    [ValidateRange(1, 300)][int]$StartupTimeoutSec = 90,
    [switch]$KeepTemporaryData
)
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$SmokeRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$SmokePython = Join-Path $SmokeRoot 'backend\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $SmokePython)) {
    throw '未找到后端虚拟环境，请先运行 scripts/install.ps1；完整 pytest 还需要识别与分离可选依赖。'
}
$SmokeArguments = @((Join-Path $PSScriptRoot 'smoke.py'), '--startup-timeout', [string]$StartupTimeoutSec)
if ($SkipTests) { $SmokeArguments += '--skip-tests' }
if ($SkipFrontendBuild) { $SmokeArguments += '--skip-frontend-build' }
if ($KeepTemporaryData) { $SmokeArguments += '--keep-temporary-data' }
& $SmokePython -X utf8 @SmokeArguments
exit $LASTEXITCODE
