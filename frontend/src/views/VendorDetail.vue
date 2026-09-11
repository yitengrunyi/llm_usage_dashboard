<script setup>
import { ref, computed, onMounted, watch, nextTick } from 'vue'
import { useRoute } from 'vue-router'
import { fetchVendorUsage, getSessionStatus, triggerLogin, triggerLogout, updateAccount, cancelLogin,
         getIngestRuns, triggerIngest, retryIngestRun, fetchVendorApiKeys } from '../api/index.js'
import { getCache, clearCache, sharedDateRange } from '../store/vendorCache.js'
import CostChart from '../components/CostChart.vue'
import WangsuModelPicker from '../components/WangsuModelPicker.vue'
import BillingReconciliation from '../components/BillingReconciliation.vue'
import { Money, DataLine, Warning, Loading, ArrowDown } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

const route = useRoute()
const vendorId = computed(() => route.params.id)
const cache = computed(() => getCache(vendorId.value))

const loading = ref(false)
// api_key 筛选下的局部结果 — 不写共享 cache (避免清掉筛选后显示旧的 key 数据).
// null = 没选 key, 直接用共享 cache.data; 非 null = 选了 key, 用这份临时结果.
const keyFilteredData = ref(null)
// data 指向"当前该展示的数据": 选了 api_key 时用 keyFilteredData, 否则用共享 cache.
// 这样所有 tokenStats / chartDaily 等 computed 自动跟随筛选状态, 无需改下游.
const data = computed(() => keyFilteredData.value ?? cache.value.data)
const error = ref('')
const sessionStatus = ref('ok')
const loginLoading = ref(false)
const showChangeAccount = ref(false)
const accountForm = ref({ username: '', password: '' })
const accountSaving = ref(false)

const today = new Date()
// 上一个自然周: 周一 ~ 周日 (跟后端 _default_week_window 对齐). 返字符串匹配 value-format
function fmt(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
function lastWeekRange() {
  const d = new Date()
  const dow = d.getDay()
  const offset = dow === 0 ? 7 : dow
  const lastSunday = new Date(d); lastSunday.setDate(d.getDate() - offset)
  const lastMonday = new Date(lastSunday); lastMonday.setDate(lastSunday.getDate() - 6)
  return [fmt(lastMonday), fmt(lastSunday)]
}
// 进入 vendor 详情时, 从全局 sharedDateRange 拷一份当快照.
// 本地改这个 dateRange 不会回写到全局, 也不影响其他 vendor.
// 切到别的 vendor / 切回 overview 改了 → 下次再进来, 用全局最新值重置.
const dateRange = ref([...sharedDateRange.value])

// 趋势图模型筛选 — 多选 model, 走 /api/vendors/:id/usage?models=A&models=B,
// 后端额外返 daily_by_model 字段, 前端传给 CostChart 画多曲线对比.
// 空数组 = 不筛选 (走原"全部 SUM"行为).
const selectedModels = ref([])

// API key 筛选 — 选中一个 API key, 后端按 api_key 过滤数据 (只影响 DB 段)
// null = 不筛选 (默认), 选中后显示该 key 的数据
const selectedApiKey = ref(null)
const apiKeys = ref([])  // 可选的 API keys 列表 [{key, label}, ...]
const apiKeysLoading = ref(false)

// 切 vendor: 重新从全局拿快照 (忘掉旧 vendor 的本地改动)
watch(() => vendorId.value, async (newId, oldId) => {
  if (newId === oldId) return
  dateRange.value = [...sharedDateRange.value]
  selectedModels.value = []   // 切 vendor 时清空 model 筛选
  selectedApiKey.value = null // 切 vendor 时清空 api_key 筛选
  apiKeys.value = []
  reconSummary.value = null   // 清空对账汇总
  error.value = ''
  await checkSession()
  // 新 vendor 的缓存可能为空 (overview 清过) 或还在 — 空就拉
  if (sessionStatus.value === 'ok' && !getCache(newId).data) loadData()
  loadIngestRuns()
  loadApiKeys()  // 加载 API keys
})

function toISOStart(d) {
  if (typeof d === 'string') return `${d}T00:00:00+08:00`
  const y = d.getFullYear(), m = String(d.getMonth() + 1).padStart(2, '0'), day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}T00:00:00+08:00`
}
function toISOEnd(d) {
  if (typeof d === 'string') return `${d}T23:59:59+08:00`
  const y = d.getFullYear(), m = String(d.getMonth() + 1).padStart(2, '0'), day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}T23:59:59+08:00`
}

async function loadData() {
  if (!dateRange.value || dateRange.value.length !== 2) return
  const vid = vendorId.value          // snapshot: 防止切 vendor 时数据写串
  const targetCache = getCache(vid)
  loading.value = true
  error.value = ''
  try {
    const [s, e] = dateRange.value
    const sel = selectedModels.value
    const key = selectedApiKey.value
    const result = await fetchVendorUsage(vid, toISOStart(s), toISOEnd(e),
                                          sel.length ? sel : null,
                                          key)
    if (key) {
      // 选了 api_key → 局部视图, 不写共享 cache. 清掉筛选后自动回退到 cache.data (全部).
      // guard: 拿到结果时如果用户已经清掉 key (或切了 vendor), 别写 — 防止 select→快速clear 竞态留旧数据
      if (vid !== vendorId.value || selectedApiKey.value !== key) return
      keyFilteredData.value = result
      // daily_by_model (多曲线趋势图) 仍贴到结果上让 chartDailyByModel 能算
      if (sel.length && result.daily_by_model) {
        keyFilteredData.value.daily_by_model = result.daily_by_model
        keyFilteredData.value.selected_models = result.selected_models
      }
    } else {
      // 默认视图 (无 key 筛选) → 写共享 cache; 清空临时 key 数据
      keyFilteredData.value = null
      if (sel.length) {
        // 只选了 model 多曲线: data 段不变, 把 daily_by_model 贴到 cache 上
        const base = targetCache.data || {}
        base.daily_by_model = result.daily_by_model
        base.selected_models = result.selected_models
        targetCache.data = base
      } else {
        targetCache.data = result
      }
    }
    if (vid === vendorId.value) sessionStatus.value = 'ok'
  } catch (e) {
    if (vid !== vendorId.value) return // 已经离开这个 vendor, 这次错误别覆盖当前页
    const detail = e.response?.data?.detail || e.message
    if (e.response?.status === 401 || detail.includes('登录态已过期') || e.response?.status === 400) {
      if (detail.includes('密码') || detail.includes('password') || detail.includes('登录失败')) {
        error.value = detail
        if (canChangeAccount.value) showChangeAccount.value = true
      } else if (needsLogin.value) {
        sessionStatus.value = 'expired'
        error.value = ''
      } else {
        error.value = detail
        if (canChangeAccount.value) showChangeAccount.value = true
      }
    } else {
      error.value = detail
    }
  } finally {
    if (vid === vendorId.value) {
      loading.value = false
      // 数据加载完成后，自动加载账单对账
      loadBillingReconciliation()
    }
  }
}

