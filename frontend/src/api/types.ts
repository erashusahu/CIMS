// Generated contract snapshot for the Phase 1 API. Regenerate with `npm run api:types` after backend changes.
export type Profile = 'retail' | 'wholesale'
export type LocationType = 'warehouse' | 'godown' | 'store' | 'office'
export type RoleName = 'Owner' | 'Admin' | 'Warehouse Manager' | 'Warehouse Staff' | 'Sales' | 'Procurement' | 'Accountant' | 'Viewer'

export interface ApiError {
  error: { code: string; message: string; request_id: string; fields?: unknown[] }
}
export interface AuthResponse {
  access_token: string
  token_type: 'bearer'
  expires_in: number
  password_change_required: boolean
}
export interface Identity {
  id: string
  full_name: string
  email: string
  role: RoleName
  all_locations: boolean
  password_change_required: boolean
  business_id: string
  business_name: string
  business_identifier: string
}
export interface Business {
  id: string
  identifier: string
  name: string
  profile: Profile
  currency: 'INR'
  country: 'IN'
  financial_year_start_month: 4
  timezone: 'Asia/Kolkata'
}
export interface Location {
  id: string
  code: string
  name: string
  type: LocationType
  address: string | null
  city: string | null
  state_code: string | null
  pin_code: string | null
  active: boolean
  created_at: string
}
export interface Page<T> { items: T[]; total: number; page: number; page_size: number }
export interface Staff {
  id: string
  full_name: string
  email: string
  role: RoleName
  all_locations: boolean
  location_ids: string[]
  suspended: boolean
  password_change_required: boolean
  temporary_password?: string
}
export interface AuditRecord {
  id: string
  actor_user_id: string | null
  timestamp: string
  action: string
  affected_type: string
  affected_id: string
  request_id: string
  changes: Record<string, unknown>
}
