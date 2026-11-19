"""知识库与场景卡模型。"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class KnowledgeItem(Base):
    __tablename__ = "knowledge_items"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id"), index=True)
    item_type: Mapped[str] = mapped_column(String(16))  # character/term/relation/rule
    canonical_key: Mapped[str] = mapped_column(String(200))
    data_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(
        String(16), default="candidate"
    )  # candidate/approved/auto_approved/rejected
    visibility_scope: Mapped[str] = mapped_column(String(16), default="global")
    reveal_segment_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="ai")  # ai/user/imported
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class KnowledgeAlias(Base):
    __tablename__ = "knowledge_aliases"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    knowledge_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("knowledge_items.id"), index=True
    )
    alias: Mapped[str] = mapped_column(String(200))
    alias_type: Mapped[str] = mapped_column(String(16), default="variant")
    scope_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="candidate")


class KnowledgeEvidence(Base):
    __tablename__ = "knowledge_evidence"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    knowledge_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("knowledge_items.id"), index=True
    )
    segment_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    quote: Mapped[str] = mapped_column(Text)
    evidence_type: Mapped[str] = mapped_column(String(16), default="explicit")


class SceneContextCard(Base):
    __tablename__ = "scene_context_cards"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id"), index=True)
    source_document_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("source_documents.id"), index=True
    )
    scene_id: Mapped[str] = mapped_column(String(32), ForeignKey("scenes.id"), index=True)
    card_json: Mapped[str] = mapped_column(Text, default="{}")
    evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(16), default="generated")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
