<template>
  <div>
    <el-tabs v-model="activeTab" type="border-card">
      <!-- 统计看板 -->
      <el-tab-pane label="统计看板" name="stats">
        <el-row :gutter="20" style="margin-bottom: 20px">
          <el-col :span="4"><el-statistic title="总任务数" :value="stats.total_tasks" /></el-col>
          <el-col :span="4"><el-statistic title="成功" :value="stats.succeeded_tasks" /></el-col>
          <el-col :span="4"><el-statistic title="失败" :value="stats.failed_tasks" /></el-col>
          <el-col :span="4"><el-statistic title="运行中" :value="stats.running_tasks" /></el-col>
          <el-col :span="4"><el-statistic title="排队中" :value="stats.queued_tasks" /></el-col>
          <el-col :span="4"><el-statistic title="用户数" :value="stats.total_users" /></el-col>
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
            <div style="margin-top: 16px" v-if="stats.avg_duration_seconds">
              <el-tag type="info">平均耗时: {{ formatDuration(stats.avg_duration_seconds) }}</el-tag>
            </div>
          </el-col>
        </el-row>
      </el-tab-pane>

      <!-- 术语库管理 -->
      <el-tab-pane label="术语库" name="terms">
        <div style="display: flex; justify-content: space-between; margin-bottom: 16px">
          <div style="display: flex; gap: 8px">
            <el-input v-model="termSearch.keyword" placeholder="搜索中文术语" clearable style="width: 200px" @clear="fetchTerms" @keyup.enter="fetchTerms" />
            <el-select v-model="termSearch.lang_pair" placeholder="语种方向" clearable style="width: 140px" @change="fetchTerms">
              <el-option v-for="lp in termLangPairs" :key="lp" :label="lp" :value="lp" />
            </el-select>
            <el-select v-model="termSearch.domain" placeholder="领域" clearable style="width: 140px" @change="fetchTerms">
              <el-option v-for="d in termDomains" :key="d" :label="d" :value="d" />
            </el-select>
            <el-button type="primary" @click="fetchTerms">搜索</el-button>
          </div>
          <div style="display: flex; gap: 8px">
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
        <el-pagination style="margin-top: 16px; justify-content: center" v-model:current-page="termPage" :page-size="20" :total="terms.total" layout="total, prev, pager, next" @current-change="fetchTerms" />

        <!-- 术语编辑对话框 -->
        <el-dialog v-model="termDialogVisible" :title="termForm.id ? '编辑术语' : '添加术语'" width="500px">
          <el-form :model="termForm" label-width="100px">
            <el-form-item label="中文术语"><el-input v-model="termForm.source_term" /></el-form-item>
            <el-form-item label="目标语言术语"><el-input v-model="termForm.target_term" /></el-form-item>
            <el-form-item label="语种方向">
              <el-select v-model="termForm.lang_pair" placeholder="选择语种方向" style="width: 100%">
                <el-option v-for="lp in langPairOptions" :key="lp.value" :label="lp.label" :value="lp.value" />
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
      </el-tab-pane>

      <!-- 翻译记忆库 -->
      <el-tab-pane label="翻译记忆库" name="tm">
        <div style="display: flex; justify-content: space-between; margin-bottom: 16px">
          <div style="display: flex; gap: 8px">
            <el-input v-model="tmSearch.keyword" placeholder="搜索原文" clearable style="width: 200px" @clear="fetchTm" @keyup.enter="fetchTm" />
            <el-input v-model="tmSearch.lang_pair" placeholder="语种方向" clearable style="width: 140px" @clear="fetchTm" />
            <el-button type="primary" @click="fetchTm">搜索</el-button>
          </div>
          <div style="display: flex; gap: 8px">
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
        <el-pagination style="margin-top: 16px; justify-content: center" v-model:current-page="tmPage" :page-size="20" :total="tm.total" layout="total, prev, pager, next" @current-change="fetchTm" />

        <!-- TM 编辑对话框 -->
        <el-dialog v-model="tmDialogVisible" :title="tmForm.id ? '编辑记录' : '添加记录'" width="500px">
          <el-form :model="tmForm" label-width="80px">
            <el-form-item label="原文"><el-input v-model="tmForm.source_text" type="textarea" :rows="3" /></el-form-item>
            <el-form-item label="译文"><el-input v-model="tmForm.target_text" type="textarea" :rows="3" /></el-form-item>
            <el-form-item label="语种方向">
              <el-select v-model="tmForm.lang_pair" placeholder="选择语种方向" style="width: 100%">
                <el-option v-for="lp in langPairOptions" :key="lp.value" :label="lp.label" :value="lp.value" />
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
      </el-tab-pane>

      <!-- 质量反馈 -->
      <el-tab-pane label="质量反馈" name="feedback">
        <div style="display: flex; gap: 8px; margin-bottom: 16px">
          <el-select v-model="feedbackFilter.status" placeholder="状态" clearable style="width: 120px" @change="fetchFeedback">
            <el-option label="待处理" value="pending" />
            <el-option label="已采纳" value="adopted" />
            <el-option label="已驳回" value="rejected" />
          </el-select>
          <el-select v-model="feedbackFilter.type" placeholder="问题类型" clearable style="width: 140px" @change="fetchFeedback">
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
              <el-rate v-model="row.rating" disabled v-if="row.rating" />
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
              <span v-else style="color: #999">{{ row.reviewed_by }}</span>
            </template>
          </el-table-column>
        </el-table>
        <el-pagination style="margin-top: 16px; justify-content: center" v-model:current-page="feedbackPage" :page-size="20" :total="feedback.total" layout="total, prev, pager, next" @current-change="fetchFeedback" />
      </el-tab-pane>

      <!-- 模型配置 -->
      <el-tab-pane label="模型配置" name="model">
        <div style="margin-bottom: 16px; display: flex; gap: 8px">
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

        <!-- 模型编辑对话框 -->
        <el-dialog v-model="modelDialogVisible" :title="modelForm.id ? '编辑模型' : '添加模型'" width="550px">
          <el-form :model="modelForm" label-width="110px">
            <el-form-item label="模型名称">
              <el-input v-model="modelForm.name" placeholder="如：Qwen-Plus 翻译模型" />
            </el-form-item>
            <el-form-item label="模型类型">
              <el-select v-model="modelForm.model_type" style="width: 100%">
                <el-option label="文本翻译模型" value="translation" />
                <el-option label="OCR 识别模型" value="vl" />
              </el-select>
            </el-form-item>
            <el-form-item label="模型 ID">
              <el-select
                v-if="modelForm.model_type === 'vl'"
                v-model="modelForm.model_id"
                style="width: 100%"
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
                style="width: 100%"
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
              <span style="color: #999; font-size: 12px; margin-left: 8px">同类型只能激活一个</span>
            </el-form-item>
          </el-form>
          <template #footer>
            <el-button @click="modelDialogVisible = false">取消</el-button>
            <el-button type="primary" @click="saveModel">保存</el-button>
          </template>
        </el-dialog>
      </el-tab-pane>

      <!-- 系统设置 -->
      <el-tab-pane label="系统设置" name="settings">
        <el-form label-width="140px" style="max-width: 600px">
          <el-form-item label="并发上限">
            <div style="display: flex; align-items: center; gap: 12px">
              <el-input-number v-model="concurrency.max_concurrency" :min="1" :max="20" />
              <el-tag>当前运行: {{ concurrency.current_running }}</el-tag>
              <el-button type="primary" size="small" @click="saveConcurrency">保存</el-button>
            </div>
          </el-form-item>
          <el-form-item label="文件保留天数">
            <div style="display: flex; align-items: center; gap: 12px">
              <el-input-number v-model="retentionDays" :min="0" :max="3650" />
              <span style="color: #999; font-size: 12px">0=永不过期</span>
              <el-button type="primary" size="small" @click="saveRetention">保存</el-button>
            </div>
          </el-form-item>
          <el-form-item label="手动清理">
            <el-button type="warning" @click="triggerCleanup">立即清理过期文件</el-button>
          </el-form-item>
        </el-form>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { onMounted, ref, computed, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'

const activeTab = ref('stats')

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

// ── 统计看板 ──────────────────────────────────────────────────────
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
  const { data } = await api.get('/admin/stats')
  stats.value = data
}

