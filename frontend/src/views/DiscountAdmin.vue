<script setup>
import { ref, reactive, computed, watch, onMounted, onUnmounted } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Delete, ArrowRight, ArrowDown, Check, Refresh, Edit, CreditCard } from '@element-plus/icons-vue'
import {
  fetchDiscounts,
  updateDiscounts,
  fetchDiscountCatalog,
  fetchFamilyModels,
} from '../api/index.js'
import { clearCache } from '../store/vendorCache.js'

// ─── 状态 ───

const config = ref({ vendors: [] })
const catalog = ref([])          // [{family_id, name, prefixes, models}]
const allVendors = ref([])       // [{vendor_id, name}] — 全量, 用来算"还能新增哪些"
const loading = ref(false)
const saving = ref(false)
// dirty = 改动还没成功落盘. 自动保存下它只在"写入中"和"写失败"时为真.
const dirty = ref(false)
const saveFailed = ref(false)
const savedAt = ref(null)

const CUSTOM = '__custom__'
const PAYMENT_TYPE_LABELS = {
  postpaid: '后付费',
  prepaid: '预付费',
}
const paymentTypeLabel = (value) => PAYMENT_TYPE_LABELS[value] || PAYMENT_TYPE_LABELS.postpaid
// 草稿存这里. 自动保存失败后页面被关掉, 下次进来还能捞回改动.
const DRAFT_KEY = 'mud:discount-config-draft'

// 折叠态: key = `${vendor_id}::${family_id}`. 缺省 = 展开, 所以只存"被折叠的".
const collapsed = reactive({})
const famKey = (vid, fid) => `${vid}::${fid}`
const isOpen = (vid, fid) => collapsed[famKey(vid, fid)] !== true
function toggleFamily(vid, fid) {
  const k = famKey(vid, fid)
  collapsed[k] = !collapsed[k]
}

// ─── 折扣数值: 解析 / 展示 ───

// discount 是**乘数**: 1 = 原价, 0.85 = 按 85% 计价.
function parseDiscount(raw, { allowEmpty }) {
  const s = String(raw ?? '').trim()
  if (!s) {
    return allowEmpty ? { ok: true, value: null } : { ok: false, msg: '请填写折扣' }
  }
  const n = Number(s)
  if (!Number.isFinite(n)) return { ok: false, msg: '折扣必须是数字' }
  if (n < 0 || n > 100) return { ok: false, msg: '折扣应在 0 ~ 100 之间' }
  return { ok: true, value: Number(n.toFixed(6)) }
}

const isBlank = (d) => d === null || d === undefined || d === ''

// 0.8500 → "0.85", 1 → "1"
function fmtDiscount(d) {
  if (isBlank(d)) return null
  const n = Number(d)
  return Number.isFinite(n) ? String(Number(n.toFixed(6))) : null
}

function discountHint(d) {
  if (isBlank(d)) return ''
  const n = Number(d)
  if (!Number.isFinite(n)) return ''
  if (n === 1) return '原价'
  if (n < 1) return `省 ${((1 - n) * 100).toFixed(1).replace(/\.0$/, '')}%`
  return `加价 ${((n - 1) * 100).toFixed(1).replace(/\.0$/, '')}%`
}

// 模型折扣留空时, 实际吃到的是系折扣
function modelFallbackHint(family) {
  if (isBlank(family.discount)) return '系折扣未设置, 按原价'
  return `系折扣 ${fmtDiscount(family.discount)} · ${discountHint(family.discount)}`
}

// ─── 加载 / 草稿 ───

function readDraft() {
  try {
    const raw = localStorage.getItem(DRAFT_KEY)
    if (!raw) return null
    const d = JSON.parse(raw)
    return d && Array.isArray(d.vendors) ? d : null
  } catch {
    return null
  }
}
function writeDraft() {
  try {
    localStorage.setItem(DRAFT_KEY, JSON.stringify({ vendors: config.value.vendors }))
  } catch { /* 隐私模式 / 配额满 — 草稿只是兜底, 失败不打扰 */ }
}
function clearDraft() {
  try { localStorage.removeItem(DRAFT_KEY) } catch { /* 同上 */ }
}

