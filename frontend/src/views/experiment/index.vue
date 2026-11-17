<template>
  <section class="page" data-module="experiment">
    <header class="page-head">
      <div>
        <h2>分析实验</h2>
        <p class="page-desc">
          按筛选条件预聚合实验分组摘要，查询、翻页与导出共用同一份样本口径；切换样本区间或分组字段只重新聚合一次。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" data-testid="export" @click="exportRows">导出当前口径</button>
        <button class="btn primary" type="button" data-testid="persist-version" @click="persistSummary">
          保存摘要版本
        </button>
      </div>
    </header>

    <div class="analysis-summary">
      <article v-for="m in summaryMetrics" :key="m.label" class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">{{ m.label }}</span>
        <strong style="font-size:22px;display:block">{{ m.value }}</strong>
        <span :class="m.trend > 0 ? 'trend-up' : 'trend-down'" style="font-size:12px">
          {{ m.trend > 0 ? '↑' : '↓' }}{{ Math.abs(m.trend) }}%
        </span>
      </article>
    </div>

    <form class="analysis-form" @submit.prevent="applyFilters">
      <label>
        <span>样本区间</span>
        <select v-model="filters['样本区间']" data-testid="filter-样本区间">
          <option value="">全部区间</option>
          <option v-for="range in sampleRanges" :key="range" :value="range">{{ range }}</option>
        </select>
      </label>
      <label>
        <span>分组字段</span>
        <select v-model="filters['分组字段']" data-testid="filter-分组字段">
          <option v-for="field in groupFields" :key="field" :value="field">{{ field }}</option>
        </select>
      </label>
      <label>
        <span>方案编号</span>
        <input v-model="filters['方案编号']" data-testid="filter-方案编号" placeholder="按方案编号或实验名称检索" />
      </label>
      <button class="btn primary" type="submit" data-testid="query">查询</button>
      <button class="btn ghost" type="button" data-testid="reset" @click="resetFilters">重置条件</button>
      <button class="btn" type="button" data-testid="run-analysis" @click="runAnalysis">运行分析</button>
    </form>

    <div class="snapshot-bar">
      <span>
        样本口径：<strong data-testid="snapshot-id">{{ snapshot || '新查询' }}</strong>
        · 样本 {{ result.sample_count }} 条 · 分组 {{ result.total }} 个
      </span>
      <span v-if="currentVersion" data-testid="current-version">
        生效版本：v{{ currentVersion.version }}（{{ currentVersion.status }}）
      </span>
      <span v-if="message" class="snapshot-message" data-testid="snapshot-message">{{ message }}</span>
    </div>

    <div v-if="chartData.length" class="analysis-chart">
      <div
        v-for="(bar, i) in chartData"
        :key="i"
        class="analysis-bar"
        :style="{ height: barHeight(bar.value) + 'px' }"
        :title="`${bar.label}：${bar.value}`"
      >
        <span>{{ bar.label }}</span>
      </div>
    </div>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column.key" :class="{ sortable: column.sortable }" @click="toggleSort(column)">
            {{ column.label }}
            <span v-if="sort === column.key">{{ order === 'desc' ? '↓' : '↑' }}</span>
          </th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`">
          <td v-for="column in columns" :key="column.key">{{ formatCell(row, column.key) }}</td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length" class="empty-state">当前筛选条件下暂无实验分组摘要</td>
        </tr>
      </tbody>
    </table>

    <div class="pager">
      <button class="btn ghost" type="button" data-testid="prev-page" :disabled="page <= 1" @click="changePage(page - 1)">
        上一页
      </button>
      <span data-testid="pager-info">第 {{ page }} / {{ totalPages }} 页 · 共 {{ result.total }} 个分组</span>
      <button
        class="btn ghost"
        type="button"
        data-testid="next-page"
        :disabled="page >= totalPages"
        @click="changePage(page + 1)"
      >
        下一页
      </button>
      <select v-model.number="size" data-testid="page-size" @change="changePage(1)">
        <option v-for="option in pageSizes" :key="option" :value="option">{{ option }} 条/页</option>
      </select>
    </div>

    <section class="version-panel">
      <header class="version-head">
        <h3>实验样本摘要版本</h3>
        <button class="btn ghost" type="button" data-testid="refresh-versions" @click="loadVersions">刷新</button>
      </header>
      <table class="data-table">
        <thead>
          <tr>
            <th>版本</th>
            <th>状态</th>
            <th>口径编号</th>
            <th>样本区间</th>
            <th>分组字段</th>
            <th>分组数</th>
            <th>样本数</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="version in versions"
            :key="version.id"
            :data-testid="`version-${version.id}`"
            :data-status="version.status"
          >
            <td>v{{ version.version }}{{ version.main ? '（主版本）' : '' }}</td>
            <td>{{ version.status }}</td>
            <td>{{ version.snapshot_id }}</td>
            <td>{{ version.filters?.['样本区间'] || '全部区间' }}</td>
            <td>{{ version.filters?.['分组字段'] || '—' }}</td>
            <td>{{ version.row_count }}</td>
            <td>{{ version.sample_count }}</td>
            <td class="row-actions">
              <button
                v-if="version.status === '冲突待核对'"
                class="link"
                type="button"
                :data-testid="`resolve-${version.id}`"
                @click="resolveConflict(version, '采纳')"
              >
                采纳
              </button>
              <button v-else-if="!version.main" class="link" type="button" :data-testid="`activate-${version.id}`" @click="activateVersion(version)">
                回退到此版本
              </button>
            </td>
          </tr>
          <tr v-if="!versions.length">
            <td colspan="8" class="empty-state">暂无摘要版本，运行分析或保存摘要后会生成版本</td>
          </tr>
        </tbody>
      </table>
      <p v-if="conflicts.length" class="error-text" data-testid="conflict-hint">
        有 {{ conflicts.length }} 条冲突待核对，采纳或弃用后再继续导出。
      </p>
    </section>

    <footer class="page-foot">
      <span>共 {{ result.total }} 条实验分组摘要 · 样本口径 {{ snapshot || '待查询' }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>
type Column = { key: string; label: string; sortable?: boolean; percent?: boolean }

const ENDPOINT = '/api/experiment'
const columns: Column[] = [
  { key: '方案编号', label: '方案编号', sortable: true },
  { key: '实验名称', label: '实验名称', sortable: true },
  { key: '样本区间', label: '样本区间', sortable: true },
  { key: '分组字段', label: '分组字段', sortable: true },
  { key: '分组值', label: '分组值', sortable: true },
  { key: '样本量', label: '样本量', sortable: true },
  { key: '样本占比', label: '样本占比', percent: true },
  { key: '指标均值', label: '指标均值', sortable: true },
  { key: '对比基准', label: '对比基准', sortable: true },
  { key: '差异', label: '差异', sortable: true },
]
const pageSizes = [10, 20, 50]

const sampleRanges = ref<string[]>(['最近7天', '最近30天', '最近90天'])
const groupFields = ref<string[]>(['区域', '类型', '渠道'])
const summaryMetrics = ref<{ label: string; value: number; trend: number }[]>([])
const rows = ref<Row[]>([])
const result = ref<Record<string, any>>({ total: 0, sample_count: 0 })
const versions = ref<Row[]>([])
const conflicts = ref<Row[]>([])
const currentVersion = ref<Row | null>(null)
const snapshot = ref('')
const filters = reactive<Record<string, string>>({ 样本区间: '', 分组字段: '区域', 方案编号: '' })
const page = ref(1)
const size = ref(10)
const sort = ref('')
const order = ref('asc')
const message = ref('')
const errorMessage = ref('')
const chartData = ref<{ label: string; value: number }[]>([])

const totalPages = computed(() => Math.max(1, Math.ceil(Number(result.value.total || 0) / size.value)))

function barHeight(value: number) {
  const max = Math.max(...chartData.value.map((item) => item.value), 1)
  return Math.max(10, (value / max) * 140)
}

function formatCell(row: Row, key: string) {
  const column = columns.find((item) => item.key === key)
  const value = row[key]
  if (value === null || value === undefined || value === '') return '—'
  if (column?.percent && typeof value === 'number') return `${(value * 100).toFixed(1)}%`
  return value
}

function buildQuery(extra: Record<string, string> = {}) {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(filters)) {
    if (String(value ?? '').trim()) params.set(key, String(value).trim())
  }
  if (snapshot.value) params.set('snapshot', snapshot.value)
  params.set('page', String(page.value))
  params.set('size', String(size.value))
  if (sort.value) {
    params.set('sort', sort.value)
    params.set('order', order.value)
  }
  for (const [key, value] of Object.entries(extra)) params.set(key, value)
  return params.toString()
}

function refreshChart() {
  chartData.value = rows.value.slice(0, 8).map((row) => ({
    label: String(row['分组值'] ?? row['分组'] ?? ''),
    value: Number(row['样本量'] ?? 0),
  }))
}

function applyResult(payload: Row) {
  rows.value = payload.items ?? payload.groups ?? []
  result.value = payload
  snapshot.value = String(payload.snapshot || payload.snapshot_id || '')
  page.value = Number(payload.page || page.value)
  size.value = Number(payload.size || size.value)
  refreshChart()
}

async function reload() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}?${buildQuery()}`)
    if (!response.ok) throw new Error('实验分组摘要读取失败')
    applyResult(await response.json())
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '实验分组摘要读取失败'
  }
}