// ── 术语库 ────────────────────────────────────────────────────────
const terms = ref({ items: [], total: 0 })
const termPage = ref(1)
const termSearch = ref({ keyword: '', lang_pair: '', domain: '' })
const termSort = ref({ sort_by: 'updated_at', sort_order: 'desc' })
const selectedTermIds = ref([])
const termLangPairs = ref([])
const termDomains = ref([])
const termDialogVisible = ref(false)
const termForm = ref({ id: '', source_term: '', target_term: '', lang_pair: 'zh→en', domain: '', priority: 'preferred', note: '' })

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
  const { data } = await api.get('/admin/terms', { params })
  terms.value = data
}

function onTermSelectionChange(rows) {
  selectedTermIds.value = rows.map(r => r.id)
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
  } catch (err) {
    ElMessage.error(err.response?.data?.detail || '批量删除失败')
  }
}

function formatDateTime(s) {
  if (!s) return ''
  const d = new Date(s)
  if (isNaN(d.getTime())) return s
  const pad = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

async function fetchTermMeta() {
  const [lp, dm] = await Promise.all([
    api.get('/admin/terms/lang-pairs'),
    api.get('/admin/terms/domains'),
  ])
  termLangPairs.value = lp.data
  termDomains.value = dm.data
}

function showTermDialog(row) {
  if (row) {
    termForm.value = { ...row }
  } else {
    termForm.value = { id: '', source_term: '', target_term: '', lang_pair: 'zh→en', domain: '', priority: 'preferred', note: '' }
  }
  termDialogVisible.value = true
}

async function saveTerm() {
  if (termForm.value.id) {
    await api.put(`/admin/terms/${termForm.value.id}`, termForm.value)
  } else {
    await api.post('/admin/terms', termForm.value)
  }
  ElMessage.success('保存成功')
  termDialogVisible.value = false
  fetchTerms()
  fetchTermMeta()
}

async function deleteTerm(id) {
  await api.delete(`/admin/terms/${id}`)
  ElMessage.success('已删除')
  fetchTerms()
}

const termImporting = ref(false)

async function importTerms(file) {
  // 先询问可选的领域
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
    return false // 用户取消
  }

  const fd = new FormData()
  fd.append('file', file)
  if (inputDomain) fd.append('domain', inputDomain)
  termImporting.value = true
  try {
    const { data } = await api.post('/admin/terms/import', fd, {
      timeout: 600000, // 10分钟超时（大文件需要较长时间）
    })
    ElMessageBox.alert(
      `成功导入 ${data.imported} 条术语，跳过 ${data.skipped} 条`,
      '导入结果',
      { confirmButtonText: '确定', type: data.imported > 0 ? 'success' : 'warning' }
    )
    fetchTerms()
    fetchTermMeta()
  } catch (err) {
    if (err.response?.status === 413) {
      ElMessage.error('文件太大，超过上传限制（最大 500MB）')
    } else {
      ElMessage.error(err.response?.data?.detail || '导入失败，请检查文件格式')
    }
  } finally {
    termImporting.value = false
  }
  return false
}

