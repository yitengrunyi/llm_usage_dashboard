import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Query, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware

# 全局日志配置：INFO 级别输出到 stdout，与 uvicorn 日志格式保持一致
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)

from tc_api import fetch_usage
from billing import calculate_billing, load_pricing, save_pricing, tc_to_unified
from vendors import load_vendors, get_vendor
from newapi_client import fetch_vendor_usage
from newapi_direct_client import fetch_vendor_usage as fetch_direct_vendor_usage
from wangsu_client import fetch_vendor_usage as fetch_wangsu_vendor_usage
from apevon_client import fetch_vendor_usage as fetch_apevon_vendor_usage
from ucloud_client import fetch_vendor_usage as fetch_ucloud_vendor_usage
from road2all_client import fetch_vendor_usage as fetch_road2all_vendor_usage
from kimi_client import fetch_vendor_usage as fetch_kimi_vendor_usage
from volcengine_client import fetch_vendor_usage as fetch_volcengine_vendor_usage
from openai_client import fetch_vendor_usage as fetch_openai_vendor_usage
from bigmodel_client import fetch_vendor_usage as fetch_bigmodel_vendor_usage
from grok_client import fetch_vendor_usage as fetch_grok_vendor_usage
from exchange_rate import convert, get_rates

# LiteLLM 模块路由
from litellm.routers.analytics import router as litellm_analytics_router
from litellm.routers.model_prices import router as litellm_prices_router
from litellm.routers.health import router as litellm_health_router
from litellm.routers.billing import router as litellm_billing_router
from litellm.routers.tenants import router as litellm_tenants_router
from litellm.routers.usage import router as litellm_usage_router

# 网宿模型清单 CRUD
from wangsu_router import router as wangsu_router

# 供应商折扣配置 CRUD
from discount_router import router as discount_router
from discounts import DiscountConfigError, get_vendor_payment_type

# 积分模块
from points_router_v3 import router as points_router

app = FastAPI(title="MUD")

# auth: 共享密码 + JWT cookie session — middleware 一处拦所有 /api/* 路由
# 白名单 (PUBLIC_PREFIXES) 含 /api/auth/* + /api/litellm/* (LiteLLM 自己 x-api-key 独立鉴权).
# scheduler 走 Python 直调不走 HTTP, 自动跳过 middleware, 不影响 cron.
from auth import router as auth_router, AuthMiddleware

@app.on_event("startup")
async def startup_litellm():
    """启动时初始化 LiteLLM 模块的价格缓存。"""
    try:
        from litellm.db import Base, engine
        from litellm import models  # noqa: F401  ← register tables on metadata
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        from litellm.services.price_persistence import seed_from_file_or_builtin
        await seed_from_file_or_builtin()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"LiteLLM price cache init failed: {e}")

    # ingest 定时调度 (每天 1:00 vendor 快路径 / 1:30 blueshirt 慢路径补拆分字段)
    try:
        from ingest import scheduler as ingest_scheduler
        ingest_scheduler.start()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"ingest scheduler start failed: {e}")


@app.on_event("shutdown")
async def shutdown_ingest_scheduler():
    try:
        from ingest import scheduler as ingest_scheduler
        ingest_scheduler.shutdown()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"ingest scheduler shutdown failed: {e}")


