from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from litellm.db import get_db
from litellm.schemas import UsageIn, UsageOut
from litellm.security import RequireApiKey
from litellm.services import usage_service

router = APIRouter(dependencies=[RequireApiKey])


@router.post("/usage", response_model=UsageOut)
async def report_usage(payload: UsageIn, db: AsyncSession = Depends(get_db)) -> UsageOut:
    return await usage_service.report_usage(db, payload=payload)
