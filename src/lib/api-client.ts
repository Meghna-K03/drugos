/**
 * DrugOS Frontend API Client
 *
 * A thin wrapper around `fetch` that:
 *  - Sends cookies with every request (`credentials: 'include'`)
 *  - Normalizes error shapes from the backend
 *  - Provides typed methods for every backend endpoint
 *  - Auto-refreshes the access token once on 401
 *
 * This is the SINGLE source of truth for backend calls from the React layer.
 * Components should NEVER call `fetch('/api/...')` directly — they should
 * use this client so that auth, error handling, and types stay consistent.
 */

// ---------------------------------------------------------------------------
// Types — mirror the backend's response shapes
// ---------------------------------------------------------------------------

export interface User {
  id: string;
  email: string;
  name: string | null;
  role: string;
}

export interface AuthResponse {
  user: User;
  organizationId?: string;
}

export interface RxNormResult {
  rxcui: string;
  name: string;
  tty: string;
}

export interface MeshResult {
  descriptorUi: string;
  label: string;
  scope?: string;
}

export interface ClinicalTrial {
  nctId: string;
  title: string;
  status: string;
  phase?: string;
  conditions: string[];
  interventions: string[];
  sponsor?: string;
  startDate?: string;
  completionDate?: string;
  enrollment?: number;
  lastUpdatePostedDate?: string;
  studyType?: string;
}

export interface PubMedArticle {
  pmid: string;
  title: string;
  authors: string[];
  journal?: string;
  pubDate?: string;
  abstract?: string;
  doi?: string;
}

export interface OpenFdaAdverseEvent {
  safetyDisclaimer: string;
  drug: string;
  totalReports: number;
  seriousReports: number;
  seriousOutcomes: { outcome: string; count: number }[];
  topReactions: { term: string; count: number }[];
  source: string;
  generatedAt: string;
}

export interface PatentResult {
  patentId: string;
  title: string;
  applicants: string[];
  grantedDate?: string;
  abstract?: string;
}

export interface EvidencePackage {
  id: string;
  drugName: string;
  diseaseName: string;
  createdAt: string;
  literatureCount: number;
  trialsCount: number;
  safetyReportsCount: number;
  markdown: string;
}

export interface Project {
  id: string;
  name: string;
  description?: string;
  status: string;
  createdAt: string;
  updatedAt: string;
  hypotheses?: Hypothesis[];
  comments?: ProjectComment[];
}

export interface Hypothesis {
  id: string;
  drugName: string;
  diseaseName: string;
  rationale?: string;
  status: string;
  createdAt: string;
}

export interface ProjectComment {
  id: string;
  authorName: string;
  body: string;
  createdAt: string;
}

export interface Subscription {
  id: string;
  plan: string;
  status: string;
  seats: number;
  currentPeriodStart: string;
  currentPeriodEnd: string;
}

export interface Invoice {
  id: string;
  number: string;
  amountCents: number;
  currency: string;
  status: string;
  createdAt: string;
  periodStart: string;
  periodEnd: string;
}

export interface Plan {
  id: string;
  name: string;
  price: number;
  currency: string;
  interval: string;
  features: string[];
}

export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  lastUsedAt: string | null;
  createdAt: string;
  revokedAt: string | null;
}

export interface ApiKeyWithSecret extends ApiKey {
  /** The full plaintext key — shown ONCE at creation time. */
  plaintextKey: string;
}

export interface Notification {
  id: string;
  title: string;
  body: string;
  type: string;
  read: boolean;
  createdAt: string;
}

export interface AdminUser {
  id: string;
  email: string;
  name: string | null;
  role: string;
  status: string;
  createdAt: string;
  lastLoginAt: string | null;
}

export interface AuditLog {
  id: string;
  actorName: string;
  action: string;
  resource: string | null;
  createdAt: string;
  metadata: string;
}

