"""Grok token 回填 — 发票精确总量 + 每日 usd 形状分摊, 只 UPDATE token 列不动 cost.

背景 (2026-08-27 实测):
- usage analytics 只开放 usd 指标, token 指标名全 400 → 每日 ingest 拿不到 token
- 但 GET /invoices 与 /postpaid/invoice/preview 的 lines[] 带每模型×unitType 的
  精确 token 数 (numUnits), 账期 = UTC 自然月, 当前账期随 preview 随用随出
- 每日按 unit_type 拆分的 usd 可查 (groupBy unit_type)

算法 (有效单价法):
  eff_price(模型, 类型, 账期) = 账期 usd ÷ 账期 token 数   (内含长上下文≥200k 档位混合)
  tokens(模型, 类型, 日) = 该日该类型 usd ÷ eff_price
  → 账期合计精确 (差<0.1% 为 CST/UTC 边界小时), 日粒度按成本形状分布

字段映射:
  prompt_tokens    = Prompt text + Prompt image + Cached prompt   (完整输入, 同 adapter 口径)
  cache_read_tokens = Cached prompt
  completion_tokens = Completion + Reasoning                     (思考 token 按输出计价)
  total_tokens     = prompt + completion
  request_count / cache_write / image_count 不动 (上游无此数据, 保持 NULL)

vendor_apikey_usage_daily 的 token 同步:
  上游按 api_key 只出 usd 不出 token → key 行的 token 在这里按
  (model, day) 内各 key 的 usd 占比分摊 model token (最大余数法, Σkey=model 精确;
  单 key 时即精确值)。cron 平时的 ingest 只写 key 行的 cost, token 靠本脚本 true-up。

用法:
    cd backend && source venv/bin/activate
    python -m ingest.grok_token_backfill --start=2026-08-01 --end=2026-08-25
    python -m ingest.grok_token_backfill --start=2026-08-01 --end=2026-08-25 --dry-run
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections import defaultdict

from sqlalchemy import text

import grok_client
from exchange_rate import get_rates
from ingest.db import SessionLocal
from ingest.job import _inject_env_credentials, _refresh_usage_daily_total
from vendors import get_vendor

# unitType → 字段归属; 出现未知类型直接失败 (宁失败不编造, 上游加新计费类型时会显式暴露)
_INPUT_PLAIN = {"Prompt text tokens", "Prompt image tokens"}
_INPUT_CACHED = {"Cached prompt text tokens"}
_OUTPUT = {"Completion text tokens", "Reasoning text tokens"}
_KNOWN = _INPUT_PLAIN | _INPUT_CACHED | _OUTPUT

# 3b: model 表 SUM 回写 vendor 层 — 与 job._ingest_one_day 完全同款, 保证两层物化一致
_SUM_BACK_SQL = text("""
    UPDATE vendor_usage_daily AS v SET
        prompt_tokens = sub.p,
        completion_tokens = sub.c,
        cache_read_tokens = sub.cr,
        cache_write_tokens = sub.cw,
        total_tokens = sub.t
    FROM (
        SELECT SUM(prompt_tokens) AS p,
               SUM(completion_tokens) AS c,
               SUM(cache_read_tokens) AS cr,
               SUM(cache_write_tokens) AS cw,
               SUM(total_tokens) AS t
        FROM vendor_model_usage_daily
        WHERE vendor_id=:v AND usage_date=:d
    ) AS sub
    WHERE v.vendor_id=:v AND v.usage_date=:d
