<template>
  <div>
    <n-card embedded>
      <n-space vertical size="medium">
        <n-space align="center" justify="space-between" :wrap="false">
          <n-space align="center" :size="8">
            <n-button type="primary" @click="pickFile">
              <template #icon>
                <Icon icon="lucide:upload" />
              </template>
              上传笔记
            </n-button>
            <n-button
              type="error"
              secondary
              :disabled="!checkedRowKeys.length"
              @click="askDeleteSelected"
            >
              <template #icon>
                <Icon icon="lucide:trash-2" />
              </template>
              删除选中{{ checkedRowKeys.length ? ` (${checkedRowKeys.length})` : '' }}
            </n-button>
          </n-space>
          <n-input
            v-model:value="keyword"
            placeholder="关键词搜索（路径 / 标题 / tags）"
            clearable
            style="max-width: 360px"
            @keyup.enter="reload"
          >
            <template #suffix>
              <Icon icon="lucide:search" @click="reload" style="cursor: pointer" />
            </template>
          </n-input>
        </n-space>

        <n-data-table
          remote
          :columns="columns"
          :data="items"
          :loading="loading"
          :pagination="pagination"
          :row-key="(row: any) => row.source_id"
          :checked-row-keys="checkedRowKeys"
          @update:checked-row-keys="onCheckedChange"
          @update:page="onPageChange"
          @update:page-size="onPageSizeChange"
        />
      </n-space>
    </n-card>

    <input
      ref="fileInputRef"
      type="file"
      accept=".md,.txt"
      style="display: none"
      @change="onFilePicked"
    />

    <!-- 正文预览 -->
    <n-modal
      v-model:show="previewDialog"
      preset="card"
      :title="previewTitle"
      style="width: 860px"
      :bordered="false"
    >
      <n-space vertical size="small">
        <n-space size="small" wrap>
          <n-tag size="small" :bordered="false">短ID {{ preview?.note_short_id }}</n-tag>
          <n-tag size="small" :bordered="false">{{ preview?.source_file_path }}</n-tag>
          <n-tag v-if="preview?.total_lines" size="small" :bordered="false">
            共 {{ preview.total_lines }} 行
          </n-tag>
        </n-space>
        <n-spin :show="previewLoading">
          <pre class="code-pre">{{ preview?.content || '（无内容）' }}</pre>
        </n-spin>
        <n-space justify="space-between" align="center">
          <n-button
            v-if="preview?.has_more"
            size="small"
            quaternary
            :loading="previewLoading"
            @click="loadMorePreview"
          >
            加载更多
          </n-button>
          <span v-else class="muted">已显示全部</span>
          <n-button
            v-if="preview"
            type="error"
            secondary
            size="small"
            @click="askDelete(preview)"
          >
            删除这篇
          </n-button>
        </n-space>
      </n-space>
    </n-modal>

    <!-- 删除确认 -->
    <n-modal
      v-model:show="deleteDialog"
      preset="dialog"
      type="warning"
      :title="deleteTitle"
      :positive-text="deletePositiveText"
      negative-text="取消"
      :positive-button-props="{ type: 'error', loading: deleting }"
      @positive-click="doDelete"
    >
      <div>以下文件会从磁盘移除，索引与切片一并清理，<b>不可撤销</b>。</div>
      <div class="delete-list">
        <div v-for="row in deleteTargets" :key="row.source_id" class="delete-item">
          <div>{{ row.source_file_path }}</div>
          <div v-if="deleteTargets.length === 1" class="delete-meta">
            {{ row.total_lines }} 行<template v-if="row.updated_at"> · 更新于 {{ formatTime(row.updated_at) }}</template>
          </div>
        </div>
      </div>
    </n-modal>

    <!-- 上传预览 -->
    <n-modal
      v-model:show="uploadDialog"
      preset="card"
      title="上传笔记"
      style="width: 860px"
      :bordered="false"
    >
      <n-space vertical size="medium">
        <n-descriptions :column="1" size="small" label-placement="left">
          <n-descriptions-item label="原文件">{{ uploadSource }}</n-descriptions-item>
          <n-descriptions-item label="标题">
            <n-input
              v-model:value="uploadTitle"
              placeholder="笔记标题"
              style="max-width: 420px"
            />
          </n-descriptions-item>
          <n-descriptions-item label="将保存为">
            <span class="mono">{{ willSavePath }}</span>
          </n-descriptions-item>
        </n-descriptions>
        <n-alert v-if="uploadTitleSource === 'filename'" type="info" :bordered="false">
          正文里没有一级标题（# 开头），已用文件名作标题。
        </n-alert>
        <n-alert v-if="titleChanged" type="warning" :bordered="false">
          确认后会把正文里的一级标题改写为新标题；下方预览仍显示上传时的原文。
        </n-alert>
        <n-alert type="info" :bordered="false">
          若目录里已有同名文件，会自动加时间戳，不会覆盖。
        </n-alert>
        <pre class="code-pre">{{ uploadContent }}</pre>
      </n-space>
      <template #footer>
        <n-space justify="end">
          <n-button @click="uploadDialog = false">取消</n-button>
          <n-button type="primary" :loading="uploading" @click="confirmUpload">
            确认上传
          </n-button>
        </n-space>
      </template>
    </n-modal>
  </div>
