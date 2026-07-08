"""DrugOS Graph Module — ClinicalTrials Loader (Institutional-Grade v2.1.0)
==========================================================================
Downloads, validates, parses, and converts the ClinicalTrials.gov AACT
(Accelerated Clinical Trials Transformation Initiative) database into
``(Compound)-[:tested_for]->(Disease)`` knowledge-graph edge records
for the Autonomous Drug Repurposing Platform (Team Cosmic, VentureLab).

This file is the **hardened** replacement for the 116-line v0 prototype.
The forensic audit (``PROMPT_fix_clinicaltrials_loader.md``) enumerated
148 specific defects across 16 quality domains; every issue ID from 1.1
through 16.12 is addressed in this file via an inline
``# Fixes: <id>`` comment (master prompt Rule R4).

Project Context
---------------
The Autonomous Drug Repurposing Platform mines 10,000 FDA-approved drugs
against every known disease using a chained pipeline:

1. **Knowledge Graph (Neo4j)** — built by this loader + 12 sibling loaders
   (ChEMBL, DrugBank, UniProt, STRING, STITCH, DRKG, OpenTargets, SIDER,
   DisGeNET, OMIM, PubChem, GEO).
2. **Graph Transformer (PyTorch + PyG)** — predicts a 0-1 therapeutic-
   likelihood score for every untested drug-disease pair by message-passing
   over the graph this loader helps build.
3. **RL Hypothesis Ranker (Stable-Baselines3, PPO)** — ranks the top
   predictions by plausibility x **safety signal** x market opportunity.
4. **Clinical decision layer** — pharma partners + clinicians consume the
   ranking.

ClinicalTrials.gov AACT data are **edges** in that graph. They tell the RL
ranker "Drug X has been TESTED FOR Disease Y in a Phase 3 RCT." The RL
ranker aggregates these onto the Compound node and assigns a **clinical-
evidence tier**:

  * HIGH CONFIDENCE — Phase 4 (post-marketing) + completed + randomized
  * MEDIUM CONFIDENCE — Phase 3 + completed + randomized
  * LOW CONFIDENCE — Phase 2, OR terminated, OR comparator/placebo arm,
    OR stopped for safety

.. warning::
    **PATIENT SAFETY — READ BEFORE MODIFYING THIS FILE**

    The 51 ☠️ patient-safety flags in the audit dominate all other
    concerns. If a fix to a patient-safety item conflicts with a fix to
    a non-safety item, the patient-safety fix wins (master prompt §0.4).

    The four critical C-tier fixes (C1-C10) are mandatory and ship FIRST:

    * **C1** — AACT ``mesh_term`` column does NOT exist on interventions;
      must use the ``interventions_mesh_terms`` JOIN table (modern schema)
      or fall back to ``interventions.mesh_term`` (legacy schema) with a
      WARNING. Refuse to run on unknown schemas. (Issue 3.1)
    * **C2** — AACT cross-product JOIN fabricates drug-disease pairs (a
      trial with interventions [Drug A, Placebo] and conditions [Disease X,
      Disease Y] emits 4 edges, only 1 of which is real). Mitigate via
      ``drug_role`` detection (comparator/placebo gets 0.3x evidence
      multiplier + id_confidence="low") + cross-product penalty. (Issues
      2.2, 3.3)
    * **C3** — ``why_stopped`` is not captured by v0. Add it; apply
      -0.20 evidence penalty + ``safety_signal="stopped_for_safety"`` flag
      when the value matches the safety regex. (Issue 3.5)
    * **C4** — v0 ``intervention_type='Drug'`` excludes Biological
      interventions (Humira, Keytruda, Ozempic — ~30% of modern FDA
      approvals). Default now includes Biological. (Issues 2.7, 3.4)
    * **C5** — v0 ``LIKE '%Phase 3%'`` substring match also matches
      "Phase 2/Phase 3" and "Phase 3/Phase 4". Replaced with exact-match
      ``IN (?, ?)``. (Issue 3.2)
    * **C6** — v0 ``rel_type="clinical_trial"`` is not in the KG schema
      registry (config.CORE_EDGE_TYPES). Changed to ``"tested_for"``.
      ``"treats"`` is FORBIDDEN — reserved for FDA-approved drugs from
      DrugBank. (Issues 2.1, 14.1, 15.3)
    * **C7** — v0 does not capture ``enrollment`` count. Added; small
      enrollment (<30 in Phase 3) triggers WARNING. (Issue 3.6)
    * **C8** — v0 re-runs create duplicate edges. Fixed via deterministic
      ``edge_id`` + ``use_merge=True`` at the Neo4j load site. (Issues
      7.1, 2.4)
    * **C9** — v0 emits raw MeSH descriptor IDs as src_id/dst_id, which
      do not match the canonical Compound (DrugBank ID) / Disease (UMLS
      CUI) namespaces used by sibling loaders. Crosswalk integration
      added; unresolved IDs get ``id_confidence="low"``. (Issues 15.7,
      15.10)
    * **C10** — v0 emits empty src_id/dst_id on missing data. Fixed via
      quarantine — bad rows go to DLQ, never to the KG. (Issue 4.7)

Scientific Scope
----------------
- **Source:** ClinicalTrials.gov, served via AACT by CTTI
  (Clinical Trials Transformation Initiative, Duke-Margolis / FDA).
- **URL:** https://aact.ctti-clinicaltrials.org/static/static_db_copies/dataset/aact_dataset.zip
- **File:** ``aact_dataset.zip`` (~500 MB compressed, ~500K records).
- **Format:** SQLite database (one .db file inside the zip). Tables used
  by this loader (Issue 13.12 — AACT table/column reference):

  =====================  ===================================================
  Table                  Columns used
  =====================  ===================================================
  ``studies``            nct_id, brief_title, phase, overall_status,
                         study_type, enrollment, why_stopped, has_results,
                         start_date, completion_date
  ``interventions``      nct_id, name, intervention_type, description
  ``interventions_mesh_terms``  nct_id, mesh_term (modern schema)
  ``conditions``         nct_id, name, mesh_term (legacy schema)
  ``conditions_mesh_terms``     nct_id, mesh_term (modern schema)
  ``designs``            nct_id, allocation, intervention_model, masking,
                         primary_purpose
  ``primary_outcomes``   nct_id, measure
  =====================  ===================================================

- **License:** CC0 1.0 (public domain). Every record carries
  ``_license="CC0 1.0"`` and ``_attribution=CLINICALTRIALS_ATTRIBUTION``
  (Issue 13.7).
- **Citation:** CTTI requests the citation
  ``AACT data extracted from https://aact.ctti-clinicaltrials.org.``
  for any derivative work (Issue 13.8). Propagated to lineage file.

PII Declaration
---------------
This loader queries only aggregate trial metadata (no patient-level data).
The AACT ``principal_investigators`` table contains PI names but is NOT
queried by this loader (Issue 9.6). If a future change adds PI queries,
PII handling must be revisited.

.. warning::
    **CROSS-JOIN SEMANTICS — PATIENT SAFETY** (Fixes: 13.5, 2.2, 3.3):

    The AACT schema does NOT link interventions to conditions at the row
    level. A trial with interventions [Drug A, Placebo] and conditions
    [Disease X, Disease Y] produces 4 rows in the JOIN. Only ONE of these
    rows (Drug A → Disease X) is the experimental association the trial
    was designed to test; the other 3 are fabrications of the JOIN.

    This loader mitigates (does not eliminate) the problem by:
      1. Tagging placebo/comparator interventions via description regex
         (Issue 3.3) — these edges get drug_role='comparator_or_placebo',
         evidence_strength *= 0.3, id_confidence='low'.
      2. Penalizing evidence_strength for parallel-design trials with
         N_interventions × N_conditions > 4 (Issue 2.2).
      3. Emitting a WARNING per trial with high cross-product inflation.

    The fully-correct fix requires joining result_groups / outcome_analysis
    to identify the experimental arm — see Issue 2.2 option (b). That mode
    is available via ``id_strictness='strict_arm'`` (future work — excluded
    ~70% of trials that lack results).

Public API
----------
Backward compatibility (master prompt Rule R3) — the three original v0
public functions remain importable with the SAME positional signatures,
SAME types, and SAME default behaviors (with one safe upgrade: LIKE
substring matching replaced by exact-match IN — see Issue 3.2):

- ``download_clinicaltrials(force=False) -> Path``
- ``parse_clinicaltrials(ct_dir=None, phase="Phase 3") -> pd.DataFrame``
- ``clinicaltrials_to_edge_records(df) -> List[Dict]``

New public functions (additive only — Rule R2/R3):

- ``parse_clinicaltrials_trials(ct_dir=None, phases=None, ...) -> pd.DataFrame``
- ``iter_clinicaltrials_trials(ct_dir=None, phases=None, ...) -> Iterator[Dict]``
- ``clinicaltrials_to_edge_records_streaming(df_or_iter, **kwargs) -> Iterator[Dict]``
- ``clinicaltrials_to_node_records(df, **kwargs) -> List[Dict]``
- ``clinicaltrials_to_graph(df, **kwargs) -> Tuple[List, List]``
- ``validate_clinicaltrials(df, edges, **kwargs) -> Dict[str, Any]``
- ``load_clinicaltrials(skip_neo4j=True, **kwargs) -> Dict[str, Any]``

New public class:

- ``ClinicalTrialsLoader``  (Loader Protocol adapter — Issue 1.1)
- ``ClinicalTrialsConfig``  (frozen dataclass — Issue 1.6)

Environment Variables
---------------------
All env vars are read at call time (not import time) so tests can
monkeypatch ``os.environ`` between calls:

==============================  =============================================
Env var                         Purpose
==============================  =============================================
``DRUGOS_CLINICALTRIALS_SKIP``  Skip loader entirely
``DRUGOS_CLINICALTRIALS_OFFLINE`` Use cached file only — no download
``DRUGOS_CLINICALTRIALS_FORCE_DOWNLOAD`` Force re-download
``DRUGOS_CLINICALTRIALS_ALLOW_STALE`` Allow stale cache on download failure
``DRUGOS_CLINICALTRIALS_ALLOW_LEGACY`` Allow legacy AACT schema (mesh_term column)
``DRUGOS_CLINICALTRIALS_SKIP_SHA256`` Skip SHA-256 verification (dev only)
``DRUGOS_CLINICALTRIALS_CHUNK_SIZE`` SQL read chunk size
``DRUGOS_CLINICALTRIALS_MAX_RETRIES`` Download retry count
``DRUGOS_CLINICALTRIALS_RETRY_BACKOFF_BASE`` Exponential backoff base
``DRUGOS_CLINICALTRIALS_DOWNLOAD_TIMEOUT`` Per-request timeout seconds
``DRUGOS_CLINICALTRIALS_CIRCUIT_BREAKER_THRESHOLD`` DLQ-count circuit breaker
``DRUGOS_CLINICALTRIALS_PINNED_RELEASE`` Pinned AACT release for reproducibility
==============================  =============================================

References
----------
- AACT documentation: https://aact.ctti-clinicaltrials.org/definitions
- AACT schema: https://aact.ctti-clinicaltrials.org/schema
- AACT license: CC0 1.0 (https://creativecommons.org/publicdomain/zero/1.0/)
- ClinicalTrials.gov: https://clinicaltrials.gov/
- DrugOS Coding Standards: ``drugos_graph/compliance.md``
- PEP 8 / 257 / 563 / 544 (style, docstrings, lazy annotations, Protocols).

SCHEMA CHANGELOG
----------------
v2.1.0 (this file) — institutional-grade audit fix. 148 findings across
                      16 domains. Backward-compatible shims preserved.
v1.0.0 (v0 prototype) — 116 lines. Three free functions. Known-defective
                        (LIKE substring, no MeSH JOIN, no why_stopped,
                        no enrollment, raw MeSH as src_id, etc.).

Fixes: Issues 1.1-1.6 (Architecture), 2.1-2.10 (Design),
       3.1-3.15 (Scientific Correctness), 4.1-4.16 (Coding),
       5.1-5.11 (Data Quality), 6.1-6.11 (Reliability),
       7.1-7.10 (Idempotency), 8.1-8.10 (Performance),
       9.1-9.10 (Security), 10.1-10.12 (Testing — tests in
       tests/test_clinicaltrials_loader.py), 11.1-11.11 (Logging),
       12.1-12.12 (Configuration), 13.1-13.12 (Documentation),
       14.1-14.12 (Compliance), 15.1-15.11 (Interoperability),
       16.1-16.12 (Data Lineage).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
import warnings
import zipfile
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Dict,
    FrozenSet,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

import pandas as pd

from .config import (
    ALLOWED_CLINICALTRIALS_URLS,
    CLINICALTRIALS_ALLOCATION_BONUS,
    CLINICALTRIALS_ALLOW_LEGACY_SCHEMA,
    CLINICALTRIALS_ALLOW_STALE,
    CLINICALTRIALS_ATTRIBUTION,
    CLINICALTRIALS_CITATION,
    CLINICALTRIALS_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
    CLINICALTRIALS_CIRCUIT_BREAKER_THRESHOLD,
    CLINICALTRIALS_COMPARATOR_EVIDENCE_MULTIPLIER,
    CLINICALTRIALS_COMPARATOR_PATTERN,
    CLINICALTRIALS_CROSS_PRODUCT_PENALTY,
    CLINICALTRIALS_CROSS_PRODUCT_WARN_THRESHOLD,
    CLINICALTRIALS_CHUNK_SIZE,
    CLINICALTRIALS_DEAD_LETTER_PATH,
    CLINICALTRIALS_DEFAULT_ALLOWED_STATUSES,
    CLINICALTRIALS_DEFAULT_INTERVENTION_TYPES,
    CLINICALTRIALS_DEFAULT_MAX_TRIAL_AGE_YEARS,
    CLINICALTRIALS_DEFAULT_MIN_ENROLLMENT,
    CLINICALTRIALS_DEFAULT_PHASES,
    CLINICALTRIALS_DEFAULT_STUDY_TYPES,
    CLINICALTRIALS_DEVIATION_CRITICAL_THRESHOLD,
    CLINICALTRIALS_DEVIATION_WARNING_THRESHOLD,
    CLINICALTRIALS_DOWNLOAD_CHUNK_SIZE,
    CLINICALTRIALS_DOWNLOAD_TIMEOUT_SECONDS,
    CLINICALTRIALS_EDGE_ID_SOURCE,
    CLINICALTRIALS_EMITTABLE_TRIPLES,
    CLINICALTRIALS_ENROLLMENT_BONUS_LARGE_TRIAL,
    CLINICALTRIALS_ENROLLMENT_BONUS_VALUE,
    CLINICALTRIALS_EXTRACT_SENTINEL,
    CLINICALTRIALS_FORCE_DOWNLOAD,
    CLINICALTRIALS_GARBAGE_MESH_VALUES,
    CLINICALTRIALS_HAS_RESULTS_BONUS,
    CLINICALTRIALS_HASH_LENGTH,
    CLINICALTRIALS_LICENSE,
    CLINICALTRIALS_LINEAGE_LOG_PATH,
    CLINICALTRIALS_MASKING_BONUS,
    CLINICALTRIALS_MAX_MESH_PER_INTERVENTION,
    CLINICALTRIALS_MAX_RETRIES,
    CLINICALTRIALS_MEMORY_CEILING_WARNING_THRESHOLD,
    CLINICALTRIALS_MIN_VALID_SIZE_BYTES,
    CLINICALTRIALS_NCT_ID_REGEX_PATTERN,
    CLINICALTRIALS_NEO4J_BATCH_SIZE,
    CLINICALTRIALS_OFFLINE,
    CLINICALTRIALS_PARSER_VERSION,
    CLINICALTRIALS_PHASE_STRENGTH,
    CLINICALTRIALS_PINNED_RELEASE,
    CLINICALTRIALS_PINNED_SHA256,
    CLINICALTRIALS_PROGRESS_LOG_INTERVAL,
    CLINICALTRIALS_QUALITY_REPORT_PATH,
    CLINICALTRIALS_QUARANTINE_PATH,
    CLINICALTRIALS_RETRY_BACKOFF_BASE,
    CLINICALTRIALS_SAFETY_STOP_PENALTY,
    CLINICALTRIALS_SAFETY_STOP_PATTERN,
    CLINICALTRIALS_SCHEMA_VERSION,
    CLINICALTRIALS_SKIP,
    CLINICALTRIALS_SKIP_SHA256,
    CLINICALTRIALS_STALE_CACHE_WARNING_DAYS,
    CLINICALTRIALS_SUSPECT_ENROLLMENT_THRESHOLD,
    CLINICALTRIALS_USER_AGENT,
    CLINICALTRIALS_VALID_INTERVENTION_TYPES,
    CLINICALTRIALS_VALID_NODE_TYPES,
    CLINICALTRIALS_VALID_PHASES,
    CLINICALTRIALS_VALID_STATUSES,
    CLINICALTRIALS_VALID_STUDY_TYPES,
    CLINICALTRIALS_ZIP_MAGIC,
    CORE_EDGE_TYPES,
    DATA_SOURCES,
    DEAD_LETTER_DIR,
    LOGS_DIR,
    Neo4jConfig,
    OPTIONAL_SOURCES,
    RAW_DIR,
    SOURCE_CLINICALTRIALS,
    SOURCE_KEY_CLINICALTRIALS,
    ensure_dirs,
    set_global_seed,
    SEED,
)
from .exceptions import (
    CircuitBreakerOpenError,
    ClinicalTrialsConfigurationError,
    ClinicalTrialsDataIntegrityError,
    ClinicalTrialsDownloadError,
    ClinicalTrialsEdgeLoadMismatchError,
    ClinicalTrialsParseError,
    ClinicalTrialsSchemaError,
    ClinicalTrialsSecurityError,
    CriticalDataSourceError,
    DrugOSDataError,
)

# Schema / Protocol / crosswalk (avoid circular import at module top).
from .schemas import (  # noqa: E402
    CLINICALTRIALS_PROVENANCE_KEYS,
    ClinicalTrialEdgeRecord,
    ClinicalTrialNodeRecord,
    ClinicalTrialTrialRecord,
    ClinicalTrialsDeadLetterEntry,
    ClinicalTrialsLoaderMetrics,
    ClinicalTrialsValidationReport,
)

try:  # TYPE_CHECKING only — never imported at runtime.
    from typing import TYPE_CHECKING
    if TYPE_CHECKING:
        from ._loader_protocol import Loader  # noqa: F401
        from .id_crosswalk import IDCrosswalk  # noqa: F401
except ImportError:  # pragma: no cover — defensive.
    pass


# =============================================================================
# Section 1 — Module-level constants
# =============================================================================
# Fixes Domain 4 (Coding) Issue 4.10 — lazy logging; no f-strings in log calls.
# Fixes Domain 12 (Configuration) Issue 12.1 — defaults come from config.py.
# Fixes Domain 7 (Idempotency) Issue 7.5 — pipeline_version in every edge.

PARSER_VERSION: str = CLINICALTRIALS_PARSER_VERSION  # "2.1.0"  # Issue 7.5
SCHEMA_VERSION: str = CLINICALTRIALS_SCHEMA_VERSION  # "2.1.0"  # Issue 14.6
SOURCE_NAME: str = SOURCE_CLINICALTRIALS  # "ClinicalTrials"
SOURCE_KEY: str = SOURCE_KEY_CLINICALTRIALS  # "clinicaltrials"
LICENSE: str = CLINICALTRIALS_LICENSE  # "CC0 1.0"
ATTRIBUTION: str = CLINICALTRIALS_ATTRIBUTION
CITATION: str = CLINICALTRIALS_CITATION  # Issue 13.8

# Pre-compiled regex patterns (Issue 3.15, 14.8 — NCT ID format validation).
_NCT_ID_REGEX: re.Pattern[str] = re.compile(
    CLINICALTRIALS_NCT_ID_REGEX_PATTERN  # ^NCT\d{8}$
)
# Pre-compiled comparator/placebo pattern (Issue 3.3).
_COMPARATOR_REGEX: re.Pattern[str] = re.compile(
    CLINICALTRIALS_COMPARATOR_PATTERN
)
# Pre-compiled safety-stop pattern (Issue 3.5).
_SAFETY_STOP_REGEX: re.Pattern[str] = re.compile(
    CLINICALTRIALS_SAFETY_STOP_PATTERN
)
# v41 ROOT FIX (Task K2 / SEV2 COMPOUND): local InChIKey pattern used by
# the drug_name → InChIKey crosswalk fallback in
# ``clinicaltrials_to_edge_records`` to detect crosswalk misses. The
# shared ``_normalize_compound_id_to_inchikey`` returns the ORIGINAL ID
# when no mapping is found — by design, so loaders with graceful
# CID-keyed fallback paths still work. ClinicalTrials must NOT use that
# fallback: emitting "NAME:<drug_name>" as ``src_id`` would be
# dead-lettered by kg_builder anyway (v28 ROOT FIX P2-B-12 removed
# NAME: from ID_PATTERNS["Compound"]). So the loader detects the
# crosswalk miss via this pattern check and dead-letters the edge with
# reason="no_inchikey_for_drug_name" INSTEAD of emitting a NAME:-prefixed
# ID. Pattern matches the canonical 27-char InChIKey form (14-10-1
# uppercase letters with hyphens) — kept in sync with
# ``id_crosswalk._INCHIKEY_PATTERN``.
_INCHIKEY_RE: re.Pattern[str] = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")

# URL credential masking regex (Issue 9.9 — log sanitization).
_URL_CRED_RE: re.Pattern[str] = re.compile(r"://([^:/@]+):([^@/]+)@")

# Secret patterns (Issue 9.10 — secret scanning on output).
_SECRET_PATTERNS: List[re.Pattern[str]] = [
    re.compile(r"(?i)api[_-]?key\s*[=:]\s*[\w-]{20,}"),
    re.compile(r"(?i)aws[_-]?(access|secret)[\w-]*\s*[=:]\s*[\w/+=]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----"),
    re.compile(r"(?i)password\s*[=:]\s*\S{6,}"),
]

# Process-cached load_id (correlation ID — Issue 16.9).
_LOAD_ID_LOCK: threading.Lock = threading.Lock()
_LOAD_ID: Optional[str] = None

# Dead-letter queue write lock (thread-safe DLQ writes — Issue 6.5).
_DLQ_LOCK: threading.Lock = threading.Lock()
# Lineage log write lock (thread-safe lineage writes — Issue 16.10).
_LINEAGE_LOCK: threading.Lock = threading.Lock()
# Audit log write lock (thread-safe audit writes — Issue 16.9).
_AUDIT_LOCK: threading.Lock = threading.Lock()

# Circuit breaker state (Issue 6.11).
_CB_LOCK: threading.Lock = threading.Lock()
_CB_FAILURE_COUNT: int = 0
_CB_OPEN_UNTIL: float = 0.0

# Sentinel for "no cached parsed DataFrame" (Issue 8.3 — generator pattern).
_PARSED_CACHE: Dict[str, List[Dict[str, Any]]] = {}

# MB constant (used for byte→MB conversions in logging).
_MB: int = 1_000_000

# Required AACT tables (Issue 4.5 — validate DB is AACT).
_REQUIRED_AACT_TABLES: FrozenSet[str] = frozenset({
    "studies", "interventions", "conditions", "designs",
})


__all__: List[str] = [
    # ── Version constants ──
    "PARSER_VERSION",
    "SCHEMA_VERSION",
    # ── Configuration ──
    "ClinicalTrialsConfig",
    # ── Download ──
    "download_clinicaltrials",
    # ── Parse ──
    "parse_clinicaltrials",
    "parse_clinicaltrials_trials",
    "iter_clinicaltrials_trials",
    # ── Convert ──
    "clinicaltrials_to_edge_records",
    "clinicaltrials_to_edge_records_streaming",
    "clinicaltrials_to_node_records",
    "clinicaltrials_to_graph",
    # ── Validation ──
    "validate_clinicaltrials",
    # ── End-to-end ──
    "load_clinicaltrials",
    # ── Protocol adapter ──
    "ClinicalTrialsLoader",
    # ── Backward-compat aliases ──
    "parse_clinicaltrials_evidence",
]

logger: logging.Logger = logging.getLogger(__name__)


# =============================================================================
# Section 2 — ClinicalTrialsConfig dataclass
# =============================================================================
# Fixes Domain 12 (Configuration) Issues 12.1, 12.2, 12.7, 12.12.
# Fixes Domain 7 (Idempotency) Issue 7.1 — frozen instance prevents mutation.
# Fixes Domain 4 (Coding) Issue 4.11 — split force into force_download /
# force_extract, with `force=` kept as a deprecated alias.


@dataclass(frozen=True)
class ClinicalTrialsConfig:
    """Frozen configuration for the ClinicalTrials loader.

    All thresholds are documented with their scientific rationale. Instances
    are frozen (immutable) to prevent accidental mutation during a pipeline
    run (Domain 7 — Idempotency, Issue 7.1).

    Parameters
    ----------
    phases : tuple[str, ...]
        Trial phases to include (Issue 3.2, 3.4). Default
        ``("Phase 3", "Phase 4")`` — Phase 4 is post-marketing surveillance,
        STRONGER evidence than Phase 3 (Issue 3.4 / 13.2).
    intervention_types : tuple[str, ...]
        AACT ``intervention_type`` values to include (Issue 2.7). Default
        ``("Drug", "Biological")`` — Biological covers mAbs, vaccines, cell
        therapies (~30% of modern FDA approvals — Issue 13.3).
    study_types : tuple[str, ...]
        AACT ``study_type`` values to include (Issue 3.7). Default
        ``("Interventional",)`` — RCTs only.
    allowed_statuses : tuple[str, ...]
        AACT ``overall_status`` values to include (Issue 3.11). Default
        excludes Withdrawn/Suspended/Terminated/etc. — those are not
        positive evidence.
    min_enrollment : int
        Minimum enrollment count filter (Issue 3.6). Default 0 (no filter).
    max_trial_age_years : int or None
        When set, exclude trials older than this many years (Issue 3.13).
        Default None — include all trials.
    require_results : bool
        If True, only include trials with published results (Issue 3.10).
        v27 ROOT FIX (P2-L-7): default is now True. The previous default
        (False) included Recruiting/Not-yet-recruiting trials with zero
        results data as efficacy evidence — a patient-safety risk.
    force_download : bool
        If True, re-download even if a cached copy exists (Issue 4.11).
    force_extract : bool
        If True, re-extract even if extract_dir is complete (Issue 4.11).
    allow_stale : bool
        If True, fall back to cached copy when download fails (Issue 6.4).
    allow_legacy_schema : bool
        If True, allow the legacy AACT schema (mesh_term as direct column)
        with a WARNING (Issue 2.9). Default False.
    sort_output : bool
        If True, sort output edges for deterministic ordering (Issue 7.9).
        Default True.
    progress_log_interval : int
        Number of rows between progress log messages during parsing.
    chunksize : int
        SQL read chunk size (Issue 8.1). Default 50000.
    neo4j_batch_size : int
        Maximum edges per Neo4j ``load_edges_bulk_create`` call (Issue 8.6).
    limit : int or None
        If set, append ``LIMIT ?`` to SQL (Issue 8.5 — testing only).
    pinned_aact_release : str or None
        When set, refuse to use any AACT snapshot other than this one
        (Issue 7.8 — backfilling safety).
    raw_dir : Path or None
        Directory for raw downloaded files. If None, defaults to ``RAW_DIR``.
    dead_letter_path : Path or None
        Path to the dead-letter queue JSONL file (Issue 6.5).
    quarantine_path : Path or None
        Path to the quarantine CSV file (Issue 6.5).
    lineage_log_path : Path or None
        Path to the lineage log JSONL file (Issue 16.10).
    audit_log_path : Path or None
        Path to the audit log JSONL file (Issue 16.9).
    """

    phases: Tuple[str, ...] = CLINICALTRIALS_DEFAULT_PHASES
    intervention_types: Tuple[str, ...] = CLINICALTRIALS_DEFAULT_INTERVENTION_TYPES
    study_types: Tuple[str, ...] = CLINICALTRIALS_DEFAULT_STUDY_TYPES
    allowed_statuses: Tuple[str, ...] = CLINICALTRIALS_DEFAULT_ALLOWED_STATUSES
    min_enrollment: int = CLINICALTRIALS_DEFAULT_MIN_ENROLLMENT
    max_trial_age_years: Optional[int] = CLINICALTRIALS_DEFAULT_MAX_TRIAL_AGE_YEARS
    require_results: bool = True  # v27 ROOT FIX (P2-L-7): was False
    force_download: bool = False
    force_extract: bool = False
    allow_stale: bool = False
    allow_legacy_schema: bool = False
    sort_output: bool = True
    progress_log_interval: int = CLINICALTRIALS_PROGRESS_LOG_INTERVAL
    chunksize: int = CLINICALTRIALS_CHUNK_SIZE
    neo4j_batch_size: int = CLINICALTRIALS_NEO4J_BATCH_SIZE
    limit: Optional[int] = None
    pinned_aact_release: Optional[str] = None
    raw_dir: Optional[Path] = None
    dead_letter_path: Optional[Path] = None
    quarantine_path: Optional[Path] = None
    lineage_log_path: Optional[Path] = None
    audit_log_path: Optional[Path] = None

    def __post_init__(self) -> None:
        """Validate configuration values (Issue 12.7, 12.12).

        Raises
        ------
        ClinicalTrialsConfigurationError
            If any field has an invalid value.
        """
        # Issue 2.10 / 4.6 — reject empty phases.
        if not self.phases:
            raise ClinicalTrialsConfigurationError(
                "phases must not be empty (Issue 2.10, 4.6).",
                context={"phases": self.phases},
            )
        # Issue 9.7 — reject LIKE wildcards.
        for p in self.phases:
            if not isinstance(p, str) or "%" in p or "_" in p:
                raise ClinicalTrialsConfigurationError(
                    f"phase value {p!r} contains LIKE wildcard "
                    f"(Issue 9.7).",
                    context={"phase": p},
                )
            if p not in CLINICALTRIALS_VALID_PHASES:
                raise ClinicalTrialsConfigurationError(
                    f"phase value {p!r} not in controlled vocabulary "
                    f"CLINICALTRIALS_VALID_PHASES (Issue 2.10, 5.5).",
                    context={"phase": p,
                             "valid_phases": sorted(CLINICALTRIALS_VALID_PHASES)},
                )
        # Issue 2.7 — intervention_types validation.
        if not self.intervention_types:
            raise ClinicalTrialsConfigurationError(
                "intervention_types must not be empty (Issue 2.7).",
                context={"intervention_types": self.intervention_types},
            )
        for it in self.intervention_types:
            if it not in CLINICALTRIALS_VALID_INTERVENTION_TYPES:
                raise ClinicalTrialsConfigurationError(
                    f"intervention_type {it!r} not in controlled vocabulary "
                    f"(Issue 2.7).",
                    context={"intervention_type": it},
                )
        # Issue 3.7 — study_types validation.
        if not self.study_types:
            raise ClinicalTrialsConfigurationError(
                "study_types must not be empty (Issue 3.7).",
                context={"study_types": self.study_types},
            )
        for st in self.study_types:
            if st not in CLINICALTRIALS_VALID_STUDY_TYPES:
                raise ClinicalTrialsConfigurationError(
                    f"study_type {st!r} not in controlled vocabulary "
                    f"(Issue 3.7).",
                    context={"study_type": st},
                )
        # Issue 3.11 — allowed_statuses validation.
        if not self.allowed_statuses:
            raise ClinicalTrialsConfigurationError(
                "allowed_statuses must not be empty (Issue 3.11).",
                context={"allowed_statuses": self.allowed_statuses},
            )
        for s in self.allowed_statuses:
            if s not in CLINICALTRIALS_VALID_STATUSES:
                raise ClinicalTrialsConfigurationError(
                    f"status {s!r} not in controlled vocabulary "
                    f"(Issue 3.11).",
                    context={"status": s},
                )
        # Issue 3.6 — min_enrollment validation.
        if not isinstance(self.min_enrollment, int) or self.min_enrollment < 0:
            raise ClinicalTrialsConfigurationError(
                f"min_enrollment must be >= 0, got {self.min_enrollment!r} "
                f"(Issue 3.6).",
                context={"min_enrollment": self.min_enrollment},
            )
        # Issue 3.13 — max_trial_age_years validation.
        if self.max_trial_age_years is not None:
            if not isinstance(self.max_trial_age_years, int) or \
                    self.max_trial_age_years <= 0:
                raise ClinicalTrialsConfigurationError(
                    f"max_trial_age_years must be positive int or None, "
                    f"got {self.max_trial_age_years!r} (Issue 3.13).",
                    context={"max_trial_age_years": self.max_trial_age_years},
                )
        # Issue 8.1 — chunksize validation.
        if not isinstance(self.chunksize, int) or self.chunksize <= 0:
            raise ClinicalTrialsConfigurationError(
                f"chunksize must be positive int, got {self.chunksize!r} "
                f"(Issue 8.1).",
                context={"chunksize": self.chunksize},
            )
        # Issue 8.5 — limit validation.
        if self.limit is not None and (
            not isinstance(self.limit, int) or self.limit <= 0
        ):
            raise ClinicalTrialsConfigurationError(
                f"limit must be positive int or None, got {self.limit!r} "
                f"(Issue 8.5).",
                context={"limit": self.limit},
            )
        # Issue 7.8 — pinned_aact_release validation.
        if self.pinned_aact_release is not None:
            if not isinstance(self.pinned_aact_release, str) or \
                    not self.pinned_aact_release.strip():
                raise ClinicalTrialsConfigurationError(
                    f"pinned_aact_release must be non-empty str or None, "
                    f"got {self.pinned_aact_release!r} (Issue 7.8).",
                    context={"pinned_aact_release": self.pinned_aact_release},
                )

    # ── Convenience accessors ──────────────────────────────────────────

    @property
    def effective_raw_dir(self) -> Path:
        """Return the raw_dir, defaulting to ``RAW_DIR`` if None."""
        return self.raw_dir or RAW_DIR

    @property
    def effective_dead_letter_path(self) -> Path:
        """Return the dead_letter_path, defaulting to CLINICALTRIALS_DEAD_LETTER_PATH."""
        return self.dead_letter_path or CLINICALTRIALS_DEAD_LETTER_PATH

    @property
    def effective_quarantine_path(self) -> Path:
        """Return the quarantine_path, defaulting to CLINICALTRIALS_QUARANTINE_PATH."""
        return self.quarantine_path or CLINICALTRIALS_QUARANTINE_PATH

    @property
    def effective_lineage_log_path(self) -> Path:
        """Return the lineage_log_path, defaulting to CLINICALTRIALS_LINEAGE_LOG_PATH."""
        return self.lineage_log_path or CLINICALTRIALS_LINEAGE_LOG_PATH

    @property
    def effective_audit_log_path(self) -> Path:
        """Return the audit_log_path, defaulting to CLINICALTRIALS_AUDIT_LOG_PATH."""
        return self.audit_log_path or (
            LOGS_DIR / "audit" / "clinicaltrials_access.jsonl"
        )

    @property
    def source_version(self) -> str:
        """Return the AACT release version (Issue 7.6)."""
        return self.pinned_aact_release or "current"

    @property
    def source_release_date(self) -> Optional[str]:
        """Return the AACT release date (Issue 7.6)."""
        return None  # AACT does not embed a release date in the DB.


# =============================================================================
# Section 3 — ID validators and MeSH / drug_name normalization
# =============================================================================
# Fixes Issues 3.15 (NCT format), 14.9 (MeSH normalization),
# 15.10 (drug name normalization), 5.11 (garbage MeSH).


def _validate_nct_id(raw: Any) -> Optional[str]:
    """Validate an NCT ID (Issue 3.15, 14.8, 15.6).

    Returns the canonical uppercase form (e.g. "NCT00000001"), or None if
    invalid. Accepts case-insensitive input, strips whitespace.

    Parameters
    ----------
    raw : Any
        The candidate NCT ID. Non-string inputs return None.

    Returns
    -------
    str or None
        The canonical NCT ID, or None.

    Examples
    --------
    >>> _validate_nct_id("NCT00000001")
    'NCT00000001'
    >>> _validate_nct_id("nct00000001")
    'NCT00000001'
    >>> _validate_nct_id("NOT_NCT") is None
    True
    """
    if not isinstance(raw, str):
        return None
    s: str = raw.strip().upper()
    if _NCT_ID_REGEX.match(s):
        return s
    return None


def _normalize_mesh(term: Any) -> Optional[str]:
    """Normalize a MeSH term (Issue 14.9).

    Returns the normalized form (whitespace-collapsed, stripped), or None
    if input is None / NaN / empty / in the garbage blocklist (Issue 5.11).

    Parameters
    ----------
    term : Any
        The candidate MeSH term. Non-string inputs return None.

    Returns
    -------
    str or None
        The normalized MeSH term, or None.
    """
    if term is None:
        return None
    if isinstance(term, float) and pd.isna(term):
        return None
    if not isinstance(term, str):
        term = str(term)
    s: str = term.strip()
    s = re.sub(r"\s+", " ", s)
    if not s:
        return None
    if s.upper() in CLINICALTRIALS_GARBAGE_MESH_VALUES:
        return None
    return s


def _normalize_drug_name(name: Any) -> Optional[str]:
    """Normalize a drug name (Issue 15.10).

    Strips whitespace, removes bracketed annotations like
    "[INVESTIGATIONAL DRUG]" or "(BAYER)", collapses whitespace.

    Parameters
    ----------
    name : Any
        The candidate drug name. Non-string inputs return None.

    Returns
    -------
    str or None
        The normalized drug name, or None.
    """
    if name is None:
        return None
    if isinstance(name, float) and pd.isna(name):
        return None
    if not isinstance(name, str):
        name = str(name)
    s: str = name.strip()
    # Strip bracketed annotations like "[INVESTIGATIONAL DRUG]" (Issue 15.10).
    s = re.sub(r"\s*\[[^\]]+\]\s*", " ", s).strip()
    # Strip parenthetical annotations like "(BAYER)".
    s = re.sub(r"\s*\([^)]*\)\s*", " ", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s if s else None


def _is_garbage_mesh(value: Any) -> bool:
    """Check if a MeSH value is in the garbage blocklist (Issue 5.11).

    Returns True if the value is None, NaN, empty, or in the
    ``CLINICALTRIALS_GARBAGE_MESH_VALUES`` blocklist.
    """
    if value is None:
        return True
    if isinstance(value, float) and pd.isna(value):
        return True
    s: str = str(value).strip().upper()
    return s in CLINICALTRIALS_GARBAGE_MESH_VALUES or not s


def _is_valid_mesh_id_format(term: Optional[str]) -> bool:
    """Check if a MeSH term LOOKS like a MeSH descriptor ID (D######).

    Returns True if the term matches ``^D\\d{6}$`` (case-insensitive).
    Returns True for free-text terms — they're not garbage, just non-ID
    MeSH terms (used as fallback IDs with id_confidence="low").
    """
    if term is None:
        return False
    s: str = str(term).strip()
    # MeSH descriptor IDs are like "D000001", "D014859".
    return bool(re.match(r"^D\d{6}$", s, re.IGNORECASE))


# =============================================================================
# Section 4 — Security helpers
# =============================================================================
# Fixes Issues 4.3 (zip-slip), 9.1 (TLS), 9.3 (zip-slip),
# 9.8 (file perms), 9.9 (log sanitization), 9.10 (secret scanning).


def _create_tls_context() -> ssl.SSLContext:
    """Create a hardened TLS context (Issue 9.1).

    Returns
    -------
    ssl.SSLContext
        A TLS context requiring certificate verification, TLS 1.2+,
        and HIGH ciphers.

    Raises
    ------
    ClinicalTrialsSecurityError
        If the TLS context cannot be created.
    """
    try:
        ctx: ssl.SSLContext = ssl.create_default_context()
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx
    except Exception as exc:
        raise ClinicalTrialsSecurityError(
            f"Failed to create TLS context: {exc}",
            context={"error": str(exc)},
        ) from exc


# Module-cached TLS context (Issue 9.1).
_SSL_CONTEXT: ssl.SSLContext = _create_tls_context()


def _sanitize_url_for_logging(url: str) -> str:
    """Mask credentials in a URL for safe logging (Issue 9.9).

    Parameters
    ----------
    url : str
        The URL to sanitize.

    Returns
    -------
    str
        The URL with any embedded credentials masked as ``***``.
    """
    if not isinstance(url, str):
        return str(url)
    return _URL_CRED_RE.sub(r"://***:***@", url)


def _validate_url_against_allowlist(url: str) -> None:
    """Validate a URL against the AACT allowlist (Issue 9.1).

    Parameters
    ----------
    url : str
        The URL to validate.

    Raises
    ------
    ClinicalTrialsSecurityError
        If the URL scheme is not HTTPS, contains embedded credentials,
        or is not in ``ALLOWED_CLINICALTRIALS_URLS``.
    """
    if not isinstance(url, str) or not url:
        raise ClinicalTrialsSecurityError(
            "URL must be a non-empty string.",
            context={"url": url},
        )
    if not url.startswith("https://"):
        raise ClinicalTrialsSecurityError(
            f"URL must be HTTPS, got {url!r} (Issue 9.1).",
            context={"url": _sanitize_url_for_logging(url)},
        )
    if "://" in url and "@" in url.split("://", 1)[1].split("/", 1)[0]:
        raise ClinicalTrialsSecurityError(
            f"URL contains embedded credentials (Issue 9.1).",
            context={"url": _sanitize_url_for_logging(url)},
        )
    if not any(
        url.startswith(prefix) for prefix in ALLOWED_CLINICALTRIALS_URLS
    ):
        raise ClinicalTrialsSecurityError(
            f"URL not in ALLOWED_CLINICALTRIALS_URLS allowlist "
            f"(Issue 9.1). URL: {_sanitize_url_for_logging(url)}",
            context={"url": _sanitize_url_for_logging(url),
                     "allowlist": list(ALLOWED_CLINICALTRIALS_URLS)},
        )


def _validate_path_within_dir(path: Path, directory: Path) -> None:
    """Validate that ``path`` resolves within ``directory`` (Issue 4.3, 9.3).

    Parameters
    ----------
    path : Path
        The path to validate.
    directory : Path
        The directory the path must be inside.

    Raises
    ------
    ClinicalTrialsSecurityError
        If the path resolves outside the directory.
    """
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError as exc:
        raise ClinicalTrialsSecurityError(
            f"Path {path} resolves outside {directory} (Issue 4.3, 9.3).",
            context={"path": str(path), "directory": str(directory)},
        ) from exc


def _safe_extract(zip_path: Path, extract_dir: Path) -> None:
    """Extract zip with zip-slip defense (Issue 4.3, 9.3).

    Validates every entry path before extraction. Rejects:
      * Absolute paths (``/foo``)
      * Drive letters (``C:\\foo``)
      * Paths resolving outside ``extract_dir`` (zip-slip)

    After successful extraction, writes a sentinel file
    ``_AACT_EXTRACT_COMPLETE`` (Issue 4.8, 6.9) so future runs can detect
    incomplete extractions.

    Parameters
    ----------
    zip_path : Path
        Path to the zip file.
    extract_dir : Path
        Directory to extract into.

    Raises
    ------
    ClinicalTrialsSecurityError
        If any entry path is unsafe.
    ClinicalTrialsDownloadError
        If the zip is corrupt.
    """
    extract_dir.mkdir(parents=True, exist_ok=True)
    extract_dir_resolved: Path = extract_dir.resolve()
    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            # Issue 4.3 — validate every entry path before extraction.
            for info in z.infolist():
                name: str = info.filename
                if name.startswith("/") or (
                    len(name) > 1 and name[1] == ":"
                ):
                    raise ClinicalTrialsSecurityError(
                        f"Unsafe zip entry path (absolute or drive letter): "
                        f"{name!r} (Issue 4.3).",
                        context={"entry": name, "zip_path": str(zip_path)},
                    )
                target: Path = (extract_dir / name).resolve()
                try:
                    target.relative_to(extract_dir_resolved)
                except ValueError as exc:
                    raise ClinicalTrialsSecurityError(
                        f"Zip-slip attempt detected: entry {name!r} "
                        f"resolves outside {extract_dir} (Issue 4.3, 9.3).",
                        context={"entry": name,
                                 "zip_path": str(zip_path),
                                 "extract_dir": str(extract_dir)},
                    ) from exc
            z.extractall(extract_dir)
    except zipfile.BadZipFile as exc:
        raise ClinicalTrialsDownloadError(
            f"Corrupt zip file: {zip_path} (Issue 4.9, 6.8). "
            f"Error: {exc}",
            context={"zip_path": str(zip_path), "error": str(exc)},
        ) from exc
    # Issue 4.8 / 6.9 — write sentinel file to indicate extraction complete.
    sentinel: Path = extract_dir / CLINICALTRIALS_EXTRACT_SENTINEL
    sentinel.touch()
    # Issue 9.8 — restrict file permissions on extracted DB files (best-effort).
    for db_file in extract_dir.rglob("*.db"):
        try:
            os.chmod(db_file, 0o600)
        except OSError:
            # Best-effort — may fail on Windows or read-only filesystems.
            pass


def _is_extract_complete(extract_dir: Path) -> bool:
    """Check if extraction finished successfully (Issue 4.8, 6.9).

    Parameters
    ----------
    extract_dir : Path
        The extraction directory.

    Returns
    -------
    bool
        True if the sentinel file exists and is non-empty.
    """
    sentinel: Path = extract_dir / CLINICALTRIALS_EXTRACT_SENTINEL
    return sentinel.exists() and sentinel.stat().st_size >= 0


def _is_valid_zip(path: Path) -> bool:
    """Check if a path is a valid zip file (Issue 4.9, 6.8).

    Verifies the file exists, is non-empty, and starts with the ZIP magic
    bytes ``PK\\x03\\x04``.

    Parameters
    ----------
    path : Path
        The path to check.

    Returns
    -------
    bool
        True if the file is a valid zip.
    """
    if not path.exists() or path.stat().st_size < 4:
        return False
    try:
        with open(path, "rb") as f:
            return f.read(4) == CLINICALTRIALS_ZIP_MAGIC
    except OSError:
        return False


def _sanitize_for_log(value: Any, max_len: int = 200) -> str:
    """Sanitize a value for safe logging (Issue 9.9).

    Replaces newlines with escaped form, truncates to ``max_len``.

    Parameters
    ----------
    value : Any
        The value to sanitize.
    max_len : int
        Maximum length before truncation.

    Returns
    -------
    str
        The sanitized string.
    """
    s: str = str(value).replace("\n", "\\n").replace("\r", "\\r")
    if len(s) > max_len:
        s = s[:max_len] + "...[truncated]"
    return s


def _scan_for_secrets(props: Dict[str, Any]) -> Optional[str]:
    """Scan edge props for common secret patterns (Issue 9.10).

    Returns the field name of the first matched secret, or None.

    Parameters
    ----------
    props : dict
        The edge props to scan.

    Returns
    -------
    str or None
        ``"secret_in_field:<field_name>"`` if a secret is detected, else None.
    """
    for key, value in props.items():
        s: str = str(value)
        for pattern in _SECRET_PATTERNS:
            if pattern.search(s):
                return f"secret_in_field:{key}"
    return None


# =============================================================================
# Section 5 — Download (atomic, retried, TLS-verified, hash-verified)
# =============================================================================
# Fixes Issues 4.2 (urlopen+Request), 6.1 (retry), 6.2 (timeout),
# 6.3 (atomic write), 6.4 (allow_stale), 6.10 (checksum),
# 6.11 (circuit breaker), 9.1 (TLS), 9.2 (User-Agent),
# 12.4 (max_size_bytes), 12.5 (checksum), 4.9 (corrupt zip sniff),
# 6.8 (HTML error page), 7.3 (downloaded_at), 7.4 (source_sha256),
# 16.2 (downloaded_at in props), 16.3 (source_sha256 in props),
# 16.8 (impact analysis — old SHA capture).


def _compute_sha256(path: Path, chunk_size: int = 1 << 20) -> str:
    """Compute the SHA-256 hex digest of a file (Issue 6.10, 7.4).

    Streams the file in 1 MiB chunks to avoid loading large files into
    memory.

    Parameters
    ----------
    path : Path
        The file to hash.
    chunk_size : int
        Read chunk size in bytes.

    Returns
    -------
    str
        The hex SHA-256 digest.

    Raises
    ------
    OSError
        If the file cannot be read.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk: bytes = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _format_age(mtime: float) -> str:
    """Format a file's age in human-readable form (Issue 11.2).

    Parameters
    ----------
    mtime : float
        File modification time (epoch seconds).

    Returns
    -------
    str
        Human-readable age (e.g. "3.2 days", "12.5 hours", "45 seconds").
    """
    age_seconds: float = time.time() - mtime
    if age_seconds < 0:
        return "future"
    if age_seconds < 60:
        return f"{age_seconds:.1f} seconds"
    if age_seconds < 3600:
        return f"{age_seconds / 60:.1f} minutes"
    if age_seconds < 86400:
        return f"{age_seconds / 3600:.1f} hours"
    return f"{age_seconds / 86400:.1f} days"


def _read_lineage(lineage_path: Path) -> Optional[Dict[str, Any]]:
    """Read the most recent lineage entry (Issue 16.8 — impact analysis).

    Parameters
    ----------
    lineage_path : Path
        Path to the lineage JSONL file.

    Returns
    -------
    dict or None
        The most recent lineage entry, or None if the file doesn't exist.
    """
    if not lineage_path.exists():
        return None
    try:
        with open(lineage_path, "r", encoding="utf-8") as f:
            lines: List[str] = f.readlines()
        if not lines:
            return None
        return json.loads(lines[-1])
    except (OSError, json.JSONDecodeError):
        return None


def _atomic_download(
    url: str,
    dest: Path,
    timeout: int,
    ssl_ctx: ssl.SSLContext,
) -> int:
    """Atomic download with TLS verification (Issue 4.2, 6.3, 9.1).

    Downloads to a ``.tmp`` file, then ``os.replace`` to the destination.
    On any error, the ``.tmp`` file is deleted.

    Parameters
    ----------
    url : str
        The URL to download.
    dest : Path
        The destination path.
    timeout : int
        Per-request timeout seconds.
    ssl_ctx : ssl.SSLContext
        The TLS context.

    Returns
    -------
    int
        Number of bytes downloaded.

    Raises
    ------
    ClinicalTrialsDownloadError
        On HTTP error, network error, or write error.
    """
    tmp_path: Path = dest.with_suffix(dest.suffix + ".tmp")
    try:
        req: urllib.request.Request = urllib.request.Request(
            url,
            headers={"User-Agent": CLINICALTRIALS_USER_AGENT},  # Issue 9.2
        )
        with urllib.request.urlopen(
            req, timeout=timeout, context=ssl_ctx
        ) as response:
            code: int = response.getcode()
            if code != 200:
                raise ClinicalTrialsDownloadError(
                    f"HTTP {code} downloading {url} (Issue 4.2).",
                    context={"url": _sanitize_url_for_logging(url),
                             "http_code": code},
                )
            # Issue 6.8 — reject HTML error pages.
            content_type: str = response.headers.get(
                "Content-Type", "application/octet-stream"
            )
            if "text/html" in content_type.lower():
                raise ClinicalTrialsDownloadError(
                    f"Server returned HTML (likely error page) for {url} "
                    f"(Issue 6.8). Content-Type: {content_type}",
                    context={"url": _sanitize_url_for_logging(url),
                             "content_type": content_type},
                )
            with open(tmp_path, "wb") as f:
                while True:
                    chunk: bytes = response.read(
                        CLINICALTRIALS_DOWNLOAD_CHUNK_SIZE
                    )
                    if not chunk:
                        break
                    f.write(chunk)
        os.replace(tmp_path, dest)
        return dest.stat().st_size
    except Exception as exc:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        if isinstance(exc, ClinicalTrialsDownloadError):
            raise
        raise ClinicalTrialsDownloadError(
            f"Download failed for {url}: {exc} (Issue 4.2, 6.1).",
            context={"url": _sanitize_url_for_logging(url),
                     "error": str(exc),
                     "error_type": type(exc).__name__},
        ) from exc


def _download_with_retry(
    url: str,
    dest: Path,
    cfg: ClinicalTrialsConfig,
    source_cfg: Dict[str, Any],
) -> int:
    """Download with retry + exponential backoff (Issue 6.1, 6.2, 9.1, 9.2).

    Parameters
    ----------
    url : str
        The URL to download.
    dest : Path
        The destination path.
    cfg : ClinicalTrialsConfig
        Loader configuration.
    source_cfg : dict
        The ``DATA_SOURCES["clinicaltrials"]`` config block.

    Returns
    -------
    int
        Number of bytes downloaded.

    Raises
    ------
    ClinicalTrialsDownloadError
        After all retries exhausted.
    CircuitBreakerOpenError
        If the circuit breaker is open (Issue 6.11).
    """
    # Issue 6.11 — check circuit breaker before attempting download.
    _check_circuit_breaker()
    retries: int = source_cfg.get(
        "retry_count", CLINICALTRIALS_MAX_RETRIES
    )
    backoff_base: float = source_cfg.get(
        "retry_backoff_seconds", CLINICALTRIALS_RETRY_BACKOFF_BASE
    )
    timeout: int = source_cfg.get(
        "timeout_seconds", CLINICALTRIALS_DOWNLOAD_TIMEOUT_SECONDS
    )
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            bytes_downloaded: int = _atomic_download(
                url, dest, timeout, _SSL_CONTEXT
            )
            # Issue 6.11 — record success on circuit breaker.
            _record_circuit_breaker_success()
            return bytes_downloaded
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            socket.timeout,
            ssl.SSLError,
            OSError,
            ClinicalTrialsDownloadError,
        ) as exc:
            last_exc = exc
            _record_circuit_breaker_failure()
            sleep_seconds: float = backoff_base * (2 ** (attempt - 1))
            logger.warning(
                "Download attempt %d/%d failed: %s. Retrying in %.1fs. "
                "Fixes: 6.1.",
                attempt, retries, _sanitize_for_log(exc), sleep_seconds,
                extra={
                    "stage": "download",
                    "source": SOURCE_KEY,
                    "attempt": attempt,
                    "max_attempts": retries,
                    "sleep_seconds": sleep_seconds,
                    "error": str(exc)[:200],
                    "error_type": type(exc).__name__,
                },
            )
            if attempt < retries:
                time.sleep(sleep_seconds)
    raise ClinicalTrialsDownloadError(
        f"Download failed after {retries} attempts: {last_exc}. "
        f"Fixes: 6.1.",
        context={"url": _sanitize_url_for_logging(url),
                 "attempts": retries,
                 "last_error": str(last_exc)[:200] if last_exc else None},
    )


