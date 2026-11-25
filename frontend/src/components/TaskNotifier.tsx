/** 任务状态通知：任务完成或失败时向用户发送应用内消息。 */

import { useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { toast } from "../lib/feedback";
import type { TaskInfo } from "../lib/types";

const SNAPSHOT_KEY = "mt-task-status-snapshot";
const MAX_SNAPSHOT = 20;

const typeLabel: Record<string, string> = {
  express: "一键直出",
  translate: "全文翻译",
  trial: "试译",
  knowledge: "知识提取",
  parse: "解析",
  scene_cards: "场景卡",
  qa: "质量检查",
  analyze: "分析",
};

function loadSnapshot(): Record<string, string> {
  try {
    const raw = localStorage.getItem(SNAPSHOT_KEY);
    if (raw) return JSON.parse(raw);
  } catch {
    /* 忽略坏数据 */
  }
  return {};
}

function saveSnapshot(snapshot: Record<string, string>) {
  const keys = Object.keys(snapshot).slice(-MAX_SNAPSHOT);
  const trimmed: Record<string, string> = {};
  for (const k of keys) trimmed[k] = snapshot[k];
  try {
    localStorage.setItem(SNAPSHOT_KEY, JSON.stringify(trimmed));
  } catch {
    /* 存储不可用忽略 */
  }
}

export default function TaskNotifier() {
  const firedRef = useRef<Set<string>>(new Set());

  const tasks = useQuery({
    queryKey: ["task-notifier"],
    queryFn: () => api<TaskInfo[]>("/tasks"),
    refetchInterval: 5000,
  });

  useEffect(() => {
    const data = tasks.data;
    if (!data) return;
    const prev = loadSnapshot();
    const next: Record<string, string> = {};
    for (const t of data) {
      const key = `${t.id}:${t.status}:${t.error_message ?? ""}`;
      next[t.id] = `${t.status}|${t.error_message ?? ""}`;
      if (!firedRef.current.has(key) && prev[t.id] && prev[t.id] !== `${t.status}|${t.error_message ?? ""}`) {
        firedRef.current.add(key);
        const name = `${t.project_name ? `${t.project_name} · ` : ""}${typeLabel[t.task_type] ?? t.task_type}`;
        if (t.status === "completed") {
          toast.success(`${name}已完成`);
        } else if (t.status === "failed") {
          // 只提示失败；错误详情在任务卡展示，避免同一错误显示两遍
          toast.error(`${name}失败，请到任务中心查看详情`);
        }
      }
    }
    saveSnapshot(next);
  }, [tasks.data]);

  return null;
}
