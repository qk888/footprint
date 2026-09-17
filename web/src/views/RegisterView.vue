<script setup lang="ts">
import { ref, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { useMessage } from 'naive-ui'
import { useAuthStore } from '@/stores/auth'
import { userApi } from '@/api/user'
import { apiError } from '@/api/http'

const auth = useAuthStore()
const router = useRouter()
const message = useMessage()

const email = ref('')
const username = ref('')
const password = ref('')
const confirmPassword = ref('')
const code = ref('')

const sending = ref(false)
const cooldown = ref(0)
let timer: number | null = null

const submitting = ref(false)

function startCooldown(seconds = 60) {
  cooldown.value = seconds
  timer = window.setInterval(() => {
    cooldown.value -= 1
    if (cooldown.value <= 0 && timer) {
      clearInterval(timer)
      timer = null
    }
  }, 1000)
}

async function onSendCode() {
  if (!email.value) {
    message.warning('请先填写邮箱')
    return
  }
  if (cooldown.value > 0 || sending.value) return
  sending.value = true
  try {
    await userApi.sendVerifyCode(email.value.trim())
    message.success('验证码已发送（本地 Mailpit：http://localhost:8025）')
    startCooldown(60)
  } catch (e) {
    message.error(apiError(e, '验证码发送失败'))
  } finally {
    sending.value = false
  }
}

async function onRegister() {
  if (password.value !== confirmPassword.value) {
    message.warning('两次输入的密码不一致')
    return
  }
  if (code.value.length !== 4) {
    message.warning('请输入 4 位验证码')
    return
  }
  submitting.value = true
  try {
    await auth.register({
      email: email.value.trim(),
      username: username.value.trim(),
      password: password.value,
      confirm_password: confirmPassword.value,
      code: code.value,
    })
    message.success('注册成功，已自动登录')
    router.push({ name: 'map' })
  } catch (e) {
    message.error(apiError(e, '注册失败'))
  } finally {
    submitting.value = false
  }
}

onUnmounted(() => {
  if (timer) clearInterval(timer)
})
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
        <h1>创建账号</h1>
        <p>加入足迹，开始点亮你去过的城市</p>
      </div>

      <form class="auth-form" @submit.prevent="onRegister">
        <label class="field">
          <span>邮箱</span>
          <input v-model="email" class="ft-input" type="email" placeholder="you@example.com" />
        </label>

        <label class="field">
          <span>用户名</span>
          <input
            v-model="username"
            class="ft-input"
            type="text"
            placeholder="3–20 个字符"
            maxlength="20"
          />
        </label>

        <label class="field">
          <span>密码</span>
          <input
            v-model="password"
            class="ft-input"
            type="password"
            placeholder="6–20 位"
            autocomplete="new-password"
          />
        </label>

        <label class="field">
          <span>确认密码</span>
          <input
            v-model="confirmPassword"
            class="ft-input"
            type="password"
            placeholder="再输一次"
            autocomplete="new-password"
          />
        </label>

        <label class="field">
          <span>邮箱验证码</span>
          <div class="code-row">
            <input
              v-model="code"
              class="ft-input"
              type="text"
              inputmode="numeric"
              maxlength="4"
              placeholder="4 位数字"
            />
            <button
              type="button"
              class="ft-btn ft-btn--ghost code-btn"
              :disabled="sending || cooldown > 0"
              @click="onSendCode"
            >
              {{ cooldown > 0 ? `${cooldown}s` : sending ? '发送中…' : '获取验证码' }}
            </button>
          </div>
        </label>

        <button class="ft-btn submit" type="submit" :disabled="submitting">
          {{ submitting ? '注册中…' : '注册并登录' }}
        </button>
      </form>

      <div class="auth-foot">
        已有账号？
        <router-link :to="{ name: 'login' }">去登录</router-link>
      </div>
    </div>
  </div>
</template>

<style scoped>
.auth-page {
  display: grid;
  place-items: center;
  min-height: 100%;
  padding: 24px;
  background: var(--c-surface);
}
.auth-card {
  width: 100%;
  max-width: 420px;
  padding: 28px;
}
.auth-head {
  text-align: center;
  margin-bottom: 22px;
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
  margin: 6px 0 0;
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
.code-row {
  display: flex;
  gap: 10px;
}
.code-btn {
  flex-shrink: 0;
  white-space: nowrap;
  min-width: 110px;
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
</style>
