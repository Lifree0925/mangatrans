"""add task error_code

Revision ID: e3f8a6b4c2d0
Revises: d2e7f5a9c3b1
Create Date: 2026-11-23 18:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'e3f8a6b4c2d0'
down_revision = 'd2e7f5a9c3b1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('tasks', sa.Column('error_code', sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column('tasks', 'error_code')
