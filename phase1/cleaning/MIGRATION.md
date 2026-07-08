# Migration Guide — `cleaning.deduplicator` v1.0.0 → v3.0.0

**Date:** 2026-06-17
**Schema version bump:** `1.0.0` → `3.0.0`
**Rule version bump:** `rules_v1` → `rules_v3`

This document describes the changes from `deduplicator.py` v1.0.0 (222 lines)
to v3.0.0 (~3,900 lines) and any migration steps required.

## TL;DR

**Zero breaking changes.** All existing call sites in
`pipelines/chembl_pipeline.py:363`, `pipelines/drugbank_pipeline.py:504`,
`cleaning/__init__.py`, and all 30+ test files continue to work without
modification. 138 issues across 16 domains have been fixed.

## Backward-Compatible Signature Changes

Both public functions preserve their original positional parameters.
New parameters are keyword-only with defaults that preserve v1.0.0
behavior exactly:

### `dedup_by_inchikey`
```python
# v1.0.0
dedup_by_inchikey(df: pd.DataFrame) -> pd.DataFrame

# v3.0.0 (backward compatible)
dedup_by_inchikey(
    df: pd.DataFrame,
    *,
    reset_index: bool = True,
    return_result: bool = False,
    conservative_defaults: bool = False,
    merge_fields: bool = False,
    keep_lineage_columns: bool = False,
    validate_inchikeys: bool = True,
    auto_standardize: bool = True,
    synth_handling: Literal["strict", "by_name", "skip"] = "strict",
    weight: CompletenessWeight | None = None,
    dedup_by_version_char: bool = False,
    null_inchikey_handler: Literal["keep_all", "drop", "quarantine"] = "keep_all",
    skip_if_already_deduped: bool = True,
    max_duplicate_ratio: float | None = None,
    source: str | None = None,
    operator_id: str | None = None,
    source_dataset_id: str | None = None,
) -> pd.DataFrame | DedupResult
```

### `dedup_interactions`
```python
# v1.0.0
dedup_interactions(df: pd.DataFrame, keys: List[str]) -> pd.DataFrame

# v3.0.0 (backward compatible)
dedup_interactions(
    df: pd.DataFrame,
    keys: list[str] | None = None,        # NOW OPTIONAL — inferred if absent
    *,
    activity_type_column: str | None = "activity_type",
    activity_value_column: str | None = "activity_value",
    activity_units_column: str | None = "activity_units",
    confidence_column: str | None = "confidence_score",
    direction: Literal["asc", "desc", "auto"] = "auto",
    keep: Literal["best", "first", "last", "mark"] = "best",
    segment_by_activity_type: bool = True,
    normalize_units: bool = True,
    handle_censored: bool = True,
    null_keys_handler: Literal["keep_all", "drop", "quarantine"] = "keep_all",
    strict_activity_type: bool = False,
    reset_index: bool = True,
    return_result: bool = False,
    conservative_defaults: bool = False,
    keep_lineage_columns: bool = False,
    skip_if_already_deduped: bool = True,
    max_duplicate_ratio: float | None = None,
    source: str | None = None,
    operator_id: str | None = None,
    source_dataset_id: str | None = None,
) -> pd.DataFrame | DedupResult
```

## Scientific Correctness Fixes (Domain 3)

The v1.0.0 code sorted ALL activity types ascending — scientifically
wrong for `pKi`/`pIC50`/`pEC50`/`pKd` and `%` inhibition. The v3.0.0
fix:

- `direction="auto"` (default) infers from `activity_type`:
  - `IC50`/`Ki`/`Kd`/`EC50`/`AC50`/`ED50`/`Kb` → ascending (lower = more potent).
  - `pKi`/`pIC50`/`pEC50`/`pKd` → descending (higher = more potent).
  - `%` inhibition → descending (higher = more potent).
- `activity_type` is now part of the dedup segmentation by default
  (`segment_by_activity_type=True`), so IC50 vs Ki rows are NOT collapsed.
- Censored values (`"<10"`, `">100"`) are penalized so they don't
  silently win over uncensored values.
- Activity values are normalized to nM before comparison (when
  `activity_units` is present).

## Data Quality Fixes (Domain 5)

