<script setup>
defineOptions({ name: 'Analysis' })
import { ref, onMounted, computed } from 'vue'
import { ElMessage } from 'element-plus'
import { ArrowUp, ArrowDown, Warning, CircleCheck } from '@element-plus/icons-vue'

const loading = ref(false)
const budget = ref(null)
const wow = ref({ risers: [], fallers: [] })
const cache = ref({ items: [] })
const health = ref({ daily: [], missing: [] })
const share = ref({ items: [], curr_window: null, prev_window: null })
const pareto = ref({ items: [], other: null, total_cost_cny: 0 })
const seriesCache = ref({ series: [], top_n_basis: 15, basis_row_count: 0 })
const seriesTopN = ref(15)  // 用户可调 — 默认 15, 调大能看到 doubao/glm 等小系列

async function loadAll() {
  loading.value = true
  try {
    const [b, w, c, h, s, p, sc] = await Promise.all([
      fetch('/api/analysis/budget').then(r => r.json()),
      fetch('/api/analysis/wow-changes?window_days=7').then(r => r.json()),
      fetch('/api/analysis/cache-hit?days=30').then(r => r.json()),
      fetch('/api/analysis/data-health?days=14').then(r => r.json()),
      fetch('/api/analysis/vendor-share').then(r => r.json()),
      fetch('/api/analysis/model-pareto?days=30&top_n=15').then(r => r.json()),
      fetch(`/api/analysis/cache-by-series?days=30&top_n=${seriesTopN.value}`).then(r => r.json()),
    ])
    budget.value = b
    wow.value = w
    cache.value = c
    health.value = h
    share.value = s
    pareto.value = p
    seriesCache.value = sc
  } catch (e) {
    ElMessage.error('加载失败: ' + e.message)
  } finally {
    loading.value = false
  }
}
onMounted(loadAll)

// 单独刷新 series cache — 调 top_n 后不用整页 reload
async function reloadSeriesCache() {
  try {
    const r = await fetch(`/api/analysis/cache-by-series?days=30&top_n=${seriesTopN.value}`).then(x => x.json())
    seriesCache.value = r
  } catch (e) {
    ElMessage.error('系列对比加载失败: ' + e.message)
  }
}

function fmt(n) {
  if (n == null) return '—'
  return Number(n).toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}
function fmtK(n) {  // 大数缩写
  if (n == null) return '—'
  if (n >= 1e8) return (n / 1e8).toFixed(2) + '亿'
  if (n >= 1e4) return (n / 1e4).toFixed(2) + '万'
  return Number(n).toLocaleString()
}
function pctClass(p) {
  if (p == null) return ''
  return p > 0 ? 'pct-up' : 'pct-down'
}

// 预算进度条颜色: 投影 > 上月总额 = 红, 跟上月差不多 = 黄, 低于上月 = 绿
const budgetBarColor = computed(() => {
  if (!budget.value || !budget.value.projected_vs_last_total_pct) return '#67c23a'
  const p = budget.value.projected_vs_last_total_pct
  if (p > 20) return '#f56c6c'
  if (p > 5) return '#e6a23c'
  return '#67c23a'
})
const budgetProgressPct = computed(() => {
  if (!budget.value) return 0
  return Math.round((budget.value.days_elapsed / budget.value.days_in_month) * 100)
})
</script>

