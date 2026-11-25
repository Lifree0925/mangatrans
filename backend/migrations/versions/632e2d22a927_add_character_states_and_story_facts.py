"""add character states and story facts

Revision ID: 632e2d22a927
Revises: d45ee2c75c84
Create Date: 2026-11-24 20:04:40.465315

"""
from alembic import op
import sqlalchemy as sa


revision = '632e2d22a927'
down_revision = 'd45ee2c75c84'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "character_states",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("chapter_id", sa.String(length=32), nullable=False),
        sa.Column("source_storyline_id", sa.String(length=32), nullable=True),
        sa.Column("character_key", sa.String(length=200), nullable=False),
        sa.Column("chapter_no", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["chapter_id"], ["chapters.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "character_key", "chapter_id", "version",
            name="uq_character_state_version",
        ),
    )
    op.create_index("ix_character_states_project_id", "character_states", ["project_id"])
    op.create_index("ix_character_states_chapter_id", "character_states", ["chapter_id"])
    op.create_index(
        "ix_character_states_source_storyline_id", "character_states", ["source_storyline_id"]
    )
    op.create_index("ix_character_states_character_key", "character_states", ["character_key"])
    op.create_index("ix_character_states_chapter_no", "character_states", ["chapter_no"])

    op.create_table(
        "story_facts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("chapter_id", sa.String(length=32), nullable=False),
        sa.Column("source_storyline_id", sa.String(length=32), nullable=True),
        sa.Column("chapter_no", sa.Integer(), nullable=False),
        sa.Column("fact_type", sa.String(length=32), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("predicate", sa.String(length=200), nullable=False),
        sa.Column("object_text", sa.String(length=500), nullable=False),
        sa.Column("fact_text", sa.Text(), nullable=False),
        sa.Column("valid_from_chapter_no", sa.Integer(), nullable=False),
        sa.Column("valid_to_chapter_no", sa.Integer(), nullable=True),
        sa.Column("reveal_chapter_no", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["chapter_id"], ["chapters.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_story_facts_project_id", "story_facts", ["project_id"])
    op.create_index("ix_story_facts_chapter_id", "story_facts", ["chapter_id"])
    op.create_index("ix_story_facts_source_storyline_id", "story_facts", ["source_storyline_id"])
    op.create_index("ix_story_facts_chapter_no", "story_facts", ["chapter_no"])
    op.create_index("ix_story_facts_fact_type", "story_facts", ["fact_type"])
    op.create_index(
        "ix_story_facts_valid_from_chapter_no", "story_facts", ["valid_from_chapter_no"]
    )


def downgrade() -> None:
    op.drop_table("story_facts")
    op.drop_table("character_states")
