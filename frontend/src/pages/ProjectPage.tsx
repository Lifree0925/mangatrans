/** 项目工作台：导入、解析、章节树、片段列表、解析报告。 */

import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Download, FileUp, ScanText, ShieldCheck, Zap } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { api } from "../lib/api";
import type { Chapter, ExportResult, ParseReport, Project, ProviderInfo, QAIssueInfo, Scene, Segment, TaskInfo } from "../lib/types";

const typeColor: Record<string, string> = {
  heading: "text-(--color-accent)",
  dialogue: "text-emerald-400",
  narrative: "text-(--color-text)",
  other: "text-yellow-400",
};

export default function ProjectPage() {
  const { id = "" } = useParams();
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [showReport, setShowReport] = useState(false);
  const [showQA, setShowQA] = useState(false);
  const [expressTaskId, setExpressTaskId] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);

  const project = useQuery({
    queryKey: ["project", id],
    queryFn: () => api<Project>(`/projects/${id}`),
  });
  const providers = useQuery({
    queryKey: ["providers"],
    queryFn: () => api<ProviderInfo[]>("/settings/providers"),
  });
  const defaultProvider = providers.data?.find((p) => p.is_default) ?? providers.data?.[0];
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
  });

  const express = useMutation({
    mutationFn: () => api<{ task_id: string }>(`/projects/${id}/translation/express`, { method: "POST", body: "{}" }),
    onSuccess: (d) => setExpressTaskId(d.task_id),
  });

  const downloadExport = async (bilingual: boolean) => {
    setExporting(true);
    try {
      const r = await api<ExportResult>(`/projects/${id}/export`, {
        method: "POST",
        body: JSON.stringify({ bilingual }),
      });
      const blob = new Blob([r.content], { type: "text/markdown" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = r.filename;
      a.click();
      URL.revokeObjectURL(a.href);
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
              className="rounded border border-(--color-border) bg-(--color-panel) px-2 py-1 font-mono text-xs outline-none focus:border-(--color-accent)"
            >
              {providers.data.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.display_name || p.model_name}
                </option>
              ))}
            </select>
          </label>
        ) : (
          <Link to="/settings" className="text-xs text-(--color-accent)">
            未配置模型 → 去设置
          </Link>
        )}
        <div className="ml-auto flex items-center gap-2">
          <button
            onClick={() => fileRef.current?.click()}
            className="flex items-center gap-1.5 rounded border border-(--color-border) px-3 py-1.5 text-sm hover:border-(--color-accent)"
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
            className="flex items-center gap-1.5 rounded border border-(--color-border) px-3 py-1.5 text-sm hover:border-(--color-accent) disabled:opacity-50"
          >
            <ScanText size={15} /> {parse.isPending ? "解析中…" : "解析"}
          </button>
          <button
            onClick={() => express.mutate()}
            disabled={express.isPending}
            className="flex items-center gap-1.5 rounded bg-(--color-accent) px-3 py-1.5 text-sm text-white hover:bg-(--color-accent-hover) disabled:opacity-50"
          >
            <Zap size={15} />
            {express.isPending
              ? "启动中…"
              : expressTask?.data?.status === "running" || expressTask?.data?.status === "queued"
                ? "直出中…"
                : "一键直出"}
          </button>
          <button
            onClick={() => downloadExport(true)}
            disabled={exporting}
            className="flex items-center gap-1.5 rounded border border-(--color-border) px-3 py-1.5 text-sm hover:border-(--color-accent) disabled:opacity-50"
          >
            <Download size={15} /> 导出
          </button>
          <button
            onClick={() => setShowQA((v) => !v)}
            className="flex items-center gap-1.5 rounded border border-(--color-border) px-3 py-1.5 text-sm hover:border-(--color-accent)"
          >
            <ShieldCheck size={15} /> QA
          </button>
        </div>
      </div>

      {expressTask?.data && (
        <div className="border-b border-(--color-border) bg-(--color-panel) px-5 py-1.5 text-xs text-(--color-muted)">
          一键直出任务：{expressTask.data.status}
          {(expressTask.data.progress as { done?: number; total?: number }).total != null &&
            `（${(expressTask.data.progress as { done?: number }).done ?? 0}/${(expressTask.data.progress as { total?: number }).total ?? 0}）`}
          {expressTask.data.error_message && (
            <span className="text-red-400"> · {expressTask.data.error_message}</span>
          )}
        </div>
      )}

      <div className="flex flex-1 overflow-hidden">
        <aside className="w-56 shrink-0 overflow-auto border-r border-(--color-border) p-3 text-sm">
          {chapters.data?.map((ch) => (
            <div key={ch.id} className="mb-2">
              <p className="font-medium text-(--color-accent)">
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
          {!segs.length && (
            <p className="text-(--color-muted)">导入 TXT / MD / DOCX 剧本，然后点击解析</p>
          )}
        </div>

        {showReport && (
          <aside className="w-72 shrink-0 overflow-auto border-l border-(--color-border) p-3 text-xs">
            <div className="mb-2 flex items-center justify-between">
              <p className="font-semibold">解析报告</p>
              <button onClick={() => setShowReport(false)} className="text-(--color-muted)">×</button>
            </div>
            {report.data ? (
              <>
                <p className={report.data.coverage_ok ? "text-emerald-400" : "text-red-400"}>
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
                    <p className="text-yellow-400">异常 {report.data.anomalies.length} 项</p>
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
              <button onClick={() => setShowQA(false)} className="text-(--color-muted)">×</button>
            </div>
            {qaIssues.data?.length === 0 && <p className="text-(--color-muted)">无问题记录</p>}
            {qaIssues.data?.map((i) => (
              <div key={i.id} className="mb-2 rounded border border-(--color-border) bg-(--color-panel) p-2">
                <p className="font-medium">
                  <span className={i.severity === "high" || i.severity === "critical" ? "text-red-400" : "text-yellow-400"}>
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
    </div>
  );
}