<template>
  <div class="analysis-page" v-loading="loading">
    <h2>横向分析</h2>
    <p class="hint">自动巡检 + 业务洞察, 跟 vendor/价格选择无关.</p>

    <!-- 1. 月预算 -->
    <el-card class="section" v-if="budget">
      <template #header><b>1. 本月预算进度 + 末月预测</b></template>
      <div class="budget-row">
        <div class="stat-block">
          <div class="stat-label">本月已花 (1 日 ~ 昨天)</div>
          <div class="stat-val big">¥ {{ fmt(budget.this_month_to_date_cny) }}</div>
          <div class="stat-sub" :class="pctClass(budget.wow_pct_vs_last_same_period)">
            vs 上月同期 ({{ fmt(budget.last_month_same_period_cny) }})
            <span v-if="budget.wow_pct_vs_last_same_period != null">
              {{ budget.wow_pct_vs_last_same_period > 0 ? '+' : '' }}{{ budget.wow_pct_vs_last_same_period }}%
            </span>
          </div>
        </div>
        <div class="stat-block">
          <div class="stat-label">
            末月预测
            <span v-if="budget.forecast_method" class="method-tag">({{ budget.forecast_method }})</span>
          </div>
          <div class="stat-val big" v-if="budget.projected_this_month_cny != null">
            ¥ {{ fmt(budget.projected_this_month_cny) }}
          </div>
          <div class="stat-val big stat-muted" v-else>
            数据不足
            <div class="stat-sub-tip">本月需累计 ≥ 7 天才给预测</div>
          </div>
          <div class="stat-sub" :class="pctClass(budget.projected_vs_last_total_pct)"
               v-if="budget.projected_this_month_cny != null">
            vs 上月全月 ({{ fmt(budget.last_month_total_cny) }})
            <span v-if="budget.projected_vs_last_total_pct != null">
              {{ budget.projected_vs_last_total_pct > 0 ? '+' : '' }}{{ budget.projected_vs_last_total_pct }}%
            </span>
          </div>
        </div>
        <div class="stat-block">
          <div class="stat-label">日均</div>
          <div class="stat-val">¥ {{ fmt(budget.daily_avg_cny) }}</div>
          <div class="stat-sub">已过 {{ budget.days_elapsed }} / {{ budget.days_in_month }} 天</div>
        </div>
      </div>
      <el-progress :percentage="budgetProgressPct" :color="budgetBarColor"
                   :stroke-width="14" style="margin-top: 16px" />
    </el-card>

    <!-- 2. 环比异常榜 -->
    <el-card class="section">
      <template #header>
        <b>2. 环比异常榜 — 近 7 天 vs 前 7 天</b>
        <span class="header-sub" v-if="wow.curr_window">
          {{ wow.curr_window.start }} ~ {{ wow.curr_window.end }}
          vs {{ wow.prev_window.start }} ~ {{ wow.prev_window.end }}
        </span>
      </template>
      <div class="wow-grid">
        <div class="wow-col">
          <div class="wow-title pct-up">
            <el-icon><ArrowUp /></el-icon> 涨幅 Top {{ wow.risers.length }}
          </div>
          <el-table :data="wow.risers" size="small" empty-text="无显著上涨">
            <el-table-column label="供应商" prop="vendor_name" width="100" />
            <el-table-column label="模型" prop="model" min-width="140">
              <template #default="{ row }">
                <span class="model-name">{{ row.model }}</span>
              </template>
            </el-table-column>
            <el-table-column label="本期 ¥" prop="curr_cost" width="100" align="right">
              <template #default="{ row }">¥{{ fmt(row.curr_cost) }}</template>
            </el-table-column>
            <el-table-column label="变化 ¥" prop="delta_cost" width="110" align="right">
              <template #default="{ row }">
                <span class="pct-up">+¥{{ fmt(row.delta_cost) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="%" prop="pct" width="80" align="right">
              <template #default="{ row }">
                <span v-if="row.pct == null" class="text-mute">新增</span>
                <span v-else class="pct-up">+{{ row.pct }}%</span>
              </template>
            </el-table-column>
          </el-table>
        </div>
        <div class="wow-col">
          <div class="wow-title pct-down">
            <el-icon><ArrowDown /></el-icon> 跌幅 Top {{ wow.fallers.length }}
          </div>
          <el-table :data="wow.fallers" size="small" empty-text="无显著下跌">
            <el-table-column label="供应商" prop="vendor_name" width="100" />
            <el-table-column label="模型" prop="model" min-width="140">
              <template #default="{ row }">
                <span class="model-name">{{ row.model }}</span>
              </template>
            </el-table-column>
            <el-table-column label="本期 ¥" prop="curr_cost" width="100" align="right">
              <template #default="{ row }">¥{{ fmt(row.curr_cost) }}</template>
            </el-table-column>
            <el-table-column label="变化 ¥" prop="delta_cost" width="110" align="right">
              <template #default="{ row }">
                <span class="pct-down">¥{{ fmt(row.delta_cost) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="%" prop="pct" width="80" align="right">
              <template #default="{ row }">
                <span v-if="row.pct == null" class="text-mute">消失</span>
                <span v-else class="pct-down">{{ row.pct }}%</span>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </div>
    </el-card>

    <!-- 3. cache 命中率排行 -->
    <el-card class="section">
      <template #header>
        <b>3. cache 命中率排行 — 近 30 天</b>
        <span class="header-sub">命中率低 + prompt 大 = 业务侧 prompt 应改造开 cache</span>
      </template>
      <el-table :data="cache.items" size="small" empty-text="无数据 (cache_read 字段全 NULL?)">
        <el-table-column label="供应商" prop="vendor_name" width="120" />
        <el-table-column label="模型" prop="model" min-width="180" />
        <el-table-column label="输入 Tokens (含 cache)" prop="input_tokens" width="180" align="right">
          <template #default="{ row }">{{ fmtK(row.input_tokens) }}</template>
        </el-table-column>
        <el-table-column label="Cache Read" prop="cache_read_tokens" width="140" align="right">
          <template #default="{ row }">{{ fmtK(row.cache_read_tokens) }}</template>
        </el-table-column>
        <el-table-column label="命中率" prop="hit_rate" width="180">
          <template #default="{ row }">
            <el-progress :percentage="row.hit_rate"
                         :color="row.hit_rate > 50 ? '#67c23a' : row.hit_rate > 20 ? '#e6a23c' : '#f56c6c'"
                         :stroke-width="10" :text-inside="true" />
          </template>
        </el-table-column>
        <el-table-column label="花费 ¥" prop="cost_cny" width="120" align="right">
          <template #default="{ row }">¥{{ fmt(row.cost_cny) }}</template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 4. Vendor 份额变化 -->
    <el-card class="section" v-if="share.curr_window">
      <template #header>
        <b>4. Vendor 份额变化 — 本月 vs 上月同期</b>
        <span class="header-sub">
          {{ share.curr_window.start }} ~ {{ share.curr_window.end }}
          (¥{{ fmt(share.curr_total) }})
          vs {{ share.prev_window.start }} ~ {{ share.prev_window.end }}
          (¥{{ fmt(share.prev_total) }})
        </span>
      </template>
      <el-table :data="share.items" size="small">
        <el-table-column label="供应商" prop="vendor_name" width="120" />
        <el-table-column label="本月成本" width="140" align="right">
          <template #default="{ row }">¥{{ fmt(row.curr_cost) }}</template>
        </el-table-column>
        <el-table-column label="本月份额" width="220">
          <template #default="{ row }">
            <el-progress :percentage="row.curr_pct" :color="'#409eff'"
                         :stroke-width="10" :text-inside="true" :format="() => row.curr_pct + '%'" />
          </template>
        </el-table-column>
        <el-table-column label="上月份额" width="220">
          <template #default="{ row }">
            <el-progress :percentage="row.prev_pct" :color="'#909399'"
                         :stroke-width="10" :text-inside="true" :format="() => row.prev_pct + '%'" />
          </template>
        </el-table-column>
        <el-table-column label="变化 (百分点)" width="140" align="right">
          <template #default="{ row }">
            <span :class="row.delta_pct_pt > 0 ? 'pct-up' : row.delta_pct_pt < 0 ? 'pct-down' : 'text-mute'">
              {{ row.delta_pct_pt > 0 ? '+' : '' }}{{ row.delta_pct_pt }} pt
            </span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 5. Model 集中度帕累托 -->
    <el-card class="section">
      <template #header>
        <b>5. Model 集中度帕累托 — 近 30 天</b>
        <span class="header-sub">
          Top {{ pareto.items.length }} 占总成本 <b>{{ pareto.top_n_share_pct }}%</b>
          (总 ¥{{ fmt(pareto.total_cost_cny) }})
        </span>
      </template>
      <el-table :data="pareto.items.concat(pareto.other ? [pareto.other] : [])" size="small">
        <el-table-column label="#" width="50" align="center">
          <template #default="{ $index }">{{ $index + 1 }}</template>
        </el-table-column>
        <el-table-column label="供应商" prop="vendor_name" width="100" />
        <el-table-column label="模型" prop="model" min-width="180" />
        <el-table-column label="成本 ¥" prop="cost_cny" width="140" align="right">
          <template #default="{ row }">¥{{ fmt(row.cost_cny) }}</template>
        </el-table-column>
        <el-table-column label="占比" width="180">
          <template #default="{ row }">
            <el-progress :percentage="row.share_pct"
                         :color="row.vendor_id ? '#409eff' : '#c0c4cc'"
                         :stroke-width="10" :text-inside="true"
                         :format="() => row.share_pct + '%'" />
          </template>
        </el-table-column>
        <el-table-column label="累计" width="100" align="right">
          <template #default="{ row }">
            <span :class="{ 'text-mute': !row.vendor_id }">{{ row.cumulative_pct }}%</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 6. 系列 cache 率对比 — top N (model, vendor) 按 series 聚合 -->
    <el-card class="section">
      <template #header>
        <b>6. 系列 cache 率对比 — Top {{ seriesCache.basis_row_count }} (model, 供应商) by cache 命中率, 按系展开全 vendor</b>
        <span class="header-sub">
          同系列 (claude/gpt/gemini/kimi 等) 在不同供应商的 cache 命中率 → 转流量到 cache 友好的供应商能省一大块.
          Top N 按 cache 命中率取最高的 (m, v), 决定哪些系入榜; 入榜后该系所有 vendor 都列出 (≥¥1 才显示).
        </span>
        <span class="series-topn-control">
          Top
          <el-input-number v-model="seriesTopN" :min="5" :max="100" :step="5" size="small"
                           @change="reloadSeriesCache" controls-position="right" style="width: 90px" />
        </span>
      </template>
      <div class="series-grid">
        <div v-for="s in seriesCache.series" :key="s.series" class="series-card">
          <div class="series-card-header">
            <span class="series-name">{{ s.series }} 系</span>
            <span class="series-meta">
              <el-tag :type="s.avg_hit_rate > 50 ? 'success' : s.avg_hit_rate > 20 ? 'warning' : 'danger'"
                      size="small" effect="light">整体 {{ s.avg_hit_rate }}%</el-tag>
              <span class="series-total">¥{{ fmt(s.total_cost_cny) }}</span>
            </span>
          </div>
          <div class="series-vendor-list">
            <div v-for="v in s.vendors" :key="v.vendor_id" class="series-vendor-row">
              <div class="series-vendor-name" :title="v.models.join(', ')">{{ v.vendor_name }}</div>
              <div class="series-vendor-bar">
                <el-progress :percentage="v.hit_rate"
                  :color="v.hit_rate > 50 ? '#67c23a' : v.hit_rate > 20 ? '#e6a23c' : '#f56c6c'"
                  :stroke-width="14" :text-inside="true"
                  :format="() => v.hit_rate + '%'" />
              </div>
              <div class="series-vendor-cost">¥{{ fmt(v.cost_cny) }}</div>
            </div>
          </div>
        </div>
        <div v-if="!seriesCache.series.length" class="series-empty">
          无 cache 数据 (cache_read 字段全 NULL?)
        </div>
      </div>
    </el-card>

    <!-- 7. 数据完整度 -->
    <el-card class="section">
      <template #header>
        <b>7. 数据完整度巡检 — 最近 14 天</b>
        <span class="header-sub">每天 enabled vendor 是否都成功入库</span>
      </template>
      <div class="health-grid">
        <div v-for="d in health.daily" :key="d.date" class="health-day"
             :class="{ ok: d.is_complete, bad: !d.is_complete }">
          <div class="health-date">{{ d.date.slice(5) }}</div>
          <div class="health-count">{{ d.complete }} / {{ d.expected }}</div>
          <el-icon v-if="d.is_complete" class="health-icon"><CircleCheck /></el-icon>
          <el-icon v-else class="health-icon"><Warning /></el-icon>
        </div>
      </div>
      <div v-if="health.missing.length" class="missing-box">
        <div class="missing-title">缺失明细 ({{ health.missing.length }} 条):</div>
        <div class="missing-list">
          <el-tag v-for="(m, i) in health.missing" :key="i" type="danger" size="small"
                  style="margin: 2px">
            {{ m.date.slice(5) }} {{ m.vendor_name }}
          </el-tag>
        </div>
      </div>
      <div v-else class="missing-box ok">
        最近 14 天数据齐全 ✓
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.analysis-page {
  padding: 0 24px 24px;
}
h2 { margin: 0 0 6px; font-size: 20px; color: #1a1a1a; }
.hint { color: #888; font-size: 13px; margin-bottom: 16px; }
.section { margin-bottom: 16px; }
.header-sub {
  margin-left: 12px; font-weight: normal; color: #888; font-size: 12px;
}

/* 预算 */
.budget-row {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 16px;
}
.stat-block {
  padding: 8px 0;
}
.stat-label { color: #666; font-size: 13px; margin-bottom: 4px; }
.stat-val { color: #1a1a1a; font-size: 20px; font-weight: 600; }
.stat-val.big { font-size: 26px; }
.stat-val.stat-muted { color: #999; font-size: 18px; font-weight: 500; }
.stat-sub { color: #888; font-size: 12px; margin-top: 4px; }
.stat-sub-tip { color: #aaa; font-size: 11px; font-weight: normal; margin-top: 4px; }
.method-tag { color: #999; font-size: 11px; font-weight: normal; margin-left: 4px; }

/* 环比 */
.wow-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.wow-title {
  font-weight: 600; font-size: 14px; margin-bottom: 8px;
  display: flex; align-items: center; gap: 4px;
}
.model-name { font-family: ui-monospace, monospace; font-size: 12px; }

/* 通用色 */
.pct-up { color: #f56c6c; }
.pct-down { color: #67c23a; }
.text-mute { color: #999; }

/* 数据完整度日历格子 */
.health-grid {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: 8px;
}
.health-day {
  position: relative;
  padding: 8px;
  border-radius: 4px;
  text-align: center;
  font-size: 12px;
}
.health-day.ok { background: #f0f9ff; border: 1px solid #b3d8ff; }
.health-day.bad { background: #fef0f0; border: 1px solid #fbc4c4; }
.health-date { color: #666; }
.health-count { font-weight: 600; margin-top: 2px; }
.health-icon {
  position: absolute; top: 4px; right: 4px; font-size: 12px;
}
.health-day.ok .health-icon { color: #67c23a; }
.health-day.bad .health-icon { color: #f56c6c; }

.missing-box {
  margin-top: 12px; padding: 10px; border-radius: 4px;
  background: #fef0f0; font-size: 13px;
}
.missing-box.ok {
  background: #f0f9ff; color: #1a7;
}
.missing-title { font-weight: 600; margin-bottom: 4px; }
.missing-list { display: flex; flex-wrap: wrap; }

/* 系列 cache 对比 (section 6) */
.series-topn-control {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #888;
  font-weight: normal;
}
.series-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
  gap: 16px;
}
.series-card {
  border: 1px solid #ebeef5;
  border-radius: 6px;
  padding: 12px 14px;
  background: #fafbfc;
}
.series-card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
  padding-bottom: 8px;
  border-bottom: 1px dashed #e4e7ed;
}
.series-name {
  font-weight: 600;
  font-size: 15px;
  color: #1a1a1a;
  text-transform: lowercase;
}
.series-meta {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.series-total {
  font-size: 13px;
  color: #666;
}
.series-vendor-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.series-vendor-row {
  display: grid;
  grid-template-columns: 90px 1fr 90px;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
.series-vendor-name {
  color: #333;
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  cursor: help;
}
.series-vendor-bar { min-width: 0; }
.series-vendor-cost {
  text-align: right;
  color: #888;
  font-size: 12px;
  font-family: ui-monospace, monospace;
}
.series-empty {
  grid-column: 1 / -1;
  text-align: center;
  color: #999;
  padding: 24px;
}
</style>
