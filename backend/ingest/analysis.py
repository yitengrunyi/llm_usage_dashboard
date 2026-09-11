"""横向分析 endpoint — 不做决策类比价, 只看异常 + 巡检 + 业务洞察.

endpoint:
- /api/analysis/wow-changes        环比异常榜 (本窗口 vs 上一个等长窗口)
- /api/analysis/budget             月预算进度 + 末月线性外推
- /api/analysis/cache-hit          cache 命中率排行 (找出该开 cache 的 model)
- /api/analysis/data-health        最近 N 天数据完整度巡检
- /api/analysis/vendor-share       vendor 份额变化
- /api/analysis/model-pareto       model 集中度帕累托 top 15
- /api/analysis/cache-by-series    top N (model, vendor) 按 series 聚合, 看同系不同 vendor cache 率
"""
from __future__ import annotations

import datetime as dt
import re
from datetime import timedelta, timezone

from fastapi import APIRouter, Query
from sqlalchemy import text

from ingest.db import SessionLocal

CST = timezone(timedelta(hours=8))
router = APIRouter(prefix="/api/analysis", tags=["analysis"])


def _today_cst() -> dt.date:
    return dt.datetime.now(CST).date()


# series 提取规则 — 同名系列把 model 累加 (claude-opus-4-6 + claude-sonnet-4 = claude 系).
# 规则按出现顺序匹配, 命中即返. 兜底 "其他".
# 数据驱动: 实测 top 25 (model, vendor) 出现的 series 覆盖 ~99% cost.
# o1/o3/o4 算 gpt 系 — OpenAI reasoning 系列, 业务上同源.
_SERIES_RULES: list[tuple[str, str]] = [
    ("claude", "claude"),
    ("gpt", "gpt"),
    ("o1", "gpt"),
    ("o3", "gpt"),
    ("o4", "gpt"),
    ("gemini", "gemini"),
    ("kimi", "kimi"),
    ("k2", "kimi"),
    ("doubao", "doubao"),
    ("deepseek", "deepseek"),
    ("glm", "glm"),
    ("qwen", "qwen"),
    ("grok", "grok"),
    ("llama", "llama"),
]


def _extract_series(model: str | None) -> str:
    """从 normalized model 名提取 series — claude-opus-4-6 → claude, gpt-5-4 → gpt 等.

    vendor_model_usage_daily.model 落库时多数已走 utils.normalize_model_name, 无 vendor 前缀.
    保留宽松匹配 (re.match, 任意位置 word-start), 兜底 "其他" 防止脏数据漏分类.
    """
    if not model:
        return "其他"
    s = model.lower().strip()
    for prefix, series in _SERIES_RULES:
        # \b 边界匹配避免 "gptx" 误进 gpt 系
        if re.match(rf"^{re.escape(prefix)}([-_.\d]|$)", s):
            return series
    return "其他"


