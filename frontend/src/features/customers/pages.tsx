import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { components } from '../../api/openapi'
import { jsonBody } from '../../api/client'
import { formatMoney, workspaceApi, type Page } from '../catalog/api'

type Customer = components['schemas']['CustomerOut']
type Address = components['schemas']['AddressOut']
type CustomerImport = components['schemas']['CustomerImportPreview']

function Failure({ error }: { error: unknown }) {
  return <p className="error" role="alert">{error instanceof Error ? error.message : 'The request failed'}</p>
}

export function CustomersView({ workspaceId, canWrite }: { workspaceId: string; canWrite: boolean }) {
  const client = useQueryClient()
  const [section, setSection] = useState<'records' | 'import'>('records')
  const [search, setSearch] = useState('')
  const [selected, setSelected] = useState<Customer | null>(null)
  const [code, setCode] = useState('')
  const [name, setName] = useState('')
  const [partyType, setPartyType] = useState<'B2B' | 'B2C'>('B2B')
  const [gstin, setGstin] = useState('')
  const [email, setEmail] = useState('')
  const [terms, setTerms] = useState('0')
  const [creditLimit, setCreditLimit] = useState('0')
  const [addressType, setAddressType] = useState<'BILLING' | 'SHIPPING'>('BILLING')
  const [line1, setLine1] = useState('')
  const [city, setCity] = useState('')
  const [stateCode, setStateCode] = useState('')
  const customers = useQuery({ queryKey: ['catalog', workspaceId, 'customers', search], queryFn: ({ signal }) => workspaceApi<Page<Customer>>(workspaceId, `/customers?page_size=100&search=${encodeURIComponent(search)}`, { signal }) })
  const addresses = useQuery({ queryKey: ['catalog', workspaceId, 'customer-addresses', selected?.customer_id], queryFn: ({ signal }) => workspaceApi<Address[]>(workspaceId, `/customers/${selected?.customer_id}/addresses`, { signal }), enabled: !!selected })
  const save = useMutation({ mutationFn: () => workspaceApi<Customer>(workspaceId, '/customers', { method: 'POST', body: jsonBody({ code, name, party_type: partyType, gstin: gstin || null, email: email || null, payment_terms_days: Number(terms), credit_limit: creditLimit }) }), onSuccess: async () => { setCode(''); setName(''); setGstin(''); setEmail(''); setTerms('0'); setCreditLimit('0'); await client.invalidateQueries({ queryKey: ['catalog', workspaceId, 'customers'] }) } })
  const addAddress = useMutation({ mutationFn: () => workspaceApi<Address>(workspaceId, `/customers/${selected?.customer_id}/addresses`, { method: 'POST', body: jsonBody({ address_type: addressType, line1, city, state_code: stateCode, is_default: true }) }), onSuccess: async () => { setLine1(''); setCity(''); setStateCode(''); await client.invalidateQueries({ queryKey: ['catalog', workspaceId, 'customer-addresses', selected?.customer_id] }) } })
  return <section>
    <div className="catalog-tabs" role="tablist" aria-label="Customer sections"><button className="button secondary" role="tab" aria-selected={section === 'records'} onClick={() => setSection('records')}>Customers</button>{canWrite && <button className="button secondary" role="tab" aria-selected={section === 'import'} onClick={() => setSection('import')}>Customer CSV</button>}</div>
    {section === 'import' && canWrite && <CustomerCsv workspaceId={workspaceId} />}
    {section === 'records' && <>
    <p className="muted">Set up buyers, tax details and credit terms before creating sales orders.</p>
    {canWrite && <section className="card form-card"><h3>New customer</h3>{save.isError && <Failure error={save.error} />}<form className="form-grid" onSubmit={(event: FormEvent) => { event.preventDefault(); save.mutate() }}><label>Customer code<input required value={code} onChange={event => setCode(event.target.value)} /></label><label>Customer name<input required value={name} onChange={event => setName(event.target.value)} /></label><label>Buyer type<select value={partyType} onChange={event => setPartyType(event.target.value as typeof partyType)}><option value="B2B">Business</option><option value="B2C">Consumer</option></select></label><label>GSTIN<input value={gstin} onChange={event => setGstin(event.target.value.toUpperCase())} /></label><label>Email<input type="email" value={email} onChange={event => setEmail(event.target.value)} /></label><label>Payment terms (days)<input type="number" min="0" value={terms} onChange={event => setTerms(event.target.value)} /></label><label>Credit limit (INR)<input type="number" min="0" step="0.0001" value={creditLimit} onChange={event => setCreditLimit(event.target.value)} /></label><div className="actions wide"><button className="button primary" disabled={save.isPending}>Save customer</button></div></form></section>}
    <div className="catalog-toolbar"><label>Find customer<input type="search" value={search} placeholder="Code or name" onChange={event => setSearch(event.target.value)} /></label></div>
    {customers.isError && <Failure error={customers.error} />}{customers.isLoading ? <p role="status">Loading customers…</p> : customers.data?.items.length ? <div className="card table-card"><div className="table-wrap"><table><thead><tr><th>Customer</th><th>Code</th><th>Terms</th><th>Credit limit</th><th>Action</th></tr></thead><tbody>{customers.data.items.map(value => <tr key={value.customer_id}><td><strong>{value.name}</strong><small>{value.email ?? value.gstin ?? ''}</small></td><td>{value.code}</td><td>{value.payment_terms_days} days</td><td>{formatMoney(value.credit_limit)}</td><td><button className="button small secondary" onClick={() => setSelected(value)}>Addresses</button></td></tr>)}</tbody></table></div></div> : <div className="card empty-state"><h3>No customers yet</h3><p>Add the first buyer to begin sales setup.</p></div>}
    {selected && <section className="card form-card"><div className="page-heading"><h3>{selected.name} · addresses</h3><button className="button small secondary" onClick={() => setSelected(null)}>Close</button></div>{addresses.isError && <Failure error={addresses.error} />}{addresses.data?.length ? <div className="table-wrap"><table><thead><tr><th>Type</th><th>Address</th><th>Default</th></tr></thead><tbody>{addresses.data.map(value => <tr key={value.address_id}><td>{value.address_type}</td><td>{value.line1}, {value.city} {value.state_code}</td><td>{value.is_default ? 'Yes' : 'No'}</td></tr>)}</tbody></table></div> : <p className="muted">No addresses saved.</p>}{canWrite && <form className="form-grid" onSubmit={(event: FormEvent) => { event.preventDefault(); addAddress.mutate() }}><label>Address type<select value={addressType} onChange={event => setAddressType(event.target.value as typeof addressType)}><option value="BILLING">Billing</option><option value="SHIPPING">Shipping</option></select></label><label>Address line<input required value={line1} onChange={event => setLine1(event.target.value)} /></label><label>City<input required value={city} onChange={event => setCity(event.target.value)} /></label><label>State code<input required pattern="[0-9]{2}" maxLength={2} value={stateCode} onChange={event => setStateCode(event.target.value)} /></label><div className="actions wide"><button className="button primary" disabled={addAddress.isPending}>Add default address</button></div>{addAddress.isError && <Failure error={addAddress.error} />}</form>}</section>}
    </>}
  </section>
}

