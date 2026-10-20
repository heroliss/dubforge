# 基础程序与可选能力安装；保留已有可选依赖，不下载配音模型。
param(
    [switch]$Asr,
    [switch]$Separation,
    [switch]$Publishing,
    [switch]$SkipFrontend
)
$ErrorActionPreference = 'Stop'
$InstallRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw '未找到 uv，请先按 https://docs.astral.sh/uv/getting-started/installation/ 安装，再重新打开终端。'
}
if ((-not $SkipFrontend -or $Publishing) -and -not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw '未找到 Node.js / npm，请先安装 Node.js LTS：https://nodejs.org/'
}
Write-Host '请在 DubForge 关闭时安装或更新依赖。已有配置与可选组件会保留。' -ForegroundColor Cyan
$SyncArgs = @('sync', '--locked', '--inexact', '--project', (Join-Path $InstallRoot 'backend'))
if ($Asr) { $SyncArgs += @('--extra', 'asr') }
if ($Separation) { $SyncArgs += @('--extra', 'separation') }
& uv @SyncArgs
if ($LASTEXITCODE -ne 0) { throw '后端依赖安装失败，请查看上方输出后重试。' }
$ConfigFile = Join-Path $InstallRoot 'config.yaml'
if (-not (Test-Path -LiteralPath $ConfigFile)) {
    Copy-Item -LiteralPath (Join-Path $InstallRoot 'config.example.yaml') -Destination ($ConfigFile + '.tmp')
    Move-Item -LiteralPath ($ConfigFile + '.tmp') -Destination $ConfigFile
}
if (-not $SkipFrontend) {
    Push-Location (Join-Path $InstallRoot 'frontend')
    try {
        & npm ci
        if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败。' }
        & npm run build
        if ($LASTEXITCODE -ne 0) { throw '前端构建失败。' }
    } finally { Pop-Location }
}
if ($Publishing) {
    Push-Location (Join-Path $InstallRoot 'workers/bilibili')
    try {
        & npm ci --ignore-scripts
        if ($LASTEXITCODE -ne 0) { throw 'B站发布组件安装失败；不会影响本地制作。' }
    } finally { Pop-Location }
}
foreach ($ToolName in @('ffmpeg', 'ffprobe')) {
    if (-not (Get-Command $ToolName -ErrorAction SilentlyContinue)) {
        Write-Warning "未找到 $ToolName；请按 docs/installation.md 安装 FFmpeg 并添加到 PATH。"
    }
}
Write-Host '所选依赖安装完成。请启动 DubForge，在“设置 → 模型与服务”检查环境并配置服务地址。' -ForegroundColor Green
Write-Host '模型权重在首次使用时按需下载；IndexTTS / GPT-SoVITS 按官方说明单独安装。'
