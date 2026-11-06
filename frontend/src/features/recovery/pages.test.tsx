import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { catalogApi, workspaceApi } from '../catalog/api'
import { RecoveryWorkspace } from './pages'

vi.mock('../catalog/api', async importOriginal => ({
  ...await importOriginal<typeof import('../catalog/api')>(),
  catalogApi: vi.fn(), workspaceApi: vi.fn(),
}))

afterEach(() => { cleanup(); vi.mocked(workspaceApi).mockReset(); vi.mocked(catalogApi).mockReset() })

function view() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(<QueryClientProvider client={client}><RecoveryWorkspace workspaceId="workspace" canWrite /></QueryClientProvider>)
}

it('shows the count guard SQL when the database is not ready', async () => {
  vi.mocked(workspaceApi).mockImplementation(async (_workspace, path) => path === '/recovery/status'
    ? { ready: false, required_sql: '008_cims_returns_counts_guards.sql' } as never : [] as never)
  view()
  expect(await screen.findByText(/Apply 008_cims_returns_counts_guards.sql/)).toBeInTheDocument()
})

it('saves a shipment-linked physical return with condition and reason', async () => {
  vi.mocked(workspaceApi).mockImplementation(async (_workspace, path, init) => {
    if (path === '/recovery/status') return { ready: true } as never
    if (path === '/recovery/shipped-options') return [{ shipment_id: 'shipment', shipment_no: 'SHP-1', from_location_id: 'location', lines: [{ item_id: 'soap', quantity_in_stock_uom: '4' }] }] as never
    if (path === '/transfers/locations') return [{ location_id: 'location', code: 'MAIN', name: 'Main', is_active: true }] as never
    if (path === '/recovery/returns' && init?.method === 'POST') return { sales_return_id: 'return' } as never
    if (path === '/recovery/returns/return') return { sales_return_id: 'return', return_no: 'RMA-1', status: 'DRAFT', shipment_id: 'shipment', reason_note: 'Damaged on arrival', lines: [] } as never
    if (path === '/recovery/returns' || path === '/recovery/counts') return [] as never
    return {} as never
  })
  vi.mocked(catalogApi).mockResolvedValue({ items: [{ item_id: 'soap', sku: 'SOAP', name: 'Soap', item_kind: 'STANDALONE' }] } as never)
  view()
  await screen.findByRole('option', { name: /SHP-1/ })
  fireEvent.change(await screen.findByLabelText('Shipment'), { target: { value: 'shipment' } })
  fireEvent.change(screen.getByLabelText('Item'), { target: { value: 'soap' } })
  fireEvent.change(screen.getByLabelText('Quantity'), { target: { value: '2' } })
  fireEvent.change(screen.getByLabelText('Condition'), { target: { value: 'DAMAGED' } })
  fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Damaged on arrival' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save return draft' }))
  await waitFor(() => expect(vi.mocked(workspaceApi).mock.calls.some(call => call[1] === '/recovery/returns' && call[2]?.method === 'POST')).toBe(true))
  const request = vi.mocked(workspaceApi).mock.calls.find(call => call[1] === '/recovery/returns' && call[2]?.method === 'POST')!
  expect(JSON.parse(String(request[2]?.body))).toMatchObject({ shipment_id: 'shipment', reason_note: 'Damaged on arrival', lines: [{ item_id: 'soap', quantity: '2', condition: 'DAMAGED' }] })
})
