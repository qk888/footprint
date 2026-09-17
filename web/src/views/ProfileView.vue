<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useDialog, useMessage } from 'naive-ui'
import { useAuthStore } from '@/stores/auth'
import { useCitiesStore } from '@/stores/cities'
import { memoryApi } from '@/api/memory'
import { apiError } from '@/api/http'
import { MEMORY_KIND_MAP } from '@/types'
import type { MemoryOut, MemoryStats } from '@/types'

const auth = useAuthStore()
const cities = useCitiesStore()
const router = useRouter()
const message = useMessage()
const dialog = useDialog()

const litCount = computed(() => cities.lit.size)

const memories = ref<MemoryOut[]>([])
const loading = ref(false)
const saving = ref(false)
const draft = ref('')
//记忆体检：条数 + 语义检索是否已启用
const memStats = ref<MemoryStats | null>(null)

async function loadStats() {
  try {
    const { data } = await memoryApi.stats()
    memStats.value = data
  } catch {
    // 体检信息拿不到不影响记忆列表本身
  }
}

async function loadMemories() {
  loading.value = true
  try {
    const { data } = await memoryApi.list()
    memories.value = data.memories
    loadStats()
  } catch (e) {
    message.error(apiError(e, '记忆加载失败'))
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  if (!cities.lit.size) cities.fetchLit().catch(() => {})
  loadMemories()
})

function kindLabel(kind: string) {
  return MEMORY_KIND_MAP[kind] ?? kind
}

// 排序: 待确认(AI 自己记的候选) → 生效中 → 已失效(只留历史)
const sortedMemories = computed(() => {
  const weight = (m: MemoryOut) => (m.status === 'superseded' ? 2 : m.needs_review ? 0 : 1)
  return [...memories.value].sort(
    (a, b) => weight(a) - weight(b) || (a.update_time < b.update_time ? 1 : -1),
  )
})

const pendingCount = computed(
  () => memories.value.filter((m) => m.needs_review && m.status !== 'superseded').length,
)

async function confirmMemory(item: MemoryOut) {
  try {
    await memoryApi.confirm(item.id)
    item.needs_review = false
    message.success('已确认，之后会自动带给 AI')
  } catch (e) {
    message.error(apiError(e, '确认失败'))
  }
}

function fmtDate(value: string) {
  return value.slice(0, 10)
}

async function addMemory() {
  const content = draft.value.trim()
  if (!content) return
  saving.value = true
  try {
    await memoryApi.add(content)
    draft.value = ''
    await loadMemories()
  } catch (e) {
    message.error(apiError(e, '添加失败'))
  } finally {
    saving.value = false
  }
}

async function removeMemory(item: MemoryOut) {
  try {
    await memoryApi.remove(item.id)
    memories.value = memories.value.filter((m) => m.id !== item.id)
  } catch (e) {
    message.error(apiError(e, '删除失败'))
  }
}

function confirmClear() {
  dialog.warning({
    title: '清空记忆',
    content: `AI 会忘记全部 ${memories.value.length} 条记忆（含偏好和自动记录的城市、消费），确定继续？`,
    positiveText: '全部清空',
    negativeText: '再想想',
    onPositiveClick: async () => {
      try {
        await memoryApi.removeAll()
        memories.value = []
        message.success('已清空')
      } catch (e) {
        message.error(apiError(e, '清空失败'))
      }
    },
  })
}

function onLogout() {
  auth.logout()
  router.push({ name: 'login' })
}
</script>

<template>
  <div class="profile ft-scroll">
    <div class="card ft-card">
      <div class="avatar">{{ (auth.user?.username || 'U').charAt(0) }}</div>
      <h2>{{ auth.user?.username || '未登录' }}</h2>
      <p class="email">{{ auth.user?.email || '—' }}</p>

      <div class="stat">
        <span class="stat-num">{{ litCount }}</span>
        <span class="stat-label">已点亮城市</span>
      </div>

      <button class="ft-btn ft-btn--ghost logout" @click="onLogout">退出登录</button>
    </div>

    <div class="card ft-card memory">
      <div class="mem-head">
        <h3>AI 记忆</h3>
        <span class="mem-count">{{ memories.length }} 条</span>
        <span v-if="pendingCount" class="mem-flag pending-flag">{{ pendingCount }} 条待确认</span>
        <button v-if="memories.length" class="ft-btn ft-btn--ghost mini" @click="confirmClear">清空</button>
      </div>
      <p class="mem-tip">
        生效中的记忆会在以后的对话里按需带给 AI（跨会话有效）。
        <b>待确认</b>的是 AI 自己记的候选，你确认之后才会自动带上；说了新说法，旧那条会自动标成「已失效」。
      </p>
      <p v-if="memStats" class="mem-tip mem-vector">
        <template v-if="memStats.vector_enabled">
          语义检索已启用：{{ memStats.vector_indexed }} 条已建索引 —— 记忆再多，早先说过的事也能按意思找回来。
        </template>
        <template v-else>
          语义检索待命：生效记忆 {{ memStats.active }}/{{ memStats.vector_min_count }} 条，超过阈值后自动开启按意思召回。
        </template>
      </p>

      <div class="mem-add">
        <input
          v-model="draft"
          class="ft-input"
          maxlength="500"
          placeholder="手动加一条，例如：我不吃辣"
          @keyup.enter="addMemory"
        />
        <button class="ft-btn" :disabled="saving || !draft.trim()" @click="addMemory">添加</button>
      </div>

      <p v-if="loading" class="mem-empty">加载中…</p>
      <p v-else-if="!memories.length" class="mem-empty">
        还没有记忆，聊天时对 AI 说「记住我不吃辣」试试
      </p>
      <ul v-else class="mem-list ft-scroll">
        <li
          v-for="m in sortedMemories"
          :key="m.id"
          class="mem-item"
          :class="{
            pending: m.needs_review && m.status !== 'superseded',
            dead: m.status === 'superseded',
          }"
        >
          <span class="ft-tag">{{ kindLabel(m.kind) }}</span>
          <span class="mem-text">{{ m.content }}</span>
          <span v-if="m.status === 'superseded'" class="mem-flag dead-flag">已失效</span>
          <span v-else-if="m.needs_review" class="mem-flag pending-flag">待确认</span>
          <span class="mem-date">{{ fmtDate(m.update_time) }}</span>
          <button
            v-if="m.needs_review && m.status !== 'superseded'"
            class="ft-btn ft-btn--ghost mini confirm"
            title="确认后这条记忆才会自动带给 AI"
            @click="confirmMemory(m)"
          >
            确认
          </button>
          <button class="mem-del" title="删除这条记忆" @click="removeMemory(m)">×</button>
        </li>
      </ul>
    </div>

    <p class="note">足迹 Footprint · 非商业许可</p>
  </div>
