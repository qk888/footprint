<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { useMessage, useDialog } from 'naive-ui'
import { plansApi } from '@/api/plans'
import { apiError } from '@/api/http'
import type { PlanRecordOut } from '@/types'

const route = useRoute()
const message = useMessage()
const dialog = useDialog()

const adcode = ref((route.query.adcode as string) || '')
const city = ref((route.query.city as string) || '')
// 同一城市可能有多份规划（对话里说「新建一份」就会多一份）→ 这里要能切换编辑
const plans = ref<PlanRecordOut[]>([])
const currentId = ref(0)
const content = ref('')
const loading = ref(false)
const saving = ref(false)

const currentIndex = computed(() => plans.value.findIndex((p) => p.id === currentId.value))
// 编号按**创建顺序**（id 升序）固定下来，不随更新时间变。
// 之前是按列表位置编号，而列表按 update_time 倒序 —— 于是编辑"第1份"保存后它跳到最新位、
// 其余每份的编号跟着变，用户会以为自己改错了行（实测过：保存一份，三份的编号全变）。
const labelOf = (i: number) => `第${i + 1}份`
// 最近更新过的那份单独打标，避免"最新"这个词同时表示"位置"和"新旧"
const latestId = computed(() => {
  let best = 0
  let bestT = ''
  for (const p of plans.value) {
    if ((p.update_time || '') >= bestT) {
      bestT = p.update_time || ''
      best = p.id
    }
  }
  return best
})
const brief = (p: PlanRecordOut) => {
  const first = (p.content || '').split('\n')[0] || '（空）'
  const t = (p.update_time || '').replace('T', ' ').slice(5, 16)
  return `${first.slice(0, 20)} · ${t}`
}

async function loadPlans() {
  if (!adcode.value) return
  loading.value = true
  try {
    const res = await plansApi.list()
    plans.value = (res.data || [])
      .filter((p) => p.adcode === adcode.value)
      .sort((a, b) => a.id - b.id)          // 创建顺序：编号才稳定
    // 保留当前选择（保存完刷新时不跳走）；没有/被删了就选最新的那份
    if (!plans.value.some((p) => p.id === currentId.value)) {
      currentId.value = plans.value[0]?.id || 0
    }
    content.value = plans.value.find((p) => p.id === currentId.value)?.content || ''
  } catch (e) {
    message.error(apiError(e, '加载规划失败'))
  } finally {
    loading.value = false
  }
}

function selectPlan(id: number) {
  currentId.value = id
  content.value = plans.value.find((p) => p.id === id)?.content || ''
}

/** 覆盖当前这一份（带 plan_id，避免改到别的份上） */
async function save() {
  if (!adcode.value || saving.value) return
  saving.value = true
  try {
    if (currentId.value) {
      await plansApi.set(adcode.value, content.value, 'overwrite', currentId.value)
      message.success('已覆盖这一份规划')
    } else {
      await plansApi.set(adcode.value, content.value, 'new')
      message.success('已新建规划')
    }
    await loadPlans()
  } catch (e) {
    message.error(apiError(e, '保存失败'))
  } finally {
    saving.value = false
  }
}

/** 另存为新的一份（不动原来那份） */
async function saveAsNew() {
  if (!adcode.value || saving.value) return
  saving.value = true
  try {
    await plansApi.set(adcode.value, content.value, 'new')
    message.success('已另存为新的一份规划')
    currentId.value = 0
    await loadPlans()
  } catch (e) {
    message.error(apiError(e, '保存失败'))
  } finally {
    saving.value = false
  }
}

function removePlan() {
  if (!currentId.value) return
  dialog.warning({
    title: '删除这一份规划？',
    content: '删掉后无法恢复；该城市的其他份不受影响。',
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await plansApi.remove(currentId.value)
        message.success('已删除')
        currentId.value = 0
        await loadPlans()
      } catch (e) {
        message.error(apiError(e, '删除失败'))
      }
    },
  })
}

watch(
  () => route.query.adcode,
  (v) => {
    adcode.value = (v as string) || ''
    city.value = (route.query.city as string) || ''
    currentId.value = 0
    loadPlans()
  },
)

onMounted(loadPlans)
</script>

