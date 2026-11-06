import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { catalogApi, workspaceApi } from '../catalog/api'
import { PosWorkspace } from './pages'

vi.mock('../catalog/api', async importOriginal => ({
  ...await importOriginal<typeof import('../catalog/api')>(),
  catalogApi: vi.fn(), workspaceApi: vi.fn(),
}))

afterEach(() => { cleanup(); vi.mocked(workspaceApi).mockReset(); vi.mocked(catalogApi).mockReset() })

function view() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(<QueryClientProvider client={client}><PosWorkspace workspaceId="workspace" canWrite /></QueryClientProvider>)
}

it('shows migration guidance before POS carts are available', async () => {
  vi.mocked(workspaceApi).mockImplementation(async (_workspace, path) => path === '/pos/status'
    ? { ready: false, checkout_ready: false, required_sql: '009_cims_pos_draft_source.sql' } as never : [] as never)
  view()
  expect(await screen.findByText(/Apply 009_cims_pos_draft_source.sql/)).toBeInTheDocument()
})

it('saves a POS sourced draft without checking out', async () => {
  vi.mocked(workspaceApi).mockImplementation(async (_workspace, path, init) => {
    if (path === '/pos/status') return { ready: true, checkout_ready: false } as never
    if (path === '/sales/orders?source=POS&page_size=100') return { items: [], total: 0 } as never
    if (path === '/customers?page_size=100') return { items: [{ customer_id: 'customer', code: 'WALKIN', name: 'Walk-in', is_active: true }] } as never
    if (path === '/inventory/locations') return [{ location_id: 'store', code: 'STORE', name: 'Store', is_active: true }] as never
    if (path === '/sales/orders' && init?.method === 'POST') return { sales_order_id: 'order' } as never
    if (path === '/sales/orders/order') return { sales_order_id: 'order', order_no: 'SO-1', status: 'DRAFT', customer_id: 'customer', location_id: 'store', lines: [] } as never
    return {} as never
  })
  vi.mocked(catalogApi).mockResolvedValue({ items: [{ item_id: 'soap', sku: 'SOAP', name: 'Soap', barcode: '123456', stock_uom_id: 'pcs' }] } as never)
  view()
  await screen.findByRole('option', { name: /STORE · Store/ })
  await screen.findByRole('option', { name: /WALKIN · Walk-in/ })
  fireEvent.change(await screen.findByLabelText('Store location'), { target: { value: 'store' } })
  fireEvent.change(screen.getByLabelText('Customer'), { target: { value: 'customer' } })
  fireEvent.change(screen.getByLabelText('Scan barcode or search SKU'), { target: { value: '123456' } })
  await screen.findByRole('button', { name: 'Add' })
  fireEvent.keyDown(screen.getByLabelText('Scan barcode or search SKU'), { key: 'Enter' })
  fireEvent.change(screen.getByLabelText('Quantity for SOAP'), { target: { value: '2' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save POS draft' }))
  await waitFor(() => expect(vi.mocked(workspaceApi).mock.calls.some(call => call[1] === '/sales/orders' && call[2]?.method === 'POST')).toBe(true))
  const request = vi.mocked(workspaceApi).mock.calls.find(call => call[1] === '/sales/orders' && call[2]?.method === 'POST')!
  expect(JSON.parse(String(request[2]?.body))).toMatchObject({ sales_source: 'POS', customer_id: 'customer', location_id: 'store', lines: [{ item_id: 'soap', quantity: '2' }] })
})
