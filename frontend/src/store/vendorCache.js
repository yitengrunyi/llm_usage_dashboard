import { reactive, ref } from 'vue'

// 全局共享的日期范围 — 由 Overview 写, 其他页面 (VendorDetail / Export) 进入时
// 从这里取当快照, 之后本地改动**不**回写, 也不污染其他 vendor.
// 切回 Overview 重新改, 才会刷新这个全局值.
function fmt(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
function defaultRange() {
  const d = new Date()
  const dow = d.getDay()
  const offset = dow === 0 ? 7 : dow
  const lastSunday = new Date(d); lastSunday.setDate(d.getDate() - offset)
  const lastMonday = new Date(lastSunday); lastMonday.setDate(lastSunday.getDate() - 6)
  return [fmt(lastMonday), fmt(lastSunday)]
}

export const sharedDateRange = ref(defaultRange())

// 每 vendor 缓存数据 + 当时的 dateRange 快照. 后者用来判失效.
const cache = reactive({})

export function getCache(vendorId) {
  if (!cache[vendorId]) {
    cache[vendorId] = { data: null, dateRange: null }
  }
  return cache[vendorId]
}

export function clearCache(vendorId) {
  if (cache[vendorId]) {
    cache[vendorId].data = null
    cache[vendorId].dateRange = null
  }
}

// 全局清缓存 — Overview 改了全局 dateRange 时显式调一下, 让所有 vendor 下次 mount 重查
export function clearAllCaches() {
  for (const k of Object.keys(cache)) {
    cache[k].data = null
    cache[k].dateRange = null
  }
}
