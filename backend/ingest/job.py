"""Ingest job 主循环 — 单 vendor × 单时间窗的入库.

写入顺序 (一天一个事务):
  1. UPSERT vendor_model_usage_daily (这家这天所有 model 行)
  2. UPSERT vendor_usage_daily       (从 ↑ 求 SUM)
  3. UPSERT usage_daily_total        (重算这天全局, 刷新 complete_vendor_count)
  4. UPSERT vendor_ingest_state.last_ingested_date

中途抛 → 事务回滚, state 不前进, 重试从 state.last_ingested_date+1 续.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import traceback
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from exchange_rate import convert as fx_convert, get_rates
from ingest.adapters import ADAPTERS, ModelRow
from ingest.db import SessionLocal
from ingest.models import (
    UsageDailyTotal,
    VendorApiKeyUsageDaily,
    VendorIngestRun,
    VendorIngestState,
    VendorMeta,
    VendorModelUsageDaily,
    VendorUsageDaily,
)

log = logging.getLogger("ingest.job")


def _load_vendor_config(vendor_id: str) -> dict | None:
    """从 backend/config/vendors.json 读单个 vendor 配置, 顺便从 .env 注入凭据."""
    cfg_path = Path(__file__).parent.parent / "config" / "vendors.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    for v in cfg.get("vendors", []):
        if v.get("id") == vendor_id:
            # 注入凭据 — 沿用现有 vendor client 的 env 读法
            _inject_env_credentials(v)
            return v
    return None


def _inject_env_credentials(vendor: dict) -> None:
    """跟主路由 main.py 一样的逻辑, 把 .env 里凭据注入到 vendor dict."""
    import os
    vid = vendor["id"]
    vtype = vendor.get("type")
    if vtype == "openai":
        vendor["api_key"] = os.environ.get("OPENAI_API_KEY", "")
    elif vtype == "grok":
        vendor["management_key"] = os.environ.get("GROK_MANAGEMENT_KEY", "")
        vendor["team_id"] = os.environ.get("GROK_TEAM_ID", "")
    elif vtype == "tc-cloud":
        vendor["secret_id"] = os.environ.get("TC_SECRET_ID", "")
        vendor["secret_key"] = os.environ.get("TC_SECRET_KEY", "")
    elif vtype == "volcengine":
        vendor["access_key"] = os.environ.get("VOLCENGINE_ACCESS_KEY", "")
        vendor["secret_key"] = os.environ.get("VOLCENGINE_SECRET_KEY", "")
    elif vtype == "wangsu-aigw":
        vendor["access_key"] = os.environ.get("WANGSU_ACCESS_KEY", "")
        vendor["secret_key"] = os.environ.get("WANGSU_SECRET_KEY", "")
        vendor["console_cookie"] = os.environ.get("WANGSU_CONSOLE_COOKIE", "")
    elif vtype == "ucloud":
        vendor["public_key"] = os.environ.get("UCLOUD_PUBLIC_KEY", "")
        vendor["secret_key"] = os.environ.get("UCLOUD_SECRET_KEY", "")
        vendor["project_id"] = os.environ.get("UCLOUD_PROJECT_ID", "")
    elif vtype in ("new-api-direct", "road2all"):
        prefix = vid.upper()
        vendor["username"] = os.environ.get(f"{prefix}_USERNAME", "")
        vendor["password"] = os.environ.get(f"{prefix}_PASSWORD", "")


def _upsert_vendor_meta(s: Session, vendor: dict, adapter_class) -> None:
    """每次入库前同步一下 vendor_meta (display_name 等可能改)."""
    stmt = pg_insert(VendorMeta).values(
        vendor_id=vendor["id"],
        display_name=vendor.get("name") or vendor["id"],
        vendor_type=vendor.get("type", "unknown"),
        native_currency=adapter_class.native_currency,
        source=adapter_class.source,
        enabled=bool(vendor.get("enabled", True)),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["vendor_id"],
        set_={
            "display_name": stmt.excluded.display_name,
            "vendor_type": stmt.excluded.vendor_type,
            "native_currency": stmt.excluded.native_currency,
            "source": stmt.excluded.source,
            "enabled": stmt.excluded.enabled,
        },
    )
    s.execute(stmt)


def _convert_cost(cost_native: float, native: str, fx_rate_usd_to_cny: float) -> tuple[float, float]:
    """native -> (usd, cny). fx_rate_usd_to_cny = 1 美元换多少人民币."""
    if native == "USD":
        return cost_native, round(cost_native * fx_rate_usd_to_cny, 6)
    if native == "CNY":
        return round(cost_native / fx_rate_usd_to_cny, 6), cost_native
    # 其他币种用 exchange_rate.convert (USD 基准)
    usd = fx_convert(cost_native, native, "USD")
    return usd, round(usd * fx_rate_usd_to_cny, 6)


def _ingest_one_day(
    s: Session,
    vendor: dict,
    adapter,
    day: dt.date,
    run_id: int,
    fx_rate_usd_to_cny: float,
    has_cache_detail: bool,
) -> int:
    """跑一天: 调 adapter, 三层 upsert. 返回 upsert 的 model 行数."""
    rows = adapter.fetch_one_day(day)

    # 1. 算每个 model 的 native/usd/cny + 累加汇总
    model_payloads: list[dict] = []
    agg = {
        "prompt": 0, "completion": 0, "cache_r": 0, "cache_w": 0, "total": 0,
        "req": 0, "img": 0, "cost_native": Decimal(0), "cost_usd": Decimal(0), "cost_cny": Decimal(0),
    }
    has_any_token_breakdown = False
    # cache_write 用 None-until-seen: 上游没暴露该维度 (所有 model 行都是 None) 时,
    # vendor 层也保持 NULL ("—"), 不写 0 — 与 model 层 NULL/0 语义对齐
    has_cache_write_value = False

    for r in rows:
        usd, cny = _convert_cost(r.cost_native, adapter.native_currency, fx_rate_usd_to_cny)
        model_payloads.append({
            "vendor_id": vendor["id"],
            "usage_date": day,
            "model": r.model,
            "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "cache_read_tokens": r.cache_read_tokens,
            "cache_write_tokens": r.cache_write_tokens,
            "total_tokens": r.total_tokens,
            "request_count": r.request_count,
            "image_count": r.image_count,
            "cost_native": r.cost_native,
            "cost_usd": usd,
            "cost_cny": cny,
            "fx_rate": fx_rate_usd_to_cny,
            "last_run_id": run_id,
        })
        # 汇总
        agg["prompt"] += r.prompt_tokens or 0
        agg["completion"] += r.completion_tokens or 0
        agg["cache_r"] += r.cache_read_tokens or 0
        agg["cache_w"] += r.cache_write_tokens or 0
        if r.cache_write_tokens is not None:
            has_cache_write_value = True
        agg["total"] += r.total_tokens or 0
        agg["req"] += r.request_count or 0
        agg["img"] += r.image_count or 0
        agg["cost_native"] += Decimal(str(r.cost_native))
        agg["cost_usd"] += Decimal(str(usd))
        agg["cost_cny"] += Decimal(str(cny))
        if r.prompt_tokens is not None or r.completion_tokens is not None:
            has_any_token_breakdown = True

    # 没数据 — 也写一行 zero (这样 has_cache_detail / state 推进可见)
    # 注意 vendor_model_usage_daily 没数据则不写任何行, 但 vendor_usage_daily 必写
    # 2. UPSERT vendor_usage_daily (先写汇总, model 表有 FK)
    vud_payload = {
        "vendor_id": vendor["id"],
        "usage_date": day,
        "prompt_tokens": agg["prompt"] if has_any_token_breakdown else None,
        "completion_tokens": agg["completion"] if has_any_token_breakdown else None,
        "cache_read_tokens": agg["cache_r"] if has_cache_detail else None,
        "cache_write_tokens": (agg["cache_w"] if has_cache_write_value else None),
        "total_tokens": agg["total"] if agg["total"] else None,
        "request_count": agg["req"] if agg["req"] else None,
        "image_count": agg["img"] if agg["img"] else None,
        "cost_native": float(agg["cost_native"]),
        "cost_usd": float(agg["cost_usd"]),
        "cost_cny": float(agg["cost_cny"]),
        "fx_rate": fx_rate_usd_to_cny,
        "has_cache_detail": has_cache_detail,
        "last_run_id": run_id,
    }
    stmt = pg_insert(VendorUsageDaily).values(**vud_payload)
    upd_cols = {k: stmt.excluded[k] for k in vud_payload if k not in ("vendor_id", "usage_date")}
    s.execute(stmt.on_conflict_do_update(
        index_elements=["vendor_id", "usage_date"], set_=upd_cols,
    ))

    # 3. UPSERT vendor_model_usage_daily (现在 FK 父行已经有了)
    if model_payloads:
        stmt = pg_insert(VendorModelUsageDaily).values(model_payloads)
        upd_cols = {
            k: stmt.excluded[k] for k in model_payloads[0]
            if k not in ("vendor_id", "usage_date", "model")
        }
        # 拆分字段 NULL 不覆盖已有值: 快路径 (如 blueshirt /api/data/self) 传 None,
        # 由慢路径事后补值. 不 COALESCE 的话 23:00 recheck / 手动重跑会把慢路径
        # 补好的 prompt/completion/cache 用 NULL 抹掉, 且之后没有任务再补回来.
        for col in ("prompt_tokens", "completion_tokens",
                    "cache_read_tokens", "cache_write_tokens"):
            upd_cols[col] = func.coalesce(
                stmt.excluded[col], getattr(VendorModelUsageDaily, col))
        s.execute(stmt.on_conflict_do_update(
            index_elements=["vendor_id", "usage_date", "model"], set_=upd_cols,
        ))

        # 3b. 从 model 表 SUM 回写 vendor token/count 字段 — 保证两层物化一致
        # cost 字段不动 (用 agg 累加的, 跟 adapter 总额一致, 避免 SUM(浮点) 误差)
        s.execute(text("""
            UPDATE vendor_usage_daily AS v SET
                prompt_tokens = sub.p,
                completion_tokens = sub.c,
                cache_read_tokens = sub.cr,
                cache_write_tokens = sub.cw,
                total_tokens = sub.t,
                request_count = sub.rc,
                image_count = sub.ic
            FROM (
                SELECT SUM(prompt_tokens) AS p,
                       SUM(completion_tokens) AS c,
                       SUM(cache_read_tokens) AS cr,
                       SUM(cache_write_tokens) AS cw,
                       SUM(total_tokens) AS t,
                       SUM(request_count) AS rc,
                       SUM(image_count) AS ic
                FROM vendor_model_usage_daily
                WHERE vendor_id=:v AND usage_date=:d
            ) AS sub
            WHERE v.vendor_id=:v AND v.usage_date=:d
        """), {"v": vendor["id"], "d": day})

    # 3c. vendor_apikey_usage_daily — 按 API key 拆分的平行表 (仅 adapter 实现了 fetch_apikey_rows)
    # 这张表颗粒度 finer (vendor, day, api_key, model), 主表 PK 容不下 key, 所以单独存.
    # 幂等: 先删这一 (vendor, day) 的旧行, 再插新行 — 同一天 reingest 不会留 stale key 行.
    key_rows = adapter.fetch_apikey_rows(day)
    if key_rows:
        s.execute(text(
            "DELETE FROM vendor_apikey_usage_daily "
            "WHERE vendor_id=:v AND usage_date=:d"
        ), {"v": vendor["id"], "d": day})
        key_payloads = []
        for kr in key_rows:
            usd, cny = _convert_cost(kr.cost_native, adapter.native_currency, fx_rate_usd_to_cny)
            key_payloads.append({
                "vendor_id": vendor["id"],
                "usage_date": day,
                "api_key": kr.api_key,
                "model": kr.model,
                "prompt_tokens": kr.prompt_tokens,
                "completion_tokens": kr.completion_tokens,
                "cache_read_tokens": kr.cache_read_tokens,
                "cache_write_tokens": kr.cache_write_tokens,
                "total_tokens": kr.total_tokens,
                "request_count": kr.request_count,
                "image_count": kr.image_count,
                "cost_native": kr.cost_native,
                "cost_usd": usd,
                "cost_cny": cny,
                "fx_rate": fx_rate_usd_to_cny,
                "last_run_id": run_id,
            })
        s.execute(pg_insert(VendorApiKeyUsageDaily).values(key_payloads))

    # 4. 重算 usage_daily_total — 这一天的全局聚合 from vendor_usage_daily
    _refresh_usage_daily_total(s, day, fx_rate_usd_to_cny)

    # 5. 更新 state — last_ingested_date 取最大值 (backfill 从昨天往老跑, 不应覆盖最新)
    state_stmt = pg_insert(VendorIngestState).values(
        vendor_id=vendor["id"], last_ingested_date=day, last_run_id=run_id,
    )
    s.execute(state_stmt.on_conflict_do_update(
        index_elements=["vendor_id"],
        set_={
            "last_ingested_date": func.greatest(
                VendorIngestState.last_ingested_date,
                state_stmt.excluded.last_ingested_date,
            ),
            "last_run_id": state_stmt.excluded.last_run_id,
        },
    ))

    return len(model_payloads)


def _refresh_usage_daily_total(s: Session, day: dt.date, fx_rate: float) -> None:
    """重算这一天的全局汇总 — SUM(vendor_usage_daily) WHERE usage_date=day."""
    # 当天已入库的 vendor 数
    q = select(
        func.sum(VendorUsageDaily.prompt_tokens),
        func.sum(VendorUsageDaily.completion_tokens),
        func.sum(VendorUsageDaily.cache_read_tokens),
        func.sum(VendorUsageDaily.cache_write_tokens),
        func.sum(VendorUsageDaily.total_tokens),
        func.sum(VendorUsageDaily.request_count),
        func.sum(VendorUsageDaily.image_count),
        func.sum(VendorUsageDaily.cost_usd),
        func.sum(VendorUsageDaily.cost_cny),
        func.count(VendorUsageDaily.vendor_id),
    ).where(VendorUsageDaily.usage_date == day)
    r = s.execute(q).one()
    complete_cnt = r[9] or 0

    # enabled vendor 总数
    expected = s.execute(
        select(func.count(VendorMeta.vendor_id)).where(VendorMeta.enabled.is_(True))
    ).scalar() or 0

    stmt = pg_insert(UsageDailyTotal).values(
        usage_date=day,
        prompt_tokens=r[0], completion_tokens=r[1],
        cache_read_tokens=r[2], cache_write_tokens=r[3],
        total_tokens=r[4],
        request_count=r[5], image_count=r[6],
        cost_usd=float(r[7] or 0), cost_cny=float(r[8] or 0),
        fx_rate=fx_rate,
        complete_vendor_count=complete_cnt,
        expected_vendor_count=expected,
    )
    upd_cols = {k: stmt.excluded[k] for k in [
        "prompt_tokens", "completion_tokens", "cache_read_tokens", "cache_write_tokens",
        "total_tokens", "request_count", "image_count", "cost_usd", "cost_cny", "fx_rate",
        "complete_vendor_count", "expected_vendor_count",
    ]}
    s.execute(stmt.on_conflict_do_update(index_elements=["usage_date"], set_=upd_cols))


def run_ingest(vendor_id: str, start: dt.date, end: dt.date,
               trigger: str = "manual", attempt: int = 1) -> int:
    """单 vendor 单时间窗的入库. 返回 run_id 供 xxl-job / 前端轮询."""
    adapter_cls = ADAPTERS.get(vendor_id)
    if adapter_cls is None:
        raise ValueError(f"adapter not registered for vendor_id={vendor_id} (PR3 才接入剩下的)")

    vendor = _load_vendor_config(vendor_id)
    if vendor is None:
        raise ValueError(f"vendor {vendor_id} not in vendors.json")

    adapter = adapter_cls(vendor)
    fx_rates = get_rates()
    fx_usd_to_cny = float(fx_rates.get("CNY") or 7.2)

    # 1. 开 run 行 — UNIQUE INDEX 防 xxl-job 并发触发
    with SessionLocal() as s:
        _upsert_vendor_meta(s, vendor, adapter_cls)
        run = VendorIngestRun(
            vendor_id=vendor_id, window_start=start, window_end=end,
            trigger=trigger, status="running", attempt=attempt,
        )
        s.add(run)
        s.commit()
        run_id = run.id

    log.info(f"[{vendor_id}] run_id={run_id} window={start}~{end} started")

    # 2. 按天循环, 每天一个事务
    total_rows = 0
    error_msg: str | None = None
    info_msg: str | None = None  # 跑成功但有需要让用户知道的 hint (e.g. stat-fallback 触发, model 拆分缺失). 不影响 status, 仅写入 run.error_msg 给前端识别.
    day = start
    while day <= end:
        try:
            with SessionLocal() as s:
                n = _ingest_one_day(s, vendor, adapter, day, run_id, fx_usd_to_cny,
                                    has_cache_detail=adapter_cls.has_cache_detail)
                s.commit()
                total_rows += n
            log.info(f"[{vendor_id}] {day}: upserted {n} models")
        except Exception as e:
            error_msg = f"{day}: {type(e).__name__}: {e}\n{traceback.format_exc()}"
            log.error(f"[{vendor_id}] {day} FAILED: {e}")
            break
        day += dt.timedelta(days=1)

    # Sanity check: total_rows == 0 不一定是失败, 可能上游真的 0 调用 (业务停用 / 周末没人调).
    # 优先走 adapter.fetch_upstream_total_cost hook — 上游 stat 接口确认 0 → 真无调用, 不报警.
    # adapter 没实现 hook → 走旧的"近 14 天 avg"启发式兜底.
    if not error_msg and total_rows == 0:
        upstream_cost: float | None = None
        try:
            upstream_cost = adapter.fetch_upstream_total_cost(end)
        except Exception as _e:
            log.warning(f"[{vendor_id}] fetch_upstream_total_cost failed: {_e}")

        if upstream_cost == 0:
            # 上游确认 0 调用 — 不是失败, 不报警
            log.info(f"[{vendor_id}] 0 rows + 上游 stat=0 → 真无调用, 不报警")
        elif upstream_cost is not None and upstream_cost > 0:
            if getattr(adapter, "has_stat_fallback", False):
                # 拆快慢路径模式 (apevon): stat 总额接口稳, model 拆分接口偶尔漏聚合.
                # 用 stat 写 vendor_usage_daily.cost (model 行不写, 等慢路径补),
                # state 不推进 → cron 下次还会回头补这天. UI 通过 prompt_tokens IS NULL
                # 走"详情数据慢路径未到"的黄色提示, 不报红色 failed.
                usd, cny = _convert_cost(upstream_cost, adapter.native_currency, fx_usd_to_cny)
                with SessionLocal() as s:
                    s.execute(text("""
                        UPDATE vendor_usage_daily
                        SET cost_native = :cn, cost_usd = :cu, cost_cny = :cy,
                            fx_rate = :fx, last_run_id = :rid
                        WHERE vendor_id = :v AND usage_date = :d
                    """), {
                        "cn": float(upstream_cost), "cu": float(usd), "cy": float(cny),
                        "fx": fx_usd_to_cny, "rid": run_id, "v": vendor_id, "d": end,
                    })
                    _refresh_usage_daily_total(s, end, fx_usd_to_cny)
                    # state 回滚 — _ingest_one_day 已经把 last_ingested_date 推到 end,
                    # 但慢路径还没补, 让 cron 下次还会从 end 重新跑
                    s.execute(text("""
                        UPDATE vendor_ingest_state
                        SET last_ingested_date = :prev
                        WHERE vendor_id = :v AND last_ingested_date = :d
                    """), {"v": vendor_id, "d": end, "prev": end - dt.timedelta(days=1)})
                    s.commit()
                log.warning(
                    f"[{vendor_id}] stat-fallback: 上游 stat=${upstream_cost:.4f} 写入 cost, "
                    f"model 拆分缺失, state 回滚到 {end - dt.timedelta(days=1)} 等慢路径补"
                )
                # status 仍 success (确实没失败), 但把 hint 留到 run.error_msg 给前端展示
                # — 否则用户点了 "补慢路径" 看到绿色 success 会误以为补完了, 几天回头才发现
                # prompt/completion/cache 列还是 - 一脸懵.
                info_msg = (
                    f"上游 statistics 接口返空 (model 拆分缺失), 已用 stat 总额接口写入 cost "
                    f"${upstream_cost:.4f}. cron 23:00 / 明天 07:00 会重试拉 statistics; "
                    f"上游账单出账延迟通常 T+1, 偶尔 T+2."
                )
            else:
                # 其他 vendor 没拆快慢路径 — 维持原 silent-empty hard fail
                error_msg = (
                    f"silent-empty: 上游 stat 显示 cost={upstream_cost:.4f} (非零), "
                    f"但 fetch_one_day 返 0 rows — 上游接口不一致, 怀疑 statistics 接口延迟 / 失效"
                )
                log.error(f"[{vendor_id}] {error_msg}")
        else:
            # adapter 没实现 hook 或拉 stat 失败 — 走旧的启发式
            try:
                with SessionLocal() as s:
                    avg = s.execute(text("""
                        SELECT COALESCE(AVG(cost_cny), 0)
                        FROM vendor_usage_daily
                        WHERE vendor_id = :v
                          AND usage_date BETWEEN :s AND :e
                    """), {
                        "v": vendor_id,
                        "s": end - dt.timedelta(days=14),
                        "e": end - dt.timedelta(days=1),
                    }).scalar() or 0
                if float(avg) > 10:
                    error_msg = (
                        f"silent-empty: 上游返 0 rows, 但 vendor {vendor_id} 近 14 天 "
                        f"均 cost=¥{float(avg):.2f}/天 (> 10), 怀疑 session 失效 / 账单未出账"
                    )
                    log.error(f"[{vendor_id}] {error_msg}")
            except Exception as _e:
                log.warning(f"[{vendor_id}] sanity check itself failed (允许 silent-success): {_e}")

    # 3. 关 run 行
    with SessionLocal() as s:
        run = s.get(VendorIngestRun, run_id)
        run.status = "failed" if error_msg else "success"
        run.rows_upserted = total_rows
        # error_msg 优先 (failed 才有); 没真错时把 info_msg 留进去 — 前端在
        # status=success && rows_upserted=0 && error_msg 时显示黄 tag "上游空"
        run.error_msg = error_msg or info_msg
        run.finished_at = dt.datetime.now(dt.timezone.utc)
        s.commit()

    if error_msg:
        raise RuntimeError(error_msg.splitlines()[0])
    return run_id


# ─────────────────────────────────────────────────────────────────────
# 慢路径补字段 (dedicated 模式) — blueshirt/nulls 用各自专门的 slow 模块
# (apevon 没专门 slow 模块, 走主入库 reingest, 由 router endpoint 直接调
#  run_ingest_with_retry, 不走本 helper.)
#
# 跟 _ingest_one_day 不一样: dedicated slow_fill 只 UPDATE vendor_model_usage_daily
# 的 prompt/comp/cache 拆分字段, cost / total / request_count 不动 (那是快路径的活).
# 走的 run 行 trigger='slow-fill', 跟主入库的 cron / manual run 区分.
# ─────────────────────────────────────────────────────────────────────
def _slow_fill_blueshirt(_vendor, day):
    from ingest.blueshirt_slow import slow_fill_day
    return slow_fill_day(day)


def _slow_fill_newapi_direct(vendor, day):
    from ingest.newapi_direct_slow import slow_fill_day
    return slow_fill_day(vendor, day)


_SLOW_FILL_DISPATCH = {
    "blueshirt": _slow_fill_blueshirt,
    "nulls":     _slow_fill_newapi_direct,
}


def run_slow_fill_dedicated(vendor_id: str, day: dt.date,
                            trigger: str = "slow-fill") -> int:
    """blueshirt/nulls 慢路径补 model 拆分字段 — 同步跑, 返回 run_id.

    跟 run_ingest 一样的 run 行管理 (running → success/failed + error_msg + rows_upserted),
    所以前端 /api/ingest/runs 拉到这条 run, 跟主入库 run 同样的 status tag 显示.

    走 uniq_running_per_vendor 部分唯一索引兜并发 — 同 vendor 已经在 running 会 IntegrityError 抛.

    谁来调:
    - router endpoint (BackgroundTask 包一层, 手动点按钮触发) — trigger='slow-fill'
    - scheduler _run_one (cron 跑完快路径后立即调) — trigger='slow-fill'
      (两边同 trigger 名, 前端不区分自动/手动, 只看 run 状态)

    不带重试 — 慢路径 retention 短, 失败基本是 session 过期 / 上游 LRU 清掉了, 重试无效;
    cron 第二天会再来.
    """
    if vendor_id not in _SLOW_FILL_DISPATCH:
        raise ValueError(f"vendor {vendor_id} 没有 dedicated slow-fill 模块 "
                         f"(支持: {sorted(_SLOW_FILL_DISPATCH)})")

    vendor = _load_vendor_config(vendor_id)
    if vendor is None:
        raise ValueError(f"vendor {vendor_id} not in vendors.json")

    with SessionLocal() as s:
        run = VendorIngestRun(
            vendor_id=vendor_id, window_start=day, window_end=day,
            trigger=trigger, status="running", attempt=1,
        )
        s.add(run)
        s.commit()
        run_id = run.id

    error_msg: str | None = None
    rows = 0
    empty_note: str | None = None  # 跑成功但上游没数据 — status 仍 success, error_msg 留印给前端识别
    try:
        stats = _SLOW_FILL_DISPATCH[vendor_id](vendor, day)
        rows = int(stats.get("updated") or 0)
        logs_n = int(stats.get("logs") or 0)
        models_n = int(stats.get("models") or 0)
        log.info(f"[{vendor_id} slow-fill] run_id={run_id} {stats}")
        # 上游没返回详细日志 (logs=0 → 上游账单 / raw log 还没出 / LRU 已清); 也可能
        # logs 有但 models 全没匹配到 vendor_model_usage_daily (快路径还没建行), updated=0.
        # 两种都让前端能看到"跑了但空", 不要静默 success.
        if rows == 0:
            if logs_n == 0:
                empty_note = (f"上游慢路径返回空 (logs=0, models={models_n}): "
                              f"上游尚未出账或 raw log LRU 已清; "
                              f"cron 每天 03:00 / 23:00 会重试, retention 约 13 天")
            else:
                empty_note = (f"上游有 {logs_n} 条 raw log 但 0 行匹配到 vendor_model_usage_daily "
                              f"(快路径还没写当天的 model 行?), 拆分字段未补")
    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        log.error(f"[{vendor_id} slow-fill] run_id={run_id} {day} failed: {e}")

    with SessionLocal() as s:
        r = s.get(VendorIngestRun, run_id)
        if r is not None:
            r.status = "failed" if error_msg else "success"
            r.rows_upserted = rows
            r.error_msg = error_msg or empty_note
            r.finished_at = dt.datetime.now(dt.timezone.utc)
            s.commit()

    return run_id


# 重试退避: 5min → 30min → 1h. cron 半夜跑, 上游短抖动几分钟到几十分钟才恢复,
# 短退避会徒劳推飞书. 4 次 attempt 合计 ~1.5h, 早上还能及时看到告警.
RETRY_DELAYS = [300, 1800, 3600]


def run_ingest_with_retry(vendor_id: str, start: dt.date, end: dt.date,
                           trigger: str = "cron") -> int:
    """run_ingest 的重试 wrapper. 3 次失败后推飞书告警, 再抛出最后一次错.

    退避: 立刻 → 5min → 30min → 1h (合计 4 次尝试, 持续 ~1.5h).
    cron / xxl-job / 手动 retry 都应该走这个 wrapper, run_ingest 自己不带退避.
    """
    from ingest import alert

    last_err: Exception | None = None
    last_run_id: int | None = None
    for attempt, delay in enumerate([0] + RETRY_DELAYS, start=1):
        if delay:
            log.warning(f"[{vendor_id}] retry attempt {attempt}/{len(RETRY_DELAYS)+1} 等 {delay}s")
            import time as _time
            _time.sleep(delay)
        try:
            run_id = run_ingest(vendor_id, start, end, trigger=trigger, attempt=attempt)
            if attempt > 1:
                log.info(f"[{vendor_id}] attempt {attempt} 成功 run_id={run_id}")
            return run_id
        except Exception as e:
            last_err = e
            # 尝试从最近一次 run 拿 id (run_ingest 抛错前已经创建了 run 行)
            try:
                with SessionLocal() as _s:
                    last_run_id = _s.execute(
                        select(VendorIngestRun.id)
                        .where(VendorIngestRun.vendor_id == vendor_id)
                        .order_by(VendorIngestRun.id.desc()).limit(1)
                    ).scalar()
            except Exception:
                pass
            log.error(f"[{vendor_id}] attempt {attempt} 失败: {e}")

    # 全部 attempt 用尽 → 飞书告警
    alert.feishu_failure(
        vendor_id=vendor_id,
        start=start.isoformat(), end=end.isoformat(),
        error=str(last_err), run_id=last_run_id,
        attempt=len(RETRY_DELAYS) + 1,
    )
    raise last_err if last_err else RuntimeError("retry exhausted")


def mark_stale_running_failed(threshold_hours: float = 2.0,
                              run_ids: list[int] | None = None,
                              note: str = "") -> list[int]:
    """把 status='running' 超过 threshold_hours 的 run 标 failed, 返回受影响的 run_id.

    场景: backend 在 run_ingest 跑到一半被 kill / rebuild / OOM, 那行 run 永远卡 running,
    下次 cron 跑到同 vendor 时 uniq_running_per_vendor 索引拒 INSERT, 整天跳过.

    2 小时阈值: 单 vendor 最慢的 xhub 30 天 raw log 也就几分钟, 加上 4 次 retry 退避
    (5min + 30min + 1h) 满打满算 ~100min, 留些余量取 2h.

    run_ids 给定时只处理指定 run (agent 定点清僵尸用), 且**不校验时长** —
    调用方 (agent) 已确认该 run 是僵尸; 时长过滤只在全表扫描模式生效.
    """
    if not note:
        note = f"[zombie cleanup] status=running for > {threshold_hours}h, marked failed"
    affected: list[int] = []
    try:
        with SessionLocal() as s:
            q = select(VendorIngestRun.id, VendorIngestRun.started_at).where(
                VendorIngestRun.status == "running")
            if run_ids is not None:
                if not run_ids:
                    return []
                q = q.where(VendorIngestRun.id.in_(run_ids))
            rows = s.execute(q).all()
            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=threshold_hours)
            for run_id, started_at in rows:
                # run_ids 定点模式不校验时长 (调用方已判定); 全表模式按阈值过滤
                if run_ids is None:
                    ts = started_at if started_at.tzinfo else started_at.replace(tzinfo=dt.timezone.utc)
                    if ts >= cutoff:
                        continue
                s.execute(text("""
                    UPDATE vendor_ingest_run
                    SET status='failed',
                        error_msg=COALESCE(error_msg, '') || '\n' || :note,
                        finished_at=now()
                    WHERE id = :rid AND status='running'
                """), {"note": note, "rid": run_id})
                affected.append(run_id)
            s.commit()
        if affected:
            log.warning(f"[zombie] marked {len(affected)} stale 'running' runs failed: {affected}")
    except Exception as e:
        log.error(f"[zombie] mark_stale_running_failed error: {e}")
    return affected

