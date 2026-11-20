/** 项目列表：首屏立即渲染，连接状态静默处理（ChatGPT 风格）。 */

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
    mutationFn: () =>
      api<Project>("/projects", {
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

  // 首次请求仍在进行（未发生过失败）时显示骨架；连接被拒后立即回到正常空状态
  const showSkeleton = projects.isLoading && projects.failureCount === 0;
  const connecting = !projects.data && projects.isFetching;

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-3 border-b border-(--color-border) px-6 py-4">
        <h1 className="text-lg font-semibold">项目</h1>
        <div className="ml-auto flex gap-2">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && create.mutate()}
            placeholder="新项目名称"
            className="w-56 rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-1.5 text-sm outline-none focus:border-(--color-text)"
          />
          <button
            onClick={() => create.mutate()}
            disabled={create.isPending}
            className="flex items-center gap-1.5 rounded-lg bg-(--color-accent) px-3 py-1.5 text-sm text-white hover:bg-(--color-accent-hover) disabled:opacity-50"
          >
            <Plus size={15} /> 创建
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-auto p-6">
        <div className="mx-auto max-w-3xl">
          {showSkeleton && (
            <div className="space-y-2">
              {[0, 1, 2].map((i) => (
                <div key={i} className="h-16 animate-pulse rounded-xl bg-(--color-panel)" />
              ))}
            </div>
          )}

          {!showSkeleton && !projects.data?.length && (
            <div className="rounded-xl border border-dashed border-(--color-border) px-6 py-16 text-center">
              <p className="text-sm text-(--color-muted)">
                还没有项目。在上方输入名称，创建第一个翻译项目。
              </p>
            </div>
          )}

          <div className="space-y-2">
            {projects.data?.map((p) => (
              <Link
                key={p.id}
                to={`/projects/${p.id}`}
                className="group flex items-center gap-3 rounded-xl px-4 py-3 hover:bg-(--color-panel)"
              >
                <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-(--color-panel2)">
                  <Folder size={17} className="text-(--color-muted)" />
                </div>
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">{p.name}</p>
                  <p className="text-xs text-(--color-muted)">
                    {p.source_language} → {p.target_language} ·{" "}
                    {new Date(p.updated_at).toLocaleString()}
                  </p>
                </div>
                <button
                  onClick={(e) => {
                    e.preventDefault();
                    remove.mutate(p.id);
                  }}
                  className="ml-auto rounded-lg p-1.5 text-(--color-muted) opacity-0 hover:bg-(--color-panel2) hover:text-red-600 group-hover:opacity-100"
                  title="删除项目"
                >
                  <Trash2 size={15} />
                </button>
              </Link>
            ))}
          </div>

          {connecting && (
            <p className="mt-4 text-center text-xs text-(--color-muted)">
              正在连接本地服务…（首次启动需几秒）
            </p>
          )}
          {projects.error && !connecting && (
            <div className="mt-4 flex items-center gap-2 rounded-xl border border-(--color-border) bg-(--color-panel) px-4 py-2 text-xs text-(--color-muted)">
              本地服务未就绪
              <button
                onClick={() => projects.refetch()}
                className="ml-auto rounded-lg border border-(--color-border) bg-(--color-bg) px-2 py-1 hover:bg-(--color-panel2)"
              >
                重试
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