async function exportTerms() {
  const resp = await api.get('/admin/terms/export', { responseType: 'blob' })
  const url = URL.createObjectURL(resp.data)
  const a = document.createElement('a')
  a.href = url
  a.download = 'terms.xlsx'
  a.click()
  URL.revokeObjectURL(url)
}

// ── 翻译记忆库 ────────────────────────────────────────────────────
const tm = ref({ items: [], total: 0 })
const tmPage = ref(1)
const tmSearch = ref({ keyword: '', lang_pair: '' })
const tmDialogVisible = ref(false)
const tmForm = ref({ id: '', source_text: '', target_text: '', lang_pair: 'zh→en', source: 'manual', domain: '' })

async function fetchTm() {
  const params = { page: tmPage.value, page_size: 20 }
  if (tmSearch.value.keyword) params.keyword = tmSearch.value.keyword
  if (tmSearch.value.lang_pair) params.lang_pair = tmSearch.value.lang_pair
  const { data } = await api.get('/admin/tm', { params })
  tm.value = data
}

// 从已完成任务导入 TM（对齐原文/译文段落写入，source=auto）
async function importTmFromTask() {
  let taskId
  try {
    const res = await ElMessageBox.prompt('输入已完成任务的 ID（任务列表可复制）', '从任务导入 TM', {
      confirmButtonText: '导入',
      cancelButtonText: '取消',
      inputPlaceholder: '任务 UUID',
    })
    taskId = res.value?.trim()
  } catch (e) { return }
  if (!taskId) return
  try {
    const { data } = await api.post(`/admin/tm/import-from-task/${taskId}`)
    ElMessage.success(`导入完成：新增/更新 ${data.imported} 条，跳过 ${data.skipped} 条`)
    fetchTm()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '导入失败')
  }
}

