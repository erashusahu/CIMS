import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { catalogApi, workspaceApi } from '../catalog/api'
import { SalesWorkspace } from './pages'

vi.mock('../catalog/api', async importOriginal => ({
  ...await importOriginal<typeof import('../catalog/api')>(),
  catalogApi: vi.fn(), workspaceApi: vi.fn(),
}))

const id = '00000000-0000-0000-0000-000000000001'
const customer = { customer_id: 'customer', code: 'BUYER', name: 'Buyer', is_active: true }
const location = { location_id: 'main', code: 'MAIN', name: 'Main', is_active: true }
const item = { item_id: 'soap', sku: 'SOAP', name: 'Soap', stock_uom_id: 'pcs' }
const order = {
  sales_order_id: 'order', order_no: 'SO/2026/00001', status: 'DRAFT', revision: 1,
  customer_id: 'customer', location_id: 'main', order_date: '2026-11-03',
  subtotal: '75', discount_total: '0', tax_total: '0', grand_total: '75',
  lines: [{ sales_order_line_id: 'line', item_id: 'soap', uom_id: 'pcs', location_id: 'main', quantity: '3', discount_pct: '0', line_total: '75', reserved_quantity: '0' }],
}

function view() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><SalesWorkspace workspaceId={id} canWrite /></QueryClientProvider>)
}

beforeEach(() => {
  vi.mocked(workspaceApi).mockReset()
  vi.mocked(catalogApi).mockReset()
  vi.mocked(workspaceApi).mockImplementation(async (_workspace, path, init) => {
    if (path === '/sales/orders?page_size=100') return { items: [order], total: 1, page: 1, page_size: 100 } as never
    if (path === '/sales/orders/order') return order as never
    if (path === '/customers?page_size=100') return { items: [customer] } as never
    if (path === '/inventory/locations') return [location] as never
    if (path.startsWith('/sales/customers/')) return { remaining_credit: '1000', credit_limit: '1000', credit_hold: false } as never
    if (path.startsWith('/sales/availability')) return { available_stock_units: '10', components: [] } as never
    if (path === '/sales/orders' && init?.method === 'POST') return order as never
    if (path === '/sales/orders/order/confirm') return { ...order, status: 'CONFIRMED', revision: 2 } as never
    return {} as never
  })
  vi.mocked(catalogApi).mockResolvedValue({ items: [item] } as never)
})
afterEach(cleanup)

it('sends a sales draft with item, location and quantity', async () => {
  view()
  fireEvent.click(screen.getByRole('button', { name: 'New sales order' }))
  fireEvent.change(await screen.findByLabelText('Item'), { target: { value: 'soap' } })
  fireEvent.change(screen.getByLabelText('Quantity'), { target: { value: '3' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
  await waitFor(() => expect(vi.mocked(workspaceApi).mock.calls.some(call => call[1] === '/sales/orders' && call[2]?.method === 'POST')).toBe(true))
  const request = vi.mocked(workspaceApi).mock.calls.find(call => call[1] === '/sales/orders' && call[2]?.method === 'POST')!
  expect(JSON.parse(String(request[2]?.body))).toMatchObject({ customer_id: 'customer', location_id: 'main', lines: [{ item_id: 'soap', uom_id: 'pcs', quantity: '3' }] })
})

it('confirms a reviewed draft with its revision and an action key', async () => {
  view()
  fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Confirm and reserve' }))
  await waitFor(() => expect(vi.mocked(workspaceApi).mock.calls.some(call => call[1] === '/sales/orders/order/confirm')).toBe(true))
  const request = vi.mocked(workspaceApi).mock.calls.find(call => call[1] === '/sales/orders/order/confirm')!
  const payload = JSON.parse(String(request[2]?.body))
  expect(payload.expected_revision).toBe(1)
  expect(payload.idempotency_key).toBeTruthy()
})
