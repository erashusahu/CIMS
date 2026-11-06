import type { ApiError, AuthResponse } from './types'

const API = import.meta.env.VITE_API_URL ?? 'http://127.0.0.1:8000/api/v1'
let accessToken: string | null = null
let refreshing: Promise<string | null> | null = null

export function setAccessToken(token: string | null) { accessToken = token }
export function getAccessToken() { return accessToken }

async function refreshAccessToken(): Promise<string | null> {
  if (!refreshing) {
    refreshing = fetch(`${API}/auth/refresh`, { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' } })
      .then(async response => {
        if (!response.ok) return null
        const result = await response.json() as AuthResponse
        accessToken = result.access_token
        return accessToken
      })
      .catch(() => null)
      .finally(() => { refreshing = null })
  }
  return refreshing
}

export async function request<T>(path: string, init: RequestInit = {}, retry = true): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('Content-Type', 'application/json')
  if (accessToken) headers.set('Authorization', `Bearer ${accessToken}`)
  const response = await fetch(`${API}${path}`, { ...init, headers, credentials: 'include' })
  if (response.status === 401 && retry && path !== '/auth/login' && path !== '/auth/refresh') {
    const token = await refreshAccessToken()
    if (token) return request<T>(path, init, false)
  }
  if (!response.ok) {
    let parsed: ApiError | null = null
    try { parsed = await response.json() as ApiError } catch { /* use status fallback */ }
    const message = parsed?.error?.message ?? `Request failed (${response.status})`
    const error = new Error(message) as Error & { status: number; code?: string; fields?: unknown[] }
    error.status = response.status
    error.code = parsed?.error?.code
    error.fields = parsed?.error?.fields
    throw error
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export function api<T>(path: string, init?: RequestInit) { return request<T>(path, init) }
export function jsonBody(value: unknown) { return JSON.stringify(value) }
