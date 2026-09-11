"""add vendor_apikey_usage_daily table for API-key filtering

Revision ID: 20260811_0002
Revises: 20260526_0001
Create Date: 2026-08-11 00:00:00.000000

新增 vendor_apikey_usage_daily 表: 按 (vendor, day, api_key, model) 拆分的平行明细表.
只有上游能按 API key (token_name) 拆分的 vendor 才写 (目前仅 apevon).
主表 vendor_model_usage_daily / vendor_usage_daily 完全不动.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '20260811_0002'
down_revision: Union[str, None] = '20260526_0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'vendor_apikey_usage_daily',
        sa.Column('vendor_id', sa.String(), nullable=False),
        sa.Column('usage_date', sa.Date(), nullable=False),
        sa.Column('api_key', sa.String(length=255), nullable=False),
        sa.Column('model', sa.String(), nullable=False),
        sa.Column('prompt_tokens', sa.BigInteger(), nullable=True),
        sa.Column('completion_tokens', sa.BigInteger(), nullable=True),
        sa.Column('cache_read_tokens', sa.BigInteger(), nullable=True),
        sa.Column('cache_write_tokens', sa.BigInteger(), nullable=True),
        sa.Column('total_tokens', sa.BigInteger(), nullable=True),
        sa.Column('request_count', sa.Integer(), nullable=True),
        sa.Column('image_count', sa.Integer(), nullable=True),
        sa.Column('cost_native', sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column('cost_usd', sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column('cost_cny', sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column('fx_rate', sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column('last_run_id', sa.BigInteger(),
                  sa.ForeignKey('llm_usage_dashboard.vendor_ingest_run.id'), nullable=True),
        sa.Column('ingested_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('vendor_id', 'usage_date', 'api_key', 'model'),
        schema='llm_usage_dashboard',
    )
    op.create_index('idx_vakud_vendor_date', 'vendor_apikey_usage_daily',
                    ['vendor_id', 'usage_date'], unique=False, schema='llm_usage_dashboard')
    op.create_index('idx_vakud_vendor_key', 'vendor_apikey_usage_daily',
                    ['vendor_id', 'api_key'], unique=False, schema='llm_usage_dashboard')
    op.create_index('idx_vakud_date_model', 'vendor_apikey_usage_daily',
                    ['usage_date', 'model'], unique=False, schema='llm_usage_dashboard')


def downgrade() -> None:
    op.drop_index('idx_vakud_date_model', table_name='vendor_apikey_usage_daily',
                  schema='llm_usage_dashboard')
    op.drop_index('idx_vakud_vendor_key', table_name='vendor_apikey_usage_daily',
                  schema='llm_usage_dashboard')
    op.drop_index('idx_vakud_vendor_date', table_name='vendor_apikey_usage_daily',
                  schema='llm_usage_dashboard')
    op.drop_table('vendor_apikey_usage_daily', schema='llm_usage_dashboard')
