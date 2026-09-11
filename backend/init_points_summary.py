"""积分预聚合表初始化脚本

从原始表中按 ID 分批提取数据并构建预聚合表：
1. 按 ID 分批查询（避免在原始表建立时间索引）
2. 内存聚合 + 定期批量写入（减少数据库压力）
3. 进度显示

使用方法：
    # 全量初始化（按 ID 分批，每 5000 条查询一次，每 50000 条写入一次）
    python init_points_summary.py
"""
import argparse
import json
import logging
import os
import sys
from datetime import date, timedelta
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

load_dotenv()

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('init_points_summary.log')
    ]
)
logger = logging.getLogger(__name__)

# MySQL 数据库连接 (凭据只走环境变量, 不写默认值)
MYSQL_URL = os.environ.get("POINTS_DB_URL") or (
    "mysql+pymysql://%(user)s:%(password)s@%(host)s:%(port)s/%(name)s?charset=utf8mb4"
    % {
        "user": os.environ.get("POINTS_DB_USER", "root"),
        "password": os.environ.get("POINTS_DB_PASSWORD", ""),
        "host": os.environ.get("POINTS_DB_HOST", "127.0.0.1"),
        "port": os.environ.get("POINTS_DB_PORT", "3306"),
        "name": os.environ.get("POINTS_DB_NAME", "points"),
    }
)

# 每批处理的记录数（可根据服务器性能调整）
BATCH_SIZE = 5000  # 每批5000条，避免一次性加载太多


def create_engine_and_session():
    """创建数据库引擎和会话"""
    engine = create_engine(
        MYSQL_URL,
        pool_pre_ping=True,
        pool_size=2,  # 初始化时用小连接池
        max_overflow=1,
        pool_recycle=300,
        # 增加超时时间，避免大批量查询超时
        connect_args={"connect_timeout": 60}
    )
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    return engine, SessionLocal


def create_summary_table(session):
    """创建预聚合表（如果不存在）"""
    logger.info("检查/创建预聚合表 point_daily_summary...")

    create_table_sql = text("""
        CREATE TABLE IF NOT EXISTS point_daily_summary (
            usage_date DATE NOT NULL COMMENT '使用日期',
            biz_type INT NOT NULL COMMENT '业务类型',
            biz_sub_type VARCHAR(100) NOT NULL COMMENT '子类型',

            total_points BIGINT NOT NULL DEFAULT 0 COMMENT '总积分',
            record_count INT NOT NULL DEFAULT 0 COMMENT '记录数',

            prompt_tokens BIGINT DEFAULT 0 COMMENT '输入token',
            completion_tokens BIGINT DEFAULT 0 COMMENT '输出token',
            cache_read_tokens BIGINT DEFAULT 0 COMMENT '缓存读token',
            cache_write_tokens BIGINT DEFAULT 0 COMMENT '缓存写token',

            last_updated TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

            PRIMARY KEY (usage_date, biz_type, biz_sub_type),
            INDEX idx_usage_date (usage_date),
            INDEX idx_biz_type (biz_type)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='积分按日聚合表'
    """)

    session.execute(create_table_sql)
    session.commit()
    logger.info("✓ 预聚合表已就绪")


