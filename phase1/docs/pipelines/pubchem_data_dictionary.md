# PubChem Pipeline — Data Dictionary

> **Companion to:** `docs/pipelines/pubchem.md`
> **Schema contract:** `pipelines/schema/v1.json#pubchem_enrichment.csv`
> **Database table:** `pubchem_compound_properties` (migration 005)

This document lists every column produced by `PubChemPipeline.clean()` and persisted by `bulk_upsert_pubchem_compound_properties`, with its type, source, valid range, scientific meaning, and caveats.

---

## CSV columns (`processed_data/pubchem_enrichment.csv`)

The CSV has 32 columns in this exact order (defined by `COLUMN_ORDER` in `pubchem_pipeline.py`).

### Identity (2 columns)

| # | Column | Type | Source | Valid range | Scientific meaning | Caveats |
|---|--------|------|--------|-------------|-------------------|---------|
| 1 | `inchikey` | string (27 chars) | Requested from user | `^[A-Z]{14}-[A-Z]{10}-[A-Z]$` | 14-char connectivity hash + 10-char stereo hash + 1-char protonation. | Verified to match PubChem's response InChIKey. Mismatches are dead-lettered (SCI-11). |
| 2 | `pubchem_cid` | integer | PubChem PUG REST `CID` | 1 to 10^12 | PubChem Compound ID (parent / standardized). | Two different salt forms of the same drug share the same parent CID (SCI-5). |

### Structural (8 columns)

| # | Column | Type | Source | Valid range | Scientific meaning | Caveats |
|---|--------|------|--------|-------------|-------------------|---------|
| 3 | `molecular_formula` | string | `MolecularFormula` | e.g., "C9H8O4" | Hill-order molecular formula. | Empty strings → NULL (SCI-18). |
| 4 | `molecular_weight` | Decimal (NUMERIC(12,6)) | `MolecularWeight` | 0 to 100,000 Da | Average MW using natural-abundance atomic weights (C=12.011, H=1.008). | Decimal, not float — float(180.063388) → 180.06338800000002 (SCI-16). Use `exact_mass` for mass-spec. |
| 5 | `exact_mass` | Decimal (NUMERIC(12,6)) | `ExactMass` | 0 to 100,000 Da | Monoisotopic mass — uses most-abundant isotope (C=12.000, H=1.0078). | Use this for mass-spectrometry-based drug discovery. |
| 6 | `canonical_smiles` | string | `CanonicalSMILES` | (any) | Canonical SMILES — no stereochemistry. | NEVER coalesce with `isomeric_smiles`. Two enantiomers produce the same canonical SMILES. |
| 7 | `isomeric_smiles` | string | `IsomericSMILES` | (any) | Isomeric SMILES — with stereochemistry (`@`), isotopes (`[18F]`), charges (`[NH4+]`). | The Graph Transformer's molecular fingerprinting MUST use this. Life-safety-critical for chiral drugs (thalidomide, escitalopram, warfarin). |
| 8 | `inchi` | string | `InChI` | (any) | Full InChI string (structural + stereo + isotopes + charge). | |
| 9 | `iupac_name` | string | `IUPACName` | (any) | PubChem IUPACName — may be Preferred IUPAC Name (PIN) or non-PIN. | PubChem does not distinguish PIN from non-PIN via PUG REST. |
| 10 | `cas_number` | string | `/compound/cid/{cid}/synonyms/JSON` | `^\d{2,7}-\d{2}-\d$` | CAS Registry Number. | Only populated when `PUBCHEM_PIPELINE_FETCH_CAS=true`. Cross-validated against `drugs.cas_number` (from DrugBank). |

### Physicochemical (5 columns + 2 source flags)

| # | Column | Type | Source | Valid range | Scientific meaning | Caveats |
|---|--------|------|--------|-------------|-------------------|---------|
| 11 | `xlogp` | Decimal (NUMERIC(6,2)) | `XLogP` | -5 to 15 | PubChem XLogP3 PREDICTION (not experimental). Group-contribution QSAR model. | Experimental logP can differ by 1+ log unit (SCI-2). See `xlogp_source`. |
| 12 | `xlogp_source` | string | (constant) | `"pubchem_xlogp3"` | Provenance flag. | Always "pubchem_xlogp3" for fetched rows; NULL when `xlogp` is NULL. |
| 13 | `tpsa` | Decimal (NUMERIC(8,2)) | `TPSA` | 0 to 500 Å² | Topological Polar Surface Area. | Calculated from 2D structure, not measured (SCI-3). See `tpsa_source`. |
| 14 | `tpsa_source` | string | (constant) | `"pubchem_calculated"` | Provenance flag. | Always "pubchem_calculated" for fetched rows; NULL when `tpsa` is NULL. |
| 15 | `complexity` | Decimal (NUMERIC(10,2)) | `Complexity` | 0 to 10,000 | PubChem Bertz complexity metric. | |

