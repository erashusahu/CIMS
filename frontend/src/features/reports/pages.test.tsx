import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { catalogApi, workspaceApi } from '../catalog/api'
import { ReportsWorkspace } from './pages'

vi.mock('../catalog/api', () => ({ workspaceApi: vi.fn(), catalogApi: vi.fn(), formatMoney: (value: string) => `₹${value}` }))
afterEach(() => { cleanup(); vi.mocked(workspaceApi).mockReset(); vi.mocked(catalogApi).mockReset() })

it('labels the operational overview as whole-business counts', async () => {
  vi.mocked(workspaceApi).mockResolvedValue({
    scope: 'WHOLE_BUSINESS', as_of: '2026-11-06T00:00:00Z', active_items: 2,
    stock_positions: 3, below_safety_positions: 1, open_purchase_orders: 4,
    confirmed_sales_orders: 5, active_counts: 1, pending_outbox_events: 0,
  } as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><ReportsWorkspace workspaceId="tenant" /></QueryClientProvider>)
  expect(await screen.findByText(/Whole-business operational counts/)).toBeInTheDocument()
  expect(screen.getByText('At or below safety stock')).toBeInTheDocument()
  expect(vi.mocked(workspaceApi).mock.calls[0][1]).toBe('/reports/overview')
})

it('filters stock by location and sales summaries by date', async () => {
  vi.mocked(workspaceApi).mockImplementation(async (_workspace, path) => {
    if (path === '/inventory/locations') return [{ location_id: 'loc-a', code: 'A', name: 'Main' }] as never
    if (path.startsWith('/inventory/balances?')) return { items: [], total: 0, page: 1, page_size: 25 } as never
    if (path.startsWith('/reports/sales-summary?')) return [] as never
    return { scope: 'WHOLE_BUSINESS', as_of: '2026-11-06T00:00:00Z', active_items: 0, stock_positions: 0, below_safety_positions: 0, open_purchase_orders: 0, confirmed_sales_orders: 0, active_counts: 0, pending_outbox_events: 0 } as never
  })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><ReportsWorkspace workspaceId="tenant" /></QueryClientProvider>)
  fireEvent.click(screen.getByRole('tab', { name: 'Stock & value' }))
  await screen.findByRole('option', { name: 'A · Main' })
  fireEvent.change(screen.getByLabelText('Location'), { target: { value: 'loc-a' } })
  await waitFor(() => expect(vi.mocked(workspaceApi).mock.calls.some(call => call[1].includes('location_id=loc-a'))).toBe(true))
  fireEvent.click(screen.getByRole('tab', { name: 'Sales orders' }))
  fireEvent.change(screen.getByLabelText('From'), { target: { value: '2026-11-01' } })
  await waitFor(() => expect(vi.mocked(workspaceApi).mock.calls.some(call => call[1].includes('/reports/sales-summary?from_date=2026-11-01'))).toBe(true))
})
