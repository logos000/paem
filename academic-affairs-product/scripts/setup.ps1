# First-run setup for a new machine (Stage 5 V4).
# Creates the two venvs + installs deps + builds the web frontend.
# The corpus build itself still comes from the builder repo (see its README).

param(
    [string]$BuilderDir = $(if ($env:BUILDER_DIR) { $env:BUILDER_DIR } else { "E:\Projects\zjui-product\llm-corpus-builder" }),
    [string]$ProductDir = $(Split-Path $PSScriptRoot -Parent),
    [string]$NodeDir = "E:\APPLICATIONS\node.js"
)
$ErrorActionPreference = "Stop"

Write-Host "[setup] product venv…" -ForegroundColor Cyan
if (-not (Test-Path (Join-Path $ProductDir ".venv"))) { py -3.12 -m venv (Join-Path $ProductDir ".venv") }
& (Join-Path $ProductDir ".venv\Scripts\python.exe") -m pip install -q -e "$ProductDir[dev]"
& (Join-Path $ProductDir ".venv\Scripts\python.exe") -m pip install -q "python-multipart>=0.0.9"

# one-time: carry LLM keys from the builder .env into the product .env
$builderEnv = Join-Path $BuilderDir ".env"
$productEnv = Join-Path $ProductDir ".env"
if ((Test-Path $builderEnv) -and -not (Test-Path $productEnv)) {
    $llm = @(Select-String -Path $builderEnv -Pattern "^(LLM_BASE_URL|LLM_API_KEY|LLM_MODEL)=.+" | ForEach-Object { $_.Line })
    if ($llm.Count -ge 3) {
        "# LLM keys carried from llm-corpus-builder/.env (never commit)`n" + ($llm -join "`n") + "`nLLM_TIMEOUT_SECONDS=120`n" | Set-Content -Encoding UTF8 $productEnv
        Write-Host "[setup] product .env 已从 builder 搬运 LLM 密钥（生成式回答可用）" -ForegroundColor Green
    }
}

Write-Host "[setup] builder venv (uv)…" -ForegroundColor Cyan
Push-Location $BuilderDir
if (-not (Test-Path (Join-Path $BuilderDir ".venv"))) { uv venv .venv }
uv sync --extra service
Pop-Location

Write-Host "[setup] web frontend…" -ForegroundColor Cyan
if (Test-Path $NodeDir) { $env:Path = "$NodeDir;$env:Path" }
Push-Location (Join-Path $ProductDir "web")
if (-not (Test-Path "node_modules")) { npm install }
npm run build
Pop-Location

Write-Host "[setup] 完成。语料库请在 builder 运行：python -m pipeline --sites zjui,sharepoint --ocr" -ForegroundColor Green
Write-Host "[setup] 之后双击 启动教务问答.bat 即可。"
