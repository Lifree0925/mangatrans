"""项目模型。"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    source_language: Mapped[str] = mapped_column(String(16), default="zh-CN")
    target_language: Mapped[str] = mapped_column(String(16), default="en")
    active_source_document_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    style_config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 项目文件夹所在磁盘位置（为空则使用软件数据目录）
    storage_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