def parse_calculation_input(calc_input_str: str | None) -> dict:
    """解析 calculation_input JSON 字段

    支持两种格式：
    1. 旧格式（四月之前）：驼峰命名 promptTokens, promptTokensDetails
    2. 新格式（四月之后）：下划线命名 prompt_tokens, prompt_tokens_details
    """
    default_result = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }

    if not calc_input_str:
        return default_result

    try:
        if isinstance(calc_input_str, dict):
            calc_input = calc_input_str
        elif isinstance(calc_input_str, str):
            calc_input = json.loads(calc_input_str)
        else:
            return default_result

        usage_list = calc_input.get("usage", [])
        if not isinstance(usage_list, list):
            return default_result

        prompt_tokens = 0
        completion_tokens = 0
        cache_read_tokens = 0
        cache_write_tokens = 0

        for usage in usage_list:
            if not isinstance(usage, dict):
                continue

            # 兼容两种格式：驼峰命名（旧）和下划线命名（新）
            prompt_tokens += usage.get("promptTokens", 0) or usage.get("prompt_tokens", 0) or 0
            completion_tokens += usage.get("completionTokens", 0) or usage.get("completion_tokens", 0) or 0

            # 缓存读和缓存写 token 从 promptTokensDetails 或 prompt_tokens_details 获取
            prompt_details = usage.get("promptTokensDetails", {})
            if not prompt_details:
                prompt_details = usage.get("prompt_tokens_details", {})

            if isinstance(prompt_details, dict):
                # 旧格式：cachedTokens, cacheCreationTokens
                # 新格式：cached_tokens, cache_creation_tokens
                cache_read_tokens += (
                    prompt_details.get("cachedTokens", 0) or
                    prompt_details.get("cached_tokens", 0) or
                    0
                )
                cache_write_tokens += (
                    prompt_details.get("cacheCreationTokens", 0) or
                    prompt_details.get("cache_creation_tokens", 0) or
                    0
                )

        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
        }
    except Exception as e:
        logger.debug(f"解析 calculation_input 失败: {e}")
        return default_result


def aggregate_batch_by_id(session, start_id: int, end_id: int, aggregated: dict) -> int:
    """按 ID 范围查询并聚合数据到内存中的 aggregated 字典

    Args:
        session: 数据库会话
        start_id: 起始 ID（包含）
        end_id: 结束 ID（包含）
        aggregated: 聚合结果字典，格式 {(date, biz_type, biz_sub_type): {points, tokens, count}}

    Returns:
        处理的原始记录数
    """
    # 查询原始数据（按 ID 分批）
    query_sql = text("""
        SELECT
            DATE(create_time) as usage_date,
            biz_type,
            biz_sub_type,
            actual_amount,
            calculation_input
        FROM point_consumption_record
        WHERE id >= :start_id
          AND id <= :end_id
          AND status = 'CONFIRMED'
          AND is_deleted = 0
    """)

    records = session.execute(
        query_sql,
        {"start_id": start_id, "end_id": end_id}
    ).fetchall()

    if not records:
        return 0

    # 在内存中聚合
    for record in records:
        usage_date = str(record[0])
        biz_type = int(record[1])
        biz_sub_type = record[2] or ""
        points = int(record[3] or 0)
        calc_input = record[4]

        tokens = parse_calculation_input(calc_input)

        key = (usage_date, biz_type, biz_sub_type)
        if key not in aggregated:
            aggregated[key] = {
                "total_points": 0,
                "record_count": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
            }

        agg = aggregated[key]
        agg["total_points"] += points
        agg["record_count"] += 1
        agg["prompt_tokens"] += tokens["prompt_tokens"]
        agg["completion_tokens"] += tokens["completion_tokens"]
        agg["cache_read_tokens"] += tokens["cache_read_tokens"]
        agg["cache_write_tokens"] += tokens["cache_write_tokens"]

    return len(records)


def flush_aggregated_to_db(session, aggregated: dict):
    """将聚合结果批量插入到数据库

    Args:
        session: 数据库会话
        aggregated: 聚合结果字典
    """
    if not aggregated:
        return

    insert_sql = text("""
        INSERT INTO point_daily_summary (
            usage_date, biz_type, biz_sub_type,
            total_points, record_count,
            prompt_tokens, completion_tokens,
            cache_read_tokens, cache_write_tokens
        ) VALUES (
            :usage_date, :biz_type, :biz_sub_type,
            :total_points, :record_count,
            :prompt_tokens, :completion_tokens,
            :cache_read_tokens, :cache_write_tokens
        )
        ON DUPLICATE KEY UPDATE
            total_points = total_points + VALUES(total_points),
            record_count = record_count + VALUES(record_count),
            prompt_tokens = prompt_tokens + VALUES(prompt_tokens),
            completion_tokens = completion_tokens + VALUES(completion_tokens),
            cache_read_tokens = cache_read_tokens + VALUES(cache_read_tokens),
            cache_write_tokens = cache_write_tokens + VALUES(cache_write_tokens)
    """)

    batch_data = []
    for (usage_date, biz_type, biz_sub_type), agg in aggregated.items():
        batch_data.append({
            "usage_date": usage_date,
            "biz_type": biz_type,
            "biz_sub_type": biz_sub_type,
            **agg
        })

    session.execute(insert_sql, batch_data)
    session.commit()


