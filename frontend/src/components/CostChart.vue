<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import * as echarts from 'echarts'

const props = defineProps({
  daily: { type: Array, default: () => [] },
  textModels: { type: Object, default: () => ({}) },
  imageModels: { type: Object, default: () => ({}) },
  // 选中 N 个 model 时, 后端额外返的 per-model daily — {model: [{date, cost, tokens}, ...]}.
  // 给则趋势图画多条曲线对比; null 则走原"全模型 SUM"单曲线.
  dailyByModel: { type: Object, default: null },
})

const trendRef = ref(null)
const tokenTrendRef = ref(null)
const barRef = ref(null)
let trendChart = null
let tokenTrendChart = null
let barChart = null

// 粒度: 'auto' / 'day' / 'week' / 'month'.
// auto: ≤14 天日, ≤90 天周, >90 天月
const granularity = ref('auto')
const effectiveGranularity = computed(() => {
  if (granularity.value !== 'auto') return granularity.value
  const n = props.daily.length
  if (n <= 14) return 'day'
  if (n <= 90) return 'week'
  return 'month'
})

function fmtTokens(n) {
  if (n >= 1e12) return (n / 1e12).toFixed(2) + 'T'
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B'
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M'
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K'
  return String(n)
}

// 把 [{date:'YYYY-MM-DD', cost, tokens}] 按粒度聚合.
// 周: ISO 周一为起点 (跟 _default_week_window 对齐). 月: 月初.
function aggregateDaily(daily, gran) {
  if (gran === 'day' || !daily.length) return daily
  const buckets = new Map()  // bucket_key → {date, cost, tokens, hasTokens}
  for (const d of daily) {
    const dt = new Date(d.date + 'T00:00:00')
    let key
    if (gran === 'week') {
      // 周一为起点: 周日 dow=0 → 减 6 天, 周一=1 → 减 0
      const dow = dt.getDay()
      const offset = dow === 0 ? 6 : dow - 1
      const monday = new Date(dt)
      monday.setDate(dt.getDate() - offset)
      key = monday.toISOString().slice(0, 10)
    } else {  // month
      key = `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}-01`
    }
    const b = buckets.get(key) || { date: key, cost: 0, tokens: 0, hasTokens: false }
    b.cost += d.cost || 0
    if (d.tokens != null) {
      b.tokens += d.tokens
      b.hasTokens = true
    }
    buckets.set(key, b)
  }
  return [...buckets.values()]
    .sort((a, b) => a.date.localeCompare(b.date))
    .map(b => ({ date: b.date, cost: b.cost, tokens: b.hasTokens ? b.tokens : null }))
}

const aggDaily = computed(() => aggregateDaily(props.daily, effectiveGranularity.value))

// 多曲线模式: 选了 N 个 model → per-model 序列也按粒度聚合.
// 返回 {model: [{date, cost, tokens}]}, 没数据的 (date, model) 不补零, 让线自然断点.
const aggDailyByModel = computed(() => {
  const raw = props.dailyByModel
  if (!raw || !Object.keys(raw).length) return null
  const gran = effectiveGranularity.value
  const out = {}
  for (const [m, series] of Object.entries(raw)) {
    out[m] = aggregateDaily(series, gran)
  }
  return out
})

// x 轴日期集合: 多曲线模式取并集 (保证一条线某天缺数据时也对得齐); 单曲线就是 aggDaily.
const chartDates = computed(() => {
  const bym = aggDailyByModel.value
  if (!bym) return aggDaily.value.map(d => d.date)
  const set = new Set()
  for (const series of Object.values(bym)) for (const d of series) set.add(d.date)
  return [...set].sort()
})

function trendTitle(prefix) {
  const g = effectiveGranularity.value
  const suffix = g === 'day' ? '每日' : g === 'week' ? '每周' : '每月'
  return prefix.replace('每日', suffix)
}

