"""系统级一致性护栏：泛词、歧义别名、同一地点规范名、结构锚点、角色声音。"""

import json

from sqlalchemy import select

import app.core.database as dbmod
from app.models.document import SourceSegment
from app.models.knowledge import KnowledgeAlias, KnowledgeItem
from app.models.project import Project
from app.services.context_builder import build_context
from app.services.knowledge_service import prune_ambiguous_aliases
from app.services.term_match import (
    build_term_records,
    forced_term_specs,
    is_generic_source,
    select_forced_specs_for_text,
    select_source_matches,
)
from app.services.translation import normalize_translation_entries, validate_window
from app.services.translation_batch import apply_known_honorifics, apply_source_honorifics


def _term(key: str, en: str, aliases: list[str] | None = None, **data) -> dict:
    return {
        "id": key,
        "item_type": "term",
        "canonical_key": key,
        "aliases": aliases or [],
        "data": {"translation": en, **data},
    }


def test_generic_terms_never_forced():
    entries = [
        _term("庄园", "Rothschild Family Manor", term_kind="place", enforcement="exact"),
        _term("狼人", "Wolf Pack", term_kind="common_noun", enforcement="contextual"),
        _term("酒吧", "Blood Bar", term_kind="place", enforcement="exact"),
    ]
    records = build_term_records(entries)
    assert forced_term_specs(records, "狼人走进庄园的酒吧") == {}
    assert is_generic_source("庄园") is True
    assert is_generic_source("荒原边缘酒吧") is False


def test_ambiguous_alias_is_ignored_and_explicit_place_wins():
    entries = [
        _term("荒原边缘酒吧", "Wasteland Edge Bar", aliases=["酒吧"]),
        _term("血月酒吧", "Blood Moon Bar", aliases=["酒吧"]),
    ]
    records = build_term_records(entries)
    # 裸泛词「酒吧」有歧义，不能随手选一个具体地点。
    assert forced_term_specs(records, "他走进酒吧") == {}
    specs = forced_term_specs(records, "他走进荒原边缘酒吧")
    assert specs["荒原边缘酒吧"]["en"] == "Wasteland Edge Bar"
    assert "酒吧" not in specs


def test_short_and_full_place_use_one_english_name():
    entries = [
        _term("边缘酒吧", "Edge Bar"),
        _term("荒原边缘酒吧", "Wasteland Edge Bar"),
    ]
    records = build_term_records(entries)
    specs = forced_term_specs(records, "边缘酒吧外，荒原边缘酒吧的灯还亮着。")
    values = {spec["en"] for spec in specs.values()}
    assert values == {"Wasteland Edge Bar"}, specs


def test_validate_window_detects_scene_speaker_and_marker_shift():
    speaker_map = {"talia": "Talia", "elara": "Elara"}
    scene_src = {
        "segment_id": "s1", "segment_type": "heading", "speaker_name": None,
        "source_text": "1-1 瓦伦丁庄园 夜 内\n",
    }
    scene_errs = validate_window([scene_src], [{
        "segment_id": "s1", "target_text": "△ The castle at night.",
    }])
    assert any("场景号" in e for e in scene_errs)

    dialogue_src = {
        "segment_id": "s2", "segment_type": "dialogue", "speaker_name": "Talia",
        "source_text": "Talia（摇头）：不。\n",
    }
    speaker_errs = validate_window([dialogue_src], [{
        "segment_id": "s2", "target_text": "Elara (shaking her head): No.",
    }], speaker_map=speaker_map)
    assert any("说话人错位" in e for e in speaker_errs)

    narrative_src = {
        "segment_id": "s3", "segment_type": "narrative", "speaker_name": None,
        "source_text": "△她推门进入。\n【音效：门轴声】\n",
    }
    marker_errs = validate_window([narrative_src], [{
        "segment_id": "s3", "target_text": "She pushes the door open.\n[Door hinge creaks.]",
    }])
    assert any("动作标记" in e for e in marker_errs)


