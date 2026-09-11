"""巡检信号收集 — 纯确定性代码, 不碰 LLM.

每轮巡检先跑这里: 返回 [] = 完全健康, patrol 直接退出 (零 LLM 成本, 不落报告行).
信号是 LLM 诊断的输入, 也是动作护栏的白名单来源 (只允许对出现信号的 vendor 动作).

信号类型 (type):
- failed_run        48h 内最新一次 run 失败 (error 已有 run_id, 可下钻全 traceback)
- missing_days      14 天窗口内有缺天 (真数据区间内的 gap)
- zombie_run        status=running 超 2h (进程内僵尸, 启动清理管不到的)
- freshness_lag     水位落后 (openai 对比 PT 昨天, 其余 CST; T+1 vendor 允许 2 天)
- slow_path_lag     blueshirt/nulls 慢路径拆分落后; 超 13 天 retention = 不可恢复
- session_expired   需要登录的 vendor session 文件缺失 (cookie 失效/被登出)
- silent_empty_hint run success 但有 info 提示 (apevon stat-fallback / 上游空)

severity: high (大概率真故障/需尽快处理) / medium / low.
"""
from __future__ import annotations

import datetime as dt
import logging
from datetime import timedelta, timezone

from vendors import load_vendors

from ingest.query import (
    get_freshness,
    get_missing_days_in_window,
    get_recently_failed_vendors,
    get_running_runs,
    get_success_runs_with_hints,
    get_vendor_runs,
    slow_path_vendor_ids,
)

log = logging.getLogger("ingest.agent.signals")

CST = timezone(timedelta(hours=8))
try:
    from zoneinfo import ZoneInfo
    PT = ZoneInfo("America/Los_Angeles")
except ImportError:  # pragma: no cover
    PT = timezone(timedelta(hours=-8))

# 巡检窗口: 缺天检测回看天数 (跟 openai 回看窗口一致, 覆盖周末两天的 gap)
WINDOW_DAYS = 14

# 水位允许滞后天数 — T+1 出账 (kimi/apevon) 与 PT 日切 (openai) 给 2 天余量
ALLOWED_LAG = {"kimi": 2, "apevon": 2, "openai": 2}

# blueshirt /api/log/self retention, 超期数据永久丢失
BLUESHIRT_RETENTION_DAYS = 13

# 需要浏览器登录的 vendor 类型 (session 文件缺失 = 无法自动恢复, 需人工)
from login_flow import LOGIN_TYPES  # noqa: E402


def _today_cst() -> dt.date:
    return dt.datetime.now(CST).date()


def _yesterday_cst() -> dt.date:
    return _today_cst() - timedelta(days=1)


def _yesterday_pt() -> dt.date:
    return (dt.datetime.now(PT) - timedelta(days=1)).date()


def _expected_day(vendor_id: str) -> dt.date:
    """vendor 水位应该追到的日子: openai 按 PT 昨天, 其余按 CST 昨天."""
    return _yesterday_pt() if vendor_id == "openai" else _yesterday_cst()


