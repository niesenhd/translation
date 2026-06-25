<template>
  <div style="max-width: 420px; margin: 80px auto">
    <el-card>
      <h2 style="margin-top: 0">登录</h2>
      <el-form @submit.prevent="onSubmit">
        <el-form-item label="用户名">
          <el-input v-model="username" placeholder="admin" />
        </el-form-item>
        <el-form-item label="Token">
          <el-input v-model="token" type="password" placeholder="管理员 Token（admin-dev-token）" show-password />
        </el-form-item>
        <el-button type="primary" @click="onSubmit">进入系统</el-button>
      </el-form>
      <el-alert
        type="info"
        :closable="false"
        style="margin-top: 16px"
        title="P0 阶段使用静态 admin Token，P3 阶段切换为律智荟 OA 跳转登录"
      />
    </el-card>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()
const username = ref('admin')
const token = ref('')

function onSubmit() {
  if (!token.value) {
    ElMessage.warning('请输入 Token')
    return
  }
  auth.login(token.value, username.value || 'admin')
  router.push('/')
}
</script>
