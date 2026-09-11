"""积分每日总计表初始化脚本

创建 point_usage_daily_total 表，包含每天的总积分和四种 token 总和。
该表从 point_daily_summary 聚合而来，用于前端的总积分展示和五张趋势图。

表结构：
- usage_date: 日期（主键）
- total_points: 当天总积分
- prompt_tokens: 当天输入 token 总和
- completion_tokens: 当天输出 token 总和
- cache_read_tokens: 当天缓存读 token 总和
- cache_write_tokens: 当天缓存写 token 总和

使用方法：
    # 全量初始化（从 point_daily_summary 聚合）
    python init_points_daily_total.py

    # 增量更新指定日期
    python init_points_daily_total.py --date 2024-01-15
"""
import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta

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
        logging.FileHandler('init_points_daily_total.log')
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


def create_engine_and_session():
    """创建数据库引擎和会话"""
    engine = create_engine(
        MYSQL_URL,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=1,
        pool_recycle=300,
        connect_args={"connect_timeout": 60}
    )
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    return engine, SessionLocal


def create_daily_total_table(session):
    """创建每日总计表（如果不存在）"""
    logger.info("检查/创建每日总计表 point_usage_daily_total...")

    create_table_sql = text("""
        CREATE TABLE IF NOT EXISTS point_usage_daily_total (
            usage_date DATE NOT NULL COMMENT '使用日期',

            total_points BIGINT NOT NULL DEFAULT 0 COMMENT '当天总积分',

            prompt_tokens BIGINT DEFAULT 0 COMMENT '当天输入token总和',
            completion_tokens BIGINT DEFAULT 0 COMMENT '当天输出token总和',
            cache_read_tokens BIGINT DEFAULT 0 COMMENT '当天缓存读token总和',
            cache_write_tokens BIGINT DEFAULT 0 COMMENT '当天缓存写token总和',

            record_count INT NOT NULL DEFAULT 0 COMMENT '当天记录数（来自summary表的行数）',
            last_updated TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

            PRIMARY KEY (usage_date),
            INDEX idx_usage_date (usage_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='积分每日总计表（用于前端总积分和趋势图）'
    """)

    session.execute(create_table_sql)
    session.commit()
    logger.info("✓ 每日总计表已就绪")


def aggregate_from_summary(session, start_date: str = None, end_date: str = None) -> int:
    """从 point_daily_summary 聚合到 point_usage_daily_total

    Args:
        session: 数据库会话
        start_date: 起始日期（包含），格式 'YYYY-MM-DD'，None 表示全部
        end_date: 结束日期（包含），格式 'YYYY-MM-DD'，None 表示全部

    Returns:
        插入/更新的记录数
    """
    # 构建日期过滤条件
    date_filter = ""
    params = {}
    if start_date and end_date:
        date_filter = "WHERE usage_date >= :start_date AND usage_date <= :end_date"
        params = {"start_date": start_date, "end_date": end_date}
    elif start_date:
        date_filter = "WHERE usage_date >= :start_date"
        params = {"start_date": start_date}
    elif end_date:
        date_filter = "WHERE usage_date <= :end_date"
        params = {"end_date": end_date}

    # 从 point_daily_summary 按日期聚合
    aggregate_sql = text(f"""
        INSERT INTO point_usage_daily_total (
            usage_date,
            total_points,
            prompt_tokens,
            completion_tokens,
            cache_read_tokens,
            cache_write_tokens,
            record_count
        )
        SELECT
            usage_date,
            SUM(total_points) as total_points,
            SUM(prompt_tokens) as prompt_tokens,
            SUM(completion_tokens) as completion_tokens,
            SUM(cache_read_tokens) as cache_read_tokens,
            SUM(cache_write_tokens) as cache_write_tokens,
            COUNT(*) as record_count
        FROM point_daily_summary
        {date_filter}
        GROUP BY usage_date
        ON DUPLICATE KEY UPDATE
            total_points = VALUES(total_points),
            prompt_tokens = VALUES(prompt_tokens),
            completion_tokens = VALUES(completion_tokens),
            cache_read_tokens = VALUES(cache_read_tokens),
            cache_write_tokens = VALUES(cache_write_tokens),
            record_count = VALUES(record_count)
    """)

    result = session.execute(aggregate_sql, params)
    session.commit()

    return result.rowcount


def init_full(session):
    """全量初始化 - 从 point_daily_summary 聚合所有数据"""
    logger.info("=" * 60)
    logger.info("模式：全量初始化（从 point_daily_summary 聚合）")
    logger.info("=" * 60)

    # 检查 point_daily_summary 是否存在
    check_table_query = text("""
        SELECT COUNT(*) as cnt
        FROM information_schema.tables
        WHERE table_schema = DATABASE()
          AND table_name = 'point_daily_summary'
    """)
    table_exists = session.execute(check_table_query).scalar()

    if not table_exists:
        logger.error("❌ point_daily_summary 表不存在，请先运行 init_points_summary.py")
        return

    # 获取 point_daily_summary 的日期范围
    range_query = text("""
        SELECT
            MIN(usage_date) as min_date,
            MAX(usage_date) as max_date,
            COUNT(DISTINCT usage_date) as days
        FROM point_daily_summary
    """)
    result = session.execute(range_query).fetchone()
    min_date, max_date, days = result[0], result[1], result[2]

    if not min_date or not max_date:
        logger.error("❌ point_daily_summary 表没有数据")
        return

    logger.info(f"point_daily_summary 日期范围: {min_date} ~ {max_date} ({days} 天)")

    # 创建每日总计表
    create_daily_total_table(session)

    # 清空每日总计表（全量初始化时）
    logger.info("清空每日总计表...")
    session.execute(text("TRUNCATE TABLE point_usage_daily_total"))
    session.commit()

    # 聚合所有数据
    logger.info("从 point_daily_summary 聚合数据...")
    inserted = aggregate_from_summary(session)

    logger.info("\n" + "=" * 60)
    logger.info(f"✅ 初始化完成！")
    logger.info(f"   日期范围: {min_date} ~ {max_date}")
    logger.info(f"   插入记录: {inserted} 条")
    logger.info("=" * 60)


