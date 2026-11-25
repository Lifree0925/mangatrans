/** 项目列表：首屏立即渲染，连接状态静默处理（ChatGPT 风格）。 */

import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Folder, FolderOpen, Pencil, Plus, Search, Trash2, X } from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { confirmAction, toast } from "../lib/feedback";
import type { Project } from "../lib/types";

interface BackendDiag {
  alive: boolean;
  port: number;
  log_tail: string;
  sidecar_path: string;
  sidecar_exists: boolean;
  last_exit_code: number | null;
  reused: boolean;
}

function ProjectListSkeleton() {
  return (
    <div className="space-y-2" role="status" aria-live="polite" aria-label="加载中">
      {[0, 1, 2].map((i) => (
        <div key={i} className="flex items-center gap-3 px-4 py-3">
          <div className="h-9 w-9 shrink-0 animate-pulse rounded-lg bg-(--color-panel2)" />
          <div className="min-w-0 flex-1 space-y-2">
            <div className="h-3.5 animate-pulse rounded bg-(--color-panel2)" style={{ width: `${42 + i * 9}%` }} />
            <div className="h-2.5 animate-pulse rounded bg-(--color-panel2)" style={{ width: `${62 + i * 7}%` }} />
          </div>
        </div>
      ))}
      <p className="pt-4 text-center text-sm text-(--color-muted)">加载中···</p>
    </div>
  );
}

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

  const connecting = !projects.data && projects.isFetching;

  // 后端诊断：启动期间即向 Rust 侧查询 sidecar 状态。
  // 若进程已退出，不必等 API 重试 30 秒结束，立即展示真实启动日志。
  const backendStatus = useQuery<BackendDiag>({
    queryKey: ["backend-status"],
    queryFn: async () => {
      const { invoke } = await import("@tauri-apps/api/core");
      return await invoke<BackendDiag>("get_backend_status");
    },
    enabled: isTauri && (connecting || !!projects.error),
    refetchInterval: connecting || projects.error ? 1500 : false,
  });
  const diag = backendStatus.data ?? null;
  const refetchBackendStatus = backendStatus.refetch;
  // 只在当前仍处于“连接中/请求失败”时展示诊断，避免成功后的旧缓存错误残留。
  const backendFailed = isTauri && (connecting || !!projects.error) && !!diag && !diag.alive;
  const autoRestartAttempts = useRef(0);

  // sidecar 存在但意外退出时自动重启一次；文件缺失不做无效拉起。
  useEffect(() => {
    if (!backendFailed || diag?.alive || !diag?.sidecar_exists) return;
    if (autoRestartAttempts.current >= 1) return;
    autoRestartAttempts.current += 1;
    const timer = window.setTimeout(async () => {
      try {
        const { invoke } = await import("@tauri-apps/api/core");
        await invoke("restart_backend");
      } catch {
        /* 错误会由状态查询和日志展示 */
      }
      void qc.invalidateQueries({ queryKey: ["projects"] });
      void refetchBackendStatus();
    }, 800);
    return () => window.clearTimeout(timer);
  }, [backendFailed, diag?.alive, diag?.sidecar_exists, qc, refetchBackendStatus]);

  const restartLocalService = async () => {
    if (!isTauri || !diag || diag.alive) return true;
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("restart_backend");
      return true;
    } catch (err) {
      toast.error(`重启本地服务失败：${String(err)}`);
      return false;
    }
  };

  return (
    <div className="flex h-full flex-col">
      <div className="relative flex items-center gap-3 border-b border-(--color-border) px-6 py-4">
        <h1 className="text-lg font-semibold">项目</h1>
        <div className="absolute left-1/2 top-1/2 w-full max-w-xs -translate-x-1/2 -translate-y-1/2">
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
          <div className="space-y-2">
            {(() => {
              if (projects.data === undefined) {
                // 首屏数据未到时始终保留主体骨架与“加载中···”，
                // 即使本地服务重试失败也不能把主区域变成空白。
                return <ProjectListSkeleton />;
              }
              const q = search.trim().toLowerCase();
              const filtered = q
                ? projects.data.filter((p) => p.name.toLowerCase().includes(q))
                : projects.data;
              if (filtered.length === 0) {
                return (
                  <p className="py-8 text-center text-sm text-(--color-muted)">
                    {q ? "未找到匹配的项目" : "暂无文件"}
                  </p>
                );
              }
              return filtered.map((p) => (
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

          {(projects.error || backendFailed) && (
            <div className="mt-4 rounded-xl border border-(--color-border) bg-(--color-panel) px-4 py-3 text-xs text-(--color-muted)">
              <div className="flex items-center gap-2">
                <span>本地服务未就绪，系统会自动重试</span>
                <button
                  onClick={async () => {
                    const restarted = await restartLocalService();
                    if (restarted) await projects.refetch();
                  }}
                  className="ml-auto rounded-lg border border-(--color-border) bg-(--color-bg) px-2 py-1 hover:bg-(--color-panel2)"
                >
                  {diag && !diag.alive ? "重启服务" : "立即重试"}
                </button>
              </div>
              {diag && (
                <div className="mt-2">
                  {diag.alive ? (
                    <p className="text-emerald-600">后端进程运行中，正在初始化</p>
                  ) : diag.sidecar_exists ? (
                    <>
                      <p className="font-medium text-red-600">
                        本地服务进程已退出，正在尝试自动重启；仍失败请点击“重启服务”：
                      </p>
                      <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-(--color-bg) p-2 font-mono text-[11px] leading-4">
                        {diag.log_tail || "（日志为空）"}
                      </pre>
                    </>
                  ) : (
                    <>
                      <p className="font-medium text-red-600">
                        本地服务文件缺失，可能被杀毒软件隔离或安装不完整：
                      </p>
                      <p className="mt-1 break-all font-mono text-[11px]">{diag.sidecar_path}</p>
                      <p className="mt-1 text-red-600">请恢复该文件或重新安装软件。</p>
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
            className="flex h-[380px] max-h-[calc(100vh-3rem)] w-full max-w-md flex-col rounded-2xl bg-(--color-bg) p-5 shadow-2xl"
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

            <div className="mt-auto flex justify-end gap-2">
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
