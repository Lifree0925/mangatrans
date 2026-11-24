"""确定性剧本解析器：机械切分，不做语义分类。

设计约束（文档 12.3）：
- 只做形式分类：heading / dialogue / narrative / other。
- 不判断旁白与动作的区别（统一 narrative），不推断无标签台词归属。
- 每个识别结果保留 parser rule ID 与原文偏移（Unicode 码点）。
- 复杂度 O(n)，8 万字 2 秒内完成。
"""

import re
from dataclasses import dataclass, field

_CN_NUM = "一二三四五六七八九十百千零两"

# 规则 ID 常量
RULE_MD_HEADING = "md_heading"
RULE_CHAPTER = "chapter_title"
RULE_SCENE_BRACKET = "scene_bracket"
RULE_SCENE_LOC = "scene_location"
RULE_SCENE_SEP = "scene_separator"
RULE_DIALOGUE = "dialogue"
RULE_PAREN = "parenthetical"
RULE_NARRATIVE = "narrative"
RULE_OTHER = "other"

_CHAPTER_RE = re.compile(
    rf"^#{{0,6}}\s*第\s*[\d{_CN_NUM}]+\s*[集章回幕].*$|^Chapter\s+\d+.*$", re.IGNORECASE
)
_SCENE_BRACKET_RE = re.compile(r"^[【〔\[](.{1,80})[】〕\]]$")
# 场景标题特征：地名·时间（如 咖啡厅·日内）或场景关键词；【闪回】【音效】等不满足则归叙述
_SCENE_HINT_RE = re.compile(r"[·・]|内景|外景|室内|室外|日内|夜内|日外|夜外|清晨|黄昏|深夜|午夜|白天|夜晚|场景|地点|摄影棚|外场")
_SCENE_LOC_RE = re.compile(
    r"^(内景|外景|日内|夜内|日外|夜外|场景|镜头|第\d+场|第[\u4e00-\u9fa5]+场)[：:\s]"
)
_SCENE_SEP_RE = re.compile(r"^[-—–]{3,}\s*$")
_DIALOGUE_RE = re.compile(
    r"^([^\s：:（）()【】\[.\]\"'’“”]{1,24})\s*(?:（[^）]{1,40}）)?[：:](\s*.*)$"
)
_PAREN_RE = re.compile(r"^（[^）]{1,200}）\s*$")
# 人物表不是对白：冒号后的角色名单必须保留原样，不能按「角色：台词」解析。
_CAST_LIST_RE = re.compile(r"^(?:人物|角色|出场人物|登场人物|主要人物)\s*[：:]")


@dataclass
class SegmentBlock:
    """解析出的片段。"""

    type: str
    text: str
    start: int
    end: int
    rule_id: str
    speaker: str | None = None
    chapter_no: int = 0
    scene_no: int = 0
    confidence: str = "high"


@dataclass
class ChapterBlock:
    no: int
    title: str
    start: int
    end: int
    rule_id: str


@dataclass
class SceneBlock:
    chapter_no: int
    no: int
    heading: str | None
    start: int
    end: int
    rule_id: str


@dataclass
class ParseReport:
    total_chars: int = 0
    covered_chars: int = 0
    uncovered_chars: int = 0
    chapter_count: int = 0
    scene_count: int = 0
    dialogue_count: int = 0
    narrative_count: int = 0
    heading_count: int = 0
    other_count: int = 0
    anomalies: list[str] = field(default_factory=list)
    parser_version: str = "1.0.0"
    coverage_ok: bool = False


@dataclass
class ParseResult:
    chapters: list[ChapterBlock]
    scenes: list[SceneBlock]
    segments: list[SegmentBlock]
    report: ParseReport


_PUNCT_ONLY_RE = re.compile(r"^[。．.！!？?…]{1,4}$")
# 杂质行：维基语言链接 / 网址 / 纯页码等（并入上一片段，不独立翻译）
_JUNK_RE = re.compile(
    r"^vi:[\s\S]*$|^[a-z]{2,3}:[^\s:]{2,}.*$|^https?://|^\d{1,5}$"
)


