import http from './http'
import type { BillOut, ResponseOut } from '@/types'

export interface BillInput {
  amount: number
  category: number
  custom_category?: string | null
}

export const billsApi = {
  categories() {
    return http.get<Record<string, string>>('/bills/categories')
  },
  add(tripId: number, data: BillInput) {
    return http.post<ResponseOut>('/bills/add', { trip_id: tripId, ...data })
  },
  get(billId: number) {
    return http.get<BillOut>(`/bills/${billId}`)
  },
  listByTrip(tripId: number) {
    return http.get<{ bills: BillOut[] }>(`/bills/trip/${tripId}`)
  },
  update(billId: number, data: BillInput) {
    return http.put<ResponseOut>(`/bills/${billId}`, data)
  },
  remove(billId: number) {
    return http.delete<ResponseOut>(`/bills/${billId}`)
  },
  removeByTrip(tripId: number) {
    return http.delete<ResponseOut>(`/bills/trip/${tripId}`)
  },
}
