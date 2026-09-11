<script setup>
defineOptions({ name: 'AgentPatrol' })
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh, Loading } from '@element-plus/icons-vue'
import { getAgentReports, getAgentReport, triggerAgentPatrol, cancelAgentPatrol } from '../api/index.js'

// ─── 状态 / 展示映射 ───

const STATUS = {
  clean:      { label: '正常',   type: 'success' },
  healed:     { label: '已自愈', type: 'success' },
  degraded:   { label: '降级',   type: 'warning' },
  escalated:  { label: '已升级', type: 'danger' },
  error:      { label: '错误',   type: 'danger' },
  running:    { label: '巡检中', type: 'info' },
  cancelled:  { label: '已取消', type: 'warning' },
}
const statusOf = (s) => STATUS[s] || { label: s || '—', type: 'info' }

// 信号严重度圆点: 红 / 橙 / 黄
const SEVERITY_COLOR = { high: '#f56c6c', medium: '#e6a23c', low: '#f7d842' }

// ISO 时间 → "YYYY-MM-DD HH:mm" (浏览器本地时区)
function fmtTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

const slotLabel = (s) => (s === 'manual' ? '手动' : s)
const fmtText = (v) => (v === null || v === undefined || v === '' ? '—' : v)
const fmtArgs = (a) => (a === null || a === undefined ? '—' : JSON.stringify(a))
const sigTypes = (arr) => (Array.isArray(arr) && arr.length ? arr.join('、') : '—')
const acceptedCount = (r) => (r.actions || []).filter(a => a.accepted).length

// ─── 列表加载 + running 轮询 ───

const reports = ref([])
const loading = ref(false)
const polling = ref(false)   // 有 running 的报告 → 正在前台轮询

let pollTimer = null
const POLL_INTERVAL = 5000            // 5s 一轮
const POLL_MAX_MS = 10 * 60 * 1000    // 轮询兜底上限 10 分钟

function stopPoll() {
  polling.value = false
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}

// 列表里有 running 的报告就每 5s 静默刷一次, 全部跑完或到上限自动停
function maybeStartPoll() {
  if (pollTimer) return
  if (!reports.value.some(r => r.status === 'running')) return
  polling.value = true
  const deadline = Date.now() + POLL_MAX_MS
  pollTimer = setInterval(async () => {
    if (Date.now() > deadline) return stopPoll()
    try {
      reports.value = await getAgentReports(20)
      if (!reports.value.some(r => r.status === 'running')) stopPoll()
    } catch { /* 单次轮询失败不打扰, 下一轮再试 */ }
  }, POLL_INTERVAL)
}

async function load({ silent = false } = {}) {
  if (!silent) loading.value = true
  try {
    reports.value = await getAgentReports(20)
    maybeStartPoll()   // 进页面时正好有巡检在跑 → 也跟着轮询
  } catch (e) {
    ElMessage.error('加载巡检报告失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    if (!silent) loading.value = false
  }
}

onMounted(() => load())
onUnmounted(stopPoll)

// ─── 手动触发 / 取消巡检 ───

const starting = ref(false)
const cancelling = ref(false)
const hasRunning = computed(() => reports.value.some(r => r.status === 'running'))

async function startPatrol() {
  starting.value = true
  try {
    const res = await triggerAgentPatrol()   // 行在响应前同步建好, load 必定可见
    if (res.status === 'replacing') {
      ElMessage.success('已请求取消上一轮, 新一轮排队启动 (旧巡检退出后自动开始)')
    } else {
      ElMessage.success('巡检已启动')
    }
    await load({ silent: true })   // load 内部会自动开轮询
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '启动巡检失败: ' + e.message)
  } finally {
    starting.value = false
  }
}

async function cancelPatrol() {
  cancelling.value = true
  try {
    const res = await cancelAgentPatrol()
    if (res.status === 'not_running') {
      ElMessage.info('当前没有进行中的巡检')
    } else {
      ElMessage.success(res.note || '已请求取消, 当前步骤结束后停止')
    }
    await load({ silent: true })   // 取消是渐进的, 轮询会追到 cancelled 终态
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '取消巡检失败: ' + e.message)
  } finally {
    cancelling.value = false
  }
}

// ─── 详情抽屉 ───

const drawer = ref(false)
const detail = ref(null)
const detailLoading = ref(false)

