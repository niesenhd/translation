<template>
  <el-card>
    <template #header>
      <div style="display: flex; align-items: center; justify-content: space-between">
        <span>翻译任务</span>
        <el-button type="primary" @click="dialogVisible = true">+ 新建翻译</el-button>
      </div>
    </template>

    <!-- 批量操作栏 -->
    <div v-if="selectedRows.length > 0" style="margin-bottom: 12px; display: flex; align-items: center; gap: 8px">
      <span>已选 {{ selectedRows.length }} 项</span>
      <el-button size="small" @click="batchDownloadSource">批量下载原文</el-button>
      <el-button size="small" type="primary" @click="batchDownloadResult">批量下载译文</el-button>
      <el-button size="small" type="danger" @click="batchDelete">批量删除</el-button>
      <el-button size="small" @click="clearSelection">取消选择</el-button>
    </div>

    <el-table :data="tasks" v-loading="loading" stripe @selection-change="onSelectionChange" ref="tableRef">
      <el-table-column type="selection" width="45" />
      <el-table-column prop="original_filename" label="文件" min-width="200" />
      <el-table-column prop="file_ext" label="格式" width="80" />
      <el-table-column prop="target_lang" label="目标语种" width="100" />
      <el-table-column label="模式" width="130">
        <template #default="{ row }">
          {{ row.output_mode === 'plain' ? '纯译文' : '双语对照' }}
          <el-tag v-if="row.refine_mode === 'double_pass'" size="small" type="warning" effect="plain" style="margin-left: 4px">精译</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="200">
        <template #default="{ row }">
          <el-tooltip
            v-if="row.status === 'failed' && row.error_message"
            :content="row.error_message"
            placement="top"
            effect="dark"
          >
            <el-tag :type="statusType(row.status)">{{ statusText(row.status) }} <el-icon style="margin-left:2px;vertical-align:-2px"><Warning /></el-icon></el-tag>
          </el-tooltip>
          <el-tag v-else :type="statusType(row.status)">{{ statusText(row.status) }}</el-tag>
          <el-progress
            v-if="row.status === 'running' || row.status === 'queued'"
            :percentage="row.progress"
            :show-text="false"
            style="margin-top: 4px"
          />
          <div
            v-if="row.status === 'queued' && row.queue_position"
            style="font-size: 12px; color: #909399; margin-top: 2px"
          >
            排队第 {{ row.queue_position }} 位
            <span v-if="row.estimated_wait_seconds != null">· 预计等待 {{ formatWait(row.estimated_wait_seconds) }}</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="创建时间" width="180">
        <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="320">
        <template #default="{ row }">
          <el-button size="small" @click="downloadSource(row)">下载原文</el-button>
          <el-button
            v-if="row.status === 'succeeded'"
            size="small"
            type="primary"
            @click="downloadResult(row)"
          >下载译文</el-button>
          <el-button
            v-if="row.status === 'succeeded'"
            size="small"
            type="warning"
            @click="showFeedbackDialog(row)"
          >反馈</el-button>
          <el-button
            v-if="row.status === 'failed'"
            size="small"
            type="primary"
            @click="retry(row)"
          >重试</el-button>
          <el-button size="small" type="danger" @click="del(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-card>

  <el-dialog v-model="dialogVisible" title="新建翻译任务" width="520px" @open="onDialogOpen">
    <el-form label-width="100px">
      <el-form-item label="文件">
        <el-upload :auto-upload="false" :on-change="onFileChange" :on-remove="onFileRemove" :file-list="fileList" multiple>
          <el-button>选择文件</el-button>
          <template #tip>
            <div class="el-upload__tip">支持：docx / doc / xlsx / xls / csv / pptx / ppt / pdf / txt / md（可多选）</div>
          </template>
        </el-upload>
      </el-form-item>
      <el-form-item label="目标语种">
        <el-select v-model="form.target_lang" style="width: 100%">
          <el-option label="中文（简体）" value="zh" />
          <el-option label="English" value="en" />
          <el-option label="Français" value="fr" />
          <el-option label="Español" value="es" />
          <el-option label="Русский" value="ru" />
          <el-option label="العربية" value="ar" />
          <el-option label="日本語" value="ja" />
          <el-option label="한국어" value="ko" />
          <el-option label="繁體中文" value="zh-Hant" />
        </el-select>
      </el-form-item>
      <el-form-item label="输出模式">
        <el-radio-group v-model="form.output_mode">
          <el-radio value="plain">纯译文</el-radio>
          <el-radio value="bilingual">双语对照</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item v-if="form.output_mode === 'bilingual'" label="脚注处理">
        <el-radio-group v-model="form.footnote_mode">
          <el-radio value="bilingual">脚注双语（原文+译文）</el-radio>
          <el-radio value="translation_only">脚注仅译文</el-radio>
          <el-radio value="skip">脚注不翻译</el-radio>
        </el-radio-group>
        <div style="color: #909399; font-size: 12px; margin-top: 2px">脚注很长时选"仅译文"可避免版面被脚注撑开/切割；文档无脚注则本项无效</div>
      </el-form-item>
      <el-form-item v-if="isPdf" label="PDF 输出">
        <el-radio-group v-model="form.pdf_output_format">
          <el-radio value="pdf">原版 PDF（保留版面/页眉页脚/图片）</el-radio>
          <el-radio value="docx">转 Word（便于二次编辑）</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="图片翻译">
        <el-radio-group v-model="form.translate_images">
          <el-radio value="yes">翻译图片中的文字</el-radio>
          <el-radio value="no">仅翻译文档文字，图片保持原样</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="精译模式">
        <el-switch v-model="form.refine_mode" active-value="double_pass" inactive-value="none" />
        <span style="margin-left: 10px; color: #909399; font-size: 12px">
          开启后翻译 + 法律译审复核两遍，术语与文体更准（耗时与成本约 2 倍，重要文书建议开启）
        </span>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="submit">提交</el-button>
    </template>
  </el-dialog>

  <!-- 反馈对话框 -->
  <el-dialog v-model="feedbackDialogVisible" title="翻译质量反馈" width="500px">
    <el-form :model="feedbackForm" label-width="80px">
      <el-form-item label="评分">
        <el-rate v-model="feedbackForm.rating" />
      </el-form-item>
      <el-form-item label="问题类型">
        <el-select v-model="feedbackForm.feedback_type" clearable placeholder="选择问题类型（可选）" style="width: 100%">
          <el-option label="术语错误" value="terminology" />
          <el-option label="语法问题" value="grammar" />
          <el-option label="格式错乱" value="format" />
          <el-option label="漏译" value="omission" />
          <el-option label="其他" value="other" />
        </el-select>
      </el-form-item>
      <el-form-item label="修改建议">
        <el-input v-model="feedbackForm.suggestion" type="textarea" :rows="4" placeholder="如：第3段 连带责任 应译为 joint and several liability" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="feedbackDialogVisible = false">取消</el-button>
      <el-button type="primary" @click="submitFeedback">提交反馈</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, nextTick, onMounted, onBeforeUnmount, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Warning } from '@element-plus/icons-vue'