def aggregate_batch(session, start_date: str, end_date: str) -> int:
    """按日期范围增量更新 point_daily_summary（供定时任务调用）

    与全量初始化的 aggregate_batch_by_id 不同，本函数：
    - 按 create_time 范围（>= 当天零点 AND < 次日零点）查询，走 create_time 索引，秒级完成
    - 一次性拉完日期内所有记录再聚合（昨天的量通常远小于全量）
    - 幂等写入：ON DUPLICATE KEY UPDATE 用 = VALUES(...)，不累加，
      重复调用同一天结果不变

    Args:
        session: 数据库会话
        start_date: 开始日期字符串，格式 'YYYY-MM-DD'（包含）
        end_date:   结束日期字符串，格式 'YYYY-MM-DD'（包含）

    Returns:
        写入/更新的聚合行数（不是原始记录数）
    """
    logger.info(f"aggregate_batch: 查询 {start_date} ~ {end_date} 的积分记录...")

    # 用 >= start_day 00:00:00 AND < (end_day+1) 00:00:00 代替 DATE() 函数包裹，
    # 让查询走 idx_create_time_status 索引，避免全表扫描。
    start_dt = f"{start_date} 00:00:00"
    end_dt = f"{(date.fromisoformat(end_date) + timedelta(days=1)).isoformat()} 00:00:00"

    query_sql = text("""
        SELECT
            DATE(create_time) AS usage_date,
            biz_type,
            biz_sub_type,
            actual_amount,
            calculation_input
        FROM point_consumption_record
        WHERE create_time >= :start_dt
          AND create_time <  :end_dt
          AND status = 'CONFIRMED'
          AND is_deleted = 0
    """)

    records = session.execute(
        query_sql,
        {"start_dt": start_dt, "end_dt": end_dt}
    ).fetchall()

    if not records:
        logger.info(f"aggregate_batch: {start_date} ~ {end_date} 无记录，跳过写库")
        return 0

    logger.info(f"aggregate_batch: 查到 {len(records)} 条原始记录，开始内存聚合...")

    # 内存聚合，逻辑与 aggregate_batch_by_id 保持一致
    aggregated: dict[tuple, dict] = {}
    for record in records:
        usage_date = str(record[0])
        biz_type = int(record[1])
        biz_sub_type = record[2] or ""
        points = int(record[3] or 0)
        tokens = parse_calculation_input(record[4])

        key = (usage_date, biz_type, biz_sub_type)
        if key not in aggregated:
            aggregated[key] = {
                "total_points": 0,
                "record_count": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
            }
        agg = aggregated[key]
        agg["total_points"] += points
        agg["record_count"] += 1
        agg["prompt_tokens"] += tokens["prompt_tokens"]
        agg["completion_tokens"] += tokens["completion_tokens"]
        agg["cache_read_tokens"] += tokens["cache_read_tokens"]
        agg["cache_write_tokens"] += tokens["cache_write_tokens"]

    logger.info(f"aggregate_batch: 聚合完成 → {len(aggregated)} 条，写入数据库...")

    # 幂等 upsert：= VALUES(...)，不是 += VALUES(...)
    # 保证重复调用同一天结果不变，与 flush_aggregated_to_db 的累加语义不同
    upsert_sql = text("""
        INSERT INTO point_daily_summary (
            usage_date, biz_type, biz_sub_type,
            total_points, record_count,
            prompt_tokens, completion_tokens,
            cache_read_tokens, cache_write_tokens
        ) VALUES (
            :usage_date, :biz_type, :biz_sub_type,
            :total_points, :record_count,
            :prompt_tokens, :completion_tokens,
            :cache_read_tokens, :cache_write_tokens
        )
        ON DUPLICATE KEY UPDATE
            total_points       = VALUES(total_points),
            record_count       = VALUES(record_count),
            prompt_tokens      = VALUES(prompt_tokens),
            completion_tokens  = VALUES(completion_tokens),
            cache_read_tokens  = VALUES(cache_read_tokens),
            cache_write_tokens = VALUES(cache_write_tokens)
    """)

    batch_data = [
        {"usage_date": usage_date, "biz_type": biz_type, "biz_sub_type": biz_sub_type, **agg}
        for (usage_date, biz_type, biz_sub_type), agg in aggregated.items()
    ]
    session.execute(upsert_sql, batch_data)
    session.commit()

    logger.info(f"aggregate_batch: {start_date} ~ {end_date} 写入完成 → {len(aggregated)} 条聚合记录")
    return len(aggregated)



