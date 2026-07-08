# Output Schema — `cleaning.normalizer.standardize_drug_record`

**Schema version:** `1.1.0` (matches `_OUTPUT_SCHEMA_VERSION` in `normalizer.py`)
**Last updated:** 2026-06-17 (v2.1.0)

This document describes the input/output schema of
`standardize_drug_record(record: dict) -> dict`.

## Input Schema

The input `record` is a dict with the following optional keys:

| Key                  | Type             | Required | Description                                    |
|----------------------|------------------|----------|------------------------------------------------|
| `name`               | `str`            | no       | Drug name (min length 2 enforced by DB)        |
| `smiles`             | `str`            | no       | SMILES molecular representation                |
| `inchikey`           | `str` or `None`  | no       | 27-char InChIKey or SYNTH-prefixed synthetic   |
| `molecular_weight`   | `float`/`str`    | no       | Molecular weight in Daltons (validated 0–5000) |
| `molecular_formula`  | `str`            | no       | Molecular formula (e.g., `C9H8O4`)             |
| `drug_type`          | `str`            | no       | Free-text; fuzzy-matched to ALLOWED_TYPES      |
| `max_phase`          | `int`/`str`      | no       | Clinical trial phase (0–4)                     |
| `groups`             | `list[str]`/`str`| no       | Drug status groups (`approved`, `withdrawn`, …)|
| `mechanism_of_action`| `str`            | no       | Mechanism description                          |
| `is_fda_approved`    | `bool`           | no       | Upstream FDA-approval flag                     |
| `source`             | `str`            | no       | Source dataset name                            |

## Output Schema — Guaranteed Keys

The output dict always contains:

| Key                    | Type             | Description                                       |
|------------------------|------------------|---------------------------------------------------|
| `is_fda_approved`      | `bool`           | Derived from max_phase/groups/withdrawn status    |
| `is_withdrawn`         | `bool`           | True if any group is in WITHDRAWN_GROUP_KEYWORDS  |
| `was_ever_approved`    | `bool`           | True if `approved` was in groups (audit field)    |
| `is_fda_approved_source`| `str`           | `"upstream"`, `"derived:max_phase"`, `"derived:groups"`, `"derived:default"`, `"derived:withdrawn"`, or `"derived:contradiction"` |
| `drug_type`            | `str`            | Always from ALLOWED_TYPES                         |
| `groups`               | `list[str]`      | Normalized to lowercase list                      |
| `_provenance`          | `dict`           | Lineage metadata (see below)                      |

## Output Schema — Conditional Keys

These keys are present iff present in input (or derivable):

- `name`, `smiles`, `inchikey`, `molecular_weight` (float or None),
  `molecular_formula`, `mechanism_of_action`, `max_phase` (int or None),
  `source`.

- `group_warnings`: list of `f"contradictory:{a}+{b}"` strings if
  contradictory group pairs were detected (e.g., `["approved","withdrawn"]`).
- `inchikey_mismatch`: `True` if `smiles` and `inchikey` were both
  present but `convert_to_inchikey(smiles) != inchikey`.

## `_provenance` Sub-schema

```json
{
  "cleaned_by": "normalizer.standardize_drug_record",
  "cleaner_version": "2.1.0",
  "rule_version": "rules_v3",
  "schema_version": "1.1.0",
  "config_version": "1.0.0",
  "logic_hash": "<16-char hex sha256 of normalizer.py source>",
  "cleaned_at": "<ISO 8601 UTC timestamp>",
  "input_sha256": "<64-char hex sha256 of input record JSON>",
  "output_sha256": "<64-char hex sha256 of output record JSON>",
  "transformations": ["stripped_whitespace:name", "fuzzy_matched_drug_type:Small molecule", ...],
  "warnings": ["name_too_short:1", "mw_out_of_range:-100.0", ...],
  "rdkit_version": "<rdkit.__version__>",
  "rapidfuzz_version": "<rapidfuzz.__version__>",
  "is_synthetic": false,
  "transformation_chain": ["standardize_drug_record"],
  "source": "<optional, from source kwarg>",
  "operator_id": "<optional, from operator_id kwarg or env>",
  "source_dataset_id": "<optional, from source_dataset_id kwarg>",
  "correlation_id": "<optional, from set_correlation_id()>",
  "pii_warnings": ["name", "groups[2]", ...],
  "unknown_keys": ["new_field", ...],
  "previous_provenance": {...},
  "signed_by": "<optional, from sign_output()>",
  "signed_at": "<optional, ISO 8601 timestamp>"
}
```

## `ConversionResult` Schema

Returned by `convert_to_inchikey_detailed`:

