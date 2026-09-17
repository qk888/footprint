import http from './http'
import type { MemoryOut, MemoryStats, ResponseOut } from '@/types'

export const memoryApi = {
  /** 记忆列表（最近更新的在前） */
  list() {
    return http.get<{ memories: MemoryOut[] }>('/memory')
  },
  /** 记忆体检：条数 + 语义检索是否已启用 */
  stats() {
    return http.get<MemoryStats>('/memory/stats')
  },
  /** 手动添加一条记忆 */
  add(content: string, kind = 'manual') {
    return http.post<ResponseOut>('/memory/add', { content, kind })
  },
  /** 确认一条 AI 自己记下的候选记忆（确认后才会自动带给 AI） */
  confirm(memoryId: number) {
    return http.post<ResponseOut>(`/memory/${memoryId}/confirm`)
  },
  remove(memoryId: number) {
    return http.delete<ResponseOut>(`/memory/${memoryId}`)
  },
  /** 清空全部记忆 */
  removeAll() {
    return http.delete<ResponseOut>('/memory/all')
  },
}
