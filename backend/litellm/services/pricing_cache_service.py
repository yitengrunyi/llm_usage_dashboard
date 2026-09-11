"""
定价查找服务：从 JSON 文件读取模型定价，支持按 (模型名, 供应商) 查找。

数据流：
  CRUD → 写 DB → export_to_file 导出 JSON → refresh_price_cache 重新加载 JSON

查找优先级：
  1. (model_name, provider)  — 供应商专属定价
  2. (model_name, None)      — 模型默认定价
  3. alias 同理
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from litellm.pricing.normalizer import KNOWN_PROVIDERS, extract_provider, normalized_model_name
from litellm.services.upstream_pricing import load_upstream

logger = logging.getLogger(__name__)

PRICES_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "model_prices.json"

# 内存中的价格映射
_price_map: dict[tuple[str, str | None], dict] = {}
_alias_map: dict[tuple[str, str | None], dict] = {}
# model_name → 是否为阶梯定价（advanced/tiered）
_tiered_model_names: set[str] = set()
_simple_model_names: set[str] = set()
# 反向索引：规范化模型名 → 原始 model_group 列表
_normalized_to_groups: dict[str, list[str]] = {}
# 反向索引（含供应商）：(规范化模型名, 供应商) → 原始 model_group 列表
_normalized_provider_to_groups: dict[tuple[str, str], list[str]] = {}


def _is_tiered_config(cfg: dict) -> bool:
    """判断定价配置是否为阶梯类型（需要逐行算价）。"""
    return cfg.get("type") in ("advanced", "tiered")


def _model_group_variants(model_group: str) -> list[str]:
    """
    为一个 model_group 生成所有可能的数据库变体，覆盖：
      - 原始值
      - 点号 ↔ 连字符：claude-sonnet-4-5 ↔ claude-sonnet-4.5
      - 加/去供应商前缀：gpt-4.1 → openai-gpt-4.1 / blueshirt-gpt-4.1 等
    """
    base_set: set[str] = {model_group}

    # 点号 → 连字符
    dash_ver = re.sub(r'(\d+)\.(\d+)', r'\1-\2', model_group)
    base_set.add(dash_ver)
    # 连字符 → 点号
    dot_ver = re.sub(r'(\d+)-(\d+)(?=\D|$)', r'\1.\2', model_group)
    base_set.add(dot_ver)

    # 去掉供应商前缀得到裸模型名
    bare = model_group
    for pfx in KNOWN_PROVIDERS:
        prefix = f"{pfx}-"
        if bare.lower().startswith(prefix):
            bare = bare[len(prefix):]
            break
    bare_dash = re.sub(r'(\d+)\.(\d+)', r'\1-\2', bare)
    bare_dot = re.sub(r'(\d+)-(\d+)(?=\D|$)', r'\1.\2', bare)
    base_set.update([bare, bare_dash, bare_dot])

    # 为每个裸名添加供应商前缀变体
    expanded: set[str] = set(base_set)
    for b in [bare, bare_dash, bare_dot]:
        for pfx in KNOWN_PROVIDERS:
            expanded.add(f"{pfx}-{b}")
    expanded.discard("")
    return sorted(expanded)


def _load_from_json() -> tuple[
    dict[tuple[str, str | None], dict],
    dict[tuple[str, str | None], dict],
    set[str],
    set[str],
    dict[str, list[str]],
    dict[tuple[str, str], list[str]],
]:
    """从 上游 JSON (优先) + 本地 JSON (fallback 填空) 加载定价数据。
       上游有的 (model, provider) key, 本地条目不会覆盖, 只有上游没有的才用本地。"""
    # ── 1. 先吃上游 (authoritative) ──────────────────────────────────
    price_map: dict[tuple[str, str | None], dict] = dict(load_upstream())
    alias_map: dict[tuple[str, str | None], dict] = {}
    tiered: set[str] = set()
    simple: set[str] = set()
    norm_to_groups: dict[str, list[str]] = {}
    norm_prov_to_groups: dict[tuple[str, str], list[str]] = {}

    for (model_key, _prov), cfg in price_map.items():
        if _is_tiered_config(cfg):
            tiered.add(model_key)
        else:
            simple.add(model_key)

    # ── 2. 再吃本地 (上游没有的 key 才补; 同 key 直接跳过) ────────────
    if not PRICES_FILE.exists():
        logger.info("Local prices file not found (gap-fill only): %s", PRICES_FILE)
        return price_map, alias_map, tiered, simple, norm_to_groups, norm_prov_to_groups

    try:
        data = json.loads(PRICES_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Failed to read local prices file: %s", e)
        return price_map, alias_map, tiered, simple, norm_to_groups, norm_prov_to_groups

    skipped = 0
    added = 0
    for item in data:
        if not item.get("is_active", True):
            continue
        cfg = item.get("pricing_config")
        if not cfg:
            continue

        model_key = item["model_name"].lower()
        provider_key = item["provider"].lower() if item.get("provider") else None

        # 上游已有则跳过, 上游优先
        if (model_key, provider_key) in price_map:
            skipped += 1
            continue

        price_map[(model_key, provider_key)] = cfg
        added += 1

        # 分类:阶梯 vs 简单
        if _is_tiered_config(cfg):
            tiered.add(model_key)
        else:
            simple.add(model_key)

        aliases = item.get("aliases") or []
        for a in aliases:
            if a and (a.lower(), provider_key) not in alias_map:
                alias_map[(a.lower(), provider_key)] = cfg

        # 构建反向索引:规范化模型名 → 原始 model_group(含变体)
        model_group_val = item.get("model_group") or item["model_name"]
        norm_name = normalized_model_name(model_group_val).lower()
        if norm_name:
            variants = _model_group_variants(model_group_val)
            for v in variants:
                if v not in norm_to_groups.get(norm_name, []):
                    norm_to_groups.setdefault(norm_name, []).append(v)
                prov = extract_provider(v, v, None)
                key = (norm_name, prov)
                if v not in norm_prov_to_groups.get(key, []):
                    norm_prov_to_groups.setdefault(key, []).append(v)

    logger.info("local prices: added %d (gap-fill), skipped %d (upstream wins)", added, skipped)
    return price_map, alias_map, tiered, simple, norm_to_groups, norm_prov_to_groups


def _ensure_loaded() -> None:
    """确保价格数据已加载(首次调用时从上游 + 本地 JSON 加载)。"""
    global _price_map, _alias_map, _tiered_model_names, _simple_model_names, _normalized_to_groups, _normalized_provider_to_groups
    if not _price_map:
        _price_map, _alias_map, _tiered_model_names, _simple_model_names, _normalized_to_groups, _normalized_provider_to_groups = _load_from_json()
        logger.info("Loaded %d price entries (%d tiered, %d simple, %d norm-groups) from upstream+local",
                     len(_price_map), len(_tiered_model_names), len(_simple_model_names), len(_normalized_to_groups))


async def refresh_price_cache() -> None:
    """CRUD 后调用，重新从 JSON 加载价格数据。"""
    global _price_map, _alias_map, _tiered_model_names, _simple_model_names, _normalized_to_groups, _normalized_provider_to_groups
    _price_map, _alias_map, _tiered_model_names, _simple_model_names, _normalized_to_groups, _normalized_provider_to_groups = _load_from_json()
    logger.info("Refreshed %d price entries (%d tiered, %d simple, %d norm-groups) from JSON",
                 len(_price_map), len(_tiered_model_names), len(_simple_model_names), len(_normalized_to_groups))


def _find_in_cache(model_name: str, provider: str | None = None) -> dict | None:
    """
    查找定价（同步）。

    查找优先级：
      1. (model_name, provider) — 供应商专属
      2. (model_name, None)    — 默认定价
      3. alias 同理
    """
    _ensure_loaded()

    key = model_name.lower()
    prov = provider.lower() if provider else None

    # 1. 供应商专属定价
    if prov:
        cfg = _price_map.get((key, prov))
        if cfg:
            return cfg
        cfg = _alias_map.get((key, prov))
        if cfg:
            return cfg

    # 2. 默认定价（provider=None）
    cfg = _price_map.get((key, None))
    if cfg:
        return cfg
    cfg = _alias_map.get((key, None))
    if cfg:
        return cfg

    return None


def get_all_models_and_providers() -> tuple[list[str], list[str]]:
    """
    从定价缓存中提取所有已配置的模型名和供应商列表。
    直接读内存，不查 DB / SpendLogs。
    """
    _ensure_loaded()
    models: set[str] = set()
    providers: set[str] = set()
    for (model, provider) in _price_map:
        if model:
            models.add(model)
        if provider:
            providers.add(provider)
    return sorted(models), sorted(providers)


async def get_pricing_config(model_name: str, provider: str | None = None) -> dict | None:
    """按模型名 + 供应商查找定价配置。"""
    return _find_in_cache(model_name, provider)


def get_tiered_model_names() -> set[str]:
    """返回所有阶梯定价的 model_name 集合（小写）。"""
    _ensure_loaded()
    return _tiered_model_names


def get_simple_model_names() -> set[str]:
    """返回所有简单定价的 model_name 集合（小写）。"""
    _ensure_loaded()
    return _simple_model_names


def get_model_groups_for(norm_name: str, provider: str | None = None) -> list[str]:
    """
    反向索引查询：规范化模型名 → 所有原始 model_group。
    如果指定 provider，只返回该供应商对应的 model_group。
    """
    _ensure_loaded()
    key = norm_name.lower()
    if provider:
        return list(_normalized_provider_to_groups.get((key, provider), []))
    return list(_normalized_to_groups.get(key, []))
