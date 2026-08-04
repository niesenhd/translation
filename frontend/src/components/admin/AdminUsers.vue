<template>
  <div class="toolbar">
    <el-input v-model="userSearch" class="search-input" placeholder="搜索姓名/登录名/邮箱/手机/部门/主管合伙人" clearable @clear="fetchUsers" @keyup.enter="fetchUsers" />
    <el-button @click="fetchUsers">搜索</el-button>
    <el-select v-model="userStatusFilter" class="status-filter" @change="fetchUsers">
      <el-option label="全部" value="" />
      <el-option label="在职" value="active" />
      <el-option label="已离职" value="inactive" />
    </el-select>
    <el-button type="primary" @click="showUserDialog">+ 添加用户</el-button>
    <el-button type="warning" :loading="syncing" @click="syncOA">同步OA用户</el-button>
    <span class="muted count">共 {{ userTotal }} 人</span>
  </div>

  <el-table :data="users" v-loading="userLoading" stripe size="small">
    <el-table-column prop="display_name" label="姓名" width="120" />
    <el-table-column prop="username" label="登录名" width="150" />
    <el-table-column prop="department" label="部门" width="150" />
    <el-table-column prop="partner_name" label="主管合伙人" width="120" />
    <el-table-column prop="email" label="邮箱" min-width="180" />
    <el-table-column prop="phone" label="手机" width="130" />
    <el-table-column label="管理员" width="80">
      <template #default="{ row }">
        <el-tag v-if="row.is_admin" size="small" type="danger">管理员</el-tag>
        <span v-else class="muted">-</span>
      </template>
    </el-table-column>
    <el-table-column label="来源" width="80">
      <template #default="{ row }">
        <el-tag v-if="row.oa_id" size="small" type="info">OA同步</el-tag>
        <span v-else class="muted">手动</span>
      </template>
    </el-table-column>
    <el-table-column label="在职状态" width="90">
      <template #default="{ row }">
        <el-tag v-if="row.oa_employed" size="small" type="success">在职</el-tag>
        <el-tag v-else size="small" type="info">离职</el-tag>
      </template>
    </el-table-column>
    <el-table-column label="启用" width="80">
      <template #default="{ row }">
        <el-switch
          :model-value="row.is_active"
          @change="value => toggleUserActive(row, value)"
        />
      </template>
    </el-table-column>
  </el-table>

  <div v-if="userTotal > userPageSize" class="pagination">
    <el-pagination
      v-model:current-page="userPage"
      :page-size="userPageSize"
      :total="userTotal"
      layout="prev, pager, next"
      @current-change="fetchUsers"
    />
  </div>

  <el-dialog v-model="userDialogVisible" title="添加用户" :width="dialogWidth">
    <el-form :model="userForm" label-width="80px">
      <el-form-item label="登录名"><el-input v-model="userForm.username" placeholder="登录用用户名" /></el-form-item>
      <el-form-item label="密码"><el-input v-model="userForm.password" type="password" show-password placeholder="初始密码" /></el-form-item>
      <el-form-item label="姓名"><el-input v-model="userForm.display_name" placeholder="显示名（可选）" /></el-form-item>
      <el-form-item label="管理员">
        <el-switch v-model="userForm.is_admin" />
        <span class="admin-hint">管理员可查看所有用户数据和系统配置</span>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="userDialogVisible = false">取消</el-button>
      <el-button type="primary" @click="saveUser">保存</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../../api'
import { persistBooleanToggle } from '../../toggle'

defineProps({
  dialogWidth: {
    type: String,
    required: true,
  },
})

const users = ref([])
const userLoading = ref(false)
const userSearch = ref('')
const userStatusFilter = ref('')
const userPage = ref(1)
const userPageSize = 50
const userTotal = ref(0)
const userDialogVisible = ref(false)
const syncing = ref(false)
const userForm = reactive({ username: '', password: '', display_name: '', is_admin: false })

watch([userSearch, userStatusFilter], () => { userPage.value = 1 }, { flush: 'sync' })

function showError(error, fallback) {
  ElMessage.error(error.response?.data?.detail || fallback)
}

async function fetchUsers() {
  userLoading.value = true
  try {
    const params = { page: userPage.value, page_size: userPageSize }
    if (userSearch.value) params.keyword = userSearch.value
    if (userStatusFilter.value) params.status = userStatusFilter.value
    const { data } = await api.get('/admin/users', { params })
    users.value = data
    const { data: countData } = await api.get('/admin/users/count', {
      params: { keyword: userSearch.value || '', status: userStatusFilter.value },
    })
    userTotal.value = countData.total
  } catch (error) {
    showError(error, '获取用户列表失败')
  } finally {
    userLoading.value = false
  }
}

function showUserDialog() {
  userForm.username = ''
  userForm.password = ''
  userForm.display_name = ''
  userForm.is_admin = false
  userDialogVisible.value = true
}

async function saveUser() {
  if (!userForm.username || !userForm.password) {
    ElMessage.warning('请填写用户名和密码')
    return
  }
  try {
    await api.post('/admin/users', userForm)
    ElMessage.success('用户创建成功')
    userDialogVisible.value = false
    fetchUsers()
  } catch (error) {
    showError(error, '创建失败')
  }
}

async function toggleUserActive(row, value) {
  try {
    row.is_active = value
    await persistBooleanToggle(
      row,
      'is_active',
      nextValue => api.put(`/admin/users/${row.id}/active`, { is_active: nextValue }),
    )
    ElMessage.success(value ? '已启用' : '已停用')
  } catch (error) {
    showError(error, '操作失败')
  }
}

async function syncOA() {
  syncing.value = true
  try {
    const { data } = await api.post('/admin/users/sync-oa')
    const summary = `同步完成：共 ${data.synced} 人，新增 ${data.created}，更新 ${data.updated}，状态变动 ${data.employed_changed || 0}`
    const conflicts = data.conflicts || 0
    if (conflicts > 0) {
      const names = (data.conflict_usernames || []).slice(0, 5)
      const nameHint = names.length ? `（${names.join('、')}${conflicts > names.length ? ' 等' : ''}）` : ''
      ElMessage.warning(`${summary}；同名本地账号未覆盖 ${conflicts} 人${nameHint}`)
    } else {
      ElMessage.success(summary)
    }
    fetchUsers()
  } catch (error) {
    showError(error, '同步失败')
  } finally {
    syncing.value = false
  }
}

onMounted(fetchUsers)
</script>

<style scoped>
.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.search-input {
  width: 320px;
}

.status-filter {
  width: 100px;
}

.muted {
  color: #909399;
}

.count {
  font-size: 13px;
}

.pagination {
  margin-top: 12px;
  text-align: right;
}

.admin-hint {
  margin-left: 10px;
  color: #909399;
  font-size: 12px;
}
</style>