function CustomerCsv({ workspaceId }: { workspaceId: string }) {
  const client = useQueryClient()
  const [csvText, setCsvText] = useState('')
  const [preview, setPreview] = useState<CustomerImport | null>(null)
  const validate = useMutation({ mutationFn: () => workspaceApi<CustomerImport>(workspaceId, '/customers/imports/validate', { method: 'POST', body: jsonBody({ csv_text: csvText }) }), onSuccess: setPreview })
  const commit = useMutation({ mutationFn: () => workspaceApi<CustomerImport>(workspaceId, '/customers/imports/commit', { method: 'POST', body: jsonBody({ csv_text: csvText }) }), onSuccess: async result => { setPreview(result); await client.invalidateQueries({ queryKey: ['catalog', workspaceId, 'customers'] }) } })
  const download = () => {
    const content = 'code,name,party_type,gstin,email,phone,tax_treatment,place_of_supply_state,payment_terms_days,credit_limit\nBUYER-001,Example buyer,B2B,,buyer@example.com,,REGISTERED,27,30,50000\n'
    const url = URL.createObjectURL(new Blob([content], { type: 'text/csv' }))
    const link = document.createElement('a')
    link.href = url; link.download = 'cims-customers-template.csv'; link.click(); URL.revokeObjectURL(url)
  }
  return <section className="card form-card"><div className="page-heading"><div><h3>Import customers</h3><p className="muted">Review the rows before adding them to this workspace.</p></div><button className="button secondary" type="button" onClick={download}>Download template</button></div>
    <label>Customer CSV file<input type="file" accept=".csv,text/csv" onChange={async event => { const file = event.target.files?.[0]; if (file) { setCsvText(await file.text()); setPreview(null) } }} /></label>
    <div className="actions"><button className="button secondary" disabled={!csvText || validate.isPending} onClick={() => validate.mutate()}>Validate CSV</button>{preview?.can_import && <button className="button primary" disabled={commit.isPending || commit.isSuccess} onClick={() => commit.mutate()}>Import {preview.valid_count} customers</button>}</div>
    {validate.isError && <Failure error={validate.error} />}{commit.isError && <Failure error={commit.error} />}
    {preview && <div><p role="status">{commit.isSuccess ? 'Import complete. ' : ''}{preview.valid_count} valid of {preview.row_count} rows.</p>{preview.issues.length > 0 && <ul>{preview.issues.map((issue, index) => <li key={index}>Row {issue.row}, {issue.field}: {issue.message}</li>)}</ul>}</div>}
  </section>
}
