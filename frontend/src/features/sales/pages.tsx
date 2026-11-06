import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { components } from '../../api/openapi'
import { jsonBody } from '../../api/client'
import { catalogApi, formatMoney, workspaceApi, type ItemSummary, type Page } from '../catalog/api'
import { ShippingPanel } from './shipping'

type SalesOrder = components['schemas']['SalesOrderOut']
type Customer = components['schemas']['CustomerOut']
type Location = components['schemas']['LocationOption']
type Availability = components['schemas']['AvailabilityOut']
type Credit = components['schemas']['CreditExposureOut']
type Line = { item_id: string; location_id: string; uom_id: string; quantity: string; discount_pct: string }

function today() { const date = new Date(); return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}` }
function Failure({ error }: { error: unknown }) { return <p className="error" role="alert">{error instanceof Error ? error.message : 'The request failed'}</p> }

export function SalesWorkspace({ workspaceId, canWrite }: { workspaceId: string; canWrite: boolean }) {
  const client = useQueryClient()
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [editing, setEditing] = useState(false)
  const orders = useQuery({ queryKey: ['catalog', workspaceId, 'sales-orders'], queryFn: ({ signal }) => workspaceApi<Page<SalesOrder>>(workspaceId, '/sales/orders?page_size=100', { signal }) })
  const selected = useQuery({ queryKey: ['catalog', workspaceId, 'sales-order', selectedId], queryFn: ({ signal }) => workspaceApi<SalesOrder>(workspaceId, `/sales/orders/${selectedId}`, { signal }), enabled: !!selectedId })
  const customers = useQuery({ queryKey: ['catalog', workspaceId, 'customers'], queryFn: ({ signal }) => workspaceApi<Page<Customer>>(workspaceId, '/customers?page_size=100', { signal }) })
  const locations = useQuery({ queryKey: ['catalog', workspaceId, 'stock-locations'], queryFn: ({ signal }) => workspaceApi<Location[]>(workspaceId, '/inventory/locations', { signal }) })
  const items = useQuery({ queryKey: ['catalog', workspaceId, 'sales-items'], queryFn: ({ signal }) => catalogApi<Page<ItemSummary>>(workspaceId, '/items?page_size=100&status=ACTIVE', { signal }) })
  const credit = useQuery({ queryKey: ['catalog', workspaceId, 'credit', selected.data?.customer_id], queryFn: ({ signal }) => workspaceApi<Credit>(workspaceId, `/sales/customers/${selected.data?.customer_id}/credit`, { signal }), enabled: !!selected.data?.customer_id })
  const refresh = async (order: SalesOrder) => { setSelectedId(order.sales_order_id); setEditing(false); await client.invalidateQueries({ queryKey: ['catalog', workspaceId] }) }
  const confirm = useMutation({ mutationFn: (order: SalesOrder) => workspaceApi<SalesOrder>(workspaceId, `/sales/orders/${order.sales_order_id}/confirm`, { method: 'POST', body: jsonBody({ expected_revision: order.revision, idempotency_key: crypto.randomUUID() }) }), onSuccess: refresh })
  const cancel = useMutation({ mutationFn: (order: SalesOrder) => workspaceApi<SalesOrder>(workspaceId, `/sales/orders/${order.sales_order_id}/cancel`, { method: 'POST', body: jsonBody({ expected_revision: order.revision, idempotency_key: crypto.randomUUID(), reason: 'Cancelled by user' }) }), onSuccess: refresh })
  const order = selected.data
  return <section>
    <div className="catalog-toolbar"><p className="muted">Create a draft, review stock and customer credit, then confirm the whole order.</p>{canWrite && <button className="button primary" onClick={() => { setSelectedId(null); setEditing(true) }}>New sales order</button>}</div>
    {orders.isError && <Failure error={orders.error} />}
    {orders.isLoading ? <p role="status">Loading sales orders…</p> : orders.data?.items.length ? <div className="card table-card"><div className="table-wrap"><table><thead><tr><th>Order</th><th>Customer</th><th>Date</th><th>Amount</th><th>Status</th><th>Action</th></tr></thead><tbody>{orders.data.items.map(value => <tr key={value.sales_order_id}><td><strong>{value.order_no}</strong></td><td>{customers.data?.items.find(customer => customer.customer_id === value.customer_id)?.name ?? value.customer_id}</td><td>{value.order_date}</td><td>{formatMoney(value.grand_total)}</td><td><span className={`status ${value.status === 'CONFIRMED' ? 'active' : ''}`}>{value.status}</span></td><td><button className="button small secondary" onClick={() => { setSelectedId(value.sales_order_id); setEditing(false) }}>Open</button></td></tr>)}</tbody></table></div></div> : <div className="card empty-state"><h3>No sales orders yet</h3><p>Create a draft when your customers, items and stock locations are ready.</p></div>}
    {selected.isError && <Failure error={selected.error} />}
    {editing && customers.data && locations.data && items.data && <SalesEditor key={order?.sales_order_id ?? 'new'} workspaceId={workspaceId} initial={order?.status === 'DRAFT' ? order : null} customers={customers.data.items} locations={locations.data} items={items.data.items} onSaved={refresh} onClose={() => setEditing(false)} />}
    {order && !editing && <section className="card form-card"><div className="page-heading"><div><p className="eyebrow">SALES ORDER</p><h3>{order.order_no} · {order.status}</h3><p className="muted">{customers.data?.items.find(value => value.customer_id === order.customer_id)?.name ?? 'Customer'} · {order.order_date}</p></div><button className="button secondary" onClick={() => setSelectedId(null)}>Close</button></div>
      <p>Subtotal {formatMoney(order.subtotal)} · Discount {formatMoney(order.discount_total)} · Estimated tax {formatMoney(order.tax_total)} · <strong>Total {formatMoney(order.grand_total)}</strong></p>
      {credit.data && <p className="muted">Customer credit: {formatMoney(credit.data.remaining_credit)} remaining of {formatMoney(credit.data.credit_limit)}{credit.data.credit_hold ? ' · On hold' : ''}.</p>}
      <div className="table-wrap"><table><thead><tr><th>Item</th><th>Location</th><th>Quantity</th><th>Line total</th><th>Reserved</th></tr></thead><tbody>{order.lines.map(line => <tr key={line.sales_order_line_id}><td>{items.data?.items.find(value => value.item_id === line.item_id)?.name ?? line.item_id}</td><td>{locations.data?.find(value => value.location_id === line.location_id)?.code ?? '—'}</td><td>{line.quantity}</td><td>{formatMoney(line.line_total)}</td><td>{line.reserved_quantity}</td></tr>)}</tbody></table></div>
      {order.status === 'DRAFT' && <div className="actions">{canWrite && <button className="button secondary" onClick={() => setEditing(true)}>Edit draft</button>}{canWrite && order.sales_source !== 'POS' && <button className="button primary" disabled={confirm.isPending} onClick={() => confirm.mutate(order)}>Confirm and reserve</button>}{order.sales_source === 'POS' && <p className="muted">POS checkout requires the separate billing and payment connection.</p>}</div>}
      {(order.status === 'CONFIRMED' || order.status === 'PARTIALLY_FULFILLED') && canWrite && <div className="actions"><button className="button secondary" disabled={cancel.isPending} onClick={() => cancel.mutate(order)}>Cancel unshipped quantity</button></div>}
      {confirm.isError && <Failure error={confirm.error} />}{cancel.isError && <Failure error={cancel.error} />}
    </section>}
    {order && !editing && canWrite && (order.status === 'CONFIRMED' || order.status === 'PARTIALLY_FULFILLED') && <ShippingPanel workspaceId={workspaceId} order={order} />}
  </section>
}

function SalesEditor({ workspaceId, initial, customers, locations, items, onSaved, onClose }: { workspaceId: string; initial: SalesOrder | null; customers: Customer[]; locations: Location[]; items: ItemSummary[]; onSaved: (order: SalesOrder) => Promise<void>; onClose: () => void }) {
  const availableLocations = locations.filter(value => value.is_active)
  const [customerId, setCustomerId] = useState(initial?.customer_id ?? customers[0]?.customer_id ?? '')
  const [locationId, setLocationId] = useState(initial?.location_id ?? availableLocations[0]?.location_id ?? '')
  const [orderDate, setOrderDate] = useState(initial?.order_date ?? today())
  const [notes, setNotes] = useState(initial?.notes ?? '')
  const [source, setSource] = useState<'MANUAL_RETAIL' | 'WHOLESALE' | 'POS'>(initial?.sales_source ?? 'MANUAL_RETAIL')
  const [lines, setLines] = useState<Line[]>(initial ? initial.lines.map(line => ({ item_id: line.item_id, location_id: line.location_id ?? '', uom_id: line.uom_id, quantity: String(line.quantity), discount_pct: String(line.discount_pct) })) : [{ item_id: '', location_id: '', uom_id: '', quantity: '1', discount_pct: '0' }])
  const credit = useQuery({ queryKey: ['catalog', workspaceId, 'credit', customerId], queryFn: ({ signal }) => workspaceApi<Credit>(workspaceId, `/sales/customers/${customerId}/credit`, { signal }), enabled: !!customerId })
  const changeLine = (position: number, change: Partial<Line>) => setLines(current => current.map((line, index) => index === position ? { ...line, ...change } : line))
  const save = useMutation({ mutationFn: () => workspaceApi<SalesOrder>(workspaceId, initial ? `/sales/orders/${initial.sales_order_id}` : '/sales/orders', { method: initial ? 'PATCH' : 'POST', body: jsonBody({ customer_id: customerId, location_id: locationId, order_date: orderDate, sales_source: source, notes: notes || null, lines: lines.map(line => ({ ...line, location_id: line.location_id || null })), ...(initial ? { expected_revision: initial.revision } : {}) }) }), onSuccess: onSaved })
  return <section className="card form-card"><div className="page-heading"><div><h3>{initial ? `Edit ${initial.order_no}` : 'New sales order'}</h3><p className="muted">Prices and estimated tax are calculated when you save the draft.</p></div><button className="button secondary" onClick={onClose}>Close</button></div>
    {customers.length === 0 && <p className="error">Add a customer before creating an order.</p>}{availableLocations.length === 0 && <p className="error">Add a warehouse or store location first.</p>}
    <form className="form-grid" onSubmit={(event: FormEvent) => { event.preventDefault(); save.mutate() }}><label>Customer<select required value={customerId} onChange={event => setCustomerId(event.target.value)}><option value="">Choose customer</option>{customers.filter(value => value.is_active).map(value => <option key={value.customer_id} value={value.customer_id}>{value.code} · {value.name}</option>)}</select></label><label>Default location<select required value={locationId} onChange={event => setLocationId(event.target.value)}><option value="">Choose location</option>{availableLocations.map(value => <option key={value.location_id} value={value.location_id}>{value.code} · {value.name}</option>)}</select></label><label>Sales source<select value={source} disabled={!!initial} onChange={event => setSource(event.target.value as 'MANUAL_RETAIL' | 'WHOLESALE')}><option value="MANUAL_RETAIL">Manual retail</option><option value="WHOLESALE">Wholesale</option>{source === 'POS' && <option value="POS">POS</option>}</select></label><label>Order date<input type="date" required value={orderDate} onChange={event => setOrderDate(event.target.value)} /></label><label>Notes<input value={notes} onChange={event => setNotes(event.target.value)} /></label>
      {credit.data && <p className="muted wide">Credit available: {formatMoney(credit.data.remaining_credit)}{credit.data.credit_hold ? ' · Customer is on hold' : ''}</p>}
      {lines.map((line, index) => <fieldset className="catalog-subform wide" key={index}><legend>Line {index + 1}</legend><div className="form-grid"><label>Item<select required value={line.item_id} onChange={event => { const item = items.find(value => value.item_id === event.target.value); changeLine(index, { item_id: event.target.value, uom_id: item?.stock_uom_id ?? '' }) }}><option value="">Choose item</option>{items.map(value => <option key={value.item_id} value={value.item_id}>{value.sku} · {value.name}</option>)}</select></label><label>Location<select value={line.location_id} onChange={event => changeLine(index, { location_id: event.target.value })}><option value="">Default location</option>{availableLocations.map(value => <option key={value.location_id} value={value.location_id}>{value.code}</option>)}</select></label><label>Unit<input readOnly value={line.uom_id ? 'Stock unit' : 'Choose item'} /></label><label>Quantity<input type="number" required min="0.0001" step="any" value={line.quantity} onChange={event => changeLine(index, { quantity: event.target.value })} /></label><label>Discount %<input type="number" min="0" max="100" step="0.0001" value={line.discount_pct} onChange={event => changeLine(index, { discount_pct: event.target.value })} /></label>{line.item_id && (line.location_id || locationId) && <AvailabilityNote workspaceId={workspaceId} itemId={line.item_id} locationId={line.location_id || locationId} />}</div>{lines.length > 1 && <button type="button" className="button small secondary" onClick={() => setLines(current => current.filter((_, position) => position !== index))}>Remove line</button>}</fieldset>)}
      <div className="actions wide"><button className="button secondary" type="button" onClick={() => setLines(current => [...current, { item_id: '', location_id: '', uom_id: '', quantity: '1', discount_pct: '0' }])}>Add line</button><button className="button primary" disabled={save.isPending || !customerId || !locationId}>Save draft</button></div>{save.isError && <Failure error={save.error} />}
    </form>
  </section>
}

function AvailabilityNote({ workspaceId, itemId, locationId }: { workspaceId: string; itemId: string; locationId: string }) {
  const result = useQuery({ queryKey: ['catalog', workspaceId, 'sales-availability', itemId, locationId], queryFn: ({ signal }) => workspaceApi<Availability>(workspaceId, `/sales/availability?item_id=${itemId}&location_id=${locationId}`, { signal }) })
  return <p className="muted">{result.isLoading ? 'Checking availability…' : result.isError ? 'Availability unavailable' : `${result.data?.available_stock_units ?? 0} stock units available${result.data?.components.length && result.data.components[0].item_id !== itemId ? ' from kit components' : ''}`}</p>
}
