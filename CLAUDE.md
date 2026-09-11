# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Multi-vendor AIGC usage and billing aggregation dashboard. Daily cron jobs pull usage from 12 cloud platforms (tencent / blueshirt / nulls / wangsu / apevon / ucloud / road2all / kimi / volcengine / openai / bigmodel / grok) into local PostgreSQL; frontend queries are split by time window (historical → DB in milliseconds, today → live API calls in seconds). Dual-currency (USD/CNY) display, per-API-key drill-down, billing reconciliation, self-healing patrol agent.

**Key insight**: This is not a conventional project. Each upstream vendor has a different API — there's no way to abstract them into a unified SDK. The core work is writing vendor-specific adapters. **上游文档不可信**：字段口径、参数字面量、单位、分页都要实测。集成新维度/新 vendor 的固定打法是先写探测脚本（`backend/scripts/`）多轮验证、对账（分组行 Σ == 无过滤总额）后才固化进 client/adapter。

## Architecture

### Data Flow

```
Browser → /api/overview → split_window
                          ├── Historical → vendor_usage_daily (DB, one SQL query)
                          └── Today → 12 vendor_client concurrent API calls
                          → merge + dual-currency conversion → JSON
```

### Cron 全景（scheduler.py `start()`，与 FastAPI 同进程，共 9 个 job）

| 时间 | Job | 说明 |
|------|-----|------|
| CST 02:30 | `login_precheck_autologin` | 登录型 vendor session 预检：过期且 .env 配了账密 → 自动登录（每 vendor 每天最多 1 次，失败推飞书）；没配账密的留给 agent/人工 |
| CST 03:00 | `ingest_all_fast_cst` | 9 家并发快路径（12 − openai − kimi − apevon），窗口 = [MAX(已入库)+1, CST 昨天]（补缺语义）。**blueshirt/nulls 快路径成功后同线程立刻串行接慢路径**（不再有独立 slow cron） |
| CST 03:30 | `points_daily_summary` | 积分预聚合（MySQL，独立数据域）。只更新 [昨天,昨天]，**错过不自动补**，需手动跑 `init_points_summary.py` |
| CST 05:00 / 09:00 / 19:00 | `agent_patrol_*` | 自愈 Agent 巡检（复核各 cron 批次），`AGENT_PATROL_ENABLED=0` 时不注册 |
| CST 07:00 | `ingest_late_vendors_cst` | kimi + apevon 专属（`LATE_VENDORS`，上游 T+N 出账慢，给 4 小时落账时间） |
| CST 23:00 | `ingest_recheck_cst` | 所有**非 openai** vendor 静默重跑昨天（trigger='recheck'，不走带告警的 retry wrapper、不推飞书；失败仍留 failed run 行）。上游白天补账后 UPSERT 覆盖——白天看到的坏数据可能 23 点自愈 |
| PT 01:00 | `ingest_openai_pt` | openai（PT 自然日，自动 DST）。每日回看重拉近 `OPENAI_LOOKBACK_DAYS=14` 天——`/costs` 出账后**持续补记数天**（实测 PT+1h 只有终值 1/2~1/4.5），UPSERT 幂等吸收漂移 |

job_defaults：`coalesce=True, misfire_grace_time=1`（>1s 错过即跳过不补跑；但每日窗口是补缺语义，挂一天第二天自动补回）、`max_instances=1`。

### Two Parallel Code Paths

- **Client** (`backend/*_client.py`): For real-time "today" queries in main.py, returns vendor service shape `{models, daily, total_cost}` for a time window
- **Adapter** (`backend/ingest/adapters/*.py`): For ingest cron, fetches one day at a time, translates to `ModelRow` for DB insert

Adapters are thin wrappers around clients but represent the **unified field semantics**. Clients can preserve historical quirks（client 内部算钱保留各家原始口径，adapter 出库前统一合并）。

### Database Schema (8 Tables, 3-Tier Materialization)

```
usage_daily_total         (1 row per day)                   ← Overview global chart
   ↑ SUM(vendor_id)
vendor_usage_daily        (1 row per day × N vendors)       ← vendor cards
   ↑ SUM(model)
vendor_model_usage_daily  (1 row per day × N vendors × M models)  ← Vendor detail page

vendor_apikey_usage_daily (1 row per vendor × day × api_key × model) ← 平行明细表,
                          详情页"按 API key 筛选"; 10/12 家经 adapter.fetch_apikey_rows 写入,
                          blueshirt/nulls 不实现该方法、由慢路径(apikey_slow.py)写入——
                          即 12 家 key 表都有数据; 未来新增的无 key 维度 vendor 才恒空、
                          前端 key 筛选器自动隐藏
agent_patrol_report       (自愈 Agent 巡检报告; cron 干净巡检不落行, 一巡最多一行)
vendor_meta               vendor metadata (display_name, currency, source)
vendor_ingest_run         task history (audit + manual retry)
vendor_ingest_state       per-vendor ingestion cursor (内部游标, 无人读它判起点)
```

DB 连接与 schema 隔离在 `ingest/settings.py`：schema 由 `BILLING_DB_SCHEMA` 控制（默认 `llm_usage_dashboard`，与同库的 LiteLLM 物理隔离）；alembic 版本表用独立名 `alembic_version_ingest`（别动 LiteLLM 的 `alembic_version`）；连接池刻意配小（pool 3 + overflow 2，10s 超时快速失败——PG max_connections=100 与其他应用共享，"连接池小"是有意设计）。

### Field Semantics (Critical for Adapters)

```python
# vendor_model_usage_daily / vendor_apikey_usage_daily 一行 = (vendor, day[, api_key], model)
prompt_tokens       = full input including cache (user perspective "input")
completion_tokens   = output
cache_read_tokens   = separately extracted (for cache column + cache pricing)
cache_write_tokens  = separately extracted
total_tokens        = prompt + completion (input already includes cache, don't add again)
request_count       = HTTP call count
image_count         = number of generated images
cost_native         = amount in upstream native currency
```

**NULL vs 0** (frontend displays accordingly):
- `cache_*` NULL → upstream doesn't expose → display "—"; 0 → genuinely no cache used
- `request_count` NULL → 上游不给该字段（kimi consumes 不返；grok 日常 ingest 只有 usd 指标；volcengine 仅套餐项行）→ "—"

Use `pack_input(prompt, cache_read, cache_write)` and `pack_total(prompt_full, completion)` helpers in adapters before writing to DB.

## 全局铁律（违反即数据事故）