import api from '../api'

const tasks = ref([])
const loading = ref(false)
const dialogVisible = ref(false)
const submitting = ref(false)
const fileList = ref([])
const selectedFiles = ref([])
const selectedRows = ref([])
const tableRef = ref(null)
const form = reactive({ target_lang: 'zh', output_mode: 'plain', pdf_output_format: 'pdf', translate_images: 'no', refine_mode: 'none', footnote_mode: 'bilingual' })

// 反馈相关
const feedbackDialogVisible = ref(false)
const feedbackForm = reactive({ task_id: '', rating: 0, feedback_type: '', suggestion: '' })

// 当前选择的文件中是否包含 PDF（控制 PDF 输出格式选择器是否显示）
const isPdf = computed(() => {
  return selectedFiles.value.some(f => /\.pdf$/i.test(f.name || ''))
})

let timer = null

async function fetchTasks() {
  // 保存当前选中的 ID
  const selectedIds = new Set(selectedRows.value.map(r => r.id))
  loading.value = true
  try {
    const { data } = await api.get('/tasks')
    tasks.value = data
    // 恢复选中状态
    if (selectedIds.size > 0) {
      await nextTick()
      for (const row of data) {
        if (selectedIds.has(row.id)) {
          tableRef.value?.toggleRowSelection(row, true)
        }
      }
    }
  } finally {
    loading.value = false
  }
}

