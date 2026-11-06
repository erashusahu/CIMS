import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { api, jsonBody } from '../../api/client'
import { useAuth } from '../../app/auth'
import type { AuthResponse } from '../../api/types'

const registerSchema = z.object({ business_name: z.string().min(1, 'Business name is required'), business_identifier: z.string().regex(/^[a-z0-9]+(?:-[a-z0-9]+)*$/, 'Use lowercase letters, numbers and hyphens'), owner_name: z.string().min(1, 'Owner name is required'), email: z.string().email(), password: z.string().min(12).max(128), password_confirmation: z.string().min(12).max(128), profile: z.enum(['retail', 'wholesale']) }).refine(value => value.password === value.password_confirmation, { path: ['password_confirmation'], message: 'Passwords do not match' })
type RegisterValues = z.infer<typeof registerSchema>
const loginSchema = z.object({ business_identifier: z.string().min(3), email: z.string().email(), password: z.string().min(1) })
type LoginValues = z.infer<typeof loginSchema>

function ErrorMessage({ message }: { message: string }) { return <p className="error" role="alert">{message}</p> }

export function RegisterPage() {
  const navigate = useNavigate()
  const [serverError, setServerError] = useState('')
  const { register, handleSubmit, formState: { errors, isSubmitting } } = useForm<RegisterValues>({ resolver: zodResolver(registerSchema), defaultValues: { profile: 'retail' } })
  const submit = handleSubmit(async values => {
    setServerError('')
    try {
      await api('/auth/register', { method: 'POST', body: jsonBody(values) })
      navigate(`/login?business_identifier=${encodeURIComponent(values.business_identifier)}&registered=1`)
    } catch (error) { setServerError(error instanceof Error ? error.message : 'Registration failed') }
  })
  return <main className="auth-wrap"><section className="card auth-card"><p className="eyebrow">CIMS · BUSINESS ACCOUNT</p><h1>Register your business</h1><p className="muted">Start with your business profile and owner account.</p>{serverError && <ErrorMessage message={serverError} />}
    <form onSubmit={submit} noValidate>
      <label>Business name<input {...register('business_name')} autoComplete="organization" /></label>{errors.business_name && <ErrorMessage message={errors.business_name.message!} />}
      <label>Business identifier<input {...register('business_identifier')} placeholder="acme-traders" autoCapitalize="none" /></label>{errors.business_identifier && <ErrorMessage message={errors.business_identifier.message!} />}
      <label>Owner full name<input {...register('owner_name')} autoComplete="name" /></label>{errors.owner_name && <ErrorMessage message={errors.owner_name.message!} />}
      <label>Owner email<input {...register('email')} type="email" autoComplete="email" /></label>{errors.email && <ErrorMessage message={errors.email.message!} />}
      <label>Business profile<select {...register('profile')}><option value="retail">Retail</option><option value="wholesale">Wholesale</option></select></label>
      <fieldset><legend>Business defaults</legend><p>Currency INR · Country India · Financial year April · Timezone Asia/Kolkata</p></fieldset>
      <label>Password<input {...register('password')} type="password" autoComplete="new-password" /></label>{errors.password && <ErrorMessage message={errors.password.message!} />}
      <label>Confirm password<input {...register('password_confirmation')} type="password" autoComplete="new-password" /></label>{errors.password_confirmation && <ErrorMessage message={errors.password_confirmation.message!} />}
      <button className="button primary" disabled={isSubmitting}>{isSubmitting ? 'Creating account…' : 'Create business'}</button>
    </form><p className="form-foot">Already registered? <Link to="/login">Sign in</Link></p></section></main>
}

export function LoginPage() {
  const navigate = useNavigate()
  const location = useLocation()
  const { signIn } = useAuth()
  const params = new URLSearchParams(location.search)
  const [notice, setNotice] = useState(params.get('registered') ? 'Business created. Sign in to continue.' : '')
  const [serverError, setServerError] = useState('')
  const { register, handleSubmit, formState: { errors, isSubmitting } } = useForm<LoginValues>({ resolver: zodResolver(loginSchema), defaultValues: { business_identifier: params.get('business_identifier') ?? '' } })
  const submit = handleSubmit(async values => {
    setServerError(''); setNotice('')
    try { const result = await api<AuthResponse>('/auth/login', { method: 'POST', body: jsonBody(values) }); await signIn(result); navigate(result.password_change_required ? '/change-password' : '/', { replace: true }) }
    catch (error) { setServerError(error instanceof Error ? error.message : 'Sign in failed') }
  })
  return <main className="auth-wrap"><section className="card auth-card"><p className="eyebrow">CIMS · SECURE SIGN IN</p><h1>Welcome back</h1>{notice && <p className="success" role="status">{notice}</p>}{serverError && <ErrorMessage message={serverError} />}
    <form onSubmit={submit} noValidate><label>Business identifier<input {...register('business_identifier')} autoCapitalize="none" /></label>{errors.business_identifier && <ErrorMessage message={errors.business_identifier.message!} />}<label>Email<input type="email" {...register('email')} autoComplete="username" /></label>{errors.email && <ErrorMessage message={errors.email.message!} />}<label>Password<input type="password" {...register('password')} autoComplete="current-password" /></label>{errors.password && <ErrorMessage message={errors.password.message!} />}<button className="button primary" disabled={isSubmitting}>{isSubmitting ? 'Signing in…' : 'Sign in'}</button></form><p className="form-foot">New to CIMS? <Link to="/register">Register a business</Link></p></section></main>
}

export function ChangePasswordPage() {
  const navigate = useNavigate()
  const { signOut } = useAuth()
  const [error, setError] = useState('')
  const { register, handleSubmit, formState: { errors, isSubmitting } } = useForm<{ password: string; confirm: string }>({ resolver: zodResolver(z.object({ password: z.string().min(12).max(128), confirm: z.string().min(12).max(128) }).refine(v => v.password === v.confirm, { path: ['confirm'], message: 'Passwords do not match' })) })
  const submit = handleSubmit(async values => {
    setError('')
    try { await api<void>('/auth/change-password', { method: 'POST', body: jsonBody({ new_password: values.password }) }); await signOut(); navigate('/login', { replace: true }) }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Unable to change password') }
  })
  return <main className="auth-wrap"><section className="card auth-card"><p className="eyebrow">ACCOUNT SETUP</p><h1>Choose a new password</h1><p className="muted">A permanent password is required before you can access business data.</p>{error && <ErrorMessage message={error} />}<form onSubmit={submit}><label>New password<input type="password" autoComplete="new-password" {...register('password')} /></label>{errors.password && <ErrorMessage message={errors.password.message!} />}<label>Confirm password<input type="password" autoComplete="new-password" {...register('confirm')} /></label>{errors.confirm && <ErrorMessage message={errors.confirm.message!} />}<button className="button primary" disabled={isSubmitting}>{isSubmitting ? 'Saving…' : 'Set password and return to sign in'}</button></form></section></main>
}
