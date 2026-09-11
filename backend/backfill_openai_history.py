"""openai 历史重跑 — 修复 PT 时区 + 双桌 bug 后, DB 现存 258 天数据 ≈ 2x 真实值,
需要按 PT 自然日逐天重新跑覆写.

跟 kimi 回填的区别:
- kimi 是补缺 (跳过已落库的天); openai 是 **覆写** (现有数据是错的, 必须重跑)
- openai 每天 ~10 秒, 258 天 ≈ 45 分钟串行

跑法 (本地 / 服务器 backfill 容器都行, 脚本自动适配):
    本地:    python3 backend/backfill_openai_history.py
    服务器:  sudo docker compose exec -d backfill python3 /app/backfill_openai_history.py
            sudo docker compose exec backfill tail -f /tmp/openai_backfill.log
"""
import sys, os, logging, datetime as dt

# 自动适配 server (/app/) vs 本地 (backend/)
HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.isdir(os.path.join(HERE, 'ingest')):
    BACKEND = HERE
elif os.path.isdir('/app/ingest'):
    BACKEND = '/app'
else:
    raise SystemExit('找不到 ingest/')
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

# 读 .env (本地需要; 服务器 docker 已注入)
env_path = os.path.join(BACKEND, '.env')
if os.path.exists(env_path):
    for line in open(env_path):
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s [openai-bf] %(message)s',
    handlers=[logging.FileHandler('/tmp/openai_backfill.log'), logging.StreamHandler()])
from ingest.job import run_ingest
from ingest.db import SessionLocal
from sqlalchemy import text

# 启动前清 stale running
with SessionLocal() as s:
    s.execute(text(
        "UPDATE vendor_ingest_run SET status='failed', error_msg='openai auto-cleanup', finished_at=now() "
        "WHERE vendor_id='openai' AND status='running'"
    ))
    s.commit()

# 找 DB 现有数据的最早一天 (有 model 行的天 = 真有数据), 从它往老接着扫.
# 不重跑已落库的天 (本地已经扫过 5/26→1/26).
with SessionLocal() as s:
    # MIN(usage_date) WHERE 这天有 model 行 — 有 vendor_usage_daily 但没 model 的天是"空天 upsert"产生的, 不算
    earliest_with_data = s.execute(text("""
        SELECT MIN(usage_date) FROM vendor_model_usage_daily WHERE vendor_id='openai'
    """)).scalar()
    earliest_scanned = s.execute(text("""
        SELECT MIN(usage_date) FROM vendor_usage_daily WHERE vendor_id='openai'
    """)).scalar()

if earliest_scanned:
    # 从已扫过的最早一天 - 1 继续往老扫
    latest = earliest_scanned - dt.timedelta(days=1)
    logging.info(f'DB 已扫到 {earliest_scanned}, 有真数据最早 {earliest_with_data}, 从 {latest} 继续往老扫')
else:
    # DB 空, 从今天开始
    latest = dt.date.today() - dt.timedelta(days=1)
    logging.info(f'DB 空, 从 {latest} 开始往老扫')

# 历史下限: 最多扫到 2024-08-01 (再老 openai 还没出 admin API)
earliest = dt.date(2024, 8, 1)

logging.info(f'重跑范围 (PT 自然日): {earliest} → {latest}, 共 {(latest - earliest).days + 1} 天')

MAX_CONSECUTIVE_FAILS = 5
MAX_CONSECUTIVE_EMPTY = 14  # 连续 14 天 0 模型 → 账号没数据了, 不再往老探 (探到的最早 ≈ 2024-09-10, 给点 buffer)

ok = fail = empty = 0
consecutive_fail = 0
consecutive_empty = 0
day = latest
while day >= earliest:
    try:
        run_ingest('openai', day, day, trigger='backfill')
        # 看这天到底有没有数据 (run_ingest 不返 row 数, 直接查 DB)
        with SessionLocal() as s:
            n = s.execute(text(
                "SELECT COUNT(*) FROM vendor_model_usage_daily WHERE vendor_id='openai' AND usage_date=:d"
            ), {"d": day}).scalar() or 0
        if n == 0:
            empty += 1
            consecutive_empty += 1
            if consecutive_empty >= MAX_CONSECUTIVE_EMPTY:
                logging.info(f'!! 连续 {MAX_CONSECUTIVE_EMPTY} 天 0 model, 推测账号到此为止, 停止往老扫')
                break
        else:
            ok += 1
            consecutive_empty = 0
        consecutive_fail = 0
        if (ok + empty) % 30 == 0:
            logging.info(f'{day} 进度: ok={ok}, empty={empty}, fail={fail}')
    except Exception as e:
        msg = str(e)[:120]
        fail += 1
        consecutive_fail += 1
        logging.warning(f'{day} ✗ ({fail}): {msg}')
        if consecutive_fail >= MAX_CONSECUTIVE_FAILS:
            logging.error(f'!! 连续 {MAX_CONSECUTIVE_FAILS} 次失败, ABORT')
            break
    day -= dt.timedelta(days=1)

logging.info(f'收尾: ok={ok} empty={empty} fail={fail}, 最后停在 {day}')
