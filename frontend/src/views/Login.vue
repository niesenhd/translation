<template>
  <div style="max-width: 420px; margin: 80px auto">
    <el-card>
      <h2 style="margin-top: 0">登录</h2>
      <el-form @submit.prevent="onSubmit">
        <el-form-item label="用户名">
          <el-input v-model="username" placeholder="请输入用户名" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input v-model="password" type="password" placeholder="请输入密码" show-password @keyup.enter="onSubmit" />
        </el-form-item>
        <el-button type="primary" :loading="loading" @click="onSubmit">进入系统</el-button>
      </el-form>
      <el-alert
        v-if="errorMsg"
        type="error"
        :closable="false"
        style="margin-top: 16px"
        :title="errorMsg"
      />
    </el-card>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import api from '../api'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()
const username = ref('')
const password = ref('')
const loading = ref(false)
const errorMsg = ref('')

async function onSubmit() {
  errorMsg.value = ''
  if (!username.value || !password.value) {
    errorMsg.value = '请输入用户名和密码'
    return
  }
  loading.value = true
  try {
    const { data } = await api.post('/auth/login', { username: username.value, password: password.value })
    auth.login(data)
    ElMessage.success('登录成功')
    router.push('/')
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || '登录失败，请检查用户名和密码'
  } finally {
    loading.value = false
  }
}
</script>
