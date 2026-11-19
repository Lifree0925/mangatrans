# MangaTrans 项目开发指南

## 定位

AI 漫剧剧本翻译系统（Windows 桌面版）：本地优先工作台，将 5–8 万字中文剧本稳定翻译为自然、忠实、连续一致的英文剧本。支持 Express（一键直出）与 Guided（人工精修）两种工作流。

## 文档权威

- 设计决策以根目录《开发文档_AI漫剧剧本翻译系统.md》为**唯一权威**；冲突时以该文档为准。
- 修改设计必须先改开发文档、再改代码；禁止代码与文档背道而驰。
- 本文件只做执行速查与经验沉淀，不重复设计细节。

## 架构速览

- `src-tauri/`：Tauri 2 桌面壳（窗口、生命周期、sidecar 管理、安装包）。
- `frontend/`：React + TypeScript + Vite + Tailwind v4 + React Router + TanStack Query。视觉基调参考 Claude Code 风格（暗色、琥珀 `#d97757` 强调、编辑区等宽字体）。
- `backend/`：Python 3.12 + FastAPI sidecar + SQLite + SQLAlchemy + Alembic。业务唯一可信状态在 SQLite；长任务走 SQLite 任务表 + 单 Worker 串行执行。

安全基线：后端只监听 `127.0.0.1`、动态端口、随机会话令牌（`x-auth-token`）；API Key 不进 SQLite/日志/快照/导出；开发凭据走可移植 keyring 后端。

## 常用命令

| 场景 | 命令 |
| --- | --- |
| 后端开发启动 | `backend/run_dev.sh`（自动迁移 + 热重载；`MT_DATA_DIR` 换数据目录，`MT_PORT` 换端口，默认 dev 令牌 `dev-token-local`） |
| 前端开发启动 | `cd frontend && pnpm dev`（Vite 5173，环境变量见 `.env.development`） |
| 后端测试 | `cd backend && .venv/bin/pytest -q` |
| 生成迁移 | `cd backend && .venv/bin/alembic revision --autogenerate -m '<说明>' && .venv/bin/alembic upgrade head` |
| Tauri 编译检查 | `cd src-tauri && cargo check` |

## 工程原则（硬约束，摘自开发文档 24 章与附录 B）

1. 解析只做机械切分（heading / dialogue / narrative / other），**不做语义分类**；复杂度 O(n log n) 以内，8 万字解析 < 2 秒；解析结果不阻塞翻译，覆盖率必须 100%。
2. 翻译必须两段式：Draft 锁事实（允许生硬）→ Polish 情境自然化（不得改变 Draft 事实）；两轮都入库，polished 为对外默认版本。
3. Express 与 Guided 共用同一管线，差异只在确认节点是否自动通过；`auto_approved` 仅限人名/高频术语且必须带 explicit 证据。
4. 场景卡自动生成、默认生效，不逐场人工审核；未来信息只能进 `spoiler_guard`。
5. 窗口串行执行并传递 carry-over，默认并发 1；开启并发必须禁用 carry-over。
6. 所有 LLM 输出必须 Pydantic 校验；LLM 不允许直接写数据库或控制系统流程。
7. 任务必须可持久化、可暂停、可恢复；不引入租约/心跳/worker_id（用 started_at 超时回收）。
8. 知识、译文、Prompt、上下文快照必须可追溯；原文不可覆盖，译文修改必须新版本。
9. 不引入向量数据库、RAG、Redis、Celery、多 Agent 框架；Docker 不作为用户运行前置。
10. 每个模块先写接口和测试，再实现业务逻辑；禁止把所有逻辑堆进 FastAPI 路由函数。
11. 模块输出 Schema 与开发文档 11 章数据模型保持一致；改表结构必须配 Alembic migration。

## 开发内循环

- 后端 uvicorn `--reload`、前端 Vite HMR；只构建/重启受影响服务。
- 验收与发布再走完整构建（PyInstaller + Tauri bundle + GitHub Actions Windows runner），本机 Linux 不产出 Windows 安装包。

## 本仓库实测的坑（务必遵守）

1. **长驻后台进程**：必须 `setsid nohup <cmd> </dev/null >log 2>&1 &`，否则 exec 会话结束进程被清理。
2. **杀端口进程**：用 `fuser -k <port>/tcp`；禁止 `pkill -f 'uvicorn app.main'`（会匹配自身 bash 命令行，导致当前会话被 SIGTERM 自杀）。
3. **alembic autogenerate**：`app/models/__init__.py` 必须显式导入全部模型，否则生成空迁移。
4. **alembic 目标库**：由 `MT_DATA_DIR` 决定；切换数据目录后必须重新 `alembic upgrade head`，且迁移文件不得先删（否则 `alembic_version` 悬空报 "Can't locate revision"）。
5. **SQLAlchemy Python 端 default**（如 `new_id`）：flush 后才生成，flush 前实例 `id` 为 None；需要映射 ID 时先 `db.flush()` 再读。
5b. **后台线程/任务里的 Session**：不要用顶部 `from app.core.database import SessionLocal` 的旧引用（测试替换 engine 后失联）；用 `import app.core.database as dbmod` 后运行时取 `dbmod.SessionLocal()`。
6. **pytest conftest**：先 `import app.main` 注册全部模型再 `create_all`；每个测试重建 engine 与 SessionLocal 并换 data_dir。
7. **FastAPI 中间件顺序**：后注册的先执行；`auth_guard` 必须放行 `OPTIONS` 预检（预检不带自定义头）。
8. **解析器约定**：`source_text` 保留原始行（含换行），偏移为 Unicode 码点；片段拼接必须能完整还原原文。
9. **网络**：直连 GitHub/rustup 会超时，走本机 Clash 代理 `http://127.0.0.1:7890`（curl/cargo 用 `https_proxy`）。

## 数据模型速查

`projects` / `source_documents` / `chapters` / `scenes` / `source_segments` / `knowledge_items` / `knowledge_aliases` / `knowledge_evidence` / `scene_context_cards` / `translations` / `qa_issues` / `tasks` / `task_items` / `audit_logs` / `provider_configs` / `app_settings`。

## 当前进度

- Sprint 1 ✅：桌面骨架、项目 CRUD、导入解析后端骨架、前端项目列表。
- Sprint 1 ✅ / Sprint 2 ✅（解析器+导入 API+前端工作台）/ Sprint 3 ✅（知识提取、场景卡、任务骨架，FakeProvider 测试）。
- 进行中：Sprint 4 模型配置/风格基线/两段式试译 → Sprint 5 翻译任务与恢复 → Sprint 6 QA/一键直出/导出/发布。
- Git：按用户要求，全部 Sprint 完成后统一提交，中途不提交。
