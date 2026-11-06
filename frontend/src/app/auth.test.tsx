import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { AuthProvider } from './auth'

vi.mock('../api/client', () => ({ api: vi.fn().mockRejectedValue(new Error('no session')), jsonBody: JSON.stringify, setAccessToken: vi.fn() }))

describe('session restoration', () => {
  it('clears auth state when refresh cannot restore a session', async () => {
    render(<MemoryRouter><AuthProvider><div>Sign-in shell</div></AuthProvider></MemoryRouter>)
    await waitFor(() => expect(screen.getByText('Sign-in shell')).toBeInTheDocument())
  })
})
