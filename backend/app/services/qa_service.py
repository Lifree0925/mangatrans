"""确定性 QA 与语义 QA。"""

import json
from dataclasses import dataclass, field

from app.llm.json_utils import parse_llm_json
from typing import Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.llm.protocol import LLMProvider
from app.services.term_match import select_forced_specs_for_text, target_hits_term
from app.models.document import SourceSegment
from app.models.qa import QAIssue, WindowQuality
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
    segment_id: str = ""
    source_quote: str = ""
    target_quote: str = ""
    explanation: str = ""
    suggested_fix: str = ""
    confidence: float = Field(default=0.5, ge=0, le=1)


class SemanticOutput(BaseModel):
    issues: list[SemanticIssue] = Field(default_factory=list)


@dataclass
class SemanticQAResult:
    """语义 QA 必须显式表达是否真正执行成功；失败不能伪装成 0 问题。"""
    ok: bool
    issues: list[dict] = field(default_factory=list)
    error: str = ""


SEMANTIC_PROMPT = """你是影视剧本翻译的终审 QA。你的职责是抓“会破坏剧情理解或连续性的大问题”，不是做润色。

只检查以下问题：
- omission：原文关键事实、动作、台词、音效/场景信息在译文中完全缺失；
- addition：译文新增了原文没有的剧情事实、人物关系或台词；
- pronoun_reference：he/she 混用，或代词指向了错误角色；
- negation_condition：否定、条件、因果、时态、时间/频率/数量范围被翻反或扩大；
- causality_chronology：事件先后、因果链、人物位置与原文矛盾；
- character_consistency：说话人、身份、立场、亲属关系、社会角色或称呼前后矛盾；
  正式英语称谓应为 Mr./Mrs./Ms./Dr. + 姓氏，不得用称谓 + 名字（除非项目明确如此）；
- terminology：专名/地点/头衔/关键设定物出现第二种实质译名，或把一种东西译成了另一种
  （不算大小写、单复数、标点差异）。

硬性规则：
1. 只能报告有明确原文与译文证据的问题；译文是合理意译且事实一致时，不要报告。
2. 不报告风格、语气、用词偏好、语法小瑕疵、标点、大小写、单复数、是否口语化。
3. 不确定、需要猜测、或 confidence 低于 0.8 的问题，不要输出。
4. 每段最多报告 3 条最高影响的问题；不要换不同说法重复同一问题。
5. PROJECT REFERENCE 是项目事实基准。不得用它强行要求逐字措辞，只用它判断人物、地点、
   亲属关系、称呼、身份、专名和剧情是否矛盾。relationships / forms_of_address / role 是
   判断亲属与称谓的硬事实；不要因为译文更自然就放过关系错误。
6. source_quote / target_quote 必须来自对应文本的原文片段；不能复述。
7. 每条问题必须填写 segment_id：使用 SOURCE/TARGET 中 [方括号] 内的精确 ID。无法定位到某个 ID 的问题不要报告。
8. 时间范围和数量范围也要检查：把“五年没去”译成“从未去过”是范围扩大；
   把“这次/这个人”译成“总是/所有人”也是范围扩大。

severity 判定：
- critical：核心情节或人物关系被译错，读者会误解剧情；
- high：重要情节/动作/台词缺失，或明显指代、因果、专名错误；
- medium：影响理解的局部事实错误；
- low：仅观感问题（不要报告）。

只返回 JSON。

[PROJECT REFERENCE]
{reference}

[SOURCE]
{source}

[TARGET]
{target}"""


def deterministic_qa(db: Session, seg: SourceSegment, tr: Translation, forced_terms: dict[str, dict]) -> list[dict]:
    """确定性 QA：不调用 AI，只检查精确专名/头衔与结构问题。

    forced_terms 由 term_match 产生：只含 exact 专名，普通名词、群体词和
    歧义别名不会进入，因此不会再出现「庄园/大厅/狼人」大面积误报。
    """
    issues: list[dict] = []
    src = seg.source_text.strip()
    tgt = tr.target_text.strip()
    segment_id = getattr(seg, "id", "") or ""
    if not src:
        issues.append(_issue("empty_source", "medium", src, tgt, "原文为空", segment_id))
    if not tgt:
        issues.append(_issue("empty_translation", "critical", src, tgt, "译文为空", segment_id))
    if "```" in tgt:
        issues.append(_issue("markdown_fence", "medium", src, tgt, "译文混入 Markdown 围栏", segment_id))
    active_terms = select_forced_specs_for_text(forced_terms, seg.source_text)
    for zh, spec in active_terms.items():
        if not isinstance(spec, dict):
            continue
        en = spec.get("en", "")
        variants = spec.get("variants", [])
        if zh not in seg.source_text or not en:
            continue
        if not target_hits_term(tgt, en, variants):
            # 大小写差异由落库前规范化处理；这里只检查是否完全没有使用规范专名。
            issues.append(
                _issue(
                    "forced_term", "high", src, tgt,
                    f"专名术语「{zh}」必须使用规范译名「{en}」", segment_id,
                )
            )
    return issues


