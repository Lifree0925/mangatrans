"""Express 一键直出端到端 + 导出 + QA 测试（FakeProvider 全程脚本化）。"""

import re
import time

from fastapi.testclient import TestClient

import app.core.database as dbmod
from app.llm.fake import FakeProvider
from app.llm.registry import set_provider
from app.models.task import Task
from app.services.exporter import (
    _build_role_map,
    _render_cast_line,
    _render_dialogue,
    _render_narrative_lines,
)
from conftest import fake_translation_entries


def _add_verified_provider() -> None:
    """测试用：登记一个已验证的模型（翻译任务前置检查需要）。"""
    import app.core.database as dbmod
    from app.models.translation import ProviderConfig

    with dbmod.SessionLocal() as db:
        db.add(
            ProviderConfig(
                model_name="fake-model",
                base_url="http://fake",
                credential_ref="test-ref",
                is_verified=True,
                is_default=True,
            )
        )
        db.commit()


def _setup(client: TestClient, wait_task) -> str:
    pid = client.post("/api/v1/projects", json={"name": "一键直出"}).json()["id"]
    script = """第一集 初遇
【咖啡厅·日内】
林晓：（走进来）你好。
王强：你是谁？

（两人对视）
"""
    r = client.post(
        f"/api/v1/projects/{pid}/documents/import",
        files={"file": ("s.txt", script.encode(), "text/plain")},
    )
    assert r.status_code == 201
    assert wait_task(client, client.post(f"/api/v1/projects/{pid}/parse").json()["task_id"])["status"] == "completed"
    _add_verified_provider()
    return pid


def _universal_fake(client: TestClient, pid: str) -> FakeProvider:
    """覆盖全部 LLM 环节的脚本化 Fake。"""
    seg_ids = [s["id"] for s in client.get(f"/api/v1/projects/{pid}/segments").json()]

    def knowledge_h(messages):
        if "提取人物与术语" in messages[-1]["content"]:
            return {
                "items": [
                    {
                        "canonical_key": "林晓",
                        "item_type": "character",
                        "aliases": [],
                        "data": {},
                        "evidence_quotes": ["你好"],
                        "evidence_type": "explicit",
                        "confidence": 0.9,
                    }
                ]
            }
        return None

    def card_h(messages):
        if "场景情境卡" in messages[-1]["content"]:
            return {
                "location": "咖啡厅",
                "time_label": "日内",
                "cast": ["林晓", "王强"],
                "prior_events": "",
                "conflict_goal": "",
                "mood": "",
                "intent_notes": [],
                "spoiler_guard": [],
                "link_prev": "",
            }
        return None

    def guide_h(messages):
        if "试译样本" in messages[-1]["content"]:
            return {
                "character_voices": [],
                "dialogue_register": "natural",
                "action_description_style": "concise",
                "cultural_strategy": "moderate",
                "capitalization_rules": "",
                "examples": [],
            }
        return None

    def translate_h(messages):
        content = messages[-1]["content"]
        system = messages[0]["content"] if messages else ""
        if "translate only these" in content or "FAITHFUL DRAFT" in content:
            natural = "POLISH pass" in system
            out = {
                "window_id": "W",
                "translations": fake_translation_entries(
                    content, "Hello", stage="polished" if natural else "draft"
                ),
            }
            if natural:
                out["carry_over"] = {
                    "forms_of_address": [],
                    "ongoing_state": "",
                    "open_threads": [],
                    "active_expressions": [],
                }
            return out
        return None

    def qa_h(messages):
        if "终审 QA" in messages[-1]["content"]:
            return {"issues": []}
        return None

    f = FakeProvider()
    f.on(knowledge_h).on(card_h).on(guide_h).on(translate_h).on(qa_h)
    return f


def _wait(task_id: str, timeout: float = 60) -> str:
    t0 = time.time()
    while time.time() - t0 < timeout:
        with dbmod.SessionLocal() as db:
            task = db.get(Task, task_id)
            if task.status in ("completed", "failed"):
                return task.status, task.error_message, task.progress_json
        time.sleep(0.2)
    return "timeout", "", "{}"


def test_express_end_to_end(client: TestClient, wait_task) -> None:
    pid = _setup(client, wait_task)
    set_provider(_universal_fake(client, pid))

    r = client.post(f"/api/v1/projects/{pid}/translation/express", json={})
    assert r.status_code == 200
    status, error, progress = _wait(r.json()["task_id"])
    assert status == "completed", f"{status}: {error}"

    # 所有 polished 已批量 approved
    from sqlalchemy import select

    from app.models.document import SourceSegment
    from app.models.translation import Translation

    with dbmod.SessionLocal() as db:
        rows = list(
            db.scalars(
                select(Translation)
                .join(SourceSegment, Translation.segment_id == SourceSegment.id)
                .where(SourceSegment.project_id == pid, Translation.stage == "polished")
            )
        )
    assert rows
    latest_by_segment = {}
    for t in rows:
        current = latest_by_segment.get(t.segment_id)
        if current is None or t.version > current.version:
            latest_by_segment[t.segment_id] = t
    assert all(t.review_status == "approved" for t in latest_by_segment.values())

    # 导出双语 Markdown（常规导出：中英对照保留）
    r = client.post(f"/api/v1/projects/{pid}/export", json={"delivery": False, "bilingual": True})
    assert r.status_code == 200
    data = r.json()
    assert data["content"].startswith("# 一键直出")
    assert "Hello" in data["content"]
    assert data["report"]["untranslated"] == 0

    # QA 记录存在（空问题列表也算跑过；导出报告包含统计）
    issues = client.get(f"/api/v1/projects/{pid}/qa-issues").json()
    assert isinstance(issues, list)


