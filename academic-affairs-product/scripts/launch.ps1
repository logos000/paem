# One-click launcher (Stage 5 V4): preflight -> runtime -> product -> watcher -> browser.
# Ctrl+C in this console stops EVERYTHING (finally-block cleanup, no orphans).
# -TestMode: start, wait healthy, self-clean after a few seconds, exit 0 (CI check).

param(
    [string]$BuilderDir = $(if ($env:BUILDER_DIR) { $env:BUILDER_DIR } else { "E:\Projects\zjui-product\llm-corpus-builder" }),
    [string]$ProviderDir = $(if ($env:PROVIDER_DIR) { $env:PROVIDER_DIR } else { "E:\Projects\zjui-product\raw-material-provider" }),
    [string]$ProductDir = $(Split-Path $PSScriptRoot -Parent),
    [string]$BuildDir = $(if ($env:BUILD_DIR) { $env:BUILD_DIR } else { "" }),
    [string]$RuntimeConfig = $(if ($env:RUNTIME_CONFIG) { $env:RUNTIME_CONFIG } else { "active_ir" }),
    [int]$RuntimePort = 8772,
    [int]$ProductPort = 8770,
    [switch]$NoWatcher,
    [switch]$TestMode,
    [switch]$SkipBrowser
)
$ErrorActionPreference = "Stop"
$RuntimePy = Join-Path $BuilderDir ".venv\Scripts\python.exe"
$ProductPy = Join-Path $ProductDir ".venv\Scripts\python.exe"
$WatcherPy = $RuntimePy   # provider scripts run under the builder venv (stdlib-only)
$CorpusDir = Join-Path $BuilderDir "data\corpus"
$LogDir = Join-Path $ProductDir "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Fail($kind, $message) { Write-Host "[$kind] $message" -ForegroundColor Red; exit 1 }
function Step($message) { Write-Host "[启动] $message" -ForegroundColor Cyan }
function Test-Url($url) {
    try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 $url -ErrorAction Stop | Out-Null; return $true } catch { return $false }
}

# ---------------- preflight (each failure names its category + the fix)
if (-not (Test-Path $RuntimePy)) { Fail "环境" "builder 缺少虚拟环境（$RuntimePy）。新机器请先运行 scripts\setup.ps1。" }
if (-not (Test-Path $ProductPy)) { Fail "环境" "product 缺少虚拟环境（$ProductPy）。请先运行 scripts\setup.ps1。" }
if (-not (Test-Path $ProviderDir)) { Fail "环境" "找不到摄入层目录（$ProviderDir）。可用 PROVIDER_DIR 环境变量指定 raw-material-provider 的位置。" }
if (-not (Test-Path (Join-Path $ProviderDir "scripts\watch_inbox.py"))) { Fail "环境" "看门狗脚本缺失（$ProviderDir\scripts\watch_inbox.py）。" }
if (-not (Test-Path (Join-Path $CorpusDir "LATEST"))) { Fail "build" "找不到语料库（$CorpusDir\LATEST）。请先在 builder 运行一次构建（python -m pipeline --sites zjui,sharepoint --ocr）。" }
if (-not (Test-Path (Join-Path $ProductDir "web\dist\index.html"))) { Fail "前端" "web\dist 不存在。请在 web\ 下运行 npm install; npm run build（或 scripts\setup.ps1）。" }

# 语义检索（hybrid 默认臂，2026-10-16）依赖本地嵌入模型；缺失则本次降级纯词汇检索。
# 模型已集中到 G:\model_download\builder（用户裁决 2026-10-20），旧仓库内路径为兼容保留。
$bgeModel = "G:\model_download\builder\embed\bge-m3\pytorch_model.bin"
$hybridAvailable = Test-Path $bgeModel
if (-not $hybridAvailable -and (Test-Path (Join-Path $BuilderDir "models/embed/bge-m3/pytorch_model.bin"))) {
    $bgeModel = Join-Path $BuilderDir "models/embed/bge-m3/pytorch_model.bin"
    $hybridAvailable = $true
}
if (-not $hybridAvailable) {
    Write-Host "[提示] 缺少语义检索模型（$bgeModel），本次启动自动降级为纯词汇检索（PRODUCT_ARM=sp_lexical）。" -ForegroundColor Yellow
    Write-Host "[提示] 如需语义检索，用 hf-mirror 下载到模型目录后重启：" -ForegroundColor Yellow
    Write-Host '        HF_ENDPOINT=https://hf-mirror.com .venv\Scripts\python -c "from huggingface_hub import snapshot_download; snapshot_download(''BAAI/bge-m3'', local_dir=''G:/model_download/builder/embed/bge-m3'', allow_patterns=[''*.json'',''*.txt'',''pytorch_model.bin'',''sentencepiece.bpe.model'',''1_Pooling/*''])"' -ForegroundColor Yellow
    $env:PRODUCT_ARM = "sp_lexical"
}

