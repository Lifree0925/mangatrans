"""add llm call degradations

Revision ID: e55fc153432e
Revises: 9d4c7b2e1a6f
Create Date: 2026-11-24 19:49:15.325868

"""
from alembic import op
import sqlalchemy as sa


revision = 'e55fc153432e'
down_revision = '9d4c7b2e1a6f'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "llm_calls",
        sa.Column("degradations_json", sa.Text(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("llm_calls", "degradations_json")
