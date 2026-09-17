<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { useMessage, NModal, NSelect, NInput, NInputNumber } from 'naive-ui'
import { tripsApi } from '@/api/trips'
import { billsApi } from '@/api/bills'
import { apiError } from '@/api/http'
import { fmtDateTime, fmtTime } from '@/utils/time'
import { CATEGORY_MAP } from '@/types'
import type { TripOut, BillOut } from '@/types'

interface TripWithBills extends TripOut {
  bills: BillOut[]
  total: number
}

const route = useRoute()
const message = useMessage()

const adcode = ref((route.query.adcode as string) || '')
const city = ref((route.query.city as string) || '')
const loading = ref(false)
const trips = ref<TripWithBills[]>([])
const expanded = ref<number | null>(null)

const showBillModal = ref(false)
const saving = ref(false)
const editingBill = ref<BillOut | null>(null)
const billTripId = ref(0)
const billForm = ref({ amount: 0, category: 1, custom_category: '' })

const categoryOptions = Object.entries(CATEGORY_MAP).map(([value, label]) => ({
  label,
  value: Number(value),
}))

function openAddBill(tripId: number) {
  editingBill.value = null
  billTripId.value = tripId
  billForm.value = { amount: 0, category: 1, custom_category: '' }
  showBillModal.value = true
}

function openEditBill(b: BillOut) {
  editingBill.value = b
  billForm.value = {
    amount: Number(b.amount),
    category: b.category,
    custom_category: b.custom_category || '',
  }
  showBillModal.value = true
}

async function saveBill() {
  const { amount, category, custom_category } = billForm.value
  if (!amount || amount <= 0) {
    message.error('金额必须大于 0')
    return
  }
  const payload = {
    amount,
    category,
    custom_category: category === 6 ? custom_category || null : null,
  }
  saving.value = true
  try {
    if (editingBill.value) {
      await billsApi.update(editingBill.value.id, payload)
      message.success('已修改')
    } else {
      await billsApi.add(billTripId.value, payload)
      message.success('已记一笔')
    }
    showBillModal.value = false
    await load()
  } catch (e) {
    message.error(apiError(e, '保存失败'))
  } finally {
    saving.value = false
  }
}

const grandTotal = computed(() => trips.value.reduce((s, t) => s + t.total, 0))

async function load() {
  if (!adcode.value) return
  loading.value = true
  try {
    const res = await tripsApi.list()
    const mine = (res.data.trips || []).filter((t) => t.adcode === adcode.value)
    const withBills = await Promise.all(
      mine.map(async (t): Promise<TripWithBills> => {
        try {
          const b = await billsApi.listByTrip(t.id)
          const bills = b.data.bills || []
          const total = bills.reduce((s, x) => s + (Number(x.amount) || 0), 0)
          return { ...t, bills, total }
        } catch {
          return { ...t, bills: [], total: 0 }
        }
      }),
    )
    trips.value = withBills.sort(
      (a, b) => new Date(b.create_time).getTime() - new Date(a.create_time).getTime(),
    )
  } catch (e) {
    message.error(apiError(e, '加载行程失败'))
  } finally {
    loading.value = false
  }
}

async function addTrip() {
  if (!adcode.value) return
  try {
    await tripsApi.add(adcode.value)
    message.success('已新增一次行程')
    await load()
  } catch (e) {
    message.error(apiError(e, '新增失败'))
  }
}

async function removeTrip(id: number) {
  try {
    await tripsApi.remove(id)
    message.success('已删除行程')
    await load()
  } catch (e) {
    message.error(apiError(e, '删除失败'))
  }
}

function catLabel(b: BillOut) {
  return b.category === 6 && b.custom_category ? b.custom_category : CATEGORY_MAP[b.category] || '其他'
}

// 每笔账单按时间倒序(新记的排最上面), 让"刚记的那笔"一眼能看到
function sortedBills(list: BillOut[]) {
  return [...list].sort((a, b) => {
    const ta = a.create_time ? new Date(a.create_time).getTime() : 0
    const tb = b.create_time ? new Date(b.create_time).getTime() : 0
    return tb - ta || b.id - a.id
  })
}

watch(
  () => route.query.adcode,
  (v) => {
    adcode.value = (v as string) || ''
    city.value = (route.query.city as string) || ''
    load()
  },
)

onMounted(load)
</script>

