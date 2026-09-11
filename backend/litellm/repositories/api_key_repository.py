"""LiteLLM_VerificationToken 列表查询 — 给前端 by-api-key 页下拉用.

只读查 LiteLLM 维护的 API key 表, 排序: 有 alias 优先 → 然后 spend 倒序.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from litellm.models import LiteLLMVerificationToken


async def list_active_api_keys(db: AsyncSession) -> list[dict]:
    """列出 VerificationToken 行, 给前端下拉框用.

    返回字段:
      token       : key 全值, 前端选中后透传给 by-api-key 查询
      label       : 显示用. alias 非空 → alias, 否则 "sk-..." + 末 6 位
      alias       : 原 key_alias
      spend       : LiteLLM 统计的累积 spend (USD)
      max_budget  : key 配置的预算上限
      expires     : 过期时间 ISO 字符串 (可能 None / 已过期)
      blocked     : 是否封禁
    """
    stmt = select(
        LiteLLMVerificationToken.token,
        LiteLLMVerificationToken.key_alias,
        LiteLLMVerificationToken.spend,
        LiteLLMVerificationToken.max_budget,
        LiteLLMVerificationToken.expires,
        LiteLLMVerificationToken.blocked,
    ).order_by(
        # NULLS LAST 让 alias 非空的排前 (PG 默认 ASC NULLS LAST 反过来要 desc)
        LiteLLMVerificationToken.key_alias.asc().nullslast(),
        LiteLLMVerificationToken.spend.desc().nullslast(),
    )
    result = await db.execute(stmt)
    rows: list[dict] = []
    for token, alias, spend, max_budget, expires, blocked in result.all():
        if alias:
            label = alias
        elif token:
            label = f"sk-...{token[-6:]}"
        else:
            label = "(无 token)"
        rows.append({
            "token": token,
            "label": label,
            "alias": alias,
            "spend": float(spend or 0),
            "max_budget": float(max_budget) if max_budget is not None else None,
            "expires": expires.isoformat() if expires else None,
            "blocked": bool(blocked),
        })
    return rows
