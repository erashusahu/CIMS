import { useRef, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { components } from '../../api/openapi'
import { getAccessToken, jsonBody } from '../../api/client'
import { catalogApi, formatMoney, localCatalogMode, workspaceApi, type ItemSummary, type Page } from '../catalog/api'
import { PurchaseWorkspace, ReceivingWorkspace, ReorderWorkspace, type SuggestedOrder } from './workflow'

type Supplier = components['schemas']['SupplierOut']
type SupplierIn = components['schemas']['SupplierIn']
type SupplierImport = components['schemas']['SupplierImportPreview']
type Preferred = components['schemas']['PreferredSupplierOut']

function Failure({ error }: { error: unknown }) {
  return <p className="error" role="alert">{error instanceof Error ? error.message : 'The request could not be completed'}</p>
}

const emptySupplier: SupplierIn = {
  code: '', name: '', legal_name: null, gstin: null, email: null, phone: null,
  tax_treatment: 'REGISTERED', payment_terms_days: 0, lead_time_days: null,
  is_active: true, notes: null,
}

function supplierFields(value: Supplier): SupplierIn {
  return {
    code: value.code, name: value.name, legal_name: value.legal_name, gstin: value.gstin,
    email: value.email, phone: value.phone, tax_treatment: value.tax_treatment as SupplierIn['tax_treatment'],
    payment_terms_days: value.payment_terms_days, lead_time_days: value.lead_time_days,
    is_active: value.is_active, notes: value.notes,
  }
}

export function ProcurementView({ workspaceId, canWrite }: { workspaceId: string; canWrite: boolean }) {
  const [tab, setTab] = useState<'suppliers' | 'preferred' | 'import' | 'reorder' | 'orders' | 'receiving'>('suppliers')
  const [suggestedOrder, setSuggestedOrder] = useState<SuggestedOrder | null>(null)
  return <section className="procurement-view">
    <p className="muted">Keep supplier details and preferred item sources together.</p>
    <div className="catalog-tabs" role="tablist" aria-label="Procurement sections">
      <button role="tab" aria-selected={tab === 'suppliers'} className="button secondary" onClick={() => setTab('suppliers')}>Suppliers</button>
      <button role="tab" aria-selected={tab === 'preferred'} className="button secondary" onClick={() => setTab('preferred')}>Preferred sources</button>
      <button role="tab" aria-selected={tab === 'reorder'} className="button secondary" onClick={() => setTab('reorder')}>Reorder</button>
      <button role="tab" aria-selected={tab === 'orders'} className="button secondary" onClick={() => setTab('orders')}>Purchase orders</button>
      <button role="tab" aria-selected={tab === 'receiving'} className="button secondary" onClick={() => setTab('receiving')}>Receiving</button>
      {canWrite && <button role="tab" aria-selected={tab === 'import'} className="button secondary" onClick={() => setTab('import')}>Supplier CSV</button>}
    </div>
    {tab === 'suppliers' && <Suppliers workspaceId={workspaceId} canWrite={canWrite} />}
    {tab === 'preferred' && <PreferredSources workspaceId={workspaceId} canWrite={canWrite} />}
    {tab === 'reorder' && <ReorderWorkspace workspaceId={workspaceId} canWrite={canWrite} onOrder={value => { setSuggestedOrder(value); setTab('orders') }} />}
    {tab === 'orders' && <PurchaseWorkspace key={suggestedOrder ? `${suggestedOrder.itemId}:${suggestedOrder.locationId}` : 'manual'} workspaceId={workspaceId} canWrite={canWrite} prefill={suggestedOrder} />}
    {tab === 'receiving' && <ReceivingWorkspace workspaceId={workspaceId} canWrite={canWrite} />}
    {tab === 'import' && canWrite && <SupplierCsv workspaceId={workspaceId} />}
  </section>
}

function Suppliers({ workspaceId, canWrite }: { workspaceId: string; canWrite: boolean }) {
  const client = useQueryClient()
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [editing, setEditing] = useState<Supplier | 'new' | null>(null)
  const result = useQuery({
    queryKey: ['catalog', workspaceId, 'suppliers', page, search],
    queryFn: ({ signal }) => workspaceApi<Page<Supplier>>(workspaceId,
      `/procurement/suppliers?page=${page}&page_size=25&search=${encodeURIComponent(search)}`, { signal }),
  })
  return <>
    <div className="catalog-toolbar">
      <label>Find a supplier<input type="search" value={search} placeholder="Code or name" onChange={event => { setSearch(event.target.value); setPage(1) }} /></label>
      {canWrite && <button className="button primary" onClick={() => setEditing('new')}>New supplier</button>}
    </div>
    {editing && <SupplierForm key={editing === 'new' ? 'new' : `${editing.vendor_id}:${editing.updated_at}`}
      workspaceId={workspaceId} initial={editing === 'new' ? null : editing} canWrite={canWrite}
      onClose={() => setEditing(null)} onSaved={async () => {
        setEditing(null)
        await client.invalidateQueries({ queryKey: ['catalog', workspaceId, 'suppliers'] })
      }} />}
    {result.isLoading ? <p role="status">Loading suppliers…</p> : result.isError ? <Failure error={result.error} />
      : result.data?.items.length === 0 ? <div className="card empty-state"><h3>{search ? 'No matching suppliers' : 'No suppliers yet'}</h3><p>{search ? 'Try a different code or name.' : 'Add a supplier to begin procurement setup.'}</p></div>
        : <div className="card table-card"><div className="table-wrap"><table><thead><tr><th>Supplier</th><th>Code</th><th>Terms</th><th>Lead time</th><th>Status</th><th>Action</th></tr></thead><tbody>
          {result.data?.items.map(value => <tr key={value.vendor_id}><td><strong>{value.name}</strong><small>{value.email ?? value.phone ?? value.gstin ?? ''}</small></td><td>{value.code}</td><td>{value.payment_terms_days} days</td><td>{value.lead_time_days === null ? '—' : `${value.lead_time_days} days`}</td><td><span className={`status ${value.is_active ? 'active' : ''}`}>{value.is_active ? 'Active' : 'Inactive'}</span></td><td><button className="button small secondary" onClick={() => setEditing(value)}>{canWrite ? 'Edit' : 'View'}</button></td></tr>)}
        </tbody></table></div><div className="pagination"><button className="button small secondary" disabled={page === 1} onClick={() => setPage(value => value - 1)}>Previous</button><span>Page {page} · {result.data?.total ?? 0} suppliers</span><button className="button small secondary" disabled={page * 25 >= (result.data?.total ?? 0)} onClick={() => setPage(value => value + 1)}>Next</button></div></div>}
  </>
}

function SupplierForm({ workspaceId, initial, canWrite, onClose, onSaved }: { workspaceId: string; initial: Supplier | null; canWrite: boolean; onClose: () => void; onSaved: () => Promise<void> }) {
  const [form, setForm] = useState<SupplierIn>(() => initial ? supplierFields(initial) : emptySupplier)
  const set = <K extends keyof SupplierIn>(key: K, value: SupplierIn[K]) => setForm(current => ({ ...current, [key]: value }))
  const save = useMutation({
    mutationFn: () => workspaceApi<Supplier>(workspaceId,
      initial ? `/procurement/suppliers/${initial.vendor_id}` : '/procurement/suppliers', {
        method: initial ? 'PATCH' : 'POST',
        body: jsonBody(initial ? { ...form, expected_updated_at: initial.updated_at } : form),
      }), onSuccess: onSaved,
  })
  const submit = (event: FormEvent) => { event.preventDefault(); save.mutate() }
  const disabled = save.isPending || !canWrite
  return <section className="card form-card"><div className="page-heading"><h3>{initial ? `Supplier ${initial.code}` : 'New supplier'}</h3><button className="button small secondary" onClick={onClose}>Close</button></div>
    {save.isError && <Failure error={save.error} />}
    <form className="form-grid" onSubmit={submit}>
      <label>Supplier code<input required maxLength={100} value={form.code} disabled={disabled} onChange={event => set('code', event.target.value)} /></label>
      <label>Supplier name<input required maxLength={200} value={form.name} disabled={disabled} onChange={event => set('name', event.target.value)} /></label>
      <label>Legal name<input maxLength={200} value={form.legal_name ?? ''} disabled={disabled} onChange={event => set('legal_name', event.target.value || null)} /></label>
      <label>GSTIN<input maxLength={15} value={form.gstin ?? ''} disabled={disabled} onChange={event => set('gstin', event.target.value.toUpperCase() || null)} /></label>
      <label>Email<input type="email" maxLength={254} value={form.email ?? ''} disabled={disabled} onChange={event => set('email', event.target.value || null)} /></label>
      <label>Phone<input type="tel" maxLength={40} value={form.phone ?? ''} disabled={disabled} onChange={event => set('phone', event.target.value || null)} /></label>
      <label>Tax treatment<select value={form.tax_treatment} disabled={disabled} onChange={event => set('tax_treatment', event.target.value as SupplierIn['tax_treatment'])}>{['REGISTERED', 'UNREGISTERED', 'COMPOSITION', 'SEZ', 'EXPORT'].map(value => <option key={value}>{value}</option>)}</select></label>
      <label>Payment terms (days)<input type="number" min="0" max="3650" value={form.payment_terms_days} disabled={disabled} onChange={event => set('payment_terms_days', Number(event.target.value))} /></label>
      <label>Lead time (days)<input type="number" min="0" max="3650" value={form.lead_time_days ?? ''} disabled={disabled} onChange={event => set('lead_time_days', event.target.value === '' ? null : Number(event.target.value))} /></label>
      <label>Status<select value={form.is_active ? 'ACTIVE' : 'INACTIVE'} disabled={disabled} onChange={event => set('is_active', event.target.value === 'ACTIVE')}><option value="ACTIVE">Active</option><option value="INACTIVE">Inactive</option></select></label>
      <label className="wide">Notes<textarea maxLength={2000} value={form.notes ?? ''} disabled={disabled} onChange={event => set('notes', event.target.value || null)} /></label>
      {canWrite && <div className="actions wide"><button className="button primary" disabled={save.isPending}>{save.isPending ? 'Saving…' : 'Save supplier'}</button></div>}
    </form>
  </section>
}

function PreferredSources({ workspaceId, canWrite }: { workspaceId: string; canWrite: boolean }) {
  const client = useQueryClient()
  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [vendorSearch, setVendorSearch] = useState('')
  const [itemId, setItemId] = useState('')
  const [vendorId, setVendorId] = useState('')
  const [unitPrice, setUnitPrice] = useState('')
  const [minOrder, setMinOrder] = useState('0')
  const [leadTime, setLeadTime] = useState('')
  const vendors = useQuery({ queryKey: ['catalog', workspaceId, 'supplier-options', vendorSearch], queryFn: ({ signal }) => workspaceApi<Page<Supplier>>(workspaceId, `/procurement/suppliers?page_size=100&search=${encodeURIComponent(vendorSearch)}`, { signal }) })
  const items = useQuery({ queryKey: ['catalog', workspaceId, 'preferred-items', search], queryFn: ({ signal }) => catalogApi<Page<ItemSummary>>(workspaceId, `/items?page_size=100&status=ACTIVE&search=${encodeURIComponent(search)}`, { signal }) })
  const links = useQuery({ queryKey: ['catalog', workspaceId, 'preferred-suppliers', page], queryFn: ({ signal }) => workspaceApi<Page<Preferred>>(workspaceId, `/procurement/preferred-suppliers?page=${page}&page_size=25`, { signal }) })
  const save = useMutation({ mutationFn: () => workspaceApi<Preferred>(workspaceId, '/procurement/preferred-suppliers', {
    method: 'POST', body: jsonBody({ item_id: itemId, vendor_id: vendorId, unit_price: unitPrice || null,
      min_order_qty: minOrder, lead_time_days: leadTime ? Number(leadTime) : null }),
  }), onSuccess: async () => { await client.invalidateQueries({ queryKey: ['catalog', workspaceId, 'preferred-suppliers'] }) } })
  const submit = (event: FormEvent) => { event.preventDefault(); save.mutate() }
  return <>
    <p className="muted">A preferred source tells the purchasing team where to buy an item. It does not create an order or change stock.</p>
    {canWrite && <section className="card form-card"><h3>Set preferred supplier</h3>{save.isError && <Failure error={save.error} />}
      <form className="form-grid" onSubmit={submit}>
        <label className="wide">Find item<input type="search" value={search} placeholder="Search by SKU or name" onChange={event => setSearch(event.target.value)} /></label>
        <label className="wide">Find supplier<input type="search" value={vendorSearch} placeholder="Search by code or name" onChange={event => setVendorSearch(event.target.value)} /></label>
        <label>Catalog item<select required value={itemId} onChange={event => setItemId(event.target.value)}><option value="">Choose an item</option>{items.data?.items.filter(value => value.item_kind !== 'COMPOSITE').map(value => <option key={value.item_id} value={value.item_id}>{value.sku} · {value.name}</option>)}{itemId && !items.data?.items.some(value => value.item_id === itemId) && <option value={itemId}>Selected item</option>}</select></label>
        <label>Supplier<select required value={vendorId} onChange={event => setVendorId(event.target.value)}><option value="">Choose a supplier</option>{vendors.data?.items.filter(value => value.is_active).map(value => <option key={value.vendor_id} value={value.vendor_id}>{value.code} · {value.name}</option>)}{vendorId && !vendors.data?.items.some(value => value.vendor_id === vendorId) && <option value={vendorId}>Selected supplier</option>}</select></label>
        <label>Price per purchase unit (INR)<input type="number" min="0" step="0.000001" value={unitPrice} onChange={event => setUnitPrice(event.target.value)} /></label>
        <label>Minimum order quantity<input type="number" min="0" step="0.0001" value={minOrder} onChange={event => setMinOrder(event.target.value)} /></label>
        <label>Lead time (days)<input type="number" min="0" max="3650" value={leadTime} onChange={event => setLeadTime(event.target.value)} /></label>
        <div className="actions wide"><button className="button primary" disabled={save.isPending || vendors.isLoading || items.isLoading}>{save.isPending ? 'Saving…' : 'Save preferred supplier'}</button></div>
      </form>
    </section>}
    {vendors.isError && <Failure error={vendors.error} />}{items.isError && <Failure error={items.error} />}
    {links.isLoading ? <p role="status">Loading preferred suppliers…</p> : links.isError ? <Failure error={links.error} /> : links.data?.total === 0 ? <div className="card empty-state"><h3>No preferred sources yet</h3><p>Choose an item and active supplier to create the first source.</p></div> : <div className="card table-card"><div className="table-wrap"><table><thead><tr><th>Item</th><th>Supplier</th><th>Unit price</th><th>Minimum</th><th>Lead time</th></tr></thead><tbody>{links.data?.items.map(value => <tr key={value.item_vendor_id}><td>{value.item_sku}</td><td>{value.vendor_name}<small>{value.vendor_code}</small></td><td>{value.unit_price == null ? '—' : formatMoney(value.unit_price)}</td><td>{value.min_order_qty}</td><td>{value.lead_time_days == null ? '—' : `${value.lead_time_days} days`}</td></tr>)}</tbody></table></div><div className="pagination"><button className="button small secondary" disabled={page === 1} onClick={() => setPage(value => value-1)}>Previous</button><span>Page {page} · {links.data?.total ?? 0} items</span><button className="button small secondary" disabled={page * 25 >= (links.data?.total ?? 0)} onClick={() => setPage(value => value+1)}>Next</button></div></div>}
  </>
}

async function downloadSupplierTemplate(workspaceId: string) {
  const base = import.meta.env.VITE_API_URL ?? 'http://127.0.0.1:8000/api/v1'
  const headers = new Headers()
  if (localCatalogMode) headers.set('X-CIMS-Tenant-ID', workspaceId)
  else { const token = getAccessToken(); if (token) headers.set('Authorization', `Bearer ${token}`) }
  const response = await fetch(`${base}/procurement/suppliers/imports/template`, { headers, credentials: 'include' })
  if (!response.ok) throw new Error('The supplier template could not be downloaded')
  const url = URL.createObjectURL(await response.blob())
  const link = document.createElement('a')
  link.href = url
  link.download = 'cims-suppliers-template.csv'
  link.click()
  URL.revokeObjectURL(url)
}

function SupplierCsv({ workspaceId }: { workspaceId: string }) {
  const client = useQueryClient()
  const fileVersion = useRef(0)
  const [csv, setCsv] = useState('')
  const [fileError, setFileError] = useState<Error | null>(null)
  const [templateError, setTemplateError] = useState<Error | null>(null)
  const validate = useMutation({ mutationFn: () => workspaceApi<SupplierImport>(workspaceId, '/procurement/suppliers/imports/validate', { method: 'POST', body: jsonBody({ csv_text: csv }) }) })
  const commit = useMutation({ mutationFn: () => workspaceApi<SupplierImport>(workspaceId, '/procurement/suppliers/imports/commit', { method: 'POST', body: jsonBody({ csv_text: csv }) }), onSuccess: async () => { setCsv(''); validate.reset(); await client.invalidateQueries({ queryKey: ['catalog', workspaceId, 'suppliers'] }) } })
  const change = (value: string, fromFile = false) => { if (!fromFile) fileVersion.current += 1; setCsv(value); validate.reset(); commit.reset() }
  const readFile = async (file?: File) => {
    const version = ++fileVersion.current
    setFileError(null)
    change('', true)
    if (!file) return
    if (file.size > 512000) { setFileError(new Error('Choose a CSV smaller than 512 KB')); return }
    try { const value = await file.text(); if (version === fileVersion.current) change(value, true) }
    catch { if (version === fileVersion.current) setFileError(new Error('The CSV could not be read')) }
  }
  return <section className="card form-card"><h3>Import suppliers</h3><p>Validate a CSV before adding its suppliers. An import adds supplier records only.</p>
    <button className="button secondary" onClick={() => void downloadSupplierTemplate(workspaceId).catch(error => setTemplateError(error))}>Download template</button>
    {templateError && <Failure error={templateError} />}
    <label className="catalog-file">Supplier CSV<input type="file" accept=".csv,text/csv" onChange={event => void readFile(event.target.files?.[0])} disabled={validate.isPending || commit.isPending} /></label>
    <label>CSV contents<textarea value={csv} maxLength={512000} onChange={event => change(event.target.value)} disabled={validate.isPending || commit.isPending} placeholder={'code,name,gstin,email,phone,lead_time_days,payment_terms_days\nSUP-001,Example supplier,,orders@example.com,,7,30'} /></label>
    {fileError && <Failure error={fileError} />}{validate.isError && <Failure error={validate.error} />}{commit.isError && <Failure error={commit.error} />}
    {commit.isSuccess && <p className="notice success" role="status">Imported {commit.data.valid_count} suppliers.</p>}
    <div className="actions"><button className="button secondary" disabled={!csv || validate.isPending || commit.isPending} onClick={() => validate.mutate()}>{validate.isPending ? 'Validating…' : 'Validate CSV'}</button>{validate.data?.can_import && <button className="button primary" disabled={commit.isPending} onClick={() => commit.mutate()}>{commit.isPending ? 'Importing…' : `Import ${validate.data.valid_count} suppliers`}</button>}</div>
    {validate.data && <div className="catalog-import-result"><p role="status">{validate.data.valid_count} of {validate.data.row_count} rows passed validation.</p>{validate.data.issues.map((issue, index) => <p className="error" key={index}>Row {issue.row}: {issue.field} — {issue.message}</p>)}{validate.data.can_import && <div className="table-wrap"><table><thead><tr><th>Code</th><th>Supplier</th><th>Email</th><th>Lead time</th></tr></thead><tbody>{validate.data.rows.map((row, index) => <tr key={index}><td>{row.code}</td><td>{row.name}</td><td>{row.email ?? '—'}</td><td>{row.lead_time_days ?? '—'}</td></tr>)}</tbody></table></div>}</div>}
  </section>
}