def test_sequential_alias_prune_removes_shared_and_generic_aliases(client):
    pid = client.post("/api/v1/projects", json={"name": "别名清理"}).json()["id"]
    with dbmod.SessionLocal() as db:
        project = db.get(Project, pid)
        a = KnowledgeItem(project_id=project.id, item_type="term", canonical_key="荒原边缘酒吧",
                          data_json=json.dumps({"translation": "Wasteland Edge Bar"}), status="auto_approved")
        b = KnowledgeItem(project_id=project.id, item_type="term", canonical_key="血月酒吧",
                          data_json=json.dumps({"translation": "Blood Moon Bar"}), status="auto_approved")
        c = KnowledgeItem(project_id=project.id, item_type="term", canonical_key="瓦伦丁庄园",
                          data_json=json.dumps({"translation": "Valentine Manor"}), status="auto_approved")
        db.add_all([a, b, c])
        db.flush()
        db.add_all([
            KnowledgeAlias(knowledge_id=a.id, alias="酒吧", status="candidate"),
            KnowledgeAlias(knowledge_id=b.id, alias="酒吧", status="candidate"),
            KnowledgeAlias(knowledge_id=c.id, alias="庄园", status="candidate"),
        ])
        db.commit()
        aid = a.id
        a_alias_id = db.scalar(select(KnowledgeAlias.id).where(
            KnowledgeAlias.knowledge_id == a.id, KnowledgeAlias.alias == "酒吧"
        ))
        assert a_alias_id

    with dbmod.SessionLocal() as db:
        removed = prune_ambiguous_aliases(db, pid)
        assert removed == 3
        rows = list(db.scalars(select(KnowledgeAlias)))
        assert rows == []


def test_context_injects_only_active_speaker_voice():
    knowledge = [
        {
            "id": "c1", "item_type": "character", "canonical_key": "Talia", "aliases": [],
            "data": {
                "translation": "Talia", "personality": "冷峻",
                "speech_style": "短句、命令式", "emotional_range": "愤怒、克制",
            },
        },
        {
            "id": "c2", "item_type": "character", "canonical_key": "Thomas", "aliases": [],
            "data": {
                "translation": "Thomas", "personality": "温和",
                "speech_style": "长句、礼貌", "emotional_range": "焦虑",
            },
        },
    ]
    result = build_context(
        segments=[{
            "segment_id": "s1", "segment_no": 1, "segment_type": "dialogue",
            "speaker_name": "Talia", "source_text": "Talia（压抑怒火）：够了。\n",
        }],
        scene_card=None,
        knowledge=knowledge,
        style_guide=None,
        carry_over=None,
        budget=6000,
    )
    text = result.prompt_text
    assert "SPEAKER VOICE & LOCAL PERFORMANCE" in text
    assert "短句、命令式" in text
    assert "压抑怒火" in text
    assert "长句、礼貌" not in text


def test_knowledge_merge_skips_generic_and_blocks_generic_alias(client):
    from app.schemas.knowledge import ExtractionOutput
    from app.services.knowledge_service import merge_candidates

    pid = client.post("/api/v1/projects", json={"name": "知识归并"}).json()["id"]
    out = ExtractionOutput.model_validate({"items": [
        {
            "canonical_key": "庄园", "item_type": "term",
            "aliases": ["私人庄园"], "data": {"translation": "Rothschild Family Manor"},
            "evidence_quotes": ["庄园里没人。"], "evidence_type": "explicit", "confidence": 0.9,
        },
        {
            "canonical_key": "荒原边缘酒吧", "item_type": "term",
            "aliases": ["酒吧", "边缘酒吧"],
            "data": {
                "translation": "Wasteland Edge Bar", "term_kind": "place", "enforcement": "exact",
            },
            "evidence_quotes": ["他在荒原边缘酒吧等她。"], "evidence_type": "explicit", "confidence": 0.9,
        },
    ]})
    source = "庄园里没人。他在荒原边缘酒吧等她。边缘酒吧外下着雨。"
    with dbmod.SessionLocal() as db:
        first = merge_candidates(db, pid, out, "express", source_text=source)
        second = merge_candidates(db, pid, out, "express", source_text=source)
    assert "庄园" not in first
    assert second  # 重复归并只更新，不报错
    with dbmod.SessionLocal() as db:
        items = list(db.scalars(select(KnowledgeItem).where(KnowledgeItem.project_id == pid)))
        names = [i.canonical_key for i in items]
        assert names.count("荒原边缘酒吧") == 1
        assert "庄园" not in names
        place = next(i for i in items if i.canonical_key == "荒原边缘酒吧")
        aliases = list(db.scalars(
            select(KnowledgeAlias.alias).where(KnowledgeAlias.knowledge_id == place.id)
        ))
        assert "酒吧" not in aliases
        assert "边缘酒吧" in aliases


