<script setup>
import { computed } from 'vue'

const props = defineProps({
  models: { type: Object, default: () => ({}) },
})

const tableData = computed(() => {
  return Object.entries(props.models)
    .map(([name, m]) => ({
      name,
      count: m.total_count,
      input_tokens: m.input.tokens,
      input_cost: m.input.cost,
      cache_tokens: m.cacheinput.tokens,
      cache_cost: m.cacheinput.cost,
      output_tokens: m.output.tokens,
      output_cost: m.output.cost,
      total_cost: m.total_cost,
    }))
    .sort((a, b) => b.total_cost - a.total_cost)
})

const totalRow = computed(() => {
  const t = tableData.value.reduce(
    (acc, r) => {
      acc.count += r.count
      acc.input_tokens += r.input_tokens
      acc.input_cost += r.input_cost
      acc.cache_tokens += r.cache_tokens
      acc.cache_cost += r.cache_cost
      acc.output_tokens += r.output_tokens
      acc.output_cost += r.output_cost
      acc.total_cost += r.total_cost
      return acc
    },
    { count: 0, input_tokens: 0, input_cost: 0, cache_tokens: 0, cache_cost: 0, output_tokens: 0, output_cost: 0, total_cost: 0 }
  )
  return t
})

function fmtTokens(n) {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K'
  return n.toString()
}

function fmtCost(n) {
  return '¥' + n.toFixed(4)
}

function getSummaries({ columns }) {
  const sums = []
  columns.forEach((col, i) => {
    if (i === 0) { sums[i] = '合计'; return }
    const key = col.property
    if (key && totalRow.value[key] !== undefined) {
      const v = totalRow.value[key]
      if (key.endsWith('_cost') || key === 'total_cost') sums[i] = fmtCost(v)
      else if (key.endsWith('_tokens')) sums[i] = fmtTokens(v)
      else sums[i] = v.toLocaleString()
    } else {
      sums[i] = ''
    }
  })
  return sums
}
</script>

<template>
  <el-table :data="tableData" stripe show-summary :summary-method="getSummaries" style="width: 100%">
    <el-table-column prop="name" label="模型" min-width="180" fixed />
    <el-table-column prop="count" label="调用次数" width="100" align="right">
      <template #default="{ row }">{{ row.count.toLocaleString() }}</template>
    </el-table-column>
    <el-table-column label="输入" align="center">
      <el-table-column prop="input_tokens" label="Tokens" width="110" align="right">
        <template #default="{ row }">{{ fmtTokens(row.input_tokens) }}</template>
      </el-table-column>
      <el-table-column prop="input_cost" label="费用" width="110" align="right">
        <template #default="{ row }">{{ fmtCost(row.input_cost) }}</template>
      </el-table-column>
    </el-table-column>
    <el-table-column label="缓存输入" align="center">
      <el-table-column prop="cache_tokens" label="Tokens" width="110" align="right">
        <template #default="{ row }">{{ fmtTokens(row.cache_tokens) }}</template>
      </el-table-column>
      <el-table-column prop="cache_cost" label="费用" width="110" align="right">
        <template #default="{ row }">{{ fmtCost(row.cache_cost) }}</template>
      </el-table-column>
    </el-table-column>
    <el-table-column label="输出" align="center">
      <el-table-column prop="output_tokens" label="Tokens" width="110" align="right">
        <template #default="{ row }">{{ fmtTokens(row.output_tokens) }}</template>
      </el-table-column>
      <el-table-column prop="output_cost" label="费用" width="110" align="right">
        <template #default="{ row }">{{ fmtCost(row.output_cost) }}</template>
      </el-table-column>
    </el-table-column>
    <el-table-column prop="total_cost" label="总费用" width="130" align="right" fixed="right" sortable>
      <template #default="{ row }">
        <span style="font-weight: 600; color: #f5222d">{{ fmtCost(row.total_cost) }}</span>
      </template>
    </el-table-column>
  </el-table>
</template>