async function loadApiKeys() {
  if (!dateRange.value || dateRange.value.length !== 2) return
  apiKeysLoading.value = true
  try {
    const [s, e] = dateRange.value
    const keys = await fetchVendorApiKeys(vendorId.value, s, e)
    apiKeys.value = keys
  } catch (e) {
    console.warn('Failed to load API keys:', e)
    apiKeys.value = []
  } finally {
    apiKeysLoading.value = false
  }
}

// BigModel 等 vendor 的 key 是 32 位十六进制串, 全显会撑宽下拉面板导致 popper 往左偏移 → 截断显示 (value 仍是完整 key)
function fmtKeyLabel(label) {
  if (!label || label.length <= 16) return label
  return label.slice(0, 8) + '…' + label.slice(-4)
}

const needsLogin = ref(false) // 是否需要登录机制
const sessionVendorType = ref(null) // 从 /session 拿的 vendor_type, 比 data 早可用
const sessionPaymentType = ref(null) // 同上; 即使用量加载失败, 标题旁仍能显示付费方式

async function checkSession() {
  sessionPaymentType.value = null
  try {
    const res = await getSessionStatus(vendorId.value)
    sessionVendorType.value = res.vendor_type ?? null
    sessionPaymentType.value = res.payment_type ?? null
    if (res.status === 'not_needed') {
      // 不需要登录的供应商（如 new-api-direct），直接标记 ok
      needsLogin.value = false
      sessionStatus.value = 'ok'
    } else {
      needsLogin.value = true
      sessionStatus.value = res.status === 'ok' ? 'ok' : res.status === 'waiting' ? 'waiting' : 'expired'
    }
  } catch {
    sessionStatus.value = 'expired'
    needsLogin.value = true
  }
}

// ─── PR5: 入库状态 + 手动重试 ───
const ingestRuns = ref([])
const ingestState = ref(null)  // {synced_through, last_ingested_date, ...}
const ingestLoading = ref(false)

async function loadIngestRuns() {
  try {
    const [runs, allState] = await Promise.all([
      getIngestRuns(vendorId.value, 5),
      fetch('/api/ingest/state').then(r => r.json()),
    ])
    ingestRuns.value = runs
    ingestState.value = (allState || []).find(s => s.vendor_id === vendorId.value) || null
  } catch (e) {
    // 静默 — 不阻塞主流程
    console.warn('ingest load failed', e)
  }
}

const latestRun = computed(() => ingestRuns.value[0] || null)
const syncedThrough = computed(() => ingestState.value?.synced_through || '—')
const earliestDate = computed(() => ingestState.value?.earliest || null)

// 慢路径滞后判定 — slow_synced_through (有 model 拆分的最新天) 落后于
// synced_through (vendor_usage_daily 最新天) 就算滞后. 后端只对走快慢双路径的
// vendor 返这个字段 (blueshirt/nulls 的 raw log retention; apevon 的 stat fallback).
const slowPathLag = computed(() => {
  const fast = ingestState.value?.synced_through
  const slow = ingestState.value?.slow_synced_through
  if (slow === undefined) return null  // 后端没给这个字段 → vendor 不走慢路径模式
  if (!fast) return null
  if (!slow || slow < fast) {
    return { fast, slow: slow || '无' }
  }
  return null
})
// 走慢路径补字段的 vendor — 后端 _SLOW_FILL_MODE dispatch 表的 key.
// blueshirt/nulls: raw log retention 短 (~13 天), 超期无法回补 → UI 加 retention 警告
// apevon: statistics 漏聚合, 走主入库 reingest 模式, 上游迟早出齐, 不带 retention 警告
const SHORT_RETENTION_VENDORS = ['blueshirt', 'nulls']
const showSlowFillUI = computed(() => slowPathLag.value !== null
  && ['blueshirt', 'nulls', 'apevon'].includes(vendorId.value))
const showRetentionWarning = computed(() => SHORT_RETENTION_VENDORS.includes(vendorId.value))
const syncUpToDate = computed(() => {
  const synced = ingestState.value?.synced_through
  if (!synced) return false
  const yest = new Date()
  yest.setDate(yest.getDate() - 1)
  const yestStr = `${yest.getFullYear()}-${String(yest.getMonth() + 1).padStart(2, '0')}-${String(yest.getDate()).padStart(2, '0')}`
  return synced >= yestStr
})

// openai 按 PT 切桌, 其他 vendor 按 CST. 在卡片角落标出来防误读.
const timezoneLabel = computed(() => vendorId.value === 'openai' ? 'US/Pacific' : 'Asia/Shanghai')

// session 过期 (而非接口异常) 的失败 → 顶部明显红色 banner
const isSessionFailure = computed(() => {
  if (!latestRun.value || latestRun.value.status !== 'failed') return false
  const msg = latestRun.value.error_msg || ''
  // 网络层错误 (timeout/connection refused/DNS) 不是 session 问题
  if (/timed?\s*out|timeout|connection\s*(refused|reset|error)|unreachable/i.test(msg)) return false
  return /用户信息|登录|未登录|重新登录|session.expired|登录态已过期|401/i.test(msg)
})

// 失败但非 session 类 (网络超时 / 上游 5xx / 其他) → 黄色 warning banner
const isOtherFailure = computed(() => {
  if (!latestRun.value || latestRun.value.status !== 'failed') return false
  return !isSessionFailure.value
})

function formatRunTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function briefError(msg) {
  if (!msg) return ''
  // 提取最后一行有意义的错误 (跳过 traceback 中间行)
  const lines = msg.split('\n').filter(l => l.trim())
  const last = lines[lines.length - 1] || ''
  // 常见格式: "2025-08-07: ReadTimeout: HTTPConnectionPool..." → 取 : 后面
  const m = last.match(/:\s*([\w.]+Error|[\w.]+Timeout|[\w.]+Exception):\s*(.+)/i)
  if (m) return `${m[1]}: ${m[2].slice(0, 120)}`
  // fallback: 截取最后一行前 150 字符
  return last.slice(0, 150)
}

