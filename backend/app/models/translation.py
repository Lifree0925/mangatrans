"""译文、Provider 配置与风格基线模型。"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Translation(Base):
    __tablename__ = "translations"
    __table_args__ = (UniqueConstraint("segment_id", "version"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    segment_id: Mapped[str] = mapped_column(String(32), ForeignKey("source_segments.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    stage: Mapped[str] = mapped_column(String(16), default="polished")  # draft/polished/final
    target_text: Mapped[str] = mapped_column(Text)
    review_status: Mapped[str] = mapped_column(String(16), default="unreviewed")
    freshness_status: Mapped[str] = mapped_column(String(16), default="current")
    notes_json: Mapped[str] = mapped_column(Text, default="[]")
    ambiguity_json: Mapped[str] = mapped_column(Text, default="[]")
    model_name: Mapped[str] = mapped_column(String(200), default="")
    provider_id: Mapped[str] = mapped_column(String(100), default="")
    prompt_version: Mapped[str] = mapped_column(String(32), default="")
    context_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    source_hash: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProviderConfig(Base):
    __tablename__ = "provider_configs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    base_url: Mapped[str] = mapped_column(String(500))
    model_name: Mapped[str] = mapped_column(String(200))
    credential_ref: Mapped[str] = mapped_column(String(200), default="")
    api_mode: Mapped[str] = mapped_column(String(32), default="openai-compatible")
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=120)
    max_retries: Mapped[int] = mapped_column(Integer, default=2)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    config_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class StyleGuide(Base):
    """冻结的风格基线（翻译资产）。"""

    __tablename__ = "style_guides"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    guide_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft/frozen
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