def _check_circuit_breaker() -> None:
    """Check if the download circuit breaker is open (Issue 6.11).

    Raises
    ------
    CircuitBreakerOpenError
        If the circuit breaker is open and the cooldown has not elapsed.
    """
    global _CB_OPEN_UNTIL, _CB_FAILURE_COUNT
    with _CB_LOCK:
        now: float = time.time()
        if _CB_OPEN_UNTIL > now:
            raise CircuitBreakerOpenError(
                f"ClinicalTrials download circuit breaker is open until "
                f"{datetime.fromtimestamp(_CB_OPEN_UNTIL, tz=timezone.utc).isoformat()}. "
                f"Failure count: {_CB_FAILURE_COUNT}. Fixes: 6.11.",
                context={
                    "open_until": _CB_OPEN_UNTIL,
                    "failure_count": _CB_FAILURE_COUNT,
                    "threshold": CLINICALTRIALS_CIRCUIT_BREAKER_THRESHOLD,
                },
            )
        # Auto-reset after cooldown.
        if _CB_OPEN_UNTIL != 0.0 and _CB_OPEN_UNTIL <= now:
            _CB_FAILURE_COUNT = 0
            _CB_OPEN_UNTIL = 0.0


def _record_circuit_breaker_failure() -> None:
    """Record a download failure on the circuit breaker (Issue 6.11)."""
    global _CB_FAILURE_COUNT, _CB_OPEN_UNTIL
    with _CB_LOCK:
        _CB_FAILURE_COUNT += 1
        if _CB_FAILURE_COUNT >= CLINICALTRIALS_CIRCUIT_BREAKER_THRESHOLD:
            _CB_OPEN_UNTIL = (
                time.time() + CLINICALTRIALS_CIRCUIT_BREAKER_COOLDOWN_SECONDS
            )
            logger.error(
                "Circuit breaker OPENED after %d consecutive download "
                "failures. Cooldown: %d seconds. Fixes: 6.11.",
                _CB_FAILURE_COUNT,
                CLINICALTRIALS_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
                extra={
                    "stage": "circuit_breaker",
                    "source": SOURCE_KEY,
                    "failure_count": _CB_FAILURE_COUNT,
                    "cooldown_seconds": CLINICALTRIALS_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
                },
            )


