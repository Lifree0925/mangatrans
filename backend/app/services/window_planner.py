"""Segment Planner：按边界优先级切翻译窗口（不跨章/场，不拆对白轮与句子）。

窗口从历史 1000 字提升到 1800 字：减少窗口数与重复上下文注入。
输出截断仍由翻译层自动拆半兜底，不能为了速度牺牲完整性。
"""

import os

from app.models.document import SourceSegment

TARGET_MIN = max(400, int(os.environ.get("MT_WINDOW_MIN_CHARS", "1200")))
TARGET_MAX = max(TARGET_MIN, int(os.environ.get("MT_WINDOW_MAX_CHARS", "1800")))


def build_windows(segments: list[SourceSegment]) -> list[list[SourceSegment]]:
    """贪心累积窗口：章节/场景变化强制切断，窗口按源文字符软上限切分。"""
    windows: list[list[SourceSegment]] = []
    current: list[SourceSegment] = []
    size = 0
    last_chapter = None
    last_scene = None

    for seg in segments:
        hard_cut = (
            (last_chapter is not None and seg.chapter_id != last_chapter)
            or (last_scene is not None and seg.scene_id != last_scene)
        )
        if hard_cut and current:
            windows.append(current)
            current = []
            size = 0
        if current and size + len(seg.source_text) > TARGET_MAX:
            windows.append(current)
            current = []
            size = 0
        current.append(seg)
        size += len(seg.source_text)
        last_chapter = seg.chapter_id
        last_scene = seg.scene_id

    if current:
        windows.append(current)
    return windows
