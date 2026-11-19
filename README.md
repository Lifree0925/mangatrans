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

> 未做代码签名的构建首次运行可能被 SmartScreen 提示"未知发布者"，点"仍要运行"即可。

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
docs/         文档
开发文档_AI漫剧剧本翻译系统.md   设计权威文档（先读它）
AGENTS.md     开发执行速查与经验沉淀
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

### 发布

推 `v*` tag 触发 GitHub Actions（Windows runner）：构建 Python sidecar → 前端 → Tauri NSIS 安装包 → SHA-256 → Release。签名证书配置于仓库 Secrets（见 workflow 注释）。

## 📄 文档

- [开发文档](开发文档_AI漫剧剧本翻译系统.md)：产品边界、总体架构、数据模型、翻译方法论、验收标准
- [AGENTS.md](AGENTS.md)：AI 编码执行约束与工程原则
