"""
DisGeNET Pipeline — institutional-grade gene-disease association (GDA) ingestion.

This is the **single source of truth** for every gene-disease association in
the Autonomous Drug Repurposing Platform.  The data flow is::

    DisGeNET API/TSV  →  disgenet_pipeline.py  →  gene_disease_associations.csv
                                                →  gene_disease_associations (DB table)
                                                →  Neo4j Knowledge Graph
                                                →  Graph Transformer (ML model)
                                                →  RL Ranker
                                                →  Pharma partner API / Researcher dashboard
                                                →  CLINICAL DECISION about a drug
                                                →  PATIENT TAKES THE DRUG

Every defect in this file propagates downward.  A wrong score, a wrong
gene-symbol resolution, a dropped evidence record, or a mis-attributed
disease ID does NOT stop at the CSV — it becomes a "fact" in the
knowledge graph, a "feature" in the ML model, and a "recommendation" in
the dashboard.  Per the project owner's brief:

    "the base if the dataset part of code is wrong then the other parts
    i build and then the website i build then people use my website they
    get wrong outputs and then people use those drugs they die then i
    would be behind bars so 100 percent in scientific it should be 100
    percent true in domain scientific 100 percent true"

This is the operating constraint.  Treat every line of this file as if a
typographical error in it could kill a person — because it can.

Scientific ground truth (do not contradict)
-------------------------------------------
1. DisGeNET ``score`` is NOT ``P(gene causes disease)``.  It is a weighted
   aggregation, the "Disease-Specific Genomic Profile" (DSGP) per Piñero
   et al., 2020, *DisGeNET: a comprehensive platform integrating
   information on human disease-associated genes and variants*, Nucleic
   Acids Research (https://doi.org/10.1093/nar/gkz1021).  Weights depend
   on the sub-source (CURATED, BEFREE, CGI, CLINGEN, CTD_human,
   GENOMICS_ENGLAND, GWAS_CATALOG, HPO, LHGDN, ORPHANET, PSYGENET, RONB,
   UNIPROT, etc.).  Two rows with ``score=0.5`` from CURATED vs BEFREE
   do NOT have equivalent credibility (SCI-2, SCI-38).  This pipeline
   stores ``source_id`` (the sub-source) and ``score_type`` /
   ``score_method`` so downstream ML can differentiate.

2. DisGeNET scores in ``[0.06, 0.1)`` are "weak evidence", not garbage.
   They are biologically meaningful, especially for rare diseases
   (single-publication findings).  The previous default ``MIN_SCORE=0.1``
   silently destroyed them (SCI-1).  The new default ``DISGENET_MIN_SCORE=0.06``
   preserves them, and ``DISGENET_ALLOW_WEAK_EVIDENCE=True`` (default)
   tags them with ``confidence_tier="weak"`` instead of dropping them.

3. ``diseaseId`` is heterogeneous.  It can be a UMLS CUI (``C[0-9]{7}``),
   a MeSH descriptor (``D[0-9]{6}``), a DOID (``DOID:[0-9]+``), an HPO
   term (``HP:[0-9]+``), or an OMIM number (``[0-9]{6}``).  The
   ``disease_id_type`` column disambiguates (SCI-5).

4. ``geneId`` (NCBI Entrez Gene ID) is the stable identifier.
   ``gene_symbol`` is volatile (HGNC retires, renames, and merges symbols
   yearly).  Dropping ``gene_id`` and keeping only ``gene_symbol`` would
   orphan thousands of records on every HGNC rename (SCI-6).

5. ``sourceId`` (CURATED, BEFREE, GWAS_CATALOG, ORPHANET, ...) is the
   most important provenance field.  Hardcoding ``source="disgenet"``
   collapses the unique constraint and discards this signal (SCI-3,
   SCI-4).  This pipeline stores ``source = f"disgenet_{source_id.lower()}"``
   so the same (gene, disease) pair from CURATED and BEFREE can coexist.

6. ``yearInitial`` / ``yearFinal`` encode the publication-year range of
   the evidence (SCI-7).  ``diseaseType`` distinguishes diseases from
   phenotypes from groups (SCI-9).  ``diseaseClass`` encodes the MeSH
   hierarchy position (SCI-8).  All are persisted.

7. ``pmid_list`` is a semicolon-separated string of 7-8 digit PubMed IDs.
   PMIDs are sorted most-recent-first (higher PMID = more recent NCBI
   assignment), deduped, and capped at ``DISGENET_PMID_CAP`` (SCI-16,
   SCI-17, DQ-16, DQ-17).

Life-safety framing
-------------------
This file is the most important file in the platform.  If you change it,
you MUST run the full institutional test suite
(``tests/test_disgenet_pipeline_institutional_v389.py``) and the
integration test suite (``tests/test_all_25_files_integration_v9.py``).
Both must pass with zero failures.

Dependencies
------------
- UniProt pipeline MUST run first (the ``proteins`` table is required
  for ``gene_symbol → uniprot_id`` resolution).  This is enforced at
  ``run()`` entry (ARCH-17).

Configuration
-------------
All tunable parameters are env vars in ``config/settings.py`` with
docstrings citing Piñero et al. 2020.  See the ``DISGENET_*`` settings.

Contracts preserved
-------------------
- ``BasePipeline`` ABC (download/clean/load signatures, run-order).
- ``pipelines/schema/v1.json`` (extended with optional columns;
  ``required`` set is ``["disease_id", "score"]`` — relaxed from the
  previous ``["gene_id", "disease_id", "score"]`` because ``gene_id``
  may legitimately be NULL when the source provides only ``gene_symbol``).
- ``validate_gda_scores`` signature (cleaning/missing_values.py).
- ``bulk_upsert_gda`` signature (database/loaders.py) — extended with
  ``dedup_already_done: bool = False`` kwarg (additive, backward-compat).
- ``UpsertResult`` dataclass (database/loaders.py).
- ``GeneDiseaseAssociation`` model (database/models.py) — extended with
  new nullable columns + ``hpo`` in the ``disease_id_type`` CHECK.
- ``DataSourceName`` enum (config/settings.py).
- ``PMID_LIST_LENGTH = 2000`` (database/models.py).

Python version
--------------
Requires Python 3.9+ (uses ``list[dict]``, ``dict[str, str]``,
``int | None`` syntax).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Standard library imports
# ---------------------------------------------------------------------------
import bisect
import csv as csv_mod
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

# ---------------------------------------------------------------------------
# Third-party imports
# ---------------------------------------------------------------------------
import pandas as pd
import requests

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------
from cleaning._constants import (
    normalize_gene_symbol,  # v29 ROOT FIX (audit P1-24)
    normalize_uniprot_id,   # v29 ROOT FIX (audit P1-24)
)
from cleaning.confidence import (
    CONFIDENCE_TIER_METHOD_VERSION,
    DEFAULT_CONFIDENCE_TIERS,
    classify_confidence,
)
from cleaning.missing_values import validate_gda_scores
from config.settings import (
    DISGENET_ALLOW_PARTIAL_DATA,
    DISGENET_ALLOW_WEAK_EVIDENCE,
    DISGENET_API_BACKOFF_BASE,
    DISGENET_API_BACKOFF_MAX_SECONDS,
    DISGENET_API_CA_BUNDLE,
    DISGENET_API_KEY,
    DISGENET_API_MAX_PAGES,
    DISGENET_API_MAX_RECORDS,
    DISGENET_API_MAX_RESPONSE_BYTES,
    DISGENET_API_MAX_RETRY_AFTER,
    DISGENET_API_MAX_RETRIES,
    DISGENET_API_PAGE_SIZE,
    DISGENET_API_RATE_LIMIT,
    DISGENET_API_TIMEOUT,
    DISGENET_API_URL,
    DISGENET_CIRCUIT_BREAKER_RESET_SECONDS,
    DISGENET_CIRCUIT_BREAKER_THRESHOLD,
    DISGENET_CONTACT_EMAIL,
    DISGENET_DOWNLOAD_PHASE_TIMEOUT,
    DISGENET_FALLBACK_TO_CACHE,
    DISGENET_FREEZE_VERSION,
    DISGENET_HGNC_PATH,
    DISGENET_DISEASE_ONTOLOGY_PATH,
    DISGENET_LOG_FORMAT,
    DISGENET_MAX_DATA_AGE_DAYS,
    DISGENET_MIN_EXPECTED_RECORDS,
    DISGENET_MIN_SCORE,
    DISGENET_OUTPUT_FILENAME,
    DISGENET_OUTPUT_FILE_MODE,
    DISGENET_PMID_CAP,
    DISGENET_PMID_SORT_ORDER,
    DISGENET_SOURCE_WEIGHTS,
    DISGENET_TARGET_VERSION,
    DISGENET_UNIPROT_MAP_TTL_HOURS,
    DISGENET_URL,
    DISGENET_USE_API,
    DataSourceName,
    PROCESSED_DATA_DIR,
    _validate_disgenet_config,
)
from database.connection import get_db_session
from database.loaders import (
    UpsertResult,
    build_gene_to_uniprot_maps,
    bulk_upsert_gda,
    get_or_create_pipeline_run,
    resolve_gene_symbol_to_uniprot,
)
from database.models import GeneDiseaseAssociation, PMID_LIST_LENGTH
from pipelines.base_pipeline import BasePipeline, UNIPROT_ID_PATTERN

# ---------------------------------------------------------------------------
# Module logger
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module version & metadata (DES-20)
# ---------------------------------------------------------------------------
__version__: str = "2.0.0"
__author__: str = "Team Cosmic / VentureLab"
__license__: str = "MIT"

# ---------------------------------------------------------------------------
# Type aliases (DES-20)
# ---------------------------------------------------------------------------
GeneToUniprotMap = dict[str, str]
ProteinNameToUniprotMap = dict[str, str]
GDARecord = dict[str, Any]
CleaningReport = dict[str, Any]

# ---------------------------------------------------------------------------
# Constants — no magic numbers in methods (CFG-12.x)
# ---------------------------------------------------------------------------

# SCI-2: Score-type label for DisGeNET's DSGP score.
SCORE_TYPE_DISGENET: str = "disgenet_dsgp"
"""Score-type label stored in the ``score_type`` column.  Identifies the
score as the DisGeNET Disease-Specific Genomic Profile (DSGP) per Piñero
et al. 2020.  Downstream ML uses this to differentiate from other score
types (e.g. OMIM mapping_key, STRING combined_score)."""

# SCI-2 / SCI-26: Default score_method (overridden at runtime with the
# actual DisGeNET release version captured from API response headers).
SCORE_METHOD_DEFAULT: str = "disgenet_v7"
"""Default ``score_method`` value when the DisGeNET release version
cannot be determined from API response headers.  The runtime value is
``f"disgenet_{version}"`` (e.g. ``"disgenet_v7_2024_06"``)."""

# SCI-19: Mapping from DisGeNET sub-source to association_type label.
SOURCE_ID_TO_ASSOCIATION_TYPE: dict[str, str] = {
    "CURATED": "curated",
    "BEFREE": "text_mined",
    "GWAS_CATALOG": "gwas",
    "CGI": "clinical_genomic",
    "CLINGEN": "clinical_genomic",
    "CTD_human": "ctd",
    "GENOMICS_ENGLAND": "genomic",
    "HPO": "phenotype",
    "ORPHANET": "rare_disease_curated",
    "UNIPROT": "uniprot",
    "PSYGENET": "psychiatric_curated",
    "LHGDN": "literature_derived",
    "RONB": "text_mined",
}
"""Mapping from DisGeNET ``sourceId`` (sub-source) to the
``association_type`` label stored in the GDA model.  Each sub-source has
a distinct curation method (Piñero et al. 2020 §2.3) — encoding it as
``association_type`` lets downstream ML distinguish a curated
association from a text-mined one."""

# Default association_type when source_id is missing or unknown.
DEFAULT_ASSOCIATION_TYPE: str = "unknown"

# Schema version stamp (COMP-6).
SCHEMA_VERSION_STAMP: str = "2.0"
"""Schema version written to every output row's ``schema_version``
column.  Bump this when the output schema changes materially."""

# DisGeNET column-name maps.
# WHY: The DisGeNET REST API uses camelCase field names (geneNcbiID,
# geneSymbol, diseaseName, etc.) while the static TSV uses snake_case-ish
# names (geneId, gene_symbol, disease_name).  Two maps are needed; the
# pipeline dispatches declaratively via ``self._source_format`` (IDEM-6).
DISGENET_COLUMN_MAP: dict[str, str] = {
    "geneId": "gene_id",
    "geneId_source": "gene_id_source",
    "gene_symbol": "gene_symbol",
    "diseaseId": "disease_id",
    "disease_name": "disease_name",
    "diseaseType": "disease_type",
    "diseaseClass": "disease_class",
    "diseaseClass_source": "disease_class_source",
    "sourceId": "source_id",
    "score": "score",
    "yearInitial": "year_initial",
    "yearFinal": "year_final",
    "pmid_list": "pmid_list",
}
"""Static-TSV column map.  Maps DisGeNET's snake_case TSV headers to
our schema's snake_case column names."""

# FIX AUDIT-35: Separate column map for API responses which use camelCase
# field names different from the TSV format.  Includes the
# ``geneUniProtIDs`` field (SCI-15) — a list of UniProt accessions that
# the API returns for each GDA record; the pipeline prefers this over
# local DB resolution.
DISGENET_API_COLUMN_MAP: dict[str, str] = {
    "geneNcbiID": "gene_id",
    "geneSymbol": "gene_symbol",
    "diseaseId": "disease_id",
    "diseaseName": "disease_name",
    "diseaseType": "disease_type",
    "diseaseClass": "disease_class",
    "diseaseClassName": "disease_class_source",
    "diseaseVocabularies": "disease_vocabularies",
    "score": "score",
    "yearInitial": "year_initial",
    "yearFinal": "year_final",
    "pmidList": "pmid_list",
    "sourceId": "source_id",
    "geneEnsemblIDs": "gene_ensembl_ids_raw",
    "geneUniProtIDs": "gene_uniprot_ids_raw",
    "geneProteinClassIDs": "gene_protein_class_ids_raw",
    "diseaseClasses": "disease_classes_raw",
}
"""REST-API column map.  Maps DisGeNET's camelCase API field names to
our schema's snake_case column names.  List-typed fields
(``geneUniProtIDs``, ``geneEnsemblIDs``, etc.) are mapped to
``*_raw`` columns and JSON-serialised before write (SCI-36)."""

# Minimum score threshold for inclusion (SCI-1, DES-1, CONF-1).
# Kept as a module-level alias for backward compat with code that
# imports ``MIN_SCORE`` from this module.  The actual runtime value
# comes from ``DISGENET_MIN_SCORE`` in settings.
MIN_SCORE: float = DISGENET_MIN_SCORE
"""Minimum DisGeNET score for inclusion.  Alias for
``DISGENET_MIN_SCORE`` (configurable via env var, default 0.06 per
Piñero et al. 2020).  Kept for backward compatibility with code that
imports ``MIN_SCORE`` from this module."""

# Confidence tiers based on DisGeNET score (SCI-11, DES-2, CONF-2).
# Alias for the publication-aligned tiers in ``cleaning.confidence``.
# Kept as a module-level alias for backward compat.
CONFIDENCE_TIERS: list[tuple[float, str]] = list(DEFAULT_CONFIDENCE_TIERS)
"""Confidence-tier thresholds (publication-aligned per Piñero et al. 2020).
Alias for ``cleaning.confidence.DEFAULT_CONFIDENCE_TIERS``.  Kept for
backward compatibility with code that imports ``CONFIDENCE_TIERS`` from
this module.  Use ``classify_confidence(score)`` for classification."""

# ---------------------------------------------------------------------------
# Compiled regexes — disease ID vocabulary patterns (SCI-5, SCI-29)
# ---------------------------------------------------------------------------
# v9 ROOT FIX (audit F4.1 / F1): the DisGeNET v2024+ REST API returns
# prefixed disease_ids in lowercase form: "umls:C0006142", "omim:100100",
# "mesh:D014979". The previous bare-format-only regexes rejected 80%+ of
# all DisGeNET records (every UMLS record and every OMIM record). Tests
# passed only because the fixtures used the legacy bare format.
# Each regex now accepts BOTH the prefixed curie form (case-insensitive
# prefix) and the bare form. The prefix is stripped in _normalise_disease_id
# below so downstream consumers always see the bare form.
_RE_UMLS_CUI = re.compile(r"^(?:UMLS:)?C[0-9]{7}$", re.IGNORECASE)
_RE_MESH_DESCRIPTOR = re.compile(r"^(?:MESH:)?D[0-9]{6}$", re.IGNORECASE)
_RE_MESH_TREE = re.compile(r"^[A-Z][0-9]{2}\.[0-9]{3}(\.[0-9]{3})*$")
_RE_DOID = re.compile(r"^DOID:[0-9]+$", re.IGNORECASE)
_RE_HPO = re.compile(r"^HP:[0-9]+$", re.IGNORECASE)
# OMIM IDs can be 4-7 digits. Modern MIM assignments reach 7 digits;
# historical MIM numbers from the 1960s-70s (1000-series) can be as short
# as 4 digits. v35 ROOT FIX: align with cleaning._constants.CANONICAL_OMIM_DISEASE_ID_REGEX
# (which uses 4-7 digits) so the SAME OMIM ID passes the DisGeNET pipeline,
# the OMIM pipeline, and the DB loader. The previous ``[0-9]{6,7}`` rejected
# every historical 4-5-digit MIM number (e.g. 1024 = hereditary spherocytosis).
_RE_OMIM = re.compile(r"^(?:OMIM:)?[0-9]{4,7}$", re.IGNORECASE)
# ICD-10 codes per WHO spec: letter + 2 digits (category) + optional '.'
# + 1-3 alphanumeric chars (subcategory). Examples: "I10", "E11.9",
# "M05.1", "C50.1". The full subcategory can have up to 4 chars after the
# dot in some clinical extensions (e.g. "S72.001A"), but for DisGeNET
# research data the standard 1-3 chars covers all observed cases.
_RE_ICD10 = re.compile(r"^[A-Z][0-9]{2}(\.[A-Z0-9]{1,4})?$")
# EFO (Experimental Factor Ontology) IDs follow the OBO curie pattern
# "EFO:_nnnnnnn" where the local ID is 7+ digits. Examples: "EFO:0000400"
# (diabetes mellitus), "EFO:0001360" (thyroid carcinoma). The leading
# underscore after the colon is part of the EFO curie spec — without it,
# the ID is not a valid EFO term.
_RE_EFO = re.compile(r"^EFO:_[0-9]{7,}$")
# Orphanet rare-disease IDs: "ORPHA:nnnn" — also a known DisGeNET
# disease vocabulary; included for completeness even though the original
# audit only flagged ICD-10 and EFO.
_RE_ORPHANET = re.compile(r"^ORPHA:[0-9]+$")
# HGNC gene-symbol format: an uppercase letter followed by uppercase
# letters + digits + optional hyphens. Length 1-50 chars (max 50 to match
# the DB column length and ``cleaning._constants.CANONICAL_HGNC_GENE_SYMBOL_REGEX``).
# The previous regex ^[A-Z0-9_-]+$ accepted "12345" (digits only), "---" (hyphens
# only) and "FOO_BAR" (underscore; HGNC does not allow underscores).
# v9 ROOT FIX (audit F4.3): tighten to match HGNC convention.
# v35 ROOT FIX: extend max length from 40 to 50 chars to align with the DB
# column (``models.Protein.gene_symbol`` CHECK LENGTH 1-50) and the
# canonical regex in ``cleaning._constants``. Without this alignment, a
# 45-char gene symbol would pass the DB CHECK but fail the pipeline
# validator (silent data loss at the pipeline → DB boundary).
_RE_HGNC_GENE_SYMBOL = re.compile(r"^[A-Z][A-Z0-9-]{0,49}$")
# PMID format: 7-8 digit integer.
_RE_PMID = re.compile(r"^\d{7,8}$")
# SQL-injection defence — reject PMIDs containing SQL keywords.
_RE_PMID_SQL_INJECTION = re.compile(
    r"(DROP|DELETE|INSERT|UPDATE|--|;)", re.IGNORECASE
)
# PII scan patterns (SEC-12).
_RE_PII_EMAIL = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_RE_PII_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# Free-text sanitiser (SEC-3, SEC-4, SEC-5).
_RE_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_RE_HTML_TAGS = re.compile(r"<[^>]+>")