<template>
  <div class="plans">
    <div v-if="!adcode" class="hint ft-card">
      <div class="hint-icon">
        <svg viewBox="0 0 24 24" width="38" height="38" aria-hidden="true">
          <rect x="3.5" y="5.5" width="17" height="15" rx="3" fill="none" stroke="var(--ft-ink-2)" stroke-width="1.4" />
          <path d="M3.5 10.5h17M8 3.5v3M16 3.5v3" fill="none" stroke="var(--ft-ink-2)" stroke-width="1.4" stroke-linecap="round" />
          <circle cx="12" cy="15.4" r="2" fill="var(--ft-accent)" />
        </svg>
      </div>
      <p>从「地图」页选中一座城市，点击「规划」进入。</p>
      <p class="sub">也可以直接告诉 AI 助手你的出行需求，让它生成行程。</p>
    </div>

    <div v-else class="editor ft-card">
      <div class="head">
        <span class="city">{{ city || '城市' }} 行程规划</span>
        <span class="adcode">adcode · {{ adcode }}</span>
        <span v-if="plans.length > 1" class="count">本市共 {{ plans.length }} 份</span>
      </div>

      <div v-if="plans.length > 1" class="picker">
        <button
          v-for="(p, i) in plans"
          :key="p.id"
          class="pick ft-btn"
          :class="{ on: p.id === currentId }"
          @click="selectPlan(p.id)"
        >
          {{ labelOf(i) }}<span v-if="p.id === latestId" class="latest">·最近更新</span>：{{ brief(p) }}
        </button>
      </div>

      <textarea
        v-model="content"
        class="ft-input area ft-scroll"
        :disabled="loading"
        placeholder="写下你的行程安排，例如：&#10;Day1 西湖环湖&#10;Day2 灵隐寺 + 龙井&#10;Day3 河坊街购物"
      ></textarea>

      <div class="actions">
        <span class="tip">
          <template v-if="plans.length > 1 && currentIndex >= 0">
            「保存计划」会覆盖第 {{ currentIndex + 1 }} 份
          </template>
          <template v-else-if="plans.length === 1">「保存计划」会覆盖这一份</template>
        </span>
        <button
          class="ft-btn ghost"
          :disabled="saving || loading || !currentId"
          @click="removePlan"
        >
          删除这份
        </button>
        <button class="ft-btn" :disabled="saving || loading" @click="saveAsNew">
          另存为新规划
        </button>
        <button class="ft-btn" :disabled="saving || loading" @click="save">
          {{ saving ? '保存中…' : '保存计划' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.plans {
  display: grid;
  place-items: center;
  height: 100%;
  padding: 24px;
}
.hint {
  max-width: 420px;
  padding: 32px;
  text-align: center;
  color: var(--c-grey-text);
}
.hint-icon {
  font-size: 40px;
  margin-bottom: 12px;
}
.hint .sub {
  font-size: 13px;
}
.editor {
  width: 100%;
  max-width: 720px;
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.head {
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.city {
  font-size: 18px;
  font-weight: 800;
}
.adcode {
  font-size: 12px;
  color: var(--c-grey-text);
}
.count {
  margin-left: auto;
  font-size: 12px;
  font-weight: 700;
  color: var(--c-grey-text);
}
/* 同城多份时的切换条：横向可滚，不换行。
   按钮不钳宽（max-width+ellipsis 会把标题裁一半，实测"总花费约¥2325·("直接断掉），
   文字保持完整，看不完的部分整条横向滑 */
.picker {
  display: flex;
  gap: 8px;
  overflow-x: auto;
  padding-bottom: 4px;
}
.pick {
  flex-shrink: 0;
  font-size: 12px;
  opacity: 0.6;
  white-space: nowrap;    /* 不加会把"第1份"竖着折行（实测过） */
}
.pick.on {
  opacity: 1;
  text-decoration: underline;
}
.latest {
  color: var(--c-grey-text);
  font-size: 11px;
}
.area {
  min-height: 320px;
  resize: vertical;
  line-height: 1.6;
}
.actions {
  display: flex;
  align-items: center;
  gap: 10px;
}
.tip {
  flex: 1;
  font-size: 12px;
  color: var(--c-grey-text);
}
.ghost {
  opacity: 0.75;
}
</style>
