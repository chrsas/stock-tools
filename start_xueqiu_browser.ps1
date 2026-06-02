# 启动专用雪球浏览器（真实 Edge + 持久化 profile + CDP 调试端口）。
# 用真实浏览器指纹过 Aliyun WAF/滑块；首次人工过一次滑块后，profile 复用。
# 用法：.\start_xueqiu_browser.ps1            （默认 profile 在 data\browser_profile，端口 9224）
#       .\start_xueqiu_browser.ps1 -Url https://xueqiu.com/u/8414744881
# 启动后保持窗口开着，再运行：python -m kol_archive run-once

param(
    [string]$ProfileDir,
    [int]$Port = 9224,
    [string]$Url = "https://xueqiu.com",
    [string]$EdgePath,
    [ValidateSet("Normal", "Minimized", "Maximized")]
    [string]$WindowStyle = "Normal"
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ProfileDir) {
    $ProfileDir = Join-Path $scriptDir "data\browser_profile"
}

$edgeCandidates = @(
    $EdgePath,
    "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "C:\Program Files\Microsoft\Edge\Application\msedge.exe"
)
$edge = $edgeCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $edge) {
    Write-Error "未找到 Edge。请用 -EdgePath 指向 msedge.exe（或 chrome.exe）。"
    exit 1
}

New-Item -ItemType Directory -Force -Path $ProfileDir | Out-Null

Start-Process `
    -FilePath $edge `
    -ArgumentList @(
        "--user-data-dir=$ProfileDir",
        "--no-first-run",
        "--new-window",
        "--remote-debugging-port=$Port",
        $Url
    ) `
    -WindowStyle $WindowStyle

Write-Output "已启动专用浏览器，CDP 端口 $Port，profile：$ProfileDir"
Write-Output "请在窗口里完成登录并过一次滑块，然后运行：python -m kol_archive run-once"
