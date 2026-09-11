# llm_usage_dashboard

多供应商 AIGC 用量与计费聚合面板。每天后台 cron 把 11 家平台的用量拉到本地 PG，前端按窗口查 → DB 一次 SQL 出结果（毫秒级），含今天的窗口才并发 hit 上游（秒级）。USD/CNY 双币展示。

> **核心读这份 README 的目的：理解"每家 vendor 怎么拿到数据"**，接新 vendor 时照葫芦画瓢。
>
> 这不是个常规项目，每家上游接口都不一样，没法抽象成统一 SDK —— 只能一家一家写适配。

---

## 整体链路

```
浏览器 → /api/overview → split_window
                          ├── 历史 → vendor_usage_daily (DB 一次 SQL)
                          └── 今天 → 11 个 vendor_client 并发 hit 上游
                          → merge + 双币换算 → JSON

cron (APScheduler, 跟 FastAPI 同进程):
  CST 03:00 → 10 vendor 串行 → adapter.fetch_one_day(day) → UPSERT 三层表
  CST 03:30 → blueshirt 慢路径补拆分字段
  PT  01:00 → openai 独立 cron (PT 自然日)
```

两条线的关系：
- **client (`backend/*_client.py`)** 给 main.py "今天实时查询"用，按时间窗返回 vendor service shape `{models, daily, total_cost}`
- **adapter (`backend/ingest/adapters/*.py`)** 给 ingest cron 用，按"一天"调 client 然后翻译成 `ModelRow` 进 DB
- adapter 大部分都是薄封装直接调对应 client，但**adapter 是字段语义的统一口径**，client 可以保留各自历史的奇怪

---

## 如何接入一个新 vendor（4 步模板）

接新 vendor 的 5 个必答问题：

1. **认证方式**：API key？cookie 登录？账号密码？需要验证码？
2. **数据接口**：上游有没有按天/按模型/按 token 的聚合？还是只能拉 raw log 自己 group？
3. **字段是否全**：prompt / completion / cache_read / cache_write / request_count / cost 哪些有哪些没？
4. **时区**：账单按 CST 切还是 UTC 还是 PT？
5. **限制**：限流多少 QPS？单次窗口最长多久？retention 多少天？需不需要分页？

回答完，开始接入：

```
步骤 1. 写 client: backend/your_vendor_client.py
  暴露 fetch_vendor_usage(vendor, start_iso, end_iso) -> dict
  返回 {total_cost, models: {name: {prompt_tokens, completion_tokens, ...}}, daily: [{date, cost}]}

步骤 2. 写 adapter: backend/ingest/adapters/your_vendor_adapter.py
  继承 VendorAdapter, 实现 fetch_one_day(day) -> list[ModelRow]
  内部调 client + 用 pack_input/pack_total 合并字段

步骤 3. 注册:
  - backend/ingest/adapters/__init__.py: ADAPTERS["your_vendor"] = YourVendorAdapter
  - backend/main.py: VENDOR_DISPATCH["your-type"] = fetch_your_vendor_usage
  - backend/config/vendors.json: 加 {"id": "your_vendor", "type": "your-type", ...}
  - backend/.env: 加凭据 (按 vendor_id 大写 + _USERNAME/_PASSWORD 等)
  - backend/ingest/job.py:_inject_env_credentials: 加 elif 分支注入

步骤 4. backfill 历史:
  docker compose exec backfill python -m ingest.backfill --vendors=your_vendor --days=30
```

接入后会自动：
- 每天 03:00 CST cron 跑（无需配置）
- 前端 vendor 列表自动出现（复用 VendorDetail.vue）
- /api/overview 自动包含进汇总

---

## 11 个 vendor 数据获取方式详解

每节回答：**认证 / 接口 / 字段 / 坑**。

### 1. `openai` — OpenAI Admin API（双接口拼）

**认证**：Admin API key（scope 含 `api.usage.read`），不是普通 sk-key。
```bash
OPENAI_API_KEY=sk-admin-...
OPENAI_BASE_URL=http://x.x.x.x:8899  # 可选, 透明反代 (国内服务器到 api.openai.com 被 GFW 拦)
```

**接口 — 拼两个 endpoint**：