def test_export_bilingual_contains_original(client: TestClient, wait_task) -> None:
    pid = _setup(client, wait_task)
    set_provider(_universal_fake(client, pid))
    # 直接跑批量翻译
    r = client.post(f"/api/v1/projects/{pid}/translation/start", json={})
    task_id = r.json()["task_id"]
    import asyncio

    from app.pipeline.worker import run_once

    for _ in range(60):
        asyncio.run(run_once())
        with dbmod.SessionLocal() as db:
            if db.get(Task, task_id).status == "completed":
                break
    r = client.post(f"/api/v1/projects/{pid}/export", json={"delivery": False, "bilingual": True})
    body = r.json()["content"]
    assert "（原文）" in body
    assert "Hello" in body


def test_export_delivery_template(client: TestClient, wait_task) -> None:
    """交付版导出：按《剧本交付模板.md》生成（中文骨架 + 英文台词）。"""
    pid = _setup(client, wait_task)
    set_provider(_universal_fake(client, pid))
    r = client.post(f"/api/v1/projects/{pid}/translation/start", json={})
    task_id = r.json()["task_id"]
    import asyncio

    from app.pipeline.worker import run_once

    for _ in range(60):
        asyncio.run(run_once())
        with dbmod.SessionLocal() as db:
            if db.get(Task, task_id).status == "completed":
                break

    r = client.post(f"/api/v1/projects/{pid}/export", json={"title_en": "First Meeting"})
    assert r.status_code == 200, r.text
    data = r.json()
    body = data["content"]
    assert data["filename"].endswith(".md")
    # 顶部：英文剧名 + 《中文剧名》
    assert body.startswith("First Meeting\n《")
    # 集标题
    assert "\n第1集\n" in body
    # 场景行（中文原文保留，且只出现一次）
    assert "【咖啡厅·日内】" in body
    # 叙述/动作行保留中文原文
    assert "（两人对视）" in body
    # 台词已替换为英文译文（fake 译文）
    assert "Hello" in body
    # 结尾
    assert "（剧终）" in body


def test_export_delivery_default_is_template(client: TestClient, wait_task) -> None:
    """不带 delivery 参数时默认输出交付模板（需求变更后的默认交付物）。"""
    pid = _setup(client, wait_task)
    set_provider(_universal_fake(client, pid))
    r = client.post(f"/api/v1/projects/{pid}/translation/start", json={})
    task_id = r.json()["task_id"]
    import asyncio

    from app.pipeline.worker import run_once

    for _ in range(60):
        asyncio.run(run_once())
        with dbmod.SessionLocal() as db:
            if db.get(Task, task_id).status == "completed":
                break
    r = client.post(f"/api/v1/projects/{pid}/export", json={})
    body = r.json()["content"]
    assert "（剧终）" in body
    assert "第1集" in body