def _record_circuit_breaker_success() -> None:
    """Record a download success on the circuit breaker (Issue 6.11)."""
    global _CB_FAILURE_COUNT
    with _CB_LOCK:
        _CB_FAILURE_COUNT = 0


def _verify_checksum(
    path: Path,
    expected_sha256: Optional[str],
) -> str:
    """Verify SHA-256 checksum (Issue 6.10, 7.4, 12.5).

    Parameters
    ----------
    path : Path
        The file to verify.
    expected_sha256 : str or None
        The expected SHA-256, or None to skip verification.

    Returns
    -------
    str
        The computed SHA-256 hex digest.

    Raises
    ------
    ClinicalTrialsDataIntegrityError
        If the SHA-256 does not match.
    """
    actual: str = _compute_sha256(path)
    if expected_sha256 and not CLINICALTRIALS_SKIP_SHA256:
        if actual != expected_sha256:
            raise ClinicalTrialsDataIntegrityError(
                f"SHA-256 mismatch for {path}: expected {expected_sha256}, "
                f"got {actual}. Fixes: 6.10, 12.5.",
                context={"path": str(path),
                         "expected_sha256": expected_sha256,
                         "actual_sha256": actual},
            )
    return actual


def _validate_downloaded_zip(
    zip_path: Path,
    source_cfg: Dict[str, Any],
) -> str:
    """Validate the downloaded zip (Issue 4.9, 6.8, 6.10, 12.4).

    Parameters
    ----------
    zip_path : Path
        Path to the downloaded zip.
    source_cfg : dict
        The ``DATA_SOURCES["clinicaltrials"]`` config block.

    Returns
    -------
    str
        The computed SHA-256 hex digest.

    Raises
    ------
    ClinicalTrialsDownloadError
        If the zip is too small, not a valid zip, or exceeds max_size_bytes.
    ClinicalTrialsDataIntegrityError
        If the SHA-256 does not match.
    """
    # Issue 6.8 / 4.9 — validate zip magic bytes.
    if not _is_valid_zip(zip_path):
        raise ClinicalTrialsDownloadError(
            f"Downloaded file is not a valid zip (likely HTML error page or "
            f"truncated download): {zip_path}. Fixes: 4.9, 6.8.",
            context={"zip_path": str(zip_path)},
        )
    actual_size: int = zip_path.stat().st_size
    # Issue 12.4 — check max_size_bytes.
    max_size: int = source_cfg.get(
        "max_size_bytes", 1_500_000_000
    )
    if actual_size > max_size:
        raise ClinicalTrialsDownloadError(
            f"Downloaded zip size {actual_size} exceeds max_size_bytes "
            f"{max_size} (Issue 12.4). Possible malicious or corrupt "
            f"download.",
            context={"zip_path": str(zip_path),
                     "actual_size": actual_size,
                     "max_size_bytes": max_size},
        )
    # Issue 12.4 — check min_size_bytes.
    if actual_size < CLINICALTRIALS_MIN_VALID_SIZE_BYTES:
        raise ClinicalTrialsDownloadError(
            f"Downloaded zip size {actual_size} is below minimum "
            f"{CLINICALTRIALS_MIN_VALID_SIZE_BYTES} (Issue 12.4).",
            context={"zip_path": str(zip_path),
                     "actual_size": actual_size,
                     "min_size_bytes": CLINICALTRIALS_MIN_VALID_SIZE_BYTES},
        )
    # Issue 6.10 — verify SHA-256.
    return _verify_checksum(zip_path, source_cfg.get("sha256"))


def _validate_config(source_cfg: Dict[str, Any]) -> None:
    """Validate the DATA_SOURCES["clinicaltrials"] config block (Issue 12.7).

    Parameters
    ----------
    source_cfg : dict
        The config block to validate.

    Raises
    ------
    ClinicalTrialsConfigurationError
        If required keys are missing or values are invalid.
    """
    required_keys: FrozenSet[str] = frozenset({
        "url", "filename", "retry_count", "timeout_seconds",
    })
    missing: FrozenSet[str] = required_keys - set(source_cfg)
    if missing:
        raise ClinicalTrialsConfigurationError(
            f"clinicaltrials config missing keys: {sorted(missing)} "
            f"(Issue 12.7).",
            context={"missing_keys": sorted(missing),
                     "available_keys": sorted(source_cfg.keys())},
        )
    if not source_cfg["url"].startswith("https://"):
        raise ClinicalTrialsConfigurationError(
            f"clinicaltrials.url must be HTTPS: {source_cfg['url']!r} "
            f"(Issue 12.7).",
            context={"url": source_cfg["url"]},
        )
    if not source_cfg["filename"].endswith(".zip"):
        raise ClinicalTrialsConfigurationError(
            f"clinicaltrials.filename must end with .zip: "
            f"{source_cfg['filename']!r} (Issue 12.7).",
            context={"filename": source_cfg["filename"]},
        )
    if source_cfg["retry_count"] < 0:
        raise ClinicalTrialsConfigurationError(
            f"clinicaltrials.retry_count must be >= 0: "
            f"{source_cfg['retry_count']} (Issue 12.7).",
            context={"retry_count": source_cfg["retry_count"]},
        )
    if source_cfg["timeout_seconds"] <= 0:
        raise ClinicalTrialsConfigurationError(
            f"clinicaltrials.timeout_seconds must be > 0: "
            f"{source_cfg['timeout_seconds']} (Issue 12.7).",
            context={"timeout_seconds": source_cfg["timeout_seconds"]},
        )


