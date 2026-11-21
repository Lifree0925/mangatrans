"""试译与风格基线集成测试（FakeProvider）。"""

import time

from fastapi.testclient import TestClient

import app.core.database as dbmod
from app.llm.fake import FakeProvider
from app.llm.registry import set_provider
from app.models.task import Task


def _add_verified_provider() -> None:
    """测试用：登记一个已验证的模型（翻译任务前置检查需要）。"""
    import app.core.database as dbmod
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


def _setup(client: TestClient, wait_task) -> str:
    pid = client.post("/api/v1/projects", json={"name": "试译测试"}).json()["id"]
    script = """第一集 初遇
【咖啡厅·日内】
林晓：（走进来）你好。
王强：你是谁？

（两人对视）
【街头·夜外】
林晓：别过来！
王强：为什么躲着我？
"""
    r = client.post(
        f"/api/v1/projects/{pid}/documents/import",
        files={"file": ("s.txt", script.encode(), "text/plain")},
    )
    assert r.status_code == 201
    assert wait_task(client, client.post(f"/api/v1/projects/{pid}/parse").json()["task_id"])["status"] == "completed"
    _add_verified_provider()
    return pid


def _fake() -> FakeProvider:
    f = FakeProvider()

    def draft_handler(messages):
        text = messages[-1]["content"]
        if "FAITHFUL DRAFT" in text or "draft" not in text:
            pass
        system = messages[0]["content"] if messages else ""
        if "FAITHFUL DRAFT pass" in system:
            return None  # 用默认翻译处理器
        return None

    def translate_handler(messages):
        content = messages[-1]["content"]
        if "SOURCE SEGMENTS" in content:
            # Draft 与 Polish 都返回逐段译文；Draft 用直译标记，Polish 用自然译文
            import re
            ids = re.findall(r'segment_id["\']?\s*[:=]\s*["\']?(S[A-Z0-9]+)', content)
            if not ids:
                # 从 JSON 块里找 segment_id 列表不可行时，返回空由校验兜底
                pass
            system = messages[0]["content"] if messages else ""
            natural = "POLISH pass" in system
            marker = "（naturalized）" if natural else "（literal）"
            items = [
                {
                    "segment_id": seg_id,
                    "target_text": f"EN-{seg_id}-{marker}".strip("-"),
                    "notes": [],
                    "ambiguity": [],
                }
                for seg_id in _seg_ids
            ]
            out = {"window_id": "W1", "translations": items}
            if natural:
                out["carry_over"] = {"forms_of_address": [], "ongoing_state": "", "open_threads": [], "active_expressions": []}
            return out
        return None

    def guide_handler(messages):
        content = messages[-1]["content"]
        if "风格基线" in content or "Style Guide" in content and "试译样本" in content:
            return {
                "character_voices": [{"name": "林晓", "voice": "calm"}],
                "dialogue_register": "natural spoken",
                "action_description_style": "concise",
                "cultural_strategy": "moderate localization",
                "capitalization_rules": "title case for names",
                "examples": [{"zh": "你好", "en": "Hello"}],
            }
        return None

    f.on(guide_handler).on(translate_handler)
    return f


_seg_ids: list[str] = []


def _collect_seg_ids(client: TestClient, pid: str) -> None:
    global _seg_ids
    _seg_ids = [s["id"] for s in client.get(f"/api/v1/projects/{pid}/segments").json()]


def _wait_task(task_id: str, timeout: float = 30) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        with dbmod.SessionLocal() as db:
            task = db.get(Task, task_id)
            if task.status in ("completed", "failed"):
                return {"status": task.status, "error": task.error_message,
                        "progress": task.progress_json}
        time.sleep(0.1)
    return {"status": "timeout", "error": ""}


def test_trial_creates_translations_and_style_guide(client: TestClient, wait_task) -> None:
    set_provider(_fake())
    pid = _setup(client, wait_task)
    _collect_seg_ids(client, pid)

    r = client.post(f"/api/v1/projects/{pid}/translation/trial", json={})
    assert r.status_code == 200
    result = _wait_task(r.json()["task_id"])
    assert result["status"] == "completed", result["error"]

    sg = client.get(f"/api/v1/projects/{pid}/style-guide").json()
    assert sg["exists"] is True
    assert sg["status"] == "frozen"
    assert sg["guide"]["dialogue_register"] == "natural spoken"

    # 试译译文入库（draft + polished 两个 stage）
    segs = client.get(f"/api/v1/projects/{pid}/segments").json()
    dialogue_ids = [s["id"] for s in segs if s["segment_type"] == "dialogue"]
    t = client.get(f"/api/v1/projects/{pid}/segments/{dialogue_ids[0]}/translations").json()
    stages = {x["stage"] for x in t["translations"]}
    assert {"draft", "polished"} <= stages