# ───────── 1. 环比异常榜 ─────────
@router.get("/wow-changes")
def wow_changes(
    window_days: int = Query(7, ge=1, le=30, description="对比窗口长度 (天)"),
    min_cost_cny: float = Query(100, description="本期+上期 cost 之和低于此值不显示"),
    top_n: int = Query(10, ge=1, le=50),
):
    """对比 [今天-N..今天-1] vs [今天-2N..今天-N-1] 同长窗口的 (vendor, model) 涨跌.

    返回涨幅 / 跌幅 各 top_n 行, 按变化绝对额排序 (不是百分比, 避免小基数百分比假大).
    """
    today = _today_cst()
    curr_end = today - timedelta(days=1)
    curr_start = curr_end - timedelta(days=window_days - 1)
    prev_end = curr_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=window_days - 1)

    sql = """
        WITH curr AS (
            SELECT vendor_id, model,
                   SUM(cost_cny) AS cost,
                   SUM(request_count) AS req
            FROM vendor_model_usage_daily
            WHERE usage_date BETWEEN :curr_start AND :curr_end
            GROUP BY 1, 2
        ),
        prev AS (
            SELECT vendor_id, model,
                   SUM(cost_cny) AS cost,
                   SUM(request_count) AS req
            FROM vendor_model_usage_daily
            WHERE usage_date BETWEEN :prev_start AND :prev_end
            GROUP BY 1, 2
        ),
        joined AS (
            SELECT
                COALESCE(c.vendor_id, p.vendor_id) AS vendor_id,
                COALESCE(c.model, p.model) AS model,
                COALESCE(c.cost, 0) AS curr_cost,
                COALESCE(p.cost, 0) AS prev_cost,
                COALESCE(c.cost, 0) - COALESCE(p.cost, 0) AS delta_cost,
                COALESCE(c.req, 0) AS curr_req,
                COALESCE(p.req, 0) AS prev_req
            FROM curr c FULL OUTER JOIN prev p USING (vendor_id, model)
        )
        SELECT j.*, COALESCE(m.display_name, j.vendor_id) AS vendor_name
        FROM joined j
        LEFT JOIN vendor_meta m ON m.vendor_id = j.vendor_id
        WHERE (curr_cost + prev_cost) >= :min_cost
        ORDER BY ABS(delta_cost) DESC
    """
    params = {
        "curr_start": curr_start, "curr_end": curr_end,
        "prev_start": prev_start, "prev_end": prev_end,
        "min_cost": min_cost_cny,
    }
    with SessionLocal() as s:
        rows = s.execute(text(sql), params).all()

    def _to_dict(r):
        prev = float(r.prev_cost or 0)
        curr = float(r.curr_cost or 0)
        return {
            "vendor_id": r.vendor_id,
            "vendor_name": r.vendor_name,
            "model": r.model,
            "curr_cost": round(curr, 2),
            "prev_cost": round(prev, 2),
            "delta_cost": round(curr - prev, 2),
            "pct": round((curr - prev) / prev * 100, 1) if prev > 0 else None,
            "curr_req": int(r.curr_req or 0),
            "prev_req": int(r.prev_req or 0),
        }
    items = [_to_dict(r) for r in rows]
    risers = [x for x in items if x["delta_cost"] > 0][:top_n]
    fallers = sorted([x for x in items if x["delta_cost"] < 0],
                     key=lambda x: x["delta_cost"])[:top_n]
    return {
        "curr_window": {"start": curr_start.isoformat(), "end": curr_end.isoformat()},
        "prev_window": {"start": prev_start.isoformat(), "end": prev_end.isoformat()},
        "risers": risers,
        "fallers": fallers,
    }


