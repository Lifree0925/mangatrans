"""术语一致性与缺行校验：validate_window 行数校验 + 术语锁定。"""

import asyncio
import json
import re
import time

from sqlalchemy import select

import app.core.database as dbmod
from app.llm.fake import FakeProvider
from app.llm.registry import set_provider
from app.models.document import SourceSegment
from app.models.knowledge import KnowledgeItem
from app.models.task import Task
from app.models.translation import ProviderConfig, Translation
from app.services.translation import validate_window


def test_validate_window_detects_missing_lines() -> None:
    """多行段译文缺行必须报错（错位/漏译根因）。"""
    segs = [{"segment_id": "a", "source_text": "第一行。\n第二行。\n"}]
    ok = validate_window(segs, [{"segment_id": "a", "target_text": "One.\nTwo."}])
    assert ok == []
    bad = validate_window(segs, [{"segment_id": "a", "target_text": "One."}])
    assert any("段内缺行" in e for e in bad)


def test_term_lock_writes_translation(client, wait_task) -> None:
    """术语锁定：无译名术语在 Draft 后由模型定名并写回知识库。"""
    from app.services.term_lock import lock_untranslated_terms

    with dbmod.SessionLocal() as db:
        db.add(ProviderConfig(display_name="t", base_url="http://127.0.0.1:9",
                              model_name="m", credential_ref="r", is_default=True, is_verified=True))
        db.commit()
    pid = client.post("/api/v1/projects", json={"name": "术语锁定测试"}).json()["id"]
    doc = client.post(
        f"/api/v1/projects/{pid}/documents/import",
        files={"file": ("s.txt", "狼后 与 狼王 对视。\n".encode(), "text/plain")},
    ).json()
    wait_task(client, client.post(f"/api/v1/projects/{pid}/parse", json={}).json()["task_id"])

    with dbmod.SessionLocal() as db:
        doc_id = db.scalar(select(SourceSegment).where(SourceSegment.project_id == pid).limit(1)).source_document_id
        db.add(KnowledgeItem(project_id=pid, item_type="term", canonical_key="狼后",
                             data_json="{}", status="auto_approved", source="ai"))
        db.add(KnowledgeItem(project_id=pid, item_type="term", canonical_key="狼王",
                             data_json=json.dumps({"translation": "Wolf King"}), status="auto_approved", source="ai"))
        db.commit()

    f = FakeProvider()

    def h(messages):
        c = messages[-1]["content"]
        if "狼后" in c and "狼王" in c and "sample" in c.lower():
            return {"terms": {"狼后": "Luna", "狼王": "Wolf King"}}
        return None

    set_provider(f.on(h))
    result = asyncio.run(
        lock_untranslated_terms(dbmod.SessionLocal, pid, doc_id, f, "fake-model")
    )
    assert result["locked"] == 1  # 只有「狼后」无译名被锁定；「狼王」已有译名不动

    with dbmod.SessionLocal() as db:
        for k in db.scalars(select(KnowledgeItem).where(KnowledgeItem.project_id == pid)):
            data = json.loads(k.data_json or "{}")
            if k.canonical_key == "狼后":
                assert data.get("translation") == "Luna"
            if k.canonical_key == "狼王":
                assert data.get("translation") == "Wolf King"


def test_validate_window_detects_duplicate_translations() -> None:
    """窗口内两段译文完全相同（台词重复+丢失根因）必须报错。"""
    segs = [
        {"segment_id": "a", "source_text": "甲：冲啊！\n"},
        {"segment_id": "b", "source_text": "乙：杀！\n"},
    ]
    dup_text = "A (roars): Warriors of Fenrir! The vampires have taken your Luna! Tonight, follow me!"
    bad = validate_window(segs, [
        {"segment_id": "a", "target_text": dup_text},
        {"segment_id": "b", "target_text": dup_text},
    ])
    assert any("重复译文" in e for e in bad)
    ok = validate_window(segs, [
        {"segment_id": "a", "target_text": dup_text},
        {"segment_id": "b", "target_text": "B (shouts): Kill them all!"},
    ])
    assert ok == []
