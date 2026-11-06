import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter, Link, Navigate, NavLink, Route, Routes, useNavigate } from 'react-router-dom'
import { AuthProvider, AdminRoute, ProtectedRoute, useAuth } from './auth'
import { RegisterPage, LoginPage, ChangePasswordPage } from '../features/auth/pages'
import { WorkspacePage } from '../features/business/pages'
import { LocationsPage } from '../features/locations/pages'
import { TeamPage } from '../features/team/pages'
import { AuditPage } from '../features/audit/pages'
import { CatalogView, CatalogWorkspace } from '../features/catalog/pages'
import { localCatalogMode } from '../features/catalog/api'
import { clearQueue, pendingCount, queueScope } from '../features/mobile/queue'

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: 1, staleTime: 15_000 } } })

function Shell() {
  const { user, signOut } = useAuth()
  const navigate = useNavigate()
  if (!user) return <Routes><Route path="/register" element={<RegisterPage />} /><Route path="/login" element={<LoginPage />} /><Route path="*" element={<Navigate to="/login" replace />} /></Routes>
  const leave = async () => {
    const scope = queueScope(user.business_id, user.id)
    try {
      const pending = await pendingCount(scope)
      if (pending > 0 && !window.confirm(`${pending} mobile action${pending === 1 ? '' : 's'} will be discarded from this device. Sign out?`)) return
      await clearQueue(scope)
    } catch (error) { window.alert(error instanceof Error ? error.message : 'Could not clear mobile data'); return }
    await signOut(); queryClient.clear(); navigate('/login')
  }
  return <div className="app-shell">
    <aside className="sidebar">
      <Link className="brand" to="/">CIMS</Link>
      <p className="muted">{user.business_name}</p>
      <nav aria-label="Main navigation">
        <NavLink to="/" end>Workspace</NavLink>
        <NavLink to="/locations">Locations</NavLink>
        <NavLink to="/catalog">Item catalog</NavLink>
        {['Owner', 'Admin'].includes(user.role) && <><NavLink to="/team">Team</NavLink><NavLink to="/settings">Business settings</NavLink><NavLink to="/audit">Audit history</NavLink></>}
      </nav>
      <div id="catalog-sidebar-navigation" />
      <div className="sidebar-user"><strong>{user.full_name}</strong><span>{user.role}</span><button className="button secondary" onClick={() => void leave()}>Sign out</button></div>
    </aside>
    <main className="content"><Routes>
      <Route path="/" element={<ProtectedRoute><WorkspacePage /></ProtectedRoute>} />
      <Route path="/change-password" element={<ProtectedRoute allowPasswordChange><ChangePasswordPage /></ProtectedRoute>} />
      <Route path="/locations" element={<ProtectedRoute><LocationsPage /></ProtectedRoute>} />
      <Route path="/catalog" element={<ProtectedRoute><CatalogView workspaceId={user.business_id} actorId={user.id} /></ProtectedRoute>} />
      <Route path="/team" element={<ProtectedRoute><AdminRoute><TeamPage /></AdminRoute></ProtectedRoute>} />
      <Route path="/settings" element={<ProtectedRoute><AdminRoute><WorkspacePage settings /></AdminRoute></ProtectedRoute>} />
      <Route path="/audit" element={<ProtectedRoute><AdminRoute><AuditPage /></AdminRoute></ProtectedRoute>} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes></main>
  </div>
}

export function App() { return <QueryClientProvider client={queryClient}><BrowserRouter>{localCatalogMode ? <CatalogWorkspace /> : <AuthProvider><Shell /></AuthProvider>}</BrowserRouter></QueryClientProvider> }
