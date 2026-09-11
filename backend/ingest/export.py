"""导出 endpoint — 通用筛选 + 颗粒度切换, 返 CSV stream.

输入:
  start / end                  日期范围 (含端)
  granularity                  'day' | 'week' | 'month'
  vendor_ids                   逗号分隔, 空 = 全部 enabled vendor
  models                       逗号分隔, 空 = 全部 model
  columns                      逗号分隔, 列白名单. 不传走默认代表性集合
                               可选: vendor_name / model / period / request_count /
                                     prompt_tokens / completion_tokens / cache_read_tokens /
                                     cache_write_tokens / total_tokens / image_count /
                                     cost_native / cost_usd / cost_cny / native_currency

颗粒度桶:
  day:   一行 = 一天 × vendor × model        (直接读 vendor_model_usage_daily)
  week:  一行 = 一周 (周一起) × vendor × model
  month: 一行 = 一月 × vendor × model
  周/月 用 PG date_trunc + 周一对齐
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import logging
from typing import Iterator

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from ingest.db import SessionLocal

log = logging.getLogger("ingest.export")

router = APIRouter(prefix="/api/export", tags=["export"])


# 所有支持的列, 顺序固定 (CSV header 按这个顺序; UI checkbox 也按这个顺序展示)
ALL_COLUMNS = [
    "period", "vendor_name", "model",
    "request_count", "image_count",
    "prompt_tokens", "completion_tokens",
    "cache_read_tokens", "cache_write_tokens", "total_tokens",
    "cost_native", "native_currency", "cost_usd", "cost_cny",
]

# 列中文名 (CSV 表头用)
COLUMN_LABELS = {
    "period": "时段",
    "vendor_name": "供应商",
    "model": "模型",
    "request_count": "调用次数",
    "image_count": "生成张数",
    "prompt_tokens": "输入Tokens",
    "completion_tokens": "输出Tokens",
    "cache_read_tokens": "缓存命中Tokens",
    "cache_write_tokens": "缓存写入Tokens",
    "total_tokens": "总Tokens",
    "cost_native": "原币金额",
    "native_currency": "原币种",
    "cost_usd": "USD",
    "cost_cny": "CNY",
}

# 默认勾选 — 跟 Overview 明细表对齐, 不含 cache 拆分 / native (太细)
DEFAULT_COLUMNS = [
    "period", "vendor_name", "model",
    "request_count", "prompt_tokens", "completion_tokens", "total_tokens",
    "cost_usd", "cost_cny",
]


@router.get("/options")
def export_options():
    """前端拉筛选项: vendor 列表 + model 列表 + 列定义.

    model 全表 DISTINCT 一次 SQL, 不分 vendor (前端可以用 vendor 过滤后再筛 model).
    """
    with SessionLocal() as s:
        vendors = s.execute(text("""
            SELECT vendor_id, display_name, enabled
            FROM vendor_meta
            ORDER BY display_name
        """)).all()
        # model 表 DISTINCT (一并带回 vendor_ids 数组, 前端可按 vendor 联动筛 model)
        models = s.execute(text("""
            SELECT model, array_agg(DISTINCT vendor_id) AS vendor_ids
            FROM vendor_model_usage_daily
            GROUP BY model
            ORDER BY model
        """)).all()
    return {
        "vendors": [{"id": v[0], "name": v[1], "enabled": v[2]} for v in vendors],
        "models": [{"name": m[0], "vendor_ids": list(m[1])} for m in models],
        "columns": [{"key": k, "label": COLUMN_LABELS[k], "default": k in DEFAULT_COLUMNS}
                    for k in ALL_COLUMNS],
        "granularities": [
            {"key": "day", "label": "按天"},
            {"key": "week", "label": "按周"},
            {"key": "month", "label": "按月"},
        ],
    }


def _build_query(
    start: dt.date, end: dt.date, granularity: str,
    vendor_ids: list[str] | None, models: list[str] | None,
    dimension: str = "vendor_model",
) -> tuple[str, dict]:
    """拼 SQL.
    dimension:
      - 'vendor_model' (默认): 一行 = period × vendor × model
      - 'vendor':              一行 = period × vendor (所有 model 合并, model 列出 NULL)
      - 'model':               一行 = period × model  (所有 vendor 合并, vendor_name 列出 '__ALL__')
    """
    if granularity == "day":
        period_expr = "u.usage_date::text"
    elif granularity == "week":
        # date_trunc('week', ...) PG 默认以周一起算 (ISO 8601), 跟前端 lastWeekRange 对齐
        period_expr = "to_char(date_trunc('week', u.usage_date), 'YYYY-MM-DD')"
    elif granularity == "month":
        period_expr = "to_char(date_trunc('month', u.usage_date), 'YYYY-MM')"
    else:
        raise HTTPException(400, f"unknown granularity: {granularity}")

    if dimension == "vendor_model":
        vendor_select = "COALESCE(m.display_name, u.vendor_id) AS vendor_name"
        model_select = "u.model"
        group_by = "period, vendor_name, u.model"
        order_by = "period, vendor_name, u.model"
    elif dimension == "vendor":
        vendor_select = "COALESCE(m.display_name, u.vendor_id) AS vendor_name"
        model_select = "NULL::text AS model"
        group_by = "period, vendor_name"
        order_by = "period, vendor_name"
    elif dimension == "model":
        # 按 model 聚合所有 vendor — vendor_name 标 '合计', native_currency 也合并不下
        vendor_select = "'(合计)'::text AS vendor_name"
        model_select = "u.model"
        group_by = "period, u.model"
        order_by = "period, SUM(u.cost_cny) DESC NULLS LAST, u.model"
    else:
        raise HTTPException(400, f"unknown dimension: {dimension}")

    where = ["u.usage_date >= :start", "u.usage_date <= :end"]
    params = {"start": start, "end": end}
    if vendor_ids:
        where.append("u.vendor_id = ANY(:vendor_ids)")
        params["vendor_ids"] = vendor_ids
    if models:
        where.append("u.model = ANY(:models)")
        params["models"] = models

    sql = f"""
        SELECT
            {period_expr} AS period,
            {vendor_select},
            {model_select},
            SUM(u.request_count)      AS request_count,
            SUM(u.image_count)        AS image_count,
            SUM(u.prompt_tokens)      AS prompt_tokens,
            SUM(u.completion_tokens)  AS completion_tokens,
            SUM(u.cache_read_tokens)  AS cache_read_tokens,
            SUM(u.cache_write_tokens) AS cache_write_tokens,
            SUM(u.total_tokens)       AS total_tokens,
            SUM(u.cost_native)        AS cost_native,
            MAX(m.native_currency)    AS native_currency,
            SUM(u.cost_usd)           AS cost_usd,
            SUM(u.cost_cny)           AS cost_cny
        FROM vendor_model_usage_daily u
        LEFT JOIN vendor_meta m ON m.vendor_id = u.vendor_id
        WHERE {' AND '.join(where)}
        GROUP BY {group_by}
        ORDER BY {order_by}
    """
    return sql, params


def _csv_stream(headers: list[str], rows: Iterator[tuple]) -> Iterator[str]:
    """生成器: 一次 yield 一行 CSV 字符串. UTF-8 BOM 防 Excel 乱码."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    # BOM 必须在第一个 yield, 不能放 header 之前的独立 yield (有的 client 把 chunk 当一行)
    buf.write("﻿")
    writer.writerow([COLUMN_LABELS.get(h, h) for h in headers])
    yield buf.getvalue()
    buf.seek(0); buf.truncate(0)

    for row in rows:
        # row 是 SQLAlchemy Row, 按 headers 顺序取
        out = []
        for col in headers:
            v = row._mapping.get(col)
            if v is None:
                out.append("")
            elif col in ("cost_native", "cost_usd", "cost_cny"):
                out.append(f"{float(v):.4f}")
            else:
                out.append(str(v))
        writer.writerow(out)
        yield buf.getvalue()
        buf.seek(0); buf.truncate(0)


