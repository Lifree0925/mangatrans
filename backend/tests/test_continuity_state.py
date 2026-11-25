"""章节级人物状态与关键事实：只允许当前章节及以前的知识进入上下文。"""

import json

import app.core.database as dbmod
from app.models.continuity import CharacterState, StoryFact
from app.models.document import Chapter
from app.models.storyline import ChapterStoryline
from app.services import continuity_service
from app.services.context_builder import build_context, build_qa_reference


def _setup(client, wait_task) -> tuple[str, list[Chapter]]:
    pid = client.post("/api/v1/projects", json={"name": "状态测试"}).json()["id"]
    script = """第1集
1-1 咖啡厅 白天 内
Chloe：你来了。
第2集
2-1 街头 黑夜 外
Chloe：别跟着我。
第3集
3-1 庄园 白天 内
Chloe：我知道了。
"""
    r = client.post(
        f"/api/v1/projects/{pid}/documents/import",
        files={"file": ("s.txt", script.encode(), "text/plain")},
    )
    assert r.status_code == 201
    wait_task(client, client.post(f"/api/v1/projects/{pid}/parse", json={}).json()["task_id"])
    with dbmod.SessionLocal() as db:
        chapters = list(
            db.query(Chapter).filter(Chapter.project_id == pid).order_by(Chapter.chapter_no)
        )
        return pid, chapters


def _sync(client, pid: str, chapter: Chapter, card: dict) -> None:
    with dbmod.SessionLocal() as db:
        ch = db.get(Chapter, chapter.id)
        storyline = ChapterStoryline(
            project_id=pid,
            chapter_id=chapter.id,
            version=1,
            card_json=json.dumps(card, ensure_ascii=False),
            status="generated",
        )
        db.add(storyline)
        db.flush()
        continuity_service.sync_storyline(db, pid, ch, storyline, card)
        db.commit()


def test_future_character_state_and_story_fact_are_not_visible(client, wait_task) -> None:
    pid, chapters = _setup(client, wait_task)
    _sync(client, pid, chapters[0], {
        "summary": "Chloe 发现旧身份",
        "character_states": {"Chloe": "警觉"},
        "character_state_details": [{
            "name": "Chloe",
            "identity": "洗车工",
            "faction": "独立",
            "emotion": "警觉",
            "form_of_address": "Chloe",
            "known_secrets": ["Chloe 是 Ghost Zero"],
            "evidence": "第1集结尾",
        }],
        "key_events": ["Chloe 发现自己的旧代号"],
        "critical_facts": [{
            "fact_type": "location",
            "subject": "Chloe",
            "predicate": "works_at",
            "object_text": "Old Brooklyn Car Wash",
            "fact": "Chloe 在 Old Brooklyn Car Wash 工作",
        }],
    })
    _sync(client, pid, chapters[2], {
        "summary": "后期揭示",
        "character_states": {"Chloe": "震惊"},
        "character_state_details": [{
            "name": "Chloe",
            "known_secrets": ["Leo 是 Damian 的儿子"],
        }],
        "key_events": ["Leo 的身份被公开"],
    })

    with dbmod.SessionLocal() as db:
        states = continuity_service.visible_character_states(db, pid, 2)
        facts = continuity_service.active_story_facts(db, pid, 2)
        assert len(db.query(CharacterState).all()) == 2
        assert len(db.query(StoryFact).all()) >= 5

    chloe = next(s for s in states if s["name"] == "Chloe")
    assert "Chloe 是 Ghost Zero" in chloe["known_secrets"]
    assert "Leo 是 Damian 的儿子" not in chloe["known_secrets"]
    assert "Leo 的身份被公开" not in {f["text"] for f in facts}
    assert "Chloe 发现自己的旧代号" in {f["text"] for f in facts}
    assert any(f["type"] == "location" and "Old Brooklyn" in f["text"] for f in facts)

    context = build_context(
        segments=[{
            "segment_id": "S1", "segment_no": 1, "segment_type": "dialogue",
            "speaker_name": "Chloe", "source_text": "Chloe：你来了。\n",
        }],
        scene_card=None,
        knowledge=[{
            "id": "K1", "item_type": "character", "canonical_key": "Chloe",
            "aliases": [], "data": {"translation": "Chloe"},
        }],
        style_guide=None,
        carry_over=None,
        character_states=states,
        story_facts=facts,
    )
    assert "CHARACTER STATE @ CURRENT CHAPTER" in context.prompt_text
    assert "Chloe 是 Ghost Zero" in context.prompt_text
    assert "Leo 是 Damian 的儿子" not in context.prompt_text
    assert "Leo 的身份被公开" not in context.prompt_text

    reference = build_qa_reference(
        segments=[{
            "segment_id": "S1", "segment_type": "dialogue", "speaker_name": "Chloe",
            "source_text": "Chloe：你来了。\n",
        }],
        knowledge=[{
            "id": "K1", "item_type": "character", "canonical_key": "Chloe",
            "aliases": [], "data": {"translation": "Chloe"},
        }],
        character_states=states,
        story_facts=facts,
    )
    assert "Chloe 是 Ghost Zero" in reference
    assert "Leo 是 Damian 的儿子" not in reference


def test_storylines_backfill_states_and_api_lists_them(client, wait_task) -> None:
    pid, chapters = _setup(client, wait_task)
    with dbmod.SessionLocal() as db:
        db.add(ChapterStoryline(
            project_id=pid,
            chapter_id=chapters[1].id,
            version=1,
            card_json=json.dumps({
                "summary": "第二章",
                "character_states": {"Chloe": "怀疑"},
                "character_state_details": [{
                    "name": "Chloe", "emotion": "怀疑",
                    "form_of_address": "Miss Chloe",
                    "known_secrets": ["有人跟踪她"],
                }],
                "key_events": ["有人在街头跟踪 Chloe"],
            }, ensure_ascii=False),
            status="generated",
        ))
        db.commit()
        result = continuity_service.sync_project_continuity(db, pid)
        assert result["characters"] == 1
        assert result["facts"] >= 2

    states = client.get(f"/api/v1/projects/{pid}/character-states")
    facts = client.get(f"/api/v1/projects/{pid}/story-facts")
    assert states.status_code == 200
    assert facts.status_code == 200
    assert states.json()[0]["character"] == "Chloe"
    assert states.json()[0]["state"]["known_secrets"] == ["有人跟踪她"]
    assert any(x["text"] == "有人在街头跟踪 Chloe" for x in facts.json())


def test_reparse_clears_storyline_derived_continuity(client, wait_task) -> None:
    pid, chapters = _setup(client, wait_task)
    _sync(client, pid, chapters[0], {
        "summary": "第一章",
        "character_states": {"Chloe": "警觉"},
        "key_events": ["Chloe 发现跟踪者"],
    })
    r = client.post(f"/api/v1/projects/{pid}/parse", json={})
    assert r.status_code == 200
    wait_task(client, r.json()["task_id"])

    with dbmod.SessionLocal() as db:
        assert db.query(CharacterState).count() == 0
        assert db.query(StoryFact).count() == 0
        assert db.query(ChapterStoryline).count() == 0
