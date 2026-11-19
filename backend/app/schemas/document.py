"""原文与解析结果 Schema。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    version: int
    filename: str
    format: str
    content_hash: str
    status: str
    imported_at: datetime


class ChapterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    chapter_no: int
    title: str | None
    source_start: int
    source_end: int


class SceneOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    chapter_id: str
    scene_no: int
    heading: str | None
    source_start: int
    source_end: int


class SegmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    chapter_id: str | None
    scene_id: str | None
    segment_no: int
    segment_type: str
    speaker_name: str | None
    source_text: str
    source_start: int
    source_end: int


class ParseReportOut(BaseModel):
    total_chars: int
    covered_chars: int
    uncovered_chars: int
    chapter_count: int
    scene_count: int
    dialogue_count: int
    narrative_count: int
    heading_count: int
    other_count: int
    anomalies: list[str]
    parser_version: str
    coverage_ok: bool