def get_id_range(session):
    """获取原始表的 ID 范围"""
    query = text("""
        SELECT
            MIN(id) as min_id,
            MAX(id) as max_id,
            COUNT(*) as total_records
        FROM point_consumption_record
        WHERE status = 'CONFIRMED' AND is_deleted = 0
    """)

    result = session.execute(query).fetchone()
    return result[0], result[1], result[2]


def init_mode(session):
    """首次全量初始化 - 按 ID 分批查询"""
    logger.info("=" * 60)
    logger.info("模式：全量初始化（按 ID 分批）")
    logger.info("=" * 60)

    # 获取 ID 范围
    min_id, max_id, total_records = get_id_range(session)

    if min_id is None or max_id is None:
        logger.error("❌ 原始表没有数据")
        return

    logger.info(f"原始表 ID 范围: {min_id} ~ {max_id}")
    logger.info(f"原始记录总数: {total_records:,}")
    logger.info(f"批次大小: {BATCH_SIZE}")

    # 创建预聚合表
    create_summary_table(session)

    # 清空预聚合表（全量初始化时）
    logger.info("清空预聚合表...")
    session.execute(text("TRUNCATE TABLE point_daily_summary"))
    session.commit()

    # 按 ID 分批处理
    current_id = min_id
    total_processed = 0
    batch_num = 0

    # 聚合字典，累积多个批次的数据
    aggregated = {}

    while current_id <= max_id:
        batch_num += 1
        end_id = min(current_id + BATCH_SIZE - 1, max_id)

        logger.info(f"\n批次 {batch_num}: ID {current_id} ~ {end_id}")

        # 查询并聚合到内存
        processed = aggregate_batch_by_id(session, current_id, end_id, aggregated)
        total_processed += processed

        logger.info(f"  ✓ 处理 {processed} 条记录，累计 {total_processed}/{total_records}")

        # 每 10 个批次（即每 50000 条记录）写入一次数据库
        if batch_num % 10 == 0 or end_id >= max_id:
            logger.info(f"  → 写入数据库... ({len(aggregated)} 条聚合记录)")
            flush_aggregated_to_db(session, aggregated)
            aggregated.clear()  # 清空已写入的数据
            logger.info(f"  ✓ 已写入数据库")

        # 移到下一批
        current_id = end_id + 1

    logger.info("\n" + "=" * 60)
    logger.info(f"✅ 初始化完成！")
    logger.info(f"   处理原始记录: {total_processed:,}")
    logger.info(f"   总批次数: {batch_num}")
    logger.info("=" * 60)


def delete_summary_range(session, start_date: str, end_date: str) -> int:
    """删除 point_daily_summary 中指定日期范围的聚合行

    为什么需要：aggregate_batch 是 upsert（= VALUES(...)），只会覆盖"这次查到的"
    (date, biz_type, biz_sub_type) 组合。如果某组合原本有数据、后来原始记录被
    软删除（is_deleted=1）或改了状态，这次查不到它 → 旧行留在表里变成脏数据。
    手动补数时先删掉整个日期范围再重建，语义才是真正的"重建"。

    Returns:
        删除的行数
    """
    result = session.execute(
        text("""
            DELETE FROM point_daily_summary
            WHERE usage_date >= :start_date AND usage_date <= :end_date
        """),
        {"start_date": start_date, "end_date": end_date},
    )
    session.commit()
    return result.rowcount