```python
ConversionResult(
    success: bool,
    inchikey: str | None,
    error: str | None,
    error_category: str | None,  # one of:
        # "INVALID_SMILES", "RDKIT_UNAVAILABLE", "RDKIT_PARSE_ERROR",
        # "RDKIT_INCHI_ERROR", "RDKIT_INCHIKEY_ERROR", "UNKNOWN_ERROR",
        # "TIMEOUT", "CIRCUIT_OPEN", "SMILES_TOO_LONG",
        # "SMILES_INVALID_CHARS", "RATE_LIMITED", "POTENTIAL_COLLISION"
    smiles_hash: str | None,     # 64-char hex sha256 of input SMILES
    canonical_smiles: str | None,
    potential_collision: bool,
    stereo_ambiguous: bool,
    rdkit_version: str,
)
```

## `ActivityValue` Schema

Returned by `normalize_activity_value`. Subclass of `tuple` whose first
two elements are `(value, unit)` for 2-tuple backward compatibility:

```python
ActivityValue(
    value: float | None,
    unit: str,                    # "nM" on success, else original unit
    original_value: Any,          # preserved input value
    original_unit: str | None,
    conversion_factor: float | None,
    censored: bool,               # True if ">", "<", "~", or out-of-range
    censor_direction: str | None, # "<", ">", "~", or None
    activity_type: str | None,    # "IC50", "Ki", "Kd", "EC50", ...
    temperature_c: float | None,
    warnings: tuple[str, ...],
)
```

## Cross-Module InChIKey Contract

A key is valid iff:

1. `len(key) == 27` AND `key` matches `^[A-Z]{14}-[A-Z]{10}-[A-Z]$` (loose),
   OR
2. `key.upper().startswith("SYNTH")`.

This matches `database/models._validate_inchikey`,
`database/loaders._validate_inchikey`, and
`entity_resolution/drug_resolver.is_synthetic_inchikey` exactly.

---

## dedup_by_inchikey output schema (v3.0.0)

**Schema version:** `3.0.0` (matches `_OUTPUT_SCHEMA_VERSION` in `deduplicator.py`)

### Required columns
Same as input — no columns added or removed by default.

### Optional columns (when `keep_lineage_columns=True`)
- `_completeness_score` (float): Weighted completeness score.
- `_dedup_winner` (bool): True for kept rows, False for dropped rows (only with `keep="mark"`).
- `_dedup_source_indices` (list[int]): Original indices merged into this row (only with `merge_fields=True`).

### `attrs` metadata
- `_provenance` (list[dict]): Transformation history (append-only audit chain).
- `_input_fingerprint` (str): SHA-256 of input (64-char hex).
- `_output_fingerprint` (str): SHA-256 of output (64-char hex).
- `_dedup_already_applied` (bool): Idempotency marker.
- `cleaning_metrics` (dict): Operation metrics.

### `DedupResult` (when `return_result=True`)
```python
DedupResult(
    df: pd.DataFrame,           # The deduplicated DataFrame
    rows_before: int,           # Input row count
    rows_after: int,            # Output row count
    duplicates_removed: int,    # rows_before - rows_after
    quarantined: int,           # Rows moved to dead-letter queue
    dead_letter_count: int,     # DLQ entries added during this call
    duration_seconds: float,    # Wall-clock time
    warnings: list[str],        # Human-readable warnings
    columns_affected: dict,     # Per-column change counts
    dtype_changes: dict,        # Columns whose dtype changed
    dropped_rows: list[dict],   # Capped list of dropped-row records
    strategy: str,              # "most_complete" | "merge_fields" | "first_occurrence" | ...
    provenance: dict,           # Provenance metadata
)
```

### Strategy values
- `most_complete` — Default; keep row with highest weighted completeness.
- `first_occurrence` — Plain `drop_duplicates(keep="first")`.
- `last_occurrence` — Plain `drop_duplicates(keep="last")`.
- `lowest_activity` — Keep lowest `activity_value` (for IC50/Ki/Kd).
- `highest_activity` — Keep highest `activity_value` (for pKi/pIC50/%).
- `merge_fields` — Column-wise merge of duplicate groups.

---

## dedup_interactions output schema (v3.0.0)

### Required columns
Same as input.

### Scientific correctness (Domain 3)
- `activity_type` is part of the dedup segmentation by default
  (`segment_by_activity_type=True`). Two rows with the same composite
  key but different `activity_type` are NOT duplicates.
- Sort direction is inferred from `activity_type`:
  - `IC50`/`Ki`/`Kd`/`EC50`/`AC50`/`ED50`/`Kb` → ascending (lower = more potent).
  - `pKi`/`pIC50`/`pEC50`/`pKd` → descending (higher = more potent).
  - `%` inhibition → descending (higher = more potent).
- Censored values (`"<10"`, `">100"`, `"~50"`) are penalized so they
  don't silently win over uncensored values.
- Activity values are normalized to nM before comparison (when
  `activity_units` column is present and `normalize_units=True`).

---

