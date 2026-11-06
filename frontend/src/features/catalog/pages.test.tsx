import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { CatalogView, CatalogWorkspace } from './pages'
import { catalogApi, workspaceApi, type CatalogOptions } from './api'

vi.mock('./api', async importOriginal => ({
  ...await importOriginal<typeof import('./api')>(), catalogApi: vi.fn(), workspaceApi: vi.fn(), downloadTemplate: vi.fn(),
}))

const workspace = '00000000-0000-0000-0000-000000000001'
const unit = '00000000-0000-0000-0000-000000000002'
const classification = '00000000-0000-0000-0000-000000000003'
const options: CatalogOptions = {
  can_write: true, local_mode: true, default_valuation_method: 'WEIGHTED_AVERAGE',
  units: [{ uom_id: unit, code: 'PCS', name: 'Pieces', uom_class: 'COUNT', decimal_places: 0, is_active: true }],
  item_types: [{ item_type_id: classification, code: 'GOODS', name: 'Goods', is_stockable: true }],
  groups: [], hsn_codes: [], cost_centers: [],
}

function view(component = <CatalogView workspaceId={workspace} />) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}>{component}</QueryClientProvider>)
}

beforeEach(() => {
  vi.mocked(catalogApi).mockReset()
  vi.mocked(catalogApi).mockImplementation(async (_workspace, path) => {
    if (path === '/options') return options as never
    if (path.startsWith('/items?')) return { items: [], total: 0, page: 1, page_size: 25 } as never
    return {} as never
  })
  vi.mocked(workspaceApi).mockReset()
  vi.mocked(workspaceApi).mockImplementation(async (_workspace, path) => {
    if (path === '/inventory/opening-status') return { ready: true, can_write: true } as never
    if (path === '/inventory/locations') return [] as never
    if (path.startsWith('/inventory/balances?')) return { items: [], total: 0, page: 1, page_size: 1 } as never
    return {} as never
  })
  sessionStorage.clear()
})
afterEach(cleanup)

async function uploadCsv(name = 'items.csv', source = 'sku,name,stock_unit,item_type\nSOAP,Soap,PCS,GOODS') {
  const file = new File([source], name, { type: 'text/csv' })
  Object.defineProperty(file, 'text', { value: async () => source })
  fireEvent.change(screen.getByLabelText('Choose an item CSV'), { target: { files: [file] } })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Validate CSV' })).toBeEnabled())
}

