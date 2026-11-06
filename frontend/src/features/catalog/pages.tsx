import { useEffect, useRef, useState, type FormEvent } from 'react'
import { createPortal } from 'react-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { jsonBody } from '../../api/client'
import { InventoryView } from '../inventory/pages'
import { ProcurementView } from '../procurement/pages'
import { CustomersView } from '../customers/pages'
import { SalesWorkspace } from '../sales/pages'
import { TransfersWorkspace } from '../transfers/pages'
import { RecoveryWorkspace } from '../recovery/pages'
import { PosWorkspace } from '../pos/pages'
import { MobileWorkspace } from '../mobile/pages'
import { ReportsWorkspace } from '../reports/pages'
import { GetStartedView } from './get-started'
import {
  catalogApi, downloadTemplate, formatMoney, type CatalogItem, type CatalogOptions,
  type ConversionResult, type ImportPreview, type ItemInput, type ItemSummary,
  type Page, type Workspace,
} from './api'

function Failure({ error }: { error: unknown }) {
  const fields = error instanceof Error && 'fields' in error && Array.isArray(error.fields) ? error.fields : []
  return <div className="error" role="alert"><p>{error instanceof Error ? error.message : 'The request could not be completed'}</p>{fields.length > 0 && <ul>{fields.slice(0, 12).map((field: unknown, index) => field && typeof field === 'object' && 'msg' in field && typeof field.msg === 'string' ? <li key={index}>{'loc' in field && Array.isArray(field.loc) ? `${field.loc.filter(value => value !== 'body').join(' · ').replaceAll('_', ' ')}: ` : ''}{field.msg}</li> : null)}</ul>}</div>
}

export function CatalogWorkspace() {
  const client = useQueryClient()
  const [selected, setSelected] = useState(() => sessionStorage.getItem('cims.local.workspace') ?? '')
  const [name, setName] = useState('')
  const [slug, setSlug] = useState('')
  const [showCreate, setShowCreate] = useState(false)
  const status = useQuery({ queryKey: ['catalog-local-status'], queryFn: ({ signal }) => catalogApi<{ ready: boolean }>(null, '/local-status', { signal }), retry: false })
  const workspaces = useQuery({ queryKey: ['catalog-local-workspaces'], queryFn: ({ signal }) => catalogApi<Workspace[]>(null, '/local-workspaces', { signal }), enabled: status.isSuccess })
  const choose = (id: string) => {
    void client.cancelQueries({ queryKey: ['catalog'] })
    client.removeQueries({ queryKey: ['catalog'] })
    sessionStorage.setItem('cims.local.workspace', id)
    setSelected(id)
  }
  useEffect(() => {
    if (workspaces.data?.length && !workspaces.data.some(value => value.tenant_id === selected)) choose(workspaces.data[0].tenant_id)
  }, [workspaces.data, selected])
  const create = useMutation({
    mutationFn: () => catalogApi<Workspace>(null, '/local-workspaces', { method: 'POST', body: jsonBody({ name, slug }) }),
    onSuccess: async workspace => { await client.invalidateQueries({ queryKey: ['catalog-local-workspaces'] }); choose(workspace.tenant_id); setName(''); setSlug(''); setShowCreate(false) },
  })
  const submit = (event: FormEvent) => { event.preventDefault(); create.mutate() }
  const workspace = workspaces.data?.find(value => value.tenant_id === selected)
  return <div className="app-shell">
    <aside className="sidebar"><span className="brand">CIMS</span><p className="muted">Local business workspace</p><nav aria-label="Workspace navigation"><a href="#catalog" aria-current="page">Workspace</a></nav><div id="catalog-sidebar-navigation" /><div className="sidebar-user"><strong>Local workspace</strong><span>Sign-in deferred on this computer</span></div></aside>
    <main className="content" id="catalog">
      {status.isLoading && <p role="status">Checking the catalog workspace…</p>}
      {status.isError && (selected ? <><p className="muted" role="status">The server is unavailable. Mobile drafts saved on this device are still accessible.</p><MobileWorkspace workspaceId={selected} actorId="local" canWrite /></> : <Failure error={status.error} />)}
      {status.isSuccess && !status.data.ready && <div className="card form-card"><h1>Finish catalog setup</h1><p>The catalog database fields are not available yet. Apply the supplied catalog SQL to this PostgreSQL database, then check again.</p><button className="button primary" onClick={() => void status.refetch()}>Check setup again</button></div>}
      {status.data?.ready && <>
        <div className="catalog-workspace-bar">
          <label>Business workspace<select value={selected} onChange={event => choose(event.target.value)} disabled={workspaces.isLoading}><option value="">Choose a workspace</option>{workspaces.data?.map(value => <option key={value.tenant_id} value={value.tenant_id}>{value.name}</option>)}</select></label>
          <button className="button secondary" onClick={() => setShowCreate(value => !value)}>New workspace</button>
        </div>
        {workspaces.isError && <Failure error={workspaces.error} />}
        {(showCreate || workspaces.data?.length === 0) && <section className="card form-card"><h2>Create a local business workspace</h2><p className="muted">This creates the business record for your catalog. No account or password is required in local mode.</p>{create.isError && <Failure error={create.error} />}<form className="form-grid" onSubmit={submit}><label>Business name<input required maxLength={200} value={name} onChange={event => setName(event.target.value)} /></label><label>Business identifier<input required pattern="[a-z0-9]+(-[a-z0-9]+)*" minLength={3} maxLength={63} placeholder="acme-traders" value={slug} onChange={event => setSlug(event.target.value)} /></label><button className="button primary" disabled={create.isPending}>{create.isPending ? 'Creating…' : 'Create workspace'}</button></form></section>}
        {workspace && <CatalogView key={workspace.tenant_id} workspaceId={workspace.tenant_id} />}
      </>}
    </main>
  </div>
}

