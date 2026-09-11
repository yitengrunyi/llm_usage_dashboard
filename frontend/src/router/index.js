import { createRouter, createWebHistory } from 'vue-router'
import Overview from '../views/Overview.vue'
import VendorDetail from '../views/VendorDetail.vue'
import LiteLLM from '../views/LiteLLM.vue'
import Export from '../views/Export.vue'
import Analysis from '../views/Analysis.vue'
import Points from '../views/Points.vue'
import DiscountAdmin from '../views/DiscountAdmin.vue'
import AgentPatrol from '../views/AgentPatrol.vue'
import Login from '../views/Login.vue'
import { checkAuth } from '../api'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    // 登录 (public, 不需登录态)
    { path: '/login', name: 'login', component: Login, meta: { public: true } },
    // AIGC 计费
    { path: '/', name: 'overview', component: Overview },
    { path: '/vendor/:id', name: 'vendor', component: VendorDetail },
    { path: '/analysis', name: 'analysis', component: Analysis },
    { path: '/export', name: 'export', component: Export },
    { path: '/points', name: 'points', component: Points },
    { path: '/discounts', name: 'discounts', component: DiscountAdmin },
    // Agent 自愈巡检报告
    { path: '/agent', name: 'agent', component: AgentPatrol },
    // LiteLLM 模块 — iframe 直接走主控制台 (按 API Key 已并入 React Tab 2)
    { path: '/litellm', name: 'litellm', component: LiteLLM },
  ],
})

// 全局 guard: 非 public 路由调 /api/auth/me 看 cookie. 401 拦截器会自动跳 /login.
// 这里只用作"首次进站"的快速重定向, 避免先显示页面后弹白屏.
// 已在 /login 的不要再 check, 否则 401 又导致死循环.
router.beforeEach(async (to) => {
  if (to.meta.public) return true
  try {
    await checkAuth()
    return true
  } catch (e) {
    // 401 拦截器已经会 location.href = /login, 但 router 这边也明确跳一次
    return { path: '/login', query: { redirect: to.fullPath } }
  }
})

export default router
