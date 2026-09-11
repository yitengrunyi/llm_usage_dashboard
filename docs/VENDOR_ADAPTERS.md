# Vendor Adapters 数据源文档

本文档描述本系统中每个供应商的数据拉取方式、字段覆盖情况、性能特征和已知限制。
维护者在新增/修改 adapter 时应同步更新此文档。

---

## 架构概览

```
xxl-job / cron (每天凌晨 1:00 CST)
       ↓ HTTP POST /api/ingest/vendors/{vendor_id}/trigger
FastAPI backend (3080)
       ↓ run_ingest(vendor_id, start, end)
ingest/job.py → adapter.fetch_one_day(day) → list[ModelRow]
       ↓ UPSERT
PostgreSQL (schema: llm_usage_dashboard)
  ├── vendor_model_usage_daily  (vendor × day × model)
  ├── vendor_usage_daily        (vendor × day 汇总)
  └── usage_daily_total         (全局 day 汇总)
```

每个 adapter 实现 `fetch_one_day(day) -> list[ModelRow]`，返回该 vendor 当天所有模型的明细行。
job.py 负责按天循环、事务写入、重试、报警。

---

## 字段说明

| 字段 | 含义 | NULL 语义 |
|------|------|-----------|
| cost_native | 上游原始币种金额 | 不会 NULL |
| total_tokens | prompt + completion (含 cache) | 不会 NULL |
| prompt_tokens | 完整输入 (含 cache_read + cache_write) | NULL = 上游不给拆分 |
| completion_tokens | 输出 token | NULL = 上游不给拆分 |
| cache_read_tokens | 缓存命中 token | NULL = 上游不暴露 |
| cache_write_tokens | 缓存写入 token | NULL = 上游不暴露 |
| request_count | HTTP 调用次数 | NULL = 上游不暴露 |
| image_count | 图片生成次数 | NULL = 上游不暴露 |

前端对 NULL 字段显示 `-`，对 0 显示 `0`。

---

## 供应商分类

### 第一类: 官方 Admin API

| vendor_id | 名称 | 币种 | 数据源 |
|-----------|------|------|--------|
| openai | OpenAI | USD | OpenAI Admin API |

### 第二类: new-api 系 (cookie 登录)

| vendor_id | 名称 | 币种 | 数据源 | 备注 |
|-----------|------|------|--------|------|
| gptmeta | Blueshirt | USD | `/api/data/self` (快) + `/api/log/self` (慢补) | 快路径不拆分 |
| apevon | Apevon | USD | `/api/statistics/` | 服务端已聚合 |

### 第三类: new-api 系 (账密直连)

| vendor_id | 名称 | 币种 | 数据源 |
|-----------|------|------|--------|
| xhub | Xhub | USD | `/api/log/self` raw log |

### 第四类: 国内云厂商 (签名鉴权)

| vendor_id | 名称 | 币种 | 数据源 |
|-----------|------|------|--------|
| tencent | 腾讯云 | CNY | `DescribeAigcUsageData` |
| wangsu | 网宿 | CNY | AIGW Billing API |
| volcengine | 火山方舟 | CNY | `ListAmortizedCostBillDaily` + CSV |
| ucloud | UCloud | CNY | `GetUserBillingByKey` + `GetUMInferTokenUsage` |

### 第五类: 代理平台 (账密登录)

| vendor_id | 名称 | 币种 | 数据源 |
|-----------|------|------|--------|
| kimi | Kimi/Moonshot | CNY | `/api?endpoint=consumes` |
| road2all | Road2All | USD | `/api/invoice/listInvoice` |

---

## 字段覆盖矩阵

| Vendor | cost | total | prompt | completion | cache_r | cache_w | req_count | img_count |
|--------|:----:|:-----:|:------:|:----------:|:-------:|:-------:|:---------:|:---------:|
| openai | ✓ | ✓ | ✓ | ✓ | ✓ | 0 | ✓ | ✓ |
| gptmeta (快) | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ | ✓ | ✗ |
| gptmeta (慢) | — | — | ✓ | ✓ | ✓ | ✓ | — | — |
| xhub | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ |
| tencent | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ | ✓ | ✓ |
| wangsu | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| kimi | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ |
| volcengine | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓* | ✗ |
| ucloud | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ |
| road2all | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ | ✓ | ✗ |
| apevon | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ |

