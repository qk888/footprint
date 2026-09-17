import http from './http'
import type { PlanRecordOut, ResponseOut } from '@/types'

export const plansApi = {
  /**
   * 保存某城市计划。
   * mode: overwrite=覆盖该城市已有规划（默认，兼容老调用）；new=另存为新的一份
   * planId: 覆盖时指定目标（规划页在编辑"第 N 份"时必须带，否则会覆盖到最新那份上）
   */
  set(adcode: string, content: string, mode: 'overwrite' | 'new' = 'overwrite', planId?: number) {
    return http.post<ResponseOut>('/plans/set', { adcode, content, mode, plan_id: planId })
  },
  /** 某城市计划（无则后端返回空内容占位；同城多份时返回最新一份） */
  get(adcode: string) {
    return http.get<PlanRecordOut>(`/plans/${adcode}`)
  },
  /** 用户所有计划（同城可能多份，按更新时间倒序） */
  list() {
    return http.get<PlanRecordOut[]>('/plans')
  },
  remove(planId: number) {
    return http.delete<ResponseOut>(`/plans/${planId}`)
  },
}
