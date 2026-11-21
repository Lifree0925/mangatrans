"""批量翻译 + Worker + 恢复测试（FakeProvider）。"""

import asyncio
import json
import re
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.core.database as dbmod
from app.llm.fake import FakeProvider
from app.llm.registry import set_provider
from app.models.document import SourceSegment
from app.models.task import Task, TaskItem
from app.models.translation import Translation
from app.pipeline.worker import recover_stale_items, run_once
from app.services.window_planner import build_windows


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


def _mk_seg(n, chapter_id="c1", text="你好。\n"):
    return SourceSegment(
        id=f"SEG{n:04d}", project_id="p1", source_document_id="d1",
        chapter_id=chapter_id, scene_id=None, segment_no=n,
        segment_type="dialogue", speaker_name="林晓",
        source_text=text, source_start=0, source_end=len(text), source_hash="h",
    )


def test_window_planner_hard_boundaries() -> None:
    segs = [_mk_seg(1, "c1"), _mk_seg(2, "c1"), _mk_seg(3, "c2"), _mk_seg(4, "c2")]
    windows = build_windows(segs)
    assert len(windows) == 2
    assert [s.chapter_id for s in windows[0]] == ["c1", "c1"]


def test_window_planner_size_split() -> None:
    segs = [_mk_seg(i, text="长" * 400 + "\n") for i in range(1, 11)]
    windows = build_windows(segs)
    assert len(windows) > 1


def test_recover_stale_items() -> None:
    """模拟运行中崩溃：started_at 超时的 running item 回收为 pending。"""
    from app.models.task import Task

    from app.models.project import Project

    with dbmod.SessionLocal() as db:
        proj = Project(name="recover")
        db.add(proj)
        db.flush()
        t = Task(project_id=proj.id, task_type="translate")
        db.add(t)
        db.flush()
        old = TaskItem(
            task_id=t.id, status="running",
            started_at=datetime.now(timezone.utc) - timedelta(minutes=60),
        )
        fresh = TaskItem(task_id=t.id, status="running", started_at=datetime.now(timezone.utc))
        db.add_all([old, fresh])
        db.commit()
        old_id, fresh_id = old.id, fresh.id

    n = 0
    with dbmod.SessionLocal() as db:
        n = recover_stale_items(db)
    with dbmod.SessionLocal() as db:
        assert db.get(TaskItem, old_id).status == "pending"
        assert db.get(TaskItem, fresh_id).status == "running"
    assert n == 1


def _setup_script(client: TestClient, wait_task) -> str:
    pid = client.post("/api/v1/projects", json={"name": "批量翻译"}).json()["id"]
    script = """第一集 初遇
【咖啡厅·日内】
林晓：你好。
王强：你是谁？

第二集 重逢
【街头·夜外】
林晓：别过来！
"""
    r = client.post(
        f"/api/v1/projects/{pid}/documents/import",
        files={"file": ("s.txt", script.encode(), "text/plain")},
    )
    assert r.status_code == 201
    assert wait_task(client, client.post(f"/api/v1/projects/{pid}/parse").json()["task_id"])["status"] == "completed"
    _add_verified_provider()
    return pid


def test_batch_translation_end_to_end(client: TestClient, wait_task) -> None:
    pid = _setup_script(client, wait_task)

    with dbmod.SessionLocal() as db:
        seg_ids = [
            s.id for s in db.scalars(select(SourceSegment).where(SourceSegment.project_id == pid))
        ]

    f = FakeProvider()

    def h(messages):
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
                        "target_text": f"EN-{sid}-" + ("polished" if natural else "draft"),
                        "notes": [],
                        "ambiguity": [],
                    }
                    for sid in dict.fromkeys(ids)
                ],
            }
            if natural:
                out["carry_over"] = {
                    "forms_of_address": ["直呼其名"],
                    "ongoing_state": "tension",
                    "open_threads": [],
                    "active_expressions": [],
                }
            return out
        return None

    set_provider(f.on(h))

    r = client.post(f"/api/v1/projects/{pid}/translation/start", json={})
    assert r.status_code == 200
    task_id = r.json()["task_id"]

    status = "running"
    for _ in range(60):
        asyncio.run(run_once())
        with dbmod.SessionLocal() as db:
            task = db.get(Task, task_id)
            status = task.status
        if status in ("completed", "failed"):
            break
    assert status == "completed"

    with dbmod.SessionLocal() as db:
        task = db.get(Task, task_id)
        progress = json.loads(task.progress_json)
        assert progress["done"] == progress["total"]
        assert progress.get("carry_over") is not None
        rows = list(
            db.scalars(select(Translation).join(SourceSegment, Translation.segment_id == SourceSegment.id).where(SourceSegment.project_id == pid))
        )
    stages = {t.stage for t in rows}
    assert {"draft", "polished"} <= stages
    # 所有 dialogue 段都有译文
    assert len({t.segment_id for t in rows}) == len(seg_ids)


def test_stale_marking_on_knowledge_change(client: TestClient, wait_task) -> None:
    """知识修改后受影响译文标记 stale（走 context 快照）。"""
    pid = _setup_script(client, wait_task)
    # 直接构造：一条 approved 知识 + 一篇引用它的译文
    from app.models.knowledge import KnowledgeItem

    with dbmod.SessionLocal() as db:
        k = KnowledgeItem(project_id=pid, item_type="term", canonical_key="天机阁",
                          data_json="{}", status="approved")
        db.add(k)
        db.flush()
        seg = db.scalar(select(SourceSegment).where(SourceSegment.project_id == pid).limit(1))
        t = Translation(
            segment_id=seg.id, version=1, stage="polished", target_text="EN",
            context_snapshot_json=json.dumps({"knowledge_ids": [k.id]}),
        )
        db.add(t)
        db.commit()
        k_id, t_id = k.id, t.id

    r = client.patch(f"/api/v1/projects/{pid}/knowledge/{k_id}", json={"status": "approved"})
    assert r.status_code == 200

    with dbmod.SessionLocal() as db:
        assert db.get(Translation, t_id).freshness_status == "stale"
