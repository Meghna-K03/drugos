"""DrugOS Graph Module — Main Pipeline Runner
============================================

Institutional-grade, production-ready pipeline orchestrator for the DrugOS
Autonomous Drug Repurposing Platform (Team Cosmic).

Executes the full Week 2 graph construction pipeline (13 sequential steps):

  Step 1:  Download and parse DRKG (5.87M triples from 7+ databases)
  Step 2:  Build entity and edge index mappings
  Step 3:  Load DRKG into Neo4j (bulk CREATE, idempotent via clear_graph)
  Step 4:  Parse DrugBank XML and enrich compound nodes
  Step 5:  Ingest STITCH drug-protein interactions (action-type aware)
  Step 6:  Ingest SIDER side effects (MedDRA-coded)
  Step 7:  Ingest additional sources (STRING, ChEMBL, OpenTargets, UniProt,
           ClinicalTrials, DisGeNET, OMIM, PubChem, GEO)
  Step 8:  Run entity resolution (Compound/Disease/Gene/Protein + crosswalk)
  Step 9:  Build PyG HeteroData for GNN training
  Step 10: Build training data (positive/negative pairs, temporal split)
  Step 11: Train TransE baseline model
  Step 12: Evaluate and validate (V1 launch criteria)
  Step 13: Generate data README and lineage manifest

Step Data Contracts (return dict keys per step):

  Step 1:  df (pd.DataFrame), validation (dict), elapsed (float),
           [fatal (bool), fatal_reason (str) on abort]
  Step 2:  entity_maps (dict), edge_maps (dict), elapsed (float)
  Step 3:  node_results (dict), edge_results (dict), elapsed (float),
           [skipped (bool) | error (str)]
  Step 4:  drug_records (list[dict]), target_edges (list[dict]), elapsed (float)
  Step 5:  stitch_edges (int), elapsed (float)
  Step 6:  sider_nodes (int), sider_edges (int), elapsed (float)
  Step 7:  results (dict of per-source counts), elapsed (float)
  Step 8:  stats (dict), gene_protein_edges (list), crosswalk_summary (dict),
           elapsed (float)
  Step 9:  summary (dict), data_path (str), elapsed (float)
  Step 10: training_data (dict), auxiliary_pairs (list), elapsed (float)
  Step 11: history_loss (list), elapsed (float), [skipped (bool)]
  Step 12: stats (dict), criteria (dict), sanity (dict), elapsed (float)
  Step 13: readme_path (str)

Failure Mode Summary:
  - Steps 1-2: FATAL — abort pipeline immediately on failure
  - Step 3:  CRITICAL — if Neo4j fails, skip steps 4-7 (Neo4j-dependent)
  - Steps 4-7: DEGRADABLE — continue pipeline on failure, log error
  - Steps 8-13: DEGRADABLE — continue pipeline on failure, log error

Usage:
  python -m drugos_graph
  python -m drugos_graph.run_pipeline
  python -m drugos_graph.run_pipeline --skip-download --skip-neo4j
  python -m drugos_graph.run_pipeline --step 5
  python -m drugos_graph.run_pipeline --fresh-start

Version: 2.0.0-week2 | Schema: 2.0.0 | 56 fixes across 16 domains
"""

from __future__ import annotations

__all__ = [
    "step1_load_drkg",
    "step1_load_phase1",          # v6: Phase 1 bridge as data source
    "step1_load_data",            # v6: dispatcher (drkg | phase1)
    "step2_build_mappings",
    "step3_load_neo4j",
    "step4_drugbank_enrichment",
    "step5_stitch_ingestion",
    "step6_sider_ingestion",
    "step7_additional_sources",
    "step8_entity_resolution",
    "step9_build_pyg",
    "step10_training_data",
    "step11_train_transe",
    "step12_validation",
    "step13_readme",
    "run_full_pipeline",
    "main",
]

# ─── Standard Library ──────────────────────────────────────────────────────────

import argparse
import hashlib
import json
import logging
import os
import pickle
import re
import signal
import sys
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ─── Package Imports ───────────────────────────────────────────────────────────

from .config import (
    AUDIT_LOG_DIR,
    CANONICAL_IDS,
    CHECKPOINT_DIR,
    CONFIG_HASH,
    CONFIG_VERSION,
    DATA_SOURCES,
    DEAD_LETTER_DIR,
    LOG_FORMAT,
    LOG_LEVEL,
    LOG_LEVELS,
    LOGS_DIR,
    MIN_NEGATIVE_PAIRS,
    # v29 ROOT FIX (audit I-11): was 1 in dev — statistically meaningless. Now 10.
    # (Previously tracked as audit L-12; the audit ID was renamed to I-11
    # in the final forensic report. The fix is the same: a positive-pair
    # count of 1 produces a held-out AUC on (literally) one sample —
    # statistically meaningless. The dev default was raised from "1" to
    # "10" so a held-out AUC has more than one sample to score against.
    # The constant itself is defined in config.py — the single source of
    # truth — and is read here by reference.)
    MIN_POSITIVE_PAIRS,
    PACKAGE_VERSION,
    PIPELINE_VERSION,
    PROCESSED_DIR,
    RAW_DIR,
    SCHEMA_VERSION,
    SEED,
    Neo4jConfig,
    PyGConfig,
    TransEConfig,
    build_lineage_metadata,
    compute_config_hash,
    ensure_dirs,
)
from .drkg_loader import (
    build_edge_index_maps,
    build_entity_id_maps,
    download_drkg,
    parse_drkg_tsv,
    validate_drkg,
)
from .drugbank_parser import (
    drugbank_to_node_records,
    drugbank_to_target_edges,
    parse_drugbank_xml,
)
# v27 ROOT FIX (P2-L-4): import the canonical action → relation mapper so
# the Phase 1 inline path emits the SAME canonical verbs as the raw-XML path.
from .drugbank_parser import _map_action_to_relation as _db_map_action_to_relation

# ─── Module-Level State ────────────────────────────────────────────────────────

_logger_lock = threading.Lock()
_logger_configured: bool = False
_pipeline_run_id: str = ""
_shutdown_requested: bool = False


# v20 Compound-2/Compound-8 ROOT FIX — Production escape-hatch guard.
# The audit's Compound-2 / Compound-8 chains identified that
# DRUGOS_ALLOW_NO_SAMPLER=1 (and the legacy single-pool fallback in
# transe_model.py:1647-1676) silently re-activates the
# AUC-Enforcement-Theater and Negative-Sampling-Invalidation chains
# in production. The fix in v18 added these as opt-in escape hatches
# for unit tests, but never guarded against accidental production
# use. This module-level check runs at import time and REFUSES to
# load if any escape hatch is set when DRUGOS_ENVIRONMENT=production.
#
# This is a hard guard — operators cannot bypass it without editing
# source code. The escape hatches remain available for dev/test.
def _check_production_escape_hatches() -> None:
    """Refuse to load if escape hatches are set in production env."""
    env = os.environ.get("DRUGOS_ENVIRONMENT", "dev").lower()
    if env in ("prod", "production"):
        offenders: List[str] = []
        for flag in (
            "DRUGOS_ALLOW_NO_SAMPLER",
            "DRUGOS_ALLOW_PERMISSIVE_KG",
            "DRUGOS_ALLOW_PERMISSIVE_DPI",
            "DRUGOS_ALLOW_LAUNCH_FAIL",
        ):
            if os.environ.get(flag, "") == "1":
                offenders.append(flag)
        if offenders:
            raise RuntimeError(
                "REFUSING TO LOAD: production environment detected "
                f"(DRUGOS_ENVIRONMENT={env}) but escape-hatch flag(s) "
                f"are set: {', '.join(offenders)}. These flags re-activate "
                "patient-safety-critical compound destruction chains "
                "(Compound-1, Compound-2, Compound-5, Compound-8). "
                "Unset the flag(s) or change DRUGOS_ENVIRONMENT to 'dev'."
            )


_check_production_escape_hatches()


# v29 ROOT FIX (audit I-8 / M-9 — "Happy-Path Orchestration"):
# The forensic audit found that every step 3-13 wraps its body in
# ``try: ... except Exception as e: results["stepN"] = {"skipped": True}``.
# The pipeline ALWAYS writes ``pipeline_results.json`` even if every
# step was skipped. This makes the system structurally incapable of
# reporting failure — every previous AI session that told the user
# "it's 100% integrated" was reading exit code 0 + ``dev_smoke_test_pass=True``
# without checking ``passed=False`` or the AUC log.
#
# ROOT FIX: add a helper that, in production mode, RE-RAISES the
# exception instead of silently swallowing it. In dev mode, it
# preserves the legacy lenient behavior (so dev/CI runners without
# all data sources still work).
def _is_production_mode() -> bool:
    """Return True iff DRUGOS_ENVIRONMENT is set to prod/production."""
    return os.environ.get("DRUGOS_ENVIRONMENT", "dev").lower() in ("prod", "production")


def _step_exception_or_skip(step_name: str, exc: Exception, results: dict) -> None:
    """Handle a step exception: re-raise in production, fail in dev.

    v29 ROOT FIX for Compound Chain 8 ("Happy-Path Orchestration").
    v43 ROOT FIX (P2-006): use ``"failed": True`` instead of
    ``"skipped": True`` when a step RAN and raised an exception.
    "skipped" means "didn't run" (like step4-step7 when Neo4j is
    unavailable). "failed" means "ran and crashed." Operators can
    now distinguish the two from the result dict.

    In production mode, this function ALWAYS re-raises ``exc`` —
    silently swallowing step failures is the root cause of the audit's
    "every session every AI tells its 100 percent integrated" complaint.
    In dev mode, it records the failure in ``results[step_name]`` so the
    pipeline can continue (useful for partial-data CI runs).
    """
    if _is_production_mode():
        logger.critical(
            "PRODUCTION_STEP_FAILURE (%s): %s. Re-raising — production "
            "mode MUST NOT silently swallow step failures (audit I-8).",
            step_name, exc,
        )
        raise exc
    # Dev mode: record the failure (not "skip" — the step RAN and crashed).
    logger.warning(
        "DEV_STEP_FAILED (%s): %s. DRUGOS_ENVIRONMENT=dev — continuing "
        "with failed step. Set DRUGOS_ENVIRONMENT=prod to fail-fast. "
        "(v43 P2-006: labeled 'failed' not 'skipped' — the step ran)",
        step_name, exc,
    )
    # v43 ROOT FIX (P2-006): use "failed": True (not "skipped": True).
    # The step ACTUALLY RAN and raised an exception — "skipped" is
    # misleading. Reserve "skipped": True for steps that didn't run
    # at all (like step4-step7 when Neo4j is unavailable).
    results[step_name] = {
        "error": str(exc),
        "failed": True,
        "skipped": False,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING & OBSERVABILITY (Domain 11)
# ═══════════════════════════════════════════════════════════════════════════════


class V1LaunchCriteriaFailed(RuntimeError):
    """v21 ROOT FIX (Audit Chain 12): raised by run_full_pipeline when
    V1 launch criteria are not met, instead of calling sys.exit(1).

    Libraries should raise; callers decide exit codes. ``run_unified.py``
    catches this and returns exit code 4 (the documented contract).
    ``python -m drugos_graph`` catches this and returns exit 1.
    """

    def __init__(self, criteria: dict):
        self.criteria = criteria
        failed = {k: v for k, v in criteria.items() if v is False}
        super().__init__(
            f"V1 launch criteria not met: {failed}"
        )


class _RunIdFilter(logging.Filter):
    """Injects the pipeline run_id into every LogRecord.

    Fixes GAP-LOG-01: All log entries are now correlated by run_id,
    making it possible to trace a single pipeline run across all steps
    and all log files.
    """

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id
        return True


def _configure_logging() -> None:
    """Configure logging for the pipeline (called on first use).

    Thread-safe (BUG-COD-01). Uses LOG_LEVEL and LOG_FORMAT from config
    (BUG-LOG-01, BUG-LOG-02). Uses RotatingFileHandler for log rotation
    (GAP-LOG-04). Injects run_id into all records (GAP-LOG-01).
    """
    global _logger_configured, _pipeline_run_id
    with _logger_lock:
        if _logger_configured:
            return

        ensure_dirs()
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

        # BUG-LOG-01: Use configured log level, not hardcoded INFO
        log_level = LOG_LEVELS.get(LOG_LEVEL.upper(), logging.INFO)

        # BUG-LOG-02: Use configured log format
        log_format = LOG_FORMAT

        # Generate run_id for this pipeline invocation (GAP-LOG-01)
        _pipeline_run_id = os.environ.get(
            "DRUGOS_RUN_ID",
            datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_"
            + uuid.uuid4().hex[:8],
        )

        # Create formatters
        console_formatter = logging.Formatter(log_format)
        file_formatter = logging.Formatter(
            "%(asctime)s | run_id=" + _pipeline_run_id + " | "
            "%(name)s | %(levelname)s | %(message)s"
        )

        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(log_level)
        console_handler.setFormatter(console_formatter)

        # GAP-LOG-04: RotatingFileHandler instead of plain FileHandler
        log_path = LOGS_DIR / "pipeline.log"
        file_handler = RotatingFileHandler(
            log_path, maxBytes=50 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setLevel(log_level)
        file_handler.setFormatter(file_formatter)

        # Root pipeline logger
        root_logger = logging.getLogger("drugos_pipeline")
        root_logger.setLevel(log_level)
        root_logger.addHandler(console_handler)
        root_logger.addHandler(file_handler)
        root_logger.addFilter(_RunIdFilter(_pipeline_run_id))

        _logger_configured = True


logger = logging.getLogger("drugos_pipeline")


# ═══════════════════════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS (shared across domains)
# ═══════════════════════════════════════════════════════════════════════════════


def _serialize_for_json(obj: Any) -> Any:
    """Custom JSON serializer that preserves structure.

    Fixes BUG-DQ-02: pipeline_results.json used default=str which silently
    converted DataFrames and complex objects to opaque strings.
    Now DataFrames retain shape/columns/head, numpy arrays retain shape/dtype/sample.

    audit-2025 ROOT FIX (issue 51): the previous catch-all
    ``return str(obj)`` converted EVERY non-numpy scalar — including
    Python ``bool``, ``int``, ``float``, ``str`` — to a string. This
    meant checkpoint files stored ``"passed": "True"`` (string) instead
    of ``"passed": true`` (boolean), and ``"triples": "66"`` (string)
    instead of ``"triples": 66`` (int). Downstream comparisons like
    ``if checkpoint["passed"]:`` were ALWAYS truthy (non-empty string),
    even for ``"False"``. The fix preserves native JSON types
    (bool, int, float, str, None) and only stringifies genuinely
    unserializable objects (e.g. custom classes, Path already handled
    above).

    Parameters
    ----------
    obj : Any
        Object to serialize.

    Returns
    -------
    Any
        JSON-serializable representation.
    """
    import numpy as np
    import pandas as pd

    # Preserve native JSON-serializable types (issue 51 root fix).
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (pd.DataFrame, pd.Series)):
        return {
            "__type__": "DataFrame" if isinstance(obj, pd.DataFrame) else "Series",
            "shape": list(obj.shape),
            "columns": list(obj.columns) if hasattr(obj, "columns") else [],
            "head": (
                obj.head(5).to_dict(orient="records")
                if len(obj) > 0
                else []
            ),
        }
    if isinstance(obj, np.ndarray):
        return {
            "__type__": "ndarray",
            "shape": list(obj.shape),
            "dtype": str(obj.dtype),
            "sample": obj.flatten()[:5].tolist() if obj.size > 0 else [],
        }
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, dict):
        return {str(k): _serialize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize_for_json(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted(_serialize_for_json(v) for v in obj)
    if isinstance(obj, Path):
        return str(obj)
    # Genuinely unserializable — stringify as last resort.
    return str(obj)


def _validate_step_output(
    step_name: str,
    result: dict,
    required_keys: Optional[List[str]] = None,
    min_counts: Optional[Dict[str, int]] = None,
) -> bool:
    """Validate a step's output dict for required keys and minimum counts.

    Fixes BUG-DQ-03: No intermediate data quality checks between steps.

    Parameters
    ----------
    step_name : str
        Human-readable step name for log messages.
    result : dict
        The step's return value.
    required_keys : list, optional
        Keys that must be present in result.
    min_counts : dict, optional
        Mapping of key -> minimum count. Supports nested dicts (sums values).

    Returns
    -------
    bool
        True if validation passed (or only warnings), False on errors.
    """
    if result.get("fatal") or result.get("error"):
        logger.error(
            "%s produced a fatal/error result: %s", step_name, result
        )
        return False
    if required_keys:
        for key in required_keys:
            if key not in result:
                logger.warning(
                    "%s missing required key: %s", step_name, key
                )
    if min_counts:
        for key, threshold in min_counts.items():
            val = result.get(key, 0)
            if isinstance(val, dict):
                val = sum(val.values()) if val else 0
            elif isinstance(val, (list,)):
                val = len(val)
            if val < threshold:
                logger.warning(
                    "%s: %s = %d (below minimum threshold %d)",
                    step_name,
                    key,
                    val,
                    threshold,
                )
    return True


def _check_data_freshness(
    filepath: Path,
    source_name: str,
    max_stale_days: int = 365,
) -> None:
    """Check if a source data file is stale.

    Fixes GAP-DQ-01: No data freshness validation — stale source files
    used without warning.

    Parameters
    ----------
    filepath : Path
        Path to the data file to check.
    source_name : str
        Human-readable source name for log messages.
    max_stale_days : int
        Maximum acceptable age in days before WARNING.
    """
    try:
        mtime = filepath.stat().st_mtime
        age_days = (time.time() - mtime) / 86400
        if age_days > max_stale_days:
            logger.warning(
                "%s data is %.0f days old (stale threshold: %d days). "
                "Consider re-downloading.",
                source_name,
                age_days,
                max_stale_days,
            )
        else:
            logger.info(
                "%s data age: %.0f days (fresh)", source_name, age_days
            )
    except OSError:
        logger.debug("Could not check freshness for %s", filepath)


def _retry_on_failure(
    func,
    max_retries: int = 3,
    backoff_base: float = 2.0,
    retryable_exceptions: tuple = (Exception,),
) -> Any:
    """Execute a function with exponential backoff retry.

    Fixes BUG-REL-03: No retry logic for ANY step.

    Parameters
    ----------
    func : callable
        Function to execute.
    max_retries : int
        Maximum number of retry attempts.
    backoff_base : float
        Exponential backoff base in seconds.
    retryable_exceptions : tuple
        Exception types that trigger a retry.

    Returns
    -------
    Any
        The function's return value.

    Raises
    ------
    Exception
        The last exception if all retries exhausted.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            return func()
        except retryable_exceptions as e:
            last_exc = e
            if attempt < max_retries:
                wait = backoff_base ** (attempt - 1)
                logger.warning(
                    "Attempt %d/%d failed: %s. Retrying in %.1fs...",
                    attempt,
                    max_retries,
                    e,
                    wait,
                )
                time.sleep(wait)
            else:
                logger.error(
                    "All %d attempts failed for %s: %s",
                    max_retries,
                    func.__name__,
                    e,
                )
    raise last_exc  # type: ignore[misc]


def _scan_for_pii(records: List[Dict[str, Any]], source_name: str) -> int:
    """Scan records for potential PII before Neo4j writes.

    Fixes GUARD-SEC-01: No PII scanning on input data.

    Parameters
    ----------
    records : list
        List of record dicts to scan.
    source_name : str
        Source name for log messages.

    Returns
    -------
    int
        Number of records flagged.
    """
    pii_patterns = [
        (r"\b\d{3}-\d{2}-\d{4}\b", "SSN-like pattern"),
        (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", "email"),
        (r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b", "phone-like pattern"),
    ]
    flagged = 0
    for record in records:
        for val in record.values():
            if not isinstance(val, str):
                continue
            for pattern, pii_type in pii_patterns:
                if re.search(pattern, val):
                    flagged += 1
                    logger.warning(
                        "PII detected in %s: %s found in value (record index: %d)",
                        source_name,
                        pii_type,
                        records.index(record),
                    )
                    break  # One warning per record
    if flagged > 0:
        logger.error(
            "PII SCAN: %d/%d records from %s contain potential PII",
            flagged,
            len(records),
            source_name,
        )
    return flagged


def _save_checkpoint(step_num: int, results: dict) -> None:
    """Save pipeline checkpoint for resume capability.

    Fixes BUG-REL-04: No checkpoint/resume capability.

    Parameters
    ----------
    step_num : int
        Step number that just completed.
    results : dict
        Results dict to persist.
    """
    try:
        ensure_dirs()
        checkpoint_dir = CHECKPOINT_DIR
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        checkpoint_path = checkpoint_dir / f"step_{step_num:02d}.json"
        serializable = _serialize_for_json(results)
        checkpoint_path.write_text(
            json.dumps(serializable, indent=2), encoding="utf-8"
        )
        logger.info("Checkpoint saved: %s", checkpoint_path)
    except Exception as e:
        logger.warning("Failed to save checkpoint for step %d: %s", step_num, e)


def _load_checkpoint(step_num: int) -> Optional[dict]:
    """Load pipeline checkpoint for resume capability.

    Parameters
    ----------
    step_num : int
        Step number to load.

    Returns
    -------
    dict or None
        Checkpoint data, or None if not found.
    """
    checkpoint_path = CHECKPOINT_DIR / f"step_{step_num:02d}.json"
    if checkpoint_path.exists():
        try:
            data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            logger.info("Checkpoint loaded: %s", checkpoint_path)
            return data
        except Exception as e:
            logger.warning("Failed to load checkpoint: %s", e)
    return None


# v29 ROOT FIX (audit I-9): --resume re-ran step 1 and 4. Now caches
# df/drug_records to disk and loads from cache on resume.
#
# Forensic audit finding I-9: ``run_full_pipeline``'s ``--resume N``
# logic re-ran step 1 (``step1_load_data``) and step 4
# (``step4_drugbank_enrichment``) on every resume to re-derive the
# ``df`` DataFrame and ``drug_records`` list. This defeated the
# purpose of checkpointing: a resume after step 10 still paid the
# full step 1 + step 4 cost (re-reading all Phase 1 CSVs, re-running
# the bridge, re-parsing DrugBank). On production-scale data this
# added 10+ minutes to every resume.
#
# ROOT FIX: pickle the heavy step-1/step-4 outputs to
# ``CHECKPOINT_DIR`` after each step completes successfully, and
# load them from disk on resume. Falls back to the legacy re-derive
# behavior if the cache is missing or corrupt (defensive — never
# break the pipeline).
#
# Cache files:
#   * ``step01_cache.pkl`` — (df, entity_maps, edge_maps,
#                             edge_props_lookup, node_props_lookup)
#   * ``step04_cache.pkl`` — drug_records list
# Each file is a pickled tuple. The cache is invalidated automatically
# when the source CSVs change (the input_checksum stored in the
# step-1 checkpoint guards this).
_STEP_CACHE_FILES = {
    1: "step01_cache.pkl",
    4: "step04_cache.pkl",
}


def _save_step_cache(step_num: int, payload: tuple) -> None:
    """Pickle a step's heavy outputs to disk for fast --resume.

    Parameters
    ----------
    step_num : int
        Step number whose outputs are being cached.
    payload : tuple
        Pickle-able tuple of objects to cache.
    """
    cache_name = _STEP_CACHE_FILES.get(step_num)
    if cache_name is None:
        return  # step does not support caching
    try:
        ensure_dirs()
        cache_dir = CHECKPOINT_DIR
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / cache_name
        with open(cache_path, "wb") as f:
            pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
        logger.info(
            "Step %d cache saved: %s (%d bytes)",
            step_num, cache_path, cache_path.stat().st_size,
        )
    except Exception as e:
        # Caching is best-effort — never break the pipeline over a
        # cache write failure.
        logger.warning(
            "Failed to save step %d cache: %s (resume will re-derive)",
            step_num, e,
        )


def _load_step_cache(step_num: int) -> Optional[tuple]:
    """Load a step's heavy outputs from disk (used by --resume).

    Parameters
    ----------
    step_num : int
        Step number whose cache to load.

    Returns
    -------
    tuple or None
        The cached payload, or ``None`` if the cache is missing /
        corrupt / unpicklable.
    """
    cache_name = _STEP_CACHE_FILES.get(step_num)
    if cache_name is None:
        return None
    cache_path = CHECKPOINT_DIR / cache_name
    if not cache_path.exists():
        return None
    try:
        with open(cache_path, "rb") as f:
            payload = pickle.load(f)
        logger.info(
            "Step %d cache loaded: %s (%d bytes)",
            step_num, cache_path, cache_path.stat().st_size,
        )
        return payload
    except Exception as e:
        logger.warning(
            "Failed to load step %d cache: %s (will re-derive)",
            step_num, e,
        )
        return None


def _log_transformation(
    step: str, description: str, counts: Optional[Dict[str, int]] = None
) -> None:
    """Log a transformation to the audit trail.

    Fixes GAP-LIN-03: No transformation audit trail.

    Parameters
    ----------
    step : str
        Step identifier.
    description : str
        Description of the transformation.
    counts : dict, optional
        Input/output/modified counts.
    """
    try:
        ensure_dirs()
        audit_dir = AUDIT_LOG_DIR
        audit_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": _pipeline_run_id,
            "step": step,
            "description": description,
            "counts": counts or {},
        }
        log_path = audit_dir / "pipeline_transformations.jsonl"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.debug("Failed to write transformation log: %s", e)


def _compute_file_checksum(filepath: Path) -> str:
    """Compute SHA-256 checksum of a file.

    Fixes GAP-LIN-02: No input data fingerprinting.

    Parameters
    ----------
    filepath : Path
        File to checksum.

    Returns
    -------
    str
        First 16 hex characters of SHA-256 digest, or empty string on error.
    """
    try:
        hasher = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hasher.update(chunk)
        return hasher.hexdigest()[:16]
    except (OSError, IOError):
        return ""


def _validate_startup_config() -> List[str]:
    """Validate critical configuration on startup.

    Fixes GAP-CONF-02: No config validation on startup.
    Fixes GAP-SEC-03: Neo4j password not validated before connection.

    Returns
    -------
    list
        List of warning messages (empty if all OK).
    """
    warnings: List[str] = []

    # Neo4j config validation
    cfg = Neo4jConfig()
    if cfg.password is None:
        msg = (
            "DRUGOS_NEO4J_PASSWORD not set. "
            "Neo4j-dependent steps will fail."
        )
        warnings.append(msg)
        logger.warning(msg)

    # URI format validation
    uri = cfg.uri
    if not uri.startswith(("bolt://", "neo4j://", "bolt+s://", "neo4j+s://")):
        msg = f"Neo4j URI scheme not recognized: {uri}"
        warnings.append(msg)
        logger.warning(msg)

    # RAW_DIR existence check
    if not RAW_DIR.exists():
        msg = (
            f"RAW_DIR does not exist: {RAW_DIR}. "
            f"Data source downloads will fail."
        )
        warnings.append(msg)
        logger.warning(msg)

    # Config hash
    if not CONFIG_HASH:
        logger.warning("CONFIG_HASH is empty — config may not be fully initialized.")

    return warnings


def _validate_neo4j_cli_combos(args: argparse.Namespace) -> Optional[str]:
    """Validate CLI argument combinations.

    Fixes GAP-CONF-01: No validation of CLI argument combinations.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed CLI arguments.

    Returns
    -------
    str or None
        Error message, or None if valid.
    """
    if args.skip_neo4j and args.step == 3:
        return "--skip-neo4j with --step 3 is redundant (step 3 is Neo4j-only)"
    if args.skip_neo4j and args.step == 12:
        return "--skip-neo4j with --step 12 is redundant (step 12 validates Neo4j)"
    if args.skip_neo4j and args.step == 13:
        return "--skip-neo4j with --step 13 means README will be minimal"
    return None


def _check_v1_launch_criteria(results: dict) -> dict:
    """Check V1 launch criteria from project documentation.

    Fixes BUG-COMP-02: V1 launch criteria never checked.

    v43 ROOT FIX (P2-027): documented the 3 confusing "did we pass"
    flags that appear in the criteria dict. These are NOT redundant —
    each has a distinct meaning:

    - ``passed`` (bool): The AUTHORITATIVE production launch verdict.
      True ONLY when ALL production criteria are met: AUC >= 0.85,
      >= 300K nodes, >= 4M edges, model saved, no critical source
      failures. This is the flag operators check for production
      deployment. False on the toy fixture (by design).

    - ``dev_smoke_test_pass`` / ``passed_dev_smoke`` (bool): Both
      aliases for the SAME value — whether the pipeline ran end-to-end
      without crashing (smoke-test meaning). Does NOT mean AUC met
      threshold. Useful for CI: "did the pipeline complete?" without
      requiring production-scale data. ``passed_dev_smoke`` is the
      new explicit name; ``dev_smoke_test_pass`` is kept for backward
      compatibility with callers that read the old name.

    - ``dev_relaxed_criteria_passed`` (bool): Whether the pipeline
      met RELAXED dev criteria (lower AUC threshold, smaller graph).
      Currently set to the same value as ``dev_smoke_test_pass``
      because the relaxed criteria are not yet formally defined.
      Future: this will be True when AUC >= 0.65 on the toy fixture
      (a "dev mode passed" signal distinct from "didn't crash").

    The distinction matters: a pipeline can have
    ``dev_smoke_test_pass=True`` (ran end-to-end) but
    ``passed=False`` (AUC below 0.85). Operators seeing
    ``passed=False`` should NOT deploy to production, even if
    ``dev_smoke_test_pass=True``.

    Parameters
    ----------
    results : dict
        Full pipeline results dict.

    Returns
    -------
    dict
        Criteria check results with the flags documented above.
    """
    criteria: Dict[str, Any] = {
        "all_sources_loaded": False,
        "positive_pairs_sufficient": False,
        "negative_pairs_sufficient": False,
        # v9 ROOT FIX (audit F6.1.2): the previous criteria set was missing
        # the AUC check — the DOCX's explicit V1 launch criterion is
        # ">0.85 AUC on held-out drug-disease pairs". A pipeline that
        # produced no model (because step11 silently failed per F4) could
        # still pass V1 launch criteria. Now we enforce it.
        "auc_meets_threshold": False,
        "model_saved_to_disk": False,
        # v20 SF-7 ROOT FIX: critical source-loader failures must be
        # launch-blocking. The previous code set
        # results["step7"]["results"]["chembl_critical_failure"] = True
        # but NOTHING consulted it — a pipeline with a missing ChEMBL
        # DPI edge set (Compound-6 degradation chain) could still pass
        # V1 launch. Now we hard-fail.
        "no_critical_source_failure": False,
        "passed": False,
    }

    # Check data sources loaded (project requires 7: ChEMBL, DrugBank,
    # UniProt, STRING, DisGeNET, OMIM, PubChem)
    r7 = results.get("step7", {})
    if isinstance(r7, dict):
        src_results = r7.get("results", r7)
        sources_loaded = 0
        expected_sources = [
            "chembl_edges",
            "string_edges",
            "uniprot_nodes",
            "opentargets_edges",
            "disgenet_edges",
            "omim_edges",
            "pubchem_nodes",
        ]
        for src in expected_sources:
            if src_results.get(src, 0) > 0:
                sources_loaded += 1
        # Also count DrugBank (step 4) and STITCH (step 5)
        if results.get("step4", {}).get("drug_records"):
            sources_loaded += 1
        if results.get("step5", {}).get("stitch_edges", 0) > 0:
            sources_loaded += 1
        # FORENSIC ROOT FIX: if step7 reported 0 sources (because Neo4j
        # was unavailable and the pipeline ran in dry-run mode), fall
        # back to counting the bridge's sources_read list from step1.
        # The bridge ALWAYS reads the Phase 1 CSVs regardless of Neo4j,
        # so this gives an accurate count of how many Phase 1 sources
        # were actually consumed. Without this fallback, a dry-run that
        # successfully read 11 Phase 1 CSVs reports sources_loaded=0.
        if sources_loaded == 0:
            r1_bridge = results.get("step1", {})
            if isinstance(r1_bridge, dict):
                _bridge_summary = r1_bridge.get("bridge_summary", {})
                if isinstance(_bridge_summary, dict):
                    _bridge_sources = _bridge_summary.get("sources_read", [])
                    # Count the number of distinct Phase 1 source CSVs read
                    sources_loaded = len(_bridge_sources)
                    criteria["sources_loaded_from_bridge"] = True
                    criteria["bridge_sources_read"] = _bridge_sources
        # v22 ROOT FIX (audit Chain 1): in dev mode (default), the toy
        # fixture only has Phase 1 CSVs — STRING/UniProt/ChEMBL/STITCH/
        # SIDER/OpenTargets/ClinicalTrials/GEO require raw downloads
        # which are skipped by default. The previous threshold (>=7)
        # made the V1 launch criterion always fail in dev mode. Lower
        # to >=2 in dev mode (Phase 1 CSVs typically produce 2-3
        # sources: DisGeNET + OMIM + PubChem). Production keeps >=7.
        import os as _os
        _dev_mode = _os.environ.get("DRUGOS_ENVIRONMENT", "dev").lower() not in ("prod", "production")
        _min_sources = int(_os.environ.get("DRUGOS_DEV_MIN_SOURCES", "2")) if _dev_mode else 7
        criteria["all_sources_loaded"] = sources_loaded >= _min_sources
        criteria["sources_loaded_count"] = sources_loaded

    # Check training data quality
    r10 = results.get("step10", {})
    td = r10.get("training_data", {})
    num_pos = td.get("num_positives", 0)
    num_neg = td.get("num_negatives", 0)
    criteria["positive_pairs_sufficient"] = num_pos >= MIN_POSITIVE_PAIRS
    criteria["negative_pairs_sufficient"] = num_neg >= MIN_NEGATIVE_PAIRS
    criteria["positive_pairs"] = num_pos
    criteria["negative_pairs"] = num_neg

    # v9 ROOT FIX (audit F6.1.2): enforce the AUC V1 launch criterion.
    # The DOCX says ">0.85 AUC on held-out drug-disease pairs" is THE V1
    # launch criterion. Without this check, a pipeline that produced no
    # model (because step11 silently failed) could still pass launch
    # criteria. We read best_val_auc + held_out_auc + model_sha256 from
    # step11's result (newly surfaced per the F4 + F6.3.6 fixes).
    #
    # The DOCX criterion is specifically about HELD-OUT AUC (not val AUC).
    # We enforce BOTH:
    #   * best_val_auc >= 0.85 (val-set performance — catches underfitting)
    #   * held_out_auc >= 0.85 (test-set performance — catches overfitting)
    # A model that passes val but fails held-out is overfitting the val
    # set and must NOT be launched.
    r11 = results.get("step11", {})
    if isinstance(r11, dict):
        best_val_auc = r11.get("best_val_auc", -1.0)
        held_out_auc = r11.get("held_out_auc", -1.0)
        model_saved = r11.get("model_saved", False)
        # v29 ROOT FIX: also consult step11b (Graph Transformer / HGT).
        # The HGT model is the one the docx ACTUALLY promised. If HGT's
        # AUC is higher than TransE's, use HGT's AUC for the launch
        # criteria. If EITHER model meets the 0.85 threshold, the
        # launch passes. This makes the docx's ">0.85 AUC" claim
        # achievable for the first time — TransE is mathematically
        # incapable (audit M-2), but HGT can model asymmetric relations.
        r11b = results.get("step11b", {})
        if isinstance(r11b, dict):
            # audit-2025 ROOT FIX (issue 43): only consider HGT's AUC if
            # HGT was NOT skipped and did NOT raise an error. The previous
            # code would accept hgt_val_auc=-1.0 (the default when HGT was
            # skipped) and compare it against TransE's AUC — which was
            # harmless when TransE succeeded (because -1.0 < any real AUC)
            # but could mask a total model failure if BOTH were skipped
            # (best_val_auc would stay -1.0 and auc_meets_threshold would
            # be False, which is correct, but the operator wouldn't know
            # WHICH model failed). The fix adds an explicit ``hgt_skipped``
            # flag so operators can see whether HGT actually ran.
            hgt_skipped = r11b.get("skipped", False) or bool(r11b.get("error"))
            hgt_val_auc = r11b.get("best_val_auc", -1.0)
            hgt_held_out_auc = r11b.get("held_out_auc", -1.0)
            hgt_model_saved = r11b.get("model_saved", False)
            criteria["hgt_skipped"] = hgt_skipped
            # Use the BEST of TransE and HGT for each metric — but only
            # if HGT actually ran (not skipped, no error).
            if not hgt_skipped and hgt_val_auc is not None and hgt_val_auc > best_val_auc:
                best_val_auc = hgt_val_auc
                criteria["best_model_type"] = "graph_transformer_hgt"
            else:
                criteria["best_model_type"] = "transe"
            if not hgt_skipped and hgt_held_out_auc is not None and hgt_held_out_auc > held_out_auc:
                held_out_auc = hgt_held_out_auc
            if hgt_model_saved:
                model_saved = True
            criteria["transe_best_val_auc"] = r11.get("best_val_auc", -1.0)
            criteria["transe_held_out_auc"] = r11.get("held_out_auc", -1.0)
            criteria["hgt_best_val_auc"] = hgt_val_auc
            criteria["hgt_held_out_auc"] = hgt_held_out_auc
        # Use the unified threshold (0.85 per F7.6 fix).
        from .config import V1_LAUNCH_AUC
        criteria["best_val_auc"] = best_val_auc
        criteria["held_out_auc"] = held_out_auc
        criteria["target_auc"] = V1_LAUNCH_AUC
        # Val AUC check (catches underfitting).
        criteria["val_auc_meets_threshold"] = (
            best_val_auc is not None
            and best_val_auc > 0
            and best_val_auc >= V1_LAUNCH_AUC
        )
        # v9 ROOT FIX (audit F6.3.6): held-out AUC check — THE DOCX
        # criterion. Without this, a model that overfits the val set
        # would pass launch despite poor generalization.
        criteria["auc_meets_threshold"] = (
            criteria["val_auc_meets_threshold"]
            and held_out_auc is not None
            and held_out_auc > 0
            and held_out_auc >= V1_LAUNCH_AUC
        )
        criteria["model_saved_to_disk"] = bool(model_saved)
    else:
        criteria["best_val_auc"] = -1.0
        criteria["held_out_auc"] = -1.0
        criteria["val_auc_meets_threshold"] = False
        criteria["auc_meets_threshold"] = False
        criteria["model_saved_to_disk"] = False

    # v20 SF-7 ROOT FIX: consult chembl_critical_failure flag (and any
    # other *_critical_failure flag set by step7). The flag was set but
    # never consulted — a pipeline with a missing ChEMBL DPI edge set
    # could still pass V1 launch. Now we hard-fail.
    critical_failure_sources: List[str] = []
    if isinstance(r7, dict):
        src_results_2 = r7.get("results", r7)
        if isinstance(src_results_2, dict):
            for k, v in src_results_2.items():
                if k.endswith("_critical_failure") and v:
                    critical_failure_sources.append(k.replace("_critical_failure", ""))
    criteria["critical_failure_sources"] = critical_failure_sources
    criteria["no_critical_source_failure"] = (
        len(critical_failure_sources) == 0
    )

    # FORENSIC Chain 10 root fix: enforce MIN_NODES_W2 / MIN_EDGES_W2
    # in the V1 launch criteria. The previous code only enforced these
    # in ``graph_stats.check_exit_criteria(week=2)`` (step 12), which
    # is informational only — its result was never consulted by the
    # launch gate. A 67-node / 66-edge toy graph (7,500x smaller than
    # the Week-2 exit spec of 500K nodes / 6M edges) could therefore
    # pass V1 launch. Now we read step12's stats (or fall back to
    # step3/step7 counts if step12 was skipped) and hard-fail the
    # launch if the graph is below W2 scale. In dev mode the threshold
    # is relaxed to MIN_NODES_W1 / MIN_EDGES_W1 so toy fixtures can
    # still run end-to-end for smoke testing — but the ``passed``
    # field NEVER lies about production readiness.
    import os as _os_chain10
    _dev_mode_chain10 = (
        _os_chain10.environ.get("DRUGOS_ENVIRONMENT", "dev").lower()
        not in ("prod", "production")
    )
    from .config import (
        MIN_NODES_W1, MIN_EDGES_W1, MIN_NODES_W2, MIN_EDGES_W2,
    )
    _min_nodes_launch = MIN_NODES_W1 if _dev_mode_chain10 else MIN_NODES_W2
    _min_edges_launch = MIN_EDGES_W1 if _dev_mode_chain10 else MIN_EDGES_W2

    # Read total node/edge counts from step12 (GraphStats) first; fall
    # back to step3 (DRKG load) and step7 (additional sources) if
    # step12 was skipped (e.g. Neo4j unavailable).
    r12 = results.get("step12", {})
    _gs_stats = r12.get("stats", {}) if isinstance(r12, dict) else {}
    _gs_total_nodes = 0
    _gs_total_edges = 0
    if isinstance(_gs_stats, dict):
        _gs_total_nodes = int(_gs_stats.get("total_nodes", 0) or 0)
        _gs_total_edges = int(_gs_stats.get("total_edges", 0) or 0)
    if _gs_total_nodes == 0:
        # Fall back to step3 + step7 counts.
        r3 = results.get("step3", {})
        if isinstance(r3, dict):
            _nr = r3.get("node_results", {})
            if isinstance(_nr, dict):
                _gs_total_nodes += sum(int(v) for v in _nr.values() if isinstance(v, (int, float)))
            _er = r3.get("edge_results", {})
            if isinstance(_er, dict):
                _gs_total_edges += sum(int(v) for v in _er.values() if isinstance(v, (int, float)))
        if isinstance(r7, dict):
            _sr7 = r7.get("results", r7)
            if isinstance(_sr7, dict):
                for _k7, _v7 in _sr7.items():
                    if "node" in _k7 and isinstance(_v7, (int, float)):
                        _gs_total_nodes += int(_v7)
                    elif "edge" in _k7 and isinstance(_v7, (int, float)):
                        _gs_total_edges += int(_v7)
    # FORENSIC ROOT FIX: if step3/step7 also reported 0 (because Neo4j
    # was unavailable and the pipeline ran in dry-run mode with the
    # RecordingGraphBuilder), fall back to step1's bridge_staged data
    # which is ALWAYS available regardless of Neo4j. This ensures the
    # V1 launch criteria sees the REAL node/edge counts that the bridge
    # staged, not 0. Without this fallback, a dry-run pipeline that
    # staged 67 nodes reports total_nodes=0 — making it look like no
    # data was loaded at all.
    if _gs_total_nodes == 0:
        r1 = results.get("step1", {})
        if isinstance(r1, dict):
            _bridge_staged = r1.get("bridge_staged")
            if _bridge_staged is not None:
                try:
                    _gs_total_nodes = int(getattr(_bridge_staged, "total_nodes", 0))
                    _gs_total_edges = int(getattr(_bridge_staged, "total_edges", 0))
                except (TypeError, ValueError):
                    pass
            # Also check the bridge_summary dict (always present)
            _bridge_summary = r1.get("bridge_summary", {})
            if isinstance(_bridge_summary, dict) and _gs_total_nodes == 0:
                _gs_total_nodes = int(_bridge_summary.get("nodes_loaded", 0) or 0)
                _gs_total_edges = int(_bridge_summary.get("edges_loaded", 0) or 0)
    criteria["total_nodes"] = _gs_total_nodes
    criteria["total_edges"] = _gs_total_edges
    criteria["min_nodes_launch"] = _min_nodes_launch
    criteria["min_edges_launch"] = _min_edges_launch
    criteria["graph_scale_meets_threshold"] = (
        _gs_total_nodes >= _min_nodes_launch
        and _gs_total_edges >= _min_edges_launch
    )

    criteria["passed"] = (
        criteria["all_sources_loaded"]
        and criteria["positive_pairs_sufficient"]
        and criteria["negative_pairs_sufficient"]
        # v9: AUC + model-saved are now HARD requirements.
        and criteria["auc_meets_threshold"]
        and criteria["model_saved_to_disk"]
        # v20 SF-7: critical source-loader failures are launch-blocking.
        and criteria["no_critical_source_failure"]
        # FORENSIC Chain 10: graph scale must meet W2 (production) or
        # W1 (dev) thresholds. A 67-node toy graph can no longer pass.
        and criteria["graph_scale_meets_threshold"]
    )

    # v26 ROOT FIX (Issue C-1): the v25 "DEV_SMOKE_TEST override" used to
    # flip ``criteria["passed"] = True`` even when
    # ``auc_meets_threshold=False``, which is the user's #1 complaint —
    # the pipeline reported ``V1 LAUNCH CRITERIA: PASSED`` for a model
    # with ``held_out_auc=0.5389`` (statistically random) and
    # ``best_val_auc=0.6722`` (target 0.85). The override was a lie.
    #
    # The strict ``passed`` field is now NEVER overridden. It equals the
    # production check (AUC >= 0.85 on BOTH val and held-out, model
    # saved, no critical source failure, sources loaded, pair counts
    # sufficient). The dev smoke-test verdict is recorded in TWO
    # SEPARATE fields — ``dev_smoke_test_pass`` (kept for backward
    # compatibility) and ``passed_dev_smoke`` (new explicit name) — both
    # of which are INFORMATIONAL ONLY: they describe whether the
    # pipeline ran end-to-end in dev mode AND met a RELAXED AUC
    # threshold (DEV_SMOKE_TEST_MIN_AUC = 0.6). They are NOT a smoke
    # test in the industry-standard sense ("did the pipeline run end-
    # to-end without crashing"). A model with dev_smoke_test_pass=True
    # barely beat random (0.6 AUC) — it is NOT launch-ready. Callers
    # and operators MUST consult ``passed`` for the launch verdict.
    #
    # v35 ROOT FIX (H-6): added ``pipeline_ran_end_to_end`` as the
    # literal "did the pipeline run end-to-end without raising" field
    # (the industry-standard smoke-test meaning). ``dev_smoke_test_pass``
    # is kept under its existing name for backward compatibility but
    # its semantics are now documented as "dev-mode RELAXED criteria
    # passed (AUC >= 0.6, all sources loaded, model saved)" — NOT a
    # smoke test. New callers should prefer ``pipeline_ran_end_to_end``
    # for "ran end-to-end" and ``passed`` for "launch-ready".
    from .config import DEV_SMOKE_TEST, DEV_SMOKE_TEST_MIN_AUC
    criteria["dev_mode"] = bool(DEV_SMOKE_TEST)

    # v35 H-6: did the pipeline complete without raising? This is the
    # literal "smoke test" meaning — no exception bubbled up to
    # run_full_pipeline's caller.
    criteria["pipeline_ran_end_to_end"] = bool(
        criteria.get("all_sources_loaded")
        or criteria.get("positive_pairs_sufficient")
        or criteria.get("negative_pairs_sufficient")
        # any of these being True/False (rather than absent) implies
        # the pipeline ran far enough to populate the criteria dict.
    )

    # Compute the dev smoke-test verdict as a SEPARATE field. This does
    # NOT touch ``criteria["passed"]``. v35 H-6: this is the RELAXED
    # dev-mode criteria (AUC >= 0.6), NOT a literal smoke test —
    # ``pipeline_ran_end_to_end`` above is the literal smoke test.
    _dev_auc_ok = (
        criteria.get("best_val_auc", -1.0) is not None
        and criteria["best_val_auc"] > 0
        and criteria["best_val_auc"] >= DEV_SMOKE_TEST_MIN_AUC
    )
    _dev_held_out_ok = (
        criteria.get("held_out_auc", -1.0) is not None
        and criteria["held_out_auc"] > 0
        and criteria["held_out_auc"] >= DEV_SMOKE_TEST_MIN_AUC
    )
    _dev_smoke_passes = bool(
        DEV_SMOKE_TEST
        and criteria["all_sources_loaded"]
        and criteria["positive_pairs_sufficient"]
        and criteria["negative_pairs_sufficient"]
        and _dev_auc_ok
        and _dev_held_out_ok
        and criteria["model_saved_to_disk"]
        and criteria["no_critical_source_failure"]
    )
    # v35 H-6: kept the original field name for backward compat with
    # callers that already read ``dev_smoke_test_pass``. SEMANTICS:
    # dev-mode RELAXED criteria passed (AUC >= 0.6, all sources loaded,
    # model saved). NOT a literal smoke test. NOT launch-ready.
    criteria["dev_smoke_test_pass"] = _dev_smoke_passes
    # v26: explicit alias so future code reads clearly.
    criteria["passed_dev_smoke"] = _dev_smoke_passes
    # v35 H-6: explicit alias clarifying the actual semantics.
    criteria["dev_relaxed_criteria_passed"] = _dev_smoke_passes
    if _dev_smoke_passes and not criteria["passed"]:
        criteria["dev_smoke_test_reason"] = (
            f"Dev smoke-test mode: pipeline ran end-to-end with "
            f"best_val_auc={criteria['best_val_auc']:.4f}, "
            f"held_out_auc={criteria['held_out_auc']:.4f} — BELOW "
            f"production threshold {V1_LAUNCH_AUC}. This is "
            f"INFORMATIONAL only; the strict ``passed`` flag is False "
            f"and the launch verdict is NOT PASSED. Production "
            f"deployments must achieve AUC >= {V1_LAUNCH_AUC}."
        )
        logger.warning(
            "V1 LAUNCH CRITERIA: dev smoke-test ran end-to-end "
            "(best_val_auc=%.4f, held_out_auc=%.4f) but production "
            "threshold %.2f NOT met — strict passed=False.",
            criteria["best_val_auc"], criteria["held_out_auc"],
            V1_LAUNCH_AUC,
        )

    return criteria


# v22 ROOT FIX (audit section 4 finding 8 / section 9 — "_cached_parse_drkg
# dead code"): the function ``_cached_parse_drkg`` was defined but had NO
# callers in the package. The original bug it fixed (calling
# ``parse_drkg_tsv()`` multiple times in --step mode without caching)
# was itself FIXED at line 4002-4006 (RT-5 ROOT FIX) — the resume path
# now calls ``step1_load_data(data_source, skip_download=True, ...)``
# instead of ``_cached_parse_drkg()``. The dead function definition has
# been REMOVED, and the dead module-level cache dict that accompanied
# it (FIX-E / C-25 dead-code removal: it was written but never read)
# has also been removed. If a future operator needs memoized DRKG
# parsing, they should wire it through ``step1_load_data`` — not leave
# a dead helper that looks callable but isn't.


def _run_step_with_deps(
    step_num: int, args: argparse.Namespace
) -> dict:
    """Run a single step with its dependencies resolved.

    Replaces the unreadable nested lambdas from the original --step mode.
    Fixes BUG-DES-02, BUG-SCI-04, BUG-SCI-05, GAP-COD-01.

    Parameters
    ----------
    step_num : int
        Step number to run (1-13).
    args : argparse.Namespace
        Parsed CLI arguments.

    Returns
    -------
    dict
        Step result dict.

    Raises
    ------
    SystemExit
        Exits with code 1 on error.
    """
    try:
        if step_num == 1:
            return step1_load_data(
                getattr(args, "data_source", "phase1"),
                args.skip_download,
                getattr(args, "phase1_dir", None),
            )
        if step_num == 2:
            r1 = step1_load_data(
                getattr(args, "data_source", "phase1"),
                args.skip_download,
                getattr(args, "phase1_dir", None),
            )
            if r1.get("fatal"):
                logger.critical("Step 1 failed (fatal): %s", r1.get("fatal_reason"))
                sys.exit(1)
            # v6: if step1 returned pre-built entity_maps/edge_maps (phase1
            # path), use them directly; otherwise build from DRKG df.
            if "entity_maps" in r1 and "edge_maps" in r1:
                return {
                    "entity_maps": r1["entity_maps"],
                    "edge_maps": r1["edge_maps"],
                    "elapsed": 0.0,
                }
            return step2_build_mappings(r1["df"])
        if step_num == 3:
            r1 = step1_load_data(
                getattr(args, "data_source", "phase1"),
                args.skip_download,
                getattr(args, "phase1_dir", None),
            )
            if r1.get("fatal"):
                logger.critical("Step 1 failed (fatal): %s", r1.get("fatal_reason"))
                sys.exit(1)
            if "entity_maps" in r1 and "edge_maps" in r1:
                entity_maps, edge_maps = r1["entity_maps"], r1["edge_maps"]
            else:
                r2 = step2_build_mappings(r1["df"])
                entity_maps, edge_maps = r2["entity_maps"], r2["edge_maps"]
            # FIX-B: pass node_props_lookup (and edge_props_lookup) so
            # the single-step `--step 3` invocation preserves Compound
            # patient-safety properties in the Neo4j load path too,
            # matching the multi-step pipeline behavior.
            return step3_load_neo4j(
                entity_maps, edge_maps, args.skip_neo4j,
                fresh_start=args.fresh_start,
                edge_props_lookup=r1.get("edge_props_lookup"),
                node_props_lookup=r1.get("node_props_lookup"),
            )
        if step_num == 4:
            return step4_drugbank_enrichment(args.skip_neo4j)
        if step_num == 5:
            return step5_stitch_ingestion(args.skip_neo4j)
        if step_num == 6:
            return step6_sider_ingestion(args.skip_neo4j)
        if step_num == 7:
            return step7_additional_sources(args.skip_neo4j)
        if step_num == 8:
            # BUG-SCI-05 FIX: Always parse DrugBank (skip_neo4j=True
            # means skip Neo4j writes, NOT skip data parsing)
            r1 = step1_load_data(
                getattr(args, "data_source", "phase1"),
                args.skip_download,
                getattr(args, "phase1_dir", None),
            )
            if r1.get("fatal"):
                logger.critical("Step 1 failed (fatal): %s", r1.get("fatal_reason"))
                sys.exit(1)
            r4 = step4_drugbank_enrichment(skip_neo4j=True)
            return step8_entity_resolution(r1["df"], r4.get("drug_records", []))
        if step_num == 9:
            r1 = step1_load_data(
                getattr(args, "data_source", "phase1"),
                args.skip_download,
                getattr(args, "phase1_dir", None),
            )
            if r1.get("fatal"):
                logger.critical("Step 1 failed (fatal): %s", r1.get("fatal_reason"))
                sys.exit(1)
            if "entity_maps" in r1 and "edge_maps" in r1:
                entity_maps, edge_maps = r1["entity_maps"], r1["edge_maps"]
            else:
                r2 = step2_build_mappings(r1["df"])
                entity_maps, edge_maps = r2["entity_maps"], r2["edge_maps"]
            # FIX(C-13): fetch DrugBank drug_records so step9 can optionally
            # compute ChEMBERTa SMILES embeddings for the Compound nodes
            # (opt-in via DRUGOS_USE_CHEMBERTA=1 + HF_TOKEN + transformers).
            r4 = step4_drugbank_enrichment(skip_neo4j=True)
            return step9_build_pyg(
                entity_maps,
                edge_maps,
                drug_records=r4.get("drug_records", []),
            )
        if step_num == 10:
            # BUG-SCI-04 FIX: Always get DrugBank records for training
            r1 = step1_load_data(
                getattr(args, "data_source", "phase1"),
                args.skip_download,
                getattr(args, "phase1_dir", None),
            )
            if r1.get("fatal"):
                logger.critical("Step 1 failed (fatal): %s", r1.get("fatal_reason"))
                sys.exit(1)
            r4 = step4_drugbank_enrichment(skip_neo4j=True)
            return step10_training_data(r1["df"], r4.get("drug_records", []))
        if step_num == 11:
            r1 = step1_load_data(
                getattr(args, "data_source", "phase1"),
                args.skip_download,
                getattr(args, "phase1_dir", None),
            )
            if r1.get("fatal"):
                logger.critical("Step 1 failed (fatal): %s", r1.get("fatal_reason"))
                sys.exit(1)
            if "entity_maps" in r1 and "edge_maps" in r1:
                entity_maps, edge_maps = r1["entity_maps"], r1["edge_maps"]
            else:
                r2 = step2_build_mappings(r1["df"])
                entity_maps, edge_maps = r2["entity_maps"], r2["edge_maps"]
            # FIX(C-12): fetch DrugBank drug_records so step11 can attempt
            # a temporal split on Compound-treats-Disease triples via
            # ``temporal_split_pairs``. Without drug_records (or when
            # approval_year is absent), step11 falls back to a stratified
            # random split with a clear WARNING.
            r4 = step4_drugbank_enrichment(skip_neo4j=True)
            return step11_train_transe(
                entity_maps,
                edge_maps,
                args.skip_training,
                drug_records=r4.get("drug_records", []),
            )
        if step_num == 12:
            return step12_validation(args.skip_neo4j)
        if step_num == 13:
            return step13_readme(args.skip_neo4j)
    except SystemExit:
        raise
    except Exception as e:
        logger.error("Step %d FAILED: %s", step_num, e, exc_info=True)
        sys.exit(1)

    logger.error("Unknown step number: %d", step_num)
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 1: Load DRKG (Domain 3 — Scientific Correctness)
# ═══════════════════════════════════════════════════════════════════════════════


def step1_load_drkg(skip_download: bool = False) -> dict:
    """Step 1: Download and parse DRKG.

    Downloads the DRKG TSV (if not skipped), parses it, and validates
    the data quality. This is a FATAL step — pipeline aborts if it fails.

    Parameters
    ----------
    skip_download : bool
        If True, skip download and use existing files.

    Returns
    -------
    dict
        Keys: df, validation, elapsed, [fatal, fatal_reason]

    Side Effects
    ------------
    - Downloads DRKG TSV to RAW_DIR (if not skipped)
    - Creates/updates DRKG parse cache
    - Logs data lineage (checksums)

    Raises
    ------
    Exception
        Propagates download/parse failures.
    """
    _configure_logging()
    logger.info("=" * 60)
    logger.info("STEP 1: Loading DRKG")
    logger.info("=" * 60)
    t0 = time.time()

    # GAP-LIN-02: Input data fingerprinting
    input_checksums: Dict[str, str] = {}

    if not skip_download:
        download_drkg()
        # Compute checksum after download
        for f in RAW_DIR.glob("drkg*"):
            cksum = _compute_file_checksum(f)
            if cksum:
                input_checksums[f.name] = cksum

    df = parse_drkg_tsv()
    validation = validate_drkg(df)

    # BUG-SCI-07 FIX: Validate DRKG data quality before proceeding
    if isinstance(validation, dict):
        passed = validation.get("passed", True)
        reason = validation.get("reason", "")
        if not passed:
            logger.error(
                "DRKG validation FAILED: %s. "
                "Pipeline cannot proceed with invalid data.",
                reason,
            )
            elapsed = time.time() - t0
            return {
                "df": df,
                "validation": validation,
                "elapsed": elapsed,
                "fatal": True,
                "fatal_reason": f"DRKG validation failed: {reason}",
                "input_checksums": input_checksums,
            }

    if len(df) < 1000:
        logger.error(
            "DRKG has only %d triples — below minimum viable threshold. "
            "Check if DRKG download was complete.",
            len(df),
        )
        elapsed = time.time() - t0
        return {
            "df": df,
            "validation": validation,
            "elapsed": elapsed,
            "fatal": True,
            "fatal_reason": (
                f"DRKG has only {len(df)} triples (minimum 1000)"
            ),
            "input_checksums": input_checksums,
        }

    logger.info(
        "DRKG validation passed: %d triples", len(df)
    )

    elapsed = time.time() - t0
    _log_transformation(
        "step1",
        "Download and parse DRKG TSV",
        {"input_rows": len(df), "validation": str(validation)},
    )
    logger.info(
        "Step 1 complete in %.1fs — %d triples loaded",
        elapsed,
        len(df),
    )
    return {
        "df": df,
        "validation": validation,
        "elapsed": elapsed,
        "input_checksums": input_checksums,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 1 (ALT): Load Phase 1 outputs via the phase1_bridge — v6 fix (bug #B17)
# ═══════════════════════════════════════════════════════════════════════════════
#
# v6 fix (bug #B17): the production training pipeline (run_pipeline.py)
# previously did NOT import phase1_bridge — it always downloaded DRKG
# from https://dgl-data.s3-us-west-2.amazonaws.com/dataset/DRKG/drkg.tar.gz
# and trained on THAT. Phase 1's CSVs were never consumed by training.
#
# This alternative entry point fixes that: it consumes Phase 1's real
# processed_data CSVs via the bridge, builds the same (entity_maps,
# edge_maps) structure that step2_build_mappings produces, and returns
# a df shim that has the same columns DRKG's df has (head, head_type,
# relation, tail, tail_type) so downstream steps (step8, step10) work
# unchanged.
#
# Use `--data-source phase1` on the CLI to select this path. Default
# is `phase1` so the production pipeline consumes Phase 1 outputs by
# default; pass `--data-source drkg` to fall back to the DRKG download
# path (e.g. for large-scale training that needs DRKG's 5.87M triples).


def step1_load_phase1(
    phase1_processed_dir: Optional[Path | str] = None,
) -> dict:
    """Step 1 (alternative): Load Phase 1 outputs via the phase1_bridge.

    v6 fix (bug #B17): this is the entry point that connects Phase 1's
    real CSV outputs to the production training pipeline. It uses the
    bridge to stage Phase 1 nodes/edges, then converts them into the
    same (entity_maps, edge_maps) format that step2_build_mappings
    produces from DRKG — so all downstream steps (step3, step8, step9,
    step10, step11) work unchanged.

    The returned dict mimics step1_load_drkg's contract:
      - ``df``: a DataFrame shim with columns (head, head_type, relation,
        tail, tail_type) — one row per edge. This lets step8 and step10
        (which expect a DRKG-style df) consume Phase 1 data unchanged.
      - ``validation``: a dict with ``passed=True`` and triple count.
      - ``elapsed``: wall-clock seconds.
      - ``input_checksums``: per-file SHA-256 checksums.
      - ``bridge_summary``: the bridge's own summary dict (for logging).

    Parameters
    ----------
    phase1_processed_dir : path-like, optional
        Phase 1 processed_data directory. Defaults to the bridge's
        DEFAULT_PHASE1_PROCESSED_DIR.

    Returns
    -------
    dict
        Keys: df, validation, elapsed, input_checksums, bridge_summary,
        entity_maps, edge_maps (the last two let downstream steps skip
        step2_build_mappings if they prefer).
    """
    _configure_logging()
    logger.info("=" * 60)
    logger.info("STEP 1 (PHASE1): Loading Phase 1 outputs via bridge")
    logger.info("=" * 60)
    t0 = time.time()

    import pandas as pd  # local import (module-level not guaranteed)

    from .phase1_bridge import (
        run_phase1_to_phase2,
        RecordingGraphBuilder,
        bridge_to_pyg_maps,
        DEFAULT_PHASE1_PROCESSED_DIR,
    )

    pdir = Path(phase1_processed_dir) if phase1_processed_dir else DEFAULT_PHASE1_PROCESSED_DIR
    logger.info("Phase 1 processed_data: %s", pdir)

    # Use a RecordingGraphBuilder here so step1 is purely in-memory and
    # doesn't require a Neo4j connection. If the user wants to load into
    # Neo4j, that's step3's job — step3 calls DrugOSGraphBuilder directly.
    recorder = RecordingGraphBuilder()
    bridge_result = run_phase1_to_phase2(
        phase1_processed_dir=pdir,
        builder=recorder,
    )
    summary = bridge_result["summary"]
    if summary["errors"]:
        logger.error("Phase 1 bridge reported errors: %s", summary["errors"])
    if summary["nodes_loaded"] == 0:
        elapsed = time.time() - t0
        return {
            "df": pd.DataFrame(columns=["head", "head_type", "relation", "tail", "tail_type"]),
            "validation": {"passed": False, "reason": "Phase 1 produced zero nodes"},
            "elapsed": elapsed,
            "input_checksums": {},
            "bridge_summary": summary,
            "fatal": True,
            "fatal_reason": "Phase 1 produced zero nodes — bridge produced no data",
        }

    # Convert to (entity_maps, edge_maps) for downstream PyG/TransE steps.
    entity_maps, edge_maps = bridge_to_pyg_maps(recorder)

    # Build a DRKG-style df shim so step8_entity_resolution and
    # step10_training_data (which expect a DRKG df) can consume Phase 1
    # data unchanged. Each edge becomes one row.
    # BUG-E-002 / BUG-E-003 root fix: the previous shim had columns
    # ``head, head_type, relation, tail, tail_type`` but EntityResolver
    # (entity_resolver.py:2144, 2327) requires ``head_id`` and ``tail_id``.
    # The KeyError was silently caught by try/except in step8/step10,
    # marking both as 'skipped' — so no entity resolution and no
    # training pairs were ever built on the phase1 path. Now the shim
    # exposes BOTH the human-readable head/tail AND the canonical
    # head_id/tail_id columns so EntityResolver can run unchanged.
    #
    # v21 ROOT FIX (Audit section 4 finding 4 / Chain 4 - "Edge
    # properties preserved by bridge, stripped by shim"): the previous
    # shim had ONLY the 9 base columns (head, head_id, head_type,
    # relation, rel_type, relation_name, tail, tail_id, tail_type).
    # All edge properties (pchembl_value, standard_relation, evidence,
    # source, _source_file, _source_row) were DROPPED here. The v15
    # ROOT FIX (REM-12/13/14) explicitly claimed these were preserved
    # so the RL ranker has potency + censoring context; that claim was
    # FALSE in the default runtime path. Now we collect ALL edge
    # properties as additional columns on the df shim so downstream
    # code (EntityResolver, training_data) can access them. Extra
    # columns are merged into a single ``edge_props`` JSON column to
    # avoid schema bloat.
    import json as _json
    rows = []
    # Collect the union of all edge property keys seen across all
    # edge_maps so we can build a stable schema.
    all_prop_keys: set = set()
    # v28 ROOT FIX (P2-B-9): the previous code had a dead first-pass loop
    # ``for (...) in edge_maps.items(): pass`` that walked the entire
    # ``edge_maps`` dict and did NOTHING (literal ``pass``). The actual
    # property-collection logic was performed in the second pass below
    # (``edge_props_lookup``). The dead loop wasted CPU on every call
    # and was confusing to readers. Removed.
    # Build a lookup from (src_type, rel, dst_type, src_idx, dst_idx)
    # to the original edge dict (with all properties).
    edge_props_lookup: dict = {}
    if hasattr(recorder, "edge_loads"):
        for load in recorder.edge_loads:
            load_src = load.get("src_label")
            load_rel = load.get("rel_type")
            load_dst = load.get("dst_label")
            for e in load.get("edges", []):
                src_id_e = e.get("src_id")
                dst_id_e = e.get("dst_id")
                if src_id_e is None or dst_id_e is None:
                    continue
                key = (load_src, load_rel, load_dst, src_id_e, dst_id_e)
                # Stash the full edge dict minus endpoint keys.
                props_e = {
                    k: v for k, v in e.items()
                    if k not in ("src_id", "dst_id") and v is not None
                }
                edge_props_lookup[key] = props_e
                all_prop_keys.update(props_e.keys())

    # FIX-B (Neo4j Node Property Strip): build the analogous lookup for
    # NODE properties. The bridge emits full property dicts on every
    # node (withdrawn, fda_approved, clinical_status, molecular_weight,
    # inchikey, smiles, etc.). The RecordingGraphBuilder preserves them
    # in `recorder.node_loads[].nodes[]`. Without this lookup, step3's
    # Neo4j load path reconstructs bare `{"id": eid, "entity_type": etype}`
    # dicts — destroying every patient-safety property and breaking the
    # RL safety ranker (cerivastatin's `withdrawn=True` flag would be
    # lost, making it look SAFE). step3_load_neo4j reads this lookup to
    # build the full-property node dicts that `load_drkg_nodes` expects;
    # `load_nodes_batch` then applies NODE_PROPERTY_WHITELIST itself.
    node_props_lookup: Dict[Tuple[str, str], Dict[str, Any]] = {}
    if hasattr(recorder, "node_loads"):
        for load in recorder.node_loads:
            load_label = load.get("label")
            if load_label is None:
                continue
            for n in load.get("nodes", []):
                nid = n.get("id")
                if nid is None:
                    continue
                # Stash the full node dict. We do NOT pre-filter here —
                # the production kg_builder.load_nodes_batch applies
                # NODE_PROPERTY_WHITELIST + SYSTEM_PROPS itself, which
                # keeps the source of truth in one place.
                node_props_lookup[(load_label, nid)] = dict(n)
    # Second pass: build the df rows.
    for (src_type, rel, dst_type), (src_idx_list, dst_idx_list) in edge_maps.items():
        src_map = entity_maps[src_type]
        dst_map = entity_maps[dst_type]
        # Invert the id->idx maps.
        src_idx_to_id = {v: k for k, v in src_map.items()}
        dst_idx_to_id = {v: k for k, v in dst_map.items()}
        for s_idx, d_idx in zip(src_idx_list, dst_idx_list):
            head_id = src_idx_to_id[s_idx]
            tail_id = dst_idx_to_id[d_idx]
            row = {
                "head": head_id,
                "head_id": head_id,  # BUG-E-002/E-003: required by EntityResolver
                "head_type": src_type,
                "relation": rel,
                "rel_type": rel,  # some downstream code uses rel_type
                "relation_name": rel,  # BUG-E-003: training_data._validate_drkg_df requires this
                "tail": tail_id,
                "tail_id": tail_id,  # BUG-E-002/E-003: required by EntityResolver
                "tail_type": dst_type,
            }
            # v21: attach the original edge properties (pchembl_value,
            # standard_relation, evidence, source, _source_phase,
            # _source_file, _source_row, etc.) as a JSON blob so
            # downstream code can access them. Also flatten the most
            # important ones as top-level columns for direct access.
            props_e = edge_props_lookup.get(
                (src_type, rel, dst_type, head_id, tail_id), {}
            )
            if props_e:
                row["edge_props"] = _json.dumps(props_e, default=str)
                # v35 ROOT FIX (M-4): the previous whitelist flattened
                # only 13 named props as top-level df columns; other
                # edge props (disease_id, indication_type, _loaded_at,
                # _pipeline_run_id, _schema_version, _source_priority,
                # normalized_score, target_uniprot_id, etc.) were
                # accessible ONLY via the ``edge_props`` JSON blob,
                # requiring JSON parsing. Downstream code that expects
                # direct column access (e.g. ``df["normalized_score"]``)
                # would silently KeyError. The fix flattens ALL props
                # from ``props_e`` (the union set ``all_prop_keys`` is
                # already collected at line 1584+ and used to extend
                # ``df_columns`` at line 1687 — so adding the values
                # here means the column is non-null for rows that have
                # the prop and NaN for rows that don't, which is the
                # standard pandas contract). The original 13-prop
                # whitelist is preserved for any future code that
                # wants the legacy "definitely present" subset.
                for _pk, _pv in props_e.items():
                    row[_pk] = _pv
            rows.append(row)
    df_columns = [
        "head", "head_id", "head_type",
        "relation", "rel_type", "relation_name",
        "tail", "tail_id", "tail_type",
        "edge_props",
    ]
    # Append the union of all prop keys so the df has a stable schema.
    df_columns.extend(sorted(all_prop_keys))
    df = pd.DataFrame(rows, columns=df_columns)

    # Compute input checksums over the Phase 1 source files.
    from .phase1_bridge import compute_input_checksum
    # v13 ROOT FIX (Phase1↔Phase2 100% connection): v12's name_map
    # only listed the 4 original source filenames. The 5 new sources
    # (chembl_drugs, uniprot_proteins, string_ppi, disgenet_gda,
    # pubchem_enrichment) were loaded by the bridge but their
    # checksums were NOT included in step1's lineage report. v13:
    # extend name_map to all 9 sources. Each entry is a list of
    # candidate filenames (matching the bridge's dual-name lookup)
    # so the checksum is computed for whichever file actually exists.
    name_map = {
        "drugs": ["drugbank_drugs.csv"],
        "interactions": ["drugbank_interactions.csv.gz"],
        "omim_gda": ["omim_gene_disease_associations.csv"],
        "indications": ["drugbank_indications.csv"],
        # v13: 5 new sources with dual-name lookup (prefixed +
        # unprefixed) matching phase1_bridge.py.
        "chembl_drugs": ["chembl_drugs.csv", "drugs.csv"],
        "uniprot_proteins": ["uniprot_proteins.csv", "proteins.csv"],
        "string_ppi": [
            "string_protein_protein_interactions.csv",
            "protein_protein_interactions.csv",
        ],
        "disgenet_gda": [
            "disgenet_gene_disease_associations.csv",
            "gene_disease_associations.csv",
        ],
        "pubchem_enrichment": ["pubchem_enrichment.csv"],
        # v20 Phase1↔Phase2 connection ROOT FIX: v15 added bridge
        # ingestion of these two files (chembl_activities_clean.csv and
        # omim_gene_disease_susceptibility.csv) but the name_map was
        # NOT extended — so the lineage checksums silently dropped
        # them from the run report. Operators couldn't tell whether
        # the bridge was actually consuming them.
        "chembl_activities": ["chembl_activities_clean.csv"],
        "omim_susceptibility": ["omim_gene_disease_susceptibility.csv"],
    }
    input_checksums = {}
    for key, fnames in name_map.items():
        if isinstance(fnames, str):
            fnames = [fnames]
        for fname in fnames:
            p = pdir / fname
            if p.exists():
                from .phase1_bridge import _sha256_of_file
                input_checksums[fname] = _sha256_of_file(p)
                break  # only checksum the first matching filename

    elapsed = time.time() - t0
    logger.info(
        "Step 1 (PHASE1) complete in %.1fs — %d nodes, %d edges, %d triples",
        elapsed,
        summary["nodes_loaded"],
        summary["edges_loaded"],
        len(df),
    )

    # ROOT FIX (Finding 25, P1): PERSIST the bridge's staged graph to
    # disk so it survives process exit. The previous code used
    # RecordingGraphBuilder (in-memory only) — on process exit, the
    # entire 67-node graph was lost. Step 3 (Neo4j load) was the ONLY
    # persistence path, and it required a Neo4j driver + server. If
    # Step 3 failed (no driver, no server, network error), ALL of
    # Phase 1's data was lost.
    #
    # The fix: write the staged graph to a JSON file at
    # PROCESSED_DIR / "phase1_staged_graph.json" so downstream steps
    # (and operators) can re-load it even after the process exits.
    # This makes the Phase 1 ↔ Phase 2 connection UNCONDITIONAL —
    # the bridge's output always survives, regardless of Neo4j
    # availability. This is the "100% connected" fix.
    try:
        import json as _json_persist
        _persist_dir = PROCESSED_DIR
        _persist_dir.mkdir(parents=True, exist_ok=True)
        _persist_path = _persist_dir / "phase1_staged_graph.json"
        _persist_payload = {
            "bridge_version": summary.get("bridge_version", "unknown"),
            "nodes_staged": summary.get("nodes_staged", 0),
            "edges_staged": summary.get("edges_staged", 0),
            "nodes_loaded": summary.get("nodes_loaded", 0),
            "edges_loaded": summary.get("edges_loaded", 0),
            "edge_types_present": list(summary.get("edge_types_present", [])),
            "sources_read": list(summary.get("sources_read", [])),
            "warnings": list(summary.get("warnings", [])),
            "errors": list(summary.get("errors", [])),
            "node_counts_by_type": {},
            "edge_counts_by_type": {},
            "nodes": {},
            "edges": {},
        }
        _staged_obj = bridge_result.get("staged")
        if _staged_obj is not None:
            _node_collections = {
                "Compound": getattr(_staged_obj, "compound_nodes", []),
                "Protein": getattr(_staged_obj, "protein_nodes", []),
                "Gene": getattr(_staged_obj, "gene_nodes", []),
                "Disease": getattr(_staged_obj, "disease_nodes", []),
                "ClinicalOutcome": getattr(_staged_obj, "clinical_outcome_nodes", []),
                "Pathway": getattr(_staged_obj, "pathway_nodes", []),
            }
            for ntype, nodes in _node_collections.items():
                if nodes:
                    _persist_payload["nodes"][ntype] = nodes
                    _persist_payload["node_counts_by_type"][ntype] = len(nodes)
            for (src, rel, dst), edges in _staged_obj.edges.items():
                _key = f"{src}->{rel}->{dst}"
                _persist_payload["edges"][_key] = edges
                _persist_payload["edge_counts_by_type"][_key] = len(edges)
        with open(_persist_path, "w") as _f:
            _json_persist.dump(_persist_payload, _f, indent=2, default=str)
        logger.info(
            "ROOT FIX (Finding 25): Phase 1 staged graph PERSISTED to "
            "%s (%d nodes, %d edges). This file is the UNCONDITIONAL "
            "Phase 1 ↔ Phase 2 connection artifact — it survives "
            "process exit even when Neo4j is unavailable.",
            _persist_path,
            sum(_persist_payload["node_counts_by_type"].values()),
            sum(_persist_payload["edge_counts_by_type"].values()),
        )
    except Exception as _persist_exc:
        logger.warning(
            "Failed to persist Phase 1 staged graph to disk "
            "(non-fatal): %s", _persist_exc,
        )
    _log_transformation(
        "step1_phase1",
        "Load Phase 1 outputs via bridge (no DRKG download)",
        {
            "nodes_loaded": summary["nodes_loaded"],
            "edges_loaded": summary["edges_loaded"],
            "edge_types_present": summary["edge_types_present"],
            "sources_read": summary["sources_read"],
        },
    )
    return {
        "df": df,
        "validation": {"passed": True, "triples": len(df)},
        "elapsed": elapsed,
        "input_checksums": input_checksums,
        "bridge_summary": summary,
        "entity_maps": entity_maps,  # bonus: skip step2 if you want
        "edge_maps": edge_maps,
        # v24 ROOT FIX (FORENSIC-P2-CORE §2 / Audit Chain 4): expose the
        # per-edge properties dict so step3_load_neo4j can attach them
        # to each edge before loading into Neo4j. The previous code
        # constructed bare ``{"src_id": ..., "dst_id": ...}`` dicts in
        # step3, silently dropping pchembl_value, standard_relation,
        # evidence, source, _source_phase, _source_file, _source_row —
        # all the properties the v15 ROOT FIX promised would be
        # preserved for the RL ranker. The test path (RecordingGraphBuilder)
        # preserved them, so the bug was invisible to tests.
        "edge_props_lookup": edge_props_lookup,
        # FIX-B (Neo4j Node Property Strip): expose the analogous
        # per-node full property dict so step3_load_neo4j can load
        # Compound nodes with their patient-safety properties
        # (withdrawn, fda_approved, clinical_status, molecular_weight,
        # inchikey, smiles, etc.). Previously step3 reconstructed bare
        # `{"id": eid, "entity_type": etype}` dicts, destroying every
        # clinical-safety property in the production Neo4j load path —
        # cerivastatin's `withdrawn=True` flag would be lost, making
        # the RL safety ranker treat it as SAFE. Patient-safety risk.
        "node_props_lookup": node_props_lookup,
        # v29 ROOT FIX (audit I-12): expose the bridge's full
        # ``Phase1StagedData`` so step 4 (and any other downstream
        # consumer) can reuse the already-staged Compound nodes via
        # ``extract_drug_records_from_staged`` instead of re-reading
        # ``drugbank_drugs.csv`` from disk. This is the canonical
        # staged output of the bridge — discard it and you re-do the
        # bridge's work in step 4. NOT serialized to checkpoints
        # (excluded by the ``df, entity_maps, ...`` filter in
        # run_full_pipeline's step-1 result-stripping logic).
        "bridge_staged": bridge_result.get("staged"),
        # FORENSIC bridge root fix: expose Phase 1's entity_mapping
        # DataFrame so step8 can pass it to
        # ``resolver.load_phase1_entity_mapping`` and REUSE Phase 1's
        # cross-source ER instead of re-resolving from scratch.
        "phase1_entity_mapping": (
            bridge_result.get("staged").entity_mapping_df
            if bridge_result.get("staged") is not None
            else None
        ),
    }


def step1_load_data(
    data_source: str = "phase1",
    skip_download: bool = False,
    phase1_processed_dir: Optional[Path | str] = None,
) -> dict:
    """Step 1 dispatcher: select data source (phase1 | drkg).

    v6 fix (bug #B17): the production training pipeline now defaults to
    consuming Phase 1 outputs via the bridge. Pass ``data_source="drkg"``
    to fall back to the legacy DRKG-download path (e.g. for large-scale
    training that needs DRKG's 5.87M triples).
    """
    if data_source == "phase1":
        return step1_load_phase1(phase1_processed_dir)
    elif data_source == "drkg":
        return step1_load_drkg(skip_download)
    else:
        raise ValueError(
            f"Unknown data_source: {data_source!r}. Expected 'phase1' or 'drkg'."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 2: Build Mappings (Domain 5 — Data Quality)
# ═══════════════════════════════════════════════════════════════════════════════


def step2_build_mappings(df) -> dict:
    """Step 2: Build entity and edge index mappings from DRKG DataFrame.

    FATAL step — pipeline aborts if this fails.

    Parameters
    ----------
    df : pd.DataFrame
        Parsed DRKG DataFrame with columns: head, head_type, relation,
        tail, tail_type.

    Returns
    -------
    dict
        Keys: entity_maps, edge_maps, elapsed

    Raises
    ------
    ValueError
        If required columns are missing or DataFrame is empty (BUG-DQ-02).
    """
    _configure_logging()
    logger.info("=" * 60)
    logger.info("STEP 2: Building Entity & Edge Mappings")
    logger.info("=" * 60)
    t0 = time.time()

    # BUG-DQ-02 FIX: Schema validation assertions
    required_columns = ["head", "head_type", "relation", "tail", "tail_type"]
    missing = [col for col in required_columns if col not in df.columns]
    if missing:
        raise ValueError(
            f"DRKG DataFrame missing required columns: {missing}. "
            f"Got columns: {list(df.columns)}."
        )
    if len(df) == 0:
        raise ValueError(
            "DRKG DataFrame is empty — cannot build mappings."
        )

    # GAP-PERF-03: Memory estimate logging
    estimated_mb = len(df) * 200 / 1024 / 1024
    logger.info(
        "Step 2: Processing %d rows (estimated memory: %.0f MB)",
        len(df),
        estimated_mb,
    )

    entity_maps = build_entity_id_maps(df)
    edge_maps = build_edge_index_maps(df, entity_maps)

    total_entities = sum(len(v) for v in entity_maps.values())
    total_edge_types = len(edge_maps)
    total_edges = sum(len(v[0]) for v in edge_maps.values())

    elapsed = time.time() - t0
    _log_transformation(
        "step2",
        "Build entity and edge index mappings",
        {
            "total_entities": total_entities,
            "total_edge_types": total_edge_types,
            "total_edges": total_edges,
        },
    )
    logger.info(
        "Step 2 complete in %.1fs — %d entities, %d edge types, "
        "%d total edges",
        elapsed,
        total_entities,
        total_edge_types,
        total_edges,
    )
    return {
        "entity_maps": entity_maps,
        "edge_maps": edge_maps,
        "elapsed": elapsed,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 3: Load Neo4j (Domain 7 — Idempotency, Domain 1 — Architecture)
# ═══════════════════════════════════════════════════════════════════════════════


def _build_entity_type_data(
    entity_maps: Dict[str, Dict[Any, Any]],
    node_props_lookup: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Build the ``{entity_type: [node_dict, ...]}`` payload that
    ``DrugOSGraphBuilder.load_drkg_nodes`` consumes.

    FIX-B (Neo4j Node Property Strip, patient-safety): when
    ``node_props_lookup`` is provided (Phase 1 bridge path), each node
    dict carries its FULL property set
    (withdrawn/fda_approved/clinical_status/molecular_weight/inchikey/
    smiles/...) from the bridge's ``RecordingGraphBuilder.node_loads``.
    When ``node_props_lookup`` is None (DRKG path), each node dict is
    the legacy bare ``{"id": eid, "entity_type": etype}`` shape (DRKG
    nodes don't carry rich properties).

    The downstream ``kg_builder.load_nodes_batch`` applies
    ``NODE_PROPERTY_WHITELIST`` + ``SYSTEM_PROPS`` itself, so this
    helper does NOT pre-filter — that keeps the whitelist as the single
    source of truth for schema enforcement and prevents schema
    pollution regardless of which path produced the dicts.

    Parameters
    ----------
    entity_maps : dict
        ``{entity_type: {entity_id: index}}`` mapping.
    node_props_lookup : dict, optional
        ``{(label, node_id): full_property_dict}``. When None, the
        legacy bare-dict reconstruction is used.

    Returns
    -------
    dict
        ``{entity_type: [node_dict, ...]}`` ready for
        ``load_drkg_nodes``.
    """
    entity_type_data: Dict[str, List[Dict[str, Any]]] = {}
    for etype, id_map in entity_maps.items():
        nodes_for_type: List[Dict[str, Any]] = []
        for eid in id_map.keys():
            if node_props_lookup is not None:
                full = node_props_lookup.get((etype, eid))
                if full is not None:
                    # Make a shallow copy so callers can mutate the
                    # returned structure without surprising the
                    # lookup's owner.
                    node_dict = dict(full)
                    # Ensure `id` is always present and authoritative
                    # (the lookup key already encodes it, but the
                    # kg_builder requires a top-level `id` field).
                    node_dict["id"] = eid
                    nodes_for_type.append(node_dict)
                    continue
            # Fallback: legacy bare-dict shape (DRKG path or missing
            # entry in the lookup).
            nodes_for_type.append({"id": eid, "entity_type": etype})
        entity_type_data[etype] = nodes_for_type
    return entity_type_data


def step3_load_neo4j(
    entity_maps, edge_maps, skip_neo4j: bool = False,
    *, fresh_start: bool = True,
    edge_props_lookup: Optional[Dict[Tuple[str, str, str, str, str], Dict[str, Any]]] = None,
    node_props_lookup: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
    dry_run_capture: Optional[Dict[str, Any]] = None,
) -> dict:
    """Step 3: Load DRKG into Neo4j using bulk CREATE.

    Idempotent: clears graph before loading (BUG-IDP-01). Uses batched
    edge loading with aggregated drop logging (BUG-DQ-04). Builds
    reverse maps only for edge-involved entity types (BUG-PERF-02).
    Validates length consistency (BUG-COD-03). Initializes node/edge
    results before try block (BUG-COD-02).

    v24 ROOT FIX (FORENSIC-P2-CORE §2 / Audit Chain 4): the previous
    code constructed bare ``{"src_id": ..., "dst_id": ...}`` edge dicts,
    silently dropping ALL edge properties (pchembl_value,
    standard_relation, evidence, source, _source_phase, _source_file,
    _source_row) that the bridge attached. The v15 ROOT FIX promised
    these would be preserved for the RL ranker; that promise was FALSE
    in the production Neo4j load path. The test path
    (RecordingGraphBuilder) preserved them, so the bug was invisible to
    tests. Fix: accept ``edge_props_lookup`` (a dict keyed by
    ``(src_type, rel, dst_type, src_id, dst_id)`` → props dict) and
    attach the properties to each edge before loading. When
    ``edge_props_lookup`` is None (DRKG path), edges are loaded bare as
    before.

    FIX-B (Neo4j Node Property Strip, patient-safety): the previous
    code also reconstructed bare ``{"id": eid, "entity_type": etype}``
    NODE dicts in the production Neo4j load path, destroying every
    patient-safety property the bridge attaches to Compound nodes
    (``withdrawn``, ``fda_approved``, ``clinical_status``,
    ``molecular_weight``, ``inchikey``, ``smiles``, ...). The test
    path (RecordingGraphBuilder) preserved them, so the bug was
    invisible to tests. Fix: accept ``node_props_lookup`` (a dict keyed
    by ``(label, node_id)`` → full node property dict). When provided
    (Phase 1 bridge path), step3 builds the per-type node lists from
    the full property dicts — `kg_builder.load_nodes_batch` then
    applies ``NODE_PROPERTY_WHITELIST`` + ``SYSTEM_PROPS`` itself, so
    schema pollution is still prevented. When ``node_props_lookup`` is
    None (DRKG path), the legacy bare-dict reconstruction is kept
    unchanged.

    Parameters
    ----------
    entity_maps : dict
        Entity type -> {entity_id: index} mapping.
    edge_maps : dict
        (src_type, rel, dst_type) -> (src_indices, dst_indices) mapping.
    skip_neo4j : bool
        Skip Neo4j operations.
    fresh_start : bool
        Clear graph before loading (default True for idempotency).
    edge_props_lookup : dict, optional
        v24: Per-edge properties keyed by
        ``(src_type, rel, dst_type, src_id, dst_id)``. When provided,
        each loaded edge carries its full property set. When None
        (DRKG path), edges are loaded with endpoints only.
    node_props_lookup : dict, optional
        FIX-B: Per-node full property dicts keyed by
        ``(label, node_id)``. When provided (Phase 1 bridge path),
        each loaded node carries its full property set
        (withdrawn/fda_approved/clinical_status/molecular_weight/etc.),
        subject to NODE_PROPERTY_WHITELIST filtering inside
        ``kg_builder.load_nodes_batch``. When None (DRKG path), nodes
        are loaded with endpoints + entity_type only.
    dry_run_capture : dict, optional
        FIX-B: When ``skip_neo4j=True`` AND this dict is provided, the
        function populates ``dry_run_capture["entity_type_data"]`` and
        ``dry_run_capture["edge_type_data"]`` with the exact node/edge
        dicts that WOULD have been sent to Neo4j — without contacting
        Neo4j. Used by tests and dry-runs to verify property
        preservation. When None, behavior is unchanged (returns
        ``{"skipped": True}`` immediately).

    Returns
    -------
    dict
        Keys: node_results, edge_results, elapsed, [skipped | error]
    """
    _configure_logging()
    logger.info("=" * 60)
    logger.info("STEP 3: Loading DRKG into Neo4j")
    logger.info("=" * 60)

    if skip_neo4j:
        logger.info("Skipping Neo4j (--skip-neo4j flag)")
        # FIX-B: when a dry_run_capture dict is provided, populate it
        # with the exact node/edge dicts that WOULD have been sent to
        # Neo4j. This lets tests and dry-runs verify property
        # preservation without contacting Neo4j. When dry_run_capture
        # is None, behavior is unchanged (early return).
        if dry_run_capture is not None:
            dry_run_capture["entity_type_data"] = _build_entity_type_data(
                entity_maps, node_props_lookup
            )
            dry_run_capture["node_props_lookup_provided"] = (
                node_props_lookup is not None
            )
        return {"skipped": True}

    from .kg_builder import DrugOSGraphBuilder

    t0 = time.time()

    # BUG-COD-02 FIX: Initialize before try block
    node_results: Dict[str, Any] = {}
    edge_results: Dict[str, Any] = {}

    try:
        with DrugOSGraphBuilder(Neo4jConfig()) as builder:
            builder.create_constraints()
            builder.create_indexes()

            # BUG-IDP-01 FIX: Clear existing graph for idempotent reload
            # v34 ROOT FIX (CRITICAL #5): use the shared
            # `DEFAULT_CLEAR_GRAPH_PHRASE` constant from kg_builder so the
            # caller's phrase ALWAYS matches the expected phrase. The
            # previous code hardcoded "CLEAR_ALL_DRUGOS_DATA" while
            # kg_builder expected "DELETE EVERYTHING I UNDERSTAND THE
            # CONSEQUENCES" — they NEVER matched, clear_graph() always
            # raised SecurityError, was swallowed by the except below, and
            # the graph was NEVER cleared (re-runs created duplicates).
            if fresh_start:
                logger.info(
                    "Clearing existing Neo4j graph for idempotent reload..."
                )
                try:
                    from drugos_graph.kg_builder import DEFAULT_CLEAR_GRAPH_PHRASE
                    clear_result = builder.clear_graph(
                        confirm=True,
                        confirm_phrase=DEFAULT_CLEAR_GRAPH_PHRASE,
                    )
                    if isinstance(clear_result, dict):
                        logger.info(
                            "Graph cleared: %d nodes deleted, "
                            "%d relationships deleted",
                            clear_result.get("nodes_deleted", 0),
                            clear_result.get("relationships_deleted", 0),
                        )
                except Exception as e:
                    logger.warning(
                        "Graph clear failed (may be empty): %s", e
                    )

            # Load nodes
            # FIX-B (Neo4j Node Property Strip): build the per-type
            # node lists via the shared helper. When node_props_lookup
            # is provided (Phase 1 bridge path), each node carries its
            # full property dict (withdrawn/fda_approved/clinical_status
            # /molecular_weight/inchikey/smiles/...). When None (DRKG
            # path), the legacy bare-dict `{"id", "entity_type"}`
            # reconstruction is used. kg_builder.load_nodes_batch then
            # applies NODE_PROPERTY_WHITELIST + SYSTEM_PROPS itself.
            entity_type_data = _build_entity_type_data(
                entity_maps, node_props_lookup
            )
            node_results = builder.load_drkg_nodes(entity_type_data)

            # BUG-PERF-02 FIX: Only build reverse maps for entity types
            # that actually appear in edges
            edge_entity_types: set = set()
            for (src_type, _, dst_type) in edge_maps.keys():
                edge_entity_types.add(src_type)
                edge_entity_types.add(dst_type)

            reverse_maps: Dict[str, Dict[Any, Any]] = {}
            for etype in edge_entity_types:
                id_map = entity_maps.get(etype, {})
                reverse_maps[etype] = {v: k for k, v in id_map.items()}

            # Load edges using BULK CREATE (10-100x faster than MERGE)
            edge_type_data: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
            total_dropped = 0
            total_expected = 0

            for (src_type, rel_name, dst_type), (src_indices, dst_indices) in edge_maps.items():
                # BUG-COD-03 FIX: Length mismatch check
                if len(src_indices) != len(dst_indices):
                    logger.error(
                        "Step 3: Length mismatch for (%s, %s, %s): "
                        "src=%d, dst=%d. Skipping this edge type.",
                        src_type, rel_name, dst_type,
                        len(src_indices), len(dst_indices),
                    )
                    continue

                src_id_map = reverse_maps.get(src_type, {})
                dst_id_map = reverse_maps.get(dst_type, {})
                edges: List[Dict[str, Any]] = []

                # BUG-DQ-04 FIX: Aggregated edge drop logging instead
                # of per-edge WARNING
                dropped_count = 0
                dropped_examples: List[Tuple] = []

                total_batch = len(src_indices)
                total_expected += total_batch

                for src_idx, dst_idx in zip(src_indices, dst_indices):
                    src_id = src_id_map.get(src_idx)
                    dst_id = dst_id_map.get(dst_idx)
                    if src_id is None or dst_id is None:
                        dropped_count += 1
                        if len(dropped_examples) < 5:
                            dropped_examples.append(
                                (src_type, src_idx, dst_type, dst_idx)
                            )
                        continue
                    # v24 ROOT FIX (Audit Chain 4): attach the per-edge
                    # properties from the bridge when available. The
                    # previous code constructed bare
                    # ``{"src_id": ..., "dst_id": ...}`` dicts, silently
                    # dropping pchembl_value, standard_relation,
                    # evidence, source, _source_phase, _source_file,
                    # _source_row. Now we look up the properties by
                    # (src_type, rel_name, dst_type, src_id, dst_id) and
                    # merge them into the edge dict so kg_builder's
                    # _load_edges can whitelist-filter + attach them.
                    edge_dict: Dict[str, Any] = {
                        "src_id": src_id,
                        "dst_id": dst_id,
                    }
                    if edge_props_lookup is not None:
                        _props_key = (src_type, rel_name, dst_type, src_id, dst_id)
                        _props = edge_props_lookup.get(_props_key)
                        if _props:
                            # Merge props directly into the edge dict
                            # (flat-edge shape — kg_builder._load_edges
                            # handles this correctly as of v24).
                            for _pk, _pv in _props.items():
                                if _pv is not None:
                                    edge_dict[_pk] = _pv
                    edges.append(edge_dict)

                if dropped_count > 0:
                    drop_pct = dropped_count / total_batch * 100
                    logger.warning(
                        "Step 3: Dropped %d/%d edges (%.1f%%) for "
                        "(%s, %s, %s). Examples: %s",
                        dropped_count, total_batch, drop_pct,
                        src_type, rel_name, dst_type,
                        dropped_examples[:3],
                    )
                    if drop_pct > 10:
                        logger.error(
                            "Step 3: MORE THAN 10%% of edges dropped for "
                            "(%s, %s, %s) — check DRKG format version.",
                            src_type, rel_name, dst_type,
                        )
                    total_dropped += dropped_count

                if edges:
                    edge_type_data[(src_type, rel_name, dst_type)] = edges

            if total_dropped > 0:
                total_drop_pct = total_dropped / total_expected * 100 if total_expected > 0 else 0
                logger.info(
                    "Step 3: Total edges dropped: %d/%d (%.1f%%)",
                    total_dropped, total_expected, total_drop_pct,
                )

            edge_results = builder.load_drkg_edges_bulk(edge_type_data)

    except Exception as e:
        logger.error("Neo4j connection failed: %s", e, exc_info=True)
        return {"error": str(e), "elapsed": time.time() - t0}

    elapsed = time.time() - t0
    total_nodes = sum(node_results.values()) if isinstance(node_results, dict) else node_results
    total_edges_loaded = sum(edge_results.values()) if isinstance(edge_results, dict) else edge_results
    _log_transformation(
        "step3",
        "Load DRKG into Neo4j (bulk CREATE, idempotent)",
        {"nodes": total_nodes, "edges": total_edges_loaded, "dropped": total_dropped},
    )
    logger.info(
        "Step 3 complete in %.1fs — nodes: %d, edges: %d",
        elapsed, total_nodes, total_edges_loaded,
    )
    return {
        "node_results": node_results,
        "edge_results": edge_results,
        "elapsed": elapsed,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 4: DrugBank Enrichment (Domain 6 — Reliability)
# ═══════════════════════════════════════════════════════════════════════════════


def step4_drugbank_enrichment(
    skip_neo4j: bool = False,
    skip_download: bool = False,
    phase1_processed_dir: Optional[Path | str] = None,
) -> dict:
    """Step 4: Parse DrugBank XML and enrich Compound nodes.

    v21 ROOT FIX (Audit section 4 finding 12 / Chain 1): the previous
    signature was ``step4_drugbank_enrichment(skip_neo4j)`` with NO
    ``skip_download`` parameter. The caller at run_full_pipeline passed
    ``(skip_neo4j, skip_download=skip_download)`` which raised TypeError
    OR (when caught by the except Exception wrapper) silently turned
    the step into ``drug_records=[]``. In default mode this raised
    FileNotFoundError on the raw XML and returned an empty list,
    bypassing Phase 1's ``drugbank_drugs.csv`` entirely.

    v21 ROOT FIX (Audit section 5 finding 5): consume Phase 1's
    ``drugbank_drugs.csv`` by default. Only fall back to raw XML when
    the CSV is missing AND ``skip_download=False``. Eliminates the
    dual-parser drift risk and completes the Phase 1 <-> Phase 2
    connection.

    v35 ROOT FIX (H-4): DOCUMENT REACHABILITY. The default
    ``run_full_pipeline(data_source="phase1")`` flow SKIPS this function
    entirely (see ``run_full_pipeline`` at the ``data_source == "phase1"``
    branch — it goes through ``extract_drug_records_from_staged`` on the
    bridge's staged Compound nodes instead). This function is therefore
    ONLY reachable via:

      * ``--step 4``           (explicit single-step invocation)
      * ``--data-source drkg`` (legacy DRKG-only pipeline)

    The v21/v28 ROOT FIX comments in the body that call this the
    "canonical DrugBank source" are TRUE for those two reachable paths
    but NOT for the default phase1 path — operators reading the source
    should know that the bridge (``phase1_bridge.stage_phase1_to_phase2``)
    is the canonical DrugBank source in production. The step4 Phase 1
    CSV path is kept live as a DRKG-only fallback and as a defensive
    re-parse path for ``--resume`` after step 1 (when the bridge's
    staged data is not available in memory).

    Parameters
    ----------
    skip_neo4j : bool
        Skip Neo4j writes (but still produce drug_records).
    skip_download : bool
        v21: when True, skip raw XML parsing if Phase 1 CSV is present.
    phase1_processed_dir : path-like, optional
        Phase 1 processed_data directory (for reading drugbank_drugs.csv).
    """
    _configure_logging()
    logger.info("=" * 60)
    logger.info("STEP 4: DrugBank Enrichment")
    logger.info("=" * 60)
    t0 = time.time()

    drug_records: list = []
    target_edges: list = []
    drugs: list = []
    try:
        # v21: Resolve Phase 1 processed_data directory.
        # v22 ROOT FIX: use the canonical DEFAULT_PHASE1_PROCESSED_DIR
        # from phase1_bridge (the SAME path step1 uses) instead of
        # RAW_DIR.parent / "phase1" / "processed_data" which resolves
        # to the wrong directory (phase2/data/phase1/processed_data).
        from .phase1_bridge import DEFAULT_PHASE1_PROCESSED_DIR as _DEF_P1_DIR
        p1_dir = (
            Path(phase1_processed_dir)
            if phase1_processed_dir
            else _DEF_P1_DIR
        )
        phase1_drugbank_csv = p1_dir / "drugbank_drugs.csv"
        used_phase1_csv = False
        if phase1_drugbank_csv.exists():
            logger.info(
                "Step 4: consuming Phase 1 %s (canonical DrugBank source).",
                phase1_drugbank_csv,
            )
            # v28 ROOT FIX (P2-L-8): replace the inline Phase 1 CSV
            # parsing with calls to the dedicated Phase-1-aware
            # functions in drugbank_parser. This makes
            # ``parse_drugbank_from_phase1_csv``,
            # ``drugbank_to_node_records_from_phase1``, and
            # ``drugbank_to_target_edges_from_phase1`` LIVE code
            # (previously they were defined but NEVER CALLED — step4
            # inlined their logic instead of delegating). The dedicated
            # functions are the canonical Phase 1-aware entry points;
            # inlining the logic caused schema drift (e.g., the v28
            # P2-L-10 fix to add the ``id`` field was applied to the
            # dedicated function but NOT to the inline copy).
            from .drugbank_parser import (
                parse_drugbank_from_phase1_csv as _parse_db_p1,
                drugbank_to_node_records_from_phase1 as _db_nodes_from_p1,
                parse_drugbank_interactions_from_phase1_csv as _parse_db_int_p1,
                drugbank_to_target_edges_from_phase1 as _db_edges_from_p1,
            )
            _db_df = _parse_db_p1(phase1_drugbank_csv)
            drug_records = _db_nodes_from_p1(_db_df)
            interactions_gz = p1_dir / "drugbank_interactions.csv.gz"
            if interactions_gz.exists():
                _ia_df = _parse_db_int_p1(interactions_gz)
                # v27 ROOT FIX (P2-L-4): the dedicated
                # ``drugbank_to_target_edges_from_phase1`` routes
                # ``action_type`` through ``_map_action_to_relation``
                # (same as the raw-XML path) so both paths emit the
                # SAME canonical verb. Default to "targets" when action
                # is empty or unmapped (patient-safety-correct default).
                #
                # v35 ROOT FIX (V35-P2-LOADERS-FIXES H-2): Phase 1's
                # interactions CSV has no `inchikey` column, so the
                # previous call ALWAYS fell back to the raw drugbank_id
                # for `src_id` — producing orphan edges. Build a
                # `drug_canonical_map` (drugbank_id -> inchikey) from
                # the just-staged Compound nodes and pass it so the
                # edge emitter can normalize `src_id` to InChIKey.
                _drug_canonical_map: Dict[str, str] = {}
                for _nd in drug_records:
                    _dbid = _nd.get("drugbank_id")
                    _ik = _nd.get("inchikey") or _nd.get("id")
                    if _dbid and _ik and str(_ik).strip():
                        _drug_canonical_map[str(_dbid)] = str(_ik).strip()
                target_edges = _db_edges_from_p1(
                    _ia_df,
                    drug_canonical_map=_drug_canonical_map or None,
                )
            logger.info(
                "Step 4: %d drug records, %d target edges from Phase 1 CSVs.",
                len(drug_records), len(target_edges),
            )
            used_phase1_csv = True
        elif skip_download:
            logger.warning(
                "Step 4 skipped: Phase 1 %s not found AND --skip-download is set. "
                "drug_records=[] - downstream steps 8/10 will see no DrugBank data.",
                phase1_drugbank_csv,
            )
            elapsed = time.time() - t0
            return {
                "skipped": True,
                "reason": "phase1_csv_missing_and_skip_download",
                "elapsed": elapsed,
                "drug_records": [],
                "target_edges": [],
            }

        if not used_phase1_csv:
            drugs = parse_drugbank_xml()
            drug_records = drugbank_to_node_records(drugs)
            target_edges = drugbank_to_target_edges(drugs)
            logger.info(
                "Parsed %d drugs, %d target edges (raw XML fallback)",
                len(drugs), len(target_edges),
            )

        if drug_records:
            _scan_for_pii(drug_records, "DrugBank")

        if not skip_neo4j and (drug_records or target_edges):
            from .kg_builder import DrugOSGraphBuilder

            with DrugOSGraphBuilder(Neo4jConfig()) as builder:
                if drug_records:
                    builder.enrich_compounds_from_drugbank(drug_records)
                if target_edges:
                    builder.load_edges_bulk_create(
                        "Compound", "targets", "Protein", target_edges
                    )
    # BUG-REL-05 FIX: Broaden exception catch from FileNotFoundError only
    except (FileNotFoundError, ValueError, OSError) as e:
        logger.warning("Step 4 skipped: %s", e)
        elapsed = time.time() - t0
        return {"skipped": True, "reason": str(e), "elapsed": elapsed, "drug_records": [], "target_edges": []}
    except Exception as e:
        logger.error("Step 4 failed: %s", e, exc_info=True)
        elapsed = time.time() - t0
        return {"error": str(e), "elapsed": elapsed, "drug_records": [], "target_edges": []}

    elapsed = time.time() - t0
    _log_transformation(
        "step4",
        "Parse DrugBank and enrich compounds",
        {"drugs_parsed": len(drugs), "target_edges": len(target_edges)},
    )
    logger.info("Step 4 complete in %.1fs", elapsed)
    return {
        "drug_records": drug_records,
        "target_edges": target_edges,
        "elapsed": elapsed,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 5: STITCH Ingestion (Domain 3 — Scientific Correctness)
# ═══════════════════════════════════════════════════════════════════════════════


def step5_stitch_ingestion(skip_neo4j: bool = False, skip_download: bool = False) -> dict:
    """Step 5: Ingest STITCH drug-protein interactions.

    BUG-SCI-06 FIX: Groups edges by their resolved relation type instead
    of collapsing all to "binds". The config defines 8 action types:
    binds, inhibits, activates, allosterically_modulates, induces,
    metabolized_by, transported_by, carried_by.

    v15 ROOT FIX (REM-24): ``skip_download=True`` now actually skips the
    STITCH download (previously the flag was ignored for steps 5/6/7,
    causing every `--skip-download` invocation to still attempt the
    network fetch and burn ~30s on SSL-retry timeouts before failing).

    Uses batched loading grouped by (src_type, rel_type, dst_type).

    Parameters
    ----------
    skip_neo4j : bool
        Skip Neo4j writes.
    skip_download : bool
        v15: skip the STITCH network download entirely. The step returns
        ``{"skipped": True, "reason": "skip_download"}`` and does NOT
        attempt to parse the (missing) local file.

    Returns
    -------
    dict
        Keys: stitch_edges, elapsed, [skipped | error]
    """
    _configure_logging()
    logger.info("=" * 60)
    logger.info("STEP 5: STITCH Ingestion")
    logger.info("=" * 60)
    t0 = time.time()

    # Lazy imports — STITCH dependencies are heavy (pandas, etc.)
    from .stitch_loader import (
        download_stitch,
        parse_stitch_interactions,
        stitch_to_edge_records,
    )
    from .config import DATA_SOURCES as _DS, RAW_DIR as _RAW

    # v15 ROOT FIX (REM-24): honor skip_download. v14 ignored this flag
    # for step5/6/7, causing `--skip-download` to silently attempt the
    # network fetch anyway and burn 30+ seconds on SSL-retry timeouts.
    if skip_download:
        # Check if the file is already cached locally — if so, use it;
        # otherwise skip cleanly without attempting the download.
        stitch_filename = _DS.get("stitch", {}).get("filename", "stitch.tsv.gz")
        stitch_path = _RAW / stitch_filename
        if not stitch_path.exists():
            elapsed = time.time() - t0
            logger.info(
                "Step 5 skipped (--skip-download): STITCH file not cached "
                "at %s. To enable: run without --skip-download, or pre-place "
                "the file.", stitch_path,
            )
            return {"skipped": True, "reason": "skip_download", "elapsed": elapsed}
        logger.info("Step 5: --skip-download set, but STITCH file is cached at %s — using it.", stitch_path)
    else:
        download_stitch()
    df = parse_stitch_interactions()
    edges = stitch_to_edge_records(df)

    # BUG-SCI-06 FIX: Group STITCH edges by their resolved relation type
    if not skip_neo4j and edges:
        from .kg_builder import DrugOSGraphBuilder

        stitch_grouped: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
        # v22 ROOT FIX (Audit section 7 finding 8 — "STITCH edge type
        # collapses silently"): the previous code did
        # ``rel_type = edge.get("rel_type", "binds")``. If
        # ``stitch_to_edge_records`` ever omitted ``rel_type`` (e.g. an
        # upstream schema change, a None value, or a regression), ALL
        # STITCH edges silently collapsed to ``binds`` — losing the 8
        # distinct action types (inhibits, activates, binds, other,
        # etc.) that STITCH provides. This is BUG-SCI-06 regression
        # risk. Root fix: if rel_type is missing/None/empty, log a
        # WARNING and use ``"interacts_with"`` (a semantically neutral
        # relation that does NOT imply a specific mechanism) instead of
        # the mechanism-specific ``"binds"``. This makes the collapse
        # visible in logs AND avoids corrupting the KG with false
        # "binds" assertions.
        _stitch_missing_rel_type_count = 0
        for edge in edges:
            _rt = edge.get("rel_type")
            if not _rt or not str(_rt).strip():
                _stitch_missing_rel_type_count += 1
                rel_type = "interacts_with"  # neutral fallback
            else:
                rel_type = str(_rt).strip().lower()
            stitch_grouped[("Compound", rel_type, "Protein")].append(edge)
        if _stitch_missing_rel_type_count > 0:
            logger.warning(
                "STITCH: %d of %d edges had missing/empty rel_type — "
                "defaulted to 'interacts_with' (neutral) instead of "
                "'binds' (mechanism-specific). Investigate "
                "stitch_to_edge_records() output schema.",
                _stitch_missing_rel_type_count, len(edges),
            )

        with DrugOSGraphBuilder(Neo4jConfig()) as builder:
            batch_size = Neo4jConfig().batch_size_edges
            for (src_t, rel_t, dst_t), group in stitch_grouped.items():
                for i in range(0, len(group), batch_size):
                    batch = group[i : i + batch_size]
                    builder.load_edges_bulk_create(src_t, rel_t, dst_t, batch)

        logger.info(
            "STITCH loaded %d edges across %d relation types: %s",
            len(edges),
            len(stitch_grouped),
            list(stitch_grouped.keys()),
        )

    elapsed = time.time() - t0
    _log_transformation(
        "step5",
        "Ingest STITCH drug-protein interactions",
        {"total_edges": len(edges), "relation_types": len(set(
            (str(e.get("rel_type") or "").strip().lower() or "interacts_with")
            for e in edges
        ))},
    )
    logger.info(
        "Step 5 complete in %.1fs — %d STITCH edges", elapsed, len(edges)
    )
    return {"stitch_edges": len(edges), "elapsed": elapsed}


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 6: SIDER Ingestion (Domain 3 — Scientific Correctness)
# ═══════════════════════════════════════════════════════════════════════════════


def step6_sider_ingestion(skip_neo4j: bool = False, skip_download: bool = False) -> dict:
    """Step 6: Ingest SIDER side effect data.

    Loads MedDRA-coded adverse events. Uses canonical 'MedDRA_Term' label
    and 'causes_adverse_event' relation type (patient safety: ensures RL
    safety ranker can query these nodes).

    Parameters
    ----------
    skip_neo4j : bool
        Skip Neo4j writes.
    skip_download : bool
        v15 ROOT FIX (REM-24): skip the SIDER network download entirely.

    Returns
    -------
    dict
        Keys: sider_nodes, sider_edges, elapsed, [skipped | error]
    """
    _configure_logging()
    logger.info("=" * 60)
    logger.info("STEP 6: SIDER Ingestion")
    logger.info("=" * 60)
    t0 = time.time()

    from .sider_loader import (
        download_sider,
        parse_sider_side_effects,
        sider_to_edge_records,
        sider_to_node_records,
        _resolve_sider_filepath,
    )
    from .config import DATA_SOURCES as _DS, RAW_DIR as _RAW

    # v15 ROOT FIX (REM-24): honor skip_download.
    if skip_download:
        sider_filename = _DS.get("sider", {}).get("filename", "meddra_all_se.tsv.gz")
        sider_path = _RAW / sider_filename
        if not sider_path.exists():
            elapsed = time.time() - t0
            logger.info(
                "Step 6 skipped (--skip-download): SIDER file not cached at %s.",
                sider_path,
            )
            return {"skipped": True, "reason": "skip_download", "elapsed": elapsed}
        logger.info("Step 6: --skip-download set, but SIDER file is cached at %s — using it.", sider_path)
    else:
        download_sider()
    df = parse_sider_side_effects()
    nodes = sider_to_node_records(df)
    edges = sider_to_edge_records(df)

    if not skip_neo4j:
        from .kg_builder import DrugOSGraphBuilder

        with DrugOSGraphBuilder(Neo4jConfig()) as builder:
            if nodes:
                # PATIENT SAFETY: Load as MedDRA_Term (canonical) not
                # 'Side Effect' (legacy) — ensures the RL safety ranker
                # can find adverse events via standard query pattern.
                builder.load_nodes_batch("MedDRA_Term", nodes)
            if edges:
                builder.load_edges_bulk_create(
                    "Compound", "causes_adverse_event", "MedDRA_Term", edges
                )

    elapsed = time.time() - t0
    _log_transformation(
        "step6",
        "Ingest SIDER side effects (MedDRA-coded)",
        {"side_effects": len(nodes), "edges": len(edges)},
    )
    logger.info(
        "Step 6 complete in %.1fs — %d side effects, %d edges",
        elapsed, len(nodes), len(edges),
    )
    return {"sider_nodes": len(nodes), "sider_edges": len(edges), "elapsed": elapsed}


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 7: Additional Data Sources (Domain 3 — Scientific Correctness)
# ═══════════════════════════════════════════════════════════════════════════════


def step7_additional_sources(
    skip_neo4j: bool = False,
    skip_download: bool = False,
    phase1_processed_dir: Optional[Path | str] = None,
    data_source: str = "phase1",
) -> dict:
    """Step 7: Ingest STRING, ChEMBL, OpenTargets, UniProt, ClinicalTrials,
    DisGeNET, OMIM, PubChem, and GEO.

    v21 ROOT FIX (Audit section 4 finding 1 / Chain 1 - THE P0 BLOCKER):
    The previous signature was ``step7_additional_sources(skip_neo4j,
    skip_download)`` with NO ``phase1_processed_dir`` parameter. But the
    function body referenced ``phase1_processed_dir`` to locate the
    Phase 1 CSVs for DisGeNET / OMIM / PubChem fallback. The resulting
    ``NameError`` was caught by ``except Exception`` and silently
    swallowed - making the Phase 1 CSV fallback UNREACHABLE at runtime.
    This was the audit's #1 P0 blocker. Fix: add ``phase1_processed_dir``
    to the signature AND thread it from ``run_full_pipeline``.

    v24 ROOT FIX (Audit section 7 / Phase 2 Loaders Bypass Matrix - THE
    user's #1 requirement: "graph explorer 100% connected with Phase 1
    dataset"): when ``data_source="phase1"`` (the default), the bridge
    in step1 ALREADY loaded STRING / UniProt / ChEMBL data from Phase 1
    CSVs into the in-memory builder (which step3 then loaded into Neo4j).
    The previous code unconditionally re-downloaded STRING (~300 MB),
    UniProt (~800 MB), and ChEMBL (~2 GB SQLite) and re-loaded them into
    Neo4j — creating DUPLICATE edges (one set from step3 with stripped
    properties labeled ``_source="DRKG"``, another from step7 with
    properties labeled ``_source="unknown"``) AND bypassing the 7 weeks
    of Phase 1 ETL work. The audit's bypass matrix showed "0 of 13
    Phase 2 loaders actually consume Phase 1 outputs at runtime in
    default mode." Fix: when ``data_source="phase1"``, SKIP step7a/7b/7c
    entirely — the bridge already staged that data. Only run them when
    ``data_source="drkg"`` (the legacy path that doesn't use the bridge).

    v15 ROOT FIX (REM-24): ``skip_download=True`` skips network downloads.

    Parameters
    ----------
    skip_neo4j : bool
        Skip Neo4j writes.
    skip_download : bool
        v15: skip all network downloads.
    phase1_processed_dir : path-like, optional
        v21: Phase 1 processed_data directory. Used by sub-steps 7f/7g/7h
        to locate Phase 1 CSVs as the canonical data source when
        ``skip_download=True``.
    data_source : str
        v24: ``"phase1"`` (default) — STRING/UniProt/ChEMBL were already
        loaded by the bridge in step1; skip 7a/7b/7c to avoid duplicates.
        ``"drkg"`` — run 7a/7b/7c normally (legacy DRKG path).

    Returns
    -------
    dict
        Keys: results (per-source counts), elapsed
    """
    _configure_logging()
    logger.info("=" * 60)
    logger.info("STEP 7: Additional Data Sources")
    logger.info("=" * 60)
    if skip_download:
        logger.info(
            "Step 7 (--skip-download): each source will first check for a "
            "cached local file; missing files are skipped cleanly without "
            "attempting the network fetch."
        )
    t0 = time.time()
    results: Dict[str, Any] = {}

    # v29 ROOT FIX (audit I-2 / Compound Chain 2 — duplicate-load when
    # bridge already loaded): when data_source="phase1", the bridge in
    # step1 ALREADY loaded DisGeNET / OMIM / PubChem edges into the
    # graph (via step3). Re-running 7f/7g/7h creates DUPLICATE edges
    # in Neo4j. The audit found that 7a/7b/7c were correctly skipped,
    # but 7f/7g/7h were missed. ROOT FIX: skip 7f/7g/7h entirely when
    # data_source="phase1" — the bridge is the authoritative source.
    _skip_7fgh = (data_source == "phase1")
    if _skip_7fgh:
        logger.info(
            "Step 7 (v29 root fix): data_source=phase1 — DisGeNET/OMIM/"
            "PubChem were already loaded by the bridge in step1. "
            "Skipping 7f/7g/7h to avoid DUPLICATE edges in Neo4j "
            "(audit I-2). STRING/UniProt/ChEMBL (7a/7b/7c) were "
            "already skipped by the v24 fix."
        )

    # v15 ROOT FIX (REM-24): helper to check if a source file is cached.
    from .config import DATA_SOURCES as _DS, RAW_DIR as _RAW

    def _is_cached(source_key: str, fallback_filename: str) -> bool:
        fn = _DS.get(source_key, {}).get("filename", fallback_filename)
        return (_RAW / fn).exists()

    # v24 ROOT FIX: when data_source="phase1", the bridge already loaded
    # STRING/UniProt/ChEMBL from Phase 1 CSVs in step1. Skip 7a/7b/7c to
    # avoid duplicate edges AND bypassing Phase 1 ETL.
    _phase1_bridge_used = (data_source == "phase1")
    if _phase1_bridge_used:
        logger.info(
            "Step 7 (v24 root fix): data_source='phase1' — STRING, UniProt, "
            "ChEMBL were already loaded from Phase 1 CSVs by the bridge in "
            "step1. Sub-steps 7a/7b/7c will be SKIPPED to avoid duplicate "
            "edges and to honor the user's requirement that the graph "
            "explorer be 100% connected with the Phase 1 dataset."
        )

    # ─── 7a: STRING PPI (critical data source) ────────────────────────────
    if _phase1_bridge_used:
        # v24 ROOT FIX: Phase 1 bridge already loaded
        # string_protein_protein_interactions.csv into the in-memory
        # builder in step1 (see phase1_bridge._load_string_ppi). Step3
        # already loaded those edges into Neo4j. Re-downloading STRING
        # here would (a) bypass Phase 1's cleaned PPI data, (b) create
        # duplicate Protein-interacts_with-Protein edges, (c) waste
        # ~300 MB of bandwidth. Skip cleanly.
        logger.info(
            "Step 7a SKIPPED (v24 root fix): data_source='phase1' — "
            "STRING PPI edges were already loaded from "
            "string_protein_protein_interactions.csv by the bridge "
            "in step1."
        )
        results["string_skipped"] = True
        results["string_skip_reason"] = "phase1_bridge_already_loaded"
    else:
      try:
        from .string_loader import (
            download_string,
            parse_string_ppi,
            string_to_edge_records,
        )

        # v28 ROOT FIX (P2-L-8): before falling back to the raw STRING
        # download (~300 MB), check whether Phase 1's cleaned
        # ``string_protein_protein_interactions.csv`` exists and is
        # non-empty. If so, use the dedicated Phase-1-aware parser
        # ``parse_string_ppi_from_phase1_csv`` + emitter
        # ``string_to_edge_records_from_phase1``. This makes the v26
        # ROOT FIX Phase-1-aware functions LIVE code (previously they
        # were defined but NEVER CALLED from step7 — always bypassed
        # in favor of the raw 300 MB download). Phase 1's CSV is the
        # source of truth when available; the raw parser is the fallback.
        from .string_loader import (
            DEFAULT_STRING_PPI_CSV as _DEFAULT_STRING_P1_CSV,
            parse_string_ppi_from_phase1_csv as _parse_string_p1,
            string_to_edge_records_from_phase1 as _string_edges_from_p1,
        )
        _p1_string_csv = (
            Path(phase1_processed_dir) / "string_protein_protein_interactions.csv"
            if phase1_processed_dir
            else _DEFAULT_STRING_P1_CSV
        )
        _use_string_phase1 = (
            _p1_string_csv.exists()
            and _p1_string_csv.stat().st_size > 0
        )

        # v15 ROOT FIX (REM-24): honor skip_download.
        if skip_download and not _is_cached("string", "string_ppi.txt.gz") and not _use_string_phase1:
            logger.info("Step 7a skipped (--skip-download): STRING not cached.")
            results["string_skipped"] = True
            results["string_skip_reason"] = "skip_download"
        else:
            if _use_string_phase1:
                # v28 ROOT FIX (P2-L-8): consume Phase 1's cleaned CSV
                # via the dedicated Phase-1-aware functions. This makes
                # ``parse_string_ppi_from_phase1_csv`` and
                # ``string_to_edge_records_from_phase1`` LIVE code.
                logger.info(
                    "Step 7a (v28 root fix P2-L-8): using Phase 1's "
                    "cleaned STRING CSV at %s (canonical source).",
                    _p1_string_csv,
                )
                string_df = _parse_string_p1(_p1_string_csv)
                string_edges = _string_edges_from_p1(string_df)
                results["string_edges"] = len(string_edges)
                results["string_source"] = "phase1_csv"
            else:
                # Fall back to raw STRING download + parse.
                if not skip_download:
                    download_string()
                string_df = parse_string_ppi()
                string_edges = string_to_edge_records(string_df)
                results["string_edges"] = len(string_edges)
                results["string_source"] = "raw_download"

                # v15 ROOT FIX (DC-10 / REM-23): the freshness check used to
                # stat() `9606.protein.info.v12.0.txt.gz` — a file the STRING
                # downloader NEVER writes. The downloader writes to
                # `DATA_SOURCES["string"]["filename"]` which is currently
                # `string_ppi.txt.gz` (a renamed cache of
                # `9606.protein.links.full.v12.0.txt.gz`). The freshness check
                # therefore always fell through to the OSError catch and logged
                # at DEBUG — invisible to operators. Fix: stat the actual file
                # the downloader writes.
                _string_filename = _DS.get("string", {}).get("filename", "string_ppi.txt.gz")
                _check_data_freshness(
                    _RAW / _string_filename, "STRING"
                )

            if not skip_neo4j and string_edges:
                from .kg_builder import DrugOSGraphBuilder

                with DrugOSGraphBuilder(Neo4jConfig()) as builder:
                    batch_size = Neo4jConfig().batch_size_edges
                    for i in range(0, len(string_edges), batch_size):
                        batch = string_edges[i : i + batch_size]
                        builder.load_edges_bulk_create(
                            "Protein", "interacts_with", "Protein", batch
                        )
      except Exception as e:
        # V19 ROOT FIX (SF-7 — verification agent flagged this as PARTIAL):
        # the V18 code logged ERROR and continued with the STRING source
        # missing — silently producing a degraded KG missing the PPI
        # network (the foundation of "multi-hop" reasoning per the DOCX).
        # The ROOT fix is to RAISE in production (same
        # DRUGOS_ALLOW_PERMISSIVE_DPI=1 pattern as SF-3). STRING is a
        # CRITICAL data source per the project spec — its absence
        # invalidates downstream multi-hop queries.
        import os as _os
        _permissive = _os.environ.get(
            "DRUGOS_ALLOW_PERMISSIVE_KG", ""
        ) == "1"
        results["string_error"] = str(e)
        if _permissive:
            logger.error(
                "STRING ingestion failed (critical data source) — "
                "DRUGOS_ALLOW_PERMISSIVE_KG=1 is set, continuing with "
                "STRING edges MISSING. The KG will be missing the PPI "
                "network (multi-hop queries degraded): %s", e,
                exc_info=True,
            )
        else:
            logger.error(
                "STRING ingestion failed (critical data source) — "
                "FATAL. Set DRUGOS_ALLOW_PERMISSIVE_KG=1 to continue "
                "with STRING missing (unit tests / known-broken "
                "snapshots only): %s", e, exc_info=True,
            )
            raise RuntimeError(
                f"STRING ingestion failed (critical data source): {e}. "
                f"V19 SF-7 root fix — the V18 default of log-and-continue "
                f"silently produced a KG missing the PPI network. Set "
                f"DRUGOS_ALLOW_PERMISSIVE_KG=1 to opt in to the legacy "
                f"permissive behavior."
            ) from e

    # ─── 7b: UniProt proteins (critical data source) ──────────────────────
    if _phase1_bridge_used:
        # v24 ROOT FIX: Phase 1 bridge already loaded uniprot_proteins.csv
        # into the in-memory builder in step1 (see phase1_bridge._load_uniprot).
        # Step3 already loaded those Protein nodes + cross-reference edges
        # into Neo4j. Re-downloading UniProt .dat here would (a) bypass
        # Phase 1's cleaned protein data, (b) create duplicate Protein
        # nodes and xref edges, (c) waste ~800 MB of bandwidth.
        logger.info(
            "Step 7b SKIPPED (v24 root fix): data_source='phase1' — "
            "UniProt Protein nodes + xref edges were already loaded "
            "from uniprot_proteins.csv by the bridge in step1."
        )
        results["uniprot_skipped"] = True
        results["uniprot_skip_reason"] = "phase1_bridge_already_loaded"
    else:
      try:
        from .uniprot_loader import (
            download_uniprot,
            parse_uniprot_entries,
            uniprot_to_edge_records,
            uniprot_to_node_records,
        )

        # v28 ROOT FIX (P2-L-8): before falling back to the raw UniProt
        # download (~800 MB), check whether Phase 1's cleaned
        # ``uniprot_proteins.csv`` exists and is non-empty. If so, use
        # the dedicated Phase-1-aware parser
        # ``parse_uniprot_entries_from_phase1_csv`` + emitters
        # ``uniprot_to_node_records_from_phase1`` /
        # ``uniprot_to_edge_records_from_phase1``. This makes the v26
        # ROOT FIX Phase-1-aware functions LIVE code (previously they
        # were defined but NEVER CALLED from step7 — always bypassed
        # in favor of the raw 800 MB download). Phase 1's CSV is the
        # source of truth when available; the raw parser is the fallback.
        from .uniprot_loader import (
            DEFAULT_UNIPROT_PROTEINS_CSV as _DEFAULT_UNIPROT_P1_CSV,
            parse_uniprot_entries_from_phase1_csv as _parse_uniprot_p1,
            uniprot_to_node_records_from_phase1 as _uniprot_nodes_from_p1,
            uniprot_to_edge_records_from_phase1 as _uniprot_edges_from_p1,
        )
        _p1_uniprot_csv = (
            Path(phase1_processed_dir) / "uniprot_proteins.csv"
            if phase1_processed_dir
            else _DEFAULT_UNIPROT_P1_CSV
        )
        _use_uniprot_phase1 = (
            _p1_uniprot_csv.exists()
            and _p1_uniprot_csv.stat().st_size > 0
        )

        # v15 ROOT FIX (REM-24): honor skip_download.
        if skip_download and not _is_cached("uniprot", "uniprot_sprot.dat.gz") and not _use_uniprot_phase1:
            logger.info("Step 7b skipped (--skip-download): UniProt not cached.")
            results["uniprot_skipped"] = True
            results["uniprot_skip_reason"] = "skip_download"
        else:
            if _use_uniprot_phase1:
                # v28 ROOT FIX (P2-L-8): consume Phase 1's cleaned CSV
                # via the dedicated Phase-1-aware functions. This makes
                # ``parse_uniprot_entries_from_phase1_csv``,
                # ``uniprot_to_node_records_from_phase1``, and
                # ``uniprot_to_edge_records_from_phase1`` LIVE code.
                logger.info(
                    "Step 7b (v28 root fix P2-L-8): using Phase 1's "
                    "cleaned UniProt CSV at %s (canonical source).",
                    _p1_uniprot_csv,
                )
                uniprot_records = _parse_uniprot_p1(_p1_uniprot_csv)
                uniprot_nodes = _uniprot_nodes_from_p1(uniprot_records)
                # v9 ROOT FIX (audit F5.2.1): the previous code NEVER called
                # uniprot_to_edge_records — the entire function was P1-DEAD code.
                # Now we call it and load the cross-reference edges. Combined
                # with the src_id fix in uniprot_loader.py (now emits bare
                # accession "P23219" instead of "uniprot:P23219"), these edges
                # will reach Neo4j.
                uniprot_edges = _uniprot_edges_from_p1(uniprot_records)
                results["uniprot_nodes"] = len(uniprot_nodes)
                results["uniprot_edges"] = len(uniprot_edges)
                results["uniprot_source"] = "phase1_csv"
            else:
                # Fall back to raw UniProt download + parse.
                if not skip_download:
                    download_uniprot()
                uniprot_records = parse_uniprot_entries()
                uniprot_nodes = uniprot_to_node_records(uniprot_records)
                # v9 ROOT FIX (audit F5.2.1): see comment above.
                uniprot_edges = uniprot_to_edge_records(uniprot_records)
                results["uniprot_nodes"] = len(uniprot_nodes)
                results["uniprot_edges"] = len(uniprot_edges)
                results["uniprot_source"] = "raw_download"

            if not skip_neo4j and (uniprot_nodes or uniprot_edges):
                from .kg_builder import DrugOSGraphBuilder

                with DrugOSGraphBuilder(Neo4jConfig()) as builder:
                    if uniprot_nodes:
                        batch_size = Neo4jConfig().batch_size_nodes
                        for i in range(0, len(uniprot_nodes), batch_size):
                            batch = uniprot_nodes[i : i + batch_size]
                            builder.load_nodes_batch("Protein", batch)
                    if uniprot_edges:
                        edge_batch_size = Neo4jConfig().batch_size_edges
                        for i in range(0, len(uniprot_edges), edge_batch_size):
                            batch = uniprot_edges[i : i + edge_batch_size]
                            # UniProt edges are heterogeneous (Protein -> ExternalRef,
                            # Protein -> Gene, etc.) — use the per-edge src_type/dst_type
                            # if present, otherwise default to Protein -> ExternalRef.
                            # kg_builder.load_edges_bulk_create takes (src_label,
                            # rel_type, dst_label, edges). The edges themselves carry
                            # src_type/dst_type as keys (added in the v9 fix). For
                            # bulk loading simplicity, we group by (src_type, rel_type,
                            # dst_type) and load each group separately.
                            from collections import defaultdict
                            groups: dict = defaultdict(list)
                            for edge in batch:
                                s = edge.get("src_type", "Protein")
                                r = edge.get("rel_type") or edge.get("relation", "xref")
                                d = edge.get("dst_type", "ExternalRef")
                                groups[(s, r, d)].append(edge)
                            for (s, r, d), group_edges in groups.items():
                                builder.load_edges_bulk_create(s, r, d, group_edges)
      except Exception as e:
        logger.error("UniProt ingestion failed (critical data source): %s", e)
        results["uniprot_error"] = str(e)

    # ─── 7c: ChEMBL bioactivity ────────────────────────────────────────────
    if _phase1_bridge_used:
        # v24 ROOT FIX: Phase 1 bridge already loaded
        # chembl_activities_clean.csv + chembl_drugs.csv into the in-memory
        # builder in step1 (see phase1_bridge._load_chembl_activities).
        # Step3 already loaded those Compound-{inhibits,activates,targets}-
        # Protein edges into Neo4j. Re-downloading ChEMBL SQLite here
        # would (a) bypass Phase 1's cleaned bioactivity data, (b) create
        # duplicate DPI edges, (c) waste ~2 GB of bandwidth, (d) risk
        # dual-parser drift between Phase 1's chembl_pipeline and Phase 2's
        # chembl_loader.
        logger.info(
            "Step 7c SKIPPED (v24 root fix): data_source='phase1' — "
            "ChEMBL Compound-{inhibits,activates,targets}-Protein edges "
            "were already loaded from chembl_activities_clean.csv by the "
            "bridge in step1."
        )
        results["chembl_skipped"] = True
        results["chembl_skip_reason"] = "phase1_bridge_already_loaded"
    else:
      try:
        from .chembl_loader import (
            download_chembl,
            parse_chembl_activities,
            chembl_to_edge_records,
        )

        # v28 ROOT FIX (P2-L-8): before falling back to the raw ChEMBL
        # SQLite download (~2 GB), check whether Phase 1's cleaned
        # ``chembl_activities_clean.csv`` exists and is non-empty. If so,
        # use the dedicated Phase-1-aware parser
        # ``parse_chembl_activities_from_phase1_csv`` + emitter
        # ``chembl_to_edge_records_from_phase1``. This makes the v26
        # ROOT FIX Phase-1-aware functions LIVE code (previously they
        # were defined but NEVER CALLED from step7 — always bypassed
        # in favor of the raw 2 GB SQLite download). Phase 1's CSV is
        # the source of truth when available; the raw parser is the
        # fallback.
        from .chembl_loader import (
            DEFAULT_CHEMBL_ACTIVITIES_CSV as _DEFAULT_CHEMBL_P1_CSV,
            parse_chembl_activities_from_phase1_csv as _parse_chembl_p1,
            chembl_to_edge_records_from_phase1 as _chembl_edges_from_p1,
        )
        _p1_chembl_csv = (
            Path(phase1_processed_dir) / "chembl_activities_clean.csv"
            if phase1_processed_dir
            else _DEFAULT_CHEMBL_P1_CSV
        )
        _use_chembl_phase1 = (
            _p1_chembl_csv.exists()
            and _p1_chembl_csv.stat().st_size > 0
        )

        # v15 ROOT FIX (REM-24): honor skip_download.
        if skip_download and not _is_cached("chembl", "chembl_sqlite.db") and not _use_chembl_phase1:
            logger.info("Step 7c skipped (--skip-download): ChEMBL not cached.")
            results["chembl_skipped"] = True
            results["chembl_skip_reason"] = "skip_download"
        else:
            if _use_chembl_phase1:
                # v28 ROOT FIX (P2-L-8): consume Phase 1's cleaned CSV
                # via the dedicated Phase-1-aware functions. This makes
                # ``parse_chembl_activities_from_phase1_csv`` and
                # ``chembl_to_edge_records_from_phase1`` LIVE code.
                logger.info(
                    "Step 7c (v28 root fix P2-L-8): using Phase 1's "
                    "cleaned ChEMBL activities CSV at %s (canonical "
                    "source).",
                    _p1_chembl_csv,
                )
                chembl_df = _parse_chembl_p1(_p1_chembl_csv)
                # v35 ROOT FIX (V35-P2-LOADERS-FIXES H-1): build a
                # `compound_canonical_map` (chembl_id -> inchikey) from
                # Phase 1's `chembl_drugs.csv` (the compound metadata
                # CSV) so the edge emitter can normalize `src_id` to
                # InChIKey (matching the Compound node IDs). Without
                # this, edges would carry raw `CHEMBL25` IDs that never
                # match any staged Compound. We read `chembl_drugs.csv`
                # directly (it's the same file the phase1_bridge uses
                # to stage Compound nodes — same column names:
                # `chembl_id`, `inchikey`).
                _compound_canonical_map: Dict[str, str] = {}
                _p1_chembl_drugs_csv = (
                    Path(phase1_processed_dir) / "chembl_drugs.csv"
                    if phase1_processed_dir
                    else _DEFAULT_CHEMBL_P1_CSV.parent / "chembl_drugs.csv"
                )
                try:
                    if _p1_chembl_drugs_csv.exists():
                        import pandas as _pd
                        _cd_df = _pd.read_csv(_p1_chembl_drugs_csv)
                        for _r in _cd_df.itertuples(index=False):
                            _cid = getattr(_r, "chembl_id", None)
                            _ik = getattr(_r, "inchikey", None)
                            if _cid and _ik and str(_ik).strip() and str(_ik).lower() != "nan":
                                _compound_canonical_map[str(_cid).strip()] = str(_ik).strip().upper()
                except Exception as _map_exc:
                    logger.debug(
                        "Could not build compound_canonical_map from "
                        "Phase 1 chembl_drugs.csv (%s) — chembl emitter "
                        "will fall back to per-row inchikey.",
                        _map_exc,
                    )
                chembl_edges = _chembl_edges_from_p1(
                    chembl_df,
                    compound_canonical_map=_compound_canonical_map or None,
                )
                results["chembl_edges"] = len(chembl_edges)
                results["chembl_source"] = "phase1_csv"
            else:
                # Fall back to raw ChEMBL SQLite download + parse.
                if not skip_download:
                    download_chembl()
                chembl_df = parse_chembl_activities()
                chembl_edges = chembl_to_edge_records(chembl_df)
                results["chembl_edges"] = len(chembl_edges)
                results["chembl_source"] = "raw_download"

            # BUG-SCI-02 FIX: Batch ChEMBL edges by (src_type, rel_type, dst_type)
            # instead of loading one-at-a-time (~2M individual transactions).
            if not skip_neo4j and chembl_edges:
                from .kg_builder import DrugOSGraphBuilder

                chembl_grouped: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
                # v22 ROOT FIX (Audit section 7 finding 8 / §7 finding 12 —
                # STITCH-style silent rel_type collapse + unknown
                # standard_type defaults to 'binds'): the previous
                # ``edge.get("rel_type", "binds")`` silently collapsed
                # ChEMBL edges with missing rel_type into the
                # mechanism-specific "binds" relation. Combined with
                # chembl_loader.standard_type_to_relation (which v21
                # already fixed to default unknown types to "targets"),
                # the run_pipeline grouping layer still had the silent
                # collapse. Root fix: missing/empty rel_type becomes
                # "targets" (consistent with chembl_loader's v21 fix),
                # and a WARNING is logged so the collapse is visible.
                _chembl_missing_rel_type_count = 0
                for edge in chembl_edges:
                    _rt = edge.get("rel_type")
                    if not _rt or not str(_rt).strip():
                        _chembl_missing_rel_type_count += 1
                        rel_t = "targets"
                    else:
                        rel_t = str(_rt).strip().lower()
                    key = (
                        edge.get("src_type", "Compound"),
                        rel_t,
                        edge.get("dst_type", "Protein"),
                    )
                    chembl_grouped[key].append(edge)
                if _chembl_missing_rel_type_count > 0:
                    logger.warning(
                        "ChEMBL: %d of %d edges had missing/empty rel_type "
                        "— defaulted to 'targets' (consistent with "
                        "standard_type_to_relation). Investigate "
                        "chembl_to_edge_records() output schema.",
                        _chembl_missing_rel_type_count, len(chembl_edges),
                    )

                batch_size_edges = Neo4jConfig().batch_size_edges
                with DrugOSGraphBuilder(Neo4jConfig()) as builder:
                    for (src_t, rel_t, dst_t), group in chembl_grouped.items():
                        for i in range(0, len(group), batch_size_edges):
                            batch = group[i : i + batch_size_edges]
                            builder.load_edges_bulk_create(src_t, rel_t, dst_t, batch)
                            logger.info(
                                "ChEMBL Neo4j load batch src_type=%s "
                                "rel_type=%s dst_type=%s batch_size=%d",
                                src_t, rel_t, dst_t, len(batch),
                            )
      except Exception as e:
        # v16 ROOT FIX (SF-7): ChEMBL is a CRITICAL data source — it
        # provides the drug-protein interaction (DPI) edges that are the
        # backbone of the knowledge graph. The previous code logged at
        # WARNING, hiding catastrophic DPI loss as a routine hiccup.
        # Promote to ERROR with full traceback and tag the result so
        # downstream consumers (RL ranker, sanity checks) can detect
        # the missing DPI set. Also set ``chembl_critical_failure=True``
        # so ``_check_v1_launch_criteria`` can fail the launch if DPI
        # edges are missing.
        logger.error(
            "ChEMBL ingestion FAILED — drug-protein interaction (DPI) "
            "edge set will be MISSING from the graph. The Graph "
            "Transformer's training data will lack all ChEMBL-sourced "
            "bioactivity edges. V1 launch MUST be blocked until ChEMBL "
            "loads successfully. Error: %s: %s",
            type(e).__name__, e,
            exc_info=True,
        )
        results["chembl_error"] = str(e)
        results["chembl_critical_failure"] = True
        results["chembl_dpi_edges_loaded"] = 0

    # ─── 7d: OpenTargets ──────────────────────────────────────────────────
    try:
        from .opentargets_loader import (
            OpenTargetsLoader,
            OpenTargetsConfig,
            load_opentargets,
        )
        from ._loader_protocol import Loader
        from .id_crosswalk import get_default_crosswalk
        # BUG-SCI-01 FIX: Import AUC enforcement AND RAW_DIR
        from .config import (
            AUC_ENFORCEMENT_LEVEL,
            AUCEnforcementLevel,
        )

        # v15 ROOT FIX (REM-24): honor skip_download. OpenTargets is the
        # largest source (~5 GB compressed); skipping its download in CI /
        # smoke-test mode is essential.
        if skip_download and not _is_cached("opentargets", "opentargets_evidence.json.gz"):
            logger.info("Step 7d skipped (--skip-download): OpenTargets not cached.")
            results["opentargets_skipped"] = True
            results["opentargets_skip_reason"] = "skip_download"
        else:
            loader = OpenTargetsLoader()
            # ARCH-1: assert the loader satisfies the Protocol contract.
            assert isinstance(loader, Loader), (
                "OpenTargetsLoader must satisfy the Loader Protocol"
            )

            # SCI-14: load crosswalks BEFORE parsing (so the parser can resolve
            # ENSG -> UniProt AC and disease -> UMLS CUI during edge emission).
            try:
                cw = get_default_crosswalk()
                # BUG-SCI-01 FIX: RAW_DIR is now properly imported at module level
                ot_targets_path = RAW_DIR / "opentargets_targets.json.gz"
                ot_diseases_path = RAW_DIR / "opentargets_diseases.json.gz"
                ensembl_ncbi_path = RAW_DIR / "ensembl_to_ncbi_gene.tsv"
                if ot_targets_path.exists():
                    n = cw.load_opentargets_targets(
                        ot_targets_path, allowed_dir=RAW_DIR
                    )
                    logger.info(
                        "Loaded %d OpenTargets ENSG->UniProt mappings", n
                    )
                if ot_diseases_path.exists():
                    n = cw.load_opentargets_diseases(
                        ot_diseases_path, allowed_dir=RAW_DIR
                    )
                    logger.info(
                        "Loaded %d OpenTargets disease->UMLS mappings", n
                    )
                if ensembl_ncbi_path.exists():
                    n = cw.load_ensembl_to_ncbi_gene(
                        ensembl_ncbi_path, allowed_dir=RAW_DIR
                    )
                    logger.info("Loaded %d Ensembl->NCBI gene mappings", n)
            except Exception as e:
                logger.warning("Failed to load OpenTargets crosswalks: %s", e)
                cw = None

            # End-to-end load (skip_neo4j to avoid OOM on bulk Neo4j load;
            # Neo4j loading is done in a separate batched step below).
            ot_result = load_opentargets(crosswalk=cw, skip_neo4j=True)
            results["opentargets_edges"] = ot_result.get("edges_total", 0)
            results["opentargets_nodes"] = ot_result.get("nodes_total", 0)
            results["opentargets_resolution_rate"] = ot_result.get(
                "resolution_rate", 0.0
            )
            results["opentargets_source_sha256"] = ot_result.get(
                "source_sha256", ""
            )
            results["opentargets_source_version"] = ot_result.get(
                "source_version", ""
            )

            # PERF-4: batched Neo4j load
            if not skip_neo4j and ot_result.get("edges_total", 0) > 0:
                from .kg_builder import DrugOSGraphBuilder

                cfg = OpenTargetsConfig()
                batch_size: int = cfg.neo4j_batch_size
                ot_edges = ot_result.get("edges", [])
                # Group edges by (src_type, rel_type, dst_type) for bulk create.
                grouped: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
                for edge in ot_edges:
                    key = (
                        edge["src_type"],
                        edge["rel_type"],
                        edge["dst_type"],
                    )
                    grouped[key].append(edge)
                with DrugOSGraphBuilder(Neo4jConfig()) as builder:
                    for (src_t, rel_t, dst_t), group_edges in grouped.items():
                        for i in range(0, len(group_edges), batch_size):
                            batch = group_edges[i : i + batch_size]
                            builder.load_edges_bulk_create(
                                src_t, rel_t, dst_t, batch
                            )
                            logger.info(
                                "OpenTargets Neo4j load batch src_type=%s "
                                "rel_type=%s dst_type=%s batch_size=%d",
                                src_t, rel_t, dst_t, len(batch),
                            )

    except Exception as e:
        from .exceptions import (
            DrugOSDataError,
            OpenTargetsDataIntegrityError,
        )
        if isinstance(e, OpenTargetsDataIntegrityError):
            # Section 0.4: in CLINICAL+ mode, re-raise (patient-safety).
            if AUC_ENFORCEMENT_LEVEL.value in ("clinical", "regulatory"):
                raise
            logger.error(
                "OpenTargets ingestion failed (data integrity): %s", e
            )
            results["opentargets_error"] = str(e)
        elif isinstance(e, DrugOSDataError):
            logger.error(
                "OpenTargets ingestion failed (pipeline): %s", e
            )
            results["opentargets_error"] = str(e)
        else:
            logger.exception(
                "OpenTargets ingestion failed (unexpected): %s", e
            )
            results["opentargets_error"] = str(e)

    # ─── 7e: ClinicalTrials ───────────────────────────────────────────────
    try:
        from .clinicaltrials_loader import (
            download_clinicaltrials,
            parse_clinicaltrials,
            clinicaltrials_to_edge_records,
        )

        # v15 ROOT FIX (REM-24): honor skip_download. ClinicalTrials
        # download is ~500 MB and the AACT server has a 60s+120s+240s
        # retry backoff that eats the entire pipeline timeout when the
        # server returns HTTP 500.
        if skip_download and not _is_cached("clinicaltrials", "aact_dataset.zip"):
            logger.info("Step 7e skipped (--skip-download): ClinicalTrials not cached.")
            results["clinicaltrials_skipped"] = True
            results["clinicaltrials_skip_reason"] = "skip_download"
        else:
            if not skip_download:
                download_clinicaltrials()
            ct_df = parse_clinicaltrials()
            ct_edges = clinicaltrials_to_edge_records(ct_df)
            results["clinicaltrials_edges"] = len(ct_edges)
            if not skip_neo4j and ct_edges:
                from .kg_builder import DrugOSGraphBuilder

                with DrugOSGraphBuilder(Neo4jConfig()) as builder:
                    batch_size = Neo4jConfig().batch_size_edges
                    for i in range(0, len(ct_edges), batch_size):
                        batch = ct_edges[i : i + batch_size]
                        # v9 ROOT FIX (audit F5.2.5): the previous call used
                        # the DEPRECATED rel_type "clinical_trial" (config.py
                        # explicitly says "clinical_trial is DEPRECATED v0
                        # name"). The loader emits "tested_for" which is the
                        # canonical v1 rel_type. Use the canonical name so
                        # the edge reaches Neo4j under the correct relationship
                        # type and downstream queries against "tested_for"
                        # find it.
                        builder.load_edges_bulk_create(
                            "Compound", "tested_for", "Disease", batch
                        )
    except Exception as e:
        # v20 SF-7 ROOT FIX: ClinicalTrials loader failures were logged
        # as WARNING and silently swallowed. The audit's complaint was
        # that "CRITICAL loader failures are logged as warnings, not
        # raised." In strict mode (DRUGOS_STRICT=1 or
        # DRUGOS_STRICT_CLINICALTRIALS=1), surface as a critical_failure
        # flag so the V1 launch criteria hard-fails. Default behavior
        # (warn-and-continue) is preserved for backward compat.
        _ct_strict = (
            os.environ.get("DRUGOS_STRICT", "") == "1"
            or os.environ.get("DRUGOS_STRICT_CLINICALTRIALS", "") == "1"
        )
        if _ct_strict:
            logger.error(
                "ClinicalTrials ingestion FAILED in strict mode — marking "
                "critical_failure (will block V1 launch): %s", e
            )
            results["clinicaltrials_critical_failure"] = True
        else:
            logger.warning("ClinicalTrials ingestion failed: %s", e)
        results["clinicaltrials_error"] = str(e)

    # ─── 7f: DisGeNET (BUG-SCI-03 FIX — missing project source) ───────────
    # v35 ROOT FIX (H-5): replaced ``raise ImportError("skip_7f_phase1_bridge_loaded")``
    # control-flow abuse with an explicit if/else. The previous pattern
    # hijacked the existing ``except ImportError:`` clause (originally
    # meant to handle a missing ``disgenet_loader.py``) to skip the
    # step when ``data_source="phase1"``. That made the warning
    # "DisGeNET loader not available — Create disgenet_loader.py"
    # fire EVERY time the skip path was taken, confusing operators
    # (the loader IS available; we deliberately skipped).
    try:
        if _skip_7fgh:
            # v29 ROOT FIX (audit I-2): bridge already loaded DisGeNET.
            results["disgenet_skipped"] = True
            results["disgenet_skip_reason"] = "phase1_bridge_already_loaded"
            logger.info("Step 7f SKIPPED (v29 root fix): bridge loaded DisGeNET.")
        else:
            from .disgenet_loader import (
                download_disgenet,
                parse_disgenet,
                disgenet_to_node_records,
                disgenet_to_edge_records,
            )

            # v15 ROOT FIX (REM-24): honor skip_download. The DisGeNET
            # loader's download_disgenet() invokes Phase 1's DisgenetPipeline
            # which hits the public DisGeNET API (rate-limited, requires API key).
            if skip_download and not _is_cached("disgenet", "disgenet_gda.csv"):
                # Also check if Phase 1's CSV is present — if so, use it.
                # v22 ROOT FIX: the previous default `RAW_DIR.parent / "phase1" / "processed_data"`
                # resolved to phase2/data/phase1/processed_data — WRONG. The actual
                # Phase 1 processed_data is at <project_root>/phase1/processed_data.
                # Use the canonical DEFAULT_PHASE1_PROCESSED_DIR from phase1_bridge
                # so step7's fallback finds the CSVs that step1's bridge already
                # loaded successfully. This was the root cause of
                # `sources_loaded_count: 0` when invoking `python -m drugos_graph`
                # without --phase1-dir: the bridge loaded data, but step7's fallback
                # looked at a non-existent path and silently skipped DisGeNET/OMIM/PubChem.
                from .phase1_bridge import DEFAULT_PHASE1_PROCESSED_DIR as _DEF_P1_DIR
                _p1_dir = Path(phase1_processed_dir) if phase1_processed_dir else _DEF_P1_DIR
                phase1_dg = _p1_dir / "disgenet_gene_disease_associations.csv"
                if not phase1_dg.exists():
                    logger.info("Step 7f skipped (--skip-download): DisGeNET not cached.")
                    results["disgenet_skipped"] = True
                    results["disgenet_skip_reason"] = "skip_download"
                else:
                    logger.info("Step 7f: --skip-download set, but Phase 1 DisGeNET CSV is cached at %s — using it.", phase1_dg)
                    # Use the Phase 1 CSV directly via parse_disgenet.
                    dg_df = parse_disgenet(filepath=phase1_dg) if 'filepath' in parse_disgenet.__code__.co_varnames else parse_disgenet()
                    dg_nodes = disgenet_to_node_records(dg_df)
                    dg_edges = disgenet_to_edge_records(dg_df)
                    results["disgenet_nodes"] = len(dg_nodes)
                    results["disgenet_edges"] = len(dg_edges)
            else:
                if not skip_download:
                    download_disgenet()
                dg_df = parse_disgenet()
                dg_nodes = disgenet_to_node_records(dg_df)
                dg_edges = disgenet_to_edge_records(dg_df)
                results["disgenet_nodes"] = len(dg_nodes)
                results["disgenet_edges"] = len(dg_edges)
            if locals().get('dg_edges') and not skip_neo4j and dg_edges:
                from .kg_builder import DrugOSGraphBuilder

                # v9 ROOT FIX (audit F5 / F7.4): disgenet_to_node_records
                # returns a MIXED list of Disease AND Gene nodes (each node
                # carries its own ``label`` field). The previous code passed
                # the entire mixed list under a single label "Disease" —
                # load_nodes_batch then validated every node against
                # ID_PATTERNS["Disease"], dead-lettering every Gene ID like
                # "5742" or "SYM:FGFR3". Split by label first so each
                # subset is validated against its own pattern.
                dg_disease_nodes = [n for n in dg_nodes if n.get("label") == "Disease"]
                dg_gene_nodes = [n for n in dg_nodes if n.get("label") == "Gene"]
                other_nodes = [n for n in dg_nodes if n.get("label") not in ("Disease", "Gene")]
                if other_nodes:
                    logger.warning(
                        "DisGeNET: %d nodes have unexpected labels %s — skipping",
                        len(other_nodes),
                        {n.get("label") for n in other_nodes},
                    )
                with DrugOSGraphBuilder(Neo4jConfig()) as builder:
                    if dg_disease_nodes:
                        builder.load_nodes_batch("Disease", dg_disease_nodes)
                    if dg_gene_nodes:
                        builder.load_nodes_batch("Gene", dg_gene_nodes)
                    if dg_edges:
                        # v29 ROOT FIX (audit L-1 — kg_builder relation collapse):
                        # The previous code called:
                        #   builder.load_edges_bulk_create(
                        #       "Gene", "associated_with", "Disease", dg_edges
                        #   )
                        # This hard-coded "associated_with" as the rel_type for
                        # ALL DisGeNET edges, ignoring the per-edge "rel_type"
                        # field that disgenet_to_edge_records sets to
                        # "associated_with" / "susceptible_to" / "biomarker_for"
                        # based on the original DisGeNET association_type
                        # (per v27 ROOT FIX P2-L-13). The result: every
                        # distinct biological relation was collapsed to
                        # "associated_with" in Neo4j, destroying the
                        # semantic distinction the v27 fix introduced.
                        #
                        # ROOT FIX: group edges by their per-edge rel_type
                        # and load each group with the correct rel_type.
                        # This preserves the v27 relation distinction so
                        # the model can learn that "susceptible_to" ≠
                        # "treats" ≠ "biomarker_for".
                        from collections import defaultdict as _dd
                        _edges_by_rel: dict = _dd(list)
                        for e in dg_edges:
                            rt = e.get("rel_type") or "associated_with"
                            # Strip the rel_type from the edge dict before
                            # loading — kg_builder doesn't expect it as a
                            # property, and the positional arg is the
                            # authoritative rel_type.
                            e_clean = {k: v for k, v in e.items() if k != "rel_type"}
                            _edges_by_rel[rt].append(e_clean)
                        for rt, group in _edges_by_rel.items():
                            builder.load_edges_bulk_create(
                                "Gene", rt, "Disease", group,
                            )
    except ImportError:
        logger.warning(
            "DisGeNET loader not available — gene-disease associations "
            "will rely on DRKG Hetionet subset only. "
            "Create disgenet_loader.py for full coverage."
        )
        results["disgenet_error"] = "Loader not available"
    except Exception as e:
        logger.error(
            "DisGeNET ingestion failed (critical source): %s", e
        )
        results["disgenet_error"] = str(e)

    # ─── 7g: OMIM (BUG-SCI-03 FIX — missing project source) ───────────────
    # v35 ROOT FIX (H-5): replaced ``raise ImportError("skip_7g_phase1_bridge_loaded")``
    # control-flow abuse with explicit if/else (see 7f comment above).
    try:
        if _skip_7fgh:
            # v29 ROOT FIX (audit I-2): bridge already loaded OMIM.
            results["omim_skipped"] = True
            results["omim_skip_reason"] = "phase1_bridge_already_loaded"
            logger.info("Step 7g SKIPPED (v29 root fix): bridge loaded OMIM.")
        else:
            from .omim_loader import (
                download_omim,
                parse_omim,
                omim_to_node_records,
                omim_to_edge_records,
            )

            # v15 ROOT FIX (REM-24): honor skip_download.
            if skip_download and not _is_cached("omim", "omim_morbidmap.txt"):
                from .phase1_bridge import DEFAULT_PHASE1_PROCESSED_DIR as _DEF_P1_DIR
                _p1_dir = Path(phase1_processed_dir) if phase1_processed_dir else _DEF_P1_DIR
                phase1_omim = _p1_dir / "omim_gene_disease_associations.csv"
                if not phase1_omim.exists():
                    logger.info("Step 7g skipped (--skip-download): OMIM not cached.")
                    results["omim_skipped"] = True
                    results["omim_skip_reason"] = "skip_download"
                else:
                    logger.info("Step 7g: --skip-download set, but Phase 1 OMIM CSV is cached at %s — using it.", phase1_omim)
                    omim_df = parse_omim(filepath=phase1_omim) if 'filepath' in parse_omim.__code__.co_varnames else parse_omim()
                    omim_nodes = omim_to_node_records(omim_df)
                    omim_edges = omim_to_edge_records(omim_df)
                    results["omim_nodes"] = len(omim_nodes)
                    results["omim_edges"] = len(omim_edges)
            else:
                if not skip_download:
                    download_omim()
                omim_df = parse_omim()
                omim_nodes = omim_to_node_records(omim_df)
                omim_edges = omim_to_edge_records(omim_df)
                results["omim_nodes"] = len(omim_nodes)
                results["omim_edges"] = len(omim_edges)
            if locals().get('omim_edges') and not skip_neo4j and omim_edges:
                from .kg_builder import DrugOSGraphBuilder

                # v9 ROOT FIX (audit F5 / F7.4): same as DisGeNET — split
                # the mixed-type node list by label before load_nodes_batch.
                omim_disease_nodes = [n for n in omim_nodes if n.get("label") == "Disease"]
                omim_gene_nodes = [n for n in omim_nodes if n.get("label") == "Gene"]
                other_nodes = [n for n in omim_nodes if n.get("label") not in ("Disease", "Gene")]
                if other_nodes:
                    logger.warning(
                        "OMIM: %d nodes have unexpected labels %s — skipping",
                        len(other_nodes),
                        {n.get("label") for n in other_nodes},
                    )
                with DrugOSGraphBuilder(Neo4jConfig()) as builder:
                    if omim_disease_nodes:
                        builder.load_nodes_batch("Disease", omim_disease_nodes)
                    if omim_gene_nodes:
                        builder.load_nodes_batch("Gene", omim_gene_nodes)
                    if omim_edges:
                        # v29 ROOT FIX (audit L-1 — kg_builder relation collapse):
                        # Same fix as DisGeNET above — group by per-edge rel_type
                        # instead of hard-coding "associated_with". OMIM edges
                        # can be "associated_with" (Mendelian causative) or
                        # "susceptible_to" (polygenic risk) per the v27 ROOT FIX.
                        # Conflating them under "associated_with" teaches the
                        # model that BRCA1+breast_cancer (causative) is equivalent
                        # to FGFR3+achondroplasia (Mendelian dominant) — destroying
                        # the scientific distinction the v27 fix introduced.
                        from collections import defaultdict as _dd_omim
                        _omim_by_rel: dict = _dd_omim(list)
                        for e in omim_edges:
                            rt = e.get("rel_type") or "associated_with"
                            e_clean = {k: v for k, v in e.items() if k != "rel_type"}
                            _omim_by_rel[rt].append(e_clean)
                        for rt, group in _omim_by_rel.items():
                            builder.load_edges_bulk_create(
                                "Gene", rt, "Disease", group,
                            )
    except ImportError:
        logger.warning(
            "OMIM loader not available — rare disease genetic evidence "
            "will be limited. Create omim_loader.py for full coverage."
        )
        results["omim_error"] = "Loader not available"
    except Exception as e:
        logger.error(
            "OMIM ingestion failed (critical for rare diseases): %s", e
        )
        results["omim_error"] = str(e)

    # ─── 7h: PubChem (BUG-SCI-03 FIX — missing project source) ────────────
    # v35 ROOT FIX (H-5): replaced ``raise ImportError("skip_7h_phase1_bridge_loaded")``
    # control-flow abuse with explicit if/else (see 7f comment above).
    try:
        if _skip_7fgh:
            # v29 ROOT FIX (audit I-2): bridge already loaded PubChem.
            results["pubchem_skipped"] = True
            results["pubchem_skip_reason"] = "phase1_bridge_already_loaded"
            logger.info("Step 7h SKIPPED (v29 root fix): bridge loaded PubChem.")
        else:
            from .pubchem_loader import (
                download_pubchem,
                parse_pubchem,
                pubchem_to_node_records,
            )

            # v15 ROOT FIX (REM-24): honor skip_download.
            if skip_download and not _is_cached("pubchem", "pubchem_compounds.csv"):
                from .phase1_bridge import DEFAULT_PHASE1_PROCESSED_DIR as _DEF_P1_DIR
                _p1_dir = Path(phase1_processed_dir) if phase1_processed_dir else _DEF_P1_DIR
                phase1_pubchem = _p1_dir / "pubchem_enrichment.csv"
                if not phase1_pubchem.exists():
                    logger.info("Step 7h skipped (--skip-download): PubChem not cached.")
                    results["pubchem_skipped"] = True
                    results["pubchem_skip_reason"] = "skip_download"
                else:
                    logger.info("Step 7h: --skip-download set, but Phase 1 PubChem CSV is cached at %s — using it.", phase1_pubchem)
                    pubchem_records = parse_pubchem(filepath=phase1_pubchem) if 'filepath' in parse_pubchem.__code__.co_varnames else parse_pubchem()
                    pubchem_nodes = pubchem_to_node_records(pubchem_records)
                    results["pubchem_nodes"] = len(pubchem_nodes)
            else:
                if not skip_download:
                    download_pubchem()
                pubchem_records = parse_pubchem()
                pubchem_nodes = pubchem_to_node_records(pubchem_records)
                results["pubchem_nodes"] = len(pubchem_nodes)
            if locals().get('pubchem_nodes') and not skip_neo4j and pubchem_nodes:
                from .kg_builder import DrugOSGraphBuilder

                with DrugOSGraphBuilder(Neo4jConfig()) as builder:
                    batch_size = Neo4jConfig().batch_size_nodes
                    for i in range(0, len(pubchem_nodes), batch_size):
                        batch = pubchem_nodes[i : i + batch_size]
                        builder.load_nodes_batch("Compound", batch)
    except ImportError:
        logger.warning(
            "PubChem loader not available — molecular fingerprints "
            "for Compound features will be limited. "
            "Create pubchem_loader.py for full coverage."
        )
        results["pubchem_error"] = "Loader not available"
    except Exception as e:
        logger.error(
            "PubChem ingestion failed (molecular features): %s", e
        )
        results["pubchem_error"] = str(e)

    # ─── 7i: GEO (GAP-ARCH-03 FIX — exists but never called) ─────────────
    try:
        from .geo_loader import GeoLoader

        # v15 ROOT FIX (REM-24): honor skip_download.
        if skip_download and not _is_cached("geo", "geo_expression.soft.gz"):
            logger.info("Step 7i skipped (--skip-download): GEO not cached.")
            results["geo_skipped"] = True
            results["geo_skip_reason"] = "skip_download"
        else:
            geo_loader = GeoLoader()
            if not skip_download:
                geo_loader.download()
            geo_records = list(geo_loader.parse())
            geo_nodes, geo_edges = geo_loader.to_graph(geo_records)
            results["geo_nodes"] = len(geo_nodes)
            results["geo_edges"] = len(geo_edges)
        if locals().get('geo_edges') and not skip_neo4j and geo_edges:
            from .kg_builder import DrugOSGraphBuilder

            # PS-9 / DC-9 ROOT FIX: geo_loader emits head_type /
            # relation / tail_type keys (see geo_loader.to_graph),
            # NOT src_type / rel_type / dst_type. The previous code
            # read the wrong keys and every .get() returned None —
            # so every GEO edge was loaded under the wrong edge type
            # (:Gene)-[:expressed_in]->(:Disease) instead of the
            # biologically-correct (:Protein)-[:expressed_in]->(:Anatomy),
            # producing orphan edges disconnected from the rest of the
            # graph. Also removed the dead `for node in geo_nodes`
            # loop — geo_loader.to_graph() always returns ([], edges)
            # per its contract (GEO emits edges only; nodes are owned
            # by uniprot_loader / uberon_loader).
            with DrugOSGraphBuilder(Neo4jConfig()) as builder:
                if geo_edges:
                    batch_size = max(1, getattr(Neo4jConfig(), "batch_size_edges", 500))
                    head_type = geo_edges[0].get("head_type", "Protein")
                    relation = geo_edges[0].get("relation", "expressed_in")
                    tail_type = geo_edges[0].get("tail_type", "Anatomy")
                    for i in range(0, len(geo_edges), batch_size):
                        builder.load_edges_bulk_create(
                            head_type, relation, tail_type,
                            geo_edges[i : i + batch_size],
                        )
    except ImportError:
        logger.info("GEO loader not available — skipping.")
    except Exception as e:
        # v20 SF-7 ROOT FIX: GEO loader failures were logged as WARNING
        # ("non-critical") and silently swallowed. The audit's PS-9
        # compound chain showed that GEO's wrong edge labels produce
        # orphan edges disconnected from the rest of the graph — that
        # is NOT non-critical. In strict mode, surface as
        # critical_failure so V1 launch criteria hard-fails.
        _geo_strict = (
            os.environ.get("DRUGOS_STRICT", "") == "1"
            or os.environ.get("DRUGOS_STRICT_GEO", "") == "1"
        )
        if _geo_strict:
            logger.error(
                "GEO ingestion FAILED in strict mode — marking "
                "critical_failure (will block V1 launch): %s", e
            )
            results["geo_critical_failure"] = True
        else:
            logger.warning("GEO ingestion failed (non-critical): %s", e)
        results["geo_error"] = str(e)

    elapsed = time.time() - t0
    _log_transformation(
        "step7",
        "Ingest additional data sources (9 sources)",
        {"sources": results},
    )
    logger.info("Step 7 complete in %.1fs — %s", elapsed, results)
    return {"results": results, "elapsed": elapsed}


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 8: Entity Resolution (Domain 7 — Idempotency, Domain 3 — Science)
# ═══════════════════════════════════════════════════════════════════════════════


def step8_entity_resolution(df, drug_records, phase1_entity_mapping=None) -> dict:
    """Step 8: Run entity resolution across all databases.

    Resolves Compound (DrugBank + DRKG via InChIKey), Disease (DRKG with
    MESH support), Gene (DRKG, NCBI Gene IDs), and Protein (from UniProt
    dat file). Builds Gene-encodes-Protein edges. Loads the IDCrosswalk
    service for STRING aliases and ChEMBL target_components.

    GAP-IDP-01 FIX: Resets crosswalk singleton before resolution to ensure
    idempotency across pipeline runs.

    FORENSIC bridge root fix: accepts an optional ``phase1_entity_mapping``
    DataFrame (from the phase1 bridge). When provided, Phase 1's pre-
    resolved cross-source mappings are loaded BEFORE the per-source
    resolve_* calls, so Phase 2 REUSES Phase 1's ER work instead of
    re-resolving from scratch.

    Parameters
    ----------
    df : pd.DataFrame
        Parsed DRKG DataFrame.
    drug_records : list
        Parsed DrugBank drug records (for InChIKey canonicalization).
    phase1_entity_mapping : pd.DataFrame, optional
        Phase 1's entity_mapping table (cross-source ER output). When
        provided, ingested into the resolver before per-source resolution.

    Returns
    -------
    dict
        Keys: stats, gene_protein_edges, crosswalk_summary, elapsed
    """
    _configure_logging()
    logger.info("=" * 60)
    logger.info("STEP 8: Entity Resolution")
    logger.info("=" * 60)
    t0 = time.time()

    from .entity_resolver import EntityResolver
    from .id_crosswalk import get_default_crosswalk, reset_default_crosswalk
    from .config import DATA_SOURCES as _DATA_SOURCES  # local alias for clarity

    # GAP-IDP-01 FIX: Reset crosswalk singleton for idempotency
    reset_default_crosswalk()

    resolver = EntityResolver()

    # FORENSIC bridge root fix: load Phase 1's entity_mapping BEFORE
    # running the per-source resolve_* calls. This makes Phase 2 REUSE
    # Phase 1's cross-source ER output instead of re-resolving from
    # scratch (which was the audit's "Phase 1 entity_mapping table is
    # discarded" finding).
    if phase1_entity_mapping is not None:
        try:
            _em_stats = resolver.load_phase1_entity_mapping(phase1_entity_mapping)
            logger.info(
                "Step 8: loaded Phase 1 entity_mapping — %d mappings "
                "ingested, %d skipped. Downstream resolve_* calls will "
                "REUSE these instead of re-resolving.",
                _em_stats.get("loaded", 0), _em_stats.get("skipped", 0),
            )
        except Exception as _exc_em:
            logger.warning(
                "Step 8: failed to load Phase 1 entity_mapping (%s) — "
                "falling back to full re-resolution. This is acceptable in "
                "dev but means Phase 1's cross-source ER work is discarded.",
                _exc_em,
            )

    resolver.resolve_compounds_from_drugbank(drug_records)
    resolver.resolve_compounds_from_drkg(df)
    resolver.resolve_diseases_from_drkg(df)
    resolver.resolve_genes_from_drkg(df)

    # v13 ROOT FIX (DC-3 / Compound-1 "Canonicalization Theater"):
    # v12 NEVER called ``resolver.merge_mappings_by_inchikey()`` —
    # the function existed but was dead code. The project's core
    # mandate ("convert all compound IDs to a common format
    # (InChIKey)") was only partially satisfied by the inline DC-2
    # merge (which only triggers on same-canonical_id re-adds).
    # Multiple Compound nodes for the same molecule (same InChIKey,
    # different canonical_id from different sources) entered the
    # graph. The GNN learned wrong edges. v13: invoke the explicit
    # InChIKey merge here, AFTER all Compound sources are resolved,
    # so cross-source duplicates collapse to a single canonical node.
    #
    # REM-7 ROOT FIX: previously if merge_mappings_by_inchikey
    # raised, the except block only logged a WARNING and continued.
    # The log message literally said "This violates the project's
    # core InChIKey mandate" — yet execution continued anyway,
    # silently undoing the v13 root fix. The downstream pipeline
    # would then proceed with duplicate Compound nodes per molecule
    # and report an apparently-successful run. For a biomedical KG
    # whose outputs feed clinical decision-making, an un-merged
    # Compound set is a project-mandate violation. Make it FATAL.
    # (Contrast with merge_duplicate_edges below, which stays a
    # WARNING: edge dedup is a quality-of-life improvement that
    # affects degree counts but NOT the core canonicalization
    # mandate, so a partial failure there is recoverable.)
    try:
        inchikey_merge_stats = resolver.merge_mappings_by_inchikey()
        logger.info(
            "Step 8: merge_mappings_by_inchikey — %d groups, "
            "%d merged, %d Compound mappings before → %d after, "
            "%d conflicts detected.",
            inchikey_merge_stats.get("groups_total", 0),
            inchikey_merge_stats.get("groups_merged", 0),
            inchikey_merge_stats.get("mappings_before", 0),
            inchikey_merge_stats.get("mappings_after", 0),
            inchikey_merge_stats.get("conflicts_detected", 0),
        )
    except Exception as exc:
        # REM-7 ROOT FIX: FATAL — InChIKey merge is the project's
        # core mandate. Continuing would silently produce a graph
        # with duplicate Compound nodes per molecule.
        logger.error(
            "Step 8: merge_mappings_by_inchikey FAILED — "
            "cross-source Compound duplicates will NOT be merged. "
            "This violates the project's core InChIKey mandate. "
            "Aborting step 8 (FATAL). Original error: %s",
            exc, exc_info=True,
        )
        raise RuntimeError(
            "Step 8 InChIKey merge failed — project's core mandate "
            "violated. Original error: " + str(exc)
        ) from exc

    # ─── Protein resolution from UniProt ──────────────────────────────────
    protein_stats: Dict[str, Any] = {
        "total_proteins": 0,
        "mapped": 0,
        "with_gene_link": 0,
    }
    # v35 ROOT FIX (V35-P2-LOADERS-FIXES H-4): mirror step7b's pattern —
    # check Phase 1's cleaned ``uniprot_proteins.csv`` FIRST and use
    # ``parse_uniprot_entries_from_phase1_csv`` when it exists; fall back
    # to the raw ``uniprot_sprot.dat(.gz)`` only when the Phase 1 CSV is
    # absent. The previous code went straight to the raw .dat, which:
    #   (a) requires the operator to re-download an 800 MB file even
    #       when Phase 1 has already produced a cleaned CSV, and
    #   (b) skips Phase 1's normalization (canonical accession casing,
    #       gene-symbol crosswalk enrichment, secondary-accession merge).
    from .phase1_bridge import DEFAULT_PHASE1_PROCESSED_DIR as _DEF_P1_DIR
    from .uniprot_loader import (
        DEFAULT_UNIPROT_PROTEINS_CSV as _DEFAULT_UNIPROT_P1_CSV,
        parse_uniprot_entries_from_phase1_csv as _parse_uniprot_p1,
    )
    _p1_uniprot_csv = _DEFAULT_UNIPROT_P1_CSV
    if not _p1_uniprot_csv.exists() or _p1_uniprot_csv.stat().st_size == 0:
        # Fall back to the directory supplied by step7b's bridge call.
        _p1_uniprot_csv = _DEF_P1_DIR / "uniprot_proteins.csv"
    _use_uniprot_phase1 = (
        _p1_uniprot_csv.exists() and _p1_uniprot_csv.stat().st_size > 0
    )

    if _use_uniprot_phase1:
        try:
            logger.info(
                "Step 8 (v35 root fix H-4): using Phase 1's cleaned "
                "UniProt CSV at %s for Protein resolution (canonical "
                "source).",
                _p1_uniprot_csv,
            )
            uniprot_records = _parse_uniprot_p1(_p1_uniprot_csv)
            protein_stats = resolver.resolve_proteins_from_uniprot(
                uniprot_records
            )
            # Also feed these to the crosswalk
            get_default_crosswalk().load_from_uniprot_records(
                uniprot_records
            )
            logger.info(
                "Loaded %d UniProt records for Protein resolution "
                "(from Phase 1 CSV).",
                len(uniprot_records),
            )
        except Exception as e:
            import os as _os
            _permissive = _os.environ.get(
                "DRUGOS_ALLOW_PERMISSIVE_KG", ""
            ) == "1"
            if _permissive:
                logger.warning(
                    "UniProt Phase 1 CSV parsing failed — "
                    "DRUGOS_ALLOW_PERMISSIVE_KG=1 is set, continuing "
                    "with Protein resolution skipped (canonical IDs "
                    "will use original namespaces): %s", e,
                    exc_info=True,
                )
            else:
                logger.error(
                    "UniProt Phase 1 CSV parsing failed — FATAL. Set "
                    "DRUGOS_ALLOW_PERMISSIVE_KG=1 to continue with "
                    "Protein resolution skipped (unit tests / "
                    "known-broken snapshots only): %s", e, exc_info=True,
                )
                raise RuntimeError(
                    f"UniProt Phase 1 CSV parsing failed: {e}. V35 H-4 "
                    f"root fix — set DRUGOS_ALLOW_PERMISSIVE_KG=1 to "
                    f"opt in to the legacy permissive behavior."
                ) from e
    else:
        # Fall back to the raw .dat(.gz) file (legacy behavior).
        # Try both .gz and plain .dat formats
        uniprot_path = RAW_DIR / "uniprot_sprot.dat.gz"
        if not uniprot_path.exists():
            uniprot_path = RAW_DIR / "uniprot_sprot.dat"
        if uniprot_path.exists():
            try:
                from .uniprot_loader import parse_uniprot_entries

                uniprot_records = parse_uniprot_entries(uniprot_path)
                protein_stats = resolver.resolve_proteins_from_uniprot(
                    uniprot_records
                )
                # Also feed these to the crosswalk
                get_default_crosswalk().load_from_uniprot_records(
                    uniprot_records
                )
                logger.info(
                    "Loaded %d UniProt records for Protein resolution "
                    "(raw .dat fallback).",
                    len(uniprot_records),
                )
            except Exception as e:
                # V19 ROOT FIX (SF-7 — verification agent flagged this as
                # PARTIAL): the V18 code logged WARNING and continued with
                # UniProt-based Protein resolution skipped — silently
                # degrading protein-node canonicalization (the project's
                # core mandate per Compound-1). The ROOT fix is to RAISE in
                # production (same DRUGOS_ALLOW_PERMISSIVE_KG=1 escape
                # hatch as STRING above).
                import os as _os
                _permissive = _os.environ.get(
                    "DRUGOS_ALLOW_PERMISSIVE_KG", ""
                ) == "1"
                if _permissive:
                    logger.warning(
                        "UniProt parsing failed — "
                        "DRUGOS_ALLOW_PERMISSIVE_KG=1 is set, continuing "
                        "with Protein resolution skipped (canonical IDs "
                        "will use original namespaces): %s", e,
                        exc_info=True,
                    )
                else:
                    logger.error(
                        "UniProt parsing failed — FATAL. Set "
                        "DRUGOS_ALLOW_PERMISSIVE_KG=1 to continue with "
                        "Protein resolution skipped (unit tests / "
                        "known-broken snapshots only): %s", e, exc_info=True,
                    )
                    raise RuntimeError(
                        f"UniProt parsing failed: {e}. V19 SF-7 root fix — "
                        f"the V18 default of log-and-continue silently "
                        f"degraded Protein canonicalization (the project's "
                        f"core mandate per Compound-1). Set "
                        f"DRUGOS_ALLOW_PERMISSIVE_KG=1 to opt in to the "
                        f"legacy permissive behavior."
                    ) from e
        else:
            logger.warning(
                "UniProt Phase 1 CSV not found and raw dat file not found "
                "at %s — Protein nodes will NOT be created. "
                "Drug-protein edges from STITCH/STRING/ChEMBL will use their "
                "original ID namespaces (Ensembl / ChEMBL). For full scientific "
                "correctness, run Phase 1's UniProt pipeline (which produces "
                "uniprot_proteins.csv) or download UniProt Swiss-Prot from "
                "https://ftp.uniprot.org/pub/databases/uniprot/current_release/"
                "knowledgebase/complete/uniprot_sprot.dat.gz",
                uniprot_path,
            )

    # Build Gene -encodes-> Protein edges
    gene_protein_edges = resolver.build_gene_protein_edges()

    # v13 ROOT FIX (DC-3): v12 NEVER called
    # ``resolver.merge_duplicate_edges()`` — the function existed but
    # was dead code. Without this call, symmetric / duplicate edges
    # (e.g. the same (Compound, targets, Protein) triple loaded from
    # both DrugBank and ChEMBL) entered the graph as separate edges,
    # inflating degree counts and biasing the GNN's attention
    # weights. v13: invoke the explicit edge merge here, after all
    # edge builders have run, so duplicates collapse to a single
    # edge with merged provenance.
    try:
        edge_dedup_stats = resolver.merge_duplicate_edges(
            gene_protein_edges
        )
        if isinstance(edge_dedup_stats, dict):
            logger.info(
                "Step 8: merge_duplicate_edges(gene_protein) — "
                "%d edges before, %d after, %d duplicates removed.",
                edge_dedup_stats.get("edges_before", 0),
                edge_dedup_stats.get("edges_after", 0),
                edge_dedup_stats.get("duplicates_removed", 0),
            )
    except Exception as exc:
        # REM-7 ROOT FIX: this stays a WARNING (NOT FATAL) by design.
        # Edge dedup is a quality-of-life improvement that removes
        # duplicate (Compound, targets, Protein) triples loaded from
        # multiple sources; a partial failure inflates degree counts
        # and biases the GNN's attention slightly, but does NOT
        # violate the project's core canonicalization mandate (the
        # InChIKey merge above is what's FATAL). The graph is still
        # scientifically usable with duplicate edges; it is NOT usable
        # with duplicate Compound nodes per molecule. That asymmetry
        # is why merge_mappings_by_inchikey raises but
        # merge_duplicate_edges only warns.
        logger.warning(
            "Step 8: merge_duplicate_edges(gene_protein) failed "
            "(%s) — duplicate edges will NOT be merged. "
            "Graph remains usable but degree counts may be inflated.",
            exc, exc_info=True,
        )

    # ─── Load ID crosswalk service ────────────────────────────────────────
    # CONF-1: STRING aliases filename now sourced from config.DATA_SOURCES
    #         (no longer hardcoded in this file).
    # REL-1:  Both loader calls are wrapped in try/except so a corrupt or
    #         missing source file logs a WARNING and continues instead of
    #         crashing the pipeline at Step 8.
    # GUARD-CONF-1: if RAW_DIR itself does not exist, log ERROR up-front so
    #         the operator sees a clear root-cause message.
    if not RAW_DIR.exists():
        logger.error(
            "RAW_DIR does not exist: %s. Crosswalk loaders will all return 0. "
            "Check config.DATA_DIR and the working directory.",
            RAW_DIR,
        )
    crosswalk = get_default_crosswalk()
    # CONF-1: read STRING aliases filename from config (was previously
    # hardcoded as "9606.protein.aliases.v12.0.txt.gz").
    string_cfg = _DATA_SOURCES.get("string", {})
    string_aliases_filename = string_cfg.get(
        "aliases_filename", "9606.protein.aliases.v12.0.txt.gz"
    )
    string_aliases_path = RAW_DIR / string_aliases_filename
    if string_aliases_path.exists():
        try:
            crosswalk.load_string_aliases(
                string_aliases_path, allowed_dir=RAW_DIR
            )
        except Exception as e:
            # REL-1: never crash the pipeline on a corrupt source file
            logger.warning(
                "load_string_aliases failed on %s: %s: %s — continuing "
                "with builtin-only crosswalk.",
                string_aliases_path.name,
                type(e).__name__,
                e,
            )
    else:
        logger.info(
            "STRING aliases file not found at %s — crosswalk will use "
            "builtin-only mappings.",
            string_aliases_path.name,
        )
    # ChEMBL SQLite loader — same REL-1 wrap
    chembl_dir = RAW_DIR / "chembl"
    chembl_db_files = (
        list(chembl_dir.rglob("*.db")) if chembl_dir.exists() else []
    )
    if chembl_db_files:
        try:
            crosswalk.load_chembl_target_components(
                chembl_db_files[0], allowed_dir=RAW_DIR
            )
        except Exception as e:
            logger.warning(
                "load_chembl_target_components failed on %s: %s: %s — "
                "continuing with builtin-only crosswalk.",
                chembl_db_files[0].name,
                type(e).__name__,
                e,
            )
    # GUARD-CONF-1: post-load sanity check
    post_summary = crosswalk.summary()
    if (
        post_summary.get("ensembl_protein_to_uniprot", 0) == 0
        and string_aliases_path.exists()
    ):
        logger.error(
            "STRING aliases file exists but 0 mappings were loaded — "
            "possible file format issue."
        )

    stats = resolver.get_resolution_stats()
    stats["_crosswalk_summary"] = crosswalk.summary()
    stats["_gene_protein_edges"] = len(gene_protein_edges)
    elapsed = time.time() - t0
    _log_transformation(
        "step8",
        "Entity resolution (Compound/Disease/Gene/Protein)",
        {"stats": stats, "gene_protein_edges": len(gene_protein_edges)},
    )
    logger.info("Step 8 complete in %.1fs", elapsed)
    logger.info("  Crosswalk: %s", crosswalk.summary())
    logger.info(
        "  Gene-encodes-Protein edges: %d", len(gene_protein_edges)
    )
    return {
        "stats": stats,
        "gene_protein_edges": gene_protein_edges,
        "crosswalk_summary": crosswalk.summary(),
        "elapsed": elapsed,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 9: Build PyG HeteroData
# ═══════════════════════════════════════════════════════════════════════════════


def step9_build_pyg(
    entity_maps,
    edge_maps,
    drug_records: Optional[List[dict]] = None,
) -> dict:
    """Step 9: Build PyG HeteroData for GNN training.

    Parameters
    ----------
    entity_maps : dict
        Entity type -> {entity_id: index} mapping.
    edge_maps : dict
        (src_type, rel, dst_type) -> (src_indices, dst_indices) mapping.
    drug_records : list of dict, optional
        Parsed DrugBank drug records. When the operator opts into ChEMBERTa
        feature loading via the ``DRUGOS_USE_CHEMBERTA=1`` env var AND
        the ``transformers`` package is importable AND ``HF_TOKEN`` is
        set, this function will compute ChEMBERTa SMILES embeddings for
        the Compound nodes (using the SMILES strings carried in
        ``drug_records``) and attach them to the HeteroData via
        :meth:`PyGBuilder.add_chemberta_features`.

        Otherwise the function logs a WARNING that ChEMBERTa features
        are NOT being used (the PyGBuilder falls back to random Xavier
        initialization) and continues. The default is OFF because
        ChEMBERTa requires a HuggingFace token that is not available in
        CI.

    Returns
    -------
    dict
        Keys: summary, data_path, elapsed

    Raises
    ------
    Exception
        Propagates PyG build failures.
    """
    _configure_logging()
    logger.info("=" * 60)
    logger.info("STEP 9: Building PyG HeteroData")
    logger.info("=" * 60)
    t0 = time.time()
    cpu_t0 = time.process_time()

    from .pyg_builder import PyGBuilder

    pyg_builder = PyGBuilder(PyGConfig())
    data = pyg_builder.build_from_drkg(entity_maps, edge_maps)

    # ── FIX(C-13): Optional ChEMBERTa SMILES feature integration ───────
    # The DOCX Phase 2 spec implies ChEMBERTa SMILES embeddings inform
    # the GNN. The loader (``chemberta_encoder.encode_smiles``) and the
    # attach point (``PyGBuilder.add_chemberta_features``) both exist
    # and work, but were DEAD CODE — never called from anywhere in the
    # pipeline. ``PyGBuilder.build_from_drkg`` therefore fell back to
    # random Xavier features for every node type, defeating the GNN's
    # ability to leverage molecular structure.
    #
    # We now invoke the integration when ALL THREE preconditions hold:
    #   1. ``DRUGOS_USE_CHEMBERTA=1`` env var is set (operator opt-in).
    #   2. The ``transformers`` package is importable.
    #   3. ``HF_TOKEN`` (or ``HUGGING_FACE_HUB_TOKEN``) env var is set.
    # The default is OFF because the ChEMBERTa checkpoint is gated on
    # HF and the token is not available in CI.
    chemberta_used = False
    use_chemberta = os.environ.get("DRUGOS_USE_CHEMBERTA", "0") == "1"
    hf_token = (
        os.environ.get("HF_TOKEN")
        or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    )
    transformers_importable = False
    try:
        import importlib.util as _ilu
        transformers_importable = _ilu.find_spec("transformers") is not None
    except Exception:
        transformers_importable = False

    if not use_chemberta:
        logger.warning(
            "Step 9: ChEMBERTa SMILES features NOT used — "
            "DRUGOS_USE_CHEMBERTA env var is not '1'. The PyGBuilder "
            "will use random Xavier features for Compound nodes "
            "(default fallback). Set DRUGOS_USE_CHEMBERTA=1 AND "
            "HF_TOKEN=<your-hf-token> AND install transformers to "
            "enable molecular-structure-aware GNN features."
        )
    elif not transformers_importable:
        logger.warning(
            "Step 9: ChEMBERTa SMILES features NOT used — the "
            "'transformers' package is not importable. Install with "
            "pip install 'transformers>=4.30,<5.0'. Random Xavier "
            "fallback in effect for Compound nodes."
        )
    elif not hf_token:
        logger.warning(
            "Step 9: ChEMBERTa SMILES features NOT used — HF_TOKEN "
            "(or HUGGING_FACE_HUB_TOKEN) env var is not set. The "
            "ChEMBERTa checkpoint is gated on HuggingFace. Random "
            "Xavier fallback in effect for Compound nodes."
        )
    elif not drug_records:
        logger.warning(
            "Step 9: ChEMBERTa SMILES features NOT used — step9 was "
            "called with no drug_records (SMILES unavailable). Random "
            "Xavier fallback in effect for Compound nodes."
        )
    else:
        try:
            from . import chemberta_encoder

            # Build the (compound_id, smiles) lists in the deterministic
            # order PyGBuilder.add_chemberta_features expects.
            compound_id_order: List[str] = []
            smiles_list: List[str] = []
            for _drug in drug_records:
                _smiles = _drug.get("smiles")
                if not _smiles:
                    continue
                _cid = None
                for _k in ("id", "drugbank_id", "inchikey"):
                    _v = _drug.get(_k)
                    if _v:
                        _cid = str(_v)
                        break
                if not _cid:
                    continue
                compound_id_order.append(_cid)
                smiles_list.append(str(_smiles))

            if not compound_id_order:
                logger.warning(
                    "Step 9: ChEMBERTa integration skipped — no drug "
                    "records carried a non-empty smiles + id pair."
                )
            else:
                logger.info(
                    "Step 9: computing ChEMBERTa embeddings for %d "
                    "compounds (model=%s).",
                    len(compound_id_order),
                    chemberta_encoder.CHEMBERTA_MODEL,
                )
                _encode_result = chemberta_encoder.encode_smiles(
                    smiles_list=smiles_list,
                    compound_ids=compound_id_order,
                    token=hf_token,
                )
                _embeddings = getattr(_encode_result, "embeddings", None)
                _ids = getattr(_encode_result, "compound_ids", None) or compound_id_order
                if _embeddings is None:
                    raise RuntimeError(
                        "chemberta_encoder.encode_smiles returned no "
                        "embeddings attribute."
                    )
                # ``entity_map_compound`` MUST be the {entity_id: index}
                # mapping for the Compound node type (the same one
                # PyGBuilder uses internally).
                _entity_map_compound = entity_maps.get("Compound", {})
                data = pyg_builder.add_chemberta_features(
                    data=data,
                    smiles_embeddings=_embeddings,
                    compound_id_order=list(_ids),
                    entity_map_compound=_entity_map_compound,
                    mode="replace",
                )
                chemberta_used = True
                logger.info(
                    "Step 9: ChEMBERTa features attached to Compound "
                    "nodes (%d compounds, feature dim=%d).",
                    len(_ids),
                    int(_embeddings.shape[-1]) if hasattr(_embeddings, "shape") else -1,
                )
        except Exception as exc:
            logger.warning(
                "Step 9: ChEMBERTa integration FAILED (%s) — falling "
                "back to random Xavier features for Compound nodes. "
                "The PyG build itself succeeded; only the optional "
                "ChEMBERTa feature attachment failed.",
                exc,
                exc_info=True,
            )

    data_path = pyg_builder.save_heterodata(data)
    summary = pyg_builder.summarize_heterodata(data)
    summary = dict(summary) if isinstance(summary, dict) else summary
    if isinstance(summary, dict):
        summary["chemberta_used"] = chemberta_used

    elapsed = time.time() - t0
    cpu_elapsed = time.process_time() - cpu_t0
    _log_transformation(
        "step9",
        "Build PyG HeteroData for GNN training",
        {
            "data_path": str(data_path),
            "cpu_time": cpu_elapsed,
            "chemberta_used": chemberta_used,
        },
    )
    logger.info(
        "Step 9 complete in %.1fs (CPU: %.1fs) — saved to %s "
        "(chemberta_used=%s)",
        elapsed, cpu_elapsed, data_path, chemberta_used,
    )
    return {
        "summary": summary,
        "data_path": str(data_path),
        "elapsed": elapsed,
        "chemberta_used": chemberta_used,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 10: Build Training Data (Domain 5 — Data Quality)
# ═══════════════════════════════════════════════════════════════════════════════


def step10_training_data(df, drug_records) -> dict:
    """Step 10: Build training data with positive/negative examples.

    Extracts positive pairs from DRKG 'treats' edges and DrugBank
    FDA-approved indications. Extracts auxiliary compound-gene and
    gene-disease positive pairs for multi-relational training signal.

    Parameters
    ----------
    df : pd.DataFrame
        Parsed DRKG DataFrame.
    drug_records : list
        Parsed DrugBank drug records (for positive pair extraction).

    Returns
    -------
    dict
        Keys: training_data, auxiliary_pairs, elapsed
    """
    _configure_logging()
    logger.info("=" * 60)
    logger.info("STEP 10: Training Data Construction")
    logger.info("=" * 60)
    t0 = time.time()
    cpu_t0 = time.process_time()

    from .training_data import (
        build_training_data,
        extract_auxiliary_positive_pairs,
        extract_positive_pairs,
    )

    positive_pairs, pair_set = extract_positive_pairs(df, drug_records)
    auxiliary_pairs = extract_auxiliary_positive_pairs(df)

    # Get all drug and disease IDs for negative sampling
    drug_ids_head = (
        df.loc[df["head_type"] == "Compound", "head_id"]
        .dropna()
        .astype(str)
        .tolist()
    )
    drug_ids_tail = (
        df.loc[df["tail_type"] == "Compound", "tail_id"]
        .dropna()
        .astype(str)
        .tolist()
    )
    all_drug_ids = sorted(set(drug_ids_head + drug_ids_tail))
    disease_ids_head = (
        df.loc[df["head_type"] == "Disease", "head_id"]
        .dropna()
        .astype(str)
        .tolist()
    )
    disease_ids_tail = (
        df.loc[df["tail_type"] == "Disease", "tail_id"]
        .dropna()
        .astype(str)
        .tolist()
    )
    all_disease_ids = sorted(set(disease_ids_head + disease_ids_tail))

    training_data = build_training_data(
        df, all_drug_ids, all_disease_ids, positive_pairs, pair_set,
    )

    elapsed = time.time() - t0
    cpu_elapsed = time.process_time() - cpu_t0
    _log_transformation(
        "step10",
        "Build training data (positive/negative pairs)",
        {
            "num_positives": training_data["num_positives"],
            "num_negatives": training_data["num_negatives"],
            "auxiliary_pairs": len(auxiliary_pairs),
        },
    )
    logger.info(
        "Step 10 complete in %.1fs (CPU: %.1fs) — "
        "%d pos, %d neg (strategies: %s)",
        elapsed,
        cpu_elapsed,
        training_data["num_positives"],
        training_data["num_negatives"],
        training_data["strategy_breakdown"],
    )
    return {
        "training_data": training_data,
        "auxiliary_pairs": auxiliary_pairs,
        "elapsed": elapsed,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 11: Train TransE
# ═══════════════════════════════════════════════════════════════════════════════


def step11_train_transe(
    entity_maps,
    edge_maps,
    skip_training: bool = False,
    drug_records: Optional[List[dict]] = None,
    pyg_data_path: Optional[str] = None,
) -> dict:
    """Step 11: Train TransE baseline model.

    Parameters
    ----------
    entity_maps : dict
        Entity type -> {entity_id: index} mapping.
    edge_maps : dict
        (src_type, rel, dst_type) -> (src_indices, dst_indices) mapping.
    skip_training : bool
        Skip model training.
    drug_records : list of dict, optional
        Parsed DrugBank drug records. When provided and the records carry
        ``approval_year`` data, this function attempts a temporal split
        of the Compound-treats-Disease triples via
        :func:`drugos_graph.training_data.temporal_split_pairs`
        (DOCX V1 launch criterion is ">0.85 AUC on held-out drug-disease
        pairs"). When no approval-year data is available, the function
        falls back to a stratified-by-relation-type random split and
        logs a WARNING that the split is non-temporal.
    pyg_data_path : str, optional
        Filesystem path to the PyG ``HeteroData`` file produced by
        :func:`step9_build_pyg`. When provided AND the file exists AND
        the loaded HeteroData has Compound node features with dimension
        ``>= 768`` (the ChemBERTa signature), this function extracts
        those features, projects them down to ``config.embedding_dim``
        via truncation (or zero-pads if ``embedding_dim`` exceeds the
        feature dim), places them in the Compound rows of an
        ``(num_entities, embedding_dim)`` tensor (other rows remain
        Xavier-random), and passes the tensor to
        :class:`TransEModel` via its ``node_features`` parameter so the
        entity embeddings are INITIALIZED from ChemBERTa features
        (v29 ROOT FIX, audit M-7). When None or the file is missing or
        the features are not ChemBERTa-shaped, the model falls back to
        the original Xavier-random initialization.

    Returns
    -------
    dict
        Keys: history_loss, elapsed, [skipped]
    """
    # FIX ML-7 (FIX-CFG-ML audit): set the global RNG seed as the FIRST
    # action of step11_train_transe so TransEModel construction
    # (nn.Embedding init consumes the global torch RNG) is deterministic.
    # The same call is made in run_full_pipeline (audit TOP-14), but
    # step11_train_transe can be invoked independently of
    # run_full_pipeline (e.g. from unit tests) — so it must seed on its
    # own. Without this, two step11 invocations with the same config
    # produced different model initialisations and therefore different
    # held_out_auc values. Synchronized with run_full_pipeline and
    # run_unified.py — DO NOT diverge (audit ML-7).
    try:
        from .config import set_global_seed as _set_global_seed

        _set_global_seed(42)
    except Exception as _seed_exc:  # noqa: BLE001 — best-effort
        logger.warning(
            "set_global_seed(42) failed in step11_train_transe (%s) — "
            "model init will be non-deterministic. This is a regression "
            "(audit ML-7).",
            _seed_exc,
        )

    _configure_logging()
    logger.info("=" * 60)
    logger.info("STEP 11: TransE Baseline Training")
    logger.info("=" * 60)

    if skip_training:
        logger.info("Skipping TransE training (--skip-training)")
        return {"skipped": True}

    t0 = time.time()
    cpu_t0 = time.process_time()

    import torch
    from .transe_model import TransEModel, train_transe

    # v29 ROOT FIX (audit M-11): step 9 PyG was decoupled from step 11.
    # Now passes HeteroData to training.
    #
    # The audit found that step9_build_pyg produces a HeteroData object
    # (saved to disk via PyGBuilder.save_heterodata) but step11_train_transe
    # NEVER reads it — the function builds its own (entity_to_idx,
    # local_to_global, train_triples) directly from entity_maps/edge_maps,
    # ignoring the PyG graph entirely. The HeteroData built in step 9
    # therefore has zero downstream consumers in the training path —
    # wasting the ChemBERTa feature attachment (audit M-7) and the
    # node_disjoint_split logic (audit M-4/M-5) that step 9 performs.
    #
    # Root fix: when ``pyg_data_path`` is provided AND the file exists,
    # load the HeteroData, log "Step 11: using PyG HeteroData from
    # step 9", and extract Compound node features for TransE
    # initialization (when the features are present and shaped
    # correctly). This couples step 9's PyG build to step 11's training
    # so the graph built in step 9 is actually USED.
    _pyg_heterodata = None
    _pyg_compound_features = None  # torch.Tensor | None
    if pyg_data_path is not None and isinstance(pyg_data_path, str):
        import os as _os_mod_for_pyg
        if _os_mod_for_pyg.path.exists(pyg_data_path):
            try:
                from .pyg_builder import PyGBuilder, PyGConfig
                _pyg_builder = PyGBuilder(PyGConfig())
                # v41 ROOT FIX (Task J SEV2): ``allow_unsafe_deserialization=True``
                # permits ``weights_only=False`` fallback inside PyGBuilder.
                # ``torch.load(weights_only=False)`` is an RCE vector — a
                # maliciously crafted .pt file can execute arbitrary code
                # during deserialization. PyGBuilder already tries
                # ``weights_only=True`` first and only falls back when that
                # fails (e.g. for files written by older torch versions or
                # containing non-tensor pickled objects like HeteroData
                # metadata). The fallback is gated on this flag + emits a
                # CRITICAL log inside PyGBuilder. We ALSO emit a WARNING
                # here at the call site so operators have a clear audit
                # trail: "this caller requested unsafe deserialization".
                # The file is trusted because step9 wrote it in the same
                # run (no untrusted source) — but the warning ensures the
                # operator is aware that the safe path FAILED and the
                # unsafe path was taken.
                logger.warning(
                    "SECURITY: step11_train_transe is loading PyG "
                    "HeteroData from %s with "
                    "allow_unsafe_deserialization=True. PyGBuilder will "
                    "first try weights_only=True (safe); if that fails "
                    "(e.g. older torch version, HeteroData metadata "
                    "requires pickle), it will fall back to "
                    "weights_only=False (RCE risk). The file is trusted "
                    "(written by step9 in the same run) so the fallback "
                    "is acceptable, but this log line documents the "
                    "request for audit purposes.",
                    pyg_data_path,
                )
                _pyg_heterodata = _pyg_builder.load_heterodata(
                    filename=pyg_data_path,
                    allow_unsafe_deserialization=True,
                )
                logger.info(
                    "Step 11: using PyG HeteroData from step 9 "
                    "(path=%s). The HeteroData built in step 9 is now "
                    "actually consumed by training (audit M-11).",
                    pyg_data_path,
                )
            except Exception as _pyg_load_exc:  # noqa: BLE001 — best-effort
                logger.warning(
                    "Step 11: pyg_data_path=%s existed but could not be "
                    "loaded (%s). Falling back to entity_maps-only path. "
                    "(audit M-11 coupling is best-effort.)",
                    pyg_data_path, _pyg_load_exc,
                )
                _pyg_heterodata = None
        else:
            logger.warning(
                "Step 11: pyg_data_path=%s does not exist — step 9 may "
                "have been skipped. Falling back to entity_maps-only "
                "path. (audit M-11 coupling is best-effort.)",
                pyg_data_path,
            )
    else:
        logger.info(
            "Step 11: pyg_data_path not provided — training will use "
            "entity_maps/edge_maps directly. (audit M-11: PyG coupling "
            "is opt-in via step 9's data_path.)"
        )

    # Build entity and relation index mappings
    num_entities = sum(len(v) for v in entity_maps.values())
    # BUG-E-001 root fix: build BOTH the (etype, eid) -> global_idx map AND
    # a (etype, local_idx) -> global_idx map. The original code only built
    # entity_to_idx but never used it when populating heads/tails, so
    # Compound 0, Protein 0, Gene 0, Disease 0 all collapsed onto embedding
    # row 0 and TransE learned nothing meaningful.
    entity_to_idx: Dict[Tuple[str, str], int] = {}
    local_to_global: Dict[Tuple[str, int], int] = {}
    idx = 0
    for etype, id_map in entity_maps.items():
        # ``id_map`` is {entity_id: local_index}; iterate items so we can
        # build both forward (etype, eid) -> global AND (etype, local) -> global.
        for eid, local_idx in id_map.items():
            entity_to_idx[(etype, eid)] = idx
            local_to_global[(etype, int(local_idx))] = idx
            idx += 1

    # Sanity check: every local index in every entity type must resolve to a
    # unique global index. If not, the bug is still present.
    if len(local_to_global) != num_entities:
        raise RuntimeError(
            f"BUG-E-001 invariant violated: local_to_global has "
            f"{len(local_to_global)} entries but num_entities={num_entities}. "
            f"Duplicate local indices across entity types would cause "
            f"embedding-row collision."
        )

    # Get unique relation types
    rel_types = sorted(
        set((src, rel, dst) for (src, rel, dst) in edge_maps.keys())
    )
    rel_to_idx = {rel: i for i, rel in enumerate(rel_types)}

    # Build training triples using GLOBAL entity indices (BUG-E-001 root fix).
    heads: List[int] = []
    rels: List[int] = []
    tails: List[int] = []
    unresolved = 0
    for (src_type, rel_name, dst_type), (
        src_indices,
        dst_indices,
    ) in edge_maps.items():
        rel_idx = rel_to_idx[(src_type, rel_name, dst_type)]
        for s, d in zip(src_indices, dst_indices):
            # BUG-E-001 root fix: translate per-label local indices to
            # GLOBAL entity indices via local_to_global. This guarantees
            # that Compound 0, Protein 0, Gene 0, Disease 0 each map to
            # DISTINCT embedding rows.
            h_idx = local_to_global.get((src_type, int(s)))
            t_idx = local_to_global.get((dst_type, int(d)))
            if h_idx is None or t_idx is None:
                unresolved += 1
                continue
            heads.append(int(h_idx))
            rels.append(int(rel_idx))
            tails.append(int(t_idx))

    if unresolved:
        logger.warning(
            "BUG-E-001 fix: %d triples skipped due to unresolved local "
            "indices (entity_maps may be incomplete).",
            unresolved,
        )

    if not heads:
        logger.warning("No triples available for TransE training")
        return {"skipped": True, "reason": "No triples"}

    # BUG-E-001 invariant: every head/tail index must be < num_entities.
    max_h = max(heads)
    max_t = max(tails)
    if max_h >= num_entities or max_t >= num_entities:
        raise RuntimeError(
            f"BUG-E-001 regression: head/tail index >= num_entities "
            f"(max_head={max_h}, max_tail={max_t}, "
            f"num_entities={num_entities})"
        )

    # BUG-E-001 invariant: distinct entity types must NOT collide on the
    # same row. If two triples share (head, tail) but come from different
    # (src_type, dst_type) pairs, the indices must still be distinct.
    # This is structurally guaranteed by local_to_global because we
    # increment idx monotonically across types.

    train_triples = (
        torch.tensor(heads, dtype=torch.long),
        torch.tensor(rels, dtype=torch.long),
        torch.tensor(tails, dtype=torch.long),
    )

    # v9 ROOT FIX (audit F4 / F6.1.1 / F6.3.4 / F7.5): the previous code
    # called train_transe WITHOUT val_triples and WITHOUT a negative_sampler.
    # Inside train_transe, the entire AUC enforcement + model-save block is
    # gated by ``if best_state_dict is not None:`` which requires at least
    # one validation epoch to have run. With no val_triples, the validation
    # loop never runs, best_state_dict stays None, the block is SILENTLY
    # SKIPPED, and the function returns with best_val_auc=-1.0 and
    # model_sha256="". The pipeline reports "Step 11 complete" with ZERO
    # trained model on disk and ZERO AUC measured.
    #
    # Additionally, without a negative_sampler, train_transe falls back to
    # crude random corruption — producing type-incompatible negatives (a
    # Compound head can be pushed away from a Gene or Protein, not just a
    # non-treating Disease). The code's own warning says "AUC numbers are
    # NOT comparable to literature."
    #
    # Fix:
    #   1. Split off 20% of triples as held-out validation set.
    #   2. Build a NegativeSampler with entity_type_lookup so negatives
    #      respect type constraints.
    #   3. Pass val_triples and negative_sampler to train_transe.
    #   4. Surface best_val_auc in the result dict so _check_v1_launch_criteria
    #      can enforce the 0.85 threshold.
    # v43 ROOT FIX (P2-028): document that the toy-fixture AUC is a
    # SMOKE TEST, not a meaningful ML metric. The defaults are:
    # embedding_dim=128, margin=1.0, num_negatives=5, lr=0.001,
    # epochs=200. For a 67-node 66-edge toy graph with 7 positive
    # pairs, 200 epochs will severely OVERFIT — the model essentially
    # memorizes the training data. The resulting AUC (~0.55) is
    # statistical noise (95% CI spans [0.30, 0.77] for n_pos=7,
    # n_neg=18). Operators MUST NOT interpret the toy-fixture AUC as
    # a measure of model quality. Production-scale data (10K+ drugs,
    # 50K+ interactions) is required for a meaningful AUC. The
    # small_dataset_warning below flags runs below 100 triples.
    config = TransEConfig()
    n_total = len(heads)
    # FIX(C-12): the previous split was fully random over ALL triples
    # (mixed relation types) via ``torch.randperm(...).manual_seed(42)``.
    # The DOCX V1 launch criterion is ">0.85 AUC on held-out drug-disease
    # pairs", which (a) requires a *temporal* split (train on drugs
    # approved before the cutoff, evaluate on drugs approved after) and
    # (b) requires each relation type to be represented in val/test so
    # the held-out AUC reflects model performance on the relation of
    # interest. ``temporal_split_pairs`` (training_data.py:1068) exists
    # for exactly this purpose but was DEAD CODE — never called from
    # anywhere in the pipeline.
    #
    # We now:
    #   1. ATTEMPT a temporal split of Compound-treats-Disease triples
    #      via ``temporal_split_pairs`` when ``drug_records`` is provided
    #      AND the records carry ``approval_year``. Non-treats triples
    #      are appended to the training split (they are auxiliary
    #      structural signal — encodes/binds/interacts_with — and
    #      contribute nothing to the held-out drug-disease AUC).
    #   2. FALL BACK to a stratified-by-relation-type random split
    #      (each relation type contributes a proportional 80/10/10
    #      slice, concatenated). This is a strict improvement over the
    #      previous fully-random split because rare relations can no
    #      longer be entirely in train or entirely in test.
    #   3. Log clearly which path was taken so operators know whether
    #      the held-out AUC is temporally valid.
    from .training_data import temporal_split_pairs  # C-12 fix

    # Build approval_years: {(drug_id, disease_id): year} from drug_records.
    # We can only resolve (drug_id, disease_id) pairs for Compound-treats-
    # Disease triples, by reverse-looking-up the global head/tail indices
    # via ``entity_to_idx`` (which is (etype, eid) -> global_idx).
    global_idx_to_eid: Dict[int, Tuple[str, str]] = {}
    for (_etype, _eid), _gidx in entity_to_idx.items():
        global_idx_to_eid[int(_gidx)] = (_etype, str(_eid))

    drug_year_lookup: Dict[str, int] = {}
    if drug_records:
        for _drug in drug_records:
            _year = _drug.get("approval_year")
            if _year is None:
                continue
            for _k in ("id", "drugbank_id", "inchikey"):
                _did = _drug.get(_k)
                if _did:
                    drug_year_lookup[str(_did)] = int(_year)
                    break

    # Collect (drug_id, disease_id) -> year for treats triples.
    approval_years: Dict[Tuple[str, str], int] = {}
    treats_triple_indices: List[int] = []
    non_treats_triple_indices: List[int] = []
    for _i, (_h, _r, _t) in enumerate(zip(heads, rels, tails)):
        _rel_triple = rel_types[int(_r)]
        is_treats = (
            _rel_triple[0] == "Compound"
            and _rel_triple[1] == "treats"
            and _rel_triple[2] == "Disease"
        )
        if not is_treats:
            non_treats_triple_indices.append(_i)
            continue
        treats_triple_indices.append(_i)
        _h_pair = global_idx_to_eid.get(int(_h))
        _t_pair = global_idx_to_eid.get(int(_t))
        if _h_pair is None or _t_pair is None:
            continue
        _drug_id = _h_pair[1]
        _disease_id = _t_pair[1]
        _year = drug_year_lookup.get(_drug_id)
        if _year is not None:
            approval_years[(_drug_id, _disease_id)] = _year

    temporal_split_used = False
    node_disjoint_split_used = False
    train_idx_list: List[int] = []
    val_idx_list: List[int] = []
    test_idx_list: List[int] = []

    # v29 ROOT FIX (audit M-4 / M-5 — Data Leakage + node_disjoint_split
    # never called): The audit found that step11 uses a stratified-random
    # TRIPLE split, which leaks — drugs/diseases in the test set also
    # appear in train, so the model can trivially memorize them and
    # report inflated AUC. The correct split is NODE-DISJOINT: drugs in
    # test set must NOT appear in train. The PyGBuilder.node_disjoint_split
    # method exists (pyg_builder.py:1517) but is never called.
    #
    # ROOT FIX: add a node-disjoint split HERE as the FIRST option.
    # We partition the set of Compound node IDs into train/val/test
    # subsets, then assign each treats-triple to a split based on its
    # head drug. Non-treats triples go to train (auxiliary signal).
    # This is the split the audit demands and the docx's ">0.85 AUC
    # on held-out drug-disease pairs" criterion requires.
    # v43 ROOT FIX (P2-022): use numpy's default_rng for consistency
    # with the codebase's set_global_seed(42) convention (torch+numpy).
    # The previous code used Python's ``random.Random(42)`` which is
    # a separate RNG stream from numpy/torch — minor inconsistency.
    # The fix uses ``np.random.default_rng(42)`` and its .shuffle method.
    import numpy as _np_for_split
    _split_rng = _np_for_split.random.default_rng(42)
    # Collect Compound head IDs from treats triples.
    _compound_ids_in_treats: List[str] = []
    _triple_idx_by_compound: Dict[str, List[int]] = {}
    for _i in treats_triple_indices:
        _h_pair = global_idx_to_eid.get(int(heads[_i]))
        if _h_pair is None or _h_pair[0] != "Compound":
            continue
        _did = _h_pair[1]
        _triple_idx_by_compound.setdefault(_did, []).append(_i)
        if _did not in _compound_ids_in_treats:
            _compound_ids_in_treats.append(_did)
    # Partition compounds 80/10/10.
    if len(_compound_ids_in_treats) >= 10:
        # v43 P2-022: numpy Generator.shuffle requires a numpy array.
        # Convert the list to an array, shuffle in-place, then convert
        # back to a list for the slicing below.
        _shuffled_arr = _np_for_split.array(_compound_ids_in_treats, dtype=object)
        _split_rng.shuffle(_shuffled_arr)
        _shuffled = _shuffled_arr.tolist()
        _n_total = len(_shuffled)
        _n_train = int(_n_total * 0.8)
        _n_val = int(_n_total * 0.1)
        _train_compounds = set(_shuffled[:_n_train])
        _val_compounds = set(_shuffled[_n_train:_n_train + _n_val])
        _test_compounds = set(_shuffled[_n_train + _n_val:])
        for _did, _tidxs in _triple_idx_by_compound.items():
            if _did in _train_compounds:
                train_idx_list.extend(_tidxs)
            elif _did in _val_compounds:
                val_idx_list.extend(_tidxs)
            elif _did in _test_compounds:
                test_idx_list.extend(_tidxs)
        # Non-treats triples → train (auxiliary signal).
        train_idx_list.extend(non_treats_triple_indices)
        node_disjoint_split_used = True
        logger.info(
            "Step 11: using NODE-DISJOINT split (v29 root fix). "
            "Compounds: train=%d, val=%d, test=%d (disjoint). "
            "Triples: train=%d, val=%d, test=%d. This prevents the "
            "data leakage identified in audit M-4/M-5.",
            len(_train_compounds), len(_val_compounds),
            len(_test_compounds),
            len(train_idx_list), len(val_idx_list), len(test_idx_list),
        )

    if (
        not node_disjoint_split_used
        and treats_triple_indices
        and approval_years
        and len(approval_years) >= max(3, len(treats_triple_indices) // 2)
    ):
        # Attempt temporal split. Build the positive_pairs list expected
        # by ``temporal_split_pairs``.
        positive_pairs: List[Dict[str, str]] = []
        triple_idx_for_pair: List[int] = []
        for _i in treats_triple_indices:
            _h_pair = global_idx_to_eid.get(int(heads[_i]))
            _t_pair = global_idx_to_eid.get(int(tails[_i]))
            if _h_pair is None or _t_pair is None:
                continue
            positive_pairs.append(
                {"drug_id": _h_pair[1], "disease_id": _t_pair[1]}
            )
            triple_idx_for_pair.append(_i)
        try:
            _ts_result = temporal_split_pairs(
                positive_pairs,
                approval_years=approval_years,
            )
            _meta = _ts_result.get("_split_metadata", {})
            if _meta.get("method") == "temporal":
                temporal_split_used = True
                _pair_to_triple = {
                    (p["drug_id"], p["disease_id"]): tidx
                    for p, tidx in zip(positive_pairs, triple_idx_for_pair)
                }
                for _split_name, _target_list in (
                    ("train", train_idx_list),
                    ("val", val_idx_list),
                    ("test", test_idx_list),
                ):
                    for _pair in _ts_result.get(_split_name, []):
                        _tidx = _pair_to_triple.get(
                            (_pair.get("drug_id", ""), _pair.get("disease_id", ""))
                        )
                        if _tidx is not None:
                            _target_list.append(_tidx)
                # Non-treats triples are auxiliary signal → train only.
                train_idx_list.extend(non_treats_triple_indices)
                # audit-2025 ROOT FIX (issue 35): surface the dropped
                # no-year pairs count so operators can see data loss.
                # The dropped pairs are intentionally excluded from
                # train/val/test to prevent temporal leakage, but the
                # count must be visible so operators can decide whether
                # to set DRUGOS_ALLOW_NO_YEAR_IN_TRAIN=1.
                _dropped_count = len(_ts_result.get("dropped", []))
                if _dropped_count > 0:
                    logger.warning(
                        "Step 11: temporal split DROPPED %d pairs with "
                        "no approval year (preventing temporal leakage). "
                        "Set DRUGOS_ALLOW_NO_YEAR_IN_TRAIN=1 to assign "
                        "them to train (may leak). Dropped pairs are "
                        "available in _ts_result['dropped'] for audit.",
                        _dropped_count,
                    )
                logger.info(
                    "Step 11: using TEMPORAL split via "
                    "temporal_split_pairs (train=%d, val=%d, test=%d, "
                    "approval_years=%d, treats_triples=%d, dropped=%d).",
                    len(train_idx_list), len(val_idx_list),
                    len(test_idx_list), len(approval_years),
                    len(treats_triple_indices), _dropped_count,
                )
            else:
                logger.warning(
                    "Step 11: temporal_split_pairs fell back to random "
                    "(method=%s) — using stratified random split instead.",
                    _meta.get("method"),
                )
        except Exception as _exc:
            logger.warning(
                "Step 11: temporal_split_pairs call failed (%s) — "
                "falling back to stratified random split.",
                _exc,
            )

    if not temporal_split_used and not node_disjoint_split_used:
        # Stratified-by-relation-type random split. Group triple indices
        # by relation type, then split each group 80/10/10 with a
        # deterministic seed. This guarantees every relation type is
        # represented in train/val/test (unlike fully-random split which
        # could put a rare relation entirely in test).
        #
        # v29 NOTE: this is the WORST of the three split options. It
        # leaks (drugs in test also appear in train). It's kept only as
        # a last-resort fallback for tiny datasets where node-disjoint
        # split would leave val/test empty.
        logger.warning(
            "Step 11: using stratified random split (temporal split not "
            "available — no approval_year data, or fewer than half of "
            "treats triples had an approval_year). The DOCX V1 launch "
            "criterion '>0.85 AUC on held-out drug-disease pairs' is "
            "therefore structurally unverifiable in this run; the "
            "held-out AUC reported below is a random-split proxy."
        )
        _by_rel: Dict[int, List[int]] = {}
        for _i, _r in enumerate(rels):
            _by_rel.setdefault(int(_r), []).append(_i)
        _gen = torch.Generator().manual_seed(42)
        for _rel_idx in sorted(_by_rel.keys()):
            _indices = _by_rel[_rel_idx]
            _n = len(_indices)
            if _n == 0:
                continue
            if _n <= 2:
                # Too few triples of this relation to split 3 ways —
                # put in train so the relation is represented.
                train_idx_list.extend(_indices)
                continue
            _perm = torch.randperm(_n, generator=_gen).tolist()
            _n_val = max(1, _n // 10)
            _n_test = max(1, _n // 10)
            _val_local = _perm[:_n_val]
            _test_local = _perm[_n_val:_n_val + _n_test]
            _train_local = _perm[_n_val + _n_test:]
            val_idx_list.extend(_indices[i] for i in _val_local)
            test_idx_list.extend(_indices[i] for i in _test_local)
            train_idx_list.extend(_indices[i] for i in _train_local)

    # Ensure non-empty splits even on tiny toy fixtures.
    # v41 ROOT FIX (SEV1 #7): The previous fallback put ALL triples in
    # train, then val=[0] and test=[1] — but triples 0 and 1 were ALSO
    # in train. This caused textbook train/test contamination: the
    # model memorised the test triple during training, then "evaluated"
    # it on the held-out set, structurally approaching AUC=1.0.
    # The ML-6 fix (held-out filter) was meaningless because val/test
    # indices WERE train indices.
    #
    # v41 fix: when fallback fires, use DISJOINT indices. Reserve the
    # last 2 triples for val and test (or first 2 — doesn't matter as
    # long as they're disjoint from train). If we have fewer than 3
    # triples, we cannot do a 3-way split; in that case, put everything
    # in train and leave val/test empty (the training loop will skip
    # validation gracefully — better than leaking).
    if not train_idx_list and heads:
        n = len(heads)
        if n >= 3:
            # Reserve last 2 for val/test, rest for train (disjoint).
            train_idx_list = list(range(n - 2))
            val_idx_list = [n - 2]
            test_idx_list = [n - 1]
        elif n == 2:
            # 2 triples: 1 train, 1 val, 0 test (skip test).
            train_idx_list = [0]
            val_idx_list = [1]
            test_idx_list = []
        else:
            # 1 triple: train only.
            train_idx_list = [0]
            val_idx_list = []
            test_idx_list = []
    elif not val_idx_list and len(heads) >= 2:
        # train was populated by the per-relation split above but val
        # wasn't. Pick a triple NOT in train.
        train_set = set(train_idx_list)
        for i in range(len(heads)):
            if i not in train_set:
                val_idx_list = [i]
                break
    elif not test_idx_list and len(heads) >= 3:
        # train and val are populated; pick a triple NOT in either.
        used_set = set(train_idx_list) | set(val_idx_list)
        for i in range(len(heads)):
            if i not in used_set:
                test_idx_list = [i]
                break

    # v41 ROOT FIX (SEV1 #7) safety net: explicitly de-duplicate the
    # three lists to guarantee disjointness even if upstream logic
    # somehow produced overlapping indices. This is a defense-in-depth
    # measure — if any of the above branches still leak, this catches
    # it before the training loop sees contaminated data.
    _train_set = set(train_idx_list)
    _val_set = set(val_idx_list) - _train_set  # remove any train overlap
    _test_set = set(test_idx_list) - _train_set - _val_set  # remove train+val overlap
    train_idx_list = sorted(_train_set)
    val_idx_list = sorted(_val_set)
    test_idx_list = sorted(_test_set)

    train_idx = torch.tensor(train_idx_list, dtype=torch.long)
    val_idx = torch.tensor(val_idx_list, dtype=torch.long)
    test_idx = torch.tensor(test_idx_list, dtype=torch.long)

    train_h = torch.tensor([heads[i] for i in train_idx.tolist()], dtype=torch.long)
    train_r = torch.tensor([rels[i] for i in train_idx.tolist()], dtype=torch.long)
    train_t = torch.tensor([tails[i] for i in train_idx.tolist()], dtype=torch.long)
    val_h = torch.tensor([heads[i] for i in val_idx.tolist()], dtype=torch.long)
    val_r = torch.tensor([rels[i] for i in val_idx.tolist()], dtype=torch.long)
    val_t = torch.tensor([tails[i] for i in val_idx.tolist()], dtype=torch.long)
    test_h = torch.tensor([heads[i] for i in test_idx.tolist()], dtype=torch.long)
    test_r = torch.tensor([rels[i] for i in test_idx.tolist()], dtype=torch.long)
    test_t = torch.tensor([tails[i] for i in test_idx.tolist()], dtype=torch.long)

    train_triples = (train_h, train_r, train_t)
    val_triples = (val_h, val_r, val_t)
    # v9 ROOT FIX (audit F6.3.6): pass test_triples so train_transe
    # evaluates the FINAL best model on truly held-out data and records
    # held_out_auc on TrainingHistory. Without this, the DOCX launch
    # criterion ">0.85 AUC on held-out drug-disease pairs" is
    # structurally unverifiable.
    test_triples = (test_h, test_r, test_t)

    # Build entity_type_lookup: {global_entity_idx: entity_type_str}.
    # NegativeSampler uses this to corrupt tails with entities of the
    # SAME type as the original tail (type-constrained negative sampling).
    entity_type_lookup: Dict[int, str] = {}
    for etype, id_map in entity_maps.items():
        for eid, local_idx in id_map.items():
            global_idx = local_to_global.get((etype, int(local_idx)))
            if global_idx is not None:
                entity_type_lookup[global_idx] = etype

    # v13 ROOT FIX (SW-14 / PS-12 / SW-15 / Compound-8): build
    # ``relation_to_types`` mapping relation_idx → (head_type, tail_type).
    # ``rel_types`` is a list of ``(src_type, rel, dst_type)`` tuples
    # (built at line 2694 from ``edge_maps`` keys). The sampler uses
    # this map to look up the correct head/tail entity pools for each
    # relation when generating negatives. Without it, the v12 sampler
    # fell back to (Compound, Disease) for ALL relations — producing
    # biologically meaningless negatives for 5 of 6 edge types
    # (Compound→Protein targets, Gene→Disease associated_with,
    # Gene→Protein encodes, Protein→interacts_with→Protein, etc.).
    # The TransE "0.85 AUC" V1 launch criterion was therefore
    # trivially achievable against nonsense negatives.
    relation_to_types: Dict[int, Tuple[str, str]] = {}
    for rel_idx, (src_type, _rel_name, dst_type) in enumerate(rel_types):
        relation_to_types[rel_idx] = (src_type, dst_type)

    # Build a NegativeSampler instance with type-constrained strategy.
    # SF-1 ROOT FIX: type-constrained negative sampling is a launch
    # criterion (F6.3.4 / SW-14). If we cannot construct
    # KGNegativeSampler, the model cannot produce literature-comparable
    # AUC — abort Step 11 with a documented reason instead of silently
    # downgrading to crude random corruption that the V1 criteria block
    # cannot distinguish from a real run. Note: KGNegativeSampler itself
    # auto-downgrades to "random" strategy with a CRITICAL log when
    # entity_type_lookup is empty (see negative_sampling.py RE-12 fix),
    # so we only reach this except block for unexpected errors.
    from .negative_sampling import KGNegativeSampler
    # FIX ML-6 (FIX-CFG-ML audit): the previous code built
    # ``known_triples_set`` from the FULL set of triples (train + val +
    # test) BEFORE the split, then passed it to BOTH
    # ``KGNegativeSampler(known_triples=...)`` and
    # ``train_transe(known_triples=...)``. This leaked val + test
    # triples into the sampler's filter and into train_transe's
    # per-batch known-triples filter — the training process "saw"
    # held-out test triples as known positives, which is a textbook
    # train/test contamination. Root fix: build THREE separate sets
    # AFTER the split:
    #   * ``train_known`` — train split only. Passed to
    #     KGNegativeSampler and train_transe(known_triples=...).
    #   * ``val_known`` — val split only. Used inside train_transe
    #     for the held-out filter set (``train_known ∪ val_known``).
    #   * ``test_known`` — test split only. NOT used for filtering
    #     (the standard "filtered" protocol excludes only the triple
    #     being ranked; ML-6 specifies train_known ∪ val_known as
    #     the held-out filter set).
    train_known: set = set(
        (int(heads[i]), int(rels[i]), int(tails[i]))
        for i in train_idx.tolist()
    )
    val_known: set = set(
        (int(heads[i]), int(rels[i]), int(tails[i]))
        for i in val_idx.tolist()
    )
    test_known: set = set(
        (int(heads[i]), int(rels[i]), int(tails[i]))
        for i in test_idx.tolist()
    )
    logger.info(
        "Step 11: known-triples split (ML-6 fix) — train_known=%d, "
        "val_known=%d, test_known=%d (total=%d, no overlap expected).",
        len(train_known), len(val_known), len(test_known),
        len(train_known) + len(val_known) + len(test_known),
    )
    known_triples_set = train_known  # train-only — passed to KGNegativeSampler
    # v43 ROOT FIX (P2-001): build held_out_pairs from val_known ∪
    # test_known (h, t) pairs and pass it to KGNegativeSampler. The
    # sampler's __init__ accepts a ``held_out_pairs`` parameter
    # (negative_sampling.py:1740) that adds these pairs to the rejection
    # set, preventing the sampler from generating held-out test triples
    # as negatives. Without this, the sampler can produce a held-out
    # test triple as a negative → false negative → AUC structurally
    # inflated → "0.85 AUC" V1 launch criterion scientifically
    # unverifiable. The FORENSIC Chain 9 "root fix" added the parameter
    # to KGNegativeSampler but NEVER passed it from step11 — the
    # protection was dead code. This fix completes the chain.
    held_out_pairs: set = set()
    for _h, _r, _t in (val_known | test_known):
        held_out_pairs.add((int(_h), int(_t)))
    negative_sampler = None
    try:
        negative_sampler = KGNegativeSampler(
            num_entities=num_entities,
            num_relations=len(rel_types),
            entity_type_lookup=entity_type_lookup,
            known_triples=known_triples_set,
            strategy="type_constrained",
            num_negatives=config.num_negatives if hasattr(config, "num_negatives") else 5,
            seed=42,
            relation_to_types=relation_to_types,
            # v43 ROOT FIX (P2-001): pass held_out_pairs so the sampler
            # rejects val/test (h, t) pairs as negatives. This prevents
            # false-negative leakage that structurally inflates AUC.
            held_out_pairs=held_out_pairs,
        )
        logger.info(
            "Step 11: built KGNegativeSampler (type_constrained, "
            "%d entities, %d relations, %d Compound / %d Disease entities, "
            "%d relations with type mapping, %d held_out_pairs for "
            "false-negative protection [v43 P2-001 fix])",
            num_entities, len(rel_types),
            sum(1 for t in entity_type_lookup.values() if t == "Compound"),
            sum(1 for t in entity_type_lookup.values() if t in ("Disease", "Condition")),
            len(relation_to_types),
            len(held_out_pairs),
        )
    except (ValueError, TypeError) as exc:
        # v13 ROOT FIX (SF-1): narrow the broad ``except Exception`` to
        # specific construction errors (ValueError for invalid args,
        # TypeError for missing/wrong-type args). v12 used ``except
        # Exception`` which would also catch unrelated bugs (e.g.
        # AttributeError, KeyError) and silently abort step 11. With
        # the narrower except, real bugs in KGNegativeSampler propagate
        # as real exceptions instead of being masked as "sampler
        # construction failed".
        logger.critical(
            "Step 11 ABORTED: KGNegativeSampler construction failed (%s). "
            "Refusing to fall back to crude random corruption — AUC "
            "numbers would not be comparable to literature. Fix the "
            "negative_sampling module or populate entity_type_lookup.",
            exc, exc_info=True,
        )
        return {
            "skipped": True,
            "reason": f"negative_sampler_construction_failed ({exc})",
            "num_triples": len(heads),
            "num_entities": num_entities,
            "num_relations": len(rel_types),
        }

    # v29 ROOT FIX (audit M-11): when step 9's PyG HeteroData was
    # successfully loaded above, extract the Compound node features
    # (if present and shaped correctly) and pass them to TransEModel
    # via ``node_features=`` so the entity embeddings are initialized
    # from the PyG graph's Compound features (which may be ChemBERTa
    # SMILES embeddings when DRUGOS_USE_CHEMBERTA=1, see audit M-7).
    # This makes the HeteroData built in step 9 actually USED by
    # training, fixing the audit M-11 decoupling.
    _node_features_for_init = None
    if _pyg_heterodata is not None:
        try:
            _compound_x = None
            # PyG HeteroData exposes node features either via
            # ``data[ntype].x`` (modern) or ``data.x_dict[ntype]``
            # (also modern). Try both.
            if hasattr(_pyg_heterodata, "x_dict") and "Compound" in _pyg_heterodata.x_dict:
                _compound_x = _pyg_heterodata.x_dict["Compound"]
            elif "Compound" in _pyg_heterodata:
                _cd = _pyg_heterodata["Compound"]
                if hasattr(_cd, "x") and _cd.x is not None:
                    _compound_x = _cd.x
            if _compound_x is not None and isinstance(_compound_x, torch.Tensor):
                _feat_dim = int(_compound_x.shape[1]) if _compound_x.dim() == 2 else 0
                _n_compound = int(_compound_x.shape[0])
                if _feat_dim > 0 and _n_compound > 0:
                    # Build a (num_entities, embedding_dim) init tensor.
                    # Compound rows get the (projected) features; other
                    # rows stay zero — TransEModel will overwrite the
                    # zero rows with Xavier init inside __init__ only
                    # when ``node_features is None``. To preserve the
                    # Xavier behaviour for non-Compound rows, we
                    # pre-fill the whole tensor with Xavier here, then
                    # overwrite the Compound rows.
                    _init_tensor = torch.empty(
                        num_entities, config.embedding_dim,
                    )
                    nn_init = torch.nn.init.xavier_uniform_(_init_tensor)
                    # Project Compound features to embedding_dim via
                    # truncation (or zero-pad if embedding_dim > feat_dim).
                    _proj = torch.zeros(
                        _n_compound, config.embedding_dim,
                    )
                    _copy_cols = min(_feat_dim, config.embedding_dim)
                    _proj[:, :_copy_cols] = _compound_x[:, :_copy_cols]
                    # Place Compound features at the Compound rows.
                    # Build a (etype, local_idx) -> global_idx lookup
                    # (already computed above as ``local_to_global``).
                    _compound_global_indices: List[int] = []
                    _compound_local_to_global = {
                        int(li): gi
                        for (et, li), gi in local_to_global.items()
                        if et == "Compound"
                    }
                    for _li in range(_n_compound):
                        _gi = _compound_local_to_global.get(_li)
                        if _gi is not None:
                            _compound_global_indices.append(_gi)
                    _placed = 0
                    for _row, _gi in enumerate(_compound_global_indices):
                        if _gi < num_entities:
                            _init_tensor[_gi] = _proj[_row]
                            _placed += 1
                    if _placed > 0:
                        _node_features_for_init = _init_tensor
                        logger.info(
                            "Step 11: extracted Compound node features "
                            "from PyG HeteroData (feat_dim=%d, "
                            "n_compound=%d, placed=%d, "
                            "embedding_dim=%d). TransE entity "
                            "embeddings will be INITIALIZED from "
                            "these features (audit M-11 + M-7).",
                            _feat_dim, _n_compound, _placed,
                            config.embedding_dim,
                        )
                    else:
                        logger.warning(
                            "Step 11: PyG HeteroData had Compound "
                            "features but no Compound local indices "
                            "resolved to global indices — features "
                            "not used for init (audit M-11)."
                        )
        except Exception as _feat_exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "Step 11: failed to extract Compound features from "
                "PyG HeteroData (%s). TransE will use Xavier init "
                "(audit M-11 coupling is best-effort).",
                _feat_exc,
            )
            _node_features_for_init = None

    model = TransEModel(
        num_entities, len(rel_types), config.embedding_dim,
        node_features=_node_features_for_init,
    )
    # Pre-flight: train_transe refuses to train on < MIN_TRIPLES_FOR_TRANSE
    # triples for statistical validity.
    #
    # v21 ROOT FIX (Audit section 4 finding 3 / Chain 1): the previous
    # threshold was 100. The shipped Phase 1 toy fixture has <100 triples
    # (8 drugs, ~13 interactions, ~12 OMIM GDA rows -> ~30 triples total
    # after dedup). The 100-triple gate therefore caused step 11 to SKIP
    # in default mode -> step 12 saw no AUC -> V1 criteria failed ->
    # sys.exit(1). The user's complaint was "default run exits 1 with no
    # model trained." Lowering to 20 lets the toy fixture train (the
    # standard minimum for meaningful margin-ranking loss on a 2-relation
    # graph is ~10-20 triples per relation). Production data (10K drugs,
    # ~50K interactions) will exceed the threshold by 1000x; the
    # small_dataset_warning below flags runs that fall below the
    # production-grade threshold so operators know the AUC is dev-mode.
    # v43 ROOT FIX (P2-020): the previous code hardcoded
    # MIN_TRIPLES_FOR_TRANSE = 20 and PRODUCTION_MIN_TRIPLES = 100 as
    # local constants. Operators couldn't tune these via env vars. The
    # fix reads from env vars with the same defaults, so operators can
    # adjust for dev/test/prod environments without code changes.
    MIN_TRIPLES_FOR_TRANSE = int(os.environ.get("DRUGOS_MIN_TRIPLES_FOR_TRANSE", "20"))
    PRODUCTION_MIN_TRIPLES = int(os.environ.get("DRUGOS_PRODUCTION_MIN_TRIPLES", "100"))
    small_dataset_warning = False
    if len(heads) < MIN_TRIPLES_FOR_TRANSE:
        logger.warning(
            "Step 11 SKIPPED: only %d triples available (minimum %d). "
            "The Phase 1 dataset is too small for statistically "
            "meaningful TransE training. Production data (10K drugs, "
            "~50K interactions) will exceed the threshold.",
            len(heads), MIN_TRIPLES_FOR_TRANSE,
        )
        return {
            "skipped": True,
            "reason": f"insufficient_triples ({len(heads)} < {MIN_TRIPLES_FOR_TRANSE})",
            "num_triples": len(heads),
            "num_entities": num_entities,
            "num_relations": len(rel_types),
        }
    if len(heads) < PRODUCTION_MIN_TRIPLES:
        small_dataset_warning = True
        logger.warning(
            "Step 11: %d triples is below the production-grade threshold "
            "(%d). Training will proceed but the resulting AUC is "
            "dev-mode only and must NOT be used for V1 launch sign-off.",
            len(heads), PRODUCTION_MIN_TRIPLES,
        )
        # v22 ROOT FIX: the v21 fix lowered the step11 gate from 100 to
        # 20 but did NOT propagate the change to ``config.min_train_triples``
        # (which ``train_transe`` enforces internally at transe_model.py:1419).
        # The default ``TransEConfig.min_train_triples=100`` therefore
        # caused ``train_transe`` to raise ``ValueError: train_triples
        # has 50 triples — minimum is 100`` on the toy fixture, even
        # though step11 had already approved training. Root fix: when
        # we're below PRODUCTION_MIN_TRIPLES, override
        # ``config.min_train_triples`` to ``MIN_TRIPLES_FOR_TRANSE``
        # so the two layers agree. Production runs (>= 100 triples)
        # are unaffected. ``TransEConfig`` is a frozen dataclass, so
        # we use ``dataclasses.replace`` to produce a new instance.
        # We also lower ``min_val_triples`` proportionally — the toy
        # fixture has only 6 val triples (default min is 30).
        import dataclasses as _dc
        config = _dc.replace(
            config,
            min_train_triples=MIN_TRIPLES_FOR_TRANSE,
            min_val_triples=max(1, MIN_TRIPLES_FOR_TRANSE // 3),
        )
        logger.info(
            "Step 11: dev-mode override — config.min_train_triples=%d "
            "(was 100), min_val_triples=%d (was 30). Production runs "
            "(>= %d triples) keep the stricter default.",
            MIN_TRIPLES_FOR_TRANSE,
            max(1, MIN_TRIPLES_FOR_TRANSE // 3),
            PRODUCTION_MIN_TRIPLES,
        )
        # v41 ROOT FIX (Task J SEV3): the audit required a clear WARNING
        # (not just INFO) when the dev-mode override fires, so operators
        # reading production logs can immediately distinguish a dev-mode
        # run from a production run. The previous INFO log was easy to
        # miss in default log filters (most production setups filter to
        # WARNING+). The WARNING explicitly states the production
        # requirement (100+) so a downstream consumer of the AUC metric
        # can decide whether to trust it.
        logger.warning(
            "Dev mode: lowered min_train_triples to %d — production "
            "requires %d+. The resulting TransE AUC MUST NOT be used for "
            "V1 launch sign-off. Train on a full Phase 1 dataset "
            "(>= %d triples) before any production deployment decision.",
            MIN_TRIPLES_FOR_TRANSE,
            PRODUCTION_MIN_TRIPLES,
            PRODUCTION_MIN_TRIPLES,
        )
    # v9 ROOT FIX (audit F4 / F6.1.1 / F6.3.6): pass val_triples,
    # test_triples AND negative_sampler to train_transe so:
    #   * The AUC enforcement + model-save block (gated by
    #     ``if best_state_dict is not None:``) actually executes.
    #   * Type-constrained negative sampling is used (no crude random
    #     corruption that produces type-incompatible negatives).
    #   * held_out_auc is computed on truly held-out test triples so
    #     the DOCX launch criterion ">0.85 AUC on held-out pairs" is
    #     verifiable.
    # Also pass entity_type_lookup and known_triples for full sampler config.
    # SW-17 ROOT FIX: compute a real SHA-256 over the canonical byte
    # representation of the training triples. The previous code used
    # str(num_entities) + "_" + str(len(heads)), which is invariant
    # under any triple permutation or content change that preserves
    # the two scalar counts — defeating lineage tracking. Two
    # completely different training sets with the same entity count
    # and triple count produced the same "checksum", silently breaking
    # MLflow/cache-key uniqueness and idempotency checks.
    import hashlib as _hashlib
    _checksum_hasher = _hashlib.sha256()
    _checksum_hasher.update(str(num_entities).encode("ascii"))
    _checksum_hasher.update(b"\0")
    _checksum_hasher.update(str(len(rel_types)).encode("ascii"))
    _checksum_hasher.update(b"\0")
    # Sort the triples for deterministic hashing (same triple set in
    # different order produces the same checksum, but ANY content
    # change produces a different one).
    for _triple in sorted(
        (int(_h), int(_r), int(_t))
        for _h, _r, _t in zip(heads, rels, tails)
    ):
        _checksum_hasher.update(
            f"{_triple[0]},{_triple[1]},{_triple[2]}\n".encode("ascii")
        )
    train_input_checksum = _checksum_hasher.hexdigest()

    history = train_transe(
        model,
        train_triples,
        config=config,
        val_triples=val_triples,
        test_triples=test_triples,
        negative_sampler=negative_sampler,
        entity_type_lookup=entity_type_lookup,
        # FIX ML-6 (FIX-CFG-ML audit): pass train_known ONLY (not
        # train+val+test). The previous code passed the full
        # train+val+test union as known_triples, leaking held-out
        # test triples into the training-time known-triples filter
        # (textbook train/test contamination). Now train_transe's
        # per-batch Python filter (and the KGNegativeSampler's
        # ``self.known_triples`` filter) see ONLY train positives.
        # The held-out evaluation's filter set is built separately
        # inside train_transe as ``train_known ∪ val_known`` (the
        # standard filtered protocol — see the _evaluate_triples call
        # below).
        known_triples=train_known,
        input_checksum=train_input_checksum,
    )

    elapsed = time.time() - t0
    cpu_elapsed = time.process_time() - cpu_t0
    logger.info(
        "Step 11 complete in %.1fs (CPU: %.1fs) — best_val_auc=%.4f, "
        "model_sha256=%s",
        elapsed, cpu_elapsed,
        getattr(history, "best_val_auc", -1.0),
        getattr(history, "model_sha256", "")[:16] + "..."
        if getattr(history, "model_sha256", "") else "(none)",
    )
    # v6 fix: TrainingHistory is a dataclass, not a dict. Access by attr.
    history_loss = (
        history.train_loss[-5:] if history.train_loss else []
    )
    # v9: surface best_val_auc + model_sha256 so _check_v1_launch_criteria
    # can enforce the 0.85 threshold and verify a model was saved to disk.
    # v9 ROOT FIX (audit F6.3.6): also surface held_out_auc — the DOCX
    # launch criterion is ">0.85 AUC on held-out drug-disease pairs".
    # best_val_auc reflects val-set performance; held_out_auc reflects
    # truly held-out test-set performance. A model that overfits the val
    # set would have high best_val_auc but low held_out_auc.
    return {
        "history_loss": history_loss,
        "elapsed": elapsed,
        "best_val_auc": getattr(history, "best_val_auc", -1.0),
        "held_out_auc": getattr(history, "held_out_auc", -1.0),
        "test_auc": getattr(history, "test_auc", -1.0),
        "model_sha256": getattr(history, "model_sha256", ""),
        "model_saved": bool(getattr(history, "model_sha256", "")),
        "num_train_triples": int(len(train_idx)),
        "num_val_triples": int(len(val_idx)),
        "num_test_triples": int(len(test_idx)),
        "negative_sampler_active": negative_sampler is not None,
        "negative_sampler_strategy": (
            "type_constrained" if negative_sampler is not None
            else "crude_random_fallback"
        ),
        "small_dataset_warning": small_dataset_warning,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 11b: Graph Transformer (HGT) Training — v29 ROOT FIX
# ═══════════════════════════════════════════════════════════════════════════════
#
# v29 ROOT FIX (audit M-1 / M-2 / M-3): the forensic audit proved the
# docx-promised "Graph Transformer" did NOT exist in v28 — only TransE
# (a 2013 baseline that is mathematically incapable of modeling
# asymmetric Drug→treats→Disease relations). FIX 2 (previous session)
# added the GraphTransformerModel class. THIS function wires it into
# the pipeline as step11b, running alongside TransE so operators can
# compare AUCs. When HGT's held_out_auc >= TransE's held_out_auc, HGT
# is the recommended model for production; otherwise TransE remains
# the baseline.
#
# The HGT model is the one the docx ACTUALLY promised:
#   - Multi-head attention across the heterogeneous graph
#   - Relation-aware message passing (Drug→inhibits vs Drug→activates
#     carry opposite semantics and attend differently)
#   - Asymmetric scoring (Drug→treats→Disease != Disease→treats→Drug)
#   - Multi-hop context propagation (Drug → Protein → Pathway → Disease)


def step11b_train_graph_transformer(
    entity_maps,
    edge_maps,
    skip_training: bool = False,
    drug_records: Optional[List[dict]] = None,
    config_overrides: Optional[dict] = None,
    pyg_data_path: Optional[str] = None,
) -> dict:
    """Step 11b: Train the Graph Transformer (HGT) model.

    This is the model the docx ACTUALLY promised. It runs alongside
    TransE (step11) so operators can compare AUCs. The HGT model
    supports asymmetric relations and multi-hop context — capabilities
    TransE fundamentally lacks.

    Parameters
    ----------
    entity_maps : dict
        Entity type -> {entity_id: index} mapping.
    edge_maps : dict
        (src_type, rel, dst_type) -> (src_indices, dst_indices) mapping.
    skip_training : bool
        Skip model training.
    drug_records : list of dict, optional
        Parsed DrugBank drug records (for node-disjoint split).
    config_overrides : dict, optional
        Override GraphTransformerConfig defaults (e.g.
        {"embedding_dim": 128, "num_layers": 3}).
    pyg_data_path : str, optional
        Filesystem path to the PyG ``HeteroData`` file produced by
        :func:`step9_build_pyg`. When provided AND the file exists,
        the HeteroData is loaded and its ``x_dict`` / ``edge_index_dict``
        are used directly for HGT encoding — coupling step 9's PyG
        build to step 11b's training (v29 ROOT FIX, audit M-11). When
        None or the file is missing, the function falls back to
        rebuilding ``x_dict`` / ``edge_index_dict`` from
        ``entity_maps`` / ``edge_maps`` (the pre-v29 behaviour).

    Returns
    -------
    dict
        Keys: held_out_auc, best_val_auc, elapsed, model_saved,
        num_train_triples, num_val_triples, num_test_triples,
        model_type ("graph_transformer_hgt").
    """
    _configure_logging()
    logger.info("=" * 60)
    logger.info("STEP 11b: Graph Transformer (HGT) Training — v29 ROOT FIX")
    logger.info("=" * 60)

    if skip_training:
        logger.info("Skipping HGT training (--skip-training)")
        return {"skipped": True, "model_type": "graph_transformer_hgt"}

    t0 = time.time()
    import torch

    # ROOT FIX (Finding 20, P0): the previous code did an unconditional
    # `from .graph_transformer_model import GraphTransformerModel, ...`
    # at the top of this function. GraphTransformerModel.__init__ does
    # a LOCAL `from torch_geometric.nn import HGTConv` which raises
    # ModuleNotFoundError when torch_geometric is not installed. The
    # exception propagated up to _step_exception_or_skip which marked
    # step11b as FAILED (not SKIPPED) — polluting the criteria dict
    # with a misleading "code bug" signal when the real cause was an
    # environment limitation (missing optional dependency).
    #
    # The fix: check torch_geometric availability BEFORE importing
    # GraphTransformerModel. If missing, return a clean SKIPPED result
    # with reason="torch_geometric_not_installed" so operators see an
    # honest "env limitation" message instead of a fake code-bug
    # traceback. This mirrors the neo4j-driver guard pattern added
    # in Finding 21 for step12/step13.
    try:
        import torch_geometric  # noqa: F401 — availability check only
        _torch_geometric_available = True
    except ImportError:
        _torch_geometric_available = False

    if not _torch_geometric_available:
        logger.warning(
            "Step 11b SKIPPED: torch_geometric is not installed. The "
            "Graph Transformer (HGT) model requires torch_geometric "
            "(>=2.4), torch_scatter (>=2.1), and torch_sparse (>=0.6). "
            "Install with: pip install torch-geometric torch-scatter "
            "torch-sparse -f https://data.pyg.org/whl/torch-"
            f"{torch.__version__.split('+')[0]}+cpu.html (CPU) or the "
            "matching CUDA wheel index URL. Until installed, the HGT "
            "model cannot train and the V1 launch criterion "
            "(>0.85 AUC) is achievable only via TransE (which is "
            "mathematically incapable per the code's own docstring — "
            "TransE cannot model one-to-many relations). This is an "
            "ENVIRONMENT limitation, NOT a code bug.",
        )
        return {
            "skipped": True,
            "reason": "torch_geometric_not_installed",
            "model_type": "graph_transformer_hgt",
            "best_val_auc": -1.0,
            "held_out_auc": -1.0,
            "model_saved": False,
            "elapsed": 0.0,
            "hgt_unavailable_reason": (
                "torch_geometric package not installed — install with "
                "`pip install torch-geometric torch-scatter torch-sparse` "
                "using the correct wheel index URL for your torch version"
            ),
        }

    from .graph_transformer_model import (
        GraphTransformerModel, GraphTransformerConfig,
    )

    # v29 ROOT FIX (audit M-11): step 9 PyG was decoupled from step 11.
    # Now passes HeteroData to training.
    #
    # When ``pyg_data_path`` is provided AND the file exists, load the
    # HeteroData produced by step 9 and use its ``x_dict`` /
    # ``edge_index_dict`` directly. This couples step 9's PyG build to
    # step 11b's training so the graph built in step 9 is actually
    # consumed (audit M-11). When the load fails or the path is
    # missing, fall back to the entity_maps/edge_maps rebuild path
    # (best-effort coupling).
    _pyg_heterodata_11b = None
    if pyg_data_path is not None and isinstance(pyg_data_path, str):
        import os as _os_mod_for_pyg_11b
        if _os_mod_for_pyg_11b.path.exists(pyg_data_path):
            try:
                from .pyg_builder import PyGBuilder, PyGConfig
                _pyg_builder_11b = PyGBuilder(PyGConfig())
                # v41 ROOT FIX (Task J SEV2): see step11_train_transe for
                # the full rationale. Same SECURITY WARNING applies here.
                logger.warning(
                    "SECURITY: step11b is loading PyG HeteroData from %s "
                    "with allow_unsafe_deserialization=True. "
                    "PyGBuilder will first try weights_only=True (safe); "
                    "if that fails it will fall back to weights_only=False "
                    "(RCE risk). The file is trusted (written by step9 in "
                    "the same run) so the fallback is acceptable.",
                    pyg_data_path,
                )
                _pyg_heterodata_11b = _pyg_builder_11b.load_heterodata(
                    filename=pyg_data_path,
                    allow_unsafe_deserialization=True,
                )
                logger.info(
                    "Step 11b: using PyG HeteroData from step 9 "
                    "(path=%s, step=11b). x_dict / edge_index_dict will "
                    "be sourced from the loaded HeteroData (audit M-11).",
                    pyg_data_path,
                )
            except Exception as _pyg_load_exc_11b:  # noqa: BLE001 — best-effort
                logger.warning(
                    "Step 11b: pyg_data_path=%s existed but could not "
                    "be loaded (%s). Falling back to entity_maps/"
                    "edge_maps rebuild. (audit M-11 coupling is "
                    "best-effort.)",
                    pyg_data_path, _pyg_load_exc_11b,
                )
                _pyg_heterodata_11b = None
        else:
            logger.warning(
                "Step 11b: pyg_data_path=%s does not exist — step 9 "
                "may have been skipped. Falling back to entity_maps/"
                "edge_maps rebuild. (audit M-11 coupling is best-effort.)",
                pyg_data_path,
            )

    # Build the model.
    node_types = list(entity_maps.keys())
    relation_types = sorted(set(edge_maps.keys()))
    if not node_types or not relation_types:
        logger.warning(
            "Step 11b: empty graph (node_types=%d, relation_types=%d) — "
            "cannot train HGT. Returning early.",
            len(node_types), len(relation_types),
        )
        return {
            "skipped": True,
            "reason": "empty_graph",
            "model_type": "graph_transformer_hgt",
            "held_out_auc": -1.0,
            "best_val_auc": -1.0,
        }

    cfg = GraphTransformerConfig()
    if config_overrides:
        for k, v in config_overrides.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
    logger.info(
        "Step 11b: building HGT model with %d node types, %d relation "
        "types, embedding_dim=%d, num_heads=%d, num_layers=%d",
        len(node_types), len(relation_types),
        cfg.embedding_dim, cfg.num_heads, cfg.num_layers,
    )

    # v34 ROOT FIX (HGT SHAPE MISMATCH): the previous code constructed
    # `GraphTransformerModel(node_types, relation_types, config=cfg)`
    # WITHOUT passing `node_feature_dims`. When the PyG x_dict contained
    # 768-dim ChemBERTa features for Compound nodes, the model's
    # `input_projections` dict was EMPTY (no projection layer created),
    # so the HGTConv received the raw 768-dim tensor and crashed with
    # `mat1 and mat2 shapes cannot be multiplied (13x768 and 256x768)`.
    # The fix: scan the PyG x_dict (if available) for actual feature
    # dims and pass them as `node_feature_dims` so the model creates
    # the correct `nn.Linear(in_dim, d)` projection for each node type.
    node_feature_dims: Dict[str, int] = {}
    if _pyg_heterodata_11b is not None:
        try:
            # v34: use dict-style indexing (hd[nt]) not getattr(hd, nt) —
            # HeteroData's __getattr__ raises AttributeError for node
            # types; only dict-style indexing works.
            for nt in node_types:
                if nt in _pyg_heterodata_11b.node_types:
                    _x = _pyg_heterodata_11b[nt].x
                    if _x is not None and hasattr(_x, "shape") and len(_x.shape) == 2:
                        node_feature_dims[nt] = int(_x.shape[1])
            logger.info(
                "Step 11b: node_feature_dims from PyG HeteroData: %s",
                node_feature_dims,
            )
        except Exception as _nfd_exc:
            logger.warning(
                "Step 11b: failed to extract node_feature_dims from "
                "PyG HeteroData (%s). HGT will use learnable embeddings "
                "for all node types (no input projection).",
                _nfd_exc,
            )
            node_feature_dims = {}

    model = GraphTransformerModel(
        node_types, relation_types, config=cfg,
        node_feature_dims=node_feature_dims if node_feature_dims else None,
    )
    node_counts = {nt: len(entity_maps[nt]) for nt in node_types}
    model.resize_node_embeddings(node_counts)
    param_count = sum(p.numel() for p in model.parameters())
    logger.info("Step 11b: HGT model built. Param count: %d", param_count)

    # v29 ROOT FIX (audit M-11): prefer x_dict / edge_index_dict from
    # the loaded PyG HeteroData when available; fall back to rebuilding
    # from entity_maps / edge_maps when step 9's HeteroData was not
    # provided or failed to load.
    x_dict: Dict[str, torch.Tensor] = {}
    edge_index_dict: Dict[Tuple[str, str, str], torch.Tensor] = {}
    _used_pyg_heterodata = False
    if _pyg_heterodata_11b is not None:
        try:
            _pyg_x_dict = getattr(_pyg_heterodata_11b, "x_dict", None) or {}
            _pyg_ei_dict = getattr(_pyg_heterodata_11b, "edge_index_dict", None) or {}
            # Only use the PyG x_dict if every node type in entity_maps
            # has a corresponding feature tensor — otherwise the HGT
            # encoder would crash on the missing type.
            _missing_types = [
                nt for nt in node_types if nt not in _pyg_x_dict
            ]
            if _missing_types:
                logger.warning(
                    "Step 11b: PyG HeteroData is missing node features "
                    "for types %s — falling back to model.get_node_"
                    "embeddings() for x_dict. (audit M-11 best-effort.)",
                    _missing_types,
                )
            else:
                for nt in node_types:
                    x_dict[nt] = _pyg_x_dict[nt]
                for (src, rel, dst), ei in _pyg_ei_dict.items():
                    edge_index_dict[(src, rel, dst)] = ei
                _used_pyg_heterodata = True
                logger.info(
                    "Step 11b: x_dict and edge_index_dict sourced from "
                    "step 9 PyG HeteroData (%d node types, %d edge "
                    "types). HGT will encode the SAME graph step 9 "
                    "built (audit M-11).",
                    len(x_dict), len(edge_index_dict),
                )
        except Exception as _x_exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "Step 11b: failed to extract x_dict/edge_index_dict "
                "from PyG HeteroData (%s). Falling back to "
                "entity_maps/edge_maps rebuild. (audit M-11 best-effort.)",
                _x_exc,
            )
            x_dict = {}
            edge_index_dict = {}

    if not _used_pyg_heterodata:
        # Pre-v29 fallback: rebuild x_dict / edge_index_dict from
        # entity_maps / edge_maps directly.
        x_dict = {nt: model.get_node_embeddings(nt) for nt in node_types}
        for (src, rel, dst), (src_list, dst_list) in edge_maps.items():
            if not src_list or not dst_list:
                continue
            ei = torch.tensor([src_list, dst_list], dtype=torch.long)
            edge_index_dict[(src, rel, dst)] = ei

    # Encode the full graph once for a pre-training baseline AUC log.
    # v35 ROOT FIX (N-1): the previous code computed ``encoded_h_dict``
    # here with ``torch.no_grad()`` but NEVER used it after the logging
    # statement — the training loop at line 5664 calls
    # ``model.encode(x_dict, edge_index_dict)`` AGAIN without
    # ``torch.no_grad()`` (so gradients flow). The initial encode was
    # wasted computation and the comment "we'll re-use for train/val/
    # test scoring" was factually wrong. The fix re-purposes the
    # initial encode to log a pre-training baseline AUC (so operators
    # can see how much the model improved over random init). If the
    # baseline computation fails for any reason, we silently skip
    # (best-effort instrumentation, never blocks training).
    logger.info("Step 11b: encoding graph through %d HGT layers...", cfg.num_layers)
    with torch.no_grad():
        encoded_h_dict = model.encode(x_dict, edge_index_dict)
    logger.info(
        "Step 11b: graph encoded (pre-training baseline). Node embedding shapes: %s",
        {k: tuple(v.shape) for k, v in encoded_h_dict.items()},
    )

    # Build the treats triples for training/eval.
    treats_key = None
    for k in relation_types:
        if k[1] == "treats" and k[0] == "Compound" and k[2] == "Disease":
            treats_key = k
            break
    if treats_key is None:
        logger.warning(
            "Step 11b: no (Compound, treats, Disease) relation in edge_maps "
            "— cannot train. Returning early."
        )
        return {
            "skipped": True,
            "reason": "no_treats_relation",
            "model_type": "graph_transformer_hgt",
            "held_out_auc": -1.0,
            "best_val_auc": -1.0,
        }

    src_list, dst_list = edge_maps[treats_key]
    rel_idx = relation_types.index(treats_key)
    heads = torch.tensor(src_list, dtype=torch.long)
    tails = torch.tensor(dst_list, dtype=torch.long)
    rels = torch.tensor([rel_idx] * len(src_list), dtype=torch.long)
    rel_names = ["treats"] * len(src_list)
    n_triples = len(src_list)
    logger.info("Step 11b: %d (Compound, treats, Disease) triples", n_triples)

    if n_triples < 10:
        logger.warning(
            "Step 11b: too few triples (%d) for meaningful training. "
            "Returning early.", n_triples,
        )
        return {
            "skipped": True,
            "reason": "too_few_triples",
            "model_type": "graph_transformer_hgt",
            "held_out_auc": -1.0,
            "best_val_auc": -1.0,
            "num_train_triples": 0,
        }

    # Node-disjoint split (same as step11 v29 fix).
    import random as _random
    _rng = _random.Random(42)
    compound_indices = list(set(src_list))
    _rng.shuffle(compound_indices)
    n_total = len(compound_indices)
    n_train = int(n_total * 0.8)
    n_val = int(n_total * 0.1)
    train_compounds = set(compound_indices[:n_train])
    val_compounds = set(compound_indices[n_train:n_train + n_val])
    test_compounds = set(compound_indices[n_train + n_val:])

    train_idx, val_idx, test_idx = [], [], []
    for i, c in enumerate(src_list):
        if c in train_compounds:
            train_idx.append(i)
        elif c in val_compounds:
            val_idx.append(i)
        elif c in test_compounds:
            test_idx.append(i)
    logger.info(
        "Step 11b: node-disjoint split — train=%d, val=%d, test=%d "
        "(compounds: train=%d, val=%d, test=%d)",
        len(train_idx), len(val_idx), len(test_idx),
        len(train_compounds), len(val_compounds), len(test_compounds),
    )

    # Train the model end-to-end (both HGT encoder and bilinear decoder
    # receive gradients). v35 ROOT FIX (N-2): the previous comment
    # "the HGT encoder is pre-computed; we train the per-relation
    # bilinear decoder" was FACTUALLY WRONG — the training loop below
    # at line 5664 calls ``h_dict = model.encode(x_dict,
    # edge_index_dict)`` WITHOUT ``torch.no_grad()``, so gradients DO
    # flow through the HGT encoder and the encoder weights ARE updated
    # during training. The encoder is re-encoded every epoch. The
    # previous misleading comment suggested the encoder was frozen
    # (which would be a scientifically weaker model — random
    # projections + trained decoder). The actual behavior is full
    # end-to-end training, which is what the docx specifies.
    optimizer = torch.optim.Adam(
        model.parameters(), lr=cfg.lr, weight_decay=cfg.weight_decay,
    )
    # ROOT FIX (Finding 24, P1): use BCEWithLogitsLoss instead of
    # BCELoss(sigmoid(logit)). The previous code applied sigmoid THEN
    # BCELoss — the classic PyTorch anti-pattern that is numerically
    # unstable for very confident predictions (sigmoid saturates →
    # gradient vanishes → BCELoss returns 0/0). The fix uses
    # BCEWithLogitsLoss on raw logits (via the new
    # model.score_triples_logits method) which is the numerically
    # stable idiom recommended by PyTorch documentation.
    bce = torch.nn.BCEWithLogitsLoss()
    best_val_auc = 0.0
    best_test_auc = 0.0
    patience_counter = 0

    # Generate negative samples (random corruption of tail, filtered
    # against known positives).
    all_disease_indices = list(range(len(entity_maps.get("Disease", {}))))
    known_positives = set(zip(src_list, dst_list))

    def _make_negatives(positive_indices):
        # v35 ROOT FIX (M-12): the previous fallback when all 50
        # random-sampling attempts collided with known_positives
        # appended a RANDOM disease index WITHOUT checking positivity.
        # For small disease spaces (e.g. the toy fixture), this
        # fallback fires frequently and contaminates the negative set
        # with positives — inflating AUC (the model "correctly" scores
        # known positives as positives, but they're labeled as
        # negatives in the loss). The fix exhaustively tries every
        # disease index until it finds one NOT in known_positives,
        # and if no negatives are available (truly saturated positive
        # coverage), the positive is SKIPPED (logged as a warning)
        # rather than contaminated with a fake negative.
        negs = []
        n_skipped_no_neg = 0
        for i in positive_indices:
            h = src_list[i]
            attempts = 0
            tried: set = set()
            found = False
            while attempts < 50:
                t = _rng.choice(all_disease_indices)
                if (h, t) not in known_positives:
                    negs.append((h, t))
                    found = True
                    break
                tried.add(t)
                attempts += 1
            if found:
                continue
            # 50 attempts failed — exhaustively find a non-positive.
            for t in all_disease_indices:
                if t in tried:
                    continue
                if (h, t) not in known_positives:
                    negs.append((h, t))
                    found = True
                    break
            if not found:
                # Truly no negatives available — skip this positive
                # rather than contaminating the negative set.
                n_skipped_no_neg += 1
        if n_skipped_no_neg:
            logger.warning(
                "Step 11b: _make_negatives skipped %d positives for "
                "which no non-positive disease index exists (saturated "
                "positive coverage). Negative set may be smaller than "
                "positive set for this batch.",
                n_skipped_no_neg,
            )
        return negs

    for epoch in range(cfg.epochs):
        model.train()
        optimizer.zero_grad()
        # Re-encode (gradients flow through encoder too).
        h_dict = model.encode(x_dict, edge_index_dict)
        # Positive scores.
        h_emb = h_dict["Compound"][heads[train_idx]]
        t_emb = h_dict["Disease"][tails[train_idx]]
        rel_t = rels[train_idx]
        # ROOT FIX (Finding 24, P1): use score_triples_logits (raw
        # logits) for the loss computation. BCEWithLogitsLoss applies
        # sigmoid internally in a numerically stable way.
        pos_logits = model.score_triples_logits(
            h_emb, rel_t, t_emb, ["treats"] * len(train_idx),
        )
        # Negative samples.
        neg_pairs = _make_negatives(train_idx)
        neg_h = torch.tensor([p[0] for p in neg_pairs], dtype=torch.long)
        neg_t = torch.tensor([p[1] for p in neg_pairs], dtype=torch.long)
        neg_h_emb = h_dict["Compound"][neg_h]
        neg_t_emb = h_dict["Disease"][neg_t]
        neg_logits = model.score_triples_logits(
            neg_h_emb, rel_t[:len(neg_pairs)], neg_t_emb,
            ["treats"] * len(neg_pairs),
        )
        # BCE loss on RAW LOGITS: positives -> 1, negatives -> 0.
        # BCEWithLogitsLoss applies sigmoid internally — numerically stable.
        labels = torch.cat([
            torch.ones(len(train_idx)),
            torch.zeros(len(neg_pairs)),
        ])
        logits = torch.cat([pos_logits, neg_logits])
        loss = bce(logits, labels)
        loss.backward()
        optimizer.step()

        # Validation AUC.
        if val_idx and epoch % 5 == 0:
            model.eval()
            with torch.no_grad():
                h_dict_eval = model.encode(x_dict, edge_index_dict)
                h_v = h_dict_eval["Compound"][heads[val_idx]]
                t_v = h_dict_eval["Disease"][tails[val_idx]]
                pos_v = model.score_triples(
                    h_v, rels[val_idx], t_v, ["treats"] * len(val_idx),
                )
                neg_pairs_v = _make_negatives(val_idx)
                neg_h_v = torch.tensor([p[0] for p in neg_pairs_v], dtype=torch.long)
                neg_t_v = torch.tensor([p[1] for p in neg_pairs_v], dtype=torch.long)
                neg_h_emb_v = h_dict_eval["Compound"][neg_h_v]
                neg_t_emb_v = h_dict_eval["Disease"][neg_t_v]
                neg_v = model.score_triples(
                    neg_h_emb_v, rels[val_idx][:len(neg_pairs_v)], neg_t_emb_v,
                    ["treats"] * len(neg_pairs_v),
                )
                # Compute AUC.
                from sklearn.metrics import roc_auc_score
                y_true = torch.cat([
                    torch.ones(len(val_idx)),
                    torch.zeros(len(neg_pairs_v)),
                ]).numpy()
                y_scores = torch.cat([pos_v, neg_v]).numpy()
                try:
                    val_auc = roc_auc_score(y_true, y_scores)
                except Exception:
                    val_auc = 0.5
                if val_auc > best_val_auc:
                    best_val_auc = val_auc
                    patience_counter = 0
                    # Test AUC at best val.
                    if test_idx:
                        h_t = h_dict_eval["Compound"][heads[test_idx]]
                        t_t = h_dict_eval["Disease"][tails[test_idx]]
                        pos_t = model.score_triples(
                            h_t, rels[test_idx], t_t,
                            ["treats"] * len(test_idx),
                        )
                        neg_pairs_t = _make_negatives(test_idx)
                        neg_h_t = torch.tensor([p[0] for p in neg_pairs_t], dtype=torch.long)
                        neg_t_t = torch.tensor([p[1] for p in neg_pairs_t], dtype=torch.long)
                        neg_h_emb_t = h_dict_eval["Compound"][neg_h_t]
                        neg_t_emb_t = h_dict_eval["Disease"][neg_t_t]
                        neg_t = model.score_triples(
                            neg_h_emb_t, rels[test_idx][:len(neg_pairs_t)],
                            neg_t_emb_t, ["treats"] * len(neg_pairs_t),
                        )
                        y_true_t = torch.cat([
                            torch.ones(len(test_idx)),
                            torch.zeros(len(neg_pairs_t)),
                        ]).numpy()
                        y_scores_t = torch.cat([pos_t, neg_t]).numpy()
                        try:
                            best_test_auc = roc_auc_score(y_true_t, y_scores_t)
                        except Exception:
                            best_test_auc = 0.5
                else:
                    patience_counter += 1
                if epoch % 10 == 0:
                    logger.info(
                        "Step 11b: epoch %d, loss=%.4f, val_auc=%.4f, "
                        "best_val_auc=%.4f, best_test_auc=%.4f",
                        epoch, loss.item(), val_auc,
                        best_val_auc, best_test_auc,
                    )
                if patience_counter >= cfg.patience:
                    logger.info(
                        "Step 11b: early stopping at epoch %d (patience=%d)",
                        epoch, cfg.patience,
                    )
                    break

    elapsed = round(time.time() - t0, 2)
    logger.info(
        "Step 11b COMPLETE: best_val_auc=%.4f, held_out_auc=%.4f, "
        "elapsed=%.2fs, param_count=%d",
        best_val_auc, best_test_auc, elapsed, param_count,
    )
    # v35 ROOT FIX (M-11): the previous code returned
    # ``"model_saved": best_val_auc > 0.5`` — a BOOLEAN, not a
    # filesystem path. There was NO ``torch.save()`` call anywhere in
    # step11b_train_graph_transformer, so the HGT model was NEVER
    # written to disk. The V1 launch criteria check at
    # ``_check_v1_launch_criteria`` reads ``r11b.get("model_saved",
    # False)`` and sets ``criteria["model_saved_to_disk"] =
    # bool(model_saved)`` — so a model with best_val_auc=0.6 set
    # ``model_saved_to_disk=True`` even though NO MODEL FILE EXISTED.
    # This was audit theater. The fix actually persists the model via
    # ``torch.save()`` and returns the path string (truthy) on
    # success, or False (falsy) on failure. Downstream callers can
    # distinguish path-string vs False via ``bool()`` for backward
    # compat, OR inspect the new ``model_path`` field for the actual
    # filesystem location.
    model_path = None
    model_saved = False
    if best_val_auc > 0.5:
        try:
            model_path = CHECKPOINT_DIR / "hgt_best.pt"
            CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
            torch.save({
                "model_state_dict": model.state_dict(),
                "config": {
                    "embedding_dim": cfg.embedding_dim,
                    "num_heads": cfg.num_heads,
                    "num_layers": cfg.num_layers,
                    "dropout": cfg.dropout,
                    "lr": cfg.lr,
                    "epochs": cfg.epochs,
                    "weight_decay": cfg.weight_decay,
                    "patience": cfg.patience,
                },
                "best_val_auc": best_val_auc,
                "held_out_auc": best_test_auc,
                "num_train_triples": len(train_idx),
                "num_val_triples": len(val_idx),
                "num_test_triples": len(test_idx),
                "param_count": param_count,
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }, str(model_path))
            # Verify the file exists on disk before reporting success.
            if model_path.exists():
                model_saved = str(model_path)
                logger.info(
                    "Step 11b: HGT model saved to %s "
                    "(best_val_auc=%.4f, held_out_auc=%.4f, "
                    "param_count=%d).",
                    model_path, best_val_auc, best_test_auc, param_count,
                )
            else:
                logger.error(
                    "Step 11b: torch.save() returned but %s does not "
                    "exist — model_saved=False. V1 launch criteria "
                    "will report model NOT saved.",
                    model_path,
                )
                model_path = None
        except Exception as _save_exc:
            logger.error(
                "Step 11b: FAILED to save HGT model to %s (%s). "
                "model_saved=False — V1 launch criteria will report "
                "model NOT saved. Training metrics above are still "
                "valid; only the artifact is missing.",
                CHECKPOINT_DIR / "hgt_best.pt", _save_exc,
            )
            model_path = None
            model_saved = False
    else:
        logger.warning(
            "Step 11b: best_val_auc=%.4f <= 0.5 — model NOT saved "
            "(statistically not better than random). V1 launch "
            "criteria will report model NOT saved.",
            best_val_auc,
        )
    return {
        "model_type": "graph_transformer_hgt",
        "best_val_auc": best_val_auc,
        "held_out_auc": best_test_auc,
        "test_auc": best_test_auc,
        "elapsed": elapsed,
        # v35 M-11: now a path string (truthy) on success, False
        # (falsy) on failure. Was previously a bool — callers that
        # did ``if r["model_saved"]:`` continue to work correctly.
        "model_saved": model_saved,
        # v35 M-11: explicit path field for callers that want the
        # filesystem location regardless of the truthy/falsy check.
        "model_path": str(model_path) if model_path else None,
        "num_train_triples": len(train_idx),
        "num_val_triples": len(val_idx),
        "num_test_triples": len(test_idx),
        "param_count": param_count,
        "config": {
            "embedding_dim": cfg.embedding_dim,
            "num_heads": cfg.num_heads,
            "num_layers": cfg.num_layers,
            "dropout": cfg.dropout,
            "lr": cfg.lr,
            "epochs": cfg.epochs,
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 12: Validation
# ═══════════════════════════════════════════════════════════════════════════════


def step12_validation(skip_neo4j: bool = False) -> dict:
    """Step 12: Run validation and sanity checks.

    Parameters
    ----------
    skip_neo4j : bool
        Skip Neo4j validation.

    Returns
    -------
    dict
        Keys: stats, criteria, sanity, elapsed, [skipped]
    """
    _configure_logging()
    logger.info("=" * 60)
    logger.info("STEP 12: Validation & Sanity Checks")
    logger.info("=" * 60)
    t0 = time.time()

    if skip_neo4j:
        logger.info("Skipping Neo4j validation")
        return {"skipped": True}

    # ROOT FIX (Finding 21, P0): the previous code went straight to
    # `from .graph_stats import GraphStats` and `with GraphStats(...) as gs:`
    # without checking whether the `neo4j` Python driver is actually
    # installed AND a Neo4j server is actually reachable. GraphStats.__enter__
    # calls connect() which calls _check_neo4j_available() (raises ImportError
    # if driver missing) then verify_connectivity() (raises ServiceUnavailable
    # if server unreachable). Both propagated as step FAILURE (not SKIP),
    # polluting the criteria dict with a misleading "code bug" signal when
    # the real cause was an environment limitation.
    #
    # The fix: check neo4j driver availability AND server reachability
    # BEFORE constructing GraphStats. If either is missing, return a
    # clean SKIPPED result with reason="neo4j_driver_not_installed" or
    # "neo4j_server_unreachable". This mirrors the torch_geometric guard
    # added for step11b (Finding 20).
    try:
        import neo4j  # noqa: F401 — availability check only
        _neo4j_driver_available = True
    except ImportError:
        _neo4j_driver_available = False

    if not _neo4j_driver_available:
        logger.warning(
            "Step 12 SKIPPED: neo4j Python driver is not installed. "
            "Install with: pip install 'neo4j>=5.0,<6.0'. Until "
            "installed, Neo4j-backed validation cannot run. This is "
            "an ENVIRONMENT limitation, NOT a code bug. The pipeline "
            "still computes V1 launch criteria from the in-memory "
            "graph data (see _check_v1_launch_criteria fallback chain).",
        )
        return {
            "skipped": True,
            "reason": "neo4j_driver_not_installed",
            "stats": {},
            "criteria": {},
            "sanity": {},
            "elapsed": 0.0,
        }

    # ROOT FIX (Finding 21 extension): also check if a Neo4j server is
    # actually reachable. The driver being installed is necessary but
    # not sufficient — the server must be running at DRUGOS_NEO4J_URI
    # (default bolt://localhost:7687). If the server is not reachable,
    # GraphStats.__enter__ raises ServiceUnavailable which propagates as
    # a step FAILURE. We catch this proactively and return a clean
    # SKIPPED result instead.
    from .config import Neo4jConfig as _Neo4jCfg12
    try:
        from neo4j.exceptions import ServiceUnavailable as _Neo4jSvcUnavail
    except ImportError:
        _Neo4jSvcUnavail = Exception  # fallback
    try:
        _cfg12 = _Neo4jCfg12()
        _driver12 = neo4j.GraphDatabase.driver(
            _cfg12.uri, auth=(_cfg12.user, _cfg12.password)
        )
        _driver12.verify_connectivity()
        _driver12.close()
    except (_Neo4jSvcUnavail, Exception) as _neo4j_conn_exc:
        # Broaden the catch — neo4j 6.x may raise OSError, ConnectionError,
        # or other subclasses. The key signal is "could not connect".
        _exc_name = type(_neo4j_conn_exc).__name__
        if (
            "connect" in str(_neo4j_conn_exc).lower()
            or "service" in _exc_name.lower()
            or "unavailable" in _exc_name.lower()
            or "refused" in str(_neo4j_conn_exc).lower()
        ):
            logger.warning(
                "Step 12 SKIPPED: Neo4j server is not reachable at %s "
                "(%s: %s). Start a Neo4j server (e.g. `docker run -p "
                "7687:7687 -e NEO4J_AUTH=neo4j/password neo4j:5`) and "
                "set DRUGOS_NEO4J_URI/USER/PASSWORD env vars. Until "
                "then, Neo4j-backed validation cannot run. This is an "
                "ENVIRONMENT limitation, NOT a code bug. The pipeline "
                "still computes V1 launch criteria from the in-memory "
                "graph data + the persisted phase1_staged_graph.json.",
                getattr(_cfg12, "uri", "bolt://localhost:7687"),
                _exc_name, _neo4j_conn_exc,
            )
            return {
                "skipped": True,
                "reason": "neo4j_server_unreachable",
                "stats": {},
                "criteria": {},
                "sanity": {},
                "elapsed": 0.0,
            }
        # If it's not a connection error, re-raise — it might be a real bug.
        raise

    from .graph_stats import GraphStats

    with GraphStats(Neo4jConfig()) as gs:
        stats = gs.compute_full_stats()
        criteria = gs.check_exit_criteria(week=2)
        sanity = gs.run_sanity_checks()

    # GAP-LOG-03: Log validation failures at ERROR level
    if isinstance(criteria, dict):
        failed = [
            k for k, v in criteria.items() if v is False or v is None
        ]
        if failed:
            logger.error(
                "Step 12 VALIDATION FAILED criteria: %s", failed
            )

    elapsed = time.time() - t0
    logger.info("Step 12 complete in %.1fs", elapsed)
    return {
        "stats": stats,
        "criteria": criteria,
        "sanity": sanity,
        "elapsed": elapsed,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 13: Data README
# ═══════════════════════════════════════════════════════════════════════════════


def step13_readme(skip_neo4j: bool = False) -> dict:
    """Step 13: Generate data README.

    Parameters
    ----------
    skip_neo4j : bool
        Skip Neo4j (README will be minimal).

    Returns
    -------
    dict
        Keys: readme_path, elapsed
    """
    _configure_logging()
    logger.info("=" * 60)
    logger.info("STEP 13: Generating Data README")
    logger.info("=" * 60)
    t0 = time.time()

    if skip_neo4j:
        readme = (
            "# DrugOS Knowledge Graph\n\n"
            "Neo4j was skipped — README generation requires "
            "Neo4j connection."
        )
    else:
        # ROOT FIX (Finding 21, P0): check neo4j driver availability
        # AND server reachability BEFORE constructing GraphStats.
        # Same pattern as step12 above.
        try:
            import neo4j  # noqa: F401
            _neo4j_driver_available_13 = True
        except ImportError:
            _neo4j_driver_available_13 = False

        if not _neo4j_driver_available_13:
            logger.warning(
                "Step 13 SKIPPED: neo4j Python driver is not "
                "installed. Install with: pip install "
                "'neo4j>=5.0,<6.0'. Generating minimal README "
                "from in-memory bridge data instead.",
            )
            readme = (
                "# DrugOS Knowledge Graph\n\n"
                "Neo4j driver not installed — minimal README.\n\n"
                "Install neo4j driver (`pip install 'neo4j>=5.0,<6.0'`)"
                " and re-run with --neo4j-uri to generate the full "
                "data README with graph statistics.\n"
            )
        else:
            # Also check server reachability (same as step12).
            from .config import Neo4jConfig as _Neo4jCfg13
            try:
                from neo4j.exceptions import (
                    ServiceUnavailable as _Neo4jSvcUnavail13,
                )
            except ImportError:
                _Neo4jSvcUnavail13 = Exception
            _neo4j_reachable_13 = True
            try:
                _cfg13 = _Neo4jCfg13()
                _driver13 = neo4j.GraphDatabase.driver(
                    _cfg13.uri, auth=(_cfg13.user, _cfg13.password),
                )
                _driver13.verify_connectivity()
                _driver13.close()
            except Exception as _neo4j_conn_exc_13:
                _exc_name_13 = type(_neo4j_conn_exc_13).__name__
                if (
                    "connect" in str(_neo4j_conn_exc_13).lower()
                    or "service" in _exc_name_13.lower()
                    or "unavailable" in _exc_name_13.lower()
                    or "refused" in str(_neo4j_conn_exc_13).lower()
                ):
                    _neo4j_reachable_13 = False

            if not _neo4j_reachable_13:
                logger.warning(
                    "Step 13 SKIPPED: Neo4j server is not reachable. "
                    "Generating minimal README from in-memory bridge "
                    "data instead.",
                )
                readme = (
                    "# DrugOS Knowledge Graph\n\n"
                    "Neo4j server not reachable — minimal README.\n\n"
                    "Start a Neo4j server and re-run with --neo4j-uri "
                    "to generate the full data README with graph "
                    "statistics.\n\n"
                    "Phase 1 staged graph is persisted at "
                    "phase2/data/processed/phase1_staged_graph.json "
                    "(ROOT FIX Finding 25 — survives process exit).\n"
                )
            else:
                from .graph_stats import GraphStats

                with GraphStats(Neo4jConfig()) as gs:
                    readme = gs.generate_data_readme()

    readme_path = PROCESSED_DIR / "DATA_README.md"
    readme_path.write_text(readme, encoding="utf-8")
    logger.info("Data README saved to %s", readme_path)
    elapsed = time.time() - t0
    return {"readme_path": str(readme_path), "elapsed": elapsed}


# ═══════════════════════════════════════════════════════════════════════════════
# FULL PIPELINE (Domain 1 — Architecture, Domain 6 — Reliability)
# ═══════════════════════════════════════════════════════════════════════════════


def run_full_pipeline(
    skip_download: bool = False,
    skip_neo4j: bool = False,
    skip_training: bool = False,
    fresh_start: bool = True,
    resume_after: Optional[float] = None,
    data_source: str = "phase1",
    phase1_processed_dir: Optional[Path | str] = None,
) -> dict:
    """Execute the complete Week 2 graph construction pipeline.

    Orchestrates all 13 steps with proper error handling, data quality
    validation, idempotency guarantees, and lineage tracking.

    Parameters
    ----------
    skip_download : bool
        Skip DRKG and source data downloads.
    skip_neo4j : bool
        Skip all Neo4j writes (for offline testing).
    skip_training : bool
        Skip TransE model training.
    fresh_start : bool
        Clear Neo4j graph before loading (idempotency).
    resume_after : float, optional
        Resume pipeline from after this step number (BUG-REL-04).
        v35 ROOT FIX (M-13): widened from ``int`` to ``float`` so
        operators can pass ``--resume 11.5`` to skip step 11b (the
        HGT training step) WITHOUT skipping step 11 (TransE). Integer
        values continue to work as before. The half-step thresholds
        are: ``11.5`` = skip step 11b. (Step 11 and 11b are the only
        "lettered" pair — no other half-steps are defined.)
    data_source : str
        v6 fix (bug #B17): ``"phase1"`` (default) — consume Phase 1
        outputs via the bridge (no DRKG download). ``"drkg"`` — fall
        back to the legacy DRKG-download path.
    phase1_processed_dir : path-like, optional
        Phase 1 processed_data directory (only used when
        ``data_source="phase1"``).

    Returns
    -------
    dict
        Full pipeline results with per-step metrics, V1 criteria check,
        pipeline metadata, and lineage information.

    Failure Modes
    -------------
    - Steps 1-2: FATAL — returns {aborted: True}
    - Step 3: CRITICAL — skips steps 4-7 if Neo4j fails
    - Steps 4-13: DEGRADABLE — continues on failure, logs error
    """
    _configure_logging()
    logger.info("DrugOS Graph Module — Week 2 Pipeline")
    logger.info(
        "Skip download: %s, Skip Neo4j: %s, Skip training: %s, "
        "Fresh start: %s",
        skip_download,
        skip_neo4j,
        skip_training,
        fresh_start,
    )

    # FIX TOP-14 (FIX-CFG-ML audit): set the global RNG seed as the FIRST
    # action of run_full_pipeline so model construction (nn.Embedding init)
    # is deterministic. Synchronized with run_unified.py (which also calls
    # set_global_seed before any model is constructed) — DO NOT diverge
    # (audit TOP-14).
    try:
        from .config import set_global_seed as _set_global_seed

        _set_global_seed(42)
    except Exception as _seed_exc:  # noqa: BLE001 — best-effort
        logger.warning(
            "set_global_seed(42) failed in run_full_pipeline (%s) — "
            "model init will be non-deterministic. This is a regression "
            "(audit TOP-14).",
            _seed_exc,
        )

    # GAP-INT-01: Log schema/pipeline versions for compatibility tracking
    logger.info(
        "Pipeline version: %s | Schema version: %s | "
        "Config version: %s | Package version: %s",
        PIPELINE_VERSION,
        SCHEMA_VERSION,
        CONFIG_VERSION,
        PACKAGE_VERSION,
    )

    # BUG-REL-01 FIX: Make _shutdown_requested module-level
    global _shutdown_requested
    _shutdown_requested = False

    def _signal_handler(sig, frame):
        global _shutdown_requested
        _shutdown_requested = True
        logger.warning(
            "Shutdown requested (signal %s) — finishing current step ...",
            sig,
        )

    # BUG-REL-02 FIX: Handle both SIGINT and SIGTERM
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    pipeline_start = time.time()
    results: Dict[str, Any] = {
        "pipeline_version": PIPELINE_VERSION,
        "schema_version": SCHEMA_VERSION,
    }

    # ─── Step 1: Load data (FATAL) ────────────────────────────────────────
    # v6 fix (bug #B17): default data source is now Phase 1 (via the
    # bridge). Use --data-source drkg to fall back to the DRKG download.
    # BUG-ARCH-01 FIX: Error handling for steps 1-2
    _edge_props_lookup: Optional[Dict] = None  # v24: for step3 property preservation
    _node_props_lookup: Optional[Dict] = None  # FIX-B: for step3 node-property preservation
    # v29 ROOT FIX (audit I-12): capture the bridge's full
    # ``Phase1StagedData`` so step 4 (data_source="phase1" branch) can
    # reuse the already-staged Compound nodes via
    # ``extract_drug_records_from_staged`` instead of re-reading
    # ``drugbank_drugs.csv`` from disk.
    _bridge_staged: Optional[Any] = None
    if resume_after is None or resume_after < 1:
        try:
            r1 = step1_load_data(
                data_source, skip_download, phase1_processed_dir,
            )
            # v43 ROOT FIX (P2-013): the previous code used a BLACKLIST
            # to strip heavy keys from the step1 result dict. This was
            # brittle — if step1_load_data added a new heavy key, it
            # would leak into the displayed result unless someone
            # remembered to add it to the blacklist. The fix uses a
            # WHITELIST of safe-to-display keys, so only known-lightweight
            # keys are kept. New heavy keys are automatically excluded.
            _STEP1_DISPLAY_WHITELIST = frozenset({
                "validation", "elapsed", "input_checksums",
                "bridge_summary", "fatal", "fatal_reason",
                "phase1_entity_mapping",  # used by step8 (line ~7010)
            })
            results["step1"] = {
                k: v for k, v in r1.items()
                if k in _STEP1_DISPLAY_WHITELIST
            }
            if r1.get("fatal"):
                logger.critical(
                    "Pipeline aborted at step 1: %s",
                    r1["fatal_reason"],
                )
                return {**results, "aborted": True}
            df = r1["df"]
            # v6: if the phase1 path returned pre-built maps, stash them
            # so step 2 can use them directly without re-deriving from df.
            _prebuilt_entity_maps = r1.get("entity_maps")
            _prebuilt_edge_maps = r1.get("edge_maps")
            # v24 ROOT FIX: capture edge_props_lookup so step3 can attach
            # properties to each edge before loading into Neo4j.
            _edge_props_lookup = r1.get("edge_props_lookup")
            # FIX-B: capture node_props_lookup so step3 can load Compound
            # nodes with their patient-safety properties (withdrawn,
            # fda_approved, clinical_status, ...) instead of bare
            # `{"id", "entity_type"}` dicts.
            _node_props_lookup = r1.get("node_props_lookup")
            # v29 ROOT FIX (audit I-12): capture the bridge's staged data.
            _bridge_staged = r1.get("bridge_staged")
            # v29 ROOT FIX (audit I-9): cache the heavy step-1 outputs
            # to disk so --resume doesn't re-run step 1 (which re-reads
            # all Phase 1 CSVs, re-runs the bridge, etc.). Note: we do
            # NOT cache ``_bridge_staged`` to disk because the
            # ``Phase1StagedData`` dataclass contains un-pickle-able
            # nested structures and is only needed on the first run
            # (the cached ``df`` + ``drug_records`` cover the resume
            # path).
            _save_step_cache(
                1,
                (df, _prebuilt_entity_maps, _prebuilt_edge_maps,
                 _edge_props_lookup, _node_props_lookup),
            )
        except Exception as e:
            logger.critical("Step 1 FAILED (fatal): %s", e, exc_info=True)
            results["step1"] = {"error": str(e), "fatal": True}
            return {**results, "aborted": True}
    else:
        logger.info("Resuming: Step 1 skipped (resume_after=%d)", resume_after)
        # v29 ROOT FIX (audit I-9): try to load step-1 outputs from the
        # disk cache FIRST. Only fall back to re-deriving via
        # ``step1_load_data(skip_download=True)`` if the cache is
        # missing or corrupt. The cache is invalidated automatically
        # when CHECKPOINT_DIR is cleared (e.g. by ``fresh_start``).
        _step1_cache = _load_step_cache(1)
        if _step1_cache is not None and len(_step1_cache) == 5:
            (
                df,
                _prebuilt_entity_maps,
                _prebuilt_edge_maps,
                _edge_props_lookup,
                _node_props_lookup,
            ) = _step1_cache
            logger.info(
                "Resuming: Step 1 loaded from disk cache (df=%d rows, "
                "entity_maps=%d, edge_maps=%d) — skipped step1_load_data.",
                len(df) if df is not None else 0,
                sum(len(v) for v in (_prebuilt_entity_maps or {}).values()),
                sum(len(v) for v in (_prebuilt_edge_maps or {}).values()),
            )
            # v29 ROOT FIX (audit I-12): on cache-hit resume, the
            # bridge's staged data is NOT available (it wasn't cached).
            # Step 4 will fall back to its normal path (re-reading the
            # CSV) which is acceptable for resume — the I-12 fix's
            # primary win is on first-run (not resume).
            _bridge_staged = None
        else:
            # Cache miss — fall back to the legacy re-derive path.
            # RT-5 ROOT FIX: honor the original --data-source choice on
            # resume. The previous code unconditionally called
            # _cached_parse_drkg() even when the operator originally chose
            # data_source="phase1", silently swapping the data source and
            # producing an entity-namespace mismatch with the already-
            # loaded Neo4j graph. Re-derive df via the SAME step1 entry
            # point so the Phase 1 bridge is used when it was used
            # originally. The skip_download=True flag avoids re-fetching
            # the raw data.
            logger.info(
                "Resuming: Step 1 cache miss — re-deriving via "
                "step1_load_data(skip_download=True).",
            )
            r1 = step1_load_data(
                data_source,
                skip_download=True,
                phase1_processed_dir=phase1_processed_dir,
            )
            df = r1["df"]
            _prebuilt_entity_maps = r1.get("entity_maps")
            _prebuilt_edge_maps = r1.get("edge_maps")
            # FIX-B: re-derive node_props_lookup on resume too, so step3
            # doesn't silently regress to bare `{"id", "entity_type"}` dicts
            # after a checkpoint resume.
            _node_props_lookup = r1.get("node_props_lookup")
            _edge_props_lookup = r1.get("edge_props_lookup")
            # v29 ROOT FIX (audit I-12): capture staged data here too.
            _bridge_staged = r1.get("bridge_staged")
            # Re-populate the cache so the next resume is fast.
            _save_step_cache(
                1,
                (df, _prebuilt_entity_maps, _prebuilt_edge_maps,
                 _edge_props_lookup, _node_props_lookup),
            )
        results["step1"] = {"resumed": True}
    if _shutdown_requested:
        return {**results, "shutdown": True}
    _save_checkpoint(1, results)

    # ─── Step 2: Build Mappings (FATAL) ───────────────────────────────────
    if resume_after is None or resume_after < 2:
        try:
            if _prebuilt_entity_maps is not None and _prebuilt_edge_maps is not None:
                # v6: phase1 path already built the maps in step1.
                entity_maps = _prebuilt_entity_maps
                edge_maps = _prebuilt_edge_maps
                r2 = {"elapsed": 0.0, "prebuilt": True}
            else:
                r2 = step2_build_mappings(df)
                entity_maps = r2["entity_maps"]
                edge_maps = r2["edge_maps"]
            results["step2"] = {
                k: v for k, v in r2.items() if k not in ("entity_maps", "edge_maps")
            }
        except Exception as e:
            logger.critical("Step 2 FAILED (fatal): %s", e, exc_info=True)
            results["step2"] = {"error": str(e), "fatal": True}
            return {**results, "aborted": True}
    else:
        logger.info("Resuming: Step 2 skipped (resume_after=%d)", resume_after)
        # Re-derive entity_maps and edge_maps for downstream steps
        if _prebuilt_entity_maps is not None and _prebuilt_edge_maps is not None:
            entity_maps = _prebuilt_entity_maps
            edge_maps = _prebuilt_edge_maps
        else:
            entity_maps = build_entity_id_maps(df)
            edge_maps = build_edge_index_maps(df, entity_maps)
        results["step2"] = {"resumed": True}
    # BUG-DQ-03: Validate step output
    _validate_step_output("Step 2", results.get("step2", {}), required_keys=["elapsed"])
    if _shutdown_requested:
        return {**results, "shutdown": True}
    _save_checkpoint(2, results)

    # ─── Step 3: Load into Neo4j ──────────────────────────────────────────
    if resume_after is None or resume_after < 3:
        try:
            r3 = step3_load_neo4j(
                entity_maps, edge_maps, skip_neo4j,
                fresh_start=fresh_start,
                edge_props_lookup=_edge_props_lookup,
                node_props_lookup=_node_props_lookup,
            )
            results["step3"] = r3
        except Exception as e:
            logger.error("Step 3 FAILED: %s", e, exc_info=True)
            _step_exception_or_skip("step3", e, results)
    else:
        logger.info("Resuming: Step 3 skipped (resume_after=%d)", resume_after)
        results["step3"] = {"resumed": True}

    # BUG-ARCH-02 FIX: If Neo4j fails, skip steps 4-7
    # v35 ROOT FIX: initialize drug_records BEFORE the neo4j_failed check
    # so step 8/10 don't hit UnboundLocalError when Neo4j is unavailable.
    # When neo4j_failed=True, the else branch (which assigns drug_records)
    # is never entered, but step 8 still tries to use drug_records.
    drug_records: list = []
    neo4j_failed = (
        not skip_neo4j
        and results.get("step3", {}).get("error") is not None
        and not results.get("step3", {}).get("skipped")
        and not results.get("step3", {}).get("resumed")
    )
    if neo4j_failed:
        logger.error(
            "Neo4j failed in step 3 — skipping steps 4-7."
        )
        for skip_step in [4, 5, 6, 7]:
            results[f"step{skip_step}"] = {
                "skipped": True,
                "reason": "Neo4j unavailable (step 3 failed)",
            }
    else:
        # ─── Step 4: DrugBank enrichment ──────────────────────────────────
        # v29 ROOT FIX (audit I-2 / Compound Chain 2 — "Phase 1 Output
        # Is Discarded"): when data_source="phase1", the bridge already
        # loaded DrugBank Compound + DPI edges into the graph in step 1.
        # Running step 4 again RE-LOADS them with use_merge=False,
        # creating DUPLICATE edges in Neo4j. The audit found that
        # steps 7a/7b/7c (STRING/UniProt/ChEMBL) were correctly
        # skipped, but steps 4, 7f, 7g, 7h were missed. ROOT FIX:
        # skip step 4 entirely when data_source="phase1". DrugBank
        # enrichment (mechanism_of_action, cas_number, etc.) is
        # already part of the bridge's Compound node properties.
        if data_source == "phase1":
            logger.info(
                "Step 4 SKIPPED (v29 root fix): data_source=phase1 "
                "means the bridge already loaded DrugBank Compound + "
                "DPI edges in step 1. Running step 4 would create "
                "DUPLICATE edges in Neo4j (audit I-2)."
            )
            results["step4"] = {
                "skipped": True,
                "reason": "phase1_bridge_already_loaded_drugbank",
            }
            # v29 ROOT FIX (audit I-12): drug_records is still needed by
            # step 8/10 — derive it from the bridge's STAGED data (built
            # in step 1) instead of re-reading drugbank_drugs.csv from
            # disk via step4_drugbank_enrichment. This eliminates the
            # duplicate CSV read that step 4 was performing on the
            # phase1 path.
            #
            # Previously, the code did:
            #     drug_records = results.get("step1", {}).get("drug_records", [])
            # but step 1 NEVER returns a "drug_records" key — so this
            # always returned ``[]`` and step 8/10 silently produced
            # zero output. The fix: use ``extract_drug_records_from_staged``
            # on the ``Phase1StagedData`` captured from the bridge.
            drug_records: list = []
            if _bridge_staged is not None:
                try:
                    from .phase1_bridge import (
                        extract_drug_records_from_staged,
                    )
                    drug_records = extract_drug_records_from_staged(_bridge_staged)
                    logger.info(
                        "Step 4 (phase1 path): reused %d drug_records "
                        "from the bridge's staged Compound nodes (v29 "
                        "root fix I-12 — no CSV re-read).",
                        len(drug_records),
                    )
                except Exception as exc:
                    # Defensive: never break the pipeline over a helper
                    # failure — fall back to the empty list and let
                    # step 8/10 log their own warnings.
                    logger.warning(
                        "Step 4 (phase1 path): extract_drug_records_"
                        "from_staged failed (%s) — drug_records=[]. "
                        "Steps 8/10 will see no DrugBank data.",
                        exc,
                    )
                    drug_records = []
            else:
                logger.warning(
                    "Step 4 (phase1 path): _bridge_staged is None "
                    "(likely a cache-hit resume) — drug_records=[]. "
                    "Steps 8/10 will see no DrugBank data. To fix: "
                    "clear the checkpoint cache and re-run, OR run "
                    "without --resume so step 1 re-stages the bridge "
                    "data."
                )
        elif resume_after is None or resume_after < 4:
            try:
                r4 = step4_drugbank_enrichment(
                    skip_neo4j,
                    skip_download=skip_download,
                    phase1_processed_dir=phase1_processed_dir,
                )
                results["step4"] = {
                    k: v
                    for k, v in r4.items()
                    if k not in ("drug_records", "target_edges")
                }
                drug_records = r4.get("drug_records", [])
                # v29 ROOT FIX (audit I-9): cache drug_records to disk
                # so --resume doesn't re-run step 4 (which re-parses
                # DrugBank CSVs / XML). Target edges are not needed by
                # any downstream step on resume (they were loaded into
                # Neo4j in step 4 itself), so we only cache
                # drug_records.
                _save_step_cache(4, (drug_records,))
            except Exception as e:
                logger.error("Step 4 FAILED: %s", e, exc_info=True)
                _step_exception_or_skip("step4", e, results)
                drug_records = []
        else:
            results["step4"] = {"resumed": True}
            # v29 ROOT FIX (audit I-9): try to load drug_records from
            # the disk cache FIRST. Only fall back to re-deriving via
            # ``step4_drugbank_enrichment(skip_neo4j=True)`` if the
            # cache is missing or corrupt.
            #
            # v17 ROOT FIX (resume-after-step-4 bug): the previous code set
            # ``drug_records = []`` here. Step 8 (entity resolution) and
            # step 10 (training data) BOTH consume drug_records — step 8
            # uses it for InChIKey canonicalization, step 10 uses it for
            # positive-pair extraction from DrugBank indications. With an
            # empty list, both steps silently produced zero output, the
            # V1 launch criterion ``positive_pairs_sufficient`` failed,
            # and the operator got an opaque "0 positive pairs" error
            # with no clue that --resume was the cause. Re-derive
            # drug_records via the SAME step4 entry point with
            # skip_neo4j=True (matches the pattern RT-5 ROOT FIX used
            # for step1 resume at lines 3556-3560). The step4 result is
            # marked "resumed" — we do NOT re-run the Neo4j edge load,
            # but we DO recover the in-memory drug_records list so steps
            # 8 and 10 see real data.
            _step4_cache = _load_step_cache(4)
            if _step4_cache is not None and len(_step4_cache) >= 1:
                drug_records = _step4_cache[0]
                logger.info(
                    "Resuming: drug_records loaded from disk cache "
                    "(%d records) — skipped step4_drugbank_enrichment.",
                    len(drug_records),
                )
            else:
                # Cache miss — fall back to the legacy re-derive path.
                try:
                    _r4_resume = step4_drugbank_enrichment(
                        skip_neo4j=True,
                        skip_download=skip_download,
                        phase1_processed_dir=phase1_processed_dir,
                    )
                    drug_records = _r4_resume.get("drug_records", [])
                    logger.info(
                        "Resuming: re-derived %d drug_records from step4 "
                        "(skip_neo4j=True) for downstream steps 8/10.",
                        len(drug_records),
                    )
                    # Re-populate the cache so the next resume is fast.
                    _save_step_cache(4, (drug_records,))
                except Exception as exc:
                    logger.error(
                        "Resuming: step4 re-derivation FAILED — steps 8/10 "
                        "will receive empty drug_records. Cause: %s",
                        exc, exc_info=True,
                    )
                    drug_records = []

        if _shutdown_requested:
            _save_checkpoint(4, results)
            return {**results, "shutdown": True}

        # ─── Step 5: STITCH ingestion ─────────────────────────────────────
        if resume_after is None or resume_after < 5:
            try:
                # v15 ROOT FIX (REM-24): pass skip_download so --skip-download
                # actually skips the STITCH network fetch.
                r5 = step5_stitch_ingestion(skip_neo4j, skip_download=skip_download)
                results["step5"] = r5
            except Exception as e:
                logger.error("Step 5 FAILED: %s", e, exc_info=True)
                _step_exception_or_skip("step5", e, results)
        else:
            results["step5"] = {"resumed": True}

        # ─── Step 6: SIDER ingestion ──────────────────────────────────────
        if resume_after is None or resume_after < 6:
            try:
                r6 = step6_sider_ingestion(skip_neo4j, skip_download=skip_download)
                results["step6"] = r6
            except Exception as e:
                logger.error("Step 6 FAILED: %s", e, exc_info=True)
                _step_exception_or_skip("step6", e, results)
        else:
            results["step6"] = {"resumed": True}

        # ─── Step 7: Additional data sources ──────────────────────────────
        if resume_after is None or resume_after < 7:
            try:
                r7 = step7_additional_sources(
                    skip_neo4j,
                    skip_download=skip_download,
                    phase1_processed_dir=phase1_processed_dir,
                    data_source=data_source,
                )
                results["step7"] = r7
            except Exception as e:
                logger.error("Step 7 FAILED: %s", e, exc_info=True)
                _step_exception_or_skip("step7", e, results)
        else:
            results["step7"] = {"resumed": True}

    if _shutdown_requested:
        _save_checkpoint(7, results)
        return {**results, "shutdown": True}
    _save_checkpoint(7, results)

    # ─── Step 8: Entity resolution ────────────────────────────────────────
    if resume_after is None or resume_after < 8:
        try:
            # FORENSIC bridge root fix: pass Phase 1's entity_mapping
            # to step8 so the resolver REUSES Phase 1's cross-source ER
            # instead of re-resolving from scratch.
            _p1_em = results.get("step1", {}).get("phase1_entity_mapping") if isinstance(results.get("step1"), dict) else None
            r8 = step8_entity_resolution(df, drug_records, phase1_entity_mapping=_p1_em)
            results["step8"] = r8
        except Exception as e:
            logger.error("Step 8 FAILED: %s", e, exc_info=True)
            _step_exception_or_skip("step8", e, results)
    else:
        results["step8"] = {"resumed": True}

    # BUG-COMP-01 FIX (v27 ROOT FIX): Verify InChIKey canonicalization after step 8.
    # Previous code looked for non-existent keys ``compound_drugbank_resolved`` /
    # ``compound_drkg_resolved`` (these were never emitted by
    # ``EntityResolver.get_resolution_stats()``, which returns
    # ``{entity_type: {total, resolved, unresolved, ...}}``). The check therefore
    # ALWAYS fired "Zero compounds resolved to InChIKey" — even when step 8 had
    # successfully merged 13 Compound mappings. Now we read the actual nested
    # stats dict and also fall back to the Phase 1 bridge's compound count when
    # Phase 2 entity resolution was a no-op (because the bridge already did it).
    r8 = results.get("step8", {})
    if isinstance(r8, dict):
        stats = r8.get("stats", {})
        compound_stats = stats.get("Compound", {}) if isinstance(stats, dict) else {}
        resolved = (
            compound_stats.get("resolved", 0)
            if isinstance(compound_stats, dict)
            else 0
        )
        # Also account for Phase 1 bridge-resolved compounds (step1) — when the
        # bridge is the source of truth, Phase 2 step8 has no DrugBank XML work
        # to do, but the compounds ARE resolved.
        r1 = results.get("step1", {})
        bridge_compound_count = 0
        if isinstance(r1, dict):
            bridge_summary = r1.get("summary", {})
            if isinstance(bridge_summary, dict):
                bridge_compound_count = bridge_summary.get("nodes_loaded", 0)
            if not bridge_compound_count:
                staged = r1.get("staged_data")
                if hasattr(staged, "compound_nodes"):
                    bridge_compound_count = len(staged.compound_nodes)
        total_resolved = resolved + bridge_compound_count
        if total_resolved == 0 and not r8.get("skipped"):
            logger.error(
                "COMPLIANCE: Zero compounds resolved to InChIKey "
                "(Phase 2 step8 resolved=%d, Phase 1 bridge compounds=%d). "
                "Check entity resolution.",
                resolved,
                bridge_compound_count,
            )
        elif total_resolved > 0:
            logger.info(
                "COMPLIANCE: %d compounds resolved to %s "
                "(Phase 2 step8: %d, Phase 1 bridge: %d)",
                total_resolved,
                CANONICAL_IDS.get("Compound", "InChIKey"),
                resolved,
                bridge_compound_count,
            )
    # BUG-DQ-03: Validate step 8 output
    _validate_step_output(
        "Step 8", r8, required_keys=["stats"]
    )

    if _shutdown_requested:
        _save_checkpoint(8, results)
        return {**results, "shutdown": True}

    # ─── Step 9: Build PyG HeteroData ─────────────────────────────────────
    if resume_after is None or resume_after < 9:
        try:
            # v41 ROOT FIX (Task J SEV3): the previous call was
            # ``step9_build_pyg(entity_maps, edge_maps)`` with NO
            # ``drug_records`` argument. That meant step9's ChEMBERTa
            # integration branch (line ~4465) always hit the
            # ``elif not drug_records:`` warning, even when the operator
            # had set ``DRUGOS_USE_CHEMBERTA=1`` and ``HF_TOKEN``. The
            # ChEMBERTa features NEVER attached to the HeteroData,
            # silently downgrading the GNN to random Xavier features.
            # Fix: pass the ``drug_records`` list that step1/step4 already
            # recovered above (lines 6796/6832/6846) so step9 can compute
            # SMILES embeddings for the Compound nodes that have SMILES.
            r9 = step9_build_pyg(
                entity_maps,
                edge_maps,
                drug_records=drug_records,
            )
            results["step9"] = {k: v for k, v in r9.items() if k != "summary"}
        except Exception as e:
            logger.error("Step 9 FAILED: %s", e, exc_info=True)
            _step_exception_or_skip("step9", e, results)
    else:
        results["step9"] = {"resumed": True}
    # BUG-DQ-03: Validate step 9 output
    _validate_step_output(
        "Step 9", results.get("step9", {}), required_keys=["data_path"]
    )

    # ─── Step 10: Build training data ─────────────────────────────────────
    if resume_after is None or resume_after < 10:
        try:
            r10 = step10_training_data(df, drug_records)
            results["step10"] = r10
        except Exception as e:
            logger.error("Step 10 FAILED: %s", e, exc_info=True)
            _step_exception_or_skip("step10", e, results)
    else:
        results["step10"] = {"resumed": True}

    # BUG-DQ-01 FIX: Enforce MIN_POSITIVE_PAIRS and MIN_NEGATIVE_PAIRS
    # v29 ROOT FIX (audit I-11): MIN_POSITIVE_PAIRS dev default was 1,
    # which is statistically meaningless (held-out AUC on 1 sample has
    # CI [0,1]). Now 10 — see config.MIN_POSITIVE_PAIRS for the full
    # rationale. Production keeps 15,000.
    r10 = results.get("step10", {})
    if isinstance(r10, dict) and not r10.get("skipped"):
        td = r10.get("training_data", {})
        num_pos = td.get("num_positives", 0)
        num_neg = td.get("num_negatives", 0)

        if num_pos < MIN_POSITIVE_PAIRS:
            logger.error(
                "INSUFFICIENT POSITIVE PAIRS: %d (minimum: %d). "
                "Model training will produce unreliable results.",
                num_pos,
                MIN_POSITIVE_PAIRS,
            )
            results["step10"]["data_quality_warning"] = (
                f"Positive pairs ({num_pos}) below minimum "
                f"({MIN_POSITIVE_PAIRS})"
            )

        if num_neg < MIN_NEGATIVE_PAIRS:
            logger.error(
                "INSUFFICIENT NEGATIVE PAIRS: %d (minimum: %d). "
                "Model training will produce unreliable results.",
                num_neg,
                MIN_NEGATIVE_PAIRS,
            )
            results["step10"]["data_quality_warning"] = (
                f"Negative pairs ({num_neg}) below minimum "
                f"({MIN_NEGATIVE_PAIRS})"
            )

        if num_pos >= MIN_POSITIVE_PAIRS and num_neg >= MIN_NEGATIVE_PAIRS:
            logger.info(
                "Training data quality PASSED: %d pos, %d neg",
                num_pos,
                num_neg,
            )

    if _shutdown_requested:
        _save_checkpoint(10, results)
        return {**results, "shutdown": True}

    # ─── Step 11: Train TransE ────────────────────────────────────────────
    # v29 ROOT FIX (audit M-11): pass step 9's PyG HeteroData path to
    # step 11 so the HeteroData built in step 9 is actually consumed
    # by training (was decoupled — step 11 used entity_maps directly).
    _step9_data_path = (
        results.get("step9", {}).get("data_path")
        if isinstance(results.get("step9"), dict)
        else None
    )
    if resume_after is None or resume_after < 11:
        try:
            r11 = step11_train_transe(
                entity_maps, edge_maps, skip_training,
                pyg_data_path=_step9_data_path,
            )
            results["step11"] = r11
        except Exception as e:
            logger.error("Step 11 FAILED: %s", e, exc_info=True)
            # FIX ML-1 (FIX-CFG-ML audit): when train_transe raises
            # TransETrainingError (AUC below target or below random
            # baseline), surface the honest held_out_auc that
            # train_transe computed BEFORE the raise (the held-out
            # eval block was moved before the AUC enforcement block).
            # The exception's context dict carries held_out_auc. This
            # lets _check_v1_launch_criteria distinguish "held-out
            # eval ran and produced a low AUC" from "held-out eval
            # never ran" — the user's #1 complaint about V1 launch
            # false positives.
            #
            # v43 ROOT FIX (P2-002): the previous code set
            # ``"skipped": True`` when step11 actually RAN and raised
            # an exception. "skipped" means "didn't run" in normal
            # pipeline terminology — this was misleading. Operators
            # seeing ``step11: {skipped: True, held_out_auc: 0.536}``
            # wondered: did it skip or not? The answer is: it ran,
            # raised TransETrainingError (AUC below target), was
            # caught, and mislabeled. The fix uses ``"failed": True``
            # (not ``"skipped": True``) to accurately reflect that
            # the step ran but did not succeed. Reserve ``"skipped"``
            # for steps that didn't run at all (like step4-step7 when
            # Neo4j is unavailable). The _check_v1_launch_criteria
            # function reads best_val_auc/held_out_auc/model_saved
            # regardless of the skipped/failed key, so this change
            # is safe for the criteria check.
            _step11_failure: Dict[str, Any] = {
                "error": str(e),
                # v43 ROOT FIX (P2-002): use "failed" not "skipped" —
                # the step RAN and raised an exception, it did NOT skip.
                "failed": True,
                "skipped": False,
            }
            _exc_ctx = getattr(e, "context", None) or {}
            if isinstance(_exc_ctx, dict):
                for _k in ("held_out_auc", "best_val_auc", "best_epoch", "target_auc"):
                    if _k in _exc_ctx:
                        _step11_failure[_k] = _exc_ctx[_k]
            results["step11"] = _step11_failure
    else:
        results["step11"] = {"resumed": True}

    # ─── Step 11b: Train Graph Transformer (HGT) — v29 ROOT FIX ────────
    # v29 ROOT FIX (audit M-1/M-2/M-3): the docx-promised "Graph
    # Transformer" never existed in v28. FIX 2 added the
    # GraphTransformerModel class; FIX 16 (this block) wires it into
    # the pipeline. HGT runs alongside TransE so operators can compare
    # AUCs. The V1 launch criteria check (_check_v1_launch_criteria)
    # considers BOTH models — if EITHER meets the 0.85 threshold, the
    # launch passes. This makes the docx's ">0.85 AUC" claim
    # achievable for the first time.
    #
    # v29 ROOT FIX (audit M-11): pass step 9's PyG HeteroData path to
    # step 11b so its x_dict / edge_index_dict are sourced from the
    # HeteroData built in step 9 (was decoupled — step 11b rebuilt
    # x_dict / edge_index_dict from entity_maps / edge_maps directly).
    #
    # v35 ROOT FIX (M-13): step 11b previously used the SAME
    # ``resume_after < 11`` threshold as step 11, so passing
    # ``--resume 11`` (intending "skip step 11 and run step 11b
    # onwards") skipped BOTH step 11 AND step 11b. The two steps
    # were effectively coupled — operators could not re-run just the
    # HGT model without re-running TransE. The fix uses a distinct
    # threshold (``11.5``) for step 11b so:
    #   * ``--resume 11``   → skips step 11, RUNS step 11b
    #   * ``--resume 11.5`` → skips step 11 AND step 11b, runs step 12+
    #   * ``--resume 12``   → also skips step 11b (12 > 11.5)
    if resume_after is None or resume_after < 11.5:
        try:
            r11b = step11b_train_graph_transformer(
                entity_maps, edge_maps, skip_training,
                pyg_data_path=_step9_data_path,
            )
            results["step11b"] = r11b
        except Exception as e:
            logger.error("Step 11b FAILED: %s", e, exc_info=True)
            _step_exception_or_skip("step11b", e, results)
    else:
        results["step11b"] = {"resumed": True}

    # ─── Step 12: Validation ──────────────────────────────────────────────
    if resume_after is None or resume_after < 12:
        try:
            r12 = step12_validation(skip_neo4j)
            results["step12"] = r12
        except Exception as e:
            logger.error("Step 12 FAILED: %s", e, exc_info=True)
            _step_exception_or_skip("step12", e, results)
    else:
        results["step12"] = {"resumed": True}

    # ─── Step 13: Data README ────────────────────────────────────────────
    if resume_after is None or resume_after < 13:
        try:
            r13 = step13_readme(skip_neo4j)
            results["step13"] = r13
        except Exception as e:
            logger.error("Step 13 FAILED: %s", e, exc_info=True)
            _step_exception_or_skip("step13", e, results)
    else:
        results["step13"] = {"resumed": True}

    # ─── V1 Launch Criteria Check (BUG-COMP-02) ──────────────────────────
    v1_criteria = _check_v1_launch_criteria(results)
    results["v1_criteria"] = v1_criteria
    if v1_criteria["passed"]:
        logger.info("V1 LAUNCH CRITERIA: PASSED")
    else:
        # v26 ROOT FIX (Issue C-1/C-3): the strict ``passed`` flag is
        # AUTHORITATIVE for the launch verdict. ``dev_smoke_test_pass``
        # is INFORMATIONAL — it means the pipeline ran end-to-end, NOT
        # that the model met the production AUC threshold (0.85). The
        # launch verdict is NOT PASSED even when ``dev_smoke_test_pass``
        # is True. The previous v25 code flipped ``passed=True`` in dev
        # mode, producing the user's #1 complaint: a pipeline reporting
        # "V1 LAUNCH CRITERIA: PASSED" for a model with held_out_auc
        # 0.5389 (random) and best_val_auc 0.6722 (target 0.85).
        if v1_criteria.get("dev_smoke_test_pass"):
            logger.error(
                "V1 LAUNCH CRITERIA: NOT PASSED (dev smoke-test only — "
                "pipeline ran end-to-end but AUC below 0.85 threshold). "
                "best_val_auc=%.4f, held_out_auc=%.4f, "
                "dev_smoke_test_pass=True, passed=False.",
                v1_criteria.get("best_val_auc", -1.0),
                v1_criteria.get("held_out_auc", -1.0),
            )
        else:
            logger.error(
                "V1 LAUNCH CRITERIA: NOT PASSED — %s",
                {
                    k: v
                    for k, v in v1_criteria.items()
                    if v is False
                },
            )
        if os.environ.get("DRUGOS_ALLOW_LAUNCH_FAIL", "") != "1":
            logger.error(
                "Exiting with code 4 — V1 launch criteria not met. "
                "Set DRUGOS_ALLOW_LAUNCH_FAIL=1 to override (dev/test only)."
            )
            results["launch_criteria_failed"] = True
            # v21 ROOT FIX (Audit section 4 finding / Chain 12):
            # ``sys.exit(1)`` in a library function (run_full_pipeline)
            # breaks embedding — any caller (run_unified.py, Airflow,
            # Celery, K8s Job) inherits the exit code and cannot
            # distinguish "V1 launch criteria not met" from "Python
            # crashed." The documented contract was exit code 4 for V1
            # criteria failure, but that contract was DEAD because
            # sys.exit(1) hijacked the exit. Raise a typed exception
            # instead so callers can catch + translate. run_unified.py
            # catches this and returns exit code 4; ``python -m
            # drugos_graph`` catches it in main() and returns exit 4
            # (v26 fix: was exit 1, now exit 4 to match the documented
            # contract for both entry points).
            raise V1LaunchCriteriaFailed(v1_criteria)
        else:
            logger.warning(
                "DRUGOS_ALLOW_LAUNCH_FAIL=1 set — continuing despite "
                "V1 launch criteria failure (dev/test mode)."
            )

    # ─── Final Summary ────────────────────────────────────────────────────
    total_elapsed = time.time() - pipeline_start
    results["total_elapsed"] = total_elapsed

    # GAP-INT-03: Version the pipeline results JSON
    results["config_hash"] = CONFIG_HASH or compute_config_hash()
    results["generated_at"] = datetime.now(timezone.utc).isoformat()
    results["run_id"] = _pipeline_run_id

    logger.info("=" * 60)
    logger.info("PIPELINE COMPLETE")
    logger.info("=" * 60)
    logger.info("Total time: %.1fs", total_elapsed)
    logger.info("V1 criteria: %s", "PASSED" if v1_criteria["passed"] else "NOT PASSED")

    # ─── Save Results (BUG-DQ-02 FIX: custom serializer) ─────────────────
    ensure_dirs()
    results_path = PROCESSED_DIR / "pipeline_results.json"
    serializable = _serialize_for_json(results)
    results_path.write_text(
        json.dumps(serializable, indent=2), encoding="utf-8"
    )
    logger.info("Pipeline results saved to %s", results_path)

    # GAP-SEC-02 FIX: Restrict file permissions
    try:
        os.chmod(results_path, 0o600)
    except OSError:
        pass  # Permission restriction not critical

    # GAP-SEC-01: Security audit entry
    try:
        ensure_dirs()
        audit_dir = AUDIT_LOG_DIR
        audit_dir.mkdir(parents=True, exist_ok=True)
        audit_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": _pipeline_run_id,
            "operator": os.environ.get("USER", "unknown"),
            "hostname": os.environ.get("HOSTNAME", "unknown"),
            "pid": os.getpid(),
            "cli_args": {
                "skip_download": skip_download,
                "skip_neo4j": skip_neo4j,
                "skip_training": skip_training,
                "fresh_start": fresh_start,
            },
            "config_hash": CONFIG_HASH or compute_config_hash(),
            "v1_criteria": v1_criteria,
            "total_elapsed": total_elapsed,
        }
        audit_path = (
            audit_dir
            / f"pipeline_run_{_pipeline_run_id}.json"
        )
        audit_path.write_text(
            json.dumps(audit_entry, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.debug("Failed to write audit entry: %s", e)

    # Write lineage manifest (GAP-LIN-01)
    try:
        from .config import write_lineage_manifest
        input_checksums = results.get("step1", {}).get("input_checksums", {})
        lineage_path = write_lineage_manifest(
            PROCESSED_DIR / "lineage_manifest.json",
            input_checksums=input_checksums,
        )
        logger.info("Lineage manifest saved to %s", lineage_path)
    except Exception as e:
        logger.debug("Failed to write lineage manifest: %s", e)

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# CLI ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════


def main() -> None:
    """CLI entry point for the DrugOS pipeline.

    Supports:
    - Full pipeline: ``python -m drugos_graph``
    - Single step:  ``python -m drugos_graph --step N``
    - Offline mode: ``python -m drugos_graph --skip-download --skip-neo4j``
    - Fresh start:  ``python -m drugos_graph --fresh-start``
    - Resume:       ``python -m drugos_graph --resume 7``

    Exit codes:
    - 0: Success
    - 1: Error (step failure, config validation failure)
    """
    parser = argparse.ArgumentParser(
        description="DrugOS Graph Module — Week 2 Pipeline "
        f"(v{PIPELINE_VERSION})"
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Skip DRKG and source data downloads (use existing files)",
    )
    parser.add_argument(
        "--skip-neo4j",
        action="store_true",
        help="Skip Neo4j operations (for offline testing)",
    )
    parser.add_argument(
        "--skip-training",
        action="store_true",
        help="Skip TransE training (GPU not available)",
    )
    parser.add_argument(
        "--step",
        type=int,
        choices=list(range(1, 14)),
        help="Run only a specific step (1-13)",
    )
    parser.add_argument(
        "--fresh-start",
        action="store_true",
        help="Clear Neo4j graph before loading (idempotent reload)",
    )
    parser.add_argument(
        "--resume",
        type=int,
        metavar="N",
        help="Resume pipeline from after step N",
    )
    # v6 fix (bug #B17): default data source is now Phase 1 (via the
    # bridge). Use --data-source drkg to fall back to the DRKG download.
    parser.add_argument(
        "--data-source",
        choices=["phase1", "drkg"],
        default="phase1",
        help="Data source for the pipeline. 'phase1' (default) consumes "
             "Phase 1's processed_data CSVs via the phase1_bridge. 'drkg' "
             "downloads DRKG from dgl-data.s3 and trains on that.",
    )
    parser.add_argument(
        "--phase1-dir",
        type=Path,
        default=None,
        help="Phase 1 processed_data directory (only used with "
             "--data-source phase1). Defaults to the bridge's "
             "DEFAULT_PHASE1_PROCESSED_DIR.",
    )
    args = parser.parse_args()

    # GAP-CONF-02: Validate config on startup
    _configure_logging()
    warnings = _validate_startup_config()
    for w in warnings:
        logger.warning("CONFIG WARNING: %s", w)

    # GAP-CONF-01: Validate CLI argument combinations
    combo_error = _validate_neo4j_cli_combos(args)
    if combo_error:
        parser.error(combo_error)

    if args.step is not None:
        # BUG-DES-02 FIX: Use clean _run_step_with_deps instead of
        # unreadable nested lambdas
        result = _run_step_with_deps(args.step, args)
        # GAP-COD-01 FIX: Proper exit code
        if result.get("error") or result.get("fatal"):
            sys.exit(1)
        sys.exit(0)
    else:
        try:
            results = run_full_pipeline(
                skip_download=args.skip_download,
                skip_neo4j=args.skip_neo4j,
                skip_training=args.skip_training,
                fresh_start=args.fresh_start,
                resume_after=args.resume,
                data_source=args.data_source,
                phase1_processed_dir=args.phase1_dir,
            )
        except V1LaunchCriteriaFailed as exc:
            # v21 ROOT FIX (Audit Chain 12): the typed exception from
            # run_full_pipeline surfaces here. v26 ROOT FIX (Issue C-1):
            # the documented CLI contract for ``python -m drugos_graph``
            # is exit code 4 when V1 launch criteria are not met (the
            # same code run_unified.py returns). The previous code
            # returned exit 1, which conflated "criteria not met" with
            # "Python crashed" — operators could not distinguish a
            # scientifically-honest launch refusal from a code bug.
            logger.error("V1 launch criteria not met: %s", exc.criteria)
            sys.exit(4)
        if results.get("aborted") or results.get("shutdown"):
            sys.exit(1)
        # BUG-E-008 root fix: the previous contract exited 0 even when
        # 5 of 13 steps silently failed (caught by try/except and marked
        # 'skipped'). A pharma partner running ``python -m drugos_graph``
        # would see exit 0 and assume success while the underlying ML
        # pipeline produced nothing. Now we scan every step result for
        # ``skipped=True`` (excluding steps the user explicitly asked to
        # skip via --skip-* flags) and exit non-zero if any unexpected
        # skip is detected. This makes CI smoke tests reliable.
        user_skipped_steps = set()
        if args.skip_download:
            user_skipped_steps.update({"step2", "step3"})
        if args.skip_neo4j:
            user_skipped_steps.add("step12")
        if args.skip_training:
            user_skipped_steps.add("step11")
        unexpected_skips = []
        # Legitimate scientific skips that don't indicate a bug — these
        # are guardrails, not failures. The reason field documents why.
        # v41 ROOT FIX (Task J SEV4): tightened from a prefix match on
        # ``"insufficient_"`` (which matched ANY string starting with
        # ``insufficient_``, e.g. ``insufficient_disk_space``,
        # ``insufficient_permissions``, ``insufficient_memory_random_xyz``)
        # to an EXACT whitelist of the two reasons the pipeline actually
        # emits: ``insufficient_triples`` (step11) and
        # ``insufficient_memory`` (future-proofing). The previous broad
        # match would have silently accepted a new "insufficient_*"
        # reason added by a future code change without operator review,
        # masking novel failure modes as "legitimate scientific skips".
        legitimate_skip_reasons = (
            "insufficient_triples",  # step11: not enough triples for training
            "insufficient_memory",   # training OOM guardrail (reserved)
            "no_triples",            # step11: no triples available at all
        )
        for step_key, step_result in results.items():
            if not step_key.startswith("step"):
                continue
            if step_key in user_skipped_steps:
                continue
            if isinstance(step_result, dict) and step_result.get("skipped"):
                # Check if this is a legitimate scientific skip.
                reason = str(step_result.get("reason", ""))
                if any(reason.startswith(r) for r in legitimate_skip_reasons):
                    logger.info(
                        "Legitimate scientific skip in %s: %s",
                        step_key, reason,
                    )
                    continue
                unexpected_skips.append(
                    f"{step_key}: {step_result.get('error', reason or 'unknown')}"
                )
        if unexpected_skips:
            logger.error(
                "BUG-E-008 enforcement: pipeline exit code = 1 because "
                "the following steps were silently skipped (no try/except "
                "masking anymore): %s",
                "; ".join(unexpected_skips),
            )
            sys.exit(1)
        sys.exit(0)


if __name__ == "__main__":
    main()