1. **宁失败不编造**：上游返回未知枚举值（如 grok 新计费 unitType）、账期数据缺失、结果被截断（limitReached）→ 一律显式失败让人工确认，不猜映射、不当 0。分不清"真没数据"和"拉取失败"时，**宁可保留旧行不删**（key 表空 fetch 不 DELETE；tencent TextDetail 拉失败返空保旧值）。写 0 等于伪造"确实没用量"。
2. **总额权威、拆分估算**：上游只给总额、拆分拿不全时——总额写上游权威值，拆分按可得形状（定价权重 / usd 占比）估算，最大行（或末行）吸收舍入差，保证 **Σ拆分 == 总额**。落点：openai key 成本分摊、road2all key 聚合、grok token 回填、对账口径（应付(抓取)=DB cost 权威 vs 应付(计算)=单价×token 估算）。拆分估算永远不能反向覆盖权威总额。
3. **`utils.normalize_model_name()` 是唯一聚合键**：所有写 DB 的模型名（主表、key 表、慢路径、回填脚本——全库 client/adapter/slow 模块统一）必须先过它（剥 vendor 路由前缀/日期 tag/context size/preview 标签，保留功能与版本变体）。否则同一模型分裂成多行，live 与 DB 对不齐、Σkey≠Σmodel、定价匹配失败——全是静默错。例外（刻意不调）：grok（日期后缀是 xAI 官方命名）、billing_reconciliation 定价匹配（preview/-200k 标签对应不同价格，剥了会错配）。历史分裂行可用 `scripts/backfill_normalize_models.py` 合并，但要 dry-run 人工确认——正解仍是入口 normalize。
4. **pack_input 前先确认上游口径**：`pack_input` 只适用于 client 内部 prompt 是"非缓存输入"的口径。openai（key 维度行）、road2all（invoice 行）、wangsu（控制台）、tencent、volcengine 的 input 已含 cache，**再 pack_input 会重复计 cache**；apevon/kimi/bigmodel/ucloud 的 per-key prompt 是非缓存输入，**确实要 pack_input**。写 adapter 第一步就是确认口径。
5. **三层物化一致性**：任何绕过 `job._ingest_one_day` 直接 UPDATE 表的脚本（回填/修复/true-up）必须复刻同一条链：① model 表 SUM 回写 vendor 层 token/count 列（`grok_token_backfill._SUM_BACK_SQL` 是标准模板，注释原话"与 job._ingest_one_day 完全同款"）；② `_refresh_usage_daily_total` 重算全局日行；③ cost 列不走 SUM（Decimal 累加防浮点误差）；④ state 是否推进要显式决定。漏一步 = 三层数字互相矛盾且无告警。
6. **key 表是平行表，"主表行为完全不变"**：`fetch_apikey_rows` 的降级约定在 **adapter 层**实现（`job.py` 裸调、无兜底）——返回空 list = 不动旧行；异常必须自己 try/except 降级为空 list + WARNING（openai_adapter 是范例）。现状：openai/tencent/wangsu/kimi/bigmodel 五家有守卫，**grok / ucloud / volcengine 没有**（key 拉取异常会回滚当天整个事务拖垮主表——已知缺口）。key 标签必须稳定（openai 撞名 key 全部带 id 尾缀，不是只补后出现的，否则删一个 key 后幸存者 label 翻转、历史行身份分裂）。
7. **未文档化参数三步走**：① 先写独立探测脚本（`backend/scripts/probe_openai_apikey.py` 是范例）；② 代码注释标注"未文档化但实测可用 + 日期"（openai_client.py 头部探测注释是格式范例）；③ 调用方必须带失败回退、不拖垮主表。教训：openai `group_by` 字面量必须写 `api_key_id`，写 `api_key` 返 400——首轮探测因这个把"参数不支持"误判成"功能不存在"。
8. **游标双轨**：`vendor_ingest_state.last_ingested_date` 只是内部游标（greatest() 只进不退，backfill 跑旧日期永远无法把它倒退；apevon stat-fallback 是唯一显式回滚一天的路径），全库已无任何"起点"读者。一切新鲜度判定——scheduler cron 起点、router 手动触发默认窗口、agent 的 freshness_lag——统一走 `query.get_freshness()` = MAX(vendor_usage_daily.usage_date)（`/api/ingest/state` 的 synced_through 走 `get_data_range()`，同一张 fact 表）。**绝不读 state 判新鲜度**。

## 计价与对账 (billing_reconciliation.py)

对账 = 逐模型对比「应付(抓取) = DB `cost_usd`（上游系统算的扣费，权威）」vs「应付(计算) = 官方单价 × token（估算）」；偏差 = 抓取 − 计算；实付(计算) = 应付(计算) × 折扣；实付(账单) 线下账单无接口恒 None。入口 `GET /api/vendors/{vendor_id}/billing-reconciliation?start=&end=&api_key=`（main.py），前端 VendorDetail 页内嵌 BillingReconciliation 组件；传 api_key 查 key 表。

**定价查找链**（`get_model_pricing`，`_match` 标记 → API 的 `pricing.source` 四值）：
1. `db` — 本地 LiteLLM `model_prices` 表（手动维护、币种准确）：provider 精确 → (model, None) 默认 → 该 model 任意 provider（vendor_id 如 kimi 与定价 provider 如 moonshot 不一致时兜住）
2. `upstream` — GitHub LiteLLM 定价（`data/upstream_model_prices.json`，24h 缓存）：exact → 剥 openai-/volcengine- 等前缀 → 剥路径段（cloudflare/@cf/xxx/glm-5.2 → glm-5.2）
3. `normalized` — 规范化兜底：点号↔短横、去日期后缀；候选按 provider 分级；**同级价格冲突返回 None（宁缺勿错——防错配护栏，如 gemini preview 后缀剥了就错价）**
4. `default` — 全未命中，单价 NULL，前端显示 "-"

计费口径：DB 的 prompt_tokens 已含 cache，算钱要拆开——(prompt − cache_read − cache_write)×input价 + cache_read×cache_read价 + cache_write×cache_write价（cache_write 无单价回落 cache_read 价）+ completion×output价；任一维度缺失 → None 而非 0。

币种：全程 USD 计算（CNY 定价先 ÷ 汇率归一），与 cost_usd 可比后再 ×汇率转 CNY 展示。汇率 `exchange_rate.get_rates()`（open.er-api.com，`backend/cache/exchange_rate.json` 24h 文件缓存，失败回落 7.2）。折扣 `discounts.py`（`config/discounts.json`，vendor→系→模型三级就近覆盖，乘数语义，CRUD 挂 `/api/discounts`）。

**两套定价查找、优先级相反——有意设计，别统一**：
- `pricing_cache_service`（LiteLLM 用量分析、wangsu 计价）：upstream GitHub 优先，本地只 gap-fill
- `billing_reconciliation`（对账）：直查本地 DB 优先，upstream 只兜底
- 原因：混合层 upstream 优先会顶掉本地手动维护定价——kimi-k2.5 就是被 GitHub 条目顶掉导致币种算错。统一回 upstream 优先会复发该事故。

**定价数据维护（不要手改 JSON）**：`model_prices` 表是源，`data/model_prices.json` 是每次 CRUD 后自动导出的快照（DB 空时启动播种用）。改价走 `/api/litellm/model-prices` CRUD（x-api-key 鉴权，没有前端管理页；前端的"定价页" `PricingAdmin.vue` 编辑的是**腾讯** `config/pricing.json` 且该页面已不在路由上）。手改 JSON 对 DB 和对账不生效，且会在下次 CRUD 自动导出时被覆盖。新增条目：currency 必须写对（对账靠它做币种归一），note 写来源 URL + 核对日期 + 特殊计费规则。`upstream_model_prices.json` 是 GitHub BerriAI/litellm 原始 JSON 的 24h 自动缓存（落盘含全部条目类型，内存加载时过滤只留文本 token 模型），同样不要手改。

**tencent 未定价 SKU = 成本按 0**（只告警一次）：billing.py 里 Specification 拆出的 key 不在 `config/pricing.json` 时该模型成本按 0 计。2026-07 曾因键名抄错静默丢 8/10 模型成本。新增腾讯模型后看对账页该模型成本是否为 0、grep 日志"未定价"。

## Common Commands

### Local Development

```bash
# 本地起库（docker-compose.local.yml 未被 git 跟踪, 新机器需自建; start_local.sh/stop_local.sh 已不存在）
docker compose -f docker-compose.local.yml up -d     # postgres:15, 端口 5432, 数据在 ./data/postgres

# 后端
cd backend && python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium        # 仅浏览器登录型 vendor 需要
python -m uvicorn main:app --host 0.0.0.0 --port 8000

# 前端（dev 端口 3000, vite 把 /api 代理到 127.0.0.1:8000, 同源无 CORS）
cd frontend && npm install && npm run dev     # http://localhost:3000

# 停库
docker compose -f docker-compose.local.yml down
```

### Database

```bash
# Initialize DB tables
cd backend && source venv/bin/activate
python -c "from ingest.db import Base, engine; from ingest import models; Base.metadata.create_all(engine)"

# Run ingest migrations (独立版本表 alembic_version_ingest; 现有 3 个迁移, 最新 20260827_0003_agent_patrol。
# backend/migrations/ 与 ingest/migrations/ 的裸 SQL 是 alembic 之前的手工迁移, 已并入——
# schema 变更只新增 alembic revision, 别往 SQL 目录加文件)
cd backend/ingest && alembic upgrade head
```