type CatalogSection = 'start' | 'items' | 'setup' | 'import' | 'stock' | 'suppliers' | 'customers' | 'sales' | 'transfers' | 'recovery' | 'pos' | 'mobile' | 'reports'
const mainSections = ['start', 'items', 'stock', 'suppliers', 'customers', 'sales'] as const
const moreSections = ['setup', 'import', 'transfers', 'recovery', 'pos', 'mobile', 'reports'] as const
const sectionLabels: Record<CatalogSection, string> = {
  start: 'Get started', items: 'Items', setup: 'Units & variants', import: 'CSV import',
  stock: 'Stock overview', suppliers: 'Purchasing', customers: 'Customers', sales: 'Sales orders',
  transfers: 'Transfers', recovery: 'Returns & counts', pos: 'POS carts', mobile: 'Mobile', reports: 'Reports',
}

export function CatalogView({ workspaceId, actorId = 'local' }: { workspaceId: string; actorId?: string }) {
  const [tab, setTab] = useState<CatalogSection>('start')
  const [moreOpen, setMoreOpen] = useState(false)
  const [stockInitialView, setStockInitialView] = useState<'balances' | 'opening'>('balances')
  const openSection = (section: CatalogSection, stockView: 'balances' | 'opening' = 'balances') => {
    setTab(section)
    if (section === 'stock') setStockInitialView(stockView)
    if ((moreSections as readonly CatalogSection[]).includes(section)) setMoreOpen(true)
  }
  const headings = {
    start: ['WELCOME', 'Get started', 'Follow one clear path from setup to your first stock balance.'],
    items: ['PRODUCTS', 'Item catalog', 'Define your items, units, variants and virtual kits.'],
    setup: ['CATALOG SETUP', 'Units & variants', 'Set up the building blocks used by your items.'],
    import: ['CATALOG DATA', 'Import items', 'Preview and validate items before adding them.'],
    stock: ['INVENTORY', 'Stock overview', 'Review balances, movements and opening stock.'],
    suppliers: ['PROCUREMENT', 'Purchasing', 'Manage suppliers, reorder decisions, purchase orders and receiving.'],
    customers: ['SALES', 'Customers', 'Set up buyers, addresses and credit terms.'],
    sales: ['SALES', 'Sales orders', 'Draft, confirm and reserve customer orders.'],
    transfers: ['INVENTORY', 'Location transfers', 'Reserve, dispatch and receive stock between locations.'],
    recovery: ['INVENTORY', 'Returns & counts', 'Receive returned goods and reconcile physical stock.'],
    pos: ['RETAIL', 'POS carts', 'Scan items and prepare walk-in sale drafts.'],
    mobile: ['FIELD WORK', 'Mobile workspace', 'Capture warehouse work offline and synchronize it later.'],
    reports: ['OPERATIONS', 'Reports', 'Review business activity and stock exceptions.'],
  } as const
  const options = useQuery({ queryKey: ['catalog', workspaceId, 'options'], queryFn: ({ signal }) => catalogApi<CatalogOptions>(workspaceId, '/options', { signal }) })
  if (options.isLoading) return <p role="status">Loading your catalog…</p>
  if (options.isError) return <><p className="muted" role="status">The server is unavailable. Open the mobile workspace to review saved actions.</p><MobileWorkspace workspaceId={workspaceId} actorId={actorId} canWrite /></>
  if (!options.data) return null
  const navButton = (section: CatalogSection) => <button key={section} type="button" role="tab" aria-selected={tab === section} className={`button ${tab === section ? 'primary' : 'secondary'}`} onClick={() => openSection(section)}>{sectionLabels[section]}</button>
  const navigation = <nav className="catalog-navigation" aria-label="Catalog sections">
    <div className="catalog-tabs" role="tablist" aria-label="Main sections">{mainSections.map(navButton)}</div>
    <details className="catalog-more" open={moreOpen} onToggle={event => setMoreOpen(event.currentTarget.open)}>
      <summary>More tools</summary>
      <div className="catalog-tabs" role="tablist" aria-label="More sections">{moreSections.filter(section => section !== 'import' || options.data.can_write).map(navButton)}</div>
    </details>
  </nav>
  const sidebarNavigation = document.getElementById('catalog-sidebar-navigation')
  return <section className="page">
    {sidebarNavigation ? createPortal(navigation, sidebarNavigation) : navigation}
    <div className="page-heading"><div><p className="eyebrow">{headings[tab][0]}</p><h1>{headings[tab][1]}</h1><p className="muted">{headings[tab][2]}</p></div><span className="pill">INR</span></div>
    {tab === 'start' && <GetStartedView workspaceId={workspaceId} options={options.data} onOpen={openSection} />}
    {tab === 'items' && <ItemsView workspaceId={workspaceId} options={options.data} />}
    {tab === 'setup' && <SetupView workspaceId={workspaceId} options={options.data} />}
    {tab === 'import' && options.data.can_write && <ImportView workspaceId={workspaceId} />}
    {tab === 'stock' && <InventoryView workspaceId={workspaceId} initialView={stockInitialView} />}
    {tab === 'suppliers' && <ProcurementView workspaceId={workspaceId} canWrite={options.data.can_write} />}
    {tab === 'customers' && <CustomersView workspaceId={workspaceId} canWrite={options.data.can_write} />}
    {tab === 'sales' && <SalesWorkspace workspaceId={workspaceId} canWrite={options.data.can_write} />}
    {tab === 'transfers' && <TransfersWorkspace workspaceId={workspaceId} canWrite={options.data.can_write} />}
    {tab === 'recovery' && <RecoveryWorkspace workspaceId={workspaceId} canWrite={options.data.can_write} />}
    {tab === 'pos' && <PosWorkspace workspaceId={workspaceId} canWrite={options.data.can_write} />}
    {tab === 'mobile' && <MobileWorkspace workspaceId={workspaceId} actorId={actorId} canWrite={options.data.can_write} />}
    {tab === 'reports' && <ReportsWorkspace workspaceId={workspaceId} />}
  </section>
}

