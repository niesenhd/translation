import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from './stores/auth'
import Login from './views/Login.vue'
import Tasks from './views/Tasks.vue'
import Admin from './views/Admin.vue'
import NotFound from './views/NotFound.vue'

const routes = [
  { path: '/login', component: Login, meta: { public: true } },
  { path: '/', component: Tasks },
  { path: '/admin', component: Admin, meta: { admin: true } },
  { path: '/:pathMatch(.*)*', component: NotFound },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach((to) => {
  const auth = useAuthStore()
  if (!to.meta.public && !auth.token) return '/login'
  if (to.path === '/login' && auth.token) return '/'
  // 管理后台需要管理员权限
  if (to.meta.admin && !auth.is_admin) return '/'
})

export default router
