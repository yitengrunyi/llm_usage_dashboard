"""model_prices 增加 provider，支持同模型不同供应商不同单价

Revision ID: 20260226_0003
Revises: 20260226_0002
Create Date: 2026-02-26

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260226_0003"
down_revision = "20260226_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_prices",
        sa.Column("provider", sa.String(length=128), nullable=True, comment="供应商，空为默认单价"),
    )
    op.drop_constraint("model_prices_model_name_key", "model_prices", type_="unique")
    op.create_unique_constraint(
        "uq_model_prices_model_provider",
        "model_prices",
        ["model_name", "provider"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_model_prices_model_provider", "model_prices", type_="unique")
    op.create_unique_constraint("model_prices_model_name_key", "model_prices", ["model_name"])
    op.drop_column("model_prices", "provider")
