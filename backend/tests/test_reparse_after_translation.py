"""回归：翻译过的文档重新解析必须成功（旧译文随解析清理）。"""

import time

import app.core.database as dbmod
from app.llm.fake import FakeProvider
from app.llm.registry import set_provider
from app.models.task import Task
from app.models.translation import ProviderConfig, Translation


def _setup(client, wait_task) -> str:
    with dbmod.SessionLocal() as db:
        db.add(ProviderConfig(
            display_name="t", base_url="http://127.0.0.1:9", model_name="m",
            credential_ref="ref-x", is_default=True, is_verified=True))
        db.commit()
    pid = client.post("/api/v1/projects", json={"name": "重解析测试"}).json()["id"]
    client.post(f"/api/v1/projects/{pid}/documents/import",
                files={"file": ("s.txt", "林风：我们走。\n夜色降临。\n".encode(), "text/plain")})
    wait_task(client, client.post(f"/api/v1/projects/{pid}/parse", json={}).json()["task_id"])
    return pid


def _fake() -> FakeProvider:
    import re
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

    def any_other(messages):
        content = messages[-1]["content"]
        if "剧本分析助手" in content:
            return {"items": []}
        if "剧本情境分析助手" in content:
            return SceneCardModel().model_dump()
        if "风格总监" in content:
            return {"character_voices": [], "dialogue_register": "", "action_description_style": "",
                    "cultural_strategy": "", "capitalization_rules": "", "examples": []}
        if "剧本翻译 QA" in content:
            return {"issues": []}
        return None

    f.on(translate_h).on(any_other)
    return f


def test_reparse_after_translation(client, wait_task) -> None:
    pid = _setup(client, wait_task)
    set_provider(_fake())

    # 一键直出完成，产生译文
    r = client.post(f"/api/v1/projects/{pid}/translation/express", json={})
    tid = r.json()["task_id"]
    for _ in range(100):
        with dbmod.SessionLocal() as db:
            s = db.get(Task, tid).status
        if s in ("completed", "failed"):
            break
        time.sleep(0.2)
    assert s == "completed", s
    with dbmod.SessionLocal() as db:
        assert db.query(Translation).count() > 0

    # 重复解析：必须 200，且旧译文已清理
    r = client.post(f"/api/v1/projects/{pid}/parse", json={})
    result = wait_task(client, r.json()["task_id"])
    assert r.status_code == 200, r.text
    assert result["status"] == "completed"
    assert result["progress"]["coverage_ok"] is True
    with dbmod.SessionLocal() as db:
        assert db.query(Translation).count() == 0
