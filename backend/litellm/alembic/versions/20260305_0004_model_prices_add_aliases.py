"""model_prices 增加 aliases JSONB 列，支持模型名别名查找

Revision ID: 20260305_0004
Revises: 20260226_0003
Create Date: 2026-03-05

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "20260305_0004"
down_revision = "20260226_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_prices",
        sa.Column(
            "aliases",
            JSONB,
            nullable=True,
            comment="模型名别名列表，如 [\"kimi-k2-0905\", \"ep-20250530...\"]，查价时自动匹配",
        ),
    )


def downgrade() -> None:
    op.drop_column("model_prices", "aliases")
