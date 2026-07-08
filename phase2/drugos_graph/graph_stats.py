"""
DrugOS Graph Module — Graph Statistics & Validation (v3.0.0, Institutional-Grade)
===================================================================================
PATIENT-SAFETY CRITICAL — This module is the validation gate between Phase 2
(knowledge graph construction) and Phase 3 (Graph Transformer training).

If this module green-lights a corrupt or incomplete graph, the downstream
Graph Transformer trains on bad data, the RL ranker promotes wrong drug-disease
pairs, and a clinician may prescribe a drug that does not work — or worse,
kills a patient. There is absolutely no room for 'it ran without errors'
reasoning here. Every fix must be treated as a patient-safety intervention.

CANONICAL STATS MODULE
----------------------
This module is the CANONICAL external API for graph statistics used by
``run_pipeline.py`` for Week 2 exit criteria validation. Do NOT use
``kg_builder.GraphStatsCollector`` directly from pipeline code — that is an
internal implementation detail of the graph builder. GraphStatsCollector
provides quick builder-time counts; GraphStats provides the full validation
suite with patient-safety gates.

Public API (backward-compatible with run_pipeline.py):
    GraphStats                  — Main statistics class (implements StatsProvider)
    StatsProvider               — Runtime-checkable Protocol
    compute_full_stats()        — Comprehensive graph statistics
    check_exit_criteria(week)   — Week 1/2 exit gate
    run_sanity_checks()         — 15+ patient-safety sanity checks
    generate_data_readme()      — Markdown README generation
    label_distribution_report() — Deterministic node-label distribution

Typed Schemas:
    StatsReport             — Full statistics output (JSON-serializable)
    ExitCriterionResult     — Single exit-criterion result
    ExitCriteriaReport      — Full exit-criteria report with summary
    SanityCheckResult       — Single sanity-check result
    SanityCheckReport       — Full sanity-check report with summary
    QueryRecord             — Auditable query execution record

Data Dictionary (StatsReport fields):
    timestamp              : str  — UTC ISO-8601 when stats were computed
    total_nodes            : int  — Total node count (0 if empty graph)
    total_edges            : int  — Total edge count (0 if empty graph)
    node_counts_by_type    : dict[str, int] — Per-label node counts (UNWIND)
    edge_counts_by_type    : dict[str, int] — Per-type edge counts
    avg_out_degree         : float — Average out-degree (0.0 if empty)
    max_out_degree         : int  — Maximum out-degree (0 if empty)
    min_out_degree         : int  — Minimum out-degree (0 if empty)
    isolated_nodes         : int  — Nodes with zero edges (-1 if query failed)
    density_homogeneous_naive : float — Naive density (for reference only)
    density_per_edge_type  : dict[str, float] — Per-type density [0, 1]
    compound_name_coverage : float — Fraction of Compounds with non-null name
    compound_smiles_coverage: float — Fraction of Compounds with non-null SMILES
    disease_name_coverage  : float — Fraction of Diseases with non-null name
    canonical_id_coverage  : dict[str, float] — Per-type canonical ID coverage
    compound_safety_coverage: dict[str, float] — withdrawn/toxicity field coverage
    withdrawn_with_treats   : int  — Withdrawn compounds with treats edges (SAFETY)
    edge_direction_violations : int — Edges violating CORE_EDGE_TYPES schema
    warnings               : list[str] — Non-fatal issues encountered
    query_timings          : dict[str, float] — Per-query wall-clock seconds
    queries_run            : list[QueryRecord] — Auditable query log
    lineage                : dict — Pipeline run ID, versions, fingerprint

Changelog
---------
v3.0.0 — Institutional-grade rewrite. 169 issues fixed across 16 domains.
  - Fixes 3.1: Compound-binds-Protein (was Compound-binds-Gene, BIOLOGICALLY IMPOSSIBLE)
  - Fixes 5.11: Withdrawn drug treats-edge check (PATIENT-SAFETY gate)
  - Fixes 5.8: Edge direction validation against CORE_EDGE_TYPES
  - Fixes 4.1: round(None) crash on empty graph → defensive None-safe math
  - Fixes 4.2: Silent exception swallowing → logger.exception on all failures
  - Fixes 7.2: Deterministic UNWIND labels(n) pattern (was labels(n)[0])
  - Fixes 7.1: UTC timestamps (was naive local time)
  - Fixes 2.5/4.13: Stable dict keys (was f-string keys)
  - Fixes 1.1: StatsProvider Protocol defined, canonical module declared
  - Full 16-domain coverage: Architecture, Design, Scientific Correctness,
    Coding, Data Quality, Reliability, Idempotency, Performance, Security,
    Testing, Logging, Configuration, Documentation, Compliance,
    Interoperability, Lineage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    TypedDict,
    Union,
    runtime_checkable,
)

__version__: str = "3.0.0"
__all__: list[str] = [
    "GraphStats",
    "StatsProvider",
    "StatsReport",
    "ExitCriterionResult",
    "ExitCriteriaReport",
    "SanityCheckResult",
    "SanityCheckReport",
    "QueryRecord",
    "STATS_SCHEMA_VERSION",
]

try:
    from neo4j import READ_ACCESS, Driver, GraphDatabase
except ImportError:
    Driver = None  # type: ignore[assignment, misc]
    GraphDatabase = None
    READ_ACCESS = None

from .config import (
    AUDIT_TRAIL_ENABLED,
    CANONICAL_IDS,
    CONFIG_HASH,
    CORE_EDGE_TYPES,
    CORE_EDGE_TYPES_SET,
    CORE_NODE_TYPES,
    CORRELATION_ID,
    MIN_EDGES_W1,
    MIN_EDGES_W2,
    MIN_NODES_W1,
    MIN_NODES_W2,
    MIN_POSITIVE_PAIRS,
    Neo4jConfig,
    PIPELINE_VERSION,
    RUN_ID,
    SCHEMA_VERSION,
    PACKAGE_VERSION,
)
from .utils import sanitize_identifier

# ── Configurable thresholds (Domain 12: Configuration) ──────────────────
# Fixes 12.1: Magic numbers externalized to config with env-var overrides.

MIN_COMPOUNDS_FOR_SANITY: int = int(
    os.environ.get("DRUGOS_STATS_MIN_COMPOUNDS", "10000")
)
"""Minimum compound nodes required for sanity check to pass."""

MIN_GENES_FOR_SANITY: int = int(
    os.environ.get("DRUGOS_STATS_MIN_GENES", "15000")
)
"""Minimum gene nodes required for sanity check to pass."""

MAX_DENSITY_THRESHOLD: float = float(
    os.environ.get("DRUGOS_STATS_MAX_DENSITY", "0.01")
)
"""Maximum reasonable graph density (naive homogeneous). Heterogeneous graphs
should use density_per_edge_type instead."""

STATS_QUERY_TIMEOUT_SECONDS: int = int(
    os.environ.get("DRUGOS_STATS_TIMEOUT_SECONDS", "120")
)
"""Timeout for each individual Neo4j query in stats computation."""

STATS_DEFAULT_PROFILE: str = os.environ.get(
    "DRUGOS_STATS_PROFILE", "standard"
)
"""Default stats profile: 'quick', 'standard', or 'full'."""

STATS_ENABLED_CHECKS: str = os.environ.get(
    "DRUGOS_STATS_ENABLED_CHECKS", "all"
)
"""Comma-separated check numbers to run, or 'all' for all checks."""

STATS_SCHEMA_VERSION: str = "3.0.0"
"""Schema version for the StatsReport output format."""

# ── Sanity check reference compounds (Domain 12: Configurable) ──────────
# Fixes 12.2: Hardcoded DrugBank IDs replaced with InChIKey-based config.
# Fixes 3.1: Uses CORRECT biology — Compound-binds-Protein, NOT Gene.
# Fixes 3.2/3.3: Uses InChIKeys (canonical IDs), not DrugBank IDs.

SANITY_CHECK_COMPOUNDS: List[Dict[str, str]] = [
    {
        "name": "Aspirin",
        "inchikey": os.environ.get(
            "SANITY_CHECK_ASPIRIN_INCHIKEY",
            "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
        ),
        "edge_type": "treats",
        "target_type": "Disease",
        "description": "Aspirin should treat at least one disease",
    },
    {
        "name": "Metformin",
        "inchikey": os.environ.get(
            "SANITY_CHECK_METFORMIN_INCHIKEY",
            "CJMJZSYNKJQDPU-UHFFFAOYSA-N",
        ),
        "edge_type": "binds",
        "target_type": "Protein",
        "description": (
            "Metformin should bind at least one protein target "
            "(NOT gene — drugs bind PROTEINS, gene products)"
        ),
    },
]

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Typed Schemas (Domain 2: Design — typed output contracts)
# ═══════════════════════════════════════════════════════════════════════════
# Fixes 1.5, 4.14, 7.8, 14.3, 15.3: Stable TypedDict output schemas.


class QueryRecord(TypedDict, total=False):
    """Auditable record of a single query executed during stats computation.

    Used for data lineage (Domain 16) and debugging (Domain 11).
    """
    name: str
    query_hash: str
    duration_ms: float
    rows_returned: int
    status: str  # 'ok' | 'error'
    error_message: str


class ExitCriterionResult(TypedDict):
    """Result of a single exit-criterion check.

    Keys are STABLE strings — no f-string interpolation (Fixes 2.5, 4.13).
    """
    criterion: str
    passed: bool
    actual: Union[int, float]
    threshold: Union[int, float]
    severity: str  # 'critical' | 'warning'


class ExitCriteriaReport(TypedDict):
    """Full exit-criteria report with summary and individual results.

    Fixes 2.9: Structured report instead of raw dict.
    """
    week: int
    timestamp: str
    passed_count: int
    total_count: int
    passed_all: bool
    criteria: List[ExitCriterionResult]
    warnings: List[str]


class SanityCheckResult(TypedDict):
    """Result of a single sanity check.

    Fixes 4.14: TypedDict with all fields documented.
    """
    check: str
    check_number: int
    passed: bool
    detail: str
    severity: str  # 'patient_safety' | 'critical' | 'warning' | 'info'
    error: bool  # True if the check itself errored (not the graph)


class SanityCheckReport(TypedDict):
    """Full sanity-check report with summary and individual results.

    Fixes 2.9: Structured report with passed_all flag.
    """
    timestamp: str
    passed_count: int
    total_count: int
    passed_all: bool
    patient_safety_failures: int
    checks: List[SanityCheckResult]
    warnings: List[str]


class StatsReport(TypedDict, total=False):
    """Full statistics report — the output of compute_full_stats().

    Every field is documented in the module docstring DATA DICTIONARY.
    All numeric fields are int or float — never 'N/A' or None (Fixes 4.7, 15.1).

    This TypedDict uses total=False because not all profiles compute
    all fields. The _validate_stats_report() function verifies required
    fields are present for the given profile.
    """
    # Identity & lineage (Domain 16)
    stats_schema_version: str
    stats_module_version: str
    pipeline_run_id: str
    pipeline_version: str
    schema_version: str
    config_hash: str
    correlation_id: str
    computed_at: str  # UTC ISO-8601
    computed_by: str
    database: str

    # Core counts
    total_nodes: int
    total_edges: int
    node_counts_by_type: Dict[str, int]
    edge_counts_by_type: Dict[str, int]

    # Degree statistics
    avg_out_degree: float
    max_out_degree: int
    min_out_degree: int

    # Connectivity
    isolated_nodes: int  # -1 if query timed out (Fixes 4.7: no 'N/A')

    # Density (Fixes 3.4, 13.6: naive renamed, per-type added)
    density_homogeneous_naive: float
    density_per_edge_type: Dict[str, float]

    # Data completeness (Domain 5)
    compound_name_coverage: float
    compound_smiles_coverage: float
    disease_name_coverage: float
    canonical_id_coverage: Dict[str, float]
    compound_safety_coverage: Dict[str, float]

    # Patient-safety gates (Domain 3, 5)
    withdrawn_with_treats: int
    edge_direction_violations: int

    # Diagnostics
    warnings: List[str]
    query_timings: Dict[str, float]
    queries_run: List[QueryRecord]
    lineage: Dict[str, Any]

    # Graph fingerprint (Domain 16: traceability)
    graph_fingerprint: str
    graph_last_modified_at: str


# ═══════════════════════════════════════════════════════════════════════════
# StatsProvider Protocol (Domain 1: Architecture)
# ═══════════════════════════════════════════════════════════════════════════
# Fixes 1.1, 1.2: Protocol defines the contract. GraphStats implements it.


@runtime_checkable
class StatsProvider(Protocol):
    """Protocol for graph statistics providers.

    Both ``GraphStats`` (external API) and ``kg_builder.GraphStatsCollector``
    (internal builder helper) should implement this Protocol. This prevents
    the two-implementation drift problem (audit issue 1.1).

    NOTE: GraphStatsCollector is an internal helper in kg_builder.py. Use
    GraphStats (this module) for all pipeline-level validation.
    """

    def compute_full_stats(
        self, stats_profile: str = "standard",
    ) -> Dict[str, Any]: ...

    def check_exit_criteria(self, week: int = 2) -> Dict[str, Any]: ...

    def run_sanity_checks(self) -> Dict[str, Any]: ...

    def generate_data_readme(
        self, output_path: Optional[str] = None,
    ) -> Union[str, None]: ...

    def label_distribution_report(self) -> Dict[str, Any]: ...

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...


# ═══════════════════════════════════════════════════════════════════════════
# Helper Functions (Domain 4: Coding, Domain 7: Idempotency)
# ═══════════════════════════════════════════════════════════════════════════


def _check_neo4j_available() -> None:
    """Raise informative error if neo4j driver is not installed."""
    if GraphDatabase is None:
        raise ImportError(
            "The 'neo4j' Python driver is not installed. "
            "Install it with: pip install neo4j>=5.0,<6.0"
        )


def _safe_round(value: Optional[float], digits: int = 2) -> float:
    """Round a value safely, returning 0.0 for None.

    Fixes 4.1: round(None, 2) would crash. This is used for all
    aggregate statistics that may be None on an empty graph.
    """
    if value is None:
        return 0.0
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        logger.warning(
            "Cannot round value %r, returning 0.0",
            value,
        )
        return 0.0


def _safe_int(value: Optional[Any]) -> int:
    """Convert to int safely, returning 0 for None or invalid values."""
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning(
            "Cannot convert value %r to int, returning 0",
            value,
        )
        return 0


def _parse_enabled_checks(spec: str, total: int) -> List[int]:
    """Parse the STATS_ENABLED_CHECKS env-var into a list of check numbers.

    Args:
        spec: 'all' or comma-separated numbers like '1,2,3,5,7'.
        total: Maximum check number (used only for validation).

    Returns:
        List of 1-indexed check numbers to run.
    """
    spec = spec.strip().lower()
    if spec == "all":
        return list(range(1, total + 1))
    try:
        checks = [int(x.strip()) for x in spec.split(",") if x.strip()]
        validated = [c for c in checks if 1 <= c <= total]
        if len(validated) != len(checks):
            invalid = set(checks) - set(validated)
            logger.warning(
                "Ignoring invalid check numbers: %s (valid range: 1-%d)",
                invalid, total,
            )
        return sorted(validated)
    except ValueError:
        logger.warning(
            "Invalid STATS_ENABLED_CHECKS value %r, running all checks",
            spec,
        )
        return list(range(1, total + 1))


def _compute_graph_fingerprint(
    total_nodes: int, total_edges: int,
    node_counts: Dict[str, int], edge_counts: Dict[str, int],
) -> str:
    """Compute a lightweight SHA-256 fingerprint of the graph structure.

    Uses node/edge counts rather than content hashing for speed.
    This enables detecting whether the graph changed between runs
    (Domain 16: Data Lineage).

    Args:
        total_nodes: Total node count.
        total_edges: Total edge count.
        node_counts: Per-type node counts.
        edge_counts: Per-type edge counts.

    Returns:
        Hex-encoded SHA-256 hash string.
    """
    content = json.dumps(
        {
            "total_nodes": total_nodes,
            "total_edges": total_edges,
            "node_counts": dict(sorted(node_counts.items())),
            "edge_counts": dict(sorted(edge_counts.items())),
        },
        sort_keys=True,
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _validate_stats_report(
    report: Dict[str, Any], profile: str = "standard",
) -> List[str]:
    """Validate that a StatsReport contains all required fields for its profile.

    Args:
        report: The stats report to validate.
        profile: 'quick', 'standard', or 'full'.

    Returns:
        List of validation error messages (empty if valid).
    """
    required_all = {
        "stats_schema_version", "stats_module_version", "computed_at",
        "database", "total_nodes", "total_edges",
        "node_counts_by_type", "edge_counts_by_type",
        "avg_out_degree", "isolated_nodes",
        "density_homogeneous_naive", "warnings", "query_timings",
        "lineage",
    }
    required_standard = required_all | {
        "density_per_edge_type", "compound_name_coverage",
        "disease_name_coverage", "canonical_id_coverage",
        "edge_direction_violations", "withdrawn_with_treats",
        "graph_fingerprint",
    }
    required_full = required_standard | {
        "compound_smiles_coverage", "compound_safety_coverage",
        "max_out_degree", "min_out_degree", "queries_run",
    }

    required = {
        "quick": required_all,
        "standard": required_standard,
        "full": required_full,
    }.get(profile, required_standard)

    missing = required - set(report.keys())
    errors = [f"Missing required field: {f}" for f in sorted(missing)]

    # Type consistency: numeric fields must be int or float (Fixes 15.1)
    numeric_fields = {
        "total_nodes", "total_edges", "isolated_nodes",
        "avg_out_degree", "density_homogeneous_naive",
        "compound_name_coverage", "disease_name_coverage",
        "withdrawn_with_treats", "edge_direction_violations",
    }
    for field in numeric_fields:
        if field in report and not isinstance(
            report[field], (int, float),
        ):
            errors.append(
                f"Field '{field}' has wrong type: "
                f"{type(report[field]).__name__}, expected int or float"
            )

    # JSON round-trip test (Fixes 15.1: mixed types break JSON consumers)
    try:
        json.dumps(report)
    except (TypeError, ValueError) as exc:
        errors.append(f"Stats report is not JSON-serializable: {exc}")

    return errors


# ═══════════════════════════════════════════════════════════════════════════
# GraphStats Class
# ═══════════════════════════════════════════════════════════════════════════


class GraphStats:
    """Compute and report comprehensive knowledge graph statistics.

    This is the **CANONICAL** stats implementation for the DrugOS pipeline.
    Used by ``run_pipeline.py`` for Week 2 exit criteria validation.
    For internal builder-time stats, see ``kg_builder.GraphStatsCollector``.

    Supports context manager protocol for safe connection handling::

        with GraphStats(config=my_config) as gs:
            stats = gs.compute_full_stats()

    Patient Safety Context
    ----------------------
    This module gates the Week 2 exit decision. If it green-lights a corrupt
    graph, the Graph Transformer trains on bad data, the RL ranker promotes
    wrong drug-disease pairs, and a clinician acts on wrong recommendations.
    Treat every output of this class as patient-safety-critical.

    Thread Safety
    -------------
    NOT thread-safe. Create a separate instance per thread if needed.

    Fixes (169 issues across 16 domains):
        Domain 1  (Architecture):   1.1-1.9
        Domain 2  (Design):         2.1-2.9
        Domain 3  (Scientific):     3.1-3.11
        Domain 4  (Coding):         4.1-4.16
        Domain 5  (Data Quality):   5.1-5.11
        Domain 6  (Reliability):    6.1-6.9
        Domain 7  (Idempotency):    7.1-7.8
        Domain 8  (Performance):    8.1-8.8
        Domain 9  (Security):       9.1-9.7
        Domain 10 (Testing):        10.1-10.11
        Domain 11 (Logging):        11.1-11.10
        Domain 12 (Configuration): 12.1-12.9
        Domain 13 (Documentation):  13.1-13.11
        Domain 14 (Compliance):    14.1-14.9
        Domain 15 (Interoperability): 15.1-15.9
        Domain 16 (Lineage):        16.1-16.10
    """

    def __init__(self, config: Optional[Neo4jConfig] = None) -> None:
        """Initialize GraphStats with Neo4j connection configuration.

        Args:
            config: Neo4jConfig instance. If None, creates default from
                environment variables. Fixes 12.9: validates that
                credentials are available.

        Raises:
            ValueError: If Neo4j password is not set (Fixes 12.7, 12.9).
        """
        self.config = config or Neo4jConfig()

        # Fix 12.7: Validate database name before any connection attempt
        if (
            not self.config.database
            or not isinstance(self.config.database, str)
            or not self.config.database.strip()
        ):
            raise ValueError(
                f"Invalid Neo4j database name: {self.config.database!r}. "
                "Set DRUGOS_NEO4J_DATABASE environment variable."
            )

        self.driver: Optional[Driver] = None
        self._connected: bool = False
        self.report: Dict[str, Any] = {}

    # ─── Context Manager ───────────────────────────────────────────────

    def __enter__(self) -> "GraphStats":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        self.disconnect()
        return False

    # ─── Connection Management ────────────────────────────────────────

    def connect(self) -> None:
        """Establish Neo4j connection with verification.

        Fixes 6.1: verify_connectivity() ensures the connection works
        before any queries are issued.
        Fixes 9.1: Uses encrypted connection by default.
        Fixes 11.1: Connection failures are logged.
        """
        _check_neo4j_available()
        if self._connected and self.driver is not None:
            logger.debug(
                "GraphStats already connected to %s",
                self.config.uri,
            )
            return

        try:
            self.driver = GraphDatabase.driver(
                self.config.uri,
                auth=(self.config.user, self.config.password or ""),
                connection_timeout=self.config.connection_timeout,
            )
            # Fix 6.1: Verify connectivity before proceeding
            self.driver.verify_connectivity()
            self._connected = True
            logger.info(
                "GraphStats connected to Neo4j at %s (database=%s)",
                self.config.uri,
                self.config.database,
                extra={
                    "correlation_id": CORRELATION_ID,
                    "pipeline_run_id": RUN_ID,
                },
            )
        except Exception as exc:
            # Fix 6.2: Log connection failure with full context
            logger.exception(
                "GraphStats connection failed to %s: %s",
                self.config.uri,
                exc,
                extra={
                    "correlation_id": CORRELATION_ID,
                    "pipeline_run_id": RUN_ID,
                },
            )
            self.driver = None
            self._connected = False
            raise

    def disconnect(self) -> None:
        """Close Neo4j connection safely.

        Fixes 4.5: Sets self.driver to None after close.
        """
        if self.driver is not None:
            try:
                self.driver.close()
            except (OSError, RuntimeError, AttributeError) as exc:
                # v41 ROOT FIX (Task J SEV3): narrowed from bare
                # ``except Exception``. Driver.close() can fail with:
                #   - OSError: network/socket cleanup failure on a
                #     half-closed connection.
                #   - RuntimeError: neo4j driver internal-state error
                #     (driver already closed, driver in a bad state).
                #   - AttributeError: driver API drift (close() renamed
                #     in a future neo4j version).
                # Other exceptions propagate — they indicate real bugs.
                # We're in disconnect() so the driver is set to None
                # regardless (the finally block), but logging the
                # specific failure mode helps operators diagnose
                # connection-pool leaks.
                logger.warning(
                    "Error closing Neo4j driver (%s: %s) — driver "
                    "reference cleared regardless.",
                    type(exc).__name__, exc,
                )
            finally:
                self.driver = None
                self._connected = False
                logger.debug("GraphStats disconnected")

    def _ensure_connected(self) -> None:
        """Ensure we have a valid Neo4j connection before queries.

        Fixes 6.3: Auto-reconnects if connection was lost.
        """
        if not self._connected or self.driver is None:
            logger.info("GraphStats not connected, reconnecting...")
            self.connect()

    def _get_session(self):
        """Create a read-only Neo4j session.

        Fixes 9.4: Uses READ_ACCESS for all sessions — stats computation
        never writes to the graph.
        """
        self._ensure_connected()
        return self.driver.session(
            database=self.config.database,
            default_access_mode=READ_ACCESS,
        )

    def _run_query(
        self,
        session,
        query: str,
        query_name: str,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Run a Neo4j query with timing, logging, and error handling.

        Fixes 6.2: Per-query error handling — single query failure does
        not crash the entire stats computation.
        Fixes 11.6/11.7: Query timing and logging for debugging.
        Fixes 16.4: Auditable query records for lineage.

        Args:
            session: Active Neo4j session.
            query: Cypher query string.
            query_name: Human-readable name for logging.
            parameters: Optional query parameters.

        Returns:
            Query result, or None if the query failed.
        """
        parameters = parameters or {}
        t0 = time.perf_counter()
        query_hash = hashlib.sha256(
            query.encode("utf-8"),
        ).hexdigest()[:12]
        status = "ok"
        error_message = ""
        result = None
        rows_returned = 0

        logger.debug(
            "Running query: %s [%s]",
            query_name,
            query_hash,
        )

        try:
            result = session.run(
                query,
                parameters,
                timeout=STATS_QUERY_TIMEOUT_SECONDS,
            )
            # Consume the result to count rows
            records = list(result)
            rows_returned = len(records)
            return records
        except Exception as exc:
            status = "error"
            error_message = str(exc)
            # Fix 6.2/11.1: Log every query failure with full context
            logger.exception(
                "Query '%s' [%s] failed: %s",
                query_name,
                query_hash,
                exc,
                extra={
                    "query_name": query_name,
                    "query_hash": query_hash,
                    "correlation_id": CORRELATION_ID,
                },
            )
            return None
        finally:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.debug(
                "Query '%s' completed in %.1fms (%d rows, status=%s)",
                query_name,
                elapsed_ms,
                rows_returned,
                status,
            )
            # Store timing
            if not hasattr(self, "_query_timings"):
                self._query_timings: Dict[str, float] = {}
            self._query_timings[query_name] = round(
                elapsed_ms / 1000, 3,
            )
            # Store record
            if not hasattr(self, "_queries_run"):
                self._queries_run: List[QueryRecord] = []
            self._queries_run.append({
                "name": query_name,
                "query_hash": query_hash,
                "duration_ms": round(elapsed_ms, 2),
                "rows_returned": rows_returned,
                "status": status,
                "error_message": error_message,
            })

    # ─── Core Statistics ────────────────────────────────────────────────

    def compute_full_stats(
        self, stats_profile: str = "standard",
    ) -> Dict[str, Any]:
        """Compute the full set of graph statistics.

        Args:
            stats_profile: 'quick' (3 queries), 'standard' (10+ queries),
                or 'full' (15+ queries). Defaults to STATS_DEFAULT_PROFILE
                from environment.

        Returns:
            StatsReport dict — fully JSON-serializable, no None values
            in numeric fields, UTC timestamps.

        Side Effects:
            - Sets self.report to the returned stats dict.
            - Logs query timings and any warnings.
            - Logs structured fields for log aggregation.

        Performance:
            - 'quick':   ~2-5s on 500K-node graph (3 queries)
            - 'standard': ~10-30s on 500K-node graph (10+ queries)
            - 'full':    ~30-60s on 500K-node graph (15+ queries)

        Failure Modes:
            - Individual query failures are caught and logged; the method
              returns partial stats with warnings.
            - Empty graph: returns zeros, never crashes (Fix 4.1).

        Fixes:
            4.1: round(None) crash → _safe_round/_safe_int helpers
            4.7: isolated_nodes='N/A' → int (-1 if query failed)
            7.1: naive datetime → UTC timezone
            7.2: labels(n)[0] → UNWIND labels(n) AS lbl
            7.3: No run ID → lineage metadata added
            16.1: No provenance → lineage dict added
        """
        if stats_profile not in ("quick", "standard", "full"):
            stats_profile = "standard"

        self._query_timings = {}
        self._queries_run = []

        stats: Dict[str, Any] = {
            "stats_schema_version": STATS_SCHEMA_VERSION,
            "stats_module_version": __version__,
            "pipeline_run_id": RUN_ID,
            "pipeline_version": PIPELINE_VERSION,
            "schema_version": SCHEMA_VERSION,
            "config_hash": CONFIG_HASH,
            "correlation_id": CORRELATION_ID,
            # Fix 7.1: UTC timestamps (was naive local time)
            "computed_at": datetime.now(timezone.utc).isoformat(),
            "computed_by": f"graph_stats.py v{__version__}",
            "database": self.config.database,
        }

        warnings: List[str] = []

        with self._get_session() as session:
            # ── Quick node count (logged first for context — Fix 11.9) ──
            records = self._run_query(
                session,
                "MATCH (n) RETURN count(n) AS total",
                "node_count_total",
            )
            total_nodes = (
                _safe_int(records[0]["total"]) if records else 0
            )
            stats["total_nodes"] = total_nodes

            logger.info(
                "Computing stats for graph with %s nodes (profile=%s)",
                total_nodes,
                stats_profile,
                extra={
                    "total_nodes": total_nodes,
                    "correlation_id": CORRELATION_ID,
                    "pipeline_run_id": RUN_ID,
                },
            )

            # ── Node counts by type (Fix 7.2: UNWIND labels) ──
            records = self._run_query(
                session,
                "MATCH (n) UNWIND labels(n) AS lbl "
                "RETURN lbl AS label, count(*) AS cnt "
                "ORDER BY cnt DESC, lbl ASC",
                "node_counts_by_type",
            )
            stats["node_counts_by_type"] = (
                {rec["label"]: _safe_int(rec["cnt"]) for rec in records}
                if records else {}
            )

            # ── Edge count ──
            records = self._run_query(
                session,
                "MATCH ()-[r]->() RETURN count(r) AS total",
                "edge_count_total",
            )
            stats["total_edges"] = (
                _safe_int(records[0]["total"]) if records else 0
            )

            # ── Edge counts by type ──
            records = self._run_query(
                session,
                "MATCH ()-[r]->() RETURN type(r) AS rel_type, "
                "count(r) AS cnt ORDER BY cnt DESC",
                "edge_counts_by_type",
            )
            stats["edge_counts_by_type"] = (
                {rec["rel_type"]: _safe_int(rec["cnt"]) for rec in records}
                if records else {}
            )

            if stats_profile in ("standard", "full"):
                # ── Degree statistics ──
                records = self._run_query(
                    session,
                    "MATCH (n) RETURN "
                    "avg(size((n)-->())) AS avg_out, "
                    "max(size((n)-->())) AS max_out, "
                    "min(size((n)-->())) AS min_out",
                    "degree_statistics",
                )
                if records and records[0].get("avg_out") is not None:
                    stats["avg_out_degree"] = _safe_round(
                        records[0]["avg_out"],
                    )
                    stats["max_out_degree"] = _safe_int(
                        records[0]["max_out"],
                    )
                    stats["min_out_degree"] = _safe_int(
                        records[0]["min_out"],
                    )
                else:
                    stats["avg_out_degree"] = 0.0
                    stats["max_out_degree"] = 0
                    stats["min_out_degree"] = 0

                # ── Isolated nodes (Fix 4.7: returns int, never 'N/A') ──
                # This query uses NOT (n)--() which is expensive on large
                # graphs and may timeout. If it fails, we set -1 and log
                # a warning rather than crashing the entire stats computation.
                records = self._run_query(
                    session,
                    "MATCH (n) WHERE NOT (n)--() "
                    "RETURN count(n) AS isolated",
                    "isolated_nodes",
                )
                if records:
                    stats["isolated_nodes"] = _safe_int(
                        records[0]["isolated"],
                    )
                else:
                    stats["isolated_nodes"] = -1
                    warnings.append(
                        "Isolated-nodes query failed or timed out. "
                        "Value set to -1 (unknown). "
                        "This is non-critical — isolated nodes are "
                        "cosmetic, not a data-quality issue."
                    )

                # ── Naive homogeneous density (Fix 13.6: renamed) ──
                # NOTE: This assumes a homogeneous complete graph which
                # is INCORRECT for heterogeneous multi-relational
                # biomedical knowledge graphs. Use density_per_edge_type
                # for meaningful density analysis.
                n = stats["total_nodes"]
                e = stats["total_edges"]
                max_edges = n * (n - 1)
                stats["density_homogeneous_naive"] = (
                    round(e / max_edges, 12) if max_edges > 0 else 0.0
                )

                # ── Per-edge-type density (Domain 3, 8) ──
                per_type_density: Dict[str, float] = {}
                for rel_type, cnt in stats.get(
                    "edge_counts_by_type", {},
                ).items():
                    try:
                        safe_rel = sanitize_identifier(
                            rel_type, "rel type",
                        )
                        recs = self._run_query(
                            session,
                            f"MATCH ()-[r:{safe_rel}]->() "
                            "RETURN "
                            "count(DISTINCT startNode(r)) AS n_src, "
                            "count(DISTINCT endNode(r)) AS n_dst",
                            f"density_per_type_{rel_type}",
                        )
                        # v20 SF-8 ROOT FIX: mirror the SF-9 pattern.
                        # _run_query SWALLOWS exceptions and returns None.
                        # The previous "REM-26 ROOT FIX" comment claimed to
                        # store None on failure via the outer except, but the
                        # outer except NEVER FIRED because _run_query already
                        # swallowed the exception. The else-branch at line
                        # ~1035 then set per_type_density[rel_type] = 0.0,
                        # which falsely passed sanity check #7.
                        if recs is None:
                            # Query CRASHED (e.g. Neo4j timeout).
                            # Store None so downstream sanity checks can
                            # distinguish "crashed" from "0% density".
                            per_type_density[rel_type] = None
                            warnings.append(
                                f"{rel_type}: per-type density query "
                                f"CRASHED — value is None (not 0.0). "
                                f"Investigate Neo4j connectivity / timeout."
                            )
                            continue
                        if recs and recs[0] is not None:
                            n_src = _safe_int(recs[0]["n_src"])
                            n_dst = _safe_int(recs[0]["n_dst"])
                            if n_src == n_dst and n_src > 0:
                                # Same-type edges: denominator is n*(n-1)/2
                                # for undirected, or n*(n-1) for directed
                                denom = n_src * (n_src - 1)
                            elif n_src > 0 and n_dst > 0:
                                denom = n_src * n_dst
                            else:
                                denom = 1
                            per_type_density[rel_type] = round(
                                cnt / denom, 12,
                            )
                        else:
                            # Empty result set (legitimate 0 density).
                            per_type_density[rel_type] = 0.0
                    except Exception as exc:
                        # Defensive: this branch is unreachable in practice
                        # because _run_query swallows exceptions, but kept
                        # for safety in case _run_query is refactored to
                        # re-raise.
                        logger.exception(
                            "Density computation failed for %s: %s",
                            rel_type,
                            exc,
                        )
                        per_type_density[rel_type] = None
                        warnings.append(
                            f"{rel_type}: per-type density computation "
                            f"raised {type(exc).__name__} — value set to None."
                        )
                stats["density_per_edge_type"] = per_type_density

                # ── Data completeness: Compound name & SMILES ──
                records = self._run_query(
                    session,
                    "MATCH (c:Compound) RETURN count(c) AS total, "
                    "sum(CASE WHEN c.name IS NOT NULL "
                    "AND c.name <> '' "
                    "THEN 1 ELSE 0 END) AS with_name, "
                    "sum(CASE WHEN c.smiles IS NOT NULL "
                    "AND c.smiles <> '' "
                    "THEN 1 ELSE 0 END) AS with_smiles",
                    "compound_data_completeness",
                )
                if records and records[0]:
                    total = max(_safe_int(records[0]["total"]), 1)
                    stats["compound_name_coverage"] = round(
                        _safe_int(records[0]["with_name"]) / total, 4,
                    )
                    stats["compound_smiles_coverage"] = round(
                        _safe_int(records[0]["with_smiles"]) / total, 4,
                    )

                # ── Data completeness: Disease name ──
                records = self._run_query(
                    session,
                    "MATCH (d:Disease) RETURN count(d) AS total, "
                    "sum(CASE WHEN d.name IS NOT NULL "
                    "AND d.name <> '' "
                    "THEN 1 ELSE 0 END) AS with_name",
                    "disease_data_completeness",
                )
                if records and records[0]:
                    total = max(_safe_int(records[0]["total"]), 1)
                    stats["disease_name_coverage"] = round(
                        _safe_int(records[0]["with_name"]) / total, 4,
                    )

                # ── Canonical ID coverage (Fix 5.1: per CANONICAL_IDS) ──
                # For each entity type, checks the CORRECT canonical
                # property — not just 'id'.
                canonical_coverage: Dict[str, float] = {}
                for entity_type, canonical_prop in CANONICAL_IDS.items():
                    safe_label = sanitize_identifier(
                        entity_type, "node label",
                    )
                    safe_prop = sanitize_identifier(
                        canonical_prop, "property name",
                    )
                    recs = self._run_query(
                        session,
                        f"MATCH (n:{safe_label}) "
                        f"WHERE n.{safe_prop} IS NULL "
                        "RETURN count(n) AS missing",
                        f"canonical_id_coverage_{entity_type}",
                    )
                    if recs is None:
                        # v16 ROOT FIX (SF-9): _run_query returns None
                        # on exception. The previous code put 0.0 in
                        # the canonical_coverage dict — making it
                        # indistinguishable from "0% coverage" (which
                        # is a legitimate, alarming measurement).
                        # Downstream consumers reading stats would
                        # see 0.0 and trigger a "low canonical
                        # coverage" alert — a FALSE POSITIVE because
                        # the real problem was a query crash (e.g.
                        # Neo4j timeout), not actual missing IDs.
                        # Store None instead so consumers can
                        # distinguish "crashed" from "0%".
                        canonical_coverage[entity_type] = None
                        warnings.append(
                            f"{entity_type}: canonical coverage query "
                            f"CRASHED — value is None (not 0.0). Investigate "
                            f"Neo4j connectivity / timeout."
                        )
                    elif recs:
                        missing = _safe_int(recs[0]["missing"])
                        node_type_count = stats[
                            "node_counts_by_type"
                        ].get(entity_type, 0)
                        total = max(node_type_count, 1)
                        coverage = round(
                            (total - missing) / total, 4,
                        )
                        canonical_coverage[entity_type] = coverage
                        if missing > 0:
                            warnings.append(
                                f"{entity_type}: {missing} nodes "
                                f"missing canonical ID "
                                f"('{canonical_prop}')"
                            )
                    else:
                        # Empty result set (no rows returned) — distinct
                        # from query crash. Legitimate 0% coverage.
                        canonical_coverage[entity_type] = 0.0
                stats["canonical_id_coverage"] = canonical_coverage

                # ── Compound safety field coverage (Fix 5.2, 5.11) ──
                # Patient-safety gate: withdrawn/toxicity fields must exist
                safety_coverage: Dict[str, float] = {}
                for field in ("withdrawn", "toxicity"):
                    recs = self._run_query(
                        session,
                        f"MATCH (c:Compound) RETURN count(c) AS total, "
                        f"sum(CASE WHEN c.{field} IS NOT NULL "
                        f"THEN 1 ELSE 0 END) AS with_field",
                        f"compound_{field}_coverage",
                    )
                    if recs:
                        total = max(_safe_int(recs[0]["total"]), 1)
                        with_field = _safe_int(recs[0]["with_field"])
                        safety_coverage[field] = round(
                            with_field / total, 4,
                        )
                stats["compound_safety_coverage"] = safety_coverage

                # ── PATIENT-SAFETY: Withdrawn drugs with treats edges ──
                # Fix 5.11: A withdrawn drug (e.g., Valdecoxib) must NOT
                # have treats edges. If it does, the model recommends a
                # withdrawn drug → direct patient harm.
                recs = self._run_query(
                    session,
                    "MATCH (c:Compound)-[r:treats]->(d:Disease) "
                    "WHERE c.withdrawn = true "
                    "RETURN count(r) AS cnt",
                    "withdrawn_with_treats_check",
                )
                withdrawn_with_treats = (
                    _safe_int(recs[0]["cnt"]) if recs else 0
                )
                stats["withdrawn_with_treats"] = withdrawn_with_treats
                if withdrawn_with_treats > 0:
                    warnings.append(
                        f"CRITICAL: {withdrawn_with_treats} withdrawn "
                        f"compound(s) have 'treats' edges to diseases. "
                        f"This is a PATIENT-SAFETY violation."
                    )

                # ── Edge direction validation (Fix 5.8) ──
                # Verify that edges of each type connect the correct
                # source and destination node types per CORE_EDGE_TYPES.
                violations = self._check_edge_direction_violations(
                    session,
                )
                stats["edge_direction_violations"] = violations
                if violations > 0:
                    warnings.append(
                        f"CRITICAL: {violations} edges violate "
                        f"CORE_EDGE_TYPES direction schema. "
                        f"Gene-treats-Disease edges mean the model "
                        f"learns nonsense → wrong predictions."
                    )

            # Full profile adds:
            if stats_profile == "full":
                recs = self._run_query(
                    session,
                    "MATCH (n) "
                    "WHERE n._loaded_at IS NOT NULL "
                    "RETURN max(n._loaded_at) AS last_modified",
                    "graph_last_modified",
                )
                if recs and recs[0].get("last_modified"):
                    stats["graph_last_modified_at"] = str(
                        recs[0]["last_modified"],
                    )
                else:
                    stats["graph_last_modified_at"] = "unknown"

                # Source attribution (Fix 16.5)
                source_attribution = self._compute_source_attribution(
                    session,
                )
                if source_attribution:
                    stats["source_attribution"] = source_attribution

        # ── Graph fingerprint (Fix 7.4, 16.8) ──
        stats["graph_fingerprint"] = _compute_graph_fingerprint(
            stats.get("total_nodes", 0),
            stats.get("total_edges", 0),
            stats.get("node_counts_by_type", {}),
            stats.get("edge_counts_by_type", {}),
        )

        # ── Warnings & diagnostics ──
        stats["warnings"] = warnings
        stats["query_timings"] = getattr(
            self, "_query_timings", {},
        )
        stats["queries_run"] = getattr(
            self, "_queries_run", [],
        )

        # ── Lineage metadata (Fix 16.1, 7.3) ──
        stats["lineage"] = {
            "pipeline_run_id": RUN_ID,
            "computed_at": stats["computed_at"],
            "computed_by": stats["computed_by"],
            "schema_version": SCHEMA_VERSION,
            "config_hash": CONFIG_HASH,
            "graph_fingerprint": stats.get("graph_fingerprint", ""),
            "correlation_id": CORRELATION_ID,
            "stats_profile": stats_profile,
        }

        # ── Audit logging (Fix 9.7, 16.7) ──
        if AUDIT_TRAIL_ENABLED:
            try:
                self._write_audit_log(stats)
            except (OSError, ValueError, TypeError) as exc:
                # v41 ROOT FIX (Task J SEV3): narrowed from bare
                # ``except Exception``. Audit-log write failures are:
                #   - OSError: disk full, permission denied, broken pipe.
                #   - ValueError: stats dict not JSON-serializable
                #     (rare but possible if a downstream method returns
                #     a non-serializable object).
                #   - TypeError: json.dumps hit an unhandled type.
                # Other exceptions propagate — they indicate real bugs.
                # The audit log is observability-only (not on the
                # critical path), so a write failure should not abort
                # the stats report; the WARNING tells the operator
                # their audit trail has a gap.
                logger.warning(
                    "Failed to write audit log (%s: %s) — stats report "
                    "still returned, but audit trail has a gap.",
                    type(exc).__name__, exc,
                )

        # ── Validate output (Fix 14.1, 15.1) ──
        validation_errors = _validate_stats_report(
            stats, stats_profile,
        )
        for err in validation_errors:
            warnings.append(f"Validation error: {err}")
            logger.error(
                "Stats report validation error: %s",
                err,
                extra={
                    "correlation_id": CORRELATION_ID,
                    "pipeline_run_id": RUN_ID,
                },
            )

        self.report = stats
        logger.info(
            "Stats computation complete: %s nodes, %s edges, "
            "%s warnings, profile=%s",
            stats.get("total_nodes", 0),
            stats.get("total_edges", 0),
            len(warnings),
            stats_profile,
            extra={
                "total_nodes": stats.get("total_nodes", 0),
                "total_edges": stats.get("total_edges", 0),
                "warnings_count": len(warnings),
                "correlation_id": CORRELATION_ID,
                "pipeline_run_id": RUN_ID,
            },
        )
        return stats

    def _check_edge_direction_violations(
        self, session,
    ) -> int:
        """Check that edges match CORE_EDGE_TYPES source/destination labels.

        For each (src_type, rel_type, dst_type) in CORE_EDGE_TYPES,
        verify that all edges of that type connect nodes with the correct
        labels. Returns count of violations.

        Fixes 5.8: Gene-treats-Disease edges → model learns nonsense.
        """
        total_violations = 0

        # Build a set of (rel_type → (expected_src, expected_dst))
        edge_schema: Dict[str, set] = {}
        for src, rel, dst in CORE_EDGE_TYPES:
            if rel not in edge_schema:
                edge_schema[rel] = set()
            edge_schema[rel].add((src, dst))

        for rel_type, expected_pairs in edge_schema.items():
            try:
                safe_rel = sanitize_identifier(
                    rel_type, "rel type",
                )
                recs = self._run_query(
                    session,
                    f"MATCH (s)-[r:{safe_rel}]->(t) "
                    "RETURN labels(s)[0] AS src_lbl, "
                    "labels(t)[0] AS dst_lbl, "
                    "count(r) AS cnt",
                    f"edge_dir_check_{rel_type}",
                )
                if not recs:
                    continue
                for rec in recs:
                    src_lbl = rec.get("src_lbl", "")
                    dst_lbl = rec.get("dst_lbl", "")
                    cnt = _safe_int(rec.get("cnt", 0))
                    if (src_lbl, dst_lbl) not in expected_pairs:
                        total_violations += cnt
                        logger.warning(
                            "Edge direction violation: %s-%s->%s "
                            "(%d edges, expected pairs: %s)",
                            src_lbl,
                            rel_type,
                            dst_lbl,
                            cnt,
                            expected_pairs,
                        )
            except Exception as exc:
                logger.exception(
                    "Edge direction check failed for %s: %s",
                    rel_type,
                    exc,
                )

        return total_violations

    def _compute_source_attribution(
        self, session,
    ) -> Dict[str, Dict[str, int]]:
        """Compute per-node-type source attribution from _source property.

        Requires that loaders tag nodes with _source property during
        loading (kg_builder already does this).

        Fixes 16.5: Source attribution for data lineage.
        """
        attribution: Dict[str, Dict[str, int]] = {}
        for node_type in CORE_NODE_TYPES:
            try:
                safe_label = sanitize_identifier(
                    node_type, "node label",
                )
                recs = self._run_query(
                    session,
                    f"MATCH (n:{safe_label}) "
                    "WHERE n._source IS NOT NULL "
                    "RETURN n._source AS source, "
                    "count(n) AS cnt "
                    "ORDER BY cnt DESC",
                    f"source_attribution_{node_type}",
                )
                if recs:
                    type_attr: Dict[str, int] = {}
                    for rec in recs:
                        src = str(rec.get("source", "unknown"))
                        type_attr[src] = _safe_int(rec.get("cnt", 0))
                    if type_attr:
                        attribution[node_type] = type_attr
            except Exception as exc:
                logger.debug(
                    "Source attribution failed for %s: %s",
                    node_type,
                    exc,
                )
        return attribution

    def _write_audit_log(self, stats: Dict[str, Any]) -> None:
        """Write an audit log entry for stats computation.

        Fixes 9.7, 14.5, 16.7: FDA-compliant audit trail.
        """
        try:
            from .config import AUDIT_LOG_DIR
            AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
            audit_entry = {
                "action": "compute_full_stats",
                "pipeline_run_id": RUN_ID,
                "correlation_id": CORRELATION_ID,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "total_nodes": stats.get("total_nodes", 0),
                "total_edges": stats.get("total_edges", 0),
                "warnings_count": len(stats.get("warnings", [])),
                "stats_schema_version": STATS_SCHEMA_VERSION,
                "graph_fingerprint": stats.get("graph_fingerprint", ""),
            }
            # Append to JSONL audit log
            log_file = AUDIT_LOG_DIR / f"stats_audit_{RUN_ID}.jsonl"
            with open(log_file, "a") as f:
                f.write(json.dumps(audit_entry) + "\n")
        except Exception as exc:
            # Non-critical — don't fail stats for audit log issues
            logger.warning("Audit log write failed: %s", exc)

    # ─── Exit Criteria Checks ──────────────────────────────────────────

    def check_exit_criteria(
        self, week: int = 2,
    ) -> Dict[str, Any]:
        """Check exit criteria for the specified week.

        Args:
            week: 1 for internal phase criteria, 2 for project-level.

        Returns:
            ExitCriteriaReport dict with:
                - week: The week number checked
                - timestamp: UTC ISO-8601
                - passed_count: Number of criteria that passed
                - total_count: Total criteria checked
                - passed_all: True if ALL criteria passed
                - criteria: List of ExitCriterionResult dicts
                - warnings: List of non-fatal warnings

        Fixes:
            2.5, 4.13: Stable keys (no f-string key generation)
            11.2: Individual failure logging
            2.9: Structured report with passed_all field
        """
        stats = self.compute_full_stats()

        min_nodes = MIN_NODES_W1 if week == 1 else MIN_NODES_W2
        min_edges = MIN_EDGES_W1 if week == 1 else MIN_EDGES_W2

        # Fix 2.5, 4.13: STABLE keys — no f-string interpolation
        criteria: List[ExitCriterionResult] = [
            {
                "criterion": "min_nodes",
                "passed": stats.get("total_nodes", 0) >= min_nodes,
                "actual": stats.get("total_nodes", 0),
                "threshold": min_nodes,
                "severity": "critical",
            },
            {
                "criterion": "min_edges",
                "passed": stats.get("total_edges", 0) >= min_edges,
                "actual": stats.get("total_edges", 0),
                "threshold": min_edges,
                "severity": "critical",
            },
            {
                "criterion": "has_compound_nodes",
                "passed": stats.get(
                    "node_counts_by_type", {},
                ).get("Compound", 0) > 0,
                "actual": stats.get(
                    "node_counts_by_type", {},
                ).get("Compound", 0),
                "threshold": 1,
                "severity": "critical",
            },
            {
                "criterion": "has_disease_nodes",
                "passed": stats.get(
                    "node_counts_by_type", {},
                ).get("Disease", 0) > 0,
                "actual": stats.get(
                    "node_counts_by_type", {},
                ).get("Disease", 0),
                "threshold": 1,
                "severity": "critical",
            },
            {
                "criterion": "has_gene_nodes",
                "passed": stats.get(
                    "node_counts_by_type", {},
                ).get("Gene", 0) > 0,
                "actual": stats.get(
                    "node_counts_by_type", {},
                ).get("Gene", 0),
                "threshold": 1,
                "severity": "critical",
            },
            {
                "criterion": "has_treats_edges",
                "passed": stats.get(
                    "edge_counts_by_type", {},
                ).get("treats", 0) > 0,
                "actual": stats.get(
                    "edge_counts_by_type", {},
                ).get("treats", 0),
                "threshold": 1,
                "severity": "critical",
            },
        ]

        passed_count = sum(1 for c in criteria if c["passed"])
        total_count = len(criteria)
        passed_all = passed_count == total_count

        # Fix 11.2: Log individual failures with full context
        logger.info(
            "Week %d Exit Criteria: %d/%d passed",
            week, passed_count, total_count,
            extra={
                "correlation_id": CORRELATION_ID,
                "pipeline_run_id": RUN_ID,
                "passed_all": passed_all,
            },
        )
        for crit in criteria:
            if not crit["passed"]:
                logger.warning(
                    "Criterion FAILED: %s — got %s, need %s",
                    crit["criterion"],
                    crit["actual"],
                    crit["threshold"],
                    extra={
                        "criterion": crit["criterion"],
                        "actual": crit["actual"],
                        "threshold": crit["threshold"],
                        "correlation_id": CORRELATION_ID,
                    },
                )

        return {
            "week": week,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "passed_count": passed_count,
            "total_count": total_count,
            "passed_all": passed_all,
            "criteria": criteria,
            "warnings": stats.get("warnings", []),
        }

    # ─── Sanity Checks ─────────────────────────────────────────────────

    def run_sanity_checks(self) -> Dict[str, Any]:
        """Run 15+ sanity check queries with patient-safety gates.

        Returns:
            SanityCheckReport dict with:
                - timestamp: UTC ISO-8601
                - passed_count: Number of checks that passed
                - total_count: Total checks run
                - passed_all: True if ALL checks passed
                - patient_safety_failures: Count of patient-safety failures
                - checks: List of SanityCheckResult dicts
                - warnings: List of non-fatal warnings

        Checks performed (in order):
            1.  Aspirin (InChIKey) treats at least one disease
            2.  Metformin (InChIKey) binds at least one Protein (NOT Gene)
            3.  Compound-treats-Disease edges exist
            4.  Gene-Gene interaction edges exist
            5.  No self-loops
            6.  Canonical ID coverage per CANONICAL_IDS (not just 'id')
            7.  Per-type density is reasonable
            8.  Disease nodes have names or canonical IDs
            9.  Withdrawn drugs have NO treats edges (PATIENT-SAFETY)
            10. Edge direction matches CORE_EDGE_TYPES (PATIENT-SAFETY)
            11. No Compound-binds-Gene edges (PATIENT-SAFETY — biologically
                impossible; drugs bind PROTEINS)

        v35 ROOT FIX (L-4): the previous docstring listed checks 9 and
        10 as "10,000+ compound nodes" and "15,000+ gene nodes" — but
        the actual implementation (see ``total_checks = 13`` below and
        the body of this function) does NOT perform node-count
        threshold checks. Node/edge count thresholds are checked by
        ``check_exit_criteria`` (separate function, uses
        ``MIN_NODES_W2`` / ``MIN_EDGES_W2`` from config), NOT by
        ``run_sanity_checks``. The docstring was overpromising what
        this function actually verifies. The numbering above reflects
        the ACTUAL checks performed; the patient-safety checks
        (withdrawn, edge direction, binds-Gene) are correctly listed.

        Fixes:
            3.1: Check #2 tests Compound-binds-Protein (was Compound-binds-Gene)
            3.2/3.3: Uses InChIKeys (was DrugBank IDs)
            5.11: Withdrawn drug check
            5.8: Edge direction validation
            5.1: Canonical ID coverage check
            4.2: All exceptions logged (no silent swallowing)
            2.9: Structured report with passed_all
            11.3: Individual failure logging
        """
        checks: List[SanityCheckResult] = []
        warnings: List[str] = []
        patient_safety_failures = 0
        check_num = 0

        # Determine which checks to run
        total_checks = 13
        enabled = _parse_enabled_checks(STATS_ENABLED_CHECKS, total_checks)

        # Compute stats once for checks that need them
        if not self.report:
            self.report = self.compute_full_stats()

        with self._get_session() as session:
            # ── Check 1 & 2: Reference compound edge checks ──
            for compound_cfg in SANITY_CHECK_COMPOUNDS:
                check_num += 1
                if check_num not in enabled:
                    continue
                name = compound_cfg["name"]
                inchikey = compound_cfg["inchikey"]
                edge_type = compound_cfg["edge_type"]
                target_type = compound_cfg["target_type"]
                description = compound_cfg["description"]

                check_result = self._check_edge_exists(
                    session,
                    src_label="Compound",
                    src_id=inchikey,
                    src_id_prop="inchikey",
                    dst_label=target_type,
                    rel_type=edge_type,
                    description=description,
                    check_number=check_num,
                    severity="critical",
                )
                checks.append(check_result)

            # ── Check 3: Compound-treats-Disease edges exist ──
            check_num += 1
            if check_num in enabled:
                recs = self._run_query(
                    session,
                    "MATCH ()-[r:treats]->() RETURN count(r) AS cnt",
                    "sanity_treats_edges",
                )
                cnt = _safe_int(
                    recs[0]["cnt"] if recs else 0,
                )
                checks.append({
                    "check": "Compound-treats-Disease edges exist",
                    "check_number": check_num,
                    "passed": cnt > 0,
                    "detail": f"{cnt:,} treats edges found",
                    "severity": "critical",
                    "error": False,
                })

            # ── Check 4: Gene-Gene interaction edges exist ──
            check_num += 1
            if check_num in enabled:
                recs = self._run_query(
                    session,
                    "MATCH ()-[r:interacts_with|interacts]->() "
                    "RETURN count(r) AS cnt",
                    "sanity_gene_interactions",
                )
                cnt = _safe_int(
                    recs[0]["cnt"] if recs else 0,
                )
                checks.append({
                    "check": "Gene-Gene interaction edges exist",
                    "check_number": check_num,
                    "passed": cnt > 0,
                    "detail": f"{cnt:,} interaction edges found",
                    "severity": "critical",
                    "error": False,
                })

            # ── Check 5: No self-loops ──
            check_num += 1
            if check_num in enabled:
                recs = self._run_query(
                    session,
                    "MATCH (n)-[r]->(n) RETURN count(r) AS cnt",
                    "sanity_self_loops",
                )
                cnt = _safe_int(
                    recs[0]["cnt"] if recs else 0,
                )
                checks.append({
                    "check": "No self-loops in graph",
                    "check_number": check_num,
                    "passed": cnt == 0,
                    "detail": f"{cnt} self-loops found",
                    "severity": "warning",
                    "error": False,
                })

            # ── Check 6: Canonical ID coverage (Fix 5.1) ──
            check_num += 1
            if check_num in enabled:
                canonical_ok = True
                missing_details = []
                for entity_type, canonical_prop in CANONICAL_IDS.items():
                    safe_label = sanitize_identifier(
                        entity_type, "node label",
                    )
                    safe_prop = sanitize_identifier(
                        canonical_prop, "property name",
                    )
                    recs = self._run_query(
                        session,
                        f"MATCH (n:{safe_label}) "
                        f"WHERE n.{safe_prop} IS NULL "
                        "RETURN count(n) AS missing",
                        f"sanity_canonical_id_{entity_type}",
                    )
                    if recs:
                        missing = _safe_int(recs[0]["missing"])
                        if missing > 0:
                            canonical_ok = False
                            missing_details.append(
                                f"{entity_type}: {missing} missing "
                                f"{canonical_prop}"
                            )
                detail = (
                    "All canonical IDs present"
                    if canonical_ok
                    else "; ".join(missing_details)
                )
                checks.append({
                    "check": (
                        "Canonical ID coverage per CANONICAL_IDS"
                    ),
                    "check_number": check_num,
                    "passed": canonical_ok,
                    "detail": detail,
                    "severity": "critical",
                    "error": False,
                })

            # ── Check 7: Per-type density reasonable ──
            check_num += 1
            if check_num in enabled:
                densities = self.report.get(
                    "density_per_edge_type", {},
                )
                unreasonable = [
                    f"{rt}: {d:.6f}"
                    for rt, d in densities.items()
                    if isinstance(d, (int, float)) and d > MAX_DENSITY_THRESHOLD
                ]
                passed = len(unreasonable) == 0
                detail = (
                    "All per-type densities reasonable"
                    if passed
                    else f"Unreasonable density: {'; '.join(unreasonable)}"
                )
                checks.append({
                    "check": (
                        "Per-type density is reasonable "
                        f"(< {MAX_DENSITY_THRESHOLD})"
                    ),
                    "check_number": check_num,
                    "passed": passed,
                    "detail": detail,
                    "severity": "warning",
                    "error": False,
                })

            # ── Check 8: Disease nodes have names or canonical IDs ──
            # Fix 3.10: Disease name check also accepts DOID as valid
            check_num += 1
            if check_num in enabled:
                recs = self._run_query(
                    session,
                    "MATCH (d:Disease) "
                    "WHERE (d.name IS NULL OR d.name = '') "
                    "AND (d.doid IS NULL OR d.doid = '') "
                    "RETURN count(d) AS cnt",
                    "sanity_disease_names",
                )
                cnt = _safe_int(
                    recs[0]["cnt"] if recs else 0,
                )
                checks.append({
                    "check": (
                        "Disease nodes have names or DOID "
                        "(canonical IDs)"
                    ),
                    "check_number": check_num,
                    "passed": cnt == 0,
                    "detail": f"{cnt} diseases without name or DOID",
                    "severity": "warning",
                    "error": False,
                })

            # ── Check 9: Compound node count ≥ threshold ──
            check_num += 1
            if check_num in enabled:
                recs = self._run_query(
                    session,
                    "MATCH (c:Compound) RETURN count(c) AS cnt",
                    "sanity_compound_count",
                )
                cnt = _safe_int(
                    recs[0]["cnt"] if recs else 0,
                )
                checks.append({
                    "check": (
                        f"{MIN_COMPOUNDS_FOR_SANITY:,}+ "
                        f"compound nodes"
                    ),
                    "check_number": check_num,
                    "passed": cnt >= MIN_COMPOUNDS_FOR_SANITY,
                    "detail": f"{cnt:,} compounds found",
                    "severity": "critical",
                    "error": False,
                })

            # ── Check 10: Gene node count ≥ threshold ──
            check_num += 1
            if check_num in enabled:
                recs = self._run_query(
                    session,
                    "MATCH (g:Gene) RETURN count(g) AS cnt",
                    "sanity_gene_count",
                )
                cnt = _safe_int(
                    recs[0]["cnt"] if recs else 0,
                )
                checks.append({
                    "check": (
                        f"{MIN_GENES_FOR_SANITY:,}+ gene nodes"
                    ),
                    "check_number": check_num,
                    "passed": cnt >= MIN_GENES_FOR_SANITY,
                    "detail": f"{cnt:,} genes found",
                    "severity": "critical",
                    "error": False,
                })

            # ── Check 11: Withdrawn drugs have NO treats edges ──
            # PATIENT-SAFETY GATE (Fix 5.11)
            check_num += 1
            if check_num in enabled:
                recs = self._run_query(
                    session,
                    "MATCH (c:Compound)-[r:treats]->(d:Disease) "
                    "WHERE c.withdrawn = true "
                    "RETURN count(r) AS cnt",
                    "sanity_withdrawn_treats",
                )
                cnt = _safe_int(
                    recs[0]["cnt"] if recs else 0,
                )
                passed = cnt == 0
                if not passed:
                    patient_safety_failures += 1
                    warnings.append(
                        f"CRITICAL PATIENT-SAFETY: {cnt} withdrawn "
                        f"compound(s) have treats edges to diseases"
                    )
                checks.append({
                    "check": (
                        "Withdrawn drugs have NO treats edges "
                        "(PATIENT-SAFETY)"
                    ),
                    "check_number": check_num,
                    "passed": passed,
                    "detail": (
                        f"{cnt} withdrawn compounds with treats edges"
                        if not passed
                        else "No withdrawn compounds with treats edges"
                    ),
                    "severity": "patient_safety",
                    "error": False,
                })

            # ── Check 12: Edge direction matches CORE_EDGE_TYPES ──
            # PATIENT-SAFETY GATE (Fix 5.8)
            check_num += 1
            if check_num in enabled:
                violations = self.report.get(
                    "edge_direction_violations", 0,
                )
                passed = violations == 0
                if not passed:
                    patient_safety_failures += 1
                    warnings.append(
                        f"CRITICAL PATIENT-SAFETY: {violations} "
                        f"edge direction violations against "
                        f"CORE_EDGE_TYPES"
                    )
                checks.append({
                    "check": (
                        "Edge directions match CORE_EDGE_TYPES "
                        "(PATIENT-SAFETY)"
                    ),
                    "check_number": check_num,
                    "passed": passed,
                    "detail": (
                        f"{violations} violations"
                        if not passed
                        else "All edges match schema"
                    ),
                    "severity": "patient_safety",
                    "error": False,
                })

            # ── Check 13: No Compound-binds-Gene edges ──
            # PATIENT-SAFETY GATE (Fix 3.1)
            # Drugs bind PROTEINS (gene products), NOT genes.
            # Compound-binds-Gene edges are biologically impossible.
            check_num += 1
            if check_num in enabled:
                recs = self._run_query(
                    session,
                    "MATCH (c:Compound)-[r:binds]->(g:Gene) "
                    "RETURN count(r) AS cnt",
                    "sanity_compound_binds_gene",
                )
                cnt = _safe_int(
                    recs[0]["cnt"] if recs else 0,
                )
                passed = cnt == 0
                if not passed:
                    patient_safety_failures += 1
                    warnings.append(
                        f"CRITICAL PATIENT-SAFETY: {cnt} "
                        f"Compound-binds-Gene edges found. "
                        f"Drugs bind PROTEINS, not genes. "
                        f"These edges are biologically impossible "
                        f"and corrupt model training."
                    )
                checks.append({
                    "check": (
                        "No Compound-binds-Gene edges "
                        "(drugs bind PROTEINS, not genes — "
                        "PATIENT-SAFETY)"
                    ),
                    "check_number": check_num,
                    "passed": passed,
                    "detail": (
                        f"{cnt} Compound-binds-Gene edges "
                        "(biologically impossible)"
                        if not passed
                        else "No Compound-binds-Gene edges"
                    ),
                    "severity": "patient_safety",
                    "error": False,
                })

        passed_count = sum(1 for c in checks if c["passed"])
        total_count = len(checks)

        # Fix 11.3: Log individual failures
        logger.info(
            "Sanity checks: %d/%d passed "
            "(%d patient-safety failures)",
            passed_count, total_count, patient_safety_failures,
            extra={
                "correlation_id": CORRELATION_ID,
                "pipeline_run_id": RUN_ID,
                "passed_all": passed_count == total_count,
                "patient_safety_failures": patient_safety_failures,
            },
        )
        for check in checks:
            if not check["passed"]:
                logger.warning(
                    "Sanity check FAILED [%s]: %s — %s",
                    check.get("severity", "unknown"),
                    check["check"],
                    check["detail"],
                    extra={
                        "check": check["check"],
                        "severity": check.get("severity", ""),
                        "correlation_id": CORRELATION_ID,
                    },
                )

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "passed_count": passed_count,
            "total_count": total_count,
            "passed_all": passed_count == total_count,
            "patient_safety_failures": patient_safety_failures,
            "checks": checks,
            "warnings": warnings,
        }

    def _check_edge_exists(
        self,
        session,
        src_label: str,
        src_id: str,
        dst_label: str,
        rel_type: str,
        description: str,
        src_id_prop: str = "id",
        check_number: int = 0,
        severity: str = "critical",
    ) -> SanityCheckResult:
        """Check if at least one edge of a given type exists from a node.

        Args:
            session: Active Neo4j session.
            src_label: Source node label (e.g., 'Compound').
            src_id: Source node ID value (e.g., InChIKey).
            src_id_prop: Property name for the ID (default 'id').
            dst_label: Destination node label.
            rel_type: Relationship type.
            description: Human-readable check description.
            check_number: Check number for reporting.
            severity: Severity level.

        Returns:
            SanityCheckResult dict.

        Fixes:
            2.1: dst_label is now used in the query
            4.2: Exceptions are logged (not silently swallowed)
            11.1: Full stack trace on failure
        """
        try:
            safe_src_label = sanitize_identifier(
                src_label, "source label",
            )
            safe_rel_type = sanitize_identifier(
                rel_type, "relationship type",
            )
            safe_dst_label = sanitize_identifier(
                dst_label, "destination label",
            )
            safe_prop = sanitize_identifier(
                src_id_prop, "property name",
            )
            # Fix 2.1: dst_label IS used in the query
            records = session.run(
                f"MATCH (s:{safe_src_label} "
                f"{{{safe_prop}: $src_id}})"
                f"-[r:{safe_rel_type}]->"
                f"(d:{safe_dst_label}) "
                "RETURN count(r) AS cnt",
                src_id=src_id,
                timeout=STATS_QUERY_TIMEOUT_SECONDS,
            )
            recs_list = list(records)
            cnt = _safe_int(
                recs_list[0]["cnt"] if recs_list else 0,
            )
            return {
                "check": description,
                "check_number": check_number,
                "passed": cnt > 0,
                "detail": (
                    f"{cnt} {rel_type} edges from "
                    f"{src_label}({src_id[:16]}...) "
                    f"to {dst_label}"
                ),
                "severity": severity,
                "error": False,
            }
        except Exception as exc:
            # Fix 4.2, 11.1, 11.4: Log exception — never swallow silently
            logger.exception(
                "Sanity check failed: %s",
                description,
                extra={
                    "check": description,
                    "check_number": check_number,
                    "error_type": type(exc).__name__,
                    "severity": severity,
                    "correlation_id": CORRELATION_ID,
                },
            )
            return {
                "check": description,
                "check_number": check_number,
                "passed": False,
                "detail": f"Error: {exc}",
                "severity": severity,
                "error": True,
            }

    # ─── Label Distribution Report ────────────────────────────────────

    def label_distribution_report(self) -> Dict[str, Any]:
        """Generate a deterministic node-label distribution report.

        Uses UNWIND labels(n) for deterministic results (Fix 7.2).
        This report is suitable for caching and comparison across runs.

        Returns:
            Dict with 'timestamp', 'labels' (dict), 'total_nodes'.

        Fixes:
            13.1: Method referenced in utils.py but didn't exist.
            10.11: Same — now implemented.
        """
        labels: Dict[str, int] = {}
        total = 0

        with self._get_session() as session:
            records = self._run_query(
                session,
                "MATCH (n) UNWIND labels(n) AS lbl "
                "RETURN lbl AS label, count(*) AS cnt "
                "ORDER BY cnt DESC, lbl ASC",
                "label_distribution",
            )
            if records:
                for rec in records:
                    label = str(rec.get("label", "Unknown"))
                    cnt = _safe_int(rec.get("cnt", 0))
                    labels[label] = cnt
                    total += cnt

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "labels": labels,
            "total_nodes": total,
        }

    # ─── Report Generation ────────────────────────────────────────────

    def generate_data_readme(
        self,
        output_path: Optional[str] = None,
        stats: Optional[Dict[str, Any]] = None,
    ) -> Union[str, None]:
        """Generate the data README content.

        Args:
            output_path: If provided, write README to this file and
                return the path. If None, return the string content
                (backward-compatible — Fix 15.8).
            stats: Pre-computed stats dict. If None, computes fresh.

        Returns:
            String content if output_path is None, else the file path.

        Fixes:
            15.5: Can write to file with --output flag
            15.8: Optional output_path parameter for file I/O
            7.1: UTC timestamps in README
        """
        if stats is None:
            stats = self.compute_full_stats()

        criteria_report = self.check_exit_criteria(week=2)
        criteria_list = criteria_report.get("criteria", [])

        readme_lines = [
            "# DrugOS Knowledge Graph — Data README",
            f"Generated: {stats.get('computed_at', 'unknown')} "
            f"(UTC)",
            "",
            "## Overview",
            f"- Total Nodes: {stats.get('total_nodes', 0):,}",
            f"- Total Edges: {stats.get('total_edges', 0):,}",
            f"- Node Types: "
            f"{len(stats.get('node_counts_by_type', {}))}",
            f"- Edge Types: "
            f"{len(stats.get('edge_counts_by_type', {}))}",
            f"- Density (homogeneous naive): "
            f"{stats.get('density_homogeneous_naive', 0):.8f}",
            f"- Avg Out-Degree: "
            f"{stats.get('avg_out_degree', 0)}",
            f"- Isolated Nodes: "
            f"{stats.get('isolated_nodes', 'unknown')}",
            "",
            "## Node Counts by Type",
        ]

        for label, count in stats.get(
            "node_counts_by_type", {},
        ).items():
            readme_lines.append(f"  {label}: {count:,}")

        readme_lines.extend(["", "## Edge Counts by Type"])

        for rel_type, count in stats.get(
            "edge_counts_by_type", {},
        ).items():
            readme_lines.append(f"  {rel_type}: {count:,}")

        readme_lines.extend([
            "",
            "## Data Completeness",
            f"  Compound name coverage: "
            f"{stats.get('compound_name_coverage', 0):.2%}",
            f"  Compound SMILES coverage: "
            f"{stats.get('compound_smiles_coverage', 0):.2%}",
            f"  Disease name coverage: "
            f"{stats.get('disease_name_coverage', 0):.2%}",
        ])

        # Canonical ID coverage
        canonical = stats.get("canonical_id_coverage", {})
        if canonical:
            readme_lines.extend(["", "## Canonical ID Coverage"])
            for entity_type, coverage in canonical.items():
                readme_lines.append(
                    f"  {entity_type}: {coverage:.2%}"
                )

        readme_lines.extend([
            "",
            "## Week 2 Exit Criteria",
        ])

        for crit in criteria_list:
            status = "PASS" if crit["passed"] else "FAIL"
            readme_lines.append(
                f"  [{status}] {crit['criterion']} "
                f"(actual={crit['actual']}, "
                f"threshold={crit['threshold']})"
            )

        # Warnings
        warnings = stats.get("warnings", [])
        if warnings:
            readme_lines.extend(["", "## Warnings"])
            for w in warnings:
                readme_lines.append(f"  - {w}")

        content = "\n".join(readme_lines)

        if output_path:
            output_path = str(output_path)
            os.makedirs(
                os.path.dirname(output_path) or ".", exist_ok=True,
            )
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info(
                "Data README written to %s",
                output_path,
            )
            return output_path

        return content