# ───────── 2. 月预算进度 + 末月预测 (线性回归外推) ─────────
@router.get("/budget")
def budget():
    """本月已花 + 上月同期 + 上月全月 + 末月预测.

    预测算法:
    - 本月累计 < 7 天: 不给预测 (sample 太少, 任何异常日都会让预测飞 ±50%).
      返 projected_this_month_cny = None, 前端显示"数据不足".
    - 累计 >= 7 天: 用最近 14 天 (跨月衔接) 做线性回归 (最小二乘) 拟合 daily cost,
      斜率 slope 反映趋势 (正=上升, 负=下降). 用回归外推剩余每天 cost,
      累加本月已花得末月预测. 比纯日均更准 ~10-20%.

    都按 CNY (后端 cost_cny 已落库时刻折算, 历史不漂移).
    """
    today = _today_cst()
    month_start = today.replace(day=1)
    # 上月
    last_month_end = month_start - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    last_month_same_day_end = min(last_month_start.replace(day=today.day) - timedelta(days=1)
                                  if today.day > 1 else last_month_start,
                                  last_month_end)
    # 月末
    if today.month == 12:
        next_month_start = today.replace(year=today.year + 1, month=1, day=1)
    else:
        next_month_start = today.replace(month=today.month + 1, day=1)
    this_month_total_days = (next_month_start - month_start).days

    # 本月已过去几天 (不含今天, 今天 live 数据不全)
    days_elapsed = (today - month_start).days  # 6/1 时 = 0

    # 回归窗口: 今天往前 14 天 (跨上月衔接), 避免月初 sample 不足
    REGRESS_DAYS = 14
    regress_start = today - timedelta(days=REGRESS_DAYS)

    sql = """
        SELECT
            (SELECT COALESCE(SUM(cost_cny), 0) FROM usage_daily_total
             WHERE usage_date >= :month_start AND usage_date < :today) AS this_to_date,
            (SELECT COALESCE(SUM(cost_cny), 0) FROM usage_daily_total
             WHERE usage_date >= :last_month_start AND usage_date <= :last_month_same_day_end)
                AS last_same_period,
            (SELECT COALESCE(SUM(cost_cny), 0) FROM usage_daily_total
             WHERE usage_date >= :last_month_start AND usage_date <= :last_month_end)
                AS last_total
    """
    params = {
        "month_start": month_start, "today": today,
        "last_month_start": last_month_start,
        "last_month_same_day_end": last_month_same_day_end,
        "last_month_end": last_month_end,
    }
    with SessionLocal() as s:
        row = s.execute(text(sql), params).first()
        # 拉回归窗口的日 cost 用来线性拟合
        daily_rows = s.execute(text("""
            SELECT usage_date, cost_cny FROM usage_daily_total
            WHERE usage_date >= :start AND usage_date < :today
            ORDER BY usage_date
        """), {"start": regress_start, "today": today}).all()

    this_to_date = float(row.this_to_date or 0)
    last_same = float(row.last_same_period or 0)
    last_total = float(row.last_total or 0)

    # 预测
    projected = None
    daily_avg = None
    slope = None
    forecast_method = None
    MIN_DAYS_FOR_FORECAST = 7

    if days_elapsed >= MIN_DAYS_FOR_FORECAST:
        daily_avg = this_to_date / days_elapsed
        # 线性回归 — 手写最小二乘, 避免 numpy 依赖
        # x = 天序号 0..n-1, y = 日 cost
        n = len(daily_rows)
        xs = list(range(n))
        ys = [float(r.cost_cny or 0) for r in daily_rows]
        if n >= 2:
            mx = sum(xs) / n
            my = sum(ys) / n
            num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
            den = sum((x - mx) ** 2 for x in xs)
            slope = num / den if den else 0.0
            intercept = my - slope * mx
            # 外推今天起到月末的剩余天数, x 从 n 开始 (回归窗口之后)
            remaining_days = (next_month_start - today).days  # 含今天
            forecast = sum(max(0.0, intercept + slope * (n + i))
                           for i in range(remaining_days))
            projected = round(this_to_date + forecast, 2)
            forecast_method = f"线性回归 (近 {n} 天, slope=¥{slope:+,.0f}/天)"
        else:
            # 极端兜底: 回归窗口连 2 天数据都没有
            projected = round(daily_avg * this_month_total_days, 2)
            forecast_method = f"日均外推 (回归窗口仅 {n} 天数据)"

    return {
        "today": today.isoformat(),
        "month_start": month_start.isoformat(),
        "days_elapsed": days_elapsed,
        "days_in_month": this_month_total_days,
        "this_month_to_date_cny": round(this_to_date, 2),
        "last_month_same_period_cny": round(last_same, 2),
        "last_month_total_cny": round(last_total, 2),
        "projected_this_month_cny": projected,  # None 表示数据不足 (本月 < 7 天)
        "wow_pct_vs_last_same_period": round((this_to_date - last_same) / last_same * 100, 1)
            if last_same > 0 else None,
        "projected_vs_last_total_pct": round((projected - last_total) / last_total * 100, 1)
            if projected is not None and last_total > 0 else None,
        "daily_avg_cny": round(daily_avg, 2) if daily_avg is not None else None,
        "forecast_method": forecast_method,  # 前端显示用 (e.g. "线性回归 (近 14 天, slope=+¥123/天)")
        "trend_slope_cny_per_day": round(slope, 2) if slope is not None else None,
    }


