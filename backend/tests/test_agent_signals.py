"""signals 单测 — 确定性信号收集. 全部 monkeypatch 掉 DB/文件系统, 不连库.

模式: patch ingest.agent.signals 里 by-name import 的 query 函数 + login_flow 属性.
"""
from __future__ import annotations

import datetime as dt

import pytest

import ingest.agent.signals as sig


def _patch_common(monkeypatch, *, vendors=None, failed=(), missing=None,
                  running=None, zombies=None, freshness=None, hints=(),
                  slow_vids=None, slow_through=None, session_ok=None):
    vendors = vendors or [{"id": "kimi", "name": "kimi", "type": "kimi",
                           "base_url": "https://platform.kimi.com", "enabled": True}]
    missing = missing or {}
    running = running or []
    zombies = zombies or []
    freshness = freshness or {}
    slow_vids = slow_vids if slow_vids is not None else set()
    hints = list(hints)

    def fake_get_running_runs(min_age_hours=None):
        if min_age_hours is None:
            return running
        return zombies

    monkeypatch.setattr(sig, "load_vendors", lambda: vendors)
    monkeypatch.setattr(sig, "get_recently_failed_vendors",
                        lambda within_hours=48: list(failed))
    monkeypatch.setattr(sig, "get_missing_days_in_window",
                        lambda d0, d1: dict(missing))
    monkeypatch.setattr(sig, "get_running_runs", fake_get_running_runs)
    monkeypatch.setattr(sig, "get_freshness", lambda: dict(freshness))
    monkeypatch.setattr(sig, "get_vendor_runs", lambda vid, limit=3: [])
    monkeypatch.setattr(sig, "get_success_runs_with_hints",
                        lambda within_hours=26: hints)
    monkeypatch.setattr(sig, "slow_path_vendor_ids", lambda: set(slow_vids))
    monkeypatch.setattr(sig, "_slow_synced_through", lambda vids: dict(slow_through or {}))

    import login_flow
    monkeypatch.setattr(login_flow, "get_session_file",
                        lambda v: "/tmp/fake.json" if session_ok else None)
    monkeypatch.setattr(login_flow, "is_logging_in", lambda vid: False)


def test_all_empty_is_clean(monkeypatch):
    _patch_common(monkeypatch, session_ok=True)
    snap = sig.collect_signals()
    assert snap["signals"] == []
    assert snap["context"]["running_vendors"] == []


def test_failed_run_signal_with_run_id(monkeypatch):
    _patch_common(monkeypatch, session_ok=True,
                  failed=[{"vendor_id": "kimi", "vendor_name": "kimi", "run_id": 42,
                           "error_msg": "2026-08-26: RuntimeError: boom",
                           "finished_at": "2026-08-27T10:00:00+08:00"}])
    snap = sig.collect_signals()
    (s,) = [x for x in snap["signals"] if x["type"] == "failed_run"]
    assert s["vendor_id"] == "kimi"
    assert s["run_id"] == 42
    assert s["severity"] == "medium"


def test_failed_run_silent_empty_is_high(monkeypatch):
    _patch_common(monkeypatch, session_ok=True,
                  failed=[{"vendor_id": "kimi", "vendor_name": "kimi", "run_id": 43,
                           "error_msg": "silent-empty: 近 14 天均 cost=¥50/day",
                           "finished_at": None}])
    (s,) = [x for x in sig.collect_signals().get("signals", []) if x["type"] == "failed_run"]
    assert s["severity"] == "high"


def test_missing_days(monkeypatch):
    _patch_common(monkeypatch, session_ok=True,
                  missing={"volcengine": ["2026-08-24", "2026-08-25"]})
    (s,) = [x for x in sig.collect_signals()["signals"] if x["type"] == "missing_days"]
    assert s["vendor_id"] == "volcengine"
    assert s["days"] == ["2026-08-24", "2026-08-25"]
    assert s["severity"] == "medium"


def test_zombie_run_flagged(monkeypatch):
    _patch_common(monkeypatch, session_ok=True,
                  running=[{"run_id": 9, "vendor_id": "kimi", "trigger": "cron",
                            "started_at": "x", "age_hours": 3.0}],
                  zombies=[{"run_id": 9, "vendor_id": "kimi", "trigger": "cron",
                            "started_at": "x", "age_hours": 3.0}])
    snap = sig.collect_signals()
    (s,) = [x for x in snap["signals"] if x["type"] == "zombie_run"]
    assert s["run_id"] == 9 and s["severity"] == "high"
    # 同时进 running context, LLM 禁止对其触发
    assert "kimi" in snap["context"]["running_vendors"]


