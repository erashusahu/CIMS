import { useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { components } from '../../api/openapi'
import { catalogApi, formatMoney, workspaceApi, type ItemSummary, type Page } from '../catalog/api'

type Overview = components['schemas']['OperationsOverview']
type Stock = components['schemas']['StockBalanceOut']
type Movement = components['schemas']['MovementOut']
type Aging = components['schemas']['AgingLayerOut']
type SalesRow = components['schemas']['SalesSummaryRow']
type PurchaseRow = components['schemas']['PurchaseSummaryRow']
type TaxRow = components['schemas']['OrderTaxSupportRow']
type Location = components['schemas']['LocationOption']
type Section = 'overview' | 'stock' | 'movements' | 'aging' | 'sales' | 'purchasing' | 'tax'

const sections: { key: Section; label: string }[] = [
  { key: 'overview', label: 'Overview' }, { key: 'stock', label: 'Stock & value' },
  { key: 'movements', label: 'Movements' }, { key: 'aging', label: 'FIFO aging' },
  { key: 'sales', label: 'Sales orders' }, { key: 'purchasing', label: 'Purchasing' },
  { key: 'tax', label: 'Order tax' },
]
const number = (value: string | number) => Number(value).toLocaleString('en-IN', { maximumFractionDigits: 4 })
const params = (values: Record<string, string | number>) => new URLSearchParams(Object.entries(values).filter(([, value]) => value !== '').map(([key, value]) => [key, String(value)])).toString()

function ReportTable({ headers, rows, empty }: { headers: string[]; rows: ReactNode[][]; empty: string }) {
  return <div className="card table-card table-wrap"><table><thead><tr>{headers.map(value => <th key={value}>{value}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={index}>{row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}</tr>)}</tbody></table>{rows.length === 0 && <p className="muted">{empty}</p>}</div>
}

function Pagination({ page, total, onPage }: { page: number; total: number; onPage: (value: number) => void }) {
  return <div className="pagination"><button className="button small secondary" disabled={page === 1} onClick={() => onPage(page - 1)}>Previous</button><span>Page {page} · {total} rows</span><button className="button small secondary" disabled={page * 25 >= total} onClick={() => onPage(page + 1)}>Next</button></div>
}

export function ReportsWorkspace({ workspaceId }: { workspaceId: string }) {
  const [section, setSection] = useState<Section>('overview')
  const [page, setPage] = useState(1)
  const [locationId, setLocationId] = useState('')
  const [itemId, setItemId] = useState('')
  const [itemSearch, setItemSearch] = useState('')
  const [stockState, setStockState] = useState('')
  const [fromDate, setFromDate] = useState('')
  const [toDate, setToDate] = useState('')
  const inventorySection = section === 'stock' || section === 'movements' || section === 'aging'
  const dated = section === 'sales' || section === 'purchasing' || section === 'tax'
  const validDates = !fromDate || !toDate || fromDate <= toDate
  const inventoryQuery = params({ page, page_size: 25, item_id: itemId, location_id: locationId })
  const dateQuery = params({ from_date: fromDate, to_date: toDate })
  const locations = useQuery({ queryKey: ['catalog', workspaceId, 'report-locations'], queryFn: ({ signal }) => workspaceApi<Location[]>(workspaceId, '/inventory/locations', { signal }), enabled: inventorySection })
  const items = useQuery({ queryKey: ['catalog', workspaceId, 'report-items', itemSearch], queryFn: ({ signal }) => catalogApi<Page<ItemSummary>>(workspaceId, `/items?page_size=25&status=ACTIVE&search=${encodeURIComponent(itemSearch)}`, { signal }), enabled: inventorySection && itemSearch.trim().length > 0 })
  const overview = useQuery({ queryKey: ['catalog', workspaceId, 'reports-overview'], queryFn: ({ signal }) => workspaceApi<Overview>(workspaceId, '/reports/overview', { signal }), enabled: section === 'overview' })
  const stock = useQuery({ queryKey: ['catalog', workspaceId, 'report-stock', inventoryQuery], queryFn: ({ signal }) => workspaceApi<Page<Stock>>(workspaceId, `/inventory/balances?${inventoryQuery}`, { signal }), enabled: section === 'stock' })
  const movements = useQuery({ queryKey: ['catalog', workspaceId, 'report-movements', inventoryQuery, stockState], queryFn: ({ signal }) => workspaceApi<Page<Movement>>(workspaceId, `/inventory/movements?${inventoryQuery}${stockState ? `&stock_state=${stockState}` : ''}`, { signal }), enabled: section === 'movements' })
  const aging = useQuery({ queryKey: ['catalog', workspaceId, 'report-aging', inventoryQuery], queryFn: ({ signal }) => workspaceApi<Page<Aging>>(workspaceId, `/reports/fifo-aging?${inventoryQuery}`, { signal }), enabled: section === 'aging' })
  const sales = useQuery({ queryKey: ['catalog', workspaceId, 'report-sales', dateQuery], queryFn: ({ signal }) => workspaceApi<SalesRow[]>(workspaceId, `/reports/sales-summary?${dateQuery}`, { signal }), enabled: section === 'sales' && validDates })
  const purchasing = useQuery({ queryKey: ['catalog', workspaceId, 'report-purchasing', dateQuery], queryFn: ({ signal }) => workspaceApi<PurchaseRow[]>(workspaceId, `/reports/purchase-summary?${dateQuery}`, { signal }), enabled: section === 'purchasing' && validDates })
  const tax = useQuery({ queryKey: ['catalog', workspaceId, 'report-tax', dateQuery], queryFn: ({ signal }) => workspaceApi<TaxRow[]>(workspaceId, `/reports/order-tax-support?${dateQuery}`, { signal }), enabled: section === 'tax' && validDates })
  const current = section === 'overview' ? overview : section === 'stock' ? stock : section === 'movements' ? movements : section === 'aging' ? aging : section === 'sales' ? sales : section === 'purchasing' ? purchasing : tax
  const cards = overview.data ? [
    ['Active items', overview.data.active_items], ['Stock positions', overview.data.stock_positions],
    ['At or below safety stock', overview.data.below_safety_positions], ['Open purchase orders', overview.data.open_purchase_orders],
    ['Confirmed sales orders', overview.data.confirmed_sales_orders], ['Active counts', overview.data.active_counts],
    ['Reconciliation jobs waiting', overview.data.pending_outbox_events],
  ] as const : []
  return <section className="reports-workspace">
    <div className="catalog-tabs" role="tablist" aria-label="Report sections">{sections.map(value => <button key={value.key} role="tab" aria-selected={section === value.key} className={`button ${section === value.key ? 'primary' : 'secondary'}`} onClick={() => { setSection(value.key); setPage(1) }}>{value.label}</button>)}</div>
    {inventorySection && <div className="card report-filters"><label>Location<select value={locationId} onChange={event => { setLocationId(event.target.value); setPage(1) }}><option value="">All locations</option>{locations.data?.map(value => <option key={value.location_id} value={value.location_id}>{value.code} · {value.name}</option>)}</select></label><label>Find item<input type="search" value={itemSearch} onChange={event => { setItemSearch(event.target.value); setItemId(''); setPage(1) }} placeholder="SKU or name" /></label><label>Item<select value={itemId} onChange={event => { setItemId(event.target.value); setPage(1) }}><option value="">All items</option>{items.data?.items.map(value => <option key={value.item_id} value={value.item_id}>{value.sku} · {value.name}</option>)}</select></label>{section === 'movements' && <label>Stock state<select value={stockState} onChange={event => { setStockState(event.target.value); setPage(1) }}><option value="">All states</option>{['PHYSICAL', 'ACCOUNTING', 'COMMITTED', 'INCOMING'].map(value => <option key={value}>{value}</option>)}</select></label>}</div>}
    {dated && <div className="card report-filters"><label>From<input type="date" value={fromDate} onChange={event => setFromDate(event.target.value)} /></label><label>To<input type="date" value={toDate} onChange={event => setToDate(event.target.value)} /></label></div>}
    {!validDates && <p className="error" role="alert">Start date must be on or before end date.</p>}
    {current.isLoading && <p role="status">Loading report…</p>}
    {current.isError && <p className="error" role="alert">{current.error instanceof Error ? current.error.message : 'Report unavailable'}</p>}
    {section === 'overview' && overview.data && <><div className="catalog-toolbar"><p className="muted">Whole-business operational counts, as of {new Date(overview.data.as_of).toLocaleString()}.</p><button className="button secondary" onClick={() => void overview.refetch()}>Refresh</button></div><div className="report-grid">{cards.map(([label, value]) => <article className="card report-card" key={label}><span className="muted">{label}</span><strong>{value.toLocaleString('en-IN')}</strong></article>)}</div><p className="muted">Stock quantities and valuation are item and location specific. Invoice and payment totals belong to the separate billing system.</p></>}
    {section === 'stock' && stock.data && <><p className="muted">Current stock and valuation per item and location. Incomplete FIFO valuations are flagged.</p><ReportTable headers={['Item', 'Location', 'Physical', 'Committed', 'Available', 'Incoming', 'Method', 'Value']} rows={stock.data.items.map(value => [<>{value.sku}<small>{value.item_name}</small></>, value.location_code, number(value.physical_on_hand), number(value.committed), number(value.available), number(value.incoming), value.valuation_method, value.valuation_complete && value.inventory_value !== null ? formatMoney(value.inventory_value) : 'Reconcile FIFO'])} empty="No stock positions match these filters." /><Pagination page={page} total={stock.data.total} onPage={setPage} /></>}
    {section === 'movements' && movements.data && <><p className="muted">Recent ledger entries, newest first. Each row retains its source document.</p><ReportTable headers={['Time', 'Item', 'Location', 'State', 'Direction', 'Quantity', 'Source']} rows={movements.data.items.map(value => [new Date(value.occurred_at).toLocaleString(), value.sku, value.location_code, value.stock_state, value.direction, number(value.quantity), value.source_document_no ?? value.source_document_type ?? '—'])} empty="No movements match these filters." /><Pagination page={page} total={movements.data.total} onPage={setPage} /></>}
    {section === 'aging' && aging.data && <><p className="muted">Remaining FIFO receipt layers only. Weighted-average stock has no receipt-layer aging.</p><ReportTable headers={['Item', 'Location', 'Received', 'Age', 'Remaining units', 'Unit cost', 'Remaining value']} rows={aging.data.items.map(value => [value.sku, value.location_code, new Date(value.received_at).toLocaleDateString(), `${value.age_days} days`, number(value.remaining_quantity), formatMoney(value.unit_cost), formatMoney(value.remaining_value)])} empty="No remaining FIFO layers match these filters." /><Pagination page={page} total={aging.data.total} onPage={setPage} /></>}
    {section === 'sales' && sales.data && <><p className="muted">Sales-order snapshots by status and source. Order value is not invoiced revenue.</p><ReportTable headers={['Status', 'Source', 'Orders', 'Order value', 'Estimated tax']} rows={sales.data.map(value => [value.status, value.source, value.order_count, formatMoney(value.order_value), formatMoney(value.estimated_tax)])} empty="No sales orders in this period." /></>}
    {section === 'purchasing' && purchasing.data && <><p className="muted">Purchase-order snapshots by status and currency. Order value is not an accounting bill.</p><ReportTable headers={['Status', 'Currency', 'Orders', 'Order value', 'Estimated tax']} rows={purchasing.data.map(value => [value.status, value.currency, value.order_count, value.currency === 'INR' ? formatMoney(value.order_value) : number(value.order_value), value.currency === 'INR' ? formatMoney(value.estimated_tax) : number(value.estimated_tax)])} empty="No purchase orders in this period." /></>}
    {section === 'tax' && tax.data && <><p className="muted">Tax estimates from confirmed sales-order lines. These are not invoice or filed tax totals.</p><ReportTable headers={['Tax rate', 'Lines', 'Taxable value', 'Estimated tax']} rows={tax.data.map(value => [`${number(value.tax_rate)}%`, value.line_count, formatMoney(value.taxable_value), formatMoney(value.estimated_tax)])} empty="No confirmed order lines in this period." /></>}
  </section>
}