### Counts (7 columns)

| # | Column | Type | Source | Valid range | Scientific meaning | Caveats |
|---|--------|------|--------|-------------|-------------------|---------|
| 16 | `h_bond_donor_count` | integer (SMALLINT) | `HBondDonorCount` | 0 to 50 | Lipinski-style H-bond donor count (N-H + O-H bonds). | Approximate — recompute from SMILES with RDKit for pharmacophore modeling (SCI-13). |
| 17 | `h_bond_acceptor_count` | integer (SMALLINT) | `HBondAcceptorCount` | 0 to 50 | Lipinski-style H-bond acceptor count (N + O atoms). | Approximate — does not include S, P, halogens (SCI-13). |
| 18 | `rotatable_bond_count` | integer (SMALLINT) | `RotatableBondCount` | 0 to 100 | PubChem rotatable bond count. | |
| 19 | `heavy_atom_count` | integer (SMALLINT) | `HeavyAtomCount` | 1 to 500 | PubChem heavy-atom count — **EXCLUDES hydrogen** (PubChem convention). | For total atom count, compute from `molecular_formula` (SCI-12). |
| 20 | `formal_charge` | integer (SMALLINT) | (parsed from `isomeric_smiles`) | -50 to 50 | Formal charge of the molecule. | RDKit authoritative; SMILES token heuristic as fallback (SCI-15). |
| 21 | `isotope_info` | string (JSON dict) | (parsed from `isomeric_smiles`) | e.g., `{"F": 18, "C": 11}` | JSON dict of isotope labels. | NULL when no isotopes (not `"{}"`). For PET tracers like [18F]FDG (SCI-14). |
| 22 | `salt_form` | string | (derived from InChIKey) | neutral / protonated / deprotonated / salt_form | Human-readable salt form. | V18 ROOT FIX (PS-1): per InChI Trust standard, N=neutral, M=deprotonated, P=protonated, S=salt_form (SCI-5). |
| 23 | `protonation_state` | char (1) | (derived from InChIKey) | N / M / P / S | Single-char protonation state from InChIKey last char. | V18 ROOT FIX (PS-1): N=neutral, M=deprotonated, P=protonated, S=salt_form (SCI-8). |

### Lineage (8 columns)

| # | Column | Type | Source | Valid range | Scientific meaning | Caveats |
|---|--------|------|--------|-------------|-------------------|---------|
| 24 | `source` | string | (constant) | `"pubchem"` | Source name. | Always "pubchem". |
| 25 | `source_id` | string | (computed) | `^pubchem:CID:\d+$` | Stable source identifier. | Format: "pubchem:CID:<cid>". |
| 26 | `source_version` | string | `get_source_version()` | e.g., `"pubchem_pug_rest_as_of_2026-06-20T03:45:21+00:00"` | PubChem access timestamp. | PubChem PUG REST has no version field — we record the access timestamp. |
| 27 | `download_date` | string (ISO 8601 UTC) | `datetime.now(timezone.utc)` | (any) | Download timestamp. | |
| 28 | `download_method` | string | (computed) | `pug_rest_batch` / `pug_rest_single` | Fetch method. | `pug_rest_batch` for normal fetches; `pug_rest_single` for split-retry (LIN-15). |
| 29 | `pipeline_run_id` | string (UUID4) | `self.run_id` | (any UUID) | Pipeline run identifier. | UUID4 generated per run. |
| 30 | `input_checksum` | string (SHA-256) | `_compute_sha256(inchikeys_to_lookup.txt)` | 64 hex chars | SHA-256 of the input file. | Identical inputs produce identical checksums — supports reproducibility audits. |
| 31 | `transformations` | string (semicolon-joined) | (computed) | (any) | List of transformations applied. | e.g., "validated_inchikey_format;fetched_pubchem_properties;verified_response_inchikey_matches_request;sanitized_empty_strings_to_null;converted_molecular_weight_to_decimal;validated_ranges;deduplicated_by_inchikey_lowest_cid;extracted_protonation_state;extracted_isotope_info;computed_formal_charge". |
| 32 | `as_of_date` | string (ISO 8601) | `self.as_of_date` | (any) | Point-in-time requested by caller. | PubChem PUG REST does not support point-in-time queries — recorded for traceability but ignored by the API (IDEM-11). |

---

## Database table columns (`pubchem_compound_properties`)

