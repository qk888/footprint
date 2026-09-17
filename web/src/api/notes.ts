import http from './http'
import type { NoteOut, ResponseOut } from '@/types'

export const notesApi = {
  add(tripId: number, content: string) {
    return http.post<ResponseOut>('/notes/add', { trip_id: tripId, content })
  },
  listByTrip(tripId: number) {
    return http.get<{ notes: NoteOut[] }>(`/notes/trip/${tripId}`)
  },
  get(noteId: number) {
    return http.get<NoteOut>(`/notes/${noteId}`)
  },
  remove(noteId: number) {
    return http.delete<ResponseOut>(`/notes/${noteId}`)
  },
}
