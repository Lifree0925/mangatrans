"""确定性 QA 与语义 QA。"""

import json
import re
from dataclasses import dataclass, field

from app.llm.json_utils import parse_llm_json
from typing import Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.llm.protocol import LLMProvider
from app.prompts.script_translation_prompt import thinking_standard_block
from app.services.term_match import (
    normalize,
    select_forced_specs_for_text,
    target_hits_term,
)
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
    "terminology",
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


class NaturalnessEdit(BaseModel):
    """QA 在 source-first 事实核对后给出的整段台词自然化替换。"""

    segment_id: str = ""
    target_quote: str = ""
    suggested_text: str = ""
    reason: str = ""
    # 只允许明确的翻译腔类别；不放宽为“可以更好”。
    reason_category: str = ""
    confidence: float = Field(default=0.0, ge=0, le=1)


class SemanticOutput(BaseModel):
    issues: list[SemanticIssue] = Field(default_factory=list)
    # 独立于事实 QA 的台词自然度修订。不是 QA issue，不参与质量门禁；
    # 只有通过确定性护栏后才允许写回 polished。
    naturalness_edits: list[NaturalnessEdit] = Field(default_factory=list)


@dataclass
class SemanticQAResult:
    """语义 QA 必须显式表达是否真正执行成功；失败不能伪装成 0 问题。"""
    ok: bool
    issues: list[dict] = field(default_factory=list)
    naturalness_edits: list[dict] = field(default_factory=list)
    error: str = ""


SEMANTIC_PROMPT = """你是影视剧本翻译的终审 QA，独立于润色环节。你的职责是抓“会破坏剧情理解或连续性的大问题”，不是做润色。

工作方式（source-first 两遍法）：
1. 先只读 SOURCE，在内部建立可验证单元：谁做了什么、对谁做、时间/地点、否定、条件、因果、数量、身份/关系/称呼、关键专名与物品归属。
2. 再逐项核对 TARGET 是否覆盖或等价表达这些单元；不要从译文反推原文，也不要因为句子通顺就假定事实正确。
3. 机器覆盖率信号只用于提示核查位置，仍须回到 SOURCE/TARGET 原文确认；误报不要输出。

事实核对必须单独确认：
- 施事/受事/使役/受益：谁对谁做了什么，动作方向、主动/被动、谁教谁、谁养谁，不能反转；
  例如「我教了儿子四年」不能变成「养儿子的四年教会了我」；
- 行为功能：台词是在威胁、请求、拒绝、试探、安抚还是认错，不能换成另一种行为；
- 指代与范围：代词指向谁，时间/频率/数量/条件覆盖到哪一段。

只检查以下问题：
- omission：原文关键事实、动作、台词、音效/场景信息在译文中完全缺失；
- addition：译文新增了原文没有的剧情事实、人物关系或台词；
- pronoun_reference：he/she 混用，或代词指向了错误角色；
- negation_condition：否定、条件、因果、时态、时间/频率/数量范围被翻反或扩大；
- causality_chronology：事件先后、因果链、人物位置与原文矛盾；
- character_consistency：说话人、身份、立场、亲属关系、社会角色或称呼前后矛盾；
  正式英语称谓应为 Mr./Mrs./Ms./Dr. + 姓氏，不得用称谓 + 名字（除非项目明确如此）；
  如果项目参考已明确家族关系（如 Helen 是 Damian 的母亲），把“Helen夫人”译为
  “Mrs. Sterling”是合法姓氏称谓，不算新增身份；反过来，项目没有给出姓氏时不得猜姓。
- terminology：专名/地点/头衔/关键设定物出现第二种实质译名，或把一种东西译成了另一种
  （不算大小写、单复数、标点差异）。

硬性规则：
1. 只能报告有明确原文与译文证据的问题；译文是合理意译且事实一致时，不要报告。
2. 不把风格、语气、用词偏好、语法小瑕疵、标点、大小写、单复数、是否口语化作为 issue；
   台词自然度只在 naturalness_edits 中按下一节规则处理。
3. 不确定、需要猜测、或 confidence 低于 0.8 的问题，不要输出。
4. 每段最多报告 3 条最高影响的问题；不要换不同说法重复同一问题。
5. PROJECT REFERENCE 是项目事实基准。不得用它强行要求逐字措辞，只用它判断人物、地点、
   亲属关系、称呼、身份、专名和剧情是否矛盾。relationships / forms_of_address / role 是
   判断亲属与称谓的硬事实；不要因为译文更自然就放过关系错误。
6. source_quote / target_quote 必须来自对应文本的原文片段；不能复述。
7. 每条问题必须填写 segment_id：使用 SOURCE/TARGET 中 [方括号] 内的精确 ID。无法定位到某个 ID 的问题不要报告。
8. 时间范围和数量范围也要检查：把“五年没去”译成“从未去过”是范围扩大；
   把“这次/这个人”译成“总是/所有人”也是范围扩大。
9. 如果译文改变了施事/受事、动作方向或台词的行为功能，即使句子通顺也必须报告。

[NATURALNESS TABLE READ — 只处理台词，不写入 issues]
完成事实核对后，再像美国演员做 table read 一样逐条读 TARGET 中的台词。
只有当当前台词存在明确的翻译腔、非母语搭配或中文句式残留时，才给出一条 naturalness_edit：
- 中文话题—评论语序、堆叠定语、名词短语碎片、为解释共享信息而加的说明从句；
- 直译中文比喻/俗语，英语观众不会在这个场景里这样说；
- 语法上成立，但母语演员在这个人物、关系和情绪下不会这样开口。

不要因为“可以更好”、个人口味、缺少俚语、语法小瑕疵、标点或语气强弱而修改。
每条编辑必须：
- 保留全部事实、说话人/受话人、行为功能、情绪力度、人物关系、数字、否定、条件、术语和行结构；
- 输出该 segment 的完整替换文本，不是片段，不是解释；
- target_quote 必须逐字等于当前 TARGET 中该段的完整文本；segment_id 必须是方括号内的精确 ID；
- reason_category 必须是 translationese、unnatural_collocation、wrong_rhythm、literal_idiom 之一；
- confidence >= 0.95；每窗最多 3 条；没有明确问题就返回空数组。
naturalness_edits 不是 QA issue，不要放进 issues；不得修改叙述段（△、场景标题、人物表、元数据）。
如果当前台词已经自然忠实，只是“可以更好”，不要输出编辑；原样保留是默认正确答案。

severity 判定：
- critical：核心情节或人物关系被译错，读者会误解剧情；
- high：重要情节/动作/台词缺失，或明显指代、因果、专名错误；
- medium：影响理解的局部事实错误；
- low：仅观感问题（不要报告）。

只返回 JSON，包含 issues 与 naturalness_edits 两个字段。

[PROJECT REFERENCE]
{reference}

[SOURCE]
{source}

[TARGET]
{target}

[DETERMINISTIC COVERAGE SIGNALS]
{coverage}"""


