<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useMessage } from 'naive-ui'
import { useAuthStore } from '@/stores/auth'
import { apiError } from '@/api/http'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()
const message = useMessage()

const email = ref('')
const password = ref('')
const loading = ref(false)

async function onLogin() {
  if (!email.value || !password.value) {
    message.warning('请填写邮箱和密码')
    return
  }
  loading.value = true
  try {
    await auth.login(email.value.trim(), password.value)
    message.success('登录成功')
    const redirect = (route.query.redirect as string) || '/map'
    router.push(redirect)
  } catch (e) {
    message.error(apiError(e, '登录失败，请检查邮箱和密码'))
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="auth-page">
    <div class="auth-card ft-card">
      <div class="auth-head">
        <span class="auth-mark">
          <svg viewBox="0 0 24 24" width="26" height="26" aria-hidden="true">
            <ellipse cx="8.7" cy="8.2" rx="3.3" ry="4.9" fill="currentColor" transform="rotate(-16 8.7 8.2)"/><ellipse cx="15.3" cy="15.6" rx="3.3" ry="4.9" fill="currentColor" transform="rotate(-16 15.3 15.6)"/>
          </svg>
        </span>
        <h1>足迹 Footprint</h1>
        <p>把走过的每一步，留在地图上</p>
      </div>

      <form class="auth-form" @submit.prevent="onLogin">
        <label class="field">
          <span>邮箱</span>
          <input
            v-model="email"
            class="ft-input"
            type="email"
            placeholder="you@example.com"
            autocomplete="username"
          />
        </label>

        <label class="field">
          <span>密码</span>
          <input
            v-model="password"
            class="ft-input"
            type="password"
            placeholder="至少 6 位"
            autocomplete="current-password"
          />
        </label>

        <button class="ft-btn submit" type="submit" :disabled="loading">
          {{ loading ? '登录中…' : '登录' }}
        </button>
      </form>

      <div class="auth-foot">
        还没有账号？
        <router-link :to="{ name: 'register' }">去注册</router-link>
      </div>

      <div class="demo-hint">
        本地演示账号：<code>demo@footprint.dev</code> / <code>demo1234</code>
        <span>（需先运行 seed_demo.py）</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.auth-page {
  display: grid;
  place-items: center;
  height: 100%;
  padding: 24px;
  background: var(--ft-sand);
}
.auth-card {
  position: relative;
  width: 100%;
  max-width: 400px;
  padding: 30px 28px 26px;
  overflow: hidden;
}
/* 卡片顶部一小段陶土橙，作为品牌记忆点 */
.auth-card::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 4px;
  background: var(--ft-accent);
}
.auth-head {
  text-align: center;
  margin-bottom: 24px;
}
.auth-mark {
  display: inline-grid;
  place-items: center;
  width: 52px;
  height: 52px;
  color: var(--c-white);
  background: var(--ft-ink);
  border-radius: 14px;
  margin-bottom: 12px;
}
.auth-head h1 {
  margin: 0;
  font-family: var(--font-display);
  font-size: 24px;
  font-weight: 500;
  letter-spacing: 2px;
  color: var(--ft-text);
}
.auth-head p {
  margin: 8px 0 0;
  font-size: 13px;
  color: var(--c-grey-text);
}
.auth-form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.field > span {
  font-size: 13px;
  font-weight: 700;
}
.submit {
  width: 100%;
  margin-top: 4px;
  padding: 12px;
}
.auth-foot {
  margin-top: 18px;
  text-align: center;
  font-size: 13px;
  color: var(--c-grey-text);
}
.auth-foot a {
  color: var(--c-black);
  font-weight: 700;
}
.demo-hint {
  margin-top: 16px;
  padding: 10px;
  font-size: 12px;
  color: var(--c-grey-text);
  text-align: center;
  background: var(--c-grey-light);
  border: 1.5px dashed var(--c-grey-medium);
  border-radius: var(--radius-sm);
}
.demo-hint code {
  font-weight: 700;
  color: var(--c-black);
}
</style>
