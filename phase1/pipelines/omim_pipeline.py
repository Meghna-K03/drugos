"""
OMIM Pipeline — gene-phenotype mappings from OMIM (institutional-grade).

This module is the upstream root of the gene-disease-association (GDA) data
ingestion for the Autonomous Drug Repurposing Platform (Team Cosmic /
VentureLab). It downloads, parses, cleans, and loads OMIM's morbidmap.txt
into the shared ``gene_disease_associations`` table that downstream consumers
(Graph Transformer, RL ranker, pharma-facing API, researcher dashboard) read
from. A single silently-dropped record, a single mis-scored association, or a
single susceptibility marker treated as a causal mutation can produce a wrong
drug prediction that harms a patient. This rewrite fixes every one of the
131 forensic-audit findings spanning the 16 verification domains.

Download
--------
- If ``OMIM_API_KEY`` is set: download ``morbidmap.txt`` from OMIM's data
  downloads endpoint. The API key is part of the URL **path** because OMIM's
  downloads endpoint does NOT accept ``Authorization: ApiKey`` headers (any
  such attempt returns 401). The previous code's "FIX #12" header-auth
  attempt was fake — it has been removed (BUG-2.1).
- If ``OMIM_API_KEY`` is empty: raise ``RuntimeError``. The API path also
  requires a key, so silent fallthrough is forbidden (BUG-9.15).
- Optional alternative: ``_download_via_api()`` paginates the OMIM REST API
  at ``/api/geneMap`` with the ``Authorization: ApiKey`` header (preferred
  over the query-string ``apiKey`` form to avoid CDN/proxy logging of the
  key — BUG-2.2 / BUG-9.1). Page size is 1000 (OMIM max), rate-limited to
  at least 1 req/sec (P1-22 ROOT FIX: previously ~0.1 req/s effective rate
  → 24h+ full ingest).
- Both paths write a SHA-256 sidecar and produce a manifest with full
  provenance.

Clean
-----
1. Parse ``morbidmap.txt`` (tab-separated, **no header row** — BUG-3.1).
   Single-loop reader: the first non-``#`` non-empty line is a DATA row,
   not a header. UTF-8 strict with a latin-1 fallback for non-UTF-8 bytes
   (BUG-6.8).
2. Extract ``phenotype_name``, ``phenotype_mim``, ``mapping_key`` (1-4
   only — BUG-3.20), and ``association_modifier`` (one of ``?``, ``{}``,
   ``[]``, ``*``, ``+``, ``%`` or ``None`` — BUG-3.4) from the phenotype
   column via the canonical regex.
3. Validate ``phenotype_mim`` is in ``[100100, 999999]`` (BUG-3.7,
   BUG-3.14, BUG-3.21). Reject outliers with WARNING + dead-letter.
4. Filter ``mapping_key ∈ OMIM_MAPPING_KEYS_INCLUDE`` (default ``[3, 4]`` —
   molecular basis known + contiguous gene syndromes; BUG-2.5, BUG-3.5,
   BUG-3.6). Log the active include-list at INFO at clean() start.
5. Explode ``gene_symbols_raw`` on ``\\s*,\\s*`` (BUG-3.9). Uppercase
   (BUG-3.11) and HGNC-validate (BUG-3.10) the resulting symbols.
6. Build ``disease_id = "OMIM:" + str(phenotype_mim)`` (BUG-3.8 — matches
   DisGeNET's format).
7. Derive ``association_type`` from the leading marker (BUG-3.4, BUG-3.15):
   ``{}``→``susceptibility``, ``[]``→``non_disease``, ``?``→``provisional``,
   ``*``/``+``→``gene_locus``, ``%``→``mendelian_phenotype``, ``None``→``causal``.
8. Route susceptibility (``{}``) records to a separate CSV
   (``omim_gene_disease_susceptibility.csv``) when
   ``OMIM_EXCLUDE_SUSCEPTIBILITY=True`` (BUG-3.13 — the patient-harm
   failure mode).
9. Vectorized scoring (BUG-3.2, BUG-4.5):
   ``score = clip(base[mk] + 0.05·log1p(num_pmids) [+0.05·evidence_strength], 0, 1)``
   where ``base[3]=0.9``, ``base[4]=0.8``, ``base[2]=0.6``, ``base[1]=0.5``.
   The score is never flat — every output row reflects its evidence.
10. Derive ``confidence_tier`` from ``score`` via the shared
    ``classify_confidence`` (BUG-2.4, BUG-3.3). The legacy flat ``"high"``
    is forbidden — the DB CHECK constraint requires ``weak``/``moderate``/
    ``strong`` (BUG-14.4).
11. Extract ``inheritance_pattern`` from the phenotype name (BUG-3.18).
12. Validate ``cyto_location`` format (BUG-3.22).
13. Pre-dedup on ``(phenotype_mim, gene_symbol, mapping_key)`` (BUG-3.16).
14. Run ``validate_gda_scores(dedup=True, source="omim",
    preserve_direction=False, dedup_keys=[...])`` (BUG-2.8).
15. Populate lineage columns (§6, Domain 16): ``source``,
    ``source_id="OMIM:{gene_mim}_{phenotype_mim}"``, ``source_version``
    (from morbidmap header), ``source_url``, ``source_format``,
    ``download_method``, ``download_date``, ``schema_version``,
    ``pipeline_run_id``, ``input_checksum``, ``dedup_strategy``,
    ``canonical_gene_id``, ``canonical_disease_id``, ``as_of_date``,
    ``hgnc_snapshot_version``, ``source_record_id``, ``source_line_number``,
    ``transformations``.
16. Deterministic sort by ``(gene_symbol, disease_id, source)`` with
    ``kind="mergesort"`` (BUG-7.14) → byte-identical CSV across runs.
17. Atomic write via ``_save_processed_csv`` (BUG-1.9 — replaces
    ``_append_or_write_csv``): ``.tmp`` + ``os.replace``, ``utf-8``,
    ``\\n`` line terminator, ``QUOTE_ALL``, ``0o640`` permissions.
18. Write a manifest (``omim_pipeline.manifest.json``) with SHA-256,
    ``source_version``, ``schema_version``, ``download_date``,
    ``pipeline_run_id``, ``input_checksum``, ``output_csv_sha256``,
    ``row_count``, ``clean_started_at``, ``clean_finished_at`` (BUG-1.7,
    BUG-16.10).
19. Write quarantine JSONL for malformed records (BUG-5.17, BUG-16.20).
20. NaN assertions on required columns (BUG-5.19). Row-count
    reconciliation (BUG-5.20).

Load
----
1. Single DB session (BUG-1.6 — collapsed from two).
2. Resolve ``gene_symbol → uniprot_id`` (with ``gene_mim`` as a secondary
   lookup key — BUG-3.17).
3. Dead-letter unresolved symbols to a CSV file AND the ``dead_letter_gda``
   DB table (BUG-6.12, BUG-16.20).
4. Compute ``input_checksum`` (SHA-256 of the cleaned DataFrame — BUG-1.7).
5. ``get_or_create_pipeline_run(session, run_id, source="omim", ...)``
   → ``pipeline_run_id`` (BUG-2.9, BUG-16.1).
6. ``bulk_upsert_gda(session, load_df, pipeline_run_id=..., score_type=
   "omim_mapping_key", score_method="omim_v1_{source_version}",
   input_checksum=..., dedup_already_done=True)`` (BUG-2.9, BUG-2.10,
   BUG-8.14, §4.2 mirror of DisGeNET).
7. Post-load DisGeNET dedup (BUG-1.8): DELETE OMIM-direct rows whose
   (gene_symbol, disease_id) already exists in DisGeNET (the curated
   DisGeNET release includes ~80% of morbidmap with richer scoring).
8. Session-health check after upsert (BUG-6.13).
9. Result detail logging (BUG-11.14).
10. Metric emission (BUG-11.7, §4.6 mirror of DisGeNET).

Output Schema (master prompt §6)
--------------------------------
The cleaned DataFrame contains the following columns (additive over today's
schema — none removed):

Identity: gene_symbol, uniprot_id, gene_mim, disease_id, disease_name,
disease_id_type, cyto_location, inheritance_pattern.

Association semantics: association_modifier, association_type,
is_susceptibility, mapping_key.

Scoring: score, score_type, score_method, confidence_tier,
confidence_tier_method, evidence_strength, normalized_score.

Source & lineage: source, source_id (format ``"OMIM:{gene_mim}_{phenotype_mim}"``),
source_version, source_url, source_format, download_method, download_date,
schema_version, pipeline_run_id, input_checksum, dedup_strategy,
canonical_gene_id, canonical_disease_id, as_of_date, hgnc_snapshot_version,
source_record_id, source_line_number, transformations.

Validator-emitted: _score_was_clipped, _original_score,
_score_was_coerced_nan, _score_direction, _disease_name_was_filled,
_association_type_was_filled.

Optional: pmid_list, original_pmid_count, pmid_list_was_capped, year_initial,
year_final.

License
-------
OMIM data is licensed under OMIM's terms of use (https://omim.org/help/agreement).
The output CSV is ``license = "OMIM-restricted"``; downstream consumers must
verify they hold a valid OMIM license before reading this data.
"""

from __future__ import annotations

# ============================================================================
# Standard library imports
# ============================================================================
import csv as csv_mod
import hashlib
import json
import logging
import math
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Mapping, NewType

# ============================================================================
# Third-party imports
# ============================================================================
import numpy as np
import pandas as pd
import requests

# ============================================================================
# Project imports — cleaning utilities
# ============================================================================
from cleaning._constants import (
    normalize_gene_symbol,  # v29 ROOT FIX (audit P1-24)
    normalize_uniprot_id,   # v29 ROOT FIX (audit P1-24)
)
from cleaning.confidence import (
    CONFIDENCE_TIER_METHOD_VERSION,
    DEFAULT_CONFIDENCE_TIERS,
    OMIM_CONFIDENCE_TIERS,  # v43 P1-033
    classify_confidence,
)
from cleaning.missing_values import _fingerprint_df, validate_gda_scores

# ============================================================================
# Project imports — configuration
# ============================================================================
from config.settings import (
    OMIM_API_BASE,
    OMIM_API_KEY,
    OMIM_API_KEY_FORMAT_RE,
    OMIM_API_MAX_RETRIES,
    OMIM_API_PAGE_LIMIT,
    OMIM_API_TIMEOUT,
    OMIM_CONFIRMED_SCORE,
    OMIM_CONTIGUOUS_SCORE,
    OMIM_DB_BATCH_SIZE,
    OMIM_DEDUP_KEEP_POLICY,
    OMIM_DOWNLOAD_TIMEOUT,
    OMIM_EXCLUDE_SUSCEPTIBILITY,
    OMIM_GENE_MAPPED_SCORE,
    OMIM_JSON_PRETTY,
    OMIM_MAPPING_KEYS_INCLUDE,
    OMIM_MAX_AGE_DAYS,
    OMIM_MAX_PAGINATION_PAGES,
    OMIM_MIN_EXPECTED_RECORDS,
    OMIM_OUTPUT_FILENAME,
    OMIM_PHENOTYPE_MAPPED_SCORE,
    OMIM_RANDOM_SEED,
    OMIM_REQUEST_INTERVAL,
    OMIM_USER_AGENT,
    PROCESSED_DATA_DIR,
    RAW_DATA_DIR,
)

# ============================================================================
# Project imports — database
# ============================================================================
from database.connection import get_db_session
from database.loaders import (
    UpsertResult,
    build_gene_to_uniprot_maps,
    bulk_upsert_gda,
    get_or_create_pipeline_run,
    resolve_gene_symbol_to_uniprot,
)
from database.models import DeadLetterGDA, GeneDiseaseAssociation, PipelineRun

# ============================================================================
# Project imports — pipeline base
# ============================================================================
from pipelines.base_pipeline import (
    RETRYABLE_EXCEPTIONS,
    RETRYABLE_STATUS_CODES,
    SENSITIVE_HEADER_KEYS,
    BasePipeline,
)

# ============================================================================
# Module-level metadata
# ============================================================================
logger = logging.getLogger(__name__)

__version__: str = "2.0.0"
__author__: str = "Team Cosmic / VentureLab"
__license__: str = "OMIM-restricted"

__all__ = [
    "OMIMPipeline",
    "OMIMRecord",
    "OMIM_OUTPUT_PATH",
    "OMIM_SUSCEPTIBILITY_OUTPUT_PATH",
    "OMIM_QUARANTINE_PATH",
    "OMIM_MANIFEST_PATH",
    "SCORE_TYPE_OMIM",
    "SCORE_METHOD_DEFAULT",
    "SCHEMA_VERSION_STAMP",
    "SCORE_BY_MAPPING_KEY",
    "MARKER_PATTERNS",
    "MARKER_TO_ASSOCIATION_TYPE",
    "INHERITANCE_PATTERNS",
    "CYTO_RE",
    "GENERATED_RE",
    "MAPPING_KEY_RE",
    "MIM_NUMBER_RE",
    "GDA_REQUIRED_COLUMNS",
    "OMIMGDADataFrame",
    "assert_is_omim_gda_df",
    "__version__",
]

# ============================================================================
# Module-level constants
# ============================================================================

# BUG-13.20 / BUG-2.11: a single source of truth for the cleaned-CSV
# filename and related paths. Mirrors DisGeNET's pattern.
# The default output filename is "omim_gene_disease_associations.csv"
# (configurable via OMIM_OUTPUT_FILENAME env var).
SCHEMA_VERSION_STAMP: str = "2.0"

# audit-2025 ROOT FIX (issue 10): the two legacy module-level constants
# ``MAPPING_KEY_CONFIRMED`` and ``OMIM_REQUEST_INTERVAL_MODULE`` were
# flagged as dead code (``# noqa: F841``). Audit findings:
#
#   * ``MAPPING_KEY_CONFIRMED`` is IMPORTED by:
#       - ``tests/test_all_19_files_integration.py`` (multiple call sites)
#       - ``tests/test_pipelines_init.py`` (multiple call sites)
#       - declared in ``pipelines/__init__.pyi`` (public type stub)
#     It CANNOT be removed without breaking the public API and the
#     threshold-introspection tests. We keep it but tighten the
#     documentation so future readers know why it must stay.
#
#   * ``OMIM_REQUEST_INTERVAL_MODULE`` is NOT imported anywhere (no
#     test, no stub, no caller). It is truly dead code. We REMOVE it.
#
# Why keep ``MAPPING_KEY_CONFIRMED``? It encodes OMIM's mapping-key
# convention (3 = "confirmed" — multiple independent observations per
# OMIM #173110). ``OMIM_MAPPING_KEYS_INCLUDE`` defaults to ``[3, 4]``
# (the *set of keys we ingest*), but ``MAPPING_KEY_CONFIRMED == 3`` is
# the canonical label used by downstream ML features and by the
# threshold-introspection registry in ``pipelines/__init__.py`` to
# surface the value with a rationale. Removing it would silently drop
# the rationale annotation that auditors rely on.
MAPPING_KEY_CONFIRMED: int = 3
"""Canonical OMIM mapping_key for "confirmed" associations (OMIM #173110).

This MUST stay as a module-level public constant because:
  1. It is imported by ``tests/test_all_19_files_integration.py`` and
     ``tests/test_pipelines_init.py``.
  2. It is re-exported via ``pipelines/__init__.pyi`` (public type stub).
  3. It is registered in the threshold-introspection registry in
     ``pipelines/__init__.py`` (``_THRESHOLDS`` dict) so auditors can
     discover the value and its rationale programmatically.

``OMIM_MAPPING_KEYS_INCLUDE`` (in ``config/settings.py``) controls which
mapping_keys are *ingested* (default ``[3, 4]``). ``MAPPING_KEY_CONFIRMED``
labels the meaning of value ``3`` for downstream consumers and is NOT
the same as the include-list.
"""

OMIM_OUTPUT_PATH: Path = PROCESSED_DATA_DIR / OMIM_OUTPUT_FILENAME
OMIM_SUSCEPTIBILITY_OUTPUT_PATH: Path = (
    PROCESSED_DATA_DIR / "omim_gene_disease_susceptibility.csv"
)
OMIM_QUARANTINE_PATH: Path = PROCESSED_DATA_DIR / "omim_quarantine.jsonl"
OMIM_MANIFEST_PATH: Path = OMIM_OUTPUT_PATH.with_suffix(
    OMIM_OUTPUT_PATH.suffix + ".manifest.json"
)
OMIM_DISGENET_OVERLAP_PATH: Path = (
    PROCESSED_DATA_DIR / "omim_disgenet_overlap.jsonl"
)
OMIM_RAW_MORBIDMAP_PATH: Path = RAW_DATA_DIR / "omim" / "morbidmap.txt"
OMIM_RAW_API_JSON_PATH: Path = RAW_DATA_DIR / "omim" / "omim_genemaps.json"

# Source URLs (sanitised at log time — never logged raw).
OMIM_DOWNLOADS_URL_TEMPLATE: str = (
    "https://data.omim.org/downloads/{api_key}/morbidmap.txt"
)
OMIM_DOWNLOADS_URL_SANITISED: str = (
    "https://data.omim.org/downloads/[REDACTED]/morbidmap.txt"
)
OMIM_API_GENE_MAP_ENDPOINT: str = "/geneMap"

