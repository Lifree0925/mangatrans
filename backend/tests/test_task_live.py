"""任务中心实时活动：在途模型请求与最近活动时间。"""

import app.core.database as dbmod
from app.llm.activity import active_calls_snapshot, begin_active_call, end_active_call
from app.llm.usage import record_usage, usage_context
from app.models.project import Project
from app.models.task import Task


def test_active_call_snapshot_tracks_in_flight_request() -> None:
    with usage_context("live-task", "live-project"):
        call_id = begin_active_call("polish", "deepseek-flash")
        try:
            snap = active_calls_snapshot(["live-task"])["live-task"]
            assert snap["active_calls"] == 1
            assert snap["stages"] == ["polish"]
            assert snap["oldest_seconds"] is not None
        finally:
            end_active_call(call_id)
    assert active_calls_snapshot(["live-task"])["live-task"]["active_calls"] == 0


def test_tasks_api_exposes_live_activity(client) -> None:
    with dbmod.SessionLocal() as db:
        project = Project(name="实时任务")
        db.add(project)
        db.flush()
        task = Task(project_id=project.id, task_type="translate", status="running")
        db.add(task)
        db.commit()
        task_id, project_id = task.id, project.id

    with usage_context(task_id, project_id):
        record_usage(
            stage="polish",
            model_name="deepseek-flash",
            provider_id="test",
            usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        )
        call_id = begin_active_call("polish", "deepseek-flash")
    try:
        row = next(t for t in client.get("/api/v1/tasks").json() if t["id"] == task_id)
        assert row["last_activity_at"] is not None
        assert row["live"]["active_calls"] == 1
        assert row["live"]["stages"] == ["polish"]
        assert row["live"]["models"] == ["deepseek-flash"]
    finally:
        end_active_call(call_id)

    detail = client.get(f"/api/v1/tasks/{task_id}").json()
    assert detail["last_activity_at"] is not None
    assert detail["live"]["active_calls"] == 0


def test_task_list_strips_heavy_progress_snapshots(client) -> None:
    import json

    with dbmod.SessionLocal() as db:
        project = Project(name="精简进度")
        db.add(project)
        db.flush()
        task = Task(
            project_id=project.id,
            task_type="translate",
            status="running",
            progress_json=json.dumps({
                "draft_carry": {"WIN_0000": {"ongoing_state": "x"}},
                "steps": {"translation": {
                    "done": 1, "total": 2, "draft_done": 1,
                    "draft_carry": {"WIN_0001": {"ongoing_state": "y"}},
                    "failed_window_errors": {"1": "detail"},
                }},
            }),
        )
        db.add(task)
        db.commit()
        task_id = task.id

    row = next(t for t in client.get("/api/v1/tasks").json() if t["id"] == task_id)
    assert "draft_carry" not in row["progress"]
    assert row["progress"]["draft_done"] == 1
    translation = row["progress"]["steps"]["translation"]
    assert "draft_carry" not in translation
    assert "failed_window_errors" not in translation
    assert translation["draft_done"] == 1

    detail = client.get(f"/api/v1/tasks/{task_id}").json()
    assert "draft_carry" in detail["progress"]
