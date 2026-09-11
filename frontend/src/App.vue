<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { DataLine, HomeFilled, Download, TrendCharts, SwitchButton, Coin, Discount, Aim } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { fetchVendors, logout as apiLogout } from './api/index.js'

const route = useRoute()
const router = useRouter()
const vendors = ref([])

const isLitellm = computed(() => route.path.startsWith('/litellm'))
const isLogin = computed(() => route.path === '/login')

onMounted(async () => {
  try {
    vendors.value = await fetchVendors()
  } catch { /* ignore (未登录时 401 会被拦截器跳到 /login) */ }
})

async function doLogout() {
  try {
    await apiLogout()
    ElMessage.success('已退出')
  } catch { /* cookie 已被服务器/浏览器清, 不阻塞跳转 */ }
  router.replace('/login')
}
</script>

<template>
  <!-- /login 页不显示 nav, 直接铺满 -->
  <router-view v-if="isLogin" />

  <div v-else class="layout">
    <div class="nav">
      <div class="nav-inner">
        <!-- 模式切换 -->
        <div class="mode-switch">
          <router-link to="/" class="mode-btn" :class="{ active: !isLitellm }">MUD</router-link>
          <router-link to="/litellm" class="mode-btn" :class="{ active: isLitellm }">LiteLLM</router-link>
        </div>

        <!-- AIGC 模式导航 -->
        <div class="nav-links" v-if="!isLitellm">
          <router-link to="/" :class="{ active: route.path === '/' }">
            <el-icon><HomeFilled /></el-icon> 总览
          </router-link>
          <router-link to="/analysis" :class="{ active: route.path === '/analysis' }">
            <el-icon><TrendCharts /></el-icon> 分析
          </router-link>
          <router-link to="/export" :class="{ active: route.path === '/export' }">
            <el-icon><Download /></el-icon> 导出
          </router-link>
          <router-link to="/points" :class="{ active: route.path === '/points' }">
            <el-icon><Coin /></el-icon> 积分
          </router-link>
          <router-link to="/discounts" :class="{ active: route.path === '/discounts' }">
            <el-icon><Discount /></el-icon> 折扣配置
          </router-link>
          <router-link to="/agent" :class="{ active: route.path === '/agent' }">
            <el-icon><Aim /></el-icon> 巡检
          </router-link>
          <router-link
            v-for="v in vendors"
            :key="v.id"
            :to="`/vendor/${v.id}`"
            :class="{ active: route.path === `/vendor/${v.id}` }"
          >
            <el-icon><DataLine /></el-icon> {{ v.name }}
          </router-link>
        </div>

        <!-- 右侧退出 -->
        <div class="nav-spacer" />
        <el-button link size="small" @click="doLogout" :icon="SwitchButton" class="logout-btn">
          退出
        </el-button>
      </div>
    </div>

    <!-- AIGC 页面走 router-view; LiteLLM 模式始终展示 iframe -->
    <div class="content" v-show="!isLitellm">
      <router-view />
    </div>

    <div class="litellm-iframe-wrapper" v-show="isLitellm">
      <iframe src="/litellm-ui/index.html" class="litellm-iframe" frameborder="0" allowfullscreen />
    </div>
  </div>
</template>

<style>
body {
  margin: 0;
  background: #f5f7fa;
}
.layout {
  min-height: 100vh;
}
.nav {
  background: #fff;
  border-bottom: 1px solid #e8e8e8;
  position: sticky;
  top: 0;
  z-index: 100;
}
.nav-inner {
  max-width: 1400px;
  margin: 0 auto;
  padding: 0 24px;
  height: 56px;
  display: flex;
  align-items: center;
  gap: 24px;
  overflow-x: auto;
  overflow-y: hidden;
  scrollbar-width: thin;
}
.nav-inner::-webkit-scrollbar {
  height: 4px;
}
.nav-inner::-webkit-scrollbar-thumb {
  background: #d9d9d9;
  border-radius: 2px;
}
.mode-switch {
  display: flex;
  background: #f0f0f0;
  border-radius: 8px;
  padding: 3px;
  gap: 2px;
  flex-shrink: 0;
}
.mode-btn {
  padding: 6px 16px;
  border-radius: 6px;
  text-decoration: none;
  font-size: 14px;
  font-weight: 600;
  color: #888;
  transition: all 0.2s;
  white-space: nowrap;
}
.mode-btn.active {
  background: #fff;
  color: #1890ff;
  box-shadow: 0 1px 3px rgba(0,0,0,0.1);
}
.mode-btn:hover:not(.active) {
  color: #333;
}
.nav-links {
  display: flex;
  gap: 8px;
}
.nav-links a {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 8px 16px;
  border-radius: 6px;
  text-decoration: none;
  color: #666;
  font-size: 14px;
  transition: all 0.2s;
  white-space: nowrap;
  flex-shrink: 0;
}
.nav-links a:hover {
  background: #f0f0f0;
  color: #333;
}
.nav-links a.active {
  background: #e6f7ff;
  color: #1890ff;
  font-weight: 500;
}
.nav-spacer { flex: 1; }
.logout-btn {
  flex-shrink: 0;
  color: #888 !important;
}
.logout-btn:hover { color: #f56c6c !important; }
.content {
  max-width: 1400px;
  margin: 0 auto;
  padding: 24px;
}
.litellm-iframe-wrapper {
  position: fixed;
  top: 56px;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 10;
}
.litellm-iframe {
  width: 100%;
  height: 100%;
  border: none;
  display: block;
}
</style>
