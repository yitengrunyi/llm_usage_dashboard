"""patrol 单测 — 编排/降级/锁. 全部 monkeypatch, 不连库不调 LLM."""
from __future__ import annotations

import threading

import pytest

import ingest.agent.patrol as P


def _sig(vid="kimi", typ="failed_run", sev="medium"):
    return {"type": typ, "vendor_id": vid, "severity": sev, "detail": "x"}


class _FakeExecutor:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.actions = []

    def specs(self):
        return []


@pytest.fixture
def fx(monkeypatch):
    state = {"created": [], "finalized": [], "cards": [], "degraded": [], "reports": []}

    def _create(slot, signals=None):
        state["created"].append((slot, signals))
        return len(state["created"])

    def _finalize(rid, **fields):
        state["finalized"].append((rid, fields))

    monkeypatch.setattr(P.report_mod, "create_report", _create)
    monkeypatch.setattr(P.report_mod, "finalize_report", _finalize)
    monkeypatch.setattr(P.report_mod, "get_report",
                        lambda rid: {"id": rid, "status": "x", "signals": [], "actions": []})
    monkeypatch.setattr(P.report_mod, "send_patrol_card",
                        lambda slot, rep: state["cards"].append((slot, rep)) or True)
    monkeypatch.setattr(P.report_mod, "send_degraded_card",
                        lambda slot, snap, err=None: state["degraded"].append((slot, err)) or True)
    monkeypatch.setattr(P.report_mod, "previous_unresolved_count",
                        lambda fp, rid, lookback=10: 0)
    def _fake_init(self, snapshot, **kw):
        self.snapshot = snapshot
        self.actions = []
        self._futures = {}
    monkeypatch.setattr(P.ToolExecutor, "__init__", _fake_init)
    monkeypatch.setattr(P.ToolExecutor, "poll_submit_errors", lambda self: [])
    return state


def _set_signals(monkeypatch, signals):
    monkeypatch.setattr(P, "collect_signals", lambda: {
        "today_cst": "2026-08-27", "signals": signals,
        "context": {"running_vendors": []}})


def test_lock_held_returns_none():
    P._lock.acquire()
    try:
        assert P.run_patrol("manual") is None
    finally:
        P._lock.release()


def test_request_cancel_kills_pending_watcher():
    """取消要同时杀掉: 持锁中的旧巡检事件 + 排队中的替换巡检 (watcher 事件)."""
    import threading as _th
    P._lock.acquire()
    live_cancel = _th.Event()
    queued_cancel = _th.Event()
    P._cancel = live_cancel
    P._pending_watchers[99] = queued_cancel
    try:
        assert P.request_cancel() == "cancelling"
        assert live_cancel.is_set() and queued_cancel.is_set()
    finally:
        P._pending_watchers.pop(99, None)
        P._cancel = None
        P._lock.release()
    assert P.request_cancel() == "not_running"


def test_clean_cron_exits_without_report(fx, monkeypatch):
    _set_signals(monkeypatch, [])
    assert P.run_patrol("05:00") is None
    assert fx["created"] == []      # cron 干净: 不落行不落卡
    assert fx["cards"] == [] and fx["degraded"] == []


def test_clean_manual_creates_clean_row(fx, monkeypatch):
    _set_signals(monkeypatch, [])
    rid = P.run_patrol("manual")
    assert rid == 1
    slot, _ = fx["created"][0]
    assert slot == "manual"
    _, fields = fx["finalized"][0]
    assert fields["status"] == "clean"


def test_no_key_degraded_with_high_signal_escalates(fx, monkeypatch):
    _set_signals(monkeypatch, [_sig(sev="high")])
    monkeypatch.setattr(P.llm_mod, "llm_configured", lambda: False)
    rid = P.run_patrol("05:00")
    assert rid == 1
    _, fields = fx["finalized"][0]
    assert fields["status"] == "escalated"  # 高危 + 无 LLM → 升级
    assert fields["llm_used"] is False
    assert fx["degraded"] and not fx["cards"]


def test_no_key_degraded_low_signal(fx, monkeypatch):
    _set_signals(monkeypatch, [_sig(sev="low")])
    monkeypatch.setattr(P.llm_mod, "llm_configured", lambda: False)
    P.run_patrol("09:00")
    _, fields = fx["finalized"][0]
    assert fields["status"] == "degraded" and fields["escalate"] is False


def test_llm_error_falls_back_to_degraded(fx, monkeypatch):
    _set_signals(monkeypatch, [_sig()])
    monkeypatch.setattr(P.llm_mod, "llm_configured", lambda: True)

    def boom(*a, **kw):
        raise P.llm_mod.LLMError("HTTP 500")
    monkeypatch.setattr(P.llm_mod, "run_diagnosis_loop", boom)
    P.run_patrol("19:00")
    assert fx["degraded"][0][1] == "HTTP 500"
    _, fields = fx["finalized"][0]
    assert fields["status"] == "degraded" and fields["llm_used"] is False


def test_llm_happy_healed_sends_card(fx, monkeypatch):
    _set_signals(monkeypatch, [_sig()])
    monkeypatch.setattr(P.llm_mod, "llm_configured", lambda: True)
    monkeypatch.setattr(P.llm_mod, "run_diagnosis_loop",
                        lambda snap, ex, prompt, **kw: {
                            "summary": "已重拉修复", "diagnosis_md": "d",
                            "escalate": False, "per_vendor": [],
                            "llm_meta": {"turns": 2}})
    P.run_patrol("05:00")
    _, fields = fx["finalized"][0]
    assert fields["status"] == "healed" and fields["llm_used"] is True
    assert fields["diagnosis"] == "d"
    assert fx["cards"] and not fx["degraded"]


