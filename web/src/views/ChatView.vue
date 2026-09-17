<script setup lang="ts">
import { ref, computed, nextTick, onMounted } from 'vue'
import { useDialog } from 'naive-ui'
import { chatStream } from '@/api/agent'
import type { HistoryMsg } from '@/api/agent'
import { stripPlan, extractPlan, hasPlanMarker, PLAN_BROKEN_TEXT } from '@/utils/plan'
import { useAuthStore } from '@/stores/auth'
import type { Plan } from '@/types'
import ItineraryCard from '@/components/ItineraryCard.vue'

interface ChatMsg {
  text: string
  isUser: boolean
  plan?: Plan | null
}

interface ChatSession {
  id: string
  title: string
  /** 用户手动改过名字: 之后不再用首句话自动覆盖 */
  custom?: boolean
  createdAt: number
  updatedAt: number
  messages: ChatMsg[]
}

// 按用户隔离: 同一浏览器切换账号不能看到上一个账号的对话
const UID = useAuthStore().user?.id ?? 'anon'
const SESSIONS_KEY = `footprint_chat_sessions_${UID}`
const ACTIVE_KEY = `footprint_chat_active_${UID}`
const LEGACY_KEY = `footprint_chat_messages_${UID}` // 单会话时代的旧记录, 首次进来迁移一次

const dialog = useDialog()

const sessions = ref<ChatSession[]>([])
const activeId = ref('')
const showList = ref(false)
const input = ref('')
const loading = ref(false)
const listEl = ref<HTMLDivElement | null>(null)
const inputEl = ref<HTMLInputElement | null>(null)
const editingId = ref('')
const draft = ref('')

const active = computed(() => sessions.value.find((s) => s.id === activeId.value) ?? null)
const messages = computed<ChatMsg[]>(() => active.value?.messages ?? [])

function newId() {
  return `c${Date.now()}${Math.random().toString(36).slice(2, 6)}`
}

function titleFrom(list: ChatMsg[]) {
  const first = list.find((m) => m.isUser && m.text.trim())
  if (!first) return '新对话'
  const t = first.text.trim()
  return t.length > 14 ? `${t.slice(0, 14)}…` : t
}

function persist() {
  localStorage.setItem(SESSIONS_KEY, JSON.stringify(sessions.value))
  localStorage.setItem(ACTIVE_KEY, activeId.value)
}

function newSession(activate = true) {
  const s: ChatSession = {
    id: newId(),
    title: '新对话',
    createdAt: Date.now(),
    updatedAt: Date.now(),
    messages: [],
  }
  sessions.value.unshift(s)
  if (activate) activeId.value = s.id
  return s
}

function loadAll() {
  try {
    const raw = localStorage.getItem(SESSIONS_KEY)
    if (raw) {
      const list = JSON.parse(raw) as ChatSession[]
      if (Array.isArray(list)) sessions.value = list.filter((s) => s && s.id)
    }
  } catch {
    /* 忽略损坏的本地记录 */
  }

  // 老版本只存了一份 messages: 迁移成第一个会话, 不丢历史
  if (!sessions.value.length) {
    let legacy: ChatMsg[] = []
    try {
      const raw = localStorage.getItem(LEGACY_KEY)
      if (raw) legacy = JSON.parse(raw) as ChatMsg[]
    } catch {
      /* 忽略 */
    }
    const s = newSession(true)
    if (Array.isArray(legacy) && legacy.length) {
      s.messages = legacy
      s.title = titleFrom(legacy)
      localStorage.removeItem(LEGACY_KEY)
    }
  }

  const saved = localStorage.getItem(ACTIVE_KEY)
  if (saved && sessions.value.some((s) => s.id === saved)) activeId.value = saved
  if (!activeId.value) activeId.value = sessions.value[0]?.id ?? newSession(true).id
  showList.value = sessions.value.length > 1
}

// 贴底策略：只有用户本来就在底部时才自动跟随新消息；
// 用户往上翻看历史时绝不抢滚动位置（否则流式输出每来一个字就把他拽回底部）
const stick = ref(true)

function atBottom() {
  const el = listEl.value
  if (!el) return true
  return el.scrollHeight - el.scrollTop - el.clientHeight < 64
}

function onListScroll() {
  stick.value = atBottom()
}

async function scrollDown(force = false) {
  await nextTick()
  const el = listEl.value
  if (!el) return
  if (force) stick.value = true
  if (stick.value) el.scrollTop = el.scrollHeight
}

function toLatest() {
  stick.value = true
  scrollDown(true)
}

function onNewChat() {
  newSession(true)
  showList.value = sessions.value.length > 1
  persist()
  scrollDown(true)
  nextTick(() => inputEl.value?.focus())
}

function switchTo(id: string) {
  if (id === activeId.value) return
  activeId.value = id
  persist()
  scrollDown(true)
}

