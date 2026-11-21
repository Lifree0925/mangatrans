"""台词本：章节 → 场景 → 角色纯对白流水（知识库反推全文一致性的地基）。"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.task import new_id, utcnow


class DialogueLine(Base):
    __tablename__ = "dialogue_lines"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id"), index=True)
    source_document_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    chapter_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    scene_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    segment_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 归一化说话人（对照知识库角色档案），无归属时用「旁白」/「未知」
    speaker_name: Mapped[str] = mapped_column(String(64), default="未知")
    # 原文中实际使用的称呼（如「玄德」），保留用于称呼体系分析
    speaker_alias: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 场景内序号（展示顺序）
    line_no: Mapped[int] = mapped_column(Integer, default=0)
    # 单句台词（必须是所属片段 source_text 的连续子串）
    source_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