def _classify_line(line: str) -> tuple[str, str, str | None]:
    """返回 (类型, 规则ID, speaker)。"""
    stripped = line.strip()
    if not stripped:
        return "blank", "", None
    if _PUNCT_ONLY_RE.match(stripped):
        # 孤立标点行（常见于标题/段落排版）：并入上一片段，不独立成段
        return "punct_only", "punct_only", None
    if _JUNK_RE.match(stripped):
        # 杂质行（链接/语言标记等）：并入上一片段
        return "junk", "junk", None
    # △ 开头是舞台动作/叙述，即使行内出现冒号也不能当对白。
    # 否则会把「△Ethan盯着报告：排除亲子关系」误判成角色名为整句动作的对白。
    if stripped.startswith("△"):
        return "narrative", RULE_NARRATIVE, None
    if _CAST_LIST_RE.match(stripped):
        return "narrative", RULE_NARRATIVE, None
    if _CHAPTER_RE.match(stripped):
        return "heading", RULE_CHAPTER, None
    m_bracket = _SCENE_BRACKET_RE.match(stripped)
    if m_bracket:
        # 舞台指示（音效/特效/闪回/字幕等）归叙述；仅具场景特征的方括号行才作场景标题
        inner = m_bracket.group(1)
        if "：" in stripped or ":" in stripped or not _SCENE_HINT_RE.search(inner):
            return "narrative", RULE_PAREN, None
        return "heading", RULE_SCENE_BRACKET, None
    if _SCENE_LOC_RE.match(stripped) and len(stripped) <= 40:
        return "heading", RULE_SCENE_LOC, None
    if _SCENE_SEP_RE.match(stripped):
        return "heading", RULE_SCENE_SEP, None
    m = _DIALOGUE_RE.match(stripped)
    if m:
        tail = m.group(2)
        if tail.strip():
            return "dialogue", RULE_DIALOGUE, m.group(1)
        # 冒号后无内容：可疑对白行
        return "other", RULE_OTHER, m.group(1)
    if _PAREN_RE.match(stripped):
        return "narrative", RULE_PAREN, None
    return "narrative", RULE_NARRATIVE, None


