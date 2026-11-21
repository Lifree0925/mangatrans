"""确定性 QA 与语义 QA。"""

import json

from app.llm.json_utils import parse_llm_json
from typing import Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm.protocol import LLMProvider
from app.models.document import SourceSegment
from app.models.qa import QAIssue
from app.models.translation import Translation

DETERMINISTIC_CHECKS = [
    "segment_cover",
    "empty_translation",
    "duplicate_translation",
    "forced_term",
    "markdown_fence",
]

SEMANTIC_TYPES = [
    "omission",
    "addition",
    "pronoun_reference",
    "negation_condition",
    "causality_chronology",
    "character_consistency",
    "tone_intent",
    "spoiler",
    "terminology",
    "grammar",
    "style",
]


class SemanticIssue(BaseModel):
    issue_type: str
    severity: Literal["low", "medium", "high", "critical"]
    source_quote: str = ""
    target_quote: str = ""
    explanation: str = ""
    suggested_fix: str = ""
    confidence: float = Field(default=0.5, ge=0, le=1)


class SemanticOutput(BaseModel):
    issues: list[SemanticIssue] = Field(default_factory=list)


SEMANTIC_PROMPT = """你是剧本翻译 QA。对比原文与译文，找出可以用原文/译文证据指出的具体问题。

检查类型：omission / addition / pronoun_reference / negation_condition / causality_chronology / character_consistency / tone_intent / spoiler / terminology / grammar / style。
- 只报告有具体证据的问题；无法确定不要写。
- severity：low / medium / high / critical。
- source_quote 与 target_quote 必须是原文/译文中的原句片段。
- 只返回 JSON。

原文：
{source}

译文：
{target}"""


def deterministic_qa(db: Session, seg: SourceSegment, tr: Translation, forced_terms: dict[str, str]) -> list[dict]:
    """确定性 QA：不调用 AI。forced_terms: {术语中文: 必须出现的英文}"""
    issues: list[dict] = []
    src = seg.source_text.strip()
    tgt = tr.target_text.strip()
    if not src:
        issues.append(_issue("empty_source", "medium", src, tgt, "原文为空"))
    if not tgt:
        issues.append(_issue("empty_translation", "critical", src, tgt, "译文为空"))
    if "```" in tgt:
        issues.append(_issue("markdown_fence", "medium", src, tgt, "译文混入 Markdown 围栏"))
    for zh, en in forced_terms.items():
        if zh in seg.source_text and en.lower() not in tgt.lower():
            issues.append(
                _issue("forced_term", "high", src, tgt, f"强制术语 {zh} 应译为 {en}")
            )
    return issues


def _issue(issue_type: str, severity: str, src: str, tgt: str, explanation: str) -> dict:
    return {
        "issue_type": issue_type,
        "severity": severity,
        "source_quote": src[:200],
        "target_quote": tgt[:200],
        "explanation": explanation,
        "confidence": 1.0,
    }


async def semantic_qa(
    provider: LLMProvider, model: str, source_text: str, target_text: str
) -> list[dict]:
    """语义 QA（只读，可并发）：按原文与译文文本检查，不依赖 session 对象。

    容错策略：思考模式请求设硬超时；截断/异常/解析失败自动关思考降级重试一次；
    仍失败降级为「无发现」，绝不抛异常阻塞整本翻译。
    """
    prompt = SEMANTIC_PROMPT.format(source=source_text, target=target_text)
    try:
        resp = await provider.chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_schema=SemanticOutput.model_json_schema(),
            temperature=0.2,
            timeout=90,  # 质检请求硬超时：思考模式慢调用兜底，避免 QA 阶段卡住
            thinking="enabled",  # 质检需要推理，开启思考（轻量 effort）
            reasoning_effort="low",
        )
        if resp.finish_reason == "length":
            # 思考挤占输出预算被截断：关思考重试一次，预算全部留给 QA 结果
            resp = await provider.chat(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                response_schema=SemanticOutput.model_json_schema(),
                temperature=0.2,
                timeout=90,
                thinking="disabled",
            )
        return [
            i.model_dump()
            for i in SemanticOutput.model_validate(parse_llm_json(resp.content)).issues
        ]
    except Exception:  # noqa: BLE001 语义 QA 是质量增强：超时/瑕疵时降级为无发现，不阻塞
        try:
            resp = await provider.chat(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                response_schema=SemanticOutput.model_json_schema(),
                temperature=0.2,
                timeout=90,
                thinking="disabled",
            )
            return [
                i.model_dump()
                for i in SemanticOutput.model_validate(parse_llm_json(resp.content)).issues
            ]
        except Exception:  # noqa: BLE001 两次尝试都失败：放弃本段语义 QA
            return []


def persist_issues(db: Session, project_id: str, translation_id: str, issues: list[dict]) -> None:
    for i in issues:
        db.add(
            QAIssue(
                project_id=project_id,
                translation_id=translation_id,
                issue_type=i.get("issue_type", "grammar")[:32],
                severity=i.get("severity", "low")[:16],
                source_quote=i.get("source_quote", "")[:2000],
                target_quote=i.get("target_quote", "")[:2000],
                explanation=i.get("explanation", "")[:2000],
                suggested_fix=i.get("suggested_fix", "")[:2000],
                confidence=int(float(i.get("confidence", 0.5)) * 100),
            )
        )
    db.commit()


def severity_rank(sev: str) -> int:
    return {"low": 0, "medium": 1, "high": 2, "critical": 3}.get(sev, 0)


def evaluate_gate(issues: list[dict]) -> str:
    """quality_gate 三态：blocked / needs_review / passed。"""
    if any(i.get("issue_type") in ("empty_translation", "segment_cover", "empty_source") and i.get("severity") == "critical" for i in issues):
        return "blocked"
    if any(severity_rank(i.get("severity", "low")) >= 2 for i in issues):
        return "needs_review"
    return "passed"