### Testing

```bash
# 单测套件 backend/tests/（约 84 用例; 生产镜像不装 dev 依赖; conftest.py 自动给 DB_* 塞占位值, 不需要连库）
cd backend && source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest backend/tests -q
```

覆盖 agent 五件套（llm 循环兼容坑每条都有测试锁着 / patrol / report / signals / tools）、kimi client+adapter、newapi_direct client、凭据注入（`_inject_env_credentials`）。改这些模块前后应跑对应测试文件；新增兼容坑/护栏时同步补测试。

### Backfill & 数据修复

```bash
# 历史回填（逐 vendor 串行 + retry; 支持 --days 或精确窗口 + --dry-run; 日志同时进 stdout 和 data/logs/scheduler.log）
cd backend && source venv/bin/activate
python -m ingest.backfill --vendors=all --days=90
python -m ingest.backfill --vendors=openai --start=2026-01-01 --end=2026-05-01 --dry-run

# grok token 回填（月度例行, 见 Vendor Quirks grok 条目; 先 --dry-run 看分摊结果）
python -m ingest.grok_token_backfill --start=2026-08-01 --end=2026-08-25 --dry-run
python -m ingest.grok_token_backfill --start=2026-08-01 --end=2026-08-25

# Manual trigger via API（立即返回 {status:"scheduled"}, **不含 run_id**——run 行由后台任务创建;
# 前端/脚本轮询 GET /api/ingest/runs?vendor_id=bigmodel 拉最新 run。省略 start/end 时默认
# 窗口 = MAX(已入库)+1 → 昨天）
curl -X POST http://localhost:8000/api/ingest/vendors/bigmodel/trigger \
  -H "Content-Type: application/json" \
  -d '{"start": "2026-07-20", "end": "2026-07-28"}'

# Check cron scheduler status（独立进程里 _scheduler 必为 None, python -c 那套已不可用）
grep "ingest scheduler started" backend/data/logs/scheduler.log   # 启动时打印全部 job 与 next_run_time
```

### Vendor Login (Browser-Based Authentication)

Playwright 浏览器登录型（`LOGIN_TYPES = new-api(blueshirt) / kimi / apevon / bigmodel`）；nulls 和 road2all 走 .env 账密登录，不需要浏览器。

```bash
curl -X POST http://localhost:8000/api/vendors/bigmodel/login
curl http://localhost:8000/api/vendors/bigmodel/session
curl -X POST http://localhost:8000/api/vendors/bigmodel/logout
# 登录卡死/状态一直 waiting 的解药：cancel 会关浏览器 + 清残留锁文件（幂等）
curl -X POST http://localhost:8000/api/vendors/bigmodel/login/cancel
```

Session files stored in `backend/config/sessions/{domain}.json`。登录完成判据必须是"session 真的可用"而非"页面像登录了"（`login_flow._login_config`）：kimi OAuth PKCE 要等 `localStorage.token` 出现（只判 URL 快照太早，存下的 session 没 token 永远报未登录），登录带**必现图片验证码 → 半自动档**；apevon 门户+工作台双账号，要用 user.id + cookie 实调一次 stat 接口成功才保存，首页「登录」按钮弹 modal（SPA 挂载晚于 domcontentloaded，引擎对 open_login_js 轮询点击 15s）；bigmodel 走「账号登录」tab（默认停在短信 tab，`_OPEN_BIGMODEL_ACCOUNT_TAB_JS` 自动切换）+ **必现文字点选验证码——人工登录也弹，与自动化无关，机器无解** → 同为半自动（自动填表+提交，人只点验证码），URL 回 `/console/overview`。

**自动填表引擎**（`save_session._try_autofill`，2026-08）：配了账密才启用——等密码框 25s（kimi OAuth 跳转后出现）→ 启发式找用户名框（name/placeholder 中英关键词打分，`selector_hints` 可按站覆盖）→ JS 找提交按钮点/回车兜底。`open_login_js`（apevon 弹 modal / bigmodel 切 tab）按 1s 间隔轮询点击最多 15s（SPA 挂载晚于 domcontentloaded，一次 evaluate 会扑空）。失败降级：表单未命中或提交后 90s 未完成时，`manual_fallback=True`（agent/手动场景）浏览器留 310s 人工窗；`False`（02:30 预检）快速失败 + 飞书。浏览器启动带反自动化参数（`--disable-blink-features=AutomationControlled` + 抹 `navigator.webdriver`）——对风控评分型验证码能降概率，对 kimi/bigmodel 这种人人必现的无效。

### Production Deployment

```bash
# On server（正文目录 /data/llm-usage-dashboard; deploy.sh 里 SERVER_DIR 与 SERVER_HOST 均为
# 占位符——上服务器确认后统一, 别盲跑 deploy.sh）
cd /data/llm-usage-dashboard
sudo git pull
sudo docker compose up -d --build backend     # backend 是 build 镜像, 改源码必须 --build

# Check cron logs（compose v2 容器名不是 "backend", 用服务名 exec）
sudo docker compose exec backend tail -f /app/data/logs/scheduler.log

# Run backfill in dedicated container（该容器 bind-mount 整个 backend/, 宿主机改源码直接生效, 无需 rebuild）
sudo docker compose exec backfill python -m ingest.backfill --vendors=all --days=90
```

**生产拓扑**（全部 `network_mode: host`）：backend **3081**（uvicorn --workers 1，本地 dev 才是 8000）、前端 nginx **3080**（`/api/` 反代到 127.0.0.1:3081，`/litellm-ui/` 是构建进镜像的 React 版 LiteLLM 管理台）、noVNC **6080**（不是独立服务——backend 容器 entrypoint 里 Xvfb + x11vnc + websockify 起的虚拟桌面，Playwright 登录浏览器显示在这里）。entrypoint 启动时自动跑 LiteLLM 的 alembic 迁移；**ingest 的迁移要手动**。`.env` 是单文件挂载：`PUT /api/vendors/{id}/account` 直写宿主机 .env 即时生效，容器重启读到最新值。

**`.env` 键清单**（换机/排障必备；vendor 凭据键名见各 quirks 条目）：
```text
DASHBOARD_PASSWORD=  JWT_SECRET=            # 登录（都必配）
DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME=        # vendor 库 PostgreSQL（与 LiteLLM 共用连接参数）
POINTS_DB_HOST/PORT/USER/PASSWORD/NAME(/URL)=       # 积分 MySQL（独立库）
FEISHU_WEBHOOK_URL=                              # 失败告警，不配则不推
ALLOWED_ORIGINS=  INGEST_TOKEN=  INGEST_NODE_ID=
API_KEY=                                      # LiteLLM x-api-key。必配：后端无默认值, 未配则 /api/litellm/*
                                              # 一律 401 (fail-closed)。前端 api/index.js 与 litellm-ui-src
                                              # 默认发 'dev-only-change-me', 构建时用 VITE_LITELLM_API_KEY
                                              # 覆盖成同值; 键名极泛，别挪作他用
OPENAI_API_KEY=  GROK_MANAGEMENT_KEY=  GROK_TEAM_ID=  GROK_TIMEZONE=
TC_SECRET_ID=  TC_SECRET_KEY=                    # tencent
VOLCENGINE_ACCESS_KEY=  VOLCENGINE_SECRET_KEY=
WANGSU_ACCESS_KEY=  WANGSU_SECRET_KEY=  WANGSU_CONSOLE_COOKIE=   # cookie 过期 key 表停更
UCLOUD_PUBLIC_KEY=  UCLOUD_SECRET_KEY=  UCLOUD_PROJECT_ID=
ROAD2ALL_USERNAME=  ROAD2ALL_PASSWORD=  ;  NULLS_USERNAME=  NULLS_PASSWORD=   # 前缀约定仅这两家
AGENT_LLM_BASE_URL=  AGENT_LLM_API_KEY=  AGENT_LLM_MODEL=  AGENT_LLM_API_STYLE=  AGENT_PATROL_ENABLED=  AGENT_NOVNC_URL=
# 代码还支持的可选覆盖（当前未配、有默认值）：OPENAI_BASE_URL/OPENAI_PROXY/OPENAI_TIMEOUT、
# GROK_MGMT_BASE_URL/GROK_PROXY/GROK_TIMEOUT（注意代理/超时不是 GROK_MGMT_* 前缀，配错静默不生效）、
# AGENT_LLM_MAX_TOKENS/MAX_TURNS/TIMEOUT、AGENT_PATROL_MAX_ACTIONS/MAX_WINDOW_DAYS/VERIFY_TIMEOUT/DEADLINE
# 浏览器登录型（blueshirt/kimi/apevon/bigmodel）可选配 {VENDOR_ID 大写}_USERNAME/_PASSWORD
# （如 KIMI_USERNAME/KIMI_PASSWORD）——配上后 02:30 预检与 agent 登录走 save_session 自动填表
# （失败降级人工 noVNC），没配才纯人工；session 本体在 config/sessions/*.json
```

