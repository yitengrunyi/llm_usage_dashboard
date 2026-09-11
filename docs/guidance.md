# LLM Usage Dashboard - 开发指南

## 1. 项目定位

**多云厂商 AIGC 用量和计费聚合仪表盘**

- **核心价值**: 统一查询 11 个云平台（腾讯云、OpenAI、Kimi、火山引擎等）的 LLM API 用量和费用
- **双时间窗口策略**: 历史数据从 PostgreSQL 毫秒级响应，今日数据实时调用各厂商 API（秒级）
- **双币种显示**: 所有金额同时显示 USD 和 CNY，自动汇率转换
- **定时入库**: 每天凌晨 3 点通过 cron job 并行拉取 10 个厂商昨日数据入库（OpenAI 独立在太平洋时间 01:00 运行）

**关键设计理念**: 这不是传统的统一 SDK 项目。每个上游厂商的 API 千差万别，核心工作是编写特定厂商的适配器（adapter），将它们翻译成统一的字段语义。

## 2. 启动入口

### 本地开发快速启动
```bash
# 一键启动（创建 docker-compose.local.yml，启动 postgres，安装依赖）
./start_local.sh

# 后端会自动启动在 http://localhost:8000
# 前端会自动启动在 http://localhost:5173
```

### 手动启动

**后端**（FastAPI + uvicorn）:
```bash
cd backend && source venv/bin/activate
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```
- 入口文件: `backend/main.py`
- API 文档: http://localhost:8000/docs
- 启动时会初始化 LiteLLM 模块和定时调度器（`ingest.scheduler.start()`）

**前端**（Vue 3 + Vite）:
```bash
cd frontend && npm run dev
```
- 入口文件: `frontend/src/main.js`
- 路由定义: `frontend/src/router/index.js`

### 生产部署
```bash
cd /data/llm-usage-dashboard
sudo docker compose up -d --build backend
```

## 3. 核心目录职责

### Backend 结构

```
backend/
├── main.py                    # FastAPI 应用入口，路由注册，VENDOR_DISPATCH 映射
├── *_client.py               # 各厂商客户端（实时查询"今日"数据）
│   ├── openai_client.py      # OpenAI
│   ├── kimi_client.py        # Moonshot Kimi
│   ├── volcengine_client.py  # 火山引擎
│   ├── wangsu_client.py      # 网宿
│   ├── bigmodel_client.py    # 智谱 GLM
│   └── ...                   # 其他 7 个厂商
│
├── ingest/                   # 定时入库核心模块（cron + 历史数据查询）
│   ├── scheduler.py          # APScheduler 定时任务定义（凌晨 3 点触发）
│   ├── job.py                # 入库主循环，事务管理，重试逻辑
│   ├── adapters/             # 厂商适配器（统一字段语义）
│   │   ├── base.py           # VendorAdapter 基类，ModelRow 数据类
│   │   ├── openai_adapter.py # 各厂商适配器实现
│   │   ├── tencent_adapter.py
│   │   └── ...
│   ├── models.py             # SQLAlchemy 表定义（6 张表）
│   ├── query.py              # 数据库查询辅助，split_window 分窗逻辑
│   ├── router.py             # /api/ingest/* 端点（手动触发、状态查询）
│   ├── backfill.py           # 历史数据回填脚本
│   └── db.py                 # SQLAlchemy session 管理
│
├── config/
│   ├── vendors.json          # 厂商配置列表（id、type、currency、base_url）
│   ├── pricing.json          # 腾讯云定价表
│   └── sessions/             # Playwright 浏览器会话（gitignored）
│
├── litellm/                  # LiteLLM 子模块（独立架构，MySQL 数据库）
├── points_router*.py         # 积分功能（MySQL，与主业务分离）
├── auth.py                   # JWT cookie 认证中间件
├── billing.py                # 腾讯云专用计费计算
├── exchange_rate.py          # 汇率转换（USD ↔ CNY）
└── vendors.py                # 加载 vendors.json，get_vendor() 辅助函数
```

### Frontend 结构

```
frontend/src/
├── main.js                   # Vue 应用入口
├── App.vue                   # 根组件
├── router/index.js           # 路由配置
├── views/                    # 页面组件
│   ├── Overview.vue          # 首页 - 全局概览和厂商卡片
│   ├── VendorDetail.vue      # 厂商详情 - 按模型展示用量
│   ├── Analysis.vue          # 数据分析页
│   ├── Export.vue            # 数据导出页
│   ├── Points.vue            # 积分功能页
│   ├── LiteLLM.vue           # LiteLLM 管理页
│   └── Login.vue             # 登录页
├── components/               # 可复用组件
├── api/                      # API 客户端封装
└── store/                    # Vuex 状态管理（如使用）
```

## 4. 数据流向

### 实时查询流（前端发起）

