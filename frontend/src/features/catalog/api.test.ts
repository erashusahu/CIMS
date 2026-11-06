import { afterEach, describe, expect, it, vi } from 'vitest'

afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); vi.resetModules() })

describe('local catalog requests', () => {
  it('passes the selected workspace explicitly', async () => {
    vi.stubEnv('VITE_CATALOG_LOCAL_MODE', 'true')
    vi.resetModules()
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [] }), { status: 200 }))
    vi.stubGlobal('fetch', fetcher)
    const { catalogApi } = await import('./api')
    await catalogApi('workspace-a', '/items')
    expect(new Headers(fetcher.mock.calls[0][1].headers).get('X-CIMS-Tenant-ID')).toBe('workspace-a')
  })

  it('does not try authentication refresh when sign-in is deferred', async () => {
    vi.stubEnv('VITE_CATALOG_LOCAL_MODE', 'true')
    vi.resetModules()
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { message: 'Unavailable' } }), { status: 401 }))
    vi.stubGlobal('fetch', fetcher)
    const { catalogApi } = await import('./api')
    await expect(catalogApi('workspace-a', '/items')).rejects.toThrow('Unavailable')
    expect(fetcher).toHaveBeenCalledTimes(1)
  })
})
