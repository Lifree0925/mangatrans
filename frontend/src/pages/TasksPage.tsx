/** 任务中心：任务步骤明细、进度与错误（中文）。 */

import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { Archive, Circle, CheckCircle2, History, Loader2, Pause, Play, RotateCcw, Trash2, Undo2, X, XCircle } from "lucide-react";
import { api } from "../lib/api";
import { toast, useConfirmStore } from "../lib/feedback";
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
  trial: "自动风格基线",
  knowledge: "知识提取",
  parse: "解析",
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
  { key: "trial", label: "自动风格基线" },
  { key: "translation", label: "全文翻译" },
  { key: "qa", label: "自动质量检查" },
  { key: "approve", label: "自动定稿" },
];

function fmtSecs(secs?: number): string {
  if (secs == null) return "";
  if (secs < 60) return `${Math.round(secs)} 秒`;
  const m = Math.floor(secs / 60);
  const r = Math.round(secs % 60);
  return r ? `${m} 分 ${r} 秒` : `${m} 分钟`;
}

/** 运行状态只用短标签表达：正常 / 等待 / 异常，不给用户堆说明文字。 */
function taskLiveState(t: TaskInfo): {
  label: string; textClass: string; dotClass: string; pulse: boolean; title: string;
} | null {
  if (t.status !== "running") return null;
  const activeCalls = t.live?.active_calls ?? 0;
  if (activeCalls > 0) {
    const stages = (t.live?.stages ?? []).join(" / ");
    const waited = t.live?.oldest_seconds ?? 0;
    return {
      label: "模型响应中",
      textClass: waited >= 180 ? "text-amber-600" : "text-emerald-600",
      dotClass: waited >= 180 ? "bg-amber-500" : "bg-emerald-500",
      pulse: true,
      title: `${stages || "模型请求"} · ${activeCalls} 个并发 · 最长等待 ${fmtSecs(waited)}`,
    };
  }
  const raw = t.last_activity_at ?? t.started_at;
  const last = raw ? new Date(raw).getTime() : NaN;
  if (!Number.isFinite(last)) {
    return {
      label: "运行正常", textClass: "text-emerald-600", dotClass: "bg-emerald-500",
      pulse: false, title: "任务进程运行中",
    };
  }
  const age = Math.max(0, (Date.now() - last) / 1000);
  if (age < 90) {
    return {
      label: "运行正常", textClass: "text-emerald-600", dotClass: "bg-emerald-500",
      pulse: false, title: `最近活动 ${fmtSecs(age)}前`,
    };
  }
  if (age < 300) {
    return {
      label: "等待响应", textClass: "text-amber-600", dotClass: "bg-amber-500",
      pulse: false, title: `最近活动 ${fmtSecs(age)}前，任务仍在运行`,
    };
  }
  return {
    label: "响应较慢", textClass: "text-red-600", dotClass: "bg-red-500",
    pulse: false, title: `已超过 ${fmtSecs(age)}没有新活动，任务进程仍在运行`,
  };
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
    case "profiles": {
      const s = step as { profiles?: number; characters?: number; error?: string };
      if (s.error) return `生成失败：${s.error}`;
      if (s.profiles == null) return "";
      return `已生成 ${s.profiles} 份角色档案（覆盖 ${s.characters ?? 0} 个角色）`;
    }
    case "storylines": {
      const s = step as { generated?: number; failed?: number };
      if (s.generated == null) return "";
      return `已生成 ${s.generated} 章剧情卡${s.failed ? `（失败 ${s.failed} 章，不阻塞）` : ""}`;
    }
    case "trial": {
      const s = step as { windows?: number; style_guide_id?: string };
      return s.windows != null ? `已生成 ${s.windows} 个窗口样本${s.style_guide_id ? " · 风格基线已建立" : ""}` : "";
    }
    case "translation": {
      const s = step as {
        done?: number; total?: number; phase?: string; draft_done?: number;
        active_polish?: number; failed_windows?: number;
        finalize_done?: number; structure_fallbacks?: number;
      };
      if (s.total == null) return "";
      if (s.phase === "draft") {
        const pct = s.total ? Math.round(((s.draft_done ?? 0) / s.total) * 100) : 0;
        const polishStarted = (s.active_polish ?? 0) > 0 ? " · 同步润色中" : "";
        return `草稿锁事实 ${s.draft_done ?? 0} / ${s.total} 个窗口（${pct}%）${polishStarted}`;
      }
      if (s.phase === "repair" || s.phase === "finalize") {
        const pct = s.total ? Math.round(((s.finalize_done ?? 0) / s.total) * 100) : 0;
        const fallback = s.structure_fallbacks ? ` · 结构兜底 ${s.structure_fallbacks} 段` : "";
        const label = s.phase === "repair" ? "结构校验与修复" : "最终校验与落库";
        return `${label} ${s.finalize_done ?? 0} / ${s.total} 个窗口（${pct}%）${fallback}`;
      }
      const pct = s.total ? Math.round(((s.done ?? 0) / s.total) * 100) : 0;
      const failed = s.failed_windows ? ` · ${s.failed_windows} 个窗口校验失败` : "";
      return `润色 ${s.done ?? 0} / ${s.total} 个窗口（${pct}%）${failed}`;
    }
    case "qa": {
      const s = step as { done?: number; total?: number; issues?: number; needs_review?: number; blocked?: number; failed?: number; severity_counts?: Record<string, number>; semantic_done?: number; semantic_total?: number; gate_passed?: boolean };
      const sem = s.semantic_total ? `语义质检 ${s.semantic_done ?? 0} / ${s.semantic_total}` : "";
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
        s.needs_review ? `未解决 ${s.needs_review} 窗` : "",
        s.blocked ? `拦截 ${s.blocked} 窗` : "",
      ].filter(Boolean).join(" · ");
      return [sem, base, extra].filter(Boolean).join(" · ");
    }
    case "approve": {
      const s = step as { approved?: number };
      return s.approved != null ? `已自动定稿 ${s.approved} 条译文` : "";
    }
  }
  return "";
}