- ✓ = 有数据, ✗ = NULL (上游不给), 0 = 固定为 0 (上游不暴露但语义确定)
- `*` volcengine request_count 仅白名单 6 模型有值，其余 NULL
- gptmeta 慢路径只 UPDATE 拆分字段，不动 cost/total/req_count（那些走快路径）

---

## 各 Vendor 详细说明

### OpenAI

- **adapter**: `ingest/adapters/openai_adapter.py`
- **上游 client**: `openai_client.py`
- **API**: OpenAI Admin API `/v1/organization/costs` + `/v1/organization/usage/completions`
- **鉴权**: Admin API Key (`sk-admin-...`)
- **时区**: **US/Pacific (PT)**，唯一一个不是 CST 的 vendor
- **性能**: ~1s/天，90 天 ~15s
- **特殊逻辑**:
  - 用 LiteLLM pricing 库按模型算 cost，再 scale 到 dashboard 总额对齐
  - DST 自动处理 (zoneinfo.ZoneInfo("America/Los_Angeles"))
  - `bucket_width=1d`，按 PT 自然日切
  - cache_write_tokens 固定 0 (OpenAI 不暴露)
  - image_count 从 `/usage/images` 拉

---

### Blueshirt (gptmeta)

- **adapter**: `ingest/adapters/blueshirt_adapter.py`
- **上游 client**: `newapi_client.py`
- **API (快路径)**: `/api/data/self` — 服务端预聚合，1s/天
- **API (慢路径)**: `/api/log/self` — raw log，40s/页，字段全
- **鉴权**: Playwright 浏览器登录 → cookie 持久化到 `data/sessions/`
- **时区**: CST
- **性能**: 快路径 1s/天; 慢路径 40s/页 × N 页/天 (大账号一天 15 万条 = 15 页 = 10min)
- **双路径设计**:
  - 快路径 (BlueshirtAdapter): cost / total_tokens / request_count 准确，但不拆 prompt/completion/cache
  - 慢路径 (ingest/blueshirt_slow.py): 只 UPDATE 拆分字段，不动 cost
  - xxl-job: 1:00 快路径 trigger, 1:30 慢路径 slow-fill
- **已知限制**:
  - `/api/log/self` **retention ~13 天**，超过的天上游返 total=0
  - 大账号 (>10 万条/天) 翻页极慢，之前 1485 页死锁过
  - session 死时返 `{success:false, message:"用户信息已更新"}` 或 HTML 登录页
  - quota / 500000 = USD (new-api 内部换算率)

---

### Xhub

- **adapter**: `ingest/adapters/newapi_direct_adapter.py` (XhubAdapter)
- **上游 client**: `newapi_direct_client.py`
- **API**: `/api/log/self` raw log (跟 blueshirt 慢路径同协议，但用账密登录不用 cookie)
- **鉴权**: 账号密码 → `/api/user/login` → token 缓存 30min
- **时区**: CST
- **性能**: 30 天 ~几分钟 (数据量大)
- **特殊逻辑**:
  - 直接走 raw log，字段全 (prompt/completion/cache_read/cache_write/request_count)
  - 不走 `/api/data/self` (那个丢字段)
  - quota_per_dollar 默认 500000，vendor config 可覆盖
  - 自定义 user header 支持 (vendor config `user_header` 字段)
- **已知限制**:
  - xhub 大账号 raw log 翻页慢 (page_size=10000, 单页 ~40s)
  - 登录限流: 频繁 login 会被封 IP

---

### 腾讯云 (tencent)

