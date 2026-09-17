import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { tokenStore } from '@/api/http'

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { public: true, title: '登录' },
  },
  {
    path: '/register',
    name: 'register',
    component: () => import('@/views/RegisterView.vue'),
    meta: { public: true, title: '注册' },
  },
  {
    path: '/',
    component: () => import('@/layout/AppShell.vue'),
    children: [
      { path: '', redirect: '/map' },
      {
        path: 'map',
        name: 'map',
        component: () => import('@/views/MapView.vue'),
        meta: { title: '地图', icon: 'map' },
      },
      {
        path: 'chat',
        name: 'chat',
        component: () => import('@/views/ChatView.vue'),
        meta: { title: 'AI 助手', icon: 'chat' },
      },
      {
        path: 'records',
        name: 'records',
        component: () => import('@/views/RecordsView.vue'),
        meta: { title: '记录', icon: 'records' },
      },
      {
        path: 'plans',
        name: 'plans',
        component: () => import('@/views/PlansView.vue'),
        meta: { title: '规划', icon: 'plans' },
      },
      {
        path: 'profile',
        name: 'profile',
        component: () => import('@/views/ProfileView.vue'),
        meta: { title: '我的', icon: 'profile' },
      },
    ],
  },
  { path: '/:pathMatch(.*)*', redirect: '/map' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

// 全局守卫：非 public 路由需要登录
router.beforeEach((to) => {
  const authed = !!tokenStore.access
  if (!to.meta.public && !authed) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.meta.public && authed && (to.name === 'login' || to.name === 'register')) {
    return { name: 'map' }
  }
  return true
})

export default router