# ───────── 3. cache 命中率排行 ─────────
@router.get("/cache-hit")
def cache_hit_ranking(
    days: int = Query(30, ge=1, le=180, description="回溯天数"),
    min_input: int = Query(100_000_000, description="输入 token 总数至少 1 亿才进榜 (过滤小量噪声)"),
):
    """各 (vendor, model) 的 cache_read / 总输入 比例排行.

    分母 (总输入) 用行级 CASE WHEN 推算, 兼容两种 vendor 上游惯例:
    - pack_input 系 (openai/ucloud/volcengine etc): DB.prompt_tokens 已含 cache,
      即 prompt >= cache_read + cache_write → 直接用 prompt 作为输入总量
    - blueshirt 系: DB.prompt_tokens 来自 raw log 单独字段, 不含 cache,
      此时 prompt < cache_read + cache_write → 加上 cache 部分得真实输入总量

    判定方式: prompt >= cache_r+cache_w → 含 cache; 否则不含. 行级判定 SUM 出来准确.
    """
    today = _today_cst()
    start = today - timedelta(days=days)
    sql = """
        WITH per_row AS (
            SELECT
                u.vendor_id, u.model, u.usage_date,
                u.cache_read_tokens, u.cost_cny,
                CASE
                  WHEN COALESCE(u.prompt_tokens, 0) >=
                       COALESCE(u.cache_read_tokens, 0) + COALESCE(u.cache_write_tokens, 0)
                    THEN COALESCE(u.prompt_tokens, 0)
                  ELSE COALESCE(u.prompt_tokens, 0)
                       + COALESCE(u.cache_read_tokens, 0)
                       + COALESCE(u.cache_write_tokens, 0)
                END AS input_calc
            FROM vendor_model_usage_daily u
            WHERE u.usage_date BETWEEN :start AND :end
              AND u.cache_read_tokens IS NOT NULL
              AND u.total_tokens IS NOT NULL
              AND u.total_tokens > 0
        )
        SELECT
            p.vendor_id,
            COALESCE(m.display_name, p.vendor_id) AS vendor_name,
            p.model,
            SUM(p.input_calc) AS input_total,
            SUM(p.cache_read_tokens) AS cache_read,
            SUM(p.cost_cny) AS cost_cny,
            SUM(p.cache_read_tokens)::float / NULLIF(SUM(p.input_calc), 0) AS hit_rate
        FROM per_row p
        LEFT JOIN vendor_meta m ON m.vendor_id = p.vendor_id
        GROUP BY 1, 2, 3
        HAVING SUM(p.input_calc) >= :min_input
        ORDER BY SUM(p.cache_read_tokens)::float / NULLIF(SUM(p.input_calc), 0) DESC NULLS LAST
        LIMIT 50
    """
    params = {"start": start, "end": today - timedelta(days=1), "min_input": min_input}
    with SessionLocal() as s:
        rows = s.execute(text(sql), params).all()
    items = [{
        "vendor_id": r.vendor_id,
        "vendor_name": r.vendor_name,
        "model": r.model,
        "input_tokens": int(r.input_total or 0),       # 总输入 = total - completion (含 cache)
        "cache_read_tokens": int(r.cache_read or 0),
        "cost_cny": round(float(r.cost_cny or 0), 2),
        "hit_rate": round(float(r.hit_rate or 0) * 100, 1),
    } for r in rows]
    return {
        "window": {"start": start.isoformat(), "end": (today - timedelta(days=1)).isoformat()},
        "items": items,
    }


