"""台词本服务：从解析器产出的 dialogue 片段生成纯对白流水。

- 抽取不再调用 LLM，直接复用 segment_type/speaker_name/章节/场景；
- 台词必须是片段原文的连续子串；
- 只有多行对白无法按行数一一对应时，才让模型做歧义对齐；
- 编辑回写：在所属片段 source_text 中替换原台词，触发该段译文 stale。
"""

import asyncio
import json
import re

from sqlalchemy import select

from app.llm.json_utils import parse_llm_json
from app.llm.protocol import LLMProvider
from app.models.dialogue import DialogueLine
from app.models.document import SourceSegment
from app.models.knowledge import KnowledgeAlias, KnowledgeItem
from app.models.translation import Translation

BATCH_SEGMENTS = 10
CONCURRENCY = 8

SYSTEM = """Extract dialogue lines from Chinese screenplay segments. Output ONLY JSON, no commentary:
{"lines":[{"segment_id":"...","speaker":"...","speaker_alias":null,"text":"exact substring"}]}
speaker: canonical name (normalize aliases like 玄德->刘备); 旁白/未知 if unknown. speaker_alias: the original form used in text if different from canonical, else null. text: verbatim substring, no quotes."""


def _norm(text: str) -> str:
    """归一化：去引号/空白，便于子串校验。"""
    t = re.sub(r"[\u3000\s]+", "", text)
    t = t.strip("「」『』“”‘’\"'：:。…—")
    return t


def _is_verbatim(source: str, text: str) -> bool:
    """校验台词为原文连续子串（先精确、再去引号、最后子序列兜底）。"""
    src = _norm(source)
    t = _norm(text)
    if not t:
        return False
    if t in src:
        return True
    # 子序列兜底：允许抽取时漏掉个别标点
    it = iter(src)
    return all(any(ch == c for c in it) for ch in t)


def _characters(project_id: str, db) -> list[dict]:
    """知识库角色：canonical + aliases，用于说话人归一化。"""
    rows = list(
        db.scalars(
            select(KnowledgeItem).where(
                KnowledgeItem.project_id == project_id,
                KnowledgeItem.item_type == "character",
                KnowledgeItem.status.in_(["approved", "auto_approved"]),
            )
        )
    )
    from app.models.knowledge import KnowledgeAlias

    aliases = list(
        db.scalars(
            select(KnowledgeAlias).where(
                KnowledgeAlias.knowledge_id.in_([k.id for k in rows])
            )
        )
    )
    alias_map = {}
    for a in aliases:
        alias_map.setdefault(a.knowledge_id, []).append(a.alias)
    return [
        {
            "canonical": k.canonical_key,
            "aliases": alias_map.get(k.id, []),
            "data": json.loads(k.data_json or "{}"),
        }
        for k in rows
    ]


def _batch_prompt(segs: list[SourceSegment], characters: list[dict]) -> str:
    char_block = "\n".join(
        f"- {c['canonical']}" + (f"（{'、'.join(c['aliases'])}）" if c["aliases"] else "")
        for c in characters
    )
    seg_block = "\n\n".join(f"[{s.id}]\n{s.source_text}" for s in segs)
    return "CHARACTERS:\n" + (char_block or "(none)") + "\n\nSEGMENTS:\n" + seg_block


async def _extract_batch(provider: LLMProvider, model: str, segs, characters) -> list[dict]:
    user = _batch_prompt(segs, characters)
    resp = await provider.chat(
        model=model,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        response_schema={"type": "object"},
        temperature=0.1,
        stage="dialogue_extract",
    )
    data = parse_llm_json(resp.content)
    return data.get("lines", []) if isinstance(data, dict) else []


