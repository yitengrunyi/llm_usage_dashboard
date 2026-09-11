import axios from 'axios'

// withCredentials: 让浏览器跨域请求自动带上 cookie (dashboard_session JWT).
// 同域部署 (nginx 反代) 不需要这个 flag 也能带 cookie, 但 dev 模式前端 :5173 / 后端 :3081
// 是跨端口的, 必须显式开. 后端要配 ALLOWED_ORIGINS 明确这两个 origin (不能 *).
const api = axios.create({ baseURL: '/api', withCredentials: true })

// 401 拦截 — token 没 / 过期 / 无效 都返 401, 全局跳 /login
api.interceptors.response.use(
  r => r,
  err => {
    if (err.response && err.response.status === 401) {
      // 已在 /login 页就别死循环
      if (!window.location.pathname.startsWith('/login')) {
        window.location.href = '/login'
      }
    }
    return Promise.reject(err)
  }
)

// 登录相关
export function login(password) {
  return api.post('/auth/login', { password }).then(r => r.data)
}
export function logout() {
  return api.post('/auth/logout').then(r => r.data)
}
export function checkAuth() {
  return api.get('/auth/me').then(r => r.data)
}

// ─── 原有接口 ───

export function fetchUsage(start, end, type = 'text') {
  return api.get('/usage', { params: { start, end, type } }).then(r => r.data)
}

export function fetchSummary(start, end) {
  return api.get('/usage/summary', { params: { start, end } }).then(r => r.data)
}

export function fetchPricing() {
  return api.get('/pricing').then(r => r.data)
}

export function updatePricing(pricing) {
  return api.put('/pricing', pricing).then(r => r.data)
}

// ─── 多供应商接口 ───

export function fetchVendors() {
  return api.get('/vendors').then(r => r.data)
}

export function fetchVendorUsage(vendorId, start, end, models = null, apiKey = null) {
  // models: 可选数组. 传了就走 ?models=A&models=B, 后端返 daily_by_model 字段供多曲线图.
  // apiKey: 可选字符串. 按 API key 过滤 (只影响 DB 段)
  const params = { start, end }
  if (models && models.length) params.models = models
  if (apiKey) params.api_key = apiKey
  return api.get(`/vendors/${vendorId}/usage`, {
    params,
    // axios 默认数组 → ?models[]=A&models[]=B, FastAPI 要 ?models=A&models=B
    paramsSerializer: { indexes: null },
  }).then(r => r.data)
}

export function fetchVendorApiKeys(vendorId, start = null, end = null) {
  const params = {}
  if (start) params.start = start
  if (end) params.end = end
  return api.get(`/vendors/${vendorId}/api-keys`, { params }).then(r => r.data)
}

export function fetchOverview(start, end, models = null) {
  const params = { start, end }
  if (models && models.length) params.models = models
  return api.get('/overview', {
    params,
    paramsSerializer: { indexes: null },
  }).then(r => r.data)
}

// ─── Session 管理 ───

export function getSessionStatus(vendorId) {
  return api.get(`/vendors/${vendorId}/session`).then(r => r.data)
}

export function triggerLogin(vendorId) {
  return api.post(`/vendors/${vendorId}/login`).then(r => r.data)
}

export function cancelLogin(vendorId) {
  return api.post(`/vendors/${vendorId}/login/cancel`).then(r => r.data)
}

export function triggerLogout(vendorId) {
  return api.post(`/vendors/${vendorId}/logout`).then(r => r.data)
}

export function updateAccount(vendorId, username, password) {
  return api.put(`/vendors/${vendorId}/account`, { username, password }).then(r => r.data)
}

// ─── Ingest 模块 (PR5): 入库状态 + 手动重试 ───

export function getIngestState() {
  return api.get('/ingest/state').then(r => r.data)
}

export function getIngestRuns(vendorId, limit = 5) {
  return api.get('/ingest/runs', { params: { vendor_id: vendorId, limit } }).then(r => r.data)
}

export function triggerIngest(vendorId, start = null, end = null) {
  return api.post(`/ingest/vendors/${vendorId}/trigger`, { start, end }).then(r => r.data)
}

export function retryIngestRun(runId) {
  return api.post(`/ingest/runs/${runId}/retry`).then(r => r.data)
}

// ─── Agent 巡检模块: 自愈巡检报告 + 手动触发 ───

export function getAgentReports(limit = 20) {
  return api.get('/ingest/agent/reports', { params: { limit } }).then(r => r.data)
}

export function getAgentReport(rid) {
  return api.get(`/ingest/agent/reports/${rid}`).then(r => r.data)
}

export function triggerAgentPatrol() {
  return api.post('/ingest/agent/patrol').then(r => r.data)
}

export function cancelAgentPatrol() {
  return api.post('/ingest/agent/patrol/cancel').then(r => r.data)
}

// ─── LiteLLM 模块 ───

const litellmApi = axios.create({
  baseURL: '/api',
  // 与后端 .env 的 API_KEY 匹配; 构建时可用 VITE_LITELLM_API_KEY 覆盖
  headers: { 'x-api-key': import.meta.env.VITE_LITELLM_API_KEY || 'dev-only-change-me' },
})

export function fetchLitellmSpend(start, end, groupBy = ['model']) {
  return litellmApi.post('/litellm/analytics/spend', {
    start, end, group_by: groupBy,
  }).then(r => r.data)
}

export function fetchLitellmOptions() {
  return litellmApi.get('/litellm/analytics/options').then(r => r.data)
}

export function fetchLitellmApiKeys() {
  return litellmApi.get('/litellm/analytics/api-keys').then(r => r.data)
}

export function fetchLitellmSpendByApiKey(start, end, apiKey) {
  return litellmApi.post('/litellm/analytics/spend/by-api-key', {
    start, end, api_key: apiKey,
  }).then(r => r.data)
}

// ─── 网宿模型清单 CRUD ───

export function fetchWangsuModels() {
  return api.get('/wangsu/models').then(r => r.data)
}

export function addWangsuModel(code, enabled = true) {
  return api.post('/wangsu/models', { code, enabled }).then(r => r.data)
}

export function toggleWangsuModel(code, enabled) {
  return api.patch(`/wangsu/models/${encodeURIComponent(code)}`, { enabled }).then(r => r.data)
}

export function deleteWangsuModel(code) {
  return api.delete(`/wangsu/models/${encodeURIComponent(code)}`).then(r => r.data)
}

export function replaceWangsuModels(models) {
  return api.put('/wangsu/models', { models }).then(r => r.data)
}

// ─── 积分模块 ───

export function fetchPointsData(start, end) {
  return api.get('/points', { params: { start, end } }).then(r => r.data)
}

// ─── 折扣配置模块 ───

export function fetchDiscounts() {
  return api.get('/discounts').then(r => r.data)
}

export function updateDiscounts(config) {
  return api.put('/discounts', config).then(r => r.data)
}

export function fetchDiscountCatalog() {
  return api.get('/discounts/catalog').then(r => r.data)
}

export function fetchFamilyModels(familyId, vendorId = null) {
  const params = { family_id: familyId }
  if (vendorId) params.vendor_id = vendorId
  return api.get('/discounts/models', { params }).then(r => r.data)
}

export function resolveDiscount(vendorId, model) {
  return api.get('/discounts/resolve', { params: { vendor_id: vendorId, model } }).then(r => r.data)
}