type StepNode = {
  key: string;
  label: string;
  complete: boolean;
  active: boolean;
  error?: boolean;
  detail: string;
};

/** 统一任务卡节点渲染（一键直出与全文翻译共用同款样式）。 */
function StepNodes({ nodes }: { nodes: StepNode[] }) {
  return (
    <div className="mt-3 space-y-1.5">
      {nodes.map((n) => (
        <div key={n.key} className="flex items-start gap-2 text-xs">
          <span className="mt-0.5 shrink-0">
            {n.error ? (
              <XCircle size={13} className="text-red-600" />
            ) : n.active ? (
              <Loader2 size={13} className="animate-spin text-emerald-600" />
            ) : n.complete ? (
              <CheckCircle2 size={13} className="text-emerald-600" />
            ) : (
              <Circle size={13} className="text-(--color-border)" />
            )}
          </span>
          <div className="min-w-0">
            <span className={n.error ? "font-medium text-red-600" : n.complete ? "text-(--color-text)" : "text-(--color-muted)"}>
              {n.label}
            </span>
            <span className={`ml-2 ${n.error ? "text-red-600" : "text-(--color-muted)"}`}>{n.detail}</span>
            {n.active && <span className="ml-2 font-medium text-(--color-accent)">执行中…</span>}
          </div>
        </div>
      ))}
    </div>
  );
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
  // 并行阶段（Draft 与准备步骤并行、对齐与 QA 并行）：有部分进度的未完成步骤同时显示执行中
  const stepRunning = (key: string): boolean => {
    if (task.status !== "running") return false;
    if (stepDone(key)) return false;
    const step = steps[key];
    if (step !== undefined) return true; // 已写入进度数据的未完成步骤 = 正在执行
    return current === key;
  };
  const nodes: StepNode[] = EXPRESS_STEPS.map((s, i) => {
    const step = steps[s.key];
    const done = stepDone(s.key) || task.status === "completed";
    const running = stepRunning(s.key);
    const failedHere =
      task.status === "failed" &&
      !done &&
      i === (firstPending >= 0 ? firstPending : currentIndex >= 0 ? currentIndex : 0);
    return {
      key: s.key,
      label: s.label,
      complete: done,
      active: running,
      error: failedHere,
      detail: stepDetail(s.key, step),
    };
  });
  return <StepNodes nodes={nodes} />;
}

