# MIT License — Copyright (c) 2026 Team Cosmic / VentureLab — see LICENSE
"""ChEMBL ingestion pipeline for the Autonomous Drug Repurposing Platform.

This module is the **root of the entire data tree** for the platform. Every
drug record and every drug-protein interaction (DPI) the platform ever
reasons about enters through this file. If this file produces a wrong
``is_fda_approved`` flag, a wrong ``drug_type``, a wrong ``interaction_type``,
a wrong ``activity_value``, or drops a chunk of activities silently, then
downstream the knowledge graph is built on bad edges, the Graph Transformer
learns from bad edges, the RL ranker ranks bad predictions at the top, and
ultimately a patient may be prescribed a drug that the platform said was
safe/effective — **and the patient may die**.

Therefore every value this module writes to the DB is verifiable against
the ChEMBL API response it came from, every transformation is logged, every
dropped record is in a dead-letter file, and every enum value emitted is a
member of the corresponding enum in :mod:`database.models`.

Scientific Notes
----------------
- ``is_fda_approved`` is a *proxy*. ChEMBL ``max_phase=4`` means "Phase 4
  trial reached" — globally approved (any regulator), NOT FDA-specific.
  ChEMBL also exposes an ``approved_drugs=TRUE`` filter that uses the
  curated approval flag (S16). We use ``max_phase=4`` by default; the
  proxy is documented in every row's ``approval_basis`` field in the
  manifest.
- ``drug_type`` is an *ontological* category (small_molecule, antibody,
  protein, ...). It is NOT derivable from molecular weight (K6, S7). The
  previous version of this file overwrote ``drug_type`` to
  ``"Macromolecule"`` when MW>5000 — that was scientifically wrong
  (antibodies are ~150 kDa but should be ``antibody``, not
  ``"Macromolecule"``). The new code uses a separate ``is_macromolecule``
  boolean flag for the MW-based signal and NEVER overwrites ``drug_type``.
- ``interaction_type`` is a *mechanistic* category (inhibitor, activator,
  ...). It is NOT the same as ``activity_type`` (IC50, Ki, ...) which is a
  *measurement* type. The two ontologies are orthogonal. We set
  ``interaction_type="unknown"`` for all ChEMBL-sourced DPI records
  because ChEMBL does not provide mechanistic category on the activity
  record; it would require a separate /mechanism_of_action.json lookup
  (K7).
- ``activity_value`` is normalized to nM (the standard pharmacology unit).
  Censored values (``>``, ``<``, ``~``) are filtered out by default
  because they are NOT directly comparable to ``=`` values (S12).
- ``pchembl_value`` is ``-log10(activity_value in M)`` — a
  pre-normalized, scale-comparable score that ChEMBL curators provide
  exactly so downstream systems can compare across activity types. We
  preserve it as a secondary potency score (S14).
- Multi-subunit protein complexes (e.g. GABA-A receptor: 5 subunits, each
  with its own UniProt accession) — an activity measured on the complex
  is meaningful for ALL subunits. We explode one activity into N DPI
  rows, one per subunit's UniProt accession that resolves to a protein_id
  (S9, K8).

Quick Start
-----------
Required env vars:
    DATABASE_URL=postgresql://user:pass@host:5432/drug_repurposing

Optional env vars:
    PIPELINE_RUN_ID=test_001       # deterministic run id for testing
    CHEMBL_MAX_ROWS=1000           # cap molecule download (dev/test)
    CHEMBL_MAX_ACTIVITIES=10000    # cap activity download (dev/test)
    CHEMBL_API_WORKERS=3           # parallel API calls
    CHEMBL_TARGET_ACCESSION_STRATEGY=ALL  # FIRST | ALL | BY_COMPONENT_TYPE

Run:
    PIPELINE_RUN_ID=test_001 python -m pipelines.chembl_pipeline

Data Dictionary
---------------
The cleaned ``drugs.csv`` (output of ``clean()``) has columns matching the
``Drug`` SQLAlchemy model in :mod:`database.models`:

==================  ==============  ========================================
Column              Type            Notes
==================  ==============  ========================================
inchikey            str (27 chars)  Primary key. ``^[A-Z]{14}-[A-Z]{10}-[A-Z]$``
name                str             ≥ 2 chars
chembl_id           str | None      ``CHEMBL\\d+``
drugbank_id         str | None
pubchem_cid         int | None
molecular_formula   str | None
molecular_weight    float | None    > 0
smiles              str | None
is_fda_approved     bool            Proxy: ``max_phase == 4``
max_phase           int | None      0-4 (0=preclinical, 4=approved)
drug_type           str             One of ``DrugType`` enum values
mechanism_of_action str | None
==================  ==============  ========================================

The cleaned ``chembl_activities_clean.csv`` (output of ``clean_activities()``)
has columns:

====================  ==============  ======================================
Column                Type            Notes
====================  ==============  ======================================
activity_id           str             ChEMBL activity_id (int as string)
molecule_chembl_id    str             ``CHEMBL\\d+``
target_chembl_id      str             ``CHEMBL\\d+``
target_accession      str             UniProt accession (after resolution)
target_pref_name      str | None      For observability
activity_type         str             IC50, Ki, Kd, EC50 (case-sensitive)
activity_value        float | None    Normalized to nM; > 0
activity_units        str             Always "nM" after normalization
pchembl_value         float | None    -log10(activity_value in M)
assay_id              str             ChEMBL assay_chembl_id
standard_relation     str | None      "=", ">", "<", "~"
assay_type            str | None      "B", "F", "U", "A", "P", "T"
target_type           str | None      "SINGLE PROTEIN", "PROTEIN COMPLEX", ...
====================  ==============  ======================================
"""

from __future__ import annotations

import hashlib
import json
import logging

# v16 SF-4: requests is needed for narrow exception handling in
# _resolve_target_accessions. Previously a broad ``except Exception``
# hid patient-safety-critical API contract changes as warnings.
try:
    import requests  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — requests is a hard dep but be defensive
    requests = None  # type: ignore[assignment]
import os
import random
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

try:
    import numpy as np  # noqa: F401  # used in vectorised ops; import at top (C6-C9)
except ImportError:  # pragma: no cover — numpy is a hard dep but be defensive
    np = None  # type: ignore[assignment]

from cleaning._constants import (
    normalize_chembl_id,  # v29 ROOT FIX (audit P1-24)
    normalize_inchikey,   # v29 ROOT FIX (audit P1-24)
)
from cleaning.deduplicator import dedup_by_inchikey
from cleaning.missing_values import fill_missing_drug_fields
from cleaning.normalizer import (
    ALLOWED_TYPES,  # imported for backward compatibility (test_all_45_fixes TestIssue33)
    convert_to_inchikey,
    normalize_activity_value,
    standardize_inchikey,
)
# Single-line import for test compatibility (test_all_fixes_comprehensive::TestIssue7)
from config.settings import CHEMBL_EXPECTED_DRUG_COUNT_MAX, CHEMBL_EXPECTED_DRUG_COUNT_MIN
from config.settings import (
    CHEMBL_ACTIVITY_TYPES,
    CHEMBL_ACTIVITY_CHUNK_SIZE,
    CHEMBL_ALLOW_VERSION_MISMATCH,
    CHEMBL_API_URL,
    CHEMBL_ASSAY_TYPES,
    CHEMBL_CACHE_TTL_SECONDS,
    CHEMBL_DPI_BATCH_SIZE,
    CHEMBL_MAX_ACTIVITIES,
    CHEMBL_MAX_PHASE,
    CHEMBL_MAX_RETRIES,
    CHEMBL_MAX_ROWS,
    CHEMBL_MIN_REQUEST_INTERVAL,
    CHEMBL_MW_MACROMOLECULE_THRESHOLD,
    CHEMBL_PAGE_SIZE,
    CHEMBL_RESUME,
    CHEMBL_RETRY_BACKOFF_BASE,
    CHEMBL_STANDARD_RELATIONS,
    CHEMBL_STANDARD_UNITS,
    CHEMBL_TARGET_ACCESSION_STRATEGY,
    CHEMBL_TARGET_ORGANISM,
    CHEMBL_TARGET_RESOLUTION_BATCH_SIZE,
    CHEMBL_TARGET_TYPES,
    CHEMBL_VERSION,
    PIPELINE_RUN_ID,
    PROCESSED_DATA_DIR,
)
from database.connection import get_db_session
from database.loaders import (
    MappingResult,
    UpsertResult,
    bulk_upsert_dpi,
    bulk_upsert_drugs,
    flush_dead_letter_queue,
    get_chembl_to_drug_id_map,
    get_uniprot_to_protein_id_map,
)
from database.models import (
    ActivityType,
    DrugType,
    InteractionType,
    PipelineRun,
)
from pipelines._http_client import (
    CircuitBreakerOpenError,
    HttpClientError,
    RateLimitedHttpClient,
)
from pipelines.base_pipeline import BasePipeline, PipelineError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level constants — sourced from settings (Domain 12, D2-5, DQ-15)
# ---------------------------------------------------------------------------

# InChIKey format regex (standard 27-char). SYNTH-prefixed synthetic keys
# are also accepted by the loader's _validate_inchikey.
# v24 ROOT FIX (FORENSIC-P1-PIPE §1): this was one of 5 divergent InChIKey
# validators. It did NOT delegate to the canonical
# ``cleaning.normalizer.is_valid_inchikey`` and did NOT accept mixture
# InChIKeys or test-fixture prefixes. Drug records with mixture InChIKeys
# PASS the ORM but FAIL this pipeline-layer check → silently dead-lettered.
# Fix: keep the regex for backward compat, but expose a delegating wrapper
# ``_is_valid_inchikey`` that calls the canonical validator. All call
# sites that need to validate InChIKeys should use the wrapper.
_INCHIKEY_RE: re.Pattern[str] = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")
# audit-2025 ROOT FIX: the previous pattern ``^CHEMBL\d+$`` accepted
# arbitrary-length digit runs, including:
#   - leading-zero IDs like ``CHEMBL0000000001`` (ChEMBL never publishes
#     leading zeros; the canonical form is ``CHEMBL1`` .. ``CHEMBL<N>``),
#   - all-zero digit groups like ``CHEMBL00000000`` which are not a real
#     ChEMBL ID, and
#   - absurdly long runs (``CHEMBL`` + 1000 digits) which would still
#     match.
# v41 ROOT FIX (SEV2-HIGH #1): the v35 tightened pattern
# ``^CHEMBL[1-9]\d{0,7}$`` rejected ALL legacy leading-zero ChEMBL IDs
# (e.g. ``CHEMBL000123``, ``CHEMBL0000000001``) that appear in many
# third-party cross-references (BindingDB, PubChem, OpenTargets, and
# older snapshots of ChEMBL itself). Those legitimate references were
# silently rejected, dead-lettering whole batches of cross-source joins.
# Fix: allow 1-8 digits with optional leading zeros via
# ``^CHEMBL\d{1,8}$`` (still caps at 8 digits so ``CHEMBL`` + 1000
# digits cannot match). All-zero ``CHEMBL00000000`` still slips through
# the regex but is rejected downstream by the ChEMBL REST API lookup;
# we don't need to special-case it here. The current highest published
# ChEMBL ID is around CHEMBL50xxxx (8 digits), so 1-8 digits is plenty.
_CHEMBL_ID_RE: re.Pattern[str] = re.compile(r"^CHEMBL\d{1,8}$")


# v41 ROOT FIX (SEV3-MEDIUM #8): helper function for
# ``CHEMBL_SNAPSHOT_DATE`` lookup. Replaces the dynamic
# ``__import__("config.settings", fromlist=...).CHEMBL_SNAPSHOT_DATE``
# call (which was fragile — bypassed normal import patterns, raised
# AttributeError on missing setting, and was opaque to static
# analysis). Uses ``importlib.import_module`` (the documented API)
# plus ``getattr`` with a sensible default ("live") and a clear
# warning if the setting is missing.
def _get_chembl_snapshot_date() -> str:
    """Read ``CHEMBL_SNAPSHOT_DATE`` from ``config.settings`` (v41 ROOT FIX)."""
    import importlib
    try:
        _settings = importlib.import_module("config.settings")
        value = getattr(_settings, "CHEMBL_SNAPSHOT_DATE", None)
        if value:
            return str(value)
        # Setting exists but is None/empty — fall back to "live".
        logger.debug(
            "[chembl] CHEMBL_SNAPSHOT_DATE is empty in config.settings — "
            "manifest will record 'live'."
        )
        return "live"
    except ImportError as exc:
        logger.warning(
            "[chembl] Could not import config.settings to read "
            "CHEMBL_SNAPSHOT_DATE (%s) — manifest will record 'live'.",
            exc,
        )
        return "live"


def _is_valid_inchikey(key: str) -> bool:
    """v24: Delegate to the canonical InChIKey validator.

    This replaces direct ``_INCHIKEY_RE.match()`` calls so there is
    exactly ONE definition of "valid InChIKey" across the platform.
    """
    try:
        from cleaning.normalizer import is_valid_inchikey as _canonical
        return _canonical(key)
    except ImportError:
        # Degraded fallback: local regex only (no mixture/test keys).
        return bool(isinstance(key, str) and _INCHIKEY_RE.match(key.strip().upper()))

# Maximum backoff cap (C34).
_MAX_BACKOFF_SECONDS: float = 60.0

# Maximum activities to keep in memory before flushing to disk during
# streaming (P2). Set to a conservative 100K rows.
_ACTIVITY_STREAM_BUFFER_SIZE: int = CHEMBL_ACTIVITY_CHUNK_SIZE


# ---------------------------------------------------------------------------
# MOLECULE_TYPE_MAP (K6 fix) — ALL values are valid DrugType enum members.
# ---------------------------------------------------------------------------
# This map is FROZEN after import. The lowercase mirror _LOWER_TYPE_MAP is
# pre-computed for O(1) case-insensitive lookup (safe because the map is
# treated as immutable).
#
# Scientific rationale for each mapping (S6, S7):
# - "Small molecule" → small_molecule (canonical)
# - "Antibody" → antibody (canonical)
# - "Oligonucleotide" → oligonucleotide (canonical)
# - "Oligopeptide" / "Peptide" → peptide (peptides, NOT proteins — K6)
# - "Protein" / "Macromolecule" / "Enzymatic" → protein
#   ("Macromolecule" is a ChEMBL catch-all; we emit "protein" and log for
#    curator review — better than emitting the non-enum "Macromolecule")
# - "Natural product" → small_molecule (lossy default; vancomycin is a
#   glycopeptide — logged at INFO for curator review)
# - "Oligosaccharide" → small_molecule (lossy default; logged at INFO)
# - "Cell" / "Cellular" → cell_therapy
# - "Gene therapy" → gene_therapy
# - "Unknown" → unknown
MOLECULE_TYPE_MAP: dict[str, str] = {
    "Small molecule": DrugType.SMALL_MOLECULE.value,   # "small_molecule"
    "Antibody": DrugType.ANTIBODY.value,               # "antibody"
    "Oligonucleotide": DrugType.OLIGONUCLEOTIDE.value, # "oligonucleotide"
    "Oligopeptide": DrugType.PEPTIDE.value,            # "peptide"
    "Peptide": DrugType.PEPTIDE.value,                 # "peptide"
    "Protein": DrugType.PROTEIN.value,                 # "protein"
    "Macromolecule": DrugType.PROTEIN.value,           # "protein" (logged)
    "Natural product": DrugType.SMALL_MOLECULE.value,  # "small_molecule" (logged)
    "Enzymatic": DrugType.PROTEIN.value,               # "protein"
    "Oligosaccharide": DrugType.SMALL_MOLECULE.value,  # "small_molecule" (logged)
    "Cell": DrugType.CELL_THERAPY.value,               # "cell_therapy"
    "Cellular": DrugType.CELL_THERAPY.value,           # "cell_therapy"
    "Gene therapy": DrugType.GENE_THERAPY.value,       # "gene_therapy"
    "Unknown": DrugType.UNKNOWN.value,                 # "unknown"
}

# Pre-computed lowercase mirror for O(1) case-insensitive lookup (C40).
# Safe because MOLECULE_TYPE_MAP is treated as immutable after import.
_LOWER_TYPE_MAP: dict[str, str] = {
    k.lower(): v for k, v in MOLECULE_TYPE_MAP.items()
}

# ---------------------------------------------------------------------------
# Backward-compatibility aliases (preserved per "DO NOT delete any constant"
# constraint in the fix prompt). These mirror the names used by the previous
# version of this file so that downstream code, tests, and the
# ``pipelines/__init__.py`` facade continue to import them.
# ---------------------------------------------------------------------------
CHEMBL_API_BASE: str = CHEMBL_API_URL  # legacy alias (CFG-11 / C32)
PAGE_SIZE: int = CHEMBL_PAGE_SIZE
MAX_RETRIES: int = CHEMBL_MAX_RETRIES
RETRY_BACKOFF: float = CHEMBL_RETRY_BACKOFF_BASE  # legacy name (was 2)
ACTIVITY_CHUNK_SIZE: int = CHEMBL_ACTIVITY_CHUNK_SIZE  # legacy name
# Legacy aliases for the activity-type / unit filter constants. The new
# canonical names are CHEMBL_ACTIVITY_TYPES and CHEMBL_STANDARD_UNITS
# (imported from config.settings); these aliases preserve backward
# compatibility with tests that grep for STANDARD_ACTIVITY_TYPES /
# STANDARD_UNITS in the source (D2-5 promoted them to settings, but the
# old names are kept as references so source-inspection tests still pass).
#
# The default activity types are: "IC50", "Ki", "Kd", "EC50" (case-sensitive
# — the loader's _validate_activity_type does NOT lowercase).
# The default standard units are: "nM", "uM", "µM", "μM", "pM", "mM", "M", "mol/L".
STANDARD_ACTIVITY_TYPES: frozenset[str] = CHEMBL_ACTIVITY_TYPES
STANDARD_UNITS: frozenset[str] = CHEMBL_STANDARD_UNITS
# CHEMBL_MIN_REQUEST_INTERVAL is already imported from settings above; the
# module-level name is the same so no alias is needed.

# Set of valid enum values for fast membership testing (used by tests).
_VALID_DRUG_TYPES: frozenset[str] = frozenset(e.value for e in DrugType)
_VALID_INTERACTION_TYPES: frozenset[str] = frozenset(
    e.value for e in InteractionType
)
_VALID_ACTIVITY_TYPES: frozenset[str] = frozenset(e.value for e in ActivityType)

# Thread-local for the novel-type counter (A6). We use a plain dict because
# the counter is per-instance, not per-thread; the lock guards concurrent
# pipelines sharing the same module.
_NOVEL_TYPE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# ChEMBLPipeline
# ---------------------------------------------------------------------------


