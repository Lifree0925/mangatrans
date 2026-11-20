/** 设置：模型参数、使用说明与软件更新（ChatGPT 风格）。 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Eye, EyeOff, Plus, Trash2, X } from "lucide-react";
import { api } from "../lib/api";
import { APP_VERSION, checkUpdate, type UpdateInfo } from "../lib/version";
import type { ProviderInfo } from "../lib/types";

const HELP_SECTIONS: { title: string; items: string[] }[] = [
  {
    title: "1. 快速开始（一键直出）",
    items: [
      "打开软件 → 进入「设置」→ 填写模型、Base URL、API Key → 保存。",
      "回到「项目」→ 输入项目名称 → 创建。",
      "点击项目进入工作台 → 「导入」选择剧本文件（TXT / Markdown / DOCX）。",
      "点击「解析」：几秒内完成结构切分与原文覆盖校验。",
      "点击「一键直出」：系统自动完成知识整理、场景卡、风格基线、全本翻译、QA 与批量接受。",
      "完成后点击「导出」，下载英文剧本或中英对照 Markdown。",
    ],
  },
  {
    title: "2. 配置模型服务",
    items: [
      "支持兼容 OpenAI Chat Completions 接口的服务商（OpenAI、DeepSeek、Moonshot、本地 vLLM 等）。",
      "模型：填写服务商提供的模型名，例如 gpt-4o-mini、deepseek-chat。",
      "Base URL：填写到 /v1 层级，例如 https://api.openai.com/v1。",
      "API Key：仅保存在本机系统凭据库（Windows Credential Manager），不会写入数据库、日志或导出文件。",
      "可在项目工作台顶部的「模型」下拉框中切换默认模型。",
    ],
  },
  {
    title: "3. 创建项目与导入原文",
    items: [
      "支持格式：.txt、.md、.docx；原始文件会保留副本，不修改源文件。",
      "每次重新导入都会创建新版本，旧版本与旧译文保留，不会静默覆盖。",
      "建议导入前确认文件编码为 UTF-8；DOCX 会按段落提取文本。",
    ],
  },
  {
    title: "4. 解析与原文覆盖",
    items: [
      "解析只做机械切分（章节标题、角色对白、叙述段落），不做语义猜测。",
      "解析报告显示字符覆盖率、章节/场景/对白数量与异常清单；覆盖率 100% 才允许继续。",
      "无法套用格式的段落会标记为 other，仍会参与翻译；解析异常不会阻塞翻译。",
      "章节/场景边界、角色标签可人工修正；修正只改变结构，不修改原文。",
    ],
  },
  {
    title: "5. 知识与场景卡",
    items: [
      "人物、别名、术语与关系从原文提取，每条知识都带原文证据。",
      "精修模式下由你确认/修改/拒绝候选；一键直出模式自动批准高置信度的人名与术语（带证据）。",
      "场景卡自动生成：地点、在场人物、前情、冲突目标、情绪基调、台词意图与伏笔保护（spoiler_guard）。",
      "场景卡用于让台词翻译贴合具体情境，无需逐场审核。",
    ],
  },
  {
    title: "6. 试译与风格基线",
    items: [
      "系统自动挑选 3–5 个代表性场景试译（人物多、情绪冲突、伏笔、动作密集、普通叙述）。",
      "从试译结果归纳风格基线：人物口吻、对白语域、动作描述风格、文化表达策略。",
      "风格基线冻结后作为全本翻译的全局约束，保证跨章节风格一致；试译译文会直接复用为正式译文。",
    ],
  },
  {
    title: "7. 批量翻译与恢复",
    items: [
      "两段式翻译：Draft 忠实草稿锁死事实 → Polish 基于情境自然化，禁止改变事实。",
      "窗口按章节/场景边界切分，串行执行并通过 carry-over 传递前文状态，保证连续一致。",
      "任务可暂停、恢复；中断后已提交窗口不会重复，未完成窗口自动回收重试。",
      "每段译文都有版本记录，可回溯生成时的模型、Prompt 与上下文快照。",
    ],
  },
  {
    title: "8. QA 与人工修订",
    items: [
      "确定性 QA：空译文、覆盖完整性、强制术语、格式异常等自动检查。",
      "语义 QA：对高风险窗口检查遗漏、增译、指代、否定条件、因果时序、情绪意图与剧情泄露。",
      "每个问题都带原文/译文证据；可手动修订，修订会生成新译文版本。",
      "修改人物或术语后，受影响译文会被标记为 stale（过期），不会自动覆盖。",
    ],
  },
  {
    title: "9. 导出",
    items: [
      "工作台右上角「导出」：默认导出中英对照 Markdown，便于校对。",
      "导出报告包含段落数、未翻译数、QA 问题统计。",
      "正式交付建议先完成 QA 审核或批量接受，再导出。",
    ],
  },
  {
    title: "10. 数据、日志与备份",
    items: [
      "项目数据保存在 %LOCALAPPDATA%\\com.mangatrans.desktop（卸载默认保留）。",
      "运行日志：%LOCALAPPDATA%\\com.mangatrans.desktop\\logs\\sidecar.log。",
      "覆盖安装或升级不会删除项目数据；数据库升级前会自动备份。",
      "设置环境变量 MT_DATA_DIR 可让程序使用自定义数据目录（便携测试用）。",
    ],
  },
  {
    title: "11. 常见问题",
    items: [
      "首次运行提示「未知发布者」：安装包未购买受信任证书时会出现，点击「仍要运行」即可。",
      "显示「本地服务未就绪」：首次启动需要几秒初始化；若持续失败，请查看 logs\\sidecar.log。",
      "检查更新失败：网络受限或无法访问 GitHub 时会出现，不影响软件正常使用。",
      "模型调用失败：检查 Base URL 是否以 /v1 结尾、模型名是否存在、API Key 是否有效、余额是否充足。",
      "长剧本翻译耗时较长：属于正常现象，可暂停/恢复；建议先试译确认风格，再批量执行。",
    ],
  },
];

function maskKey(key: string): string {
  if (!key) return "";
  if (key.length <= 8) return "*".repeat(key.length);
  return key.slice(0, 4) + "*".repeat(Math.max(4, key.length - 8)) + key.slice(-4);
}

export default function SettingsPage() {
  const qc = useQueryClient();
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [keyDraft, setKeyDraft] = useState("");
  const [editingKeyFor, setEditingKeyFor] = useState<string | null>(null);
  const [showEditKey, setShowEditKey] = useState(false);
  const [updateInfo, setUpdateInfo] = useState<UpdateInfo | null>(null);
  const [showHelp, setShowHelp] = useState(false);

  const providers = useQuery({
    queryKey: ["providers"],
    queryFn: () => api<ProviderInfo[]>("/settings/providers"),
  });

  const create = useMutation({
    mutationFn: () =>
      api("/settings/providers", {
        method: "POST",
        body: JSON.stringify({
          model_name: model.trim(),
          base_url: baseUrl.trim(),
          api_key: apiKey,
          is_default: (providers.data?.length ?? 0) === 0,
        }),
      }),
    onSuccess: () => {
      setModel("");
      setBaseUrl("");
      setApiKey("");
      qc.invalidateQueries({ queryKey: ["providers"] });
    },
  });

  const saveKey = useMutation({
    mutationFn: (pid: string) =>
      api(`/settings/providers/${pid}`, {
        method: "PATCH",
        body: JSON.stringify({ api_key: keyDraft }),
      }),
    onSuccess: () => {
      setKeyDraft("");
      setEditingKeyFor(null);
      qc.invalidateQueries({ queryKey: ["providers"] });
    },
  });

  const remove = useMutation({
    mutationFn: (pid: string) => api<void>(`/settings/providers/${pid}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["providers"] }),
  });

  const canCreate = model.trim() && baseUrl.trim();

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-(--color-border) px-6 py-4">
        <h1 className="text-lg font-semibold">设置</h1>
      </div>

      <div className="flex-1 overflow-auto p-6">
        <div className="mx-auto max-w-3xl space-y-10">
          <section>
            <h2 className="text-sm font-semibold">模型服务</h2>
            <p className="mt-1 text-xs text-(--color-muted)">
              API Key 仅保存在本机系统凭据库，不写入数据库、日志或导出文件。
            </p>

            <div className="mt-4 space-y-3 rounded-xl bg-(--color-panel) p-4">
              <label className="block text-xs text-(--color-muted)">
                模型
                <input
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  placeholder="gpt-4o-mini"
                  className="mt-1 block w-full rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-2 text-sm outline-none focus:border-(--color-text)"
                />
              </label>
              <label className="block text-xs text-(--color-muted)">
                Base URL
                <input
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                  placeholder="https://api.openai.com/v1"
                  className="mt-1 block w-full rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-2 font-mono text-sm outline-none focus:border-(--color-text)"
                />
              </label>
              <label className="block text-xs text-(--color-muted)">
                API Key
                <div className="mt-1 flex gap-2">
                  <input
                    type="text"
                    value={showKey ? apiKey : maskKey(apiKey)}
                    readOnly={!showKey}
                    onClick={() => setShowKey(true)}
                    onChange={(e) => setApiKey(e.target.value)}
                    placeholder="sk-..."
                    autoComplete="off"
                    className="block w-full rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-2 font-mono text-sm outline-none focus:border-(--color-text)"
                  />
                  <button
                    type="button"
                    onClick={() => setShowKey((v) => !v)}
                    title={showKey ? "隐藏 Key" : "显示 Key"}
                    className="rounded-lg border border-(--color-border) bg-(--color-bg) px-2.5 text-(--color-muted) hover:bg-(--color-panel2)"
                  >
                    {showKey ? <EyeOff size={16} /> : <Eye size={16} />}
                  </button>
                </div>
              </label>
              <button
                onClick={() => create.mutate()}
                disabled={!canCreate || create.isPending}
                className="flex items-center gap-1.5 rounded-lg bg-(--color-accent) px-3 py-1.5 text-sm text-white hover:bg-(--color-accent-hover) disabled:opacity-40"
              >
                <Plus size={15} /> 保存模型服务
              </button>
            </div>

            <div className="mt-3 space-y-2">
              {providers.data?.map((p) => (
                <div key={p.id} className="rounded-xl bg-(--color-panel) px-4 py-3">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium">{p.model_name}</span>
                    {p.is_default && (
                      <span className="rounded-md bg-(--color-panel2) px-1.5 py-0.5 text-xs">默认</span>
                    )}
                    {p.has_credential && (
                      <span className="rounded-md bg-(--color-panel2) px-1.5 py-0.5 text-xs text-emerald-600">
                        已配置 Key
                      </span>
                    )}
                    <button
                      onClick={() => remove.mutate(p.id)}
                      className="ml-auto rounded-lg p-1 text-(--color-muted) hover:bg-(--color-panel2) hover:text-red-600"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                  <p className="mt-0.5 font-mono text-xs text-(--color-muted)">{p.base_url}</p>
                  <div className="mt-2 flex items-center gap-2">
                    {editingKeyFor === p.id ? (
                      <>
                        <input
                          type="text"
                          value={showEditKey ? keyDraft : maskKey(keyDraft)}
                          readOnly={!showEditKey}
                          onClick={() => setShowEditKey(true)}
                          onChange={(e) => setKeyDraft(e.target.value)}
                          placeholder="输入新 Key"
                          autoComplete="off"
                          className="w-64 rounded-lg border border-(--color-border) bg-(--color-bg) px-2 py-1 font-mono text-xs outline-none focus:border-(--color-text)"
                        />
                        <button
                          type="button"
                          onClick={() => setShowEditKey((v) => !v)}
                          title={showEditKey ? "隐藏 Key（* 掩码）" : "显示 Key"}
                          className="rounded-lg border border-(--color-border) bg-(--color-bg) px-2 py-1 text-(--color-muted) hover:bg-(--color-panel2)"
                        >
                          {showEditKey ? <EyeOff size={14} /> : <Eye size={14} />}
                        </button>
                        <button
                          onClick={() => saveKey.mutate(p.id)}
                          disabled={!keyDraft.trim()}
                          className="rounded-lg bg-(--color-accent) px-2 py-1 text-xs text-white hover:bg-(--color-accent-hover) disabled:opacity-40"
                        >
                          保存
                        </button>
                        <button
                          onClick={() => {
                            setEditingKeyFor(null);
                            setKeyDraft("");
                          }}
                          className="rounded-lg border border-(--color-border) bg-(--color-bg) px-2 py-1 text-xs hover:bg-(--color-panel2)"
                        >
                          取消
                        </button>
                      </>
                    ) : (
                      <>
                        <span className="font-mono text-xs text-(--color-muted)">
                          {p.masked_key ?? "未配置 Key"}
                        </span>
                        <button
                          onClick={() => {
                            setEditingKeyFor(p.id);
                            setKeyDraft("");
                            setShowEditKey(true);
                          }}
                          className="rounded-lg border border-(--color-border) bg-(--color-bg) px-2 py-1 text-xs hover:bg-(--color-panel2)"
                        >
                          修改 Key
                        </button>
                      </>
                    )}
                  </div>
                </div>
              ))}
              {providers.data?.length === 0 && (
                <p className="text-xs text-(--color-muted)">
                  尚未配置模型服务。一键直出与真实翻译需要至少一个模型服务商。
                </p>
              )}
            </div>
          </section>

          <section>
            <h2 className="text-sm font-semibold">使用说明</h2>
            <div className="mt-3 flex items-center gap-3 rounded-xl bg-(--color-panel) px-4 py-3 text-sm">
              <span className="text-(--color-muted)">查看完整的功能说明与操作步骤。</span>
              <button
                onClick={() => setShowHelp(true)}
                className="ml-auto rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-1 text-xs hover:bg-(--color-panel2)"
              >
                打开使用说明
              </button>
            </div>
          </section>

          <section>
            <h2 className="text-sm font-semibold">软件更新</h2>
            <div className="mt-3 flex items-center gap-3 rounded-xl bg-(--color-panel) px-4 py-3 text-sm">
              <span className="text-(--color-muted)">当前版本 v{APP_VERSION}</span>
              {updateInfo?.has_update && updateInfo.download_url && (
                <a href={updateInfo.download_url} target="_blank" rel="noreferrer" className="text-xs underline">
                  发现新版本 v{updateInfo.latest}，去下载 →
                </a>
              )}
              {updateInfo && !updateInfo.has_update && (
                <span className="text-xs text-emerald-600">
                  {updateInfo.error ? "暂时无法检查更新（网络受限）" : "已是最新版本"}
                </span>
              )}
              <button
                onClick={async () => setUpdateInfo(await checkUpdate(true))}
                className="ml-auto rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-1 text-xs hover:bg-(--color-panel2)"
              >
                检查更新
              </button>
            </div>
          </section>
        </div>
      </div>

      {showHelp && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-6"
          onClick={() => setShowHelp(false)}
        >
          <div
            className="flex max-h-[85vh] w-full max-w-2xl flex-col rounded-2xl bg-(--color-bg) shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b border-(--color-border) px-5 py-4">
              <h3 className="text-base font-semibold">MangaTrans 使用说明</h3>
              <button
                onClick={() => setShowHelp(false)}
                className="rounded-lg p-1.5 text-(--color-muted) hover:bg-(--color-panel2)"
              >
                <X size={18} />
              </button>
            </div>
            <div className="flex-1 space-y-6 overflow-auto px-5 py-5">
              {HELP_SECTIONS.map((section) => (
                <div key={section.title}>
                  <h4 className="text-sm font-semibold">{section.title}</h4>
                  <ul className="mt-2 space-y-1.5">
                    {section.items.map((item, i) => (
                      <li key={i} className="flex gap-2 text-xs leading-5 text-(--color-muted)">
                        <span className="mt-2 h-1 w-1 shrink-0 rounded-full bg-(--color-muted)" />
                        <span>{item}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
