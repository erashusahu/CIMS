import { useQuery } from '@tanstack/react-query'
import type { components } from '../../api/openapi'
import { workspaceApi, catalogApi, type CatalogOptions, type ItemSummary, type Page } from './api'

type Location = components['schemas']['LocationOption']

type Destination = 'setup' | 'items' | 'import' | 'stock' | 'suppliers' | 'sales'
type Step = {
  title: string
  description: string
  action: string
  destination: Destination
  stockView?: 'opening' | 'balances'
  done?: boolean
  error?: boolean
  blocked?: boolean
}

function stepState(step: Step) {
  if (step.error) return 'Could not check'
  if (step.blocked) return 'Setup needed'
  if (step.done === undefined) return 'Checking'
  return step.done ? 'Done' : 'To do'
}

export function GetStartedView({ workspaceId, options, onOpen }: {
  workspaceId: string
  options: CatalogOptions
  onOpen: (destination: Destination, stockView?: 'opening' | 'balances') => void
}) {
  const items = useQuery({
    queryKey: ['catalog', workspaceId, 'onboarding', 'items'],
    queryFn: ({ signal }) => catalogApi<Page<ItemSummary>>(workspaceId, '/items?page=1&page_size=1&search=', { signal }),
  })
  const opening = useQuery({
    queryKey: ['catalog', workspaceId, 'opening-status'],
    queryFn: ({ signal }) => workspaceApi<{ ready: boolean }>(workspaceId, '/inventory/opening-status', { signal }),
  })
  const locations = useQuery({
    queryKey: ['catalog', workspaceId, 'stock-locations'],
    queryFn: ({ signal }) => workspaceApi<Location[]>(workspaceId, '/inventory/locations', { signal }),
    enabled: opening.data?.ready === true,
  })
  const balances = useQuery({
    queryKey: ['catalog', workspaceId, 'onboarding', 'balances'],
    queryFn: ({ signal }) => workspaceApi<Page<unknown>>(workspaceId, '/inventory/balances?page=1&page_size=1', { signal }),
    enabled: opening.data?.ready === true,
  })

  const steps: Step[] = [
    {
      title: 'Set up units and item types',
      description: 'Add a stock unit such as PCS and an item type such as GOODS. You only need to do this once.',
      action: 'Open units & variants', destination: 'setup',
      done: options.units.some(unit => unit.is_active) && options.item_types.length > 0,
    },
    {
      title: 'Add your first item',
      description: 'Create one simple item to learn the flow. You can import many items by CSV later.',
      action: 'Open items', destination: 'items',
      done: items.data ? items.data.total > 0 : undefined, error: items.isError,
    },
    {
      title: 'Add a storage location',
      description: 'Choose where this stock lives, such as a store or warehouse.',
      action: 'Add a location', destination: 'stock', stockView: 'opening',
      done: locations.data ? locations.data.some(location => location.is_active) : undefined, error: locations.isError,
      blocked: opening.data?.ready === false,
    },
    {
      title: 'Post opening stock',
      description: 'Save a draft with quantity and cost, review it, then post it to create the first stock balance.',
      action: 'Open opening stock', destination: 'stock', stockView: 'opening',
      done: balances.data ? balances.data.total > 0 : undefined, error: balances.isError,
      blocked: opening.data?.ready === false,
    },
  ]
  const completed = steps.filter(step => step.done).length
  const next = steps.findIndex(step => step.done !== true)
  const allDone = completed === steps.length

  return <div className="getting-started">
    <section className="card setup-intro">
      <div><p className="eyebrow">YOUR FIRST STOCK BALANCE</p><h2>{allDone ? 'Your workspace is ready' : 'Four steps to get started'}</h2><p>Start with one item. Add the rest with CSV once the flow makes sense.</p></div>
      <div className="setup-progress" role="status" aria-label={`${completed} of ${steps.length} steps complete`}><strong>{completed}/{steps.length}</strong><span>steps complete</span></div>
    </section>
    {!opening.isLoading && opening.data?.ready === false && <div className="notice error" role="alert">Opening stock needs database setup before you can add a location or post stock. Apply SQL 003, then <button className="button small secondary" onClick={() => void opening.refetch()}>Check again</button>.</div>}
    {(opening.isError || items.isError || locations.isError || balances.isError) && <div className="notice error" role="alert">Could not check all setup steps. <button className="button small secondary" onClick={() => { void opening.refetch(); void items.refetch(); if (opening.data?.ready) { void locations.refetch(); void balances.refetch() } }}>Retry checks</button></div>}
    <ol className="setup-steps">
      {steps.map((step, index) => <li key={step.title} className={`card setup-step ${step.done ? 'complete' : index === next ? 'current' : ''}`}>
        <span className="setup-step-number" aria-hidden="true">{step.done ? '✓' : index + 1}</span>
        <div className="setup-step-content"><div className="setup-step-heading"><h3>{step.title}</h3><span className={`setup-state ${step.done ? 'complete' : ''}`}>{stepState(step)}</span></div><p>{step.description}</p>
          {index === next && step.done === false && options.can_write && <div className="actions"><button className="button primary" onClick={() => onOpen(step.destination, step.stockView)}>{step.action}</button>{index === 1 && <button className="button secondary" onClick={() => onOpen('import')}>Import items by CSV</button>}</div>}
        </div>
      </li>)}
    </ol>
    {allDone && <section className="card setup-next"><h2>What to do next</h2><p>Your stock balance is ready. Use Purchasing to receive more stock or Sales orders to reserve and ship it.</p><div className="actions"><button className="button primary" onClick={() => onOpen('stock', 'balances')}>View stock balances</button><button className="button secondary" onClick={() => onOpen('suppliers')}>Purchasing</button><button className="button secondary" onClick={() => onOpen('sales')}>Sales orders</button></div></section>}
    {!options.can_write && !allDone && <p className="muted">This workspace is read-only. Ask an administrator to complete the setup steps.</p>}
  </div>
}
