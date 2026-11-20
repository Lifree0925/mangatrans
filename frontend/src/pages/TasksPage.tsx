/** 任务中心：当前项目的任务与进度。 */

import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import type { Project, TaskInfo } from "../lib/types";

const statusColor: Record<string, string> = {
  queued: "text-(--color-muted)",
  running: "text-(--color-text)",
  completed: "text-emerald-600",
  failed: "text-red-600",
  paused: "text-amber-600",
};

/** 任务类型与状态的中文显示名（面向非技术用户）。 */
const taskTypeLabel: Record<string, string> = {
  express: "一键直出",
  translate: "翻译",
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

export default function TasksPage() {
  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api<Project[]>("/projects"),
  });
  const active = projects.data?.[0];

  const tasks = useQuery({
    queryKey: ["tasks", active?.id],
    queryFn: () => api<TaskInfo[]>(`/projects/${active!.id}/tasks`),
    enabled: !!active,
    refetchInterval: 2000,
  });

  return (
    <div className="h-full flex flex-col">
      <div className="border-b border-(--color-border) px-6 py-4">
        <h1 className="text-lg font-semibold">任务中心</h1>
      </div>
      <div className="flex-1 overflow-auto p-6">
        <div className="space-y-2">
          {tasks.data?.map((t) => (
            <div key={t.id} className="rounded-xl border border-(--color-border) bg-(--color-panel) px-4 py-3">
              <div className="flex items-center gap-3">
                <span className={`text-sm font-medium ${statusColor[t.status] ?? ""}`}>
                  {taskTypeLabel[t.task_type] ?? t.task_type} · {statusLabel[t.status] ?? t.status}
                </span>
                {(t.progress as { done?: number; total?: number }).total != null && (
                  <span className="font-mono text-xs text-(--color-muted)">
                    {(t.progress as { done?: number }).done ?? 0}/{(t.progress as { total?: number }).total}
                  </span>
                )}
              </div>
              {t.error_message && (
                <p className="mt-1 break-all text-xs text-red-600">{t.error_message}</p>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
