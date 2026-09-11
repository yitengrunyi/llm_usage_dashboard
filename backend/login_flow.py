"""Vendor 浏览器登录编排 — 从 main.py 抽出, 供 HTTP 端点与 agent 巡检共用.

负责:
- session 文件定位 (session_file_path / get_session_file)
- 登录进行中状态 (跨进程文件锁 .{vendor_id}.logging_in)
- 各类型登录完成判据 + 自动登录站点配置 (kimi OAuth / apevon 双账号 / bigmodel)
- 异步发起登录 (start_login_async) + 等待结果 (wait_login_result) + 取消 (cancel_login)
- 02:30 预检 (precheck_auto_login): session 过期且 .env 有账密 → 自动登录,
  赶在 03:00 摄取 cron 前修好, 数据不断; 每 vendor 每天最多自动尝试 1 次.

自动登录: .env 配了 {VENDOR_ID}_USERNAME/_PASSWORD (如 KIMI_USERNAME) 就自动
填表提交 (save_session._try_autofill), 失败降级人工 noVNC; 没配 = 纯人工 (原行为).

main.py 的 /api/vendors/{id}/login 系列端点在这里只做 HTTP 壳;
ingest/agent 的 request_login 工具直接调 start_login_async (有账密时等自动结果).
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
from pathlib import Path
from urllib.parse import urlparse

from save_session import save_session, STATE_DIR
from vendors import get_vendor, load_vendors

log = logging.getLogger("login_flow")

# 需要浏览器登录的 vendor 类型
LOGIN_TYPES = ("new-api", "kimi", "apevon", "bigmodel")

# 登录中的跨进程标记 (文件锁) + 进程内 cancel event / 结果槽 / 线程句柄
_LOGIN_EVENTS_LOCK = threading.Lock()
_LOGIN_CANCEL_EVENTS: dict[str, threading.Event] = {}
_LOGIN_RESULTS: dict[str, dict] = {}
_LOGIN_THREADS: dict[str, threading.Thread] = {}

CST = dt.timezone(dt.timedelta(hours=8))

# apevon (codingflow.ai): 首页 "/" 顶部「登录」按钮弹 modal, 自动填表前要先点开.
# SPA 挂载可能比 domcontentloaded 晚 — save_session 侧轮询点击本 JS (最多 15s).
# button/a 优先; 兜底 span/div 里文本最内层那个 (document 序最后 = 最深).
_OPEN_APEVON_LOGIN_JS = """() => {
  const all = [...document.querySelectorAll('button, a, span, div')]
    .filter(e => e.offsetParent !== null && /^登\\s*录$/.test(e.textContent.trim()));
  const primary = all.filter(e => e.tagName === 'BUTTON' || e.tagName === 'A');
  const target = primary[0] || all[all.length - 1];
  if (!target) return false;
  target.click();
  return true;
}"""

# bigmodel: 登录页默认停在短信/扫码 tab — 密码框 (el-input) 在 DOM 里但 hidden,
# 自动填表前先点「账号登录」tab 切过去 (2026-08 实测, 报错特征: password resolved to hidden).
_OPEN_BIGMODEL_ACCOUNT_TAB_JS = """() => {
  const els = [...document.querySelectorAll('button, a, span, div, li, label')]
    .filter(e => e.offsetParent !== null
      && /^(账号登录|密码登录|账号密码登录)$/.test(e.textContent.trim()));
  if (!els.length) return false;
  els[0].click();
  return true;
}"""


def session_file_path(vendor: dict) -> str | None:
    """vendor 的 session 文件路径 (不管存在与否). 非登录型 vendor 返回 None."""
    if vendor.get("type") not in LOGIN_TYPES:
        return None
    domain = urlparse(vendor["base_url"]).hostname.replace(".", "_")
    return str(STATE_DIR / f"{domain}.json")


def get_session_file(vendor: dict) -> str | None:
    """session 文件路径, 文件存在才返回 (登录态可能可用的必要条件)."""
    from pathlib import Path
    path = session_file_path(vendor)
    if path is None:
        return None
    p = Path(path)
    return str(p) if p.exists() else None


def is_logging_in(vendor_id: str) -> bool:
    return (STATE_DIR / f".{vendor_id}.logging_in").exists()


def set_logging_in(vendor_id: str, value: bool):
    lock_file = STATE_DIR / f".{vendor_id}.logging_in"
    if value:
        lock_file.touch()
    elif lock_file.exists():
        lock_file.unlink()


def session_status(vendor: dict) -> dict:
    """登录态四态: not_needed / waiting / expired / ok."""
    vtype = vendor.get("type")
    if vtype not in LOGIN_TYPES:
        return {"status": "not_needed", "vendor_type": vtype,
                "message": "此供应商不需要登录"}
    if is_logging_in(vendor["id"]):
        return {"status": "waiting", "vendor_type": vtype,
                "message": "浏览器已弹出，请完成登录..."}
    if not get_session_file(vendor):
        return {"status": "expired", "vendor_type": vtype,
                "message": "未登录，请点击登录"}
    return {"status": "ok", "vendor_type": vtype, "message": "已登录"}


def has_credentials(vendor_id: str) -> bool:
    """.env 里配了该 vendor 的自动登录账密 (USERNAME + PASSWORD 都非空)."""
    prefix = vendor_id.upper()
    return bool(os.environ.get(f"{prefix}_USERNAME", "").strip()
                and os.environ.get(f"{prefix}_PASSWORD", "").strip())


def _login_config(vendor: dict) -> tuple[str, str | None, str | None,
                                         str | None, list[str] | None]:
    """(login_path, done_url_contains, done_check_js, open_login_js, selector_hints)
    — 各类型登录完成判据 + 自动填表站点差异.

    kimi 走 moonshot 域名登录, 登录完成判断 = URL 回到 platform.kimi.com/console
    apevon (codingflow.ai) 改版后 /workbench 已 404, 首页 "/" 顶部"登录"按钮弹 modal,
    完成后 localStorage.user 写入。
    注意: codingflow 是「门户+工作台」双账号体系, localStorage.user 出现过先写门户 id
    再被工作台 id 覆盖的中间态 (实测 6472→92), 拿门户 id 调采集接口会 401 "与登录用户不匹配"。
    所以完成判据不能只看 "user 存在", 而是用 user.id + cookie 实调一次 /api/log/self/stat
    (与 apevon_client 同一鉴权组合) — 调通才保存, 存下来的 session 必可用。
    bigmodel 登录完成后 URL 回到 /console/overview
    """
    vtype = vendor.get("type")
    if vtype == "kimi":
        # 登录完成判据: 必须等到 localStorage 写入 token, 不能只看 URL 回到 /console。
        # kimi 走 OAuth(PKCE): 跳回 /console 后 SPA 仍需异步拿 code 换 token 才写入
        # localStorage. 只判 URL 会拍快照太早 → 存下的 session 没有 token → 报 "未登录"。
        return ("/console/account", None,
                '() => window.location.href.includes("platform.kimi.com/console")'
                ' && !!localStorage.getItem("token")',
                None, None)
    if vtype == "apevon":
        return ("/", None,
                'async () => {'
                ' try {'
                '  const u = JSON.parse(localStorage.getItem("user") || "null");'
                '  if (!u || !u.id) return false;'
                '  const now = Math.floor(Date.now() / 1000);'
                '  const r = await fetch("/api/log/self/stat?start_timestamp=" + (now - 3600) + "&end_timestamp=" + now,'
                '    { headers: { "New-Api-User": String(u.id) }, credentials: "include" });'
                '  const j = await r.json();'
                '  return !!(j && j.success === true);'
                ' } catch (e) { return false; }'
                '}',
                _OPEN_APEVON_LOGIN_JS, None)
    if vtype == "bigmodel":
        return ("/login?redirect=%2Fconsole%2Foverview", "/console/overview", None,
                _OPEN_BIGMODEL_ACCOUNT_TAB_JS, None)
    # 通用 new-api (blueshirt 等): 标准账密表单
    return ("/login", None, None, None, None)


def start_login_async(vendor_id: str, manual_fallback: bool = True) -> dict:
    """非阻塞发起登录: 弹浏览器, 立即返回. 结果用 wait_login_result 取.

    .env 配了 {VENDOR_ID}_USERNAME/_PASSWORD → 自动填表提交 (mode=auto),
    否则纯人工 (原行为). 自动失败时: manual_fallback=True 保留 5 分钟人工窗口,
    False (预检场景) 90s 快速失败.
    返回 {"status": "started"} 或 {"status": "waiting"} (已在登录中).
    vendor 不存在 / 类型不需要登录 → ValueError (调用方转 404/400).
    """
    vendor = get_vendor(vendor_id)
    if not vendor:
        raise LookupError(f"vendor {vendor_id} 不存在")
    if vendor.get("type") not in LOGIN_TYPES:
        raise ValueError("此供应商不需要登录")

    if is_logging_in(vendor_id):
        return {"status": "waiting", "message": "登录窗口已打开，请完成登录"}

    set_logging_in(vendor_id, True)
    cancel_event = threading.Event()
    with _LOGIN_EVENTS_LOCK:
        _LOGIN_CANCEL_EVENTS[vendor_id] = cancel_event
        _LOGIN_RESULTS[vendor_id] = {"status": "failed", "message": "登录失败"}
    login_path, done_url_contains, done_check_js, open_login_js, selector_hints = \
        _login_config(vendor)
    prefix = vendor_id.upper()
    username = os.environ.get(f"{prefix}_USERNAME", "").strip() or None
    password = os.environ.get(f"{prefix}_PASSWORD", "").strip() or None
    auto = bool(username and password)
    if auto:
        log.info(f"[login-flow] {vendor_id} 配有账密 → 自动填表 (fallback={'人工' if manual_fallback else '无'})")

    def do_login():
        try:
            res = save_session(vendor["base_url"], login_path=login_path,
                               done_url_contains=done_url_contains,
                               done_check_js=done_check_js,
                               cancel_event=cancel_event,
                               username=username, password=password,
                               open_login_js=open_login_js,
                               selector_hints=selector_hints,
                               manual_fallback=manual_fallback)
            _LOGIN_RESULTS[vendor_id] = {"status": "ok", "message": "登录成功",
                                         "mode": res.get("mode", "manual")}
        except Exception as e:
            _LOGIN_RESULTS[vendor_id] = {"status": "failed", "message": str(e)}
        finally:
            set_logging_in(vendor_id, False)
            with _LOGIN_EVENTS_LOCK:
                _LOGIN_CANCEL_EVENTS.pop(vendor_id, None)

    t = threading.Thread(target=do_login, daemon=True, name=f"login-{vendor_id}")
    with _LOGIN_EVENTS_LOCK:
        _LOGIN_THREADS[vendor_id] = t
    t.start()
    log.info(f"[login-flow] {vendor_id} 登录流程已启动 (非阻塞, auto={auto})")
    return {"status": "started", "message": "登录流程已启动", "auto": auto}


def wait_login_result(vendor_id: str, timeout: float = 310) -> dict | None:
    """等登录线程结束 (save_session 自身 5 分钟超时, 这里 310s 收尾). 没有进行中的登录返回 None."""
    with _LOGIN_EVENTS_LOCK:
        t = _LOGIN_THREADS.get(vendor_id)
    if t is None:
        return None
    t.join(timeout=timeout)
    with _LOGIN_EVENTS_LOCK:
        _LOGIN_THREADS.pop(vendor_id, None)
        return _LOGIN_RESULTS.get(vendor_id)


def cancel_login(vendor_id: str) -> dict:
    """中止进行中的登录 (用户关 VNC tab 后调这个, 后端立刻关浏览器 + 释放 lock).

    幂等: 即使没有 active login 也返回 ok, 顺便清掉可能残留的 lock file
    (上次进程崩了没清干净的情况).
    """
    with _LOGIN_EVENTS_LOCK:
        ev = _LOGIN_CANCEL_EVENTS.get(vendor_id)
    if ev is not None:
        ev.set()
        return {"status": "ok", "message": "已发送取消信号"}
    if is_logging_in(vendor_id):
        set_logging_in(vendor_id, False)
        return {"status": "ok", "message": "已清理残留登录锁"}
    return {"status": "ok", "message": "无进行中的登录"}


def delete_session(vendor: dict) -> bool:
    """删除 session 文件 (logout). 返回是否删除了文件."""
    path = session_file_path(vendor)
    if path:
        from pathlib import Path
        p = Path(path)
        if p.exists():
            p.unlink()
            return True
    return False


# ─────────────────────────── 02:30 预检 ───────────────────────────

def _autologin_marker(vendor_id: str) -> Path:
    """当日已自动尝试标记 (内容 = CST 日期). 防密码改了/上游故障时反复撞登录."""
    return STATE_DIR / f".{vendor_id}.autologin"


def precheck_auto_login() -> dict:
    """CST 02:30 预检: session 过期且配了账密的登录型 vendor → 自动登录.

    赶在 03:00 摄取 cron 之前修好, session 过期不再造成摄取失败. 顺序执行
    (过期是少数事件, 不值得并发). 每家失败推飞书一张卡; 成功静默 (cron 日志有记录).
    """
    from ingest.alert import _send_card
    results: dict[str, dict | None] = {}
    today = dt.datetime.now(CST).strftime("%Y-%m-%d")

    for vendor in load_vendors():
        vid = vendor["id"]
        if vendor.get("type") not in LOGIN_TYPES:
            continue
        if is_logging_in(vid):
            continue
        if session_status(vendor)["status"] == "ok":
            continue
        if not has_credentials(vid):
            # 没账密只能人工 — 不在这里弹浏览器 (无人时段), 摄取失败后 agent 巡检走 noVNC
            log.info(f"[precheck] {vid} session 过期但未配账密, 跳过 (留给 agent/人工)")
            continue
        marker = _autologin_marker(vid)
        if marker.exists() and marker.read_text().strip() == today:
            continue

        log.info(f"[precheck] {vid} session 过期且有账密 → 自动登录")
        try:
            start_login_async(vid, manual_fallback=False)
        except (LookupError, ValueError) as e:
            log.warning(f"[precheck] {vid} 启动登录失败: {e}")
            continue
        # goto 90s + 填表 ~30s + 90s 快速失败线 → 200s 覆盖典型路径
        res = wait_login_result(vid, timeout=200)
        marker.write_text(today)
        results[vid] = res
        if res and res.get("status") == "ok":
            log.info(f"[precheck] {vid} 自动登录成功 (mode={res.get('mode')})")
        else:
            msg = (res or {}).get("message", "未知原因")
            log.error(f"[precheck] {vid} 自动登录失败: {msg}")
            _send_card(
                f"⚠️ 自动登录失败 - {vid}",
                f"**{vid}** 02:30 预检自动登录未成功 (当日不再重试)\n"
                f"原因: `{msg[:200]}`\n"
                f"请人工登录: `curl -X POST /api/vendors/{vid}/login` "
                f"(浏览器/noVNC 完成), 或检查 `.env` 里 `{vid.upper()}_USERNAME/_PASSWORD` 是否正确",
            )
    return results
