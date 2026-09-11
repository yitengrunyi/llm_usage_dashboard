<script setup>
defineOptions({ name: 'Points' })
import { ref, computed, onMounted, watch, onUnmounted } from 'vue'
import { fetchPointsData } from '../api/index.js'
import { sharedDateRange } from '../store/vendorCache.js'
import { Coin, ArrowDown } from '@element-plus/icons-vue'
import * as echarts from 'echarts'

const loading = ref(false)
const data = ref(null)
const error = ref('')

// 日期范围：从全局拷贝快照
const dateRange = ref([...sharedDateRange.value])
const startDate = computed({
  get: () => dateRange.value[0],
  set: (v) => { dateRange.value = [v, dateRange.value[1] || v] },
})
const endDate = computed({
  get: () => dateRange.value[1],
  set: (v) => { dateRange.value = [dateRange.value[0] || v, v] },
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
  loading.value = true
  error.value = ''
  try {
    const [s, e] = dateRange.value
    data.value = await fetchPointsData(toISOStart(s), toISOEnd(e))
  } catch (e) {
    error.value = e.response?.data?.detail || e.message
  } finally {
    loading.value = false
  }
}

const shortcuts = [
  { text: '最近7天', value: () => { const e = new Date(); e.setDate(e.getDate() - 1); const s = new Date(); s.setDate(e.getDate() - 6); return [s, e] } },
  { text: '最近30天', value: () => { const e = new Date(); e.setDate(e.getDate() - 1); const s = new Date(); s.setDate(e.getDate() - 29); return [s, e] } },
  { text: '最近90天', value: () => { const e = new Date(); e.setDate(e.getDate() - 1); const s = new Date(); s.setDate(e.getDate() - 89); return [s, e] } },
]

function disabledDate(time) {
  const today = new Date()
  today.setHours(23, 59, 59, 999)
  return time > today
}

// 格式化数字
function fmtTokens(n) {
  if (n == null) return '-'
  if (n >= 1_000_000_000) return (n / 1_000_000_000).toFixed(2) + 'B'
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K'
  return n.toString()
}

function fmtPoints(n) {
  if (n == null) return '-'
  return n.toLocaleString()
}

// 总计数据
const totalPoints = computed(() => data.value?.total_points ?? 0)
const totalRecharge = computed(() => data.value?.total_recharge ?? 0)
const totalRefund = computed(() => data.value?.total_refund ?? 0)
const totalInputTokens = computed(() => {
  if (!data.value?.by_parent_type) return 0
  return Object.values(data.value.by_parent_type).reduce((sum, p) => sum + (p.input_tokens || 0), 0)
})
const totalOutputTokens = computed(() => {
  if (!data.value?.by_parent_type) return 0
  return Object.values(data.value.by_parent_type).reduce((sum, p) => sum + (p.output_tokens || 0), 0)
})
const totalCacheReadTokens = computed(() => {
  if (!data.value?.by_parent_type) return 0
  return Object.values(data.value.by_parent_type).reduce((sum, p) => sum + (p.cache_read_tokens || 0), 0)
})
const totalCacheWriteTokens = computed(() => {
  if (!data.value?.by_parent_type) return 0
  return Object.values(data.value.by_parent_type).reduce((sum, p) => sum + (p.cache_write_tokens || 0), 0)
})

// 图表
const pointsTrendRef = ref(null)
const pointsDistRef = ref(null)
const rechargeTrendRef = ref(null)
const refundTrendRef = ref(null)
const inputTokensTrendRef = ref(null)
const inputTokensDistRef = ref(null)
const outputTokensTrendRef = ref(null)
const outputTokensDistRef = ref(null)
const cacheReadTokensTrendRef = ref(null)
const cacheReadTokensDistRef = ref(null)
const cacheWriteTokensTrendRef = ref(null)
const cacheWriteTokensDistRef = ref(null)

let pointsTrendChart = null
let pointsDistChart = null
let rechargeTrendChart = null
let refundTrendChart = null
let inputTokensTrendChart = null
let inputTokensDistChart = null
let outputTokensTrendChart = null
let outputTokensDistChart = null
let cacheReadTokensTrendChart = null
let cacheReadTokensDistChart = null
let cacheWriteTokensTrendChart = null
let cacheWriteTokensDistChart = null

function renderPointsTrend() {
  if (!pointsTrendRef.value || !data.value?.daily) return
  if (!pointsTrendChart) pointsTrendChart = echarts.init(pointsTrendRef.value)

  const daily = data.value.daily || []
  const dates = daily.map(d => d.date)
  const points = daily.map(d => d.points)

  pointsTrendChart.setOption({
    title: { text: '每日消费积分趋势', left: 'center', textStyle: { fontSize: 14 } },
    tooltip: {
      trigger: 'axis',
      formatter: params => {
        const p = params[0]
        return `${p.name}<br/>消费积分: ${p.value.toLocaleString()}`
      },
    },
    grid: { left: 60, right: 20, bottom: 30, top: 40 },
    xAxis: { type: 'category', data: dates },
    yAxis: { type: 'value', name: '消费积分', axisLabel: { formatter: v => v.toLocaleString() } },
    series: [{
      type: 'line',
      data: points,
      smooth: true,
      areaStyle: { opacity: 0.15 },
      itemStyle: { color: '#faad14' },
    }],
  }, true)
}

function renderRechargeTrend() {
  if (!rechargeTrendRef.value || !data.value?.daily_recharge) return
  if (!rechargeTrendChart) rechargeTrendChart = echarts.init(rechargeTrendRef.value)

  const daily = data.value.daily_recharge || []
  const dates = daily.map(d => d.date)
  const amounts = daily.map(d => d.amount)

  rechargeTrendChart.setOption({
    title: { text: '每日充值积分趋势', left: 'center', textStyle: { fontSize: 14 } },
    tooltip: {
      trigger: 'axis',
      formatter: params => {
        const p = params[0]
        return `${p.name}<br/>充值积分: ${p.value.toLocaleString()}`
      },
    },
    grid: { left: 60, right: 20, bottom: 30, top: 40 },
    xAxis: { type: 'category', data: dates },
    yAxis: { type: 'value', name: '充值积分', axisLabel: { formatter: v => v.toLocaleString() } },
    series: [{
      type: 'line',
      data: amounts,
      smooth: true,
      areaStyle: { opacity: 0.15 },
      itemStyle: { color: '#52c41a' },
    }],
  }, true)
}

function renderRefundTrend() {
  if (!refundTrendRef.value || !data.value?.daily_refund) return
  if (!refundTrendChart) refundTrendChart = echarts.init(refundTrendRef.value)

  const daily = data.value.daily_refund || []
  const dates = daily.map(d => d.date)
  const amounts = daily.map(d => d.amount)

  refundTrendChart.setOption({
    title: { text: '每日退款积分趋势', left: 'center', textStyle: { fontSize: 14 } },
    tooltip: {
      trigger: 'axis',
      formatter: params => {
        const p = params[0]
        return `${p.name}<br/>退款积分: ${p.value.toLocaleString()}`
      },
    },
    grid: { left: 60, right: 20, bottom: 30, top: 40 },
    xAxis: { type: 'category', data: dates },
    yAxis: { type: 'value', name: '退款积分', axisLabel: { formatter: v => v.toLocaleString() } },
    series: [{
      type: 'line',
      data: amounts,
      smooth: true,
      areaStyle: { opacity: 0.15 },
      itemStyle: { color: '#ff4d4f' },
    }],
  }, true)
}

function renderPointsDist() {
  if (!pointsDistRef.value || !data.value?.by_parent_type) return
  if (!pointsDistChart) pointsDistChart = echarts.init(pointsDistRef.value)

  const parentTypes = data.value.by_parent_type || {}
  const typeMap = {
    10: 'AI转纪要',
    22: '翻译',
    130: 'PaiPai问答',
  }

  const chartData = Object.entries(parentTypes).map(([type, info]) => ({
    name: typeMap[type] || `类型${type}`,
    value: info.points,
  }))

  pointsDistChart.setOption({
    title: { text: '积分分布', left: 'center', textStyle: { fontSize: 14 } },
    tooltip: {
      trigger: 'item',
      formatter: params => `${params.name}<br/>积分: ${params.value.toLocaleString()} (${params.percent}%)`,
    },
    legend: { bottom: 10, left: 'center' },
    series: [{
      type: 'pie',
      radius: ['40%', '70%'],
      avoidLabelOverlap: false,
      itemStyle: {
        borderRadius: 10,
        borderColor: '#fff',
        borderWidth: 2,
      },
      label: { show: true, formatter: '{b}: {d}%' },
      data: chartData,
    }],
  }, true)
}

// Token 趋势图渲染函数
function renderTokenTrend(chartRef, tokenKey, title, color) {
  if (!chartRef.value || !data.value?.daily_tokens) return

  const dailyData = data.value.daily_tokens[tokenKey] || []

  // 检查是否所有值为0
  const hasData = dailyData.some(d => d.value > 0)
  if (dailyData.length === 0 || !hasData) {
    // 隐藏整行（chart-row）
    if (chartRef.value.parentElement && chartRef.value.parentElement.parentElement) {
      chartRef.value.parentElement.parentElement.style.display = 'none'
    }
    return
  }

  // 显示整行
  if (chartRef.value.parentElement && chartRef.value.parentElement.parentElement) {
    chartRef.value.parentElement.parentElement.style.display = ''
  }

  let chartInstance = null
  if (chartRef.value === inputTokensTrendRef.value) {
    if (!inputTokensTrendChart) inputTokensTrendChart = echarts.init(chartRef.value)
    chartInstance = inputTokensTrendChart
  } else if (chartRef.value === outputTokensTrendRef.value) {
    if (!outputTokensTrendChart) outputTokensTrendChart = echarts.init(chartRef.value)
    chartInstance = outputTokensTrendChart
  } else if (chartRef.value === cacheReadTokensTrendRef.value) {
    if (!cacheReadTokensTrendChart) cacheReadTokensTrendChart = echarts.init(chartRef.value)
    chartInstance = cacheReadTokensTrendChart
  } else if (chartRef.value === cacheWriteTokensTrendRef.value) {
    if (!cacheWriteTokensTrendChart) cacheWriteTokensTrendChart = echarts.init(chartRef.value)
    chartInstance = cacheWriteTokensTrendChart
  }

  if (!chartInstance) return

  const dates = dailyData.map(d => d.date)
  const values = dailyData.map(d => d.value)

  chartInstance.setOption({
    title: { text: title, left: 'center', textStyle: { fontSize: 14 } },
    tooltip: {
      trigger: 'axis',
      formatter: params => {
        const p = params[0]
        return `${p.name}<br/>${title.replace('每日', '').replace('趋势', '')}: ${fmtTokens(p.value)}`
      },
    },
    grid: { left: 60, right: 20, bottom: 30, top: 40 },
    xAxis: { type: 'category', data: dates },
    yAxis: { type: 'value', name: title.replace('每日', '').replace('趋势', ''), axisLabel: { formatter: fmtTokens } },
    series: [{
      type: 'line',
      data: values,
      smooth: true,
      areaStyle: { opacity: 0.15 },
      itemStyle: { color: color },
    }],
  }, true)
}

// Token 分布图渲染函数
function renderTokenDist(chartRef, tokenKey, title, color) {
  if (!chartRef.value || !data.value?.by_parent_type) return

  const parentTypes = data.value.by_parent_type || {}
  const typeMap = {
    10: 'AI转纪要',
    22: '翻译',
    130: 'PaiPai问答',
  }

  const chartData = Object.entries(parentTypes)
    .map(([type, info]) => ({
      name: typeMap[type] || `类型${type}`,
      value: info[tokenKey] || 0,
    }))
    .filter(item => item.value > 0)

  if (chartData.length === 0) {
    // 隐藏整行（chart-row）
    if (chartRef.value.parentElement && chartRef.value.parentElement.parentElement) {
      chartRef.value.parentElement.parentElement.style.display = 'none'
    }
    return
  }

  // 显示整行
  if (chartRef.value.parentElement && chartRef.value.parentElement.parentElement) {
    chartRef.value.parentElement.parentElement.style.display = ''
  }

  let chartInstance = null
  if (chartRef.value === inputTokensDistRef.value) {
    if (!inputTokensDistChart) inputTokensDistChart = echarts.init(chartRef.value)
    chartInstance = inputTokensDistChart
  } else if (chartRef.value === outputTokensDistRef.value) {
    if (!outputTokensDistChart) outputTokensDistChart = echarts.init(chartRef.value)
    chartInstance = outputTokensDistChart
  } else if (chartRef.value === cacheReadTokensDistRef.value) {
    if (!cacheReadTokensDistChart) cacheReadTokensDistChart = echarts.init(chartRef.value)
    chartInstance = cacheReadTokensDistChart
  } else if (chartRef.value === cacheWriteTokensDistRef.value) {
    if (!cacheWriteTokensDistChart) cacheWriteTokensDistChart = echarts.init(chartRef.value)
    chartInstance = cacheWriteTokensDistChart
  }

  if (!chartInstance) return

  chartInstance.setOption({
    title: { text: title, left: 'center', textStyle: { fontSize: 14 } },
    tooltip: {
      trigger: 'item',
      formatter: params => `${params.name}<br/>${title.replace('业务类型', '')}: ${fmtTokens(params.value)} (${params.percent}%)`,
    },
    legend: { bottom: 10, left: 'center' },
    series: [{
      type: 'pie',
      radius: ['40%', '70%'],
      avoidLabelOverlap: false,
      itemStyle: {
        borderRadius: 10,
        borderColor: '#fff',
        borderWidth: 2,
      },
      label: { show: true, formatter: '{b}: {d}%' },
      data: chartData,
      color: color,
    }],
  }, true)
}

function renderAllCharts() {
  renderPointsTrend()
  renderPointsDist()
  renderRechargeTrend()
  renderRefundTrend()

  // Token 趋势图
  renderTokenTrend(inputTokensTrendRef, 'input_tokens', '每日输入Tokens趋势', '#5470c6')
  renderTokenTrend(outputTokensTrendRef, 'output_tokens', '每日输出Tokens趋势', '#91cc75')
  renderTokenTrend(cacheReadTokensTrendRef, 'cache_read_tokens', '每日缓存读Tokens趋势', '#fac858')
  renderTokenTrend(cacheWriteTokensTrendRef, 'cache_write_tokens', '每日缓存写Tokens趋势', '#ee6666')

  // Token 分布图
  renderTokenDist(inputTokensDistRef, 'input_tokens', '输入Tokens分布', ['#5470c6', '#91cc75', '#fac858'])
  renderTokenDist(outputTokensDistRef, 'output_tokens', '输出Tokens分布', ['#ee6666', '#73c0de', '#3ba272'])
  renderTokenDist(cacheReadTokensDistRef, 'cache_read_tokens', '缓存读Tokens分布', ['#9a60b4', '#ea7ccc', '#fc8452'])
  renderTokenDist(cacheWriteTokensDistRef, 'cache_write_tokens', '缓存写Tokens分布', ['#5470c6', '#91cc75', '#fac858'])
}

// 明细表数据 - 折叠式
const expandedRows = ref([])

const tableData = computed(() => {
  if (!data.value?.by_parent_type) return []

  const parentTypes = data.value.by_parent_type || {}
  const typeMap = {
    10: 'AI转纪要',
    22: '翻译',
    130: 'PaiPai问答',
  }

  const rows = []

  // 添加父类型行
  for (const [type, info] of Object.entries(parentTypes)) {
    rows.push({
      id: `parent_${type}`,
      type: 'parent',
      name: typeMap[type] || `类型${type}`,
      biz_type: parseInt(type),
      input_tokens: info.input_tokens,
      output_tokens: info.output_tokens,
      cache_read_tokens: info.cache_read_tokens,
      cache_write_tokens: info.cache_write_tokens,
      points: info.points,
      children: info.sub_types || [],
    })
  }

  // 按积分降序排列
  rows.sort((a, b) => b.points - a.points)

  return rows
})

// 合计行数据
const summaryData = computed(() => {
  if (!data.value?.by_parent_type) return null

  const parentTypes = data.value.by_parent_type || {}
  let total_input = 0, total_output = 0, total_cache_read = 0, total_cache_write = 0, total_points = 0

  for (const info of Object.values(parentTypes)) {
    total_input += info.input_tokens || 0
    total_output += info.output_tokens || 0
    total_cache_read += info.cache_read_tokens || 0
    total_cache_write += info.cache_write_tokens || 0
    total_points += info.points || 0
  }

  return {
    input_tokens: total_input,
    output_tokens: total_output,
    cache_read_tokens: total_cache_read,
    cache_write_tokens: total_cache_write,
    points: total_points,
  }
})

function handleResize() {
  pointsTrendChart?.resize()
  pointsDistChart?.resize()
  rechargeTrendChart?.resize()
  refundTrendChart?.resize()
  inputTokensTrendChart?.resize()
  outputTokensTrendChart?.resize()
  cacheReadTokensTrendChart?.resize()
  cacheWriteTokensTrendChart?.resize()
  inputTokensDistChart?.resize()
  outputTokensDistChart?.resize()
  cacheReadTokensDistChart?.resize()
  cacheWriteTokensDistChart?.resize()
}

watch(() => data.value, async () => {
  if (!data.value) return
  // 等待 DOM 更新完成后再渲染图表
  await new Promise(resolve => setTimeout(resolve, 0))
  renderAllCharts()
}, { deep: true })

onMounted(async () => {
  await loadData()
  window.addEventListener('resize', handleResize)
})

onUnmounted(() => {
  window.removeEventListener('resize', handleResize)
  pointsTrendChart?.dispose()
  pointsDistChart?.dispose()
  rechargeTrendChart?.dispose()
  refundTrendChart?.dispose()
  inputTokensTrendChart?.dispose()
  outputTokensTrendChart?.dispose()
  cacheReadTokensTrendChart?.dispose()
  cacheWriteTokensTrendChart?.dispose()
  inputTokensDistChart?.dispose()
  outputTokensDistChart?.dispose()
  cacheReadTokensDistChart?.dispose()
  cacheWriteTokensDistChart?.dispose()
})
</script>

<template>
  <div class="points-page">
    <div class="header">
      <h2><el-icon style="vertical-align: middle; margin-right: 4px"><Coin /></el-icon>积分</h2>
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
      </div>
    </div>

    <el-alert v-if="error" :title="error" type="error" show-icon closable style="margin-bottom: 16px" />

    <!-- 顶部统计卡片 - 第一行：积分 -->
    <div class="cards-row" v-if="data">
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">总消费积分</div>
        <div class="stat-value primary">{{ fmtPoints(totalPoints) }}</div>
      </el-card>
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">总充值积分</div>
        <div class="stat-value success">{{ fmtPoints(totalRecharge) }}</div>
      </el-card>
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">总退款积分</div>
        <div class="stat-value danger">{{ fmtPoints(totalRefund) }}</div>
      </el-card>
    </div>

    <!-- 顶部统计卡片 - 第二行：Tokens -->
    <div class="cards-row" v-if="data">
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">输入Tokens</div>
        <div class="stat-value">{{ fmtTokens(totalInputTokens) }}</div>
      </el-card>
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">输出Tokens</div>
        <div class="stat-value">{{ fmtTokens(totalOutputTokens) }}</div>
      </el-card>
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">缓存读Tokens</div>
        <div class="stat-value">{{ fmtTokens(totalCacheReadTokens) }}</div>
      </el-card>
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">缓存写Tokens</div>
        <div class="stat-value">{{ fmtTokens(totalCacheWriteTokens) }}</div>
      </el-card>
    </div>

    <!-- 图表 -->
    <div class="charts" v-if="data">
      <!-- 消费积分图表 -->
      <div class="chart-row">
        <el-card class="chart-card">
          <div ref="pointsTrendRef" class="chart"></div>
        </el-card>
        <el-card class="chart-card">
          <div ref="pointsDistRef" class="chart"></div>
        </el-card>
      </div>

      <!-- 充值 / 退款积分图表 -->
      <div class="chart-row">
        <el-card class="chart-card">
          <div ref="rechargeTrendRef" class="chart"></div>
        </el-card>
        <el-card class="chart-card">
          <div ref="refundTrendRef" class="chart"></div>
        </el-card>
      </div>

      <!-- 输入Tokens -->
      <div class="chart-row">
        <el-card class="chart-card">
          <div ref="inputTokensTrendRef" class="chart"></div>
        </el-card>
        <el-card class="chart-card">
          <div ref="inputTokensDistRef" class="chart"></div>
        </el-card>
      </div>

      <!-- 输出Tokens -->
      <div class="chart-row">
        <el-card class="chart-card">
          <div ref="outputTokensTrendRef" class="chart"></div>
        </el-card>
        <el-card class="chart-card">
          <div ref="outputTokensDistRef" class="chart"></div>
        </el-card>
      </div>

      <!-- 缓存读Tokens -->
      <div class="chart-row">
        <el-card class="chart-card">
          <div ref="cacheReadTokensTrendRef" class="chart"></div>
        </el-card>
        <el-card class="chart-card">
          <div ref="cacheReadTokensDistRef" class="chart"></div>
        </el-card>
      </div>

      <!-- 缓存写Tokens -->
      <div class="chart-row">
        <el-card class="chart-card">
          <div ref="cacheWriteTokensTrendRef" class="chart"></div>
        </el-card>
        <el-card class="chart-card">
          <div ref="cacheWriteTokensDistRef" class="chart"></div>
        </el-card>
      </div>
    </div>

    <!-- 积分明细 -->
    <div class="table-section" v-if="data">
      <el-card>
        <template #header><span style="font-weight: 600">消费积分明细</span></template>
        <el-table
          :data="tableData"
          style="width: 100%"
          row-key="id"
          :expand-row-keys="expandedRows"
          @expand-change="(row, expanded) => { if (expanded.includes(row)) expandedRows.push(row.id); else expandedRows = expandedRows.filter(id => id !== row.id) }"
        >
          <el-table-column type="expand">
            <template #default="{ row }">
              <div class="sub-table">
                <el-table :data="row.children" style="width: 100%" :show-header="false">
                  <el-table-column width="48" />
                  <el-table-column prop="name" min-width="200">
                    <template #default="{ row }">
                      <span style="padding-left: 24px">{{ row.name }}</span>
                    </template>
                  </el-table-column>
                  <el-table-column prop="input_tokens" width="130" align="right">
                    <template #default="{ row }">{{ fmtTokens(row.input_tokens) }}</template>
                  </el-table-column>
                  <el-table-column prop="output_tokens" width="130" align="right">
                    <template #default="{ row }">{{ fmtTokens(row.output_tokens) }}</template>
                  </el-table-column>
                  <el-table-column prop="cache_read_tokens" width="150" align="right">
                    <template #default="{ row }">{{ fmtTokens(row.cache_read_tokens) }}</template>
                  </el-table-column>
                  <el-table-column prop="cache_write_tokens" width="150" align="right">
                    <template #default="{ row }">{{ fmtTokens(row.cache_write_tokens) }}</template>
                  </el-table-column>
                  <el-table-column prop="points" width="130" align="right">
                    <template #default="{ row }">
                      <span style="font-weight: 600; color: #faad14">{{ fmtPoints(row.points) }}</span>
                    </template>
                  </el-table-column>
                </el-table>
              </div>
            </template>
          </el-table-column>
          <el-table-column prop="name" label="业务类型" min-width="200" />
          <el-table-column prop="input_tokens" label="输入Tokens" width="130" align="right">
            <template #default="{ row }">{{ fmtTokens(row.input_tokens) }}</template>
          </el-table-column>
          <el-table-column prop="output_tokens" label="输出Tokens" width="130" align="right">
            <template #default="{ row }">{{ fmtTokens(row.output_tokens) }}</template>
          </el-table-column>
          <el-table-column prop="cache_read_tokens" label="缓存读Tokens" width="150" align="right">
            <template #default="{ row }">{{ fmtTokens(row.cache_read_tokens) }}</template>
          </el-table-column>
          <el-table-column prop="cache_write_tokens" label="缓存写Tokens" width="150" align="right">
            <template #default="{ row }">{{ fmtTokens(row.cache_write_tokens) }}</template>
          </el-table-column>
          <el-table-column prop="points" label="积分" width="130" align="right">
            <template #default="{ row }">
              <span style="font-weight: 600; color: #faad14">{{ fmtPoints(row.points) }}</span>
            </template>
          </el-table-column>
        </el-table>

        <!-- 合计行 -->
        <div class="summary-row" v-if="summaryData">
          <el-table :data="[summaryData]" style="width: 100%; margin-top: 0" :show-header="false">
            <el-table-column width="48" />
            <el-table-column prop="name" min-width="200">
              <template #default><strong>合计</strong></template>
            </el-table-column>
            <el-table-column width="130" align="right">
              <template #default><strong>{{ fmtTokens(summaryData.input_tokens) }}</strong></template>
            </el-table-column>
            <el-table-column width="130" align="right">
              <template #default><strong>{{ fmtTokens(summaryData.output_tokens) }}</strong></template>
            </el-table-column>
            <el-table-column width="150" align="right">
              <template #default><strong>{{ fmtTokens(summaryData.cache_read_tokens) }}</strong></template>
            </el-table-column>
            <el-table-column width="150" align="right">
              <template #default><strong>{{ fmtTokens(summaryData.cache_write_tokens) }}</strong></template>
            </el-table-column>
            <el-table-column width="130" align="right">
              <template #default>
                <strong style="color: #faad14">{{ fmtPoints(summaryData.points) }}</strong>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-card>
    </div>
  </div>
</template>

<style scoped>
.points-page {
  padding: 0 24px 24px;
}
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
.cards-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
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
  font-size: 28px;
  font-weight: 700;
  color: #1a1a1a;
  line-height: 1.2;
}
.stat-value.primary {
  color: #faad14;
}
.stat-value.success {
  color: #52c41a;
}
.stat-value.danger {
  color: #ff4d4f;
}
.charts {
  margin-bottom: 24px;
}
.chart-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-bottom: 16px;
}
.chart {
  min-height: 360px;
}
.table-section {
  margin-bottom: 24px;
}
.sub-table {
  padding: 0;
  background: #fafafa;
}
.sub-table :deep(.el-table td) {
  background: #fafafa !important;
}
.summary-row {
  border-top: 2px solid #e8e8e8;
  background: #fafafa;
}
.summary-row :deep(.el-table td) {
  background: #fafafa !important;
  font-weight: 600;
}
</style>