def test_semantic_qa_prompt_contains_reference_without_style_noise():
    import asyncio
    from app.llm.fake import FakeProvider
    from app.services.qa_service import semantic_qa

    f = FakeProvider()
    seen: dict[str, str] = {}

    def h(messages):
        seen["prompt"] = messages[-1]["content"]
        return {"issues": []}

    f.on(h)
    asyncio.run(semantic_qa(
        f, "fake-model", "Elara走进酒吧。", "Elara walks into the bar.",
        reference='{"terms":[{"source":"荒原边缘酒吧","english":"Wasteland Edge Bar"}]}',
    ))
    prompt = seen["prompt"]
    assert "PROJECT REFERENCE" in prompt
    assert "Wasteland Edge Bar" in prompt
    assert "不把风格" in prompt
    assert "大小写" in prompt
    assert "naturalness_edits" in prompt


def test_polish_split_errors_retry_and_preserve_all_segments(client):
    """Polish 截断或上下文超限时自动拆半；最终仍覆盖所有段。"""
    import asyncio
    import re

    from app.llm.fake import FakeProvider
    from app.llm.protocol import LLMError
    from app.llm.registry import set_provider
    from app.models.task import Task
    from app.models.translation import ProviderConfig, Translation
    from app.pipeline.worker import run_once
    from conftest import fake_translation_entries

    with dbmod.SessionLocal() as db:
        db.add(ProviderConfig(
            model_name="fake-model", base_url="http://fake", credential_ref="test-ref",
            is_verified=True, is_default=True,
        ))
        db.commit()

    pid = client.post("/api/v1/projects", json={"name": "截断拆窗"}).json()["id"]
    script = "\n\n".join(f"夜色压下来，这是第{i}段动作描写。" for i in range(6))
    client.post(
        f"/api/v1/projects/{pid}/documents/import",
        files={"file": ("s.txt", script.encode(), "text/plain")},
    )
    # 导入与解析由独立任务完成；这里直接使用同步等待，避免依赖外部 fixture 的等待函数。
    import time
    task_id = client.post(f"/api/v1/projects/{pid}/parse", json={}).json()["task_id"]
    for _ in range(100):
        if client.get(f"/api/v1/tasks/{task_id}").json().get("status") in ("completed", "failed"):
            break
        time.sleep(0.05)

    f = FakeProvider()
    state = {"raised": False}

    def h(messages):
        content = messages[-1]["content"]
        system = messages[0]["content"] if messages else ""
        ids = re.findall(r"\[([0-9a-f]{32})\]", content)
        natural = "POLISH pass" in system
        if natural and len(set(ids)) > 1 and not state["raised"]:
            state["raised"] = True
            raise LLMError("模拟上下文超限", retryable=False, code="context_length")
        out = {
            "window_id": "W",
            "translations": fake_translation_entries(
                content, "EN", stage="polished" if natural else "draft"
            ),
        }
        if natural:
            out["carry_over"] = {
                "forms_of_address": [],
                "ongoing_state": "继续",
                "open_threads": [],
                "active_expressions": [],
            }
        return out

    set_provider(f.on(h))
    task_id = client.post(f"/api/v1/projects/{pid}/translation/start", json={}).json()["task_id"]
    for _ in range(80):
        asyncio.run(run_once())
        with dbmod.SessionLocal() as db:
            status = db.get(Task, task_id).status
        if status in ("completed", "failed"):
            break
    assert state["raised"] is True
    assert status == "completed"
    with dbmod.SessionLocal() as db:
        rows = list(db.scalars(select(Translation).where(Translation.stage == "polished")))
        seg_ids = [s.id for s in db.scalars(select(SourceSegment).where(SourceSegment.project_id == pid))]
    assert {r.segment_id for r in rows} == set(seg_ids)


