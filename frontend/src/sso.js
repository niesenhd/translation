import api from './api'

/**
 * 从当前地址消费一次性 SSO code。返回 false 表示地址中没有 code。
 * code 会在发起网络请求前从地址栏清除，避免被刷新、复制或 Referer 泄露。
 */
export async function exchangeSsoCode(
  auth,
  { apiClient = api, location = window.location, history = window.history } = {},
) {
  const url = new URL(location.href)
  const code = url.searchParams.get('sso_code')
  if (!code) return false

  url.searchParams.delete('sso_code')
  history.replaceState({}, '', `${url.pathname}${url.search}${url.hash}`)
  auth.clearSession()
  const { data } = await apiClient.post('/sso/exchange', { code })
  auth.login(data)
  return true
}
