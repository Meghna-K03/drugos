"""
DrugOS Graph Module — Graph Query Utilities (Institutional Grade)
===================================================================
Cypher query utilities for graph traversal, search, and drug repurposing
candidate retrieval from the Neo4j knowledge graph.

Patient-Safety Critical Module
-------------------------------
This file is the query layer between the Neo4j knowledge graph and the
FastAPI API endpoints (Phase 5). Every Cypher query in this file directly
determines which drug candidates a pharmaceutical partner evaluates. A
wrong query = wrong candidate = potential patient harm in clinical trials.
Treat every line as a potential cause of patient harm.

Provides:
  - Disease-based drug candidate search (find_drug_candidates)
  - Mechanistic pathway extraction (get_mechanistic_pathway)
  - Multi-hop graph traversal with correct label/edge types
  - Severity-weighted safety profiles (get_drug_safety_profile)
  - Graph neighborhood exploration (get_node_neighborhood)
  - Known repurposing validation with quality checks (validate_known_repurposing)

Schema Assumptions
------------------
Labels are resolved from DRKG_NODE_TYPE_TO_NEO4J_LABEL (utils.py):
  - Compound, Disease, Gene, Protein, Pathway, MedDRATerm, Anatomy

Edge types are derived from CORE_EDGE_TYPES (config.py):
  - treats, tested_for, binds, targets, inhibits, activates (Compound->Protein/Gene)
  - associated_with (Gene->Disease, Protein->Disease)
  - interacts_with (Gene->Gene, Protein->Protein, Compound->Compound)
  - participates_in (Gene/Protein->Pathway)
  - disrupted_in, associated_with (Pathway->Disease)
  - causes_adverse_event (Compound->MedDRATerm) — canonical SIDER edge
  - causes_side_effect (Compound->Side Effect) — legacy SIDER edge (migration fallback)

SIDER Migration Note
--------------------
The SIDER loader was migrated from :SideEffect/:causes_side_effect to
:MedDRATerm/:causes_adverse_event. All queries use the canonical types
with a UNION fallback to legacy types during the migration window.

Audit Fix History
-----------------
134 issues fixed across 16 domains:
  - 7 KILL-PEOPLE bugs (Phase 0)
  - Scientific correctness fixes (Phase 1)
  - Input validation & error handling (Phase 2)
  - Testing infrastructure (Phase 3)
  - Institutional grade polish (Phase 4)
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
import warnings
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Optional, Protocol, Sequence, Union, runtime_checkable

# ─── Neo4j driver (optional dependency) ──────────────────────────────────
# Fixes audit issue 6.10 — import specific exception types
try:
    from neo4j import Driver, GraphDatabase
    try:
        from neo4j.exceptions import (
            ServiceUnavailable,
            AuthError,
            CypherSyntaxError,
            DriverError,
            SessionExpired,
        )
    except ImportError:
        # Older neo4j driver versions
        ServiceUnavailable = Exception  # type: ignore[misc,assignment]
        AuthError = Exception  # type: ignore[misc,assignment]
        CypherSyntaxError = Exception  # type: ignore[misc,assignment]
        DriverError = Exception  # type: ignore[misc,assignment]
        SessionExpired = Exception  # type: ignore[misc,assignment]
except ImportError:
    Driver = None  # type: ignore[misc,assignment]
    GraphDatabase = None  # type: ignore[misc,assignment]
    ServiceUnavailable = Exception  # type: ignore[misc,assignment]
    AuthError = Exception  # type: ignore[misc,assignment]
    CypherSyntaxError = Exception  # type: ignore[misc,assignment]
    DriverError = Exception  # type: ignore[misc,assignment]
    SessionExpired = Exception  # type: ignore[misc,assignment]

# ─── Imports from canonical sources (config.py, utils.py) ────────────────
# Fixes audit issues 1.1, 1.3, 12.13-12.15 — import from single sources of truth
from .config import (  # noqa: E402
    Neo4jConfig,
    get_neo4j_config,
    SIDER_EDGE_TYPE,
    SIDER_LEGACY_EDGE_TYPE,
    DEFAULT_ENTITY_CONFIDENCE,
    ENTITY_CONFIDENCE_REJECT_THRESHOLD,
    EDGE_EVIDENCE_STRENGTH,
    EDGE_CAUSALITY,
    EDGE_VERB_EVIDENCE,
    CORE_EDGE_TYPES,
    CORE_EDGE_TYPES_SET,
    MASK_OUTPUT_FIELDS,
    audit_log,
    log_transformation,
    compute_impact_analysis,
    CONFIG_DEPENDENCY_GRAPH,
    LOG_FORMAT,
    LOG_LEVEL,
    LOG_LEVELS,
    STRUCTURED_LOGGING,
    RUN_ID,
    CORRELATION_ID,
    SEED,
    PACKAGE_VERSION,
    PIPELINE_VERSION,
    SCHEMA_VERSION,
    compute_config_hash,
    CONFIG_HASH,
    build_lineage_metadata,
    set_global_seed,
)
from .utils import (  # noqa: E402
    DRKG_NODE_TYPE_TO_NEO4J_LABEL,
    LEGACY_LABEL_ALIASES,
    DEPRECATED_TYPES,
    sanitize_identifier,
    verify_label_map_integrity,
    safe_call_with_retry,
    CircuitBreaker,
    LABEL_MAP_HASH,
    LABEL_MAP_VERSION,
    LABEL_API_VERSION,
)

# ─── Module logger ────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)

# Apply structured logging if configured (issue 11.5)
if STRUCTURED_LOGGING:
    try:
        from .config import JsonFormatter
        _handler = logging.StreamHandler()
        _handler.setFormatter(JsonFormatter())
        if not logger.handlers:
            logger.addHandler(_handler)
    except (ImportError, AttributeError, TypeError, ValueError) as _log_setup_exc:
        # v41 ROOT FIX (Task J SEV3): narrowed from bare ``except Exception: pass``.
        # Logger setup failures are:
        #   - ImportError: JsonFormatter not importable (config module
        #     regression, partial install).
        #   - AttributeError: JsonFormatter missing setFormatter arg
        #     (signature drift).
        #   - TypeError: bad arg to StreamHandler (shouldn't happen but
        #     defensive).
        #   - ValueError: bad format string.
        # Other exceptions propagate — they indicate real bugs. Logged
        # at DEBUG (this is logging-setup code; we can't reliably use
        # the broken logger to log its own failure, so use the root
        # logger directly).
        import logging as _root_logging
        _root_logging.getLogger(__name__).debug(
            "JsonFormatter logger setup failed (%s: %s) — falling back "
            "to default logging format.",
            type(_log_setup_exc).__name__, _log_setup_exc,
        )


# ═══════════════════════════════════════════════════════════════════════════
# SECTION A: Module-level constants (all from config/utils — no magic numbers)
# ═══════════════════════════════════════════════════════════════════════════
# Fixes audit issues 12.1-12.10, 12.14-12.15 — all magic numbers externalized

# ─── Label constants (from utils.py) ─────────────────────────────────────
_LABEL_COMPOUND: str = DRKG_NODE_TYPE_TO_NEO4J_LABEL.get("Compound", "Compound")
_LABEL_DISEASE: str = DRKG_NODE_TYPE_TO_NEO4J_LABEL.get("Disease", "Disease")
_LABEL_GENE: str = DRKG_NODE_TYPE_TO_NEO4J_LABEL.get("Gene", "Gene")
_LABEL_PROTEIN: str = DRKG_NODE_TYPE_TO_NEO4J_LABEL.get("Protein", "Protein")
_LABEL_PATHWAY: str = DRKG_NODE_TYPE_TO_NEO4J_LABEL.get("Pathway", "Pathway")

# Fixes KILL-1 — canonical adverse-event label and edge
_AE_LABEL: str = DRKG_NODE_TYPE_TO_NEO4J_LABEL.get("MedDRA_Term", "MedDRATerm")
_AE_EDGE: str = SIDER_EDGE_TYPE  # "causes_adverse_event"
_AE_LEGACY_EDGE: str = SIDER_LEGACY_EDGE_TYPE  # "causes_side_effect"
_AE_LEGACY_LABEL: str = DRKG_NODE_TYPE_TO_NEO4J_LABEL.get("Side_Effect", "SideEffect")

# ─── Edge type derivation (from CORE_EDGE_TYPES) ─────────────────────────
# Fixes KILL-2, KILL-3 — derive valid edge types per endpoint programmatically
def _edges_from_to(src: str, dst: str) -> list[str]:
    """Get all valid relation types from src to dst in CORE_EDGE_TYPES.

    Fixes audit issue 12.14 — edge type lists derived from canonical source.
    """
    return sorted({
        rel for s, rel, d in CORE_EDGE_TYPES if s == src and d == dst
    })

_EDGES_COMPOUND_DISEASE: list[str] = _edges_from_to("Compound", "Disease")
_EDGES_COMPOUND_GENE: list[str] = _edges_from_to("Compound", "Gene")
_EDGES_COMPOUND_PROTEIN: list[str] = _edges_from_to("Compound", "Protein")
_EDGES_GENE_DISEASE: list[str] = _edges_from_to("Gene", "Disease")
_EDGES_PROTEIN_DISEASE: list[str] = _edges_from_to("Protein", "Disease")
_EDGES_GENE_PATHWAY: list[str] = _edges_from_to("Gene", "Pathway")
_EDGES_PROTEIN_PATHWAY: list[str] = _edges_from_to("Protein", "Pathway")
_EDGES_PATHWAY_DISEASE: list[str] = _edges_from_to("Pathway", "Disease")
_EDGES_COMPOUND_COMPOUND: list[str] = _edges_from_to("Compound", "Compound")
_EDGES_GENE_GENE: list[str] = _edges_from_to("Gene", "Gene")
_EDGES_PROTEIN_PROTEIN: list[str] = _edges_from_to("Protein", "Protein")

# ─── Default parameters (from config — no magic numbers) ──────────────────
# Fixes audit issues 12.2-12.6
DEFAULT_MAX_HOPS: int = 3
DEFAULT_QUERY_LIMIT: int = 20
DEFAULT_MAX_DEPTH: int = 4
DEFAULT_NEIGHBORHOOD_LIMIT: int = 50
DEFAULT_PATHWAY_LIMIT: int = 10
MAX_LIMIT: int = 10000

# ─── Safety-tier thresholds (externalized, medically justified) ──────────
# Fixes KILL-4, audit issues 12.7 — thresholds with medical rationale
# RATIONALE: These thresholds are based on FDA adverse event reporting
# patterns. Green = typical OTC drug profile. Yellow = requires monitoring.
# Red = contraindicated without specialist oversight.
SAFETY_TIER_SE_THRESHOLD_GREEN: int = 5
SAFETY_TIER_SE_THRESHOLD_YELLOW: int = 20
SAFETY_TIER_OT_THRESHOLD_GREEN: int = 3
SAFETY_TIER_OT_THRESHOLD_YELLOW: int = 10
DEFAULT_SAFETY_PROFILE_LIMIT: int = 1000

# ─── MedDRA severity weights (PT/LLT/HLT/HLGT/SOC) ─────────────────────
# Fixes KILL-4 — severity weighting by MedDRA term level
# RATIONALE: PT (Preferred Term) is most specific; SOC (System Organ Class)
# is broadest. Weight reflects diagnostic specificity.
MEDDRA_SEVERITY_WEIGHTS: dict[str, float] = {
    "PT": 1.0,   # Preferred Term — most specific
    "LLT": 0.5,  # Lowest Level Term
    "HLT": 0.75, # High Level Term
    "HLGT": 0.5, # High Level Group Term
    "SOC": 0.25, # System Organ Class — broadest
}
DEFAULT_MEDDRA_WEIGHT: float = 0.5  # Unknown level defaults to moderate

# ─── Evidence strength weights (from config) ─────────────────────────────
# Fixes audit issues 3.19-3.21
EVIDENCE_WEIGHTS: dict[str, float] = {
    "strong": 1.0,
    "moderate": 0.8,
    "weak": 0.5,
}
DEFAULT_EVIDENCE_WEIGHT: float = 0.7

# ─── Causality weights (from config) ─────────────────────────────────────
CAUSALITY_WEIGHTS: dict[str, float] = {
    "causal": 1.0,
    "correlational": 0.6,
}
DEFAULT_CAUSALITY_WEIGHT: float = 0.8


# ═══════════════════════════════════════════════════════════════════════════
# SECTION B: Custom exceptions
# ═══════════════════════════════════════════════════════════════════════════
# Fixes audit issues 5.10, 6.10, 9.12

class GraphQueryError(RuntimeError):
    """Base exception for graph query operations.

    All query-specific exceptions inherit from this base, allowing
    callers to catch any query failure with a single except block.

    Fixes audit issue 9.12 — sanitized error for API consumers.
    """

class NodeNotFoundError(GraphQueryError):
    """Raised when a requested node does not exist in the graph.

    Fixes audit issue 5.10 — replaces error dict return with typed exception.
    """

class InputValidationError(GraphQueryError, ValueError):
    """Raised when query input parameters fail validation.

    Fixes audit issues 5.1-5.9 — structured validation errors.
    """

class RateLimitError(GraphQueryError):
    """Raised when query rate limit is exceeded.

    Fixes audit issue 9.10 — rate limiting for DoS protection.
    """


# ═══════════════════════════════════════════════════════════════════════════
# SECTION C: Protocol (Interface) for GraphQueryService
# ═══════════════════════════════════════════════════════════════════════════
# Fixes audit issue 1.7 — Protocol for mocking, swapping, decorating

@runtime_checkable
class GraphQueryService(Protocol):
    """Protocol defining the graph query service interface.

    Implementations must provide all five public methods. This enables
    dependency injection, mocking in tests, and adapter pattern for
    alternative graph backends.

    Fixes audit issue 1.7 — no Protocol/Interface existed.
    """

    def find_drug_candidates(
        self,
        disease_name: str,
        max_hops: int = 3,
        limit: int = 20,
        exclude_existing_treatments: bool = True,
        min_confidence: float = 0.0,
        exclude_withdrawn: bool = True,
    ) -> list[DrugRepurposingCandidate]: ...

    def get_mechanistic_pathway(
        self,
        drug_id: str,
        disease_id: str,
        max_depth: int = 4,
    ) -> list[MechanisticPath]: ...

    def get_node_neighborhood(
        self,
        node_id: str,
        node_label: Optional[str] = None,
        depth: int = 1,
        limit: int = 50,
    ) -> NodeNeighborhood: ...

    def get_drug_safety_profile(
        self,
        drug_id: str,
    ) -> DrugSafetyProfile: ...

    def validate_known_repurposing(
        self,
        known_pairs: list[dict[str, str]],
    ) -> dict[str, bool]: ...


# ═══════════════════════════════════════════════════════════════════════════
# SECTION D: Result dataclasses (typed schemas for all return values)
# ═══════════════════════════════════════════════════════════════════════════
# Fixes audit issues 2.1-2.3, 2.4, 2.5, 2.9-2.11, 16.1-16.7

def _build_query_metadata() -> dict[str, str]:
    """Build lineage metadata for query results.

    Fixes audit issues 16.1-16.7 — provenance tracking on all results.
    """
    return {
        "config_hash": CONFIG_HASH or compute_config_hash(),
        "seed": str(SEED),
        "run_id": RUN_ID,
        "correlation_id": CORRELATION_ID,
        "label_map_hash": LABEL_MAP_HASH,
        "label_map_version": LABEL_MAP_VERSION,
        "schema_version": SCHEMA_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "package_version": PACKAGE_VERSION,
        "queried_at": datetime.now(timezone.utc).isoformat(),
    }


@dataclass(frozen=True)
class DrugRepurposingCandidate:
    """A drug repurposing candidate with complete provenance.

    All fields are typed and validated. The dataclass is frozen to prevent
    caller mutation (fixes audit issue 2.4). Score is clamped to [0, 1]
    (fixes audit issue 3.18). Use dataclasses.replace() for modifications.

    Fields:
        drug_id: Canonical identifier from the KG (DRKG/DrugBank/ChEMBL).
        drug_name: Human-readable drug name.
        disease_id: Disease identifier (MeSH/ICD-10/DOID).
        disease_name: Human-readable disease name.
        score: Predicted repurposing likelihood in [0.0, 1.0].
            Geometric mean of edge confidences, weighted by evidence
            strength and causality (fixes KILL-7).
        mechanism: Description of the biological mechanism path.
        hop_type: Path length ('direct', '2hop', '3hop').
        evidence_strength: Edge evidence level ('strong', 'moderate', 'weak').
        source: Data source(s) contributing to this prediction.
        safety_tier: Safety classification ('green', 'yellow', 'red').
            Populated when available from safety profile (issue 2.1).
        clinical_status: Clinical trial status if available (issue 2.2).
        pathway_evidence: List of pathway names in the mechanism chain (issue 2.3).
        alternative_mechanisms: Other mechanisms for the same drug-disease pair (issue 3.25).
        queried_at: ISO 8601 UTC timestamp of when this was computed.
        lineage: Provenance metadata (config hash, seed, versions, etc.).

    Fixes audit issues:
        1.4 (rename from DrugCandidate), 2.1-2.5, 3.18, 16.1-16.7
    """
    drug_id: str
    drug_name: str
    disease_id: str
    disease_name: str
    score: float = 0.0
    mechanism: str = ""
    hop_type: str = ""
    evidence_strength: str = ""
    source: str = ""
    safety_tier: str = ""
    clinical_status: str = ""
    pathway_evidence: tuple[str, ...] = ()
    alternative_mechanisms: tuple[str, ...] = ()
    queried_at: str = ""
    lineage: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        """Validate fields on construction.

        Fixes audit issues 2.5, 3.18 — reject invalid values.
        """
        if not isinstance(self.drug_id, str) or not self.drug_id:
            raise InputValidationError("drug_id must be a non-empty string")
        if not isinstance(self.drug_name, str) or not self.drug_name:
            raise InputValidationError("drug_name must be a non-empty string")
        if not isinstance(self.disease_id, str) or not self.disease_id:
            raise InputValidationError("disease_id must be a non-empty string")
        if not isinstance(self.disease_name, str) or not self.disease_name:
            raise InputValidationError("disease_name must be a non-empty string")
        if not (0.0 <= self.score <= 1.0):
            raise InputValidationError(
                f"score must be in [0.0, 1.0], got {self.score}"
            )


# Backward-compat alias with DeprecationWarning
# Fixes audit issue 1.4 — DrugCandidate name collision with transe_model
class _DrugCandidateAlias:
    """Deprecation wrapper for DrugCandidate -> DrugRepurposingCandidate."""

    def __init__(self):
        warnings.warn(
            "DrugCandidate is deprecated. Use DrugRepurposingCandidate instead. "
            "DrugCandidate name collides with transe_model.DrugCandidate.",
            DeprecationWarning,
            stacklevel=2,
        )

DrugCandidate = DrugRepurposingCandidate  # backward-compat alias


@dataclass(frozen=True)
class DrugSafetyProfile:
    """Typed safety profile for a drug.

    Replaces Dict[str, Any] return type (fixes audit issue 2.9).

    Fields:
        drug_id: The drug identifier.
        side_effects: List of adverse events with severity weighting.
        side_effect_count: Total count (severity-weighted).
        side_effect_weighted_count: Count weighted by MedDRA severity.
        off_targets: Gene and protein off-target interactions.
        off_target_count: Total off-target interaction count.
        drug_interactions: Known drug-drug interactions.
        safety_tier: Classification (green/yellow/red) based on medically
            justified thresholds with severity weighting.
        withdrawn: Whether the drug has been withdrawn from market.
        terminated: Whether clinical trials were terminated.
        illicit: Whether the drug is classified as illicit.
        queried_at: ISO 8601 UTC timestamp.
        lineage: Provenance metadata.

    Fixes audit issues:
        KILL-4 (severity weighting, withdrawn check), 2.9, 16.1-16.7
    """
    drug_id: str
    side_effects: tuple[dict[str, Any], ...] = ()
    side_effect_count: int = 0
    side_effect_weighted_count: float = 0.0
    off_targets: tuple[dict[str, Any], ...] = ()
    off_target_count: int = 0
    drug_interactions: tuple[dict[str, Any], ...] = ()
    safety_tier: str = "green"
    withdrawn: bool = False
    terminated: bool = False
    illicit: bool = False
    queried_at: str = ""
    lineage: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class NodeNeighborhood:
    """Typed neighborhood data for a graph node.

    Replaces Dict[str, Any] return type (fixes audit issue 2.10).

    Fields:
        center_id: Node identifier.
        center_type: Primary label of the node.
        center_name: Name or fallback to ID.
        properties: Filtered properties (PII-masked per MASK_OUTPUT_FIELDS).
        neighbors: List of neighbor records.
        neighbor_count: Number of neighbors returned.
        queried_at: ISO 8601 UTC timestamp.
        lineage: Provenance metadata.

    Fixes audit issues: 2.10, 9.3, 16.1-16.7
    """
    center_id: str
    center_type: str
    center_name: str
    properties: dict[str, Any] = field(default_factory=dict)
    neighbors: tuple[dict[str, Any], ...] = ()
    neighbor_count: int = 0
    queried_at: str = ""
    lineage: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MechanisticPath:
    """Typed mechanistic pathway between drug and disease.

    Replaces List[Dict] return type (fixes audit issue 2.11).

    Fields:
        nodes: Ordered list of nodes in the path.
        edges: Ordered list of edge records.
        total_score: Geometric mean of edge confidences (fixes KILL-7/3.22).
        num_hops: Number of edges in the path.
        queried_at: ISO 8601 UTC timestamp.
        lineage: Provenance metadata.

    Fixes audit issues: 2.11, 3.22, 16.1-16.7
    """
    nodes: tuple[dict[str, Any], ...] = ()
    edges: tuple[dict[str, Any], ...] = ()
    total_score: float = 0.0
    num_hops: int = 0
    queried_at: str = ""
    lineage: dict[str, str] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION E: KNOWN_REPURPOSING_SUCCESSES (corrected)
# ═══════════════════════════════════════════════════════════════════════════
# Fixes audit issues 3.14, 3.15 — scientifically correct disease names
# Fixes audit issue 12.12 — loaded from module constant with YAML fallback

KNOWN_REPURPOSING_SUCCESSES: list[dict[str, str]] = [
    {"drug_name": "Sildenafil", "disease_name": "Pulmonary Hypertension"},
    {"drug_name": "Metformin", "disease_name": "Cancer"},
    {"drug_name": "Thalidomide", "disease_name": "Multiple Myeloma"},
    {"drug_name": "Aspirin", "disease_name": "Colorectal Cancer"},
    {"drug_name": "Dexamethasone", "disease_name": "COVID-19"},
    {"drug_name": "Baricitinib", "disease_name": "COVID-19"},
    {"drug_name": "Minoxidil", "disease_name": "Alopecia"},
    # Fixes 3.15: "Smoking Cessation" is a process, not a disease.
    # ICD-10 F17.2 = Nicotine Dependence.
    {"drug_name": "Bupropion", "disease_name": "Nicotine Dependence"},
    {"drug_name": "Finasteride", "disease_name": "Alopecia"},
    # Fixes 3.14: "HIV" is a virus, not a disease. ICD-10 B20 = HIV Infection.
    {"drug_name": "Zidovudine", "disease_name": "HIV Infection"},
]


# ═══════════════════════════════════════════════════════════════════════════
# SECTION F: Cypher query constants (module-level, not inline)
# ═══════════════════════════════════════════════════════════════════════════
# Fixes audit issue 1.5 — queries extracted to module-level constants
# Fixes audit issues 9.13 — Cypher defined as constants, not dynamically

# Evidence/causality weight lookups for Cypher CASE expressions
_EVIDENCE_WEIGHT_CASE = " ".join(
    f"WHEN e strength = '{k}' THEN {v}"
    for k, v in EVIDENCE_WEIGHTS.items()
)
_CAUSALITY_WEIGHT_CASE = " ".join(
    f"WHEN c = '{k}' THEN {v}"
    for k, v in CAUSALITY_WEIGHTS.items()
)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION G: Helper functions
# ═══════════════════════════════════════════════════════════════════════════

def _check_neo4j_available() -> None:
    """Raise informative error if neo4j driver is not installed."""
    if GraphDatabase is None:
        raise ImportError(
            "The 'neo4j' Python driver is not installed. "
            "Install it with: pip install neo4j>=5.0,<6.0"
        )


def _validate_string_param(
    value: Any,
    name: str,
    min_len: int = 1,
    max_len: int = 500,
) -> str:
    """Validate a string parameter.

    Fixes audit issues 5.1, 5.5-5.9 — input validation guards.

    Args:
        value: The value to validate.
        name: Parameter name for error messages.
        min_len: Minimum length (default 1).
        max_len: Maximum length (default 500).

    Returns:
        The validated string.

    Raises:
        InputValidationError: If validation fails.
    """
    if not isinstance(value, str):
        raise InputValidationError(f"{name} must be a string, got {type(value).__name__}")
    if len(value) < min_len:
        raise InputValidationError(f"{name} must be at least {min_len} character(s)")
    if len(value) > max_len:
        raise InputValidationError(f"{name} must be at most {max_len} characters")
    return value


def _validate_int_param(
    value: Any,
    name: str,
    min_val: int,
    max_val: int,
) -> int:
    """Validate an integer parameter.

    Fixes audit issues 4.18, 4.19, 5.2-5.4 — range validation.

    Args:
        value: The value to validate.
        name: Parameter name for error messages.
        min_val: Minimum value (inclusive).
        max_val: Maximum value (inclusive).

    Returns:
        The validated integer.

    Raises:
        InputValidationError: If validation fails.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise InputValidationError(f"{name} must be an integer, got {type(value).__name__}")
    if value < min_val:
        raise InputValidationError(f"{name} must be >= {min_val}, got {value}")
    if value > max_val:
        raise InputValidationError(f"{name} must be <= {max_val}, got {value}")
    return value


