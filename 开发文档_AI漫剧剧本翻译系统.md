# AI 漫剧剧本翻译系统

## 开发技术文档（Windows 桌面版）

> 产品形态：Windows 桌面应用，通过 GitHub Releases 发布安装包。  
> 核心技术：Tauri 2 + React + TypeScript + Python/FastAPI + SQLite。  
> 核心目标：将 5–8 万字中文剧本稳定翻译为自然、忠实、连续一致的英文剧本；支持一键直出与人工精修两种工作流。
> 部署原则：应用和业务数据在用户本机运行；用户自行配置模型服务商和 API Key；不依赖项目方云端服务器。  

---

# 第一部分：先了解整个项目

## 1. 项目是什么

这是一个**本地优先的 Windows 桌面剧本翻译工作台**，不是在线翻译网站，也不是一个只把全文发给大模型的简单套壳工具。

下载并安装 Windows 安装包，打开软件后创建项目、导入剧本、配置自己的模型 API Key，即可在本机完成剧本解析、知识整理、分段翻译、质量检查、人工修订和导出。

系统提供两种工作流：

- **一键直出（Express）**：导入剧本后，系统自动完成解析、人物与术语整理、风格基线建立、全本两段式翻译、QA 和导出，全程无需人工干预；用户拿到完整英文剧本后可随时审阅、修订和局部重译。
- **人工精修（Guided）**：在关键节点引入人工确认（人物、术语、风格、质量门禁），适合对质量要求高、愿意投入时间的用户。

系统解决的核心问题不是单句翻译，而是长篇剧本的跨章节一致性与可控性：

1. 人物姓名、别名、称呼和代词需要长期一致，但不能机械全局替换。
2. 前后章节的剧情信息需要连续，但不能把未来才揭晓的秘密提前泄露。
3. 台词必须放在具体情境中翻译：谁在场、之前发生了什么、这句话的真实意图是什么。
4. 翻译不能遗漏动作、情绪、潜台词、否定、条件和关键细节；既不能无依据增译（幻想），也不能生硬直译。
5. AI 提取的知识可能有误，不能未经追溯直接成为正式设定（Express 自动批准也必须保留证据）。
6. 5–8 万字的翻译任务可能运行很久，必须可暂停、可恢复、可追溯。
7. 人工修改必须形成新版本，不能覆盖历史译文，也不能无意污染其他章节。

## 2. 产品边界与取舍

### 2.1 V1 必须具备

- Windows 10/11 x64 安装包。
- 无需用户安装 Python、Node.js、Rust、Docker 或数据库。
- 项目、原文、知识库、译文、任务和日志保存在本机。
- 用户自行配置 Provider、Base URL、API Key、模型名称。
- TXT、Markdown、DOCX 导入。
- 机械切分式结构解析、片段切分和原文覆盖校验（解析不做语义分类，不阻塞翻译）。
- 人物、别名、术语候选提取；Express 自动批准，Guided 人工审核。
- 场景情境卡与风格基线（Style Guide）自动生成。
- 两段式翻译（Draft 忠实草稿 + Polish 情境自然化）与滚动连续性（carry-over）。
- 一键直出（Express）与人工精修（Guided）两种工作流。
- 批量翻译、断点续传、逐段版本管理。
- 确定性 QA、按需语义 QA、批量接受、人工修订和导出。
- GitHub Releases 发布安装包。

### 2.2 V1 明确不做

- 不做账号注册、云端项目存储或多人协作。
- 不做积分、付费、订阅和项目方代付 API 费用。
- 不使用向量数据库，不引入 RAG 服务。
- 不构建自主多 Agent 系统。
- 不引入 Redis、Celery 或独立消息队列。
- 不要求用户对解析结果或试译结果做全量人工确认（默认自动通过，精修可选）。
- 不让 AI 未经追溯地写正式知识库（Express 自动批准必须带证据与来源）。
- 不自动联网查资料。
- 不做云端同步和强制遥测。
- 不承诺云端模型 API 调用时剧本文本完全不离开本机。

## 3. 总体架构：

### 3.1 架构图

```text
┌──────────────────────────────────────────────────────────────┐
│                    Windows Desktop Application               │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ Tauri 2                                                │  │
│  │  - Windows 窗口 / 菜单 / 生命周期 / 安装包                │  │
│  │  - 启动、监控、关闭 Python Sidecar                       │  │
│  │  - 本地文件选择、导出目录等桌面能力                         │  │
│  │                                                        │  │
│  │  ┌──────────────────────────────────────────────────┐  │  │
│  │  │ React + TypeScript UI                            │  │  │
│  │  │ 项目列表 / 剧本工作台 / 知识审核 / 任务中心 / 设置     │  │  │
│  │  └──────────────────────────────────────────────────┘  │  │
│  └───────────────────────────┬────────────────────────────┘  │
└──────────────────────────────┼───────────────────────────────┘
                               │ HTTP + SSE
                               │ 仅 127.0.0.1，动态端口
                               ▼
┌──────────────────────────────────────────────────────────────┐
│              Python Sidecar（PyInstaller 打包）               │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ FastAPI 本地 API                                        │  │
│  │ 项目 / 导入 / 解析 / 知识 / 翻译 / QA / 导出 / 设置         │  │
│  └───────────────────────────┬────────────────────────────┘  │
│                              │                               │
│  ┌───────────────────────────▼────────────────────────────┐  │
│  │ Application Services                                   │  │
│  │ Project / Parser / Knowledge / Scene Cards             │  │
│  │ Style Guide / Context Builder / Continuity             │  ││  │ Translation Orchestrator / QA / Export                 │  │
│  └───────────────────────────┬────────────────────────────┘  │
│                              │                               │
│  ┌───────────────────────────▼────────────────────────────┐  │
│  │ Persistent Worker                                      │  │
│  │ SQLite 任务领取 / 租约 / 心跳 / 重试 / 断点恢复             │  │
│  └───────────────────────────┬────────────────────────────┘  │
│                              │                               │
│  ┌───────────────────────────▼────────────────────────────┐  │
│  │ SQLite + 本地项目文件                                    │  │
│  │ 原文版本 / 片段 / 知识 / 译文版本 / QA / 任务 / 审计        │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────┬───────────────────────────────┘
                               │
                               │ 用户主动触发模型任务时 HTTPS
                               ▼
                    用户自行配置的模型服务商
                    Base URL + API Key + Model
```

### 3.2 关键理解

- **Tauri 是桌面外壳**：负责窗口、安装包、应用生命周期和本地桌面集成，不承载翻译业务逻辑。
- **React 是界面层**：负责交互和展示，不直接访问 SQLite，也不保存 API Key。
- **Python/FastAPI 是业务核心**：解析、知识管理、上下文构建、翻译编排、QA、导出均由 Python 实现。
- **Persistent Worker 是任务执行核心**：从 SQLite 领取任务并执行，不依赖 HTTP 请求生命周期。
- **SQLite 是唯一业务状态来源**：任务进度、译文版本和知识版本都以数据库为准，不依赖内存状态。
- **模型服务是外部依赖**：通过统一 Provider Adapter 接入。模型只负责语义理解、候选提取和翻译，不控制系统流程。
- **用户数据与程序文件分离**：软件升级或卸载不能默认删除用户的项目数据。

## 4. 技术栈与选型说明

| 层级       | 技术                                           | 主要职责                           | 说明                                     |
| ---------- | ---------------------------------------------- | ---------------------------------- | ---------------------------------------- |
| 桌面框架   | Tauri 2                                        | Windows 桌面窗口、生命周期、安装包 | 相比 Electron 更轻；V1 只面向 Windows    |
| 前端       | React + TypeScript + Vite                      | 项目管理、编辑器、双语工作台       | 前端静态资源随安装包发布                 |
| UI         | Tailwind CSS + shadcn/ui                       | 组件与样式                         | 现代、简洁、可维护                       |
| 状态管理   | Zustand                                        | UI 状态、任务进度、编辑状态        | 服务端数据仍以 API/SQLite 为准           |
| 路由       | React Router                                   | 页面路由                           | 单机桌面 SPA                             |
| 数据请求   | TanStack Query                                 | API 请求、缓存和刷新               | 避免将服务端状态重复塞进 Zustand         |
| 后端       | Python 3.12 + FastAPI                          | 本地 API 和业务服务                | 打包成独立 sidecar                       |
| 后端打包   | PyInstaller                                    | 将 Python 和依赖打包               | 先采用 one-folder，验证后再评估 one-file |
| ORM        | SQLAlchemy 2.x                                 | 数据库模型与访问                   | 业务层不直接拼接 SQL                     |
| 数据库     | SQLite                                         | 所有项目业务状态                   | 单机应用足够；开启外键和 WAL             |
| 迁移       | Alembic                                        | 数据库 Schema 版本升级             | 每次版本升级可迁移、可备份               |
| 数据校验   | Pydantic v2                                    | API 和模型输出 Schema              | LLM 返回必须校验                         |
| 文档解析   | python-docx、Markdown parser、纯文本解析       | TXT/MD/DOCX 导入                   | 原文必须保留                             |
| Token 估算 | tiktoken 或 Provider Token Adapter             | Prompt 预算                        | 估算值不可代替模型实际限制               |
| 模型接入   | OpenAI SDK 兼容客户端 + Provider Adapter       | 模型请求统一封装                   | 支持自定义 Base URL / Key / Model        |
| 本地凭据   | Windows Credential Manager / Credential Locker | API Key 保存                       | SQLite 不存明文 Key                      |
| 桌面打包   | Tauri Bundler                                  | NSIS EXE / MSI                     | 首发优先 NSIS EXE                        |
| 发布       | GitHub Actions + GitHub Releases               | Windows 构建与分发                 | 固定依赖并生成 SHA-256                   |

### 4.1 为什么保留 Python 后端

项目中最复杂的工作是文档解析、结构化数据、任务编排、文本校验和模型调用。Python 在这些领域生态成熟。为了获得桌面软件体验，不需要把业务逻辑全部改写成 Rust；由 Tauri 管理 Python sidecar 即可。

### 4.2 为什么使用 Tauri + Python Sidecar

Tauri 提供桌面应用形态，Python 继续承载业务逻辑。最终用户拿到安装包后不需要单独安装 Python。Tauri 负责启动 sidecar、获取进程状态和关闭进程；FastAPI 只在本机回环地址提供 API。

### 4.3 为什么不用 Docker

Docker 更适合开发、服务器部署和可控运行环境，不适合作为普通 Windows 用户的必备前置条件。本项目最终交付目标是“下载、安装、打开即可使用”，因此 Docker 仅可作为开发者自选环境，不属于正式产品运行依赖。

## 5. 核心工作流：

### 5.1 用户完整流程

```text
Express（一键直出，全程无人工干预）
导入 TXT / MD / DOCX
  ↓
自动解析（机械切分 + 覆盖校验，几秒内同步完成）
  ↓
自动知识整理：人名 / 高频术语自动批准（auto_approved），场景卡自动生成
  ↓
自动试译并归纳风格基线（Style Guide）
  ↓
全本批量翻译（每个窗口两段式：Draft → Polish，carry-over 滚动连续）
  ↓
确定性 QA + 语义 QA（风险触发）
  ↓
批量接受 + 导出完整英文剧本（附问题报告）
  ↓
（用户拿到成品后可随时审阅、修订、局部重译）

Guided（人工精修，在关键节点把关）
导入 → 解析（可审异常项）→ 知识候选审核（人物 / 术语）
  ↓
试译与风格基线（可人工调整，也可跳过）
  ↓
批量翻译 → QA → 人工修订 / 批准 → 导出
```

两条工作流共用同一套翻译管线，差异仅在"确认节点"是否自动通过。