The CSV columns are persisted to the `pubchem_compound_properties` table (migration 005), plus additional database-specific columns:

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY | Surrogate primary key. |
| `source_batch_idx` | INTEGER | Batch index of the PubChem API response that produced this row. Combined with `pipeline_run_id`, locates the exact raw JSON archive file: `batch_{N:04d}.json`. |
| `source_response_sha256` | VARCHAR(64) | SHA-256 of the batch's raw JSON response. Tamper-evidence. |
| `electronic_signature` | TEXT | Operator identity for FDA 21 CFR Part 11. Populated from `settings.OPERATOR_ID`. |
| `triggered_by` | TEXT | Triggering context — Airflow DAG run ID or "manual". |
| `enriched_at` | TIMESTAMPTZ NOT NULL DEFAULT NOW() | Enrichment timestamp. Non-deterministic across runs by design. |
| `is_deleted` | BOOLEAN NOT NULL DEFAULT FALSE | Soft-delete flag. Set when superseded by a re-enrichment. |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT NOW() | Row creation timestamp. |
| `updated_at` | TIMESTAMPTZ NOT NULL DEFAULT NOW() | Row update timestamp. |

**Constraints:**
- `UNIQUE (inchikey, pubchem_cid)` — one row per (inchikey, cid) pair. Re-running the pipeline UPSERTs in place.
- `FOREIGN KEY (inchikey) REFERENCES drugs(inchikey)` — a properties row cannot exist for a drug that does not exist.

**Indexes:**
- `idx_pubchem_props_cid` — for lookup by CID.
- `idx_pubchem_props_inchikey` — for lookup by InChIKey.
- `idx_pubchem_props_is_deleted` (partial, WHERE is_deleted=TRUE) — for cleanup queries.
- `idx_pubchem_props_run_id` — for "find all rows from a given pipeline run".

---

## Range validation thresholds

Defined in `RANGES` in `pubchem_pipeline.py` (SCI-17). Values outside these ranges are dead-lettered with reason `range_violation_<field>` and the field is set to NULL.

| Field | Minimum | Maximum | Rationale |
|-------|---------|---------|-----------|
| `molecular_weight` | 0.0 | 100,000.0 Da | >10K likely a protein, not a small molecule. |
| `exact_mass` | 0.0 | 100,000.0 Da | Same. |
| `xlogp` | -5.0 | 15.0 | XLogP3 model is unreliable outside this range. |
| `tpsa` | 0.0 | 500.0 Å² | Theoretical max for a 1000-atom molecule. |
| `complexity` | 0.0 | 10,000.0 | PubChem Bertz complexity cap. |
| `h_bond_donor_count` | 0 | 50 | Lipinski limit is 5; 50 is a generous safety margin. |
| `h_bond_acceptor_count` | 0 | 50 | Lipinski limit is 10. |
| `rotatable_bond_count` | 0 | 100 | Veber limit is 10. |
| `heavy_atom_count` | 1 | 500 | Minimum 1 (no empty molecules); 500 is well above typical drug-like. |
| `pubchem_cid` | 1 | 10^12 | PubChem CID range. |
| `formal_charge` | -50 | 50 | Generous; most drugs have charge in [-2, +2]. |

---

## NULL string sentinels

Defined in `NULL_STRING_VALUES` in `pubchem_pipeline.py` (SCI-18). These string values are converted to Python `None` (SQL `NULL`) before persistence:

- `""` (empty string)
- `"nan"` (case-insensitive)
- `"none"` (case-insensitive)
- `"null"` (case-insensitive)
- `"n/a"` (case-insensitive)
- `"unknown"` (case-insensitive)
- `"-"` (single dash)

This is critical for `COALESCE` semantics in the loader's UPDATE statements. The legacy pipeline stored `""` and `COALESCE("", existing)` treated `""` as non-NULL — silently overwriting existing real data with empty strings across the entire `drugs` table.

---

## InChIKey format

The standard InChIKey is exactly 27 characters:

```
AAAAAAAAAAAAAA-BBBBBBBBBB-C
└── 14 chars ──┘ └ 10 chars ┘└1┘
   connectivity   stereo    protonation
     layer        layer       layer
```

The pipeline validates every InChIKey against `^[A-Z]{14}-[A-Z]{10}-[A-Z]$` before:
- Sending it to PubChem (SEC-5, DQ-2).
- Using it in any SQL (DQ-2).
- Storing it in the output DataFrame.

Invalid InChIKeys are dead-lettered with reason `invalid_inchikey_format`.

The last character (protonation layer) is one of:
- `N` — neutral
- `M` — charged
- `P` — mixed (multiple protonation states)
- `S` — sulfur-containing

This is extracted into the `protonation_state` column.
