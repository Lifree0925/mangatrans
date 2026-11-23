/** 设置：模型参数、使用说明与软件更新（ChatGPT 风格）。 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Eye, EyeOff, Plus, Trash2, X } from "lucide-react";
import { api } from "../lib/api";
import { confirmAction, toast } from "../lib/feedback";
import { APP_VERSION, checkUpdate, type UpdateInfo } from "../lib/version";
import { openExternal } from "../lib/open";
import type { ProviderInfo } from "../lib/types";

const HELP_SECTIONS: { title: string; items: string[] }[] = [
  {
    title: "1. 快速开始（一键直出）",
    items: [
      "打开软件 → 进入「设置」→ 填写模型、Base URL、API Key → 保存。",
      "回到「项目」→ 输入项目名称 → 创建。",
      "点击项目进入工作台 → 「导入」选择剧本文件（TXT / Markdown / DOCX）。",
      "点击「解析」：几秒内完成结构切分与原文覆盖校验。",
      "点击「一键直出」：系统自动完成知识整理、角色档案、章节剧情卡、风格基线、台词本抽取、全本翻译、QA 与批量接受。",
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
    title: "5. 知识库与章节剧情",
    items: [
      "人物、别名、术语与关系从原文提取，每条知识都带原文证据。",
      "精修模式下由你确认/修改/拒绝候选；一键直出模式自动批准高置信度的人名与术语（带证据）。",
      "角色档案提供性格特征与说话方式，是润色时保持人物风格一致的依据。",
      "每章自动生成剧情卡（重要情节、伏笔、前情依赖），翻译时按需注入上下文，保证长剧本连续一致。",
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
      "显示「本地服务未就绪」：后端服务正在启动，窗口会先进入主界面并自动重试；若持续失败，请查看 logs\\sidecar.log。",
      "检查更新失败：网络受限或无法访问 GitHub 时会出现，不影响软件正常使用。",
      "模型调用失败：检查 Base URL 是否以 /v1 结尾、模型名是否存在、API Key 是否有效、余额是否充足。",
      "长剧本翻译耗时较长：属于正常现象，可暂停/恢复；建议先试译确认风格，再批量执行。",
    ],
  },
];

interface StorageInfo {
  data_dir: string;
  config_path: string;
  uses_custom_dir: boolean;
}

const PRESETS = [
  {
    id: "openai",
    label: "OpenAI",
    baseUrl: "https://api.openai.com/v1",
    model: "gpt-4o-mini",
    hint: "",
  },
  {
    id: "ollama",
    label: "Ollama（本地模型）",
    baseUrl: "http://127.0.0.1:11434/v1",
    model: "qwen2.5:7b",
    hint: "本地模型无需 API Key：先运行 ollama serve，并 ollama pull <模型名>。模型名需与 ollama list 中的一致。",
  },
  {
    id: "custom",
    label: "自定义 / 其他兼容接口",
    baseUrl: "",
    model: "",
    hint: "任何兼容 OpenAI Chat Completions 的接口（DeepSeek、Moonshot、LM Studio、vLLM 等）。",
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
  const [preset, setPreset] = useState("openai");
  const isTauri = !!(
    window as unknown as { __TAURI_INTERNALS__?: unknown }
  ).__TAURI_INTERNALS__;

  const storage = useQuery({
    queryKey: ["storage"],
    queryFn: async (): Promise<StorageInfo | null> => {
      if (!isTauri) return null;
      const { invoke } = await import("@tauri-apps/api/core");
      return await invoke<StorageInfo>("get_storage_info");
    },
    enabled: isTauri,
  });

  const changeDataDir = async () => {
    const { invoke } = await import("@tauri-apps/api/core");
    const dir = await invoke<string | null>("choose_data_dir");
    if (dir) {
      toast.success(`数据目录已设置为：${dir}（重启应用后生效）`);
      storage.refetch();
    }
  };

  const providers = useQuery({
    queryKey: ["providers"],
    queryFn: () => api<ProviderInfo[]>("/settings/providers"),
  });

  const create = useMutation({
    mutationFn: () =>
      api<{ id: string }>("/settings/providers", {
        method: "POST",
        body: JSON.stringify({
          model_name: model.trim(),
          base_url: baseUrl.trim(),
          api_key: apiKey,
          is_default: (providers.data?.length ?? 0) === 0,
        }),
      }),
    onSuccess: async (data) => {
      setModel("");
      setBaseUrl("");
      setApiKey("");
      qc.invalidateQueries({ queryKey: ["providers"] });
      // 保存后立即做连通性测试：通过才可用于翻译
      try {
        const r = await api<{ ok: boolean; detail?: string }>(
          `/settings/providers/${data.id}/test`,
          { method: "POST" }
        );
        if (r.ok) toast.success("模型连接成功，已启用");
        else toast.error(`模型不可用：${r.detail || "请检查模型名、Base URL 与 API Key"}`);
      } catch (err) {
        toast.error(`模型测试失败：${String(err)}`);
      }
      qc.invalidateQueries({ queryKey: ["providers"] });
    },
  });

  const testOne = useMutation({
    mutationFn: (pid: string) =>
      api<{ ok: boolean; detail?: string }>(`/settings/providers/${pid}/test`, {
        method: "POST",
      }),
    onSuccess: (r) => {
      if (r.ok) toast.success("连接成功，模型已启用");
      else toast.error(`模型不可用：${r.detail || "未知错误"}`);
      qc.invalidateQueries({ queryKey: ["providers"] });
    },
    onError: (err) => toast.error(`测试失败：${String(err)}`),
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
    onError: (err) => toast.error(`删除失败：${String(err)}`),
  });

  const confirmRemoveProvider = async (pid: string, modelName: string) => {
    const ok = await confirmAction("删除模型服务", `确定删除「${modelName}」及其 Key 配置？`, true);
    if (ok) remove.mutate(pid);
  };

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
                服务类型
                <select
                  value={preset}
                  onChange={(e) => {
                    const id = e.target.value;
                    setPreset(id);
                    const found = PRESETS.find((p) => p.id === id);
                    if (found) {
                      setModel(found.model);
                      setBaseUrl(found.baseUrl);
                    }
                  }}
                  className="mt-1 block w-full rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-2 text-sm outline-none focus:border-(--color-text)"
                >
                  {PRESETS.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.label}
                    </option>
                  ))}
                </select>
              </label>
              {PRESETS.find((p) => p.id === preset)?.hint && (
                <p className="text-xs text-(--color-muted)">
                  {PRESETS.find((p) => p.id === preset)?.hint}
                </p>
              )}
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
                    onClick={() => setShowKey((v) => !v)}
                    onChange={(e) => setApiKey(e.target.value)}
                    placeholder="sk-..."
                    autoComplete="off"
                    title={showKey ? "点击隐藏 Key" : "点击显示 Key"}
                    className="block w-full cursor-pointer rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-2 font-mono text-sm outline-none focus:border-(--color-text)"
                  />
                  <button
                    type="button"
                    onClick={(e) => { e.preventDefault(); e.stopPropagation(); setShowKey((v) => !v); }}
                    title={showKey ? "隐藏 Key" : "显示 Key"}
                    aria-label={showKey ? "隐藏 Key" : "显示 Key"}
                    className="rounded-lg border border-(--color-border) bg-(--color-bg) px-2.5 py-2 text-(--color-muted) hover:bg-(--color-panel2)"
                  >
                    <span className="pointer-events-none select-none block">
                      {showKey ? <EyeOff size={16} /> : <Eye size={16} />}
                    </span>
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
                    <span
                      className={`rounded-md px-1.5 py-0.5 text-xs ${
                        p.is_verified ? "bg-(--color-panel2) text-emerald-600" : "text-amber-600"
                      }`}
                    >
                      {p.is_verified ? "已验证可用" : "未验证"}
                    </span>
                    <button
                      onClick={() => testOne.mutate(p.id)}
                      disabled={testOne.isPending}
                      className="ml-auto rounded-lg border border-(--color-border) bg-(--color-bg) px-2 py-1 text-xs hover:bg-(--color-panel2) disabled:opacity-40"
                    >
                      {testOne.isPending && testOne.variables === p.id ? "测试中…" : "测试连接"}
                    </button>
                    <button
                      onClick={() => confirmRemoveProvider(p.id, p.model_name)}
                      className="rounded-lg p-1 text-(--color-muted) hover:bg-(--color-panel2) hover:text-red-600"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                  <p className="mt-0.5 font-mono text-xs text-(--color-muted)">{p.base_url}</p>
                  {!p.is_verified && p.last_test_error && (
                    <p className="mt-1 break-all text-xs text-amber-600">{p.last_test_error}</p>
                  )}
                  <div className="mt-2 flex items-center gap-2">
                    {editingKeyFor === p.id ? (
                      <>
                        <input
                          type="text"
                          value={showEditKey ? keyDraft : maskKey(keyDraft)}
                          readOnly={!showEditKey}
                          onClick={() => setShowEditKey((v) => !v)}
                          onChange={(e) => setKeyDraft(e.target.value)}
                          placeholder="输入新 Key"
                          autoComplete="off"
                          title={showEditKey ? "点击隐藏 Key" : "点击显示 Key"}
                          className="w-64 cursor-pointer rounded-lg border border-(--color-border) bg-(--color-bg) px-2 py-1 font-mono text-xs outline-none focus:border-(--color-text)"
                        />
                        <button
                          type="button"
                          onClick={(e) => { e.preventDefault(); e.stopPropagation(); setShowEditKey((v) => !v); }}
                          title={showEditKey ? "隐藏 Key（* 掩码）" : "显示 Key"}
                          aria-label={showEditKey ? "隐藏 Key（* 掩码）" : "显示 Key"}
                          className="rounded-lg border border-(--color-border) bg-(--color-bg) px-2 py-1 text-(--color-muted) hover:bg-(--color-panel2)"
                        >
                          <span className="pointer-events-none select-none block">
                            {showEditKey ? <EyeOff size={14} /> : <Eye size={14} />}
                          </span>
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
            <h2 className="text-sm font-semibold">数据位置</h2>
            <p className="mt-1 text-xs text-(--color-muted)">
              项目原文、译文、知识库与数据库的保存目录。更改后需重启应用生效，已有数据不会自动迁移。
            </p>
            <div className="mt-3 rounded-xl bg-(--color-panel) px-4 py-3">
              {isTauri ? (
                <>
                  <p className="break-all font-mono text-xs">
                    {storage.data?.data_dir ?? "读取中…"}
                  </p>
                  <div className="mt-2 flex items-center gap-2">
                    <button
                      onClick={changeDataDir}
                      className="rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-1 text-xs hover:bg-(--color-panel2)"
                    >
                      更改位置
                    </button>
                    {storage.data?.uses_custom_dir && (
                      <span className="text-xs text-(--color-muted)">使用自定义目录</span>
                    )}
                  </div>
                </>
              ) : (
                <p className="text-xs text-(--color-muted)">
                  开发模式：数据目录由 MT_DATA_DIR 环境变量控制；桌面版可在应用内选择目录。
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
            <div className="mt-3 flex flex-wrap items-center gap-3 rounded-xl bg-(--color-panel) px-4 py-3 text-sm">
              <span className="text-(--color-muted)">当前版本 v{APP_VERSION}</span>
              {updateInfo?.has_update && (updateInfo.download_url || updateInfo.installer_url) && (
                <>
                  <span className="text-xs font-medium text-amber-600">发现新版本 v{updateInfo.latest}</span>
                  <button
                    onClick={() =>
                      openExternal(updateInfo.installer_url || updateInfo.download_url || "")
                    }
                    className="rounded-lg bg-(--color-accent) px-3 py-1.5 text-xs font-medium text-white hover:bg-(--color-accent-hover)"
                  >
                    立即下载更新
                  </button>
                  <span className="text-xs text-(--color-muted)">
                    下载完成后运行安装包，按提示覆盖安装即可
                  </span>
                </>
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
            className="flex h-[80vh] max-h-[calc(100vh-3rem)] w-full max-w-2xl flex-col rounded-2xl bg-(--color-bg) shadow-xl"
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