async function applyFilters() {
  snapshot.value = ''
  page.value = 1
  await reload()
}

async function resetFilters() {
  filters['样本区间'] = ''
  filters['分组字段'] = groupFields.value[0] || '区域'
  filters['方案编号'] = ''
  sort.value = ''
  order.value = 'asc'
  message.value = ''
  await applyFilters()
}

async function changePage(target: number) {
  if (target < 1 || target > totalPages.value) return
  page.value = target
  await reload()
}

function toggleSort(column: Column) {
  if (!column.sortable) return
  if (sort.value === column.key) {
    order.value = order.value === 'asc' ? 'desc' : 'asc'
  } else {
    sort.value = column.key
    order.value = 'asc'
  }
  void reload()
}

async function runAnalysis() {
  message.value = ''
  errorMessage.value = ''
  try {
    const values: Record<string, string> = {}
    for (const [key, value] of Object.entries(filters)) {
      if (String(value ?? '').trim()) values[key] = String(value).trim()
    }
    const response = await request(`${ENDPOINT}/run`, {
      method: 'POST',
      body: JSON.stringify({ values }),
    })
    if (!response.ok) throw new Error('实验分析执行失败')
    const payload = await response.json()
    applyResult(payload)
    message.value = String(payload.message || '')
    currentVersion.value = payload.version ?? null
    await Promise.all([loadVersions(), loadSummary()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '实验分析执行失败'
  }
}

async function persistSummary() {
  message.value = ''
  errorMessage.value = ''
  try {
    const values: Record<string, string> = {}
    for (const [key, value] of Object.entries(filters)) {
      if (String(value ?? '').trim()) values[key] = String(value).trim()
    }
    const response = await request(`${ENDPOINT}/summary`, {
      method: 'POST',
      body: JSON.stringify({
        values,
        snapshot: snapshot.value || null,
        base_version: currentVersion.value?.version ?? null,
      }),
    })
    if (!response.ok) throw new Error('摘要版本保存失败')
    const payload = await response.json()
    message.value = String(payload.message || '')
    if (payload.version) currentVersion.value = payload.version
    await Promise.all([loadVersions(), loadSummary()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '摘要版本保存失败'
  }
}

async function activateVersion(version: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/versions/${version.id}/activate`, { method: 'POST' })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '版本回退失败')
    snapshot.value = String(version.snapshot_id || payload.version?.snapshot_id || '')
    page.value = 1
    message.value = String(payload.message || '')
    await Promise.all([reload(), loadVersions()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '版本回退失败'
  }
}

async function resolveConflict(version: Row, action: string) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/conflicts/${version.id}/resolve`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '冲突核对失败')
    message.value = String(payload.message || '')
    await Promise.all([loadVersions(), loadConflicts()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '冲突核对失败'
  }
}

async function exportRows() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/export?${buildQuery()}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'experiment.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

async function loadSummary() {
  try {
    const response = await request(`${ENDPOINT}/summary`)
    if (!response.ok) return
    summaryMetrics.value = (await response.json()).metrics || []
  } catch {
    summaryMetrics.value = []
  }
}

async function loadVersions() {
  try {
    const response = await request(`${ENDPOINT}/versions`)
    if (!response.ok) return
    const payload = await response.json()
    versions.value = payload.items || []
    currentVersion.value = payload.main ?? currentVersion.value
  } catch {
    versions.value = []
  }
}

async function loadConflicts() {
  try {
    const response = await request(`${ENDPOINT}/conflicts`)
    if (!response.ok) return
    conflicts.value = (await response.json()).items || []
  } catch {
    conflicts.value = []
  }
}

async function loadMetadata() {
  try {
    const response = await request(`${ENDPOINT}/metadata`)
    if (!response.ok) return
    const payload = await response.json()
    sampleRanges.value = payload.sample_ranges || sampleRanges.value
    groupFields.value = payload.group_fields || groupFields.value
    filters['分组字段'] = filters['分组字段'] || payload.defaults?.['分组字段'] || '区域'
  } catch {
    // 元数据读取失败时保留内置选项，不阻塞列表加载。
  }
}

onMounted(async () => {
  await loadMetadata()
  await Promise.all([loadSummary(), loadVersions(), loadConflicts(), reload()])
})
</script>
