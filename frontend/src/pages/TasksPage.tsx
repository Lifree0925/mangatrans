/** 任务中心：任务步骤明细、进度与错误（中文）。 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Circle, Loader2, RotateCcw, Trash2, XCircle } from "lucide-react";
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
  { key: "scene_cards", label: "场景卡生成" },
  { key: "trial", label: "试译与风格基线" },
  { key: "translation", label: "全文翻译" },
  { key: "qa", label: "质量检查" },
  { key: "approve", label: "批准定稿" },
];

function stepDetail(key: string, step: Record<string, unknown> | undefined): string {
  if (!step) return "";
  switch (key) {
    case "knowledge": {
      const s = step as { batches?: number; added?: number; failed_batches?: number };
      return s.batches != null ? `共 ${s.batches} 批，提取 ${s.added ?? 0} 条${s.failed_batches ? `（失败 ${s.failed_batches} 批）` : ""}` : "";
    }
    case "scene_cards": {
      const s = step as { generated?: number; failed?: number };
      return s.generated != null ? `已生成 ${s.generated} 场${s.failed ? `（失败 ${s.failed} 场）` : ""}` : "";
    }
    case "trial": {
      const s = step as { windows?: number };
      return s.windows != null ? `已试译 ${s.windows} 个窗口` : "";
    }
    case "translation": {
      const s = step as { done?: number; total?: number };
      return s.total != null ? `已翻译 ${s.done ?? 0} / ${s.total} 个窗口` : "";
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
        s.issues ? `发现问题 ${s.issues} 条` : "",
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
  const current = progress.current_step ?? (task.status === "running" ? EXPRESS_STEPS[0].key : "");
  // 完成判定：该步骤已写入 steps；进行中：current_step 指向；失败任务：停在 current
  const currentIndex = EXPRESS_STEPS.findIndex((s) => s.key === current);
  return (
    <div className="mt-3 space-y-1.5">
      {EXPRESS_STEPS.map((s, i) => {
        const step = steps[s.key];
        const done = step !== undefined || (task.status === "completed");
        const running = task.status === "running" && !done && i === (currentIndex >= 0 ? currentIndex : i);
        const failedHere = task.status === "failed" && !done && i === (currentIndex >= 0 ? currentIndex : 0);
        return (
          <div key={s.key} className="flex items-start gap-2 text-xs">
            <span className="mt-0.5 shrink-0">
              {failedHere ? (
                <XCircle size={13} className="text-red-600" />
              ) : done ? (
                <CheckCircle2 size={13} className="text-emerald-600" />
              ) : running ? (
                <Loader2 size={13} className="animate-spin text-(--color-accent)" />
              ) : (
                <Circle size={13} className="text-(--color-border)" />
              )}
            </span>
            <div className="min-w-0">
              <span className={done ? "text-(--color-text)" : failedHere ? "font-medium text-red-600" : "text-(--color-muted)"}>
                {s.label}
              </span>
              <span className="ml-2 text-(--color-muted)">{stepDetail(s.key, step)}</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

export default function TasksPage() {
  const qc = useQueryClient();

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
      <div className="border-b border-(--color-border) px-6 py-4">
        <h1 className="text-lg font-semibold">任务中心</h1>
      </div>
      <div className="flex-1 overflow-auto p-6">
        <div className="w-full space-y-3">
          {tasks.data?.map((t) => {
            const prog = t.progress as { done?: number; total?: number };
            const pct = prog.total ? Math.min(100, Math.round(((prog.done ?? 0) / prog.total) * 100)) : null;
            return (
              <div key={t.id} className="rounded-xl border border-(--color-border) bg-(--color-panel) px-4 py-3">
                <div className="flex items-center gap-3">
                  <span className="rounded-md bg-(--color-panel2) px-2 py-0.5 text-xs font-medium">
                    {taskTypeLabel[t.task_type] ?? t.task_type}
                  </span>
                  {t.project_name && (
                    <span className="text-sm font-medium text-(--color-text)">{t.project_name}</span>
                  )}
                  <span className={`text-sm font-medium ${statusColor[t.status] ?? ""}`}>
                    {statusLabel[t.status] ?? t.status}
                  </span>
                  {t.model && (
                    <span className="rounded-md bg-(--color-panel2) px-2 py-0.5 font-mono text-xs text-(--color-muted)">
                      {t.model}
                    </span>
                  )}
                  {t.created_at && (
                    <span className="text-xs text-(--color-muted)">
                      {new Date(t.created_at).toLocaleString()}
                      {t.status === "running" && " · 运行中"}
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
                    {["failed", "cancelled", "completed"].includes(t.status) && (
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
                {t.task_type !== "express" && prog.total != null && (
                  <p className="mt-2 text-xs text-(--color-muted)">
                    已处理 {prog.done ?? 0} / {prog.total} 个窗口
                  </p>
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
    </div>
  );
}
