<script setup>
defineOptions({ name: 'Dashboard' })
import { ref, computed, onMounted, watch } from 'vue'
import { fetchSummary } from '../api/index.js'
import { getCache } from '../store/vendorCache.js'
import CostChart from '../components/CostChart.vue'
import ModelTable from '../components/ModelTable.vue'
import ImageTable from '../components/ImageTable.vue'
import PricingAdmin from './PricingAdmin.vue'
import { Money, DataLine, Picture, Coin } from '@element-plus/icons-vue'

const cache = getCache('tencent')

const activeTab = ref('usage')
const loading = ref(false)
const data = computed({
  get: () => cache.data,
  set: (v) => { cache.data = v },
})
const error = ref('')

const today = new Date()
const dateRange = ref(cache.dateRange || [today, today])
watch(dateRange, (v) => { cache.dateRange = v })

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
    data.value = await fetchSummary(toISOStart(s), toISOEnd(e))
  } catch (e) {
    error.value = e.response?.data?.detail || e.message
  } finally {
    loading.value = false
  }
}

const totalCost = computed(() => data.value?.total_cost ?? 0)
const totalCostUsd = computed(() => data.value?.total_cost_usd ?? 0)
const totalCostCny = computed(() => data.value?.total_cost_cny ?? 0)
const textCost = computed(() => data.value?.text_cost ?? 0)
const textCostUsd = computed(() => data.value?.text_cost_usd ?? 0)
const textCostCny = computed(() => data.value?.text_cost_cny ?? 0)
const imageCost = computed(() => data.value?.image_cost ?? 0)
const imageCostUsd = computed(() => data.value?.image_cost_usd ?? 0)
const imageCostCny = computed(() => data.value?.image_cost_cny ?? 0)

// Token 汇总
const tokenStats = computed(() => {
  if (!data.value?.text_models) return { input: 0, output: 0, cache: 0, total: 0, calls: 0 }
  let input = 0, output = 0, cache = 0, calls = 0
  for (const m of Object.values(data.value.text_models)) {
    input += m.input?.tokens ?? 0
    output += m.output?.tokens ?? 0
    cache += m.cacheinput?.tokens ?? 0
    calls += m.total_count ?? 0
  }
  // 生图调用次数
  for (const m of Object.values(data.value.image_models ?? {})) {
    calls += m.total_count ?? 0
  }
  return { input, output, cache, total: input + output + cache, calls }
})

const hasImageData = computed(() => Object.keys(data.value?.image_models ?? {}).length > 0)

function fmtTokens(n) {
  if (n >= 1_000_000_000) return (n / 1_000_000_000).toFixed(2) + 'B'
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K'
  return n.toString()
}

const shortcuts = [
  { text: '今天', value: () => { const d = new Date(); return [d, d] } },
  { text: '最近3天', value: () => { const e = new Date(); const s = new Date(); s.setDate(e.getDate() - 2); return [s, e] } },
  { text: '最近7天', value: () => { const e = new Date(); const s = new Date(); s.setDate(e.getDate() - 6); return [s, e] } },
  { text: '最近30天', value: () => { const e = new Date(); const s = new Date(); s.setDate(e.getDate() - 29); return [s, e] } },
  { text: '最近90天', value: () => { const e = new Date(); const s = new Date(); s.setDate(e.getDate() - 89); return [s, e] } },
]

function disabledDate(time) {
  const today = new Date()
  today.setHours(23, 59, 59, 999)
  const earliest = new Date()
  earliest.setDate(earliest.getDate() - 89)
  earliest.setHours(0, 0, 0, 0)
  return time < earliest || time > today
}

onMounted(() => {
  if (!data.value) loadData()  // 有缓存就不重新请求
})
</script>