# already running? -> just open the browser
$runtimeUp = $false; $productUp = $false
try { Invoke-RestMethod -TimeoutSec 3 "http://127.0.0.1:$RuntimePort/health" | Out-Null; $runtimeUp = $true } catch {}
try { Invoke-RestMethod -TimeoutSec 3 "http://127.0.0.1:$ProductPort/health/ready" | Out-Null; $productUp = $true } catch {}
if ($runtimeUp -and $productUp) {
    Write-Host "[启动] 服务已在运行，直接打开页面。" -ForegroundColor Yellow
    if (-not $SkipBrowser) { Start-Process "http://127.0.0.1:$ProductPort/" }
    exit 0
}

# Resolve the build: SP×gfxwj merged by default (user ruling 2026-10-19 —
# merged passed all regression gates: gfxwj 743 & SP 289 judge --all no-regression,
# honesty 8/8 + 28/28, cross-site dedup clean). SP-only and other builds
# remain available via BUILD_DIR=<dir>.
$MergedBuild = Join-Path $CorpusDir "sp-gfxwj-merged-20261019"
$SPBuild = Join-Path $CorpusDir "sharepoint-only-20261015"
$EffectiveBuild = if ($BuildDir) { $BuildDir } elseif (Test-Path (Join-Path $MergedBuild "manifest.json")) { $MergedBuild } elseif (Test-Path (Join-Path $SPBuild "manifest.json")) { $SPBuild } else { $CorpusDir }

# Governance mode (2026-10-17 five-arm eval on v2_289): advisory = telemetry
# yes, veto no — core 90.7% vs 85.0% enforce, 0 bad refusals, traps 13/13.
# Override with GOVERNANCE_MODE=enforce to restore the gating behavior.
$env:RUNTIME_GOVERNANCE_MODE = if ($env:GOVERNANCE_MODE) { $env:GOVERNANCE_MODE } else { "advisory" }

# 完整问答记录（2026-10-17 公网测试裁决）：默认逐字记录提问（否则日期/课程码被
# 脱敏）；限流放宽到评测多人并发档。logs/ask.jsonl 按大小轮转、保留有限后缀，
# 重要测试时段结束后建议手工备份一份。
$env:ASK_LOG_FULL = if ($env:ASK_LOG_FULL) { $env:ASK_LOG_FULL } else { "true" }
$env:RATE_LIMIT_PER_MINUTE = if ($env:RATE_LIMIT_PER_MINUTE) { $env:RATE_LIMIT_PER_MINUTE } else { "120" }
$env:RATE_LIMIT_BURST = if ($env:RATE_LIMIT_BURST) { $env:RATE_LIMIT_BURST } else { "30" }

