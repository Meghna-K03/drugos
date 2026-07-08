"""
Cleaning sub-package for the Drug Repurposing ETL Platform.

.. warning::
    **v29 ROOT FIX (audit C-12): 2377 lines of re-exports — significant
    import-time cost.** This file is a mega-export ``__init__.py`` that
    re-exports ~80+ public symbols from four sub-modules
    (``normalizer``, ``deduplicator``, ``missing_values``, ``confidence``)
    plus a large amount of package-level glue code (exceptions, a
    circuit breaker, dead-letter store, audit/provenance helpers,
    metrics, fingerprinting, public-API declaration, etc.). The bulk of
    the file is *module-level documentation* (lines 1-444) plus a
    90-line ``__all__`` (lines 2287-2377) plus the
    ``_LAZY_IMPORTS`` / ``_OPTIONAL_DEPS`` / ``_API_VERSIONS`` registry
    tables (each ~100+ lines, each mirroring the same symbol list).

    Future cleanup (out of scope for v29): split this file into
    sub-packages — e.g. ``cleaning.api``, ``cleaning.internals``,
    ``cleaning.registry`` — and let consumers import from those. The
    current single-file design was acceptable when there were ~12
    symbols; at 80+ it has become a maintenance and import-time
    liability (importing ``cleaning`` parses ~2400 lines of module
    body). For now, the bloat is documented but unchanged — deleting
    re-exports would break downstream pipelines (``omim_pipeline.py``,
    ``drugbank_pipeline.py``, etc.) and the existing
    ``__getattr__``/``_LAZY_IMPORTS`` machinery already defers the
    *expensive* sub-module imports until first attribute access.

    The most impactful follow-up would be to factor the multi-hundred-
    line docstring at the top of this file into ``cleaning/SCHEMA.md``
    (which already exists) and ``cleaning/MIGRATION.md`` (also exists).
    That alone would shave ~440 lines from the import-time parse cost.

This package re-exports the public API from its sub-modules so that
consumers can import functions directly from the ``cleaning`` namespace::

    from cleaning import convert_to_inchikey, dedup_by_inchikey

Re-exported symbols are listed in ``__all__``.  Every public name from
each sub-module's own ``__all__`` is re-exported here — this package
does NOT selectively exclude any public name.

Sub-modules
-----------
normalizer
    SMILES to InChIKey conversion, InChIKey standardization, drug record
    normalization, activity value unit conversion.
deduplicator
    Drug deduplication by InChIKey, interaction deduplication by composite key.
missing_values
    InChIKey recovery from SMILES, missing drug field defaults,
    protein field cleaning, GDA score validation.

Recommended Processing Order
----------------------------
For drug DataFrames, apply cleaning in this order to ensure correctness:

1. ``standardize_inchikey`` — normalize InChIKey format FIRST
2. ``handle_missing_inchikey`` — recover missing InChIKeys from SMILES
3. ``fill_missing_drug_fields`` — fill default values for missing fields
4. ``standardize_drug_record`` — normalize drug type, FDA approval, etc.
5. ``dedup_by_inchikey`` — deduplicate AFTER normalization (order matters!)

For protein DataFrames:

1. ``handle_missing_protein_fields`` — drop null IDs, fill defaults,
   truncate sequences

For GDA DataFrames:

1. ``validate_gda_scores`` — clip scores to [0,1], fill missing disease names

**WARNING:** Calling ``dedup_by_inchikey`` before ``standardize_inchikey``
will fail to detect duplicates that differ only in InChIKey formatting
(whitespace, casing), producing silently incorrect results.

Scientific Note on InChIKey Normalization
------------------------------------------
InChIKeys use the format [A-Z]{14}-[A-Z]{10}-[A-Z] (27 characters total).
The first 14 characters represent the molecular connectivity layer — two
molecules with the same connectivity block share the same molecular skeleton.
The remaining characters encode stereochemistry and protonation.

**Ordering matters:** ``dedup_by_inchikey`` MUST be called AFTER
``standardize_inchikey``, because the same drug may appear with InChIKeys
that differ only in whitespace or casing.  Without normalization, chemically
identical drugs are treated as distinct, inflating drug counts and fragmenting
interaction data.

InChIKey Structure and Drug Repurposing
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
An InChIKey has three blocks separated by hyphens::

  AAAAABBBBBBBBCC-DDDDDDDDDF-E
  |--- 14 chars --|-- 10 chars -| 1 char

- Block 1 (14 chars): Molecular connectivity layer.  Two molecules with
  the same block 1 share the same molecular skeleton (same atoms, same bonds).
- Block 2 (10 chars): Stereochemistry and protonation.  Enantiomers share
  block 1 but differ in block 2.
- Block 3 (1 char): Version/charge layer.

**Drug repurposing implication:** Enantiomers (same connectivity, different
stereochemistry) can have dramatically different biological activities.  The
classic example is thalidomide: one enantiomer is therapeutic, the other is
teratogenic.  The ``standardize_inchikey`` function validates the FULL 27-char
InChIKey, preserving stereochemistry information.  When comparing drugs for
deduplication, only drugs with IDENTICAL full InChIKeys are considered
duplicates.  Drugs that share only the connectivity block are NOT duplicates —
they may have different pharmacological profiles.

.. note::
   ``convert_to_inchikey`` CANNOT handle biologics (antibodies, proteins,
   cell therapies).  RDKit only converts small-molecule SMILES to InChIKeys.
   For biologics, use UniProt IDs or other protein-level identifiers instead.

Why Nanomolar (nM)?
~~~~~~~~~~~~~~~~~~~
In pharmacology, nM is the standard unit for IC50/Ki/Kd measurements because
most drug-target interactions of therapeutic relevance fall in the nanomolar
range (1-100 nM).  The conversion factors are:

  - pM to nM:  divide by 1,000   (1 pM = 0.001 nM)
  - nM to nM:  identity           (1 nM = 1 nM)
  - uM to nM:  multiply by 1,000 (1 uM = 1,000 nM)
  - mM to nM:  multiply by 1e6   (1 mM = 1,000,000 nM)

A typical strong drug-target interaction has IC50 approximately 10 nM.
A weak interaction has IC50 approximately 10,000 nM (10 uM).
Values above 100 uM (100,000 nM) are generally considered non-specific.

Scientific Assumptions & Limitations
-------------------------------------
1. ``handle_missing_inchikey`` DROPS drugs that cannot be identified by
   either InChIKey or SMILES.  This means the final dataset may be missing
   entire drug classes (biologics, antibodies, cell therapies) that lack
   SMILES representations.  For small-molecule-centric analyses this is
   acceptable; for broad drug class coverage, consumers must supplement
   with alternative identifiers (e.g., UniProt IDs for biologics).

2. ``validate_gda_scores`` CLIPS scores to [0, 1].  This assumes all GDA
   scores are normalized.  Some data sources (e.g., DisGeNET) use different
   scoring scales.  Consumers should verify that their source scores are
   on a [0, 1] scale before relying on this function; if not, they must
   pre-normalize before calling this function.

3. ``fill_missing_drug_fields`` defaults ``is_fda_approved`` to ``False``.
   This is a CONSERVATIVE assumption that excludes drugs approved by other
   regulatory agencies (EMA, PMDA, TGA, Health Canada).  Consumers working
   with non-US drug data should override this default.

4. ``handle_missing_protein_fields`` defaults ``organism`` to
   ``'Homo sapiens'``.  This is correct for the primary use case (human
   drug repurposing) but will MISLABEL non-human proteins (veterinary,
   bacterial, plant).  Consumers processing non-human data MUST explicitly
   set the organism field before calling this function.

5. ``convert_to_inchikey`` uses RDKit which only handles small-molecule
   SMILES.  Antibodies, proteins, and cell-based therapies do not have
   SMILES representations and will return None.  This is not a bug — it
   reflects a fundamental limitation of the SMILES/InChIKey system for
   biologics.

Error Behavior
--------------
Each cleaning function follows one of these error patterns:

1. **DataFrame functions** (dedup_by_inchikey, fill_missing_drug_fields, etc.):
   - Return a (possibly modified) COPY of the input DataFrame
   - Never return None
   - Log warnings for missing columns and return DataFrame unchanged
   - Log info for operations that modify data

2. **Conversion functions** (convert_to_inchikey, standardize_inchikey):
   - Return None if conversion fails
   - Log debug for expected failures (empty input, non-string)
   - Log warning for unexpected failures (RDKit errors)

3. **Value normalization** (normalize_activity_value):
   - Return (value, original_units) if conversion fails
   - Never raise for invalid input
   - Log debug for unrecognised units

4. **Package-level functions** (clean_drugs, check_health):
   - Raise KeyError for unknown step names
   - Raise SchemaValidationError for invalid input types
   - Never silently fail

Configuration
-------------
The cleaning package can be configured through:

1. **Environment variables:**

   - ``CLEANING_LAZY_IMPORTS=1`` — Enable lazy imports (default: 1)
   - ``CLEANING_SKIP_RDKIT=1`` — Skip RDKit import entirely (default: 0)
   - ``CLEANING_LOG_LEVEL=DEBUG`` — Set package-wide log level

2. **Programmatic configuration:**

   - ``cleaning.configure(fuzzy_threshold=0.8)`` — Override defaults
   - ``cleaning.configure(max_sequence_length=5000)`` — Override defaults

3. **Health and validation:**

   - ``cleaning.check_health()`` — Check which features are available
   - ``cleaning.validate_environment()`` — Check runtime requirements
   - ``cleaning.validate_all_exports()`` — Verify all exports are valid

Scalability Characteristics
---------------------------
- ``dedup_by_inchikey``: O(n log n) — sorts the entire DataFrame by InChIKey.
  Memory: 2x input (working copy + sort).  Tested up to 1M rows.
- ``handle_missing_inchikey``: O(n) with expensive RDKit call per row
  (10-100ms per SMILES).  For 10K rows with missing InChIKeys, expect
  100-1000 seconds.  Consider batching for larger datasets.
- ``convert_to_inchikey``: O(1) per call, but 10-100ms per SMILES.
  Not vectorized — each call creates a new RDKit Mol object.
- ``dedup_interactions``: O(n log n) — sorts by composite key + activity.
- ``fill_missing_drug_fields``: O(n) — single pass per column.
- ``validate_gda_scores``: O(n) — vectorized clip operation.

Memory: All functions create a copy of the input DataFrame.
For datasets >1M rows, consider processing in chunks.

Design Decisions
----------------
1. **Why re-export ALL public names from sub-modules?**
   The package re-exports every name listed in each sub-module's ``__all__``.
   This eliminates the "which import path should I use?" ambiguity.  If a name
   is public in a sub-module, it should be public at the package level too.

2. **Why lazy imports by default?**
   The cleaning package is imported by Airflow DAG files, which are parsed
   every 30 seconds.  Eager imports of pandas, numpy, and RDKit add
   500ms-5s per DAG parse cycle.  Lazy imports reduce this to near-zero.

3. **Why is ALLOWED_TYPES re-exported?**
   The ChEMBL pipeline imports it directly from ``cleaning.normalizer``.
   This creates two inconsistent API surfaces.  Re-exporting it at the
   package level makes ``from cleaning import ALLOWED_TYPES`` the canonical
   path.

4. **Why does ``fill_missing_drug_fields`` use ``None`` for max_phase
   instead of 0?**
   None means "unknown" — we don't know if the drug has clinical data.
   0 means "confirmed no clinical data."  These are semantically different.
   Using None preserves this distinction for downstream consumers.

Data Dictionary
---------------
Drug DataFrame columns processed by this package:

=========================  ===========  ========================================
Column                     Type         Description
=========================  ===========  ========================================
inchikey                   str          27-char InChIKey identifier (primary key)
smiles                     str          SMILES molecular representation
name                       str          Drug name (common or IUPAC)
drug_type                  str          One of ALLOWED_TYPES enum values
is_fda_approved            bool         Whether FDA-approved (default: False)
max_phase                  Int64/None   Maximum clinical trial phase (1-4, None=unknown)
mechanism_of_action        str          Mechanism of action description
molecular_formula          str          Molecular formula (e.g., C9H8O4)
molecular_weight           float/None   Molecular weight in Daltons
groups                     str/list     Drug status groups (approved, experimental, etc.)
=========================  ===========  ========================================

Protein DataFrame columns:

=========================  ===========  ========================================
Column                     Type         Description
=========================  ===========  ========================================
uniprot_id                 str          UniProt accession (primary key, required)
gene_name                  str          Gene symbol (e.g., BRCA1)
organism                   str          Source organism (default: Homo sapiens)
function_desc              str          Protein function description
sequence                   str          Amino acid sequence (truncated to 10,000 chars)
=========================  ===========  ========================================

GDA DataFrame columns:

=========================  ===========  ========================================
Column                     Type         Description
=========================  ===========  ========================================
disease_id                 str          Disease identifier (e.g., C0001 from DisGeNET)
disease_name               str          Disease name (backfilled from disease_id if missing)
score                      float        Association score, clipped to [0, 1]
association_type           str          Type of association (default: 'unknown')
gene_symbol                str          Gene symbol associated with the disease
=========================  ===========  ========================================

Required DataFrame schemas for cleaning functions:

- dedup_by_inchikey: requires 'inchikey' column
- dedup_interactions: requires columns specified in ``keys`` parameter
- handle_missing_inchikey: requires 'inchikey' column; uses 'smiles' if present
- fill_missing_drug_fields: no required columns (fills optional ones)
- handle_missing_protein_fields: requires 'uniprot_id' column
- validate_gda_scores: uses 'score', 'disease_name', 'disease_id',
  'association_type' if present

API Stability
-------------
Names in ``__all__`` are considered STABLE — they will not be removed or
have their behavior changed in a backward-incompatible way without:

1. A deprecation warning for at least one minor version
2. A migration guide in the CHANGELOG
3. The ``_API_VERSIONS`` dict being updated

Internal names (prefixed with _) are PRIVATE and may change without notice.

Dependency Compatibility
------------------------
- pandas >= 1.5, < 3.0
- numpy >= 1.20
- rdkit-pypi >= 2022.03  (x86_64 only; not available on ARM)
- rapidfuzz >= 3.0
- Python >= 3.9

Platform Compatibility
----------------------
- **Linux x86_64**: Fully supported (including RDKit)
- **macOS x86_64**: Fully supported (including RDKit)
- **macOS ARM (Apple Silicon)**: Partial — RDKit may not be available
  via pip; install via conda or use CLEANING_SKIP_RDKIT=1
- **Windows**: Not officially tested; path handling may have issues
- **AWS Graviton (ARM)**: Partial — same RDKit limitation as Apple Silicon

Access Control Note
-------------------
This package provides BOTH read-only cleaning functions (standardize_inchikey,
normalize_activity_value) and MUTATING cleaning functions (handle_missing_inchikey,
fill_missing_drug_fields, dedup_by_inchikey) that can drop records or overwrite
values with defaults.

In regulated environments (FDA 21 CFR Part 11, GxP), record deletion operations
must be auditable and restricted to authorized personnel.  This package does NOT
enforce access control — it relies on the calling pipeline to enforce permissions.

Recommended practice: Wrap the cleaning package in a permission-gated facade
in production environments.

PII Handling Guidance
---------------------
The cleaning functions process drug, protein, and disease data.  In clinical
contexts, this data could contain patient-identifiable information (e.g.,
a drug named after a patient in a case study, or a disease association with
a specific patient's genetic variant).

This package does NOT perform PII detection.  It is the caller's responsibility
to:

1. Strip PII from input data BEFORE passing it to cleaning functions
2. Anonymize or aggregate data that could identify individuals
3. Ensure compliance with GDPR, HIPAA, or other applicable regulations

If you are processing clinical data, use a PII detection tool (e.g.,
Microsoft Presidio) before the cleaning pipeline.

Secrets Management
------------------
The cleaning package does NOT use any secrets, API keys, or credentials
directly.  However, its transitive dependency chain includes:

- rdkit: Some commercial RDKit builds require a license key
- pandas/numpy: No secrets involved
- rapidfuzz: No secrets involved

If RDKit requires a license key, ensure it is stored in an environment
variable (e.g., RDKIT_LICENSE_PATH) or a secrets manager, NOT in source
code.  This package does not read or manage any license keys.

Regulatory Compliance
---------------------
This cleaning pipeline processes biomedical data that may be subject to:

- **FDA 21 CFR Part 11** (Electronic Records; Electronic Signatures):
  The audit trail and provenance tracking features support compliance
  by recording all data transformations.

- **GxP Guidelines** (Good Practice):
  The idempotent design and reproducibility features (fingerprinting,
  versioning) support validated data processing.

- **GDPR** (General Data Protection Regulation):
  If processing EU patient data, PII must be removed BEFORE entering
  the cleaning pipeline.  This package does not perform PII detection.

- **HIPAA** (Health Insurance Portability and Accountability Act):
  Protected Health Information (PHI) must be de-identified before
  processing.  The data masking utilities (_mask_sensitive) support
  this but do not guarantee compliance.

Consumers in regulated environments should:

1. Enable audit logging (use _audit_log for all operations)
2. Use compute_data_fingerprint() for input/output verification
3. Maintain the provenance metadata (_provenance in DataFrame.attrs)
4. Implement access control at the pipeline level

Structured Logging Convention
-----------------------------
All cleaning package log messages follow this format::

  FUNCTION_NAME: description — detail (N rows in, M rows out)

Log levels:

  DEBUG   — Internal state, skipped steps, empty DataFrames
  INFO    — Operations completed, rows affected, metrics
  WARNING — Anomalies that don't stop processing (missing columns, bad values)
  ERROR   — Operations that failed (import failures, dependency issues)
  CRITICAL — System-level failures that make the package unusable

Every operation log MUST include:

1. The function name (for filtering)
2. Row counts before/after (for data quality tracking)
3. Specific details about what changed

Coding Standards
----------------
This package follows:

- **PEP 8** — Style Guide for Python Code
- **PEP 257** — Docstring Conventions
- **PEP 328** — Imports: Multi-Line and Absolute/Relative (using relative imports)
- **PEP 562** — Module __getattr__ and __dir__ (lazy loading)
- **PEP 561** — Distributing and Packaging Type Information (py.typed marker)
- **numpydoc** — Docstring format (Parameters, Returns, Examples sections)

Examples
--------
Complete drug cleaning pipeline::

    >>> from cleaning import clean_drugs
    >>> cleaned = clean_drugs(raw_drugs_df)

Step-by-step with custom ordering::

    >>> from cleaning import (
    ...     standardize_inchikey,
    ...     handle_missing_inchikey,
    ...     fill_missing_drug_fields,
    ...     standardize_drug_record,
    ...     dedup_by_inchikey,
    ... )
    >>> df = standardize_inchikey(df)
    >>> df = handle_missing_inchikey(df)
    >>> df = fill_missing_drug_fields(df)
    >>> df = standardize_drug_record(df)
    >>> df = dedup_by_inchikey(df)

Check package health before processing::

    >>> from cleaning import check_health
    >>> health = check_health()
    >>> if health["status"] != "healthy":
    ...     print("Some features unavailable:", health)

Version History
---------------
- v2.0.0: Lazy imports, full __all__, health check, composition API,
  schema validation, dead-letter mechanism, provenance tracking,
  circuit breaker, correlation ID support.
- v1.0.0: Initial release with eager imports, partial __all__,
  no package-level utilities.

License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2024 Team Cosmic, VentureLab
"""