function onFileChange(file) {
  selectedFiles.value.push(file.raw)
}

function onFileRemove(file) {
  const idx = selectedFiles.value.findIndex(f => f.name === file.name && f.size === file.size)
  if (idx > -1) {
    selectedFiles.value.splice(idx, 1)
  }
}

function onDialogOpen() {
  fileList.value = []
  selectedFiles.value = []
}

function onSelectionChange(rows) {
  selectedRows.value = rows
}

function clearSelection() {
  tableRef.value?.clearSelection()
}

async function submit() {
  if (!selectedFiles.value.length) {
    ElMessage.warning('请选择文件')
    return
  }
  submitting.value = true
  const files = [...selectedFiles.value]
  const results = await Promise.allSettled(
    files.map(file => {
      const fd = new FormData()
      fd.append('file', file)
      fd.append('target_lang', form.target_lang)
      fd.append('output_mode', form.output_mode)
      fd.append('pdf_output_format', form.pdf_output_format)
      fd.append('translate_images', form.translate_images)
      fd.append('refine_mode', form.refine_mode)
      fd.append('footnote_mode', form.footnote_mode)
      return api.post('/tasks/upload', fd)
    })
  )
  const succeeded = results.filter(r => r.status === 'fulfilled').length
  const failed = results.filter(r => r.status === 'rejected').length
  if (succeeded && !failed) {
    ElMessage.success(`已提交 ${succeeded} 个翻译任务`)
  } else if (succeeded && failed) {
    ElMessage.warning(`${succeeded} 个成功，${failed} 个失败`)
  } else {
    const reason = results[0]?.reason?.response?.data?.detail || '提交失败'
    ElMessage.error(reason)
  }
  if (succeeded) {
    dialogVisible.value = false
    fileList.value = []
    selectedFiles.value = []
    fetchTasks()
  }
  submitting.value = false
}

async function downloadFile(url, fallbackName) {
  const resp = await api.get(url, { responseType: 'blob' })
  const blob = resp.data
  const blobUrl = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = blobUrl
  // 优先解析后端返回的 RFC 5987 文件名（filename*=UTF-8''xxx），其次 filename="xxx"
  const cd = resp.headers['content-disposition'] || ''
  let name = fallbackName
  const m1 = cd.match(/filename\*=UTF-8''([^;]+)/i)
  const m2 = cd.match(/filename="?([^";]+)"?/i)
  if (m1) {
    try { name = decodeURIComponent(m1[1]) } catch { /* ignore */ }
  } else if (m2) {
    name = m2[1]
  }
  a.download = name
  a.click()
  URL.revokeObjectURL(blobUrl)
}

function downloadResult(row) {
  return downloadFile(`/tasks/${row.id}/download`, `${row.id}.bin`)
}

function downloadSource(row) {
  return downloadFile(`/tasks/${row.id}/download/source`, row.original_filename || `${row.id}.bin`)
}

async function retry(row) {
  try {
    await api.post(`/tasks/${row.id}/retry`)
    ElMessage.success('已重新提交')
    fetchTasks()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '重试失败')
  }
}

async function del(row) {
  await api.delete(`/tasks/${row.id}`)
  ElMessage.success('已删除')
  fetchTasks()
}