- NaN InChIKeys are no longer collapsed (v1.0.0 had a `NaN == NaN`
  data-loss bug). Each null InChIKey row is preserved.
- SYNTH-prefixed InChIKeys are treated as unique placeholders.
- Mixture InChIKeys (multi-component) are not deduplicated.
- InChIKey version-char mismatches are detected and warned.
- Activity-value range validation (0 to 1e9) — out-of-range values
  are quarantined.

## Idempotency Fixes (Domain 7)

- `_dedup_already_applied` marker on `df.attrs` — second call returns
  input unchanged.
- Deterministic tie-breaking via `mergesort` + original index.
- Input/output SHA-256 fingerprints for reproducibility verification.
- Backfill safety check (`backfill_safety_check`).

## New Public API (v3.0.0)

The module now exports 42 public names (up from 2 in v1.0.0):

**Types:**
- `DedupResult` (dataclass), `DedupStrategy` (enum),
  `ActivityDirection` (enum), `CompletenessWeight` (dataclass)

**Functions:**
- `dedup_by_inchikey`, `dedup_interactions`, `dedup_by_inchikey_chunked`
- `compute_completeness_score`, `merge_duplicate_groups`
- `quality_report`, `referential_integrity_check`, `backfill_safety_check`
- `recover_from_failure`, `checkpoint_state`, `validate_recovery_state`
- `performance_benchmark`, `is_reproducible`, `reproducibility_report`
- `get_metrics`, `reset_metrics`, `get_dead_letters`, `clear_dead_letters`,
  `flush_dead_letters`
- `set_correlation_id`, `get_correlation_id`, `get_provenance`
- `timing_report`, `health_check`
- `configure_deduplicator`, `validate_config`, `validate_environment`,
  `revert_configuration`
- `requires_api_version`, `clean_interactions`

**Constants:**
- `DEFAULT_COMPLETENESS_WEIGHTS`, `DEFAULT_DPI_KEYS`,
  `POTENCY_ACTIVITY_TYPES`, `INVERSE_ACTIVITY_TYPES`,
  `PERCENT_ACTIVITY_TYPES`, `MAX_DATAFRAME_ROWS`, `MAX_DEAD_LETTERS`,
  `MAX_DROPPED_ROWS_IN_RESULT`

## Behavior Changes to Watch For

1. **`dedup_interactions` with `activity_type` column**: by default now
   segments by activity_type. Two rows with same key but different
   activity_type (e.g., IC50 and Ki) are no longer collapsed. Pass
   `segment_by_activity_type=False` to restore v1.0.0 behavior.

2. **`dedup_by_inchikey` with NaN InChIKeys**: by default now preserves
   all null-InChIKey rows. Pass `null_inchikey_handler="drop"` to
   restore v1.0.0 behavior (which collapsed them).

3. **`dedup_interactions` with censored values**: by default now
   penalizes censored values. Pass `handle_censored=False` to restore
   v1.0.0 behavior (which let censored values silently win).

4. **`dedup_interactions` with `pKi`/`pIC50`**: by default now keeps
   the highest value (correct). Pass `direction="asc"` to restore
   v1.0.0 behavior (which was scientifically wrong).

---

## Migration Guide — `cleaning.normalizer` v2.0.0 → v2.1.0

**Date:** 2026-06-17
**Schema version bump:** `1.0.0` → `1.1.0`
**Rule version bump:** `rules_v2` → `rules_v3`

This document describes the changes from `normalizer.py` v2.0.0 (473 lines)
to v2.1.0 (~4500 lines) and any migration steps required.

## TL;DR

**Zero breaking changes.** All existing call sites in
`pipelines/chembl_pipeline.py`, `pipelines/drugbank_pipeline.py`,
`cleaning/__init__.py`, `cleaning/missing_values.py`,
`cleaning/deduplicator.py`, `entity_resolution/drug_resolver.py`, and
all 30+ test files continue to work without modification.

## Backward-Compatible Signature Changes

All four public functions (`convert_to_inchikey`, `standardize_inchikey`,
`standardize_drug_record`, `normalize_activity_value`) preserve their
original positional parameters. New parameters are keyword-only with
defaults that preserve v2.0.0 behavior:

### `convert_to_inchikey`
```python
# v2.0.0
convert_to_inchikey(smiles: str) -> Optional[str]

# v2.1.0 (backward compatible)
convert_to_inchikey(
    smiles: Union[str, bytes, Mol, None],
    *,
    options: Optional[str] = None,       # NEW
    standard: bool = True,                # NEW
    timeout: Optional[float] = None,      # NEW
    raise_on_error: bool = False,         # NEW
) -> Optional[str]
```

### `standardize_inchikey`
```python
# v2.0.0 and v2.1.0 (identical signature)
standardize_inchikey(raw_inchikey: Union[str, bytes, None]) -> Optional[str]
```

### `standardize_drug_record`
```python
# v2.0.0
standardize_drug_record(record: dict) -> dict

# v2.1.0 (backward compatible)
standardize_drug_record(
    record: dict,
    *,
    required_keys: Optional[Tuple[str, ...]] = None,    # NEW
    known_inchikeys: Optional[set[str]] = None,         # NEW
    seen_inchikeys: Optional[set[str]] = None,          # NEW
    mw_range: Optional[Tuple[float, float]] = None,     # NEW
    source: Optional[str] = None,                       # NEW
    operator_id: Optional[str] = None,                  # NEW
    source_dataset_id: Optional[str] = None,            # NEW
    raise_on_error: bool = False,                       # NEW
) -> dict
```

### `normalize_activity_value`
```python
# v2.0.0
normalize_activity_value(value, units: str) -> Tuple[Optional[float], str]

# v2.1.0 (backward compatible — returns ActivityValue, which IS a 2-tuple)
normalize_activity_value(
    value: Any,
    units: Any,
    *,
    activity_type: Optional[str] = None,    # NEW
    temperature_c: Optional[float] = None,  # NEW
    raise_on_error: bool = False,           # NEW
) -> ActivityValue  # subclasses tuple, so val, unit = r still works
```

## Behavior Changes

