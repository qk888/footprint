import http from './http'
import type { ResponseOut, TripOut } from '@/types'

export const tripsApi = {
  add(adcode: string) {
    return http.post<ResponseOut>('/trips/add', { adcode })
  },
  list() {
    return http.get<{ trips: TripOut[] }>('/trips')
  },
  get(tripId: number) {
    return http.get<TripOut>(`/trips/${tripId}`)
  },
  remove(tripId: number) {
    return http.delete<ResponseOut>(`/trips/${tripId}`)
  },
}
