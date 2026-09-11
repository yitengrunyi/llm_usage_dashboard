"""ingest + points 定时调度 — backend 启动时起, 跟 FastAPI 同进程.

任务:
- 02:30 CST: 登录型 vendor session 预检 — 过期且配了账密 → 自动登录 (赶在摄取前)
- 03:00 CST: 10 vendor 并发跑快路径, 从 MAX(已入库)+1 到昨天.
             blueshirt 快路径成功后立刻接慢路径补拆分字段 (单线程内串行, 不另开 cron)
- 03:30 CST: 积分预聚合表增量更新（更新昨天的数据到 point_daily_summary）
- 01:00 PT:  openai 单独 cron (PT 自然日切, CST 时间约 17:00, 自动处理夏令时)

为啥 3:00 不是 1:00: kimi/apevon 等代理平台账单 T+1 出账, 1:00 太早拉到空 list →
job 写 0 行 → 那天数据看着是 0. 改 3:00 给上游 3 小时出账时间.

为啥 openai 拆出来: openai 账单按 PT 自然日切, CST 1:00 时 PT 还在"今天"上午,
PT "昨天" 那一天还没结束, 拉的数据不完整. 让 openai 在 PT 1:00 跑 PT 昨天.

为啥并发 (原先串行): 串行 10 vendor 加重试退避能拖到 1.5h+, blueshirt 排在第 6,
后面的慢路径不得不依赖时序假设 — 不可靠. 改并发后:
- 10 家上游域名不同, 不互相挤限流; 各 adapter 自己内部已有并发控制
- 总耗时 = max(单家), 一般 < 5min
- blueshirt 慢路径直接接在自己快路径后跑, 不再依赖独立 03:30 cron

为啥积分在 03:30: 在 vendor ingest (03:00) 完成后跑，避免时序问题。
积分预聚合是独立的 MySQL 表，不依赖 vendor 数据，但放在后面更保险。

错过策略: misfire_grace_time=1, backend 1:00 时正在重启就跳过, 等明天.
(用户决策 — 不做补跑, 避免不可预期时刻跑 backfill.)
但每天 cron 走的是 [MAX(已入库)+1, 昨天] 的补缺语义, 所以错过一天明天会自动补.

注意: 假设 uvicorn 单 worker. 多 worker 起此 scheduler 会重复执行,
需要补文件锁或拆独立 cron 容器.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from datetime import timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.events import (
    EVENT_JOB_EXECUTED, EVENT_JOB_ERROR, EVENT_JOB_MISSED, EVENT_JOB_SUBMITTED,
)

try:
    from zoneinfo import ZoneInfo
    PT = ZoneInfo("America/Los_Angeles")
except ImportError:
    PT = timezone(timedelta(hours=-8))  # fallback, 不处理 DST

CST = timezone(timedelta(hours=8))
log = logging.getLogger("ingest.scheduler")

# 这俩上游账单 T+N 出账延迟, 3:00 跑常拿空, 单独挪到 7:00 给上游 4 小时落账时间
LATE_VENDORS = {"kimi", "apevon"}

# openai /costs 账单在 PT 日切后数天内持续补记, cron 每天回看重拉这么多天
# (窗口内 UPSERT 幂等覆盖; 14 天 × ~10s/天 ≈ 2.5 分钟, 上游扛得住)
OPENAI_LOOKBACK_DAYS = 14

_scheduler: BackgroundScheduler | None = None


def _yesterday() -> dt.date:
    """CST 算的昨天 — 大部分 vendor 按 CST 自然日入库."""
    return (dt.datetime.now(CST) - timedelta(days=1)).date()


def _pt_yesterday() -> dt.date:
    """PT 算的昨天 — openai 账单按 PT 自然日切, 用这个."""
    return (dt.datetime.now(PT) - timedelta(days=1)).date()


def _cron_precheck_login() -> None:
    """CST 02:30 — 登录型 vendor session 预检: 过期且 .env 配了账密 → 自动登录.

    赶在 03:00 摄取 cron 之前修好, session 过期不再造成摄取失败.
    没配账密的 vendor 不弹浏览器 (无人时段没意义), 摄取失败后 agent 巡检走 noVNC.
    每 vendor 每天最多自动尝试 1 次 (防密码改了反复撞登录), 失败推飞书.
    """
    from login_flow import precheck_auto_login
    try:
        precheck_auto_login()
    except Exception as e:
        log.error(f"[cron-precheck] 登录预检异常: {e}", exc_info=True)


def _cron_all_fast_cst() -> None:
    """CST 03:00 — 除 openai 外 10 个 vendor **并发**跑快路径.

    每家从 MAX(已入库)+1 到 CST 昨天 (补缺语义). backend 挂几天 / 上游限流连失败, 自动补回.
    全新 vendor (DB 没记录) 默认拉近 7 天, 避免一上来就大跨度.

    并发安全:
    - 10 家上游域名都不同, 不互相挤限流; 每家 adapter 内部已有自己的并发控制
    - SessionLocal() 每次新建 session, 线程间不共享
    - vendor_ingest_run.uniq_running_per_vendor 部分唯一索引只防"同 vendor 并发", 不阻跨 vendor
    - _refresh_usage_daily_total 多 vendor 并发刷同一天的 total 行, PG 行锁 + READ COMMITTED 串行化

    blueshirt 快路径成功后**立刻接慢路径** (单线程内串行), 不依赖独立 03:30 cron.
    这样避免"快路径还没跑到 blueshirt 时慢路径已经查了空"的时序 bug.

    一个失败不影响其他.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from ingest.adapters import ADAPTERS
    from ingest.job import run_ingest_with_retry
    from ingest.query import get_freshness

    yesterday = _yesterday()
    freshness = get_freshness()
    cst_vendors = [v for v in ADAPTERS if v != "openai" and v not in LATE_VENDORS]
    log.info(f"[cron-cst] 开始 — {len(cst_vendors)} vendor 并发, end={yesterday}")

    def _run_one(vid: str) -> tuple[str, str]:
        synced = freshness.get(vid)
        start = (synced + timedelta(days=1)) if synced else yesterday - timedelta(days=6)
        if start > yesterday:
            return vid, f"skip (synced={synced})"
        try:
            run_ingest_with_retry(vid, start, yesterday, trigger="cron")
        except Exception as e:
            return vid, f"fast failed: {e}"

        # blueshirt/nulls 快成功 → 立刻接慢路径补 prompt/comp/cache 拆分字段.
        # 两家都是 new-api 系, 快路径走 /api/data/self 不返拆分, 慢路径走 /api/log/self 拿.
        # 通过 job.run_slow_fill_dedicated 跑 — 它建 trigger='slow-fill' 的 VendorIngestRun,
        # 前端 latestRun 自然能看到这一步的 running/success/failed (不光是消失的黄色 alert).
        if vid in ("blueshirt", "nulls"):
            try:
                from ingest.job import run_slow_fill_dedicated
                run_slow_fill_dedicated(vid, yesterday, trigger="slow-fill")
                return vid, "ok + slow"
            except Exception as e:
                return vid, f"fast ok, slow failed: {e}"
        return vid, "ok"

    with ThreadPoolExecutor(max_workers=len(cst_vendors)) as ex:
        futs = {ex.submit(_run_one, vid): vid for vid in cst_vendors}
        for fut in as_completed(futs):
            vid, msg = fut.result()
            log.info(f"[cron-cst] {vid} → {msg}")
    log.info(f"[cron-cst] 全部跑完 end={yesterday}")