def update_mode(session, start_date: str, end_date: str) -> None:
    """按日期范围手动重建 point_daily_summary（幂等，可重复执行）

    逐天处理，每天先删后聚合：
    - 逐天而非一次性查整个范围 → 避免长跨度时一次性把几百万行拉进内存
    - 先删后插 → 清掉原始记录被软删除后残留的脏行（见 delete_summary_range）

    注意：单天处理期间该天数据短暂缺失（毫秒级），前端此刻查这天会看到 0。
    手动补数是运维操作，建议低峰期执行。

    Args:
        session: 数据库会话
        start_date: 起始日期 'YYYY-MM-DD'（包含）
        end_date:   结束日期 'YYYY-MM-DD'（包含）
    """
    logger.info("=" * 60)
    if start_date == end_date:
        logger.info(f"模式：手动更新单天 — {start_date}")
    else:
        logger.info(f"模式：手动更新日期范围 — {start_date} ~ {end_date}")
    logger.info("=" * 60)

    # 确保表存在（可能是在全新库上直接跑 --date）
    create_summary_table(session)

    d_start = date.fromisoformat(start_date)
    d_end = date.fromisoformat(end_date)
    total_days = (d_end - d_start).days + 1

    total_rows = 0
    empty_days = []

    for i in range(total_days):
        day = (d_start + timedelta(days=i)).isoformat()
        logger.info(f"\n[{i + 1}/{total_days}] 处理 {day} ...")

        deleted = delete_summary_range(session, day, day)
        if deleted:
            logger.info(f"  · 清掉旧聚合行 {deleted} 条")

        rows = aggregate_batch(session, day, day)
        total_rows += rows

        if rows == 0:
            empty_days.append(day)
            logger.warning(f"  ⚠️  {day} 原始表无 CONFIRMED 记录，该天聚合为空")
        else:
            logger.info(f"  ✓ {day} → {rows} 条聚合记录")

    logger.info("\n" + "=" * 60)
    logger.info("✅ 更新完成！")
    logger.info(f"   日期范围: {start_date} ~ {end_date}（{total_days} 天）")
    logger.info(f"   聚合行数: {total_rows}")
    if empty_days:
        logger.info(f"   空数据天: {len(empty_days)} 天 → {', '.join(empty_days)}")
    logger.info("=" * 60)
    logger.info("提示：point_daily_summary 更新后，需同步刷新每日总计表：")
    logger.info(f"      python init_points_daily_total.py --start {start_date} --end {end_date}")


def _parse_date(value: str, flag: str) -> date:
    """校验并解析 YYYY-MM-DD，失败抛 argparse 能识别的异常"""
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{flag} 日期格式错误: {value!r}，需要 YYYY-MM-DD")


def main():
    parser = argparse.ArgumentParser(
        description="积分明细聚合表 point_daily_summary 初始化 / 手动按日期更新",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # 全量初始化（按 ID 分批扫全表，会 TRUNCATE 重建）
  python init_points_summary.py

  # 手动重建单天
  python init_points_summary.py --date 2026-07-20

  # 手动重建日期范围（逐天处理）
  python init_points_summary.py --start 2026-07-01 --end 2026-07-20
""",
    )
    parser.add_argument("--date", type=str, help="重建指定单天 (YYYY-MM-DD)")
    parser.add_argument("--start", type=str, help="重建范围起始日期 (YYYY-MM-DD)，需配合 --end")
    parser.add_argument("--end", type=str, help="重建范围结束日期 (YYYY-MM-DD)，需配合 --start")
    args = parser.parse_args()

    # 参数组合校验
    if args.date and (args.start or args.end):
        parser.error("--date 不能与 --start/--end 同时使用")
    if bool(args.start) != bool(args.end):
        parser.error("--start 和 --end 必须同时提供")

    start_date = end_date = None
    if args.date:
        _parse_date(args.date, "--date")
        start_date = end_date = args.date
    elif args.start:
        d_start = _parse_date(args.start, "--start")
        d_end = _parse_date(args.end, "--end")
        if d_start > d_end:
            parser.error(f"--start ({args.start}) 不能晚于 --end ({args.end})")
        start_date, end_date = args.start, args.end

    try:
        engine, SessionLocal = create_engine_and_session()
        session = SessionLocal()

        if start_date:
            update_mode(session, start_date, end_date)
        else:
            init_mode(session)

        session.close()
        engine.dispose()
        return 0

    except argparse.ArgumentTypeError as e:
        logger.error(f"❌ {e}")
        return 1
    except Exception as e:
        logger.exception(f"❌ 执行失败: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