</template>

<style scoped>
.profile {
  display: grid;
  place-items: center;
  align-content: start;
  gap: 16px;
  height: 100%;
  padding: 24px 24px 56px;
  overflow-y: auto;
}
.card {
  width: 100%;
  max-width: 360px;
  padding: 28px;
  text-align: center;
}
.avatar {
  display: inline-grid;
  place-items: center;
  width: 64px;
  height: 64px;
  font-size: 28px;
  font-weight: 800;
  color: var(--c-white);
  background: var(--c-black);
  border-radius: 50%;
  margin-bottom: 12px;
}
.card h2 {
  margin: 0;
  font-size: 20px;
}
.email {
  margin: 4px 0 18px;
  font-size: 13px;
  color: var(--c-grey-text);
}
.stat {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 14px;
  margin-bottom: 18px;
  background: var(--c-grey-light);
  border: 1.5px solid var(--c-grey-medium);
  border-radius: var(--radius-sm);
}
.stat-num {
  font-size: 28px;
  font-weight: 800;
}
.stat-label {
  font-size: 12px;
  color: var(--c-grey-text);
}
.logout {
  width: 100%;
}
.memory {
  max-width: 520px;
  padding: 20px;
  text-align: left;
}
.mem-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.mem-head h3 {
  margin: 0;
  font-size: 16px;
}
.mem-count {
  font-size: 12px;
  color: var(--c-grey-text);
}
.mini {
  margin-left: auto;
  padding: 4px 10px;
  font-size: 12px;
}
.mem-tip {
  margin: 6px 0 14px;
  font-size: 12px;
  color: var(--c-grey-text);
}
.mem-vector {
  margin: 0 0 14px;
  opacity: 0.85;
}
.mem-add {
  display: flex;
  gap: 8px;
  margin-bottom: 14px;
}
.mem-add .ft-input {
  flex: 1;
  min-width: 0;
}
.mem-add .ft-btn {
  padding: 0 14px;
}
.mem-empty {
  margin: 0;
  padding: 16px 0;
  font-size: 13px;
  color: var(--c-grey-medium);
  text-align: center;
}
.mem-list {
  max-height: 260px;
  margin: 0;
  padding: 0;
  overflow-y: auto;
  list-style: none;
}
.mem-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 0;
  border-bottom: 1.5px dashed var(--c-grey-medium);
}
.mem-item:last-child {
  border-bottom: none;
}
.mem-text {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* 待确认的高亮出来, 并且内容允许换行(要看清 AI 到底记了什么) */
.mem-item.pending {
  padding: 8px;
  background: #fdf6d8;
  border-left: 3px solid var(--c-black);
}
.mem-item.pending .mem-text {
  white-space: normal;
}
.mem-item.dead {
  opacity: 0.45;
}
.mem-flag {
  flex-shrink: 0;
  padding: 1px 6px;
  font-size: 11px;
  font-weight: 700;
  color: var(--c-white);
  background: var(--c-black);
  border-radius: 4px;
}
.pending-flag {
  background: #b07d00;
}
.dead-flag {
  background: var(--c-grey-medium);
}
.confirm {
  flex-shrink: 0;
  padding: 2px 8px;
  font-size: 12px;
}
.mem-date {
  font-size: 11px;
  color: var(--c-grey-medium);
}
.mem-del {
  padding: 0 6px;
  font-size: 16px;
  line-height: 1;
  color: var(--c-grey-text);
  background: none;
  border: none;
  cursor: pointer;
}
.mem-del:hover {
  color: var(--c-black);
}
.note {
  position: absolute;
  bottom: 16px;
  font-size: 11px;
  color: var(--c-grey-medium);
  text-align: center;
}
</style>