from __future__ import annotations

import contextvars
import functools
import hashlib
import importlib
import json
import logging
import os
import re
import time as _time
import warnings
from collections import defaultdict
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Module metadata
# ---------------------------------------------------------------------------
__version__: str = "2.0.0"

# ---------------------------------------------------------------------------
# Package-level logger (GAP-A4)
# ---------------------------------------------------------------------------
_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment variables (GAP-CF1)
# ---------------------------------------------------------------------------
_LAZY_IMPORTS_ENABLED: bool = os.environ.get("CLEANING_LAZY_IMPORTS", "1") == "1"
_SKIP_RDKIT: bool = os.environ.get("CLEANING_SKIP_RDKIT", "0") == "1"
_LOG_LEVEL: str = os.environ.get("CLEANING_LOG_LEVEL", "")

if _LOG_LEVEL:
    _logger.setLevel(getattr(logging, _LOG_LEVEL.upper(), logging.NOTSET))

if _SKIP_RDKIT:
    _logger.info(
        "CLEANING_SKIP_RDKIT=1 — RDKit-dependent functions will return None"
    )

# Idempotency note: Sub-modules are imported lazily on first access.
# Python's import system ensures each module is loaded exactly once.
# Module-level state (e.g., _RDKIT_AVAILABLE) is computed once and cached.
# If the environment changes after import (e.g., RDKit is installed mid-run),
# a module reload is required: importlib.reload(cleaning.normalizer)


