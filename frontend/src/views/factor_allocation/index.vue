<template>
  <section class="page" data-module="factor_allocation">
    <header class="page-head">
      <div>
        <h2>因子约束分配</h2>
        <p class="page-desc">
          整组生成因子约束分配表：按批次复用已校验约束，逐条回执并保留缓存批次与规则版本。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="generate" type="button" :disabled="busy" @click="generate()">
          {{ busy ? '生成中…' : '生成分配表' }}
        </button>
        <button class="btn" data-testid="retry" type="button" :disabled="busy" @click="generate('retry')">
          按未完成组补算
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出分配表</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value" :data-testid="`stat-${item.label}`">{{ item.value }}</strong>
      </article>
    </div>

    <section v-if="batch" class="batch-panel" data-testid="batch-panel">
      <div class="batch-line">
        <span>批次号 <strong data-testid="batch-no">{{ batch['缓存批次'] || '—' }}</strong></span>
        <span>状态 <strong :data-testid="'batch-status'">{{ batch['状态'] }}</strong></span>
        <span>范围 {{ scopeLabel(batch['范围']) }}</span>
        <span>复用策略 {{ batch['复用策略说明'] || '—' }}</span>
        <span>规则版本 {{ batch['规则版本'] || '—' }}</span>
        <span>重试 {{ batch['重试次数'] }} 次</span>
      </div>
      <div class="batch-line">
        <span data-testid="completed-groups">已完成组：{{ (batch['已完成因子组'] || []).join('、') || '—' }}</span>
        <span data-testid="pending-groups">未完成组：{{ (batch['未完成因子组'] || []).join('、') || '—' }}</span>
        <span v-if="(batch['补算实验'] || []).length" data-testid="compensated">
          补算实验：{{ batch['补算实验'].join('、') }}
        </span>
      </div>
      <div class="group-grid">
        <article
          v-for="(item, name) in batch['因子组状态'] || {}"
          :key="name"
          class="group-card"
          :data-testid="`group-${name}`"
          :data-group-status="item['状态']"
        >
          <header>
            <strong>{{ name }}</strong>
            <span :class="item['状态'] === '已完成' ? 'chip ok' : 'chip warn'">{{ item['状态'] }}</span>
          </header>
          <dl>
            <dt>规则版本</dt><dd>{{ item['规则版本'] }}</dd>
            <dt>明细数</dt><dd>{{ item['明细数'] }}</dd>
            <dt v-if="item['失败原因']">失败原因</dt><dd v-if="item['失败原因']">{{ item['失败原因'] }}</dd>
          </dl>
        </article>
      </div>
    </section>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>实验编号</span>
        <input v-model="filters['实验编号']" data-testid="filter-实验编号" placeholder="按实验编号检索" />
      </label>
      <label class="filter-item">
        <span>因子编号</span>
        <input v-model="filters['因子编号']" data-testid="filter-因子编号" placeholder="按因子编号检索" />
      </label>
      <label class="filter-item">
        <span>生成状态</span>
        <select v-model="state" data-testid="filter-state">
          <option value="">全部</option>
          <option v-for="status in states" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>详情</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="row in rows"
          :key="String(row.id)"
          :data-testid="`row-${row.id}`"
          :data-status="String(row['生成状态'])"
          :class="{ selected: selected && selected.id === row.id }"
          @click="select(row)"
        >
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button class="link" type="button" :data-testid="`detail-${row.id}`" @click.stop="select(row)">
              查看详情
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">
            暂无分配明细，可先点击「生成分配表」按批次生成
          </td>
        </tr>
      </tbody>
    </table>

    <aside v-if="selected" class="detail-panel" data-testid="allocation-detail">
      <header class="page-head">
        <div>
          <h3>分配明细 · {{ selected['实验编号'] }} / {{ selected['因子编号'] }}</h3>
          <p class="page-desc">列表入口与详情面板读取同一份明细，字段保持一致。</p>
        </div>
        <button class="btn ghost" type="button" data-testid="close-detail" @click="selected = null">关闭</button>
      </header>
      <dl class="detail-grid">
        <template v-for="field in detailFields" :key="field">
          <dt>{{ field }}</dt>
          <dd :data-testid="`detail-${field}`">{{ selected[field] ?? '—' }}</dd>
        </template>
      </dl>
    </aside>

    <footer class="page-foot">
      <span>共 {{ total }} 条分配明细</span>
      <span v-if="message" class="foot-message" data-testid="message">{{ message }}</span>
      <span v-if="errorMessage" class="error-text" data-testid="error">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type GroupState = Record<string, { 状态: string; 规则版本: string; 明细数: number; 失败原因?: string }>
