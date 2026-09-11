"""
火山方舟 (Volcengine Ark) 客户端 — 双数据源.

主源 GetInferenceUsage (ark 推理统计):
- per-day × per-model token / cache / request_count / image_count
- 跟官方控制台数字完全一致 (含套餐内消耗的 token)
- 一次 API 调用拿整段 (~1 秒)
- 关键: 加 Filters: [{"Key":"ModelName","Value":"*"}] 触发按模型分组返回 (Value 内容随意,
  起到的是 "开启分组" 而不是"过滤"作用)

辅源 ListAmortizedCostBillDaily (billing 账单):
- 出 per-model cost (按 token 计费的金额)
- 套餐项 (Coding-Plan-Lite / Doubao 免费包) 也出 cost 但 token=NULL
  (套餐内的 token 消耗只在 GetInferenceUsage 里能看到, Bill 不暴露)

历史: 之前用 Bill 算 token 比官方少 17% (套餐内消耗被吞), 试过 CSV CreateRecordExportTask
但服务端白名单卡死 6 个豆包模型. GetInferenceUsage 是正解, 字段全 + 无白名单.

性能: GetInferenceUsage 一次调用 1 秒; Bill 30 天 ~55 秒 (5 QPS 限流). 总耗时主要在 Bill.
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter

from utils import normalize_model_name

# 共享 Session + 连接池: 复用 keep-alive 避免每次 TLS 握手
_SESSION = requests.Session()
_SESSION.mount("https://", HTTPAdapter(pool_connections=32, pool_maxsize=32))


# ────── 全局 5 QPS 令牌桶 (火山 billing OpenAPI 限流) ──────
class _RateLimiter:
    def __init__(self, qps: float):
        self.interval = 1.0 / qps
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.time()
            if now < self._next:
                time.sleep(self._next - now)
                now = self._next
            self._next = now + self.interval


_QPS_LIMITER = _RateLimiter(5)  # 火山 billing OpenAPI 单账号 5 QPS


# ────── Volcengine TC3-style HMAC v4 签名 ──────
def _h(key: bytes, data: str) -> bytes:
    return hmac.new(key, data.encode(), hashlib.sha256).digest()


def _sign_post(ak: str, sk: str, action: str, body_json: dict,
               host: str, service: str, region: str, version: str) -> tuple[int, dict]:
    now = datetime.datetime.now(datetime.UTC)
    date_short = now.strftime("%Y%m%d")
    date_long = now.strftime("%Y%m%dT%H%M%SZ")
    body = json.dumps(body_json, separators=(",", ":")).encode()
    payload_hash = hashlib.sha256(body).hexdigest()
    qp = {"Action": action, "Version": version}
    cq = "&".join(
        f"{quote(k, safe='-_.~')}={quote(str(v), safe='-_.~')}" for k, v in sorted(qp.items())
    )
    ch = (
        f"content-type:application/json\nhost:{host}\n"
        f"x-content-sha256:{payload_hash}\nx-date:{date_long}\n"
    )
    sh = "content-type;host;x-content-sha256;x-date"
    cr = "\n".join(["POST", "/", cq, ch, sh, payload_hash])
    cs = f"{date_short}/{region}/{service}/request"
    sts = "\n".join(["HMAC-SHA256", date_long, cs, hashlib.sha256(cr.encode()).hexdigest()])
    sig = hmac.new(
        _h(_h(_h(_h(sk.encode(), date_short), region), service), "request"),
        sts.encode(), hashlib.sha256,
    ).hexdigest()
    auth = f"HMAC-SHA256 Credential={ak}/{cs}, SignedHeaders={sh}, Signature={sig}"
    r = _SESSION.post(
        f"https://{host}/?{cq}",
        data=body,
        headers={
            "Authorization": auth, "Content-Type": "application/json", "Host": host,
            "X-Content-Sha256": payload_hash, "X-Date": date_long,
        },
        timeout=30,
    )
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"_raw": r.text}


def _billing_call(ak: str, sk: str, action: str, body: dict, retries: int = 5) -> dict:
    """billing service 调用 + 全局 5 QPS 限流 + FlowLimit 退避重试。"""
    for i in range(retries):
        _QPS_LIMITER.wait()
        status, resp = _sign_post(
            ak, sk, action, body,
            host="billing.volcengineapi.com", service="billing",
            region="cn-beijing", version="2022-01-01",
        )
        err = (resp.get("ResponseMetadata") or {}).get("Error") or {}
        if err and "FlowLimit" in (err.get("Code") or ""):
            time.sleep(1.5 * (i + 1))
            continue
        if err:
            raise RuntimeError(f"volcengine {action}: {err.get('Code')}: {err.get('Message')}")
        if status != 200:
            raise RuntimeError(f"volcengine {action}: status={status}")
        return resp.get("Result") or {}
    raise RuntimeError(f"volcengine {action}: FlowLimit 重试 {retries} 次仍失败")


# ────── 单天拉取 ──────
PAGE_SIZE = 200  # Daily Limit 实测上限 200, ≥500 报 InvalidParameter
DAY_WORKERS = 5  # 配合 5 QPS 令牌桶, 多了也用不上


def _fetch_day(ak: str, sk: str, day: str) -> list[dict]:
    """单天 ark_bd 行 (服务端不过滤 Product, 客户端过滤)。
    单天 ~1700 行 / 200 = 9 页串行翻 (没法预知页数)。"""
    month = day[:7]
    items: list[dict] = []
    offset = 0
    while True:
        r = _billing_call(ak, sk, "ListAmortizedCostBillDaily", {
            "AmortizedMonth": month, "AmortizedDay": day,
            "Limit": PAGE_SIZE, "Offset": offset,
        })
        page = r.get("List") or []
        items.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return [it for it in items if it.get("Product") == "ark_bd"]


# ────── 聚合 ──────
def _safe_float(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _classify_element(element: str) -> str:
    # 火山账单实际字串是 `（缓存-命中)` / `（缓存-写入)` 带短横, 不能用 "缓存命中" 紧贴匹配.
    # 拆开判断: 出现 "缓存" 且 "命中" → read; 出现 "缓存" 且 "写"/"创建" → write.
    if "缓存" in element and ("命中" in element or "读" in element):
        return "cache_read"
    if "缓存" in element and ("写" in element or "创建" in element):
        return "cache_write"
    if "输入" in element:
        return "input"
    if "输出" in element:
        return "output"
    return "other"


def _aggregate(rows: list[dict]) -> dict:
    """按 ExpandField 聚合 model + 按 AmortizedDay 聚合 daily。"""
    models: dict[str, dict] = {}
    daily_map: dict[str, float] = {}

    for r in rows:
        day = (r.get("AmortizedDay") or "")[:10]
        if not day:
            continue
        model = normalize_model_name((r.get("ExpandField") or "").strip()) or "volcengine 其它项"
        kind = _classify_element(r.get("Element") or "")
        count = _safe_float(r.get("Count"))
        unit = r.get("Unit") or ""
        tokens = int(round(count * 1000)) if "千" in unit else int(round(count))
        cost = _safe_float(r.get("PayableAmount"))

        m = models.setdefault(model, {
            "prompt_tokens": 0, "completion_tokens": 0,
            "cache_tokens": 0, "cache_read_tokens": 0,
            # ark 用量/账单均无 cache write 维度 → None = 未暴露, 不伪造 0
            "cache_write_tokens": None,
            "total_tokens": 0, "total_count": 0, "total_cost": 0.0,
        })
        if kind == "input":
            m["prompt_tokens"] += tokens
        elif kind == "output":
            m["completion_tokens"] += tokens
        elif kind == "cache_read":
            m["cache_tokens"] += tokens
            m["cache_read_tokens"] += tokens
        elif kind == "cache_write":
            m["cache_write_tokens"] = (m["cache_write_tokens"] or 0) + tokens
        m["total_cost"] += cost

        daily_map[day] = daily_map.get(day, 0.0) + cost

    for m in models.values():
        m["total_tokens"] = m["prompt_tokens"] + m["cache_read_tokens"] + m["completion_tokens"]
        m["total_cost"] = round(m["total_cost"], 6)

    daily = sorted(
        ({"date": d, "cost": round(c, 6)} for d, c in daily_map.items()),
        key=lambda x: x["date"],
    )
    total_cost = round(sum(m["total_cost"] for m in models.values()), 6)
    return {"total_cost": total_cost, "models": models, "daily": daily}


def _iso_to_date(iso: str) -> datetime.date:
    return datetime.datetime.fromisoformat(iso).date()


def _days_in_range(start: datetime.date, end: datetime.date) -> list[str]:
    out: list[str] = []
    d = start
    while d <= end:
        out.append(d.strftime("%Y-%m-%d"))
        d += datetime.timedelta(days=1)
    return out


def fetch_vendor_usage(vendor: dict, start_time: str, end_time: str) -> dict:
    """主入口: 合并 GetInferenceUsage (token/req) + Bill API (cost).

    输出 vendor service shape:
      models[name] = {prompt_tokens, completion_tokens, cache_read/write, total_tokens,
                      total_count, image_count, total_cost}
      daily = [{date, cost, total_tokens}]

    合并规则:
    - GetInferenceUsage 出有 token 的 model → token/req 用它, cost 从 Bill 匹配
    - Bill 出有 cost 但 GetInferenceUsage 没的 model → 套餐项 (Coding-Plan / 免费包),
      作为独立 model 行, token = NULL, cost 保留
    - GetInferenceUsage 有 token 但 Bill 无 cost → 套餐覆盖期内的调用, cost = 0
    """
    ak = vendor["access_key"]
    sk = vendor["secret_key"]
    start_d = _iso_to_date(start_time)
    end_d = _iso_to_date(end_time)
    empty = {
        "total_cost": 0.0, "models": {}, "daily": [],
        "vendor_id": vendor["id"], "vendor_name": vendor["name"],
        "currency": vendor.get("currency", "CNY"),
    }
    if start_d > end_d:
        return empty

    # ─── 1. GetInferenceUsage 拿 per-day per-model token / req / image ───
    usage_rows = _fetch_inference_usage(ak, sk, start_d, end_d)
    # 按 model 聚合 token
    usage_by_model: dict[str, dict] = {}
    daily_tokens: dict[str, int] = defaultdict(int)
    for row in usage_rows:
        m = normalize_model_name(row["model"]) or row["model"]
        d = row["day"]
        u = usage_by_model.setdefault(m, {
            "prompt_tokens": 0, "completion_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": None,  # ark 无 cache write 维度, 同账单路径
            "total_tokens": 0, "total_count": 0, "image_count": 0,
        })
        u["prompt_tokens"] += row["input"]
        u["completion_tokens"] += row["output"]
        u["cache_read_tokens"] += row["cache"]
        u["total_tokens"] += row["total"]
        u["total_count"] += row["req"]
        u["image_count"] += row["image"]
        daily_tokens[d] += row["total"]

    # ─── 2. Bill API 拿 per-model cost + daily cost ───
    days = _days_in_range(start_d, end_d)
    all_rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=DAY_WORKERS) as ex:
        for f in as_completed([ex.submit(_fetch_day, ak, sk, d) for d in days]):
            all_rows.extend(f.result())
    bill = _aggregate(all_rows)  # {total_cost, models: {name: {...cost}}, daily: [{date,cost}]}
    bill_models = bill["models"]
    daily_cost = {d["date"]: d["cost"] for d in bill["daily"]}

    # ─── 3. 合并 ───
    models: dict[str, dict] = {}
    # 先把 GetInferenceUsage 的 model 全部放进去, cost 从 Bill 匹配 (匹配不上 = 0)
    for m, u in usage_by_model.items():
        bm = bill_models.get(m, {})
        models[m] = {
            **u,
            "cache_write_tokens": None,  # ark 无 cache write 维度
            "total_cost": float(bm.get("total_cost") or 0),
        }
    # Bill 出但 GetInferenceUsage 没的 (套餐项 e.g. Coding-Plan-Lite(包月) / 免费资源包)
    # → 作为独立 model 行, token=None 区分 "不是按 token 计费"
    for m, b in bill_models.items():
        if m in models:
            continue
        models[m] = {
            "prompt_tokens": None, "completion_tokens": None,
            "cache_read_tokens": None, "cache_write_tokens": None,
            "total_tokens": None, "total_count": None, "image_count": None,
            "total_cost": float(b.get("total_cost") or 0),
        }

    # daily: cost 来自 Bill, total_tokens 来自 GetInferenceUsage
    all_days = sorted(set(daily_cost) | set(daily_tokens))
    daily = [
        {"date": d, "cost": round(daily_cost.get(d, 0.0), 6),
         "total_tokens": daily_tokens.get(d) or None}
        for d in all_days
    ]

    return {
        "total_cost": round(sum(m["total_cost"] for m in models.values()), 6),
        "models": models,
        "daily": daily,
        "vendor_id": vendor["id"],
        "vendor_name": vendor["name"],
        "currency": vendor.get("currency", "CNY"),
    }


def _fetch_inference_usage(ak: str, sk: str, start_d: datetime.date,
                            end_d: datetime.date) -> list[dict]:
    """一次 GetInferenceUsage 调用拿 [start..end] × 所有 model 行.

    返回: [{day, model, input, output, cache, total, image, req}, ...]

    关键: Filters 字段任意, 触发服务端按 ModelName 维度分组 (返回多出 ModelName 列).
    没 Filters 就只回账户级 1 行/天.
    """
    return _giu(ak, sk, start_d, end_d, [{"Key": "ModelName", "Value": "*"}])


def _giu(ak: str, sk: str, start_d: datetime.date, end_d: datetime.date,
         filters: list[dict]) -> list[dict]:
    """GetInferenceUsage 通用查询 + 行解析. filters 决定分组维度.

    返回行附带回 Filters 里出现的维度列 (sid / model).
    """
    status, resp = _sign_post(
        ak, sk, "GetInferenceUsage",
        {
            "QueryInterval": "Day",
            "StartTime": start_d.isoformat(),
            "EndTime": end_d.isoformat(),
            "Filters": filters,
        },
        host="open.volcengineapi.com", service="ark",
        region="cn-beijing", version="2024-01-01",
    )
    err = (resp.get("ResponseMetadata") or {}).get("Error") or {}
    if err:
        raise RuntimeError(f"GetInferenceUsage: {err.get('Code')}: {err.get('Message')}")
    if status != 200:
        raise RuntimeError(f"GetInferenceUsage: status={status}")

    result = resp.get("Result") or {}
    fields = [f["Name"] for f in result.get("Fields") or []]
    out: list[dict] = []
    idx = {f: fields.index(f) for f in fields}
    for row in result.get("Data") or []:
        model = row[idx["ModelName"]] if "ModelName" in idx else ""
        if not model:
            continue
        out.append({
            "sid": (row[idx["ApikeyID"]] if "ApikeyID" in idx else ""),
            "day": row[idx["Day"]],
            "model": model,
            "input": int(row[idx["InputTokens"]] or 0),
            "output": int(row[idx["OutputTokens"]] or 0),
            "cache": int(row[idx["CacheTokensHit"]] or 0),
            "total": int(row[idx["TotalTokens"]] or 0),
            "image": int(row[idx["ImageCount"]] or 0),
            "req": int(row[idx["ReqCnt"]] or 0),
        })
    return out


def fetch_key_usage(ak: str, sk: str, day: str) -> list[dict]:
    """单天 (ApikeyID × ModelName) 推理用量, 一次调用.

    返回: [{sid, model, input, output, cache, total, image, req}, ...]
    sid 可能为 '' — 接入点(ep-xxx)/AuthToken 鉴权的用量归不到具体 API Key
    (doc: Filters Key=ApikeyID, Values 空数组 = 不过滤但按该维度分组返回).
    """
    d = datetime.date.fromisoformat(day)
    return _giu(ak, sk, d, d, [
        {"Key": "ApikeyID", "Values": []},
        {"Key": "ModelName", "Value": "*"},
    ])


def list_api_keys(ak: str, sk: str) -> dict[str, str]:
    """方舟 API Key 列表: {SID(资源ID): Name}. 用于把用量行的 sid 映射成可读名.

    PageSize 服务端上限 10, 翻页拉全. 失败抛 RuntimeError (调用方可捕获降级用裸 SID).
    """
    out: dict[str, str] = {}
    page = 1
    while True:
        status, resp = _sign_post(
            ak, sk, "ListApiKeys",
            {"ProjectName": "default", "Limit": 10, "PageNumber": page},
            host="open.volcengineapi.com", service="ark",
            region="cn-beijing", version="2024-01-01",
        )
        err = (resp.get("ResponseMetadata") or {}).get("Error") or {}
        if err:
            raise RuntimeError(f"ListApiKeys: {err.get('Code')}: {err.get('Message')}")
        result = resp.get("Result") or {}
        items = result.get("Items") or []
        for it in items:
            out[it["SID"]] = (it.get("Name") or "").strip() or it["SID"]
        total = int(result.get("TotalCount") or 0)
        if not items or len(out) >= total or page > 30:
            break
        page += 1
    return out

