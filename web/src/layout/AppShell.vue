<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

/* 图标用内联 SVG（描边随文字色走），不依赖 emoji 字体，跨平台一致 */
const navItems = [
  {
    name: 'map',
    label: '足迹地图',
    path: 'M9 3.5c3.2 0 5.8 2.6 5.8 5.8 0 4.2-5.8 11.2-5.8 11.2S3.2 13.5 3.2 9.3C3.2 6.1 5.8 3.5 9 3.5Zm0 3.6a2.2 2.2 0 1 0 0 4.4 2.2 2.2 0 0 0 0-4.4Z',
  },
  {
    name: 'chat',
    label: 'AI 助手',
    path: 'M3.5 5.6c0-1.2 1-2.1 2.1-2.1h6.8c1.2 0 2.1 1 2.1 2.1v4.2c0 1.2-1 2.1-2.1 2.1H7.4L4 14.6v-2.6c-.3-.3-.5-.7-.5-1.2V5.6Z',
  },
  {
    name: 'records',
    label: '旅行记录',
    path: 'M4.2 3.8h7.1c.9 0 1.6.7 1.6 1.6v9c0 .9-.7 1.6-1.6 1.6H4.2c-.5 0-.9-.4-.9-.9V4.7c0-.5.4-.9.9-.9Zm2 3.1h4.6M6.2 9.4h4.6M6.2 11.9h2.9',
  },
  {
    name: 'plans',
    label: '行程规划',
    path: 'M5.2 3.4v1.7M10.9 3.4v1.7M4 6.6h10.4M4 6.6c0-.9.7-1.7 1.7-1.7h6.7c.9 0 1.7.7 1.7 1.7v6.9c0 .9-.8 1.7-1.7 1.7H5.7c-.9 0-1.7-.8-1.7-1.7V6.6Zm2.7 3.3h2.2v2.2H6.7v-2.2Z',
  },
  {
    name: 'profile',
    label: '我的',
    path: 'M9 9.1a2.6 2.6 0 1 0 0-5.2 2.6 2.6 0 0 0 0 5.2Zm-5 5.5c0-1.9 2.2-3 5-3s5 1.1 5 3',
  },
]

const activeName = computed(() => route.name as string)

function go(name: string) {
  router.push({ name })
}

function onLogout() {
  auth.logout()
  router.push({ name: 'login' })
}
</script>

<template>
  <div class="shell">
    <aside class="sidebar">
      <div class="brand">
        <span class="ft-brand-mark">
          <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
            <ellipse cx="8.7" cy="8.2" rx="3.3" ry="4.9" fill="currentColor" transform="rotate(-16 8.7 8.2)"/><ellipse cx="15.3" cy="15.6" rx="3.3" ry="4.9" fill="currentColor" transform="rotate(-16 15.3 15.6)"/>
          </svg>
        </span>
        <span class="brand-copy">
          <span class="brand-text">足迹</span>
          <span class="brand-en">Footprint</span>
        </span>
      </div>

      <nav class="nav">
        <button
          v-for="item in navItems"
          :key="item.name"
          class="nav-item"
          :class="{ active: activeName === item.name }"
          @click="go(item.name)"
        >
          <svg class="nav-icon" viewBox="0 0 18 18" width="17" height="17" aria-hidden="true">
            <path
              :d="item.path"
              fill="none"
              stroke="currentColor"
              stroke-width="1.4"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
          </svg>
          <span class="nav-label">{{ item.label }}</span>
        </button>
      </nav>

      <div class="sidebar-foot">
        <div class="user-chip" :title="auth.user?.email">
          {{ auth.user?.username || '未登录' }}
        </div>
        <button class="ft-btn ft-btn--ghost logout" @click="onLogout">退出</button>
      </div>
    </aside>

    <!-- 聊天页自己管滚动(消息区独立滚动), 外层不许再滚, 否则整页会跟着动 -->
    <main class="content ft-scroll" :class="{ 'content--fixed': activeName === 'chat' }">
      <router-view v-slot="{ Component }">
        <keep-alive :include="['MapView']">
          <component :is="Component" />
        </keep-alive>
      </router-view>
    </main>
  </div>
</template>

<style scoped>
.shell {
  display: flex;
  /* 用视口高度而不是 height:100%：height:100% 依赖父级链(html/body/#app 之外，
     naive-ui 的 Provider 可能还会插一层没有高度的 div)，链一断 .shell 就变成 auto，
     内容一长就撑高整页 → 滚动跑到 body 上，表现为"滑动聊天时整个界面跟着动"。
     直接锁视口高度，整页永不滚动，滚动只发生在各视图内部。 */
  height: 100vh;
  height: 100dvh;
  overflow: hidden;
  background: var(--ft-sand);
}

.sidebar {
  display: flex;
  flex-direction: column;
  width: 228px;
  flex-shrink: 0;
  background: var(--ft-paper);
  border-right: 1px solid var(--ft-line);
}

.brand {
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 22px 18px 18px;
}
.brand-copy {
  display: flex;
  flex-direction: column;
}
.brand-text {
  font-family: var(--font-display);
  font-size: 20px;
  font-weight: 500;
  letter-spacing: 3px;
  color: var(--ft-text);
}
.brand-en {
  margin-top: 1px;
  font-size: 10px;
  font-weight: 500;
  letter-spacing: 2.4px;
  text-transform: uppercase;
  color: var(--ft-text-3);
}

.nav {
  flex: 1;
  padding: 6px 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.nav-item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 11px 12px;
  font-size: 14px;
  font-weight: 400;
  color: var(--ft-text-2);
  background: transparent;
  border: 0;
  border-radius: var(--ft-radius-sm);
  transition: background 0.15s ease, color 0.15s ease;
}
.nav-item:hover {
  color: var(--ft-ink);
  background: var(--ft-sand-2);
}
.nav-item.active {
  color: var(--ft-ink);
  font-weight: 500;
  background: var(--ft-sand-2);
}
/* 当前项用一小段陶土橙竖条点出来（不用整块深色底，和原版视觉拉开距离） */
.nav-item.active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 50%;
  width: 3px;
  height: 18px;
  margin-top: -9px;
  background: var(--ft-accent);
  border-radius: var(--ft-radius-pill);
}
.nav-icon {
  flex-shrink: 0;
  color: currentColor;
}

.sidebar-foot {
  padding: 14px 12px 16px;
  border-top: 1px solid var(--ft-line);
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.user-chip {
  padding: 9px 11px;
  font-size: 13px;
  color: var(--ft-text-2);
  background: var(--ft-sand);
  border-radius: var(--ft-radius-sm);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.logout {
  width: 100%;
  padding: 9px 16px;
  font-size: 13px;
}

.content {
  flex: 1;
  min-width: 0;
  /* flex 子项默认 min-height:auto，不置 0 的话内容一高就撑破容器、把父层顶出滚动条 */
  min-height: 0;
  overflow: auto;
}

/* 聊天页自己管滚动（消息区独立滚），外层锁死不再滚 */
.content--fixed {
  overflow: hidden;
}
</style>
