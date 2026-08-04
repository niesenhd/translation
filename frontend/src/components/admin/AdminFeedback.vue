<template>
  <div class="filters">
    <el-select v-model="feedbackFilter.status" class="status-filter" placeholder="状态" clearable @change="fetchFeedback">
      <el-option label="待处理" value="pending" />
      <el-option label="已采纳" value="adopted" />
      <el-option label="已驳回" value="rejected" />
    </el-select>
    <el-select v-model="feedbackFilter.type" class="type-filter" placeholder="问题类型" clearable @change="fetchFeedback">
      <el-option label="术语错误" value="terminology" />
      <el-option label="语法问题" value="grammar" />
      <el-option label="格式错乱" value="format" />
      <el-option label="漏译" value="omission" />
      <el-option label="其他" value="other" />
    </el-select>
  </div>

  <el-table :data="feedback.items" size="small" stripe>
    <el-table-column prop="original_filename" label="文件" width="180" show-overflow-tooltip />
    <el-table-column prop="username" label="用户" width="100" />
    <el-table-column prop="rating" label="评分" width="80">
      <template #default="{ row }">
        <el-rate v-if="row.rating" v-model="row.rating" disabled />
        <span v-else>-</span>
      </template>
    </el-table-column>
    <el-table-column prop="feedback_type" label="类型" width="100" />
    <el-table-column prop="suggestion" label="建议" show-overflow-tooltip />
    <el-table-column prop="status" label="状态" width="80">
      <template #default="{ row }">
        <el-tag :type="row.status === 'pending' ? 'warning' : row.status === 'adopted' ? 'success' : 'danger'" size="small">
          {{ row.status === 'pending' ? '待处理' : row.status === 'adopted' ? '已采纳' : '已驳回' }}
        </el-tag>
      </template>
    </el-table-column>
    <el-table-column label="操作" width="160" fixed="right">
      <template #default="{ row }">
        <template v-if="row.status === 'pending'">
          <el-button link type="success" @click="reviewFeedback(row.id, 'adopted')">采纳</el-button>
          <el-button link type="danger" @click="rejectFeedback(row.id)">驳回</el-button>
        </template>
        <span v-else class="reviewer">{{ row.reviewed_by }}</span>
      </template>
    </el-table-column>
  </el-table>
  <el-pagination class="pagination" v-model:current-page="feedbackPage" :page-size="20" :total="feedback.total" layout="total, prev, pager, next" @current-change="fetchFeedback" />
</template>

<script setup>
import { onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../../api'

const feedback = ref({ items: [], total: 0 })
const feedbackPage = ref(1)
const feedbackFilter = ref({ status: '', type: '' })

watch(feedbackFilter, () => { feedbackPage.value = 1 }, { deep: true, flush: 'sync' })

function showError(error, fallback) {
  ElMessage.error(error.response?.data?.detail || fallback)
}

async function fetchFeedback() {
  const params = { page: feedbackPage.value, page_size: 20 }
  if (feedbackFilter.value.status) params.status = feedbackFilter.value.status
  if (feedbackFilter.value.type) params.feedback_type = feedbackFilter.value.type
  try {
    const { data } = await api.get('/feedback', { params })
    feedback.value = data
  } catch (error) {
    showError(error, '获取反馈列表失败')
  }
}

async function reviewFeedback(id, status) {
  try {
    await api.put(`/feedback/${id}`, { status })
    ElMessage.success('操作成功')
    fetchFeedback()
  } catch (error) {
    showError(error, '审核反馈失败')
  }
}

async function rejectFeedback(id) {
  let reason = ''
  try {
    const result = await ElMessageBox.prompt('请输入驳回原因（可选）', '驳回反馈', {
      confirmButtonText: '确定驳回',
      cancelButtonText: '取消',
      inputPlaceholder: '驳回原因',
    })
    reason = result.value || ''
  } catch {
    return
  }
  try {
    await api.put(`/feedback/${id}`, { status: 'rejected', reject_reason: reason })
    ElMessage.success('已驳回')
    fetchFeedback()
  } catch (error) {
    showError(error, '驳回反馈失败')
  }
}

onMounted(fetchFeedback)
</script>

<style scoped>
.filters {
  display: flex;
  gap: 8px;
  margin-bottom: 16px;
}

.status-filter {
  width: 120px;
}

.type-filter {
  width: 140px;
}

.reviewer {
  color: #999;
}

.pagination {
  justify-content: center;
  margin-top: 16px;
}
</style>
