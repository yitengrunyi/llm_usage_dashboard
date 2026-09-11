"""
智谱 BigModel 平台客户端 (bigmodel.cn)。

认证: JWT Token (Authorization header) + Cookie + 额外 headers
接口: /api/finance/expenseBill/expenseBillListByDay (按天明细)
登录: Playwright 微信扫码登录

数据特点:
- 按天 + 按模型 + 按 token 类型（输入/输出/缓存命中/缓存写入）分组
- 有完整的 token 数量和费用
- 有调用次数 (apiUsage)
- 货币: CNY (人民币)

响应字段:
- usageCount: token 数量
- tokenType: "输入"/"输出"/"缓存命中"/"缓存写入"
- settlementAmount: 实际结算金额（扣除资源包后）
- originalAmount: 原价金额
- apiUsage: API 调用次数
- modelCode: 模型代码 (如 glm-4.5-air)
"""
import json
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

CST = timezone(timedelta(hours=8))
STATE_DIR = Path(__file__).parent / "config" / "sessions"


def _get_state_file(base_url: str) -> Path:
    """根据 base_url 获取 session state 文件路径"""
    from urllib.parse import urlparse
    domain = urlparse(base_url).hostname.replace(".", "_")
    return STATE_DIR / f"{domain}.json"


def _load_session(state_file: Path) -> tuple[dict, str, dict]:
    """
    从 storage_state 文件加载登录态。

    Returns:
        (cookies_dict, authorization_token, extra_headers)
    """
    with open(state_file, "r") as f:
        data = json.load(f)

    # 提取 cookies
    cookies = {c["name"]: c["value"] for c in data.get("cookies", [])}

    # 提取 Authorization token (从 cookie 或 localStorage)
    authorization = None
    extra_headers = {}

    # 1. 尝试从 cookie 中获取 token
    if "bigmodel_token_production" in cookies:
        authorization = cookies["bigmodel_token_production"]

    # 2. 尝试从 localStorage 获取其他必需的 headers
    for origin in data.get("origins", []):
        for item in origin.get("localStorage", []):
            # 可能的 localStorage keys
            if item["name"] == "bigmodel-organization":
                extra_headers["bigmodel-organization"] = item["value"]
            elif item["name"] == "bigmodel-project":
                extra_headers["bigmodel-project"] = item["value"]

    if not authorization:
        raise ValueError("无法从 session 文件中获取 Authorization token")

    return cookies, authorization, extra_headers


def fetch_bills_by_day(base_url: str, token: str, month: str,
                       extra_headers: dict = None) -> list:
    """
    拉取指定月份的按天账单明细。

    Args:
        base_url: API 基础地址 (https://bigmodel.cn)
        token: JWT token (完整的 token 字符串)
        month: 月份 YYYY-MM
        extra_headers: 额外的 headers (bigmodel-organization, bigmodel-project 等)

    Returns:
        账单明细列表
    """
    headers = {
        "Authorization": token,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh",
        "Set-Language": "zh",
        "Referer": f"{base_url}/finance-center/bill/expensebill/list",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }

    # 合并额外的 headers
    if extra_headers:
        headers.update(extra_headers)

    page_num = 1
    page_size = 100  # 加大 page size
    all_rows = []

    while True:
        params = {
            "billingMonth": month,
            "billStatus": "",
            "modelProductName": "",
            "paymentType": "",
            "pageNum": page_num,
            "pageSize": page_size,
        }

        r = requests.get(
            f"{base_url}/api/finance/expenseBill/expenseBillListByDay",
            params=params,
            headers=headers,
            timeout=30,
        )

        data = r.json()

        if data.get("code") != 200:
            msg = data.get("msg", "查询失败")
            raise RuntimeError(f"智谱账单接口失败: {msg}")

        rows = data.get("rows") or []
        all_rows.extend(rows)

        total = data.get("total") or 0
        if len(all_rows) >= total or len(rows) < page_size:
            break

        page_num += 1

        if page_num > 100:  # 防御性
            raise RuntimeError(f"智谱翻页超过 100 页 (累计 {len(all_rows)} 行)")

    return all_rows