function showTmDialog(row) {
  if (row) {
    tmForm.value = { ...row }
  } else {
    tmForm.value = { id: '', source_text: '', target_text: '', lang_pair: 'zh→en', source: 'manual', domain: '' }
  }
  tmDialogVisible.value = true
}

async function saveTm() {
  if (tmForm.value.id) {
    await api.put(`/admin/tm/${tmForm.value.id}`, tmForm.value)
  } else {
    await api.post('/admin/tm', tmForm.value)
  }
  ElMessage.success('保存成功')
  tmDialogVisible.value = false
  fetchTm()
}

async function deleteTm(id) {
  await api.delete(`/admin/tm/${id}`)
  ElMessage.success('已删除')
  fetchTm()
}

// ── 质量反馈 ──────────────────────────────────────────────────────
const feedback = ref({ items: [], total: 0 })
const feedbackPage = ref(1)
const feedbackFilter = ref({ status: '', type: '' })

async function fetchFeedback() {
  const params = { page: feedbackPage.value, page_size: 20 }
  if (feedbackFilter.value.status) params.status = feedbackFilter.value.status
  if (feedbackFilter.value.type) params.feedback_type = feedbackFilter.value.type
  const { data } = await api.get('/feedback', { params })
  feedback.value = data
}

async function reviewFeedback(id, status) {
  await api.put(`/feedback/${id}`, { status })
  ElMessage.success('操作成功')
  fetchFeedback()
}

async function rejectFeedback(id) {
  const { value } = await ElMessageBox.prompt('请输入驳回原因（可选）', '驳回反馈', {
    confirmButtonText: '确定驳回',
    cancelButtonText: '取消',
    inputPlaceholder: '驳回原因',
  }).catch(() => ({ value: '' }))
  await api.put(`/feedback/${id}`, { status: 'rejected', reject_reason: value || '' })
  ElMessage.success('已驳回')
  fetchFeedback()
}

// ── 模型配置（多模型管理） ──────────────────────────────────────────
const modelList = ref([])
const modelDialogVisible = ref(false)
const modelForm = ref({ id: '', name: '', model_type: 'translation', model_id: '', api_base_url: '', api_key: '', is_active: false })

async function fetchModels() {
  const { data } = await api.get('/admin/models')
  modelList.value = data
}

function showModelDialog(row) {
  if (row) {
    modelForm.value = { ...row, api_key: '' }
  } else {
    modelForm.value = { id: '', name: '', model_type: 'translation', model_id: '', api_base_url: '', api_key: '', is_active: false }
  }
  modelDialogVisible.value = true
}

// 选择 PaddleOCR 时自动填充
watch(() => modelForm.value.model_id, (val) => {
  if (val === 'paddleocr') {
    modelForm.value.api_base_url = 'local'
    modelForm.value.api_key = 'local'
  }
})

async function saveModel() {
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
}

async function deleteModel(id) {
  await api.delete(`/admin/models/${id}`)
  ElMessage.success('已删除')
  fetchModels()
}

async function toggleModelActive(row) {
  await api.put(`/admin/models/${row.id}`, { is_active: row.is_active })
  ElMessage.success(row.is_active ? '已激活' : '已停用')
  fetchModels()
}

// ── 系统设置 ──────────────────────────────────────────────────────
const concurrency = ref({ max_concurrency: 4, current_running: 0 })
const retentionDays = ref(180)

async function fetchConcurrency() {
  const { data } = await api.get('/admin/concurrency')
  concurrency.value = data
}

async function saveConcurrency() {
  await api.put('/admin/concurrency', { max_concurrency: concurrency.value.max_concurrency })
  ElMessage.success('并发上限已更新')
  fetchConcurrency()
}

async function fetchRetention() {
  const { data } = await api.get('/admin/retention')
  retentionDays.value = data.days
}

async function saveRetention() {
  await api.put('/admin/retention', { days: retentionDays.value })
  ElMessage.success('保留天数已更新')
}

async function triggerCleanup() {
  const { data } = await api.post('/admin/cleanup')
  ElMessage.success(`清理完成：扫描 ${data.scanned}，删除 ${data.deleted}，失败 ${data.failed}`)
}

// ── 初始化 ────────────────────────────────────────────────────────
onMounted(() => {
  fetchStats()
  fetchTerms()
  fetchTermMeta()
  fetchTm()
  fetchFeedback()
  fetchModels()
  fetchConcurrency()
  fetchRetention()
})
</script>
