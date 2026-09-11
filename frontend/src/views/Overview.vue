<script setup>
defineOptions({ name: 'Overview' })
import { ref, computed, onMounted, watch } from 'vue'
import { fetchOverview } from '../api/index.js'
import { getCache, sharedDateRange, clearAllCaches } from '../store/vendorCache.js'
import CostChart from '../components/CostChart.vue'
import { Calendar, Money, Coin, DataLine, ArrowDown } from '@element-plus/icons-vue'

const cache = getCache('__overview__')

const loading = ref(false)
const data = computed({
  get: () => cache.data,
  set: (v) => { cache.data = v },
})
const error = ref('')

const today = new Date()
// 上一个自然周: 周一 ~ 周日 (跟后端 _default_week_window 对齐).
// 返 YYYY-MM-DD 字符串, 匹配 picker 的 value-format
function fmt(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
function lastWeekRange() {
  const d = new Date()
  const dow = d.getDay()  // 0=日 ... 6=六
  const offset = dow === 0 ? 7 : dow  // 周一 = 1, 周日 = 7
  const lastSunday = new Date(d); lastSunday.setDate(d.getDate() - offset)
  const lastMonday = new Date(lastSunday); lastMonday.setDate(lastSunday.getDate() - 6)
  return [fmt(lastMonday), fmt(lastSunday)]
}
const dateRange = sharedDateRange  // Overview = 全局日期的"源头", 直接绑这个 ref

// 趋势图模型筛选 — 多选, 切了重拉 ?models=A&models=B, 后端额外返 daily_by_model
// 给 CostChart 画多曲线对比. 空数组 = 不筛 (全局 SUM).
const selectedModels = ref([])

// dateRange 一变 → 清所有 vendor 缓存, 让他们下次 mount 走 Overview 的新日期重查.
// (vendor 详情页本地改自己的日期不进这里 — 那是 local snapshot, 不写 sharedDateRange.)
watch(dateRange, () => clearAllCaches(), { deep: true })

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
  loading.value = true
  error.value = ''
  try {
    const [s, e] = dateRange.value
    const sel = selectedModels.value
    const result = await fetchOverview(toISOStart(s), toISOEnd(e),
                                       sel.length ? sel : null)
    // 选了 model 时只追加 daily_by_model 到现有 data, 不覆盖 — 防止"全部"baseline 丢失
    if (sel.length && data.value) {
      data.value = { ...data.value,
                     daily_by_model: result.daily_by_model,
                     selected_models: result.selected_models }
    } else {
      data.value = result
    }
  } catch (e) {
    error.value = e.response?.data?.detail || e.message
  } finally {
    loading.value = false
  }
}

const totalCostCny = computed(() => data.value?.total_cost_cny ?? 0)
const totalCostUsd = computed(() => data.value?.total_cost_usd ?? 0)
const totalPromptTokens = computed(() => data.value?.total_prompt_tokens ?? 0)
const totalCompletionTokens = computed(() => data.value?.total_completion_tokens ?? 0)
const totalCacheTokens = computed(() => data.value?.total_cache_tokens ?? data.value?.total_cache_read_tokens ?? 0)
const totalCacheReadTokens = computed(() => data.value?.total_cache_read_tokens ?? data.value?.total_cache_tokens ?? 0)
const totalCacheWriteTokens = computed(() => data.value?.total_cache_write_tokens ?? 0)
const showSplitCache = computed(() => totalCacheWriteTokens.value > 0)
const showSingleCache = computed(() => totalCacheReadTokens.value > 0 && !showSplitCache.value)
const totalTokens = computed(() => data.value?.total_tokens ?? 0)
const vendorCount = computed(() => data.value?.vendors?.length ?? 0)
const totalCalls = computed(() => {
  if (!data.value?.vendors) return 0
  return data.value.vendors.reduce((s, v) => s + v.total_count, 0)
})