## Dead-letter entry schema (v3.0.0)

Each entry in the dead-letter queue is a dict with:
```json
{
  "function": "dedup_by_inchikey",
  "reason": "duplicate_inchikey",
  "row": {"inchikey": "BSYNRYMUTXBXSQ-...", "name": "Aspirin", ...},
  "timestamp": "2026-06-17T12:34:56.789+00:00",
  "correlation_id": "abc-123",
  "module_version": "3.0.0",
  "schema_version": "3.0.0",
  "rule_version": "rules_v3",
  "logic_hash": "<16-char hex>",
  "survivor_info": {
    "inchikey": "BSYNRYMUTXBXSQ-...",
    "original_index": 0
  }
}
```

---

## validate_gda_scores output schema (v2.0)

`cleaning.missing_values.validate_gda_scores` is the shared GDA validator
used by both the DisGeNET and OMIM pipelines. It enforces score ranges,
deduplicates records, and emits lineage columns tracking every
transformation applied.

### Input columns (required)

- `score` — numeric (int or float); coerced to float, non-numeric → NaN
- `disease_id` — string; used for dedup

### Input columns (optional)

- `gene_symbol` — string; used for dedup
- `source` — string; used for dedup
- `disease_name` — string; filled with `disease_id` value if NaN
- `association_type` — string; filled with `"unknown"` if NaN

### Output columns (added by the validator)

These columns are prefixed with `_` in the CSV output and mapped to
non-underscore DB column names at load time:

| CSV column                        | DB column                       | Type  | Description                                       |
|-----------------------------------|---------------------------------|-------|---------------------------------------------------|
| `_score_was_clipped`              | `score_was_clipped`             | bool  | True if score was clipped to `[min, max]`         |
| `_original_score`                 | `original_score`                | float | Original score if clipped; None otherwise         |
| `_score_was_coerced_nan`          | `score_was_coerced_nan`         | bool  | True if non-numeric score was coerced to NaN      |
| `_score_direction`                | `score_direction`               | str   | `"positive"`/`"negative"`/`"neutral"` (only if `preserve_direction=True`) |
| `_disease_name_was_filled`        | `disease_name_was_filled`       | bool  | True if `disease_name` was filled with `disease_id` |
| `_association_type_was_filled`    | `association_type_was_filled`   | bool  | True if `association_type` was filled with `"unknown"` |

### Kwargs

| Kwarg                  | Default                          | Description                                           |
|------------------------|----------------------------------|-------------------------------------------------------|
| `score_range`          | `(0.0, 1.0)`                    | Valid score range; out-of-range scores are clipped    |
| `preserve_direction`   | `False`                          | If True, set `_score_direction` based on sign         |
| `alternative_id_columns` | `None`                         | Additional columns to consider for dedup              |
| `source`               | `None`                           | Source name (e.g., `"omim"`, `"disgenet"`)            |
| `dedup`                | `False`                          | If True, deduplicate records                          |
| `dedup_keys`           | `["gene_symbol", "disease_id", "source"]` | Columns to dedup on (filtered to existing)   |
| `reset_index`          | `False`                          | If True, reset the DataFrame index                    |
| `return_result`        | `False`                          | If True, return a `DataCleaningResult` instead of df  |

### OMIM-specific call site (master prompt §4.1)

```python
df = validate_gda_scores(
    df,
    score_range=(0.0, 1.0),
    preserve_direction=False,          # OMIM scores are always positive
    source="omim",                     # COMP-5 / SCI-23
    dedup=True,                        # DQ-4 / SCI-22
    dedup_keys=["gene_symbol", "disease_id", "source"],
)
```

### DisGeNET-specific call site (for comparison)

```python
df = validate_gda_scores(
    df,
    score_range=(0.0, 1.0),
    preserve_direction=True,           # DisGeNET scores can be negative (inhibition)
    source="disgenet",
    dedup=True,
    dedup_keys=["gene_id", "disease_id", "source"],  # falls back to gene_symbol
)
```

### DB CHECK constraints (enforced by `gene_disease_associations` table)

- `confidence_tier IN ('weak', 'moderate', 'strong')` — never `"high"`
- `disease_id_type IN ('omim', 'disgenet', 'doid', 'mesh', 'umls', 'hpo')`
- `disease_type IN ('disease', 'phenotype', 'group')`
- `evidence_strength IN ('robust', 'moderate', 'limited', 'unsupported')`
- `normalized_score BETWEEN 0.0 AND 1.0`
- `year_initial IS NULL OR year_final IS NULL OR year_initial <= year_final`

The OMIM pipeline's `association_type` values (`causal`, `susceptibility`,
`non_disease`, `provisional`, `gene_locus`, `mendelian_phenotype`) are NOT
CHECK-constrained — they're free-text. Downstream consumers should treat
any unknown value as `"unknown"`.