def update_date_range(session, start_date: str, end_date: str):
    """更新指定日期范围的数据（幂等，可重复执行）

    从 point_daily_summary 按 usage_date 聚合到 point_usage_daily_total。
    单日更新就是 start_date == end_date 的特例。

    幂等性：aggregate_from_summary 用 ON DUPLICATE KEY UPDATE = VALUES(...)
    全量覆盖，重复跑同一区间结果不变。

    注意：如果某天在 point_daily_summary 里没有数据（例如源数据被清理），
    该天在 point_usage_daily_total 里的旧行不会被自动删除 —
    因为 GROUP BY 只产出有数据的天。需要彻底重建请跑全量模式（不带参数）。

    Args:
        session: 数据库会话
        start_date: 起始日期（包含），格式 'YYYY-MM-DD'
        end_date:   结束日期（包含），格式 'YYYY-MM-DD'
    """
    logger.info("=" * 60)
    if start_date == end_date:
        logger.info(f"模式：增量更新 - {start_date}")
    else:
        logger.info(f"模式：增量更新 - {start_date} ~ {end_date}")
    logger.info("=" * 60)

    # 检查 point_daily_summary 是否存在
    check_table_query = text("""
        SELECT COUNT(*) as cnt
        FROM information_schema.tables
        WHERE table_schema = DATABASE()
          AND table_name = 'point_daily_summary'
    """)
    table_exists = session.execute(check_table_query).scalar()

    if not table_exists:
        logger.error("❌ point_daily_summary 表不存在，请先运行 init_points_summary.py")
        return

    # 确保每日总计表存在
    create_daily_total_table(session)

    # 聚合指定区间
    logger.info(f"更新 {start_date} ~ {end_date} 的数据...")
    inserted = aggregate_from_summary(session, start_date, end_date)

    if inserted > 0:
        logger.info("=" * 60)
        logger.info(f"✅ 更新完成 - {start_date} ~ {end_date}（影响 {inserted} 行）")
        logger.info("=" * 60)
    else:
        logger.warning(f"⚠️  {start_date} ~ {end_date} 在 point_daily_summary 中没有数据")


# 兼容旧调用方式
def update_single_date(session, date_str: str):
    """更新单个日期的数据（幂等）— update_date_range 的单日包装"""
    update_date_range(session, date_str, date_str)


def _parse_date(s: str) -> str | None:
    """校验 YYYY-MM-DD 格式，合法返回原串，非法返回 None"""
    try:
        datetime.strptime(s, '%Y-%m-%d')
        return s
    except ValueError:
        return None


def main():
    parser = argparse.ArgumentParser(
        description='积分每日总计表初始化/更新（从 point_daily_summary 聚合）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # 全量初始化（TRUNCATE 后重建所有日期）
  python init_points_daily_total.py

  # 更新单天
  python init_points_daily_total.py --date 2026-07-20

  # 更新日期区间（含首尾）
  python init_points_daily_total.py --start 2026-07-01 --end 2026-07-20

  # 更新最近 N 天（不含今天）
  python init_points_daily_total.py --days 7
        """,
    )
    parser.add_argument('--date', type=str, help='更新单个日期 (YYYY-MM-DD)')
    parser.add_argument('--start', type=str, help='起始日期 (YYYY-MM-DD)，需配合 --end')
    parser.add_argument('--end', type=str, help='结束日期 (YYYY-MM-DD)，需配合 --start')
    parser.add_argument('--days', type=int, help='更新最近 N 天（不含今天）')
    args = parser.parse_args()

    # 参数互斥校验
    modes = [bool(args.date), bool(args.start or args.end), bool(args.days)]
    if sum(modes) > 1:
        logger.error("❌ --date / --start+--end / --days 三种模式互斥，只能用一个")
        return 1
    if bool(args.start) != bool(args.end):
        logger.error("❌ --start 和 --end 必须同时提供")
        return 1

    # 解析出目标区间
    start_date = end_date = None
    if args.date:
        start_date = end_date = _parse_date(args.date)
        if not start_date:
            logger.error(f"❌ 日期格式错误: {args.date}，需要 YYYY-MM-DD")
            return 1
    elif args.start:
        start_date, end_date = _parse_date(args.start), _parse_date(args.end)
        if not start_date:
            logger.error(f"❌ 日期格式错误: {args.start}，需要 YYYY-MM-DD")
            return 1
        if not end_date:
            logger.error(f"❌ 日期格式错误: {args.end}，需要 YYYY-MM-DD")
            return 1
        if start_date > end_date:
            logger.error(f"❌ 起始日期 {start_date} 晚于结束日期 {end_date}")
            return 1
    elif args.days:
        if args.days < 1:
            logger.error(f"❌ --days 必须 >= 1，收到 {args.days}")
            return 1
        today = date.today()
        start_date = str(today - timedelta(days=args.days))
        end_date = str(today - timedelta(days=1))

    try:
        engine, SessionLocal = create_engine_and_session()
        session = SessionLocal()

        if start_date:
            update_date_range(session, start_date, end_date)
        else:
            # 全量初始化
            init_full(session)

        session.close()
        engine.dispose()
        return 0

    except Exception as e:
        logger.exception(f"❌ 执行失败: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
