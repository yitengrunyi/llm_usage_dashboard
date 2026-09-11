"""Agent 工具集 — 读工具 + 动作工具 + 代码级护栏.

护栏原则: 系统提示词里的规则只是建议, 这里的代码是法律.
- 动作只允许打到 signals 里出现过的 vendor (白名单)
- 每 vendor 每轮 1 个动作; 全局 max_actions 上限 (默认 4)
- trigger_ingest 窗口 ≤14 天 (blueshirt ≤13 天 retention), end ≤ vendor 的"昨天"
- 有 running run 的 vendor 禁止触发 (先查库, uniq_running_per_vendor 索引兜底)
- 动作线程池提交 run_ingest(trigger="agent"), 绝不用 run_ingest_with_retry
  (它会 sleep 到 90 分钟; "重试"语义 = 下一轮巡检再看)

拒绝不抛异常: 返回 {"error": "..."} 给 LLM 让它调整策略, 同时记入 actions 审计.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta, timezone

from vendors import get_vendor

from ingest.query import get_run_detail, get_running_runs, get_vendor_runs, slow_path_vendor_ids

log = logging.getLogger("ingest.agent.tools")

CST = timezone(timedelta(hours=8))
try:
    from zoneinfo import ZoneInfo
    PT = ZoneInfo("America/Los_Angeles")
except ImportError:  # pragma: no cover
    PT = timezone(timedelta(hours=-8))

BLUESHIRT_RETENTION_DAYS = 13

# 动作线程池: 模块级 (巡检退出后长动作继续跑完自己的线程, 不阻塞报告)
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent-act")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _vendor_yesterday(vendor_id: str, today_cst: dt.date) -> dt.date:
    """vendor 视角的"昨天": openai 按 PT, 其余按 CST (跟 cron 窗口一致)."""
    if vendor_id == "openai":
        return (dt.datetime.now(PT) - timedelta(days=1)).date()
    return today_cst - timedelta(days=1)


class ToolExecutor:
    def __init__(self, snapshot: dict,
                 max_actions: int | None = None,
                 max_window_days: int | None = None):
        self.snapshot = snapshot
        self.signals = snapshot["signals"]
        self.signal_vendors = {s["vendor_id"] for s in self.signals}
        self.today_cst = dt.date.fromisoformat(snapshot["today_cst"])
        self.running_vendors = set(snapshot["context"].get("running_vendors") or [])
        self.max_actions = max_actions if max_actions is not None \
            else _env_int("AGENT_PATROL_MAX_ACTIONS", 4)
        self.max_window_days = max_window_days if max_window_days is not None \
            else _env_int("AGENT_PATROL_MAX_WINDOW_DAYS", 14)
        self.actions: list[dict] = []           # 审计: 含被拒绝的
        self.per_vendor_acted: set[str] = set()
        # 动作 Future 池: key = 动作在 self.actions 里的下标.
        # Future 本身绝不进 actions (要落 JSONB); 提交失败在这里被捕获并写回动作
        self._futures: dict[int, object] = {}

    def poll_submit_errors(self) -> list[dict]:
        """检查已提交动作的 Future: run_ingest 在建 run 行**之前**抛的错
        (IntegrityError 并发竞态 / vendor 配置缺失 / DB 抖动) 不会有 run 行,
        也不会有日志 — 必须查 Future 才能发现. 已完成的记 submit_error 返回."""
        import concurrent.futures as _cf
        errored = []
        for idx, fut in list(self._futures.items()):
            if not fut.done():
                continue
            del self._futures[idx]
            exc = fut.exception()
            if exc is None:
                continue
            action = self.actions[idx]
            note = f"提交失败: {type(exc).__name__}: {exc}"[:300]
            action["submit_error"] = note
            action["verify_result"] = note
            errored.append({"vendor_id": action.get("vendor_id"), "ok": False, "note": note})
            log.error(f"[tools] 动作 {action.get('tool')} {action.get('args')} {note}")
        return errored

    # ───────────────────────── 对 LLM 暴露的 schema ─────────────────────────
    def specs(self) -> list[dict]:
        return [
            {"type": "function", "function": {
                "name": "get_run_detail",
                "description": "查某个 run 的完整信息 (含未截断的完整报错 traceback), 诊断失败原因用",
                "parameters": {"type": "object",
                               "properties": {"run_id": {"type": "integer"}},
                               "required": ["run_id"]}}},
            {"type": "function", "function": {
                "name": "get_vendor_history",
                "description": "查某 vendor 最近 N 次入库 run (状态/窗口/行数/报错摘要), 判断偶发还是连续失败",
                "parameters": {"type": "object",
                               "properties": {"vendor_id": {"type": "string"},
                                              "limit": {"type": "integer", "default": 5}},
                               "required": ["vendor_id"]}}},
            {"type": "function", "function": {
                "name": "get_session_status",
                "description": "查某 vendor 的浏览器登录态 (ok / expired / waiting / not_needed)",
                "parameters": {"type": "object",
                               "properties": {"vendor_id": {"type": "string"}},
                               "required": ["vendor_id"]}}},
            {"type": "function", "function": {
                "name": "trigger_ingest",
                "description": "重新触发某 vendor 的入库 (幂等 UPSERT). 只拉缺的天, 窗口尽量小. 动作工具",
                "parameters": {"type": "object",
                               "properties": {"vendor_id": {"type": "string"},
                                              "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                                              "end_date": {"type": "string", "description": "YYYY-MM-DD"}},
                               "required": ["vendor_id", "start_date", "end_date"]}}},
            {"type": "function", "function": {
                "name": "clear_zombie_run",
                "description": "把卡死超 2h 的僵尸 run 标 failed, 解除该 vendor 的并发锁. 动作工具",
                "parameters": {"type": "object",
                               "properties": {"run_id": {"type": "integer"}},
                               "required": ["run_id"]}}},
            {"type": "function", "function": {
                "name": "trigger_slow_fill",
                "description": "触发慢路径补 prompt/completion/cache 拆分字段 (仅 blueshirt/nulls; apevon 走单天重拉). 动作工具",
                "parameters": {"type": "object",
                               "properties": {"vendor_id": {"type": "string"},
                                              "day": {"type": "string", "description": "YYYY-MM-DD 要补的那天"}},
                               "required": ["vendor_id", "day"]}}},
            {"type": "function", "function": {
                "name": "request_login",
                "description": "session 过期时发起浏览器登录: 配了 .env 账密会自动填表当场完成, 未配/自动失败才需人工 5 分钟内通过 noVNC. 动作工具",
                "parameters": {"type": "object",
                               "properties": {"vendor_id": {"type": "string"}},
                               "required": ["vendor_id"]}}},
            {"type": "function", "function": {
                "name": "submit_report",
                "description": "提交最终结构化诊断报告, 巡检循环结束",
                "parameters": {"type": "object",
                               "properties": {
                                   "summary": {"type": "string", "description": "一句话中文总结"},
                                   "diagnosis_md": {"type": "string", "description": "逐 vendor 诊断全文 (markdown)"},
                                   "escalate": {"type": "boolean", "description": "是否有需人工介入的问题"},
                                   "escalate_reason": {"type": "string"},
                                   "per_vendor": {"type": "array", "items": {"type": "object", "properties": {
                                       "vendor_id": {"type": "string"},
                                       "diagnosis": {"type": "string"},
                                       "action": {"type": "string"},
                                       "escalate": {"type": "boolean"}}}}},
                               "required": ["summary", "diagnosis_md", "escalate"]}}},
        ]

    # ───────────────────────── 执行入口 ─────────────────────────
    def execute(self, name: str, args: dict) -> dict:
        handlers = {
            "get_run_detail": self._t_get_run_detail,
            "get_vendor_history": self._t_get_vendor_history,
            "get_session_status": self._t_get_session_status,
            "trigger_ingest": self._t_trigger_ingest,
            "clear_zombie_run": self._t_clear_zombie_run,
            "trigger_slow_fill": self._t_trigger_slow_fill,
            "request_login": self._t_request_login,
        }
        handler = handlers.get(name)
        if not handler:
            return {"error": f"unknown tool: {name}"}
        try:
            return handler(**{k: v for k, v in (args or {}).items() if k != "_"})
        except TypeError as e:
            return {"error": f"参数不合法: {e}"}
        except Exception as e:
            log.error(f"[tools] {name} 执行异常: {type(e).__name__}: {e}")
            return {"error": f"{type(e).__name__}: {e}"}

    def _record(self, tool: str, args: dict, accepted: bool, detail: str, **extra) -> dict:
        entry = {"tool": tool, "args": args, "accepted": accepted, "detail": detail, **extra}
        self.actions.append(entry)
        return entry

    # ───────────────────────── 通用护栏 ─────────────────────────
    def _action_gate(self, vendor_id: str, action: str) -> str | None:
        """返回拒绝原因; None = 放行. 顺序: 白名单 → 并发 → 单 vendor 一次 → 全局上限."""
        if vendor_id not in self.signal_vendors:
            return f"{vendor_id} 不在本轮信号集内, 拒绝动作"
        if vendor_id in self.running_vendors:
            return f"{vendor_id} 有 run 正在跑, 禁止触发"
        # 二次确认 (LLM 循环期间可能有新 run 启动)
        if any(r["vendor_id"] == vendor_id for r in get_running_runs()):
            return f"{vendor_id} 刚启动了新的 running run, 禁止触发"
        if vendor_id in self.per_vendor_acted:
            return f"{vendor_id} 本轮已执行过动作, 每轮每 vendor 限 1 个"
        if len([a for a in self.actions if a.get("accepted")]) >= self.max_actions:
            return f"全局动作上限 {self.max_actions} 已用完"
        return None

    def _parse_day(self, s: str) -> dt.date:
        return dt.date.fromisoformat(str(s).strip())

    # ───────────────────────── 读工具 ─────────────────────────
    def _t_get_run_detail(self, run_id: int) -> dict:
        detail = get_run_detail(int(run_id))
        if not detail:
            return {"error": f"run {run_id} 不存在"}
        if detail.get("error_msg") and len(detail["error_msg"]) > 4000:
            detail["error_msg"] = detail["error_msg"][:4000] + "\n... (截断)"
        return detail

    def _t_get_vendor_history(self, vendor_id: str, limit: int = 5) -> dict:
        limit = max(1, min(int(limit or 5), 10))
        return {"vendor_id": vendor_id, "runs": get_vendor_runs(vendor_id, limit=limit)}

    def _t_get_session_status(self, vendor_id: str) -> dict:
        vendor = get_vendor(vendor_id)
        if not vendor:
            return {"error": f"vendor {vendor_id} 不存在"}
        import login_flow
        return login_flow.session_status(vendor)

    # ───────────────────────── 动作工具 ─────────────────────────
    def _t_trigger_ingest(self, vendor_id: str, start_date: str, end_date: str) -> dict:
        args = {"vendor_id": vendor_id, "start_date": start_date, "end_date": end_date}
        try:
            start = self._parse_day(start_date)
            end = self._parse_day(end_date)
        except (ValueError, TypeError) as e:
            return self._record("trigger_ingest", args, False, f"日期不合法: {e}")

        reject = self._action_gate(vendor_id, "trigger_ingest")
        if reject:
            return self._record("trigger_ingest", args, False, reject)
        if start > end:
            return self._record("trigger_ingest", args, False, f"start {start} > end {end}")
        window_days = (end - start).days + 1
        if window_days > self.max_window_days:
            return self._record("trigger_ingest", args, False,
                                f"窗口 {window_days} 天超上限 {self.max_window_days}")
        yesterday = _vendor_yesterday(vendor_id, self.today_cst)
        if end > yesterday:
            return self._record("trigger_ingest", args, False,
                                f"end {end} 超过 {vendor_id} 的昨天 {yesterday} (今天数据走 live)")
        if vendor_id == "blueshirt" and (self.today_cst - start).days > BLUESHIRT_RETENTION_DAYS:
            return self._record("trigger_ingest", args, False,
                                f"start 超过 blueshirt {BLUESHIRT_RETENTION_DAYS} 天 retention, 不可恢复")

        from ingest.job import run_ingest
        fut = _pool.submit(run_ingest, vendor_id, start, end, trigger="agent", attempt=1)
        self.per_vendor_acted.add(vendor_id)
        self._record("trigger_ingest", args, True,
                     f"已触发 {start}~{end} 重拉", run_id=None, vendor_id=vendor_id,
                     verify_kind="run", window=[start.isoformat(), end.isoformat()],
                     submitted_at=dt.datetime.now(dt.timezone.utc).isoformat())
        self._futures[len(self.actions) - 1] = fut
        # 给 run 行创建留点时间, 拿 run_id 供验证 (拿不到也不影响, 验证阶段会按 vendor 找)
        run_id = None
        import time as _t
        for _ in range(6):
            if fut.done() and fut.exception() is not None:
                break  # 提交就失败了, 别等 run 行
            _t.sleep(0.5)
            runs = get_vendor_runs(vendor_id, limit=1)
            if runs and runs[0]["trigger"] == "agent" and runs[0]["status"] == "running":
                run_id = runs[0]["id"]
                break
        self.actions[-1]["run_id"] = run_id
        if fut.done() and fut.exception() is not None:
            note = f"提交失败: {type(fut.exception()).__name__}: {fut.exception()}"[:300]
            self.actions[-1]["submit_error"] = note
            self.actions[-1]["verify_result"] = note
            self._futures.pop(len(self.actions) - 1, None)  # 已消费, 防止 poll 重复上报
            log.error(f"[tools] trigger_ingest {vendor_id} {note}")
            return {"error": note}
        log.info(f"[tools] trigger_ingest {vendor_id} {start}~{end} run_id={run_id} (async)")
        return {"status": "scheduled", "run_id": run_id, "vendor_id": vendor_id,
                "window": [start.isoformat(), end.isoformat()],
                "note": "后台执行中, 结果由巡检验证阶段确认"}

    def _t_clear_zombie_run(self, run_id: int) -> dict:
        args = {"run_id": run_id}
        zombie_ids = {s["run_id"] for s in self.signals if s["type"] == "zombie_run"}
        if int(run_id) not in zombie_ids:
            return self._record("clear_zombie_run", args, False,
                                f"run {run_id} 不在本轮僵尸信号里 (只允许清确认过的僵尸)")
        from ingest.job import mark_stale_running_failed
        affected = mark_stale_running_failed(
            run_ids=[int(run_id)], note="[agent] 巡检判定僵尸 run, 定点清理")
        vendor_id = next(s["vendor_id"] for s in self.signals if s.get("run_id") == int(run_id))
        self.per_vendor_acted.add(vendor_id)
        ok = bool(affected)
        self._record("clear_zombie_run", args, ok,
                     "已标 failed" if ok else "run 已不在 running 状态 (可能刚好结束)",
                     run_id=int(run_id), vendor_id=vendor_id, verify_kind="instant")
        return {"status": "ok" if ok else "noop", "affected": affected, "vendor_id": vendor_id}

    def _t_trigger_slow_fill(self, vendor_id: str, day: str) -> dict:
        args = {"vendor_id": vendor_id, "day": day}
        try:
            d = self._parse_day(day)
        except (ValueError, TypeError) as e:
            return self._record("trigger_slow_fill", args, False, f"日期不合法: {e}")

        reject = self._action_gate(vendor_id, "trigger_slow_fill")
        if reject:
            return self._record("trigger_slow_fill", args, False, reject)
        if vendor_id not in slow_path_vendor_ids():
            return self._record("trigger_slow_fill", args, False,
                                f"{vendor_id} 不是慢路径 vendor (blueshirt/nulls/apevon)")
        yesterday = _vendor_yesterday(vendor_id, self.today_cst)
        if d > yesterday:
            return self._record("trigger_slow_fill", args, False,
                                f"day {d} 超过昨天 {yesterday}")
        if (self.today_cst - d).days > BLUESHIRT_RETENTION_DAYS:
            return self._record("trigger_slow_fill", args, False,
                                f"day {d} 超过 {BLUESHIRT_RETENTION_DAYS} 天 retention, 不可恢复")

        if vendor_id == "apevon":
            # apevon 没有专门 slow 模块 — 单天重拉主入库 (与 router /slow-fill 端点同款分发)
            from ingest.job import run_ingest
            fut = _pool.submit(run_ingest, vendor_id, d, d, trigger="agent", attempt=1)
            kind = "single-day-reingest"
        else:
            from ingest.job import run_slow_fill_dedicated
            fut = _pool.submit(run_slow_fill_dedicated, vendor_id, d, trigger="agent")
            kind = "slow-fill"
        self.per_vendor_acted.add(vendor_id)
        self._record("trigger_slow_fill", args, True,
                     f"已触发 {kind} {d}", vendor_id=vendor_id, verify_kind="run",
                     submitted_at=dt.datetime.now(dt.timezone.utc).isoformat())
        self._futures[len(self.actions) - 1] = fut
        log.info(f"[tools] trigger_slow_fill {vendor_id} {d} ({kind}, async)")
        return {"status": "scheduled", "kind": kind, "vendor_id": vendor_id, "day": day}

    def _t_request_login(self, vendor_id: str) -> dict:
        args = {"vendor_id": vendor_id}
        reject = self._action_gate(vendor_id, "request_login")
        if reject:
            return self._record("request_login", args, False, reject)
        import login_flow
        vendor = get_vendor(vendor_id)
        if not vendor:
            return self._record("request_login", args, False, f"vendor {vendor_id} 不存在")
        if vendor.get("type") not in login_flow.LOGIN_TYPES:
            return self._record("request_login", args, False,
                                f"{vendor_id} ({vendor.get('type')}) 不需要浏览器登录")

        novnc = os.environ.get("AGENT_NOVNC_URL", "").strip() or "http://<服务器IP>:6080 (noVNC)"
        try:
            started = login_flow.start_login_async(vendor_id)
        except (LookupError, ValueError) as e:
            return self._record("request_login", args, False, str(e))
        self.per_vendor_acted.add(vendor_id)

        # 配了 .env 账密 → 自动填表, 巡检当场等结果 (成功立即验证, 不再等下一轮)
        if login_flow.has_credentials(vendor_id):
            res = login_flow.wait_login_result(vendor_id, timeout=180)
            if res and res.get("status") == "ok":
                note = f"自动登录成功 (mode={res.get('mode', 'auto')})"
                self._record("request_login", args, True, note,
                             vendor_id=vendor_id, verify_kind="session")
                log.info(f"[tools] request_login {vendor_id} → {note}")
                return {"status": "ok", "vendor_id": vendor_id, "note": note}
            # 自动未成 (180s 超时/失败) → 浏览器仍开着 (manual_fallback), 留给人工
            msg = (res or {}).get("message") or ""
            if msg == "登录失败":  # 结果槽占位符 = 线程还没跑完
                msg = "180s 内未完成"
            log.warning(f"[tools] request_login {vendor_id} 自动登录未完成: {msg}, 转人工 noVNC")
            self._record("request_login", args, True,
                         f"自动登录未完成 ({msg[:100]}), 浏览器已留给人工 (310s 超时)",
                         vendor_id=vendor_id, verify_kind="session")
            return {
                "status": "waiting", "vendor_id": vendor_id, "novnc": novnc,
                "note": (f"自动登录未完成: {msg[:150]}. 浏览器已弹出, "
                         "请人工 5 分钟内通过 noVNC 完成登录; "
                         "登录完成由下一轮巡检验证 session 文件出现"),
            }

        self._record("request_login", args, True,
                     f"登录流程已发起 ({started['status']}), 未配账密走人工, 310s 超时",
                     vendor_id=vendor_id, verify_kind="session")
        log.info(f"[tools] request_login {vendor_id} → 浏览器已弹出, 未配账密, 等人工 noVNC")
        return {
            "status": started["status"],
            "vendor_id": vendor_id,
            "novnc": novnc,
            "note": ("浏览器已弹出, 该 vendor 未配自动登录账密 "
                     f"(.env {vendor_id.upper()}_USERNAME/_PASSWORD), "
                     "请人工 5 分钟内通过 noVNC 完成登录; "
                     "登录完成由下一轮巡检验证 session 文件出现"),
        }