export interface ServiceStatus {
  services: Record<string, {
    available: boolean;
    service: string;
    description?: string;
    reason?: string;
  }>;
  generatedAt: string;
}

export interface ApiError {
  error: string;
  message: string;
  status: number;
}

// ---------------------------------------------------------------------------
// Core fetch wrapper
// ---------------------------------------------------------------------------

const BASE_URL =
  typeof window !== "undefined"
    ? ""  // relative URLs from the browser hit the same origin
    : (process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:3000");

let refreshing: Promise<boolean> | null = null;

async function refreshOnce(): Promise<boolean> {
  if (refreshing) return refreshing;
  refreshing = (async () => {
    try {
      const res = await fetch(`${BASE_URL}/api/auth/refresh`, {
        method: "POST",
        credentials: "include",
      });
      return res.ok;
    } catch {
      return false;
    } finally {
      refreshing = null;
    }
  })();
  return refreshing;
}

async function request<T>(
  method: "GET" | "POST" | "PATCH" | "DELETE" | "PUT",
  path: string,
  opts: {
    body?: unknown;
    query?: Record<string, string | number | boolean | undefined>;
    /** Set to true to skip the auto-refresh-on-401 behavior (used by /refresh itself). */
    skipRefresh?: boolean;
  } = {}
): Promise<T> {
  const url = new URL(`${BASE_URL}${path}`, BASE_URL ? undefined : "http://localhost");
  if (opts.query) {
    for (const [k, v] of Object.entries(opts.query)) {
      if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v));
    }
  }

  const init: RequestInit = {
    method,
    credentials: "include",
    headers: { "Content-Type": "application/json", "Accept": "application/json" },
  };
  if (opts.body !== undefined) init.body = JSON.stringify(opts.body);

  let res = await fetch(url.toString(), init);

  // If we got a 401 and haven't already refreshed, try refreshing the access
  // token once and replay the request.
  if (res.status === 401 && !opts.skipRefresh) {
    const refreshed = await refreshOnce();
    if (refreshed) {
      res = await fetch(url.toString(), init);
    }
  }

  if (!res.ok) {
    let body: { error?: string; message?: string } = {};
    try { body = await res.json(); } catch { /* non-JSON error */ }
    const err: ApiError = {
      error: body.error || "request_failed",
      message: body.message || `Request to ${path} failed with status ${res.status}`,
      status: res.status,
    };
    throw err;
  }

  // 204 No Content
  if (res.status === 204) return undefined as unknown as T;
  return (await res.json()) as T;
}

// ---------------------------------------------------------------------------
// Public API surface
// ---------------------------------------------------------------------------

