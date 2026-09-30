# 统一使用流式写入，绕过 Windows PowerShell Host 对双宽中文字符的布局处理。
# 重定向输出仍为 UTF-8；颜色只在交互控制台临时设置，不改变用户终端配置。
function Write-DevText {
    param([Parameter(Position=0)][AllowEmptyString()][object]$Text = '',
          [ConsoleColor]$ForegroundColor, [switch]$NoNewline)
    $colored = $PSBoundParameters.ContainsKey('ForegroundColor') -and -not [Console]::IsOutputRedirected
    $previous = [Console]::ForegroundColor
    try {
        if ($colored) { [Console]::ForegroundColor = $ForegroundColor }
        if ($NoNewline) { [Console]::Write([string]$Text) }
        else { [Console]::WriteLine([string]$Text) }
    } finally {
        if ($colored) { [Console]::ForegroundColor = $previous }
    }
}

function Read-DevInput {
    param([string]$Prompt)
    Write-DevText ($Prompt + ': ') -NoNewline
    return [Console]::ReadLine()
}