function ItemsView({ workspaceId, options }: { workspaceId: string; options: CatalogOptions }) {
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState('')
  const [page, setPage] = useState(1)
  const [editing, setEditing] = useState<{ id: string | null } | null>(null)
  const items = useQuery({ queryKey: ['catalog', workspaceId, 'items', search, status, page], queryFn: ({ signal }) => catalogApi<Page<ItemSummary>>(workspaceId, `/items?page=${page}&page_size=25&search=${encodeURIComponent(search)}${status ? `&status=${status}` : ''}`, { signal }) })
  return <>
    <div className="catalog-toolbar"><label>Search catalog<input type="search" placeholder="Name, SKU or barcode" value={search} onChange={event => { setSearch(event.target.value); setPage(1) }} /></label><label>Status<select value={status} onChange={event => { setStatus(event.target.value); setPage(1) }}><option value="">All statuses</option><option value="ACTIVE">Active</option><option value="INACTIVE">Inactive</option><option value="DRAFT">Draft</option></select></label>{options.can_write && <button className="button primary" onClick={() => setEditing({ id: null })}>New item</button>}</div>
    {editing && <ItemEditor key={editing.id ?? 'new'} workspaceId={workspaceId} options={options} itemId={editing.id} onClose={() => setEditing(null)} />}
    {items.isLoading ? <p role="status">Loading items…</p> : items.isError ? <Failure error={items.error} /> : items.data?.items.length === 0 ? <div className="card empty-state"><h2>{search || status ? 'No matching items' : 'Your catalog is empty'}</h2><p>{search || status ? 'Try a different search or status.' : 'Create a stock unit and item type in Units & variants, then add your first item.'}</p></div> : <div className="card table-card"><div className="table-wrap"><table><thead><tr><th>Item</th><th>SKU</th><th>Kind</th><th>Unit</th><th>Selling price</th><th>Status</th><th>Action</th></tr></thead><tbody>{items.data?.items.map(item => <tr key={item.item_id}><td><strong>{item.name}</strong><small>{item.barcode}</small></td><td>{item.sku}</td><td>{kindLabel(item.item_kind)}</td><td>{options.units.find(unit => unit.uom_id === item.stock_uom_id)?.code ?? '—'}</td><td>{formatMoney(item.selling_price)}</td><td><span className={`status ${item.status === 'ACTIVE' ? 'active' : ''}`}>{item.status}</span></td><td><button className="button small secondary" onClick={() => setEditing({ id: item.item_id })}>{options.can_write ? 'Edit' : 'View'}</button></td></tr>)}</tbody></table></div><div className="pagination"><button className="button small secondary" disabled={page === 1} onClick={() => setPage(value => value - 1)}>Previous</button><span>Page {page} · {items.data?.total ?? 0} items</span><button className="button small secondary" disabled={page * 25 >= (items.data?.total ?? 0)} onClick={() => setPage(value => value + 1)}>Next</button></div></div>}
  </>
}

