"""Express 断点续跑：只补未完成窗口，已初翻/已润色部分不重复执行。"""

import asyncio
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
from app.pipeline.worker import run_once
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


def _setup_multiline_structure(client, wait_task):
    """构造一个含多行动作块的项目，用于复现模型合并行导致的结构失败。"""
    with dbmod.SessionLocal() as db:
        db.add(ProviderConfig(
            display_name="t", base_url="http://127.0.0.1:9",
            model_name="m", credential_ref="r", is_default=True, is_verified=True,
        ))
        db.commit()
    pid = client.post("/api/v1/projects", json={"name": "结构恢复测试"}).json()["id"]
    dialogue = [f"林风：第{i}句话，我们要坚持到底，绝不后退。" for i in range(60)]
    marker = "△结构测试动作一。\n△结构测试动作二。"
    body = "\n\n".join(dialogue[:30] + [marker] + dialogue[30:])
    client.post(
        f"/api/v1/projects/{pid}/documents/import",
        files={"file": ("s.txt", body.encode(), "text/plain")},
    )
    wait_task(
        client,
        client.post(f"/api/v1/projects/{pid}/parse", json={}).json()["task_id"],
    )
    return pid


class _CorruptPolishProvider:
    """按需删掉多行译文的最后一行，模拟真实模型的“合并动作行”。"""

    def __init__(self, inner, *, always: bool = False):
        self.inner = inner
        self.always = always

    async def chat(self, **kwargs):
        resp = await self.inner.chat(**kwargs)
        if kwargs.get("stage") not in ("polish", "repair"):
            return resp
        messages = kwargs.get("messages") or []
        user = messages[-1].get("content", "") if messages else ""
        ids = set(re.findall(r"\[([0-9a-f]{32})\]", user))
        # 精简后的单段修复只含一个 segment id，给恢复路径一个成功出口。
        if not self.always and len(ids) <= 1:
            return resp
        try:
            data = json.loads(resp.content)
        except (TypeError, ValueError):
            return resp
        changed = False
        for entry in data.get("translations") or []:
            lines = (entry.get("target_text") or "").splitlines()
            if len(lines) > 1:
                entry["target_text"] = "\n".join(lines[:-1])
                changed = True
                break
        if changed:
            resp.content = json.dumps(data, ensure_ascii=False)
        return resp

    async def test_connection(self):
        return await self.inner.test_connection()


def _structured_fake() -> FakeProvider:
    """只从 SOURCE SEGMENTS 块生成译文，避免上下文/Draft 中重复 ID 干扰结构测试。"""
    from conftest import fake_translation_entries

    f = _fake()

    def translation_h(messages):
        system = messages[0].get("content", "") if messages else ""
        user = messages[-1].get("content", "") if messages else ""
        if "FAITHFUL DRAFT" not in system and "POLISH pass" not in system:
            return None
        marker = "SOURCE SEGMENTS (translate only these, one translation per segment_id):\n"
        if marker not in user:
            return None
        source = user.split(marker, 1)[1]
        for end in (
            "\nSTYLE GUIDE", "\nSCENE CONTEXT CARD", "\nSPOILER GUARD",
            "\nCHAPTER STORYLINE", "\nFORESHADOWING", "\nAPPROVED CHARACTERS",
            "\nCHARACTER PROFILES", "\nNAME-FORM MAP", "\nSPEAKER VOICE",
            "\nFORCED TERMS", "\nCONTEXTUAL TERMS", "\nRELEVANT RULES",
            "\nCARRY-OVER STATE", "\n[FAITHFUL DRAFT",
        ):
            if end in source:
                source = source.split(end, 1)[0]
        return {
            "window_id": "W",
            "translations": fake_translation_entries(
                source,
                "Polished" if "POLISH pass" in system else "Draft",
                stage="polished" if "POLISH pass" in system else "draft",
            ),
            "carry_over": {},
        }

    f.handlers.insert(0, translation_h)
    return f


def _prepare_structure_retry(client, wait_task):
    """先用正常 fake 跑完一次（trial 已写入进度），再让目标窗口需要重译。

    这样重试时不会重新执行 trial，避免 trial 的译文版本覆盖主翻译窗口，
    从而能单独验证 Polish 结构失败恢复路径。
    """
    pid = _setup_multiline_structure(client, wait_task)
    set_provider(_fake())
    tid = client.post(f"/api/v1/projects/{pid}/translation/express", json={}).json()["task_id"]
    first = wait_task(client, tid, timeout=90)
    assert first["status"] == "completed", first.get("error_message")

    with dbmod.SessionLocal() as db:
        seg = db.scalar(
            select(SourceSegment).where(
                SourceSegment.project_id == pid,
                SourceSegment.source_text.contains("结构测试动作一"),
            )
        )
        assert seg is not None
        tr = db.scalar(
            select(Translation)
            .where(Translation.segment_id == seg.id, Translation.stage == "polished")
            .order_by(Translation.version.desc())
            .limit(1)
        )
        assert tr is not None
        tr.freshness_status = "stale"
        task = db.get(Task, tid)
        task.status = "failed"
        task.error_code = "items_failed"
        task.error_message = "模拟结构失败重试"
        db.commit()
        target_id = seg.id
    return pid, tid, target_id