## Adding a New Vendor

Before starting, answer these 6 questions:

1. **Authentication**: API key? Cookie login (Playwright + CAPTCHA)? Username/password? 
2. **Data Interface**: per-day/per-model/per-token aggregation? Or raw logs requiring grouping?
3. **Field Completeness**: prompt/completion/cache_read/cache_write/request_count/cost — which available?
4. **Timezone**: billing splits by CST/UTC/PT?
5. **Limits**: QPS? Max window per request? Retention days? Pagination?
6. **Key 维度与 sanity 基准**: 上游能按 API key/token_name 拆分吗（有就实现 `fetch_apikey_rows`）？有独立的**轻量总额接口**可当 0 行 sanity 基准吗（有就实现 `fetch_upstream_total_cost`）？

**先探测再集成**：新接口维度先在 `backend/scripts/` 写独立 probe 脚本多轮实测（注意 scripts/ 里老脚本可能硬编码他人机器路径），对账验证 Σ 分组 == 总额后才写 client；探测结论以注释形式固化在 client 头部。

```bash
# Step 1: Write client (backend/your_vendor_client.py)
# Expose: fetch_vendor_usage(vendor, start_iso, end_iso) -> dict
# Return: {total_cost, models: {name: {prompt_tokens, completion_tokens, ...}}, daily: [{date, cost}]}

# Step 2: Write adapter (backend/ingest/adapters/your_vendor_adapter.py)
# Inherit VendorAdapter, implement fetch_one_day(day) -> list[ModelRow]
# - Use pack_input/pack_total, but FIRST确认上游 input 是否已含 cache（铁律 4）
# - 模型名过 normalize_model_name（铁律 3）
# - [可选] fetch_apikey_rows(day) -> list[ApiKeyRow]  写 key 平行表; 必须 try/except 降级空 list
# - [可选] fetch_upstream_total_cost(day) + has_stat_fallback  0 行 sanity 防线

# Step 3: Register
# - backend/ingest/adapters/__init__.py: ADAPTERS["your_vendor"] = YourVendorAdapter
# - backend/main.py: VENDOR_DISPATCH["your-type"] = fetch_your_vendor_usage
# - backend/config/vendors.json: add {"id": "your_vendor", "type": "your-type", ...}
#   (每项有 enabled 开关, 缺省 true: 设 false 则 live 查询/ingest/前端列表同时消失而 DB 历史保留——
#    临时下线出问题的 vendor 用它, 别删条目; 排查"某 vendor 不见了"第一步先看这个字段)
# - backend/.env: 凭据键名——{VENDOR_ID 大写}_USERNAME/_PASSWORD 前缀约定只适用于
#   new-api-direct 和 road2all 两类; 其余类型在 job.py:_inject_env_credentials 的 elif
#   分支里写死固定键名(OPENAI_API_KEY / GROK_MANAGEMENT_KEY / TC_SECRET_ID 等)
# - backend/ingest/job.py:_inject_env_credentials: add elif branch for injection

# Step 4: Backfill history（回填 key 表历史后跑一次 Σ key == 主表 天级对账排查）
docker compose exec backfill python -m ingest.backfill --vendors=your_vendor --days=30
```

After integration, the vendor automatically:
- Runs at 03:00/07:00 CST per LATE_VENDORS membership (no config needed)
- Appears in frontend vendor list (reuses VendorDetail.vue)
- Included in /api/overview aggregation

## Important Patterns & Gotchas

### Database Write Order (Critical)

In `job.py:_ingest_one_day`, single transaction per day:

1. UPSERT `vendor_usage_daily` (parent row first, model table has FK)
2. UPSERT `vendor_model_usage_daily` — prompt/completion/cache_read/cache_write 四列 **`COALESCE(新值, 原值)`：新值 NULL 不覆盖旧值**。这是为"快路径先写 cost、慢路径事后补拆分"设计的（23:00 recheck / openai 14 天回看 / 手动重跑每天都在重写老日期，没有 COALESCE 会把补好的字段抹回 NULL 且再无任务补）。推论：**想清掉某天的坏拆分数据，"重跑让 NULL 覆盖"永远不生效，必须先 DELETE 该日行**。注意 `total_tokens` 不在 COALESCE 列表里（会直接覆盖）
3. UPDATE `vendor_usage_daily` SET tokens = SUM(model) ★ 保持两层一致
3c. 若 `fetch_apikey_rows` 返回**非空**：DELETE 该 (vendor, day) 全部旧 key 行再 INSERT（幂等整段替换）；返回空 list（含降级）→ 不动旧行
4. UPDATE `usage_daily_total` (recalculate global for this day)
5. UPSERT `vendor_ingest_state.last_ingested_date = max(old, day)`

Any exception → transaction rollback, state doesn't advance, next cron resumes from `MAX(vendor_usage_daily.usage_date)+1`（触发入口统一按 fact 表取起点，不用 state）。

### 游标双轨与 Silent-Empty 防护链

- 新鲜度判定一律 fact 表（铁律 8）。`vendor_ingest_state` 只进（greatest）不出，唯一例外是 apevon stat-fallback 故意回滚一天。
- `fetch_one_day` 返 0 行 ≠ 失败（周末没人调）。sanity 链（仅当**整个窗口** 0 行时触发，多日 backfill 只验证末日）：优先调 `adapter.fetch_upstream_total_cost(end)`——stat=0 → 真无调用不报警；stat>0 且 `has_stat_fallback=True`（**目前仅 apevon**）→ stat 写 vendor 层 cost、model 行不写、state 回滚一天等慢路径补、run 仍 success 但 error_msg 留 info（前端黄 tag"上游空"，agent 的 silent_empty_hint 吃这个）；stat>0 无 fallback → hard fail；未实现 hook → 近 14 天日均 >¥10 启发式（弱）。**"success + error_msg 非空"是正式的半健康状态**，别当脏数据清。

### HTTP API 面总览

新增端点先对这张表，别重复造；改某组行为去对应文件（不在 main.py 里另起炉灶）：

