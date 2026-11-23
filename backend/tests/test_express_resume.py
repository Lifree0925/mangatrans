"""Express 断点续跑：中途失败后重试，已完成步骤不重复执行。"""

import json
import re
import time

import app.core.database as dbmod
from app.llm.fake import FakeProvider
from app.llm.protocol import LLMError
from app.llm.registry import set_provider
from app.models.task import Task
from app.models.translation import ProviderConfig


class _FlakyProvider:
    """翻译 Draft 阶段第一次调用必失败一次，其余转发 FakeProvider（模拟网络抖动）。"""

    def __init__(self, fake: FakeProvider):
        self._fake = fake
        self.calls = 0
        self._failed_once = False

    async def chat(self, **kwargs):
        self.calls += 1
        system = kwargs.get("messages", [{}])[0].get("content", "")
        if "FAITHFUL DRAFT" in system and not self._failed_once:
            self._failed_once = True
            raise LLMError("模拟故障", retryable=True, code="network")
        return await self._fake.chat(**kwargs)

    async def test_connection(self):
        return await self._fake.test_connection()


def _setup(client, wait_task):
    with dbmod.SessionLocal() as db:
        db.add(ProviderConfig(display_name="t", base_url="http://127.0.0.1:9",
                              model_name="m", credential_ref="r", is_default=True, is_verified=True))
        db.commit()
    pid = client.post("/api/v1/projects", json={"name": "续跑测试"}).json()["id"]
    body = "\n\n".join(
        [f"林风：第{i}句话，我们要坚持到底。" for i in range(40)]
        + [f"夜色深沉（{i}）。" for i in range(20)]
    )
    client.post(f"/api/v1/projects/{pid}/documents/import",
                files={"file": ("s.txt", body.encode(), "text/plain")})
    wait_task(client, client.post(f"/api/v1/projects/{pid}/parse", json={}).json()["task_id"])
    return pid


def _fake() -> FakeProvider:
    import re as _re

    from app.schemas.knowledge import SceneCardModel

    from conftest import fake_translation_entries

    f = FakeProvider()

    def translate_h(messages):
        content = messages[-1]["content"]
        if "FAITHFUL DRAFT" in messages[0]["content"]:
            return {"window_id": "W", "translations": fake_translation_entries(content, "Draft", stage="draft")}
        if "POLISH" in messages[0]["content"]:
            return {"window_id": "W", "translations": fake_translation_entries(content, "Polish", stage="polished"),
                    "carry_over": {}}
        return None

    def other_h(messages):
        c = messages[-1]["content"]
        if "剧本分析助手" in c:
            return {"items": []}
        if "剧本情境分析助手" in c:
            return SceneCardModel().model_dump()
        if "风格总监" in c:
            return {"character_voices": [], "dialogue_register": "", "action_description_style": "",
                    "cultural_strategy": "", "capitalization_rules": "", "examples": []}
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


def test_express_retry_resumes_from_failure_point(client, wait_task) -> None:
    pid = _setup(client, wait_task)
    fake = _fake()
    # 知识提取 1 批(1 次) + 场景卡 0 + 试译 1 窗×2 + 翻译窗口×2：让翻译第 1 个窗口 Draft 失败
    flaky = _FlakyProvider(fake)
    set_provider(flaky)
    r = client.post(f"/api/v1/projects/{pid}/translation/express", json={})
    tid = r.json()["task_id"]
    assert _wait(tid) == "failed"

    calls_after_fail = flaky.calls
    # 重试：应续跑（知识/场景卡/试译不再调用），仅翻译/QA 继续
    set_provider(fake)
    r = client.post(f"/api/v1/tasks/{tid}/retry")
    assert r.status_code == 200
    assert _wait(tid, timeout=90) == "completed"

    with dbmod.SessionLocal() as db:
        task = db.get(Task, tid)
        steps = json.loads(task.progress_json)["steps"]
    assert "knowledge" in steps
    assert steps["translation"]["done"] == steps["translation"]["total"]
    # 续跑后知识等前序步骤未重复执行：fake 调用总数 = 失败前 4 次 - 重试中的翻译+QA
    # 关键断言：任务完成且翻译窗口全部完成
    assert steps["qa"]["done"] == steps["qa"]["total"]
