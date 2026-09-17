import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { userApi, type RegisterPayload } from '@/api/user'
import { tokenStore, setAuthExpiredHandler } from '@/api/http'
import type { UserOut } from '@/types'

const USER_KEY = 'footprint_user'

function loadUser(): UserOut | null {
  const raw = localStorage.getItem(USER_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw) as UserOut
  } catch {
    return null
  }
}

export const useAuthStore = defineStore('auth', () => {
  const user = ref<UserOut | null>(loadUser())
  const accessToken = ref<string | null>(tokenStore.access)

  const isLoggedIn = computed(() => !!accessToken.value && !!user.value)

  function persistTokens(access: string, refresh: string, u: UserOut) {
    tokenStore.save(access, refresh)
    localStorage.setItem(USER_KEY, JSON.stringify(u))
    accessToken.value = access
    user.value = u
  }

  async function login(email: string, password: string) {
    const res = await userApi.login(email, password)
    persistTokens(res.data.access_token, res.data.refresh_token, res.data.user)
    return res.data.user
  }

  async function register(payload: RegisterPayload) {
    const res = await userApi.register(payload)
    persistTokens(res.data.access_token, res.data.refresh_token, res.data.user)
    return res.data.user
  }

  function logout() {
    tokenStore.clear()
    localStorage.removeItem(USER_KEY)
    accessToken.value = null
    user.value = null
  }

  // 注册鉴权失效回调：刷新失败时清状态（路由守卫负责跳转）
  setAuthExpiredHandler(() => {
    user.value = null
    accessToken.value = null
    localStorage.removeItem(USER_KEY)
  })

  return { user, accessToken, isLoggedIn, login, register, logout }
})
