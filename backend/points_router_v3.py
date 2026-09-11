"""积分查询 API 路由 V3（真正的异步并发版）

使用 aiomysql 异步驱动实现两张表的真正并发查询，避免 GIL 导致的串行执行。
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any

import aiomysql
from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api")

# MySQL 数据库连接配置 (内网地址/凭据一律走环境变量, 默认值只留本机占位)
MYSQL_CONFIG = {
    "host": os.environ.get("POINTS_DB_HOST", "127.0.0.1"),
    "port": int(os.environ.get("POINTS_DB_PORT", "3306")),
    "user": os.environ.get("POINTS_DB_USER", "root"),
    "password": os.environ.get("POINTS_DB_PASSWORD", ""),
    "db": os.environ.get("POINTS_DB_NAME", "points"),
    "charset": "utf8mb4",
    "autocommit": True,
}

logger = logging.getLogger(__name__)

# 表存在性检查缓存
_tables_verified = False

# 业务类型映射
BIZ_TYPE_MAP = {
    10: "AI转纪要",
    22: "翻译",
    130: "PaiPai问答",
}

# 子类型中文名映射（与 v2 相同）
SUB_TYPE_MAP = {
    "RECORD_CONVERT": "转纪要",
    "TRANSLATION": "翻译",
    "PAIPAI_FLASH": "PaiPai问答·Flash",
    "PAIPAI_THINK": "PaiPai问答·Think",
    "PAIPAI_PRO": "PaiPai问答·PRO",
    "AGENT_INVESTMENT_LOGIC": "投资逻辑",
    "AGENT_RESEARCH_OUTLINE": "调研大纲",
    "AGENT_PERFORMANCE_REVIEW": "业绩点评",
    "AGENT_OPINION_CHALLENGE": "观点Challenge",
    "AGENT_COMPARABLE_COMPANY": "可比公司",
    "AGENT_THEME_STOCK_SELECTION": "主题选股",
    "AGENT_QUANTITATIVE_ANALYSIS": "定量分析",
    "AGENT_THEME_FUND_SELECTION": "主题选基",
    "AGENT_STOCK_FUND_SELECTION": "个股选基",
    "AGENT_SEARCH_DOCUMENT": "搜文档",
    "AGENT_SEARCH_CHART": "搜图表",
    "AGENT_DRAW_CHART": "画图",
    "AGENT_BROWSER_PLUGIN": "浏览器插件",
    "AGENT_COMPANY_ONE_PAGER": "公司一页纸",
    "AGENT_INDUSTRY_ONE_PAGER": "行业一页纸",
    "AGENT_WRITE_REPORT": "写报告",
    "AGENT_ASK_IM": "ASK IM",
    "PAIPAI_WORK": "PaiWork",
    "EXPORT_FILE": "导出文件",
}


async def _verify_tables(pool: aiomysql.Pool) -> None:
    """确保预聚合表存在（只在首次请求时检查，之后缓存结果）"""
    global _tables_verified
    if _tables_verified:
        return

    async with pool.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("""
                SELECT
                    SUM(CASE WHEN table_name = 'point_usage_daily_total' THEN 1 ELSE 0 END) as has_total,
                    SUM(CASE WHEN table_name = 'point_daily_summary' THEN 1 ELSE 0 END) as has_summary
                FROM information_schema.tables
                WHERE table_schema = DATABASE()
                  AND table_name IN ('point_usage_daily_total', 'point_daily_summary')
            """)
            result = await cursor.fetchone()
            has_total = result[0] > 0
            has_summary = result[1] > 0

            if not has_total or not has_summary:
                missing = []
                if not has_total:
                    missing.append("point_usage_daily_total")
                if not has_summary:
                    missing.append("point_daily_summary")
                raise HTTPException(
                    status_code=500,
                    detail=f"预聚合表不存在: {', '.join(missing)}，请先运行初始化脚本"
                )

    _tables_verified = True
    logger.info("✓ 预聚合表验证通过（已缓存，后续请求跳过检查）")


# 全局连接池（FastAPI 启动时初始化）
_pool: aiomysql.Pool | None = None


async def get_pool() -> aiomysql.Pool:
    """获取或创建数据库连接池"""
    global _pool
    if _pool is None:
        _pool = await aiomysql.create_pool(
            minsize=5,
            maxsize=10,
            **MYSQL_CONFIG
        )
    return _pool


@router.get("/points")
async def get_points_data_v3(
    start: str = Query(..., description="起始时间 ISO 格式"),
    end: str = Query(..., description="结束时间 ISO 格式"),
) -> dict[str, Any]:
    """查询积分数据 V3（真正的异步并发版）

    使用 aiomysql 异步驱动，实现多张表的真正并发查询。
    """
    try:
        start_dt = datetime.fromisoformat(start.replace("+08:00", ""))
        end_dt = datetime.fromisoformat(end.replace("+08:00", ""))
    except ValueError as e:
        logger.error(f"时间格式错误: {e}, start={start}, end={end}")
        raise HTTPException(status_code=400, detail="时间格式错误，需 ISO 8601")

    if start_dt > end_dt:
        raise HTTPException(status_code=400, detail="起始时间不能晚于结束时间")

    pool = await get_pool()
    await _verify_tables(pool)

    logger.info(f"使用异步并发查询: start={start_dt.date()}, end={end_dt.date()}")

    query_start = time.time()

    try:
        # 真正的异步并发执行（无 GIL 限制）
        import asyncio
        total_data, detail_data, recharge_data, refund_data = await asyncio.gather(
            _query_total_table(pool, start_dt, end_dt),
            _query_summary_table(pool, start_dt, end_dt),
            _query_recharge_data(pool, start_dt, end_dt),
            _query_refund_data(pool, start_dt, end_dt),
        )

        total_elapsed = (time.time() - query_start) * 1000
        logger.info(f"[异步并发] 总耗时 {total_elapsed:.0f}ms")

        return {
            "total_points": total_data["total_points"],
            "daily": total_data["daily"],
            "daily_tokens": total_data["daily_tokens"],
            "by_parent_type": detail_data["by_parent_type"],
            "total_recharge": recharge_data["total_recharge"],
            "daily_recharge": recharge_data["daily_recharge"],
            "total_refund": refund_data["total_refund"],
            "daily_refund": refund_data["daily_refund"],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"查询积分数据失败: {e}")
        raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")


async def _query_total_table(pool: aiomysql.Pool, start_dt: datetime, end_dt: datetime) -> dict[str, Any]:
    """从 point_usage_daily_total 查询总积分和趋势图数据"""
    t0 = time.time()

    async with pool.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("""
                SELECT
                    usage_date,
                    total_points,
                    prompt_tokens,
                    completion_tokens,
                    cache_read_tokens,
                    cache_write_tokens
                FROM point_usage_daily_total
                WHERE usage_date >= DATE(%s)
                  AND usage_date <= DATE(%s)
                ORDER BY usage_date
            """, (start_dt, end_dt))

            records = await cursor.fetchall()

    logger.info(f"从 point_usage_daily_total 查询到 {len(records)} 条记录")
    logger.info(f"[异步并发] point_usage_daily_total 耗时 {(time.time() - t0)*1000:.0f}ms")

    # 计算总积分
    total_points = sum(int(record[1] or 0) for record in records)

    # 每日积分
    daily = [{"date": str(record[0]), "points": int(record[1] or 0)} for record in records]

    # 每日Token数据
    daily_tokens = {
        "input_tokens": [
            {"date": str(record[0]), "value": int(record[2] or 0)}
            for record in records
        ],
        "output_tokens": [
            {"date": str(record[0]), "value": int(record[3] or 0)}
            for record in records
        ],
        "cache_read_tokens": [
            {"date": str(record[0]), "value": int(record[4] or 0)}
            for record in records
        ],
        "cache_write_tokens": [
            {"date": str(record[0]), "value": int(record[5] or 0)}
            for record in records
        ],
    }

    return {
        "total_points": total_points,
        "daily": daily,
        "daily_tokens": daily_tokens,
    }


async def _query_summary_table(pool: aiomysql.Pool, start_dt: datetime, end_dt: datetime) -> dict[str, Any]:
    """从 point_daily_summary 查询积分明细（按父类型和子类型）"""
    t0 = time.time()

    async with pool.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("""
                SELECT
                    biz_type,
                    biz_sub_type,
                    SUM(total_points) as total_points,
                    SUM(prompt_tokens) as prompt_tokens,
                    SUM(completion_tokens) as completion_tokens,
                    SUM(cache_read_tokens) as cache_read_tokens,
                    SUM(cache_write_tokens) as cache_write_tokens
                FROM point_daily_summary
                WHERE usage_date >= DATE(%s)
                  AND usage_date <= DATE(%s)
                GROUP BY biz_type, biz_sub_type
                ORDER BY biz_type, total_points DESC
            """, (start_dt, end_dt))

            records = await cursor.fetchall()

    logger.info(f"从 point_daily_summary 查询到 {len(records)} 条聚合记录")
    logger.info(f"[异步并发] point_daily_summary 耗时 {(time.time() - t0)*1000:.0f}ms")

    # 按父类型聚合
    parent_map = {}

    for record in records:
        biz_type = int(record[0])
        biz_sub_type = record[1] or ""
        points = int(record[2] or 0)
        prompt_tokens = int(record[3] or 0)
        completion_tokens = int(record[4] or 0)
        cache_read_tokens = int(record[5] or 0)
        cache_write_tokens = int(record[6] or 0)

        biz_type_str = str(biz_type)

        # 初始化父类型
        if biz_type_str not in parent_map:
            parent_map[biz_type_str] = {
                "points": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "sub_types": {}
            }

        parent_data = parent_map[biz_type_str]
        parent_data["points"] += points
        parent_data["input_tokens"] += prompt_tokens
        parent_data["output_tokens"] += completion_tokens
        parent_data["cache_read_tokens"] += cache_read_tokens
        parent_data["cache_write_tokens"] += cache_write_tokens

        # 按子类型聚合
        if biz_sub_type:
            parent_data["sub_types"][biz_sub_type] = {
                "points": points,
                "input_tokens": prompt_tokens,
                "output_tokens": completion_tokens,
                "cache_read_tokens": cache_read_tokens,
                "cache_write_tokens": cache_write_tokens,
            }

    # 格式化返回结果
    by_parent_type = {}
    for biz_type_str, parent_data in sorted(
        parent_map.items(),
        key=lambda x: x[1]["points"],
        reverse=True
    ):
        sub_types = [
            {
                "name": SUB_TYPE_MAP.get(sub_type, sub_type),
                "code": sub_type,
                "points": data["points"],
                "input_tokens": data["input_tokens"],
                "output_tokens": data["output_tokens"],
                "cache_read_tokens": data["cache_read_tokens"],
                "cache_write_tokens": data["cache_write_tokens"],
            }
            for sub_type, data in sorted(
                parent_data["sub_types"].items(),
                key=lambda x: x[1]["points"],
                reverse=True
            )
        ]

        by_parent_type[biz_type_str] = {
            "points": parent_data["points"],
            "input_tokens": parent_data["input_tokens"],
            "output_tokens": parent_data["output_tokens"],
            "cache_read_tokens": parent_data["cache_read_tokens"],
            "cache_write_tokens": parent_data["cache_write_tokens"],
            "sub_types": sub_types,
        }

    return {"by_parent_type": by_parent_type}


async def _query_recharge_data(pool: aiomysql.Pool, start_dt: datetime, end_dt: datetime) -> dict[str, Any]:
    """从 payment_order 查询充值数据（总充值和每日充值趋势）"""
    t0 = time.time()

    async with pool.acquire() as conn:
        async with conn.cursor() as cursor:
            # 查询每日充值金额（按创建时间分组）
            await cursor.execute("""
                SELECT
                    DATE(create_time) as recharge_date,
                    SUM(amount) as daily_amount
                FROM payment_order
                WHERE create_time >= %s
                  AND create_time <= %s
                  AND state = 2
                GROUP BY DATE(create_time)
                ORDER BY recharge_date
            """, (start_dt, end_dt))

            records = await cursor.fetchall()

    logger.info(f"从 payment_order 查询到 {len(records)} 条记录")
    logger.info(f"[异步并发] payment_order 耗时 {(time.time() - t0)*1000:.0f}ms")

    # 计算总充值（单位：分）
    total_recharge = sum(int(record[1] or 0) for record in records)

    # 每日充值（单位：分）
    daily_recharge = [{"date": str(record[0]), "amount": int(record[1] or 0)} for record in records]

    return {
        "total_recharge": total_recharge,
        "daily_recharge": daily_recharge,
    }


async def _query_refund_data(pool: aiomysql.Pool, start_dt: datetime, end_dt: datetime) -> dict[str, Any]:
    """从 payment_refund_order 查询退款数据（总退款和每日退款趋势）"""
    t0 = time.time()

    async with pool.acquire() as conn:
        async with conn.cursor() as cursor:
            # 查询每日退款金额（按创建时间分组）
            await cursor.execute("""
                SELECT
                    DATE(create_time) as refund_date,
                    SUM(refund_amount) as daily_amount
                FROM payment_refund_order
                WHERE create_time >= %s
                  AND create_time <= %s
                  AND state = 2
                GROUP BY DATE(create_time)
                ORDER BY refund_date
            """, (start_dt, end_dt))

            records = await cursor.fetchall()

    logger.info(f"从 payment_refund_order 查询到 {len(records)} 条记录")
    logger.info(f"[异步并发] payment_refund_order 耗时 {(time.time() - t0)*1000:.0f}ms")

    # 计算总退款（单位：分）
    total_refund = sum(int(record[1] or 0) for record in records)

    # 每日退款（单位：分）
    daily_refund = [{"date": str(record[0]), "amount": int(record[1] or 0)} for record in records]

    return {
        "total_refund": total_refund,
        "daily_refund": daily_refund,
    }
