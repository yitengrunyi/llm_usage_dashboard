"""kimi 回填 — 探到 2024-06-15 有数据, DB 现 2025-07-22, 中间补 14 个月.

跟 xhub 脚本的关键区别:
- kimi 历史**有空档** (某些月份没用过), 不能像 xhub 那样"3 次失败 abort"
- 用更长的 abort 阈值 (10 次), 容忍空档但防真挂掉
- 跳过 DB 里已落的天
"""
import sys, os, logging, datetime as dt

# 自动适配 server (/app/) vs 本地 (backend/)
HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.isdir(os.path.join(HERE, 'ingest')):
    BACKEND = HERE                      # 本地: backend/ 目录
elif os.path.isdir('/app/ingest'):
    BACKEND = '/app'                    # 服务器: /app/ 是 backend
else:
    raise SystemExit('找不到 ingest/')
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

# 读 .env (本地需要; 服务器 docker 已注入环境变量)
env_path = os.path.join(BACKEND, '.env')
if os.path.exists(env_path):
    for line in open(env_path):
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s [kimi] %(message)s',
    handlers=[logging.FileHandler('/tmp/kimi_backfill.log'), logging.StreamHandler()])
from ingest.job import run_ingest
from ingest.db import SessionLocal
from sqlalchemy import text

# 启动前清 stale running
with SessionLocal() as s:
    s.execute(text("UPDATE vendor_ingest_run SET status='failed', error_msg='kimi auto-cleanup', finished_at=now() WHERE vendor_id='kimi' AND status='running'"))
    s.commit()

# 已有的天 (跳过)
with SessionLocal() as s:
    existing = set(s.execute(text(
        "SELECT usage_date FROM vendor_usage_daily WHERE vendor_id='kimi'"
    )).scalars())
logging.info(f'已落库 {len(existing)} 天, 会跳过')

# 探到 2024-06 有数据, 留点 buffer 试到 2023-06
START = dt.date(2023, 6, 1)
# DB 现最早 2025-07-22, 从前一天倒推
END = dt.date(2025, 7, 21)

MAX_CONSECUTIVE_API_FAILS = 10   # API 真挂(报错抛异常) 10 次连续才停; 0 行不算

ok = skip = empty = api_fail = 0
consecutive_api_fail = 0
day = END
logging.info(f'范围: {END} → {START}, 共 {(END - START).days + 1} 天')

while day >= START:
    if day in existing:
        day -= dt.timedelta(days=1); skip += 1
        continue
    try:
        run_ingest('kimi', day, day, trigger='backfill')
        ok += 1
        consecutive_api_fail = 0
        if ok % 30 == 0:
            logging.info(f'{day} 进度: ok={ok}, skip={skip}, empty={empty}, fail={api_fail}')
    except Exception as e:
        msg = str(e)[:100]
        # 上游报错 / 网络异常 → 计数; 但"无数据"通常不抛异常 (rows=[] 走 ok 路径)
        api_fail += 1
        consecutive_api_fail += 1
        if api_fail % 5 == 0:
            logging.warning(f'{day} ✗ ({api_fail} 失败): {msg}')
        if consecutive_api_fail >= MAX_CONSECUTIVE_API_FAILS:
            logging.error(f'!! 连续 {MAX_CONSECUTIVE_API_FAILS} 次 API 失败, ABORT 防打死上游')
            break
    day -= dt.timedelta(days=1)

logging.info(f'收尾: ok={ok} skip={skip} fail={api_fail}, 最后停在 {day}')
