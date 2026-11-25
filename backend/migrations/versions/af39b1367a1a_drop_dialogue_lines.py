"""drop dialogue lines

Revision ID: af39b1367a1a
Revises: 632e2d22a927
Create Date: 2026-11-25 13:07:43.849485

"""
from alembic import op
import sqlalchemy as sa


revision = 'af39b1367a1a'
down_revision = '632e2d22a927'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 台词本已从产品中移除：它只是 source_segments 的重复派生数据，
    # 不参与翻译、QA 或最终剧本导出。
    op.drop_table("dialogue_lines")


def downgrade() -> None:
    op.create_table(
        "dialogue_lines",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("source_document_id", sa.String(length=32), nullable=True),
        sa.Column("chapter_id", sa.String(length=32), nullable=True),
        sa.Column("scene_id", sa.String(length=32), nullable=True),
        sa.Column("segment_id", sa.String(length=32), nullable=True),
        sa.Column("speaker_name", sa.String(length=64), nullable=False),
        sa.Column("speaker_alias", sa.String(length=64), nullable=True),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("target_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_dialogue_lines_project_id", "dialogue_lines", ["project_id"])
    op.create_index("ix_dialogue_lines_chapter_id", "dialogue_lines", ["chapter_id"])
    op.create_index("ix_dialogue_lines_scene_id", "dialogue_lines", ["scene_id"])
    op.create_index("ix_dialogue_lines_segment_id", "dialogue_lines", ["segment_id"])