function renderTrend() {
  if (!trendRef.value || !aggDaily.value.length) return
  if (!trendChart) trendChart = echarts.init(trendRef.value)

  const bym = aggDailyByModel.value
  const dates = chartDates.value
  const titleBase = bym ? '每日费用对比 (按模型)' : '每日费用趋势（生文+生图）'

  // bym 模式: 每个 model 一条 series; 用 date → cost map 对齐 x 轴, 缺的位置塞 null (线断开)
  let series
  if (bym) {
    series = Object.entries(bym).map(([m, ser]) => {
      const map = new Map(ser.map(d => [d.date, d.cost]))
      return { name: m, type: 'line', smooth: true, connectNulls: false,
               data: dates.map(d => map.has(d) ? map.get(d) : null) }
    })
  } else {
    const map = new Map(aggDaily.value.map(d => [d.date, d.cost]))
    series = [{
      type: 'line', data: dates.map(d => map.get(d) ?? 0), smooth: true,
      areaStyle: { opacity: 0.15 }, itemStyle: { color: '#1890ff' },
    }]
  }

  trendChart.setOption({
    title: { text: trendTitle(titleBase), left: 'center', textStyle: { fontSize: 14 } },
    legend: bym ? { top: 28, type: 'scroll' } : { show: false },
    tooltip: {
      trigger: 'axis',
      formatter: params => {
        // 多曲线: 拼每条 series 的值; 单曲线: 老逻辑
        if (bym) {
          const head = `${params[0].name}<br/>`
          return head + params
            .filter(p => p.value != null)
            .map(p => `${p.marker} ${p.seriesName}: ¥${p.value.toFixed(4)}`)
            .join('<br/>')
        }
        const p = params[0]
        return `${p.name}<br/>费用: ¥${p.value.toFixed(4)}`
      },
    },
    grid: { left: 60, right: 20, bottom: 30, top: bym ? 60 : 40 },
    xAxis: { type: 'category', data: dates },
    yAxis: { type: 'value', name: '费用(元)', axisLabel: { formatter: v => '¥' + v.toFixed(2) } },
    series,
  }, true)
}

function renderTokenTrend() {
  if (!tokenTrendRef.value || !aggDaily.value.length) return
  const bym = aggDailyByModel.value
  const dates = chartDates.value

  // 检查有没有 token 数据 — 多曲线模式看任一 series 有, 单曲线看 aggDaily
  const hasTokens = bym
    ? Object.values(bym).some(ser => ser.some(d => d.tokens != null && d.tokens > 0))
    : aggDaily.value.some(d => d.tokens != null && d.tokens > 0)
  if (!hasTokens) {
    tokenTrendRef.value.style.display = 'none'
    return
  }
  tokenTrendRef.value.style.display = ''
  if (!tokenTrendChart) tokenTrendChart = echarts.init(tokenTrendRef.value)

  const titleBase = bym ? '每日 Token 对比 (按模型)' : '每日 Token 趋势'
  let series
  if (bym) {
    series = Object.entries(bym).map(([m, ser]) => {
      const map = new Map(ser.map(d => [d.date, d.tokens ?? null]))
      return { name: m, type: 'line', smooth: true, connectNulls: false,
               data: dates.map(d => map.has(d) ? map.get(d) : null) }
    })
  } else {
    const map = new Map(aggDaily.value.map(d => [d.date, d.tokens ?? 0]))
    series = [{
      type: 'line', data: dates.map(d => map.get(d) ?? 0), smooth: true,
      areaStyle: { opacity: 0.15 }, itemStyle: { color: '#52c41a' },
    }]
  }

  tokenTrendChart.setOption({
    title: { text: trendTitle(titleBase), left: 'center', textStyle: { fontSize: 14 } },
    legend: bym ? { top: 28, type: 'scroll' } : { show: false },
    tooltip: {
      trigger: 'axis',
      formatter: params => {
        if (bym) {
          const head = `${params[0].name}<br/>`
          return head + params
            .filter(p => p.value != null)
            .map(p => `${p.marker} ${p.seriesName}: ${fmtTokens(p.value)}`)
            .join('<br/>')
        }
        const p = params[0]
        return `${p.name}<br/>Tokens: ${fmtTokens(p.value)} (${p.value.toLocaleString()})`
      },
    },
    grid: { left: 70, right: 20, bottom: 30, top: bym ? 60 : 40 },
    xAxis: { type: 'category', data: dates },
    yAxis: { type: 'value', name: 'Tokens', axisLabel: { formatter: fmtTokens } },
    series,
  }, true)
}