async function handleManualIngest() {
  ingestLoading.value = true
  try {
    const r = await triggerIngest(vendorId.value)
    ElMessage.success(`已触发入库: ${r.start || ''} ~ ${r.end || ''}`)
    // background task 一启动就 INSERT 一行 status='running' 的新 run, 立刻覆盖之前 failed —
    // get_recently_failed_vendors 看的是最新 run, running != failed → Overview 报警自动消失.
    // 800ms 等 backend 建好 row, 再清 __overview__ 缓存触发 Overview reload (watch cache.data).
    setTimeout(() => clearCache('__overview__'), 800)
    pollRunsAfterTrigger()
  } catch (e) {
    ElMessage.error(`触发失败: ${e.response?.data?.detail || e.message}`)
  } finally {
    ingestLoading.value = false
  }
}

// 触发型操作 (manual ingest / slow-fill / retry) 后多次 reload runs.
// 单次 5s reload 会跨过 running 阶段 — 实测大部分 vendor cron 0.3 ~ 3s 就完事
// (kimi 0.3s, wangsu 1s, road2all 1.8s, apevon 0.5-1s, blueshirt 2.5s),
// 用户点完只能看到终态, 看不到任务正在跑. 改为多次 poll:
// - 500ms: 抓 BG task 刚 INSERT VendorIngestRun (status='running') 的瞬间
// - 2s/5s: 中等任务 (running → 终态) 的过渡
// - 12s: 兜底, blueshirt cron / openai / volcengine 这种偶尔到 10s+ 的任务的终态
function pollRunsAfterTrigger() {
  for (const ms of [500, 2000, 5000, 12000]) setTimeout(loadIngestRuns, ms)
}

// 慢路径单独触发 — 用于快路径已成功但拆分字段还 NULL 的情况
// 默认补昨天, 想补具体一天用 ?day=YYYY-MM-DD (后端支持)
// 状态显示走 ingest_runs (后端建 trigger='slow-fill' 的 VendorIngestRun 行).
async function handleSlowFill(day = null) {
  ingestLoading.value = true
  try {
    const url = day
      ? `/api/ingest/vendors/${vendorId.value}/slow-fill?day=${day}`
      : `/api/ingest/vendors/${vendorId.value}/slow-fill`
    const resp = await fetch(url, { method: 'POST' })
    const r = await resp.json()
    if (!resp.ok) throw new Error(r.detail || resp.statusText)
    ElMessage.success(`已触发慢路径补字段: ${r.day} — 状态自动刷新中`)
    setTimeout(() => clearCache('__overview__'), 800)
    pollRunsAfterTrigger()
  } catch (e) {
    ElMessage.error(`补字段失败: ${e.message}`)
  } finally {
    ingestLoading.value = false
  }
}

const isBlueshirt = computed(() => vendorId.value === 'blueshirt')
const slowFillDay = ref(null)

// ─── 账单对账 ───
const billingReconciliationRef = ref(null)
// 子组件加载完把 summary 抛上来, 顶部卡片和下面两张表用同一份数字
const reconSummary = ref(null)

// 核算区块在 v-if="data" 里面, loadData 的 finally 跑到时它还没渲染出来 (ref 是 null).
// 等一个 tick 让 DOM 补上, 再调 load —— 否则首次查询核算表永远是空的.
async function loadBillingReconciliation() {
  await nextTick()
  billingReconciliationRef.value?.load()
}

async function handleRetryRun(runId) {
  ingestLoading.value = true
  try {
    await retryIngestRun(runId)
    ElMessage.success('已发起重试')
    setTimeout(() => clearCache('__overview__'), 800)  // 等 backend 建好 running run 再清, 让 Overview 报警自动消
    pollRunsAfterTrigger()
  } catch (e) {
    ElMessage.error(`重试失败: ${e.response?.data?.detail || e.message}`)
  } finally {
    ingestLoading.value = false
  }
}

async function handleLogin() {
  loginLoading.value = true
  sessionStatus.value = 'waiting'
  // 自动弹出 noVNC 窗口，用户在里面完成验证码
  const vncUrl = `${window.location.protocol}//${window.location.hostname}:6080`
  window.open(vncUrl, '_blank', 'width=1300,height=750')
  try {
    await triggerLogin(vendorId.value)
    sessionStatus.value = 'ok'
    loadData()
  } catch {
    sessionStatus.value = 'expired'
  } finally {
    loginLoading.value = false
  }
}

async function handleCancelLogin() {
  // 通知后端关 chromium + 释放 lock, 这样用户能立刻重新点登录
  try { await cancelLogin(vendorId.value) } catch {}
  sessionStatus.value = 'expired'
  loginLoading.value = false
}

const vendorName = computed(() => data.value?.vendor_name ?? vendorId.value)
const vendorType = computed(() => data.value?.vendor_type ?? sessionVendorType.value)
const paymentType = computed(() => data.value?.payment_type ?? sessionPaymentType.value)
const paymentTypeLabel = computed(() => paymentType.value === 'prepaid' ? '预付费' : '后付费')
const canChangeAccount = computed(() => ['new-api-direct', 'road2all'].includes(vendorType.value))
const currency = computed(() => data.value?.currency ?? 'USD')
const sym = computed(() => currency.value === 'CNY' ? '¥' : '$')
const totalCostUsd = computed(() => data.value?.total_cost_usd ?? 0)
const totalCostCny = computed(() => data.value?.total_cost_cny ?? 0)

const tokenStats = computed(() => {
  if (!data.value?.models) return { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0, calls: 0 }
  // null/undefined 不算 (说明上游没给, 不是真的 0); 累加里 null + n = n
  const add = (a, v) => v == null ? a : a + v
  let input = 0, output = 0, cacheRead = 0, cacheWrite = 0, total = 0, calls = 0
  for (const m of Object.values(data.value.models)) {
    input = add(input, m.prompt_tokens)
    output = add(output, m.completion_tokens)
    cacheRead = add(cacheRead, m.cache_read_tokens ?? m.cache_tokens)
    cacheWrite = add(cacheWrite, m.cache_write_tokens)
    total = add(total, m.total_tokens)
    calls = add(calls, m.total_count)
  }
  return { input, output, cacheRead, cacheWrite, total, calls }
})
// 整个 vendor 上游不给该字段 → 卡片整列显示 "-"; 上游给 0 仍显示 0
const allNull = (key) => {
  if (!data.value?.models) return false
  return Object.values(data.value.models).every(m => m[key] == null)
}
const inputAllNull = computed(() => allNull('prompt_tokens'))
const outputAllNull = computed(() => allNull('completion_tokens'))
const callsAllNull = computed(() => allNull('total_count'))
const totalAllNull = computed(() => allNull('total_tokens'))