_ZH_NEGATION_RE = re.compile(r"并非|不是|没有|从未|绝不|不能|不会|不要")
_EN_NEGATION_RE = re.compile(
    r"\b(?:not|no|never|none|nobody|nothing|neither|nor|without|unable|impossible|"
    r"refuse[ds]?|denied|lack(?:s|ed)?|stop|cannot|can't|don't|doesn't|didn't|"
    r"isn't|aren't|wasn't|weren't|won't|wouldn't|shouldn't|couldn't|mustn't|"
    r"haven't|hasn't|hadn't)\b",
    re.IGNORECASE,
)
_ZH_CONDITION_RE = re.compile(r"如果|假如|要是|只要|除非|否则|不然|一旦")
_EN_CONDITION_RE = re.compile(
    r"\b(?:if|unless|otherwise|as long as|only if|once|whether|or else)\b",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")


def build_coverage_signals(segments: list[dict], targets: dict[str, str]) -> tuple[str, dict]:
    """机器覆盖率信号：只报告需要 QA 复核的位置，不直接判错。"""
    rows: list[str] = []
    stats = {"checked": 0, "negation": 0, "condition": 0, "number": 0}
    for seg in segments:
        sid = str(seg.get("segment_id") or "")
        src = str(seg.get("source_text") or "")
        tgt = str(targets.get(sid) or "")
        if not sid or not src or not tgt:
            continue
        stats["checked"] += 1
        notes: list[str] = []
        if _ZH_NEGATION_RE.search(src) and not _EN_NEGATION_RE.search(tgt):
            stats["negation"] += 1
            notes.append("原文含否定/反义限定，译文中未检出否定表达")
        if _ZH_CONDITION_RE.search(src) and not _EN_CONDITION_RE.search(tgt):
            stats["condition"] += 1
            notes.append("原文含条件/转折连接，译文中未检出对应连接")
        src_nums = {x.replace(",", "") for x in _NUMBER_RE.findall(src)}
        tgt_nums = {x.replace(",", "") for x in _NUMBER_RE.findall(tgt)}
        missing_nums = sorted(src_nums - tgt_nums)
        if missing_nums:
            stats["number"] += 1
            notes.append(f"原文数字未在译文中以数字形式出现：{', '.join(missing_nums[:6])}")
        if notes:
            rows.append(f"- [{sid}] " + "；".join(notes))
    hint = "\n".join(rows[:12]) if rows else "无；仍需按 SOURCE 逐项核对。"
    return hint, stats


_EN_MODAL_RE = re.compile(
    r"\b(?:must|have to|has to|had to|need to|needs to|should|shouldn't|can|could|may|"
    r"might|will|would|shall|can't|cannot|won't|wouldn't)\b",
    re.IGNORECASE,
)
_EN_QUANTIFIER_RE = re.compile(
    r"\b(?:all|every|each|any|some|both|none|nobody|nothing|everything|everyone|"
    r"someone|anyone)\b",
    re.IGNORECASE,
)
_ENTITY_STOPWORDS = {
    "i", "i'm", "i've", "i'll", "i'd", "you", "your", "you're", "he", "she", "it",
    "it's", "we", "they", "what", "who", "where", "when", "why", "how", "this", "that",
    "these", "those", "there", "then", "now", "no", "yes", "well", "okay", "fine",
    "please", "hey", "oh", "so", "and", "but", "if", "because", "the", "a", "an",
    "my", "our", "his", "her", "their", "mr", "mrs", "ms", "miss", "dr", "don't",
    "did", "do", "does", "can", "could", "would", "will", "shall", "should",
}


def _entity_tokens(text: str) -> set[str]:
    """提取需要保护的角色标签/人名，避免把句首问候语当成实体。"""
    raw = text or ""
    out: set[str] = set()

    def add(segment: str, *, skip_sentence_start: bool) -> None:
        for m in re.finditer(r"\b[A-Z][A-Za-z'’-]*\b", segment):
            prefix = segment[:m.start()].rstrip()
            if skip_sentence_start and (not prefix or re.search(r"[.!?]\s*$", prefix)):
                continue
            norm = re.sub(r"['’]s$", "", m.group(0)).casefold()
            if norm and norm not in _ENTITY_STOPWORDS:
                out.add(norm)

    role = re.match(r"^\s*([^:：\n]{1,80})[:：]", raw)
    if role:
        add(role.group(1), skip_sentence_start=False)
        raw = raw[role.end():]
    add(raw, skip_sentence_start=True)
    return out


_EN_NUMBER_WORD_RE = re.compile(
    r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|"
    r"forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion|"
    r"dozen|once|twice)\b",
    re.IGNORECASE,
)


def _quote_norm(text: str) -> str:
    return re.sub(r"\s+", " ", normalize(text)).strip()


def _number_tokens(text: str) -> set[str]:
    tokens = {x.replace(",", "") for x in _NUMBER_RE.findall(text or "")}
    tokens |= {x.casefold() for x in _EN_NUMBER_WORD_RE.findall(text or "")}
    return tokens


def naturalness_edit_guard(
    *, source_text: str, old_text: str, new_text: str,
    forced_terms: dict[str, dict],
) -> tuple[bool, str]:
    """自然化编辑的确定性护栏：宁可少改，也不让台词编辑改坏事实。

    QA 的自然度编辑必须保持否定、条件、数字和强制术语；结构、台词类型和
    目标引用由 select_naturalness_edits 再检查。
    """
    old = (old_text or "").strip()
    new = (new_text or "").strip()
    if not old or not new:
        return False, "empty"
    if len(new) < max(8, int(len(old) * 0.35)) or len(new) > max(80, int(len(old) * 2.5)):
        return False, "length"
    if bool(_EN_NEGATION_RE.search(old)) != bool(_EN_NEGATION_RE.search(new)):
        return False, "negation"
    if bool(_EN_CONDITION_RE.search(old)) != bool(_EN_CONDITION_RE.search(new)):
        return False, "condition"
    if _number_tokens(old) != _number_tokens(new):
        return False, "number"
    if {m.casefold() for m in _EN_MODAL_RE.findall(old)} != {
        m.casefold() for m in _EN_MODAL_RE.findall(new)
    }:
        return False, "modal"
    if {m.casefold() for m in _EN_QUANTIFIER_RE.findall(old)} != {
        m.casefold() for m in _EN_QUANTIFIER_RE.findall(new)
    }:
        return False, "quantifier"
    for zh, spec in select_forced_specs_for_text(forced_terms, source_text).items():
        en = str((spec or {}).get("en") or "")
        if not en:
            continue
        variants = (spec or {}).get("variants", []) or []
        if not target_hits_term(new, en, variants):
            return False, f"forced_term:{zh}"
    old_entities = _entity_tokens(old)
    new_entities = _entity_tokens(new)
    source_entities = _entity_tokens(source_text)
    if not old_entities.issubset(new_entities):
        return False, "entity_lost"
    if new_entities - old_entities - source_entities:
        return False, "entity_added"
    return True, ""


def select_naturalness_edits(
    *,
    segments: list[dict],
    targets: dict[str, str],
    edits: list[dict],
    forced_terms: dict[str, dict],
    speaker_map: dict[str, str] | None = None,
    exclude_sids: set[str] | None = None,
    max_edits: int = 3,
) -> dict[str, str]:
    """筛选 QA 自然度编辑；只返回可安全写回的 segment -> 完整新文本。"""
    # 延迟导入，避免 qa_service 与 translation 在模块加载期形成环。
    from app.services.translation import validate_window

    seg_by_id = {str(s.get("segment_id") or ""): s for s in segments}
    target_by_id = {str(k): str(v or "") for k, v in (targets or {}).items()}
    excluded = exclude_sids or set()
    ranked = sorted(
        edits or [],
        key=lambda e: float(e.get("confidence") or 0),
        reverse=True,
    )
    accepted: dict[str, str] = {}
    for edit in ranked:
        if len(accepted) >= max_edits:
            break
        sid = str(edit.get("segment_id") or "")
        seg = seg_by_id.get(sid)
        if not sid or seg is None or sid in excluded or sid in accepted:
            continue
        if str(seg.get("segment_type") or "") != "dialogue":
            continue
        old = target_by_id.get(sid, "")
        quote = _quote_norm(str(edit.get("target_quote") or ""))
        new = str(edit.get("suggested_text") or "").strip()
        if not quote or not new or quote != _quote_norm(old) or new == old.strip():
            continue
        if str(edit.get("reason_category") or "") not in {
            "translationese", "unnatural_collocation", "wrong_rhythm", "literal_idiom",
        }:
            continue
        if float(edit.get("confidence") or 0) < 0.95:
            continue
        ok, _reason = naturalness_edit_guard(
            source_text=str(seg.get("source_text") or ""),
            old_text=old,
            new_text=new,
            forced_terms=forced_terms,
        )
        if not ok:
            continue
        seg_dict = {
            "segment_id": sid,
            "segment_type": seg.get("segment_type", ""),
            "speaker_name": seg.get("speaker_name"),
            "source_text": seg.get("source_text", ""),
            "line_count_optional": bool(seg.get("line_count_optional", False)),
        }
        errors = validate_window(
            [seg_dict],
            [{"segment_id": sid, "target_text": new}],
            speaker_map=speaker_map,
        )
        if errors:
            continue
        accepted[sid] = new
    return accepted


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
    reference: str = "", coverage: str = "",
) -> SemanticQAResult:
    """语义 QA：成功返回 issues；任何未完成检查都返回 ok=False。

    容错策略：思考模式请求超时/截断/解析失败时关闭思考再试一次；
    两次都失败时必须向上暴露，调用方不得把它当成“无问题通过”。
    """
    prompt = SEMANTIC_PROMPT.format(
        source=source_text, target=target_text,
        reference=reference or "（无额外项目事实；仅按本段原文判断）",
        coverage=coverage or "无",
    )
    last_error = ""
    for attempt in range(2):
        use_thinking = attempt == 0
        attempt_prompt = prompt
        if use_thinking:
            attempt_prompt = prompt.replace(
                "[PROJECT REFERENCE]",
                thinking_standard_block() + "\n\n[PROJECT REFERENCE]",
                1,
            )
        try:
            resp = await provider.chat(
                model=model,
                messages=[{"role": "user", "content": attempt_prompt}],
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
                naturalness_edits=[i.model_dump() for i in parsed.naturalness_edits],
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
