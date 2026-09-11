from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from litellm.db import get_db
from litellm.schemas import (
    ApiKeyOption,
    SpendByApiKeyIn,
    SpendByModelProviderIn,
    SpendByProviderSummaryIn,
    SpendOptionsOut,
    SpendQueryIn,
    SpendSingleOut,
    SpendSummaryOut,
    UnpricedModelsOut,
)
from litellm.security import RequireApiKey
from litellm.services import spend_analysis_service
from litellm.repositories.api_key_repository import list_active_api_keys

router = APIRouter(prefix="/analytics", tags=["analytics"], dependencies=[RequireApiKey])


@router.post("/spend/by-model-provider", response_model=SpendSingleOut)
async def get_spend_by_model_provider(
    body: SpendByModelProviderIn,
    db: AsyncSession = Depends(get_db),
) -> SpendSingleOut:
    """
    按「供应商 + 模型」查询：传入时间范围、model、provider，只返回该组合的总 cost 与输入/输出/缓存 token。
    """
    return await spend_analysis_service.get_spend_by_model_provider(
        db,
        start=body.start,
        end=body.end,
        model=body.model,
        provider=body.provider,
    )


@router.post("/spend/by-provider", response_model=SpendSummaryOut)
async def get_spend_by_provider(
    body: SpendByProviderSummaryIn,
    db: AsyncSession = Depends(get_db),
) -> SpendSummaryOut:
    """
    按供应商汇总：传入时间范围，返回各供应商的 token 与花费汇总。
    """
    query = SpendQueryIn(
        start=body.start,
        end=body.end,
        user=body.user,
        team_id=body.team_id,
        group_by=["provider"],
    )
    return await spend_analysis_service.get_spend_summary(db, query=query)


@router.post("/spend/by-api-key", response_model=SpendSingleOut)
async def get_spend_by_api_key(
    body: SpendByApiKeyIn,
    db: AsyncSession = Depends(get_db),
) -> SpendSingleOut:
    """
    按 api_key 查询：传入时间范围、api_key，只返回该 key 的总 cost 与输入/输出/缓存 token。
    """
    return await spend_analysis_service.get_spend_by_api_key(
        db,
        start=body.start,
        end=body.end,
        api_key=body.api_key,
    )


@router.get("/options", response_model=SpendOptionsOut)
async def get_spend_options(
    db: AsyncSession = Depends(get_db),
) -> SpendOptionsOut:
    """返回可选的模型和供应商列表，供前端下拉框使用。"""
    return await spend_analysis_service.get_options(db)


@router.get("/api-keys", response_model=list[ApiKeyOption])
async def get_api_key_options(
    db: AsyncSession = Depends(get_db),
) -> list[ApiKeyOption]:
    """列出 LiteLLM_VerificationToken 全部 — 给前端 by-api-key 下拉用.

    排序: 有 alias 优先 → 然后 spend 倒序. label 字段做了掩码 (alias 或末 6 位).
    """
    rows = await list_active_api_keys(db)
    return [ApiKeyOption(**r) for r in rows]


@router.post("/spend", response_model=SpendSummaryOut)
async def get_spend_summary(
    query: SpendQueryIn,
    db: AsyncSession = Depends(get_db),
) -> SpendSummaryOut:
    """
    通用查询：可选过滤 model/provider/user/team_id，按 group_by 分组返回多条汇总。
    若只需「单个供应商+模型」或「单个 api_key」的汇总，请用 /spend/by-model-provider 或 /spend/by-api-key。
    """
    return await spend_analysis_service.get_spend_summary(db, query=query)


@router.post("/unpriced-models", response_model=UnpricedModelsOut)
async def get_unpriced_models(
    body: SpendByProviderSummaryIn,
    db: AsyncSession = Depends(get_db),
) -> UnpricedModelsOut:
    """
    查询时间范围内有请求但无定价的模型+供应商组合，按请求数降序排列。
    用于发现需要补充定价的模型。
    """
    return await spend_analysis_service.get_unpriced_models(
        db, start=body.start, end=body.end,
    )
