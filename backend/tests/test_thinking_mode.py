"""思考模式分层：Draft/知识等环节关闭思考，Polish/QA 开启思考。"""

import json
import re
import time

import app.core.database as dbmod
from app.llm.fake import FakeProvider
from app.llm.registry import set_provider
from app.models.task import Task
from app.models.translation import ProviderConfig


def _setup(client, wait_task):
    with dbmod.SessionLocal() as db:
        db.add(ProviderConfig(display_name="t", base_url="http://127.0.0.1:9",
                              model_name="m", credential_ref="r", is_default=True, is_verified=True))
        db.commit()
    pid = client.post("/api/v1/projects", json={"name": "thinking 测试"}).json()["id"]
    body = "\n\n".join(
        [f"林风：第{i}句话，我们要坚持到底。" for i in range(40)]
        + [f"夜色深沉（{i}）。" for i in range(20)]
    )
    client.post(f"/api/v1/projects/{pid}/documents/import",
                files={"file": ("s.txt", body.encode(), "text/plain")})
    wait_task(client, client.post(f"/api/v1/projects/{pid}/parse", json={}).json()["task_id"])
    return pid


def _fake() -> FakeProvider:
    from app.schemas.knowledge import SceneCardModel

    f = FakeProvider()

    def translate_h(messages):
        ids = re.findall(r"\[([a-f0-9]{32})\]", messages[-1]["content"])
        if "FAITHFUL DRAFT" in messages[0]["content"]:
            return {"window_id": "W", "translations": [
                {"segment_id": i, "target_text": "D", "notes": [], "ambiguity": []} for i in ids]}
        if "POLISH" in messages[0]["content"]:
            return {"window_id": "W", "translations": [
                {"segment_id": i, "target_text": "P", "notes": [], "ambiguity": []} for i in ids],
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


def test_thinking_mode_layered(client, wait_task) -> None:
    """润色（Polish）与质检（QA）开思考；Draft 等其余环节显式关闭。"""
    pid = _setup(client, wait_task)
    f = _fake()
    set_provider(f)
    r = client.post(f"/api/v1/projects/{pid}/translation/express", json={})
    assert r.status_code == 200
    tid = r.json()["task_id"]
    assert _wait(tid) == "completed", "express 未完成"

    calls = f.calls
    draft_calls = [c for c in calls if "FAITHFUL DRAFT" in (c["messages"][0]["content"] or "")]
    polish_calls = [c for c in calls if "POLISH pass" in (c["messages"][0]["content"] or "")]
    qa_calls = [c for c in calls if "剧本翻译 QA" in (c["messages"][-1]["content"] or "")]

    assert draft_calls, "应有 Draft 调用"
    assert polish_calls, "应有 Polish 调用"
    # 知识抽取等其它环节均显式关闭思考
    assert all(c["thinking"] == "disabled" for c in draft_calls)
    assert all(c["thinking"] == "enabled" for c in polish_calls)
    assert all(c.get("thinking") == "disabled" for c in calls if c not in polish_calls and c not in qa_calls)
    # QA 语义调用开启思考（本测试无风险窗口，语义 QA 可能不触发；仅校验若有则 enabled）
    if qa_calls:
        assert all(c["thinking"] == "enabled" for c in qa_calls)