""")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="grok token backfill (invoice-based)")
    p.add_argument("--start", required=True, help="开始日期 YYYY-MM-DD (CST)")
    p.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD (CST)")
    p.add_argument("--dry-run", action="store_true", help="只打印不写库")
    return p.parse_args()


def _alloc_tokens(slot: dict, shares: dict[str, float]) -> dict[str, dict[str, int]]:
    """把 (model, day) 的 {in, cache, out} 按各 key 的 usd 占比分摊; 最大余数法保证 Σkey=slot。"""
    keys = sorted(shares)
    total_usd = sum(shares.values())
    out: dict[str, dict[str, int]] = {k: {} for k in keys}
    for field in ("in", "cache", "out"):
        total_tokens = slot[field]
        if total_tokens <= 0 or total_usd <= 0:
            for k in keys:
                out[k][field] = 0
            continue
        raw = [(k, total_tokens * shares[k] / total_usd) for k in keys]
        alloc = {k: int(v) for k, v in raw}
        for k, _ in sorted(raw, key=lambda x: x[1] - int(x[1]), reverse=True)[:total_tokens - sum(alloc.values())]:
            alloc[k] += 1
        for k in keys:
            out[k][field] = alloc[k]
    return out


def main() -> int:
    args = parse_args()
    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end)
    if end < start:
        print("end 不能早于 start")
        return 1

    vendor = get_vendor("grok")
    if not vendor:
        print("vendors.json 里没有 grok")
        return 1
    _inject_env_credentials(vendor)

    # 1) 每日 (model, unitType) usd — 分摊形状
    daily = grok_client.fetch_daily_usd_by_type(vendor, start, end)

    # 2) 涉及的账期 (按 CST 日所属月份归组, CST/UTC 边界 8h 漂移可忽略)
    months = sorted({(d.year, d.month) for d in
                     (start + dt.timedelta(days=i) for i in range((end - start).days + 1))})
    eff_price: dict[tuple[int, int, str, str], float] = {}
    for y, m in months:
        cycle = grok_client.fetch_cycle_lines(vendor, y, m)
        for (model, utype), totals in cycle.items():
            if utype not in _KNOWN:
                print(f"FAIL  未知 unitType {utype!r} (model={model}) — 上游可能新增计费类型, 先人工确认")
                return 1
            if totals["tokens"] > 0 and totals["usd"] > 0:
                eff_price[(y, m, model, utype)] = totals["usd"] / totals["tokens"]
        print(f"账期 {y}-{m:02d}: {len(cycle)} 组 (模型×unitType), "
              f"${sum(t['usd'] for t in cycle.values()):,.2f}")

    # 3) 逐日分摊 → (day, model) 四桶 token
    #    per_day[model][day] = {"in": x, "cache": y, "out": z}
    per_day: dict[str, dict[str, dict[str, int]]] = defaultdict(dict)
    for (model, utype), by_date in daily.items():
        if utype not in _KNOWN:
            print(f"FAIL  analytics 返回未知 unitType {utype!r} (model={model})")
            return 1
        for date_iso, usd in by_date.items():
            day = dt.date.fromisoformat(date_iso)
            price = eff_price.get((day.year, day.month, model, utype))
            if price is None or price <= 0:
                continue
            n = round(usd / price)
            slot = per_day[model].setdefault(date_iso, {"in": 0, "cache": 0, "out": 0})
            if utype in _INPUT_PLAIN:
                slot["in"] += n
            elif utype in _INPUT_CACHED:
                slot["in"] += n
                slot["cache"] += n
            else:
                slot["out"] += n

    total_in = total_cache = total_out = 0
    for model in sorted(per_day):
        s_in = sum(v["in"] for v in per_day[model].values())
        s_cache = sum(v["cache"] for v in per_day[model].values())
        s_out = sum(v["out"] for v in per_day[model].values())
        total_in += s_in; total_cache += s_cache; total_out += s_out
        print(f"{model}: 输入(含缓存)={s_in:,} 缓存读={s_cache:,} 输出={s_out:,} "
              f"共 {len(per_day[model])} 天")
    print(f"合计: 输入 {total_in:,} / 缓存 {total_cache:,} / 输出 {total_out:,}")

    # key 表分摊形状: (date, model) -> {api_key: usd} (上游按 key 只出 usd, 不出 token)
    per_km: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    for (key_label, model), by_date in grok_client.fetch_usage_by_api_key(vendor, start, end).items():
        for date_iso, usd in by_date.items():
            per_km[(date_iso, model)][key_label] = usd

    if args.dry_run:
        print("dry-run, 不写库")
        return 0

    # 4) 写库 — 只 UPDATE token 列; cost/run_id/state 全不动
    fx_usd_to_cny = float(get_rates().get("CNY") or 7.2)
    days = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
    with SessionLocal() as s:
        for day in days:
            diso = day.isoformat()
            # 该日 DB 里的 model 行 (成本口径已在), 逐行补 token
            rows = s.execute(text(
                "SELECT model FROM vendor_model_usage_daily "
                "WHERE vendor_id='grok' AND usage_date=:d"), {"d": day}).fetchall()
            for (model,) in rows:
                slot = per_day.get(model, {}).get(diso, {"in": 0, "cache": 0, "out": 0})
                s.execute(text(
                    "UPDATE vendor_model_usage_daily SET "
                    "prompt_tokens=:p, completion_tokens=:c, cache_read_tokens=:cr, "
                    "total_tokens=:t WHERE vendor_id='grok' AND usage_date=:d AND model=:m"),
                    {"p": slot["in"], "c": slot["out"], "cr": slot["cache"],
                     "t": slot["in"] + slot["out"], "d": day, "m": model})
            # vendor 层: token SUM 回写 + has_cache_detail 置真 (cache 已是真实数据)
            s.execute(_SUM_BACK_SQL, {"v": "grok", "d": day})
            s.execute(text(
                "UPDATE vendor_usage_daily SET has_cache_detail=true "
                "WHERE vendor_id='grok' AND usage_date=:d"), {"d": day})

            # key 表: (model, day) token 按各 key usd 占比分摊 (行由日常 ingest 写入 cost)
            key_rows = s.execute(text(
                "SELECT api_key, model FROM vendor_apikey_usage_daily "
                "WHERE vendor_id='grok' AND usage_date=:d"), {"d": day}).fetchall()
            for model in sorted({m for _, m in key_rows}):
                slot = per_day.get(model, {}).get(diso) or {"in": 0, "cache": 0, "out": 0}
                alloc = _alloc_tokens(slot, per_km.get((diso, model), {}))
                for api_key, m in key_rows:
                    if m != model:
                        continue
                    a = alloc.get(api_key) or {"in": 0, "cache": 0, "out": 0}
                    s.execute(text(
                        "UPDATE vendor_apikey_usage_daily SET "
                        "prompt_tokens=:p, completion_tokens=:c, cache_read_tokens=:cr, "
                        "total_tokens=:t WHERE vendor_id='grok' AND usage_date=:d "
                        "AND api_key=:k AND model=:m"),
                        {"p": a["in"], "c": a["out"], "cr": a["cache"],
                         "t": a["in"] + a["out"], "d": day, "k": api_key, "m": model})
            _refresh_usage_daily_total(s, day, fx_usd_to_cny)
        s.commit()
    print(f"完成: {len(days)} 天 token 已回填 (model/vendor/全局三层 + apikey 表)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
