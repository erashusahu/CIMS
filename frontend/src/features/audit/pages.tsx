import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '../../api/client'
import type { AuditRecord, Page } from '../../api/types'

export function AuditPage() {
  const [page, setPage] = useState(1)
  const audit = useQuery({ queryKey: ['audit', page], queryFn: () => api<Page<AuditRecord>>(`/audit-logs?page=${page}&page_size=25`) })
  return <section className="page"><div className="page-heading"><div><p className="eyebrow">ACCOUNTABILITY</p><h1>Audit history</h1></div><span className="pill">{audit.data?.total ?? 0} events</span></div><p className="lead">A read-only history of important business changes. Passwords and authentication secrets are excluded.</p>{audit.isLoading ? <p role="status">Loading audit history…</p> : audit.isError ? <p className="error" role="alert">Unable to load audit history: {audit.error.message}</p> : audit.data?.items.length === 0 ? <div className="card empty-state"><h2>No audit events yet</h2><p>Important changes will appear here.</p></div> : <div className="card table-card"><div className="table-wrap"><table><thead><tr><th>Time</th><th>Action</th><th>Record</th><th>Actor</th><th>Request</th></tr></thead><tbody>{audit.data?.items.map(record => <tr key={record.id}><td>{new Date(record.timestamp).toLocaleString()}</td><td><strong>{record.action}</strong><pre>{JSON.stringify(record.changes, null, 2)}</pre></td><td>{record.affected_type}<small>{record.affected_id}</small></td><td>{record.actor_user_id ?? 'System'}</td><td><code>{record.request_id}</code></td></tr>)}</tbody></table></div><div className="pagination"><button className="button small secondary" disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</button><span>Page {page} of {Math.max(1, Math.ceil((audit.data?.total ?? 0) / 25))}</span><button className="button small secondary" disabled={page * 25 >= (audit.data?.total ?? 0)} onClick={() => setPage(page + 1)}>Next</button></div></div>}</section>
}