### 5.2 一次翻译窗口的内部流程

```text
Worker 从 SQLite 领取一个 pending task_item
  ↓
检查任务是否暂停、取消或已完成
  ↓
加载本次任务冻结的配置快照 + 风格基线 + Prompt 版本
  ↓
Context Builder 组装：场景卡 + 已批准知识（按剧情位置过滤未来信息）
  + 滚动连续性块（上一窗口 carry-over）
  ↓
估算 Token，裁剪非关键上下文
  ↓
Draft 轮：忠实直译，锁死事实（人物、动作、否定、因果、时序、数量）
  ↓
Polish 轮：基于情境与风格基线自然化，不得改变 Draft 的事实
  ↓
Pydantic 校验 JSON 结构、segment 完整性、强制术语、原文哈希
  ↓
事务提交 draft / polished 两个译文版本
  ↓
生成下一窗口的 carry-over 连续性块
  ↓
执行确定性 QA，更新 task_item 与任务进度
  ↓
领取下一个窗口
```

每个窗口独立提交。某一窗口失败不应导致已成功窗口回滚。

### 5.3 数据流与责任边界

| 环节     | 程序负责                                     | AI 负责                           | 用户负责                 |
| -------- | -------------------------------------------- | --------------------------------- | ------------------------ |
| 导入     | 文件读取、哈希、原文留档                     | 不参与                            | 选择文件                 |
| 解析     | 机械切分、覆盖检查、异常报告                 | 低置信度批量分类辅助（可选）      | 修正异常项（可跳过）     |
| 知识提取 | 分批、Schema、证据检查、候选入库             | 提出候选与证据                    | Guided 确认；Express 不参与 |
| 场景卡   | 生成任务编排、剧情可见性过滤                 | 自动生成情境卡                    | 可修订（可选）           |
| 风格基线 | 试译编排、归纳流程、冻结                     | 试译代表性场景并归纳 Style Guide  | 可调整（可选）           |
| 翻译     | 窗口顺序、Prompt、两段式校验、carry-over     | 生成 Draft 与 Polish              | 审阅、修订（可选）       |
| QA       | 确定性规则、问题入库                         | 语义风险检查                      | 批量接受或逐条处理       |
| 导出     | 顺序、完整性、格式生成                       | 不参与                            | 选择导出范围             |

**总原则：程序管理事实和流程，AI 负责语言理解与生成，用户按所选工作流决定参与程度；Express 模式下用户参与度为零。**

### 5.4 前端视觉风格参考

界面视觉基调参考 Claude Code 风格：

- 暗色为主的主题，低对比柔和色阶，单一暖色强调色（琥珀/珊瑚色系）用于关键操作与选中态。
- 极简边框与分隔，避免大面积色块、渐变和装饰性元素。
- 剧本原文/译文编辑区使用等宽字体（monospace），带行号，贴合"文本工作台"气质。
- 状态语义色（成功/警告/错误/进行中）在暗色主题下保证可读性。
- 信息密度优先：列表、表格紧凑；长文本区域保持舒适行高。
- shadcn/ui 组件定制为暗色主题，视觉语言统一。

（Claude Code 本身是终端 UI，这里借鉴其视觉语言，不是照搬交互形态。）

# 第二部分：详细技术设计

## 6. 前端页面与交互结构

### 6.1 页面

1. **项目列表**
   - 新建、打开、重命名、归档、删除项目。
   - 显示原文字符数、章节数、当前翻译进度、待审核数量、更新时间。
2. **项目工作台**
   - 左侧：章节和场景树。
   - 中间：原文与译文双栏编辑区。
   - 右侧：当前片段上下文、场景卡、人物、术语和 QA 问题。
   - 顶部：导入、解析、一键直出、知识整理、试译/风格、批量翻译、QA、导出。
3. **项目知识库**
   - 人物和关系。
   - 专有名词与称呼规则。
   - 场景卡与关键事件。
   - 项目翻译规则。
   - 候选审核队列。
   - 以上为同一知识库的不同视图，不建立多个独立知识系统。
4. **任务中心**
   - 知识提取、翻译、QA 任务（解析与导出为同步操作）。   - 显示状态、阶段、进度、错误、暂停、恢复和重试。
5. **设置**
   - Provider、Base URL、API Key、模型名称。
   - 连接测试、保存、更新和删除凭据。
   - 默认模型与任务级模型选择。
   - Prompt 版本、并发数、窗口目标长度。
   - 数据目录、日志和应用版本。
### 6.2 前端工程约束

- React 组件不得直接访问 SQLite 或本地文件系统。
- 所有业务读写通过 FastAPI API。
- Tauri API 只用于桌面能力，如打开文件选择器、读取应用路径、启动生命周期等。
- API Key 输入控件不得写入 localStorage、sessionStorage、Zustand 持久化或前端日志。
- 长任务进度通过 SSE 获取；断线后重新请求任务详情并恢复进度。
- 原文编辑和译文编辑要区分：原文结构修正不等于修改源文件内容；人工修改译文必须创建新版本。
- 对可能影响历史译文的知识修改，前端必须显示影响范围与 stale 提示。

## 7. 桌面进程生命周期与本地 API 安全

### 7.1 启动流程

1. Tauri 启动。
2. 获取应用数据目录。
3. 检查是否已有本应用实例运行；V1 使用单实例策略。
4. 在本地创建随机会话令牌。
5. 启动打包的 Python sidecar，并传入数据目录、动态端口候选和会话令牌。
6. Python 后端绑定 `127.0.0.1`，优先由系统分配可用端口。
7. 后端写入启动握手信息（端口、进程实例 ID、健康状态）；握手文件权限应限制为当前用户。
8. Tauri 校验进程实例 ID 和会话令牌，调用健康检查。
9. 前端加载后端 API 地址并显示工作台。
10. 启动 Persistent Worker，恢复过期租约。

不得把端口写死为 8000，也不得监听 `0.0.0.0`。

### 7.2 关闭流程

1. Tauri 通知后端进入 shutdown 状态。
2. 后端停止领取新任务。
3. 当前窗口如果尚未提交，记录可恢复状态；不把未提交结果当成成功。
4. 完成数据库事务并关闭连接。
5. Worker 退出。
6. FastAPI 退出。
7. Tauri 回收 sidecar 进程。

强制结束进程时，由下次启动的租约恢复机制处理，不依赖优雅退出一定成功。

### 7.3 本地 API 安全

本地服务不能因为监听 localhost 就默认安全。必须实施：

- 仅绑定 `127.0.0.1`。
- 每次启动生成高强度随机会话令牌。
- 所有 API 请求必须携带令牌；令牌不写入 URL。
- 限制允许的 Origin 为当前 Tauri 应用来源。
- 对浏览器跨站请求实施 Origin / Host 校验和适当的 CSRF 防护。
- 不开放任意文件路径读写 API。
- 导入和导出路径必须由文件选择流程或后端安全校验确定。
- 错误响应不返回 API Key、系统凭据或敏感路径。
- 不提供局域网访问模式。

## 8. 用户模型配置与 API Key 安全

### 8.1 Provider 配置模型

V1 通过统一 Provider Adapter 兼容 OpenAI 风格接口。每个 Provider 配置至少包含：

- `provider_id`
- `display_name`
- `base_url`
- `model_name`
- `credential_ref`
- `api_mode`（例如 OpenAI-compatible）
- `timeout_seconds`
- `max_retries`
- `is_default`
- `config_version`

API Key 本身不存入上述普通配置表，只存 Windows Credential Manager / Credential Locker 中，并在数据库保存 `credential_ref`。

### 8.2 用户操作流程

1. 用户打开设置。
2. 选择预设 Provider 或选择“自定义兼容接口”。
3. 填写 Base URL、API Key、模型名称。
4. 点击测试连接。
5. 后端从系统凭据存储读取 Key，发送最小测试请求。
6. UI 只显示测试成功/失败、模型名称和脱敏错误。
7. 保存配置；后续任务使用配置版本快照。

### 8.3 安全要求

- API Key 不得写入 SQLite、`.env`（正式用户配置）、日志、任务快照、错误堆栈、导出文件或 Git。
- 前端不持久化 API Key；保存后只返回“已配置”和脱敏状态。
- 修改 Key 时允许覆盖系统凭据；删除 Key 不删除项目。
- 错误日志必须过滤 `Authorization`、`api_key`、`token` 等字段。
- 自定义 Base URL 默认要求 HTTPS；用户明确使用本机测试地址时才允许 HTTP，并显示风险提示。
- 用户使用云端模型时，界面明确说明当前任务文本会发送到该服务商。
- 不做隐藏网络请求、内容遥测或后台上传。

## 9. 模型接入层（Provider Adapter）

### 9.1 设计目标

业务服务不能直接依赖某一家模型厂商 SDK。统一通过 `LLMProvider` 接口发起请求，使用户可配置不同兼容服务。

建议接口：

```python
class LLMProvider(Protocol):
    async def chat(
        self,
        *,
        model: str,
        messages: list[dict],
        response_schema: dict | None,
        temperature: float | None,
        timeout: float,
        request_id: str | None,
    ) -> LLMResponse: ...

    async def test_connection(self) -> ProviderTestResult: ...
```

`LLMResponse` 统一包含：

- `content`
- `provider_id`
- `model_name`
- `request_id`
- `usage`（若服务商返回）
- `finish_reason`
- `latency_ms`
- `raw_response_metadata`（仅允许安全字段）

不得在日志或数据库保存包含密钥的原始请求头。

### 9.2 模型能力配置

不同服务商对 JSON Schema、结构化输出、上下文长度、超时和 usage 的支持可能不同。Provider Adapter 必须维护能力声明：

- `supports_json_schema`
- `supports_json_object`
- `supports_streaming`
- `context_window`
- `max_output_tokens`
- `supports_idempotency_key`

不支持严格 JSON Schema 的模型，使用 JSON 输出模式并在本地进行 Pydantic 校验。模型能力未知时采用保守默认值，不得假定支持。

### 9.3 请求与重试

- 网络超时、限流、服务端临时错误可重试。
- 认证错误、模型不存在、参数错误不盲目重试。
- 指数退避并设置最大次数。
- 每次请求记录 request ID、模型、耗时、token usage（若可得）和错误分类。
- API Key 不得进入 request ID。
- 如果服务商支持幂等键，使用稳定的窗口请求 ID；不支持时接受崩溃边界处可能发生一次重复计费。

---

# 第三部分：原文、知识和译文的数据模型

## 10. 统一知识库设计

系统只维护一个项目知识库，底层按数据类型和状态组织。

### 10.1 三类数据域

**A. 规范知识 Canonical Knowledge**

- 人物：规范中文名、英文名、角色类型、剧情明确的属性、简介。
- 别名：昵称、头衔、姓名称呼、文本中其他称呼。
- 人物关系：关系类型、生效范围、证据位置。
- 术语：组织、地点、道具、职位、特殊设定。
- 翻译规则：固定译法、偏好译法、语境规则、禁止译法（黑名单）。
- 项目规则：美式英语、对白口语程度、专名大小写等。

**B. 场景情境卡 Scene Context Card**

- 每场一张卡，AI 自动生成，默认直接生效。
- 地点/时间、在场人物及进场状态、前情提要、本场冲突或目标、情绪基调。
- 关键台词意图与潜台词标注（可人工加注）。
- 该场不得提及的后续事实（spoiler_guard，供剧情可见性过滤）。
- 与前场的衔接关系。

**C. 翻译资产 Translation Assets**

