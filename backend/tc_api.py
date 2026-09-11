import hashlib
import hmac
import json
import logging
import time
import datetime
import requests
import os
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

SECRET_ID = os.getenv("TC_SECRET_ID")
SECRET_KEY = os.getenv("TC_SECRET_KEY")

SERVICE = "vod"
HOST = "vod.tencentcloudapi.com"
VERSION = "2018-07-17"


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _call_tc_api(action: str, payload: dict) -> dict:
    timestamp = int(time.time())
    date = datetime.datetime.fromtimestamp(timestamp, datetime.UTC).strftime("%Y-%m-%d")

    payload_str = json.dumps(payload)
    ct = "application/json; charset=utf-8"

    canonical_headers = f"content-type:{ct}\nhost:{HOST}\nx-tc-action:{action.lower()}\n"
    signed_headers = "content-type;host;x-tc-action"
    hashed_payload = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()
    canonical_request = f"POST\n/\n\n{canonical_headers}\n{signed_headers}\n{hashed_payload}"

    algorithm = "TC3-HMAC-SHA256"
    credential_scope = f"{date}/{SERVICE}/tc3_request"
    hashed_canonical_request = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    string_to_sign = f"{algorithm}\n{timestamp}\n{credential_scope}\n{hashed_canonical_request}"

    secret_date = _sign(("TC3" + SECRET_KEY).encode("utf-8"), date)
    secret_service = _sign(secret_date, SERVICE)
    secret_signing = _sign(secret_service, "tc3_request")
    signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"{algorithm} Credential={SECRET_ID}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    headers = {
        "Authorization": authorization,
        "Content-Type": ct,
        "Host": HOST,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": VERSION,
    }

    resp = requests.post(f"https://{HOST}", headers=headers, data=payload_str, timeout=30)
    return resp.json()


def fetch_usage(aigc_type: str, start_time: str, end_time: str) -> list:
    """
    调用 DescribeAigcUsageData 接口获取用量数据。

    Args:
        aigc_type: "Text" 或 "Image"
        start_time: ISO 格式，如 "2026-03-19T00:00:00+08:00"
        end_time: ISO 格式，如 "2026-03-23T23:59:59+08:00"

    Returns:
        AigcUsageDataSet 列表
    """
    payload = {
        "StartTime": start_time,
        "EndTime": end_time,
        "AigcType": aigc_type,
    }
    result = _call_tc_api("DescribeAigcUsageData", payload)

    response = result.get("Response", {})
    if "Error" in response:
        raise Exception(f"TC API Error: {response['Error']['Code']} - {response['Error']['Message']}")

    return response.get("AigcUsageDataSet", [])


def _fetch_text_detail_window(start_time: str, end_time: str, max_pages: int = 300) -> list:
    """拉一个时间窗的 TextDetail 明细 (带 ScrollToken 翻页)。

    任一页失败 / ScrollToken 不推进 / 超页数上限 → 抛异常 (上游没有"部分成功"语义)。
    """
    payload = {
        "StartTime": start_time,
        "EndTime": end_time,
        "AigcType": "TextDetail",
        "PageSize": 200,
    }
    rows: list = []
    seen_tokens: set = set()
    for _ in range(max_pages):
        result = _call_tc_api("DescribeAigcUsageData", payload)
        response = result.get("Response", {})
        if "Error" in response:
            raise Exception(
                f"TC API Error: {response['Error']['Code']} - {response['Error']['Message']}"
                f" (window {start_time}~{end_time})"
            )
        detail = response.get("AigcTextDetails") or {}
        batch = detail.get("Data") or []
        rows.extend(batch)
        scroll = detail.get("ScrollToken")
        if not scroll or not batch:
            return rows
        if scroll in seen_tokens:
            raise Exception(f"ScrollToken 未推进, 疑似翻页死循环 (window {start_time}~{end_time})")
        seen_tokens.add(scroll)
        payload["ScrollToken"] = scroll
    raise Exception(f"翻页超过 {max_pages} 页, 疑似数据异常 (window {start_time}~{end_time})")


def _fetch_text_detail_adaptive(start_time: str, end_time: str, depth: int = 0) -> list:
    """拉一个时间窗, 失败时二分窗口重试 (基础 1h, 最小 1/16 窗口 ≈ 3.75 分钟)。

    高峰日 (8/10~8/12 实测 25~32 万请求/天) 单个 4h 窗口可超 6 万行, 撞
    _fetch_text_detail_window 的 300 页上限; 二分后行数摊薄到子窗口, 只有
    最小窗仍失败才向上抛 (调用方放弃该天 key 拆分, 不写残缺数据)。
    窗口边界语义为 [start, end] 闭区间秒级端点, 二分在整秒上切。
    """
    import datetime as _dt

    try:
        return _fetch_text_detail_window(start_time, end_time)
    except Exception as e:
        if depth >= 4:
            raise
        s = _dt.datetime.fromisoformat(start_time)
        e_dt = _dt.datetime.fromisoformat(end_time)
        half = int((e_dt - s).total_seconds()) // 2
        mid_end = s + _dt.timedelta(seconds=half)
        mid_start = mid_end + _dt.timedelta(seconds=1)
        left = _fetch_text_detail_adaptive(start_time, mid_end.isoformat(), depth + 1)
        right = _fetch_text_detail_adaptive(mid_start.isoformat(), end_time, depth + 1)
        logger.warning("tc TextDetail 窗口 %s~%s 失败(%s), 二分重试成功", start_time, end_time, e)
        return left + right


def fetch_text_detail_day(day) -> list:
    """拉整天生文逐请求明细 (AigcType=TextDetail), 供按 API key 拆分。

    - 整天一次查询上游直接 InternalError, 按小时窗口分片 (窗口再大也偶发报错);
      高峰日 4h 窗口会撞 300 页上限, 所以基础窗口取 1h, 失败再二分 (见 _fetch_text_detail_adaptive)
    - 6 窗口并发拉 (上游限频 20 次/秒, 远够), 窗口内串行翻页
    - 每行字段: ApiKey(脱敏, 如 "TgawD3m1****") / Model(display 名) /
      InputTokens(不含缓存) / CacheInputTokens / OutputTokens / StatusCode / Timestamp(UTC)

    任何窗口(含二分重试后)失败 → 抛异常, 调用方应放弃当天 key 拆分 (别写残缺数据覆盖已有行)。
    """
    from concurrent.futures import ThreadPoolExecutor

    windows = []
    for h in range(24):
        windows.append((
            f"{day.isoformat()}T{h:02d}:00:00+08:00",
            f"{day.isoformat()}T{h:02d}:59:59+08:00",
        ))
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [ex.submit(_fetch_text_detail_adaptive, s, e) for s, e in windows]
        parts = [f.result() for f in futures]  # 任一窗口异常在此抛出
    return [row for part in parts for row in part]