// 只要有未保存改动就把草稿刷一份. deep watch 命中所有层级 (含弹窗改的折扣).
watch(config, () => { if (dirty.value) writeDraft() }, { deep: true })

// 展示字段 (vendor_name / vendor_missing / is_custom) 不落盘, 每次从 vendors.json + 目录重算
function hydrateDisplay(vendors) {
  const nameMap = new Map(allVendors.value.map(v => [v.vendor_id, v.name]))
  const catalogIds = new Set(catalog.value.map(f => f.family_id))
  return vendors.map(v => ({
    vendor_id: v.vendor_id,
    payment_type: v.payment_type || 'postpaid',
    vendor_name: nameMap.get(v.vendor_id) || v.vendor_id,
    vendor_missing: !nameMap.has(v.vendor_id),
    families: (v.families || []).map(f => ({
      family_id: f.family_id,
      name: f.name,
      prefixes: f.prefixes || [],
      discount: f.discount ?? null,
      is_custom: !catalogIds.has(f.family_id),
      models: (f.models || []).map(m => ({ model: m.model, discount: m.discount ?? null })),
    })),
  }))
}

// 全量 vendor = 还没配的 + 已经配上的. 本地新增/删除后下拉立刻跟着变.
function syncAllVendors(cfg) {
  const merged = new Map()
  for (const v of cfg.available_vendors || []) merged.set(v.vendor_id, v.name)
  for (const v of cfg.vendors || []) {
    if (!v.vendor_missing) merged.set(v.vendor_id, v.vendor_name || v.vendor_id)
  }
  allVendors.value = [...merged].map(([vendor_id, name]) => ({ vendor_id, name }))
}

function applyConfig(cfg) {
  syncAllVendors(cfg)
  config.value = { vendors: hydrateDisplay(cfg.vendors || []) }
}

