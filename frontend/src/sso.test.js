import { describe, expect, it, vi } from 'vitest'

import { exchangeSsoCode } from './sso'

describe('exchangeSsoCode', () => {
  it('removes the code before exchanging it and stores the returned session', async () => {
    const calls = []
    const auth = {
      clearSession: vi.fn(() => calls.push('cleared')),
      login: vi.fn((payload) => calls.push(['login', payload])),
    }
    const history = {
      replaceState: vi.fn((_state, _title, url) => calls.push(['url', url])),
    }
    const response = { token: 'session', username: 'oa-user', is_admin: false }
    const apiClient = {
      post: vi.fn(async (path, body) => {
        calls.push(['exchange', path, body])
        return { data: response }
      }),
    }

    const consumed = await exchangeSsoCode(auth, {
      apiClient,
      history,
      location: { href: 'https://translation.example/tasks?sso_code=one-time&tab=all#top' },
    })

    expect(consumed).toBe(true)
    expect(history.replaceState).toHaveBeenCalledWith({}, '', '/tasks?tab=all#top')
    expect(apiClient.post).toHaveBeenCalledWith('/sso/exchange', { code: 'one-time' })
    expect(auth.login).toHaveBeenCalledWith(response)
    expect(calls[0]).toEqual(['url', '/tasks?tab=all#top'])
    expect(calls[1]).toBe('cleared')
  })

  it('does nothing when the URL has no code', async () => {
    const auth = { clearSession: vi.fn(), login: vi.fn() }
    const apiClient = { post: vi.fn() }
    const consumed = await exchangeSsoCode(auth, {
      apiClient,
      history: { replaceState: vi.fn() },
      location: { href: 'https://translation.example/' },
    })

    expect(consumed).toBe(false)
    expect(apiClient.post).not.toHaveBeenCalled()
  })
})