// 当前查询窗口里哪些 vendor 缺哪几天 (后端已排除今天). 没缺 = 数组空
// 后端给的: { vendor_id: ['2026-05-27', '2026-05-28', ...] }
const missingVendors = computed(() => {
  const m = data.value?.missing_days || {}
  return Object.entries(m).map(([vendor_id, days]) => ({
    vendor_id,
    days,
    summary: days.length <= 3 ? days.join(', ') : `${days[0]} ~ ${days[days.length - 1]} (共 ${days.length} 天)`,
  }))
})

// 模型家族排序: 同家族的挤在一起, 家族内按费用降序
// 顺序: gpt → gemini → kimi → claude → 其它
const FAMILY_ORDER = ['gpt', 'gemini', 'kimi', 'claude']
function familyRank(name) {
  const low = (name || '').toLowerCase()
  for (let i = 0; i < FAMILY_ORDER.length; i++) {
    if (low.startsWith(FAMILY_ORDER[i])) return i
  }
  return FAMILY_ORDER.length
}

// 全模型明细表数据
const allModelData = computed(() => {
  if (!data.value?.all_models) return []
  return Object.entries(data.value.all_models)
    .map(([name, m]) => ({
      name,
      prompt_tokens: m.prompt_tokens,
      completion_tokens: m.completion_tokens,
      cache_tokens: m.cache_tokens ?? 0,
      total_tokens: m.total_tokens,
      total_count: m.total_count,
      total_cost_cny: m.total_cost_cny,
      total_cost_usd: m.total_cost_usd ?? 0,
      image_count: m.image_count ?? 0,
      vendors: m.vendors.join(', '),
    }))
    .sort((a, b) => {
      const fa = familyRank(a.name), fb = familyRank(b.name)
      if (fa !== fb) return fa - fb
      return b.total_cost_cny - a.total_cost_cny
    })
})

// 供应商汇总表数据
const vendorData = computed(() => data.value?.vendors ?? [])
const hasVendorCache = computed(() => vendorData.value.some(v => v.cache_tokens > 0))

// 图表用的 daily 数据 (含 cost 和 token)
const chartDaily = computed(() => {
  if (!data.value?.daily) return []
  return data.value.daily.map(d => ({ date: d.date, cost: d.cost_cny, tokens: d.total_tokens }))
})

// 多曲线: 后端 daily_by_model = {model: [{date, cost_cny, cost_usd, total_tokens}, ...]}
// 映射成 CostChart 期望的 {model: [{date, cost, tokens}]} (cost 用 CNY).
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

// model 下拉选项 — 从 all_models 取, 按 cost 倒序
const modelOptions = computed(() => {
  if (!data.value?.all_models) return []
  return Object.entries(data.value.all_models)
    .map(([name, m]) => ({ name, cost: m.total_cost_cny ?? 0 }))
    .sort((a, b) => b.cost - a.cost)
    .map(({ name, cost }) => ({ value: name, label: name, cost }))
})

// 切 model → 重拉 (data 已经有了再触发, 避免初次加载噪声)
watch(selectedModels, () => {
  if (!data.value) return
  loadData()
}, { deep: true })

// 图表用的 models 数据（转为 CostChart 期望的格式）
const chartModels = computed(() => {
  if (!data.value?.all_models) return {}
  const result = {}
  for (const [name, m] of Object.entries(data.value.all_models)) {
    result[name] = { total_cost: m.total_cost_cny }
  }
  return result
})

function fmtTokens(n) {
  if (n == null) return '-'
  if (n >= 1_000_000_000_000) return (n / 1_000_000_000_000).toFixed(2) + 'T'
  if (n >= 1_000_000_000) return (n / 1_000_000_000).toFixed(2) + 'B'
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K'
  return n.toString()
}

function fmtCount(n) {
  return n == null ? '-' : n.toLocaleString()
}

function fmtCost(n) {
  return '¥' + (n == null ? '0.0000' : n.toFixed(4))
}

function getModelSummaries({ columns }) {
  const sums = []
  columns.forEach((col, i) => {
    if (i === 0) { sums[i] = '合计'; return }
    const key = col.property
    if (!key) { sums[i] = ''; return }
    const total = allModelData.value.reduce((s, r) => s + (r[key] || 0), 0)
    if (key === 'total_cost_cny') sums[i] = '¥' + total.toFixed(4)
    else if (key === 'total_cost_usd') sums[i] = '$' + total.toFixed(4)
    else if (key.includes('tokens')) sums[i] = fmtTokens(total)
    else if (key === 'total_count') sums[i] = total.toLocaleString()
    else sums[i] = ''
  })
  return sums
}