# ===========================================================================
# Exception hierarchy (GAP-D5)
# ===========================================================================


class CleaningError(Exception):
    """Base exception for all cleaning package errors."""

    pass


class CleaningWarning(UserWarning):
    """Base warning for non-fatal cleaning issues."""

    pass


class SchemaValidationError(CleaningError):
    """Raised when input data doesn't match the expected schema."""

    pass


class DependencyNotAvailableError(CleaningError):
    """Raised when an optional dependency is required but not installed."""

    pass


# Error behavior contract:
# - Functions that CANNOT produce a valid result return None
#   (e.g., convert_to_inchikey)
# - Functions that process DataFrames return a (possibly modified) copy,
#   never None
# - Functions that encounter invalid input log a warning and skip the record
# - Functions that encounter a MISSING REQUIRED dependency raise
#   DependencyNotAvailableError
# - Functions that encounter an INVALID SCHEMA raise SchemaValidationError


# ===========================================================================
# Lazy import map (GAP-A2, BUG-A1, BUG-C1, BUG-D1)
# ===========================================================================

_LAZY_IMPORTS: dict[str, str] = {
    # v29 ROOT FIX (audit C-12): This registry, combined with the
    # module-level ``__getattr__`` below, implements the lazy-import
    # pattern demanded by audit C-12. Sub-modules (``normalizer``,
    # ``deduplicator``, ``missing_values``, ``confidence``) are NOT
    # imported at package import time — only when a consumer actually
    # accesses one of the names below. ``import cleaning`` is therefore
    # near-instant; the real cost is parsing this ~2400-line module
    # body (see the top-of-file warning). Splitting this file into
    # sub-packages would shrink the parse cost — that is future work.
    # normalizer
    "ALLOWED_TYPES": ".normalizer",
    "FUZZY_THRESHOLD": ".normalizer",
    "UNIT_CONVERSIONS": ".normalizer",
    "convert_to_inchikey": ".normalizer",
    "normalize_activity_value": ".normalizer",
    "standardize_drug_record": ".normalizer",
    "standardize_inchikey": ".normalizer",
    # [v2.1.0] New normalizer public symbols (ARCH-1, ARCH-2, ARCH-6,
    # DESIGN-3, DESIGN-8, DQ-2, IDEM-9, LOG-3, DQ-11, REL-4)
    "convert_to_inchikey_detailed": ".normalizer",
    "convert_to_inchikeys": ".normalizer",
    "normalize_inchikey": ".normalizer",
    "validate_inchikey": ".normalizer",
    "is_valid_inchikey": ".normalizer",
    "is_synthetic_inchikey": ".normalizer",
    "fuzzy_match_drug_type": ".normalizer",
    "fuzzy_match_drug_types": ".normalizer",
    "standardize_drug_records_batch": ".normalizer",
    "standardize_drug_records_chunked": ".normalizer",
    "normalize_activity_values": ".normalizer",
    "refresh_capabilities": ".normalizer",
    "get_dq_counts": ".normalizer",
    "reset_dq_counts": ".normalizer",
    "get_cache_info": ".normalizer",
    "configure_normalizer": ".normalizer",
    "save_config": ".normalizer",
    "load_config": ".normalizer",
    "validate_config": ".normalizer",
    "requires_api_version": ".normalizer",
    "is_backfill_needed": ".normalizer",
    "sign_output": ".normalizer",
    "get_validation_status": ".normalizer",
    "ActivityValue": ".normalizer",
    "ConversionResult": ".normalizer",
    "WITHDRAWN_GROUP_KEYWORDS": ".normalizer",
    "STEREO_POLICY": ".normalizer",
    "RECORD_SCHEMA": ".normalizer",
    # deduplicator — v1.0.0 stable + v3.0.0 new public API
    "ActivityDirection": ".deduplicator",
    "CompletenessWeight": ".deduplicator",
    "DEFAULT_COMPLETENESS_WEIGHTS": ".deduplicator",
    "DEFAULT_DPI_KEYS": ".deduplicator",
    "DedupResult": ".deduplicator",
    "DedupStrategy": ".deduplicator",
    "INVERSE_ACTIVITY_TYPES": ".deduplicator",
    "MAX_DATAFRAME_ROWS": ".deduplicator",
    "MAX_DEAD_LETTERS": ".deduplicator",
    "MAX_DROPPED_ROWS_IN_RESULT": ".deduplicator",
    "PERCENT_ACTIVITY_TYPES": ".deduplicator",
    "POTENCY_ACTIVITY_TYPES": ".deduplicator",
    "backfill_safety_check": ".deduplicator",
    "checkpoint_state": ".deduplicator",
    "clean_interactions": ".deduplicator",  # orchestrator in deduplicator.py
    "clear_dead_letters": ".deduplicator",
    "compute_completeness_score": ".deduplicator",
    "configure_deduplicator": ".deduplicator",
    "dedup_by_inchikey": ".deduplicator",
    "dedup_by_inchikey_chunked": ".deduplicator",
    "dedup_interactions": ".deduplicator",
    "flush_dead_letters": ".deduplicator",
    "get_correlation_id": ".deduplicator",
    "get_dead_letters": ".deduplicator",
    "get_metrics": ".deduplicator",
    "get_provenance": ".deduplicator",
    "health_check": ".deduplicator",
    "is_reproducible": ".deduplicator",
    "merge_duplicate_groups": ".deduplicator",
    "performance_benchmark": ".deduplicator",
    "quality_report": ".deduplicator",
    "recover_from_failure": ".deduplicator",
    "referential_integrity_check": ".deduplicator",
    "reproducibility_report": ".deduplicator",
    "requires_api_version": ".deduplicator",
    "reset_metrics": ".deduplicator",
    "revert_configuration": ".deduplicator",
    "set_correlation_id": ".deduplicator",
    "timing_report": ".deduplicator",
    "validate_config": ".deduplicator",
    "validate_environment": ".deduplicator",
    "validate_recovery_state": ".deduplicator",
    # missing_values
    "MAX_SEQUENCE_LENGTH": ".missing_values",
    "fill_missing_drug_fields": ".missing_values",
    "handle_missing_inchikey": ".missing_values",
    "handle_missing_protein_fields": ".missing_values",
    "validate_gda_scores": ".missing_values",
    # confidence — institutional-grade confidence-tier classifier (ARCH-7)
    "DEFAULT_CONFIDENCE_TIERS": ".confidence",
    "CONFIDENCE_TIER_METHOD_VERSION": ".confidence",
    "classify_confidence": ".confidence",
}