type Batch = {
  批次号: string
  缓存批次: string
  状态: string
  范围: string
  复用策略说明: string
  规则版本: string
  明细总数: number
  已回执数: number
  已完成因子组: string[]
  未完成因子组: string[]
  因子组状态: GroupState
  补算实验: string[]
  重试次数: number
}

const ENDPOINT = '/api/factor_allocation'
const columns = ["分配批次", "实验编号", "因子编号", "因子层级", "取值", "对照比例", "实验比例", "缓存批次", "规则版本", "补算标记"]
const detailFields = [
  "分配批次", "实验编号", "实验名称", "因子编号", "因子名称", "因子层级", "取值", "分配比例",
  "对照比例", "实验比例", "对照样本", "实验样本", "约束校验", "约束摘要",
  "缓存批次", "缓存键", "规则版本", "生成状态", "补算标记",
]
const states = ["已回执", "待复核"]

const rows = ref<Row[]>([])
const total = ref(0)
const batch = ref<Batch | null>(null)
const selected = ref<Row | null>(null)
const filters = ref<Record<string, string>>({})
const state = ref('')
const busy = ref(false)
const message = ref('')
const errorMessage = ref('')

const stats = computed(() => [
  { label: '明细总数', value: total.value },
  { label: '已回执', value: rows.value.filter((row) => row['生成状态'] === '已回执').length },
  { label: '缓存批次', value: batch.value?.['缓存批次'] || '—' },
  { label: '规则版本', value: batch.value?.['规则版本'] || '—' },
])

function scopeLabel(scope: string) {
  return { full: '整批生成', incremental: '增量重算', retry: '按未完成组补算' }[scope] ?? scope ?? '—'
}

function resetFilters() {
  filters.value = {}
  state.value = ''
  void reload()
}

async function loadStatus() {
  try {
    const response = await request(`${ENDPOINT}/status`)
    if (!response.ok) return
    const payload = await response.json()
    batch.value = payload['批次'] ?? null
  } catch {
    // 状态读取失败不阻断列表展示。
  }
}

function select(row: Row) {
  selected.value = row
}

async function generate(scope: 'full' | 'retry' = 'full') {
  busy.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${scope === 'retry' ? 'retry' : 'generate'}`, {
      method: 'POST',
      body: JSON.stringify(scope === 'retry' ? {} : { scope: 'full', strategy: 'reuse' }),
    })
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail || '生成失败')
    const result = payload['批次'] || {}
    const rerun = (payload['本轮重算因子组'] || []).join('、') || '无'
    message.value = scope === 'retry'
      ? `补算完成：${rerun}；已完成组保持不变`
      : `分配表已提交：${result['缓存批次'] ?? '—'}，重算 ${rerun}`
    selected.value = null
    await Promise.all([loadStatus(), reload()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '生成失败'
  } finally {
    busy.value = false
  }
}

async function exportRows() {
  errorMessage.value = ''
  const params = new URLSearchParams({ ...filters.value, ...(state.value ? { state: state.value } : {}) })
  try {
    const response = await request(`${ENDPOINT}/export?${params.toString()}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'factor_allocation.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const params = new URLSearchParams(filters.value as Record<string, string>)
  if (state.value) params.set('state', state.value)
  try {
    const response = await request(`${ENDPOINT}?${params.toString()}`)
    if (!response.ok) throw new Error('分配明细读取失败')
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    if (selected.value) {
      // 详情面板跟随列表刷新，避免两处显示不同版本。
      selected.value = rows.value.find((row) => row.id === selected.value?.id) ?? null
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分配明细读取失败'
  }
}

onMounted(async () => {
  await loadStatus()
  await reload()
})
</script>

<style scoped>
.batch-panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 12px; }
.batch-line { display: flex; flex-wrap: wrap; gap: 16px; font-size: 13px; color: var(--muted); margin-bottom: 8px; }
.batch-line strong { color: #1f2937; }
.group-grid { display: flex; flex-wrap: wrap; gap: 10px; }
.group-card { flex: 1; min-width: 200px; border: 1px solid var(--border); border-radius: 8px; padding: 10px; }
.group-card header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
.group-card dl { display: grid; grid-template-columns: auto 1fr; gap: 4px 10px; margin: 0; font-size: 12px; }
.group-card dt { color: var(--muted); }
.group-card dd { margin: 0; }
.chip { font-size: 12px; border-radius: 10px; padding: 1px 8px; }
.chip.ok { background: #dcfce7; color: #15803d; }
.chip.warn { background: #fef3c7; color: #b45309; }
tr.selected td { background: #eff6ff; }
.detail-panel { margin-top: 16px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 16px; }
.detail-grid { display: grid; grid-template-columns: 120px 1fr 120px 1fr; gap: 6px 12px; font-size: 13px; margin: 0; }
.detail-grid dt { color: var(--muted); }
.detail-grid dd { margin: 0; }
.foot-message { color: #15803d; }
</style>
