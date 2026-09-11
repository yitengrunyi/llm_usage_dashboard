"""DB 查询层 — 把 vendor_usage_daily / vendor_model_usage_daily 翻译成
vendor service 那套 shape ({total_cost, models, daily, currency, vendor_id, vendor_name}).

设计要点:
- 输入: vendor 字典 (含 id/name/currency) + date 范围 (含端的自然日)
- 输出: 跟 vendor_client.fetch_vendor_usage 同 shape — 主路由代码不用改
- total_cost / daily.cost 用 native 币种 (跟现 vendor service 一致), 主路由后面再双币换算

split_window(start_iso, end_iso) → (db_range, live_range)
   db_range:   [start_date, min(end_date, today-1)]   — 走 DB
   live_range: [max(start_date, today), end_date]     — 走 live (今天的还没入库)
今天的数据要走原 vendor_client (适配器层不动). 上层 split + merge.
"""
from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ingest.db import SessionLocal
from ingest.models import (
    UsageDailyTotal,
    VendorApiKeyUsageDaily,
    VendorIngestState,
    VendorMeta,
    VendorModelUsageDaily,
    VendorUsageDaily,
)

log = logging.getLogger("ingest.query")
CST = timezone(timedelta(hours=8))


def split_window(start_iso: str, end_iso: str) -> tuple[tuple[dt.date, dt.date] | None,
                                                          tuple[dt.date, dt.date] | None]:
    """把 ISO 时间窗切成 (DB 范围, live 范围). 边界自然日 CST.

    DB 走"昨天及之前" — 已入库;
    Live 走"今天" — 当天数据 cron 跑不到, 还得打上游.

    返回 (db_range, live_range), 任一为 None 表示该侧无需查询.
    """
    today = datetime.now(CST).date()
    start_date = datetime.fromisoformat(start_iso).date()
    end_date = datetime.fromisoformat(end_iso).date()

    # DB 上限 = min(end, yesterday)
    db_end = min(end_date, today - dt.timedelta(days=1))
    db_range = (start_date, db_end) if start_date <= db_end else None

    # Live 下限 = max(start, today)
    live_start = max(start_date, today)
    live_range = (live_start, end_date) if live_start <= end_date else None

    return db_range, live_range


