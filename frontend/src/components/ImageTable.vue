<script setup>
import { computed } from 'vue'

const props = defineProps({
  models: { type: Object, default: () => ({}) },
})

const tableData = computed(() => {
  return Object.entries(props.models)
    .map(([name, m]) => ({
      name,
      count: m.count,
      total_count: m.total_count,
      unit_price: m.unit_price,
      total_cost: m.total_cost,
    }))
    .sort((a, b) => b.total_cost - a.total_cost)
})

function fmtCost(n) {
  return '¥' + n.toFixed(4)
}

function getSummaries({ columns }) {
  const sums = []
  const totals = tableData.value.reduce(
    (acc, r) => { acc.count += r.count; acc.total_count += r.total_count; acc.total_cost += r.total_cost; return acc },
    { count: 0, total_count: 0, total_cost: 0 }
  )
  columns.forEach((col, i) => {
    if (i === 0) { sums[i] = '合计'; return }
    if (col.property === 'count') sums[i] = totals.count.toLocaleString()
    else if (col.property === 'total_count') sums[i] = totals.total_count.toLocaleString()
    else if (col.property === 'total_cost') sums[i] = fmtCost(totals.total_cost)
    else sums[i] = ''
  })
  return sums
}
</script>

<template>
  <el-table :data="tableData" stripe show-summary :summary-method="getSummaries" style="width: 100%">
    <el-table-column prop="name" label="模型" min-width="200" />
    <el-table-column prop="total_count" label="调用次数" width="120" align="right">
      <template #default="{ row }">{{ row.total_count.toLocaleString() }}</template>
    </el-table-column>
    <el-table-column prop="count" label="生成张数" width="120" align="right">
      <template #default="{ row }">{{ row.count.toLocaleString() }}</template>
    </el-table-column>
    <el-table-column prop="unit_price" label="单价 (元/张)" width="140" align="right">
      <template #default="{ row }">{{ row.unit_price.toFixed(3) }}</template>
    </el-table-column>
    <el-table-column prop="total_cost" label="总费用" width="140" align="right" sortable>
      <template #default="{ row }">
        <span style="font-weight: 600; color: #f5222d">{{ fmtCost(row.total_cost) }}</span>
      </template>
    </el-table-column>
  </el-table>
</template>