def parse_script(text: str) -> ParseResult:
    """全文解析：行级分类 + 覆盖校验。所有 segment 的 source_text 保留原始行（含换行），
    保证片段拼接可完整还原原文。"""
    lines = text.splitlines(keepends=True)
    if not lines:
        return ParseResult(chapters=[], scenes=[], segments=[], report=ParseReport())

    chapters: list[ChapterBlock] = []
    scenes: list[SceneBlock] = []
    segments: list[SegmentBlock] = []
    anomalies: list[str] = []

    chapter_no = 0
    scene_no = 0
    pos = 0

    # narrative 合并缓冲
    buf = ""
    buf_start = 0

    def append_blank(pos_start: int, pos_end: int) -> None:
        """空白行 = 自然段边界：先落当前叙述缓冲，空行并入上一段尾部（保持原文还原）。

        这样每个自然段独立成片段，避免多个段落合并成一大段翻译、中英对照观感差。
        """
        nonlocal buf, buf_start
        flush_narrative(pos_start)
        if segments:
            last = segments[-1]
            last.end = pos_end
            last.text += text[pos_start:pos_end]
        else:
            buf_start = pos_start
            buf = text[pos_start:pos_end]

    def flush_narrative(end: int) -> None:
        nonlocal buf
        if buf:
            segments.append(
                SegmentBlock(
                    type="narrative",
                    text=buf,
                    start=buf_start,
                    end=end,
                    rule_id=RULE_NARRATIVE,
                    chapter_no=chapter_no,
                    scene_no=scene_no,
                )
            )
            buf = ""

    def ensure_chapter() -> None:
        nonlocal chapter_no
        if chapter_no == 0:
            chapter_no = 1
            chapters.append(ChapterBlock(no=1, title="", start=0, end=0, rule_id=RULE_OTHER))

    for raw_line in lines:
        line = raw_line.rstrip("\n")
        content = line.strip()
        ltype, rule, speaker = _classify_line(line)

        if ltype == "blank":
            append_blank(pos, pos + len(raw_line))
            pos += len(raw_line)
            continue

        if ltype in ("punct_only", "junk"):
            # 并入上一片段（保持偏移与原文还原），不独立成段
            if segments:
                last = segments[-1]
                last.end = pos + len(raw_line)
                last.text += raw_line
            else:
                buf += raw_line
            pos += len(raw_line)
            continue

        if ltype == "heading" and rule == RULE_CHAPTER:
            flush_narrative(pos)
            # 首个真实章节标题：若此前只有自动占位章（无标题且仅叙述），并入本章，避免目录多出“第一章”
            if (
                len(chapters) == 1
                and chapters[0].rule_id == RULE_OTHER
                and not chapters[0].title.strip()
            ):
                chapters.pop()
                for s in segments:
                    s.chapter_no = 1
                chapter_no = 0
            chapter_no += 1
            scene_no = 0
            chapters.append(
                ChapterBlock(no=chapter_no, title=content, start=pos, end=pos + len(raw_line), rule_id=rule)
            )
            segments.append(
                SegmentBlock(
                    type="heading", text=raw_line, start=pos, end=pos + len(raw_line),
                    rule_id=rule, chapter_no=chapter_no, scene_no=scene_no,
                )
            )
            pos += len(raw_line)
            continue

        if ltype == "heading" and rule in (RULE_SCENE_BRACKET, RULE_SCENE_LOC):
            flush_narrative(pos)
            ensure_chapter()
            scene_no += 1
            scenes.append(
                SceneBlock(
                    chapter_no=chapter_no, no=scene_no, heading=content,
                    start=pos, end=pos + len(raw_line), rule_id=rule,
                )
            )
            segments.append(
                SegmentBlock(
                    type="heading", text=raw_line, start=pos, end=pos + len(raw_line),
                    rule_id=rule, chapter_no=chapter_no, scene_no=scene_no,
                )
            )
            pos += len(raw_line)
            continue

        if ltype == "heading" and rule == RULE_SCENE_SEP:
            flush_narrative(pos)
            ensure_chapter()
            scene_no += 1
            scenes.append(
                SceneBlock(
                    chapter_no=chapter_no, no=scene_no, heading=None,
                    start=pos, end=pos + len(raw_line), rule_id=rule,
                )
            )
            pos += len(raw_line)
            continue

        if ltype == "dialogue":
            flush_narrative(pos)
            ensure_chapter()
            segments.append(
                SegmentBlock(
                    type="dialogue", text=raw_line, start=pos, end=pos + len(raw_line),
                    rule_id=rule, speaker=speaker, chapter_no=chapter_no, scene_no=scene_no,
                )
            )
            pos += len(raw_line)
            continue

        if ltype == "other":
            flush_narrative(pos)
            ensure_chapter()
            segments.append(
                SegmentBlock(
                    type="other", text=raw_line, start=pos, end=pos + len(raw_line),
                    rule_id=rule, chapter_no=chapter_no, scene_no=scene_no, confidence="low",
                )
            )
            anomalies.append(f"偏移 {pos}：无法套用格式的行（已保留原文）：{content[:30]}")
            pos += len(raw_line)
            continue

        # narrative
        ensure_chapter()
        if not buf:
            buf_start = pos
        buf += raw_line
        pos += len(raw_line)

    flush_narrative(pos)

    # 章节 end 补齐：下一章起点或文末
    for i, ch in enumerate(chapters):
        ch.end = chapters[i + 1].start if i + 1 < len(chapters) else len(text)
    # 场景 end 补齐：下一场景起点（同章）与本章结束取最小
    for i, sc in enumerate(scenes):
        ch_end = next((c.end for c in chapters if c.no == sc.chapter_no), len(text))
        nxt = scenes[i + 1].start if i + 1 < len(scenes) else len(text)
        sc.end = min(ch_end, nxt)

    # 覆盖校验：段必须首尾相接且覆盖全文
    covered = 0
    prev_end = 0
    ok = True
    for seg in segments:
        if seg.start != prev_end:
            ok = False
            anomalies.append(f"覆盖缺口：期望 {prev_end}，实际 {seg.start}")
        prev_end = seg.end
        covered += seg.end - seg.start
    if prev_end != len(text):
        ok = False
        anomalies.append(f"覆盖缺口：末尾期望 {len(text)}，实际 {prev_end}")

    report = ParseReport(
        total_chars=len(text),
        covered_chars=covered,
        uncovered_chars=max(0, len(text) - covered),
        chapter_count=len(chapters),
        scene_count=len(scenes),
        dialogue_count=sum(1 for s in segments if s.type == "dialogue"),
        narrative_count=sum(1 for s in segments if s.type == "narrative"),
        heading_count=sum(1 for s in segments if s.type == "heading"),
        other_count=sum(1 for s in segments if s.type == "other"),
        anomalies=anomalies,
        coverage_ok=ok,
    )
    return ParseResult(chapters=chapters, scenes=scenes, segments=segments, report=report)