# Optional dependency map: name -> {dep_name: is_required} (GAP-A8)
_OPTIONAL_DEPS: dict[str, dict[str, bool]] = {
    "ALLOWED_TYPES": {},
    "FUZZY_THRESHOLD": {},
    "UNIT_CONVERSIONS": {},
    "convert_to_inchikey": {"rdkit": True},
    "standardize_inchikey": {"rdkit": False},
    "standardize_drug_record": {"rapidfuzz": False},
    "normalize_activity_value": {},
    "ActivityDirection": {},
    "CompletenessWeight": {},
    "DEFAULT_COMPLETENESS_WEIGHTS": {},
    "DEFAULT_DPI_KEYS": {},
    "DedupResult": {},
    "DedupStrategy": {},
    "INVERSE_ACTIVITY_TYPES": {},
    "MAX_DATAFRAME_ROWS": {},
    "MAX_DEAD_LETTERS": {},
    "MAX_DROPPED_ROWS_IN_RESULT": {},
    "PERCENT_ACTIVITY_TYPES": {},
    "POTENCY_ACTIVITY_TYPES": {},
    "backfill_safety_check": {},
    "checkpoint_state": {},
    "clean_interactions": {},
    "clear_dead_letters": {},
    "compute_completeness_score": {},
    "configure_deduplicator": {},
    "dedup_by_inchikey": {},
    "dedup_by_inchikey_chunked": {},
    "dedup_interactions": {},
    "flush_dead_letters": {},
    "get_correlation_id": {},
    "get_dead_letters": {},
    "get_metrics": {},
    "get_provenance": {},
    "health_check": {},
    "is_reproducible": {},
    "merge_duplicate_groups": {},
    "performance_benchmark": {},
    "quality_report": {},
    "recover_from_failure": {},
    "referential_integrity_check": {},
    "reproducibility_report": {},
    "requires_api_version": {},
    "reset_metrics": {},
    "revert_configuration": {},
    "set_correlation_id": {},
    "timing_report": {},
    "validate_config": {},
    "validate_environment": {},
    "validate_recovery_state": {},
    "MAX_SEQUENCE_LENGTH": {},
    "handle_missing_inchikey": {"rdkit": True},
    "fill_missing_drug_fields": {},
    "handle_missing_protein_fields": {},
    "validate_gda_scores": {},
    "DEFAULT_CONFIDENCE_TIERS": {},
    "CONFIDENCE_TIER_METHOD_VERSION": {},
    "classify_confidence": {},
}

# API version tracking (GAP-I4)
_API_VERSIONS: dict[str, str] = {
    # Original v1.0.0 public API
    "ALLOWED_TYPES": "1.0.0",
    "FUZZY_THRESHOLD": "1.0.0",
    "UNIT_CONVERSIONS": "1.0.0",
    "convert_to_inchikey": "1.0.0",
    "standardize_inchikey": "1.0.0",
    "standardize_drug_record": "1.0.0",
    "normalize_activity_value": "1.0.0",
    "dedup_by_inchikey": "3.0.0",
    "dedup_interactions": "3.0.0",
    "MAX_SEQUENCE_LENGTH": "1.0.0",
    "handle_missing_inchikey": "1.0.0",
    "fill_missing_drug_fields": "1.0.0",
    "handle_missing_protein_fields": "1.0.0",
    "validate_gda_scores": "1.0.0",
    "DEFAULT_CONFIDENCE_TIERS": "2.2.0",
    "CONFIDENCE_TIER_METHOD_VERSION": "2.2.0",
    "classify_confidence": "2.2.0",
    # [v2.1.0] New normalizer public symbols
    "ActivityValue": "2.1.0",
    "ConversionResult": "2.1.0",
    "RECORD_SCHEMA": "2.1.0",
    "STEREO_POLICY": "2.1.0",
    "WITHDRAWN_GROUP_KEYWORDS": "2.1.0",
    "configure_normalizer": "2.1.0",
    "convert_to_inchikey_detailed": "2.1.0",
    "convert_to_inchikeys": "2.1.0",
    "fuzzy_match_drug_type": "2.1.0",
    "fuzzy_match_drug_types": "2.1.0",
    "get_cache_info": "2.1.0",
    "get_dq_counts": "2.1.0",
    "get_validation_status": "2.1.0",
    "is_backfill_needed": "2.1.0",
    "is_synthetic_inchikey": "2.1.0",
    "is_valid_inchikey": "2.1.0",
    "load_config": "2.1.0",
    "normalize_activity_values": "2.1.0",
    "normalize_inchikey": "2.1.0",
    "refresh_capabilities": "2.1.0",
    "requires_api_version": "2.1.0",
    "reset_dq_counts": "2.1.0",
    "save_config": "2.1.0",
    "sign_output": "2.1.0",
    "standardize_drug_records_batch": "2.1.0",
    "standardize_drug_records_chunked": "2.1.0",
    "validate_config": "2.1.0",
    "validate_inchikey": "2.1.0",
    # [v3.0.0] New deduplicator public symbols
    "ActivityDirection": "3.0.0",
    "CompletenessWeight": "3.0.0",
    "DEFAULT_COMPLETENESS_WEIGHTS": "3.0.0",
    "DEFAULT_DPI_KEYS": "3.0.0",
    "DedupResult": "3.0.0",
    "DedupStrategy": "3.0.0",
    "INVERSE_ACTIVITY_TYPES": "3.0.0",
    "MAX_DATAFRAME_ROWS": "3.0.0",
    "MAX_DEAD_LETTERS": "3.0.0",
    "MAX_DROPPED_ROWS_IN_RESULT": "3.0.0",
    "PERCENT_ACTIVITY_TYPES": "3.0.0",
    "POTENCY_ACTIVITY_TYPES": "3.0.0",
    "backfill_safety_check": "3.0.0",
    "checkpoint_state": "3.0.0",
    "clean_interactions": "3.0.0",
    "clear_dead_letters": "3.0.0",
    "compute_completeness_score": "3.0.0",
    "configure_deduplicator": "3.0.0",
    "dedup_by_inchikey_chunked": "3.0.0",
    "flush_dead_letters": "3.0.0",
    "get_correlation_id": "3.0.0",
    "get_dead_letters": "3.0.0",
    "get_metrics": "3.0.0",
    "get_provenance": "3.0.0",
    "health_check": "3.0.0",
    "is_reproducible": "3.0.0",
    "merge_duplicate_groups": "3.0.0",
    "performance_benchmark": "3.0.0",
    "quality_report": "3.0.0",
    "recover_from_failure": "3.0.0",
    "referential_integrity_check": "3.0.0",
    "reproducibility_report": "3.0.0",
    "requires_api_version": "3.0.0",
    "reset_metrics": "3.0.0",
    "revert_configuration": "3.0.0",
    "set_correlation_id": "3.0.0",
    "timing_report": "3.0.0",
    "validate_config": "3.0.0",
    "validate_environment": "3.0.0",
    "validate_recovery_state": "3.0.0",
}

# Deprecated names: old_name -> replacement (GAP-CO6, GAP-IO6)
_DEPRECATED_NAMES: dict[str, str] = {}

# Dependency graph for impact analysis (GAP-DL4)
_CLEANING_DEPENDENCY_GRAPH: dict[str, list[str]] = {
    "inchikey": [
        "standardize_inchikey",
        "handle_missing_inchikey",
        "dedup_by_inchikey",
    ],
    "smiles": [
        "convert_to_inchikey",
        "handle_missing_inchikey",
        "fill_missing_drug_fields",
    ],
    "drug_type": [
        "standardize_drug_record",
        "fill_missing_drug_fields",
    ],
    "is_fda_approved": [
        "standardize_drug_record",
        "fill_missing_drug_fields",
    ],
    "max_phase": [
        "standardize_drug_record",
        "fill_missing_drug_fields",
    ],
    "uniprot_id": ["handle_missing_protein_fields"],
    "organism": ["handle_missing_protein_fields"],
    "sequence": ["handle_missing_protein_fields"],
    "score": ["validate_gda_scores"],
    "disease_name": ["validate_gda_scores"],
    "association_type": ["validate_gda_scores"],
    # [v3.0.0] dedup_interactions affects these columns (ARCH-5, LINEAGE-5)
    "activity_value": ["dedup_interactions"],
    "activity_type": ["dedup_interactions"],
    "activity_units": ["dedup_interactions"],
    "confidence_score": ["dedup_interactions"],
    "drug_id": ["dedup_interactions"],
    "protein_id": ["dedup_interactions"],
    "source_id": ["dedup_interactions"],
}


# ===========================================================================
# Dead-letter mechanism (GAP-R3)
# ===========================================================================

_dead_letters: list[dict[str, Any]] = []


def get_dead_letters() -> list[dict[str, Any]]:
    """Return records that failed cleaning, for inspection and recovery.

    FIX-F / C-18: this function now AGGREGATES dead letters from ALL
    three cleaning submodules:

      * ``cleaning`` (package-level ``_dead_letters``)
      * ``cleaning.deduplicator._dead_letters``
      * ``cleaning.missing_values._dead_letters``

    Previously it returned ONLY the package-level queue, silently
    dropping dead letters raised inside the deduplicator (e.g., dropped
    duplicates from ``dedup_interactions``) and missing-value handlers
    (e.g., rows that failed imputation). The audit (C-18) found three
    parallel queues with no unified view — operators had to call
    ``deduplicator.get_dead_letters()`` and ``missing_values.get_dead_letters()``
    separately to see the full failure set. This function now returns
    the union so a single call captures every dead letter.

    The aggregation is read-only — entries are NOT moved or copied
    between queues. Each submodule keeps its own queue (preserving the
    per-module bounded-FIFO eviction semantics); this function simply
    concatenates snapshots.

    Returns
    -------
    list[dict]
        A list of dead-letter entries from all three queues. Each entry
        is a dict; the key set varies by submodule (see each module's
        ``_append_dead_letter`` for the exact schema).
    """
    aggregated = list(_dead_letters)  # package-level queue
    try:
        from .deduplicator import _dead_letters as _dedup_queue
        aggregated.extend(list(_dedup_queue))
    except Exception:  # pragma: no cover — defensive: deduplicator import must not break DLQ reads
        pass
    try:
        from .missing_values import _dead_letters as _mv_queue
        aggregated.extend(list(_mv_queue))
    except Exception:  # pragma: no cover — defensive
        pass
    return aggregated


def clear_dead_letters() -> None:
    """Clear the dead-letter queue."""
    _dead_letters.clear()