async def extract_dialogue_lines(
    db_factory, project_id: str, source_document_id: str, provider: LLMProvider,
    model: str, progress_cb=None,
) -> dict:
    """台词本抽取：直接复用解析器的 dialogue 类型与 speaker_name，不再调用模型。

    解析器已经把对白和非对白分开；再让模型重新识别只会增加成本并引入
    重复台词、丢句和章节错乱。
    """
    import re as _re

    with db_factory() as db:
        segs = list(
            db.scalars(
                select(SourceSegment)
                .where(
                    SourceSegment.source_document_id == source_document_id,
                    SourceSegment.segment_type == "dialogue",
                )
                .order_by(SourceSegment.segment_no)
            )
        )
        char_rows = list(
            db.scalars(
                select(KnowledgeItem).where(
                    KnowledgeItem.project_id == project_id,
                    KnowledgeItem.item_type == "character",
                    KnowledgeItem.status.in_(["approved", "auto_approved"]),
                )
            )
        )
    alias_to_canonical: dict[str, str] = {}
    for c in char_rows:
        alias_to_canonical[c.canonical_key] = c.canonical_key
    if char_rows:
        with db_factory() as db:
            aliases = list(
                db.scalars(
                    select(KnowledgeAlias).where(
                        KnowledgeAlias.knowledge_id.in_([c.id for c in char_rows])
                    )
                )
            )
        owner = {c.id: c.canonical_key for c in char_rows}
        for a in aliases:
            if a.knowledge_id in owner:
                alias_to_canonical[a.alias] = owner[a.knowledge_id]

    with db_factory() as db:
        db.query(DialogueLine).filter(
            DialogueLine.source_document_id == source_document_id
        ).delete()
        db.commit()

    line_no_by_scene: dict[str, int] = {}
    inserted = 0
    for seg in segs:
        speaker = (seg.speaker_name or "未知").strip()
        speaker = alias_to_canonical.get(speaker, speaker)
        raw_lines = [x.strip() for x in (seg.source_text or "").splitlines() if x.strip()]
        for raw in raw_lines:
            # 保留原文连续子串；若行首带「角色：」，只去掉角色标签作为台词内容。
            text = raw
            m = _re.match(r"^\s*([^：:]{1,24})\s*[：:]\s*(.*)$", raw)
            if m:
                text = m.group(2).strip() or m.group(1).strip()
            if not text or text not in raw:
                continue
            key = seg.scene_id or seg.chapter_id or "-"
            line_no = line_no_by_scene.get(key, 0) + 1
            line_no_by_scene[key] = line_no
            with db_factory() as db:
                db.add(
                    DialogueLine(
                        project_id=project_id,
                        source_document_id=source_document_id,
                        chapter_id=seg.chapter_id,
                        scene_id=seg.scene_id,
                        segment_id=seg.id,
                        speaker_name=speaker,
                        speaker_alias=None,
                        line_no=line_no,
                        source_text=text,
                    )
                )
                db.commit()
            inserted += 1
    result = {
        "extracted": inserted,
        "discarded": 0,
        "batches": 1 if segs else 0,
        "failed_batches": 0,
    }
    if progress_cb:
        progress_cb({
            "done_batches": 1 if segs else 0,
            "total_batches": 1 if segs else 0,
            "lines": inserted,
            "discarded": 0,
            "failed_batches": 0,
        })
    return result