```python
# 1. cost 走 /v1/organization/costs (USD, 跟 dashboard "Costs" tab 一致)
GET /v1/organization/costs?start_time={ts}&end_time={ts}&bucket_width=1d&limit=31

# 2. token 走 /v1/organization/usage/completions (按模型分组)
GET /v1/organization/usage/completions?start_time={ts}&end_time={ts}&bucket_width=1d&group_by=model&limit=31
```

为啥两个接口拼：`/costs` 只到 line_item 类别（Chat Models / Image Models），没有 per-model cost。token 用 `/usage/completions` 拿，然后用 **LiteLLM 定价库**反推 per-model cost，最后按 `/costs` 总额做缩放，保证 sum 严格等于 dashboard 总额。

**字段映射**：
| ModelRow | 来源 |
|---|---|
| prompt_tokens | `usage/completions.input_tokens` |
| completion_tokens | `usage/completions.output_tokens` |
| cache_read_tokens | `usage/completions.input_cached_tokens` |
| cache_write_tokens | NULL（openai 无 cache write） |
| request_count | `usage/completions.num_model_requests` |
| cost_native | `costs` 总额 × per-model 占比 |

**时区**：账单按 **US/Pacific** 自然日切（不是 UTC，不是 CST）。一天的窗口 `[PT 00:00, 次日 PT 00:00)`。

**坑**：
- **必须用 PT 自然日**否则跟 dashboard 对不上 → adapter 用 `zoneinfo.ZoneInfo("America/Los_Angeles")` 自动跟随 DST
- **bucket_width=1d 服务端硬卡 31 bucket/页**，超过返空
- **国内访问被 GFW 拦** → 透明反代部署在能出海的机器上
- cron 单独拆出来跑 **PT 01:00**（CST 约 16~17:00）

---

### 2. `tencent` — 腾讯云 VOD AIGC

**认证**：SecretId/SecretKey（腾讯云 TC3-HMAC-SHA256 签名）。
```bash
TC_SECRET_ID=AKID...
TC_SECRET_KEY=...
```

**接口 — DescribeAigcUsageData**（分两次调，Text 和 Image）：

```python
POST https://vod.tencentcloudapi.com/
  X-TC-Action: DescribeAigcUsageData
  Body: {
    "StartTime": "2026-05-28T00:00:00+08:00",
    "EndTime":   "2026-05-28T23:59:59+08:00",
    "AigcType":  "Text"  # 或 "Image"
  }
```

**字段映射**（这是个**关键坑**，spec 字段比想象的多）：

每个模型一组 5min 桶，按 `Specification` 字段分维度：
| Specification 后缀 | 含义 |
|---|---|
| `_input` | prompt_tokens |
| `_output` | completion_tokens |
| `_cacheinput` | **cache_read_tokens** ★ 之前漏了，导致 claude 类模型 cache 显示为 0 |

无 `cache_write` 概念。生图（Image type）单独算，每张 1 个 `image_count`，金额按张计。

`request_count` = 取 `_input` 维度 Count 之和（其他维度跟它一致，不重复）。

**坑**：
- **生文 / 生图必须分两次调**，AigcType 不同
- `Specification` 字段 e.g. `Tog5.4_cacheinput`，前缀是模型名，要按下划线 split 提取 dim
- `billing.py` 已经把这套算钱逻辑实现了，adapter 复用 `calculate_billing`

---

### 3. `wangsu` — 网宿 sharkletAIgateway

**认证**：AccessKey/SecretKey（自定义 CNC-HMAC-SHA256 签名）。
```bash
WANGSU_ACCESS_KEY=...
WANGSU_SECRET_KEY=...
```

**接口 — 按模型 fan-out 拉**：
```python
POST https://open.chinanetcenter.com/myview/sharkletAIgateway
  Body: {"queryStartDate":"...", "queryEndDate":"...", "modelCode":"gpt-4.1"}
```

**字段全**，prompt / completion / cache_read / cache_write 都有。

**坑**：
- **网宿不返"全部模型列表"**，必须客户端**主动告诉它要查哪个 model** → 前端有个 `/wangsu/models` CRUD 页人工维护清单（`wangsu_router.py`）
- **单次窗口最长 31 天**，超过自动拆窗口并发 merge
- **并发限制 8**（实测 >10 触发 446 错误码 → 退避重试）
- 上游**不返金额**，按 `wangsu_pricing.py` 维护的单价 + token 反推（精度有限）