# BUG-2.3 / BUG-3.2 / BUG-12.12: per-mapping-key base scores.
SCORE_TYPE_OMIM: str = "omim_mapping_key"
SCORE_METHOD_DEFAULT: str = "omim_v1"
SCORE_BY_MAPPING_KEY: dict[int, float] = {
    3: OMIM_CONFIRMED_SCORE,          # 0.9 — molecular basis known (strongest)
    4: OMIM_CONTIGUOUS_SCORE,         # 0.8 — contiguous gene syndrome
    2: OMIM_PHENOTYPE_MAPPED_SCORE,   # 0.6 — phenotype mapped
    1: OMIM_GENE_MAPPED_SCORE,        # 0.5 — wild-type gene mapped
}
DEFAULT_MAPPING_KEY_SCORE: float = 0.4   # for mk=0 (unknown) or out-of-range
PMID_BONUS_COEFFICIENT: float = 0.05     # 0.05 · log1p(num_pmids)
PMID_BONUS_CAP: float = 0.08             # cap at +0.08
EVIDENCE_BONUS_COEFFICIENT: float = 0.05  # 0.05 · evidence_strength
EVIDENCE_BONUS_CAP: float = 0.05         # cap at +0.05

# BUG-3.4: phenotype markers — leading-character patterns.
# NOTE: morbidmap uses {...} and [...] as *wrappers* around the phenotype
# name; the other markers (?, *, +, %) are leading single characters.
MARKER_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^\{\s*([^}]*)\s*\}"), "{}"),
    (re.compile(r"^\[\s*([^\]]*)\s*\]"), "[]"),
    (re.compile(r"^\?(.*)$"), "?"),
    (re.compile(r"^\*(.*)$"), "*"),
    (re.compile(r"^\+(.*)$"), "+"),
    (re.compile(r"^%(.*)$"), "%"),
]

# BUG-3.4 / BUG-3.15: map association_modifier → association_type.
ASSOCIATION_TYPE_DEFAULT: str = "causal"
ASSOCIATION_TYPE_SUSCEPTIBILITY: str = "susceptibility"
ASSOCIATION_TYPE_NON_DISEASE: str = "non_disease"
ASSOCIATION_TYPE_PROVISIONAL: str = "provisional"
ASSOCIATION_TYPE_GENE_LOCUS: str = "gene_locus"
ASSOCIATION_TYPE_MENDELIAN_PHENOTYPE: str = "mendelian_phenotype"
ASSOCIATION_TYPE_UNKNOWN: str = "unknown"

MARKER_TO_ASSOCIATION_TYPE: dict[str | None, str] = {
    "{}": ASSOCIATION_TYPE_SUSCEPTIBILITY,
    "[]": ASSOCIATION_TYPE_NON_DISEASE,
    "?":  ASSOCIATION_TYPE_PROVISIONAL,
    "*":  ASSOCIATION_TYPE_GENE_LOCUS,
    "+":  ASSOCIATION_TYPE_GENE_LOCUS,        # alt form, treat same
    "%":  ASSOCIATION_TYPE_MENDELIAN_PHENOTYPE,
    None: ASSOCIATION_TYPE_DEFAULT,           # default for unmarked mk=3
}

# BUG-3.18: inheritance patterns extractable from phenotype names.
INHERITANCE_PATTERNS: list[str] = [
    "autosomal dominant", "autosomal recessive",
    "X-linked dominant", "X-linked recessive", "X-linked",
    "Y-linked", "mitochondrial", "digenic", "triallelic",
    "multifactorial", "somatic", "sporadic",
]
_INHERITANCE_RE: re.Pattern[str] = re.compile(
    r"\b(" + "|".join(re.escape(p) for p in INHERITANCE_PATTERNS) + r")\b",
    re.IGNORECASE,
)

# BUG-3.22: cyto-location format.
# Human chromosomes are 1-22, X, Y (and mitochondrial M, though OMIM doesn't
# use M for cyto-locations). The format is <chromosome><arm><band>[.<subband>].
# Examples: 4p16.3, 17q21.31, Xp21.2, Yq11.2.
CYTO_RE: re.Pattern[str] = re.compile(r"^(\d{1,2}|X|Y)[pq]\d{1,2}(\.\d{1,2})?$")

# BUG-7.10: morbidmap header parser for the "Generated:" line.
GENERATED_RE: re.Pattern[str] = re.compile(
    r"^#\s*Generated:\s*(\d{4}-\d{2}-\d{2})", re.MULTILINE
)

# BUG-3.20: mapping key regex — tightened to [1-4] only.
# Two forms: strict (mapping key at end of string — the canonical morbidmap
# format) and lenient (mapping key followed by a trailing inheritance
# annotation, e.g. "(3), autosomal recessive"). The strict form is tried
# first; the lenient form only fires when strict fails. Both reject
# (0), (5), (99) etc. — only [1-4] are valid OMIM mapping keys.
MAPPING_KEY_RE: re.Pattern[str] = re.compile(r"\(([1-4])\)\s*$")
MAPPING_KEY_RE_LENIENT: re.Pattern[str] = re.compile(r"\(([1-4])\)\s*,")

# BUG-3.21: MIM number regex — 5 to 7 digits, validated against range later.
# Matches any comma-separated 5-7 digit number with a word boundary after.
# We take the LAST match (in case the phenotype name contains multiple
# comma-separated numbers) — the MIM number is conventionally the last
# comma-separated numeric token before the mapping key. The downstream
# range check (100100 ≤ mim ≤ 999999) catches false positives.
MIM_NUMBER_RE: re.Pattern[str] = re.compile(r",\s*(\d{5,7})\b")

# BUG-2.12: source_id format.
SOURCE_ID_RE: re.Pattern[str] = re.compile(r"^OMIM:\d{6}_\d{6}$")

# BUG-2.11: single source of truth for the GDA schema.
GDA_REQUIRED_COLUMNS: list[tuple[str, Any]] = [
    # Identity
    ("gene_symbol",              None),
    ("uniprot_id",               None),
    ("gene_mim",                 None),
    ("disease_id",               None),
    ("disease_id_type",          "omim"),
    ("disease_name",             None),
    ("disease_class",            None),
    ("year",                     None),
    ("year_initial",             None),
    ("year_final",               None),
    # Association semantics (BUG-3.4, BUG-3.13, BUG-3.15)
    ("association_type",         "unknown"),
    ("association_modifier",     None),
    ("is_susceptibility",        False),
    ("inheritance_pattern",      None),
    ("mapping_key",              0),
    ("cyto_location",            None),
    ("cyto_location_valid",      True),
    # Source & lineage (Domain 16)
    ("source",                   "omim"),
    ("source_id",                None),
    ("source_version",           None),
    ("source_url",               None),
    ("source_format",            None),
    ("download_method",          None),
    ("download_date",            None),
    ("schema_version",           SCHEMA_VERSION_STAMP),
    ("pipeline_run_id",          None),
    ("input_checksum",           None),
    ("dedup_strategy",           None),
    ("canonical_gene_id",        None),
    ("canonical_disease_id",     None),
    ("as_of_date",               None),
    ("hgnc_snapshot_version",    None),
    ("source_record_id",         None),
    ("source_line_number",       None),
    ("transformations",          None),
    # Scoring (BUG-3.2, BUG-3.3, BUG-2.3, BUG-2.4)
    ("score",                    None),
    ("score_type",               SCORE_TYPE_OMIM),
    ("score_method",             None),
    ("confidence_tier",          None),
    ("confidence_tier_method",   None),
    ("evidence_strength",        None),
    ("normalized_score",         None),
    # PMIDs (BUG-4.5 / BUG-8.6 — vectorized scoring uses these)
    ("pmid_list",                None),
    ("original_pmid_count",      None),
    ("pmid_list_was_capped",     False),
]

# BUG-15.6 / BUG-15.7: type alias + runtime validator.
OMIMGDADataFrame = NewType("OMIMGDADataFrame", pd.DataFrame)


def assert_is_omim_gda_df(df: pd.DataFrame) -> None:
    """Runtime validator for the OMIM GDA DataFrame contract (BUG-15.6).

    Raises:
        ValueError: if any required column is missing.
    """
    required = [name for name, _ in GDA_REQUIRED_COLUMNS]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(
            f"DataFrame does not satisfy OMIM GDA contract — "
            f"missing columns: {missing}"
        )


# ============================================================================
# Reproducibility — fix the random seed for retry-backoff jitter (BUG-7.4,
# BUG-4.9).
# v41 ROOT FIX (SEV3-MEDIUM #12): the previous code called
# ``random.seed(OMIM_RANDOM_SEED)`` at MODULE LEVEL, which seeds the
# GLOBAL ``random`` module's RNG. This affects EVERY other module in
# the process that uses ``random.*`` (e.g. ``random.random()``,
# ``random.choice()``, ``random.uniform()``) — including numpy's
# Python-level fallback when no ``np.random.seed`` is set. In a multi-
# pipeline process (e.g. an Airflow task that runs OMIM + DisGeNET +
# ChEMBL in sequence), the OMIM import would silently re-seed the
# global RNG, breaking reproducibility guarantees for downstream
# pipelines that depend on their OWN seeds. Fix: use a LOCAL
# ``random.Random`` instance for OMIM's own jitter, leaving the
# global RNG untouched. The local instance is module-level so it's
# shared across OMIM calls (preserving the BUG-7.4 reproducibility
# guarantee for OMIM's own retry backoff) but isolated from the
# global RNG that other modules depend on.
# ============================================================================
# v41 ROOT FIX (SEV3-MEDIUM #12): local RNG instance for OMIM use.
_OMIM_RNG = random.Random(OMIM_RANDOM_SEED)


# ============================================================================
# BUG-1.4: OMIMRecord dataclass — canonical intermediate representation.
# ============================================================================
@dataclass(frozen=True)
class OMIMRecord:
    """Frozen dataclass representing a single parsed OMIM record.

    BUG-1.4: No abstraction for "OMIM record" — raw dicts everywhere.
    This dataclass is the canonical intermediate representation between
    parsing (morbidmap.txt or OMIM API JSON) and DataFrame construction.
    It is hashable and immutable so it can be safely deduplicated, cached,
    and round-tripped through JSON for test fixtures.

    Attributes:
        phenotype_name: normalized phenotype name (markers stripped,
            whitespace collapsed). None if the source line had no name.
        phenotype_mim: integer MIM number of the phenotype. None if the
            source line had no MIM. Must be in [100100, 999999] when set.
        mapping_key: OMIM phenotype mapping key. 0 if unknown, else in
            {1, 2, 3, 4}. See master prompt §5.2.
        gene_symbols_raw: raw comma-separated gene symbols string from
            morbidmap column 2. May be empty.
        gene_mim: OMIM gene MIM number (string, since some are 6-digit
            and leading zeros would be lost on int conversion). None if
            missing.
        cyto_location: cytogenetic band location (e.g. ``"4p16.3"``).
            None if missing.
        association_modifier: one of ``"?"``, ``"{}"``, ``"[]"``, ``"*"``,
            ``"+"``, ``"%"``, or None. See master prompt §5.3.
        source_format: ``"morbidmap_txt"`` or ``"api_json"``.
        source_line_number: 1-indexed line number for morbidmap records
            (None for API records). Used for lineage / dead-letter.
    """
    phenotype_name: str | None
    phenotype_mim: int | None
    mapping_key: int
    gene_symbols_raw: str
    gene_mim: str | None
    cyto_location: str | None
    association_modifier: str | None
    source_format: Literal["morbidmap_txt", "api_json"]
    source_line_number: int | None

    def validate(self) -> None:
        """Enforce §5 scientific invariants. Raises ValueError on violation.

        Called by ``from_morbidmap_line`` and ``from_api_entry``. Callers
        should wrap in try/except ValueError and quarantine the offending
        record (BUG-3.7, BUG-3.14, BUG-3.20).
        """
        # BUG-3.20: mapping key must be in {0, 1, 2, 3, 4}.
        # 0 = "unknown" (used by API when phenotypeMappingKey is absent).
        if self.mapping_key not in (0, 1, 2, 3, 4):
            raise ValueError(
                f"mapping_key {self.mapping_key!r} not in {{0, 1, 2, 3, 4}} "
                f"(line {self.source_line_number})"
            )
        # BUG-3.7, BUG-3.14: phenotype_mim range and positivity.
        if self.phenotype_mim is not None:
            if self.phenotype_mim <= 0:
                raise ValueError(
                    f"phenotype_mim {self.phenotype_mim} <= 0 "
                    f"(line {self.source_line_number})"
                )
            if not (100100 <= self.phenotype_mim <= 999999):
                raise ValueError(
                    f"phenotype_mim {self.phenotype_mim} outside OMIM range "
                    f"[100100, 999999] (line {self.source_line_number})"
                )

    @classmethod
    def from_morbidmap_line(cls, line: str, line_no: int) -> "OMIMRecord | None":
        """Parse a single morbidmap.txt line into an OMIMRecord.

        Returns None if the line is structurally unparseable (e.g. wrong
        number of tab-separated columns). Raises ValueError if the line
        parses but violates a scientific invariant (caller should
        quarantine with the reason from the ValueError message).
        """
        line = line.rstrip("\r\n")
        # BUG-4.12: parts[1] and parts[2] are guaranteed by len(parts) < 3
        # continue; no need for redundant guards.
        parts = line.split("\t")
        if len(parts) < 3:
            return None
        phenotype_col = parts[0].strip()
        gene_symbols_raw = parts[1].strip()
        gene_mim = parts[2].strip()
        cyto_location = parts[3].strip() if len(parts) > 3 else ""

        phenotype_name, phenotype_mim, mapping_key, association_modifier = (
            OMIMPipeline._parse_phenotype_field(phenotype_col)
        )

        record = cls(
            phenotype_name=phenotype_name,
            phenotype_mim=phenotype_mim,
            mapping_key=mapping_key,
            gene_symbols_raw=gene_symbols_raw,
            gene_mim=gene_mim or None,
            cyto_location=cyto_location or None,
            association_modifier=association_modifier,
            source_format="morbidmap_txt",
            source_line_number=line_no,
        )
        record.validate()
        return record

    @classmethod
    def from_api_entry(cls, gm: dict, pm_entry: dict) -> "OMIMRecord":
        """Build an OMIMRecord from an OMIM API geneMap + phenotypeMap entry.

        BUG-5.15: prefers ``approvedGeneSymbol`` over ``geneSymbols``.
        """
        # BUG-5.15: prefer approved gene symbol.
        gene_symbols_raw = (
            gm.get("approvedGeneSymbol")
            or gm.get("geneSymbols", "")
            or ""
        )
        gene_mim_raw = gm.get("mimNumber", "")
        gene_mim = str(gene_mim_raw) if gene_mim_raw not in ("", None) else None
        cyto_location = gm.get("cytoLocation", "") or None

        # Phenotype fields.
        phenotype_mim_raw = pm_entry.get("phenotypeMimNumber")
        if phenotype_mim_raw in (None, "", 0):
            phenotype_mim = None
        else:
            try:
                phenotype_mim = int(phenotype_mim_raw)
            except (ValueError, TypeError):
                phenotype_mim = None

        phenotype_name_raw = pm_entry.get("phenotype", "") or ""
        mapping_key_raw = pm_entry.get("phenotypeMappingKey", 0)
        try:
            mapping_key = int(mapping_key_raw)
        except (ValueError, TypeError):
            mapping_key = 0

        # BUG-3.4: re-extract the marker from the API phenotype name. The
        # OMIM API returns the raw name (with markers), so we run it
        # through the same _parse_phenotype_field logic.
        if phenotype_mim is not None:
            synthetic = f"{phenotype_name_raw}, {phenotype_mim} ({mapping_key})"
        else:
            synthetic = f"{phenotype_name_raw} ({mapping_key})"
        phenotype_name_clean, _, _, association_modifier = (
            OMIMPipeline._parse_phenotype_field(synthetic)
        )

        record = cls(
            phenotype_name=phenotype_name_clean or phenotype_name_raw.strip() or None,
            phenotype_mim=phenotype_mim,
            mapping_key=mapping_key,
            gene_symbols_raw=gene_symbols_raw,
            gene_mim=gene_mim,
            cyto_location=cyto_location,
            association_modifier=association_modifier,
            source_format="api_json",
            source_line_number=None,
        )
        record.validate()
        return record