def _add_dead_letter(record: Any, step: str, reason: str) -> None:
    """Add a failed record to the dead-letter queue.

    Parameters
    ----------
    record : Any
        The record that failed processing.
    step : str
        The cleaning step that failed.
    reason : str
        Human-readable reason for the failure.
    """
    import datetime

    entry = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "step": step,
        "reason": reason,
        "record_preview": str(record)[:500],
    }
    _dead_letters.append(entry)
    _logger.debug("Dead letter added: step=%s reason=%s", step, reason)


# ===========================================================================
# Circuit breaker (GAP-R5)
# ===========================================================================


class _CircuitBreaker:
    """Simple circuit breaker that opens after N consecutive failures."""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        reset_timeout: float = 60.0,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.failure_count: int = 0
        self.last_failure_time: float = 0.0
        self.state: str = "closed"  # closed, open, half-open

    def record_success(self) -> None:
        """Record a successful operation, resetting the breaker."""
        self.failure_count = 0
        self.state = "closed"

    def record_failure(self) -> None:
        """Record a failed operation, potentially opening the breaker."""
        self.failure_count += 1
        self.last_failure_time = _time.time()
        if self.failure_count >= self.failure_threshold:
            self.state = "open"
            _logger.error(
                "Circuit breaker '%s' OPENED after %d consecutive failures",
                self.name,
                self.failure_count,
            )

    def allow_request(self) -> bool:
        """Check if a request should be allowed through.

        Returns
        -------
        bool
            True if the request should proceed, False if blocked.
        """
        if self.state == "closed":
            return True
        if self.state == "open":
            if _time.time() - self.last_failure_time > self.reset_timeout:
                self.state = "half-open"
                _logger.info(
                    "Circuit breaker '%s' moved to HALF-OPEN", self.name
                )
                return True
            return False
        # half-open: allow one request
        return True


_circuit_breakers: dict[str, _CircuitBreaker] = {}


def get_circuit_breaker(name: str) -> _CircuitBreaker:
    """Get or create a circuit breaker for a named operation.

    Parameters
    ----------
    name : str
        Name of the operation to protect.

    Returns
    -------
    _CircuitBreaker
        The circuit breaker instance for the given name.
    """
    if name not in _circuit_breakers:
        _circuit_breakers[name] = _CircuitBreaker(name)
    return _circuit_breakers[name]


# ===========================================================================
# Correlation ID support (GAP-L5)
# ===========================================================================

_correlation_id: contextvars.ContextVar[str | None] = (
    contextvars.ContextVar("cleaning_correlation_id", default=None)
)


def set_correlation_id(cid: str | None) -> None:
    """Set a correlation ID for tracing cleaning operations.

    Parameters
    ----------
    cid : str or None
        The correlation ID to set, or None to clear it.
    """
    _correlation_id.set(cid)


def get_correlation_id() -> str | None:
    """Get the current correlation ID.

    Returns
    -------
    str or None
        The current correlation ID, or None if not set.
    """
    return _correlation_id.get()


# ===========================================================================
# Cleaning registry (GAP-D3)
# ===========================================================================

_CLEANING_REGISTRY: dict[str, Callable] = {}


def _register_cleaning_function(name: str, func: Callable) -> None:
    """Register a cleaning function in the package registry.

    Parameters
    ----------
    name : str
        The name under which to register the function.
    func : Callable
        The cleaning function to register.
    """
    if name in _CLEANING_REGISTRY:
        _logger.warning(
            "Overwriting existing cleaning function: %s", name
        )
    _CLEANING_REGISTRY[name] = func


def get_cleaning_function(name: str) -> Callable:
    """Retrieve a registered cleaning function by name.

    Parameters
    ----------
    name : str
        The name of the cleaning function to retrieve.

    Returns
    -------
    Callable
        The registered cleaning function.

    Raises
    ------
    KeyError
        If no function with the given name is registered.
    """
    if name not in _CLEANING_REGISTRY:
        raise KeyError(
            f"No cleaning function named {name!r}. "
            f"Available: {sorted(_CLEANING_REGISTRY.keys())}"
        )
    return _CLEANING_REGISTRY[name]


def list_cleaning_functions() -> list[str]:
    """Return sorted list of all registered cleaning function names.

    Returns
    -------
    list[str]
        Sorted list of registered function names.
    """
    return sorted(_CLEANING_REGISTRY.keys())


# ===========================================================================
# Pre/post clean hooks (GAP-IO5)
# ===========================================================================

_pre_clean_hooks: list[Callable] = []
_post_clean_hooks: list[Callable] = []


def register_pre_clean_hook(hook: Callable) -> None:
    """Register a callback to run before each cleaning step.

    Parameters
    ----------
    hook : Callable
        A callable accepting (step_name: str, df: pd.DataFrame).
    """
    _pre_clean_hooks.append(hook)


def register_post_clean_hook(hook: Callable) -> None:
    """Register a callback to run after each cleaning step.

    Parameters
    ----------
    hook : Callable
        A callable accepting (step_name: str, df: pd.DataFrame).
    """
    _post_clean_hooks.append(hook)


# ===========================================================================
# Module load-time tracking (GAP-L2)
# ===========================================================================

_MODULE_LOAD_TIMES: dict[str, float] = {}


# ===========================================================================
# Lazy loading via __getattr__ (GAP-A2, BUG-A3)
# ===========================================================================


