# ClinicalTrials Loader — Data Dictionary

> **Version:** 2.1.0 (institutional-grade audit fix)
> **Source:** ClinicalTrials.gov AACT database (CTTI)
> **License:** CC0 1.0 (public domain)
> **Citation:** AACT data extracted from https://aact.ctti-clinicaltrials.org. Duke-Margolis Center for Health Policy and FDA. Clinical Trials Transformation Initiative (CTTI).

This document is the authoritative reference for every field emitted by
`drugos_graph/clinicaltrials_loader.py`. Each field's source AACT
table/column, type, valid values, nullability, and example are documented.

---

## AACT Tables / Columns Used

| AACT Table                       | Columns Used                                                                |
|----------------------------------|-----------------------------------------------------------------------------|
| `studies`                        | nct_id, brief_title, phase, overall_status, study_type, enrollment, why_stopped, has_results, start_date, completion_date |
| `interventions`                  | nct_id, name, description, intervention_type                                |
| `interventions_mesh_terms`       | nct_id, intervention_id, mesh_term (modern schema only)                     |
| `conditions`                     | nct_id, name, mesh_term (legacy schema only)                                |
| `conditions_mesh_terms`          | nct_id, condition_id, mesh_term (modern schema only)                        |
| `designs`                        | nct_id, allocation, intervention_model, masking, primary_purpose            |
| `primary_outcomes`               | nct_id, measure                                                             |

**Schema documentation:** https://aact.ctti-clinicaltrials.org/definitions

---

## Trial Record Fields (`ClinicalTrialTrialRecord`)

Produced by `parse_clinicaltrials_trials` / `iter_clinicaltrials_trials`.

### Identity

| Field         | Type           | Source                  | Nullable | Example              | Notes                                              |
|---------------|----------------|-------------------------|----------|----------------------|----------------------------------------------------|
| `nct_id`      | `str`          | studies.nct_id          | No       | "NCT00000001"        | Validated against `^NCT\d{8}$` (Issue 3.15).       |
| `brief_title` | `str`          | studies.brief_title     | No       | "Aspirin for Headache" | Propagated to props (Issue 3.12).                |
| `nct_url`     | `str`          | derived                 | No       | "https://clinicaltrials.gov/study/NCT00000001" | Constructed from nct_id (Issue 16.6). |

### Trial Design

| Field              | Type             | Source                       | Nullable | Example            | Notes                                              |
|--------------------|------------------|------------------------------|----------|--------------------|----------------------------------------------------|
| `phase`            | `str`            | studies.phase                | No       | "Phase 3"          | Validated against `CLINICALTRIALS_VALID_PHASES`.   |
| `overall_status`   | `str`            | studies.overall_status       | No       | "Completed"        | Validated against `CLINICALTRIALS_VALID_STATUSES`. |
| `study_type`       | `str`            | studies.study_type           | No       | "Interventional"   | Default filter: Interventional only (Issue 3.7).   |
| `enrollment`       | `Optional[int]`  | studies.enrollment           | Yes      | 200                | WARNING if <30 in Phase 3 (Issue 3.6).             |
| `why_stopped`      | `Optional[str]`  | studies.why_stopped          | Yes      | "Stopped due to severe adverse events" | Safety signal if matches regex (Issue 3.5). |
| `has_results`      | `Optional[bool]` | studies.has_results          | Yes      | True               | +0.05 evidence bonus if True (Issue 3.10).         |

### Trial Dates

| Field             | Type             | Source                  | Nullable | Example      | Notes                                              |
|-------------------|------------------|-------------------------|----------|--------------|----------------------------------------------------|
| `start_date`      | `Optional[str]`  | studies.start_date      | Yes      | "2020-01-01" | ISO-8601 YYYY-MM-DD (Issue 3.13, 14.7).            |
| `completion_date` | `Optional[str]`  | studies.completion_date | Yes      | "2022-01-01" | ISO-8601 YYYY-MM-DD (Issue 3.13, 14.7).            |

### Design Details