/** 全文翻译（重新翻译/章节重译）任务节点：从进度字段推导各阶段。 */
function TranslateSteps({ task }: { task: TaskInfo }) {
  const p = task.progress as {
    done?: number; total?: number; draft_done?: number;
    qa_issues?: number; qa_done?: number; failed?: number; phase?: string;
  };
  const total = p.total ?? 0;
  const draftDone = p.draft_done ?? 0;
  const done = p.done ?? 0;
  const running = task.status === "running" || task.status === "queued";
  const finished = task.status === "completed";
  const nodes = [
    {
      key: "draft",
      label: "草稿（锁事实）",
      complete: finished || (total > 0 && draftDone >= total),
      active: running && draftDone < total,
      detail:
        total > 0
          ? `已完成 ${draftDone} / ${total} 个窗口草稿（${total ? Math.round((draftDone / total) * 100) : 0}%）`
          : "等待开始",
    },
    {
      key: "polish",
      label: "润色（情境化 · 思考模式）",
      complete: finished || (total > 0 && done >= total),
      active: running && draftDone > 0 && done < total,
      detail:
        total > 0
          ? `已润色 ${done} / ${total} 个窗口（${total ? Math.round((done / total) * 100) : 0}%）`
          : "等待草稿完成",
    },
    {
      key: "qa",
      label: "质检打回定点修复",
      complete: total > 0 && (p.qa_done ?? 0) >= total,
      active: running && done >= total && (p.qa_done ?? 0) < total,
      detail:
        total > 0
          ? `定点修复 ${p.qa_done ?? 0} / ${total} 个窗口，发现问题 ${p.qa_issues ?? 0} 条`
          : "等待润色完成",
    },
    {
      key: "failed",
      label: "失败窗口",
      complete: !(p.failed ?? 0),
      active: false,
      detail: p.failed ? `失败 ${p.failed} 个窗口（自动重试 3 次后放弃，可按章节重译）` : "无失败窗口",
      danger: !!p.failed,
    },
  ];
  return <StepNodes nodes={nodes.map((n) => ({ ...n, error: n.danger }))} />;
}

