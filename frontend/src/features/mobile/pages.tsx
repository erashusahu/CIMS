import { useEffect, useRef, useState, type FormEvent } from 'react'
import type { components } from '../../api/openapi'
import { catalogApi, workspaceApi, type ItemSummary, type Page } from '../catalog/api'
import {
  cacheReference, discardOperation, listQueue, newOperation, queueScope,
  readReference, saveOperation, synchronizeQueue, type MobileKind, type MobileOperation,
} from './queue'

type PurchaseOrder = components['schemas']['PurchaseOrderOut']
type SalesOrder = components['schemas']['SalesOrderOut']
type Transfer = components['schemas']['TransferOut']
type Count = components['schemas']['CountOut']
type Customer = components['schemas']['CustomerOut']
type Location = components['schemas']['LocationOption']
type References = { purchaseOrders: PurchaseOrder[]; salesOrders: SalesOrder[]; transfers: Transfer[]; counts: Count[]; customers: Customer[]; locations: Location[]; items: ItemSummary[] }
type Mode = MobileKind
const labels: Record<Mode, string> = { RECEIPT_DRAFT: 'Receive', PICK_SCAN: 'Pick', SHIPMENT_PACK: 'Pack', TRANSFER_RECEIVE: 'Transfer receipt', COUNT_SUBMIT: 'Count', POS_DRAFT: 'POS draft' }
const empty: References = { purchaseOrders: [], salesOrders: [], transfers: [], counts: [], customers: [], locations: [], items: [] }
const today = () => { const date = new Date(); return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}` }
const errorText = (error: unknown) => error instanceof Error ? error.message : 'The action failed'

async function loadReferences(workspaceId: string): Promise<References> {
  const [purchaseOrders, salesOrders, transfers, counts, customers, locations, items] = await Promise.all([
    workspaceApi<Page<PurchaseOrder>>(workspaceId, '/procurement/purchase-orders?page_size=100'),
    workspaceApi<Page<SalesOrder>>(workspaceId, '/sales/orders?page_size=100'),
    workspaceApi<Transfer[]>(workspaceId, '/transfers'),
    workspaceApi<Count[]>(workspaceId, '/recovery/counts'),
    workspaceApi<Page<Customer>>(workspaceId, '/customers?page_size=100'),
    workspaceApi<Location[]>(workspaceId, '/inventory/locations'),
    catalogApi<Page<ItemSummary>>(workspaceId, '/items?page_size=100&status=ACTIVE'),
  ])
  return { purchaseOrders: purchaseOrders.items, salesOrders: salesOrders.items, transfers, counts, customers: customers.items, locations, items: items.items }
}

function CameraScan({ onCode }: { onCode: (code: string) => void }) {
  const video = useRef<HTMLVideoElement>(null)
  const [active, setActive] = useState(false)
  const [failure, setFailure] = useState('')
  useEffect(() => {
    if (!active) return
    let stream: MediaStream | null = null
    let timer: number | undefined
    let stopped = false
    void (async () => {
      try {
        const detectorType = (window as unknown as { BarcodeDetector?: new (options: { formats: string[] }) => { detect: (source: HTMLVideoElement) => Promise<{ rawValue: string }[]> } }).BarcodeDetector
        if (!detectorType) throw new Error('Camera barcode scanning is unavailable in this browser. Use the barcode field or a Bluetooth scanner.')
        stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' }, audio: false })
        if (!video.current || stopped) return
        video.current.srcObject = stream
        await video.current.play()
        const detector = new detectorType({ formats: ['code_128', 'code_39', 'ean_13', 'ean_8', 'upc_a', 'qr_code'] })
        timer = window.setInterval(() => {
          if (!video.current || video.current.readyState < 2) return
          void detector.detect(video.current).then(codes => { if (codes[0]?.rawValue) { onCode(codes[0].rawValue); setActive(false) } }).catch(() => undefined)
        }, 350)
      } catch (error) { setFailure(errorText(error)); setActive(false) }
    })()
    return () => { stopped = true; if (timer) window.clearInterval(timer); stream?.getTracks().forEach(track => track.stop()) }
  }, [active, onCode])
  return <div className="mobile-camera"><button type="button" className="button secondary" onClick={() => { setFailure(''); setActive(value => !value) }}>{active ? 'Stop camera' : 'Scan with camera'}</button>{active && <video ref={video} playsInline muted aria-label="Camera barcode preview" />}{failure && <p className="muted" role="status">{failure}</p>}</div>
}

export function MobileWorkspace({ workspaceId, actorId, canWrite }: { workspaceId: string; actorId: string; canWrite: boolean }) {
  const scope = queueScope(workspaceId, actorId)
  const [rows, setRows] = useState<MobileOperation[]>([])
  const [refs, setRefs] = useState<References>(empty)
  const [offline, setOffline] = useState(!navigator.onLine)
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [ready, setReady] = useState<boolean | null>(null)
  const [mode, setMode] = useState<Mode>('RECEIPT_DRAFT')
  const [documentId, setDocumentId] = useState('')
  const [lineId, setLineId] = useState('')
  const [quantity, setQuantity] = useState('1')
  const [damaged, setDamaged] = useState('0')
  const [short, setShort] = useState('0')
  const [barcode, setBarcode] = useState('')
  const [locationId, setLocationId] = useState('')
  const [customerId, setCustomerId] = useState('')
  const [cart, setCart] = useState<{ item_id: string; uom_id: string; quantity: string }[]>([])
  const [counted, setCounted] = useState<Record<string, string>>({})
  const [dependsOn, setDependsOn] = useState('')
  const [editing, setEditing] = useState<MobileOperation | null>(null)
  const [json, setJson] = useState('')
  const [editVersion, setEditVersion] = useState<string | null>(null)
  const refresh = async () => setRows(await listQueue(scope))
  useEffect(() => {
    let mounted = true
    void listQueue(scope).then(value => { if (mounted) setRows(value) }).catch(error => setMessage(errorText(error)))
    void readReference<References>(scope, 'workspace').then(value => { if (mounted && value) setRefs(value) }).catch(error => setMessage(errorText(error)))
    const online = () => setOffline(false)
    const disconnected = () => setOffline(true)
    window.addEventListener('online', online); window.addEventListener('offline', disconnected)
    return () => { mounted = false; window.removeEventListener('online', online); window.removeEventListener('offline', disconnected) }
  }, [scope])
  const reload = async () => {
    setBusy(true); setMessage('')
    try {
      const status = await workspaceApi<{ ready: boolean; required_sql: string }>(workspaceId, '/mobile/status')
      setReady(status.ready)
      if (!status.ready) { setMessage(`Apply scripts/sql/${status.required_sql} to enable synchronization.`); return }
      const value = await loadReferences(workspaceId)
      setRefs(value); await cacheReference(scope, 'workspace', value)
      setMessage('Current documents saved for offline use.')
    } catch (error) { setMessage(`Using saved documents. ${errorText(error)}`) }
    finally { setBusy(false) }
  }
  useEffect(() => { if (!offline) void reload() }, [workspaceId, scope])
  const purchaseOrder = refs.purchaseOrders.find(value => value.purchase_order_id === documentId)
  const salesOrder = refs.salesOrders.find(value => value.sales_order_id === documentId)
  const transfer = refs.transfers.find(value => value.transfer_order_id === documentId)
  const count = refs.counts.find(value => value.audit_count_id === documentId)
  const selectedItem = refs.items.find(value => value.item_id === lineId)
  const selectedLine = purchaseOrder?.lines.find(value => value.purchase_order_line_id === lineId)
    ?? salesOrder?.lines.find(value => value.sales_order_line_id === lineId)
  const reservation = salesOrder?.reservations.find(value => value.sales_order_line_id === lineId && value.state === 'ACTIVE')
  const eligibleDocuments = mode === 'RECEIPT_DRAFT' ? refs.purchaseOrders.filter(value => value.status === 'SENT' || value.status === 'PARTIALLY_RECEIVED')
    : mode === 'PICK_SCAN' || mode === 'SHIPMENT_PACK' ? refs.salesOrders.filter(value => value.status === 'CONFIRMED' || value.status === 'PARTIALLY_FULFILLED')
      : mode === 'TRANSFER_RECEIVE' ? refs.transfers.filter(value => value.status === 'IN_TRANSIT' || value.status === 'PARTIALLY_RECEIVED')
        : mode === 'COUNT_SUBMIT' ? refs.counts.filter(value => value.status === 'IN_PROGRESS') : []
  const documentLabel = (value: PurchaseOrder | SalesOrder | Transfer | Count) => 'po_no' in value ? value.po_no : 'order_no' in value ? value.order_no : 'transfer_no' in value ? value.transfer_no : value.count_no
  const changeMode = (value: Mode) => { setMode(value); setDocumentId(''); setLineId(''); setBarcode(''); setCart([]); setCounted({}); setMessage('') }
  const payload = (): Record<string, unknown> => {
    if (mode === 'RECEIPT_DRAFT') return { purchase_order_id: documentId, lines: [{ purchase_order_line_id: lineId, received_quantity: quantity, damaged_quantity: damaged, short_quantity: short }] }
    if (mode === 'PICK_SCAN') return { reservation_id: reservation?.reservation_id, scanned_code: barcode.trim(), quantity }
    if (mode === 'SHIPMENT_PACK') {
      const line = salesOrder?.lines.find(value => value.sales_order_line_id === lineId)
      const stockQuantity = line ? String(Number((Number(quantity) * Number(line.quantity_in_stock_uom) / Number(line.quantity)).toFixed(4))) : quantity
      return { from_location_id: salesOrder?.location_id, lines: [{ sales_order_line_id: lineId, quantity }], packages: [{ contents: [{ reservation_id: reservation?.reservation_id, quantity: stockQuantity }] }] }
    }
    if (mode === 'TRANSFER_RECEIVE') return { line_id: lineId, quantity }
    if (mode === 'COUNT_SUBMIT') return { lines: (count?.lines ?? []).map(value => ({ item_id: value.item_id, counted_quantity: counted[value.item_id] })) }
    return { customer_id: customerId, sales_source: 'POS', location_id: locationId, order_date: today(), lines: cart.map(value => ({ ...value, location_id: null, discount_pct: '0' })) }
  }
  const version = () => mode === 'RECEIPT_DRAFT' ? purchaseOrder?.updated_at ?? null
    : mode === 'PICK_SCAN' || mode === 'SHIPMENT_PACK' ? String(salesOrder?.revision ?? '')
      : mode === 'TRANSFER_RECEIVE' ? String(transfer?.operations.length ?? '')
        : mode === 'COUNT_SUBMIT' ? count?.snapshot_at ?? null : null
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setMessage('')
    try {
      if (mode === 'COUNT_SUBMIT' && (!count?.lines.length || count.lines.some(value => counted[value.item_id] === undefined || counted[value.item_id] === ''))) throw new Error('Count every item in the selected scope.')
      if (mode === 'RECEIPT_DRAFT' && Number(quantity) + Number(damaged) + Number(short) <= 0) throw new Error('Record a received, damaged, or short quantity.')
      const value = newOperation(scope, mode, payload(), mode === 'POS_DRAFT' ? null : documentId, version(), dependsOn ? [dependsOn] : [])
      await saveOperation(value); await refresh(); setMessage('Saved on this device. Synchronize when connected.')
      setDependsOn(''); setBarcode(''); setCart([])
    } catch (error) { setMessage(errorText(error)) }
  }
  const sync = async () => {
    setBusy(true); setMessage('')
    try {
      if (!ready) { const status = await workspaceApi<{ ready: boolean }>(workspaceId, '/mobile/status'); setReady(status.ready); if (!status.ready) throw new Error('Apply SQL 010 before synchronizing.') }
      const result = await synchronizeQueue(scope, workspaceId, setRows)
      await reload()
      setMessage(`${result.filter(value => value.state === 'accepted').length} accepted; ${result.filter(value => value.state === 'conflict').length} need review.`)
    } catch (error) { setMessage(errorText(error)) }
    finally { setBusy(false) }
  }
  const currentVersion = (value: MobileOperation) => value.kind === 'RECEIPT_DRAFT' ? refs.purchaseOrders.find(order => order.purchase_order_id === value.document_id)?.updated_at ?? null
    : value.kind === 'PICK_SCAN' || value.kind === 'SHIPMENT_PACK' ? String(refs.salesOrders.find(order => order.sales_order_id === value.document_id)?.revision ?? '')
      : value.kind === 'TRANSFER_RECEIVE' ? String(refs.transfers.find(order => order.transfer_order_id === value.document_id)?.operations.length ?? '')
        : value.kind === 'COUNT_SUBMIT' ? refs.counts.find(order => order.audit_count_id === value.document_id)?.snapshot_at ?? null : null
  const edit = (value: MobileOperation) => {
    if (rows.some(other => other.depends_on.includes(value.operation_id))) { setMessage('Discard dependent actions before editing this action.'); return }
    setEditing(value); setJson(JSON.stringify(value.payload, null, 2)); setEditVersion(value.expected_version)
  }
  const saveEdit = async () => {
    if (!editing) return
    try {
      const payload: unknown = JSON.parse(json)
      if (!payload || Array.isArray(payload) || typeof payload !== 'object') throw new Error('Action fields must be a JSON object.')
      const replacement = newOperation(scope, editing.kind, payload as Record<string, unknown>, editing.document_id, editVersion, editing.depends_on)
      await saveOperation(replacement); await discardOperation(editing); await refresh(); setEditing(null); setMessage('Edited action saved with a new operation ID. Review its dependencies before sync.')
    } catch (error) { setMessage(errorText(error)) }
  }
  const retry = async (value: MobileOperation) => { await saveOperation({ ...value, state: 'pending', error: null }); await refresh() }
  const discard = async (value: MobileOperation) => {
    if (rows.some(other => other.depends_on.includes(value.operation_id) && other.state !== 'accepted')) { setMessage('Discard dependent actions first.'); return }
    if (!window.confirm('Discard this local action?')) return
    await discardOperation(value); await refresh()
  }
  const addCode = (code: string) => {
    const item = refs.items.find(value => value.barcode?.toLowerCase() === code.trim().toLowerCase() || value.sku.toLowerCase() === code.trim().toLowerCase())
    if (!item) { setMessage('No matching item in the saved catalog. Refresh documents when connected.'); return }
    setCart(current => current.some(value => value.item_id === item.item_id) ? current.map(value => value.item_id === item.item_id ? { ...value, quantity: String(Number(value.quantity) + 1) } : value) : [...current, { item_id: item.item_id, uom_id: item.stock_uom_id, quantity: '1' }])
    setBarcode('')
  }
  return <section className="mobile-workspace">
    <div className="catalog-toolbar"><p className="muted">Capture work on this device, then synchronize it. Server rules are checked when each action arrives.</p><div className="actions"><span className={`status ${offline ? '' : 'active'}`}>{offline ? 'Offline' : 'Online'}</span><button className="button secondary" disabled={busy || offline} onClick={() => void reload()}>Refresh documents</button><button className="button primary" disabled={busy || offline || !canWrite} onClick={() => void sync()}>{busy ? 'Working…' : 'Synchronize queue'}</button></div></div>
    {message && <p role="status" className={message.includes('failed') ? 'error' : 'muted'}>{message}</p>}
    {ready === false && <p className="error" role="alert">SQL 010 is required before these actions can synchronize.</p>}
    <div className="mobile-layout"><section className="card form-card"><h2>Capture action</h2><div className="catalog-tabs" role="tablist" aria-label="Mobile actions">{(Object.keys(labels) as Mode[]).map(value => <button type="button" role="tab" aria-selected={mode === value} key={value} className={`button small ${mode === value ? 'primary' : 'secondary'}`} onClick={() => changeMode(value)}>{labels[value]}</button>)}</div>
      <form className="form-grid" onSubmit={event => void submit(event)}>
        {mode !== 'POS_DRAFT' && <label className="wide">Document<select required value={documentId} onChange={event => { setDocumentId(event.target.value); setLineId('') }}><option value="">Choose document</option>{eligibleDocuments.map(value => <option key={documentLabel(value)} value={'purchase_order_id' in value ? value.purchase_order_id : 'sales_order_id' in value ? value.sales_order_id : 'transfer_order_id' in value ? value.transfer_order_id : value.audit_count_id}>{documentLabel(value)} · {value.status}</option>)}</select></label>}
        {(mode === 'RECEIPT_DRAFT' || mode === 'PICK_SCAN' || mode === 'SHIPMENT_PACK' || mode === 'TRANSFER_RECEIVE') && <label className="wide">Line<select required value={lineId} onChange={event => setLineId(event.target.value)}><option value="">Choose line</option>{(mode === 'RECEIPT_DRAFT' ? purchaseOrder?.lines.map(value => ({ id: value.purchase_order_line_id, label: refs.items.find(item => item.item_id === value.item_id)?.sku ?? value.item_id })) : mode === 'PICK_SCAN' || mode === 'SHIPMENT_PACK' ? salesOrder?.lines.map(value => ({ id: value.sales_order_line_id, label: refs.items.find(item => item.item_id === value.item_id)?.sku ?? value.item_id })) : transfer?.lines.map(value => ({ id: value.transfer_order_line_id, label: refs.items.find(item => item.item_id === value.item_id)?.sku ?? value.item_id })))?.map(value => <option key={value.id} value={value.id}>{value.label}</option>)}</select></label>}
        {mode === 'COUNT_SUBMIT' && count?.lines.map(value => <label key={value.item_id}>Count {refs.items.find(item => item.item_id === value.item_id)?.sku ?? value.item_id}<input required type="number" min="0" step="any" value={counted[value.item_id] ?? ''} onChange={event => setCounted(current => ({ ...current, [value.item_id]: event.target.value }))} /></label>)}
        {mode === 'POS_DRAFT' && <><label>Location<select required value={locationId} onChange={event => setLocationId(event.target.value)}><option value="">Choose location</option>{refs.locations.filter(value => value.is_active).map(value => <option key={value.location_id} value={value.location_id}>{value.code} · {value.name}</option>)}</select></label><label>Customer<select required value={customerId} onChange={event => setCustomerId(event.target.value)}><option value="">Choose customer</option>{refs.customers.filter(value => value.is_active).map(value => <option key={value.customer_id} value={value.customer_id}>{value.code} · {value.name}</option>)}</select></label><label className="wide">Item<select value={lineId} onChange={event => setLineId(event.target.value)}><option value="">Choose item</option>{refs.items.map(value => <option key={value.item_id} value={value.item_id}>{value.sku} · {value.name}</option>)}</select></label><button type="button" className="button secondary" disabled={!selectedItem} onClick={() => selectedItem && setCart(current => current.some(value => value.item_id === selectedItem.item_id) ? current.map(value => value.item_id === selectedItem.item_id ? { ...value, quantity: String(Number(value.quantity) + 1) } : value) : [...current, { item_id: selectedItem.item_id, uom_id: selectedItem.stock_uom_id, quantity: '1' }])}>Add item</button><div className="wide">{cart.map(value => <div key={value.item_id} className="mobile-cart-line"><span>{refs.items.find(item => item.item_id === value.item_id)?.sku ?? value.item_id}</span><input aria-label="Cart quantity" type="number" min="0.0001" step="any" value={value.quantity} onChange={event => setCart(current => current.map(line => line.item_id === value.item_id ? { ...line, quantity: event.target.value } : line))} /><button type="button" className="button small secondary" onClick={() => setCart(current => current.filter(line => line.item_id !== value.item_id))}>Remove</button></div>)}</div></>}
        {mode === 'PICK_SCAN' && <><label className="wide">Barcode or SKU<input required value={barcode} onChange={event => setBarcode(event.target.value)} placeholder="Type or use a Bluetooth scanner" /></label><CameraScan onCode={setBarcode} /></>}
        {mode === 'POS_DRAFT' && <><label className="wide">Barcode or SKU<input value={barcode} onChange={event => setBarcode(event.target.value)} placeholder="Type or use a Bluetooth scanner" onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); addCode(barcode) } }} /></label><CameraScan onCode={addCode} /></>}
        {mode !== 'POS_DRAFT' && mode !== 'COUNT_SUBMIT' && <label>{mode === 'RECEIPT_DRAFT' ? 'Received quantity' : 'Quantity'}<input required type="number" min={mode === 'RECEIPT_DRAFT' ? '0' : '0.0001'} step="any" value={quantity} onChange={event => setQuantity(event.target.value)} /></label>}
        {mode === 'RECEIPT_DRAFT' && <><label>Damaged<input required type="number" min="0" step="any" value={damaged} onChange={event => setDamaged(event.target.value)} /></label><label>Short<input required type="number" min="0" step="any" value={short} onChange={event => setShort(event.target.value)} /></label></>}
        {mode === 'SHIPMENT_PACK' && <p className="wide muted">Creates one shipment draft with one package. Review it in Shipping before posting.</p>}
        {mode === 'PICK_SCAN' && selectedLine && <p className="wide muted">Reservation: {reservation ? 'Available' : 'No active reservation'}</p>}
        <label className="wide">After action (optional)<select value={dependsOn} onChange={event => setDependsOn(event.target.value)}><option value="">No dependency</option>{rows.filter(value => value.state !== 'conflict' && value.state !== 'blocked').map(value => <option key={value.operation_id} value={value.operation_id}>{labels[value.kind]} · {value.operation_id.slice(0, 8)}</option>)}</select></label>
        <button className="button primary wide" disabled={!canWrite || (mode === 'POS_DRAFT' && cart.length === 0)}>Save on this device</button>
      </form><p className="muted">Device time is recorded as capture metadata. The server decides the accepted time. Offline POS remains a draft.</p></section>
      <section className="card table-card"><h2>Device queue</h2><p className="muted">{rows.filter(value => value.state !== 'accepted').length} awaiting acceptance</p>{rows.length === 0 ? <p className="muted">No actions saved on this device.</p> : <div className="mobile-queue">{rows.map(value => <article className="mobile-queue-item" key={value.operation_id}><div><strong>{labels[value.kind]}</strong><span className={`status ${value.state === 'accepted' ? 'active' : ''}`}>{value.state}</span></div><small>{new Date(value.captured_at).toLocaleString()} · {value.operation_id.slice(0, 8)}</small>{value.error && <p className="error" role="alert">{value.error}</p>}{value.receipt && <p className="muted">Accepted {new Date(value.receipt.accepted_at).toLocaleString()}</p>}<div className="actions">{value.state !== 'accepted' && <><button type="button" className="button small secondary" onClick={() => edit(value)}>Edit</button><button type="button" className="button small secondary" onClick={() => void retry(value)}>Retry</button></>}<button type="button" className="button small secondary" onClick={() => void discard(value)}>Discard</button></div></article>)}</div>}</section></div>
    {editing && <section className="card form-card"><h2>Review {labels[editing.kind]}</h2><p className="muted">Editing creates a new operation ID. Check quantities and document state before synchronizing.</p>{editing.kind !== 'POS_DRAFT' && <label>Document version<input value={editVersion ?? ''} onChange={event => setEditVersion(event.target.value)} /><button type="button" className="button small secondary" disabled={!currentVersion(editing)} onClick={() => setEditVersion(currentVersion(editing))}>Use latest loaded version</button></label>}<label className="wide">Action fields<textarea rows={12} value={json} onChange={event => setJson(event.target.value)} /></label><div className="actions"><button className="button primary" onClick={() => void saveEdit()}>Save changes</button><button className="button secondary" onClick={() => setEditing(null)}>Cancel</button></div></section>}
  </section>
}