def test_llm_escalate_propagates(fx, monkeypatch):
    _set_signals(monkeypatch, [_sig()])
    monkeypatch.setattr(P.llm_mod, "llm_configured", lambda: True)
    monkeypatch.setattr(P.llm_mod, "run_diagnosis_loop",
                        lambda snap, ex, prompt, **kw: {
                            "summary": "需人工", "diagnosis_md": "d",
                            "escalate": True, "escalate_reason": "session 失效",
                            "per_vendor": [], "llm_meta": {}})
    P.run_patrol("05:00")
    _, fields = fx["finalized"][0]
    assert fields["status"] == "escalated"
    assert "session 失效" in fields["escalate_reason"]


def test_signal_collection_failure_error_report(fx, monkeypatch):
    def boom():
        raise RuntimeError("DB down")
    monkeypatch.setattr(P, "collect_signals", boom)
    rid = P.run_patrol("05:00")
    assert rid is not None
    _, fields = fx["finalized"][0]
    assert fields["status"] == "error"


def test_top_level_never_raises(fx, monkeypatch):
    # finalize 之后 send_patrol_card 抛错也不该外抛 (顶层兜底)
    _set_signals(monkeypatch, [_sig()])
    monkeypatch.setattr(P.llm_mod, "llm_configured", lambda: False)
    monkeypatch.setattr(P.report_mod, "send_degraded_card",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("feishu down")))
    # 内部 _run 的卡片发送在 finalize 之后 — 异常会冒到 run_patrol 顶层兜底
    result = P.run_patrol("05:00")  # 不抛即通过
    assert result is None or result  # 顶层兜底可能写了 error 报告


# ───────── ok=None / ok=False 的升级语义 (审查修复) ─────────
def test_verify_ok_none_does_not_escalate(fx, monkeypatch):
    """still_running (ok=None) 是未定态, 不该被当成失败升级红色."""
    monkeypatch.setattr(P, "_verify_actions",
                        lambda ex, snap, dl, cancel=None: [{"vendor_id": "kimi", "ok": None,
                                               "note": "still_running"}])
    monkeypatch.setattr(P, "_build_verification", lambda *a: [])
    _set_signals(monkeypatch, [_sig()])
    monkeypatch.setattr(P.llm_mod, "llm_configured", lambda: True)
    monkeypatch.setattr(P.llm_mod, "run_diagnosis_loop",
                        lambda snap, ex, prompt, **kw: {
                            "summary": "动作仍在跑", "diagnosis_md": "d",
                            "escalate": False, "per_vendor": [], "llm_meta": {}})
    P.run_patrol("05:00")
    _, fields = fx["finalized"][0]
    assert fields["status"] == "healed" and fields["escalate"] is False


def test_verify_ok_false_escalates_even_degraded(fx, monkeypatch):
    """降级模式下, 已执行动作的明确失败 (ok=False) 也必须升级, 不能被 has_high 覆盖."""
    monkeypatch.setattr(P, "_verify_actions",
                        lambda ex, snap, dl, cancel=None: [{"vendor_id": "kimi", "ok": False,
                                               "note": "提交失败: IntegrityError"}])
    monkeypatch.setattr(P, "_build_verification", lambda *a: [])
    _set_signals(monkeypatch, [_sig(sev="low")])  # 低危信号, has_high=False
    monkeypatch.setattr(P.llm_mod, "llm_configured", lambda: False)
    P.run_patrol("09:00")
    _, fields = fx["finalized"][0]
    assert fields["status"] == "escalated"
    assert "IntegrityError" in fields["escalate_reason"]


def test_stale_run_not_attributed(fx, monkeypatch):
    """run_id 没抓到时, 提交时间之前的旧 agent run 不能被认成本轮动作的结局."""
    import time as _time
    from datetime import datetime, timedelta, timezone
    import ingest.agent.patrol as patrol_mod

    submitted = datetime.now(timezone.utc)
    old_run = {"id": 100, "vendor_id": "kimi", "trigger": "agent", "status": "success",
               "attempt": 1, "window_start": None, "window_end": None,
               "rows_upserted": 5, "error_msg": None,
               "started_at": (submitted - timedelta(hours=2)).isoformat(),
               "finished_at": None}
    monkeypatch.setattr("ingest.query.get_vendor_runs", lambda vid, limit=5: [old_run])
    monkeypatch.setattr("ingest.query.get_run_detail",
                        lambda rid: {**old_run, "id": rid, "started_at": old_run["started_at"]})
    monkeypatch.setattr("time.sleep", lambda s: None)  # 轮询间隔不休眠
    monkeypatch.setenv("AGENT_PATROL_VERIFY_TIMEOUT", "2")  # 2s 后超时
    action = {"vendor_id": "kimi", "run_id": None, "verify_kind": "run",
              "accepted": True,
              "submitted_at": submitted.isoformat()}

    class _Ex:
        actions = [action]
        def poll_submit_errors(self):
            return []
    r = patrol_mod._wait_run_outcome(action, _time.time() + 5, _Ex())
    # 旧 run (2h 前) 在整个窗口内被时间下界挡掉 → 超时返回未定态, 而非"success"
    assert r["ok"] is None and "still_running" in r["note"]
