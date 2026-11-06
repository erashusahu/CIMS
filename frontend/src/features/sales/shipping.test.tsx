import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { workspaceApi } from '../catalog/api'
import { ShippingPanel } from './shipping'

vi.mock('../catalog/api', async importOriginal => ({
  ...await importOriginal<typeof import('../catalog/api')>(), workspaceApi: vi.fn(),
}))

const order = {
  sales_order_id: 'order', order_no: 'SO-1', status: 'CONFIRMED', revision: 2,
  customer_id: 'customer', location_id: 'main', order_date: '2026-11-03',
  subtotal: '75', discount_total: '0', tax_total: '0', grand_total: '75',
  lines: [{ sales_order_line_id: 'line', line_no: 1, item_id: 'soap', uom_id: 'pcs',
    location_id: 'main', quantity: '3', quantity_in_stock_uom: '3',
    shipped_quantity: '0', cancelled_quantity: '0', reserved_quantity: '3' }],
  reservations: [],
}

afterEach(() => { cleanup(); vi.mocked(workspaceApi).mockReset() })

it('records a keyboard scan and packs the selected partial quantity', async () => {
  vi.mocked(workspaceApi).mockImplementation(async (_workspace, path) => {
    if (path === '/sales/shipping-status') return { ready: true } as never
    if (path.endsWith('/pick-list')) return { sales_order_id: 'order', order_no: 'SO-1', status: 'CONFIRMED', entries: [
      { reservation_id: 'reservation', sales_order_line_id: 'line', item_id: 'soap', sku: 'SOAP', barcode: '123',
        location_id: 'main', location_code: 'MAIN', reserved_quantity: '3', shipped_quantity: '0', picked_unshipped: '0', remaining_to_pick: '3' },
    ] } as never
    if (path.endsWith('/shipments') && !path.includes('/shipments/')) return [] as never
    return {} as never
  })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(<QueryClientProvider client={client}><ShippingPanel workspaceId="workspace" order={order as never} /></QueryClientProvider>)
  fireEvent.change(await screen.findByLabelText('Pick item'), { target: { value: 'reservation' } })
  fireEvent.change(screen.getByLabelText('Scan barcode or SKU'), { target: { value: '123' } })
  fireEvent.click(screen.getByRole('button', { name: 'Verify pick' }))
  await waitFor(() => expect(vi.mocked(workspaceApi).mock.calls.some(call => call[1] === '/sales/orders/order/scans' && call[2]?.method === 'POST')).toBe(true))
  const scan = vi.mocked(workspaceApi).mock.calls.find(call => call[1] === '/sales/orders/order/scans')!
  expect(JSON.parse(String(scan[2]?.body))).toMatchObject({ reservation_id: 'reservation', scanned_code: '123', quantity: '1' })
  fireEvent.change(screen.getByLabelText(/Ship quantity · 1/), { target: { value: '2' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save packed shipment' }))
  await waitFor(() => expect(vi.mocked(workspaceApi).mock.calls.some(call => call[1] === '/sales/orders/order/shipments' && call[2]?.method === 'POST')).toBe(true))
  const packed = vi.mocked(workspaceApi).mock.calls.find(call => call[1] === '/sales/orders/order/shipments' && call[2]?.method === 'POST')!
  expect(JSON.parse(String(packed[2]?.body))).toMatchObject({ from_location_id: 'main',
    lines: [{ sales_order_line_id: 'line', quantity: '2' }],
    packages: [{ contents: [{ reservation_id: 'reservation', quantity: '2' }] }],
  })
})
