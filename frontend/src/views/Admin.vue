<template>
  <div class="admin-page">
    <el-tabs v-model="activeTab" type="border-card">
      <el-tab-pane label="统计看板" name="stats">
        <AdminStats />
      </el-tab-pane>
      <el-tab-pane label="术语库" name="terms">
        <AdminTerminology section="terms" :dialog-width="dialogWidth" />
      </el-tab-pane>
      <el-tab-pane label="翻译记忆库" name="tm">
        <AdminTerminology section="tm" :dialog-width="dialogWidth" />
      </el-tab-pane>
      <el-tab-pane label="质量反馈" name="feedback">
        <AdminFeedback />
      </el-tab-pane>
      <el-tab-pane label="模型配置" name="model">
        <AdminModels section="model" :dialog-width="dialogWidth" />
      </el-tab-pane>
      <el-tab-pane label="用户管理" name="users">
        <AdminUsers :dialog-width="dialogWidth" />
      </el-tab-pane>
      <el-tab-pane label="系统设置" name="settings">
        <AdminModels section="settings" :dialog-width="dialogWidth" />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, ref } from 'vue'
import AdminFeedback from '../components/admin/AdminFeedback.vue'
import AdminModels from '../components/admin/AdminModels.vue'
import AdminStats from '../components/admin/AdminStats.vue'
import AdminTerminology from '../components/admin/AdminTerminology.vue'
import AdminUsers from '../components/admin/AdminUsers.vue'

const activeTab = ref('stats')

const winWidth = ref(window.innerWidth)
const dialogWidth = computed(() => winWidth.value < 768 ? '90%' : '550px')
const handleResize = () => { winWidth.value = window.innerWidth }
window.addEventListener('resize', handleResize)

onBeforeUnmount(() => window.removeEventListener('resize', handleResize))
</script>

<style scoped>
.admin-page {
  min-width: 0;
}
</style>