def query_vendor_usage(vendor: dict, day_start: dt.date, day_end: dt.date, api_key: str | None = None) -> dict:
    """单 vendor × 日期范围 → vendor service shape ({total_cost, models, daily, currency, ...}).

    api_key=None (默认): 走 vendor_model_usage_daily + vendor_usage_daily (主表, 跟改造前一致).
    api_key='某key': 走 vendor_apikey_usage_daily (平行表) 按 key 过滤, models + daily 都从这张表 SUM.
    """
    with SessionLocal() as s:
        if api_key is None:
            # ─── 默认路径: 主表 (不变) ───
            rows = s.execute(
                select(
                    VendorModelUsageDaily.model,
                    func.sum(VendorModelUsageDaily.prompt_tokens),
                    func.sum(VendorModelUsageDaily.completion_tokens),
                    func.sum(VendorModelUsageDaily.cache_read_tokens),
                    func.sum(VendorModelUsageDaily.cache_write_tokens),
                    func.sum(VendorModelUsageDaily.total_tokens),
                    func.sum(VendorModelUsageDaily.request_count),
                    func.sum(VendorModelUsageDaily.image_count),
                    func.sum(VendorModelUsageDaily.cost_native),
                )
                .where(
                    VendorModelUsageDaily.vendor_id == vendor["id"],
                    VendorModelUsageDaily.usage_date >= day_start,
                    VendorModelUsageDaily.usage_date <= day_end,
                )
                .group_by(VendorModelUsageDaily.model)
            ).all()

            daily_rows = s.execute(
                select(
                    VendorUsageDaily.usage_date,
                    VendorUsageDaily.cost_native,
                    VendorUsageDaily.total_tokens,
                )
                .where(
                    VendorUsageDaily.vendor_id == vendor["id"],
                    VendorUsageDaily.usage_date >= day_start,
                    VendorUsageDaily.usage_date <= day_end,
                )
                .order_by(VendorUsageDaily.usage_date)
            ).all()
        else:
            # ─── api_key 筛选路径: 平行 key 表 ───
            rows = s.execute(
                select(
                    VendorApiKeyUsageDaily.model,
                    func.sum(VendorApiKeyUsageDaily.prompt_tokens),
                    func.sum(VendorApiKeyUsageDaily.completion_tokens),
                    func.sum(VendorApiKeyUsageDaily.cache_read_tokens),
                    func.sum(VendorApiKeyUsageDaily.cache_write_tokens),
                    func.sum(VendorApiKeyUsageDaily.total_tokens),
                    func.sum(VendorApiKeyUsageDaily.request_count),
                    func.sum(VendorApiKeyUsageDaily.image_count),
                    func.sum(VendorApiKeyUsageDaily.cost_native),
                )
                .where(
                    VendorApiKeyUsageDaily.vendor_id == vendor["id"],
                    VendorApiKeyUsageDaily.usage_date >= day_start,
                    VendorApiKeyUsageDaily.usage_date <= day_end,
                    VendorApiKeyUsageDaily.api_key == api_key,
                )
                .group_by(VendorApiKeyUsageDaily.model)
            ).all()

            daily_rows = s.execute(
                select(
                    VendorApiKeyUsageDaily.usage_date,
                    func.sum(VendorApiKeyUsageDaily.cost_native),
                    func.sum(VendorApiKeyUsageDaily.total_tokens),
                )
                .where(
                    VendorApiKeyUsageDaily.vendor_id == vendor["id"],
                    VendorApiKeyUsageDaily.usage_date >= day_start,
                    VendorApiKeyUsageDaily.usage_date <= day_end,
                    VendorApiKeyUsageDaily.api_key == api_key,
                )
                .group_by(VendorApiKeyUsageDaily.usage_date)
                .order_by(VendorApiKeyUsageDaily.usage_date)
            ).all()

        models = {}
        for r in rows:
            (mname, p, c, cr, cw, total, req, img, cost_n) = r
            models[mname] = {
                "prompt_tokens": int(p) if p is not None else None,
                "completion_tokens": int(c) if c is not None else None,
                "cache_tokens": int(cr) if cr is not None else None,
                "cache_read_tokens": int(cr) if cr is not None else None,
                "cache_write_tokens": int(cw) if cw is not None else None,
                "total_tokens": int(total) if total is not None else None,
                "total_count": int(req) if req is not None else None,
                "image_count": int(img) if img is not None else None,
                "total_cost": float(cost_n or 0),
            }

        daily = [
            {"date": d.isoformat(), "cost": float(c or 0),
             "total_tokens": int(t) if t is not None else None}
            for (d, c, t) in daily_rows
        ]
        total_native = float(sum(c for (_, c, _) in daily_rows) or 0)

    return {
        "total_cost": round(total_native, 6),
        "models": models,
        "daily": daily,
        "vendor_id": vendor["id"],
        "vendor_name": vendor.get("name") or vendor["id"],
        "currency": vendor.get("currency", "CNY"),
    }


def query_per_model_daily(
    vendor_id: str, day_start: dt.date, day_end: dt.date, models: list[str], api_key: str | None = None,
) -> dict[str, list[dict]]:
    """选中 N 个 model 在窗口内的 per-day cost / total_tokens — 给 VendorDetail
    多曲线趋势图 (用户从下拉勾了几个 model) 用.

    返回 shape: {model_name: [{date, cost, total_tokens}, ...] }
    没数据的 (date, model) 不填零, 让前端自然断点.

    api_key=None: 走主表 vendor_model_usage_daily; api_key='key': 走 vendor_apikey_usage_daily.
    """
    if not models:
        return {}
    table = VendorApiKeyUsageDaily if api_key is not None else VendorModelUsageDaily
    with SessionLocal() as s:
        query = select(
            table.model,
            table.usage_date,
            table.cost_native,
            table.total_tokens,
        ).where(
            table.vendor_id == vendor_id,
            table.usage_date >= day_start,
            table.usage_date <= day_end,
            table.model.in_(models),
        )

        if api_key is not None:
            query = query.where(table.api_key == api_key)

        rows = s.execute(
            query.order_by(table.model, table.usage_date)
        ).all()

    out: dict[str, list[dict]] = {m: [] for m in models}
    for mname, d, cost, tt in rows:
        out.setdefault(mname, []).append({
            "date": d.isoformat(),
            "cost": float(cost or 0),
            "total_tokens": int(tt) if tt is not None else None,
        })
    return out


