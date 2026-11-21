/** 知识库：角色档案（性格/说话方式）与章节剧情卡（情节/伏笔）。修改均通过浮层完成。 */

import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ChevronLeft, ChevronRight, Pencil, Plus, Trash2, X } from "lucide-react";
import { api } from "../lib/api";
import { confirmAction, toast } from "../lib/feedback";
import type { ChapterStoryline, KnowledgeEntry, Project } from "../lib/types";

type StoryCard = {
  summary: string;
  key_events: string[];
  foreshadowing: string[];
  prior_deps: string[];
  character_states: Record<string, string>;
};

const STORY_FIELDS = [
  { key: "summary", label: "摘要" },
  { key: "key_events", label: "重要情节" },
  { key: "foreshadowing", label: "伏笔（勿剧透）" },
  { key: "prior_deps", label: "前情依赖" },
] as const;

export default function KnowledgePage() {
  const qc = useQueryClient();
  const [projectId, setProjectId] = useState<string | null>(
    () => localStorage.getItem("mt-knowledge-project") || null
  );
  const [tab, setTab] = useState<"profiles" | "storylines" | "dialogue">("profiles");
  const [adding, setAdding] = useState(false);
  const [addForm, setAddForm] = useState({ canonical_key: "", translation: "" });

  // 角色编辑浮层
  const [editingChar, setEditingChar] = useState<KnowledgeEntry | null>(null);
  const [charDraft, setCharDraft] = useState({ translation: "", personality: "", speech_style: "", forms_of_address: "", emotional_range: "" });

  // 剧情卡编辑浮层
  const [editingStory, setEditingStory] = useState<ChapterStoryline | null>(null);
  const [storyDraft, setStoryDraft] = useState<StoryCard>({ summary: "", key_events: [], foreshadowing: [], prior_deps: [], character_states: {} });
  const [storyActiveField, setStoryActiveField] = useState<(typeof STORY_FIELDS)[number]["key"]>("summary");
  const [storyFieldText, setStoryFieldText] = useState("");

  // 台词编辑：复用项目页编辑模式（铅笔 → 原文/译文 → 浮层）
  const [editingLine, setEditingLine] = useState<{ line: DialogueLineInfo; side: "source" | "target" } | null>(null);
  const [lineDraft, setLineDraft] = useState({ speaker_name: "", source_text: "", target_text: "" });
  const [lineMenuFor, setLineMenuFor] = useState<string | null>(null);
  const [lineMenuPos, setLineMenuPos] = useState<{ x: number; y: number } | null>(null);
  // 台词本目录栏（可收起）与滚动高亮
  const [dialogueCollapsed, setDialogueCollapsed] = useState(false);
  const [dialogueActiveChapter, setDialogueActiveChapter] = useState<string | null>(null);
  const dialogueBodyRef = useRef<HTMLDivElement | null>(null);

  const projects = useQuery({ queryKey: ["projects"], queryFn: () => api<Project[]>("/projects") });
  // 默认不展示内容：未选择项目时显示"请选择项目"
  const active = projectId;

  const chooseProject = (id: string) => {
    setProjectId(id || null);
    if (id) localStorage.setItem("mt-knowledge-project", id);
    else localStorage.removeItem("mt-knowledge-project");
  };

  const characters = useQuery({
    queryKey: ["knowledge", active],
    queryFn: () => api<KnowledgeEntry[]>(`/projects/${active}/knowledge`),
    enabled: !!active && tab === "profiles",
    refetchInterval: 8000,
  });
  const storylines = useQuery({
    queryKey: ["storylines", active],
    queryFn: () => api<ChapterStoryline[]>(`/projects/${active}/storylines`),
    enabled: !!active && tab === "storylines",
    refetchInterval: 8000,
  });

  const patchCharacter = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Record<string, unknown> }) =>
      api(`/projects/${active}/knowledge/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
    onSuccess: (r: unknown) => {
      const result = r as { stale_translations?: number };
      qc.invalidateQueries({ queryKey: ["knowledge", active] });
      setEditingChar(null);
      toast.success(result.stale_translations ? `已保存，${result.stale_translations} 条译文标记待重译` : "已保存");
    },
    onError: (err) => toast.error(`保存失败：${String(err)}`),
  });

  const removeCharacter = useMutation({
    mutationFn: (id: string) => api(`/projects/${active}/knowledge/${id}`, { method: "DELETE" }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["knowledge", active] }); toast.success("已删除"); },
    onError: (err) => toast.error(`删除失败：${String(err)}`),
  });

  const addCharacter = useMutation({
    mutationFn: () => api(`/projects/${active}/knowledge`, {
      method: "POST",
      body: JSON.stringify({ item_type: "character", canonical_key: addForm.canonical_key.trim(), translation: addForm.translation.trim() }),
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["knowledge", active] });
      setAdding(false);
      setAddForm({ canonical_key: "", translation: "" });
      toast.success("角色已添加");
    },
    onError: (err) => toast.error(`添加失败：${String(err)}`),
  });

  const patchStoryline = useMutation({
    mutationFn: ({ id, card }: { id: string; card: StoryCard }) =>
      api(`/projects/${active}/storylines/${id}`, { method: "PATCH", body: JSON.stringify({ card }) }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["storylines", active] });
      setEditingStory(null);
      toast.success("剧情卡已保存");
    },
    onError: (err) => toast.error(`保存失败：${String(err)}`),
  });

  const dialogueLines = useQuery({
    queryKey: ["dialogue-lines", active],
    queryFn: () => api<DialogueLinesResponse>(`/projects/${active}/dialogue-lines`),
    enabled: !!active && tab === "dialogue",
    refetchInterval: 10000,
  });
  const patchDialogueLine = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Record<string, unknown> }) =>
      api(`/projects/${active}/dialogue-lines/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
    onSuccess: (r: unknown) => {
      const res = r as { wrote_back?: boolean; warning?: string | null };
      qc.invalidateQueries({ queryKey: ["dialogue-lines", active] });
      setEditingLine(null);
      if (res.wrote_back) toast.success("已保存，片段原文已同步更新，译文标记待重译");
      else if (res.warning) toast.success("已保存（" + res.warning + "）");
      else toast.success("已保存");
    },
    onError: (err) => toast.error(`保存失败：${String(err)}`),
  });

  const patchSegmentTranslation = useMutation({
    mutationFn: ({ segmentId, targetText }: { segmentId: string; targetText: string }) =>
      api(`/projects/${active}/segments/${segmentId}/translations`, {
        method: "POST",
        body: JSON.stringify({ target_text: targetText }),
      }),
    onSuccess: () => {
      toast.success("译文已保存（新增校对版本）");
      qc.invalidateQueries({ queryKey: ["dialogue-lines", active] });
      qc.invalidateQueries({ queryKey: ["segments", active] });
      qc.invalidateQueries({ queryKey: ["latest-translations", active] });
      setEditingLine(null);
    },
    onError: (err) => toast.error(`译文保存失败：${String(err)}`),
  });

  const parseStory = (json: string): StoryCard => {
    try { return JSON.parse(json); } catch { return { summary: "", key_events: [], foreshadowing: [], prior_deps: [], character_states: {} }; }
  };

  const confirmRemove = async (name: string, id: string) => {
    const ok = await confirmAction("删除角色档案", `确定删除「${name}」？相关译文会标记待重译。`, true);
    if (ok) removeCharacter.mutate(id);
  };

  const openCharEdit = (k: KnowledgeEntry) => {
    const data = (() => { try { return JSON.parse(k.data_json || "{}"); } catch { return {}; } })();
    setEditingChar(k);
    setCharDraft({
      translation: data.translation || "",
      personality: data.personality || "",
      speech_style: data.speech_style || "",
      forms_of_address: (data.forms_of_address || []).join(" / "),
      emotional_range: data.emotional_range || "",
    });
  };

  const saveCharEdit = () => {
    if (!editingChar) return;
    const data = (() => { try { return JSON.parse(editingChar.data_json || "{}"); } catch { return {}; } })();
    patchCharacter.mutate({
      id: editingChar.id,
      body: {
        data: {
          ...data,
          translation: charDraft.translation.trim(),
          personality: charDraft.personality.trim(),
          speech_style: charDraft.speech_style.trim(),
          forms_of_address: charDraft.forms_of_address.split("/").map((x) => x.trim()).filter(Boolean),
          emotional_range: charDraft.emotional_range.trim(),
        },
      },
    });
  };

  const openStoryEdit = (s: ChapterStoryline) => {
    setEditingStory(s);
    setStoryDraft(parseStory(s.card_json));
    setStoryActiveField("summary");
    setStoryFieldText(parseStory(s.card_json).summary);
  };

  const switchStoryField = (f: (typeof STORY_FIELDS)[number]["key"]) => {
    if (!storyDraft) return;
    setStoryActiveField(f);
    setStoryFieldText(f === "summary" ? storyDraft.summary : (storyDraft[f] as string[]).join("\n"));
  };

  const applyStoryField = () => {
    if (!storyDraft) return;
    const next: StoryCard = { ...storyDraft };
    if (storyActiveField === "summary") {
      next.summary = storyFieldText.trim();
    } else {
      (next as unknown as Record<string, string[]>)[storyActiveField] =
        storyFieldText.split("\n").map((l) => l.trim()).filter(Boolean);
    }
    setStoryDraft(next);
  };

  const saveStoryEdit = () => {
    applyStoryField();
    if (editingStory && storyDraft) {
      patchStoryline.mutate({ id: editingStory.id, card: storyDraft });
    }
  };

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-3 border-b border-(--color-border) px-6 py-4">
        <h1 className="text-lg font-semibold">知识库</h1>
        <select
          value={active ?? ""}
          onChange={(e) => chooseProject(e.target.value)}
          className="rounded-lg border border-(--color-border) bg-(--color-bg) px-2 py-1 text-sm outline-none focus:border-(--color-accent)"
        >
          <option value="">请选择项目</option>
          {(projects.data ?? []).map((p) => (
            <option key={p.id} value={p.id}>{p.name}</option>
          ))}
        </select>
        <div className="ml-2 flex items-center gap-1.5">
          <button
            onClick={() => setTab("profiles")}
            className={`rounded-lg px-3 py-1 text-sm ${tab === "profiles" ? "bg-(--color-panel2) font-medium" : "text-(--color-muted) hover:bg-(--color-panel)"}`}
          >
            角色档案
          </button>
          <button
            onClick={() => setTab("storylines")}
            className={`rounded-lg px-3 py-1 text-sm ${tab === "storylines" ? "bg-(--color-panel2) font-medium" : "text-(--color-muted) hover:bg-(--color-panel)"}`}
          >
            章节剧情
          </button>
          <button
            onClick={() => setTab("dialogue")}
            className={`rounded-lg px-3 py-1 text-sm ${tab === "dialogue" ? "bg-(--color-panel2) font-medium" : "text-(--color-muted) hover:bg-(--color-panel)"}`}
          >
            台词本
          </button>
        </div>

        {tab === "profiles" && (
          <button
            onClick={() => setAdding((v) => !v)}
            className="ml-auto flex items-center gap-1.5 rounded-lg bg-(--color-accent) px-3 py-1.5 text-sm text-white hover:bg-(--color-accent-hover)"
          >
            <Plus size={15} /> 添加角色
          </button>
        )}
      </div>

      <div className={tab === "dialogue" && active ? "flex-1 overflow-hidden" : "flex-1 overflow-auto p-4"}>
        {!active ? null : (
        <div className={tab === "dialogue" ? "h-full" : "mx-auto max-w-5xl"}>
          {tab === "profiles" && (
            <>
              {adding && (
                <div className="mb-3 flex items-center gap-2 rounded-xl border border-(--color-accent) bg-(--color-panel) px-4 py-2.5">
                  <input
                    value={addForm.canonical_key}
                    onChange={(e) => setAddForm((f) => ({ ...f, canonical_key: e.target.value }))}
                    placeholder="角色名（如：林风）"
                    className="w-52 rounded-lg border border-(--color-border) bg-(--color-bg) px-2 py-1 text-sm outline-none focus:border-(--color-accent)"
                  />
                  <input
                    value={addForm.translation}
                    onChange={(e) => setAddForm((f) => ({ ...f, translation: e.target.value }))}
                    placeholder="译名（可留空）"
                    className="w-52 rounded-lg border border-(--color-border) bg-(--color-bg) px-2 py-1 text-sm outline-none focus:border-(--color-accent)"
                  />
                  <button onClick={() => addCharacter.mutate()} disabled={!addForm.canonical_key.trim()}
                    className="flex items-center gap-1 rounded-lg bg-(--color-accent) px-2.5 py-1 text-xs text-white disabled:opacity-40">
                    <Check size={13} /> 保存
                  </button>
                  <button onClick={() => setAdding(false)} className="rounded-lg border border-(--color-border) px-2.5 py-1 text-xs hover:bg-(--color-panel2)">取消</button>
                </div>
              )}
              <table className="w-full border-collapse text-sm">
                <thead>
                  <tr className="border-b border-(--color-border) text-left text-xs text-(--color-muted)">
                    <th className="px-3 py-2 font-medium">角色</th>
                    <th className="px-3 py-2 font-medium">译名</th>
                    <th className="px-3 py-2 font-medium">性格特征</th>
                    <th className="px-3 py-2 font-medium">说话方式</th>
                    <th className="w-24 px-3 py-2 font-medium">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {(characters.data ?? []).filter((k) => k.item_type === "character").map((k) => {
                    const data = (() => { try { return JSON.parse(k.data_json || "{}"); } catch { return {}; } })();
                    return (
                      <tr key={k.id} className="border-b border-(--color-border) hover:bg-(--color-panel)">
                        <td className="px-3 py-1.5 font-medium">{k.canonical_key}</td>
                        <td className="px-3 py-1.5">
                          {data.translation ? <span className="font-mono text-xs text-(--color-accent)">{data.translation}</span> : <span className="text-(--color-muted)">—</span>}
                        </td>
                        <td className="max-w-64 truncate px-3 py-1.5">{data.personality || <span className="text-(--color-muted)">—</span>}</td>
                        <td className="max-w-64 truncate px-3 py-1.5">{data.speech_style || <span className="text-(--color-muted)">—</span>}</td>
                        <td className="px-3 py-1.5">
                          <div className="flex items-center gap-1">
                            <button onClick={() => openCharEdit(k)}
                              className="rounded p-1.5 text-(--color-muted) hover:text-(--color-accent)" title="编辑角色档案">
                              <Pencil size={14} />
                            </button>
                            <button onClick={() => confirmRemove(k.canonical_key, k.id)}
                              className="rounded p-1.5 text-(--color-muted) hover:text-red-600" title="删除角色">
                              <Trash2 size={14} />
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {(characters.data ?? []).filter((k) => k.item_type === "character").length === 0 && (
                <p className="py-8 text-center text-sm text-(--color-muted)">暂无角色档案（一键直出会自动提取，或手动添加角色）</p>
              )}
            </>
          )}

          {tab === "storylines" && (
            <div className="space-y-3">
              {(storylines.data ?? []).map((s) => {
                const card = parseStory(s.card_json);
                return (
                  <div key={s.id} className="rounded-xl border border-(--color-border) bg-(--color-panel) px-4 py-3">
                    <div className="flex items-center gap-2">
                      <span className="font-semibold">第 {s.chapter_no} 章</span>
                      {s.chapter_title && <span className="text-sm text-(--color-muted)">{s.chapter_title}</span>}
                      <button
                        onClick={() => openStoryEdit(s)}
                        className="ml-auto flex items-center gap-1 rounded-lg border border-(--color-border) px-2.5 py-1 text-xs hover:bg-(--color-panel2)"
                      >
                        <Pencil size={13} /> 编辑
                      </button>
                    </div>
                    <div className="mt-2 grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
                      <p className="col-span-2 text-(--color-muted)">{card.summary || "（无摘要）"}</p>
                      <div>
                        <p className="text-xs font-medium text-(--color-muted)">重要情节</p>
                        <ul className="mt-1 list-disc space-y-0.5 pl-4">
                          {card.key_events.map((e, i) => <li key={i}>{e}</li>)}
                          {card.key_events.length === 0 && <li className="text-(--color-muted)">—</li>}
                        </ul>
                      </div>
                      <div>
                        <p className="text-xs font-medium text-amber-600">伏笔（翻译时勿剧透）</p>
                        <ul className="mt-1 list-disc space-y-0.5 pl-4">
                          {card.foreshadowing.map((e, i) => <li key={i}>{e}</li>)}
                          {card.foreshadowing.length === 0 && <li className="text-(--color-muted)">—</li>}
                        </ul>
                        <p className="mt-2 text-xs font-medium text-(--color-muted)">前情依赖</p>
                        <ul className="mt-1 list-disc space-y-0.5 pl-4">
                          {card.prior_deps.map((e, i) => <li key={i}>{e}</li>)}
                          {card.prior_deps.length === 0 && <li className="text-(--color-muted)">—</li>}
                        </ul>
                      </div>
                    </div>
                  </div>
                );
              })}
              {(storylines.data ?? []).length === 0 && (
                <p className="py-8 text-center text-sm text-(--color-muted)">暂无剧情卡（一键直出会自动生成）</p>
              )}
            </div>
          )}

          {tab === "dialogue" && (
          <div className="flex h-full">
            {/* 章节目录（可收起） */}
            {!dialogueCollapsed && (
              <div className="w-56 shrink-0 overflow-auto border-r-0 py-3">
                <p className="mb-1 px-3 text-xs font-medium text-(--color-muted)">章节目录</p>
                {(dialogueLines.data?.chapters ?? []).map((ch) => (
                  <button
                    key={ch.chapter_id}
                    onClick={() => {
                      const el = dialogueBodyRef.current?.querySelector<HTMLElement>(`[data-dlg-chapter="${ch.chapter_id}"]`);
                      el?.scrollIntoView({ behavior: "smooth", block: "start" });
                    }}
                    className={`block w-full truncate rounded-lg px-3 py-1.5 text-left text-sm transition-colors hover:text-(--color-accent) ${
                      dialogueActiveChapter === ch.chapter_id
                        ? "font-medium text-(--color-accent)"
                        : "text-(--color-text)"
                    }`}
                    title={ch.chapter_title ? String(ch.chapter_title).replace(/^#\s*第\d+回\s*/, "") : `第 ${ch.chapter_no ?? "?"} 章`}
                  >
                    第 {ch.chapter_no ?? "?"} 章
                    <span className="ml-1.5 text-xs text-(--color-muted)">
                      {ch.chapter_title ? String(ch.chapter_title).replace(/^#\s*第\d+回\s*/, "").slice(0, 12) : ""}
                    </span>
                  </button>
                ))}
                {(dialogueLines.data?.chapters ?? []).length === 0 && (
                  <p className="px-3 py-4 text-xs text-(--color-muted)">
                    暂无台词本。一键直出时会自动抽取章节 → 场景 → 角色的纯对白流水并逐句对齐译文。
                  </p>
                )}
              </div>
            )}
            {/* 收展箭头：贴线、无圆框 */}
            <div
              className={`relative w-4 shrink-0 cursor-pointer ${dialogueCollapsed ? "" : "border-r border-(--color-border)"}`}
              onClick={() => setDialogueCollapsed((v) => !v)}
              title={dialogueCollapsed ? "展开章节目录" : "收起章节目录"}
            >
              <span
                className={`absolute top-1/2 -translate-y-1/2 text-(--color-muted) hover:text-(--color-accent) ${
                  dialogueCollapsed ? "left-0.5" : "right-0"
                }`}
              >
                {dialogueCollapsed ? <ChevronRight size={15} /> : <ChevronLeft size={15} />}
              </span>
            </div>
            {/* 内容区：章节 → 场景 → 角色纯对白（上下中英对照，编辑复用项目页模式） */}
            <div
              ref={dialogueBodyRef}
              onScroll={() => {
                const container = dialogueBodyRef.current;
                if (!container) return;
                const top = container.getBoundingClientRect().top;
                let current: string | null = null;
                for (const el of container.querySelectorAll<HTMLElement>("[data-dlg-chapter]")) {
                  if (el.getBoundingClientRect().top >= top - 4) {
                    current = el.dataset.dlgChapter ?? null;
                    break;
                  }
                }
                setDialogueActiveChapter(current);
              }}
              className="flex-1 overflow-auto p-4 font-mono text-[13px] leading-6"
            >
              {(dialogueLines.data?.chapters ?? []).map((ch) => (
                <div key={ch.chapter_id} data-dlg-chapter={ch.chapter_id} className="mx-auto max-w-4xl">
                  <h3 className="mb-1 mt-2 text-base font-bold">
                    第 {ch.chapter_no ?? "?"} 章
                    {ch.chapter_title && (
                      <span className="ml-2 text-sm font-normal text-(--color-muted)">
                        {String(ch.chapter_title).replace(/^#\s*第\d+回\s*/, "")}
                      </span>
                    )}
                  </h3>
                  {ch.scenes.map((sc) => (
                    <div key={sc.scene_id} className="mb-3">
                      {sc.scene_heading && (
                        <p className="mb-1 mt-3 text-xs text-(--color-muted)">
                          {String(sc.scene_heading).replace(/^#\s*/, "")}
                        </p>
                      )}
                      {sc.lines.map((ln) => (
                        <div
                          key={ln.id}
                          className="group relative border-b border-(--color-border) py-2 pr-8"
                          onMouseLeave={() => {
                            if (lineMenuFor === ln.id) {
                              setLineMenuFor(null);
                              setLineMenuPos(null);
                            }
                          }}
                        >
                          <button
                            onClick={(e) => {
                              if (lineMenuFor === ln.id) {
                                setLineMenuFor(null);
                                setLineMenuPos(null);
                              } else {
                                const rect = (e.currentTarget as HTMLButtonElement).getBoundingClientRect();
                                const menuW = 50;
                                const menuH = 76;
                                const x = rect.right + 6 + menuW > window.innerWidth
                                  ? rect.left - menuW - 6
                                  : rect.right + 6;
                                const y = rect.top + menuH > window.innerHeight
                                  ? Math.max(8, rect.bottom - menuH)
                                  : rect.top;
                                setLineMenuFor(ln.id);
                                setLineMenuPos({ x: Math.max(8, x), y });
                              }
                            }}
                            title="编辑本行"
                            className="absolute right-1 top-0.5 hidden p-0.5 text-(--color-muted) hover:text-(--color-accent) group-hover:block"
                          >
                            <Pencil size={14} />
                          </button>
                          {lineMenuFor === ln.id && lineMenuPos && (
                            <div
                              className="fixed z-[80] flex flex-col overflow-hidden rounded-lg border border-(--color-border) bg-(--color-bg) py-1 shadow-xl"
                              style={{ left: lineMenuPos.x, top: lineMenuPos.y }}
                            >
                              <button
                                onClick={() => {
                                  setLineMenuFor(null);
                                  setEditingLine({ line: ln, side: "source" });
                                  setLineDraft({ speaker_name: ln.speaker_name, source_text: ln.source_text, target_text: "" });
                                }}
                                className="whitespace-nowrap px-3 py-1.5 text-left text-xs hover:bg-(--color-panel)"
                              >
                                原文
                              </button>
                              <button
                                onClick={() => {
                                  setLineMenuFor(null);
                                  setEditingLine({ line: ln, side: "target" });
                                  setLineDraft({ speaker_name: ln.speaker_name, source_text: ln.source_text, target_text: ln.segment_translation ?? "" });
                                }}
                                className="whitespace-nowrap px-3 py-1.5 text-left text-xs hover:bg-(--color-panel)"
                              >
                                译文
                              </button>
                            </div>
                          )}
                          {/* 原文行 */}
                          <p className="whitespace-pre-wrap transition-colors group-hover:text-(--color-accent)">
                            <span className="mr-2 font-medium text-(--color-accent)">
                              {ln.speaker_name}
                              {ln.speaker_alias && ln.speaker_alias !== ln.speaker_name && (
                                <span className="ml-1 font-normal text-(--color-muted)">({ln.speaker_alias})</span>
                              )}
                              ：
                            </span>
                            {ln.source_text}
                          </p>
                          {/* 译文行：逐句对齐译文优先，无则回退片段译文 */}
                          <p className="mt-0.5 whitespace-pre-wrap pl-4 text-(--color-muted) transition-colors group-hover:text-(--color-accent)">
                            {ln.target_text ?? ln.segment_translation ?? "—"}
                          </p>
                        </div>
                      ))}
                    </div>
                  ))}
                </div>
              ))}
              {(dialogueLines.data?.chapters ?? []).length === 0 && (
                <p className="py-8 text-center text-sm text-(--color-muted)">
                  暂无台词本。一键直出时会自动抽取章节 → 场景 → 角色的纯对白流水并逐句对齐译文。
                </p>
              )}
            </div>
          </div>
          )}
        </div>
        )}
      </div>

      {/* 角色档案编辑浮层 */}
      {editingChar && (
        <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/30 p-6" onClick={() => setEditingChar(null)}>
          <div className="w-full max-w-lg rounded-2xl bg-(--color-bg) p-5 shadow-2xl" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between">
              <h2 className="text-base font-semibold">编辑角色档案 · {editingChar.canonical_key}</h2>
              <button onClick={() => setEditingChar(null)} className="rounded-lg p-1.5 text-(--color-muted) hover:bg-(--color-panel2)"><X size={16} /></button>
            </div>
            <div className="mt-4 space-y-3">
              <label className="block text-xs text-(--color-muted)">
                译名
                <input value={charDraft.translation} onChange={(e) => setCharDraft((d) => ({ ...d, translation: e.target.value }))}
                  placeholder="如：Lin Feng"
                  className="mt-1 block w-full rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-1.5 font-mono text-sm outline-none focus:border-(--color-accent)" />
              </label>
              <label className="block text-xs text-(--color-muted)">
                性格特征
                <textarea value={charDraft.personality} onChange={(e) => setCharDraft((d) => ({ ...d, personality: e.target.value }))} rows={3}
                  placeholder="如：沉稳内敛，重情重义，遇事先谋后动"
                  className="mt-1 block w-full resize-y rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-1.5 text-sm outline-none focus:border-(--color-accent)" />
              </label>
              <label className="block text-xs text-(--color-muted)">
                说话方式
                <textarea value={charDraft.speech_style} onChange={(e) => setCharDraft((d) => ({ ...d, speech_style: e.target.value }))} rows={3}
                  placeholder="如：短句为主，语气冷静克制，极少寒暄，多用反问"
                  className="mt-1 block w-full resize-y rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-1.5 text-sm outline-none focus:border-(--color-accent)" />
              </label>
              <label className="block text-xs text-(--color-muted)">
                称呼习惯（多个用 / 分隔）
                <input value={charDraft.forms_of_address} onChange={(e) => setCharDraft((d) => ({ ...d, forms_of_address: e.target.value }))}
                  placeholder="如：大哥 / 玄德"
                  className="mt-1 block w-full rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-1.5 text-sm outline-none focus:border-(--color-accent)" />
              </label>
              <label className="block text-xs text-(--color-muted)">
                情绪区间
                <input value={charDraft.emotional_range} onChange={(e) => setCharDraft((d) => ({ ...d, emotional_range: e.target.value }))}
                  placeholder="如：冷静—激昂（受激时）"
                  className="mt-1 block w-full rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-1.5 text-sm outline-none focus:border-(--color-accent)" />
              </label>
            </div>
            <div className="mt-5 flex justify-end gap-2">
              <button onClick={() => setEditingChar(null)} className="rounded-lg border border-(--color-border) bg-(--color-bg) px-4 py-1.5 text-sm hover:bg-(--color-panel2)">取消</button>
              <button onClick={saveCharEdit} disabled={patchCharacter.isPending}
                className="rounded-lg bg-(--color-accent) px-4 py-1.5 text-sm text-white hover:bg-(--color-accent-hover) disabled:opacity-50">
                {patchCharacter.isPending ? "保存中…" : "保存"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 章节剧情卡编辑浮层 */}
      {editingStory && (
        <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/30 p-6" onClick={() => setEditingStory(null)}>
          <div className="flex max-h-[85vh] w-full max-w-2xl flex-col rounded-2xl bg-(--color-bg) p-5 shadow-2xl" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between">
              <h2 className="text-base font-semibold">
                编辑剧情卡 · 第 {editingStory.chapter_no} 章 {editingStory.chapter_title}
              </h2>
              <button onClick={() => setEditingStory(null)} className="rounded-lg p-1.5 text-(--color-muted) hover:bg-(--color-panel2)"><X size={16} /></button>
            </div>
            <div className="mb-2 mt-4 flex gap-1.5">
              {STORY_FIELDS.map((f) => (
                <button
                  key={f.key}
                  onClick={() => { applyStoryField(); switchStoryField(f.key); }}
                  className={`rounded-lg px-3 py-1 text-sm ${storyActiveField === f.key ? "bg-(--color-panel2) font-medium" : "text-(--color-muted) hover:bg-(--color-panel)"}`}
                >
                  {f.label}
                </button>
              ))}
            </div>
            <textarea
              value={storyFieldText}
              onChange={(e) => setStoryFieldText(e.target.value)}
              rows={storyActiveField === "summary" ? 4 : 10}
              placeholder={storyActiveField === "summary" ? "本章内容摘要" : "每行一条"}
              className="flex-1 w-full resize-y rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-2 text-sm leading-6 outline-none focus:border-(--color-accent)"
            />
            <div className="mt-4 flex justify-end gap-2">
              <button onClick={() => setEditingStory(null)} className="rounded-lg border border-(--color-border) bg-(--color-bg) px-4 py-1.5 text-sm hover:bg-(--color-panel2)">取消</button>
              <button onClick={saveStoryEdit} disabled={patchStoryline.isPending}
                className="rounded-lg bg-(--color-accent) px-4 py-1.5 text-sm text-white hover:bg-(--color-accent-hover) disabled:opacity-50">
                {patchStoryline.isPending ? "保存中…" : "保存"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 台词编辑浮层（复用项目页模式：原文 / 译文） */}
      {editingLine && (
        <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/30 p-6" onClick={() => setEditingLine(null)}>
          <div className="w-full max-w-2xl rounded-2xl bg-(--color-bg) p-5 shadow-2xl" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between">
              <h2 className="text-base font-semibold">
                编辑{editingLine.side === "source" ? "原文" : "译文"}
                <span className="ml-2 font-sans text-xs font-normal text-(--color-muted)">
                  {editingLine.line.speaker_name} · 第 {editingLine.line.chapter_no ?? "?"} 章
                </span>
              </h2>
              <button onClick={() => setEditingLine(null)} className="rounded-lg p-1.5 text-(--color-muted) hover:bg-(--color-panel2)"><X size={16} /></button>
            </div>
            {editingLine.side === "source" ? (
              <>
                <p className="mt-3 text-xs text-(--color-muted)">修改台词文本会同步回写片段原文，该段译文将标记为待重译。</p>
                <div className="mt-3 space-y-3">
                  <label className="block text-xs text-(--color-muted)">
                    说话人
                    <input value={lineDraft.speaker_name} onChange={(e) => setLineDraft((d) => ({ ...d, speaker_name: e.target.value }))}
                      className="mt-1 block w-full rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-1.5 text-sm outline-none focus:border-(--color-accent)" />
                  </label>
                  <label className="block text-xs text-(--color-muted)">
                    台词文本
                    <textarea value={lineDraft.source_text} onChange={(e) => setLineDraft((d) => ({ ...d, source_text: e.target.value }))} rows={4}
                      className="mt-1 block w-full resize-y rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-1.5 text-sm outline-none focus:border-(--color-accent)" />
                  </label>
                </div>
                <div className="mt-4 flex justify-end gap-2">
                  <button onClick={() => setEditingLine(null)} className="rounded-lg border border-(--color-border) px-4 py-1.5 text-sm hover:bg-(--color-panel2)">取消</button>
                  <button
                    onClick={() => patchDialogueLine.mutate({
                      id: editingLine.line.id,
                      body: { speaker_name: lineDraft.speaker_name, source_text: lineDraft.source_text },
                    })}
                    disabled={patchDialogueLine.isPending}
                    className="rounded-lg bg-(--color-accent) px-4 py-1.5 text-sm text-white hover:bg-(--color-accent-hover) disabled:opacity-50"
                  >
                    {patchDialogueLine.isPending ? "保存中…" : "保存"}
                  </button>
                </div>
              </>
            ) : (
              <>
                <p className="mt-3 text-xs text-(--color-muted)">
                  本行原文：{editingLine.line.source_text}
                  <br />
                  修改的是该行所属片段的整段译文，保存后作为新校对版本，不会被重译覆盖。
                </p>
                <textarea value={lineDraft.target_text} onChange={(e) => setLineDraft((d) => ({ ...d, target_text: e.target.value }))} rows={8}
                  className="mt-3 block w-full resize-y rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-1.5 font-mono text-[13px] leading-6 outline-none focus:border-(--color-accent)" />
                <div className="mt-4 flex justify-end gap-2">
                  <button onClick={() => setEditingLine(null)} className="rounded-lg border border-(--color-border) px-4 py-1.5 text-sm hover:bg-(--color-panel2)">取消</button>
                  <button
                    onClick={() => {
                      if (editingLine.line.segment_id) {
                        patchSegmentTranslation.mutate({ segmentId: editingLine.line.segment_id, targetText: lineDraft.target_text });
                      }
                    }}
                    disabled={patchSegmentTranslation.isPending || !editingLine.line.segment_id}
                    className="rounded-lg bg-(--color-accent) px-4 py-1.5 text-sm text-white hover:bg-(--color-accent-hover) disabled:opacity-50"
                  >
                    {patchSegmentTranslation.isPending ? "保存中…" : "保存"}
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
