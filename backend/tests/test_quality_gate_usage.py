"""自动质量门禁、交付闸门、阶段用量与确定性台词本回归。"""

import asyncio
import re

import app.core.database as dbmod
from app.llm.fake import FakeProvider
from app.llm.protocol import ProviderTestResult
from app.llm.registry import set_provider
from app.llm.usage import record_usage, usage_context
from app.models.dialogue import DialogueLine
from app.models.translation import ProviderConfig, Translation
from conftest import fake_translation_entries


def _provider_config() -> None:
    with dbmod.SessionLocal() as db:
        db.add(
            ProviderConfig(
                display_name="gate", base_url="http://fake", model_name="fake-model",
                credential_ref="gate-ref", is_default=True, is_verified=True,
            )
        )
        db.commit()


def _setup(client, wait_task, body: str = "林风：我们走。\n夜色降临。\n") -> str:
    _provider_config()
    pid = client.post("/api/v1/projects", json={"name": "质量门禁"}).json()["id"]
    client.post(
        f"/api/v1/projects/{pid}/documents/import",
        files={"file": ("s.txt", body.encode(), "text/plain")},
    )
    wait_task(client, client.post(f"/api/v1/projects/{pid}/parse", json={}).json()["task_id"])
    return pid


def _valid_fake(*, qa_fail: bool = False) -> FakeProvider:
    f = FakeProvider()

    def handler(messages):
        prompt = messages[-1]["content"]
        system = messages[0]["content"] if messages else ""
        if "终审 QA" in prompt:
            if qa_fail:
                raise RuntimeError("模拟质检服务不可用")
            return {"issues": []}
        if "FAITHFUL DRAFT" in system or "POLISH pass" in system:
            return {
                "window_id": "W",
                "translations": fake_translation_entries(
                    prompt, "Polished" if "POLISH pass" in system else "Draft",
                    stage="polished" if "POLISH pass" in system else "draft",
                ),
                "carry_over": {} if "POLISH pass" in system else None,
            }
        return None

    return f.on(handler)


def test_qa_incomplete_never_marks_task_success(client, wait_task) -> None:
    pid = _setup(client, wait_task)
    set_provider(_valid_fake(qa_fail=True))
    tid = client.post(f"/api/v1/projects/{pid}/translation/express", json={}).json()["task_id"]
    result = wait_task(client, tid, timeout=60)
    assert result["status"] == "failed"
    assert "自动质检未通过" in (result["error_message"] or "")

    with dbmod.SessionLocal() as db:
        approved = db.query(Translation).filter(Translation.review_status == "approved").count()
        assert approved == 0


def test_manual_edit_cannot_bypass_auto_gate(client, wait_task) -> None:
    pid = _setup(client, wait_task)
    with dbmod.SessionLocal() as db:
        from app.models.document import SourceSegment
        from sqlalchemy import select
        seg = db.scalar(select(SourceSegment).where(SourceSegment.project_id == pid).limit(1))
        seg_id = seg.id
    r = client.post(
        f"/api/v1/projects/{pid}/segments/{seg_id}/translations",
        json={"target_text": "Manual line."},
    )
    assert r.status_code == 201

    approved = client.post(f"/api/v1/projects/{pid}/approve/batch").json()
    assert approved["approved"] == 0
    assert approved["gate"] != "passed"

    export = client.post(
        f"/api/v1/projects/{pid}/export",
        json={"delivery": True, "bilingual": False, "format": "md"},
    )
    assert export.status_code == 409
    assert "自动质检未通过" in export.json()["detail"]


def test_usage_ledger_is_exposed_on_task(client, wait_task) -> None:
    pid = _setup(client, wait_task)
    tid = client.post(f"/api/v1/projects/{pid}/translation/start", json={}).json()["task_id"]
    with usage_context(tid, pid):
        record_usage(
            stage="draft", model_name="fake-model", provider_id="fake",
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            latency_ms=12.5, finish_reason="stop",
        )
        record_usage(
            stage="qa", model_name="fake-model", provider_id="fake",
            usage={"prompt_tokens": 70, "completion_tokens": 10, "total_tokens": 80},
            cache_hit=True, finish_reason="stop",
        )
    payload = client.get(f"/api/v1/tasks/{tid}").json()
    usage = payload["usage"]
    assert usage["requests"] == 2
    assert usage["api_total_tokens"] == 150
    assert usage["cache_hits"] == 1
    assert usage["by_stage"]["draft"]["requests"] == 1


