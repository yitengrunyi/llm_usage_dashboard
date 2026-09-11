"""tc-cloud adapter — 腾讯云 VOD AIGC 用量.

DescribeAigcUsageData 接口返"整段聚合", 每个 model 一个 list[5min 桶].
adapter 一天一次调用, billing 模块复用算金额.

cache 字段:
- prompt_tokens = input dimension (_input)
- completion_tokens = output dimension (_output)
- cache_read_tokens = cacheinput dimension (_cacheinput)  ← 新发现!
- cache_write_tokens = NULL (腾讯云没暴露 cache write)
- request_count = 取 input dimension 的 Count 之和 (其他 dim 的 Count 跟它一致)
- image_count = Image 类型的 Usage 求和

fetch_apikey_rows (额外): AigcType=TextDetail 返回逐请求明细, 每行带
ApiKey (脱敏, 如 "TgawD3m1****"), 按 (ApiKey, model) 聚合写 key 表.
聚合接口的 APIKey 过滤参数只认完整 key 值 (脱敏串过滤返回 0 条),
所以只能翻明细自行聚合 — 同 new-api 慢路径模式. 生图无 Detail 接口,
key 表只覆盖生文.
"""
from __future__ import annotations

import datetime as dt
import logging

from tc_api import fetch_usage, fetch_text_detail_day
from billing import load_pricing, parse_specification, _calc_text, _calc_image, tc_to_unified, calculate_billing
from utils import normalize_model_name
from ingest.adapters.base import ApiKeyRow, ModelRow, VendorAdapter, pack_input, pack_total

logger = logging.getLogger(__name__)


def _display_price_map() -> dict[str, dict]:
    """pricing.json text 段按归一化 display_name 反查单价 (元/1M token).

    同名多 SKU (标准档 / _272 长上下文档 / 200 高档) 取 input 价最低的标准档 —
    TextDetail 明细只有 display 模型名, 区分不了档位, 长上下文档按标准档近似
    (七月实测 _272 档用量占 gpt-5.4 总量 <0.5%, 误差可忽略).
    """
    out: dict[str, dict] = {}
    for key, cfg in load_pricing().get("text", {}).items():
        dn = normalize_model_name(cfg.get("display_name") or key) or key
        cur = out.get(dn)
        if cur is None or (cfg.get("input", 0) or 0) < (cur.get("input", 0) or 0):
            out[dn] = cfg
    return out


