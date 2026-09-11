<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'

const props = defineProps({
  vendorId: { type: String, required: true },
  dateRange: { type: Array, required: true },
  currency: { type: String, default: 'USD' }, // 供应商原生币种
  apiKey: { type: String, default: null },     // 可选, 按 API key 过滤
})

// 汇总抛给父组件 (VendorDetail 顶部卡片要用同一份数字, 避免两处各算一遍)
const emit = defineEmits(['loaded'])

const loading = ref(false)
const data = ref(null)
const error = ref('')

async function load() {
  if (!props.dateRange || props.dateRange.length !== 2) {
    ElMessage.warning('请先选择日期范围')
    return
  }

  loading.value = true
  error.value = ''
  try {
    const [start, end] = props.dateRange
    const params = new URLSearchParams({ start, end })
    if (props.apiKey) params.set('api_key', props.apiKey)
    const resp = await fetch(
      `/api/vendors/${props.vendorId}/billing-reconciliation?${params}`
    )
    if (!resp.ok) {
      const errData = await resp.json()
      throw new Error(errData.detail || resp.statusText)
    }
    data.value = await resp.json()
    emit('loaded', data.value.summary)
  } catch (e) {
    error.value = e.message
    data.value = null
    emit('loaded', null)   // 失败就把顶部卡片清掉, 别留上一次的旧数字
    ElMessage.error('加载模型费用核算失败: ' + e.message)
  } finally {
    loading.value = false
  }
}

const rate = () => data.value?.usd_to_cny ?? 0

function fmtTokens(n) {
  if (n == null) return '-'
  if (n >= 1_000_000_000) return (n / 1_000_000_000).toFixed(2) + 'B'
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K'
  return n.toString()
}

// null = 上游/定价拿不到 → '-'; 0 = 真的 0 元 → 照常显示 0
function fmtUsd(n) {
  return n == null ? '-' : '$' + n.toFixed(4)
}
function fmtCny(n) {
  return n == null ? '-' : '¥' + n.toFixed(4)
}
function fmtPriceUsd(n) {
  return n == null ? '-' : '$' + n.toFixed(4)
}
function fmtPriceCny(n) {
  return n == null ? '-' : '¥' + (n * rate()).toFixed(4)
}
function fmtDevUsd(n) {
  if (n == null) return '-'
  return (n >= 0 ? '+$' : '-$') + Math.abs(n).toFixed(4)
}
function fmtDevCny(n) {
  if (n == null) return '-'
  return (n >= 0 ? '+¥' : '-¥') + Math.abs(n).toFixed(4)
}

function fmtDiscount(n) {
  if (n == null || n === 1) return '-'
  return (n * 100).toFixed(1) + '%'
}

// null = 上游没有调用次数字段 → '-'; 否则千分位
function fmtCount(n) {
  return n == null ? '-' : n.toLocaleString()
}

// 平均调用价格 = 应付(抓取) / 调用次数. 调用次数缺失或为 0 → 算不出, 返回 null (前端 '-')
function avgCallPrice(cost, calls) {
  if (cost == null || calls == null || calls === 0) return null
  return cost / calls
}
function fmtAvgCallUsd(n) {
  return n == null ? '-' : '$' + n.toFixed(6)
}
function fmtAvgCallCny(n) {
  return n == null ? '-' : '¥' + n.toFixed(6)
}

// 偏差分级: 基于百分比而非绝对值
function deviationClass(calculated, fetched) {
  if (calculated == null || fetched == null || calculated === 0) return ''
  const deviation = fetched - calculated
  const percent = Math.abs(deviation / calculated)
  if (percent <= 0.1) return 'deviation-ok'      // ≤10%: 绿色
  if (percent <= 0.2) return 'deviation-warn'    // 10%~20%: 黄色
  return 'deviation-error'                       // >20%: 红色
}

defineExpose({ load })
</script>

