"""volcengine adapter — 火山方舟.

字段策略 (双源, 在 volcengine_client 内部已合好):
- GetInferenceUsage (ark 推理统计): per-model token / cache / request_count / image
                                    跟官方控制台一致, 含套餐内消耗 token
- ListAmortizedCostBillDaily (账单): per-model cost (按 token 计费部分)
                                     额外把套餐项 (Coding-Plan / 免费包) 作为独立 model 出 cost,
                                     token=None 区分

历史: 之前只用 Bill 算 token, 套餐内消耗被吞 (少 17%) + 调用次数靠 CSV 白名单卡死 6 模型.
GetInferenceUsage 是正解, 字段全 + 无白名单. CSV 整段已删.

按 API key 拆分 (vendor 详情页筛选, 写 vendor_apikey_usage_daily):
- token/请求数: GetInferenceUsage 的 ApikeyID×ModelName 双维分组, 一次调用 (fetch_key_usage)
- 费用: 账单 API 不支持按 key 拆 (实测 Filters ApikeyID 无效, SplitItemID 空) →
  按官方单价加权分摊: key 权重 = 非缓存输入×输入价 + 缓存命中×缓存价 + 输出×输出价,
  模型当日账单金额按权重比例分配, 合计与账单精确一致 (尾差归排序末行). 币种只影响
  比例无关紧要 (同模型同一价格条目). 无价模型退回 token 数加权.
- ApikeyID 为空的用量 (ep-xxx 接入点 / AuthToken 鉴权) 归到合成 key "(未关联Key)",
  保证 Σ(key) + 未关联 = 主表; sid 经 ListApiKeys 映射成可读名, 重名加 SID 尾缀.
- 套餐项 (volcengine 其它项) 无推理用量, 不参与分摊 — Σ(key 费用) 比总计少这部分, 属预期.
"""
from __future__ import annotations

import datetime as dt

from volcengine_client import fetch_key_usage, fetch_vendor_usage, list_api_keys
from ingest.adapters.base import ApiKeyRow, ModelRow, VendorAdapter
from utils import normalize_model_name

UNATTRIBUTED_KEY = "(未关联Key)"

# 分摊权重用的本地价格表, 模块级缓存 (长驻进程里价格更新后要重启才生效;
# 权重只影响分摊比例不影响合计, 陈旧一点无害)
_PRICING_CACHE: dict | None = None


def _cached_db_prices() -> dict:
    global _PRICING_CACHE
    if _PRICING_CACHE is None:
        from billing_reconciliation import _load_db_prices
        _PRICING_CACHE = _load_db_prices() or {}
    return _PRICING_CACHE