```
浏览器
  ↓ GET /api/overview?start=2026-07-01&end=2026-07-31
FastAPI main.py
  ↓ split_window(start, end) → 拆分历史区间和今日区间
  ├─ 历史区间 (2026-07-01 ~ 2026-07-30)
  │   ↓ query_vendor_usage() → PostgreSQL
  │   ↓ SELECT FROM vendor_usage_daily WHERE date BETWEEN ...
  │   ↓ 毫秒级响应
  │
  └─ 今日区间 (2026-07-31)
      ↓ VENDOR_DISPATCH[vendor.type] → 11 个厂商并发调用
      ↓ ThreadPoolExecutor.map(fetch_*_vendor_usage, vendors)
      ↓ 每个 *_client.py 调用上游 API（秒级）
      ↓ 返回 {total_cost, models: {...}, daily: [...]}

  ↓ merge_results(db_result, live_result)
  ↓ convert(amount, from_currency, to_currency) → 双币种
  ↓ JSON 返回前端
```

### 定时入库流（cron 触发）

```
APScheduler (凌晨 3:00 CST)
  ↓ ingest/scheduler.py 触发
  ↓ 10 个厂商并行执行
  ↓
  ↓ job.py:ingest_vendor_day(vendor_id, date)
  ↓   ├─ 读取 vendor_ingest_state.last_ingested_date
  ↓   ├─ 从 last_ingested_date+1 开始逐天处理
  ↓   ↓
  ↓   ↓ ADAPTERS[vendor_id].fetch_one_day(day)
  ↓   ↓   ↓ 调用 *_client.py 获取原始数据
  ↓   ↓   ↓ 翻译成 list[ModelRow]（统一字段语义）
  ↓   ↓   ↓ pack_input() / pack_total() 合并 token 字段
  ↓   ↓   ↓ 返回: [ModelRow(model="gpt-4", prompt_tokens=1000, ...)]
  ↓   ↓
  ↓   ↓ PostgreSQL 单事务写入（按顺序）:
  ↓   ↓   1. UPSERT vendor_model_usage_daily（模型粒度）
  ↓   ↓   2. UPSERT vendor_usage_daily（从 model 表 SUM）
  ↓   ↓   3. UPDATE usage_daily_total（全局汇总）
  ↓   ↓   4. UPDATE vendor_ingest_state.last_ingested_date
  ↓   ↓
  ↓   ↓ 异常 → 事务回滚，state 不前进，下次 cron 重试
  ↓   ↓
  ↓   ↓ 成功 → INSERT vendor_ingest_run (status='success')
  ↓   └─ 失败 4 次 → 飞书告警

OpenAI 单独 cron (太平洋时间 01:00)
  ↓ 使用 zoneinfo.ZoneInfo("America/Los_Angeles") + DST 支持
  ↓ 14 天回溯窗口（/costs API 持续更新历史）
```

### 数据库表关系（3 层物化）

```
usage_daily_total (全局日汇总 - 1 行/天)
  ↑ SUM(vendor_id)
vendor_usage_daily (厂商日汇总 - N 厂商 × 1 行/天)
  ↑ SUM(model)
vendor_model_usage_daily (模型明细 - N 厂商 × M 模型 × 1 行/天)

辅助表:
├─ vendor_meta (厂商元数据: display_name, currency, source)
├─ vendor_ingest_run (任务历史: 审计 + 手动重试)
└─ vendor_ingest_state (入库游标: per-vendor 的 last_ingested_date)
```

## 5. 添加新功能时应该看的文件

### 场景 1: 添加新厂商

**必读顺序**:

1. **`CLAUDE.md`** - 阅读 "Adding a New Vendor" 章节，回答 5 个问题
2. **`backend/ingest/adapters/base.py`** - 理解 `VendorAdapter` 基类和 `ModelRow` 字段语义
3. **参考现有适配器**（选择最接近的）:
   - 简单 API: `backend/ingest/adapters/openai_adapter.py`
   - 复杂计费: `backend/ingest/adapters/tencent_adapter.py`
   - 浏览器登录: `backend/ingest/adapters/kimi_adapter.py`
4. **`backend/config/vendors.json`** - 注册新厂商配置
5. **`backend/ingest/adapters/__init__.py`** - 注册 adapter 到 `ADAPTERS` 字典
6. **`backend/main.py`** - 注册 client 到 `VENDOR_DISPATCH` 字典
7. **`backend/ingest/job.py:_inject_env_credentials`** - 添加凭据注入逻辑

**实施步骤**:
```bash
# 1. 写 client（实时查询）
backend/your_vendor_client.py

# 2. 写 adapter（定时入库）
backend/ingest/adapters/your_vendor_adapter.py

# 3. 注册到系统
编辑: vendors.json, adapters/__init__.py, main.py, job.py

# 4. 回填历史数据
python -m ingest.backfill --vendors=your_vendor --days=30
```

### 场景 2: 修改前端页面

**必读顺序**:

1. **`frontend/src/router/index.js`** - 找到路由对应的组件
2. **`frontend/src/views/Overview.vue`** - 如果改首页
3. **`frontend/src/views/VendorDetail.vue`** - 如果改厂商详情
4. **`frontend/src/api/`** - API 调用封装

### 场景 3: 修改数据库表结构

**必读顺序**:

1. **`backend/ingest/models.py`** - SQLAlchemy 表定义
2. **`backend/ingest/db.py`** - Session 管理
3. **`backend/ingest/alembic/`** - 迁移脚本目录

**变更流程**:
```bash
cd backend/ingest
# 修改 models.py 后生成迁移
alembic revision --autogenerate -m "描述变更"
# 应用迁移
alembic upgrade head
```

### 场景 4: 调试入库问题

**必读顺序**:

1. **`backend/ingest/job.py`** - 理解入库主循环和事务顺序
2. **`backend/ingest/scheduler.py`** - 定时任务定义
3. **`backend/ingest/query.py`** - 查看 `split_window` 和数据查询逻辑
4. **`data/logs/scheduler.log`** - 查看 cron 执行日志

**常见命令**:
```bash
# 查看定时任务状态
python -c "from ingest.scheduler import _scheduler; [print(j.id, j.next_run_time) for j in _scheduler.get_jobs()]"

# 手动触发入库
curl -X POST http://localhost:8000/api/ingest/vendors/bigmodel/trigger \
  -H "Content-Type: application/json" \
  -d '{"start": "2026-07-20", "end": "2026-07-28"}'

# 回填历史数据
python -m ingest.backfill --vendors=all --days=90
```

### 场景 5: 添加新的 API 端点

**必读顺序**:

1. **`backend/main.py`** - 主路由注册
2. **`backend/ingest/router.py`** - ingest 相关端点示例
3. **`backend/auth.py`** - 理解认证中间件（默认拦截所有 `/api/*`）

**注意**:
- 新端点自动受 JWT 认证保护
- 如需公开访问，添加到 `auth.py:PUBLIC_PREFIXES`

### 场景 6: 理解字段语义（NULL vs 0）

**必读顺序**:

1. **`backend/ingest/adapters/base.py`** - 字段语义说明
2. **`CLAUDE.md`** - "Field Semantics" 章节
3. **`backend/ingest/job.py`** - 看 `_safe_sum()` 和 `_add()` 如何处理 NULL

**关键原则**:
- `cache_*` 为 NULL → 上游不提供此字段 → 前端显示 "—"
- `cache_*` 为 0 → 真实无缓存使用 → 前端显示 0
- `request_count` 为 NULL → 上游不支持（如 Kimi）→ 前端显示 "—"

## 6. 重要注意事项

### 并发与单例假设
- **APScheduler 假设单 uvicorn worker**，多 worker 会重复执行 cron
- 生产环境需要文件锁或独立 cron 容器

### 厂商特殊性
- **OpenAI**: 独立 PT 时区 cron，DST 支持，14 天回溯
- **Blueshirt**: 双路径（快路径 1s 无 token 拆分，慢路径 30-40s 完整字段）
- **Wangsu**: 需手动维护模型列表，并发限制 8
- **Kimi/Apevon/Bigmodel**: 需 Playwright 浏览器登录 + CAPTCHA

### 认证与安全
- 凭据存放: `backend/.env`（gitignored）
- 会话文件: `backend/config/sessions/*.json`（gitignored）
- 切换账号: `PUT /api/vendors/{id}/account` → 写入 `.env` + 清除会话缓存

### 时区处理
- 数据库存储: UTC 或带时区的 ISO 8601
- 前端显示: CST (UTC+8)
- OpenAI: PT (America/Los_Angeles) with DST

### 架构分离
- **LiteLLM**: 独立子模块，MySQL 数据库，独立认证
- **Points**: 积分功能，MySQL 数据库，与主业务无依赖

## 7. 快速调试命令

```bash
# 检查厂商会话状态
curl http://localhost:8000/api/vendors/bigmodel/session

# 浏览器登录（打开 Playwright）
curl -X POST http://localhost:8000/api/vendors/bigmodel/login

# 查看数据新鲜度
curl http://localhost:8000/api/overview?start=2026-07-01&end=2026-07-31 | jq '.freshness'

# 查看失败任务
curl http://localhost:8000/api/ingest/runs/failed

# 初始化数据库表
python -c "from ingest.db import Base, engine; from ingest import models; Base.metadata.create_all(engine)"

# 查看汇率缓存
cat backend/cache/exchange_rate.json
```

---

**总结**: 此项目的核心是"多厂商适配"而非"统一抽象"。每添加一个厂商，重点是理解其 API 特性并编写对应的 adapter 和 client，而非试图抽象出通用接口。数据流有"实时"和"定时"两条并行路径，分别优化响应速度和数据完整性。