def query_per_model_daily_overview(
    day_start: dt.date, day_end: dt.date, models: list[str],
) -> dict[str, list[dict]]:
    """跨 vendor 按 model name 聚合的 per-day cost / total_tokens —
    Overview 页多曲线趋势图用 (e.g. 'claude-opus-4-6' 跨 6 个 vendor SUM).

    返回 {model_name: [{date, cost_cny, cost_usd, total_tokens}, ...]}.
    cost 直接用 vendor_model_usage_daily.cost_cny / cost_usd (入库时已换币).
    """
    if not models:
        return {}
    with SessionLocal() as s:
        rows = s.execute(
            select(
                VendorModelUsageDaily.model,
                VendorModelUsageDaily.usage_date,
                func.sum(VendorModelUsageDaily.cost_cny),
                func.sum(VendorModelUsageDaily.cost_usd),
                func.sum(VendorModelUsageDaily.total_tokens),
            )
            .where(
                VendorModelUsageDaily.usage_date >= day_start,
                VendorModelUsageDaily.usage_date <= day_end,
                VendorModelUsageDaily.model.in_(models),
            )
            .group_by(VendorModelUsageDaily.model, VendorModelUsageDaily.usage_date)
            .order_by(VendorModelUsageDaily.model, VendorModelUsageDaily.usage_date)
        ).all()

    out: dict[str, list[dict]] = {m: [] for m in models}
    for mname, d, cny, usd, tt in rows:
        out.setdefault(mname, []).append({
            "date": d.isoformat(),
            "cost_cny": float(cny or 0),
            "cost_usd": float(usd or 0),
            "total_tokens": int(tt) if tt is not None else None,
        })
    return out


def query_overview_vendors(day_start: dt.date, day_end: dt.date) -> list[dict]:
    """所有 enabled vendor 的日聚合 → [vendor service shape, ...] (一项/vendor).

    一次 SQL 拿 vendor_usage_daily + vendor_model_usage_daily (JOIN vendor_meta), 按 vendor 分组。
    """
    with SessionLocal() as s:
        # vendor 级 daily (用于 daily 拼接 + total)
        vendor_rows = s.execute(
            select(
                VendorMeta.vendor_id,
                VendorMeta.display_name,
                VendorMeta.native_currency,
                VendorUsageDaily.usage_date,
                VendorUsageDaily.cost_native,
                VendorUsageDaily.total_tokens,
            )
            .join(VendorUsageDaily, VendorUsageDaily.vendor_id == VendorMeta.vendor_id)
            .where(
                VendorMeta.enabled.is_(True),
                VendorUsageDaily.usage_date >= day_start,
                VendorUsageDaily.usage_date <= day_end,
            )
            .order_by(VendorMeta.vendor_id, VendorUsageDaily.usage_date)
        ).all()

        # vendor × model 聚合
        model_rows = s.execute(
            select(
                VendorModelUsageDaily.vendor_id,
                VendorModelUsageDaily.model,
                func.sum(VendorModelUsageDaily.prompt_tokens),
                func.sum(VendorModelUsageDaily.completion_tokens),
                func.sum(VendorModelUsageDaily.cache_read_tokens),
                func.sum(VendorModelUsageDaily.cache_write_tokens),
                func.sum(VendorModelUsageDaily.total_tokens),
                func.sum(VendorModelUsageDaily.request_count),
                func.sum(VendorModelUsageDaily.image_count),
                func.sum(VendorModelUsageDaily.cost_native),
            )
            .join(VendorMeta, VendorMeta.vendor_id == VendorModelUsageDaily.vendor_id)
            .where(
                VendorMeta.enabled.is_(True),
                VendorModelUsageDaily.usage_date >= day_start,
                VendorModelUsageDaily.usage_date <= day_end,
            )
            .group_by(VendorModelUsageDaily.vendor_id, VendorModelUsageDaily.model)
        ).all()

    # 按 vendor 聚合
    vendor_info: dict[str, dict] = {}
    vendor_daily: dict[str, list[tuple[dt.date, float, int | None]]] = defaultdict(list)
    vendor_total: dict[str, float] = defaultdict(float)
    for vid, dn, nc, d, cost, tt in vendor_rows:
        vendor_info[vid] = {"display_name": dn, "native_currency": nc}
        vendor_daily[vid].append((d, float(cost or 0), int(tt) if tt is not None else None))
        vendor_total[vid] += float(cost or 0)

    vendor_models: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in model_rows:
        (vid, mname, p, c, cr, cw, total, req, img, cost_n) = r
        vendor_models[vid][mname] = {
            "prompt_tokens": int(p) if p is not None else None,
            "completion_tokens": int(c) if c is not None else None,
            "cache_tokens": int(cr) if cr is not None else None,
            "cache_read_tokens": int(cr) if cr is not None else None,
            "cache_write_tokens": int(cw) if cw is not None else None,
            "total_tokens": int(total) if total is not None else None,
            "total_count": int(req) if req is not None else None,
            "image_count": int(img) if img is not None else None,
            "total_cost": float(cost_n or 0),
        }

    # 输出
    out = []
    for vid, info in vendor_info.items():
        out.append({
            "total_cost": round(vendor_total[vid], 6),
            "models": vendor_models[vid],
            "daily": [{"date": d.isoformat(), "cost": round(c, 6), "total_tokens": tt}
                      for (d, c, tt) in vendor_daily[vid]],
            "vendor_id": vid,
            "vendor_name": info["display_name"],
            "currency": info["native_currency"],
        })
    return out


