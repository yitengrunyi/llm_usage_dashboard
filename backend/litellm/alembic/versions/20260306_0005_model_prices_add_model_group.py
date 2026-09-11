"""model_prices 增加 model_group 列，记录原始 LiteLLM model_group

Revision ID: 20260306_0005
Revises: 20260305_0004
Create Date: 2026-03-06

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260306_0005"
down_revision = "20260305_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_prices",
        sa.Column(
            "model_group",
            sa.String(255),
            nullable=True,
            comment="原始 model_group（来自 LiteLLM SpendLogs），用于追溯定价来源",
        ),
    )


def downgrade() -> None:
    op.drop_column("model_prices", "model_group")
