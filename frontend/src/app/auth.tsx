import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { api, jsonBody, setAccessToken } from '../api/client'
import type { AuthResponse, Identity } from '../api/types'

type AuthContextValue = { user: Identity | null; loading: boolean; signIn: (data: AuthResponse) => Promise<void>; reloadMe: () => Promise<void>; signOut: () => Promise<void> }
const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<Identity | null>(null)
  const [loading, setLoading] = useState(true)
  const reloadMe = async () => {
    const identity = await api<Identity>('/auth/me')
    setUser(identity)
  }
  useEffect(() => {
    let alive = true
    api<AuthResponse>('/auth/refresh', { method: 'POST', body: jsonBody({}) })
      .then(result => { if (alive) { setAccessToken(result.access_token); return api<Identity>('/auth/me') } return null })
      .then(identity => { if (alive && identity) setUser(identity) })
      .catch(() => { if (alive) { setAccessToken(null); setUser(null) } })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [])
  const value = useMemo<AuthContextValue>(() => ({
    user, loading,
    signIn: async result => { setAccessToken(result.access_token); await reloadMe() },
    reloadMe,
    signOut: async () => { try { await api<void>('/auth/logout', { method: 'POST', body: jsonBody({}) }) } finally { setAccessToken(null); setUser(null) } },
  }), [user, loading])
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside AuthProvider')
  return context
}

export function ProtectedRoute({ children, allowPasswordChange = false }: { children: ReactNode; allowPasswordChange?: boolean }) {
  const { user, loading } = useAuth()
  const location = useLocation()
  if (loading) return <main className="center"><p role="status">Restoring your session…</p></main>
  if (!user) return <Navigate to="/login" replace state={{ from: location.pathname }} />
  if (user.password_change_required && !allowPasswordChange) return <Navigate to="/change-password" replace />
  if (!user.password_change_required && location.pathname === '/change-password') return <Navigate to="/" replace />
  return <>{children}</>
}

export function AdminRoute({ children }: { children: ReactNode }) {
  const { user } = useAuth()
  if (!user || !['Owner', 'Admin'].includes(user.role)) return <Navigate to="/" replace />
  return <>{children}</>
}
