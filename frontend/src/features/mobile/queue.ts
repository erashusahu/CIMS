import { workspaceApi } from '../catalog/api'
import { jsonBody } from '../../api/client'

export type MobileKind = 'RECEIPT_DRAFT' | 'PICK_SCAN' | 'SHIPMENT_PACK' | 'TRANSFER_RECEIVE' | 'COUNT_SUBMIT' | 'POS_DRAFT'
export type QueueState = 'pending' | 'conflict' | 'accepted' | 'blocked'
export type MobileOperation = {
  key: string
  scope: string
  operation_id: string
  kind: MobileKind
  document_id: string | null
  expected_version: string | null
  depends_on: string[]
  captured_at: string
  payload: Record<string, unknown>
  state: QueueState
  error: string | null
  receipt: MobileReceipt | null
}
export type MobileReceipt = {
  operation_id: string
  kind: MobileKind
  document_id: string | null
  captured_at: string
  accepted_at: string
  result: Record<string, unknown>
}
export type MobileReference = { key: string; scope: string; name: string; captured_at: string; data: unknown }

export function queueScope(tenantId: string, actorId: string) { return `${tenantId}:${actorId}` }

function request<T>(source: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    source.onsuccess = () => resolve(source.result)
    source.onerror = () => reject(source.error)
  })
}

function open(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const source = indexedDB.open('cims-mobile-drafts', 1)
    source.onupgradeneeded = () => {
      const db = source.result
      const operations = db.createObjectStore('operations', { keyPath: 'key' })
      operations.createIndex('scope', 'scope', { unique: false })
      const references = db.createObjectStore('references', { keyPath: 'key' })
      references.createIndex('scope', 'scope', { unique: false })
    }
    source.onsuccess = () => resolve(source.result)
    source.onerror = () => reject(source.error)
  })
}