async function load({ keepDraft = false } = {}) {
  loading.value = true
  try {
    const [cfg, cat] = await Promise.all([fetchDiscounts(), fetchDiscountCatalog()])
    catalog.value = cat.families || []   // applyConfig 要用它算 is_custom, 先赋值
    applyConfig(cfg)
    dirty.value = false
    saveFailed.value = false
    savedAt.value = null
    if (keepDraft) await maybeRestoreDraft()
    else clearDraft()
  } catch (e) {
    ElMessage.error('加载折扣配置失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    loading.value = false
  }
}

async function maybeRestoreDraft() {
  const draft = readDraft()
  if (!draft) return
  try {
    await ElMessageBox.confirm(
      '检测到上次退出时有未保存的折扣配置改动。要恢复吗？',
      '恢复未保存的改动',
      { confirmButtonText: '恢复', cancelButtonText: '丢弃', type: 'warning' },
    )
  } catch {
    clearDraft()
    return
  }
  config.value = { vendors: hydrateDisplay(draft.vendors) }
  dirty.value = true
  // 恢复出来的改动直接落盘, 页面上不存在"待保存"这个状态
  if (await persist()) ElMessage.success('已恢复未保存的改动并保存')
}

onMounted(() => load({ keepDraft: true }))

// ─── 未保存保护 ───

// 1) 关标签页 / 刷新 — 浏览器原生二次确认
function guardUnload(e) {
  if (!dirty.value) return
  e.preventDefault()
  e.returnValue = ''
}
onMounted(() => window.addEventListener('beforeunload', guardUnload))
onUnmounted(() => window.removeEventListener('beforeunload', guardUnload))

// 2) 站内跳走 (点导航栏别的页面) — 只有自动保存失败时才拦
onBeforeRouteLeave(async () => {
  if (inflight) await inflight   // 正在写就等它写完, 别为了几十毫秒弹窗
  if (!dirty.value) return true
  let action
  try {
    await ElMessageBox.confirm(
      '有折扣改动没成功写入服务器。选「重试保存」再试一次, 选「放弃改动」丢弃; 关闭本弹窗则留在当前页。',
      '改动未写入',
      {
        confirmButtonText: '重试保存',
        cancelButtonText: '放弃改动',
        distinguishCancelAndClose: true,   // 关闭(X)/ESC 区别于"放弃改动"
        type: 'warning',
      },
    )
    action = 'confirm'
  } catch (e) {
    action = e   // 'cancel' = 放弃改动 / 'close' = 留下
  }

  if (action === 'confirm') return await persist()   // 还是失败 → false, 不放人走
  if (action === 'cancel') {
    clearDraft()
    dirty.value = false
    saveFailed.value = false
    return true
  }
  return false
})

// ─── 新增供应商 ───

const vendorDialog = ref(false)
const newVendorId = ref('')

const availableVendors = computed(() => {
  const used = new Set(config.value.vendors.map(v => v.vendor_id))
  return allVendors.value.filter(v => !used.has(v.vendor_id))
})

function openAddVendor() {
  newVendorId.value = ''
  vendorDialog.value = true
}

function confirmAddVendor() {
  const vid = newVendorId.value
  if (!vid) return ElMessage.warning('请选择供应商')
  const hit = allVendors.value.find(v => v.vendor_id === vid)
  config.value.vendors.push({
    vendor_id: vid,
    payment_type: 'postpaid',
    vendor_name: hit?.name || vid,
    vendor_missing: false,
    families: [],
  })
  vendorDialog.value = false
  touch()
}

async function removeVendor(vendor) {
  try {
    await ElMessageBox.confirm(
      `删除供应商 ${vendor.vendor_name} 的全部折扣配置 (含 ${vendor.families.length} 个系)？`,
      '确认删除', { type: 'warning' },
    )
  } catch { return }
  config.value.vendors = config.value.vendors.filter(v => v !== vendor)
  touch()
}

function updatePaymentType(vendor, paymentType) {
  if (!PAYMENT_TYPE_LABELS[paymentType] || vendor.payment_type === paymentType) return
  vendor.payment_type = paymentType
  clearCache(vendor.vendor_id)
  touch()
}

// ─── 新增系 ───

const familyDialog = ref(false)
const familyTarget = ref(null)     // 正在往哪个 vendor 加
const familyForm = reactive({ family_id: '', custom_name: '', custom_prefixes: '', discount: '1' })

function openAddFamily(vendor) {
  familyTarget.value = vendor
  Object.assign(familyForm, { family_id: '', custom_name: '', custom_prefixes: '', discount: '1' })
  familyDialog.value = true
}

// 该 vendor 还没加过的系
const availableFamilies = computed(() => {
  if (!familyTarget.value) return []
  const used = new Set(familyTarget.value.families.map(f => f.family_id))
  return catalog.value.filter(f => !used.has(f.family_id))
})

const isCustomFamily = computed(() => familyForm.family_id === CUSTOM)

function slugify(name) {
  return name.trim().toLowerCase().replace(/\s+/g, '-').replace(/[^\w一-龥-]/g, '')
}

function confirmAddFamily() {
  const vendor = familyTarget.value
  if (!vendor) return

  const parsed = parseDiscount(familyForm.discount, { allowEmpty: false })
  if (!parsed.ok) return ElMessage.warning(parsed.msg)

  let family_id, name, prefixes
  if (isCustomFamily.value) {
    name = familyForm.custom_name.trim()
    if (!name) return ElMessage.warning('请填写自定义系名称')
    family_id = slugify(name) || `custom-${Date.now()}`
    if (vendor.families.some(f => f.family_id === family_id)) {
      return ElMessage.warning(`该供应商下已存在系 ${name}`)
    }
    prefixes = familyForm.custom_prefixes
      .split(',').map(s => s.trim().toLowerCase()).filter(Boolean)
  } else {
    if (!familyForm.family_id) return ElMessage.warning('请选择大模型系')
    const hit = catalog.value.find(f => f.family_id === familyForm.family_id)
    family_id = hit.family_id
    name = hit.name
    prefixes = hit.prefixes || []
  }

  vendor.families.push({
    family_id, name, prefixes,
    discount: parsed.value,
    is_custom: isCustomFamily.value,
    models: [],
  })
  familyDialog.value = false
  touch()
}

async function removeFamily(vendor, family) {
  try {
    await ElMessageBox.confirm(
      `删除 ${vendor.vendor_name} 下的 ${family.name} (含 ${family.models.length} 个模型)？`,
      '确认删除', { type: 'warning' },
    )
  } catch { return }
  vendor.families = vendor.families.filter(f => f !== family)
  delete collapsed[famKey(vendor.vendor_id, family.family_id)]
  touch()
}

// ─── 新增模型 ───

const modelDialog = ref(false)
const modelTargetVendor = ref(null)
const modelTargetFamily = ref(null)
const modelOptions = ref([])
const modelOptionsLoading = ref(false)
const modelForm = reactive({ model: '', discount: '' })

async function openAddModel(vendor, family) {
  modelTargetVendor.value = vendor
  modelTargetFamily.value = family
  Object.assign(modelForm, { model: '', discount: '' })
  modelOptions.value = []
  modelDialog.value = true

  modelOptionsLoading.value = true
  try {
    const r = await fetchFamilyModels(family.family_id, vendor.vendor_id)
    // 已经加过的不再出现在下拉里
    const used = new Set(family.models.map(m => m.model.toLowerCase()))
    modelOptions.value = (r.models || []).filter(m => !used.has(m.toLowerCase()))
  } catch {
    ElMessage.warning('模型候选加载失败, 可直接手动输入模型名')
  } finally {
    modelOptionsLoading.value = false
  }
}

function confirmAddModel() {
  const family = modelTargetFamily.value
  const name = (modelForm.model || '').trim()
  if (!name) return ElMessage.warning('请选择或输入模型名')
  if (family.models.some(m => m.model.toLowerCase() === name.toLowerCase())) {
    return ElMessage.warning(`${family.name} 下已存在模型 ${name}`)
  }
  const parsed = parseDiscount(modelForm.discount, { allowEmpty: true })
  if (!parsed.ok) return ElMessage.warning(parsed.msg)

  family.models.push({ model: name, discount: parsed.value })
  // 加了模型就展开, 否则新增完看不见
  collapsed[famKey(modelTargetVendor.value.vendor_id, family.family_id)] = false
  modelDialog.value = false
  touch()
}

function removeModel(family, model) {
  family.models = family.models.filter(m => m !== model)
  touch()
}

// ─── 编辑折扣 (系 / 模型共用一个弹窗) ───

const editDialog = ref(false)
const editTarget = ref(null)     // {kind: 'family'|'model', family, model}
const editValue = ref('')

const editIsModel = computed(() => editTarget.value?.kind === 'model')
const editTitle = computed(() => {
  const t = editTarget.value
  if (!t) return '编辑折扣'
  return t.kind === 'family' ? `编辑系折扣 — ${t.family.name}` : `编辑模型折扣 — ${t.model.model}`
})
// 弹窗里实时回显换算结果, 填完就知道填对没
const editPreview = computed(() => {
  const parsed = parseDiscount(editValue.value, { allowEmpty: editIsModel.value })
  if (!parsed.ok) return parsed.msg
  if (parsed.value === null) return `留空 = 跟随${modelFallbackHint(editTarget.value.family)}`
  return discountHint(parsed.value)
})

function openEditFamily(family) {
  editTarget.value = { kind: 'family', family }
  editValue.value = fmtDiscount(family.discount) ?? ''
  editDialog.value = true
}

function openEditModel(family, model) {
  editTarget.value = { kind: 'model', family, model }
  editValue.value = fmtDiscount(model.discount) ?? ''
  editDialog.value = true
}

function confirmEdit() {
  const t = editTarget.value
  if (!t) return
  const parsed = parseDiscount(editValue.value, { allowEmpty: t.kind === 'model' })
  if (!parsed.ok) return ElMessage.warning(parsed.msg)

  if (t.kind === 'family') t.family.discount = parsed.value
  else t.model.discount = parsed.value

  editDialog.value = false
  touch()
}

// ─── 自动保存 ───

// 任何改动都立刻落盘, 页面上没有"保存"按钮.
// 写入中又来了改动 → 只记一个 pending, 当前这轮写完再补一次, 保证最后一次改动一定进去.
let savePending = false
let inflight = null

function touch() {
  dirty.value = true
  persist()
}

async function persist() {
  if (inflight) {
    savePending = true
    return inflight      // 同一轮循环会带上这次改动, 直接等它的结果
  }
  inflight = (async () => {
    saving.value = true
    try {
      while (true) {
        savePending = false
        const ok = await pushConfig()
        if (!ok) return false
        if (!savePending) return true
      }
    } finally {
      saving.value = false
      inflight = null
    }
  })()
  return inflight
}

// 单次 PUT. 失败不回滚本地改动, 保留 dirty + 草稿, 等重试 / 离开时再问.
async function pushConfig() {
  try {
    // 只送后端认的字段, 展示字段不落盘
    const payload = {
      vendors: config.value.vendors.map(v => ({
        vendor_id: v.vendor_id,
        payment_type: v.payment_type,
        families: v.families.map(f => ({
          family_id: f.family_id,
          name: f.name,
          discount: f.discount ?? null,
          prefixes: f.prefixes || [],
          models: f.models.map(m => ({ model: m.model, discount: m.discount ?? null })),
        })),
      })),
    }
    const saved = await updateDiscounts(payload)
    // 只同步下拉用的 vendor 列表, 不回写 config —— 回写会换掉对象引用,
    // 打断"请求还在飞时用户已经打开下一个弹窗"的编辑目标.
    syncAllVendors(saved)
    saveFailed.value = false
    savedAt.value = new Date()
    // 飞行途中又改了 → 那份改动还没落盘, dirty / 草稿都留着, 等下一轮
    if (!savePending) {
      dirty.value = false
      clearDraft()
    }
    return true
  } catch (e) {
    saveFailed.value = true
    ElMessage.error('自动保存失败: ' + (e.response?.data?.detail || e.message))
    return false
  }
}

async function reload() {
  if (dirty.value) {
    try {
      await ElMessageBox.confirm(
        '有改动没成功写入服务器, 重新加载会丢弃它们。继续？', '确认', { type: 'warning' },
      )
    } catch { return }
  }
  await load()
}

// ─── 头部状态 ───

const saveStatus = computed(() => {
  if (saving.value) return { text: '保存中…', type: 'info', effect: 'dark' }
  if (dirty.value || saveFailed.value) return { text: '未写入', type: 'danger', effect: 'dark' }
  if (savedAt.value) {
    const t = savedAt.value.toLocaleTimeString('zh-CN', { hour12: false })
    return { text: `已自动保存 ${t}`, type: 'success', effect: 'plain' }
  }
  return null
})
</script>

<template>
  <div class="discount-admin" v-loading="loading">
    <div class="page-header">
      <div class="title-wrap">
        <h2>供应商折扣配置</h2>
        <el-tag v-if="saveStatus" :type="saveStatus.type" size="small" :effect="saveStatus.effect">
          {{ saveStatus.text }}
        </el-tag>
      </div>
      <div class="header-actions">
        <el-button
          v-if="dirty && !saving"
          type="danger"
          :icon="Check"
          @click="persist"
        >
          重试保存
        </el-button>
        <el-button :icon="Refresh" @click="reload">重新加载</el-button>
        <el-button type="primary" :icon="Plus" @click="openAddVendor" :disabled="!availableVendors.length">
          新增供应商
        </el-button>
      </div>
    </div>

    <el-alert type="info" :closable="false" class="semantics">
      折扣是<b>乘数</b>: <code>1</code> = 原价, <code>0.85</code> = 按 85% 计价, <code>0</code> = 免费。
      模型折扣留空表示跟随所属系的折扣。改动即时写入服务器, 无需手动保存。
    </el-alert>

    <el-empty
      v-if="!loading && !config.vendors.length"
      description="还没有配置任何供应商折扣"
    >
      <el-button type="primary" :icon="Plus" @click="openAddVendor">新增供应商</el-button>
    </el-empty>

    <!-- 一家 vendor 一张卡 -->
    <el-card v-for="vendor in config.vendors" :key="vendor.vendor_id" class="vendor-card" shadow="never">
      <template #header>
        <div class="vendor-header">
          <div class="vendor-title">
            <span class="vendor-name">{{ vendor.vendor_name }}</span>
            <span class="mono-id">{{ vendor.vendor_id }}</span>
            <el-tag v-if="vendor.vendor_missing" type="danger" size="small" effect="plain">
              vendors.json 中已不存在
            </el-tag>
            <span class="muted">{{ vendor.families.length }} 个系</span>
          </div>
          <div class="vendor-actions">
            <el-dropdown trigger="click" @command="(command) => updatePaymentType(vendor, command)">
              <el-button size="small" :icon="CreditCard">
                付费方式：{{ paymentTypeLabel(vendor.payment_type) }}
                <el-icon class="el-icon--right"><ArrowDown /></el-icon>
              </el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item
                    v-for="(label, value) in PAYMENT_TYPE_LABELS"
                    :key="value"
                    :command="value"
                    :disabled="vendor.payment_type === value"
                  >
                    {{ label }}
                  </el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
            <el-button size="small" :icon="Plus" @click="openAddFamily(vendor)">新增大模型系</el-button>
            <el-button size="small" type="danger" :icon="Delete" plain @click="removeVendor(vendor)">
              删除供应商
            </el-button>
          </div>
        </div>
      </template>

      <el-empty v-if="!vendor.families.length" description="还没有配置大模型系" :image-size="60" />

      <!-- 一个系一块, 模型列表可折叠, 默认展开 -->
      <div v-for="family in vendor.families" :key="family.family_id" class="family-block">
        <div class="family-header">
          <div class="row-left family-left" @click="toggleFamily(vendor.vendor_id, family.family_id)">
            <el-icon class="arrow" :class="{ open: isOpen(vendor.vendor_id, family.family_id) }">
              <ArrowRight />
            </el-icon>
            <span class="family-name">{{ family.name }}</span>
            <el-tag v-if="family.is_custom" size="small" type="info" effect="plain">自定义</el-tag>
            <span class="muted">{{ family.models.length }} 个模型</span>
          </div>

          <!-- 右侧四列定宽, 和下面模型行严格对齐 -->
          <div class="row-right">
            <span class="cell-label">系折扣</span>
            <span class="cell-value">
              <b v-if="fmtDiscount(family.discount) !== null" class="num">
                {{ fmtDiscount(family.discount) }}
              </b>
              <span v-else class="unset">未设置</span>
            </span>
            <span class="cell-hint">{{ discountHint(family.discount) || '按原价' }}</span>
            <span class="cell-actions">
              <el-button size="small" :icon="Edit" @click="openEditFamily(family)">编辑</el-button>
              <el-button size="small" :icon="Plus" @click="openAddModel(vendor, family)">模型</el-button>
              <el-button size="small" type="danger" :icon="Delete" plain
                @click="removeFamily(vendor, family)" />
            </span>
          </div>
        </div>

        <div v-show="isOpen(vendor.vendor_id, family.family_id)" class="model-list">
          <div v-if="!family.models.length" class="no-model">
            该系下还没有单独配折扣的模型 — 本系所有模型吃系折扣
          </div>
          <div v-for="model in family.models" :key="model.model" class="model-row">
            <div class="row-left">
              <span class="model-name">{{ model.model }}</span>
            </div>
            <div class="row-right">
              <span class="cell-label" />
              <span class="cell-value">
                <b v-if="fmtDiscount(model.discount) !== null" class="num">
                  {{ fmtDiscount(model.discount) }}
                </b>
                <span v-else class="unset">跟随系</span>
              </span>
              <span class="cell-hint">
                {{ fmtDiscount(model.discount) !== null ? discountHint(model.discount) : modelFallbackHint(family) }}
              </span>
              <span class="cell-actions">
                <el-button size="small" :icon="Edit" @click="openEditModel(family, model)">编辑</el-button>
                <el-button size="small" type="danger" :icon="Delete" plain
                  @click="removeModel(family, model)" />
              </span>
            </div>
          </div>
        </div>
      </div>
    </el-card>

    <!-- 编辑折扣 (系 / 模型共用) -->
    <el-dialog v-model="editDialog" :title="editTitle" width="440px">
      <el-form label-width="80px">
        <el-form-item label="折扣">
          <el-input
            v-model="editValue"
            :placeholder="editIsModel ? '留空 = 跟随系折扣' : '如 0.85'"
            @keyup.enter="confirmEdit"
          />
          <div class="form-tip">{{ editPreview }}</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editDialog = false">取消</el-button>
        <el-button type="primary" @click="confirmEdit">确定</el-button>
      </template>
    </el-dialog>

    <!-- 新增供应商 -->
    <el-dialog v-model="vendorDialog" title="新增供应商" width="440px">
      <el-form label-width="80px">
        <el-form-item label="供应商">
          <el-select v-model="newVendorId" placeholder="选择供应商" filterable style="width: 100%">
            <el-option
              v-for="v in availableVendors"
              :key="v.vendor_id"
              :label="`${v.name} (${v.vendor_id})`"
              :value="v.vendor_id"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="vendorDialog = false">取消</el-button>
        <el-button type="primary" @click="confirmAddVendor">添加</el-button>
      </template>
    </el-dialog>

    <!-- 新增大模型系 -->
    <el-dialog v-model="familyDialog" title="新增大模型系" width="480px">
      <el-form label-width="100px">
        <el-form-item label="大模型系">
          <el-select v-model="familyForm.family_id" placeholder="选择大模型系" filterable style="width: 100%">
            <el-option v-for="f in availableFamilies" :key="f.family_id" :label="f.name" :value="f.family_id" />
            <el-option label="自定义…" :value="CUSTOM" />
          </el-select>
        </el-form-item>
        <template v-if="isCustomFamily">
          <el-form-item label="系名称" required>
            <el-input v-model="familyForm.custom_name" placeholder="如 ernie系" />
          </el-form-item>
          <el-form-item label="模型名前缀">
            <el-input v-model="familyForm.custom_prefixes" placeholder="逗号分隔, 如 ernie,wenxin" />
            <div class="form-tip">选填。没单独配折扣的模型靠前缀归到本系, 吃系折扣。</div>
          </el-form-item>
        </template>
        <el-form-item label="系折扣">
          <el-input v-model="familyForm.discount" placeholder="如 0.85" />
          <div class="form-tip">{{ discountHint(parseDiscount(familyForm.discount, { allowEmpty: false }).value) || '请填 0 ~ 100 的数字' }}</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="familyDialog = false">取消</el-button>
        <el-button type="primary" @click="confirmAddFamily">添加</el-button>
      </template>
    </el-dialog>

    <!-- 新增模型 -->
    <el-dialog v-model="modelDialog" :title="`新增模型 — ${modelTargetFamily?.name || ''}`" width="480px">
      <el-form label-width="90px">
        <el-form-item label="模型">
          <el-select
            v-model="modelForm.model"
            placeholder="选择模型, 或直接输入自定义模型名"
            filterable allow-create default-first-option
            :loading="modelOptionsLoading"
            style="width: 100%"
          >
            <el-option v-for="m in modelOptions" :key="m" :label="m" :value="m" />
          </el-select>
          <div class="form-tip">下拉是该系的候选模型 (含该供应商历史用过的)。没有就直接输入。</div>
        </el-form-item>
        <el-form-item label="模型折扣">
          <el-input v-model="modelForm.discount" placeholder="留空 = 跟随系折扣" />
          <div class="form-tip">
            {{ modelTargetFamily ? `留空则跟随${modelFallbackHint(modelTargetFamily)}` : '' }}
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="modelDialog = false">取消</el-button>
        <el-button type="primary" @click="confirmAddModel">添加</el-button>
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
.title-wrap {
  display: flex;
  align-items: center;
  gap: 10px;
}
.page-header h2 {
  margin: 0;
  font-size: 20px;
  color: #1a1a1a;
}
.semantics {
  margin-bottom: 16px;
}
.semantics code {
  background: #f0f0f0;
  padding: 1px 5px;
  border-radius: 3px;
}
.vendor-card {
  margin-bottom: 16px;
}
.vendor-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
}
.vendor-title {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.vendor-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.vendor-actions .el-button + .el-button {
  margin-left: 0;
}
.vendor-name {
  font-size: 16px;
  font-weight: 600;
  color: #1a1a1a;
}
.mono-id {
  font-size: 12px;
  color: #aaa;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.muted {
  font-size: 12px;
  color: #aaa;
}
.family-block {
  border: 1px solid #ebeef5;
  border-radius: 6px;
  margin-bottom: 10px;
}
.family-block:last-child {
  margin-bottom: 0;
}
.family-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 14px;
  background: #fafbfc;
  border-radius: 6px 6px 0 0;
}
.family-left {
  cursor: pointer;
  user-select: none;
}
.arrow {
  transition: transform 0.2s;
  color: #909399;
}
.arrow.open {
  transform: rotate(90deg);
}
.family-name {
  font-weight: 600;
  color: #303133;
}

