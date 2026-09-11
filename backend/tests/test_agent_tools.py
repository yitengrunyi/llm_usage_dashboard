"""tools 单测 — 护栏全覆盖. patch 掉 DB 查询与 job 动作函数, 不连库不发请求."""
from __future__ import annotations

import datetime as dt

import pytest

import ingest.agent.tools as T


TODAY = dt.date(2026, 8, 27)


def _snapshot(signals, running=()):
    return {
        "today_cst": TODAY.isoformat(),
        "signals": signals,
        "context": {"running_vendors": list(running), "vendor_names": {}},
    }


def _sig(vid, typ, run_id=None):
    return {"type": typ, "vendor_id": vid, "severity": "medium",
            "detail": "x", **({"run_id": run_id} if run_id else {})}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)


@pytest.fixture
def fx(monkeypatch):
    """patch tools 模块的 by-name import + ingest.job 动作函数 + 同步假线程池."""
    import concurrent.futures as _cf
    calls = {"run_ingest": [], "slow_fill": [], "mark_stale": [], "login": []}

    class _SyncPool:  # submit 立即执行, 测试免竞态免 sleep; 返回真 Future (poll 接口兼容)
        def submit(self, fn, *a, **kw):
            fut = _cf.Future()
            try:
                fut.set_result(fn(*a, **kw))
            except Exception as e:  # noqa: BLE001 — 同步传递异常给 Future
                fut.set_exception(e)
            return fut

    monkeypatch.setattr(T, "_pool", _SyncPool())
    monkeypatch.setattr(T, "get_running_runs", lambda: [])
    monkeypatch.setattr(T, "get_run_detail", lambda rid: None)
    monkeypatch.setattr(T, "get_vendor_runs",
                        lambda vid, limit=5: [{"id": 555, "trigger": "agent",
                                               "status": "running"}])
    monkeypatch.setattr(T, "slow_path_vendor_ids", lambda: {"blueshirt", "nulls", "apevon"})
    import ingest.job as job
    monkeypatch.setattr(job, "run_ingest",
                        lambda vid, s, e, trigger=None, attempt=1:
                        calls["run_ingest"].append((vid, s, e, trigger)) or 777)
    monkeypatch.setattr(job, "run_slow_fill_dedicated",
                        lambda vid, d, trigger=None:
                        calls["slow_fill"].append((vid, d, trigger)) or 888)
    monkeypatch.setattr(job, "mark_stale_running_failed",
                        lambda threshold_hours=2, run_ids=None, note="":
                        calls["mark_stale"].append(run_ids) or list(run_ids or []))
    import login_flow
    monkeypatch.setattr(login_flow, "start_login_async",
                        lambda vid: calls["login"].append(vid) or {"status": "started"})
    # 自动登录路径: creds = 有账密的 vendor 集合, login_result = wait_login_result 返回值
    calls["creds"] = set()
    calls["login_result"] = None
    monkeypatch.setattr(login_flow, "has_credentials", lambda vid: vid in calls["creds"])
    monkeypatch.setattr(login_flow, "wait_login_result",
                        lambda vid, timeout=310: calls["login_result"])
    return calls


def _mk(signals, running=(), **kw):
    return T.ToolExecutor(_snapshot(signals, running), **kw)


# ───────────────────────── trigger_ingest 护栏 ─────────────────────────
def test_trigger_happy(fx):
    ex = _mk([_sig("kimi", "failed_run")])
    out = ex.execute("trigger_ingest", {"vendor_id": "kimi",
                                        "start_date": "2026-08-25",
                                        "end_date": "2026-08-26"})
    assert out["status"] == "scheduled"
    assert fx["run_ingest"] == [("kimi", dt.date(2026, 8, 25), dt.date(2026, 8, 26), "agent")]
    assert ex.actions[0]["accepted"] is True


def test_trigger_reject_vendor_not_in_signals(fx):
    ex = _mk([_sig("kimi", "failed_run")])
    out = ex.execute("trigger_ingest", {"vendor_id": "volcengine",
                                        "start_date": "2026-08-26", "end_date": "2026-08-26"})
    assert "error" in out or out.get("accepted") is False
    assert not fx["run_ingest"]


def test_trigger_reject_window_too_big(fx):
    ex = _mk([_sig("kimi", "failed_run")])
    out = ex.execute("trigger_ingest", {"vendor_id": "kimi",
                                        "start_date": "2026-08-01", "end_date": "2026-08-26"})
    assert out.get("accepted") is False and "超上限" in out["detail"]
    assert not fx["run_ingest"]


def test_trigger_reject_end_after_yesterday(fx):
    # today_cst=2026-08-27 → CST vendor 昨天 = 08-26
    ex = _mk([_sig("kimi", "failed_run")])
    out = ex.execute("trigger_ingest", {"vendor_id": "kimi",
                                        "start_date": "2026-08-26", "end_date": "2026-08-27"})
    assert out.get("accepted") is False and "昨天" in out["detail"]