# ───────── 4. 数据完整度巡检 ─────────
@router.get("/data-health")
def data_health(days: int = Query(14, ge=1, le=60)):
    """最近 N 天每天的 vendor 入库完整度 + 缺哪些 vendor.

    依赖 usage_daily_total.complete_vendor_count / expected_vendor_count (入库时 trigger 更新).
    """
    today = _today_cst()
    start = today - timedelta(days=days)

    sql_daily = """
        SELECT usage_date, complete_vendor_count, expected_vendor_count, is_complete
        FROM usage_daily_total
        WHERE usage_date BETWEEN :start AND :end
        ORDER BY usage_date DESC
    """
    # 哪些 (vendor, 日期) 缺了 — 不是按 ingest_run, 是真实"那天没有这家数据行"
    sql_missing = """
        WITH days AS (
            SELECT generate_series(CAST(:start AS date), CAST(:end AS date), '1 day') AS d
        ),
        enabled_vendors AS (
            SELECT vendor_id, display_name FROM vendor_meta WHERE enabled = TRUE
        )
        SELECT CAST(d.d AS date) AS usage_date, v.vendor_id,
               COALESCE(v.display_name, v.vendor_id) AS vendor_name
        FROM days d
        CROSS JOIN enabled_vendors v
        LEFT JOIN vendor_usage_daily vud
            ON vud.vendor_id = v.vendor_id AND vud.usage_date = CAST(d.d AS date)
        WHERE vud.vendor_id IS NULL
        ORDER BY d.d DESC, v.display_name
    """
    params = {"start": start, "end": today - timedelta(days=1)}
    with SessionLocal() as s:
        daily = s.execute(text(sql_daily), params).all()
        missing = s.execute(text(sql_missing), params).all()

    return {
        "window": {"start": start.isoformat(), "end": (today - timedelta(days=1)).isoformat()},
        "daily": [{
            "date": r.usage_date.isoformat(),
            "complete": int(r.complete_vendor_count or 0),
            "expected": int(r.expected_vendor_count or 0),
            "is_complete": bool(r.is_complete),
        } for r in daily],
        "missing": [{
            "date": r.usage_date.isoformat(),
            "vendor_id": r.vendor_id,
            "vendor_name": r.vendor_name,
        } for r in missing],
    }