| 路由组 | 文件 | 说明 |
|--------|------|------|
| `/api/overview`、`/api/vendors*`、`/api/usage*`、`/api/pricing` | main.py | overview 响应含 failed_vendors/freshness/missing_days/exchange_rates；vendors 下发走 SAFE_FIELDS 白名单（凭据不下发）；usage 支持 `?models=A&B` 多曲线、`?api_key=` 筛选；pricing 是腾讯定价表 |
| `/api/vendors/{id}/billing-reconciliation` | main.py → billing_reconciliation.py | 对账（见计价与对账节） |
| `/api/vendors/{id}/api-keys` | main.py | key 下拉列表（key 表里有行的 vendor 才有） |
| `/api/auth/*`、session/login/login-cancel/logout/account | main.py + login_flow.py | 登录与会话；main.py 只是 HTTP 壳 |
| `/api/ingest/*`（trigger/retry/runs/slow-fill/state/agent） | ingest/router.py + ingest/agent/router.py | trigger 异步；slow-fill 支持 blueshirt/nulls（dedicated）+ apevon（reingest），running 时 409 仅 dedicated 分支 |
| `/api/export` | ingest/export.py | CSV StreamingResponse，granularity=day/week/month，columns 走白名单 |
| `/api/analysis/*` | ingest/analysis.py | 7 个只读分析端点（wow-changes/budget/cache-hit/data-health/vendor-share/model-pareto/cache-by-series） |
| `/api/wangsu/models`、`/api/discounts` | wangsu_router.py / discount_router.py | 配置 CRUD |
| `/api/points` | points_router_v3.py（v1/v2/_optimized 已删除，2026-09 清理死代码+去硬编码凭据） | MySQL 积分预聚合 |
| `/api/litellm/*` | litellm/ 6 个 router | 独立 x-api-key 鉴权 |

### Authentication & Security

- Auth middleware (`auth.py`): Shared password + JWT cookie session。**`DASHBOARD_PASSWORD` 和 `JWT_SECRET` 两个都必配**，缺任何一个 login 直接 500。cookie httponly + samesite=lax + `secure=False`（http 内网假设）——**上 https 后要把 secure 改 True**，否则部分浏览器不回带 cookie。
- 白名单 `PUBLIC_PREFIXES` = `/api/auth/*`、`/api/litellm/*`（独立 x-api-key）、`/docs`、`/openapi.json`、`/redoc`；OPTIONS 预检与非 `/api/*` 路径直接放行。
- CORS：`allow_credentials=True` 时浏览器禁 `origins=*`，白名单从 `ALLOWED_ORIGINS` 读显式列表（默认仅 localhost:3080 + 5173；dev 前端 3000 走 vite 代理同源，不需要进白名单）。前端 axios 全局 withCredentials；跨域直连后端必须把 origin 加进 ALLOWED_ORIGINS。
- Credentials: `backend/.env`，由 `job.py:_inject_env_credentials()` 按 type 注入。Session files（`config/sessions/*.json`）never commit。账号切换 `PUT /api/vendors/{id}/account` 写 .env + 失效缓存 session + 追加 `data/audit.log`（只记用户名）。

### Split Window Optimization

Frontend queries split by `split_window(start, end)`:
- **Historical range** (before today): Single SQL query to `vendor_usage_daily`, millisecond response
- **Today range**: Concurrent fan-out to upstream vendor APIs, second response
- Results merged via `merge_results(db_result, live_result)`

This is why cron ingestion runs for "yesterday" — today's data always comes from live API anyway.

**API key 筛选**（`/api/vendors/{id}/usage?api_key=`）：只过滤 DB 段（走 key 平行表）；**live（今天）段不支持 key 过滤，选了 api_key 时 live 段整体跳过**——响应里没有今天的数据是预期行为，不是 bug。前端 key 筛选结果放局部 ref，不写共享缓存（防止清掉筛选后显示旧 key 数据）。

### Vendor-Specific Quirks (12 家)

- **openai**: PT 01:00 独立 cron + 14 天回看（见 cron 表）。Admin API 两个服务端硬坑：① `bucket_width=1d` 时服务端硬卡 **31 buckets/页**，`limit>31` 时 `/costs` 实测返空 data（不报错）——PAGE_LIMIT=31 + cursor 翻页，改大 limit 静默拿 0 数据；② `/costs` **不能传 end_time**——传了之后部分 bucket 的 results 被服务端吃成空（同窗口 only-start $717 / 加 end_time 全空），客户端只传 start_time 再自己按 bucket.start_time 双侧截断。API key 筛选（`fetch_apikey_rows`）：`group_by=api_key_id`（字面量必须 `api_key_id`，写 `api_key` 会 400——2026-08 探测踩过坑）一次调用拿全——usage 双维 `["model","api_key_id"]`、costs 单维；分组行完整划分日总额（对账 Σ==总额）。key 名从 `/organization/projects/{id}/api_keys` 解析（TTL 1h 缓存），查不到名的 id（admin key/已删 key）归"(未归属/已删除 key)"合成行；有成本无 completions 用量的 key 补"(非 completions 用量)"合成行；成本按 model 定价权重分摊（总额权威、拆分估算）。adapter 层 try/except 降级。边界：cron 只回看 14 天，**更早历史日 key 行降级一次即永久缺失（backfill 不报错），回填后应跑 Σ key == 主表 天级对账**。

- **tencent**: Text/Image 两次接口。`Specification` 维度后缀（`_input`/`_output`/`_cacheinput`）拆 token 类型；`billing.py:calculate_billing()` + `config/pricing.json` 算价；**未定价 SKU 成本按 0**（见计价与对账节）。key 维度 = `AigcType=TextDetail` 逐请求明细（每行**脱敏** ApiKey，如 `TgawD3m1****`）按 (ApiKey, model) 聚合；**聚合接口的 APIKey 过滤只认完整 key 值，传脱敏串返 0 条**——只能全量翻明细自己聚；生图无 Detail 接口（key 表只覆盖生文）；明细只有 display 模型名，高档位（_272/200）按标准档近似（实测 <0.5% 误差）。

- **volcengine**: 双源 = `GetInferenceUsage`（ark 推理统计，**主源**）+ `ListAmortizedCostBillDaily`（账单，仅成本）。关键坑：调 GetInferenceUsage 必须带 `Filters:[{"Key":"ModelName","Value":"*"}]` 才触发按模型分组（Value 内容随意，起分组开关作用），不带只回账户级 1 行/天；返回与官方控制台一致且**含套餐内消耗 token**（曾用 Bill 算 token 比控制台少 17%，套餐内消耗被吞——CSV 路径已整段删除，"Bill+CSV 双源"是历史）。套餐项（Coding-Plan/免费包）只在账单出 cost、token/count 全 NULL 作独立 model 行。billing 接口全局 5 QPS 令牌桶 + FlowLimit 退避（ark 接口不吃这个限流）。key 维度：Filters 再加 ApikeyID 即得 ApikeyID×ModelName 双维一次调用；账单不支持按 key 拆 → 按官方单价加权分摊（尾差归末行 Σ==账单）；空 ApikeyID（ep-xxx 接入点/AuthToken）归"(未关联Key)"，sid 经 ListApiKeys 映射可读名、重名加 SID 尾缀。

- **road2all**: Invoice API 返回**多种 `type`**（TOKEN / GPT / GPT_TIER / CLAUDE / GEMINI_TIER / GEMINI_IMAGE…）——官方账 = 全部 type 求和，只聚合 TOKEN 会漏 ~90% 成本（2026-08 对账事故）。行级 `inputTokens` 口径跨 type 不一致（tier 行含 cacheRead、CLAUDE 行不含）——完整 input 一律从 `usage.inputUncached + cacheRead + cacheWrite(5m+1h)` 推导。Login 限流（session 缓存 25 min）。key 维度 = invoice 行的 `account`（上游子账号名，一账号一把 key；type=TOKEN/DETAIL/KEY 变体均 10002 拒绝——这是唯一 key 粒度）；`fetch_apikey_rows` 与 `fetch_one_day` 用同实例 per-day memo 共享同一次拉取（同口径、不重复请求）；account 缺失行跳过（实测 2025-12 起行行都有，Σkey==总额是经验成立）。