def aggregate_by_day_and_model(rows: list) -> dict:
    """
    将账单明细聚合成 vendor service shape。

    智谱的数据特点:
    - 同一天同一模型会有多条记录，按 tokenType 区分
    - tokenType: "输入"/"输出"/"缓存命中"/"缓存写入"
    - 需要按 (date, modelCode) 分组，合并不同 tokenType 的数据
    """
    # 按 (date, model) 分组
    grouped = defaultdict(lambda: {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_read_tokens": 0,
        # 缓存写入目前"限时免费"→ 账单从不出现该 tokenType 行. None = 上游未暴露
        # (有 cache_read 就必有写入发生, 写 0 是错的); 开始计费后行会出现, 届时是真数.
        "cache_write_tokens": None,
        "total_tokens": 0,
        "total_count": 0,
        "total_cost": 0.0,
    })

    daily_map = {}

    for row in rows:
        date = row.get("billingDate")  # "2026-07-12"
        model = row.get("modelCode")   # "glm-4.5-air"
        token_type = row.get("tokenType")  # "输入"/"输出"/"缓存命中"/"缓存写入"
        payment_type = row.get("paymentType", "")  # 付费类型

        if not date or not model:
            continue

        key = (date, model)
        m = grouped[key]

        # token 数量
        usage = int(row.get("usageCount") or 0)

        # 根据 tokenType 分配到不同字段
        if token_type == "输入":
            m["prompt_tokens"] += usage
        elif token_type == "输出":
            m["completion_tokens"] += usage
        elif token_type == "缓存命中":
            m["cache_read_tokens"] += usage
        elif token_type == "缓存写入":
            m["cache_write_tokens"] = (m["cache_write_tokens"] or 0) + usage

        m["total_tokens"] += usage

        # API 调用次数
        api_usage = int(row.get("apiUsage") or 0)
        m["total_count"] += api_usage

        # 费用 - 使用 originalAmount（原价）而不是 settlementAmount（可能被资源包抵扣为0）
        # 排除"后付费(减免)"部分：这是平台调账减免的金额，不应计入实际成本
        cost = float(row.get("originalAmount") or 0)
        # cost = float(row.get("settlementAmount") or 0)
        # if payment_type != "后付费(减免)":
        #     m["total_cost"] += cost
            
        #     # 按日期汇总
        #     if date not in daily_map:
        #         daily_map[date] = {"date": date, "cost": 0.0}
        #     daily_map[date]["cost"] += cost
        m["total_cost"] += cost
        # 按日期汇总
        if date not in daily_map:
            daily_map[date] = {"date": date, "cost": 0.0}
        daily_map[date]["cost"] += cost

    # 转换为 models dict
    models = {}
    for (date, model_code), m in grouped.items():
        if model_code not in models:
            models[model_code] = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cache_tokens": 0,  # 兼容字段
                "cache_read_tokens": 0,
                # 与 grouped 同语义: None = 上游未暴露, 所有日期都无"缓存写入"行时保持 None
                "cache_write_tokens": None,
                "total_tokens": 0,
                "total_count": 0,
                "total_cost": 0.0,
            }

        # 累加到模型总量 — 聚合一律 None 视为 0（2026-08 起上游部分行计数字段返回 null，
        # 且 grouped 的 cache_write_tokens 在无"缓存写入"行时为 None）
        models[model_code]["prompt_tokens"] += m["prompt_tokens"] or 0
        models[model_code]["completion_tokens"] += m["completion_tokens"] or 0
        models[model_code]["cache_read_tokens"] += m["cache_read_tokens"] or 0
        # cache_write: 任一日期有真数才算总账（其余日期 None 视为 0）；全 None 保持 None
        if m["cache_write_tokens"] is not None:
            models[model_code]["cache_write_tokens"] = (
                models[model_code]["cache_write_tokens"] or 0
            ) + m["cache_write_tokens"]
        models[model_code]["cache_tokens"] += m["cache_read_tokens"] or 0  # cache_tokens = cache_read
        models[model_code]["total_tokens"] += m["total_tokens"] or 0
        models[model_code]["total_count"] += m["total_count"] or 0
        models[model_code]["total_cost"] += m["total_cost"] or 0.0

    # 四舍五入
    for m in models.values():
        m["total_cost"] = round(m["total_cost"], 6)
    for d in daily_map.values():
        d["cost"] = round(d["cost"], 6)

    total_cost = round(sum(m["total_cost"] or 0.0 for m in models.values()), 6)
    daily = sorted(daily_map.values(), key=lambda x: x["date"])

    return {
        "total_cost": total_cost,
        "models": models,
        "daily": daily,
        "grouped": grouped,  # 保留 (date, model) 分组，给 adapter 用
    }


def fetch_vendor_usage(vendor: dict, start_time: str, end_time: str) -> dict:
    """
    主入口：拉取智谱账单数据。

    Args:
        vendor: vendors.json 中的配置
        start_time: ISO 格式时间字符串 (YYYY-MM-DDTHH:MM:SS+08:00)
        end_time: ISO 格式时间字符串

    Returns:
        Vendor service shape: {total_cost, models, daily, grouped, vendor_id, vendor_name, currency}
    """
    # 解析时间窗口 → 月份
    start_dt = datetime.fromisoformat(start_time)
    end_dt = datetime.fromisoformat(end_time)

    # 收集需要查询的月份列表
    months = set()
    current = start_dt.replace(day=1)
    end_month = end_dt.replace(day=1)

    while current <= end_month:
        months.add(current.strftime("%Y-%m"))
        # 下个月
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)

    # 从 session 文件加载认证信息（必需）
    state_file = _get_state_file(vendor["base_url"])
    if not state_file.exists():
        raise FileNotFoundError(
            f"智谱 BigModel 未登录。请先登录:\n"
            f"  方式 1: 前端页面点击「登录」按钮\n"
            f"  方式 2: curl -X POST http://localhost:8000/api/vendors/bigmodel/login\n"
            f"Session 文件: {state_file}"
        )

    cookies, token, extra_headers = _load_session(state_file)

    # 拉取所有月份的数据
    all_rows = []
    for month in sorted(months):
        rows = fetch_bills_by_day(
            vendor["base_url"],
            token,
            month,
            extra_headers,
        )
        all_rows.extend(rows)

    # 过滤到时间窗口内
    start_date = start_dt.date().isoformat()
    end_date = end_dt.date().isoformat()
    filtered_rows = [
        r for r in all_rows
        if r.get("billingDate") and start_date <= r["billingDate"] <= end_date
    ]

    # 聚合
    result = aggregate_by_day_and_model(filtered_rows)

    # 补充 vendor 信息
    result["vendor_id"] = vendor["id"]
    result["vendor_name"] = vendor.get("name", vendor["id"])
    result["currency"] = vendor.get("currency", "CNY")

    return result
