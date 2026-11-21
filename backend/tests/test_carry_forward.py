"""carry-over 前置与 Polish 并发测试：Draft carry 必须传入下一窗口 Polish。"""

import json
import re
import time

from sqlalchemy import select

import app.core.database as dbmod
from app.llm.fake import FakeProvider
from app.llm.registry import set_provider
from app.models.document import SourceSegment
from app.models.task import Task
from app.services.window_planner import build_windows


def _script_text() -> str:
    """8 场 × 约 300 字 → 2+ 个翻译窗口（窗口预算 2200 字）。"""
    lines = []
    for i in range(8):
        lines.append(f"第{i + 1}场")
        for j in range(3):
            lines.append("林晓：" + ("我们走。" * 20)[:100])
            lines.append(("夜色降临。" * 20)[:100])
        lines.append("")
    return "\n".join(lines)


def _setup(client, wait_task, provider) -> tuple[str, list]:
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
    r = client.post("/api/v1/projects", json={"name": "carry 前置测试", "workflow_mode": "express"})
    pid = r.json()["id"]
    client.post(
        f"/api/v1/projects/{pid}/documents/import",
        files={"file": ("s.txt", _script_text().encode(), "text/plain")},
    )
    wait_task(client, client.post(f"/api/v1/projects/{pid}/parse", json={}).json()["task_id"])
    set_provider(provider)
    with dbmod.SessionLocal() as db:
        segs = list(
            db.scalars(
                select(SourceSegment)
                .where(SourceSegment.project_id == pid)
                .order_by(SourceSegment.segment_no)
            )
        )
    windows = build_windows(segs)
    return pid, windows


def test_draft_carry_forwarded_to_polish(client, wait_task) -> None:
    """Express 全链路：第 N 窗口 Polish 的上下文必须包含第 N-1 窗口 Draft 的 carry。"""
    errors: list[str] = []

    f = FakeProvider()

    def h(messages):
        content = messages[-1]["content"]
        system = messages[0]["content"] if messages else ""
        if "translate only these" in content or "FAITHFUL DRAFT" in content:
            ids = re.findall(r"\[([0-9a-f]{32})\]", content)
            ids = list(dict.fromkeys(ids))
            natural = "POLISH pass" in system
            if natural and ids:
                # 定位当前窗口
                wi = next((i for i, w in enumerate(windows) if w[0].id == ids[0]), None)
                if wi is not None and wi > 0:
                    prev_mark = f"S{windows[wi - 1][0].id[:8]}"
                    if "CARRY-OVER STATE" not in content:
                        errors.append(f"{wi} 窗口缺失 carry 块")
                    elif prev_mark not in content:
                        errors.append(f"{wi} 窗口 carry 未取到前一窗口 Draft 状态: {prev_mark}")
            out = {
                "window_id": "W",
                "translations": [
                    {
                        "segment_id": sid,
                        "target_text": f"EN {sid[:8]}",
                        "notes": [],
                        "ambiguity": [],
                    }
                    for sid in ids
                ],
            }
            if not natural and ids:
                # Draft 输出 carry-over（前置链路）
                out["carry_over"] = {
                    "forms_of_address": [],
                    "ongoing_state": f"S{ids[0][:8]}",
                    "open_threads": [],
                    "active_expressions": [],
                }
            return out
        return None

    set_provider(f.on(h))
    pid, windows = _setup(client, wait_task, f)
    assert len(windows) >= 2, f"测试剧本需至少 2 个窗口，实际 {len(windows)}"

    r = client.post(f"/api/v1/projects/{pid}/translation/express", json={})
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]
    t0 = time.time()
    status = "running"
    while time.time() - t0 < 30:
        d = client.get(f"/api/v1/tasks/{task_id}").json()
        status = d["status"]
        if status in ("completed", "failed"):
            break
        time.sleep(0.2)
    assert status == "completed", errors or d.get("error_message")
    assert errors == [], errors


def test_worker_concurrent_polish_draft_carry_map(client, wait_task) -> None:
    """Worker 并发 Polish：progress 中每个窗口都有 Draft carry（并发下无丢失）。"""
    import asyncio

    from app.models.task import TaskItem
    from app.pipeline.worker import run_once

    f = FakeProvider()

    def h(messages):
        content = messages[-1]["content"]
        system = messages[0]["content"] if messages else ""
        if "translate only these" in content or "FAITHFUL DRAFT" in content:
            ids = list(dict.fromkeys(re.findall(r"\[([0-9a-f]{32})\]", content)))
            natural = "POLISH pass" in system
            out = {
                "window_id": "W",
                "translations": [
                    {
                        "segment_id": sid,
                        "target_text": f"EN {sid[:8]}" + ("-p" if natural else "-d"),
                        "notes": [],
                        "ambiguity": [],
                    }
                    for sid in ids
                ],
            }
            if not natural and ids:
                out["carry_over"] = {
                    "forms_of_address": [],
                    "ongoing_state": f"S{ids[0][:8]}",
                    "open_threads": [],
                    "active_expressions": [],
                }
            elif natural:
                out["carry_over"] = {
                    "forms_of_address": [],
                    "ongoing_state": "polished",
                    "open_threads": [],
                    "active_expressions": [],
                }
            return out
        return None

    pid, windows = _setup(client, wait_task, f.on(h))
    r = client.post(f"/api/v1/projects/{pid}/translation/start", json={})
    task_id = r.json()["task_id"]
    for _ in range(60):
        asyncio.run(run_once())
        with dbmod.SessionLocal() as db:
            task = db.get(Task, task_id)
            if task.status in ("completed", "failed"):
                break
    assert task.status == "completed", task.error_message
    with dbmod.SessionLocal() as db:
        progress = json.loads(task.progress_json)
        carry_map = progress.get("draft_carry") or {}
        expected = {f"WIN_{i:04d}" for i in range(len(windows))}
        assert set(carry_map.keys()) == expected, f"draft carry 不完整: {carry_map.keys()}"
        items = list(db.scalars(select(TaskItem).where(TaskItem.task_id == task_id)))
        assert all(it.status == "succeeded" for it in items)
