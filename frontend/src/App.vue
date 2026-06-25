<template>
  <el-container style="height: 100vh">
    <el-header style="display: flex; align-items: center; justify-content: space-between; background: #1f2d3d; color: #fff">
      <div style="font-size: 18px; font-weight: 600">法律文档翻译系统</div>
      <div style="display: flex; align-items: center; gap: 12px">
        <router-link v-if="auth.token && auth.username === 'admin'" to="/admin">
          <el-button size="small" type="warning">管理后台</el-button>
        </router-link>
        <router-link v-if="auth.token && $route.path === '/admin'" to="/">
          <el-button size="small">返回任务</el-button>
        </router-link>
        <el-tag v-if="auth.token" type="success">已登录: {{ auth.username }}</el-tag>
        <el-button v-if="auth.token" size="small" @click="auth.logout()">退出</el-button>
      </div>
    </el-header>
    <el-main>
      <router-view />
    </el-main>
  </el-container>
</template>

<script setup>
import { useAuthStore } from './stores/auth'
const auth = useAuthStore()
</script>
