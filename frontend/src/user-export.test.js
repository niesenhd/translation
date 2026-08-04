import { describe, expect, it, vi } from 'vitest'

import { downloadAllUsersCsv, userExportFilename } from './user-export'


describe('user CSV export', () => {
  it('prefers the encoded filename', () => {
    expect(
      userExportFilename("attachment; filename*=UTF-8''%E5%85%A8%E9%83%A8%E7%94%A8%E6%88%B7.csv"),
    ).toBe('全部用户.csv')
  })

  it('downloads every user without forwarding page or filter parameters', async () => {
    const blob = new Blob(['users'])
    const apiClient = {
      get: vi.fn().mockResolvedValue({
        data: blob,
        headers: { 'content-disposition': 'attachment; filename="users.csv"' },
      }),
    }
    const anchor = { href: '', download: '', click: vi.fn(), remove: vi.fn() }
    const documentRef = {
      body: { appendChild: vi.fn() },
      createElement: vi.fn().mockReturnValue(anchor),
    }
    const urlApi = {
      createObjectURL: vi.fn().mockReturnValue('blob:users'),
      revokeObjectURL: vi.fn(),
    }

    const filename = await downloadAllUsersCsv(apiClient, { documentRef, urlApi })

    expect(apiClient.get).toHaveBeenCalledWith('/admin/users/export', {
      responseType: 'blob',
    })
    expect(anchor.href).toBe('blob:users')
    expect(anchor.download).toBe('users.csv')
    expect(anchor.click).toHaveBeenCalledOnce()
    expect(anchor.remove).toHaveBeenCalledOnce()
    expect(urlApi.revokeObjectURL).toHaveBeenCalledWith('blob:users')
    expect(filename).toBe('users.csv')
  })
})