// 快捷选项: end 一律取昨天 (今天数据不完整, 走 live 拼接的话又慢, 大多数场景不需要)
const shortcuts = [
  { text: '上一个自然周', value: () => lastWeekRange() },
  { text: '本月至今', value: () => {
      // 本月 1 日 → 昨天. 月初当天点会变 [1 号, 上月最后一天], 退化成"上个自然月"也合理
      const e = new Date(); e.setDate(e.getDate() - 1)
      const s = new Date(); s.setDate(1)
      return [s, e]
    } },
  { text: '上个自然月', value: () => {
      // 上月 1 日 → 上月最后一天 (本月 1 日 - 1)
      const now = new Date()
      const s = new Date(now.getFullYear(), now.getMonth() - 1, 1)
      const e = new Date(now.getFullYear(), now.getMonth(), 0)  // day 0 = 上月末
      return [s, e]
    } },
  { text: '最近7天', value: () => { const e = new Date(); e.setDate(e.getDate() - 1); const s = new Date(); s.setDate(e.getDate() - 6); return [s, e] } },
  { text: '最近30天', value: () => { const e = new Date(); e.setDate(e.getDate() - 1); const s = new Date(); s.setDate(e.getDate() - 29); return [s, e] } },
  { text: '最近90天', value: () => { const e = new Date(); e.setDate(e.getDate() - 1); const s = new Date(); s.setDate(e.getDate() - 89); return [s, e] } },
  { text: '最近半年', value: () => { const e = new Date(); e.setDate(e.getDate() - 1); const s = new Date(); s.setDate(e.getDate() - 179); return [s, e] } },
  { text: '最近一年', value: () => { const e = new Date(); e.setDate(e.getDate() - 1); const s = new Date(); s.setDate(e.getDate() - 364); return [s, e] } },
]

// 两个独立 date-picker 各自的 v-model — 跟 dateRange 双向同步
const startDate = computed({
  get: () => dateRange.value?.[0] || '',
  set: (v) => { dateRange.value = [v, dateRange.value?.[1] || v] },
})
const endDate = computed({
  get: () => dateRange.value?.[1] || '',
  set: (v) => { dateRange.value = [dateRange.value?.[0] || v, v] },
})

function disabledDate(time) {
  // 不允许选未来. 今天可以选 (今天数据走 live 拼接, 慢但能看实时)
  const today = new Date()
  today.setHours(23, 59, 59, 999)
  return time > today
}

onMounted(() => {
  if (!data.value) loadData()
})

// 别处 (e.g. VendorDetail 重试入库) clearCache('__overview__') 把 cache.data 清 null 时,
// 这里自动重新拉一次 — 避免用户在 Overview 时报警不自动消失.
watch(() => cache.data, (v) => {
  if (v == null && !loading.value) loadData()
})
</script>