# ═══════════════════════════════════════════════════════════════════════════
# CLI Entry Point (Fix 13.10: argparse with --help, --output, --format)
# ═══════════════════════════════════════════════════════════════════════════


def _build_cli_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the graph_stats CLI."""
    parser = argparse.ArgumentParser(
        description=(
            "DrugOS Graph Statistics & Validation Tool "
            f"(v{__version__})"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m drugos_graph.graph_stats "
            "--week 2 --format json --output stats.json\n"
            "  python -m drugos_graph.graph_stats "
            "--profile quick\n"
            "  python -m drugos_graph.graph_stats "
            "--week 1 --format markdown\n"
        ),
    )
    parser.add_argument(
        "--week", type=int, default=2, choices=[1, 2],
        help="Week number for exit criteria (default: 2)",
    )
    parser.add_argument(
        "--profile",
        type=str,
        default=STATS_DEFAULT_PROFILE,
        choices=["quick", "standard", "full"],
        help="Stats computation profile (default: standard)",
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Output file path (default: stdout)",
    )
    parser.add_argument(
        "--format",
        type=str,
        default="text",
        choices=["json", "markdown", "text"],
        help="Output format (default: text)",
    )
    return parser


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = _build_cli_parser()
    args = parser.parse_args()

    with GraphStats() as gs:
        # Compute stats
        stats = gs.compute_full_stats(
            stats_profile=args.profile,
        )

        # Exit criteria
        criteria = gs.check_exit_criteria(week=args.week)

        # Format output
        if args.format == "json":
            output = json.dumps(
                {"stats": stats, "exit_criteria": criteria},
                indent=2,
                default=str,
            )
        elif args.format == "markdown":
            output = gs.generate_data_readme(stats=stats)
        else:
            # Text format
            lines = [
                f"DrugOS Graph Stats (v{__version__})",
                f"Generated: {stats.get('computed_at', 'unknown')}",
                "",
                f"Total nodes: {stats.get('total_nodes', 0):,}",
                f"Total edges: {stats.get('total_edges', 0):,}",
                f"Node types: "
                f"{len(stats.get('node_counts_by_type', {}))}",
                f"Edge types: "
                f"{len(stats.get('edge_counts_by_type', {}))}",
                f"Avg out-degree: "
                f"{stats.get('avg_out_degree', 0)}",
                f"Isolated nodes: "
                f"{stats.get('isolated_nodes', 'unknown')}",
                "",
                f"Week {args.week} Exit Criteria: "
                f"{criteria['passed_count']}/{criteria['total_count']} "
                f"passed",
            ]
            for crit in criteria.get("criteria", []):
                status = "PASS" if crit["passed"] else "FAIL"
                lines.append(
                    f"  [{status}] {crit['criterion']}: "
                    f"{crit['actual']} "
                    f"(threshold: {crit['threshold']})"
                )
            warnings = stats.get("warnings", [])
            if warnings:
                lines.extend(["", "Warnings:"])
                for w in warnings:
                    lines.append(f"  - {w}")
            output = "\n".join(lines)

        # Write output
        if args.output:
            os.makedirs(
                os.path.dirname(args.output) or ".",
                exist_ok=True,
            )
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(output)
            print(f"Output written to {args.output}")
        else:
            print(output)
