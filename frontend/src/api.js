import axios from 'axios'
import { useAuthStore } from './stores/auth'

const api = axios.create({ baseURL: '/api', timeout: 30_000 })

api.interceptors.request.use((config) => {
  const auth = useAuthStore()
  if (auth.token) {
    config.headers.Authorization = `Bearer ${auth.token}`
  }
  return config
})

api.interceptors.response.use(
  (resp) => resp,
  (error) => {
    // 登录接口的 401 不触发 logout 跳转（否则错误提示刚显示就被页面刷新清掉）
    const isLoginRequest = error.config?.url?.includes('/auth/login') || error.config?.url?.includes('/sso/exchange')
    if (error.response?.status === 401 && !isLoginRequest) {
      const auth = useAuthStore()
      auth.logout()
    }
    return Promise.reject(error)
  }
)

export default api
