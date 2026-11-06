import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { components } from '../../api/openapi'
import { getAccessToken, jsonBody } from '../../api/client'
import { localCatalogMode, workspaceApi } from '../catalog/api'

type Order = components['schemas']['SalesOrderOut']
type PickList = components['schemas']['PickListOut']
type Shipment = components['schemas']['ShipmentOut']
type PackRow = { weight_kg: string; length_cm: string; width_cm: string; height_cm: string; contents: Record<string, string> }
const emptyPackage = (): PackRow => ({ weight_kg: '', length_cm: '', width_cm: '', height_cm: '', contents: {} })
function Failure({ error }: { error: unknown }) { return <p className="error" role="alert">{error instanceof Error ? error.message : 'The request failed'}</p> }

export function ShippingPanel({ workspaceId, order }: { workspaceId: string; order: Order }) {
  const client = useQueryClient()
  const [reservationId, setReservationId] = useState('')
  const [scannedCode, setScannedCode] = useState('')
  const [scanQty, setScanQty] = useState('1')
  const [overrideReason, setOverrideReason] = useState('')
  const [sourceLocation, setSourceLocation] = useState('')
  const [shipQty, setShipQty] = useState<Record<string, string>>({})
  const [packages, setPackages] = useState<PackRow[]>([emptyPackage()])
  const [carrier, setCarrier] = useState('')
  const [trackingNo, setTrackingNo] = useState('')
  const [trackingUrl, setTrackingUrl] = useState('')
  const [downloadError, setDownloadError] = useState('')
  const status = useQuery({ queryKey: ['catalog', workspaceId, 'shipping-status'], queryFn: ({ signal }) => workspaceApi<{ ready: boolean }>(workspaceId, '/sales/shipping-status', { signal }) })
  const pick = useQuery({ queryKey: ['catalog', workspaceId, 'pick-list', order.sales_order_id], queryFn: ({ signal }) => workspaceApi<PickList>(workspaceId, `/sales/orders/${order.sales_order_id}/pick-list`, { signal }), enabled: !!status.data?.ready })
  const shipments = useQuery({ queryKey: ['catalog', workspaceId, 'shipments', order.sales_order_id], queryFn: ({ signal }) => workspaceApi<Shipment[]>(workspaceId, `/sales/orders/${order.sales_order_id}/shipments`, { signal }), enabled: !!status.data?.ready })
  const refresh = async () => { await client.invalidateQueries({ queryKey: ['catalog', workspaceId] }) }
  const scan = useMutation({ mutationFn: () => workspaceApi(workspaceId, `/sales/orders/${order.sales_order_id}/scans`, { method: 'POST', body: jsonBody({ reservation_id: reservationId, scanned_code: scannedCode, quantity: scanQty, override_reason: overrideReason || null }) }), onSuccess: async () => { setScannedCode(''); setOverrideReason(''); await refresh() } })
  const locationId = sourceLocation || pick.data?.entries[0]?.location_id || ''
  const selectedLines = order.lines.filter(line => (line.location_id || order.location_id) === locationId)
  const selectedEntries = pick.data?.entries.filter(entry => entry.location_id === locationId) ?? []
  const required = (entry: PickList['entries'][number]) => {
    const line = order.lines.find(value => value.sales_order_line_id === entry.sales_order_line_id)
    const quantity = Number(shipQty[entry.sales_order_line_id] || 0)
    return line && quantity > 0 ? Number((Number(entry.reserved_quantity) * quantity / Number(line.quantity)).toFixed(4)) : 0
  }
  const pack = useMutation({ mutationFn: () => {
    const lines = selectedLines.filter(line => Number(shipQty[line.sales_order_line_id]) > 0).map(line => ({ sales_order_line_id: line.sales_order_line_id, quantity: shipQty[line.sales_order_line_id] }))
    const packed = packages.map((pkg, index) => ({
      weight_kg: pkg.weight_kg || null, length_cm: pkg.length_cm || null,
      width_cm: pkg.width_cm || null, height_cm: pkg.height_cm || null,
      contents: selectedEntries.filter(entry => required(entry) > 0).map(entry => ({ reservation_id: entry.reservation_id, quantity: pkg.contents[entry.reservation_id] || (packages.length === 1 && index === 0 ? String(required(entry)) : '0') })).filter(value => Number(value.quantity) > 0),
    })).filter(pkg => pkg.contents.length)
    return workspaceApi<Shipment>(workspaceId, `/sales/orders/${order.sales_order_id}/shipments`, { method: 'POST', body: jsonBody({ from_location_id: locationId, lines, packages: packed, carrier: carrier || null, tracking_no: trackingNo || null, tracking_url: trackingUrl || null }) })
  }, onSuccess: refresh })
  const post = useMutation({ mutationFn: (shipment: Shipment) => workspaceApi<Shipment>(workspaceId, `/sales/shipments/${shipment.shipment_id}/post`, { method: 'POST', body: jsonBody({ idempotency_key: crypto.randomUUID() }) }), onSuccess: refresh })
  const cancel = useMutation({ mutationFn: (shipment: Shipment) => workspaceApi<Shipment>(workspaceId, `/sales/shipments/${shipment.shipment_id}/cancel`, { method: 'POST' }), onSuccess: refresh })
  const download = async (shipment: Shipment) => {
    setDownloadError('')
    try {
      const base = import.meta.env.VITE_API_URL ?? 'http://127.0.0.1:8000/api/v1'
      const headers = new Headers()
      if (localCatalogMode) headers.set('X-CIMS-Tenant-ID', workspaceId)
      else { const token = getAccessToken(); if (token) headers.set('Authorization', `Bearer ${token}`) }
      const response = await fetch(`${base}/sales/shipments/${shipment.shipment_id}/packing-slip`, { headers, credentials: 'include' })
      if (!response.ok) throw new Error('The packing slip could not be downloaded')
      const url = URL.createObjectURL(await response.blob())
      const link = document.createElement('a')
      link.href = url; link.download = `${shipment.shipment_no.replaceAll('/', '-')}-packing-slip.pdf`; link.click()
      URL.revokeObjectURL(url)
    } catch (error) { setDownloadError(error instanceof Error ? error.message : 'Download failed') }
  }
  const setContent = (index: number, id: string, value: string) => setPackages(current => current.map((pkg, position) => position === index ? { ...pkg, contents: { ...pkg.contents, [id]: value } } : pkg))
  const setPackageField = (index: number, key: 'weight_kg' | 'length_cm' | 'width_cm' | 'height_cm', value: string) => setPackages(current => current.map((pkg, position) => position === index ? { ...pkg, [key]: value } : pkg))
  if (status.isLoading) return <p role="status">Checking shipping setup…</p>
  if (status.isError) return <Failure error={status.error} />
  if (!status.data?.ready) return <section className="card form-card"><h3>Shipping setup is required</h3><p>Apply SQL 006 to the Inventroy database after SQL 003–005, then check again.</p><button className="button secondary" onClick={() => void status.refetch()}>Check setup again</button></section>
  return <section className="card form-card"><h3>Pick and ship</h3><p className="muted">Scanning and packing record preparation. Physical stock changes only when you ship.</p>
    {pick.isError && <Failure error={pick.error} />}{pick.isLoading && <p role="status">Loading pick list…</p>}
    {pick.data && <><div className="table-wrap"><table><thead><tr><th>Component to pick</th><th>Reserved</th><th>Picked</th><th>Already shipped</th><th>To pick</th></tr></thead><tbody>{pick.data.entries.map(entry => <tr key={entry.reservation_id}><td><strong>{entry.sku}</strong><small>{entry.barcode ?? 'Use SKU to scan'}</small></td><td>{entry.reserved_quantity}</td><td>{entry.picked_unshipped}</td><td>{entry.shipped_quantity}</td><td>{entry.remaining_to_pick}</td></tr>)}</tbody></table></div>
      <form className="form-grid" onSubmit={(event: FormEvent) => { event.preventDefault(); scan.mutate() }}><label>Pick item<select required value={reservationId} onChange={event => setReservationId(event.target.value)}><option value="">Choose reservation</option>{pick.data.entries.filter(entry => Number(entry.remaining_to_pick) > 0).map(entry => <option key={entry.reservation_id} value={entry.reservation_id}>{entry.sku} · {entry.location_code}</option>)}</select></label><label>Scan barcode or SKU<input required value={scannedCode} onChange={event => setScannedCode(event.target.value)} placeholder="Scan or type, then press Enter" /></label><label>Quantity<input required type="number" min="0.0001" step="any" value={scanQty} onChange={event => setScanQty(event.target.value)} /></label><label>Manager override reason<input minLength={8} value={overrideReason} onChange={event => setOverrideReason(event.target.value)} placeholder="Only for a mismatched label" /></label><div className="actions wide"><button className="button primary" disabled={scan.isPending}>Verify pick</button></div>{scan.isError && <Failure error={scan.error} />}</form>
    </>}
    <h3>Pack a shipment</h3><p className="muted">Choose quantities from one location. The component amounts come from the confirmed reservation snapshot.</p>
    <form className="form-grid" onSubmit={(event: FormEvent) => { event.preventDefault(); pack.mutate() }}><label>Source location<select required value={locationId} onChange={event => { setSourceLocation(event.target.value); setShipQty({}); setPackages([emptyPackage()]) }}>{[...new Set(pick.data?.entries.map(entry => entry.location_id) ?? [])].map(value => <option key={value} value={value}>{pick.data?.entries.find(entry => entry.location_id === value)?.location_code ?? value}</option>)}</select></label>
      {selectedLines.map(line => <label key={line.sales_order_line_id}>Ship quantity · {line.line_no}<input type="number" min="0" max={Number(line.quantity) - Number(line.shipped_quantity) - Number(line.cancelled_quantity)} step="any" value={shipQty[line.sales_order_line_id] ?? '0'} onChange={event => setShipQty(current => ({ ...current, [line.sales_order_line_id]: event.target.value }))} /><small>Unshipped: {Number(line.quantity) - Number(line.shipped_quantity) - Number(line.cancelled_quantity)}</small></label>)}
      <label>Carrier (optional)<input value={carrier} onChange={event => setCarrier(event.target.value)} /></label><label>Tracking number (optional)<input value={trackingNo} onChange={event => setTrackingNo(event.target.value)} /></label><label>Tracking URL (optional)<input type="url" value={trackingUrl} onChange={event => setTrackingUrl(event.target.value)} /></label>
      {packages.map((pkg, index) => <fieldset className="catalog-subform wide" key={index}><legend>Package {index + 1}</legend><div className="form-grid"><label>Weight (kg)<input type="number" min="0" step="0.001" value={pkg.weight_kg} onChange={event => setPackageField(index, 'weight_kg', event.target.value)} /></label><label>Length (cm)<input type="number" min="0" step="0.001" value={pkg.length_cm} onChange={event => setPackageField(index, 'length_cm', event.target.value)} /></label><label>Width (cm)<input type="number" min="0" step="0.001" value={pkg.width_cm} onChange={event => setPackageField(index, 'width_cm', event.target.value)} /></label><label>Height (cm)<input type="number" min="0" step="0.001" value={pkg.height_cm} onChange={event => setPackageField(index, 'height_cm', event.target.value)} /></label>{selectedEntries.filter(entry => required(entry) > 0).map(entry => <label key={entry.reservation_id}>{entry.sku} in package <input type="number" min="0" step="any" placeholder={packages.length === 1 ? String(required(entry)) : '0'} value={pkg.contents[entry.reservation_id] ?? ''} onChange={event => setContent(index, entry.reservation_id, event.target.value)} /><small>Shipment needs {required(entry)} component units</small></label>)}</div>{packages.length > 1 && <button type="button" className="button small secondary" onClick={() => setPackages(current => current.filter((_, position) => position !== index))}>Remove package</button>}</fieldset>)}
      <div className="actions wide"><button type="button" className="button secondary" onClick={() => setPackages(current => [...current, emptyPackage()])}>Add package</button><button className="button primary" disabled={pack.isPending || !selectedLines.some(line => Number(shipQty[line.sales_order_line_id]) > 0)}>Save packed shipment</button></div>{pack.isError && <Failure error={pack.error} />}
    </form>
    <h3>Shipments</h3>{shipments.isError && <Failure error={shipments.error} />}{shipments.data?.length ? shipments.data.map(shipment => <div className="catalog-subform" key={shipment.shipment_id}><strong>{shipment.shipment_no} · {shipment.status}</strong><p className="muted">{shipment.packages.length} package(s) · {shipment.lines.length} line(s){shipment.tracking_no ? ` · Tracking ${shipment.tracking_no}` : ''}</p><div className="actions"><button className="button secondary" onClick={() => void download(shipment)}>Packing slip PDF</button>{shipment.status === 'PACKED' && <><button className="button primary" disabled={post.isPending} onClick={() => post.mutate(shipment)}>Ship and post stock</button><button className="button secondary" disabled={cancel.isPending} onClick={() => cancel.mutate(shipment)}>Cancel packed draft</button></>}</div></div>) : <p className="muted">No shipments yet.</p>}
    {post.isError && <Failure error={post.error} />}{cancel.isError && <Failure error={cancel.error} />}{downloadError && <p className="error" role="alert">{downloadError}</p>}
  </section>
}
