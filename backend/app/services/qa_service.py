"""确定性 QA 与语义 QA。"""

import json

from app.llm.json_utils import parse_llm_json
from typing import Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm.protocol import LLMProvider
from app.services.term_match import target_hits_term
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
    "terminology",
]

# 打回定点修复的核心类型：这些类型的 medium 也值得修（关系到理解/连续性）；
# 其余类型只有 high/critical 才修。纯风格/语法问题不再由语义 QA 报告。
CORE_ISSUE_TYPES = {
    "omission",
    "addition",
    "pronoun_reference",
    "negation_condition",
    "causality_chronology",
    "character_consistency",
}


def should_repair(issue: dict) -> bool:
    """是否打回定点修复：high/critical 一律修；核心类型的 medium 也修。"""
    sev = issue.get("severity", "low")
    if sev in ("high", "critical"):
        return True
    return sev == "medium" and issue.get("issue_type") in CORE_ISSUE_TYPES


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


SEMANTIC_PROMPT = """你是剧本翻译 QA。对比原文与译文，只排查会破坏剧本完整性的大问题，不要报告风格偏好、大小写、措辞润色等小问题。

检查类型（仅限以下）：
- omission：漏译——原文的情节、动作、台词、声音提示、场景信息在译文中缺失
- addition：加戏——译文出现了原文没有的情节/信息
- pronoun_reference：人称代词错乱——he/she 混用、指代对象错误
- negation_condition：否定/条件翻反——原文的否定、条件、因果被译成相反含义
- causality_chronology：剧情连续性——事件先后、因果链条与原文矛盾，或与已知剧情/场景设定冲突
- character_consistency：人物一致性——同一人物的身份、立场、关系前后矛盾（如把 A 说的话安到 B 头上）
- terminology：术语断裂——同一实体/地点在译文中出现第二种译名（注意：大小写差异、单复数差异不算问题）

severity 判定：
- critical：情节/人物关系被写错，会直接误导读者
- high：漏译重要情节或明显指代/因果错误
- medium：会干扰阅读理解的较小错误
- low：仅影响行文观感（此类不报告，直接跳过）

只报告有具体原文/译文证据的问题；无法确定的不写。
source_quote 与 target_quote 必须是原文/译文中的原句片段。
只返回 JSON。

原文：
{source}

译文：
{target}"""


def deterministic_qa(db: Session, seg: SourceSegment, tr: Translation, forced_terms: dict[str, dict]) -> list[dict]:
    """确定性 QA：不调用 AI。forced_terms: {术语中文: {"en": 标准译名, "variants": [...]}}"""
    issues: list[dict] = []
    src = seg.source_text.strip()
    tgt = tr.target_text.strip()
    if not src:
        issues.append(_issue("empty_source", "medium", src, tgt, "原文为空"))
    if not tgt:
        issues.append(_issue("empty_translation", "critical", src, tgt, "译文为空"))
    if "```" in tgt:
        issues.append(_issue("markdown_fence", "medium", src, tgt, "译文混入 Markdown 围栏"))
    for zh, spec in forced_terms.items():
        en = spec.get("en", "") if isinstance(spec, dict) else spec
        variants = spec.get("variants", []) if isinstance(spec, dict) else []
        if zh not in seg.source_text or not en:
            continue
        hit = target_hits_term(tgt, en, variants)
        if not hit:
            # 未命中：词边界 + 大小写不敏感，werewolves 不再被误判为漏用 werewolf。
            # 大小写差异/单复数差异不检查（属小问题，不在 QA 职责内）。
            issues.append(
                _issue("forced_term", "high", src, tgt, f"强制术语 {zh} 必须使用标准译名 {en}")
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
    # 只持久化 medium 及以上：low（观感类）不落库，降噪并控制数据库体积
    for i in issues:
        if severity_rank(i.get("severity", "low")) < 1:
            continue
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
