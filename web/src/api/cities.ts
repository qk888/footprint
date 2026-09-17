import http from './http'
import type { ResponseOut } from '@/types'

export const citiesApi = {
  /** 已点亮城市 adcode 列表 */
  getLighted() {
    return http.get<{ adcode: string[] }>('/cities/lighted')
  },

  /** 点亮城市 */
  light(adcode: string) {
    return http.post<ResponseOut>('/cities/light', { adcode })
  },

  /** 取消点亮（级联删除该城行程/账单/笔记） */
  unlight(adcode: string) {
    return http.delete<ResponseOut>(`/cities/${adcode}`)
  },
}