def merge_results(db_result: dict | None, live_result: dict | None) -> dict | None:
    """合并 DB 历史 + Live 今天的结果. 两者 shape 一致 (vendor service shape).

    None 表示该来源无数据, 全 None → 返 None.
    """
    if db_result is None and live_result is None:
        return None
    if db_result is None:
        return live_result
    if live_result is None:
        return db_result

    # ─── 同 vendor, 两段时间窗 → 累加 ───
    out = {
        "vendor_id": db_result["vendor_id"],
        "vendor_name": db_result.get("vendor_name") or live_result.get("vendor_name"),
        "currency": db_result.get("currency") or live_result.get("currency"),
        "total_cost": round(db_result["total_cost"] + live_result["total_cost"], 6),
    }
    # models — 按 model 字段累加
    merged_models: dict[str, dict] = {}
    for src in (db_result, live_result):
        for mname, m in src.get("models", {}).items():
            if mname in merged_models:
                t = merged_models[mname]
                for k in ("prompt_tokens", "completion_tokens", "cache_tokens",
                          "cache_read_tokens", "cache_write_tokens", "total_tokens",
                          "total_count", "image_count"):
                    # 保留 None — 两边都 None 才 None, 否则当 0
                    a, b = t.get(k), m.get(k)
                    if a is None and b is None:
                        t[k] = None
                    else:
                        t[k] = (a or 0) + (b or 0)
                t["total_cost"] = round((t.get("total_cost") or 0) + (m.get("total_cost") or 0), 6)
            else:
                merged_models[mname] = dict(m)
    out["models"] = merged_models

    # daily — 按 date 字段 dict 合并 (有些上游 bucket 跨 UTC 日边界会同一天两边都有)
    daily_map: dict[str, dict] = {}
    for src in (db_result, live_result):
        for d in src.get("daily", []):
            entry = daily_map.setdefault(d["date"], {"cost": 0.0, "total_tokens": None})
            entry["cost"] += float(d["cost"] or 0)
            tt = d.get("total_tokens")
            if tt is not None:
                entry["total_tokens"] = (entry["total_tokens"] or 0) + tt
    out["daily"] = [{"date": k, "cost": round(v["cost"], 6), "total_tokens": v["total_tokens"]}
                    for k, v in sorted(daily_map.items())]
    return out


def get_freshness() -> dict:
    """每个 vendor 在 vendor_usage_daily 里的最新日期. 给前端显示"最近同步".

    不直接读 vendor_ingest_state.last_ingested_date — 因为 backfill 从昨天往回填,
    state 会被无脑覆盖成最旧的天. 直接从 fact 表 MAX(usage_date) 出准.
    """
    with SessionLocal() as s:
        rows = s.execute(
            select(
                VendorUsageDaily.vendor_id,
                func.max(VendorUsageDaily.usage_date),
            ).group_by(VendorUsageDaily.vendor_id)
        ).all()
        return {vid: d for vid, d in rows}