def list_dialogue_lines(db, project_id: str) -> list[dict]:
    """按章节→场景→行号排序返回台词行（含所属片段当前译文，供聚合展示）。"""
    from sqlalchemy import func as _func

    from app.models.document import Chapter, Scene

    # 按章节号/场景号排序（chapter_id 是 UUID，字符串排序会乱序）
    rows = list(
        db.scalars(
            select(DialogueLine)
            .outerjoin(Chapter, DialogueLine.chapter_id == Chapter.id)
            .outerjoin(Scene, DialogueLine.scene_id == Scene.id)
            .where(DialogueLine.project_id == project_id)
            .order_by(
                _func.coalesce(Chapter.chapter_no, 999999),
                _func.coalesce(Scene.scene_no, 999999),
                DialogueLine.line_no,
            )
        )
    )
    chapter_meta: dict[str, tuple[int | None, str | None]] = {}
    scene_meta: dict[str, tuple[int | None, str | None]] = {}
    if rows:
        ch_rows = list(
            db.scalars(
                select(Chapter).where(
                    Chapter.id.in_([r.chapter_id for r in rows if r.chapter_id])
                )
            )
        )
        chapter_meta = {c.id: (c.chapter_no, c.title) for c in ch_rows}
        sc_rows = list(
            db.scalars(
                select(Scene).where(
                    Scene.id.in_([r.scene_id for r in rows if r.scene_id])
                )
            )
        )
        scene_meta = {s.id: (s.scene_no, s.heading) for s in sc_rows}
    if not rows:
        return []
    seg_ids = [r.segment_id for r in rows if r.segment_id]
    trans_rows = list(
        db.scalars(
            select(Translation)
            .where(
                Translation.segment_id.in_(seg_ids),
                Translation.stage == "polished",
                Translation.freshness_status == "current",
            )
            .order_by(Translation.version.desc())
        )
    )
    latest: dict[str, Translation] = {}
    for t in trans_rows:
        latest.setdefault(t.segment_id, t)
    out = []
    # 同片段拆出的多句台词：整段译文只在片段首行展示，避免重复噪音
    shown_seg: set[str] = set()
    for r in rows:
        t = latest.get(r.segment_id)
        ch_no, ch_title = chapter_meta.get(r.chapter_id, (None, None))
        sc_no, sc_heading = scene_meta.get(r.scene_id, (None, None))
        t = latest.get(r.segment_id)
        first_of_seg = r.segment_id and r.segment_id not in shown_seg
        if first_of_seg:
            shown_seg.add(r.segment_id)
        out.append(
            {
                "id": r.id,
                "chapter_id": r.chapter_id,
                "chapter_no": ch_no,
                "chapter_title": ch_title,
                "scene_id": r.scene_id,
                "scene_no": sc_no,
                "scene_heading": sc_heading,
                "segment_id": r.segment_id,
                "speaker_name": r.speaker_name,
                "speaker_alias": r.speaker_alias,
                "line_no": r.line_no,
                "source_text": r.source_text,
                "target_text": r.target_text,
                "segment_translation": t.target_text if (t and first_of_seg) else None,
            }
        )
    return out


def update_dialogue_line(db, line_id: str, text: str | None, speaker_name: str | None) -> dict:
    """编辑台词行：文本变更回写所属片段原文（触发待重译）。"""
    line = db.get(DialogueLine, line_id)
    if line is None:
        return {"error": "台词行不存在"}
    if speaker_name is not None:
        line.speaker_name = (speaker_name or "").strip()[:64] or "未知"
    wrote_back = False
    if text is not None and text != line.source_text and line.segment_id:
        seg = db.get(SourceSegment, line.segment_id)
        if seg is not None:
            idx = seg.source_text.find(line.source_text)
            if idx >= 0:
                seg.source_text = (
                    seg.source_text[:idx] + text + seg.source_text[idx + len(line.source_text) :]
                )
                # 该段译文全部 stale（原文变更，需重译）
                for t in db.scalars(
                    select(Translation).where(Translation.segment_id == seg.id)
                ):
                    t.freshness_status = "stale"
                wrote_back = True
                line.source_text = text
    db.commit()
    return {
        "ok": True,
        "wrote_back": wrote_back,
        "warning": None if wrote_back else "未在片段原文中找到原台词，仅更新台词本本身",
    }


ALIGN_SYSTEM = """You align Chinese dialogue lines to their English translations.
For each Chinese line, extract the EXACT English sentence(s) from the translation that correspond to it.
RULES:
- Output ONLY JSON, no commentary: {"alignments":[{"line_index":0,"english":"exact substring from translation"}]}
- english MUST be a verbatim substring of the given translation. Do NOT rewrite or translate.
- A line may map to one or more consecutive sentences; join them with a single space.
- If a Chinese line is narration, stage direction, or has no English counterpart, omit it.
- The Chinese lines are in order; the English translation preserves the same order."""


def _align_prompt(chunk_lines: list[dict], seg_src: str, seg_tgt: str) -> str:
    lines_block = "\n".join(f"[{i}] {ln['source_text']}" for i, ln in enumerate(chunk_lines))
    return (
        "CHINESE LINES:\n" + lines_block
        + "\n\nSEGMENT SOURCE:\n" + seg_src
        + "\n\nTRANSLATION:\n" + seg_tgt
    )


async def _align_one(provider: LLMProvider, model: str, payload: dict):
    resp = await provider.chat(
        model=model,
        messages=[
            {"role": "system", "content": ALIGN_SYSTEM},
            {"role": "user", "content": payload["user"]},
        ],
        response_schema={"type": "object"},
        temperature=0.1,
        stage="dialogue_align",
    )
    data = parse_llm_json(resp.content)
    return payload, data.get("alignments", []) if isinstance(data, dict) else []