// 批量操作
async function batchDownloadPost(url, fallbackName) {
  const ids = selectedRows.value.map(r => r.id)
  if (!ids.length) return
  try {
    const resp = await api.post(url, ids, { responseType: 'blob' })
    const blob = resp.data
    const blobUrl = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = blobUrl
    const cd = resp.headers['content-disposition'] || ''
    let name = fallbackName
    const m1 = cd.match(/filename\*=UTF-8''([^;]+)/i)
    const m2 = cd.match(/filename="?([^";]+)"?/i)
    if (m1) {
      try { name = decodeURIComponent(m1[1]) } catch { /* ignore */ }
    } else if (m2) {
      name = m2[1]
    }
    a.download = name
    a.click()
    URL.revokeObjectURL(blobUrl)
  } catch (e) {
    ElMessage.error('批量下载失败')
  }
}

async function batchDownloadSource() {
  await batchDownloadPost('/tasks/batch/download-source', 'sources.zip')
}

async function batchDownloadResult() {
  await batchDownloadPost('/tasks/batch/download-result', 'translations.zip')
}

async function batchDelete() {
  const ids = selectedRows.value.map(r => r.id)
  if (!ids.length) return
  try {
    await ElMessageBox.confirm(`确定删除选中的 ${ids.length} 个任务？`, '批量删除', { type: 'warning' })
  } catch {
    return
  }
  try {
    await api.post('/tasks/batch/delete', ids)
    ElMessage.success('已删除')
    clearSelection()
    fetchTasks()
  } catch (e) {
    ElMessage.error('批量删除失败')
  }
}

function statusType(s) {
  return { queued: 'info', running: 'warning', succeeded: 'success', failed: 'danger' }[s] || ''
}
function statusText(s) {
  return { queued: '排队中', running: '翻译中', succeeded: '完成', failed: '失败' }[s] || s
}
function formatTime(t) {
  return new Date(t).toLocaleString('zh-CN')
}

function formatWait(seconds) {
  if (seconds == null) return ''
  if (seconds < 60) return `${seconds}秒`
  if (seconds < 3600) return `${Math.ceil(seconds / 60)}分钟`
  return `${(seconds / 3600).toFixed(1)}小时`
}

function showFeedbackDialog(row) {
  feedbackForm.task_id = row.id
  feedbackForm.rating = 0
  feedbackForm.feedback_type = ''
  feedbackForm.suggestion = ''
  feedbackDialogVisible.value = true
}

async function submitFeedback() {
  if (!feedbackForm.rating && !feedbackForm.suggestion && !feedbackForm.feedback_type) {
    ElMessage.warning('请至少填写一项反馈内容')
    return
  }
  try {
    await api.post('/feedback', {
      task_id: feedbackForm.task_id,
      rating: feedbackForm.rating || null,
      feedback_type: feedbackForm.feedback_type || null,
      suggestion: feedbackForm.suggestion || null,
    })
    ElMessage.success('反馈已提交，感谢您的评价')
    feedbackDialogVisible.value = false
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '提交反馈失败')
  }
}

// 动态轮询：有活跃任务时 3s，无活跃任务时 15s，减少不必要的数据库和网络开销
const ACTIVE_STATUSES = ['queued', 'running']
let currentInterval = 3000

const smartPoll = () => {
  fetchTasks().then(() => {
    const hasActive = tasks.value.some(t => ACTIVE_STATUSES.includes(t.status))
    const nextInterval = hasActive ? 3000 : 15000
    if (nextInterval !== currentInterval) {
      clearInterval(timer)
      currentInterval = nextInterval
      timer = setInterval(smartPoll, currentInterval)
    }
  }).catch(() => {
    // 网络错误时保持原间隔重试
  })
}

onMounted(() => {
  fetchTasks()
  timer = setInterval(smartPoll, currentInterval)
})
onBeforeUnmount(() => clearInterval(timer))
</script>
