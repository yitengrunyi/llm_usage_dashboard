"""blueshirt 慢路径覆盖脚本 — 服务器上独立跑.

背景:
- 快路径 (BlueshirtAdapter 走 /api/data/self) 已经把 cost/total_tokens/request_count 填进 DB
- 但 /api/data/self 不给 prompt/completion/cache_read/cache_write 拆分
- 此脚本走 raw /api/log/self, 慢但字段全, 一天一天 UPDATE 补上拆分字段

特性 — 跟 backfill_blueshirt.py 不一样的地方:
- 完全串行翻页 (不用 ThreadPoolExecutor — 之前 14.8 万条 1485 页死锁过)
- 单页 60s timeout, 失败 1 次跳过这页 (不 retry)
- 单天总超时 30 min, 超就放弃这天去下一天
- 只 UPDATE 拆分字段, 不动 cost / total_tokens / request_count

用法 (服务器后台):
    sudo docker cp backend/backfill_blueshirt_slow.py llm-usage-dashboard-backend-1:/app/
    sudo docker compose exec -d backend python3 /app/backfill_blueshirt_slow.py
    sudo docker compose exec backend tail -f /tmp/gptmeta_slow.log

跑多久无所谓 (一天 5-30 分钟, 一年数据 ~10 天连续跑). 中途断了重启接着跑, 已 UPDATE 的天不会重复.
"""
import sys, os, time, json, logging, datetime as dt
from datetime import datetime, timezone, timedelta
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.isdir(os.path.join(HERE, 'ingest')):
    BACKEND = HERE
elif os.path.isdir(os.path.join(HERE, 'backend', 'ingest')):
    BACKEND = os.path.join(HERE, 'backend')
else:
    raise SystemExit(f'找不到 ingest/, HERE={HERE}')
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

LOG_FILE = '/tmp/gptmeta_slow.log'
logging.basicConfig(
    level=logging.INFO, force=True,
    format='%(asctime)s [gptmeta-slow] %(message)s',
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
logging.getLogger('urllib3').setLevel(logging.WARNING)

from sqlalchemy import text

from ingest.db import SessionLocal
from ingest.blueshirt_slow import (
    VENDOR_ID, QUOTA_PER_DOLLAR, fetch_day_logs_serial, aggregate_with_cache, update_day,
)
from ingest.apikey_slow import aggregate_by_key, write_apikey_rows
from newapi_client import _load_session, _get_state_file

BASE_URL = 'https://www.blueshirtmap.com'
SLEEP_BETWEEN_DAYS = 5


def main():
    cookies, user_id, header_name = _load_session(_get_state_file(BASE_URL))
    headers = {header_name: user_id}
    logging.info(f'session: user_id={user_id}, header_name={header_name}')

    # 显式日期参数: python backfill_blueshirt_slow.py 2026-08-01 2026-08-18
    # 强制覆盖指定范围 (用于口径修复后的重灌), 不受"已有 prompt 数据跳过"限制.
    # 无参数时保持旧行为: 只挑 prompt_tokens IS NULL/0 的天.
    if len(sys.argv) >= 3:
        d0 = dt.date.fromisoformat(sys.argv[1])
        d1 = dt.date.fromisoformat(sys.argv[2])
        days = [d0 + dt.timedelta(days=i) for i in range((d1 - d0).days + 1)]
    else:
        with SessionLocal() as s:
            days = s.execute(text("""
                SELECT DISTINCT usage_date FROM vendor_model_usage_daily
                WHERE vendor_id=:v
                  AND (prompt_tokens IS NULL OR prompt_tokens = 0)
                ORDER BY usage_date DESC
            """), {"v": VENDOR_ID}).scalars().all()
    if not days:
        logging.info('没有需要慢路径覆盖的天, 退出')
        return

    logging.info(f'需要覆盖 {len(days)} 天: {days[0]} ~ {days[-1]}')

    ok = 0; skip = 0
    for day in days:
        t0 = time.time()
        try:
            logs, complete = fetch_day_logs_serial(cookies, headers, day)
            agg = aggregate_with_cache(logs)
            n = update_day(day, agg)
            # 日志完整时同步刷新 key 表 (跟 slow_fill_day 同语义); 残缺不动已有行
            keys = write_apikey_rows(VENDOR_ID, day, aggregate_by_key(logs), QUOTA_PER_DOLLAR) if complete else None
            elapsed = time.time() - t0
            logging.info(f'{day} ✓ logs={len(logs)} models={len(agg)} updated={n} keys={keys} complete={complete} 用时 {elapsed:.0f}s')
            ok += 1
        except RuntimeError as e:
            if 'session expired' in str(e):
                logging.error(f'!! {day} {e} — ABORT 整个脚本, 请刷 session 后重启')
                break
            logging.error(f'{day} 失败: {e}')
            skip += 1
        except Exception as e:
            logging.error(f'{day} 失败: {e}')
            skip += 1
        time.sleep(SLEEP_BETWEEN_DAYS)

    logging.info(f'收尾: ✓ {ok}, ✗ {skip}')


if __name__ == '__main__':
    main()