def test_trigger_reject_blueshirt_retention(fx):
    ex = _mk([_sig("blueshirt", "missing_days")])
    out = ex.execute("trigger_ingest", {"vendor_id": "blueshirt",
                                        "start_date": "2026-08-01", "end_date": "2026-08-10"})
    assert out.get("accepted") is False and "retention" in out["detail"]


def test_trigger_reject_running_vendor(fx, monkeypatch):
    # ① snapshot context 里 running
    ex = _mk([_sig("kimi", "failed_run")], running=["kimi"])
    out = ex.execute("trigger_ingest", {"vendor_id": "kimi",
                                        "start_date": "2026-08-26", "end_date": "2026-08-26"})
    assert out.get("accepted") is False and "正在跑" in out["detail"]
    # ② 二次查库发现新 running (LLM 循环期间别人先触发了)
    ex2 = _mk([_sig("kimi", "failed_run")])
    monkeypatch.setattr(T, "get_running_runs",
                        lambda: [{"run_id": 1, "vendor_id": "kimi"}])
    out2 = ex2.execute("trigger_ingest", {"vendor_id": "kimi",
                                          "start_date": "2026-08-26", "end_date": "2026-08-26"})
    assert out2.get("accepted") is False


def test_trigger_per_vendor_once(fx):
    ex = _mk([_sig("kimi", "failed_run")], max_actions=4)
    first = ex.execute("trigger_ingest", {"vendor_id": "kimi",
                                          "start_date": "2026-08-26", "end_date": "2026-08-26"})
    assert first["status"] == "scheduled"
    second = ex.execute("trigger_ingest", {"vendor_id": "kimi",
                                           "start_date": "2026-08-26", "end_date": "2026-08-26"})
    assert second.get("accepted") is False and "限 1 个" in second["detail"]


def test_trigger_global_action_cap(fx):
    ex = _mk([_sig("kimi", "failed_run"), _sig("volcengine", "failed_run")], max_actions=1)
    assert ex.execute("trigger_ingest", {"vendor_id": "kimi",
                                         "start_date": "2026-08-26",
                                         "end_date": "2026-08-26"})["status"] == "scheduled"
    out = ex.execute("trigger_ingest", {"vendor_id": "volcengine",
                                        "start_date": "2026-08-26", "end_date": "2026-08-26"})
    assert out.get("accepted") is False and "上限" in out["detail"]


# ───────────────────────── clear_zombie_run ─────────────────────────
def test_clear_zombie_rejects_unknown_run(fx):
    ex = _mk([_sig("kimi", "zombie_run", run_id=9)])
    out = ex.execute("clear_zombie_run", {"run_id": 123})
    assert out.get("accepted") is False
    assert not fx["mark_stale"]


def test_clear_zombie_happy(fx):
    ex = _mk([_sig("kimi", "zombie_run", run_id=9)])
    out = ex.execute("clear_zombie_run", {"run_id": 9})
    assert out["status"] == "ok"
    assert fx["mark_stale"] == [[9]]


# ───────────────────────── trigger_slow_fill ─────────────────────────
def test_slow_fill_rejects_non_slow_vendor(fx, monkeypatch):
    monkeypatch.setattr(T, "slow_path_vendor_ids", lambda: {"blueshirt", "nulls"})
    ex = _mk([_sig("kimi", "slow_path_lag")])
    out = ex.execute("trigger_slow_fill", {"vendor_id": "kimi", "day": "2026-08-26"})
    assert out.get("accepted") is False


def test_slow_fill_blueshirt(fx):
    ex = _mk([_sig("blueshirt", "slow_path_lag")])
    out = ex.execute("trigger_slow_fill", {"vendor_id": "blueshirt", "day": "2026-08-26"})
    assert out["kind"] == "slow-fill"
    assert fx["slow_fill"] == [("blueshirt", dt.date(2026, 8, 26), "agent")]


def test_slow_fill_apevon_single_day_reingest(fx):
    ex = _mk([_sig("apevon", "slow_path_lag")])
    out = ex.execute("trigger_slow_fill", {"vendor_id": "apevon", "day": "2026-08-26"})
    assert out["kind"] == "single-day-reingest"
    assert fx["run_ingest"] == [("apevon", dt.date(2026, 8, 26), dt.date(2026, 8, 26), "agent")]


def test_slow_fill_rejects_beyond_retention(fx):
    ex = _mk([_sig("blueshirt", "slow_path_lag")])
    out = ex.execute("trigger_slow_fill", {"vendor_id": "blueshirt", "day": "2026-08-05"})
    assert out.get("accepted") is False and "retention" in out["detail"]