class VolcengineAdapter(VendorAdapter):
    vendor_id = "volcengine"
    source = "volc_inference_usage+bill"
    native_currency = "CNY"
    has_cache_detail = True

    # job._ingest_one_day 同一天先调 fetch_one_day 再调 fetch_apikey_rows (同一实例),
    # 暂存当天各 model 账单费用, 避免重复拉一次账单
    _day_model_cost_day: dt.date | None = None
    _day_model_cost: dict[str, float] | None = None

    def fetch_one_day(self, day: dt.date) -> list[ModelRow]:
        start_iso = f"{day.isoformat()}T00:00:00+08:00"
        end_iso = f"{day.isoformat()}T23:59:59+08:00"

        result = fetch_vendor_usage(self.vendor, start_iso, end_iso)
        self._day_model_cost_day = day
        self._day_model_cost = {
            m: float(v.get("total_cost") or 0)
            for m, v in (result.get("models") or {}).items()
        }
        rows: list[ModelRow] = []
        for model_name, m in (result.get("models") or {}).items():
            # client 返的 prompt_tokens 来自 GetInferenceUsage 的 InputTokens.
            # 官方 InputTokens 已经是"含 cache 的完整 input" — 不需要再 pack_input 把 cache 加上去.
            # 套餐项 (Coding-Plan-Lite 等) token 字段都是 None, 透传.
            rows.append(ModelRow(
                model=model_name,
                prompt_tokens=m.get("prompt_tokens"),
                completion_tokens=m.get("completion_tokens"),
                cache_read_tokens=m.get("cache_read_tokens"),
                cache_write_tokens=m.get("cache_write_tokens"),
                total_tokens=m.get("total_tokens"),
                request_count=m.get("total_count"),
                image_count=m.get("image_count"),
                cost_native=float(m.get("total_cost") or 0),
            ))
        return rows

    def fetch_apikey_rows(self, day: dt.date) -> list[ApiKeyRow]:
        ak = self.vendor["access_key"]
        sk = self.vendor["secret_key"]
        raw = fetch_key_usage(ak, sk, day.isoformat())
        if not raw:
            return []

        if self._day_model_cost_day == day and self._day_model_cost is not None:
            model_cost = self._day_model_cost
        else:
            # 未经 fetch_one_day 直接调用 (手动/测试) → 自己拉一次当天账单口径
            result = fetch_vendor_usage(
                self.vendor,
                f"{day.isoformat()}T00:00:00+08:00",
                f"{day.isoformat()}T23:59:59+08:00",
            )
            model_cost = {m: float(v.get("total_cost") or 0)
                          for m, v in (result.get("models") or {}).items()}

        # 1. (sid, model) 聚合 token — model 名走与主表相同的 normalize
        agg: dict[tuple[str, str], dict] = {}
        for r in raw:
            sid = (r.get("sid") or "").strip()
            model = normalize_model_name(r.get("model") or "") or (r.get("model") or "unknown")
            a = agg.setdefault((sid, model), {
                "input": 0, "output": 0, "cache": 0, "total": 0, "image": 0, "req": 0})
            a["input"] += r["input"]
            a["output"] += r["output"]
            a["cache"] += r["cache"]
            a["total"] += r["total"]
            a["image"] += r["image"]
            a["req"] += r["req"]

        # 2. 费用分摊: 每个 model 的当日账单金额按权重分给各 sid
        by_model: dict[str, list[tuple[str, dict]]] = {}
        for (sid, model), a in agg.items():
            by_model.setdefault(model, []).append((sid, a))

        alloc: dict[tuple[str, str], float] = {}
        for model, items in by_model.items():
            cost = model_cost.get(model, 0.0)
            if cost <= 0:
                for sid, _a in items:
                    alloc[(sid, model)] = 0.0
                continue
            weights = [self._alloc_weight(model, a) for _, a in items]
            total_w = sum(weights)
            if total_w <= 0:
                # 无价/全零 → token 数加权; token 也全 0 → 请求数
                weights = [a["total"] for _, a in items]
                total_w = sum(weights)
                if total_w <= 0:
                    weights = [a["req"] or 1 for _, a in items]
                    total_w = sum(weights)
            acc = 0.0
            for (sid, _a), w in zip(items[:-1], weights[:-1]):
                share = round(cost * w / total_w, 6)
                alloc[(sid, model)] = share
                acc += share
            # 尾差归排序后末行, 保证 Σ(分摊) == 当日账单金额
            alloc[(items[-1][0], model)] = round(cost - acc, 6)

        # 3. sid → 可读名: ListApiKeys 的 Name, 重名加 SID 尾缀, 查不到用裸 SID
        try:
            names = list_api_keys(ak, sk)
        except Exception:
            names = {}
        name_count: dict[str, int] = {}
        for n in names.values():
            name_count[n] = name_count.get(n, 0) + 1

        def display(sid: str) -> str:
            if not sid:
                return UNATTRIBUTED_KEY
            name = names.get(sid) or sid
            if name_count.get(name, 0) > 1:
                return f"{name}·{sid[-5:]}"
            return name

        out: list[ApiKeyRow] = []
        for (sid, model), a in sorted(agg.items()):
            out.append(ApiKeyRow(
                api_key=display(sid),
                model=model,
                prompt_tokens=a["input"],     # InputTokens 已含 cache, 同主表口径
                completion_tokens=a["output"],
                cache_read_tokens=a["cache"],
                cache_write_tokens=None,      # ark 用量无 cache write 维度, 同主表
                total_tokens=a["total"],
                request_count=a["req"],
                image_count=a["image"],
                cost_native=alloc.get((sid, model), 0.0),
            ))
        return out

    def _alloc_weight(self, model: str, a: dict) -> float:
        """分摊权重 = 官方单价 × token 量. 同模型各 key 用同一条价格, 币种不影响比例.

        价格缺失 (None) 返回 0, 外层会对整组退化成 token 数加权.
        """
        from billing_reconciliation import get_model_pricing
        p = get_model_pricing(model, self.vendor_id, db_prices=_cached_db_prices())
        if not p:
            return 0.0
        in_p, out_p = p.get("input_per_1m"), p.get("output_per_1m")
        cr_p = p.get("cache_read_per_1m")
        if in_p is None or out_p is None:
            return 0.0
        non_cache = max(a["input"] - a["cache"], 0)
        w = non_cache / 1e6 * in_p + a["output"] / 1e6 * out_p
        if cr_p is not None:
            w += a["cache"] / 1e6 * cr_p
        return w
