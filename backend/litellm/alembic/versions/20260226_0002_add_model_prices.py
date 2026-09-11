"""add model_prices table with advanced pricing config

Revision ID: 20260226_0002
Revises: 20260226_0001
Create Date: 2026-02-26

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "20260226_0002"
down_revision = "20260226_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_prices",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("model_name", sa.String(length=255), nullable=False, unique=True),
        sa.Column("model_family", sa.String(length=50), nullable=False, server_default="generic"),
        sa.Column("pricing_config", JSONB, nullable=False),
        sa.Column("currency", sa.String(length=16), nullable=False, server_default="USD"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_model_prices_model_name", "model_prices", ["model_name"])
    op.create_index("ix_model_prices_is_active", "model_prices", ["is_active"])


def downgrade() -> None:
    op.drop_index("ix_model_prices_is_active", table_name="model_prices")
    op.drop_index("ix_model_prices_model_name", table_name="model_prices")
    op.drop_table("model_prices")
