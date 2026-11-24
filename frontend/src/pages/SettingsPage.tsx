/** 设置：模型参数、使用说明与软件更新（ChatGPT 风格）。 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookOpen, ChevronRight, Cpu, Eye, EyeOff, FolderOpen, Plus, RefreshCw, Trash2, X } from "lucide-react";
import { api } from "../lib/api";
import { confirmAction, toast } from "../lib/feedback";
import { APP_VERSION } from "../lib/version";
import { useAppUpdate } from "../lib/updater";
import type { ProviderInfo } from "../lib/types";

const HELP_SECTIONS: { title: string; items: string[] }[] = [
  {
    title: "第一次使用",
    items: [
      "打开「设置」→「模型服务」，填写模型、Base URL 和 API Key，保存并等待连接测试通过。",
      "回到「项目」→ 点右上角「创建」，输入项目名称；项目文件夹可选，不选则使用软件默认数据目录。",
      "进入项目后点「导入」，选择 TXT、Markdown 或 Word 剧本。导入后会自动解析，不需要再点解析。",
      "等待任务中心的解析完成后，点项目顶部的「一键直出」。系统会自动整理知识、翻译全文并做质量检查。",
      "完成后点「导出」，选择交付版或常规版，再选择文件类型和保存位置。",
    ],
  },
  {
    title: "模型服务",
    items: [
      "支持 OpenAI 兼容接口（OpenAI、DeepSeek、Moonshot、vLLM 等）和本地 Ollama 模型。",
      "OpenAI、DeepSeek 等服务选「自定义 / 其他兼容接口」；本地模型选「Ollama（本地模型）」。",
      "Base URL 通常填写到 /v1，例如 https://api.openai.com/v1；模型名按服务商提供的名称填写。",
      "API Key 只保存在本机系统凭据库，不写入数据库、日志或导出文件。",
      "保存后软件会自动测试连接。只有测试通过的模型才会出现在项目顶部，并可用于翻译。",
    ],
  },
  {
    title: "项目和剧本",
    items: [
      "支持 .txt、.md、.docx，建议使用 UTF-8 编码。",
      "导入后会自动解析章节、场景、对白和动作；解析进度与结果可在任务中心查看。",
      "原始剧本只读保存，软件不会修改导入的文件。重新导入会作为新版本保留，旧版本不会被静默覆盖。",
      "项目名称可以修改；创建时选择的项目文件夹用于导出文件的默认保存位置。",
    ],
  },
  {
    title: "一键直出和任务中心",
    items: [
      "一键直出会自动完成：人物与术语整理 → 角色档案 → 章节剧情 → 台词整理 → 全文翻译 → 自动质检与定点修正 → 自动定稿。",
      "质量检查由系统自动完成：QA 未完成、严重问题未解决或结构校验失败时，任务不会标记成功，交付版也不会放行。",
      "任务中心会实时显示当前步骤、已完成内容、进度、失败原因，以及各阶段模型请求数和 Token 用量。",
      "任务可以暂停和继续；暂停会等当前片段完成后停下，继续时从断点恢复。",
      "失败或暂停后点「重试」，系统会从失败节点继续，不会重新执行已经完成的步骤。",
      "已完成任务可以归档到「历史记录」。历史记录中的任务记录可永久删除，已生成的译文不受影响。",
    ],
  },
  {
    title: "知识库与内容修改",
    items: [
      "一键直出后，左侧「知识库」会出现该项目入口，包含角色档案、章节剧情和台词本。",
      "角色档案记录性格、说话方式和称呼；章节剧情记录重要情节与伏笔；台词本按章节、场景和角色整理对白。",
      "角色卡片可以切换主要/次要；点铅笔可在浮层中修改角色资料或章节剧情。",
      "翻译时只加载与当前章节相关的角色和剧情，帮助长剧本保持人物、称呼和故事连续。",
      "修改原文或角色档案后，受影响的译文会标记为待更新；点项目顶部的「更新翻译」只补译这些片段。",
      "修改章节剧情后，建议对相关章节执行「重新翻译」，让新的剧情信息参与翻译。",
      "在项目正文中把鼠标移到段落上，点铅笔可选择修改「原文」或「译文」。修改原文会触发相关译文待更新；修改译文会新增校对版本，不覆盖历史版本。",
      "翻译完成后顶部按钮会变为「重新翻译」：可以全部重翻，也可以只选章节重译。全部重翻不会覆盖已手动修改的知识。",
      "删除知识库会清空该项目的角色、剧情和台词本，但项目、原文和译文都会保留。",
    ],
  },
  {
    title: "导出",
    items: [
      "项目右上角「导出」会打开固定面板，先选择导出类型，再选择文件格式。",
      "交付版：按《剧本交付模板》生成，场景、动作和音效保留中文，台词及旁白/画外音/字幕等叙述内容输出英文；剧本开头已有英文标题时会自动复用，不重复翻译。",
      "常规导出：可选「中英对照」或「纯英文」，适合查看或外部复核；交付版必须通过自动质检。",
      "交付版和常规版都支持 Markdown、TXT、Word；确认后会打开系统保存窗口，可选择任意磁盘位置。",
    ],
  },
  {
    title: "数据位置和软件更新",
    items: [
      "「数据位置」显示数据库和项目数据的保存目录，可点右侧「更换」。更换后重启软件生效，已有数据不会自动迁移。",
      "「软件更新」在设置最下方，显示当前版本；点「检查更新」可检查新版本。",
      "发现新版本后点下载，运行安装包覆盖安装即可，已有项目数据会保留。",
    ],
  },
  {
    title: "常见问题",
    items: [
      "模型不可用：确认 Base URL 以 /v1 结尾、模型名存在、API Key 有效且余额充足。Ollama 需要先运行 ollama serve 并拉取对应模型。",
      "首次启动提示「未知发布者」：安装包未使用受信任商业证书时会出现，确认来源后选择继续运行。",
      "检查更新失败：通常是当前网络无法访问 GitHub，不影响已有项目和翻译。",
      "长剧本翻译时间较长：保持软件打开，在任务中心查看实时进度；可暂停后继续，不会从头重做。",
      "本地服务启动失败：启动错误区域会显示后端日志，可复制日志用于排查反馈。",
      "需要外部复核时，请先确认任务显示「已完成」且自动质量门禁通过，再导出交付稿交给 ChatGPT 检查。",
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
  const [activePanel, setActivePanel] = useState<"models" | "help" | null>(null);
  const [preset, setPreset] = useState("openai");
  const { state: updateState, check: checkUpdate, install: installUpdate } = useAppUpdate(false);
  const updateChecking = updateState.phase === "checking";
  const updateBusy = updateState.phase === "downloading" || updateState.phase === "installing";
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
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const dir = await invoke<string | null>("choose_data_dir");
      if (dir) {
        toast.success(`数据目录已设置为：${dir}（重启应用后生效）`);
        storage.refetch();
      }
    } catch (err) {
      toast.error(`更换数据目录失败：${String(err)}`);
    }
  };

  const checkForUpdates = async () => {
    try {
      const result = await checkUpdate();
      if (result.phase === "available") toast.success(`发现新版本 v${result.version ?? ""}`);
      else if (result.phase === "error") toast.error("检查更新失败，请稍后重试");
      else toast.success("已是最新版本");
    } catch (err) {
      toast.error(`检查更新失败：${String(err)}`);
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
        <div className="mx-auto max-w-3xl">
          <button
            onClick={() => setActivePanel("help")}
            className="flex w-full items-center gap-4 border-b border-(--color-border) px-2 py-4 text-left transition-colors hover:bg-(--color-panel)"
          >
            <BookOpen size={18} className="shrink-0 text-(--color-muted)" />
            <span className="min-w-0 flex-1">
              <span className="block text-sm font-medium">使用说明</span>
              <span className="mt-0.5 block text-xs text-(--color-muted)">从配置模型到导出成品</span>
            </span>
            <ChevronRight size={16} className="shrink-0 text-(--color-muted)" />
          </button>

          <button
            onClick={() => setActivePanel("models")}
            className="flex w-full items-center gap-4 border-b border-(--color-border) px-2 py-4 text-left transition-colors hover:bg-(--color-panel)"
          >
            <Cpu size={18} className="shrink-0 text-(--color-muted)" />
            <span className="min-w-0 flex-1">
              <span className="block text-sm font-medium">模型服务</span>
              <span className="mt-0.5 block text-xs text-(--color-muted)">模型、Base URL 与 API Key</span>
            </span>
            <ChevronRight size={16} className="shrink-0 text-(--color-muted)" />
          </button>

          <div className="flex w-full items-center gap-4 border-b border-(--color-border) px-2 py-4">
            <FolderOpen size={18} className="shrink-0 text-(--color-muted)" />
            <span className="min-w-0 flex-1">
              <span className="block text-sm font-medium">数据位置</span>
              <span className="mt-0.5 block text-xs text-(--color-muted)">项目与数据库保存目录</span>
            </span>
            <div className="flex min-w-0 max-w-[52%] items-center justify-end gap-2">
              <span
                className="truncate font-mono text-xs text-(--color-muted)"
                title={isTauri ? storage.data?.data_dir : "由 MT_DATA_DIR 控制"}
              >
                {isTauri ? storage.data?.data_dir ?? "读取中…" : "由 MT_DATA_DIR 控制"}
              </span>
              {isTauri && (
                <button
                  onClick={changeDataDir}
                  className="shrink-0 rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-1 text-xs hover:bg-(--color-panel2)"
                >
                  更换
                </button>
              )}
            </div>
          </div>

          <div className="flex w-full items-center gap-4 border-b border-(--color-border) px-2 py-4">
            <RefreshCw size={18} className="shrink-0 text-(--color-muted)" />
            <span className="min-w-0 flex-1">
              <span className="block text-sm font-medium">软件更新</span>
              <span className="mt-0.5 block text-xs text-(--color-muted)">检查并安装新版本</span>
            </span>
            <div className="flex shrink-0 flex-col items-end gap-1">
              <div className="flex items-center gap-2">
                {updateState.phase === "available" && (
                  <button
                    onClick={() => void installUpdate()}
                    className="rounded-lg bg-(--color-accent) px-3 py-1 text-xs font-medium text-white hover:bg-(--color-accent-hover)"
                  >
                    更新 v{updateState.version ?? ""}
                  </button>
                )}
                <span className="font-mono text-xs text-(--color-muted)">v{APP_VERSION}</span>
                <button
                  onClick={checkForUpdates}
                  disabled={updateChecking || updateBusy}
                  className="rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-1 text-xs hover:bg-(--color-panel2) disabled:opacity-40"
                >
                  {updateChecking ? "检查中…" : "检查更新"}
                </button>
              </div>
              {updateState.phase !== "idle" && updateState.phase !== "checking" && (
                <span
                  className={`text-[11px] ${
                    updateState.phase === "error"
                      ? "text-red-600"
                      : updateState.phase === "latest"
                        ? "text-emerald-600"
                        : "text-amber-600"
                  }`}
                >
                  {updateState.phase === "available"
                    ? `发现新版本 v${updateState.version ?? ""}`
                    : updateState.phase === "downloading"
                      ? `正在下载更新${updateState.progress != null ? ` ${updateState.progress}%` : "…"}`
                      : updateState.phase === "installing"
                        ? "正在安装更新…"
                        : updateState.phase === "error"
                          ? updateState.failedAction === "install"
                            ? "更新失败，请稍后重试"
                            : "检查失败，请稍后重试"
                          : "已是最新版本"}
                </span>
              )}
            </div>
          </div>
        </div>
      </div>

      {activePanel && (
        <div
          className="fixed inset-0 z-[60] flex items-center justify-center bg-black/30 p-6"
          onClick={() => setActivePanel(null)}
        >
          <div
            className="flex h-[600px] max-h-[calc(100vh-3rem)] w-full max-w-2xl flex-col rounded-2xl bg-(--color-bg) shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b border-(--color-border) px-6 py-4">
              <h3 className="text-base font-semibold">
                {activePanel === "models" ? "模型服务" : "使用说明"}
              </h3>
              <button
                onClick={() => setActivePanel(null)}
                className="rounded-lg p-1.5 text-(--color-muted) hover:bg-(--color-panel2)"
              >
                <X size={18} />
              </button>
            </div>

            <div className="flex-1 overflow-auto px-6 py-5">
              {activePanel === "models" && (
                <div>
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
                </div>
              )}
              {activePanel === "help" && (
                <div className="space-y-6">
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
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
