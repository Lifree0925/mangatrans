"""任务恢复测试：线程型任务失败后 resume 能真正重新执行。"""

import json
import time

import app.core.database as dbmod
from app.llm.fake import FakeProvider
from app.llm.protocol import LLMError
from app.llm.registry import set_provider
from app.models.task import Task


class _BrokenProvider:
    """首次执行必失败的 Provider。"""

    async def chat(self, **kwargs):
        raise LLMError("模拟瞬时故障", retryable=True, code="network")

    async def test_connection(self):
        from app.llm.protocol import ProviderTestResult

        return ProviderTestResult(ok=False, model_name="broken", detail="")


def _setup(client, provider, wait_task) -> str:
    import app.core.database as dbmod
    from app.models.translation import ProviderConfig

    with dbmod.SessionLocal() as db:
        db.add(
            ProviderConfig(
                display_name="t",
                base_url="http://127.0.0.1:9",
                model_name="m",
                credential_ref="ref-t",
                is_default=True,
                is_verified=True,
            )
        )
        db.commit()
    r = client.post("/api/v1/projects", json={"name": "恢复测试", "workflow_mode": "express"})
    pid = r.json()["id"]
    client.post(
        f"/api/v1/projects/{pid}/documents/import",
        files={"file": ("s.txt", "林风：我们走。\n夜色降临。\n".encode(), "text/plain")},
    )
    wait_task(client, client.post(f"/api/v1/projects/{pid}/parse", json={}).json()["task_id"])
    set_provider(provider)
    return pid


def _wait(task_id: str, timeout: float = 30) -> str:
    t0 = time.time()
    while time.time() - t0 < timeout:
        with dbmod.SessionLocal() as db:
            task = db.get(Task, task_id)
            if task.status in ("completed", "failed"):
                return task.status
        time.sleep(0.2)
    return "timeout"


def _universal_fake() -> FakeProvider:
    from app.schemas.knowledge import SceneCardModel

    f = FakeProvider()

    def knowledge_h(messages):
        if "剧本分析助手" in messages[-1]["content"]:
            return {"items": []}
        return None

    def card_h(messages):
        if "剧本情境分析助手" in messages[-1]["content"]:
            return SceneCardModel().model_dump()
        return None

    def guide_h(messages):
        if "风格总监" in messages[-1]["content"]:
            return {
                "character_voices": [],
                "dialogue_register": "",
                "action_description_style": "",
                "cultural_strategy": "",
                "capitalization_rules": "",
                "examples": [],
            }
        return None

    def translate_h(messages):
        from conftest import fake_translation_entries

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

    def qa_h(messages):
        if "剧本翻译 QA" in messages[-1]["content"]:
            return {"issues": []}
        return None

    f.on(knowledge_h).on(card_h).on(guide_h).on(translate_h).on(qa_h)
    return f


def test_express_fail_then_resume_completes(client, wait_task) -> None:
    pid = _setup(client, _BrokenProvider(), wait_task)
    r = client.post(f"/api/v1/projects/{pid}/translation/express", json={})
    task_id = r.json()["task_id"]
    assert _wait(task_id) == "failed"

    # 故障消除后 resume：应真正重新执行并完成
    set_provider(_universal_fake())
    r = client.post(f"/api/v1/tasks/{task_id}/resume")
    assert r.json()["status"] == "running"
    assert _wait(task_id, timeout=30) == "completed"


def test_express_fail_then_retry_completes(client, wait_task) -> None:
    pid = _setup(client, _BrokenProvider(), wait_task)
    r = client.post(f"/api/v1/projects/{pid}/translation/express", json={})
    task_id = r.json()["task_id"]
    assert _wait(task_id) == "failed"

    set_provider(_universal_fake())
    r = client.post(f"/api/v1/tasks/{task_id}/retry")
    assert r.json()["status"] == "running"
    assert _wait(task_id, timeout=30) == "completed"
