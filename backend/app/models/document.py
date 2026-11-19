"""原文版本、章节、场景、片段模型。"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SourceDocument(Base):
    __tablename__ = "source_documents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(500))
    format: Mapped[str] = mapped_column(String(16))  # txt / md / docx
    raw_path: Mapped[str] = mapped_column(String(1000))
    normalized_text_path: Mapped[str] = mapped_column(String(1000))
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="active")
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Chapter(Base):
    __tablename__ = "chapters"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id"), index=True)
    source_document_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("source_documents.id"), index=True
    )
    chapter_no: Mapped[int] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_start: Mapped[int] = mapped_column(Integer)
    source_end: Mapped[int] = mapped_column(Integer)


class Scene(Base):
    __tablename__ = "scenes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id"), index=True)
    source_document_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("source_documents.id"), index=True
    )
    chapter_id: Mapped[str] = mapped_column(String(32), ForeignKey("chapters.id"), index=True)
    scene_no: Mapped[int] = mapped_column(Integer)
    heading: Mapped[str | None] = mapped_column(String(500), nullable=True)
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    time_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_start: Mapped[int] = mapped_column(Integer)
    source_end: Mapped[int] = mapped_column(Integer)


class SourceSegment(Base):
    __tablename__ = "source_segments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id"), index=True)
    source_document_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("source_documents.id"), index=True
    )
    chapter_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    scene_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    segment_no: Mapped[int] = mapped_column(Integer)
    segment_type: Mapped[str] = mapped_column(String(16))  # heading/dialogue/narrative/other
    speaker_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_text: Mapped[str] = mapped_column(Text)
    source_start: Mapped[int] = mapped_column(Integer)
    source_end: Mapped[int] = mapped_column(Integer)
    source_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
