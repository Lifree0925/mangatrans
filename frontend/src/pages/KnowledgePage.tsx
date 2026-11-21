/** 知识库：查看/修正系统抽取的人物、术语与译名（修改后相关译文标待重译）。 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookOpen, Check, Pencil, Plus, Trash2, X } from "lucide-react";
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

      <div className="flex-1 overflow-auto p-6">
        <div className="mx-auto max-w-4xl space-y-2">
          {adding && (
            <div className="flex flex-wrap items-center gap-2 rounded-xl border border-(--color-accent) bg-(--color-panel) px-4 py-3">
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

          {items.data?.map((k) => {
            const data = (() => { try { return JSON.parse(k.data_json || "{}"); } catch { return {}; } })();
            return (
              <div key={k.id} className="flex items-center gap-3 rounded-xl border border-(--color-border) bg-(--color-panel) px-4 py-2.5">
                <span className="w-12 shrink-0 rounded-md bg-(--color-panel2) px-1.5 py-0.5 text-center text-xs text-(--color-muted)">
                  {typeLabel[k.item_type] ?? k.item_type}
                </span>
                {editingId === k.id ? (
                  <>
                    <input
                      value={draft}
                      onChange={(e) => setDraft(e.target.value)}
                      autoFocus
                      onKeyDown={(e) => e.key === "Enter" && draft.trim() && patchItem.mutate({ id: k.id, body: { data: { ...data, translation: draft.trim() } } })}
                      placeholder="译名"
                      className="w-64 rounded-lg border border-(--color-accent) bg-(--color-bg) px-2 py-1 text-sm outline-none"
                    />
                    <button
                      onClick={() => draft.trim() && patchItem.mutate({ id: k.id, body: { data: { ...data, translation: draft.trim() } } })}
                      className="rounded p-1 text-(--color-accent) hover:bg-(--color-panel2)" title="保存"
                    >
                      <Check size={14} />
                    </button>
                    <button onClick={() => setEditingId(null)} className="rounded p-1 text-(--color-muted) hover:bg-(--color-panel2)" title="取消">
                      <X size={14} />
                    </button>
                  </>
                ) : (
                  <>
                    <span className="font-medium">{k.canonical_key}</span>
                    <span className="font-mono text-xs text-(--color-accent)">
                      {data.translation || "（未设置译名）"}
                    </span>
                    <span className="rounded-md bg-(--color-panel2) px-1.5 py-0.5 text-xs text-(--color-muted)">
                      {statusLabel[k.status] ?? k.status}
                    </span>
                    {k.source === "manual" && <span className="text-xs text-(--color-muted)">手动添加</span>}
                  </>
                )}
                <div className="ml-auto flex items-center gap-1">
                  {editingId !== k.id && (
                    <button
                      onClick={() => { setEditingId(k.id); setDraft(data.translation || ""); }}
                      className="rounded p-1.5 text-(--color-muted) hover:text-(--color-accent)" title="修改译名"
                    >
                      <Pencil size={14} />
                    </button>
                  )}
                  <button
                    onClick={() => confirmRemove(k)}
                    className="rounded p-1.5 text-(--color-muted) hover:text-red-600" title="删除条目"
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              </div>
            );
          })}
          {items.data?.length === 0 && (
            <div className="py-12 text-center">
              <BookOpen size={28} className="mx-auto text-(--color-border)" />
              <p className="mt-3 text-sm text-(--color-muted)">暂无知识条目（先运行一键直出自动提取，或手动添加）</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