const hasCacheRead = computed(() => tokenStats.value.cacheRead > 0)
const hasCacheWrite = computed(() => tokenStats.value.cacheWrite > 0)
// 只有命中没有写入 → 单卡 "缓存"; 都有 → 拆成 "缓存命中" / "缓存写入"
const showSplitCache = computed(() => hasCacheWrite.value)
const showSingleCache = computed(() => hasCacheRead.value && !hasCacheWrite.value)

const chartDaily = computed(() => {
  if (!data.value?.daily) return []
  // 统一用 CNY (cost_cny 由后端补; 没有就退回 cost, tencent 这种本币 CNY 也能用)
  return data.value.daily.map(d => ({
    date: d.date,
    cost: d.cost_cny ?? d.cost,
    tokens: d.total_tokens,
  }))
})
const chartModels = computed(() => {
  if (!data.value?.models) return {}
  const result = {}
  for (const [name, m] of Object.entries(data.value.models)) {
    result[name] = { total_cost: m.cost_cny ?? m.total_cost }
  }
  return result
})

// 多曲线: 后端返的 daily_by_model = {model: [{date, cost_cny, cost_usd, total_tokens}, ...]}
// 统一映射成 CostChart 期望的 {model: [{date, cost, tokens}]} (cost 用 CNY).
const chartDailyByModel = computed(() => {
  const raw = data.value?.daily_by_model
  if (!raw || !selectedModels.value.length) return null
  const out = {}
  for (const m of selectedModels.value) {
    const series = raw[m]
    if (!series) continue
    out[m] = series.map(d => ({
      date: d.date,
      cost: d.cost_cny ?? d.cost,
      tokens: d.total_tokens,
    }))
  }
  return Object.keys(out).length ? out : null
})

// model 多选下拉选项 — 从 data.models 取 key, 按 cost 倒序 (大头排前)
const modelOptions = computed(() => {
  if (!data.value?.models) return []
  return Object.entries(data.value.models)
    .map(([name, m]) => ({ name, cost: m.cost_cny ?? m.total_cost ?? 0 }))
    .sort((a, b) => b.cost - a.cost)
    .map(({ name, cost }) => ({ value: name, label: name, cost }))
})

// 切 model 选择 → 重拉. data 段不变 (后端只额外算 daily_by_model), 但要避开第一次
// 加载未 ready 时无谓触发.
watch(selectedModels, () => {
  if (!data.value) return
  loadData()
}, { deep: true })

// 切 api_key 选择 → 重拉
watch(selectedApiKey, () => {
  if (!data.value) return
  loadData()
})

// 日期范围变化 → 重新加载 API keys
watch(dateRange, () => {
  if (sessionStatus.value === 'ok') {
    loadApiKeys()
  }
}, { deep: true })

function fmtTokens(n) {
  if (n == null) return '-'
  if (n >= 1_000_000_000_000) return (n / 1_000_000_000_000).toFixed(2) + 'T'
  if (n >= 1_000_000_000) return (n / 1_000_000_000).toFixed(2) + 'B'
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K'
  return n.toString()
}

function fmtCost(n) { return '¥' + n.toFixed(4) }

// 偏差 (顶部核算卡片用): null = 抓不到, 显示 '-'; 有值带正负号
function fmtDeviation(n) {
  if (n == null) return '-'
  return (n >= 0 ? '+$' : '-$') + Math.abs(n).toFixed(2)
}
function fmtDeviationCNY(n) {
  if (n == null) return '-'
  return (n >= 0 ? '+¥' : '-¥') + Math.abs(n).toFixed(2)
}
// 综合折扣 = 总实付 / 总应付. 应付缺失或为 0 → 除不出来, 显示 '-'
function discountText(actual, payable) {
  if (actual == null || payable == null || payable === 0) return '-'
  return ((actual / payable) * 100).toFixed(1) + '%'
}
// 计算侧: 后端已经反推好了 effective_discount, 直接用
const calcDiscountText = computed(() => {
  const d = reconSummary.value?.effective_discount
  return d != null ? (d * 100).toFixed(1) + '%' : '-'
})
// 抓取侧: 实际平均折扣 = 实付(账单) ÷ 应付(抓取); 账单无接口 → 恒为 '-'
const fetchedDiscountText = computed(() =>
  discountText(reconSummary.value?.fetched_actual_pay, reconSummary.value?.fetched_payable)
)

// 偏差大小分级上色 — 基于百分比
function deviationClass(calculated, fetched) {
  if (calculated == null || fetched == null || calculated === 0) return ''
  const deviation = fetched - calculated
  const percent = Math.abs(deviation / calculated)
  if (percent <= 0.1) return 'deviation-ok'      // ≤10%: 绿色
  if (percent <= 0.2) return 'deviation-warn'    // 10%~20%: 黄色
  return 'deviation-error'                       // >20%: 红色
}

async function handleLogout() {
  try {
    await triggerLogout(vendorId.value)
    sessionStatus.value = 'expired'
    clearCache(vendorId.value)
  } catch { /* ignore */ }
}

async function handleChangeAccount() {
  if (!accountForm.value.username || !accountForm.value.password) return
  accountSaving.value = true
  try {
    await updateAccount(vendorId.value, accountForm.value.username, accountForm.value.password)
    showChangeAccount.value = false
    accountForm.value = { username: '', password: '' }
    clearCache(vendorId.value)
    loadData()
  } catch (e) {
    error.value = e.response?.data?.detail || '换号失败'
  } finally {
    accountSaving.value = false
  }
}