def test_screenplay_marks_are_canonicalized():
    from app.services.translation import localize_screenplay_marks

    assert localize_screenplay_marks("Elara (OS): Run.") == "Elara (voice-over): Run."
    assert localize_screenplay_marks("Talia（O.S.）：走。") == "Talia(voice-over)：走。"
    assert localize_screenplay_marks("Lucian (voice-over): Stop.") == "Lucian (voice-over): Stop."


def test_qa_issues_for_window_are_replaced(client, wait_task):
    from app.models.qa import QAIssue
    from app.services.qa_service import clear_issues_for_segments
    from app.services.translation_batch import insert_translation_row

    pid = client.post("/api/v1/projects", json={"name": "QA清理"}).json()["id"]
    client.post(
        f"/api/v1/projects/{pid}/documents/import",
        files={"file": ("s.txt", "林晓：你好。\n".encode(), "text/plain")},
    )
    wait_task(client, client.post(f"/api/v1/projects/{pid}/parse", json={}).json()["task_id"])
    sid = client.get(f"/api/v1/projects/{pid}/segments").json()[0]["id"]
    with dbmod.SessionLocal() as db:
        t1 = insert_translation_row(db, segment_id=sid, stage="polished", target_text="Hi.")
        t2 = insert_translation_row(db, segment_id=sid, stage="polished", target_text="Hello.")
        db.add_all([
            QAIssue(project_id=pid, translation_id=t1.id, issue_type="forced_term", severity="high"),
            QAIssue(project_id=pid, translation_id=t2.id, issue_type="forced_term", severity="high"),
        ])
        db.commit()
    with dbmod.SessionLocal() as db:
        removed = clear_issues_for_segments(db, pid, [sid])
        assert removed == 2
        assert list(db.scalars(select(QAIssue))) == []


def test_qa_term_selection_uses_longest_match_per_segment():
    specs = {
        "狼王": {"en": "Wolf King"},
        "女狼王": {"en": "Wolf Queen"},
        "血族": {"en": "Blood Clan"},
        "血族议会": {"en": "Vampire Council"},
    }
    active = select_forced_specs_for_text(specs, "女狼王走进血族议会。")
    assert set(active) == {"女狼王", "血族议会"}
    active2 = select_forced_specs_for_text(specs, "狼王与女狼王对视，血族在旁。")
    assert set(active2) == {"狼王", "女狼王", "血族"}


def test_named_place_prefix_canonicalized_and_title_alias_not_forced():
    entries = [
        _term("瓦伦丁古堡", "Valentine Castle", term_kind="place", enforcement="exact"),
        _term("瓦伦丁古堡地牢", "Valentin Castle Dungeon", term_kind="place", enforcement="exact"),
        _term("狼王", "Wolf King", aliases=["狼王大人"], term_kind="title", enforcement="exact"),
    ]
    records = build_term_records(entries)
    by_key = {r.canonical: r for r in records}
    assert by_key["瓦伦丁古堡地牢"].en == "Valentine Castle Dungeon"
    assert forced_term_specs(records, "狼王大人来了") == {}
    match = select_source_matches(records, "狼王大人来了")[0]
    assert match["source_form"] == "狼王大人"
    assert match["force"] is False


