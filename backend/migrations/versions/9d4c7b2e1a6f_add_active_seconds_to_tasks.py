"""add active_seconds to tasks

Revision ID: 9d4c7b2e1a6f
Revises: e3f8a6b4c2d0
Create Date: 2026-11-24 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '9d4c7b2e1a6f'
down_revision = 'e3f8a6b4c2d0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'tasks',
        sa.Column('active_seconds', sa.Float(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    op.drop_column('tasks', 'active_seconds')