def _issue(
    issue_type: str, severity: str, src: str, tgt: str, explanation: str,
    segment_id: str = "",
) -> dict:
    return {
        "issue_type": issue_type,
        "severity": severity,
        "segment_id": segment_id,
        "source_quote": src[:200],
        "target_quote": tgt[:200],
        "explanation": explanation,
        "confidence": 1.0,
    }


async def semantic_qa(
    provider: LLMProvider, model: str, source_text: str, target_text: str,
    reference: str = "",
) -> SemanticQAResult:
    """语义 QA：成功返回 issues；任何未完成检查都返回 ok=False。

    容错策略：思考模式请求超时/截断/解析失败时关闭思考再试一次；
    两次都失败时必须向上暴露，调用方不得把它当成“无问题通过”。
    """
    prompt = SEMANTIC_PROMPT.format(
        source=source_text, target=target_text,
        reference=reference or "（无额外项目事实；仅按本段原文判断）",
    )
    last_error = ""
    for attempt in range(2):
        use_thinking = attempt == 0
        try:
            resp = await provider.chat(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                response_schema=SemanticOutput.model_json_schema(),
                temperature=0.2,
                timeout=90,
                thinking="enabled" if use_thinking else "disabled",
                reasoning_effort="low" if use_thinking else None,
                max_tokens=8192,
                stage="qa",
            )
            if resp.finish_reason == "length":
                last_error = "语义质检输出被截断"
                continue
            parsed = SemanticOutput.model_validate(parse_llm_json(resp.content))
            return SemanticQAResult(
                ok=True,
                issues=[i.model_dump() for i in parsed.issues],
            )
        except Exception as exc:  # noqa: BLE001 记录后降级重试
            last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
    return SemanticQAResult(ok=False, issues=[], error=last_error or "语义质检未完成")


def replace_window_quality(
    db: Session, task_id: str, window_id: str, gate: str, details: dict
) -> None:
    """同一任务/窗口只保留最新一次质检结果，避免旧 gate 残留。"""
    db.execute(
        delete(WindowQuality).where(
            WindowQuality.task_id == task_id,
            WindowQuality.window_id == window_id,
        )
    )
    db.add(
        WindowQuality(
            task_id=task_id,
            window_id=window_id,
            gate=gate,
            details_json=json.dumps(details, ensure_ascii=False),
        )
    )


def clear_issues_for_segments(db: Session, project_id: str, segment_ids: list[str]) -> int:
    """清理某窗口所有历史译文版本的旧 QA 告警，避免修复后旧误报常驻。"""
    if not segment_ids:
        return 0
    translation_ids = select(Translation.id).where(Translation.segment_id.in_(segment_ids))
    result = db.execute(
        delete(QAIssue).where(
            QAIssue.project_id == project_id,
            QAIssue.translation_id.in_(translation_ids),
        )
    )
    db.commit()
    return int(result.rowcount or 0)


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


def refresh_forced_term_issues(db: Session, project_id: str) -> int:
    """按当前术语规则重算并删除已失效的旧 forced_term 告警。

    只做本地确定性检查，不调用模型、不修改译文；用于兼容升级前产生的误报。
    """
    from app.models.document import SourceSegment
    from app.services.translation_batch import _forced_terms_for_text

    rows = list(
        db.scalars(
            select(QAIssue).where(
                QAIssue.project_id == project_id,
                QAIssue.issue_type == "forced_term",
            )
        )
    )
    if not rows:
        return 0
    segs = {
        s.id: s for s in db.scalars(
            select(SourceSegment).where(SourceSegment.project_id == project_id)
        )
    }
    polished = list(
        db.scalars(
            select(Translation)
            .where(Translation.stage == "polished")
            .order_by(Translation.version.desc())
        )
    )
    latest: dict[str, Translation] = {}
    for t in polished:
        latest.setdefault(t.segment_id, t)
    by_id = {t.id: t for t in latest.values()}
    current_ids = set(by_id)
    full_text = "".join(s.source_text for s in segs.values())
    forced = _forced_terms_for_text(db, project_id, full_text)
    removed = 0
    for issue in rows:
        if issue.translation_id not in current_ids:
            continue
        tr = by_id.get(issue.translation_id)
        seg = segs.get(tr.segment_id) if tr is not None else None
        if tr is None or seg is None:
            continue
        still = any(
            i.get("issue_type") == "forced_term"
            for i in deterministic_qa(db, seg, tr, forced)
        )
        if not still:
            db.delete(issue)
            removed += 1
    db.commit()
    return removed


def severity_rank(sev: str) -> int:
    return {"low": 0, "medium": 1, "high": 2, "critical": 3}.get(sev, 0)


def evaluate_gate(issues: list[dict]) -> str:
    """quality_gate 三态：blocked / needs_review / passed。"""
    if any(i.get("issue_type") in ("empty_translation", "segment_cover", "empty_source") and i.get("severity") == "critical" for i in issues):
        return "blocked"
    if any(severity_rank(i.get("severity", "low")) >= 2 for i in issues):
        return "needs_review"
    return "passed"
