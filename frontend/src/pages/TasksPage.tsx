/** 任务中心：任务步骤明细、进度与错误（中文）。 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Archive, CheckCircle2, Circle, History, Loader2, RotateCcw, Trash2, Undo2, X, XCircle } from "lucide-react";
import { api } from "../lib/api";
import { toast } from "../lib/feedback";
import type { TaskInfo } from "../lib/types";

const statusColor: Record<string, string> = {
  queued: "text-(--color-muted)",
  running: "text-(--color-text)",
  completed: "text-emerald-600",
  failed: "text-red-600",
  paused: "text-amber-600",
};

const taskTypeLabel: Record<string, string> = {
  express: "一键直出",
  translate: "全文翻译",
  trial: "试译",
  knowledge: "知识提取",
  parse: "解析",
  dialogue: "台词本抽取",
  scene_cards: "场景卡",
  qa: "质量检查",
  analyze: "分析",
};
const statusLabel: Record<string, string> = {
  queued: "排队中",
  running: "执行中",
  completed: "已完成",
  failed: "失败",
  paused: "已暂停",
  cancelled: "已取消",
};

/** Express 流水线固定步骤（顺序展示）。 */
const EXPRESS_STEPS = [
  { key: "knowledge", label: "知识提取（人物/术语）" },
  { key: "profiles", label: "角色档案" },
  { key: "storylines", label: "章节剧情卡" },
  { key: "dialogue", label: "台词本抽取" },
  { key: "scene_cards", label: "场景卡生成" },
  { key: "trial", label: "试译与风格基线" },
  { key: "translation", label: "全文翻译" },
  { key: "qa", label: "质量检查" },
  { key: "approve", label: "批准定稿" },
];

function fmtSecs(secs?: number): string {
  if (secs == null) return "";
  if (secs < 60) return `${Math.round(secs)} 秒`;
  const m = Math.floor(secs / 60);
  const r = Math.round(secs % 60);
  return r ? `${m} 分 ${r} 秒` : `${m} 分钟`;
}

function stepDetail(key: string, step: Record<string, unknown> | undefined): string {
  if (!step) return "";
  switch (key) {
    case "knowledge": {
      const s = step as { batches?: number; added?: number; failed_batches?: number; elapsed_s?: number };
      const parts: string[] = [];
      if (s.batches != null) parts.push(`共 ${s.batches} 批`);
      if (s.added != null) parts.push(s.added ? `新提取 ${s.added} 条` : "无新条目（已存在则去重）");
      if (s.failed_batches) parts.push(`失败 ${s.failed_batches} 批（已跳过，不阻塞）`);
      if (s.elapsed_s) parts.push(`耗时 ${fmtSecs(s.elapsed_s)}`);
      return parts.join(" · ");
    }
    case "scene_cards": {
      const s = step as { generated?: number; failed?: number; note?: string };
      if (s.note) return `已生成 ${s.generated ?? 0} 场 · ${s.note}`;
      return s.generated != null ? `已生成 ${s.generated} 场${s.failed ? `（失败 ${s.failed} 场）` : ""}` : "";
    }
    case "trial": {
      const s = step as { windows?: number; style_guide_id?: string };
      return s.windows != null ? `已试译 ${s.windows} 个窗口${s.style_guide_id ? " · 风格基线已建立" : ""}` : "";
    }
    case "translation": {
      const s = step as { done?: number; total?: number };
      if (s.total == null) return "";
      const pct = s.total ? Math.round(((s.done ?? 0) / s.total) * 100) : 0;
      return `已翻译 ${s.done ?? 0} / ${s.total} 个窗口（${pct}%）`;
    }
    case "qa": {
      const s = step as { done?: number; total?: number; issues?: number; needs_review?: number; blocked?: number; severity_counts?: Record<string, number> };
      const base = s.total != null ? `已检查 ${s.done ?? 0} / ${s.total} 个窗口` : "";
      const sc = s.severity_counts ?? {};
      const sevParts = [
        sc.critical ? `严重 ${sc.critical}` : "",
        sc.high ? `高 ${sc.high}` : "",
        sc.medium ? `中 ${sc.medium}` : "",
        sc.low ? `低 ${sc.low}` : "",
      ].filter(Boolean);
      const extra = [
        s.issues ? `发现问题 ${s.issues} 条` : "未发现问题",
        sevParts.length ? sevParts.join(" / ") : "",
        s.needs_review ? `待复核 ${s.needs_review} 窗` : "",
        s.blocked ? `拦截 ${s.blocked} 窗` : "",
      ].filter(Boolean).join(" · ");
      return [base, extra].filter(Boolean).join(" · ");
    }
    case "approve": {
      const s = step as { approved?: number };
      return s.approved != null ? `已批准 ${s.approved} 条译文` : "";
    }
  }
  return "";
}

