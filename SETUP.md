# DrugOS — Complete Setup Guide

This guide explains how to install, run, and test the entire DrugOS platform
(frontend + backend + tests) from the ZIP file you just downloaded.

---

## 1. What's in the ZIP

```
drugos/
├── src/                          # Frontend + backend source code
│   ├── app/                      # Next.js App Router
│   │   ├── api/                  # 29 backend API route handlers
│   │   ├── layout.tsx
│   │   └── page.tsx
│   ├── components/               # React UI components
│   │   ├── drugos/               # DrugOS screens (232 total)
│   │   ├── layout/
│   │   └── ui/                   # shadcn/ui primitives
│   ├── hooks/
│   └── lib/
│       ├── auth/                 # bcrypt + JWT auth
│       ├── services/             # 11 backend services (RxNorm, PubMed, etc.)
│       │   └── __tests__/        # 67 Jest unit tests
│       └── ...
├── prisma/
│   └── schema.prisma             # 14 Prisma models
├── tests/
│   ├── api/                      # Jest setup
│   └── e2e/                      # 22 Playwright E2E tests
├── scripts/
│   ├── run-all-tests.sh          # Run all 110 tests
│   ├── run-integration-tests.js  # 21 integration tests
│   ├── run-e2e-tests.js          # 22 E2E tests
│   └── create-zip.py             # Re-build the ZIP
├── public/                       # Static assets
├── .env.example                  # Copy to .env and fill in
├── package.json
├── tsconfig.json
├── tailwind.config.ts
├── jest.config.js
├── playwright.config.ts
├── README.md                     # Project overview
└── SETUP.md                      # This file
```

---

## 2. Prerequisites

- **Node.js 18+** or **Bun 1.0+** (Bun recommended — installs faster)
- **SQLite** (file-based — no server needed; bundled with Node)
- Internet access (for live biomedical API calls: RxNorm, PubMed,
  ClinicalTrials.gov, openFDA, PatentsView)

---

## 3. Installation

```bash
# 1. Unzip
unzip drugos_complete.zip
cd drugos

# 2. Install dependencies
bun install          # or: npm install

# 3. Configure environment
cp .env.example .env
# Edit .env and set JWT_SECRET to a random 32-byte hex string:
#   openssl rand -hex 32
# (DATABASE_URL is already set to a SQLite file path; leave as-is for dev.)

# 4. Initialize the database (creates db/custom.db from schema.prisma)
bun x prisma db push
bun x prisma generate

# 5. Start the dev server
bun run dev
# → http://localhost:3000
```

---

## 4. Verify the Installation

Open `http://localhost:3000/api/system/status` in your browser. You should
see a JSON object listing all backend services with their availability:

- `auth`, `rxnorm`, `mesh`, `clinicalTrials`, `pubmed`, `openfda`,
  `projects`, `billing`, `admin`, `apiKeys`, `evidence` → `available: true`
- `knowledgeGraph`, `dataset`, `rl` → `available: false` with the
  explicit "service not deployed" reason (these are intentionally NOT
  implemented — see the Scientific Integrity Notice in README.md)