def test_zombie_excludes_backfill(monkeypatch):
    """backfill 合法跑数小时 (90 天窗口), 不能被当僵尸误杀."""
    backfill_run = {"run_id": 10, "vendor_id": "tencent", "trigger": "backfill",
                    "started_at": "x", "age_hours": 4.5}
    _patch_common(monkeypatch, session_ok=True,
                  running=[backfill_run], zombies=[backfill_run])
    snap = sig.collect_signals()
    assert not [x for x in snap["signals"] if x["type"] == "zombie_run"]
    # 但仍在 running context 里 (禁止触发动作)
    assert "tencent" in snap["context"]["running_vendors"]


def test_freshness_lag_allowed_margin(monkeypatch):
    yesterday = dt.date.today() - dt.timedelta(days=1)
    # kimi 允许 2 天 (T+1): 落后 2 天不应报
    _patch_common(monkeypatch, session_ok=True,
                  freshness={"kimi": yesterday - dt.timedelta(days=2)})
    assert not [x for x in sig.collect_signals()["signals"] if x["type"] == "freshness_lag"]
    # 落后 3 天应报
    _patch_common(monkeypatch, session_ok=True,
                  freshness={"kimi": yesterday - dt.timedelta(days=3)})
    assert [x for x in sig.collect_signals()["signals"] if x["type"] == "freshness_lag"]


def test_freshness_lag_pt_for_openai(monkeypatch):
    # 钉死时钟: CST 昨天 与 PT 昨天 差很远 (跨日切), openai 应按 PT 比
    monkeypatch.setattr(sig, "_yesterday_cst", lambda: dt.date(2026, 8, 27))
    monkeypatch.setattr(sig, "_yesterday_pt", lambda: dt.date(2026, 8, 26))
    _patch_common(monkeypatch,
                  vendors=[{"id": "openai", "name": "openai", "type": "openai",
                            "enabled": True}],
                  freshness={"openai": dt.date(2026, 8, 26)},  # 已到 PT 昨天 → 不报
                  session_ok=True)
    assert not [x for x in sig.collect_signals()["signals"] if x["type"] == "freshness_lag"]

    _patch_common(monkeypatch,
                  vendors=[{"id": "openai", "name": "openai", "type": "openai",
                            "enabled": True}],
                  freshness={"openai": dt.date(2026, 8, 23)},  # PT 落后 3 天 > 允许 2 → 报
                  session_ok=True)
    assert [x for x in sig.collect_signals()["signals"] if x["type"] == "freshness_lag"]


def test_slow_path_lag(monkeypatch):
    yesterday = dt.date.today() - dt.timedelta(days=1)
    _patch_common(monkeypatch, session_ok=True, slow_vids={"blueshirt"},
                  freshness={"blueshirt": yesterday},
                  slow_through={"blueshirt": yesterday - dt.timedelta(days=4)})
    (s,) = [x for x in sig.collect_signals()["signals"] if x["type"] == "slow_path_lag"]
    assert s["vendor_id"] == "blueshirt"
    assert s["lag_days"] == 4


def test_session_expired(monkeypatch):
    _patch_common(monkeypatch, session_ok=False)  # kimi 无 session 文件
    (s,) = [x for x in sig.collect_signals()["signals"] if x["type"] == "session_expired"]
    assert s["vendor_id"] == "kimi" and s["severity"] == "high"


def test_hint_suppressed_when_stronger_signal(monkeypatch):
    # 同 vendor 既有 failed_run 又有 hint → 只留 failed_run
    _patch_common(monkeypatch, session_ok=True,
                  failed=[{"vendor_id": "kimi", "vendor_name": "kimi", "run_id": 42,
                           "error_msg": "RuntimeError: boom", "finished_at": None}],
                  hints=[{"run_id": 41, "vendor_id": "kimi",
                          "error_msg": "stat-fallback", "rows_upserted": 0,
                          "window_start": None, "window_end": None, "finished_at": None}])
    types = {x["type"] for x in sig.collect_signals()["signals"]}
    assert "silent_empty_hint" not in types
    assert "failed_run" in types


def test_hint_alone_reported_low(monkeypatch):
    _patch_common(monkeypatch, session_ok=True,
                  hints=[{"run_id": 41, "vendor_id": "kimi",
                          "error_msg": "stat-fallback", "rows_upserted": 0,
                          "window_start": None, "window_end": None, "finished_at": None}])
    (s,) = [x for x in sig.collect_signals()["signals"] if x["type"] == "silent_empty_hint"]
    assert s["severity"] == "low"
