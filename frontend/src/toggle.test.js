import { describe, expect, it, vi } from 'vitest'

import { persistBooleanToggle } from './toggle'

describe('persistBooleanToggle', () => {
  it('rolls the UI value back when persistence fails', async () => {
    const row = { is_active: true }
    const persist = vi.fn().mockRejectedValue(new Error('network'))

    await expect(persistBooleanToggle(row, 'is_active', persist)).rejects.toThrow('network')

    expect(persist).toHaveBeenCalledWith(true)
    expect(row.is_active).toBe(false)
  })
})
