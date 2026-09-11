"""
用保存的登录态爬取 new-api 平台的使用日志数据。
需先运行 save_session.py 保存登录态。
"""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
STATE_DIR = Path(__file__).parent / "config" / "sessions"


def _get_state_file(base_url: str) -> Path:
    from urllib.parse import urlparse
    domain = urlparse(base_url).hostname.replace(".", "_")
    return STATE_DIR / f"{domain}.json"


def scrape_logs(base_url: str, page_size: int = 100) -> dict:
    """
    用已保存的登录态，通过浏览器 session 调用管理 API 获取日志。
    返回 /api/log/self 的原始响应。
    """
    state_file = _get_state_file(base_url)
    if not state_file.exists():
        raise FileNotFoundError(
            f"未找到登录态文件: {state_file}\n请先运行: python3 save_session.py {base_url}"
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=CHROME_PATH,
            headless=True,
        )
        context = browser.new_context(storage_state=str(state_file))
        page = context.new_page()

        # 先访问首页让 cookie 生效
        page.goto(base_url, timeout=15000)
        page.wait_for_timeout(2000)

        # 检查是否仍然登录
        if "/login" in page.url:
            browser.close()
            raise PermissionError(
                "登录态已过期，请重新运行: python3 save_session.py"
            )

        # 通过浏览器内 fetch 调用管理 API（自动带 session cookie）
        all_logs = []
        current_page = 0

        while True:
            result = page.evaluate(f"""
                async () => {{
                    const r = await fetch('/api/log/self?p={current_page}&page_size={page_size}');
                    return await r.json();
                }}
            """)

            if not result.get("success"):
                break

            data = result.get("data", [])
            if not data:
                break

            all_logs.extend(data)
            current_page += 1

            # 安全限制：最多取 50 页
            if current_page >= 50:
                break

        # 也获取用户信息
        user_info = page.evaluate("""
            async () => {
                const r = await fetch('/api/user/self');
                return await r.json();
            }
        """)

        browser.close()

    return {
        "logs": all_logs,
        "user": user_info.get("data", {}),
        "total_entries": len(all_logs),
    }


if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.blueshirtmap.com"

    print(f"正在爬取 {url} 的使用日志...")
    try:
        result = scrape_logs(url)
        print(f"共获取 {result['total_entries']} 条日志")
        print(f"用户信息: {json.dumps(result['user'], indent=2, ensure_ascii=False)}")
        if result["logs"]:
            print("\n前 3 条日志:")
            for log in result["logs"][:3]:
                print(json.dumps(log, indent=2, ensure_ascii=False))
    except FileNotFoundError as e:
        print(f"错误: {e}")
    except PermissionError as e:
        print(f"错误: {e}")
