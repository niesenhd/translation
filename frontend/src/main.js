import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import { ElMessage } from 'element-plus'
import 'element-plus/dist/index.css'
import { createPinia } from 'pinia'
import router from './router'
import App from './App.vue'
import { exchangeSsoCode } from './sso'
import { useAuthStore } from './stores/auth'

const app = createApp(App)
app.use(createPinia())
app.use(ElementPlus)

// 启动时校验本地 token 并刷新权限（token 可能来自旧会话或已过期）
const auth = useAuthStore()

async function bootstrap() {
  let exchanged = false
  try {
    exchanged = await exchangeSsoCode(auth)
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '单点登录兑换失败，请重新从 OA 发起登录')
  }
  if (!exchanged && auth.token) {
    await auth.fetchMe()
  }
  // SSO 兑换完成后再安装路由，避免首次导航守卫在 code 尚未兑换时跳到登录页。
  app.use(router)
  await router.isReady()
  app.mount('#app')
}

bootstrap()