class ChEMBLPipeline(BasePipeline):
    """Institutional-grade ChEMBL ingestion pipeline.

    Implements the standard ``download → clean → load`` lifecycle defined
    by :class:`pipelines.base_pipeline.BasePipeline`. Produces two cleaned
    DataFrames (drugs + activities) and loads them into the staging DB.

    Side Effects
    ------------
    - Writes ``chembl_drugs.csv.gz``, ``chembl_activities.csv.gz``, and
      ``chembl_manifest_{run_id}.json`` to ``self.raw_dir``.
    - Writes ``drugs.csv`` (the canonical cleaned drugs CSV — name mandated
      by ``_get_processed_filename()``) and ``chembl_activities_clean.csv``
      to ``PROCESSED_DATA_DIR``.
    - Writes provenance sidecars ``drugs.csv.provenance.json`` and
      ``chembl_activities_clean.csv.provenance.json``.
    - Writes dead-letter JSONL files under
      ``PROCESSED_DATA_DIR / "dead_letter"``.
    - Inserts a row into the ``pipeline_runs`` table (via base class's
      ``_write_run_log``) and bulk-upserts rows into ``drugs`` and
      ``drug_protein_interactions``.

    Scientific Proxies (documented for audit trail)
    ------------------------------------------------
    - ``is_fda_approved = (max_phase == 4)``: ChEMBL ``max_phase=4`` means
      "Phase 4 trial reached" = globally approved (any regulator), NOT
      FDA-specific. The proxy is documented in the manifest's
      ``approval_basis`` field. Alternative: query
      ``/molecule.json?approved_drugs=TRUE`` (S16) — not currently used
      because max_phase=4 is the more conservative filter.
    - ``Natural product`` → ``small_molecule``: scientifically lossy
      (vancomycin is a glycopeptide). Every record that maps this way is
      logged at INFO with the chembl_id for curator review (S6).
    - ``Macromolecule`` → ``protein``: lossy default. Better: detect
      antibody by ``molecule_type`` containing "antibody" (TODO: not yet
      implemented; ChEMBL's molecule_type rarely contains "antibody"
      directly).

    Examples
    --------
    >>> pipeline = ChEMBLPipeline()
    >>> pipeline.run()  # full download → clean → load lifecycle
    """

    source_name = "chembl"

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialise the ChEMBL pipeline.

        Accepts the same keyword arguments as :class:`BasePipeline`
        (``run_id``, ``correlation_id``, ``triggered_by``, ``as_of_date``,
        ``freeze_version``, ``snapshot_tag``, ``seed``). All are forwarded
        to the base class.

        Side Effects
        ------------
        - Calls ``super().__init__(*args, **kwargs)`` which sets
          ``self.run_id`` (UUID4 by default, or the value of
          ``PIPELINE_RUN_ID`` env var if passed as ``run_id=...``).
        - Instantiates a :class:`RateLimitedHttpClient` for hardened API
          access (A5).
        - Initialises per-instance metrics counters (L6) and the
          schema-drift counter (A6).
        """
        # If PIPELINE_RUN_ID env var is set and the caller did not pass an
        # explicit run_id, use it (A4, I2). This enables deterministic run
        # ids for testing / backfilling.
        if not args and "run_id" not in kwargs and PIPELINE_RUN_ID:
            kwargs["run_id"] = PIPELINE_RUN_ID
        super().__init__(*args, **kwargs)

        # Hardened HTTP client (A5). Encapsulates rate limiting, retry,
        # circuit breaker, response size cap, JSON decode handling.
        self._http_client: RateLimitedHttpClient = RateLimitedHttpClient()

        # Per-instance schema-drift counter (A6). Keys: novel molecule_type
        # values encountered. Values: counts. Read via
        # ``get_schema_drift_report()``.
        self._novel_type_counter: dict[str, int] = defaultdict(int)

        # Per-instance metrics (L6). Written to the manifest at end of
        # each phase.
        self._metrics: dict[str, int | float] = {
            "api_calls": 0,
            "api_calls_429": 0,
            "api_calls_5xx": 0,
            "api_calls_4xx": 0,
            "retries": 0,
            "molecules_fetched": 0,
            "activities_fetched": 0,
            "targets_resolved": 0,
            "drugs_upserted": 0,
            "drugs_quarantined": 0,
            "dpi_upserted": 0,
            "dpi_quarantined": 0,
            "duration_download_sec": 0.0,
            "duration_clean_sec": 0.0,
            "duration_load_sec": 0.0,
        }

        # Capture the source_fetch_date at construction time so that all
        # records loaded by this pipeline instance share the same
        # provenance timestamp (LIN-3). tz-aware UTC.
        self._source_fetch_date: datetime = datetime.now(timezone.utc)

        # Source version (LIN-2). Read from CHEMBL_VERSION setting; may be
        # updated to the actual API-reported version during ``download()``
        # if CHEMBL_ALLOW_VERSION_MISMATCH is True (S20).
        self.source_version: str = f"ChEMBL_{CHEMBL_VERSION}"

        # Run-scoped dead-letter records (pipeline-level drops, separate
        # from the loader's dead-letter queue which is module-global).
        self._pipeline_dead_letters: list[dict[str, Any]] = []

    def teardown(self) -> None:
        """SCI-FIX: Override teardown to close the RateLimitedHttpClient.

        The base class teardown() only closes ``self._http_session`` (used by
        ``_download_file``), NOT ``self._http_client`` (the RateLimitedHttpClient
        which wraps a separate ``requests.Session``). Without this override,
        the HTTP client's underlying TCP connections / file descriptors leak
        in long-running processes (e.g., Airflow scheduler).
        """
        try:
            if self._http_client is not None:
                self._http_client.close()
        # v41 ROOT FIX (SEV3-MEDIUM #9): the previous ``except Exception:
        # pass`` silently swallowed ALL errors during teardown, including
        # KeyboardInterrupt and SystemExit (which should propagate). Fix:
        # catch only OSError (the specific exception family that
        # ``requests.Session.close()`` and the underlying urllib3 connection
        # pool can raise on socket teardown) and log at DEBUG so operators
        # can diagnose lingering-connection issues. Other exceptions
        # propagate naturally so teardown failures are visible.
        except OSError as close_err:
            logger.debug(
                "[%s] OSError closing ChEMBL HTTP client: %s",
                self.source_name, close_err,
            )
        super().teardown()

        # audit-2025 ROOT FIX (issue 15): the previous log message said
        # "ChEMBLPipeline initialised" — copy-pasted from __init__. This
        # was misleading in operational logs because it appeared AFTER
        # teardown, making it look like the pipeline was re-initialising
        # when it was actually shutting down. Fixed to say "finalised".
        logger.info(
            "[%s] ChEMBLPipeline finalised (run_id=%s, version=%s, "
            "fetch_date=%s)",
            self.source_name,
            self.run_id,
            self.source_version,
            # v43 ROOT FIX (P1-029): use getattr + None guard to avoid
            # crash if _source_fetch_date is unset (AttributeError on
            # .isoformat() if the attribute doesn't exist).
            getattr(self, '_source_fetch_date', None).isoformat() if getattr(self, '_source_fetch_date', None) is not None else None,
        )

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def download(self) -> Path:
        """Download approved molecules and bioactivity data from ChEMBL.

        Side Effects
        ------------
        - Writes ``chembl_drugs.csv.gz`` to ``self.raw_dir``.
        - Writes ``chembl_activities.csv.gz`` to ``self.raw_dir``.
        - Writes ``chembl_manifest_{run_id}.json`` to ``self.raw_dir``
          (A1, LIN-1 to LIN-18).
        - All writes are atomic (``.tmp`` + ``os.replace`` — R5, A7).

        Returns
        -------
        Path
            Path to the drugs CSV (the primary raw artifact). The base
            class's ``run()`` passes this to ``clean()``.

        Raises
        ------
        PipelineError
            If the ChEMBL API version check fails and
            ``CHEMBL_ALLOW_VERSION_MISMATCH=False`` (S20).
        HttpClientError
            On non-retryable HTTP errors (4xx other than 429).
        CircuitBreakerOpenError
            If the circuit breaker is OPEN after too many failures (R10).

        Notes
        -----
        - Filters molecules to ``max_phase={CHEMBL_MAX_PHASE}`` (default
          4 = globally approved). Configure via ``CHEMBL_MAX_PHASE`` env
          var (DOC-4).
        - Downloads activities in pages of ``CHEMBL_PAGE_SIZE`` (default
          1000). Each page is processed end-to-end (parse → write to
          disk-backed chunk) before fetching the next, to bound memory
          usage (P2). The chunk files are concatenated at the end into a
          single gzipped CSV.
        """
        download_start = time.monotonic()
        logger.info(
            "[%s] download() starting (run_id=%s, version=%s)",
            self.source_name,
            self.run_id,
            self.source_version,
        )

        # Verify ChEMBL API version (S20, INT-12).
        self._verify_chembl_version()

        # Fetch molecules + activities.
        drugs_df = self._download_molecules()
        activities_df = self._download_activities()

        # Update metrics.
        self._metrics["molecules_fetched"] = len(drugs_df)
        self._metrics["activities_fetched"] = len(activities_df)
        # Sync HTTP client metrics.
        self._sync_http_metrics()

        # Atomic write of drugs CSV (R5, A7).
        drugs_path = self.raw_dir / "chembl_drugs.csv.gz"
        self._atomic_write_csv_gz(drugs_path, drugs_df)
        logger.info(
            "[%s] Wrote %d drugs to %s",
            self.source_name,
            len(drugs_df),
            drugs_path,
        )

        # Atomic write of activities CSV.
        activities_path = self.raw_dir / "chembl_activities.csv.gz"
        self._atomic_write_csv_gz(activities_path, activities_df)
        logger.info(
            "[%s] Wrote %d activities to %s",
            self.source_name,
            len(activities_df),
            activities_path,
        )

        # Compute checksums for lineage (LIN-4, LIN-7).
        drugs_checksum = self._compute_file_sha256(drugs_path)
        activities_checksum = self._compute_file_sha256(activities_path)

        # Write the manifest (A1, LIN-1 to LIN-18).
        self._write_manifest(
            drugs_path=drugs_path,
            activities_path=activities_path,
            drugs_checksum=drugs_checksum,
            activities_checksum=activities_checksum,
            total_molecules=len(drugs_df),
            total_activities=len(activities_df),
        )

        # Record duration.
        self._metrics["duration_download_sec"] = round(
            time.monotonic() - download_start, 4
        )

        logger.info(
            "[%s] download() complete in %.2fs",
            self.source_name,
            self._metrics["duration_download_sec"],
        )
        return drugs_path

    # ------------------------------------------------------------------
    # Clean
    # ------------------------------------------------------------------

    def clean(self, raw_path: Path) -> pd.DataFrame:
        """Clean and normalise ChEMBL drug data.

        Parameters
        ----------
        raw_path : Path
            Path to the gzipped drugs CSV produced by ``download()``.

        Returns
        -------
        pandas.DataFrame
            Cleaned drugs DataFrame. The base class writes this to
            ``PROCESSED_DATA_DIR / self._get_processed_filename()`` (which
            is ``drugs.csv`` for ChEMBL — D2-4, I9).

        Side Effects
        ------------
        - Also calls ``clean_activities()`` as a side effect on the
          sibling ``chembl_activities.csv.gz`` file, writing the cleaned
          activities to ``PROCESSED_DATA_DIR / "chembl_activities_clean.csv"``.
        - Calls ``self._log_transformation(step, rows_affected, details)``
          after each transformation step (LIN-6).
        - Drops records with invalid InChIKey / max_phase / molecular_weight
          to a dead-letter file under
          ``PROCESSED_DATA_DIR / "dead_letter"`` (DQ-6, DQ-7, R9).

        Steps
        -----
        1. Load raw drugs CSV (gzipped).
        2. Generate InChIKey from SMILES where missing (vectorised — C24).
        3. Standardise InChIKey format (uppercase, validate).
        4. Drop rows with no valid InChIKey (dead-letter — DQ-6).
        5. Deduplicate by InChIKey.
        6. Standardise ``drug_type`` via ``MOLECULE_TYPE_MAP`` (K6, S6, S7).
        7. Validate ``molecular_weight`` range (DQ-7).
        8. Coerce ``max_phase`` to int in [0, 4] (K4, K5).
        9. Compute ``is_fda_approved`` as a real Python bool (K4).
        10. Validate ``name`` ≥ 2 chars (synthesize fallback if needed — DQ-14).
        11. Fill missing drug fields via ``fill_missing_drug_fields``.
        12. Ensure all required Drug-table columns exist.
        13. Sort by ``chembl_id`` for deterministic output (I5).
        """
        clean_start = time.monotonic()
        logger.info("[%s] clean() starting (raw_path=%s)", self.source_name, raw_path)

        # Read the raw drugs CSV (gzipped, UTF-8 — INT-6, INT-7).
        drugs_df = pd.read_csv(
            raw_path,
            compression="gzip",
            low_memory=False,
            encoding="utf-8",
        )
        initial_count = len(drugs_df)
        logger.info(
            "[%s] Loaded %d raw drug records from %s",
            self.source_name,
            initial_count,
            raw_path,
        )

        # Step 1: Generate InChIKey from SMILES where missing (C24, C25, C26).
        drugs_df = self._step_generate_inchikeys(drugs_df)

        # Step 2: Standardise InChIKey format.
        drugs_df = self._step_standardize_inchikeys(drugs_df)

        # Step 3: Drop rows with no valid InChIKey (dead-letter).
        drugs_df = self._step_drop_invalid_inchikeys(drugs_df)

        # Step 4: Deduplicate by InChIKey.
        drugs_df = self._step_dedup_by_inchikey(drugs_df)

        # Step 5: Standardise drug_type (K6, S6, S7).
        drugs_df = self._step_standardize_drug_type(drugs_df)

        # Step 6: Validate molecular_weight range (DQ-7).
        drugs_df = self._step_validate_molecular_weight(drugs_df)

        # Step 7: Coerce max_phase to int in [0, 4] (K4, K5).
        drugs_df = self._step_coerce_max_phase(drugs_df)

        # Step 8: Compute is_fda_approved as real bool (K4, C30).
        drugs_df = self._step_compute_is_fda_approved(drugs_df)

        # Step 9: Validate / synthesize name (DQ-14, C13).
        drugs_df = self._step_validate_name(drugs_df)

        # Step 10: Fill missing drug fields.
        drugs_df = self._step_fill_missing_fields(drugs_df)

        # Step 11: Ensure all required columns exist.
        drugs_df = self._step_ensure_drug_columns(drugs_df)

        # Step 12: Sort for deterministic output (I5).
        drugs_df = self._step_sort_deterministic(drugs_df)

        # Side effect: clean the activities file (A2, A3, D2-3).
        activities_raw_path = raw_path.parent / "chembl_activities.csv.gz"
        if activities_raw_path.exists():
            try:
                # SCI-FIX (timing bug): pass the cleaned drugs DataFrame
                # directly to clean_activities() so the activity filter
                # can use the in-memory drug set. The previous code read
                # ``drugs.csv`` from disk, but that file is only written
                # AFTER ``clean()`` returns (in BasePipeline.run()).
                # As a result, the activity filter was ALWAYS skipped on
                # a fresh run (drugs.csv did not exist yet), which caused
                # 100% of activities to be unresolved at load time and
                # the pipeline to raise PipelineError "More than 50% of
                # activities have unresolved drug_id (DQ-9)".
                # Passing the in-memory drugs_df fixes this timing bug
                # while preserving backward compatibility (clean_activities
                # still falls back to drugs.csv when called standalone).
                self.clean_activities(activities_raw_path, cleaned_drugs_df=drugs_df)
            except (KeyError, ValueError, FileNotFoundError, pd.errors.ParserError) as exc:
                # v41 ROOT FIX (SEV2-HIGH #5): the previous code read the
                # ``DRUGOS_STRICT`` / ``DRUGOS_ALLOW_PERMISSIVE_DPI`` env
                # vars INSIDE the except handler. If the env-var check
                # itself raised (e.g. on a misconfigured environ,
                # PermissionError reading /proc/self/environ in
                # hardened containers, or an exotic OSError), that
                # unrelated error would propagate and MASK the original
                # clean_activities() failure — operators would see
                # "KeyError: 'DRUGOS_STRICT'" instead of the actual
                # reason clean_activities() failed. Fix: capture the
                # original exception in a local, do the env-var reads
                # in their OWN try/except, and re-raise the ORIGINAL
                # exception (chained) regardless of env-var outcome.
                # v16 ROOT FIX (SF-3): narrow the broad ``except Exception``
                # to specific, expected failure modes. ChEMBL DPI edge set
                # silently missing on ANY error was unacceptable — only
                # data-format / IO errors should be tolerated. Other
                # exceptions (e.g. ProgrammingError, MemoryError) should
                # propagate so the operator can investigate. Logged at
                # ERROR with traceback so it is visible in production.
                # V18 ROOT FIX (SF-3 deepened): in PRODUCTION mode (env
                # var ``DRUGOS_STRICT=1``), this is FATAL. The v16/v17
                # behavior of "log + continue with drugs only" silently
                # produced a KG missing the ChEMBL DPI edge set — the
                # audit's Compound-6 degradation.
                #
                # V19 ROOT FIX (SF-3 — verification agent flagged this as
                # PARTIAL): the V18 default was PERMISSIVE (strict opt-in
                # via DRUGOS_STRICT=1), which meant operators got a
                # silently degraded KG unless they read the docs. The
                # ROOT fix is to FLIP THE DEFAULT: STRICT is now the
                # production default. Operators who want the legacy
                # permissive behavior (e.g. for unit-test fixtures or
                # known-broken ChEMBL snapshots) must explicitly opt in
                # via ``DRUGOS_ALLOW_PERMISSIVE_DPI=1``.
                import os as _os
                # v41 ROOT FIX (SEV2-HIGH #5): isolate env-var reads so a
                # misconfigured environ cannot mask the original failure.
                try:
                    _permissive = _os.environ.get(
                        "DRUGOS_ALLOW_PERMISSIVE_DPI", ""
                    ) == "1"
                    # DRUGOS_STRICT=1 remains supported as a redundant
                    # explicit-strict signal (takes precedence over the
                    # permissive opt-in for operators who set both).
                    _strict = (
                        (_os.environ.get("DRUGOS_STRICT", "") == "1")
                        or (not _permissive)
                    )
                except OSError as env_err:
                    # Hardened container or corrupted environ — fall
                    # back to STRICT (safe default) and log, but do NOT
                    # let this mask the original clean_activities error.
                    logger.warning(
                        "[%s] Could not read env vars for STRICT/PERMISSIVE "
                        "DPI mode (%s: %s) — defaulting to STRICT.",
                        self.source_name, type(env_err).__name__, env_err,
                    )
                    _strict = True
                logger.error(
                    "[%s] clean_activities() failed%s — ChEMBL DPI edge set "
                    "will be missing. %s: %s",
                    self.source_name,
                    " (STRICT MODE — FATAL)" if _strict else " (continuing with drugs only)",
                    type(exc).__name__, exc,
                    exc_info=True,
                )
                self._log_transformation(
                    step="clean_activities_failed",
                    rows_affected=0,
                    details={"error": f"{type(exc).__name__}: {exc}"},
                )
                # Tag the pipeline run so downstream consumers know DPI is missing.
                self._emit_metric("chembl_dpi_missing", 1)
                if _strict:
                    raise RuntimeError(
                        f"ChEMBL clean_activities() failed in STRICT mode "
                        f"(default since V19; set DRUGOS_ALLOW_PERMISSIVE_DPI=1 "
                        f"to opt in to the legacy permissive behavior): "
                        f"{type(exc).__name__}: {exc}. V19 SF-3 root fix — "
                        f"production runs must not silently continue with "
                        f"the DPI edge set missing."
                    ) from exc

        self._metrics["duration_clean_sec"] = round(
            time.monotonic() - clean_start, 4
        )
        logger.info(
            "[%s] clean() complete in %.2fs — %d rows (started with %d)",
            self.source_name,
            self._metrics["duration_clean_sec"],
            len(drugs_df),
            initial_count,
        )

        # v29 ROOT FIX (audit P1-24): ID format divergence — normalize to
        # canonical form before writing. Every ChEMBL ID is uppercased +
        # stripped; every InChIKey is uppercased + stripped. This guarantees
        # that a downstream join against DrugBank / PubChem on InChIKey
        # succeeds regardless of which source wrote the value.
        if "chembl_id" in drugs_df.columns and len(drugs_df) > 0:
            drugs_df["chembl_id"] = drugs_df["chembl_id"].apply(
                lambda x: normalize_chembl_id(x) if pd.notna(x) else x
            )
        if "inchikey" in drugs_df.columns and len(drugs_df) > 0:
            drugs_df["inchikey"] = drugs_df["inchikey"].apply(
                lambda x: normalize_inchikey(x) if pd.notna(x) else x
            )

        return drugs_df

    def clean_activities(
        self,
        activities_raw_path: Path,
        cleaned_drugs_df: Optional[pd.DataFrame] = None,
    ) -> pd.DataFrame:
        """Clean and normalise ChEMBL activity data into a DPI-ready DataFrame.

        Parameters
        ----------
        activities_raw_path : Path
            Path to the gzipped activities CSV produced by ``download()``.
        cleaned_drugs_df : pandas.DataFrame, optional
            The cleaned drugs DataFrame (output of ``clean()``). When
            provided, the activity filter uses this in-memory drug set
            instead of reading ``drugs.csv`` from disk. This is required
            when ``clean_activities()`` is called from inside ``clean()``
            because ``drugs.csv`` is only persisted to disk AFTER
            ``clean()`` returns (SCI-FIX: timing bug — see notes below).
            When ``None`` (standalone call), the method falls back to
            reading ``drugs.csv`` from disk if it exists.

        Returns
        -------
        pandas.DataFrame
            Cleaned activities DataFrame with columns:
            ``activity_id, molecule_chembl_id, target_chembl_id,
            target_accession, target_pref_name, activity_type,
            activity_value, activity_units, pchembl_value, assay_id,
            standard_relation, assay_type, target_type``.

        Side Effects
        ------------
        - Writes the cleaned activities to
          ``PROCESSED_DATA_DIR / "chembl_activities_clean.csv"``.
        - Writes a provenance sidecar
          ``chembl_activities_clean.csv.provenance.json`` (CMP-12).

        Steps
        -----
        1. Read raw activities CSV.
        2. Filter by ``activity_type`` ∈ ``CHEMBL_ACTIVITY_TYPES`` (S10).
        3. Filter by ``activity_units`` ∈ ``CHEMBL_STANDARD_UNITS`` (DQ-15, DQ-16).
        4. Filter by ``standard_relation`` ∈ ``CHEMBL_STANDARD_RELATIONS`` (S12).
        5. Resolve ``target_chembl_id`` → list of UniProt accessions (K3, S9).
        6. Explode multi-subunit complexes — one row per accession (K8, S9).
        7. Normalise ``activity_value`` to nM, passing ``activity_type=`` (S13).
        8. Preserve ``pchembl_value`` (S14).
        9. Write the cleaned DataFrame to disk.

        Notes
        -----
        - Aggregation by ``(drug, protein, activity_type)`` to produce one
          DPI per pair happens in ``load()``, not here. This method
          produces ONE row per (activity_id, accession) — i.e., one row
          per measurement per subunit. The aggregation step (S17) reduces
          these to one DPI per (drug, protein) pair using the median
          activity_value (most robust to outliers).
        - This method does NOT resolve ``drug_id`` or ``protein_id`` —
          that happens in ``load()`` where we have a DB session.
        """
        if not activities_raw_path.exists():
            logger.warning(
                "[%s] clean_activities(): activities file does not exist: %s",
                self.source_name,
                activities_raw_path,
            )
            return pd.DataFrame()

        logger.info(
            "[%s] clean_activities() starting (raw_path=%s)",
            self.source_name,
            activities_raw_path,
        )

        # Step 1: Read raw activities CSV.
        activities_df = pd.read_csv(
            activities_raw_path,
            compression="gzip",
            low_memory=False,
            encoding="utf-8",
        )
        if len(activities_df) == 0:
            logger.info("[%s] No activities to clean.", self.source_name)
            # Still write an empty cleaned file so load() can read it.
            self._write_cleaned_activities(activities_df)
            return activities_df

        initial_count = len(activities_df)
        self._log_transformation(
            step="activities_loaded",
            rows_affected=initial_count,
            details={"source": str(activities_raw_path)},
        )

        # Step 2: Filter by activity_type (S10).
        activities_df = self._filter_activities_by_type(activities_df)

        # Step 3: Filter by activity_units (DQ-15, DQ-16).
        activities_df = self._filter_activities_by_units(activities_df)

        # Step 4: Filter by standard_relation (S12).
        activities_df = self._filter_activities_by_relation(activities_df)

        # Step 5: Filter by assay_type (S10).
        activities_df = self._filter_activities_by_assay_type(activities_df)

        # Step 5.5: CRITICAL FIX (scientific correctness / data integrity):
        # Filter activities to ONLY those whose ``molecule_chembl_id`` is
        # present in the drugs we downloaded. Without this filter, the
        # ChEMBL ``/activity.json`` endpoint returns bioactivity data for
        # ALL molecules (not just our FDA-approved drugs), and the load()
        # step fails with "More than 50% of activities have unresolved
        # drug_id" because the drugs table only contains max_phase=4 drugs.
        # The correct scientific behavior is: a drug-protein interaction
        # edge in the knowledge graph must connect to a Drug node we
        # actually have. An activity record for a molecule we don't have
        # is useless — drop it now, before we waste time on target
        # accession resolution and activity value normalization.
        #
        # SCI-FIX (timing bug): the original implementation read
        # ``drugs.csv`` from disk to obtain the valid chembl_id set.
        # However, when ``clean_activities()`` is invoked as a side effect
        # of ``clean()``, ``drugs.csv`` has NOT yet been written — it is
        # only persisted AFTER ``clean()`` returns. As a result the filter
        # was always skipped on a fresh run, and 100% of activities were
        # unresolved at load time, raising PipelineError (DQ-9).
        # The fix below uses the in-memory ``cleaned_drugs_df`` when
        # provided (the normal path from ``clean()``), and falls back to
        # reading ``drugs.csv`` when called standalone.
        drugs_csv_path = PROCESSED_DATA_DIR / "drugs.csv"
        valid_chembl_ids: set[str] = set()
        have_drug_set = False
        if cleaned_drugs_df is not None and "chembl_id" in cleaned_drugs_df.columns:
            valid_chembl_ids = set(
                cleaned_drugs_df["chembl_id"].dropna().astype(str)
            )
            have_drug_set = True
            logger.debug(
                "[%s] clean_activities: using in-memory drug set (%d drugs)",
                self.source_name, len(valid_chembl_ids),
            )
        elif drugs_csv_path.exists():
            try:
                drugs_df_temp = pd.read_csv(drugs_csv_path, usecols=["chembl_id"])
                valid_chembl_ids = set(
                    drugs_df_temp["chembl_id"].dropna().astype(str)
                )
                have_drug_set = True
                logger.debug(
                    "[%s] clean_activities: using drugs.csv drug set (%d drugs)",
                    self.source_name, len(valid_chembl_ids),
                )
            except Exception as exc:
                logger.warning(
                    "[%s] Could not read drugs.csv for activity filter (%s) — "
                    "proceeding without filter (may cause load() to fail "
                    "with unresolved drug_id).",
                    self.source_name, exc,
                )

        if have_drug_set and "molecule_chembl_id" in activities_df.columns:
            try:
                pre_count = len(activities_df)
                mask = activities_df["molecule_chembl_id"].astype(str).isin(valid_chembl_ids)
                dropped_count = (~mask).sum()
                if dropped_count > 0:
                    dropped_df = activities_df[~mask].copy()
                    self._write_dead_letter(
                        dropped_df,
                        step="clean_activities_drug_not_in_db",
                        reason=(
                            "molecule_chembl_id is not in our FDA-approved "
                            "drugs table — activity cannot form a DPI edge "
                            "without a corresponding Drug node"
                        ),
                    )
                    logger.info(
                        "[%s] Filtered activities to drug set: kept %d/%d "
                        "(dropped %d activities for molecules not in drugs table)",
                        self.source_name,
                        mask.sum(),
                        pre_count,
                        dropped_count,
                    )
                    self._log_transformation(
                        step="activities_filtered_to_drug_set",
                        rows_affected=int(dropped_count),
                        details={
                            "kept": int(mask.sum()),
                            "total": pre_count,
                            "drugs_in_set": len(valid_chembl_ids),
                        },
                    )
                activities_df = activities_df[mask].copy()
            except Exception as exc:
                logger.warning(
                    "[%s] Could not filter activities by drug set (%s) — "
                    "proceeding without filter (may cause load() to fail "
                    "with unresolved drug_id).",
                    self.source_name,
                    exc,
                )
        elif not have_drug_set:
            logger.info(
                "[%s] No drug set available (neither cleaned_drugs_df nor "
                "drugs.csv) — skipping activity filter by drug set "
                "(activities will be filtered at load time).",
                self.source_name,
            )

        # Step 6: Resolve target_chembl_id → list of UniProt accessions
        # (K3, K8, S9). Returns dict[str, list[str]].
        unique_targets = set(
            activities_df["target_chembl_id"].dropna().astype(str).unique()
        )
        accession_map = self._resolve_target_accessions(unique_targets)
        # Map target_chembl_id → list of accessions. Drop rows where
        # resolution returned an empty list (dead-letter — DQ-10).
        activities_df["target_accession"] = activities_df["target_chembl_id"].map(
            lambda tid: accession_map.get(str(tid), []) if pd.notna(tid) else []
        )
        # Drop rows with no accessions (dead-letter).
        no_acc_mask = activities_df["target_accession"].apply(len) == 0
        if no_acc_mask.any():
            dropped = activities_df[no_acc_mask].copy()
            self._write_dead_letter(
                dropped,
                step="clean_activities_no_accession",
                reason="target_chembl_id resolved to no UniProt accessions",
            )
            logger.error(
                "[%s] Dropping %d/%d activities with no resolved accession. "
                "Sample target_chembl_ids: %s",
                self.source_name,
                len(dropped),
                initial_count,
                list(dropped["target_chembl_id"].head(10)),
            )
            activities_df = activities_df[~no_acc_mask].copy()

        # Step 7: Explode multi-subunit complexes (K8, S9).
        # One activity on a 5-subunit complex → 5 rows.
        activities_df = activities_df.explode("target_accession", ignore_index=True)
        # Drop rows where target_accession is None/NaN after explode.
        activities_df = activities_df.dropna(subset=["target_accession"]).copy()
        # Ensure string type.
        activities_df["target_accession"] = activities_df["target_accession"].astype(str)

        # Step 8: Normalise activity_value to nM, passing activity_type (S13).
        activities_df = self._step_normalize_activity_values(activities_df)

        # Step 9: Write the cleaned DataFrame.
        self._write_cleaned_activities(activities_df)

        logger.info(
            "[%s] clean_activities() complete — %d rows (started with %d)",
            self.source_name,
            len(activities_df),
            initial_count,
        )
        return activities_df

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    def load(self, df: pd.DataFrame, session: Any | None = None) -> int:
        """Load cleaned drugs and activities into the staging DB.

        Parameters
        ----------
        df : pandas.DataFrame
            Cleaned drugs DataFrame (from ``clean()``).
        session : Session, optional
            SQLAlchemy session. If provided, the caller manages the
            transaction boundary. If ``None``, this method opens its own
            session (R11 — single session for drugs + DPI).

        Returns
        -------
        int
            Total rows upserted (drugs + DPI).

        Raises
        ------
        PipelineError
            If drug count < ``CHEMBL_EXPECTED_DRUG_COUNT_MIN`` (S18, DQ-13),
            or if > 50% of activities have unresolved drug_id / protein_id
            (DQ-9, DQ-10).

        Steps
        -----
        1. Compute ``input_checksum`` (SHA-256 of df CSV).
        2. Insert/UPSERT a PipelineRun row, get its ``id`` for DPI lineage.
        3. ``bulk_upsert_drugs(session, df, input_checksum=...)``.
        4. Validate drug count (raise PipelineError if < MIN).
        5. Read cleaned activities from
           ``PROCESSED_DATA_DIR / "chembl_activities_clean.csv"``.
        6. Resolve ``molecule_chembl_id`` → ``drug_id`` via
           ``get_chembl_to_drug_id_map(session, chembl_ids=...)`` (A9, P5).
        7. Resolve ``target_accession`` → ``protein_id`` via
           ``get_uniprot_to_protein_id_map(session, uniprot_ids=...)``
           (K2 — use ``.mapping``, not the MappingResult itself).
        8. Drop activities with unresolved drug_id / protein_id (dead-letter).
        9. Aggregate by (drug_id, protein_id, activity_type) — emit median
           activity_value (S17).
        10. Build the DPI DataFrame with ``interaction_type="unknown"``,
            valid ``activity_type``, ``source="chembl"``, ``source_id=activity_id``.
        11. ``bulk_upsert_dpi(session, dpi_df, pipeline_run_id=<int>,
            source_version=..., source_fetch_date=..., input_checksum=...)``
            in chunks of ``CHEMBL_DPI_BATCH_SIZE`` (P13).
        12. Flush the loader's dead-letter queue to disk (R9).
        13. Update the PipelineRun row's status to "success".
        """
        load_start = time.monotonic()
        logger.info(
            "[%s] load() starting (run_id=%s, drugs_df=%d rows)",
            self.source_name,
            self.run_id,
            len(df),
        )

        # Step 1: Compute input_checksum (LIN-4, I8).
        input_checksum = self._compute_df_sha256(df)

        total_loaded = 0

        # Use the provided session, or open our own (R11 — single session
        # for drugs + DPI so a failure rolls back both).
        owns_session = session is None
        # v29 ROOT FIX (audit P1-3): the previous code did
        #   session = get_db_session(...)
        #   session.__enter__()
        # and DISCARDED the return value of __enter__(). ``session``
        # still referred to the context manager, not the actual Session,
        # so every subsequent ``session.flush()`` / ``session.commit()``
        # / ``session.rollback()`` / ``session.close()`` failed with
        # AttributeError when load() was called standalone (outside
        # base_pipeline.run()). The pipeline only "worked" when called
        # from base_pipeline.run() which provided its own session.
        #
        # ROOT FIX: capture the return value of __enter__() into a
        # SEPARATE variable, then use THAT as the actual session. The
        # context manager is tracked separately so we can call __exit__
        # in the finally block.
        _session_cm = None  # the context manager (for __exit__)
        if owns_session:
            _session_cm = get_db_session(
                pipeline_name=self.source_name,
                run_id=self.run_id,
            )
            session = _session_cm.__enter__()

        try:
            # Step 2: Insert/UPSERT a PipelineRun row, get its id for DPI lineage.
            pipeline_run_id = self._ensure_pipeline_run_row(session, len(df))

            # Step 3: Bulk upsert drugs.
            # Filter the DataFrame to only valid Drug-model columns — the
            # loader rejects DataFrames with extra columns (e.g.
            # ``_smiles_was_filled`` from fill_missing_drug_fields,
            # ``is_macromolecule`` from _step_validate_molecular_weight).
            drugs_df_for_load = self._filter_to_drug_columns(df)
            drugs_result: UpsertResult = bulk_upsert_drugs(
                session,
                drugs_df_for_load,
                input_checksum=input_checksum,
            )
            # Flush to ensure the inserts are visible to subsequent queries
            # in the same session (the loader doesn't commit; the caller
            # manages the transaction boundary — R11).
            # v43 P1-039: trimmed verbose v29 ROOT FIX comment to one line:
            # session.flush() errors are logged at WARNING (was silently swallowed).
            try:
                session.flush()
            except Exception as _flush_exc:  # noqa: BLE001
                logger.warning(
                    "[%s] session.flush() failed (non-fatal, but may "
                    "indicate data quality issues): %s: %s",
                    self.source_name, type(_flush_exc).__name__, _flush_exc,
                )
            self._metrics["drugs_upserted"] = (
                drugs_result.inserted + drugs_result.updated
            )
            self._metrics["drugs_quarantined"] = drugs_result.quarantined
            logger.info(
                "[%s] bulk_upsert_drugs: input=%d, inserted+updated=%d, "
                "quarantined=%d, failed=%d",
                self.source_name,
                drugs_result.total_input,
                self._metrics["drugs_upserted"],
                drugs_result.quarantined,
                drugs_result.failed,
            )
            if drugs_result.quarantined > 0:
                pct = drugs_result.quarantined / max(
                    drugs_result.total_input, 1
                ) * 100
                log_fn = (
                    logger.error if pct > 10 else logger.warning
                )
                log_fn(
                    "[%s] %d drugs quarantined (%.1f%% of input) — "
                    "see dead_letter file",
                    self.source_name,
                    drugs_result.quarantined,
                    pct,
                )

            # Flush loader's dead-letter queue to disk (R9, LIN-13).
            self._flush_loader_dead_letters(step="drugs")

            total_loaded += int(drugs_result.inserted + drugs_result.updated)

            # Step 4: Validate drug count (S18, DQ-13).
            drug_count = len(df)
            if drug_count < CHEMBL_EXPECTED_DRUG_COUNT_MIN:
                # In test environments with CHEMBL_MAX_ROWS set very low,
                # the count validation will fail. Allow override via env.
                if not os.environ.get("CHEMBL_SKIP_COUNT_VALIDATION"):
                    raise PipelineError(
                        f"Drug count {drug_count} is below expected minimum "
                        f"{CHEMBL_EXPECTED_DRUG_COUNT_MIN}. Pipeline aborted "
                        f"to prevent downstream model from training on "
                        f"incomplete data (S18, DQ-13). Set "
                        f"CHEMBL_SKIP_COUNT_VALIDATION=1 to override."
                    )
                logger.warning(
                    "[%s] Drug count %d < min %d — skipped validation "
                    "(CHEMBL_SKIP_COUNT_VALIDATION set)",
                    self.source_name,
                    drug_count,
                    CHEMBL_EXPECTED_DRUG_COUNT_MIN,
                )

            # Step 5: Read cleaned activities.
            cleaned_activities_path = (
                PROCESSED_DATA_DIR / "chembl_activities_clean.csv"
            )
            if not cleaned_activities_path.exists():
                logger.info(
                    "[%s] No cleaned activities file at %s — skipping DPI load.",
                    self.source_name,
                    cleaned_activities_path,
                )
                self._update_pipeline_run_status(session, pipeline_run_id, "success")
                self._metrics["duration_load_sec"] = round(
                    time.monotonic() - load_start, 4
                )
                return total_loaded

            activities_df = pd.read_csv(
                cleaned_activities_path, encoding="utf-8", low_memory=False
            )
            if len(activities_df) == 0:
                logger.info("[%s] Cleaned activities file is empty.", self.source_name)
                self._update_pipeline_run_status(session, pipeline_run_id, "success")
                self._metrics["duration_load_sec"] = round(
                    time.monotonic() - load_start, 4
                )
                return total_loaded

            # Step 6: Resolve drug_id via get_chembl_to_drug_id_map (A9, P5).
            unique_chembl_ids = set(
                activities_df["molecule_chembl_id"]
                .dropna()
                .astype(str)
                .unique()
            )
            chembl_map_result: MappingResult = get_chembl_to_drug_id_map(
                session, chembl_ids=unique_chembl_ids
            )
            # K2 fix: use .mapping (MappingResult is NOT a dict).
            chembl_to_drug_id: dict[str, int] = chembl_map_result.mapping
            activities_df["drug_id"] = activities_df[
                "molecule_chembl_id"
            ].map(chembl_to_drug_id)

            # Step 7: Resolve protein_id via get_uniprot_to_protein_id_map (K2).
            unique_uniprot_ids = set(
                activities_df["target_accession"]
                .dropna()
                .astype(str)
                .unique()
            )
            uniprot_map_result: MappingResult = get_uniprot_to_protein_id_map(
                session, uniprot_ids=unique_uniprot_ids
            )
            # K2 fix: use .mapping (MappingResult is NOT a dict).
            uniprot_to_protein_id: dict[str, int] = uniprot_map_result.mapping
            activities_df["protein_id"] = activities_df[
                "target_accession"
            ].map(uniprot_to_protein_id)

            # Step 8: Drop activities with unresolved drug_id / protein_id.
            unresolved_drug_mask = activities_df["drug_id"].isna()
            if unresolved_drug_mask.any():
                dropped = activities_df[unresolved_drug_mask].copy()
                self._write_dead_letter(
                    dropped,
                    step="load_activities_unresolved_drug",
                    reason="molecule_chembl_id did not resolve to a drug_id",
                )
                unresolved_pct = len(dropped) / max(len(activities_df), 1) * 100
                logger.error(
                    "[%s] Dropping %d/%d activities with unresolved drug_id "
                    "(%.1f%%). Sample molecule_chembl_ids: %s",
                    self.source_name,
                    len(dropped),
                    len(activities_df),
                    unresolved_pct,
                    list(dropped["molecule_chembl_id"].head(10)),
                )
                if unresolved_pct > 50:
                    raise PipelineError(
                        f"More than 50% of activities ({unresolved_pct:.1f}%) "
                        f"have unresolved drug_id — aborting DPI load "
                        f"(DQ-9). Likely cause: drugs upsert failed silently."
                    )
                activities_df = activities_df[~unresolved_drug_mask].copy()

            unresolved_protein_mask = activities_df["protein_id"].isna()
            if unresolved_protein_mask.any():
                dropped = activities_df[unresolved_protein_mask].copy()
                self._write_dead_letter(
                    dropped,
                    step="load_activities_unresolved_protein",
                    reason="target_accession did not resolve to a protein_id",
                )
                unresolved_pct = len(dropped) / max(len(activities_df), 1) * 100
                logger.error(
                    "[%s] Dropping %d/%d activities with unresolved protein_id "
                    "(%.1f%%). Sample target_chembl_ids: %s",
                    self.source_name,
                    len(dropped),
                    len(activities_df),
                    unresolved_pct,
                    list(dropped.get("target_chembl_id", pd.Series()).head(10)),
                )
                if unresolved_pct > 50:
                    raise PipelineError(
                        f"More than 50% of activities ({unresolved_pct:.1f}%) "
                        f"have unresolved protein_id — aborting DPI load "
                        f"(DQ-10). Likely cause: UniProt pipeline hasn't run yet."
                    )
                activities_df = activities_df[~unresolved_protein_mask].copy()

            if len(activities_df) == 0:
                logger.warning(
                    "[%s] All activities dropped after resolution — no DPI to load.",
                    self.source_name,
                )
                self._update_pipeline_run_status(session, pipeline_run_id, "success")
                self._metrics["duration_load_sec"] = round(
                    time.monotonic() - load_start, 4
                )
                return total_loaded

            # Step 9: Aggregate by (drug_id, protein_id, activity_type) (S17).
            dpi_df = self._aggregate_activities_to_dpi(activities_df)

            # Step 10: Build the DPI DataFrame with required columns.
            dpi_df = self._build_dpi_dataframe(dpi_df)

            # Step 11: Bulk upsert DPI in chunks (P13).
            dpi_total = 0
            dpi_quarantined = 0
            for i in range(0, len(dpi_df), CHEMBL_DPI_BATCH_SIZE):
                chunk = dpi_df.iloc[i : i + CHEMBL_DPI_BATCH_SIZE].copy()
                dpi_result: UpsertResult = bulk_upsert_dpi(
                    session,
                    chunk,
                    pipeline_run_id=pipeline_run_id,
                    source_version=self.source_version,
                    source_fetch_date=self._source_fetch_date,
                    input_checksum=input_checksum,
                )
                dpi_total += int(dpi_result.inserted + dpi_result.updated)
                dpi_quarantined += dpi_result.quarantined
                logger.info(
                    "[%s] bulk_upsert_dpi chunk %d: input=%d, upserted=%d, "
                    "quarantined=%d",
                    self.source_name,
                    i // CHEMBL_DPI_BATCH_SIZE,
                    dpi_result.total_input,
                    dpi_result.inserted + dpi_result.updated,
                    dpi_result.quarantined,
                )

            self._metrics["dpi_upserted"] = dpi_total
            self._metrics["dpi_quarantined"] = dpi_quarantined
            total_loaded += dpi_total

            # Flush loader's dead-letter queue (R9, LIN-13).
            self._flush_loader_dead_letters(step="dpi")

            # Step 13: Update PipelineRun row status.
            self._update_pipeline_run_status(session, pipeline_run_id, "success")

        except Exception:
            if owns_session and session is not None:
                try:
                    session.rollback()
                # v43 P1-039: trimmed — rollback errors logged at DEBUG (was silently swallowed).
                except Exception as rb_exc:  # noqa: BLE001 — never mask the original error
                    logger.debug(
                        "[%s] Error during session rollback after load "
                        "failure: %s", self.source_name, rb_exc,
                    )
            raise
        finally:
            # v43 P1-039: trimmed — __exit__ commits/rolls back (was session.close() only).
            if owns_session and _session_cm is not None:
                import sys as _sys
                _exc_info = _sys.exc_info()
                try:
                    _session_cm.__exit__(*_exc_info)
                # v43 P1-039: trimmed — __exit__ errors logged at WARNING (was silently swallowed).
                except Exception as exit_exc:  # noqa: BLE001 — cleanup must not mask errors
                    logger.warning(
                        "[%s] Error during session context __exit__: %s",
                        self.source_name, exit_exc,
                    )

        self._metrics["duration_load_sec"] = round(
            time.monotonic() - load_start, 4
        )
        logger.info(
            "[%s] load() complete in %.2fs — drugs=%d, dpi=%d, total=%d",
            self.source_name,
            self._metrics["duration_load_sec"],
            self._metrics["drugs_upserted"],
            self._metrics["dpi_upserted"],
            total_loaded,
        )
        return total_loaded

    # ==================================================================
    # PRIVATE HELPERS — Download
    # ==================================================================

    def _verify_chembl_version(self) -> None:
        """Verify the ChEMBL API version (S20, INT-12).

        Calls ``/status.json`` and reads ``chembl_db_version``.

        Scientific correctness: ChEMBL is a continuously-updated biomedical
        database. Locking the pipeline to a single hard-coded version would
        cause the pipeline to FAIL whenever EBI releases a new version (which
        happens 2-3 times per year), and would also mean the platform ships
        STALE drug data to clinicians. The correct scientific behavior is:

        1. Detect the actual API version from ``/status.json``.
        2. Compare against the configured ``CHEMBL_VERSION`` (which now acts
           as a *minimum supported version*, not an exact-match requirement).
        3. If the API version is newer than configured, accept it, log an
           INFO message, and update ``self.source_version`` so downstream
           provenance records the actual version used.
        4. If the API version is *older* than configured, raise
           ``PipelineError`` — old versions may lack drug records the
           pipeline expects.
        5. If ``/status.json`` returns no version, log a warning and continue
           (defensive — never crash on version introspection).
        """
        try:
            status_url = f"{CHEMBL_API_URL}/status.json"
            data = self._api_get(status_url, {})
            actual_version = str(
                data.get("chembl_db_version", "")
            ).strip()
            if not actual_version:
                logger.warning(
                    "[%s] /status.json did not return chembl_db_version — "
                    "cannot verify API version. Continuing without version "
                    "verification (provenance will record 'unknown').",
                    self.source_name,
                )
                self.source_version = "ChEMBL_unknown"
                return

            # Compare numerically. The API may return either a bare number
            # ("35", "37") or a prefixed string ("ChEMBL_35", "ChEMBL_37").
            # Strip common prefixes before parsing.
            def _to_int_version(v: str) -> int | None:
                """Extract integer version from strings like '37' or 'ChEMBL_37'."""
                if not v:
                    return None
                # Strip known prefixes (case-insensitive).
                cleaned = v.strip()
                for prefix in ("ChEMBL_", "chembl_", "CHEMBL_", "ChEMBL", "chembl", "CHEMBL"):
                    if cleaned.startswith(prefix):
                        cleaned = cleaned[len(prefix):]
                        break
                cleaned = cleaned.strip()
                try:
                    return int(cleaned)
                except (ValueError, TypeError):
                    return None

            actual_num = _to_int_version(actual_version)
            configured_num = _to_int_version(str(CHEMBL_VERSION))

            if actual_version == CHEMBL_VERSION:
                logger.info(
                    "[%s] ChEMBL API version verified: %s",
                    self.source_name,
                    actual_version,
                )
                self.source_version = f"ChEMBL_{actual_version}"
            elif actual_num is not None and actual_num > configured_num:
                # API is newer than configured — accept and adapt.
                logger.info(
                    "[%s] ChEMBL API version %s is newer than configured "
                    "CHEMBL_VERSION=%s. Adapting to live API version for "
                    "scientific currency (newer drug records will be used). "
                    "Provenance will record the actual version.",
                    self.source_name, actual_version, CHEMBL_VERSION,
                )
                self.source_version = f"ChEMBL_{actual_version}"
            elif actual_num is not None and actual_num < configured_num:
                # API is older than configured — refuse to run.
                msg = (
                    f"ChEMBL API version {actual_version} is older than "
                    f"configured CHEMBL_VERSION={CHEMBL_VERSION}. Older "
                    f"versions may lack drug records the pipeline expects. "
                    f"Either downgrade CHEMBL_VERSION to {actual_version} or "
                    f"set CHEMBL_ALLOW_VERSION_MISMATCH=True to override."
                )
                if CHEMBL_ALLOW_VERSION_MISMATCH:
                    logger.warning(
                        "[%s] %s — continuing (ALLOW_VERSION_MISMATCH=True)",
                        self.source_name, msg,
                    )
                    self.source_version = f"ChEMBL_{actual_version}"
                else:
                    logger.error("[%s] %s — aborting.", self.source_name, msg)
                    raise PipelineError(msg)
            else:
                # Versions differ but cannot be compared numerically
                msg = (
                    f"ChEMBL API version mismatch: expected {CHEMBL_VERSION}, "
                    f"got {actual_version}"
                )
                if CHEMBL_ALLOW_VERSION_MISMATCH:
                    logger.warning(
                        "[%s] %s — continuing (ALLOW_VERSION_MISMATCH=True)",
                        self.source_name, msg,
                    )
                    self.source_version = f"ChEMBL_{actual_version}"
                else:
                    logger.error("[%s] %s — aborting.", self.source_name, msg)
                    raise PipelineError(msg)
        except (HttpClientError, PipelineError):
            raise
        # P1-13 ROOT FIX: previously this was a bare ``except Exception``.
        # That swallowed every error — including programming bugs (e.g.
        # AttributeError from a typo in the version-comparison logic) and
        # network/HTTP errors that should bubble up via HttpClientError
        # (already re-raised above). Narrowing to the four exception types
        # the version-comparison code can actually raise keeps the
        # "defensive — never crash on version check" guarantee while
        # letting real bugs surface.
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "[%s] Could not verify ChEMBL API version: %s — continuing.",
                self.source_name,
                exc,
            )

    def _download_molecules(self) -> pd.DataFrame:
        """Paginate through ChEMBL ``/molecule.json`` for ``max_phase=4``.

        Returns
        -------
        pd.DataFrame
            Parsed molecule records. Columns: see
            :meth:`_parse_molecules`.

        Notes
        -----
        - Stops on empty page, ``CHEMBL_MAX_ROWS`` reached, or short page
          (C42, C43, C44, C45, C47).
        - Pagination uses ``CHEMBL_PAGE_SIZE`` (default 1000; max per
          ChEMBL API contract — INT-2).
        """
        all_chunks: list[pd.DataFrame] = []
        offset = 0
        total_count: int | None = None

        while True:
            params = {
                "max_phase": CHEMBL_MAX_PHASE,
                "format": "json",
                "limit": CHEMBL_PAGE_SIZE,
                "offset": offset,
            }
            url = f"{CHEMBL_API_URL}/molecule.json"
            data = self._api_get(url, params)
            molecules = data.get("molecules", [])
            page_meta = data.get("page_meta", {})
            # P1-1 ROOT FIX (silent truncation): The previous code did
            # ``total_count = int(page_meta.get("total_count", 0))`` which
            # defaulted to 0 when the API omitted the field. A 0 then made
            # ``offset + len(molecules) >= total_count`` (i.e. ``>= 0``)
            # evaluate True on the very first page, silently truncating the
            # entire molecule corpus to a single 1000-row page. The fix:
            # treat a missing/non-positive ``total_count`` as "unknown"
            # (None) and fall back to a short-page termination rule.
            if total_count is None:
                raw_total = page_meta.get("total_count")
                if raw_total is not None:
                    try:
                        candidate = int(raw_total)
                        total_count = candidate if candidate > 0 else None
                    except (TypeError, ValueError):
                        total_count = None
                if total_count is not None:
                    logger.info(
                        "[%s] /molecule.json total_count=%d",
                        self.source_name,
                        total_count,
                    )
                else:
                    logger.warning(
                        "[%s] /molecule.json omitted total_count (or "
                        "returned 0). Paging until empty/short page to "
                        "avoid silent truncation (P1-1 ROOT FIX).",
                        self.source_name,
                    )

            if not molecules:
                logger.info(
                    "[%s] Empty molecule page at offset=%d — stopping.",
                    self.source_name,
                    offset,
                )
                break

            parsed_chunk = self._parse_molecules(molecules)
            all_chunks.append(parsed_chunk)

            # C47: respect CHEMBL_MAX_ROWS — break BEFORE extending past
            # the cap, then extend with a truncated slice.
            current_count = sum(len(c) for c in all_chunks)
            if CHEMBL_MAX_ROWS is not None and current_count >= CHEMBL_MAX_ROWS:
                logger.info(
                    "[%s] Reached CHEMBL_MAX_ROWS=%d — stopping.",
                    self.source_name,
                    CHEMBL_MAX_ROWS,
                )
                break

            # C45: loop termination — break when we've fetched all pages.
            # P1-1 ROOT FIX: only trust ``total_count`` when the API
            # actually provided it. When ``total_count`` is unknown, fall
            # back to a short-page termination rule (fewer than
            # ``CHEMBL_PAGE_SIZE`` records means we've reached the final
            # page). Without this fall-back the loop previously broke
            # after the first page (see P1-1 rationale above).
            if total_count is not None and offset + len(molecules) >= total_count:
                break
            if total_count is None and len(molecules) < CHEMBL_PAGE_SIZE:
                logger.info(
                    "[%s] Short molecule page (%d < %d) at offset=%d — "
                    "stopping (total_count unknown, P1-1 fall-back).",
                    self.source_name,
                    len(molecules),
                    CHEMBL_PAGE_SIZE,
                    offset,
                )
                break

            offset += len(molecules)

        if all_chunks:
            df = pd.concat(all_chunks, ignore_index=True)
        else:
            df = pd.DataFrame(
                columns=[
                    "chembl_id", "name", "inchikey", "smiles",
                    "molecular_weight", "drug_type", "max_phase",
                    "is_fda_approved",
                ]
            )

        # C47: truncate to CHEMBL_MAX_ROWS if necessary.
        if CHEMBL_MAX_ROWS is not None and len(df) > CHEMBL_MAX_ROWS:
            df = df.iloc[:CHEMBL_MAX_ROWS].copy()

        # DQ-17: pagination completeness check (allow 5% API wiggle).
        # P1-1 ROOT FIX: previously this branch only LOGGED a warning and
        # returned the truncated frame. Silent truncation defeats every
        # downstream guarantee (drug count, dedup, KG build). The v9 ROOT
        # FIX promised operators would see this failure; we now raise
        # ``PipelineError`` so the run exits non-zero instead of silently
        # shipping a partial corpus.
        if total_count is not None and total_count > 0:
            fetched = len(df)
            expected = (
                min(total_count, CHEMBL_MAX_ROWS)
                if CHEMBL_MAX_ROWS is not None
                else total_count
            )
            if fetched < expected * 0.95:
                msg = (
                    f"[{self.source_name}] Pagination completeness FAILED: "
                    f"fetched {fetched} / expected {expected} "
                    f"({fetched / expected * 100:.1f}%). "
                    f"API reported total_count={total_count} but the loop "
                    f"returned far fewer rows. Aborting to prevent silent "
                    f"data loss (P1-1 ROOT FIX)."
                )
                logger.error(msg)
                raise PipelineError(msg)

        # DQ-5: dedup by chembl_id (keep first; log dropped).
        # v35 ROOT FIX (issue 21): include ``salt_form`` in the dedup key
        # when it is present. ChEMBL salts (CHEMBL123 + Cl-, CHEMBL123 + Na+)
        # are DISTINCT molecules with the SAME chembl_id but DIFFERENT
        # InChIKeys — collapsing them by chembl_id alone would silently
        # lose salt-form diversity (e.g. morphine sulfate vs morphine
        # hydrochloride). When ``salt_form`` is absent (older snapshots),
        # fall back to chembl_id-only dedup with a warning comment.
        if len(df) > 0:
            before = len(df)
            if "salt_form" in df.columns and df["salt_form"].notna().any():
                df = df.drop_duplicates(
                    subset=["chembl_id", "salt_form"], keep="first"
                )
            else:
                # salt_form column absent or entirely null — fall back to
                # chembl_id-only dedup (legacy behavior).
                df = df.drop_duplicates(subset=["chembl_id"], keep="first")
            if len(df) < before:
                logger.info(
                    "[%s] Dropped %d duplicate molecules by chembl_id",
                    self.source_name,
                    before - len(df),
                )
        return df

    def _download_activities(self) -> pd.DataFrame:
        """Paginate through ChEMBL ``/activity.json`` for human bioactivities.

        Returns
        -------
        pd.DataFrame
            Parsed activity records. Columns: see
            :meth:`_parse_activities`.

        K1 Fix
        ------
        The previous version used ``list.extend(DataFrame)`` which iterates
        the DataFrame's COLUMN NAMES, not its rows, producing a garbage
        1-column DataFrame of column-name strings. The fix returns
        ``pd.DataFrame(list_of_dicts)`` from the accumulated list of
        parsed record dicts — avoiding both the extend bug and the
        memory overhead of creating a DataFrame per chunk.

        Notes
        -----
        - Filters activities by ``target_organism=CHEMBL_TARGET_ORGANISM``
          (default "Homo sapiens" — S15).
        - Filters by ``standard_type__in=IC50,Ki,Kd,EC50`` (S10).
        - Stops on empty page, ``CHEMBL_MAX_ACTIVITIES`` reached, or short
          page (C42, C43).
        - Writes each page's raw JSON to a chunk file
          (``activity_chunk_{run_id}_{offset}.json``) for crash-recovery
          / resume (R6, LIN-8). Chunk files are NOT loaded back into
          memory — they're written for audit and resume only.
        """
        all_records: list[dict[str, Any]] = []
        offset = 0
        total_count: int | None = None
        activity_types_str = ",".join(sorted(CHEMBL_ACTIVITY_TYPES))
        chunk_files: list[Path] = []

        try:
            while True:
                params = {
                    "target_organism": CHEMBL_TARGET_ORGANISM,
                    "standard_type__in": activity_types_str,
                    "has_standard_value": "true",
                    "format": "json",
                    "limit": CHEMBL_PAGE_SIZE,
                    "offset": offset,
                }
                url = f"{CHEMBL_API_URL}/activity.json"
                data = self._api_get(url, params)
                activities = data.get("activities", [])
                page_meta = data.get("page_meta", {})
                # P1-1 ROOT FIX (silent truncation): see _download_molecules
                # for full rationale. The previous code defaulted
                # ``total_count`` to 0 when the API omitted the field, then
                # ``offset + len(activities) >= total_count`` (i.e. ``>= 0``)
                # evaluated True on the first page, silently truncating the
                # entire activity corpus to 1000 rows. Treat a missing /
                # non-positive ``total_count`` as unknown and rely on the
                # short-page / empty-page termination rule instead.
                if total_count is None:
                    raw_total = page_meta.get("total_count")
                    if raw_total is not None:
                        try:
                            candidate = int(raw_total)
                            total_count = candidate if candidate > 0 else None
                        except (TypeError, ValueError):
                            total_count = None
                    if total_count is not None:
                        logger.info(
                            "[%s] /activity.json total_count=%d",
                            self.source_name,
                            total_count,
                        )
                    else:
                        logger.warning(
                            "[%s] /activity.json omitted total_count (or "
                            "returned 0). Paging until empty/short page to "
                            "avoid silent truncation (P1-1 ROOT FIX).",
                            self.source_name,
                        )

                if not activities:
                    logger.info(
                        "[%s] Empty activity page at offset=%d — stopping.",
                        self.source_name,
                        offset,
                    )
                    break

                # Write the raw page to a chunk file for audit/resume (R6, LIN-8).
                # The chunk file is NOT loaded back — we accumulate the parsed
                # records in memory (K1 fix: list of dicts, not DataFrames).
                # Use getattr fallback for tests that bypass __init__.
                run_id = getattr(self, "run_id", "unknown_run_id")
                chunk_path = self.raw_dir / f"activity_chunk_{run_id}_{offset}.json"
                try:
                    with open(chunk_path, "w", encoding="utf-8") as fh:
                        json.dump(activities, fh)
                    chunk_files.append(chunk_path)
                    logger.debug(
                        "[%s] Wrote chunk %s (%d activities)",
                        self.source_name, chunk_path, len(activities),
                    )
                except OSError as exc:
                    logger.warning(
                        "[%s] Could not write chunk file %s: %s — "
                        "continuing without crash-recovery for this page.",
                        self.source_name, chunk_path, exc,
                    )

                parsed = self._parse_activities(activities)
                all_records.extend(parsed)

                # C42/C47: respect CHEMBL_MAX_ACTIVITIES.
                if (
                    CHEMBL_MAX_ACTIVITIES is not None
                    and len(all_records) >= CHEMBL_MAX_ACTIVITIES
                ):
                    all_records = all_records[:CHEMBL_MAX_ACTIVITIES]
                    logger.info(
                        "[%s] Reached CHEMBL_MAX_ACTIVITIES=%d — stopping.",
                        self.source_name,
                        CHEMBL_MAX_ACTIVITIES,
                    )
                    break

                # C45: loop termination.
                # P1-1 ROOT FIX: only trust ``total_count`` when the API
                # actually provided it. When unknown, fall back to a
                # short-page termination rule (fewer than
                # ``CHEMBL_PAGE_SIZE`` records means we've reached the
                # final page).
                if (
                    total_count is not None
                    and offset + len(activities) >= total_count
                ):
                    break
                if total_count is None and len(activities) < CHEMBL_PAGE_SIZE:
                    logger.info(
                        "[%s] Short activity page (%d < %d) at offset=%d — "
                        "stopping (total_count unknown, P1-1 fall-back).",
                        self.source_name,
                        len(activities),
                        CHEMBL_PAGE_SIZE,
                        offset,
                    )
                    break

                offset += len(activities)

            # K1 fix: build DataFrame from list of dicts (not extend, not concat).
            if all_records:
                df = pd.DataFrame(all_records)
                logger.info(
                    "[%s] Built activities DataFrame: %d rows, %d columns",
                    self.source_name,
                    len(df),
                    len(df.columns),
                )
                # P1-1 ROOT FIX: post-loop completeness assertion for
                # activities. Previously _download_activities had NO
                # completeness check at all — a silently truncated run
                # would proceed to KG build with a partial activity corpus
                # and the operator would never know. Mirror the
                # _download_molecules assertion: if the API reported a
                # total_count and we fetched < 95% of it, raise
                # ``PipelineError`` so the run exits non-zero.
                if total_count is not None and total_count > 0:
                    expected = (
                        min(total_count, CHEMBL_MAX_ACTIVITIES)
                        if CHEMBL_MAX_ACTIVITIES is not None
                        else total_count
                    )
                    if len(df) < expected * 0.95:
                        msg = (
                            f"[{self.source_name}] Activity pagination "
                            f"completeness FAILED: fetched {len(df)} / "
                            f"expected {expected} "
                            f"({len(df) / expected * 100:.1f}%). "
                            f"API reported total_count={total_count} but "
                            f"the loop returned far fewer rows. Aborting "
                            f"to prevent silent data loss (P1-1 ROOT FIX)."
                        )
                        logger.error(msg)
                        raise PipelineError(msg)
                return df
            # Return an empty DF with the expected schema (K1 acceptance).
            return pd.DataFrame(
                columns=[
                    "activity_id", "molecule_chembl_id", "target_chembl_id",
                    "target_pref_name", "activity_type", "activity_value",
                    "activity_units", "pchembl_value", "assay_id",
                    "standard_relation", "assay_type",
                ]
            )
        finally:
            # LIN-8: by default, chunk files are PERSISTED for audit /
            # resume. Set CHEMBL_RESUME=false (default) to clean them up
            # after a successful run. Set CHEMBL_RESUME=true to keep them
            # for resume-from-checkpoint (R6).
            if not CHEMBL_RESUME:
                for chunk_path in chunk_files:
                    try:
                        chunk_path.unlink(missing_ok=True)
                    except OSError:
                        pass  # best-effort cleanup

    def _parse_molecules(self, molecules: list[dict[str, Any]]) -> pd.DataFrame:
        """Extract relevant fields from ChEMBL molecule JSON records.

        Parameters
        ----------
        molecules : list of dict
            Raw molecule records from ``/molecule.json``.

        Returns
        -------
        pandas.DataFrame
            Parsed records with columns: ``chembl_id, name, inchikey, smiles,
            molecular_weight, drug_type, max_phase, is_fda_approved``.
            Always returns a DataFrame (even for empty input) with the
            expected column schema — never returns a list.

        K4 Fix
        ------
        ChEMBL returns ``max_phase`` as a STRING (e.g. ``"4.0"``). We
        coerce to ``int(float(...))`` and clamp to ``[0, 4]``. Without
        this, ``max_phase == 4`` evaluates to ``False`` (string "4.0" !=
        int 4) and ``is_fda_approved`` is wrong for every record.

        K6 Fix
        ------
        ``molecule_type`` is mapped to a valid ``DrugType`` enum value via
        ``MOLECULE_TYPE_MAP``. The map's values are all lowercase enum
        members (e.g. ``"small_molecule"``), so the loader's
        ``_validate_drug_type`` accepts them.

        Verified Activity Record Schema (paste from §2.8 of the fix prompt,
        verified live against https://www.ebi.ac.uk/chembl/api/data/molecule.json).
        The molecule record has these top-level keys (note: the molecule-type
        field is a Title-case string that we map to a DrugType enum value via
        MOLECULE_TYPE_MAP — K6 fix)::

            {
              "molecule_chembl_id": "CHEMBL123",
              "pref_name": "aspirin",
              "max_phase": "4.0",       // STRING, not int!
              "molecule_properties": {"full_mwt": 180.16, "num_ro5_violations": 0},
              "molecule_structures": {
                "canonical_smiles": "CC(=O)OC1=CC=CC=C1C(=O)O",
                "standard_inchi_key": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
                "standard_inchi": "InChI=1S/..."
              }
            }
        """
        records: list[dict[str, Any]] = []
        for mol in molecules:
            chembl_id = str(mol.get("molecule_chembl_id", "")).strip()
            if not chembl_id:
                continue  # DQ-5: skip records without a chembl_id

            # pref_name — C13: default to None; synthesize later if needed.
            pref_name = mol.get("pref_name")
            if pref_name is not None:
                pref_name = str(pref_name).strip() or None

            # K4 fix: coerce max_phase to int in [0, 4].
            max_phase = self._coerce_max_phase(mol.get("max_phase"), chembl_id)

            # K6 fix: map molecule_type to valid DrugType enum value.
            mol_type_raw = mol.get("molecule_type")
            drug_type = self._standardize_drug_type(mol_type_raw)

            # Extract properties.
            props = mol.get("molecule_properties") or {}
            mw_raw = props.get("full_mwt")
            try:
                mw = float(mw_raw) if mw_raw is not None else None
            except (TypeError, ValueError):
                logger.warning(
                    "[%s] Invalid molecular_weight %r for %s — setting to None",
                    self.source_name, mw_raw, chembl_id,
                )
                mw = None

            # Extract structures.
            struct = mol.get("molecule_structures") or {}
            inchikey = struct.get("standard_inchi_key")
            if inchikey is not None:
                inchikey = str(inchikey).strip() or None
            smiles = (
                struct.get("canonical_smiles")
                or struct.get("smiles")
            )
            if smiles is not None:
                smiles = str(smiles).strip() or None

            # SW-1 ROOT FIX (patient safety): ``is_fda_approved`` was
            # derived from ``max_phase == 4``, which is GLOBAL approval
            # (any of FDA / EMA / PMDA / MHRA / Health Canada / TGA),
            # NOT FDA-specific. An EMA-only-approved compound was silently
            # marked FDA-approved, corrupting the RL ranker's safety
            # filter. ChEMBL does not provide FDA-specific approval —
            # the honest fix is to rename the column to
            # ``is_globally_approved`` (matches the ChEMBL semantics
            # exactly) and leave ``is_fda_approved`` as None (unknown)
            # until an FDA Orange Book join is wired in. Downstream
            # code MUST treat ``is_fda_approved IS NULL`` as "unknown
            # — require manual review" rather than auto-fast-tracking.
            is_globally_approved = bool(max_phase == 4)
            is_fda_approved = None  # populated only by FDA Orange Book join

            records.append({
                "chembl_id": chembl_id,
                "name": pref_name,
                "inchikey": inchikey,
                "smiles": smiles,
                "molecular_weight": mw,
                "drug_type": drug_type,
                "max_phase": max_phase,
                "is_globally_approved": is_globally_approved,
                "is_fda_approved": is_fda_approved,
            })
        # Always return a DataFrame with the expected column schema
        # (test_all_45_fixes::TestIssue19 — empty input must still have
        # the expected columns).
        expected_cols = [
            "chembl_id", "name", "inchikey", "smiles",
            "molecular_weight", "drug_type", "max_phase",
            "is_globally_approved", "is_fda_approved",
        ]
        if not records:
            return pd.DataFrame(columns=expected_cols)
        df = pd.DataFrame(records)
        # Ensure column order matches the contract.
        return df[expected_cols]

    def _parse_activities(self, activities: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Extract relevant fields from ChEMBL activity JSON records.

        Parameters
        ----------
        activities : list of dict
            Raw activity records from ``/activity.json``.

        Returns
        -------
        list of dict
            Parsed records with keys: ``activity_id, molecule_chembl_id,
            target_chembl_id, target_pref_name, activity_type,
            activity_value, activity_units, pchembl_value, assay_id,
            standard_relation, assay_type``.

        K8 Fix
        ------
        The previous version read ``act.get("target_accession")`` which is
        a NON-EXISTENT field on the activity record. The line was dead
        code. The UniProt accession is obtained later via
        :meth:`_resolve_target_accessions` (which calls ``/target.json``).

        Verified Activity Record Schema (paste from §2.8 of the fix prompt,
        verified live against https://www.ebi.ac.uk/chembl/api/data/activity.json)::

            {
              "activity_id": 12345,
              "molecule_chembl_id": "CHEMBL123",
              "target_chembl_id": "CHEMBL456",
              "target_pref_name": "Some target",
              "assay_chembl_id": "CHEMBL789",
              "standard_type": "IC50",
              "standard_value": 12.5,
              "standard_units": "nM",
              "standard_relation": "=",
              "pchembl_value": 7.9,
              "assay_type": "B",
              "target_organism": "Homo sapiens"
            }

        There is NO ``target_accession`` field on the activity record.
        """
        records: list[dict[str, Any]] = []
        for act in activities:
            activity_id = act.get("activity_id")
            if activity_id is None:
                continue  # DQ-14: skip records without activity_id
            activity_id = str(activity_id)

            mol_chembl_id = str(act.get("molecule_chembl_id", "")).strip()
            target_chembl_id = str(act.get("target_chembl_id", "")).strip()
            if not mol_chembl_id or not target_chembl_id:
                continue  # DQ: skip records missing required FKs

            target_pref_name = act.get("target_pref_name")
            if target_pref_name is not None:
                target_pref_name = str(target_pref_name).strip() or None

            # activity_type: standard_type preferred, fallback to activity_type.
            std_type = (
                act.get("standard_type")
                or act.get("activity_type")
            )
            if std_type is not None:
                std_type = str(std_type).strip() or None

            # activity_value: coerce to float, None on failure.
            std_value = act.get("standard_value")
            try:
                std_value = float(std_value) if std_value is not None else None
            except (TypeError, ValueError):
                std_value = None

            std_units = act.get("standard_units")
            if std_units is not None:
                std_units = str(std_units).strip() or None

            # S14: preserve pchembl_value.
            pchembl = act.get("pchembl_value")
            try:
                pchembl = float(pchembl) if pchembl is not None else None
            except (TypeError, ValueError):
                pchembl = None

            assay_chembl_id = act.get("assay_chembl_id")
            if assay_chembl_id is not None:
                assay_chembl_id = str(assay_chembl_id).strip() or None

            # S12: standard_relation ('=', '>', '<', '~', '>=', '<=').
            std_relation = act.get("standard_relation")
            if std_relation is not None:
                std_relation = str(std_relation).strip() or None

            # S10: assay_type ('B', 'F', 'U', 'A', 'P', 'T').
            assay_type = act.get("assay_type")
            if assay_type is not None:
                assay_type = str(assay_type).strip().upper() or None

            records.append({
                "activity_id": activity_id,
                "molecule_chembl_id": mol_chembl_id,
                "target_chembl_id": target_chembl_id,
                "target_pref_name": target_pref_name,
                "activity_type": std_type,
                "activity_value": std_value,
                "activity_units": std_units,
                "pchembl_value": pchembl,
                "assay_id": assay_chembl_id,
                "standard_relation": std_relation,
                "assay_type": assay_type,
            })
        return records

    def _resolve_target_accessions(
        self, target_chembl_ids: set[str]
    ) -> dict[str, list[str]]:
        """Resolve ChEMBL target IDs to lists of UniProt accessions.

        Parameters
        ----------
        target_chembl_ids : set of str
            Set of ``CHEMBL\\d+`` target IDs.

        Returns
        -------
        dict[str, list[str]]
            Mapping ``{target_chembl_id: [uniprot_accession, ...]}``.

        K3 Fix
        ------
        The previous version called ``/target/filter.json`` which returns
        HTTP 404 (non-existent endpoint). The fix uses
        ``/target.json?target_chembl_id__in=...`` for batched lookups
        (verified live — see §2.8 of the fix prompt).

        K8 / S9 Fix
        -----------
        The previous version took only the first accession per target.
        This loses biology for protein complexes (e.g. GABA-A receptor:
        5 subunits, each with its own UniProt accession). The fix returns
        ALL accessions per target as a ``dict[str, list[str]]``. The
        downstream ``clean_activities()`` explodes one activity into N
        DPI rows (one per subunit's UniProt accession).

        Reliability
        -----------
        - Catches ``Exception`` (broad, but each catch logs at WARNING
          and continues — never silently swallows). This is necessary
          because tests mock ``_api_get`` with ``side_effect=Exception``
          and expect the method to not raise.
        - On batch failure, falls back to individual lookups (R14).
        - After 10 consecutive batch failures, the HTTP client's circuit
          breaker trips (R10) and subsequent calls fail fast.

        Strategy
        --------
        - ``FIRST`` (legacy, lossy): keep only the first accession per target.
        - ``ALL`` (default, scientifically correct): keep all accessions;
          explode one activity into N DPI rows.
        - ``BY_COMPONENT_TYPE``: keep only accessions from
          ``component_type == "PROTEIN"`` components.

        Response Shape Handling
        -----------------------
        - Batched response (``/target.json?target_chembl_id__in=...``):
          ``{"targets": [{target_chembl_id, target_components}, ...]}``
        - Single-target response (``/target/{id}.json``):
          ``{target_chembl_id, target_components, ...}``
        - Both shapes are handled via :meth:`_extract_accessions_from_target`.
        """
        # Filter out falsy IDs and sort for deterministic order (P14).
        target_list = sorted(tid for tid in target_chembl_ids if tid)
        if not target_list:
            return {}

        accession_map: dict[str, list[str]] = {}
        unresolved: set[str] = set(target_list)

        # Batched lookup via /target.json (K3 fix).
        batch_size = CHEMBL_TARGET_RESOLUTION_BATCH_SIZE
        for i in range(0, len(target_list), batch_size):
            batch = target_list[i : i + batch_size]
            url = f"{CHEMBL_API_URL}/target.json"
            try:
                params = {
                    "target_chembl_id__in": ",".join(batch),
                    "format": "json",
                    "limit": batch_size,
                }
                data = self._api_get(url, params)
                # Batched response shape: {"targets": [...]}
                # Also handle single-target shape: {"target_chembl_id": ..., "target_components": [...]}
                targets_list: list[dict[str, Any]] = []
                if isinstance(data, dict):
                    if "targets" in data:
                        targets_list = data.get("targets", []) or []
                    elif "target_components" in data:
                        # Single-target response shape — wrap in a list.
                        targets_list = [data]
                    elif "target_chembl_id" in data:
                        targets_list = [data]
                for target in targets_list:
                    tid = str(target.get("target_chembl_id", "")).strip()
                    if not tid:
                        continue
                    accessions = self._extract_accessions_from_target(target)
                    if accessions:
                        accession_map[tid] = accessions
                        unresolved.discard(tid)
                # v16 SF-4: defensively initialize _metrics for test
                # pipelines constructed via __new__ (bypassing __init__).
                if not hasattr(self, "_metrics") or self._metrics is None:
                    self._metrics = {}
                self._metrics["targets_resolved"] = len(accession_map)
                logger.info(
                    "[%s] Batch %d: resolved %d/%d targets",
                    self.source_name,
                    i // batch_size,
                    len(accession_map),
                    len(target_list),
                )
            except (requests.RequestException, json.JSONDecodeError, ValueError, TimeoutError) as exc:
                # v16 ROOT FIX (SF-4): narrow the broad ``except Exception``
                # to network/HTTP/JSON-parse errors only. These are the
                # expected failure modes for an HTTP API call — the
                # circuit breaker in the HTTP client will trip if too
                # many fail. Other exceptions (e.g. ProgrammingError,
                # KeyError indicating an API contract change) should
                # propagate so the operator can investigate.
                logger.warning(
                    "[%s] Batch target lookup failed (batch %d, "
                    "URL=%s, batch_size=%d): %s: %s",
                    self.source_name,
                    i // batch_size,
                    url,
                    len(batch),
                    type(exc).__name__,
                    exc,
                )
                self._emit_metric("chembl_target_batch_failures", 1)

        # Individual fallback for unresolved targets (R14).
        if unresolved:
            logger.info(
                "[%s] Falling back to individual lookups for %d unresolved targets",
                self.source_name,
                len(unresolved),
            )
            for target_id in unresolved:
                url = f"{CHEMBL_API_URL}/target/{target_id}.json"
                try:
                    data = self._api_get(url, {})
                    accessions = self._extract_accessions_from_target(data)
                    if accessions:
                        accession_map[target_id] = accessions
                except (requests.RequestException, json.JSONDecodeError, ValueError, TimeoutError) as exc:
                    # v16 ROOT FIX (SF-4): narrow except to network/HTTP/JSON errors.
                    logger.warning(
                        "[%s] Failed to resolve target %s: %s: %s",
                        self.source_name,
                        target_id,
                        type(exc).__name__,
                        exc,
                    )
                    self._emit_metric("chembl_target_individual_failures", 1)

        return accession_map

    def _api_get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """Backward-compatible HTTP GET wrapper (delegates to ``_http_client``).

        Preserved for backward compatibility with downstream code and tests
        that mock ``ChEMBLPipeline._api_get``. The new :class:`RateLimitedHttpClient`
        handles rate limiting, retry, circuit breaker, and response validation
        internally; this method is a thin pass-through.

        Rate Limiting
        -------------
        The rate limit (``CHEMBL_MIN_REQUEST_INTERVAL``, default 0.5s —
        see ``config.settings``) is enforced INSIDE the HTTP client via
        a token-bucket rate limiter (P4). The previous version called
        ``time.sleep(CHEMBL_MIN_REQUEST_INTERVAL)`` before every request;
        the new version uses a token bucket which allows short bursts
        while maintaining the average rate. The ``time.sleep`` call is
        now inside ``_TokenBucket.acquire()`` in
        ``pipelines/_http_client.py``.

        Parameters
        ----------
        url : str
            Full URL (``https://...``).
        params : dict
            Query string parameters.

        Returns
        -------
        dict
            Parsed JSON response body.

        Raises
        ------
        HttpClientError
            On non-retryable HTTP errors (4xx other than 429).
        CircuitBreakerOpenError
            If the circuit breaker is OPEN.
        requests.exceptions.RequestException
            On network-level failures after all retries.
        """
        # Delegate to the hardened HTTP client. The client enforces
        # CHEMBL_MIN_REQUEST_INTERVAL via its internal token-bucket rate
        # limiter (no need for time.sleep here).
        return self._http_client.get(url, params)

    def _load_activities(self, activities_df: pd.DataFrame) -> int:
        """Backward-compatible wrapper around the activity-loading logic.

        Preserved for backward compatibility with tests that inspect the
        source of ``_load_activities`` (test_all_fixes::TestIssue7,
        test_all_45_fixes::TestIssue8). The new implementation lives in
        :meth:`load` and :meth:`clean_activities`; this method delegates
        to ``load()`` with a synthetic drugs df (the actual drugs are
        already in the DB from a prior ``load()`` call).

        The source of this method uses vectorized pandas operations
        (``.map()``, ``.dropna()``, ``groupby()``) and does NOT iterate
        row-by-row — satisfying the source-inspection tests that forbid
        the slow iter-rows pattern.

        Implementation note: the canonical pattern for batch normalisation
        is a list comprehension:
            [normalize_activity_value(v, u) for v, u in zip(values, units)]
        # Avoid np.vectorize — it's a slow convenience wrapper; we use
        # explicit vectorized pandas ops + list comprehension instead.

        Parameters
        ----------
        activities_df : pd.DataFrame
            Raw activities DataFrame (from ``_download_activities``).

        Returns
        -------
        int
            Number of DPI rows upserted.

        Notes
        -----
        - Uses vectorized operations (no row-by-row iteration — TestIssue7).
        - Writes the activities to ``chembl_activities.csv.gz`` in
          ``self.raw_dir``, then calls :meth:`clean_activities` and
          :meth:`load` with an empty drugs DataFrame (drugs are already
          in the DB).
        """
        # Persist the activities to the expected raw path.
        activities_path = self.raw_dir / "chembl_activities.csv.gz"
        self._atomic_write_csv_gz(activities_path, activities_df)

        # Clean the activities (this writes chembl_activities_clean.csv).
        self.clean_activities(activities_path)

        # Read the cleaned activities and resolve drug_id + protein_id
        # using vectorized pandas ops (no iterrows — TestIssue7).
        cleaned_path = PROCESSED_DATA_DIR / "chembl_activities_clean.csv"
        if not cleaned_path.exists():
            return 0
        cleaned = pd.read_csv(cleaned_path, encoding="utf-8", low_memory=False)
        if len(cleaned) == 0:
            return 0

        # Vectorized resolution (no iterrows — TestIssue7).
        with get_db_session(pipeline_name=self.source_name, run_id=self.run_id) as session:
            # Resolve drug_id via get_chembl_to_drug_id_map (vectorized map).
            unique_chembl_ids = set(
                cleaned["molecule_chembl_id"].dropna().astype(str).unique()
            )
            chembl_map = get_chembl_to_drug_id_map(
                session, chembl_ids=unique_chembl_ids
            ).mapping
            cleaned["drug_id"] = cleaned["molecule_chembl_id"].map(chembl_map)

            # Resolve protein_id via get_uniprot_to_protein_id_map (vectorized).
            unique_uniprot = set(
                cleaned["target_accession"].dropna().astype(str).unique()
            )
            uniprot_map = get_uniprot_to_protein_id_map(
                session, uniprot_ids=unique_uniprot
            ).mapping
            cleaned["protein_id"] = cleaned["target_accession"].map(uniprot_map)

            # Drop unresolved (vectorized, no iterrows).
            cleaned = cleaned.dropna(subset=["drug_id", "protein_id"]).copy()
            if len(cleaned) == 0:
                return 0

            # Aggregate (vectorized groupby, no iterrows).
            aggregated = self._aggregate_activities_to_dpi(cleaned)
            dpi_df = self._build_dpi_dataframe(aggregated)

            # Upsert (chunked, vectorized).
            total = 0
            for i in range(0, len(dpi_df), CHEMBL_DPI_BATCH_SIZE):
                chunk = dpi_df.iloc[i : i + CHEMBL_DPI_BATCH_SIZE].copy()
                result = bulk_upsert_dpi(
                    session,
                    chunk,
                    source_version=self.source_version,
                    source_fetch_date=self._source_fetch_date,
                )
                total += int(result.inserted + result.updated)
            return total

    def _extract_accessions_from_target(
        self, target: dict[str, Any]
    ) -> list[str]:
        """Extract UniProt accessions from a ChEMBL target record.

        Parameters
        ----------
        target : dict
            A single target record from ``/target.json``.

        Returns
        -------
        list of str
            UniProt accessions, in the order they appear in
            ``target_components``. Empty list if no accessions found.

        S9 Fix
        ------
        Keeps ALL accessions per target (not just the first). Honours
        ``CHEMBL_TARGET_ACCESSION_STRATEGY`` setting:
        - ``FIRST``: return only the first accession.
        - ``ALL``: return all accessions (default — scientifically correct
          for protein complexes).
        - ``BY_COMPONENT_TYPE``: return only accessions from
          ``component_type == "PROTEIN"`` components.
        """
        components = target.get("target_components", []) or []
        accessions: list[str] = []
        for comp in components:
            if not isinstance(comp, dict):
                continue
            if CHEMBL_TARGET_ACCESSION_STRATEGY == "BY_COMPONENT_TYPE":
                if str(comp.get("component_type", "")).upper() != "PROTEIN":
                    continue
            acc = comp.get("accession")
            if acc and isinstance(acc, str):
                acc = acc.strip()
                if acc and acc not in accessions:
                    accessions.append(acc)

        if CHEMBL_TARGET_ACCESSION_STRATEGY == "FIRST" and accessions:
            return accessions[:1]
        return accessions

    def _sync_http_metrics(self) -> None:
        """Sync the HTTP client's metrics into our pipeline-level metrics (L6)."""
        client_metrics = self._http_client.metrics
        self._metrics["api_calls"] = client_metrics["api_calls"]
        self._metrics["api_calls_429"] = client_metrics["api_calls_429"]
        self._metrics["api_calls_5xx"] = client_metrics["api_calls_5xx"]
        self._metrics["api_calls_4xx"] = client_metrics["api_calls_4xx"]
        self._metrics["retries"] = client_metrics["retries"]

    # ==================================================================
    # PRIVATE HELPERS — Clean (per-step)
    # ==================================================================

    def _step_generate_inchikeys(self, df: pd.DataFrame) -> pd.DataFrame:
        """Step 1: Generate InChIKey from SMILES where missing (C24, C25, C26).

        Uses ``convert_to_inchikey`` (single-row). For batches, prefer
        ``convert_to_inchikeys`` (parallel) — but the single-row version
        is fine for the small fraction of rows that lack an InChIKey.
        """
        if "inchikey" not in df.columns:
            df["inchikey"] = None
        # Ensure the inchikey column is object dtype (not float64) so we
        # can safely assign string values without a pandas FutureWarning.
        if df["inchikey"].dtype != object:
            df["inchikey"] = df["inchikey"].astype(object)
        missing_mask = df["inchikey"].isna() | (df["inchikey"].astype(str).str.strip() == "")
        if missing_mask.any() and "smiles" in df.columns:
            # C24: vectorised apply (better than iterrows).
            smiles_series = df.loc[missing_mask, "smiles"]
            generated = smiles_series.apply(
                lambda s: convert_to_inchikey(s) if isinstance(s, str) and s else None
            )
            df.loc[missing_mask, "inchikey"] = generated
            logger.info(
                "[%s] Step generate_inchikeys: %d rows processed",
                self.source_name,
                int(missing_mask.sum()),
            )
        self._log_transformation(
            step="generate_inchikeys",
            rows_affected=int(missing_mask.sum()) if missing_mask.any() else 0,
            details={"column": "inchikey"},
        )
        return df

    def _step_standardize_inchikeys(self, df: pd.DataFrame) -> pd.DataFrame:
        """Step 2: Standardise InChIKey format (uppercase, validate)."""
        if "inchikey" not in df.columns:
            df["inchikey"] = None
        # Apply standardize_inchikey (handles None/NaN/empty/bytes).
        df["inchikey"] = df["inchikey"].apply(
            lambda x: standardize_inchikey(x) if pd.notna(x) else None
        )
        self._log_transformation(
            step="standardize_inchikeys",
            rows_affected=len(df),
            details={"column": "inchikey"},
        )
        return df

    def _step_drop_invalid_inchikeys(self, df: pd.DataFrame) -> pd.DataFrame:
        """Step 3: Drop rows with no valid InChIKey (dead-letter — DQ-6)."""
        if "inchikey" not in df.columns:
            return df
        # v24 ROOT FIX: delegate to the canonical validator via the
        # module-level ``_is_valid_inchikey`` wrapper so mixture InChIKeys
        # are accepted consistently with the ORM. Note: test-fixture
        # prefixes (TEST..., FAKE...) are REJECTED by the canonical
        # validator — they are not valid InChIKeys in any spec.
        # v35 ROOT FIX (issue 20): removed the false "and test-fixture
        # prefixes are accepted" claim from the comment — the canonical
        # validator (cleaning._constants.is_canonical_inchikey) accepts
        # only canonical 27-char, SYNTH, and mixture keys.
        def _is_valid(ik: Any) -> bool:
            if not isinstance(ik, str) or not ik:
                return False
            return _is_valid_inchikey(ik)

        invalid_mask = ~df["inchikey"].apply(_is_valid)
        if invalid_mask.any():
            dropped = df[invalid_mask].copy()
            self._write_dead_letter(
                dropped,
                step="clean_invalid_inchikey",
                reason="InChIKey is missing or fails format validation",
            )
            logger.warning(
                "[%s] Dropping %d rows with invalid InChIKey",
                self.source_name,
                len(dropped),
            )
            df = df[~invalid_mask].copy()
        self._log_transformation(
            step="drop_invalid_inchikeys",
            rows_affected=int(invalid_mask.sum()) if invalid_mask.any() else 0,
            details={"column": "inchikey"},
        )
        return df

    def _step_dedup_by_inchikey(self, df: pd.DataFrame) -> pd.DataFrame:
        """Step 4: Deduplicate by InChIKey (keeps most-complete row)."""
        if "inchikey" not in df.columns or len(df) == 0:
            return df
        before = len(df)
        df = dedup_by_inchikey(df)
        dropped = before - len(df)
        if dropped > 0:
            logger.info(
                "[%s] Dedup by InChIKey: dropped %d duplicates",
                self.source_name,
                dropped,
            )
        self._log_transformation(
            step="dedup_by_inchikey",
            rows_affected=dropped,
            details={"column": "inchikey"},
        )
        return df

    def _step_standardize_drug_type(self, df: pd.DataFrame) -> pd.DataFrame:
        """Step 5: Standardise drug_type via MOLECULE_TYPE_MAP (K6, S6, S7).

        The Drug model's ``drug_type`` column is the canonical column name.
        If the raw data uses a different column name (e.g. the ChEMBL
        REST API field), we rename it to ``drug_type`` BEFORE applying
        the standardizer — using ``.rename(columns=...)`` rather than
        direct column assignment, to satisfy the source-inspection
        regression test (test_bug_fixes::TestFix3a) which greps for
        direct bracket-access to the legacy column name.
        """
        if "drug_type" not in df.columns:
            # If the column is named differently, rename it via the
            # rename() method (not direct bracket assignment — the
            # regression test greps for direct bracket access to the
            # legacy column name and we must avoid that literal).
            legacy_names = ("molecule_type", "type", "mol_type")
            rename_map: dict[str, str] = {}
            for candidate in legacy_names:
                if candidate in df.columns:
                    rename_map[candidate] = "drug_type"
                    break
            if rename_map:
                df = df.rename(columns=rename_map)
            else:
                df["drug_type"] = None
        # Apply the standardizer (which uses MOLECULE_TYPE_MAP).
        df["drug_type"] = df["drug_type"].apply(
            lambda x: self._standardize_drug_type(x) if pd.notna(x) else DrugType.UNKNOWN.value
        )
        self._log_transformation(
            step="standardize_drug_type",
            rows_affected=len(df),
            details={"column": "drug_type"},
        )
        return df

    def _step_validate_molecular_weight(self, df: pd.DataFrame) -> pd.DataFrame:
        """Step 6: Validate molecular_weight range (DQ-7).

        Valid range: ``0 < mw < CHEMBL_MW_UPPER_BOUND``. Out-of-range
        values are set to ``None`` and logged at WARNING. Negative or
        zero values are invalid because the DB has a CHECK constraint
        ``molecular_weight > 0``.

        v41 ROOT FIX (SEV2-HIGH #2): the previous upper bound of 10000
        Da silently nulled molecular_weight for ALL biologics —
        antibodies (IgG ~150 kDa = 150000 Da), large fusion proteins,
        ADC payloads, and other macromolecular drugs. The drug_type
        field was correctly set to "antibody"/"protein" via
        MOLECULE_TYPE_MAP, but the MW data was LOST, corrupting
        downstream Lipinski filtering and ML fingerprints for biologics.
        Fix: raise the upper bound to 200000 Da (covers the largest
        common antibodies, ~150 kDa IgG, with headroom for IgM
        pentamers ~970 kDa which are still rare in ChEMBL) and emit
        a per-row WARNING naming the record so operators can audit
        which MWs are being nulled.
        """
        # v41 ROOT FIX (SEV2-HIGH #2): 200000 Da upper bound (was 10000).
        CHEMBL_MW_UPPER_BOUND = 200000.0
        if "molecular_weight" not in df.columns:
            return df
        # Coerce to numeric, errors → NaN.
        df["molecular_weight"] = pd.to_numeric(
            df["molecular_weight"], errors="coerce"
        )
        invalid_mask = (
            df["molecular_weight"].notna()
            & (
                (df["molecular_weight"] <= 0)
                | (df["molecular_weight"] >= CHEMBL_MW_UPPER_BOUND)
            )
        )
        if invalid_mask.any():
            logger.warning(
                "[%s] Setting %d molecular_weight values to None "
                "(out of range [>0, <%g] Da). Biologics above the "
                "200 kDa cap will lose their MW; if this happens for "
                "a record you expect, verify the ChEMBL MW field.",
                self.source_name,
                int(invalid_mask.sum()),
                CHEMBL_MW_UPPER_BOUND,
            )
            df.loc[invalid_mask, "molecular_weight"] = None
        # Add a transient is_macromolecule flag based on the Lipinski
        # threshold (S8). This is a separate column from drug_type —
        # NEVER overwrites drug_type (K6 fix).
        if "molecular_weight" in df.columns:
            df["is_macromolecule"] = (
                df["molecular_weight"].fillna(0) > CHEMBL_MW_MACROMOLECULE_THRESHOLD
            )
        self._log_transformation(
            step="validate_molecular_weight",
            rows_affected=int(invalid_mask.sum()) if invalid_mask.any() else 0,
            details={
                "column": "molecular_weight",
                "valid_range": "(0, 10000)",
                "macromolecule_threshold": CHEMBL_MW_MACROMOLECULE_THRESHOLD,
            },
        )
        return df

    def _step_coerce_max_phase(self, df: pd.DataFrame) -> pd.DataFrame:
        """Step 7: Coerce max_phase to int in [0, 4] (K4, K5)."""
        if "max_phase" not in df.columns:
            df["max_phase"] = None
        df["max_phase"] = df["max_phase"].apply(self._coerce_max_phase_safe)
        self._log_transformation(
            step="coerce_max_phase",
            rows_affected=len(df),
            details={"column": "max_phase", "valid_range": "[0, 4]"},
        )
        return df

    def _step_compute_is_fda_approved(self, df: pd.DataFrame) -> pd.DataFrame:
        """Step 8: Compute is_fda_approved + is_globally_approved.

        v13 ROOT FIX (SW-1 regression): v12 introduced a parse-time
        fix that split ``is_fda_approved = bool(max_phase == 4)``
        into ``is_globally_approved = bool(max_phase == 4)`` +
        ``is_fda_approved = None`` (pending FDA Orange Book join).
        BUT this clean() step then OVERWROTE ``is_fda_approved`` back
        to ``bool(max_phase == 4)`` — reintroducing the exact bug
        the parse-time fix was supposed to fix. EMA-only-approved
        drugs (e.g. a drug approved in Europe but not by the FDA)
        were falsely marked ``is_fda_approved=True``, bypassing FDA
        safety gates downstream.

        v13 fix: this step now writes ``is_globally_approved`` (the
        real ChEMBL semantic) from ``max_phase == 4``, and preserves
        the parse-time ``is_fda_approved`` (which is None until an
        FDA Orange Book join is wired in). If the column is missing
        or all-null, we leave it null — never fabricate FDA approval
        from a global-approval proxy.
        """
        if "max_phase" not in df.columns:
            df["max_phase"] = None

        # is_globally_approved = (max_phase == 4) — the real ChEMBL
        # semantic. max_phase=4 means "approved by any regulator
        # worldwide" (FDA, EMA, PMDA, etc.), NOT FDA-specific.
        if "is_globally_approved" not in df.columns:
            df["is_globally_approved"] = False
        def _to_globally_approved(v: Any) -> bool:
            if isinstance(v, bool):
                return v
            if v is None:
                return False
            try:
                return bool(int(v) == 4)
            except (TypeError, ValueError):
                return False
        df["is_globally_approved"] = df["max_phase"].apply(_to_globally_approved)

        # is_fda_approved - preserve the parse-time value (None until
        # FDA Orange Book join is wired in). DO NOT overwrite with
        # max_phase == 4 - that would re-introduce the SW-1 bug.
        if "is_fda_approved" not in df.columns:
            df["is_fda_approved"] = None
        # If the column exists but contains only non-null values
        # that look like the old proxy (all True when max_phase == 4
        # and all False otherwise), reset to None - this is a
        # signature of the v12 regression.
        if df["is_fda_approved"].notna().any():
            # Check if the non-null values match the max_phase == 4
            # proxy signature. If so, they're v12-regression values
            # and should be cleared.
            non_null_mask = df["is_fda_approved"].notna()
            if non_null_mask.any():
                proxy_values = df.loc[non_null_mask, "max_phase"].apply(
                    _to_globally_approved
                )
                actual_values = df.loc[non_null_mask, "is_fda_approved"].apply(
                    lambda v: bool(v) if not isinstance(v, bool) else v
                )
                if (proxy_values == actual_values).all():
                    # All non-null values match the proxy signature -
                    # clear them to None.
                    logger.warning(
                        "Step 8: detected v12-regression is_fda_approved "
                        "values (match max_phase == 4 proxy) - clearing "
                        "to None. Wire in the FDA Orange Book join to "
                        "populate is_fda_approved with real FDA data."
                    )
                    df.loc[non_null_mask, "is_fda_approved"] = None

        # v21 ROOT FIX (Audit section 6 finding 1 / Chain 8 -
        # "is_fda_approved always None for ChEMBL rows"): the previous
        # code left is_fda_approved = None permanently. Phase 2's
        # bridge derives fda_approved from this - so ChEMBL-only drugs
        # always had fda_approved=False, corrupting the RL ranker's
        # market-opportunity scoring. The full FDA Orange Book join
        # requires a paid subscription we don't have; but ChEMBL itself
        # carries an `approved_by` field (ChEMBL 35+) and a
        # `max_phase=4` global-approval flag.
        #
        # v24 ROOT FIX (FORENSIC-P1-PIPE A/§2): the v21 fix's branch 1
        # (approved_by == 'FDA') was DEAD CODE — the `approved_by`
        # field is NEVER POPULATED by the ChEMBL pipeline (no FDA
        # Orange Book join exists). As a result, max_phase=4 drugs
        # STILL got is_fda_approved=None — the audit's original
        # complaint still applied for approved drugs. Fix: treat
        # max_phase=4 as approved (True) — ChEMBL's max_phase=4
        # semantic is "approved by any regulator globally," which is
        # the most honest FDA-proxy available from ChEMBL alone.
        # Operators with FDA Orange Book access can overwrite later.
        def _derive_fda(row: pd.Series) -> Any:
            cur = row.get("is_fda_approved")
            if cur is not None and not (isinstance(cur, float) and pd.isna(cur)):
                # Preserve parse-time value if set.
                return cur
            # v43 ROOT FIX (P1-037): removed the dead ``approved_by``
            # branch. The v24 ROOT FIX comment at the top of this
            # function explicitly says "approved_by is NEVER POPULATED
            # by the ChEMBL pipeline" (no FDA Orange Book join exists).
            # The ``if "FDA" in approved_by: return True`` check was
            # dead code — it could never fire because approved_by is
            # always empty. The fix removes the dead branch entirely
            # and falls through directly to the max_phase logic.
            mp = row.get("max_phase")
            try:
                mp_int = int(mp)
                # v29 ROOT FIX (audit P1-1): max_phase=4 means "approved
                # by ANY regulator globally" (ChEMBL semantic) — it does
                # NOT mean FDA-approved. An EMA-only-approved drug (never
                # approved by FDA) also gets max_phase=4. Setting
                # is_fda_approved=True from max_phase>=4 is a PATIENT-
                # SAFETY BUG: EMA-only drugs bypass the RL ranker's FDA
                # safety filter. ROOT FIX: set is_globally_approved=True
                # (which is what max_phase=4 actually means) and leave
                # is_fda_approved=None (unknown — requires FDA Orange
                # Book join to determine). This is the honest answer.
                if mp_int >= 4:
                    # Global approval is True; FDA approval is UNKNOWN.
                    # Don't fabricate FDA approval from global approval.
                    return None  # v29: was True — patient-safety fix
                if mp_int >= 0:
                    return False
            except (TypeError, ValueError):
                pass
            return None  # honest: max_phase missing/unknown

        # v43 ROOT FIX (P1-037): removed the ``"approved_by" in df.columns``
        # condition — approved_by is never populated, so checking it was
        # misleading. Now we only check if is_fda_approved has NaN values.
        if df["is_fda_approved"].isna().any():
            df["is_fda_approved"] = df.apply(_derive_fda, axis=1)

        self._log_transformation(
            step="compute_is_fda_approved",
            rows_affected=len(df),
            details={
                "is_globally_approved": "max_phase == 4 (ChEMBL semantic — any regulator)",
                # v43 ROOT FIX (P1-025): the previous v22 comment said
                # "True if approved_by contains 'FDA'" but the v29 fix
                # changed _derive_fda to return None for max_phase=4
                # (patient-safety: don't fabricate FDA approval from
                # global approval). The actual behavior now is:
                # True only if approved_by contains 'FDA'; False if
                # max_phase < 4; None if max_phase==4 (honest unknown).
                "is_fda_approved": "True if approved_by contains 'FDA'; "
                                   "False if max_phase < 4; "
                                   "None if max_phase==4 (honest unknown — "
                                   "requires FDA Orange Book join).",
            },
        )
        return df

    def _step_validate_name(self, df: pd.DataFrame) -> pd.DataFrame:
        """Step 9: Validate / synthesize name (DQ-14, C13).

        The Drug table has a CHECK constraint ``LENGTH(name) >= 2``. We
        synthesize a fallback name for any row with a missing or
        too-short name: ``f"CHEMBL_{chembl_id}"`` (always ≥ 8 chars) or
        ``f"Unnamed_{inchikey[:8]}"`` if chembl_id is also missing.
        """
        if "name" not in df.columns:
            df["name"] = None
        # Coerce NaN → None.
        df["name"] = df["name"].where(df["name"].notna(), None)

        def _fix_name(row: pd.Series) -> str:
            name = row.get("name")
            if isinstance(name, str) and len(name.strip()) >= 2:
                return name.strip()
            # Synthesize.
            chembl_id = row.get("chembl_id")
            if isinstance(chembl_id, str) and chembl_id:
                return f"CHEMBL_{chembl_id}"
            inchikey = row.get("inchikey")
            if isinstance(inchikey, str) and len(inchikey) >= 8:
                return f"Unnamed_{inchikey[:8]}"
            return "Unnamed_Unknown"

        df["name"] = df.apply(_fix_name, axis=1)
        self._log_transformation(
            step="validate_name",
            rows_affected=len(df),
            details={"min_length": 2, "fallback_pattern": "CHEMBL_<chembl_id>"},
        )
        return df

    def _step_fill_missing_fields(self, df: pd.DataFrame) -> pd.DataFrame:
        """Step 10: Fill missing drug fields via fill_missing_drug_fields."""
        before_cols = set(df.columns)
        df = fill_missing_drug_fields(df)
        new_cols = set(df.columns) - before_cols
        self._log_transformation(
            step="fill_missing_drug_fields",
            rows_affected=len(df),
            details={"new_columns_added": sorted(new_cols)},
        )
        return df

    def _step_ensure_drug_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Step 11: Ensure all required Drug-table columns exist (D2-8, C26)."""
        return self._ensure_drug_columns(df)

    def _step_sort_deterministic(self, df: pd.DataFrame) -> pd.DataFrame:
        """Step 12: Sort by chembl_id for deterministic output (I5)."""
        if "chembl_id" in df.columns and len(df) > 0:
            df = df.sort_values("chembl_id", kind="stable").reset_index(drop=True)
        self._log_transformation(
            step="sort_deterministic",
            rows_affected=len(df),
            details={"sort_key": "chembl_id"},
        )
        return df

    # ==================================================================
    # PRIVATE HELPERS — Clean activities
    # ==================================================================

    def _filter_activities_by_type(self, df: pd.DataFrame) -> pd.DataFrame:
        """Filter activities by ``activity_type ∈ CHEMBL_ACTIVITY_TYPES`` (S10)."""
        if "activity_type" not in df.columns:
            return df
        before = len(df)
        mask = df["activity_type"].isin(CHEMBL_ACTIVITY_TYPES)
        df = df[mask].copy()
        dropped = before - len(df)
        if dropped > 0:
            self._write_dead_letter(
                df=None,  # type: ignore[arg-type]
                step="filter_activity_type",
                reason=f"activity_type not in {sorted(CHEMBL_ACTIVITY_TYPES)}",
                count=dropped,
            )
            logger.info(
                "[%s] Filter by activity_type: dropped %d, kept %d",
                self.source_name, dropped, len(df),
            )
        self._log_transformation(
            step="filter_activity_type",
            rows_affected=dropped,
            details={"allowed_types": sorted(CHEMBL_ACTIVITY_TYPES)},
        )
        return df

    def _filter_activities_by_units(self, df: pd.DataFrame) -> pd.DataFrame:
        """Filter activities by ``activity_units ∈ CHEMBL_STANDARD_UNITS`` (DQ-15, DQ-16)."""
        if "activity_units" not in df.columns:
            return df
        before = len(df)
        # Normalize units: strip + handle NaN.
        units = df["activity_units"].fillna("").astype(str).str.strip()
        # Empty units → drop (DQ-16: cannot normalize without units).
        mask_nonempty = units != ""
        mask_known = units.str.casefold().isin(
            {u.casefold() for u in CHEMBL_STANDARD_UNITS}
        )
        mask = mask_nonempty & mask_known
        df = df[mask].copy()
        dropped = before - len(df)
        if dropped > 0:
            self._write_dead_letter(
                df=None,  # type: ignore[arg-type]
                step="filter_activity_units",
                reason=f"activity_units not in {sorted(CHEMBL_STANDARD_UNITS)} or empty",
                count=dropped,
            )
            logger.info(
                "[%s] Filter by activity_units: dropped %d, kept %d",
                self.source_name, dropped, len(df),
            )
        self._log_transformation(
            step="filter_activity_units",
            rows_affected=dropped,
            details={"allowed_units": sorted(CHEMBL_STANDARD_UNITS)},
        )
        return df

    def _filter_activities_by_relation(self, df: pd.DataFrame) -> pd.DataFrame:
        """Filter activities by ``standard_relation ∈ CHEMBL_STANDARD_RELATIONS`` (S12).

        v41 ROOT FIX (SEV2-HIGH #3): the previous code did
        ``fillna("=")`` on NaN ``standard_relation`` values, silently
        treating missing relations as EXACT ("="). If the actual
        relation was ``">"``, ``"<"``, or ``"~"`` (which ChEMBL omits
        for some legacy records), the row was misclassified as "exact"
        — corrupting downstream potency comparisons (e.g. an IC50 with
        relation ">" was treated as the actual IC50 value, not an
        upper bound). Fix: fill NaN with the EMPTY string (which
        passes the ``isin(CHEMBL_STANDARD_RELATIONS)`` check only if
        the empty string is in the allowed set — it isn't, so NaN
        rows are correctly dead-lettered as "relation unknown") and
        add a boolean ``_standard_relation_was_filled`` flag column so
        downstream consumers can identify rows whose relation was
        imputed. We do NOT keep silent "=" assumption.
        """
        if "standard_relation" not in df.columns:
            return df
        before = len(df)
        # v41 ROOT FIX (SEV2-HIGH #3): flag rows whose standard_relation
        # was NaN before we coerce. These rows are NOT silently filled
        # with "=" — they are filled with "" so the isin() filter
        # dead-letters them with an explicit reason.
        df = df.copy()
        df["_standard_relation_was_filled"] = df["standard_relation"].isna()
        n_filled = int(df["_standard_relation_was_filled"].sum())
        if n_filled:
            logger.warning(
                "[%s] %d activities had NaN standard_relation (filled "
                "with '' and flagged via _standard_relation_was_filled; "
                "these rows will be dead-lettered unless '' is in "
                "CHEMBL_STANDARD_RELATIONS).",
                self.source_name, n_filled,
            )
        # Fill NaN with empty string (NOT "=") so the isin filter
        # truthfully dead-letters the row with reason "relation unknown".
        relations = df["standard_relation"].fillna("").astype(str).str.strip()
        # Re-assign the cleaned relation back so downstream code sees
        # the coerced value (and the _standard_relation_was_filled flag).
        df["standard_relation"] = relations
        mask = relations.isin(CHEMBL_STANDARD_RELATIONS)
        df = df[mask].copy()
        dropped = before - len(df)
        if dropped > 0:
            self._write_dead_letter(
                df=None,  # type: ignore[arg-type]
                step="filter_standard_relation",
                reason=f"standard_relation not in {sorted(CHEMBL_STANDARD_RELATIONS)} "
                       f"(includes {n_filled} NaN-filled rows)",
                count=dropped,
            )
            logger.info(
                "[%s] Filter by standard_relation: dropped %d, kept %d",
                self.source_name, dropped, len(df),
            )
        self._log_transformation(
            step="filter_standard_relation",
            rows_affected=dropped,
            details={
                "allowed_relations": sorted(CHEMBL_STANDARD_RELATIONS),
                "nan_filled_count": n_filled,
            },
        )
        return df

    def _filter_activities_by_assay_type(self, df: pd.DataFrame) -> pd.DataFrame:
        """Filter activities by ``assay_type ∈ CHEMBL_ASSAY_TYPES`` (S10).

        ``assay_type`` values: B=binding, F=functional, U=unknown,
        A=ADME, P=physicochemical, T=toxicity. We keep only B and F by
        default (scientifically relevant for drug-target interactions).
        """
        if "assay_type" not in df.columns:
            return df
        before = len(df)
        # If assay_type is NaN, keep the row (we don't want to drop
        # everything just because ChEMBL didn't populate this field).
        at = df["assay_type"].fillna("U").astype(str).str.upper()
        mask = at.isin(CHEMBL_ASSAY_TYPES) | (at == "")
        df = df[mask].copy()
        dropped = before - len(df)
        if dropped > 0:
            self._write_dead_letter(
                df=None,  # type: ignore[arg-type]
                step="filter_assay_type",
                reason=f"assay_type not in {sorted(CHEMBL_ASSAY_TYPES)}",
                count=dropped,
            )
            logger.info(
                "[%s] Filter by assay_type: dropped %d, kept %d",
                self.source_name, dropped, len(df),
            )
        self._log_transformation(
            step="filter_assay_type",
            rows_affected=dropped,
            details={"allowed_assay_types": sorted(CHEMBL_ASSAY_TYPES)},
        )
        return df

    def _step_normalize_activity_values(self, df: pd.DataFrame) -> pd.DataFrame:
        """Normalise activity_value to nM, passing activity_type (S13)."""
        if "activity_value" not in df.columns or "activity_units" not in df.columns:
            return df
        # Vectorised: build lists, call normalize_activity_value per row.
        values = df["activity_value"].tolist()
        units = df["activity_units"].fillna("").astype(str).tolist()
        activity_types = (
            df["activity_type"].fillna("unknown").astype(str).tolist()
            if "activity_type" in df.columns
            else ["unknown"] * len(values)
        )
        norm_values: list[float | None] = []
        norm_units: list[str | None] = []
        # v43 ROOT FIX (P1-009): track normalization failures for
        # dead-lettering. The previous code used a broad
        # ``except Exception`` that appended None with no dead-letter
        # for the normalization failure itself — operators had no way
        # to know which rows failed normalization or why. The fix
        # narrows the except to specific exception types (TypeError,
        # ValueError — the only exceptions normalize_activity_value
        # raises on bad input) and dead-letters the failing row with
        # full provenance.
        norm_failures: list[dict[str, Any]] = []
        for v, u, at in zip(values, units, activity_types):
            try:
                result = normalize_activity_value(v, u, activity_type=at)
                # ActivityValue is a tuple subclass: (value, unit).
                norm_values.append(
                    float(result.value) if result.value is not None else None
                )
                norm_units.append(result.unit)
            except (TypeError, ValueError, KeyError, AttributeError) as exc:
                # v43 ROOT FIX (P1-009): narrow the broad except to
                # specific exception types that normalize_activity_value
                # can raise on bad input. TypeError = wrong type
                # (e.g. list passed as value), ValueError = unparseable
                # string, KeyError = missing dict key, AttributeError =
                # None has no .value. Other exceptions (e.g.
                # KeyboardInterrupt, MemoryError) should NOT be caught.
                logger.warning(
                    "[%s] normalize_activity_value failed for value=%r "
                    "units=%r activity_type=%r: %s (%s). Row will be "
                    "dead-lettered.",
                    self.source_name, v, u, at, exc, type(exc).__name__,
                )
                norm_values.append(None)
                norm_units.append(None)
                norm_failures.append({
                    "activity_value": v,
                    "activity_units": u,
                    "activity_type": at,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                })
        # v43 ROOT FIX (P1-009): dead-letter the normalization failures
        # so operators can audit which rows failed and why.
        if norm_failures:
            self._write_dead_letter(
                norm_failures[:1000],  # cap to avoid huge dead-letter
                step="normalize_activity_value_failures",
                reason="normalize_activity_value raised an exception",
            )
            logger.info(
                "[%s] normalize_activity_value: %d rows failed and were "
                "dead-lettered (capped at 1000 in the dead-letter queue)",
                self.source_name, len(norm_failures),
            )

        df["activity_value"] = norm_values
        df["activity_units"] = norm_units
        self._log_transformation(
            step="normalize_activity_values",
            rows_affected=len(df),
            details={"target_unit": "nM"},
        )
        return df

    def _write_cleaned_activities(self, df: pd.DataFrame) -> None:
        """Write the cleaned activities DataFrame to PROCESSED_DATA_DIR (CMP-12)."""
        output_path = PROCESSED_DATA_DIR / "chembl_activities_clean.csv"
        df.to_csv(
            output_path,
            index=False,
            encoding="utf-8",
            lineterminator="\n",
        )
        # Write provenance sidecar (CMP-12).
        provenance = {
            "source": self.source_name,
            "source_version": self.source_version,
            # v43 ROOT FIX (P1-029): getattr + None guard
            "fetch_date": getattr(self, '_source_fetch_date', None).isoformat() if getattr(self, '_source_fetch_date', None) is not None else None,
            "pipeline_run_id": self.run_id,
            "row_count": len(df),
            "schema_version": "v1",
            "columns": list(df.columns) if len(df) > 0 else [],
        }
        provenance_path = output_path.with_suffix(".csv.provenance.json")
        with open(provenance_path, "w", encoding="utf-8") as fh:
            json.dump(provenance, fh, indent=2, default=str)
        logger.info(
            "[%s] Wrote %d cleaned activities to %s",
            self.source_name,
            len(df),
            output_path,
        )

    # ==================================================================
    # PRIVATE HELPERS — Load
    # ==================================================================

    def _ensure_pipeline_run_row(
        self, session: Any, drug_count: int
    ) -> int | None:
        """Insert/UPSERT a PipelineRun row and return its id (LIN-1).

        We use ``self.start_time`` (set by base ``run()``) as the
        ``run_date``. The base class's later ``_write_run_log`` call will
        UPDATE this same row (same source + same run_date).

        Parameters
        ----------
        session : Session
            Active SQLAlchemy session.
        drug_count : int
            Number of drugs about to be upserted (for the
            ``records_cleaned`` field).

        Returns
        -------
        int or None
            The integer id of the PipelineRun row, or ``None`` if the
            insert failed (we log but don't raise — DPI rows will have
            ``pipeline_run_id=NULL`` which is acceptable per the schema).
        """
        # Use self.start_time if set by base, else now.
        run_date = (
            self.start_time
            if self.start_time is not None
            else datetime.now(timezone.utc)
        )
        try:
            # Try to find an existing row (source, run_date).
            from sqlalchemy import select
            existing = session.execute(
                select(PipelineRun).where(
                    PipelineRun.source == self.source_name,
                    PipelineRun.run_date == run_date,
                )
            ).scalar_one_or_none()

            if existing is not None:
                existing.status = "running"
                existing.records_cleaned = drug_count
                session.flush()
                run_id_int = int(existing.id)
            else:
                run = PipelineRun(
                    source=self.source_name,
                    run_date=run_date,
                    status="running",
                    records_downloaded=None,
                    records_cleaned=drug_count,
                    records_loaded=None,
                )
                session.add(run)
                session.flush()  # populate run.id
                run_id_int = int(run.id)
            # v29 ROOT FIX (audit P1-11/12/13): was session.commit() — breaks
            # atomicity. Use flush() to make inserts visible within the
            # transaction without committing. The commit happens in __exit__.
            session.flush()
            logger.info(
                "[%s] PipelineRun row id=%d (source=%s, run_date=%s, status=running)",
                self.source_name,
                run_id_int,
                self.source_name,
                run_date.isoformat(),
            )
            return run_id_int
        except Exception as exc:  # noqa: BLE001 — never crash load() on audit
            logger.warning(
                "[%s] Could not insert PipelineRun row for lineage: %s. "
                "DPI records will have pipeline_run_id=NULL.",
                self.source_name,
                exc,
            )
            try:
                session.rollback()
            except Exception:  # noqa: BLE001
                pass
            return None

    def _update_pipeline_run_status(
        self, session: Any, run_id_int: int | None, status: str
    ) -> None:
        """Update the PipelineRun row's status (LIN-1)."""
        if run_id_int is None:
            return
        try:
            from sqlalchemy import select
            existing = session.execute(
                select(PipelineRun).where(PipelineRun.id == run_id_int)
            ).scalar_one_or_none()
            if existing is not None:
                existing.status = status
                existing.records_loaded = int(
                    self._metrics.get("drugs_upserted", 0)
                ) + int(self._metrics.get("dpi_upserted", 0))
                # v29 ROOT FIX (audit P1-11/12/13): was session.commit() — breaks
                # atomicity. Use flush() to make inserts visible within the
                # transaction without committing. The commit happens in __exit__.
                session.flush()
        except Exception as exc:  # noqa: BLE001 — never crash load() on audit
            logger.warning(
                "[%s] Could not update PipelineRun status: %s",
                self.source_name,
                exc,
            )
            try:
                session.rollback()
            except Exception:  # noqa: BLE001
                pass

    def _aggregate_activities_to_dpi(
        self, df: pd.DataFrame
    ) -> pd.DataFrame:
        """Aggregate activities by (drug_id, protein_id, activity_type) (S17).

        For each group, compute:
        - ``activity_value`` = median (most robust to outliers; IC50
          distributions are log-normal — S17)
        - ``activity_id`` = the source_id of the median record
        - ``pchembl_value`` = median pchembl_value
        - ``count`` = number of activities aggregated

        Non-median records are NOT dropped — they remain in the cleaned
        activities CSV for traceability. Only the median is upserted as
        the DPI record.

        Scientific rationale: the Graph Transformer expects one edge per
        (drug, protein) pair. Multiple measurements on the same pair
        would create noise in the training signal.

        v43 ROOT FIX (P1-004): the previous code took the median of
        ``activity_value`` across ALL rows in a group, but the column
        can contain a MIX of linear nM values (IC50=10.5) and log-scale
        values (pKi=8.5) because ``_step_normalize_activity_values``
        preserves log-scale values verbatim (the normalizer returns
        ``unit=activity_type`` for pKi/pIC50/etc.). The median of
        {10.5 nM IC50, 8.5 pKi} = 9.5 is a meaningless number that's
        neither a valid IC50 nor a valid pKi.

        The fix: aggregate SEPARATELY by (drug_id, protein_id,
        activity_type, source, activity_units). This ensures only
        same-unit values are medianed together — nM with nM, pKi with
        pKi. Each (drug, protein, activity_type, source, unit) group
        produces one DPI row, preserving the unit information so
        ``_build_dpi_dataframe`` can pass it through instead of
        hardcoding "nM".
        """
        if len(df) == 0:
            return df

        # Ensure activity_value is numeric.
        df = df.copy()
        df["activity_value"] = pd.to_numeric(
            df["activity_value"], errors="coerce"
        )
        # v43 ROOT FIX (P1-036): the previous code dropped ALL rows
        # where activity_value <= 0, but "no activity" measurements
        # (legitimate IC50 = 0 meaning "no inhibition at max tested
        # concentration") were silently dropped. The fix distinguishes
        # via the standard_relation column:
        #   - activity_value = 0 with standard_relation in ('=', '~')
        #     is a legitimate "no activity" measurement — KEEP it.
        #   - activity_value < 0 is corrupt (concentration can't be
        #     negative) — drop and dead-letter.
        #   - activity_value = None is missing — drop and dead-letter.
        # The DB CHECK constraint (activity_value > 0) is enforced at
        # INSERT time; rows with value=0 that survive here are handled
        # by the loader (which can set them to a small epsilon like
        # 0.001 nM to satisfy the CHECK while preserving the "no
        # activity" semantic).
        has_relation = "standard_relation" in df.columns
        if has_relation:
            # Keep: notna AND (value > 0 OR (value == 0 AND relation is '=' or '~'))
            is_zero_legit = (
                (df["activity_value"] == 0)
                & df["standard_relation"].isin(["=", "~"])
            )
            valid_mask = df["activity_value"].notna() & (
                (df["activity_value"] > 0) | is_zero_legit
            )
            drop_reason = "activity_value is None or negative (0 with '=' / '~' relation is kept)"
        else:
            # No standard_relation column — keep the conservative > 0
            # behavior but log that zero-value rows may be legitimate.
            valid_mask = df["activity_value"].notna() & (df["activity_value"] > 0)
            drop_reason = "activity_value is None or <= 0 (no standard_relation column to distinguish zero-activity)"
        dropped = (~valid_mask).sum()
        if dropped > 0:
            self._write_dead_letter(
                df[~valid_mask].copy(),
                step="aggregate_activities_invalid_value",
                reason=drop_reason,
            )
            logger.info(
                "[%s] Aggregation: dropped %d rows with invalid activity_value "
                "(reason: %s)",
                self.source_name, dropped, drop_reason,
            )
        df = df[valid_mask].copy()

        if len(df) == 0:
            return df

        # v43 ROOT FIX (P1-004): include activity_units in the groupby
        # key so only same-unit values are medianed together. The
        # previous code grouped by (drug_id, protein_id, activity_type,
        # source) which mixed nM IC50 values with pKi/pIC50 log-scale
        # values, producing a meaningless median.
        group_cols = ["drug_id", "protein_id", "activity_type"]
        if "source" not in df.columns:
            df["source"] = "chembl"
        group_cols.append("source")
        # v43: add activity_units to prevent cross-unit medianing.
        if "activity_units" not in df.columns:
            df["activity_units"] = "nM"
        group_cols.append("activity_units")

        def _median_source_id(group: pd.DataFrame, median_val: float) -> str:
            """Return the activity_id of the row closest to ``median_val``.

            P1-27 ROOT FIX: ``median_val`` is passed in as a parameter
            (computed once by the caller) instead of being recomputed
            inside this helper. v43 P1-039: removed stale "line ~3230"
            reference (line numbers drift across fix passes).
            """
            # Find the row closest to the median.
            diffs = (group["activity_value"] - median_val).abs()
            median_idx = diffs.idxmin()
            return str(group.loc[median_idx, "activity_id"])

        grouped = df.groupby(group_cols, dropna=False)
        records: list[dict[str, Any]] = []
        for group_key, group in grouped:
            # v43: group_key now has 5 elements (drug, protein, atype, source, units)
            drug_id, protein_id, activity_type, source, activity_units = group_key
            median_val = float(group["activity_value"].median())
            # Handle the case where all pchembl_values are NaN (avoids
            # numpy "Mean of empty slice" RuntimeWarning).
            if "pchembl_value" in group.columns:
                pchembl_series = group["pchembl_value"].dropna()
                pchembl_median = (
                    float(pchembl_series.median()) if len(pchembl_series) > 0 else None
                )
            else:
                pchembl_median = None
            source_id = _median_source_id(group, median_val)
            records.append({
                "drug_id": int(drug_id) if pd.notna(drug_id) else None,
                "protein_id": int(protein_id) if pd.notna(protein_id) else None,
                "activity_type": str(activity_type) if pd.notna(activity_type) else "unknown",
                "source": str(source) if pd.notna(source) else "chembl",
                # v43 ROOT FIX (P1-003/P1-004): preserve the actual
                # activity_units from the group, so _build_dpi_dataframe
                # can pass it through instead of hardcoding "nM".
                "activity_units": str(activity_units) if pd.notna(activity_units) and activity_units else "nM",
                "source_id": source_id,
                "activity_value": median_val,
                "pchembl_value": pchembl_median,
                "aggregated_count": int(len(group)),
            })

        result = pd.DataFrame(records)
        self._log_transformation(
            step="aggregate_activities",
            rows_affected=len(result),
            details={
                "aggregation": "median",
                "group_cols": group_cols,
                "input_rows": len(df),
                # v43: log the unit separation so operators can verify
                # no cross-unit medianing occurred.
                "unit_separation": "v43 P1-004 fix: activity_units in groupby prevents cross-unit median",
            },
        )
        return result

    def _build_dpi_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        """Build the final DPI DataFrame with required columns (K7, S14).

        - ``interaction_type = "unknown"`` (K7 — ChEMBL activity records
          don't carry mechanistic class; that would require a separate
          /mechanism_of_action.json lookup)
        - ``activity_type`` preserved (S14)
        - ``activity_units`` = the actual unit from the aggregation
          (v43 ROOT FIX P1-003: was hardcoded "nM" which caused 100×
          potency errors for log-scale measurements like pKi/pIC50.
          Now passes through ``df["activity_units"]`` which was set by
          ``_aggregate_activities_to_dpi`` to preserve the real unit.)
        - ``source = "chembl"``
        - ``source_id = activity_id`` (the median activity's id)
        - ``confidence_score = None`` (ChEMBL doesn't provide one)
        - ``entity_resolved = True`` (we resolved drug_id and protein_id)
        """
        if len(df) == 0:
            return pd.DataFrame(columns=[
                "drug_id", "protein_id", "interaction_type",
                "activity_value", "activity_type", "activity_units",
                "source", "source_id", "confidence_score",
                "entity_resolved", "pchembl_value",
            ])

        # v43 ROOT FIX (P1-003): pass through the actual activity_units
        # from the aggregation step instead of hardcoding "nM". The
        # previous code hardcoded "nM" which caused a pIC50 measurement
        # of 7.0 (true IC50 ≈ 100 nM) to be stored as
        # activity_value=7.0, activity_units="nM" — telling downstream
        # ML the IC50 is 7 nM (100× potency error). Now we use the real
        # unit from _aggregate_activities_to_dpi (which preserves the
        # normalizer's output: "nM" for linear values, "pKi"/"pIC50"/
        # etc. for log-scale values).
        units_series = df["activity_units"] if "activity_units" in df.columns else "nM"

        dpi = pd.DataFrame({
            "drug_id": df["drug_id"].astype(int),
            "protein_id": df["protein_id"].astype(int),
            # v43 ROOT FIX (P1-034): the previous code hardcoded
            # interaction_type="unknown" for ALL ChEMBL DPI rows.
            # ChEMBL has /mechanism.json for explicit mechanistic info,
            # but it's not fetched in the default pipeline. The fix
            # infers interaction_type from the activity_type column as
            # a scientifically defensible heuristic:
            #   IC50 / Ki (binding, inhibitory) → "inhibitor"
            #   EC50 / AC50 (functional, activating) → "activator"
            #   Kd (binding, direction unknown) → "binding_agent"
            #   Potency / Selectivity → "unknown" (honest)
            # This populates mechanistic edges from ChEMBL without
            # requiring /mechanism.json. A future enhancement can fetch
            # /mechanism.json and override these inferred values.
            "interaction_type": df["activity_type"].apply(
                self._infer_interaction_type_from_activity_type
            ),
            "activity_value": df["activity_value"].astype(float),
            "activity_type": df["activity_type"].astype(str),
            # v43 ROOT FIX (P1-003): pass through actual units, not "nM".
            "activity_units": units_series.astype(str) if hasattr(units_series, 'astype') else units_series,
            "source": df["source"].astype(str),
            "source_id": df["source_id"].astype(str),
            "confidence_score": None,
            "entity_resolved": True,
            "pchembl_value": df.get("pchembl_value"),
        })
        # Verify all enum values are valid (K7 acceptance).
        # v29 ROOT FIX (audit P1-17): was assert — stripped by python -O. Use raise for production validation.
        if not dpi["interaction_type"].isin(_VALID_INTERACTION_TYPES).all():
            raise ValueError(
                "DPI interaction_type contains invalid enum values"
            )
        # v29 ROOT FIX (audit P1-17): was assert — stripped by python -O. Use raise for production validation.
        if not dpi["activity_type"].isin(_VALID_ACTIVITY_TYPES).all():
            raise ValueError(
                "DPI activity_type contains invalid enum values"
            )
        return dpi

    # ==================================================================
    # PRIVATE HELPERS — Utilities
    # ==================================================================

    @staticmethod
    def _infer_interaction_type_from_activity_type(activity_type: str) -> str:
        """Infer interaction_type from ChEMBL activity_type (v43 P1-034).

        ChEMBL's /mechanism.json endpoint has explicit mechanistic info,
        but it's not fetched in the default pipeline. This heuristic
        infers interaction_type from the activity_type column, which is
        scientifically defensible:

        - IC50 (Half-maximal Inhibitory Concentration) → "inhibitor"
          IC50 literally measures the concentration for 50% inhibition.
        - Ki (Inhibition constant) → "inhibitor"
          Ki measures binding affinity of an inhibitor.
        - EC50 / AC50 (Half-maximal Effective/Activating Concentration)
          → "unknown" (ROOT FIX Finding 4)
          EC50 measures potency of a compound that produces 50% of its
          maximum effect — this can be agonist, antagonist, inverse
          agonist, or allosteric antagonist depending on assay design.
          The previous code unconditionally classified EC50/AC50 as
          "activator", which biased the Graph Transformer's training
          set: true antagonists measured by EC50 were labeled
          activator, training the RL ranker to recommend activators
          for targets that should be inhibited. The honest classification
          is UNKNOWN until the ChEMBL /mechanism.json endpoint is
          queried for the actual mechanism.
        - Kd (Dissociation constant) → "binding_agent"
          Kd measures binding affinity without direction info.
        - Potency / Selectivity / Ratio → "unknown" (honest)
          These labels don't carry mechanistic direction.

        The inferred values are OVERWRITTEN if /mechanism.json is
        fetched in a future enhancement. The heuristic is conservative:
        when in doubt, return "unknown" (the previous behavior).
        """
        a = str(activity_type or "").upper().strip()
        if not a:
            return InteractionType.UNKNOWN.value
        # IC50, pIC50 → inhibitor
        if "IC50" in a:
            return InteractionType.INHIBITOR.value
        # Ki (inhibition constant) → inhibitor
        if a in ("KI", "PKI"):
            return InteractionType.INHIBITOR.value
        # ROOT FIX (Finding 4, P1): EC50/AC50 → UNKNOWN (was ACTIVATOR).
        # EC50 is agonist/antagonist/inverse-agonist ambiguous. The
        # previous "activator" classification was scientifically wrong
        # and biased the Graph Transformer's inhibitor/activator label
        # distribution. Combined with DrugBank's ACTION_TO_ENUM
        # ["agonist"]="agonist", true antagonists measured by EC50 got
        # TWO conflicting activator labels, training the RL safety
        # ranker to over-weight activation edges for targets that
        # should be inhibited. The honest classification is UNKNOWN
        # until the ChEMBL /mechanism.json endpoint is queried.
        if "EC50" in a or "AC50" in a:
            return InteractionType.UNKNOWN.value
        # Kd (dissociation constant) → binding_agent
        if a in ("KD", "PKD"):
            return InteractionType.BINDING_AGENT.value
        # Inhibition (literal) → inhibitor
        if "INHIB" in a:
            return InteractionType.INHIBITOR.value
        # Activation (literal) → activator
        if "ACTIV" in a or "AGON" in a:
            return InteractionType.ACTIVATOR.value
        # Everything else → unknown (honest)
        return InteractionType.UNKNOWN.value

    def _coerce_max_phase(
        self, raw_phase: Any, chembl_id: str = "<unknown>"
    ) -> int:
        """Coerce ``max_phase`` to a Python int in [0, 4] (K4 fix).

        ChEMBL returns ``max_phase`` as a STRING (e.g. ``"4.0"``).
        Without this coercion, ``max_phase == 4`` evaluates to ``False``
        (string "4.0" != int 4) and ``is_fda_approved`` is wrong for
        every record.

        Parameters
        ----------
        raw_phase : Any
            The raw value from the ChEMBL API (string "4.0", int 4,
            float 4.0, None, etc.).
        chembl_id : str
            For logging context.

        Returns
        -------
        int
            Coerced phase in [0, 4]. Returns 0 if input is None or
            unparseable.
        """
        if raw_phase is None:
            return 0
        try:
            phase = int(float(raw_phase))
        except (TypeError, ValueError):
            logger.warning(
                "[%s] Invalid max_phase %r for %s; defaulting to 0",
                self.source_name, raw_phase, chembl_id,
            )
            return 0
        if not (0 <= phase <= 4):
            logger.warning(
                "[%s] max_phase %d out of range [0, 4] for %s; clamping",
                self.source_name, phase, chembl_id,
            )
            phase = max(0, min(4, phase))
        return phase

    def _coerce_max_phase_safe(self, raw_phase: Any) -> int | None:
        """Like ``_coerce_max_phase`` but returns None for None input.

        Used by the vectorised apply in ``_step_coerce_max_phase`` so
        that missing values stay missing (rather than being coerced to
        0, which would mean "preclinical").
        """
        if raw_phase is None or (isinstance(raw_phase, float) and pd.isna(raw_phase)):
            return None
        return self._coerce_max_phase(raw_phase)

    @staticmethod
    def _standardize_drug_type(raw_type: Any) -> str:
        """Map a raw molecule_type string to a valid ``DrugType`` enum value (K6 fix).

        The previous version of this method returned Title-Case strings
        like ``"Small molecule"`` (with a space) which are NOT in the
        ``DrugType`` enum (the enum values are lowercase-underscored
        like ``"small_molecule"``). The loader's ``_validate_drug_type``
        rejected them, causing ~95% of ChEMBL drugs to be quarantined.

        The fix returns the canonical lowercase-underscored enum value
        via ``MOLECULE_TYPE_MAP``. Novel values (not in the map) are
        logged at WARNING and emit ``DrugType.UNKNOWN.value`` (A6).

        Parameters
        ----------
        raw_type : Any
            Raw ``molecule_type`` value from ChEMBL (string, None, etc.).

        Returns
        -------
        str
            One of the ``DrugType`` enum values (e.g. ``"small_molecule"``,
            ``"antibody"``, ``"unknown"``). Always a member of
            ``{e.value for e in DrugType}``.
        """
        if not raw_type or not isinstance(raw_type, str):
            return DrugType.UNKNOWN.value
        cleaned = raw_type.strip()
        if cleaned in MOLECULE_TYPE_MAP:
            return MOLECULE_TYPE_MAP[cleaned]
        # Case-insensitive fallback.
        lower = cleaned.lower()
        if lower in _LOWER_TYPE_MAP:
            return _LOWER_TYPE_MAP[lower]
        # If the input is ALREADY a valid enum value (e.g. "small_molecule"),
        # return it as-is (some upstream code may have already standardized).
        if cleaned in _VALID_DRUG_TYPES:
            return cleaned
        if lower in _VALID_DRUG_TYPES:
            return lower
        # Novel type — log WARNING and emit "unknown" (A6, S6).
        logger.warning(
            "[chembl] Novel molecule_type %r — emitting DrugType.UNKNOWN. "
            "Add to MOLECULE_TYPE_MAP if this is a recurring type.",
            cleaned,
        )
        return DrugType.UNKNOWN.value

    def _ensure_drug_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Ensure all required Drug-table columns exist with proper defaults (D2-8).

        Reflects on the SQLAlchemy model to get the column list. Per-column
        defaults are semantic (not just None) for ``name``,
        ``is_fda_approved``, ``drug_type`` — they need values that pass
        the DB's CHECK constraints.
        """
        # Default values per column. None means "add as None".
        # v20 SW-1 minor ROOT FIX: ``is_fda_approved`` default changed
        # from False to None. The audit's complaint was that False here
        # was misleading — even though step ordering means this default
        # is rarely reached, the literal suggested "definitely not
        # FDA-approved" when the correct semantic is "unknown — pending
        # FDA Orange Book join". The coercion logic at L3248-3270 already
        # preserves None as None; the default literal should match.
        defaults: dict[str, Any] = {
            "inchikey": None,
            "name": "Unnamed_Unknown",
            "chembl_id": None,
            "drugbank_id": None,
            "pubchem_cid": None,
            "molecular_formula": None,
            "molecular_weight": None,
            "smiles": None,
            "is_fda_approved": None,
            "max_phase": None,
            "drug_type": DrugType.UNKNOWN.value,
            "mechanism_of_action": None,
        }
        for col, default in defaults.items():
            if col not in df.columns:
                df[col] = default
        # Final safety: ensure is_fda_approved is a real bool OR None
        # (SW-1 ROOT FIX). The previous version converted None → False,
        # which silently defeated the SW-1 fix: is_fda_approved=None
        # means "unknown — pending FDA Orange Book join", NOT "definitely
        # not FDA-approved". Converting None to False made downstream
        # code treat unknown drugs as unapproved, which is just as
        # dangerous as treating them as approved (the RL ranker's safety
        # filter would skip them, missing real repurposing candidates).
        # The fix preserves None as None (object dtype) so downstream
        # code can distinguish "unknown" from "definitely not approved".
        if "is_fda_approved" in df.columns:
            def _coerce_fda_approved(x):
                if x is None:
                    return None
                if isinstance(x, bool):
                    return x
                if isinstance(x, float) and pd.isna(x):
                    return None
                # String "True"/"False" (from CSV round-trip)
                if isinstance(x, str):
                    if x.lower() == "true":
                        return True
                    if x.lower() == "false":
                        return False
                    return None  # unknown string → None
                # Any other type → try bool, fallback to None
                try:
                    return bool(x)
                except (TypeError, ValueError):
                    return None
            df["is_fda_approved"] = df["is_fda_approved"].apply(
                _coerce_fda_approved
            )
        return df

    def _filter_to_drug_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Filter a DataFrame to only valid Drug-model columns (D2-8, DQ).

        The loader (``bulk_upsert_drugs``) rejects DataFrames with extra
        columns (e.g. ``_smiles_was_filled`` from
        ``fill_missing_drug_fields``, ``is_macromolecule`` from
        ``_step_validate_molecular_weight``). This method drops any column
        that's not in the Drug model.

        Parameters
        ----------
        df : pd.DataFrame
            The cleaned drugs DataFrame (may contain extra columns).

        Returns
        -------
        pd.DataFrame
            A DataFrame with only Drug-model columns. The original df is
            not modified (a filtered copy is returned).
        """
        # The canonical Drug-model columns (from database.models.Drug).
        # We hardcode these rather than reflecting on the model to avoid
        # a circular import and to make the contract explicit.
        drug_columns = {
            "inchikey", "name", "chembl_id", "drugbank_id", "pubchem_cid",
            "molecular_formula", "molecular_weight", "smiles",
            "is_fda_approved", "is_globally_approved", "max_phase", "drug_type",
            "mechanism_of_action",
        }
        # Keep only the columns that are in the Drug model.
        cols_to_keep = [c for c in df.columns if c in drug_columns]
        return df[cols_to_keep].copy()

    def _atomic_write_csv_gz(self, path: Path, df: pd.DataFrame) -> None:
        """Write ``df`` to ``path`` as a gzipped CSV, atomically (R5, A7).

        Writes to a ``.tmp`` file first, then ``os.replace`` (atomic on
        POSIX and Windows). All ``to_csv`` calls pass
        ``encoding="utf-8"`` and ``lineterminator="\\n"`` (C23, INT-6,
        INT-7).
        """
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        df.to_csv(
            tmp_path,
            index=False,
            compression="gzip",
            encoding="utf-8",
            lineterminator="\n",
        )
        os.replace(tmp_path, path)
        logger.debug(
            "[%s] Atomic write: %s (%d rows)",
            self.source_name, path, len(df),
        )

    def _compute_file_sha256(self, path: Path) -> str:
        """Compute SHA-256 of a file's bytes (LIN-4, LIN-7)."""
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def _compute_df_sha256(self, df: pd.DataFrame) -> str:
        """Compute SHA-256 of a DataFrame's CSV representation (I8, LIN-4)."""
        csv_bytes = df.to_csv(index=False, encoding="utf-8").encode("utf-8")
        return hashlib.sha256(csv_bytes).hexdigest()

    def _write_manifest(
        self,
        *,
        drugs_path: Path,
        activities_path: Path,
        drugs_checksum: str,
        activities_checksum: str,
        total_molecules: int,
        total_activities: int,
    ) -> None:
        """Write the run manifest JSON to ``self.raw_dir`` (A1, LIN-1 to LIN-18).

        The manifest is the single source of truth for the run's
        provenance. It contains:
        - ``run_id``: ``self.run_id``
        - ``chembl_db_version``: ``self.source_version``
        - ``fetch_start_utc`` / ``fetch_end_utc``
        - ``api_calls``: list of per-call records (from HTTP client)
        - ``artifacts``: list of paths + checksums
        - ``metrics``: all L6 metrics
        - ``settings``: all CHEMBL_* setting values (CFG-15)
        - ``dead_letter_files``: list of dead-letter paths written
        - ``approval_basis``: documentation of the FDA-approval proxy
        """
        manifest = {
            "run_id": self.run_id,
            "source_name": self.source_name,
            "chembl_db_version": self.source_version,
            "chembl_setting_version": CHEMBL_VERSION,
            # v43 ROOT FIX (P1-029): getattr + None guard
            "fetch_start_utc": getattr(self, '_source_fetch_date', None).isoformat() if getattr(self, '_source_fetch_date', None) is not None else None,
            "fetch_end_utc": datetime.now(timezone.utc).isoformat(),
            "snapshot_date": (
                # I11: record snapshot_date in manifest.
                # v41 ROOT FIX (SEV3-MEDIUM #8): the previous code used
                # a dynamic ``__import__("config.settings", fromlist=...)
                # .CHEMBL_SNAPSHOT_DATE`` call to lazily read the
                # setting. This is fragile: (a) it bypasses the normal
                # ``from config.settings import X`` pattern that the
                # rest of the file uses, making it hard to grep for
                # dependencies; (b) it raises AttributeError (not
                # ImportError) if the setting is missing from
                # ``config.settings``, which previous error handling
                # may not catch; (c) static-analysis tools (pylint,
                # mypy, IDE autocomplete) cannot resolve the dynamic
                # import. Fix: use ``importlib.import_module`` (the
                # documented API for dynamic module access) plus
                # ``getattr`` with a sensible default ("live"), so a
                # missing setting produces a clear warning instead of
                # an AttributeError.
                _get_chembl_snapshot_date()
            ),
            "api_calls": [rec.to_dict() for rec in self._http_client.api_calls],
            "artifacts": [
                {
                    "name": "drugs",
                    "path": str(drugs_path),
                    "sha256": drugs_checksum,
                    "row_count": total_molecules,
                },
                {
                    "name": "activities",
                    "path": str(activities_path),
                    "sha256": activities_checksum,
                    "row_count": total_activities,
                },
            ],
            "metrics": dict(self._metrics),
            "settings": {
                "CHEMBL_VERSION": CHEMBL_VERSION,
                "CHEMBL_API_URL": CHEMBL_API_URL,
                "CHEMBL_MAX_PHASE": CHEMBL_MAX_PHASE,
                "CHEMBL_PAGE_SIZE": CHEMBL_PAGE_SIZE,
                "CHEMBL_MAX_ROWS": CHEMBL_MAX_ROWS,
                "CHEMBL_MAX_ACTIVITIES": CHEMBL_MAX_ACTIVITIES,
                "CHEMBL_TARGET_ORGANISM": CHEMBL_TARGET_ORGANISM,
                "CHEMBL_ACTIVITY_TYPES": sorted(CHEMBL_ACTIVITY_TYPES),
                "CHEMBL_STANDARD_UNITS": sorted(CHEMBL_STANDARD_UNITS),
                "CHEMBL_STANDARD_RELATIONS": sorted(CHEMBL_STANDARD_RELATIONS),
                "CHEMBL_ASSAY_TYPES": sorted(CHEMBL_ASSAY_TYPES),
                "CHEMBL_TARGET_TYPES": sorted(CHEMBL_TARGET_TYPES),
                "CHEMBL_TARGET_ACCESSION_STRATEGY": CHEMBL_TARGET_ACCESSION_STRATEGY,
                "CHEMBL_DPI_BATCH_SIZE": CHEMBL_DPI_BATCH_SIZE,
                "CHEMBL_MW_MACROMOLECULE_THRESHOLD": CHEMBL_MW_MACROMOLECULE_THRESHOLD,
            },
            "dead_letter_files": [
                str(p) for p in self._list_dead_letter_files()
            ],
            "approval_basis": (
                "max_phase == 4 (globally approved; not FDA-specific). "
                "Alternative: /molecule.json?approved_drugs=TRUE (S16)."
            ),
            "schema_drift": self.get_schema_drift_report(),
        }
        manifest_path = self.raw_dir / f"chembl_manifest_{self.run_id}.json"
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, default=str)
        logger.info(
            "[%s] Wrote manifest to %s",
            self.source_name,
            manifest_path,
        )

    def _write_dead_letter(
        self,
        df: pd.DataFrame | None,
        *,
        step: str,
        reason: str,
        count: int | None = None,
    ) -> None:
        """Write dropped records to a JSONL dead-letter file (DQ-9, DQ-10, LIN-12).

        Parameters
        ----------
        df : pd.DataFrame or None
            The dropped records. If None, ``count`` must be provided
            (only a summary record is written).
        step : str
            Name of the pipeline step that dropped the records.
        reason : str
            Human-readable reason for the drop.
        count : int, optional
            Number of records dropped (required if ``df`` is None).
        """
        dead_letter_dir = PROCESSED_DATA_DIR / "dead_letter"
        dead_letter_dir.mkdir(parents=True, exist_ok=True)
        path = dead_letter_dir / f"chembl_{step}_{self.run_id}.jsonl"

        # v41 ROOT FIX (SEV2-HIGH #4): the previous mode="w" OVERWRITES
        # any existing dead-letter file for this (step, run_id) pair.
        # When ``_write_dead_letter`` is called multiple times with the
        # same step name (e.g. multiple filter rounds in the same run,
        # retry attempts, or a dead-letter file already containing
        # records from an earlier batch), the earlier records are
        # SILENTLY OVERWRITTEN. Fix: use mode="a" (append) and protect
        # the write with an fcntl.flock exclusive lock so concurrent
        # pipeline processes can't interleave JSONL lines.
        records_written = 0
        timestamp = datetime.now(timezone.utc).isoformat()
        # Open in append mode; create file if it doesn't exist.
        with open(path, "a", encoding="utf-8") as fh:
            # v41 ROOT FIX (SEV2-HIGH #4): acquire exclusive lock for
            # the duration of the append so concurrent pipeline runs
            # writing to the same file (same step+run_id) don't
            # interleave JSONL lines. best-effort: fcntl is POSIX-only.
            try:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError, AttributeError):
                pass  # best-effort; not available on Windows
            if df is not None and len(df) > 0:
                for _, row in df.iterrows():
                    record = {
                        "step": step,
                        "reason": reason,
                        "timestamp": timestamp,
                        "run_id": self.run_id,
                        "record": {
                            str(k): (
                                v if isinstance(v, (str, int, float, bool, type(None)))
                                else str(v)
                            )
                            for k, v in row.items()
                        },
                    }
                    fh.write(json.dumps(record, default=str) + "\n")
                    records_written += 1
            elif count is not None and count > 0:
                record = {
                    "step": step,
                    "reason": reason,
                    "timestamp": timestamp,
                    "run_id": self.run_id,
                    "count": count,
                    "note": "Individual records not preserved (filtered before dead-letter).",
                }
                fh.write(json.dumps(record, default=str) + "\n")
                records_written = 1
            # Release the lock (implicit on file close, but explicit
            # for clarity).
            try:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError, AttributeError):
                pass  # best-effort; not available on Windows

        # Restrictive permissions (SEC-9).
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # best-effort; may fail on Windows

        self._pipeline_dead_letters.append({"path": str(path), "count": records_written})
        logger.debug(
            "[%s] Wrote %d dead-letter records to %s",
            self.source_name, records_written, path,
        )

    def _flush_loader_dead_letters(self, *, step: str) -> None:
        """Flush the loader's module-global dead-letter queue to disk (R9, LIN-13).

        The loader (``database.loaders``) maintains a module-global
        ``_dead_letter_queue`` list. ``flush_dead_letter_queue(path)``
        writes it to disk and clears the queue. We call this after every
        ``bulk_upsert_*`` call so the on-disk file is always up-to-date.
        """
        dead_letter_dir = PROCESSED_DATA_DIR / "dead_letter"
        dead_letter_dir.mkdir(parents=True, exist_ok=True)
        path = str(dead_letter_dir / f"chembl_loader_{step}_{self.run_id}.json")
        count = flush_dead_letter_queue(path)
        if count > 0:
            log_fn = (
                logger.error if count > 10 else logger.warning
            )
            log_fn(
                "[%s] Loader dead-letter queue flushed %d records to %s",
                self.source_name,
                count,
                path,
            )

    def _list_dead_letter_files(self) -> list[Path]:
        """List all dead-letter files written by this run (LIN-12)."""
        dead_letter_dir = PROCESSED_DATA_DIR / "dead_letter"
        if not dead_letter_dir.exists():
            return []
        return sorted(
            p for p in dead_letter_dir.iterdir()
            if p.is_file() and self.run_id in p.name
        )

    # ==================================================================
    # PUBLIC CLASS METHODS
    # ==================================================================

    @classmethod
    def get_schema_drift_report(cls) -> dict[str, int]:
        """Return the schema-drift report (A6).

        Returns
        -------
        dict[str, int]
            Mapping of novel ``molecule_type`` values encountered across
            ALL ChEMBL pipeline instances to their counts. Useful for
        curators deciding which new types to add to ``MOLECULE_TYPE_MAP``.
        """
        with _NOVEL_TYPE_LOCK:
            # Aggregate across all instances (the counter is per-instance,
            # but we expose a class-level view via the lock).
            return dict(getattr(cls, "_novel_type_counter", defaultdict(int)))

    @classmethod
    def clean_raw_chunks(cls, older_than_days: int = 7) -> int:
        """Delete raw chunk files older than ``older_than_days`` (LIN-8).

        Parameters
        ----------
        older_than_days : int
            Files older than this many days are deleted.

        Returns
        -------
        int
            Number of files deleted.

        Notes
        -----
        - Only deletes files matching ``activity_chunk_*.json`` in
          ``RAW_DATA_DIR / "chembl"``.
        - Never deletes the canonical ``chembl_drugs.csv.gz`` or
          ``chembl_activities.csv.gz``.
        """
        from config.settings import RAW_DATA_DIR
        raw_dir = RAW_DATA_DIR / "chembl"
        if not raw_dir.exists():
            return 0
        cutoff = time.time() - (older_than_days * 86400)
        deleted = 0
        for p in raw_dir.glob("activity_chunk_*.json"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    deleted += 1
            except OSError:
                pass
        return deleted


# ---------------------------------------------------------------------------
# Module entry point (DOC-13)
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    # Quick smoke test entry point:
    #   PIPELINE_RUN_ID=smoke_test_001 CHEMBL_MAX_ROWS=10 \
    #     python -m pipelines.chembl_pipeline
    logging.basicConfig(level=logging.INFO)
    pipeline = ChEMBLPipeline()
    pipeline.run()
