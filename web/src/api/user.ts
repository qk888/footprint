import http from './http'
import type { LoginOut, ResponseOut } from '@/types'

export interface RegisterPayload {
  email: string
  username: string
  password: string
  confirm_password: string
  code: string
}

export const userApi = {
  /** 发送邮箱验证码（本地 Mailpit 收件） */
  sendVerifyCode(email: string) {
    return http.post<ResponseOut>('/user/verify-code', { email })
  },

  register(data: RegisterPayload) {
    return http.post<LoginOut>('/user/register', data)
  },

  login(email: string, password: string) {
    return http.post<LoginOut>('/user/login', { email, password })
  },

  /** 天地图 key（当前用 OSM 占位，接口保留） */
  getMapKey() {
    return http.get<{ key: string }>('/user/map/key')
  },
}
