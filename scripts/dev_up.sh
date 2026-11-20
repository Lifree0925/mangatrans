#!/usr/bin/env bash
# 本地开发内循环（不经 Docker）：后端 --reload + 前端 Vite HMR
set -e
cd "$(dirname "$0")/.."

if [ ! -d backend/.venv ]; then
  echo "初始化 Python 虚拟环境…"
  python3 -m venv backend/.venv
  backend/.venv/bin/pip install -q -e 'backend[dev]'
fi

cleanup() { kill "$BACK_PID" "$FRONT_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

( cd backend && ./run_dev.sh ) & BACK_PID=$!
( cd frontend && pnpm dev ) & FRONT_PID=$!

echo "前端 http://localhost:5173 · 后端 http://127.0.0.1:8765（Ctrl+C 退出）"
wait
