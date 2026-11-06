import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { catalogApi, workspaceApi } from '../catalog/api'
import { ProcurementView } from './pages'

vi.mock('../catalog/api', async importOriginal => ({
  ...await importOriginal<typeof import('../catalog/api')>(),
  catalogApi: vi.fn(), workspaceApi: vi.fn(),
}))

const workspaceId = '00000000-0000-0000-0000-000000000001'
const supplier = {
  vendor_id: '00000000-0000-0000-0000-000000000002', code: 'SUP-001', name: 'First supplier',
  legal_name: null, gstin: null, email: null, phone: null, tax_treatment: 'REGISTERED',
  payment_terms_days: 30, lead_time_days: 7, is_active: true, notes: null,
  created_at: '2026-11-01T00:00:00Z', updated_at: '2026-11-01T00:00:00Z',
}

function view(canWrite = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><ProcurementView workspaceId={workspaceId} canWrite={canWrite} /></QueryClientProvider>)
}

beforeEach(() => {
  vi.mocked(workspaceApi).mockReset()
  vi.mocked(catalogApi).mockReset()
  vi.mocked(workspaceApi).mockImplementation(async (_workspace, path) => {
    if (path.startsWith('/procurement/suppliers?')) return { items: [supplier], total: 1, page: 1, page_size: 25 } as never
    if (path.startsWith('/procurement/preferred-suppliers?')) return { items: [], total: 0, page: 1, page_size: 25 } as never
    return supplier as never
  })
  vi.mocked(catalogApi).mockResolvedValue({ items: [{ item_id: 'soap', sku: 'SOAP', name: 'Soap', item_kind: 'STANDALONE' }], total: 1 } as never)
})
afterEach(cleanup)

it('shows supplier details without edit actions to a read-only user', async () => {
  view(false)
  fireEvent.click(await screen.findByRole('button', { name: 'View' }))
  expect(screen.getByLabelText('Supplier name')).toBeDisabled()
  expect(screen.queryByRole('button', { name: 'Save supplier' })).not.toBeInTheDocument()
  expect(screen.queryByRole('tab', { name: 'Supplier CSV' })).not.toBeInTheDocument()
})