| Field                  | Type             | Source                       | Nullable | Example       | Notes                                              |
|------------------------|------------------|------------------------------|----------|---------------|----------------------------------------------------|
| `allocation`           | `Optional[str]`  | designs.allocation           | Yes      | "Randomized"  | +0.10 evidence bonus if Randomized (Issue 3.8).    |
| `intervention_model`   | `Optional[str]`  | designs.intervention_model   | Yes      | "Parallel"    | Propagated to props (Issue 3.8).                   |
| `masking`              | `Optional[str]`  | designs.masking              | Yes      | "Double Blind" | +0.08 bonus if Double Blind (Issue 3.8).          |
| `primary_purpose`      | `Optional[str]`  | designs.primary_purpose      | Yes      | "Treatment"   | Propagated to props (Issue 3.8).                   |
| `primary_outcome`      | `Optional[str]`  | primary_outcomes.measure     | Yes      | "6-month mortality" | GROUP_CONCAT'd via subquery (Issue 3.9).     |

### Drug / Condition (one row per cross-join combination)

| Field              | Type             | Source                                          | Nullable | Example       | Notes                                              |
|--------------------|------------------|-------------------------------------------------|----------|---------------|----------------------------------------------------|
| `drug_name`        | `str`            | interventions.name                              | Yes      | "Aspirin"     | Normalized (Issue 15.10).                          |
| `drug_mesh`        | `Optional[str]`  | interventions_mesh_terms.mesh_term              | Yes      | "D001241"     | Modern schema only (Issue 3.1).                    |
| `drug_role`        | `str`            | derived                                         | No       | "experimental" | "comparator_or_placebo" if description matches regex (Issue 3.3). |
| `description`      | `Optional[str]`  | interventions.description                       | Yes      | "Aspirin 100mg daily" | Used for drug_role detection (Issue 3.3).    |
| `condition_name`   | `str`            | conditions.name                                 | Yes      | "Headache"    |                                                    |
| `condition_mesh`   | `Optional[str]`  | conditions_mesh_terms.mesh_term                 | Yes      | "D006261"     | Modern schema only (Issue 3.1).                    |

### Provenance & Compliance

| Field               | Type             | Source                  | Nullable | Notes                                              |
|---------------------|------------------|-------------------------|----------|----------------------------------------------------|
| `_provenance`       | `Dict[str, Any]` | derived                 | No       | Contains every key in `CLINICALTRIALS_PROVENANCE_KEYS`. |
| `_source`           | `str`            | derived                 | No       | Always "ClinicalTrials".                           |
| `_license`          | `str`            | derived                 | No       | Always "CC0 1.0".                                  |
| `_attribution`      | `str`            | derived                 | No       | `CLINICALTRIALS_ATTRIBUTION`.                      |
| `_schema_version`   | `str`            | derived                 | No       | `SCHEMA_VERSION` ("2.1.0").                        |

---

## Edge Record Fields (`ClinicalTrialEdgeRecord`)

Produced by `clinicaltrials_to_edge_records`. Consumed by
`kg_builder.DrugOSGraphBuilder.load_edges_bulk_create`.

### Top-Level Edge Fields

| Field                | Type             | Nullable | Example            | Notes                                              |
|----------------------|------------------|----------|--------------------|----------------------------------------------------|
| `src_id`             | `str`            | No       | "D001241"          | Preference: drug_mesh > drug_name. Never empty (Issue 4.7). |
| `dst_id`             | `str`            | No       | "D006261"          | Preference: condition_mesh > condition_name. Never empty (Issue 4.7). |
| `src_type`           | `str`            | No       | "Compound"         | Always "Compound" (Issue 15.9).                    |
| `dst_type`           | `str`            | No       | "Disease"          | Always "Disease" (Issue 15.9).                     |
| `rel_type`           | `str`            | No       | "tested_for"       | Always "tested_for" (Issue 2.1, 14.1, 15.3). NEVER "clinical_trial" (deprecated) or "treats" (forbidden). |
| `edge_id`            | `str`            | No       | "a1b2c3d4e5f6a1b2" | SHA-1 of `"{src_id}|{dst_id}|{src_type}|{dst_type}|{rel_type}|{nct_id}"` (Issue 2.3). |
| `source_tag`         | `str`            | No       | "ClinicalTrials"   | Always "ClinicalTrials" (Issue 2.6).               |
| `evidence_strength`  | `float`          | No       | 0.85               | In [0.0, 1.0]. Computed by `_compute_evidence_strength` (Issue 2.5). |
| `confidence`         | `str`            | No       | "high"             | "high" / "medium" / "low" — RL ranker safety dimension. |
| `id_confidence`      | `str`            | No       | "high"             | "high" / "medium" / "low" — ID resolution confidence (Issue 16.12). |
| `props`              | `Dict[str, Any]` | No       | (see below)        | All edge properties.                               |

