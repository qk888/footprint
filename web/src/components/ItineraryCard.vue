<script setup lang="ts">
import { computed, ref } from 'vue'
import type { Plan, ItineraryDay, Activity, Transport } from '@/types'
import { typeLabel, modeLabel, money } from '@/utils/plan'
import { plansApi } from '@/api/plans'

const props = defineProps<{ plan: Plan }>()

const itinerary = computed<ItineraryDay[]>(() => props.plan.itinerary || [])
const warning = computed(() => props.plan.warning || '')

const saving = ref(false)
const saved = ref(false)
const savedMsg = ref('')      // 「新建」和「覆盖」要说清楚是哪一种，否则这个选择等于白给
const saveErr = ref('')
// 该城市已有几份规划；>0 时点保存先问"覆盖还是新建"
const existing = ref(0)
const asking = ref(false)

function dayRoute(day: ItineraryDay): string {
  for (const a of day.activities || []) {
    if (a.start && a.end) return `${a.start}→${a.end}`
  }
  return ''
}

function dayCost(day: ItineraryDay): number {
  let sum = 0
  for (const a of day.activities || []) {
    sum += Number(a.cost) || 0
    for (const t of a.transports || []) sum += Number(t.cost) || 0
  }
  return sum
}

function activityName(a: Activity): string {
  const isIntercity = !!(a.start && a.end)
  if (isIntercity) return a.vehicle ? `${a.vehicle} ${a.start}→${a.end}` : `${a.start}→${a.end}`
  return a.position || ''
}

const summary = computed(
  () =>
    `共${props.plan.days}天 · 出行${props.plan.people}人 · 总花费约${money(props.plan.total_cost)}`,
)

// 把结构化行程转成可读文本(规划页是按城市存文本计划)
function planToText(): string {
  const city = props.plan.city || props.plan.target_city || ''
  const lines: string[] = [`${city} ${props.plan.days}天${props.plan.people}人行程，总花费约${money(props.plan.total_cost)}`]
  for (const day of itinerary.value) {
    lines.push(`第${day.day}天：`)
    for (const a of day.activities || []) {
      const t = a.start_time ? `${a.start_time} ` : ''
      const cost = Number(a.cost) > 0 ? money(a.cost) : '免费'
      lines.push(`  ${t}${typeLabel(a.type)} ${activityName(a)}（${cost}）`)
    }
  }
  if (warning.value) lines.push(`提示：${warning.value}`)
  return lines.join('\n')
}

async function onSaveClick() {
  if (!props.plan.adcode || saving.value) return
  saveErr.value = ''
  try {
    // 先看这个城市有没有旧规划：有就让用户选"覆盖 / 新建一份"
    // —— 以前是静默覆盖，用户不知道原来那份还在不在
    const res = await plansApi.list()
    existing.value = (res.data || []).filter((p) => p.adcode === props.plan.adcode).length
  } catch {
    existing.value = 0
  }
  if (existing.value > 0) {
    asking.value = true
    return
  }
  await doSave('new')
}