---

### 4. `kimi` — Moonshot Kimi 平台

**认证**：浏览器扫码登录（Playwright），`storage_state` 保存到 `config/sessions/{domain}.json`。
- 走的不是公开 OpenAPI，是 **dashboard 内部 API**（platform.kimi.com）
- 登录后 Playwright 启 context 加载 state，拦截到自动带的 `Authorization` 头，然后用 `context.request` 重放

**接口**：
```python
GET /api?endpoint=consumes&start_time=...&end_time=...
```

返回按 `(date, product_name)` 分组的金额（10^-5 CNY），product_name 包括 `prompt` / `auto-caching` / `completion`。

**字段映射**（**字段缺很多**）：
| ModelRow | 来源 |
|---|---|
| cost_native | `amount / 100000`（10^-5 CNY → CNY） |
| prompt_tokens | 用 `amount` ÷ 单价反推 |
| cache_read_tokens | 同上（product_name=auto-caching） |
| cache_write_tokens | **NULL**（kimi 无此概念） |
| request_count | **NULL**（上游/UI 都没这字段） |

单价来自 LiteLLM DB 价格表，没匹配走 `FALLBACK_PRICING`（内置 kimi 官网价）。

**坑**：
- **没有调用次数**，前端 request_count 显示 "—"
- T+1 出账延迟，cron 03:00 跑（早于 03:00 拉到空 list）
- cookie 失效频繁，要重新扫码

---

### 5. `volcengine` — 火山方舟（双源）

**认证**：AccessKey/SecretKey（火山自定义签名 + 全局 5 QPS 限流）。
```bash
VOLCENGINE_ACCESS_KEY=...
VOLCENGINE_SECRET_KEY=...
```

**这家最复杂**，**11 轮接口探测**（`probe1-11`）后定了"Bill + CSV 双源"方案。

#### 主源 — `ListAmortizedCostBillDaily`（账单接口）

```python
POST https://billing.volcengineapi.com/?Action=ListAmortizedCostBillDaily
  Body: {"BillPeriod":"2026-05", "AmortizedDay":"2026-05-28", ...}
```

返回 `Element` 字段含"输入/输出/缓存"中文片段，按这个拆 token：
- `prompt_tokens` / `completion_tokens` / `cache_read_tokens` / `cache_write_tokens`
- `cost_native = PayableAmount`
- **不返调用次数** ✗

#### 辅源 — `CreateRecordExportTask`（异步 CSV）

```python
# 异步: 创建 task → 轮询 status → 下载 CSV
# CSV 列: req_id, model_name, input_tokens, output_tokens, cache_hit_tokens, timestamp, ...
# COUNT(req_id) = request_count ✓
```

**白名单限制（关键）**：服务端只允许 **6 个豆包新模型**：
- `doubao-seed-2-0-{pro,lite,mini,code}`
- `doubao-seedance-2-0{,-fast}`

其他模型（`doubao-seed-1-6` / `glm-4-7` 等）服务端拒绝。

**adapter 合并逻辑**：
- 白名单内：Bill 出 token+cost，CSV 出 request_count
- 白名单外：仅 Bill，request_count NULL

**坑**：
- **CSV 异步**：一个 task 3~6 秒，时间窗最多 3 小时 → 一天要发 8 个 task → 慢但 cron 不怕
- **5 QPS 全局限流**，全局 token bucket 控制
- **Product 字段**服务端"过滤参数"接受但忽略 → 只能客户端 `Product == "ark_bd"` 过滤

---

### 6. `apevon` — Apevon (new-api fork)

**认证**：cookie 登录（Playwright，**带验证码**，要 noVNC 人工操作）。
- cookie 存 `config/sessions/{域名}.json`
- 是 SPA，登录是 modal 不切路由，完成后 `localStorage.user` 写入作为判定

**接口 — 走 new-api 自带的两个聚合接口**：

```python
# 1. 总 quota (与 UI 一致)
GET /api/log/self/stat?start_timestamp=&end_timestamp=

# 2. 按 (day, model, token_name) 分组的明细
GET /api/statistics/?startTime=&endTime=&date_type=cst&p=1&page_size=2000
```

为啥不自己 group raw log：apevon raw log 太多，`/api/statistics/` 服务端已经按天聚合了，一年 1100 行能一次拿完。