- **wangsu**: 不返回全模型列表——模型清单经 `/wangsu/models` CRUD 手工维护（前端）。**上游不返回成本**：client 用 `wangsu_pricing.py:compute_cost_cny`（走 pricing_cache_service 的 upstream 优先定价链）本地算 CNY，**匹配不到定价的模型 cost 静默按 0**——新增模型后确认该模型成本非 0（为 0 = 模型名没命中 LiteLLM 定价，同类陷阱见 tencent）。按模型 fan-out，**8 并发上限（>10 触发错误码 446）**。key 维度在独立**控制台 API**（`wangsu_console_client.py`，cookie 来自 `.env WANGSU_CONSOLE_COOKIE`，筛选配置 `wangsu_tokens.json`）——open API 完全没有 key 维度。Console session 会过期；从浏览器复制新 cookie，否则 key 表停更（主表不受影响）。

- **blueshirt**（type=new-api）: CAPTCHA 登录需 Playwright + noVNC 6080 人工过码。快路径 `/api/data/self` 1s 出 cost/total 但无 token 拆分；慢路径 `/api/log/self` 字段全（page_size=10000 上游硬上限，一天约 14 页，慢页 40s+，单天预算 60min 超时即弃；"1500 页"是旧 page_size=100 时代的数字勿再引用）。快成功后同线程串行接慢路径（trigger='slow-fill' 建 run 行前端可见）。慢路径两个守卫（**仅 blueshirt**，nulls 相反——见下）：① 翻页 **1-based**，p=0 被服务端钳到第 1 页，从 0 翻会把第一页抓两遍（历史数据曾虚高约一页）；② 翻页 `complete=False`（超时/单页失败）→ **放弃 key 表写入**（残缺数据不换完整数据）。慢路径翻日志时顺手按 (token_name, model) 聚合写 key 表（`apikey_slow.py`）——跑一天补拆分+key 两个产出。retention：护栏按保守 **13 天**（agent 硬编码），日志窗口实测可达 ~22 天（apikey_slow 注释）；过窗永久丢失。抢救：`backfill_blueshirt_slow.py`（带完整性守卫）。

- **nulls**（type=new-api-direct，xhub）: 与 blueshirt 同为快/慢双路径，但**账密登录**（`.env NULLS_USERNAME/_PASSWORD`；XHUB_* 是旧名残留不用），慢路径模块 `newapi_direct_slow.py`（与 blueshirt_slow.py 按 session 来源拆分，job._SLOW_FILL_DISPATCH 与 /slow-fill 自动分发）。与 blueshirt 相反的两点：翻页 **0-based**（p=0 就是第一页，无钳位）；`complete=False` **照写** key 表——xhub 日志按 row 数 LRU 滚动，高吞吐账号当天就顶掉旧数据，complete=False 是常态，"只有日志就写"（DELETE+INSERT 幂等，cron 每天更新）。retention 红线同 blueshirt：错过窗口历史永久无法恢复。历史抢救脚本 `backfill_apikey_slow.py` **只跑 nulls**（别拿它救 blueshirt）。

- **kimi**: 无 request_count（上游不提供）。金额 10^-5 CNY 单位（÷100000）。token 由定价表从成本反推（LiteLLM 价优先，FALLBACK_PRICING 兜底）。出账 T+N → 07:00 批次。key 维度 = api_key_name。

- **apevon**（codingflow）: CAPTCHA 登录需 noVNC。`has_stat_fallback=True`（全仓库唯一一家）——`/api/statistics/` 偶尔漏聚合返空而 `/api/log/self/stat` 总额接口稳，0 行 + stat>0 时 stat 写 cost、state 回滚一天等重拉（见 Silent-Empty 节）。slow-fill 走 reingest（`_SLOW_FILL_REINGEST`，复用主入库 trigger='slow-fill'；**没有 apevon_slow 模块**，adapter 里的旧注释别信）。key 维度 = token_name。

- **ucloud**: key 维度 = 订单 **ResourceName**（用户起的 key 名）优先，没名字才退回 ResourceID。注意其 fetch_apikey_rows 目前无 try/except 守卫（铁律 6 已知缺口）。

- **bigmodel**: 微信扫码登录（Playwright）。数据源 `/api/finance/expenseBill/expenseBillListByDay`（按天×模型×tokenType）。费用**取 `originalAmount`（原价），不取 `settlementAmount`**（资源包抵扣后可为 0）——主表与 key 表同口径保证 Σkey==主表，改成 settlement 全部历史成本口径漂移。"缓存写入" tokenType 限时免费 → 账单从不出现该行 → **cache_write 记 NULL 不伪造 0**（有 cache_read 必有写入发生）；官方开始计费后该行自然出现真值。key 维度 = 账单行自带 apiKey。

- **grok**: xAI **Management API**（`management-api.x.ai`），需独立 Management Key（`GROK_MANAGEMENT_KEY`，console.x.ai → Settings → Management Keys——不是推理 API key；team id 默认 "default"，`GROK_TEAM_ID` 覆盖）。usage analytics `POST /v1/billing/teams/{id}/usage` 按 (description 计费项, day) 出 **USD**；日桶跟随 `GROK_TIMEZONE`（默认 Asia/Shanghai → 走 03:00 主批次）。**token 指标只有 usd 是文档化的**——候选指标名仍走进程内首调探测（grok_client.py），但 2026-08-27 实测 token 指标名全部 400，实践中总是回退 usd-only（token 列 NULL 是预期，cost 恒有值）。**token 精确数据来自发票明细**：`GET /invoices`（已结账期，最终权威）与 `/postpaid/invoice/preview`（当前账期随用随出）的 lines[] 带 (model, unitType) 的精确 numUnits；**账期 = UTC 自然月**（与 GROK_TIMEZONE 日桶并存两套时区，边界 8h 漂移）；lines 的 amount 单位 = USD **cents**（÷100）；`unitType=prepaid_tokens` 是充值凭证必须滤掉。由 `ingest/grok_token_backfill.py` 用**有效单价法**回填（账期 usd ÷ 账期 token = 内含长上下文档位混合的有效单价，日 token = 日 usd ÷ eff_price → 账期合计精确差 <0.1%）；字段映射 prompt = Prompt text+image+Cached（完整输入）、cache_read = Cached、completion = Completion+**Reasoning**（思考按输出计价）；白名单 5 个 unitType 字符串（Prompt text/image、Cached prompt、Completion、Reasoning），**出现集合外字符串立即 FAIL**（宁失败不编造）。脚本只 UPDATE token 列 + 置 has_cache_detail=true + 刷全局层，cost/run_id/state 全不动；key 行 token 按 (model, day) 内各 key usd 占比分摊（最大余数法，Σkey=model）。**月度例行**：账期出票后跑一次定稿（当前账期 preview 分摊自洽但非终值）。**重灌冲突**：对已回填的天重跑 ingest 时 4 个拆分列靠 COALESCE 保留，但 `total_tokens` 不在 COALESCE 列表会被抹回 NULL、`has_cache_detail` 被打回 False、key 表 DELETE+重插 token 全 NULL——三种丢失都靠重跑回填脚本修复。key 维度：groupBy api_key_id 未文档化但实测可用（groupLabels 直接带人读 key 名），只有 usd → 日常 key 行 token NULL，靠回填脚本 true-up。计费项描述剥 "Chat/Image/Embedding/Function/API " 前缀得模型名（日期后缀是官方命名保留）。

### Scheduler & Concurrency

- **Assumes single uvicorn worker**（entrypoint --workers 1）。Multiple workers duplicate cron + 挤爆共享 PG 连接池。
- **03:00 / 07:00 分批**：LATE_VENDORS（kimi/apevon，T+N 出账）单独 07:00，给 4 小时落账；03:00 只跑 9 家。
- **并发安全**：不同 vendor 不同域名无限流冲突；每 adapter 内部自控并发；`vendor_ingest_run.uniq_running_per_vendor` 部分唯一索引防同 vendor 并发（最终兜底）。
- **Zombie cleanup**：backend 启动时把 running >2h 的 run 标 failed（`mark_stale_running_failed`，agent 巡检复用）。agent 的 zombie_run 信号**排除 trigger='backfill' 的 run**（backfill 合法跑数小时；误杀会解除并发锁导致双进程并发抓取）。
- **重试链**：`run_ingest_with_retry` 共 **4 次尝试**（初始 + 3 重试），退避 5min/30min/1h（退避合计 ~95min，加执行时间——巡检时点排在 ~100min 后就是这个原因）。agent 动作只调 `run_ingest(trigger='agent')`，绝不走 retry wrapper。