</template>

<script setup lang="ts">
import { h, ref, computed, onMounted } from 'vue'
import type { DataTableColumns } from 'naive-ui'
import {
  NAlert,
  NButton,
  NCard,
  NDataTable,
  NDescriptions,
  NDescriptionsItem,
  NInput,
  NModal,
  NSpace,
  NSpin,
  NTag,
  useMessage,
} from 'naive-ui'
import { Icon } from '@iconify/vue'
import { useBridge } from '@/composables/useBridge'
import { parseTags } from '@/utils/format'

const { apiGet, apiPost } = useBridge()
const message = useMessage()

const loading = ref(false)
const items = ref<any[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const keyword = ref('')
const checkedRowKeys = ref<string[]>([])

const fileInputRef = ref<HTMLInputElement | null>(null)

const previewDialog = ref(false)
const previewLoading = ref(false)
const preview = ref<any>(null)

const deleteDialog = ref(false)
const deleteTargets = ref<any[]>([])
const deleting = ref(false)

const uploadDialog = ref(false)
const uploading = ref(false)
const uploadSource = ref('')
const uploadTitle = ref('')
const uploadOriginalTitle = ref('')
const uploadTitleSource = ref<'h1' | 'filename'>('h1')
const uploadContent = ref('')

const titleChanged = computed(
  () => uploadTitle.value.trim() !== uploadOriginalTitle.value.trim(),
)

function buildHeading(item: any): string {
  const parts = []
  for (let i = 1; i <= 6; i++) {
    const hh = item[`heading_h${i}`]
    if (hh) parts.push(hh)
  }
  return parts.join(' / ') || '(无标题)'
}

const previewTitle = computed(() => {
  const heading = preview.value ? buildHeading(preview.value) : ''
  return heading === '(无标题)' ? '笔记正文' : heading
})

const deleteTitle = computed(() =>
  deleteTargets.value.length > 1
    ? `删除 ${deleteTargets.value.length} 篇笔记`
    : '删除这篇笔记',
)

const deletePositiveText = computed(() =>
  deleteTargets.value.length > 1 ? `删除 ${deleteTargets.value.length} 篇` : '删除',
)

const willSavePath = computed(() => {
  const source = uploadSource.value || 'note.md'
  const dot = source.lastIndexOf('.')
  const extension = dot >= 0 ? source.slice(dot).toLowerCase() : '.md'
  return `upload/${sanitizeTitle(uploadTitle.value)}${extension}`
})

function formatTime(value: number | string | null | undefined): string {
  if (!value) return ''
  const stamp = typeof value === 'number' ? value * 1000 : Date.parse(String(value))
  if (!stamp || Number.isNaN(stamp)) return ''
  const date = new Date(stamp)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function sanitizeTitle(title: string): string {
  const cleaned = Array.from(title || '')
    .filter(ch => {
      const code = ch.codePointAt(0) ?? 0
      return code >= 0x20 && code !== 0x7f
    })
    .join('')
  const safe = cleaned
    .replace(/[<>:"/\\|?*]/g, '_')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/^[.\s]+|[.\s]+$/g, '')
    .slice(0, 80)
    .replace(/^[.\s]+|[.\s]+$/g, '')
  return safe || 'untitled'
}

const selectedRows = computed(() =>
  items.value.filter(item =>
    checkedRowKeys.value.includes(item.source_id),
  ),
)

const pagination = computed(() => ({
  page: page.value,
  pageSize: pageSize.value,
  itemCount: total.value,
  showSizePicker: true,
  pageSizes: [10, 20, 50, 100],
  prefix: ({ itemCount }: { itemCount?: number }) => `共 ${itemCount ?? 0} 条`,
}))

const columns: DataTableColumns<any> = [
  { type: 'selection', width: 40 },
  { title: 'Short ID', key: 'note_short_id', width: 90 },
  {
    title: '文件路径',
    key: 'source_file_path',
    ellipsis: { tooltip: true },
  },
  {
    title: '标题',
    key: 'heading',
    ellipsis: { tooltip: true },
    render: row => buildHeading(row),
  },
  {
    title: 'Tags',
    key: 'tags_text',
    width: 200,
    render: row => {
      const tags = parseTags(row.tags_text)
      if (!tags.length) return ''
      return h(
        'div',
        { style: 'display:flex; flex-wrap:wrap; gap:4px;' },
        tags.map(tag => h(NTag, { size: 'small', bordered: false }, { default: () => tag })),
      )
    },
  },
  { title: '行数', key: 'total_lines', width: 70 },
  {
    title: '操作',
    key: 'actions',
    width: 86,
    render: row =>
      h(
        NSpace,
        { size: 4, wrap: false },
        {
          default: () => [
            h(
              NButton,
              {
                size: 'tiny',
                quaternary: true,
                onClick: () => showPreview(row),
              },
              { icon: () => h(Icon, { icon: 'lucide:eye' }) },
            ),
            h(
              NButton,
              {
                size: 'tiny',
                quaternary: true,
                type: 'error',
                onClick: () => askDelete(row),
              },
              { icon: () => h(Icon, { icon: 'lucide:trash-2' }) },
            ),
          ],
        },
      ),
  },
]

function onCheckedChange(keys: Array<string | number>) {
  checkedRowKeys.value = keys.map(key => String(key))
}

function onPageChange(p: number) {
  page.value = p
  loadData()
}

function onPageSizeChange(s: number) {
  pageSize.value = s
  page.value = 1
  loadData()
}

async function loadData() {
  loading.value = true
  try {
    const data: any = await apiGet('notes', {
      keyword: keyword.value,
      page: page.value,
      page_size: pageSize.value,
    })
    items.value = data.items || []
    total.value = data.total || 0
    checkedRowKeys.value = []
  } catch (e) {
    console.error('加载笔记索引失败:', e)
  } finally {
    loading.value = false
  }
}

function reload() {
  page.value = 1
  loadData()
}

// ---------- 预览 ----------

async function showPreview(row: any) {
  preview.value = { ...row, content: '', actual_end_line: 0, has_more: false }
  previewDialog.value = true
  previewLoading.value = true
  try {
    const data: any = await apiPost('notes/recall', {
      note_short_id: row.note_short_id,
      offset: 1,
      limit: 400,
    })
    if (data.error) {
      message.error(data.error)
    } else {
      preview.value = {
        ...row,
        ...data,
        has_more: data.actual_end_line < data.total_lines,
      }
    }
  } catch (e: any) {
    message.error(e?.message || '读取失败')
  } finally {
    previewLoading.value = false
  }
}

async function loadMorePreview() {
  const current = preview.value
  if (!current) return
  previewLoading.value = true
  try {
    const next = (current.actual_end_line || 0) + 1
    const data: any = await apiPost('notes/recall', {
      note_short_id: current.note_short_id,
      offset: next,
      limit: 400,
    })
    if (data.error) {
      message.error(data.error)
      return
    }
    preview.value = {
      ...current,
      content: `${current.content}\n${data.content}`,
      actual_end_line: data.actual_end_line,
      has_more: data.actual_end_line < data.total_lines,
    }
  } finally {
    previewLoading.value = false
  }
}

// ---------- 删除 ----------

function askDelete(row: any) {
  deleteTargets.value = [row]
  deleteDialog.value = true
}

function askDeleteSelected() {
  if (!selectedRows.value.length) {
    message.warning('请先勾选要删除的笔记')
    return
  }
  deleteTargets.value = selectedRows.value
  deleteDialog.value = true
}

async function doDelete() {
  if (deleting.value) return
  deleting.value = true
  try {
    const sourceIds = deleteTargets.value.map(item => item.source_id)
    const data: any = await apiPost('notes/delete', { source_ids: sourceIds })
    if (data?.error) {
      message.error(data.error)
      return
    }
    const deleted = data?.deleted?.length || 0
    const failed = data?.failed?.length || 0
    if (deleted) message.success(`已删除 ${deleted} 篇笔记`)
    if (failed) {
      const reasons = (data?.failed || [])
        .slice(0, 3)
        .map((item: any) => `${item.source_id}：${item.reason}`)
        .join('；')
      message.warning(`${failed} 篇未能删除：${reasons}`, { duration: 8000 })
    }
    deleteDialog.value = false
    previewDialog.value = false
    await loadData()
  } catch (e: any) {
    message.error(e?.message || '删除失败')
  } finally {
    deleting.value = false
  }
}

// ---------- 上传 ----------

function pickFile() {
  fileInputRef.value?.click()
}

function readAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const result = String(reader.result || '')
      const comma = result.indexOf(',')
      resolve(comma >= 0 ? result.slice(comma + 1) : '')
    }
    reader.onerror = () => reject(reader.error)
    reader.readAsDataURL(file)
  })
}

