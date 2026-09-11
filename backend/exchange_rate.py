"""
汇率服务：获取实时汇率并缓存到本地文件（每日更新一次）。
"""
import json
import time
from pathlib import Path
import requests

CACHE_FILE = Path(__file__).parent / "cache" / "exchange_rate.json"
# 免费汇率 API
RATE_API = "https://open.er-api.com/v6/latest/USD"


def _load_cache() -> dict | None:
    if not CACHE_FILE.exists():
        return None
    with open(CACHE_FILE, "r") as f:
        data = json.load(f)
    # 缓存 24 小时有效
    if time.time() - data.get("timestamp", 0) < 86400:
        return data
    return None


def _save_cache(data: dict):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    data["timestamp"] = time.time()
    with open(CACHE_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_rates() -> dict:
    """获取以 USD 为基准的汇率表。"""
    cached = _load_cache()
    if cached:
        return cached.get("rates", {})

    try:
        resp = requests.get(RATE_API, timeout=10)
        data = resp.json()
        rates = data.get("rates", {})
        _save_cache({"rates": rates})
        return rates
    except Exception:
        # API 失败时使用默认汇率
        return {"CNY": 7.2, "USD": 1.0}


def convert(amount: float, from_currency: str, to_currency: str) -> float:
    """货币换算。"""
    if from_currency == to_currency:
        return amount
    rates = get_rates()
    # 先转成 USD，再转目标
    usd = amount / rates.get(from_currency, 1.0)
    return round(usd * rates.get(to_currency, 1.0), 6)
