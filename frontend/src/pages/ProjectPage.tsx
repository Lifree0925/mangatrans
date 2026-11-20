/** 项目工作台：导入、解析、章节树、片段列表、解析报告。 */

import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Download, FileUp, ScanText, ShieldCheck, X, Zap } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { api } from "../lib/api";
import { toast } from "../lib/feedback";
import type { Chapter, ExportResult, ParseReport, Project, ProviderInfo, QAIssueInfo, Scene, Segment, TaskInfo } from "../lib/types";

const typeColor: Record<string, string> = {
  heading: "text-(--color-text)",
  dialogue: "text-emerald-600",
  narrative: "text-(--color-text)",
  other: "text-amber-600",
};

export default function ProjectPage() {
  const { id = "" } = useParams();
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [showReport, setShowReport] = useState(false);
  const [showQA, setShowQA] = useState(false);
  const [expressTaskId, setExpressTaskId] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [exportBilingual, setExportBilingual] = useState(true);
  const [exportFormat, setExportFormat] = useState<"md" | "txt" | "docx">("md");
  const isTauri = !!(
    window as unknown as { __TAURI_INTERNALS__?: unknown }
  ).__TAURI_INTERNALS__;

  const project = useQuery({
    queryKey: ["project", id],
    queryFn: () => api<Project>(`/projects/${id}`),
  });
  const providers = useQuery({
    queryKey: ["providers"],
    queryFn: () => api<ProviderInfo[]>("/settings/providers"),
  });
  const availableProviders = providers.data?.filter((p) => p.is_verified) ?? [];
  const defaultProvider = availableProviders.find((p) => p.is_default) ?? availableProviders[0];
  const setDefaultProvider = useMutation({
    mutationFn: (pid: string) =>
      api(`/settings/providers/${pid}`, {
        method: "PATCH",
        body: JSON.stringify({ is_default: true }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["providers"] }),
  });
  const chapters = useQuery({
    queryKey: ["chapters", id],
    queryFn: () => api<Chapter[]>(`/projects/${id}/chapters`),
  });
  const scenes = useQuery({
    queryKey: ["scenes", id],
    queryFn: () => api<Scene[]>(`/projects/${id}/scenes`),
  });
  const segments = useQuery({
    queryKey: ["segments", id],
    queryFn: () => api<Segment[]>(`/projects/${id}/segments`),
  });
  const report = useQuery({
    queryKey: ["parse-report", id],
    queryFn: () => api<ParseReport>(`/projects/${id}/parse-report`),
    enabled: false,
  });
  const qaIssues = useQuery({
    queryKey: ["qa-issues", id],
    queryFn: () => api<QAIssueInfo[]>(`/projects/${id}/qa-issues`),
    enabled: showQA,
  });
  const expressTask = useQuery({
    queryKey: ["task", expressTaskId],
    queryFn: () => api<TaskInfo>(`/tasks/${expressTaskId!}`),
    enabled: !!expressTaskId,
    refetchInterval: (q) => (q.state.data?.status === "running" || q.state.data?.status === "queued" ? 1500 : false),
  });

  const invalidate = () =>
    qc.invalidateQueries({ queryKey: [["chapters", id], ["scenes", id], ["segments", id], ["project", id]] });

  const upload = useMutation({
    mutationFn: async (file: File) => {
      const form = new FormData();
      form.append("file", file);
      await api(`/projects/${id}/documents/import`, { method: "POST", body: form });
    },
    onSuccess: () => invalidate(),
    onError: (err) => toast.error(`导入失败：${String(err)}`),
  });

  const express = useMutation({
    mutationFn: () => api<{ task_id: string }>(`/projects/${id}/translation/express`, { method: "POST", body: "{}" }),
    onSuccess: (d) => setExpressTaskId(d.task_id),
    onError: (err) => toast.error(`启动一键直出失败：${String(err)}`),
  });

  const runExport = async () => {
    setExporting(true);
    try {
      const r = await api<ExportResult>(`/projects/${id}/export`, {
        method: "POST",
        body: JSON.stringify({ bilingual: exportBilingual, format: exportFormat }),
      });
      if (isTauri) {
        const { invoke } = await import("@tauri-apps/api/core");
        const saved = r.content_base64
          ? await invoke<string | null>("save_file_base64", {
              defaultName: r.filename,
              contentBase64: r.content_base64,
              defaultDir: project.data?.storage_path ?? null,
            })
          : await invoke<string | null>("save_text_file", {
              defaultName: r.filename,
              content: r.content ?? "",
              defaultDir: project.data?.storage_path ?? null,
            });
        if (saved) toast.success(`已保存：${saved}`);
      } else {
        const mime =
          r.format === "docx"
            ? "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            : r.format === "txt"
              ? "text/plain"
              : "text/markdown";
        let blob: Blob;
        if (r.content_base64) {
          const bin = atob(r.content_base64);
          const bytes = new Uint8Array(bin.length);
          for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
          blob = new Blob([bytes], { type: mime });
        } else {
          blob = new Blob([r.content ?? ""], { type: mime });
        }
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = r.filename;
        a.click();
        URL.revokeObjectURL(a.href);
        toast.success("已开始下载");
      }
      setExportOpen(false);
    } catch (err) {
      toast.error(`导出失败：${String(err)}`);
    } finally {
      setExporting(false);
    }
  };

  const parse = useMutation({
    mutationFn: () => api<{ coverage_ok: boolean }>(`/projects/${id}/parse`, { method: "POST" }),
    onSuccess: () => {
      invalidate();
      report.refetch();
      setShowReport(true);
    },
    onError: (err) => toast.error(`解析失败：${String(err)}`),
  });

  const segs = segments.data ?? [];

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center gap-3 border-b border-(--color-border) px-5 py-3">
        <Link to="/" className="rounded p-1 text-(--color-muted) hover:bg-(--color-panel2)">
          <ArrowLeft size={16} />
        </Link>
        <h1 className="font-semibold">{project.data?.name ?? "…"}</h1>
        {providers.data && providers.data.length > 0 ? (
          <label className="flex items-center gap-1.5 text-xs text-(--color-muted)">
            模型
            <select
              value={defaultProvider?.id ?? ""}
              onChange={(e) => setDefaultProvider.mutate(e.target.value)}
              className="rounded-xl border border-(--color-border) bg-(--color-panel) px-2 py-1 font-mono text-xs outline-none focus:border-(--color-accent)"
            >
              {providers.data.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.display_name || p.model_name}
                </option>
              ))}
            </select>
          </label>
        ) : (
          <Link to="/settings" className="text-xs text-(--color-text)">
            未配置模型 → 去设置
          </Link>
        )}
        <div className="ml-auto flex items-center gap-2">
          {/* 1. 数据准备 */}
          <button
            onClick={() => fileRef.current?.click()}
            className="flex items-center gap-1.5 rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-1.5 text-sm hover:bg-(--color-panel2)"
            title="导入 TXT / Markdown / DOCX 剧本"
          >
            <FileUp size={15} /> 导入
          </button>
          <input
            ref={fileRef}
            type="file"
            accept=".txt,.md,.docx"
            className="hidden"
            onChange={(e) => e.target.files?.[0] && upload.mutate(e.target.files[0])}
          />
          <button
            onClick={() => parse.mutate()}
            disabled={parse.isPending}
            className="flex items-center gap-1.5 rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-1.5 text-sm hover:bg-(--color-panel2) disabled:opacity-50"
            title="切分章节/场景/对白并校验原文覆盖"
          >
            <ScanText size={15} /> {parse.isPending ? "解析中…" : "解析"}
          </button>

          {/* 2. 主操作：翻译 */}
          <span className="mx-1 h-5 w-px bg-(--color-border)" />
          <button
            onClick={() => express.mutate()}
            disabled={express.isPending}
            className="flex items-center gap-1.5 rounded-lg bg-(--color-accent) px-3 py-1.5 text-sm text-white hover:bg-(--color-accent-hover) disabled:opacity-50"
            title="自动完成整理、翻译与检查，生成完整英文剧本"
          >
            <Zap size={15} />
            {express.isPending
              ? "启动中…"
              : expressTask?.data?.status === "running" || expressTask?.data?.status === "queued"
                ? "直出中…"
                : "一键直出"}
          </button>

          {/* 3. 检查与交付 */}
          <span className="mx-1 h-5 w-px bg-(--color-border)" />
          <button
            onClick={() => setShowQA((v) => !v)}
            className="flex items-center gap-1.5 rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-1.5 text-sm hover:bg-(--color-panel2)"
            title="查看译文质量问题"
          >
            <ShieldCheck size={15} /> QA
          </button>
          <button
            onClick={() => setExportOpen(true)}
            className="flex items-center gap-1.5 rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-1.5 text-sm hover:bg-(--color-panel2)"
            title="导出英文剧本或中英对照"
          >
            <Download size={15} /> 导出
          </button>
        </div>
      </div>

      {expressTask?.data && (
        <div className="border-b border-(--color-border) bg-(--color-panel) px-5 py-1.5 text-xs text-(--color-muted)">
          一键直出任务：{expressTask.data.status}
          {(expressTask.data.progress as { done?: number; total?: number }).total != null &&
            `（${(expressTask.data.progress as { done?: number }).done ?? 0}/${(expressTask.data.progress as { total?: number }).total ?? 0}）`}
          {expressTask.data.error_message && (
            <span className="text-red-600"> · {expressTask.data.error_message}</span>
          )}
        </div>
      )}

      <div className="flex flex-1 overflow-hidden">
        <aside className="w-56 shrink-0 overflow-auto border-r border-(--color-border) p-3 text-sm">
          {chapters.data?.map((ch) => (
            <div key={ch.id} className="mb-2">
              <p className="font-medium text-(--color-text)">
                {ch.title || `第 ${ch.chapter_no} 章`}
              </p>
              {scenes.data
                ?.filter((sc) => sc.chapter_id === ch.id)
                .map((sc) => (
                  <p key={sc.id} className="ml-2 mt-1 truncate text-xs text-(--color-muted)">
                    {sc.heading || `场景 ${sc.scene_no}`}
                  </p>
                ))}
            </div>
          ))}
          {!chapters.data?.length && (
            <p className="text-xs text-(--color-muted)">导入并解析后显示章节结构</p>
          )}
        </aside>

        <div className="flex-1 overflow-auto p-4 font-mono text-[13px] leading-6">
          {segs.map((s) => (
            <div key={s.id} className="group flex gap-2 rounded px-2 hover:bg-(--color-panel)">
              <span className="w-10 shrink-0 select-none text-right text-xs leading-6 text-(--color-muted)">
                {s.segment_no}
              </span>
              <p className={`whitespace-pre-wrap ${typeColor[s.segment_type] ?? ""}`}>
                {s.source_text.replace(/\n$/, "")}
              </p>
            </div>
          ))}
        </div>

        {showReport && (
          <aside className="w-72 shrink-0 overflow-auto border-l border-(--color-border) p-3 text-xs">
            <div className="mb-2 flex items-center justify-between">
              <p className="font-semibold">解析报告</p>
              <button
                onClick={() => setShowReport(false)}
                className="rounded-lg p-1.5 text-(--color-muted) hover:bg-(--color-panel2)"
                title="关闭"
              >
                <X size={18} />
              </button>
            </div>
            {report.data ? (
              <>
                <p className={report.data.coverage_ok ? "text-emerald-600" : "text-red-600"}>
                  覆盖：{report.data.covered_chars}/{report.data.total_chars}
                  {report.data.coverage_ok ? " ✓" : " ✗"}
                </p>
                <p className="mt-1 text-(--color-muted)">
                  章节 {report.data.chapter_count} · 场景 {report.data.scene_count} · 对白{" "}
                  {report.data.dialogue_count} · 叙述 {report.data.narrative_count} · other{" "}
                  {report.data.other_count}
                </p>
                {report.data.anomalies.length > 0 && (
                  <div className="mt-2">
                    <p className="text-amber-600">异常 {report.data.anomalies.length} 项</p>
                    {report.data.anomalies.slice(0, 20).map((a, i) => (
                      <p key={i} className="mt-1 break-all text-(--color-muted)">{a}</p>
                    ))}
                  </div>
                )}
              </>
            ) : (
              <p className="text-(--color-muted)">加载中…</p>
            )}
          </aside>
        )}

        {showQA && (
          <aside className="w-80 shrink-0 overflow-auto border-l border-(--color-border) p-3 text-xs">
            <div className="mb-2 flex items-center justify-between">
              <p className="font-semibold">QA 问题</p>
              <button
                onClick={() => setShowQA(false)}
                className="rounded-lg p-1.5 text-(--color-muted) hover:bg-(--color-panel2)"
                title="关闭"
              >
                <X size={18} />
              </button>
            </div>
            {qaIssues.data?.length === 0 && <p className="text-(--color-muted)">无问题记录</p>}
            {qaIssues.data?.map((i) => (
              <div key={i.id} className="mb-2 rounded-xl border border-(--color-border) bg-(--color-panel) p-2">
                <p className="font-medium">
                  <span className={i.severity === "high" || i.severity === "critical" ? "text-red-600" : "text-amber-600"}>
                    [{i.severity}]
                  </span>{" "}
                  {i.issue_type}
                </p>
                <p className="mt-1 break-all text-(--color-muted)">{i.explanation}</p>
              </div>
            ))}
          </aside>
        )}
      </div>

      {exportOpen && (
        <div
          className="fixed inset-0 z-[65] flex items-center justify-center bg-black/30 p-6"
          onClick={() => setExportOpen(false)}
        >
          <div
            className="w-full max-w-md rounded-2xl bg-(--color-bg) p-5 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between">
              <h3 className="text-base font-semibold">导出剧本</h3>
              <button
                onClick={() => setExportOpen(false)}
                className="rounded-lg p-1.5 text-(--color-muted) hover:bg-(--color-panel2)"
              >
                <X size={18} />
              </button>
            </div>

            <p className="mt-4 text-xs text-(--color-muted)">文件类型</p>
            <div className="mt-2 grid grid-cols-3 gap-2">
              {[
                { value: "md" as const, label: "Markdown", hint: ".md" },
                { value: "txt" as const, label: "纯文本", hint: ".txt" },
                { value: "docx" as const, label: "Word 文档", hint: ".docx" },
              ].map((f) => (
                <button
                  key={f.value}
                  onClick={() => setExportFormat(f.value)}
                  className={`rounded-lg border px-2 py-2.5 text-sm ${
                    exportFormat === f.value
                      ? "border-(--color-text) bg-(--color-panel) font-medium"
                      : "border-(--color-border) hover:bg-(--color-panel)"
                  }`}
                >
                  {f.label}
                  <span className="mt-0.5 block text-[11px] font-normal text-(--color-muted)">
                    {f.hint}
                  </span>
                </button>
              ))}
            </div>

            <p className="mt-4 text-xs text-(--color-muted)">内容形式</p>
            <div className="mt-2 grid grid-cols-2 gap-2">
              <button
                onClick={() => setExportBilingual(true)}
                className={`rounded-lg border px-3 py-2.5 text-sm ${
                  exportBilingual
                    ? "border-(--color-text) bg-(--color-panel) font-medium"
                    : "border-(--color-border) hover:bg-(--color-panel)"
                }`}
              >
                中英对照
                <span className="mt-0.5 block text-[11px] font-normal text-(--color-muted)">
                  便于人工校对
                </span>
              </button>
              <button
                onClick={() => setExportBilingual(false)}
                className={`rounded-lg border px-3 py-2.5 text-sm ${
                  !exportBilingual
                    ? "border-(--color-text) bg-(--color-panel) font-medium"
                    : "border-(--color-border) hover:bg-(--color-panel)"
                }`}
              >
                纯英文
                <span className="mt-0.5 block text-[11px] font-normal text-(--color-muted)">
                  交付版本
                </span>
              </button>
            </div>

            <div className="mt-4 rounded-lg bg-(--color-panel) px-3 py-2.5 text-xs leading-5 text-(--color-muted)">
              <p>
                保存位置：
                {isTauri
                  ? "确认后将弹出系统“另存为”对话框，可选择任意磁盘位置。"
                  : "浏览器将下载到默认下载目录。"}
              </p>
              {project.data?.storage_path && (
                <p className="mt-1 break-all font-mono">项目文件夹：{project.data.storage_path}</p>
              )}
            </div>

            <div className="mt-5 flex justify-end gap-2">
              <button
                onClick={() => setExportOpen(false)}
                className="rounded-lg border border-(--color-border) bg-(--color-bg) px-4 py-1.5 text-sm hover:bg-(--color-panel2)"
              >
                取消
              </button>
              <button
                onClick={runExport}
                disabled={exporting}
                className="rounded-lg bg-(--color-accent) px-4 py-1.5 text-sm text-white hover:bg-(--color-accent-hover) disabled:opacity-50"
              >
                {exporting ? "导出中…" : "导出"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
