"""任务级幂等与数据库并发约束。"""

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy.exc import IntegrityError

import app.core.database as dbmod
from app.models.task import Task, TaskItem
from app.pipeline.runner import ActiveTaskConflict, create_task
from app.pipeline.worker import _claim_pending_items


def _project(client) -> str:
    return client.post("/api/v1/projects", json={"name": "并发任务"}).json()["id"]


def test_database_rejects_two_active_tasks_for_same_project(client) -> None:
    pid = _project(client)
    with dbmod.SessionLocal() as db:
        db.add(Task(project_id=pid, task_type="express", status="queued"))
        db.commit()
    with dbmod.SessionLocal() as db:
        db.add(Task(project_id=pid, task_type="translate", status="running"))
        with pytest.raises(IntegrityError):
            db.commit()


def test_create_task_conflict_is_explicit(client) -> None:
    pid = _project(client)
    with dbmod.SessionLocal() as db:
        db.add(Task(project_id=pid, task_type="express", status="queued"))
        db.commit()
    with dbmod.SessionLocal() as db:
        with pytest.raises(ActiveTaskConflict):
            create_task(db, pid, "translate", {})


def test_pending_item_can_only_be_claimed_once(client) -> None:
    pid = _project(client)
    with dbmod.SessionLocal() as db:
        task = Task(project_id=pid, task_type="translate", status="running")
        db.add(task)
        db.flush()
        item = TaskItem(task_id=task.id, window_id="WIN_0001", status="pending", phase="draft")
        db.add(item)
        db.commit()
        task_id, item_id = task.id, item.id

    def claim():
        with dbmod.SessionLocal() as db:
            item = db.get(TaskItem, item_id)
            return [x.id for x in _claim_pending_items(db, [item])]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert sum(len(x) for x in results) == 1
    with dbmod.SessionLocal() as db:
        assert db.get(TaskItem, item_id).status == "running"
        assert db.get(Task, task_id).status == "running"


def test_late_pause_cannot_overwrite_completed_task(client) -> None:
    pid = _project(client)
    with dbmod.SessionLocal() as db:
        task = Task(project_id=pid, task_type="express", status="completed")
        db.add(task)
        db.commit()
        task_id = task.id

    r = client.post(f"/api/v1/tasks/{task_id}/pause")
    assert r.status_code == 200
    assert r.json()["status"] == "completed"
