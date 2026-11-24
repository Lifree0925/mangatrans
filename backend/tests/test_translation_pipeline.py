"""翻译流水线：Draft 与 Polish 依赖、提前润色和术语门控。"""

import asyncio
import json
import time

import app.core.database as dbmod
from app.llm.fake import FakeProvider
from app.llm.registry import set_provider
from app.models.knowledge import KnowledgeItem
from app.models.project import Project
from app.models.task import Task, TaskItem
from app.models.translation import ProviderConfig
from app.pipeline.worker import claim_polish_batch
from app.services.term_lock import has_pending_untranslated_terms
from conftest import fake_translation_entries


def _fake() -> FakeProvider:
    from app.schemas.knowledge import SceneCardModel

    f = FakeProvider()

    def translate_h(messages):
        content = messages[-1]["content"]
        system = messages[0]["content"] if messages else ""
        if "FAITHFUL DRAFT" in system:
            return {
                "window_id": "W",
                "translations": fake_translation_entries(content, "Draft", stage="draft"),
            }
        if "POLISH" in system:
            return {
                "window_id": "W",
                "translations": fake_translation_entries(content, "Polish", stage="polished"),
                "carry_over": {},
            }
        return None

    def other_h(messages):
        content = messages[-1]["content"]
        if "剧本分析助手" in content:
            return {"items": []}
        if "剧本情境分析助手" in content:
            return SceneCardModel().model_dump()
        if "风格总监" in content:
            return {
                "character_voices": [],
                "dialogue_register": "",
                "action_description_style": "",
                "cultural_strategy": "",
                "capitalization_rules": "",
                "examples": [],
            }
        if "剧本翻译 QA" in content:
            return {"issues": []}
        return None

    return f.on(translate_h).on(other_h)


def _setup_project(client, wait_task, lines: int = 500) -> str:
    with dbmod.SessionLocal() as db:
        db.add(ProviderConfig(
            display_name="pipeline", base_url="http://fake", model_name="fake-model",
            credential_ref="pipeline-ref", is_default=True, is_verified=True,
        ))
        db.commit()
    pid = client.post("/api/v1/projects", json={"name": "流水线测试"}).json()["id"]
    body = "\n\n".join(
        f"林风：第{i}句，我们必须继续前进，绝不能后退，因为这场战斗还没有结束。"
        for i in range(lines)
    )
    client.post(
        f"/api/v1/projects/{pid}/documents/import",
        files={"file": ("s.txt", body.encode(), "text/plain")},
    )
    wait_task(client, client.post(f"/api/v1/projects/{pid}/parse", json={}).json()["task_id"])
    return pid


def _setup_two_chapters(client, wait_task) -> str:
    with dbmod.SessionLocal() as db:
        db.add(ProviderConfig(
            display_name="pipeline2", base_url="http://fake", model_name="fake-model",
            credential_ref="pipeline2-ref", is_default=True, is_verified=True,
        ))
        db.commit()
    pid = client.post("/api/v1/projects", json={"name": "窗口剧情卡测试"}).json()["id"]
    chapter1 = "\n\n".join(
        f"林风：第一章标记第{i}句，我们必须继续前进，绝不能后退，因为战斗还没有结束。"
        for i in range(140)
    )
    chapter2 = "\n\n".join(
        f"王强：第二章标记第{i}句，我们也要继续前进，绝不能后退，因为战斗还没有结束。"
        for i in range(140)
    )
    body = f"第1集\n\n{chapter1}\n\n第2集\n\n{chapter2}\n"
    client.post(
        f"/api/v1/projects/{pid}/documents/import",
        files={"file": ("s.txt", body.encode(), "text/plain")},
    )
    wait_task(client, client.post(f"/api/v1/projects/{pid}/parse", json={}).json()["task_id"])
    return pid


def _observe_first_main_polish(monkeypatch, task_id: str) -> dict:
    """只观察正式窗口 Polish，不把试译的 polish 调用算进来。"""
    from app.services import express as express_mod

    original = express_mod.translate_window_polish_direct
    observed: dict[str, int | float | None] = {"draft_done": None, "first_at": None}

    async def wrapped(*args, **kwargs):
        if observed["draft_done"] is None:
            observed["first_at"] = time.monotonic()
            with dbmod.SessionLocal() as db:
                task = db.get(Task, task_id)
                progress = json.loads(task.progress_json or "{}") if task else {}
                translation = (progress.get("steps") or {}).get("translation") or {}
                observed["draft_done"] = translation.get("draft_done")
            await asyncio.sleep(0.02)
        return await original(*args, **kwargs)

    monkeypatch.setattr(express_mod, "translate_window_polish_direct", wrapped)
    return observed


def _final_draft_done(task_id: str) -> int:
    with dbmod.SessionLocal() as db:
        task = db.get(Task, task_id)
        progress = json.loads(task.progress_json or "{}")
        return int(((progress.get("steps") or {}).get("translation") or {}).get("draft_done") or 0)


