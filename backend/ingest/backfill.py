"""一次性回填脚本 — 按 vendor / 时间窗补历史数据.

用法:
    cd backend
    python3 -m ingest.backfill --days=90 --vendors=all
    python3 -m ingest.backfill --vendors=volcengine --days=30
    python3 -m ingest.backfill --start=2026-01-01 --end=2026-05-01 --vendors=openai

策略:
- 串行跑, 一个 vendor 跑完再下一个 (避免上游限流爆炸)
- 单 vendor 内 run_ingest_with_retry 自己按天串行 + 失败 4 次重试 + 飞书报警
- UPSERT 语义, 重复跑同时段会覆盖旧数据 (e.g. volcengine adapter 改造后重跑修历史)
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timedelta, timezone

import psycopg2

from ingest.settings import (
    DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_SCHEMA, DB_USER,
)

CST = timezone(timedelta(hours=8))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ingest backfill")
    p.add_argument("--days", type=int, default=90, help="从今天往前回填几天 (默认 90)")
    p.add_argument("--start", help="开始日期 YYYY-MM-DD (跟 --days 二选一)")
    p.add_argument("--end", help="结束日期 YYYY-MM-DD (默认昨天 CST)")
    p.add_argument(
        "--vendors", default="all",
        help="逗号分隔的 vendor_id 列表, 或 'all' 表示全部 enabled",
    )
    p.add_argument("--dry-run", action="store_true", help="只打印不跑")
    return p.parse_args()


def resolve_window(args: argparse.Namespace) -> tuple[date, date]:
    today_cst = datetime.now(CST).date()
    end = date.fromisoformat(args.end) if args.end else today_cst - timedelta(days=1)
    if args.start:
        start = date.fromisoformat(args.start)
    else:
        start = end - timedelta(days=args.days - 1)
    return start, end


def list_enabled_vendors() -> list[str]:
    import json
    from pathlib import Path
    cfg_path = Path(__file__).parent.parent / "config" / "vendors.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    return [v["id"] for v in cfg.get("vendors", []) if v.get("enabled")]


def db_ping() -> str:
    """连 DB 看 schema 通不通. 不假设 alembic 表存在 (生产可能手工建表)."""
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD,
        connect_timeout=10,
    )
    cur = conn.cursor()
    cur.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema=%s",
        (DB_SCHEMA,),
    )
    n = cur.fetchone()[0]
    conn.close()
    return f"{DB_SCHEMA} schema 中有 {n} 个表"


def main() -> int:
    args = parse_args()
    start, end = resolve_window(args)

    if args.vendors == "all":
        vendor_ids = list_enabled_vendors()
    else:
        vendor_ids = [v.strip() for v in args.vendors.split(",") if v.strip()]

    print(f"DB: {db_ping()}")
    print(f"窗口: {start} → {end} ({(end - start).days + 1} 天)")
    print(f"Vendors ({len(vendor_ids)}): {vendor_ids}")
    print()

    if args.dry_run:
        print("dry-run, 不实际跑")
        return 0

    # 真跑 — import 放下面避免无谓启动时间
    from ingest.adapters import ADAPTERS
    from ingest.job import run_ingest_with_retry
    from ingest.scheduler import _setup_logging

    # backfill 容器/进程没有 uvicorn, ingest.* logger 无 handler → 降级 WARNING
    # (e.g. openai key 维度拉取失败) 只经 lastResort 打 stderr 易丢。挂上后
    # stdout + data/logs/scheduler.log 都可见。幂等 (tagged handler 防重复)。
    _setup_logging()

    failed: list[tuple[str, str]] = []
    for i, vid in enumerate(vendor_ids, 1):
        if vid not in ADAPTERS:
            print(f"[{i}/{len(vendor_ids)}] {vid}: 跳过 (adapter 未注册)")
            continue
        t0 = time.time()
        print(f"[{i}/{len(vendor_ids)}] {vid}: {start} ~ {end} 跑中...")
        try:
            run_id = run_ingest_with_retry(vid, start, end, trigger="backfill")
            print(f"  ok  耗时 {time.time()-t0:.1f}s, run_id={run_id}")
        except Exception as e:
            print(f"  FAIL  {e}")
            failed.append((vid, str(e)))

    print()
    print(f"完成. 成功 {len(vendor_ids) - len(failed)} / {len(vendor_ids)}")
    if failed:
        print("失败:")
        for vid, err in failed:
            print(f"  - {vid}: {err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