describe('catalog workspace', () => {
  it('starts with a guided checklist and opens the next item step', async () => {
    view()
    expect(await screen.findByRole('heading', { name: 'Four steps to get started' })).toBeInTheDocument()
    expect(screen.getByRole('status', { name: '1 of 4 steps complete' })).toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', { name: 'Open items' }))
    expect(await screen.findByText('Your catalog is empty')).toBeInTheDocument()
  })

  it('takes the location step directly to opening stock', async () => {
    const original = vi.mocked(catalogApi).getMockImplementation()!
    vi.mocked(catalogApi).mockImplementation(async (business, path, init) => path.startsWith('/items?')
      ? { items: [{ item_id: unit, name: 'Soap' }], total: 1, page: 1, page_size: 1 } as never
      : original(business, path, init))
    view()
    fireEvent.click(await screen.findByRole('button', { name: 'Add a location' }))
    expect(await screen.findByRole('button', { name: 'Add storage location' })).toBeInTheDocument()
  })

  it('places catalog sections in the sidebar and switches the visible section', async () => {
    const sidebar = document.createElement('aside')
    sidebar.id = 'catalog-sidebar-navigation'
    document.body.append(sidebar)
    try {
      view()
      fireEvent.click(await screen.findByText('More tools'))
      const setup = await screen.findByRole('tab', { name: 'Units & variants' })
      expect(sidebar).toContainElement(setup)
      fireEvent.click(setup)
      expect(screen.getByRole('heading', { name: 'Units & variants' })).toBeInTheDocument()
      expect(setup).toHaveAttribute('aria-selected', 'true')
    } finally {
      sidebar.remove()
    }
  })

  it('shows an empty catalog without invented stock quantities', async () => {
    view()
    fireEvent.click(await screen.findByRole('tab', { name: 'Items' }))
    expect(await screen.findByText('Your catalog is empty')).toBeInTheDocument()
    expect(screen.queryByText(/physical on hand|available stock|stock value/i)).not.toBeInTheDocument()
  })

  it('submits prices as decimal strings without tenant or stock-write fields', async () => {
    view()
    fireEvent.click(await screen.findByRole('tab', { name: 'Items' }))
    fireEvent.click(await screen.findByRole('button', { name: 'New item' }))
    fireEvent.change(screen.getByLabelText('Item name'), { target: { value: 'Soap bar' } })
    fireEvent.change(screen.getByLabelText('SKU'), { target: { value: 'SOAP' } })
    fireEvent.change(screen.getByLabelText('Selling price (INR)'), { target: { value: '25.123456' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create item' }))
    await waitFor(() => expect(vi.mocked(catalogApi).mock.calls.some(call => call[1] === '/items' && call[2]?.method === 'POST')).toBe(true))
    const call = vi.mocked(catalogApi).mock.calls.find(call => call[1] === '/items' && call[2]?.method === 'POST')!
    const payload = JSON.parse(String(call[2]?.body))
    expect(call[0]).toBe(workspace)
    expect(payload).toMatchObject({ sku: 'SOAP', name: 'Soap bar', stock_uom_id: unit, selling_price: '25.123456' })
    expect(payload).not.toHaveProperty('tenant_id')
    expect(payload).not.toHaveProperty('stock_quantity')
  })

  it('hides editing and import actions for read-only users', async () => {
    vi.mocked(catalogApi).mockImplementation(async (_workspace, path) => path === '/options' ? { ...options, can_write: false } as never : { items: [], total: 0, page: 1, page_size: 25 } as never)
    view()
    fireEvent.click(await screen.findByRole('tab', { name: 'Items' }))
    await screen.findByText('Your catalog is empty')
    expect(screen.queryByRole('button', { name: 'New item' })).not.toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: 'CSV import' })).not.toBeInTheDocument()
  })

  it('shows CSV issues and does not offer import after validation failure', async () => {
    const original = vi.mocked(catalogApi).getMockImplementation()!
    vi.mocked(catalogApi).mockImplementation(async (business, path, init) => path === '/imports/validate' ? {
      row_count: 1, valid_count: 0, can_import: false, rows: [],
      issues: [{ row: 2, field: 'sku', message: 'Duplicate sku in this business or CSV' }],
    } as never : original(business, path, init))
    view()
    fireEvent.click(await screen.findByText('More tools'))
    fireEvent.click(await screen.findByRole('tab', { name: 'CSV import' }))
    await uploadCsv()
    fireEvent.click(screen.getByRole('button', { name: 'Validate CSV' }))
    expect(await screen.findByText('Duplicate sku in this business or CSV')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Import \d/ })).not.toBeInTheDocument()
    expect(vi.mocked(catalogApi).mock.calls.some(call => call[1] === '/imports/commit')).toBe(false)
  })

  it('clears a successful preview when another file is selected', async () => {
    const original = vi.mocked(catalogApi).getMockImplementation()!
    vi.mocked(catalogApi).mockImplementation(async (business, path, init) => path === '/imports/validate' ? {
      row_count: 1, valid_count: 1, can_import: true, rows: [{ sku: 'SOAP', name: 'Soap' }], issues: [],
    } as never : original(business, path, init))
    view()
    fireEvent.click(await screen.findByText('More tools'))
    fireEvent.click(await screen.findByRole('tab', { name: 'CSV import' }))
    await uploadCsv()
    fireEvent.click(screen.getByRole('button', { name: 'Validate CSV' }))
    expect(await screen.findByRole('button', { name: 'Import 1 items' })).toBeInTheDocument()
    await uploadCsv('different.csv')
    expect(screen.queryByRole('button', { name: 'Import 1 items' })).not.toBeInTheDocument()
  })

  it('keeps catalog actions unavailable until database setup is ready', async () => {
    vi.mocked(catalogApi).mockImplementation(async (_workspace, path) => path === '/local-status' ? { ready: false } as never : [] as never)
    view(<CatalogWorkspace />)
    expect(await screen.findByText('Finish catalog setup')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'New item' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Check setup again' })).toBeInTheDocument()
  })
})
