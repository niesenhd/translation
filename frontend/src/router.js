import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from './stores/auth'
import Login from './views/Login.vue'
import Tasks from './views/Tasks.vue'
import Admin from './views/Admin.vue'

const routes = [
  { path: '/login', component: Login, meta: { public: true } },
  { path: '/', component: Tasks },
  { path: '/admin', component: Admin, meta: { admin: true } },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach((to) => {
  const auth = useAuthStore()
  if (!to.meta.public && !auth.token) return '/login'
  if (to.path === '/login' && auth.token) return '/'
  // 管理后台需要 admin 用户
  if (to.meta.admin && auth.username !== 'admin') return '/'
})

export default router
