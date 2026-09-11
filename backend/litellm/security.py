from __future__ import annotations

from fastapi import Depends, Header, HTTPException
from starlette.status import HTTP_401_UNAUTHORIZED

from litellm.settings import settings


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """
    简单鉴权：Header `x-api-key: <API_KEY>`
    你后续可以替换成 JWT/OAuth2。
    """

    if not settings.api_key:
        # fail-closed: 未配置 API_KEY 时拒绝全部请求, 不回落到任何公开默认值
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="API_KEY not configured; set it in backend/.env (and matching VITE_LITELLM_API_KEY at frontend build time)",
        )
    if not x_api_key or x_api_key != settings.api_key:
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="Unauthorized")


RequireApiKey = Depends(require_api_key)

