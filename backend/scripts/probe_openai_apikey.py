"""一次性探测 round4: group_by=api_key_id 的正确用法 (审查发现 round1 探测用了错字面量)."""
import json
import os
import time
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

load_dotenv()
BASE_URL = (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com").rstrip("/") + "/v1"
PROXY = os.environ.get("OPENAI_PROXY") or None
PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None
KEY = os.environ.get("OPENAI_API_KEY", "")
H = {"Authorization": f"Bearer {KEY}"}


def get(path, params):
    r = requests.get(f"{BASE_URL}{path}?{urlencode(params, doseq=True)}",
                     headers=H, timeout=30, proxies=PROXIES)
    print(f"GET {path} group_by={params.get('group_by')} -> {r.status_code}")
    if r.status_code != 200:
        print("  ", r.text[:300])
        return None
    return r.json()


end = int(time.time())
start = end - 7 * 86400

# 1) usage/completions 双维 model+api_key_id
b = get("/organization/usage/completions",
        {"start_time": start, "bucket_width": "1d", "limit": 31,
         "group_by": ["model", "api_key_id"]})
if b:
    rows = [dict(r, _d=bucket.get("start_time"))
            for bucket in (b.get("data") or []) for r in (bucket.get("results") or [])]
    print(f"  rows={len(rows)}; 样例:")
    for r in rows[:4]:
        print("   ", json.dumps({k: r[k] for k in ("api_key_id", "model", "input_tokens",
                "output_tokens", "input_cached_tokens", "input_cache_write_tokens",
                "num_model_requests")}, ensure_ascii=False))

# 2) costs 单维 api_key_id
c = get("/organization/costs",
        {"start_time": start, "bucket_width": "1d", "limit": 31, "group_by": "api_key_id"})
if c:
    rows = [dict(r, _d=bucket.get("start_time"))
            for bucket in (c.get("data") or []) for r in (bucket.get("results") or [])]
    tot = sum(float((r.get("amount") or {}).get("value") or 0) for r in rows)
    ids = sorted({str(r.get("api_key_id")) for r in rows})
    print(f"  rows={len(rows)} Σ={tot:.4f} ids={ids}")

# 3) 对照: 同窗口无过滤总额 — 分组行是否完整划分总额
c2 = get("/organization/costs", {"start_time": start, "bucket_width": "1d", "limit": 31})
if c2:
    tot2 = sum(float((r.get("amount") or {}).get("value") or 0)
               for bucket in (c2.get("data") or []) for r in (bucket.get("results") or []))
    print(f"  无过滤 Σ={tot2:.4f}  (分组Σ-无过滤Σ={tot-tot2:+.6f})")