The following behaviors changed. All changes are backward-compatible
(Constraint #4: existing call sites continue to work).

### 1. SYNTH InChIKey Pattern Loosened (ARCH-1, DQ-1)

**Before (v2.0.0):** `^SYNTH[0-9A-F]{9}-[0-9A-F]{10}-[0-9A-F]$` (strict hex)
**After (v2.1.0):** `^SYNTH.+$` (case-insensitive)

**Impact:** `standardize_inchikey("SYNTH-001")` now returns `"SYNTH-001"`
instead of `None`. This matches the DB layer's `startswith("SYNTH")`
contract.

**Migration:** None required. Callers that previously received `None` for
synthetic keys now receive the uppercased key. If you relied on `None`
being returned for synthetic keys (unlikely), use
`validate_inchikey(key, strict=True)` instead.

### 2. `is_fda_approved` Now Handles Withdrawn Drugs (SCI-1)

**Before (v2.0.0):** `is_fda_approved = (max_phase == 4) or ("approved" in groups)`
**After (v2.1.0):** `is_fda_approved = (max_phase == 4 or "approved" in groups) and not is_withdrawn`

**Impact:** Drugs with `groups=["approved","withdrawn"]` now have
`is_fda_approved=False` (was `True`). New field `is_withdrawn=True` and
`was_ever_approved=True` are added for audit.

**Migration:** None required. The new behavior is scientifically correct
(withdrawn drugs are no longer FDA-approved).

### 3. `max_phase` Now Accepts Float-Encoded Strings (SCI-2)

**Before (v2.0.0):** `int(max_phase)` raised `ValueError` on `"4.0"`.
**After (v2.1.0):** `_coerce_phase_to_int("4.0") == 4`.

**Impact:** More records correctly marked `is_fda_approved=True`.

**Migration:** None required.

### 4. `standardize_inchikey` Now Uppercases Input (CODE-25)

**Before (v2.0.0):** Lowercase input `"bsynrym..."` rejected.
**After (v2.1.0):** Lowercase input uppercased and accepted.

**Migration:** None required.

### 5. `standardize_drug_record` Adds `_provenance` Field (LINEAGE-1)

**Before (v2.0.0):** Output dict had same keys as input (plus `is_fda_approved`).
**After (v2.1.0):** Output dict gains `_provenance` (dict),
`is_withdrawn` (bool), `was_ever_approved` (bool),
`is_fda_approved_source` (str), and optionally `group_warnings`,
`inchikey_mismatch`.

**Impact on `clean_drugs()`:** The `_provenance` field would have become
a DataFrame column with non-deterministic timestamps. To prevent this,
`cleaning/__init__.py:clean_drugs` was updated (v2.1.0) to skip
`_`-prefixed columns when applying `standardize_drug_record` row-by-row.
Provenance is still accessible via `df.attrs["_provenance"]`.

**Migration:** If you call `standardize_drug_record` directly and were
iterating over `out.keys()`, you will now see `_provenance` and the new
bool fields. Filter them with `if not key.startswith("_")` if needed.

### 6. `normalize_activity_value` Returns `ActivityValue` (DESIGN-8)

**Before (v2.0.0):** Returned a plain 2-tuple `(value, unit)`.
**After (v2.1.0):** Returns an `ActivityValue` (subclass of tuple) whose
first two elements are still `(value, unit)`. Additional metadata is
accessible via attributes: `r.value`, `r.censored`, `r.censor_direction`,
`r.activity_type`, `r.original_value`, `r.warnings`, etc.

**Migration:** None required. `val, unit = normalize_activity_value(v, u)`
still works. New callers should use `r.value` and `r.unit` for clarity.

### 7. Path Traversal in `name` Now Blocked (SEC-5)

**Before (v2.0.0):** `name = "../../etc/passwd"` was accepted as-is.
**After (v2.1.0):** Replaced with `f"BLOCKED-{sha256(name)[:8]}"`.

**Migration:** None required (security hardening).

### 8. `molecular_weight` Range Validation (SCI-7)

**Before (v2.0.0):** Any float accepted.
**After (v2.1.0):** Values outside `[0, 5000]` set to `None` with a
WARNING log. Override with `standardize_drug_record(rec, mw_range=(0, 10000))`.

**Migration:** None required (existing valid MW values are unaffected).

## New Public API (v2.1.0)

The following new symbols are added to `cleaning.normalizer.__all__`
and re-exported from `cleaning/__init__.py`:

- `convert_to_inchikey_detailed` — returns `ConversionResult` with full
  error context, canonical SMILES, collision tracking.
- `convert_to_inchikeys` — batch API using ThreadPoolExecutor.
- `normalize_inchikey`, `validate_inchikey`, `is_valid_inchikey`,
  `is_synthetic_inchikey` — split-out validation/normalization helpers.
- `fuzzy_match_drug_type`, `fuzzy_match_drug_types` — public alias of
  `_fuzzy_match_drug_type` + batch API.
- `standardize_drug_records_batch`, `standardize_drug_records_chunked` —
  batch APIs with checkpointing.
- `normalize_activity_values` — batch API.
- `refresh_capabilities`, `get_dq_counts`, `reset_dq_counts`,
  `get_cache_info`, `get_validation_status` — observability helpers.
- `configure_normalizer`, `save_config`, `load_config`, `watch_config`,
  `validate_config` — configuration management.
- `requires_api_version`, `is_backfill_needed` — version compatibility.
- `sign_output` — minimal e-signature stub (FDA 21 CFR Part 11).
- `ActivityValue`, `ConversionResult` — public types.
- `WITHDRAWN_GROUP_KEYWORDS`, `STEREO_POLICY`, `RECORD_SCHEMA` — public
  constants.

## New Internal Helpers (backward-compatible)

- `_coerce_phase_to_int`, `_coerce_molecular_weight`,
  `_shallow_copy_record`, `_smiles_to_mol`, `_round_sig`,
  `_smiles_hash`, `_truncate_for_log`, `_sanitize_string_local`,
  `_log_event`, `_audit_log_local`, `_validate_inchikey_version_char`,
  `_normalize_groups`, `_check_pii`, `_set_unit_conversion`,
  `_set_allowed_types`, `_validate_config`, `_get_fuzzy_scorer`.
- `_LocalCircuitBreaker`, `_CorrelationIdFilter`.
- `_AV_EXTRAS` (side-channel dict for ActivityValue extras).

## Module-Level Constants (Constraint #3 — all preserved)

The following constants from v2.0.0 are preserved (some are now
immutable views, but the names are unchanged):

`ALLOWED_TYPES`, `FUZZY_THRESHOLD`, `UNIT_CONVERSIONS`,
`_INCHIKEY_PATTERN`, `_SYNTHETIC_INCHIKEY_PATTERN`,
`_FUZZY_THRESHOLD`, `_UNIT_CONVERSIONS`, `_RDKIT_AVAILABLE`,
`_FUZZY_AVAILABLE`, `_DEFAULT_FUZZY_SCORER_NAME`, `_ALLOWED_TYPES_TUPLE`,
`_ALLOWED_TYPES_LOWER`, `_UNIT_CONVERSIONS_CF`, `_INCHI_OPTIONS_DEFAULT`,
`_MAX_SMILES_ATOMS`, `_SMILES_MAX_LENGTH`, `_SMILES_ALLOWED_CHARS`,
`_LOG_SMILES_TRUNC`, `_MAX_DRUG_TYPE_LENGTH`, `_MAX_RECORD_KEYS`,
`_MAX_VALID_PHASE`, `_MIN_NAME_LENGTH`, `_MW_VALID_RANGE`, `_MW_TOLERANCE`,
`_ACTIVITY_VALUE_MAX`, `_ACTIVITY_VALUE_SIG_FIGS`, `_ALLOWED_ACTIVITY_TYPES`,
`_CB_FAILURE_THRESHOLD`, `_CB_RESET_TIMEOUT`, `_MAX_DEAD_LETTERS`,
`_MAX_COLLISION_TRACK`, `_LRU_CACHE_MAX`, `_KNOWN_RECORD_KEYS`,
`_CENSOR_PATTERN`, `_PII_PATTERNS`, `_PATH_TRAVERSAL_PATTERN`,
`_STANDARD_INCHIKEY_PATTERN`, `_NONSTANDARD_INCHIKEY_PATTERN`,
`_MIXTURE_INCHIKEY_PATTERN`, `_SYNTHETIC_INCHIKEY_STRICT_PATTERN`,
`_NORMALIZER_VERSION`, `_OUTPUT_SCHEMA_VERSION`, `_RULE_VERSION`,
`_CONFIG_VERSION`, `_LOGIC_HASH`, `_DEFAULT_DRUG_TYPE`,
`_DEFAULT_ALLOWED_TYPES`, `_DEFAULT_UNIT_CONVERSIONS`,
`_DEFAULT_FUZZY_SCORER_NAME`, `_FUZZY_SCORERS`, `_FUZZY_TIE_EPSILON`,
`_NULL_MECHANISM_VALUES`, `_CONTRADICTORY_GROUP_PAIRS`,
`_RDKIT_VERSION`, `_RAPIDFUZZ_VERSION`, `_RDKIT_INCHI_BROKEN`,
`_METRICS`, `_METRICS_LOCK`, `_DQ_COUNTS`, `_DQ_LOCK`,
`_dead_letters`, `_DEAD_LETTERS_LOCK`, `_INCHIKEY_TO_SMILES`,
`_COLLISION_LOCK`, `_normalize_call_count`, `_NORMALIZE_LOCK`,
`_LAST_CONFIG_SNAPSHOT`, `_rate_limiter`, `_openlineage_emitter`,
`_config_watcher_thread`.

## Deprecation Policy

No symbols are deprecated in v2.1.0. The deprecation policy is documented
in the module docstring: deprecated names are tracked in
`_DEPRECATED_NAMES` and emit `DeprecationWarning` on access for at least
one minor version before removal.

## Questions / Edge Cases

If you encounter an issue migrating, check:

1. The `[DOMAIN-NN]` comment markers in `normalizer.py` (grep for the
   issue ID from the master prompt).
2. `cleaning/SCHEMA.md` for the output schema.
3. `CHANGELOG.md` for the full list of fixes by domain.
4. The test suite: `pytest tests/test_normalizer_v21_comprehensive.py`
   exercises every behavior change.
