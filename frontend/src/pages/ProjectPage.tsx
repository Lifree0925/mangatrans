/** 项目工作台：导入、解析、章节树、片段列表、解析报告。 */

import { startTransition, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, BookOpen, Check, ChevronLeft, ChevronRight, Download, FileUp, Loader2, Pencil, X, Zap } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { api } from "../lib/api";
import { chapterLabel } from "../lib/format";
import { toast } from "../lib/feedback";
import type { Chapter, ExportResult, LatestTranslation, Project, ProviderInfo, Scene, Segment, TaskInfo } from "../lib/types";


export default function ProjectPage() {
  const { id = "" } = useParams();
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [chaptersCollapsed, setChaptersCollapsed] = useState(false);
  // 片段编辑状态：editing = {segmentId, side: 'source'|'target'} | null
  const [editing, setEditing] = useState<{ segmentId: string; side: "source" | "target" } | null>(null);
  const [editDraft, setEditDraft] = useState("");
  const [editMenuFor, setEditMenuFor] = useState<string | null>(null);
  const [editMenuPos, setEditMenuPos] = useState<{ x: number; y: number } | null>(null);
  const [activeChapterId, setActiveChapterId] = useState<string | null>(null);
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const scrollRaf = useRef<number | null>(null);

  // 段锚点缓存：数据变化时计算一次 offsetTop，滚动时不再触发布局查询（性能关键）
  const segAnchorsRef = useRef<{ top: number; chapterId: string }[]>([]);

  const onBodyScroll = () => {
    if (scrollRaf.current != null) return;
    scrollRaf.current = requestAnimationFrame(() => {
      scrollRaf.current = null;
      const c = bodyRef.current;
      if (!c) return;
      const st = c.scrollTop;
      let current: string | null = null;
      for (const a of segAnchorsRef.current) {
        if (a.top >= st) {
          current = a.chapterId;
          break;
        }
      }
      startTransition(() => setActiveChapterId(current));
    });
  };
  const [exporting, setExporting] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  // 重新翻译浮层：全部重翻 / 章节多选重译
  const [retranslateOpen, setRetranslateOpen] = useState(false);
  const [retranslateChapters, setRetranslateChapters] = useState<Set<string>>(new Set());
  const [exportMode, setExportMode] = useState<"delivery" | "general">("delivery");
  const [exportTitleEn, setExportTitleEn] = useState("");
  const [exportBilingual, setExportBilingual] = useState(true);
  const [exportFormat, setExportFormat] = useState<"md" | "txt" | "docx">("md");
  const isTauri = !!(
    window as unknown as { __TAURI_INTERNALS__?: unknown }
  ).__TAURI_INTERNALS__;

  const project = useQuery({
    queryKey: ["project", id],
    queryFn: () => api<Project>(`/projects/${id}`),
  });
  const providers = useQuery({
    queryKey: ["providers"],
    queryFn: () => api<ProviderInfo[]>("/settings/providers"),
  });
  const availableProviders = providers.data?.filter((p) => p.is_verified) ?? [];
  const defaultProvider = availableProviders.find((p) => p.is_default) ?? availableProviders[0];
  const setDefaultProvider = useMutation({
    mutationFn: (pid: string) =>
      api(`/settings/providers/${pid}`, {
        method: "PATCH",
        body: JSON.stringify({ is_default: true }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["providers"] }),
  });
  const projectTasks = useQuery({
    queryKey: ["project-tasks", id],
    queryFn: () => api<TaskInfo[]>(`/projects/${id}/tasks`),
    refetchInterval: 2000,
  });
  // 仅在项目有运行中任务时轮询（数据解析/翻译完成后停止，避免每 3 秒全列表重渲染卡顿）
  const hasActiveTask = (projectTasks.data ?? []).some(
    (t) => t.status === "running" || t.status === "queued"
  );
  const chapters = useQuery({
    queryKey: ["chapters", id],
    queryFn: () => api<Chapter[]>(`/projects/${id}/chapters`),
    refetchInterval: hasActiveTask ? 3000 : false,
  });
  const scenes = useQuery({
    queryKey: ["scenes", id],
    queryFn: () => api<Scene[]>(`/projects/${id}/scenes`),
    refetchInterval: hasActiveTask ? 3000 : false,
  });
  const segments = useQuery({
    queryKey: ["segments", id],
    queryFn: () => api<Segment[]>(`/projects/${id}/segments`),
    refetchInterval: hasActiveTask ? 3000 : false,
  });

  const latestTranslations = useQuery({
    queryKey: ["latest-translations", id],
    queryFn: () => api<{ items: LatestTranslation[] }>(`/projects/${id}/translations/latest`),
    refetchInterval: hasActiveTask ? 2000 : false,
  });

  const saveEdit = async () => {
    if (!editing) return;
    try {
      if (editing.side === "source") {
        await api(`/projects/${id}/segments/${editing.segmentId}`, {
          method: "PATCH",
          body: JSON.stringify({ source_text: editDraft }),
        });
        toast.success("原文已保存，对应译文已标记「待重译」");
      } else {
        await api(`/projects/${id}/segments/${editing.segmentId}/translations`, {
          method: "POST",
          body: JSON.stringify({ target_text: editDraft }),
        });
        toast.success("译文已保存（新增校对版本）");
      }
      qc.invalidateQueries({ queryKey: ["segments", id] });
      qc.invalidateQueries({ queryKey: ["latest-translations", id] });
      setEditing(null);
    } catch (err) {
      toast.error(`保存失败：${String(err)}`);
    }
  };

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["chapters", id] });
    qc.invalidateQueries({ queryKey: ["scenes", id] });
    qc.invalidateQueries({ queryKey: ["segments", id] });
    qc.invalidateQueries({ queryKey: ["project", id] });
    qc.invalidateQueries({ queryKey: ["latest-translations", id] });
    qc.invalidateQueries({ queryKey: ["project-tasks", id] });
  };

  const upload = useMutation({
    mutationFn: async (file: File) => {
      const form = new FormData();
      form.append("file", file);
      await api(`/projects/${id}/documents/import`, { method: "POST", body: form });
    },
    onSuccess: () => {
      invalidate();
      parse.mutate(); // 导入后自动解析（解析任务与报告在任务中心展示）
    },
    onError: (err) => toast.error(`导入失败：${String(err)}`),
  });

  const update = useMutation({
    mutationFn: () => api<{ task_id: string | null; windows: number }>(`/projects/${id}/translation/update`, { method: "POST", body: "{}" }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["project-tasks", id] });
      if (r.task_id) toast.success(`已启动更新翻译（${r.windows} 个窗口）`);
      else toast.info("没有需要更新的内容");
    },
    onError: (err) => toast.error(`更新翻译失败：${String(err)}`),
  });

  const retranslate = useMutation({
    mutationFn: (chapterIds: string[] | null) =>
      api<{ task_id: string }>(`/projects/${id}/translation/start`, {
        method: "POST",
        body: JSON.stringify(chapterIds && chapterIds.length > 0 ? { chapter_ids: chapterIds } : {}),
      }),
    onSuccess: (r: { task_id?: string; windows?: number }) => {
      setRetranslateOpen(false);
      qc.invalidateQueries({ queryKey: ["project-tasks", id] });
      toast.success(r.task_id ? `重新翻译已开始（${r.windows ?? 0} 个窗口）` : "没有需要翻译的内容");
    },
    onError: (err) => toast.error(`启动重新翻译失败：${String(err)}`),
  });
  const express = useMutation({
    mutationFn: () => api<{ task_id: string }>(`/projects/${id}/translation/express`, { method: "POST", body: "{}" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["project-tasks", id] }),
    onError: (err) => toast.error(`启动一键直出失败：${String(err)}`),
  });

  const runExport = async () => {
    setExporting(true);
    try {
      const r = await api<ExportResult>(`/projects/${id}/export`, {
        method: "POST",
        body: JSON.stringify(
          exportMode === "delivery"
            ? {
                delivery: true,
                title_en: exportTitleEn.trim() || undefined,
                format: exportFormat,
              }
            : { delivery: false, bilingual: exportBilingual, format: exportFormat }
        ),
      });
      if (isTauri) {
        const { invoke } = await import("@tauri-apps/api/core");
        const saved = r.content_base64
          ? await invoke<string | null>("save_file_base64", {
              defaultName: r.filename,
              contentBase64: r.content_base64,
              defaultDir: project.data?.storage_path ?? null,
            })
          : await invoke<string | null>("save_text_file", {
              defaultName: r.filename,
              content: r.content ?? "",
              defaultDir: project.data?.storage_path ?? null,
            });
        if (saved) toast.success(`已保存：${saved}`);
      } else {
        const mime =
          r.format === "docx"
            ? "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            : r.format === "txt"
              ? "text/plain"
              : "text/markdown";
        let blob: Blob;
        if (r.content_base64) {
          const bin = atob(r.content_base64);
          const bytes = new Uint8Array(bin.length);
          for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
          blob = new Blob([bytes], { type: mime });
        } else {
          blob = new Blob([r.content ?? ""], { type: mime });
        }
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = r.filename;
        a.click();
        URL.revokeObjectURL(a.href);
        toast.success("已开始下载");
      }
      setExportOpen(false);
    } catch (err) {
      toast.error(`导出失败：${String(err)}`);
    } finally {
      setExporting(false);
    }
  };

  const parse = useMutation({
    mutationFn: () => api<{ task_id: string }>(`/projects/${id}/parse`, { method: "POST" }),
    onSuccess: () => {
      invalidate();
      qc.invalidateQueries({ queryKey: ["project-tasks", id] });
    },
    onError: (err) => toast.error(`解析失败：${String(err)}`),
  });

  const segs = segments.data ?? [];
  const trMap = useMemo(() => {
    const m = new Map<string, LatestTranslation>();
    for (const i of latestTranslations.data?.items ?? []) m.set(i.segment_id, i);
    return m;
  }, [latestTranslations.data]);
  useEffect(() => {
    const c = bodyRef.current;
    if (!c) return;
    const anchors: { top: number; chapterId: string }[] = [];
    c.querySelectorAll<HTMLElement>("[data-seg-no]").forEach((el) => {
      anchors.push({ top: el.offsetTop, chapterId: el.dataset.chapterId ?? "" });
    });
    segAnchorsRef.current = anchors;
  }, [segments.data]);

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center gap-3 border-b border-(--color-border) px-5 py-3">
        <Link to="/" className="rounded p-1 text-(--color-muted) hover:bg-(--color-panel2)">
          <ArrowLeft size={16} />
        </Link>
        {providers.data && providers.data.length > 0 ? (
          <label className="flex items-center gap-1.5 text-xs text-(--color-muted)">
            <select
              value={defaultProvider?.id ?? ""}
              onChange={(e) => setDefaultProvider.mutate(e.target.value)}
              className="rounded-xl border border-(--color-border) bg-(--color-panel) px-2 py-1 font-mono text-xs outline-none focus:border-(--color-accent)"
            >
              {providers.data.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.display_name || p.model_name}
                </option>
              ))}
            </select>
          </label>
        ) : (
          <Link to="/settings" className="text-xs text-(--color-text)">
            未配置模型 → 去设置
          </Link>
        )}
        <div className="flex flex-1 items-center justify-center overflow-hidden">
          {project.data?.name && (
            <span className="truncate text-sm font-medium" title={project.data.name}>
              {project.data.name}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {/* 1. 数据准备 */}
          <button
            onClick={() => fileRef.current?.click()}
            className="flex items-center gap-1.5 rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-1.5 text-sm hover:bg-(--color-panel2)"
            title="导入 TXT / Markdown / DOCX 剧本"
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
          {/* 2. 主操作：翻译（情境化） */}
          <span className="mx-1 h-5 w-px bg-(--color-border)" />
          {(() => {
            // busy 仅指 AI 翻译任务；解析等准备任务不占用直出按钮
            const busy =
              express.isPending ||
              update.isPending ||
              (projectTasks.data ?? []).some(
                (t) =>
                  (t.status === "running" || t.status === "queued") &&
                  (t.task_type === "translate" || t.task_type === "express")
              );
            const items = latestTranslations.data?.items ?? [];
            const staleCount = items.filter((i) => i.freshness_status === "stale").length;
            const translatedCount = items.length;
            if (busy) {
              return (
                <button
                  disabled
                  className="flex items-center gap-1.5 rounded-lg bg-(--color-accent) px-3 py-1.5 text-sm text-white opacity-50"
                >
                  <Loader2 size={15} className="animate-spin" />
                  直出中…
                </button>
              );
            }
            if (staleCount > 0) {
              return (
                <button
                  onClick={() => update.mutate()}
                  className="flex items-center gap-1.5 rounded-lg bg-(--color-accent) px-3 py-1.5 text-sm text-white hover:bg-(--color-accent-hover)"
                  title={`有 ${staleCount} 条译文待重译（原文或知识库被修改），只重译受影响内容`}
                >
                  <Zap size={15} /> 更新翻译（{staleCount}）
                </button>
              );
            }
            if (translatedCount > 0) {
              return (
                <button
                  onClick={() => setRetranslateOpen(true)}
                  className="rounded-lg border border-(--color-border) px-3 py-1.5 text-sm text-(--color-muted) hover:bg-(--color-panel2)"
                  title="重新翻译：可选全部重翻或指定章节重译"
                >
                  重新翻译
                </button>
              );
            }
            return (
              <button
                onClick={() => express.mutate()}
                className="flex items-center gap-1.5 rounded-lg bg-(--color-accent) px-3 py-1.5 text-sm text-white hover:bg-(--color-accent-hover)"
                title="自动完成整理、翻译与检查，生成完整英文剧本"
              >
                <Zap size={15} /> 一键直出
              </button>
            );
          })()}

          {/* 3. 检查与交付 */}
          <span className="mx-1 h-5 w-px bg-(--color-border)" />

          <Link
            to={`/knowledge/${id}`}
            title="查看项目知识库"
            className="flex items-center gap-1.5 rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-1.5 text-sm text-(--color-muted) hover:bg-(--color-panel2) hover:text-(--color-text)"
          >
            <BookOpen size={15} /> 知识库
          </Link>

          <button
            onClick={() => setExportOpen(true)}
            className="flex items-center gap-1.5 rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-1.5 text-sm hover:bg-(--color-panel2)"
            title="导出英文剧本或中英对照"
          >
            <Download size={15} /> 导出
          </button>
        </div>
      </div>


      <div className="flex flex-1 overflow-hidden">
        {!chaptersCollapsed && (
        <aside className="toc-hide-scroll w-[179px] shrink-0 overflow-auto p-3 text-sm">
          {chapters.data?.map((ch) => {
            const firstSegNo = segs.find((seg) => seg.chapter_id === ch.id)?.segment_no;
            return (
            <div key={ch.id} className="mb-2">
              <button
                type="button"
                onClick={() => {
                  if (!firstSegNo) return;
                  const el = document.querySelector(`[data-seg-no="${firstSegNo}"]`);
                  el?.scrollIntoView({ behavior: "smooth", block: "start" });
                }}
                className={`rounded-md px-1.5 py-0.5 text-left font-medium text-(--color-text) transition-colors hover:text-(--color-accent) ${
                  activeChapterId === ch.id ? "bg-(--color-panel2)" : ""
                }`}
                title={firstSegNo ? "点击跳转到正文" : undefined}
              >
                {chapterLabel(ch.chapter_no, ch.title)}
              </button>
              {scenes.data
                ?.filter((sc) => sc.chapter_id === ch.id)
                .map((sc) => (
                  <p key={sc.id} className="ml-2 mt-1 truncate text-xs text-(--color-muted)">
                    {sc.heading || `场景 ${sc.scene_no}`}
                  </p>
                ))}
            </div>
            );
          })}
          {!chapters.data?.length && (
            <p className="text-xs text-(--color-muted)">导入并解析后显示章节结构</p>
          )}
        </aside>
        )}
        <div
          className={`relative w-4 shrink-0 ${
            chaptersCollapsed ? "" : "border-r border-(--color-border)"
          }`}
        >
          <button
            onClick={() => setChaptersCollapsed((v) => !v)}
            title={chaptersCollapsed ? "展开章节目录" : "收起章节目录"}
            className={`absolute top-1/2 -translate-y-1/2 cursor-pointer text-(--color-muted) hover:text-(--color-accent) ${
              chaptersCollapsed ? "left-0.5" : "right-0"
            }`}
          >
            {chaptersCollapsed ? <ChevronRight size={15} /> : <ChevronLeft size={15} />}
          </button>
        </div>

        <div
          ref={bodyRef}
          onScroll={onBodyScroll}
          className="relative flex-1 overflow-auto p-4 font-mono text-[13px] leading-6"
        >
          <div className="mx-auto max-w-4xl">
            {(() => {
              // 仅当 AI 翻译任务(translate/express)处于运行中才显示状态，导入/解析等一律不显示
              const list = projectTasks.data ?? [];
              const anyTask = list.find(
                (t) =>
                  (t.status === "running" || t.status === "queued") &&
                  (t.task_type === "translate" || t.task_type === "express")
              );
              if (!anyTask) return null;
              return (
                <span className="mb-2 inline-flex items-center gap-1.5 rounded-lg bg-(--color-panel) px-2 py-1 text-xs font-medium text-(--color-accent)">
                  <Loader2 size={12} className="animate-spin" />
                  翻译中…
                </span>
              );
            })()}
            {segs.map((s) => {
              const tr = trMap.get(s.id);
              const startEdit = (side: "source" | "target") => {
                setEditing({ segmentId: s.id, side });
                setEditDraft(
                  side === "source"
                    ? s.source_text.replace(/\n$/, "")
                    : (tr?.target_text ?? "")
                );
              };
              return (
                <div
                  key={s.id}
                  data-seg-no={s.segment_no}
                  data-chapter-id={s.chapter_id ?? ""}
                  className="group border-b border-(--color-border) py-2"
                  onMouseLeave={() => {
                    if (editMenuFor === s.id) {
                      setEditMenuFor(null);
                      setEditMenuPos(null);
                    }
                  }}
                >
                  <div className="relative min-w-0 pr-8">
                      <>
                        <button
                          onClick={(e) => {
                            if (editMenuFor === s.id) {
                              setEditMenuFor(null);
                              setEditMenuPos(null);
                            } else {
                              const rect = (e.currentTarget as HTMLButtonElement).getBoundingClientRect();
                              const menuW = 50;
                              const menuH = 76;
                              // 右侧放不下时翻转到按钮左侧；下方放不下时上移
                              const x = rect.right + 6 + menuW > window.innerWidth
                                ? rect.left - menuW - 6
                                : rect.right + 6;
                              const y = rect.top + menuH > window.innerHeight
                                ? Math.max(8, rect.bottom - menuH)
                                : rect.top;
                              setEditMenuFor(s.id);
                              setEditMenuPos({ x: Math.max(8, x), y });
                            }
                          }}
                          title="编辑本段"
                          className="absolute right-1 top-0.5 hidden p-0.5 text-(--color-muted) hover:text-(--color-accent) group-hover:block"
                        >
                          <Pencil size={14} />
                        </button>
                        {editMenuFor === s.id && editMenuPos && (
                          <div
                            className="fixed z-[80] flex flex-col overflow-hidden rounded-lg border border-(--color-border) bg-(--color-bg) py-1 shadow-xl"
                            style={{ left: editMenuPos.x, top: editMenuPos.y }}
                          >
                            <button
                              onClick={() => { setEditMenuFor(null); startEdit("source"); }}
                              className="whitespace-nowrap px-3 py-1.5 text-left text-xs hover:bg-(--color-panel)"
                            >
                              原文
                            </button>
                            <button
                              onClick={() => { setEditMenuFor(null); startEdit("target"); }}
                              className="whitespace-nowrap px-3 py-1.5 text-left text-xs hover:bg-(--color-panel)"
                            >
                              译文
                            </button>
                          </div>
                        )}
                        <p className="whitespace-pre-wrap transition-colors group-hover:text-(--color-accent)">
                          {s.source_text.replace(/\n$/, "")}
                        </p>
                      </>
                  </div>
                  <div className="relative mt-0.5 min-w-0 pl-4">
                      <>
                        {tr ? (
                          <p className={`translation-fade-in whitespace-pre-wrap text-(--color-muted) transition-colors group-hover:text-(--color-accent) ${tr.freshness_status === "stale" ? "italic" : ""}`}>
                            {tr.target_text}
                            {tr.freshness_status === "stale" && (
                              <span
                                title="原文已修改，译文需重新翻译"
                                className="ml-2 rounded-md bg-amber-100 px-1.5 py-0.5 font-sans text-[11px] font-medium text-amber-700"
                              >
                                待重译
                              </span>
                            )}
                          </p>
                        ) : (
                          <p className="text-(--color-muted) transition-colors group-hover:text-(--color-accent)">—</p>
                        )}
                      </>
                  </div>
                </div>
              );
            })}
          </div>
        </div>

      </div>

      {editing && (() => {
        const seg = segs.find((x) => x.id === editing.segmentId);
        const sideLabel = editing.side === "source" ? "原文" : "译文";
        return (
          <div className="fixed inset-0 z-[90] flex items-center justify-center bg-black/30 p-6" onClick={() => setEditing(null)}>
            <div className="flex h-[520px] w-full max-w-xl flex-col rounded-2xl bg-(--color-bg) p-5 shadow-2xl" onClick={(e) => e.stopPropagation()}>
              <div className="flex shrink-0 items-center justify-between">
                <h3 className="text-base font-semibold">
                  编辑{sideLabel}
                  {seg && (
                    <span className="ml-2 font-sans text-xs font-normal text-(--color-muted)">
                      片段 {seg.segment_no} · {seg.segment_type === "dialogue" ? "对白" : seg.segment_type === "narrative" ? "叙述" : seg.segment_type}
                    </span>
                  )}
                </h3>
                <button
                  onClick={() => setEditing(null)}
                  className="rounded-lg p-1.5 text-(--color-muted) hover:bg-(--color-panel2)"
                  title="关闭"
                >
                  <X size={18} />
                </button>
              </div>
              <textarea
                value={editDraft}
                onChange={(e) => setEditDraft(e.target.value)}
                autoFocus
                className="mt-4 min-h-0 w-full flex-1 resize-none rounded-xl border border-(--color-border) bg-(--color-bg) px-3 py-2 font-mono text-[13px] leading-6 outline-none focus:border-(--color-text)"
              />
              <div className="mt-3 flex shrink-0 items-end justify-between">
                <p className="font-sans text-xs leading-5 text-(--color-muted)">
                  {editing.side === "source"
                    ? "修改原文后，该段译文将标记为「待重译」，可一键重译。"
                    : "直接修改译文将保存为新校对版本，不会被重译覆盖。"}
                </p>
                <div className="flex gap-2">
                  <button
                    onClick={() => setEditing(null)}
                    className="rounded-lg border border-(--color-border) bg-(--color-bg) px-4 py-1.5 text-sm hover:bg-(--color-panel2)"
                  >
                    取消
                  </button>
                  <button
                    onClick={saveEdit}
                    className="flex items-center gap-1 rounded-lg bg-(--color-accent) px-4 py-1.5 text-sm text-white hover:bg-(--color-accent-hover)"
                  >
                    <Check size={14} /> 保存
                  </button>
                </div>
              </div>
            </div>
          </div>
        );
      })()}

      {retranslateOpen && (() => {
        const chs = chapters.data ?? [];
        const sel = retranslateChapters;
        return (
          <div
            className="fixed inset-0 z-[65] flex items-center justify-center bg-black/30 p-6"
            onClick={() => setRetranslateOpen(false)}
          >
            <div
              className="flex h-[540px] max-h-[calc(100vh-3rem)] w-full max-w-md flex-col rounded-2xl bg-(--color-bg) p-5 shadow-2xl"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="flex items-center justify-between">
                <h3 className="text-base font-semibold">重新翻译</h3>
                <button
                  onClick={() => setRetranslateOpen(false)}
                  className="rounded-lg p-1.5 text-(--color-muted) hover:bg-(--color-panel2)"
                >
                  <X size={18} />
                </button>
              </div>

              <button
                onClick={() => {
                  setRetranslateOpen(false);
                  express.mutate();
                }}
                disabled={express.isPending}
                className="mt-4 flex items-center gap-2 rounded-xl border border-(--color-border) bg-(--color-panel) px-4 py-3 text-left text-sm hover:bg-(--color-panel2) disabled:opacity-50"
              >
                <Zap size={16} className="text-(--color-accent)" />
                <span>
                  全部重翻（完整流程）
                  <span className="mt-0.5 block text-xs font-normal text-(--color-muted)">
                    重新抽取知识库、角色档案、章节剧情卡、台词本，再全本翻译（已编辑知识不会被覆盖）
                  </span>
                </span>
              </button>

              <div className="mt-3 flex items-center justify-between text-xs text-(--color-muted)">
                <span>或按章节翻译（可多选）</span>
                <button
                  onClick={() =>
                    setRetranslateChapters(
                      sel.size === chs.length ? new Set() : new Set(chs.map((c) => c.id))
                    )
                  }
                  className="rounded px-1.5 py-0.5 hover:bg-(--color-panel)"
                >
                  {sel.size === chs.length ? "清空" : "全选"}
                </button>
              </div>
              <div className="mt-1.5 flex-1 space-y-1 overflow-auto rounded-xl border border-(--color-border) p-2">
                {chs.map((c) => (
                  <label
                    key={c.id}
                    className="flex cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 text-sm hover:bg-(--color-panel)"
                  >
                    <input
                      type="checkbox"
                      checked={sel.has(c.id)}
                      onChange={(e) => {
                        const next = new Set(sel);
                        if (e.target.checked) next.add(c.id);
                        else next.delete(c.id);
                        setRetranslateChapters(next);
                      }}
                      className="accent-(--color-accent)"
                    />
                    {chapterLabel(c.chapter_no, c.title)}
                  </label>
                ))}
              </div>

              <div className="mt-4 flex justify-end gap-2">
                <button
                  onClick={() => setRetranslateOpen(false)}
                  className="rounded-lg border border-(--color-border) bg-(--color-bg) px-4 py-1.5 text-sm hover:bg-(--color-panel2)"
                >
                  取消
                </button>
                <button
                  onClick={() => retranslate.mutate([...sel])}
                  disabled={retranslate.isPending || sel.size === 0}
                  className="rounded-lg bg-(--color-accent) px-4 py-1.5 text-sm text-white hover:bg-(--color-accent-hover) disabled:opacity-50"
                >
                  {retranslate.isPending ? "启动中…" : `翻译选中章节${sel.size > 0 ? `（${sel.size}）` : ""}`}
                </button>
              </div>
            </div>
          </div>
        );
      })()}

      {exportOpen && (
        <div
          className="fixed inset-0 z-[65] flex items-center justify-center bg-black/30 p-6"
          onClick={() => setExportOpen(false)}
        >
          <div
            className="flex h-[540px] max-h-[calc(100vh-3rem)] w-full max-w-md flex-col rounded-2xl bg-(--color-bg) p-5 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between">
              <h3 className="text-base font-semibold">导出剧本</h3>
              <button
                onClick={() => setExportOpen(false)}
                className="rounded-lg p-1.5 text-(--color-muted) hover:bg-(--color-panel2)"
              >
                <X size={18} />
              </button>
            </div>

            <div className="mt-4 flex-1 overflow-auto pr-1">
            <p className="text-xs text-(--color-muted)">导出类型</p>
            <div className="mt-2 grid grid-cols-2 gap-2">
              <button
                onClick={() => setExportMode("delivery")}
                className={`rounded-lg border px-3 py-2.5 text-sm ${
                  exportMode === "delivery"
                    ? "border-(--color-text) bg-(--color-panel) font-medium"
                    : "border-(--color-border) hover:bg-(--color-panel)"
                }`}
              >
                交付版
                <span className="mt-0.5 block text-[11px] font-normal text-(--color-muted)">
                  剧本交付模板
                </span>
              </button>
              <button
                onClick={() => setExportMode("general")}
                className={`rounded-lg border px-3 py-2.5 text-sm ${
                  exportMode === "general"
                    ? "border-(--color-text) bg-(--color-panel) font-medium"
                    : "border-(--color-border) hover:bg-(--color-panel)"
                }`}
              >
                常规导出
                <span className="mt-0.5 block text-[11px] font-normal text-(--color-muted)">
                  中英对照 / 纯英文
                </span>
              </button>
            </div>

            {exportMode === "delivery" ? (
              <>
                <p className="mt-4 text-xs text-(--color-muted)">英文剧名（可选，留空自动识别）</p>
                <input
                  value={exportTitleEn}
                  onChange={(e) => setExportTitleEn(e.target.value)}
                  placeholder="留空则使用剧本自带的英文标题"
                  className="mt-2 w-full rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-2 text-sm outline-none focus:border-(--color-text)"
                />
                <p className="mt-4 text-xs text-(--color-muted)">文件类型</p>
                <div className="mt-2 grid grid-cols-3 gap-2">
                  {[
                    { value: "md" as const, label: "Markdown", hint: ".md" },
                    { value: "txt" as const, label: "纯文本", hint: ".txt" },
                    { value: "docx" as const, label: "Word 文档", hint: ".docx" },
                  ].map((f) => (
                    <button
                      key={f.value}
                      onClick={() => setExportFormat(f.value)}
                      className={`rounded-lg border px-2 py-2.5 text-sm ${
                        exportFormat === f.value
                          ? "border-(--color-text) bg-(--color-panel) font-medium"
                          : "border-(--color-border) hover:bg-(--color-panel)"
                      }`}
                    >
                      {f.label}
                      <span className="mt-0.5 block text-[11px] font-normal text-(--color-muted)">
                        {f.hint}
                      </span>
                    </button>
                  ))}
                </div>
              </>
            ) : (
              <>
                <p className="mt-4 text-xs text-(--color-muted)">文件类型</p>
                <div className="mt-2 grid grid-cols-3 gap-2">
                  {[
                    { value: "md" as const, label: "Markdown", hint: ".md" },
                    { value: "txt" as const, label: "纯文本", hint: ".txt" },
                    { value: "docx" as const, label: "Word 文档", hint: ".docx" },
                  ].map((f) => (
                    <button
                      key={f.value}
                      onClick={() => setExportFormat(f.value)}
                      className={`rounded-lg border px-2 py-2.5 text-sm ${
                        exportFormat === f.value
                          ? "border-(--color-text) bg-(--color-panel) font-medium"
                          : "border-(--color-border) hover:bg-(--color-panel)"
                      }`}
                    >
                      {f.label}
                      <span className="mt-0.5 block text-[11px] font-normal text-(--color-muted)">
                        {f.hint}
                      </span>
                    </button>
                  ))}
                </div>

                <p className="mt-4 text-xs text-(--color-muted)">内容形式</p>
                <div className="mt-2 grid grid-cols-2 gap-2">
                  <button
                    onClick={() => setExportBilingual(true)}
                    className={`rounded-lg border px-3 py-2.5 text-sm ${
                      exportBilingual
                        ? "border-(--color-text) bg-(--color-panel) font-medium"
                        : "border-(--color-border) hover:bg-(--color-panel)"
                    }`}
                  >
                    中英对照
                    <span className="mt-0.5 block text-[11px] font-normal text-(--color-muted)">
                      中英对照
                    </span>
                  </button>
                  <button
                    onClick={() => setExportBilingual(false)}
                    className={`rounded-lg border px-3 py-2.5 text-sm ${
                      !exportBilingual
                        ? "border-(--color-text) bg-(--color-panel) font-medium"
                        : "border-(--color-border) hover:bg-(--color-panel)"
                    }`}
                  >
                    纯英文
                    <span className="mt-0.5 block text-[11px] font-normal text-(--color-muted)">
                      英文成品
                    </span>
                  </button>
                </div>
              </>
            )}

            <div className="mt-4 rounded-lg bg-(--color-panel) px-3 py-2.5 text-xs leading-5 text-(--color-muted)">
              <p>
                保存位置：
                {isTauri
                  ? "确认后将弹出系统“另存为”对话框，可选择任意磁盘位置。"
                  : "浏览器将下载到默认下载目录。"}
              </p>
              {project.data?.storage_path && (
                <p className="mt-1 break-all font-mono">项目文件夹：{project.data.storage_path}</p>
              )}
            </div>
            </div>

            <div className="mt-4 flex justify-end gap-2">
              <button
                onClick={() => setExportOpen(false)}
                className="rounded-lg border border-(--color-border) bg-(--color-bg) px-4 py-1.5 text-sm hover:bg-(--color-panel2)"
              >
                取消
              </button>
              <button
                onClick={runExport}
                disabled={exporting}
                className="rounded-lg bg-(--color-accent) px-4 py-1.5 text-sm text-white hover:bg-(--color-accent-hover) disabled:opacity-50"
              >
                {exporting ? "导出中…" : "导出"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
