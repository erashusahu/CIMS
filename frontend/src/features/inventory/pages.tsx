import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { components } from '../../api/openapi'
import { formatMoney, workspaceApi, type Page } from '../catalog/api'
import { OpeningStockView } from './opening'

type Balance = components['schemas']['StockBalanceOut']
type Movement = components['schemas']['MovementOut']
type Difference = components['schemas']['StockDifferenceOut']

function quantity(value: string) { return value.includes('.') ? value.replace(/0+$/, '').replace(/\.$/, '') : value }

export function InventoryView({ workspaceId, initialView = 'balances' }: { workspaceId: string; initialView?: 'balances' | 'opening' }) {
  const [view, setView] = useState<'balances' | 'movements' | 'reconciliation' | 'opening'>(initialView)
  const [page, setPage] = useState(1)
  const [state, setState] = useState('')
  const balances = useQuery({ queryKey: ['catalog', workspaceId, 'stock', page], queryFn: ({ signal }) => workspaceApi<Page<Balance>>(workspaceId, `/inventory/balances?page=${page}&page_size=25`, { signal }), enabled: view === 'balances' })
  const movements = useQuery({ queryKey: ['catalog', workspaceId, 'movements', page, state], queryFn: ({ signal }) => workspaceApi<Page<Movement>>(workspaceId, `/inventory/movements?page=${page}&page_size=25${state ? `&stock_state=${state}` : ''}`, { signal }), enabled: view === 'movements' })
  const reconciliation = useQuery({ queryKey: ['catalog', workspaceId, 'reconciliation', page], queryFn: ({ signal }) => workspaceApi<Page<Difference>>(workspaceId, `/inventory/reconciliation?page=${page}&page_size=25`, { signal }), enabled: view === 'reconciliation' })
  const current = view === 'balances' ? balances : view === 'movements' ? movements : reconciliation
  return <section>
    <div className="catalog-tabs" role="tablist" aria-label="Stock views">{(['balances', 'movements', 'reconciliation', 'opening'] as const).map(value => <button key={value} role="tab" aria-selected={view === value} className={`button ${view === value ? 'primary' : 'secondary'}`} onClick={() => { setView(value); setPage(1) }}>{value === 'balances' ? 'Stock balances' : value === 'movements' ? 'Movement history' : value === 'opening' ? 'Opening stock' : 'Reconciliation'}</button>)}</div>
    {view === 'opening' && <OpeningStockView workspaceId={workspaceId} />}
    {view === 'balances' && <p className="muted">Physical and accounting quantities are separate. Available is physical minus committed. ATP also deducts safety stock at eligible locations.</p>}
    {view === 'movements' && <div className="catalog-toolbar"><label>Stock state<select value={state} onChange={event => { setState(event.target.value); setPage(1) }}><option value="">All stock states</option><option value="PHYSICAL">Physical</option><option value="ACCOUNTING">Accounting</option><option value="COMMITTED">Committed</option><option value="INCOMING">Incoming</option></select></label></div>}
    {view !== 'opening' && current.isLoading && <p role="status">Loading stock records…</p>}
    {view !== 'opening' && current.isError && <p className="error" role="alert">{current.error.message}</p>}
    {view !== 'opening' && current.data?.total === 0 && <div className="card empty-state"><h2>{view === 'reconciliation' ? 'No quantity drift found' : view === 'movements' ? 'No stock movements yet' : 'No posted stock yet'}</h2><p>{view === 'reconciliation' ? 'Ledger quantities and recorded balances agree for this workspace.' : 'Catalog setup does not create stock. Posted movements will appear here.'}</p></div>}
    {view === 'balances' && !!balances.data?.items.length && <div className="card table-card"><div className="table-wrap"><table><thead><tr><th>Item / location</th><th>Physical</th><th>Accounting</th><th>Committed</th><th>Incoming</th><th>Available</th><th>ATP</th><th>Valuation</th></tr></thead><tbody>{balances.data.items.map(row => <tr key={`${row.item_id}:${row.location_id}`}><td><strong>{row.sku} · {row.item_name}</strong><small>{row.location_code} · {row.location_name}</small></td><td>{quantity(row.physical_on_hand)}</td><td>{quantity(row.accounting_on_hand)}</td><td>{quantity(row.committed)}</td><td>{quantity(row.incoming)}</td><td>{quantity(row.available)}</td><td>{quantity(row.atp)}{!row.eligible_for_sales && <small>Location not eligible for sales</small>}</td><td>{row.inventory_value === null ? <span className="error">Review FIFO layers</span> : formatMoney(row.inventory_value)}<small>{row.valuation_method === 'FIFO' ? 'FIFO layers' : 'Weighted average'}</small></td></tr>)}</tbody></table></div></div>}
    {view === 'movements' && !!movements.data?.items.length && <div className="card table-card"><div className="table-wrap"><table><thead><tr><th>Time</th><th>Item / location</th><th>Stock state</th><th>Direction</th><th>Quantity</th><th>Unit cost</th><th>Source document</th></tr></thead><tbody>{movements.data.items.map(row => <tr key={`${row.entry_id}:${row.occurred_at}`}><td>{new Date(row.occurred_at).toLocaleString('en-IN')}</td><td><strong>{row.sku}</strong><small>{row.location_code}</small></td><td>{row.stock_state}</td><td>{row.direction}</td><td>{quantity(row.quantity)}</td><td>{row.unit_cost === null ? '—' : formatMoney(row.unit_cost)}</td><td>{row.source_document_no ?? row.source_document_id ?? '—'}<small>{row.source_document_type}</small></td></tr>)}</tbody></table></div></div>}
    {view === 'reconciliation' && !!reconciliation.data?.items.length && <div className="card table-card"><p className="notice error" role="alert">{reconciliation.data.total} item/location records differ from their ledger. Review the history before making a correction.</p><div className="table-wrap"><table><thead><tr><th>Item</th><th>Location</th><th>Physical difference</th><th>Accounting difference</th><th>Committed difference</th><th>Incoming difference</th></tr></thead><tbody>{reconciliation.data.items.map(row => <tr key={`${row.item_id}:${row.location_id}`}><td><code>{row.item_id}</code></td><td><code>{row.location_id}</code></td><td>{quantity(row.physical_difference)}</td><td>{quantity(row.accounting_difference)}</td><td>{quantity(row.committed_difference)}</td><td>{quantity(row.incoming_difference)}</td></tr>)}</tbody></table></div></div>}
    {view !== 'opening' && !!current.data?.total && <div className="pagination"><button className="button small secondary" disabled={page === 1} onClick={() => setPage(value => value - 1)}>Previous</button><span>Page {page} · {current.data.total} records</span><button className="button small secondary" disabled={page * 25 >= current.data.total} onClick={() => setPage(value => value + 1)}>Next</button></div>}
  </section>
}
