# MangaTrans — AI 漫剧剧本翻译系统

将 5–8 万字中文剧本稳定翻译为自然、忠实、连续一致的英文剧本的本地优先 Windows 桌面工作台。剧本解析、人物术语整理、情境化翻译、质量检查、人工修订与导出全部在本机完成，模型由用户自行配置服务商与 API Key。

## ✨ 特性

- **一键直出（Express）**：导入剧本后全自动完成解析 → 人物/术语整理 → 风格基线 → 全本翻译 → QA → 导出，全程无需人工干预
- **人工精修（Guided）**：在知识审核、风格确认、质量门禁处人工把关
- **情境准确翻译**：每场戏自动生成情境卡（谁在场、之前发生什么、台词意图），台词翻译贴合场景
- **两段式翻译**：Draft 忠实草稿锁死事实 → Polish 情境自然化，同时抑制"AI 幻想"与"生硬直译"
- **跨章节一致**：人物姓名/称呼/术语长期一致；滚动连续性（carry-over）保证前后文衔接；剧情时间线过滤防止未来秘密提前泄露
- **可靠**：任务可暂停/恢复、译文逐段版本管理、知识带原文证据、QA 问题可追溯到具体证据
- **本地安全**：API Key 存系统凭据库，后端仅监听 127.0.0.1，每次启动随机会话令牌

## 📦 下载安装

从 [Releases](https://github.com/Lifree0925/mangatrans/releases) 下载 `mangatrans_x.x.x_x64-setup.exe` 安装即可（Windows 10/11 x64，无需安装 Python/Node/Docker）。

> 自签名证书构建的安装包首次运行可能被 SmartScreen 提示"未知发布者"，点"仍要运行"即可；正式发布建议配置受信任的代码签名证书（仓库 Secrets：`WINDOWS_SIGN_PFX_BASE64` / `WINDOWS_SIGN_PASSWORD`）。

## 🚀 使用流程

1. 新建项目 → 配置模型服务商（Base URL / API Key / 模型名，兼容 OpenAI 接口）
2. 导入剧本（TXT / Markdown / DOCX）→ 自动解析（几秒内完成，含覆盖率校验）
3. **一键直出**：等待自动完成，下载英文剧本 / 中英对照 Markdown
4. 或走精修流程：审核人物术语 → 试译确认风格 → 批量翻译 → QA 修订 → 导出

## 🛠 开发

### 环境

- Python 3.12（后端 FastAPI + SQLite）
- Node.js 22 + pnpm 10（前端 React + TypeScript + Vite）
- Rust stable + Tauri 2 系统依赖（Windows 桌面壳）

### 目录结构

```
frontend/     React + TS + Tailwind（Claude Code 风格暗色 UI）
backend/      FastAPI sidecar：解析、知识库、翻译管线、任务系统、QA、导出
src-tauri/    Tauri 2 桌面壳：生命周期、sidecar 管理、安装包
AGENTS.md     开发执行速查与工程原则
```

### 常用命令

```bash
# 后端（自动迁移 + 热重载；开发令牌 dev-token-local）
cd backend && ./run_dev.sh

# 前端（Vite HMR，http://localhost:5173）
cd frontend && pnpm dev

# 后端测试（24 个，覆盖解析/知识/两段式翻译/一键直出全链路）
cd backend && .venv/bin/pytest -q

# 数据库迁移
cd backend && .venv/bin/alembic revision --autogenerate -m '说明' && .venv/bin/alembic upgrade head
```

### 本地测试（无需每次下载安装包）

**方式 A · Docker Compose（推荐，浏览器模式，秒级热更新）**

```bash
docker compose up --build
# 前端 http://localhost:5173 · 后端 http://127.0.0.1:8765
```

代码改动即时生效（后端 `--reload`、前端 Vite HMR），SQLite 数据保存在 Docker volume。适合日常验证解析、知识、翻译、QA、一键直出等全部业务链路。

**方式 B · 本机脚本（需要 Node + Python）**

```bash
./scripts/dev_up.sh        # Linux / macOS
./scripts/dev_up.ps1       # Windows
```

**方式 C · 桌面壳联调**

```bash
cd frontend && pnpm dev          # 终端 1
cd backend && ./run_dev.sh       # 终端 2
cd src-tauri && cargo tauri dev  # 终端 3（需 Rust 工具链）
```

**方式 D · 便携版（与安装包同一构建产物）**

每个 Release 附带 `mangatrans_x.y.z_portable_x64.zip`：解压即运行，免安装。设置环境变量 `MT_DATA_DIR` 可隔离测试数据（例如 `D:\mt-test-data`）。

> 边界：Docker/浏览器模式验证业务逻辑；sidecar 生命周期、安装包 DLL、图标、签名等只能在桌面壳或安装包环境验证。

### 发布

推 `v*` tag 触发 GitHub Actions（Windows runner）：构建 Python sidecar → 前端 → Tauri NSIS 安装包 → SHA-256 → Release。签名证书配置于仓库 Secrets（见 workflow 注释）。

## 📄 文档

- [AGENTS.md](AGENTS.md)：工程原则、开发命令速查与执行约束