- 原文片段与译文版本（draft / polished / final）。
- 人工修订记录。
- 用户确认的特殊表达。
- 项目英文风格规则与冻结的风格基线（Style Guide）。
- 滚动连续性块（carry-over）。
- 少量精选典型对白案例。

### 10.2 知识状态

```text
AI 提取 / 用户新增
        │
        ▼
    candidate
      ├── Guided：用户确认 ──> approved
      ├── Guided：用户修改 ──> approved（保留修改前后版本）
      ├── Express：自动批准 ──> auto_approved
      └── 用户拒绝 ──> rejected
```

- approved 与 auto_approved 均可作为强约束进入 Prompt。
- auto_approved 仅限 Express 模式下的"人物规范名、别名、高频强制术语"，且必须满足：至少一条原文显式证据（explicit）；只做确定性精确归并；不确定项保持候选不生效。
- 用户事后修改 auto_approved 知识，同样走版本与 stale 管理。
- 不确定的剧情推断不作为强约束，可保留在场景卡中作为提示性信息。

### 10.3 证据要求

AI 提出的每条知识至少记录：

- 原文引用。
- 来源 `source_document_id`、章节和 `segment_id`。
- 结论类型：explicit / inferred / uncertain。
- 模型置信度（只用于候选排序，不作为自动批准标准）。
- 创建任务和模型信息。

### 10.4 剧情时间可见性

“已批准”不代表“在整部剧本任何位置都可以使用”。

例如人物真实身份在第 40 集才揭晓，那么翻译第 5 集时不能将这个事实放进上下文。

知识和场景卡需要：Context Builder 根据当前窗口位置过滤未来信息。无法确认可见范围的剧情推断不作为强约束。

### 10.5 人物指代规则

- 明确角色标签由 Parser 识别。
- 已批准别名可精确匹配。
- “他/她/对方”等代词不做全局替换。
- 代词消歧在当前翻译窗口内结合上下文判断。
- 多个角色都可能是指代对象时，允许保留歧义或标记人工审核，不强行猜测。

## 11. SQLite 数据模型

V1 使用一个 SQLite 数据库文件。所有项目业务数据通过 `project_id` 隔离，原文版本通过 `source_document_id` 隔离。

### 11.1 核心表

#### projects

| 字段                      | 类型         | 说明             |
| ------------------------- | ------------ | ---------------- |
| id                        | TEXT PK      | 项目 ID          |
| name                      | TEXT         | 项目名称         |
| source_language           | TEXT         | 默认 zh-CN       |
| target_language           | TEXT         | 默认 en          |
| active_source_document_id | TEXT NULL FK | 当前生效原文版本 |
| style_config_json         | TEXT         | 项目级翻译规则   |
| created_at / updated_at   | DATETIME     | 时间             |

#### source_documents

| 字段                 | 类型     | 说明               |
| -------------------- | -------- | ------------------ |
| id                   | TEXT PK  | 文档版本 ID        |
| project_id           | TEXT FK  | 项目               |
| version              | INTEGER  | 项目内递增版本     |
| filename             | TEXT     | 文件名             |
| format               | TEXT     | txt / md / docx    |
| raw_path             | TEXT     | 原始文件保存路径   |
| normalized_text_path | TEXT     | 规范化文本文件路径 |
| content_hash         | TEXT     | SHA-256            |
| status               | TEXT     | active / archived  |
| imported_at          | DATETIME | 导入时间           |

重新导入内容时创建新版本，不覆盖旧文档。项目通过 `active_source_document_id` 指向当前版本。

#### chapters

| 字段                      | 类型    | 说明     |
| ------------------------- | ------- | -------- |
| id                        | TEXT PK | 章节 ID  |
| project_id                | TEXT FK | 项目     |
| source_document_id        | TEXT FK | 原文版本 |
| chapter_no                | INTEGER | 章节序号 |
| title                     | TEXT    | 标题     |
| source_start / source_end | INTEGER | 原文偏移 |

#### scenes

| 字段                      | 类型      | 说明     |
| ------------------------- | --------- | -------- |
| id                        | TEXT PK   | 场景 ID  |
| project_id                | TEXT FK   | 项目     |
| source_document_id        | TEXT FK   | 原文版本 |
| chapter_id                | TEXT FK   | 章节     |
| scene_no                  | INTEGER   | 场景序号 |
| heading                   | TEXT      | 场景标题 |
| location                  | TEXT NULL | 地点     |
| time_label                | TEXT NULL | 时间标记 |
| source_start / source_end | INTEGER   | 原文偏移 |

#### source_segments

| 字段                      | 类型         | 说明                                            |
| ------------------------- | ------------ | ----------------------------------------------- |
| id                        | TEXT PK      | 片段 ID                                         |
| project_id                | TEXT FK      | 项目                                            |
| source_document_id        | TEXT FK      | 原文版本                                        |
| chapter_id                | TEXT FK      | 章节                                            |
| scene_id                  | TEXT NULL FK | 场景                                            |
| segment_no                | INTEGER      | 当前文档版本内顺序                              |
| segment_type              | TEXT         | heading / dialogue / narrative / other          |
| speaker_name              | TEXT NULL    | 明确角色标签（仅来自格式匹配）                  |
| source_text               | TEXT         | 原文                                            |
| source_start / source_end | INTEGER      | 原文偏移                                        |
| source_hash               | TEXT         | 片段哈希                                        |
| created_at                | DATETIME     | 创建时间                                        |

片段 ID 稳定性规则：

- 仅调整场景归属或结构标签，且原文边界不变时尽量保留 ID。
- 原文内容或片段边界实质变化时创建新片段 ID。
- 旧译文不能继续伪装成新片段的当前译文。
- V1 不维护旧片段到新片段的映射表：重新导入原文即创建新 `source_documents` 版本，旧译文随旧版本归档；同版本内的结构修正直接更新片段记录。

偏移坐标统一使用**规范化文本的 Unicode 码点偏移**。前端若使用 UTF-16 索引，必须通过转换函数处理，禁止混用 UTF-8 字节、Unicode 码点和 UTF-16 code unit。

#### knowledge_items

| 字段                    | 类型         | 说明                                            |
| ----------------------- | ------------ | ----------------------------------------------- |
| id                      | TEXT PK      | 知识 ID                                         |
| project_id              | TEXT FK      | 项目                                            |
| item_type               | TEXT         | character / term / relation / rule              |
| canonical_key           | TEXT         | 规范键                                          |
| data_json               | TEXT         | 具体结构                                        |
| status                  | TEXT         | candidate / approved / auto_approved / rejected |
| visibility_scope        | TEXT         | global / from_segment / segment_range           |
| reveal_segment_id       | TEXT NULL FK | 首次揭晓位置                                    |
| source                  | TEXT         | ai / user / imported                            |
| version                 | INTEGER      | 版本                                            |
| created_at / updated_at | DATETIME     | 时间                                            |

`auto_approved` 仅 Express 模式自动批准人名与强制术语时使用，必须带证据，且可追溯创建任务与模型信息。

#### knowledge_aliases

| 字段         | 类型      | 说明                                      |
| ------------ | --------- | ----------------------------------------- |
| id           | TEXT PK   | ID                                        |
| knowledge_id | TEXT FK   | 对应知识                                  |
| alias        | TEXT      | 别名或称呼                                |
| alias_type   | TEXT      | nickname / title / variant / pronoun_hint |
| scope_json   | TEXT NULL | 适用范围                                  |
| status       | TEXT      | candidate / approved                      |

#### knowledge_evidence

| 字段          | 类型    | 说明                            |
| ------------- | ------- | ------------------------------- |
| id            | TEXT PK | ID                              |
| knowledge_id  | TEXT FK | 知识                            |
| segment_id    | TEXT FK | 原文片段                        |
| quote         | TEXT    | 证据原文                        |
| evidence_type | TEXT    | explicit / inferred / uncertain |

#### scene_context_cards

| 字段                   | 类型     | 说明                                                         |
| ---------------------- | -------- | ------------------------------------------------------------ |
| id                     | TEXT PK  | 卡片 ID                                                      |
| project_id             | TEXT FK  | 项目                                                         |
| source_document_id     | TEXT FK  | 原文版本                                                     |
| scene_id               | TEXT FK  | 场景                                                         |
| card_json              | TEXT     | 地点/时间、在场人物及状态、前情、冲突目标、情绪基调、台词意图、spoiler_guard、前场衔接 |
| evidence_json          | TEXT     | 证据引用：segment_id + 原文摘录                              |
| status                 | TEXT     | generated / human_edited                                     |
| version                | INTEGER  | 版本                                                         |
| created_at / updated_at | DATETIME | 时间                                                        |

场景卡是翻译上下文材料，不是用户正式知识：由 AI 自动生成，默认直接进入上下文；人工编辑生成新版本。

#### translations

| 字段                  | 类型     | 说明                                 |
| --------------------- | -------- | ------------------------------------ |
| id                    | TEXT PK  | 译文 ID                              |
| segment_id            | TEXT FK  | 原文片段                             |
| version               | INTEGER  | 递增版本                             |
| stage                 | TEXT     | draft / polished / final             |
| target_text           | TEXT     | 英文                                 |
| review_status         | TEXT     | unreviewed / edited / approved       |
| freshness_status      | TEXT     | current / stale                      |
| notes_json            | TEXT     | 模型备注                             |
| ambiguity_json        | TEXT     | 歧义标记                             |
| model_name            | TEXT     | 模型                                 |
| provider_id           | TEXT     | Provider                             |
| prompt_version        | TEXT     | Prompt 版本                          |
| context_snapshot_json | TEXT     | 实际使用知识 ID 与版本               |
| source_hash           | TEXT     | 对应原文哈希                         |
| created_at            | DATETIME | 时间                                 |

- 必须建立 `UNIQUE(segment_id, version)`；当前有效译文通过明确的 current 标记或事务维护的查询规则确定，不允许仅靠最大 ID 猜测。
- 两段式翻译分别以 `stage=draft` 与 `stage=polished` 入库，polished 为对外默认版本；`final` 由人工修订批准产生。
- 不再设置独立的 status 字段：审核状态由 `review_status` 表达，阶段由 `stage` 表达。

#### qa_issues

| 字段           | 类型      | 说明                                                         |
| -------------- | --------- | ------------------------------------------------------------ |
| id             | TEXT PK   | 问题 ID                                                      |
| translation_id | TEXT FK   | 对应具体译文版本                                             |
| issue_type     | TEXT      | omission / addition / pronoun_reference / negation_condition / causality_chronology / character_consistency / tone_intent / spoiler / terminology / grammar / style |
| severity       | TEXT      | low / medium / high / critical                               |
| source_quote   | TEXT      | 原文证据                                                     |
| target_quote   | TEXT NULL | 译文证据                                                     |
| explanation    | TEXT      | 问题说明                                                     |
| status         | TEXT      | open / accepted / fixed / ignored / superseded               |
| created_at     | DATETIME | 时间                                                         |

新译文版本产生后重新 QA。旧 issue 保留审计信息并标记为 superseded，不得错误地作用于新译文。

#### tasks

| 字段                    | 类型          | 说明                                                          |
| ----------------------- | ------------- | ------------------------------------------------------------- |
| id                      | TEXT PK       | 任务 ID                                                       |
| project_id              | TEXT FK       | 项目                                                          |
| task_type               | TEXT          | analyze / translate / qa                                      || status                  | TEXT          | queued / running / paused / failed / completed / needs_review |
| progress_json           | TEXT          | 总量、完成量、失败量                                          |
| config_snapshot_json    | TEXT          | 本次任务配置快照（不含密钥）                                  |
| last_checkpoint         | TEXT NULL     | 最后成功片段                                                  |
| error_message           | TEXT NULL     | 脱敏错误                                                      |
| created_at / updated_at | DATETIME      | 时间                                                          |

