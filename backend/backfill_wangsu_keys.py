"""wangsu 按 key 数据回填 — 控制台令牌维度, 只写 vendor_apikey_usage_daily.

不跑主表 ingest (open API 限流敏感), 只调 adapter.fetch_apikey_rows +
DELETE+INSERT, 跟 job.py 的 key 写入同幂等语义. 空行不动已有数据.

用法 (backend/ 下):
    PYTHONPATH=. python backfill_wangsu_keys.py --start 2026-07-01 --end 2026-08-19
"""
import argparse
import datetime as dt
import logging
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.isdir(os.path.join(HERE, "ingest")):
    sys.path.insert(0, HERE)
    os.chdir(HERE)
else:
    raise SystemExit(f"找不到 ingest/, 请在 backend/ 下跑, HERE={HERE}")

logging.basicConfig(level=logging.INFO, force=True,
                    format="%(asctime)s [wangsu-keys] %(message)s")

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from exchange_rate import get_rates
from vendors import get_vendor
from ingest.job import _convert_cost, _inject_env_credentials
from ingest.db import SessionLocal
from ingest.models import VendorApiKeyUsageDaily
from ingest.adapters.wangsu_adapter import WangsuAdapter


def write_day(adapter: WangsuAdapter, day: dt.date, fx: float, run_note: str) -> int:
    rows = adapter.fetch_apikey_rows(day)
    if not rows:
        logging.info("%s 无 key 行 (或拉取失败), 表不动", day)
        return 0
    payloads = [{
        "vendor_id": "wangsu",
        "usage_date": day,
        "api_key": r.api_key,
        "model": r.model,
        "prompt_tokens": r.prompt_tokens,
        "completion_tokens": r.completion_tokens,
        "cache_read_tokens": r.cache_read_tokens,
        "cache_write_tokens": r.cache_write_tokens,
        "total_tokens": r.total_tokens,
        "request_count": r.request_count,
        "image_count": r.image_count,
        "cost_native": r.cost_native,
        "cost_usd": usd,
        "cost_cny": cny,
        "fx_rate": fx,
        "last_run_id": None,
    } for r in rows
        for usd, cny in [_convert_cost(r.cost_native, "CNY", fx)]]
    with SessionLocal() as s:
        s.execute(text(
            "DELETE FROM vendor_apikey_usage_daily "
            "WHERE vendor_id='wangsu' AND usage_date=:d"), {"d": day})
        s.execute(pg_insert(VendorApiKeyUsageDaily).values(payloads))
        s.commit()
    return len(payloads)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    args = ap.parse_args()
    start, end = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)

    vendor = get_vendor("wangsu")
    if vendor is None:
        raise SystemExit("vendors.json 里找不到 wangsu")
    _inject_env_credentials(vendor)
    if not vendor.get("console_cookie"):
        raise SystemExit("WANGSU_CONSOLE_COOKIE 未配置")
    adapter = WangsuAdapter(vendor)

    fx = float(get_rates().get("CNY") or 7.2)
    day = start
    ok = fail = 0
    while day <= end:
        try:
            n = write_day(adapter, day, fx, "backfill")
            logging.info("%s ✓ %d 行", day, n)
            ok += 1
        except Exception as e:
            logging.error("%s 失败: %s", day, e)
            fail += 1
        day += dt.timedelta(days=1)
        time.sleep(1.2)
    logging.info("收尾: ✓ %d 天, ✗ %d 天", ok, fail)


if __name__ == "__main__":
    main()