**字段全**，prompt / completion / cache_read / cache_write / request_count 都有。

**坑**：
- **登录要人工**（验证码），noVNC 端口 6080
- T+1 出账延迟

---

### 7. `blueshirt` — **双路径**

**认证**：cookie 登录（Playwright，带验证码），同 apevon。

这家是**最复杂的设计**，因为大账号 raw log 拉不下来（一天 14 万条 × 1500 页 → 卡死）。

#### 快路径（默认 adapter） — `/api/data/self`

```python
GET /api/data/self?start_timestamp=&end_timestamp=
```

预聚合接口，**1 秒/天**，但字段不全：
- ✓ cost / total_tokens / request_count
- ✗ **prompt / completion / cache_read / cache_write 全 NULL**

#### 慢路径（独立模块） — `/api/log/self`

```python
GET /api/log/self?type=2&page_size=10000&start_timestamp=&end_timestamp=&p=N
```

raw log 翻页，**一页 30~40 秒**，单天 14 万条要翻 1500 页。但字段全。

**Retention 13 天**：超过的天上游返 total=0，**老天数据永远拿不到**。

**调用编排**：
- cron 03:00 跑快路径 → 拿 cost 和 total
- cron 03:30 跑慢路径 → `slow_fill_day(yesterday)` 只 UPDATE 拆分字段
- 手动 retry / trigger：**先快后慢**，快失败就跳过慢
- 前端 vendor 详情页有专属 alert："拆分字段同步至 X，快路径已到 Y"+ "补任意天"按钮

实现见 `backend/ingest/blueshirt_slow.py`。

---

### 8. `nulls` — new-api-direct（账号密码登录）

**认证**：账号密码 POST 登录 → cookie session（不需要 Playwright）。
```bash
NULLS_USERNAME=...
NULLS_PASSWORD=...
```

**接口 — raw log，自己 group**：

```python
# 登录
POST {base_url}/api/user/login {"username":..., "password":...}
# → 拿到 session cookie + user_id

# raw log 翻页
GET {base_url}/api/log/self?type=2&page_size=10000&p=N
  Headers: {user_header: user_id}   # 不同部署 header 名不同 (默认 Rix-Api-User, 有的是 New-Api-User)
```

**性能优化**：
- **session 缓存 30 min TTL**（上游 login 限流严，重复登录会 429）
- 翻页**并发 8 路**
- 第 0 页拿 total，余下页并发

**字段全**，prompt / completion / cache_read / cache_write / request_count 都有。

**坑**：
- `nulls` 原名 `xhub`，DB rename 通过 FK CASCADE 改了所有引用（见 `migrations/2026-05-29_rename_vendor_ids.sql`）。改名前的代码注释里可能还有 "xhub"
- `quota_per_dollar` vendor 配置项（不同部署兑换比例不同，e.g. 500000 = 1 dollar = 500000 quota）

---

### 9. `road2all` — 自研账单接口

**认证**：账号密码登录（同 newapi-direct）。
```bash
ROAD2ALL_USERNAME=...
ROAD2ALL_PASSWORD=...
```

**接口 — 一次拿全**：

```python
POST /api/invoice/listInvoice
  Body: {
    "type": "DAY",
    "usageTimeMin": "2026-05-28",
    "usageTimeMax": "2026-05-28",
    "pageNumber": 1, "pageSize": 100000
  }
```

**字段缺很多**：
| 字段 | 状态 |
|---|---|
| prompt_tokens / completion_tokens | ✓ |
| cost_native | ✓ |
| request_count | ✓ |
| cache_read_tokens / cache_write_tokens | **NULL**（上游不暴露） |

`has_cache_detail=False` → 前端 cache 列对 road2all 显示 "—"。

---

### 10. `ucloud` — UModelVerse（双源）

**认证**：UCloud SDK PublicKey/PrivateKey。
```bash
UCLOUD_PUBLIC_KEY=...
UCLOUD_SECRET_KEY=...
UCLOUD_PROJECT_ID=...
```

**接口 — 双源合并**：

```python
# 主源: 订单粒度 - 拆分 token + cache + cost
GetUserBillingByKey()    # 时间窗费用, 按 KeyId 聚合
ListPaidOrders()         # PricingSKU 解析 → prompt/completion/cache_read/cache_write

# 辅源: 调用次数
GetUMInferTokenUsage()   # 按 KeyId 切 10 天/片 fan-out, RequestTotal = request_count
```

