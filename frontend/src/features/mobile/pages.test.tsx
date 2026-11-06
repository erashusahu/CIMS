import 'fake-indexeddb/auto'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { cacheReference, clearQueue, listQueue, queueScope } from './queue'
import { MobileWorkspace } from './pages'

const scope = queueScope('mobile-test-tenant', 'mobile-test-user')
afterEach(async () => { cleanup(); await clearQueue(scope) })

it('keeps a complete offline count after reloading the mobile view', async () => {
  Object.defineProperty(navigator, 'onLine', { configurable: true, value: false })
  await cacheReference(scope, 'workspace', {
    purchaseOrders: [], salesOrders: [], transfers: [], customers: [], locations: [], items: [
      { item_id: 'item-one', sku: 'ONE' }, { item_id: 'item-two', sku: 'TWO' },
    ],
    counts: [{ audit_count_id: 'count-one', count_no: 'COUNT-1', status: 'IN_PROGRESS', snapshot_at: '2026-11-06T00:00:00Z', lines: [{ item_id: 'item-one' }, { item_id: 'item-two' }] }],
  })
  render(<MobileWorkspace workspaceId="mobile-test-tenant" actorId="mobile-test-user" canWrite />)
  fireEvent.click(screen.getByRole('tab', { name: 'Count' }))
  await screen.findByRole('option', { name: 'COUNT-1 · IN_PROGRESS' })
  fireEvent.change(screen.getByLabelText('Document'), { target: { value: 'count-one' } })
  fireEvent.change(screen.getByLabelText('Count ONE'), { target: { value: '4' } })
  fireEvent.change(screen.getByLabelText('Count TWO'), { target: { value: '0' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save on this device' }))
  await waitFor(async () => expect((await listQueue(scope)).length).toBe(1))
  expect((await listQueue(scope))[0].payload).toEqual({ lines: [
    { item_id: 'item-one', counted_quantity: '4' },
    { item_id: 'item-two', counted_quantity: '0' },
  ] })
  cleanup()
  render(<MobileWorkspace workspaceId="mobile-test-tenant" actorId="mobile-test-user" canWrite />)
  expect(await screen.findByText(/1 awaiting acceptance/)).toBeInTheDocument()
})
