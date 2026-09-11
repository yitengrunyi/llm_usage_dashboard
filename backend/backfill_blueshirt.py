"""blueshirt (gptmeta) backfill — 独立子进程, 不影响 xhub.

用法 (服务器):
    cd /data/llm-usage-dashboard
    sudo docker compose exec -d backend python3 /app/backfill_blueshirt.py
    sudo docker compose exec backend tail -f /tmp/gptmeta_backfill.log

撞 14 天连空停 — blueshirt 数据可能跨 1+ 年 (probe 显示总量 176 万条 log).
节奏: 3-7 分钟/天 (raw log 慢), 估计 30+ 小时跑完.
"""
import sys, os, time, logging, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
# 自动找 ingest/ 所在目录: 优先看 HERE/ingest, 否则 HERE/backend/ingest
if os.path.isdir(os.path.join(HERE, 'ingest')):
    BACKEND = HERE
elif os.path.isdir(os.path.join(HERE, 'backend', 'ingest')):
    BACKEND = os.path.join(HERE, 'backend')
else:
    raise SystemExit(f'找不到 ingest/, HERE={HERE}')
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

VENDOR_ID = 'gptmeta'
EMPTY_THRESHOLD = 14
SLEEP_BETWEEN = 2.0
LOG_FILE = f'/tmp/{VENDOR_ID}_backfill.log'

logging.basicConfig(
    level=logging.INFO, force=True,
    format=f'%(asctime)s [{VENDOR_ID}] %(message)s',
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)


def main():
    # 启动前 cleanup: 把本 vendor stale running 标 failed
    from ingest.db import SessionLocal
    from sqlalchemy import text
    with SessionLocal() as s:
        r = s.execute(text(
            "UPDATE vendor_ingest_run SET status='failed', "
            "error_msg='blueshirt backfill startup cleanup', finished_at=now() "
            "WHERE vendor_id=:v AND status='running'"
        ), {'v': VENDOR_ID})
        s.commit()
        if r.rowcount:
            logging.info(f'清理 {r.rowcount} 个 stale running run')

    from ingest.job import run_ingest, _load_vendor_config
    from ingest.adapters import ADAPTERS

    adapter_cls = ADAPTERS[VENDOR_ID]
    vendor = _load_vendor_config(VENDOR_ID)
    adapter = adapter_cls(vendor)

    # 从 DB 最老 - 1 开始 (没数据就从昨天)
    with SessionLocal() as s:
        earliest = s.execute(text(
            'SELECT MIN(usage_date) FROM vendor_usage_daily WHERE vendor_id=:v'
        ), {'v': VENDOR_ID}).scalar()
    if earliest:
        day = earliest - dt.timedelta(days=1)
    else:
        day = dt.date.today() - dt.timedelta(days=1)
    logging.info(f'从 {day} 往老挖, 撞 {EMPTY_THRESHOLD} 天连空停')

    empty_streak = 0; ok = 0; fail = 0
    while empty_streak < EMPTY_THRESHOLD:
        rows = None
        for attempt in range(3):
            try:
                rows = adapter.fetch_one_day(day); break
            except Exception as e:
                wait = 30 * (attempt + 1)
                logging.warning(f'{day} fetch attempt {attempt+1}/3: {str(e)[:200]}, 等 {wait}s')
                time.sleep(wait)
        if rows is None:
            logging.error(f'{day} 3 次都失败, 跳过')
            fail += 1
            day -= dt.timedelta(days=1)
            continue
        if not rows:
            empty_streak += 1
            logging.info(f'{day} 空 ({empty_streak}/{EMPTY_THRESHOLD})')
        else:
            empty_streak = 0
            try:
                rid = run_ingest(VENDOR_ID, day, day, trigger='blueshirt_backfill')
                logging.info(f'{day} ✓ run_id={rid} models={len(rows)}')
                ok += 1
            except Exception as e:
                logging.error(f'{day} 入库失败: {e}'); fail += 1
        day -= dt.timedelta(days=1)
        time.sleep(SLEEP_BETWEEN)
    logging.info(f'收尾: ✓ {ok}, ✗ {fail}, 撞 {EMPTY_THRESHOLD} 天连空停')


if __name__ == '__main__':
    main()
