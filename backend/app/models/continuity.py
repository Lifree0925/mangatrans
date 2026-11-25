"""章节级人物状态与关键事实（跨章节连续性和剧透边界）。"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CharacterState(Base):
    """人物在某章结束时的可见状态；旧版本保留，读取时取最新版本。"""

    __tablename__ = "character_states"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "character_key", "chapter_id", "version",
            name="uq_character_state_version",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id"), index=True)
    chapter_id: Mapped[str] = mapped_column(String(32), ForeignKey("chapters.id"), index=True)
    source_storyline_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    character_key: Mapped[str] = mapped_column(String(200), index=True)
    chapter_no: Mapped[int] = mapped_column(Integer, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    state_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(16), default="current")  # current/superseded
    source: Mapped[str] = mapped_column(String(16), default="storyline")
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class StoryFact(Base):
    """章节级可验证事实，只允许在当前/后续章节的 QA 与上下文中出现。"""

    __tablename__ = "story_facts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id"), index=True)
    chapter_id: Mapped[str] = mapped_column(String(32), ForeignKey("chapters.id"), index=True)
    source_storyline_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    chapter_no: Mapped[int] = mapped_column(Integer, index=True)
    fact_type: Mapped[str] = mapped_column(String(32), index=True)
    subject: Mapped[str] = mapped_column(String(200), default="")
    predicate: Mapped[str] = mapped_column(String(200), default="")
    object_text: Mapped[str] = mapped_column(String(500), default="")
    fact_text: Mapped[str] = mapped_column(Text)
    valid_from_chapter_no: Mapped[int] = mapped_column(Integer, index=True)
    valid_to_chapter_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reveal_chapter_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="current")
    source: Mapped[str] = mapped_column(String(16), default="storyline")
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