- **adapter**: `ingest/adapters/tencent_adapter.py`
- **上游 client**: `tencent_client.py`
- **API**: `DescribeAigcUsageData` (VOD AIGC 用量接口)
- **鉴权**: SecretId + SecretKey (TC3-HMAC-SHA256 签名)
- **时区**: CST
- **性能**: 单次调用返一天所有模型 5min 粒度桶，adapter 聚合到天
- **特殊逻辑**:
  - 维度拆分: `_input` / `_output` / `_cacheinput` (Specification 字段后缀)
  - request_count = input 维度的事件 COUNT
  - image_count 从 Image 类型用量单独拉
  - cache_write_tokens = NULL (腾讯不暴露)
  - 计费用内部 billing 模块 (`calculate_billing()`)

---

### 网宿 (wangsu)

- **adapter**: `ingest/adapters/wangsu_adapter.py`
- **上游 client**: `wangsu_client.py`
- **API**: AIGW Billing API (CNC-HMAC-v4 签名)
- **鉴权**: AccessKey + SecretKey
- **时区**: CST
- **性能**: 单天 1 次调用
- **字段**: **全部可用** (唯一一个所有字段都有的 vendor)
- **已知限制**:
  - 并发 >10 触发 HTTP 446
  - 单次查询最多 31 天
  - 需要 `wangsu_models.json` 配置启用的模型列表

---

### Kimi (Moonshot)

- **adapter**: `ingest/adapters/kimi_adapter.py`
- **上游 client**: `kimi_client.py`
- **API**: `/api?endpoint=consumes` (平台内部接口)
- **鉴权**: Playwright 浏览器登录 → 拦截 Authorization header → replay
- **时区**: CST
- **性能**: 单次调用返整段
- **特殊逻辑**:
  - 金额单位: 10^-5 CNY (除以 100000 得元)
  - auto-caching 架构: 只有 cache_read，没有 cache_write 概念
  - 按 product_name 拆: prompt / auto-caching / completion
  - request_count = NULL (上游不暴露，控制台也没有)
- **已知限制**:
  - 登录需要扫码 (Playwright storage_state 持久化)
  - 无法获取调用次数

---

### 火山方舟 (volcengine)

- **adapter**: `ingest/adapters/volcengine_adapter.py`
- **上游 client**: `volcengine_client.py`
- **API (双源)**:
  - 主源: `ListAmortizedCostBillDaily` — 按天成本账单 (所有模型)
  - 辅源: `CreateRecordExportTask` — 异步 CSV 导出 (仅白名单 6 模型)
- **鉴权**: AK/SK (TC3-style HMAC v4 签名)
- **时区**: CST
- **性能**: Bill 1-4s/天; CSV 异步 3-6s/task × 8 task/天; 90 天总 ~153s
- **双源拼接逻辑**:
  - Bill: 所有模型的 cost + token 拆分 (从 Element 中文字段解析"输入/输出/缓存")
  - CSV: 白名单模型的 request_count (COUNT(req_id))
  - 白名单: `doubao-seed-2-0-{pro,lite,mini,code}` / `doubao-seedance-2-0{,-fast}`
  - 非白名单模型: request_count = NULL
- **已知限制**:
  - 全局 5 QPS 限流 (billing OpenAPI)
  - CSV 时间窗最多 3 小时 → 一天 8 个 task
  - 账单 T+1~T+2 延迟 (当天数据拉不到)
  - Product 过滤参数被服务端忽略，只能客户端过滤 `Product == "ark_bd"`

---

### UCloud

- **adapter**: `ingest/adapters/ucloud_adapter.py`
- **上游 client**: `ucloud_client.py` (UCloud SDK)
- **API (双源)**:
  - Phase 1: `GetUserBillingByKey` — 按天账单 (token 从 PricingSKU 解析)
  - Phase 2: `GetUMInferTokenUsage` — 按模型调用次数 (时间窗 ≤10 天/片)
- **鉴权**: PublicKey + PrivateKey (UCloud SDK 签名)
- **时区**: CST
- **性能**: Phase 1 ~1-2s/30天; Phase 2 ~30s/30天 (10 天分片)
- **特殊逻辑**:
  - Token 拆分从 PricingSKU 字段名解析 (不是直接的 token 列)
  - 模型名需要 normalize (e.g. `claude-opus-4-6-200k` → `claude-opus-4-6`)
  - SDK monkey-patch 共享连接池