<template>
  <div class="overview">
    <div class="header">
      <h2>总览</h2>
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
        <span class="range-hint">历史数据已落库, 可任意回查</span>
      </div>
    </div>

    <el-alert v-if="error" :title="error" type="error" show-icon closable style="margin-bottom: 16px" />

    <!-- 当前查询窗口里 vendor 缺数据的天 (已排除今天) -->
    <el-alert
      v-if="missingVendors.length"
      type="warning"
      show-icon
      :closable="false"
      style="margin-bottom: 8px"
    >
      <template #title>
        <span>以下供应商在当前查询窗口内有未入库的天 (历史数据不全, 点供应商名手动重试):
          <span v-for="(v, i) in missingVendors" :key="v.vendor_id" style="margin-left: 4px">
            <router-link :to="`/vendor/${v.vendor_id}`" style="color: #1890ff">{{ v.vendor_id }}</router-link>
            <span style="color: #909399; margin-left: 4px">({{ v.summary }})</span><span v-if="i < missingVendors.length - 1">,</span>
          </span>
        </span>
      </template>
    </el-alert>

    <!-- 未登录供应商提示 (查询今天上游失败) -->
    <el-alert
      v-for="fv in (data?.failed_vendors ?? [])"
      :key="fv.vendor_id"
      type="warning"
      show-icon
      closable
      style="margin-bottom: 8px"
    >
      <template #title>
        <span>{{ fv.vendor_name }} 数据获取失败：{{ fv.error }}
          <router-link :to="`/vendor/${fv.vendor_id}`" style="color: #1890ff; margin-left: 8px">去登录</router-link>
        </span>
      </template>
    </el-alert>

    <!-- 最近 48h 内 cron 入库失败的 vendor (session 过期 / cookie 失效 / 上游 502 等) -->
    <el-alert
      v-for="iv in (data?.failed_ingest_vendors ?? [])"
      :key="`ingest-fail-${iv.vendor_id}`"
      type="error"
      show-icon
      closable
      style="margin-bottom: 8px"
    >
      <template #title>
        <span>{{ iv.vendor_name }} 最近一次入库失败：{{ iv.error_msg }}
          <router-link :to="`/vendor/${iv.vendor_id}`" style="color: #1890ff; margin-left: 8px">去处理</router-link>
        </span>
      </template>
    </el-alert>

    <!-- 费用卡片 -->
    <div class="cards-row" v-if="data">
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">总费用 (USD)</div>
        <div class="stat-value primary">${{ totalCostUsd.toFixed(2) }}</div>
      </el-card>
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">总费用 (CNY)</div>
        <div class="stat-value primary">¥{{ totalCostCny.toFixed(2) }}</div>
      </el-card>
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">调用次数</div>
        <div class="stat-value">{{ totalCalls.toLocaleString() }}</div>
        <div class="stat-sub">{{ vendorCount }} 个供应商</div>
      </el-card>
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">模型数量</div>
        <div class="stat-value">{{ Object.keys(data.all_models || {}).length }}</div>
      </el-card>
    </div>

    <!-- Token 卡片 -->
    <div class="cards-row" v-if="data">
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">总 Tokens</div>
        <div class="stat-value primary">{{ fmtTokens(totalTokens) }}</div>
      </el-card>
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">输入 Tokens</div>
        <div class="stat-value">{{ fmtTokens(totalPromptTokens) }}</div>
      </el-card>
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">输出 Tokens</div>
        <div class="stat-value">{{ fmtTokens(totalCompletionTokens) }}</div>
      </el-card>
      <el-card shadow="hover" class="stat-card" v-if="showSingleCache">
        <div class="stat-label">缓存 Tokens</div>
        <div class="stat-value">{{ fmtTokens(totalCacheReadTokens) }}</div>
      </el-card>
      <el-card shadow="hover" class="stat-card" v-if="showSplitCache">
        <div class="stat-label">缓存命中</div>
        <div class="stat-value">{{ fmtTokens(totalCacheReadTokens) }}</div>
      </el-card>
      <el-card shadow="hover" class="stat-card" v-if="showSplitCache">
        <div class="stat-label">缓存写入</div>
        <div class="stat-value">{{ fmtTokens(totalCacheWriteTokens) }}</div>
      </el-card>
    </div>

    <!-- 供应商汇总 -->
    <div class="table-section" v-if="data">
      <el-card>
        <template #header><span style="font-weight: 600">供应商费用汇总</span></template>
        <el-table :data="vendorData" stripe style="width: 100%">
          <el-table-column prop="vendor_name" label="供应商" min-width="160" />
          <el-table-column prop="total_cost_usd" label="费用 (USD)" width="130" align="right" sortable>
            <template #default="{ row }">
              <span style="font-weight: 600; color: #1890ff">${{ (row.total_cost_usd ?? 0).toFixed(4) }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="total_cost_cny" label="费用 (CNY)" width="140" align="right" sortable>
            <template #default="{ row }">
              <span style="font-weight: 600; color: #1890ff">¥{{ (row.total_cost_cny ?? 0).toFixed(4) }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="total_count" label="调用次数" width="100" align="right">
            <template #default="{ row }">{{ fmtCount(row.total_count) }}</template>
          </el-table-column>
          <el-table-column prop="prompt_tokens" label="输入 Tokens" width="120" align="right">
            <template #default="{ row }">{{ fmtTokens(row.prompt_tokens) }}</template>
          </el-table-column>
          <el-table-column prop="completion_tokens" label="输出 Tokens" width="120" align="right">
            <template #default="{ row }">{{ fmtTokens(row.completion_tokens) }}</template>
          </el-table-column>
          <el-table-column prop="cache_tokens" label="缓存 Tokens" width="120" align="right" v-if="hasVendorCache">
            <template #default="{ row }">{{ row.cache_tokens > 0 ? fmtTokens(row.cache_tokens) : '-' }}</template>
          </el-table-column>
          <el-table-column prop="total_tokens" label="总 Tokens" width="120" align="right">
            <template #default="{ row }">{{ fmtTokens(row.total_tokens) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="80" align="center">
            <template #default="{ row }">
              <router-link :to="`/vendor/${row.vendor_id}`" style="color: #1890ff; text-decoration: none">详情</router-link>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </div>

    <!-- 图表 -->
    <div class="charts" v-if="data && chartDaily.length">
      <div class="model-filter-bar">
        <span class="g-label">趋势图按模型:</span>
        <el-select
          v-model="selectedModels"
          multiple
          filterable
          collapse-tags
          collapse-tags-tooltip
          placeholder="全部 (跨 vendor 合计)"
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

    <!-- 全模型明细（不分供应商） -->
    <div class="table-section" v-if="data">
      <el-card>
        <template #header><span style="font-weight: 600">全模型费用明细（所有供应商汇总）</span></template>
        <el-table :data="allModelData" stripe show-summary :summary-method="getModelSummaries" style="width: 100%">
          <el-table-column prop="name" label="模型" min-width="200" fixed />
          <el-table-column prop="total_count" label="调用次数" width="100" align="right">
            <template #default="{ row }">{{ fmtCount(row.total_count) }}</template>
          </el-table-column>
          <el-table-column prop="prompt_tokens" label="输入 Tokens" width="130" align="right">
            <template #default="{ row }">{{ fmtTokens(row.prompt_tokens) }}</template>
          </el-table-column>
          <el-table-column prop="completion_tokens" label="输出 Tokens" width="130" align="right">
            <template #default="{ row }">{{ fmtTokens(row.completion_tokens) }}</template>
          </el-table-column>
          <el-table-column prop="cache_tokens" label="缓存 Tokens" width="130" align="right">
            <template #default="{ row }">{{ row.cache_tokens > 0 ? fmtTokens(row.cache_tokens) : '-' }}</template>
          </el-table-column>
          <el-table-column prop="total_tokens" label="总 Tokens" width="130" align="right">
            <template #default="{ row }">{{ fmtTokens(row.total_tokens) }}</template>
          </el-table-column>
          <el-table-column prop="image_count" label="生成张数" width="100" align="right" v-if="allModelData.some(r => r.image_count > 0)">
            <template #default="{ row }">{{ row.image_count > 0 ? row.image_count.toLocaleString() : '-' }}</template>
          </el-table-column>
          <el-table-column prop="total_cost_usd" label="费用 (USD)" width="130" align="right" sortable>
            <template #default="{ row }">
              <span style="font-weight: 600; color: #1890ff">${{ (row.total_cost_usd ?? 0).toFixed(4) }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="total_cost_cny" label="费用 (CNY)" width="140" align="right" sortable>
            <template #default="{ row }">
              <span style="font-weight: 600; color: #1890ff">¥{{ row.total_cost_cny.toFixed(4) }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="vendors" label="供应商" min-width="150" />
        </el-table>
      </el-card>
    </div>
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
.range-hint {
  color: #999;
  font-size: 12px;
}
.cards-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 16px;
  margin-bottom: 16px;
}
.stat-card {
  text-align: center;
  padding: 8px 0;
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
.table-section {
  margin-bottom: 24px;
}
</style>
