/** 知识库：角色档案（性格/说话方式）与章节剧情卡（情节/伏笔）。修改均通过浮层完成。 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Pencil, Plus, Trash2, X } from "lucide-react";
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
  const [tab, setTab] = useState<"profiles" | "storylines">("profiles");
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

      <div className="flex-1 overflow-auto p-4">
        {!active ? null : (
        <div className="mx-auto max-w-5xl">
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
    </div>
  );
}