def test_refresh_removes_obsolete_generic_term_alerts(client, wait_task):
    from app.models.qa import QAIssue
    from app.services.qa_service import refresh_forced_term_issues
    from app.services.translation_batch import insert_translation_row

    pid = client.post("/api/v1/projects", json={"name": "旧告警校准"}).json()["id"]
    client.post(
        f"/api/v1/projects/{pid}/documents/import",
        files={"file": ("s.txt", "狼人走进庄园。\n\n狼王来了。\n".encode(), "text/plain")},
    )
    wait_task(client, client.post(f"/api/v1/projects/{pid}/parse", json={}).json()["task_id"])
    segs = client.get(f"/api/v1/projects/{pid}/segments").json()
    with dbmod.SessionLocal() as db:
        db.add_all([
            KnowledgeItem(project_id=pid, item_type="term", canonical_key="庄园",
                          data_json=json.dumps({"translation": "Rothschild Family Manor"}),
                          status="approved"),
            KnowledgeItem(project_id=pid, item_type="term", canonical_key="狼王",
                          data_json=json.dumps({"translation": "Wolf King", "term_kind": "title",
                                                "enforcement": "exact"}), status="approved"),
        ])
        db.commit()
        first = insert_translation_row(db, segment_id=segs[0]["id"], stage="polished",
                                       target_text="The werewolves walk into the manor.")
        second = insert_translation_row(db, segment_id=segs[1]["id"], stage="polished",
                                        target_text="The Alpha has come.")
        db.add_all([
            QAIssue(project_id=pid, translation_id=first.id, issue_type="forced_term", severity="high"),
            QAIssue(project_id=pid, translation_id=second.id, issue_type="forced_term", severity="high"),
        ])
        db.commit()
    with dbmod.SessionLocal() as db:
        removed = refresh_forced_term_issues(db, pid)
        assert removed == 1
        left = list(db.scalars(select(QAIssue)))
        assert len(left) == 1
        assert left[0].translation_id == second.id


def test_validate_window_accepts_out_of_order_entries():
    """落库按 segment_id 映射；输出数组顺序不同不应导致窗口永久失败。"""
    segs = [
        {"segment_id": "a", "segment_type": "narrative", "speaker_name": None,
         "source_text": "第一段。"},
        {"segment_id": "b", "segment_type": "narrative", "speaker_name": None,
         "source_text": "第二段。"},
    ]
    translations = [
        {"segment_id": "b", "target_text": "Second."},
        {"segment_id": "a", "target_text": "First."},
    ]
    assert validate_window(segs, translations) == []


def test_validate_window_accepts_multi_speaker_labels() -> None:
    """多人同说对白允许斜杠角色标签，不能误报「缺少角色标签」。"""
    src = {
        "segment_id": "s-multi",
        "segment_type": "dialogue",
        "speaker_name": "Damian/Ethan",
        "source_text": "Damian/Ethan（厉声）：不行！\n",
    }
    ok = validate_window([src], [{
        "segment_id": "s-multi",
        "target_text": "Damian/Ethan: No!\n",
    }])
    assert ok == []


def test_validate_window_accepts_multiple_speaker_cues_and_markdown_labels() -> None:
    """画外音+情绪等多个提示、Markdown 标签和破折号分隔都是合法角色标签。"""
    src = {
        "segment_id": "s-cues",
        "segment_type": "dialogue",
        "speaker_name": "Damian画外音",
        "source_text": "Damian画外音（焦急且带着命令口吻）：立刻带Leo回来！\n",
    }
    variants = [
        "Damian (voice-over) (anxious, commanding): Bring Leo back now!",
        "**Damian (voice-over) (anxious, commanding)**: Bring Leo back now!",
        "Damian (voice-over) — Bring Leo back now!",
    ]
    for target in variants:
        assert validate_window([src], [{
            "segment_id": "s-cues",
            "target_text": target,
        }]) == [], target