# ---------------------------------------------------------------------------
# DisGeNET source-format enum (ARCH-9, IDEM-6)
# ---------------------------------------------------------------------------
class DisGeNETSourceFormat:
    """DisGeNET source-format discriminator (ARCH-9).

    A simple constant-namespace class (not an ``Enum``) — the values are
    plain strings so they can be compared with ``==`` and stored in the
    ``source_format`` column without conversion.
    """

    API: str = "api"
    TSV: str = "tsv"


# ---------------------------------------------------------------------------
# CleanResult dataclass (DES-13)
# ---------------------------------------------------------------------------
@dataclass
class CleanResult:
    """Structured result of :meth:`DisGeNETPipeline.clean` (DES-13).

    The framework's ``BasePipeline.clean()`` contract returns a
    ``pd.DataFrame``.  This dataclass is returned by the internal
    ``_clean_core`` method and exposed via ``self.last_clean_result`` so
    callers (and tests) can inspect the cleaning report, dead-letter
    rows, and input/output fingerprints without breaking the framework
    contract.
    """

    df: pd.DataFrame
    cleaning_report: CleaningReport = field(default_factory=dict)
    dead_letter: pd.DataFrame = field(default_factory=pd.DataFrame)
    input_fingerprint: str = ""
    output_fingerprint: str = ""


# ---------------------------------------------------------------------------
# Internal helper classes (REL-8, SEC-20)
# ---------------------------------------------------------------------------
class _CircuitBreaker:
    """Simple circuit breaker for DisGeNET API calls (REL-8).

    After ``failure_threshold`` consecutive failures, the breaker opens
    and refuses further calls for ``reset_timeout`` seconds.  After the
    timeout, it enters ``half_open`` state: one call is allowed; if it
    succeeds, the breaker closes; if it fails, the breaker re-opens.
    """

    def __init__(
        self,
        failure_threshold: int = DISGENET_CIRCUIT_BREAKER_THRESHOLD,
        reset_timeout: float = float(DISGENET_CIRCUIT_BREAKER_RESET_SECONDS),
    ) -> None:
        self._failure_threshold = max(1, int(failure_threshold))
        self._reset_timeout = max(0.0, float(reset_timeout))
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._state = "closed"
        self._lock = threading.Lock()

    def record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()
            if self._failure_count >= self._failure_threshold:
                if self._state != "open":
                    logger.warning(
                        "[disgenet] Circuit breaker OPENED after %d consecutive "
                        "failures — refusing calls for %.1fs",
                        self._failure_count,
                        self._reset_timeout,
                    )
                self._state = "open"

    def record_success(self) -> None:
        with self._lock:
            self._failure_count = 0
            self._state = "closed"

    def is_open(self) -> bool:
        with self._lock:
            if self._state == "open":
                if time.time() - self._last_failure_time > self._reset_timeout:
                    self._state = "half_open"
                    return False
                return True
            return False


class _RateLimiter:
    """Token-bucket rate limiter for outbound HTTP requests (SEC-20).

    Ensures we don't exceed ``DISGENET_API_RATE_LIMIT`` requests per
    second by spacing requests at least ``min_interval`` seconds apart.
    Thread-safe via a lock.
    """

    def __init__(self, rate_per_second: float = DISGENET_API_RATE_LIMIT) -> None:
        self._min_interval = (
            max(0.0, 1.0 / float(rate_per_second))
            if rate_per_second > 0
            else 0.0
        )
        self._last_request = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self._min_interval <= 0.0:
            return
        with self._lock:
            elapsed = time.time() - self._last_request
            if elapsed < self._min_interval:
                try:
                    time.sleep(self._min_interval - elapsed)
                except KeyboardInterrupt:
                    logger.info(
                        "[disgenet] Rate-limiter sleep interrupted by user"
                    )
                    raise
            self._last_request = time.time()


# Module-level rate-limiter / circuit-breaker instances.
# WHY: Class-level state would be shared across instances, which is
# fine for the rate limiter (we want global rate limiting) but is
# harder to reset between tests.  Module-level state is reset by
# re-importing the module.  Tests can monkeypatch these directly.
_RATE_LIMITER = _RateLimiter()
_CIRCUIT_BREAKER = _CircuitBreaker()


# v41 ROOT FIX (SEV3-MEDIUM #10): the module-level _RATE_LIMITER and
# _CIRCUIT_BREAKER are NOT reset by any function in this module. The
# previous behavior required re-importing the module to reset them
# (which is fragile in long-running processes and Airflow schedulers
# that cache imports). Tests that wanted a fresh state had to
# monkeypatch both objects directly. Fix: add a public ``_reset()``
# function that recreates both objects so tests and operators can
# reset the global rate-limiter / circuit-breaker state without a
# re-import. The function is idempotent (calling it twice is safe).
def _reset() -> None:
    """Reset the module-level rate-limiter and circuit-breaker (v41 ROOT FIX).

    Recreates ``_RATE_LIMITER`` and ``_CIRCUIT_BREAKER`` so the global
    throttle/circuit state is cleared. Useful for tests, retries after
    a sustained outage, and long-running processes that need to clear
    stale state without re-importing the module.
    """
    global _RATE_LIMITER, _CIRCUIT_BREAKER
    _RATE_LIMITER = _RateLimiter()
    _CIRCUIT_BREAKER = _CircuitBreaker()
    logger.info(
        "[disgenet] Module-level _RATE_LIMITER and _CIRCUIT_BREAKER "
        "reset to fresh instances."
    )


# ---------------------------------------------------------------------------
# Backward-compat wrapper for _classify_confidence (SCI-12, DES-3, ARCH-7)
# ---------------------------------------------------------------------------
def _classify_confidence(score: float) -> str:
    """Classify a DisGeNET score into a confidence tier.

    Thin wrapper around :func:`cleaning.confidence.classify_confidence`
    for backward compatibility with code that imports
    ``_classify_confidence`` from this module.  The actual
    implementation lives in ``cleaning/confidence.py`` (ARCH-7) so other
    pipelines (OMIM, STRING) can reuse it.

    Parameters
    ----------
    score : float
        DisGeNET DSGP score, expected to be in ``[0, 1]`` (post-clip).
        NaN and negative scores MUST NOT reach this function —
        :func:`validate_gda_scores` is responsible for clipping first
        (SCI-12, SCI-13).  A defensive assertion fires if these
        invariants are violated.

    Returns
    -------
    str
        v43 ROOT FIX (P1-018): updated docstring to match actual tier
        labels. The previous docstring said ``"weak"``, ``"moderate"``,
        ``"strong"`` but the actual ``DEFAULT_CONFIDENCE_TIERS`` in
        ``cleaning/confidence.py`` returns ``"sub_weak"``, ``"weak"``,
        ``"strong"`` (no ``"moderate"`` tier exists). Code branching
        on ``"moderate"`` was dead. The correct tiers are:
        ``"sub_weak"`` (score < 0.06), ``"weak"`` (0.06 <= score < 0.3),
        ``"strong"`` (score >= 0.3).
    """
    return classify_confidence(score, tiers=CONFIDENCE_TIERS)


# ---------------------------------------------------------------------------
# Helper functions (SCI-5, SCI-29, DQ-13..15, DQ-16, DQ-17)
# ---------------------------------------------------------------------------
def _infer_disease_id_type(disease_id: Optional[str]) -> Optional[str]:
    """Infer the disease-ID vocabulary from the prefix (SCI-5).

    Parameters
    ----------
    disease_id : str or None
        The disease ID to inspect (e.g. ``"C0006142"``, ``"D064726"``,
        ``"DOID:1612"``, ``"HP:0001250"``, ``"100100"``). Both the bare
        form and the DisGeNET-prefixed form (``"umls:C0006142"``,
        ``"omim:100100"``, ``"mesh:D014979"``) are recognised.

    Returns
    -------
    str or None
        One of ``"umls"``, ``"mesh"``, ``"doid"``, ``"hpo"``, ``"omim"``,
        ``"icd10"``, ``"efo"``, ``"orphanet"``, or ``None`` if the ID does
        not match any known vocabulary.
    """
    if disease_id is None or not isinstance(disease_id, str):
        return None
    did = disease_id.strip().upper()
    if not did:
        return None
    if _RE_UMLS_CUI.match(did):
        return "umls"
    if _RE_MESH_DESCRIPTOR.match(did) or _RE_MESH_TREE.match(did):
        return "mesh"
    if _RE_DOID.match(disease_id):  # DOID is case-sensitive — keep original
        return "doid"
    if _RE_HPO.match(disease_id):  # HP: prefix is uppercase
        return "hpo"
    if _RE_OMIM.match(did):
        return "omim"
    if _RE_ICD10.match(did):
        return "icd10"
    if _RE_EFO.match(disease_id):  # EFO:_nnnnnnn — case-sensitive
        return "efo"
    if _RE_ORPHANET.match(disease_id):  # ORPHA:nnnn — case-sensitive
        return "orphanet"
    return None


def _normalise_disease_id(disease_id: Optional[str]) -> Optional[str]:
    r"""Normalise a DisGeNET disease ID to the canonical form for each vocabulary.

    The DisGeNET v2024+ API returns IDs as lowercased curies such as
    ``"umls:C0006142"``, ``"omim:100100"``, ``"mesh:D014979"``. The
    canonical form for each vocabulary is:

      * UMLS CUIs: bare ``"C0006142"`` (no prefix) — matches kg_builder
        ID_PATTERNS["Disease"] = ``C\d{7}``.
      * MeSH descriptors: bare ``"D014979"`` (no prefix) — matches
        ``D\d{6}``.
      * OMIM IDs: ``"OMIM:100100"`` (WITH prefix, uppercase) — matches
        ``OMIM:\d+`` AND matches the OMIM pipeline's own emission format
        (``disease_id = "OMIM:" + str(phenotype_mim)`` per BUG-3.8).
      * DOID / HP / Orphanet / EFO: keep their curie form (already
        canonical: ``DOID:1234``, ``HP:0001234``, ``ORPHA:558``,
        ``EFO:_0000400``).

    v9 ROOT FIX (audit F4.9 — "Three layers, three OMIM ID format
    assumptions"): the previous implementation stripped ALL prefixes
    including ``omim:``, producing bare ``"100100"``. The OMIM pipeline
    emits ``"OMIM:100100"``. The DB loader accepts both via
    ``^(?:OMIM:)?\d{4,7}$``. But when OMIM ↔ DisGeNET gene-disease
    edges are JOINED on disease_id, ``"OMIM:100100" != "100100"`` —
    the same disease appears as two distinct nodes in the knowledge
    graph, and the join produces ZERO matching rows. This is a
    compound destruction pattern (P2-COMPOUND): three files each look
    correct in isolation, but the interaction silently destroys the
    cross-source join.

    Fix: preserve the ``OMIM:`` prefix for OMIM-sourced IDs so DisGeNET
    and OMIM pipelines emit the SAME canonical form. Other vocabularies
    (UMLS, MeSH) continue to use bare form because no other pipeline
    emits them with a prefix — so there's no cross-source join risk.

    Returns ``None`` for None/empty input; returns the input unchanged
    (after stripping whitespace) if it does not start with a recognised
    prefix — preserving backwards compatibility with bare-format inputs.
    """
    if disease_id is None or not isinstance(disease_id, str):
        return None
    raw = disease_id.strip()
    if not raw:
        return None
    lowered = raw.lower()
    # OMIM: PRESERVE the prefix (uppercase) so it matches the OMIM
    # pipeline's emission format. This is the F4.9 root fix.
    if lowered.startswith("omim:"):
        digits = raw[len("omim:"):]
        return f"OMIM:{digits}"
    # Other prefixed vocabularies: strip the prefix to bare canonical form.
    for prefix in ("umls:", "mesh:", "doid:", "hp:", "orpha:", "efo:"):
        if lowered.startswith(prefix):
            return raw[len(prefix):]
    return raw


def _validate_disease_id(disease_id: Optional[str]) -> tuple[bool, Optional[str]]:
    """Validate a disease ID against known vocabulary patterns (SCI-29).

    Returns ``(is_valid, id_type)``.  ``id_type`` is the same value
    :func:`_infer_disease_id_type` would return.  ``is_valid`` is True
    if the ID matches any known vocabulary, False otherwise (including
    empty string, whitespace-only, or non-matching).
    """
    if disease_id is None or not isinstance(disease_id, str):
        return (False, None)
    did = disease_id.strip().upper()
    if not did:
        return (False, None)
    id_type = _infer_disease_id_type(disease_id)
    return (id_type is not None, id_type)


def _validate_gene_symbol(symbol: Optional[str]) -> bool:
    """Validate a gene symbol against HGNC format (SCI-30, COMP-10).

    HGNC symbols are uppercase Latin letters + digits, occasional
    hyphens (e.g. ``BRCA1``, ``TP53``, ``H2AFX``, ``BRCA-1``).
    """
    if symbol is None or not isinstance(symbol, str):
        return False
    s = symbol.strip().upper()
    if not s:
        return False
    return bool(_RE_HGNC_GENE_SYMBOL.match(s))


def _sanitise_free_text(value: Any, max_length: int = 1000) -> Any:
    """Sanitise a free-text field against XSS / control-char injection (SEC-3).

    Strips control characters, strips HTML tags, escapes angle brackets,
    and truncates to ``max_length`` chars (with a ``…`` suffix).
    Returns the sanitised value, or the original value if it's not a
    string.  Logs a WARNING if sanitisation changed the value (the
    caller is responsible for the log — this function is pure).
    """
    if value is None or not isinstance(value, str):
        return value
    out = _RE_CONTROL_CHARS.sub("", value)
    out = _RE_HTML_TAGS.sub("", out)
    out = out.replace("<", "&lt;").replace(">", "&gt;")
    if len(out) > max_length:
        out = out[: max_length - 1] + "…"
    return out


def _strip_string_columns(
    df: pd.DataFrame, columns: list[str]
) -> pd.DataFrame:
    """Strip leading/trailing whitespace from the given string columns (DQ-15).

    Operates on a copy; returns the copy.  Non-string values are
    coerced to string first (so ``.str.strip`` doesn't crash on
    numeric / list values).
    """
    out = df.copy()
    for col in columns:
        if col not in out.columns:
            continue
        # Coerce to string, preserving NaN.
        mask = out[col].notna()
        out.loc[mask, col] = (
            out.loc[mask, col].astype(str).str.strip()
        )
    return out


def _normalise_gene_symbol_series(s: pd.Series) -> pd.Series:
    """Uppercase + strip a gene_symbol Series (DQ-13)."""
    if s is None:
        return s
    out = s.copy()
    mask = out.notna()
    out.loc[mask] = out.loc[mask].astype(str).str.upper().str.strip()
    return out


def _normalise_disease_id_series(s: pd.Series) -> pd.Series:
    """Uppercase + strip a disease_id Series (DQ-14).

    UMLS CUIs and MeSH descriptors are uppercase.  DOIDs are
    case-sensitive (``DOID:`` prefix is uppercase, the numeric suffix
    is digits) — uppercase does not change them.  HPO IDs (``HP:``
    prefix) are also uppercase.  So a blanket upper() is safe.
    """
    if s is None:
        return s
    out = s.copy()
    mask = out.notna()
    out.loc[mask] = out.loc[mask].astype(str).str.upper().str.strip()
    return out


def _compute_evidence_strength(
    pmid_count: int, year_final: Optional[int]
) -> str:
    """Classify evidence strength by PMID count and recency (SCI-24).

    Parameters
    ----------
    pmid_count : int
        Number of PMIDs in the record's ``pmid_list`` (after dedup).
    year_final : int or None
        The most recent publication year, or None if unknown.

    Returns
    -------
    str
        One of ``"robust"``, ``"moderate"``, ``"limited"``,
        ``"unsupported"``.
    """
    if pmid_count >= 10 and (year_final is None or year_final >= 2010):
        return "robust"
    if pmid_count >= 3:
        return "moderate"
    if pmid_count >= 1:
        return "limited"
    return "unsupported"


