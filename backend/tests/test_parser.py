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


def test_stage_direction_and_cast_list_with_colon_are_narrative() -> None:
    """含冒号的△动作行与人物表不能误判成对白。"""
    text = (
        "△Ethan盯着手里的报告。白纸黑字写着：排除亲子关系。\n\n"
        "人物：Chloe，Leo，Ethan\n\n"
        "Chloe：这是真的吗？\n"
    )
    r = parse_script(text)
    assert [s.type for s in r.segments] == ["narrative", "narrative", "dialogue"]
    assert r.segments[0].speaker is None
    assert r.segments[1].speaker is None
    assert r.segments[2].speaker == "Chloe"


def test_numbered_scene_and_cast_list_never_merge_into_action() -> None:
    """26-4 这类场景标题必须独立成段；人物表也不能与后续动作合并。"""
    text = (
        "第26集\n"
        "26-4 Klein集团顶层实验室 黑夜 内\n"
        "人物：Richard，黑客头目\n"
        "△进度条跳到99%。\n"
        "Richard：二十亿是我的了！\n"
        "26-5 地下机房 黑夜 内\n"
        "△屏幕黑屏。\n"
    )
    r = parse_script(text)
    assert r.report.coverage_ok
    assert r.report.scene_count == 2
    assert [s.text.strip() for s in r.segments if s.type == "heading"] == [
        "第26集", "26-4 Klein集团顶层实验室 黑夜 内", "26-5 地下机房 黑夜 内",
    ]
    assert [s.text.strip() for s in r.segments if s.rule_id == "cast_list"] == [
        "人物：Richard，黑客头目",
    ]
    # 场景标题与人物表都不能再混进动作/叙述段。
    narrative = [s.text for s in r.segments if s.type == "narrative"]
    assert narrative == [
        "人物：Richard，黑客头目\n",
        "△进度条跳到99%。\n",
        "△屏幕黑屏。\n",
    ]


def test_story_outline_is_marked_and_excluded_from_translation_scope() -> None:
    text = (
        "故事大纲\n故事讲述女主带着孩子复仇。\n\n"
        "人物小传\n林晓：冷静果断的母亲。\n\n"
        "第一集\n林晓：你好。\n"
    )
    r = parse_script(text)
    outline = [s for s in r.segments if s.type == "outline"]
    assert len(outline) == 1
    assert "故事大纲" in outline[0].text
    assert "人物小传" in outline[0].text
    assert "林晓：冷静果断的母亲。" in outline[0].text
    assert outline[0].rule_id == "outline"
    assert outline[0].chapter_no == 0
    assert r.report.coverage_ok
    assert "".join(s.text for s in r.segments) == text
    assert not any(s.type == "narrative" and s.text.startswith("人物小传") for s in r.segments)
    assert any(s.type == "dialogue" and s.text.startswith("林晓") for s in r.segments)


def test_front_matter_without_blank_lines_is_not_chapter_one() -> None:
    text = (
        "My Title\n《中文标题》\n"
        "故事大纲\n这是开头大纲。\n"
        "人物小传\n角色甲，简介。\n"
        "第1集\n1-1 咖啡厅 白天 内\n甲：你好。\n"
    )
    r = parse_script(text)
    assert r.report.coverage_ok
    assert r.report.chapter_count == 1
    assert [s.type for s in r.segments[:3]] == ["narrative", "outline", "heading"]
    assert [s.chapter_no for s in r.segments[:3]] == [0, 0, 1]
    assert "故事大纲" in r.segments[1].text
    assert "人物小传" in r.segments[1].text
    assert r.segments[2].text.strip() == "第1集"
    assert "".join(s.text for s in r.segments) == text


def test_outline_marker_only_matches_section_header_not_body_sentence() -> None:
    r = parse_script("林晓：我写了一份大纲。\n她继续解释。\n")
    assert all(s.type != "outline" for s in r.segments)
