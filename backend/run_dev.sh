#!/usr/bin/env bash
# 开发模式启动：迁移 + 固定令牌 + 热重载
set -e
cd "$(dirname "$0")"
export MT_AUTH_TOKEN="${MT_AUTH_TOKEN:-dev-token-local}"
export MT_DATA_DIR="${MT_DATA_DIR:-$HOME/.local/share/mangatrans}"
.venv/bin/alembic upgrade head
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "${MT_PORT:-8765}" --reload