def test_export_delivery_file_types(client: TestClient, wait_task) -> None:
    """交付版支持 md / txt / docx 三种文件类型，内容结构一致。"""
    pid = _setup(client, wait_task)
    set_provider(_universal_fake(client, pid))
    r = client.post(f"/api/v1/projects/{pid}/translation/start", json={})
    task_id = r.json()["task_id"]
    import asyncio

    from app.pipeline.worker import run_once

    for _ in range(60):
        asyncio.run(run_once())
        with dbmod.SessionLocal() as db:
            if db.get(Task, task_id).status == "completed":
                break

    for fmt in ("md", "txt", "docx"):
        r = client.post(
            f"/api/v1/projects/{pid}/export",
            json={"delivery": True, "format": fmt, "title_en": "First Meeting"},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["format"] == fmt
        assert data["filename"].endswith(f".{fmt}")
        if fmt == "docx":
            assert data["content_base64"], "docx 应返回 base64"
            assert data["content"] is None
        else:
            assert "（剧终）" in data["content"]
            assert "第1集" in data["content"]


def test_delivery_reuses_existing_english_title(client: TestClient, wait_task) -> None:
    """剧本自带英文标题时：正文不重复输出，交付版直接复用且不再改写标题。"""
    pid = client.post(
        "/api/v1/projects", json={"name": "被狼王抛弃的新娘"}
    ).json()["id"]
    script = """The Alpha's Rejected Mate
被狼王抛弃的新娘

第一集 初遇
【咖啡厅·日内】
林晓：（走进来）你好。
"""
    r = client.post(
        f"/api/v1/projects/{pid}/documents/import",
        files={"file": ("titled.txt", script.encode(), "text/plain")},
    )
    assert r.status_code == 201
    assert wait_task(
        client, client.post(f"/api/v1/projects/{pid}/parse").json()["task_id"]
    )["status"] == "completed"
    _add_verified_provider()
    set_provider(_universal_fake(client, pid))

    task_id = client.post(
        f"/api/v1/projects/{pid}/translation/start", json={}
    ).json()["task_id"]
    import asyncio

    from app.pipeline.worker import run_once

    for _ in range(60):
        asyncio.run(run_once())
        with dbmod.SessionLocal() as db:
            if db.get(Task, task_id).status == "completed":
                break

    # 首段译文强制保留原英文标题
    segments = client.get(f"/api/v1/projects/{pid}/segments").json()
    first = min(segments, key=lambda s: s["segment_no"])
    latest = client.get(f"/api/v1/projects/{pid}/translations/latest").json()["items"]
    title_translation = next(x for x in latest if x["segment_id"] == first["id"])
    assert title_translation["target_text"] == "The Alpha's Rejected Mate"

    # 交付版自动使用剧本标题，且正文不再重复一次
    body = client.post(f"/api/v1/projects/{pid}/export", json={}).json()["content"]
    assert body.startswith("The Alpha's Rejected Mate\n《被狼王抛弃的新娘》\n")
    assert body.count("The Alpha's Rejected Mate") == 1


def test_delivery_multi_speaker_dialogue_keeps_chinese_direction() -> None:
    """多人同说标签必须能解析，交付版不能退回英文括号格式。"""
    rendered = _render_dialogue(
        "Damian/Ethan（厉声）：不行！",
        "Damian/Ethan (sharp): No!",
    )
    assert rendered == ["Damian/Ethan（厉声）：No!"]

    rendered = _render_dialogue(
        "Ethan/Marcus（异口同声，嘲讽拉满）：就凭你？",
        "Ethan/Marcus (in unison, dripping with mockery): You?",
    )
    assert rendered == ["Ethan/Marcus（异口同声，嘲讽拉满）：You?"]


def test_delivery_cast_lines_are_anglicized_and_role_labels_canonicalized() -> None:
    """人物行即使属于 narrative 也要英文化；角色变体按多数名统一。"""
    from types import SimpleNamespace

    def seg(sid, stype, source):
        return SimpleNamespace(id=sid, segment_type=stype, source_text=source)

    segs = [
        seg("c1", "narrative", "人物：Chloe，恶霸房东，保镖若干"),
        seg("d1", "dialogue", "恶霸房东（冷笑）：滚。"),
        seg("d2", "dialogue", "头目（冷笑）：滚。"),
        seg("d3", "dialogue", "头目（怒）：滚。"),
        seg("d4", "dialogue", "头目（冷）：滚。"),
        seg("c2", "narrative", "人物：Damian，Helen（Damian母亲）"),
    ]
    trans = {
        "c1": SimpleNamespace(target_text="Characters: Chloe, Bully Landlord, several bodyguards"),
        "d1": SimpleNamespace(target_text="Bully Landlord (sneers): Get out."),
        "d2": SimpleNamespace(target_text="African Burly Man Leader (sneers): Get out."),
        "d3": SimpleNamespace(target_text="African Strongman Leader (angry): Get out."),
        "d4": SimpleNamespace(target_text="African Strongman Leader (cold): Get out."),
        "c2": SimpleNamespace(target_text="Characters: Damian, Helen (Damian's mother)"),
    }
    role_map = _build_role_map(segs, trans)
    assert role_map["头目"] == "African Strongman Leader"
    assert _render_cast_line("人物：Chloe，恶霸房东，保镖若干", role_map) == (
        "人物：Chloe、Bully Landlord、several bodyguards"
    )
    assert _render_cast_line("人物：Damian，Helen（Damian母亲）", role_map) == (
        "人物：Damian、Helen (Damian's mother)"
    )
    assert _render_dialogue("头目（冷笑）：滚。", "African Burly Man Leader (sneers): Get out.", role_map) == [
        "African Strongman Leader（冷笑）：Get out."
    ]


def test_delivery_narration_uses_translation_but_action_stays_chinese() -> None:
    """旁白/画外音/字幕输出英文；纯动作和剧终标记仍保留中文。"""
    source = "\n".join([
        "△Chloe推开门。",
        "△电视画外音：据知情人透露，五年前的黑客就是她。",
        "△字幕：三个月后。",
        "△系统提示音冰冷响起：警告，防火墙破损百分之六十！",
        "旁白：故事从这一夜开始。",
        "（剧终）",
    ])
    target = "\n".join([
        "△Chloe pushes the door open.",
        "△TV voice-over: Sources say the hacker from five years ago was her.",
        "△Subtitle: Three months later.",
        "△The system alert chimes cold: Warning — firewall sixty percent compromised!",
        "Narration: The story begins that night.",
        "(End)",
    ])
    assert _render_narrative_lines(source, target) == [
        "△Chloe推开门。",
        "△TV voice-over: Sources say the hacker from five years ago was her.",
        "△Subtitle: Three months later.",
        "△The system alert chimes cold: Warning — firewall sixty percent compromised!",
        "Narration: The story begins that night.",
        "（剧终）",
    ]
