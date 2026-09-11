from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.status import HTTP_400_BAD_REQUEST

from litellm.models import TokenUsageEvent
from litellm.repositories import usage_repository
from litellm.schemas import UsageIn, UsageOut


async def report_usage(db: AsyncSession, *, payload: UsageIn) -> UsageOut:
    tokens_total = payload.tokens_total
    if tokens_total is None:
        tokens_total = payload.tokens_input + payload.tokens_output
    if tokens_total < 0 or payload.tokens_input < 0 or payload.tokens_output < 0:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="tokens must be >= 0")

    event = TokenUsageEvent(
        tenant_id=payload.tenant_id,
        request_id=payload.request_id,
        model=payload.model,
        tokens_input=payload.tokens_input,
        tokens_output=payload.tokens_output,
        tokens_total=tokens_total,
        meta_json=payload.meta_json,
        occurred_at=payload.occurred_at or datetime.now(timezone.utc),
    )
    row = await usage_repository.insert_usage_event(db, event=event)
    return UsageOut(id=row.id)