async function doSave(mode: 'overwrite' | 'new') {
  if (!props.plan.adcode || saving.value) return
  asking.value = false
  saving.value = true
  saveErr.value = ''
  try {
    await plansApi.set(props.plan.adcode, planToText(), mode)
    saved.value = true
    savedMsg.value = mode === 'new' ? '✓ 已新建一份' : '✓ 已覆盖原规划'
    // 保存完这一份就不再是"没有规划"了，下次点要重新问；否则会把第二份也静默覆盖掉
    existing.value = (existing.value || 0) + (mode === 'new' ? 1 : 0)
    setTimeout(() => (saved.value = false), 3500)
  } catch (e) {
    saveErr.value = (e as Error).message || '保存失败'
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="itinerary ft-card">
    <div v-if="warning" class="warning">
      <span class="warn-icon">ⓘ</span>
      <span>{{ warning }}</span>
    </div>

    <div v-for="(day, di) in itinerary" :key="di" class="day">
      <div class="day-bar">
        <span class="day-no">第{{ day.day }}天</span>
        <span v-if="dayRoute(day)" class="day-route">{{ dayRoute(day) }}</span>
        <span class="day-cost">{{ money(dayCost(day)) }}</span>
      </div>

      <div class="timeline">
        <div
          v-for="(a, ai) in day.activities || []"
          :key="ai"
          class="activity"
          :class="{ last: di === itinerary.length - 1 && ai === (day.activities || []).length - 1 }"
        >
          <div class="rail">
            <span class="dot"></span>
            <span class="line"></span>
          </div>
          <div class="body">
            <div class="row">
              <span class="time">{{ a.start_time || '' }}</span>
              <span class="type-pill">{{ typeLabel(a.type) }}</span>
              <span class="name">{{ activityName(a) }}</span>
              <span class="cost">{{ money(a.cost) }}</span>
            </div>
            <div v-for="(t, ti) in (a.transports || []) as Transport[]" :key="ti" class="transport">
              <span class="t-text">
                {{ t.start_time || '' }} {{ modeLabel(t.mode)
                }}<template v-if="activityName(a)">前往{{ activityName(a) }}</template>
              </span>
              <span v-if="(Number(t.cost) || 0) > 0" class="t-cost">{{ money(t.cost) }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div class="footer">
      <span class="summary">{{ summary }}</span>
      <template v-if="plan.adcode">
        <span v-if="asking" class="ask">这个城市已有 {{ existing }} 份规划：</span>
        <button v-if="asking" class="save-btn" :disabled="saving" @click="doSave('overwrite')">
          覆盖最新一份
        </button>
        <button v-if="asking" class="save-btn" :disabled="saving" @click="doSave('new')">
          新建一份
        </button>
        <button v-if="asking" class="save-btn ghost" :disabled="saving" @click="asking = false">
          取消
        </button>
        <button v-if="!asking" class="save-btn" :disabled="saving" @click="onSaveClick">
          {{ saved ? savedMsg : saving ? '保存中…' : '保存到规划' }}
        </button>
      </template>
    </div>
    <div v-if="saveErr" class="save-err">{{ saveErr }}</div>
  </div>
</template>

<style scoped>
.itinerary {
  overflow: hidden;
  box-shadow: var(--shadow-hard-sm);
  /* 卡片自带 overflow:hidden，作为纵向 flex 子项时"自动最小尺寸"会退化成 0，
     不显式写死就会被压扁、行程内容被截断 */
  flex-shrink: 0;
}
.warning {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  font-size: 12px;
  color: var(--c-grey-text);
  background: var(--c-grey-light);
  border-bottom: 1.5px solid var(--c-surface-variant);
}
.day-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  color: var(--c-white);
  background: var(--c-black);
}
.day-no {
  font-size: 12px;
  font-weight: 700;
}
.day-route {
  font-size: 12px;
  opacity: 0.85;
}
.day-cost {
  margin-left: auto;
  font-size: 12px;
  font-weight: 700;
}
.timeline {
  padding: 10px 12px 4px;
}
.activity {
  display: flex;
  gap: 8px;
}
.rail {
  display: flex;
  flex-direction: column;
  align-items: center;
  width: 14px;
  flex-shrink: 0;
}
.dot {
  width: 8px;
  height: 8px;
  margin-top: 5px;
  background: var(--c-black);
  border-radius: 50%;
  flex-shrink: 0;
}
.line {
  flex: 1;
  width: 2px;
  background: var(--c-black);
}
.activity.last .line {
  display: none;
}
.body {
  flex: 1;
  min-width: 0;
  padding-bottom: 12px;
}
.row {
  display: flex;
  align-items: center;
  gap: 6px;
}
.time {
  width: 40px;
  flex-shrink: 0;
  font-size: 12px;
  font-weight: 600;
}
.type-pill {
  flex-shrink: 0;
  padding: 2px 6px;
  font-size: 10px;
  font-weight: 600;
  color: var(--c-white);
  background: var(--c-black);
  border-radius: 4px;
}
.name {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.cost {
  flex-shrink: 0;
  font-size: 12px;
  font-weight: 700;
}
.transport {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 3px;
  padding-left: 46px;
  font-size: 11px;
  color: var(--c-grey-text);
}
.t-text {
  flex: 1;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.footer {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  background: var(--c-black);
}
.summary {
  flex: 1;
  font-size: 12px;
  font-weight: 700;
  color: var(--c-white);
  text-align: left;
}
.save-btn {
  flex-shrink: 0;
  padding: 4px 12px;
  font-size: 12px;
  font-weight: 700;
  color: var(--c-black);
  background: var(--c-white);
  border: 1.5px solid var(--c-black);
  border-radius: 6px;
  cursor: pointer;
}
.save-btn:hover:not(:disabled) {
  opacity: 0.85;
}
.save-btn:disabled {
  cursor: default;
  opacity: 0.7;
}
/* "覆盖还是新建"的追问用的：反色按钮 + 一句提示 */
.save-btn.ghost {
  color: var(--c-white);
  background: transparent;
}
.ask {
  flex-shrink: 0;
  font-size: 11px;
  color: var(--c-white);
}
.save-err {
  padding: 6px 12px;
  font-size: 12px;
  color: #d33;
  background: var(--c-grey-light);
}
</style>