@router.get("/count")
def export_count(
    start: dt.date = Query(...),
    end: dt.date = Query(...),
    granularity: str = Query("day"),
    dimension: str = Query("vendor_model", description="vendor_model | vendor"),
    vendor_ids: str = Query(""),
    models: str = Query(""),
):
    """同 /api/export 的 SQL 但只数行 — 给前端做精确的"预计导出行数"."""
    if start > end:
        raise HTTPException(400, "start > end")
    vlist = [v.strip() for v in vendor_ids.split(",") if v.strip()] or None
    mlist = [m.strip() for m in models.split(",") if m.strip()] or None
    sql, params = _build_query(start, end, granularity, vlist, mlist, dimension)
    count_sql = f"SELECT COUNT(*) FROM ({sql}) AS sub"
    with SessionLocal() as s:
        n = s.execute(text(count_sql), params).scalar() or 0
    return {"count": int(n)}


@router.get("")
def export_csv(
    start: dt.date = Query(..., description="起始日期 (含)"),
    end: dt.date = Query(..., description="结束日期 (含)"),
    granularity: str = Query("day", description="day / week / month"),
    dimension: str = Query("vendor_model", description="vendor_model | vendor"),
    vendor_ids: str = Query("", description="逗号分隔 vendor_id, 空=全部"),
    models: str = Query("", description="逗号分隔 model name, 空=全部"),
    columns: str = Query("", description="逗号分隔列名, 空=默认列"),
):
    """流式返回 CSV. 大数据量也不爆内存."""
    if start > end:
        raise HTTPException(400, "start > end")

    vlist = [v.strip() for v in vendor_ids.split(",") if v.strip()] or None
    mlist = [m.strip() for m in models.split(",") if m.strip()] or None
    clist = [c.strip() for c in columns.split(",") if c.strip()] or DEFAULT_COLUMNS
    # 防越权 — 只允许已知列
    bad = [c for c in clist if c not in ALL_COLUMNS]
    if bad:
        raise HTTPException(400, f"unknown columns: {bad}")
    # 按 vendor 聚合时 model 列没意义, 自动剔掉避免一列全空
    if dimension == "vendor" and "model" in clist:
        clist = [c for c in clist if c != "model"]
    # 按 model 聚合 (跨 vendor) 时, native 币种混着 (USD+CNY 不能直接加), 强制剔
    if dimension == "model":
        clist = [c for c in clist if c not in ("cost_native", "native_currency", "vendor_name")]

    sql, params = _build_query(start, end, granularity, vlist, mlist, dimension)

    # 用 session.execute().yield_per 让 PG 流式吐, 不一次性 fetchall (大窗口可能上百万行)
    s = SessionLocal()
    try:
        result = s.execute(text(sql), params).yield_per(1000)
    except Exception:
        s.close()
        raise

    def _gen():
        try:
            yield from _csv_stream(clist, result)
        finally:
            s.close()

    fname = f"export_{start}_{end}_{granularity}.csv"
    return StreamingResponse(
        _gen(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