### 前端实况（防改死文件）

实际挂路由的页面：`/login`(public)、`/`=Overview、`/vendor/:id`=VendorDetail、`/analysis`、`/export`、`/points`、`/discounts`、`/agent`=AgentPatrol、`/litellm`。

**孤儿文件，改了不会有任何效果**：`views/Dashboard.vue`、`views/PricingAdmin.vue`（腾讯时代旧页面，未挂路由）、`views/api切换`（0 字节垃圾）——均可删。（points_router v1/v2/_optimized 已于 2026-09 删除。）

- `store/vendorCache.js` **不是 Vuex**（项目无 vuex/pinia）：sharedDateRange 全局日期 Overview 写入、VendorDetail/Export 进页取快照（本地改动不回写）；Overview 改日期 clearAllCaches() 全部失效。
- `/litellm` 路由的 LiteLLM.vue 只是 4 行占位 stub——真身在 App.vue：路由以 /litellm 开头时隐藏导航、显示 iframe（src=/litellm-ui/index.html，React 版管理台构建进 nginx 镜像）。
- 前端两套 axios 实例：MUD API（cookie 鉴权）与 LiteLLM API（独立实例自带 x-api-key header）——别混用（cookie 打 litellm 端点 401）。
- 401 双保险：axios 拦截器跳 /login（防死循环）+ router.beforeEach 调 /api/auth/me。

### Self-Healing Agent (`ingest/agent/`)

Patrol agent that closes the loop after crons: collect fault signals → LLM diagnosis（**双协议** tool-calling 循环，OpenAI 兼容 `/chat/completions` 或 Anthropic 兼容 `/v1/messages`）→ safe remediation → verify → one consolidated Feishu card. Deterministic code owns everything safety-critical; the LLM only decides whether/what to remediate and writes the diagnosis.

- **Schedule**: CST 05:00 / 09:00 / 19:00（排在各 cron 完整重试链 ~100min 之后；23:00 recheck 后不巡——该层故意静默，次日 05:00 评判）。`AGENT_PATROL_ENABLED=0` 关闭。Manual: `POST /api/ingest/agent/patrol`（**替换语义**——已有巡检在跑则请求取消旧的并排队启动新一轮；报告行在响应前同步建好，前端触发后立即可见）。`POST /api/ingest/agent/patrol/cancel` 取消当前+排队的巡检（协作式：当前步骤 ≤3min 结束后停，收尾 status='cancelled'，已触发动作后台跑完）。Frontend page at `/agent`。**cron 干净巡检直接退出不落报告行**（零噪音）；手动触发即使干净也落 status='clean' 行。startup 清理重启遗留的 running 报告行（`report.mark_stale_running_failed`）。
- **Signals** (`signals.py`, deterministic, zero LLM cost): failed_run / missing_days / zombie_run（排除 backfill run）/ freshness_lag（openai 对 PT 昨天，T+1 vendor 允许滞后 2 天）/ slow_path_lag / session_expired / silent_empty_hint。
- **快照预嵌（弱模型鲁棒性的关键，别当冗余数据删）**：signals.py 收集信号时确定性预取每个信号 vendor 最近 3 次 run（`recent_runs`）+ 登录型 vendor 的 session 状态，直接嵌进快照；llm.py 把完整快照嵌进循环第一条 user 消息。正常情况 LLM 一轮读工具都不用（确需完整 traceback 才调 get_run_detail）。删掉 recent_runs 的后果：多 vendor 巡检必走强制收尾、报告质量降级（forced），只有收尾也失败才降级——弱模型会一家一轮地读工具烧光轮次。
- **Guardrails** (`tools.py`, code is law): actions only on vendors present in signals; 1 action per vendor per patrol; global cap `AGENT_PATROL_MAX_ACTIONS=4`; window ≤`AGENT_PATROL_MAX_WINDOW_DAYS=14`（blueshirt 硬编码 13）; end ≤ vendor-yesterday; running-run vendors rejected; `clear_zombie_run` 只允许清**本轮僵尸信号里出现过的 run_id**（白名单二次校验）；actions call `run_ingest(trigger="agent")` directly——never `run_ingest_with_retry`（sleep 最长 ~90min；"retry" = next patrol slot）。`uniq_running_per_vendor` 是最终兜底。
- **动作异步提交与验证**：动作经模块级 ThreadPoolExecutor(max_workers=2) 异步提交——报告发出后动作在自己的线程继续跑。验证防两类隐性失败：**submit_error**（run_ingest 建 run 行之前就抛错，如并发 IntegrityError——只有轮询 Future 才能发现）；**旧 run 误归因**（run_id 没抓到时只认 submitted_at − 60s 容差之后新建的 agent run）。验证三态：ok=False（明确失败→升级）、ok=None（still_running / 等人工登录——**未定态不算失败不升红**，下一轮收尾）、ok=True。轮询 30s，单动作 `AGENT_PATROL_VERIFY_TIMEOUT=600s`，全局 `AGENT_PATROL_DEADLINE=1500s`。
- **LLM 配置**：`AGENT_LLM_BASE_URL`（端点**前缀**，如 `https://open.bigmodel.cn/api/anthropic`，代码自己拼 `/v1/messages` 或 `/chat/completions`）/ `AGENT_LLM_API_KEY`（回落 `OPENAI_API_KEY`）/ `AGENT_LLM_MODEL`（默认 gpt-4.1-mini）/ `AGENT_LLM_API_STYLE=anthropic|openai`（缺省按 BASE_URL 含 /anthropic 自动判断）/ `AGENT_LLM_MAX_TOKENS`（默认 16384，**仅 Anthropic 路径生效**；glm-5.x 思考型模型 thinking 计入输出，勿调小）/ `AGENT_LLM_MAX_TURNS=8` / `AGENT_LLM_TIMEOUT=120s`（思考型模型单轮 1~2 分钟，超时先调这里）。GLM coding plan 订阅只覆盖 Anthropic 端点，v4 端点按量付费——同一把 key 两个端点两种额度池。轮次耗尽先走**无工具强制收尾**（`_forced_final_report`，llm_meta 标 forced=True，已执行的动作仍有效），收尾也失败才走降级。
- **LLM 循环兼容坑**（llm.py，零依赖手写 httpx）：content-only 轮回显 assistant 消息**绝不能带 `tool_calls=[]` 空数组键**（部分 OpenAI 兼容 API 直接 400）；连续 2 轮无 tool_call 且未 submit_report → LLMError 走降级；tool 结果 JSON 截断 4000 字符；循环内部始终维护 OpenAI 格式历史、Anthropic 出入两侧转换；不传 temperature（思考型模型有限制）；同时发 x-api-key 和 Authorization Bearer 两个头（GLM 兼容）。
- **降级模式是强制行为**：无 key / API 错 / 循环失败 → 确定性信号卡片 + 固定建议，`status='degraded'`（高危信号则 escalated）；巡检顶层 try/except，cron 永不因 agent 报错。
- **Persistence**: `agent_patrol_report` 表（status running/clean/healed/degraded/escalated/error；signals/actions/verification JSONB；fingerprint = 信号集合 sha1，连续同指纹加"连续 N 轮未修复"警示）。
- **登录修复**：`session_expired` → `request_login`（2026-08 从 request_human_login 改名）→ `login_flow.start_login_async()`。配了 `.env` 账密 → 自动填表，工具内 `wait_login_result(180s)` **当场确认成功**；未配/自动失败 → 浏览器留 310s 人工经 noVNC 6080 过码（`AGENT_NOVNC_URL` 展示在卡片）。02:30 预检 cron 先自动登录配了账密的（每 vendor 每天 1 次，失败推飞书）。
- **双份知识库**：`prompts.py` 的 SYSTEM_PROMPT 内嵌中文版供应商排障知识库（kimi/apevon T+1、openai PT 日切、blueshirt retention、volcengine 限流勿立刻重试等）。**改 vendor quirks 时 CLAUDE.md 与 prompts.py 两处都要同步**——知识库过期会让 LLM 诊断方向性错误。