单机单 Worker，不维护租约、心跳和 worker_id。

#### task_items

| 字段                     | 类型         | 说明                                             |
| ------------------------ | ------------ | ------------------------------------------------ |
| id                       | TEXT PK      | 子任务 ID                                        |
| task_id                  | TEXT FK      | 任务                                             |
| segment_id               | TEXT FK NULL | 对应片段                                         |
| window_id                | TEXT NULL    | 翻译窗口                                         |
| status                   | TEXT         | pending / running / succeeded / failed / skipped |
| attempts                 | INTEGER      | 重试次数                                         |
| request_hash             | TEXT         | 请求输入哈希                                     |
| result_id                | TEXT NULL    | 结果记录                                         |
| error_code               | TEXT NULL    | 错误代码                                         |
| started_at / finished_at | DATETIME     | 时间                                             |

崩溃恢复规则：应用启动时，将 `status=running` 且 `started_at` 超时（默认 30 分钟）且无成功结果的 task_item 重置为 pending。

#### 其他必要表

- `audit_logs`：用户确认、拒绝、修改、译文批准和知识版本变更。
- `provider_configs`：Provider 非敏感配置和 `credential_ref`，不存 API Key。
- `app_settings`：应用级非敏感设置。
- `schema_migrations`：由 Alembic 管理。

### 11.2 数据约束

- 所有业务数据必须有项目隔离关系。
- 章节、场景、片段必须属于同一个 `source_document_id`。
- 原文片段不可被翻译任务修改。
- 每次译文修改都新增版本，不覆盖历史版本。
- 每次知识修改增加版本或保留审计记录。
- 只有 approved / auto_approved 且在当前剧情位置可见的知识才可作为硬约束。
- 场景卡只作为上下文材料，不作为正式设定；其中未来信息必须写入 spoiler_guard 而非正文。
- `translations` 的新鲜度与人工审核状态分开维护。
- QA issue 必须关联具体 translation 版本。
- SQLite 开启外键约束、WAL 模式和合理事务。
- 数据库迁移使用 Alembic。
- 原文和译文通过 `segment_id` 关联，禁止仅依赖段落序号。

## 12. 原文导入与解析

### 12.1 支持格式

- `.txt`
- `.md`
- `.docx`

### 12.2 导入步骤

1. 用户通过桌面文件选择器选择文件。
2. 后端检查文件类型、大小和可读性。
3. 保存原始文件副本，不修改原始文件。
4. 计算 SHA-256。
5. 生成 UTF-8 规范化文本副本。
6. 创建 `source_documents` 版本记录。
7. 输出导入报告：字符数、章节候选、场景候选、空段落、异常格式。
8. 用户确认后进入解析。解析是确定性机械切分，几秒内同步完成。

重新导入不同内容时创建新版本，不能静默覆盖原文。新版本解析完成后，由用户确认是否设为项目当前版本。

### 12.3 解析职责：机械切分，不做语义分类

**解析器只负责把原文切成带偏移的结构片段，不负责理解语义。**

明确不做：

- 不判断一段文字是旁白还是动作描述（统一归为 narrative）。
- 不推断无标签台词由谁说出（speaker 仅来自格式匹配，匹配不到留空）。
- 不判断两个名字是否同一个人（归并交给知识提取环节）。

确定性识别规则（全部可单测）：

- 章节标题：`第X集`、`第X章`、`Chapter X`、Markdown 标题层级。
- 场景标题/分隔：地点、时间、内外景标记、分隔线。
- 对白行：`角色名：`、`角色名（动作）：`、全角/半角冒号等格式。
- 括号动作、音效、字幕等明确标记。

规则约束：

1. 使用可配置规则集，不把某一种剧本格式写死。
2. 每个识别结果保存 parser rule ID 和原文偏移。
3. 不确定段落标记为 `other`，保留原文，不猜测分类。
4. 不确定的角色标签不自动创建人物。

segment_type 只保留形式分类：

- `heading`：章节/场景标题。
- `dialogue`：按格式判定为对白行，speaker_name 仅来自格式匹配。
- `narrative`：叙述段落（动作、旁白、环境描写统一归此）。
- `other`：无法套用任何格式的段落。

用户可以修正章节、场景和片段边界，也可以完全不修正直接进入翻译。

### 12.4 原文覆盖校验

将所有结构节点和片段按原文顺序映射回规范化文本，校验：

- 所有字符都被某个结构节点覆盖。
- 没有重叠区间，除非明确标记为嵌套结构。
- 所有空行、标题和无法识别段落均有明确处理。
- 片段拼接与原文可追溯。
- 任何未覆盖内容必须进入解析报告，不能静默丢弃。

### 12.5 解析报告与异常治理

解析完成后立即生成解析报告，报告包含：

- 原文字符总数、已覆盖数、未覆盖数。
- 章节、场景、对白、叙述、`other` 数量统计。
- 异常清单：格式歧义行、疑似对白归属缺失、无法识别的分隔、重叠与偏移异常。
- 每项异常给出 `source_start` / `source_end`，支持在界面局部处理。

规则：

1. 原文覆盖校验必须通过：不允许未处理的字符缺失或非法重叠，缺失即硬错误，修复前不得继续。
2. **解析结果不阻塞翻译**：异常只是提示，其余段落照常进入翻译；`other` 段按叙述文本翻译。
3. 用户可在解析界面局部修正（章节/场景边界、speaker、类型），也可跳过审核直接翻译。
4. 大量低置信度异常（如批量无标签台词）可选 AI 辅助分类：异步任务输出带证据的修正建议，用户选择接受或忽略。
5. 解析修正只改变结构化结果，不修改原始文本。
6. 解析报告记录 Parser 版本、规则集版本和解析时间。

### 12.6 性能与复杂度约束

5–8 万字中文剧本的确定性拆解是轻量任务，FastAPI 完全有能力承担：

- 算法复杂度：切分与覆盖校验必须为 O(n) 或 O(n log n)，禁止 O(n²) 实现。
- 性能目标：8 万字符的解析 + 覆盖校验在 2 秒内完成，内存占用 < 100 MB。
- 解析通过同步 API 返回，不占用后台任务队列；若未来支持更大文本再评估异步化。
- 片段批量入库使用单事务；SQLite WAL 模式下写入万级片段为秒级操作。
- 解析模块必须包含 8 万字规模的性能测试。

拆解环节的正确性风险通过"不做语义分类 + 不阻塞翻译 + 异常局部治理"消化：FastAPI 只做机械工作，语义判断交给 AI 辅助和用户局部修正，不依赖解析器对全剧本做语义理解。

## 13. 知识提取与场景卡

### 13.1 人物和术语候选

Python 按章节或场景分批发送文本。模型只返回：

- 候选人物名。
- 原文中出现的称呼。
- 候选术语。
- 候选人物关系。
- 原文引用和来源片段 ID。
- 判断类型 explicit / inferred / uncertain。

不要求模型撰写长篇人物小传，不允许补充剧情之外的背景。

Express 模式自动批准规则（auto_approved）：

1. 仅限人物规范名、别名、高频强制术语三类。
2. 必须有至少一条原文显式证据（explicit）；inferred 与 uncertain 保持候选，不生效。
3. 仅对确定性精确匹配自动归并（完全相同姓名、格式规范化后相同）；"两个名称是否同一人"在 Express 模式默认不合并，留给翻译上下文与 QA 处理。
4. 自动批准内容同样可追溯来源与模型信息，用户事后可修改（触发 stale 管理）。

### 13.2 候选归并

程序先做确定性归并：

- 完全相同姓名。
- 已批准别名精确匹配。
- Unicode、空格和标点规范化。
- 明确角色标签映射。

只有"两个名称是否同一人物"这类语义冲突才交给模型判断。模型必须提供证据；不能确认时保留多个候选，不强行合并。

### 13.3 人工审核（Guided）

Guided 模式优先展示：

- 主角和高频人物。
- 高频术语。
- 候选冲突。
- 会影响剧情理解的关系和设定。

可批量审核同一明确规则下的候选；证据不足、实体冲突或语义不明确的候选必须逐项处理。Express 模式跳过此界面，自动批准满足 13.1 规则的候选。

### 13.4 场景情境卡

每场自动生成一张场景卡，字段：

- `location` / `time_label`：地点、时间。
- `cast`：在场人物及进场时状态。
- `prior_events`：本场前情（与剧情可见性一致，不含未来信息）。
- `conflict_goal`：本场冲突或人物目标。
- `mood`：情绪基调与变化。
- `intent_notes`：关键台词意图与潜台词（AI 标注，可空、可人工加注）。
- `spoiler_guard`：本场禁止提及的后续事实（供 Context Builder 过滤）。
- `link_prev`：与前场衔接。

规则：

1. 场景卡由 AI 生成，默认直接生效进入翻译上下文，**不需要逐场人工审核**。
2. 卡片必须引用来源片段（segment_id + 原文摘录）。
3. 用户可编辑卡片（生成新版本）或关闭卡片参与上下文。
4. 场景卡是上下文材料，不是正式设定；人物、术语等强约束仍走知识库。
5. 卡片正文不得包含该场之后才揭晓的信息，此类信息只能写入 `spoiler_guard` 供过滤。

### 13.5 知识证据与字段级审核

所有 AI 提取的知识必须保留可追溯证据，不能只保存模型生成的结论。

每一条知识至少记录：

- `knowledge_id`：知识 ID。
- `item_type`：character / term / relation / rule。
- `canonical_key`：规范名称或唯一键。
- `data_json`：知识内容。
- `status`：candidate / approved / auto_approved / rejected。
- `source_segment_ids`：来源原文片段 ID。
- `evidence_quotes`：支持该知识的原文引用。
- `evidence_type`：explicit / inferred / uncertain。
- `confidence`：候选可信程度。
- `version`：知识版本。
- `reviewed_by`：确认来源，user / system。
- `reviewed_at`：审核时间。

重要规则：

1. 每个人物、别名、人物关系、身份和术语必须分别保留证据。
2. 不能因为人物姓名正确，就默认其人物关系、身份和背景也正确。
3. 原文明确出现的信息可以标记为 explicit。
4. 根据上下文推断的信息必须标记为 inferred。
5. 无法确认的信息必须标记为 uncertain。
6. inferred 和 uncertain 不得直接成为强制翻译规则；auto_approved 只允许 explicit。
7. 用户批准知识时，必须能够查看对应原文证据。
8. 证据不足时，允许用户保留候选状态，不得强迫用户确认。

人物知识需要特别区分：

- 英文规范姓名。
- 中文姓名。
- 别名。
- 社会身份。
- 人物关系。
- 不同人物对其使用的称呼。
- 不同剧情阶段使用的称呼。

不能将"人物姓名"和"人物称呼"混为一谈。

### 13.6 人物指代与身份消歧

人物指代是中文剧本翻译中的高风险环节，必须单独处理。

例如：

- 他。
- 她。
- 这个女人。
- 那个男人。
- 父亲。
- 老板。
- 陈总。
- 你以为是谁。
- 之前提到的那个人。

系统不得简单地将这些词语全局替换为某个人物的英文姓名。

处理规则：

1. 优先使用明确的角色标签和已批准人物信息。
2. 结合当前场景、前后对白、人物出场情况和已确认关系进行判断。
3. 只有存在足够证据时，才将代词关联到具体人物。
4. 如果多个角色都可能是指代对象，必须保留歧义。
5. 无法确定的指代不得强行写入人物关系或知识库。
6. 对影响剧情理解的歧义，生成 `pronoun` 或 `reference_ambiguity` QA 问题。
7. 人工确认后的指代规则可以进入知识库，但必须保留适用范围和来源证据。
8. 指代规则不能脱离场景和剧情阶段全局生效。

