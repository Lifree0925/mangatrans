"""任务中心运行时长：只累计实际执行时间，排除暂停与停机。"""

from datetime import datetime, timedelta, timezone

import app.core.database as dbmod
from app.models.project import Project
from app.models.task import Task
from app.pipeline.task_timer import (
    elapsed_task_seconds,
    settle_task_timer,
    settle_task_timer_for_recovery,
    start_task_timer,
)


def test_timer_excludes_pause_and_downtime() -> None:
    t0 = datetime(2026, 11, 23, 22, 0, tzinfo=timezone.utc)
    task = Task(project_id="p1", task_type="translate", active_seconds=60.0)

    start_task_timer(task, now=t0)
    settle_task_timer(task, now=t0 + timedelta(seconds=30))
    assert task.active_seconds == 90.0
    assert task.started_at is None

    # 模拟停机 700 分钟后再继续：这一段等待绝不能计入运行时长。
    resumed = t0 + timedelta(minutes=700)
    start_task_timer(task, now=resumed)
    assert elapsed_task_seconds(task, now=resumed + timedelta(seconds=20)) == 110.0
    settle_task_timer(task, now=resumed + timedelta(seconds=20))
    assert task.active_seconds == 110.0


def test_recovery_only_counts_until_last_persisted_write() -> None:
    t0 = datetime(2026, 11, 23, 22, 0, tzinfo=timezone.utc)
    task = Task(
        project_id="p1",
        task_type="express",
        active_seconds=10.0,
        started_at=t0,
    )
    task.updated_at = t0 + timedelta(minutes=5)

    # 应用崩溃后隔夜重启：只结算到崩溃前最后一次写库时间。
    settle_task_timer_for_recovery(task)
    assert task.active_seconds == 310.0
    assert task.started_at is None


def test_tasks_api_exposes_actual_elapsed_fields(client) -> None:
    started = datetime.now(timezone.utc) - timedelta(seconds=30)
    with dbmod.SessionLocal() as db:
        project = Project(name="计时测试")
        db.add(project)
        db.flush()
        task = Task(
            project_id=project.id,
            task_type="translate",
            status="running",
            active_seconds=120.0,
            started_at=started,
        )
        db.add(task)
        db.commit()
        task_id = task.id

    row = next(t for t in client.get("/api/v1/tasks").json() if t["id"] == task_id)
    assert row["active_seconds"] == 120.0
    assert row["started_at"] is not None
    assert 149.0 <= row["elapsed_seconds"] <= 153.0

    detail = client.get(f"/api/v1/tasks/{task_id}").json()
    assert detail["active_seconds"] == 120.0
    assert detail["elapsed_seconds"] >= 149.0