// 行点击打开. 列表行已带全部字段, 再拉一次单报告保证新鲜
async function openDetail(row) {
  drawer.value = true
  detail.value = row
  detailLoading.value = true
  try {
    detail.value = await getAgentReport(row.id)
  } catch {
    /* 拉取失败就用行内数据先展示, 不额外弹错 */
  } finally {
    detailLoading.value = false
  }
}
</script>

<template>
  <div class="agent-patrol">
    <h2>Agent 巡检</h2>
    <p class="hint">自愈巡检 Agent 的运行报告 — 异常信号、LLM 诊断、自动修复与验证. 点击行查看详情.</p>

    <!-- 工具栏: 触发巡检 + 取消 + 刷新 + 状态说明 -->
    <div class="toolbar">
      <el-button type="primary" :loading="starting" @click="startPatrol">立即巡检</el-button>
      <el-button type="warning" plain :loading="cancelling" :disabled="!hasRunning" @click="cancelPatrol">
        取消巡检
      </el-button>
      <el-button :icon="Refresh" :loading="loading" @click="load()">刷新</el-button>
      <span v-if="polling" class="polling">
        <el-icon class="is-loading"><Loading /></el-icon>
        巡检进行中, 每 5 秒自动刷新…
      </span>
      <span class="legend">
        正常=无异常 · 已自愈=自动修复完成 · 降级=LLM 不可用(纯规则跑完) · 已升级=需人工介入 · 错误=巡检本身失败 · 已取消=手动取消
      </span>
    </div>

    <el-table :data="reports" v-loading="loading" class="reports-table" @row-click="openDetail">
      <template #empty>
        <el-empty description="暂无巡检报告 — 系统健康或巡检未运行" :image-size="80" />
      </template>
      <el-table-column label="时间" width="140">
        <template #default="{ row }">{{ fmtTime(row.patrolled_at) }}</template>
      </el-table-column>
      <el-table-column label="批次" width="80">
        <template #default="{ row }">
          <el-tag v-if="row.slot === 'manual'" size="small">手动</el-tag>
          <span v-else>{{ row.slot }}</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag :type="statusOf(row.status).type" size="small">{{ statusOf(row.status).label }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="LLM" min-width="110">
        <template #default="{ row }">
          <span v-if="row.llm_used">{{ row.model || '✓' }}</span>
          <span v-else class="muted">降级</span>
        </template>
      </el-table-column>
      <el-table-column label="信号" width="60" align="center">
        <template #default="{ row }">{{ (row.signals || []).length }}</template>
      </el-table-column>
      <el-table-column label="动作" width="60" align="center">
        <template #default="{ row }">{{ acceptedCount(row) }}</template>
      </el-table-column>
      <el-table-column label="需人工" width="80" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.escalate" type="danger" size="small">需人工</el-tag>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="摘要" min-width="240" show-overflow-tooltip>
        <template #default="{ row }">{{ fmtText(row.summary) }}</template>
      </el-table-column>
    </el-table>

    <!-- 详情抽屉 -->
    <el-drawer v-model="drawer" size="55%" :title="detail ? `巡检报告 #${detail.id}` : '巡检报告'">
      <div v-if="detail" v-loading="detailLoading" class="detail">
        <!-- 需人工告警置顶 -->
        <el-alert v-if="detail.escalate" type="error" :closable="false" show-icon class="escalate-alert">
          需人工介入: {{ fmtText(detail.escalate_reason) }}
        </el-alert>

        <!-- ① 状态 + 总结 -->
        <div class="detail-header">
          <el-tag :type="statusOf(detail.status).type">{{ statusOf(detail.status).label }}</el-tag>
          <span class="muted">{{ fmtTime(detail.patrolled_at) }} · 批次 {{ slotLabel(detail.slot) }}</span>
          <el-tag v-if="detail.llm_used" size="small" effect="plain">LLM: {{ detail.model || '✓' }}</el-tag>
          <el-tag v-else size="small" type="info" effect="plain">LLM 降级</el-tag>
        </div>
        <p class="summary">{{ fmtText(detail.summary) }}</p>

        <!-- ② 信号 -->
        <h4 class="sec">信号 ({{ (detail.signals || []).length }})</h4>
        <div v-if="(detail.signals || []).length" class="sig-list">
          <div v-for="(s, i) in detail.signals" :key="i" class="sig-row">
            <span class="sev-dot" :style="{ background: SEVERITY_COLOR[s.severity] || '#909399' }" />
            <span class="mono">{{ s.vendor_id || '全局' }}</span>
            <el-tag size="small" effect="plain">{{ s.type }}</el-tag>
            <span class="sig-detail">{{ fmtText(s.detail) }}</span>
          </div>
        </div>
        <p v-else class="muted">无异常信号</p>

        <!-- ③ 诊断 (纯文本保留换行, 不做 markdown 渲染) -->
        <h4 class="sec">诊断</h4>
        <pre v-if="detail.diagnosis" class="diagnosis">{{ detail.diagnosis }}</pre>
        <p v-else class="muted">无诊断内容</p>

        <!-- ④ 动作 -->
        <h4 class="sec">动作 ({{ (detail.actions || []).length }})</h4>
        <el-table v-if="(detail.actions || []).length" :data="detail.actions" size="small" border>
          <el-table-column label="工具" prop="tool" width="150" />
          <el-table-column label="参数" min-width="170">
            <template #default="{ row }"><code class="args">{{ fmtArgs(row.args) }}</code></template>
          </el-table-column>
          <el-table-column label="采纳" width="52" align="center">
            <template #default="{ row }">
              <span :class="row.accepted ? 'ok' : 'bad'">{{ row.accepted ? '✓' : '✗' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="说明" min-width="150">
            <template #default="{ row }">{{ fmtText(row.detail) }}</template>
          </el-table-column>
          <el-table-column label="验证" min-width="120">
            <template #default="{ row }">{{ fmtText(row.verify_result) }}</template>
          </el-table-column>
        </el-table>
        <p v-else class="muted">未执行任何动作</p>

        <!-- ⑤ 修复验证: before → after 信号类型对比 -->
        <h4 class="sec">修复验证</h4>
        <div v-if="(detail.verification || []).length" class="sig-list">
          <div v-for="(v, i) in detail.verification" :key="i" class="sig-row">
            <span class="mono">{{ v.vendor_id }}</span>
            <span class="verify-sig">{{ sigTypes(v.before) }} → {{ sigTypes(v.after) }}</span>
            <span :class="v.ok === true ? 'ok' : v.ok === false ? 'bad' : 'muted'">
              {{ v.ok === true ? '✓' : v.ok === false ? '✗' : '—' }}
            </span>
            <span class="muted verify-note">{{ fmtText(v.note) }}</span>
          </div>
        </div>
        <p v-else class="muted">无验证项</p>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
/* 单行规则为主, 与 App.vue 风格一致 */
h2 { margin: 0 0 6px; font-size: 20px; color: #1a1a1a; }
.hint { color: #888; font-size: 13px; margin-bottom: 16px; }
.toolbar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
.legend { margin-left: auto; color: #aaa; font-size: 12px; }
.polling { display: inline-flex; align-items: center; gap: 4px; color: #1890ff; font-size: 13px; }
.reports-table :deep(.el-table__row) { cursor: pointer; }
.muted { color: #aaa; font-size: 12px; }
.escalate-alert { margin-bottom: 14px; }
.detail-header { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.summary { margin: 10px 0 0; color: #303133; font-size: 14px; line-height: 1.6; }
.sec { margin: 20px 0 8px; font-size: 14px; color: #303133; }
/* 信号 / 验证共用的行式列表 */
.sig-list { border: 1px solid #ebeef5; border-radius: 6px; padding: 2px 12px; }
.sig-row { display: flex; align-items: center; gap: 10px; padding: 7px 0; border-bottom: 1px dashed #f0f2f5; }
.sig-row:last-child { border-bottom: none; }
.sev-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px; color: #303133; flex-shrink: 0;
}
.sig-detail { flex: 1; min-width: 0; font-size: 13px; color: #606266; word-break: break-all; }
/* 诊断: 纯文本, 只保留换行 */
.diagnosis {
  margin: 0; padding: 12px;
  background: #fafbfc; border: 1px solid #ebeef5; border-radius: 6px;
  font-family: inherit; font-size: 13px; line-height: 1.7; color: #303133;
  white-space: pre-wrap; word-break: break-word;
}
.args { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; word-break: break-all; }
.verify-sig { font-size: 13px; color: #606266; }
.verify-note { flex: 1; min-width: 0; word-break: break-all; }
.ok { color: #67c23a; font-weight: 600; }
.bad { color: #f56c6c; font-weight: 600; }
</style>
