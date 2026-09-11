"""网宿模型清单 CRUD + 启用/停用。挂在 /api/wangsu 前缀下。

数据形态: list[{code: str, enabled: bool}]
返回排序: enabled 在上, 内部按 code 字典序。
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from wangsu_client import load_models, save_models

router = APIRouter()


class ModelIn(BaseModel):
    code: str
    enabled: bool = True


class ToggleIn(BaseModel):
    enabled: bool


class ModelsBulk(BaseModel):
    models: list[ModelIn]


@router.get("/models")
def list_models() -> dict:
    models = load_models()
    enabled_count = sum(1 for m in models if m.get("enabled"))
    return {"models": models, "count": len(models), "enabled_count": enabled_count}


@router.post("/models")
def add_model(body: ModelIn) -> dict:
    code = body.code.strip()
    if not code:
        raise HTTPException(400, "code 不能为空")
    models = load_models()
    if any(m["code"] == code for m in models):
        raise HTTPException(409, f"模型 {code} 已存在")
    saved = save_models(models + [{"code": code, "enabled": body.enabled}])
    enabled_count = sum(1 for m in saved if m.get("enabled"))
    return {"models": saved, "count": len(saved), "enabled_count": enabled_count}


@router.patch("/models/{code:path}")
def toggle_model(code: str, body: ToggleIn) -> dict:
    models = load_models()
    found = False
    for m in models:
        if m["code"] == code:
            m["enabled"] = body.enabled
            found = True
            break
    if not found:
        raise HTTPException(404, f"模型 {code} 不存在")
    saved = save_models(models)
    enabled_count = sum(1 for m in saved if m.get("enabled"))
    return {"models": saved, "count": len(saved), "enabled_count": enabled_count}


@router.delete("/models/{code:path}")
def delete_model(code: str) -> dict:
    models = load_models()
    if not any(m["code"] == code for m in models):
        raise HTTPException(404, f"模型 {code} 不存在")
    saved = save_models([m for m in models if m["code"] != code])
    enabled_count = sum(1 for m in saved if m.get("enabled"))
    return {"models": saved, "count": len(saved), "enabled_count": enabled_count}


@router.put("/models")
def replace_models(body: ModelsBulk) -> dict:
    saved = save_models([m.model_dump() for m in body.models])
    enabled_count = sum(1 for m in saved if m.get("enabled"))
    return {"models": saved, "count": len(saved), "enabled_count": enabled_count}