- **已知限制**:
  - `GetUMInferTokenUsage` 超过 10 天服务端超时
  - 整段 billing 调用 >30 天走慢路径 (50s+)

---

### Road2All

- **adapter**: `ingest/adapters/road2all_adapter.py`
- **上游 client**: `road2all_client.py`
- **API**: `/api/invoice/listInvoice` (type=DAY)
- **鉴权**: 账号密码 → `/api/user/login`
- **时区**: CST
- **性能**: 单次 POST 返整段所有天
- **特殊逻辑**:
  - 上游已按 day × model 聚合好
  - prompt_tokens 是完整 input (不含 cache 拆分)
- **已知限制**:
  - cache_read / cache_write = NULL (上游不暴露)
  - image_count = NULL

---

### Apevon

- **adapter**: `ingest/adapters/apevon_adapter.py`
- **上游 client**: 复用 `newapi_client.py` 的 session 基础设施
- **API**: `/api/statistics/` (服务端聚合，按 day × model × token_type)
- **鉴权**: Playwright 浏览器登录 → cookie 持久化
- **时区**: CST
- **性能**: ~91 行/月，一页搞定
- **特殊逻辑**:
  - 服务端已按 (day, model, token_type) 聚合
  - `/api/log/self/stat` 做 quota 总额校验
  - 字段全 (prompt/completion/cache_read/cache_write/request_count)
- **已知限制**:
  - image_count = NULL

---

## 时区汇总

| 时区 | Vendor |
|------|--------|
| **US/Pacific (PT)** | openai |
| **CST (UTC+8)** | 其余所有 |

OpenAI 是唯一按 PT 自然日切分的 vendor。adapter 内部用 `zoneinfo.ZoneInfo("America/Los_Angeles")` 处理 DST。

---

## 登录方式汇总

| 方式 | Vendor | 说明 |
|------|--------|------|
| API Key | openai | Admin API Key，长期有效 |
| AK/SK 签名 | tencent, wangsu, volcengine, ucloud | 标准云厂商鉴权 |
| 账密登录 | xhub, road2all | HTTP login → token 缓存 |
| 浏览器登录 (Playwright) | gptmeta, kimi, apevon | cookie 持久化到 `data/sessions/` |

浏览器登录的 vendor session 会过期，过期后 adapter 抛异常，前端 vendor 详情页显示红色 banner。
需要人工在 vendor 详情页点"登录"按钮刷新 session。

---

## 定时任务配置

| 时间 (CST) | 任务 | 说明 |
|------------|------|------|
| 01:00 | 11 个 vendor trigger | 快路径入库昨天数据 |
| 01:30 | gptmeta slow-fill | 慢路径补 blueshirt 拆分字段 |

所有 vendor 1:00 同时触发，互不依赖。每个 trigger 内部有 3 次重试 (30s / 2min / 5min)，
用尽后推飞书报警。

---

## 维护注意事项

1. **新增 vendor**: 在 `ingest/adapters/` 下新建 adapter，注册到 `__init__.py` 的 `ADAPTERS` dict，
   在 `config/vendors.json` 加配置，`vendor_meta` 表 INSERT 一行。
2. **session 过期**: 浏览器登录类 vendor (gptmeta/kimi/apevon) 需要人工刷 session。
   前端 vendor 详情页有"登录"按钮。
3. **字段 NULL vs 0**: 上游不给的字段存 NULL，真 0 存 0。前端 NULL 显示 `-`。
   不要把 NULL 改成 0，会丢失"上游不支持"的语义。
4. **服务器 .env**: xhub/road2all 的登录账号**永远不动**。
   只追加新 key，不改已有 key。
5. **blueshirt retention**: `/api/log/self` 只保留近 ~13 天 raw log。
   超过的天慢路径拿不到数据，拆分字段保持 NULL。
