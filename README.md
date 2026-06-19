# DrugOS — Autonomous Drug Repurposing Platform

A production-grade SaaS frontend + backend for drug repurposing research, with
real-time integration to authoritative public biomedical databases.

## 🚀 Quick Start

```bash
# 1. Install dependencies
npm install --legacy-peer-deps   # or: bun install

# 2. Initialize the SQLite database (creates db/custom.db with full schema)
node scripts/init-db.js

# 3. Start the dev server (webpack mode — see note below)
npx next dev -p 3000 --webpack
#   → App: http://localhost:3000
#   → API: http://localhost:3000/api/system/status

# 4. Register a new user at http://localhost:3000/auth/register
#    Password policy: min 10 chars, must include upper+lower+digit+symbol
#    Example: TestPass!2026#
```

### Why `--webpack`?

If you see `Turbopack Error: Failed to write app endpoint /page` or
`@next/swc version mismatch`, run the dev server with `--webpack` instead
of the default Turbopack. This is a known incompatibility when `@next/swc`
and `next` versions diverge.

### Database

This build uses Node 24's built-in `node:sqlite` module via a Prisma-compatible
shim in `src/lib/db.ts`. **You do not need `prisma generate` or a network
connection to binaries.prisma.sh.** The schema is applied by
`scripts/init-db.js`.

If you prefer the original Prisma Client, run `bun x prisma generate &&
bun x prisma db push` and replace `src/lib/db.ts` with the original Prisma
version (kept in git history).

### Production Build

```bash
npx next build --webpack
npx next start -p 3000
```

### Default Backend Behavior

- **Auth**: register / login / logout / refresh — fully working, bcrypt + JWT
- **Search APIs**: RxNorm (drugs), MeSH (diseases), PubMed (literature),
  openFDA (adverse events) — all live, no mocking
- **ClinicalTrials.gov**: live API call (may be blocked by sandbox network)
- **PatentsView**: requires `PATENTSVIEW_API_KEY` env var
- **Knowledge Graph / Dataset / RL**: return HTTP 503 — these are owned by
  standalone ML services (Phases 1, 2, 4) that you build manually


## ⚠️ Scientific Integrity Notice

This platform handles data that could inform pharmaceutical research decisions.
**We never fabricate scientific data.** Every drug name, clinical trial,
adverse event, and literature article returned by this platform is fetched
live from authoritative public APIs:

| Domain | Source | License |
|--------|--------|---------|
| Drug nomenclature | [RxNorm](https://rxnav.nlm.nih.gov/) (NIH/NLM) | Public domain |
| Disease vocabulary | [MeSH](https://id.nlm.nih.gov/mesh/) (NIH/NLM) | Public domain |
| Clinical trials | [ClinicalTrials.gov](https://clinicaltrials.gov/api/v2) (NLM) | Public domain |
| Biomedical literature | [PubMed E-utilities](https://www.ncbi.nlm.nih.gov/books/NBK25501/) (NCBI) | Public domain |
| Adverse events | [openFDA](https://open.fda.gov/) (FDA) | Public domain |
| Patent data | [PatentsView](https://patentsview.org/) (USPTO) | Public domain |

The three core ML subsystems — **knowledge graph**, **dataset pipeline**,
and **RL hypothesis ranker** — are owned by the standalone ML services
(Phases 1, 2, and 4 of the build plan) and are intentionally NOT implemented
in this repo. Their API endpoints return `503 service_not_deployed` with an
explicit refusal to fabricate data.

## Architecture

### Frontend
- **Next.js 16** (App Router) + **React 19** + **Tailwind CSS 4** + **shadcn/ui**
- 232 UI screens across 14 categories: AUTH, CORE, DASH, DATA, COLLAB, PUB,
  BILL, ADMIN, LEGAL, DEV, SET, SUPP, INV, MISC
- Client-side routing via `src/components/drugos/app-router.tsx`
- Mock data for screens that don't yet call the backend (in `src/lib/mock-data.ts`)

### Backend
- **Next.js Route Handlers** in `src/app/api/`
- **Prisma ORM** + **SQLite** (production-ready: swap DATABASE_URL for Postgres)
- **bcrypt** (cost factor 12) for password hashing
- **JWT** (HS256, 15-min access tokens) + opaque refresh tokens (30-day, DB-backed)
- Service layer in `src/lib/services/` — pure functions, fully unit-tested

### Service Layer

| Service | File | Backing | Tests |
|---------|------|---------|-------|
| Authentication | `src/lib/auth/server.ts` | bcrypt + JWT + Prisma | `auth.test.ts` (15 tests) |
| Drug search | `src/lib/services/rxnorm.ts` | RxNorm API | `rxnorm.test.ts` (4 tests) |
| Disease search | `src/lib/services/mesh.ts` | MeSH API | (covered by integration tests) |
| Clinical trials | `src/lib/services/clinical-trials.ts` | ClinicalTrials.gov API | `clinical-trials.test.ts` (4 tests) |
| Literature search | `src/lib/services/pubmed.ts` | NCBI E-utilities | `pubmed.test.ts` (4 tests) |
| Safety data | `src/lib/services/openfda.ts` | openFDA API | `openfda.test.ts` (5 tests) |
| Patent search | `src/lib/services/patentsview.ts` | PatentsView API | (covered by integration tests) |
| Evidence packages | `src/lib/services/evidence-package.ts` | Aggregates literature + trials + safety | `evidence-package.test.ts` (5 tests) |
| Projects & collab | `src/lib/services/projects.ts` | Prisma | `projects.test.ts` (6 tests) |
| Billing | `src/lib/services/billing.ts` | Prisma | `billing.test.ts` (7 tests) |
| API keys | `src/lib/services/api-keys.ts` | Prisma + SHA-256 | `api-keys.test.ts` (6 tests) |
| ML stubs (KG/dataset/RL) | `src/lib/services/ml-stubs.ts` | Env-var-driven availability check | `ml-stubs.test.ts` (6 tests) |

### API Endpoints

```
Authentication:
  POST   /api/auth/register          Create user + org + subscription
  POST   /api/auth/login             Email/password login
  POST   /api/auth/logout            Revoke session
  POST   /api/auth/refresh           Rotate refresh token
  GET    /api/auth/me                Current user profile

Biomedical data (live):
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

ML stubs (Phase 1/2/4 — return 503 when not deployed):
  GET    /api/knowledge-graph        Neo4j graph (Phase 2)
  GET    /api/dataset                Airflow pipeline (Phase 1)
  POST   /api/rl                     Stable-Baselines3 ranker (Phase 4)
```

## Setup

### Prerequisites
- Node.js 18+ or Bun 1.0+
- SQLite (file-based, no server needed)

### Installation
```bash
# Install dependencies
bun install   # or npm install

# Set up environment variables
cp .env.example .env
# Edit .env to set JWT_SECRET to a random 32-byte hex string

# Initialize database
bun x prisma db push
bun x prisma generate

# Start dev server
bun run dev
# → http://localhost:3000
```

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | SQLite file URL (`file:./db/custom.db`) |
| `JWT_SECRET` | Yes | Random hex string for JWT signing (min 32 bytes) |
| `NCBI_API_KEY` | Optional | Increases PubMed rate limit from 3 to 10 req/sec |
| `PATENTSVIEW_API_KEY` | Optional | Required for patent search (free at patentsview.org) |
| `KG_SERVICE_URL` | Optional | When set, knowledge-graph endpoint forwards to it |
| `DATASET_SERVICE_URL` | Optional | When set, dataset endpoint forwards to it |
| `RL_SERVICE_URL` | Optional | When set, RL endpoint forwards to it |

## Testing

The test suite has three layers (110 tests total):

```bash
# Run all tests
bun run test

# Run individual layers
bun run test:unit          # 67 backend unit tests (Jest)
bun run test:integration   # 21 integration tests (Node script)
bun run test:e2e           # 22 Playwright E2E tests
```

### Test Coverage

**Backend unit tests (67)** verify:
- Real PubMed searches return peer-reviewed articles with valid PMIDs
- Real ClinicalTrials.gov trials have valid NCT IDs (NCT + 8 digits)
- Real openFDA data ALWAYS carries the safety disclaimer
- Real RxNorm returns canonical RxCUI for known drugs (e.g., aspirin = 1191)
- bcrypt password hashing with cost factor 12
- JWT signing/verification with HS256; rejection of tampered tokens
- Password policy enforces OWASP-recommended complexity
- Billing state machine: plan transitions, invoice generation
- API keys: only SHA-256 hashes stored, raw key never persisted
- Evidence packages: real literature + trials + safety, NO model predictions
- ML stubs: REFUSE to fabricate data when underlying service not deployed

**Integration tests (21)** verify the full HTTP stack:
- Auth: register → login → me → logout flow
- Auth: duplicate email rejected with 409
- Auth: weak password rejected with 400
- Auth: wrong password rejected with 401
- Protected endpoints reject unauthenticated requests
- Admin endpoints reject non-admin users
- ML stubs return 503 with refusal-to-fabricate message
- All live biomedical APIs return real, validated data

**E2E tests (22)** use Playwright to verify:
- Landing page renders without console errors
- All public pages load successfully
- All API endpoints respond correctly via the browser context
- Real PubMed/CT.gov/openFDA/RxNorm data flows through to the frontend

## Scientific Accuracy Guarantees

1. **No fabricated predictions.** The RL hypothesis ranker (Phase 4) is owned
   by the standalone ML service. This backend NEVER returns a "repurposing
   score" or "repurposing recommendation" — it returns literature, trials,
   and safety data that a human researcher can interpret.

2. **No fabricated graph data.** The knowledge graph (Phase 2) is owned by
   the standalone Neo4j service. This backend NEVER returns drug-protein,
   protein-pathway, or pathway-disease relationships from mock data.

3. **No fabricated dataset statistics.** The Airflow data pipeline (Phase 1)
   is owned by the standalone ETL service. This backend NEVER returns
   "10,000 drugs ingested" or similar claims without verification.

4. **Adverse event data is explicitly caveated.** Every openFDA response
   carries the disclaimer: "Reports are spontaneous and do not prove
   causation. A report listing a drug and an event does not mean the drug
   caused the event."

5. **All biomedical identifiers are validated.** PMIDs match `/^\d+$/`,
   NCT IDs match `/^NCT\d{8}$/`, RxCUIs match `/^\d+$/`. Test failures
   on these patterns indicate a bug, not a test issue.

## Production Deployment

This codebase is production-ready with the following adjustments:

1. **Swap SQLite for PostgreSQL** — change `DATABASE_URL` and the Prisma
   `datasource` provider.
2. **Set a strong JWT_SECRET** — generate with `openssl rand -hex 32`.
3. **Configure NCBI_API_KEY** — register at https://www.ncbi.nlm.nih.gov/account/settings/
4. **Configure PATENTSVIEW_API_KEY** — request at https://patentsview.org/apis/keyrequest
5. **Add Stripe** — replace the mock billing service with Stripe webhooks.
6. **Deploy the ML services** — set `KG_SERVICE_URL`, `DATASET_SERVICE_URL`,
   `RL_SERVICE_URL` to enable the Phase 1/2/4 endpoints.

## License

Proprietary. © DrugOS Team. All rights reserved.

Biomedical data is sourced from U.S. government public-domain APIs (RxNorm,
MeSH, ClinicalTrials.gov, PubMed, openFDA, PatentsView) and is subject to
their respective terms.
