"""供应商折扣配置 CRUD. 挂在 /api/discounts 前缀下.

前端是一张整表 (vendor → 系 → 模型), 编辑完整体 PUT 覆盖 —— 跟 /api/pricing 一个路子.
粒度接口只留读侧 (catalog / models / resolve), 写侧不拆, 免得前后端两套增量合并逻辑对不齐.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from discounts import (
    DiscountConfigError,
    get_family,
    load_discounts,
    load_families,
    resolve_discount,
    save_discounts,
)
from vendors import load_vendors

log = logging.getLogger(__name__)
router = APIRouter()


# ────────────── 请求体 ──────────────

class ModelIn(BaseModel):
    model: str
    # None = 跟随系折扣
    discount: float | None = None
    # pydantic v2 默认把 model_ 前缀当保留字, 这里字段就叫 model, 关掉保护
    model_config = {"protected_namespaces": ()}


class FamilyIn(BaseModel):
    family_id: str
    name: str | None = None
    discount: float | None = None
    prefixes: list[str] | None = None
    models: list[ModelIn] = Field(default_factory=list)


class VendorIn(BaseModel):
    vendor_id: str
    payment_type: str = "postpaid"
    families: list[FamilyIn] = Field(default_factory=list)


class ConfigIn(BaseModel):
    vendors: list[VendorIn] = Field(default_factory=list)


# ────────────── 辅助 ──────────────

def _vendor_names() -> dict[str, str]:
    """vendor_id → 展示名. vendors.json 里 enabled 的那些。"""
    try:
        return {v["id"]: v.get("name") or v["id"] for v in load_vendors()}
    except Exception as e:  # noqa: BLE001 — 配置读失败不该让折扣页 500
        log.warning("加载 vendors.json 失败: %s", e)
        return {}


def _observed_models(vendor_id: str) -> list[str]:
    """DB 里这家 vendor 真实出现过的模型名.

    目录写不全是常态, 用实际入库过的模型补齐下拉候选.
    DB 连不上时返空 —— 折扣配置页不该被 DB 故障拖死, 下拉还能手输.
    """
    try:
        from sqlalchemy import select

        from ingest.db import SessionLocal
        from ingest.models import VendorModelUsageDaily

        with SessionLocal() as s:
            rows = s.execute(
                select(VendorModelUsageDaily.model)
                .where(VendorModelUsageDaily.vendor_id == vendor_id)
                .distinct()
            ).scalars().all()
        return sorted({r for r in rows if r})
    except Exception as e:  # noqa: BLE001
        log.warning("查 vendor=%s 已用模型失败 (降级为空): %s", vendor_id, e)
        return []


def _enrich(config: dict) -> dict:
    """给配置补上前端要用的展示字段, 不落盘。"""
    names = _vendor_names()
    catalog = {f["family_id"]: f for f in load_families()}
    for v in config["vendors"]:
        v["vendor_name"] = names.get(v["vendor_id"], v["vendor_id"])
        # vendors.json 里删掉过的 vendor, 配置还留着 —— 标出来让人自己决定要不要清
        v["vendor_missing"] = v["vendor_id"] not in names
        for fam in v["families"]:
            fam["is_custom"] = fam["family_id"] not in catalog
    return config


# ────────────── 读 ──────────────

@router.get("")
def get_config() -> dict:
    """整份折扣配置 + 可选 vendor 列表 (前端"新增供应商"下拉的数据源)。"""
    try:
        cfg = load_discounts()
    except DiscountConfigError as e:
        raise HTTPException(500, f"折扣配置文件有问题: {e}")

    names = _vendor_names()
    used = {v["vendor_id"] for v in cfg["vendors"]}
    return {
        **_enrich(cfg),
        # 已配过的不再出现在下拉里, 避免重复添加
        "available_vendors": [
            {"vendor_id": vid, "name": name}
            for vid, name in names.items() if vid not in used
        ],
    }


@router.get("/catalog")
def get_catalog() -> dict:
    """大模型系目录. 前端"新增系"下拉 + 模型下拉的静态数据源。"""
    return {"families": load_families()}


@router.get("/models")
def list_family_models(
    family_id: str = Query(..., description="系 ID; 自定义系传自定义 ID 即可"),
    vendor_id: str | None = Query(None, description="传了就并入该 vendor 在 DB 里出现过的模型"),
) -> dict:
    """某个系下可选的模型: 目录候选 ∪ (该 vendor 已入库且前缀命中本系的模型)。

    自定义系目录里没有, 只会返回 DB 补充部分 —— 此时前端下拉支持直接手输.
    """
    fam = get_family(family_id)
    catalog_models = fam["models"] if fam else []
    prefixes = fam["prefixes"] if fam else []

    observed: list[str] = []
    if vendor_id:
        known_lc = {m.lower() for m in catalog_models}
        for m in _observed_models(vendor_id):
            m_lc = m.lower()
            if m_lc in known_lc:
                continue
            # 目录里没有的, 只收前缀命中本系的; 自定义系 (无前缀) 就全给出来让人挑
            if not prefixes or any(m_lc.startswith(p) for p in prefixes):
                observed.append(m)

    return {
        "family_id": family_id,
        "is_custom": fam is None,
        "catalog_models": catalog_models,
        "observed_models": observed,
        "models": catalog_models + observed,
    }


@router.get("/resolve")
def resolve(
    vendor_id: str = Query(...),
    model: str = Query(...),
) -> dict:
    """查某 vendor 某模型实际吃到的折扣. 用来验证配置写对了没。

    只读查询, 没有接进任何账单计算.
    """
    try:
        cfg = load_discounts()
    except DiscountConfigError as e:
        raise HTTPException(500, f"折扣配置文件有问题: {e}")
    return {"vendor_id": vendor_id, "model": model, **resolve_discount(vendor_id, model, cfg)}


# ────────────── 写 ──────────────

@router.put("")
def put_config(body: ConfigIn) -> dict:
    """整体覆盖保存。校验不过返 400, 原文件不动。"""
    try:
        saved = save_discounts(body.model_dump())
    except DiscountConfigError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        raise HTTPException(500, f"写入折扣配置失败: {e}")

    names = _vendor_names()
    used = {v["vendor_id"] for v in saved["vendors"]}
    return {
        **_enrich(saved),
        "available_vendors": [
            {"vendor_id": vid, "name": name}
            for vid, name in names.items() if vid not in used
        ],
    }
