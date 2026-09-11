"""巡检报告 — 落库 (agent_patrol_report) + 飞书卡片渲染 (正常 / 降级).

卡片模板色: green=全部自愈 / orange=部分自愈或有弱信号 / red=升级人工或巡检自身出错.
fingerprint = 信号集合的 sha1 (只看 type+vendor_id); 与上一轮非 clean 报告相同
= 同一问题连续未修复, 卡片加 "⚠️ 连续 N 轮未修复" 警示.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, select

from ingest.db import SessionLocal
from ingest.models import AgentPatrolReport

log = logging.getLogger("ingest.agent.report")

CST = timezone(timedelta(hours=8))

# 飞书卡片内诊断正文截断 (lark_md 卡片有大小限制)
CARD_DIAGNOSIS_LIMIT = 1500


# ───────────────────────── 持久化 ─────────────────────────
def create_report(slot: str, signals: list[dict] | None = None) -> int:
    """创建 running 状态的报告行, 返回 id."""
    with SessionLocal() as s:
        row = AgentPatrolReport(slot=slot, status="running", llm_used=False,
                                signals=signals or [], actions=[])
        s.add(row)
        s.commit()
        return row.id


def mark_stale_running_failed(note: str = "backend 重启时巡检被中断") -> int:
    """startup 清理: 把遗留 status='running' 的报告行标 error.

    巡检开始即写 running 行, 跑完才 finalize — 进程被杀就永远停在 running,
    前端 /agent 页会一直"巡检进行中"轮询. 单进程架构下重启后不可能有存活的
    巡检 (互斥锁也在进程内), 所以 startup 时所有 running 行都是僵尸.
    静默标记 (不推飞书 — 重启是已知动作, 中断的信号下一轮巡检自然会再看).
    返回清理行数.
    """
    with SessionLocal() as s:
        rows = s.execute(
            select(AgentPatrolReport).where(AgentPatrolReport.status == "running")
        ).scalars().all()
        for row in rows:
            row.status = "error"
            row.summary = note
            row.escalate = False
        s.commit()
        if rows:
            log.warning(f"[report] 清理 {len(rows)} 条重启遗留的 running 巡检报告: "
                        f"{[r.id for r in rows]}")
        return len(rows)


def finalize_report(rid: int, **fields) -> None:
    """把 running 行收尾成最终状态. 字段对应 AgentPatrolReport 列."""
    with SessionLocal() as s:
        row = s.get(AgentPatrolReport, rid)
        if not row:
            log.error(f"[report] report {rid} 不存在, 无法收尾")
            return
        for k, v in fields.items():
            setattr(row, k, v)
        row.finished_at = datetime.now(timezone.utc)
        s.commit()


def get_reports(limit: int = 20) -> list[dict]:
    with SessionLocal() as s:
        rows = s.execute(
            select(AgentPatrolReport)
            .order_by(desc(AgentPatrolReport.patrolled_at))
            .limit(max(1, min(limit, 100)))
        ).scalars().all()
        return [_row_to_dict(r) for r in rows]


def get_report(rid: int) -> dict | None:
    with SessionLocal() as s:
        r = s.get(AgentPatrolReport, rid)
        return _row_to_dict(r) if r else None


def _row_to_dict(r: AgentPatrolReport) -> dict:
    return {
        "id": r.id, "slot": r.slot,
        "patrolled_at": r.patrolled_at.isoformat() if r.patrolled_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "status": r.status, "llm_used": r.llm_used, "model": r.model,
        "signals": r.signals or [], "fingerprint": r.fingerprint,
        "diagnosis": r.diagnosis, "actions": r.actions or [],
        "verification": r.verification or [], "summary": r.summary,
        "escalate": r.escalate, "escalate_reason": r.escalate_reason,
        "llm_meta": r.llm_meta,
    }


# ───────────────────────── 指纹: 连续未修复检测 ─────────────────────────
def compute_fingerprint(signals: list[dict]) -> str:
    """信号集合指纹 — 只看 (type, vendor_id), 与天数/报错文本无关."""
    keys = sorted({f"{s.get('type')}|{s.get('vendor_id')}" for s in signals})
    return hashlib.sha1("\n".join(keys).encode()).hexdigest()[:40]


def previous_unresolved_count(fingerprint: str, before_id: int, lookback: int = 10) -> int:
    """往前数连续同指纹的非 clean 报告数 (当前轮之前已经出现了几轮同一问题)."""
    count = 0
    with SessionLocal() as s:
        rows = s.execute(
            select(AgentPatrolReport)
            .where(AgentPatrolReport.status != "clean",
                   AgentPatrolReport.id < before_id)
            .order_by(desc(AgentPatrolReport.id))
            .limit(lookback)
        ).scalars().all()
        for r in rows:
            if r.fingerprint == fingerprint:
                count += 1
            else:
                break
    return count


# ───────────────────────── 飞书卡片 ─────────────────────────
def send_patrol_card(slot: str, report: dict) -> bool:
    """发正常巡检报告卡片. report 为 finalize 后的完整 dict."""
    from ingest import alert
    title = f"🤖 巡检自愈报告 · {slot}"
    md = _render_report_md(report)
    if report.get("status") == "escalated" or report.get("status") == "error":
        template = "red"
    elif report.get("status") == "healed":
        template = "green"
    else:
        template = "orange"
    return alert._send_card(title, md, template)


def _render_report_md(report: dict) -> str:
    parts: list[str] = []
    n_escalate = sum(1 for v in (report.get("actions") or []) if not v.get("accepted", True))
    status_label = {"healed": "✅ 已自愈", "escalated": "🔴 需人工",
                    "degraded": "🟠 降级模式", "error": "⛔ 巡检异常"}.get(
                        report.get("status"), report.get("status", "?"))
    parts.append(f"**状态**: {status_label} · 信号 {len(report.get('signals') or [])} 条 · "
                 f"动作 {len([a for a in (report.get('actions') or []) if a.get('accepted')])} 个"
                 + (f" · 拒绝 {n_escalate}" if n_escalate else ""))
    parts.append(f"**总结**: {report.get('summary') or '-'}")

    if report.get("signals"):
        parts.append("**── 信号 ──**")
        for s in report["signals"][:12]:
            sev = {"high": "🔴", "medium": "🟠", "low": "🟡"}.get(s.get("severity"), "⚪")
            parts.append(f"{sev} `{s.get('vendor_id')}` {s.get('type')}: {s.get('detail', '')[:100]}")

    if report.get("diagnosis"):
        diag = report["diagnosis"]
        if len(diag) > CARD_DIAGNOSIS_LIMIT:
            diag = diag[:CARD_DIAGNOSIS_LIMIT] + "\n…(截断, 全文见报告 API)"
        parts.append("**── 诊断 (LLM) ──**\n" + diag)

    if report.get("actions"):
        parts.append("**── 动作与验证 ──**")
        for a in report["actions"]:
            mark = "✓" if a.get("accepted") else "✗"
            v = a.get("verify_result", "")
            parts.append(f"{mark} `{a.get('tool')}` {json.dumps(a.get('args', {}),
                             ensure_ascii=False)} → {a.get('detail', '')[:80]}"
                         + (f"\n　验证: {v}" if v else ""))

    if report.get("escalate"):
        parts.append(f"**── 需人工 ──**\n{report.get('escalate_reason') or '见诊断'}")

    repeat = report.get("repeat_count") or 0
    if repeat >= 1:
        parts.append(f"⚠️ 该问题已连续 {repeat + 1} 轮巡检未修复, 请人工介入")

    parts.append(f"报告详情: `GET /api/ingest/agent/reports/{report.get('id')}`")
    return "\n".join(parts)


# ───────────────────────── 降级模式 ─────────────────────────
# LLM 不可用 / 循环异常时的确定性建议 (按信号类型)
_DEGRADED_ADVICE = {
    "failed_run": "等下一轮 cron 自动补拉, 或前端 vendor 详情页手动触发; 连续失败再看 run 完整报错",
    "missing_days": "前端 vendor 详情页手动触发入库 (只拉缺的天)",
    "zombie_run": "若持续卡住: 重启 backend (启动自动清理) 或 SQL: UPDATE vendor_ingest_run SET status='failed' WHERE id=<run_id>",
    "freshness_lag": "T+1 出账 / PT 日切落后 1~2 天属正常时序; 持续落后再手动触发",
    "slow_path_lag": "vendor 详情页点「补拆分字段」(13 天 retention 内有效)",
    "session_expired": "02:30 预检会自动登录 (配了 .env 账密的); 仍未恢复再人工: curl -X POST /api/vendors/{vendor_id}/login (浏览器/noVNC 完成)",
    "silent_empty_hint": "stat-fallback 半健康状态, cost 已入库; 拆分等下轮慢路径",
}


def send_degraded_card(slot: str, snapshot: dict, error: str | None = None) -> bool:
    """降级卡片 — 无 LLM 结论, 只有信号 + 确定性建议. 高危信号 → red, 否则 orange."""
    from ingest import alert
    signals = snapshot.get("signals") or []
    has_high = any(s.get("severity") == "high" for s in signals)
    parts = [f"**信号**: {len(signals)} 条 (LLM 不可用, 降级为确定性建议)"]
    if error:
        parts.append(f"**降级原因**: {error[:200]}")
    parts.append("**── 信号与建议 ──**")
    for s in signals[:12]:
        sev = {"high": "🔴", "medium": "🟠", "low": "🟡"}.get(s.get("severity"), "⚪")
        advice = _DEGRADED_ADVICE.get(s.get("type"), "人工检查").replace("{vendor_id}", str(s.get("vendor_id")))
        parts.append(f"{sev} `{s.get('vendor_id')}` {s.get('type')}: {s.get('detail', '')[:100]}\n"
                     f"　建议: {advice}")
    parts.append(f"信号快照: `GET /api/ingest/agent/reports` (本轮报告 id 见响应)")
    return alert._send_card(f"🤖 巡检报告 · {slot} (降级模式 — LLM 不可用)",
                            "\n".join(parts), "red" if has_high else "orange")


def degraded_summary(snapshot: dict, error: str | None) -> str:
    """降级模式的 report.summary 字段内容."""
    signals = snapshot.get("signals") or []
    by_type: dict[str, int] = {}
    for s in signals:
        by_type[s.get("type", "?")] = by_type.get(s.get("type", "?"), 0) + 1
    base = "降级模式 (LLM 不可用): " + ", ".join(f"{k}×{v}" for k, v in sorted(by_type.items()))
    return (base + f"; 原因: {error[:150]}") if error else base