/* 左侧自适应, 右侧四列定宽 —— 系折扣和模型折扣因此严格对齐 */
.row-left {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  min-width: 0;
}
.row-right {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-shrink: 0;
}
.cell-label {
  width: 48px;
  text-align: right;
  font-size: 13px;
  color: #909399;
}
.cell-value {
  width: 72px;
  text-align: right;
}
.cell-value .num {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 15px;
  font-weight: 600;
  color: #1a1a1a;
}
.cell-value .unset {
  font-size: 12px;
  color: #c0c4cc;
}
.cell-hint {
  width: 170px;
  font-size: 12px;
  color: #909399;
}
.cell-actions {
  width: 190px;
  display: flex;
  justify-content: flex-end;
  gap: 6px;
}
.model-list {
  padding: 2px 14px 8px;
}
.model-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 7px 0;
  border-bottom: 1px dashed #f0f2f5;
}
.model-row:last-child {
  border-bottom: none;
}
/* 缩进对齐系名 (箭头 16px + gap 8px), 体现层级 */
.model-row .row-left {
  padding-left: 24px;
}
.model-name {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px;
  color: #303133;
  word-break: break-all;
}
.no-model {
  font-size: 12px;
  color: #c0c4cc;
  padding: 8px 0 4px 24px;
}
.form-tip {
  font-size: 12px;
  color: #909399;
  line-height: 1.5;
  margin-top: 2px;
}

@media (max-width: 768px) {
  .page-header,
  .vendor-header {
    align-items: flex-start;
    flex-direction: column;
  }
  .header-actions,
  .vendor-actions,
  .vendor-title {
    display: flex;
    flex-wrap: wrap;
  }
  .header-actions,
  .vendor-actions {
    width: 100%;
  }
  .family-header,
  .model-row {
    align-items: stretch;
    flex-direction: column;
  }
  .row-right {
    display: grid;
    grid-template-columns: 48px 64px minmax(0, 1fr);
    width: 100%;
    gap: 8px;
  }
  .cell-value,
  .cell-hint,
  .cell-actions {
    width: auto;
  }
  .cell-hint {
    overflow-wrap: anywhere;
  }
  .cell-actions {
    grid-column: 1 / -1;
    justify-content: flex-end;
  }
}
</style>