function kindLabel(kind: string) { return kind === 'COMPOSITE' ? 'Virtual kit' : kind === 'VARIANT' ? 'Variant' : 'Simple item' }

function ItemEditor({ workspaceId, options, itemId, onClose }: { workspaceId: string; options: CatalogOptions; itemId: string | null; onClose: () => void }) {
  const detail = useQuery({ queryKey: ['catalog', workspaceId, 'detail', itemId], queryFn: ({ signal }) => catalogApi<CatalogItem>(workspaceId, `/items/${itemId}`, { signal }), enabled: !!itemId })
  if (itemId && detail.isLoading) return <p role="status">Loading item details…</p>
  if (itemId && detail.isError) return <><Failure error={detail.error} /><button className="button secondary" onClick={onClose}>Close</button></>
  return <ItemForm key={`${itemId ?? 'new'}:${detail.data?.catalog_version ?? 0}`} workspaceId={workspaceId} options={options} initial={detail.data ?? null} onClose={onClose} />
}

type ItemDraft = Omit<ItemInput, 'conversions' | 'components' | 'attributes'> & {
  conversions: { from_uom_id: string; factor: string }[];
  components: { component_item_id: string; quantity: string; uom_id: string }[];
  attributes: Record<string, string>;
}

function blankItem(options: CatalogOptions): ItemDraft {
  return { sku: '', name: '', description: '', item_kind: 'STANDALONE', item_group_id: null,
    item_type_id: options.item_types[0]?.item_type_id ?? '', barcode: null, stock_uom_id: options.units.find(unit => unit.is_active)?.uom_id ?? '',
    purchase_uom_id: null, sales_uom_id: null, cost_price: '0', selling_price: '0', gst_rate: '0', safety_stock: '0',
    is_tax_exempt: false, is_returnable: true, status: 'ACTIVE', valuation_method: null, hsn_sac_id: null, cost_center_id: null,
    conversions: [], components: [], attributes: {} }
}

