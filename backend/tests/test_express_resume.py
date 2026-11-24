"""Express 断点续跑：只补未完成窗口，已初翻/已润色部分不重复执行。"""

import json
import re
import time

from sqlalchemy import select

import app.core.database as dbmod
from app.llm.fake import FakeProvider
from app.llm.registry import set_provider
from app.models.document import SourceSegment
from app.models.task import Task
from app.models.translation import ProviderConfig, Translation
from app.services.window_planner import build_windows


def _setup(client, wait_task):
    with dbmod.SessionLocal() as db:
        db.add(ProviderConfig(
            display_name="t", base_url="http://127.0.0.1:9",
            model_name="m", credential_ref="r", is_default=True, is_verified=True,
        ))
        db.commit()
    pid = client.post("/api/v1/projects", json={"name": "续跑测试"}).json()["id"]
    # 体量必须超过单个 1800 字符窗口，确保存在多个翻译窗口。
    body = "\n\n".join(
        [f"林风：第{i}句话，我们要坚持到底，绝不后退。" for i in range(100)]
        + [f"夜色深沉，风声穿过长街（{i}）。" for i in range(50)]
    )
    client.post(
        f"/api/v1/projects/{pid}/documents/import",
        files={"file": ("s.txt", body.encode(), "text/plain")},
    )
    wait_task(
        client,
        client.post(f"/api/v1/projects/{pid}/parse", json={}).json()["task_id"],
    )
    return pid


def _fake() -> FakeProvider:
    from app.schemas.knowledge import SceneCardModel

    from conftest import fake_translation_entries

    f = FakeProvider()

    def translate_h(messages):
        content = messages[-1]["content"]
        if "FAITHFUL DRAFT" in messages[0]["content"]:
            return {
                "window_id": "W",
                "translations": fake_translation_entries(content, "Draft", stage="draft"),
            }
        if "POLISH" in messages[0]["content"]:
            return {
                "window_id": "W",
                "translations": fake_translation_entries(content, "Polish", stage="polished"),
                "carry_over": {},
            }
        return None

    def other_h(messages):
        c = messages[-1]["content"]
        if "剧本分析助手" in c:
            return {"items": []}
        if "剧本情境分析助手" in c:
            return SceneCardModel().model_dump()
        if "风格总监" in c:
            return {
                "character_voices": [], "dialogue_register": "",
                "action_description_style": "", "cultural_strategy": "",
                "capitalization_rules": "", "examples": [],
            }
        if "剧本翻译 QA" in c:
            return {"issues": []}
        return None

    f.on(translate_h).on(other_h)
    return f


def _wait(task_id: str, timeout: float = 60) -> str:
    t0 = time.time()
    while time.time() - t0 < timeout:
        with dbmod.SessionLocal() as db:
            s = db.get(Task, task_id).status
        if s in ("completed", "failed"):
            return s
        time.sleep(0.2)
    return "timeout"


class _RecordingProvider:
    """记录重试期间真正发起的 Draft 段，用于验证不会重复初翻。"""

    def __init__(self, inner):
        self.inner = inner
        self.draft_ids: set[str] = set()

    async def chat(self, **kwargs):
        messages = kwargs.get("messages") or []
        system = messages[0].get("content", "") if messages else ""
        user = messages[-1].get("content", "") if messages else ""
        if "FAITHFUL DRAFT" in system:
            self.draft_ids.update(re.findall(r"\[([0-9a-f]{32})\]", user))
        return await self.inner.chat(**kwargs)

    async def test_connection(self):
        return await self.inner.test_connection()


def test_express_retry_skips_draft_and_polished_windows(client, wait_task) -> None:
    pid = _setup(client, wait_task)
    fake = _fake()
    set_provider(fake)
    tid = client.post(f"/api/v1/projects/{pid}/translation/express", json={}).json()["task_id"]
    assert _wait(tid, timeout=90) == "completed"

    # 模拟“上次只完成了一部分”的失败任务：首个窗口的 polished 失效，
    # 同时让旧进度丢失 draft_carry、done 归零。重试必须按 DB 实际状态恢复。
    with dbmod.SessionLocal() as db:
        segs = list(
            db.scalars(
                select(SourceSegment)
                .where(SourceSegment.project_id == pid)
                .order_by(SourceSegment.segment_no)
            )
        )
        windows = build_windows(segs)
        assert len(windows) >= 2, "测试剧本必须至少两个窗口"
        target_seg = windows[0][0]
        latest = db.scalar(
            select(Translation)
            .where(
                Translation.segment_id == target_seg.id,
                Translation.stage == "polished",
            )
            .order_by(Translation.version.desc())
            .limit(1)
        )
        assert latest is not None
        latest.freshness_status = "stale"
        task = db.get(Task, tid)
        progress = json.loads(task.progress_json)
        translation = progress["steps"]["translation"]
        translation["done"] = 0
        translation.pop("draft_carry", None)
        task.progress_json = json.dumps(progress, ensure_ascii=False)
        task.status = "failed"
        task.error_code = "items_failed"
        task.error_message = "模拟部分窗口未完成"
        db.commit()

    recording = _RecordingProvider(fake)
    set_provider(recording)
    r = client.post(f"/api/v1/tasks/{tid}/retry")
    assert r.status_code == 200
    status = _wait(tid, timeout=90)
    with dbmod.SessionLocal() as db:
        failed_task = db.get(Task, tid)
        error = failed_task.error_message
    assert status == "completed", error

    # 未完成窗口的 Draft 已存在，因此重试不应发起任何 Draft 请求。
    assert recording.draft_ids == set(), f"重试重复初翻: {recording.draft_ids}"

    with dbmod.SessionLocal() as db:
        task = db.get(Task, tid)
        steps = json.loads(task.progress_json)["steps"]
        assert steps["translation"]["done"] == steps["translation"]["total"]
        assert steps["qa"]["done"] == steps["qa"]["total"]
        for seg in db.scalars(
            select(SourceSegment).where(SourceSegment.project_id == pid)
        ):
            current = db.scalar(
                select(Translation)
                .where(
                    Translation.segment_id == seg.id,
                    Translation.stage == "polished",
                    Translation.freshness_status == "current",
                )
                .order_by(Translation.version.desc())
                .limit(1)
            )
            assert current is not None and (current.target_text or "").strip()