<template>
  <div class="billing-reconciliation" v-loading="loading">
    <el-alert v-if="error" :title="error" type="error" show-icon closable style="margin-bottom: 12px" />

    <div v-if="!data && !loading && !error" class="empty-state">
      <p>点击上方「查询」加载模型费用核算数据</p>
    </div>

    <template v-if="data">
      <div class="rate-note">
        汇率 1 USD = {{ data.usd_to_cny.toFixed(4) }} CNY
      </div>

      <el-table :data="data.models" stripe style="width: 100%">
        <el-table-column prop="model" label="模型" min-width="180" fixed />

        <el-table-column label="调用次数" width="112" align="right">
          <template #default="{ row }">
            <div class="number-value">{{ fmtCount(row.request_count) }}</div>
          </template>
        </el-table-column>

        <el-table-column label="输入 Tokens" width="128" align="right">
          <template #default="{ row }">
            <div class="number-value">{{ fmtTokens(row.prompt_tokens) }}</div>
            <div class="price-hint" :class="{ 'native': currency === 'USD' }">{{ fmtPriceUsd(row.pricing.input_per_1m) }}/1M</div>
            <div class="price-hint" :class="{ 'native': currency === 'CNY' }">{{ fmtPriceCny(row.pricing.input_per_1m) }}/1M</div>
          </template>
        </el-table-column>

        <el-table-column label="输出 Tokens" width="128" align="right">
          <template #default="{ row }">
            <div class="number-value">{{ fmtTokens(row.completion_tokens) }}</div>
            <div class="price-hint" :class="{ 'native': currency === 'USD' }">{{ fmtPriceUsd(row.pricing.output_per_1m) }}/1M</div>
            <div class="price-hint" :class="{ 'native': currency === 'CNY' }">{{ fmtPriceCny(row.pricing.output_per_1m) }}/1M</div>
          </template>
        </el-table-column>

        <el-table-column label="缓存命中" width="128" align="right">
          <template #default="{ row }">
            <div class="number-value">{{ fmtTokens(row.cache_read_tokens) }}</div>
            <div class="price-hint" :class="{ 'native': currency === 'USD' }">{{ fmtPriceUsd(row.pricing.cache_read_per_1m) }}/1M</div>
            <div class="price-hint" :class="{ 'native': currency === 'CNY' }">{{ fmtPriceCny(row.pricing.cache_read_per_1m) }}/1M</div>
          </template>
        </el-table-column>

        <el-table-column label="缓存写入" width="128" align="right">
          <template #default="{ row }">
            <div class="number-value">{{ fmtTokens(row.cache_write_tokens) }}</div>
            <div class="price-hint" :class="{ 'native': currency === 'USD' }">{{ fmtPriceUsd(row.pricing.cache_write_per_1m) }}/1M</div>
            <div class="price-hint" :class="{ 'native': currency === 'CNY' }">{{ fmtPriceCny(row.pricing.cache_write_per_1m) }}/1M</div>
          </template>
        </el-table-column>

        <el-table-column label="总 Tokens" width="110" align="right">
          <template #default="{ row }">
            <div class="number-value">{{ fmtTokens(row.total_tokens) }}</div>
          </template>
        </el-table-column>

        <el-table-column label="折扣" width="76" align="right">
          <template #default="{ row }">
            <el-tooltip :content="`来源: ${row.discount_source}`" placement="top">
              <div class="number-value">{{ fmtDiscount(row.discount) }}</div>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column label="应付(计算)" width="132" align="right">
          <template #default="{ row }">
            <div class="money" :class="{ 'native': currency === 'USD' }">{{ fmtUsd(row.calculated.payable) }}</div>
            <div class="money" :class="{ 'native': currency === 'CNY' }">{{ fmtCny(row.calculated.payable_cny) }}</div>
          </template>
        </el-table-column>

        <el-table-column label="实付(计算)" width="132" align="right">
          <template #default="{ row }">
            <div class="money" :class="{ 'native': currency === 'USD' }">{{ fmtUsd(row.calculated.actual_pay) }}</div>
            <div class="money" :class="{ 'native': currency === 'CNY' }">{{ fmtCny(row.calculated.actual_pay_cny) }}</div>
          </template>
        </el-table-column>

        <el-table-column label="应付(抓取)" width="132" align="right">
          <template #default="{ row }">
            <div class="money" :class="{ 'native': currency === 'USD' }">{{ fmtUsd(row.fetched.payable) }}</div>
            <div class="money" :class="{ 'native': currency === 'CNY' }">{{ fmtCny(row.fetched.payable_cny) }}</div>
          </template>
        </el-table-column>

        <el-table-column label="实付(账单)" width="132" align="right">
          <template #default="{ row }">
            <div class="money" :class="{ 'native': currency === 'USD' }">{{ fmtUsd(row.fetched.actual_pay) }}</div>
            <div class="money" :class="{ 'native': currency === 'CNY' }">{{ fmtCny(row.fetched.actual_pay_cny) }}</div>
          </template>
        </el-table-column>

        <el-table-column label="应付偏差" width="132" align="right">
          <template #default="{ row }">
            <div class="money" :class="[deviationClass(row.calculated.payable, row.fetched.payable), { 'native': currency === 'USD' }]">
              {{ fmtDevUsd(row.deviation.payable) }}
            </div>
            <div class="money" :class="[deviationClass(row.calculated.payable_cny, row.fetched.payable_cny), { 'native': currency === 'CNY' }]">
              {{ fmtDevCny(row.deviation.payable_cny) }}
            </div>
          </template>
        </el-table-column>

        <el-table-column label="实付偏差" width="132" align="right">
          <template #default="{ row }">
            <div class="money" :class="[deviationClass(row.calculated.actual_pay, row.fetched.actual_pay), { 'native': currency === 'USD' }]">
              {{ fmtDevUsd(row.deviation.actual_pay) }}
            </div>
            <div class="money" :class="[deviationClass(row.calculated.actual_pay_cny, row.fetched.actual_pay_cny), { 'native': currency === 'CNY' }]">
              {{ fmtDevCny(row.deviation.actual_pay_cny) }}
            </div>
          </template>
        </el-table-column>

        <el-table-column label="平均调用价格" width="140" align="right">
          <template #default="{ row }">
            <div class="money" :class="{ 'native': currency === 'USD' }">{{ fmtAvgCallUsd(avgCallPrice(row.fetched.payable, row.request_count)) }}</div>
            <div class="money" :class="{ 'native': currency === 'CNY' }">{{ fmtAvgCallCny(avgCallPrice(row.fetched.payable_cny, row.request_count)) }}</div>
          </template>
        </el-table-column>
      </el-table>

      <el-alert type="info" :closable="false" style="margin-top: 14px">
        <template #title>
          <div class="notes">
            <ul>
              <li>单价来源: OpenRouter 官方定价 (从 LiteLLM upstream pricing 自动同步)</li>
              <li>应付(计算) = (输入 − 缓存命中 − 缓存写入) × 输入价 + 缓存命中 × 命中价 + 缓存写入 × 写入价 + 输出 × 输出价</li>
              <li>实付(计算) = 应付(计算) × 折扣 (折扣来自「供应商折扣配置」页)</li>
              <li>应付(抓取) = 供应商系统算出的扣费金额 (从 DB cost 字段获取)</li>
              <li>实付(账单) = 供应商出具的账单实付金额 (无接口可抓，恒显示 -)</li>
              <li>应付偏差 = 应付(抓取) − 应付(计算); 实付偏差 = 实付(账单) − 实付(计算) (账单无数据，显示 -)</li>
              <li>偏差颜色: <span class="deviation-ok">绿色 (≤10%)</span>, <span class="deviation-warn">黄色 (10%~20%)</span>, <span class="deviation-error">红色 (>20%)</span>; 显示 <code>-</code> 表示该项无数据</li>
              <li>平均调用价格 = 应付(抓取) ÷ 调用次数; 调用次数无数据或为 0 时显示 <code>-</code></li>
            </ul>
          </div>
        </template>
      </el-alert>
    </template>
  </div>
