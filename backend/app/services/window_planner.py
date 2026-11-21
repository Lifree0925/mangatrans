"""Segment Planner：按边界优先级切翻译窗口（不跨章/场，不拆对白轮与句子）。"""

from app.models.document import SourceSegment

TARGET_MIN = 800
TARGET_MAX = 2200


def build_windows(segments: list[SourceSegment]) -> list[list[SourceSegment]]:
    """贪心累积窗口：硬边界（章节/场景变化）强制切断，软上限 2200 字符。"""
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