def __getattr__(name: str):
    """Lazily load sub-module attributes on first access.

    This defers importing sub-modules until they're actually needed.
    ``import cleaning`` becomes near-instant.  ``from cleaning import X``
    triggers the import only when X is accessed.

    If a sub-module fails to import, the failure is isolated — only that
    name is unavailable; the rest of the package still works.
    """
    # Handle deprecated name redirections
    if name in _DEPRECATED_NAMES:
        replacement = _DEPRECATED_NAMES[name]
        msg = (
            f"cleaning.{name} is deprecated and will be removed in v3.0.0. "
            f"Use cleaning.{replacement} instead."
        )
        warnings.warn(msg, DeprecationWarning, stacklevel=2)
        name = replacement

    if name in _LAZY_IMPORTS:
        module_name = _LAZY_IMPORTS[name]
        start = _time.perf_counter()
        try:
            module = importlib.import_module(module_name, __name__)
            elapsed = _time.perf_counter() - start
            _MODULE_LOAD_TIMES[name] = elapsed
            _logger.debug(
                "Lazy-loaded %r from %s in %.3f seconds",
                name,
                module_name,
                elapsed,
            )
            attr = getattr(module, name)
            if callable(attr):
                # Attach lineage metadata (GAP-DL5)
                attr._cleaning_source_module = _LAZY_IMPORTS.get(
                    name, ""
                )
                attr._cleaning_api_version = _API_VERSIONS.get(
                    name, "0.0.0"
                )
                _CLEANING_REGISTRY[name] = attr
            return attr
        except ImportError as exc:
            elapsed = _time.perf_counter() - start
            _logger.error(
                "Failed to import %r from %s after %.3f seconds: %s. "
                "Sub-module may have missing dependencies.",
                name,
                module_name,
                elapsed,
                exc,
            )
            raise
        except Exception as exc:
            elapsed = _time.perf_counter() - start
            _logger.critical(
                "Unexpected error importing %r from %s after %.3f seconds: %s",
                name,
                module_name,
                elapsed,
                exc,
                exc_info=True,
            )
            raise

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Control dir() output to show only public names (GAP-C7)."""
    return sorted(set(globals().keys()) | set(__all__))


# ===========================================================================
# Health check (GAP-A6)
# ===========================================================================


def check_health() -> dict:
    """Return a health status dict for the cleaning package.

    Checks which sub-modules loaded and which optional dependencies
    are available, without raising exceptions on failure.

    Returns
    -------
    dict
        Keys: 'status' ('healthy'|'degraded'), 'version', 'modules',
        'optional_deps'.
    """
    health: dict[str, Any] = {
        "status": "healthy",
        "version": __version__,
        "modules": {},
        "optional_deps": {},
    }
    for name, module_path in _LAZY_IMPORTS.items():
        try:
            importlib.import_module(module_path, __name__)
            health["modules"][name] = "available"
        except Exception:
            health["modules"][name] = "unavailable"
            health["status"] = "degraded"

    # Check optional dependencies
    try:
        import rdkit  # noqa: F401

        health["optional_deps"]["rdkit"] = True
    except ImportError:
        health["optional_deps"]["rdkit"] = False
        health["status"] = "degraded"

    try:
        import rapidfuzz  # noqa: F401

        health["optional_deps"]["rapidfuzz"] = True
    except ImportError:
        health["optional_deps"]["rapidfuzz"] = False
        # Not degraded — rapidfuzz is truly optional (fallback exists)

    return health


# ===========================================================================
# Dependency availability queries (GAP-R6)
# ===========================================================================


def has_rdkit_support() -> bool:
    """Check if RDKit is available for InChIKey conversion.

    Returns
    -------
    bool
        True if RDKit is importable, False otherwise.
    """
    try:
        import rdkit  # noqa: F401

        return True
    except ImportError:
        return False


def has_rapidfuzz_support() -> bool:
    """Check if rapidfuzz is available for fuzzy matching.

    Returns
    -------
    bool
        True if rapidfuzz is importable, False otherwise.
    """
    try:
        import rapidfuzz  # noqa: F401

        return True
    except ImportError:
        return False


# ===========================================================================
# Export validation (GAP-C5)
# ===========================================================================


def validate_all_exports() -> list[str]:
    """Verify that every name in __all__ can be resolved.

    Returns
    -------
    list[str]
        A list of names that FAILED to resolve.  An empty list
        means all exports are valid.

    This function is intended for use in test suites, not at normal
    import time (to avoid the cost of importing all sub-modules).
    """
    failures: list[str] = []
    for name in __all__:
        try:
            __getattr__(name)
        except (ImportError, AttributeError) as exc:
            failures.append(f"{name}: {exc}")
    return failures


# ===========================================================================
# Environment validation (GAP-CF2)
# ===========================================================================


def validate_environment() -> dict:
    """Validate that the runtime environment meets package requirements.

    Returns
    -------
    dict
        Keys: 'python_version', 'required_deps', 'optional_deps',
        'issues'.
    """
    import sys

    result: dict[str, Any] = {
        "python_version": sys.version,
        "required_deps": {},
        "optional_deps": {},
        "issues": [],
    }

    # Required dependencies
    for dep in ("pandas", "numpy"):
        try:
            mod = __import__(dep)
            result["required_deps"][dep] = getattr(
                mod, "__version__", "unknown"
            )
        except ImportError:
            result["required_deps"][dep] = "MISSING"
            result["issues"].append(
                f"Required dependency '{dep}' is not installed"
            )

    # Optional dependencies
    for dep in ("rdkit", "rapidfuzz"):
        try:
            mod = __import__(dep)
            result["optional_deps"][dep] = getattr(
                mod, "__version__", "unknown"
            )
        except ImportError:
            result["optional_deps"][dep] = "not installed"

    # Python version check
    if sys.version_info < (3, 9):
        result["issues"].append(
            f"Python 3.9+ required, got "
            f"{sys.version_info.major}.{sys.version_info.minor}"
        )

    return result


# ===========================================================================
# Data quality report (GAP-DQ2)
# ===========================================================================


def quality_report(
    df: Any, *, data_type: str = "drug"
) -> dict:
    """Run data quality checks on a DataFrame and return a summary report.

    Parameters
    ----------
    df : pd.DataFrame
        Data to validate.
    data_type : str
        One of 'drug', 'protein', 'gda', 'interaction'.

    Returns
    -------
    dict
        Quality metrics including completeness, uniqueness, validity.
    """
    import pandas as pd

    report: dict[str, Any] = {
        "data_type": data_type,
        "total_rows": len(df),
        "total_columns": len(df.columns),
        "completeness": {},
        "uniqueness": {},
        "validity": {},
    }

    # Completeness: percentage of non-null values per column
    for col in df.columns:
        non_null_pct = (
            (df[col].notna().sum() / len(df)) * 100 if len(df) > 0 else 0
        )
        report["completeness"][col] = round(non_null_pct, 2)

    # Uniqueness
    if "inchikey" in df.columns and data_type == "drug":
        valid_keys = df["inchikey"].dropna()
        duplicate_count = valid_keys.duplicated().sum()
        report["uniqueness"]["inchikey_duplicates"] = int(duplicate_count)

    if "uniprot_id" in df.columns and data_type == "protein":
        valid_ids = df["uniprot_id"].dropna()
        duplicate_count = valid_ids.duplicated().sum()
        report["uniqueness"]["uniprot_id_duplicates"] = int(
            duplicate_count
        )

    # Validity: check for out-of-range values
    if "score" in df.columns:
        numeric_scores = pd.to_numeric(df["score"], errors="coerce")
        out_of_range = int(
            ((numeric_scores < 0) | (numeric_scores > 1)).sum()
        )
        report["validity"]["gda_scores_out_of_range"] = out_of_range

    return report


# ===========================================================================
# Data fingerprinting (GAP-I3)
# ===========================================================================


def compute_data_fingerprint(df: Any) -> str:
    """Compute a deterministic SHA-256 hash of a DataFrame's content.

    This allows verifying that the same input data produces the same
    fingerprint across different runs, ensuring reproducibility.

    Parameters
    ----------
    df : pd.DataFrame
        Data to fingerprint.

    Returns
    -------
    str
        Hex-encoded SHA-256 hash.
    """
    # Sort columns for determinism
    sorted_df = df[sorted(df.columns)]
    # Convert to a canonical string representation
    data_str = sorted_df.to_csv(index=False, float_format="%.10f")
    return hashlib.sha256(data_str.encode("utf-8")).hexdigest()


# ===========================================================================
# Sanitization utilities (GAP-S1)
# ===========================================================================


def _sanitize_string(value: str, *, max_length: int = 10_000) -> str:
    """Sanitize a string value for safe processing.

    - Truncates to max_length to prevent buffer overflow attacks
    - Removes null bytes
    - Strips control characters except newline and tab

    Parameters
    ----------
    value : str
        The string to sanitize.
    max_length : int
        Maximum allowed length.  Default: 10,000.

    Returns
    -------
    str
        Sanitized string.
    """
    if not isinstance(value, str):
        return str(value)
    # Remove null bytes
    value = value.replace("\x00", "")
    # Remove control characters except \n and \t
    value = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    # Truncate
    if len(value) > max_length:
        _logger.warning(
            "String value truncated from %d to %d characters",
            len(value),
            max_length,
        )
        value = value[:max_length]
    return value


# ===========================================================================
# Data masking (GUARD-S4)
# ===========================================================================


def _mask_sensitive(value: str, visible_chars: int = 10) -> str:
    """Mask sensitive data in log messages, showing only the first few chars.

    Parameters
    ----------
    value : str
        The string to mask.
    visible_chars : int
        Number of characters to show before masking.

    Returns
    -------
    str
        Masked string like 'CC(=O)OC1***...***' (first N chars + asterisks).
    """
    if not isinstance(value, str):
        return str(value)
    if len(value) <= visible_chars:
        return value
    return value[:visible_chars] + "***...***"


# ===========================================================================
# Audit logging (GAP-S2)
# ===========================================================================


def _audit_log(
    operation: str, details: dict | None = None
) -> None:
    """Log an audit-trail entry for a cleaning operation.

    Parameters
    ----------
    operation : str
        Name of the operation being audited.
    details : dict or None
        Additional details about the operation.
    """
    import datetime

    entry: dict[str, Any] = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "package": "cleaning",
        "version": __version__,
        "operation": operation,
        "details": details or {},
    }
    if _correlation_id.get():
        entry["correlation_id"] = _correlation_id.get()
    _logger.info("AUDIT: %s", json.dumps(entry, default=str))


# ===========================================================================
# Provenance tracking (GAP-I5, GAP-DL1)
# ===========================================================================


def _add_provenance(
    df: Any, step: str, params: dict | None = None
) -> Any:
    """Add provenance metadata to a cleaned DataFrame.

    Parameters
    ----------
    df : pd.DataFrame
        The DataFrame to annotate.
    step : str
        The cleaning step name.
    params : dict or None
        Parameters used for the step.

    Returns
    -------
    pd.DataFrame
        The same DataFrame with provenance metadata added to attrs.
    """
    import datetime

    provenance = {
        "cleaning_package_version": __version__,
        "step": step,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "params": params or {},
    }
    if _correlation_id.get():
        provenance["correlation_id"] = _correlation_id.get()
    if "_provenance" not in df.attrs:
        df.attrs["_provenance"] = []
    df.attrs["_provenance"].append(provenance)
    return df


# ===========================================================================
# Cleaning metadata tracking (GUARD-DQ5, GAP-DQ6)
# ===========================================================================

_CLEANING_METADATA_COL = "_cleaning_applied"


def _mark_cleaned(df: Any, step_name: str) -> Any:
    """Add metadata tracking which cleaning steps have been applied.

    Parameters
    ----------
    df : pd.DataFrame
        The DataFrame to mark.
    step_name : str
        Name of the cleaning step that was applied.

    Returns
    -------
    pd.DataFrame
        The same DataFrame with the cleaning metadata column updated.
    """
    import pandas as pd

    if _CLEANING_METADATA_COL not in df.columns:
        df[_CLEANING_METADATA_COL] = ""
    df[_CLEANING_METADATA_COL] = (
        df[_CLEANING_METADATA_COL].astype(str) + step_name + ";"
    )
    return df


def _is_already_cleaned(df: Any, step_name: str) -> bool:
    """Check if a specific cleaning step has already been applied.

    Parameters
    ----------
    df : pd.DataFrame
        The DataFrame to check.
    step_name : str
        Name of the cleaning step to check for.

    Returns
    -------
    bool
        True if the step appears to have been applied already.
    """
    if _CLEANING_METADATA_COL not in df.columns:
        return False
    return df[_CLEANING_METADATA_COL].str.contains(step_name).any()


# ===========================================================================
# Retry decorator (GAP-R4)
# ===========================================================================


def _retry(max_retries: int = 2, backoff_factor: float = 1.0):
    """Decorator that retries a function on transient failures.

    Parameters
    ----------
    max_retries : int
        Maximum number of retry attempts.  Default: 2.
    backoff_factor : float
        Base for exponential backoff.  Default: 1.0.
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc: Exception | None = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    last_exc = exc
                    if attempt < max_retries:
                        wait_time = backoff_factor * (2**attempt)
                        _logger.warning(
                            "Retry %d/%d for %s after error: %s "
                            "(waiting %.1fs)",
                            attempt + 1,
                            max_retries,
                            func.__name__,
                            exc,
                            wait_time,
                        )
                        _time.sleep(wait_time)
                    else:
                        _logger.error(
                            "All %d retries exhausted for %s: %s",
                            max_retries,
                            func.__name__,
                            exc,
                        )
            raise last_exc  # type: ignore[misc]

        return wrapper

    return decorator


# ===========================================================================
# Performance timing decorator (GAP-P3)
# ===========================================================================


