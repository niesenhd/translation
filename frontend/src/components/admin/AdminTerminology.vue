<template>
  <section v-if="section === 'terms'">
    <div class="toolbar">
      <div class="toolbar-group">
        <el-input v-model="termSearch.keyword" class="search-input" placeholder="搜索中文术语" clearable @clear="fetchTerms" @keyup.enter="fetchTerms" />
        <el-select v-model="termSearch.lang_pair" class="filter-select" placeholder="语种方向" clearable @change="fetchTerms">
          <el-option v-for="lp in termLangPairs" :key="lp" :label="lp" :value="lp" />
        </el-select>
        <el-select v-model="termSearch.domain" class="filter-select" placeholder="领域" clearable @change="fetchTerms">
          <el-option v-for="domain in termDomains" :key="domain" :label="domain" :value="domain" />
        </el-select>
        <el-button type="primary" @click="fetchTerms">搜索</el-button>
      </div>
      <div class="toolbar-group">
        <el-button type="success" @click="showTermDialog()">添加术语</el-button>
        <el-upload :show-file-list="false" :before-upload="importTerms" accept=".xlsx,.xls,.csv,.sdltb" :disabled="termImporting">
          <el-button type="warning" :loading="termImporting">导入术语</el-button>
        </el-upload>
        <el-button @click="exportTerms">导出 Excel</el-button>
        <el-popconfirm :title="`确定删除选中的 ${selectedTermIds.length} 条术语？`" @confirm="batchDeleteTerms">
          <template #reference>
            <el-button type="danger" :disabled="selectedTermIds.length === 0">批量删除 ({{ selectedTermIds.length }})</el-button>
          </template>
        </el-popconfirm>
      </div>
    </div>

    <el-table :data="terms.items" size="small" stripe @selection-change="onTermSelectionChange" @sort-change="onTermSortChange">
      <el-table-column type="selection" width="42" />
      <el-table-column prop="source_term" label="中文术语" width="200" sortable="custom" />
      <el-table-column prop="target_term" label="目标语言术语" width="200" sortable="custom" />
      <el-table-column prop="lang_pair" label="语种方向" width="100" sortable="custom" />
      <el-table-column prop="domain" label="领域" width="100" sortable="custom" />
      <el-table-column prop="priority" label="优先级" width="80">
        <template #default="{ row }">
          <el-tag :type="row.priority === 'strict' ? 'danger' : 'info'" size="small">
            {{ row.priority === 'strict' ? '强制' : '优先' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="note" label="备注" show-overflow-tooltip />
      <el-table-column prop="created_at" label="添加日期" width="160" sortable="custom">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="120" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="showTermDialog(row)">编辑</el-button>
          <el-popconfirm title="确定删除？" @confirm="deleteTerm(row.id)">
            <template #reference><el-button link type="danger">删除</el-button></template>
          </el-popconfirm>
        </template>
      </el-table-column>
    </el-table>
    <el-pagination class="pagination" v-model:current-page="termPage" :page-size="20" :total="terms.total" layout="total, prev, pager, next" @current-change="fetchTerms" />

    <el-dialog v-model="termDialogVisible" :title="termForm.id ? '编辑术语' : '添加术语'" :width="dialogWidth">
      <el-form :model="termForm" label-width="100px">
        <el-form-item label="中文术语"><el-input v-model="termForm.source_term" /></el-form-item>
        <el-form-item label="目标语言术语"><el-input v-model="termForm.target_term" /></el-form-item>
        <el-form-item label="语种方向">
          <el-select v-model="termForm.lang_pair" class="full-width" placeholder="选择语种方向">
            <el-option v-for="pair in langPairOptions" :key="pair.value" :label="pair.label" :value="pair.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="领域标签"><el-input v-model="termForm.domain" placeholder="如 合同、侵权" /></el-form-item>
        <el-form-item label="优先级">
          <el-radio-group v-model="termForm.priority">
            <el-radio value="strict">强制</el-radio>
            <el-radio value="preferred">优先</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="备注"><el-input v-model="termForm.note" type="textarea" :rows="2" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="termDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="saveTerm">保存</el-button>
      </template>
    </el-dialog>
  </section>

  <section v-else-if="section === 'tm'">
    <div class="toolbar">
      <div class="toolbar-group">
        <el-input v-model="tmSearch.keyword" class="search-input" placeholder="搜索原文" clearable @clear="fetchTm" @keyup.enter="fetchTm" />
        <el-input v-model="tmSearch.lang_pair" class="filter-select" placeholder="语种方向" clearable @clear="fetchTm" />
        <el-button type="primary" @click="fetchTm">搜索</el-button>
      </div>
      <div class="toolbar-group">
        <el-button type="warning" @click="importTmFromTask">从任务导入</el-button>
        <el-button type="success" @click="showTmDialog()">添加记录</el-button>
      </div>
    </div>

    <el-table :data="tm.items" size="small" stripe>
      <el-table-column prop="source_text" label="原文" show-overflow-tooltip />
      <el-table-column prop="target_text" label="译文" show-overflow-tooltip />
      <el-table-column prop="lang_pair" label="语种方向" width="100" />
      <el-table-column prop="source" label="来源" width="80" />
      <el-table-column prop="domain" label="领域" width="100" />
      <el-table-column label="操作" width="120" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="showTmDialog(row)">编辑</el-button>
          <el-popconfirm title="确定删除？" @confirm="deleteTm(row.id)">
            <template #reference><el-button link type="danger">删除</el-button></template>
          </el-popconfirm>
        </template>
      </el-table-column>
    </el-table>
    <el-pagination class="pagination" v-model:current-page="tmPage" :page-size="20" :total="tm.total" layout="total, prev, pager, next" @current-change="fetchTm" />

    <el-dialog v-model="tmDialogVisible" :title="tmForm.id ? '编辑记录' : '添加记录'" :width="dialogWidth">
      <el-form :model="tmForm" label-width="80px">
        <el-form-item label="原文"><el-input v-model="tmForm.source_text" type="textarea" :rows="3" /></el-form-item>
        <el-form-item label="译文"><el-input v-model="tmForm.target_text" type="textarea" :rows="3" /></el-form-item>
        <el-form-item label="语种方向">
          <el-select v-model="tmForm.lang_pair" class="full-width" placeholder="选择语种方向">
            <el-option v-for="pair in langPairOptions" :key="pair.value" :label="pair.label" :value="pair.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="来源"><el-input v-model="tmForm.source" placeholder="manual" /></el-form-item>
        <el-form-item label="领域"><el-input v-model="tmForm.domain" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="tmDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="saveTm">保存</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<script setup>
import { onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../../api'

const props = defineProps({
  section: {
    type: String,
    required: true,
    validator: value => ['terms', 'tm'].includes(value),
  },
  dialogWidth: {
    type: String,
    required: true,
  },
})

const langPairOptions = [
  { value: 'zh→en', label: '中文 → 英文' },
  { value: 'en→zh', label: '英文 → 中文' },
  { value: 'zh→fr', label: '中文 → 法文' },
  { value: 'fr→zh', label: '法文 → 中文' },
  { value: 'zh→es', label: '中文 → 西班牙文' },
  { value: 'es→zh', label: '西班牙文 → 中文' },
  { value: 'zh→ru', label: '中文 → 俄文' },
  { value: 'ru→zh', label: '俄文 → 中文' },
  { value: 'zh→ar', label: '中文 → 阿拉伯文' },
  { value: 'ar→zh', label: '阿拉伯文 → 中文' },
  { value: 'zh→ja', label: '中文 → 日文' },
  { value: 'ja→zh', label: '日文 → 中文' },
  { value: 'zh→ko', label: '中文 → 韩文' },
  { value: 'ko→zh', label: '韩文 → 中文' },
  { value: 'zh→de', label: '中文 → 德文' },
  { value: 'de→zh', label: '德文 → 中文' },
  { value: 'zh→pt', label: '中文 → 葡萄牙文' },
  { value: 'pt→zh', label: '葡萄牙文 → 中文' },
  { value: 'zh→it', label: '中文 → 意大利文' },
  { value: 'it→zh', label: '意大利文 → 中文' },
  { value: 'en→fr', label: '英文 → 法文' },
  { value: 'en→es', label: '英文 → 西班牙文' },
  { value: 'en→ja', label: '英文 → 日文' },
  { value: 'en→ko', label: '英文 → 韩文' },
]

const terms = ref({ items: [], total: 0 })
const termPage = ref(1)
const termSearch = ref({ keyword: '', lang_pair: '', domain: '' })
const termSort = ref({ sort_by: 'updated_at', sort_order: 'desc' })
const selectedTermIds = ref([])
const termLangPairs = ref([])
const termDomains = ref([])
const termDialogVisible = ref(false)
const termImporting = ref(false)
const termForm = ref({ id: '', source_term: '', target_term: '', lang_pair: 'zh→en', domain: '', priority: 'preferred', note: '' })

watch(termSearch, () => { termPage.value = 1 }, { deep: true, flush: 'sync' })

function showError(error, fallback) {
  ElMessage.error(error.response?.data?.detail || fallback)
}

async function fetchTerms() {
  const params = {
    page: termPage.value,
    page_size: 20,
    sort_by: termSort.value.sort_by,
    sort_order: termSort.value.sort_order,
  }
  if (termSearch.value.keyword) params.keyword = termSearch.value.keyword
  if (termSearch.value.lang_pair) params.lang_pair = termSearch.value.lang_pair
  if (termSearch.value.domain) params.domain = termSearch.value.domain
  try {
    const { data } = await api.get('/admin/terms', { params })
    terms.value = data
  } catch (error) {
    showError(error, '获取术语列表失败')
  }
}

function onTermSelectionChange(rows) {
  selectedTermIds.value = rows.map(row => row.id)
}

function onTermSortChange({ prop, order }) {
  if (!prop || !order) {
    termSort.value = { sort_by: 'updated_at', sort_order: 'desc' }
  } else {
    termSort.value = {
      sort_by: prop,
      sort_order: order === 'ascending' ? 'asc' : 'desc',
    }
  }
  termPage.value = 1
  fetchTerms()
}

async function batchDeleteTerms() {
  if (!selectedTermIds.value.length) return
  try {
    const { data } = await api.post('/admin/terms/batch-delete', { ids: selectedTermIds.value })
    ElMessage.success(`已删除 ${data.deleted} 条术语`)
    selectedTermIds.value = []
    fetchTerms()
  } catch (error) {
    showError(error, '批量删除失败')
  }
}

function formatDateTime(value) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const pad = number => String(number).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

async function fetchTermMeta() {
  try {
    const [langPairs, domains] = await Promise.all([
      api.get('/admin/terms/lang-pairs'),
      api.get('/admin/terms/domains'),
    ])
    termLangPairs.value = langPairs.data
    termDomains.value = domains.data
  } catch (error) {
    showError(error, '获取术语筛选项失败')
  }
}

function showTermDialog(row) {
  termForm.value = row
    ? { ...row }
    : { id: '', source_term: '', target_term: '', lang_pair: 'zh→en', domain: '', priority: 'preferred', note: '' }
  termDialogVisible.value = true
}

async function saveTerm() {
  try {
    if (termForm.value.id) {
      await api.put(`/admin/terms/${termForm.value.id}`, termForm.value)
    } else {
      await api.post('/admin/terms', termForm.value)
    }
    ElMessage.success('保存成功')
    termDialogVisible.value = false
    fetchTerms()
    fetchTermMeta()
  } catch (error) {
    showError(error, '保存术语失败')
  }
}

async function deleteTerm(id) {
  try {
    await api.delete(`/admin/terms/${id}`)
    ElMessage.success('已删除')
    fetchTerms()
  } catch (error) {
    showError(error, '删除术语失败')
  }
}

async function importTerms(file) {
  let inputDomain = ''
  try {
    const { value } = await ElMessageBox.prompt(
      '可选填本次导入术语的领域（如：合同、知识产权）。文件中已有领域字段时以文件值优先；留空则不设置领域。',
      '导入术语 - 选填领域',
      {
        confirmButtonText: '开始导入',
        cancelButtonText: '取消',
        inputPlaceholder: '可选，留空则不设置',
        inputValue: '',
      }
    )
    inputDomain = (value || '').trim()
  } catch {
    return false
  }

  const formData = new FormData()
  formData.append('file', file)
  if (inputDomain) formData.append('domain', inputDomain)
  termImporting.value = true
  try {
    const { data } = await api.post('/admin/terms/import', formData, { timeout: 600000 })
    ElMessageBox.alert(
      `成功导入 ${data.imported} 条术语，跳过 ${data.skipped} 条`,
      '导入结果',
      { confirmButtonText: '确定', type: data.imported > 0 ? 'success' : 'warning' }
    )
    fetchTerms()
    fetchTermMeta()
  } catch (error) {
    if (error.response?.status === 413) {
      ElMessage.error('文件太大，超过上传限制（最大 200MB）')
    } else {
      showError(error, '导入失败，请检查文件格式')
    }
  } finally {
    termImporting.value = false
  }
  return false
}

async function exportTerms() {
  try {
    const response = await api.get('/admin/terms/export', { responseType: 'blob' })
    const url = URL.createObjectURL(response.data)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = 'terms.xlsx'
    anchor.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    showError(error, '导出术语失败')
  }
}

const tm = ref({ items: [], total: 0 })
const tmPage = ref(1)
const tmSearch = ref({ keyword: '', lang_pair: '' })
const tmDialogVisible = ref(false)
const tmForm = ref({ id: '', source_text: '', target_text: '', lang_pair: 'zh→en', source: 'manual', domain: '' })

watch(tmSearch, () => { tmPage.value = 1 }, { deep: true, flush: 'sync' })

async function fetchTm() {
  const params = { page: tmPage.value, page_size: 20 }
  if (tmSearch.value.keyword) params.keyword = tmSearch.value.keyword
  if (tmSearch.value.lang_pair) params.lang_pair = tmSearch.value.lang_pair
  try {
    const { data } = await api.get('/admin/tm', { params })
    tm.value = data
  } catch (error) {
    showError(error, '获取翻译记忆失败')
  }
}

async function importTmFromTask() {
  let taskId
  try {
    const result = await ElMessageBox.prompt('输入已完成任务的 ID（任务列表可复制）', '从任务导入 TM', {
      confirmButtonText: '导入',
      cancelButtonText: '取消',
      inputPlaceholder: '任务 UUID',
    })
    taskId = result.value?.trim()
  } catch {
    return
  }
  if (!taskId) return
  try {
    const { data } = await api.post(`/admin/tm/import-from-task/${taskId}`)
    ElMessage.success(`导入完成：新增/更新 ${data.imported} 条，跳过 ${data.skipped} 条`)
    fetchTm()
  } catch (error) {
    showError(error, '导入失败')
  }
}

function showTmDialog(row) {
  tmForm.value = row
    ? { ...row }
    : { id: '', source_text: '', target_text: '', lang_pair: 'zh→en', source: 'manual', domain: '' }
  tmDialogVisible.value = true
}

async function saveTm() {
  try {
    if (tmForm.value.id) {
      await api.put(`/admin/tm/${tmForm.value.id}`, tmForm.value)
    } else {
      await api.post('/admin/tm', tmForm.value)
    }
    ElMessage.success('保存成功')
    tmDialogVisible.value = false
    fetchTm()
  } catch (error) {
    showError(error, '保存翻译记忆失败')
  }
}

async function deleteTm(id) {
  try {
    await api.delete(`/admin/tm/${id}`)
    ElMessage.success('已删除')
    fetchTm()
  } catch (error) {
    showError(error, '删除翻译记忆失败')
  }
}

onMounted(() => {
  if (props.section === 'terms') {
    fetchTerms()
    fetchTermMeta()
  } else {
    fetchTm()
  }
})
</script>

<style scoped>
.toolbar {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 16px;
}

.toolbar-group {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.search-input {
  width: 200px;
}

.filter-select {
  width: 140px;
}

.full-width {
  width: 100%;
}

.pagination {
  justify-content: center;
  margin-top: 16px;
}

@media (max-width: 900px) {
  .toolbar {
    flex-direction: column;
  }
}
</style>