function ItemForm({ workspaceId, options, initial, onClose }: { workspaceId: string; options: CatalogOptions; initial: CatalogItem | null; onClose: () => void }) {
  const client = useQueryClient()
  const [form, setForm] = useState<ItemDraft>(() => initial ? {
    ...initial, conversions: (initial.conversions ?? []).map(value => ({ ...value, factor: String(value.factor) })),
    components: (initial.components ?? []).map(value => ({ ...value, quantity: String(value.quantity) })), attributes: initial.attributes ?? {},
  } : blankItem(options))
  const [kitSearch, setKitSearch] = useState('')
  const [componentNames, setComponentNames] = useState<Record<string, string>>({})
  const [componentError, setComponentError] = useState<Error | null>(null)
  const [addingComponent, setAddingComponent] = useState(false)
  const [conversionQuantity, setConversionQuantity] = useState('10')
  const [conversionUnit, setConversionUnit] = useState('')
  const available = useQuery({ queryKey: ['catalog', workspaceId, 'kit-choices', kitSearch], queryFn: ({ signal }) => catalogApi<Page<ItemSummary>>(workspaceId, `/items?page_size=25&status=ACTIVE&search=${encodeURIComponent(kitSearch)}`, { signal }), enabled: form.item_kind === 'COMPOSITE' && options.can_write })
  const save = useMutation({
    mutationFn: () => {
      // Only write fields belong in the body; server versions/timestamps are not editable.
      const { item_id: _id, catalog_version: _version, created_at: _created, updated_at: _updated, valuation_locked_at: _locked, ...values } = form as ItemDraft & Partial<CatalogItem>
      return catalogApi<CatalogItem>(workspaceId, initial ? `/items/${initial.item_id}` : '/items', { method: initial ? 'PATCH' : 'POST', body: jsonBody({ ...values, ...(initial ? { expected_version: initial.catalog_version } : {}) }) })
    },
    onSuccess: async () => { await client.invalidateQueries({ queryKey: ['catalog', workspaceId] }); onClose() },
  })
  const calculate = useMutation({ mutationFn: () => catalogApi<ConversionResult>(workspaceId, '/convert', { method: 'POST', body: jsonBody({ item_id: initial!.item_id, from_uom_id: conversionUnit || initial!.stock_uom_id, quantity: conversionQuantity }) }) })
  const set = <K extends keyof ItemDraft>(key: K, value: ItemDraft[K]) => { setForm(current => ({ ...current, [key]: value })); save.reset() }
  const selectUnit = (kind: 'purchase_uom_id' | 'sales_uom_id', id: string) => setForm(current => {
    const next = { ...current, [kind]: id || null }
    const wanted = new Set([next.purchase_uom_id, next.sales_uom_id].filter(value => value && value !== next.stock_uom_id))
    next.conversions = [...wanted].map(unitId => current.conversions.find(value => value.from_uom_id === unitId) ?? { from_uom_id: unitId!, factor: '' })
    return next
  })
  const changeKind = (kind: ItemInput['item_kind']) => setForm(current => ({ ...current, item_kind: kind, item_group_id: null, attributes: {}, components: [] }))
  const changeGroup = (id: string) => { const group = options.groups.find(value => value.item_group_id === id); setForm(current => ({ ...current, item_group_id: id || null, attributes: Object.fromEntries((group?.attributes ?? []).map(name => [name, ''])) })) }
  const addComponent = async (item: ItemSummary) => {
    setComponentError(null); setAddingComponent(true)
    try { const detail = await catalogApi<CatalogItem>(workspaceId, `/items/${item.item_id}`); setForm(current => current.components.some(value => value.component_item_id === item.item_id) ? current : { ...current, components: [...current.components, { component_item_id: item.item_id, quantity: '1', uom_id: detail.stock_uom_id }] }); setComponentNames(current => ({ ...current, [item.item_id]: `${item.sku} · ${item.name}` })) }
    catch (error) { setComponentError(error instanceof Error ? error : new Error('Unable to add component')) }
    finally { setAddingComponent(false) }
  }
  const submit = (event: FormEvent) => { event.preventDefault(); save.mutate() }
  const disabled = !options.can_write || save.isPending
  const activeUnits = options.units.filter(unit => unit.is_active)
  return <section className="card form-card catalog-editor">
    <div className="page-heading"><h2>{initial ? `${options.can_write ? 'Edit' : 'View'} ${initial.sku}` : 'Create an item'}</h2><button className="button small secondary" onClick={onClose} disabled={save.isPending}>Close</button></div>
    {save.isError && <Failure error={save.error} />}
    <form className="form-grid" onSubmit={submit}>
      <label>Item name<input required maxLength={200} value={form.name} onChange={event => set('name', event.target.value)} disabled={disabled} /></label>
      <label>SKU<input required maxLength={100} value={form.sku} onChange={event => set('sku', event.target.value)} disabled={disabled} /></label>
      <label>Item kind<select value={form.item_kind} onChange={event => changeKind(event.target.value as ItemInput['item_kind'])} disabled={disabled || !!initial}><option value="STANDALONE">Simple item</option><option value="VARIANT">Variant</option><option value="COMPOSITE">Virtual kit</option></select></label>
      <label>Classification<select required value={form.item_type_id} onChange={event => set('item_type_id', event.target.value)} disabled={disabled}><option value="">Choose an item type</option>{options.item_types.map(type => <option key={type.item_type_id} value={type.item_type_id}>{type.name}</option>)}</select></label>
      <label>Barcode<input maxLength={100} value={form.barcode ?? ''} onChange={event => set('barcode', event.target.value || null)} disabled={disabled} /></label>
      <label>Status<select value={form.status} onChange={event => set('status', event.target.value as ItemInput['status'])} disabled={disabled}><option value="ACTIVE">Active</option><option value="INACTIVE">Inactive</option><option value="DRAFT">Draft</option></select></label>
      <label className="wide">Description<textarea maxLength={4000} value={form.description ?? ''} onChange={event => set('description', event.target.value)} disabled={disabled} /></label>
      {form.item_kind === 'VARIANT' && <fieldset className="wide catalog-subform"><legend>Variant details</legend><label>Variant group<select required value={form.item_group_id ?? ''} onChange={event => changeGroup(event.target.value)} disabled={disabled}><option value="">Choose a group</option>{options.groups.filter(group => group.is_active).map(group => <option key={group.item_group_id} value={group.item_group_id}>{group.name}</option>)}</select></label>{Object.entries(form.attributes).map(([name, value]) => <label key={name}>{name}<input required maxLength={200} value={value} onChange={event => set('attributes', { ...form.attributes, [name]: event.target.value })} disabled={disabled} /></label>)}</fieldset>}
      <fieldset className="wide catalog-subform"><legend>Units and conversions</legend>
        <label>Stock unit<select required value={form.stock_uom_id} onChange={event => { setForm(current => ({ ...current, stock_uom_id: event.target.value, purchase_uom_id: null, sales_uom_id: null, conversions: [] })) }} disabled={disabled || !!initial?.valuation_locked_at}><option value="">Choose a stock unit</option>{activeUnits.map(unit => <option key={unit.uom_id} value={unit.uom_id}>{unit.code} · {unit.name}</option>)}</select></label>
        <label>Purchase unit<select value={form.purchase_uom_id ?? ''} onChange={event => selectUnit('purchase_uom_id', event.target.value)} disabled={disabled}><option value="">Same as stock unit</option>{activeUnits.map(unit => <option key={unit.uom_id} value={unit.uom_id}>{unit.code}</option>)}</select></label>
        <label>Sales unit<select value={form.sales_uom_id ?? ''} onChange={event => selectUnit('sales_uom_id', event.target.value)} disabled={disabled}><option value="">Same as stock unit</option>{activeUnits.map(unit => <option key={unit.uom_id} value={unit.uom_id}>{unit.code}</option>)}</select></label>
        {form.conversions.map((entry, index) => <label key={entry.from_uom_id}>Stock units in one {options.units.find(unit => unit.uom_id === entry.from_uom_id)?.code ?? 'purchase/sales unit'}<input required type="number" min="0.0000000001" step="any" value={entry.factor} onChange={event => set('conversions', form.conversions.map((value, position) => position === index ? { ...value, factor: event.target.value } : value))} disabled={disabled} /></label>)}
      </fieldset>
      <label>Cost price (INR)<input required type="number" min="0" step="0.000001" value={String(form.cost_price)} onChange={event => set('cost_price', event.target.value)} disabled={disabled} /></label>
      <label>Selling price (INR)<input required type="number" min="0" step="0.000001" value={String(form.selling_price)} onChange={event => set('selling_price', event.target.value)} disabled={disabled} /></label>
      <label>Tax classification<select value={form.hsn_sac_id ?? ''} onChange={event => set('hsn_sac_id', event.target.value || null)} disabled={disabled}><option value="">No HSN/SAC selected</option>{options.hsn_codes.map(code => <option key={code.hsn_sac_id} value={code.hsn_sac_id}>{code.code} · {code.description}</option>)}</select></label>
      <label>GST rate (%)<input required type="number" min="0" max="100" step="0.0001" value={String(form.gst_rate)} onChange={event => set('gst_rate', event.target.value)} disabled={disabled || !!form.is_tax_exempt} /></label>
      <label>Safety stock threshold<input required type="number" min="0" step="0.0001" value={String(form.safety_stock)} onChange={event => set('safety_stock', event.target.value)} disabled={disabled} /></label>
      <label>Valuation method<select value={form.valuation_method ?? ''} onChange={event => set('valuation_method', (event.target.value || null) as ItemInput['valuation_method'])} disabled={disabled || !!initial?.valuation_locked_at}><option value="">Use group/business default ({options.default_valuation_method === 'FIFO' ? 'FIFO' : 'Weighted average'})</option><option value="WEIGHTED_AVERAGE">Weighted average</option><option value="FIFO">FIFO</option></select></label>
      <label className="checkbox"><input type="checkbox" checked={!!form.is_returnable} onChange={event => set('is_returnable', event.target.checked)} disabled={disabled} />Returnable</label>
      <label className="checkbox"><input type="checkbox" checked={!!form.is_tax_exempt} onChange={event => setForm(current => ({ ...current, is_tax_exempt: event.target.checked, gst_rate: event.target.checked ? '0' : current.gst_rate }))} disabled={disabled} />Tax exempt</label>
      {form.item_kind === 'COMPOSITE' && <fieldset className="wide catalog-subform"><legend>Virtual kit components</legend><p className="muted wide">A virtual kit groups component items. Creating it does not assemble or move stock.</p>
        {componentError && <Failure error={componentError} />}
        {form.components.map((entry, index) => <div key={entry.component_item_id} className="catalog-component"><strong>{componentNames[entry.component_item_id] ?? entry.component_item_id}</strong><label>Component quantity<input required type="number" min="0.0001" step="0.0001" value={entry.quantity} onChange={event => set('components', form.components.map((value, position) => index === position ? { ...value, quantity: event.target.value } : value))} disabled={disabled} /></label><span>{options.units.find(unit => unit.uom_id === entry.uom_id)?.code ?? 'Stock unit'}</span>{options.can_write && <button type="button" className="button small secondary" onClick={() => set('components', form.components.filter((_, position) => position !== index))} disabled={disabled}>Remove</button>}</div>)}
        {options.can_write && <><label className="wide">Find a component<input type="search" value={kitSearch} onChange={event => setKitSearch(event.target.value)} placeholder="Search item name or SKU" disabled={disabled} /></label>{available.isError && <Failure error={available.error} />}{available.isLoading && <p role="status">Loading components…</p>}<div className="catalog-choice-list wide">{available.data?.items.filter(item => item.item_id !== initial?.item_id && !form.components.some(entry => entry.component_item_id === item.item_id)).map(item => <button type="button" className="button small secondary" key={item.item_id} disabled={disabled || addingComponent} onClick={() => void addComponent(item)}>Add {item.sku} · {item.name}</button>)}</div></>}
      </fieldset>}
      {options.can_write && <div className="actions wide"><button className="button primary" disabled={disabled || !form.stock_uom_id || !form.item_type_id || addingComponent}>{save.isPending ? 'Saving…' : initial ? 'Save item' : 'Create item'}</button><button type="button" className="button secondary" onClick={onClose} disabled={save.isPending}>Cancel</button></div>}
    </form>
    {initial && <div className="catalog-converter"><h3>Check a unit conversion</h3><div className="catalog-toolbar"><label>Quantity<input type="number" min="0.0000000001" step="any" value={conversionQuantity} onChange={event => { setConversionQuantity(event.target.value); calculate.reset() }} /></label><label>From unit<select value={conversionUnit || initial.stock_uom_id} onChange={event => { setConversionUnit(event.target.value); calculate.reset() }}>{[initial.stock_uom_id, ...(initial.conversions ?? []).map(value => value.from_uom_id)].map(id => <option key={id} value={id}>{options.units.find(unit => unit.uom_id === id)?.code ?? 'Unit'}</option>)}</select></label><button className="button secondary" disabled={calculate.isPending} onClick={() => calculate.mutate()}>{calculate.isPending ? 'Calculating…' : 'Convert'}</button></div>{calculate.isError && <Failure error={calculate.error} />}{calculate.data && <p role="status">{calculate.data.stock_quantity} {options.units.find(unit => unit.uom_id === calculate.data.stock_uom_id)?.code}</p>}</div>}
  </section>
}

