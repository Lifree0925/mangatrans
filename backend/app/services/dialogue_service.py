"""台词本服务：LLM 抽取章节→场景→角色纯对白流水，编辑回写主线片段。

- 抽取发生在解析后（Express 准备阶段），分批并发；
- 台词必须是片段原文的连续子串（不允许改写原文）；
- 说话人归一化对照知识库角色档案，原文称呼存 speaker_alias；
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
from app.models.knowledge import KnowledgeItem
from app.models.translation import Translation

BATCH_SEGMENTS = 10
CONCURRENCY = 4

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
    )
    data = parse_llm_json(resp.content)
    return data.get("lines", []) if isinstance(data, dict) else []


async def extract_dialogue_lines(
    db_factory, project_id: str, source_document_id: str, provider: LLMProvider,
    model: str, progress_cb=None,
) -> dict:
    """全量抽取并落库（幂等：先删旧行）。progress_cb 每批回调用于实时进度。"""
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
        characters = _characters(project_id, db)
    if not segs:
        return {"extracted": 0, "discarded": 0, "batches": 0, "failed_batches": 0}

    batches = [segs[i : i + BATCH_SEGMENTS] for i in range(0, len(segs), BATCH_SEGMENTS)]
    seg_by_id = {s.id: s for s in segs}
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(batch, hold_sem: bool = True):
        """单批抽取：限流退避重试；仍失败则拆半递归（长思考耗爆预算的批次变小后可成功）。"""
        async def call():
            for attempt in range(3):
                try:
                    return await _extract_batch(provider, model, batch, characters)
                except Exception:  # noqa: BLE001
                    if attempt == 2:
                        if len(batch) <= 2:
                            return []
                        mid = len(batch) // 2
                        left = await one(batch[:mid], hold_sem=False)
                        right = await one(batch[mid:], hold_sem=False)
                        return left + right
                    await asyncio.sleep(4 * (attempt + 1))

        if hold_sem:
            async with sem:
                return await call()
        return await call()

    tasks = [asyncio.create_task(one(b)) for b in batches]
    lines: list[dict] = []
    discarded = 0
    failed_batches = 0
    done = 0
    for fut in asyncio.as_completed(tasks):
        batch_lines = await fut
        done += 1
        if not batch_lines:
            failed_batches += 1
        for item in batch_lines:
            sid = item.get("segment_id")
            seg = seg_by_id.get(sid)
            if seg is None:
                discarded += 1
                continue
            text = (item.get("text") or "").strip()
            if not _is_verbatim(seg.source_text, text):
                discarded += 1
                continue
            speaker = (item.get("speaker_name") or item.get("speaker") or "").strip()[:64] or "未知"
            # 归一化检查：speaker 若恰是知识库别名，替换为 canonical
            for c in characters:
                if speaker in c["aliases"]:
                    speaker = c["canonical"]
                    break
            lines.append(
                {
                    "segment_id": sid,
                    "speaker_name": speaker,
                    "speaker_alias": (item.get("speaker_alias") or None),
                    "text": text,
                }
            )
        if progress_cb:
            progress_cb(
                {
                    "done_batches": done,
                    "total_batches": len(batches),
                    "lines": len(lines),
                    "discarded": discarded,
                    "failed_batches": failed_batches,
                }
            )

    with db_factory() as db:
        db.query(DialogueLine).filter(
            DialogueLine.source_document_id == source_document_id
        ).delete()
        line_no_by_scene: dict[str, int] = {}
        for ln in lines:
            seg = seg_by_id[ln["segment_id"]]
            scene_key = seg.scene_id or seg.chapter_id or "-"
            line_no = line_no_by_scene.get(scene_key, 0) + 1
            line_no_by_scene[scene_key] = line_no
            db.add(
                DialogueLine(
                    project_id=project_id,
                    source_document_id=source_document_id,
                    chapter_id=seg.chapter_id,
                    scene_id=seg.scene_id,
                    segment_id=seg.id,
                    speaker_name=ln["speaker_name"],
                    speaker_alias=ln["speaker_alias"],
                    line_no=line_no,
                    source_text=ln["text"],
                )
            )
        db.commit()
    return {
        "extracted": len(lines),
        "discarded": discarded,
        "batches": len(batches),
        "failed_batches": failed_batches,
    }


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
        jobs.append(
            {
                "seg": seg,
                "lines": seg_lines,
                "src": seg.source_text,
                "tgt": tr.target_text,
            }
        )

    sem = asyncio.Semaphore(6)

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
