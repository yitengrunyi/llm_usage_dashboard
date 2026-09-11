"""Dashboard 共享密码登录 + JWT cookie session.

设计:
- 单密码: 服务器 .env 的 DASHBOARD_PASSWORD, 所有人共享一个密码 (内部小工具够用)
- token: JWT, HS256 签, 7 天 TTL, 写 httponly cookie 防 JS 偷
- 注入方式: HTTP middleware (而不是 router 级 dependency), 因为 main.py 有 13 个散落
  @app.get/post 不在 router 里, 用 middleware 一处管所有
- scheduler 调 run_ingest 不走 HTTP, 自动跳过, 不需要服务账号
- LiteLLM 子模块 (/api/litellm/*) 已有自己的 x-api-key, 这里加白名单不重复拦
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


router = APIRouter(prefix="/api/auth", tags=["auth"])

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
JWT_SECRET = os.environ.get("JWT_SECRET", "")
JWT_ALG = "HS256"
JWT_TTL_HOURS = 24 * 7
COOKIE_NAME = "dashboard_session"

# 不需要登录就能访问的路径 (前缀匹配). 其余全要登录.
# - /api/auth/*    自己, 否则 login 死循环
# - /api/litellm/* LiteLLM 子模块独立鉴权 (x-api-key)
# - /docs, /openapi.json, /redoc  FastAPI 自动文档
# - /  根路径 (静态前端)
PUBLIC_PREFIXES = (
    "/api/auth/",
    "/api/litellm/",
    "/docs",
    "/openapi.json",
    "/redoc",
)


class LoginIn(BaseModel):
    password: str


def _verify_token(token: str) -> None:
    """验证 JWT, 失败抛 HTTPException 401."""
    if not JWT_SECRET:
        raise HTTPException(500, "服务器未配置 JWT_SECRET")
    try:
        jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "登录已过期")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "无效 token")


@router.post("/login")
def login(body: LoginIn, response: Response):
    if not DASHBOARD_PASSWORD:
        raise HTTPException(500, "服务器未配置 DASHBOARD_PASSWORD")
    if not JWT_SECRET:
        raise HTTPException(500, "服务器未配置 JWT_SECRET")
    if body.password != DASHBOARD_PASSWORD:
        raise HTTPException(401, "密码错误")
    exp = datetime.now(timezone.utc) + timedelta(hours=JWT_TTL_HOURS)
    token = jwt.encode({"exp": exp, "sub": "dashboard"}, JWT_SECRET, algorithm=JWT_ALG)
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=JWT_TTL_HOURS * 3600,
        httponly=True,
        samesite="lax",
        secure=False,  # 内网 http 部署; 上 https 后改 True
    )
    return {"ok": True, "exp": exp.isoformat()}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    """前端 router guard 调用, 看 cookie 还在不在."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(401, "未登录")
    _verify_token(token)
    return {"ok": True}


class AuthMiddleware(BaseHTTPMiddleware):
    """全站 cookie 校验. 未登录访问受保护路径返 401 JSON.

    放在路由匹配之前, 一处管所有 @app.get / @app.post / include_router 的路径.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # 白名单前缀 + OPTIONS (CORS preflight) 直接放
        if request.method == "OPTIONS":
            return await call_next(request)
        if any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return await call_next(request)
        # 非 /api/* 路径 (前端静态资源) 也放, nginx 反代时不会走到这里, dev 时直接放
        if not path.startswith("/api/"):
            return await call_next(request)

        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return JSONResponse({"detail": "未登录"}, status_code=401)
        try:
            _verify_token(token)
        except HTTPException as e:
            return JSONResponse({"detail": e.detail}, status_code=e.status_code)
        return await call_next(request)