function removeSession(id: string) {
  const s = sessions.value.find((x) => x.id === id)
  if (!s) return
  dialog.warning({
    title: '删除这段对话',
    content: `「${s.title}」的聊天记录将被删除，删除后无法恢复。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: () => {
      sessions.value = sessions.value.filter((x) => x.id !== id)
      if (!sessions.value.length) newSession(true)
      else if (activeId.value === id) activeId.value = sessions.value[0].id
      showList.value = sessions.value.length > 1
      persist()
    },
  })
}

function startRename(id: string) {
  const s = sessions.value.find((x) => x.id === id)
  if (!s) return
  editingId.value = id
  draft.value = s.title
  nextTick(() => {
    const el = document.querySelector<HTMLInputElement>('.s-input')
    el?.focus()
    el?.select()
  })
}

function commitRename() {
  const s = sessions.value.find((x) => x.id === editingId.value)
  if (s) {
    const t = draft.value.trim()
    if (t) {
      s.title = t.slice(0, 24)
      s.custom = true
      s.updatedAt = Date.now()
    } else if (!s.custom) {
      s.title = titleFrom(s.messages) // 清空则退回自动标题
    }
    persist()
  }
  editingId.value = ''
}

function cancelRename() {
  editingId.value = ''
}

async function sendAndRender() {
  const text = input.value.trim()
  const s = active.value
  if (!text || loading.value || !s) return
  input.value = ''

  // 发送前, 本会话已有消息就是历史: 过滤空文本、映射角色、只带最近20条(约10轮, 后端还会按字数兜底裁剪)
  const history: HistoryMsg[] = s.messages
    .filter((m) => m.text && m.text.trim())
    .slice(-20)
    .map((m) => ({ role: m.isUser ? ('user' as const) : ('assistant' as const), content: m.text }))

  s.messages.push({ text, isUser: true })
  const aiIndex = s.messages.length
  s.messages.push({ text: '', isUser: false })
  if (!s.custom) s.title = titleFrom(s.messages)
  s.updatedAt = Date.now()
  loading.value = true
  // 自己发了消息：无论之前翻到哪，都跳到底部看自己的话
  await scrollDown(true)

  try {
    const full = await chatStream(
      text,
      (acc) => {
        s.messages[aiIndex] = { text: stripPlan(acc), isUser: false }
        scrollDown()
      },
      undefined,
      history,
      s.id,
    )
    let plan: Plan | null = null
    let finalText = full
    if (hasPlanMarker(full)) {
      plan = extractPlan(full)
      finalText = plan ? stripPlan(full) : PLAN_BROKEN_TEXT
    }
    s.messages[aiIndex] = { text: finalText.trim(), isUser: false, plan }
  } catch (e) {
    s.messages.pop()
    s.messages.push({
      text: (e as Error).message || '连接不上服务器，请检查网络后再试',
      isUser: false,
    })
  } finally {
    loading.value = false
    s.updatedAt = Date.now()
    await scrollDown()
    persist()
  }
}

onMounted(async () => {
  loadAll()
  await scrollDown(true)
})
</script>

<template>
  <div class="chat-view">
    <header class="chat-head">
      <span class="title">足迹 · AI 旅行助手</span>
      <button
        v-if="sessions.length > 1"
        class="ft-btn ft-btn--ghost hist"
        :class="{ on: showList }"
        @click="showList = !showList"
      >
        会话 {{ sessions.length }}
      </button>
      <button class="ft-btn ft-btn--ghost clear" @click="onNewChat">＋ 新建对话</button>
    </header>

    <div v-if="showList && sessions.length > 1" class="sessions ft-scroll">
      <div
        v-for="s in sessions"
        :key="s.id"
        class="session"
        :class="{ active: s.id === activeId, editing: s.id === editingId }"
        @click="switchTo(s.id)"
        @dblclick="startRename(s.id)"
      >
        <input
          v-if="s.id === editingId"
          v-model="draft"
          class="s-input"
          maxlength="24"
          placeholder="给这段对话起个名"
          @click.stop
          @keyup.enter="commitRename"
          @keyup.esc="cancelRename"
          @blur="commitRename"
        />
        <template v-else>
          <span class="s-title">{{ s.title }}</span>
          <span class="s-count">{{ s.messages.filter((m) => m.isUser).length }}</span>
          <span class="s-edit" title="重命名（也可双击）" @click.stop="startRename(s.id)">✎</span>
          <span class="s-del" title="删除这段对话" @click.stop="removeSession(s.id)">✕</span>
        </template>
      </div>
    </div>

    <div ref="listEl" class="chat-list ft-scroll" @scroll="onListScroll">
      <div v-if="!messages.length" class="empty">
        <div class="empty-icon">
          <svg viewBox="0 0 72 40" width="132" height="74" aria-hidden="true">
            <path
              d="M9 31C21 31 25 11 42 11"
              stroke="var(--ft-line-strong)"
              stroke-width="1.5"
              stroke-dasharray="3 3"
              stroke-linecap="round"
              fill="none"
            />
            <ellipse cx="10" cy="30" rx="5.6" ry="7.6" fill="var(--ft-ink)" />
            <ellipse cx="45" cy="9.5" rx="4.6" ry="6.2" fill="var(--ft-accent)" />
          </svg>
        </div>
        <p>想去哪儿玩？告诉我出发地、目的地、天数和预算，</p>
        <p>我来帮你规划行程。</p>
      </div>

      <template v-for="(m, i) in messages" :key="i">
        <ItineraryCard v-if="m.plan" :plan="m.plan" class="msg-card" />
        <div v-else class="bubble" :class="m.isUser ? 'me' : 'ai'">
          {{ m.text || '…' }}
        </div>
      </template>

      <!-- 往上翻看历史时，AI 来了新回复不抢滚动位置，用这个按钮一键回到最新 -->
      <button v-if="!stick && messages.length" class="to-latest ft-btn" @click="toLatest">
        ↓ 回到最新
      </button>
    </div>

    <footer class="chat-input">
      <input
        ref="inputEl"
        v-model="input"
        class="ft-input"
        type="text"
        placeholder="输入消息，回车发送…"
        :disabled="loading"
        @keyup.enter="sendAndRender"
      />
      <button class="ft-btn send" :disabled="loading || !input.trim()" @click="sendAndRender">
        {{ loading ? '思考中…' : '发送' }}
      </button>
    </footer>
  </div>
</template>

<style scoped>
.chat-view {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  overflow: hidden;
  background: var(--c-surface);
}
.chat-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
  padding: 14px 20px;
  background: var(--c-white);
  border-bottom: var(--border-w) solid var(--c-black);
}
.title {
  font-family: var(--font-display);
  font-size: 18px;
  font-weight: 500;
  letter-spacing: 1px;
}
.hist,
.clear {
  padding: 6px 14px;
  font-size: 13px;
}
.hist {
  margin-left: auto;
}
.hist.on {
  color: var(--c-white);
  background: var(--c-black);
}
.clear {
  margin-left: 0;
}
.sessions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
  padding: 10px 20px;
  overflow-x: auto;
  background: var(--c-white);
  border-bottom: var(--border-w) solid var(--c-black);
}
.session {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 6px;
  max-width: 200px;
  padding: 6px 10px;
  font-size: 13px;
  color: var(--c-black);
  background: var(--c-grey-light);
  border: 1.5px solid var(--c-grey-medium);
  border-radius: 999px;
}
.session {
  cursor: pointer;
}
.session.active {
  color: var(--c-white);
  background: var(--c-black);
  border-color: var(--c-black);
}
.session.editing {
  background: var(--c-white);
  border-color: var(--c-black);
}
.s-title {
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.s-count {
  flex-shrink: 0;
  font-size: 11px;
  opacity: 0.65;
}
.s-del,
.s-edit {
  flex-shrink: 0;
  padding: 0 2px;
  font-size: 12px;
  opacity: 0.7;
}
.s-del:hover,
.s-edit:hover {
  opacity: 1;
}
.s-input {
  width: 132px;
  padding: 2px 8px;
  font-size: 13px;
  color: var(--c-black);
  background: var(--c-white);
  border: 1.5px solid var(--c-black);
  border-radius: 999px;
  outline: none;
}
.chat-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
/* 列表是纵向 flex 容器：子项默认 flex-shrink:1，而行程卡片自己带 overflow:hidden，
   会让它的"自动最小尺寸"变成 0 → 消息一多卡片就被压扁(实测 618px 压到 78px)、行程被截断。
   所以子项一律禁止压缩，超出就交给列表滚动。 */
.chat-list > * {
  flex-shrink: 0;
}
.empty {
  margin: auto;
  text-align: center;
  color: var(--c-grey-text);
  font-size: 14px;
}
.empty-icon {
  font-size: 40px;
  margin-bottom: 10px;
}
.empty p {
  margin: 2px 0;
}
.to-latest {
  position: sticky;
  bottom: 4px;
  align-self: center;
  flex-shrink: 0;
  padding: 6px 14px;
  font-size: 12px;
}
.msg-card {
  align-self: stretch;
  max-width: 640px;
  margin: 0 auto;
  width: 100%;
}
.bubble {
  max-width: 70%;
  padding: 12px 16px;
  font-size: 15px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  border: var(--border-w) solid var(--c-black);
}
.bubble.me {
  align-self: flex-end;
  color: var(--c-white);
  background: var(--c-black);
  border-radius: 16px 16px 4px 16px;
}
.bubble.ai {
  align-self: flex-start;
  color: var(--c-black);
  background: var(--c-white);
  border-radius: 16px 16px 16px 4px;
  box-shadow: var(--shadow-hard-sm);
}
.chat-input {
  display: flex;
  gap: 12px;
  flex-shrink: 0;
  padding: 14px 20px;
  background: var(--c-white);
  border-top: var(--border-w) solid var(--c-black);
}
.chat-input .ft-input {
  flex: 1;
}
.send {
  flex-shrink: 0;
  min-width: 96px;
}
</style>
