/** 知识库总览：仅展示已有知识库数据的项目（一键直出后自动出现），支持整体硬删除。 */

import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookOpen, ChevronRight, Trash2 } from "lucide-react";
import { api } from "../lib/api";
import { confirmAction, toast } from "../lib/feedback";

interface OverviewItem {
  project_id: string;
  project_name: string;
  filename: string | null;
  characters: number;
  terms: number;
  rules: number;
  storylines: number;
  dialogue_lines: number;
  updated_at: string | null;
}

export default function KnowledgeOverviewPage() {
  const qc = useQueryClient();
  const overview = useQuery({
    queryKey: ["knowledge-overview"],
    queryFn: () => api<{ items: OverviewItem[] }>("/knowledge/overview"),
    refetchInterval: 8000,
  });

  const remove = useMutation({
    mutationFn: (projectId: string) =>
      api(`/projects/${projectId}/knowledge`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["knowledge-overview"] });
      toast.success("知识库已删除");
    },
    onError: (err) => toast.error(`删除失败：${String(err)}`),
  });

  const items = overview.data?.items ?? [];

  return (
    <div className="h-full overflow-auto">
      <div className="mx-auto max-w-5xl px-8 py-8">
        <h1 className="text-xl font-semibold">知识库</h1>

        {overview.isLoading && <div className="mt-8" />}
        {!overview.isLoading && items.length === 0 && (
          <div className="mt-24 flex flex-col items-center gap-3 text-(--color-muted)">
            <BookOpen size={32} strokeWidth={1.5} />
            <p className="text-sm">暂无知识库</p>
          </div>
        )}

        <div className="mt-6 grid grid-cols-1 gap-4 xl:grid-cols-2">
          {items.map((item) => (
            <Link
              key={item.project_id}
              to={`/knowledge/${item.project_id}`}
              className="group relative rounded-2xl border border-(--color-border) bg-(--color-bg) p-5 transition-colors hover:border-(--color-text)"
            >
              <div className="flex items-center gap-2 pr-10">
                <BookOpen size={16} className="shrink-0 text-(--color-accent)" />
                <span className="truncate text-[15px] font-medium">{item.project_name}</span>
                {item.filename && (
                  <span className="truncate text-xs text-(--color-muted)">{item.filename}</span>
                )}
              </div>

              <div className="mt-4 flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
                {[
                  { n: item.characters, label: "角色" },
                  { n: item.storylines, label: "章节剧情" },
                  { n: item.dialogue_lines, label: "台词" },
                  { n: item.terms, label: "术语" },
                ].map((s) => (
                  <span key={s.label} className="flex items-baseline gap-1.5">
                    <span className="font-medium">{s.n}</span>
                    <span className="text-xs text-(--color-muted)">{s.label}</span>
                  </span>
                ))}
                <span className="ml-auto flex items-center gap-1 text-xs text-(--color-muted) opacity-0 transition-opacity group-hover:opacity-100">
                  查看 <ChevronRight size={14} />
                </span>
              </div>

              {item.updated_at && (
                <p className="mt-3 text-[11px] text-(--color-muted)">
                  更新于 {new Date(item.updated_at).toLocaleString()}
                </p>
              )}

              <button
                onClick={async (e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  const ok = await confirmAction(
                    "删除知识库？",
                    "将清空该项目的角色档案、术语表、章节剧情与台词本，且不可恢复。项目、原文与译文不受影响。",
                    true
                  );
                  if (ok) remove.mutate(item.project_id);
                }}
                title="删除知识库"
                className="absolute right-4 top-4 rounded-lg p-1.5 text-(--color-muted) opacity-0 transition-colors hover:bg-red-50 hover:text-red-600 group-hover:opacity-100"
              >
                <Trash2 size={15} />
              </button>
            </Link>
          ))}
        </div>
      </div>
    </div>
  );
}
