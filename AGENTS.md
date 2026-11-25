# MangaTrans 项目开发指南

## 定位

AI 漫剧剧本翻译系统（Windows 桌面版）：本地优先工作台，将 5–8 万字中文剧本稳定翻译为自然、忠实、连续一致的英文剧本。默认 Express 一键直出与自动质检；Guided 仅作兼容入口，不作为用户英语验收前置。

## 设计原则

- 核心设计已固化于代码与本文档：机械切分解析、两段式翻译（Draft→Polish）、carry-over 连续性、Express/Guided 双工作流、SQLite 持久任务。
- 修改核心设计（数据模型、翻译管线、任务机制）前先评估对现有模块的影响，并同步更新本文档。

## 架构速览

- `src-tauri/`：Tauri 2 桌面壳（窗口、生命周期、sidecar 管理、安装包）。
- `frontend/`：React + TypeScript + Vite + Tailwind v4 + React Router + TanStack Query。视觉基调为 ChatGPT 风格浅色界面（白色主题、简洁分区、编辑区等宽字体）。
- `backend/`：Python 3.12 + FastAPI sidecar + SQLite + SQLAlchemy + Alembic。业务唯一可信状态在 SQLite；长任务走 SQLite 任务表 + 单 Worker 串行执行。

安全基线：后端只监听 `127.0.0.1`、动态端口、随机会话令牌（`x-auth-token`）；API Key 不进 SQLite/日志/快照/导出。
正式构建强制使用系统凭据库（Windows Credential Manager），凭据库不可用时明确报错，禁止明文回退；仅开发环境可显式启用文件回退，
启动时把历史明文凭据迁移到系统凭据库并回读确认后删除。Tauri 必须配置严格 CSP 和最小权限 capabilities。

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
3. Express 与 Guided 共用同一管线，差异只在确认节点是否自动通过；`auto_approved` 仅限高置信度、explicit 证据的人名/术语。
4. 场景卡已剔除；情境准确翻译由知识库、章节剧情卡、speaker voice 与 carry-over 承担。
5. carry-over 由 Draft 阶段前置生成并随任务进度持久化；Draft 与 Polish 均窗口级并发（`MT_WINDOW_CONCURRENCY` 默认 3），Polish 消费前一窗口 Draft 的 carry。无待定术语时 Draft/Polish 流水线并行，Polish 在自身及前一窗口 Draft 完成、术语锁定、全局角色档案/风格基线就绪、且本窗口所属章节剧情卡完成后启动，不等待无关章节剧情卡或台词本抽取；存在待定术语时等待全量 Draft 完成并锁定术语后再启动 Polish。任务级进度由调度层单协程顺序写。
6. 思考模式：仅 Polish 与语义 QA 开启；Draft、抽取、对齐等环节关闭。禁止无意义地对不可重试错误反复重试。
7. 结构完整性：`validate_window` 除 segment/行数/重复外，必须校验说话人、场景号、△/【】标记；单行对白被换成相邻段也必须判错。截断（finish_reason=length）优先自动拆半重跑，不能无限重复同一请求。
8. 术语策略：`exact` 只用于专名/地点/头衔/具名物件；普通名词、物种、群体、泛称只做 `contextual` 参考。地点别名统一，头衔/称谓别名按语境翻译，不能一律替换为正式名。
9. 术语命中必须 Unicode 归一化 + 大小写不敏感 + 词边界 + 合法 variants + **最长匹配优先**；同词形多实体歧义时丢弃，禁止后写覆盖。
10. 知识抽取的模型调用可并发，但写库归并严格串行；泛词/泛别名不入库，冲突别名确定性清理。抽取有失败批次时必须显式报错，不能带着残缺知识继续翻译。
11. QA 只抓会破坏理解/连续性的大问题（漏译、加戏、指代、否定/因果、人物一致性、实质术语断裂），不查大小写、标点、措辞偏好。语义 QA 必须带窗口相关项目事实，问题必须能定位到 `segment_id`，修复只改 polished 问题段。
12. QA 结果按窗口替换而不是永久追加；导出/审批只读取每段最新 polished 的告警。旧版误报在读取/导出/审批前做本地确定性校准。
13. 所有 LLM 输出必须 Pydantic 校验；LLM 不允许直接写数据库或控制系统流程。译文写入按 `segment_id` 映射，版本号原子分配。
14. 任务必须可持久化、可暂停、可恢复；不引入租约/心跳/worker_id（用 started_at 超时回收）。
15. 知识、译文、Prompt、上下文快照必须可追溯；原文不可覆盖，译文修改必须新版本。
16. 不引入向量数据库、RAG、Redis、Celery、多 Agent 框架；Docker 不作为用户运行前置。
17. 每个模块先写接口和测试，再实现业务逻辑；禁止把所有逻辑堆进 FastAPI 路由函数。
18. 模块输出 Schema 与开发文档 11 章数据模型保持一致；改表结构必须配 Alembic migration。
19. 自动质量门禁：语义 QA 异常/超时/坏 JSON 必须返回明确失败，不能返回空问题列表冒充通过；必须覆盖每个翻译窗口，最多 3 轮定点修复，修复后重新检查。
20. 项目成功与交付：只有全部原文段有当前 polished、结构校验通过、每窗语义 QA 成功、无未解决 high/critical 且术语一致时，任务才可 completed；否则 failed。交付版导出必须再过项目级门禁。用户不做人工英语校验。
21. 自动定稿：质量门禁通过后系统自动 approved；`/approve/batch` 只能调用同一自动门禁，不能绕过 QA 缺失或 needs_review。
22. 用量账本：所有真实 Provider 调用写入 `llm_calls`（task/stage/model/prompt/completion/reasoning/cache_hit/latency/error）；缓存命中不计 API Token。
23. 性能：默认窗口上限 1800 字符（环境变量可调）；上下文超预算按低优先级裁剪，源文永不裁剪；知识/别名按项目签名缓存。
24. 重试：只有 timeout/network/429/5xx/明确 retryable 才重排；质量门禁、结构校验、认证、参数错误、截断最终失败不得无限重试。
25. 台词本：从解析器 dialogue 片段与 speaker_name 确定性生成；只有多行对白无法按行数对齐时才允许模型介入。
26. 任务并发：数据库必须保证同一项目最多一个 `queued/running/paused` 任务；重试、暂停、恢复和窗口领取使用条件更新/原子领取，
    不能只依赖应用层“先查再写”。