- `patentsview` → `available: false` UNLESS you set `PATENTSVIEW_API_KEY`
  in `.env` (free key from https://patentsview.org/apis/keyrequest)

---

## 5. Running the Tests

The test suite has three layers and 110 tests in total:

```bash
# Run ALL 110 tests (unit + integration + E2E)
bun run test

# Or run each layer separately:
bun run test:unit          # 67 Jest unit tests (no server needed)
bun run test:integration   # 21 HTTP integration tests (needs dev server)
bun run test:e2e           # 22 Playwright E2E tests (needs dev server)
```

The integration and E2E test runners will automatically detect an
already-running dev server on `http://localhost:3000` and reuse it. If
no server is detected, they start their own on a different port.

Expected output:

```
=== Unit Tests ===
Test Suites: 10 passed, 10 total
Tests:       67 passed, 67 total

=== Integration Tests ===
Results: 21 passed, 0 failed

=== E2E Tests ===
22 passed
```

---

## 6. Environment Variables (reference)

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | SQLite file URL (`file:./db/custom.db`) |
| `JWT_SECRET` | Yes | Random hex string for JWT signing (≥32 bytes) |
| `NCBI_API_KEY` | Optional | Increases PubMed rate limit from 3 to 10 req/sec |
| `PATENTSVIEW_API_KEY` | Optional | Required for patent search |
| `KG_SERVICE_URL` | Optional | When set, `/api/knowledge-graph` forwards to it |
| `DATASET_SERVICE_URL` | Optional | When set, `/api/dataset` forwards to it |
| `RL_SERVICE_URL` | Optional | When set, `/api/rl` forwards to it |

---

## 7. API Quick Reference

All endpoints are under `/api/`. Authentication uses HTTP-only cookies
set by `/api/auth/login` and `/api/auth/register`.

```
Authentication:
  POST   /api/auth/register          Create user + org + subscription
  POST   /api/auth/login             Email/password login
  POST   /api/auth/logout            Revoke session
  POST   /api/auth/refresh           Rotate refresh token
  GET    /api/auth/me                Current user profile

Biomedical data (live, real APIs):
  GET    /api/drugs/search?q=        RxNorm drug lookup
  GET    /api/diseases/search?q=     MeSH disease lookup
  GET    /api/clinical-trials/search ClinicalTrials.gov
  GET    /api/literature/search?q=   PubMed E-utilities
  GET    /api/safety/[drug]          openFDA adverse events
  GET    /api/patents/search?q=      USPTO PatentsView

Evidence packages:
  GET    /api/evidence-package       List user's packages
  POST   /api/evidence-package       Build new (drug, disease) package

Projects & collaboration:
  GET    /api/projects               List org projects
  POST   /api/projects               Create project
  GET    /api/projects/[id]          Get project + hypotheses + comments + activity
  POST   /api/projects/[id]          Add hypothesis
  POST   /api/projects/[id]/comments Add comment

Billing:
  GET    /api/billing/plans          Plan catalog
  GET    /api/billing/subscription   Current subscription
  POST   /api/billing/subscription   Change plan
  GET    /api/billing/invoices       Invoice history

Developer platform:
  GET    /api/api-keys               List active keys
  POST   /api/api-keys               Issue new key (returns raw key once)
  POST   /api/api-keys/[id]/revoke   Revoke key

Admin:
  GET    /api/admin/users            List users (admin only)
  PATCH  /api/admin/users            Update user role/status (admin only)
  GET    /api/audit-logs             Audit trail (admin only)

Notifications & system:
  GET    /api/notifications          User notifications
  POST   /api/notifications/[id]/read  Mark as read
  GET    /api/system/status          All services availability

ML stubs (return 503 with refusal-to-fabricate message):
  GET    /api/knowledge-graph        Neo4j graph (Phase 2 — user-owned)
  GET    /api/dataset                Airflow pipeline (Phase 1 — user-owned)
  POST   /api/rl                     Stable-Baselines3 ranker (Phase 4 — user-owned)
```

---

## 8. Scientific Integrity Guarantees

This platform handles data that could inform pharmaceutical research
decisions. We never fabricate scientific data:

1. **No fabricated predictions.** The RL ranker is owned by the
   standalone ML service. This backend NEVER returns a "repurposing score"
   or "repurposing recommendation" — only literature, trials, and safety
   data that a human researcher can interpret.

2. **No fabricated graph data.** The knowledge graph is owned by the
   standalone Neo4j service. This backend NEVER returns drug-protein,
   protein-pathway, or pathway-disease relationships from mock data.

3. **No fabricated dataset statistics.** The Airflow data pipeline is
   owned by the standalone ETL service. This backend NEVER returns
   "10,000 drugs ingested" or similar claims without verification.

4. **Adverse event data is explicitly caveated.** Every openFDA response
   carries: "Reports are spontaneous and do not prove causation. A
   report listing a drug and an event does not mean the drug caused the
   event."

5. **All biomedical identifiers are validated.** PMIDs match `/^\d+$/`,
   NCT IDs match `/^NCT\d{8}$/`, RxCUIs match `/^\d+$/`. Tests fail
   loudly if these patterns are violated.

---

## 9. Production Deployment

The codebase is production-ready with these adjustments:

1. **Swap SQLite for PostgreSQL** — change `DATABASE_URL` and the
   Prisma `datasource` provider to `postgresql`.
2. **Set a strong `JWT_SECRET`** — `openssl rand -hex 32`.
3. **Configure `NCBI_API_KEY`** — register at
   https://www.ncbi.nlm.nih.gov/account/settings/
4. **Configure `PATENTSVIEW_API_KEY`** — request at
   https://patentsview.org/apis/keyrequest
5. **Add Stripe** — replace the mock billing service with Stripe
   webhooks (the billing service file is `src/lib/services/billing.ts`).
6. **Deploy the ML services** — set `KG_SERVICE_URL`,
   `DATASET_SERVICE_URL`, `RL_SERVICE_URL` to enable the Phase 1/2/4
   endpoints.

```bash
# Production build
bun run build

# Start production server
bun run start
```

---

## 10. License

Proprietary. © DrugOS Team. All rights reserved.

Biomedical data is sourced from U.S. government public-domain APIs
(RxNorm, MeSH, ClinicalTrials.gov, PubMed, openFDA, PatentsView) and is
subject to their respective terms.
