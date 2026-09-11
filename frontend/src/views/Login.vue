<script setup>
defineOptions({ name: 'Login' })
import { ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { login } from '../api'

const router = useRouter()
const route = useRoute()
const password = ref('')
const loading = ref(false)

async function submit() {
  if (!password.value) {
    ElMessage.warning('请输入密码')
    return
  }
  loading.value = true
  try {
    await login(password.value)
    ElMessage.success('登录成功')
    // 如果有 redirect 参数, 跳回原页; 否则跳 /
    const redirect = route.query.redirect || '/'
    router.replace(redirect)
  } catch (e) {
    const msg = e.response?.data?.detail || e.message || '登录失败'
    ElMessage.error(msg)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <h2>MUD</h2>
      <p class="hint">请输入登录密码</p>
      <el-input v-model="password" type="password" placeholder="密码"
                show-password size="large" @keyup.enter="submit"
                style="margin-bottom: 12px" />
      <el-button type="primary" size="large" :loading="loading"
                 @click="submit" style="width: 100%">
        登录
      </el-button>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #f5f7fa;
}
.login-card {
  width: 360px;
  padding: 40px 36px;
  background: #fff;
  border-radius: 8px;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.08);
}
h2 { margin: 0 0 8px; font-size: 22px; color: #1a1a1a; text-align: center; }
.hint { color: #888; font-size: 13px; text-align: center; margin: 0 0 24px; }
</style>
