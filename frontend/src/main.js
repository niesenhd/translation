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
if (auth.token) {
  auth.fetchMe().finally(() => app.mount('#app'))
} else {
  app.mount('#app')
}

