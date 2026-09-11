<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowDown, ArrowUp, Plus, Delete, Refresh } from '@element-plus/icons-vue'
import {
  fetchWangsuModels,
  addWangsuModel,
  deleteWangsuModel,
  replaceWangsuModels,
} from '../api/index.js'

const emit = defineEmits(['applied'])

const models = ref([])     // 后端权威状态: [{code, enabled}]
const draft = ref({})      // 本地草稿: code -> bool, 修改未提交
const expanded = ref(false)
const filter = ref('')
const newCode = ref('')
const loading = ref(false)
const saving = ref(false)

const enabledCount = computed(() =>
  models.value.reduce((s, m) => s + (codeChecked(m.code) ? 1 : 0), 0)
)
const dirty = computed(() =>
  models.value.some(m => codeChecked(m.code) !== m.enabled)
)
const sorted = computed(() => {
  const q = filter.value.trim().toLowerCase()
  const filtered = models.value.filter(m => !q || m.code.toLowerCase().includes(q))
  return [...filtered].sort((a, b) => {
    const ea = codeChecked(a.code), eb = codeChecked(b.code)
    if (ea !== eb) return ea ? -1 : 1
    return a.code.localeCompare(b.code)
  })
})

function codeChecked(code) {
  return draft.value[code] !== undefined ? draft.value[code] : (models.value.find(m => m.code === code)?.enabled ?? false)
}

function toggle(code) {
  draft.value[code] = !codeChecked(code)
}

function selectAll() {
  models.value.forEach(m => { draft.value[m.code] = true })
}

function clearAll() {
  models.value.forEach(m => { draft.value[m.code] = false })
}

function reset() {
  draft.value = {}
}

async function load() {
  loading.value = true
  try {
    const r = await fetchWangsuModels()
    models.value = r.models
    draft.value = {}
  } catch (e) {
    ElMessage.error('加载模型清单失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    loading.value = false
  }
}

async function applyAndQuery() {
  if (!dirty.value) {
    emit('applied')
    return
  }
  saving.value = true
  try {
    const next = models.value.map(m => ({ code: m.code, enabled: codeChecked(m.code) }))
    const r = await replaceWangsuModels(next)
    models.value = r.models
    draft.value = {}
    ElMessage.success(`已保存, 启用 ${r.enabled_count} / ${r.count}`)
    emit('applied')
  } catch (e) {
    ElMessage.error('保存失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    saving.value = false
  }
}

async function handleAdd() {
  const code = newCode.value.trim()
  if (!code) return
  try {
    const r = await addWangsuModel(code, true)
    models.value = r.models
    newCode.value = ''
    ElMessage.success(`已添加 ${code}`)
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || e.message)
  }
}

async function handleDelete(code) {
  try {
    await ElMessageBox.confirm(`从清单移除 "${code}" ?`, '确认', { type: 'warning' })
  } catch { return }
  try {
    const r = await deleteWangsuModel(code)
    models.value = r.models
    delete draft.value[code]
    ElMessage.success('已删除')
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || e.message)
  }
}

onMounted(load)
</script>

<template>
  <el-card class="picker" shadow="never">
    <div class="bar">
      <div class="bar-info">
        <span class="title">wangsu 模型选择</span>
        <span class="badge">已启用 {{ enabledCount }} / 共 {{ models.length }}</span>
        <span v-if="dirty" class="dirty">● 未保存</span>
      </div>
      <div class="bar-actions">
        <el-button v-if="expanded" size="small" :icon="Refresh" @click="reset" :disabled="!dirty">重置</el-button>
        <el-button
          v-if="expanded"
          size="small"
          type="primary"
          :loading="saving"
          @click="applyAndQuery"
        >应用并查询</el-button>
        <el-button :icon="expanded ? ArrowUp : ArrowDown" size="small" text @click="expanded = !expanded">
          {{ expanded ? '收起' : '展开' }}
        </el-button>
      </div>
    </div>

    <div v-if="expanded" class="body" v-loading="loading">
      <div class="ops">
        <el-input
          v-model="filter"
          placeholder="筛选模型..."
          clearable
          size="small"
          style="width: 240px"
        />
        <el-button size="small" @click="selectAll">全选</el-button>
        <el-button size="small" @click="clearAll">全不选</el-button>
        <div class="spacer" />
        <el-input
          v-model="newCode"
          placeholder="添加新模型, 如 gpt-6"
          size="small"
          style="width: 220px"
          @keyup.enter="handleAdd"
        />
        <el-button size="small" :icon="Plus" @click="handleAdd">添加</el-button>
      </div>

      <div class="grid">
        <div
          v-for="m in sorted"
          :key="m.code"
          class="row"
          :class="{ on: codeChecked(m.code) }"
        >
          <el-checkbox
            :model-value="codeChecked(m.code)"
            @change="toggle(m.code)"
          >
            <code>{{ m.code }}</code>
          </el-checkbox>
          <el-button :icon="Delete" size="small" text type="danger" @click.stop="handleDelete(m.code)" />
        </div>
      </div>
    </div>
  </el-card>
</template>

<style scoped>
.picker {
  margin-bottom: 16px;
}
.bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
}
.bar-info {
  display: flex;
  align-items: center;
  gap: 12px;
}
.title {
  font-weight: 600;
  color: #1a1a1a;
}
.badge {
  background: #e6f7ff;
  color: #1890ff;
  padding: 2px 10px;
  border-radius: 10px;
  font-size: 12px;
}
.dirty {
  color: #f5222d;
  font-size: 12px;
}
.bar-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}
.body {
  margin-top: 16px;
  border-top: 1px solid #f0f0f0;
  padding-top: 16px;
}
.ops {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.spacer {
  flex: 1;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 4px 12px;
  max-height: 480px;
  overflow-y: auto;
  padding-right: 8px;
}
.row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 4px 8px;
  border-radius: 4px;
  border: 1px solid transparent;
}
.row:hover {
  background: #fafafa;
}
.row.on {
  background: #f6ffed;
  border-color: #d9f7be;
}
code {
  background: transparent;
  font-size: 13px;
  color: #333;
}
</style>