# ───────── 5. Vendor 份额变化 (本月 vs 上月同期) ─────────
@router.get("/vendor-share")
def vendor_share():
    """本月 1 日 ~ 昨天 vs 上月同期, 每个 vendor 占总成本的份额对比.

    返回份额百分比 + 百分点变化 (delta_pct_pt, 不是百分比变化是绝对差值).
    """
    today = _today_cst()
    curr_start = today.replace(day=1)
    curr_end = today - timedelta(days=1)
    # 上月同期 = 上月 1 日 ~ 上月 (今天日 - 1)
    last_month_end_full = curr_start - timedelta(days=1)
    last_month_start = last_month_end_full.replace(day=1)
    same_day_offset = (today - curr_start).days  # 6/2 时 = 1
    prev_start = last_month_start
    prev_end = min(last_month_start + timedelta(days=same_day_offset - 1)
                   if same_day_offset > 0 else last_month_start,
                   last_month_end_full)

    sql = """
        WITH curr AS (
            SELECT vendor_id, SUM(cost_cny) AS cost
            FROM vendor_usage_daily
            WHERE usage_date BETWEEN :curr_start AND :curr_end
            GROUP BY 1
        ),
        prev AS (
            SELECT vendor_id, SUM(cost_cny) AS cost
            FROM vendor_usage_daily
            WHERE usage_date BETWEEN :prev_start AND :prev_end
            GROUP BY 1
        ),
        totals AS (
            SELECT (SELECT COALESCE(SUM(cost), 0) FROM curr) AS curr_total,
                   (SELECT COALESCE(SUM(cost), 0) FROM prev) AS prev_total
        )
        SELECT
            COALESCE(c.vendor_id, p.vendor_id) AS vendor_id,
            COALESCE(m.display_name, COALESCE(c.vendor_id, p.vendor_id)) AS vendor_name,
            COALESCE(c.cost, 0) AS curr_cost,
            COALESCE(p.cost, 0) AS prev_cost,
            (SELECT curr_total FROM totals) AS curr_total,
            (SELECT prev_total FROM totals) AS prev_total
        FROM curr c FULL OUTER JOIN prev p USING (vendor_id)
        LEFT JOIN vendor_meta m ON m.vendor_id = COALESCE(c.vendor_id, p.vendor_id)
        ORDER BY COALESCE(c.cost, 0) DESC
    """
    params = {
        "curr_start": curr_start, "curr_end": curr_end,
        "prev_start": prev_start, "prev_end": prev_end,
    }
    with SessionLocal() as s:
        rows = s.execute(text(sql), params).all()

    items = []
    for r in rows:
        curr = float(r.curr_cost or 0)
        prev = float(r.prev_cost or 0)
        curr_total = float(r.curr_total or 0)
        prev_total = float(r.prev_total or 0)
        curr_pct = curr / curr_total * 100 if curr_total > 0 else 0
        prev_pct = prev / prev_total * 100 if prev_total > 0 else 0
        items.append({
            "vendor_id": r.vendor_id,
            "vendor_name": r.vendor_name,
            "curr_cost": round(curr, 2),
            "prev_cost": round(prev, 2),
            "curr_pct": round(curr_pct, 2),
            "prev_pct": round(prev_pct, 2),
            "delta_pct_pt": round(curr_pct - prev_pct, 2),  # 百分点变化, 不是相对%
        })
    return {
        "curr_window": {"start": curr_start.isoformat(), "end": curr_end.isoformat()},
        "prev_window": {"start": prev_start.isoformat(), "end": prev_end.isoformat()},
        "curr_total": round(float(rows[0].curr_total) if rows else 0, 2),
        "prev_total": round(float(rows[0].prev_total) if rows else 0, 2),
        "items": items,
    }


# ───────── 6. Model 集中度帕累托 ─────────
@router.get("/model-pareto")
def model_pareto(
    days: int = Query(30, ge=1, le=180),
    top_n: int = Query(15, ge=5, le=50),
):
    """所有 (vendor, model) 按 cost 排序, 前 top_n 行 + "其他" 汇总. 含累计占比."""
    today = _today_cst()
    start = today - timedelta(days=days)
    end = today - timedelta(days=1)
    sql = """
        SELECT u.vendor_id,
               COALESCE(m.display_name, u.vendor_id) AS vendor_name,
               u.model,
               SUM(u.cost_cny) AS cost,
               SUM(u.request_count) AS req,
               SUM(u.total_tokens) AS total_tokens
        FROM vendor_model_usage_daily u
        LEFT JOIN vendor_meta m ON m.vendor_id = u.vendor_id
        WHERE u.usage_date BETWEEN :start AND :end
        GROUP BY 1, 2, 3
        HAVING SUM(u.cost_cny) > 0
        ORDER BY cost DESC
    """
    with SessionLocal() as s:
        rows = s.execute(text(sql), {"start": start, "end": end}).all()

    total = sum(float(r.cost or 0) for r in rows)
    items = []
    cum = 0.0
    for r in rows[:top_n]:
        c = float(r.cost or 0)
        cum += c
        items.append({
            "vendor_id": r.vendor_id,
            "vendor_name": r.vendor_name,
            "model": r.model,
            "cost_cny": round(c, 2),
            "share_pct": round(c / total * 100, 2) if total > 0 else 0,
            "cumulative_pct": round(cum / total * 100, 2) if total > 0 else 0,
            "req": int(r.req or 0),
            "total_tokens": int(r.total_tokens or 0),
        })
    # 其他汇总
    other_cost = total - cum
    other = None
    if len(rows) > top_n and other_cost > 0:
        other = {
            "vendor_id": None,
            "vendor_name": "其他",
            "model": f"({len(rows) - top_n} 个 model)",
            "cost_cny": round(other_cost, 2),
            "share_pct": round(other_cost / total * 100, 2),
            "cumulative_pct": 100.0,
            "req": None,
            "total_tokens": None,
        }
    return {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "total_cost_cny": round(total, 2),
        "top_n_share_pct": round(cum / total * 100, 2) if total > 0 else 0,
        "items": items,
        "other": other,
    }