function SetupView({ workspaceId, options }: { workspaceId: string; options: CatalogOptions }) {
  const client = useQueryClient()
  const [unit, setUnit] = useState({ code: '', name: '', uom_class: 'COUNT', decimal_places: '0' })
  const [type, setType] = useState({ code: '', name: '', is_stockable: true })
  const [group, setGroup] = useState({ code: '', name: '', attributes: 'Size, Colour' })
  const save = useMutation({ mutationFn: ({ path, body }: { path: string; body: unknown }) => catalogApi(workspaceId, path, { method: 'POST', body: jsonBody(body) }), onSuccess: async () => { await client.invalidateQueries({ queryKey: ['catalog', workspaceId] }); setUnit({ code: '', name: '', uom_class: 'COUNT', decimal_places: '0' }); setType({ code: '', name: '', is_stockable: true }); setGroup({ code: '', name: '', attributes: 'Size, Colour' }) } })
  return <div className="catalog-setup">
    {save.isError && <Failure error={save.error} />}{save.isSuccess && <p className="notice success" role="status">Catalog setup saved.</p>}
    <section className="card form-card"><h2>Units</h2><p className="muted">For example: PCS for pieces and CTN for cartons. Set how many decimal places each unit permits.</p><div className="catalog-tags">{options.units.map(value => <span className="pill" key={value.uom_id}>{value.code} · {value.name} · {value.decimal_places} decimals</span>)}</div>{options.can_write && <form className="form-grid" onSubmit={event => { event.preventDefault(); save.mutate({ path: '/units', body: { ...unit, decimal_places: Number(unit.decimal_places), is_active: true } }) }}><label>Unit code<input required value={unit.code} onChange={event => setUnit({ ...unit, code: event.target.value })} /></label><label>Unit name<input required value={unit.name} onChange={event => setUnit({ ...unit, name: event.target.value })} /></label><label>Unit class<select value={unit.uom_class} onChange={event => setUnit({ ...unit, uom_class: event.target.value })}>{['COUNT', 'MASS', 'VOLUME', 'LENGTH', 'AREA', 'TIME'].map(value => <option key={value}>{value}</option>)}</select></label><label>Decimal places<select value={unit.decimal_places} onChange={event => setUnit({ ...unit, decimal_places: event.target.value })}>{['0', '1', '2', '3', '4'].map(value => <option key={value}>{value}</option>)}</select></label><button className="button primary" disabled={save.isPending}>Add unit</button></form>}</section>
    <section className="card form-card"><h2>Item classifications</h2><div className="catalog-tags">{options.item_types.map(value => <span className="pill" key={value.item_type_id}>{value.code} · {value.name}</span>)}</div>{options.can_write && <form className="form-grid" onSubmit={event => { event.preventDefault(); save.mutate({ path: '/item-types', body: type }) }}><label>Classification code<input required placeholder="GOODS" value={type.code} onChange={event => setType({ ...type, code: event.target.value })} /></label><label>Classification name<input required placeholder="Stockable goods" value={type.name} onChange={event => setType({ ...type, name: event.target.value })} /></label><label className="checkbox"><input type="checkbox" checked={type.is_stockable} onChange={event => setType({ ...type, is_stockable: event.target.checked })} />Stockable item type</label><button className="button primary" disabled={save.isPending}>Add classification</button></form>}</section>
    <section className="card form-card"><h2>Variant groups</h2><p className="muted">Create a group such as Soap bars, then give each size or colour its own item and SKU.</p><div className="catalog-tags">{options.groups.map(value => <span className="pill" key={value.item_group_id}>{value.name} · {value.attributes.join(', ')}</span>)}</div>{options.can_write && <form className="form-grid" onSubmit={event => { event.preventDefault(); save.mutate({ path: '/groups', body: { code: group.code, name: group.name, attributes: group.attributes.split(',').map(value => value.trim()).filter(Boolean) } }) }}><label>Group code<input required value={group.code} onChange={event => setGroup({ ...group, code: event.target.value })} /></label><label>Group name<input required value={group.name} onChange={event => setGroup({ ...group, name: event.target.value })} /></label><label className="wide">Variant attributes, separated by commas<input required value={group.attributes} onChange={event => setGroup({ ...group, attributes: event.target.value })} /></label><button className="button primary" disabled={save.isPending}>Add variant group</button></form>}</section>
  </div>
}

