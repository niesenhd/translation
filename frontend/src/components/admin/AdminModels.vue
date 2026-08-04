<template>
  <section v-if="section === 'model'">
    <div class="model-actions">
      <el-button type="success" @click="showModelDialog()">添加模型</el-button>
    </div>
    <el-table :data="modelList" size="small" stripe>
      <el-table-column prop="name" label="名称" width="150" />
      <el-table-column prop="model_type" label="类型" width="120">
        <template #default="{ row }">
          <el-tag :type="row.model_type === 'translation' ? 'primary' : 'success'" size="small">
            {{ row.model_type === 'translation' ? '文本翻译' : 'OCR识别' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="model_id" label="模型 ID" width="200" />
      <el-table-column prop="api_base_url" label="API 地址" show-overflow-tooltip />
      <el-table-column prop="is_active" label="状态" width="100">
        <template #default="{ row }">
          <el-switch v-model="row.is_active" @change="toggleModelActive(row)" />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="120" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="showModelDialog(row)">编辑</el-button>
          <el-popconfirm title="确定删除此模型配置？" @confirm="deleteModel(row.id)">
            <template #reference><el-button link type="danger">删除</el-button></template>
          </el-popconfirm>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="modelDialogVisible" :title="modelForm.id ? '编辑模型' : '添加模型'" :width="dialogWidth">
      <el-form :model="modelForm" label-width="110px">
        <el-form-item label="模型名称">
          <el-input v-model="modelForm.name" placeholder="如：Qwen-Plus 翻译模型" />
        </el-form-item>
        <el-form-item label="模型类型">
          <el-select v-model="modelForm.model_type" class="full-width">
            <el-option label="文本翻译模型" value="translation" />
            <el-option label="OCR 识别模型" value="vl" />
          </el-select>
        </el-form-item>
        <el-form-item label="模型 ID">
          <el-select
            v-if="modelForm.model_type === 'vl'"
            v-model="modelForm.model_id"
            class="full-width"
            filterable
            allow-create
            default-first-option
            placeholder="选择或输入模型 ID"
          >
            <el-option label="PaddleOCR + PP-Structure (推荐，离线运行)" value="paddleocr" />
            <el-option label="qwen-vl-max (备选，需API)" value="qwen-vl-max" />
            <el-option label="qwen-vl-plus (备选，需API)" value="qwen-vl-plus" />
            <el-option label="qwen2.5-vl-72b-instruct (备选，需私有化部署)" value="qwen2.5-vl-72b-instruct" />
            <el-option label="qwen2.5-vl-7b-instruct (备选，需私有化部署)" value="qwen2.5-vl-7b-instruct" />
          </el-select>
          <el-select
            v-else
            v-model="modelForm.model_id"
            class="full-width"
            filterable
            allow-create
            default-first-option
            placeholder="选择或输入模型 ID"
          >
            <el-option label="qwen-plus" value="qwen-plus" />
            <el-option label="qwen-turbo" value="qwen-turbo" />
            <el-option label="qwen-max" value="qwen-max" />
            <el-option label="qwen3-235b-a22b" value="qwen3-235b-a22b" />
            <el-option label="qwen3-30b-a3b" value="qwen3-30b-a3b" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="modelForm.model_id !== 'paddleocr'" label="API 地址">
          <el-input v-model="modelForm.api_base_url" placeholder="如 https://dashscope.aliyuncs.com/compatible-mode/v1" />
        </el-form-item>
        <el-form-item v-if="modelForm.model_id !== 'paddleocr'" label="API Key">
          <el-input v-model="modelForm.api_key" type="password" show-password :placeholder="modelForm.id ? '留空不变' : '请输入 API Key'" />
        </el-form-item>
        <el-form-item label="设为当前使用">
          <el-switch v-model="modelForm.is_active" />
          <span class="form-hint">同类型只能激活一个</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="modelDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="saveModel">保存</el-button>
      </template>
    </el-dialog>
  </section>

  <section v-else-if="section === 'settings'">
    <el-form class="settings-form" label-width="140px">
      <el-form-item label="并发上限">
        <div class="setting-row">
          <el-input-number v-model="concurrency.max_concurrency" :min="1" :max="20" />
          <el-tag>当前运行: {{ concurrency.current_running }}</el-tag>
          <el-button type="primary" size="small" @click="saveConcurrency">保存</el-button>
        </div>
      </el-form-item>
      <el-form-item label="文件保留天数">
        <div class="setting-row">
          <el-input-number v-model="retentionDays" :min="0" :max="3650" />
          <span class="form-hint no-margin">0=永不过期</span>
          <el-button type="primary" size="small" @click="saveRetention">保存</el-button>
        </div>
      </el-form-item>
      <el-form-item label="手动清理">
        <el-button type="warning" @click="triggerCleanup">立即清理过期文件</el-button>
      </el-form-item>
    </el-form>
  </section>
</template>

<script setup>
import { onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../../api'
import { persistBooleanToggle } from '../../toggle'

const props = defineProps({
  section: {
    type: String,
    required: true,
    validator: value => ['model', 'settings'].includes(value),
  },
  dialogWidth: {
    type: String,
    required: true,
  },
})

function showError(error, fallback) {
  ElMessage.error(error.response?.data?.detail || fallback)
}

const modelList = ref([])
const modelDialogVisible = ref(false)
const modelForm = ref({ id: '', name: '', model_type: 'translation', model_id: '', api_base_url: '', api_key: '', is_active: false })

async function fetchModels() {
  try {
    const { data } = await api.get('/admin/models')
    modelList.value = data
  } catch (error) {
    showError(error, '获取模型配置失败')
  }
}

function showModelDialog(row) {
  modelForm.value = row
    ? { ...row, api_key: '' }
    : { id: '', name: '', model_type: 'translation', model_id: '', api_base_url: '', api_key: '', is_active: false }
  modelDialogVisible.value = true
}

watch(() => modelForm.value.model_id, value => {
  if (value === 'paddleocr') {
    modelForm.value.api_base_url = 'local'
    modelForm.value.api_key = 'local'
  }
})

async function saveModel() {
  try {
    const payload = { ...modelForm.value }
    if (!payload.api_key) delete payload.api_key
    if (payload.id) {
      await api.put(`/admin/models/${payload.id}`, payload)
    } else {
      await api.post('/admin/models', payload)
    }
    ElMessage.success('保存成功')
    modelDialogVisible.value = false
    fetchModels()
  } catch (error) {
    showError(error, '保存模型配置失败')
  }
}

async function deleteModel(id) {
  try {
    await api.delete(`/admin/models/${id}`)
    ElMessage.success('已删除')
    fetchModels()
  } catch (error) {
    showError(error, '删除模型配置失败')
  }
}

async function toggleModelActive(row) {
  try {
    const nextValue = await persistBooleanToggle(
      row,
      'is_active',
      value => api.put(`/admin/models/${row.id}`, { is_active: value }),
    )
    ElMessage.success(nextValue ? '已激活' : '已停用')
    fetchModels()
  } catch (error) {
    showError(error, '模型状态更新失败')
  }
}

const concurrency = ref({ max_concurrency: 4, current_running: 0 })
const retentionDays = ref(180)

async function fetchConcurrency() {
  try {
    const { data } = await api.get('/admin/concurrency')
    concurrency.value = data
  } catch (error) {
    showError(error, '获取并发设置失败')
  }
}

async function saveConcurrency() {
  try {
    await api.put('/admin/concurrency', { max_concurrency: concurrency.value.max_concurrency })
    ElMessage.success('并发上限已更新')
    fetchConcurrency()
  } catch (error) {
    showError(error, '更新并发上限失败')
  }
}

async function fetchRetention() {
  try {
    const { data } = await api.get('/admin/retention')
    retentionDays.value = data.days
  } catch (error) {
    showError(error, '获取保留策略失败')
  }
}

async function saveRetention() {
  try {
    await api.put('/admin/retention', { days: retentionDays.value })
    ElMessage.success('保留天数已更新')
  } catch (error) {
    showError(error, '更新保留策略失败')
  }
}

async function triggerCleanup() {
  try {
    const { data } = await api.post('/admin/cleanup')
    ElMessage.success(`清理完成：扫描 ${data.scanned}，删除 ${data.deleted}，失败 ${data.failed}`)
  } catch (error) {
    showError(error, '清理过期文件失败')
  }
}

onMounted(() => {
  if (props.section === 'model') {
    fetchModels()
  } else {
    fetchConcurrency()
    fetchRetention()
  }
})
</script>

<style scoped>
.model-actions {
  display: flex;
  gap: 8px;
  margin-bottom: 16px;
}

.full-width {
  width: 100%;
}

.form-hint {
  margin-left: 8px;
  color: #999;
  font-size: 12px;
}

.form-hint.no-margin {
  margin-left: 0;
}

.settings-form {
  max-width: 600px;
}

.setting-row {
  display: flex;
  align-items: center;
  gap: 12px;
}
</style>