27. LLM 兼容降级：400 时按 `thinking/reasoning_effort` → `response_format` 逐级移除并记录原因；缓存 Key 必须包含实际请求体
    （含实际 temperature、默认 max_tokens），禁止不同生成配置互相复用。
28. 独立 QA：语义 QA 使用 source-first 独立复核策略，并附带确定性覆盖率信号（否定、条件、数字、术语）；信号用于复核定位，
    不单独制造整窗失败。人物状态为章节级状态 + 段落级秘密揭示，暂不做场景级细粒度追踪；
    `character_states` / `story_facts` 由剧情卡确定性派生，不额外增加 LLM 调用，翻译上下文与 QA 只能读取
    当前章节及以前生效的信息，未来章节事实不得提前进入。

## 开发内循环

- 后端 uvicorn `--reload`、前端 Vite HMR；只构建/重启受影响服务。
- 验收与发布再走完整构建（PyInstaller + Tauri bundle + GitHub Actions Windows runner），本机 Linux 不产出 Windows 安装包。

## 发版边界（用户 2026-11-21 明确要求）

- **只有用户明确说"发版"才发版**；用户未发出发版指令时，只修改本地文件并跑测试，
  不 commit、不 push、不打 tag、不触发 CI 构建。
- 发版指令一旦给出，流程为：bump 版本号（`tauri.conf.json` / `frontend/package.json` / `update.json`）
  → 前后端测试 → commit → push main → tag `vX.Y.Z` → push tag → 盯 CI → 确认 release 资产。
