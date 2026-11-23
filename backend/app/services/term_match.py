"""术语匹配工具：Unicode 归一化 + 大小写不敏感 + 词边界 + variants。

历史问题：
- "werewolves" 被误判不含 "werewolf"（子串匹配无词边界）
- "wolf king" 被误判不含 "Wolf King"（大小写敏感导致重译风暴）
- 合法形态（复数、时态等）无法声明（variants）
"""

import re
import unicodedata


def normalize(s: str) -> str:
    """NFKC 归一化 + casefold，用于匹配前的文本规整。"""
    return unicodedata.normalize("NFKC", s).casefold()


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


_PATTERN_CACHE: dict[str, re.Pattern] = {}


def _word_pattern(word: str) -> re.Pattern:
    """英文词边界匹配：前后不能是字母/数字（复数、后缀、连字符拼接都算未命中）。"""
    pat = _PATTERN_CACHE.get(word)
    if pat is None:
        pat = re.compile(
            r"(?<![A-Za-z0-9])" + re.escape(normalize(word)) + r"(?![A-Za-z0-9])"
        )
        _PATTERN_CACHE[word] = pat
    return pat


def target_hits_term(
    target_text: str, standard_en: str, variants: list[str] | None = None
) -> str:
    """译文是否命中术语（标准译名或任一合法变体）。命中返回声明形态，未命中返回空串。

    大小写不敏感 + 词边界；纯中文候选用子串匹配。
    """
    t = normalize(target_text)
    for candidate in [standard_en, *(variants or [])]:
        candidate = (candidate or "").strip()
        if not candidate:
            continue
        if _has_cjk(candidate):
            if normalize(candidate) in t:
                return candidate
        elif _word_pattern(candidate).search(t):
            return candidate
    return ""


def case_drifted(target_text: str, standard_en: str, variants: list[str] | None = None) -> bool:
    """casefold 命中但精确大小写不一致（如 wolf king vs Wolf King）→ True。"""
    hit = target_hits_term(target_text, standard_en, variants)
    if not hit:
        return False
    for candidate in [standard_en, *(variants or [])]:
        candidate = (candidate or "").strip()
        if candidate and candidate in target_text:
            return False
    return True
