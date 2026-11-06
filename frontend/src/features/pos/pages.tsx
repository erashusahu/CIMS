import { useEffect, useState, type FormEvent } from 'react'
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import type { components } from '../../api/openapi'
import { jsonBody } from '../../api/client'
import { catalogApi, formatMoney, workspaceApi, type CatalogItem, type ItemSummary, type Page } from '../catalog/api'

type Order = components['schemas']['SalesOrderOut']
type Customer = components['schemas']['CustomerOut']
type Location = components['schemas']['LocationOption']
type CartLine = { item_id: string; sku: string; name: string; uom_id: string; quantity: string }

function today() { const date = new Date(); return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}` }
function Failure({ error }: { error: unknown }) { return <p className="error" role="alert">{error instanceof Error ? error.message : 'The request failed'}</p> }

export function PosWorkspace({ workspaceId, canWrite }: { workspaceId: string; canWrite: boolean }) {
  const client = useQueryClient()
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [locationId, setLocationId] = useState('')
  const [customerId, setCustomerId] = useState('')
  const [search, setSearch] = useState('')
  const [cart, setCart] = useState<CartLine[]>([])
  const status = useQuery({ queryKey: ['catalog', workspaceId, 'pos-status'], queryFn: ({ signal }) => workspaceApi<{ ready: boolean; checkout_ready: boolean; required_sql: string }>(workspaceId, '/pos/status', { signal }) })
  const ready = status.data?.ready === true
  const orders = useQuery({ queryKey: ['catalog', workspaceId, 'pos-drafts'], queryFn: ({ signal }) => workspaceApi<Page<Order>>(workspaceId, '/sales/orders?source=POS&page_size=100', { signal }), enabled: ready })
  const selected = useQuery({ queryKey: ['catalog', workspaceId, 'pos-draft', selectedId], queryFn: ({ signal }) => workspaceApi<Order>(workspaceId, `/sales/orders/${selectedId}`, { signal }), enabled: ready && !!selectedId })
  const cartItems = useQueries({ queries: (selected.data?.lines ?? []).map(line => ({ queryKey: ['catalog', workspaceId, 'pos-item', line.item_id], queryFn: ({ signal }: { signal: AbortSignal }) => catalogApi<CatalogItem>(workspaceId, `/items/${line.item_id}`, { signal }) })) })
  const customers = useQuery({ queryKey: ['catalog', workspaceId, 'pos-customers'], queryFn: ({ signal }) => workspaceApi<Page<Customer>>(workspaceId, '/customers?page_size=100', { signal }), enabled: ready })
  const locations = useQuery({ queryKey: ['catalog', workspaceId, 'pos-locations'], queryFn: ({ signal }) => workspaceApi<Location[]>(workspaceId, '/inventory/locations', { signal }), enabled: ready })
  const found = useQuery({ queryKey: ['catalog', workspaceId, 'pos-search', search], queryFn: ({ signal }) => catalogApi<Page<ItemSummary>>(workspaceId, `/items?page_size=25&status=ACTIVE&search=${encodeURIComponent(search.trim())}`, { signal }), enabled: ready && search.trim().length > 0 })
  const walkIn = useMutation({ mutationFn: () => workspaceApi<Customer>(workspaceId, `/pos/walk-in-customer?location_id=${locationId}`, { method: 'POST', body: jsonBody({}) }), onSuccess: async value => { setCustomerId(value.customer_id); await client.invalidateQueries({ queryKey: ['catalog', workspaceId, 'pos-customers'] }) } })
  const save = useMutation({ mutationFn: () => workspaceApi<Order>(workspaceId, selectedId ? `/sales/orders/${selectedId}` : '/sales/orders', { method: selectedId ? 'PATCH' : 'POST', body: jsonBody({ customer_id: customerId, location_id: locationId, order_date: today(), sales_source: 'POS', lines: cart.map(line => ({ item_id: line.item_id, uom_id: line.uom_id, quantity: line.quantity, location_id: null, discount_pct: '0' })), ...(selectedId ? { expected_revision: selected.data?.revision } : {}) }) }), onSuccess: async value => { setSelectedId(value.sales_order_id); await client.invalidateQueries({ queryKey: ['catalog', workspaceId] }) } })
  const discard = useMutation({ mutationFn: () => workspaceApi<Order>(workspaceId, `/pos/carts/${selectedId}/discard`, { method: 'POST', body: jsonBody({ expected_revision: selected.data?.revision }) }), onSuccess: async () => { newCart(); await client.invalidateQueries({ queryKey: ['catalog', workspaceId] }) } })

  useEffect(() => {
    if (!selected.data || selected.data.sales_order_id !== selectedId) return
    setLocationId(selected.data.location_id ?? '')
    setCustomerId(selected.data.customer_id)
    setCart(selected.data.lines.map(line => ({ item_id: line.item_id, sku: line.item_id, name: line.item_id, uom_id: line.uom_id, quantity: String(line.quantity) })))
  }, [selected.data?.sales_order_id, selectedId])

  const add = (item: ItemSummary) => setCart(current => {
    if (current.some(line => line.item_id === item.item_id)) return current.map(line => line.item_id === item.item_id ? { ...line, quantity: String(Number(line.quantity) + 1) } : line)
    return [...current, { item_id: item.item_id, sku: item.sku, name: item.name, uom_id: item.stock_uom_id, quantity: '1' }]
  })
  const newCart = () => { setSelectedId(null); setCart([]); setCustomerId(''); setSearch(''); save.reset() }
  const locationOptions = locations.data?.filter(value => value.is_active) ?? []
  const displayItem = (line: CartLine) => {
    const item = cartItems.find(result => result.data?.item_id === line.item_id)?.data
    return item ? `${item.sku} · ${item.name}` : `${line.sku} · ${line.name}`
  }
  return <section>
    {status.isLoading && <p role="status">Checking POS setup…</p>}{status.isError && <Failure error={status.error} />}
    {status.data && !ready && <section className="card form-card"><h2>Finish POS draft setup</h2><p>Apply {status.data.required_sql} after SQL 003–008, then check again.</p><button className="button secondary" onClick={() => void status.refetch()}>Check again</button></section>}
    {ready && <>
      <div className="catalog-toolbar"><p className="muted">Scan or search items and save a POS cart as a priced sales draft. Checkout opens after the billing and payment connection is defined.</p>{canWrite && <button className="button secondary" onClick={newCart}>New cart</button>}</div>
      {orders.isError && <Failure error={orders.error} />}
      {orders.data && <section className="card table-card"><h3>POS carts</h3><div className="table-wrap"><table><thead><tr><th>Draft</th><th>Customer</th><th>Amount</th><th>Status</th><th></th></tr></thead><tbody>{orders.data.items.map(order => <tr key={order.sales_order_id}><td>{order.order_no}</td><td>{customers.data?.items.find(value => value.customer_id === order.customer_id)?.name ?? order.customer_id}</td><td>{formatMoney(order.grand_total)}</td><td>{order.status}</td><td><button className="button small secondary" onClick={() => setSelectedId(order.sales_order_id)}>Open</button></td></tr>)}</tbody></table></div>{orders.data.items.length === 0 && <p className="muted">No POS carts yet.</p>}</section>}
      {selected.isError && <Failure error={selected.error} />}{customers.isError && <Failure error={customers.error} />}{locations.isError && <Failure error={locations.error} />}
      {canWrite && <section className="card form-card"><div className="page-heading"><div><p className="eyebrow">POS CART</p><h3>{selected.data?.order_no ?? 'New cart'}</h3></div></div><form className="form-grid" onSubmit={(event: FormEvent) => { event.preventDefault(); save.mutate() }}><label>Store location<select required value={locationId} onChange={event => setLocationId(event.target.value)}><option value="">Choose location</option>{locationOptions.map(value => <option key={value.location_id} value={value.location_id}>{value.code} · {value.name}</option>)}</select></label><label>Customer<select required value={customerId} onChange={event => setCustomerId(event.target.value)}><option value="">Choose customer</option>{customers.data?.items.filter(value => value.is_active).map(value => <option key={value.customer_id} value={value.customer_id}>{value.code} · {value.name}</option>)}</select></label><button type="button" className="button secondary" disabled={!locationId || walkIn.isPending} onClick={() => walkIn.mutate()}>Use walk-in customer</button>{walkIn.isError && <Failure error={walkIn.error} />}<label className="wide">Scan barcode or search SKU<input value={search} onChange={event => setSearch(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); const code = search.trim().toLowerCase(); const exact = found.data?.items.find(item => item.barcode?.toLowerCase() === code || item.sku.toLowerCase() === code); if (exact) { add(exact); setSearch('') } } }} placeholder="Barcode, SKU or name" /></label>{found.isError && <Failure error={found.error} />}{found.data && <div className="wide table-wrap"><table><thead><tr><th>Item</th><th>Barcode</th><th></th></tr></thead><tbody>{found.data.items.map(item => <tr key={item.item_id}><td>{item.sku} · {item.name}</td><td>{item.barcode ?? '—'}</td><td><button type="button" className="button small secondary" onClick={() => { add(item); setSearch('') }}>Add</button></td></tr>)}</tbody></table></div>}{cart.length > 0 && <div className="wide table-wrap"><table><thead><tr><th>Cart item</th><th>Quantity</th><th></th></tr></thead><tbody>{cart.map(line => <tr key={line.item_id}><td>{displayItem(line)}</td><td><input aria-label={`Quantity for ${line.sku}`} type="number" min="0.0001" step="any" required value={line.quantity} onChange={event => setCart(current => current.map(value => value.item_id === line.item_id ? { ...value, quantity: event.target.value } : value))} /></td><td><button type="button" className="button small secondary" onClick={() => setCart(current => current.filter(value => value.item_id !== line.item_id))}>Remove</button></td></tr>)}</tbody></table></div>}<div className="actions wide"><button className="button primary" disabled={save.isPending || !locationId || !customerId || cart.length === 0 || (selectedId !== null && selected.data?.status !== 'DRAFT')}>{selectedId ? 'Update POS draft' : 'Save POS draft'}</button>{selected.data?.status === 'DRAFT' && <button type="button" className="button secondary" disabled={discard.isPending} onClick={() => discard.mutate()}>Discard draft</button>}</div>{save.isError && <Failure error={save.error} />}{discard.isError && <Failure error={discard.error} />}</form><p className="muted">POS drafts do not reserve stock, ship goods, record payment or issue receipts.</p></section>}
    </>}
  </section>
}
