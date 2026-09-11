import json
import logging
from pathlib import Path

PRICING_FILE = Path(__file__).parent / "config" / "pricing.json"

logger = logging.getLogger(__name__)

DIMENSION_SUFFIXES = ("_input", "_output", "_cacheinput")


def load_pricing() -> dict:
    with open(PRICING_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_pricing(pricing: dict):
    with open(PRICING_FILE, "w", encoding="utf-8") as f:
        json.dump(pricing, f, indent=2, ensure_ascii=False)


def parse_specification(spec: str) -> tuple[str, str]:
    """
    解析 Specification 编码（生文类型）。
    "Tog5.4_input" → ("Tog5.4", "input")
    "Tgg25flash_cacheinput" → ("Tgg25flash", "cacheinput")
    """
    for suffix in DIMENSION_SUFFIXES:
        if spec.endswith(suffix):
            return spec[: -len(suffix)], suffix[1:]
    return spec, "unknown"


def _calc_text(usage_data: list, type_pricing: dict) -> dict:
    """生文计费：按 token 计价。"""
    models = {}
    daily_map = {}
    warned = set()

    for item in usage_data:
        spec = item["Specification"]
        model_key, dimension = parse_specification(spec)

        if model_key not in type_pricing:
            # 未定价 SKU 计不了费(成本按0) — 必须告警, 2026-07 曾因键名抄错静默丢了 8/10 模型
            if model_key not in warned:
                warned.add(model_key)
                logger.warning("tencent 生文 SKU 未定价, 成本按 0: %s (补 config/pricing.json)", model_key)
            continue

        model_pricing = type_pricing[model_key]
        unit_price = model_pricing.get(dimension, 0)
        display_name = model_pricing.get("display_name", model_key)

        if display_name not in models:
            models[display_name] = {
                "input": {"tokens": 0, "cost": 0},
                "cacheinput": {"tokens": 0, "cost": 0},
                "output": {"tokens": 0, "cost": 0},
                "total_cost": 0,
                "total_count": 0,
            }

        for dp in item.get("DataSet", []):
            usage = dp.get("Usage", 0)
            count = dp.get("Count", 0)
            time_str = dp.get("Time", "")
            if usage == 0 and count == 0:
                continue

            date_str = time_str[:10] if time_str else "unknown"
            cost = usage / 1_000_000 * unit_price

            if dimension in ("input", "output", "cacheinput"):
                models[display_name][dimension]["tokens"] += usage
                models[display_name][dimension]["cost"] += cost
                models[display_name]["total_cost"] += cost
                if dimension == "input":
                    models[display_name]["total_count"] += count

            if date_str not in daily_map:
                daily_map[date_str] = {"date": date_str, "cost": 0, "models": {}}
            daily_map[date_str]["cost"] += cost
            if display_name not in daily_map[date_str]["models"]:
                daily_map[date_str]["models"][display_name] = {"cost": 0, "count": 0, "tokens": 0}
            daily_map[date_str]["models"][display_name]["cost"] += cost
            if dimension == "input":
                daily_map[date_str]["models"][display_name]["count"] += count
            daily_map[date_str]["models"][display_name]["tokens"] += usage

    # 保留精度
    for m in models.values():
        m["total_cost"] = round(m["total_cost"], 4)
        for dim in ("input", "output", "cacheinput"):
            m[dim]["cost"] = round(m[dim]["cost"], 4)

    return models, daily_map


def _calc_image(usage_data: list, type_pricing: dict) -> dict:
    """生图计费：按张数计价，Usage = 张数。"""
    models = {}
    daily_map = {}
    warned = set()

    for item in usage_data:
        spec = item["Specification"]

        if spec not in type_pricing:
            if spec not in warned:
                warned.add(spec)
                logger.warning("tencent 生图 SKU 未定价, 成本按 0: %s (补 config/pricing.json)", spec)
            continue

        model_pricing = type_pricing[spec]
        unit_price = model_pricing.get("price", 0)
        display_name = model_pricing.get("display_name", spec)

        if display_name not in models:
            models[display_name] = {
                "count": 0,
                "cost": 0,
                "unit_price": unit_price,
                "total_cost": 0,
                "total_count": 0,
            }

        for dp in item.get("DataSet", []):
            usage = dp.get("Usage", 0)  # 张数
            count = dp.get("Count", 0)
            time_str = dp.get("Time", "")
            if usage == 0 and count == 0:
                continue

            date_str = time_str[:10] if time_str else "unknown"
            cost = usage * unit_price  # 张数 × 单价

            models[display_name]["count"] += usage
            models[display_name]["cost"] += cost
            models[display_name]["total_cost"] += cost
            models[display_name]["total_count"] += count

            if date_str not in daily_map:
                daily_map[date_str] = {"date": date_str, "cost": 0, "models": {}}
            daily_map[date_str]["cost"] += cost
            if display_name not in daily_map[date_str]["models"]:
                daily_map[date_str]["models"][display_name] = {"cost": 0, "count": 0, "images": 0}
            daily_map[date_str]["models"][display_name]["cost"] += cost
            daily_map[date_str]["models"][display_name]["count"] += count
            daily_map[date_str]["models"][display_name]["images"] += usage

    for m in models.values():
        m["total_cost"] = round(m["total_cost"], 4)
        m["cost"] = round(m["cost"], 4)

    return models, daily_map


def calculate_billing(usage_data: list, aigc_type: str = "text") -> dict:
    pricing = load_pricing()
    type_pricing = pricing.get(aigc_type, {})

    if aigc_type == "image":
        models, daily_map = _calc_image(usage_data, type_pricing)
    else:
        models, daily_map = _calc_text(usage_data, type_pricing)

    total_cost = sum(m["total_cost"] for m in models.values())
    daily = sorted(daily_map.values(), key=lambda x: x["date"])
    for d in daily:
        d["cost"] = round(d["cost"], 4)

    return {
        "total_cost": round(total_cost, 4),
        "models": models,
        "daily": daily,
        "billing_type": aigc_type,
    }


def tc_to_unified(text_billing: dict, image_billing: dict) -> dict:
    """
    将腾讯云的 text + image 计费结果转换为统一格式。
    统一格式的 models: { name: { prompt_tokens, completion_tokens, total_tokens, total_count, total_cost } }
    """
    models = {}
    daily_map = {}

    # 生文模型
    for name, m in text_billing.get("models", {}).items():
        models[name] = {
            "prompt_tokens": m["input"]["tokens"],
            "completion_tokens": m["output"]["tokens"],
            "cache_tokens": m["cacheinput"]["tokens"],
            "total_tokens": m["input"]["tokens"] + m["output"]["tokens"] + m["cacheinput"]["tokens"],
            "total_count": m["total_count"],
            "total_cost": m["total_cost"],
        }

    # 生图模型
    for name, m in image_billing.get("models", {}).items():
        models[name] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cache_tokens": 0,
            "total_tokens": 0,
            "total_count": m["total_count"],
            "total_cost": m["total_cost"],
            "image_count": m.get("count", 0),
        }

    # 合并 daily
    for billing in [text_billing, image_billing]:
        for d in billing.get("daily", []):
            date = d["date"]
            if date in daily_map:
                daily_map[date]["cost"] += d["cost"]
            else:
                daily_map[date] = {"date": date, "cost": d["cost"]}

    total_cost = round(sum(m["total_cost"] for m in models.values()), 4)
    daily = sorted(daily_map.values(), key=lambda x: x["date"])

    return {
        "vendor_id": "tencent",
        "vendor_name": "tencent",
        "currency": "CNY",
        "total_cost": total_cost,
        "models": models,
        "daily": daily,
        "text_models": text_billing.get("models", {}),
        "image_models": image_billing.get("models", {}),
    }
