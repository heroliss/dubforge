# 服务登记仅信任 Python 的完整身份核验；PowerShell 不按端口或进程名猜测所有权。
function Invoke-DevOwner {
    param([object[]]$Arguments)
    $output = & (Join-Path $Root 'backend\.venv\Scripts\python.exe') -m dubforge.dev_owner @Arguments
    $result = ($output -join "`n") | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw $result.message }
    return $result
}

function Register-DevServiceOwner {
    param($Process, [string]$Name, [string[]]$Command)
    if ($Name -notin @('backend','frontend') -or -not $Root) { return }
    $ports = [regex]::Match([IO.Path]::GetFileName($LogDir), '^(\d+)-(\d+)$')
    # 被单独提取的非服务启动测试不创建登记；真实开发命令和维护启动使用同一协议。
    if (-not $ports.Success -or $Command -notcontains '--port') { return }
    try {
        $created = ([DateTimeOffset]$Process.StartTime.ToUniversalTime()).ToUnixTimeMilliseconds() / 1000.0
        $createdText = $created.ToString('R', [Globalization.CultureInfo]::InvariantCulture)
        $owner = Invoke-DevOwner -Arguments (@('register', $Root, $ports.Groups[1].Value, $ports.Groups[2].Value, $Name,
            '--pid', $Process.Id, '--created', $createdText, '--') + $Command)
        $Process | Add-Member -NotePropertyName DubForgeOwner -NotePropertyValue $owner -Force
    } catch {
        # 新进程已启动；登记失败仍返回其 Process，当前控制台继续管理，不能制造无主进程。
        Write-DevText "  [警告] $Name 已启动，但身份登记失败，其他控制台不能接管：$_" -ForegroundColor Yellow
    }
}

function Resolve-DevServiceOwner {
    param([hashtable]$State, [ValidateSet('backend','frontend')][string]$Name, [switch]$RequireListener)
    if ($Name -eq 'backend' -and $State.BackendReused) { return $null }
    $registry = Join-Path $Root "data\dev-logs\$($State.BackendPort)-$($State.FrontendPort)\$Name.process.json"
    if (-not (Test-Path -LiteralPath $registry)) { return $null }
    $arguments = @('resolve', $Root, $State.BackendPort, $State.FrontendPort, $Name)
    if ($RequireListener) { $arguments += '--require-listener' }
    $owner = Invoke-DevOwner -Arguments $arguments
    if (-not $owner) { return $null }
    $process = Get-Process -Id $owner.pid -ErrorAction Stop
    $created = ([DateTimeOffset]$process.StartTime.ToUniversalTime()).ToUnixTimeMilliseconds() / 1000.0
    if ([Math]::Abs($created - [double]$owner.created) -gt .01) { throw '登记核验后进程身份已变化，拒绝接管。' }
    $process | Add-Member -NotePropertyName DubForgeOwner -NotePropertyValue $owner -Force
    return $process
}

function Unregister-DevServiceOwner {
    param($Process)
    $property = $Process.PSObject.Properties['DubForgeOwner']
    if (-not $property -or -not $property.Value) { return }
    $owner = $property.Value
    $created = ([double]$owner.created).ToString('R', [Globalization.CultureInfo]::InvariantCulture)
    try {
        $null = Invoke-DevOwner -Arguments @('forget', $Root, $owner.backend_port, $owner.frontend_port, $owner.role,
            '--pid', $owner.pid, '--created', $created, '--generation', $owner.generation)
    } catch { Write-DevText "  [警告] 服务已停止，但旧身份登记未清理：$_" -ForegroundColor Yellow }
}