app.add_middleware(
    CORSMiddleware,
    # cookie 鉴权要求 allow_credentials=True, 此时浏览器规范不允许 allow_origins=["*"],
    # 所以从 .env 读显式列表. 默认开发环境放本地 vite + nginx
    allow_origins=os.environ.get(
        "ALLOWED_ORIGINS",
        "http://localhost:3080,http://localhost:5173"
    ).split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# auth middleware 在 CORS 之后挂, 这样 OPTIONS 预检请求先被 CORS 处理放行,
# 再到 auth (auth 内部对 OPTIONS 也直接 pass).
app.add_middleware(AuthMiddleware)

# /api/auth/login /logout /me — 不在 middleware 拦截白名单内 (POST 自己路径)
app.include_router(auth_router)

# ─── 挂载 LiteLLM 模块路由（独立前缀 /api/litellm） ───
app.include_router(litellm_analytics_router, prefix="/api/litellm")
app.include_router(litellm_prices_router, prefix="/api/litellm")
app.include_router(litellm_health_router, prefix="/api/litellm")
app.include_router(litellm_billing_router, prefix="/api/litellm")
app.include_router(litellm_tenants_router, prefix="/api/litellm")
app.include_router(litellm_usage_router, prefix="/api/litellm")

# ─── 挂载网宿模型清单 CRUD ───
app.include_router(wangsu_router, prefix="/api/wangsu")

# ─── 挂载供应商折扣配置 CRUD ───
app.include_router(discount_router, prefix="/api/discounts")

# ─── 账单对账路由 ───
from billing_reconciliation import reconcile_vendor_billing

@app.get("/api/vendors/{vendor_id}/billing-reconciliation")
def get_billing_reconciliation(
    vendor_id: str,
    start: str = Query(..., description="开始日期 YYYY-MM-DD"),
    end: str = Query(..., description="结束日期 YYYY-MM-DD"),
    api_key: str | None = Query(None, description="可选, 按 API key 过滤"),
):
    """供应商账单对账：计算应付/实付并与实际费用对比。"""
    try:
        return reconcile_vendor_billing(vendor_id, start, end, api_key=api_key)
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception(f"账单对账失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ─── 挂载 ingest (PR2) — 后台入库 + 手动重试 ───
from ingest.router import router as ingest_router
from ingest.export import router as export_router
from ingest.analysis import router as analysis_router
from ingest.agent.router import router as agent_router
from ingest.query import (
    split_window,
    query_vendor_usage,
    query_overview_vendors,
    merge_results,
    get_freshness,
    get_missing_days_in_window,
    get_recently_failed_vendors,
    get_vendor_api_keys,
)
app.include_router(ingest_router)
app.include_router(export_router)
app.include_router(analysis_router)
app.include_router(agent_router)
app.include_router(points_router)

CST = timezone(timedelta(hours=8))


def _default_dates() -> tuple[str, str]:
    now = datetime.now(CST)
    end = now.strftime("%Y-%m-%dT23:59:59+08:00")
    start = now.strftime("%Y-%m-%dT00:00:00+08:00")
    return start, end


def _default_week_window() -> tuple[str, str]:
    """上周一 00:00 → 上周日 23:59:59 CST. Overview / VendorDetail 默认窗口."""
    today = datetime.now(CST).date()
    last_monday = today - timedelta(days=today.weekday() + 7)
    last_sunday = last_monday + timedelta(days=6)
    return (
        f"{last_monday.isoformat()}T00:00:00+08:00",
        f"{last_sunday.isoformat()}T23:59:59+08:00",
    )



def _enforce_max_range(start: str, end: str) -> None:
    """校验时间窗合法性。"""
    try:
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
    except ValueError:
        raise HTTPException(status_code=400, detail="时间格式错误, 需 ISO 8601")
    if start_dt > end_dt:
        raise HTTPException(status_code=400, detail="起始时间不能晚于结束时间")


# ─── 原有接口（保留向后兼容） ───


def _safe_sum(values):
    """求和但忽略 None — 用于 token/count 字段, 上游不暴露的字段是 None.
    全 None → 返回 None (保留"上游不给"语义); 否则 None 当 0."""
    seen_real = False
    total = 0
    for v in values:
        if v is None:
            continue
        seen_real = True
        total += v
    return total if seen_real else None


def _add(a, b):
    """安全加法 — 任一为 None 时另一个不变. 都 None → None. 用于累加循环."""
    if a is None: return b
    if b is None: return a
    return a + b


@app.get("/api/usage")
def get_usage(
    start: str = Query(None, description="起始时间 ISO 格式"),
    end: str = Query(None, description="结束时间 ISO 格式"),
    type: str = Query("text", description="text 或 image"),
):
    if not start or not end:
        start, end = _default_dates()
    _enforce_max_range(start, end)

    aigc_type = "Text" if type == "text" else "Image"

    try:
        raw_data = fetch_usage(aigc_type, start, end)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    result = calculate_billing(raw_data, aigc_type=type)
    result["query"] = {"start": start, "end": end, "type": type}
    return result


@app.get("/api/usage/summary")
def get_summary(
    start: str = Query(None),
    end: str = Query(None),
):
    if not start or not end:
        start, end = _default_dates()
    _enforce_max_range(start, end)

    text_models = {}
    image_models = {}
    total_cost = 0
    text_cost = 0
    image_cost = 0
    daily_map = {}

    def _one(t, aigc_type):
        try:
            raw = fetch_usage(aigc_type, start, end)
            return t, calculate_billing(raw, aigc_type=t)
        except Exception:
            return t, None

    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(_one, "text", "Text"), ex.submit(_one, "image", "Image")]
        for fut in as_completed(futs):
            t, billing = fut.result()
            if billing is None:
                continue
            total_cost += billing["total_cost"]
            if t == "text":
                text_cost = billing["total_cost"]
                text_models = billing["models"]
            else:
                image_cost = billing["total_cost"]
                image_models = billing["models"]

            for d in billing["daily"]:
                date = d["date"]
                if date in daily_map:
                    daily_map[date]["cost"] += d["cost"]
                else:
                    daily_map[date] = d

    daily = sorted(daily_map.values(), key=lambda x: x["date"])

    # 双币换算 (tencent 原币种 CNY)
    rate_usd = lambda v: convert(v, "CNY", "USD")
    return {
        "total_cost": round(total_cost, 4),
        "total_cost_cny": round(total_cost, 4),
        "total_cost_usd": round(rate_usd(total_cost), 4),
        "text_cost": round(text_cost, 4),
        "text_cost_cny": round(text_cost, 4),
        "text_cost_usd": round(rate_usd(text_cost), 4),
        "image_cost": round(image_cost, 4),
        "image_cost_cny": round(image_cost, 4),
        "image_cost_usd": round(rate_usd(image_cost), 4),
        "text_models": text_models,
        "image_models": image_models,
        "daily": daily,
        "query": {"start": start, "end": end},
    }


@app.get("/api/pricing")
def get_pricing():
    return load_pricing()


@app.put("/api/pricing")
def update_pricing(pricing: dict):
    save_pricing(pricing)
    return {"status": "ok"}


# ─── 多供应商接口 ───


def _fetch_tc_unified(start: str, end: str) -> dict:
    """获取腾讯云数据并转为统一格式 (Text + Image 并发)。"""
    def _one(t, aigc_type):
        try:
            raw = fetch_usage(aigc_type, start, end)
            return t, calculate_billing(raw, aigc_type=t)
        except Exception:
            return t, {"models": {}, "daily": []}

    text_billing = {"models": {}, "daily": []}
    image_billing = {"models": {}, "daily": []}
    with ThreadPoolExecutor(max_workers=2) as ex:
        for fut in as_completed([
            ex.submit(_one, "text", "Text"),
            ex.submit(_one, "image", "Image"),
        ]):
            t, billing = fut.result()
            if t == "text":
                text_billing = billing
            else:
                image_billing = billing

    return tc_to_unified(text_billing, image_billing)


# 各 vendor 类型 → fetch 函数, 统一签名: (vendor, start, end) -> unified dict
VENDOR_DISPATCH = {
    "tc-cloud":       lambda v, s, e: _fetch_tc_unified(s, e),
    "new-api":        fetch_vendor_usage,
    "new-api-direct": fetch_direct_vendor_usage,
    "wangsu-aigw":    fetch_wangsu_vendor_usage,
    "apevon":         fetch_apevon_vendor_usage,
    "ucloud":         fetch_ucloud_vendor_usage,
    "road2all":       fetch_road2all_vendor_usage,
    "kimi":           fetch_kimi_vendor_usage,
    "volcengine":     fetch_volcengine_vendor_usage,
    "openai":         fetch_openai_vendor_usage,
    "bigmodel":       fetch_bigmodel_vendor_usage,
    "grok":           fetch_grok_vendor_usage,
}


def _get_vendor_payment_type(vendor_id: str) -> str | None:
    try:
        return get_vendor_payment_type(vendor_id)
    except DiscountConfigError as e:
        logging.getLogger(__name__).warning("读取供应商付费方式失败: %s", e)
        return None


@app.get("/api/vendors")
def list_vendors():
    # 凭据字段不下发到前端 (api_key/secret/password 等)
    SAFE_FIELDS = {"id", "name", "type", "enabled", "base_url", "currency",
                   "quota_per_dollar", "user_header"}
    return [{k: v for k, v in vendor.items() if k in SAFE_FIELDS}
            for vendor in load_vendors()]


@app.get("/api/vendors/{vendor_id}/usage")
def get_vendor_usage(
    vendor_id: str,
    start: str = Query(None),
    end: str = Query(None),
    models: list[str] | None = Query(None,
        description="可选, 多次传 ?models=A&models=B 或 ?models=A,B 返 per-model daily"),
    api_key: str | None = Query(None,
        description="可选, 按 API key 过滤 (只影响 DB 段, live 段不支持)"),
):
    if not start or not end:
        start, end = _default_week_window()
    _enforce_max_range(start, end)

    vendor = get_vendor(vendor_id)
    if not vendor:
        raise HTTPException(status_code=404, detail=f"供应商 {vendor_id} 不存在")

    fetch = VENDOR_DISPATCH.get(vendor["type"])
    if fetch is None:
        raise HTTPException(status_code=400, detail=f"不支持的供应商类型: {vendor['type']}")

    # ─── DB+Live 切分: 历史走 DB (vendor_usage_daily), 今天走 live (上游 API) ───
    db_range, live_range = split_window(start, end)

    db_result = None
    live_result = None
    try:
        if db_range is not None:
            db_result = query_vendor_usage(vendor, db_range[0], db_range[1], api_key=api_key)
        if live_range is not None:
            # live 段暂不支持 api_key 过滤 (上游实时接口未按 key 分组)
            # 如果选了 api_key, live 段跳过, 只返 DB 段
            if api_key is None:
                live_start = f"{live_range[0].isoformat()}T00:00:00+08:00"
                live_end = f"{live_range[1].isoformat()}T23:59:59+08:00"
                live_result = fetch(vendor, live_start, live_end)
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        # live 段失败 — 历史还能用就降级返
        if db_result is None:
            raise HTTPException(status_code=502, detail=str(e))

    result = merge_results(db_result, live_result)
    if result is None:
        # 空时间窗 (理论上 split 后至少一段有, 这里防御)
        result = {"total_cost": 0, "models": {}, "daily": [],
                  "vendor_id": vendor_id, "vendor_name": vendor.get("name"),
                  "currency": vendor.get("currency", "CNY")}

    # 添加双币 (USD + CNY) 换算 — 所有 vendor 统一双币展示
    currency = result.get("currency", "CNY")
    def _to(v, target):
        if currency == target:
            return v
        return convert(v, currency, target)

    result["total_cost_usd"] = round(_to(result["total_cost"], "USD"), 6)
    result["total_cost_cny"] = round(_to(result["total_cost"], "CNY"), 6)
    for m in result.get("models", {}).values():
        m["cost_usd"] = round(_to(m["total_cost"], "USD"), 6)
        m["cost_cny"] = round(_to(m["total_cost"], "CNY"), 6)
    for d in result.get("daily", []):
        d["cost_usd"] = round(_to(d["cost"], "USD"), 6)
        d["cost_cny"] = round(_to(d["cost"], "CNY"), 6)

    result["query"] = {"start": start, "end": end}
    result["vendor_type"] = vendor["type"]
    result["payment_type"] = _get_vendor_payment_type(vendor_id)

    # per-model daily — 仅当前端传了 models 才计算 (多曲线趋势图用)
    # models 可能是 ['A,B'] (逗号合一段传) 或 ['A', 'B'] (多次传 ?models=A&models=B), 规范化展平
    if models:
        from ingest.query import query_per_model_daily
        flat_models: list[str] = []
        for m in models:
            for part in m.split(","):
                p = part.strip()
                if p and p not in flat_models:
                    flat_models.append(p)

        # DB 段: SQL 直接出每个 (model, date) 的 cost / total_tokens
        db_per_model = {}
        if db_range is not None:
            db_per_model = query_per_model_daily(vendor_id, db_range[0], db_range[1], flat_models, api_key=api_key)

        # live 段 (今天): live_result.models[name] 是当天聚合, date 取 live_range 的最后一天
        # (上游接口只给一天总额, 不分小时, 不需要拆桶)
        if live_range is not None and live_result is not None:
            live_day_iso = live_range[1].isoformat()
            for m in flat_models:
                live_m = live_result.get("models", {}).get(m)
                if not live_m:
                    continue
                db_per_model.setdefault(m, []).append({
                    "date": live_day_iso,
                    "cost": float(live_m.get("total_cost") or 0),
                    "total_tokens": live_m.get("total_tokens"),
                })

        # 双币换算 — 跟 daily 同套规则
        for m, series in db_per_model.items():
            for pt in series:
                pt["cost_usd"] = round(_to(pt["cost"], "USD"), 6)
                pt["cost_cny"] = round(_to(pt["cost"], "CNY"), 6)
            # 按 date 排序 — DB 段已 ORDER BY, 但 live 段 append 在末尾, 同天可能合并
            series.sort(key=lambda x: x["date"])

        result["daily_by_model"] = db_per_model
        result["selected_models"] = flat_models

    return result


@app.get("/api/vendors/{vendor_id}/api-keys")
def get_vendor_api_keys_list(
    vendor_id: str,
    start: str = Query(None, description="开始日期 (可选, 限制范围)"),
    end: str = Query(None, description="结束日期 (可选, 限制范围)"),
):
    """获取 vendor 在指定时间范围内使用过的所有 API keys.

    返回 [{key, label}, ...], key 是实际值, label 是显示名 (可后续优化为别名).
    不传时间范围 = 返回全部历史.
    """
    vendor = get_vendor(vendor_id)
    if not vendor:
        raise HTTPException(status_code=404, detail=f"供应商 {vendor_id} 不存在")

    day_start = None
    day_end = None
    try:
        if start:
            day_start = datetime.fromisoformat(start).date()
        if end:
            day_end = datetime.fromisoformat(end).date()
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式错误, 需 YYYY-MM-DD")

    keys = get_vendor_api_keys(vendor_id, day_start, day_end)
    return [{"key": k, "label": k} for k in keys]


@app.get("/api/overview")
def get_overview(
    start: str = Query(None),
    end: str = Query(None),
    models: list[str] | None = Query(None,
        description="可选, 跨 vendor 按 model 多曲线对比"),
):
    """所有供应商汇总：总费用、全模型明细（不分供应商）、分供应商明细.

    DB+Live 切分:
    - 历史段 (start ~ yesterday): 走 vendor_usage_daily 一次 SQL 聚合 (毫秒级)
    - 今天段: 并发 fan-out 上游 vendor service (跟改造前一样)
    """
    if not start or not end:
        start, end = _default_week_window()
    _enforce_max_range(start, end)

    db_range, live_range = split_window(start, end)

    # 1. DB 段 — 一次 SQL 拿所有 vendor 的聚合
    db_vendor_results: list[dict] = []
    if db_range is not None:
        db_vendor_results = query_overview_vendors(db_range[0], db_range[1])

    # 2. Live 段 — 跟改造前一样并发 fan-out
    live_vendor_results: list[tuple[dict, dict]] = []
    failed_vendors: list[dict] = []
    if live_range is not None:
        live_start = f"{live_range[0].isoformat()}T00:00:00+08:00"
        live_end = f"{live_range[1].isoformat()}T23:59:59+08:00"
        enabled = [v for v in load_vendors() if v["type"] in VENDOR_DISPATCH]
        with ThreadPoolExecutor(max_workers=max(1, len(enabled))) as ex:
            futs = {ex.submit(VENDOR_DISPATCH[v["type"]], v, live_start, live_end): v
                    for v in enabled}
            for fut in as_completed(futs):
                vendor = futs[fut]
                try:
                    live_vendor_results.append((vendor, fut.result()))
                except Exception as e:
                    failed_vendors.append({
                        "vendor_id": vendor["id"],
                        "vendor_name": vendor["name"],
                        "error": str(e),
                    })

    # 3. 合并 DB + Live — 按 vendor_id 索引, merge 两段
    db_by_id = {v["vendor_id"]: v for v in db_vendor_results}
    live_by_id = {v["id"]: data for v, data in live_vendor_results}
    live_vendor_meta = {v["id"]: v for v, _ in live_vendor_results}
    all_vendor_ids = set(db_by_id) | set(live_by_id)

    vendors_data = []
    all_models: dict[str, dict] = {}
    total_cost_cny = 0
    total_cost_usd = 0
    total_prompt_tokens = None
    total_completion_tokens = None
    total_cache_tokens = None
    total_cache_read_tokens = None
    total_cache_write_tokens = None
    total_tokens = None
    daily_map: dict[str, dict] = {}

    # vendors.json 提供 name 等 fallback 元数据
    vendors_cfg = {v["id"]: v for v in load_vendors()}

    for vid in all_vendor_ids:
        db_part = db_by_id.get(vid)
        live_part = live_by_id.get(vid)
        merged = merge_results(db_part, live_part)
        if merged is None:
            continue

        # 优先用 vendors.json 的 name (display), 然后 DB 的 display_name, 最后 vid
        vendor_name = (vendors_cfg.get(vid, {}).get("name")
                       or merged.get("vendor_name") or vid)
        currency = merged.get("currency", "CNY")

        def _to(v, target, _cur=currency):
            return v if _cur == target else convert(v, _cur, target)

        cost_cny = _to(merged["total_cost"], "CNY")
        cost_usd = _to(merged["total_cost"], "USD")
        total_cost_cny += cost_cny
        total_cost_usd += cost_usd

        for name, m in merged.get("models", {}).items():
            model_cost_cny = _to(m["total_cost"], "CNY")
            model_cost_usd = _to(m["total_cost"], "USD")
            if name not in all_models:
                all_models[name] = {
                    "prompt_tokens": None, "completion_tokens": None,
                    "cache_tokens": None, "cache_read_tokens": None, "cache_write_tokens": None,
                    "total_tokens": None, "total_count": None,
                    "total_cost_cny": 0, "total_cost_usd": 0,
                    "image_count": None, "vendors": [],
                }
            all_models[name]["prompt_tokens"] = _add(all_models[name]["prompt_tokens"], m.get("prompt_tokens"))
            all_models[name]["completion_tokens"] = _add(all_models[name]["completion_tokens"], m.get("completion_tokens"))
            all_models[name]["cache_tokens"] = _add(all_models[name]["cache_tokens"], m.get("cache_tokens"))
            all_models[name]["cache_read_tokens"] = _add(all_models[name]["cache_read_tokens"],
                                                          m.get("cache_read_tokens", m.get("cache_tokens")))
            all_models[name]["cache_write_tokens"] = _add(all_models[name]["cache_write_tokens"], m.get("cache_write_tokens"))
            all_models[name]["total_tokens"] = _add(all_models[name]["total_tokens"], m.get("total_tokens"))
            all_models[name]["total_count"] = _add(all_models[name]["total_count"], m.get("total_count"))
            all_models[name]["total_cost_cny"] += model_cost_cny
            all_models[name]["total_cost_usd"] += model_cost_usd
            all_models[name]["image_count"] = _add(all_models[name]["image_count"], m.get("image_count"))
            if vendor_name not in all_models[name]["vendors"]:
                all_models[name]["vendors"].append(vendor_name)

            total_prompt_tokens = _add(total_prompt_tokens, m.get("prompt_tokens"))
            total_completion_tokens = _add(total_completion_tokens, m.get("completion_tokens"))
            total_cache_tokens = _add(total_cache_tokens, m.get("cache_tokens"))
            total_cache_read_tokens = _add(total_cache_read_tokens,
                                              m.get("cache_read_tokens", m.get("cache_tokens")))
            total_cache_write_tokens = _add(total_cache_write_tokens, m.get("cache_write_tokens"))
            total_tokens = _add(total_tokens, m.get("total_tokens"))

        for d in merged.get("daily", []):
            date = d["date"]
            day_cost_cny = _to(d["cost"], "CNY")
            day_cost_usd = _to(d["cost"], "USD")
            tt = d.get("total_tokens")
            if date in daily_map:
                daily_map[date]["cost_cny"] += day_cost_cny
                daily_map[date]["cost_usd"] += day_cost_usd
                daily_map[date]["total_tokens"] = _add(daily_map[date].get("total_tokens"), tt)
            else:
                daily_map[date] = {"date": date, "cost_cny": day_cost_cny,
                                    "cost_usd": day_cost_usd,
                                    "total_tokens": tt}

        vendors_data.append({
            "vendor_id": vid,
            "vendor_name": vendor_name,
            "currency": currency,
            "total_cost": merged["total_cost"],
            "total_cost_cny": cost_cny,
            "total_cost_usd": cost_usd,
            "model_count": len(merged.get("models", {})),
            "total_count": _safe_sum(m.get("total_count")
                                for m in merged.get("models", {}).values()),
            "prompt_tokens": _safe_sum(m.get("prompt_tokens")
                                  for m in merged.get("models", {}).values()),
            "completion_tokens": _safe_sum(m.get("completion_tokens")
                                      for m in merged.get("models", {}).values()),
            "cache_tokens": _safe_sum(m.get("cache_tokens")
                                 for m in merged.get("models", {}).values()),
            "cache_read_tokens": _safe_sum(m.get("cache_read_tokens")
                                      for m in merged.get("models", {}).values()),
            "cache_write_tokens": _safe_sum(m.get("cache_write_tokens")
                                       for m in merged.get("models", {}).values()),
            "total_tokens": _safe_sum(m.get("total_tokens")
                                 for m in merged.get("models", {}).values()),
        })

    for m in all_models.values():
        m["total_cost_cny"] = round(m["total_cost_cny"], 4)
        m["total_cost_usd"] = round(m["total_cost_usd"], 4)
    for d in daily_map.values():
        d["cost_cny"] = round(d["cost_cny"], 4)
        d["cost_usd"] = round(d["cost_usd"], 4)

    daily = sorted(daily_map.values(), key=lambda x: x["date"])

    # per-model daily — 仅当前端勾了 models 才计算 (跨 vendor SUM, 给多曲线趋势图用)
    daily_by_model: dict | None = None
    flat_selected: list[str] = []
    if models:
        from ingest.query import query_per_model_daily_overview
        for m in models:
            for part in m.split(","):
                p = part.strip()
                if p and p not in flat_selected:
                    flat_selected.append(p)

        # DB 段直接 SQL
        daily_by_model = {}
        if db_range is not None:
            daily_by_model = query_per_model_daily_overview(db_range[0], db_range[1], flat_selected)
        for m in flat_selected:
            daily_by_model.setdefault(m, [])

        # live 段 (今天): 从已经 fan-out 拿到的 live_vendor_results 里, 把选中 model
        # 在每家 vendor 当天的 cost / total_tokens SUM 起来
        if live_range is not None and live_vendor_results:
            live_day_iso = live_range[1].isoformat()
            for m in flat_selected:
                cny_sum = 0.0
                usd_sum = 0.0
                tt_sum: int | None = None
                for vendor, live_data in live_vendor_results:
                    live_m = (live_data or {}).get("models", {}).get(m)
                    if not live_m:
                        continue
                    cur = live_data.get("currency", "CNY")
                    raw_cost = live_m.get("total_cost") or 0
                    cny_sum += raw_cost if cur == "CNY" else convert(raw_cost, cur, "CNY")
                    usd_sum += raw_cost if cur == "USD" else convert(raw_cost, cur, "USD")
                    tt = live_m.get("total_tokens")
                    if tt is not None:
                        tt_sum = (tt_sum or 0) + tt
                if cny_sum > 0 or tt_sum is not None:
                    daily_by_model[m].append({
                        "date": live_day_iso,
                        "cost_cny": round(cny_sum, 6),
                        "cost_usd": round(usd_sum, 6),
                        "total_tokens": tt_sum,
                    })

        # 同 model 同 date 可能 DB + live 都出 (理论上不会, split_window 保证不重叠), 防御性 dedup + 排序
        for m, series in daily_by_model.items():
            by_date: dict[str, dict] = {}
            for pt in series:
                if pt["date"] in by_date:
                    a = by_date[pt["date"]]
                    a["cost_cny"] += pt["cost_cny"]
                    a["cost_usd"] += pt["cost_usd"]
                    if pt.get("total_tokens") is not None:
                        a["total_tokens"] = (a.get("total_tokens") or 0) + pt["total_tokens"]
                else:
                    by_date[pt["date"]] = dict(pt)
            daily_by_model[m] = [by_date[d] for d in sorted(by_date)]

    return {
        "total_cost_cny": round(total_cost_cny, 4),
        "total_cost_usd": round(total_cost_usd, 4),
        "total_prompt_tokens": total_prompt_tokens,
        "total_completion_tokens": total_completion_tokens,
        "total_cache_tokens": total_cache_tokens,
        "total_cache_read_tokens": total_cache_read_tokens,
        "total_cache_write_tokens": total_cache_write_tokens,
        "total_tokens": total_tokens,
        "all_models": all_models,
        "vendors": vendors_data,
        "failed_vendors": failed_vendors,
        "daily": daily,
        "exchange_rates": get_rates(),
        "query": {"start": start, "end": end},
        "freshness": {vid: d.isoformat() for vid, d in get_freshness().items()},
        "missing_days": get_missing_days_in_window(
            datetime.fromisoformat(start).date(),
            datetime.fromisoformat(end).date(),
        ),
        # 最近 48h 内最后一次 run 是 failed 的 vendor (session 过期 / cookie 失效 / 上游 502 等)
        # 前端 Overview 顶部报"X 需重新登录或检查", 引导去 vendor 详情页
        "failed_ingest_vendors": get_recently_failed_vendors(within_hours=48),
        # 选了 model 才返这两个字段, 没选则不返 (前端 chartDailyByModel 自动 fallback)
        **({"daily_by_model": daily_by_model, "selected_models": flat_selected}
           if daily_by_model is not None else {}),
    }


# ─── Session 管理接口 ───
# 登录编排 (session 文件 / 锁 / 判据配置 / 异步发起) 在 login_flow.py,
# agent 巡检 (ingest/agent) 直接复用, 这里只做 HTTP 壳.
import login_flow as _login_flow


@app.get("/api/vendors/{vendor_id}/session")
def get_session_status(vendor_id: str):
    """检查供应商的 session 状态。"""
    vendor = get_vendor(vendor_id)
    if not vendor:
        raise HTTPException(status_code=404, detail="供应商不存在")

    status = _login_flow.session_status(vendor)
    return {**status, "payment_type": _get_vendor_payment_type(vendor_id)}


@app.post("/api/vendors/{vendor_id}/login")
def trigger_login(vendor_id: str):
    """弹出浏览器让用户登录，等待完成后返回。

    编排逻辑在 login_flow.start_login_async (判据配置/锁/线程都在那里,
    agent 巡检复用同一入口), 这里只等结果 + 转 HTTP 状态码.
    """
    try:
        started = _login_flow.start_login_async(vendor_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="供应商不存在")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if started["status"] == "waiting":
        return started

    # save_session 自己 5 分钟超时, 这里再多 10s 给 _close_all 收尾
    result = _login_flow.wait_login_result(vendor_id, timeout=310)
    if result is None or result["status"] != "ok":
        raise HTTPException(status_code=400, detail=(result or {}).get("message", "登录失败"))
    return result


@app.post("/api/vendors/{vendor_id}/login/cancel")
def cancel_login(vendor_id: str):
    """中止正在进行的登录 (用户关 VNC tab 后调这个, 后端立刻关浏览器 + 释放 lock)。

    幂等: 即使没有 active login 也返回 ok, 顺便清掉可能残留的 lock file
    (上次进程崩了没清干净的情况)。"""
    return _login_flow.cancel_login(vendor_id)



@app.post("/api/vendors/{vendor_id}/logout")
def trigger_logout(vendor_id: str):
    """删除供应商的 session 文件。"""
    vendor = get_vendor(vendor_id)
    if not vendor:
        raise HTTPException(status_code=404, detail="供应商不存在")

    if vendor.get("type") not in ("new-api", "kimi", "apevon", "bigmodel"):
        raise HTTPException(status_code=400, detail="此供应商不需要登录")

    _login_flow.delete_session(vendor)

    return {"status": "ok", "message": "已登出"}


@app.put("/api/vendors/{vendor_id}/account")
def update_account(vendor_id: str, body: dict, request: Request):
    """更新 new-api-direct / road2all 供应商的账号密码 (写到 .env)。

    审计: 每次调用都追加一行到 backend/data/audit.log (持久化, 容器 rebuild
    不丢). 只记 username, 不记密码.
    """
    vendor = get_vendor(vendor_id)
    if not vendor:
        raise HTTPException(status_code=404, detail="供应商不存在")
    if vendor.get("type") not in ("new-api-direct", "road2all"):
        raise HTTPException(status_code=400, detail="此供应商不支持换号")

    import os
    from dotenv import set_key
    from pathlib import Path
    env_file = Path(__file__).parent / ".env"
    env_file.touch(exist_ok=True)
    user_key = f"{vendor_id.upper()}_USERNAME"
    pass_key = f"{vendor_id.upper()}_PASSWORD"

    # 审计日志: 写盘到 data/audit.log, 容器重建/重启都不丢
    audit_path = Path(__file__).parent / "data" / "audit.log"
    audit_path.parent.mkdir(exist_ok=True)
    old_user = os.environ.get(user_key, "<unset>")
    new_user = body.get("username", "")
    client_ip = request.client.host if request.client else "<unknown>"
    fwd = request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip") or ""
    ts = datetime.now(CST).isoformat()
    with audit_path.open("a", encoding="utf-8") as f:
        f.write(
            f"{ts}\tPUT /account\tvendor={vendor_id}\t"
            f"client_ip={client_ip}\tx-fwd={fwd}\t"
            f"old_user={old_user!r}\tnew_user={new_user!r}\n"
        )

    set_key(str(env_file), user_key, body["username"])
    set_key(str(env_file), pass_key, body["password"])
    # 同步到当前进程, 下次 fetch 立刻用新值 (load_vendors 重新 _hydrate 时会读)
    os.environ[user_key] = body["username"]
    os.environ[pass_key] = body["password"]

    # 清掉对应 vendor 的内存 session 缓存, 让下次 fetch 用新账号重登
    if vendor.get("type") == "new-api-direct":
        from newapi_direct_client import _invalidate_session
        # 重新拿一遍 vendor (现在带新凭据)
        _invalidate_session(get_vendor(vendor_id))

    return {"status": "ok", "message": "账号已更新"}
