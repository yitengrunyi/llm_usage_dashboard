"""一次性 backfill: 把 vendor_model_usage_daily 历史 raw model 名归一化 + 合并同 (vendor, day, norm) 行.

跑两遍: 先 dry-run 列出会改的行数 + 哪些 model 会被合并; 确认后再加 --apply 真改.

为啥需要: utils.py 之前漏了 -unlimit / road2all+tencent adapter 之前没调 normalize, 历史
入库残留同模型多 raw 名 (e.g. gemini-3-flash + gemini-3-flash-preview 两行存了同一模型不同
渠道的 cost). 修完 adapter 后只对未来生效, 历史得这个脚本来洗.

合并规则:
- key = (vendor_id, usage_date, normalize_model_name(model))
- 如果 norm_name == raw model, 不动 (跳过)
- 如果 norm_name != raw model, 找同 key 是否已有 norm 行:
    - 已有 → SUM 数值列 UPDATE 进去, DELETE raw 行
    - 没有 → 直接 UPDATE raw 行的 model 字段成 norm_name
"""
import os
import sys
import argparse
_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND)

from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND, ".env"))

from sqlalchemy import create_engine, text
from utils import normalize_model_name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="不加这个 flag 仅 dry-run 报告; 加了才真改库")
    ap.add_argument("--days", type=int, default=365, help="只处理最近 N 天的数据 (默认 365)")
    args = ap.parse_args()

    dsn = (f"postgresql+psycopg2://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
           f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
           f"?options=-csearch_path%3Dllm_usage_dashboard,public")
    engine = create_engine(dsn)

    # 1. 先扫: 全表读 (vendor_id, usage_date, model), 算每行 norm_name
    print(f"[1/4] 扫表 (近 {args.days} 天)...")
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT vendor_id, usage_date, model
            FROM vendor_model_usage_daily
            WHERE usage_date >= CURRENT_DATE - :days
            ORDER BY vendor_id, usage_date, model
        """), {"days": args.days}).all()
    print(f"  扫到 {len(rows)} 行")

    # 2. 找 norm != raw 的行 (即名字会变的)
    will_rename = []
    for r in rows:
        norm = normalize_model_name(r.model or "")
        if norm and norm != r.model:
            will_rename.append((r.vendor_id, r.usage_date, r.model, norm))
    print(f"[2/4] {len(will_rename)} 行 raw 名 != norm 名")

    # 3. 模拟应用: 逐行决定 update or merge
    print(f"[3/4] 模拟应用...")
    # (vendor_id, usage_date, norm_name) → 是否已存在
    norm_keys = set()  # 已有 norm 行的 key
    for r in rows:
        norm = normalize_model_name(r.model or "") or r.model
        if norm == r.model:
            norm_keys.add((r.vendor_id, r.usage_date, norm))

    plan_update = []  # 直接改名 (norm key 不冲突)
    plan_merge = []   # 合并到已有 norm 行 (会先 DELETE 旧 + UPDATE 新行)
    for vid, day, raw, norm in will_rename:
        key = (vid, day, norm)
        if key in norm_keys:
            plan_merge.append((vid, day, raw, norm))
        else:
            plan_update.append((vid, day, raw, norm))
            norm_keys.add(key)  # 改完之后这个 norm key 就存在了

    print(f"  纯改名 (无冲突): {len(plan_update)} 行")
    print(f"  需合并 (已有 norm 行, 数值要 SUM): {len(plan_merge)} 行")

    # 列合并细节 — 担心 preview 价格和 GA 不同, 合并会把 cost 加错
    if plan_merge:
        print(f"\n[3.5] 需合并的 {len(plan_merge)} 行明细 (检查 raw vs norm 是否真同模型):")
        # 拉这些 row 的 cost 看下
        with engine.connect() as conn:
            for vid, day, raw, norm in plan_merge[:50]:
                pair = conn.execute(text("""
                    SELECT model, cost_cny, request_count, total_tokens
                    FROM vendor_model_usage_daily
                    WHERE vendor_id = :vid AND usage_date = :day AND model IN (:raw, :norm)
                    ORDER BY model
                """), {"vid": vid, "day": day, "raw": raw, "norm": norm}).all()
                if len(pair) == 2:
                    a = pair[0]; b = pair[1]
                    print(f"  {vid} {day}  {a.model:<35} ¥{float(a.cost_cny or 0):>8.2f}  +  {b.model:<35} ¥{float(b.cost_cny or 0):>8.2f}")

    # 4. 列出 top 影响 (按 model 维度, 看哪些 raw 会变怎样)
    from collections import Counter
    rename_counter = Counter()
    for vid, day, raw, norm in will_rename:
        rename_counter[(raw, norm)] += 1
    print(f"\n[4/4] 影响最大的 raw → norm (按行数):")
    for (raw, norm), n in rename_counter.most_common(20):
        print(f"  [{n:>5}] {raw:<45} → {norm}")

    if not args.apply:
        print(f"\n=== dry-run 结束, 加 --apply 才真改 ===")
        return

    # 真跑
    print(f"\n=== 开始 apply ({len(plan_update)} 改名 + {len(plan_merge)} 合并) ===")
    with engine.begin() as conn:
        # 先 merge: 把 raw 行数值加到 norm 行, 删 raw 行
        for vid, day, raw, norm in plan_merge:
            conn.execute(text("""
                UPDATE vendor_model_usage_daily AS dst
                SET request_count = COALESCE(dst.request_count, 0) + COALESCE(src.request_count, 0),
                    image_count   = COALESCE(dst.image_count, 0) + COALESCE(src.image_count, 0),
                    prompt_tokens = COALESCE(dst.prompt_tokens, 0) + COALESCE(src.prompt_tokens, 0),
                    completion_tokens = COALESCE(dst.completion_tokens, 0) + COALESCE(src.completion_tokens, 0),
                    cache_read_tokens = COALESCE(dst.cache_read_tokens, 0) + COALESCE(src.cache_read_tokens, 0),
                    cache_write_tokens = COALESCE(dst.cache_write_tokens, 0) + COALESCE(src.cache_write_tokens, 0),
                    total_tokens = COALESCE(dst.total_tokens, 0) + COALESCE(src.total_tokens, 0),
                    cost_native = COALESCE(dst.cost_native, 0) + COALESCE(src.cost_native, 0),
                    cost_usd    = COALESCE(dst.cost_usd, 0) + COALESCE(src.cost_usd, 0),
                    cost_cny    = COALESCE(dst.cost_cny, 0) + COALESCE(src.cost_cny, 0)
                FROM vendor_model_usage_daily AS src
                WHERE dst.vendor_id = :vid AND dst.usage_date = :day AND dst.model = :norm
                  AND src.vendor_id = :vid AND src.usage_date = :day AND src.model = :raw
            """), {"vid": vid, "day": day, "norm": norm, "raw": raw})
            conn.execute(text("""
                DELETE FROM vendor_model_usage_daily
                WHERE vendor_id = :vid AND usage_date = :day AND model = :raw
            """), {"vid": vid, "day": day, "raw": raw})

        # 再 update: 直接改名
        for vid, day, raw, norm in plan_update:
            conn.execute(text("""
                UPDATE vendor_model_usage_daily
                SET model = :norm
                WHERE vendor_id = :vid AND usage_date = :day AND model = :raw
            """), {"vid": vid, "day": day, "raw": raw, "norm": norm})

    print("=== apply 完成 ===")


if __name__ == "__main__":
    main()
