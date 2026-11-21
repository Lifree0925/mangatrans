"""片段编辑：原文编辑留痕 + 译文标记待重译；人工译文新增版本。"""

from sqlalchemy import select

import app.core.database as dbmod
from app.models.audit import AuditLog
from app.models.document import SourceSegment
from app.models.translation import Translation


def _setup(client, wait_task) -> tuple[str, str]:
    pid = client.post("/api/v1/projects", json={"name": "编辑测试"}).json()["id"]
    client.post(f"/api/v1/projects/{pid}/documents/import",
                files={"file": ("s.txt", "林风：我们走。\n夜色降临。\n".encode(), "text/plain")})
    wait_task(client, client.post(f"/api/v1/projects/{pid}/parse", json={}).json()["task_id"])
    segs = client.get(f"/api/v1/projects/{pid}/segments").json()
    sid = next(s["id"] for s in segs if s["segment_type"] == "dialogue")
    # 预置一条译文（模拟翻译产出）
    with dbmod.SessionLocal() as db:
        db.add(Translation(segment_id=sid, version=1, stage="polished",
                           target_text="Let's go.", review_status="approved",
                           freshness_status="current", source_hash="x"))
        db.commit()
    return pid, sid


def test_edit_source_marks_translation_stale(client, wait_task) -> None:
    pid, sid = _setup(client, wait_task)
    r = client.patch(f"/api/v1/projects/{pid}/segments/{sid}",
                     json={"source_text": "林风：我们快走！"})
    assert r.status_code == 200, r.text
    assert r.json()["stale_translations"] == 1
    with dbmod.SessionLocal() as db:
        t = db.scalar(select(Translation).where(Translation.segment_id == sid))
        assert t.freshness_status == "stale"
        log = db.scalar(select(AuditLog).where(AuditLog.entity_id == sid))
        assert log is not None
        assert log.action == "edit_source"
        assert log.before_text == "林风：我们走。\n"
        assert log.after_text == "林风：我们快走！"
        seg = db.get(SourceSegment, sid)
        assert seg.source_text == "林风：我们快走！"


def test_add_manual_translation_creates_new_version(client, wait_task) -> None:
    pid, sid = _setup(client, wait_task)
    r = client.post(f"/api/v1/projects/{pid}/segments/{sid}/translations",
                    json={"target_text": "Let us hurry!"})
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 2
    assert r.json()["freshness_status"] == "current"
    with dbmod.SessionLocal() as db:
        rows = list(db.scalars(
            select(Translation).where(Translation.segment_id == sid).order_by(Translation.version)
        ))
        assert [t.version for t in rows] == [1, 2]
        assert rows[-1].target_text == "Let us hurry!"
        assert rows[-1].review_status == "approved"


def test_latest_translations_batch(client, wait_task) -> None:
    pid, sid = _setup(client, wait_task)
    r = client.get(f"/api/v1/projects/{pid}/translations/latest")
    assert r.status_code == 200
    items = r.json()["items"]
    assert any(i["segment_id"] == sid and i["target_text"] == "Let's go." for i in items)


def test_edit_empty_source_rejected(client, wait_task) -> None:
    pid, sid = _setup(client, wait_task)
    r = client.patch(f"/api/v1/projects/{pid}/segments/{sid}", json={"source_text": "   "})
    assert r.status_code == 400