export const api = {
  // === Auth ===
  auth: {
    register: (body: { email: string; password: string; name: string; organizationName?: string }) =>
      request<AuthResponse>("POST", "/api/auth/register", { body }),

    login: (body: { email: string; password: string }) =>
      request<AuthResponse>("POST", "/api/auth/login", { body }),

    logout: () =>
      request<{ ok: true }>("POST", "/api/auth/logout", { skipRefresh: true }),

    refresh: () =>
      request<{ ok: true }>("POST", "/api/auth/refresh", { skipRefresh: true }),

    me: () =>
      request<{ user: User; organizationId?: string }>("GET", "/api/auth/me"),
  },

  // === Biomedical data ===
  drugs: {
    search: (q: string, limit = 10) =>
      request<{ query: string; results: RxNormResult[] }>("GET", "/api/drugs/search", {
        query: { q, limit },
      }),
  },

  diseases: {
    search: (q: string, limit = 10) =>
      request<{ query: string; results: MeshResult[] }>("GET", "/api/diseases/search", {
        query: { q, limit },
      }),
  },

  clinicalTrials: {
    search: (params: { condition?: string; intervention?: string; q?: string; limit?: number }) =>
      request<{ trials: ClinicalTrial[]; count: number }>("GET", "/api/clinical-trials/search", {
        query: params,
      }),
  },

  literature: {
    search: (q: string, limit = 10) =>
      request<{ articles: PubMedArticle[]; count: number }>("GET", "/api/literature/search", {
        query: { q, limit },
      }),
  },

  safety: {
    get: (drug: string) =>
      request<OpenFdaAdverseEvent>("GET", `/api/safety/${encodeURIComponent(drug)}`),
  },

  patents: {
    search: (q: string, limit = 10) =>
      request<{ patents: PatentResult[]; count: number }>("GET", "/api/patents/search", {
        query: { q, limit },
      }),
  },

  evidence: {
    list: () =>
      request<{ packages: EvidencePackage[] }>("GET", "/api/evidence-package"),
    create: (body: { drugName: string; diseaseName: string }) =>
      request<EvidencePackage>("POST", "/api/evidence-package", { body }),
  },

  // === Projects & collaboration ===
  projects: {
    list: () =>
      request<{ projects: Project[] }>("GET", "/api/projects"),
    create: (body: { name: string; description?: string }) =>
      request<{ project: Project }>("POST", "/api/projects", { body }),
    get: (id: string) =>
      request<{ project: Project }>("GET", `/api/projects/${id}`),
    addHypothesis: (id: string, body: { drugName: string; diseaseName: string; rationale?: string }) =>
      request<{ hypothesis: Hypothesis }>("POST", `/api/projects/${id}`, { body }),
    addComment: (id: string, body: { body: string }) =>
      request<{ comment: ProjectComment }>("POST", `/api/projects/${id}/comments`, { body }),
  },

  // === Billing ===
  billing: {
    plans: () =>
      request<{ plans: Plan[] }>("GET", "/api/billing/plans"),
    subscription: () =>
      request<{ subscription: Subscription | null }>("GET", "/api/billing/subscription"),
    changePlan: (planId: string) =>
      request<{ subscription: Subscription }>("POST", "/api/billing/subscription", { body: { planId } }),
    invoices: () =>
      request<{ invoices: Invoice[] }>("GET", "/api/billing/invoices"),
  },

  // === Developer platform ===
  apiKeys: {
    list: () =>
      request<{ keys: ApiKey[] }>("GET", "/api/api-keys"),
    create: (body: { name: string }) =>
      request<{ key: ApiKeyWithSecret }>("POST", "/api/api-keys", { body }),
    revoke: (id: string) =>
      request<{ ok: true }>("POST", `/api/api-keys/${id}/revoke`),
  },

  // === Admin ===
  admin: {
    users: () =>
      request<{ users: AdminUser[] }>("GET", "/api/admin/users"),
    auditLogs: (limit = 50) =>
      request<{ logs: AuditLog[] }>("GET", "/api/audit-logs", { query: { limit } }),
  },

  // === Notifications ===
  notifications: {
    list: () =>
      request<{ notifications: Notification[] }>("GET", "/api/notifications"),
    markRead: (id: string) =>
      request<{ ok: true }>("POST", `/api/notifications/${id}/read`),
  },

  // === System ===
  system: {
    status: () =>
      request<ServiceStatus>("GET", "/api/system/status"),
  },

  // === ML stubs (return 503 — surfaced as errors with explanatory messages) ===
  ml: {
    knowledgeGraph: () =>
      request<{ data: unknown }>("GET", "/api/knowledge-graph"),
    dataset: () =>
      request<{ data: unknown }>("GET", "/api/dataset"),
    rl: (body: unknown) =>
      request<{ data: unknown }>("POST", "/api/rl", { body }),
  },
};

// ---------------------------------------------------------------------------
// Error helpers
// ---------------------------------------------------------------------------

export function isApiError(e: unknown): e is ApiError {
  return typeof e === "object" && e !== null && "error" in e && "status" in e;
}

export function errorMessage(e: unknown, fallback = "Something went wrong"): string {
  if (isApiError(e)) return e.message;
  if (e instanceof Error) return e.message;
  return fallback;
}
