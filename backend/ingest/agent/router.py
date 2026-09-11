"""Agent 巡检 HTTP 端点 — 手动触发 (可取消/可替换) + 报告查询.

挂在 /api/ingest/agent 前缀下, 跟其他 /api/ingest/* 一样由 dashboard cookie
middleware 保护 (进程内 cron 直调 run_patrol 不走这里).
"""
from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, HTTPException, Query

from ingest.agent import patrol as patrol_mod
from ingest.agent import report as report_mod

log = logging.getLogger("ingest.agent.router")

router = APIRouter(prefix="/api/ingest/agent", tags=["ingest-agent"])


@router.post("/patrol")
def trigger_patrol():
    """手动触发一轮巡检 (异步跑, 立即返回).

    报告行在返回前同步建好 (status=running) — 前端触发后立刻刷新必定可见.
    已有巡检在跑 → 替换语义: 请求取消旧巡检, 排队启动新一轮
    (旧巡检在当前步骤结束后退出, 最长约 3 分钟).
    """
    if patrol_mod.patrol_busy():
        patrol_mod.request_cancel()
        rid = report_mod.create_report("manual", signals=[])
        threading.Thread(target=patrol_mod.replace_after_current, args=(rid,),
                         daemon=True, name="agent-patrol-replace").start()
        return {"status": "replacing", "report_id": rid,
                "note": "已请求取消上一轮, 其退出后自动开始新一轮 (新报告行已出现在列表)"}
    rid = report_mod.create_report("manual", signals=[])
    threading.Thread(target=patrol_mod.run_patrol,
                     kwargs={"slot": "manual", "rid": rid},
                     daemon=True, name="agent-patrol-manual").start()
    return {"status": "started", "report_id": rid,
            "note": "巡检已异步启动, 结果见 GET /api/ingest/agent/reports"}


@router.post("/patrol/cancel")
def cancel_patrol():
    """取消当前巡检 (协作式: 当前步骤结束后停止, 最长约 3 分钟).

    已触发的摄取动作继续后台跑完; 报告收尾为 status='cancelled'."""
    st = patrol_mod.request_cancel()
    if st == "not_running":
        return {"status": "not_running", "note": "当前没有进行中的巡检"}
    return {"status": "cancelling",
            "note": "已请求取消, 当前步骤 (LLM 调用/登录等待, 最长约 3 分钟) 结束后停止"}


@router.get("/reports")
def list_reports(limit: int = Query(20, ge=1, le=100)):
    """最近巡检报告列表 (倒序)."""
    return report_mod.get_reports(limit=limit)


@router.get("/reports/{rid}")
def get_report(rid: int):
    """单份巡检报告详情 (信号 / 诊断全文 / 动作 / 验证)."""
    r = report_mod.get_report(rid)
    if not r:
        raise HTTPException(status_code=404, detail=f"报告 {rid} 不存在")
    return r
