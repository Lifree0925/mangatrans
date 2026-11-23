"""剧本标题识别：复用剧本已有的英文标题，避免二次翻译。

只检查剧本开头少量短行，识别范围限制在首个对白/章节之前，降低把正文英文
误判为标题的概率。支持：
- 整行英文标题；
- 英文标题 + 中文标题（括号、斜杠、破折号或换行分隔）；
- Markdown 标题前缀。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_CJK = r"\u4e00-\u9fff"
_CJK_RE = re.compile(rf"[{_CJK}]")
_CHAPTER_RE = re.compile(rf"^\s*#{{0,6}}\s*第\s*[0-9{_CJK}百千零两]+\s*[集章回幕]|^\s*Chapter\s+\d+", re.IGNORECASE)
_SCENE_RE = re.compile(r"^\s*(?:INT\.|EXT\.|内景|外景|场景|镜头|第\s*\d+\s*场)", re.IGNORECASE)
_META_RE = re.compile(r"^\s*(?:编剧|作者|原著|类型|集数|出品|导演|主演|人物|角色|简介|摘要)\s*[：:]")
_MARKUP_RE = re.compile(r"^\s*#{1,6}\s*")
_SCENE_HINT_RE = re.compile(r"[·・]|内景|外景|室内|室外|日内|夜内|日外|夜外|清晨|黄昏|深夜|午夜")
_EN_LINE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\s'’&.,:!?()\[\]\-–—]*$")
_EN_RUN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'’&.,!?:\- ]{1,120}")
_CJK_RUN_RE = re.compile(rf"[{_CJK}][{_CJK}0-9：:·…—–\- ]{{1,80}}")


@dataclass(frozen=True)
class SourceTitle:
    """从原文识别出的标题。segment_ids 用于交付版避免正文重复输出。"""

    english_title: str
    chinese_title: str | None
    segment_ids: tuple[str, ...]


def _clean_line(value: str) -> str:
    line = value.strip()
    line = _MARKUP_RE.sub("", line)
    return line.strip(" \t\"'“”‘’《》")


def _lines(text: str | None) -> list[str]:
    return [x for x in (_clean_line(line) for line in (text or "").splitlines()) if x]


def _english_title(text: str | None) -> str | None:
    for line in _lines(text):
        if len(line) > 160 or _META_RE.match(line) or _SCENE_RE.match(line) or _CHAPTER_RE.match(line):
            continue
        if _EN_LINE_RE.fullmatch(line):
            words = re.findall(r"[A-Za-z][A-Za-z0-9'’\-]*", line)
            if words and len("".join(words)) >= 2:
                return line.strip(" -.,:!?")
        for match in _EN_RUN_RE.finditer(line):
            candidate = match.group(0).strip(" -.,:!?()[]")
            words = re.findall(r"[A-Za-z][A-Za-z0-9'’\-]*", candidate)
            # 混排行要求至少两个英文词，避免把中文句中的人名/缩写误当标题。
            if len(words) >= 2:
                return candidate
    return None


def _chinese_title(text: str | None) -> str | None:
    for line in _lines(text):
        if _META_RE.match(line) or _CHAPTER_RE.match(line) or _SCENE_HINT_RE.search(line):
            continue
        for match in _CJK_RUN_RE.finditer(line):
            candidate = match.group(0).strip(" ：:·…—–-")
            if len(_CJK_RE.findall(candidate)) >= 2:
                return candidate
    return None


def detect_source_title(segments: list[Any]) -> SourceTitle | None:
    """扫描剧本开头，返回已有英文标题及其所在片段。"""
    ordered = sorted(segments, key=lambda s: getattr(s, "segment_no", 0))
    title_ids: list[str] = []
    for seg in ordered:
        if getattr(seg, "segment_no", 0) > 8:
            break
        segment_type = getattr(seg, "segment_type", "")
        text = getattr(seg, "source_text", "") or ""
        if segment_type == "dialogue":
            break
        if not text.strip():
            continue
        lines = _lines(text)
        # 章节开始时标题区已结束；继续扫描会把正文里的英文对白名误识别成标题。
        if any(_CHAPTER_RE.match(line) for line in lines):
            break
        # 标题区只接受少量短行，避免把开头的英文正文整段当成标题跳过。
        if len(lines) > 4 or any(len(line) > 160 for line in lines):
            continue
        if any(_SCENE_HINT_RE.search(line) for line in lines):
            continue
        english = _english_title(text)
        if not english:
            continue
        segment_id = getattr(seg, "id", None)
        if segment_id:
            title_ids.append(segment_id)
        return SourceTitle(
            english_title=english,
            chinese_title=_chinese_title(text),
            segment_ids=tuple(title_ids),
        )
    return None


def preserved_title_for_segment(segment: Any, max_segment_no: int = 3) -> str | None:
    """开头标题片段的确定性译文：已有英文标题原样复用。

    仅对最前面的少量非对白短片段生效，避免影响正文。更靠后的前置元数据由
    翻译/润色提示中的标题规则兜底。
    """
    if getattr(segment, "segment_no", 0) > max_segment_no:
        return None
    if getattr(segment, "segment_type", "") == "dialogue":
        return None
    text = getattr(segment, "source_text", "") or ""
    lines = _lines(text)
    if not lines or len(lines) > 4 or any(len(line) > 160 for line in lines):
        return None
    if any(_CHAPTER_RE.match(line) or _SCENE_HINT_RE.search(line) for line in lines):
        return None
    return _english_title(text)
