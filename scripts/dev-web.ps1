# 就绪检查覆盖网页入口及其 API 代理；浏览器启动不影响服务生命周期。
function Test-DevBackend {
    param([int]$Port)
    try {
        $health = Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 1
        return [bool]($health.ok -and $health.app -eq 'DubForge')
    } catch { return $false }
}

function Test-DevFrontend {
    param([int]$Port, [switch]$SkipApi)
    try {
        $page = Invoke-WebRequest "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 1
        if ($page.StatusCode -ne 200 -or $page.Content -notmatch 'DubForge') { return $false }
        if ($SkipApi) { return $true }
        # 入口 200 不代表可用：前端可能代理到了旧端口或其他实例。
        $health = Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 1
        return [bool]($health.ok -and $health.app -eq 'DubForge')
    } catch { return $false }
}

function Write-DevLifecycle {
    param([string]$Message)
    if (-not $LogDir) { return }
    try {
        $line = '{0} {1}{2}' -f [DateTimeOffset]::Now.ToString('o'), $Message, [Environment]::NewLine
        [IO.File]::AppendAllText((Join-Path $LogDir 'launcher.log'), $line, [Text.UTF8Encoding]::new($false))
    } catch {
        Write-DevText '  [提示] 启动器日志暂时无法写入，服务日志仍可查看。' -ForegroundColor Yellow
    }
}

function Get-DevBuiltPage {
    param([int]$Port)
    if (-not (Test-DevBackend -Port $Port)) {
        return @{ Ready = $false; Reason = '后端健康检查未通过。' }
    }
    $base = "http://127.0.0.1:$Port/"
    try {
        $page = Invoke-WebRequest -Uri $base -UseBasicParsing -TimeoutSec 2
        $html = [string]$page.Content
        if ($page.StatusCode -ne 200 -or $html -notmatch '<html\b' -or $html -notmatch '<title>[^<]*DubForge[^<]*</title>') {
            return @{ Ready = $false; Reason = '后端 API 已连接，但没有已构建的 DubForge 网页。' }
        }
        # 仅验证入口引用的本地构建资源，不能把 API 存活或 SPA 回退 HTML 当成可用界面。
        $pattern = '(?:src|href)\s*=\s*["''](?<asset>/assets/[^"''\s<>]+\.(?:m?js|css)(?:\?[^"''\s<>]*)?)["'']'
        $assets = @([regex]::Matches($html, $pattern, [Text.RegularExpressions.RegexOptions]::IgnoreCase) |
            ForEach-Object { [Net.WebUtility]::HtmlDecode($_.Groups['asset'].Value) } | Sort-Object -Unique)
        if (-not ($assets | Where-Object { $_ -match '\.m?js(?:\?|$)' })) {
            return @{ Ready = $false; Reason = '后端网页未包含可验证的本地构建脚本，请先构建前端。' }
        }
        foreach ($asset in $assets) {
            $uri = [Uri]::new([Uri]$base, $asset)
            if (-not $uri.AbsolutePath.StartsWith('/assets/')) {
                return @{ Ready = $false; Reason = '后端网页引用了无效的构建资源路径。' }
            }
            $resource = Invoke-WebRequest -Uri $uri.AbsoluteUri -Method Head -UseBasicParsing -TimeoutSec 2
            $contentType = [string]$resource.Headers['Content-Type']
            $correctType = if ($uri.AbsolutePath -match '\.css$') { $contentType -match '^text/css(?:;|$)' }
                else { $contentType -match '(?:javascript|ecmascript)' }
            if ($resource.StatusCode -ne 200 -or -not $correctType) {
                return @{ Ready = $false; Reason = '后端构建资源不可用或返回了错误内容，请重新构建前端。' }
            }
        }
        return @{ Ready = $true; Reason = '' }
    } catch {
        return @{ Ready = $false; Reason = '后端已连接，但构建网页或其资源无法读取。' }
    }
}

function Open-DevPage {
    param([int]$BackendPort, [int]$FrontendPort)
    $url = "http://127.0.0.1:$FrontendPort/"
    if (-not (Test-DevFrontend -Port $FrontendPort)) {
        $built = Get-DevBuiltPage -Port $BackendPort
        if (-not $built.Ready) {
            Write-DevText "  [提示] 前端入口或 API 代理尚未就绪；备用页面也不可用：$($built.Reason) 请使用 status 和 logs 检查。" -ForegroundColor Yellow
            Write-DevLifecycle "浏览器未打开：前端未就绪且后端备用页面不可用；$($built.Reason)"
            return $false
        }
        $url = "http://127.0.0.1:$BackendPort/"
        Write-DevText '  [提示] 前端开发入口不可用，已验证并改用后端托管的已构建页面；可能与最新开发代码存在差异。' -ForegroundColor Yellow
        Write-DevText '  后端和当前制作任务保持运行；这里只打开备用网页。' -ForegroundColor DarkGray
        Write-DevLifecycle "前端入口不可用，使用已验证的后端构建页面 $url"
    }
    Write-DevText "  网页地址：$url" -ForegroundColor Cyan
    Write-DevText '  请保持此控制台运行；关闭控制台或输入 quit 会停止本次前后端，网页随后将无法连接。' -ForegroundColor DarkGray
    try {
        Start-Process $url
        Write-DevLifecycle "已请求默认浏览器打开 $url"
        return $true
    } catch {
        Write-DevText "  [提示] 无法自动打开默认浏览器，服务仍在运行。请手动打开：$url" -ForegroundColor Yellow
        Write-DevText "  备用页面：http://127.0.0.1:$BackendPort/" -ForegroundColor DarkGray
        Write-DevLifecycle "默认浏览器启动失败，服务保留 $url"
        return $false
    }
}