### `props` Sub-Fields

#### Trial Identity
| Field              | Type             | Notes                                              |
|--------------------|------------------|----------------------------------------------------|
| `nct_id`           | `str`            | NCT ID.                                            |
| `nct_url`          | `str`            | https://clinicaltrials.gov/study/{nct_id} (Issue 16.6). |
| `brief_title`      | `str`            | Trial title (Issue 3.12).                          |
| `phase`            | `str`            | e.g. "Phase 3".                                    |
| `status`           | `str`            | e.g. "Completed".                                  |
| `study_type`       | `str`            | e.g. "Interventional".                             |
| `enrollment`       | `Optional[int]`  | Trial enrollment count (Issue 3.6).                |
| `why_stopped`      | `str`            | Empty if not stopped.                              |
| `has_results`      | `bool`           | True if trial has published results.               |

#### Drug / Condition
| Field              | Type             | Notes                                              |
|--------------------|------------------|----------------------------------------------------|
| `drug_name`        | `str`            | Normalized drug name (Issue 15.10).                |
| `drug_mesh`        | `str`            | MeSH descriptor ID, empty if missing.              |
| `drug_role`        | `str`            | "experimental" or "comparator_or_placebo" (Issue 3.3). |
| `condition_name`   | `str`            |                                                    |
| `condition_mesh`   | `str`            | MeSH descriptor ID, empty if missing.              |

#### Design
| Field                  | Type             | Notes                                              |
|------------------------|------------------|----------------------------------------------------|
| `allocation`           | `str`            | e.g. "Randomized".                                 |
| `intervention_model`   | `str`            | e.g. "Parallel".                                   |
| `masking`              | `str`            | e.g. "Double Blind".                               |
| `primary_purpose`      | `str`            | e.g. "Treatment".                                  |
| `primary_outcome`      | `str`            | GROUP_CONCAT'd primary outcomes (Issue 3.9).       |

#### Dates
| Field              | Type  | Notes                                              |
|--------------------|-------|----------------------------------------------------|
| `start_date`       | `str` | ISO-8601 YYYY-MM-DD or empty (Issue 3.13, 14.7).   |
| `completion_date`  | `str` | ISO-8601 YYYY-MM-DD or empty (Issue 3.13, 14.7).   |

#### Safety
| Field              | Type             | Notes                                              |
|--------------------|------------------|----------------------------------------------------|
| `safety_signal`    | `Optional[str]`  | "stopped_for_safety" if why_stopped matches safety regex, else None (Issue 3.5). |
| `orphan_src`       | `bool`           | True if src_id is a MeSH descriptor not crosswalked to DrugBank (Issue 5.4, 15.7). |
| `orphan_dst`       | `bool`           | True if dst_id is a MeSH descriptor not crosswalked to UMLS (Issue 5.4, 15.8). |

#### Lineage (Issues 16.1-16.6)
| Field                | Type  | Notes                                              |
|----------------------|-------|----------------------------------------------------|
| `source_url`         | `str` | AACT download URL.                                 |
| `downloaded_at`      | `str` | ISO-8601 timestamp when AACT was downloaded.       |
| `source_sha256`      | `str` | SHA-256 of the AACT zip.                           |
| `source_version`     | `str` | AACT release version.                              |
| `pipeline_version`   | `str` | `PARSER_VERSION` ("2.1.0").                        |
| `schema_version`     | `str` | `SCHEMA_VERSION` ("2.1.0").                        |
| `license`            | `str` | "CC0 1.0".                                         |
| `citation`           | `str` | CTTI citation.                                     |

#### Compliance
| Field                | Type  | Notes                                              |
|----------------------|-------|----------------------------------------------------|
| `_source`            | `str` | Always "ClinicalTrials".                           |
| `_license`           | `str` | Always "CC0 1.0".                                  |
| `_attribution`       | `str` | `CLINICALTRIALS_ATTRIBUTION`.                      |
| `_schema_version`    | `str` | `SCHEMA_VERSION`.                                  |
| `_provenance`        | `dict`| Contains every key in `CLINICALTRIALS_PROVENANCE_KEYS`. |