建议在知识结构中增加人物指代记录：

- `reference_text`：原文中的代词或称呼。
- `resolved_character_id`：确认指向的人物 ID，可为空。
- `scope_scene_id`：适用场景。
- `source_segment_id`：证据片段。
- `resolution_status`：confirmed / ambiguous / unresolved。
- `evidence_quote`：判断依据。

对于 unresolved 和 ambiguous 的情况，翻译时应尽可能保留原文歧义，不得为了追求英文流畅而擅自确定人物身份。

## 14. Context Builder 上下文组装

上下文由程序构造，模型不能自行访问知识库。

### 14.1 固定组装顺序

1. 项目级翻译规则 + 冻结的风格基线（Style Guide）。
2. 当前场景情境卡。
3. 当前窗口涉及的已批准/自动批准人物资料（含 voice 特征）。
4. 当前窗口原文精确命中的已批准术语。
5. 滚动连续性块（carry-over）：上一窗口输出的连续性状态（称呼用法、进行中动作/情绪、未尽话题）。
6. 当前窗口前后相邻原文（仅作上下文，不要求翻译）。
7. 少量相关历史翻译案例（可选）。

### 14.2 筛选与预算

- 按 speaker、scene_id、approved alias、精确术语命中筛选。
- 按剧情位置过滤尚未揭晓的知识；场景卡中的未来信息由 spoiler_guard 排除。
- 不放入全体人物档案或全书摘要。
- 不使用向量相似度。
- 每类内容设置 Token 上限。
- 超预算时先删除历史示例，再缩短相邻原文与非关键摘要；不得删除当前原文、场景卡、强制术语、风格基线和必要人物规则。
- 记录 `context_snapshot_json`，保存本次使用的知识 ID、版本、范围和上下文哈希。

### 14.3 英文连续性与 carry-over 块

相邻原文窗口不一定能保证相邻英文译文风格连续。因此每个窗口完成后，由程序从 polished 译文生成 carry-over 块：

- 当前使用的称呼（谁如何称呼谁）。
- 进行中的动作/情绪状态。
- 未尽话题、悬念。
- 正在使用的关键表达。

下一个窗口把该块作为"前情状态"输入，实现滚动连续。

规则：

1. 窗口默认串行执行（并发 1）；carry-over 依赖上一窗口结果。
2. 开启并发时必须先禁用 carry-over（放弃连续性），V1 默认不开放。
3. carry-over 块受 Token 预算限制，只保留状态要点，不携带译文原文。

### 14.4 上下文完整性检查

Context Builder 不仅负责构建上下文，还必须检查上下文是否完整、是否存在遗漏，以及是否错误地引入了不应该出现的信息。

每次翻译窗口构建上下文时，必须生成 Context Report，至少包含：

- 当前翻译窗口 ID 与涉及的人物。
- 已匹配的人物知识、未匹配或未确认的人物。
- 已匹配的强制术语。
- 已加载的场景卡与 carry-over 状态。
- 被过滤的未来剧情知识。
- 被排除的低可信度知识。
- Token 预算及实际使用量、是否发生上下文裁剪。
- 是否存在关键知识缺失、是否需要人工确认。

完整性检查规则：

1. 当前原文必须始终进入翻译 Prompt，不允许因 Token 超限被裁剪。
2. 当前窗口涉及的已批准人物知识必须经过匹配检查。
3. 当前窗口命中的强制术语必须进入上下文。
4. 未来才揭晓的信息必须被剧情可见性规则过滤。
5. 未批准知识不得作为强制事实传入模型。
6. 人物指代存在重大歧义时，必须在 Context Report 中标记。
7. 如果关键人物或术语未能匹配，必须记录原因。
8. 如果发生上下文裁剪，必须记录被裁剪内容及原因。
9. 不得将上下文不完整的情况伪装成正常完整状态。

上下文质量状态：

- `complete`：关键上下文均已成功匹配。
- `partial`：存在非关键上下文缺失。
- `needs_review`：关键人物、术语或剧情信息存在无法解决的问题。
- `failed`：上下文构建失败。

当状态为 `needs_review` 或 `failed` 时：

- Express 模式：自动尝试一次补救（如放宽匹配规则重新构建）；仍失败则跳过窗口并记入导出报告。
- Guided 模式：展示问题原因，用户可补充知识、修正规则或明确选择继续，并记录风险确认。

### 14.5 上下文快照与失效规则

每次翻译窗口必须保存实际使用的上下文快照，而不是只保存上下文生成规则。

快照至少记录：

- source_document_id。
- source_segment_ids。
- source_hash。
- knowledge_ids 及对应版本。
- scene_card_ids 及对应版本。
- carry-over 来源窗口与哈希。
- translation_rule_version。
- prompt_version。
- provider_id。
- model_id。
- context_hash。
- context_report。
- 创建时间。

当原文、知识、人物名称、强制术语、场景卡、风格基线或翻译规则发生变化时，系统必须判断哪些历史译文受到影响。

受影响的译文标记为 stale，不自动覆盖原译文。

历史译文必须能够追溯到生成时实际使用的上下文版本。

## 15. 翻译任务设计

### 15.1 工作流模式

- **Express**：导入后自动完成全部环节（知识、风格基线、翻译、QA、导出），无人工确认。
- **Guided**：在知识审核、风格确认、质量门禁处人工把关。

两种模式共用同一套翻译管线，差异仅在"确认节点"是否被自动通过。

### 15.2 试译与风格基线（自动建立，可跳过人工）

正式批量翻译前，自动选取 3–5 个代表性场景试译：

- 人物多、指代复杂。
- 情绪冲突明显。
- 有伏笔或反转。
- 动作描述密集。
- 普通叙述场景。

流程：

1. 试译场景走完整两段式管线，产出译文。
2. AI 从试译结果归纳 Style Guide（风格基线）：人物 voice 卡（用词、句式、语域、称呼习惯、情绪强度）、对白口语度、动作描述风格、文化表达策略、专名大小写。
3. Express 模式：Style Guide 自动冻结，直接作为全本翻译的全局上下文；试译场景译文直接复用为正式译文（不重复计费）。
4. Guided 模式：用户可查看并修改 Style Guide 后冻结，也可以跳过直接冻结。
5. 风格基线只增不改：批量翻译开始后基线冻结，中途修改必须暂停任务并触发受影响译文 stale 标记。

试译环节**不要求人工确认**，默认自动通过。

### 15.3 分段策略

不要简单按固定字数切割。

切分优先级：

1. 不跨章节。
2. 尽量不跨场景。
3. 不拆完整对白轮次。
4. 不拆连续动作描述。
5. 不拆句子。
6. 在上述边界内根据 Token 预算合并段落。

建议初始窗口约 800–1,500 个中文字符，最终以模型 Token 预算为准。一个窗口可以包含多个原始 segment，但输出仍按原始 `segment_id` 返回。

### 15.4 两段式翻译

每个窗口执行两轮：

**Draft 轮（忠实草稿）**：贴原文翻译，允许生硬，目标是事实锁死——人物、关系、动作主体/对象、否定、条件、因果、时序、数量与原文严格一致；不确定的指代保留歧义并写入 ambiguity 字段。

**Polish 轮（情境自然化）**：基于场景卡 + Style Guide + Draft，输出符合情境的自然英文。硬约束：不得相对 Draft 改变任何事实、信息点、情绪强度；只能改变措辞、句式、语域。

- 两轮都入库（`stage=draft` / `stage=polished`），polished 为对外默认版本。
- Polish 相对 Draft 的事实漂移是语义 QA 的最高优先级检查项。
- 两轮使用同一次窗口上下文，分别发起请求，任一失败可单独重试。

### 15.5 翻译 Prompt 核心要求

```text
You are a professional Chinese-to-English screenplay translator.

Translate only the supplied source segments.
Preserve all plot facts, actions, emotional cues, speaker intent,
negation, conditions, chronology, quantities, and scene structure.

Do not invent, infer, explain, or add facts that are not supported
by the source text and approved project context.

Use approved character names and terminology according to their
scope and context. Do not mechanically replace pronouns or forms
of address when the source context indicates a meaningful change.

Write natural, concise English suitable for screen dialogue.
Do not omit stage directions or emotional details.

If a reference is genuinely ambiguous, do not fabricate certainty.
Preserve the ambiguity where possible and report it in the ambiguity field.

Return exactly the required JSON schema. Do not include commentary.
```

Draft 轮与 Polish 轮使用同一套要求，但分工不同：Draft 允许生硬、优先保事实；Polish 禁止改变 Draft 的事实、优先自然度与情境贴合。

### 15.6 模型输出结构

```json
{
  "window_id": "WIN_0008",
  "stage": "polished",
  "translations": [
    {
      "segment_id": "SEG_00231",
      "target_text": "You have no idea what happened that night.",
      "notes": [],
      "ambiguity": []
    }
  ],
  "carry_over": {
    "forms_of_address": "Lin calls him 'Mr. Chen' to his face.",
    "ongoing_state": "Chen is furious; Lin is stalling.",
    "open_threads": "Who sent the photo remains unanswered."
  }
}
```

模型返回后：

1. Pydantic 校验 JSON。
2. 检查 segment_id 是否完整、重复或多出。
3. 检查每个原文 segment 是否有且只有一个译文。
4. 检查是否混入解释、Markdown 围栏或额外内容。
5. 检查强制术语。
6. 检查原文哈希和任务快照是否仍有效。
7. 通过后保存 draft / polished 新版本与 carry_over。
8. 校验失败最多做一次格式修复；仍失败则标记失败。

模型无权直接写数据库、修改知识或提交任务状态。

### 15.7 翻译忠实度与英文表达规则

本系统的翻译目标是：在不改变原文事实、人物意图和剧情信息的前提下，生成自然、符合英语表达习惯的英文剧本。

翻译不能简单等同于逐字直译，也不能为了英文流畅而自由改写剧情。

必须遵循以下优先级：

第一优先级：剧情事实正确。

- 不得改变人物身份。
- 不得改变人物关系。
- 不得改变事件发生顺序。
- 不得改变动作主体和动作对象。
- 不得改变否定、条件、因果和转折关系。
- 不得提前泄露原文尚未揭晓的信息。
- 不得增加原文没有的剧情事实。

第二优先级：信息完整。

- 不得遗漏对白中的重要信息。
- 不得遗漏动作和情绪描写。
- 不得遗漏人物的威胁、质问、讽刺、暗示和潜台词。
- 不得弱化或强化原文表达的情绪程度。
- 不得擅自省略重复表达，除非只是英语表达中确实需要调整的冗余。

第三优先级：人物和术语一致。

- 人物英文姓名遵循已批准规范。
- 人物关系和称呼遵循当前剧情阶段。
- 强制术语遵循已批准规则。
- 同一中文称呼在不同语境下允许使用不同英文表达。
- 不允许对代词、称呼和姓名进行无条件全局替换。

第四优先级：英文自然度。

- 对白应符合英语母语者的日常表达习惯。
- 避免明显的中文语序和生硬直译。
- 根据人物年龄、身份、性格和关系调整口吻。
- 动作描述应简洁、清楚，符合英文剧本习惯。
- 可以调整句式和语序，但不能改变原文事实。

当自然表达与原文事实发生冲突时，必须优先保留原文事实。

### 15.8 翻译模式与项目风格配置

系统提供以下两种基础翻译模式：