def test_dialogue_extraction_is_deterministic_and_ordered(client, wait_task) -> None:
    from app.services import dialogue_service

    pid = _setup(client, wait_task, "第1集 开始\n\n【房间·日】\n林风：第一句。\n夜色降临。\n林风：第二句。\n")
    class _NoCallProvider:
        async def chat(self, **kwargs):
            raise AssertionError("台词本抽取不应调用模型")
        async def test_connection(self):
            return ProviderTestResult(ok=True, model_name="no-call")

    from app.models.document import SourceDocument

    with dbmod.SessionLocal() as db:
        doc_id = db.query(SourceDocument).filter_by(project_id=pid).one().id
    result = asyncio.run(dialogue_service.extract_dialogue_lines(
        lambda: dbmod.SessionLocal(), pid, doc_id, _NoCallProvider(), "fake-model",
    ))
    assert result["extracted"] == 2
    with dbmod.SessionLocal() as db:
        rows = list(db.query(DialogueLine).filter_by(project_id=pid).order_by(DialogueLine.line_no))
        assert [r.source_text for r in rows] == ["第一句。", "第二句。"]
        assert all(r.speaker_name == "林风" for r in rows)


def test_window_planner_uses_larger_windows() -> None:
    from app.models.document import SourceSegment
    from app.services.window_planner import TARGET_MAX, build_windows

    segs = [
        SourceSegment(
            id=f"{i:032x}", project_id="p", source_document_id="d", chapter_id="c",
            scene_id=None, segment_no=i, segment_type="narrative", speaker_name=None,
            source_text="长" * 300, source_start=0, source_end=300, source_hash="h",
        )
        for i in range(1, 13)
    ]
    windows = build_windows(segs)
    assert TARGET_MAX >= 1800
    assert len(windows) < 6
    assert all(sum(len(s.source_text) for s in w) <= TARGET_MAX for w in windows)


def test_openai_provider_records_stage_usage(client, wait_task) -> None:
    """Provider 包装层必须记录真实 stage，而不是只记一个笼统 llm。"""
    from app.llm.openai_compat import OpenAICompatProvider
    from app.llm.protocol import LLMResponse
    from app.models.usage import LLMCall

    pid = _setup(client, wait_task)
    tid = client.post(f"/api/v1/projects/{pid}/translation/start", json={}).json()["task_id"]

    class _Capture(OpenAICompatProvider):
        async def _chat(self, **kwargs):
            return LLMResponse(
                content="{}", provider_id="capture", model_name="fake-model",
                request_id="r", usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
                finish_reason="stop", latency_ms=3.0,
            )

    provider = _Capture(base_url="http://fake", api_key="", model="fake-model")
    with usage_context(tid, pid):
        asyncio.run(provider.chat(messages=[{"role": "user", "content": "x"}], stage="polish"))
    with dbmod.SessionLocal() as db:
        row = db.query(LLMCall).filter_by(task_id=tid, stage="polish").one()
        assert row.prompt_tokens == 11
        assert row.completion_tokens == 7
        assert row.success is True


def test_context_budget_trims_low_priority_blocks() -> None:
    from app.services.context_builder import DEFAULT_BUDGET, build_context

    res = build_context(
        segments=[{
            "segment_id": "s1", "segment_no": 1, "segment_type": "narrative",
            "speaker_name": "", "source_text": "短句" * 10,
        }],
        scene_card=None,
        knowledge=[
            {
                "id": "k1", "item_type": "rule", "canonical_key": "规则",
                "data": {"x": "很长的规则" * 200}, "aliases": [],
            }
        ],
        style_guide={"dialogue_register": "natural" * 100},
        carry_over=None,
        budget=20,
    )
    assert "SOURCE SEGMENTS" in res.prompt_text
    assert res.total_tokens <= DEFAULT_BUDGET
    assert res.report["trimmed"]


def test_quality_report_passes_and_auto_approves(client, wait_task) -> None:
    pid = _setup(client, wait_task)
    set_provider(_valid_fake())
    tid = client.post(f"/api/v1/projects/{pid}/translation/express", json={}).json()["task_id"]
    result = wait_task(client, tid, timeout=60)
    assert result["status"] == "completed", result.get("error_message")

    report = client.get(f"/api/v1/projects/{pid}/quality-report").json()
    assert report["gate"] == "passed"
    assert report["missing"] == 0
    assert report["qa_unverified"] == 0
    assert report["coverage_rate"] == 100.0
    assert report["qa_coverage_rate"] == 100.0
    assert report["term_consistency_rate"] == 100.0
    assert report["auto_approved_ready"] is True

    # 不依赖人工：再次请求自动定稿也应保持通过且不会回退。
    approved = client.post(f"/api/v1/projects/{pid}/approve/batch").json()
    assert approved["gate"] == "passed"


def test_quality_failure_retry_reruns_qa(client, wait_task) -> None:
    pid = _setup(client, wait_task)
    set_provider(_valid_fake(qa_fail=True))
    tid = client.post(f"/api/v1/projects/{pid}/translation/express", json={}).json()["task_id"]
    first = wait_task(client, tid, timeout=60)
    assert first["status"] == "failed"

    set_provider(_valid_fake())
    retry = client.post(f"/api/v1/tasks/{tid}/retry")
    assert retry.status_code == 200
    second = wait_task(client, tid, timeout=60)
    assert second["status"] == "completed", second.get("error_message")
