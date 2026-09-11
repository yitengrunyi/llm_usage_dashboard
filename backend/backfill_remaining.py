"""服务器 backfill — xhub + openai 并行, 一个命令完事.

用法 (服务器, 后台跑):
    cd /path/to/llm-usage-dashboard/backend
    nohup python3 backfill_remaining.py > /tmp/backfill.log 2>&1 &
    tail -f /tmp/xhub_backfill.log /tmp/openai_backfill.log

两个独立子进程并行:
  1. xhub (nulls): 两段**固定范围** 不撞空停 (避免 25/11~26/02 的 4 个月 gap 误停)
     - 中段 2026-03-01 ~ 2026-04-28 (~60 天, raw log 5-7 分钟/天)
     - 老段 2025-07-01 ~ 2025-10-31 (~123 天)
     合计 ~183 天 × 6 分钟 ≈ 18 小时
  2. openai: 从 DB 现存最老一天 - 1 往老挖, 撞 14 天连空停 (~3-5 小时)

中间断了重启都没事 — UPSERT 幂等, 同一 vendor uniq_running 索引防并发.
"""
import sys, os, time, logging, datetime as dt
import multiprocessing

# 脚本可放项目根 或 backend/ 任一位置, 自动找到 backend/ 目录
HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = HERE if os.path.basename(HERE) == 'backend' else os.path.join(HERE, 'backend')
if not os.path.isdir(os.path.join(BACKEND, 'ingest')):
    raise SystemExit(f'找不到 backend/ingest, 当前 BACKEND={BACKEND}')
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

EMPTY_THRESHOLD = 14   # openai 撞这么多天连空就停


def _setup_log(vid: str):
    logfile = f'/tmp/{vid}_backfill.log'
    logging.basicConfig(
        level=logging.INFO, force=True,
        format=f'%(asctime)s [{vid}] %(message)s',
        handlers=[logging.FileHandler(logfile), logging.StreamHandler()],
    )
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)


def run_range_one_vendor(vendor_id: str, ranges: list[tuple[dt.date, dt.date]],
                          sleep_between: float = 2.0):
    """跑 vendor 的多个固定范围 — 不撞空停, 范围跑完为止."""
    _setup_log(vendor_id)
    from ingest.job import run_ingest
    for r_idx, (start, end) in enumerate(ranges):
        logging.info(f'─── 范围 {r_idx+1}/{len(ranges)}: {start} ~ {end}, {(end-start).days+1} 天 ───')
        day = end
        ok = 0; fail = 0
        while day >= start:
            success = False; rid = None
            for attempt in range(3):
                try:
                    rid = run_ingest(vendor_id, day, day, trigger='server_backfill')
                    success = True; break
                except Exception as e:
                    wait = 30 * (attempt + 1)
                    logging.warning(f'{day} attempt {attempt+1}/3 失败: {str(e)[:200]}, 等 {wait}s')
                    time.sleep(wait)
            if success:
                logging.info(f'{day} ✓ run_id={rid}')
                ok += 1
            else:
                logging.error(f'{day} 3 次失败, 跳过')
                fail += 1
            day -= dt.timedelta(days=1)
            time.sleep(sleep_between)
        logging.info(f'范围 {r_idx+1} 完: ✓ {ok}, ✗ {fail}')
    logging.info(f'全部范围跑完')


def run_until_empty_one_vendor(vendor_id: str, sleep_between: float = 2.0):
    """从 DB 最老 - 1 往老挖, 撞 EMPTY_THRESHOLD 天连空停. 给 openai 用."""
    _setup_log(vendor_id)
    from ingest.job import run_ingest, _load_vendor_config
    from ingest.adapters import ADAPTERS
    from ingest.db import SessionLocal
    from sqlalchemy import text
    with SessionLocal() as s:
        earliest = s.execute(text(
            'SELECT MIN(usage_date) FROM vendor_usage_daily WHERE vendor_id=:v'
        ), {'v': vendor_id}).scalar()
    if not earliest:
        logging.error(f'DB 无数据, 退出')
        return
    day = earliest - dt.timedelta(days=1)
    logging.info(f'从 {day} 往老挖, 撞 {EMPTY_THRESHOLD} 天连空停')
    adapter_cls = ADAPTERS[vendor_id]
    vendor = _load_vendor_config(vendor_id)
    adapter = adapter_cls(vendor)
    empty_streak = 0; ok = 0; fail = 0
    while empty_streak < EMPTY_THRESHOLD:
        rows = None
        for attempt in range(3):
            try:
                rows = adapter.fetch_one_day(day); break
            except Exception as e:
                wait = 30 * (attempt + 1)
                logging.warning(f'{day} fetch attempt {attempt+1}/3 失败: {str(e)[:200]}, 等 {wait}s')
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
                rid = run_ingest(vendor_id, day, day, trigger='server_backfill')
                logging.info(f'{day} ✓ run_id={rid} models={len(rows)}')
                ok += 1
            except Exception as e:
                logging.error(f'{day} 入库失败: {e}'); fail += 1
        day -= dt.timedelta(days=1)
        time.sleep(sleep_between)
    logging.info(f'收尾: ✓ {ok}, ✗ {fail}, 撞 {EMPTY_THRESHOLD} 天连空停')


if __name__ == '__main__':
    print('启动并行 backfill...')

    # 启动前 cleanup: 把所有 stale running run 标成 failed
    # 避免之前 kill / 崩溃留下的 status='running' 行卡住 uniq_running_per_vendor 索引
    from ingest.db import SessionLocal
    from sqlalchemy import text
    with SessionLocal() as _s:
        _r = _s.execute(text(
            "UPDATE vendor_ingest_run SET status='failed', "
            "error_msg='stale running auto-cleanup', finished_at=now() "
            "WHERE status='running'"
        ))
        _s.commit()
        if _r.rowcount:
            print(f'清理 {_r.rowcount} 个 stale running run')

    # xhub: 两段固定范围, 跳过 25/11~26/02 的 4 个月 gap
    XHUB_RANGES = [
        (dt.date(2026, 3, 1),  dt.date(2026, 4, 28)),   # 中段
        (dt.date(2025, 7, 1),  dt.date(2025, 10, 31)),  # 老段
    ]
    p_xhub = multiprocessing.Process(
        target=run_range_one_vendor, args=('xhub', XHUB_RANGES, 2.0),
        name='xhub-backfill',
    )

    # openai: 撞 14 天连空停
    p_oai = multiprocessing.Process(
        target=run_until_empty_one_vendor, args=('openai', 1.0),
        name='openai-backfill',
    )

    p_xhub.start()
    p_oai.start()
    print(f'xhub PID={p_xhub.pid}, openai PID={p_oai.pid}')
    print(f'日志: /tmp/xhub_backfill.log, /tmp/openai_backfill.log')
    print(f'等两个进程结束...')

    p_xhub.join()
    p_oai.join()
    print('全部完成')
