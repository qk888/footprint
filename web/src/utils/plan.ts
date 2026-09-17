import type { Plan } from '@/types'

// 行程数据哨兵：后端把行程 JSON 包在 ⟦PLAN⟧...⟦/PLAN⟧ 里混在文本中发回
const PLAN_START = '⟦PLAN⟧'
const PLAN_END = '⟦/PLAN⟧'

// 行程 JSON 解析失败时的兜底话术（旧客户端也用过同一句，保持一致）
export const PLAN_BROKEN_TEXT = '行程已生成，但展示出了点问题，请重新询问'

/** 去掉行程数据段只留正文；哨兵未闭合时从 ⟦PLAN⟧ 截到末尾，避免半截 JSON 闪现 */
export function stripPlan(raw: string): string {
  const start = raw.indexOf(PLAN_START)
  if (start === -1) return raw
  const end = raw.indexOf(PLAN_END, start)
  const head = raw.slice(0, start)
  if (end === -1) return head
  return head + stripPlan(raw.slice(end + PLAN_END.length))
}

/** 从文本里抠出行程 JSON 并解析；没有/解析失败返回 null */
export function extractPlan(raw: string): Plan | null {
  const start = raw.indexOf(PLAN_START)
  if (start === -1) return null
  const end = raw.indexOf(PLAN_END, start)
  if (end === -1) return null
  try {
    const decoded = JSON.parse(raw.slice(start + PLAN_START.length, end))
    return decoded && typeof decoded === 'object' ? (decoded as Plan) : null
  } catch {
    return null
  }
}

export function hasPlanMarker(raw: string): boolean {
  return raw.includes(PLAN_START)
}

// ---- 文案格式化（端口自 itinerary_card.dart）----
const TYPE_LABELS: Record<string, string> = {
  train: '火车',
  airplane: '飞机',
  intercity: '跨城',
  breakfast: '早餐',
  lunch: '午餐',
  dinner: '晚餐',
  attraction: '游览',
  accommodation: '入住',
  free: '自由活动',
}

const MODE_LABELS: Record<string, string> = {
  metro: '地铁',
  taxi: '打车',
  walk: '步行',
}

export const typeLabel = (t: string): string => TYPE_LABELS[t] ?? t ?? ''
export const modeLabel = (m: string): string => MODE_LABELS[m] ?? m ?? ''

/** 费用文案：0 → 免费；整数 → ¥73；小数 → ¥73.5（去掉多余的 0） */
export function money(v: number | undefined | null): string {
  const n = typeof v === 'number' && !Number.isNaN(v) ? v : 0
  if (n <= 0) return '免费'
  if (Number.isInteger(n)) return `¥${n}`
  return `¥${n.toFixed(2).replace(/0+$/, '').replace(/\.$/, '')}`
}