def _norm_en(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


async def align_dialogue_translations(
    db_factory, project_id: str, source_document_id: str, provider: LLMProvider,
    model: str, progress_cb=None,
) -> dict:
    """翻译完成后：把每句台词的英文从片段译文中逐字摘录对齐。

    - 单句片段直接用整段译文；
    - 多句片段由 LLM 对齐（摘录必须是译文字面连续子串，不改写）；
    - 对齐失败/无译文的行保持 target_text=null。
    """
    from app.models.translation import Translation

    with db_factory() as db:
        seg_rows = list(
            db.scalars(
                select(SourceSegment).where(
                    SourceSegment.source_document_id == source_document_id,
                    SourceSegment.segment_type == "dialogue",
                ).order_by(SourceSegment.segment_no)
            )
        )
        lines = list(
            db.scalars(
                select(DialogueLine)
                .where(DialogueLine.source_document_id == source_document_id)
                .order_by(DialogueLine.line_no)
            )
        )
        line_by_seg: dict[str, list[DialogueLine]] = {}
        for ln in lines:
            line_by_seg.setdefault(ln.segment_id or "", []).append(ln)
        seg_map = {s.id: s for s in seg_rows}
        # 当前 polished 译文
        trans_rows = list(
            db.scalars(
                select(Translation)
                .where(
                    Translation.segment_id.in_([s.id for s in seg_rows]),
                    Translation.stage == "polished",
                    Translation.freshness_status == "current",
                ).order_by(Translation.version.desc())
            )
        )
        latest: dict[str, Translation] = {}
        for t in trans_rows:
            latest.setdefault(t.segment_id, t)

    jobs = []
    single = 0
    for seg in seg_rows:
        seg_lines = line_by_seg.get(seg.id, [])
        if not seg_lines:
            continue
        tr = latest.get(seg.id)
        if tr is None or not (tr.target_text or "").strip():
            continue
        if len(seg_lines) == 1:
            seg_lines[0].target_text = tr.target_text.strip()
            single += 1
            continue
        tgt_lines = [x.strip() for x in (tr.target_text or "").splitlines() if x.strip()]
        if len(tgt_lines) == len(seg_lines):
            # 行数完全一致时按行号确定性对齐，零模型调用。
            for ln, text in zip(seg_lines, tgt_lines):
                ln.target_text = text
            single += len(seg_lines)
            continue
        jobs.append(
            {
                "seg": seg,
                "lines": seg_lines,
                "src": seg.source_text,
                "tgt": tr.target_text,
            }
        )

    sem = asyncio.Semaphore(10)

    async def one(job):
        async with sem:
            for attempt in range(3):
                try:
                    payload = {"user": _align_prompt(
                        [{"source_text": ln.source_text} for ln in job["lines"]],
                        job["src"], job["tgt"],
                    )}
                    _payload, aligns = await _align_one(provider, model, payload)
                    tgt_norm = _norm_en(job["tgt"])
                    for a in aligns:
                        try:
                            idx = int(a.get("line_index"))
                        except (TypeError, ValueError):
                            continue
                        en = _norm_en(a.get("english"))
                        # 硬校验：必须是译文的连续子串
                        if en and en in tgt_norm:
                            if 0 <= idx < len(job["lines"]):
                                job["lines"][idx].target_text = en
                    return
                except Exception:  # noqa: BLE001
                    if attempt == 2:
                        return
                    await asyncio.sleep(4 * (attempt + 1))

    tasks = [asyncio.create_task(one(j)) for j in jobs]
    done = 0
    for fut in asyncio.as_completed(tasks):
        await fut
        done += 1
        if progress_cb:
            progress_cb({"done": done, "total": len(jobs) + single})
    aligned = 0
    with db_factory() as db:
        touched = 0
        for seg in seg_rows:
            for ln in line_by_seg.get(seg.id, []):
                t = db.get(DialogueLine, ln.id)
                if t is not None and t.target_text != ln.target_text:
                    t.target_text = ln.target_text
                    touched += 1
        db.commit()
    for seg in seg_rows:
        for ln in line_by_seg.get(seg.id, []):
            if ln.target_text:
                aligned += 1
    if progress_cb:
        progress_cb({"done": len(jobs) + single, "total": len(jobs) + single})
    return {"aligned": aligned, "total_lines": len(lines), "segments": len(jobs) + single}