def test_polish_structure_failure_recovers_without_unpack_error(client, wait_task) -> None:
    pid, tid, target_id = _prepare_structure_retry(client, wait_task)
    set_provider(_CorruptPolishProvider(_structured_fake()))
    assert client.post(f"/api/v1/tasks/{tid}/retry").status_code == 200
    result = wait_task(client, tid, timeout=90)
    assert result["status"] == "completed", result.get("error_message")
    assert "not enough values" not in (result.get("error_message") or "")

    with dbmod.SessionLocal() as db:
        tr = db.scalar(
            select(Translation)
            .where(Translation.segment_id == target_id, Translation.stage == "polished")
            .order_by(Translation.version.desc())
            .limit(1)
        )
        assert tr is not None
        assert len([x for x in (tr.target_text or "").splitlines() if x.strip()]) == 2


def test_polish_structure_failure_has_actionable_error(client, wait_task) -> None:
    """持续结构失败时必须报窗口错误，不能把内部二元解包异常泄漏给用户。"""
    _pid, tid, _target_id = _prepare_structure_retry(client, wait_task)
    set_provider(_CorruptPolishProvider(_structured_fake(), always=True))
    assert client.post(f"/api/v1/tasks/{tid}/retry").status_code == 200
    result = wait_task(client, tid, timeout=90)
    assert result["status"] == "failed"
    message = result.get("error_message") or ""
    assert "个翻译窗口失败" in message
    assert "结构校验失败" in message
    assert "not enough values" not in message


def test_repeated_retries_after_structure_failure_eventually_complete(client, wait_task) -> None:
    """连续多次重试不能卡死；服务恢复后下一次重试应继续并完成。"""
    _pid, tid, _target_id = _prepare_structure_retry(client, wait_task)
    set_provider(_CorruptPolishProvider(_structured_fake(), always=True))

    for _ in range(2):
        assert client.post(f"/api/v1/tasks/{tid}/retry").status_code == 200
        failed = wait_task(client, tid, timeout=90)
        assert failed["status"] == "failed"
        message = failed.get("error_message") or ""
        assert "个翻译窗口失败" in message
        assert "not enough values" not in message
        with dbmod.SessionLocal() as db:
            assert db.get(Task, tid).status == "failed"

    # 模拟模型服务恢复后再次点击重试：不能因为前两次失败而保留死锁。
    set_provider(_CorruptPolishProvider(_structured_fake()))
    assert client.post(f"/api/v1/tasks/{tid}/retry").status_code == 200
    completed = wait_task(client, tid, timeout=90)
    assert completed["status"] == "completed", completed.get("error_message")


def _drive_worker_until_done(task_id: str, max_rounds: int = 80) -> str:
    status = "running"
    for _ in range(max_rounds):
        asyncio.run(run_once())
        with dbmod.SessionLocal() as db:
            task = db.get(Task, task_id)
            status = task.status
        if status in ("completed", "failed"):
            break
    return status


def test_full_retranslate_after_structure_failure_completes(client, wait_task) -> None:
    """Express 结构失败后，“全部重新翻译”应创建新任务并完整完成。"""
    pid, tid, target_id = _prepare_structure_retry(client, wait_task)
    set_provider(_CorruptPolishProvider(_structured_fake(), always=True))
    assert client.post(f"/api/v1/tasks/{tid}/retry").status_code == 200
    failed = wait_task(client, tid, timeout=90)
    assert failed["status"] == "failed"

    # 用户不再重试旧任务，直接点“全部重新翻译”。
    set_provider(_structured_fake())
    r = client.post(f"/api/v1/projects/{pid}/translation/start", json={})
    assert r.status_code == 200, r.text
    new_tid = r.json()["task_id"]
    assert _drive_worker_until_done(new_tid) == "completed"

    with dbmod.SessionLocal() as db:
        tr = db.scalar(
            select(Translation)
            .where(Translation.segment_id == target_id, Translation.stage == "polished")
            .order_by(Translation.version.desc())
            .limit(1)
        )
        assert tr is not None
        assert len([x for x in (tr.target_text or "").splitlines() if x.strip()]) == 2
    quality = client.get(f"/api/v1/projects/{pid}/quality-report").json()
    assert quality["gate"] == "passed", quality
    assert quality["missing"] == 0


def test_repeated_retry_calls_do_not_launch_duplicate_thread(
    client, wait_task, monkeypatch
) -> None:
    """双击/连续点击重试时，第一次已调度后第二次必须幂等返回。"""
    from app.api.v1 import settings_api

    pid = _setup_multiline_structure(client, wait_task)
    with dbmod.SessionLocal() as db:
        task = Task(project_id=pid, task_type="express", status="failed")
        db.add(task)
        db.commit()
        task_id = task.id

    launches: list[str] = []
    monkeypatch.setattr(settings_api, "launch_task", lambda tid: launches.append(tid))

    first = client.post(f"/api/v1/tasks/{task_id}/retry")
    second = client.post(f"/api/v1/tasks/{task_id}/retry")
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["status"] == "running"
    assert second.json()["status"] == "running"
    assert launches == [task_id]