def _cron_late_vendors_cst() -> None:
    """CST 07:00 — kimi + apevon 单独跑 (上游账单 T+N 延迟, 3:00 跑常拿空).

    给上游 4 小时落账时间. 跟主 cron 同款逻辑 (并发 + freshness 补缺), 只是 vendor 名单不同.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from ingest.job import run_ingest_with_retry
    from ingest.query import get_freshness

    yesterday = _yesterday()
    freshness = get_freshness()
    log.info(f"[cron-late] 开始 — {len(LATE_VENDORS)} vendor (kimi/apevon), end={yesterday}")

    def _run_one(vid: str) -> tuple[str, str]:
        synced = freshness.get(vid)
        start = (synced + timedelta(days=1)) if synced else yesterday - timedelta(days=6)
        if start > yesterday:
            return vid, f"skip (synced={synced})"
        try:
            run_ingest_with_retry(vid, start, yesterday, trigger="cron")
            return vid, "ok"
        except Exception as e:
            return vid, f"failed: {e}"

    with ThreadPoolExecutor(max_workers=len(LATE_VENDORS)) as ex:
        futs = {ex.submit(_run_one, vid): vid for vid in LATE_VENDORS}
        for fut in as_completed(futs):
            vid, msg = fut.result()
            log.info(f"[cron-late] {vid} → {msg}")
    log.info(f"[cron-late] 全部跑完 end={yesterday}")


def _cron_openai_pt() -> None:
    """PT 1:00 — openai 单独 cron (账单按 PT 自然日切).

    CST 时间约 17:00, APScheduler 自动跟随夏令时 (PDT/PST). 拉 [MAX(已入库)+1, PT 昨天].
    PT 昨天此时已结束 1 小时, 数据已可用.
    """
    from ingest.job import run_ingest_with_retry
    from ingest.query import get_freshness

    yesterday_pt = _pt_yesterday()
    synced = get_freshness().get("openai")
    # OpenAI /costs 账单在 PT 日切后**数天内持续补记** — 实测 06-27~07-02 六天,
    # PT+1h 拉到的只有终值的 1/2 ~ 1/4.5 (入库 $1275 vs 上游终值 $3780)。
    # 只覆盖 yesterday_pt 会把 day-1 快照永久当终值 → 每天固定回看重拉近
    # OPENAI_LOOKBACK_DAYS 天, UPSERT 幂等覆盖, 自动吸收补记漂移。
    # 有更老的 gap (backend 挂过几天) 就从 synced+1 一起补回来。
    lookback_start = yesterday_pt - timedelta(days=OPENAI_LOOKBACK_DAYS - 1)
    if synced and synced < lookback_start:
        start = synced + timedelta(days=1)
    elif synced:
        start = lookback_start
    else:  # 第一次跑
        start = yesterday_pt - timedelta(days=6)
    try:
        run_ingest_with_retry("openai", start, yesterday_pt, trigger="cron")
        log.info(f"[cron-pt] openai {start}~{yesterday_pt} ok")
    except Exception as e:
        log.error(f"[cron-pt] openai {start}~{yesterday_pt} failed: {e}")


def _cron_recheck_yesterday_cst() -> None:
    """CST 23:00 — 所有非 openai vendor 重跑昨天一遍, 静默兜底.

    动机: 03:00 / 07:00 主跑时上游某些接口可能未完整出账 / 漏聚合
    (e.g. 火山 Bill 接口偶尔返空, statistics 漏 model 拆分, retention 类问题),
    导致 cost=0 / 拆分缺失的"潜伏入库" — 主路径都是 success run 不会报错,
    用户看 dashboard 才发现某天数字异常. 11 PM 上游通常已经把当天 (CST 昨天)
    账单/聚合补齐, 再拉一遍 UPSERT 覆盖.

    UPSERT 幂等 → 上游没变跟 DB 一致 = no-op (DB 层值无变化); 上游补了 → DB 跟上.

    设计:
    - openai 走 PT 时区 ≠ CST 昨天, 跳过 (它有自己的 17:00 PT 1:00 cron)
    - 失败彻底静默: 不推飞书, 不留 failed run — 这是兜底层, 主路径已经报过了
    - trigger='recheck' 标识, run 表里可查但 alert 路径不触发
    - 串行跑 (不抢上游配额, 23:00 不赶时间)
    """
    from ingest.adapters import ADAPTERS
    from ingest.job import run_ingest
    yesterday = _yesterday()
    recheck_vids = [v for v in ADAPTERS if v != "openai"]
    log.info(f"[recheck-cst] 开始 {len(recheck_vids)} vendor recheck for {yesterday}")

    ok = fail = 0
    for vid in recheck_vids:
        try:
            # 直接 run_ingest 不走 retry wrapper — wrapper 会推飞书, recheck 不应该报警
            run_ingest(vid, yesterday, yesterday, trigger="recheck")
            ok += 1
        except Exception as e:
            # 静默: 主路径已经报过 / 没报就是无伤大雅. 这里只 warning, 不 alert.
            log.warning(f"[recheck-cst] {vid} {yesterday} silent fail: {type(e).__name__}: {e}")
            fail += 1

    log.info(f"[recheck-cst] 全部跑完 {yesterday}: ok={ok} fail={fail}")


def _cron_blueshirt_slow() -> None:
    """blueshirt /api/log/self 拿 prompt/completion/cache 拆分.

    现在不再单独 cron 触发 — 已经合并到 _cron_all_fast_cst 里 (blueshirt 快成功后立刻跑).
    保留这个函数: 启动时旧 cron 残留任务可能调到; 手动测试用 (容器里 python -c 直接调).

    单独跑场景: 跑 CST 昨天. retention ~13 天, 老天上游返 total=0 跑也无效.
    走 run_slow_fill_dedicated 建 run 行, 前端能看到状态.
    """
    from ingest.job import run_slow_fill_dedicated

    day = _yesterday()
    try:
        run_slow_fill_dedicated("blueshirt", day, trigger="slow-fill")
        log.info(f"[cron-slow] blueshirt {day}: ok")
    except Exception as e:
        log.error(f"[cron-slow] blueshirt {day} failed: {e}")



def _cron_update_points_summary() -> None:
    """CST 03:30 — 增量更新昨天的积分预聚合数据.

    两层聚合:
    1. point_consumption_record → point_daily_summary (按日期+业务类型+子类型)
       走 create_time 索引范围查询，秒级完成
    2. point_daily_summary → point_usage_daily_total (按日期总计)

    采用增量更新策略, 幂等安全.

    为什么在 03:30 跑: 在 vendor ingest 主任务（03:00）完成后，
    时间上有缓冲。积分数据来自独立的 MySQL 表，不直接依赖 vendor ingest，
    但放在后面避免资源竞争。

    错过一天会自动补: 每天都更新昨天，缺失的数据下次运行时会被补上。
    """
    from init_points_summary import aggregate_batch, create_engine_and_session

    yesterday = _yesterday()

    log.info(f"[cron-points] 开始更新积分预聚合 — date={yesterday}")

    try:
        engine, SessionLocal = create_engine_and_session()
        session = SessionLocal()

        # 第一层：增量更新昨天的数据到 point_daily_summary（走 create_time 索引）
        inserted = aggregate_batch(session, str(yesterday), str(yesterday))
        log.info(f"[cron-points] point_daily_summary {yesterday} 完成 — 更新了 {inserted} 条聚合记录")

        # 第二层：从 point_daily_summary 聚合到 point_usage_daily_total（幂等操作）
        from init_points_daily_total import aggregate_from_summary
        total_inserted = aggregate_from_summary(session, str(yesterday), str(yesterday))
        log.info(f"[cron-points] point_usage_daily_total {yesterday} 完成 — 更新了 {total_inserted} 条记录")

        session.close()
        engine.dispose()

        log.info(f"[cron-points] {yesterday} 全部完成")
    except Exception as e:
        log.error(f"[cron-points] {yesterday} 失败: {e}", exc_info=True)


def _setup_logging() -> None:
    """让 ingest.* 的 log 走到 stdout (docker logs 可见) + 文件 (data/logs/scheduler.log).

    backend 没全局 logging.basicConfig, uvicorn 默认 WARNING 把 info 级吞了.
    我们自己挂 handler, 只 attach 到 'ingest' / 'apscheduler' 两个 namespace.
    """
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")
    log_dir = Path(__file__).parent.parent / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    fh = logging.FileHandler(log_dir / "scheduler.log", encoding="utf-8")
    fh.setFormatter(fmt)

    for name in ("ingest", "apscheduler"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        # 避免重复挂 (start 重复调用 or 多 worker)
        if not any(getattr(h, "_ingest_tag", False) for h in lg.handlers):
            sh._ingest_tag = True
            fh._ingest_tag = True
            lg.addHandler(sh)
            lg.addHandler(fh)
            lg.propagate = False


def _on_job_event(event) -> None:
    """job 状态变化打 log — 跑了 / 错了 / 错过."""
    jid = event.job_id
    if event.code == EVENT_JOB_SUBMITTED:
        log.info(f"[event] {jid} submitted")
    elif event.code == EVENT_JOB_EXECUTED:
        log.info(f"[event] {jid} executed ok")
    elif event.code == EVENT_JOB_ERROR:
        log.error(f"[event] {jid} ERROR: {event.exception}")
    elif event.code == EVENT_JOB_MISSED:
        log.warning(f"[event] {jid} MISSED scheduled_run_time={event.scheduled_run_time}")


def _cleanup_zombie_runs() -> None:
    """backend startup 时扫一遍 status='running' 但已经超 2 小时的 run, 标 failed.

    实际逻辑在 job.mark_stale_running_failed (agent 巡检也复用它), 这里只做启动时调用.
    同步清掉 agent_patrol_report 里遗留的 running 行 (重启中断的巡检),
    否则 /agent 页面会永远"巡检进行中"轮询.
    """
    from ingest.job import mark_stale_running_failed
    mark_stale_running_failed(
        threshold_hours=2.0,
        note="[zombie cleanup] backend startup detected status=running for > 2h, marked failed",
    )
    from ingest.agent.report import mark_stale_running_failed as mark_stale_reports
    mark_stale_reports()


def _cron_agent_patrol(slot: str) -> None:
    """自愈 Agent 巡检 — 信号收集 → LLM 诊断 → 安全修复 → 飞书报告.

    run_patrol 内部全捕获 (cron 永不因 agent 报 ERROR); 干净时零 LLM 成本直接退出.
    """
    from ingest.agent.patrol import run_patrol
    run_patrol(slot=slot)


def start() -> None:
    """backend startup 调用. 重复 no-op."""
    global _scheduler
    if _scheduler:
        return
    _setup_logging()
    _cleanup_zombie_runs()
    s = BackgroundScheduler(timezone=CST, job_defaults={
        "coalesce": True,         # 多次错过合并成一次
        "misfire_grace_time": 1,  # >1s 错过即跳过
        "max_instances": 1,       # 同 job 不并发
    })
    s.add_listener(
        _on_job_event,
        EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED | EVENT_JOB_SUBMITTED,
    )
    # 02:30 CST 登录预检 — 过期 session 自动登录 (配了 .env 账密的), 赶在 03:00 摄取前
    s.add_job(_cron_precheck_login, CronTrigger(hour=2, minute=30, timezone=CST),
              id="login_precheck_autologin", name="login:precheck (CST 2:30, 自动登录过期 session)")
    # CST 自然日 vendor — 03:00 触发, 9 家并发 (除 openai + LATE_VENDORS).
    # blueshirt/nulls 慢路径合并到快路径后跑.
    s.add_job(_cron_all_fast_cst, CronTrigger(hour=3, minute=0, timezone=CST),
              id="ingest_all_fast_cst", name="ingest:all-fast (CST 3:00, 9 家并发)")
    # LATE_VENDORS (kimi + apevon) — 07:00 触发, 给上游账单 T+N 4 小时落账时间
    s.add_job(_cron_late_vendors_cst, CronTrigger(hour=7, minute=0, timezone=CST),
              id="ingest_late_vendors_cst", name="ingest:late-vendors (CST 7:00, kimi+apevon)")
    # openai 单独 cron — PT 1:00 (CST 约 17:00, APScheduler 自动 DST)
    s.add_job(_cron_openai_pt, CronTrigger(hour=1, minute=0, timezone=PT),
              id="ingest_openai_pt", name="ingest:openai (PT 1:00)")
    # 23:00 CST 昨天复核 — 所有非 openai vendor 静默重跑 (修上游慢出账 / 漏聚合)
    s.add_job(_cron_recheck_yesterday_cst, CronTrigger(hour=23, minute=0, timezone=CST),
              id="ingest_recheck_cst", name="ingest:recheck (CST 23:00, 复核昨天)")
    # 03:30 CST 积分预聚合更新 — 在 vendor ingest 完成后更新昨天的积分数据
    s.add_job(_cron_update_points_summary, CronTrigger(hour=3, minute=30, timezone=CST),
              id="points_daily_summary", name="points:daily-summary (CST 3:30, 更新昨天)")
    # 自愈 Agent 巡检 — 三个时段各复核一轮, 时点选在各 cron 的完整重试链
    # (最晚 ~100min) 结束之后; 23:00 recheck 后不巡 (该层故意静默, 次日 05:00 评判).
    # AGENT_PATROL_ENABLED=0 可整体关闭.
    if os.environ.get("AGENT_PATROL_ENABLED", "1") != "0":
        s.add_job(lambda: _cron_agent_patrol("05:00"), CronTrigger(hour=5, minute=0, timezone=CST),
                  id="agent_patrol_morning", name="agent:patrol (CST 05:00, 复核 03:00 批)")
        s.add_job(lambda: _cron_agent_patrol("09:00"), CronTrigger(hour=9, minute=0, timezone=CST),
                  id="agent_patrol_noon", name="agent:patrol (CST 09:00, 复核 07:00 批)")
        s.add_job(lambda: _cron_agent_patrol("19:00"), CronTrigger(hour=19, minute=0, timezone=CST),
                  id="agent_patrol_evening", name="agent:patrol (CST 19:00, 复核 openai PT 批)")
    s.start()
    _scheduler = s
    jobs = s.get_jobs()
    log.info(f"ingest scheduler started, jobs={[(j.id, str(j.next_run_time)) for j in jobs]}")



def shutdown() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        log.info("ingest scheduler shutdown")
