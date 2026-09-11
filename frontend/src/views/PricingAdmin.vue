<script setup>
import { ref, computed, onMounted } from 'vue'
import { fetchPricing, updatePricing } from '../api/index.js'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Delete, Edit } from '@element-plus/icons-vue'

const pricing = ref(null)
const loading = ref(false)
const saving = ref(false)
const activeTab = ref('text')

const dialogVisible = ref(false)
const isEdit = ref(false)
const editingKey = ref('')
const form = ref({})

const isText = computed(() => activeTab.value === 'text')

async function loadPricing() {
  loading.value = true
  try {
    pricing.value = await fetchPricing()
  } catch (e) {
    ElMessage.error('加载定价失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    loading.value = false
  }
}

function getTextData() {
  if (!pricing.value?.text) return []
  return Object.entries(pricing.value.text).map(([key, v]) => ({
    spec_key: key,
    display_name: v.display_name,
    input: v.input ?? 0,
    cacheinput: v.cacheinput ?? 0,
    output: v.output ?? 0,
  }))
}

function getImageData() {
  if (!pricing.value?.image) return []
  return Object.entries(pricing.value.image).map(([key, v]) => ({
    spec_key: key,
    display_name: v.display_name,
    price: v.price ?? 0,
    unit: v.unit ?? '元/张',
  }))
}

function openAdd() {
  isEdit.value = false
  editingKey.value = ''
  if (isText.value) {
    form.value = { spec_key: '', display_name: '', input: 0, cacheinput: 0, output: 0 }
  } else {
    form.value = { spec_key: '', display_name: '', price: 0, unit: '元/张' }
  }
  dialogVisible.value = true
}

function openEdit(row) {
  isEdit.value = true
  editingKey.value = row.spec_key
  form.value = { ...row }
  dialogVisible.value = true
}

async function handleSave() {
  const type = activeTab.value
  if (!form.value.spec_key || !form.value.display_name) {
    ElMessage.warning('Specification Key 和模型名称不能为空')
    return
  }
  if (!isEdit.value && pricing.value[type]?.[form.value.spec_key]) {
    ElMessage.warning('该 Specification Key 已存在')
    return
  }

  saving.value = true
  try {
    if (!pricing.value[type]) pricing.value[type] = {}
    if (isEdit.value && editingKey.value !== form.value.spec_key) {
      delete pricing.value[type][editingKey.value]
    }

    if (isText.value) {
      pricing.value[type][form.value.spec_key] = {
        display_name: form.value.display_name,
        input: Number(form.value.input),
        cacheinput: Number(form.value.cacheinput),
        output: Number(form.value.output),
      }
    } else {
      pricing.value[type][form.value.spec_key] = {
        display_name: form.value.display_name,
        price: Number(form.value.price),
        unit: form.value.unit || '元/张',
      }
    }

    await updatePricing(pricing.value)
    ElMessage.success(isEdit.value ? '修改成功' : '添加成功')
    dialogVisible.value = false
  } catch (e) {
    ElMessage.error('保存失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    saving.value = false
  }
}

async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(
      `确定删除 "${row.display_name}" (${row.spec_key}) 的定价规则吗？`,
      '确认删除',
      { type: 'warning' }
    )
  } catch { return }

  const type = activeTab.value
  delete pricing.value[type][row.spec_key]
  saving.value = true
  try {
    await updatePricing(pricing.value)
    ElMessage.success('删除成功')
  } catch (e) {
    ElMessage.error('删除失败')
    await loadPricing()
  } finally {
    saving.value = false
  }
}

onMounted(loadPricing)
</script>