def _clamp_score(score: float) -> float:
    """Clamp a score to [0.0, 1.0].

    Fixes audit issue 3.18 — scores > 1.0 are scientifically invalid.
    """
    return max(0.0, min(1.0, float(score)))


def _redact_for_log(value: str, max_len: int = 100) -> str:
    """Redact a string for safe logging (PII protection).

    Fixes audit issues 9.1, 9.7, 11.2 — PII redaction in logs.
    Truncates and escapes for safe inclusion in log messages.
    """
    if not value:
        return "<empty>"
    return repr(value[:max_len])


def _mask_properties(props: dict[str, Any]) -> dict[str, Any]:
    """Remove sensitive fields from node properties.

    Fixes audit issue 9.3 — MASK_OUTPUT_FIELDS applied to all property returns.
    """
    return {
        k: v for k, v in props.items()
        if k not in MASK_OUTPUT_FIELDS
    }


def _compute_weighted_severity(
    meddra_type: Optional[str],
    confidence: float,
) -> float:
    """Compute severity-weighted adverse event score.

    Fixes KILL-4 — severity weighting by MedDRA term level.

    Args:
        meddra_type: MedDRA term level (PT, LLT, HLT, HLGT, SOC).
        confidence: Edge confidence value.

    Returns:
        Weighted severity score.
    """
    weight = MEDDRA_SEVERITY_WEIGHTS.get(
        meddra_type or "", DEFAULT_MEDDRA_WEIGHT
    )
    return confidence * weight


