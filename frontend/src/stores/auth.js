import { defineStore } from 'pinia'
import api from '../api'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: localStorage.getItem('token') || '',
    username: localStorage.getItem('username') || '',
    is_admin: localStorage.getItem('is_admin') === 'true',
    display_name: localStorage.getItem('display_name') || '',
  }),
  actions: {
    login({ token, username, is_admin, display_name }) {
      this.token = token
      this.username = username
      this.is_admin = !!is_admin
      this.display_name = display_name || username
      localStorage.setItem('token', token)
      localStorage.setItem('username', username)
      localStorage.setItem('is_admin', this.is_admin ? 'true' : 'false')
      localStorage.setItem('display_name', this.display_name)
    },
    // 启动时校验 token 并刷新 is_admin（token 可能来自旧会话）
    async fetchMe() {
      if (!this.token) return false
      try {
        const { data } = await api.get('/auth/me')
        this.username = data.username
        this.is_admin = !!data.is_admin
        this.display_name = data.display_name || data.username
        localStorage.setItem('username', this.username)
        localStorage.setItem('is_admin', this.is_admin ? 'true' : 'false')
        localStorage.setItem('display_name', this.display_name)
        return true
      } catch (e) {
        // 401 由 api 拦截器触发 logout
        return false
      }
    },
    logout() {
      this.token = ''
      this.username = ''
      this.is_admin = false
      this.display_name = ''
      localStorage.removeItem('token')
      localStorage.removeItem('username')
      localStorage.removeItem('is_admin')
      localStorage.removeItem('display_name')
      window.location.href = '/login'
    },
  },
})
