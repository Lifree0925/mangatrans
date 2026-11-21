/** 知识库：查看/修正系统抽取的人物、术语与译名（修改后相关译文标待重译）。 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Pencil, Plus, Trash2, X } from "lucide-react";
import { api } from "../lib/api";
import { confirmAction, toast } from "../lib/feedback";
import type { KnowledgeEntry, Project } from "../lib/types";

const typeLabel: Record<string, string> = {
  character: "人物",
  term: "术语",
  relation: "关系",
  rule: "规则",
};
const statusLabel: Record<string, string> = {
  candidate: "待确认",
  approved: "已批准",
  auto_approved: "自动批准",
  rejected: "已驳回",
};

export default function KnowledgePage() {
  const qc = useQueryClient();
  const [projectId, setProjectId] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [adding, setAdding] = useState(false);
  const [addForm, setAddForm] = useState({ item_type: "character", canonical_key: "", translation: "" });
  const [typeFilter, setTypeFilter] = useState<string>("all");

  const projects = useQuery({ queryKey: ["projects"], queryFn: () => api<Project[]>("/projects") });
  const active = projectId ?? projects.data?.[0]?.id ?? null;

  const items = useQuery({
    queryKey: ["knowledge", active],
    queryFn: () => api<KnowledgeEntry[]>(`/projects/${active}/knowledge`),
    enabled: !!active,
    refetchInterval: 5000,
  });

  const patchItem = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Record<string, unknown> }) =>
      api(`/projects/${active}/knowledge/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
    onSuccess: (r: unknown) => {
      const result = r as { stale_translations?: number };
      qc.invalidateQueries({ queryKey: ["knowledge", active] });
      setEditingId(null);
      if (result.stale_translations) toast.success(`已保存，${result.stale_translations} 条译文标记待重译`);
      else toast.success("已保存");
    },
    onError: (err) => toast.error(`保存失败：${String(err)}`),
  });

  const removeItem = useMutation({
    mutationFn: (id: string) => api(`/projects/${active}/knowledge/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["knowledge", active] });
      toast.success("条目已删除");
    },
    onError: (err) => toast.error(`删除失败：${String(err)}`),
  });

  const addItem = useMutation({
    mutationFn: () =>
      api(`/projects/${active}/knowledge`, {
        method: "POST",
        body: JSON.stringify({ ...addForm, canonical_key: addForm.canonical_key.trim() }),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["knowledge", active] });
      setAdding(false);
      setAddForm({ item_type: "character", canonical_key: "", translation: "" });
      toast.success("已添加知识条目");
    },
    onError: (err) => toast.error(`添加失败：${String(err)}`),
  });

  const confirmRemove = async (k: KnowledgeEntry) => {
    const ok = await confirmAction("删除知识条目", `确定删除「${k.canonical_key}」？相关译文会标记待重译。`, true);
    if (ok) removeItem.mutate(k.id);
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
        <p className="text-xs text-(--color-muted)">
          系统抽取的人物/术语及译名；修改后已译内容会标记「待重译」
        </p>
        <button
          onClick={() => setAdding((v) => !v)}
          className="ml-auto flex items-center gap-1.5 rounded-lg bg-(--color-accent) px-3 py-1.5 text-sm text-white hover:bg-(--color-accent-hover)"
        >
          <Plus size={15} /> 添加条目
        </button>
      </div>

      <div className="flex-1 overflow-auto p-4">
        <div className="mx-auto max-w-5xl">
          {adding && (
            <div className="mb-3 flex flex-wrap items-center gap-2 rounded-xl border border-(--color-accent) bg-(--color-panel) px-4 py-2.5">
              <select
                value={addForm.item_type}
                onChange={(e) => setAddForm((f) => ({ ...f, item_type: e.target.value }))}
                className="rounded-lg border border-(--color-border) bg-(--color-bg) px-2 py-1 text-sm outline-none"
              >
                <option value="character">人物</option>
                <option value="term">术语</option>
                <option value="relation">关系</option>
                <option value="rule">规则</option>
              </select>
              <input
                value={addForm.canonical_key}
                onChange={(e) => setAddForm((f) => ({ ...f, canonical_key: e.target.value }))}
                placeholder="原文名称（如：林风）"
                className="w-52 rounded-lg border border-(--color-border) bg-(--color-bg) px-2 py-1 text-sm outline-none focus:border-(--color-accent)"
              />
              <input
                value={addForm.translation}
                onChange={(e) => setAddForm((f) => ({ ...f, translation: e.target.value }))}
                placeholder="译名（如：Lin Feng，可留空）"
                className="w-52 rounded-lg border border-(--color-border) bg-(--color-bg) px-2 py-1 text-sm outline-none focus:border-(--color-accent)"
              />
              <button
                onClick={() => addItem.mutate()}
                disabled={!addForm.canonical_key.trim()}
                className="flex items-center gap-1 rounded-lg bg-(--color-accent) px-2.5 py-1 text-xs text-white disabled:opacity-40"
              >
                <Check size={13} /> 保存
              </button>
              <button onClick={() => setAdding(false)} className="rounded-lg border border-(--color-border) px-2.5 py-1 text-xs hover:bg-(--color-panel2)">
                取消
              </button>
            </div>
          )}

          {(() => {
            const all = items.data ?? [];
            const counts: Record<string, number> = {};
            for (const k of all) counts[k.item_type] = (counts[k.item_type] ?? 0) + 1;
            const filtered = typeFilter === "all" ? all : all.filter((k) => k.item_type === typeFilter);
            return (
              <>
                <div className="mb-3 flex items-center gap-1.5">
                  {[
                    ["all", "全部", all.length],
                    ["character", "人物", counts.character ?? 0],
                    ["term", "术语", counts.term ?? 0],
                    ["relation", "关系", counts.relation ?? 0],
                    ["rule", "规则", counts.rule ?? 0],
                  ].map(([key, label, count]) => (
                    <button
                      key={key as string}
                      onClick={() => setTypeFilter(key as string)}
                      className={`rounded-lg px-3 py-1 text-sm ${
                        typeFilter === key
                          ? "bg-(--color-panel2) font-medium text-(--color-text)"
                          : "text-(--color-muted) hover:bg-(--color-panel)"
                      }`}
                    >
                      {label} {count}
                    </button>
                  ))}
                </div>

                <table className="w-full border-collapse text-sm">
                  <thead>
                    <tr className="border-b border-(--color-border) text-left text-xs text-(--color-muted)">
                      <th className="px-3 py-2 font-medium">类型</th>
                      <th className="px-3 py-2 font-medium">原文</th>
                      <th className="px-3 py-2 font-medium">译名</th>
                      <th className="px-3 py-2 font-medium">状态</th>
                      <th className="w-24 px-3 py-2 text-right font-medium">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filtered.map((k) => {
                      const data = (() => { try { return JSON.parse(k.data_json || "{}"); } catch { return {}; } })();
                      return (
                        <tr key={k.id} className="group border-b border-(--color-border) hover:bg-(--color-panel)">
                          <td className="px-3 py-1.5">
                            <span className="rounded-md bg-(--color-panel2) px-1.5 py-0.5 text-xs text-(--color-muted)">
                              {typeLabel[k.item_type] ?? k.item_type}
                            </span>
                          </td>
                          <td className="max-w-56 truncate px-3 py-1.5 font-medium">{k.canonical_key}</td>
                          <td className="px-3 py-1.5">
                            {editingId === k.id ? (
                              <div className="flex items-center gap-1">
                                <input
                                  value={draft}
                                  onChange={(e) => setDraft(e.target.value)}
                                  autoFocus
                                  onKeyDown={(e) => e.key === "Enter" && draft.trim() && patchItem.mutate({ id: k.id, body: { data: { ...data, translation: draft.trim() } } })}
                                  placeholder="译名"
                                  className="w-48 rounded-lg border border-(--color-accent) bg-(--color-bg) px-2 py-0.5 text-sm outline-none"
                                />
                                <button
                                  onClick={() => draft.trim() && patchItem.mutate({ id: k.id, body: { data: { ...data, translation: draft.trim() } } })}
                                  className="rounded p-0.5 text-(--color-accent) hover:bg-(--color-panel2)" title="保存"
                                >
                                  <Check size={14} />
                                </button>
                                <button onClick={() => setEditingId(null)} className="rounded p-0.5 text-(--color-muted) hover:bg-(--color-panel2)" title="取消">
                                  <X size={14} />
                                </button>
                              </div>
                            ) : (
                              <span className={`font-mono text-xs ${data.translation ? "text-(--color-accent)" : "text-(--color-muted)"}`}>
                                {data.translation || "（未设置译名）"}
                              </span>
                            )}
                          </td>
                          <td className="px-3 py-1.5">
                            <span className="rounded-md bg-(--color-panel2) px-1.5 py-0.5 text-xs text-(--color-muted)">
                              {statusLabel[k.status] ?? k.status}
                            </span>
                          </td>
                          <td className="px-3 py-1.5">
                            <div className="flex items-center justify-end gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
                              {editingId !== k.id && (
                                <button
                                  onClick={() => { setEditingId(k.id); setDraft(data.translation || ""); }}
                                  className="rounded p-1 text-(--color-muted) hover:text-(--color-accent)" title="修改译名"
                                >
                                  <Pencil size={13} />
                                </button>
                              )}
                              <button
                                onClick={() => confirmRemove(k)}
                                className="rounded p-1 text-(--color-muted) hover:text-red-600" title="删除条目"
                              >
                                <Trash2 size={13} />
                              </button>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
                {filtered.length === 0 && (
                  <p className="py-8 text-center text-sm text-(--color-muted)">
                    该分类暂无条目（先运行一键直出自动提取，或手动添加）
                  </p>
                )}
              </>
            );
          })()}
        </div>
      </div>
    </div>
  );
}
