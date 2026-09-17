// 与后端 schema / ⟦PLAN⟧ 行程结构对齐的类型定义

export interface UserOut {
  id: number
  email: string
  username: string
}

export interface LoginOut {
  user: UserOut
  access_token: string
  refresh_token: string
}

export interface ResponseOut {
  result: 'success' | 'failure'
}

// ---- 行程卡片（⟦PLAN⟧JSON⟦/PLAN⟧ 解析结果）----
export interface Transport {
  mode: string // metro | taxi | walk
  start_time?: string
  end_time?: string
  cost?: number
}

export interface Activity {
  type: string // train|airplane|intercity|breakfast|lunch|dinner|attraction|accommodation|free
  position?: string
  start?: string
  end?: string
  vehicle?: string
  start_time?: string
  end_time?: string
  cost?: number
  transports?: Transport[]
}

export interface ItineraryDay {
  day: number
  activities: Activity[]
}

export interface Plan {
  days: number
  people: number
  start_city?: string
  target_city?: string
  total_cost?: number
  warning?: string
  city?: string   // 目的城市名(后端下发, 用于保存定位城市)
  adcode?: string // 城市编码(后端下发, 保存规划接口需要)
  itinerary: ItineraryDay[]
}

// ---- 业务实体 ----
export interface TripOut {
  id: number
  adcode: string
  create_time: string
}

export interface BillOut {
  id: number
  amount: number
  category: number
  custom_category?: string | null
  create_time?: string
}

export interface NoteOut {
  id: number
  trip_id: number
  content: string
}

export interface BudgetOut {
  id: number
  adcode: string
  category: number
  custom_category?: string | null
  amount: number
  update_time: string
}

export interface PlanRecordOut {
  id: number
  adcode: string
  content: string
  update_time: string
}

// 账单/预算分类枚举（后端 CATEGORY_ENUM_MAP）
export const CATEGORY_MAP: Record<number, string> = {
  1: '交通',
  2: '餐饮',
  3: '购物',
  4: '住宿',
  5: '娱乐',
  6: '其他',
}

// ---- AI 长期记忆 ----
export interface MemoryOut {
  id: number
  kind: string
  content: string
  source: string
  update_time: string
  /** active=生效中；superseded=已被新说法取代（保留历史，不再自动带到对话里） */
  status?: string
  /** true=AI 自己记的候选，确认后才会自动带给 AI */
  needs_review?: boolean
}

// 记忆类别（后端 KIND_LABEL）
export const MEMORY_KIND_MAP: Record<string, string> = {
  city_light: '点亮城市',
  bill: '消费记录',
  budget: '预算',
  plan: '行程计划',
  manual: '手动记录',
  chat: '聊天记住',
}

// 记忆体检（后端 /memory/stats）
export interface MemoryStats {
  /** 生效中的记忆条数 */
  active: number
  /** 语义检索是否已启用（生效条数过了阈值才启用） */
  vector_enabled: boolean
  /** 启用阈值 */
  vector_min_count: number
  /** 已进向量索引的条数 */
  vector_indexed: number
}
