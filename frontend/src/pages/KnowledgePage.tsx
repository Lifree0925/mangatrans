/** 知识库：角色档案（性格/说话方式）与章节剧情卡（情节/伏笔），翻译质量的核心依据。 */

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

export default function KnowledgePage() {
  const qc = useQueryClient();
  const [projectId, setProjectId] = useState<string | null>(null);
  const [tab, setTab] = useState<"profiles" | "storylines">("profiles");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [draftField, setDraftField] = useState<"translation" | "personality" | "speech_style" | "forms_of_address" | "emotional_range">("translation");
  const [adding, setAdding] = useState(false);
  const [addForm, setAddForm] = useState({ canonical_key: "", translation: "" });
  const [editingStoryId, setEditingStoryId] = useState<string | null>(null);
  const [storyDraft, setStoryDraft] = useState<StoryCard | null>(null);
  const [storyDraftField, setStoryDraftField] = useState<"summary" | "key_events" | "foreshadowing" | "prior_deps">("summary");
  const [storyDraftText, setStoryDraftText] = useState("");

  const projects = useQuery({ queryKey: ["projects"], queryFn: () => api<Project[]>("/projects") });
  const active = projectId ?? projects.data?.[0]?.id ?? null;

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
      setEditingId(null);
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
      setEditingStoryId(null);
      toast.success("剧情卡已保存");
    },
    onError: (err) => toast.error(`保存失败：${String(err)}`),
  });

  const confirmRemove = async (name: string, id: string) => {
    const ok = await confirmAction("删除角色档案", `确定删除「${name}」？相关译文会标记待重译。`, true);
    if (ok) removeCharacter.mutate(id);
  };

  const parseStory = (json: string): StoryCard => {
    try { return JSON.parse(json); } catch { return { summary: "", key_events: [], foreshadowing: [], prior_deps: [], character_states: {} }; }
  };

  const saveStoryEdit = () => {
    if (!storyDraft || !editingStoryId) return;
    const next: StoryCard = { ...storyDraft };
    if (storyDraftField === "summary") {
      next.summary = storyDraftText.trim();
    } else {
      (next as unknown as Record<string, string[]>)[storyDraftField] = storyDraftText.split("\n").map((l) => l.trim()).filter(Boolean);
    }
    patchStoryline.mutate({ id: editingStoryId, card: next });
  };

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-3 border-b border-(--color-border) px-6 py-4">
        <h1 className="text-lg font-semibold">知识库</h1>
        <select
          value={active ?? ""}
          onChange={(e) => setProjectId(e.target.value)}
          className="rounded-lg border border-(--color-border) bg-(--color-bg) px-2 py-1 text-sm outline-none focus:border-(--color-accent)"
        >
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
                    <th className="w-32 px-3 py-2 font-medium">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {(characters.data ?? []).filter((k) => k.item_type === "character").map((k) => {
                    const data = (() => { try { return JSON.parse(k.data_json || "{}"); } catch { return {}; } })();
                    const fields: Array<{ key: "translation" | "personality" | "speech_style" | "forms_of_address" | "emotional_range"; label: string; value: string }> = [
                      { key: "translation", label: "译名", value: data.translation || "" },
                      { key: "personality", label: "性格特征", value: data.personality || "" },
                      { key: "speech_style", label: "说话方式", value: data.speech_style || "" },
                      { key: "forms_of_address", label: "称呼习惯", value: (data.forms_of_address || []).join(" / ") },
                      { key: "emotional_range", label: "情绪区间", value: data.emotional_range || "" },
                    ];
                    const editing = editingId === k.id;
                    return (
                      <tr key={k.id} className="border-b border-(--color-border) hover:bg-(--color-panel)">
                        <td className="px-3 py-1.5 font-medium">{k.canonical_key}</td>
                        {fields.slice(0, 3).map((f) => (
                          <td key={f.key} className="px-3 py-1.5">
                            {editing && draftField === f.key ? (
                              <div className="flex items-center gap-1">
                                <input
                                  value={draft}
                                  onChange={(e) => setDraft(e.target.value)}
                                  autoFocus
                                  onKeyDown={(e) => e.key === "Enter" && patchCharacter.mutate({ id: k.id, body: { data: { ...data, [f.key]: f.key === "forms_of_address" ? draft.split("/").map((s) => s.trim()).filter(Boolean) : draft.trim() } } })}
                                  className="w-full rounded-lg border border-(--color-accent) bg-(--color-bg) px-2 py-0.5 text-sm outline-none"
                                />
                                <button
                                  onClick={() => patchCharacter.mutate({ id: k.id, body: { data: { ...data, [f.key]: f.key === "forms_of_address" ? draft.split("/").map((s) => s.trim()).filter(Boolean) : draft.trim() } } })}
                                  className="rounded p-0.5 text-(--color-accent) hover:bg-(--color-panel2)" title="保存"
                                >
                                  <Check size={14} />
                                </button>
                                <button onClick={() => setEditingId(null)} className="rounded p-0.5 text-(--color-muted) hover:bg-(--color-panel2)" title="取消">
                                  <X size={14} />
                                </button>
                              </div>
                            ) : (
                              <span className={f.value ? "" : "text-(--color-muted)"}>
                                {f.key === "translation" && f.value ? <span className="font-mono text-xs text-(--color-accent)">{f.value}</span> : f.value || "—"}
                              </span>
                            )}
                          </td>
                        ))}
                        <td className="px-3 py-1.5">
                          {editing ? (
                            <div className="flex flex-wrap gap-1">
                              {fields.map((f) => (
                                <button key={f.key} onClick={() => { setDraftField(f.key); setDraft(f.key === "forms_of_address" ? (data.forms_of_address || []).join(" / ") : (data[f.key] || "")); }}
                                  className={`rounded px-1.5 py-0.5 text-xs ${draftField === f.key ? "bg-(--color-panel2) font-medium" : "text-(--color-muted) hover:bg-(--color-panel)"}`}>
                                  {f.label}
                                </button>
                              ))}
                              <button onClick={() => setEditingId(null)} className="rounded px-1.5 py-0.5 text-xs text-(--color-muted) hover:bg-(--color-panel)">完成</button>
                            </div>
                          ) : (
                            <div className="flex items-center gap-1">
                              <button onClick={() => { setEditingId(k.id); setDraftField("translation"); setDraft(data.translation || ""); }}
                                className="rounded p-1.5 text-(--color-muted) hover:text-(--color-accent)" title="编辑角色档案">
                                <Pencil size={14} />
                              </button>
                              <button onClick={() => confirmRemove(k.canonical_key, k.id)}
                                className="rounded p-1.5 text-(--color-muted) hover:text-red-600" title="删除角色">
                                <Trash2 size={14} />
                              </button>
                            </div>
                          )}
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
                const editing = editingStoryId === s.id;
                return (
                  <div key={s.id} className="rounded-xl border border-(--color-border) bg-(--color-panel) px-4 py-3">
                    <div className="flex items-center gap-2">
                      <span className="font-semibold">第 {s.chapter_no} 章</span>
                      {s.chapter_title && <span className="text-sm text-(--color-muted)">{s.chapter_title}</span>}
                      {editing ? (
                        <div className="ml-auto flex items-center gap-1.5">
                          <button onClick={saveStoryEdit} className="flex items-center gap-1 rounded-lg bg-(--color-accent) px-2.5 py-1 text-xs text-white">
                            <Check size={13} /> 保存
                          </button>
                          <button onClick={() => setEditingStoryId(null)} className="rounded-lg border border-(--color-border) px-2.5 py-1 text-xs hover:bg-(--color-panel2)">取消</button>
                        </div>
                      ) : (
                        <button onClick={() => { setEditingStoryId(s.id); setStoryDraft(card); setStoryDraftField("summary"); setStoryDraftText(card.summary); }}
                          className="ml-auto flex items-center gap-1 rounded-lg border border-(--color-border) px-2.5 py-1 text-xs hover:bg-(--color-panel2)">
                          <Pencil size={13} /> 编辑
                        </button>
                      )}
                    </div>
                    {editing ? (
                      <div className="mt-2 space-y-2">
                        <div>
                          <div className="mb-1 flex gap-1">
                            {(["summary", "key_events", "foreshadowing", "prior_deps"] as const).map((f) => (
                              <button key={f} onClick={() => { setStoryDraftField(f); setStoryDraftText(f === "summary" ? (storyDraft?.summary ?? "") : ((storyDraft?.[f] as string[]) ?? []).join("\n")); }}
                                className={`rounded px-2 py-0.5 text-xs ${storyDraftField === f ? "bg-(--color-panel2) font-medium" : "text-(--color-muted) hover:bg-(--color-panel)"}`}>
                                {f === "summary" ? "摘要" : f === "key_events" ? "重要情节" : f === "foreshadowing" ? "伏笔（勿剧透）" : "前情依赖"}
                              </button>
                            ))}
                          </div>
                          <textarea
                            value={storyDraftText}
                            onChange={(e) => setStoryDraftText(e.target.value)}
                            rows={storyDraftField === "summary" ? 2 : 6}
                            placeholder={storyDraftField === "key_events" || storyDraftField === "foreshadowing" || storyDraftField === "prior_deps" ? "每行一条" : ""}
                            className="w-full rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-2 text-sm leading-6 outline-none focus:border-(--color-accent)"
                          />
                        </div>
                      </div>
                    ) : (
                      <div className="mt-2 grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
                        <p className="col-span-2 text-(--color-muted)">{card.summary || "（无摘要）"}</p>
                        <div>
                          <p className="text-xs font-medium text-(--color-muted)">重要情节</p>
                          <ul className="mt-1 list-disc space-y-0.5 pl-4 text-sm">
                            {card.key_events.map((e, i) => <li key={i}>{e}</li>)}
                            {card.key_events.length === 0 && <li className="text-(--color-muted)">—</li>}
                          </ul>
                        </div>
                        <div>
                          <p className="text-xs font-medium text-amber-600">伏笔（翻译时勿剧透）</p>
                          <ul className="mt-1 list-disc space-y-0.5 pl-4 text-sm">
                            {card.foreshadowing.map((e, i) => <li key={i}>{e}</li>)}
                            {card.foreshadowing.length === 0 && <li className="text-(--color-muted)">—</li>}
                          </ul>
                          <p className="mt-2 text-xs font-medium text-(--color-muted)">前情依赖</p>
                          <ul className="mt-1 list-disc space-y-0.5 pl-4 text-sm">
                            {card.prior_deps.map((e, i) => <li key={i}>{e}</li>)}
                            {card.prior_deps.length === 0 && <li className="text-(--color-muted)">—</li>}
                          </ul>
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
              {(storylines.data ?? []).length === 0 && (
                <p className="py-8 text-center text-sm text-(--color-muted)">暂无剧情卡（一键直出会自动生成）</p>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