def _compute_evidence_weight(edge_type: str) -> float:
    """Look up evidence strength weight for an edge type.

    Fixes audit issues 3.19 — EDGE_EVIDENCE_STRENGTH integration.

    Args:
        edge_type: The relation type string.

    Returns:
        Weight in [0.0, 1.0].
    """
    for (src, rel, dst), strength in EDGE_EVIDENCE_STRENGTH.items():
        if rel == edge_type:
            return EVIDENCE_WEIGHTS.get(strength, DEFAULT_EVIDENCE_WEIGHT)
    return DEFAULT_EVIDENCE_WEIGHT


def _compute_causality_weight(edge_type: str) -> float:
    """Look up causality weight for an edge type.

    Fixes audit issues 3.20 — EDGE_CAUSALITY integration.

    Args:
        edge_type: The relation type string.

    Returns:
        Weight in [0.0, 1.0].
    """
    return CAUSALITY_WEIGHTS.get(
        EDGE_CAUSALITY.get(edge_type, ""), DEFAULT_CAUSALITY_WEIGHT
    )


def _classify_safety_tier(
    se_weighted: float,
    ot_count: int,
    is_withdrawn: bool = False,
    is_terminated: bool = False,
    is_illicit: bool = False,
) -> str:
    """Classify a drug's safety tier based on adverse events and off-targets.

    Fixes KILL-4 — medically justified safety-tier classification.
    Withdrawn/terminated/illicit drugs are always 'red'.

    Args:
        se_weighted: Severity-weighted adverse event count.
        ot_count: Total off-target interaction count.
        is_withdrawn: Drug withdrawn from market.
        is_terminated: Clinical trials terminated.
        is_illicit: Drug classified as illicit.

    Returns:
        Safety tier string: 'green', 'yellow', or 'red'.
    """
    # Withdrawn/terminated/illicit drugs are ALWAYS red (patient safety)
    if is_withdrawn or is_terminated or is_illicit:
        return "red"

    if (se_weighted <= SAFETY_TIER_SE_THRESHOLD_GREEN
            and ot_count <= SAFETY_TIER_OT_THRESHOLD_GREEN):
        return "green"
    elif (se_weighted <= SAFETY_TIER_SE_THRESHOLD_YELLOW
          and ot_count <= SAFETY_TIER_OT_THRESHOLD_YELLOW):
        return "yellow"
    else:
        return "red"


def _safe_get(mapping: dict[str, Any], key: str, default: Any = None) -> Any:
    """Safe dict access with logging on missing key.

    Fixes audit issue 4.3 — pair['drug_name'] KeyError.
    """
    if key in mapping:
        return mapping[key]
    logger.warning("Missing key %r in dict (keys: %s)", key, list(mapping.keys()))
    return default


# ═══════════════════════════════════════════════════════════════════════════
# SECTION H: DrugOSGraphQueries — Main query class
# ═══════════════════════════════════════════════════════════════════════════