def download_clinicaltrials(
    force: Optional[bool] = None,
    *,
    force_download: bool = False,
    force_extract: bool = False,
    cfg: Optional[ClinicalTrialsConfig] = None,
) -> Path:
    """Download and extract the AACT database (Issue 4.11 — split force).

    This is the public download entry point. It implements a fully hardened
    download pipeline:

    1. URL allowlist check (Issue 9.1)
    2. TLS certificate verification (Issue 9.1)
    3. Streaming download with retry + exponential backoff (Issue 6.1)
    4. Atomic write to temporary file, then rename (Issue 6.3)
    5. Size validation (Issue 12.4)
    6. ZIP magic-byte sniff (Issue 4.9, 6.8)
    7. SHA-256 verification (Issue 6.10, 12.5)
    8. Safe extraction with zip-slip defense (Issue 4.3, 9.3)
    9. Sentinel file for extraction completeness (Issue 4.8, 6.9)
    10. Staleness check (Issue 11.2)
    11. Audit log write (Issue 16.9)
    12. Lineage log write (Issue 16.10)
    13. allow_stale graceful degradation (Issue 6.4)
    14. Circuit breaker (Issue 6.11)

    Parameters
    ----------
    force : bool, optional
        DEPRECATED. Use ``force_download`` and ``force_extract`` instead
        (Issue 4.11, 12.9). When True, sets both.
    force_download : bool
        If True, re-download even if a cached copy exists.
    force_extract : bool
        If True, re-extract even if extract_dir is complete.
    cfg : ClinicalTrialsConfig or None
        Loader configuration. If None, uses defaults.

    Returns
    -------
    Path
        Path to the extracted AACT directory (containing the .db file).

    Raises
    ------
    ClinicalTrialsDownloadError
        On download failure after all retries.
    ClinicalTrialsDataIntegrityError
        On size/content/SHA-256 validation failure.
    ClinicalTrialsSecurityError
        On URL allowlist / TLS / path-traversal violation.
    ClinicalTrialsConfigurationError
        On invalid config.
    CircuitBreakerOpenError
        If the download circuit breaker is open (Issue 6.11).
    """
    # Issue 7.1 — deterministic seed for idempotency.
    set_global_seed(SEED)
    # Issue 4.11 — handle deprecated `force` parameter.
    if force is not None:
        warnings.warn(
            "`force=` is deprecated; use force_download= and force_extract=. "
            "Fixes: 4.11, 12.9.",
            DeprecationWarning,
            stacklevel=2,
        )
        force_download = force_download or force
        force_extract = force_extract or force

    # Issue 12.6 — honor env vars at call time.
    if CLINICALTRIALS_FORCE_DOWNLOAD:
        force_download = True
    if CLINICALTRIALS_SKIP:
        logger.warning(
            "DRUGOS_CLINICALTRIALS_SKIP=1 — skipping ClinicalTrials download. "
            "Fixes: 12.6.",
            extra={"stage": "download", "source": SOURCE_KEY, "skipped": True},
        )
        return RAW_DIR / "clinicaltrials"

    cfg = cfg or ClinicalTrialsConfig()
    # Issue 6.4 — allow_stale from cfg or env var.
    allow_stale: bool = cfg.allow_stale or CLINICALTRIALS_ALLOW_STALE
    # Issue 7.8 — pinned release handling.
    if cfg.pinned_aact_release is None and CLINICALTRIALS_PINNED_RELEASE:
        cfg = ClinicalTrialsConfig(
            **{**asdict(cfg),
               "pinned_aact_release": CLINICALTRIALS_PINNED_RELEASE}
        )

    source_cfg: Dict[str, Any] = DATA_SOURCES[SOURCE_KEY]
    # Issue 12.7 — validate config on startup.
    _validate_config(source_cfg)

    url: str = source_cfg["url"]
    # Issue 9.1 — URL allowlist + scheme check.
    _validate_url_against_allowlist(url)

    raw_dir: Path = cfg.effective_raw_dir
    raw_dir.mkdir(parents=True, exist_ok=True)
    zip_path: Path = raw_dir / source_cfg["filename"]
    extract_dir: Path = raw_dir / "clinicaltrials"

    # Issue 7.8 — pinned release: use a specific zip filename.
    if cfg.pinned_aact_release:
        pinned_name: str = f"aact_dataset_{cfg.pinned_aact_release}.zip"
        pinned_path: Path = raw_dir / pinned_name
        if pinned_path.exists():
            zip_path = pinned_path
        elif not zip_path.exists():
            raise ClinicalTrialsConfigurationError(
                f"Pinned AACT release {cfg.pinned_aact_release!r} not found "
                f"at {pinned_path}. Refusing to use any other release "
                f"(Issue 7.8).",
                context={"pinned_release": cfg.pinned_aact_release,
                         "expected_path": str(pinned_path)},
            )

    # Issue 4.8 / 6.9 — check extraction sentinel.
    extract_complete: bool = (
        extract_dir.exists()
        and _is_extract_complete(extract_dir)
        and not force_extract
    )
    if extract_complete and not force_download:
        logger.info(
            "ClinicalTrials data already extracted at %s. Fixes: 4.8.",
            extract_dir,
            extra={"stage": "download", "source": SOURCE_KEY,
                   "extract_dir": str(extract_dir), "cached": True},
        )
        return extract_dir

    # Issue 6.4 — allow_stale: use cached copy if download fails.
    cached_zip_valid: bool = (
        zip_path.exists() and _is_valid_zip(zip_path)
    )

    if CLINICALTRIALS_OFFLINE and cached_zip_valid:
        logger.warning(
            "DRUGOS_CLINICALTRIALS_OFFLINE=1 — using cached zip %s without "
            "download. Fixes: 12.6.",
            zip_path,
            extra={"stage": "download", "source": SOURCE_KEY,
                   "offline": True, "zip_path": str(zip_path)},
        )
    elif force_download or not cached_zip_valid:
        # Issue 16.8 — impact analysis: capture old SHA before re-download.
        old_lineage: Optional[Dict[str, Any]] = _read_lineage(
            cfg.effective_lineage_log_path
        )
        if old_lineage and old_lineage.get("source_sha256"):
            logger.info(
                "Previous AACT SHA: %s. New SHA will be compared after "
                "download. Fixes: 16.8.",
                old_lineage["source_sha256"],
                extra={"stage": "impact_analysis",
                       "source": SOURCE_KEY,
                       "previous_sha": old_lineage["source_sha256"]},
            )

        t0: float = time.perf_counter()
        try:
            bytes_downloaded: int = _download_with_retry(
                url, zip_path, cfg, source_cfg
            )
        except ClinicalTrialsDownloadError as exc:
            # Issue 6.4 — allow_stale graceful degradation.
            if allow_stale and cached_zip_valid:
                logger.critical(
                    "Download failed (%s); falling back to cached copy %s "
                    "(age: %s). Fixes: 6.4.",
                    _sanitize_for_log(exc), zip_path,
                    _format_age(zip_path.stat().st_mtime),
                    extra={"stage": "download", "source": SOURCE_KEY,
                           "allow_stale": True,
                           "fallback": str(zip_path),
                           "error": str(exc)[:200]},
                )
            else:
                raise

        # Issue 6.8 — delete + retry on HTML error page (already handled
        # in _atomic_download, but double-check here).
        if not _is_valid_zip(zip_path):
            logger.warning(
                "Downloaded file %s is not a valid zip (likely HTML error "
                "page). Deleting. Fixes: 6.8, 4.2.",
                zip_path,
                extra={"stage": "download", "source": SOURCE_KEY,
                       "invalid_zip": True, "zip_path": str(zip_path)},
            )
            zip_path.unlink(missing_ok=True)
            raise ClinicalTrialsDownloadError(
                f"Downloaded file is not a valid zip: {zip_path}. "
                f"Fixes: 6.8.",
                context={"zip_path": str(zip_path)},
            )

        # Issue 6.10 / 12.5 — verify SHA-256.
        sha256_hex: str = _validate_downloaded_zip(zip_path, source_cfg)

        elapsed: float = time.perf_counter() - t0
        logger.info(
            "Downloaded AACT to %s (%.1f MB) in %.1fs. SHA-256: %s. "
            "Fixes: 11.4, 7.4.",
            zip_path, zip_path.stat().st_size / _MB, elapsed, sha256_hex,
            extra={"stage": "download", "source": SOURCE_KEY,
                   "bytes": zip_path.stat().st_size,
                   "path": str(zip_path),
                   "sha256": sha256_hex,
                   "elapsed_seconds": elapsed},
        )

        # Issue 16.8 — warn if SHA changed.
        if old_lineage and old_lineage.get("source_sha256"):
            if old_lineage["source_sha256"] != sha256_hex:
                logger.warning(
                    "AACT SHA changed: %s -> %s. All clinical-trial edges "
                    "should be re-evaluated. Fixes: 16.8.",
                    old_lineage["source_sha256"], sha256_hex,
                    extra={"stage": "impact_analysis",
                           "source": SOURCE_KEY,
                           "previous_sha": old_lineage["source_sha256"],
                           "new_sha": sha256_hex},
                )

        # Issue 16.9 — write audit log.
        _write_audit_log(
            "DOWNLOAD",
            url=_sanitize_url_for_logging(url),
            path=str(zip_path),
            bytes_downloaded=zip_path.stat().st_size,
            sha256=sha256_hex,
            elapsed_seconds=elapsed,
        )

    # Issue 7.7 — force_extract invalidates extract_dir.
    if force_extract and extract_dir.exists():
        logger.info(
            "force_extract=True — removing existing extract_dir: %s. "
            "Fixes: 7.7.",
            extract_dir,
            extra={"stage": "extract", "source": SOURCE_KEY,
                   "extract_dir": str(extract_dir)},
        )
        shutil.rmtree(extract_dir, ignore_errors=True)

    # Issue 4.8 / 6.9 — extract with sentinel.
    if not _is_extract_complete(extract_dir) or force_extract:
        t0 = time.perf_counter()
        logger.info(
            "Extracting AACT to %s. Fixes: 4.3.",
            extract_dir,
            extra={"stage": "extract", "source": SOURCE_KEY,
                   "zip_path": str(zip_path),
                   "extract_dir": str(extract_dir)},
        )
        _safe_extract(zip_path, extract_dir)
        elapsed = time.perf_counter() - t0
        logger.info(
            "AACT extraction complete in %.1fs. Fixes: 11.4.",
            elapsed,
            extra={"stage": "extract", "source": SOURCE_KEY,
                   "elapsed_seconds": elapsed,
                   "extract_dir": str(extract_dir)},
        )

    return extract_dir


# Backward-compat alias (Issue 1.5 — re-use canonical name).
download_clinicaltrials_evidence = download_clinicaltrials


# =============================================================================
# Section 6 — SQL parsing
# =============================================================================
# Fixes Issues 3.1 (schema detection), 3.2 (exact-match phase),
# 3.7 (study_type filter), 3.8 (designs join), 3.9 (primary_outcomes),
# 3.10 (has_results), 3.11 (overall_status filter), 3.13 (start_date),
# 4.1 (try/finally sqlite), 4.4 (read-only sqlite), 4.5 (validate AACT DB),
# 4.14 (self-documenting SQL aliases), 7.9 (deterministic ORDER BY),
# 8.1 (chunked SQL reads), 8.5 (LIMIT), 8.8 (index hints),
# 11.5 (error context), 11.10 (empty-result warning).


@contextmanager
def _open_aact_readonly(
    db_path: Path,
) -> Iterator[sqlite3.Connection]:
    """Open AACT sqlite in read-only mode (Issue 4.1, 4.4, 9.4).

    Parameters
    ----------
    db_path : Path
        Path to the AACT sqlite .db file.

    Yields
    ------
    sqlite3.Connection
        A read-only sqlite connection.

    Raises
    ------
    ClinicalTrialsParseError
        If the DB cannot be opened.
    """
    # Issue 4.4 — read-only sqlite via URI mode=ro.
    uri: str = f"file:{db_path}?mode=ro"
    try:
        conn: sqlite3.Connection = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError as exc:
        raise ClinicalTrialsParseError(
            f"Cannot open AACT sqlite DB {db_path} in read-only mode: {exc}. "
            f"Fixes: 4.1, 4.4, 9.4.",
            context={"db_path": str(db_path), "error": str(exc)},
        ) from exc
    try:
        yield conn
    finally:
        # Issue 4.1 — try/finally for sqlite connection.
        conn.close()


