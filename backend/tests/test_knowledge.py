"""知识提取与场景卡测试（FakeProvider）。"""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.llm.registry import set_provider
from app.llm.fake import FakeProvider


def _setup_project(client: TestClient, wait_task) -> str:
    pid = client.post("/api/v1/projects", json={"name": "知识测试"}).json()["id"]
    script = """第一集 初遇
【咖啡厅·日内】
林晓：你好，我是林晓。
王强：林总，久仰久仰。

（林晓点头示意）
"""
    import io

    r = client.post(
        f"/api/v1/projects/{pid}/documents/import",
        files={"file": ("s.txt", script.encode("utf-8"), "text/plain")},
    )
    assert r.status_code == 201
    assert wait_task(client, client.post(f"/api/v1/projects/{pid}/parse").json()["task_id"])["status"] == "completed"
    return pid


def _fake_provider() -> FakeProvider:
    f = FakeProvider()

    def knowledge_handler(messages):
        text = messages[0]["content"]
        if "提取人物" in text:
            return {
                "items": [
                    {
                        "canonical_key": "林晓",
                        "item_type": "character",
                        "aliases": ["林总"],
                        "data": {},
                        "evidence_quotes": ["你好，我是林晓。"],
                        "evidence_type": "explicit",
                        "confidence": 0.9,
                    }
                ]
            }
        return None

    def card_handler(messages):
        text = messages[0]["content"]
        if "场景情境卡" in text:
            return {
                "location": "咖啡厅",
                "time_label": "日内",
                "cast": ["林晓", "王强"],
                "prior_events": "二人初次见面",
                "conflict_goal": "互相认识",
                "mood": "礼貌寒暄",
                "intent_notes": [],
                "spoiler_guard": [],
                "link_prev": "",
            }
        return None

    f.on(knowledge_handler).on(card_handler)
    return f


def test_extract_knowledge_express_auto_approved(client: TestClient, wait_task) -> None:
    set_provider(_fake_provider())
    pid = _setup_project(client, wait_task)

    r = client.post(
        f"/api/v1/projects/{pid}/knowledge/extract", json={"mode": "express"}
    )
    assert r.status_code == 200
    task_id = r.json()["task_id"]

    import asyncio
    import time

    t0 = time.time()
    from app.core.database import SessionLocal
    from app.models.task import Task

    while time.time() - t0 < 10:
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            if task.status in ("completed", "failed"):
                break
        time.sleep(0.1)
    with SessionLocal() as db:
        task = db.get(Task, task_id)
    assert task.status == "completed", task.error_message

    items = client.get(f"/api/v1/projects/{pid}/knowledge").json()
    assert items
    lin = next(i for i in items if i["canonical_key"] == "林晓")
    assert lin["status"] == "auto_approved"  # Express + explicit 证据


def test_descriptive_place_is_not_auto_approved_as_exact() -> None:
    from types import SimpleNamespace

    from app.services.knowledge_service import _auto_approved

    cand = SimpleNamespace(
        item_type="term",
        evidence_type="explicit",
        confidence=0.95,
        canonical_key="城郊废弃酒庄地下室",
    )
    data = {
        "translation": "Suburban Abandoned Winery Basement",
        "term_kind": "place",
        "enforcement": "contextual",
    }
    assert _auto_approved("express", cand, data) is False


def test_conflicting_term_translation_downgrades_and_list_exposes_effective_policy(
    client: TestClient,
) -> None:
    from app.services.knowledge_service import _merge_data

    merged = json.loads(_merge_data(
        json.dumps({"translation": "Imperial Hotel", "enforcement": "exact"}),
        {"translation": "Empire Hotel"},
    ))
    assert merged["translation"] == "Imperial Hotel"
    assert merged["translation_conflicts"] == ["Empire Hotel"]
    assert merged["enforcement"] == "contextual"

    pid = client.post("/api/v1/projects", json={"name": "术语表"}).json()["id"]
    r = client.post(f"/api/v1/projects/{pid}/knowledge", json={
        "item_type": "term",
        "canonical_key": "城郊废弃酒庄地下室",
        "translation": "Suburban Abandoned Winery Basement",
        "status": "approved",
    })
    assert r.status_code == 201
    items = client.get(f"/api/v1/projects/{pid}/knowledge").json()
    term = next(i for i in items if i["canonical_key"] == "城郊废弃酒庄地下室")
    assert term["effective_policy"] == "contextual"


