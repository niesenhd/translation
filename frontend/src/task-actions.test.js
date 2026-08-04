import { describe, expect, it, vi } from 'vitest'

import { confirmAndDeleteTask, taskPageParams } from './task-actions'

describe('task actions', () => {
  it('uses the paginated task API contract', () => {
    expect(taskPageParams(3, 50)).toEqual({ page: 3, page_size: 50 })
  })

  it('never deletes before confirmation succeeds', async () => {
    const row = { id: 'task-1' }
    const confirm = vi.fn().mockRejectedValue(new Error('cancelled'))
    const remove = vi.fn()

    await expect(confirmAndDeleteTask(row, confirm, remove)).rejects.toThrow('cancelled')
    expect(remove).not.toHaveBeenCalled()
  })
})