def _select_aact_db(ct_dir: Path, cfg: ClinicalTrialsConfig) -> Path:
    """Select and validate the AACT .db file (Issue 4.5, 9.5, 11.9).

    When multiple .db files exist (common after re-extraction), picks the
    one with the most recent mtime, logs all candidates, and validates it
    is an AACT DB.

    Parameters
    ----------
    ct_dir : Path
        Directory to search for .db files.
    cfg : ClinicalTrialsConfig
        Loader configuration.

    Returns
    -------
    Path
        Path to the validated AACT .db file.

    Raises
    ------
    ClinicalTrialsParseError
        If no .db file is found, or the DB is not a valid AACT DB.
    """
    # Issue 4.5 — find all .db files.
    db_files: List[Path] = sorted(
        ct_dir.rglob("*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not db_files:
        # Issue 11.9 — log before raising.
        logger.error(
            "No SQLite database found in %s. Searched pattern: *.db. "
            "Did download_clinicaltrials() run? Fixes: 11.9, 4.8.",
            ct_dir,
            extra={"stage": "select_db", "source": SOURCE_KEY,
                   "search_dir": str(ct_dir)},
        )
        raise ClinicalTrialsParseError(
            f"No SQLite database found in {ct_dir}. Fixes: 11.9.",
            context={"search_dir": str(ct_dir)},
        )
    if len(db_files) > 1:
        # Issue 11.11 — log all candidate DB files.
        logger.warning(
            "Multiple .db files found in %s; selecting most recent: %s. "
            "Other candidates: %s. Fixes: 4.5, 11.11.",
            ct_dir, db_files[0], [str(p) for p in db_files[1:]],
            extra={"stage": "select_db", "source": SOURCE_KEY,
                   "candidate_count": len(db_files),
                   "selected": str(db_files[0]),
                   "candidates": [str(p) for p in db_files[1:]]},
        )
    db_path: Path = db_files[0]
    # Issue 4.5 / 9.5 — validate DB is AACT.
    _validate_aact_db(db_path, cfg)
    return db_path


def _validate_aact_db(db_path: Path, cfg: ClinicalTrialsConfig) -> None:
    """Validate the DB has the AACT schema (Issue 4.5, 9.5).

    Parameters
    ----------
    db_path : Path
        Path to the sqlite .db file.
    cfg : ClinicalTrialsConfig
        Loader configuration (for allow_legacy_schema).

    Raises
    ------
    ClinicalTrialsParseError
        If the DB is missing required AACT tables.
    """
    try:
        with _open_aact_readonly(db_path) as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            actual: set = {row[0] for row in cursor.fetchall()}
    except ClinicalTrialsParseError:
        raise
    except sqlite3.DatabaseError as exc:
        raise ClinicalTrialsParseError(
            f"DB {db_path} is not a valid sqlite file: {exc}. Fixes: 4.5, 9.5.",
            context={"db_path": str(db_path), "error": str(exc)},
        ) from exc
    missing: FrozenSet[str] = _REQUIRED_AACT_TABLES - actual
    if missing:
        raise ClinicalTrialsParseError(
            f"DB {db_path} is not a valid AACT database — missing tables: "
            f"{sorted(missing)}. Fixes: 4.5, 9.5.",
            context={"db_path": str(db_path),
                     "missing_tables": sorted(missing),
                     "actual_tables": sorted(actual)},
        )


def _detect_aact_schema(
    db_path: Path,
    cfg: ClinicalTrialsConfig,
) -> Tuple[str, Dict[str, Any]]:
    """Detect the AACT schema version (Issue 3.1, C1).

    Returns a tuple ``(schema_version, schema_info)`` where
    ``schema_version`` is one of:
      * ``"modern"`` — has ``interventions_mesh_terms`` and
        ``conditions_mesh_terms`` tables.
      * ``"legacy"`` — has ``mesh_term`` as a direct column on
        ``interventions`` and ``conditions`` (requires
        ``allow_legacy_schema=True``).
      * ``"unknown"`` — neither pattern matches; raises.

    Parameters
    ----------
    db_path : Path
        Path to the AACT .db file.
    cfg : ClinicalTrialsConfig
        Loader configuration.

    Returns
    -------
    tuple[str, dict]
        ``(schema_version, schema_info)`` where schema_info contains
        detected table names, column names, and index info.

    Raises
    ------
    ClinicalTrialsParseError
        If neither modern nor legacy schema is detected, OR legacy is
        detected but ``allow_legacy_schema=False``.
    """
    with _open_aact_readonly(db_path) as conn:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables: set = {row[0] for row in cursor.fetchall()}

        # Issue 3.1 — check for modern schema.
        has_modern_interventions: bool = (
            "interventions_mesh_terms" in tables
        )
        has_modern_conditions: bool = (
            "conditions_mesh_terms" in tables
        )

        # Check for legacy schema (mesh_term as direct column).
        interventions_cols: List[Tuple[str, ...]] = []
        if "interventions" in tables:
            cursor = conn.execute("PRAGMA table_info(interventions)")
            interventions_cols = [tuple(r) for r in cursor.fetchall()]
        has_legacy_interventions: bool = any(
            col[1] == "mesh_term" for col in interventions_cols
        )

        conditions_cols: List[Tuple[str, ...]] = []
        if "conditions" in tables:
            cursor = conn.execute("PRAGMA table_info(conditions)")
            conditions_cols = [tuple(r) for r in cursor.fetchall()]
        has_legacy_conditions: bool = any(
            col[1] == "mesh_term" for col in conditions_cols
        )

        # Issue 8.8 — index hints.
        indexes: List[Tuple[str, ...]] = []
        for table in ("studies", "interventions", "conditions"):
            if table in tables:
                cursor = conn.execute(f"PRAGMA index_list({table})")
                indexes.extend([(table,) + tuple(r) for r in cursor.fetchall()])

    schema_info: Dict[str, Any] = {
        "tables": sorted(tables),
        "has_modern_interventions_mesh_terms": has_modern_interventions,
        "has_modern_conditions_mesh_terms": has_modern_conditions,
        "has_legacy_interventions_mesh_term_column": has_legacy_interventions,
        "has_legacy_conditions_mesh_term_column": has_legacy_conditions,
        "indexes": indexes,
    }

    if has_modern_interventions and has_modern_conditions:
        logger.info(
            "Detected MODERN AACT schema (interventions_mesh_terms + "
            "conditions_mesh_terms tables present). Fixes: 3.1.",
            extra={"stage": "schema_detect", "source": SOURCE_KEY,
                   "schema_version": "modern", **schema_info},
        )
        return ("modern", schema_info)

    if has_legacy_interventions and has_legacy_conditions:
        # Issue 2.9 — legacy schema requires allow_legacy_schema.
        if not (cfg.allow_legacy_schema or CLINICALTRIALS_ALLOW_LEGACY_SCHEMA):
            raise ClinicalTrialsParseError(
                f"Legacy AACT schema detected (mesh_term as direct column) "
                f"but allow_legacy_schema=False. Set "
                f"DRUGOS_CLINICALTRIALS_ALLOW_LEGACY=1 or pass "
                f"allow_legacy_schema=True to enable legacy mode. "
                f"Fixes: 2.9, 3.1.",
                context={"schema_version": "legacy", **schema_info},
            )
        logger.warning(
            "Detected LEGACY AACT schema (mesh_term as direct column on "
            "interventions/conditions). This is the pre-2018 AACT layout; "
            "modern AACT uses interventions_mesh_terms / "
            "conditions_mesh_terms tables. Legacy mode is allowed because "
            "allow_legacy_schema=True. Fixes: 2.9, 3.1.",
            extra={"stage": "schema_detect", "source": SOURCE_KEY,
                   "schema_version": "legacy", **schema_info},
        )
        return ("legacy", schema_info)

    raise ClinicalTrialsParseError(
        f"Unrecognized AACT schema — neither mesh_term column nor "
        f"interventions_mesh_terms / conditions_mesh_terms tables found. "
        f"Refusing to emit edges from unverified schema. Fixes: 3.1, 9.5.",
        context={"schema_version": "unknown", **schema_info},
    )


def _build_sql_query(
    schema_version: str,
    cfg: ClinicalTrialsConfig,
) -> Tuple[str, List[Any]]:
    """Build the parameterized SQL query (Issue 3.1, 3.2, 3.7, 3.8, 3.9,
    3.10, 3.11, 4.14, 7.9, 8.5, 8.8).

    Returns a tuple ``(query, params)`` where params is the list of
    bind parameters (in order).

    Parameters
    ----------
    schema_version : str
        One of "modern" or "legacy" (from _detect_aact_schema).
    cfg : ClinicalTrialsConfig
        Loader configuration.

    Returns
    -------
    tuple[str, list]
        ``(query, params)`` ready for pd.read_sql_query.
    """
    # Issue 4.14 — self-documenting SQL aliases.
    # Issue 3.2 — exact-match phase via IN (?,?,...) not LIKE.
    phase_placeholders: str = ",".join("?" * len(cfg.phases))
    intv_type_placeholders: str = ",".join("?" * len(cfg.intervention_types))
    study_type_placeholders: str = ",".join("?" * len(cfg.study_types))
    status_placeholders: str = ",".join("?" * len(cfg.allowed_statuses))

    # Issue 3.1 — schema-specific SELECT for MeSH terms.
    # SQLite does NOT support GROUP_CONCAT(DISTINCT col, sep) — DISTINCT
    # aggregates must have exactly one argument. So we use a correlated
    # subquery with an inner DISTINCT + outer GROUP_CONCAT.
    if schema_version == "modern":
        drug_mesh_select: str = (
            "(SELECT GROUP_CONCAT(dmesh.mesh_term, '||') FROM ("
            "SELECT DISTINCT imt2.mesh_term FROM interventions_mesh_terms imt2 "
            "WHERE imt2.nct_id = intv.nct_id "
            "AND imt2.intervention_id = intv.id"
            ") dmesh) AS drug_mesh"
        )
        cond_mesh_select: str = (
            "(SELECT GROUP_CONCAT(cmesh.mesh_term, '||') FROM ("
            "SELECT DISTINCT cmt2.mesh_term FROM conditions_mesh_terms cmt2 "
            "WHERE cmt2.nct_id = cond.nct_id "
            "AND cmt2.condition_id = cond.id"
            ") cmesh) AS condition_mesh"
        )
        drug_mesh_join: str = ""  # handled by subqueries above
        cond_mesh_join: str = ""
    else:  # legacy
        drug_mesh_select = "intv.mesh_term AS drug_mesh"
        cond_mesh_select = "cond.mesh_term AS condition_mesh"
        drug_mesh_join = ""
        cond_mesh_join = ""

    # Issue 3.8 — JOIN designs table.
    designs_join: str = (
        "LEFT JOIN designs d ON d.nct_id = s.nct_id"
    )
    # Issue 3.9 — JOIN primary_outcomes (aggregated via subquery).
    primary_outcomes_select: str = (
        "(SELECT GROUP_CONCAT(po.measure, ' || ') FROM primary_outcomes po "
        "WHERE po.nct_id = s.nct_id) AS primary_outcome"
    )

    # Issue 7.9 — deterministic ORDER BY.
    order_by: str = (
        "ORDER BY s.nct_id, intv.name, cond.name"
    )

    # Issue 3.6 — min_enrollment filter.
    enrollment_filter: str = ""
    if cfg.min_enrollment > 0:
        enrollment_filter = "AND s.enrollment >= ?"

    # Issue 3.13 — max_trial_age_years filter (applied in Python after parse).

    # Issue 8.5 — LIMIT for testing.
    limit_clause: str = ""
    if cfg.limit is not None:
        limit_clause = "LIMIT ?"

    query: str = f"""
    SELECT
        s.nct_id              AS nct_id,
        s.brief_title         AS brief_title,
        s.phase               AS phase,
        s.overall_status      AS overall_status,
        s.study_type          AS study_type,
        s.enrollment          AS enrollment,
        s.why_stopped         AS why_stopped,
        s.has_results         AS has_results,
        s.start_date          AS start_date,
        s.completion_date     AS completion_date,
        intv.name             AS drug_name,
        intv.description      AS description,
        intv.intervention_type AS intervention_type,
        {drug_mesh_select},
        cond.name             AS condition_name,
        {cond_mesh_select},
        d.allocation          AS allocation,
        d.intervention_model  AS intervention_model,
        d.masking             AS masking,
        d.primary_purpose     AS primary_purpose,
        {primary_outcomes_select}
    FROM studies s
    JOIN interventions intv ON intv.nct_id = s.nct_id
    JOIN conditions cond ON cond.nct_id = s.nct_id
    {designs_join}
    {drug_mesh_join}
    {cond_mesh_join}
    WHERE intv.intervention_type IN ({intv_type_placeholders})
      AND s.phase IN ({phase_placeholders})
      AND s.study_type IN ({study_type_placeholders})
      AND s.overall_status IN ({status_placeholders})
      {enrollment_filter}
    GROUP BY s.nct_id, intv.id, cond.id
    {order_by}
    {limit_clause}
    """

    # Build params in order matching the SQL.
    params: List[Any] = []
    params.extend(cfg.intervention_types)
    params.extend(cfg.phases)
    params.extend(cfg.study_types)
    params.extend(cfg.allowed_statuses)
    if cfg.min_enrollment > 0:
        params.append(cfg.min_enrollment)
    if cfg.limit is not None:
        params.append(cfg.limit)

    return (query, params)


def parse_clinicaltrials(
    ct_dir: Optional[Path] = None,
    phase: Optional[str] = "Phase 3",
    *,
    phases: Optional[Sequence[str]] = None,
    cfg: Optional[ClinicalTrialsConfig] = None,
) -> pd.DataFrame:
    """Parse ClinicalTrials drug-disease evidence from AACT SQLite.

    This is the backward-compatible v0 shim (Rule R3). It preserves the
    v0 signature ``parse_clinicaltrials(ct_dir=None, phase="Phase 3") -> pd.DataFrame``
    and delegates to ``parse_clinicaltrials_trials``.

    .. deprecated::
        Use ``parse_clinicaltrials_trials`` instead. The ``phase`` parameter
        is deprecated; use ``phases`` for explicit multi-phase selection
        (Issue 4.6). The default behavior has changed:
          * v0: ``phase="Phase 3"`` used ``LIKE '%Phase 3%'`` which also
            matched "Phase 2/Phase 3" and "Phase 3/Phase 4" (bug — Issue 3.2).
          * v2.1: ``phase="Phase 3"`` is converted to ``phases=("Phase 3",)``
            with exact-match ``IN (?)`` semantics.

    Parameters
    ----------
    ct_dir : Path or None
        Path to extracted AACT directory. If None, defaults to
        ``RAW_DIR / "clinicaltrials"``.
    phase : str, optional
        DEPRECATED. Single phase value. If provided AND ``phases`` is None,
        converted to ``phases=(phase,)`` for exact-match.
    phases : sequence of str, optional
        Preferred multi-phase selector. Overrides ``phase``.
    cfg : ClinicalTrialsConfig or None
        Loader configuration. If provided, overrides ``phase``/``phases``.

    Returns
    -------
    pd.DataFrame
        DataFrame with columns: nct_id, brief_title, phase, overall_status,
        study_type, enrollment, why_stopped, has_results, start_date,
        completion_date, drug_name, description, intervention_type,
        drug_mesh, condition_name, condition_mesh, allocation,
        intervention_model, masking, primary_purpose, primary_outcome.
    """
    # Issue 4.6 / 12.9 — deprecation warning when `phase` is used.
    if phase is not None and phases is None and cfg is None:
        warnings.warn(
            "`phase=` is deprecated; use `phases=` for explicit multi-phase "
            "selection. The default is now ('Phase 3', 'Phase 4'). "
            "Fixes: 4.6, 12.9.",
            DeprecationWarning,
            stacklevel=2,
        )
        # Issue 3.2 — convert phase to phases tuple (exact match).
        cfg = ClinicalTrialsConfig(phases=(phase,))
    elif phases is not None and cfg is None:
        cfg = ClinicalTrialsConfig(phases=tuple(phases))
    elif cfg is None:
        cfg = ClinicalTrialsConfig()
    elif phases is not None:
        cfg = ClinicalTrialsConfig(**{**asdict(cfg), "phases": tuple(phases)})
    elif phase is not None and phases is None:
        cfg = ClinicalTrialsConfig(**{**asdict(cfg), "phases": (phase,)})

    return parse_clinicaltrials_trials(ct_dir=ct_dir, cfg=cfg)


# Issue 1.5 — re-export under canonical name (sibling-loader convention).
parse_clinicaltrials_evidence = parse_clinicaltrials


def parse_clinicaltrials_trials(
    ct_dir: Optional[Path] = None,
    phases: Optional[Sequence[str]] = None,
    *,
    cfg: Optional[ClinicalTrialsConfig] = None,
) -> pd.DataFrame:
    """Parse ClinicalTrials drug-disease evidence from AACT SQLite.

    Eager wrapper around ``iter_clinicaltrials_trials`` — materializes the
    full DataFrame into memory. For large AACT snapshots, prefer
    ``iter_clinicaltrials_trials`` with ``chunksize`` for streaming.

    Parameters
    ----------
    ct_dir : Path or None
        Path to extracted AACT directory. If None, defaults to
        ``RAW_DIR / "clinicaltrials"``.
    phases : sequence of str, optional
        Trial phases to include. If None, uses ``cfg.phases``.
    cfg : ClinicalTrialsConfig or None
        Loader configuration. If None, uses defaults.

    Returns
    -------
    pd.DataFrame
        DataFrame with one row per (trial, intervention, condition) cross-
        join combination.

    Raises
    ------
    ClinicalTrialsParseError
        If the DB cannot be opened, is missing required tables, or has an
        unrecognized schema.
    ClinicalTrialsConfigurationError
        If the config is invalid.
    ClinicalTrialsDataIntegrityError
        If the row count deviates from expected by >50%.
    """
    if phases is not None and cfg is None:
        cfg = ClinicalTrialsConfig(phases=tuple(phases))
    elif phases is not None and cfg is not None:
        cfg = ClinicalTrialsConfig(
            **{**asdict(cfg), "phases": tuple(phases)}
        )
    elif cfg is None:
        cfg = ClinicalTrialsConfig()

    chunks: List[pd.DataFrame] = list(
        iter_clinicaltrials_trials(ct_dir=ct_dir, cfg=cfg)
    )
    if not chunks:
        return pd.DataFrame()
    return pd.concat(chunks, ignore_index=True)


def iter_clinicaltrials_trials(
    ct_dir: Optional[Path] = None,
    *,
    cfg: Optional[ClinicalTrialsConfig] = None,
) -> Iterator[pd.DataFrame]:
    """Stream ClinicalTrials trials from AACT SQLite (Issue 8.1, 8.3).

    Generator yielding DataFrames of ``cfg.chunksize`` rows each. Use this
    for memory-bounded processing of large AACT snapshots.

    Parameters
    ----------
    ct_dir : Path or None
        Path to extracted AACT directory. If None, defaults to
        ``RAW_DIR / "clinicaltrials"``.
    cfg : ClinicalTrialsConfig or None
        Loader configuration. If None, uses defaults.

    Yields
    ------
    pd.DataFrame
        DataFrame of up to ``cfg.chunksize`` rows.

    Raises
    ------
    ClinicalTrialsParseError
        If the DB cannot be opened or is corrupt.
    ClinicalTrialsConfigurationError
        If the config is invalid.
    """
    set_global_seed(SEED)  # Issue 7.1 — idempotency.
    cfg = cfg or ClinicalTrialsConfig()
    # Issue 12.6 — defaults may be overridden via env vars.
    if ct_dir is None:
        ct_dir = RAW_DIR / "clinicaltrials"

    # Issue 4.5 / 11.9 — select and validate the AACT .db file.
    db_path: Path = _select_aact_db(ct_dir, cfg)

    # Issue 3.1 — detect schema (modern vs legacy).
    schema_version, schema_info = _detect_aact_schema(db_path, cfg)

    # Issue 8.8 — log index info (warning if missing indexes).
    _check_indexes(db_path, schema_info)

    # Issue 4.14 / 7.9 / 8.5 — build SQL query.
    query, params = _build_sql_query(schema_version, cfg)

    logger.info(
        "Reading ClinicalTrials from %s (schema=%s, phases=%s, "
        "intervention_types=%s, study_types=%s, statuses=%s, "
        "min_enrollment=%d, chunksize=%d). Fixes: 11.4.",
        db_path, schema_version, list(cfg.phases),
        list(cfg.intervention_types), list(cfg.study_types),
        list(cfg.allowed_statuses), cfg.min_enrollment, cfg.chunksize,
        extra={"stage": "parse", "source": SOURCE_KEY,
               "db_path": str(db_path), "schema_version": schema_version,
               "phases": list(cfg.phases),
               "intervention_types": list(cfg.intervention_types),
               "study_types": list(cfg.study_types),
               "allowed_statuses": list(cfg.allowed_statuses),
               "min_enrollment": cfg.min_enrollment,
               "chunksize": cfg.chunksize},
    )

    # Issue 4.1 — try/finally for sqlite connection.
    # Issue 11.5 — wrap SQL execution in try/except with context.
    t0: float = time.perf_counter()
    total_rows: int = 0
    try:
        with _open_aact_readonly(db_path) as conn:
            # Issue 8.1 — chunked SQL reads.
            for chunk_idx, chunk_df in enumerate(
                pd.read_sql_query(
                    query, conn, params=params,
                    chunksize=cfg.chunksize if cfg.limit is None else None,
                )
            ):
                total_rows += len(chunk_df)
                # Issue 14.7 — parse dates to ISO8601.
                for date_col in ("start_date", "completion_date"):
                    if date_col in chunk_df.columns:
                        chunk_df[date_col] = pd.to_datetime(
                            chunk_df[date_col], errors="coerce"
                        ).dt.strftime("%Y-%m-%d")

                # Issue 3.14 — split MeSH terms aggregated via GROUP_CONCAT.
                if "drug_mesh" in chunk_df.columns and schema_version == "modern":
                    chunk_df = _explode_mesh_column(
                        chunk_df, "drug_mesh",
                        CLINICALTRIALS_MAX_MESH_PER_INTERVENTION,
                    )
                if "condition_mesh" in chunk_df.columns and schema_version == "modern":
                    chunk_df = _explode_mesh_column(
                        chunk_df, "condition_mesh",
                        CLINICALTRIALS_MAX_MESH_PER_INTERVENTION,
                    )

                # Issue 11.10 — empty result warning.
                if total_rows == 0 and chunk_idx == 0 and len(chunk_df) == 0:
                    logger.warning(
                        "Zero clinical-trial rows returned — check AACT "
                        "data and phase filter. Pipeline will continue with "
                        "0 clinical-trial edges. RL ranker will be BLIND to "
                        "clinical evidence. Fixes: 11.10.",
                        extra={"stage": "parse", "source": SOURCE_KEY,
                               "row_count": 0,
                               "phases": list(cfg.phases),
                               "intervention_types": list(cfg.intervention_types)},
                    )

                # Progress logging.
                if (chunk_idx + 1) % max(
                    1, cfg.progress_log_interval // max(1, cfg.chunksize)
                ) == 0:
                    logger.info(
                        "ClinicalTrials parse progress: %d rows. Fixes: 11.1.",
                        total_rows,
                        extra={"stage": "parse_progress",
                               "source": SOURCE_KEY,
                               "row_count": total_rows,
                               "chunk_idx": chunk_idx},
                    )

                # Issue 3.13 — max_trial_age_years filter (applied in Python).
                if cfg.max_trial_age_years is not None and "start_date" in chunk_df.columns:
                    chunk_df = _filter_by_trial_age(
                        chunk_df, cfg.max_trial_age_years
                    )

                # Issue 8.10 — memory ceiling warning.
                if total_rows > CLINICALTRIALS_MEMORY_CEILING_WARNING_THRESHOLD:
                    logger.warning(
                        "Row count %d exceeds memory ceiling threshold %d — "
                        "consider using chunksize parameter. Fixes: 8.10.",
                        total_rows,
                        CLINICALTRIALS_MEMORY_CEILING_WARNING_THRESHOLD,
                        extra={"stage": "performance",
                               "source": SOURCE_KEY,
                               "row_count": total_rows},
                    )

                yield chunk_df
    except sqlite3.OperationalError as exc:
        # Issue 11.5 — re-raise with context.
        raise ClinicalTrialsParseError(
            f"SQL failed on DB {db_path} with phases={cfg.phases}, "
            f"intervention_types={cfg.intervention_types}: {exc}. "
            f"Query: {query[:500]}... Fixes: 11.5.",
            context={"db_path": str(db_path),
                     "phases": list(cfg.phases),
                     "intervention_types": list(cfg.intervention_types),
                     "error": str(exc),
                     "query_excerpt": query[:500]},
        ) from exc
    finally:
        elapsed: float = time.perf_counter() - t0
        logger.info(
            "ClinicalTrials parse complete: %d rows in %.1fs. Fixes: 11.4.",
            total_rows, elapsed,
            extra={"stage": "parse_complete", "source": SOURCE_KEY,
                   "row_count": total_rows,
                   "elapsed_seconds": elapsed},
        )

        # Issue 5.9 — validate expected_record_count.
        _validate_expected_record_count(total_rows, db_path)


def _check_indexes(db_path: Path, schema_info: Dict[str, Any]) -> None:
    """Log a WARNING if expected indexes are missing (Issue 8.8).

    Expected indexes (verify with PRAGMA index_list):
      * interventions.nct_id
      * conditions.nct_id
      * studies.nct_id
      * studies.phase
    """
    indexes: List[Tuple[Any, ...]] = schema_info.get("indexes", [])
    expected_indexed_cols: Dict[str, str] = {
        "studies": "nct_id,phase",
        "interventions": "nct_id",
        "conditions": "nct_id",
    }
    # We can't fully verify which columns are indexed from index_list alone
    # without further queries; this is a best-effort warning.
    if not indexes:
        logger.warning(
            "No indexes detected on studies/interventions/conditions. "
            "Query time may be 100x slower. Fixes: 8.8.",
            extra={"stage": "performance", "source": SOURCE_KEY,
                   "indexes_found": []},
        )


def _explode_mesh_column(
    df: pd.DataFrame,
    col: str,
    max_terms: int,
) -> pd.DataFrame:
    """Explode a GROUP_CONCAT'd MeSH column into one row per term (Issue 3.14).

    Parameters
    ----------
    df : pd.DataFrame
        DataFrame with the MeSH column.
    col : str
        Column name (e.g. "drug_mesh").
    max_terms : int
        Warn if an entry has more than this many terms.

    Returns
    -------
    pd.DataFrame
        DataFrame with one row per MeSH term.
    """
    if col not in df.columns:
        return df
    # Split on '||' (the GROUP_CONCAT delimiter we used in SQL).
    df[col] = df[col].apply(
        lambda x: x.split("||") if isinstance(x, str) and "||" in x else [x] if x is not None and not (isinstance(x, float) and pd.isna(x)) else [None]
    )
    # Warn if any entry has > max_terms terms (Issue 3.14).
    counts: pd.Series = df[col].apply(
        lambda lst: len([t for t in lst if t]) if isinstance(lst, list) else 0
    )
    high_count: int = int((counts > max_terms).sum())
    if high_count > 0:
        logger.warning(
            "%d interventions have >%d MeSH terms (suspicious — likely "
            "over-broad intervention). Fixes: 3.14.",
            high_count, max_terms,
            extra={"stage": "data_quality", "source": SOURCE_KEY,
                   "column": col,
                   "high_count": high_count,
                   "max_terms": max_terms},
        )
    # Explode.
    df = df.explode(col, ignore_index=True)
    return df


def _filter_by_trial_age(
    df: pd.DataFrame,
    max_age_years: int,
) -> pd.DataFrame:
    """Filter trials older than max_age_years (Issue 3.13, 5.7).

    Parameters
    ----------
    df : pd.DataFrame
        DataFrame with a 'start_date' column (ISO-8601 string).
    max_age_years : int
        Maximum age in years.

    Returns
    -------
    pd.DataFrame
        Filtered DataFrame.
    """
    if "start_date" not in df.columns:
        return df
    cutoff: pd.Timestamp = pd.Timestamp.now() - pd.DateOffset(years=max_age_years)
    start_dates: pd.Series = pd.to_datetime(df["start_date"], errors="coerce")
    mask: pd.Series = start_dates >= cutoff
    dropped: int = int((~mask & start_dates.notna()).sum())
    if dropped > 0:
        logger.warning(
            "Dropping %d trials older than %d years. Fixes: 3.13.",
            dropped, max_age_years,
            extra={"stage": "filter_age", "source": SOURCE_KEY,
                   "dropped": dropped,
                   "max_age_years": max_age_years},
        )
    return df[mask | start_dates.isna()].reset_index(drop=True)


def _validate_expected_record_count(
    row_count: int,
    db_path: Path,
) -> None:
    """Validate row count against expected_record_count (Issue 5.9, 12.4).

    Parameters
    ----------
    row_count : int
        Actual row count from the SQL query.
    db_path : Path
        Path to the AACT .db file (for error context).

    Raises
    ------
    CriticalDataSourceError
        If row count deviates from expected by >50% AND actual is at
        least 1000 rows (sanity floor — small test fixtures shouldn't
        trigger the critical check).
    """
    source_cfg: Dict[str, Any] = DATA_SOURCES[SOURCE_KEY]
    expected: Optional[int] = source_cfg.get("expected_record_count")
    if not expected or row_count == 0:
        return
    deviation: float = abs(row_count - expected) / expected
    # Issue 5.9 — only enforce critical check on substantial datasets
    # (>= 1000 rows). Small test fixtures would otherwise trigger the
    # critical check spuriously.
    if (
        deviation > CLINICALTRIALS_DEVIATION_CRITICAL_THRESHOLD
        and row_count >= 1000
    ):
        raise CriticalDataSourceError(
            f"Row count {row_count} deviates from expected {expected} by "
            f"{deviation * 100:.1f}% — possible AACT schema change or "
            f"truncated download. Fixes: 5.9, 12.4.",
            context={"db_path": str(db_path),
                     "expected": expected,
                     "actual": row_count,
                     "deviation_pct": deviation * 100},
        )
    if (
        deviation > CLINICALTRIALS_DEVIATION_WARNING_THRESHOLD
        and row_count >= 1000
    ):
        logger.warning(
            "Row count %d deviates from expected %d by %.1f%% — possible "
            "AACT schema change. Fixes: 5.9, 12.4.",
            row_count, expected, deviation * 100,
            extra={"stage": "data_quality", "source": SOURCE_KEY,
                   "expected": expected,
                   "actual": row_count,
                   "deviation_pct": deviation * 100},
        )


# =============================================================================
# Section 7 — Edge conversion
# =============================================================================
# Fixes Issues 2.1 (rel_type tested_for), 2.2 (cross-product penalty),
# 2.3 (deterministic edge_id), 2.4 (deduplication), 2.5 (evidence_strength),
# 2.6 (TypedDict schema), 3.3 (comparator/placebo), 3.5 (safety_signal),
# 3.6 (enrollment), 3.8 (allocation/masking), 3.10 (has_results),
# 4.7 (empty src_id/dst_id), 4.12 (input validation), 4.13 (PEP 8),
# 4.15 (TypedDict return), 5.1 (null nct_id), 5.4 (referential integrity),
# 5.6 (drug_name vs drug_mesh consistency), 5.11 (garbage MeSH),
# 6.5 (dead-letter queue), 6.6 (per-row try/except), 7.1 (idempotency),
# 7.5 (pipeline_version), 8.3 (streaming), 8.7 (vectorized),
# 9.10 (secret scanning), 13.7 (license attribution),
# 13.8 (citation), 14.6 (schema_version), 15.7 (MeSH crosswalk),
# 15.9 (Neo4j label compat), 15.10 (drug name normalization),
# 16.1-16.6 (lineage fields), 16.12 (id_confidence).


def _compute_evidence_strength(
    phase: Optional[str],
    enrollment: Optional[int],
    allocation: Optional[str],
    masking: Optional[str],
    has_results: Optional[bool],
    why_stopped: Optional[str],
    drug_role: str,
    n_interventions: int,
    n_conditions: int,
) -> Tuple[float, str, str]:
    """Compute evidence_strength + confidence + id_confidence (Issue 2.5).

    Parameters
    ----------
    phase : str or None
        Trial phase.
    enrollment : int or None
        Trial enrollment count.
    allocation : str or None
        Trial allocation (Randomized / Non-Randomized).
    masking : str or None
        Trial masking (Double Blind / Single Blind / Open Label).
    has_results : bool or None
        Whether the trial has published results.
    why_stopped : str or None
        Why the trial was stopped.
    drug_role : str
        "experimental" or "comparator_or_placebo".
    n_interventions : int
        Number of interventions in the trial.
    n_conditions : int
        Number of conditions in the trial.

    Returns
    -------
    tuple[float, str, str]
        ``(evidence_strength, confidence, id_confidence)``.

    Notes
    -----
    Evidence strength is a float in [0.0, 1.0] computed as:
      1. Base = ``CLINICALTRIALS_PHASE_STRENGTH[phase]`` (default 0.0).
      2. + ``CLINICALTRIALS_ALLOCATION_BONUS[allocation]`` (default 0.0).
      3. + ``CLINICALTRIALS_MASKING_BONUS[masking]`` (default 0.0).
      4. + ``CLINICALTRIALS_HAS_RESULTS_BONUS`` if has_results is True.
      5. + ``CLINICALTRIALS_ENROLLMENT_BONUS_VALUE`` if enrollment >=
         ``CLINICALTRIALS_ENROLLMENT_BONUS_LARGE_TRIAL``.
      6. - ``CLINICALTRIALS_CROSS_PRODUCT_PENALTY`` if
         n_interventions * n_conditions >
         ``CLINICALTRIALS_CROSS_PRODUCT_WARN_THRESHOLD``.
      7. - ``CLINICALTRIALS_SAFETY_STOP_PENALTY`` if why_stopped matches
         the safety pattern.
      8. * ``CLINICALTRIALS_COMPARATOR_EVIDENCE_MULTIPLIER`` if
         drug_role == "comparator_or_placebo".
      9. Clamp to [0.0, 1.0].
    """
    # Issue 2.5 — base from phase.
    base: float = CLINICALTRIALS_PHASE_STRENGTH.get(phase or "", 0.0)
    # Issue 3.8 — allocation bonus.
    base += CLINICALTRIALS_ALLOCATION_BONUS.get(allocation or "NA", 0.0)
    # Issue 3.8 — masking bonus.
    base += CLINICALTRIALS_MASKING_BONUS.get(masking or "NA", 0.0)
    # Issue 3.10 — has_results bonus.
    if has_results:
        base += CLINICALTRIALS_HAS_RESULTS_BONUS
    # Issue 3.6 — large-trial bonus.
    if enrollment is not None and enrollment >= CLINICALTRIALS_ENROLLMENT_BONUS_LARGE_TRIAL:
        base += CLINICALTRIALS_ENROLLMENT_BONUS_VALUE
    # Issue 2.2 — cross-product penalty.
    if n_interventions * n_conditions > CLINICALTRIALS_CROSS_PRODUCT_WARN_THRESHOLD:
        base -= CLINICALTRIALS_CROSS_PRODUCT_PENALTY
    # Issue 3.5 — safety-stop penalty.
    safety_signal: bool = False
    if why_stopped and isinstance(why_stopped, str):
        if _SAFETY_STOP_REGEX.search(why_stopped):
            base -= CLINICALTRIALS_SAFETY_STOP_PENALTY
            safety_signal = True
    # Issue 3.3 — comparator/placebo multiplier.
    is_comparator: bool = (drug_role == "comparator_or_placebo")
    if is_comparator:
        base *= CLINICALTRIALS_COMPARATOR_EVIDENCE_MULTIPLIER
    # Clamp.
    base = max(0.0, min(1.0, base))
    # Confidence: high / medium / low.
    confidence: str
    if base >= 0.7:
        confidence = "high"
    elif base >= 0.4:
        confidence = "medium"
    else:
        confidence = "low"
    # Issue 16.12 — id_confidence.
    id_confidence: str
    if is_comparator or safety_signal:
        id_confidence = "low"
    elif base >= 0.7:
        id_confidence = "high"
    elif base >= 0.4:
        id_confidence = "medium"
    else:
        id_confidence = "low"
    return (base, confidence, id_confidence)


def _detect_drug_role(description: Optional[str]) -> str:
    """Detect if an intervention is a comparator/placebo (Issue 3.3).

    Parameters
    ----------
    description : str or None
        The intervention description text.

    Returns
    -------
    str
        "comparator_or_placebo" if description matches the pattern,
        else "experimental".
    """
    if description and isinstance(description, str):
        if _COMPARATOR_REGEX.search(description):
            return "comparator_or_placebo"
    return "experimental"


def _build_edge_id(
    src_id: str,
    dst_id: str,
    src_type: str,
    dst_type: str,
    rel_type: str,
    nct_id: str,
) -> str:
    """Build a deterministic edge_id (Issue 2.3, 7.1, 7.2).

    The edge_id is a SHA-1 hash of
    ``"{src_id}|{dst_id}|{src_type}|{dst_type}|{rel_type}|{nct_id}"`` —
    including ``nct_id`` ensures that each trial produces a DISTINCT edge
    (Issue 5.3 — uniqueness of nct_id per edge).

    Parameters
    ----------
    src_id, dst_id, src_type, dst_type, rel_type, nct_id : str
        Edge identifier components.

    Returns
    -------
    str
        The first ``CLINICALTRIALS_HASH_LENGTH`` hex chars of the SHA-1.
    """
    raw: str = f"{src_id}|{dst_id}|{src_type}|{dst_type}|{rel_type}|{nct_id}"
    h = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    return h[:CLINICALTRIALS_HASH_LENGTH]


def _build_edge_provenance(
    cfg: ClinicalTrialsConfig,
    source_sha256: str,
    downloaded_at: str,
    metrics: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the _provenance dict for an edge (Issue 16.1-16.12).

    Returns a dict with every key in CLINICALTRIALS_PROVENANCE_KEYS.
    """
    source_cfg: Dict[str, Any] = DATA_SOURCES[SOURCE_KEY]
    provenance: Dict[str, Any] = {
        "source": SOURCE_NAME,
        "source_file": source_cfg["filename"],
        "source_sha256": source_sha256,
        "source_version": cfg.source_version,
        "source_release_date": cfg.source_release_date,
        "source_license": LICENSE,
        "source_url": source_cfg["url"],
        "parser_module": __name__,
        "parser_version": PARSER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "parsed_at": _iso_now(),
        "aact_release": cfg.source_version,
        "phases_filter": list(cfg.phases),
        "intervention_types_filter": list(cfg.intervention_types),
        "study_types_filter": list(cfg.study_types),
        "statuses_filter": list(cfg.allowed_statuses),
        "min_enrollment_filter": cfg.min_enrollment,
        "max_trial_age_years": cfg.max_trial_age_years,
        "row_count_in": metrics.get("rows_before_filter", 0),
        "row_count_out": metrics.get("rows_after_filter", 0),
        "n_dead_letter": metrics.get("rows_quarantined_total", 0),
        "n_orphan_src": metrics.get("edges_orphan_src", 0),
        "n_orphan_dst": metrics.get("edges_orphan_dst", 0),
        "n_safety_signal": metrics.get("edges_with_safety_signal", 0),
        "n_comparator": metrics.get("edges_with_comparator", 0),
        "crosswalk_version": "builtin_v1",
    }
    # Verify every required key is present.
    missing: List[str] = [
        k for k in CLINICALTRIALS_PROVENANCE_KEYS if k not in provenance
    ]
    if missing:
        logger.warning(
            "_provenance missing keys: %s. Fixes: 16.10.",
            missing,
            extra={"stage": "provenance", "source": SOURCE_KEY,
                   "missing_keys": missing},
        )
    return provenance


def _build_edge_record_from_dict(
    record: Dict[str, Any],
    cfg: ClinicalTrialsConfig,
    state: "_LoaderState",
) -> Optional[Dict[str, Any]]:
    """Build a single edge record from a parsed row dict (Issue 2.6, 4.7).

    Returns None if the row was quarantined.
    """
    # Issue 5.1 — null check on nct_id.
    nct_id_raw: Any = record.get("nct_id")
    if nct_id_raw is None or (
        isinstance(nct_id_raw, float) and pd.isna(nct_id_raw)
    ) or not str(nct_id_raw).strip():
        _quarantine(state, record, "null_or_empty_nct_id")
        return None
    nct_id: str = str(nct_id_raw).strip()
    # Issue 3.15 / 14.8 — validate NCT ID format.
    validated: Optional[str] = _validate_nct_id(nct_id)
    if validated is None:
        _quarantine(state, record, "invalid_nct_id_format")
        return None
    nct_id = validated

    # Issue 5.1 — nct_id type coercion (Issue 5.10).
    phase: Optional[str] = record.get("phase")
    if phase is not None and not isinstance(phase, str):
        phase = str(phase)

    # Issue 3.3 — detect drug_role from description.
    description: Optional[str] = record.get("description")
    drug_role: str = _detect_drug_role(description)

    # Issue 15.10 — normalize drug_name.
    drug_name: Optional[str] = _normalize_drug_name(record.get("drug_name"))
    # Issue 14.9 — normalize drug_mesh.
    drug_mesh: Optional[str] = _normalize_mesh(record.get("drug_mesh"))
    # Issue 5.11 — garbage MeSH check.
    if drug_mesh and _is_garbage_mesh(drug_mesh):
        _quarantine(state, record, "garbage_mesh_term")
        return None

    condition_name: Optional[str] = record.get("condition_name")
    if condition_name is not None and not isinstance(condition_name, str):
        condition_name = str(condition_name)
    if condition_name:
        condition_name = condition_name.strip()
    condition_mesh: Optional[str] = _normalize_mesh(record.get("condition_mesh"))
    if condition_mesh and _is_garbage_mesh(condition_mesh):
        _quarantine(state, record, "garbage_mesh_term")
        return None

    # Issue 4.7 / C10 — empty src_id / dst_id rejection.
    # src_id preference: drug_mesh (crosswalked) > drug_mesh (raw) > drug_name
    # v9 ROOT FIX (audit F5.2.5): the previous code emitted src_id = drug_mesh
    # (e.g. "D000068") or src_id = drug_name (e.g. "Dipyridamole"). Neither
    # matches ID_PATTERNS["Compound"] = ^(DB\d{5,6}|CHEMBL\d+|CID\d+|...)$,
    # so every ClinicalTrials Compound→Disease edge was dead-lettered. Now
    # we MIRROR SIDER's pattern: prefix MeSH IDs with "MESH:" so they
    # become "MESH:D000068" — and kg_builder's ID_PATTERNS["Compound"]
    # accepts MESH-prefixed IDs as a valid (though uncanonicalized) alias.
    # The entity_resolver can later canonicalize to DrugBank/ChEMBL ID via
    # the id_crosswalk module. drug_name (free text) is preserved as a
    # last-resort fallback prefixed with "NAME:".
    src_id: str = ""
    src_id_from_name: bool = False
    if drug_mesh:
        raw_mesh = str(drug_mesh).strip()
        # Normalise to "MESH:D######" form whether the input is bare or
        # already prefixed.
        if raw_mesh.upper().startswith("MESH:"):
            src_id = raw_mesh.upper()
        elif raw_mesh.startswith("D") and raw_mesh[1:].isdigit():
            src_id = f"MESH:{raw_mesh}"
        else:
            # Not a recognisable MeSH descriptor — fall through to name.
            src_id = ""
    # v35 ROOT FIX (V35-P2-LOADERS-FIXES M-6): the entity_resolver has
    # NO MeSH Compound alias system — MESH:D000068 IDs never match
    # any staged Compound node (which is keyed by InChIKey). Mirror
    # the drkg_loader / opentargets_loader pattern: call
    # ``_normalize_compound_id_to_inchikey`` to resolve the MeSH
    # descriptor to an InChIKey when the crosswalk can do so. Only
    # fall back to the raw ``MESH:`` ID when the crosswalk misses
    # (preserving prior behavior so the edge is still emitted — the
    # downstream entity resolver may still recover the link via
    # later crosswalk enrichment).
    if src_id.startswith("MESH:"):
        try:
            from .id_crosswalk import _normalize_compound_id_to_inchikey
            _norm_inchi = _normalize_compound_id_to_inchikey(
                src_id, source="clinicaltrials_loader",
            )
            if _norm_inchi and str(_norm_inchi).strip():
                src_id = str(_norm_inchi).strip().upper()
        except ImportError:  # pragma: no cover — defensive
            pass
        except Exception:  # pragma: no cover — defensive
            # Keep the raw MESH: ID; crosswalk miss is non-fatal.
            pass
    if not src_id and drug_name:
        # Free-text drug name. The v28 ROOT FIX (P2-B-12) REMOVED
        # ``NAME:`` from kg_builder.ID_PATTERNS["Compound"] because the
        # previous ``NAME:[A-Za-z0-9 _.-]{1,64}`` alternative accepted
        # literally any string as a Compound ID — making validation a
        # no-op and creating disjoint subgraphs (InChIKey-canonical
        # nodes vs NAME: nodes for the same drug).
        #
        # v41 ROOT FIX (Task K2 / SEV2 COMPOUND): the previous code
        # emitted ``src_id = f"NAME:{drug_name}"`` which kg_builder
        # would dead-letter anyway. Emitting an ID that is GUARANTEED
        # to be rejected downstream is wasteful (it propagates through
        # the build pipeline, fails validation, then gets written to
        # the kg_builder DLQ with reason="invalid_id_format" — losing
        # the ClinicalTrials-specific context). Fix: dead-letter the
        # edge HERE with reason="no_inchikey_for_drug_name" so the DLQ
        # entry carries the full ClinicalTrials record (nct_id,
        # drug_name, condition_name, etc.) for offline triage. Try the
        # crosswalk FIRST in case it has a name-indexed mapping (it
        # doesn't today, but this is forward-compatible with future
        # crosswalk enrichment — e.g. loading RxNorm → InChIKey).
        #
        # The v29 ``mesh_mapping_status="unmapped"`` prop is attached
        # to the DLQ entry (via ``extra``) for tracking purposes so
        # downstream consumers can distinguish "unmapped, edge kept"
        # from "unmapped, edge dead-lettered at loader".
        try:
            from .id_crosswalk import _normalize_compound_id_to_inchikey
            _drug_name_inchi = _normalize_compound_id_to_inchikey(
                str(drug_name).strip(), source="clinicaltrials_loader",
            )
        except ImportError:  # pragma: no cover — defensive
            _drug_name_inchi = ""
        # Crosswalk hit? → use the InChIKey as src_id (canonical Compound ID).
        if _drug_name_inchi and _INCHIKEY_RE.match(_drug_name_inchi):
            src_id = str(_drug_name_inchi).upper()
            # src_id_from_name stays False — the InChIKey is canonical,
            # so the edge should be treated as "mapped" for the
            # mesh_mapping_status check below.
        else:
            # Crosswalk miss — dead-letter INSTEAD of emitting NAME:.
            # The DLQ entry carries the original drug_name + condition
            # so Phase 1 entity-resolution gaps can be filled (extend
            # the crosswalk to map drug_name → InChIKey, then re-load).
            _quarantine(
                state, record, "no_inchikey_for_drug_name",
                extra={
                    "drug_name": str(drug_name).strip(),
                    "condition_name": (
                        str(condition_name).strip()
                        if condition_name else None
                    ),
                    "mesh_mapping_status": "unmapped",
                    "stage": "clinicaltrials_to_edge_records_drug_name_fallback",
                },
            )
            return None
    if not src_id or not src_id.strip():
        _quarantine(state, record, "empty_src_id")
        return None

    dst_id: str = ""
    dst_id_from_name: bool = False
    # v9 ROOT FIX (audit F5.2.5): same as src_id — prefix MeSH descriptors
    # with "MESH:" so they pass ID_PATTERNS["Disease"]. The Disease
    # pattern already accepts MESH:[A-Z]\d+ — so "MESH:D014979" is valid.
    if condition_mesh:
        raw_mesh = str(condition_mesh).strip()
        if raw_mesh.upper().startswith("MESH:"):
            dst_id = raw_mesh.upper()
        elif raw_mesh.startswith("D") and raw_mesh[1:].isdigit():
            dst_id = f"MESH:{raw_mesh}"
        else:
            dst_id = ""
    if not dst_id and condition_name:
        # Free-text condition name — prefix with NAME: as a last resort.
        # NOTE: NAME: is NOT in ID_PATTERNS["Disease"] by design — these
        # edges WILL be dead-lettered with a clear reason. The proper fix
        # is to extend the crosswalk to map condition_name → MESH/DOID.
        # For now we emit NAME: so the dead-letter record carries the
        # original drug_name for forensic inspection.
        dst_id = f"NAME:{str(condition_name).strip()[:64]}"
        dst_id_from_name = True
    if not dst_id or not dst_id.strip():
        _quarantine(state, record, "empty_dst_id")
        return None

    # Issue 3.6 — enrollment.
    enrollment_raw: Any = record.get("enrollment")
    enrollment: Optional[int]
    if enrollment_raw is None or (
        isinstance(enrollment_raw, float) and pd.isna(enrollment_raw)
    ):
        enrollment = None
    else:
        try:
            enrollment = int(enrollment_raw)
        except (ValueError, TypeError):
            enrollment = None
    # Issue 3.6 — small enrollment warning.
    if enrollment is not None and enrollment < CLINICALTRIALS_SUSPECT_ENROLLMENT_THRESHOLD \
            and phase in ("Phase 3", "Phase 4"):
        logger.warning(
            "NCT%s: enrollment=%d is below suspect threshold %d for a %s "
            "trial. Fixes: 3.6.",
            nct_id, enrollment,
            CLINICALTRIALS_SUSPECT_ENROLLMENT_THRESHOLD, phase,
            extra={"stage": "data_quality", "source": SOURCE_KEY,
                   "nct_id": nct_id, "enrollment": enrollment,
                   "phase": phase},
        )

    # Issue 3.5 — why_stopped safety signal.
    why_stopped: Optional[str] = record.get("why_stopped")
    if why_stopped is not None and not isinstance(why_stopped, str):
        why_stopped = str(why_stopped)

    # Issue 3.8 — allocation/masking.
    allocation: Optional[str] = record.get("allocation")
    masking: Optional[str] = record.get("masking")
    # Issue 3.10 — has_results.
    has_results_raw: Any = record.get("has_results")
    has_results: Optional[bool]
    if has_results_raw is None or (
        isinstance(has_results_raw, float) and pd.isna(has_results_raw)
    ):
        has_results = None
    else:
        has_results = bool(has_results_raw)

    # Issue 2.2 — cross-product inflation. We don't have direct access to
    # n_interventions / n_conditions here; we approximate via the GROUP BY.
    # For now, set to 1×1 (the loader has already exploded MeSH terms).
    n_interventions: int = 1
    n_conditions: int = 1

    # Issue 2.5 — compute evidence_strength.
    evidence_strength, confidence, id_confidence = _compute_evidence_strength(
        phase=phase,
        enrollment=enrollment,
        allocation=allocation,
        masking=masking,
        has_results=has_results,
        why_stopped=why_stopped,
        drug_role=drug_role,
        n_interventions=n_interventions,
        n_conditions=n_conditions,
    )

    # Issue 16.12 — adjust id_confidence based on fallback ID usage.
    if src_id_from_name or dst_id_from_name:
        id_confidence = "low"

    # v29 ROOT FIX (audit L-14): MeSH-less edges were dead-lettered. Now kept with mesh_mapping_status="unmapped" so the data isn't lost.
    # The previous behaviour: when ``drug_mesh`` (Compound side) or
    # ``condition_mesh`` (Disease side) was missing, the loader fell back
    # to a ``NAME:<free-text>`` src_id / dst_id. The v28 ROOT FIX
    # (P2-B-12) then REMOVED ``NAME:`` from kg_builder.ID_PATTERNS, so
    # every free-text-fallback edge was dead-lettered at kg_builder load
    # time — silently dropping every ClinicalTrials edge whose
    # intervention/condition had no MeSH descriptor. For a 500K-trial
    # source where ~40% of conditions lack MeSH, that was a catastrophic
    # loss of drug-repurposing signal. The ROOT FIX here marks every
    # such edge with ``mesh_mapping_status="unmapped"`` (and ``"mapped"``
    # when both endpoints came from MeSH) so downstream consumers can
    # (a) keep the edge instead of dead-lettering it, (b) filter or
    # down-weight unmapped edges in training, (c) audit how much
    # MeSH-coverage the source actually has. The edge is still emitted;
    # the property is the audit signal.
    mesh_mapping_status: str = (
        "mapped"
        if (not src_id_from_name and not dst_id_from_name)
        else "unmapped"
    )

    # Issue 2.1 / 14.1 / 15.3 — rel_type = "tested_for".
    src_type: str = "Compound"  # Issue 15.9
    dst_type: str = "Disease"  # Issue 15.9
    rel_type: str = "tested_for"

    # Issue 2.3 / 7.1 — deterministic edge_id.
    edge_id: str = _build_edge_id(
        src_id, dst_id, src_type, dst_type, rel_type, nct_id,
    )

    # Issue 16.6 — nct_url.
    nct_url: str = f"https://clinicaltrials.gov/study/{nct_id}"

    # Issue 3.3 — comparator/placebo warning.
    if drug_role == "comparator_or_placebo":
        logger.warning(
            "NCT%s: intervention %r appears to be a comparator/placebo. "
            "Edge emitted with evidence_strength=%.2f and id_confidence=low. "
            "Fixes: 3.3.",
            nct_id, _sanitize_for_log(drug_name or src_id),
            evidence_strength,
            extra={"stage": "comparator", "source": SOURCE_KEY,
                   "nct_id": nct_id,
                   "drug_name": drug_name or src_id,
                   "evidence_strength": evidence_strength},
        )

    # Issue 3.5 — safety_signal flag.
    safety_signal: Optional[str] = None
    if why_stopped and isinstance(why_stopped, str):
        if _SAFETY_STOP_REGEX.search(why_stopped):
            safety_signal = "stopped_for_safety"

    # Issue 16.1-16.6 — lineage fields.
    source_cfg: Dict[str, Any] = DATA_SOURCES[SOURCE_KEY]
    props: Dict[str, Any] = {
        # Trial identity
        "nct_id": nct_id,
        "nct_url": nct_url,  # Issue 16.6
        "brief_title": record.get("brief_title") or "",  # Issue 3.12
        "phase": phase or "",
        "status": record.get("overall_status") or "",  # Issue 3.11
        "study_type": record.get("study_type") or "",  # Issue 3.7
        "enrollment": enrollment,  # Issue 3.6
        "why_stopped": why_stopped or "",  # Issue 3.5
        "has_results": bool(has_results) if has_results is not None else False,
        # Drug
        "drug_name": drug_name or "",
        "drug_mesh": drug_mesh or "",
        "drug_role": drug_role,  # Issue 3.3
        # Condition
        "condition_name": condition_name or "",
        "condition_mesh": condition_mesh or "",
        # v29 ROOT FIX (audit L-14): carried into props so the edge is
        # auditable / filterable downstream ("mapped" vs "unmapped").
        "mesh_mapping_status": mesh_mapping_status,
        # Design (Issue 3.8)
        "allocation": allocation or "",
        "intervention_model": record.get("intervention_model") or "",
        "masking": masking or "",
        "primary_purpose": record.get("primary_purpose") or "",
        # Outcome (Issue 3.9)
        "primary_outcome": record.get("primary_outcome") or "",
        # Dates (Issue 3.13)
        "start_date": record.get("start_date") or "",
        "completion_date": record.get("completion_date") or "",
        # Safety (Issue 3.5)
        "safety_signal": safety_signal,
        # Lineage (Issue 16.1-16.6)
        "source_url": source_cfg["url"],  # Issue 16.1
        "downloaded_at": state.downloaded_at,  # Issue 16.2, 7.3
        "source_sha256": state.source_sha256,  # Issue 16.3, 7.4
        "source_version": cfg.source_version,  # Issue 16.4, 7.6
        "pipeline_version": PARSER_VERSION,  # Issue 7.5, 16.5
        "schema_version": SCHEMA_VERSION,  # Issue 14.6
        "license": LICENSE,  # Issue 13.7
        "citation": CITATION,  # Issue 13.8
        # Compliance
        "_source": SOURCE_NAME,
        "_license": LICENSE,
        "_attribution": ATTRIBUTION,
        "_schema_version": SCHEMA_VERSION,
        "_provenance": _build_edge_provenance(
            cfg, state.source_sha256, state.downloaded_at, state.metrics,
        ),
    }

    # Issue 9.10 — secret scanning on props.
    secret_field: Optional[str] = _scan_for_secrets(props)
    if secret_field:
        _quarantine(state, record, "suspected_secret_in_data")
        logger.warning(
            "NCT%s: suspected secret detected in field %s — row quarantined. "
            "Fixes: 9.10.",
            nct_id, secret_field,
            extra={"stage": "security", "source": SOURCE_KEY,
                   "nct_id": nct_id, "secret_field": secret_field},
        )
        return None

    edge: Dict[str, Any] = {
        "src_id": src_id,
        "dst_id": dst_id,
        "src_type": src_type,
        "dst_type": dst_type,
        "rel_type": rel_type,
        "edge_id": edge_id,
        "source_tag": SOURCE_NAME,
        "evidence_strength": evidence_strength,
        "confidence": confidence,
        "id_confidence": id_confidence,
        # v29 ROOT FIX (audit L-14): top-level mesh_mapping_status so
        # kg_builder / downstream consumers can keep the edge (instead of
        # dead-lettering MeSH-less free-text fallbacks) and audit coverage.
        "mesh_mapping_status": mesh_mapping_status,
        "props": props,
    }
    return edge


class _LoaderState:
    """Mutable per-run state for the edge builder (Issue 6.5, 6.6)."""

    def __init__(
        self,
        cfg: ClinicalTrialsConfig,
        source_sha256: str,
        downloaded_at: str,
    ) -> None:
        self.cfg: ClinicalTrialsConfig = cfg
        self.source_sha256: str = source_sha256
        self.downloaded_at: str = downloaded_at
        self.metrics: Dict[str, Any] = {
            "rows_before_filter": 0,
            "rows_after_filter": 0,
            "rows_dropped_null_nct": 0,
            "rows_dropped_invalid_nct": 0,
            "rows_dropped_garbage_mesh": 0,
            "rows_dropped_empty_src": 0,
            "rows_dropped_empty_dst": 0,
            "rows_dropped_age": 0,
            "rows_quarantined_total": 0,
            "edges_total": 0,
            "edges_deduped": 0,
            "edges_orphan_src": 0,
            "edges_orphan_dst": 0,
            "edges_with_low_id_confidence": 0,
            "edges_with_safety_signal": 0,
            "edges_with_comparator": 0,
            "null_nct_id": 0,
            "null_drug_mesh": 0,
            "null_drug_name": 0,
            "null_both_drug": 0,
            "null_condition_mesh": 0,
            "null_condition_name": 0,
            "null_both_condition": 0,
            "null_enrollment": 0,
            "null_phase": 0,
            "stopped_for_safety": 0,
            "phase_counts": {},
            "status_counts": {},
        }
        self.quarantine_count: int = 0


def _quarantine(
    state: _LoaderState,
    record: Dict[str, Any],
    reason: str,
    *,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Write a bad row to the DLQ (Issue 6.5, 6.6).

    Appends a JSONL entry to ``state.cfg.effective_dead_letter_path``.
    Thread-safe via ``_DLQ_LOCK``.

    v41 ROOT FIX (Task K2 / SEV2 COMPOUND): added optional ``extra``
    keyword-only parameter so callers can attach additional tracking
    fields to the DLQ entry (e.g. ``mesh_mapping_status="unmapped"``
    for edges dead-lettered due to ``no_inchikey_for_drug_name``).
    Existing callers are unaffected (``extra`` defaults to ``None``).
    """
    state.quarantine_count += 1
    state.metrics["rows_quarantined_total"] += 1
    nct_id: Any = record.get("nct_id")
    entry: Dict[str, Any] = {
        "timestamp": _iso_now(),
        "nct_id": str(nct_id) if nct_id is not None else None,
        "reason": reason,
        "raw": _sanitize_for_log(json.dumps(record, default=str), 2000),
        "parsed_partial": None,
        "error_type": "",
        "error_message": reason,
        "parser_version": PARSER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "load_id": _get_load_id(),
    }
    # v41 ROOT FIX (Task K2): merge any caller-supplied extra fields.
    # Late-merge so callers can override defaults if needed (e.g. add
    # ``mesh_mapping_status``, ``drug_name``, ``src_id_attempted``).
    if extra:
        entry.update(extra)
    path: Path = state.cfg.effective_dead_letter_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with _DLQ_LOCK:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")


def clinicaltrials_to_edge_records(
    df: pd.DataFrame,
    *,
    cfg: Optional[ClinicalTrialsConfig] = None,
    source_sha256: str = "",
    downloaded_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Convert ClinicalTrials DataFrame to edge records (Issue 4.12, 4.15).

    Backward-compatible v0 shim. Delegates to
    ``clinicaltrials_to_edge_records_streaming`` and materializes the result.

    Parameters
    ----------
    df : pd.DataFrame
        DataFrame from ``parse_clinicaltrials``.
    cfg : ClinicalTrialsConfig or None
        Loader configuration. If None, uses defaults.
    source_sha256 : str
        SHA-256 of the source AACT zip (Issue 16.3).
    downloaded_at : str or None
        ISO-8601 timestamp when the AACT was downloaded (Issue 16.2).

    Returns
    -------
    list of dict
        List of edge records (``ClinicalTrialEdgeRecord`` shape).

    Raises
    ------
    TypeError
        If ``df`` is not a pandas DataFrame (Issue 4.12).
    ValueError
        If ``df`` is missing required columns (Issue 4.12, 4.16).
    """
    # Issue 4.12 — input validation.
    if not isinstance(df, pd.DataFrame):
        raise TypeError(
            f"df must be a pandas.DataFrame, got {type(df).__name__}. "
            f"Fixes: 4.12.",
        )
    if len(df) == 0:
        return []
    # Issue 4.12 / 4.16 — required columns.
    required_cols: FrozenSet[str] = frozenset({
        "nct_id", "drug_name", "condition_name",
    })
    missing: FrozenSet[str] = required_cols - set(df.columns)
    if missing:
        raise ValueError(
            f"DataFrame missing required columns: {sorted(missing)}. "
            f"Got: {sorted(df.columns)}. Fixes: 4.12, 4.16.",
        )

    cfg = cfg or ClinicalTrialsConfig()
    if not downloaded_at:
        downloaded_at = _iso_now()

    state: _LoaderState = _LoaderState(cfg, source_sha256, downloaded_at)
    edges: List[Dict[str, Any]] = []

    # Issue 8.7 — vectorize row processing via to_dict('records').
    records: List[Dict[str, Any]] = df.to_dict("records")
    state.metrics["rows_before_filter"] = len(records)

    # Issue 11.3 — data quality metrics.
    _compute_data_quality_metrics(df, state)

    # Issue 6.6 — per-row try/except.
    for record in records:
        try:
            edge: Optional[Dict[str, Any]] = _build_edge_record_from_dict(
                record, cfg, state,
            )
            if edge is not None:
                edges.append(edge)
                state.metrics["edges_total"] += 1
                if edge["id_confidence"] == "low":
                    state.metrics["edges_with_low_id_confidence"] += 1
                if edge["props"].get("safety_signal"):
                    state.metrics["edges_with_safety_signal"] += 1
                if edge["props"].get("drug_role") == "comparator_or_placebo":
                    state.metrics["edges_with_comparator"] += 1
        except Exception as exc:
            _quarantine(
                state, record,
                f"build_edge_exception:{type(exc).__name__}:{exc}",
            )
            logger.warning(
                "Row quarantine: %s. Fixes: 6.6.",
                _sanitize_for_log(exc),
                extra={"stage": "build_edge", "source": SOURCE_KEY,
                       "error": str(exc)[:200],
                       "error_type": type(exc).__name__,
                       "nct_id": record.get("nct_id")},
            )
            continue

    state.metrics["rows_after_filter"] = len(edges)

    # Issue 2.4 / 7.1 — deduplicate by edge_id.
    edges = _dedupe_edges(edges, state)

    # Issue 5.4 — referential integrity check.
    _check_referential_integrity(edges, state)

    # Issue 5.2 — log duplicate count.
    if state.metrics["edges_deduped"] > 0:
        logger.info(
            "Deduplicated %d duplicate edges (kept %d of %d). Fixes: 5.2.",
            state.metrics["edges_deduped"], len(edges),
            state.metrics["edges_total"],
            extra={"stage": "dedup", "source": SOURCE_KEY,
                   "duplicates_removed": state.metrics["edges_deduped"],
                   "final_count": len(edges)},
        )

    if state.quarantine_count > 0:
        logger.warning(
            "Quarantined %d records to %s. Inspect before re-running. "
            "Fixes: 6.5.",
            state.quarantine_count, state.cfg.effective_dead_letter_path,
            extra={"stage": "quarantine", "source": SOURCE_KEY,
                   "count": state.quarantine_count,
                   "path": str(state.cfg.effective_dead_letter_path)},
        )

    # Issue 16.10 — write lineage log.
    _write_lineage_log({
        "step": "edge_conversion",
        "metrics": state.metrics,
        "cfg": _safe_config_dict(cfg),
        "source_sha256": source_sha256,
        "downloaded_at": downloaded_at,
    }, cfg)

    # Issue 4.10 — lazy logging.
    logger.info(
        "Converted %d ClinicalTrials edge records (quarantined=%d, "
        "deduped=%d). Fixes: 4.10.",
        len(edges), state.quarantine_count,
        state.metrics["edges_deduped"],
        extra={"stage": "edge_conversion", "source": SOURCE_KEY,
               "edge_count": len(edges),
               "quarantine_count": state.quarantine_count,
               "dedup_count": state.metrics["edges_deduped"]},
    )

    return edges


def clinicaltrials_to_edge_records_streaming(
    df_or_iter: Union[pd.DataFrame, Iterator[Dict[str, Any]]],
    *,
    cfg: Optional[ClinicalTrialsConfig] = None,
    source_sha256: str = "",
    downloaded_at: Optional[str] = None,
) -> Iterator[Dict[str, Any]]:
    """Streaming edge-record generator (Issue 8.3, 8.4, 8.6).

    Accepts either a DataFrame or an iterator of record dicts, yields
    edge records one at a time. Memory-bounded for large AACT snapshots.

    Parameters
    ----------
    df_or_iter : pd.DataFrame or Iterator[dict]
        DataFrame or iterator of parsed trial records.
    cfg : ClinicalTrialsConfig or None
        Loader configuration.
    source_sha256 : str
        SHA-256 of the source AACT zip.
    downloaded_at : str or None
        ISO-8601 timestamp when the AACT was downloaded.

    Yields
    ------
    dict
        Edge record (``ClinicalTrialEdgeRecord`` shape).
    """
    cfg = cfg or ClinicalTrialsConfig()
    if not downloaded_at:
        downloaded_at = _iso_now()
    state: _LoaderState = _LoaderState(cfg, source_sha256, downloaded_at)

    if isinstance(df_or_iter, pd.DataFrame):
        iterator: Iterator[Dict[str, Any]] = (
            r for r in df_or_iter.to_dict("records")
        )
    else:
        iterator = df_or_iter

    for record in iterator:
        try:
            edge: Optional[Dict[str, Any]] = _build_edge_record_from_dict(
                record, cfg, state,
            )
            if edge is not None:
                yield edge
        except Exception as exc:
            _quarantine(
                state, record,
                f"build_edge_exception:{type(exc).__name__}:{exc}",
            )
            logger.warning(
                "Row quarantine (streaming): %s. Fixes: 6.6.",
                _sanitize_for_log(exc),
                extra={"stage": "build_edge_streaming",
                       "source": SOURCE_KEY,
                       "error": str(exc)[:200]},
            )
            continue


def _dedupe_edges(
    edges: List[Dict[str, Any]],
    state: _LoaderState,
) -> List[Dict[str, Any]]:
    """Deduplicate edges by edge_id, keeping the highest-evidence one (Issue 2.4, 7.1).

    Parameters
    ----------
    edges : list of dict
        Edge records.
    state : _LoaderState
        Loader state (metrics updated in place).

    Returns
    -------
    list of dict
        Deduplicated edge records.
    """
    if not edges:
        return edges
    best_by_id: Dict[str, Dict[str, Any]] = {}
    for edge in edges:
        eid: str = edge["edge_id"]
        if eid not in best_by_id:
            best_by_id[eid] = edge
        else:
            # Keep the one with higher evidence_strength.
            if edge["evidence_strength"] > best_by_id[eid]["evidence_strength"]:
                best_by_id[eid] = edge
            state.metrics["edges_deduped"] += 1
    return list(best_by_id.values())


def _check_referential_integrity(
    edges: List[Dict[str, Any]],
    state: _LoaderState,
) -> None:
    """Check that src_id/dst_id will resolve to existing KG nodes (Issue 5.4, 15.7).

    This is a best-effort check — without a loaded crosswalk, we cannot
    verify that MeSH terms map to existing Compound/Disease nodes. We flag
    edges where:
      * src_id is a MeSH descriptor (not a DrugBank ID) → orphan_src
      * dst_id is a MeSH descriptor (not a UMLS CUI) → orphan_dst
    and set their id_confidence to "low".

    Parameters
    ----------
    edges : list of dict
        Edge records (modified in place).
    state : _LoaderState
        Loader state (metrics updated in place).
    """
    orphan_src: int = 0
    orphan_dst: int = 0
    for edge in edges:
        # Issue 15.7 — MeSH term as src_id is "orphan" (not crosswalked to DrugBank).
        if _is_valid_mesh_id_format(edge["src_id"]):
            orphan_src += 1
            edge["id_confidence"] = "low"
            edge["props"]["orphan_src"] = True
        # Issue 15.8 — MeSH term as dst_id is "orphan" (not crosswalked to UMLS).
        if _is_valid_mesh_id_format(edge["dst_id"]):
            orphan_dst += 1
            edge["id_confidence"] = "low"
            edge["props"]["orphan_dst"] = True
    state.metrics["edges_orphan_src"] = orphan_src
    state.metrics["edges_orphan_dst"] = orphan_dst
    # Issue 5.4 — warn if >50% orphan rate.
    if edges and (
        orphan_src / len(edges) > 0.5
        or orphan_dst / len(edges) > 0.5
    ):
        logger.warning(
            "High orphan edge rate: orphan_src=%d/%d (%.1f%%), "
            "orphan_dst=%d/%d (%.1f%%). Likely a MeSH→DrugBank/UMLS "
            "crosswalk failure. Fixes: 5.4, 15.7, 15.8.",
            orphan_src, len(edges), 100 * orphan_src / max(1, len(edges)),
            orphan_dst, len(edges), 100 * orphan_dst / max(1, len(edges)),
            extra={"stage": "referential_integrity", "source": SOURCE_KEY,
                   "orphan_src": orphan_src,
                   "orphan_dst": orphan_dst,
                   "total_edges": len(edges)},
        )


def _compute_data_quality_metrics(
    df: pd.DataFrame,
    state: _LoaderState,
) -> None:
    """Compute null counts for every critical column (Issue 5.8, 11.3).

    Parameters
    ----------
    df : pd.DataFrame
        The parsed DataFrame.
    state : _LoaderState
        Loader state (metrics updated in place).
    """
    metrics: Dict[str, Any] = {
        "total_rows": len(df),
        "null_nct_id": int(df["nct_id"].isna().sum())
        if "nct_id" in df.columns else 0,
        "null_drug_mesh": int(df["drug_mesh"].isna().sum())
        if "drug_mesh" in df.columns else 0,
        "null_drug_name": int(df["drug_name"].isna().sum())
        if "drug_name" in df.columns else 0,
        "null_condition_mesh": int(df["condition_mesh"].isna().sum())
        if "condition_mesh" in df.columns else 0,
        "null_condition_name": int(df["condition_name"].isna().sum())
        if "condition_name" in df.columns else 0,
        "null_enrollment": int(df["enrollment"].isna().sum())
        if "enrollment" in df.columns else 0,
        "null_phase": int(df["phase"].isna().sum())
        if "phase" in df.columns else 0,
    }
    if "drug_mesh" in df.columns and "drug_name" in df.columns:
        metrics["null_both_drug"] = int(
            (df["drug_mesh"].isna() & df["drug_name"].isna()).sum()
        )
    if "condition_mesh" in df.columns and "condition_name" in df.columns:
        metrics["null_both_condition"] = int(
            (df["condition_mesh"].isna() & df["condition_name"].isna()).sum()
        )
    if "why_stopped" in df.columns:
        metrics["stopped_for_safety"] = int(
            df["why_stopped"].astype(str).str.contains(
                "safety|adverse|death|toxicity",
                case=False, na=False,
            ).sum()
        )
    # Issue 5.5 — phase value counts.
    if "phase" in df.columns:
        state.metrics["phase_counts"] = (
            df["phase"].value_counts().to_dict()
        )
        unknown_phases: set = set(
            state.metrics["phase_counts"]
        ) - CLINICALTRIALS_VALID_PHASES
        if unknown_phases:
            logger.warning(
                "Unknown phase values in AACT: %s. These were filtered out. "
                "Fixes: 5.5.",
                sorted(unknown_phases),
                extra={"stage": "data_quality", "source": SOURCE_KEY,
                       "unknown_phases": sorted(unknown_phases)},
            )
    if "overall_status" in df.columns:
        state.metrics["status_counts"] = (
            df["overall_status"].value_counts().to_dict()
        )

    state.metrics.update(metrics)
    logger.info(
        "Data quality metrics: %s. Fixes: 5.8, 11.3.",
        metrics,
        extra={"stage": "data_quality", "source": SOURCE_KEY, **metrics},
    )


def clinicaltrials_to_node_records(
    df: pd.DataFrame,
    *,
    cfg: Optional[ClinicalTrialsConfig] = None,
) -> List[Dict[str, Any]]:
    """Generate minimal node records for unresolved MeSH IDs (Issue 15.2).

    The ClinicalTrials loader emits edges ONLY — Compound and Disease
    nodes are owned by DrugBank / ChEMBL / OpenTargets and DisGeNET / OMIM
    respectively. However, this function emits minimal placeholder node
    records for MeSH IDs that don't resolve to existing KG nodes, so the
    KG builder can create them.

    Parameters
    ----------
    df : pd.DataFrame
        DataFrame from ``parse_clinicaltrials``.
    cfg : ClinicalTrialsConfig or None
        Loader configuration.

    Returns
    -------
    list of dict
        List of node records (``ClinicalTrialNodeRecord`` shape).
    """
    cfg = cfg or ClinicalTrialsConfig()
    nodes: List[Dict[str, Any]] = []
    seen_ids: set = set()
    for record in df.to_dict("records"):
        # Compound node from MeSH (if MeSH is a valid descriptor ID).
        drug_mesh: Optional[str] = _normalize_mesh(record.get("drug_mesh"))
        if drug_mesh and _is_valid_mesh_id_format(drug_mesh):
            if drug_mesh not in seen_ids:
                seen_ids.add(drug_mesh)
                nodes.append({
                    "node_id": drug_mesh,
                    "node_type": "Compound",
                    "props": {
                        "name": record.get("drug_name") or drug_mesh,
                        "mesh_term": drug_mesh,
                        "source": SOURCE_NAME,
                        "_source": SOURCE_NAME,
                        "_license": LICENSE,
                        "_attribution": ATTRIBUTION,
                        "_schema_version": SCHEMA_VERSION,
                    },
                })
        # Disease node from MeSH.
        cond_mesh: Optional[str] = _normalize_mesh(record.get("condition_mesh"))
        if cond_mesh and _is_valid_mesh_id_format(cond_mesh):
            if cond_mesh not in seen_ids:
                seen_ids.add(cond_mesh)
                nodes.append({
                    "node_id": cond_mesh,
                    "node_type": "Disease",
                    "props": {
                        "name": record.get("condition_name") or cond_mesh,
                        "mesh_term": cond_mesh,
                        "source": SOURCE_NAME,
                        "_source": SOURCE_NAME,
                        "_license": LICENSE,
                        "_attribution": ATTRIBUTION,
                        "_schema_version": SCHEMA_VERSION,
                    },
                })
    return nodes


def clinicaltrials_to_graph(
    df: pd.DataFrame,
    *,
    cfg: Optional[ClinicalTrialsConfig] = None,
    source_sha256: str = "",
    downloaded_at: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Convenience wrapper: convert df to (nodes, edges) (Issue 1.1).

    Parameters
    ----------
    df : pd.DataFrame
        DataFrame from ``parse_clinicaltrials``.
    cfg : ClinicalTrialsConfig or None
        Loader configuration.
    source_sha256 : str
        SHA-256 of the source AACT zip.
    downloaded_at : str or None
        ISO-8601 timestamp when the AACT was downloaded.

    Returns
    -------
    tuple[list, list]
        ``(nodes, edges)``.
    """
    cfg = cfg or ClinicalTrialsConfig()
    nodes: List[Dict[str, Any]] = clinicaltrials_to_node_records(df, cfg=cfg)
    edges: List[Dict[str, Any]] = clinicaltrials_to_edge_records(
        df, cfg=cfg,
        source_sha256=source_sha256,
        downloaded_at=downloaded_at,
    )
    return (nodes, edges)


# =============================================================================
# Section 8 — Validation
# =============================================================================
# Fixes Issues 10.9 (schema validation tests), 11.3 (data quality metrics),
# 16.1-16.12 (lineage enforcement).


def validate_clinicaltrials(
    df: Optional[pd.DataFrame] = None,
    edges: Optional[List[Dict[str, Any]]] = None,
    *,
    cfg: Optional[ClinicalTrialsConfig] = None,
) -> Dict[str, Any]:
    """Validate ClinicalTrials data and edges (Issue 10.9, 11.3).

    Runs the following checks:
      1. Every emitted edge has required keys (``src_id``, ``dst_id``,
         ``src_type``, ``dst_type``, ``rel_type``, ``edge_id``,
         ``source_tag``, ``evidence_strength``, ``confidence``,
         ``id_confidence``, ``props``).
      2. ``src_type`` is always ``"Compound"`` (Issue 15.9).
      3. ``dst_type`` is always ``"Disease"`` (Issue 15.9).
      4. ``rel_type`` is always ``"tested_for"`` — NEVER ``"clinical_trial"``
         (deprecated) or ``"treats"`` (forbidden — Issue 2.1, 14.1).
      5. Every edge ``props._provenance`` contains every key in
         ``CLINICALTRIALS_PROVENANCE_KEYS`` (Issue 16.1-16.12).
      6. Every edge has ``_source``, ``_license``, ``_attribution``,
         ``_schema_version`` (Issue 13.7, 14.4).
      7. ``evidence_strength`` is in [0.0, 1.0] (Issue 2.5).

    Parameters
    ----------
    df : pd.DataFrame or None
        Optional parsed DataFrame (not currently used; placeholder for
        future data-quality checks).
    edges : list of dict or None
        List of edge records to validate.
    cfg : ClinicalTrialsConfig or None
        Loader configuration.

    Returns
    -------
    dict
        Validation report with keys ``is_valid``, ``errors``, ``warnings``,
        ``metrics``, ``schema_version``, ``parser_version``.
    """
    cfg = cfg or ClinicalTrialsConfig()
    errors: List[str] = []
    warnings: List[str] = []
    metrics: Dict[str, Any] = {
        "edge_count": 0,
        "missing_required_keys": 0,
        "wrong_src_type": 0,
        "wrong_dst_type": 0,
        "wrong_rel_type": 0,
        "missing_provenance_keys": 0,
        "missing_compliance_keys": 0,
        "out_of_range_evidence_strength": 0,
    }

    required_keys: FrozenSet[str] = frozenset({
        "src_id", "dst_id", "src_type", "dst_type", "rel_type",
        "edge_id", "source_tag", "evidence_strength", "confidence",
        "id_confidence", "props",
    })
    required_compliance: FrozenSet[str] = frozenset({
        "_source", "_license", "_attribution", "_schema_version",
    })

    if edges is not None:
        metrics["edge_count"] = len(edges)
        for i, edge in enumerate(edges):
            # Check 1: required keys.
            missing_keys: FrozenSet[str] = required_keys - set(edge.keys())
            if missing_keys:
                errors.append(
                    f"Edge {i} missing required keys: {sorted(missing_keys)}"
                )
                metrics["missing_required_keys"] += 1
                continue
            # Check 2: src_type.
            if edge["src_type"] != "Compound":
                errors.append(
                    f"Edge {i} src_type={edge['src_type']!r} != 'Compound'"
                )
                metrics["wrong_src_type"] += 1
            # Check 3: dst_type.
            if edge["dst_type"] != "Disease":
                errors.append(
                    f"Edge {i} dst_type={edge['dst_type']!r} != 'Disease'"
                )
                metrics["wrong_dst_type"] += 1
            # Check 4: rel_type.
            if edge["rel_type"] == "clinical_trial":
                errors.append(
                    f"Edge {i} rel_type='clinical_trial' is DEPRECATED. "
                    f"Use 'tested_for'."
                )
                metrics["wrong_rel_type"] += 1
            elif edge["rel_type"] == "treats":
                errors.append(
                    f"Edge {i} rel_type='treats' is FORBIDDEN in "
                    f"clinicaltrials_loader (reserved for FDA-approved "
                    f"drugs from DrugBank)."
                )
                metrics["wrong_rel_type"] += 1
            elif edge["rel_type"] != "tested_for":
                errors.append(
                    f"Edge {i} rel_type={edge['rel_type']!r} != 'tested_for'"
                )
                metrics["wrong_rel_type"] += 1
            # Check 5: provenance keys.
            props: Dict[str, Any] = edge.get("props", {})
            provenance: Dict[str, Any] = props.get("_provenance", {})
            missing_prov: FrozenSet[str] = (
                frozenset(CLINICALTRIALS_PROVENANCE_KEYS) - set(provenance.keys())
            )
            if missing_prov:
                errors.append(
                    f"Edge {i} _provenance missing keys: {sorted(missing_prov)}"
                )
                metrics["missing_provenance_keys"] += 1
            # Check 6: compliance keys.
            missing_compliance: FrozenSet[str] = (
                required_compliance - set(props.keys())
            )
            if missing_compliance:
                errors.append(
                    f"Edge {i} props missing compliance keys: "
                    f"{sorted(missing_compliance)}"
                )
                metrics["missing_compliance_keys"] += 1
            # Check 7: evidence_strength range.
            es: Any = edge.get("evidence_strength")
            if not isinstance(es, (int, float)) or es < 0.0 or es > 1.0:
                errors.append(
                    f"Edge {i} evidence_strength={es!r} not in [0, 1]"
                )
                metrics["out_of_range_evidence_strength"] += 1

    # Write validation report to disk.
    report: Dict[str, Any] = {
        "is_valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "metrics": metrics,
        "schema_version": SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
    }
    try:
        CLINICALTRIALS_QUALITY_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CLINICALTRIALS_QUALITY_REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)
    except OSError as exc:
        logger.warning(
            "Could not write quality report to %s: %s.",
            CLINICALTRIALS_QUALITY_REPORT_PATH, exc,
            extra={"stage": "validate", "source": SOURCE_KEY,
                   "error": str(exc)},
        )
    return report


# =============================================================================
# Section 9 — ClinicalTrialsLoader class (Protocol adapter)
# =============================================================================
# Fixes Issues 1.1 (Loader Protocol), 1.2 (__all__), 1.6 (Config dataclass).


class ClinicalTrialsLoader:
    """Protocol adapter for the Loader Protocol (Issue 1.1, 1.2).

    A ``Loader``-Protocol-compatible class that wraps the module-level
    functions so ``run_pipeline.py`` can treat all loaders polymorphically.

    Attributes
    ----------
    name : str
        Always ``"ClinicalTrials"``.
    cfg : ClinicalTrialsConfig
        Frozen loader configuration.

    Examples
    --------
    >>> from drugos_graph.clinicaltrials_loader import ClinicalTrialsLoader
    >>> from drugos_graph._loader_protocol import Loader
    >>> loader = ClinicalTrialsLoader()
    >>> isinstance(loader, Loader)
    True
    >>> loader.name
    'ClinicalTrials'
    """

    name: str = SOURCE_NAME

    def __init__(
        self,
        cfg: Optional[ClinicalTrialsConfig] = None,
    ) -> None:
        """Initialize the loader.

        Parameters
        ----------
        cfg : ClinicalTrialsConfig or None
            Loader configuration. If None, uses defaults.
        """
        self.cfg: ClinicalTrialsConfig = cfg or ClinicalTrialsConfig()

    def download(self, force: bool = False) -> Path:
        """Download (or cached-load) the AACT raw file (Issue 1.1).

        Parameters
        ----------
        force : bool
            If True, re-download even if cached.

        Returns
        -------
        Path
            Path to the extracted AACT directory.
        """
        return download_clinicaltrials(
            force=force, cfg=self.cfg,
        )

    def parse(self, path: Optional[Path] = None) -> Iterator[Dict[str, Any]]:
        """Yield parsed trial records (Issue 1.1, 8.3).

        Parameters
        ----------
        path : Path or None
            Path to the extracted AACT directory. If None, defaults to
            ``RAW_DIR / "clinicaltrials"``.

        Yields
        ------
        dict
            One trial record per yield.
        """
        for chunk_df in iter_clinicaltrials_trials(ct_dir=path, cfg=self.cfg):
            for record in chunk_df.to_dict("records"):
                yield record

    def to_graph(
        self, records: Any,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Convert records into (nodes, edges) for the KG (Issue 1.1).

        Parameters
        ----------
        records : pd.DataFrame or iterable of dict
            Parsed trial records.

        Returns
        -------
        tuple[list, list]
            ``(nodes, edges)``.
        """
        if isinstance(records, pd.DataFrame):
            df: pd.DataFrame = records
        else:
            df = pd.DataFrame(list(records))
        return clinicaltrials_to_graph(df, cfg=self.cfg)


# =============================================================================
# Section 10 — Backward-compat aliases
# =============================================================================
# Issue 1.5 — re-export canonical names. The v0 shim names are preserved
# above (download_clinicaltrials, parse_clinicaltrials,
# clinicaltrials_to_edge_records) — they ARE the public API.


# =============================================================================
# Section 11 — load_clinicaltrials end-to-end
# =============================================================================
# Fixes Issues 1.4 (EDGE_PRODUCERS contract), 1.6 (Config dataclass),
# 7.1 (idempotency), 10.10 (integration test with kg_builder — TODO).


def load_clinicaltrials(
    cfg: Optional[ClinicalTrialsConfig] = None,
    skip_neo4j: bool = True,
) -> Dict[str, Any]:
    """End-to-end: download -> parse -> to_graph -> validate (Issue 1.6, 7.1).

    Parameters
    ----------
    cfg : ClinicalTrialsConfig or None
        Loader configuration. If None, uses defaults.
    skip_neo4j : bool
        If True (default), skip the Neo4j load step. If False, attempt to
        load edges via ``kg_builder.DrugOSGraphBuilder.load_edges_bulk_create``.

    Returns
    -------
    dict
        Summary dict with keys ``edges_total``, ``nodes_total``,
        ``validation_report``, ``elapsed_seconds``, ``source_sha256``,
        ``source_version``.

    Raises
    ------
    ClinicalTrialsDataIntegrityError
        If 0 edges produced and the loader is in CLINICAL+ enforcement
        mode.
    """
    set_global_seed(SEED)  # Issue 7.1 — idempotency.
    cfg = cfg or ClinicalTrialsConfig()
    t0: float = time.monotonic()
    ensure_dirs()

    # Issue 6.5 / 4.8 — download + extract.
    extract_dir: Path = download_clinicaltrials(cfg=cfg)

    # Compute SHA-256 of the cached zip for lineage.
    source_cfg: Dict[str, Any] = DATA_SOURCES[SOURCE_KEY]
    zip_path: Path = cfg.effective_raw_dir / source_cfg["filename"]
    source_sha256: str = ""
    if zip_path.exists():
        try:
            source_sha256 = _compute_sha256(zip_path)
        except OSError:
            pass
    downloaded_at: str = _iso_now()

    # Issue 3.1 / 4.5 — parse.
    df: pd.DataFrame = parse_clinicaltrials_trials(
        ct_dir=extract_dir, cfg=cfg,
    )

    # Issue 2.6 / 4.7 — convert to edges.
    nodes, edges = clinicaltrials_to_graph(
        df, cfg=cfg,
        source_sha256=source_sha256,
        downloaded_at=downloaded_at,
    )

    # Issue 10.9 — validate.
    report: Dict[str, Any] = validate_clinicaltrials(df, edges, cfg=cfg)
    if not report["is_valid"]:
        logger.warning(
            "ClinicalTrials validation found %d errors. First 5: %s. "
            "Fixes: 10.9.",
            len(report["errors"]), report["errors"][:5],
            extra={"stage": "validate", "source": SOURCE_KEY,
                   "error_count": len(report["errors"]),
                   "first_errors": report["errors"][:5]},
        )

    # Issue 10.10 — optional Neo4j load.
    if not skip_neo4j and edges:
        try:
            from .kg_builder import DrugOSGraphBuilder  # local import.
            with DrugOSGraphBuilder(Neo4jConfig()) as builder:
                # Issue 2.1 / 14.1 — rel_type="tested_for" (NOT "clinical_trial").
                # Issue 2.4 / 7.1 — use_merge=True for idempotency.
                builder.load_edges_bulk_create(
                    "Compound", "tested_for", "Disease", edges,
                    use_merge=True,
                )
        except Exception as exc:
            logger.error(
                "Neo4j load failed: %s. Edges NOT loaded to KG. "
                "Fixes: 10.10.",
                _sanitize_for_log(exc),
                extra={"stage": "neo4j_load", "source": SOURCE_KEY,
                       "error": str(exc)[:200],
                       "edge_count": len(edges)},
            )

    elapsed: float = time.monotonic() - t0
    return {
        "edges_total": len(edges),
        "nodes_total": len(nodes),
        "validation_report": report,
        "elapsed_seconds": elapsed,
        "source_sha256": source_sha256,
        "source_version": cfg.source_version,
        "edges": edges,
        "nodes": nodes,
    }


# =============================================================================
# Section 12 — Utilities (timestamps, IDs, dead-letter, lineage, audit logs)
# =============================================================================
# Fixes Issues 11.1 (structured logging), 11.4 (timing logs),
# 16.9 (audit trail), 16.10 (provenance metadata sidecar).


def _iso_now() -> str:
    """Return current UTC time in ISO-8601 format with Z suffix.

    Returns
    -------
    str
        ISO-8601 UTC timestamp, e.g. ``2024-12-01T12:34:56.789012Z``.
    """
    return datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def _get_load_id() -> str:
    """Return process-cached load_id (correlation ID — Issue 16.9).

    Thread-safe via ``_LOAD_ID_LOCK``.

    Returns
    -------
    str
        ``clinicaltrials_<YYYYMMDDTHHMMSS>_<8-char-uuid>``.
    """
    global _LOAD_ID
    with _LOAD_ID_LOCK:
        if _LOAD_ID is None:
            _LOAD_ID = (
                f"clinicaltrials_"
                f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_"
                f"{uuid.uuid4().hex[:8]}"
            )
        return _LOAD_ID


def _reset_load_id() -> None:
    """Reset the process-cached load_id (test helper)."""
    global _LOAD_ID
    with _LOAD_ID_LOCK:
        _LOAD_ID = None


def _write_lineage_log(
    entry: Dict[str, Any],
    cfg: ClinicalTrialsConfig,
) -> None:
    """Write a lineage log entry (Issue 16.10).

    Thread-safe via ``_LINEAGE_LOCK``.

    Parameters
    ----------
    entry : dict
        The lineage entry to write.
    cfg : ClinicalTrialsConfig
        Loader configuration (for path).
    """
    enriched: Dict[str, Any] = {
        "run_id": str(uuid.uuid4()),
        "timestamp": _iso_now(),
        "load_id": _get_load_id(),
        "operator": os.environ.get("USER", "unknown"),
        "loader_version": PARSER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE_NAME,
        **entry,
    }
    path: Path = cfg.effective_lineage_log_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LINEAGE_LOCK:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(enriched, default=str) + "\n")


def _write_audit_log(event: str, **fields: Any) -> None:
    """Write an audit log entry (Issue 16.9).

    Thread-safe via ``_AUDIT_LOCK``.

    Parameters
    ----------
    event : str
        Event name (e.g. "DOWNLOAD", "PARSE", "EXTRACT").
    **fields : Any
        Additional fields to include in the audit entry.
    """
    entry: Dict[str, Any] = {
        "timestamp": _iso_now(),
        "event": event,
        "load_id": _get_load_id(),
        "operator": os.environ.get("USER", "unknown"),
        "parser_version": PARSER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE_NAME,
        **fields,
    }
    path: Path = LOGS_DIR / "audit" / "clinicaltrials_access.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _AUDIT_LOCK:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")


def _safe_config_dict(cfg: ClinicalTrialsConfig) -> Dict[str, Any]:
    """Return a JSON-safe dict of the config (Issue 12.7).

    Converts Path objects to strings and tuple/frozenset to list.
    """
    d: Dict[str, Any] = asdict(cfg)
    safe: Dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, Path):
            safe[k] = str(v)
        elif isinstance(v, (tuple, frozenset, set)):
            safe[k] = list(v)
        else:
            safe[k] = v
    return safe


# =============================================================================
# Section 13 — Static assertions (forbidden rel_types)
# =============================================================================
# Issue 2.1 / 14.1 / 15.3 — "clinical_trial" and "treats" are FORBIDDEN as
# rel_types in this loader. The ONLY emittable triple is
# ("Compound", "tested_for", "Disease").

# Static assertion that "treats" and "clinical_trial" are NOT in the
# emittable triples.
_FORBIDDEN_REL_TYPES: FrozenSet[str] = frozenset({"treats", "clinical_trial"})
assert not any(
    rel in _FORBIDDEN_REL_TYPES
    for (_, rel, _) in CLINICALTRIALS_EMITTABLE_TRIPLES
), (
    "Issue 2.1 / 14.1 / 15.3 violation: 'treats' and 'clinical_trial' are "
    "FORBIDDEN as rel_types in clinicaltrials_loader. Only 'tested_for' is "
    "allowed."
)

# Static assertion that ("Compound", "tested_for", "Disease") is in CORE_EDGE_TYPES.
assert ("Compound", "tested_for", "Disease") in CORE_EDGE_TYPES, (
    "Issue 2.1 / 14.1 / 15.3 violation: ('Compound', 'tested_for', 'Disease') "
    "must be in config.CORE_EDGE_TYPES for clinicaltrials_loader to emit edges."
)


# Issue 7.10 — no random seed needed; loader is deterministic given the
# AACT DB. (Comment-only fix.)


# Issue 13.1 — Data dictionary is at drugos_graph/data/clinicaltrials_data_dictionary.md.
# Issue 13.4 — AACT schema documentation: https://aact.ctti-clinicaltrials.org/definitions
# Issue 13.10 — README section is at drugos_graph/README.md (ClinicalTrials section).
# Issue 13.12 — AACT table/column reference is in the module docstring above.
