/** 项目列表：首屏立即渲染，连接状态静默处理（ChatGPT 风格）。 */

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Folder, FolderOpen, Pencil, Plus, Search, Trash2, X } from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { confirmAction, toast } from "../lib/feedback";
import type { Project } from "../lib/types";

export default function ProjectsPage() {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [storagePath, setStoragePath] = useState("");
  const [search, setSearch] = useState("");
  const isTauri = !!(
    window as unknown as { __TAURI_INTERNALS__?: unknown }
  ).__TAURI_INTERNALS__;

  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api<Project[]>("/projects"),
    // 本地服务未就绪时每 3 秒自动重试：sidecar 启动/恢复后页面自动回到可用状态
    refetchInterval: (q) => (q.state.error ? 3000 : false),
  });

  const create = useMutation({
    mutationFn: () =>
      api<Project>("/projects", {
        method: "POST",
        body: JSON.stringify({
          name: name.trim() || "未命名项目",
          storage_path: storagePath.trim() || null,
        }),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      setName("");
      setStoragePath("");
      setCreateOpen(false);
    },
    onError: (err) => toast.error(`创建失败：${String(err)}`),
  });

  const chooseFolder = async () => {
    if (!isTauri) return;
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const dir = await invoke<string | null>("pick_directory");
      if (dir) setStoragePath(dir);
    } catch (err) {
      toast.error(`选择文件夹失败：${String(err)}`);
    }
  };

  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");

  const rename = useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) =>
      api<Project>(`/projects/${id}`, { method: "PATCH", body: JSON.stringify({ name }) }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      setRenamingId(null);
      toast.success("项目名称已更新");
    },
    onError: (err) => toast.error(`重命名失败：${String(err)}`),
  });

  const remove = useMutation({
    mutationFn: (id: string) => api<void>(`/projects/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["projects"] }),
    onError: (err) => toast.error(`删除失败：${String(err)}`),
  });

  const confirmRemove = async (p: Project) => {
    const ok = await confirmAction(
      "删除项目",
      `确定删除「${p.name}」？该项目的原文、译文、知识与任务记录将一并删除。`,
      true
    );
    if (ok) remove.mutate(p.id);
  };

  // 首次请求仍在进行（未发生过失败）时显示骨架；连接被拒后立即回到正常空状态
  const showSkeleton = projects.isLoading && projects.failureCount === 0;
  const connecting = !projects.data && projects.isFetching;

  // 后端诊断：连接失败时向 Rust 侧查询 sidecar 存活状态与日志（定位根因）
  const [diag, setDiag] = useState<{ alive: boolean; log_tail: string } | null>(null);
  useEffect(() => {
    if (!projects.error || !(window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__) {
      setDiag(null);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const { invoke } = await import("@tauri-apps/api/core");
        const st = await invoke<{ alive: boolean; log_tail: string }>("get_backend_status");
        if (!cancelled) setDiag(st);
      } catch {
        if (!cancelled) setDiag(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [projects.error]);

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-3 border-b border-(--color-border) px-6 py-4">
        <h1 className="text-lg font-semibold">项目</h1>
        <div className="relative mx-auto w-full max-w-xs">
          <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-(--color-muted)" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="搜索项目名称…"
            className="w-full rounded-lg border border-(--color-border) bg-(--color-bg) py-1.5 pl-9 pr-8 text-sm outline-none focus:border-(--color-text)"
          />
          {search && (
            <button
              onClick={() => setSearch("")}
              title="清空搜索"
              className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-0.5 text-(--color-muted) hover:bg-(--color-panel2)"
            >
              <X size={14} />
            </button>
          )}
        </div>
        <div className="ml-auto flex gap-2">
          <button
            onClick={() => setCreateOpen(true)}
            className="flex items-center gap-1.5 rounded-lg bg-(--color-accent) px-3 py-1.5 text-sm text-white hover:bg-(--color-accent-hover)"
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

          <div className="space-y-2">
            {(() => {
              const q = search.trim().toLowerCase();
              const filtered = q
                ? (projects.data ?? []).filter((p) => p.name.toLowerCase().includes(q))
                : projects.data;
              if (filtered && filtered.length === 0) {
                return <p className="py-8 text-center text-sm text-(--color-muted)">未找到匹配的项目</p>;
              }
              return filtered?.map((p) => (
              <div
                key={p.id}
                className="group flex items-center gap-3 rounded-xl px-4 py-3 hover:bg-(--color-panel)"
              >
                {renamingId === p.id ? (
                  <div className="flex min-w-0 flex-1 items-center gap-3">
                    <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-(--color-panel2)">
                      <Folder size={17} className="text-(--color-muted)" />
                    </div>
                    <div className="flex min-w-0 flex-1 items-center gap-1.5">
                      <input
                        value={renameDraft}
                        onChange={(e) => setRenameDraft(e.target.value)}
                        onKeyDown={(e) => e.key === "Enter" && renameDraft.trim() && rename.mutate({ id: p.id, name: renameDraft.trim() })}
                        autoFocus
                        className="w-full max-w-64 rounded-lg border border-(--color-accent) bg-(--color-bg) px-2 py-1 text-sm font-medium outline-none"
                      />
                      <button
                        onClick={() => renameDraft.trim() && rename.mutate({ id: p.id, name: renameDraft.trim() })}
                        className="rounded p-1 text-(--color-accent) hover:bg-(--color-panel2)"
                        title="保存"
                      >
                        <Check size={14} />
                      </button>
                      <button
                        onClick={() => setRenamingId(null)}
                        className="rounded p-1 text-(--color-muted) hover:bg-(--color-panel2)"
                        title="取消"
                      >
                        <X size={14} />
                      </button>
                    </div>
                  </div>
                ) : (
                  <Link to={`/projects/${p.id}`} className="flex min-w-0 flex-1 items-center gap-3">
                    <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-(--color-panel2)">
                      <Folder size={17} className="text-(--color-muted)" />
                    </div>
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium">{p.name}</p>
                      <p className="text-xs text-(--color-muted)">
                        {p.source_language} → {p.target_language} ·{" "}
                        {new Date(p.updated_at).toLocaleString()}
                      </p>
                      {p.storage_path && (
                        <p className="truncate font-mono text-[11px] text-(--color-muted)">
                          {p.storage_path}
                        </p>
                      )}
                    </div>
                  </Link>
                )}
                <button
                  onClick={() => {
                    setRenamingId(p.id);
                    setRenameDraft(p.name);
                  }}
                  className="rounded-lg p-1.5 text-(--color-muted) opacity-0 hover:bg-(--color-panel2) hover:text-(--color-accent) group-hover:opacity-100"
                  title="修改项目名称"
                >
                  <Pencil size={15} />
                </button>
                <button
                  onClick={() => confirmRemove(p)}
                  className="rounded-lg p-1.5 text-(--color-muted) opacity-0 hover:bg-(--color-panel2) hover:text-red-600 group-hover:opacity-100"
                  title="删除项目"
                >
                  <Trash2 size={15} />
                </button>
              </div>
              ));
            })()}
          </div>

          {connecting && <div className="mt-4" /> /* 静默等待：窗口在后端就绪后才显示，此处不出现等待字样 */}
          {projects.error && !connecting && (
            <div className="mt-4 rounded-xl border border-(--color-border) bg-(--color-panel) px-4 py-3 text-xs text-(--color-muted)">
              <div className="flex items-center gap-2">
                <span>本地服务未就绪（正在自动重试…）</span>
                <button
                  onClick={() => projects.refetch()}
                  className="ml-auto rounded-lg border border-(--color-border) bg-(--color-bg) px-2 py-1 hover:bg-(--color-panel2)"
                >
                  立即重试
                </button>
              </div>
              {diag && (
                <div className="mt-2">
                  {diag.alive ? (
                    <p className="text-emerald-600">后端进程运行中，等待服务就绪…</p>
                  ) : (
                    <>
                      <p className="font-medium text-red-600">
                        本地服务进程已退出（启动失败）。请复制以下日志反馈排查：
                      </p>
                      <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-(--color-bg) p-2 font-mono text-[11px] leading-4">
                        {diag.log_tail || "（日志为空）"}
                      </pre>
                    </>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {createOpen && (
        <div
          className="fixed inset-0 z-[65] flex items-center justify-center bg-black/30 p-6"
          onClick={() => setCreateOpen(false)}
        >
          <div
            className="w-full max-w-md rounded-2xl bg-(--color-bg) p-5 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between">
              <h3 className="text-base font-semibold">新建项目</h3>
              <button
                onClick={() => setCreateOpen(false)}
                className="rounded-lg p-1 text-(--color-muted) hover:bg-(--color-panel2)"
              >
                <X size={18} />
              </button>
            </div>

            <label className="mt-4 block text-xs text-(--color-muted)">
              项目名称
              <input
                autoFocus
                value={name}
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && create.mutate()}
                placeholder="例如：都市悬疑短剧第一季"
                className="mt-1 block w-full rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-2 text-sm outline-none focus:border-(--color-text)"
              />
            </label>

            <label className="mt-3 block text-xs text-(--color-muted)">
              项目文件夹位置
              <div className="mt-1 flex gap-2">
                <input
                  value={storagePath}
                  onChange={(e) => setStoragePath(e.target.value)}
                  placeholder={isTauri ? "未选择：使用软件默认数据目录" : "开发模式可手填绝对路径，或留空"}
                  className="block w-full rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-2 font-mono text-xs outline-none focus:border-(--color-text)"
                />
                {isTauri && (
                  <button
                    onClick={chooseFolder}
                    className="flex shrink-0 items-center gap-1 rounded-lg border border-(--color-border) bg-(--color-bg) px-2.5 py-2 text-xs hover:bg-(--color-panel2)"
                  >
                    <FolderOpen size={14} /> 选择
                  </button>
                )}
              </div>
            </label>
            <p className="mt-2 text-[11px] leading-3 text-(--color-muted)">
              原文与项目文件将保存在此文件夹；建议每个项目使用独立文件夹。留空则使用软件默认目录。
            </p>

            <div className="mt-5 flex justify-end gap-2">
              <button
                onClick={() => setCreateOpen(false)}
                className="rounded-lg border border-(--color-border) bg-(--color-bg) px-4 py-1.5 text-sm hover:bg-(--color-panel2)"
              >
                取消
              </button>
              <button
                onClick={() => create.mutate()}
                disabled={create.isPending}
                className="rounded-lg bg-(--color-accent) px-4 py-1.5 text-sm text-white hover:bg-(--color-accent-hover) disabled:opacity-50"
              >
                {create.isPending ? "创建中…" : "创建"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