def _timed(func: Callable) -> Callable:
    """Decorator that logs execution time for cleaning functions."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = _time.perf_counter()
        try:
            result = func(*args, **kwargs)
        finally:
            elapsed = _time.perf_counter() - start
            _logger.info(
                "%s: completed in %.3f seconds", func.__name__, elapsed
            )
            if hasattr(result, "attrs"):
                result.attrs[f"_timing_{func.__name__}"] = elapsed
        return result

    return wrapper


# ===========================================================================
# Ordering validation (GUARD-D7)
# ===========================================================================


def _validate_step_order(steps: list[str]) -> None:
    """Warn if steps are in a scientifically incorrect order.

    Parameters
    ----------
    steps : list[str]
        The list of cleaning step names in the order they will be applied.
    """
    step_ranks = {
        name: i
        for i, name in enumerate(
            [
                "standardize_inchikey",
                "handle_missing_inchikey",
                "fill_missing_drug_fields",
                "standardize_drug_record",
                "dedup_by_inchikey",
            ]
        )
    }
    relevant = [
        (name, step_ranks[name])
        for name in steps
        if name in step_ranks
    ]
    for i in range(len(relevant) - 1):
        if relevant[i][1] > relevant[i + 1][1]:
            _logger.warning(
                "clean_drugs: step %r comes before %r, but the "
                "recommended order is the reverse.  This may produce "
                "silently incorrect results.",
                relevant[i][0],
                relevant[i + 1][0],
            )


# ===========================================================================
# Composition/Chaining API (GAP-D6)
# ===========================================================================


def clean_drugs(
    df: Any,
    *,
    steps: list[str] | None = None,
    skip_steps: set[str] | None = None,
) -> Any:
    """Apply the recommended cleaning pipeline to a drug DataFrame.

    Parameters
    ----------
    df : pd.DataFrame
        Raw drug records.
    steps : list[str] or None
        Ordered list of cleaning function names to apply.  If None,
        uses the default recommended order.
    skip_steps : set[str] or None
        Names of steps to skip.  Useful for partial re-cleaning.

    Returns
    -------
    pd.DataFrame
        Cleaned drug records.

    Raises
    ------
    KeyError
        If a step name is not in the cleaning registry or lazy imports.
    SchemaValidationError
        If the input is not a pandas DataFrame.
    """
    import pandas as pd

    if not isinstance(df, pd.DataFrame):
        raise SchemaValidationError(
            f"clean_drugs expects a pandas DataFrame, "
            f"got {type(df).__name__}"
        )

    default_steps = [
        "standardize_inchikey",
        "handle_missing_inchikey",
        "fill_missing_drug_fields",
        "standardize_drug_record",
        "dedup_by_inchikey",
    ]
    steps = steps or default_steps
    skip_steps = skip_steps or set()

    # Validate step ordering (GUARD-D7)
    _validate_step_order(steps)

    # Cross-step consistency check (GAP-DQ4)
    if (
        "handle_missing_inchikey" in steps
        and "fill_missing_drug_fields" in steps
    ):
        hmm_idx = steps.index("handle_missing_inchikey")
        fmf_idx = steps.index("fill_missing_drug_fields")
        if fmf_idx < hmm_idx:
            _logger.warning(
                "clean_drugs: fill_missing_drug_fields runs before "
                "handle_missing_inchikey.  Empty-string defaults for "
                "'smiles' may prevent InChIKey recovery."
            )

    # Ensure all steps are loaded (triggers lazy imports)
    for step_name in steps:
        if step_name not in _CLEANING_REGISTRY and step_name in _LAZY_IMPORTS:
            # Force lazy load
            __getattr__(step_name)

    # Record input fingerprint for lineage (GAP-DL3)
    input_fingerprint = compute_data_fingerprint(df)

    out = df.copy()
    _metrics: dict[str, dict[str, int]] = defaultdict(dict)

    for step_name in steps:
        if step_name in skip_steps:
            _logger.info("clean_drugs: skipping step %r", step_name)
            continue

        # Double-cleaning guard (GUARD-DQ5)
        if _is_already_cleaned(out, step_name):
            _logger.warning(
                "clean_drugs: step %r appears to have already been "
                "applied.  Re-applying may overwrite data with "
                "defaults.  Skipping.",
                step_name,
            )
            continue

        func = _CLEANING_REGISTRY.get(step_name)
        if func is None:
            raise KeyError(
                f"Unknown cleaning step: {step_name!r}. "
                f"Available: {sorted(_CLEANING_REGISTRY.keys())}"
            )

        # Pre-clean hooks (GAP-IO5)
        for hook in _pre_clean_hooks:
            try:
                hook(step_name, out)
            except Exception as hook_exc:
                _logger.warning(
                    "Pre-clean hook failed for step %r: %s",
                    step_name,
                    hook_exc,
                )

        before_rows = len(out)
        _logger.info(
            "clean_drugs: applying step %r (%d rows)",
            step_name,
            before_rows,
        )

        # Apply the cleaning function.
        # Some functions are scalar (take a single value, not a DataFrame).
        # We need to wrap them for DataFrame-level application.
        if step_name == "standardize_inchikey":
            # standardize_inchikey takes a string, returns a string.
            # Apply it element-wise to the 'inchikey' column.
            if "inchikey" in out.columns:
                out["inchikey"] = out["inchikey"].apply(
                    lambda x: func(x) if isinstance(x, str) and x.strip() else x
                )
            else:
                _logger.warning(
                    "clean_drugs: standardize_inchikey skipped — "
                    "'inchikey' column not found"
                )
        elif step_name == "standardize_drug_record":
            # standardize_drug_record takes a dict, returns a dict.
            # Apply it row-by-row, converting each row to/from a dict.
            import numpy as np

            def _apply_drug_record(row):
                record = row.to_dict()
                cleaned = func(record)
                return pd.Series(cleaned)

            # Only apply to rows that have data worth normalizing
            result_rows = out.apply(_apply_drug_record, axis=1)
            # Update out with cleaned values, preserving columns that
            # were not in the record.
            # [v2.1.0] Skip _-prefixed metadata columns (e.g., _provenance,
            # _cleaning_applied) to keep the output deterministic across
            # runs — they contain per-row timestamps that break fingerprint
            # reproducibility (IDEM-7).  Provenance is still accessible via
            # df.attrs["_provenance"] for callers that need it.
            for col in result_rows.columns:
                if col.startswith("_"):
                    continue
                if col in out.columns:
                    out[col] = result_rows[col].values
                else:
                    out[col] = result_rows[col].values
        else:
            # DataFrame -> DataFrame functions
            out = func(out)

        after_rows = len(out)
        _metrics[step_name] = {
            "rows_before": before_rows,
            "rows_after": after_rows,
            "rows_dropped": before_rows - after_rows,
        }

        if after_rows != before_rows:
            _logger.info(
                "clean_drugs: step %r changed row count: "
                "%d -> %d (%+d)",
                step_name,
                before_rows,
                after_rows,
                after_rows - before_rows,
            )

        # Mark as cleaned
        _mark_cleaned(out, step_name)

        # Add provenance metadata (GAP-I5)
        _add_provenance(out, step_name)

        # Post-clean hooks (GAP-IO5)
        for hook in _post_clean_hooks:
            try:
                hook(step_name, out)
            except Exception as hook_exc:
                _logger.warning(
                    "Post-clean hook failed for step %r: %s",
                    step_name,
                    hook_exc,
                )

    # Record output fingerprint (GAP-DL3)
    output_fingerprint = compute_data_fingerprint(out)
    out.attrs["_input_fingerprint"] = input_fingerprint
    out.attrs["_output_fingerprint"] = output_fingerprint
    out.attrs["cleaning_metrics"] = dict(_metrics)

    # Audit log (GAP-S2)
    _audit_log(
        "clean_drugs",
        {
            "steps": steps,
            "skipped": list(skip_steps),
            "input_rows": len(df),
            "output_rows": len(out),
            "input_fingerprint": input_fingerprint,
            "output_fingerprint": output_fingerprint,
        },
    )

    return out


def clean_proteins(df: Any) -> Any:
    """Apply the recommended cleaning pipeline to a protein DataFrame.

    Parameters
    ----------
    df : pd.DataFrame
        Raw protein records.

    Returns
    -------
    pd.DataFrame
        Cleaned protein records.
    """
    import pandas as pd

    if not isinstance(df, pd.DataFrame):
        raise SchemaValidationError(
            f"clean_proteins expects a pandas DataFrame, "
            f"got {type(df).__name__}"
        )

    # Force lazy load
    if "handle_missing_protein_fields" not in _CLEANING_REGISTRY:
        __getattr__("handle_missing_protein_fields")

    input_fingerprint = compute_data_fingerprint(df)
    func = _CLEANING_REGISTRY["handle_missing_protein_fields"]
    out = func(df)
    _add_provenance(out, "handle_missing_protein_fields")
    output_fingerprint = compute_data_fingerprint(out)
    out.attrs["_input_fingerprint"] = input_fingerprint
    out.attrs["_output_fingerprint"] = output_fingerprint

    _audit_log(
        "clean_proteins",
        {
            "input_rows": len(df),
            "output_rows": len(out),
            "input_fingerprint": input_fingerprint,
            "output_fingerprint": output_fingerprint,
        },
    )

    return out


def clean_gda(df: Any) -> Any:
    """Apply the recommended cleaning pipeline to a GDA DataFrame.

    Parameters
    ----------
    df : pd.DataFrame
        Raw GDA records.

    Returns
    -------
    pd.DataFrame
        Cleaned GDA records.
    """
    import pandas as pd

    if not isinstance(df, pd.DataFrame):
        raise SchemaValidationError(
            f"clean_gda expects a pandas DataFrame, "
            f"got {type(df).__name__}"
        )

    # Force lazy load
    if "validate_gda_scores" not in _CLEANING_REGISTRY:
        __getattr__("validate_gda_scores")

    input_fingerprint = compute_data_fingerprint(df)
    func = _CLEANING_REGISTRY["validate_gda_scores"]
    out = func(df)
    _add_provenance(out, "validate_gda_scores")
    output_fingerprint = compute_data_fingerprint(out)
    out.attrs["_input_fingerprint"] = input_fingerprint
    out.attrs["_output_fingerprint"] = output_fingerprint

    _audit_log(
        "clean_gda",
        {
            "input_rows": len(df),
            "output_rows": len(out),
            "input_fingerprint": input_fingerprint,
            "output_fingerprint": output_fingerprint,
        },
    )

    return out


# ===========================================================================
# Chunked processing (GAP-P5)
# ===========================================================================


def clean_drugs_chunked(
    df: Any,
    chunk_size: int = 10_000,
    *,
    steps: list[str] | None = None,
    skip_steps: set[str] | None = None,
) -> Any:
    """Apply the cleaning pipeline in chunks to reduce memory usage.

    Parameters
    ----------
    df : pd.DataFrame
        Raw drug records.
    chunk_size : int
        Number of rows per chunk.  Default: 10,000.
    steps : list[str] or None
        Cleaning steps to apply.
    skip_steps : set[str] or None
        Steps to skip.

    Returns
    -------
    pd.DataFrame
        Concatenated cleaned results.
    """
    import pandas as pd

    chunks: list = []
    for start in range(0, len(df), chunk_size):
        chunk = df.iloc[start : start + chunk_size]
        cleaned_chunk = clean_drugs(
            chunk, steps=steps, skip_steps=skip_steps
        )
        chunks.append(cleaned_chunk)
        _logger.info(
            "clean_drugs_chunked: processed chunk %d-%d (%d rows)",
            start,
            min(start + chunk_size, len(df)),
            len(cleaned_chunk),
        )
    return pd.concat(chunks, ignore_index=True)


# ===========================================================================
# Configuration (GAP-I2)
# ===========================================================================


def configure(
    *,
    fuzzy_threshold: float | None = None,
    max_sequence_length: int | None = None,
) -> None:
    """Override package-level configuration defaults.

    Parameters
    ----------
    fuzzy_threshold : float or None
        Minimum similarity score (0-1) for drug type fuzzy matching.
        None means use the default (0.7).
    max_sequence_length : int or None
        Maximum protein sequence length before truncation.
        None means use the default (10000).

    Raises
    ------
    ValueError
        If configuration values are out of valid range.
    """
    if fuzzy_threshold is not None:
        if not (0.0 <= fuzzy_threshold <= 1.0):
            raise ValueError(
                f"fuzzy_threshold must be in [0, 1], "
                f"got {fuzzy_threshold}"
            )
        normalizer_mod = importlib.import_module(".normalizer", __name__)
        normalizer_mod._FUZZY_THRESHOLD = fuzzy_threshold
        _logger.info(
            "Configured fuzzy_threshold=%.4f", fuzzy_threshold
        )

    if max_sequence_length is not None:
        if max_sequence_length < 1:
            raise ValueError(
                f"max_sequence_length must be >= 1, "
                f"got {max_sequence_length}"
            )
        missing_vals_mod = importlib.import_module(
            ".missing_values", __name__
        )
        missing_vals_mod._MAX_SEQUENCE_LENGTH = max_sequence_length
        _logger.info(
            "Configured max_sequence_length=%d", max_sequence_length
        )

    # [v2.1.0 ARCH-4 IDEM-9] Refresh optional-dependency capability flags
    # after a configure() call so hot-installed deps are picked up.
    try:
        normalizer_mod = importlib.import_module(".normalizer", __name__)
        if hasattr(normalizer_mod, "refresh_capabilities"):
            normalizer_mod.refresh_capabilities()
    except Exception as exc:
        _logger.debug(
            "configure: refresh_capabilities failed: %s", exc
        )


# ===========================================================================
# Impact analysis (GAP-DL4)
# ===========================================================================


def get_affected_functions(column_name: str) -> list[str]:
    """Return cleaning functions that depend on a given column.

    Useful for impact analysis when source data changes.

    Parameters
    ----------
    column_name : str
        The column name to look up.

    Returns
    -------
    list[str]
        List of cleaning function names that use this column.
    """
    return _CLEANING_DEPENDENCY_GRAPH.get(column_name, [])


# ===========================================================================
# Metrics (GAP-L3)
# ===========================================================================


def get_load_times() -> dict[str, float]:
    """Return import times for lazy-loaded names.

    Returns
    -------
    dict[str, float]
        Mapping from name to load time in seconds.
    """
    return dict(_MODULE_LOAD_TIMES)


def get_metrics() -> dict:
    """Return package-level metrics for observability.

    Returns
    -------
    dict
        Package metrics including version, health, load times,
        dead-letter count, and registry size.
    """
    return {
        "version": __version__,
        "health": check_health(),
        "load_times": get_load_times(),
        "dead_letter_count": len(_dead_letters),
        "registry_size": len(_CLEANING_REGISTRY),
    }


# ===========================================================================
# Deprecation helper (GAP-CO6)
# ===========================================================================


def _deprecated(
    name: str,
    replacement: str | None = None,
    removal_version: str = "3.0.0",
) -> None:
    """Mark a name as deprecated and issue a warning when accessed.

    Parameters
    ----------
    name : str
        The deprecated name.
    replacement : str or None
        The replacement name, if any.
    removal_version : str
        Version in which the name will be removed.
    """
    msg = (
        f"cleaning.{name} is deprecated and will be removed in "
        f"v{removal_version}."
    )
    if replacement:
        msg += f" Use cleaning.{replacement} instead."
    warnings.warn(msg, DeprecationWarning, stacklevel=3)


# ===========================================================================
# Public API declaration (BUG-D1, BUG-C1)
# ===========================================================================
# v29 ROOT FIX (audit C-12): 2377 lines of re-exports — significant
# import-time cost. Future cleanup: split into sub-packages. For now,
# documented the bloat. The actual sub-module imports are deferred via
# the ``_LAZY_IMPORTS`` dict + module-level ``__getattr__`` above so
# that ``import cleaning`` does NOT eagerly load pandas/numpy/rdkit.

__all__ = [
    # normalizer — original + new public API (alphabetical, case-sensitive)
    "ALLOWED_TYPES",
    "ActivityValue",
    "ConversionResult",
    "FUZZY_THRESHOLD",
    "RECORD_SCHEMA",
    "STEREO_POLICY",
    "UNIT_CONVERSIONS",
    "WITHDRAWN_GROUP_KEYWORDS",
    "configure_normalizer",
    "convert_to_inchikey",
    "convert_to_inchikey_detailed",
    "convert_to_inchikeys",
    "fuzzy_match_drug_type",
    "fuzzy_match_drug_types",
    "get_cache_info",
    "get_dq_counts",
    "get_validation_status",
    "is_backfill_needed",
    "is_synthetic_inchikey",
    "is_valid_inchikey",
    "load_config",
    "normalize_activity_value",
    "normalize_activity_values",
    "normalize_inchikey",
    "refresh_capabilities",
    "requires_api_version",
    "reset_dq_counts",
    "save_config",
    "sign_output",
    "standardize_drug_record",
    "standardize_drug_records_batch",
    "standardize_drug_records_chunked",
    "standardize_inchikey",
    "validate_config",
    "validate_inchikey",
    # deduplicator — alphabetical within section (v3.0.0)
    "ActivityDirection",
    "CompletenessWeight",
    "DEFAULT_COMPLETENESS_WEIGHTS",
    "DEFAULT_DPI_KEYS",
    "DedupResult",
    "DedupStrategy",
    "INVERSE_ACTIVITY_TYPES",
    "MAX_DATAFRAME_ROWS",
    "MAX_DEAD_LETTERS",
    "MAX_DROPPED_ROWS_IN_RESULT",
    "PERCENT_ACTIVITY_TYPES",
    "POTENCY_ACTIVITY_TYPES",
    "backfill_safety_check",
    "checkpoint_state",
    "clean_interactions",
    "clear_dead_letters",
    "compute_completeness_score",
    "configure_deduplicator",
    "dedup_by_inchikey",
    "dedup_by_inchikey_chunked",
    "dedup_interactions",
    "flush_dead_letters",
    "get_correlation_id",
    "get_dead_letters",
    "get_metrics",
    "get_provenance",
    "health_check",
    "is_reproducible",
    "merge_duplicate_groups",
    "performance_benchmark",
    "quality_report",
    "recover_from_failure",
    "referential_integrity_check",
    "reproducibility_report",
    "requires_api_version",
    "reset_metrics",
    "revert_configuration",
    "set_correlation_id",
    "timing_report",
    "validate_config",
    "validate_environment",
    "validate_recovery_state",
    # missing_values — alphabetical within section
    "MAX_SEQUENCE_LENGTH",
    "fill_missing_drug_fields",
    "handle_missing_inchikey",
    "handle_missing_protein_fields",
    "validate_gda_scores",
    # confidence — institutional-grade confidence-tier classifier (ARCH-7)
    "DEFAULT_CONFIDENCE_TIERS",
    "CONFIDENCE_TIER_METHOD_VERSION",
    "classify_confidence",
]
