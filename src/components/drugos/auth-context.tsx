'use client'

/**
 * Auth context for the DrugOS frontend.
 *
 * Wraps the entire app and exposes:
 *   - `user` (null when logged out)
 *   - `loading` (true while the initial /api/auth/me check is in flight)
 *   - `login(email, password)` — calls the backend, updates state
 *   - `register(...)` — calls the backend, updates state
 *   - `logout()` — calls the backend, clears state
 *
 * Any component under <AuthProvider> can call `useAuth()` to read state.
 */

import React, { createContext, useContext, useEffect, useState, useCallback } from 'react'
import { api, type User, isApiError, errorMessage } from '@/lib/api-client'

interface AuthState {
  user: User | null
  organizationId: string | null
  loading: boolean  // true during the initial me() check
  error: string | null  // last error from login/register, or null
}

interface AuthContextValue extends AuthState {
  login: (email: string, password: string) => Promise<{ ok: boolean; error?: string }>
  register: (body: { email: string; password: string; name: string; organizationName?: string }) => Promise<{ ok: boolean; error?: string }>
  logout: () => Promise<void>
  clearError: () => void
  refresh: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue>({
  user: null,
  organizationId: null,
  loading: true,
  error: null,
  login: async () => ({ ok: false, error: 'AuthProvider not mounted' }),
  register: async () => ({ ok: false, error: 'AuthProvider not mounted' }),
  logout: async () => {},
  clearError: () => {},
  refresh: async () => {},
})

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AuthState>({
    user: null,
    organizationId: null,
    loading: true,
    error: null,
  })

  // On mount: ask the backend who we are (cookies are sent automatically).
  const refresh = useCallback(async () => {
    try {
      const { user, organizationId } = await api.auth.me()
      setState({ user, organizationId: organizationId ?? null, loading: false, error: null })
    } catch (e) {
      // 401 is the expected case when logged out — not an error to surface.
      if (isApiError(e) && e.status === 401) {
        setState({ user: null, organizationId: null, loading: false, error: null })
      } else {
        // Network or server error — keep loading false but log it.
        setState({ user: null, organizationId: null, loading: false, error: null })
        if (typeof console !== 'undefined') console.warn('auth/me failed:', errorMessage(e))
      }
    }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  const login = useCallback(async (email: string, password: string) => {
    setState(s => ({ ...s, error: null }))
    try {
      const { user, organizationId } = await api.auth.login({ email, password })
      setState({ user, organizationId: organizationId ?? null, loading: false, error: null })
      return { ok: true }
    } catch (e) {
      const msg = errorMessage(e, 'Login failed')
      setState(s => ({ ...s, error: msg }))
      return { ok: false, error: msg }
    }
  }, [])

  const register = useCallback(async (body: { email: string; password: string; name: string; organizationName?: string }) => {
    setState(s => ({ ...s, error: null }))
    try {
      const { user, organizationId } = await api.auth.register(body)
      setState({ user, organizationId: organizationId ?? null, loading: false, error: null })
      return { ok: true }
    } catch (e) {
      const msg = errorMessage(e, 'Registration failed')
      setState(s => ({ ...s, error: msg }))
      return { ok: false, error: msg }
    }
  }, [])

  const logout = useCallback(async () => {
    try { await api.auth.logout() } catch { /* ignore — we clear local state regardless */ }
    setState({ user: null, organizationId: null, loading: false, error: null })
  }, [])

  const clearError = useCallback(() => setState(s => ({ ...s, error: null })), [])

  return (
    <AuthContext.Provider value={{ ...state, login, register, logout, clearError, refresh }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  return useContext(AuthContext)
}
