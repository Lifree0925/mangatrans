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

SYSTEM = """You are a script dialogue extraction assistant for a Chinese screenplay.
Extract every spoken line from the supplied dialogue segments.

Rules:
- Split each segment into individual spoken lines (single utterance per line).
- For each line output:
  - "segment_id": the id of the segment the line belongs to
  - "speaker_name": the canonical character name (normalize aliases like 玄德 -> 刘备 using the provided character list; if the speaker cannot be determined use "旁白" for narration-style quotes or "未知" when genuinely unidentifiable)
  - "speaker_alias": the exact form of address used in the source text for this speaker (e.g. 玄德); null if same as canonical or unknown
  - "text": the EXACT verbatim substring of the source text, without surrounding quote marks, colon prefixes or stage directions
- Do NOT rewrite, paraphrase, merge or skip any spoken line.
- Keep lines in source order.
Return ONLY JSON: {"lines": [{"segment_id": "...", "speaker_name": "...", "speaker_alias": null, "text": "..."}]}"""


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
        f"- {c['canonical']}" + (f"（别名：{'、'.join(c['aliases'])}）" if c["aliases"] else "")
        for c in characters
    )
    seg_block = "\n\n".join(
        f"[{s.id}] (segment_no={s.segment_no}, scene={s.scene_id or '-'}, "
        f"speaker_hint={s.speaker_name or '-'})\n{s.source_text}"
        for s in segs
    )
    return (
        "CHARACTERS (normalize speakers to these canonical names):\n"
        + (char_block or "(none)")
        + "\n\nSEGMENTS:\n"
        + seg_block
    )


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
    db_factory, project_id: str, source_document_id: str, provider: LLMProvider, model: str
) -> dict:
    """全量抽取并落库（幂等：先删旧行）。返回统计。"""
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
        return {"extracted": 0, "discarded": 0, "batches": 0}

    batches = [segs[i : i + BATCH_SEGMENTS] for i in range(0, len(segs), BATCH_SEGMENTS)]
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(batch):
        async with sem:
            try:
                return await _extract_batch(provider, model, batch, characters)
            except Exception:  # noqa: BLE001 单批失败不阻断整本
                return []

    results = await asyncio.gather(*[one(b) for b in batches])
    seg_by_id = {s.id: s for s in segs}

    lines: list[dict] = []
    discarded = 0
    for batch_lines in results:
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
            speaker = (item.get("speaker_name") or "").strip()[:64] or "未知"
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
        "failed_batches": sum(1 for r in results if not r),
    }


def list_dialogue_lines(db, project_id: str) -> list[dict]:
    """按章节→场景→行号排序返回台词行（含所属片段当前译文，供聚合展示）。"""
    rows = list(
        db.scalars(
            select(DialogueLine)
            .where(DialogueLine.project_id == project_id)
            .order_by(
                DialogueLine.chapter_id, DialogueLine.scene_id, DialogueLine.line_no
            )
        )
    )
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
    for r in rows:
        t = latest.get(r.segment_id)
        out.append(
            {
                "id": r.id,
                "chapter_id": r.chapter_id,
                "scene_id": r.scene_id,
                "segment_id": r.segment_id,
                "speaker_name": r.speaker_name,
                "speaker_alias": r.speaker_alias,
                "line_no": r.line_no,
                "source_text": r.source_text,
                "segment_translation": t.target_text if t else None,
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
