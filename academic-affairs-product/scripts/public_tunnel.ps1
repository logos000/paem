# 公网访问隧道（2026-10-17）：把本机 8770 的问答网页暴露为一个公网 HTTPS 链接。
# 用法：先双击 启动教务问答.bat 让本机栈跑起来，再双击 公网访问.bat。
# 依赖：首次运行自动下载 cloudflared.exe（~60MB，存 tools\）。无需账号。
# 注意：问答无需口令；控制台（材料导入/知识库切换）受 PRODUCT_ADMIN_PASSWORD 保护。
#       语料为内部教务资料，链接只发给信任的人；
#       Ctrl+C 关闭本窗口即断开公网入口，本机服务不受影响。

param(
    [int]$ProductPort = 8770
)
$ErrorActionPreference = "Stop"
$Repo = Split-Path $PSScriptRoot -Parent
$Tools = Join-Path $Repo "tools"
$Exe = Join-Path $Tools "cloudflared.exe"

if (-not (Test-Path "http://127.0.0.1:$ProductPort/health/live")) {
    Write-Host "[错误] 本机产品未运行（127.0.0.1:$ProductPort 无响应）。" -ForegroundColor Red
    Write-Host "        请先双击 启动教务问答.bat，等浏览器打开后再运行本脚本。"
    exit 1
}

if (-not (Test-Path $Exe)) {
    New-Item -ItemType Directory -Force -Path $Tools | Out-Null
    Write-Host "[下载] cloudflared.exe（首次运行需要，约 60MB）…"
    $url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    try {
        Invoke-WebRequest -Uri $url -OutFile $Exe
    } catch {
        Write-Host "[错误] 下载失败（$($_.Exception.Message)）。" -ForegroundColor Red
        Write-Host "        请手动下载 $url 放到 $Tools 后重试。"
        exit 1
    }
}

Write-Host ""
Write-Host "════════════════════════════════════════════════════════════════"
Write-Host " 公网隧道启动中——下方 cloudflared 输出里的"
Write-Host "   https://xxxx.trycloudflare.com  就是公网链接"
Write-Host " 发给评委/同学，浏览器直接打开（HTTPS，无需安装任何东西）"
Write-Host " 问答无口令；控制台（导入/切换）需管理员口令。Ctrl+C 关闭本窗口即断开公网入口"
Write-Host "════════════════════════════════════════════════════════════════"
Write-Host ""
& $Exe tunnel --url "http://127.0.0.1:$ProductPort"
