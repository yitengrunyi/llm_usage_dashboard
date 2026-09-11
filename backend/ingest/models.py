"""6 张表的 ORM 定义. 全部自动落在 llm_usage_dashboard schema.

颗粒度: 按天 (CST 自然日) × vendor × model.
三层物化避免热查询做 GROUP BY.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB

from ingest.db import Base


# ────────────── 1. Vendor 元数据 (静态, < 20 行) ──────────────
class VendorMeta(Base):
    __tablename__ = "vendor_meta"

    vendor_id: Mapped[str] = mapped_column(String, primary_key=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    vendor_type: Mapped[str] = mapped_column(String, nullable=False)
    native_currency: Mapped[str] = mapped_column(String(8), nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ────────────── 2. 全局日汇总 (Overview 总图热表, 一天 1 行) ──────────────
class UsageDailyTotal(Base):
    __tablename__ = "usage_daily_total"

    usage_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)

    prompt_tokens: Mapped[int | None] = mapped_column(BigInteger)
    completion_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_read_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_write_tokens: Mapped[int | None] = mapped_column(BigInteger)
    total_tokens: Mapped[int | None] = mapped_column(BigInteger)

    request_count: Mapped[int | None] = mapped_column(BigInteger)
    image_count: Mapped[int | None] = mapped_column(BigInteger)

    cost_usd: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    cost_cny: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    fx_rate: Mapped[float | None] = mapped_column(Numeric(12, 6))

    complete_vendor_count: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_vendor_count: Mapped[int] = mapped_column(Integer, nullable=False)
    is_complete: Mapped[bool] = mapped_column(
        Boolean,
        Computed("complete_vendor_count = expected_vendor_count", persisted=True),
        nullable=False,
    )

    ingested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("idx_udt_complete", "is_complete"),
    )


# ────────────── 3. Vendor 维度日聚合 (一行 = 一家 vendor 一天) ──────────────
class VendorUsageDaily(Base):
    __tablename__ = "vendor_usage_daily"

    vendor_id: Mapped[str] = mapped_column(String, nullable=False)
    usage_date: Mapped[dt.date] = mapped_column(Date, nullable=False)

    prompt_tokens: Mapped[int | None] = mapped_column(BigInteger)
    completion_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_read_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_write_tokens: Mapped[int | None] = mapped_column(BigInteger)
    total_tokens: Mapped[int | None] = mapped_column(BigInteger)

    request_count: Mapped[int | None] = mapped_column(Integer)
    image_count: Mapped[int | None] = mapped_column(Integer)

    cost_native: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    cost_usd: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    cost_cny: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    fx_rate: Mapped[float | None] = mapped_column(Numeric(12, 6))

    has_cache_detail: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("vendor_ingest_run.id")
    )

    ingested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        PrimaryKeyConstraint("vendor_id", "usage_date"),
        ForeignKeyConstraint(["vendor_id"], ["vendor_meta.vendor_id"]),
        Index("idx_vud_date", "usage_date"),
    )


# ────────────── 4. Vendor × Model 维度日明细 ──────────────
class VendorModelUsageDaily(Base):
    __tablename__ = "vendor_model_usage_daily"

    vendor_id: Mapped[str] = mapped_column(String, nullable=False)
    usage_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)

    prompt_tokens: Mapped[int | None] = mapped_column(BigInteger)
    completion_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_read_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_write_tokens: Mapped[int | None] = mapped_column(BigInteger)
    total_tokens: Mapped[int | None] = mapped_column(BigInteger)

    request_count: Mapped[int | None] = mapped_column(Integer)
    image_count: Mapped[int | None] = mapped_column(Integer)

    cost_native: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    cost_usd: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    cost_cny: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    fx_rate: Mapped[float | None] = mapped_column(Numeric(12, 6))

    last_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("vendor_ingest_run.id")
    )

    ingested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        PrimaryKeyConstraint("vendor_id", "usage_date", "model"),
        ForeignKeyConstraint(
            ["vendor_id", "usage_date"],
            ["vendor_usage_daily.vendor_id", "vendor_usage_daily.usage_date"],
        ),
        Index("idx_vmud_vendor_date", "vendor_id", "usage_date"),
        Index("idx_vmud_date_model", "usage_date", "model"),
        Index("idx_vmud_model", "model"),
    )


# ────────────── 4b. Vendor × API Key × Model 维度日明细 (按 key 筛选用) ──────────────
# 与 vendor_model_usage_daily 平行的表, 颗粒度 finer: 一行 = (vendor, day, api_key, model).
# 只有上游能拆到 API key 级别的 vendor 才写这张表 (adapter 实现 fetch_apikey_rows 即可,
# e.g. apevon / wangsu / grok / openai / new-api 系 slow path). 不带 key 拆分的 vendor
# 这张表始终空 — vendor 详情页的 api key 筛选器自动隐藏.
# vendor_usage_daily / vendor_model_usage_daily 这两张主表完全不受影响.
class VendorApiKeyUsageDaily(Base):
    __tablename__ = "vendor_apikey_usage_daily"

    vendor_id: Mapped[str] = mapped_column(String, nullable=False)
    usage_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    api_key: Mapped[str] = mapped_column(String(255), nullable=False)  # 上游 token_name
    model: Mapped[str] = mapped_column(String, nullable=False)

    prompt_tokens: Mapped[int | None] = mapped_column(BigInteger)
    completion_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_read_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_write_tokens: Mapped[int | None] = mapped_column(BigInteger)
    total_tokens: Mapped[int | None] = mapped_column(BigInteger)

    request_count: Mapped[int | None] = mapped_column(Integer)
    image_count: Mapped[int | None] = mapped_column(Integer)

    cost_native: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    cost_usd: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    cost_cny: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    fx_rate: Mapped[float | None] = mapped_column(Numeric(12, 6))

    last_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("vendor_ingest_run.id")
    )

    ingested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        PrimaryKeyConstraint("vendor_id", "usage_date", "api_key", "model"),
        Index("idx_vakud_vendor_date", "vendor_id", "usage_date"),
        Index("idx_vakud_vendor_key", "vendor_id", "api_key"),
        Index("idx_vakud_date_model", "usage_date", "model"),
    )


# ────────────── 5. 任务运行历史 (审计 + 手动重试入口) ──────────────
class VendorIngestRun(Base):
    __tablename__ = "vendor_ingest_run"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    vendor_id: Mapped[str] = mapped_column(String, nullable=False)
    window_start: Mapped[dt.date] = mapped_column(Date, nullable=False)
    window_end: Mapped[dt.date] = mapped_column(Date, nullable=False)
    trigger: Mapped[str] = mapped_column(String, nullable=False)  # cron / manual / backfill
    status: Mapped[str] = mapped_column(String, nullable=False)  # running / success / failed
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    rows_upserted: Mapped[int | None] = mapped_column(Integer)
    error_msg: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("idx_run_vendor_started", "vendor_id", "started_at"),
        # 部分唯一索引: 同一 vendor 同时只允许一个 running 任务, DB 层防 xxl-job 重复触发
        Index(
            "uniq_running_per_vendor",
            "vendor_id",
            unique=True,
            postgresql_where=(status == "running"),
        ),
    )


# ────────────── 6. 每 vendor 入库游标 ──────────────
class VendorIngestState(Base):
    __tablename__ = "vendor_ingest_state"

    vendor_id: Mapped[str] = mapped_column(
        String, ForeignKey("vendor_meta.vendor_id"), primary_key=True
    )
    last_ingested_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    last_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("vendor_ingest_run.id")
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ────────────── 7. Agent 巡检报告 ──────────────
class AgentPatrolReport(Base):
    """自愈 Agent 每轮巡检的报告 — 一行一轮 (干净退出不落行, 零噪音).

    status 语义:
      running   巡检进行中 (进程崩了会有僵尸行, 只作展示用, 不阻塞下一轮)
      clean     无信号 (仅手动触发时才可能落 clean 行, cron 巡检干净直接退出)
      healed    LLM 判定无需人工 (自动修复, 或判定观察即可等 cron 自愈)
      degraded  LLM 不可用 / 循环异常, 走了确定性降级路径
      escalated 需要人工介入 (升级原因见 escalate_reason)
      error     巡检自身出错 (报告写入都失败时的兜底行)
    """
    __tablename__ = "agent_patrol_report"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    slot: Mapped[str] = mapped_column(String(16), nullable=False)  # 05:00 / 09:00 / 19:00 / manual
    patrolled_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    llm_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    model: Mapped[str | None] = mapped_column(String(64))
    signals: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # 信号指纹 (sha1) — 上一轮非 clean 报告指纹相同 = 同一问题连续未修复, 卡片加警示
    fingerprint: Mapped[str | None] = mapped_column(String(44))
    diagnosis: Mapped[str | None] = mapped_column(Text)  # LLM 中文诊断 (全文)
    actions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    verification: Mapped[list | None] = mapped_column(JSONB)  # [{action, before, after, ok}]
    summary: Mapped[str | None] = mapped_column(Text)
    escalate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    escalate_reason: Mapped[str | None] = mapped_column(Text)
    llm_meta: Mapped[dict | None] = mapped_column(JSONB)  # {turns, prompt_tokens, completion_tokens}

    __table_args__ = (Index("idx_apr_patrolled_at", "patrolled_at"),)
