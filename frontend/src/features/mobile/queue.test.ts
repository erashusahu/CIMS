import 'fake-indexeddb/auto'
import { describe, expect, it, vi } from 'vitest'
import { workspaceApi } from '../catalog/api'
import { dependencyOrder, listQueue, newOperation, queueScope, saveOperation, synchronizeQueue, type MobileOperation } from './queue'

vi.mock('../catalog/api', () => ({ workspaceApi: vi.fn() }))

function row(id: string, depends_on: string[] = []): MobileOperation {
  return { key: `tenant:user:${id}`, scope: 'tenant:user', operation_id: id, kind: 'POS_DRAFT',
    document_id: null, expected_version: null, depends_on, captured_at: '2026-11-06T00:00:00Z',
    payload: {}, state: 'pending', error: null, receipt: null }
}

describe('mobile queue boundaries', () => {
  it('separates tenant and actor scopes', () => {
    expect(queueScope('tenant-a', 'user-a')).not.toBe(queueScope('tenant-a', 'user-b'))
    expect(queueScope('tenant-a', 'user-a')).not.toBe(queueScope('tenant-b', 'user-a'))
  })

  it('orders predecessors before their dependents', () => {
    expect(dependencyOrder([row('third', ['second']), row('second', ['first']), row('first')]).map(value => value.operation_id))
      .toEqual(['first', 'second', 'third'])
  })

  it('rejects dependency cycles and nested credentials', () => {
    expect(() => dependencyOrder([row('one', ['two']), row('two', ['one'])])).toThrow(/cycle/)
    expect(() => newOperation('tenant:user', 'POS_DRAFT', { sale: { accessToken: 'private' } }, null)).toThrow(/Credentials/)
  })

  it('persists drafts by tenant and user after reopening the database', async () => {
    const one = { ...row('persist-one'), scope: 'tenant-a:user-a', key: 'tenant-a:user-a:persist-one' }
    const other = { ...row('persist-two'), scope: 'tenant-a:user-b', key: 'tenant-a:user-b:persist-two' }
    await saveOperation(one); await saveOperation(other)
    expect((await listQueue(one.scope)).map(value => value.operation_id)).toEqual(['persist-one'])
    expect((await listQueue(other.scope)).map(value => value.operation_id)).toEqual(['persist-two'])
  })

  it('recovers a committed result after the POST response is lost', async () => {
    const operation = { ...row('lost-response'), scope: 'tenant:user-lost', key: 'tenant:user-lost:lost-response' }
    await saveOperation(operation)
    const receipt = { operation_id: operation.operation_id, kind: operation.kind, document_id: null,
      captured_at: operation.captured_at, accepted_at: '2026-11-06T01:00:00Z', result: { status: 'DRAFT' } }
    vi.mocked(workspaceApi).mockRejectedValueOnce(new TypeError('Network response lost')).mockResolvedValueOnce(receipt)
    const result = await synchronizeQueue(operation.scope, 'tenant')
    expect(result[0].state).toBe('accepted')
    expect((await listQueue(operation.scope))[0].receipt).toEqual(receipt)
    expect(vi.mocked(workspaceApi).mock.calls.map(call => call[1])).toEqual(['/mobile/operations', '/mobile/receipts/lost-response'])
  })

  it('blocks a dependent action when its predecessor conflicts', async () => {
    const first = { ...row('first-failed'), scope: 'tenant:user-blocked', key: 'tenant:user-blocked:first-failed' }
    const next = { ...row('second-blocked', ['first-failed']), scope: first.scope, key: 'tenant:user-blocked:second-blocked' }
    await saveOperation(first); await saveOperation(next)
    vi.mocked(workspaceApi).mockReset()
    vi.mocked(workspaceApi).mockRejectedValueOnce(Object.assign(new Error('Stock no longer available'), { status: 409 }))
    const result = await synchronizeQueue(first.scope, 'tenant')
    expect(result.map(value => value.state)).toEqual(['conflict', 'blocked'])
    expect(vi.mocked(workspaceApi)).toHaveBeenCalledTimes(1)
  })
})