为啥分两个接口：`GetUserBillingByKey` 只到 KeyId 不到 model（要 `ListPaidOrders` 看订单 SKU）；`GetUMInferTokenUsage` 只给 token 和 count 不给金额。

**合并 + 归一化**：
- 两源都用 `normalize_model_name()` 归一化模型名（e.g. `claude-opus-4-6-200k` → `claude-opus-4-6`）
- adapter 内部 dict merge

**字段全**（双源拼出来），prompt / completion / cache_read / cache_write / request_count 都有。

**坑**：
- **已知 bug**：`gpt-5.4` 和 `gpt-5.4-unlimit` normalize 漏 `-unlimit` 后缀，导致同模型两行
- **SDK 默认每次 invoke 都新开 session** → 这里 monkey-patch 共享 session + 大连接池（`ucloud_client.py`）
- token_usage 长窗口（>10 天）服务端超时 → 按 1 天/片 fan-out

---

### 11. `tc-cloud` 老接口（保留向后兼容）

`tencent` adapter 接管了新接入，但**原来的 `Dashboard.vue` 还在用老的 `/api/usage` 和 `/api/pricing`**（直接 hit 腾讯云，不走 DB）。

不要删，老路由是给"实时调价"用的（pricing.json 管理界面会即时刷新）。

---

## 字段统一口径（重要）

落 DB 时的语义：

```python
# vendor_model_usage_daily 一行 = 一家 vendor 一天一个模型
prompt_tokens       = 含 cache 的完整 input (用户视角"输入")
completion_tokens   = 输出
cache_read_tokens   = 单独拆出 (cache 列 + 算钱用 cache 单价)
cache_write_tokens  = 单独拆出
total_tokens        = prompt + completion (input 已含 cache, 不要再加)
request_count       = HTTP 调用次数
image_count         = 生图张数
cost_native         = 上游原币种金额
```

**上游 client 内部** prompt 通常是"非缓存 input"（为了用单价反推 token），**adapter 用 `pack_input(p, cr, cw)` 在出库前合并成完整 input**。client 内部算钱逻辑不改，避免回归。

**NULL vs 0**（前端按这个显示）：
- `cache_*` NULL → 上游不暴露（road2all / blueshirt 快路径）→ 显示 "—"
- `cache_*` 0 → 真没用 cache → 显示 0
- `request_count` NULL → 上游没这字段（kimi / volcengine 白名单外）→ 显示 "—"

`has_cache_detail` 字段记在 `vendor_usage_daily` 行上，区分"上游不给"和"真 0"。

---

## DB 设计（6 表，三层物化）

```
usage_daily_total         (一天 1 行)                  ← Overview 全局图
   ↑ SUM(vendor_id)
vendor_usage_daily        (一天 × N vendor)            ← vendor 卡片
   ↑ SUM(model)
vendor_model_usage_daily  (一天 × N vendor × M model)  ← Vendor 详情页明细

vendor_meta               vendor 元数据 (display_name, currency, source)
vendor_ingest_run         任务历史 (审计 + 手动重试)
vendor_ingest_state       每 vendor 入库游标 (cron 增量从这里 +1)
```

**写入顺序**（`job.py:_ingest_one_day`，单事务）：

1. UPSERT `vendor_usage_daily`（父行先写，model 表 FK）
2. UPSERT `vendor_model_usage_daily`（model 粒度）
3. **UPDATE `vendor_usage_daily` SET tokens = SUM(model)** ★ 关键，保证两层一致（早期 bug：NULL 累加成 0 导致对不上）
4. UPDATE `usage_daily_total`（重算当天全局）
5. UPSERT `vendor_ingest_state.last_ingested_date = max(old, day)`

任何一步抛 → 事务回滚，state 不前进，下次 cron 从 `last_ingested_date+1` 续。

**`uniq_running_per_vendor` 部分唯一索引**：同 vendor 同时只允许一个 running，防 cron 重复触发。

**zombie 清理**：backend startup 时扫一遍 >2h running 的 run 标 failed（避免被 kill 后的 run 永远卡 running，下次 cron 拒插）。

---

