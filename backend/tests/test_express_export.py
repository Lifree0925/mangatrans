"""Express 一键直出端到端 + 导出 + QA 测试（FakeProvider 全程脚本化）。"""

import re
import time

from fastapi.testclient import TestClient

import app.core.database as dbmod
from app.llm.fake import FakeProvider
from app.llm.registry import set_provider
from app.models.task import Task


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
            ids = re.findall(r"\[([0-9a-f]{32})\]", content)
            natural = "POLISH pass" in system
            out = {
                "window_id": "W",
                "translations": [
                    {
                        "segment_id": sid,
                        "target_text": f"Hello {sid[:8]}",
                        "notes": [],
                        "ambiguity": [],
                    }
                    for sid in dict.fromkeys(ids)
                ],
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
        if "剧本翻译 QA" in messages[-1]["content"]:
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
    assert all(t.review_status == "approved" for t in rows)

    # 导出双语 Markdown
    r = client.post(f"/api/v1/projects/{pid}/export", json={"bilingual": True})
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
    r = client.post(f"/api/v1/projects/{pid}/export", json={"bilingual": True})
    body = r.json()["content"]
    assert "（原文）" in body
    assert "Hello" in body