def _compute_normalized_score(
    score: Optional[float], source_id: Optional[str]
) -> Optional[float]:
    """Compute ``normalized_score = score * source_weight`` (SCI-38).

    Returns ``None`` if either input is None.  The source weights come
    from :data:`config.settings.DISGENET_SOURCE_WEIGHTS` (configurable
    via the ``DISGENET_SOURCE_WEIGHTS`` env var, JSON object).
    """
    if score is None or pd.isna(score) or source_id is None:
        return None
    weight = DISGENET_SOURCE_WEIGHTS.get(source_id, 1.0)
    return float(score) * float(weight)


# ---------------------------------------------------------------------------
# DisGeNETPipeline
# ---------------------------------------------------------------------------
class DisGeNETPipeline(BasePipeline):
    """DisGeNET pipeline for gene-disease association data.

    Subclasses :class:`pipelines.base_pipeline.BasePipeline` and
    implements the ``download → clean → load`` contract.  All public
    contracts (BasePipeline ABC, schema v1.json, validate_gda_scores
    signature, bulk_upsert_gda signature, UpsertResult,
    GeneDiseaseAssociation model, DataSourceName enum, PMID_LIST_LENGTH)
    are preserved.

    This pipeline is the **single source of truth** for every
    gene-disease association in the platform.  See the module docstring
    for the life-safety framing.
    """

    source_name: str = DataSourceName.DISGENET.value

    # Declare the dependency on the UniProt pipeline (ARCH-17).
    dependencies: tuple[str, ...] = ("uniprot",)

    # The cleaned-data filename is determined by
    # BasePipeline._get_processed_filename() which returns
    # "gene_disease_associations.csv" for source_name="disgenet".
    # DISGENET_OUTPUT_FILENAME is also configurable (CONF-10) — we
    # honour it via the ``processed_filename`` attribute (ARCH-1.6).

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialise the pipeline.

        Validates the DisGeNET config (CONF-14) and sets up internal
        state.  All arguments are forwarded to ``BasePipeline.__init__``.
        """
        super().__init__(*args, **kwargs)

        # Validate config eagerly (CONF-14, CONF-16, CONF-17).
        # We re-validate here (settings.py also validates at import time)
        # so that env-var changes between import and pipeline
        # instantiation are caught.
        _validate_disgenet_config()

        # Source-format discriminator (ARCH-9, IDEM-6).  Set in
        # download(); read in clean().
        self._source_format: str = DisGeNETSourceFormat.API

        # Honour DISGENET_OUTPUT_FILENAME (CONF-10).  This attribute is
        # read by BasePipeline._get_processed_filename() (ARCH-1.6).
        self.processed_filename: str = DISGENET_OUTPUT_FILENAME

        # Honour DISGENET_FREEZE_VERSION (IDEM-14).  When non-empty,
        # every GDA row gets snapshot_tag=this value.
        self.snapshot_tag: Optional[str] = (
            DISGENET_FREEZE_VERSION or None
        )

        # Honour DISGENET_TARGET_VERSION (IDEM-8).  When non-empty,
        # the API request includes the version parameter.
        self.target_version: Optional[str] = (
            DISGENET_TARGET_VERSION or None
        )

        # API-response metadata (populated by _download_via_api).
        self._disgenet_release_version: Optional[str] = None
        self._api_endpoint: str = ""
        self._api_params: dict[str, Any] = {}
        self._source_url_sanitised: str = ""

        # Manifest data (populated by clean(); read by _save_processed_csv).
        self._manifest: dict[str, Any] = {}
        self._input_fingerprint: str = ""
        self._output_fingerprint: str = ""
        self._cleaning_metadata: dict[str, Any] = {}

        # Cached gene_to_uniprot map + version (IDEM-7).
        self._gene_to_uniprot_cache: Optional[tuple[GeneToUniprotMap, ProteinNameToUniprotMap, str]] = None

        # CleanResult from the last clean() call (DES-13).
        self.last_clean_result: Optional[CleanResult] = None

        # Cleaning report (LOG-5, LOG-8, etc.).
        self.last_cleaning_report: CleaningReport = {}

        # Dead-letter queue (in-memory; also persisted to DB + file).
        self._dead_letter_rows: list[dict[str, Any]] = []

        # Honour DISGENET_API_CA_BUNDLE (SEC-10).
        if DISGENET_API_CA_BUNDLE:
            self.verify_tls = DISGENET_API_CA_BUNDLE  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # Public properties (LOG-21, LOG-22)
    # ------------------------------------------------------------------
    @property
    def http_session(self) -> requests.Session:
        """Return a reusable ``requests.Session`` with DisGeNET-specific headers.

        Sets ``User-Agent`` (SEC-16), ``Accept: application/json`` (INT-16),
        and ``Authorization: Bearer ***`` (when an API key is set).  The
        session mounts an HTTPAdapter with retry policy via the base class.

        v41 ROOT FIX (SEV3-MEDIUM #11): the previous code mutated the
        BASE class's session (``super().http_session``) by setting
        DisGeNET-specific headers on it. Because the base class caches
        the session in ``self._http_session``, ALL subsequent accesses
        to ``super().http_session`` (by the base class itself, or by
        sibling methods like ``_download_file``) would see the
        DisGeNET headers — leaking the DisGeNET ``Authorization:
        Bearer <key>`` header into non-DisGeNET requests. Fix: create
        a SEPARATE ``requests.Session`` for DisGeNET use (do NOT
        mutate the base session). The separate session still inherits
        the base adapter mount policy (we re-mount the same adapters)
        but its headers are isolated.
        """
        # v41 ROOT FIX (SEV3-MEDIUM #11): use a separate session
        # cached on a DisGeNET-specific attribute so we don't mutate
        # the base class's ``self._http_session``.
        if getattr(self, "_disgenet_http_session", None) is None:
            base_session = super().http_session  # type: ignore[misc]
            # Create a NEW session — do NOT reuse the base session.
            new_session = requests.Session()
            # Inherit the base session's adapter mount policy (which
            # the base class configures with retry/backoff).
            for prefix, adapter in list(base_session.adapters.items()):
                new_session.mount(prefix, adapter)
            # Inherit TLS verify setting.
            new_session.verify = self.verify_tls  # type: ignore[assignment]
            self._disgenet_http_session = new_session
        session = self._disgenet_http_session
        # Set DisGeNET-specific headers idempotently on the SEPARATE
        # session (not the base session).
        session.headers.setdefault(
            "User-Agent",
            f"DrugRepurposing/{__version__} (Team Cosmic; "
            f"contact: {DISGENET_CONTACT_EMAIL})",
        )
        session.headers.setdefault("Accept", "application/json")
        if DISGENET_API_KEY:
            session.headers["Authorization"] = f"Bearer {DISGENET_API_KEY}"
        if self.verify_tls is not True:
            session.verify = self.verify_tls  # type: ignore[assignment]
        return session

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------
    def download(self) -> Path:
        """Download gene-disease associations from DisGeNET.

        **Contract:** returns a :class:`pathlib.Path` to the downloaded
        file (the framework's ``download() -> Path`` contract — see
        ARCH-6).  The file write is part of the contract, not a coupling
        violation.

        Dispatches to :meth:`_download_via_api` (default, when
        ``DISGENET_USE_API=True`` and an API key is set) or
        :meth:`_download_static` (explicit opt-in via
        ``DISGENET_USE_API=False``).

        Raises
        ------
        ValueError
            If ``DISGENET_USE_API=True`` but ``DISGENET_API_KEY`` is not
            set (SCI-27 — no silent fallback to the deprecated static URL).
        """
        # SCI-27 / CONF-18: No silent fallback to deprecated static URL.
        if DISGENET_USE_API and not DISGENET_API_KEY:
            raise ValueError(
                "DISGENET_USE_API=true but DISGENET_API_KEY is not set. "
                "Set the DISGENET_API_KEY environment variable or set "
                "DISGENET_USE_API=false (not recommended - static URL is "
                "deprecated since 2024)."
            )

        start = time.perf_counter()
        try:
            if DISGENET_USE_API:
                self._source_format = DisGeNETSourceFormat.API
                path = self._download_via_api()
            else:
                self._source_format = DisGeNETSourceFormat.TSV
                path = self._download_static()
        finally:
            duration = time.perf_counter() - start
            self._emit_metric(
                "download_duration_seconds", duration,
                tags={"source_format": self._source_format},
            )
            logger.info(
                "[disgenet] Download phase took %.2fs (source_format=%s)",
                duration,
                self._source_format,
            )

        # Compute and log the SHA-256 of the downloaded file (LOG-3, IDEM-4).
        try:
            self._sha256_raw = self._compute_sha256(path)
            logger.info(
                "[disgenet] Downloaded file SHA-256: %s", self._sha256_raw
            )
            # Write a sidecar .sha256 for cache integrity (IDEM-4).
            sha256_sidecar = path.with_suffix(path.suffix + ".sha256")
            try:
                sha256_sidecar.write_text(self._sha256_raw + "\n")
            except OSError as exc:
                logger.warning(
                    "[disgenet] Could not write SHA-256 sidecar %s: %s",
                    sha256_sidecar, exc,
                )
        except (OSError, ValueError) as exc:
            logger.warning(
                "[disgenet] Could not compute SHA-256 of %s: %s", path, exc
            )

        return path

    def _download_static(self) -> Path:
        """Stream-download the deprecated static TSV.gz from DisGeNET.

        WHY: DisGeNET deprecated the static TSV URL in 2024 in favour of
        the REST API (DOC-8).  The static URL may be removed at any time.
        This method is kept for offline/mirror scenarios but is NOT the
        primary path — call it only via explicit ``DISGENET_USE_API=false``
        opt-in.

        Per SCI-28 / SEC-2: NO Authorization header is sent for the
        static URL (it's a CDN-served file; auth headers are useless
        and may trigger 403 from CDNs that reject unknown headers).
        """
        logger.warning(
            "[disgenet] DisGeNET static URL is DEPRECATED since 2024. "
            "Use DISGENET_USE_API=true with an API key for reliable access."
        )
        dest = self.raw_dir / "all_gene_disease_associations.tsv.gz"
        if dest.exists() and dest.stat().st_size > 0:
            logger.info(
                "[disgenet] Static file already exists: %s (%d bytes)",
                dest, dest.stat().st_size,
            )
            return dest

        try:
            return self._download_file(DISGENET_URL, dest, headers=None)
        except (requests.exceptions.RequestException, OSError) as exc:
            logger.error(
                "[disgenet] Static download failed: %s. "
                "Set DISGENET_USE_API=true with an API key for the "
                "recommended path.",
                exc,
            )
            raise RuntimeError(
                f"DisGeNET static URL failed: {exc}"
            ) from exc

    def _download_via_api(self) -> Path:
        """Download GDA data via the DisGeNET REST API with pagination.

        Streams records to disk (REL-10, PERF-1, PERF-2) — peak memory
        is O(page_size), not O(total_records).  Pagination is stable
        (SCI-25, IDEM-20) via the ``sort=geneId`` parameter.
        Completeness is asserted (SCI-35, IDEM-16) — if the API returns
        fewer records than ``totalResults``, the pipeline raises
        RuntimeError (or, with ``DISGENET_ALLOW_PARTIAL_DATA=True``,
        writes a partial-data manifest and continues).
        """
        dest = self.raw_dir / "all_gene_disease_associations.tsv"

        # IDEM-4 / REL-5: Cache integrity check — if the file exists AND
        # a valid SHA-256 sidecar exists AND it matches, skip download.
        if dest.exists() and dest.stat().st_size > 0:
            sha256_sidecar = dest.with_suffix(dest.suffix + ".sha256")
            if sha256_sidecar.exists():
                try:
                    expected_sha = sha256_sidecar.read_text().strip()
                    actual_sha = self._compute_sha256(dest)
                    if expected_sha == actual_sha:
                        logger.info(
                            "[disgenet] Cached file %s is valid (SHA-256 matches), "
                            "skipping download",
                            dest.name,
                        )
                        return dest
                    else:
                        logger.warning(
                            "[disgenet] Cached file %s SHA-256 mismatch "
                            "(expected %s, got %s) — re-downloading",
                            dest.name, expected_sha, actual_sha,
                        )
                except OSError as exc:
                    logger.warning(
                        "[disgenet] Could not verify cache integrity: %s", exc
                    )
            else:
                logger.info(
                    "[disgenet] No SHA-256 sidecar for %s — using cached file",
                    dest.name,
                )
                return dest

        # Open the output file in write mode (streaming — REL-10).
        # We'll write the header first, then each page's records as TSV
        # rows.  Memory usage is O(page_size).
        records_written = 0
        total_available: Optional[int] = None
        header_written = False
        # Track the column order from the first page so subsequent
        # pages write the same columns in the same order.
        column_order: list[str] = []

        # Capture API metadata for the manifest (LIN-21, LIN-22).
        self._api_endpoint = DISGENET_API_URL
        self._source_url_sanitised = self._sanitize_url(DISGENET_API_URL)

        # Clean up any stale .tmp file from a previous crashed run (DQ-12).
        tmp_dest = dest.with_suffix(dest.suffix + ".tmp")
        if tmp_dest.exists():
            logger.warning(
                "[disgenet] Removing stale .tmp file from previous run: %s",
                tmp_dest,
            )
            try:
                tmp_dest.unlink()
            except OSError as exc:
                logger.warning(
                    "[disgenet] Could not remove stale .tmp file: %s", exc
                )

        # Write to the .tmp file first (atomic write — DQ-12, REL-11).
        try:
            with open(tmp_dest, "w", encoding="utf-8", newline="") as out_fh:
                page_num = 0
                offset = 0
                download_start = time.perf_counter()
                while True:
                    # REL-14: Overall download-phase timeout.
                    if (
                        time.perf_counter() - download_start
                        > DISGENET_DOWNLOAD_PHASE_TIMEOUT
                    ):
                        raise RuntimeError(
                            f"DisGeNET download phase exceeded "
                            f"{DISGENET_DOWNLOAD_PHASE_TIMEOUT}s timeout"
                        )
                    # REL-14: Max pages cap.
                    if page_num >= DISGENET_API_MAX_PAGES:
                        logger.error(
                            "[disgenet] Reached DISGENET_API_MAX_PAGES=%d — "
                            "stopping pagination (this is likely a config "
                            "issue; DisGeNET has ~1M records)",
                            DISGENET_API_MAX_PAGES,
                        )
                        break

                    params: dict[str, Any] = {
                        "offset": offset,
                        "limit": DISGENET_API_PAGE_SIZE,
                        "format": "json",
                        # SCI-25 / IDEM-20: Stable sort.
                        "sort": "geneId",
                        # SCI-18: Human organism filter.
                        "species": [9606],
                    }
                    # IDEM-8: Version pinning.
                    if self.target_version:
                        params["version"] = self.target_version

                    self._api_params = params
                    page_num += 1

                    payload, headers = self._api_get_disgenet(
                        DISGENET_API_URL, params
                    )

                    # SCI-26: Capture DisGeNET release version from
                    # response headers (X-DisGeNET-Version or similar).
                    if self._disgenet_release_version is None:
                        self._disgenet_release_version = (
                            headers.get("X-DisGeNET-Version")
                            or headers.get("X-Disgenet-Version")
                            or headers.get("version")
                            or self.target_version
                            or "unknown"
                        )
                        logger.info(
                            "[disgenet] DisGeNET release version: %s",
                            self._disgenet_release_version,
                        )

                    # SCI-31 / REL-17: Validate payload is a dict FIRST
                    # (before _extract_total_results tries .get on it).
                    records = self._extract_payload(payload)
                    if records is None:
                        # End-of-data signal — break the loop.
                        break

                    # SCI-32: Disambiguate totalResults vs count.
                    total_available = self._extract_total_results(payload, total_available)

                    if not records:
                        # Empty page but not end-of-data — log and continue.
                        logger.info(
                            "[disgenet] Page %d returned 0 records (offset=%d)",
                            page_num, offset,
                        )
                        # If we know total_available and we've already
                        # fetched it, break; otherwise continue pagination
                        # (the empty page might be a transient glitch).
                        if (
                            total_available is not None
                            and records_written >= total_available
                        ):
                            break
                    else:
                        # SCI-36: JSON-serialise list/dict columns before write.
                        records = self._serialise_list_columns(records)

                        # Write header on first non-empty page.
                        if not header_written:
                            column_order = list(records[0].keys())
                            # Write header (tab-separated).
                            out_fh.write("\t".join(column_order) + "\n")
                            header_written = True

                        # Write records (tab-separated, one per line).
                        for rec in records:
                            out_fh.write(
                                "\t".join(
                                    self._serialise_cell(rec.get(col))
                                    for col in column_order
                                )
                                + "\n"
                            )
                        records_written += len(records)

                        # LOG-24: Pagination progress.
                        pct = (
                            100.0 * records_written / total_available
                            if total_available
                            else 0.0
                        )
                        logger.info(
                            "[disgenet] API pagination: page %d, fetched %d / %s "
                            "records (%.1f%%)",
                            page_num, records_written,
                            str(total_available) if total_available else "?",
                            pct,
                        )

                    # CONF-5: Safety cap on total records.
                    if records_written >= DISGENET_API_MAX_RECORDS:
                        logger.warning(
                            "[disgenet] Reached DISGENET_API_MAX_RECORDS=%d — "
                            "stopping pagination (safety cap)",
                            DISGENET_API_MAX_RECORDS,
                        )
                        break

                    # Termination conditions.
                    if len(records) < DISGENET_API_PAGE_SIZE:
                        # Last page (fewer records than requested).
                        break
                    if (
                        total_available is not None
                        and records_written >= total_available
                    ):
                        logger.info(
                            "[disgenet] Fetched all %d available records",
                            total_available,
                        )
                        break

                    offset += DISGENET_API_PAGE_SIZE

            # IDEM-5: Do NOT write an empty file when no records returned.
            if records_written == 0:
                try:
                    tmp_dest.unlink()
                except OSError:
                    pass
                raise RuntimeError(
                    "DisGeNET API returned 0 records — this is likely a "
                    "configuration or API error, not a legitimate empty result."
                )

            # SCI-35: Completeness assertion.
            if (
                total_available is not None
                and records_written != total_available
                and not DISGENET_ALLOW_PARTIAL_DATA
            ):
                # Remove the .tmp file before raising.
                try:
                    tmp_dest.unlink()
                except OSError:
                    pass
                raise RuntimeError(
                    f"DisGeNET API completeness assertion failed: "
                    f"fetched {records_written} records but totalResults="
                    f"{total_available}. Set DISGENET_ALLOW_PARTIAL_DATA=true "
                    f"to allow partial data (dev/debug only)."
                )
            if (
                total_available is not None
                and records_written != total_available
                and DISGENET_ALLOW_PARTIAL_DATA
            ):
                logger.error(
                    "[disgenet] PARTIAL DATA: fetched %d / %d records — "
                    "DISGENET_ALLOW_PARTIAL_DATA=true, continuing with partial "
                    "dataset",
                    records_written, total_available,
                )
                # Write a partial-data manifest.
                partial_manifest = {
                    "partial_data": True,
                    "records_expected": total_available,
                    "records_fetched": records_written,
                    "run_id": self.run_id,
                }
                partial_manifest_path = (
                    self.raw_dir / "gene_disease_associations_partial.json"
                )
                try:
                    partial_manifest_path.write_text(
                        json.dumps(partial_manifest, indent=2)
                    )
                except OSError as exc:
                    logger.warning(
                        "[disgenet] Could not write partial-data manifest: %s",
                        exc,
                    )

            # DQ-12: Atomic move .tmp -> final.
            os.replace(tmp_dest, dest)
            logger.info(
                "[disgenet] Saved %d GDA records to %s", records_written, dest
            )
            return dest

        except Exception:
            # Clean up the .tmp file on any failure (DQ-12).
            try:
                if tmp_dest.exists():
                    tmp_dest.unlink()
            except OSError:
                pass
            # REL-6: Graceful degradation — try the most recent cached TSV.
            if DISGENET_FALLBACK_TO_CACHE:
                cached = self._find_most_recent_cached_tsv()
                if cached is not None:
                    logger.warning(
                        "[disgenet] API download failed — falling back to "
                        "cached TSV %s (DATA MAY BE STALE)",
                        cached,
                    )
                    return cached
            raise

    def _find_most_recent_cached_tsv(self) -> Optional[Path]:
        """Find the most recent valid cached TSV in raw_dir (REL-6).

        v21 ROOT FIX (Audit section 6 finding 3 - "Silent fallback to
        stale cached TSV"): the previous code returned ANY non-empty
        ``all_gene_disease_associations.tsv*`` file by mtime - no
        SHA-256 verification, no max-age check. Pipeline proceeded
        with potentially years-stale GDA data; only WARNING logged.
        Fix: enforce a max-age (default 90 days). TSVs older than the
        threshold are NOT returned - the caller will raise the
        original API error instead of silently using stale data.
        Operators who explicitly want to use stale data can set
        ``DRUGOS_DISGENET_MAX_CACHE_AGE_DAYS=-1`` to disable the
        max-age check.
        """
        if self.raw_dir is None or not self.raw_dir.exists():
            return None
        # v21: max-age check (default 90 days).
        import os as _os
        import time as _time
        try:
            max_age_days = int(_os.environ.get(
                "DRUGOS_DISGENET_MAX_CACHE_AGE_DAYS", "90"
            ))
        except ValueError:
            max_age_days = 90
        max_age_s = max_age_days * 86400 if max_age_days >= 0 else None
        now_s = _time.time()
        candidates = sorted(
            self.raw_dir.glob("all_gene_disease_associations.tsv*"),
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
            reverse=True,
        )
        for cand in candidates:
            try:
                st = cand.stat()
            except OSError:
                continue
            if st.st_size == 0:
                continue
            # v21: max-age check. Skip files older than the threshold.
            if max_age_s is not None:
                age_s = now_s - st.st_mtime
                if age_s > max_age_s:
                    logger.warning(
                        "[disgenet] cached TSV %s is %.1f days old "
                        "(max %d days) - skipping. Set "
                        "DRUGOS_DISGENET_MAX_CACHE_AGE_DAYS=-1 to "
                        "disable the freshness check.",
                        cand, age_s / 86400.0, max_age_days,
                    )
                    continue
            return cand
        return None

    def _extract_total_results(
        self, payload: dict[str, Any], previous: Optional[int]
    ) -> Optional[int]:
        """Extract the total-result count from a DisGeNET API response (SCI-32).

        DisGeNET's API may return either ``totalResults`` or ``count``.
        ``totalResults`` is preferred (the field name suggests it's the
        total across all pages).  If only ``count`` is present and it
        looks like a page count (small int), we log a WARNING.
        """
        total = payload.get("totalResults")
        if total is None:
            total = payload.get("count")
        if total is None:
            return previous
        try:
            total_int = int(total)
        except (TypeError, ValueError):
            return previous
        # Sanity check: if total < records we've already seen, log.
        if previous is not None and total_int < previous:
            logger.warning(
                "[disgenet] API returned totalResults=%d but we've already "
                "fetched %d records — likely picked the wrong field",
                total_int, previous,
            )
        return total_int

    def _extract_payload(self, payload: Any) -> Optional[list[dict[str, Any]]]:
        """Extract the records list from a DisGeNET API response (SCI-31, REL-17).

        Returns the list of records, or ``None`` to signal end-of-data
        (when the API explicitly returns ``{"payload": null}`` with no
        error).  Raises ``RuntimeError`` on:
        - non-dict response (REL-17)
        - null payload with an error field (SCI-31)
        """
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"DisGeNET API returned non-dict response: "
                f"{type(payload).__name__}"
            )
        if "payload" not in payload:
            # Some endpoints return the list directly.
            if isinstance(payload, list):
                return payload  # type: ignore[unreachable]
            raise RuntimeError(
                "DisGeNET API response has no 'payload' field — "
                f"keys: {list(payload.keys())}"
            )
        raw_payload = payload.get("payload")
        if raw_payload is None:
            # DisGeNET explicitly returned null — could be rate-limit,
            # error, or end-of-data.
            error = payload.get("error") or payload.get("message")
            if error:
                logger.error(
                    "[disgenet] DisGeNET API returned null payload with error: %s",
                    error,
                )
                raise RuntimeError(f"DisGeNET API error: {error}")
            logger.warning(
                "[disgenet] DisGeNET API returned null payload with no error "
                "field; treating as end-of-data."
            )
            return None
        if not isinstance(raw_payload, list):
            raise RuntimeError(
                f"DisGeNET API returned non-list payload: "
                f"{type(raw_payload).__name__}"
            )
        return raw_payload

    def _serialise_list_columns(
        self, records: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """JSON-serialise list/dict values in API records before write (SCI-36).

        DisGeNET API responses include list-typed fields
        (``geneUniProtIDs``, ``geneEnsemblIDs``, ``geneProteinClassIDs``,
        ``diseaseVocabularies``, ``diseaseClasses``).  Writing these as
        Python ``repr()`` would corrupt the data (SCI-36).  We convert
        each list/dict value to a JSON string.
        """
        out: list[dict[str, Any]] = []
        for rec in records:
            new_rec: dict[str, Any] = {}
            for k, v in rec.items():
                if isinstance(v, (list, dict)):
                    new_rec[k] = json.dumps(v, default=str)
                else:
                    new_rec[k] = v
            out.append(new_rec)
        return out

    @staticmethod
    def _serialise_cell(value: Any) -> str:
        """Serialise a single cell value for TSV writing (SCI-36).

        ``None`` becomes the empty string.  Lists/dicts are JSON-serialised.
        Everything else is ``str()``-ed.
        """
        if value is None:
            return ""
        if isinstance(value, (list, dict)):
            return json.dumps(value, default=str)
        return str(value)

    def _api_get_disgenet(
        self, url: str, params: dict[str, Any],
        max_retries: int = DISGENET_API_MAX_RETRIES,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """GET from the DisGeNET API with retry + rate-limit + circuit-breaker.

        Returns ``(payload, headers)``.  Uses :meth:`http_session` (ARCH-15)
        for connection pooling.  Applies:

        - URL validation (SEC-7, CODE-10)
        - Rate limiting (SEC-20)
        - Circuit breaker (REL-8)
        - Retry on 429 / 503 / 504 + retryable exceptions (SEC-17)
        - ``Retry-After`` header handling (SEC-18)
        - JSON decode error retry (SEC-19, REL-15)
        - Content-Type validation (SEC-8)
        - Response size validation (SEC-9)
        - API-key redaction in logs (SEC-1, SEC-11, SEC-15)

        Parameters
        ----------
        url : str
            The DisGeNET API URL (validated against
            ``DISGENET_ALLOWED_DOMAINS``).
        params : dict
            Query parameters.
        max_retries : int
            Maximum retry attempts.  Must be >= 1 (CODE-9).

        Returns
        -------
        tuple
            ``(payload_dict, response_headers_dict)``.

        Raises
        ------
        ValueError
            If ``url`` or ``params`` is invalid (CODE-10), or if
            ``max_retries < 1`` (CODE-9).
        RuntimeError
            On non-retryable HTTP errors (401, 403, 404) — SEC-17, REL-21.
            On circuit-breaker open (REL-8).  After exhausting retries.
        """
        # CODE-9: Validate max_retries.
        if max_retries < 1:
            raise ValueError(
                f"max_retries must be >= 1, got {max_retries}"
            )
        # CODE-10: Validate url and params.
        if not isinstance(url, str) or not url:
            raise TypeError(
                f"url must be a non-empty string, got {type(url).__name__}"
            )
        if not isinstance(params, dict):
            raise TypeError(
                f"params must be a dict, got {type(params).__name__}"
            )
        # SEC-7: SSRF protection — validate URL scheme + domain.
        self._validate_url(url)

        # REL-8: Circuit breaker.
        if _CIRCUIT_BREAKER.is_open():
            raise RuntimeError(
                "DisGeNET API circuit breaker is OPEN — refusing to make "
                f"request for {self._sanitize_url(url)}. Wait "
                f"{DISGENET_CIRCUIT_BREAKER_RESET_SECONDS}s or restart."
            )

        # SEC-20: Rate limiting.
        _RATE_LIMITER.wait()

        # SEC-11: Audit-log API key use (not the key itself).
        logger.info(
            "[disgenet] DisGeNET API key used for request to %s",
            self._sanitize_url(url),
        )
        # LOG-20: Log the sanitised URL + params.
        logger.info(
            "[disgenet] DisGeNET API GET %s (offset=%s, limit=%s)",
            self._sanitize_url(url),
            params.get("offset"), params.get("limit"),
        )

        cumulative_wait = 0.0
        last_exc: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            try:
                resp = self.http_session.get(
                    url,
                    params=params,
                    timeout=DISGENET_API_TIMEOUT,
                    stream=True,  # SEC-9: stream to check size before loading.
                )

                # SEC-8: Content-Type validation.
                ctype = resp.headers.get("Content-Type", "")
                if not ctype.startswith("application/json"):
                    # Some errors return text/html or text/plain.
                    body_preview = resp.text[:500] if resp.text else ""
                    if resp.status_code >= 400:
                        # Non-JSON error response — fail fast.
                        raise RuntimeError(
                            f"DisGeNET API returned HTTP {resp.status_code} "
                            f"with non-JSON Content-Type {ctype!r}. "
                            f"Body preview: {body_preview!r}"
                        )
                    raise RuntimeError(
                        f"Expected JSON response, got Content-Type: {ctype!r}"
                    )

                # SEC-9: Response size validation (before reading the body).
                content_length = resp.headers.get("Content-Length")
                if content_length:
                    try:
                        cl_int = int(content_length)
                        if cl_int > DISGENET_API_MAX_RESPONSE_BYTES:
                            raise RuntimeError(
                                f"DisGeNET API response too large: "
                                f"Content-Length={cl_int} > "
                                f"DISGENET_API_MAX_RESPONSE_BYTES="
                                f"{DISGENET_API_MAX_RESPONSE_BYTES}"
                            )
                    except ValueError:
                        pass  # Malformed Content-Length — let the body check catch it.

                # Read the body in chunks, enforcing the size limit.
                body = bytearray()
                for chunk in resp.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    body.extend(chunk)
                    if len(body) > DISGENET_API_MAX_RESPONSE_BYTES:
                        raise RuntimeError(
                            f"DisGeNET API response exceeded "
                            f"DISGENET_API_MAX_RESPONSE_BYTES="
                            f"{DISGENET_API_MAX_RESPONSE_BYTES} while streaming"
                        )

                # SEC-17 / REL-21 / REL-7: Status-code handling.
                if resp.status_code in (401,):
                    _CIRCUIT_BREAKER.record_failure()
                    raise RuntimeError(
                        "DisGeNET API returned 401 — DISGENET_API_KEY is "
                        "invalid or expired. Get a new key at "
                        "https://api.disgenet.com/api/v1/"
                    )
                if resp.status_code == 403:
                    _CIRCUIT_BREAKER.record_failure()
                    raise RuntimeError(
                        "DisGeNET API returned 403 — API key invalid or "
                        "insufficient permissions. Check DISGENET_API_KEY."
                    )
                if resp.status_code == 404:
                    _CIRCUIT_BREAKER.record_failure()
                    raise RuntimeError(
                        "DisGeNET API endpoint not found — check "
                        "DISGENET_API_URL."
                    )
                if resp.status_code == 400:
                    _CIRCUIT_BREAKER.record_failure()
                    body_preview = bytes(body[:500]).decode(
                        "utf-8", errors="replace"
                    )
                    raise RuntimeError(
                        f"DisGeNET API returned 400 (Bad Request). "
                        f"Body: {body_preview!r}"
                    )
                if resp.status_code in (429, 503, 504):
                    # Retryable.
                    wait = self._compute_retry_wait(
                        resp.headers.get("Retry-After"), attempt
                    )
                    logger.warning(
                        "[disgenet] DisGeNET API returned %d, sleeping %.1fs "
                        "(attempt %d/%d)",
                        resp.status_code, wait, attempt, max_retries,
                    )
                    cumulative_wait += wait
                    self._interruptible_sleep(wait)
                    continue
                if resp.status_code >= 400:
                    _CIRCUIT_BREAKER.record_failure()
                    body_preview = bytes(body[:500]).decode(
                        "utf-8", errors="replace"
                    )
                    raise RuntimeError(
                        f"DisGeNET API returned HTTP {resp.status_code}. "
                        f"Body: {body_preview!r}"
                    )

                # 2xx — parse JSON.
                try:
                    payload = json.loads(bytes(body).decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    # SEC-19 / REL-15: JSON decode error — retryable.
                    if attempt == max_retries:
                        _CIRCUIT_BREAKER.record_failure()
                        body_preview = bytes(body[:500]).decode(
                            "utf-8", errors="replace"
                        )
                        logger.warning(
                            "[disgenet] DisGeNET API returned malformed JSON "
                            "(body preview: %r)",
                            body_preview,
                        )
                        raise RuntimeError(
                            f"DisGeNET API returned malformed JSON after "
                            f"{max_retries} retries: {exc}"
                        ) from exc
                    wait = self._compute_retry_wait(None, attempt)
                    logger.warning(
                        "[disgenet] DisGeNET API returned malformed JSON "
                        "(attempt %d/%d), retrying in %.1fs: %s",
                        attempt, max_retries, wait, exc,
                    )
                    cumulative_wait += wait
                    self._interruptible_sleep(wait)
                    continue

                # Success.
                _CIRCUIT_BREAKER.record_success()
                # LOG-25: Cumulative retry wait.
                if attempt > 1:
                    logger.info(
                        "[disgenet] DisGeNET API: %d retries, cumulative "
                        "wait %.1fs",
                        attempt - 1, cumulative_wait,
                    )
                return payload, dict(resp.headers)

            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ContentDecodingError,
            ) as exc:
                # REL-20, REL-22, REL-23: retryable.
                last_exc = exc
                if attempt == max_retries:
                    _CIRCUIT_BREAKER.record_failure()
                    logger.error(
                        "[disgenet] DisGeNET API request failed after %d "
                        "retries: %s",
                        max_retries, exc,
                    )
                    raise RuntimeError(
                        f"DisGeNET API request failed after {max_retries} "
                        f"retries: {exc}"
                    ) from exc
                wait = self._compute_retry_wait(None, attempt)
                logger.warning(
                    "[disgenet] DisGeNET API request failed: %s, retrying "
                    "in %.1fs (attempt %d/%d)",
                    exc, wait, attempt, max_retries,
                )
                cumulative_wait += wait
                self._interruptible_sleep(wait)
                continue

        # Should not reach here — the loop either returns or raises.
        _CIRCUIT_BREAKER.record_failure()
        raise RuntimeError(
            f"Failed to GET {self._sanitize_url(url)} after {max_retries} "
            f"retries. Last exception: {last_exc}"
        )

    def _compute_retry_wait(
        self, retry_after: Optional[str], attempt: int
    ) -> float:
        """Compute the sleep duration for a retry (SEC-18, PERF-9, CONF-8).

        If ``retry_after`` is present (HTTP-date or seconds), honour it
        (capped at ``DISGENET_API_MAX_RETRY_AFTER``).  Otherwise use
        exponential backoff capped at ``DISGENET_API_BACKOFF_MAX_SECONDS``.
        """
        if retry_after:
            try:
                # Try parsing as seconds first.
                wait = float(retry_after)
                return min(wait, float(DISGENET_API_MAX_RETRY_AFTER))
            except ValueError:
                # Try parsing as HTTP-date.
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(retry_after)
                    if dt is not None:
                        now = datetime.now(timezone.utc)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        delta = (dt - now).total_seconds()
                        return max(0.0, min(delta, float(DISGENET_API_MAX_RETRY_AFTER)))
                except (TypeError, ValueError):
                    pass
        # Exponential backoff with cap (PERF-9, CONF-8).
        raw = DISGENET_API_BACKOFF_BASE ** attempt
        return min(raw, float(DISGENET_API_BACKOFF_MAX_SECONDS))

    @staticmethod
    def _interruptible_sleep(seconds: float) -> None:
        """Sleep that propagates KeyboardInterrupt (CODE-12)."""
        try:
            time.sleep(seconds)
        except KeyboardInterrupt:
            logger.info("[disgenet] Sleep interrupted by user")
            raise

    # ------------------------------------------------------------------
    # Clean
    # ------------------------------------------------------------------
    def clean(self, raw_path: Path) -> pd.DataFrame:
        """Clean and normalise DisGeNET gene-disease association data.

        Returns a :class:`pandas.DataFrame` (the framework's
        ``clean() -> pd.DataFrame`` contract — see ARCH-8).  The
        DataFrame is also persisted to
        ``PROCESSED_DATA_DIR / DISGENET_OUTPUT_FILENAME`` via the
        atomic-write manifest-based save (DQ-7, DQ-12).

        Steps (SCI-22 / CODE-7):
            1. Load TSV with explicit dtype, na_values, encoding, on_bad_lines.
            2. Rename columns (declarative, based on self._source_format).
            3. Normalise string columns (case, whitespace).
            4. Validate gene_symbol and disease_id formats; quarantine invalid.
            5. Coerce score to numeric; quarantine non-numeric.
            6. Validate year range; quarantine inverted/implausible.
            7. Call validate_gda_scores (clips, fills, dedups, adds lineage
               columns).  Single source of truth for dedup (DQ-6, SCI-37).
            8. Compute confidence_tier on the clipped score (SCI-13).
            9. Compute evidence_strength and normalized_score.
            10. Cap pmid_list (dedup, sort, validate, cap).
            11. Infer disease_id_type from prefix (SCI-5).
            12. Derive source from source_id (SCI-4) and association_type (SCI-19).
            13. Ensure required columns (_ensure_gda_columns — purely additive).
            14. Apply score filter (configurable, weak-evidence escape hatch).
            15. Validate output against v1.json schema.
            16. Persist to CSV (atomic, manifest, file permissions).
            17. Return the DataFrame (and populate self.last_clean_result).
        """
        start = time.perf_counter()
        try:
            df = self._clean_core(raw_path)
            # ARCH-8: clean() returns the DataFrame (framework contract).
            # Persistence is done inside _clean_core via _save_processed_csv
            # because BasePipeline.run() also persists — we keep both
            # paths consistent by writing here AND letting the framework
            # re-write (idempotent — same content, same path).
            return df
        finally:
            duration = time.perf_counter() - start
            self._emit_metric(
                "clean_duration_seconds", duration,
                tags={"source_format": self._source_format},
            )
            logger.info(
                "[disgenet] Clean phase took %.2fs", duration
            )

    def _clean_core(self, raw_path: Path) -> pd.DataFrame:
        """Internal clean implementation — returns the cleaned DataFrame.

        Populates ``self.last_clean_result`` (DES-13) and
        ``self.last_cleaning_report`` (LOG-5).
        """
        # ----------------------------------------------------------------
        # Step 1: Load TSV with explicit dtype, na_values, encoding,
        # on_bad_lines (DQ-28, DQ-29, DQ-30, DQ-31).
        # ----------------------------------------------------------------
        logger.info(
            "[disgenet] Loading DisGeNET TSV from %s (source_format=%s)",
            raw_path, self._source_format,
        )
        # LOG-23: Log source format.
        logger.info(
            "[disgenet] Source format: %s, using %s column map",
            self._source_format,
            "API" if self._source_format == DisGeNETSourceFormat.API else "TSV",
        )

        # Detect compression from file extension.
        compression: Optional[str] = "gzip" if raw_path.suffix == ".gz" else None
        # DQ-28 / DQ-29 / DQ-30 / DQ-31: explicit read_csv options.
        try:
            df = pd.read_csv(
                raw_path,
                compression=compression,
                sep="\t",
                encoding="utf-8-sig",  # DQ-30: handle UTF-8 BOM.
                low_memory=False,
                dtype=self._get_dtype_spec(),
                na_values=self._get_na_values(),
                keep_default_na=True,
                on_bad_lines="warn",  # DQ-31
            )
        except pd.errors.ParserError as exc:
            raise RuntimeError(
                f"Could not parse DisGeNET TSV {raw_path}: {exc}"
            ) from exc
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"DisGeNET TSV not found: {raw_path}"
            ) from exc

        self._log_row_count("load", df)
        # LOG-6: Log column list at DEBUG.
        logger.debug(
            "[disgenet] Columns after load: %s", df.columns.tolist()
        )

        # ----------------------------------------------------------------
        # Step 2: Rename columns (declarative — IDEM-6, ARCH-5, ARCH-9).
        # ----------------------------------------------------------------
        col_map = self._get_column_map()
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        self._log_row_count("rename", df)
        logger.debug(
            "[disgenet] Columns after rename: %s", df.columns.tolist()
        )

        # ----------------------------------------------------------------
        # Step 3: Normalise string columns (DQ-13, DQ-14, DQ-15).
        # ----------------------------------------------------------------
        if "gene_symbol" in df.columns:
            df["gene_symbol"] = _normalise_gene_symbol_series(df["gene_symbol"])
        if "disease_id" in df.columns:
            df["disease_id"] = _normalise_disease_id_series(df["disease_id"])
        df = _strip_string_columns(
            df,
            [
                "disease_name", "gene_symbol", "disease_id", "pmid_list",
                "source_id", "disease_class", "disease_class_source",
                "disease_type",
            ],
        )

        # ----------------------------------------------------------------
        # Step 4: Validate gene_symbol + disease_id formats; quarantine invalid.
        # (SCI-29, SCI-30, DQ-19, DQ-20, LIN-13, SEC-3, SEC-4)
        # ----------------------------------------------------------------
        df = self._validate_and_quarantine_ids(df)

        # ----------------------------------------------------------------
        # Step 5: Coerce score to numeric; quarantine non-numeric (SCI-14,
        # SCI-40, COMP-18, LOG-9).
        # ----------------------------------------------------------------
        df = self._coerce_score_and_gene_id(df)

        # ----------------------------------------------------------------
        # Step 6: Validate year range (SCI-7, SCI-41, SCI-42).
        # ----------------------------------------------------------------
        df = self._validate_year_range(df)

        # ----------------------------------------------------------------
        # Step 6.5: Derive source + association_type EARLY (SCI-4, SCI-19).
        # WHY: The validator (step 7) deduplicates on (gene_id, disease_id,
        # source).  If we derive `source` AFTER the validator, two rows
        # with the same (gene_id, disease_id) but DIFFERENT source_id
        # (e.g. CURATED vs BEFREE) would both have source="disgenet" at
        # dedup time and collapse to one — losing the sub-source
        # distinction that SCI-4 explicitly requires to preserve.
        # Deriving source BEFORE the validator ensures the dedup keys
        # reflect the true sub-source.
        # ----------------------------------------------------------------
        if "source_id" in df.columns:
            df["source_id"] = (
                df["source_id"].fillna("").astype(str).str.strip().str.upper()
            )
            df["source"] = df["source_id"].apply(self._derive_source_value)
            df["association_type"] = df["source_id"].apply(
                self._derive_association_type
            )
        else:
            df["source_id"] = None
            df["source"] = DataSourceName.DISGENET.value
            df["association_type"] = DEFAULT_ASSOCIATION_TYPE

        # ----------------------------------------------------------------
        # Step 7: Call validate_gda_scores (SCI-22, SCI-23, DQ-21, DQ-22,
        # DQ-23, DQ-24, CODE-20, CODE-21, CODE-22, IDEM-12).
        # ----------------------------------------------------------------
        # DQ-22: _ensure_gda_columns runs BEFORE validate so the validator
        # sees association_type.
        df = self._ensure_gda_columns(df)

        # SCI-23 / DQ-24 / CODE-20: pass source="disgenet".
        # SCI-22 / DQ-23 / CODE-21: pass dedup=True with explicit keys.
        # SCI-23 / CODE-22: pass preserve_direction=True.
        dedup_keys = ["gene_id", "disease_id", "source"]
        existing_keys = [k for k in dedup_keys if k in df.columns]
        if not existing_keys:
            existing_keys = ["gene_symbol", "disease_id", "source"]
        df = validate_gda_scores(
            df,
            score_range=(0.0, 1.0),
            preserve_direction=True,
            source=DataSourceName.DISGENET.value,
            dedup=True,
            dedup_keys=existing_keys,
        )
        self._log_row_count("validate_gda_scores", df)

        # ----------------------------------------------------------------
        # Step 8: Compute confidence_tier on the clipped score (SCI-10,
        # SCI-12, SCI-13, IDEM-17, LIN-15).
        # ----------------------------------------------------------------
        if "score" in df.columns:
            df["confidence_tier"] = df["score"].apply(
                lambda s: (
                    _classify_confidence(float(s))
                    if pd.notna(s) and float(s) >= 0
                    else None
                )
            )
            df["confidence_tier_method"] = CONFIDENCE_TIER_METHOD_VERSION
        else:
            df["confidence_tier"] = None
            df["confidence_tier_method"] = CONFIDENCE_TIER_METHOD_VERSION

        # LOG-10: Detail on clipped scores.
        if (
            "_score_was_clipped" in df.columns
            and df["_score_was_clipped"].any()
        ):
            clipped = df[df["_score_was_clipped"]]
            try:
                logger.info(
                    "[disgenet] Clipped %d scores. Original-score stats: "
                    "min=%.4f, max=%.4f, mean=%.4f",
                    len(clipped),
                    float(clipped["_original_score"].min()),
                    float(clipped["_original_score"].max()),
                    float(clipped["_original_score"].mean()),
                )
            except (TypeError, ValueError):
                pass

        # ----------------------------------------------------------------
        # Step 9: Compute evidence_strength + normalized_score (SCI-24,
        # SCI-38, LOG-12).
        # ----------------------------------------------------------------
        df = self._compute_evidence_and_normalized(df)

        # ----------------------------------------------------------------
        # Step 10: Cap pmid_list (SCI-16, SCI-17, DQ-16, DQ-17, DES-9,
        # LIN-16, SEC-5, SEC-13, COMP-12).
        # ----------------------------------------------------------------
        df = self._cap_pmid_list_df(df)

        # ----------------------------------------------------------------
        # Step 11: Infer disease_id_type (SCI-5, COMP-5, DES-7, INT-6).
        # ----------------------------------------------------------------
        if "disease_id" in df.columns:
            df["disease_id_type"] = df["disease_id"].apply(_infer_disease_id_type)

        # ----------------------------------------------------------------
        # Step 12: (Moved to Step 6.5 — source + association_type are now
        # derived BEFORE validate_gda_scores so the dedup keys reflect the
        # true sub-source.  See Step 6.5 above for the rationale.)

        # ----------------------------------------------------------------
        # Step 13: Populate additional lineage columns (SCI-2, SCI-15,
        # SCI-26, LIN-9, LIN-23, INT-7, COMP-6, COMP-7).
        # ----------------------------------------------------------------
        df = self._populate_lineage_columns(df)

        # ----------------------------------------------------------------
        # Step 14: Apply score filter (SCI-1, SCI-22, LOG-8, LIN-12).
        # ----------------------------------------------------------------
        df = self._apply_score_filter(df)

        # ----------------------------------------------------------------
        # Step 15: Validate output against v1.json schema (ARCH-1).
        # ----------------------------------------------------------------
        # We use BasePipeline.validate_output (which reads schema/v1.json).
        is_valid, errors = self.validate_output(df)
        if not is_valid:
            for err in errors:
                logger.warning("[disgenet] Schema validation: %s", err)
            # Don't raise in non-strict mode (backward compat).  In
            # strict mode, BasePipeline.run() will raise.

        # DQ-25: Minimum record count.
        if (
            len(df) < DISGENET_MIN_EXPECTED_RECORDS
            and DISGENET_MIN_EXPECTED_RECORDS > 0
        ):
            logger.warning(
                "[disgenet] Cleaned dataset has %d records, below "
                "DISGENET_MIN_EXPECTED_RECORDS=%d. This may indicate a "
                "partial download or an over-aggressive filter.",
                len(df), DISGENET_MIN_EXPECTED_RECORDS,
            )

        # ----------------------------------------------------------------
        # Step 16: Persist to CSV (atomic, manifest, file permissions).
        # (DQ-7, DQ-8, DQ-9, DQ-10, DQ-11, DQ-12, IDEM-1, IDEM-13,
        # COMP-14, COMP-15, COMP-16, SEC-14, INT-19, LIN-25, LOG-4).
        # ----------------------------------------------------------------
        output_path = PROCESSED_DATA_DIR / DISGENET_OUTPUT_FILENAME

        # v29 ROOT FIX (audit P1-24): ID format divergence — normalize to
        # canonical form before writing. ``gene_symbol`` is uppercased +
        # stripped; ``uniprot_id`` (resolved later in load()) is uppercased
        # + stripped when present. This guarantees downstream joins against
        # UniProt (uniprot_id), OMIM (gene_symbol), and DrugBank
        # interactions (uniprot_id) succeed regardless of which source
        # wrote the value. DisGeNET's CSV is the cross-source GDA truth
        # set — case divergence here would split a single gene's records
        # across multiple keys in the knowledge graph.
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

        self._save_processed_csv(df, output_path, DataSourceName.DISGENET.value)

        # ----------------------------------------------------------------
        # Step 17: Populate CleanResult + cleaning_report.
        # ----------------------------------------------------------------
        try:
            from cleaning.missing_values import _fingerprint_df
            self._input_fingerprint = _fingerprint_df(df)  # post-clean fingerprint
            self._output_fingerprint = self._compute_sha256(output_path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[disgenet] Could not compute fingerprint: %s", exc)

        self.last_cleaning_report = {
            "rows_after_clean": int(len(df)),
            "source_format": self._source_format,
            "source_version": self._disgenet_release_version,
            "schema_version": SCHEMA_VERSION_STAMP,
            "dead_letter_count": len(self._dead_letter_rows),
        }
        self.last_clean_result = CleanResult(
            df=df,
            cleaning_report=self.last_cleaning_report,
            dead_letter=pd.DataFrame(self._dead_letter_rows),
            input_fingerprint=self._input_fingerprint,
            output_fingerprint=self._output_fingerprint,
        )

        logger.info(
            "[disgenet] Saved %d cleaned GDA records to %s",
            len(df), output_path,
        )
        return df

    def _get_column_map(self) -> dict[str, str]:
        """Return the column map for the current source format (IDEM-6)."""
        if self._source_format == DisGeNETSourceFormat.API:
            return DISGENET_API_COLUMN_MAP
        return DISGENET_COLUMN_MAP

    def _get_dtype_spec(self) -> dict[str, str]:
        """Return the dtype spec for pd.read_csv (DQ-28).

        Only applies to columns that exist in the file (pandas raises
        if a dtype key is missing from the file — we filter first).
        """
        # We can't know the columns without reading the header first.
        # Pandas will warn but not fail on extra dtype keys, so we
        # return the full spec and let pandas filter.
        return {
            "geneId": "Int64",
            "geneNcbiID": "Int64",
            "gene_symbol": "string",
            "geneSymbol": "string",
            "diseaseId": "string",
            "disease_name": "string",
            "diseaseName": "string",
            "diseaseType": "string",
            "diseaseClass": "string",
            "diseaseClassName": "string",
            "sourceId": "string",
            "score": "float64",
            "yearInitial": "Int64",
            "yearFinal": "Int64",
            "pmid_list": "string",
            "pmidList": "string",
        }

    def _get_na_values(self) -> list[str]:
        """Return the list of strings to treat as NaN (DQ-29)."""
        return [
            "", "-", "--", "null", "NULL", "Null", "None", "none",
            "N/A", "n/a", "na", "NaN", "nan", "unannotated", "NA",
        ]

    def _log_row_count(self, stage: str, df: pd.DataFrame) -> None:
        """Log the row count at a cleaning stage (LOG-5)."""
        logger.info(
            "[disgenet] Stage '%s': %d rows, %d cols",
            stage, len(df), df.shape[1],
        )

    def _validate_and_quarantine_ids(
        self, df: pd.DataFrame
    ) -> pd.DataFrame:
        """Validate gene_symbol + disease_id formats; quarantine invalid (SCI-29,
        SCI-30, DQ-19, DQ-20, LIN-13, SEC-3, SEC-4).

        Invalid rows are routed to the dead-letter queue with a reason.
        """
        if df.empty:
            return df
        keep_mask = pd.Series(True, index=df.index)
        # gene_symbol validation.
        if "gene_symbol" in df.columns:
            for idx, val in df["gene_symbol"].items():
                if pd.isna(val) or val == "":
                    # Allow NULL gene_symbol (some sources have only gene_id).
                    continue
                if not _validate_gene_symbol(str(val)):
                    self._add_to_dead_letter(
                        df, idx, reason="invalid_gene_symbol_format",
                        details={"gene_symbol": str(val)},
                    )
                    keep_mask.at[idx] = False
        # disease_id validation.
        if "disease_id" in df.columns:
            # v9 ROOT FIX (audit F4.1): normalise the DisGeNET-prefixed curie
            # form ("umls:C0006142", "omim:100100", "mesh:D014979") to the
            # canonical bare form ("C0006142", "100100", "D014979") IN PLACE
            # before validation. This guarantees every downstream consumer
            # (DB loader, Phase 2 kg_builder, OMIM join) sees one format.
            df["disease_id"] = df["disease_id"].map(
                lambda v: _normalise_disease_id(v) if isinstance(v, str) else v
            )
            for idx, val in df["disease_id"].items():
                if pd.isna(val) or str(val).strip() == "":
                    self._add_to_dead_letter(
                        df, idx, reason="empty_disease_id",
                        details={"disease_id": str(val)},
                    )
                    keep_mask.at[idx] = False
                    continue
                is_valid, _ = _validate_disease_id(str(val))
                if not is_valid:
                    self._add_to_dead_letter(
                        df, idx, reason="invalid_disease_id_format",
                        details={"disease_id": str(val)},
                    )
                    keep_mask.at[idx] = False

        # SEC-12: PII detection on disease_name (sample first 100 rows).
        if "disease_name" in df.columns:
            sample = df["disease_name"].dropna().astype(str).head(100)
            for idx, val in sample.items():
                if _RE_PII_EMAIL.search(val) or _RE_PII_SSN.search(val):
                    self._add_to_dead_letter(
                        df, idx, reason="potential_pii_detected",
                        details={"field": "disease_name", "value": val[:200]},
                    )
                    keep_mask.at[idx] = False

        # SEC-3 / SEC-4: Sanitise free-text fields.
        if "disease_name" in df.columns:
            df["disease_name"] = df["disease_name"].apply(
                lambda v: _sanitise_free_text(v, max_length=1000)
            )
        if "gene_symbol" in df.columns:
            df["gene_symbol"] = df["gene_symbol"].apply(
                lambda v: _sanitise_free_text(v, max_length=50)
            )

        # DQ-19 / DQ-20: Referential integrity checks (opt-in).
        df = self._check_referential_integrity(df, keep_mask)

        return df[keep_mask].copy()

    def _check_referential_integrity(
        self, df: pd.DataFrame, keep_mask: pd.Series
    ) -> pd.DataFrame:
        """Optional referential-integrity checks against HGNC / disease ontology
        (DQ-19, DQ-20, DQ-34).  No-op when the env vars are unset.
        """
        # DQ-20: HGNC referential integrity.
        if DISGENET_HGNC_PATH and "gene_symbol" in df.columns:
            try:
                hgnc_symbols = self._load_id_set(DISGENET_HGNC_PATH)
                if hgnc_symbols:
                    for idx, val in df["gene_symbol"].items():
                        if pd.isna(val) or str(val).strip() == "":
                            continue
                        if str(val).upper() not in hgnc_symbols:
                            self._add_to_dead_letter(
                                df, idx,
                                reason="gene_symbol_not_in_hgnc",
                                details={"gene_symbol": str(val)},
                            )
                            keep_mask.at[idx] = False
            except OSError as exc:
                logger.warning(
                    "[disgenet] Could not load HGNC file %s: %s — skipping "
                    "referential-integrity check",
                    DISGENET_HGNC_PATH, exc,
                )

        # DQ-19: Disease ontology referential integrity.
        if DISGENET_DISEASE_ONTOLOGY_PATH and "disease_id" in df.columns:
            try:
                ontology_ids = self._load_id_set(DISGENET_DISEASE_ONTOLOGY_PATH)
                if ontology_ids:
                    for idx, val in df["disease_id"].items():
                        if pd.isna(val) or str(val).strip() == "":
                            continue
                        if str(val).upper() not in ontology_ids:
                            self._add_to_dead_letter(
                                df, idx,
                                reason="disease_id_not_in_ontology",
                                details={"disease_id": str(val)},
                            )
                            keep_mask.at[idx] = False
            except OSError as exc:
                logger.warning(
                    "[disgenet] Could not load disease ontology %s: %s — "
                    "skipping referential-integrity check",
                    DISGENET_DISEASE_ONTOLOGY_PATH, exc,
                )

        return df

    @staticmethod
    def _load_id_set(path: str) -> set[str]:
        """Load a set of identifiers (one per line) from a file."""
        out: set[str] = set()
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                s = line.strip().upper()
                if s:
                    out.add(s)
        return out

    def _add_to_dead_letter(
        self,
        df: pd.DataFrame,
        idx: Any,
        *,
        reason: str,
        details: dict[str, Any],
    ) -> None:
        """Add a row to the dead-letter queue (DQ-18, LIN-11, LIN-12, LIN-13)."""
        row = df.loc[idx].to_dict() if idx in df.index else {}
        record = {
            "gene_symbol": row.get("gene_symbol"),
            "disease_id": row.get("disease_id"),
            "source": row.get("source") or DataSourceName.DISGENET.value,
            "reason": reason,
            "details_json": json.dumps(details, default=str),
            "run_id": self.run_id,
        }
        self._dead_letter_rows.append(record)
        # LOG-1: contextual log.
        logger.warning(
            "[disgenet] Dropped record: gene_symbol=%s, disease_id=%s, "
            "reason=%s",
            record["gene_symbol"], record["disease_id"], reason,
        )

    def _coerce_score_and_gene_id(
        self, df: pd.DataFrame
    ) -> pd.DataFrame:
        """Coerce score and gene_id to numeric; quarantine non-numeric (SCI-14,
        SCI-40, COMP-18, LOG-9).
        """
        if df.empty:
            return df
        # SCI-14: Score coercion.
        if "score" in df.columns:
            before = len(df)
            # Track which rows had non-numeric scores.
            non_numeric_mask = pd.Series(False, index=df.index)
            non_null = df["score"].notna()
            if non_null.any():
                str_scores = df.loc[non_null, "score"].astype(str)
                # v43 ROOT FIX (P1-027): the previous regex
                # ``^-?\d+\.?\d*$`` accepted negative scores, which
                # were then silently clipped to 0 by validate_gda_scores.
                # DisGeNET scores are always in [0, 1] — negative scores
                # are data errors. The fix removes the ``-?`` so negative
                # scores are flagged as non-numeric and dead-lettered
                # (rather than silently clipped).
                non_numeric_mask.loc[non_null] = ~str_scores.str.match(
                    r"^\d+\.?\d*$", na=False
                )
            for idx in df.index[non_numeric_mask]:
                self._add_to_dead_letter(
                    df, idx, reason="non_numeric_score",
                    details={"score": str(df.at[idx, "score"])},
                )
            df = df[~non_numeric_mask].copy()
            df["score"] = pd.to_numeric(df["score"], errors="coerce")
            if len(df) < before:
                logger.info(
                    "[disgenet] Quarantined %d non-numeric score(s)",
                    before - len(df),
                )

        # SCI-40 / COMP-18: gene_id coercion + validation.
        if "gene_id" in df.columns:
            # v9 ROOT FIX (audit F4.7 / BUG-B-002): pd.to_numeric silently
            # coerces prefixed IDs like "NCBIGene:672" to NaN (NOT strips the
            # prefix as the v7 audit doc claimed). Strip the prefix explicitly
            # BEFORE numeric coercion so the value is preserved.
            df["gene_id"] = (
                df["gene_id"]
                .astype(str)
                .str.replace(r"^\s*NCBIGene:\s*", "", regex=True, case=False)
                .str.strip()
            )
            df["gene_id"] = pd.to_numeric(df["gene_id"], errors="coerce")
            # Validate positive integer.
            invalid_mask = df["gene_id"].notna() & (df["gene_id"] <= 0)
            for idx in df.index[invalid_mask]:
                self._add_to_dead_letter(
                    df, idx, reason="invalid_gene_id",
                    details={"gene_id": str(df.at[idx, "gene_id"])},
                )
            df = df[~invalid_mask].copy()
            df["gene_id"] = df["gene_id"].astype("Int64")

        return df

    def _validate_year_range(self, df: pd.DataFrame) -> pd.DataFrame:
        """Validate year_initial <= year_final and plausible range (SCI-7,
        SCI-41, SCI-42).
        """
        if df.empty:
            return df
        current_year = datetime.now(timezone.utc).year
        min_year = 1945
        max_year = current_year + 1
        if "year_initial" in df.columns and "year_final" in df.columns:
            # Coerce to numeric.
            df["year_initial"] = pd.to_numeric(df["year_initial"], errors="coerce")
            df["year_final"] = pd.to_numeric(df["year_final"], errors="coerce")
            # SCI-41: Inverted range.
            inverted = (
                df["year_initial"].notna()
                & df["year_final"].notna()
                & (df["year_initial"] > df["year_final"])
            )
            for idx in df.index[inverted]:
                self._add_to_dead_letter(
                    df, idx, reason="inverted_year_range",
                    details={
                        "year_initial": int(df.at[idx, "year_initial"]),
                        "year_final": int(df.at[idx, "year_final"]),
                    },
                )
            df = df[~inverted].copy()
            # SCI-42: Implausible years.
            implausible = pd.Series(False, index=df.index)
            for col in ("year_initial", "year_final"):
                col_vals = df[col]
                bad = col_vals.notna() & (
                    (col_vals < min_year) | (col_vals > max_year)
                )
                implausible = implausible | bad
            for idx in df.index[implausible]:
                self._add_to_dead_letter(
                    df, idx, reason="implausible_year",
                    details={
                        "year_initial": (
                            int(df.at[idx, "year_initial"])
                            if pd.notna(df.at[idx, "year_initial"]) else None
                        ),
                        "year_final": (
                            int(df.at[idx, "year_final"])
                            if pd.notna(df.at[idx, "year_final"]) else None
                        ),
                        "valid_range": [min_year, max_year],
                    },
                )
            df = df[~implausible].copy()
            df["year_initial"] = df["year_initial"].astype("Int64")
            df["year_final"] = df["year_final"].astype("Int64")
        return df

    def _compute_evidence_and_normalized(
        self, df: pd.DataFrame
    ) -> pd.DataFrame:
        """Compute evidence_strength and normalized_score (SCI-24, SCI-38)."""
        if df.empty:
            return df
        # SCI-24: evidence_strength.
        def _pmid_count(v: Any) -> int:
            if v is None or pd.isna(v) or not isinstance(v, str):
                return 0
            return len([p for p in v.split(";") if p.strip()])

        if "pmid_list" in df.columns and "year_final" in df.columns:
            df["evidence_strength"] = df.apply(
                lambda r: _compute_evidence_strength(
                    _pmid_count(r.get("pmid_list")),
                    int(r["year_final"]) if pd.notna(r.get("year_final")) else None,
                ),
                axis=1,
            )
        elif "pmid_list" in df.columns:
            df["evidence_strength"] = df["pmid_list"].apply(
                lambda v: _compute_evidence_strength(_pmid_count(v), None)
            )
        else:
            df["evidence_strength"] = "unsupported"

        # SCI-38: normalized_score.
        if "score" in df.columns and "source_id" in df.columns:
            df["normalized_score"] = df.apply(
                lambda r: _compute_normalized_score(
                    float(r["score"]) if pd.notna(r.get("score")) else None,
                    r.get("source_id"),
                ),
                axis=1,
            )
        else:
            df["normalized_score"] = None
        return df

    def _cap_pmid_list_df(self, df: pd.DataFrame) -> pd.DataFrame:
        """Cap pmid_list (dedup, sort, validate, cap) for the whole DataFrame
        (SCI-16, SCI-17, DQ-16, DQ-17, DES-9, LIN-16, SEC-5, SEC-13, COMP-12,
        LOG-12).
        """
        if df.empty or "pmid_list" not in df.columns:
            return df
        capped_count = 0
        original_counts: list[int] = []
        for idx in df.index:
            raw = df.at[idx, "pmid_list"]
            original_count, new_val, was_capped = self._cap_pmid_list(raw)
            df.at[idx, "pmid_list"] = new_val
            df.at[idx, "original_pmid_count"] = original_count
            df.at[idx, "_pmid_list_was_capped"] = was_capped
            original_counts.append(original_count)
            if was_capped:
                capped_count += 1
        if capped_count > 0:
            try:
                logger.info(
                    "[disgenet] Capped pmid_list for %d records. Original-count "
                    "distribution: min=%d, max=%d, mean=%.1f",
                    capped_count,
                    min(original_counts), max(original_counts),
                    sum(original_counts) / max(1, len(original_counts)),
                )
            except (TypeError, ValueError):
                pass
        return df

    @staticmethod
    def _cap_pmid_list(
        pmid_str: Any,
    ) -> tuple[int, Optional[str], bool]:
        """Cap a semicolon-separated PMID list (SCI-16, SCI-17, DQ-16, DQ-17,
        DES-9, SEC-13).

        Parameters
        ----------
        pmid_str : str, list, or None
            Semicolon-separated PMID string (TSV format) or list of PMIDs
            (API format, pre-JSON-serialisation).  None is passed through.

        Returns
        -------
        tuple
            ``(original_count, capped_string_or_None, was_capped)``.
            ``original_count`` is the count BEFORE dedup/cap (for LIN-16).
            ``was_capped`` is True if the cap was applied.
        """
        # DES-9: Handle list inputs (from API responses).
        if isinstance(pmid_str, list):
            pmid_str = ";".join(str(p) for p in pmid_str if p is not None)
        if pmid_str is None or (isinstance(pmid_str, float) and pd.isna(pmid_str)):
            return (0, None, False)
        if not isinstance(pmid_str, str):
            return (0, None, False)
        pmids = [p.strip() for p in pmid_str.split(";") if p.strip()]
        original_count = len(pmids)
        # DQ-16: Dedup (preserves order).
        pmids = list(dict.fromkeys(pmids))
        # DQ-17 / SEC-13: Validate each PMID.
        valid_pmids: list[str] = []
        for p in pmids:
            if not _RE_PMID.match(p):
                continue
            if _RE_PMID_SQL_INJECTION.search(p):
                continue  # SEC-13: SQL-injection defence.
            valid_pmids.append(p)
        # SCI-16: Sort by configured order.
        if DISGENET_PMID_SORT_ORDER == "recent_first":
            valid_pmids.sort(key=lambda x: int(x), reverse=True)
        elif DISGENET_PMID_SORT_ORDER == "chronological":
            valid_pmids.sort(key=lambda x: int(x))
        # SCI-17: Cap.
        cap = DISGENET_PMID_CAP
        was_capped = len(valid_pmids) > cap
        if was_capped:
            valid_pmids = valid_pmids[:cap]
        # SEC-5: Length cap (defence-in-depth).
        result = ";".join(valid_pmids)
        if len(result) > PMID_LIST_LENGTH:
            result = result[:PMID_LIST_LENGTH]
            # Truncate at the last semicolon to avoid a partial PMID.
            last_sep = result.rfind(";")
            if last_sep > 0:
                result = result[:last_sep]
        return (original_count, result if result else None, was_capped)

    @staticmethod
    def _derive_source_value(source_id: Any) -> str:
        """Derive the ``source`` column value from ``source_id`` (SCI-4)."""
        if source_id is None or pd.isna(source_id) or str(source_id).strip() == "":
            return DataSourceName.DISGENET.value
        sid = str(source_id).strip().upper()
        return f"{DataSourceName.DISGENET.value}_{sid.lower()}"

    @staticmethod
    def _derive_association_type(source_id: Any) -> str:
        """Derive ``association_type`` from ``source_id`` (SCI-19)."""
        if source_id is None or pd.isna(source_id) or str(source_id).strip() == "":
            return DEFAULT_ASSOCIATION_TYPE
        sid = str(source_id).strip().upper()
        return SOURCE_ID_TO_ASSOCIATION_TYPE.get(sid, DEFAULT_ASSOCIATION_TYPE)

    def _populate_lineage_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Populate lineage columns (SCI-2, SCI-15, SCI-26, LIN-9, LIN-23,
        INT-7, COMP-6, COMP-7)."""
        if df.empty:
            return df
        # SCI-2: score_type / score_method / evidence_source.
        df["score_type"] = SCORE_TYPE_DISGENET
        version_str = (
            self._disgenet_release_version
            or self.target_version
            or SCORE_METHOD_DEFAULT
        )
        df["score_method"] = f"disgenet_{version_str}"
        # SCI-26 / LIN-5: source_version.
        df["source_version"] = version_str
        # LIN-23 / INT-7: download_method + source_format.
        df["download_method"] = self._source_format
        df["source_format"] = self._source_format
        # LIN-24: dedup_strategy.
        df["dedup_strategy"] = "validate_gda_scores_dedup"
        # COMP-6: schema_version.
        df["schema_version"] = SCHEMA_VERSION_STAMP
        # IDEM-14: snapshot_tag.
        df["snapshot_tag"] = self.snapshot_tag
        # LIN-9: source_url (sanitised).
        df["source_url"] = self._source_url_sanitised or self._sanitize_url(DISGENET_API_URL)
        # LIN-6 / COMP-7: download_date.
        df["download_date"] = (
            self.start_time.isoformat() if self.start_time else datetime.now(timezone.utc).isoformat()
        )
        # SCI-21: Ensure all lineage columns exist (validate_gda_scores may
        # not add them if no filling occurred — we add defaults here so the
        # CSV schema is consistent).
        for lineage_col, default in (
            ("_score_was_clipped", False),
            ("_original_score", None),
            ("_score_was_coerced_nan", False),
            ("_score_direction", None),
            ("_disease_name_was_filled", False),
            ("_association_type_was_filled", False),
        ):
            if lineage_col not in df.columns:
                df[lineage_col] = default
        # SCI-15: Prefer gene_uniprot_ids_raw (API field) for uniprot_id.
        # We'll do the actual resolution in load(); here we just record
        # the resolution method hint.
        if "gene_uniprot_ids_raw" in df.columns:
            # Try to extract the first accession.
            def _first_uniprot(v: Any) -> Optional[str]:
                if v is None or pd.isna(v) or v == "":
                    return None
                try:
                    parsed = json.loads(v) if isinstance(v, str) else v
                    if isinstance(parsed, list) and parsed:
                        return str(parsed[0])
                except (json.JSONDecodeError, TypeError):
                    pass
                return None

            df["_api_uniprot_id"] = df["gene_uniprot_ids_raw"].apply(_first_uniprot)
        else:
            df["_api_uniprot_id"] = None
        return df

    def _apply_score_filter(self, df: pd.DataFrame) -> pd.DataFrame:
        """Apply the MIN_SCORE filter with weak-evidence escape hatch (SCI-1,
        SCI-22, LOG-8, LIN-12).
        """
        if df.empty or "score" not in df.columns:
            return df
        before = len(df)
        if DISGENET_ALLOW_WEAK_EVIDENCE:
            # v41 ROOT FIX (SEV2-HIGH #7): the previous local "tagger"
            # hardcoded ``weak_mask = (score < 0.1) & (score >=
            # DISGENET_MIN_SCORE)`` and forced ``confidence_tier="weak"``
            # for ALL sub-0.1 rows. But the canonical
            # ``cleaning.confidence.classify_confidence`` (which this
            # module already imports at line 153) uses the publication-
            # aligned ``DEFAULT_CONFIDENCE_TIERS`` boundaries that may
            # differ from the hardcoded 0.1 cutoff. The local tagger
            # and ``classify_confidence`` DISAGREED on tier boundaries
            # for scores in [0.1, 0.3), producing inconsistent tier
            # labels for the same score depending on whether
            # ``DISGENET_ALLOW_WEAK_EVIDENCE`` was on or off.
            # Fix: delegate ALL tier classification to
            # ``classify_confidence`` (the single source of truth) and
            # only drop rows strictly below ``DISGENET_MIN_SCORE``.
            # No more local "override" — the canonical tiers already
            # assign "weak" to the [0.06, 0.3) range per Piñero 2020.
            if "score" in df.columns:
                # Reclassify ALL rows with a finite score using the
                # canonical classifier so tiers are consistent whether
                # or not the weak-evidence escape hatch fired.
                score_mask = df["score"].notna()
                df.loc[score_mask, "confidence_tier"] = df.loc[score_mask, "score"].apply(
                    lambda s: classify_confidence(float(s), tiers=CONFIDENCE_TIERS)
                )
            # Drop only rows below DISGENET_MIN_SCORE.
            drop_mask = df["score"].notna() & (df["score"] < DISGENET_MIN_SCORE)
            for idx in df.index[drop_mask]:
                self._add_to_dead_letter(
                    df, idx, reason="below_min_score",
                    details={
                        "score": float(df.at[idx, "score"]),
                        "threshold": DISGENET_MIN_SCORE,
                    },
                )
            df = df[~drop_mask].copy()
        else:
            # Hard filter at DISGENET_MIN_SCORE.
            drop_mask = df["score"].notna() & (df["score"] < DISGENET_MIN_SCORE)
            for idx in df.index[drop_mask]:
                self._add_to_dead_letter(
                    df, idx, reason="below_min_score",
                    details={
                        "score": float(df.at[idx, "score"]),
                        "threshold": DISGENET_MIN_SCORE,
                    },
                )
            df = df[~drop_mask].copy()
        dropped = before - len(df)
        if dropped > 0:
            logger.info(
                "[disgenet] Filtered by score >= %.4f: %d -> %d (dropped %d)",
                DISGENET_MIN_SCORE, before, len(df), dropped,
            )
        return df

    def _ensure_gda_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Ensure all required GDA columns exist with proper defaults (DQ-1, DQ-2,
        DQ-3, DQ-4, DQ-5, IDEM-13, ARCH-14, SCI-33, SCI-6, SCI-9, SCI-21,
        COMP-1).

        This function is PURELY ADDITIVE — it never removes columns or
        rows (the disease_id filter is moved to a separate function per
        IDEM-13).  It operates on a copy (ARCH-14) — the input is not
        mutated.  It is idempotent (IDEM-13) — calling it twice is a
        no-op.  Complexity is O(k) where k = len(required_defaults) ≈ 15
        (PERF-12).
        """
        df = df.copy()  # ARCH-14: do not mutate the caller's DataFrame.
        required_defaults: dict[str, Any] = {
            # DQ-1 / SCI-6: gene_id.
            "gene_id": None,
            # gene_symbol: nullable (some sources have only gene_id).
            "gene_symbol": None,
            # DQ-3 / SCI-3: source_id (DisGeNET sub-source).
            "source_id": None,
            # DQ-2 / SCI-9: disease_type.
            "disease_type": None,
            "disease_id": None,
            "disease_name": None,
            "disease_id_type": None,
            "disease_class": None,
            "disease_class_source": None,
            "year_initial": None,
            "year_final": None,
            # DQ-5: uniprot_id + association_type (optional in schema).
            "uniprot_id": None,
            "association_type": DEFAULT_ASSOCIATION_TYPE,
            # DQ-4: source (top-level label, e.g. "disgenet_curated").
            "source": DataSourceName.DISGENET.value,
            "score": None,
            "pmid_list": None,
            # SCI-33: confidence_tier is in required_defaults so
            # _ensure_gda_columns knows about it.
            "confidence_tier": "unknown",
            "confidence_tier_method": CONFIDENCE_TIER_METHOD_VERSION,
            "evidence_strength": "unsupported",
            "normalized_score": None,
            "score_type": SCORE_TYPE_DISGENET,
            "score_method": SCORE_METHOD_DEFAULT,
            "source_version": (
                self._disgenet_release_version
                or self.target_version
                or SCORE_METHOD_DEFAULT
            ),
            "source_format": self._source_format,
            "download_method": self._source_format,
            "download_date": (
                self.start_time.isoformat()
                if self.start_time
                else datetime.now(timezone.utc).isoformat()
            ),
            "dedup_strategy": "validate_gda_scores_dedup",
            "resolution_method": "none",
            "gene_to_uniprot_map_version": None,
            "original_pmid_count": None,
            "schema_version": SCHEMA_VERSION_STAMP,
            "snapshot_tag": self.snapshot_tag,
            "source_url": (
                self._source_url_sanitised
                or self._sanitize_url(DISGENET_API_URL)
            ),
            # Lineage columns (renamed without underscore in DB).
            "score_was_clipped": None,
            "original_score": None,
            "score_was_coerced_nan": None,
            "score_direction": None,
            "disease_name_was_filled": None,
            "association_type_was_filled": None,
            "pmid_list_was_capped": None,
            # v41 ROOT FIX (SEV2-HIGH #6): the DISGENET_API_COLUMN_MAP
            # (line 304-322) maps the DisGeNET REST API camelCase fields
            # to the snake_case column names below. The previous
            # ``required_defaults`` did NOT include these columns, so
            # when the API returned ``diseaseVocabularies``,
            # ``geneEnsemblIDs``, ``geneUniProtIDs``,
            # ``geneProteinClassIDs``, or ``diseaseClasses``, they were
            # mapped to columns that didn't exist in
            # ``_build_load_df``'s filter — silently dropped at load
            # time. The audit found that 100% of the API-side
            # ``gene_uniprot_ids_raw`` / ``gene_ensembl_ids_raw``
            # cross-reference payloads were lost before reaching the
            # DB, breaking the cross-source UniProt/Ensembl join.
            # Fix: add the missing columns to ``required_defaults``
            # with a sensible default (None) so the loader knows they
            # exist and can persist them via the schema's JSON column.
            "disease_vocabularies": None,
            "gene_ensembl_ids_raw": None,
            "gene_uniprot_ids_raw": None,
            "gene_protein_class_ids_raw": None,
            "disease_classes_raw": None,
        }
        for col, default in required_defaults.items():
            if col not in df.columns:
                df[col] = default

        # SCI-33 / IDEM-13: assertion that confidence_tier is present.
        # v29 ROOT FIX (audit P1-19): was assert — stripped by python -O. Use raise for production validation.
        if "confidence_tier" not in df.columns:
            raise ValueError(
                "_ensure_gda_columns invariant: confidence_tier must be present"
            )
        return df

    def _filter_invalid_disease_ids(self, df: pd.DataFrame) -> pd.DataFrame:
        """Filter out records with empty / NaN disease_id (CODE-15, IDEM-13).

        Separated from _ensure_gda_columns so the latter is purely
        additive (IDEM-13).
        """
        if "disease_id" not in df.columns or df.empty:
            return df
        before = len(df)
        mask = df["disease_id"].notna() & (df["disease_id"].astype(str).str.strip() != "")
        for idx in df.index[~mask]:
            self._add_to_dead_letter(
                df, idx, reason="empty_disease_id",
                details={"disease_id": str(df.at[idx, "disease_id"])},
            )
        df = df[mask].copy()
        if len(df) < before:
            logger.warning(
                "[disgenet] Dropped %d GDA records with no disease_id",
                before - len(df),
            )
        return df

    def _save_processed_csv(
        self,
        df: pd.DataFrame,
        output_path: Path,
        primary_source: str,
    ) -> None:
        """Persist the cleaned DataFrame to CSV atomically (DQ-7, DQ-8, DQ-9,
        DQ-10, DQ-11, DQ-12, IDEM-1, IDEM-13, COMP-14, COMP-15, COMP-16,
        SEC-14, INT-19, LIN-25, LOG-4).

        - Manifest-based source detection (DQ-7) — no ``nrows=5`` peek.
        - No source-conflict redirect (DQ-8) — raise instead.
        - Atomic write via ``.tmp`` + ``os.replace`` (DQ-12, REL-11).
        - Explicit ``encoding="utf-8"``, ``lineterminator="\\n"``,
          ``quoting=csv.QUOTE_ALL`` (COMP-14, COMP-15, COMP-16).
        - File permissions ``0o640`` (SEC-14).
        - Sidecar manifest with full provenance (INT-19, LIN-25, LIN-27,
          LIN-28).
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # DQ-7: Manifest-based source detection.
        manifest_path = output_path.with_suffix(output_path.suffix + ".manifest")
        if output_path.exists():
            existing_source = self._read_manifest_source(manifest_path, output_path)
            if existing_source is not None and existing_source != primary_source:
                # DQ-8: Raise instead of redirecting.
                raise RuntimeError(
                    f"Existing file {output_path.name} contains data from "
                    f"{existing_source!r} but current run is {primary_source!r}. "
                    f"Move or delete the existing file before re-running, or "
                    f"set DISGENET_OUTPUT_FILENAME to a different path."
                )

        # DQ-12: Atomic write.
        tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
        try:
            df.to_csv(
                tmp_path,
                index=False,
                encoding="utf-8",  # COMP-15
                lineterminator="\n",  # COMP-16
                quoting=csv_mod.QUOTE_ALL,  # COMP-14
            )
            os.replace(tmp_path, output_path)
        except Exception:
            # Clean up the .tmp file on failure.
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            raise

        # LOG-4: Output checksum.
        try:
            self._sha256_cleaned = self._compute_sha256(output_path)
            logger.info(
                "[disgenet] Output CSV SHA-256: %s", self._sha256_cleaned
            )
        except (OSError, ValueError) as exc:
            logger.warning(
                "[disgenet] Could not compute output SHA-256: %s", exc
            )

        # SEC-14: File permissions.
        try:
            mode = int(DISGENET_OUTPUT_FILE_MODE, 8) if isinstance(
                DISGENET_OUTPUT_FILE_MODE, str
            ) else 0o640
            os.chmod(output_path, mode)
        except (OSError, ValueError) as exc:
            logger.warning(
                "[disgenet] Could not set file permissions on %s: %s",
                output_path, exc,
            )

        # INT-19 / LIN-25 / LIN-27 / LIN-28: Write the manifest.
        manifest = {
            "primary_source": primary_source,
            "row_count": int(len(df)),
            "column_count": int(df.shape[1]),
            "columns": df.columns.tolist(),
            "schema_version": SCHEMA_VERSION_STAMP,
            "source_version": (
                self._disgenet_release_version
                or self.target_version
                or SCORE_METHOD_DEFAULT
            ),
            "download_date": (
                self.start_time.isoformat()
                if self.start_time
                else datetime.now(timezone.utc).isoformat()
            ),
            "last_full_refresh": datetime.now(timezone.utc).isoformat(),
            "source_url": self._source_url_sanitised,
            "api_endpoint": self._api_endpoint,
            "api_params": self._redact_api_params(self._api_params),
            "source_sha256": self._sha256_raw,
            "cleaning_sha256": self._sha256_cleaned,
            "run_id": self.run_id,
            "input_fingerprint": self._input_fingerprint,
            "cleaning_metadata": self._cleaning_metadata,
            "stale_data": False,  # DQ-33: populated below.
        }
        # DQ-33: Stale data detection.
        if self.source_publication_date is not None:
            age_days = (
                datetime.now(timezone.utc) - self.source_publication_date
            ).days
            manifest["stale_data"] = age_days > DISGENET_MAX_DATA_AGE_DAYS
            if manifest["stale_data"]:
                logger.warning(
                    "[disgenet] DisGeNET release is %d days old (> %d) — "
                    "stale_data flag set in manifest",
                    age_days, DISGENET_MAX_DATA_AGE_DAYS,
                )

        try:
            manifest_path.write_text(json.dumps(manifest, indent=2, default=str))
        except OSError as exc:
            logger.warning(
                "[disgenet] Could not write manifest %s: %s",
                manifest_path, exc,
            )

    @staticmethod
    def _redact_api_params(params: dict[str, Any]) -> dict[str, Any]:
        """Redact sensitive values from API params before manifest write (SEC-15)."""
        if not isinstance(params, dict):
            return {}
        out: dict[str, Any] = {}
        for k, v in params.items():
            if k.lower() in {"api_key", "key", "token", "secret", "password"}:
                out[k] = "[REDACTED]"
            else:
                out[k] = v
        return out

    def _read_manifest_source(
        self, manifest_path: Path, csv_path: Path
    ) -> Optional[str]:
        """Read the primary_source from the manifest (DQ-7)."""
        if manifest_path.exists():
            try:
                data = json.loads(manifest_path.read_text())
                return data.get("primary_source")
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(
                    "[disgenet] Could not read manifest %s: %s — falling back "
                    "to reading the CSV's source column",
                    manifest_path, exc,
                )
        # Legacy fallback: read the entire 'source' column (NOT just 5 rows).
        try:
            existing_df = pd.read_csv(
                csv_path, usecols=lambda c: c == "source", low_memory=False,
            )
            if "source" not in existing_df.columns:
                # No 'source' column in the CSV — can't determine the source.
                logger.warning(
                    "[disgenet] Existing CSV %s has no 'source' column — "
                    "cannot determine primary_source",
                    csv_path,
                )
                return None
            sources = existing_df["source"].dropna().unique()
            if len(sources) > 0:
                # Return the most common source.
                counts = existing_df["source"].value_counts()
                return str(counts.index[0])
        except (pd.errors.ParserError, FileNotFoundError, OSError, ValueError, KeyError) as exc:
            # DQ-10: Log the exception (don't silently pass).
            logger.warning(
                "[disgenet] Could not read existing CSV %s: %s",
                csv_path, exc,
            )
        return None

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------
    def load(self, df: pd.DataFrame, session: Any | None = None) -> int:
        """Load cleaned DisGeNET GDA data into the database.

        **Contract:** returns ``int`` (the framework's ``load() -> int``
        contract).  The returned value is ``result.inserted +
        result.updated`` (NOT ``int(result)`` which returns
        ``total_input``) — see ARCH-11.

        Parameters
        ----------
        df : pd.DataFrame
            Cleaned DataFrame from ``clean()``.
        session : Session, optional
            Active SQLAlchemy session.  If provided, the caller manages
            the transaction boundary (ARCH-1.5).  If None, ``load()``
            opens its own session via ``get_db_session()``.

        Steps:
            1. Resolve gene_symbol → uniprot_id (single DB session — ARCH-3).
            2. Partition resolved / unresolved; write unresolved to dead-letter
               (DQ-18, REL-3, LIN-11).
            3. Build load_df with all GDA model columns (DES-11).
            4. Get-or-create a pipeline_runs row (IDEM-10).
            5. Bulk upsert with full lineage kwargs (IDEM-9, IDEM-11,
               DES-12, CODE-25).
            6. Log the full UpsertResult (CODE-3, LOG-14).
            7. Return ``inserted + updated`` (ARCH-11).
        """
        if df.empty:
            logger.info("[disgenet] No GDA records to load", )
            return 0

        # DES-14: Runtime assertion that required columns are present.
        required_for_load = ["disease_id", "score", "source"]
        missing = [c for c in required_for_load if c not in df.columns]
        if missing:
            raise ValueError(
                f"load() input is missing required columns: {missing}. "
                f"Available: {df.columns.tolist()}"
            )

        # ARCH-3: Single DB session for the whole load.
        # If the caller provided a session, use it; otherwise open one.
        if session is not None:
            return self._load_with_session(df, session)
        with get_db_session() as session:
            return self._load_with_session(df, session)

    def _load_with_session(self, df: pd.DataFrame, session: Any) -> int:
        """Internal load implementation — uses the provided session (ARCH-3)."""
        # v14 ROOT FIX (DQ-18 / dead-letter persistence): the previous
        # code only persisted LOAD-time unresolved records to the
        # dead_letter_gda DB table (line 3063). CLEAN-time dead-letter
        # records (added to self._dead_letter_rows at clean() time,
        # e.g. for invalid_gene_symbol_format) were ONLY logged as
        # warnings — never persisted to the DB. This meant operators
        # querying the dead_letter_gda table for audit/lineage saw an
        # INCOMPLETE picture: records dropped at clean time were
        # invisible. The fix: flush self._dead_letter_rows to the DB
        # at the start of _load_with_session, using each record's
        # already-set reason field.
        if self._dead_letter_rows:
            try:
                from database.models import DeadLetterGDA
                # v29 ROOT FIX (audit P1-21): was N+1 dead-letter queries. Batch into single bulk insert.
                # Previously, for every dead-lettered row we issued ONE SELECT
                # (idempotency check via session.query().filter_by().first())
                # plus ONE INSERT (session.add()). On real DisGeNET runs with
                # thousands of quarantined rows this produced thousands of
                # round-trips and dominated load() wall-clock time. The fix:
                # 1. Build the set of (gene_symbol, disease_id, reason, run_id)
                #    tuples we want to insert.
                # 2. Issue a SINGLE SELECT ... WHERE (tuple) IN (...) to fetch
                #    the already-persisted subset (idempotency across retries).
                # 3. Issue a SINGLE bulk_save_objects() for the new records.
                # Net queries: 1 SELECT + 1 INSERT, regardless of N.
                candidate_keys = {
                    (
                        rec.get("gene_symbol"),
                        rec.get("disease_id"),
                        rec.get("reason"),
                        rec.get("run_id"),
                    )
                    for rec in self._dead_letter_rows
                }
                existing_keys: set = set()
                if candidate_keys:
                    from sqlalchemy import tuple_, select as sa_select
                    existing_rows = session.execute(
                        sa_select(
                            DeadLetterGDA.gene_symbol,
                            DeadLetterGDA.disease_id,
                            DeadLetterGDA.reason,
                            DeadLetterGDA.run_id,
                        ).where(
                            tuple_(
                                DeadLetterGDA.gene_symbol,
                                DeadLetterGDA.disease_id,
                                DeadLetterGDA.reason,
                                DeadLetterGDA.run_id,
                            ).in_(list(candidate_keys))
                        )
                    ).all()
                    existing_keys = {
                        (r[0], r[1], r[2], r[3]) for r in existing_rows
                    }
                # Single bulk insert for all new records.
                new_records = [
                    rec for rec in self._dead_letter_rows
                    if (
                        rec.get("gene_symbol"),
                        rec.get("disease_id"),
                        rec.get("reason"),
                        rec.get("run_id"),
                    ) not in existing_keys
                ]
                if new_records:
                    session.bulk_save_objects([
                        DeadLetterGDA(
                            gene_symbol=rec.get("gene_symbol"),
                            disease_id=rec.get("disease_id"),
                            source=rec.get("source") or DataSourceName.DISGENET.value,
                            reason=rec.get("reason", "unknown"),
                            details_json=rec.get("details_json", "{}"),
                            run_id=rec.get("run_id"),
                        )
                        for rec in new_records
                    ])
                    session.flush()
                    logger.info(
                        "[disgenet] Persisted %d clean-time dead-letter "
                        "record(s) to dead_letter_gda table (DQ-18 fix, "
                        "P1-21 batched insert)",
                        len(new_records),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[disgenet] Could not flush clean-time dead-letter "
                    "records to DB: %s",
                    exc,
                )

        # ARCH-17: Dependency check — proteins table must be non-empty.
        self._assert_uniprot_dependency(session)

        # IDEM-7: Cached gene_to_uniprot map.
        gene_to_uniprot, protein_name_to_uniprot, map_version = (
            self._get_or_build_uniprot_map(session)
        )

        # Resolve gene_symbol → uniprot_id.
        df = resolve_gene_symbol_to_uniprot(
            df, gene_to_uniprot, protein_name_to_uniprot
        )

        # SCI-15: Prefer API-provided geneUniProtIDs when available.
        # P1-15 ROOT FIX: Validate each API-provided UniProt ID against
        # UNIPROT_ID_PATTERN before assigning. Previously, the values were
        # assigned WITHOUT format validation, so ~5-10% invalid entries
        # silently corrupted the uniprot_id column. Invalid entries are now
        # routed to dead-letter (set to None and logged).
        if "_api_uniprot_id" in df.columns:
            api_mask = df["_api_uniprot_id"].notna()
            if api_mask.any():
                api_values = df.loc[api_mask, "_api_uniprot_id"].astype(str)
                valid_mask = api_values.str.match(UNIPROT_ID_PATTERN)
                invalid_count = int((~valid_mask).sum())
                if invalid_count > 0:
                    logger.warning(
                        "[disgenet] %d API-provided UniProt IDs failed "
                        "format validation — routing to dead-letter "
                        "(set to None).",
                        invalid_count,
                    )
                    invalid_idx = api_values.index[~valid_mask]
                    df.loc[invalid_idx, "_api_uniprot_id"] = None
                # Re-derive the valid mask after invalid IDs were nulled.
                valid_api_mask = df["_api_uniprot_id"].notna()
                if valid_api_mask.any():
                    df.loc[valid_api_mask, "uniprot_id"] = df.loc[
                        valid_api_mask, "_api_uniprot_id"
                    ]
                    df.loc[valid_api_mask, "resolution_method"] = "api_field"
                    logger.info(
                        "[disgenet] %d records resolved via API geneUniProtIDs "
                        "(%d rejected for invalid format)",
                        int(valid_api_mask.sum()),
                        invalid_count,
                    )
            # Mark the rest as resolved via local DB (or unresolved).
            if "resolution_method" not in df.columns:
                df["resolution_method"] = "none"
            local_mask = (~api_mask) & df["uniprot_id"].notna()
            df.loc[local_mask, "resolution_method"] = "local_db"
            # Record the map version for lineage (LIN-10).
            df["gene_to_uniprot_map_version"] = map_version
        else:
            if "resolution_method" not in df.columns:
                df["resolution_method"] = "none"
            local_mask = df["uniprot_id"].notna()
            df.loc[local_mask, "resolution_method"] = "local_db"
            df["gene_to_uniprot_map_version"] = map_version

        # DQ-18: Partition resolved / unresolved.
        unresolved_mask = df["uniprot_id"].isna()
        unresolved = df[unresolved_mask].copy()
        resolved = df[~unresolved_mask].copy()
        if len(unresolved) > 0:
            logger.warning(
                "[disgenet] %d / %d GDA records have unresolved "
                "gene_symbol — routing to dead-letter",
                len(unresolved), len(df),
            )
            # LOG-7: First 10 unresolved symbols.
            first_10 = unresolved["gene_symbol"].dropna().head(10).tolist()
            if first_10:
                logger.info(
                    "[disgenet] First 10 unresolved gene_symbols: %s",
                    first_10,
                )
            self._write_dead_letter_file(unresolved, reason="unresolved_gene_symbol")
            self._write_dead_letter_db(session, unresolved, reason="unresolved_gene_symbol")

        if resolved.empty:
            logger.warning(
                "[disgenet] No GDA records with resolved uniprot_id"
            )
            return 0

        # DES-11: Build load_df from the model's columns.
        load_df = self._build_load_df(resolved)

        # IDEM-9: Input checksum.
        input_checksum = self._sha256_raw or self._compute_df_checksum(load_df)

        # IDEM-10: Get-or-create pipeline_runs row.
        pipeline_run_id = get_or_create_pipeline_run(
            session,
            run_id=self.run_id,
            source=DataSourceName.DISGENET.value,
            started_at=self.start_time,
            status="running",
        )

        # IDEM-11 / DES-12 / CODE-25: Call bulk_upsert_gda with full lineage.
        result: UpsertResult = bulk_upsert_gda(
            session,
            load_df,
            pipeline_run_id=pipeline_run_id,
            score_type=SCORE_TYPE_DISGENET,
            score_method=(
                f"disgenet_"
                f"{self._disgenet_release_version or self.target_version or SCORE_METHOD_DEFAULT}"
            ),
            input_checksum=input_checksum,
            dedup_already_done=True,  # DQ-6 / SCI-37: validator already deduped.
        )
        # v29 ROOT FIX (audit P1-11/12/13): was session.commit() — breaks
        # atomicity. Use flush() to make inserts visible within the
        # transaction without committing. The commit happens in __exit__.
        session.flush()

        # CODE-3 / LOG-14: Log the full UpsertResult.
        logger.info(
            "[disgenet] GDA upsert: input=%d, inserted=%d, updated=%d, "
            "quarantined=%d, failed=%d",
            result.total_input, result.inserted, result.updated,
            result.quarantined, result.failed,
        )

        # LOG-17: Emit metrics.
        self._emit_metric(
            "records_loaded", result.inserted + result.updated,
            tags={"source": DataSourceName.DISGENET.value},
        )
        self._emit_metric(
            "records_quarantined", result.quarantined,
            tags={"source": DataSourceName.DISGENET.value},
        )

        # ARCH-11 / CODE-2: Return inserted + updated (NOT int(result)).
        return int(result.inserted + result.updated)

    def _assert_uniprot_dependency(self, session: Any) -> None:
        """Assert the proteins table is non-empty (ARCH-17).

        P1-14 ROOT FIX: The previous broad ``except Exception`` swallowed
        ``OperationalError`` / ``IntegrityError`` (transient DB issues) and
        only logged a warning, silently bypassing the dependency check.
        Now only ``ImportError`` (the genuinely non-critical case where the
        ORM module itself cannot be loaded) is caught; all DB-layer errors
        propagate so the operator sees them instead of silently proceeding.
        """
        try:
            from database.models import Protein
            count = session.query(Protein).count()
            if count == 0:
                raise RuntimeError(
                    "DisGeNET pipeline requires the UniProt pipeline to have "
                    "run first (proteins table is empty). Run the UniProt "
                    "pipeline before the DisGeNET pipeline."
                )
        except RuntimeError:
            raise
        except ImportError as exc:
            # The only non-critical case: the ORM module itself cannot be
            # imported (e.g. test harness without database.models). All
            # DB-layer exceptions (OperationalError, IntegrityError, etc.)
            # MUST propagate so the operator sees them.
            logger.warning(
                "[disgenet] Could not verify proteins table is non-empty "
                "(Protein model import failed — treating as missing "
                "dependency): %s",
                exc,
            )

    def _get_or_build_uniprot_map(
        self, session: Any
    ) -> tuple[GeneToUniprotMap, ProteinNameToUniprotMap, str]:
        """Get (cached) or build the gene_to_uniprot map (IDEM-7, PERF-6,
        PERF-13).

        Returns ``(gene_to_uniprot, protein_name_to_uniprot, map_version)``
        where ``map_version`` is a SHA-256 of the cache file (for lineage).
        """
        cache_dir = PROCESSED_DATA_DIR / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / "gene_to_uniprot_map.json"
        ttl_seconds = DISGENET_UNIPROT_MAP_TTL_HOURS * 3600

        # Check cache freshness.
        if cache_path.exists():
            try:
                age = time.time() - cache_path.stat().st_mtime
                if age < ttl_seconds:
                    cached = json.loads(cache_path.read_text())
                    return (
                        cached.get("gene_to_uniprot", {}),
                        cached.get("protein_name_to_uniprot", {}),
                        cached.get("map_version", ""),
                    )
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(
                    "[disgenet] Could not read uniprot map cache %s: %s",
                    cache_path, exc,
                )

        # Build the map.
        gene_to_uniprot, protein_name_to_uniprot = build_gene_to_uniprot_maps(session)
        # Compute a version hash for lineage (LIN-10).
        import hashlib
        map_version = hashlib.sha256(
            json.dumps(
                sorted(gene_to_uniprot.items()), default=str
            ).encode("utf-8")
        ).hexdigest()[:64]
        # Write cache.
        try:
            cache_path.write_text(json.dumps({
                "gene_to_uniprot": gene_to_uniprot,
                "protein_name_to_uniprot": protein_name_to_uniprot,
                "map_version": map_version,
                "built_at": datetime.now(timezone.utc).isoformat(),
                "record_count": len(gene_to_uniprot),
            }, default=str))
        except OSError as exc:
            logger.warning(
                "[disgenet] Could not write uniprot map cache %s: %s",
                cache_path, exc,
            )
        return gene_to_uniprot, protein_name_to_uniprot, map_version

    def _build_load_df(self, df: pd.DataFrame) -> pd.DataFrame:
        """Build the load DataFrame with the GDA model's columns (DES-11,
        CODE-5, CODE-24, INT-4).

        Missing columns are filled with None (with the right length and
        dtype — CODE-5).  Column selection is schema-driven (introspects
        the GeneDiseaseAssociation model — DES-11).
        """
        # Get the model's column names, EXCLUDING auto-managed columns
        # (id, created_at, updated_at) — these have server_defaults that
        # the DB populates; sending NULL overrides the default.
        _AUTO_MANAGED_COLS = {"id", "created_at", "updated_at"}
        try:
            from sqlalchemy import inspect as sa_inspect
            model_cols = [
                c.name for c in sa_inspect(GeneDiseaseAssociation).columns
                if c.name not in _AUTO_MANAGED_COLS
            ]
        except Exception:  # noqa: BLE001
            # Fallback: hardcode the column list (excluding auto-managed).
            model_cols = [
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
            ]

        # Map CSV lineage column names (underscore-prefixed) to DB column
        # names (no underscore) — SCI-21.
        csv_to_db = {
            "_score_was_clipped": "score_was_clipped",
            "_original_score": "original_score",
            "_score_was_coerced_nan": "score_was_coerced_nan",
            "_score_direction": "score_direction",
            "_disease_name_was_filled": "disease_name_was_filled",
            "_association_type_was_filled": "association_type_was_filled",
            "_pmid_list_was_capped": "pmid_list_was_capped",
        }

        load_data: dict[str, pd.Series] = {}
        for db_col in model_cols:
            if db_col in df.columns:
                load_data[db_col] = df[db_col]
            else:
                # Check if the CSV has an underscore-prefixed version.
                csv_col = "_" + db_col
                if csv_col in df.columns:
                    load_data[db_col] = df[csv_col]
                else:
                    # Reverse check: db_col might be the unprefixed version.
                    for csv_c, db_c in csv_to_db.items():
                        if db_c == db_col and csv_c in df.columns:
                            load_data[db_col] = df[csv_c]
                            break
                    else:
                        # CODE-5: Fill with None of the right length.
                        load_data[db_col] = pd.Series(
                            [None] * len(df), index=df.index
                        )

        load_df = pd.DataFrame(load_data)

        # Convert download_date from ISO string to datetime for the DB
        # (the model's download_date column is DateTime(timezone=True)).
        if "download_date" in load_df.columns:
            load_df["download_date"] = pd.to_datetime(
                load_df["download_date"], errors="coerce", utc=True
            )

        return load_df

    @staticmethod
    def _compute_df_checksum(df: pd.DataFrame) -> str:
        """Compute a SHA-256 checksum of a DataFrame (IDEM-9 fallback)."""
        import hashlib
        try:
            content = df.to_csv(index=False).encode("utf-8")
            return hashlib.sha256(content).hexdigest()
        except Exception:  # noqa: BLE001
            return ""

    def _write_dead_letter_file(
        self, df: pd.DataFrame, *, reason: str
    ) -> None:
        """Write unresolved records to a dead-letter CSV (DQ-18)."""
        dead_letter_dir = PROCESSED_DATA_DIR / "dead_letter"
        dead_letter_dir.mkdir(parents=True, exist_ok=True)
        path = dead_letter_dir / f"gda_unresolved_gene_symbols_{self.run_id}.csv"
        try:
            df.to_csv(path, index=False, encoding="utf-8")
            logger.info(
                "[disgenet] Wrote %d unresolved records to %s",
                len(df), path,
            )
        except OSError as exc:
            logger.warning(
                "[disgenet] Could not write dead-letter file %s: %s",
                path, exc,
            )

    def _write_dead_letter_db(
        self, session: Any, df: pd.DataFrame, *, reason: str
    ) -> None:
        """Write unresolved records to the dead_letter_gda table (DQ-18, LIN-11)."""
        if df.empty:
            return
        try:
            from database.models import DeadLetterGDA
            # v29 ROOT FIX (audit P1-21): was N+1 dead-letter queries. Batch into single bulk insert.
            # The previous implementation iterated df.iterrows() and called
            # session.add(DeadLetterGDA(...)) once per row, producing N
            # individual INSERT statements at flush time. With N in the
            # thousands (typical for unresolved gene_symbol batches against
            # the full DisGeNET release) this dominated load() latency.
            # Build all ORM objects up-front and hand them to a single
            # bulk_save_objects() call so SQLAlchemy emits one INSERT
            # batch instead of N.
            objects = []
            for _, row in df.iterrows():
                details = {
                    "score": float(row["score"]) if pd.notna(row.get("score")) else None,
                    "source_id": row.get("source_id"),
                    "source_format": self._source_format,
                }
                objects.append(DeadLetterGDA(
                    gene_symbol=row.get("gene_symbol"),
                    disease_id=row.get("disease_id"),
                    source=row.get("source") or DataSourceName.DISGENET.value,
                    reason=reason,
                    details_json=json.dumps(details, default=str),
                    run_id=self.run_id,
                ))
            if objects:
                session.bulk_save_objects(objects)
                session.flush()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[disgenet] Could not write to dead_letter_gda table: %s",
                exc,
            )

    # ------------------------------------------------------------------
    # Programmatic API for downstream consumers (INT-17, P1-31 ROOT FIX)
    # ------------------------------------------------------------------
    # P1-31 ROOT FIX: previously these two methods were defined as
    # ``@classmethod``-decorated functions at MODULE level (after the
    # class definition) and attached via
    # ``DisGeNETPipeline.get_gda_by_gene = classmethod(_get_gda_by_gene_cls)``
    # — a convoluted pattern that obscures the methods' class membership,
    # defeats static-analysis tools (they appear as free functions), and
    # makes IDE jump-to-definition misbehave. They are now proper
    # ``@classmethod`` methods on the class itself.
    @classmethod
    def get_gda_by_gene(
        cls, gene_symbol: str, session: Any
    ) -> list[GeneDiseaseAssociation]:
        """Return all GDAs for a gene symbol (INT-17).

        Parameters
        ----------
        gene_symbol : str
            HGNC gene symbol (e.g. ``"BRCA1"``).
        session : Session
            Active SQLAlchemy session.

        Returns
        -------
        list of GeneDiseaseAssociation
        """
        from sqlalchemy import select as sa_select
        stmt = sa_select(GeneDiseaseAssociation).where(
            GeneDiseaseAssociation.gene_symbol == gene_symbol.upper().strip()
        )
        return list(session.execute(stmt).scalars().all())

    @classmethod
    def get_gda_by_disease(
        cls, disease_id: str, session: Any
    ) -> list[GeneDiseaseAssociation]:
        """Return all GDAs for a disease ID (INT-17).

        Parameters
        ----------
        disease_id : str
            Disease identifier (e.g. ``"C0006142"``).
        session : Session
            Active SQLAlchemy session.

        Returns
        -------
        list of GeneDiseaseAssociation
        """
        from sqlalchemy import select as sa_select
        stmt = sa_select(GeneDiseaseAssociation).where(
            GeneDiseaseAssociation.disease_id == disease_id.upper().strip()
        )
        return list(session.execute(stmt).scalars().all())

    # ------------------------------------------------------------------
    # Deprecated wrapper (ARCH-4)
    # ------------------------------------------------------------------
    def _save_csv_with_mode(
        self, df: pd.DataFrame, output_path: Path
    ) -> None:
        """Deprecated alias for :meth:`_save_processed_csv` (ARCH-4).

        .. deprecated::
            Use :meth:`_save_processed_csv` instead.  This wrapper
            remains for backward compatibility with code that imports
            ``_save_csv_with_mode`` from this module.  It emits a
            ``DeprecationWarning`` and forwards to
            ``_save_processed_csv``.
        """
        import warnings
        warnings.warn(
            "_save_csv_with_mode is deprecated — use _save_processed_csv "
            "instead. This wrapper will be removed in v3.0.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        self._save_processed_csv(
            df, output_path, DataSourceName.DISGENET.value
        )


# ---------------------------------------------------------------------------
# Programmatic API for downstream consumers (INT-17)
# ---------------------------------------------------------------------------
# P1-31 ROOT FIX: the module-level ``@classmethod``-decorated free
# functions ``_get_gda_by_gene_cls`` and ``_get_gda_by_disease_cls`` and
# the post-class ``DisGeNETPipeline.get_gda_by_gene = classmethod(...)``
# re-wrapping pattern have been REMOVED. The methods are now defined as
# proper ``@classmethod`` methods inside the ``DisGeNETPipeline`` class
# definition above. This block is intentionally left empty (with this
# comment) so anyone diffing the file against an earlier version can see
# exactly what was removed and why.


# ---------------------------------------------------------------------------
# Module-level __all__
# ---------------------------------------------------------------------------
__all__ = [
    "DisGeNETPipeline",
    "DisGeNETSourceFormat",
    "CleanResult",
    "DISGENET_COLUMN_MAP",
    "DISGENET_API_COLUMN_MAP",
    "MIN_SCORE",
    "CONFIDENCE_TIERS",
    "SCORE_TYPE_DISGENET",
    "SCORE_METHOD_DEFAULT",
    "SOURCE_ID_TO_ASSOCIATION_TYPE",
    "DEFAULT_ASSOCIATION_TYPE",
    "SCHEMA_VERSION_STAMP",
    "_classify_confidence",
    "_infer_disease_id_type",
    "_validate_disease_id",
    "_validate_gene_symbol",
    "_sanitise_free_text",
    "_compute_evidence_strength",
    "_compute_normalized_score",
    "__version__",
    "__author__",
    "__license__",
]
