#Requires -Version 5.1
<#
.SYNOPSIS
    数据分析 Agent 一键启动脚本(Windows)。

.DESCRIPTION
    依次完成 8 个步骤:Python 版本检查 -> .venv 与依赖 -> .env 检查 ->
    docker info -> 缺失镜像构建 -> 冒烟自检 -> 演示数据 -> 启动 Web 并打开页面。

    退出码:
        0  服务已启动 / 页面已打开
        1  冒烟自检或启动失败
        2  环境未就绪(.env 缺 Key / Docker 未启动 / Python 版本不符)

.PARAMETER DryRun
    只打印 8 个步骤,不写入文件、不联网、不调用 Docker、不启动服务。

.EXAMPLE
    scripts/bootstrap.ps1
    scripts/bootstrap.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$ImageName = "data-agent-sandbox:py311"
$AppUrl = "http://127.0.0.1:8000"

function Write-Step([int]$Index, [string]$Text) {
    Write-Host ""
    Write-Host "[$Index/8] $Text" -ForegroundColor Cyan
}

function Write-Info([string]$Text) {
    Write-Host "        $Text" -ForegroundColor DarkGray
}

function Fail([string]$Text, [int]$Code = 1) {
    Write-Host ""
    Write-Host "[错误] $Text" -ForegroundColor Red
    exit $Code
}

if ($DryRun) {
    Write-Host "[DRY-RUN] 只打印步骤,不执行任何写入 / 网络 / Docker 操作。" -ForegroundColor Yellow
    Write-Step 1 "检查 Python 版本(要求 3.11 - 3.13)"
    Write-Info "将执行: python --version"
    Write-Step 2 "复用或创建 .venv,并安装 requirements.txt"
    Write-Info "将执行: python -m venv .venv; .venv\Scripts\python.exe -m pip install -r requirements.txt"
    Write-Step 3 "检查 .env;不存在时从 .env.example 复制并退出码 2"
    Write-Info "将执行: Test-Path .env / Copy-Item .env.example .env; 检查 LLM_API_KEY 是否仍为占位符"
    Write-Step 4 "检查 Docker Desktop 是否可用"
    Write-Info "将执行: docker info(失败时提示启动 Docker Desktop,退出码 2)"
    Write-Step 5 "检查沙箱镜像 $ImageName;缺失时自动构建"
    Write-Info "将执行: docker image inspect $ImageName; docker build -t $ImageName sandbox/"
    Write-Step 6 "运行冒烟自检 scripts/smoke.py"
    Write-Info "将执行: .venv\Scripts\python.exe scripts/smoke.py"
    Write-Step 7 "data/processed 为空时生成演示数据"
    Write-Info "将执行: .venv\Scripts\python.exe scripts/make_demo_data.py"
    Write-Step 8 "启动 python web/run.py 并打开 $AppUrl"
    Write-Info "将执行: Start-Process ... web/run.py;轮询 /api/health;打开浏览器"
    Write-Host ""
    Write-Host "[DRY-RUN] 已打印全部步骤,未产生任何副作用。" -ForegroundColor Yellow
    exit 0
}

# ---------- 1. Python 版本 ----------
Write-Step 1 "检查 Python 版本(要求 3.11 - 3.13)"
$versionOutput = (& python --version 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $versionOutput -notmatch "Python (\d+)\.(\d+)") {
    Fail "未找到 python 命令。请安装 Python 3.11 - 3.13 并加入 PATH。" 2
}
$major = [int]$Matches[1]
$minor = [int]$Matches[2]
if ($major -ne 3 -or $minor -lt 11 -or $minor -gt 13) {
    Fail "当前 $versionOutput,需要 Python 3.11 - 3.13。" 2
}
Write-Info "已检测到 $versionOutput"

# ---------- 2. .venv 与依赖 ----------
Write-Step 2 "复用或创建 .venv,并安装 requirements.txt"
if (Test-Path $VenvPython) {
    Write-Info "复用已有 .venv"
} else {
    Write-Info "创建 .venv ..."
    & python -m venv .venv
    if ($LASTEXITCODE -ne 0) { Fail "创建 .venv 失败。" 2 }
}
Write-Info "安装依赖(pip 会复用已安装版本)..."
& $VenvPython -m pip install --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { Fail "依赖安装失败,请检查网络或 requirements.txt。" 2 }
Write-Info "依赖已就绪"