function renderBar() {
  if (!barRef.value) return
  if (!barChart) barChart = echarts.init(barRef.value)

  // 动态高度：每个模型 36px，最低 360px
  const modelCount = Object.keys(props.textModels).length + Object.keys(props.imageModels).length
  const barHeight = Math.max(360, modelCount * 36 + 80)
  barRef.value.style.height = barHeight + 'px'
  barChart.resize()

  // 合并生文和生图模型
  const allModels = []

  for (const [name, m] of Object.entries(props.textModels)) {
    allModels.push({
      name,
      totalCost: m.total_cost ?? m.total_cost_cny ?? 0,
    })
  }

  for (const [name, m] of Object.entries(props.imageModels)) {
    allModels.push({
      name,
      totalCost: m.total_cost ?? m.cost ?? 0,
    })
  }

  if (!allModels.length) return

  allModels.sort((a, b) => b.totalCost - a.totalCost)

  const names = allModels.map(m => m.name)
  const costs = allModels.map(m => m.totalCost)

  barChart.setOption({
    title: { text: '模型费用分布', left: 'center', textStyle: { fontSize: 14 } },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: params => {
        const p = params[0]
        return `${p.name}<br/>费用: ¥${p.value.toFixed(4)}`
      },
    },
    grid: { left: 200, right: 20, bottom: 30, top: 40 },
    xAxis: { type: 'value', name: '费用(元)' },
    yAxis: { type: 'category', data: names, inverse: true, axisLabel: { width: 180, overflow: 'none' } },
    series: [{ name: '费用', type: 'bar', data: costs, color: '#1890ff' }],
  }, true)
}

function handleResize() {
  trendChart?.resize()
  tokenTrendChart?.resize()
  barChart?.resize()
}

watch(() => [aggDaily.value, props.textModels, props.imageModels, props.dailyByModel], () => {
  renderTrend()
  renderTokenTrend()
  renderBar()
}, { deep: true })

onMounted(() => {
  renderTrend()
  renderTokenTrend()
  renderBar()
  window.addEventListener('resize', handleResize)
})

onUnmounted(() => {
  window.removeEventListener('resize', handleResize)
  trendChart?.dispose()
  tokenTrendChart?.dispose()
  barChart?.dispose()
})
</script>

<template>
  <div class="granularity-bar">
    <span class="g-label">粒度:</span>
    <el-radio-group v-model="granularity" size="small">
      <el-radio-button label="auto">自动</el-radio-button>
      <el-radio-button label="day">日</el-radio-button>
      <el-radio-button label="week">周</el-radio-button>
      <el-radio-button label="month">月</el-radio-button>
    </el-radio-group>
    <span class="g-hint" v-if="granularity === 'auto'">(当前: {{ effectiveGranularity === 'day' ? '日' : effectiveGranularity === 'week' ? '周' : '月' }})</span>
  </div>
  <div class="chart-row">
    <el-card class="chart-card">
      <div ref="trendRef" class="chart"></div>
    </el-card>
    <el-card class="chart-card">
      <div ref="tokenTrendRef" class="chart"></div>
    </el-card>
  </div>
  <div class="chart-row chart-row-single">
    <el-card class="chart-card">
      <div ref="barRef" class="chart"></div>
    </el-card>
  </div>
</template>

<style scoped>
.granularity-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
.g-label {
  font-size: 13px;
  color: #555;
}
.g-hint {
  font-size: 12px;
  color: #999;
}
.chart-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-bottom: 16px;
}
.chart-row-single {
  grid-template-columns: 1fr;
}
.chart {
  min-height: 360px;
}
</style>