function ImportView({ workspaceId }: { workspaceId: string }) {
  const client = useQueryClient()
  const [csv, setCsv] = useState('')
  const [fileName, setFileName] = useState('')
  const [fileError, setFileError] = useState<Error | null>(null)
  const fileSequence = useRef(0)
  useEffect(() => () => { fileSequence.current += 1 }, [])
  const validate = useMutation({ mutationFn: () => catalogApi<ImportPreview>(workspaceId, '/imports/validate', { method: 'POST', body: jsonBody({ csv_text: csv }) }) })
  const commit = useMutation({ mutationFn: () => catalogApi<{ created: number }>(workspaceId, '/imports/commit', { method: 'POST', body: jsonBody({ csv_text: csv }) }), onSuccess: async () => { setCsv(''); validate.reset(); await client.invalidateQueries({ queryKey: ['catalog', workspaceId] }) } })
  const template = useMutation({ mutationFn: () => downloadTemplate(workspaceId) })
  const readFile = async (file: File | undefined) => {
    const sequence = ++fileSequence.current
    setFileError(null); setCsv(''); validate.reset(); commit.reset()
    if (!file) { setFileName(''); return }
    setFileName(file.name)
    if (file.size > 512_000) { setFileError(new Error('Choose a CSV smaller than 512 KB')); return }
    try { const text = await file.text(); if (sequence === fileSequence.current) setCsv(text) } catch { if (sequence === fileSequence.current) setFileError(new Error('The selected file could not be read')) }
  }
  return <section className="card form-card"><h2>Import simple items</h2><p className="muted">Use existing unit and classification codes. Validate up to 500 rows, review every error, then import the whole file. Create variants and kits in the item editor.</p><div className="actions"><button className="button secondary" disabled={template.isPending} onClick={() => template.mutate()}>Download CSV template</button></div>{template.isError && <Failure error={template.error} />}<label className="catalog-file">Choose an item CSV<input type="file" accept=".csv,text/csv" disabled={validate.isPending || commit.isPending} onChange={event => void readFile(event.target.files?.[0])} /></label>{fileName && <p className="muted">{fileName}</p>}{fileError && <Failure error={fileError} />}{validate.isError && <Failure error={validate.error} />}{commit.isError && <Failure error={commit.error} />}{commit.isSuccess && <p className="notice success" role="status">Imported {commit.data.created} items.</p>}<div className="actions"><button className="button secondary" disabled={!csv || validate.isPending || commit.isPending} onClick={() => { commit.reset(); validate.mutate() }}>{validate.isPending ? 'Validating…' : 'Validate CSV'}</button>{validate.data?.can_import && <button className="button primary" disabled={commit.isPending} onClick={() => commit.mutate()}>{commit.isPending ? 'Importing…' : `Import ${validate.data.row_count} items`}</button>}</div>
    {validate.data && <div className="catalog-import-result"><p role="status">{validate.data.valid_count} of {validate.data.row_count} rows passed validation.</p>{validate.data.issues.length > 0 && <div className="table-wrap"><table><thead><tr><th>Row</th><th>Field</th><th>Issue</th></tr></thead><tbody>{validate.data.issues.map((issue, index) => <tr key={`${issue.row}:${index}`}><td>{issue.row}</td><td>{issue.field}</td><td>{issue.message}</td></tr>)}</tbody></table></div>}{validate.data.can_import && <div className="table-wrap"><table><thead><tr><th>SKU</th><th>Name</th><th>Stock unit</th><th>Classification</th></tr></thead><tbody>{validate.data.rows.slice(0, 25).map((row, index) => <tr key={index}><td>{row.sku}</td><td>{row.name}</td><td>{row.stock_unit}</td><td>{row.item_type}</td></tr>)}</tbody></table>{validate.data.rows.length > 25 && <p className="muted">Preview shows the first 25 rows. The import includes all validated rows.</p>}</div>}</div>}
  </section>
}