<template>
  <div class="records">
    <div v-if="!adcode" class="hint ft-card">
      <div class="hint-icon">
        <svg viewBox="0 0 24 24" width="38" height="38" aria-hidden="true">
          <path
            d="M6.2 3.5h10.3c1.1 0 2 .9 2 2v13c0 1.1-.9 2-2 2H6.2c-1.5 0-2.2-.8-2.2-2v-13c0-1.2.7-2 2.2-2Z"
            fill="none"
            stroke="var(--ft-ink-2)"
            stroke-width="1.4"
          />
          <path
            d="M8.6 8.6h6.2M8.6 12h6.2M8.6 15.4h3.6"
            fill="none"
            stroke="var(--ft-ink-2)"
            stroke-width="1.4"
            stroke-linecap="round"
          />
          <circle cx="17.6" cy="7" r="1.6" fill="var(--ft-accent)" />
        </svg>
      </div>
      <p>从「地图」页选中一座已点亮的城市，点击「记录」查看行程与账单。</p>
    </div>

    <div v-else class="panel">
      <div class="panel-head ft-card">
        <div>
          <span class="city">{{ city || '城市' }} · 行程记录</span>
          <span class="adcode">adcode · {{ adcode }}</span>
        </div>
        <div class="head-right">
          <span class="total">合计 ¥{{ grandTotal.toFixed(2) }}</span>
          <button class="ft-btn" @click="addTrip">+ 新增行程</button>
        </div>
      </div>

      <div v-if="loading" class="state">加载中…</div>
      <div v-else-if="!trips.length" class="state ft-card">还没有行程，点「新增行程」记一笔。</div>

      <div v-else class="trip-list">
        <div v-for="t in trips" :key="t.id" class="trip ft-card">
          <div class="trip-head" @click="expanded = expanded === t.id ? null : t.id">
            <span class="trip-date">{{ fmtDateTime(t.create_time) }}</span>
            <span class="trip-total">¥{{ t.total.toFixed(2) }}</span>
            <span class="chev">{{ expanded === t.id ? '▲' : '▼' }}</span>
          </div>

          <div v-if="expanded === t.id" class="trip-body">
            <div v-if="!t.bills.length" class="no-bills">这次行程还没有账单</div>
            <table v-else class="bills">
              <thead>
                <tr>
                  <th class="time-col">记账时间</th>
                  <th>分类</th>
                  <th class="right">金额</th>
                  <th class="right">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="b in sortedBills(t.bills)" :key="b.id">
                  <td class="time-col">{{ fmtTime(b.create_time) }}</td>
                  <td>{{ catLabel(b) }}</td>
                  <td class="right">¥{{ Number(b.amount).toFixed(2) }}</td>
                  <td class="right">
                    <button class="ft-btn ft-btn--ghost mini" @click="openEditBill(b)">编辑</button>
                  </td>
                </tr>
              </tbody>
            </table>
            <div class="trip-actions">
              <button class="ft-btn ft-btn--ghost mini" @click="openAddBill(t.id)">+ 记一笔</button>
              <button class="ft-btn ft-btn--ghost mini" @click="removeTrip(t.id)">删除此行程</button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <n-modal
      v-model:show="showBillModal"
      preset="card"
      :title="editingBill ? '编辑账单' : '记一笔'"
      style="width: 360px"
    >
      <div class="bill-form">
        <label>分类</label>
        <n-select v-model:value="billForm.category" :options="categoryOptions" />
        <template v-if="billForm.category === 6">
          <label>自定义分类</label>
          <n-input v-model:value="billForm.custom_category" placeholder="如：伴手礼" />
        </template>
        <label>金额</label>
        <n-input-number v-model:value="billForm.amount" :min="0" :precision="2" style="width: 100%" />
        <button class="ft-btn" :disabled="saving" @click="saveBill">保存</button>
      </div>
    </n-modal>
  </div>
</template>

<style scoped>
.records {
  height: 100%;
  padding: 24px;
  overflow: auto;
}
.hint {
  max-width: 460px;
  margin: 80px auto;
  padding: 32px;
  text-align: center;
  color: var(--c-grey-text);
}
.hint-icon {
  font-size: 40px;
  margin-bottom: 12px;
}
.panel {
  max-width: 760px;
  margin: 0 auto;
}
.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 16px 18px;
  margin-bottom: 16px;
}
.city {
  font-size: 18px;
  font-weight: 800;
}
.adcode {
  display: block;
  margin-top: 2px;
  font-size: 12px;
  color: var(--c-grey-text);
}
.head-right {
  display: flex;
  align-items: center;
  gap: 12px;
}
.total {
  font-size: 14px;
  font-weight: 700;
}
.state {
  padding: 28px;
  text-align: center;
  color: var(--c-grey-text);
}
.trip-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.trip {
  box-shadow: var(--shadow-hard-sm);
}
.trip-head {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 16px;
  cursor: pointer;
}
.trip-date {
  font-size: 14px;
  font-weight: 600;
}
.trip-total {
  margin-left: auto;
  font-size: 14px;
  font-weight: 800;
}
.chev {
  font-size: 11px;
  color: var(--c-grey-text);
}
.trip-body {
  padding: 0 16px 16px;
  border-top: 1.5px dashed var(--c-surface-variant);
}
.no-bills {
  padding: 14px 0;
  font-size: 13px;
  color: var(--c-grey-text);
}
.bills {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.bills th,
.bills td {
  padding: 8px 4px;
  border-bottom: 1.5px solid var(--c-grey-light);
  text-align: left;
}
.bills .right {
  text-align: right;
  font-weight: 600;
}
.bills .time-col {
  width: 116px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  color: var(--c-grey-text);
}
.trip-actions {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}
.mini {
  padding: 3px 10px;
  font-size: 12px;
}
.bill-form {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.bill-form label {
  font-size: 13px;
  font-weight: 700;
}
.bill-form .ft-btn {
  margin-top: 8px;
}
</style>