---

## Evidence Strength Computation (Issue 2.5)

`evidence_strength` is computed by `_compute_evidence_strength` as follows:

1. **Base** = `CLINICALTRIALS_PHASE_STRENGTH[phase]` (default 0.0).
   - Phase 4: 0.85 (strongest — post-marketing surveillance)
   - Phase 3: 0.70
   - Phase 2/3: 0.60
   - Phase 2: 0.40
   - Phase 1/2: 0.25
   - Phase 1: 0.15
   - Early Phase 1: 0.10
   - N/A: 0.0
2. **+ Allocation bonus** (Issue 3.8):
   - Randomized: +0.10
   - Non-Randomized / NA: +0.0
3. **+ Masking bonus** (Issue 3.8):
   - Quadruple / Triple Blind: +0.10
   - Double Blind: +0.08
   - Single Blind: +0.04
   - Open Label / None / NA: +0.0
4. **+ Has-results bonus** (Issue 3.10): +0.05 if `has_results` is True.
5. **+ Large-trial bonus** (Issue 3.6): +0.05 if `enrollment >= 500`.
6. **- Cross-product penalty** (Issue 2.2): -0.10 if
   `n_interventions × n_conditions > 4`.
7. **- Safety-stop penalty** (Issue 3.5): -0.20 if `why_stopped` matches
   the safety regex (`(?i)\b(safety|adverse|death|toxicity|severe)\b`).
8. **× Comparator multiplier** (Issue 3.3): ×0.3 if
   `drug_role == "comparator_or_placebo"`.
9. **Clamp** to [0.0, 1.0].

### Confidence Tiers

- **high**: `evidence_strength >= 0.7`
- **medium**: `0.4 <= evidence_strength < 0.7`
- **low**: `evidence_strength < 0.4`

### `id_confidence` (Issue 16.12)

- **high**: MeSH term resolved to DrugBank/UMLS via crosswalk (future work).
- **medium**: MeSH term present but no crosswalk match.
- **low**: Any of:
  - `drug_mesh` null, used `drug_name` as fallback
  - `condition_mesh` null, used `condition_name` as fallback
  - `drug_role == "comparator_or_placebo"`
  - `why_stopped` matched safety pattern
  - `orphan_src` or `orphan_dst` (referential integrity failure)

---

## Controlled Vocabularies

### Valid Phases (`CLINICALTRIALS_VALID_PHASES`)

- "Early Phase 1"
- "Phase 1"
- "Phase 1/Phase 2"
- "Phase 2"
- "Phase 2/Phase 3"
- "Phase 3"
- "Phase 3/Phase 4"
- "Phase 4"
- "N/A"

### Valid Intervention Types (`CLINICALTRIALS_VALID_INTERVENTION_TYPES`)

- "Drug"
- "Biological"
- "Device"
- "Procedure"
- "Behavioral"
- "Dietary Supplement"
- "Radiation"
- "Genetic"
- "Combination Product"
- "Diagnostic Test"
- "Other"

### Valid Study Types (`CLINICALTRIALS_VALID_STUDY_TYPES`)

- "Interventional"
- "Observational"
- "Observational [Patient Registry]"
- "Expanded Access"

### Valid Statuses (`CLINICALTRIALS_VALID_STATUSES`)

- "Completed"
- "Active, not recruiting"
- "Recruiting"
- "Enrolling by invitation"
- "Not yet recruiting"
- "Withdrawn"
- "Suspended"
- "Terminated"
- "No Longer Available"
- "Unknown status"
- "Approved for marketing"
- "Available"
- "No longer recruiting"
- "Temporarily not available"

### Garbage MeSH Values (`CLINICALTRIALS_GARBAGE_MESH_VALUES`)

These values are filtered out by `_normalize_mesh` (Issue 5.11):

- "" (empty string)
- "D000001" (placeholder)
- "ERROR"
- "N/A"
- "UNKNOWN"
- "NULL"
- "NONE"

---

## Environment Variables

All env vars are read at call time (not import time) so tests can
monkeypatch `os.environ` between calls:

| Env var                                              | Default | Purpose                                              |
|------------------------------------------------------|---------|------------------------------------------------------|
| `DRUGOS_CLINICALTRIALS_SKIP`                         | "0"     | Skip loader entirely.                                |
| `DRUGOS_CLINICALTRIALS_OFFLINE`                      | "0"     | Use cached file only — no download.                  |
| `DRUGOS_CLINICALTRIALS_FORCE_DOWNLOAD`               | "0"     | Force re-download.                                   |
| `DRUGOS_CLINICALTRIALS_ALLOW_STALE`                  | "0"     | Allow stale cache on download failure.               |
| `DRUGOS_CLINICALTRIALS_ALLOW_LEGACY`                 | "0"     | Allow legacy AACT schema (mesh_term as direct column). |
| `DRUGOS_CLINICALTRIALS_SKIP_SHA256`                  | "0"     | Skip SHA-256 verification (dev only).                |
| `DRUGOS_CLINICALTRIALS_CHUNK_SIZE`                   | "50000" | SQL read chunk size.                                 |
| `DRUGOS_CLINICALTRIALS_MAX_RETRIES`                  | "3"     | Download retry count.                                |
| `DRUGOS_CLINICALTRIALS_RETRY_BACKOFF_BASE`           | "2.0"   | Exponential backoff base.                            |
| `DRUGOS_CLINICALTRIALS_DOWNLOAD_TIMEOUT`             | "600"   | Per-request timeout seconds.                         |
| `DRUGOS_CLINICALTRIALS_CIRCUIT_BREAKER_THRESHOLD`    | "5"     | DLQ-count circuit breaker threshold.                 |
| `DRUGOS_CLINICALTRIALS_CIRCUIT_BREAKER_COOLDOWN`     | "3600"  | Circuit breaker cooldown seconds.                    |
| `DRUGOS_CLINICALTRIALS_PINNED_RELEASE`               | (unset) | Pinned AACT release for reproducibility.             |
| `DRUGOS_CLINICALTRIALS_PROGRESS_INTERVAL`            | "100000"| Lines between progress logs.                         |
| `DRUGOS_CLINICALTRIALS_NEO4J_BATCH_SIZE`             | "50000" | Neo4j batch size.                                    |
| `DRUGOS_CLINICALTRIALS_USER_AGENT`                   | "DrugOS/2.1 (drugos@example.com)" | HTTP User-Agent header.                |

---

## Cross-Join Warning (Issue 13.5, 2.2, 3.3 — PATIENT SAFETY)

The AACT schema does NOT link interventions to conditions at the row
level. A trial with interventions [Drug A, Placebo] and conditions
[Disease X, Disease Y] produces 4 rows in the JOIN. Only ONE of these
rows (Drug A → Disease X) is the experimental association the trial
was designed to test; the other 3 are fabrications of the JOIN.

This loader mitigates (does not eliminate) the problem by:

1. **Tagging placebo/comparator interventions** via description regex
   (Issue 3.3) — these edges get `drug_role='comparator_or_placebo'`,
   `evidence_strength *= 0.3`, `id_confidence='low'`.
2. **Penalizing evidence_strength** for parallel-design trials with
   `N_interventions × N_conditions > 4` (Issue 2.2).
3. **Emitting a WARNING** per trial with high cross-product inflation.

The fully-correct fix requires joining `result_groups` /
`outcome_analysis` to identify the experimental arm — see Issue 2.2
option (b). That mode is available via `id_strictness='strict_arm'`
(future work — excluded ~70% of trials that lack results).

---

## References

- AACT documentation: https://aact.ctti-clinicaltrials.org/definitions
- AACT schema: https://aact.ctti-clinicaltrials.org/schema
- AACT license: CC0 1.0 (https://creativecommons.org/publicdomain/zero/1.0/)
- ClinicalTrials.gov: https://clinicaltrials.gov/
- NCT ID format: https://clinicaltrials.gov/data-api/about-api/study-data-structure/
- MeSH descriptors: https://www.ncbi.nlm.nih.gov/mesh
- DrugOS Coding Standards: `drugos_graph/compliance.md`
- PEP 8 / 257 / 563 / 544 (style, docstrings, lazy annotations, Protocols).