def get_recently_failed_vendors(within_hours: int = 48) -> list[dict]:
    """每 vendor 拿"最近一次 run", 是 failed 就标. Overview 顶部"vendor 需要关注"提示用.

    判定: 最近一次 run (按 started_at desc) status='failed' 且发生在 within_hours 内.
    返回 [{vendor_id, vendor_name, error_msg, finished_at, run_id}, ...]

    包含 session 过期 / cookie 失效 / 上游 502 等所有 cron 抓不到的情况.
    比单纯查 session 文件准 (有的 vendor cookie 过期文件还在).
    """
    from sqlalchemy import desc
    from ingest.models import VendorIngestRun

    with SessionLocal() as s:
        # 每 vendor 拿最近一次 run (DISTINCT ON 走 PG 特性, 一次 SQL)
        rows = s.execute(text("""
            SELECT DISTINCT ON (r.vendor_id)
                r.vendor_id, r.id, r.status, r.error_msg, r.started_at, r.finished_at,
                COALESCE(m.display_name, r.vendor_id) AS display_name
            FROM vendor_ingest_run r
            LEFT JOIN vendor_meta m ON m.vendor_id = r.vendor_id
            ORDER BY r.vendor_id, r.started_at DESC
        """)).all()

    cutoff = datetime.now(timezone.utc) - timedelta(hours=within_hours)
    out = []
    for vid, run_id, status, error_msg, started_at, finished_at, display_name in rows:
        if status != "failed":
            continue
        # 时间戳没时区就当 UTC (PG TIMESTAMPTZ 默认带, 防御一下)
        ts = finished_at or started_at
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts and ts < cutoff:
            continue
        out.append({
            "vendor_id": vid,
            "vendor_name": display_name,
            "run_id": run_id,
            "error_msg": (error_msg or "")[:200],  # 截短, 别把 trace 全推前端
            "finished_at": ts.isoformat() if ts else None,
        })
    return out


def get_missing_days_in_window(day_start: dt.date, day_end: dt.date) -> dict[str, list[str]]:
    """查询窗口内 vendor 缺哪几天数据 — 排除今天 (今天数据走 live 拼接, 不在 DB).

    给 Overview 顶部"未入库"提示用. 返回 {vendor_id: [缺的天 ISO 字符串, ...]}.

    精确语义 — 只标"应该有但没有"的天:
    - vendor 在窗口内**首次有真数据** (cost>0 或 total_tokens>0) 到**最后有真数据**之间, 缺的天才算 missing
    - vendor 整窗口都没真数据 → 该 vendor 在那段时间根本没被调用过, 不标 (避免对稀疏 vendor 误报)
    - zero 行 (total=null cost=0 n_models=0) 算"扫过没数据", 不标 missing
    """
    today = datetime.now(CST).date()
    # 今天数据走 live, 不算 stale
    effective_end = min(day_end, today - dt.timedelta(days=1))
    if effective_end < day_start:
        return {}

    with SessionLocal() as s:
        # 1. enabled vendor 列表
        vendors = s.execute(
            select(VendorMeta.vendor_id).where(VendorMeta.enabled.is_(True))
        ).scalars().all()

        # 2. 窗口内 (vendor, date) 存在情况 + 是否有真实数据 (cost>0 or total_tokens>0)
        rows = s.execute(
            select(
                VendorUsageDaily.vendor_id,
                VendorUsageDaily.usage_date,
                VendorUsageDaily.cost_native,
                VendorUsageDaily.total_tokens,
            )
            .where(VendorUsageDaily.usage_date >= day_start,
                   VendorUsageDaily.usage_date <= effective_end)
        ).all()

    existing_set: set[tuple[str, dt.date]] = set()
    real_data_dates: dict[str, list[dt.date]] = defaultdict(list)
    for vid, d, cost, tt in rows:
        existing_set.add((vid, d))
        if (cost and float(cost) > 0) or (tt is not None and tt > 0):
            real_data_dates[vid].append(d)

    # 3. 算每个 vendor 缺的天 (只在 [first_real, last_real] 范围内)
    missing: dict[str, list[str]] = {}
    for vid in vendors:
        real = real_data_dates.get(vid)
        if not real:
            # 整窗口都无真实调用 — 不标 (vendor 在那段时间没用过)
            continue
        first_real, last_real = min(real), max(real)
        span_days = (last_real - first_real).days + 1
        check_days = [first_real + dt.timedelta(days=i) for i in range(span_days)]
        gaps = [d.isoformat() for d in check_days if (vid, d) not in existing_set]
        if gaps:
            missing[vid] = gaps
    return missing