1. 忠实剧本翻译模式（Faithful Mode）
- 优先保证原文信息完整。
- 尽可能保留原文表达意图。
- 不主动改写文化背景和剧情设定。
- 适合需要准确还原原文的场景。
2. 海外短剧本地化模式（Localization Mode）
- 保留原文剧情事实和人物关系。
- 允许对中文俗语、口语和文化表达进行自然英文转换。
- 允许调整对白句式，使其更符合海外观众的观看习惯。
- 不允许改变剧情走向、人物动机和关键事实。
- 不允许擅自删除影响剧情的信息。

项目配置至少支持：

- target_language：English。
- english_variant：American English / British English。
- translation_mode：faithful / localization。
- dialogue_style：自然口语 / 正常对白 / 正式表达。
- action_description_style：简洁 / 标准 / 详细。
- cultural_localization：none / moderate。
- preserve_emotional_intensity：true。
- preserve_plot_facts：true。
- preserve_ambiguity：true。

以上配置必须纳入翻译任务快照和 Style Guide 生成输入，任务执行期间不得被后续设置修改而静默改变。

## 16. QA 与人工修订

### 16.1 确定性 QA

默认每段执行，不调用 AI：

- segment 覆盖完整性。
- segment 顺序。
- 空译文和重复译文。
- 原文哈希是否变化。
- 强制译名一致性。
- 角色标签和场景标题是否丢失。
- 数字、日期和专名异常。
- 是否包含非预期模型说明。

确定性 QA 只判断规则能确定的内容，不宣称能自动识别所有语义错误。

### 16.2 语义 QA

按风险触发：

- 当前场景人物较多。
- 多个可能指向不同人物的代词。
- 存在未确认人物或术语。
- 用户标记重要剧情场景。
- 确定性 QA 发现异常。
- 用户主动点击语义复核。

检查遗漏、增译、人物指代、剧情提前泄露、否定条件因果变化等。语义 QA 只生成 issue，不直接覆盖译文。

### 16.3 语义 QA 检查标准

语义 QA 的目标不是给译文打一个笼统的 AI 质量分数，而是发现原文与译文之间可以指出具体证据的问题。

语义 QA 必须同时读取：

- 当前原文 segment。

- 当前译文 segment。

- 必要的前后文。

- 当前人物和关系知识。

- 已批准术语。

- 当前 Context Snapshot。

- 当前翻译模式和风格配置。

至少检查以下内容：

1. 信息遗漏（Omission）

   - 原文有信息，译文没有表达。

   - 对白中的重要细节被省略。

   - 动作、表情、情绪描写遗漏。

2. 无依据增译（Addition / Hallucination）

   - 译文增加原文没有的事实。

   - 擅自补充人物身份、关系或动机。

   - 将推测写成确定事实。

3. 人物指代错误（Pronoun / Reference）

   - 他、她、你、那个人等指代对象错误。

   - 对话归属错误。

   - 将不同人物错误合并。

4. 否定与条件错误（Negation / Condition）

   - 肯定变成否定。

   - 否定变成肯定。

   - 条件关系发生改变。

   - 可能性被翻译成确定性。

5. 因果与时间错误（Causality / Chronology）

   - 原因和结果颠倒。

   - 事件先后顺序错误。

   - 人物行动发生时间错误。

6. 人物关系和称呼错误（Character Consistency）

   - 人物英文姓名不一致。

   - 人物称呼不符合当前关系。

   - 人物身份或社会地位表达错误。

7. 情绪和潜台词错误（Tone / Intent）

   - 威胁翻译成普通陈述。

   - 讽刺翻译成字面赞美。

   - 愤怒、恐惧、轻蔑等情绪程度明显变化。

   - 人物真实表达意图发生变化。

8. 剧情泄露（Spoiler）

   - 将后续章节才揭晓的身份、关系或事实提前写入当前译文。

9. 术语错误（Terminology）

   - 强制术语使用错误。

   - 同一术语在相同语境下译法不一致。

   - 不同概念被错误翻译为同一个英文词。

每个 QA Issue 必须包含：

- `issue_type`：问题类型。

- `severity`：low / medium / high / critical。

- `source_segment_id`：对应原文片段。

- `translation_id`：对应译文版本。

- `source_quote`：原文证据。

- `target_quote`：译文证据。

- `explanation`：问题说明。

- `suggested_fix`：建议修改方向，可为空。

- `confidence`：QA 判断可信程度。

- `status`：open / accepted / fixed / ignored / superseded。

QA 规则：

1. QA 必须提供原文和译文的具体证据，不能只说“翻译不准确”。

2. 无法确定的问题必须标记低置信度，不得伪装成确定错误。

3. QA 只能生成问题记录，不得直接覆盖译文。

4. 用户可以忽略、接受、手动修复或使用 AI 定点修复。

5. 修复后必须生成新的译文版本。

6. 新版本必须重新执行相关 QA。

7. 旧版本 QA 记录保留审计信息。

### 16.4 修复流程

```text
QA 发现问题
  ↓
创建关联当前 translation_id 的 issue
  ↓
用户查看原文 / 译文 / 证据
  ↓
用户选择忽略、手动修改或 AI 定点修复
  ↓
生成新的 translation version
  ↓
重新执行确定性 QA
  ↓
必要时重新执行语义 QA
  ↓
用户批准
```

AI 定点修复必须提供完整原文 segment、当前译文、相关人物规则和问题描述，不能只提供孤立句子。旧译文保留可回滚。

### 16.5 知识变更后的译文 stale 管理

知识、规则或原文版本变更时，根据译文 `context_snapshot_json` 找出受影响译文并标记 `freshness_status=stale`。

必须区分：

- `freshness_status`：current / stale，表示译文是否基于当前知识和原文。
- `review_status`：unreviewed / human_edited / approved，表示人工审核状态。

系统不得自动覆盖旧译文，也不得擅自全量重译。用户选择：

- 只对后续翻译生效。
- 重译指定章节或片段。
- 保留旧译文，不重译。

### 16.6 翻译质量门禁

系统不能仅以模型成功返回 JSON 或确定性 QA 通过作为翻译完成标准。

每个翻译窗口的质量状态简化为 quality_gate：

- `blocked`：结构校验失败或确定性 QA 致命错误，不得作为有效译文。
- `needs_review`：语义 QA 发现 high/critical 问题、关键人物指代不明确、存在未来剧情泄露风险。
- `passed`：其余情况。

处理规则：

1. blocked 窗口自动重试（最多 2 次），仍失败则跳过并记录，不中断全本任务。
2. needs_review 窗口自动执行一次定点修复；修复后重新执行相关 QA；仍为 needs_review 则标记并在导出报告中列出。
3. QA 通过不等于翻译绝对正确，用户仍可进行人工修订。

批量接受（Express 与 Guided 通用）：

1. 全本翻译完成后，支持"批量接受"：确定性 QA 通过 + 语义 QA 无 high/critical + 用户抽查 N%（默认 5%）→ 批量标记 approved。
2. 抽查发现问题的，对同类型风险窗口扩大抽查比例。
3. Express 模式默认执行批量接受（抽查按配置执行或跳过），并在导出报告中列出 needs_review 项。

导出时支持区分：

- 完整导出（默认，含问题报告）。
- 仅导出已批准译文。

Express 默认完整导出并在报告中标注 needs_review 项，避免将未经审核的内容误认为最终交付结果。

## 17. Worker、任务状态机与断点恢复

### 17.1 不把 BackgroundTasks 当作持久化队列

FastAPI `BackgroundTasks` 与 HTTP 应用进程绑定，不足以承担长时间翻译任务的持久化执行。V1 使用 SQLite 任务表 + 独立 Worker 循环。

### 17.2 任务状态

```text
queued → running → completed
             ├── paused
             ├── failed
             └── needs_review
```

`needs_review` 必须同时存在于数据库、Pydantic Schema、API、前端映射和测试中。

子任务状态：

- pending
- running
- succeeded
- failed
- skipped

### 17.3 Worker 机制

- 单个本地 Worker 为默认实现。
- Worker 通过 SQLite 原子更新领取 pending task_item（`UPDATE ... SET status='running', started_at=... WHERE id=(SELECT id ... LIMIT 1)` 单事务原子领取），记录 started_at。
- 窗口串行执行：默认并发 1；carry-over 依赖上一窗口结果，并发 > 1 会破坏连续性，V1 默认禁用。
- 窗口成功后在事务中写入译文（draft / polished）、carry-over、QA 基础结果、task_item 成功状态和进度。
- 任务暂停后不再领取新窗口；当前窗口完成或安全中断后进入 paused。
- 恢复规则：应用启动时，将 `status=running` 且 `started_at` 超时（默认 30 分钟）且无成功结果的 task_item 重置为 pending。

V1 不引入租约、心跳、worker_id 等分布式队列机制：单机单 Worker 场景下，用 started_at 超时即可安全回收。

### 17.4 崩溃边界

模型请求已完成，但进程在数据库提交前崩溃时，重启后可能再次调用模型，造成一次重复 API 费用。V1 接受这一有限风险，不追求复杂的 Exactly Once。

保证：

- 数据库已提交的成功结果不会被覆盖。
- 未提交窗口可以重试。
- 若服务商支持幂等请求 ID，使用稳定 request ID。
- 同一 `request_hash` 已成功的 task_item 不重复执行。

### 17.5 请求去重与配置快照

- `task_items.request_hash` 防止同一请求重复执行已成功项。
- 配置快照（`config_snapshot_json`）保证原文、知识、场景卡、风格基线、Prompt 或翻译配置变化时生成新的任务键，不覆盖旧结果。
- 不引入独立的 job_hash 概念：去重靠 request_hash，追溯靠快照。

### 17.6 并发和重试

- 默认并发 1（carry-over 串行）。
- 开启并发必须禁用 carry-over；V1 不推荐。
- 失败采用指数退避，最多自动重试 2 次。
- 格式错误可额外执行一次 JSON 修复。
- 认证错误、模型不存在和参数错误不盲目重试。
- 并发任务不得各自修改共享知识。

## 18. API 设计

统一前缀 `/api/v1`。以下为逻辑 API 清单，具体请求/响应 Schema 需在实现时同步维护 OpenAPI。

### 18.1 项目与原文

| 方法   | 路径                                         | 用途             |
| ------ | -------------------------------------------- | ---------------- |
| GET    | `/projects`                                  | 项目列表         |
| POST   | `/projects`                                  | 创建项目         |
| GET    | `/projects/{id}`                             | 项目详情         |
| PATCH  | `/projects/{id}`                             | 修改项目         |
| DELETE | `/projects/{id}`                             | 删除项目         |
| POST   | `/projects/{id}/documents/import`            | 导入原文版本     |
| GET    | `/projects/{id}/documents`                   | 文档版本列表     |
| POST   | `/projects/{id}/documents/{doc_id}/activate` | 切换当前原文版本 |
| POST   | `/projects/{id}/parse`                       | 解析（同步返回，几秒内完成） |
| GET    | `/projects/{id}/parse-report`                | 解析报告         |
| GET    | `/projects/{id}/chapters`                    | 章节列表         |
| GET    | `/projects/{id}/segments`                    | 片段列表         |

### 18.2 知识、场景卡与风格基线

| 方法  | 路径                                  | 用途                           |
| ----- | ------------------------------------- | ------------------------------ |
| GET   | `/projects/{id}/knowledge`            | 查询知识                       |
| POST  | `/projects/{id}/knowledge/extract`    | 创建候选提取任务               |
| PATCH | `/knowledge/{id}`                     | 修改、确认或拒绝               |
| GET   | `/projects/{id}/scene-cards`          | 查询场景卡                     |
| POST  | `/projects/{id}/scene-cards/generate` | 生成场景卡任务（可选，随翻译自动生成） |
| PATCH | `/scene-cards/{id}`                   | 修订场景卡                     |
| GET   | `/projects/{id}/style-guide`          | 查询风格基线                   |
| PATCH | `/projects/{id}/style-guide`          | 冻结前修改（Guided）           |

