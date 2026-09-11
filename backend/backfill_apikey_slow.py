"""blueshirt/nulls 按 API key 数据抢救脚本 — 跑最近 N 天 (默认 7) 的 slow path.

slow path 翻日志时已顺手按 (token_name, model) 聚合写 vendor_apikey_usage_daily,
所以跑一天 = 补齐拆分字段 + 补齐 key 表, 一批请求两个产出.

retention 提醒: blueshirt /api/log/self 实测保留 ~22 天, xhub 是 row 数 LRU —
超窗的天上游返空, 跑了也只有 0 行, 不会报错.

用法 (本地 venv):
    cd backend && source venv/bin/activate
    python backfill_apikey_slow.py            # 最近 7 天 (不含今天)
    python backfill_apikey_slow.py --days 3

注意: 本脚本不建 run 行, 避开 03:00 CST 前后跑 (防止和 cron 的 slow-fill 撞同一天).
"""
import argparse
import datetime as dt
import logging
import os
import sys
from datetime import timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.isdir(os.path.join(HERE, "ingest")):
    sys.path.insert(0, HERE)
    os.chdir(HERE)
else:
    raise SystemExit(f"找不到 ingest/, 请在 backend/ 下跑, HERE={HERE}")

logging.basicConfig(
    level=logging.INFO, force=True,
    format="%(asctime)s [apikey-slow] %(message)s",
)

from vendors import get_vendor
from ingest.blueshirt_slow import slow_fill_day as blueshirt_slow_fill
from ingest.newapi_direct_slow import slow_fill_day as xhub_slow_fill

CST = timezone(timedelta(hours=8))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="回填最近 N 天 (默认 7, 不含今天)")
    args = ap.parse_args()
    if args.days < 1:
        ap.error("--days 至少为 1")

    today = dt.datetime.now(CST).date()
    days = [today - timedelta(days=i) for i in range(args.days, 0, -1)]  # 老到新
    logging.info(f"窗口: {days[0]} ~ {days[-1]} ({len(days)} 天)")

    nulls = get_vendor("nulls")
    if nulls is None:
        raise SystemExit("vendors.json 里找不到 nulls (或 enabled=false), 不跑")

    ok = 0
    fail = 0
    for day in days:
        # 只跑 nulls（blueshirt 已在别处回填完）
        try:
            r = xhub_slow_fill(nulls, day)
            keys = r.get("apikeys")
            keys_s = "跳过(日志不完整)" if keys is None else keys
            logging.info(
                f"nulls {day} ✓ logs={r['logs']} models={r['models']} "
                f"updated={r['updated']} apikeys={keys_s} 用时 {r['elapsed_s']}s"
            )
            ok += 1
        except Exception as e:
            logging.error(f"nulls {day} 失败: {e}")
            fail += 1

    logging.info(f"收尾: ✓ {ok}, ✗ {fail}")


if __name__ == "__main__":
    main()
