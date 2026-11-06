import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

afterEach(cleanup)
import { MemoryRouter } from 'react-router-dom'
import { RegisterPage } from './pages'

vi.mock('../../api/client', () => ({ api: vi.fn(), jsonBody: (value: unknown) => JSON.stringify(value) }))

function renderRegister() { return render(<MemoryRouter><RegisterPage /></MemoryRouter>) }

describe('business registration form', () => {
  it('shows field validation and does not submit invalid identifiers', async () => {
    renderRegister()
    fireEvent.change(screen.getByLabelText('Business identifier'), { target: { value: 'Bad ID' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create business' }))
    expect(await screen.findByText('Use lowercase letters, numbers and hyphens')).toBeInTheDocument()
  })

  it('requires matching passwords', async () => {
    const view = renderRegister()
    fireEvent.change(view.container.querySelector('input[name="password"]')!, { target: { value: 'a sufficiently long password' } })
    fireEvent.change(view.container.querySelector('input[name="password_confirmation"]')!, { target: { value: 'a different long password' } })
    fireEvent.click(view.getByRole('button', { name: 'Create business' }))
    await waitFor(() => expect(view.getByText('Passwords do not match')).toBeInTheDocument())
  })
})
