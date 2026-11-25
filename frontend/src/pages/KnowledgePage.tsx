/** 知识库：角色档案、术语表、章节剧情与人物状态。修改均通过浮层完成。 */

import { startTransition, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ChevronLeft, ChevronRight, Loader2, Pencil, Plus, Trash2, X } from "lucide-react";
import { api } from "../lib/api";
import { chapterLabel, characterDisplayName } from "../lib/format";
import { confirmAction, toast } from "../lib/feedback";
import type { ChapterStoryline, KnowledgeEntry, Project } from "../lib/types";

type StoryCard = {
  summary: string;
  key_events: string[];
  foreshadowing: string[];
  prior_deps: string[];
  character_states: Record<string, string>;
  character_state_details?: Array<{
    name: string;
    identity?: string;
    relationship_to_protagonist?: string;
    faction?: string;
    emotion?: string;
    form_of_address?: string;
    known_secrets?: string[];
  }>;
  critical_facts?: Array<Record<string, unknown>>;
};

type CharacterStateRow = {
  id: string;
  character: string;
  chapter_no: number;
  version: number;
  state: {
    identity?: string;
    relationship_to_protagonist?: string;
    faction?: string;
    emotion?: string;
    form_of_address?: string;
    known_secrets?: string[];
    summary?: string;
  };
};

type StoryFactRow = {
  id: string;
  chapter_no: number;
  fact_type: string;
  subject?: string;
  text: string;
};

const FACT_LABELS: Record<string, string> = {
  event: "事件",
  prior_dependency: "前情",
  foreshadowing: "伏笔",
  secret: "秘密",
  relationship: "关系",
  identity: "身份",
  time: "时间",
  location: "地点",
  possession: "归属",
  negation_condition: "否定/条件",
  faction: "阵营",
  address: "称呼",
};

const TERM_KIND_LABELS: Record<string, string> = {
  proper_name: "专名",
  title: "头衔",
  place: "地点",
  location: "地点",
  item: "设定物",
  common_noun: "普通名词",
  species: "物种",
  group: "群体",
  concept: "概念",
};

const TERM_STATUS_LABELS: Record<string, string> = {
  auto_approved: "自动启用",
  approved: "已启用",
  candidate: "候选",
  rejected: "已忽略",
};

const STORY_FIELDS = [
  { key: "summary", label: "摘要" },
  { key: "key_events", label: "重要情节" },
  { key: "foreshadowing", label: "伏笔（勿剧透）" },
  { key: "prior_deps", label: "前情依赖" },
] as const;

