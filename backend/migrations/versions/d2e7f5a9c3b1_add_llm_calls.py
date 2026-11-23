"""add llm call usage ledger

Revision ID: d2e7f5a9c3b1
Revises: c7fcc0f742a1
Create Date: 2026-11-23 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'd2e7f5a9c3b1'
down_revision = 'c7fcc0f742a1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'llm_calls',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('task_id', sa.String(length=32), nullable=True),
        sa.Column('project_id', sa.String(length=32), nullable=True),
        sa.Column('stage', sa.String(length=32), nullable=False),
        sa.Column('model_name', sa.String(length=200), nullable=False),
        sa.Column('provider_id', sa.String(length=100), nullable=False),
        sa.Column('prompt_tokens', sa.Integer(), nullable=False),
        sa.Column('completion_tokens', sa.Integer(), nullable=False),
        sa.Column('reasoning_tokens', sa.Integer(), nullable=False),
        sa.Column('cached_tokens', sa.Integer(), nullable=False),
        sa.Column('total_tokens', sa.Integer(), nullable=False),
        sa.Column('cache_hit', sa.Boolean(), nullable=False),
        sa.Column('success', sa.Boolean(), nullable=False),
        sa.Column('latency_ms', sa.Float(), nullable=False),
        sa.Column('finish_reason', sa.String(length=32), nullable=True),
        sa.Column('error_code', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_llm_calls_project_id'), 'llm_calls', ['project_id'], unique=False)
    op.create_index(op.f('ix_llm_calls_stage'), 'llm_calls', ['stage'], unique=False)
    op.create_index(op.f('ix_llm_calls_task_id'), 'llm_calls', ['task_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_llm_calls_task_id'), table_name='llm_calls')
    op.drop_index(op.f('ix_llm_calls_stage'), table_name='llm_calls')
    op.drop_index(op.f('ix_llm_calls_project_id'), table_name='llm_calls')
    op.drop_table('llm_calls')
