"""章节级人物状态与关键事实：从剧情卡确定性派生，不额外调用模型。"""

import json

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.continuity import CharacterState, StoryFact
from app.models.document import Chapter
from app.models.storyline import ChapterStoryline
from app.services.term_match import normalize

_STATE_FIELDS = (
    "identity",
    "relationship_to_protagonist",
    "faction",
    "emotion",
    "form_of_address",
    "summary",
)


def _clean_secrets(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(x).strip() for x in value if str(x).strip()))


def _detail_map(card: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for row in card.get("character_state_details") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if name:
            result[normalize(name)] = row
    return result


def _state_from_rows(name: str, summary: str, detail: dict | None) -> dict:
    detail = detail or {}
    state = {key: str(detail.get(key) or "").strip() for key in _STATE_FIELDS}
    state["summary"] = state["summary"] or str(summary or "").strip()
    state["known_secrets"] = _clean_secrets(detail.get("known_secrets"))
    state["evidence"] = str(detail.get("evidence") or "").strip()
    return state


def sync_storyline(db: Session, project_id: str, chapter: Chapter, storyline: ChapterStoryline,
                   card: dict) -> dict:
    """把一章剧情卡同步为人物状态和事实；同类旧 current 记录标 superseded。"""
    state_map = card.get("character_states") or {}
    if not isinstance(state_map, dict):
        state_map = {}
    details = _detail_map(card)

    names: list[str] = []
    for name in [*state_map.keys(), *[
        str(r.get("name") or "").strip()
        for r in (card.get("character_state_details") or [])
        if isinstance(r, dict)
    ]]:
        name = str(name or "").strip()
        if name and name not in names:
            names.append(name)

    # 本章重新生成/编辑后，旧 current 状态保留为历史，但不再参与上下文。
    db.execute(
        update(CharacterState)
        .where(
            CharacterState.project_id == project_id,
            CharacterState.chapter_id == chapter.id,
            CharacterState.status == "current",
        )
        .values(status="superseded")
    )

    for name in names:
        detail = details.get(normalize(name), {})
        state = _state_from_rows(name, str(state_map.get(name) or ""), detail)
        max_version = db.scalar(
            select(func.max(CharacterState.version)).where(
                CharacterState.project_id == project_id,
                CharacterState.character_key == name,
                CharacterState.chapter_id == chapter.id,
            )
        ) or 0
        db.add(CharacterState(
            project_id=project_id,
            chapter_id=chapter.id,
            source_storyline_id=storyline.id,
            character_key=name,
            chapter_no=chapter.chapter_no,
            version=int(max_version) + 1,
            state_json=json.dumps(state, ensure_ascii=False),
            status="current",
            source="storyline",
            evidence_json=json.dumps(
                {"storyline_id": storyline.id, "evidence": state.get("evidence", "")},
                ensure_ascii=False,
            ),
        ))

    # 事实按章节重建：只保留剧情卡派生事实，后续人工事实/规则可单独扩展。
    from sqlalchemy import delete

    db.execute(delete(StoryFact).where(
        StoryFact.project_id == project_id,
        StoryFact.chapter_id == chapter.id,
        StoryFact.source == "storyline",
    ))
    seen: set[tuple[str, str, str]] = set()

    def add_fact(fact_type: str, text: str, *, subject: str = "", predicate: str = "",
                 object_text: str = "", reveal_chapter_no: int | None = None) -> None:
        text = str(text or "").strip()
        key = (fact_type, subject, text)
        if not text or key in seen:
            return
        seen.add(key)
        db.add(StoryFact(
            project_id=project_id,
            chapter_id=chapter.id,
            source_storyline_id=storyline.id,
            chapter_no=chapter.chapter_no,
            fact_type=fact_type,
            subject=subject[:200],
            predicate=predicate[:200],
            object_text=object_text[:500],
            fact_text=text,
            valid_from_chapter_no=chapter.chapter_no,
            reveal_chapter_no=reveal_chapter_no if reveal_chapter_no is not None else chapter.chapter_no,
            status="current",
            source="storyline",
            evidence_json=json.dumps({"storyline_id": storyline.id}, ensure_ascii=False),
        ))

    for text in card.get("key_events") or []:
        add_fact("event", str(text))
    for text in card.get("prior_deps") or []:
        add_fact("prior_dependency", str(text))
    for text in card.get("foreshadowing") or []:
        add_fact("foreshadowing", str(text))
    for name in names:
        state = _state_from_rows(name, str(state_map.get(name) or ""), details.get(normalize(name), {}))
        if state.get("identity"):
            add_fact("identity", f"{name} 的身份：{state['identity']}", subject=name,
                     predicate="identity", object_text=state["identity"])
        if state.get("relationship_to_protagonist"):
            add_fact("relationship", f"{name} 与主角的关系：{state['relationship_to_protagonist']}",
                     subject=name, predicate="relationship_to_protagonist",
                     object_text=state["relationship_to_protagonist"])
        if state.get("faction"):
            add_fact("faction", f"{name} 的阵营：{state['faction']}", subject=name,
                     predicate="faction", object_text=state["faction"])
        if state.get("form_of_address"):
            add_fact("address", f"{name} 的称呼：{state['form_of_address']}", subject=name,
                     predicate="form_of_address", object_text=state["form_of_address"])
        for secret in state.get("known_secrets", []):
            add_fact(
                "secret", secret, subject=name, predicate="knows", object_text=secret,
                reveal_chapter_no=chapter.chapter_no,
            )
    allowed_types = {
        "relationship", "identity", "time", "location", "possession",
        "secret", "negation_condition", "event", "faction", "address",
    }
    for fact in card.get("critical_facts") or []:
        if not isinstance(fact, dict):
            continue
        fact_type = str(fact.get("fact_type") or "event").strip().lower()
        if fact_type not in allowed_types:
            fact_type = "event"
        reveal = fact.get("reveal_chapter_no")
        try:
            reveal_no = int(reveal) if reveal is not None else chapter.chapter_no
        except (TypeError, ValueError):
            reveal_no = chapter.chapter_no
        reveal_no = max(chapter.chapter_no, reveal_no)
        text = str(fact.get("fact") or "").strip()
        if not text:
            subject = str(fact.get("subject") or "").strip()
            predicate = str(fact.get("predicate") or "").strip()
            object_text = str(fact.get("object_text") or "").strip()
            text = " ".join(x for x in (subject, predicate, object_text) if x)
        add_fact(
            fact_type, text,
            subject=str(fact.get("subject") or ""),
            predicate=str(fact.get("predicate") or ""),
            object_text=str(fact.get("object_text") or ""),
            reveal_chapter_no=reveal_no,
        )
    return {"characters": len(names), "facts": len(seen)}


def sync_project_continuity(db: Session, project_id: str) -> dict:
    """回填已有剧情卡；已同步过的最新卡直接跳过。"""
    rows = list(db.scalars(
        select(ChapterStoryline)
        .where(ChapterStoryline.project_id == project_id)
        .order_by(ChapterStoryline.chapter_id, ChapterStoryline.version.desc())
    ))
    latest: dict[str, ChapterStoryline] = {}
    for row in rows:
        latest.setdefault(row.chapter_id, row)
    chars = facts = 0
    for storyline in latest.values():
        synced = db.scalar(
            select(CharacterState.id).where(
                CharacterState.source_storyline_id == storyline.id
            ).limit(1)
        ) or db.scalar(
            select(StoryFact.id).where(StoryFact.source_storyline_id == storyline.id).limit(1)
        )
        if synced:
            continue
        chapter = db.get(Chapter, storyline.chapter_id)
        if chapter is None:
            continue
        try:
            card = json.loads(storyline.card_json or "{}")
        except json.JSONDecodeError:
            continue
        result = sync_storyline(db, project_id, chapter, storyline, card)
        chars += result["characters"]
        facts += result["facts"]
    db.commit()
    return {"characters": chars, "facts": facts}


def visible_character_states(db: Session, project_id: str, chapter_no: int) -> list[dict]:
    """读取截至当前章节的最新人物状态；known_secrets 跨章节累积。"""
    rows = list(db.scalars(
        select(CharacterState)
        .where(
            CharacterState.project_id == project_id,
            CharacterState.status == "current",
            CharacterState.chapter_no <= chapter_no,
        )
        .order_by(CharacterState.chapter_no, CharacterState.character_key)
    ))
    merged: dict[str, dict] = {}
    for row in rows:
        try:
            state = json.loads(row.state_json or "{}")
        except json.JSONDecodeError:
            continue
        key = row.character_key
        target = merged.setdefault(key, {
            "name": key,
            "ids": [],
            "known_secrets": [],
            "chapters": [],
        })
        for field in _STATE_FIELDS:
            value = str(state.get(field) or "").strip()
            if value:
                target[field] = value
        target["known_secrets"] = list(dict.fromkeys(
            [*target["known_secrets"], *_clean_secrets(state.get("known_secrets"))]
        ))
        target["chapters"] = list(dict.fromkeys([*target["chapters"], row.chapter_no]))
        target["ids"] = list(dict.fromkeys([*target["ids"], row.id]))
        target["last_chapter"] = row.chapter_no
    return list(merged.values())


def active_story_facts(db: Session, project_id: str, chapter_no: int) -> list[dict]:
    """当前章节已生效事实；未来章节事实不会进入翻译上下文或 QA 基准。"""
    rows = list(db.scalars(
        select(StoryFact)
        .where(
            StoryFact.project_id == project_id,
            StoryFact.status == "current",
            StoryFact.valid_from_chapter_no <= chapter_no,
            (
                StoryFact.reveal_chapter_no.is_(None)
                | (StoryFact.reveal_chapter_no <= chapter_no)
            ),
        )
        .order_by(StoryFact.chapter_no, StoryFact.fact_type)
    ))
    return [
        {
            "id": row.id,
            "chapter": row.chapter_no,
            "type": row.fact_type,
            "subject": row.subject,
            "predicate": row.predicate,
            "object": row.object_text,
            "text": row.fact_text,
            "reveal_chapter": row.reveal_chapter_no,
        }
        for row in rows
        if row.valid_to_chapter_no is None or row.valid_to_chapter_no >= chapter_no
    ]
