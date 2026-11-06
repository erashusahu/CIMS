import { getAccessToken, request } from '../../api/client'
import type { components } from '../../api/openapi'

type Schemas = components['schemas']
export type CatalogItem = Schemas['ItemOut']
export type ItemInput = Schemas['ItemIn']
export type ItemSummary = Schemas['ItemSummaryOut']
export type CatalogOptions = Schemas['CatalogOptions']
export type Unit = Schemas['UnitOut']
export type Workspace = Schemas['WorkspaceOut']
export type ImportPreview = Schemas['ImportPreview']
export type ConversionResult = Schemas['ConvertOut']
export type Page<T> = { items: T[]; total: number; page: number; page_size: number }
export const localCatalogMode = import.meta.env.VITE_CATALOG_LOCAL_MODE === 'true'

export function workspaceApi<T>(workspace: string | null, path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (localCatalogMode && workspace) headers.set('X-CIMS-Tenant-ID', workspace)
  return request<T>(path, { ...init, headers }, !localCatalogMode)
}

export function catalogApi<T>(workspace: string | null, path: string, init: RequestInit = {}): Promise<T> {
  return workspaceApi<T>(workspace, `/catalog${path}`, init)
}

export async function downloadTemplate(workspace: string): Promise<void> {
  // The existing API client expects JSON; the CSV download requires a text response.
  const base = import.meta.env.VITE_API_URL ?? 'http://127.0.0.1:8000/api/v1'
  const headers = new Headers()
  if (localCatalogMode) headers.set('X-CIMS-Tenant-ID', workspace)
  else {
    const token = getAccessToken()
    if (token) headers.set('Authorization', `Bearer ${token}`)
  }
  const response = await fetch(`${base}/catalog/imports/template`, { headers, credentials: 'include' })
  if (!response.ok) throw new Error('The CSV template could not be downloaded')
  const url = URL.createObjectURL(await response.blob())
  const link = document.createElement('a')
  link.href = url
  link.download = 'cims-items-template.csv'
  link.click()
  URL.revokeObjectURL(url)
}

export function formatMoney(value: string | number): string {
  const [whole, fraction = ''] = String(value).split('.')
  const leading = whole.length > 3 ? `${whole.slice(0, -3).replace(/\B(?=(\d{2})+(?!\d))/g, ',')},` : ''
  const decimals = fraction.replace(/0+$/, '').padEnd(2, '0')
  return `₹${leading}${whole.slice(-3)}.${decimals}`
}
