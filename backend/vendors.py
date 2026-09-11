"""
供应商管理模块：加载配置、调度客户端、统一数据格式。

凭据从 .env 注入到 vendor dict (按 vendor.id 约定 env key)，vendors.json
只存结构性配置 (id/name/type/base_url/currency 等), 不存任何密钥。

约定 (per vendor.id):
  openai     → OPENAI_API_KEY
  volcengine → VOLCENGINE_ACCESS_KEY / VOLCENGINE_SECRET_KEY
  ucloud     → UCLOUD_PUBLIC_KEY / UCLOUD_SECRET_KEY / UCLOUD_PROJECT_ID
  wangsu     → WANGSU_ACCESS_KEY / WANGSU_SECRET_KEY
  nulls      → NULLS_USERNAME / NULLS_PASSWORD
  road2all   → ROAD2ALL_USERNAME / ROAD2ALL_PASSWORD
  grok       → GROK_MANAGEMENT_KEY / GROK_TEAM_ID (xAI Management Key, ≠ 推理 API key)
(tencent / db / openai_proxy 等仍直接由各模块读 env)
"""
import json
import os
from pathlib import Path

from dotenv import load_dotenv

CONFIG_FILE = Path(__file__).parent / "config" / "vendors.json"

# 模块加载时读 .env (load_dotenv 是幂等的, 重复调用没副作用)
load_dotenv(Path(__file__).parent / ".env")


# 按 vendor.id 约定 env 字段名: {vendor_id: {dict_field: env_var_name}}
_VENDOR_ENV_KEYS: dict[str, dict[str, str]] = {
    "openai":     {"api_key": "OPENAI_API_KEY"},
    "volcengine": {"access_key": "VOLCENGINE_ACCESS_KEY", "secret_key": "VOLCENGINE_SECRET_KEY"},
    "ucloud":     {"public_key": "UCLOUD_PUBLIC_KEY", "secret_key": "UCLOUD_SECRET_KEY", "project_id": "UCLOUD_PROJECT_ID"},
    "wangsu":     {"access_key": "WANGSU_ACCESS_KEY", "secret_key": "WANGSU_SECRET_KEY"},
    "nulls":      {"username": "NULLS_USERNAME", "password": "NULLS_PASSWORD"},
    "road2all":   {"username": "ROAD2ALL_USERNAME", "password": "ROAD2ALL_PASSWORD"},
    "grok":       {"management_key": "GROK_MANAGEMENT_KEY", "team_id": "GROK_TEAM_ID"},
}


def _hydrate(vendor: dict) -> dict:
    """从环境变量按约定注入凭据字段, 缺失字段保留 None (调用方报错时定位用)。"""
    for field, env_key in _VENDOR_ENV_KEYS.get(vendor["id"], {}).items():
        vendor[field] = os.environ.get(env_key) or vendor.get(field)
    return vendor


def load_vendors() -> list[dict]:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [_hydrate(v) for v in data["vendors"] if v.get("enabled", True)]


def get_vendor(vendor_id: str) -> dict | None:
    for v in load_vendors():
        if v["id"] == vendor_id:
            return v
    return None
