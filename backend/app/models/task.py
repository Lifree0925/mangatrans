"""任务与子任务模型（SQLite 持久化队列，无租约/心跳）。"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id"), index=True)
    task_type: Mapped[str] = mapped_column(String(16))  # analyze / translate / qa
    status: Mapped[str] = mapped_column(String(16), default="queued")
    progress_json: Mapped[str] = mapped_column(Text, default="{}")
    config_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    last_checkpoint: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 线程型任务的实际开始时间：用于僵尸检测与崩溃恢复（约束：started_at 超时回收）
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 归档：成功后归档，任务中心隐藏、历史记录查看
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class TaskItem(Base):
    __tablename__ = "task_items"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(String(32), ForeignKey("tasks.id"), index=True)
    segment_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    window_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    # 两阶段调度：draft（可并发）→ polish（串行 carry-over）
    phase: Mapped[str] = mapped_column(
        String(16), default="draft", server_default="draft", nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    window_segments_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