def collect_signals(within_hours: int = 26) -> dict:
    """收集全部巡检信号. 返回完整快照 (含 context), signals 为空 = 健康.

    返回 {"generated_at", "today_cst", "signals": [...], "context": {...}}.
    每个 signal: {"type", "vendor_id", "severity", "detail", **附带上钻字段}.
    """
    today = _today_cst()
    signals: list[dict] = []

    vendors = load_vendors()
    by_id = {v["id"]: v for v in vendors}
    failed = get_recently_failed_vendors(within_hours=within_hours)
    missing = get_missing_days_in_window(today - timedelta(days=WINDOW_DAYS - 1),
                                         today - timedelta(days=1))
    running = get_running_runs()          # 全部 running (进 context)
    zombies = get_running_runs(min_age_hours=2.0)
    freshness = get_freshness()
    slow_vids = slow_path_vendor_ids()
    hints = get_success_runs_with_hints(within_hours=within_hours)

    # 1. failed_run — 唯一来源是最近一次 run 失败 (含 session 过期/上游 5xx 等)
    for f in failed:
        vid = f["vendor_id"]
        sev = "high" if any(k in (f["error_msg"] or "") for k in
                            ("silent-empty", "IntegrityError", "session", "登录", "401")) else "medium"
        signals.append({
            "type": "failed_run", "vendor_id": vid, "severity": sev,
            "run_id": f["run_id"],
            "error": (f["error_msg"] or "")[:300],
            "finished_at": f["finished_at"],
            "detail": f"最近一次 run #{f['run_id']} 失败: {(f['error_msg'] or '')[:120]}",
        })

    # 2. missing_days — 真数据区间内的 gap (该 vendor 用过但某天没入库)
    for vid, days in missing.items():
        sev = "high" if len(days) > 3 or _yesterday_cst().isoformat() in days else "medium"
        unrecoverable = [d for d in days
                         if vid == "blueshirt"
                         and (dt.date.fromisoformat(d) < today - timedelta(days=BLUESHIRT_RETENTION_DAYS))]
        note = f" (超 {BLUESHIRT_RETENTION_DAYS} 天 retention, 不可恢复: {unrecoverable})" if unrecoverable else ""
        signals.append({
            "type": "missing_days", "vendor_id": vid, "severity": sev,
            "days": days,
            "detail": f"缺 {len(days)} 天: {', '.join(days[:8])}{'...' if len(days) > 8 else ''}{note}",
        })

    # 3. zombie_run — 进程内卡死的 running (启动清理只处理重启前的).
    #    trigger='backfill' 排除: backfill 合法跑数小时 (90 天窗口 / tencent 24×1h),
    #    DB 年龄判不了死活, 宁可漏杀留给 backend 重启清理, 也不能误杀活任务
    #    (误杀会解除并发锁 → 同 vendor 双进程并发抓取).
    for z in zombies:
        if z.get("trigger") == "backfill":
            continue
        signals.append({
            "type": "zombie_run", "vendor_id": z["vendor_id"], "severity": "high",
            "run_id": z["run_id"],
            "age_hours": z["age_hours"],
            "trigger": z.get("trigger"),
            "detail": f"run #{z['run_id']} ({z.get('trigger') or '?'}) running 已 "
                      f"{z['age_hours']}h (阈值 2h), 阻塞该 vendor 后续触发",
        })

    # 4. freshness_lag — 水位落后 (missing_days 看不到的"尾部缺失": 区间末尾之后全缺)
    enabled_ids = {v["id"] for v in vendors if v.get("enabled") is not False}
    for vid in sorted(enabled_ids):
        latest = freshness.get(vid)
        if latest is None:
            continue  # 从没入库过的 vendor 交给 onboarding / backfill, 不巡检
        expected = _expected_day(vid)
        allowed = ALLOWED_LAG.get(vid, 1)
        lag_days = (expected - latest).days
        if lag_days > allowed:
            signals.append({
                "type": "freshness_lag", "vendor_id": vid,
                "severity": "medium" if lag_days <= allowed + 2 else "high",
                "latest": latest.isoformat(),
                "expected": expected.isoformat(),
                "lag_days": lag_days,
                "detail": f"水位停在 {latest.isoformat()} (应到 {expected.isoformat()}, 落后 {lag_days} 天)",
            })

    # 5. slow_path_lag — 慢路径拆分 (prompt/cache 字段) 落后于快路径水位
    slow_through = _slow_synced_through(slow_vids)
    for vid in sorted(slow_vids):
        latest = freshness.get(vid)
        slow = slow_through.get(vid)
        if not latest:
            continue
        lag = (latest - slow).days if slow else 999  # 从没补过拆分
        if lag >= 2:
            expired = []
            if vid == "blueshirt" and slow:
                d = slow
                while d < latest:
                    d += timedelta(days=1)
                    if (today - d).days > BLUESHIRT_RETENTION_DAYS:
                        expired.append(d.isoformat())
            signals.append({
                "type": "slow_path_lag", "vendor_id": vid,
                "severity": "high" if expired else "medium",
                "slow_through": slow.isoformat() if slow else None,
                "fast_through": latest.isoformat(),
                "lag_days": lag,
                "detail": (f"拆分字段只补到 {slow.isoformat() if slow else '从未'} "
                           f"(快路径已到 {latest.isoformat()}, 落后 {lag} 天)"
                           + (f"; 超 retention 不可恢复: {len(expired)} 天" if expired else "")),
            })

    # 6. session_expired — 需要登录的 vendor session 文件缺失
    for v in vendors:
        if v.get("type") not in LOGIN_TYPES:
            continue
        from login_flow import get_session_file
        if not get_session_file(v):
            # 在登录中不算过期 (浏览器已弹, 等人)
            from login_flow import is_logging_in
            if is_logging_in(v["id"]):
                continue
            signals.append({
                "type": "session_expired", "vendor_id": v["id"], "severity": "high",
                "detail": f"session 文件缺失 (cookie 失效/被登出), 需要浏览器登录恢复",
            })

    # 7. silent_empty_hint — success 但有 info 提示 (半健康, 多数为 stat-fallback)
    for h in hints:
        vid = h["vendor_id"]
        # 已有 failed/zombie/missing 信号的 vendor 不重复报弱信号
        if any(s["vendor_id"] == vid and s["type"] != "silent_empty_hint" for s in signals):
            continue
        signals.append({
            "type": "silent_empty_hint", "vendor_id": vid, "severity": "low",
            "run_id": h["run_id"],
            "hint": h["error_msg"][:200],
            "detail": f"run #{h['run_id']} 成功但有提示: {(h['error_msg'] or '')[:120]}",
        })

    # 预取读数据: 出现信号的 vendor 最近 3 次 run + 登录态 直接嵌进快照 — LLM
    # 一般无需再调读工具下钻 (弱模型会一家一轮地读, 白烧轮次; 确定性预取一劳永逸)
    recent_runs: dict[str, list] = {}
    session_states: dict[str, dict] = {}
    for vid in sorted({s["vendor_id"] for s in signals}):
        try:
            recent_runs[vid] = get_vendor_runs(vid, limit=3)
        except Exception as e:
            log.warning(f"[signals] 预取 {vid} run 历史失败 (跳过): {e}")
            recent_runs[vid] = []
        v = by_id.get(vid)
        if v and v.get("type") in LOGIN_TYPES:
            try:
                import login_flow as _lf
                session_states[vid] = _lf.session_status(v)
            except Exception as e:
                log.warning(f"[signals] 预取 {vid} session 状态失败 (跳过): {e}")

    snapshot = {
        "generated_at": dt.datetime.now(CST).isoformat(),
        "today_cst": today.isoformat(),
        "signals": signals,
        "context": {
            # 正在跑的 vendor — LLM 禁止对其触发动作
            "running_vendors": sorted({r["vendor_id"] for r in running}),
            "vendor_names": {v["id"]: v.get("name", v["id"]) for v in vendors},
            "window_days": WINDOW_DAYS,
            "allowed_lag": ALLOWED_LAG,
            # 信号 vendor 的近期 run (状态/窗口/行数/报错摘要) — 诊断材料已内嵌
            "recent_runs": recent_runs,
            # 需要登录的信号 vendor 的当前登录态 (ok/expired/waiting)
            "session_status": session_states,
        },
    }
    log.info(f"[signals] collected {len(signals)} signals "
             f"(failed={len(failed)} missing={len(missing)} zombie={len(zombies)} "
             f"hints={len(hints)})")
    return snapshot


def _slow_synced_through(slow_vids: set[str]) -> dict[str, dt.date]:
    """慢路径拆分进度 — prompt_tokens IS NOT NULL 的最大日期 (与 /state 同判定)."""
    if not slow_vids:
        return {}
    from sqlalchemy import func, select
    from ingest.db import SessionLocal
    from ingest.models import VendorUsageDaily
    with SessionLocal() as s:
        rows = s.execute(
            select(VendorUsageDaily.vendor_id, func.max(VendorUsageDaily.usage_date))
            .where(VendorUsageDaily.vendor_id.in_(slow_vids),
                   VendorUsageDaily.prompt_tokens.isnot(None))
            .group_by(VendorUsageDaily.vendor_id)
        ).all()
        return {vid: d for vid, d in rows}
