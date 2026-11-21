"""解析器单元测试：分类规则 + 覆盖校验 + 性能。"""

import time

from app.parsers.script_parser import parse_script

SAMPLE = """第一集 初遇
【咖啡厅·日内】
林晓：（走进来）你好。
王强：你是谁？

（两人对视）
林晓缓缓开口，说道：
第二章 重逢
Chapter 3 决裂
内景 会议室
"""


def test_basic_classification() -> None:
    r = parse_script(SAMPLE)
    types = [s.type for s in r.segments]
    assert types.count("heading") >= 5
    assert types.count("dialogue") == 2
    assert types.count("narrative") >= 1
    assert types.count("other") == 1


def test_speaker_extraction() -> None:
    r = parse_script(SAMPLE)
    speakers = {s.speaker for s in r.segments if s.type == "dialogue"}
    assert speakers == {"林晓", "王强"}


def test_coverage_and_roundtrip() -> None:
    r = parse_script(SAMPLE)
    assert r.report.coverage_ok
    assert r.report.uncovered_chars == 0
    # 片段拼接可完整还原原文
    assert "".join(s.text for s in r.segments) == SAMPLE


def test_no_chapter_heading_defaults_to_chapter_one() -> None:
    r = parse_script("林晓：你好。\n")
    assert r.report.coverage_ok
    assert r.report.chapter_count == 1
    assert r.report.scene_count == 0


def test_offsets_are_codepoint_based() -> None:
    # 含 emoji（代理对）与中文：偏移必须是码点而非 UTF-16 单元
    text = "林晓：你好👋。\n王强：再见。\n"
    r = parse_script(text)
    assert r.report.coverage_ok
    assert "".join(s.text for s in r.segments) == text


def test_performance_80k_chars_under_2s() -> None:
    block = """第一集 测试
【地点·日内】
林晓：这是一段测试对白，内容长度适中。
王强：回答内容同样长度适中。

（动作描述与叙述文字，保证解析器有足够内容处理。）
"""
    text = (block * 1400)[:80_000]  # 约 8 万字符
    t0 = time.perf_counter()
    r = parse_script(text)
    elapsed = time.perf_counter() - t0
    assert r.report.coverage_ok
    assert elapsed < 2.0, f"解析耗时 {elapsed:.2f}s 超过 2 秒限制"


def test_chapter_title_with_md_prefix() -> None:
    """# 第N回 / # 第N章 等 Markdown 风格章节标题应识别为 heading。"""
    r = parse_script("# 第1回 宴桃园豪杰三结义　斩黄巾英雄首立功\n话说天下大势。\n# 第2章 重逢\n林晓：你好。")
    assert r.report.chapter_count == 2
    assert sum(1 for s in r.segments if s.type == "heading") >= 2


def test_stray_punctuation_merged_into_previous_segment() -> None:
    """标题后的孤立句号行并入标题，不产生独立片段。"""
    r = parse_script("# 第1回 桃园结义\n。\n话说天下大势。")
    types = [s.type for s in r.segments]
    assert "punct_only" not in types
    first = r.segments[0]
    assert first.type == "heading"
    assert first.text == "# 第1回 桃园结义\n。\n"
    # 覆盖校验：拼接可还原原文
    restored = "".join(s.text for s in r.segments)
    assert restored == "# 第1回 桃园结义\n。\n话说天下大势。"


def test_junk_line_merged_into_previous_segment() -> None:
    """维基语言链接等杂质行并入上一片段，不独立成段。"""
    r = parse_script("# 第1回 桃园结义\nvi:Tam quốc diễn nghĩa/Hồi 1\n话说天下大势。")
    types = [s.type for s in r.segments]
    assert "junk" not in types
    assert r.segments[0].text == "# 第1回 桃园结义\nvi:Tam quốc diễn nghĩa/Hồi 1\n"
