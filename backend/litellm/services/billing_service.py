from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.status import HTTP_400_BAD_REQUEST

from litellm.repositories import usage_repository
from litellm.schemas import BillingSummaryOut
from litellm.settings import settings


async def billing_summary(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    start: datetime,
    end: datetime,
) -> BillingSummaryOut:
    if end <= start:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="end must be > start")

    tokens_total = await usage_repository.sum_tokens_total(db, tenant_id=tenant_id, start=start, end=end)
    amount = (tokens_total / 1000.0) * float(settings.price_per_1k_tokens)
    return BillingSummaryOut(
        tenant_id=tenant_id,
        period_start=start,
        period_end=end,
        tokens_total=tokens_total,
        price_per_1k_tokens=float(settings.price_per_1k_tokens),
        amount=amount,
    )
