<script setup>
defineOptions({ name: 'Export' })
import { ref, computed, onMounted, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Download } from '@element-plus/icons-vue'
import { sharedDateRange } from '../store/vendorCache.js'

const options = ref({ vendors: [], models: [], columns: [], granularities: [] })
const loading = ref(false)

// 日期: 进入页面时从全局拷一份 snapshot, 本地改不影响全局, 跟 VendorDetail 一致.
const dateRange = ref([...sharedDateRange.value])
const startDate = computed({
  get: () => dateRange.value[0],
  set: (v) => { dateRange.value = [v, dateRange.value[1] || v] },
})
const endDate = computed({
  get: () => dateRange.value[1],
  set: (v) => { dateRange.value = [dateRange.value[0] || v, v] },
})

const granularity = ref('day')
const dimension = ref('vendor_model')  // vendor_model | vendor | model
const selectedVendors = ref([])  // [] = 全选
const selectedModels = ref([])
const selectedColumns = ref([])

// 加载选项
onMounted(async () => {
  loading.value = true
  try {
    const r = await fetch('/api/export/options')
    options.value = await r.json()
    // 初始: vendor 全选, model 全选, column 走 default
    selectedVendors.value = options.value.vendors.map(v => v.id)
    selectedModels.value = options.value.models.map(m => m.name)
    selectedColumns.value = options.value.columns.filter(c => c.default).map(c => c.key)
  } catch (e) {
    ElMessage.error('加载选项失败: ' + e.message)
  } finally {
    loading.value = false
  }
})

// model 列表按 vendor 联动: 选了 vendor 子集 → model 下拉只显示这些 vendor 用过的
const visibleModels = computed(() => {
  if (selectedVendors.value.length === options.value.vendors.length) {
    return options.value.models
  }
  const vset = new Set(selectedVendors.value)
  return options.value.models.filter(m => m.vendor_ids.some(v => vset.has(v)))
})

// vendor 变了, model 自动同步: 不在新可见 model 列表的去掉
watch(visibleModels, (vm) => {
  const vmSet = new Set(vm.map(m => m.name))
  selectedModels.value = selectedModels.value.filter(m => vmSet.has(m))
})

function selectAllVendors() { selectedVendors.value = options.value.vendors.map(v => v.id) }
function clearAllVendors() { selectedVendors.value = [] }
function selectAllModels() { selectedModels.value = visibleModels.value.map(m => m.name) }
function clearAllModels() { selectedModels.value = [] }

const downloading = ref(false)

async function download() {
  if (!selectedColumns.value.length) {
    ElMessage.warning('至少选一列')
    return
  }
  if (!selectedVendors.value.length) {
    ElMessage.warning('至少选一个供应商')
    return
  }
  downloading.value = true
  try {
    const params = new URLSearchParams({
      start: startDate.value,
      end: endDate.value,
      granularity: granularity.value,
      dimension: dimension.value,
      columns: selectedColumns.value.join(','),
    })
    // vendor 全选 → 不传, 让后端跑全部 (URL 短)
    if (selectedVendors.value.length < options.value.vendors.length) {
      params.set('vendor_ids', selectedVendors.value.join(','))
    }
    if (selectedModels.value.length < visibleModels.value.length) {
      params.set('models', selectedModels.value.join(','))
    }
    // 浏览器流式下载 — 直接 location 或 anchor click 让浏览器接管
    const a = document.createElement('a')
    a.href = `/api/export?${params}`
    a.download = ''  // server side 也设了 Content-Disposition, 这里随便
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    ElMessage.success('开始下载')
  } catch (e) {
    ElMessage.error('下载失败: ' + e.message)
  } finally {
    downloading.value = false
  }
}

// 预估行数: 调后端 /api/export/count, 跑同样的 SQL 数行 — 跟实际导出 100% 一致.
// debounce 400ms 避免快速勾选连环打.
const estimatedRows = ref(0)
const estimating = ref(false)
let countTimer = null

function refreshCount() {
  clearTimeout(countTimer)
  if (!startDate.value || !endDate.value || !selectedVendors.value.length) {
    estimatedRows.value = 0
    return
  }
  countTimer = setTimeout(async () => {
    estimating.value = true
    try {
      const params = new URLSearchParams({
        start: startDate.value, end: endDate.value, granularity: granularity.value,
        dimension: dimension.value,
      })
      if (selectedVendors.value.length < options.value.vendors.length) {
        params.set('vendor_ids', selectedVendors.value.join(','))
      }
      if (selectedModels.value.length < visibleModels.value.length) {
        params.set('models', selectedModels.value.join(','))
      }
      const r = await fetch(`/api/export/count?${params}`)
      const j = await r.json()
      estimatedRows.value = j.count || 0
    } catch {
      estimatedRows.value = 0
    } finally {
      estimating.value = false
    }
  }, 400)
}

watch([startDate, endDate, granularity, dimension, selectedVendors, selectedModels],
      refreshCount, { deep: true })

