import axios, {
  type AxiosInstance,
  type AxiosRequestConfig,
  type InternalAxiosRequestConfig,
} from 'axios'

// 所有请求走 /api 前缀：开发态 vite 代理、生产态 nginx 反代到 backend:8000
const BASE_URL = '/api'

export const TOKEN_KEY = 'footprint_access_token'
export const REFRESH_KEY = 'footprint_refresh_token'

export const tokenStore = {
  get access(): string | null {
    return localStorage.getItem(TOKEN_KEY)
  },
  get refresh(): string | null {
    return localStorage.getItem(REFRESH_KEY)
  },
  save(access: string, refresh: string) {
    localStorage.setItem(TOKEN_KEY, access)
    localStorage.setItem(REFRESH_KEY, refresh)
  },
  saveAccess(access: string) {
    localStorage.setItem(TOKEN_KEY, access)
  },
  clear() {
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(REFRESH_KEY)
  },
}

// 鉴权失效回调（由 auth store 注册 → 跳登录）
let onAuthExpired: (() => void) | null = null
export function setAuthExpiredHandler(fn: () => void) {
  onAuthExpired = fn
}

const http: AxiosInstance = axios.create({
  baseURL: BASE_URL,
  timeout: 15000,
  headers: { 'Content-Type': 'application/json' },
})

// 请求拦截：附加 Bearer
http.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = tokenStore.access
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

// 是否正在刷新，避免并发 401 触发多次刷新
let refreshing: Promise<string> | null = null

async function doRefresh(): Promise<string> {
  const refresh = tokenStore.refresh
  if (!refresh) throw new Error('no refresh token')
  // 用独立实例刷新，避免拦截器死循环
  const res = await axios.post(
    `${BASE_URL}/user/refresh-token`,
    { refresh_token: refresh },
    { timeout: 15000 },
  )
  const newAccess = res.data?.access_token as string
  if (!newAccess) throw new Error('refresh failed')
  tokenStore.saveAccess(newAccess)
  return newAccess
}

// 响应拦截：401 → 用 refresh_token 无感刷新后重试
http.interceptors.response.use(
  (res) => res,
  async (error) => {
    const original = error.config as AxiosRequestConfig & { _retry?: boolean }
    const status = error.response?.status
    // 后端 token 过期/类型错误用 403，未带 token 用 401，两者都尝试刷新
    if ((status === 401 || status === 403) && original && !original._retry) {
      original._retry = true
      try {
        refreshing = refreshing || doRefresh()
        const newAccess = await refreshing
        refreshing = null
        original.headers = { ...(original.headers || {}), Authorization: `Bearer ${newAccess}` }
        return http.request(original)
      } catch {
        refreshing = null
        tokenStore.clear()
        onAuthExpired?.()
        return Promise.reject(error)
      }
    }
    return Promise.reject(error)
  },
)

/** 从 FastAPI HTTPException 里提取 detail 文案 */
export function apiError(e: unknown, fallback = '请求失败'): string {
  const err = e as { response?: { data?: { detail?: unknown } } }
  const detail = err?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0] as { msg?: string }
    if (first?.msg) return first.msg
  }
  return fallback
}

export default http
