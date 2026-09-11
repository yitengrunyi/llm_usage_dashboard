"""供应商折扣和付费方式配置: 读写 config/discounts.json + 大模型系目录 config/model_families.json.

三级结构 (vendor → 系 → 具体模型), 折扣按"就近覆盖"生效:

    vendor tencent
      └─ gpt系   discount=0.85          ← 系折扣, 兜底本系所有模型
           ├─ gpt-5      discount=0.8   ← 模型折扣, 覆盖系折扣
           └─ gpt-4o     discount=null  ← 留空 = 跟随系折扣

折扣是**乘数**语义: 1 = 原价, 0.85 = 85 折, 0 = 免费. 不是"折掉的比例".

resolve_discount() 的匹配顺序:
  1. 该 vendor 下配了这个模型 (忽略大小写) 且 discount 非空 → 模型折扣
  2. 命中某个系 (先看该系配过的模型名前缀, 再看目录里的 prefixes) → 系折扣
  3. 都没命中 → 1.0 (原价)

注意: 本模块只负责配置的存取和查询, 没有接进任何账单计算路径.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(__file__).parent / "config"
DISCOUNTS_FILE = CONFIG_DIR / "discounts.json"
FAMILIES_FILE = CONFIG_DIR / "model_families.json"

# 全量覆盖写, 多请求并发 PUT 时序列化, 避免两个写者交错产生半截 JSON
_write_lock = threading.Lock()

# 折扣取值范围. 上限给 100 是为了容忍"填了 85 表示 85 折"这种误用后还能存下来,
# 前端会提示正确语义, 后端不擅自换算.
DISCOUNT_MIN = 0.0
DISCOUNT_MAX = 100.0
PAYMENT_TYPES = {"postpaid", "prepaid"}
DEFAULT_PAYMENT_TYPE = "postpaid"


class DiscountConfigError(ValueError):
    """配置不合法. 由 router 转成 HTTP 400."""


# ────────────── 大模型系目录 (只读) ──────────────

def load_families() -> list[dict]:
    """读 model_families.json. 文件缺失/损坏时返回空目录, 不让页面整体挂掉。"""
    try:
        with open(FAMILIES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    out = []
    for fam in data.get("families", []):
        fid = str(fam.get("family_id") or "").strip()
        if not fid:
            continue
        out.append({
            "family_id": fid,
            "name": str(fam.get("name") or fid),
            "prefixes": [str(p).strip().lower() for p in fam.get("prefixes", []) if str(p).strip()],
            "models": [str(m).strip() for m in fam.get("models", []) if str(m).strip()],
        })
    return out


def get_family(family_id: str) -> dict | None:
    for fam in load_families():
        if fam["family_id"] == family_id:
            return fam
    return None


# ────────────── 折扣配置读写 ──────────────

def _normalize_discount(value: Any, field: str) -> float | None:
    """None / 空串 → None (表示"跟随上一级"). 其余必须是 [0, 100] 内的数。"""
    if value is None or value == "":
        return None
    try:
        d = float(value)
    except (TypeError, ValueError):
        raise DiscountConfigError(f"{field} 必须是数字, 收到 {value!r}")
    if d != d or d in (float("inf"), float("-inf")):  # NaN / inf
        raise DiscountConfigError(f"{field} 不是有效数字")
    if not (DISCOUNT_MIN <= d <= DISCOUNT_MAX):
        raise DiscountConfigError(
            f"{field} 应在 {DISCOUNT_MIN}~{DISCOUNT_MAX} 之间 (1=原价, 0.85=85折), 收到 {d}"
        )
    return round(d, 6)


def _normalize_model(raw: Any, ctx: str) -> dict:
    if isinstance(raw, str):
        raw = {"model": raw}
    if not isinstance(raw, dict):
        raise DiscountConfigError(f"{ctx} 的模型项格式不对")
    name = str(raw.get("model") or "").strip()
    if not name:
        raise DiscountConfigError(f"{ctx} 下存在空的模型名")
    return {
        "model": name,
        # 留空 = 跟随系折扣, 这是有意义的状态, 不要塞默认值
        "discount": _normalize_discount(raw.get("discount"), f"模型 {name} 的折扣"),
    }


def _normalize_payment_type(value: Any, vendor_id: str) -> str:
    payment_type = DEFAULT_PAYMENT_TYPE if value is None or value == "" else str(value).strip().lower()
    if payment_type not in PAYMENT_TYPES:
        raise DiscountConfigError(
            f"供应商 {vendor_id} 的付费方式必须是 postpaid 或 prepaid, 收到 {value!r}"
        )
    return payment_type


def _normalize_family(raw: Any, ctx: str, catalog: dict[str, dict]) -> dict:
    if not isinstance(raw, dict):
        raise DiscountConfigError(f"{ctx} 的系项格式不对")
    fid = str(raw.get("family_id") or "").strip()
    if not fid:
        raise DiscountConfigError(f"{ctx} 下存在没有 family_id 的系")
    # 自定义系没有目录条目, name 就以传入的为准
    name = str(raw.get("name") or "").strip() or catalog.get(fid, {}).get("name") or fid

    models: list[dict] = []
    seen: set[str] = set()
    for m in raw.get("models", []):
        item = _normalize_model(m, f"{ctx} / {name}")
        key = item["model"].lower()
        if key in seen:
            raise DiscountConfigError(f"{ctx} / {name} 下模型 {item['model']} 重复")
        seen.add(key)
        models.append(item)

    return {
        "family_id": fid,
        "name": name,
        "discount": _normalize_discount(raw.get("discount"), f"{name} 的系折扣"),
        # 自定义系可以自带前缀; 没带就回落到目录
        "prefixes": [
            str(p).strip().lower() for p in
            (raw.get("prefixes") or catalog.get(fid, {}).get("prefixes") or [])
            if str(p).strip()
        ],
        "models": models,
    }


def _normalize_config(raw: Any) -> dict:
    """把任意来源 (文件 / 前端 PUT) 的配置规整成标准形状, 顺手做校验。"""
    if not isinstance(raw, dict):
        raise DiscountConfigError("配置根节点必须是对象")
    catalog = {f["family_id"]: f for f in load_families()}

    vendors: list[dict] = []
    seen_vendor: set[str] = set()
    for v in raw.get("vendors", []):
        if not isinstance(v, dict):
            raise DiscountConfigError("vendors 数组里存在非对象项")
        vid = str(v.get("vendor_id") or "").strip()
        if not vid:
            raise DiscountConfigError("存在没有 vendor_id 的供应商")
        if vid in seen_vendor:
            raise DiscountConfigError(f"供应商 {vid} 重复")
        seen_vendor.add(vid)

        families: list[dict] = []
        seen_family: set[str] = set()
        for fam in v.get("families", []):
            item = _normalize_family(fam, f"供应商 {vid}", catalog)
            if item["family_id"] in seen_family:
                raise DiscountConfigError(f"供应商 {vid} 下系 {item['name']} 重复")
            seen_family.add(item["family_id"])
            families.append(item)

        vendors.append({
            "vendor_id": vid,
            "payment_type": _normalize_payment_type(v.get("payment_type"), vid),
            "families": families,
        })

    return {"vendors": vendors}


def load_discounts() -> dict:
    """读 discounts.json. 首次使用 (文件不存在) 返回空配置。"""
    try:
        with open(DISCOUNTS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {"vendors": []}
    except json.JSONDecodeError as e:
        raise DiscountConfigError(f"discounts.json 解析失败: {e}")
    return _normalize_config(raw)


def save_discounts(config: dict) -> dict:
    """校验 → 原子落盘 → 返回规整后的配置。

    先写临时文件再 os.replace, 避免写一半被读到 (docker 里 config/ 是 bind mount,
    backend 自己也会在请求线程里读这个文件).
    """
    normalized = _normalize_config(config)
    payload = json.dumps(normalized, ensure_ascii=False, indent=2) + "\n"

    with _write_lock:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=CONFIG_DIR, prefix=".discounts-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, DISCOUNTS_FILE)
        except BaseException:
            # 落盘失败别留垃圾临时文件
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    return normalized


def get_vendor_payment_type(vendor_id: str, config: dict | None = None) -> str | None:
    """返回已配置供应商的付费方式; 供应商尚未进入折扣配置时返回 None。"""
    cfg = config if config is not None else load_discounts()
    vendor = next((v for v in cfg["vendors"] if v["vendor_id"] == vendor_id), None)
    return vendor["payment_type"] if vendor else None


# ────────────── 折扣查询 ──────────────

def _family_matches(family: dict, model_lc: str) -> bool:
    """模型是否属于这个系: 先看本系已配模型名, 再看前缀。"""
    for m in family.get("models", []):
        name = m["model"].lower()
        if model_lc == name or model_lc.startswith(name):
            return True
    return any(model_lc.startswith(p) for p in family.get("prefixes", []))


def resolve_discount(vendor_id: str, model: str, config: dict | None = None) -> dict:
    """查某 vendor 某模型的实际折扣。

    返回 {discount, source, family_id, family_name, matched_model}:
      source = "model"   命中模型折扣
             = "family"  只命中系, 吃系折扣
             = "default" 没配, 原价 1.0
    """
    cfg = config if config is not None else load_discounts()
    model_lc = (model or "").strip().lower()
    miss = {
        "discount": 1.0, "source": "default",
        "family_id": None, "family_name": None, "matched_model": None,
    }
    if not model_lc:
        return miss

    vendor = next((v for v in cfg["vendors"] if v["vendor_id"] == vendor_id), None)
    if vendor is None:
        return miss

    fallback: dict | None = None
    for fam in vendor["families"]:
        # 精确模型折扣优先. 同名多系的情况下, 谁精确命中谁赢.
        for m in fam["models"]:
            if m["model"].lower() == model_lc and m["discount"] is not None:
                return {
                    "discount": m["discount"], "source": "model",
                    "family_id": fam["family_id"], "family_name": fam["name"],
                    "matched_model": m["model"],
                }
        # 系折扣先记下, 继续找有没有别的系能精确命中模型
        if fallback is None and fam["discount"] is not None and _family_matches(fam, model_lc):
            fallback = {
                "discount": fam["discount"], "source": "family",
                "family_id": fam["family_id"], "family_name": fam["name"],
                "matched_model": None,
            }

    return fallback or miss