<template>
  <div class="pricing-admin">
    <div class="page-header">
      <h2>定价管理</h2>
      <el-button type="primary" :icon="Plus" @click="openAdd">添加模型</el-button>
    </div>

    <el-card>
      <el-tabs v-model="activeTab">
        <el-tab-pane label="生文 (Text)" name="text" />
        <el-tab-pane label="生图 (Image)" name="image" />
      </el-tabs>

      <!-- 生文表格 -->
      <el-table v-if="isText" :data="getTextData()" v-loading="loading" stripe style="width: 100%">
        <el-table-column prop="spec_key" label="Specification Key" width="180" />
        <el-table-column prop="display_name" label="模型名称" width="220" />
        <el-table-column prop="input" label="输入 (元/M token)" width="160" align="right">
          <template #default="{ row }">{{ row.input.toFixed(3) }}</template>
        </el-table-column>
        <el-table-column prop="cacheinput" label="缓存输入 (元/M token)" width="180" align="right">
          <template #default="{ row }">{{ row.cacheinput.toFixed(3) }}</template>
        </el-table-column>
        <el-table-column prop="output" label="输出 (元/M token)" width="160" align="right">
          <template #default="{ row }">{{ row.output.toFixed(3) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="160" align="center" fixed="right">
          <template #default="{ row }">
            <el-button :icon="Edit" size="small" @click="openEdit(row)">编辑</el-button>
            <el-button :icon="Delete" size="small" type="danger" @click="handleDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <!-- 生图表格 -->
      <el-table v-else :data="getImageData()" v-loading="loading" stripe style="width: 100%">
        <el-table-column prop="spec_key" label="Specification Key" width="180" />
        <el-table-column prop="display_name" label="模型名称" width="260" />
        <el-table-column prop="price" label="单价 (元/张)" width="160" align="right">
          <template #default="{ row }">{{ row.price.toFixed(3) }}</template>
        </el-table-column>
        <el-table-column prop="unit" label="计费单位" width="120" align="center" />
        <el-table-column label="操作" width="160" align="center" fixed="right">
          <template #default="{ row }">
            <el-button :icon="Edit" size="small" @click="openEdit(row)">编辑</el-button>
            <el-button :icon="Delete" size="small" type="danger" @click="handleDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="tips">
        <template v-if="isText">
          <p><b>Specification Key</b>: API 返回的模型编码前缀，如 <code>Tog5.4</code>、<code>Tgg25flash</code></p>
          <p><b>价格单位</b>: 元 / 百万 token</p>
        </template>
        <template v-else>
          <p><b>Specification Key</b>: API 返回的编码，如 <code>Gem3.1_1K</code>、<code>Gem2.5</code></p>
          <p><b>价格单位</b>: 元 / 张</p>
        </template>
      </div>
    </el-card>

    <!-- 新增/编辑弹窗 -->
    <el-dialog
      v-model="dialogVisible"
      :title="isEdit ? '编辑模型定价' : '添加模型定价'"
      width="500px"
    >
      <el-form :model="form" label-width="140px">
        <el-form-item label="Specification Key" required>
          <el-input v-model="form.spec_key" :placeholder="isText ? '如 Tog5.4' : '如 Gem3.1_1K'" :disabled="isEdit" />
        </el-form-item>
        <el-form-item label="模型名称" required>
          <el-input v-model="form.display_name" :placeholder="isText ? '如 gpt-5.4' : '如 gemini-3.1 生图(1K)'" />
        </el-form-item>
        <template v-if="isText">
          <el-form-item label="输入 (元/M token)">
            <el-input-number v-model="form.input" :precision="3" :step="0.1" :min="0" style="width: 100%" />
          </el-form-item>
          <el-form-item label="缓存输入 (元/M token)">
            <el-input-number v-model="form.cacheinput" :precision="3" :step="0.01" :min="0" style="width: 100%" />
          </el-form-item>
          <el-form-item label="输出 (元/M token)">
            <el-input-number v-model="form.output" :precision="3" :step="1" :min="0" style="width: 100%" />
          </el-form-item>
        </template>
        <template v-else>
          <el-form-item label="单价 (元/张)">
            <el-input-number v-model="form.price" :precision="3" :step="0.1" :min="0" style="width: 100%" />
          </el-form-item>
        </template>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="handleSave" :loading="saving">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}
.page-header h2 {
  margin: 0;
  font-size: 20px;
  color: #1a1a1a;
}
.tips {
  margin-top: 16px;
  padding: 12px 16px;
  background: #fafafa;
  border-radius: 6px;
  font-size: 13px;
  color: #888;
}
.tips p {
  margin: 4px 0;
}
.tips code {
  background: #f0f0f0;
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 12px;
}
</style>