def test_normalize_translation_entries_repairs_hallucinated_ids_and_action_lines():
    segments = [
        {
            "segment_id": "scene-1", "segment_type": "heading", "speaker_name": None,
            "source_text": "1-2 帝国酒店走廊 黑夜 内\n",
        },
        {
            "segment_id": "narr-1", "segment_type": "narrative", "speaker_name": None,
            "source_text": "△她推开门。\n△警报灯闪烁。\n△她躲进房间。\n",
        },
        {
            "segment_id": "line-1", "segment_type": "dialogue", "speaker_name": "Chloe",
            "source_text": "Chloe：谁在那里？\n",
        },
    ]
    # 模型臆造了第二个 segment_id，但条目顺序与原文一致。
    entries = [
        {"segment_id": "scene-1", "target_text": "INT. EMPIRE HOTEL CORRIDOR - NIGHT"},
        {"segment_id": "hallucinated", "target_text": "△ She pushes the door open. △ Alarms flash. △ She hides in the room."},
        {"segment_id": "line-1", "target_text": "Chloe: Who's there?"},
    ]
    out = normalize_translation_entries(segments, entries)
    assert [x["segment_id"] for x in out] == ["scene-1", "narr-1", "line-1"]
    assert out[0]["target_text"].startswith("1-2 ")
    assert len([x for x in out[1]["target_text"].splitlines() if x.strip()]) == 3
    assert all(x.startswith("△") for x in out[1]["target_text"].splitlines())
    assert validate_window(segments, out) == []


def test_validate_window_allows_front_matter_line_merge_but_not_body_merge():
    front = {
        "segment_id": "front", "segment_type": "narrative", "speaker_name": None,
        "source_text": (
            "My Title\n故事大纲\n第一段。\n第二段。\n第三段。\n"
            "人物小传\nChloe\n角色一。\nLeo\n角色二。\n"
        ),
    }
    assert validate_window([front], [{
        "segment_id": "front",
        "target_text": "My Title\nStory Outline\nChloe and Leo.\nCharacter Bios",
    }]) == []

    body = {
        "segment_id": "body", "segment_type": "narrative", "speaker_name": None,
        "source_text": "△第一段动作。\n△第二段动作。\n",
    }
    errs = validate_window([body], [{
        "segment_id": "body", "target_text": "△ The first and second actions are merged.",
    }])
    assert any("段内缺行" in e for e in errs)


def test_address_forms_are_not_exact_terms_and_honorifics_use_family_names():
    from app.services.term_match import is_address_form

    assert is_address_form("Helen夫人") is True
    assert is_address_form("Richard先生") is True
    assert is_address_form("Sterling家族") is False
    fixed = apply_known_honorifics(
        "Mrs. Helen said Mr. Richard would see Mr. Cross.",
        {"helen": "Sterling", "richard": "Klein", "chloe": "Vance"},
    )
    assert fixed == "Mrs. Sterling said Mr. Klein would see Mr. Cross."
    assert apply_known_honorifics("Miss Chloe", {"chloe": "Vance"}) == "Miss Vance"


def test_generic_carwash_and_blue_pill_are_not_exact_terms():
    from app.services.term_match import is_generic_source

    assert is_generic_source("洗车行") is True
    assert is_generic_source("蓝色药丸") is True
    assert is_generic_source("Old Brooklyn Car Wash") is False


def test_source_honorifics_fix_wrong_family_names():
    family = {"helen": "Sterling", "chloe": "Vance", "richard": "Klein"}
    assert apply_source_honorifics("Mrs. Vance will see you.", "Helen夫人看到你。", family) == "Mrs. Sterling will see you."
    assert apply_source_honorifics("Miss. Chloe is here.", "Chloe小姐来了。", family) == "Miss Vance is here."
    assert apply_source_honorifics("Mr. Richard gave the order.", "Richard先生下令。", family) == "Mr. Klein gave the order."
    assert apply_source_honorifics("Miss. Chloe's file is here.", "Chloe小姐的档案。", family) == "Miss Vance's file is here."
    assert apply_source_honorifics(
        "Mr. Cross, Mr. Damian, the results are ready.",
        "Cross先生，Damian先生，报告出来了。",
        {**family, "damian": "Sterling"},
    ) == "Mr. Cross, Mr. Sterling, the results are ready."
    assert apply_source_honorifics(
        "Mrs. Sterling is asking for you.",
        "Helen夫人找你。",
        family,
        {"helen夫人": "Madam Helen"},
    ) == "Madam Helen is asking for you."
    assert apply_source_honorifics(
        "Five years ago... Mr. Sterling died.",
        "五年前……Sterling老先生去世了。",
        family,
    ) == "Five years ago... the elder Mr. Sterling died."