class TencentAdapter(VendorAdapter):
    vendor_id = "tencent"
    source = "tc_vod_describe_aigc"
    native_currency = "CNY"
    has_cache_detail = True  # 腾讯云能拿到 cache_read (无 cache_write)

    def fetch_one_day(self, day: dt.date) -> list[ModelRow]:
        start_iso = f"{day.isoformat()}T00:00:00+08:00"
        end_iso = f"{day.isoformat()}T23:59:59+08:00"

        text_raw = fetch_usage("Text", start_iso, end_iso) or []
        image_raw = fetch_usage("Image", start_iso, end_iso) or []

        # 复用 billing 模块算金额
        text_billing = calculate_billing(text_raw, "text")
        image_billing = calculate_billing(image_raw, "image")

        # 直接读 _calc_text 的 models 字典 — 已经拆好 input/output/cacheinput
        rows: list[ModelRow] = []

        # text + image 都先按归一化 model 名再聚合, 避免同模型多变体在 DB 残留
        text_merged: dict[str, dict] = {}
        for display_name, m in text_billing.get("models", {}).items():
            key = normalize_model_name(display_name) or display_name
            agg = text_merged.setdefault(key, {
                "input_t": 0, "output_t": 0, "cache_t": 0,
                "total_count": 0, "total_cost": 0.0,
            })
            agg["input_t"] += int(m.get("input", {}).get("tokens", 0) or 0)
            agg["output_t"] += int(m.get("output", {}).get("tokens", 0) or 0)
            agg["cache_t"] += int(m.get("cacheinput", {}).get("tokens", 0) or 0)
            agg["total_count"] += int(m.get("total_count") or 0)
            agg["total_cost"] += float(m.get("total_cost") or 0)

        for display_name, agg in text_merged.items():
            input_t = agg["input_t"]
            output_t = agg["output_t"]
            cache_t = agg["cache_t"]
            prompt_full = pack_input(input_t, cache_t, None)
            rows.append(ModelRow(
                model=display_name,
                prompt_tokens=prompt_full or None,
                completion_tokens=output_t or None,
                cache_read_tokens=cache_t,                  # 新补的字段
                cache_write_tokens=None,                    # 腾讯云上游没暴露
                total_tokens=pack_total(prompt_full, output_t) or None,
                request_count=agg["total_count"] or None,
                image_count=None,
                cost_native=agg["total_cost"],
            ))

        image_merged: dict[str, dict] = {}
        for display_name, m in image_billing.get("models", {}).items():
            key = normalize_model_name(display_name) or display_name
            agg = image_merged.setdefault(key, {
                "total_count": 0, "count": 0, "total_cost": 0.0,
            })
            agg["total_count"] += int(m.get("total_count") or 0)
            agg["count"] += int(m.get("count") or 0)
            agg["total_cost"] += float(m.get("total_cost") or 0)

        for display_name, agg in image_merged.items():
            rows.append(ModelRow(
                model=display_name,
                prompt_tokens=None, completion_tokens=None,
                cache_read_tokens=None, cache_write_tokens=None,
                total_tokens=None,
                request_count=agg["total_count"] or None,
                image_count=agg["count"] or None,
                cost_native=agg["total_cost"],
            ))

        return rows

    def fetch_apikey_rows(self, day: dt.date) -> list[ApiKeyRow]:
        """按 (ApiKey 脱敏值, model) 聚合 TextDetail 明细, 写 vendor_apikey_usage_daily.

        fetch_text_detail_day 任一 4h 窗口拉取失败会抛异常 → 这里返空 list,
        job.py 不动该天已有 key 行 (残缺数据不覆盖完整数据).
        上游偶尔抖动时宁可这天 key 表缺一天, 等下次 ingest 重写.
        """
        try:
            details = fetch_text_detail_day(day)
        except Exception as e:
            logger.warning("tencent %s TextDetail 拉取失败, key 拆分跳过: %s", day, e)
            return []

        prices = _display_price_map()
        grouped: dict[tuple[str, str], dict] = {}
        for r in details:
            key = (r.get("ApiKey") or "").strip()
            if not key:
                continue  # 没 ApiKey 的行不进 key 表
            model = normalize_model_name(r.get("Model") or "unknown") or "unknown"
            g = grouped.setdefault((key, model), {"noncache": 0, "cache": 0, "output": 0, "req": 0})
            i = int(r.get("InputTokens") or 0)
            c = int(r.get("CacheInputTokens") or 0)
            # InputTokens 是完整输入 (行级校验: In+Out=Total 且 Cache≤In 恒成立),
            # 拆成非缓存部分计价, 别再 pack_input 加一次 cache (会双算).
            g["noncache"] += max(0, i - c)
            g["cache"] += c
            g["output"] += int(r.get("OutputTokens") or 0)
            if r.get("StatusCode") == 200:
                g["req"] += 1  # 失败行(如502)不带用量, 聚合接口也不计, 对齐

        out: list[ApiKeyRow] = []
        for (key, model), g in grouped.items():
            p = prices.get(model)
            if p is None:
                logger.warning("tencent key 拆分遇到未定价模型 %s, 该组成本按 0", model)
                cost = 0.0
            else:
                cost = (g["noncache"] * (p.get("input") or 0)
                        + g["cache"] * (p.get("cacheinput") or 0)
                        + g["output"] * (p.get("output") or 0)) / 1_000_000
            prompt_full = pack_input(g["noncache"], g["cache"], None)
            out.append(ApiKeyRow(
                api_key=key,
                model=model,
                prompt_tokens=prompt_full,
                completion_tokens=g["output"],
                cache_read_tokens=g["cache"],
                cache_write_tokens=None,
                total_tokens=pack_total(prompt_full, g["output"]),
                request_count=g["req"],
                image_count=None,
                cost_native=round(cost, 6),
            ))
        return out
