"""
弹出浏览器窗口保存登录态。有人工模式 (默认) 与自动填账密模式:

- 人工: 弹出浏览器, 等用户手动登录 (原行为, 保持不变)
- 自动: username/password 传入时, Playwright 自动找表单填账密提交;
       表单结构不认识 / 提交后没登录成功 → 静默降级人工 (浏览器仍开着),
       除非 manual_fallback=False (预检场景, 无人看, 快速失败).

自动模式的站点差异 (打开表单的前置动作 / 用户名框选择器) 由调用方
login_flow._login_config 注入, 这里只放通用引擎.
"""
import sys
import os
import signal
import time
import threading
from pathlib import Path

import shutil

# Docker 容器内用 Playwright 自带 Chromium，macOS 用系统 Chrome
CHROME_PATH = (
    shutil.which("chromium")
    or shutil.which("google-chrome")
    or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
)
STATE_DIR = Path(__file__).parent / "config" / "sessions"

# 启发式找用户名框: 在密码框所在 form (无 form 则全文档) 里挑"最像用户名"的
# 可见输入框 (name/id/placeholder/autocomplete 含 user/email/account/phone 优先,
# 其次 type=email, 再次 type=text), 打上 data 标记返回给 Python 侧 locator。
# 返回选中框的 name (日志用); null = 没有候选。
_FIND_USER_JS = """() => {
  const pwds = [...document.querySelectorAll('input[type="password"]')]
    .filter(i => i.offsetParent !== null && !i.disabled);
  if (!pwds.length) return null;
  const scope = pwds[0].closest('form') || document;
  const bad = ['password', 'hidden', 'checkbox', 'radio', 'submit', 'button', 'file'];
  const cands = [...scope.querySelectorAll('input')]
    .filter(i => i !== pwds[0])
    .filter(i => !bad.includes(i.type))
    .filter(i => i.offsetParent !== null && !i.disabled && !i.readOnly);
  const hint = i => ((i.name || '') + (i.id || '') + (i.placeholder || '') + (i.autocomplete || ''));
  const score = i => (/user|email|account|phone|mail|手机|账号|邮箱|用户/i.test(hint(i)) ? 3
                      : (i.type === 'email' ? 3 : i.type === 'text' ? 2 : 0));
  cands.sort((a, b) => score(b) - score(a));
  if (!cands.length) return null;
  cands[0].setAttribute('data-autologin-user', '1');
  return (cands[0].name || cands[0].id || cands[0].placeholder || '?');
}"""

# 点提交: 在密码框所在 form 范围内找 submit 型按钮 (type=submit 或文本是
# 登录/Login/Sign in), 找不到就点最后一个按钮; 再由 Python 侧兜底密码框回车。
_CLICK_SUBMIT_JS = """() => {
  const pwds = [...document.querySelectorAll('input[type="password"]')]
    .filter(i => i.offsetParent !== null);
  if (!pwds.length) return false;
  const scope = pwds[0].closest('form') || document;
  const btns = [...scope.querySelectorAll('button, input[type="submit"]')]
    .filter(b => b.offsetParent !== null && !b.disabled);
  const isSubmit = b => b.type === 'submit'
    || /^(登\\s*录|立即登录|login|log\\s*in|sign\\s*in)$/i.test((b.textContent || b.value || '').trim());
  const target = btns.find(isSubmit) || btns[btns.length - 1];
  if (!target) return false;
  target.click();
  return true;
}"""


def _try_autofill(page, username: str, password: str,
                  open_login_js: str | None, selector_hints: list[str] | None) -> bool:
    """自动填账密并提交. 返回是否已提交; 任何一步失败返回 False, 调用方降级人工."""
    try:
        if open_login_js:
            # SPA 场景: domcontentloaded 返回时 React/Vue 往往还没挂载出 header/tab,
            # 一次 evaluate 大概率扑空 — 轮询点击直到成功 (最多 15s).
            open_deadline = time.time() + 15
            while time.time() < open_deadline:
                try:
                    if page.evaluate(open_login_js):
                        break
                except Exception:
                    pass
                page.wait_for_timeout(1000)
            page.wait_for_timeout(1500)  # modal 动画
        # 密码框: 25s — kimi 走 moonshot OAuth 域名跳转, 要等跳转完成才出现
        page.wait_for_selector('input[type="password"]', state="visible", timeout=25_000)
        # 用户名框: 每站实测后的 hints 优先, 没有则启发式
        user_loc = None
        for sel in (selector_hints or []):
            try:
                loc = page.locator(sel).first
                if loc.is_visible():
                    user_loc = loc
                    break
            except Exception:
                pass
        if user_loc is None:
            if page.evaluate(_FIND_USER_JS):
                user_loc = page.locator('[data-autologin-user]').first
        pwd_loc = page.locator('input[type="password"]:visible').first
        if user_loc:
            user_loc.fill(username, timeout=5_000)
        pwd_loc.fill(password, timeout=5_000)
        if not page.evaluate(_CLICK_SUBMIT_JS):
            pwd_loc.press("Enter")  # 无按钮的纯回车表单兜底
        return True
    except Exception as e:
        print(f"自动填表未命中 ({type(e).__name__}: {e}), 请人工完成登录")
        return False


