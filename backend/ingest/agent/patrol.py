"""巡检编排 — 单一入口 run_patrol(slot), cron 与手动触发共用.

流程: 锁 → 信号收集 → (干净即退出, cron 不落行) → 报告行 → LLM 诊断/降级 →
确定性验证 (轮询 run 结果 + 重收信号) → 收尾落库 + 飞书卡片.

安全承诺:
- 顶层 try/except: 任何异常都吞掉并落 error 报告, cron 永不因 agent 报 ERROR
- 全局 deadline (AGENT_PATROL_DEADLINE, 默认 1500s): 验证最多等到 deadline,
  没等完的动作标 still_running, 下一轮巡检从 vendor_ingest_run 收尾 (不丢)
- LLM 不可用 / 循环异常 → 降级卡片 (纯信号 + 确定性建议), 巡检本身照常结束
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from datetime import timedelta, timezone

from ingest.agent import llm as llm_mod
from ingest.agent import report as report_mod
from ingest.agent.prompts import build_system_prompt
from ingest.agent.signals import collect_signals
from ingest.agent.tools import ToolExecutor

log = logging.getLogger("ingest.agent.patrol")

CST = timezone(timedelta(hours=8))

# 巡检互斥锁: cron 三时段 + 手动触发共用, 防重叠
_lock = threading.Lock()
# 当前巡检的取消事件 (持锁期间非 None) — request_cancel 置位, 循环各阶段检查
_cancel: threading.Event | None = None
# 排队中的替换巡检 (等锁释放): rid → 取消事件. request_cancel 连它们一起杀,
# 否则"替换后立刻反悔取消"只杀到旧巡检, 排队的新一轮照样跑.
_pending_watchers: dict[int, threading.Event] = {}

VERIFY_POLL_INTERVAL = 30  # 动作结果轮询间隔


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def patrol_busy() -> bool:
    """巡检是否正在进行 (router 手动触发时判重用)."""
    return _lock.locked()


def request_cancel() -> str:
    """请求取消当前巡检 + 排队中的替换巡检. 返回 'cancelling' | 'not_running'.

    取消是协作式的: 循环各阶段 (LLM 轮次开始 / 验证轮询 tick) 检查事件,
    实际停止延迟 ≤ 当前单步耗时 (LLM 调用 ≤2min / request_login 等待 ≤3min).
    """
    global _cancel
    hit = False
    if _lock.locked() and _cancel is not None:
        _cancel.set()
        hit = True
    for ev in list(_pending_watchers.values()):
        ev.set()
        hit = True
    return "cancelling" if hit else "not_running"


def replace_after_current(rid: int, wait_s: int = 300) -> None:
    """等当前巡检退出 (调用方应先 request_cancel) 后, 用预建报告行启动新巡检.

    替换语义的接力棒: 端点先同步建好 running 行 (前端立即可见), 本 watcher
    等锁释放再真正跑. 等待期间被再次取消 → 预建行标 cancelled 直接不跑;
    等不到锁 (旧巡检卡死) → 标 error, 旧巡检终会撞自身 deadline 收尾;
    锁被 cron 抢走 → 同样标 error 说明未启动.
    """
    my_cancel = threading.Event()
    _pending_watchers[rid] = my_cancel
    try:
        waited = 0.0
        while _lock.locked() and not my_cancel.is_set() and waited < wait_s:
            time.sleep(1)
            waited += 1
        if my_cancel.is_set():
            report_mod.finalize_report(
                rid, status="cancelled", llm_used=False, escalate=False,
                summary="排队的新一轮被手动取消, 未启动")
            return
        if _lock.locked():
            report_mod.finalize_report(
                rid, status="error", llm_used=False, escalate=False,
                summary=f"上一次巡检 {wait_s}s 内未退出, 本轮未启动 (旧行终会撞自身 deadline 收尾)")
            return
        if run_patrol("manual", rid=rid) is None:
            report_mod.finalize_report(
                rid, status="error", llm_used=False, escalate=False,
                summary="互斥锁被其他巡检 (cron) 抢先占用, 本轮未启动")
    finally:
        _pending_watchers.pop(rid, None)


def run_patrol(slot: str = "manual", rid: int | None = None) -> int | None:
    """跑一轮巡检. 返回报告 id (干净且非手动 → None; 锁被占 → None).

    rid: 预建报告行 id (router 手动触发/替换语义时先同步建行保证前端立即可见),
    不传则巡检自建. 锁被占时直接返回 None — 预建行的收尾是调用方 (watcher) 的责任.
    """
    global _cancel
    if not _lock.acquire(blocking=False):
        log.warning(f"[patrol] 已有巡检在进行, 本次 ({slot}) 跳过")
        return None
    cancel = threading.Event()
    _cancel = cancel
    deadline = time.time() + _env_int("AGENT_PATROL_DEADLINE", 1500)
    try:
        return _run(slot, deadline, rid=rid, cancel=cancel)
    except Exception as e:
        # 最外层兜底: 理论上不可达 (_run 内部已全捕获), 真到了这里就落 error 报告
        log.error(f"[patrol] 顶层异常: {type(e).__name__}: {e}", exc_info=True)
        try:
            if rid is None:
                rid = report_mod.create_report(slot, signals=[])
            report_mod.finalize_report(
                rid, status="error", llm_used=False,
                summary=f"巡检顶层异常: {type(e).__name__}: {e}",
                escalate=True, escalate_reason=str(e)[:500],
                actions=[], verification=[])
            report_mod.send_patrol_card(slot, report_mod.get_report(rid) or {"id": rid})
            return rid
        except Exception:
            log.error("[patrol] 兜底报告写入也失败了", exc_info=True)
            return None
    finally:
        _cancel = None
        _lock.release()


def _finalize_cancelled(rid: int, executor=None, llm_used: bool = False) -> None:
    """手动取消的收尾: status='cancelled', 已触发动作原样记录, 不推飞书卡片
    (取消是页面上的用户动作, 人就在看). 已提交的摄取任务继续后台跑完."""
    actions = executor.actions if executor is not None else []
    n = len([a for a in actions if a.get("accepted")])
    note = "手动取消"
    if n:
        note += f" ({n} 个已触发动作继续后台跑完, 结果由下一轮巡检收尾)"
    report_mod.finalize_report(rid, status="cancelled", llm_used=llm_used,
                               summary=note, actions=actions,
                               verification=[], escalate=False)
    log.info(f"[patrol] report {rid} 已取消")


def _run(slot: str, deadline: float, rid: int | None = None,
         cancel: threading.Event | None = None) -> int | None:
    def _cancelled() -> bool:
        return cancel is not None and cancel.is_set()

    # ── 1. 信号收集 (确定性, 零 LLM 成本) ──
    try:
        snapshot = collect_signals()
    except Exception as e:
        log.error(f"[patrol] 信号收集失败: {e}", exc_info=True)
        rid = rid or report_mod.create_report(slot, signals=[])
        report_mod.finalize_report(rid, status="error", llm_used=False,
                                   summary=f"信号收集失败: {e}",
                                   escalate=True, escalate_reason=str(e)[:500])
        report_mod.send_patrol_card(slot, report_mod.get_report(rid) or {"id": rid})
        return rid

    if _cancelled():
        rid = rid or report_mod.create_report(slot, signals=snapshot["signals"])
        _finalize_cancelled(rid)
        return rid

    signals = snapshot["signals"]
    if not signals:
        if slot == "manual":
            # 手动触发干净也落一行, 前端页能看到"刚巡检过, 健康"
            rid = rid or report_mod.create_report(slot, signals=[])
            report_mod.finalize_report(rid, status="clean", llm_used=False,
                                       summary="巡检完成: 无故障信号", actions=[], verification=[])
            log.info("[patrol] 手动巡检: 干净")
            return rid
        log.info("[patrol] 干净, 退出 (零 LLM 成本)")
        return None

    log.info(f"[patrol] {slot}: {len(signals)} 条信号, 进入诊断")
    rid = rid or report_mod.create_report(slot, signals=signals)
    fingerprint = report_mod.compute_fingerprint(signals)
    repeat_count = report_mod.previous_unresolved_count(fingerprint, rid)

    # ── 2. LLM 诊断 (不可用 → 降级) ──
    report: dict = {}
    llm_used = False
    model = None
    llm_error: str | None = None
    executor = ToolExecutor(snapshot)

    if llm_mod.llm_configured():
        try:
            now = dt.datetime.now(CST)
            system_prompt = build_system_prompt(snapshot["today_cst"], now.isoformat())
            report = llm_mod.run_diagnosis_loop(snapshot, executor, system_prompt,
                                                cancel_event=cancel)
            llm_used = True
            model = os.environ.get("AGENT_LLM_MODEL", "gpt-4.1-mini")
        except llm_mod.PatrolCancelled:
            _finalize_cancelled(rid, executor=executor)
            return rid
        except llm_mod.LLMError as e:
            llm_error = str(e)
            log.error(f"[patrol] LLM 诊断失败, 走降级: {llm_error}")
        except Exception as e:  # 防御: llm 模块 bug 不该炸掉巡检
            llm_error = f"{type(e).__name__}: {e}"
            log.error(f"[patrol] LLM 循环意外异常, 走降级: {llm_error}", exc_info=True)
    else:
        llm_error = "未配置 LLM key (AGENT_LLM_API_KEY / OPENAI_API_KEY)"

    # ── 3. 动作结果验证 (确定性; LLM 循环里动作已异步提交) ──
    verify_results = _verify_actions(executor, snapshot, deadline, cancel=cancel)
    if _cancelled():
        _finalize_cancelled(rid, executor=executor, llm_used=llm_used)
        return rid
    after_snapshot = None
    try:
        after_snapshot = collect_signals()
    except Exception:
        log.warning("[patrol] 动作后信号重收失败 (不影响报告)", exc_info=True)

    verification = _build_verification(executor.actions, snapshot, after_snapshot)
    # 只有明确失败 (ok is False) 才升级; ok=None (still_running / 等人工登录) 是
    # 未定态, 下一轮巡检收尾 — 长动作不该被误报成红色"需人工"
    action_failed = any(v.get("ok") is False for v in verify_results)
    escalate = bool(report.get("escalate")) or action_failed

    # ── 4. 收尾 ──
    if llm_used:
        # healed = LLM 判定无需人工 (含"观察即可, 等 cron 自愈"的情况);
        # 持续未解决的信号由 fingerprint 连续未修复警示兜底, 不在这里强制升级
        status = "escalated" if escalate else "healed"
        summary = report.get("summary") or "巡检完成"
    else:
        has_high = any(s.get("severity") == "high" for s in signals)
        # 降级模式同样尊重验证结果: 动作已执行且明确失败 → 升级 (不能被 has_high 覆盖掉)
        status = "escalated" if (has_high or action_failed) else "degraded"
        escalate = has_high or action_failed
        summary = report_mod.degraded_summary(snapshot, llm_error)

    escalate_reason = None
    if escalate:
        reasons = []
        if report.get("escalate_reason"):
            reasons.append(str(report["escalate_reason"]))
        for v in verify_results:
            if not v.get("ok"):
                reasons.append(f"{v.get('vendor_id')}: {v.get('note', '动作未成功')}")
        if not llm_used:
            reasons.append("LLM 降级模式, 无法自动诊断处置")
        escalate_reason = "; ".join(dict.fromkeys(r for r in reasons if r))[:1000] or None

    report_mod.finalize_report(
        rid, status=status, llm_used=llm_used, model=model,
        fingerprint=fingerprint,
        diagnosis=(report.get("diagnosis_md") if llm_used else None),
        actions=executor.actions, verification=verification,
        summary=summary, escalate=escalate, escalate_reason=escalate_reason,
        llm_meta=report.get("llm_meta"),
    )
    final = report_mod.get_report(rid) or {"id": rid, "status": status, "signals": signals,
                                           "actions": executor.actions, "summary": summary,
                                           "escalate": escalate}
    final["repeat_count"] = repeat_count
    if llm_used:
        report_mod.send_patrol_card(slot, final)
    else:
        report_mod.send_degraded_card(slot, snapshot, llm_error)
    log.info(f"[patrol] {slot} 完成: status={status} report_id={rid}")
    return rid


# ───────────────────────── 动作验证 ─────────────────────────
def _verify_actions(executor, snapshot: dict, deadline: float,
                    cancel: threading.Event | None = None) -> list[dict]:
    """等每个已接受动作的最终结果. run 类动作轮询到终态/deadline; 其余即时.

    cancel 置位 → 剩余 run 类动作不再等待 (标已取消, 结果由下一轮巡检收尾).

    两类隐性失败必须先捞出来:
    - submit_error: run_ingest 在建 run 行之前抛的错 (并发 IntegrityError 等),
      只有查 Future 才能发现 (executor.poll_submit_errors)
    - 旧 run 误归因: run_id 没抓到时只认 submitted_at 之后新建的 run
    """
    results = list(executor.poll_submit_errors())
    pending = [a for a in executor.actions
               if a.get("accepted") and a.get("verify_kind") == "run"
               and not a.get("submit_error")]
    for a in pending:
        if cancel is not None and cancel.is_set():
            a["verify_result"] = "已取消 (动作结果由下一轮巡检收尾)"
            results.append({"vendor_id": a.get("vendor_id"), "ok": None,
                            "note": a["verify_result"]})
            continue
        r = _wait_run_outcome(a, deadline, executor, cancel=cancel)
        a["verify_result"] = r["note"]
        results.append(r)
    # 即时类 (clear_zombie) / session 类: 结果已在动作里 or 留给下一轮
    for a in executor.actions:
        if a.get("accepted") and a.get("verify_kind") in ("instant", "session"):
            if a.get("verify_kind") == "session":
                note = _session_check(a.get("vendor_id"))
                a["verify_result"] = note
                results.append({"vendor_id": a.get("vendor_id"), "ok": None, "note": note})
            else:
                results.append({"vendor_id": a.get("vendor_id"),
                                "ok": True, "note": a.get("detail", "")})
    return results


def _wait_run_outcome(action: dict, deadline: float, executor=None,
                      cancel: threading.Event | None = None) -> dict:
    """轮询 vendor 的 agent 触发 run 到终态. 超时返回 still_running (下一轮收尾).

    cancel 置位 → 立即返回已取消 (巡检要停了, run 本身继续后台跑完).

    run_id 没抓到时按 vendor 找最新的 agent run, 但必须晚于动作提交时间
    (submitted_at - 60s 容差) — 否则会把上一轮巡检的旧 run 结局错认成本轮动作的.
    """
    from ingest.query import get_run_detail, get_vendor_runs
    vid = action.get("vendor_id")
    run_id = action.get("run_id")
    submitted_at = None
    if action.get("submitted_at"):
        try:
            submitted_at = dt.datetime.fromisoformat(action["submitted_at"])
        except ValueError:
            pass
    verify_timeout = time.time() + _env_int("AGENT_PATROL_VERIFY_TIMEOUT", 600)
    effective_deadline = min(deadline, verify_timeout)

    while time.time() < effective_deadline:
        if cancel is not None and cancel.is_set():
            return {"vendor_id": vid, "run_id": run_id, "ok": None,
                    "note": "已取消 (动作结果由下一轮巡检收尾)"}
        # Future 抛错优先 (run 行可能根本没建出来)
        if executor is not None:
            errs = executor.poll_submit_errors()
            for e in errs:
                if e.get("vendor_id") == vid:
                    return {"vendor_id": vid, "run_id": None, "ok": False,
                            "note": e["note"]}
        try:
            if run_id:
                detail = get_run_detail(run_id)
            else:
                runs = [r for r in get_vendor_runs(vid, limit=5) if r["trigger"] == "agent"]
                detail = get_run_detail(runs[0]["id"]) if runs else None
                # 时间下界: 只认本轮提交之后创建的 run, 防 stale 误归因
                if detail and submitted_at and detail.get("started_at"):
                    try:
                        started = dt.datetime.fromisoformat(detail["started_at"])
                        if started.tzinfo is None:
                            started = started.replace(tzinfo=dt.timezone.utc)
                        if started < submitted_at - dt.timedelta(seconds=60):
                            detail = None  # 旧 run, 继续等新的
                    except ValueError:
                        pass
            if detail and detail.get("status") != "running":
                ok = detail.get("status") == "success"
                return {"vendor_id": vid, "run_id": detail.get("id"), "ok": ok,
                        "note": f"run #{detail.get('id')} {detail.get('status')}"
                                + (f": {(detail.get('error_msg') or '')[:150]}" if not ok else "")}
        except Exception as e:
            log.warning(f"[verify] 查询 {vid} run 状态失败 (继续等): {e}")
        time.sleep(VERIFY_POLL_INTERVAL)

    return {"vendor_id": vid, "run_id": run_id, "ok": None,
            "note": "still_running (超过验证窗口, 下一轮巡检自动收尾)"}


def _session_check(vendor_id: str | None) -> str:
    if not vendor_id:
        return "vendor 信息缺失"
    try:
        from vendors import get_vendor
        import login_flow
        vendor = get_vendor(vendor_id)
        if not vendor:
            return f"vendor {vendor_id} 不存在"
        st = login_flow.session_status(vendor)
        if st["status"] == "ok":
            return "登录已完成 (session 文件已出现)"
        if st["status"] == "waiting":
            return "浏览器登录进行中 (自动填表中或等人工 noVNC, 310s 超时, 下一轮巡检确认)"
        return f"登录未完成: {st['status']} — 下一轮巡检继续跟踪"
    except Exception as e:
        return f"session 检查失败: {e}"


def _build_verification(actions: list[dict], before: dict, after: dict | None) -> list[dict]:
    """before/after 信号对比 — 被 acted vendor 的信号条数变化."""
    if not after:
        return []
    def _sig_set(snap, vid):
        return [f"{s['type']}" for s in (snap.get("signals") or []) if s.get("vendor_id") == vid]
    vids = {a.get("vendor_id") for a in actions if a.get("accepted") and a.get("vendor_id")}
    out = []
    for vid in sorted(v for v in vids if v):
        b, a = _sig_set(before, vid), _sig_set(after, vid)
        out.append({
            "vendor_id": vid,
            "before": b, "after": a,
            "ok": len(a) < len(b) if b else None,
            "note": f"信号 {len(b)} → {len(a)}" + ("" if not b else (" ✓" if len(a) < len(b) else " ✗未消除")),
        })
    return out
