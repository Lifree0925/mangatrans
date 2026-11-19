"""QA 与窗口质量门禁模型。"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class QAIssue(Base):
    __tablename__ = "qa_issues"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id"), index=True)
    translation_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("translations.id"), nullable=True
    )
    issue_type: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16), default="low")
    source_quote: Mapped[str] = mapped_column(Text, default="")
    target_quote: Mapped[str] = mapped_column(Text, default="")
    explanation: Mapped[str] = mapped_column(Text, default="")
    suggested_fix: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Integer, default=50)
    status: Mapped[str] = mapped_column(String(16), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WindowQuality(Base):
    __tablename__ = "window_quality"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(String(32), ForeignKey("tasks.id"), index=True)
    window_id: Mapped[str] = mapped_column(String(64), index=True)
    gate: Mapped[str] = mapped_column(String(16), default="passed")  # blocked/needs_review/passed
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