def save_session(base_url: str, login_path: str = "/login",
                 done_url_contains: str | None = None,
                 done_check_js: str | None = None,
                 cancel_event: threading.Event | None = None,
                 username: str | None = None,
                 password: str | None = None,
                 open_login_js: str | None = None,
                 selector_hints: list[str] | None = None,
                 manual_fallback: bool = True) -> dict:
    """打开浏览器登录, 把 cookies + localStorage 一起保存到 state.json。

    base_url: 用于 domain → state 文件名 (与 _get_session_file 对齐)
    login_path: 拼接到 base_url 后的初始打开路径
    done_url_contains: 登录完成判断 — URL 包含该字符串时认为登录成功
    done_check_js: 自定义 done check JS expression, 优先于 done_url_contains。
                   适用于 SPA 登录是 modal 不切路由的场景 (e.g. apevon)。
                   例: '() => !!localStorage.getItem("user")'
    cancel_event: 外部可以 set() 来提前中止登录 (用户关 VNC tab 后端死等的解药)。
    username/password: 都非空时启用自动填表; 缺省 = 纯人工 (原行为)。
    open_login_js: 打开登录表单的前置 JS (apevon 首页要点「登录」弹 modal)。
    selector_hints: 用户名框 CSS 选择器候选 (每站实测后补; 启发式兜底)。
    manual_fallback: 自动提交后未完成时是否保留人工窗口。True = 浏览器继续开
                     满 5 分钟等人 (agent 场景, 人可能看着飞书卡片过来);
                     False = 90s 快速失败关浏览器 (预检场景, 无人看)。

    返回 {"mode": "auto"|"manual", "state_file": ...}; 失败抛异常。
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    from urllib.parse import urlparse
    from playwright.sync_api import sync_playwright

    domain = urlparse(base_url).hostname.replace(".", "_")
    state_file = STATE_DIR / f"{domain}.json"

    pw = sync_playwright().start()
    # --disable-blink-features=AutomationControlled + 抹 navigator.webdriver:
    # 国内站风控验证码 (文字点选等) 按自动化特征打分触发, webdriver 标记是最重扣分项.
    # 不保证归零 — 真弹了就人工点 (manual_fallback 窗口), 自动填表已把成本降到只剩点验证码.
    launch_args = {"headless": False,
                   "args": ["--disable-blink-features=AutomationControlled"]}
    if os.path.exists(CHROME_PATH):
        launch_args["executable_path"] = CHROME_PATH
    browser = pw.chromium.launch(**launch_args)
    chrome_pid = browser.process.pid if hasattr(browser, 'process') and browser.process else None

    def _close_all():
        """统一回收: browser.close() + 兜底 SIGKILL Chrome 主进程 + 停 playwright driver。"""
        try:
            browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass
        if chrome_pid:
            try:
                os.kill(chrome_pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                os.kill(chrome_pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    try:
        context = browser.new_context()
        # 见上方 launch_args 注释: 抹掉 webdriver 标记, 降低风控验证码触发概率
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page = context.new_page()
        # blueshirt 等海外站从国内服务器要 16s+ 才返回, 默认 30s 等 'load' 拉
        # 所有资源会超时. 用 'domcontentloaded' (DOM 就绪即返回) + 90s 超时.
        page.goto(base_url + login_path, wait_until="domcontentloaded", timeout=90_000)

        auto_submitted = False
        if username and password:
            auto_submitted = _try_autofill(page, username, password,
                                           open_login_js, selector_hints)
            if auto_submitted:
                print("已自动填写并提交登录表单, 等待登录完成…")
        else:
            print("请在浏览器窗口中完成登录")

        if done_check_js:
            done_check = done_check_js
        elif done_url_contains is None:
            done_check = "() => !window.location.pathname.includes('/login')"
        else:
            done_check = f"() => window.location.href.includes({done_url_contains!r})"

        # 自动提交后无人接手的快速失败线 (预检场景); 人工窗口不受影响
        auto_fail_at = (time.time() + 90) if (auto_submitted and not manual_fallback) else None

        # 自己 poll, 才能同时检测 (done | cancel | 用户关浏览器). wait_for_function 是黑盒.
        deadline = time.time() + 300  # 5 分钟给用户登录
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("登录已取消")
            if page.is_closed():
                raise RuntimeError("浏览器已关闭")
            if time.time() > deadline:
                raise TimeoutError("登录超时")
            if auto_fail_at is not None and time.time() > auto_fail_at:
                raise TimeoutError("自动登录提交后 90s 未完成 (账密可能有误/出现验证码), 快速失败")
            try:
                if page.evaluate(f"({done_check})()"):
                    break
            except Exception as e:
                # 用户关 page / context 时 evaluate 会抛 "Target page, context or browser has been closed".
                # 导航中也偶尔瞬时报错, 看消息区分: 含 closed → fatal, 否则忽略下轮再试.
                if "closed" in str(e).lower() or "target" in str(e).lower():
                    raise RuntimeError("浏览器已关闭")
            time.sleep(0.5)

        page.wait_for_timeout(1500)  # 等 localStorage 写入
        context.storage_state(path=str(state_file))
        mode = "auto" if auto_submitted else "manual"
        print(f"登录态已保存到: {state_file} (mode={mode})")
        return {"mode": mode, "state_file": str(state_file)}
    finally:
        _close_all()


def _kill_browser(pid):
    """强制关闭浏览器进程。"""
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.blueshirtmap.com"
    print(save_session(url))