export default function TasksPage() {
  const qc = useQueryClient();
  const [searchParams] = useSearchParams();
  const focusedTaskId = searchParams.get("task");
  const focusedOnceRef = useRef(false);
  const [showHistory, setShowHistory] = useState(false);
  const tasks = useQuery({
    queryKey: ["tasks"],
    queryFn: () => api<TaskInfo[]>("/tasks"),
    // 有活动任务时快速刷新；全部结束后降频，避免空转轮询。
    refetchInterval: (query) => {
      const rows = query.state.data as TaskInfo[] | undefined;
      return rows?.some((t) => t.status === "running" || t.status === "queued") ? 1500 : 10000;
    },
  });

  useEffect(() => {
    if (!focusedTaskId || focusedOnceRef.current || !tasks.data) return;
    const el = document.getElementById(`task-${focusedTaskId}`);
    if (el) {
      el.scrollIntoView({ block: "center", behavior: "smooth" });
      focusedOnceRef.current = true;
    }
  }, [focusedTaskId, tasks.data]);

  const pauseTask = useMutation({
    mutationFn: (id: string) => api(`/tasks/${id}/pause`, { method: "POST", body: "{}" }),
    onSuccess: () => {
      toast.success("任务已暂停（当前窗口完成后停止）");
      qc.invalidateQueries({ queryKey: ["tasks"] });
    },
    onError: (err) => toast.error(`暂停失败：${String(err)}`),
  });
  const resumeTask = useMutation({
    mutationFn: (id: string) => api(`/tasks/${id}/resume`, { method: "POST", body: "{}" }),
    onSuccess: () => {
      toast.success("任务已继续执行");
      qc.invalidateQueries({ queryKey: ["tasks"] });
    },
    onError: (err) => toast.error(`继续失败：${String(err)}`),
  });
  const removeTask = useMutation({
    mutationFn: (taskId: string) => api(`/tasks/${taskId}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["tasks", "archived"] });
      toast.success("任务记录已删除");
    },
    onError: (err) => toast.error(`删除失败：${String(err)}`),
  });

  const confirm = useConfirmStore((s) => s.show);

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
        <div className="mx-auto max-w-4xl space-y-3">
          {tasks.data?.map((t) => {
            const prog = t.progress as {
              done?: number; total?: number; phase?: string; draft_done?: number;
              lines?: number; discarded?: number; failed_batches?: number;
              steps?: Record<string, {
                done?: number; total?: number; phase?: string; draft_done?: number;
                base_done?: number; model_done?: number; model_total?: number;
              }>;
            };
            const trStep = prog.steps?.translation;
            const doneN = trStep?.total != null ? trStep.done ?? 0 : prog.done ?? 0;
            const totalN = trStep?.total != null ? trStep.total : prog.total ?? 0;
            const draftN = trStep?.draft_done ?? prog.draft_done ?? 0;
            // 只按已经通过结构校验并落库的窗口推进，避免模型“已返回但未落库”被误显示为完成。
            const livePolish = doneN;
            const combinedTotal = totalN > 0 ? totalN * 2 : 0;
            const pct = combinedTotal
              ? Math.min(100, Math.round(((Math.min(totalN, draftN) + Math.min(totalN, livePolish)) / combinedTotal) * 100))
              : null;
            return (
              <div
                key={t.id}
                id={`task-${t.id}`}
                className={`rounded-xl border bg-(--color-panel) px-4 py-3 transition-colors ${
                  focusedTaskId === t.id
                    ? "border-(--color-accent) ring-2 ring-(--color-accent)"
                    : "border-(--color-border)"
                }`}
              >
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
                  {t.status === "running" ? (
                    <span className="flex items-center gap-1 text-sm font-medium text-emerald-600">
                      <Loader2 size={13} className="animate-spin" /> 运行中
                    </span>
                  ) : t.status === "queued" ? (
                    <span className="flex items-center gap-1 text-sm font-medium text-(--color-muted)">
                      <Circle size={11} /> 排队中
                    </span>
                  ) : (
                    <span className={`text-sm font-medium ${statusColor[t.status] ?? ""}`}>
                      {statusLabel[t.status] ?? t.status}
                    </span>
                  )}
                  {t.status === "running" && (() => {
                    const live = taskLiveState(t);
                    return live ? (
                      <span
                        title={live.title}
                        className={`inline-flex items-center gap-1.5 text-xs font-medium ${live.textClass}`}
                      >
                        <span className="relative flex h-1.5 w-1.5">
                          {live.pulse && <span className={`absolute inline-flex h-full w-full animate-ping rounded-full opacity-70 ${live.dotClass}`} />}
                          <span className={`relative inline-flex h-1.5 w-1.5 rounded-full ${live.dotClass}`} />
                        </span>
                        {live.label}
                      </span>
                    ) : null;
                  })()}
                  {t.status === "running" && (t.elapsed_seconds ?? 0) > 0 && (
                    <span className="text-xs text-(--color-muted)">
                      已运行 {fmtSecs(t.elapsed_seconds)}
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
                    {(t.status === "running" || t.status === "queued") && (
                      <button
                        onClick={() => pauseTask.mutate(t.id)}
                        disabled={pauseTask.isPending}
                        className="flex items-center gap-1 rounded-lg border border-(--color-border) px-2 py-1 text-xs hover:bg-(--color-panel2) disabled:opacity-50"
                        title="暂停任务（当前窗口完成后停止，可随时继续）"
                      >
                        <Pause size={12} /> 暂停
                      </button>
                    )}
                    {t.status === "paused" && (
                      <button
                        onClick={() => resumeTask.mutate(t.id)}
                        disabled={resumeTask.isPending}
                        className="flex items-center gap-1 rounded-lg border border-(--color-border) px-2 py-1 text-xs text-emerald-600 hover:bg-(--color-panel2) disabled:opacity-50"
                        title="从暂停处继续执行"
                      >
                        <Play size={12} /> 继续
                      </button>
                    )}
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

                {pct != null && t.status !== "completed" && (
                  <div
                    className="relative mt-2 h-1.5 overflow-hidden rounded-full bg-(--color-panel2)"
                    role="progressbar"
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={pct}
                  >
                    <div
                      className="h-full rounded-full bg-(--color-accent) transition-all duration-500"
                      style={{ width: `${pct}%` }}
                    />
                    {t.status === "running" && (
                      <div className="absolute inset-y-0 right-0 w-16 animate-pulse bg-white/45" />
                    )}
                  </div>
                )}

                {t.task_type === "express" && <ExpressSteps task={t} />}
                {t.task_type === "translate" && <TranslateSteps task={t} />}
                {t.error_message && (
                  <p className="mt-2 break-all rounded-lg bg-red-50 px-3 py-2 text-xs leading-5 text-red-600">
                    失败原因：{t.error_message}
                  </p>
                )}
                {t.usage && (t.usage.requests ?? 0) > 0 && (() => {
                  const cachedInput = t.usage?.cached_tokens ?? 0;
                  const totalInput = t.usage?.prompt_tokens ?? 0;
                  const uncachedInput = Math.max(0, totalInput - cachedInput);
                  const output = t.usage?.completion_tokens ?? 0;
                  return (
                    <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 rounded-lg bg-(--color-panel2) px-3 py-2 text-xs text-(--color-muted)">
                      <span>缓存输入 {cachedInput.toLocaleString()} tokens</span>
                      <span>未缓存输入 {uncachedInput.toLocaleString()} tokens</span>
                      <span>输出 {output.toLocaleString()} tokens</span>
                    </div>
                  );
                })()}
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
            className="flex h-[560px] max-h-[calc(100vh-3rem)] w-full max-w-3xl flex-col rounded-2xl bg-(--color-bg) p-5 shadow-2xl"
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
                  <button
                    onClick={async () => {
                      const ok = await confirm(
                        "永久删除任务记录？",
                        "删除后不可恢复（不影响已生成的译文）。",
                        true
                      );
                      if (ok) removeTask.mutate(t.id);
                    }}
                    className="flex items-center gap-1 rounded-lg border border-(--color-border) px-2 py-1 text-xs text-(--color-muted) hover:bg-red-50 hover:text-red-600"
                    title="永久删除这条任务记录"
                  >
                    <Trash2 size={12} /> 删除
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
