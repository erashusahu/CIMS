import { useEffect, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { components } from '../../api/openapi'
import { getAccessToken, jsonBody } from '../../api/client'
import { catalogApi, formatMoney, localCatalogMode, workspaceApi, type CatalogOptions, type ItemSummary, type Page } from '../catalog/api'

type Rule = components['schemas']['ReorderRuleOut']
type Suggestion = components['schemas']['PurchaseSuggestionGroup']
type Order = components['schemas']['PurchaseOrderOut']
type Receipt = components['schemas']['ReceiptOut']
type Supplier = components['schemas']['SupplierOut']
type Location = components['schemas']['LocationOption']
type OrderLine = { item_id: string; uom_id: string; quantity: string; unit_price: string }
export type SuggestedOrder = { itemId: string; vendorId: string; locationId: string; quantity: string }

function Failure({ error }: { error: unknown }) { return <p className="error" role="alert">{error instanceof Error ? error.message : 'The request failed'}</p> }
function number(value: string | number) { return Number(value).toLocaleString('en-IN', { maximumFractionDigits: 4 }) }

export function ReorderWorkspace({ workspaceId, canWrite, onOrder }: { workspaceId: string; canWrite: boolean; onOrder: (value: SuggestedOrder) => void }) {
  const client = useQueryClient()
  const [itemId, setItemId] = useState('')
  const [locationId, setLocationId] = useState('')
  const [vendorId, setVendorId] = useState('')
  const [method, setMethod] = useState<'REORDER_POINT' | 'MIN_MAX' | 'PAR_LEVEL'>('REORDER_POINT')
  const [point, setPoint] = useState('5')
  const [qty, setQty] = useState('10')
  const [target, setTarget] = useState('20')
  const items = useQuery({ queryKey: ['catalog', workspaceId, 'purchase-items'], queryFn: ({ signal }) => catalogApi<Page<ItemSummary>>(workspaceId, '/items?page_size=100&status=ACTIVE', { signal }) })
  const locations = useQuery({ queryKey: ['catalog', workspaceId, 'stock-locations'], queryFn: ({ signal }) => workspaceApi<Location[]>(workspaceId, '/inventory/locations', { signal }) })
  const suppliers = useQuery({ queryKey: ['catalog', workspaceId, 'purchase-suppliers'], queryFn: ({ signal }) => workspaceApi<Page<Supplier>>(workspaceId, '/procurement/suppliers?page_size=100', { signal }) })
  const rules = useQuery({ queryKey: ['catalog', workspaceId, 'reorder-rules'], queryFn: ({ signal }) => workspaceApi<Page<Rule>>(workspaceId, '/procurement/reorder-rules?page_size=100', { signal }) })
  const suggestions = useQuery({ queryKey: ['catalog', workspaceId, 'suggestions'], queryFn: ({ signal }) => workspaceApi<Suggestion[]>(workspaceId, '/procurement/suggestions', { signal }) })
  const save = useMutation({ mutationFn: () => workspaceApi<Rule>(workspaceId, '/procurement/reorder-rules', { method: 'POST', body: jsonBody({ item_id: itemId, location_id: locationId, preferred_vendor_id: vendorId || null, method, reorder_point: point, reorder_qty: qty, max_level: method === 'MIN_MAX' ? target : null, par_level: method === 'PAR_LEVEL' ? target : null }) }), onSuccess: async () => { await client.invalidateQueries({ queryKey: ['catalog', workspaceId, 'reorder-rules'] }); await client.invalidateQueries({ queryKey: ['catalog', workspaceId, 'suggestions'] }) } })
  return <>
    <p className="muted">Suggestions use current physical stock, commitments, safety stock and outstanding purchase orders.</p>
    {canWrite && <section className="card form-card"><h3>Add reorder rule</h3>{save.isError && <Failure error={save.error} />}<form className="form-grid" onSubmit={(event: FormEvent) => { event.preventDefault(); save.mutate() }}>
      <label>Item<select required value={itemId} onChange={event => setItemId(event.target.value)}><option value="">Choose an item</option>{items.data?.items.filter(item => item.item_kind !== 'COMPOSITE').map(item => <option key={item.item_id} value={item.item_id}>{item.sku} · {item.name}</option>)}</select></label>
      <label>Location<select required value={locationId} onChange={event => setLocationId(event.target.value)}><option value="">Choose a location</option>{locations.data?.filter(location => location.is_active).map(location => <option key={location.location_id} value={location.location_id}>{location.code}</option>)}</select></label>
      <label>Method<select value={method} onChange={event => setMethod(event.target.value as typeof method)}><option value="REORDER_POINT">Reorder point</option><option value="MIN_MAX">Min / max</option><option value="PAR_LEVEL">Par level</option></select></label>
      <label>Preferred supplier<select value={vendorId} onChange={event => setVendorId(event.target.value)}><option value="">Use item preference</option>{suppliers.data?.items.filter(value => value.is_active).map(value => <option key={value.vendor_id} value={value.vendor_id}>{value.name}</option>)}</select></label>
      {method !== 'PAR_LEVEL' && <label>Reorder point<input type="number" min="0" step="0.0001" value={point} onChange={event => setPoint(event.target.value)} /></label>}
      {method === 'REORDER_POINT' ? <label>Reorder quantity<input type="number" min="0.0001" step="0.0001" value={qty} onChange={event => setQty(event.target.value)} /></label> : <label>{method === 'MIN_MAX' ? 'Maximum stock' : 'Par stock'}<input type="number" min="0.0001" step="0.0001" value={target} onChange={event => setTarget(event.target.value)} /></label>}
      <div className="actions wide"><button className="button primary" disabled={save.isPending}>Save rule</button></div>
    </form></section>}
    {rules.isError && <Failure error={rules.error} />}{suggestions.isError && <Failure error={suggestions.error} />}
    <section className="card table-card"><h3>Current rules</h3>{rules.isLoading ? <p role="status">Loading rules…</p> : rules.data?.items.length ? <div className="table-wrap"><table><thead><tr><th>Item</th><th>Location</th><th>Method</th><th>Supplier</th></tr></thead><tbody>{rules.data.items.map(rule => <tr key={rule.reorder_rule_id}><td>{rule.item_sku}</td><td>{rule.location_code}</td><td>{rule.method.replaceAll('_', ' ')}</td><td>{rule.vendor_code ?? 'Item preference'}</td></tr>)}</tbody></table></div> : <p className="muted">No reorder rules yet.</p>}</section>
    <section className="card table-card"><h3>Purchase suggestions</h3>{suggestions.isLoading ? <p role="status">Calculating suggestions…</p> : suggestions.data?.length ? suggestions.data.map(group => <div key={group.vendor_id ?? 'unassigned'}><h4>{group.vendor_name}</h4><div className="table-wrap"><table><thead><tr><th>Item</th><th>Location</th><th>Projected</th><th>On order</th><th>Suggest</th><th>Action</th></tr></thead><tbody>{group.items.map(row => <tr key={`${row.item_id}:${row.location_id}`}><td>{row.item_sku}</td><td>{row.location_code}</td><td>{number(row.projected_available)}</td><td>{number(row.outstanding_order_qty)}</td><td><strong>{number(row.suggested_qty)}</strong></td><td>{canWrite && row.vendor_id && <button className="button small secondary" onClick={() => onOrder({ itemId: row.item_id, vendorId: row.vendor_id!, locationId: row.location_id, quantity: row.suggested_qty })}>Draft order</button>}</td></tr>)}</tbody></table></div></div>) : <p className="muted">No items currently need purchasing.</p>}</section>
  </>
}

async function downloadOrderPdf(workspaceId: string, orderId: string, orderNo: string) {
  const base = import.meta.env.VITE_API_URL ?? 'http://127.0.0.1:8000/api/v1'
  const headers = new Headers()
  if (localCatalogMode) headers.set('X-CIMS-Tenant-ID', workspaceId)
  else { const token = getAccessToken(); if (token) headers.set('Authorization', `Bearer ${token}`) }
  const response = await fetch(`${base}/procurement/purchase-orders/${orderId}/pdf`, { headers, credentials: 'include' })
  if (!response.ok) throw new Error('The purchase order PDF could not be downloaded')
  const url = URL.createObjectURL(await response.blob())
  const link = document.createElement('a'); link.href = url; link.download = `${orderNo.replaceAll('/', '-')}.pdf`; link.click()
  URL.revokeObjectURL(url)
}

export function PurchaseWorkspace({ workspaceId, canWrite, prefill }: { workspaceId: string; canWrite: boolean; prefill?: SuggestedOrder | null }) {
  const client = useQueryClient()
  const [vendorId, setVendorId] = useState(prefill?.vendorId ?? '')
  const [locationId, setLocationId] = useState(prefill?.locationId ?? '')
  const [lines, setLines] = useState<OrderLine[]>([{ item_id: prefill?.itemId ?? '', uom_id: '', quantity: prefill?.quantity ?? '1', unit_price: '0' }])
  const [pdfError, setPdfError] = useState<Error | null>(null)
  const [amendOrderId, setAmendOrderId] = useState('')
  const [amendLineId, setAmendLineId] = useState('')
  const [amendQuantity, setAmendQuantity] = useState('')
  const [amendReason, setAmendReason] = useState('')
  const options = useQuery({ queryKey: ['catalog', workspaceId, 'options'], queryFn: ({ signal }) => catalogApi<CatalogOptions>(workspaceId, '/options', { signal }) })
  const items = useQuery({ queryKey: ['catalog', workspaceId, 'purchase-items'], queryFn: ({ signal }) => catalogApi<Page<ItemSummary>>(workspaceId, '/items?page_size=100&status=ACTIVE', { signal }) })
  useEffect(() => { if (prefill && items.data) { const item = items.data.items.find(value => value.item_id === prefill.itemId); if (item) setLines([{ item_id: item.item_id, uom_id: item.stock_uom_id, quantity: prefill.quantity, unit_price: '0' }]) } }, [prefill, items.data])
  const locations = useQuery({ queryKey: ['catalog', workspaceId, 'stock-locations'], queryFn: ({ signal }) => workspaceApi<Location[]>(workspaceId, '/inventory/locations', { signal }) })
  const suppliers = useQuery({ queryKey: ['catalog', workspaceId, 'purchase-suppliers'], queryFn: ({ signal }) => workspaceApi<Page<Supplier>>(workspaceId, '/procurement/suppliers?page_size=100', { signal }) })
  const orders = useQuery({ queryKey: ['catalog', workspaceId, 'purchase-orders'], queryFn: ({ signal }) => workspaceApi<Page<Order>>(workspaceId, '/procurement/purchase-orders?page_size=100', { signal }) })
  const changeLine = (index: number, change: Partial<OrderLine>) => setLines(current => current.map((line, position) => position === index ? { ...line, ...change } : line))
  const refresh = async () => { await client.invalidateQueries({ queryKey: ['catalog', workspaceId, 'purchase-orders'] }); await client.invalidateQueries({ queryKey: ['catalog', workspaceId, 'suggestions'] }) }
  const create = useMutation({ mutationFn: () => workspaceApi<Order>(workspaceId, '/procurement/purchase-orders', { method: 'POST', body: jsonBody({ vendor_id: vendorId, location_id: locationId, order_date: new Date().toISOString().slice(0, 10), lines }) }), onSuccess: async () => { setLines([{ item_id: '', uom_id: '', quantity: '1', unit_price: '0' }]); await refresh() } })
  const action = useMutation({ mutationFn: ({ order, name }: { order: Order; name: string }) => workspaceApi<Order>(workspaceId, `/procurement/purchase-orders/${order.purchase_order_id}/actions/${name}`, { method: 'POST', body: jsonBody({ expected_updated_at: order.updated_at }) }), onSuccess: refresh })
  const amendOrder = orders.data?.items.find(value => value.purchase_order_id === amendOrderId)
  const amend = useMutation({ mutationFn: () => workspaceApi<Order>(workspaceId, `/procurement/purchase-orders/${amendOrderId}/amendments`, { method: 'POST', body: jsonBody({ expected_updated_at: amendOrder?.updated_at, line_id: amendLineId, new_quantity: amendQuantity, reason: amendReason }) }), onSuccess: async () => { setAmendOrderId(''); setAmendLineId(''); setAmendQuantity(''); setAmendReason(''); await refresh() } })
  return <>
    <p className="muted">Orders record the supplier commitment. They do not increase physical or accounting stock. Approval is required from INR 50,000.</p>
    {canWrite && <section className="card form-card"><h3>New purchase order</h3>{create.isError && <Failure error={create.error} />}<form className="form-grid" onSubmit={(event: FormEvent) => { event.preventDefault(); create.mutate() }}>
      <label>Supplier<select required value={vendorId} onChange={event => setVendorId(event.target.value)}><option value="">Choose a supplier</option>{suppliers.data?.items.filter(value => value.is_active).map(value => <option key={value.vendor_id} value={value.vendor_id}>{value.name}</option>)}</select></label>
      <label>Receiving location<select required value={locationId} onChange={event => setLocationId(event.target.value)}><option value="">Choose a location</option>{locations.data?.filter(value => value.is_active).map(value => <option key={value.location_id} value={value.location_id}>{value.code}</option>)}</select></label>
      {lines.map((line, index) => <fieldset className="wide catalog-subform" key={index}><legend>Line {index + 1}</legend><div className="form-grid"><label>Item<select required value={line.item_id} onChange={event => { const item = items.data?.items.find(value => value.item_id === event.target.value); changeLine(index, { item_id: event.target.value, uom_id: item?.stock_uom_id ?? '' }) }}><option value="">Choose item</option>{items.data?.items.filter(value => value.item_kind !== 'COMPOSITE').map(value => <option key={value.item_id} value={value.item_id}>{value.sku} · {value.name}</option>)}</select></label><label>Unit<select required value={line.uom_id} onChange={event => changeLine(index, { uom_id: event.target.value })}><option value="">Choose unit</option>{options.data?.units.map(value => <option key={value.uom_id} value={value.uom_id}>{value.code}</option>)}</select></label><label>Quantity<input required type="number" min="0.0001" step="0.0001" value={line.quantity} onChange={event => changeLine(index, { quantity: event.target.value })} /></label><label>Price per selected unit (INR)<input required type="number" min="0" step="0.000001" value={line.unit_price} onChange={event => changeLine(index, { unit_price: event.target.value })} /></label></div>{lines.length > 1 && <button type="button" className="button small secondary" onClick={() => setLines(current => current.filter((_, position) => position !== index))}>Remove line</button>}</fieldset>)}
      <div className="actions wide"><button type="button" className="button secondary" onClick={() => setLines(current => [...current, { item_id: '', uom_id: '', quantity: '1', unit_price: '0' }])}>Add line</button><button className="button primary" disabled={create.isPending}>Save draft</button></div>
    </form></section>}
    {orders.isError && <Failure error={orders.error} />}{action.isError && <Failure error={action.error} />}{pdfError && <Failure error={pdfError} />}
    <section className="card table-card"><h3>Purchase orders</h3>{orders.isLoading ? <p role="status">Loading purchase orders…</p> : orders.data?.items.length ? <div className="table-wrap"><table><thead><tr><th>Order</th><th>Status</th><th>Value</th><th>Outstanding</th><th>Action</th></tr></thead><tbody>{orders.data.items.map(order => <tr key={order.purchase_order_id}><td><strong>{order.po_no}</strong><small>{order.lines.length} line(s)</small></td><td>{order.status.replaceAll('_', ' ')}</td><td>{formatMoney(order.grand_total)}</td><td>{number(order.lines.reduce((total, line) => total + Number(line.quantity) - Number(line.received_quantity) - Number(line.cancelled_quantity), 0))}</td><td className="actions">{canWrite && (order.status === 'DRAFT' || order.status === 'PENDING_APPROVAL' || order.status === 'APPROVED') && <button className="button small primary" disabled={action.isPending} onClick={() => action.mutate({ order, name: order.status === 'DRAFT' ? 'submit' : order.status === 'PENDING_APPROVAL' ? 'approve' : 'send' })}>{order.status === 'DRAFT' ? 'Submit' : order.status === 'PENDING_APPROVAL' ? 'Approve' : 'Mark sent'}</button>}{!['DRAFT', 'PENDING_APPROVAL', 'CANCELLED'].includes(order.status) && <button className="button small secondary" onClick={() => void downloadOrderPdf(workspaceId, order.purchase_order_id, order.po_no).catch(error => setPdfError(error))}>PDF</button>}</td></tr>)}</tbody></table></div> : <p className="muted">No purchase orders yet.</p>}</section>
    {canWrite && orders.data?.items.some(value => ['SENT', 'PARTIALLY_RECEIVED'].includes(value.status)) && <section className="card form-card"><h3>Request an order increase</h3><p className="muted">An increased quantity must be approved before any excess delivery can be received.</p>{amend.isError && <Failure error={amend.error} />}<form className="form-grid" onSubmit={(event: FormEvent) => { event.preventDefault(); amend.mutate() }}><label>Sent order<select required value={amendOrderId} onChange={event => { setAmendOrderId(event.target.value); setAmendLineId('') }}><option value="">Choose an order</option>{orders.data.items.filter(value => ['SENT', 'PARTIALLY_RECEIVED'].includes(value.status)).map(value => <option key={value.purchase_order_id} value={value.purchase_order_id}>{value.po_no}</option>)}</select></label><label>Order line<select required value={amendLineId} onChange={event => { setAmendLineId(event.target.value); setAmendQuantity('') }}><option value="">Choose a line</option>{amendOrder?.lines.map(value => <option key={value.purchase_order_line_id} value={value.purchase_order_line_id}>Line {value.line_no} · ordered {number(value.quantity)}</option>)}</select></label><label>New total quantity<input required type="number" min="0.0001" step="0.0001" value={amendQuantity} onChange={event => setAmendQuantity(event.target.value)} /></label><label>Reason<input required minLength={3} maxLength={1000} value={amendReason} onChange={event => setAmendReason(event.target.value)} /></label><div className="actions wide"><button className="button primary" disabled={amend.isPending}>Submit for approval</button></div></form></section>}
  </>
}

export function ReceivingWorkspace({ workspaceId, canWrite }: { workspaceId: string; canWrite: boolean }) {
  const client = useQueryClient()
  const [orderId, setOrderId] = useState('')
  const [quantities, setQuantities] = useState<Record<string, { received: string; damaged: string; short: string }>>({})
  const orders = useQuery({ queryKey: ['catalog', workspaceId, 'purchase-orders'], queryFn: ({ signal }) => workspaceApi<Page<Order>>(workspaceId, '/procurement/purchase-orders?page_size=100', { signal }) })
  const receipts = useQuery({ queryKey: ['catalog', workspaceId, 'receipts'], queryFn: ({ signal }) => workspaceApi<Page<Receipt>>(workspaceId, '/procurement/receipts?page_size=100', { signal }) })
  const order = orders.data?.items.find(value => value.purchase_order_id === orderId)
  const update = (id: string, key: 'received' | 'damaged' | 'short', value: string) => setQuantities(current => ({ ...current, [id]: { received: current[id]?.received ?? '0', damaged: current[id]?.damaged ?? '0', short: current[id]?.short ?? '0', [key]: value } }))
  const refresh = async () => { await client.invalidateQueries({ queryKey: ['catalog', workspaceId, 'receipts'] }); await client.invalidateQueries({ queryKey: ['catalog', workspaceId, 'purchase-orders'] }); await client.invalidateQueries({ queryKey: ['catalog', workspaceId, 'stock'] }) }
  const create = useMutation({ mutationFn: () => workspaceApi<Receipt>(workspaceId, '/procurement/receipts', { method: 'POST', body: jsonBody({ purchase_order_id: orderId, lines: Object.entries(quantities).filter(([, value]) => Number(value.received) + Number(value.damaged) + Number(value.short) > 0).map(([id, value]) => ({ purchase_order_line_id: id, received_quantity: value.received, damaged_quantity: value.damaged, short_quantity: value.short })) }) }), onSuccess: async () => { setQuantities({}); await refresh() } })
  const post = useMutation({ mutationFn: (receipt: Receipt) => workspaceApi<Receipt>(workspaceId, `/procurement/receipts/${receipt.purchase_receive_id}/post`, { method: 'POST', body: jsonBody({ idempotency_key: crypto.randomUUID() }) }), onSuccess: refresh })
  return <>
    <p className="muted">Post accepted goods to physical stock. Short goods stay outstanding. Damaged goods go to a separate, non-sellable quarantine location.</p>
    {canWrite && <section className="card form-card"><h3>Record delivery</h3>{create.isError && <Failure error={create.error} />}<form className="form-grid" onSubmit={(event: FormEvent) => { event.preventDefault(); create.mutate() }}><label className="wide">Sent purchase order<select required value={orderId} onChange={event => { setOrderId(event.target.value); setQuantities({}) }}><option value="">Choose a sent order</option>{orders.data?.items.filter(value => ['SENT', 'PARTIALLY_RECEIVED'].includes(value.status)).map(value => <option key={value.purchase_order_id} value={value.purchase_order_id}>{value.po_no}</option>)}</select></label>{order?.lines.map(line => { const value = quantities[line.purchase_order_line_id] ?? { received: '0', damaged: '0', short: '0' }; return <fieldset className="wide catalog-subform" key={line.purchase_order_line_id}><legend>Line {line.line_no} · Outstanding {number(Number(line.quantity) - Number(line.received_quantity) - Number(line.cancelled_quantity))}</legend><div className="form-grid"><label>Accepted<input type="number" min="0" step="0.0001" value={value.received} onChange={event => update(line.purchase_order_line_id, 'received', event.target.value)} /></label><label>Damaged<input type="number" min="0" step="0.0001" value={value.damaged} onChange={event => update(line.purchase_order_line_id, 'damaged', event.target.value)} /></label><label>Short<input type="number" min="0" step="0.0001" value={value.short} onChange={event => update(line.purchase_order_line_id, 'short', event.target.value)} /></label></div></fieldset> })}<div className="actions wide"><button className="button primary" disabled={create.isPending || !order || !Object.values(quantities).some(value => Number(value.received) + Number(value.damaged) + Number(value.short) > 0)}>Save receipt draft</button></div></form></section>}
    {receipts.isError && <Failure error={receipts.error} />}{post.isError && <Failure error={post.error} />}
    <section className="card table-card"><h3>Receipts</h3>{receipts.isLoading ? <p role="status">Loading receipts…</p> : receipts.data?.items.length ? <div className="table-wrap"><table><thead><tr><th>Receipt</th><th>Status</th><th>Accepted</th><th>Damaged</th><th>Short</th><th>Action</th></tr></thead><tbody>{receipts.data.items.map(value => <tr key={value.purchase_receive_id}><td>{value.receive_no}</td><td>{value.status}</td><td>{number(value.lines.reduce((sum, line) => sum + Number(line.received_quantity), 0))}</td><td>{number(value.total_damaged_qty)}</td><td>{number(value.total_short_qty)}</td><td>{canWrite && value.status === 'DRAFT' && <button className="button small primary" disabled={post.isPending} onClick={() => post.mutate(value)}>Post receipt</button>}</td></tr>)}</tbody></table></div> : <p className="muted">No receipts yet.</p>}</section>
  </>
}