# ───────────────────────── request_login ─────────────────────────
def test_login_rejects_non_login_vendor(fx, monkeypatch):
    import vendors as vendors_mod
    monkeypatch.setattr(T, "get_vendor",
                        lambda vid: {"id": vid, "type": "tc-cloud"})
    ex = _mk([_sig("tencent", "failed_run")])
    out = ex.execute("request_login", {"vendor_id": "tencent"})
    assert out.get("accepted") is False
    assert not fx["login"]


def test_login_happy_starts_async(fx, monkeypatch):
    monkeypatch.setattr(T, "get_vendor",
                        lambda vid: {"id": vid, "type": "kimi"})
    ex = _mk([_sig("kimi", "session_expired")])
    out = ex.execute("request_login", {"vendor_id": "kimi"})
    assert out["status"] == "started" and "noVNC" in out["note"]
    assert fx["login"] == ["kimi"]


def test_login_auto_success_confirmed_inline(fx, monkeypatch):
    """配了账密 → 等自动结果, 成功当场确认 (不再等下一轮巡检)."""
    monkeypatch.setattr(T, "get_vendor",
                        lambda vid: {"id": vid, "type": "kimi"})
    fx["creds"] = {"kimi"}
    fx["login_result"] = {"status": "ok", "mode": "auto"}
    ex = _mk([_sig("kimi", "session_expired")])
    out = ex.execute("request_login", {"vendor_id": "kimi"})
    assert out["status"] == "ok" and "自动登录成功" in out["note"]
    assert ex.actions[-1]["accepted"] is True


def test_login_auto_fail_falls_back_human(fx, monkeypatch):
    """自动失败 (如密码错) → 浏览器留给人工, note 说明转 noVNC."""
    monkeypatch.setattr(T, "get_vendor",
                        lambda vid: {"id": vid, "type": "kimi"})
    fx["creds"] = {"kimi"}
    fx["login_result"] = {"status": "failed", "message": "TimeoutError: 登录超时"}
    ex = _mk([_sig("kimi", "session_expired")])
    out = ex.execute("request_login", {"vendor_id": "kimi"})
    assert out["status"] == "waiting" and out.get("novnc")
    assert "登录超时" in out["note"]


# ───────────────────────── Future 提交失败不吞错 ─────────────────────────
def test_submit_failure_recorded_not_swallowed(fx, monkeypatch):
    """run_ingest 建 run 行之前抛错 (IntegrityError 竞态等) → 必须记 submit_error,
    动作不能再以 scheduled 姿态去烧 600s 验证窗口."""
    import ingest.job as job

    def boom(vid, s, e, trigger=None, attempt=1):
        raise RuntimeError("IntegrityError: uniq_running_per_vendor 竞态")
    monkeypatch.setattr(job, "run_ingest", boom)
    # fx 的 _SyncPool 同步执行 boom 并把异常塞进真 Future → 确定性失败
    ex = _mk([_sig("kimi", "failed_run")])
    out = ex.execute("trigger_ingest", {"vendor_id": "kimi",
                                        "start_date": "2026-08-26", "end_date": "2026-08-26"})
    assert "提交失败" in (out.get("error") or "")
    assert "submit_error" in ex.actions[0]
    # poll 幂等: 已消费的 future 不重复报
    assert ex.poll_submit_errors() == []


def test_poll_submit_errors_catches_late_failure(fx):
    """LLM 循环结束后才抛的错, patrol 验证阶段 poll 也要能捞到."""
    import concurrent.futures as _cf

    class _SlowFailPool:
        def submit(self, fn, *a, **kw):
            return _cf.Future()  # 不执行, 模拟还在排队; 稍后手动置异常

    T._pool = _SlowFailPool()  # fixture 的 monkeypatch 会在 teardown 恢复原 pool
    ex = _mk([_sig("kimi", "failed_run")])
    ex.execute("trigger_ingest", {"vendor_id": "kimi",
                                  "start_date": "2026-08-26", "end_date": "2026-08-26"})
    assert ex.poll_submit_errors() == []          # 未完成 → 无错
    fut = next(iter(ex._futures.values()))
    fut.set_exception(RuntimeError("DB down"))
    errs = ex.poll_submit_errors()
    assert len(errs) == 1 and errs[0]["ok"] is False
    assert "DB down" in ex.actions[0]["submit_error"]


# ───────────────────────── 读工具 + 未知工具 ─────────────────────────
def test_unknown_tool(fx):
    ex = _mk([])
    assert "error" in ex.execute("nope", {})


def test_get_run_detail_not_found(fx):
    ex = _mk([])
    assert "error" in ex.execute("get_run_detail", {"run_id": 1})


def test_specs_shape(fx):
    specs = _mk([]).specs()
    names = {s["function"]["name"] for s in specs}
    assert {"get_run_detail", "get_vendor_history", "get_session_status",
            "trigger_ingest", "clear_zombie_run", "trigger_slow_fill",
            "request_login", "submit_report"} == names