$procs = @()
try {
    if (-not $runtimeUp) {
        Step "语料检索运行时 (port $RuntimePort, build=$(Split-Path $EffectiveBuild -Leaf))…"
        # 两段式启动设计：① 以 active_ir 便宜启动（纯 CPU、任何构建都能 fail-fast），
        # ② 启动完成后下方预热块加载产品实际使用的 active_hybrid（bge-m3 + 向量缓存），
        # 用户第一题即为热路径。不要把启动配置直接改成 hybrid——模型缺失/无 GPU 的
        # 环境会起不来，且开机即占显存。
        $procs += Start-Process -PassThru -NoNewWindow -FilePath $RuntimePy `
            -ArgumentList "-m", "runtime.service", "--build", "$EffectiveBuild", "--config", "$RuntimeConfig", "--port", "$RuntimePort" `
            -WorkingDirectory $BuilderDir -RedirectStandardOutput (Join-Path $LogDir "runtime.log") -RedirectStandardError (Join-Path $LogDir "runtime.err.log")
        $deadline = (Get-Date).AddSeconds(300)
        do { Start-Sleep -Seconds 3 } while (-not (Test-Url "http://127.0.0.1:$RuntimePort/health") -and (Get-Date) -lt $deadline)
        if (-not (Test-Url "http://127.0.0.1:$RuntimePort/health")) { Fail "build" "runtime 未能在 5 分钟内就绪，日志：$LogDir\runtime.err.log" }
        Step "runtime 就绪。"
    }
    Step "产品 API + 界面 (port $ProductPort)…"
    $env:BUILDER_URL = "http://127.0.0.1:$RuntimePort"
    $env:PYTHONIOENCODING = "utf-8"; $env:PYTHONPATH = $ProductDir
    $procs += Start-Process -PassThru -NoNewWindow -FilePath $ProductPy `
        -ArgumentList "-m", "uvicorn", "product.app:app", "--host", "127.0.0.1", "--port", "$ProductPort" `
        -WorkingDirectory $ProductDir -RedirectStandardOutput (Join-Path $LogDir "product.log") -RedirectStandardError (Join-Path $LogDir "product.err.log")
    $deadline = (Get-Date).AddSeconds(60)
    do { Start-Sleep -Seconds 2 } while (-not (Test-Url "http://127.0.0.1:$ProductPort/health/ready") -and (Get-Date) -lt $deadline)
    if (-not (Test-Url "http://127.0.0.1:$ProductPort/health/ready")) { Fail "环境" "product 未就绪，日志：$LogDir\product.err.log" }
    Step "product 就绪。"
    try {
        $ready = Invoke-RestMethod -TimeoutSec 5 "http://127.0.0.1:$ProductPort/health/ready"
        if (-not $ready.llm_enabled) {
            Write-Host "[提示] 未配置 LLM 密钥，当前为证据模板模式（答案=证据原文摘录，非生成式）。" -ForegroundColor Yellow
            Write-Host "[提示] 在 $($ProductDir)\.env 配置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 后重启可启用生成式回答。" -ForegroundColor Yellow
        }
    } catch {}

    # 管理面口令（2026-10-17）：/admin/* 与 /v1/materials 上传受 PRODUCT_ADMIN_PASSWORD
    # 保护，公网链接下别人拿不到控制台；未设置则任何人都可导入/切换知识库。
    $envFile = Join-Path $ProductDir ".env"
    $adminPwSet = (Test-Path $envFile) -and [bool](Select-String -Path $envFile -Pattern "^\s*PRODUCT_ADMIN_PASSWORD=\S" -Quiet)
    if ($adminPwSet) {
        Write-Host "[提示] 管理口令已启用：/admin 与材料上传需要口令（见 .env 的 PRODUCT_ADMIN_PASSWORD，浏览器首次进入控制台时输入一次）。"
    } else {
        Write-Host "[提示] 未设置 PRODUCT_ADMIN_PASSWORD：公网链接下任何人都可导入材料/切换知识库，建议在 .env 配置后重启。" -ForegroundColor Yellow
    }

    if ($hybridAvailable) {
        Step "预热语义检索引擎（新构建首次需构建语义索引，约 1 分钟）…"
        try {
            $warmBody = '{"question": "warmup", "config": "active_hybrid", "k": 1, "pack_limit": 1}'
            Invoke-RestMethod -TimeoutSec 600 -Method Post -Uri "http://127.0.0.1:$RuntimePort/v1/answer_context" -ContentType "application/json" -Body $warmBody | Out-Null
            Step "语义检索引擎就绪。"
        } catch {
            Write-Host "[提示] 语义检索预热失败（$($_.Exception.Message)）。问答界面仍可用；如持续失败可设 PRODUCT_ARM=sp_lexical 回退纯词汇检索。" -ForegroundColor Yellow
        }
    }

    if (-not $NoWatcher) {
        Step "导入材料看门狗（自动入库→构建→发布）…"
        $procs += Start-Process -PassThru -NoNewWindow -FilePath $WatcherPy `
            -ArgumentList (Join-Path $ProviderDir "scripts\watch_inbox.py") `
            -WorkingDirectory $ProviderDir -RedirectStandardOutput (Join-Path $LogDir "watcher.log") -RedirectStandardError (Join-Path $LogDir "watcher.err.log")
    }

    if (-not $SkipBrowser -and -not $TestMode) { Start-Process "http://127.0.0.1:$ProductPort/" }
    Write-Host ""
    Write-Host "================ 全部就绪：http://127.0.0.1:$ProductPort/ ================" -ForegroundColor Green
    Write-Host "日志目录：$LogDir   （在本窗口按 Ctrl+C 一键停止全部服务）"
    if ($TestMode) {
        Step "TestMode：运行 8 秒后自检退出。"
        Start-Sleep -Seconds 8
        return
    }
    while ($true) { Start-Sleep -Seconds 5 }
}
finally {
    foreach ($proc in $procs) {
        if ($proc -and -not $proc.HasExited) {
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        }
    }
    if ($null -ne $procs -and $procs.Count -gt 0) { Write-Host "[停止] 已停止全部服务（$($procs.Count) 个进程）。" -ForegroundColor Yellow }
}
