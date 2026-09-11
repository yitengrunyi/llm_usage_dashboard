"""FastAPI 路由: ingest trigger / retry / status / state 查询.

设计:
- 每个 vendor 一个独立 endpoint, xxl-job 配 N 个 task 并发触发
- trigger 立即返回 run_id, BackgroundTasks 异步跑 (xhub 几分钟 / xxl-job HTTP 通常 30s 超时)
- 鉴权 — INGEST_TOKEN header, 防误触发 / 外网直调
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, select

from ingest.db import SessionLocal
from ingest.job import run_ingest, run_ingest_with_retry
from ingest.models import VendorIngestRun, VendorIngestState

log = logging.getLogger("ingest.router")

router = APIRouter(prefix="/api/ingest", tags=["ingest"])

# 鉴权: 设了 token 就校验; 没设跳过 (本地开发方便)
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "").strip()


def _check_token(token: str | None) -> None:
    if not INGEST_TOKEN:
        return  # 没配 token = 关闭鉴权 (本地)
    if token != INGEST_TOKEN:
        raise HTTPException(status_code=401, detail="invalid X-Ingest-Token")


class TriggerBody(BaseModel):
    start: dt.date | None = None  # 默认从 state.last_ingested_date+1
    end: dt.date | None = None    # 默认昨天 (今天数据走 live 拼接, 不入库)


@router.post("/vendors/{vendor_id}/trigger")
def trigger_ingest(
    vendor_id: str,
    body: TriggerBody,
    background: BackgroundTasks,
):
    """触发单个 vendor 的入库. 立即返 run_id, 后台跑. 前端面板用 — 公开 (无 token).

    默认: 从 MAX(已入库的天) + 1 → 今天 0 点之前 (= 昨天).
    取 MAX(vendor_usage_daily.usage_date) 而不是 state, 避免 backfill 把 state 覆盖成最旧.

    openai 时区注意: 账单按 PT 自然日切, CST 昨天 ≠ PT 昨天. 但本 endpoint 不 clamp,
    允许用户手动拉"今天 PT 那天能拿多少算多少". scheduler cron 走 PT 1:00 触发,
    那时 PT 昨天已完整 — 第二天会重跑覆盖, 不影响最终数据完整性.
    """
    # 默认窗口: 已入库最新天+1 → CST 昨天
    yesterday = dt.date.today() - dt.timedelta(days=1)
    start = body.start
    end = body.end or yesterday

    if start is None:
        from ingest.query import get_freshness
        synced = get_freshness().get(vendor_id)
        if synced:
            start = synced + dt.timedelta(days=1)
        else:
            # 第一次跑, 默认回填 7 天
            start = end - dt.timedelta(days=6)

    if start > end:
        return {"vendor_id": vendor_id, "skipped": True,
                "reason": f"start {start} > end {end}, already up-to-date"}

    # 后台跑, 立即返回. 走 retry wrapper — 失败 3 次后自动推飞书.
    # trigger='manual' — 外部 HTTP 调进来的, 跟 scheduler cron 区分开方便审计
    # blueshirt/nulls 都是 new-api 系 (快 data/self + 慢 log/self) → dual 路径
    if vendor_id in ("blueshirt", "nulls"):
        background.add_task(_retry_dual_path, vendor_id, start, end)
        mode = "dual (fast then slow)"
    else:
        def _bg():
            try:
                run_ingest_with_retry(vendor_id, start, end, trigger="manual")
            except Exception as e:
                log.error(f"[{vendor_id}] background ingest failed after retries: {e}")
        background.add_task(_bg)
        mode = "fast"

    return {"vendor_id": vendor_id, "start": start.isoformat(),
            "end": end.isoformat(), "mode": mode, "status": "scheduled"}


def _retry_dual_path(vendor_id: str, start: dt.date, end: dt.date) -> None:
    """new-api 系 (blueshirt/nulls) 重试 — 串行先快后慢.

    快失败就不跑慢 (没 cost 行, 慢补拆分字段没意义).
    慢只补窗口最后一天 (跟 cron 行为一致, /api/log/self retention 有限, 老天空跑没用).
    """
    try:
        run_ingest_with_retry(vendor_id, start, end, trigger="manual")
        log.info(f"[retry-dual] {vendor_id} fast {start}~{end} ok")
    except Exception as e:
        log.error(f"[retry-dual] {vendor_id} fast failed, skip slow: {e}")
        return

    try:
        if vendor_id == "blueshirt":
            from ingest.blueshirt_slow import slow_fill_day
            stats = slow_fill_day(end)
        else:  # nulls
            from ingest.newapi_direct_slow import slow_fill_day
            from vendors import get_vendor
            stats = slow_fill_day(get_vendor(vendor_id), end)
        log.info(f"[retry-dual] {vendor_id} slow {end}: {stats}")
    except Exception as e:
        log.error(f"[retry-dual] {vendor_id} slow {end} failed: {e}")


@router.post("/runs/{run_id}/retry")
def retry_run(
    run_id: int,
    background: BackgroundTasks,
):
    """手动重试失败的 run, 沿用原 vendor/window. 前端面板用 — 公开 (无 token).

    blueshirt 双路径: 快 /api/data/self (cost/total/req) + 慢 /api/log/self (拆分).
    重试时串行先快后慢, 快失败就不跑慢. 其他 vendor 单路径保持原逻辑.
    """
    with SessionLocal() as s:
        run = s.get(VendorIngestRun, run_id)
        if not run:
            raise HTTPException(404, f"run {run_id} not found")
        vendor_id = run.vendor_id
        start = run.window_start
        end = run.window_end

    if vendor_id in ("blueshirt", "nulls"):
        background.add_task(_retry_dual_path, vendor_id, start, end)
        mode = "dual (fast then slow)"
    else:
        def _bg():
            try:
                run_ingest_with_retry(vendor_id, start, end, trigger="manual")
            except Exception as e:
                log.error(f"[{vendor_id}] retry failed: {e}")
        background.add_task(_bg)
        mode = "fast"

    return {"vendor_id": vendor_id, "start": start.isoformat(),
            "end": end.isoformat(), "mode": mode, "status": "retry_scheduled"}


@router.get("/runs/{run_id}")
def get_run(run_id: int):
    """查任务状态 — xxl-job 轮询用."""
    with SessionLocal() as s:
        run = s.get(VendorIngestRun, run_id)
        if not run:
            raise HTTPException(404, "run not found")
        return {
            "id": run.id, "vendor_id": run.vendor_id,
            "window_start": run.window_start.isoformat(),
            "window_end": run.window_end.isoformat(),
            "trigger": run.trigger, "status": run.status, "attempt": run.attempt,
            "rows_upserted": run.rows_upserted, "error_msg": run.error_msg,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        }


@router.get("/runs")
def list_runs(vendor_id: str | None = Query(None), limit: int = Query(20, le=100)):
    """任务历史 — 前端 vendor 详情页拿"上次同步时间" 用."""
    with SessionLocal() as s:
        q = select(VendorIngestRun).order_by(desc(VendorIngestRun.started_at)).limit(limit)
        if vendor_id:
            q = q.where(VendorIngestRun.vendor_id == vendor_id)
        rows = s.execute(q).scalars().all()
        return [
            {
                "id": r.id, "vendor_id": r.vendor_id,
                "window_start": r.window_start.isoformat(),
                "window_end": r.window_end.isoformat(),
                "trigger": r.trigger, "status": r.status, "attempt": r.attempt,
                "rows_upserted": r.rows_upserted, "error_msg": r.error_msg,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            } for r in rows
        ]


# 慢路径补字段:
# - blueshirt/nulls: 调 job.run_slow_fill_dedicated (它建 trigger='slow-fill' run 行)
# - apevon: 没专门 slow 模块, 复用主入库 run_ingest_with_retry (它自己建 trigger='slow-fill' run)
_SLOW_FILL_REINGEST = {"apevon"}


@router.post("/vendors/{vendor_id}/slow-fill")
def slow_fill(
    vendor_id: str,
    background: BackgroundTasks,
    day: dt.date | None = Query(None, description="默认昨天 (CST)"),
):
    """慢路径补 model 拆分字段 — 前端面板"补慢路径"按钮入口. 公开 (无 token).

    走快慢双路径的 vendor (blueshirt/nulls/apevon) 都支持:
    - blueshirt/nulls: 调 job.run_slow_fill_dedicated 同步 helper, 它建 run 行 + UPDATE 状态
    - apevon: reingest 模式 — 复用 run_ingest_with_retry, trigger='slow-fill'

    scheduler cron 跑慢路径走同一个 helper, trigger 都标 'slow-fill', 前端 latestRun 自然能看到
    "自动 / 手动" 都通过 status tag (running/success/failed) 呈现.

    状态查询: GET /api/ingest/runs?vendor_id=... 拉最新 run.
    并发保护: VendorIngestRun 表上 uniq_running_per_vendor 部分唯一索引兜住; 这里先 SELECT 探
    一下 status='running' 给个 409 明确报错, 避免后台 BackgroundTask 静默 IntegrityError.
    """
    from ingest.job import _SLOW_FILL_DISPATCH, run_slow_fill_dedicated
    supported = set(_SLOW_FILL_DISPATCH) | _SLOW_FILL_REINGEST
    if vendor_id not in supported:
        raise HTTPException(400, f"vendor {vendor_id} 不走慢路径补字段 (支持: {sorted(supported)})")

    target = day or (dt.date.today() - dt.timedelta(days=1))

    # apevon — 走主入库重跑这一天, run_ingest 自己建 run 行
    if vendor_id in _SLOW_FILL_REINGEST:
        def _bg_reingest():
            try:
                run_ingest_with_retry(vendor_id, target, target, trigger="slow-fill")
            except Exception as e:
                log.error(f"[{vendor_id} slow-fill] {target} failed after retries: {e}")
        background.add_task(_bg_reingest)
        return {"vendor_id": vendor_id, "day": target.isoformat(),
                "mode": "slow-fill-reingest", "status": "scheduled"}

    # dedicated 模式 — 先探一下没人在跑, 再扔进 BackgroundTask
    with SessionLocal() as s:
        busy = s.execute(
            select(VendorIngestRun.id)
            .where(VendorIngestRun.vendor_id == vendor_id,
                   VendorIngestRun.status == "running")
            .limit(1)
        ).scalar()
    if busy:
        raise HTTPException(409, f"vendor {vendor_id} 已有 running 任务 (run_id={busy}), 等它跑完再补")

    def _bg_dedicated():
        try:
            run_slow_fill_dedicated(vendor_id, target, trigger="slow-fill")
        except Exception as e:
            log.error(f"[{vendor_id} slow-fill bg] {target} failed: {e}")

    background.add_task(_bg_dedicated)
    return {"vendor_id": vendor_id, "day": target.isoformat(),
            "mode": "slow-fill", "status": "scheduled"}


@router.get("/state")
def list_state():
    """所有 vendor 当前入库游标 — 前端 overview 顶部"数据新鲜度"用.

    synced_through: MAX(usage_date) FROM vendor_usage_daily — 真正落库到哪天 (准确)
    earliest:      MIN(usage_date) — backfill 探到的最早一天 (前端显示"数据范围")
    state.last_ingested_date: 仅作为 cron 起点 (backfill 期间会被覆盖, 别拿这个判鲜度)
    slow_synced_through: 凡是上游 model 拆分接口跟总额接口分离, 总额稳但 model 偶尔
                         漏数据的 vendor 都给这个字段. 当前: blueshirt (data/self vs log/self)
                         + apevon (stat vs statistics). 取 prompt_tokens IS NOT NULL 的
                         最大日期 — NULL 表示那天 stat 拿到了 cost 但 model 拆分没进来.
    """
    from ingest.query import get_data_range, slow_path_vendor_ids
    from sqlalchemy import func
    from ingest.models import VendorUsageDaily
    ranges = get_data_range()

    # 哪些 vendor 走"慢路径补 model 拆分"模式 — query.slow_path_vendor_ids (agent 也复用)
    SLOW_PATH_VIDS = slow_path_vendor_ids()

    with SessionLocal() as s:
        rows = s.execute(select(VendorIngestState)).scalars().all()
        state_by_vid = {r.vendor_id: r for r in rows}

        # 慢路径进度 — prompt_tokens 不为 NULL 的最大日期, 按 vendor_id 一次性查
        slow_through_by_vid: dict[str, dt.date] = {}
        if SLOW_PATH_VIDS:
            slow_rows = s.execute(
                select(VendorUsageDaily.vendor_id,
                       func.max(VendorUsageDaily.usage_date))
                .where(VendorUsageDaily.vendor_id.in_(SLOW_PATH_VIDS),
                       VendorUsageDaily.prompt_tokens.isnot(None))
                .group_by(VendorUsageDaily.vendor_id)
            ).all()
            slow_through_by_vid = {vid: d for vid, d in slow_rows}

    all_vids = set(ranges) | set(state_by_vid)
    out = []
    for vid in sorted(all_vids):
        r = state_by_vid.get(vid)
        rng = ranges.get(vid)
        earliest, latest = rng if rng else (None, None)
        item = {
            "vendor_id": vid,
            "earliest": earliest.isoformat() if earliest else None,
            "synced_through": latest.isoformat() if latest else None,
            "last_ingested_date": r.last_ingested_date.isoformat() if r else None,
            "last_run_id": r.last_run_id if r else None,
            "updated_at": r.updated_at.isoformat() if r and r.updated_at else None,
        }
        if vid in SLOW_PATH_VIDS:
            slow_d = slow_through_by_vid.get(vid)
            item["slow_synced_through"] = slow_d.isoformat() if slow_d else None
        out.append(item)
    return out
