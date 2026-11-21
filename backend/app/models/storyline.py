"""章节剧情卡：每章重要情节、伏笔与前情依赖（翻译连续性的关键上下文）。"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ChapterStoryline(Base):
    __tablename__ = "chapter_storylines"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id"), index=True)
    chapter_id: Mapped[str] = mapped_column(String(32), ForeignKey("chapters.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    # JSON：{key_events:[], foreshadowing:[], prior_deps:[], character_states:{}, summary:""}
    card_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(16), default="generated")  # generated / edited
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
