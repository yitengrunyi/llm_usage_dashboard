"""report 单测 — 指纹 / 卡片渲染 / 降级卡片 / 落库(fake session). 不连库."""
from __future__ import annotations

import ingest.agent.report as R


def _signals():
    return [
        {"type": "failed_run", "vendor_id": "kimi", "severity": "high", "detail": "boom"},
        {"type": "missing_days", "vendor_id": "volcengine", "severity": "medium", "detail": "缺 2 天"},
    ]


def test_fingerprint_order_insensitive():
    a = R.compute_fingerprint(_signals())
    b = R.compute_fingerprint(list(reversed(_signals())))
    assert a == b
    c = R.compute_fingerprint(_signals() + [{"type": "zombie_run", "vendor_id": "x",
                                             "severity": "high", "detail": ""}])
    assert a != c


def test_fingerprint_ignores_detail_text():
    s1 = [{"type": "failed_run", "vendor_id": "kimi", "detail": "error A"}]
    s2 = [{"type": "failed_run", "vendor_id": "kimi", "detail": "error B 完全不同"}]
    assert R.compute_fingerprint(s1) == R.compute_fingerprint(s2)


def test_render_report_md_contains_sections():
    report = {
        "id": 7, "status": "healed", "signals": _signals(),
        "summary": "已自愈",
        "diagnosis": "kimi 是 session 失效;\nvolcengine 上游 502.",
        "actions": [{"tool": "trigger_ingest", "args": {"vendor_id": "volcengine"},
                     "accepted": True, "detail": "已触发重拉",
                     "verify_result": "run #777 success"}],
        "escalate": False,
    }
    md = R._render_report_md(report)
    assert "信号" in md and "kimi" in md
    assert "诊断 (LLM)" in md and "session 失效" in md
    assert "动作与验证" in md and "run #777 success" in md
    assert "reports/7" in md


def test_render_report_md_truncates_diagnosis():
    report = {"id": 1, "status": "healed", "signals": [], "diagnosis": "x" * 3000,
              "actions": [], "escalate": False}
    md = R._render_report_md(report)
    assert "截断" in md and len(md) < 3000


def test_render_report_md_repeat_warning():
    report = {"id": 1, "status": "escalated", "signals": _signals(),
              "actions": [], "escalate": True, "repeat_count": 2}
    md = R._render_report_md(report)
    assert "连续 3 轮" in md  # repeat_count=2 → 之前 2 轮 + 本轮 = 3


def test_send_patrol_card_template_by_status(monkeypatch):
    sent = {}
    import ingest.alert as alert
    monkeypatch.setattr(alert, "_send_card",
                        lambda title, md, template="red": sent.update(
                            title=title, md=md, template=template) or True)
    R.send_patrol_card("05:00", {"id": 1, "status": "healed", "signals": [], "actions": [],
                                 "escalate": False, "summary": "ok"})
    assert sent["template"] == "green"
    R.send_patrol_card("05:00", {"id": 2, "status": "escalated", "signals": [], "actions": [],
                                 "escalate": True, "summary": "x"})
    assert sent["template"] == "red"
    R.send_patrol_card("05:00", {"id": 3, "status": "degraded", "signals": [], "actions": [],
                                 "escalate": False, "summary": "x"})
    assert sent["template"] == "orange"


def test_send_degraded_card(monkeypatch):
    sent = {}
    import ingest.alert as alert
    monkeypatch.setattr(alert, "_send_card",
                        lambda title, md, template="red": sent.update(
                            title=title, md=md, template=template) or True)
    snap = {"signals": [{"type": "session_expired", "vendor_id": "kimi",
                         "severity": "high", "detail": "文件缺失"},
                        {"type": "slow_path_lag", "vendor_id": "blueshirt",
                         "severity": "medium", "detail": "落后 3 天"}]}
    ok = R.send_degraded_card("05:00", snap, error="LLM HTTP 500")
    assert ok is True
    assert "降级" in sent["title"]
    assert sent["template"] == "red"  # 有 high 信号
    assert "curl -X POST /api/vendors/kimi/login" in sent["md"]
    assert "补拆分字段" in sent["md"]  # slow_path_lag 建议命中


def test_degraded_summary_counts_by_type():
    s = R.degraded_summary({"signals": _signals()}, "timeout")
    assert "failed_run×1" in s and "missing_days×1" in s and "timeout" in s


# ───────── 落库 (fake SessionLocal) ─────────
class _FakeSession:
    def __init__(self):
        self._store = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def add(self, obj):
        obj.id = len(self._store) + 1
        self._store[obj.id] = obj

    def commit(self):
        for o in self._store.values():
            if getattr(o, "status", None) == "running" and o.finished_at is not None:
                o.status = o.status  # no-op, 保持真实行为近似

    def get(self, model, rid):
        return self._store.get(rid)


def test_create_and_finalize_report(monkeypatch):
    fake = _FakeSession()
    monkeypatch.setattr(R, "SessionLocal", lambda: fake)
    rid = R.create_report("05:00", signals=_signals())
    assert rid == 1
    R.finalize_report(rid, status="healed", llm_used=True, summary="ok",
                      actions=[], escalate=False)
    row = fake._store[rid]
    assert row.status == "healed" and row.summary == "ok" and row.finished_at is not None
    # 不存在的 id 不炸
    R.finalize_report(999, status="healed")


def test_mark_stale_running_failed(monkeypatch):
    """startup 清理: 重启遗留的 running 行标 error, 已终态的行不动."""
    fake = _FakeSession()
    monkeypatch.setattr(R, "SessionLocal", lambda: fake)
    zombie = R.create_report("05:00")              # 停在 running (重启中断)
    done = R.create_report("09:00")
    R.finalize_report(done, status="clean", llm_used=False)

    class _ExecResult:
        def __init__(self, rows): self._rows = rows
        def scalars(self): return self
        def all(self): return self._rows

    def _execute(stmt):  # fake 不解析 whereclause, 按语义挑 running 行 (本函数只有这一个查询)
        return _ExecResult([o for o in fake._store.values() if o.status == "running"])
    fake.execute = _execute

    n = R.mark_stale_running_failed()
    assert n == 1
    assert fake._store[zombie].status == "error"
    assert "重启" in fake._store[zombie].summary
    assert fake._store[done].status == "clean"      # 已终态的不被误伤