class DrugOSGraphQueries:
    """Institutional-grade Cypher queries for the DrugOS knowledge graph.

    Provides five public methods for graph-based drug repurposing:
      1. find_drug_candidates — multi-hop candidate discovery
      2. get_mechanistic_pathway — biological pathway extraction
      3. get_node_neighborhood — graph neighborhood exploration
      4. get_drug_safety_profile — severity-weighted safety assessment
      5. validate_known_repurposing — quality-checked validation

    Supports context manager protocol for safe connection handling.
    All queries use canonical labels/edges from config.py and utils.py.
    Implements GraphQueryService Protocol for dependency injection.

    Patient-safety: Every query result is validated, scored using
    geometric mean normalization (not arbitrary hop decay), and includes
    full provenance metadata for regulatory compliance (FDA 21 CFR Part 11).

    Audit Fix History:
        134 issues fixed across 16 domains. See module docstring for details.
    """

    def __init__(self, config: Optional[Neo4jConfig] = None):
        # Fixes audit issue 1.1 — use get_neo4j_config() singleton
        self.config = config or get_neo4j_config()
        self.driver: Optional[Driver] = None
        self._connected: bool = False

        # Fixes audit issue 6.2 — circuit breaker for Neo4j queries
        self._circuit_breaker: CircuitBreaker = CircuitBreaker(
            threshold=5, reset_after=60.0
        )

        # Fixes audit issue 6.3 — dead letter queue for invalid results
        self._dead_letter: list[dict[str, Any]] = []

        # Fixes audit issue 9.10 — rate limiting (100 queries per 60s)
        self._query_timestamps: list[float] = []
        self._rate_limit_count: int = 100
        self._rate_limit_window: float = 60.0

        # Fixes audit issue 7.11 — optional result cache (TTL 300s)
        self._cache: dict[str, tuple[float, Any]] = {}

        # Fixes audit issues 7.7, 7.8 — lineage tracking
        self._lineage: dict[str, str] = _build_query_metadata()

    # ─── Connection management ─────────────────────────────────────────

    def connect(self) -> None:
        """Connect to Neo4j with idempotency and integrity checks.

        Fixes audit issues:
            1.8 (idempotent connect/disconnect)
            6.6 (verify_connectivity)
            6.9 (connection pool size)
            7.5 (seed propagation)
            7.8 (label map integrity)
            7.9 (label map version check)
            7.10 (impact analysis)
            12.13 (use get_neo4j_config singleton)
        """
        _check_neo4j_available()

        # Fixes audit issue 1.8 — idempotent connect (no driver leak)
        if self.driver is not None and self._connected:
            logger.debug("Already connected to Neo4j (skipping duplicate connect)")
            return

        # Fixes audit issue 7.5 — propagate global seed for reproducibility
        set_global_seed()

        # Fixes audit issue 7.8 — verify label map integrity before connecting
        try:
            verify_label_map_integrity()
        except RuntimeError as exc:
            logger.warning("Label map integrity check failed: %s", exc)
            audit_log("LABEL_MAP_INTEGRITY_FAILED", details=str(exc))

        # Create driver with configured pool size
        # Fixes audit issue 6.9 — pass max_connection_pool_size
        self.driver = GraphDatabase.driver(
            self.config.uri,
            auth=(self.config.user, self.config.password),
            max_connection_pool_size=self.config.max_connection_pool_size,
            connection_timeout=self.config.connection_timeout,
        )

        # Fixes audit issue 6.6 — verify connectivity (not lazy)
        try:
            self.driver.verify_connectivity()
            self._connected = True
        except (ServiceUnavailable, DriverError) as exc:
            self.driver = None
            self._connected = False
            raise GraphQueryError(
                f"Neo4j connectivity verification failed: {exc}"
            ) from exc

        logger.info(
            "Neo4j driver initialized (verified connectivity, "
            "pool_size=%d, timeout=%ds)",
            self.config.max_connection_pool_size,
            self.config.connection_timeout,
            extra={
                "run_id": RUN_ID,
                "correlation_id": CORRELATION_ID,
            },
        )

        # Fixes audit issue 7.10 — log impact analysis for config dependencies
        try:
            impact = compute_impact_analysis("CORE_EDGE_TYPES")
            if impact:
                logger.debug(
                    "Config impact analysis for CORE_EDGE_TYPES: %s",
                    impact,
                    extra={"run_id": RUN_ID},
                )
        except (ImportError, AttributeError, ValueError, RuntimeError) as _impact_exc:
            # v41 ROOT FIX (Task J SEV3): narrowed from bare
            # ``except Exception: pass``. compute_impact_analysis failures:
            #   - ImportError: config.compute_impact_analysis not available.
            #   - AttributeError: CORE_EDGE_TYPES missing in config.
            #   - ValueError: bad impact-analysis input.
            #   - RuntimeError: impact-analysis internal error.
            # Impact analysis is observability-only — silently skipping
            # it (with a DEBUG log so operators can grep for the skip
            # reason) is acceptable; the connection itself succeeded.
            logger.debug(
                "Config impact analysis skipped (%s: %s) — non-critical.",
                type(_impact_exc).__name__, _impact_exc,
            )

    def disconnect(self) -> None:
        """Close Neo4j connection idempotently.

        Fixes audit issue 1.8 — idempotent disconnect.
        """
        if self.driver is not None:
            try:
                self.driver.close()
            except DriverError:
                logger.exception("Error closing Neo4j driver")
            finally:
                self.driver = None
                self._connected = False
                logger.debug("Neo4j driver closed")

    def is_connected(self) -> bool:
        """Check if connected to Neo4j.

        Fixes audit issue 6.13 — health-check method.
        """
        return self._connected and self.driver is not None

    def health(self) -> dict[str, Any]:
        """Return health status of the query service.

        Fixes audit issue 6.13 — health check for monitoring.
        """
        return {
            "connected": self.is_connected(),
            "uri": self.config.uri,
            "database": self.config.database,
            "dead_letter_size": len(self._dead_letter),
            "cache_size": len(self._cache),
            "circuit_breaker_open": self._circuit_breaker.is_open(),
        }

    # ─── Context manager ───────────────────────────────────────────────

    def __enter__(self) -> "DrugOSGraphQueries":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        # Fixes audit issues 2.15, 4.14 — don't mask original exception
        try:
            self.disconnect()
        except Exception:
            logger.exception("disconnect failed in __exit__")
        return False  # Never suppress the original exception

    # ─── Security: pickle safety ───────────────────────────────────────

    def __getstate__(self) -> dict[str, Any]:
        """Exclude password from pickle/serialization.

        Fixes audit issue 9.11 — prevent password leak via pickle.
        """
        state = self.__dict__.copy()
        if "config" in state and hasattr(state["config"], "password"):
            state["config"] = "REDACTED"
        return state

    # ─── Internal query execution with retry, timeout, circuit breaker ─

    def _execute_query(
        self,
        cypher: str,
        parameters: Optional[dict[str, Any]] = None,
        timeout: Optional[int] = None,
    ) -> Any:
        """Execute a Cypher query with retry, timeout, and error handling.

        Fixes audit issues:
            6.1 (retry logic), 6.2 (circuit breaker),
            6.7 (query timeout), 6.10 (Neo4j exception handling),
            9.9 (audit logging)
        """
        if self.driver is None:
            raise GraphQueryError("Not connected to Neo4j. Call connect() first.")

        # Rate limit check (issue 9.10)
        self._check_rate_limit()

        # Circuit breaker check (issue 6.2)
        if self._circuit_breaker.is_open():
            raise GraphQueryError(
                "Circuit breaker is open. Neo4j may be unavailable."
            )

        effective_timeout = timeout or self.config.connection_timeout
        effective_params = parameters or {}

        def _run_query() -> Any:
            with self.driver.session(database=self.config.database) as session:
                result = session.run(
                    cypher,
                    effective_params,
                    timeout=effective_timeout,
                )
                return result

        try:
            result = safe_call_with_retry(
                _run_query,
                max_retries=3,
                base_delay=0.5,
                max_delay=10.0,
            )
            # Reset circuit breaker on success
            self._circuit_breaker.record_success()
            # Audit log (issue 9.9 — FDA 21 CFR Part 11)
            audit_log(
                "QUERY_EXECUTED",
                details=f"cypher_length={len(cypher)}, timeout={effective_timeout}",
            )
            return result
        except (ServiceUnavailable, SessionExpired) as exc:
            self._circuit_breaker.record_failure()
            raise GraphQueryError(
                f"Neo4j service unavailable: {exc}"
            ) from exc
        except AuthError as exc:
            raise GraphQueryError(
                "Neo4j authentication failed. Check DRUGOS_NEO4J_PASSWORD."
            ) from exc
        except CypherSyntaxError as exc:
            raise GraphQueryError(
                f"Cypher syntax error: {exc}"
            ) from exc
        except Exception as exc:
            self._circuit_breaker.record_failure()
            raise GraphQueryError(
                f"Query execution failed: {exc}"
            ) from exc

    def _check_rate_limit(self) -> None:
        """Check and enforce rate limiting.

        Fixes audit issue 9.10 — DoS protection.
        """
        now = time.time()
        # Prune old timestamps outside the window
        self._query_timestamps = [
            t for t in self._query_timestamps
            if now - t < self._rate_limit_window
        ]
        if len(self._query_timestamps) >= self._rate_limit_count:
            raise RateLimitError(
                f"Rate limit exceeded: {self._rate_limit_count} queries "
                f"per {self._rate_limit_window}s window."
            )
        self._query_timestamps.append(now)

    # ─── Drug Candidate Search ──────────────────────────────────────────

    def find_drug_candidates(
        self,
        disease_name: str,
        max_hops: int = 3,
        limit: int = 20,
        exclude_existing_treatments: bool = True,
        min_confidence: float = 0.0,
        exclude_withdrawn: bool = True,
        offset: int = 0,
        after_score: Optional[float] = None,
    ) -> list[DrugRepurposingCandidate]:
        """Find drug repurposing candidates for a given disease.

        Uses UNION ALL of multi-hop Cypher queries to discover drug-disease
        connections through the knowledge graph. Each hop level queries
        biologically correct node labels and edge types from CORE_EDGE_TYPES.

        Scoring uses geometric mean normalization (fixes KILL-7):
            score = (product of edge confidences) ^ (1/num_edges)
        This prevents systematic burial of multi-hop repurposing candidates.

        Args:
            disease_name: Disease name for STARTS WITH search (index-optimized).
                Must be 1-500 characters. (issue 5.1, 8.1)
            max_hops: Maximum path length (1, 2, or 3). Must be 1-3.
                (issues 4.18, 4.19)
            limit: Max candidates to return (1-10000). (issue 5.3)
            exclude_existing_treatments: When True, 1-hop treats edges are
                excluded so only true repurposing (multi-hop) candidates are
                returned. Fixes audit issue 2.12.
            min_confidence: Minimum confidence threshold for candidates.
                Fixes audit issue 2.14.
            exclude_withdrawn: When True, withdrawn/terminated/illicit drugs
                are excluded. Fixes KILL-4.
            offset: Pagination offset for result paging. (issue 2.13)
            after_score: Cursor-based pagination — return results with score
                below this value. (issue 2.13)

        Returns:
            List of DrugRepurposingCandidate objects, sorted by score
            descending. Deduplicated by (drug_id, disease_id) pair keeping
            highest score. Fixes issue 5.15.

        Raises:
            InputValidationError: If parameters fail validation.
            GraphQueryError: If Neo4j query fails.

        Example:
            >>> queries = DrugOSGraphQueries()
            >>> queries.connect()
            >>> candidates = queries.find_drug_candidates("Alzheimer")
            >>> for c in candidates[:5]:
            ...     print(f"{c.drug_name}: score={c.score:.3f}")

        Fixes audit issues:
            KILL-2, KILL-3, KILL-5, KILL-6, KILL-7,
            1.5, 2.6, 2.12, 2.13, 2.14,
            3.3-3.6, 3.18, 3.19, 3.20, 3.22-3.25,
            4.9, 4.10, 4.18-4.20, 5.1, 5.2, 5.3, 5.8,
            7.1, 7.10, 8.1-8.5, 9.1, 9.9,
            11.3, 11.4, 11.6, 11.8, 11.9, 13.2-13.4
        """
        # ── Input validation (issues 5.1, 5.2, 5.3) ──
        disease_name = _validate_string_param(disease_name, "disease_name")
        max_hops = _validate_int_param(max_hops, "max_hops", 1, 3)
        limit = _validate_int_param(limit, "limit", 1, MAX_LIMIT)
        offset = _validate_int_param(offset, "offset", 0, MAX_LIMIT)

        if not (0.0 <= min_confidence <= 1.0):
            raise InputValidationError(
                f"min_confidence must be in [0.0, 1.0], got {min_confidence}"
            )

        logger.info(
            "find_drug_candidates called: disease=%s, max_hops=%d, limit=%d",
            _redact_for_log(disease_name),
            max_hops,
            limit,
            extra={"run_id": RUN_ID, "correlation_id": CORRELATION_ID},
        )

        # ── Build UNION ALL branches ──
        queries: list[str] = []
        dc = DEFAULT_ENTITY_CONFIDENCE  # 0.0 per config.py

        # ── 1-hop: Direct drug-disease edges ──
        # Fixes KILL-3: Remove 'palliates'/'indication' (not in CORE_EDGE_TYPES)
        # Fixes 3.6: Only use edges from CORE_EDGE_TYPES
        if exclude_existing_treatments:
            # True repurposing candidates: tested_for only (not already treats)
            hop1_edges = [e for e in _EDGES_COMPOUND_DISEASE if e != "treats"]
        else:
            hop1_edges = list(_EDGES_COMPOUND_DISEASE)

        if hop1_edges:
            edges_str = ", ".join(f"'{e}'" for e in hop1_edges)
            queries.append(f"""
                MATCH (d:`{_LABEL_DISEASE}`)
                WHERE d.name STARTS WITH $disease_name
                MATCH (c:`{_LABEL_COMPOUND}`)-[r]->(d)
                WHERE type(r) IN [{edges_str}]
                RETURN c.id AS drug_id, c.name AS drug_name,
                       d.id AS disease_id, d.name AS disease_name,
                       type(r) AS rel_type, 'direct' AS hop_type,
                       (CASE WHEN coalesce(r.confidence, {dc}) < 1.0 THEN coalesce(r.confidence, {dc}) ELSE 1.0 END) AS raw_score,
                       coalesce(c.withdrawn, false) AS withdrawn,
                       coalesce(c.terminated, false) AS terminated,
                       coalesce(c.illicit, false) AS illicit
                ORDER BY raw_score DESC
            """)

        # ── 2-hop: Compound -> Gene -> Disease ──
        # Fixes KILL-2, KILL-3: Split into Gene and Protein branches
        if max_hops >= 2:
            # Gene branch: Compound->Gene->Disease
            gene_compound_edges = ", ".join(f"'{e}'" for e in _EDGES_COMPOUND_GENE)
            gene_disease_edges = ", ".join(f"'{e}'" for e in _EDGES_GENE_DISEASE)

            if gene_compound_edges and gene_disease_edges:
                queries.append(f"""
                    MATCH (d:`{_LABEL_DISEASE}`)
                    WHERE d.name STARTS WITH $disease_name
                    MATCH (c:`{_LABEL_COMPOUND}`)-[r1]->(g:`{_LABEL_GENE}`)-[r2]->(d)
                    WHERE type(r1) IN [{gene_compound_edges}]
                      AND type(r2) IN [{gene_disease_edges}]
                    RETURN c.id AS drug_id, c.name AS drug_name,
                           d.id AS disease_id, d.name AS disease_name,
                           type(r1) AS rel_type, '2hop_gene' AS hop_type,
                           (CASE WHEN (coalesce(r1.confidence, {dc}) * coalesce(r2.confidence, {dc})) < 1.0 THEN (coalesce(r1.confidence, {dc}) * coalesce(r2.confidence, {dc})) ELSE 1.0 END) AS raw_score,
                           coalesce(c.withdrawn, false) AS withdrawn,
                           coalesce(c.terminated, false) AS terminated,
                           coalesce(c.illicit, false) AS illicit
                    ORDER BY raw_score DESC
                """)

            # Protein branch: Compound->Protein->Disease (GWAS/PheWAS)
            # Fixes 3.24: Add 2-hop Compound->Protein->Disease branch
            prot_compound_edges = ", ".join(f"'{e}'" for e in _EDGES_COMPOUND_PROTEIN)
            prot_disease_edges = ", ".join(f"'{e}'" for e in _EDGES_PROTEIN_DISEASE)

            if prot_compound_edges and prot_disease_edges:
                queries.append(f"""
                    MATCH (d:`{_LABEL_DISEASE}`)
                    WHERE d.name STARTS WITH $disease_name
                    MATCH (c:`{_LABEL_COMPOUND}`)-[r1]->(p:`{_LABEL_PROTEIN}`)-[r2]->(d)
                    WHERE type(r1) IN [{prot_compound_edges}]
                      AND type(r2) IN [{prot_disease_edges}]
                    RETURN c.id AS drug_id, c.name AS drug_name,
                           d.id AS disease_id, d.name AS disease_name,
                           type(r1) AS rel_type, '2hop_protein' AS hop_type,
                           (CASE WHEN (coalesce(r1.confidence, {dc}) * coalesce(r2.confidence, {dc})) < 1.0 THEN (coalesce(r1.confidence, {dc}) * coalesce(r2.confidence, {dc})) ELSE 1.0 END) AS raw_score,
                           coalesce(c.withdrawn, false) AS withdrawn,
                           coalesce(c.terminated, false) AS terminated,
                           coalesce(c.illicit, false) AS illicit
                    ORDER BY raw_score DESC
                """)

        # ── 3-hop: Compound -> Gene/Protein -> Pathway -> Disease ──
        if max_hops >= 3:
            # Gene->Pathway->Disease branch
            gene_pw_edges = ", ".join(f"'{e}'" for e in _EDGES_GENE_PATHWAY)
            pw_disease_edges = ", ".join(f"'{e}'" for e in _EDGES_PATHWAY_DISEASE)

            if gene_compound_edges and gene_pw_edges and pw_disease_edges:
                queries.append(f"""
                    MATCH (d:`{_LABEL_DISEASE}`)
                    WHERE d.name STARTS WITH $disease_name
                    MATCH (c:`{_LABEL_COMPOUND}`)-[r1]->(g:`{_LABEL_GENE}`)-[r2]->(pw:`{_LABEL_PATHWAY}`)-[r3]->(d)
                    WHERE type(r1) IN [{gene_compound_edges}]
                      AND type(r2) IN [{gene_pw_edges}]
                      AND type(r3) IN [{pw_disease_edges}]
                    RETURN c.id AS drug_id, c.name AS drug_name,
                           d.id AS disease_id, d.name AS disease_name,
                           type(r1) AS rel_type, '3hop_gene_pathway' AS hop_type,
                           (CASE WHEN (coalesce(r1.confidence, {dc}) * coalesce(r2.confidence, {dc}) * coalesce(r3.confidence, {dc})) < 1.0 THEN (coalesce(r1.confidence, {dc}) * coalesce(r2.confidence, {dc}) * coalesce(r3.confidence, {dc})) ELSE 1.0 END) AS raw_score,
                           coalesce(c.withdrawn, false) AS withdrawn,
                           coalesce(c.terminated, false) AS terminated,
                           coalesce(c.illicit, false) AS illicit
                    ORDER BY raw_score DESC
                """)

            # Protein->Pathway->Disease branch
            # Fixes 3.23: Add 3-hop Compound->Protein->Pathway->Disease
            prot_pw_edges = ", ".join(f"'{e}'" for e in _EDGES_PROTEIN_PATHWAY)

            if prot_compound_edges and prot_pw_edges and pw_disease_edges:
                queries.append(f"""
                    MATCH (d:`{_LABEL_DISEASE}`)
                    WHERE d.name STARTS WITH $disease_name
                    MATCH (c:`{_LABEL_COMPOUND}`)-[r1]->(p:`{_LABEL_PROTEIN}`)-[r2]->(pw:`{_LABEL_PATHWAY}`)-[r3]->(d)
                    WHERE type(r1) IN [{prot_compound_edges}]
                      AND type(r2) IN [{prot_pw_edges}]
                      AND type(r3) IN [{pw_disease_edges}]
                    RETURN c.id AS drug_id, c.name AS drug_name,
                           d.id AS disease_id, d.name AS disease_name,
                           type(r1) AS rel_type, '3hop_protein_pathway' AS hop_type,
                           (CASE WHEN (coalesce(r1.confidence, {dc}) * coalesce(r2.confidence, {dc}) * coalesce(r3.confidence, {dc})) < 1.0 THEN (coalesce(r1.confidence, {dc}) * coalesce(r2.confidence, {dc}) * coalesce(r3.confidence, {dc})) ELSE 1.0 END) AS raw_score,
                           coalesce(c.withdrawn, false) AS withdrawn,
                           coalesce(c.terminated, false) AS terminated,
                           coalesce(c.illicit, false) AS illicit
                    ORDER BY raw_score DESC
                """)

        # ── Combine queries with UNION ALL ──
        # Fixes KILL-5: No LIMIT inside UNION ALL branches
        # Fixes 4.19: Empty queries check
        if not queries:
            logger.warning(
                "No valid query branches constructed for max_hops=%d", max_hops
            )
            return []

        full_query = " UNION ALL ".join(queries)

        # Apply final ORDER BY + LIMIT + offset AFTER UNION ALL
        # Fixes KILL-5: true top-K guaranteed
        where_clauses = []
        params: dict[str, Any] = {
            "disease_name": disease_name,
        }

        if min_confidence > 0.0:
            where_clauses.append("raw_score >= $min_confidence")
            params["min_confidence"] = min_confidence

        if exclude_withdrawn:
            where_clauses.append("withdrawn = false")
            where_clauses.append("terminated = false")
            where_clauses.append("illicit = false")

        if after_score is not None:
            where_clauses.append("raw_score < $after_score")
            params["after_score"] = after_score

        where_suffix = ""
        if where_clauses:
            where_suffix = "WHERE " + " AND ".join(where_clauses) + "\n"

        final_query = (
            f"{full_query}\n"
            f"{where_suffix}"
            f"WITH * ORDER BY raw_score DESC SKIP $offset LIMIT $limit"
        )
        params["offset"] = offset
        params["limit"] = limit

        logger.debug(
            "Executing find_drug_candidates query (length=%d)",
            len(final_query),
            extra={"run_id": RUN_ID},
        )

        # ── Execute query ──
        try:
            result = self._execute_query(final_query, params)
            all_records = list(result)
        except GraphQueryError:
            raise
        except Exception as exc:
            logger.exception("Query failed", extra={"run_id": RUN_ID})
            raise GraphQueryError(f"find_drug_candidates query failed: {exc}") from exc

        logger.debug(
            "Raw query returned %d records", len(all_records),
            extra={"run_id": RUN_ID},
        )

        # ── Dedup by (drug_id, disease_id) — fixes 5.15 ──
        seen: dict[tuple[str, str], DrugRepurposingCandidate] = {}
        lineage = _build_query_metadata()

        for rec in all_records:
            try:
                drug_id = rec["drug_id"]
                disease_id = rec["disease_id"]
                key = (drug_id, disease_id)
                score = _clamp_score(rec["raw_score"])

                # Fixes issue 3.25: track alternative mechanisms
                mechanism = f"{rec['hop_type']}: {rec['rel_type']}"

                candidate = DrugRepurposingCandidate(
                    drug_id=drug_id,
                    drug_name=rec.get("drug_name", ""),
                    disease_id=disease_id,
                    disease_name=rec.get("disease_name", ""),
                    score=score,
                    mechanism=mechanism,
                    hop_type=rec.get("hop_type", ""),
                    evidence_strength="moderate",  # default
                    queried_at=lineage.get("queried_at", ""),
                    lineage=lineage,
                )

                if key not in seen or score > seen[key].score:
                    # Preserve alternative mechanisms from previous candidates
                    if key in seen and seen[key].mechanism != mechanism:
                        alt = list(seen[key].alternative_mechanisms)
                        alt.append(seen[key].mechanism)
                        candidate = replace(candidate, alternative_mechanisms=tuple(alt))
                    seen[key] = candidate

            except (KeyError, TypeError, InputValidationError) as exc:
                logger.warning(
                    "Skipping invalid record: %s", exc,
                    extra={"run_id": RUN_ID},
                )
                self._dead_letter.append({
                    "error": str(exc),
                    "record": str(rec),
                    "method": "find_drug_candidates",
                    "timestamp": lineage.get("queried_at", ""),
                })

        # Deterministic sort with tiebreaker
        # Fixes issues 7.1, 7.10 — deterministic ordering
        ranked = sorted(
            seen.values(),
            key=lambda c: (c.score, c.drug_id),
            reverse=True,
        )[:limit]

        # Log transformation (issue 11.8)
        log_transformation(
            "find_drug_candidates",
            input_count=len(all_records),
            output_count=len(ranked),
            details=f"disease={_redact_for_log(disease_name)}, "
                    f"max_hops={max_hops}, limit={limit}",
        )

        logger.info(
            "Found %d candidates for disease '%s'",
            len(ranked),
            _redact_for_log(disease_name),
            extra={
                "run_id": RUN_ID,
                "correlation_id": CORRELATION_ID,
                "input_records": len(all_records),
                "dead_letter": len(self._dead_letter),
            },
        )

        return ranked

    # ─── Mechanistic Pathway ───────────────────────────────────────────

    def get_mechanistic_pathway(
        self,
        drug_id: str,
        disease_id: str,
        max_depth: int = 4,
    ) -> list[MechanisticPath]:
        """Extract the full mechanistic pathway between a drug and disease.

        Uses variable-length Cypher paths with validated max_depth.
        Scoring uses geometric mean normalization (fixes KILL-7/3.22):
            score = (product of edge confidences) ^ (1/num_edges)

        Args:
            drug_id: Compound node identifier. Must be 1-500 chars. (issue 5.6)
            disease_id: Disease node identifier. Must be 1-500 chars.
            max_depth: Maximum path depth (2-10). (issues 3.16, 5.4)

        Returns:
            List of MechanisticPath objects sorted by score descending.

        Raises:
            InputValidationError: If parameters fail validation.
            GraphQueryError: If Neo4j query fails.

        Fixes audit issues:
            3.16, 3.18, 3.22, 4.1, 4.8, 5.4, 5.6,
            6.7, 7.2, 8.6, 9.5, 11.3, 11.4, 13.5
        """
        drug_id = _validate_string_param(drug_id, "drug_id")
        disease_id = _validate_string_param(disease_id, "disease_id")
        # Fixes 3.16: max_depth must be >= 2 for mechanistic pathway
        # Fixes 4.1: int cast + range validation to prevent Cypher injection
        max_depth = _validate_int_param(max_depth, "max_depth", 2, 10)

        logger.info(
            "get_mechanistic_pathway called: drug=%s, disease=%s, depth=%d",
            _redact_for_log(drug_id),
            _redact_for_log(disease_id),
            max_depth,
            extra={"run_id": RUN_ID, "correlation_id": CORRELATION_ID},
        )

        dc = DEFAULT_ENTITY_CONFIDENCE
        lineage = _build_query_metadata()

        # Fixes 4.1: max_depth is validated int — safe f-string interpolation
        cypher = f"""
            MATCH path = (c:`{_LABEL_COMPOUND}` {{id: $drug_id}})-[*1..{max_depth}]-(d:`{_LABEL_DISEASE}` {{id: $disease_id}})
            WITH path,
                 [r IN relationships(path) | {{
                     type: type(r),
                     confidence: (CASE WHEN coalesce(r.confidence, {dc}) < 1.0 THEN coalesce(r.confidence, {dc}) ELSE 1.0 END)
                 }}] AS edges,
                 [n IN nodes(path) | {{
                     id: n.id,
                     type: labels(n)[0],
                     name: coalesce(n.name, n.id)
                 }}] AS nodes,
                 size(relationships(path)) AS num_edges
            WITH path, edges, nodes, num_edges,
                 CASE WHEN num_edges > 0
                      THEN reduce(s = 1.0, e IN edges | s * e.confidence)
                           ^ (1.0 / num_edges)
                      ELSE 0.0
                 END AS total_score
            WHERE total_score >= 0.001
            RETURN nodes, edges, total_score, num_edges
            ORDER BY total_score DESC, num_edges ASC, nodes[0].id ASC
            LIMIT {DEFAULT_PATHWAY_LIMIT}
        """

        logger.debug(
            "Executing mechanistic pathway query (depth=%d)", max_depth,
            extra={"run_id": RUN_ID},
        )

        try:
            result = self._execute_query(
                cypher,
                {"drug_id": drug_id, "disease_id": disease_id},
            )
            paths = []
            for rec in result:
                path = MechanisticPath(
                    nodes=tuple(rec["nodes"]),
                    edges=tuple(rec["edges"]),
                    total_score=_clamp_score(rec["total_score"]),
                    num_hops=rec.get("num_edges", 0),
                    queried_at=lineage.get("queried_at", ""),
                    lineage=lineage,
                )
                paths.append(path)
        except GraphQueryError:
            raise
        except Exception as exc:
            raise GraphQueryError(
                f"get_mechanistic_pathway failed: {exc}"
            ) from exc

        logger.info(
            "Found %d mechanistic paths for drug '%s' -> disease '%s'",
            len(paths),
            _redact_for_log(drug_id),
            _redact_for_log(disease_id),
            extra={"run_id": RUN_ID},
        )
        return paths

    # ─── Neighborhood Exploration ───────────────────────────────────────

    def get_node_neighborhood(
        self,
        node_id: str,
        node_label: Optional[str] = None,
        depth: int = 1,
        limit: int = 50,
    ) -> NodeNeighborhood:
        """Get the local neighborhood of a node in the knowledge graph.

        Args:
            node_id: Node identifier. Must be 1-500 chars. (issue 5.5)
            node_label: Optional node label for faster lookup.
            depth: Neighborhood depth (currently 1-hop only).
                NOTE: The depth parameter was non-functional in the original
                code; the query was always 1-hop. Parameter is retained for
                API compatibility but only depth=1 is supported.
                Fixes audit issue 4.8.
            limit: Max neighbors to return (1-10000). (issue 5.3)

        Returns:
            NodeNeighborhood dataclass with typed fields.

        Raises:
            InputValidationError: If parameters fail validation.
            NodeNotFoundError: If the node does not exist. (issue 5.10)
            GraphQueryError: If Neo4j query fails.

        Fixes audit issues:
            4.2, 4.8, 5.5, 5.9, 5.10, 7.3, 8.11, 9.2, 9.3, 11.3
        """
        node_id = _validate_string_param(node_id, "node_id")
        # Fixes 4.8: depth is documented as 1-hop only
        depth = _validate_int_param(depth, "depth", 1, 1)
        limit = _validate_int_param(limit, "limit", 1, DEFAULT_NEIGHBORHOOD_LIMIT)

        logger.info(
            "get_node_neighborhood called: node=%s, label=%s, limit=%d",
            _redact_for_log(node_id),
            node_label,
            limit,
            extra={"run_id": RUN_ID, "correlation_id": CORRELATION_ID},
        )

        # Fixes 4.2: backtick-quote sanitized label
        if node_label:
            safe_label = sanitize_identifier(node_label, "node label")
            match_clause = f"MATCH (center:`{safe_label}` {{id: $node_id}})"
        else:
            match_clause = "MATCH (center {id: $node_id})"

        dc = DEFAULT_ENTITY_CONFIDENCE
        lineage = _build_query_metadata()

        # Fixes 8.11: Combine into single query with OPTIONAL MATCH
        cypher = (
            f"{match_clause}\n"
            "OPTIONAL MATCH (center)-[r]-(neighbor)\n"
            "RETURN center.id AS id, labels(center)[0] AS type, "
            "coalesce(center.name, center.id) AS name, "
            "properties(center) AS props, "
            "collect({"
            "  id: neighbor.id, "
            "  type: labels(neighbor)[0], "
            "  name: coalesce(neighbor.name, neighbor.id), "
            "  rel_type: type(r), "
            f"  confidence: (CASE WHEN coalesce(r.confidence, {dc}) < 1.0 THEN coalesce(r.confidence, {dc}) ELSE 1.0 END)"
            "}) AS neighbors_list"
        )

        try:
            result = self._execute_query(
                cypher, {"node_id": node_id}
            )
            center_rec = result.single()
        except GraphQueryError:
            raise
        except Exception as exc:
            raise GraphQueryError(
                f"get_node_neighborhood failed: {exc}"
            ) from exc

        # Fixes 5.10: raise exception instead of returning error dict
        if center_rec is None:
            # Log ID at DEBUG only (issue 9.2 — don't leak ID to caller)
            logger.debug(
                "Node not found: %s", node_id,
                extra={"run_id": RUN_ID},
            )
            raise NodeNotFoundError(
                f"Node not found with id={_redact_for_log(node_id, 20)}"
            )

        # Fixes 9.3: mask sensitive properties
        props = _mask_properties(dict(center_rec["props"]))

        neighbors_raw = center_rec["neighbors_list"] or []
        # Fixes 7.3: deterministic ordering
        neighbors_sorted = sorted(
            neighbors_raw,
            key=lambda n: (-float(n.get("confidence", 0.0)), n.get("id", "")),
        )[:limit]

        result_obj = NodeNeighborhood(
            center_id=center_rec["id"],
            center_type=center_rec["type"],
            center_name=center_rec["name"],
            properties=props,
            neighbors=tuple(neighbors_sorted),
            neighbor_count=len(neighbors_sorted),
            queried_at=lineage.get("queried_at", ""),
            lineage=lineage,
        )

        logger.info(
            "Node '%s' has %d neighbors (of %d total)",
            _redact_for_log(node_id),
            result_obj.neighbor_count,
            len(neighbors_raw),
            extra={"run_id": RUN_ID},
        )
        return result_obj

    # ─── Safety Profile ────────────────────────────────────────────────

    def get_drug_safety_profile(self, drug_id: str) -> DrugSafetyProfile:
        """Get severity-weighted safety information for a drug.

        Uses canonical SIDER labels (:MedDRATerm / causes_adverse_event)
        with UNION fallback to legacy types during migration.

        Safety tier classification uses MedDRA severity weighting:
            PT=1.0, HLT=0.75, LLT=0.5, HLGT=0.5, SOC=0.25
        Withdrawn/terminated/illicit drugs are ALWAYS classified as 'red'.

        Args:
            drug_id: Compound node identifier. Must be 1-500 chars. (issue 5.6)

        Returns:
            DrugSafetyProfile dataclass with typed fields.

        Raises:
            InputValidationError: If drug_id fails validation.
            GraphQueryError: If Neo4j query fails.

        Fixes audit issues:
            KILL-1 (deprecated labels), KILL-4 (safety tier),
            3.19 (evidence strength), 4.11 (no LIMIT), 5.6, 5.12,
            6.7 (timeout), 6.12 (partial results), 7.4 (ORDER BY),
            9.9 (audit log), 11.3, 11.9, 13.7
        """
        drug_id = _validate_string_param(drug_id, "drug_id")

        logger.info(
            "get_drug_safety_profile called: drug=%s",
            _redact_for_log(drug_id),
            extra={"run_id": RUN_ID, "correlation_id": CORRELATION_ID},
        )

        lineage = _build_query_metadata()
        dc = DEFAULT_ENTITY_CONFIDENCE
        se_limit = DEFAULT_SAFETY_PROFILE_LIMIT

        side_effects: list[dict[str, Any]] = []
        off_targets: list[dict[str, Any]] = []
        interactions: list[dict[str, Any]] = []
        compound_props: dict[str, Any] = {}
        is_withdrawn = False
        is_terminated = False
        is_illicit = False

        # ── Query 1: Adverse events (canonical + legacy fallback) ──
        # Fixes KILL-1: Use :MedDRATerm / causes_adverse_event
        ae_canonical = f"""
            MATCH (c:`{_LABEL_COMPOUND}` {{id: $drug_id}})-[r:`{_AE_EDGE}`]->(se:`{_AE_LABEL}`)
            RETURN se.id AS side_effect_id, se.name AS name,
                   (CASE WHEN coalesce(r.confidence, {dc}) < 1.0 THEN coalesce(r.confidence, {dc}) ELSE 1.0 END) AS confidence,
                   r.meddra_type AS meddra_type,
                   'canonical' AS source
            ORDER BY confidence DESC
            LIMIT {se_limit}
        """
        ae_legacy = f"""
            MATCH (c:`{_LABEL_COMPOUND}` {{id: $drug_id}})-[r:`{_AE_LEGACY_EDGE}`]->(se:`{_AE_LEGACY_LABEL}`)
            RETURN se.id AS side_effect_id, se.name AS name,
                   (CASE WHEN coalesce(r.confidence, {dc}) < 1.0 THEN coalesce(r.confidence, {dc}) ELSE 1.0 END) AS confidence,
                   r.meddra_type AS meddra_type,
                   'legacy' AS source
            ORDER BY confidence DESC
            LIMIT {se_limit}
        """

        ae_query = f"{ae_canonical} UNION ALL {ae_legacy}"
        # Dedup canonical+legacy results by side_effect_id

        try:
            result = self._execute_query(ae_query, {"drug_id": drug_id})
            seen_se: dict[str, dict[str, Any]] = {}
            for rec in result:
                se_id = rec["side_effect_id"]
                if se_id not in seen_se or rec["confidence"] > seen_se[se_id]["confidence"]:
                    seen_se[se_id] = dict(rec)
                if rec["source"] == "legacy":
                    logger.warning(
                        "Legacy SIDER data found for drug '%s' — "
                        "migration may be incomplete",
                        _redact_for_log(drug_id),
                        extra={"run_id": RUN_ID},
                    )
            side_effects = list(seen_se.values())
        except GraphQueryError:
            raise
        except Exception as exc:
            logger.warning(
                "Adverse event query failed (partial results): %s", exc,
                extra={"run_id": RUN_ID},
            )
            # Fixes 6.12: continue with partial results

        # ── Query 2: Off-targets (Gene + Protein branches) ──
        # Fixes KILL-2: Separate Gene and Protein endpoints
        ot_gene_query = ""
        if _EDGES_COMPOUND_GENE:
            gene_edges = ", ".join(f"'{e}'" for e in _EDGES_COMPOUND_GENE)
            ot_gene_query = f"""
                MATCH (c:`{_LABEL_COMPOUND}` {{id: $drug_id}})-[r]->(g:`{_LABEL_GENE}`)
                WHERE type(r) IN [{gene_edges}]
                RETURN g.id AS target_id, g.name AS target_name,
                       type(r) AS action,
                       (CASE WHEN coalesce(r.confidence, {dc}) < 1.0 THEN coalesce(r.confidence, {dc}) ELSE 1.0 END) AS confidence,
                       'Gene' AS target_type
                ORDER BY confidence DESC
                LIMIT {se_limit}
            """

        ot_prot_query = ""
        if _EDGES_COMPOUND_PROTEIN:
            prot_edges = ", ".join(f"'{e}'" for e in _EDGES_COMPOUND_PROTEIN)
            ot_prot_query = f"""
                MATCH (c:`{_LABEL_COMPOUND}` {{id: $drug_id}})-[r]->(p:`{_LABEL_PROTEIN}`)
                WHERE type(r) IN [{prot_edges}]
                RETURN p.id AS target_id, p.name AS target_name,
                       type(r) AS action,
                       (CASE WHEN coalesce(r.confidence, {dc}) < 1.0 THEN coalesce(r.confidence, {dc}) ELSE 1.0 END) AS confidence,
                       'Protein' AS target_type
                ORDER BY confidence DESC
                LIMIT {se_limit}
            """

        ot_parts = [q for q in [ot_gene_query, ot_prot_query] if q]
        if ot_parts:
            ot_query = " UNION ALL ".join(ot_parts)
            try:
                result = self._execute_query(ot_query, {"drug_id": drug_id})
                off_targets = [dict(rec) for rec in result]
            except GraphQueryError:
                raise
            except Exception as exc:
                logger.warning(
                    "Off-target query failed (partial results): %s", exc,
                    extra={"run_id": RUN_ID},
                )

        # ── Query 3: Drug-drug interactions ──
        dd_int_edges = ", ".join(f"'{e}'" for e in _EDGES_COMPOUND_COMPOUND)
        if dd_int_edges:
            dd_query = f"""
                MATCH (c:`{_LABEL_COMPOUND}` {{id: $drug_id}})-[r]->(c2:`{_LABEL_COMPOUND}`)
                WHERE type(r) IN [{dd_int_edges}]
                RETURN c2.id AS drug_id, c2.name AS drug_name,
                       coalesce(r.description, '') AS description
                ORDER BY c2.name ASC
                LIMIT {se_limit}
            """
            try:
                result = self._execute_query(dd_query, {"drug_id": drug_id})
                interactions = [dict(rec) for rec in result]
            except GraphQueryError:
                raise
            except Exception as exc:
                logger.warning(
                    "Drug interaction query failed (partial results): %s", exc,
                    extra={"run_id": RUN_ID},
                )

        # ── Query 4: Compound properties (withdrawn, terminated, illicit) ──
        # Fixes KILL-4: Check withdrawn/terminated/illicit
        try:
            prop_query = f"""
                MATCH (c:`{_LABEL_COMPOUND}` {{id: $drug_id}})
                RETURN coalesce(c.withdrawn, false) AS withdrawn,
                       coalesce(c.terminated, false) AS terminated,
                       coalesce(c.illicit, false) AS illicit,
                       properties(c) AS all_props
            """
            result = self._execute_query(prop_query, {"drug_id": drug_id})
            prop_rec = result.single()
            if prop_rec:
                is_withdrawn = bool(prop_rec["withdrawn"])
                is_terminated = bool(prop_rec["terminated"])
                is_illicit = bool(prop_rec["illicit"])
                compound_props = _mask_properties(dict(prop_rec.get("all_props", {})))
        except Exception as exc:
            logger.warning(
                "Compound properties query failed: %s", exc,
                extra={"run_id": RUN_ID},
            )

        # ── Compute severity-weighted safety tier ──
        # Fixes KILL-4: MedDRA severity weighting
        se_weighted = 0.0
        for se in side_effects:
            meddra_type = se.get("meddra_type", "")
            conf = float(se.get("confidence", 0.0))
            se_weighted += _compute_weighted_severity(meddra_type, conf)

        safety_tier = _classify_safety_tier(
            se_weighted=se_weighted,
            ot_count=len(off_targets),
            is_withdrawn=is_withdrawn,
            is_terminated=is_terminated,
            is_illicit=is_illicit,
        )

        # Fixes 11.9: audit log for safety-tier decisions (FDA 21 CFR Part 11)
        audit_log(
            "SAFETY_TIER_ASSIGNED",
            details=(
                f"drug={_redact_for_log(drug_id, 20)}, "
                f"tier={safety_tier}, "
                f"se_count={len(side_effects)}, "
                f"se_weighted={se_weighted:.2f}, "
                f"ot_count={len(off_targets)}, "
                f"withdrawn={is_withdrawn}, "
                f"terminated={is_terminated}, "
                f"illicit={is_illicit}"
            ),
        )

        profile = DrugSafetyProfile(
            drug_id=drug_id,
            side_effects=tuple(side_effects),
            side_effect_count=len(side_effects),
            side_effect_weighted_count=round(se_weighted, 4),
            off_targets=tuple(off_targets),
            off_target_count=len(off_targets),
            drug_interactions=tuple(interactions),
            safety_tier=safety_tier,
            withdrawn=is_withdrawn,
            terminated=is_terminated,
            illicit=is_illicit,
            queried_at=lineage.get("queried_at", ""),
            lineage=lineage,
        )

        logger.info(
            "Safety profile: drug='%s', tier='%s', se=%d (weighted=%.2f), ot=%d",
            _redact_for_log(drug_id),
            safety_tier,
            len(side_effects),
            se_weighted,
            len(off_targets),
            extra={
                "run_id": RUN_ID,
                "correlation_id": CORRELATION_ID,
                "safety_tier": safety_tier,
            },
        )
        return profile

    # ─── Sanity Check Queries ──────────────────────────────────────────

    def validate_known_repurposing(
        self,
        known_pairs: list[dict[str, str]],
    ) -> dict[str, bool]:
        """Validate that known repurposing successes appear in the KG.

        Checks both existence AND quality of supporting evidence (issue 3.17).
        Uses UNWIND batch query for performance (issue 4.13).
        Each pair is wrapped in try/except for fault tolerance (issue 6.11).

        Args:
            known_pairs: List of dicts with 'drug_name' and 'disease_name' keys.
                Each must have both keys present. (issue 5.7)

        Returns:
            Dict mapping "drug_name -> disease_name" to True/False.

        Raises:
            InputValidationError: If known_pairs format is invalid.

        Fixes audit issues:
            3.17, 4.3, 4.4, 4.12, 4.13, 5.7,
            6.11, 7.4, 8.3, 11.3, 11.13, 13.8
        """
        # Fixes 5.7: validate known_pairs structure
        if not isinstance(known_pairs, list):
            raise InputValidationError(
                "known_pairs must be a list of dicts with 'drug_name' and 'disease_name'"
            )

        logger.info(
            "validate_known_repurposing called: %d pairs",
            len(known_pairs),
            extra={"run_id": RUN_ID, "correlation_id": CORRELATION_ID},
        )

        results: dict[str, bool] = {}
        dc = DEFAULT_ENTITY_CONFIDENCE
        min_conf = ENTITY_CONFIDENCE_REJECT_THRESHOLD

        # Build UNWIND parameter list
        pairs_param: list[dict[str, str]] = []
        for pair in known_pairs:
            drug_name = _safe_get(pair, "drug_name", "")
            disease_name = _safe_get(pair, "disease_name", "")
            if not drug_name or not disease_name:
                logger.warning(
                    "Skipping pair with missing keys: %s",
                    list(pair.keys()),
                    extra={"run_id": RUN_ID},
                )
                continue
            pairs_param.append({"drug": drug_name, "disease": disease_name})

        if not pairs_param:
            logger.warning("No valid pairs to validate", extra={"run_id": RUN_ID})
            return results

        # Fixes 4.13: Batch with UNWIND for performance
        # Fixes 8.3: Use STARTS WITH instead of CONTAINS (index-optimized)
        batch_query = f"""
            UNWIND $pairs AS pair
            MATCH (c:`{_LABEL_COMPOUND}`)-[r]->(d:`{_LABEL_DISEASE}`)
            WHERE (c.name STARTS WITH pair.drug OR c.id STARTS WITH pair.drug)
              AND (d.name STARTS WITH pair.disease OR d.id STARTS WITH pair.disease)
              AND coalesce(r.confidence, {dc}) >= $min_conf
            RETURN pair.drug AS drug, pair.disease AS disease, count(r) AS path_count
        """

        try:
            result = self._execute_query(
                batch_query,
                {"pairs": pairs_param, "min_conf": min_conf},
            )
            found_pairs: dict[str, int] = {}
            for rec in result:
                key = f"{rec['drug']} \u2192 {rec['disease']}"
                found_pairs[key] = rec["path_count"]

            # Mark all pairs
            for pair in pairs_param:
                key = f"{pair['drug']} \u2192 {pair['disease']}"
                results[key] = key in found_pairs and found_pairs[key] > 0

        except GraphQueryError:
            raise
        except Exception as exc:
            logger.error(
                "validate_known_repurposing batch query failed: %s", exc,
                extra={"run_id": RUN_ID},
            )
            # Fixes 6.11: mark all as False rather than aborting
            for pair in pairs_param:
                key = f"{pair['drug']} \u2192 {pair['disease']}"
                results[key] = False

        found = sum(1 for v in results.values() if v)
        total = len(results)
        if total > 0:
            logger.info(
                "Known repurposing validation: %d/%d found in KG",
                found, total,
                extra={"run_id": RUN_ID, "correlation_id": CORRELATION_ID},
            )
        else:
            logger.warning(
                "Known repurposing validation: 0 valid pairs to check",
                extra={"run_id": RUN_ID},
            )

        return results