export default function KnowledgePage() {
  const qc = useQueryClient();
  // 项目由路由显式指定（/knowledge/:projectId），不再使用下拉切换
  const { projectId } = useParams<{ projectId: string }>();
  const [tab, setTab] = useState<"profiles" | "terms" | "storylines" | "states">("profiles");
  const [adding, setAdding] = useState(false);
  const [addForm, setAddForm] = useState({ canonical_key: "", translation: "" });

  // 角色编辑浮层
  const [editingChar, setEditingChar] = useState<KnowledgeEntry | null>(null);
  const [charDraft, setCharDraft] = useState({ translation: "", personality: "", speech_style: "", forms_of_address: "", emotional_range: "" });

  // 术语表
  const [termAdding, setTermAdding] = useState(false);
  const [termAddForm, setTermAddForm] = useState({ canonical_key: "", translation: "" });
  const [editingTerm, setEditingTerm] = useState<KnowledgeEntry | null>(null);
  const [termDraft, setTermDraft] = useState({
    translation: "",
    term_kind: "proper_name",
    enforcement: "exact",
    variants: "",
    status: "approved",
  });

  // 剧情卡编辑浮层
  const [editingStory, setEditingStory] = useState<ChapterStoryline | null>(null);
  const [storyDraft, setStoryDraft] = useState<StoryCard>({ summary: "", key_events: [], foreshadowing: [], prior_deps: [], character_states: {} });
  const [storyActiveField, setStoryActiveField] = useState<(typeof STORY_FIELDS)[number]["key"]>("summary");
  const [storyFieldText, setStoryFieldText] = useState("");

  const storyAnchorsRef = useRef<{ top: number; chapterId: string }[]>([]);
  const [charGroup, setCharGroup] = useState<"major" | "minor">("major");
  // 章节剧情目录栏
  const [storyCollapsed, setStoryCollapsed] = useState(false);
  const [storyActiveChapter, setStoryActiveChapter] = useState<string | null>(null);
  const storyBodyRef = useRef<HTMLDivElement | null>(null);

  const active = projectId ?? null;
  const project = useQuery({
    queryKey: ["project", active],
    queryFn: () => api<Project>(`/projects/${active}`),
    enabled: !!active,
  });

  const characters = useQuery({
    queryKey: ["knowledge", active],
    queryFn: () => api<KnowledgeEntry[]>(`/projects/${active}/knowledge`),
    enabled: !!active && tab === "profiles",
    refetchInterval: 8000,
  });
  const terms = useQuery({
    queryKey: ["knowledge", active],
    queryFn: () => api<KnowledgeEntry[]>(`/projects/${active}/knowledge`),
    enabled: !!active && tab === "terms",
    refetchInterval: 8000,
  });
  const storylines = useQuery({
    queryKey: ["storylines", active],
    queryFn: () => api<ChapterStoryline[]>(`/projects/${active}/storylines`),
    enabled: !!active && tab === "storylines",
    refetchInterval: 8000,
  });
  const characterStates = useQuery({
    queryKey: ["character-states", active],
    queryFn: () => api<CharacterStateRow[]>(`/projects/${active}/character-states`),
    enabled: !!active && tab === "states",
    refetchInterval: 8000,
  });
  const storyFacts = useQuery({
    queryKey: ["story-facts", active],
    queryFn: () => api<StoryFactRow[]>(`/projects/${active}/story-facts`),
    enabled: !!active && tab === "states",
    refetchInterval: 8000,
  });

  const patchRoleWeight = useMutation({
    mutationFn: ({ id, weight }: { id: string; weight: "major" | "minor" }) =>
      api(`/projects/${active}/knowledge/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ data: { role_weight: weight } }),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["knowledge", active] });
      toast.success("角色分组已更新");
    },
    onError: (err) => toast.error(`更新失败：${String(err)}`),
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

  const patchTerm = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Record<string, unknown> }) =>
      api(`/projects/${active}/knowledge/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
    onSuccess: (r: unknown) => {
      const result = r as { stale_translations?: number };
      qc.invalidateQueries({ queryKey: ["knowledge", active] });
      setEditingTerm(null);
      toast.success(result.stale_translations ? `已保存，${result.stale_translations} 条译文标记待重译` : "已保存");
    },
    onError: (err) => toast.error(`保存失败：${String(err)}`),
  });

  const addTerm = useMutation({
    mutationFn: () => api(`/projects/${active}/knowledge`, {
      method: "POST",
      body: JSON.stringify({
        item_type: "term",
        canonical_key: termAddForm.canonical_key.trim(),
        translation: termAddForm.translation.trim(),
        status: "approved",
      }),
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["knowledge", active] });
      setTermAdding(false);
      setTermAddForm({ canonical_key: "", translation: "" });
      toast.success("术语已添加");
    },
    onError: (err) => toast.error(`添加失败：${String(err)}`),
  });

  const removeTerm = useMutation({
    mutationFn: (id: string) => api(`/projects/${active}/knowledge/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["knowledge", active] });
      toast.success("术语已删除");
    },
    onError: (err) => toast.error(`删除失败：${String(err)}`),
  });

  const patchStoryline = useMutation({
    mutationFn: ({ id, card }: { id: string; card: StoryCard }) =>
      api(`/projects/${active}/storylines/${id}`, { method: "PATCH", body: JSON.stringify({ card }) }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["storylines", active] });
      qc.invalidateQueries({ queryKey: ["character-states", active] });
      qc.invalidateQueries({ queryKey: ["story-facts", active] });
      setEditingStory(null);
      toast.success("剧情卡已保存");
    },
    onError: (err) => toast.error(`保存失败：${String(err)}`),
  });

  useEffect(() => {
    const sc = storyBodyRef.current;
    if (sc) {
      storyAnchorsRef.current = [];
      sc.querySelectorAll<HTMLElement>("[data-story-chapter]").forEach((el) => {
        storyAnchorsRef.current.push({ top: el.offsetTop, chapterId: el.dataset.storyChapter ?? "" });
      });
    }
  }, [storylines.data]);

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

  const openTermEdit = (k: KnowledgeEntry) => {
    const data = (() => { try { return JSON.parse(k.data_json || "{}"); } catch { return {}; } })();
    setEditingTerm(k);
    setTermDraft({
      translation: data.translation || "",
      term_kind: data.term_kind || "proper_name",
      enforcement: data.enforcement || k.effective_policy || "contextual",
      variants: (data.variants || []).join(" / "),
      status: k.status || "approved",
    });
  };

  const saveTermEdit = () => {
    if (!editingTerm) return;
    const data = (() => { try { return JSON.parse(editingTerm.data_json || "{}"); } catch { return {}; } })();
    patchTerm.mutate({
      id: editingTerm.id,
      body: {
        status: termDraft.status,
        data: {
          ...data,
          translation: termDraft.translation.trim(),
          term_kind: termDraft.term_kind,
          enforcement: termDraft.enforcement,
          variants: termDraft.variants.split(/[,，\n]/).map((x) => x.trim()).filter(Boolean),
        },
      },
    });
  };

  const confirmRemoveTerm = async (name: string, id: string) => {
    const ok = await confirmAction("删除术语", `确定删除「${name}」？相关译文会标记待重译。`, true);
    if (ok) removeTerm.mutate(id);
  };

  const submitTermAdd = () => {
    if (!termAddForm.canonical_key.trim() || !termAddForm.translation.trim()) return;
    addTerm.mutate();
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
      <div className="flex items-center gap-2 border-b border-(--color-border) px-6 py-4">
        <Link
          to="/knowledge"
          title="返回知识库"
          className="rounded-lg p-1.5 text-(--color-muted) transition-colors hover:bg-(--color-panel2) hover:text-(--color-text)"
        >
          <ChevronLeft size={18} />
        </Link>
        <h1 className="text-lg font-semibold">知识库</h1>
        <span className="text-(--color-muted)">/</span>
        <span className="max-w-[280px] truncate text-sm font-medium">
          {project.data?.name ?? "…"}
        </span>
        <div className="ml-2 flex items-center gap-1.5">
          <button
            onClick={() => setTab("profiles")}
            className={`rounded-lg px-3 py-1 text-sm ${tab === "profiles" ? "bg-(--color-panel2) font-medium" : "text-(--color-muted) hover:bg-(--color-panel)"}`}
          >
            角色档案
          </button>
          <button
            onClick={() => setTab("terms")}
            className={`rounded-lg px-3 py-1 text-sm ${tab === "terms" ? "bg-(--color-panel2) font-medium" : "text-(--color-muted) hover:bg-(--color-panel)"}`}
          >
            术语表
          </button>
          <button
            onClick={() => setTab("storylines")}
            className={`rounded-lg px-3 py-1 text-sm ${tab === "storylines" ? "bg-(--color-panel2) font-medium" : "text-(--color-muted) hover:bg-(--color-panel)"}`}
          >
            章节剧情
          </button>
          <button
            onClick={() => setTab("states")}
            className={`rounded-lg px-3 py-1 text-sm ${tab === "states" ? "bg-(--color-panel2) font-medium" : "text-(--color-muted) hover:bg-(--color-panel)"}`}
          >
            人物状态
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
        {tab === "terms" && (
          <button
            onClick={() => setTermAdding((v) => !v)}
            className="ml-auto flex items-center gap-1.5 rounded-lg bg-(--color-accent) px-3 py-1.5 text-sm text-white hover:bg-(--color-accent-hover)"
          >
            <Plus size={15} /> 添加术语
          </button>
        )}
      </div>

      <div className={tab === "storylines" && active ? "flex-1 overflow-hidden" : "toc-hide-scroll flex-1 overflow-auto p-4"}>
        {!active ? null : (
        <div className={tab === "storylines" ? "h-full" : "mx-auto max-w-5xl"}>
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
              {!characters.isFetchedAfterMount && (
                <div className="flex justify-center py-16 text-(--color-muted)">
                  <Loader2 size={22} className="animate-spin" />
                </div>
              )}
              {characters.isFetchedAfterMount && characters.isError && (
                <p className="py-8 text-center text-sm text-(--color-muted)">角色档案加载失败</p>
              )}
              {characters.isFetchedAfterMount && !characters.isError && (() => {
                const chars = (characters.data ?? []).filter((k) => k.item_type === "character");
                // 分类：用户手动指定优先（role_weight），否则按出场数（>=5 句为主要）
                const isMajor = (k: KnowledgeEntry) => {
                  const d = (() => { try { return JSON.parse(k.data_json || "{}"); } catch { return {}; } })();
                  if (d.role_weight === "major") return true;
                  if (d.role_weight === "minor") return false;
                  return (k.occurrence_count ?? 0) >= 5;
                };
                const major = chars.filter(isMajor);
                const minor = chars.filter((k) => !isMajor(k));
                const Card = ({ k }: { k: KnowledgeEntry }) => {
                  const data = (() => { try { return JSON.parse(k.data_json || "{}"); } catch { return {}; } })();
                  const occ = k.occurrence_count;
                  return (
                    <div className="rounded-xl border border-(--color-border) bg-(--color-panel) px-4 py-3">
                      <div className="flex items-center gap-2">
                        <span className="text-base font-semibold">{characterDisplayName(k.canonical_key)}</span>
                        {typeof occ === "number" && occ > 0 && (
                          <span className="rounded-md bg-(--color-panel2) px-1.5 py-0.5 text-[11px] text-(--color-muted)">
                            出现 {occ} 次
                          </span>
                        )}
                        <div className="ml-auto flex items-center gap-1">
                          <button
                            onClick={() => patchRoleWeight.mutate({ id: k.id, weight: isMajor(k) ? "minor" : "major" })}
                            className="rounded border border-(--color-border) px-1.5 py-0.5 text-[11px] text-(--color-muted) hover:bg-(--color-panel2) hover:text-(--color-accent)"
                            title={isMajor(k) ? "调整为次要角色" : "调整为主要角色"}
                          >
                            {isMajor(k) ? "设次要" : "设主要"}
                          </button>
                          <button onClick={() => openCharEdit(k)}
                            className="rounded p-1.5 text-(--color-muted) hover:text-(--color-accent)" title="编辑角色档案">
                            <Pencil size={14} />
                          </button>
                          <button onClick={() => confirmRemove(characterDisplayName(k.canonical_key), k.id)}
                            className="rounded p-1.5 text-(--color-muted) hover:text-red-600" title="删除角色">
                            <Trash2 size={14} />
                          </button>
                        </div>
                      </div>
                      <div className="mt-2 space-y-1.5 text-sm">
                        {data.translation && (
                          <p><span className="text-xs text-(--color-muted)">译名</span> <span className="font-mono text-xs text-(--color-accent)">{data.translation}</span></p>
                        )}
                        <p>
                          <span className="text-xs text-(--color-muted)">性格</span>{" "}
                          <span>{data.personality || "—"}</span>
                        </p>
                        <p>
                          <span className="text-xs text-(--color-muted)">说话方式</span>{" "}
                          <span>{data.speech_style || "—"}</span>
                        </p>
                        {Array.isArray(data.forms_of_address) && data.forms_of_address.length > 0 && (
                          <p>
                            <span className="text-xs text-(--color-muted)">称呼</span>{" "}
                            <span>{data.forms_of_address.join(" / ")}</span>
                          </p>
                        )}
                        {data.emotional_range && (
                          <p>
                            <span className="text-xs text-(--color-muted)">情绪区间</span>{" "}
                            <span>{data.emotional_range}</span>
                          </p>
                        )}
                      </div>
                    </div>
                  );
                };
                return (
                  <>
                    <div className="mb-3 flex items-center gap-1.5">
                      <button
                        onClick={() => setCharGroup("major")}
                        className={`rounded-lg px-3 py-1.5 text-sm ${charGroup === "major" ? "bg-(--color-panel2) font-medium" : "text-(--color-muted) hover:bg-(--color-panel)"}`}
                      >
                        主要角色（常驻 / 重要）· {major.length}
                      </button>
                      <button
                        onClick={() => setCharGroup("minor")}
                        className={`rounded-lg px-3 py-1.5 text-sm ${charGroup === "minor" ? "bg-(--color-panel2) font-medium" : "text-(--color-muted) hover:bg-(--color-panel)"}`}
                      >
                        次要角色（出场少 / 无权重）· {minor.length}
                      </button>
                    </div>
                    {charGroup === "major" && (
                      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
                        {major.map((k) => <Card key={k.id} k={k} />)}
                        {major.length === 0 && chars.length > 0 && (
                          <p className="col-span-2 text-sm text-(--color-muted)">暂无主要角色</p>
                        )}
                      </div>
                    )}
                    {charGroup === "minor" && (
                      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
                        {minor.map((k) => <Card key={k.id} k={k} />)}
                        {minor.length === 0 && chars.length > 0 && (
                          <p className="col-span-2 text-sm text-(--color-muted)">暂无次要角色</p>
                        )}
                      </div>
                    )}
                  </>
                );
              })()}
              {characters.isFetchedAfterMount &&
                !characters.isError &&
                (characters.data ?? []).filter((k) => k.item_type === "character").length === 0 && (
                  <p className="py-8 text-center text-sm text-(--color-muted)">暂无角色档案（一键直出会自动提取，或手动添加角色）</p>
                )}
            </>
          )}

          {tab === "terms" && (() => {
            const rows = (terms.data ?? []).filter((k) => k.item_type === "term");
            const policyLabel = (policy?: string | null) =>
              policy === "exact" ? "强制" : policy === "lexical" ? "词形" : "参考";
            return (
              <div className="space-y-3">
                {termAdding && (
                  <div className="flex items-center gap-2 rounded-xl border border-(--color-accent) bg-(--color-panel) px-4 py-2.5">
                    <input
                      value={termAddForm.canonical_key}
                      onChange={(e) => setTermAddForm((f) => ({ ...f, canonical_key: e.target.value }))}
                      placeholder="原文术语"
                      className="w-52 rounded-lg border border-(--color-border) bg-(--color-bg) px-2 py-1 text-sm outline-none focus:border-(--color-accent)"
                    />
                    <input
                      value={termAddForm.translation}
                      onChange={(e) => setTermAddForm((f) => ({ ...f, translation: e.target.value }))}
                      placeholder="标准英文译名"
                      className="w-60 rounded-lg border border-(--color-border) bg-(--color-bg) px-2 py-1 font-mono text-sm outline-none focus:border-(--color-accent)"
                    />
                    <button
                      onClick={submitTermAdd}
                      disabled={!termAddForm.canonical_key.trim() || !termAddForm.translation.trim() || addTerm.isPending}
                      className="flex items-center gap-1 rounded-lg bg-(--color-accent) px-2.5 py-1 text-xs text-white disabled:opacity-40"
                    >
                      <Check size={13} /> 保存
                    </button>
                    <button onClick={() => setTermAdding(false)} className="rounded-lg border border-(--color-border) px-2.5 py-1 text-xs hover:bg-(--color-panel2)">取消</button>
                  </div>
                )}
                {!terms.isFetchedAfterMount && (
                  <div className="flex justify-center py-16 text-(--color-muted)"><Loader2 size={22} className="animate-spin" /></div>
                )}
                {terms.isFetchedAfterMount && terms.isError && (
                  <p className="py-8 text-center text-sm text-(--color-muted)">术语表加载失败</p>
                )}
                {terms.isFetchedAfterMount && !terms.isError && rows.length > 0 && (
                  <div className="overflow-x-auto rounded-xl border border-(--color-border) bg-(--color-panel)">
                    <table className="w-full min-w-[680px] text-left text-sm">
                      <thead className="border-b border-(--color-border) text-xs text-(--color-muted)">
                        <tr>
                          <th className="px-4 py-2 font-medium">原文</th>
                          <th className="px-4 py-2 font-medium">标准译名</th>
                          <th className="px-4 py-2 font-medium">类型</th>
                          <th className="px-4 py-2 font-medium">策略</th>
                          <th className="px-4 py-2 font-medium">状态</th>
                          <th className="w-20 px-4 py-2 font-medium">操作</th>
                        </tr>
                      </thead>
                      <tbody>
                        {rows.map((k) => {
                          const data = (() => { try { return JSON.parse(k.data_json || "{}"); } catch { return {}; } })();
                          const conflicts = Array.isArray(data.translation_conflicts) ? data.translation_conflicts : [];
                          return (
                            <tr key={k.id} className="border-b border-(--color-border) last:border-0">
                              <td className="px-4 py-2.5">
                                <span className="font-medium">{k.canonical_key}</span>
                                {conflicts.length > 0 && (
                                  <p className="mt-0.5 text-[11px] text-amber-600">冲突：{conflicts.join(" / ")}</p>
                                )}
                              </td>
                              <td className="px-4 py-2.5 font-mono text-xs text-(--color-accent)">{data.translation || "—"}</td>
                              <td className="px-4 py-2.5 text-xs text-(--color-muted)">{TERM_KIND_LABELS[data.term_kind] || data.term_kind || "—"}</td>
                              <td className="px-4 py-2.5">
                                <span className={`rounded-md px-1.5 py-0.5 text-[11px] ${(k.effective_policy || data.enforcement) === "exact" ? "bg-amber-100 text-amber-700" : "bg-(--color-panel2) text-(--color-muted)"}`}>
                                  {policyLabel(k.effective_policy || data.enforcement)}
                                </span>
                              </td>
                              <td className="px-4 py-2.5 text-xs text-(--color-muted)">{TERM_STATUS_LABELS[k.status] || k.status}</td>
                              <td className="px-4 py-2.5">
                                <div className="flex items-center gap-1">
                                  <button onClick={() => openTermEdit(k)} className="rounded p-1.5 text-(--color-muted) hover:text-(--color-accent)" title="编辑术语">
                                    <Pencil size={14} />
                                  </button>
                                  <button onClick={() => confirmRemoveTerm(k.canonical_key, k.id)} className="rounded p-1.5 text-(--color-muted) hover:text-red-600" title="删除术语">
                                    <Trash2 size={14} />
                                  </button>
                                </div>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
                {terms.isFetchedAfterMount && !terms.isError && rows.length === 0 && (
                  <p className="py-8 text-center text-sm text-(--color-muted)">暂无术语（一键直出会自动提取，或手动添加）</p>
                )}
              </div>
            );
          })()}

          {tab === "storylines" && (
          <div className="relative flex h-full">
            {/* 章节目录（可收起） */}
            {!storyCollapsed && (
              <div className="toc-hide-scroll w-[179px] shrink-0 overflow-auto py-3">
                <p className="mb-1 px-3 text-xs font-medium text-(--color-muted)">章节目录</p>
                {(storylines.data ?? []).map((s) => (
                  <button
                    key={s.id}
                    onClick={() => {
                      const el = storyBodyRef.current?.querySelector<HTMLElement>(`[data-story-chapter="${s.chapter_id}"]`);
                      el?.scrollIntoView({ behavior: "smooth", block: "start" });
                    }}
                    className={`block w-full truncate rounded-lg px-3 py-1.5 text-left text-sm transition-colors hover:text-(--color-accent) ${
                      storyActiveChapter === s.chapter_id
                        ? "font-medium text-(--color-accent)"
                        : "text-(--color-text)"
                    }`}
                    title={chapterLabel(s.chapter_no, s.chapter_title)}
                  >
                    {chapterLabel(s.chapter_no, s.chapter_title)}
                  </button>
                ))}
                {storylines.isFetchedAfterMount && !storylines.isError && (storylines.data ?? []).length === 0 && (
                  <p className="px-3 py-4 text-xs text-(--color-muted)">暂无剧情卡（一键直出会自动生成）</p>
                )}
              </div>
            )}
            <div
              className={`relative w-4 shrink-0 ${storyCollapsed ? "" : "border-r border-(--color-border)"}`}
            >
              <button
                onClick={() => setStoryCollapsed((v) => !v)}
                title={storyCollapsed ? "展开章节目录" : "收起章节目录"}
                className={`absolute top-1/2 -translate-y-1/2 cursor-pointer text-(--color-muted) hover:text-(--color-accent) ${
                  storyCollapsed ? "left-0.5" : "right-0"
                }`}
              >
                {storyCollapsed ? <ChevronRight size={15} /> : <ChevronLeft size={15} />}
              </button>
            </div>
            <div
              ref={storyBodyRef}
              onScroll={() => {
                const c = storyBodyRef.current;
                if (!c) return;
                const st = c.scrollTop;
                let current: string | null = null;
                for (const a of storyAnchorsRef.current) {
                  if (a.top >= st) {
                    current = a.chapterId;
                    break;
                  }
                }
                startTransition(() => setStoryActiveChapter(current));
              }}
              className="relative flex-1 overflow-auto p-4"
            >
              <div className="mx-auto max-w-4xl space-y-3">
                {(storylines.data ?? []).map((s) => {
                  const card = parseStory(s.card_json);
                  return (
                    <div key={s.id} data-story-chapter={s.chapter_id} className="rounded-xl border border-(--color-border) bg-(--color-panel) px-4 py-3">
                      <div className="flex items-center gap-2">
                        <span className="font-semibold">{chapterLabel(s.chapter_no, s.chapter_title)}</span>
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
                          {card.key_events.length > 0 ? (
                            <ul className="mt-1 list-disc space-y-0.5 pl-4">
                              {card.key_events.map((e, i) => <li key={i}>{e}</li>)}
                            </ul>
                          ) : (
                            <p className="mt-1 text-(--color-muted)">暂无</p>
                          )}
                        </div>
                        <div>
                          <p className="text-xs font-medium text-amber-600">伏笔（翻译时勿剧透）</p>
                          {card.foreshadowing.length > 0 ? (
                            <ul className="mt-1 list-disc space-y-0.5 pl-4">
                              {card.foreshadowing.map((e, i) => <li key={i}>{e}</li>)}
                            </ul>
                          ) : (
                            <p className="mt-1 text-(--color-muted)">暂无</p>
                          )}
                          {s.chapter_no !== 1 && (
                            <>
                              <p className="mt-2 text-xs font-medium text-(--color-muted)">前情依赖</p>
                              {card.prior_deps.length > 0 ? (
                                <ul className="mt-1 list-disc space-y-0.5 pl-4">
                                  {card.prior_deps.map((e, i) => <li key={i}>{e}</li>)}
                                </ul>
                              ) : (
                                <p className="mt-1 text-(--color-muted)">暂无</p>
                              )}
                            </>
                          )}
                        </div>
                      </div>
                    </div>
                  );
                })}
                {storylines.isFetchedAfterMount && !storylines.isError && (storylines.data ?? []).length === 0 && (
                  <p className="py-8 text-center text-sm text-(--color-muted)">暂无剧情卡（一键直出会自动生成）</p>
                )}
              </div>
            </div>
            {!storylines.isFetchedAfterMount && (
              <div className="pointer-events-none absolute inset-0 flex items-center justify-center text-(--color-muted)">
                <Loader2 size={22} className="animate-spin" />
              </div>
            )}
            {storylines.isFetchedAfterMount && storylines.isError && (
              <div className="absolute inset-0 flex items-center justify-center text-sm text-(--color-muted)">
                章节剧情加载失败
              </div>
            )}
          </div>
          )}

          {tab === "states" && (
            <div className="mx-auto max-w-5xl space-y-6">
              <section>
                <h2 className="mb-2 text-sm font-semibold">状态时间线</h2>
                <div className="space-y-2">
                  {(characterStates.data ?? []).map((row) => {
                    const s = row.state || {};
                    const fields = [
                      ["身份", s.identity],
                      ["与主角关系", s.relationship_to_protagonist],
                      ["阵营", s.faction],
                      ["情绪", s.emotion],
                      ["称呼", s.form_of_address],
                    ].filter(([, value]) => String(value || "").trim());
                    return (
                      <div key={row.id} className="rounded-xl border border-(--color-border) bg-(--color-panel) px-4 py-3">
                        <div className="flex items-center gap-2">
                          <span className="font-medium">{row.character}</span>
                          <span className="text-xs text-(--color-muted)">{chapterLabel(row.chapter_no, null)}</span>
                          <span className="ml-auto text-[11px] text-(--color-muted)">v{row.version}</span>
                        </div>
                        {fields.length > 0 && (
                          <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-xs">
                            {fields.map(([label, value]) => (
                              <span key={label}>
                                <span className="text-(--color-muted)">{label}：</span>{String(value)}
                              </span>
                            ))}
                          </div>
                        )}
                        {Array.isArray(s.known_secrets) && s.known_secrets.length > 0 && (
                          <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs">
                            <span className="text-(--color-muted)">已知秘密：</span>
                            {s.known_secrets.map((secret) => (
                              <span key={secret} className="rounded-md bg-(--color-panel2) px-1.5 py-0.5">{secret}</span>
                            ))}
                          </div>
                        )}
                        {s.summary && <p className="mt-2 text-xs text-(--color-muted)">{s.summary}</p>}
                      </div>
                    );
                  })}
                  {characterStates.isFetchedAfterMount && !characterStates.isError && (characterStates.data ?? []).length === 0 && (
                    <p className="py-6 text-center text-sm text-(--color-muted)">一键直出后生成章节状态</p>
                  )}
                </div>
              </section>

              <section>
                <h2 className="mb-2 text-sm font-semibold">关键事实</h2>
                <div className="space-y-1.5">
                  {(storyFacts.data ?? []).map((fact) => (
                    <div key={fact.id} className="flex gap-3 rounded-lg px-3 py-2 text-sm hover:bg-(--color-panel)">
                      <span className="w-16 shrink-0 text-xs text-(--color-muted)">{chapterLabel(fact.chapter_no, null)}</span>
                      <span className="shrink-0 rounded-md bg-(--color-panel2) px-1.5 py-0.5 text-[11px] text-(--color-muted)">
                        {FACT_LABELS[fact.fact_type] ?? fact.fact_type}
                      </span>
                      <span className="min-w-0">{fact.subject ? `${fact.subject}：` : ""}{fact.text}</span>
                    </div>
                  ))}
                  {storyFacts.isFetchedAfterMount && !storyFacts.isError && (storyFacts.data ?? []).length === 0 && (
                    <p className="py-4 text-center text-sm text-(--color-muted)">暂无关键事实</p>
                  )}
                </div>
              </section>

              {!characterStates.isFetchedAfterMount && (
                <div className="flex justify-center py-4 text-(--color-muted)">
                  <Loader2 size={20} className="animate-spin" />
                </div>
              )}
            </div>
          )}

        </div>
        )}
      </div>

      {/* 术语编辑浮层 */}
      {editingTerm && (() => {
        const data = (() => { try { return JSON.parse(editingTerm.data_json || "{}"); } catch { return {}; } })();
        const conflicts = Array.isArray(data.translation_conflicts) ? data.translation_conflicts : [];
        return (
          <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/30 p-6" onClick={() => setEditingTerm(null)}>
            <div className="flex w-full max-w-lg flex-col rounded-2xl bg-(--color-bg) p-5 shadow-2xl" onClick={(e) => e.stopPropagation()}>
              <div className="flex items-center justify-between">
                <h2 className="text-base font-semibold">编辑术语 · {editingTerm.canonical_key}</h2>
                <button onClick={() => setEditingTerm(null)} className="rounded-lg p-1.5 text-(--color-muted) hover:bg-(--color-panel2)"><X size={16} /></button>
              </div>
              {conflicts.length > 0 && (
                <p className="mt-3 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-700">发现译名冲突：{conflicts.join(" / ")}</p>
              )}
              <div className="mt-4 space-y-3">
                <label className="block text-xs text-(--color-muted)">
                  标准译名
                  <input value={termDraft.translation} onChange={(e) => setTermDraft((d) => ({ ...d, translation: e.target.value }))}
                    className="mt-1 block w-full rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-1.5 font-mono text-sm outline-none focus:border-(--color-accent)" />
                </label>
                <div className="grid grid-cols-2 gap-3">
                  <label className="block text-xs text-(--color-muted)">
                    类型
                    <select value={termDraft.term_kind} onChange={(e) => setTermDraft((d) => ({ ...d, term_kind: e.target.value }))}
                      className="mt-1 block w-full rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-1.5 text-sm outline-none focus:border-(--color-accent)">
                      {Object.entries(TERM_KIND_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                    </select>
                  </label>
                  <label className="block text-xs text-(--color-muted)">
                    策略
                    <select value={termDraft.enforcement} onChange={(e) => setTermDraft((d) => ({ ...d, enforcement: e.target.value }))}
                      className="mt-1 block w-full rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-1.5 text-sm outline-none focus:border-(--color-accent)">
                      <option value="exact">强制统一</option>
                      <option value="lexical">词形参考</option>
                      <option value="contextual">按语境参考</option>
                    </select>
                  </label>
                </div>
                <label className="block text-xs text-(--color-muted)">
                  合法词形（多个用 / 分隔）
                  <input value={termDraft.variants} onChange={(e) => setTermDraft((d) => ({ ...d, variants: e.target.value }))}
                    placeholder="如：Pandora Source Codes"
                    className="mt-1 block w-full rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-1.5 font-mono text-sm outline-none focus:border-(--color-accent)" />
                </label>
                <label className="block text-xs text-(--color-muted)">
                  状态
                  <select value={termDraft.status} onChange={(e) => setTermDraft((d) => ({ ...d, status: e.target.value }))}
                    className="mt-1 block w-full rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-1.5 text-sm outline-none focus:border-(--color-accent)">
                    <option value="auto_approved">自动启用</option>
                    <option value="approved">已启用</option>
                    <option value="candidate">候选</option>
                    <option value="rejected">已忽略</option>
                  </select>
                </label>
              </div>
              <div className="mt-5 flex justify-end gap-2">
                <button onClick={() => setEditingTerm(null)} className="rounded-lg border border-(--color-border) bg-(--color-bg) px-4 py-1.5 text-sm hover:bg-(--color-panel2)">取消</button>
                <button onClick={saveTermEdit} disabled={patchTerm.isPending}
                  className="rounded-lg bg-(--color-accent) px-4 py-1.5 text-sm text-white hover:bg-(--color-accent-hover) disabled:opacity-50">
                  {patchTerm.isPending ? "保存中…" : "保存"}
                </button>
              </div>
            </div>
          </div>
        );
      })()}

      {/* 角色档案编辑浮层 */}
      {editingChar && (
        <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/30 p-6" onClick={() => setEditingChar(null)}>
          <div className="flex h-[520px] w-full max-w-xl flex-col rounded-2xl bg-(--color-bg) p-5 shadow-2xl" onClick={(e) => e.stopPropagation()}>
            <div className="flex shrink-0 items-center justify-between">
              <h2 className="text-base font-semibold">编辑角色档案 · {characterDisplayName(editingChar.canonical_key)}</h2>
              <button onClick={() => setEditingChar(null)} className="rounded-lg p-1.5 text-(--color-muted) hover:bg-(--color-panel2)"><X size={16} /></button>
            </div>
            <div className="mt-4 flex-1 space-y-3 overflow-auto pr-1">
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
            <div className="mt-4 flex shrink-0 justify-end gap-2">
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
          <div className="flex h-[520px] w-full max-w-xl flex-col rounded-2xl bg-(--color-bg) p-5 shadow-2xl" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between">
              <h2 className="text-base font-semibold">
                编辑剧情卡 · {chapterLabel(editingStory.chapter_no, editingStory.chapter_title)}
              </h2>
              <button onClick={() => setEditingStory(null)} className="rounded-lg p-1.5 text-(--color-muted) hover:bg-(--color-panel2)"><X size={16} /></button>
            </div>
            <div className="mb-2 mt-4 flex shrink-0 gap-1.5">
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
              placeholder={storyActiveField === "summary" ? "本章内容摘要" : "每行一条"}
              className="min-h-0 w-full flex-1 resize-none rounded-lg border border-(--color-border) bg-(--color-bg) px-3 py-2 text-sm leading-6 outline-none focus:border-(--color-accent)"
            />
            <div className="mt-4 flex shrink-0 justify-end gap-2">
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