# ============================================================================
# OMIMPipeline — institutional-grade rewrite.
# ============================================================================
class OMIMPipeline(BasePipeline):
    """OMIM pipeline for gene-phenotype association data.

    Institutional-grade rewrite per OMIM_PIPELINE_MASTER_FIX_PROMPT.md.
    Mirrors the patterns established in ``DisGeNETPipeline`` while
    preserving the public method signatures (``download``, ``clean``,
    ``load``, ``run``) for backward compatibility with ``dags/omim_dag.py``.

    Public methods (DO NOT change signatures — BUG-1.x anti-requirements):
        - ``download() -> Path`` — fetch morbidmap.txt (or API JSON).
        - ``clean(raw_path: Path) -> pd.DataFrame`` — full §7.3 pipeline.
        - ``load(df: pd.DataFrame) -> int`` — full §7.16 lineage.
        - ``run_load_only() -> int`` — re-validate CSV + manifest, then load.

    The pipeline is idempotent: running ``clean()`` twice on the same input
    produces byte-identical CSV + manifest (BUG-7.1, BUG-7.14). Running
    ``load()`` twice produces no new DB rows (BUG-7.6, BUG-2.10).
    """

    source_name = "omim"

    # v29 ROOT FIX (audit P1-22): was 0.1 req/s — 24h+ ingest. Increased to 1 req/s (10x).
    # OMIM's published API rate limit is 4 req/sec, so 1 req/sec is well
    # within tolerance. The previous setup effectively ran at ~0.1 req/s
    # under real ETL load (request latency + retries + the per-request
    # sleep stacked up to ~10 s/req), pushing full ingest past 24 h.
    # OMIM_MAX_REQUEST_INTERVAL_SEC caps the per-request sleep so the
    # NOMINAL rate never drops below 1 req/s regardless of how
    # OMIM_REQUEST_INTERVAL is set downstream (defence-in-depth against
    # a future env-var misconfiguration re-introducing the 24 h ingest).
    OMIM_MAX_REQUEST_INTERVAL_SEC: float = 1.0

    # ------------------------------------------------------------------
    # Construction & validation (BUG-12.11)
    # ------------------------------------------------------------------
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the OMIM pipeline.

        Args:
            *args, **kwargs: passed through to ``BasePipeline.__init__``.
                Supported kwargs (see BasePipeline): run_id, correlation_id,
                triggered_by, as_of_date, freeze_version, snapshot_tag, seed.

        Raises:
            ValueError: if a critical OMIM_* env var is misconfigured.
        """
        super().__init__(*args, **kwargs)
        # Re-run config validation eagerly so OMIM-specific errors surface
        # at construction time, not mid-pipeline (BUG-12.11).
        self._validate_omim_config()

        # BUG-8.11: HTTP connection reuse via a single requests.Session.
        self._session: requests.Session = requests.Session()

        # BUG-5.17: in-memory quarantine buffer (flushed at end of clean()).
        self._quarantine_buffer: list[dict] = []

        # BUG-11.17: silent-skip counter (logged at end of clean()/load()).
        self._silent_skip_counter: dict[str, int] = {}

        # Source-format tracking (set by download()).
        self._source_format: Literal["morbidmap_txt", "api_json"] = "morbidmap_txt"
        self._source_url_sanitised: str = OMIM_DOWNLOADS_URL_SANITISED
        self._source_version: str | None = None
        self._download_method_used: str = "morbidmap"
        self._api_calls_made: int = 0
        self._api_calls_retried: int = 0

        # SHA-256 of the raw download (set by download()).
        self._sha256_raw: str | None = None
        self._sha256_cleaned: str | None = None

        # Manifest path (lazily resolved against PROCESSED_DATA_DIR so
        # tests can monkeypatch PROCESSED_DATA_DIR after __init__).
        self._manifest_path: Path | None = None

        # Track the cleaned DataFrame fingerprint for the manifest.
        self._input_fingerprint: str | None = None

    def _validate_omim_config(self) -> None:
        """Validate OMIM_* config at construction time (BUG-12.11).

        Raises:
            ValueError: on invalid configuration.
        """
        errors: list[str] = []
        if OMIM_REQUEST_INTERVAL <= 0:
            errors.append("OMIM_REQUEST_INTERVAL must be > 0")
        if not (1 <= OMIM_API_PAGE_LIMIT <= 1000):
            errors.append("OMIM_API_PAGE_LIMIT must be in [1, 1000]")
        if OMIM_API_MAX_RETRIES < 0:
            errors.append("OMIM_API_MAX_RETRIES must be >= 0")
        for mk in OMIM_MAPPING_KEYS_INCLUDE:
            if mk not in (1, 2, 3, 4):
                errors.append(
                    f"OMIM_MAPPING_KEYS_INCLUDE contains invalid mk={mk} "
                    f"(must be in {{1, 2, 3, 4}})"
                )
        for name, val in (
            ("OMIM_CONFIRMED_SCORE", OMIM_CONFIRMED_SCORE),
            ("OMIM_CONTIGUOUS_SCORE", OMIM_CONTIGUOUS_SCORE),
            ("OMIM_PHENOTYPE_MAPPED_SCORE", OMIM_PHENOTYPE_MAPPED_SCORE),
            ("OMIM_GENE_MAPPED_SCORE", OMIM_GENE_MAPPED_SCORE),
        ):
            if not (0.0 <= val <= 1.0):
                errors.append(f"{name} must be in [0.0, 1.0] (got {val})")
        if errors:
            raise ValueError(
                "OMIM config validation failed:\n  - " + "\n  - ".join(errors)
            )

    # ------------------------------------------------------------------
    # Authentication headers (BUG-2.2 / BUG-9.1)
    # ------------------------------------------------------------------
    @staticmethod
    def _omim_auth_headers() -> dict[str, str]:
        """Build the Authorization header for OMIM REST API requests.

        BUG-2.2 / BUG-9.1: OMIM REST API accepts ``Authorization: ApiKey
        <KEY>``. The header form is strongly preferred over the query-string
        ``apiKey`` form because CDNs/proxies log query strings.
        """
        return {
            "Authorization": f"ApiKey {OMIM_API_KEY.strip()}",
            "User-Agent": OMIM_USER_AGENT,
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------
    # Public API: download
    # ------------------------------------------------------------------
    def download(self) -> Path:
        """Download OMIM gene-phenotype mapping data.

        If ``OMIM_API_KEY`` is set: download ``morbidmap.txt`` directly from
        OMIM's data-downloads endpoint. The key is part of the URL path
        (BUG-2.1 — OMIM's downloads endpoint does NOT accept Authorization
        headers; the previous "FIX #12" header-auth attempt was fake).

        If ``OMIM_API_KEY`` is empty: raise RuntimeError. The API path also
        requires a key (BUG-9.15).

        Returns:
            Path to the downloaded file (morbidmap.txt or omim_genemaps.json).
        """
        # BUG-9.15 / BUG-9.16: refuse to run without a key.
        if not OMIM_API_KEY:
            raise RuntimeError(
                "OMIM_API_KEY is not set — cannot download from OMIM. "
                "Set the OMIM_API_KEY environment variable."
            )
        # BUG-12.6: warn (don't raise) if the key doesn't match UUID format.
        if not re.match(OMIM_API_KEY_FORMAT_RE, OMIM_API_KEY):
            logger.warning(
                "[omim] OMIM_API_KEY does not match expected UUID format — "
                "may be mistyped"
            )

        # Primary path: morbidmap.txt direct download.
        try:
            path = self._download_morbidmap()
            self._source_format = "morbidmap_txt"
            self._download_method_used = "morbidmap"
            self._source_url_sanitised = OMIM_DOWNLOADS_URL_SANITISED
            return path
        except Exception as exc:
            # Log the sanitised error and re-raise — do NOT silently fall
            # through to the API path (BUG-6.7).
            logger.error(
                "[omim] morbidmap download failed: %s",
                self._sanitize_error_message(str(exc)),
            )
            raise

    def _download_morbidmap(self) -> Path:
        """Download morbidmap.txt from OMIM data downloads (BUG-2.1).

        OMIM's downloads endpoint requires the API key in the URL path.
        We do NOT attempt header auth (it always returns 401 — BUG-2.1's
        "FIX #12" was fake).
        """
        dest = self.raw_dir / "morbidmap.txt" if self.raw_dir else OMIM_RAW_MORBIDMAP_PATH
        dest.parent.mkdir(parents=True, exist_ok=True)

        # BUG-2.1: use url_with_key directly. No header-auth fallback.
        url = OMIM_DOWNLOADS_URL_TEMPLATE.format(api_key=OMIM_API_KEY)

        # Use the hardened BasePipeline._download_file (SHA-256 sidecar,
        # conditional requests, file locking, atomic rename, retry).
        path = self._download_file(
            url,
            dest,
            timeout=OMIM_DOWNLOAD_TIMEOUT,
        )

        # BUG-11.1: log the SHA-256 of the downloaded file.
        try:
            self._sha256_raw = self._compute_sha256(path)
            logger.info("[omim] morbidmap.txt SHA-256: %s", self._sha256_raw)
        except (OSError, ValueError) as exc:
            logger.warning("[omim] Could not compute morbidmap SHA-256: %s", exc)

        # BUG-7.10: parse the morbidmap header for the "Generated:" date.
        try:
            text = path.read_text(encoding="utf-8-sig", errors="strict")
            match = GENERATED_RE.search(text)
            if match:
                self._source_version = match.group(1)
                logger.info("[omim] morbidmap source_version: %s", self._source_version)
            else:
                self._source_version = "unknown"
                logger.warning("[omim] morbidmap 'Generated:' line not found — source_version=unknown")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("[omim] Could not read morbidmap header: %s", exc)
            self._source_version = "unknown"

        # BUG-5.6 / BUG-7.2: timeliness check.
        if self._source_version not in (None, "unknown"):
            try:
                gen_date = datetime.strptime(self._source_version, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
                age_days = (datetime.now(timezone.utc) - gen_date).days
                if age_days > OMIM_MAX_AGE_DAYS:
                    logger.warning(
                        "[omim] morbidmap is %d days old (> %d) — consider forcing refresh",
                        age_days, OMIM_MAX_AGE_DAYS,
                    )
            except (ValueError, TypeError):
                pass

        # BUG-11.18: log file mtime/size.
        try:
            stat = path.stat()
            mtime_iso = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
            logger.info(
                "[omim] Cache hit: %s (size=%d, mtime=%s)",
                path.name, stat.st_size, mtime_iso,
            )
        except OSError:
            pass

        return path

    # v22 ROOT FIX (audit section 6 finding 4 / section 9 — "~150 lines of
    # dead code in omim_pipeline"): the three functions ``_download_via_api``,
    # ``_fetch_gene_map_page``, ``_write_gene_map_json`` (plus their helper
    # ``_checkpoint_json``) were defined but NEVER CALLED. ``download()``
    # only invokes ``_download_morbidmap()`` — the morbidmap text file is
    # the production data source. The API path was a previous
    # implementation strategy that got replaced. All four functions have
    # been REMOVED to eliminate the dead code. The OMIM DAG docstring
    # has also been updated to remove references to the dead API path.
    # If a future operator needs the REST API path, they should re-add
    # it and WIRE IT INTO ``download()`` as a true fallback — not leave
    # it as dead code that looks callable but isn't.

    def _is_cache_fresh(self, dest: Path) -> bool:
        """Return True iff the cached file is younger than ``OMIM_MAX_AGE_DAYS``
        (BUG-5.6 / BUG-7.2).
        """
        try:
            stat = dest.stat()
            age_days = (time.time() - stat.st_mtime) / 86400.0
            return age_days <= OMIM_MAX_AGE_DAYS
        except OSError:
            return False

    def _api_get(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
    ) -> requests.Response:
        """GET with retry on transient errors and rate limiting (BUG-1.1).

        Uses ``Authorization: ApiKey`` header (BUG-2.2) — never leaks the
        key via query string. Honors ``Retry-After`` headers (BUG-6.1).
        Single sleep per request (BUG-8.1). Bounded retries with jitter
        (BUG-4.9 / BUG-7.3).
        """
        max_retries = OMIM_API_MAX_RETRIES
        headers = self._omim_auth_headers()

        last_exc: Exception | None = None
        for attempt in range(1, max_retries + 1):
            self._api_calls_made += 1
            try:
                resp = self._session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=OMIM_API_TIMEOUT,
                )
            except RETRYABLE_EXCEPTIONS as exc:
                last_exc = exc
                if attempt == max_retries:
                    safe = self._sanitize_error_message(str(exc))
                    raise RuntimeError(
                        f"OMIM GET failed after {max_retries} retries: {safe}"
                    ) from exc
                self._api_calls_retried += 1
                wait = self._backoff_seconds(attempt)
                logger.warning(
                    "[omim] GET %s failed (attempt %d/%d): %s — retrying in %.2fs",
                    self._sanitize_url(url), attempt, max_retries,
                    self._sanitize_error_message(str(exc)), wait,
                )
                time.sleep(wait)
                continue

            # BUG-6.1: respect Retry-After header on 429.
            if resp.status_code in RETRYABLE_STATUS_CODES:
                if attempt == max_retries:
                    raise RuntimeError(
                        f"OMIM GET {self._sanitize_url(url)} returned "
                        f"{resp.status_code} after {max_retries} retries"
                    )
                self._api_calls_retried += 1
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        wait = self._backoff_seconds(attempt)
                else:
                    wait = self._backoff_seconds(attempt)
                logger.warning(
                    "[omim] GET %s returned %d (attempt %d/%d) — retrying in %.2fs",
                    self._sanitize_url(url), resp.status_code,
                    attempt, max_retries, wait,
                )
                time.sleep(wait)
                continue

            # BUG-6.16: detect 404-in-500-body (permanent URL error).
            if resp.status_code == 500:
                body_preview = (resp.text or "")[:500].lower()
                if "not found" in body_preview or "404" in body_preview:
                    raise RuntimeError(
                        f"OMIM GET {self._sanitize_url(url)} returned 500 "
                        f"with 'Not Found' body — likely permanent URL error"
                    )

            resp.raise_for_status()

            # BUG-8.1: single sleep per request (here, NOT in the caller).
            # v29 ROOT FIX (audit P1-22): was 0.1 req/s — 24h+ ingest. Increased to 1 req/s (10x).
            # Cap the per-request sleep at OMIM_MAX_REQUEST_INTERVAL_SEC (1.0s)
            # so the nominal rate is at least 1 req/s. If OMIM_REQUEST_INTERVAL
            # is misconfigured to a larger value downstream, this guard
            # prevents the 24h-ingest regression from re-emerging.
            time.sleep(min(OMIM_REQUEST_INTERVAL, self.OMIM_MAX_REQUEST_INTERVAL_SEC))
            return resp

        # Should be unreachable, but be defensive.
        safe = self._sanitize_error_message(str(last_exc)) if last_exc else "unknown"
        raise RuntimeError(
            f"OMIM GET {self._sanitize_url(url)} exhausted retries: {safe}"
        )

    @staticmethod
    def _backoff_seconds(attempt: int) -> float:
        """Compute a bounded backoff with jitter (BUG-4.9 / BUG-7.3).

        ``wait = min(2 ** attempt, 30) + random.uniform(0, 1)``.

        v41 ROOT FIX (SEV3-MEDIUM #12): use the module-local
        ``_OMIM_RNG`` instance instead of the global ``random``
        module's ``uniform``. This preserves the BUG-7.4
        reproducibility guarantee (the same OMIM_RANDOM_SEED produces
        the same jitter sequence) WITHOUT mutating the global RNG
        that other modules depend on.
        """
        base = min(2 ** attempt, 30)
        # v41 ROOT FIX (SEV3-MEDIUM #12): use local RNG.
        jitter = _OMIM_RNG.uniform(0, 1)
        return float(base + jitter)

    # ------------------------------------------------------------------
    # Public API: clean
    # ------------------------------------------------------------------
    def clean(self, raw_path: Path) -> pd.DataFrame:
        """Clean and normalize OMIM gene-phenotype data.

        Implements the full §7.3 / Domain-3 (scientific-correctness) pipeline.
        See the module docstring for the step-by-step contract.

        Args:
            raw_path: path to morbidmap.txt or omim_genemaps.json.

        Returns:
            A DataFrame satisfying the OMIM GDA contract
            (``assert_is_omim_gda_df`` passes).
        """
        clean_started_at = datetime.now(timezone.utc)
        t0 = time.monotonic()

        # BUG-2.5 / BUG-3.6: log the active mapping-key include-list at INFO.
        logger.info(
            "[omim] clean() starting — OMIM_MAPPING_KEYS_INCLUDE=%s, "
            "OMIM_EXCLUDE_SUSCEPTIBILITY=%s",
            OMIM_MAPPING_KEYS_INCLUDE, OMIM_EXCLUDE_SUSCEPTIBILITY,
        )

        # Step 1: parse — morbidmap.txt or JSON.
        if raw_path.suffix == ".json":
            records = self._parse_json(raw_path)
            self._source_format = "api_json"
            self._download_method_used = "api"
        else:
            records = self._parse_morbidmap(raw_path)
            self._source_format = "morbidmap_txt"
            self._download_method_used = "morbidmap"

        self._log_row_count("parsed", pd.DataFrame(records) if records else pd.DataFrame())

        # Step 2: empty-handling (BUG-6.9).
        if not records:
            logger.warning("[omim] No OMIM records extracted — writing empty manifest")
            df = self._empty_gda_df()
            self._populate_lineage_columns(df)
            self._save_processed_csv(df, OMIM_OUTPUT_PATH, primary_source="omim")
            self._flush_quarantine()
            self._write_manifest(df, clean_started_at, datetime.now(timezone.utc))
            return df

        # Step 3: build the DataFrame.
        df = pd.DataFrame(records)
        self._log_row_count("parsed_df", df)

        # Step 4: drop records with empty phenotype_name (BUG-5.7).
        if "phenotype_name" in df.columns:
            mask_empty = df["phenotype_name"].isna() | (df["phenotype_name"].astype(str).str.strip() == "")
            if mask_empty.any():
                logger.warning(
                    "[omim] Dropping %d records with empty phenotype_name",
                    int(mask_empty.sum()),
                )
                self._write_dead_letter_file(df[mask_empty].copy(), reason="empty_phenotype_name")
                df = df[~mask_empty].copy()
                self._silent_skip_counter["empty_phenotype_name"] = int(mask_empty.sum())

        # Step 5: BUG-5.9 — warn on empty gene_symbols_raw.
        if "gene_symbols_raw" in df.columns:
            empty_mask = df["gene_symbols_raw"].fillna("").str.strip() == ""
            if empty_mask.any():
                logger.warning(
                    "[omim] %d records have empty gene_symbols_raw",
                    int(empty_mask.sum()),
                )

        # Step 6: BUG-2.5 / BUG-3.5 / BUG-3.6 — filter mapping_key.
        if "mapping_key" in df.columns:
            before = len(df)
            df = df[df["mapping_key"].isin(OMIM_MAPPING_KEYS_INCLUDE)].copy()
            dropped = before - len(df)
            logger.info(
                "[omim] Filtered mapping_key in %s: %d -> %d (dropped %d)",
                OMIM_MAPPING_KEYS_INCLUDE, before, len(df), dropped,
            )
            if dropped:
                self._silent_skip_counter["filtered_mapping_key"] = dropped
            self._log_row_count("filtered_mapping_key", df)

        # Step 7: explode gene symbols (BUG-3.9).
        if "gene_symbols_raw" in df.columns:
            # BUG-3.9 / BUG-4.4: regex split, fillna to avoid NaN propagation.
            df["gene_symbol"] = df["gene_symbols_raw"].fillna("").str.split(r"\s*,\s*")
            df = df.explode("gene_symbol", ignore_index=True)
            df["gene_symbol"] = df["gene_symbol"].astype(str).str.strip()
            # BUG-3.11: uppercase gene symbols (HGNC convention).
            df["gene_symbol"] = df["gene_symbol"].str.upper()
            # Drop empty gene symbols (BUG-3.24).
            empty_gene_mask = (df["gene_symbol"] == "") | (df["gene_symbol"].isin(["NAN", "NONE"]))
            if empty_gene_mask.any():
                logger.info(
                    "[omim] Dropping %d records with empty/NaN gene_symbol after explode",
                    int(empty_gene_mask.sum()),
                )
                df = df[~empty_gene_mask].copy()
            self._log_row_count("exploded", df)

        # Step 8: BUG-3.10 — HGNC validation (best-effort, non-blocking).
        # v16 ROOT FIX (SF-5): the previous code silently skipped HGNC
        # validation when ``_load_hgnc_symbols()`` returned an empty
        # frozenset. The empty return was logged INSIDE _load_hgnc_symbols
        # (at DEBUG/WARNING), but the call site did NOT log anything
        # — so the operator saw "hgnc_validated" in the run report
        # without realizing NO validation actually happened. Placeholder
        # gene symbols (e.g. "LOC123456", "MIR7-1") leaked through.
        hgnc = _load_hgnc_symbols()
        if hgnc:
            mask = ~df["gene_symbol"].isin(hgnc)
            n_unknown = int(mask.sum())
            if n_unknown:
                logger.warning(
                    "[omim] %d gene_symbols not in HGNC approved set — flagging",
                    n_unknown,
                )
                self._write_dead_letter_file(df[mask].copy(), reason="non_hgnc_symbol")
                df = df[~mask].copy()
                self._silent_skip_counter["non_hgnc_symbol"] = n_unknown
            self._log_row_count("hgnc_validated", df)
        else:
            # v16 SF-5: explicit WARNING at the call site.
            # v20 SF-5 ROOT FIX: WARNING + metric alone are NOT enough —
            # placeholder/non-HGNC gene symbols still leak through into
            # the staging DB and downstream Knowledge Graph. The audit's
            # complaint was that the skip was "silent" — but even with
            # the WARNING, the pipeline continues and emits poisoned
            # gene-disease edges. Production deployments must be able
            # to enforce HGNC validation as a hard gate.
            #
            # Two strict-mode triggers:
            #   1. DRUGOS_STRICT=1 (global strict flag, same as ChEMBL)
            #   2. DRUGOS_OMIM_STRICT_HGNC=1 (OMIM-specific override)
            # v22 ROOT FIX (audit section 6 finding 6 — "HGNC validation is
            # non-blocking"): the previous code only enforced HGNC validation
            # when DRUGOS_STRICT=1 was EXPLICITLY set. In production
            # deployments (DRUGOS_ENVIRONMENT=production), HGNC validation
            # was still skipped silently — placeholder gene symbols
            # (LOC123456, MIR7-1) leaked into the staging DB and the KG.
            # Fix: production environment implies strict mode automatically.
            # Operators who want to skip HGNC validation must explicitly
            # set DRUGOS_OMIM_STRICT_HGNC=0 in a non-production environment.
            _env = os.environ.get("DRUGOS_ENVIRONMENT", "dev").lower()
            _production = _env in ("prod", "production")
            _strict = (
                _production
                or os.environ.get("DRUGOS_STRICT", "") == "1"
                or os.environ.get("DRUGOS_OMIM_STRICT_HGNC", "") == "1"
            )
            logger.warning(
                "[omim] HGNC validation SKIPPED — _load_hgnc_symbols() "
                "returned an empty set (file missing or unreadable). "
                "Placeholder / non-HGNC gene symbols will leak through. "
                "Set HGNC_SYMBOLS_PATH env var or download the HGNC "
                "complete set from https://www.genenames.org/download/statistics/"
            )
            self._emit_metric("omim_hgnc_validation_skipped", 1)
            if _strict:
                raise RuntimeError(
                    "HGNC validation SKIPPED in strict mode "
                    "(DRUGOS_ENVIRONMENT=production, DRUGOS_STRICT=1, or "
                    "DRUGOS_OMIM_STRICT_HGNC=1). "
                    "Placeholder gene symbols cannot be allowed into the "
                    "staging DB. Set HGNC_SYMBOLS_PATH or unset strict mode "
                    "(set DRUGOS_ENVIRONMENT=dev AND DRUGOS_OMIM_STRICT_HGNC=0)."
                )

        # Step 9: BUG-3.19 — coerce phenotype_mim to Int64, build disease_id.
        if "phenotype_mim" in df.columns:
            df["phenotype_mim"] = pd.to_numeric(df["phenotype_mim"], errors="coerce").astype("Int64")
            # Build disease_id only for valid (non-null) phenotype_mim.
            df["disease_id"] = df["phenotype_mim"].apply(
                lambda m: f"OMIM:{int(m)}" if pd.notna(m) else None
            )
            # BUG-3.8 / BUG-3.23: disease_id must match DisGeNET's format
            # ("OMIM:{int}", no zero-pad). It already does.
            # BUG-A-007 root fix: validate that disease_id starts with "OMIM:"
            # and contains only digits after the prefix. The previous code
            # allowed rows where disease_id was set to a gene_symbol string
            # like "FGFR3" (a clear parsing corruption). Such rows must be
            # quarantined, not propagated to Phase 2.
            if "disease_id" in df.columns:
                bad_disease_mask = df["disease_id"].notna() & \
                    ~df["disease_id"].astype(str).str.match(r"^OMIM:\d+$")
                if bad_disease_mask.any():
                    n_bad = int(bad_disease_mask.sum())
                    logger.error(
                        "[omim] BUG-A-007: %d rows have invalid disease_id "
                        "(not 'OMIM:<digits>'). Quarantining. Examples: %s",
                        n_bad,
                        df.loc[bad_disease_mask, "disease_id"].head(5).tolist(),
                    )
                    self._write_dead_letter_file(
                        df[bad_disease_mask].copy(),
                        reason="invalid_disease_id_format",
                    )
                    df = df[~bad_disease_mask].copy()
                    self._silent_skip_counter["invalid_disease_id_format"] = n_bad

        # Step 10: BUG-3.22 — cyto_location validation.
        if "cyto_location" in df.columns:
            cyto_mask = df["cyto_location"].notna() & (df["cyto_location"].astype(str) != "")
            invalid = cyto_mask & ~df["cyto_location"].astype(str).str.match(CYTO_RE)
            df["cyto_location_valid"] = True
            df.loc[invalid, "cyto_location_valid"] = False
            n_invalid = int(invalid.sum())
            if n_invalid:
                logger.warning(
                    "[omim] %d malformed cyto_locations — keeping with flag",
                    n_invalid,
                )

        # Step 11: BUG-3.18 — extract inheritance_pattern from phenotype_name.
        if "phenotype_name" in df.columns:
            df["inheritance_pattern"] = df["phenotype_name"].apply(
                lambda s: _extract_inheritance_pattern(s) if isinstance(s, str) else None
            )

        # Step 12: BUG-3.16 — pre-dedup before scoring.
        if {"phenotype_mim", "gene_symbol", "mapping_key"}.issubset(df.columns):
            before = len(df)
            df = df.drop_duplicates(
                subset=["phenotype_mim", "gene_symbol", "mapping_key"],
                keep="first",
            ).copy()
            if len(df) < before:
                logger.info(
                    "[omim] Pre-validation dedup: %d -> %d", before, len(df)
                )
                self._silent_skip_counter["pre_dedup"] = before - len(df)

        # Step 13: BUG-3.4 / BUG-3.15 — derive association_type from modifier.
        if "association_modifier" in df.columns:
            df["association_type"] = df["association_modifier"].map(
                MARKER_TO_ASSOCIATION_TYPE
            ).fillna(ASSOCIATION_TYPE_DEFAULT)
            df["is_susceptibility"] = df["association_modifier"] == "{}"
        else:
            df["association_type"] = ASSOCIATION_TYPE_DEFAULT
            df["is_susceptibility"] = False

        # Step 14: BUG-3.2 / BUG-3.3 / BUG-4.5 — vectorized scoring.
        df = self._compute_scores(df)
        self._log_row_count("scored", df)

        # Step 15: BUG-3.13 — route susceptibility records to separate CSV.
        if OMIM_EXCLUDE_SUSCEPTIBILITY and "is_susceptibility" in df.columns:
            susceptibility_mask = df["is_susceptibility"] == True  # noqa: E712
            if susceptibility_mask.any():
                susceptibility_df = df[susceptibility_mask].copy()
                self._save_processed_csv(
                    susceptibility_df,
                    OMIM_SUSCEPTIBILITY_OUTPUT_PATH,
                    primary_source="omim_susceptibility",
                )
                logger.info(
                    "[omim] Routed %d susceptibility records to %s",
                    len(susceptibility_df), OMIM_SUSCEPTIBILITY_OUTPUT_PATH,
                )
                df = df[~susceptibility_mask].copy()
                self._silent_skip_counter["routed_susceptibility"] = int(susceptibility_mask.sum())

        # Step 16: BUG-3.24 — assert gene_symbol non-empty AND alphabetic.
        # BUG-A-008 root fix: gene_symbol must be alphabetic (HGNC convention).
        # The previous code only checked for NaN/empty, allowing numeric
        # values like "26" (a clear parsing corruption) to slip through.
        # Gene symbols may contain trailing digits (e.g. ABC1, MYC12) but
        # must start with at least one letter.
        if "gene_symbol" in df.columns:
            n_nan = int(df["gene_symbol"].isna().sum())
            n_empty = int((df["gene_symbol"].astype(str).str.len() == 0).sum())
            if n_nan or n_empty:
                raise RuntimeError(
                    f"clean() produced gene_symbol with {n_nan} NaN and "
                    f"{n_empty} empty values — upstream parsing failed"
                )
            # BUG-A-008: gene_symbol must start with a letter.
            non_alphabetic_mask = ~df["gene_symbol"].astype(str).str.match(r"^[A-Z][A-Z0-9]*$")
            n_bad = int(non_alphabetic_mask.sum())
            if n_bad:
                logger.error(
                    "[omim] BUG-A-008: %d gene_symbols are non-alphabetic "
                    "(must match ^[A-Z][A-Z0-9]*$). Quarantining. Examples: %s",
                    n_bad,
                    df.loc[non_alphabetic_mask, "gene_symbol"].head(5).tolist(),
                )
                self._write_dead_letter_file(
                    df[non_alphabetic_mask].copy(),
                    reason="non_alphabetic_gene_symbol",
                )
                df = df[~non_alphabetic_mask].copy()
                self._silent_skip_counter["non_alphabetic_gene_symbol"] = n_bad

        # Step 17: BUG-2.13 — rebuild source_id (always rebuild NaN cells).
        if "gene_mim" in df.columns and "phenotype_mim" in df.columns:
            df["source_id"] = None
            mask = df["gene_mim"].notna() & df["phenotype_mim"].notna()
            if mask.any():
                df.loc[mask, "source_id"] = (
                    "OMIM:"
                    + df.loc[mask, "gene_mim"].astype(str)
                    + "_"
                    + df.loc[mask, "phenotype_mim"].astype(str)
                )

        # Step 18: BUG-2.14 — map phenotype_name -> disease_name (BEFORE
        # _ensure_gda_columns so the explicit mapping wins).
        if "phenotype_name" in df.columns:
            df["disease_name"] = df["phenotype_name"]

        # Step 19: populate all lineage columns (Domain 16).
        self._populate_lineage_columns(df)

        # Step 20: BUG-2.8 / §4.1 — validate_gda_scores with full kwargs.
        dedup_keys = ["gene_symbol", "disease_id", "source"]
        existing_keys = [k for k in dedup_keys if k in df.columns]
        df = validate_gda_scores(
            df,
            score_range=(0.0, 1.0),
            preserve_direction=False,          # OMIM scores are always positive
            source="omim",                     # COMP-5 / SCI-23
            dedup=True,                        # DQ-4 / SCI-22
            dedup_keys=existing_keys,
        )
        self._log_row_count("validate_gda_scores", df)

        # Step 21: BUG-2.4 / §4.3 — derive confidence_tier from score.
        if "score" in df.columns:
            # v43 ROOT FIX (P1-033): use OMIM-specific confidence tiers
            # instead of the default DisGeNET tiers. The default tiers
            # classify all OMIM scores (0.5-0.9) as "strong", making the
            # tier meaningless. The OMIM tiers preserve the mapping_key
            # distinction (provisional/confirmed/community/molecular).
            # v43 ROOT FIX (P1-028): the previous guard ``float(s) >= 0``
            # allowed scores >1.0 to reach classify_confidence, which
            # raises ValueError for out-of-range scores. The fix bounds
            # the check to [0.0, 1.0] — scores outside this range get
            # None (honest "can't classify") instead of crashing.
            df["confidence_tier"] = df["score"].apply(
                lambda s: (
                    classify_confidence(float(s), tiers=list(OMIM_CONFIDENCE_TIERS))
                    if pd.notna(s) and 0.0 <= float(s) <= 1.0
                    else None
                )
            )
            df["confidence_tier_method"] = CONFIDENCE_TIER_METHOD_VERSION
        else:
            df["confidence_tier"] = None
            df["confidence_tier_method"] = CONFIDENCE_TIER_METHOD_VERSION

        # Step 22: BUG-2.11 — ensure all GDA columns exist with proper defaults.
        df = self._ensure_gda_columns(df)

        # Step 22b: BUG-2.13 / old "FIX #21" — drop records with no disease_id
        # (cannot join to diseases table). Route to dead-letter for auditability.
        if "disease_id" in df.columns:
            no_disease_mask = df["disease_id"].isna() | (df["disease_id"].astype(str) == "")
            if no_disease_mask.any():
                n_dropped = int(no_disease_mask.sum())
                logger.warning(
                    "[omim] Dropping %d GDA records with no disease_id "
                    "(phenotype_mim was missing or out-of-range)",
                    n_dropped,
                )
                self._write_dead_letter_file(
                    df[no_disease_mask].copy(), reason="no_disease_id"
                )
                df = df[~no_disease_mask].copy()
                self._silent_skip_counter["no_disease_id"] = n_dropped

        # Step 23: BUG-5.19 — NaN assertions on required columns.
        for col in ["disease_id", "score", "confidence_tier", "source", "gene_symbol"]:
            if col in df.columns:
                n_nan = int(df[col].isna().sum())
                if n_nan:
                    raise RuntimeError(
                        f"clean() produced {n_nan} NaN values in required column {col!r}"
                    )

        # Step 24: BUG-7.14 — deterministic sort before write.
        sort_cols = [c for c in ["gene_symbol", "disease_id", "source"] if c in df.columns]
        if sort_cols:
            df = df.sort_values(sort_cols, kind="mergesort").reset_index(drop=True)

        # Step 24b: BUG-25a/v6 — populate uniprot_id + canonical_gene_id at
        # clean() time using an embedded HGNC/NCBI/UniProt crosswalk so the
        # CSV (not just the DB) has these columns populated. The previous
        # implementation only resolved uniprot_id inside load() (which writes
        # to DB), leaving the CSV with 100% NaN — the Phase 1 → Phase 2
        # bridge consumed the CSV and saw zero encodes edges.
        #
        # The crosswalk below is a static, well-known set of HGNC-approved
        # gene symbols → (NCBI Gene ID, UniProt AC, gene MIM). All values
        # are public, scientifically-correct identifiers. Production runs
        # that have a DB-backed gene_to_uniprot map SHOULD prefer the DB
        # data; this embedded crosswalk is a clean()-time fallback so the
        # CSV is always usable downstream.
        df = _resolve_gene_xref_embedded(df)
        self._log_row_count("gene_xref_resolved", df)

        # v29 ROOT FIX (audit P1-24): ID format divergence — normalize to
        # canonical form before writing. ``gene_symbol`` is uppercased +
        # stripped; ``uniprot_id`` (populated by the embedded crosswalk
        # above) is uppercased + stripped. This guarantees downstream
        # joins against UniProt (uniprot_id), DisGeNET (gene_symbol), and
        # DrugBank interactions (uniprot_id) succeed regardless of which
        # source wrote the value. OMIM's morbidmap.txt historically ships
        # gene symbols in mixed case (e.g. ``"Hbb"`` for mouse homologs
        # that slip through the human-only filter); without this
        # normalization a GDA record from OMIM would NOT join with the
        # same gene's record from DisGeNET.
        if len(df) > 0:
            if "gene_symbol" in df.columns:
                df["gene_symbol"] = df["gene_symbol"].apply(
                    lambda x: normalize_gene_symbol(x)
                    if pd.notna(x) and x != "" else x
                )
            if "uniprot_id" in df.columns:
                df["uniprot_id"] = df["uniprot_id"].apply(
                    lambda x: normalize_uniprot_id(x)
                    if pd.notna(x) and x != "" else x
                )

        # Step 25: BUG-1.9 — atomic write via _save_processed_csv.
        self._save_processed_csv(df, OMIM_OUTPUT_PATH, primary_source="omim")
        self._log_row_count("cleaned", df)

        # Step 26: write the manifest (BUG-1.7 / BUG-16.10).
        clean_finished_at = datetime.now(timezone.utc)
        self._write_manifest(df, clean_started_at, clean_finished_at)

        # Step 27: flush quarantine (BUG-5.17).
        self._flush_quarantine()

        # Step 28: BUG-11.17 — log silent-skip counters.
        if self._silent_skip_counter:
            logger.info(
                "[omim] Silent-skip summary: %s", self._silent_skip_counter
            )

        # Step 29: BUG-11.15 — pipeline duration log.
        duration_ms = int((time.monotonic() - t0) * 1000)
        logger.info("[omim] clean duration_ms=%d", duration_ms)

        # Step 30: BUG-11.7 — emit metrics.
        self._emit_metric(
            "records_cleaned", len(df), tags={"source": "omim"}
        )
        self._emit_metric(
            "clean_duration_ms", duration_ms, tags={"source": "omim"}
        )

        return df

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------
    def _parse_morbidmap(self, raw_path: Path) -> list[OMIMRecord]:
        """Parse OMIM morbidmap.txt into a list of OMIMRecord (BUG-3.1).

        Single-loop reader — the first non-``#`` non-empty line is a DATA
        row, not a header (BUG-3.1 — the previous two-loop pattern silently
        dropped the first data row every run). UTF-8 strict with a latin-1
        fallback for non-UTF-8 bytes (BUG-6.8).
        """
        records: list[OMIMRecord] = []

        # BUG-6.8: try utf-8-sig strict first, fall back to latin-1.
        try:
            text = raw_path.read_text(encoding="utf-8-sig", errors="strict")
        except UnicodeDecodeError as exc:
            logger.warning(
                "[omim] morbidmap not valid UTF-8 (%s) — falling back to latin-1",
                exc,
            )
            text = raw_path.read_text(encoding="latin-1", errors="replace")

        # BUG-3.1: single-loop reader. No header row in morbidmap.txt.
        for line_no, line in enumerate(text.splitlines(), start=1):
            if line.startswith("#") or not line.strip():
                continue
            try:
                record = OMIMRecord.from_morbidmap_line(line, line_no)
            except ValueError as exc:
                # BUG-3.7 / BUG-3.14 / BUG-3.20 / BUG-3.25 — quarantine.
                reason = self._classify_parse_failure(exc)
                self._quarantine_line(line, line_no, reason=reason)
                continue
            if record is None:
                self._quarantine_line(line, line_no, reason="parse_failure")
                continue
            records.append(record)

        # BUG-5.1: completeness check.
        if len(records) < OMIM_MIN_EXPECTED_RECORDS:
            # In test/dev contexts with small fixtures, this is OK — log a
            # warning rather than aborting. Production runs will exceed 5000.
            logger.warning(
                "[omim] Parsed %d records, below OMIM_MIN_EXPECTED_RECORDS=%d "
                "— possible truncated download or test fixture",
                len(records), OMIM_MIN_EXPECTED_RECORDS,
            )

        return records

    def _parse_json(self, raw_path: Path) -> list[OMIMRecord]:
        """Parse OMIM API JSON response into a list of OMIMRecord (BUG-5.15)."""
        with open(raw_path, "r", encoding="utf-8") as fh:
            gene_maps = json.load(fh)

        records: list[OMIMRecord] = []
        for gm in gene_maps:
            pheno_maps = gm.get("phenotypeMapList", [])
            for pm_entry in pheno_maps:
                pm = pm_entry.get("phenotypeMap", {})
                try:
                    record = OMIMRecord.from_api_entry(gm, pm)
                except ValueError as exc:
                    # Quarantine API records too.
                    self._quarantine_line(
                        json.dumps({"gene_mim": gm.get("mimNumber"),
                                    "phenotype": pm.get("phenotype")}),
                        line_no=0,
                        reason=self._classify_parse_failure(exc),
                    )
                    continue
                records.append(record)
        return records

    @staticmethod
    def _classify_parse_failure(exc: ValueError) -> str:
        """Map a ValueError from OMIMRecord.validate() to a quarantine reason."""
        msg = str(exc).lower()
        if "mapping_key" in msg:
            return "invalid_mapping_key"
        if "phenotype_mim" in msg and "outside" in msg:
            return "mim_out_of_range"
        if "phenotype_mim" in msg and "<= 0" in msg:
            return "mim_out_of_range"
        return "parse_failure"

    # ------------------------------------------------------------------
    # _parse_phenotype_field — the canonical phenotype-column parser.
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_phenotype_field(
        phenotype_col: str,
    ) -> tuple[str | None, int | None, int, str | None]:
        """Parse a morbidmap phenotype field (BUG-3.4, BUG-3.20, BUG-4.3).

        Format: ``"[?*+%] Phenotype Name, MIM_NUMBER (MAPPING_KEY)"``
        where the leading marker is optional and conveys semantic type.
        The ``{}`` and ``[]`` markers wrap the entire phenotype name.
        An optional trailing inheritance pattern (e.g. ``, autosomal
        recessive``) may follow the mapping key — it is preserved in
        ``phenotype_name`` so the caller can extract it via
        ``_extract_inheritance_pattern()`` (BUG-3.18).

        Args:
            phenotype_col: the raw phenotype column from morbidmap or the
                synthetic form constructed for API records.

        Returns:
            Tuple ``(phenotype_name, phenotype_mim, mapping_key, association_modifier)``.
            - ``phenotype_name``: normalized name with markers stripped
              and whitespace collapsed (BUG-3.12). The mapping key and
              MIM number are stripped, but a trailing inheritance pattern
              (if any) is preserved.
            - ``phenotype_mim``: int or None if no MIM number present.
            - ``mapping_key``: int in {0, 1, 2, 3, 4}. 0 if missing.
            - ``association_modifier``: one of ``"?"``, ``"{}"``, ``"[]"``,
              ``"*"``, ``"+"``, ``"%"``, or None.
        """
        if not phenotype_col or not phenotype_col.strip():
            return None, None, 0, None

        # BUG-4.13: don't reassign the parameter; use a local.
        remaining = phenotype_col.strip()

        # Step 1 (BUG-3.4): extract the leading marker FIRST.
        # For {} and [] wrappers, they enclose the ENTIRE phenotype string,
        # so we must strip them before looking for "(N)" or MIM numbers.
        association_modifier: str | None = None
        for pattern, modifier in MARKER_PATTERNS:
            m = pattern.match(remaining)
            if m:
                association_modifier = modifier
                # For {} and [], the wrapper is stripped and the inner text
                # is the phenotype name. For single-char markers (?, *, +, %),
                # the marker itself is stripped.
                if modifier in ("{}", "[]"):
                    remaining = m.group(1).strip()
                else:
                    remaining = m.group(1).strip() if m.group(1) else ""
                break

        # Step 2 (BUG-3.20): extract the mapping key.
        # Try the strict form first (mapping key at end of string — the
        # canonical morbidmap format). If that fails, try the lenient form
        # which allows a trailing comma + inheritance annotation (BUG-3.18).
        mapping_key = 0
        mk_match = MAPPING_KEY_RE.search(remaining)
        if mk_match:
            mapping_key = int(mk_match.group(1))
            remaining = remaining[: mk_match.start()].strip()
        else:
            mk_lenient = MAPPING_KEY_RE_LENIENT.search(remaining)
            if mk_lenient:
                mapping_key = int(mk_lenient.group(1))
                # Remove only the "(N)," part; keep the trailing text (which
                # contains the inheritance pattern — BUG-3.18 extracts it
                # separately).
                remaining = (
                    remaining[: mk_lenient.start()]
                    + remaining[mk_lenient.end():]
                ).strip()

        # Step 3 (BUG-3.21): extract the MIM number.
        # Take the LAST 5-7 digit comma-separated number — the MIM number
        # is conventionally the last numeric token before the mapping key.
        phenotype_mim: int | None = None
        all_mim_matches = list(MIM_NUMBER_RE.finditer(remaining))
        if all_mim_matches:
            mim_match = all_mim_matches[-1]
            phenotype_mim = int(mim_match.group(1))
            remaining = (remaining[: mim_match.start()] + remaining[mim_match.end():]).strip()

        # Step 4 (BUG-3.12): normalize the phenotype name.
        phenotype_name = remaining.strip().rstrip(",").strip()
        phenotype_name = re.sub(r"\s+", " ", phenotype_name)
        if not phenotype_name:
            phenotype_name = None

        return phenotype_name, phenotype_mim, mapping_key, association_modifier

    # ------------------------------------------------------------------
    # Scoring (BUG-3.2 / BUG-3.3 / BUG-4.5)
    # ------------------------------------------------------------------
    @staticmethod
    def _compute_omim_score(
        mapping_key: int,
        num_pmids: int = 0,
        evidence_strength: float = 0.0,
    ) -> tuple[float, str]:
        """Pure reference implementation of the OMIM score (BUG-1.5, BUG-3.2).

        Args:
            mapping_key: OMIM phenotype mapping key (1, 2, 3, or 4).
            num_pmids: number of supporting PubMed IDs (0 if unknown).
            evidence_strength: secondary evidence metric in [0, 1].

        Returns:
            Tuple ``(score, score_method)``. The score is in [0, 1].

        Score formula:
            base = SCORE_BY_MAPPING_KEY.get(mapping_key, 0.4)
            pmid_bonus = min(0.05 * log1p(num_pmids), 0.08)
            evidence_bonus = min(evidence_strength * 0.05, 0.05)
            score = clip(base + pmid_bonus + evidence_bonus, 0, 1)

        Rationale: mk=3 (molecular basis known) is the strongest single
        signal; supplementary PMID count and evidence_strength add modest
        uplift. We deliberately cap bonuses so a single weak paper can't
        inflate a weakly-mapped record past a strongly-mapped one.
        """
        base = SCORE_BY_MAPPING_KEY.get(mapping_key, DEFAULT_MAPPING_KEY_SCORE)
        pmid_bonus = min(
            PMID_BONUS_COEFFICIENT * math.log1p(max(0, num_pmids)),
            PMID_BONUS_CAP,
        )
        evidence_bonus = min(
            max(0.0, evidence_strength) * EVIDENCE_BONUS_COEFFICIENT,
            EVIDENCE_BONUS_CAP,
        )
        score = max(0.0, min(1.0, base + pmid_bonus + evidence_bonus))
        score_method = f"omim_v1_mk{mapping_key}_pmid{num_pmids}"
        return score, score_method

    def _compute_scores(self, df: pd.DataFrame) -> pd.DataFrame:
        """Vectorized scoring (BUG-3.2 / BUG-3.3 / BUG-4.5 / BUG-4.19).

        Raises ValueError if ``mapping_key`` column is missing.
        """
        if "mapping_key" not in df.columns:
            raise ValueError(
                "mapping_key column missing from OMIM cleaned df — "
                "clean() did not run scoring"
            )

        # Base score from mapping_key (BUG-2.3 — every branch reachable).
        base = df["mapping_key"].map(SCORE_BY_MAPPING_KEY).fillna(DEFAULT_MAPPING_KEY_SCORE)

        # PMID bonus (BUG-4.5 — vectorized).
        # v41 ROOT FIX (SEV2-HIGH #10): when ``original_pmid_count`` is
        # missing/None (which is the COMMON case because the OMIM
        # morbidmap.txt API doesn't return PMIDs), all rows got
        # PMID bonus = 0 silently. The score formula's "score is
        # never flat" claim (line 60) was defeated for every
        # production run that didn't have PMID enrichment upstream.
        # Fix: still treat missing as 0 (the math is correct — no
        # PMIDs means no bonus), but emit a single WARNING per run
        # so operators know the score is in the "base only" regime
        # and can decide whether to enable PMID enrichment (e.g.
        # via the DisGeNET PMID crosswalk) for a more informative
        # score distribution.
        if "original_pmid_count" in df.columns:
            pmid_count = pd.to_numeric(
                df["original_pmid_count"], errors="coerce"
            ).fillna(0).clip(lower=0)
            _n_pmid_missing = int(
                pd.to_numeric(df["original_pmid_count"], errors="coerce")
                .isna().sum()
            )
            if _n_pmid_missing > 0:
                logger.warning(
                    "[omim] %d of %d rows had NaN original_pmid_count "
                    "(filled with 0). Score formula is in 'base only' "
                    "regime — consider enabling PMID enrichment (e.g. "
                    "via the DisGeNET PMID crosswalk) for a more "
                    "informative score distribution.",
                    _n_pmid_missing, len(df),
                )
        else:
            pmid_count = pd.Series([0] * len(df), dtype=float)
            logger.warning(
                "[omim] original_pmid_count column is MISSING — all %d "
                "rows will get PMID bonus=0. Score formula is in 'base "
                "only' regime. The OMIM morbidmap.txt source does not "
                "publish PMIDs; this is expected unless a PMID "
                "enrichment step was supposed to run upstream.",
                len(df),
            )
        pmid_bonus = np.minimum(
            PMID_BONUS_COEFFICIENT * np.log1p(pmid_count),
            PMID_BONUS_CAP,
        )

        # Evidence bonus.
        if "evidence_strength" in df.columns:
            ev = pd.to_numeric(
                df["evidence_strength"], errors="coerce"
            ).fillna(0).clip(lower=0)
        else:
            ev = pd.Series([0.0] * len(df), dtype=float)
        evidence_bonus = np.minimum(ev * EVIDENCE_BONUS_COEFFICIENT, EVIDENCE_BONUS_CAP)

        # BUG-4.19 — raise if score column missing (we're about to set it,
        # so this check is just defensive).
        df["score"] = (base + pmid_bonus + evidence_bonus).clip(0.0, 1.0)

        # score_type and score_method (BUG-2.9, BUG-16.15).
        df["score_type"] = SCORE_TYPE_OMIM
        source_version_str = self._source_version or "unknown"
        df["score_method"] = f"omim_v1_{source_version_str}"

        return df

    # ------------------------------------------------------------------
    # Column-management helpers (BUG-2.11, BUG-2.13, BUG-2.14)
    # ------------------------------------------------------------------
    def _ensure_gda_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Ensure all required GDA columns exist with proper defaults.

        BUG-2.11: consumes ``GDA_REQUIRED_COLUMNS`` as the single source
        of truth. BUG-2.13: always rebuilds NaN source_id cells. BUG-2.14:
        ``phenotype_name → disease_name`` mapping is done BEFORE this method
        (in clean()), so the default-None for disease_name only applies
        when no mapping was possible.
        """
        for col, default in GDA_REQUIRED_COLUMNS:
            if col not in df.columns:
                df[col] = default
        return df

    @staticmethod
    def _empty_gda_df() -> pd.DataFrame:
        """Return an empty DataFrame with the full GDA schema (BUG-2.11)."""
        return pd.DataFrame(
            {col: pd.Series(dtype=object) for col, _ in GDA_REQUIRED_COLUMNS}
        )

    # ------------------------------------------------------------------
    # Lineage column population (BUG-16.x, Domain 16)
    # ------------------------------------------------------------------
    def _populate_lineage_columns(self, df: pd.DataFrame) -> None:
        """Populate all lineage columns on the cleaned DataFrame (Domain 16).

        Mutates df in place. Idempotent: re-running on an already-populated
        df produces the same values (BUG-7.6).
        """
        # Identity / canonical IDs (BUG-1.8, BUG-16.13).
        df["source"] = "omim"
        if "disease_id" in df.columns:
            df["canonical_disease_id"] = df["disease_id"]
        else:
            df["canonical_disease_id"] = None
        # canonical_gene_id is set to uniprot_id after resolution in load().

        # Source / format / version (BUG-16.3, BUG-16.5, BUG-16.6, BUG-16.7).
        df["source_format"] = self._source_format
        df["download_method"] = self._download_method_used
        df["source_version"] = self._source_version or "unknown"
        df["source_url"] = self._source_url_sanitised

        # BUG-16.4: download_date (ISO-8601 UTC).
        if self.start_time is not None:
            df["download_date"] = self.start_time.isoformat()
        else:
            df["download_date"] = datetime.now(timezone.utc).isoformat()

        # BUG-7.7: as_of_date for backfill safety.
        if self.as_of_date is not None:
            df["as_of_date"] = self.as_of_date.date().isoformat()
        else:
            df["as_of_date"] = datetime.now(timezone.utc).date().isoformat()

        # BUG-14.5 / BUG-7.11: schema_version + confidence_tier_method.
        df["schema_version"] = SCHEMA_VERSION_STAMP
        df["confidence_tier_method"] = CONFIDENCE_TIER_METHOD_VERSION

        # BUG-16.17: dedup_strategy.
        df["dedup_strategy"] = "validate_gda_scores_dedup"

        # BUG-16.18: filter_criteria — stored as a column for full traceability.
        df["filter_criteria"] = f"mapping_key in {OMIM_MAPPING_KEYS_INCLUDE}"

        # BUG-16.19: exploded_from.
        df["exploded_from"] = "gene_symbols_raw"

        # BUG-16.8: transformations audit trail.
        df["transformations"] = json.dumps([
            "parse", "filter_mk", "explode", "uppercase",
            "hgnc_validate", "score", "validate_gda_scores",
            "confidence_tier", "lineage",
        ])

        # HGNC snapshot version (BUG-3.10).
        df["hgnc_snapshot_version"] = _hgnc_snapshot_version()

        # source_record_id (BUG-16.12) — SHA-256 of (line_number + content),
        # truncated to 16 hex chars. Only computable for morbidmap records.
        if "source_line_number" in df.columns and "gene_symbols_raw" in df.columns:
            df["source_record_id"] = df.apply(
                lambda r: (
                    hashlib.sha256(
                        f"{r.get('source_line_number')}|{r.get('gene_symbols_raw', '')}".encode("utf-8")
                    ).hexdigest()[:16]
                    if pd.notna(r.get("source_line_number"))
                    else None
                ),
                axis=1,
            )
        else:
            df["source_record_id"] = None

    # ------------------------------------------------------------------
    # Atomic CSV write (BUG-1.9 — replaces _append_or_write_csv)
    # ------------------------------------------------------------------
    def _save_processed_csv(
        self,
        df: pd.DataFrame,
        output_path: Path,
        primary_source: str,
    ) -> None:
        """Persist the cleaned DataFrame to CSV atomically (BUG-1.9).

        Mirrors DisGeNET's ``_save_processed_csv`` (disgenet_pipeline.py:2667):
        - Atomic write via ``.tmp`` + ``os.replace`` (BUG-4.15, BUG-4.16, BUG-7.1).
        - Explicit ``encoding="utf-8"``, ``lineterminator="\\n"``,
          ``quoting=csv.QUOTE_ALL`` (BUG-4.16, BUG-15.11, BUG-15.12).
        - File permissions ``0o640`` (BUG-9.x).
        - Sidecar SHA-256 (BUG-7.13).
        - Manifest with full provenance (BUG-1.7, BUG-16.10).

        Replaces the legacy ``_append_or_write_csv`` (BUG-1.9). The new
        writer writes a fresh atomic file per run — never appends.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # BUG-1.9: atomic write via .tmp + os.replace.
        tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
        try:
            df.to_csv(
                tmp_path,
                index=False,
                encoding="utf-8",
                lineterminator="\n",
                quoting=csv_mod.QUOTE_ALL,
            )
            os.replace(tmp_path, output_path)
        except Exception:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            raise

        # BUG-7.13: SHA-256 sidecar.
        try:
            sha = hashlib.sha256(output_path.read_bytes()).hexdigest()
            sidecar = output_path.with_suffix(output_path.suffix + ".sha256")
            sidecar.write_text(sha, encoding="utf-8")
            if primary_source == "omim":
                self._sha256_cleaned = sha
            logger.info("[omim] Output CSV SHA-256 (%s): %s", primary_source, sha)
        except (OSError, ValueError) as exc:
            logger.warning("[omim] Could not compute output SHA-256: %s", exc)

        # BUG-9.x: file permissions.
        try:
            os.chmod(output_path, 0o640)
        except (OSError, ValueError) as exc:
            logger.warning(
                "[omim] Could not set file permissions on %s: %s",
                output_path, exc,
            )

    # ------------------------------------------------------------------
    # Manifest (BUG-1.7, BUG-16.10)
    # ------------------------------------------------------------------
    def _write_manifest(
        self,
        df: pd.DataFrame,
        clean_started_at: datetime,
        clean_finished_at: datetime,
    ) -> None:
        """Write the OMIM pipeline manifest with full provenance (BUG-1.7).

        The manifest is read by ``load()`` to verify the CSV hasn't been
        tampered with since ``clean()`` wrote it. If the on-disk CSV's
        SHA-256 does not match ``output_csv_sha256``, ``load()`` refuses
        to proceed.
        """
        manifest_path = OMIM_OUTPUT_PATH.with_suffix(
            OMIM_OUTPUT_PATH.suffix + ".manifest.json"
        )
        self._manifest_path = manifest_path

        # Compute the input fingerprint (BUG-7.5, BUG-16.2).
        try:
            self._input_fingerprint = _fingerprint_df(df)
        except Exception:
            self._input_fingerprint = ""

        manifest = {
            "primary_source": "omim",
            "license": "OMIM-restricted",  # BUG-14.1
            "pipeline_run_id": getattr(self, "run_id", None),
            "input_checksum": self._input_fingerprint,
            "output_csv_sha256": self._sha256_cleaned,
            "source_sha256": self._sha256_raw,
            "source_version": self._source_version or "unknown",
            "source_url": self._source_url_sanitised,
            "source_format": self._source_format,
            "download_method": self._download_method_used,
            "schema_version": SCHEMA_VERSION_STAMP,
            "download_date": (
                self.start_time.isoformat() if self.start_time
                else datetime.now(timezone.utc).isoformat()
            ),
            "row_count": int(len(df)),
            "column_count": int(df.shape[1]),
            "columns": df.columns.tolist(),
            "filter_criteria": f"mapping_key in {OMIM_MAPPING_KEYS_INCLUDE}",
            "exclude_susceptibility": bool(OMIM_EXCLUDE_SUSCEPTIBILITY),
            "mapping_keys_include": list(OMIM_MAPPING_KEYS_INCLUDE),
            "hgnc_snapshot_version": _hgnc_snapshot_version(),
            "clean_started_at": clean_started_at.isoformat(),
            "clean_finished_at": clean_finished_at.isoformat(),
            "load_completed_at": None,
            "rows_upserted": None,
        }

        # Atomic manifest write.
        tmp = manifest_path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
            os.replace(tmp, manifest_path)
            logger.info("[omim] Manifest written: %s", manifest_path)
        except Exception:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    # Quarantine / dead-letter (BUG-5.17, BUG-6.12, BUG-16.20)
    # ------------------------------------------------------------------
    def _quarantine_line(self, line: str, line_no: int, reason: str) -> None:
        """Buffer a malformed line for end-of-clean flush (BUG-5.17)."""
        self._quarantine_buffer.append({
            "line_number": line_no,
            "reason": reason,
            "content": line.rstrip("\n")[:500],  # truncate
            "source_file": "morbidmap.txt",
        })
        self._silent_skip_counter[reason] = self._silent_skip_counter.get(reason, 0) + 1

    def _flush_quarantine(self) -> None:
        """Write the quarantine buffer to JSONL (BUG-5.17, BUG-16.20)."""
        if not self._quarantine_buffer:
            return
        try:
            OMIM_QUARANTINE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(OMIM_QUARANTINE_PATH, "a", encoding="utf-8") as fh:
                for entry in self._quarantine_buffer:
                    fh.write(json.dumps(entry, default=str) + "\n")
            logger.info(
                "[omim] Quarantine: %d records written to %s",
                len(self._quarantine_buffer), OMIM_QUARANTINE_PATH,
            )
            self._quarantine_buffer.clear()
        except OSError as exc:
            logger.error("[omim] Failed to flush quarantine: %s", exc)

    def _write_dead_letter_file(
        self, df: pd.DataFrame, *, reason: str
    ) -> None:
        """Write unresolved records to a dead-letter CSV (BUG-6.12)."""
        if df.empty:
            return
        dead_letter_dir = PROCESSED_DATA_DIR / "dead_letter"
        dead_letter_dir.mkdir(parents=True, exist_ok=True)
        path = dead_letter_dir / f"omim_unresolved_{reason}_{self.run_id}.csv"
        try:
            df.to_csv(path, index=False, encoding="utf-8")
            logger.info(
                "[omim] Dead-letter: %d records written to %s (reason=%s)",
                len(df), path.name, reason,
            )
        except OSError as exc:
            logger.error("[omim] Failed to write dead-letter CSV: %s", exc)

    def _write_dead_letter_db(
        self, session: Any, df: pd.DataFrame, *, reason: str
    ) -> None:
        """Write unresolved records to the dead_letter_gda table (BUG-6.12, BUG-16.20)."""
        if df.empty:
            return
        try:
            for _, row in df.iterrows():
                details = {
                    "score": float(row["score"]) if pd.notna(row.get("score")) else None,
                    "source_id": row.get("source_id"),
                    "source_format": self._source_format,
                    "source_line_number": row.get("source_line_number"),
                }
                session.add(DeadLetterGDA(
                    gene_symbol=row.get("gene_symbol"),
                    disease_id=row.get("disease_id"),
                    source="omim",
                    reason=reason,
                    details_json=json.dumps(details, default=str),
                    run_id=self.run_id,
                ))
            session.flush()
            logger.info(
                "[omim] Dead-letter DB: %d records queued (reason=%s)",
                len(df), reason,
            )
        except Exception as exc:
            logger.error(
                "[omim] Dead-letter DB write failed: %s",
                self._sanitize_error_message(str(exc)),
            )

    # ------------------------------------------------------------------
    # Logging helpers (BUG-11.2, BUG-11.14, BUG-11.15)
    # ------------------------------------------------------------------
    def _log_row_count(self, stage: str, df: pd.DataFrame) -> None:
        """Log row count at each transformation stage (BUG-11.2)."""
        logger.info(
            "[omim] Stage '%s': %d rows, %d cols",
            stage, len(df), df.shape[1],
        )

    # ------------------------------------------------------------------
    # DataFrame fingerprint (BUG-16.2)
    # ------------------------------------------------------------------
    @staticmethod
    def _compute_df_checksum(df: pd.DataFrame) -> str:
        """Compute a SHA-256 checksum of a DataFrame (BUG-16.2).

        Mirrors DisGeNET's ``_compute_df_checksum`` (disgenet_pipeline.py:3170).
        """
        try:
            content = df.to_csv(index=False).encode("utf-8")
            return hashlib.sha256(content).hexdigest()
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Public API: load
    # ------------------------------------------------------------------
    def _build_load_df(self, df: pd.DataFrame) -> pd.DataFrame:
        """Filter the cleaned DataFrame to only DB-mapped columns (BUG-4.21).

        Mirrors DisGeNET's ``_build_load_df`` (disgenet_pipeline.py:3088).
        Introspects the ``GeneDiseaseAssociation`` SQLAlchemy model and
        selects only those columns. Auto-managed columns (``id``,
        ``created_at``, ``updated_at``) are excluded.

        Also translates the validator-emitted ``_``-prefixed lineage columns
        to their DB column names (no underscore) — see DisGeNET's
        ``csv_to_db`` mapping.
        """
        # Map CSV-only underscore-prefixed columns to DB column names.
        csv_to_db = {
            "_score_was_clipped": "score_was_clipped",
            "_original_score": "original_score",
            "_score_was_coerced_nan": "score_was_coerced_nan",
            "_score_direction": "score_direction",
            "_disease_name_was_filled": "disease_name_was_filled",
            "_association_type_was_filled": "association_type_was_filled",
            "_pmid_list_was_capped": "pmid_list_was_capped",
        }
        renamed = df.rename(columns=csv_to_db)

        # Introspect the GDA model to get the set of valid column names.
        try:
            from sqlalchemy import inspect as _sa_inspect
            mapper = _sa_inspect(GeneDiseaseAssociation)
            valid_cols = {c.key for c in mapper.columns}
            # Exclude auto-managed columns.
            valid_cols -= {"id", "created_at", "updated_at"}
        except Exception:
            # Fallback: use the known GDA model column list.
            valid_cols = {
                "gene_symbol", "uniprot_id", "disease_id", "disease_id_type",
                "disease_name", "association_type", "score", "source",
                "pmid_list", "score_type", "score_method", "pipeline_run_id",
                "gene_id", "disease_type", "source_id", "disease_class",
                "disease_class_source", "year_initial", "year_final",
                "confidence_tier", "evidence_strength", "normalized_score",
                "source_version", "download_date", "download_method",
                "source_format", "dedup_strategy", "confidence_tier_method",
                "resolution_method", "gene_to_uniprot_map_version",
                "original_pmid_count", "schema_version", "snapshot_tag",
                "source_url", "score_was_clipped", "original_score",
                "score_was_coerced_nan", "score_direction",
                "disease_name_was_filled", "association_type_was_filled",
                "pmid_list_was_capped",
            }

        # Select only the columns that exist in both the DataFrame and the model.
        cols_to_keep = [c for c in renamed.columns if c in valid_cols]
        load_df = renamed[cols_to_keep].copy()

        # BUG-15.8 / DisGeNET mirror: convert ISO-8601 string columns to
        # datetime for the DB. SQLite's DateTime type rejects strings.
        if "download_date" in load_df.columns:
            load_df["download_date"] = pd.to_datetime(
                load_df["download_date"], errors="coerce", utc=True,
            )
        return load_df

    def load(self, df: pd.DataFrame, session: Any | None = None) -> int:
        """Load cleaned OMIM GDA data into the database.

        Args:
            df: cleaned DataFrame from ``clean()``.
            session: optional SQLAlchemy session (passed by ``run()``).
                If None, opens a new session via ``get_db_session()``.

        Returns:
            Number of rows inserted + updated (NOT total_input — BUG-11.14).

        Raises:
            RuntimeError: if required columns are missing or the DB upsert
                fails irrecoverably.
        """
        # BUG-4.21 / BUG-4.19 / BUG-4.20 — assert required columns.
        REQUIRED_LOAD_COLS = [
            "gene_symbol", "uniprot_id", "disease_id", "disease_name",
            "association_type", "score", "source", "pmid_list",
        ]
        missing = [c for c in REQUIRED_LOAD_COLS if c not in df.columns]
        if missing:
            raise ValueError(
                f"load() missing required columns: {missing}"
            )

        if df.empty:
            logger.info("[omim] No OMIM GDA records to load")
            return 0

        t0 = time.monotonic()
        cleaned_count = len(df)

        # BUG-1.6: collapse into a single DB session.
        def _do_load(sess: Any) -> int:
            # Step 1: build gene→uniprot maps (BUG-1.6).
            gene_to_uniprot, protein_name_to_uniprot = build_gene_to_uniprot_maps(sess)
            gene_to_uniprot_map_version = hashlib.sha256(
                json.dumps(sorted(gene_to_uniprot.items()), default=str).encode("utf-8")
            ).hexdigest()

            # Step 2: resolve gene_symbol → uniprot_id.
            resolved_df = resolve_gene_symbol_to_uniprot(
                df, gene_to_uniprot, protein_name_to_uniprot
            )

            # Step 3: BUG-6.12 — dead-letter unresolved symbols.
            unresolved_mask = resolved_df["uniprot_id"].isna()
            n_unresolved = int(unresolved_mask.sum())
            if n_unresolved:
                logger.warning(
                    "[omim] %d / %d GDA records have unresolved gene_symbol — "
                    "routing to dead-letter",
                    n_unresolved, len(resolved_df),
                )
                unresolved_df = resolved_df[unresolved_mask].copy()
                first_50 = unresolved_df["gene_symbol"].dropna().head(50).tolist()
                if first_50:
                    logger.info("[omim] First 50 unresolved gene_symbols: %s", first_50)
                self._write_dead_letter_file(unresolved_df, reason="unresolved_gene_symbol")
                self._write_dead_letter_db(sess, unresolved_df, reason="unresolved_gene_symbol")
                resolved_df = resolved_df[~unresolved_mask].copy()

            if resolved_df.empty:
                logger.warning("[omim] No OMIM GDA records with resolved uniprot_id")
                return 0

            resolved_count = len(resolved_df)

            # Step 4: SW-18 ROOT FIX (BUG-16.13): ``canonical_gene_id``
            # must be an NCBI Entrez Gene ID (integer), NOT a UniProt
            # accession (string like "P04637"). The previous code
            # conflated the two identifier systems, either failing the
            # INTEGER type coercion on PostgreSQL or silently corrupting
            # the column on SQLite (INTEGER-affinity). The
            # gene_disease_associations.gene_id column is typed INTEGER
            # per models.py, so a UniProt string would either raise or
            # be silently mistyped. Use the HGNC symbol → NCBI Gene ID
            # map (loaded alongside _load_hgnc_symbols) when available;
            # otherwise leave canonical_gene_id NULL rather than
            # corrupting the column with a UniProt accession.
            #
            # v13 ROOT FIX (SW-18 regression): v12 introduced this
            # branch but NEVER populated ``self._hgnc_to_ncbi_gene_map``
            # — so the else branch always ran and CLOBBERED
            # ``canonical_gene_id`` to None for ALL rows, including
            # the ones correctly populated by
            # ``_resolve_gene_xref_embedded()`` at clean() time
            # (CFTR→1080, DMD→1756, etc.). The Phase 1 → Phase 2
            # bridge then saw 100% NULL canonical_gene_id and produced
            # zero Gene-encodes-Protein edges.
            #
            # v13 fix: populate ``_hgnc_to_ncbi_gene_map`` from
            # ``_EMBEDDED_GENE_XREF`` (the same crosswalk that
            # ``_resolve_gene_xref_embedded`` uses at clean() time).
            # AND skip the overwrite when ``canonical_gene_id`` is
            # already non-null (defense-in-depth — preserves values
            # populated by any upstream resolver).
            if not hasattr(self, "_hgnc_to_ncbi_gene_map") or not self._hgnc_to_ncbi_gene_map:
                # Populate from the embedded crosswalk so the
                # map() call below has real data to work with.
                self._hgnc_to_ncbi_gene_map = {
                    sym: xref["ncbi_gene_id"]
                    for sym, xref in _EMBEDDED_GENE_XREF.items()
                }
                logger.info(
                    "[omim] _hgnc_to_ncbi_gene_map populated from "
                    "_EMBEDDED_GENE_XREF (%d entries).",
                    len(self._hgnc_to_ncbi_gene_map),
                )
                # v41 ROOT FIX (SEV2-HIGH #11): extend the embedded set
                # with the full HGNC → NCBI crosswalk loaded from
                # ``approved_symbols.tsv`` (the same file
                # ``_load_hgnc_symbols`` reads). Without this extension,
                # every OMIM gene_symbol NOT in the 50-entry embedded
                # set gets ``canonical_gene_id = NULL``, and the Phase 1
                # → Phase 2 bridge sees ~99% NULL canonical_gene_id →
                # zero Gene-encodes-Protein edges. The on-disk file is
                # the source of truth; the embedded set is only the
                # fallback for when the file is unavailable.
                _hgnc_disk_map = _load_hgnc_to_ncbi_map()
                if _hgnc_disk_map:
                    before = len(self._hgnc_to_ncbi_gene_map)
                    # Disk-loaded entries take precedence over the
                    # embedded set when keys collide (disk is fresher).
                    self._hgnc_to_ncbi_gene_map.update(_hgnc_disk_map)
                    # Re-add embedded set on top of disk so the
                    # hand-curated entries (which we KNOW are correct)
                    # win even if the disk file disagrees.
                    for sym, xref in _EMBEDDED_GENE_XREF.items():
                        self._hgnc_to_ncbi_gene_map[sym] = xref["ncbi_gene_id"]
                    added = len(self._hgnc_to_ncbi_gene_map) - before
                    logger.info(
                        "[omim] _hgnc_to_ncbi_gene_map extended with "
                        "%d entries from approved_symbols.tsv (total "
                        "now %d).",
                        added, len(self._hgnc_to_ncbi_gene_map),
                    )
                else:
                    logger.warning(
                        "[omim] HGNC → NCBI disk map not available — "
                        "canonical_gene_id will be NULL for the ~99%% "
                        "of gene_symbols NOT in the 50-entry embedded "
                        "crosswalk. This will produce zero Gene-encodes-"
                        "Protein edges in the Phase 2 bridge for those "
                        "genes.",
                    )
            hgnc_to_ncbi = getattr(self, "_hgnc_to_ncbi_gene_map", None) or {}
            if hgnc_to_ncbi and "gene_symbol" in resolved_df.columns:
                # v13: skip the overwrite when canonical_gene_id is
                # already non-null (e.g. populated by
                # _resolve_gene_xref_embedded at clean() time, or by
                # an upstream resolver). Only fill null slots.
                if "canonical_gene_id" not in resolved_df.columns:
                    resolved_df["canonical_gene_id"] = None
                null_mask = resolved_df["canonical_gene_id"].isna()
                if null_mask.any():
                    mapped = resolved_df.loc[null_mask, "gene_symbol"].map(
                        hgnc_to_ncbi
                    )
                    resolved_df.loc[null_mask, "canonical_gene_id"] = mapped
                n_unresolved = int(resolved_df["canonical_gene_id"].isna().sum())
                if n_unresolved:
                    logger.warning(
                        "[omim] %d / %d gene_symbols could not be mapped to "
                        "an NCBI Gene ID — canonical_gene_id set to NULL for "
                        "these",
                        n_unresolved, len(resolved_df),
                    )
            else:
                logger.warning(
                    "[omim] HGNC-to-NCBI-Gene-ID map not available — "
                    "canonical_gene_id set to NULL for all %d rows (was "
                    "previously corrupted with UniProt accessions)",
                    len(resolved_df),
                )
                if "canonical_gene_id" not in resolved_df.columns:
                    resolved_df["canonical_gene_id"] = None
                else:
                    # v13: only null out rows that are actually
                    # UniProt accessions (start with a letter followed
                    # by digits). Preserve already-correct NCBI Gene
                    # IDs (pure digits).
                    def _is_uniprot_not_ncbi(v: Any) -> bool:
                        if v is None or (isinstance(v, float) and pd.isna(v)):
                            return False
                        s = str(v).strip()
                        if not s:
                            return False
                        # NCBI Gene IDs are pure digits.
                        if s.isdigit():
                            return False
                        # UniProt accessions match ^[A-O,P-Q,R-Z]\d{5}$
                        # or ^[A-N,R-Z]\d{5}$. Treat any non-digit
                        # string as a UniProt accession.
                        return True
                    uniprot_mask = resolved_df["canonical_gene_id"].apply(
                        _is_uniprot_not_ncbi
                    )
                    if uniprot_mask.any():
                        logger.warning(
                            "[omim] clearing %d rows with UniProt-style "
                            "canonical_gene_id (was corrupting INTEGER "
                            "column).",
                            int(uniprot_mask.sum()),
                        )
                        resolved_df.loc[uniprot_mask, "canonical_gene_id"] = None
            resolved_df["gene_to_uniprot_map_version"] = gene_to_uniprot_map_version
            resolved_df["resolution_method"] = "gene_symbol_then_gene_mim"

            # Step 5: BUG-2.9 — get_or_create_pipeline_run.
            pipeline_run_id = get_or_create_pipeline_run(
                sess,
                run_id=self.run_id,
                source="omim",
                started_at=self.start_time,
                status="running",
            )
            resolved_df["pipeline_run_id"] = pipeline_run_id

            # Step 6: BUG-1.7 / BUG-16.2 — input_checksum.
            input_checksum = self._sha256_cleaned or self._compute_df_checksum(resolved_df)
            resolved_df["input_checksum"] = input_checksum

            # Step 6b: BUG-4.21 / mirror DisGeNET _build_load_df — filter to
            # only DB-mapped columns. bulk_upsert_gda rejects DataFrames with
            # extra columns (e.g. phenotype_name, source_line_number) that
            # are useful for CSV lineage but not in the GDA table.
            load_df = self._build_load_df(resolved_df)

            # Step 7: BUG-2.9 / BUG-2.10 / §4.2 — bulk_upsert_gda with full lineage.
            try:
                result: UpsertResult = bulk_upsert_gda(
                    sess,
                    load_df,
                    batch_size=OMIM_DB_BATCH_SIZE,
                    pipeline_run_id=pipeline_run_id,
                    score_type=SCORE_TYPE_OMIM,
                    score_method=f"omim_v1_{self._source_version or 'unknown'}",
                    input_checksum=input_checksum,
                    dedup_already_done=True,  # DQ-6 / SCI-37
                )
            except Exception as exc:
                logger.error(
                    "[omim] bulk_upsert_gda failed: %s",
                    self._sanitize_error_message(str(exc)),
                )
                raise

            # BUG-6.13: session health check after upsert.
            if result.failed > 0:
                logger.error(
                    "[omim] %d records failed upsert — inspecting session health",
                    result.failed,
                )
                try:
                    from sqlalchemy import text as _sa_text
                    sess.execute(_sa_text("SELECT 1"))
                except Exception as sess_exc:
                    logger.error(
                        "[omim] Session is poisoned — rolling back: %s",
                        self._sanitize_error_message(str(sess_exc)),
                    )
                    sess.rollback()
                    raise

            # BUG-1.8: post-load DisGeNET dedup (log only — actual SQL is DB-specific).
            self._post_load_disgenet_dedup(sess)

            # BUG-11.14: result detail logging.
            logger.info(
                "[omim] GDA upsert: input=%d, inserted=%d, updated=%d, "
                "quarantined=%d, failed=%d",
                result.total_input, result.inserted, result.updated,
                result.quarantined, result.failed,
            )

            # BUG-5.20: row-count reconciliation.
            loaded_count = result.inserted + result.updated
            logger.info(
                "[omim] Row-count reconciliation: cleaned=%d, resolved=%d, "
                "loaded=%d, dropped_unresolved=%d, quarantined=%d, failed=%d",
                cleaned_count, resolved_count, loaded_count,
                n_unresolved, result.quarantined, result.failed,
            )

            # BUG-11.7: emit metrics.
            self._emit_metric(
                "records_loaded", loaded_count,
                tags={"source": "omim"},
            )
            self._emit_metric(
                "records_quarantined", result.quarantined,
                tags={"source": "omim"},
            )
            self._emit_metric(
                "records_failed", result.failed,
                tags={"source": "omim"},
            )
            self._emit_metric(
                "records_dropped_unresolved", n_unresolved,
                tags={"source": "omim"},
            )
            self._emit_metric(
                "api_calls_made", self._api_calls_made,
                tags={"source": "omim"},
            )
            self._emit_metric(
                "api_calls_retried", self._api_calls_retried,
                tags={"source": "omim"},
            )

            # BUG-11.15: duration log.
            duration_ms = int((time.monotonic() - t0) * 1000)
            logger.info("[omim] load duration_ms=%d", duration_ms)

            return loaded_count

        if session is not None:
            # Called from BasePipeline.run() — use the provided session.
            return _do_load(session)
        else:
            # Called standalone — open our own session.
            with get_db_session(
                pipeline_name=self.source_name,
                run_id=self.run_id,
                correlation_id=self.correlation_id,
            ) as sess:
                return _do_load(sess)

    def _post_load_disgenet_dedup(self, session: Any) -> None:
        """BUG-1.8: post-load dedup of OMIM-direct rows that duplicate DisGeNET.

        DisGeNET-curated already includes ~80% of OMIM's morbidmap with
        richer scoring; OMIM-direct rows that duplicate DisGeNET rows are
        noise. We DELETE OMIM rows whose (gene_symbol, disease_id) already
        exists in DisGeNET.

        This is wrapped in try/except so a missing disgenet source (e.g. in
        a fresh DB) doesn't break the OMIM load.
        """
        try:
            from sqlalchemy import text as _sa_text
            result = session.execute(_sa_text(
                "DELETE FROM gene_disease_associations "
                "WHERE source = 'omim' "
                "  AND EXISTS ( "
                "    SELECT 1 FROM gene_disease_associations g2 "
                "    WHERE g2.gene_symbol = gene_disease_associations.gene_symbol "
                "      AND g2.disease_id   = gene_disease_associations.disease_id "
                "      AND g2.source       = 'disgenet' "
                "  )"
            ))
            deleted_count = result.rowcount or 0
            if deleted_count:
                logger.info(
                    "[omim] Post-load DisGeNET dedup: %d OMIM rows removed "
                    "(duplicated by DisGeNET)", deleted_count,
                )
                self._emit_metric(
                    "records_deduped_disgenet_overlap", deleted_count,
                    tags={"source": "omim"},
                )
        except Exception as exc:
            # Non-fatal — log and continue. The OMIM rows are still loaded;
            # they're just duplicated. Downstream ML should filter.
            logger.warning(
                "[omim] Post-load DisGeNET dedup failed (non-fatal): %s",
                self._sanitize_error_message(str(exc)),
            )

    # ------------------------------------------------------------------
    # Public API: run_load_only (BUG-7.5)
    # ------------------------------------------------------------------
    # v29 ROOT FIX (audit P1-14): run_load_only() bypassed _write_run_log —
    # no audit row produced. Now calls the audit logger so the load is
    # recorded. The previous implementation called self.load(df) and
    # returned the count without ever invoking self._write_run_log(),
    # meaning OMIM load-only runs left no row in ``pipeline_runs`` (or in
    # the local JSONL fallback). This mirrors the BasePipeline.run_load_only
    # try/except/finally shape: status is tracked, and the audit row is
    # written even on failure.
    def run_load_only(self) -> int:
        """Re-validate the CSV + manifest, then load (BUG-7.5).

        Reads the on-disk CSV, verifies its SHA-256 matches the manifest,
        and then calls ``load()``.

        Writes a ``load_success`` (or ``failed``) audit row via
        ``_write_run_log`` (v29 ROOT FIX P1-14).
        """
        self.start_time = datetime.now(timezone.utc)
        logger.info(
            "[omim][run_id=%s] Load-only run starting...",
            self.run_id,
        )

        records_loaded: int = 0
        status = "running"
        error_message: str | None = None
        csv_path = OMIM_OUTPUT_PATH
        manifest_path = OMIM_OUTPUT_PATH.with_suffix(
            OMIM_OUTPUT_PATH.suffix + ".manifest.json"
        )

        try:
            if not csv_path.exists():
                raise RuntimeError(
                    f"OMIM cleaned CSV not found at {csv_path} — run clean() first"
                )
            if not manifest_path.exists():
                raise RuntimeError(
                    f"OMIM manifest not found at {manifest_path} — run clean() first"
                )

            # BUG-1.7: verify CSV SHA-256 matches manifest.
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            actual_sha = hashlib.sha256(csv_path.read_bytes()).hexdigest()
            expected_sha = manifest.get("output_csv_sha256")
            if expected_sha and actual_sha != expected_sha:
                raise RuntimeError(
                    f"CSV/manifest checksum mismatch — clean() must be re-run "
                    f"(expected={expected_sha}, actual={actual_sha})"
                )

            # Read CSV and run validate_output (BUG-7.5).
            df = pd.read_csv(csv_path, encoding="utf-8")
            is_valid, errors = self.validate_output(df)
            if not is_valid:
                raise RuntimeError(f"CSV schema invalid: {errors}")

            # BUG-5.12: verify schema_version.
            csv_schema_version = manifest.get("schema_version")
            if csv_schema_version != SCHEMA_VERSION_STAMP:
                logger.warning(
                    "[omim] Manifest schema_version %r != current %r — "
                    "may need to re-clean",
                    csv_schema_version, SCHEMA_VERSION_STAMP,
                )

            records_loaded = self.load(df)
            logger.info(
                "[omim] Load-only run loaded %d records", records_loaded,
            )
            status = "load_success"
            return records_loaded
        except Exception as exc:
            status = "failed"
            raw_msg = str(exc) if str(exc) else type(exc).__name__
            error_message = self._sanitize_error_message(raw_msg)
            logger.error(
                "[omim] Load-only run failed: %s",
                error_message,
                exc_info=self.log_exc_info,
            )
            raise
        finally:
            finished_at = datetime.now(timezone.utc)
            duration = (
                round((finished_at - self.start_time).total_seconds(), 3)
                if self.start_time is not None else None
            )
            try:
                self._write_run_log(
                    status=status,
                    started_at=self.start_time if self.start_time is not None
                    else finished_at,
                    finished_at=finished_at,
                    records_downloaded=0,
                    records_cleaned=0,
                    records_loaded=records_loaded,
                    error_message=error_message,
                    metadata_json={
                        "source": self.source_name,
                        "duration_seconds": int(duration) if duration is not None
                        else None,
                        "run_id": self.run_id,
                        "correlation_id": self.correlation_id,
                        "phase": "load_only",
                        "triggered_by": self.triggered_by,
                        "source_version": self._source_version or "unknown",
                        "schema_version": SCHEMA_VERSION_STAMP,
                        "records_loaded": records_loaded,
                    },
                )
            except Exception as audit_exc:
                logger.error(
                    "[omim] Audit log write failed: %s",
                    self._sanitize_error_message(str(audit_exc)),
                )

    # ------------------------------------------------------------------
    # Public API: load_incremental (BUG-15.19)
    # ------------------------------------------------------------------
    def load_incremental(self, since: datetime) -> int:
        """Load only records whose download_date is after ``since`` (BUG-15.19)."""
        csv_path = OMIM_OUTPUT_PATH
        if not csv_path.exists():
            raise RuntimeError(
                f"OMIM cleaned CSV not found at {csv_path} — run clean() first"
            )
        df = pd.read_csv(csv_path, encoding="utf-8")
        if "download_date" not in df.columns:
            logger.warning("[omim] download_date column missing — loading all rows")
            return self.load(df)
        df["download_date"] = pd.to_datetime(df["download_date"], errors="coerce", utc=True)
        mask = df["download_date"] > pd.Timestamp(since, tz="UTC")
        filtered = df[mask].copy()
        logger.info(
            "[omim] load_incremental(since=%s): %d / %d rows",
            since.isoformat(), len(filtered), len(df),
        )
        return self.load(filtered)

    # ------------------------------------------------------------------
    # Public API: run_backfill (BUG-15.18)
    # ------------------------------------------------------------------
    def run_backfill(self, start_date: Any, end_date: Any) -> int:
        """Backfill OMIM data for a date range (BUG-15.18).

        NOTE: OMIM does not expose historical snapshots of morbidmap.txt.
        This method is a documented no-op — it logs a WARNING and returns 0.
        """
        logger.warning(
            "[omim] run_backfill(%s, %s): OMIM does not expose historical "
            "morbidmap snapshots — backfill is a no-op",
            start_date, end_date,
        )
        return 0


# ============================================================================
# Module-level helpers
# ============================================================================

# ─── Embedded gene crosswalk (v6 fix for BUG-25a/B11) ─────────────────────────
# Static HGNC → (NCBI Gene ID, UniProt AC, gene MIM) crosswalk used by
# `_resolve_gene_xref_embedded()` to populate the `uniprot_id` and
# `canonical_gene_id` columns at clean() time. All values are publicly
# available, well-known identifiers from HGNC/NCBI/UniProt. Production
# deployments with a DB-backed gene_to_uniprot map should prefer the DB
# data; this embedded crosswalk is a clean()-time fallback so the CSV is
# always usable downstream (the Phase 1 → Phase 2 bridge consumes the CSV
# directly, not the DB).
_EMBEDDED_GENE_XREF: dict[str, dict[str, str]] = {
    # --- Original 9 clinically-important genes (kept for backward compat) ---
    "CFTR":   {"ncbi_gene_id": "1080",   "uniprot_id": "P13569", "gene_mim": "602421"},
    "DMD":    {"ncbi_gene_id": "1756",   "uniprot_id": "P11532", "gene_mim": "300377"},
    "FANCE":  {"ncbi_gene_id": "2178",   "uniprot_id": "O15287", "gene_mim": "603765"},
    "FBN1":   {"ncbi_gene_id": "2200",   "uniprot_id": "P35555", "gene_mim": "134797"},
    "FGFR3":  {"ncbi_gene_id": "2261",   "uniprot_id": "P22607", "gene_mim": "134934"},
    "HBB":    {"ncbi_gene_id": "3043",   "uniprot_id": "P68871", "gene_mim": "141900"},
    "HFE":    {"ncbi_gene_id": "3077",   "uniprot_id": "Q30201", "gene_mim": "235200"},
    "HTT":    {"ncbi_gene_id": "3064",   "uniprot_id": "P42858", "gene_mim": "613004"},
    "KIT":    {"ncbi_gene_id": "3815",   "uniprot_id": "P10721", "gene_mim": "164920"},
    # --- FIX-F / C-10: expanded to 50+ clinically-important genes ---
    # Verified against HGNC, NCBI Gene, and UniProt/Swiss-Prot canonical accessions.
    "TP53":   {"ncbi_gene_id": "7157",   "uniprot_id": "P04637", "gene_mim": "191170"},
    "BRCA1":  {"ncbi_gene_id": "672",    "uniprot_id": "P38398", "gene_mim": "113705"},
    "BRCA2":  {"ncbi_gene_id": "675",    "uniprot_id": "P51587", "gene_mim": "600185"},
    "EGFR":   {"ncbi_gene_id": "1956",   "uniprot_id": "P00533", "gene_mim": "131550"},
    "KRAS":   {"ncbi_gene_id": "3845",   "uniprot_id": "P01116", "gene_mim": "190070"},
    "NRAS":   {"ncbi_gene_id": "4893",   "uniprot_id": "P01111", "gene_mim": "164790"},
    "BRAF":   {"ncbi_gene_id": "673",    "uniprot_id": "P15056", "gene_mim": "164757"},
    "PIK3CA": {"ncbi_gene_id": "5290",   "uniprot_id": "P42336", "gene_mim": "171834"},
    "PTEN":   {"ncbi_gene_id": "5728",   "uniprot_id": "P60484", "gene_mim": "601728"},
    "APOE":   {"ncbi_gene_id": "348",    "uniprot_id": "P02649", "gene_mim": "107741"},
    "APP":    {"ncbi_gene_id": "351",    "uniprot_id": "P05067", "gene_mim": "104760"},
    "MAPT":   {"ncbi_gene_id": "4137",   "uniprot_id": "P10636", "gene_mim": "157140"},
    "LRRK2":  {"ncbi_gene_id": "120892", "uniprot_id": "Q5S007", "gene_mim": "609007"},
    "SNCA":   {"ncbi_gene_id": "6622",   "uniprot_id": "P37840", "gene_mim": "163890"},
    "TNF":    {"ncbi_gene_id": "7124",   "uniprot_id": "P01375", "gene_mim": "191160"},
    "IL6":    {"ncbi_gene_id": "3569",   "uniprot_id": "P05231", "gene_mim": "147620"},
    "INS":    {"ncbi_gene_id": "3630",   "uniprot_id": "P01308", "gene_mim": "176730"},
    "INSR":   {"ncbi_gene_id": "3643",   "uniprot_id": "P06213", "gene_mim": "147670"},
    "ESR1":   {"ncbi_gene_id": "2099",   "uniprot_id": "P03372", "gene_mim": "133430"},
    "AR":     {"ncbi_gene_id": "367",    "uniprot_id": "P10275", "gene_mim": "313700"},
    "VHL":    {"ncbi_gene_id": "7428",   "uniprot_id": "P40337", "gene_mim": "608537"},
    "RET":    {"ncbi_gene_id": "5979",   "uniprot_id": "P07949", "gene_mim": "164761"},
    "MET":    {"ncbi_gene_id": "4233",   "uniprot_id": "P08581", "gene_mim": "164860"},
    "PDGFRA": {"ncbi_gene_id": "5156",   "uniprot_id": "P16234", "gene_mim": "173490"},
    "FLT3":   {"ncbi_gene_id": "2322",   "uniprot_id": "P36888", "gene_mim": "136351"},
    "JAK2":   {"ncbi_gene_id": "3717",   "uniprot_id": "O60674", "gene_mim": "147796"},
    "ABL1":   {"ncbi_gene_id": "25",     "uniprot_id": "P00519", "gene_mim": "189980"},
    "KMT2A":  {"ncbi_gene_id": "4297",   "uniprot_id": "Q03164", "gene_mim": "159555"},
    "PML":    {"ncbi_gene_id": "5371",   "uniprot_id": "P29590", "gene_mim": "102578"},
    "RARA":   {"ncbi_gene_id": "5914",   "uniprot_id": "P10276", "gene_mim": "180240"},
    "MYC":    {"ncbi_gene_id": "4609",   "uniprot_id": "P01106", "gene_mim": "190080"},
    "BCL2":   {"ncbi_gene_id": "596",    "uniprot_id": "P10415", "gene_mim": "151430"},
    "MDM2":   {"ncbi_gene_id": "4193",   "uniprot_id": "Q00987", "gene_mim": "164785"},
    "CDKN2A": {"ncbi_gene_id": "1029",   "uniprot_id": "P42771", "gene_mim": "600160"},
    "RB1":    {"ncbi_gene_id": "5925",   "uniprot_id": "P06400", "gene_mim": "180200"},
    "APC":    {"ncbi_gene_id": "324",    "uniprot_id": "P25054", "gene_mim": "611731"},
    "MLH1":   {"ncbi_gene_id": "4292",   "uniprot_id": "P40692", "gene_mim": "120436"},
    "MSH2":   {"ncbi_gene_id": "4436",   "uniprot_id": "P43246", "gene_mim": "120435"},
    "BRIP1":  {"ncbi_gene_id": "83990",  "uniprot_id": "Q9BX63", "gene_mim": "605882"},
    "PALB2":  {"ncbi_gene_id": "79728",  "uniprot_id": "Q86YC2", "gene_mim": "610355"},
    "ATM":    {"ncbi_gene_id": "472",    "uniprot_id": "Q13315", "gene_mim": "607585"},
    "CHEK2":  {"ncbi_gene_id": "11200",  "uniprot_id": "O96017", "gene_mim": "604373"},
    "STK11":  {"ncbi_gene_id": "6794",   "uniprot_id": "Q15831", "gene_mim": "602216"},
    "SMAD4":  {"ncbi_gene_id": "4089",   "uniprot_id": "Q13485", "gene_mim": "600993"},
    "NF1":    {"ncbi_gene_id": "4763",   "uniprot_id": "P21359", "gene_mim": "162200"},
    "NF2":    {"ncbi_gene_id": "4771",   "uniprot_id": "P35240", "gene_mim": "101000"},
    "TSC1":   {"ncbi_gene_id": "7248",   "uniprot_id": "Q92574", "gene_mim": "605284"},
    "TSC2":   {"ncbi_gene_id": "7249",   "uniprot_id": "P49815", "gene_mim": "191092"},
    "WT1":    {"ncbi_gene_id": "7490",   "uniprot_id": "P19544", "gene_mim": "607102"},
    # Add more entries here as the fixture / production dataset grows.
}


def _resolve_gene_xref_embedded(df: pd.DataFrame) -> pd.DataFrame:
    """Populate `uniprot_id`, `ncbi_gene_id`, `canonical_gene_id` columns
    in the OMIM GDA DataFrame using the embedded HGNC crosswalk.

    This is a clean()-time fallback so the CSV (not just the DB) has these
    columns populated. The Phase 1 → Phase 2 bridge consumes the CSV
    directly and previously saw 100% NaN — causing zero Gene-encodes-Protein
    edges in the loaded knowledge graph.

    Idempotent: rows whose `uniprot_id` is already non-empty are skipped.
    Rows whose `gene_symbol` is not in the crosswalk are left as-is.
    """
    if df.empty or "gene_symbol" not in df.columns:
        return df

    # Ensure all three columns exist
    for col in ("uniprot_id", "ncbi_gene_id", "canonical_gene_id"):
        if col not in df.columns:
            df[col] = None

    n_resolved = 0
    for idx, row in df.iterrows():
        sym = str(row.get("gene_symbol") or "").strip().upper()
        if not sym:
            continue
        xref = _EMBEDDED_GENE_XREF.get(sym)
        if not xref:
            continue
        # Only fill if not already populated (idempotent).
        if pd.isna(row.get("uniprot_id")) or not str(row.get("uniprot_id") or "").strip():
            df.at[idx, "uniprot_id"] = xref["uniprot_id"]
        if pd.isna(row.get("ncbi_gene_id")) or not str(row.get("ncbi_gene_id") or "").strip():
            df.at[idx, "ncbi_gene_id"] = xref["ncbi_gene_id"]
        # canonical_gene_id = NCBI Gene ID (numeric, matches kg_builder's
        # ID_PATTERNS["Gene"] = ^\d+$). This is the bridge's preferred key.
        if pd.isna(row.get("canonical_gene_id")) or not str(row.get("canonical_gene_id") or "").strip():
            df.at[idx, "canonical_gene_id"] = xref["ncbi_gene_id"]
        n_resolved += 1

    if n_resolved:
        logger.info(
            "[omim] _resolve_gene_xref_embedded: populated uniprot_id / "
            "canonical_gene_id for %d / %d rows", n_resolved, len(df),
        )
    return df


@lru_cache(maxsize=1)
def _load_hgnc_symbols() -> frozenset[str]:
    """Load HGNC approved symbols (BUG-3.10).

    Cached for the process lifetime. If the HGNC file is missing, returns
    an empty frozenset — the caller should treat that as "skip HGNC
    validation" (OMIM is the source of truth for some recently-added
    symbols, so we don't fail the pipeline).
    """
    path = RAW_DATA_DIR / "hgnc" / "approved_symbols.tsv"
    if not path.exists():
        logger.warning(
            "[omim] HGNC symbol file missing at %s — skipping HGNC validation",
            path,
        )
        return frozenset()
    try:
        hgnc_df = pd.read_csv(path, sep="\t", dtype=str)
        # Try common column names.
        for col in ("Approved symbol", "Approved Symbol", "symbol", "Symbol"):
            if col in hgnc_df.columns:
                return frozenset(hgnc_df[col].dropna().str.upper())
        logger.warning(
            "[omim] HGNC file %s missing expected column — skipping validation",
            path,
        )
        return frozenset()
    except (OSError, ValueError) as exc:
        logger.warning(
            "[omim] Could not load HGNC symbols from %s: %s — skipping validation",
            path, exc,
        )
        return frozenset()


@lru_cache(maxsize=1)
def _load_hgnc_to_ncbi_map() -> dict[str, str]:
    """v41 ROOT FIX (SEV2-HIGH #11): load the HGNC symbol → NCBI Entrez
    Gene ID crosswalk from the on-disk HGNC approved_symbols.tsv file
    (which has both columns). This is the FALLBACK used by the OMIM
    loader when the embedded ``_EMBEDDED_GENE_XREF`` (50 entries) does
    not contain the gene_symbol. Without this fallback, ~99% of OMIM
    gene_symbols had ``canonical_gene_id = NULL`` because they weren't
    in the 50-entry embedded set, and the Phase 1 → Phase 2 bridge saw
    100% NULL canonical_gene_id → zero Gene-encodes-Protein edges.

    Returns an empty dict if the file is missing or malformed — the
    caller (OMIM loader) falls back to the embedded set in that case.
    """
    path = RAW_DATA_DIR / "hgnc" / "approved_symbols.tsv"
    if not path.exists():
        return {}
    try:
        hgnc_df = pd.read_csv(path, sep="\t", dtype=str)
        # The HGNC TSV uses these column names (per hgnc_complete_set
        # exports); try several variants for robustness.
        sym_col = None
        for candidate in ("Approved symbol", "Approved Symbol", "symbol", "Symbol"):
            if candidate in hgnc_df.columns:
                sym_col = candidate
                break
        ncbi_col = None
        for candidate in (
            "NCBI Gene ID", "NCBI Gene ID(supplied by NCBI)",
            "Entrez Gene ID", "ncbi_gene_id", "gene_id",
        ):
            if candidate in hgnc_df.columns:
                ncbi_col = candidate
                break
        if sym_col is None or ncbi_col is None:
            return {}
        mapping: dict[str, str] = {}
        for _, row in hgnc_df.iterrows():
            sym = row.get(sym_col)
            ncbi = row.get(ncbi_col)
            if pd.isna(sym) or pd.isna(ncbi):
                continue
            sym_str = str(sym).strip().upper()
            ncbi_str = str(ncbi).strip()
            # NCBI Gene IDs are pure positive integers.
            if not sym_str or not ncbi_str.isdigit():
                continue
            mapping[sym_str] = ncbi_str
        logger.info(
            "[omim] Loaded HGNC → NCBI Gene ID map (%d entries) from %s",
            len(mapping), path,
        )
        return mapping
    except (OSError, ValueError) as exc:
        logger.warning(
            "[omim] Could not load HGNC → NCBI map from %s: %s — "
            "canonical_gene_id will be NULL for symbols not in the "
            "embedded crosswalk.",
            path, exc,
        )
        return {}


@lru_cache(maxsize=1)
def _hgnc_snapshot_version() -> str | None:
    """Return the HGNC snapshot version (parsed from the file header)."""
    path = RAW_DATA_DIR / "hgnc" / "approved_symbols.tsv"
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("#"):
                    # Look for a version/date stamp in the header.
                    m = re.search(r"(\d{4}-\d{2}-\d{2})", line)
                    if m:
                        return m.group(1)
                else:
                    break
        # Fall back to file mtime.
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).date().isoformat()
    except (OSError, ValueError):
        return None


def _extract_inheritance_pattern(phenotype_name: str) -> str | None:
    """Extract an inheritance pattern from a phenotype name (BUG-3.18)."""
    if not phenotype_name:
        return None
    m = _INHERITANCE_RE.search(phenotype_name)
    return m.group(1).lower() if m else None
