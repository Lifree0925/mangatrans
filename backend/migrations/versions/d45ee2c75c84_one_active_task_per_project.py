"""one active task per project

Revision ID: d45ee2c75c84
Revises: e55fc153432e
Create Date: 2026-11-24 19:50:38.979286

"""
from alembic import op
import sqlalchemy as sa


revision = 'd45ee2c75c84'
down_revision = 'e55fc153432e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 历史版本没有数据库级约束：先保留每个项目优先级最高/最新的活动任务，
    # 其余置为 failed，避免直接建唯一索引失败。
    op.execute(
        """
        UPDATE tasks
        SET status = 'failed',
            error_message = COALESCE(error_message, '历史并发活动任务已自动终止')
        WHERE id IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY project_id
                           ORDER BY CASE status
                               WHEN 'running' THEN 0
                               WHEN 'queued' THEN 1
                               ELSE 2
                           END,
                           created_at DESC
                       ) AS rn
                FROM tasks
                WHERE status IN ('queued', 'running', 'paused')
            )
            WHERE rn > 1
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_tasks_one_active_per_project
        ON tasks(project_id)
        WHERE status IN ('queued', 'running', 'paused')
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_tasks_one_active_per_project")
