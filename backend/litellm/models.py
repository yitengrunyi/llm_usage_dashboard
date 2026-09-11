from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from litellm.db import Base


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    usage_events: Mapped[list["TokenUsageEvent"]] = relationship(back_populates="tenant")


class TokenUsageEvent(Base):
    __tablename__ = "token_usage_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tokens_input: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_output: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    meta_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="usage_events")

    __table_args__ = (
        Index("ix_usage_tenant_time", "tenant_id", "occurred_at"),
        Index("ix_usage_request_id", "request_id"),
    )


class BillingSnapshot(Base):
    __tablename__ = "billing_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    tokens_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    currency: Mapped[str] = mapped_column(String(16), nullable=False, default="USD")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (Index("ix_snapshot_tenant_period", "tenant_id", "period_start", "period_end"),)


# ---------------------------------------------------------------------------
# LiteLLM_SpendLogs —— 只读映射，不由本服务管理迁移
# ---------------------------------------------------------------------------
class LiteLLMSpendLog(Base):
    """
    映射 LiteLLM 已有的 LiteLLM_SpendLogs 表（只读，字段以数据库实际结构为准）。
    metadata / request_tags / messages / response / proxy_server_request 均为 JSONB，
    SQLAlchemy 读出后直接是 Python dict，无需 json.loads。
    """

    __tablename__ = "LiteLLM_SpendLogs"

    request_id: Mapped[str] = mapped_column(Text, primary_key=True)
    call_type: Mapped[str] = mapped_column(Text, nullable=False)
    api_key: Mapped[str] = mapped_column(Text, nullable=False)
    spend: Mapped[float] = mapped_column(Float, nullable=False)

    # token 用量（NOT NULL）
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False)

    # 时间（数据库存的是 timestamp WITHOUT time zone）
    startTime: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    endTime: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    completionStartTime: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)

    # 模型信息
    model: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_group: Mapped[str | None] = mapped_column(Text, nullable=True)
    custom_llm_provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_base: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 用户/团队维度
    user: Mapped[str | None] = mapped_column(Text, nullable=True)
    team_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    end_user: Mapped[str | None] = mapped_column(Text, nullable=True)
    organization_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 缓存
    cache_hit: Mapped[str | None] = mapped_column(Text, nullable=True)
    cache_key: Mapped[str | None] = mapped_column(Text, nullable=True)

    # JSONB 字段（SQLAlchemy 读出即为 dict，不需要 json.loads）
    # 注意：metadata 是 SQLAlchemy 保留名，Python 属性用 log_metadata，数据库列名仍为 metadata
    log_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    request_tags: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    messages: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    proxy_server_request: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # 其他
    requester_ip_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    mcp_namespaced_tool_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_spendlog_model_time", "model", "startTime"),
        Index("ix_spendlog_user_time", "user", "startTime"),
        Index("ix_spendlog_team_time", "team_id", "startTime"),
        {"extend_existing": True},
    )


# ---------------------------------------------------------------------------
# LiteLLMVerificationToken —— LiteLLM 自己维护的 API key 表 (read-only 映射)
# ---------------------------------------------------------------------------
class LiteLLMVerificationToken(Base):
    """LiteLLM_VerificationToken 表只读映射, 给 by-api-key 下拉用.

    实际表 36 列, 只映射前端展示需要的: token / alias / spend / max_budget /
    expires / blocked / user_id / team_id. 不参与 alembic, extend_existing=True.
    """
    __tablename__ = "LiteLLM_VerificationToken"
    __table_args__ = ({"extend_existing": True},)

    token: Mapped[str] = mapped_column(Text, primary_key=True)
    key_alias: Mapped[str | None] = mapped_column(Text)
    spend: Mapped[float | None] = mapped_column(Float)
    max_budget: Mapped[float | None] = mapped_column(Float)
    expires: Mapped[datetime | None] = mapped_column(DateTime)
    blocked: Mapped[bool | None] = mapped_column(Boolean)
    user_id: Mapped[str | None] = mapped_column(Text)
    team_id: Mapped[str | None] = mapped_column(Text)


# ---------------------------------------------------------------------------
# ModelPrice —— 本服务自己维护的模型单价表
# ---------------------------------------------------------------------------
class ModelPrice(Base):
    """
    各模型的计费配置。pricing_config 存 JSON，对应 app.pricing.types.PricingConfig。

    pricing_config 示例：
      标准：     {"type": "standard", "input_per_1m": 2.5, "output_per_1m": 10.0}
      含缓存：   {"type": "with_cache", "input_per_1m": 3.0, "output_per_1m": 15.0,
                  "cache_write_per_1m": 3.75, "cache_read_per_1m": 0.30}
      含思考：   {"type": "with_thinking", "input_per_1m": 3.0, "output_per_1m": 15.0,
                  "thinking_per_1m": 15.0}
      组合：     {"type": "combined", ...}
      阶梯：     {"type": "tiered", "tiers": [...]}
    """

    __tablename__ = "model_prices"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    model_group: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="原始 model_group（来自 LiteLLM SpendLogs），用于追溯定价来源")
    provider: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="供应商，空表示该模型默认单价；同模型不同供应商可配置不同单价")
    model_family: Mapped[str] = mapped_column(String(50), nullable=False, server_default="generic")
    pricing_config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False, server_default="USD")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    aliases: Mapped[list | None] = mapped_column(JSONB, nullable=True, comment="模型名别名列表，如 [\"kimi-k2-0905\", \"ep-20250530...\"]，查价时自动匹配")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (UniqueConstraint("model_name", "provider", name="uq_model_prices_model_provider"),)