### 18.3 翻译、QA 和导出

| 方法  | 路径                               | 用途                             |
| ----- | ---------------------------------- | -------------------------------- |
| POST  | `/projects/{id}/translation/trial` | 创建试译与风格基线任务（自动）   |
| POST  | `/projects/{id}/translation/start` | 创建批量翻译任务（mode: express / guided） |
| POST  | `/projects/{id}/translation/express` | 创建一键直出任务（导入后一键触发，串联全部环节） |
| GET   | `/segments/{id}/translation`       | 获取译文版本                     |
| PATCH | `/translations/{id}`               | 人工修改并创建新版本             |
| POST  | `/translations/{id}/qa`            | 创建 QA 任务                     |
| POST  | `/qa-issues/{id}/repair`           | 定点修复                         |
| POST  | `/translations/{id}/approve`       | 批准译文                         |
| POST  | `/projects/{id}/approve/batch`     | 批量接受                         |
| POST  | `/projects/{id}/export`            | 导出（同步返回成品文件）         |

所有长任务 API 只创建任务并返回 `task_id`，不在 HTTP 请求中同步等待整个剧本处理完成（解析除外：确定性、秒级，直接同步返回）。

### 18.4 任务中心

| 方法 | 路径                 | 用途         |
| ---- | -------------------- | ------------ |
| GET  | `/tasks/{id}`        | 任务详情     |
| POST | `/tasks/{id}/pause`  | 暂停         |
| POST | `/tasks/{id}/resume` | 恢复         |
| POST | `/tasks/{id}/retry`  | 重试失败项   |
| GET  | `/tasks/{id}/events` | SSE 进度事件 |

### 18.5 模型设置

| 方法   | 路径                                  | 用途                          |
| ------ | ------------------------------------- | ----------------------------- |
| GET    | `/settings/providers`                 | Provider 配置列表（不含 Key） |
| POST   | `/settings/providers`                 | 新建 Provider 配置            |
| PATCH  | `/settings/providers/{id}`            | 修改非敏感配置                |
| PUT    | `/settings/providers/{id}/credential` | 写入或更新系统凭据            |
| DELETE | `/settings/providers/{id}/credential` | 删除系统凭据                  |
| POST   | `/settings/providers/{id}/test`       | 测试连接                      |
| GET    | `/settings/app`                       | 应用设置                      |
| PATCH  | `/settings/app`                       | 修改应用设置                  |
| GET    | `/health`                             | 本地后端健康检查              |

API Key 不得出现在响应体、错误详情、日志或任务快照中。

## 19. 项目目录结构

```text
mangatrans/
├── frontend/
│   ├── src/
│   │   ├── app/
│   │   ├── components/
│   │   ├── pages/
│   │   ├── features/
│   │   │   ├── projects/
│   │   │   ├── script-editor/
│   │   │   ├── knowledge/
│   │   │   ├── translation/
│   │   │   ├── qa/
│   │   │   ├── tasks/
│   │   │   └── settings/
│   │   ├── services/
│   │   ├── stores/
│   │   ├── hooks/
│   │   └── types/
│   ├── package.json
│   └── vite.config.ts
├── src-tauri/
│   ├── src/
│   ├── capabilities/
│   ├── binaries/                 # 构建阶段放入 sidecar，不提交临时产物
│   ├── icons/
│   └── tauri.conf.json
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── api/v1/
│   │   ├── core/
│   │   │   ├── config.py
│   │   │   ├── database.py
│   │   │   ├── paths.py
│   │   │   ├── security.py
│   │   │   └── logging.py
│   │   ├── models/
│   │   ├── schemas/
│   │   ├── repositories/
│   │   ├── services/
│   │   ├── pipeline/
│   │   │   ├── state_machine.py
│   │   │   ├── worker.py
│   │   │   ├── translation_pipeline.py
│   │   │   ├── style_guide.py
│   │   │   ├── scene_cards.py
│   │   │   ├── continuity.py
│   │   │   └── recovery.py│   │   ├── llm/
│   │   │   ├── protocol.py
│   │   │   ├── providers/
│   │   │   ├── prompts/
│   │   │   ├── response_parser.py
│   │   │   └── retry.py
│   │   ├── parsers/
│   │   ├── validators/
│   │   └── utils/
│   ├── migrations/
│   ├── tests/
│   ├── pyproject.toml
│   └── pyinstaller.spec
├── scripts/
│   ├── build_backend.ps1
│   ├── build_frontend.ps1
│   └── build_windows.ps1
├── .github/
│   └── workflows/
│       └── release-windows.yml
├── docs/
│   ├── ARCHITECTURE.md
│   ├── DATABASE.md
│   └── RELEASE.md
├── .env.example                 # 仅开发环境
├── .gitignore
├── README.md
└── LICENSE
```

`data/` 不应放在仓库中作为用户数据目录。正式运行路径由 Tauri Path API 获取，用户数据位于 Windows 用户数据目录。

## 20. Windows 打包、安装和 GitHub 发布

### 20.1 用户最终拿到什么

首发目标：

- `A-Translator-Setup-x64.exe`：NSIS 安装程序，默认下载项。
- 可选 `A-Translator-x64.msi`：后续提供。
- `checksums.txt`：SHA-256。
- Release Notes：版本变化、已知问题和升级说明。

### 20.2 构建流程

GitHub Actions 使用 Windows Runner：

1. 安装锁定版本的 Node.js、Rust、Python。
2. 安装前端依赖并构建 React。
3. 使用 PyInstaller 构建 Python sidecar。
4. 将 sidecar 放入 Tauri `externalBin` 约定目录，并确保文件名带正确 target triple。
5. 执行 Tauri build，生成 NSIS 安装程序。
6. 对安装包生成 SHA-256。
7. 上传 GitHub Release Assets。
8. 在干净 Windows 环境执行安装冒烟测试。

### 20.3 安装与升级

- V1 面向 Windows 10/11 x64。
- 用户不需要安装 Python、Node.js、Rust、Docker。
- WebView2 Runtime 使用 Tauri 支持的安装策略；首发可使用在线 bootstrapper。若以后要求完全离线安装，再评估离线 Runtime 包体积。
- 安装目录只放程序文件。
- 用户数据放 `%LOCALAPPDATA%/A-Translator/`。
- 导出默认放用户 Documents 下的 `A-Translator/Exports/`，用户可修改。
- 升级不覆盖用户数据。
- 数据库迁移前自动备份。
- 卸载默认保留用户数据；如提供清理选项，必须由用户主动确认。

### 20.4 代码签名

未配置代码签名证书时可以发布未签名安装包，但 Windows SmartScreen 可能显示未知发布者提示。面向公众长期发布时建议配置代码签名证书，并保护签名密钥。

## 21. 本地目录与数据备份

建议目录：

```text
%LOCALAPPDATA%/A-Translator/
├── app/
├── database/
│   └── app.sqlite
├── projects/
│   └── {project_id}/
│       ├── sources/
│       ├── normalized/
│       ├── cache/
│       └── attachments/
├── logs/
└── backups/
```

- API Key 保存在 Windows Credential Manager，不随数据库备份。
- 原始导入文件、规范化文本、SQLite 数据库和必要附件均需备份。
- 数据库迁移前自动备份（保持一致副本，使用 SQLite Backup API）。
- V1 提供"导出项目数据包"：将原文、规范化文本、SQLite 和附件打包为一个文件。
- 完整的手动备份/恢复界面降级到 V1.1，V1 不实现恢复向导。

## 22. 测试策略与验收标准

### 22.1 单元测试

- 文件哈希与规范化。
- Parser 规则与 8 万字规模性能测试（解析 + 覆盖校验 < 2 秒，O(n log n)）。
- 原文覆盖校验。
- Segment Planner 边界。
- 知识可见性过滤与 spoiler_guard。
- Context Builder Token 预算与组装顺序。
- 风格基线冻结与注入。
- carry-over 块生成与串行约束。
- 两段式翻译的校验规则（Polish 与 Draft 事实漂移检测的确定性部分）。
- Provider Adapter 请求与错误分类。
- JSON Schema 校验。
- 术语校验与禁止译法（黑名单）校验。
- 任务状态转换与 started_at 超时回收。
- request_hash 稳定性。
- stale 影响分析。

### 22.2 集成测试

- 导入 TXT/MD/DOCX 到解析完成。
- 知识提取、审核、快照生成。
- 单窗口翻译和译文入库。
- Worker 暂停、恢复和失败重试。
- 模型请求中断后的恢复。
- 新知识导致译文 stale。
- 新译文版本触发 QA issue superseded。
- 两段式翻译（Draft/Polish）入库与 carry-over 传递。
- Express 一键直出端到端（无人工干预）。- 导出英文和中英对照文件。

### 22.3 Windows 桌面测试

在干净 Windows 10/11 x64 虚拟机上：

- 不预装 Python、Node.js、Docker。
- 安装包可正常安装和卸载。
- 开始菜单/快捷方式可启动。
- Tauri 自动启动 Python sidecar。
- 本地 API 仅监听 127.0.0.1。
- 应用关闭后 sidecar 正常退出。
- 强制结束后重启可恢复任务。
- API Key 不出现在 SQLite、日志和导出中。
- 升级不删除项目数据。
- 卸载默认保留项目数据。

### 22.4 翻译质量测试集

必须建立独立于日常开发数据的人工标注翻译回归测试集，用于验证 Parser、Knowledge、Context Builder、Translation 和 QA 的实际质量。

测试集分为两个阶段：

第一阶段：开发冒烟测试集。

准备 20–30 个人工标注片段，用于开发早期快速验证，覆盖：

- 多人同时出现和连续代词。
- 人物不同阶段更换称呼。
- 故意隐瞒身份。
- 反讽、威胁、双关和潜台词。
- 连续动作和情绪描写。
- 跨章节伏笔。
- 人物关系变化。
- 场景切换、旁白和画外音。
- 同一术语的多语境译法。
- 中式表达的生硬直译样本（必须被 Polish 自然化，且不得改变事实）。
- Express 一键直出端到端样本（无人工干预产出可用译文）。

第二阶段：正式回归测试集。

正式发布前，逐步扩充至不少于 300 条人工标注样本。

测试集应覆盖：

1. 人物姓名、别名和身份一致性。
2. 人物代词和复杂指代。
3. 人物称呼与关系变化。
4. 多人物对话归属。
5. 反讽、威胁、双关和潜台词。
6. 否定、条件、因果和转折。
7. 时间顺序和事件逻辑。
8. 连续动作与情绪描写。
9. 场景切换、旁白和画外音。
10. 术语在不同语境下的翻译。
11. 伏笔、反转和未来剧情可见性。
12. 原文格式异常和解析歧义。
13. 长窗口和上下文预算裁剪。
14. 多章节人物和剧情连续性（carry-over 生效）。
15. 模型输出格式错误及异常恢复。
16. Draft → Polish 事实漂移。
17. 直译腔（必须自然化）与增译幻觉（必须拦截）双向样本。

每条测试样本至少包含：

- 原文。
- 必要上下文。
- 人工确认的人物和术语知识。
- 允许使用的剧情信息范围。
- 参考译文或关键语义约束。
- 必须保留的信息点。
- 不允许出现的增译信息。
- 必须自然化的直译表达（若适用）。
- 可能存在的指代歧义。
- 人工标注的重点错误类型。

测试原则：

