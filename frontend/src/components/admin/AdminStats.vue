<template>
  <el-row :gutter="20" class="stats-row">
    <el-col :xs="12" :sm="8" :md="4"><el-statistic title="总任务数" :value="stats.total_tasks" /></el-col>
    <el-col :xs="12" :sm="8" :md="4"><el-statistic title="成功" :value="stats.succeeded_tasks" /></el-col>
    <el-col :xs="12" :sm="8" :md="4"><el-statistic title="失败" :value="stats.failed_tasks" /></el-col>
    <el-col :xs="12" :sm="8" :md="4"><el-statistic title="运行中" :value="stats.running_tasks" /></el-col>
    <el-col :xs="12" :sm="8" :md="4"><el-statistic title="排队中" :value="stats.queued_tasks" /></el-col>
    <el-col :xs="12" :sm="8" :md="4"><el-statistic title="用户数" :value="stats.total_users" /></el-col>
  </el-row>
  <el-row :gutter="20">
    <el-col :span="12">
      <h4>最近 7 天翻译量</h4>
      <el-table :data="stats.daily_trend" size="small" stripe>
        <el-table-column prop="date" label="日期" />
        <el-table-column prop="count" label="任务数" />
      </el-table>
    </el-col>
    <el-col :span="12">
      <h4>文件类型分布</h4>
      <el-table :data="fileTypeRows" size="small" stripe>
        <el-table-column prop="type" label="类型" />
        <el-table-column prop="count" label="数量" />
      </el-table>
      <div v-if="stats.avg_duration_seconds" class="average-duration">
        <el-tag type="info">平均耗时: {{ formatDuration(stats.avg_duration_seconds) }}</el-tag>
      </div>
    </el-col>
  </el-row>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../../api'

const stats = ref({
  total_tasks: 0, succeeded_tasks: 0, failed_tasks: 0,
  running_tasks: 0, queued_tasks: 0, total_users: 0,
  file_type_distribution: {}, daily_trend: [], avg_duration_seconds: null,
})

const fileTypeRows = computed(() =>
  Object.entries(stats.value.file_type_distribution || {}).map(([type, count]) => ({ type, count }))
)

function formatDuration(seconds) {
  if (!seconds) return '-'
  if (seconds < 60) return `${Math.round(seconds)}秒`
  if (seconds < 3600) return `${Math.round(seconds / 60)}分${Math.round(seconds % 60)}秒`
  return `${Math.round(seconds / 3600)}时${Math.round((seconds % 3600) / 60)}分`
}

async function fetchStats() {
  try {
    const { data } = await api.get('/admin/stats')
    stats.value = data
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '获取统计数据失败')
  }
}

onMounted(fetchStats)
</script>

<style scoped>
.stats-row {
  margin-bottom: 20px;
}

.average-duration {
  margin-top: 16px;
}
</style>