def get_data_range() -> dict[str, tuple]:
    """{vendor_id: (earliest, latest)}. 前端 vendor 详情页"数据范围: 最早 ~ 最新"用.
    跟 get_freshness 一次 SQL 就能拿到, 单独函数让调用方语义清."""
    with SessionLocal() as s:
        rows = s.execute(
            select(
                VendorUsageDaily.vendor_id,
                func.min(VendorUsageDaily.usage_date),
                func.max(VendorUsageDaily.usage_date),
            ).group_by(VendorUsageDaily.vendor_id)
        ).all()
        return {vid: (mn, mx) for vid, mn, mx in rows}


def get_running_runs(min_age_hours: float | None = None) -> list[dict]:
    """当前 status='running' 的 run 列表 — agent 巡检判僵尸 / 触发前防并发用.

    min_age_hours 给定时只返回跑了超过该时长的 (僵尸判定); None 返回全部 running.
    返回 [{run_id, vendor_id, started_at, age_hours}], age_hours 四舍五入到 0.1.
    """
    from ingest.models import VendorIngestRun

    with SessionLocal() as s:
        rows = s.execute(
            select(VendorIngestRun.id, VendorIngestRun.vendor_id,
                   VendorIngestRun.started_at, VendorIngestRun.trigger)
            .where(VendorIngestRun.status == "running")
            .order_by(VendorIngestRun.started_at)
        ).all()

    now = datetime.now(timezone.utc)
    out = []
    for run_id, vid, started_at, trigger in rows:
        ts = started_at if started_at.tzinfo else started_at.replace(tzinfo=timezone.utc)
        age_h = (now - ts).total_seconds() / 3600
        if min_age_hours is not None and age_h < min_age_hours:
            continue
        out.append({
            "run_id": run_id,
            "vendor_id": vid,
            "trigger": trigger,
            "started_at": ts.isoformat(),
            "age_hours": round(age_h, 1),
        })
    return out


def get_run_detail(run_id: int) -> dict | None:
    """单个 run 的完整信息 — agent 诊断下钻用, error_msg **不截断** (DB 里有全 traceback).

    跟 get_recently_failed_vendors 的 200 字符截断不同, 这是给 LLM 看完整报错用的.
    """
    from ingest.models import VendorIngestRun

    with SessionLocal() as s:
        run = s.get(VendorIngestRun, run_id)
        if not run:
            return None
        return {
            "id": run.id,
            "vendor_id": run.vendor_id,
            "window_start": run.window_start.isoformat() if run.window_start else None,
            "window_end": run.window_end.isoformat() if run.window_end else None,
            "trigger": run.trigger,
            "status": run.status,
            "attempt": run.attempt,
            "rows_upserted": run.rows_upserted,
            "error_msg": run.error_msg,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        }


def get_vendor_runs(vendor_id: str, limit: int = 5) -> list[dict]:
    """某 vendor 最近 N 次 run — agent 诊断"是偶发还是连续失败"用."""
    from ingest.models import VendorIngestRun
    from sqlalchemy import desc

    with SessionLocal() as s:
        rows = s.execute(
            select(VendorIngestRun)
            .where(VendorIngestRun.vendor_id == vendor_id)
            .order_by(desc(VendorIngestRun.started_at))
            .limit(limit)
        ).scalars().all()
        return [{
            "id": r.id, "status": r.status, "trigger": r.trigger, "attempt": r.attempt,
            "window_start": r.window_start.isoformat() if r.window_start else None,
            "window_end": r.window_end.isoformat() if r.window_end else None,
            "rows_upserted": r.rows_upserted,
            "error_msg": (r.error_msg or "")[:500],
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        } for r in rows]


