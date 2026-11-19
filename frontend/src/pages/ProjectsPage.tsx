/** 项目列表：创建、打开、重命名、删除。 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Folder, Plus, Trash2 } from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import type { Project } from "../lib/types";

export default function ProjectsPage() {
  const qc = useQueryClient();
  const [name, setName] = useState("");

  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api<Project[]>("/projects"),
  });

  const create = useMutation({
    mutationFn: () => api<Project>("/projects", {
      method: "POST",
      body: JSON.stringify({ name: name.trim() || "未命名项目" }),
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      setName("");
    },
  });

  const remove = useMutation({
    mutationFn: (id: string) => api<void>(`/projects/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["projects"] }),
  });

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center gap-3 border-b border-(--color-border) px-6 py-4">
        <h1 className="text-lg font-semibold">项目</h1>
        <div className="ml-auto flex gap-2">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && create.mutate()}
            placeholder="新项目名称"
            className="w-56 rounded border border-(--color-border) bg-(--color-panel) px-3 py-1.5 text-sm outline-none focus:border-(--color-accent)"
          />
          <button
            onClick={() => create.mutate()}
            disabled={create.isPending}
            className="flex items-center gap-1.5 rounded bg-(--color-accent) px-3 py-1.5 text-sm text-white hover:bg-(--color-accent-hover) disabled:opacity-50"
          >
            <Plus size={15} /> 创建
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-auto p-6">
        {projects.isLoading && <p className="text-(--color-muted) text-sm">加载中…</p>}
        {projects.error && (
          <p className="text-sm text-red-400">后端连接失败：{String(projects.error)}</p>
        )}
        <div className="grid grid-cols-2 gap-3">
          {projects.data?.map((p) => (
            <Link
              key={p.id}
              to={`/projects/${p.id}`}
              className="group flex items-center gap-3 rounded border border-(--color-border) bg-(--color-panel) px-4 py-3 hover:border-(--color-accent)"
            >
              <Folder size={18} className="shrink-0 text-(--color-accent)" />
              <div className="min-w-0">
                <p className="truncate text-sm font-medium">{p.name}</p>
                <p className="font-mono text-xs text-(--color-muted)">
                  {p.source_language} → {p.target_language} ·{" "}
                  {new Date(p.updated_at).toLocaleString()}
                </p>
              </div>
              <button
                onClick={() => remove.mutate(p.id)}
                className="ml-auto rounded p-1.5 text-(--color-muted) opacity-0 hover:bg-(--color-panel2) hover:text-red-400 group-hover:opacity-100"
                title="删除项目"
              >
                <Trash2 size={15} />
              </button>
            </Link>
          ))}
        </div>
        {projects.data?.length === 0 && (
          <p className="mt-10 text-center text-sm text-(--color-muted)">
            还没有项目，创建第一个项目开始翻译
          </p>
        )}
      </div>
    </div>
  );
}