- 应用内更新走 `tauri-plugin-updater`：updater 公钥在 `tauri.conf.json`，私钥和密码只放本机
  `~/.tauri/mangatrans-updater.key` / `.password`，绝不入仓库。CI 必须配置 Secrets
  `TAURI_SIGNING_PRIVATE_KEY` 与 `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`；发布资产必须包含
  `latest.json` 和对应的 NSIS updater 签名产物。
- 本机项目备份位于 `.secrets/updater/`（已被 `.gitignore` 排除，权限 600，禁止提交）。
- updater 私钥不可轮换，否则已安装客户端无法验证后续更新；`update.json` 继续供仍在使用
  旧版手动更新流程的客户端使用，发版时同步 bump。

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
10. **PyInstaller sidecar**：必须用 **one-file**（one-folder 的 `_internal` 目录无法随 Tauri `externalBin` 分发，安装后报 "Failed to load Python DLL"）；`app/main.py` 必须保留 `if __name__ == "__main__": uvicorn.run(...)` 入口，否则打包后立即静默退出。
11. **Tauri 启动**：setup 中禁止同步等待 sidecar 健康检查（会阻塞窗口数十秒）；用 `spawn_blocking` 后台检查，前端 fetch 对本地回环失败自动重试 60 秒。sidecar 日志写入 `数据目录/logs/sidecar.log`，spawn 失败不得 panic。
12. **更新检查**：使用仓库根 `update.json`（raw.githubusercontent + jsDelivr 双源），不用 GitHub API（匿名请求会 403 限流）。发版时同步更新 `update.json` 的 version。
13. **Windows CI 构建**：`tauri.conf.json` 不得配置 `beforeBuildCommand`（tauri CLI 在仓库根执行它，仓库根无 package.json 会失败）；前端由 workflow 显式构建。CLI 用前端 `@tauri-apps/cli`（`../frontend/node_modules/.bin/tauri build`），不要 `cargo install tauri-cli`。bundle.targets 只打 `["nsis"]`。identifier 避免 `.app` 结尾。
14. **Windows 安装包门禁**：发布前必须运行 `scripts/test_windows_installer.ps1`，真实执行静默安装 → 校验包内 exe → 启动桌面应用 → sidecar `/health` 与 CORS 预检 → 关闭回收 → 静默卸载。任何一步失败禁止 Release。
15. **Windows 签名**：有证书时用 `tauri build --config tauri.signing.conf.json` 签名主程序、sidecar、卸载器和安装器；只签最终 setup.exe 不够，已安装 exe 仍会被 SmartScreen/Defender 拦截。自签名证书只验证签名者，不等于公共信任发布者。
16. **NSIS 配置**：显式 `installMode=currentUser`，WebView2 用 `embedBootstrapper`，安装后用 `windows/installer.nsh` 自检主程序与 sidecar；手动构建脚本必须使用 `backend/dist/mangatrans-backend.exe` one-file 产物。

## 数据模型速查

`projects` / `source_documents` / `chapters` / `scenes` / `source_segments` / `knowledge_items` / `knowledge_aliases` / `knowledge_evidence` / `scene_context_cards` / `translations` / `qa_issues` / `window_quality` / `tasks` / `task_items` / `llm_calls` / `dialogue_lines` / `chapter_storylines` / `character_states` / `story_facts` / `audit_logs` / `provider_configs` / `style_guides`。

## 当前进度

- Sprint 1–6 ✅ 完成；后端 112 个测试全过（含自动质量门禁、用量账本、确定性台词本、窗口/上下文预算、FakeProvider 全链路）。前端 tsc/lint 通过。
- 最近已发布 v0.1.0；本轮 0–4 阶段优化尚未发版。新安装包必须通过 Windows 静默安装/启动/卸载门禁。
- 软件更新：启动检查 GitHub Releases + 侧栏提示 + 设置页手动检查（缓存 24h）。
- 开发文档仅存本地 `docs/`（.gitignore 排除，不入仓库）。
- 签名注意：自签名证书不校验公共信任链，只校验 Authenticode 签名者；换正式证书只需替换 Secrets。