// 切聚合维度时, 自动联动调整列勾选 (vendor 维度无 model 列, model 维度无 vendor/native 列)
watch(dimension, (d) => {
  if (d === 'vendor') {
    selectedColumns.value = selectedColumns.value.filter(c => c !== 'model')
    // 加回 vendor_name 如果之前在 model 维度被剔了
    if (!selectedColumns.value.includes('vendor_name')) {
      selectedColumns.value = [...selectedColumns.value, 'vendor_name']
    }
  } else if (d === 'model') {
    // model 维度 — vendor 列没意义, native 跨币种不能合
    selectedColumns.value = selectedColumns.value.filter(
      c => !['vendor_name', 'cost_native', 'native_currency'].includes(c)
    )
    if (!selectedColumns.value.includes('model')) {
      selectedColumns.value = [...selectedColumns.value, 'model']
    }
  } else {  // vendor_model
    if (!selectedColumns.value.includes('model')) {
      selectedColumns.value = [...selectedColumns.value, 'model']
    }
    if (!selectedColumns.value.includes('vendor_name')) {
      selectedColumns.value = [...selectedColumns.value, 'vendor_name']
    }
  }
})
</script>

<template>
  <div class="export-page" v-loading="loading">
    <h2>导出</h2>
    <p class="hint">从落库的明细数据生成 CSV. 颗粒度 / 供应商 / 模型 / 列都可选.</p>

    <!-- 1. 日期 + 颗粒度 + 聚合粒度 -->
    <el-card class="section">
      <template #header><b>1. 时间范围 + 颗粒度</b></template>
      <div class="row">
        <el-date-picker v-model="startDate" type="date" placeholder="开始日期"
                        value-format="YYYY-MM-DD" style="width: 150px" />
        <span class="sep">至</span>
        <el-date-picker v-model="endDate" type="date" placeholder="结束日期"
                        value-format="YYYY-MM-DD" style="width: 150px" />
        <el-radio-group v-model="granularity" style="margin-left: 24px">
          <el-radio-button v-for="g in options.granularities" :key="g.key" :value="g.key">
            {{ g.label }}
          </el-radio-button>
        </el-radio-group>
      </div>
      <div class="row" style="margin-top: 12px">
        <span class="sep">聚合方式</span>
        <el-radio-group v-model="dimension">
          <el-radio-button value="vendor_model">按供应商 + 模型</el-radio-button>
          <el-radio-button value="vendor">按供应商汇总 (合并所有模型)</el-radio-button>
          <el-radio-button value="model">按模型汇总 (合并所有供应商)</el-radio-button>
        </el-radio-group>
      </div>
    </el-card>

    <!-- 2. 供应商 -->
    <el-card class="section">
      <template #header>
        <b>2. 供应商</b>
        <span style="margin-left: 16px; font-weight: normal; color: #888">
          已选 {{ selectedVendors.length }} / {{ options.vendors.length }}
        </span>
        <el-button link size="small" @click="selectAllVendors" style="margin-left: 8px">全选</el-button>
        <el-button link size="small" @click="clearAllVendors">反选</el-button>
      </template>
      <el-checkbox-group v-model="selectedVendors">
        <el-checkbox v-for="v in options.vendors" :key="v.id" :value="v.id">
          {{ v.name }}
          <span v-if="!v.enabled" style="color: #999; font-size: 12px">(disabled)</span>
        </el-checkbox>
      </el-checkbox-group>
    </el-card>

    <!-- 3. 模型 (按 vendor 联动) -->
    <el-card class="section">
      <template #header>
        <b>3. 模型</b>
        <span style="margin-left: 16px; font-weight: normal; color: #888">
          已选 {{ selectedModels.length }} / {{ visibleModels.length }}
          (按上面供应商联动)
          <span v-if="dimension === 'vendor'" style="color: #e6a23c; margin-left: 8px">
            按供应商汇总模式: 此处仅作筛选, 结果不会按模型分行
          </span>
          <span v-else-if="dimension === 'model'" style="color: #e6a23c; margin-left: 8px">
            按模型汇总模式: 同名 model 跨供应商合并, 原币列已自动剔除 (USD/CNY 不能直加)
          </span>
        </span>
        <el-button link size="small" @click="selectAllModels" style="margin-left: 8px">全选</el-button>
        <el-button link size="small" @click="clearAllModels">反选</el-button>
      </template>
      <el-checkbox-group v-model="selectedModels" class="models-grid">
        <el-checkbox v-for="m in visibleModels" :key="m.name" :value="m.name">
          {{ m.name }}
        </el-checkbox>
      </el-checkbox-group>
    </el-card>

    <!-- 4. 列 -->
    <el-card class="section">
      <template #header><b>4. 导出列</b></template>
      <el-checkbox-group v-model="selectedColumns">
        <el-checkbox v-for="c in options.columns" :key="c.key" :value="c.key">
          {{ c.label }}
          <span v-if="c.default" style="color: #1890ff; font-size: 12px; margin-left: 4px">(默认)</span>
        </el-checkbox>
      </el-checkbox-group>
    </el-card>

    <!-- 5. 下载 -->
    <div class="actions">
      <span class="estimate" v-if="estimating">计算行数中...</span>
      <span class="estimate" v-else-if="estimatedRows">预计 {{ estimatedRows.toLocaleString() }} 行</span>
      <el-button type="primary" size="large" @click="download"
                 :loading="downloading" :icon="Download">
        下载 CSV
      </el-button>
    </div>
  </div>
</template>

<style scoped>
.export-page {
  padding: 0 24px 24px;
}
h2 { margin: 0 0 6px; font-size: 20px; color: #1a1a1a; }
.hint { color: #888; font-size: 13px; margin-bottom: 16px; }
.section { margin-bottom: 16px; }
.row {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
}
.sep { color: #888; font-size: 13px; }
.models-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 4px;
}
.models-grid :deep(.el-checkbox) {
  margin-right: 0;
}
.actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 16px;
  padding: 16px 0;
}
.estimate { color: #888; font-size: 13px; }
</style>
