import http from './http'
import type { BudgetOut, ResponseOut } from '@/types'

export interface SetBudgetInput {
  adcode: string
  category: number
  amount: number
  custom_category?: string | null
}

export const budgetApi = {
  set(data: SetBudgetInput) {
    return http.post<ResponseOut>('/budget/set', data)
  },
  list(adcode?: string) {
    return http.get<{ budgets: BudgetOut[] }>('/budget', {
      params: adcode ? { adcode } : {},
    })
  },
  get(budgetId: number) {
    return http.get<BudgetOut>(`/budget/${budgetId}`)
  },
  remove(budgetId: number) {
    return http.delete<ResponseOut>(`/budget/${budgetId}`)
  },
}