def get_success_runs_with_hints(within_hours: int = 26) -> list[dict]:
    """最近一次 run 是 success 但带 error_msg 提示 (info_msg 路径) — "黄色"半健康信号.

    语义跟 get_recently_failed_vendors 对齐: 先按 vendor 拿**最新一次 run** (不看状态),
    再过滤"success 且有提示". 这样 23:00 recheck / 后续 cron 已修好的旧 hint 不会
    持续误报 (旧实现先过滤再 DISTINCT ON, 干净的新 run 无法覆盖老的 hint 行).

    场景: apevon stat-fallback (cost 已入但 model 拆分缺) / 上游空, run 不算失败
    但值得 agent 看一眼.
    """
    from ingest.models import VendorIngestRun

    cutoff = datetime.now(timezone.utc) - timedelta(hours=within_hours)
    with SessionLocal() as s:
        rows = s.execute(text("""
            WITH latest AS (
                SELECT DISTINCT ON (r.vendor_id)
                    r.id, r.vendor_id, r.status, r.error_msg, r.rows_upserted,
                    r.finished_at, r.window_start, r.window_end
                FROM vendor_ingest_run r
                WHERE COALESCE(r.finished_at, r.started_at) >= :cutoff
                ORDER BY r.vendor_id, r.started_at DESC
            )
            SELECT id, vendor_id, error_msg, rows_upserted,
                   finished_at, window_start, window_end
            FROM latest
            WHERE status = 'success' AND error_msg IS NOT NULL
        """), {"cutoff": cutoff}).all()
        return [{
            "run_id": rid, "vendor_id": vid,
            "error_msg": (msg or "")[:400],
            "rows_upserted": rows_upserted,
            "window_start": ws.isoformat() if ws else None,
            "window_end": we.isoformat() if we else None,
            "finished_at": fin.isoformat() if fin else None,
        } for rid, vid, msg, rows_upserted, fin, ws, we in rows]


def slow_path_vendor_ids() -> set[str]:
    """走"慢路径补 model 拆分"模式的 vendor — adapter has_stat_fallback 或 blueshirt/nulls.

    从 adapter 类属性读, 不硬编码 (跟 router /state 同一判定).
    """
    from ingest.adapters import ADAPTERS
    return {
        vid for vid, cls in ADAPTERS.items()
        if getattr(cls, "has_stat_fallback", False) or vid in ("blueshirt", "nulls")
    }


def get_vendor_api_keys(vendor_id: str, day_start: dt.date | None = None, day_end: dt.date | None = None) -> list[str]:
    """获取 vendor 在指定时间范围内使用过的所有 API keys (去重排序).

    查 vendor_apikey_usage_daily (只有上游能按 key 拆分的 vendor 才有数据).
    day_start/day_end: 可选, 限制时间范围; None = 查全部历史.
    返回 api_key 列表, 按字母序.
    """
    with SessionLocal() as s:
        query = select(VendorApiKeyUsageDaily.api_key).where(
            VendorApiKeyUsageDaily.vendor_id == vendor_id,
        ).distinct()

        if day_start is not None:
            query = query.where(VendorApiKeyUsageDaily.usage_date >= day_start)
        if day_end is not None:
            query = query.where(VendorApiKeyUsageDaily.usage_date <= day_end)

        rows = s.execute(query.order_by(VendorApiKeyUsageDaily.api_key)).scalars().all()
        return list(rows)


def query_global_daily(day_start: dt.date, day_end: dt.date) -> list[dict]:
    """全公司日趋势 — usage_daily_total. 一行/天, Overview 顶部图直接渲染."""
    with SessionLocal() as s:
        rows = s.execute(
            select(
                UsageDailyTotal.usage_date,
                UsageDailyTotal.cost_usd,
                UsageDailyTotal.cost_cny,
                UsageDailyTotal.total_tokens,
                UsageDailyTotal.request_count,
                UsageDailyTotal.is_complete,
                UsageDailyTotal.complete_vendor_count,
                UsageDailyTotal.expected_vendor_count,
            )
            .where(
                UsageDailyTotal.usage_date >= day_start,
                UsageDailyTotal.usage_date <= day_end,
            )
            .order_by(UsageDailyTotal.usage_date)
        ).all()
    return [
        {
            "date": r[0].isoformat(),
            "cost_usd": float(r[1] or 0),
            "cost_cny": float(r[2] or 0),
            "total_tokens": int(r[3] or 0),
            "request_count": int(r[4] or 0),
            "is_complete": bool(r[5]),
            "complete_vendor_count": int(r[6] or 0),
            "expected_vendor_count": int(r[7] or 0),
        }
        for r in rows
    ]