<template>
  <div class="dashboard">
    <div class="header">
      <h2>tencent</h2>
      <el-tabs v-model="activeTab" class="header-tabs">
        <el-tab-pane label="用量总览" name="usage" />
        <el-tab-pane label="定价管理" name="pricing" />
      </el-tabs>
    </div>

    <!-- 用量总览 -->
    <template v-if="activeTab === 'usage'">
      <div class="date-bar">
        <el-date-picker
          v-model="dateRange"
          type="daterange"
          range-separator="至"
          start-placeholder="开始日期"
          end-placeholder="结束日期"
          size="small"
          style="width: 340px"
          :shortcuts="shortcuts"
          :disabled-date="disabledDate"
          value-format="YYYY-MM-DD"
          @change="loadData"
        />
        <el-button type="primary" size="small" @click="loadData" :loading="loading">查询</el-button>
        <span class="range-hint">最多查询近 90 天</span>
      </div>

      <el-alert v-if="error" :title="error" type="error" show-icon closable style="margin-bottom: 16px" />

      <!-- 费用卡片 -->
      <div class="cards-row" v-if="data">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-label">总费用 (USD)</div>
          <div class="stat-value primary">${{ totalCostUsd.toFixed(2) }}</div>
          <div class="stat-sub">{{ tokenStats.calls.toLocaleString() }} 次调用</div>
        </el-card>
        <el-card shadow="hover" class="stat-card">
          <div class="stat-label">总费用 (CNY)</div>
          <div class="stat-value primary">¥{{ totalCostCny.toFixed(2) }}</div>
        </el-card>
        <el-card shadow="hover" class="stat-card">
          <div class="stat-label">生文费用</div>
          <div class="stat-value primary">${{ textCostUsd.toFixed(2) }}</div>
          <div class="stat-sub">¥{{ textCostCny.toFixed(2) }}</div>
        </el-card>
        <el-card shadow="hover" class="stat-card">
          <div class="stat-label">生图费用</div>
          <div class="stat-value primary">${{ imageCostUsd.toFixed(2) }}</div>
          <div class="stat-sub">¥{{ imageCostCny.toFixed(2) }}</div>
        </el-card>
      </div>

      <!-- Token 卡片 -->
      <div class="cards-row" v-if="data">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-label">总 Tokens</div>
          <div class="stat-value primary">{{ fmtTokens(tokenStats.total) }}</div>
        </el-card>
        <el-card shadow="hover" class="stat-card">
          <div class="stat-label">输入 Tokens</div>
          <div class="stat-value">{{ fmtTokens(tokenStats.input) }}</div>
        </el-card>
        <el-card shadow="hover" class="stat-card">
          <div class="stat-label">输出 Tokens</div>
          <div class="stat-value">{{ fmtTokens(tokenStats.output) }}</div>
        </el-card>
        <el-card shadow="hover" class="stat-card">
          <div class="stat-label">缓存 Tokens</div>
          <div class="stat-value">{{ fmtTokens(tokenStats.cache) }}</div>
        </el-card>
      </div>

      <!-- 图表 -->
      <div class="charts" v-if="data">
        <CostChart :daily="data.daily" :textModels="data.text_models" :imageModels="data.image_models" />
      </div>

      <!-- 生文明细表 -->
      <div class="table-section" v-if="data">
        <el-card>
          <template #header><span style="font-weight: 600">生文模型费用明细</span></template>
          <ModelTable :models="data.text_models" />
        </el-card>
      </div>

      <!-- 生图明细表 -->
      <div class="table-section" v-if="data && hasImageData">
        <el-card>
          <template #header><span style="font-weight: 600">生图模型费用明细</span></template>
          <ImageTable :models="data.image_models" />
        </el-card>
      </div>
    </template>

    <!-- 定价管理 -->
    <template v-if="activeTab === 'pricing'">
      <PricingAdmin />
    </template>
  </div>
</template>

<style scoped>
.header {
  display: flex;
  align-items: center;
  gap: 32px;
  margin-bottom: 24px;
}
.header h2 {
  margin: 0;
  font-size: 20px;
  color: #1a1a1a;
  white-space: nowrap;
}
.header-tabs {
  flex: 1;
}
.header-tabs :deep(.el-tabs__header) {
  margin: 0;
}
.date-bar {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  align-items: center;
  margin-bottom: 16px;
}
.date-bar .el-date-editor {
  max-width: 340px;
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
.table-section {
  margin-bottom: 24px;
}
</style>
