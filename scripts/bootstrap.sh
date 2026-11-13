#!/usr/bin/env bash
# 数据分析 Agent 一键启动脚本(macOS / Linux)。
#
# 依次完成 8 个步骤:Python 版本检查 -> .venv 与依赖 -> .env 检查 ->
# docker info -> 缺失镜像构建 -> 冒烟自检 -> 演示数据 -> 启动 Web 并打开页面。
#
# 退出码:
#   0  服务已启动 / 页面已打开
#   1  冒烟自检或启动失败
#   2  环境未就绪(.env 缺 Key / Docker 未启动 / Python 版本不符)
#
# 用法:
#   bash scripts/bootstrap.sh
#   bash scripts/bootstrap.sh --dry-run

set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

VENV_PY="$PROJECT_ROOT/.venv/bin/python"
IMAGE_NAME="data-agent-sandbox:py311"
APP_URL="http://127.0.0.1:8000"
DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
fi

step() { printf '\n\033[36m[%s/8] %s\033[0m\n' "$1" "$2"; }
info() { printf '        %s\n' "$1"; }
fail() { printf '\n\033[31m[错误] %s\033[0m\n' "$1"; exit "${2:-1}"; }

if [[ "$DRY_RUN" == "true" ]]; then
  printf '\033[33m[DRY-RUN] 只打印步骤,不执行任何写入 / 网络 / Docker 操作。\033[0m\n'
  step 1 "检查 Python 版本(要求 3.11 - 3.13)"
  info "将执行: python3 --version"
  step 2 "复用或创建 .venv,并安装 requirements.txt"
  info "将执行: python3 -m venv .venv; .venv/bin/python -m pip install -r requirements.txt"
  step 3 "检查 .env;不存在时从 .env.example 复制并退出码 2"
  info "将执行: test -f .env / cp .env.example .env; 检查 LLM_API_KEY 是否仍为占位符"
  step 4 "检查 Docker 是否可用"
  info "将执行: docker info(失败时提示启动 Docker,退出码 2)"
  step 5 "检查沙箱镜像 $IMAGE_NAME;缺失时自动构建"
  info "将执行: docker image inspect $IMAGE_NAME; docker build -t $IMAGE_NAME sandbox/"
  step 6 "运行冒烟自检 scripts/smoke.py"
  info "将执行: .venv/bin/python scripts/smoke.py"
  step 7 "data/processed 为空时生成演示数据"
  info "将执行: .venv/bin/python scripts/make_demo_data.py"
  step 8 "启动 python web/run.py 并打开 $APP_URL"
  info "将执行: nohup ... web/run.py;轮询 /api/health;打开浏览器"
  printf '\n\033[33m[DRY-RUN] 已打印全部步骤,未产生任何副作用。\033[0m\n'
  exit 0
fi

# ---------- 1. Python 版本 ----------
step 1 "检查 Python 版本(要求 3.11 - 3.13)"
if ! command -v python3 >/dev/null 2>&1; then
  fail "未找到 python3 命令。请安装 Python 3.11 - 3.13。" 2
fi
PY_VERSION="$(python3 --version 2>&1)"
if ! python3 -c 'import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) else 1)'; then
  fail "当前 $PY_VERSION,需要 Python 3.11 - 3.13。" 2
fi
info "已检测到 $PY_VERSION"

# ---------- 2. .venv 与依赖 ----------
step 2 "复用或创建 .venv,并安装 requirements.txt"
if [[ -x "$VENV_PY" ]]; then
  info "复用已有 .venv"
else
  info "创建 .venv ..."
  python3 -m venv .venv || fail "创建 .venv 失败。" 2
fi
info "安装依赖(pip 会复用已安装版本)..."
"$VENV_PY" -m pip install --disable-pip-version-check -r requirements.txt \
  || fail "依赖安装失败,请检查网络或 requirements.txt。" 2
info "依赖已就绪"

# ---------- 3. .env ----------
step 3 "检查 .env 配置"
if [[ ! -f .env ]]; then
  cp .env.example .env
  printf '        已从 .env.example 生成 .env。请填入 LLM_API_KEY 后重新运行 scripts/bootstrap.sh。\n'
  exit 2
fi
if grep -q '^LLM_API_KEY=sk-your-key-here' .env; then
  printf '        .env 中的 LLM_API_KEY 仍是占位符。请填入真实 Key 后重新运行 scripts/bootstrap.sh。\n'
  exit 2
fi
info ".env 已存在"

# ---------- 4. Docker ----------
step 4 "检查 Docker 是否可用"
if ! command -v docker >/dev/null 2>&1; then
  fail "未找到 docker 命令。请安装并启动 Docker Desktop 或 Docker Engine。" 2
fi
if ! docker info >/dev/null 2>&1; then
  fail "Docker 守护进程不可用。请启动 Docker Desktop(或 Docker Engine)后重试。" 2
fi
info "Docker 已可用"

# ---------- 5. 沙箱镜像 ----------
step 5 "检查沙箱镜像 $IMAGE_NAME"
if docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
  info "镜像已存在,跳过构建"
else
  info "镜像不存在,开始构建(首次约 1-3 分钟)..."
  docker build -t "$IMAGE_NAME" sandbox/ || fail "沙箱镜像构建失败。" 1
  info "镜像构建完成"
fi

# ---------- 6. 冒烟自检 ----------
step 6 "运行冒烟自检 scripts/smoke.py"
"$VENV_PY" scripts/smoke.py || fail "冒烟自检未通过,已中止启动。" 1
info "冒烟自检通过"

# ---------- 7. 演示数据 ----------
step 7 "检查 data/processed 是否需要演示数据"
CSV_COUNT="$(find data/processed -maxdepth 1 -name '*.csv' -type f 2>/dev/null | wc -l | tr -d ' ')"
if [[ "$CSV_COUNT" == "0" ]]; then
  info "data/processed 为空,生成演示数据 ..."
  "$VENV_PY" scripts/make_demo_data.py || fail "演示数据生成失败。" 1
else
  info "data/processed 已有 $CSV_COUNT 个 CSV,跳过演示数据生成"
fi

# ---------- 8. 启动服务 ----------
step 8 "启动 Web 服务并打开页面"
PORT_IN_USE=false
if command -v curl >/dev/null 2>&1 && curl -fsS --max-time 2 "$APP_URL/api/health" >/dev/null 2>&1; then
  PORT_IN_USE=true
fi

if [[ "$PORT_IN_USE" == "true" ]]; then
  info "端口 8000 已有服务在监听,跳过启动,直接打开页面"
else
  mkdir -p outputs
  nohup "$VENV_PY" web/run.py > outputs/server.out.log 2>&1 &
  info "服务进程已启动,等待健康检查 ..."
  READY=false
  for _ in $(seq 1 30); do
    sleep 1
    if curl -fsS --max-time 2 "$APP_URL/api/health" >/dev/null 2>&1; then
      READY=true
      break
    fi
  done
  if [[ "$READY" != "true" ]]; then
    fail "服务启动超时。请查看 outputs/server.out.log。" 1
  fi
  info "健康检查通过"
fi

if command -v open >/dev/null 2>&1; then
  open "$APP_URL" >/dev/null 2>&1 && info "已尝试打开默认浏览器"
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$APP_URL" >/dev/null 2>&1 && info "已尝试打开默认浏览器"
else
  info "无法自动打开浏览器,请手动访问 $APP_URL"
fi

printf '\n\033[32m[完成] 数据分析 Agent 已启动: %s\033[0m\n' "$APP_URL"
exit 0
