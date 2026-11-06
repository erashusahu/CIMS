import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { catalogApi, workspaceApi } from '../catalog/api'
import { TransfersWorkspace } from './pages'

vi.mock('../catalog/api', async importOriginal => ({
  ...await importOriginal<typeof import('../catalog/api')>(),
  catalogApi: vi.fn(), workspaceApi: vi.fn(),
}))

afterEach(() => { cleanup(); vi.mocked(workspaceApi).mockReset(); vi.mocked(catalogApi).mockReset() })

function view() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(<QueryClientProvider client={client}><TransfersWorkspace workspaceId="workspace" canWrite /></QueryClientProvider>)
}

it('shows the required schema while transfer SQL is missing', async () => {
  vi.mocked(workspaceApi).mockImplementation(async (_workspace, path) => {
    if (path === '/transfers/status') return { ready: false, required_sql: '007_cims_transfer_extension.sql' } as never
    return [] as never
  })
  vi.mocked(catalogApi).mockResolvedValue({ items: [] } as never)
  view()
  expect(await screen.findByText(/Apply 007_cims_transfer_extension.sql/)).toBeInTheDocument()
})

it('submits a stock-unit transfer draft between selected locations', async () => {
  const draft = { transfer_order_id: 'transfer', transfer_no: 'TRF-1', status: 'DRAFT',
    from_location_id: 'source', to_location_id: 'destination', in_transit_location_id: 'transit',
    lines: [], operations: [], notes: null }
  vi.mocked(workspaceApi).mockImplementation(async (_workspace, path, init) => {
    if (path === '/transfers/status') return { ready: true } as never
    if (path === '/transfers/in-transit') return [] as never
    if (path === '/transfers' && init?.method === 'POST') return draft as never
    if (path === '/transfers') return [] as never
    if (path === '/transfers/transfer') return draft as never
    if (path === '/transfers/locations') return [
      { location_id: 'source', code: 'SRC', name: 'Source', is_active: true, state_code: '29', tax_registration_id: 'reg' },
      { location_id: 'destination', code: 'DEST', name: 'Destination', is_active: true, state_code: '29', tax_registration_id: 'reg' },
    ] as never
    if (path === '/transfers/registrations') return [{ tax_registration_id: 'reg', gstin: '29ABCDE1234F1Z5', legal_name: 'Business', state_code: '29' }] as never
    return {} as never
  })
  vi.mocked(catalogApi).mockResolvedValue({ items: [{ item_id: 'soap', sku: 'SOAP', name: 'Soap', item_kind: 'STANDALONE' }] } as never)
  view()
  fireEvent.click(await screen.findByRole('button', { name: 'New transfer' }))
  fireEvent.change(await screen.findByLabelText('From location'), { target: { value: 'source' } })
  fireEvent.change(screen.getByLabelText('To location'), { target: { value: 'destination' } })
  fireEvent.change(screen.getByLabelText('Item'), { target: { value: 'soap' } })
  fireEvent.change(screen.getByLabelText('Quantity'), { target: { value: '40' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
  await waitFor(() => expect(vi.mocked(workspaceApi).mock.calls.some(call => call[1] === '/transfers' && call[2]?.method === 'POST')).toBe(true))
  const request = vi.mocked(workspaceApi).mock.calls.find(call => call[1] === '/transfers' && call[2]?.method === 'POST')!
  expect(JSON.parse(String(request[2]?.body))).toMatchObject({ from_location_id: 'source', to_location_id: 'destination', lines: [{ item_id: 'soap', quantity: '40' }] })
})
