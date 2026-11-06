import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { catalogApi, workspaceApi } from '../catalog/api'
import { OpeningStockView } from './opening'

vi.mock('../catalog/api', async importOriginal => ({
  ...await importOriginal<typeof import('../catalog/api')>(), catalogApi: vi.fn(), workspaceApi: vi.fn(),
}))

const document = {
  opening_stock_id: 'opening-one', document_no: 'OPEN/2026-27/00001', business_date: '2026-01-01',
  version: 1, status: 'DRAFT', notes: null, created_at: '2026-01-01T00:00:00Z', posted_at: null,
  lines: [{ opening_stock_line_id: 'line-one', line_no: 1, item_id: 'soap', location_id: 'main',
    entered_uom_id: 'pcs', entered_quantity: '10', conversion_factor: '1', stock_uom_id: 'pcs',
    stock_quantity: '10', unit_cost: '100.123456', item_catalog_version: 1,
    sku_snapshot: 'SOAP', item_name_snapshot: 'Soap', stock_unit_snapshot: 'PCS', location_code_snapshot: 'MAIN' }],
}

function view() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><OpeningStockView workspaceId="business-one" /></QueryClientProvider>)
}

beforeEach(() => {
  vi.mocked(catalogApi).mockReset()
  vi.mocked(workspaceApi).mockReset()
  vi.mocked(catalogApi).mockImplementation(async (_workspace, path) => (path === '/options' ? {
    units: [{ uom_id: 'pcs', code: 'PCS', name: 'Pieces', is_active: true }],
  } : { items: [{ item_id: 'soap', sku: 'SOAP', name: 'Soap', item_kind: 'SIMPLE', stock_uom_id: 'pcs' }], total: 1 }) as never)
  vi.mocked(workspaceApi).mockImplementation(async (_workspace, path) => {
    if (path.endsWith('/opening-status')) return { ready: true, can_write: true } as never
    if (path.endsWith('/locations')) return [{ location_id: 'main', code: 'MAIN', name: 'Main warehouse', is_active: true }] as never
    return { items: [document], total: 1 } as never
  })
})
afterEach(cleanup)

it('blocks posting until changed draft fields have been saved for review', async () => {
  view()
  fireEvent.click(await screen.findByRole('button', { name: /^Open$/ }))
  expect(await screen.findByRole('button', { name: 'Post opening stock' })).toBeEnabled()
  fireEvent.change(screen.getByLabelText('Quantity'), { target: { value: '12' } })
  expect(screen.getByRole('button', { name: 'Post opening stock' })).toBeDisabled()
  expect(screen.getByText(/Save the draft to refresh/)).toBeInTheDocument()
})

it('reuses the same posting key after a failed response', async () => {
  const original = vi.mocked(workspaceApi).getMockImplementation()!
  let attempts = 0
  vi.mocked(workspaceApi).mockImplementation(async (business, path, init) => {
    if (path.endsWith('/post')) {
      if (++attempts === 1) throw new Error('Response was interrupted')
      return { ...document, status: 'POSTED', version: 2 } as never
    }
    return original(business, path, init)
  })
  view()
  fireEvent.click(await screen.findByRole('button', { name: /^Open$/ }))
  fireEvent.click(await screen.findByRole('button', { name: 'Post opening stock' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Retry posting' }))
  expect(await screen.findByText(/Posted. Physical and accounting entries/)).toBeInTheDocument()
  const calls = vi.mocked(workspaceApi).mock.calls.filter(call => call[1].endsWith('/post'))
  expect(calls).toHaveLength(2)
  const firstKey = new Headers(calls[0][2]?.headers).get('Idempotency-Key')
  expect(firstKey).toBeTruthy()
  expect(new Headers(calls[1][2]?.headers).get('Idempotency-Key')).toBe(firstKey)
})

it('saves decimal quantities and costs as strings without posting', async () => {
  const original = vi.mocked(workspaceApi).getMockImplementation()!
  vi.mocked(workspaceApi).mockImplementation(async (business, path, init) => init?.method === 'PATCH'
    ? { ...document, version: 2 } as never : original(business, path, init))
  view()
  fireEvent.click(await screen.findByRole('button', { name: /^Open$/ }))
  fireEvent.click(await screen.findByRole('button', { name: 'Save draft for review' }))
  await waitFor(() => expect(vi.mocked(workspaceApi).mock.calls.some(call => call[2]?.method === 'PATCH')).toBe(true))
  const saved = vi.mocked(workspaceApi).mock.calls.find(call => call[2]?.method === 'PATCH')!
  expect(JSON.parse(String(saved[2]?.body))).toMatchObject({ expected_version: 1,
    lines: [{ quantity: '10', unit_cost: '100.123456' }] })
  expect(vi.mocked(workspaceApi).mock.calls.some(call => call[1].endsWith('/post'))).toBe(false)
})

it('shows missing-schema instructions without offering a posting action', async () => {
  vi.mocked(workspaceApi).mockResolvedValue({ ready: false, can_write: true } as never)
  view()
  expect(await screen.findByText('Opening-stock setup is required')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'New opening-stock draft' })).not.toBeInTheDocument()
})