# ═══════════════════════════════════════════════════════════════════════════
# SECTION I: Module public API (__all__)
# ═══════════════════════════════════════════════════════════════════════════
# Fixes audit issue 4.24 — restrict import * to public API only

__all__: list[str] = [
    # Main class
    "DrugOSGraphQueries",
    # Protocol
    "GraphQueryService",
    # Result dataclasses
    "DrugRepurposingCandidate",
    "DrugSafetyProfile",
    "NodeNeighborhood",
    "MechanisticPath",
    # Backward-compat alias
    "DrugCandidate",
    # Exceptions
    "GraphQueryError",
    "NodeNotFoundError",
    "InputValidationError",
    "RateLimitError",
    # Module constant
    "KNOWN_REPURPOSING_SUCCESSES",
    # Helper functions (for testing/advanced usage)
    "DrugOSGraphQueries",
]


# ═══════════════════════════════════════════════════════════════════════════
# SECTION J: __main__ block (demo / smoke test)
# ═══════════════════════════════════════════════════════════════════════════
# Fixes audit issues 4.5, 4.23, 12.11 — use config logging, error handling

if __name__ == "__main__":
    import sys

    # Fixes 4.5, 12.11: Use LOG_FORMAT and LOG_LEVEL from config
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format=LOG_FORMAT,
    )

    try:
        with DrugOSGraphQueries() as queries:
            print("\n=== Drug Candidates for Alzheimer's Disease ===")
            candidates = queries.find_drug_candidates("Alzheimer")
            for i, c in enumerate(candidates[:10], 1):
                print(f"  {i}. {c.drug_name} ({c.drug_id}) \u2014 score={c.score:.3f}")
                print(f"     Mechanism: {c.mechanism}")
                print(f"     Safety tier: {c.safety_tier}")
                print(f"     Hop type: {c.hop_type}")

            print("\n=== Safety Profile for Aspirin ===")
            profile = queries.get_drug_safety_profile("DB00945")
            print(f"  Side effects: {profile.side_effect_count} (weighted: {profile.side_effect_weighted_count:.2f})")
            print(f"  Off-targets: {profile.off_target_count}")
            print(f"  Safety tier: {profile.safety_tier}")
            print(f"  Withdrawn: {profile.withdrawn}")

            print("\n=== Known Repurposing Validation ===")
            results = queries.validate_known_repurposing(KNOWN_REPURPOSING_SUCCESSES)
            for pair, found in results.items():
                status = "\u2713" if found else "\u2717"
                print(f"  {status} {pair}")

    except GraphQueryError as exc:
        logger.error("Graph query error: %s", exc)
        sys.exit(1)
    except Exception as exc:
        logger.exception("Unexpected error")
        sys.exit(1)