function ExpressSteps({ task }: { task: TaskInfo }) {
  const progress = task.progress as {
    steps?: Record<string, Record<string, unknown>>;
    current_step?: string;
  };
  const steps = progress.steps ?? {};
  // 单步完成判定：translation/qa 有进度字段，须 done>=total 才算完成；其余步骤写入即完成
  const stepDone = (key: string): boolean => {
    const step = steps[key];
    if (step === undefined) return false;
    if (key === "translation" || key === "qa") {
      const s = step as { done?: number; total?: number };
      return s.total != null && (s.done ?? 0) >= s.total;
    }
    return true;
  };
  const current = progress.current_step ?? (task.status === "running" ? EXPRESS_STEPS[0].key : "");
  const currentIndex = EXPRESS_STEPS.findIndex((s) => s.key === current);
  // 进行中步骤：运行状态下第一个未完成步骤
  const firstPending = EXPRESS_STEPS.findIndex((s) => !stepDone(s.key));
  return (
    <div className="mt-3 space-y-1.5">
      {EXPRESS_STEPS.map((s, i) => {
        const step = steps[s.key];
        const done = stepDone(s.key) || task.status === "completed";
        const running =
          task.status === "running" &&
          !done &&
          (currentIndex >= 0 ? i === currentIndex : i === (firstPending >= 0 ? firstPending : i));
        const failedHere =
          task.status === "failed" &&
          !done &&
          i === (firstPending >= 0 ? firstPending : currentIndex >= 0 ? currentIndex : 0);
        return (
          <div key={s.key} className="flex items-start gap-2 text-xs">
            <span className="mt-0.5 shrink-0">
              {failedHere ? (
                <XCircle size={13} className="text-red-600" />
              ) : done ? (
                <CheckCircle2 size={13} className="text-emerald-600" />
              ) : running ? (
                <Loader2 size={13} className="animate-spin text-emerald-600" />
              ) : (
                <Circle size={13} className="text-(--color-border)" />
              )}
            </span>
            <div className="min-w-0">
              <span className={done ? "text-(--color-text)" : failedHere ? "font-medium text-red-600" : "text-(--color-muted)"}>
                {s.label}
              </span>
              <span className="ml-2 text-(--color-muted)">{stepDetail(s.key, step)}</span>
              {running && (
                <span className="ml-2 font-medium text-(--color-accent)">执行中…</span>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

export default function TasksPage() {
  const qc = useQueryClient();
  const [showHistory, setShowHistory] = useState(false);

  const tasks = useQuery({
    queryKey: ["tasks"],
    queryFn: () => api<TaskInfo[]>("/tasks"),
    refetchInterval: 2000,
  });

  const removeTask = useMutation({
    mutationFn: (taskId: string) => api(`/tasks/${taskId}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks"] });
      toast.success("任务记录已删除");
    },
    onError: (err) => toast.error(`删除失败：${String(err)}`),
  });

  const archivedTasks = useQuery({
    queryKey: ["tasks", "archived"],
    queryFn: () => api<TaskInfo[]>("/tasks?archived=true"),
    enabled: showHistory,
    refetchInterval: 3000,
  });

  const archiveTask = useMutation({
    mutationFn: ({ taskId, archived }: { taskId: string; archived: boolean }) =>
      api(`/tasks/${taskId}/archive`, { method: "POST", body: JSON.stringify({ archived }) }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["tasks", "archived"] });
      toast.success("已归档，可在历史记录中查看");
    },
    onError: (err) => toast.error(`归档失败：${String(err)}`),
  });

  const retry = useMutation({
    mutationFn: (taskId: string) => api(`/tasks/${taskId}/retry`, { method: "POST" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks"] });
      toast.success("已重新调度该任务");
    },
    onError: (err) => toast.error(`重试失败：${String(err)}`),
  });

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center border-b border-(--color-border) px-6 py-4">
        <h1 className="text-lg font-semibold">任务中心</h1>
        <button
          onClick={() => setShowHistory(true)}
          className="ml-auto flex items-center gap-1.5 rounded-lg border border-(--color-border) px-3 py-1.5 text-sm hover:bg-(--color-panel2)"
        >
          <History size={15} /> 历史记录
        </button>
      </div>
      <div className="flex-1 overflow-auto p-6">
        <div className="w-full space-y-3">
          {tasks.data?.map((t) => {
            const prog = t.progress as { done?: number; total?: number; lines?: number; discarded?: number; failed_batches?: number };
            const pct = prog.total ? Math.min(100, Math.round(((prog.done ?? 0) / prog.total) * 100)) : null;
            return (
              <div key={t.id} className="rounded-xl border border-(--color-border) bg-(--color-panel) px-4 py-3">
                <div className="flex items-center gap-3">
                  <span className="rounded-md bg-(--color-panel2) px-2 py-0.5 text-xs font-medium">
                    {taskTypeLabel[t.task_type] ?? t.task_type}
                  </span>
                  {t.project_name && t.project_id && (
                    t.status === "running" || t.status === "queued" ? (
                      <Link
                        to={`/projects/${t.project_id}`}
                        title="返回该项目"
                        className="text-sm font-medium text-(--color-text) hover:text-(--color-accent)"
                      >
                        {t.project_name}
                      </Link>
                    ) : (
                      <span className="text-sm font-medium text-(--color-text)">{t.project_name}</span>
                    )
                  )}
                  {t.status !== "running" && t.status !== "queued" && (
                    <span className={`text-sm font-medium ${statusColor[t.status] ?? ""}`}>
                      {statusLabel[t.status] ?? t.status}
                    </span>
                  )}
                  {t.model && (
                    <span className="rounded-md bg-(--color-panel2) px-2 py-0.5 font-mono text-xs text-(--color-muted)">
                      {t.model}
                    </span>
                  )}
                  {t.created_at && (
                    <span className="text-xs text-(--color-muted)">
                      {new Date(t.created_at).toLocaleString()}
                    </span>
                  )}
                  <div className="ml-auto flex items-center gap-2">
                    {(t.status === "failed" || t.status === "paused") && (
                      <button
                        onClick={() => retry.mutate(t.id)}
                        className="flex items-center gap-1 rounded-lg border border-(--color-border) px-2 py-1 text-xs hover:bg-(--color-panel2)"
                        title="重新执行（已完成的步骤会保留）"
                      >
                        <RotateCcw size={12} /> 重试
                      </button>
                    )}
                    {t.status === "completed" && (
                      <button
                        onClick={() => archiveTask.mutate({ taskId: t.id, archived: true })}
                        className="flex items-center gap-1 rounded-lg border border-(--color-border) px-2 py-1 text-xs text-(--color-muted) hover:bg-(--color-panel2)"
                        title="归档到历史记录"
                      >
                        <Archive size={12} /> 归档
                      </button>
                    )}
                    {["failed", "cancelled"].includes(t.status) && (
                      <button
                        onClick={() => removeTask.mutate(t.id)}
                        className="flex items-center gap-1 rounded-lg border border-(--color-border) px-2 py-1 text-xs text-(--color-muted) hover:bg-red-50 hover:text-red-600"
                        title="删除这条任务记录（不影响译文）"
                      >
                        <Trash2 size={12} /> 删除
                      </button>
                    )}
                  </div>
                </div>

                {pct != null && (
                  <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-(--color-panel2)">
                    <div
                      className="h-full rounded-full bg-(--color-accent) transition-all"
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                )}

                {t.task_type === "express" && <ExpressSteps task={t} />}
                {t.task_type === "parse" && (() => {
                  const p = t.progress as {
                    coverage_ok?: boolean; total_chars?: number; covered_chars?: number;
                    chapter_count?: number; scene_count?: number; dialogue_count?: number;
                    narrative_count?: number; anomalies?: string[];
                  };
                  if (p.total_chars == null) return null;
                  return (
                    <div className="mt-2 rounded-lg bg-(--color-panel) px-3 py-2 text-xs leading-5 text-(--color-muted)">
                      <p className={p.coverage_ok ? "text-emerald-600" : "text-red-600"}>
                        覆盖：{p.covered_chars}/{p.total_chars} 字符 {p.coverage_ok ? "✓ 完整" : "✗ 有缺口"}
                      </p>
                      <p className="mt-1">
                        章节 {p.chapter_count} · 场景 {p.scene_count} · 对白 {p.dialogue_count} · 叙述 {p.narrative_count}
                      </p>
                      {(p.anomalies ?? []).length > 0 && (
                        <div className="mt-1">
                          <p className="text-amber-600">异常 {p.anomalies!.length} 项</p>
                          {p.anomalies!.slice(0, 10).map((a, i) => (
                            <p key={i} className="break-all">{a}</p>
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })()}

                {t.task_type !== "express" && prog.total != null && (
                  <div className="mt-2 flex items-center gap-2 text-xs text-(--color-muted)">
                    {t.status === "running" && <Loader2 size={13} className="animate-spin text-(--color-accent)" />}
                    {t.task_type === "dialogue" ? (
                      <span>
                        批次 {prog.done ?? 0} / {prog.total}
                        {typeof prog.lines === "number" && ` · 已抽出台词 ${prog.lines} 条`}
                        {prog.discarded ? ` · 丢弃 ${prog.discarded}` : ""}
                        {prog.failed_batches ? ` · 失败批次 ${prog.failed_batches}` : ""}
                      </span>
                    ) : (
                      <span>
                        已处理 {prog.done ?? 0} / {prog.total} 个窗口
                        （{prog.total ? Math.round(((prog.done ?? 0) / prog.total) * 100) : 0}%）
                        {(t.progress as { qa_issues?: number }).qa_issues != null && (
                          <> · QA 问题 {(t.progress as { qa_issues?: number }).qa_issues} 条</>
                        )}
                      </span>
                    )}
                  </div>
                )}

                {t.error_message && (
                  <div className="mt-2 rounded-lg bg-red-50 px-3 py-2 text-xs leading-5 text-red-600">
                    出错：{t.error_message}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {showHistory && (
        <div
          className="fixed inset-0 z-[70] flex items-center justify-center bg-black/30 p-6"
          onClick={() => setShowHistory(false)}
        >
          <div
            className="flex max-h-[80vh] w-full max-w-3xl flex-col rounded-2xl bg-(--color-bg) p-5 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between">
              <h2 className="text-base font-semibold">历史记录</h2>
              <button
                onClick={() => setShowHistory(false)}
                className="rounded-lg p-1.5 text-(--color-muted) hover:bg-(--color-panel2)"
                title="关闭"
              >
                <X size={18} />
              </button>
            </div>
            <div className="mt-4 flex-1 space-y-2 overflow-auto">
              {archivedTasks.data?.length === 0 && (
                <p className="py-8 text-center text-sm text-(--color-muted)">暂无历史记录</p>
              )}
              {archivedTasks.data?.map((t) => (
                <div key={t.id} className="flex items-center gap-3 rounded-xl border border-(--color-border) bg-(--color-panel) px-4 py-2.5">
                  <span className="rounded-md bg-(--color-panel2) px-2 py-0.5 text-xs font-medium">
                    {taskTypeLabel[t.task_type] ?? t.task_type}
                  </span>
                  <span className="text-sm font-medium">{t.project_name ?? "已删除项目"}</span>
                  <span className={`text-xs ${t.status === "completed" ? "text-emerald-600" : "text-red-600"}`}>
                    {statusLabel[t.status] ?? t.status}
                  </span>
                  {t.created_at && (
                    <span className="text-xs text-(--color-muted)">{new Date(t.created_at).toLocaleString()}</span>
                  )}
                  <button
                    onClick={() => archiveTask.mutate({ taskId: t.id, archived: false })}
                    className="ml-auto flex items-center gap-1 rounded-lg border border-(--color-border) px-2 py-1 text-xs hover:bg-(--color-panel2)"
                    title="取消归档，回到任务中心"
                  >
                    <Undo2 size={12} /> 取消归档
                  </button>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
