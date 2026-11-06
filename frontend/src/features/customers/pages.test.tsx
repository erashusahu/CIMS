import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { workspaceApi } from '../catalog/api'
import { CustomersView } from './pages'

vi.mock('../catalog/api', async importOriginal => ({
  ...await importOriginal<typeof import('../catalog/api')>(),
  workspaceApi: vi.fn(),
}))

afterEach(cleanup)

it('saves customer credit terms without making a stock movement', async () => {
  vi.mocked(workspaceApi).mockImplementation(async (_workspace, path) => path.startsWith('/customers?')
    ? { items: [], total: 0, page: 1, page_size: 100 } as never : {} as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><CustomersView workspaceId="test-workspace" canWrite /></QueryClientProvider>)
  fireEvent.change(screen.getByLabelText('Customer code'), { target: { value: 'BUYER-01' } })
  fireEvent.change(screen.getByLabelText('Customer name'), { target: { value: 'Buyer One' } })
  fireEvent.change(screen.getByLabelText('Payment terms (days)'), { target: { value: '30' } })
  fireEvent.change(screen.getByLabelText('Credit limit (INR)'), { target: { value: '50000' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save customer' }))
  await waitFor(() => expect(vi.mocked(workspaceApi).mock.calls.some(call => call[1] === '/customers' && call[2]?.method === 'POST')).toBe(true))
  const request = vi.mocked(workspaceApi).mock.calls.find(call => call[1] === '/customers' && call[2]?.method === 'POST')!
  expect(JSON.parse(String(request[2]?.body))).toMatchObject({ code: 'BUYER-01', name: 'Buyer One', payment_terms_days: 30, credit_limit: '50000' })
  expect(vi.mocked(workspaceApi).mock.calls.some(call => call[1].startsWith('/inventory/'))).toBe(false)
})