async function store<T>(mode: IDBTransactionMode, action: (object: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const db = await open()
  try {
    const tx = db.transaction('operations', mode)
    const result = await request(action(tx.objectStore('operations')))
    await new Promise<void>((resolve, reject) => { tx.oncomplete = () => resolve(); tx.onerror = () => reject(tx.error); tx.onabort = () => reject(tx.error) })
    return result
  } finally { db.close() }
}

export async function listQueue(scope: string): Promise<MobileOperation[]> {
  const rows = await store('readonly', object => object.index('scope').getAll(scope)) as MobileOperation[]
  return rows.sort((a, b) => a.captured_at.localeCompare(b.captured_at) || a.operation_id.localeCompare(b.operation_id))
}

export async function saveOperation(value: MobileOperation): Promise<void> { await store('readwrite', object => object.put(value)) }
export async function discardOperation(value: MobileOperation): Promise<void> { await store('readwrite', object => object.delete(value.key)) }
export async function clearQueue(scope: string): Promise<void> {
  for (const value of await listQueue(scope)) await discardOperation(value)
  const db = await open()
  try {
    const tx = db.transaction('references', 'readwrite')
    const object = tx.objectStore('references')
    const rows = await request(object.index('scope').getAll(scope)) as MobileReference[]
    for (const value of rows) object.delete(value.key)
    await new Promise<void>((resolve, reject) => { tx.oncomplete = () => resolve(); tx.onerror = () => reject(tx.error); tx.onabort = () => reject(tx.error) })
  } finally { db.close() }
}
export async function pendingCount(scope: string): Promise<number> { return (await listQueue(scope)).filter(value => value.state !== 'accepted').length }

export async function cacheReference(scope: string, name: string, data: unknown): Promise<void> {
  const db = await open()
  try {
    const tx = db.transaction('references', 'readwrite')
    tx.objectStore('references').put({ key: `${scope}:${name}`, scope, name, captured_at: new Date().toISOString(), data })
    await new Promise<void>((resolve, reject) => { tx.oncomplete = () => resolve(); tx.onerror = () => reject(tx.error); tx.onabort = () => reject(tx.error) })
  } finally { db.close() }
}
export async function readReference<T>(scope: string, name: string): Promise<T | null> {
  const db = await open()
  try {
    const tx = db.transaction('references', 'readonly')
    const value = await request(tx.objectStore('references').get(`${scope}:${name}`)) as MobileReference | undefined
    return value ? value.data as T : null
  } finally { db.close() }
}

const forbidden = /password|token|authorization|cookie|credential|secret/i
function assertNoCredentials(value: unknown): void {
  if (!value || typeof value !== 'object') return
  for (const [key, child] of Object.entries(value)) {
    if (forbidden.test(key)) throw new Error('Credentials cannot be saved in mobile drafts')
    assertNoCredentials(child)
  }
}

export function newOperation(scope: string, kind: MobileKind, payload: Record<string, unknown>, documentId: string | null, expectedVersion: string | null = null, dependsOn: string[] = []): MobileOperation {
  assertNoCredentials(payload)
  const operationId = crypto.randomUUID()
  return { key: `${scope}:${operationId}`, scope, operation_id: operationId, kind, document_id: documentId,
    expected_version: expectedVersion, depends_on: dependsOn, captured_at: new Date().toISOString(),
    payload, state: 'pending', error: null, receipt: null }
}

function httpStatus(error: unknown): number | null { return error && typeof error === 'object' && 'status' in error && typeof error.status === 'number' ? error.status : null }
function message(error: unknown) { return error instanceof Error ? error.message : 'Synchronization failed' }

export function dependencyOrder(rows: MobileOperation[]): MobileOperation[] {
  const byId = new Map(rows.map(value => [value.operation_id, value]))
  const ordered: MobileOperation[] = []
  const visiting = new Set<string>()
  const visited = new Set<string>()
  const visit = (operation: MobileOperation) => {
    if (visiting.has(operation.operation_id)) throw new Error('Draft dependencies contain a cycle')
    if (visited.has(operation.operation_id)) return
    visiting.add(operation.operation_id)
    for (const id of operation.depends_on) {
      const earlier = byId.get(id)
      if (earlier) visit(earlier)
    }
    visiting.delete(operation.operation_id)
    visited.add(operation.operation_id)
    ordered.push(operation)
  }
  rows.forEach(visit)
  return ordered
}

export async function synchronizeQueue(scope: string, workspaceId: string, onChange?: (rows: MobileOperation[]) => void): Promise<MobileOperation[]> {
  const rows = await listQueue(scope)
  const byId = new Map(rows.map(value => [value.operation_id, value]))
  const emit = () => onChange?.([...rows])
  for (const operation of dependencyOrder(rows)) {
    if (operation.state === 'accepted' || operation.state === 'conflict') continue
    if (operation.depends_on.some(id => byId.get(id)?.state !== 'accepted')) {
      operation.state = 'blocked'; operation.error = 'An earlier action must synchronize first'
      await saveOperation(operation); emit(); continue
    }
    operation.state = 'pending'; operation.error = null
    try {
      let receipt: MobileReceipt
      try {
        receipt = await workspaceApi<MobileReceipt>(workspaceId, '/mobile/operations', { method: 'POST', body: jsonBody({
          operation_id: operation.operation_id, kind: operation.kind, document_id: operation.document_id,
          expected_version: operation.expected_version, depends_on: operation.depends_on,
          captured_at: operation.captured_at, payload: operation.payload,
        }) })
      } catch (error) {
        if (httpStatus(error) !== null) throw error
        receipt = await workspaceApi<MobileReceipt>(workspaceId, `/mobile/receipts/${operation.operation_id}`)
      }
      operation.state = 'accepted'; operation.receipt = receipt; operation.error = null
      await saveOperation(operation); emit()
    } catch (error) {
      const status = httpStatus(error)
      operation.state = status === 409 || status === 422 ? 'conflict' : 'pending'
      operation.error = message(error)
      await saveOperation(operation); emit()
      if (operation.state === 'pending') break
    }
  }
  return rows
}