## 调度

| 时间 | 任务 |
|---|---|
| CST 03:00 | 10 vendor 串行跑 `[MAX(已入库)+1, 昨天]` |
| CST 03:30 | blueshirt 慢路径补昨天 |
| PT  01:00 | openai 独立 cron |

**为啥 03:00 不是 01:00**：kimi / apevon T+1 出账，01:00 太早拉到空。

**为啥串行**：上游限流（历史上有 vendor 429 过）。

**重试**：每 vendor 4 次 attempt（立刻 / 5min / 30min / 1h），用尽推飞书。

**`misfire_grace_time=1`**：backend 重启时错过 → 跳过等明天（cron 走"补缺语义"自动补上）。

---

## HTTP 接口

| Endpoint | 说明 |
|---|---|
| `GET /api/overview` | 全局总览 |
| `GET /api/vendors` | vendor 列表（去凭据字段） |
| `GET /api/vendors/{id}/usage` | 单 vendor 详情 |
| `POST /api/ingest/vendors/{id}/trigger` | 手动触发入库，body `{start?, end?}` |
| `POST /api/ingest/vendors/blueshirt/slow-fill?day=` | 单天补慢路径 |
| `GET /api/ingest/runs?vendor_id=&limit=` | 任务历史 |
| `POST /api/ingest/runs/{id}/retry` | 重试某次失败 run |
| `GET /api/ingest/state` | 各 vendor 入库游标 |
| `POST /api/vendors/{id}/login` | Playwright 弹浏览器登录 |
| `PUT /api/vendors/{id}/account` | 换号（写 .env + 失效 session + 审计） |
| `GET/PUT /api/litellm/*` | LiteLLM 子模块（独立架构，**跟 ingest 不交集**） |

---

## 部署

```bash
# 服务器拉取 + 重建
cd /data/llm-usage-dashboard
sudo git pull
sudo docker compose up -d --build backend

# backfill (一次性补历史)
sudo docker compose exec backfill python -m ingest.backfill --vendors=all --days=90

# 看 cron 日志
sudo docker exec backend tail -f /app/data/logs/scheduler.log

# 看下次跑时间
sudo docker exec backend python -c "
from ingest.scheduler import _scheduler
for j in _scheduler.get_jobs(): print(j.id, j.next_run_time)
"
```

`docker-compose.yml` 3 个 service：`backend` / `backfill`（bind-mount 源码，长跑用） / `frontend`，全 `network_mode: host`。

> ⚠️ 服务器 `.env` 里的 nulls / road2all 账号是手动配的，**git pull 时如果 vendors.json 有冲突永远 `git checkout` 远端版本**。

---

## 故障排查（速查）

| 现象 | 原因 / 修法 |
|---|---|
| Overview 顶部 "X 供应商有未入库的天" | `get_missing_days_in_window` 检测：vendor 首次~最后有数据**之间**缺天 → 去 vendor 详情页点"更新"或 curl trigger |
| 某 vendor 一直显示"上次同步"很久没动 | zombie 卡 running → backend restart 自动清，或 `UPDATE vendor_ingest_run SET status='failed' WHERE vendor_id='...' AND status='running'` |
| openai 某天数据比平时少一半 | CST 白天手动 trigger 拉的，PT 还没结束 → 等 PT 01:00 cron 自动覆盖（UPSERT），或手动 trigger 重跑 |
| blueshirt 模型有但 prompt/cache 全 NULL | 慢路径没跑 → vendor 详情页"补拆分字段"按钮（仅 13 天内），超过 retention 无法恢复 |
| cron 跑了但数据是 0 | 上游 T+1 延迟 → 看 `scheduler.log` 确认 cron 有跑，没有就是异常退出 |
| 飞书没收到失败告警 | `FEISHU_WEBHOOK_URL` 没配 / 4 次 retry 未用尽 |

---

## 已知坑 / TODO

- **ucloud `gpt-5.4` vs `gpt-5.4-unlimit`** → `normalize_model_name` 漏 `-unlimit` 后缀
- **scheduler 假设单 worker** → 多 worker 会重复跑，需 PG advisory lock

---

## 项目代码量

~15.2K 行（不含 node_modules / __pycache__）：backend ~11.6K Python / frontend ~3.0K Vue+JS / migrations ~0.6K SQL。