const shortcuts = [
  { text: '上一个自然周', value: () => lastWeekRange() },
  { text: '本月至今', value: () => {
      const e = new Date(); e.setDate(e.getDate() - 1)
      const s = new Date(); s.setDate(1)
      return [s, e]
    } },
  { text: '上个自然月', value: () => {
      const now = new Date()
      const s = new Date(now.getFullYear(), now.getMonth() - 1, 1)
      const e = new Date(now.getFullYear(), now.getMonth(), 0)
      return [s, e]
    } },
  { text: '最近7天', value: () => { const e = new Date(); e.setDate(e.getDate() - 1); const s = new Date(); s.setDate(e.getDate() - 6); return [s, e] } },
  { text: '最近30天', value: () => { const e = new Date(); e.setDate(e.getDate() - 1); const s = new Date(); s.setDate(e.getDate() - 29); return [s, e] } },
  { text: '最近90天', value: () => { const e = new Date(); e.setDate(e.getDate() - 1); const s = new Date(); s.setDate(e.getDate() - 89); return [s, e] } },
  { text: '最近半年', value: () => { const e = new Date(); e.setDate(e.getDate() - 1); const s = new Date(); s.setDate(e.getDate() - 179); return [s, e] } },
  { text: '最近一年', value: () => { const e = new Date(); e.setDate(e.getDate() - 1); const s = new Date(); s.setDate(e.getDate() - 364); return [s, e] } },
]

const startDate = computed({
  get: () => dateRange.value?.[0] || '',
  set: (v) => { dateRange.value = [v, dateRange.value?.[1] || v] },
})
const endDate = computed({
  get: () => dateRange.value?.[1] || '',
  set: (v) => { dateRange.value = [dateRange.value?.[0] || v, v] },
})

function disabledDate(time) {
  // 不允许选未来. 今天可以选 (今天数据走 live 拼接)
  const today = new Date()
  today.setHours(23, 59, 59, 999)
  return time > today
}

onMounted(async () => {
  await checkSession()
  if (sessionStatus.value === 'ok') {
    if (!data.value) loadData()  // 有缓存就不重新请求
    loadApiKeys()  // 加载 API keys
  }
  loadIngestRuns()
})
</script>