# ───────── 7. 系列 cache 率对比 (top N model-vendor 按 series 聚合) ─────────
@router.get("/cache-by-series")
def cache_by_series(
    days: int = Query(30, ge=1, le=180),
    top_n: int = Query(15, ge=5, le=100,
                       description="top N (model, vendor) by cache 命中率 决定哪些 series 入榜; "
                                   "一旦 series 入榜, 该系所有 vendor 都列出对比 (不再受 top_n 截断)"),
    min_input_for_rank: int = Query(1_000_000, ge=0,
                                    description="参与 top_n 排名的 (m,v) 最低 input_calc 门槛, "
                                                 "过滤 100 tokens 全命中这种没规模的水货"),
    min_vendor_cost: float = Query(1.0, ge=0,
                                   description="series 内 vendor cost 起步门槛 (¥), "
                                                "过滤 ¥0.x 噪声; 设 0 显示全部"),
):
    """同系列 (claude/gpt/gemini/kimi 等) 跨供应商的 cache 率对比.

    业务洞察: claude 系在 wangsu cache 83% 但 road2all 0% — 同样请求转 wangsu 能省一大块.

    流程:
      1. 取全部 (model, vendor) 数据 (cost > 0)
      2. 按 cache 命中率 (cache_read / input_calc) 降序, 取前 top_n 决定哪些 series "入榜"
         (高 cache 率 = 该 model+vendor 把 cache 玩明白了, 入榜的是"已经开了 cache"的系;
          gemini 大家都没开 cache → 全 ~0% 进不来, 比对意义本来就弱, 入不入都行)
      3. 入榜的 series, 该系**所有** vendor 都列出 (即使 cost 不在 top_n 里),
         过滤 cost < min_vendor_cost 的长尾 (¥0.55 nulls 这种)
      4. 卡片视图: 每 series 一张, 内部 vendor 按 cost 排, 横排柱图对比

    "_extract_series 为'其他'的行"不参与入榜筛选 (避免 garbage model 拉一个'其他'系出来对比).
    min_input_for_rank 过滤掉 input 太小的 (m,v) 参与 top 排名 — 100 tokens 全命中 100% 没业务意义.
    被过滤掉的 (m,v) 仍参与 series 内 vendor 累加 (只是不影响哪些 series 入榜).

    input_calc 规则跟 cache-hit endpoint 一致 (行级 CASE WHEN, 兼容 含cache/不含cache 两种约定).
    """
    today = _today_cst()
    start = today - timedelta(days=days)
    end = today - timedelta(days=1)
    sql = """
        WITH per_row AS (
            SELECT
                u.vendor_id, u.model,
                u.cache_read_tokens, u.cost_cny,
                CASE
                  WHEN COALESCE(u.prompt_tokens, 0) >=
                       COALESCE(u.cache_read_tokens, 0) + COALESCE(u.cache_write_tokens, 0)
                    THEN COALESCE(u.prompt_tokens, 0)
                  ELSE COALESCE(u.prompt_tokens, 0)
                       + COALESCE(u.cache_read_tokens, 0)
                       + COALESCE(u.cache_write_tokens, 0)
                END AS input_calc
            FROM vendor_model_usage_daily u
            WHERE u.usage_date BETWEEN :start AND :end
              AND u.total_tokens IS NOT NULL
              AND u.total_tokens > 0
        ),
        agg AS (
            SELECT
                p.vendor_id, p.model,
                SUM(p.input_calc) AS input_total,
                SUM(COALESCE(p.cache_read_tokens, 0)) AS cache_read,
                SUM(p.cost_cny) AS cost_cny
            FROM per_row p
            GROUP BY 1, 2
            HAVING SUM(p.cost_cny) > 0
        )
        SELECT
            a.vendor_id,
            COALESCE(m.display_name, a.vendor_id) AS vendor_name,
            a.model, a.input_total, a.cache_read, a.cost_cny,
            CASE WHEN a.input_total > 0
                 THEN a.cache_read::float / a.input_total
                 ELSE 0 END AS hit_rate_raw
        FROM agg a
        LEFT JOIN vendor_meta m ON m.vendor_id = a.vendor_id
        ORDER BY a.cost_cny DESC
    """
    with SessionLocal() as s:
        rows = s.execute(text(sql), {"start": start, "end": end}).all()

    # Step 1: 按 hit_rate 降序选 top_n (m,v), 提取入榜 series
    # 用 Python 排序而不是 SQL ORDER BY hit_rate, 因为还要应用 min_input_for_rank 过滤
    ranked = [r for r in rows if (r.input_total or 0) >= min_input_for_rank]
    ranked.sort(key=lambda r: r.hit_rate_raw or 0, reverse=True)
    series_in_chart: set[str] = set()
    for r in ranked[:top_n]:
        s = _extract_series(r.model)
        if s != "其他":  # garbage 不入榜
            series_in_chart.add(s)

    # Step 2: 全数据按 (series, vendor) 累加, 只保留入榜 series
    by_series: dict[str, dict[str, dict]] = {}
    for r in rows:
        series = _extract_series(r.model)
        if series not in series_in_chart:
            continue
        sv = by_series.setdefault(series, {})
        v = sv.setdefault(r.vendor_id, {
            "vendor_id": r.vendor_id,
            "vendor_name": r.vendor_name,
            "input_total": 0,
            "cache_read": 0,
            "cost_cny": 0.0,
            "models": [],
        })
        v["input_total"] += int(r.input_total or 0)
        v["cache_read"] += int(r.cache_read or 0)
        v["cost_cny"] += float(r.cost_cny or 0)
        v["models"].append(r.model)

    # Step 3: 过滤 vendor cost < min_vendor_cost, 排序输出
    series_list = []
    for series, vendors in by_series.items():
        vendor_list = []
        for v in vendors.values():
            if v["cost_cny"] < min_vendor_cost:
                continue
            hit = (v["cache_read"] / v["input_total"] * 100) if v["input_total"] > 0 else 0
            vendor_list.append({
                "vendor_id": v["vendor_id"],
                "vendor_name": v["vendor_name"],
                "input_tokens": v["input_total"],
                "cache_read_tokens": v["cache_read"],
                "cost_cny": round(v["cost_cny"], 2),
                "hit_rate": round(hit, 1),
                "models": sorted(set(v["models"])),
            })
        if not vendor_list:
            continue
        vendor_list.sort(key=lambda x: x["cost_cny"], reverse=True)
        total_cost = sum(x["cost_cny"] for x in vendor_list)
        total_input = sum(x["input_tokens"] for x in vendor_list)
        total_cache = sum(x["cache_read_tokens"] for x in vendor_list)
        avg_hit = (total_cache / total_input * 100) if total_input > 0 else 0
        series_list.append({
            "series": series,
            "total_cost_cny": round(total_cost, 2),
            "avg_hit_rate": round(avg_hit, 1),
            "vendor_count": len(vendor_list),
            "vendors": vendor_list,
        })
    series_list.sort(key=lambda x: x["total_cost_cny"], reverse=True)

    return {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "top_n_basis": top_n,
        "basis_row_count": min(top_n, len(ranked)),  # 实际参与 top_n 排名的行数 (含 input 阈值过滤后)
        "min_input_for_rank": min_input_for_rank,
        "series": series_list,
    }