# ---------- 3. .env ----------
Write-Step 3 "检查 .env 配置"
$envFile = Join-Path $ProjectRoot ".env"
$envExample = Join-Path $ProjectRoot ".env.example"
if (-not (Test-Path $envFile)) {
    Copy-Item $envExample $envFile
    Write-Host "        已从 .env.example 生成 .env。请填入 LLM_API_KEY 后重新运行 scripts/bootstrap.ps1。" -ForegroundColor Yellow
    exit 2
}
$keyLine = Select-String -Path $envFile -Pattern "^LLM_API_KEY=" -Encoding UTF8 | Select-Object -First 1
if ($keyLine -and $keyLine.Line -match "sk-your-key-here") {
    Write-Host "        .env 中的 LLM_API_KEY 仍是占位符。请填入真实 Key 后重新运行 scripts/bootstrap.ps1。" -ForegroundColor Yellow
    exit 2
}
Write-Info ".env 已存在"

# ---------- 4. Docker ----------
Write-Step 4 "检查 Docker Desktop 是否可用"
# 兼容两种安装路径:系统级与当前用户级
$dockerCandidates = @(
    (Join-Path $env:ProgramFiles "Docker\Docker\resources\bin"),
    (Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\resources\bin")
)
foreach ($candidate in $dockerCandidates) {
    if (Test-Path $candidate) {
        $env:PATH = "$candidate;" + $env:PATH
        break
    }
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail "未找到 docker 命令。请安装 Docker Desktop,并确认已启动。" 2
}
& docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Fail "Docker 守护进程不可用。请启动 Docker Desktop,等待状态变为 Running 后重试。" 2
}
Write-Info "Docker 已可用"

# ---------- 5. 沙箱镜像 ----------
Write-Step 5 "检查沙箱镜像 $ImageName"
& docker image inspect $ImageName *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Info "镜像已存在,跳过构建"
} else {
    Write-Info "镜像不存在,开始构建(首次约 1-3 分钟)..."
    & docker build -t $ImageName sandbox/
    if ($LASTEXITCODE -ne 0) { Fail "沙箱镜像构建失败。" 1 }
    Write-Info "镜像构建完成"
}

# ---------- 6. 冒烟自检 ----------
Write-Step 6 "运行冒烟自检 scripts/smoke.py"
& $VenvPython scripts/smoke.py
if ($LASTEXITCODE -ne 0) { Fail "冒烟自检未通过,已中止启动。" 1 }
Write-Info "冒烟自检通过"

# ---------- 7. 演示数据 ----------
Write-Step 7 "检查 data/processed 是否需要演示数据"
$existingCsv = @(Get-ChildItem -Path "data/processed" -Filter "*.csv" -File -ErrorAction SilentlyContinue)
if ($existingCsv.Count -eq 0) {
    Write-Info "data/processed 为空,生成演示数据 ..."
    & $VenvPython scripts/make_demo_data.py
    if ($LASTEXITCODE -ne 0) { Fail "演示数据生成失败。" 1 }
} else {
    Write-Info "data/processed 已有 $($existingCsv.Count) 个 CSV,跳过演示数据生成"
}

# ---------- 8. 启动服务 ----------
Write-Step 8 "启动 Web 服务并打开页面"
$portInUse = $false
try {
    $listener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
    $portInUse = [bool]$listener
} catch {
    $portInUse = $false
}

if ($portInUse) {
    Write-Info "端口 8000 已有服务在监听,跳过启动,直接打开页面"
} else {
    $outputsDir = Join-Path $ProjectRoot "outputs"
    New-Item -ItemType Directory -Force -Path $outputsDir | Out-Null
    $stdoutLog = Join-Path $outputsDir "server.out.log"
    $stderrLog = Join-Path $outputsDir "server.err.log"
    Start-Process -FilePath $VenvPython -ArgumentList "web/run.py" `
        -WorkingDirectory $ProjectRoot `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -WindowStyle Hidden | Out-Null
    Write-Info "服务进程已启动,等待健康检查 ..."

    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        try {
            Invoke-WebRequest -UseBasicParsing -Uri "$AppUrl/api/health" -TimeoutSec 2 | Out-Null
            $ready = $true
            break
        } catch {
            # 服务尚未就绪,继续轮询
        }
    }
    if (-not $ready) {
        Fail "服务启动超时。请查看 outputs/server.err.log。" 1
    }
    Write-Info "健康检查通过"
}

try {
    Start-Process $AppUrl | Out-Null
    Write-Info "已尝试打开默认浏览器"
} catch {
    Write-Info "无法自动打开浏览器,请手动访问 $AppUrl"
}

Write-Host ""
Write-Host "[完成] 数据分析 Agent 已启动: $AppUrl" -ForegroundColor Green
Write-Host "        按 Ctrl+C 不会停止后台服务;如需停止,请关闭对应的 python 进程。" -ForegroundColor DarkGray
exit 0