<template>
  <div class="vendor-detail">
    <div class="header">
      <div class="vendor-heading">
        <h2>{{ vendorName }}</h2>
        <el-tag
          v-if="paymentType"
          :type="paymentType === 'prepaid' ? 'warning' : 'info'"
          size="small"
          effect="plain"
        >
          {{ paymentTypeLabel }}
        </el-tag>
      </div>
      <el-button v-if="needsLogin && sessionStatus === 'ok'" type="info" size="small" text @click="handleLogout">登出</el-button>
      <el-button v-if="canChangeAccount && sessionStatus === 'ok'" type="info" size="small" text @click="showChangeAccount = true">换号</el-button>
      <div class="date-picker">
        <el-date-picker
          v-model="startDate"
          type="date"
          placeholder="开始日期"
          :disabled-date="disabledDate"
          value-format="YYYY-MM-DD"
          style="width: 150px"
        />
        <span class="date-sep">至</span>
        <el-date-picker
          v-model="endDate"
          type="date"
          placeholder="结束日期"
          :disabled-date="disabledDate"
          value-format="YYYY-MM-DD"
          style="width: 150px"
        />
        <el-dropdown @command="(c) => { const r = shortcuts.find(s => s.text === c).value(); dateRange = [typeof r[0] === 'string' ? r[0] : `${r[0].getFullYear()}-${String(r[0].getMonth()+1).padStart(2,'0')}-${String(r[0].getDate()).padStart(2,'0')}`, typeof r[1] === 'string' ? r[1] : `${r[1].getFullYear()}-${String(r[1].getMonth()+1).padStart(2,'0')}-${String(r[1].getDate()).padStart(2,'0')}`]; loadData() }">
          <el-button>快捷选择 <el-icon class="el-icon--right"><ArrowDown /></el-icon></el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item v-for="s in shortcuts" :key="s.text" :command="s.text">{{ s.text }}</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <el-button type="primary" @click="loadData" :loading="loading">查询</el-button>
        <!-- API Key 筛选: 与日期筛选同一行, 选中后自动重拉 (无需再点查询) -->
        <template v-if="apiKeys.length > 0">
          <span class="filter-divider"></span>
          <span class="filter-label">按 API Key 筛选:</span>
          <el-select
            v-model="selectedApiKey"
            clearable
            filterable
            fit-input-width
            placeholder="全部 (不筛选)"
            style="width: 260px"
            :loading="apiKeysLoading"
            @clear="selectedApiKey = null"
          >
            <el-option
              v-for="item in apiKeys"
              :key="item.key"
              :value="item.key"
              :label="fmtKeyLabel(item.label)"
            />
          </el-select>
          <span class="filter-hint" v-if="selectedApiKey">
            (今日数据不支持按 key 筛选)
          </span>
        </template>
      </div>
    </div>

    <el-alert v-if="error" :title="error" type="error" show-icon closable style="margin-bottom: 16px" />

    <!-- session 过期类失败 → 顶部红 banner, 比下面那行 tag 更显眼 -->
    <el-alert
      v-if="isSessionFailure"
      type="error"
      show-icon
      :closable="false"
      style="margin-bottom: 12px"
    >
      <template #title>
        <strong>登录失效</strong> — 入库脚本因 session 过期未拿到数据, 请重新登录后点"同步至昨天"
      </template>
      <div style="font-size: 12px; color: #888; margin-top: 4px; font-family: monospace">
        {{ briefError(latestRun?.error_msg) }}
      </div>
    </el-alert>

    <!-- 其他类型失败 (网络超时 / 上游 5xx) → 黄色 warning, 比 session 红色温和 -->
    <el-alert
      v-if="isOtherFailure"
      type="warning"
      show-icon
      :closable="false"
      style="margin-bottom: 12px"
    >
      <template #title>
        <strong>入库失败</strong> — 上次同步未拿到数据 ({{ formatRunTime(latestRun?.started_at) }})
      </template>
      <div style="font-size: 12px; color: #888; margin-top: 4px; font-family: monospace">
        {{ briefError(latestRun?.error_msg) }}
      </div>
    </el-alert>

    <!-- PR5: 入库状态 + 手动同步 -->
    <div v-if="ingestState || latestRun" class="ingest-status">
      <span class="ingest-label">数据已同步至:</span>
      <span class="ingest-time">{{ syncedThrough }}</span>
      <span v-if="earliestDate" class="ingest-window">
        数据范围 {{ earliestDate }} → {{ syncedThrough }}
      </span>
      <el-tag v-if="vendorId === 'openai'" size="small" type="info" effect="plain">
        时区 {{ timezoneLabel }}
      </el-tag>
      <span v-if="latestRun" class="ingest-window">
        {{ latestRun.trigger === 'slow-fill' ? '最近一次补慢路径' : '最近一次入库' }}
        {{ formatRunTime(latestRun.finished_at || latestRun.started_at) }}
      </span>
      <el-tag
        v-if="latestRun && latestRun.status !== 'success'"
        :type="latestRun.status === 'failed' ? 'danger' : 'warning'"
        size="small"
      >{{ latestRun.trigger === 'slow-fill' ? '慢路径 ' : '' }}{{ latestRun.status }}</el-tag>
      <!-- 跑成功但 rows_upserted=0 + error_msg 有内容 — 任务跑通了, 但上游没给详情数据.
           三种触发都可能命中: slow-fill (blueshirt/nulls raw log 暂无 / LRU 已清),
           cron / manual reingest (apevon statistics 接口返空, 走 stat-fallback 用总额补 cost).
           tag 文案明确 "未返回详细数据" 跟 failed 区分开 — 用户一眼知道是跑了 + 上游没给. -->
      <el-tooltip
        v-if="latestRun && latestRun.status === 'success' && latestRun.rows_upserted === 0
              && latestRun.error_msg"
        :content="latestRun.error_msg"
        placement="bottom-start"
        :show-after="200"
      >
        <el-tag type="warning" size="small">跑成功, 上游未返回详细数据</el-tag>
      </el-tooltip>
      <el-button
        v-if="latestRun && latestRun.status === 'failed'"
        size="small"
        type="warning"
        :loading="ingestLoading"
        @click="handleRetryRun(latestRun.id)"
      >重试上次失败</el-button>
      <el-button
        size="small"
        type="primary"
        plain
        :loading="ingestLoading"
        :disabled="syncUpToDate"
        @click="handleManualIngest"
      >{{ syncUpToDate ? '已同步至昨天' : '同步至昨天' }}</el-button>
      <el-tooltip
        v-if="latestRun && latestRun.error_msg && latestRun.status === 'failed'"
        :content="briefError(latestRun.error_msg)"
        placement="bottom-start"
      >
        <el-icon style="color: #f56c6c"><Warning /></el-icon>
      </el-tooltip>
    </div>
    <div v-else class="ingest-status">
      <span class="ingest-label">入库状态:</span>
      <span class="ingest-time">无记录</span>
      <el-button
        size="small"
        type="primary"
        plain
        :loading="ingestLoading"
        @click="handleManualIngest"
      >手动触发入库</el-button>
    </div>

    <!-- 慢路径滞后提示 (model 拆分没补齐 → prompt/completion/cache 列显示 -)
         blueshirt/nulls: raw log retention ~13 天, 提示 retention 警告
         apevon: 上游 statistics 偶尔漏, 重补走 run_ingest_with_retry, 不带 retention 提示
         按钮统一显示, slow-fill 跑完通过 ingest_runs 看 running/success/failed 状态 -->
    <el-alert
      v-if="slowPathLag"
      type="warning"
      show-icon
      :closable="false"
      style="margin-bottom: 16px"
    >
      <template #title>
        <template v-if="showRetentionWarning">
          拆分字段 (输入/输出/缓存 tokens) 慢路径同步至 <strong>{{ slowPathLag.slow }}</strong>,
          快路径已到 {{ slowPathLag.fast }} — 中间这段 prompt/completion/cache 列显示为 -.
          慢路径 retention 仅 13 天, 超期后无法回补.
        </template>
        <template v-else>
          详情数据 (model 拆分) 慢路径同步至 <strong>{{ slowPathLag.slow }}</strong>,
          快路径已到 {{ slowPathLag.fast }} — 中间这段 cost 已用上游总额接口写入, 但模型拆分缺失.
        </template>
      </template>
      <div v-if="showSlowFillUI" style="margin-top: 8px; display: flex; gap: 8px; align-items: center;">
        <el-button size="small" type="warning" plain :loading="ingestLoading" @click="handleSlowFill()">补昨天</el-button>
        <el-date-picker v-model="slowFillDay" type="date" size="small" placeholder="选择日期" value-format="YYYY-MM-DD" style="width: 160px" />
        <el-button size="small" type="warning" :disabled="!slowFillDay" :loading="ingestLoading" @click="handleSlowFill(slowFillDay)">补指定天</el-button>
        <span style="font-size: 12px; color: #909399; margin-left: 4px;">
          点完查看上方"最近一次入库"状态 (running → success/failed)
        </span>
      </div>
    </el-alert>

    <!-- 网宿: 模型勾选面板 (只在 wangsu 出现) -->
    <WangsuModelPicker v-if="vendorId === 'wangsu'" @applied="loadData" />

    <!-- 未匹配定价的模型提示: 提醒去 LiteLLM 模块加价 -->
    <el-alert
      v-if="vendorId === 'wangsu' && data?.unpriced_models?.length"
      type="warning"
      show-icon
      :closable="false"
      style="margin-bottom: 16px"
    >
      <template #title>
        以下 {{ data.unpriced_models.length }} 个模型有用量但未匹配 LiteLLM 定价 (费用按 0 计), 请到 LiteLLM 模块补充:
      </template>
      <div class="unpriced-list">
        <el-tag v-for="code in data.unpriced_models" :key="code" type="warning" effect="plain" size="small">{{ code }}</el-tag>
      </div>
    </el-alert>

    <!-- 登录提示 -->
    <div class="login-prompt" v-if="sessionStatus === 'expired'">
      <el-card shadow="hover">
        <div class="login-content">
          <el-icon :size="48" color="#faad14"><Warning /></el-icon>
          <h3>需要登录</h3>
          <p>Session 已过期或未登录，点击下方按钮弹出浏览器完成登录。</p>
          <el-button type="primary" size="large" @click="handleLogin" :loading="loginLoading">
            打开浏览器登录
          </el-button>
        </div>
      </el-card>
    </div>

    <div class="login-prompt" v-if="sessionStatus === 'waiting'">
      <el-card shadow="hover">
        <div class="login-content">
          <el-icon :size="48" color="#1890ff" class="spin"><Loading /></el-icon>
          <h3>等待登录</h3>
          <p>浏览器已弹出，请在浏览器中完成验证码并登录。登录成功后将自动加载数据。</p>
          <el-button size="small" @click="handleCancelLogin">取消登录</el-button>
        </div>
      </el-card>
    </div>

    <!-- 费用九宫格: 行=抓取/计算/偏差, 列=应付/实付/折扣, 第9格留空 -->
    <div class="cards-row nine-grid" v-if="data && sessionStatus === 'ok'">
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">应付(抓取)</div>
        <div class="stat-value primary dual">
          <div :class="{ 'native': currency === 'USD' }">${{ totalCostUsd.toFixed(2) }}<span v-if="currency === 'USD'" class="native-mark">*</span></div>
          <div :class="{ 'native': currency === 'CNY' }">¥{{ totalCostCny.toFixed(2) }}<span v-if="currency === 'CNY'" class="native-mark">*</span></div>
        </div>
      </el-card>
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">实付(账单)</div>
        <div class="stat-value primary dual">
          <div :class="{ 'native': currency === 'USD' }">{{ reconSummary && reconSummary.fetched_actual_pay != null ? '$' + reconSummary.fetched_actual_pay.toFixed(2) : '-' }}</div>
          <div :class="{ 'native': currency === 'CNY' }">{{ reconSummary && reconSummary.fetched_actual_pay_cny != null ? '¥' + reconSummary.fetched_actual_pay_cny.toFixed(2) : '-' }}</div>
        </div>
      </el-card>
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">实际平均折扣</div>
        <div class="stat-value">{{ fetchedDiscountText }}</div>
      </el-card>
      <el-card shadow="hover" class="stat-card recon-card">
        <div class="stat-label">应付(计算)</div>
        <div class="stat-value primary dual">
          <div :class="{ 'native': currency === 'USD' }">{{ reconSummary ? '$' + (reconSummary.calculated_payable || 0).toFixed(2) : '-' }}</div>
          <div :class="{ 'native': currency === 'CNY' }">{{ reconSummary ? '¥' + (reconSummary.calculated_payable_cny || 0).toFixed(2) : '-' }}</div>
        </div>
      </el-card>
      <el-card shadow="hover" class="stat-card recon-card">
        <div class="stat-label">实付(计算)</div>
        <div class="stat-value primary dual">
          <div :class="{ 'native': currency === 'USD' }">{{ reconSummary ? '$' + (reconSummary.calculated_actual_pay || 0).toFixed(2) : '-' }}</div>
          <div :class="{ 'native': currency === 'CNY' }">{{ reconSummary ? '¥' + (reconSummary.calculated_actual_pay_cny || 0).toFixed(2) : '-' }}</div>
        </div>
      </el-card>
      <el-card shadow="hover" class="stat-card recon-card">
        <div class="stat-label">计算平均折扣</div>
        <div class="stat-value">{{ calcDiscountText }}</div>
      </el-card>
      <el-card shadow="hover" class="stat-card recon-card sub-card">
        <div class="stat-label">应付偏差</div>
        <!-- 偏差 = 上方「应付(抓取)卡 − 应付(计算)卡」, 与页面展示的两张卡对得上;
             百分比上色用同币种一对 (calculated_payable 是 USD, 对 totalCostUsd) -->
        <div class="stat-value dual" :class="deviationClass(reconSummary?.calculated_payable, totalCostUsd)">
          <div :class="{ 'native': currency === 'USD' }">{{ fmtDeviation(reconSummary?.calculated_payable != null ? totalCostUsd - reconSummary.calculated_payable : null) }}</div>
          <div :class="{ 'native': currency === 'CNY' }">{{ fmtDeviationCNY(reconSummary?.calculated_payable_cny != null ? totalCostCny - reconSummary.calculated_payable_cny : null) }}</div>
        </div>
      </el-card>
      <el-card shadow="hover" class="stat-card recon-card sub-card">
        <div class="stat-label">实付偏差</div>
        <div class="stat-value dual" :class="deviationClass(reconSummary?.calculated_actual_pay, reconSummary?.fetched_actual_pay)">
          <div :class="{ 'native': currency === 'USD' }">{{ fmtDeviation(reconSummary?.deviation_actual_pay) }}</div>
          <div :class="{ 'native': currency === 'CNY' }">{{ fmtDeviationCNY(reconSummary?.deviation_actual_pay_cny) }}</div>
        </div>
      </el-card>
      <!-- 第9格: 空白占位, 无文字 -->
      <el-card shadow="hover" class="stat-card recon-card sub-card"></el-card>
    </div>

    <div v-if="data && sessionStatus === 'ok'" class="native-mark-note">
      <span class="native-mark">*</span> 上游返回的原始币种 (另一币种是按入库时刻汇率换算)
    </div>

    <!-- 计数卡片 (调用次数 / 模型数量) -->
    <div class="cards-row" v-if="data && sessionStatus === 'ok'">
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">调用次数</div>
        <div class="stat-value">{{ callsAllNull ? '-' : tokenStats.calls.toLocaleString() }}</div>
      </el-card>
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">模型数量</div>
        <div class="stat-value">{{ Object.keys(data.models || {}).length }}</div>
      </el-card>
    </div>

    <!-- Token 卡片 -->
    <div class="cards-row" v-if="data && sessionStatus === 'ok'">
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">总 Tokens</div>
        <div class="stat-value primary">{{ totalAllNull ? '-' : fmtTokens(tokenStats.total) }}</div>
      </el-card>
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">输入 Tokens</div>
        <div class="stat-value">{{ inputAllNull ? '-' : fmtTokens(tokenStats.input) }}</div>
      </el-card>
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">输出 Tokens</div>
        <div class="stat-value">{{ outputAllNull ? '-' : fmtTokens(tokenStats.output) }}</div>
      </el-card>
      <el-card shadow="hover" class="stat-card" v-if="showSingleCache">
        <div class="stat-label">缓存 Tokens</div>
        <div class="stat-value">{{ fmtTokens(tokenStats.cacheRead) }}</div>
      </el-card>
      <el-card shadow="hover" class="stat-card" v-if="showSplitCache">
        <div class="stat-label">缓存命中</div>
        <div class="stat-value">{{ fmtTokens(tokenStats.cacheRead) }}</div>
      </el-card>
      <el-card shadow="hover" class="stat-card" v-if="showSplitCache">
        <div class="stat-label">缓存写入</div>
        <div class="stat-value">{{ fmtTokens(tokenStats.cacheWrite) }}</div>
      </el-card>
    </div>

    <!-- 图表 -->
    <div class="charts" v-if="data && sessionStatus === 'ok' && chartDaily.length">
      <!-- 模型筛选: 多选, 切换 → 重拉 ?models=... → 趋势图画多条对比线;
           空 = 显示原"全模型 SUM"曲线 -->
      <div class="model-filter-bar">
        <span class="g-label">趋势图按模型:</span>
        <el-select
          v-model="selectedModels"
          multiple
          filterable
          collapse-tags
          collapse-tags-tooltip
          placeholder="全部 (合计)"
          style="min-width: 320px; max-width: 560px"
          :loading="loading"
        >
          <el-option
            v-for="m in modelOptions"
            :key="m.value"
            :value="m.value"
            :label="m.label"
          >
            <span>{{ m.label }}</span>
            <span style="float: right; color: #999; font-size: 12px">¥{{ m.cost.toFixed(2) }}</span>
          </el-option>
        </el-select>
        <el-button
          size="default"
          :disabled="!selectedModels.length"
          @click="selectedModels = []"
        >清空</el-button>
        <span class="g-hint" v-if="selectedModels.length">
          (对比 {{ selectedModels.length }} 个 model)
        </span>
      </div>
      <CostChart
        :daily="chartDaily"
        :textModels="chartModels"
        :imageModels="{}"
        :dailyByModel="chartDailyByModel"
      />
    </div>

    <!-- 模型费用核算 -->
    <div class="billing-section" v-if="data && sessionStatus === 'ok'">
      <el-card>
        <template #header>
          <span style="font-weight: 600">模型费用核算</span>
        </template>
        <BillingReconciliation
          ref="billingReconciliationRef"
          :vendor-id="vendorId"
          :date-range="dateRange"
          :currency="currency"
          :api-key="selectedApiKey"
          @loaded="reconSummary = $event"
        />
      </el-card>
    </div>

    <!-- 换号弹窗 -->
    <el-dialog v-model="showChangeAccount" title="换号" width="400px">
      <el-form :model="accountForm" label-width="80px">
        <el-form-item label="用户名">
          <el-input v-model="accountForm.username" placeholder="输入新用户名" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input v-model="accountForm.password" type="password" show-password placeholder="输入新密码" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showChangeAccount = false">取消</el-button>
        <el-button type="primary" @click="handleChangeAccount" :loading="accountSaving">确认换号</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 24px;
}
.header h2 {
  margin: 0;
  font-size: 20px;
  color: #1a1a1a;
}
.vendor-heading {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}
.date-picker {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.date-sep {
  color: #888;
  font-size: 13px;
}
.cards-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 16px;
  margin-bottom: 16px;
}
/* 九宫格: 固定 3 列等宽, 9 格同尺寸 */
.nine-grid {
  grid-template-columns: repeat(3, 1fr);
}
.stat-card {
  text-align: center;
  padding: 8px 0;
}
/* 配对卡片: 计算 (上) + 偏差 (下) 垂直堆叠成一组, 跟旁边独立卡片等宽 */
.card-pair {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 200px;
}
/* 偏差子卡片字号小一档, 区分主次 */
.sub-card .stat-value {
  font-size: 22px;
}
.sub-card .stat-value.dual > div {
  font-size: 16px;
}
.sub-card .stat-value.dual > div.native {
  font-size: 20px;
}
.stat-label {
  font-size: 14px;
  color: #888;
  margin-bottom: 8px;
}
.stat-value {
  font-size: 32px;
  font-weight: 700;
  color: #1a1a1a;
  line-height: 1.2;
}
.stat-value.primary {
  color: #1890ff;
}
.stat-value.cny {
  color: #f5222d;
}
.stat-value.dual {
  font-size: 24px;
  line-height: 1.4;
}
/* 非原生币种行: 跟随卡片主色 (偏差卡的红/黄/绿也要吃到), 淡一档做层次 */
.stat-value.dual > div {
  font-size: 20px;
  font-weight: 600;
  color: inherit;
  opacity: 0.7;
}
/* 上游返回的原始币种行加重 */
.stat-value.dual > div.native {
  font-size: 24px;
  font-weight: 700;
  opacity: 1;
}
.stat-value.deviation-ok {
  color: #52c41a;
}
.stat-value.deviation-warn {
  color: #faad14;
}
.stat-value.deviation-error {
  color: #f5222d;
}
.stat-dual {
  display: flex;
  align-items: baseline;
  justify-content: center;
  gap: 8px;
  font-size: 24px;
  font-weight: 700;
  line-height: 1.2;
}
.stat-dual-item.primary { color: #1890ff; }
.stat-dual-item.cny { color: #f5222d; }
.stat-dual-sep { color: #ccc; font-weight: 400; font-size: 18px; }
.stat-sub {
  font-size: 13px;
  color: #aaa;
  margin-top: 6px;
}
.charts {
  margin-bottom: 24px;
}
.model-filter-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.model-filter-bar .g-label {
  font-size: 13px;
  color: #555;
}
.model-filter-bar .g-hint {
  font-size: 12px;
  color: #999;
}
.billing-section {
  margin-bottom: 24px;
}
.login-prompt {
  margin-bottom: 24px;
}
.login-content {
  text-align: center;
  padding: 40px 20px;
}
.login-content h3 {
  margin: 16px 0 8px;
  font-size: 18px;
  color: #1a1a1a;
}
.login-content p {
  color: #888;
  margin-bottom: 24px;
}
.spin {
  animation: spin 1.5s linear infinite;
}
.unpriced-list {
  margin-top: 8px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
.ingest-status {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  margin-bottom: 12px;
  background: #f5f7fa;
  border-radius: 4px;
  font-size: 13px;
  color: #606266;
  flex-wrap: wrap;
}
.ingest-label {
  font-weight: 500;
  color: #303133;
}
.ingest-time {
  font-family: monospace;
}
.ingest-window {
  color: #909399;
  font-family: monospace;
  font-size: 12px;
}
.native-mark {
  color: #f59f00;
  font-weight: 700;
  margin-left: 4px;
}
.native-mark-note {
  font-size: 12px;
  color: #909399;
  margin: -8px 0 16px 4px;
}
/* API key 筛选与日期筛选同行, 竖线分隔两组 */
.filter-divider {
  width: 1px;
  height: 20px;
  background: #dcdfe6;
}
.filter-label {
  font-size: 14px;
  color: #303133;
  font-weight: 500;
  white-space: nowrap;
}
.filter-hint {
  font-size: 12px;
  color: #909399;
  white-space: nowrap;
}

@media (max-width: 768px) {
  .header {
    align-items: flex-start;
    flex-wrap: wrap;
    gap: 10px;
  }
  .date-picker {
    width: 100%;
  }
}
</style>
