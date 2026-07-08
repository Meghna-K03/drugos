# PubChem Pipeline — Institutional-Grade Documentation

> **Pipeline:** `pipelines/pubchem_pipeline.py`
> **Source:** [PubChem PUG REST](https://pubchemdocs.ncbi.nlm.nih.gov/pug-rest)
> **Output:** `processed_data/pubchem_enrichment.csv` + `pubchem_compound_properties` table
> **Audit reference:** `pubchem_pipeline_forensic_audit.docx` (187 findings; 131 unique issues across 16 domains)
> **Fix prompt:** `PUBCHEM_PIPELINE_MASTER_FIX_PROMPT.md`

---

## 1. Overview

The PubChem pipeline enriches existing `drugs` table rows with physicochemical properties fetched from the PubChem PUG REST API. The output feeds the Phase 3 Graph Transformer's molecular fingerprinting module, which uses the structural descriptors to compute Tanimoto similarity between drugs.

**Why this pipeline exists in the platform:**

1. ChEMBL and DrugBank provide the initial drug records (InChIKey, name, clinical-trial phase, FDA approval status) but only sparse physicochemical data.
2. PubChem is the authoritative source for **canonical SMILES**, **isomeric SMILES** (with stereochemistry), **InChI**, **molecular formula**, **molecular weight**, **exact mass**, **XLogP** (predicted), **TPSA** (calculated), and a host of atom/bond counts.
3. The Graph Transformer's molecular fingerprints (Morgan / ECFP4) require the **isomeric SMILES** — losing stereochemistry would make (R)-thalidomide and (S)-thalidomide indistinguishable, with potentially fatal clinical-trial consequences.

**Life-safety context:** The downstream consumer is a Graph Transformer that predicts drug-disease repurposing hypotheses. Those hypotheses are surfaced to pharmaceutical partners and academic researchers via an API and web dashboard. If the dataset layer is wrong, the model is wrong. If the model is wrong, clinicians and biotech researchers may run clinical trials on the wrong enantiomer, the wrong salt form, or the wrong compound. People can die. Treat every line of `pubchem_pipeline.py` as if a patient's life depends on it.

---

## 2. Data Flow Diagram

```
                       ┌──────────────────────────────────────────────────┐
                       │              drugs (existing table)              │
                       │  inchikey | name | chembl_id | drugbank_id |     │
                       │  pubchem_cid (NULL) | molecular_formula | ...    │
                       └─────────────────────┬────────────────────────────┘
                                             │
                                             │ ORM query (download):
                                             │   WHERE pubchem_cid IS NULL
                                             │     AND inchikey IS NOT NULL
                                             │     AND is_deleted = FALSE
                                             │     AND inchikey ~ '^[A-Z]{14}-...'
                                             │   ORDER BY inchikey ASC
                                             │   LIMIT :max_records (optional)
                                             ▼
                                  ┌────────────────────────┐
                                  │ inchikeys_to_lookup.txt│
                                  │  + .sha256 sidecar     │
                                  │  (raw_data/pubchem/)   │
                                  └───────────┬────────────┘
                                              │
                                              │ Batch (95 InChIKeys / batch)
                                              ▼
              ┌────────────────────────────────────────────────────────┐
              │ PubChem PUG REST                                       │
              │ POST /compound/inchikey/property/<15 properties>/JSON  │
              │ Body: inchikey=IK1,IK2,...,IK95                        │
              │ Headers: User-Agent, Accept, Accept-Encoding           │
              │ TLS: ca_bundle, cert (optional)                        │
              └─────────────────────┬──────────────────────────────────┘
                                    │
                                    │ JSON response ({"PropertyTable":
                                    │  {"Properties": [{CID, InChIKey,
                                    │   MolecularFormula, ...}]}})
                                    ▼
              ┌────────────────────────────────────────────────────────┐
              │ raw_data/pubchem/pubchem_responses/batch_NNNN.json     │
              │  + .sha256 sidecar (per batch)                         │
              └─────────────────────┬──────────────────────────────────┘
                                    │
                                    │ clean() — pure transformation, NO HTTP
                                    │   * Verify response InChIKey == request
                                    │   * Sanitize empty strings → None
                                    │   * Convert floats → Decimal
                                    │   * Validate numeric ranges
                                    │   * Dedupe by InChIKey (lowest CID)
                                    │   * Extract protonation_state, isotope_info,
                                    │     formal_charge
                                    │   * Add lineage columns (source, source_id,
                                    │     source_version, download_date,
                                    │     pipeline_run_id, input_checksum,
                                    │     transformations)
                                    ▼
                  ┌──────────────────────────────────────────┐
                  │ processed_data/pubchem_enrichment.csv    │
                  │  + .sha256 sidecar                       │
                  │  + .run_context.json sidecar             │
                  │  (written by BasePipeline._persist_      │
                  │   cleaned_data — NOT by clean())         │
                  └─────────────────────┬────────────────────┘
                                        │
                                        │ load(df, session=...)
                                        │   Step 1: bulk_update_drugs_from_pubchem
                                        │     UPDATE drugs SET pubchem_cid=...,
                                        │       molecular_formula=COALESCE(...),
                                        │       molecular_weight=COALESCE(...),
                                        │       smiles=COALESCE(...)
                                        │     WHERE inchikey=:inchikey
                                        │       AND pubchem_cid IS NULL
                                        │   Step 2: bulk_upsert_pubchem_compound_properties
                                        │     INSERT INTO pubchem_compound_properties
                                        │     ON CONFLICT (inchikey, pubchem_cid)
                                        │     DO UPDATE SET ...
                                        ▼
              ┌──────────────────────────────────────────────────────────┐
              │ drugs (pubchem_cid, molecular_formula, molecular_weight,│
              │        smiles populated where they were NULL)            │
              │ pubchem_compound_properties (NEW table — migration 005)  │
              │   15+ physicochemical properties + lineage columns       │
              └──────────────────────────────────────────────────────────┘
```

---

## 3. Configuration

All configuration is env-var-driven via `config/settings.py`. **No hardcoded constants in the pipeline file.**

### PubChem pipeline-specific settings

| Env var | Default | Description |
|---------|---------|-------------|
| `PUBCHEM_PIPELINE_BATCH_SIZE` | `95` | InChIKeys per PubChem PUG REST batch. PubChem hard limit is 100; we use 95 for a 5% safety margin. |
| `PUBCHEM_PIPELINE_MIN_BACKOFF` | `2.0` | Minimum backoff (seconds) for retry on transient failures. Multiplied by `2^attempt`, capped at `MAX_BACKOFF`. |
| `PUBCHEM_PIPELINE_MAX_BACKOFF` | `32.0` | Maximum backoff (seconds) — caps exponential growth. |
| `PUBCHEM_PIPELINE_READ_TIMEOUT` | `30.0` | Read timeout for PubChem PUG REST. Connect timeout comes from `ENTITY_RESOLUTION_PUBCHEM_TIMEOUT` (default 10.0). |
| `PUBCHEM_PIPELINE_CACHE_TTL_SECONDS` | `3600` | Cache TTL for `inchikeys_to_lookup.txt`. Files older than this trigger a re-query. Set to 0 to disable caching. |
| `PUBCHEM_PIPELINE_CONCURRENCY` | `1` | Thread pool size for concurrent batch fetching. Default 1 (sequential) for determinism. Production may set to 5 (PubChem allows 5 req/sec). |
| `PUBCHEM_PIPELINE_FETCH_SYNONYMS` | `false` | When true, fetch PubChem synonyms and store as JSON array in `pubchem_compound_properties.synonyms`. |
| `PUBCHEM_PIPELINE_FETCH_CAS` | `false` | When true, fetch CAS Registry Number via the synonyms endpoint. Adds 1 extra HTTP call per resolved CID. |
| `PUBCHEM_PIPELINE_SPLIT_RETRY_MAX` | `20` | Maximum batch size for split-retry on permanent 4xx failures. Prevents 100 individual requests for a fully-bad batch. |
| `PUBCHEM_PIPELINE_MAX_RECORDS` | (none) | Maximum InChIKeys to enrich per run. None = unlimited. |
| `PUBCHEM_PIPELINE_RAW_RESPONSE_RETENTION_DAYS` | `90` | Retention for raw PubChem JSON responses. Older files eligible for cleanup. |
| `PUBCHEM_CIRCUIT_BREAKER_THRESHOLD` | `5` | Consecutive failures before the circuit breaker opens. |
| `PUBCHEM_CIRCUIT_BREAKER_RESET_SECONDS` | `60.0` | Cooldown before the breaker enters HALF_OPEN. |
| `PUBCHEM_PIPELINE_PROPERTIES` | (15 properties) | Comma-separated list of PubChem property names to fetch. |

### Reused settings (from `ENTITY_RESOLUTION_PUBCHEM_*` block)

| Env var | Default | Description |
|---------|---------|-------------|
| `ENTITY_RESOLUTION_PUBCHEM_REST_BASE` | `https://pubchem.ncbi.nlm.nih.gov/rest/pug` | PubChem PUG REST base URL. |
| `ENTITY_RESOLUTION_PUBCHEM_CALL_DELAY` | `0.2` | Rate-limit interval between batches (5 req/sec). |
| `ENTITY_RESOLUTION_PUBCHEM_TIMEOUT` | `10.0` | Connect timeout (seconds). |
| `ENTITY_RESOLUTION_PUBCHEM_MAX_RETRIES` | `3` | Maximum retry attempts per batch. |
| `ENTITY_RESOLUTION_PUBCHEM_API_KEY` | (none) | Optional PubChem API key. |
| `ENTITY_RESOLUTION_PUBCHEM_CA_BUNDLE` | (none) | Optional TLS CA bundle path. |
| `ENTITY_RESOLUTION_PUBCHEM_CERT_PEM` | (none) | Optional client cert PEM path. |
| `ENTITY_RESOLUTION_PUBCHEM_KEY_PEM` | (none) | Optional client key PEM path. |
| `ENTITY_RESOLUTION_PUBCHEM_STRICT_SALT_FORM` | `false` | When true, fetches all CIDs (parent + salts) for each InChIKey. |

### Operational settings

| Env var | Default | Description |
|---------|---------|-------------|
| `PIPELINE_CONTACT_EMAIL` | `team-cosmic@example.com` | Contact email for the User-Agent header (PubChem ToS requirement). |
| `PROMETHEUS_ENABLED` | `false` | When true, emit Prometheus metrics. |
| `OTEL_ENABLED` | `false` | When true, emit OpenTelemetry spans. |
| `OPERATOR_ID` | (none) | Operator identity for FDA 21 CFR Part 11 electronic-signature compliance. |
| `RDKIT_AVAILABLE` | (auto-detected) | Whether RDKit is installed (used for SMILES validation and authoritative formal charge). |

---

## 4. Scientific Caveats

This section is **mandatory reading** for anyone consuming the output of this pipeline. The data is not as simple as it looks.

### 4.1 Stereochemistry is life-safety-critical

PubChem returns two SMILES strings per compound:
- **`CanonicalSMILES`** — no stereochemistry. Two enantiomers produce the same canonical SMILES.
- **`IsomericSMILES`** — includes stereochemistry (`@`, `@@`), isotopes (`[18F]`, `[13C]`), and formal charges (`[NH4+]`).

The pipeline stores these as **two separate columns**: `canonical_smiles` and `isomeric_smiles`. **Never coalesce them.** The Graph Transformer's molecular fingerprinting MUST use `isomeric_smiles` — losing stereochemistry would make:

- (R)-thalidomide (teratogen) indistinguishable from (S)-thalidomide (sedative)
- (S)-escitalopram (active antidepressant) indistinguishable from (R)-escitalopram (inactive)
- (S)-warfarin (more potent enantiomer) indistinguishable from (R)-warfarin

If a clinical trial is launched on the wrong enantiomer, patients can die. The engineer who shipped the bug can be criminally liable.

### 4.2 XLogP is a QSAR prediction, not experimental logP

PubChem's `XLogP` is computed by the PubChem-XLogP3 group-contribution QSAR model. It is **not** an experimentally measured partition coefficient. Experimental logP can differ from XLogP by **1+ log unit** for some compound classes.

The pipeline stores `xlogp` (the value) AND `xlogp_source = "pubchem_xlogp3"` (the provenance flag). The model must not over-fit a noisy predictor as if it were ground truth.

### 4.3 TPSA is calculated, not measured

PubChem's `TPSA` (Topological Polar Surface Area) is computed from the 2D structure. It is **not** an experimentally measured value. The pipeline stores `tpsa` AND `tpsa_source = "pubchem_calculated"`.

### 4.4 PubChem CID is the parent / standardized CID

PubChem PUG REST returns the **standardized (parent) CID**. Two different salt forms of the same drug share the same parent CID:

- Esomeprazole sodium (CID 9579578) and esomeprazole magnesium (CID 23664567) both map to parent CID 5489319.
- Warfarin sodium and warfarin potassium both map to parent CID 54678486.

When `ENTITY_RESOLUTION_PUBCHEM_STRICT_SALT_FORM=true`, the pipeline additionally fetches all CIDs (parent + salts) via the `/pug/compound/inchikey/{ik}/cids/JSON` endpoint and stores them in `pubchem_compound_properties.salt_form_cids` (a JSON array).

### 4.5 molecular_weight vs. exact_mass

- **`molecular_weight`** = average MW using natural-abundance atomic weights (C=12.011, H=1.008, O=15.999, N=14.007).
- **`exact_mass`** = monoisotopic mass — uses the most-abundant isotope (C=12.000, H=1.0078, O=15.9949, N=14.0031).

**Use `exact_mass` (not `molecular_weight`) for mass-spectrometry-based drug discovery.** The mass-spec calibrated against `molecular_weight` will be off by ~0.5 Da for a typical small molecule, which is the difference between detecting the parent ion and missing it entirely.

Both columns use `NUMERIC(12,6)` precision (Decimal, not float) — Python `float(180.063388)` becomes `180.06338800000002`, which propagates errors into Tanimoto similarity calculations.

### 4.6 HeavyAtomCount excludes hydrogen

PubChem's `HeavyAtomCount` excludes hydrogen atoms (PubChem convention). For total atom count, compute from `molecular_formula`:

```python
import re
formula = "C9H8O4"  # aspirin
atoms = re.findall(r"([A-Z][a-z]?)(\d*)", formula)
total = sum(int(n) if n else 1 for _, n in atoms)
# total = 13 (9 C + 8 H + 4 O — wait, that's 21 atoms... let me recompute)
# Actually: C9 → 9, H8 → 8, O4 → 4 → total 21. Heavy atoms = 13.
```

### 4.7 HBondDonorCount / HBondAcceptorCount are Lipinski-style

- **Donors** = N-H bonds + O-H bonds (Lipinski's definition).
- **Acceptors** = N atoms + O atoms (Lipinski's definition, ignores S, P, halogens).

For detailed pharmacophore modeling, recompute from `isomeric_smiles` with RDKit:

```python
from rdkit import Chem
from rdkit.Chem import Lipinski
mol = Chem.MolFromSmiles(isomeric_smiles)
donors = Lipinski.NumHDonors(mol)
acceptors = Lipinski.NumHAcceptors(mol)
```

### 4.8 Empty strings become SQL NULL

PubChem occasionally returns `""` for a field (e.g., `MolecularFormula: ""`). The pipeline converts every `""`, whitespace-only string, and the literal sentinels `"nan"`, `"none"`, `"null"`, `"n/a"`, `"unknown"`, `"-"` to Python `None` (which becomes SQL `NULL`) before persistence.

This is critical: the legacy pipeline stored `""` and the loader's `COALESCE(:field, drugs.field)` SQL treated `""` as non-NULL — **silently overwriting existing real data with empty strings across the entire drugs table.** This is silent data corruption.

### 4.9 InChIKey protonation layer

The last character of a standard InChIKey encodes the protonation state:
- `N` = neutral
- `M` = charged
- `P` = mixed (multiple protonation states)
- `S` = sulfur-containing

The pipeline extracts this into the `protonation_state` column (CHAR(1)) and the human-readable `salt_form` column (VARCHAR(100)).

### 4.10 InChIKey mismatch detection

PubChem may return a different InChIKey than the one we requested (e.g., it normalizes the structure). The pipeline verifies `response_inchikey == requested_inchikey` for every record. Mismatches are dead-lettered with reason `"inchikey_mismatch"` — the response InChIKey is **never** stored under the requested key.

---

## 5. Schema Contract

The pipeline's output is governed by `pipelines/schema/v1.json#pubchem_enrichment.csv`. The schema lists 32 columns; the pipeline emits exactly these columns in the order defined by `COLUMN_ORDER` in `pubchem_pipeline.py`.

### Identity (2 columns)
| Column | Type | Description |
|--------|------|-------------|
| `inchikey` | VARCHAR(50) NOT NULL | Requested InChIKey (verified to match response). |
| `pubchem_cid` | BIGINT NOT NULL | PubChem Compound ID (parent / standardized). |

### Structural (8 columns)
| Column | Type | Description |
|--------|------|-------------|
| `molecular_formula` | VARCHAR(200) | e.g., "C9H8O4" for aspirin. |
| `molecular_weight` | NUMERIC(12,6) | Average MW (natural-abundance atomic weights). Decimal precision. |
| `exact_mass` | NUMERIC(12,6) | Monoisotopic mass (Da). Use for mass-spec. |
| `canonical_smiles` | VARCHAR(50000) | PubChem CanonicalSMILES (no stereo). |
| `isomeric_smiles` | VARCHAR(50000) | PubChem IsomericSMILES (with stereo, isotopes, charge). |
| `inchi` | TEXT | Full InChI string. |
| `iupac_name` | TEXT | PubChem IUPACName (may be PIN or non-PIN). |
| `cas_number` | VARCHAR(20) | CAS Registry Number (when `PUBCHEM_PIPELINE_FETCH_CAS=true`). |

### Physicochemical (5 columns + 2 source flags)
| Column | Type | Description |
|--------|------|-------------|
| `xlogp` | NUMERIC(6,2) | PubChem XLogP3 PREDICTION (not experimental). |
| `xlogp_source` | VARCHAR(50) | Always "pubchem_xlogp3" for fetched rows. |
| `tpsa` | NUMERIC(8,2) | PubChem-calculated TPSA. |
| `tpsa_source` | VARCHAR(50) | Always "pubchem_calculated" for fetched rows. |
| `complexity` | NUMERIC(10,2) | PubChem Bertz complexity. |

### Counts (7 columns)
| Column | Type | Description |
|--------|------|-------------|
| `h_bond_donor_count` | SMALLINT | Lipinski-style (N-H + O-H bonds). |
| `h_bond_acceptor_count` | SMALLINT | Lipinski-style (N + O atoms). |
| `rotatable_bond_count` | SMALLINT | PubChem rotatable bond count. |
| `heavy_atom_count` | SMALLINT | Excludes hydrogen (PubChem convention). |
| `formal_charge` | SMALLINT | Parsed from isomeric_smiles (RDKit authoritative; SMILES heuristic fallback). |
| `isotope_info` | TEXT | JSON dict, e.g., `{"F": 18, "C": 11}` for a PET tracer. NULL when no isotopes. |
| `salt_form` | VARCHAR(100) | Derived from InChIKey protonation layer. |
| `protonation_state` | CHAR(1) | N/M/P/S from InChIKey last char. |

### Lineage (8 columns)
| Column | Type | Description |
|--------|------|-------------|
| `source` | VARCHAR | Always "pubchem". |
| `source_id` | VARCHAR(100) NOT NULL | "pubchem:CID:<cid>". |
| `source_version` | VARCHAR(100) | "pubchem_pug_rest_as_of_<ISO 8601 UTC>". |
| `download_date` | TIMESTAMPTZ NOT NULL | ISO 8601 UTC. |
| `download_method` | VARCHAR(20) | "pug_rest_batch" or "pug_rest_single" (split-retry). |
| `pipeline_run_id` | UUID NOT NULL | UUID4 of the pipeline run. |
| `input_checksum` | VARCHAR(64) NOT NULL | SHA-256 of `inchikeys_to_lookup.txt`. |
| `transformations` | TEXT | Semicolon-joined list of applied transforms. |
| `as_of_date` | (TEXT) | Point-in-time requested by caller (PubChem ignores it). |

See also `docs/pipelines/pubchem_data_dictionary.md` for the full data dictionary.

### Database table

The new `pubchem_compound_properties` table (migration 005) is the persistence layer for these properties. It has additional columns not in the CSV:

- `source_batch_idx` — batch index of the PubChem API response that produced this row.
- `source_response_sha256` — SHA-256 of the batch's raw JSON response.
- `electronic_signature` — operator identity for FDA 21 CFR Part 11.
- `triggered_by` — Airflow DAG run ID or "manual".
- `enriched_at` — enrichment timestamp.
- `is_deleted` — soft-delete flag.
- `created_at`, `updated_at` — standard timestamps.

**JOIN pattern for downstream consumers:**

```sql
SELECT d.inchikey, d.name, p.canonical_smiles, p.isomeric_smiles,
       p.molecular_weight, p.exact_mass, p.xlogp, p.tpsa
FROM drugs d
JOIN pubchem_compound_properties p ON d.inchikey = p.inchikey
WHERE d.is_deleted = FALSE
  AND p.is_deleted = FALSE;
```

---

## 6. Failure Modes

### 6.1 HTTP 4xx (except 429) — permanent failure

`400`, `401`, `403`, `404`, `405`, `406`, `410`, `422` are permanent failures — retrying will not help. The batch is:

1. Split into individual InChIKey lookups (when batch size ≤ `PUBCHEM_PIPELINE_SPLIT_RETRY_MAX`).
2. Each individual InChIKey is queried separately.
3. Successful individual lookups are added to the results.
4. Individual 4xx failures are dead-lettered per-InChIKey with reason `http_<status>_permanent_split`.

### 6.2 HTTP 429 / 5xx — transient failure

`408`, `425`, `429`, `500`, `502`, `503`, `504` are transient. The pipeline retries with jittered exponential backoff:

```
backoff = min_backoff * (2 ** attempt) + random.uniform(0, min(backoff, 1.0))
backoff = max(backoff, retry_after_seconds)
backoff = min(backoff, max_backoff)
```

When the response includes a `Retry-After` header (either delta-seconds or HTTP-date), the wait is `max(backoff, retry_after_seconds)` — never less.

### 6.3 Circuit breaker

After `PUBCHEM_CIRCUIT_BREAKER_THRESHOLD` (default 5) consecutive failures, the circuit breaker opens. All subsequent requests fail fast with reason `"circuit_breaker_open"` for `PUBCHEM_CIRCUIT_BREAKER_RESET_SECONDS` (default 60s). After the cooldown, one probe request is allowed (HALF_OPEN state). If the probe succeeds, the breaker closes; if it fails, the breaker re-opens for another cooldown.

### 6.4 Dead-letter queue

Every failed InChIKey is appended to `self.dead_letter_queue` (a list of dicts). At end of run (`teardown()`), the queue is persisted to `raw_data/pubchem/pubchem_dead_letters.csv` with `QUOTE_NONNUMERIC` quoting and a SHA-256 sidecar.

Each entry includes:
- `inchikey` — the InChIKey that failed.
- `reason` — a machine-readable code (e.g., `http_404_permanent`, `inchikey_mismatch`, `range_violation_molecular_weight`, `circuit_breaker_open`).
- `batch_idx` — the batch index where the failure occurred.
- `status_code` — the HTTP status code (when applicable).
- `response_snippet` — first 500 chars of the response body.
- `timestamp` — ISO 8601 UTC.

### 6.5 PubChem unreachability

If the first 3 batches all fail with `ConnectionError`, the pipeline raises `PubChemUnreachableError`. This is a signal to the Airflow DAG to alert and retry later — PubChem is completely unreachable (DNS failure, firewall block, regional outage).

### 6.6 InChIKey mismatch

When PubChem returns a different InChIKey than the one we requested (e.g., it normalizes the structure), the record is dead-lettered with reason `"inchikey_mismatch"`. The response InChIKey is **never** stored under the requested key — this would silently corrupt the entity-resolution chain.

### 6.7 Range violations

Each numeric property has a valid range (see `RANGES` in `pubchem_pipeline.py`). Out-of-range values are dead-lettered with reason `"range_violation_<field>"` and the field is set to `None` (not stored). Examples:

- `molecular_weight < 0` or `> 100,000 Da` (likely a protein, not a small molecule).
- `xlogp < -5` or `> 15` (XLogP3 model is unreliable outside this range).
- `pubchem_cid < 1` or `> 10^12` (PubChem CID range).

---

## 7. Testing

The pipeline is covered by:

- **`tests/test_pubchem_pipeline_institutional_v131.py`** — 131+ unit tests, one per audit finding ID. Mocked HTTP via `unittest.mock.patch`. In-memory SQLite via the `conftest.db_session` fixture.
- **`tests/test_all_27_files_integration_v11.py`** — Integration test verifying the pipeline works end-to-end with the other 26 fixed files.

Run:

```bash
# Unit tests
pytest tests/test_pubchem_pipeline_institutional_v131.py -v

# Integration tests
pytest tests/test_all_27_files_integration_v11.py -v

# Coverage
pytest --cov=pipelines.pubchem_pipeline tests/test_pubchem_pipeline_institutional_v131.py
```

All tests are mock-based — no network access required.

---

## 8. Operational Runbook

### 8.1 How to backfill (re-enrich specific drugs)

To re-enrich a specific set of InChIKeys (e.g., after PubChem adds new compounds):

```sql
-- NULL out the pubchem_cid for the InChIKeys you want to re-enrich.
UPDATE drugs
SET pubchem_cid = NULL
WHERE inchikey IN ('IK1...', 'IK2...', 'IK3...');

-- Soft-delete the old pubchem_compound_properties rows (for audit).
UPDATE pubchem_compound_properties
SET is_deleted = TRUE, updated_at = NOW()
WHERE inchikey IN ('IK1...', 'IK2...', 'IK3...');
```

Then re-run the pipeline with `force_refresh=True`:

```python
from pipelines.pubchem_pipeline import PubChemPipeline
p = PubChemPipeline()
p._force_refresh = True
p.run()
```

### 8.2 How to debug dead-lettered InChIKeys

1. Locate the dead-letter file: `raw_data/pubchem/pubchem_dead_letters.csv`.
2. Filter by `reason`:
   - `invalid_inchikey_format` — the InChIKey in the drugs table is malformed. Fix the upstream pipeline (ChEMBL / DrugBank).
   - `http_404_permanent` / `http_404_permanent_split` — PubChem has no record for this InChIKey. May be a non-standard structure; consider manually adding it to PubChem.
   - `inchikey_mismatch` — PubChem normalized the structure. Investigate the mismatch — may indicate an error in the source database.
   - `range_violation_<field>` — the value is out of the expected range. Investigate whether the range is too narrow or PubChem returned bad data.
   - `circuit_breaker_open` — PubChem was unreachable for an extended period. Re-run the pipeline later.
3. After fixing the underlying issue, NULL out `pubchem_cid` for the affected InChIKeys and re-run the pipeline (see §8.1).

### 8.3 How to handle PubChem outages

1. The pipeline raises `PubChemUnreachableError` after 3 consecutive connection failures on the first batches. The Airflow DAG should catch this and retry with exponential backoff (e.g., 5 min, 15 min, 1 hour, 4 hours).
2. Check the PubChem status page: https://pubchemdocs.ncbi.nlm.nih.gov/status
3. If PubChem is down for an extended period, consider switching to a mirror or using cached raw responses (in `raw_data/pubchem/pubchem_responses/`).

### 8.4 How to verify idempotency

```bash
# Run the pipeline 3 times on the same input.
pytest tests/test_pubchem_pipeline_institutional_v131.py::TestDomain7Idempotency -v
```

The test asserts that running the pipeline 3× produces identical `pubchem_compound_properties` rows (modulo `enriched_at`, `pipeline_run_id`, `updated_at`).

---

## 9. References

- **PubChem PUG REST**: https://pubchemdocs.ncbi.nlm.nih.gov/pug-rest
- **InChIKey spec**: https://www.inchi-trust.org/technical-faq/
- **Lipinski Rule of 5**: Lipinski CA et al., *Adv Drug Deliv Rev* 1997;23(1-3):3-25.
- **XLogP3**: Cheng T et al., *J Chem Inf Model* 2007;47(6):2140-8.
- **PubChem compound properties**: Kim S et al., *Nucleic Acids Res* 2023;51(D1):D1373-D1380.
- **Audit document**: `pubchem_pipeline_forensic_audit.docx` (187 findings, 131 unique issues).
- **Fix prompt**: `PUBCHEM_PIPELINE_MASTER_FIX_PROMPT.md`
- **Fix report**: `docs/audits/PUBCHEM_PIPELINE_FIX_REPORT.md`
- **Data dictionary**: `docs/pipelines/pubchem_data_dictionary.md`