def test_express_pipeline_starts_polish_before_all_drafts(client, wait_task, monkeypatch) -> None:
    pid = _setup_project(client, wait_task)
    set_provider(_fake())
    from app.services import express as express_mod

    original_draft = express_mod.translate_window_draft_direct

    async def slow_main_draft(*args, **kwargs):
        await asyncio.sleep(0.05)
        return await original_draft(*args, **kwargs)

    # 只放慢正式窗口 Draft，确保测试能观察到与 Polish 的真实时间重叠。
    monkeypatch.setattr(express_mod, "translate_window_draft_direct", slow_main_draft)

    tid = client.post(
        f"/api/v1/projects/{pid}/translation/express", json={}
    ).json()["task_id"]
    observed = _observe_first_main_polish(monkeypatch, tid)
    result = wait_task(client, tid, timeout=90)
    assert result["status"] == "completed", result.get("error_message")

    final_drafts = _final_draft_done(tid)
    assert final_drafts > 3, "测试剧本必须产生多个窗口"
    assert observed["draft_done"] is not None
    assert observed["draft_done"] < final_drafts


def test_polish_does_not_wait_for_other_chapter_storylines(
    client, wait_task, monkeypatch
) -> None:
    """第一章窗口只等本章剧情卡，不能等第二章剧情卡完成。"""
    pid = _setup_two_chapters(client, wait_task)
    inner = _fake()
    chapter2_done_at: list[float] = []

    class _DelayedStorylineProvider:
        capabilities = inner.capabilities

        async def chat(self, **kwargs):
            if kwargs.get("stage") == "storyline":
                content = kwargs.get("messages", [{}])[-1].get("content", "")
                if "第二章标记" in content:
                    await asyncio.sleep(1.5)
                    try:
                        return await inner.chat(**kwargs)
                    finally:
                        chapter2_done_at.append(time.monotonic())
            return await inner.chat(**kwargs)

        async def test_connection(self):
            return await inner.test_connection()

    set_provider(_DelayedStorylineProvider())
    from app.services import express as express_mod

    original_draft = express_mod.translate_window_draft_direct

    async def slow_main_draft(*args, **kwargs):
        await asyncio.sleep(0.03)
        return await original_draft(*args, **kwargs)

    monkeypatch.setattr(express_mod, "translate_window_draft_direct", slow_main_draft)

    tid = client.post(
        f"/api/v1/projects/{pid}/translation/express", json={}
    ).json()["task_id"]
    observed = _observe_first_main_polish(monkeypatch, tid)
    result = wait_task(client, tid, timeout=90)
    assert result["status"] == "completed", result.get("error_message")
    assert chapter2_done_at, "第二章剧情卡请求必须发生"
    assert observed["first_at"] is not None
    assert observed["first_at"] < chapter2_done_at[0]


def test_express_pending_terms_waits_for_all_drafts(client, wait_task, monkeypatch) -> None:
    """有待定术语时必须等全量 Draft 后锁名，不能提前润色。"""
    pid = _setup_project(client, wait_task, lines=220)
    with dbmod.SessionLocal() as db:
        db.add(KnowledgeItem(
            project_id=pid,
            item_type="term",
            canonical_key="测试锁定术语",
            data_json=json.dumps({"translation": ""}, ensure_ascii=False),
            status="auto_approved",
        ))
        db.commit()
        assert has_pending_untranslated_terms(db, pid) is True
    set_provider(_fake())

    tid = client.post(
        f"/api/v1/projects/{pid}/translation/express", json={}
    ).json()["task_id"]
    observed = _observe_first_main_polish(monkeypatch, tid)
    result = wait_task(client, tid, timeout=90)
    assert result["status"] == "completed", result.get("error_message")

    final_drafts = _final_draft_done(tid)
    assert observed["draft_done"] is not None
    assert observed["draft_done"] == final_drafts


def test_term_pending_gate_detects_untranslated_terms(client) -> None:
    with dbmod.SessionLocal() as db:
        project = Project(name="术语门控")
        db.add(project)
        db.flush()
        item = KnowledgeItem(
            project_id=project.id,
            item_type="term",
            canonical_key="测试术语",
            data_json=json.dumps({"translation": ""}, ensure_ascii=False),
            status="auto_approved",
        )
        db.add(item)
        db.commit()
        assert has_pending_untranslated_terms(db, project.id) is True

        data = json.loads(item.data_json)
        data["translation"] = "Test Term"
        item.data_json = json.dumps(data, ensure_ascii=False)
        db.commit()
        assert has_pending_untranslated_terms(db, project.id) is False


def test_claim_polish_waits_for_terms_and_previous_draft(client) -> None:
    with dbmod.SessionLocal() as db:
        project = Project(name="Polish 依赖")
        db.add(project)
        db.flush()
        task = Task(
            project_id=project.id,
            task_type="translate",
            status="running",
            progress_json=json.dumps({}),
        )
        db.add(task)
        db.flush()
        first = TaskItem(task_id=task.id, window_id="WIN_0000", status="running", phase="draft")
        second = TaskItem(task_id=task.id, window_id="WIN_0001", status="pending", phase="polish")
        db.add_all([first, second])
        db.commit()
        task_id, first_id = task.id, first.id

    with dbmod.SessionLocal() as db:
        assert claim_polish_batch(db) == []

    with dbmod.SessionLocal() as db:
        task = db.get(Task, task_id)
        task.progress_json = json.dumps({"terms_locked": {"locked": 0}})
        db.commit()

    with dbmod.SessionLocal() as db:
        assert claim_polish_batch(db) == []

    with dbmod.SessionLocal() as db:
        first = db.get(TaskItem, first_id)
        first.phase = "polish"
        first.status = "pending"
        db.commit()

    with dbmod.SessionLocal() as db:
        claimed = claim_polish_batch(db)
        assert {item.window_id for item in claimed} == {"WIN_0000", "WIN_0001"}