async function onFilePicked(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return

  uploading.value = true
  try {
    const base64 = await readAsBase64(file)
    const data: any = await apiPost('notes/upload', { filename: file.name, base64 })
    if (data?.error) {
      message.error(data.error)
      return
    }
    const previewData = data.preview || {}
    uploadSource.value = previewData.filename || file.name
    uploadTitle.value = previewData.title || ''
    uploadOriginalTitle.value = previewData.title || ''
    uploadTitleSource.value = previewData.title_source === 'filename' ? 'filename' : 'h1'
    uploadContent.value = previewData.content || ''
    uploadDialog.value = true
  } catch (e: any) {
    message.error(e?.message || '读取文件失败')
  } finally {
    uploading.value = false
  }
}

async function confirmUpload() {
  if (uploading.value) return
  const title = uploadTitle.value.trim()
  if (!title) {
    message.warning('标题不能为空')
    return
  }
  uploading.value = true
  try {
    const data: any = await apiPost('notes/upload/confirm', {
      filename: uploadSource.value,
      title,
      original_title: uploadOriginalTitle.value,
      content: uploadContent.value,
    })
    if (data?.error) {
      message.error(data.error)
      return
    }
    message.success(`已保存为 ${data?.saved?.relative_path || '笔记'}`)
    uploadDialog.value = false
    await reload()
  } catch (e: any) {
    message.error(e?.message || '上传失败')
  } finally {
    uploading.value = false
  }
}

onMounted(() => loadData())
</script>

<style scoped>
.code-pre {
  max-height: 52vh;
  overflow: auto;
  background: rgba(0, 0, 0, 0.3);
  border-radius: 6px;
  padding: 12px;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}

.muted {
  opacity: 0.6;
  font-size: 12px;
}

.delete-list {
  margin-top: 10px;
  max-height: 30vh;
  overflow: auto;
  background: rgba(0, 0, 0, 0.15);
  border-radius: 6px;
  padding: 8px 10px;
}

.delete-item {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  line-height: 1.9;
  word-break: break-all;
}

.delete-meta {
  font-family: inherit;
  font-size: 11px;
  opacity: 0.6;
}
</style>
