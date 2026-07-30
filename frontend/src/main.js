import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import { createPinia } from 'pinia'
import router from './router'
import App from './App.vue'
import { useAuthStore } from './stores/auth'

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.use(ElementPlus)

// 启动时校验本地 token 并刷新权限（token 可能来自旧会话或已过期）
const auth = useAuthStore()

// SSO 跳转处理：律智荟带 ?token=xxx 跳转到首页，自动登录
const params = new URLSearchParams(window.location.search)
const ssoToken = params.get('token')
if (ssoToken) {
  // 存储后清除 URL 中的 token（避免刷新/分享时泄漏）
  localStorage.setItem('token', ssoToken)
  auth.token = ssoToken
  // 清除 URL 参数
  window.history.replaceState({}, '', '/')
  auth.fetchMe().finally(() => app.mount('#app'))
} else if (auth.token) {
  auth.fetchMe().finally(() => app.mount('#app'))
} else {
  app.mount('#app')
}