it('submits supplier details without a tenant or stock mutation', async () => {
  view()
  fireEvent.click(await screen.findByRole('button', { name: 'New supplier' }))
  fireEvent.change(screen.getByLabelText('Supplier code'), { target: { value: 'SUP-002' } })
  fireEvent.change(screen.getByLabelText('Supplier name'), { target: { value: 'Second supplier' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save supplier' }))
  await waitFor(() => expect(vi.mocked(workspaceApi).mock.calls.some(call => call[1] === '/procurement/suppliers' && call[2]?.method === 'POST')).toBe(true))
  const call = vi.mocked(workspaceApi).mock.calls.find(value => value[1] === '/procurement/suppliers' && value[2]?.method === 'POST')!
  const payload = JSON.parse(String(call[2]?.body))
  expect(payload).toMatchObject({ code: 'SUP-002', name: 'Second supplier' })
  expect(payload).not.toHaveProperty('tenant_id')
  expect(payload).not.toHaveProperty('stock_quantity')
})

it('keeps CSV import separate from preview and blocks import on row errors', async () => {
  const original = vi.mocked(workspaceApi).getMockImplementation()!
  vi.mocked(workspaceApi).mockImplementation(async (business, path, init) => path.endsWith('/imports/validate') ? {
    row_count: 1, valid_count: 0, can_import: false, rows: [],
    issues: [{ row: 2, field: 'code', message: 'Supplier code already exists' }],
  } as never : original(business, path, init))
  view()
  fireEvent.click(screen.getByRole('tab', { name: 'Supplier CSV' }))
  fireEvent.change(screen.getByLabelText('CSV contents'), { target: { value: 'code,name,gstin,email,phone,lead_time_days,payment_terms_days\nSUP-001,First supplier,,,,7,30' } })
  fireEvent.click(screen.getByRole('button', { name: 'Validate CSV' }))
  expect(await screen.findByText(/Supplier code already exists/)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /^Import \d/ })).not.toBeInTheDocument()
  expect(vi.mocked(workspaceApi).mock.calls.some(call => call[1].endsWith('/imports/commit'))).toBe(false)
})

it('sets a preferred supplier without creating an order or stock movement', async () => {
  view()
  fireEvent.click(screen.getByRole('tab', { name: 'Preferred sources' }))
  await screen.findByRole('option', { name: /SUP-001/ })
  fireEvent.change(screen.getByLabelText('Catalog item'), { target: { value: 'soap' } })
  fireEvent.change(screen.getByLabelText('Supplier', { exact: true }), { target: { value: supplier.vendor_id } })
  fireEvent.change(screen.getByLabelText('Price per purchase unit (INR)'), { target: { value: '20.123456' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save preferred supplier' }))
  await waitFor(() => expect(vi.mocked(workspaceApi).mock.calls.some(call => call[1] === '/procurement/preferred-suppliers' && call[2]?.method === 'POST')).toBe(true))
  const call = vi.mocked(workspaceApi).mock.calls.find(value => value[1] === '/procurement/preferred-suppliers' && value[2]?.method === 'POST')!
  expect(JSON.parse(String(call[2]?.body))).toMatchObject({ item_id: 'soap', vendor_id: supplier.vendor_id, unit_price: '20.123456' })
  expect(vi.mocked(workspaceApi).mock.calls.some(value => value[1].startsWith('/inventory/'))).toBe(false)
})

it('turns a reorder suggestion into a reviewed purchase-order draft', async () => {
  const original = vi.mocked(workspaceApi).getMockImplementation()!
  vi.mocked(workspaceApi).mockImplementation(async (business, path, init) => {
    if (path === '/procurement/suggestions') return [{ vendor_id: supplier.vendor_id, vendor_name: supplier.name, items: [{ item_id: 'soap', item_sku: 'SOAP', location_id: 'main', location_code: 'MAIN', vendor_id: supplier.vendor_id, vendor_name: supplier.name, projected_available: '0', outstanding_order_qty: '0', suggested_qty: '10', method: 'REORDER_POINT' }] }] as never
    if (path.startsWith('/procurement/reorder-rules?') || path.startsWith('/procurement/purchase-orders?')) return { items: [], total: 0, page: 1, page_size: 100 } as never
    if (path === '/inventory/locations') return [{ location_id: 'main', code: 'MAIN', name: 'Main', is_active: true }] as never
    return original(business, path, init)
  })
  vi.mocked(catalogApi).mockImplementation(async (_business, path) => {
    if (path === '/options') return { units: [{ uom_id: 'pcs', code: 'PCS', name: 'Pieces', is_active: true }] } as never
    return { items: [{ item_id: 'soap', sku: 'SOAP', name: 'Soap', item_kind: 'STANDALONE', stock_uom_id: 'pcs' }], total: 1 } as never
  })
  view()
  fireEvent.click(screen.getByRole('tab', { name: 'Reorder' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Draft order' }))
  expect(screen.getByRole('tab', { name: 'Purchase orders' })).toHaveAttribute('aria-selected', 'true')
  await waitFor(() => expect(screen.getByLabelText('Item')).toHaveValue('soap'))
  expect(screen.getByLabelText('Quantity')).toHaveValue(10)
  fireEvent.change(screen.getByLabelText('Price per selected unit (INR)'), { target: { value: '20' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
  await waitFor(() => expect(vi.mocked(workspaceApi).mock.calls.some(call => call[1] === '/procurement/purchase-orders' && call[2]?.method === 'POST')).toBe(true))
  const request = vi.mocked(workspaceApi).mock.calls.find(call => call[1] === '/procurement/purchase-orders' && call[2]?.method === 'POST')!
  expect(JSON.parse(String(request[2]?.body))).toMatchObject({ vendor_id: supplier.vendor_id, location_id: 'main', lines: [{ item_id: 'soap', uom_id: 'pcs', quantity: '10', unit_price: '20' }] })
})