</template>

<style scoped>
.empty-state {
  text-align: center;
  padding: 40px 20px;
  color: #909399;
}

.rate-note {
  font-size: 12px;
  color: #909399;
  margin-bottom: 10px;
}

/* 数字列: 等宽字体, 与金额列统一样式 */
.number-value {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px;
  font-weight: 600;
  line-height: 1.5;
  color: #303133;
}

/* 单元格顶对齐 — token 列 3 行 / 金额列 2 行, 默认垂直居中会让首行错位,
   顶对齐后 119.34M 与「应付(计算)」的首行数字在同一基线 */
.billing-reconciliation :deep(.el-table td.el-table__cell) {
  vertical-align: top;
}

/* 金额: 等宽字体让上下两行小数点对齐 */
.money {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px;
  font-weight: 500;
  line-height: 1.5;
  color: #303133;
  opacity: 0.62;
}
/* 原生币种行加重显示 */
.money.native {
  font-weight: 600;
  opacity: 1;
}
/* 移除 payable/actual 的颜色，全部用黑色 */

.price-hint {
  font-size: 11px;
  color: #d0d3d8;
  line-height: 1.45;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-weight: 400;
}
/* 原生币种价格提示加重显示 */
.price-hint.native {
  color: #909399;
  font-weight: 500;
}

.deviation-ok {
  color: #52c41a;
}
.deviation-warn {
  color: #faad14;
}
.deviation-error {
  color: #f5222d;
}

.notes {
  font-size: 12px;
  line-height: 1.7;
}
.notes ul {
  margin: 2px 0;
  padding-left: 18px;
}
.notes code {
  background: #f0f0f0;
  padding: 0 4px;
  border-radius: 3px;
}
</style>