1. 参考译文不是唯一允许的英文表达。
2. 不能因为英文措辞与参考译文不同就判定错误。
3. 重点检查剧情事实、信息完整性、人物指代、情绪意图和术语。
4. 对存在多种正确英文表达的样本，使用语义约束而不是逐字比对。
5. 测试集应保留固定版本，模型或 Prompt 更新后重新执行回归测试。
6. 记录每次测试使用的 Provider、Model、Prompt Version 和测试集版本。

### 22.5 核心验收指标

指标分为两类：自动门禁指标（可由程序测量，作为上线硬门禁）与人工评测指标（抽样人工标注，不进入硬门禁）。

**自动门禁指标**

- 原文字符覆盖率（必须 100%）。
- 解析性能（8 万字符 < 2 秒）。
- 片段偏移与哈希可追溯率。
- 强制术语漏加载次数。
- 未批准知识进入正式上下文的次数。
- 未来剧情知识被错误注入当前翻译窗口的次数。
- 译文与原文片段关联完整率。
- 已提交译文因程序异常丢失或重复提交次数。
- 任务断点恢复成功率。
- 模型异常响应处理成功率。
- QA Issue 与译文版本关联正确率。
- 确定性 QA 覆盖率。

**人工抽样评测指标（不设硬性通过阈值，作为质量趋势跟踪）**

- 关键剧情事实保持率。
- 人物指代正确率。
- 信息完整率。
- 无依据增译率。
- 否定、条件和因果关系保持率。
- 情绪和人物意图保持率。
- 剧情提前泄露次数。
- 直译腔残留率。
- Draft → Polish 事实漂移率。

上线标准：

1. 原文覆盖率必须达到 100%，且不能存在未处理的字符缺失。
2. 所有译文必须能够追溯到对应原文片段。
3. 未批准知识不得进入正式强制上下文。
4. 不得出现未来剧情知识被错误注入当前翻译窗口的情况。
5. 关键人物姓名和强制术语必须通过自动批准（带证据）或人工确认及自动检查。
6. 不得存在未处理的 high / critical 级 QA 问题而被默认标记为最终译文。
7. 已提交译文不能因程序异常而丢失或被覆盖。
8. Express 模式必须在无人工干预下完成端到端流程并产出完整英文剧本。
9. 正式回归测试集必须执行并保存测试报告。

不要使用单一 AI 质量分数作为上线标准。

## 23. 开发阶段与实施顺序

### Sprint 1：桌面骨架与原文底座

- Tauri + React 桌面项目（暗色主题视觉基线，参考 Claude Code 风格）。
- FastAPI sidecar 本地启动、健康检查、动态端口和会话令牌。
- SQLite + Alembic。
- 项目 CRUD。
- TXT/MD/DOCX 导入。
- 原文版本、哈希和 Source Ledger。
- Windows 开发启动脚本。

验收：桌面应用启动后可创建项目、导入原文，关闭重启数据不丢失。

### Sprint 2：机械切分解析与编辑器

- 确定性 Parser（heading / dialogue / narrative / other）。
- 章节/场景识别与 Segment Planner。
- 原文覆盖率检查与解析报告。
- 8 万字性能测试（< 2 秒）。
- 解析修正界面（可选使用）。
- 原文偏移和片段 ID 规则。

验收：真实剧本可解析，所有原文字符都有可追溯归属；解析异常不阻塞翻译。

### Sprint 3：知识候选与场景卡

- 人物/术语候选提取。
- 证据引用。
- 候选归并与冲突提示。
- Guided 人工批准/修改/拒绝；Express 自动批准（auto_approved）。
- 场景情境卡自动生成与 spoiler_guard。
- 剧情可见范围。

验收：未批准知识不会作为硬约束；未来才揭晓的事实不会泄露到前文；Express 模式无需人工可自动批准。

### Sprint 4：模型配置、风格基线与两段式试译

- Provider Adapter。
- Windows Credential Manager 接入。
- API Key 设置和连接测试。
- Context Builder + 场景卡 + carry-over。
- Token 预算。
- Prompt 版本管理。
- 两段式翻译管线（Draft → Polish）。
- 试译与风格基线自动生成。

验收：用户可自行配置模型，Key 不明文落库；试译自动生成 Style Guide，无需人工确认。

### Sprint 5：翻译任务与恢复

- Persistent Worker。
- 任务状态机与 started_at 超时回收。
- 单窗口两段式翻译。
- 多窗口串行队列与 carry-over。
- 译文版本管理。
- 双语对照编辑。
- stale 管理。

验收：中断后已提交窗口不重复写入，未完成窗口可恢复；carry-over 跨窗口生效。

### Sprint 6：QA、一键直出、导出与发布

- 确定性 QA。
- 按需语义 QA 与定点修复。
- quality_gate 三态与批量接受。
- QA 版本关联。
- Express 一键直出端到端。
- Markdown/TXT 剧本格式导出。
- 项目数据包导出。
- PyInstaller 打包。
- Tauri Windows 安装包。
- GitHub Actions Release。

验收：干净 Windows 环境安装后，Express 模式一次真实剧本端到端无人工干预产出完整英文剧本。

## 24. AI Coding Agent 实施约束

1. 严格按本文档 V1 范围开发，不擅自引入向量数据库、Redis、Celery 或多 Agent 框架。

2. 不允许把 Docker 作为最终用户运行前置条件。

3. 解析只做机械切分（heading / dialogue / narrative / other），不做语义分类；算法复杂度 O(n log n) 以内，8 万字解析必须 2 秒内完成。

4. 解析结果不阻塞翻译：覆盖率必须 100%，但类型/归属异常只进入报告，其余片段照常翻译。

5. 所有 LLM 输出必须经过 Pydantic Schema 校验。

6. LLM 不允许直接调用数据库写操作。

7. Guided 模式 AI 知识默认是 candidate，只有用户批准才进入正式上下文；Express 模式仅人名、别名、高频强制术语可 auto_approved，且必须带 explicit 证据与来源。

8. 原始剧本不可被覆盖；译文修改必须保留版本。

9. 所有任务必须可持久化、可暂停、可恢复；不引入租约、心跳等分布式队列机制。

10. 翻译必须两段式（Draft → Polish）：Draft 锁事实，Polish 自然化且不得改变 Draft 的事实；两轮都入库，polished 为对外默认版本。

11. 每个翻译窗口独立校验和提交；窗口串行执行并传递 carry-over，默认禁止并发。

12. 所有知识和译文必须能追溯到原文片段及 Prompt/模型/上下文版本。

13. 对无法确定的人物指代或剧情事实，系统必须允许标记不确定，不得为了填满字段而猜测。

14. 不允许用"再调用一个 AI 检查"替代可由 Python 完成的校验。

15. Python sidecar 只监听 `127.0.0.1`，使用动态端口和随机会话令牌。

16. API Key 使用 Windows Credential Manager / Credential Locker；禁止明文写入 SQLite、日志、任务快照和导出文件。开发环境使用可移植凭据后端（如 keyring），不硬编码 Windows 专属实现。

17. 用户数据必须写入系统用户数据目录，不得写入安装目录；卸载默认保留用户数据。

18. 云端模型调用前必须明确告知用户：相关原文片段和上下文将发送到其配置的模型服务商。

19. GitHub Release 构建必须固定依赖版本并提供 SHA-256；发布前在干净 Windows 环境完成安装、升级和恢复测试。

20. 任何会改变数据库结构的改动必须提供 Alembic migration。

21. 每个模块先写接口和测试，再实现业务逻辑；禁止把所有逻辑堆进 FastAPI 路由函数。

22. 试译与风格基线默认自动建立，不得强制要求人工确认；Guided 用户可选择调整。

23. 风格基线冻结后只增不改；批量翻译中途变更基线必须暂停任务并标记受影响译文 stale。

24. 场景卡自动生成、默认生效，不要求逐场人工审核；卡片正文不得含未来信息，未来信息只能进 spoiler_guard。

25. Context Builder 必须生成 Context Report，能够说明当前窗口加载了哪些人物、术语、场景卡、carry-over，以及哪些信息因剧情时间线或 Token 预算被排除。

26. 每次翻译必须保存完整上下文快照。不得只保存知识 ID 列表而无法还原实际使用的上下文内容或版本。

27. QA 必须区分确定性 QA 和语义 QA。语义 QA 必须提供原文证据、译文证据和具体问题说明，不允许只返回笼统评分。

28. 质量门禁使用 quality_gate 三态（blocked / needs_review / passed），Express 模式必须能全自动走完整个流程。

29. 必须建立翻译回归测试机制。开发早期使用 20–30 条人工标注样本，正式发布前扩充至不少于 300 条，并在 Prompt、模型或核心翻译逻辑发生变化后重新测试；样本须包含直译腔自然化与增译拦截双向用例。

30. 不得将模型请求成功、JSON 格式正确或确定性 QA 通过等同于翻译质量合格。最终译文必须经过质量门禁，关键问题必须进入人工审核（Express 模式以下线后的用户修订为兜底）。

# 附录 A：关键设计决策速查

| 问题         | V1 决策                                       |
| ------------ | --------------------------------------------- |
| 产品形态     | Windows 桌面应用                              |
| 桌面框架     | Tauri 2                                       |
| 前端         | React + TypeScript，视觉参考 Claude Code 风格 |
| 后端         | Python 3.12 + FastAPI                         |
| 后端分发     | PyInstaller sidecar                           |
| 数据库       | SQLite                                        |
| 模型         | 用户自行配置 OpenAI-compatible Provider       |
| API Key      | Windows Credential Manager（开发用可移植后端）|
| 工作流       | Express 一键直出 + Guided 人工精修，同一管线  |
| 解析         | 确定性机械切分，不做语义分类，同步返回        |
| 场景卡       | AI 自动生成，默认生效，不逐场人工审核         |
| 试译/风格基线| 自动建立，不强制人工确认                      |
| 翻译         | 两段式：Draft 锁事实 + Polish 情境自然化      |
| 连续性       | carry-over 滚动状态块，窗口串行（并发 1）     |
| 任务系统     | SQLite + Persistent Worker，无租约/心跳       |
| 向量数据库   | 不使用                                        |
| 多 Agent     | 不使用                                        |
| 默认并发     | 1                                             |
| 数据保存     | 用户本机                                      |
| 云端依赖     | 仅用户主动触发模型调用时访问 Provider         |
| 发布         | GitHub Releases Windows 安装包                |
| Docker       | 不作为正式用户运行方式                        |

# 附录 B：V1 最重要的工程原则

1. 原文是事实来源，知识库是证据驱动的索引和约束。
2. 数据库是业务状态唯一可信来源，不能依赖进程内存判断任务是否完成。
3. AI 只能提出候选和生成译文，不能直接决定流程或写入正式知识；Express 的自动批准也必须带证据与来源。
4. Context Builder 必须尊重剧情时间线，不能提前注入未来信息。
5. 每个翻译窗口独立校验、独立提交、可重试；窗口串行，carry-over 滚动传递。
6. 译文版本、知识版本和 Prompt 版本必须可追溯。
7. 两段式翻译是防直译与防幻想的双保险：Draft 锁事实，Polish 自然化，漂移即最高优先级 QA 问题。
8. 台词翻译必须带着情境：场景卡提供谁在场、之前发生什么、这句话的真实意图。
9. 解析只做机械切分，不做语义分类；解析异常不阻塞翻译。
10. 一键直出与人工精修共用同一管线，差异只在确认节点是否自动通过。
11. 用户数据与程序安装文件分离。
12. 本地 API Key 安全和本地数据安全是产品基本能力，不是后期补丁。
13. 不为架构复杂而复杂；V1 优先做可靠的单机闭环。
14. 最终价值是让 AI 在一个可控、可审计、可恢复的系统中完成长篇剧本翻译。