## File Structure

```
backend/
  *_client.py           # Vendor clients for real-time queries (命名例外: tencent=tc_api.py, blueshirt=newapi_client.py, wangsu 控制台=wangsu_console_client.py)
  save_session.py       # 活代码! login_flow 直接 import (自动填表登录在里面), 别当一次性脚本清理
  backfill_*.py / scrape_logs.py / probe_earliest.py / scripts_verify/
                        # 根目录历史一次性脚本 — 别跑别照着改; 现行回填一律 ingest/backfill.py
                        # (backfill_wangsu_keys.py 仍可用: 只补 wangsu key 表不跑主表)
  main.py               # FastAPI app, 散落端点, VENDOR_DISPATCH
  vendors.py            # Load vendors.json, get_vendor()
  billing.py            # Tencent-specific billing calculation (config/pricing.json)
  billing_reconciliation.py  # 账单对账 (定价查找链 + 应付/实付偏差, 见计价与对账节)
  exchange_rate.py      # USD 汇率 (24h 文件缓存, 失败回落 7.2)
  discounts.py          # 折扣配置 (config/discounts.json 三级就近覆盖)
  login_flow.py         # 浏览器登录编排 (main.py 端点只是 HTTP 壳)
  auth.py               # JWT session middleware
  utils.py              # normalize_model_name — 唯一聚合键 (铁律 3)
  points_router_v3.py   # 积分 (MySQL)
  config/
    vendors.json        # Vendor list with credentials placeholders
    pricing.json        # Tencent pricing table
    discounts.json      # 折扣 (vendor→系→模型)
    sessions/           # Playwright browser sessions (gitignored)
  data/
    model_prices.json       # LiteLLM model_prices 表的导出快照 (DB 为源, 别手改)
    upstream_model_prices.json  # GitHub LiteLLM 定价 24h 缓存 (别手改)
  scripts/              # 一次性探测/修复脚本 (先探测再集成的方法论)
  ingest/
    settings.py         # DB 连接/BILLING_DB_SCHEMA/alembic 版本表隔离
    adapters/           # Vendor adapters for cron ingestion
    scheduler.py        # APScheduler cron (9 jobs, 见 cron 全景表)
    job.py              # Core ingestion logic, retry, alerting, COALESCE 语义
    query.py            # split_window, get_freshness, get_data_range
    router.py           # /api/ingest/* endpoints
    db.py / models.py   # SQLAlchemy session / 8 张表定义
    backfill.py         # Manual backfill (--vendors/--days|--start/--end/--dry-run)
    blueshirt_slow.py   # Blueshirt slow-path补拆分 (Playwright session)
    newapi_direct_slow.py  # nulls/xhub slow-path (账密 session)
    apikey_slow.py      # 慢路径按 token_name 聚合写 key 表 (共享模块)
    grok_token_backfill.py  # grok token 发票回填 (月度手动脚本)
    analysis.py         # /api/analysis/* 横向分析 (7 端点, 只读)
    export.py           # CSV 导出
    alert.py            # 飞书失败告警
    alembic/            # ingest 迁移 (独立版本表)
    agent/              # Self-healing patrol agent (signals/llm/tools/report/patrol)
  litellm/              # LiteLLM submodule (independent architecture, 独立 x-api-key)
  docker-entrypoint.sh  # Xvfb+x11vnc+websockify(6080) + uvicorn 3081 + litellm alembic

frontend/
  src/
    views/              # 页面组件 (Dashboard.vue/PricingAdmin.vue 是孤儿, 见前端实况)
    components/         # BillingReconciliation.vue / WangsuModelPicker.vue / CostChart.vue
    api/index.js        # 两套 axios 实例 (cookie / x-api-key)
    router/             # Vue Router config
    store/vendorCache.js  # 共享日期+vendor 缓存 (非 Vuex)

docker-compose.yml      # Production: backend(3081,build) + backfill(bind-mount 热更新) + frontend(3080)
docker-compose.local.yml # Local: postgres only (未被 git 跟踪)
```

## Code Style

- Backend: FastAPI + SQLAlchemy async where beneficial, sync for cron simplicity
- Frontend: Vue 3 + Vite (dev 端口 3000)
- Database: PostgreSQL (schema llm_usage_dashboard) for vendor data, MySQL for points feature (separate concern)
- Match existing patterns when adding vendors — see `backend/ingest/adapters/` for examples
- Use `pack_input()`/`pack_total()` only after confirming upstream input semantics（铁律 4）; preserve NULL vs 0 distinction

## Troubleshooting

| 症状 | 原因 / 解法 |
|------|------------|
| Overview 提示 "X vendors have un-ingested days" | `get_missing_days_in_window` 检测到缺口 → vendor 详情页点"同步至昨天"按钮或 curl trigger（异步，轮询 GET /api/ingest/runs?vendor_id=…） |
| Vendor "last sync" 卡住几天 | 僵尸 run 卡 running → backend 重启自动清理，或手动 `UPDATE vendor_ingest_run SET status='failed' WHERE vendor_id='…' AND status='running'` |
| openai 某天数据比平时少一半 | CST 白天手动触发时 PT 日未结束 → 等 PT 01:00 cron 14 天回看自动覆盖，或手动重触发 |
| blueshirt 有模型但 prompt/cache 全 NULL | 慢路径没跑成 → 详情页"慢路径滞后"警告条里的"补昨天"/"补指定天"按钮（仅 retention 窗内，护栏 13 天）；过窗永久无法恢复 |
| grok token/request 列全是 "—" | **预期**（analytics 只有 usd）→ 跑 `python -m ingest.grok_token_backfill`（月度例行）；重灌过 grok 某天后 token 又没了也是它修（total_tokens/has_cache_detail/key 行被重灌抹掉） |
| 重跑想清掉某天的坏拆分数据"不生效" | UPSERT 的 COALESCE：NULL 不覆盖旧值 → 必须先 DELETE 该日行再重跑 |
| Cron 跑了但数据是 0 | 上游 T+1 延迟 → 查 scheduler.log 确认 cron 执行；03:00 拿空可能是 LATE_VENDORS（等 07:00）或上游慢出账（等 23:00 recheck 自愈） |
| 白天看到的坏数据 | 可能 23:00 recheck 自动覆盖修正——第二天再看，仍在再排查 |
| key 筛选下拉没了 / 某天 key 行缺失 | console cookie 过期（wangsu）或 openai >14 天回填降级（永久缺失）→ 查 Σ key == 主表 天级对账；wangsu 复制新 WANGSU_CONSOLE_COOKIE |
| 登录一直卡 "waiting" | `POST /api/vendors/{id}/login/cancel` 清残留锁（幂等），再重新 login |
| points 某天没数据 | 03:30 cron 错过**不自动补** → 手动跑 `init_points_summary.py` / `init_points_daily_total.py` |
| 改了 backend 源码线上没生效 | backend 是 build 镜像要 `--build`；backfill 容器 bind-mount 热更新（对比着改会误判"没修好"） |
| 没有飞书失败告警 | `FEISHU_WEBHOOK_URL` 未配，或重试链未走完（4 次尝试、退避 5/30/60min、全程 ~95min+——按这个时间线等） |
| 本地起服务登录 500 | `DASHBOARD_PASSWORD` / `JWT_SECRET` 缺配（两个都必配） |

---

**Note**: `litellm/` submodule and points feature are architecturally separate from the vendor usage system. They share the FastAPI app but use different databases and have no cross-dependencies.
