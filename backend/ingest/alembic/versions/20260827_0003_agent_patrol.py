"""add agent_patrol_report table for self-healing agent

Revision ID: 20260827_0003
Revises: 20260811_0002
Create Date: 2026-08-27 00:00:00.000000

新增 agent_patrol_report 表: 自愈 Agent 每轮巡检一行报告
(信号快照 / LLM 诊断 / 执行的动作 / 验证结果 / 升级原因).
干净巡检不落行; 该表只增不改, 无外键 (报告独立于 run 生命周期).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = '20260827_0003'
down_revision: Union[str, None] = '20260811_0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'agent_patrol_report',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('slot', sa.String(length=16), nullable=False),
        sa.Column('patrolled_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('llm_used', sa.Boolean(), nullable=False),
        sa.Column('model', sa.String(length=64), nullable=True),
        sa.Column('signals', JSONB(), nullable=False),
        sa.Column('fingerprint', sa.String(length=44), nullable=True),
        sa.Column('diagnosis', sa.Text(), nullable=True),
        sa.Column('actions', JSONB(), nullable=False),
        sa.Column('verification', JSONB(), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('escalate', sa.Boolean(), nullable=False),
        sa.Column('escalate_reason', sa.Text(), nullable=True),
        sa.Column('llm_meta', JSONB(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        schema='llm_usage_dashboard',
    )
    op.create_index('idx_apr_patrolled_at', 'agent_patrol_report',
                    ['patrolled_at'], unique=False, schema='llm_usage_dashboard')


def downgrade() -> None:
    op.drop_index('idx_apr_patrolled_at', table_name='agent_patrol_report',
                  schema='llm_usage_dashboard')
    op.drop_table('agent_patrol_report', schema='llm_usage_dashboard')
