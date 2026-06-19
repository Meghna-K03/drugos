'use client'

/**
 * Data hooks for DrugOS screens.
 *
 * Each hook wraps a `useEffect + fetch` pattern with proper loading/error state.
 * Components consume these hooks instead of importing mock-data.ts.
 */

import { useEffect, useState, useCallback } from 'react'
import { api, isApiError, errorMessage, type ApiError } from '@/lib/api-client'

interface DataState<T> {
  data: T | null
  loading: boolean
  error: string | null
  refetch: () => Promise<void>
}

function useData<T>(fetcher: () => Promise<T>, deps: unknown[] = []): DataState<T> {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const refetch = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const result = await fetcher()
      setData(result)
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  useEffect(() => { refetch() }, [refetch])

  return { data, loading, error, refetch }
}

// ---------------------------------------------------------------------------
// Individual hooks
// ---------------------------------------------------------------------------

export function useSystemStatus() {
  return useData(() => api.system.status(), [])
}

export function useDrugsSearch(query: string, limit = 10) {
  return useData(
    () => query.trim().length >= 2 ? api.drugs.search(query, limit) : Promise.resolve({ query, results: [] }),
    [query, limit]
  )
}

export function useDiseasesSearch(query: string, limit = 10) {
  return useData(
    () => query.trim().length >= 2 ? api.diseases.search(query, limit) : Promise.resolve({ query, results: [] }),
    [query, limit]
  )
}

export function useClinicalTrialsSearch(params: { condition?: string; intervention?: string; q?: string; limit?: number }) {
  const { condition, intervention, q, limit = 10 } = params
  const hasQuery = !!(condition || intervention || q)
  return useData(
    () => hasQuery ? api.clinicalTrials.search({ condition, intervention, q, limit }) : Promise.resolve({ trials: [], count: 0 }),
    [condition, intervention, q, limit]
  )
}

export function useLiteratureSearch(query: string, limit = 10) {
  return useData(
    () => query.trim().length >= 2 ? api.literature.search(query, limit) : Promise.resolve({ articles: [], count: 0 }),
    [query, limit]
  )
}

export function useSafetyReport(drug: string | null) {
  return useData(
    () => drug ? api.safety.get(drug) : Promise.resolve(null),
    [drug]
  )
}

export function usePatentsSearch(query: string, limit = 10) {
  return useData(
    () => query.trim().length >= 2 ? api.patents.search(query, limit) : Promise.resolve({ patents: [], count: 0 }),
    [query, limit]
  )
}

export function useEvidencePackages() {
  return useData(() => api.evidence.list(), [])
}

export function useProjects() {
  return useData(() => api.projects.list(), [])
}

export function useProject(id: string | null) {
  return useData(
    () => id ? api.projects.get(id) : Promise.resolve({ project: null as never }),
    [id]
  )
}

export function useBillingPlans() {
  return useData(() => api.billing.plans(), [])
}

export function useSubscription() {
  return useData(() => api.billing.subscription(), [])
}

export function useInvoices() {
  return useData(() => api.billing.invoices(), [])
}

export function useApiKeys() {
  return useData(() => api.apiKeys.list(), [])
}

export function useNotifications() {
  return useData(() => api.notifications.list(), [])
}

export function useAdminUsers() {
  return useData(() => api.admin.users(), [])
}

export function useAuditLogs(limit = 50) {
  return useData(() => api.admin.auditLogs(limit), [limit])
}

// ---------------------------------------------------------------------------
// Mutation helpers (return async fns that also trigger a refetch)
// ---------------------------------------------------------------------------

export function useMutation<TArgs extends unknown[], TResult>(
  fn: (...args: TArgs) => Promise<TResult>
) {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<TResult | null>(null)

  const mutate = useCallback(async (...args: TArgs): Promise<{ ok: boolean; data?: TResult; error?: string }> => {
    setLoading(true)
    setError(null)
    try {
      const data = await fn(...args)
      setResult(data)
      return { ok: true, data }
    } catch (e) {
      const msg = errorMessage(e)
      setError(msg)
      return { ok: false, error: msg }
    } finally {
      setLoading(false)
    }
  }, [fn])

  return { mutate, loading, error, result }
}

export { isApiError, type ApiError }
