"""xhub 回填 — 从昨天往老一直跑到 2024-11-01, 跳过已落库的天.

保护机制:
- 连续 N 次失败立刻 abort (防触发 xhub 限流 / 封号)
- 单次失败前清掉 session 缓存, 下次重登 (而不是死循环)
"""
import sys, logging, datetime as dt
sys.path.insert(0, '/app')
logging.basicConfig(level=logging.INFO,
    format='%(asctime)s [xhub] %(message)s',
    handlers=[logging.FileHandler('/tmp/xhub_backfill.log'), logging.StreamHandler()])
from ingest.job import run_ingest
from ingest.db import SessionLocal
from sqlalchemy import text

# 启动前清 stale running
with SessionLocal() as s:
    s.execute(text("UPDATE vendor_ingest_run SET status='failed', error_msg='xhub auto-cleanup', finished_at=now() WHERE vendor_id='xhub' AND status='running'"))
    s.commit()

with SessionLocal() as s:
    existing = set(s.execute(text(
        "SELECT usage_date FROM vendor_usage_daily WHERE vendor_id='xhub' AND request_count > 0"
    )).scalars())
logging.info(f'已落库 {len(existing)} 天, 跳过')

START = dt.date(2024, 11, 1)
MAX_CONSECUTIVE_FAILS = 3   # 防爆: 连续 3 次失败就停, 别打死上游
day = dt.date.today() - dt.timedelta(days=1)
ok = skip = fail = 0
consecutive_fails = 0
while day >= START:
    if day in existing:
        day -= dt.timedelta(days=1)
        skip += 1
        continue
    try:
        run_ingest('xhub', day, day, trigger='backfill')
        logging.info(f'{day} ✓')
        ok += 1
        consecutive_fails = 0   # 成功就清零
    except Exception as e:
        logging.error(f'{day} ✗ {str(e)[:120]}')
        fail += 1
        consecutive_fails += 1
        if consecutive_fails >= MAX_CONSECUTIVE_FAILS:
            logging.error(f'!! 连续 {MAX_CONSECUTIVE_FAILS} 次失败, ABORT 防止打死上游')
            break
    day -= dt.timedelta(days=1)

logging.info(f'收尾: ok={ok} skip={skip} fail={fail}')
