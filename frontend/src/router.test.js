import { describe, expect, it } from 'vitest'

import router from './router'
import NotFound from './views/NotFound.vue'

describe('router', () => {
  it('resolves unknown URLs to the 404 view', () => {
    const route = router.resolve('/path-that-does-not-exist')
    expect(route.matched.at(-1).components.default).toBe(NotFound)
  })
})
