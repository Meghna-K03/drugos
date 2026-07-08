"""
DrugOS — Graph Module
=====================
Autonomous Drug Repurposing Platform — Knowledge Graph Construction
& GNN Data Pipeline (Team Cosmic / VentureLab).

This package provides all components for building, loading, querying,
and converting the DrugOS biomedical knowledge graph for drug
repurposing. The Phase 2 build integrates nine external biomedical
databases (DRKG, DrugBank, ChEMBL, OpenTargets, STRING, STITCH,
UniProt, SIDER, ClinicalTrials.gov) plus GEO into a Neo4j knowledge
graph with five core node types (Compound, Disease, Gene, Protein,
Pathway) and 13 DRKG-derived types.

Parameters
----------
(none — this is a package)

Modules
-------
config : module
    Global configuration and path constants. Exposes DATA_SOURCES,
    CORE_NODE_TYPES, CORE_EDGE_TYPES, CANONICAL_IDS, Neo4jConfig,
    PyGConfig, TransEConfig, ensure_dirs(), and all path constants
    (PROJECT_ROOT, DATA_DIR, RAW_DIR, PROCESSED_DIR, KG_DIR,
    EMBEDDINGS_DIR, LOGS_DIR, MODEL_DIR). Pure stdlib — safe to
    import eagerly.
id_crosswalk : module
    **CRITICAL** for scientific correctness. Translates external
    protein/gene IDs (STRING/STITCH Ensembl protein IDs, ChEMBL
    target IDs, OpenTargets Ensembl gene IDs, NCBI Gene IDs) to the
    canonical UniProt accession. Without this module the knowledge
    graph splits into five disconnected Protein subgraphs for the
    same real protein (e.g. COX1/PTGS1 would appear as five separate
    :Protein nodes and ``Drug -> Protein -> Gene -> Disease`` queries
    would silently return empty results). Loaded by every
    cross-database loader (string, stitch, chembl, opentargets).
    Pure stdlib — safe to import eagerly.
entity_resolver : module
    Cross-database entity resolution and ID mapping for Compound,
    Disease, and Gene nodes. Uses pandas (lightweight).
drkg_loader : module
    Download and parse the DRKG baseline knowledge graph.
drugbank_parser : module
    Parse DrugBank XML into structured drug records.
kg_builder : module
    Build and manage the Neo4j knowledge graph. Uses neo4j
    (guarded — ImportError is deferred until first access).
pyg_builder : module
    Convert KG to PyTorch Geometric HeteroData for GNN training.
    Uses torch + torch_geometric (heavy — lazy-loaded).
graph_queries : module
    Cypher query utilities for graph traversal and search.
graph_stats : module
    KG statistics, validation, and sanity checks.
utils : module
    Shared utilities (identifier sanitization, type mapping). Pure
    Python — safe to import eagerly.
run_pipeline : module
    Main runner script to execute the full pipeline.
stitch_loader : module
    STITCH chemical-protein interaction ingestion.
sider_loader : module
    SIDER side effect database ingestion.
string_loader : module
    STRING protein-protein interaction ingestion.
chembl_loader : module
    ChEMBL bioactivity data ingestion.
opentargets_loader : module
    OpenTargets drug-target-disease evidence ingestion.
uniprot_loader : module
    UniProt Swiss-Prot protein data ingestion.
clinicaltrials_loader : module
    ClinicalTrials.gov AACT database ingestion.
geo_loader : module
    GEO Gene Expression Omnibus loader (Institutional-Grade v1.0.0).
    Downloads, parses, validates, and converts GEO SOFT files into
    Protein→expressed_in→Anatomy edges for the knowledge graph. GEO
    is the SOLE source of tissue-specificity data in the KG. The
    loader implements the ``Loader`` Protocol via the ``GeoLoader``
    adapter class and addresses all 192 audit findings across 16
    quality domains (GEO_LOADER_MASTER_REPAIR_PROMPT.md).
transe_model : module
    TransE knowledge graph embedding baseline model. Uses torch
    (heavy — lazy-loaded).
evaluation : module
    Link prediction evaluation metrics (AUC, P@K, MRR).
negative_sampling : module
    Negative sampling strategies for training data. Uses torch
    (heavy — lazy-loaded).
training_data : module
    Training data construction and temporal splitting.
chemberta_encoder : module
    ChemBERTa SMILES molecular embedding generation. Uses torch +
    transformers (heavy — lazy-loaded).
mlflow_tracker : module
    MLflow experiment tracking integration. Lazy import inside
    __init__.
gpu_utils : module
    GPU memory validation and batch testing. Uses torch (heavy —
    lazy-loaded).

Notes
-----
This package uses PEP 562 lazy imports. ``import drugos_graph`` loads
only the lightweight ``config``, ``utils``, and ``id_crosswalk``
modules; their public objects (``Neo4jConfig``, ``IDCrosswalk``, etc.)
are eagerly re-exported for backward compatibility. Heavy ML
dependencies (``torch``, ``torch_geometric``, ``transformers``,
``mlflow``) are loaded on first access of the corresponding
submodule or public API object.

Three version constants are exposed for reproducibility:

- ``__version__`` — package version (from ``importlib.metadata``).
- ``__schema_version__`` — bumps when ``CORE_NODE_TYPES``,
  ``CORE_EDGE_TYPES``, or ``CANONICAL_IDS`` change in ``config.py``.
  Two runs with the same ``__version__`` but different
  ``__schema_version__`` may produce non-equivalent graphs.
- ``__pipeline_version__`` — bumps when ``run_pipeline.py``'s step
  ordering or any transformation step changes. MLflow logs both
  ``__version__`` and ``__pipeline_version__`` so experiment runs are
  fully reproducible.
- ``__data_sources_version__`` — a dict mirroring the
  ``version_note`` strings in ``config.DATA_SOURCES`` in a
  programmatically accessible form. Downstream consumers (e.g. MLflow
  tracker) can log this to record exactly which dataset versions
  produced a given graph.

Examples
--------
Run the full pipeline::

    python -m drugos_graph

Access configuration::

    from drugos_graph import Neo4jConfig, DATA_SOURCES
    cfg = Neo4jConfig()

Build the knowledge graph::

    from drugos_graph import DrugOSGraphBuilder
    with DrugOSGraphBuilder() as builder:
        builder.connect()
        stats = builder.get_graph_stats()

Cross-database ID resolution (CRITICAL for scientific correctness)::

    from drugos_graph import get_default_crosswalk
    xw = get_default_crosswalk()
    # Builtin-known UniProt AC -> NCBI Gene ID (PTGS1 = COX-1, aspirin target).
    # Returns the primary (highest-priority) value as a string.
    gene_id = xw.uniprot_ac_to_ncbi_gene_id("P23219")   # -> "5742"
    # Reverse:
    uniprot_acs = xw.ncbi_gene_id_to_uniprot_ac("5742")  # -> "P23219"

For STRING/STITCH Ensembl-protein or OpenTargets ENSG resolution, run the
appropriate loader first (``load_string_aliases`` /
``load_opentargets_targets``) — see ``drugos_graph/id_crosswalk.py``.

Lazy import tier (skip ML stack in CI / data-only jobs)::

    import drugos_graph
    drugos_graph.import_tier("DATA")   # data pipeline only, no torch

Self-test::

    DRUGOS_SELF_TEST=1 python -c "import drugos_graph"
"""

from __future__ import annotations

# ─── 1. Standard library imports ──────────────────────────────────────────
import importlib
import logging
import os
import re
import sys
import warnings
from importlib.metadata import version as _pkg_version, PackageNotFoundError
from typing import Any, Callable

# ─── 2. Public metadata ───────────────────────────────────────────────────
# Single source of truth for the version. In a pip-installed deployment
# the value comes from the package metadata; in development mode (no
# `pip install`) it falls back to the literal below, which MUST match
# the `version` field in `pyproject.toml`.
try:
    __version__: str = _pkg_version("drugos-graph")
except PackageNotFoundError:  # pragma: no cover — dev mode only
    __version__: str = "2.0.0"

__author__: str = "DrugOS Team"
__email__: str = "team-cosmic@drugos.local"
__license__: str = "MIT"

# Schema version — bump when CORE_NODE_TYPES, CORE_EDGE_TYPES, or
# CANONICAL_IDS change in config.py. Two runs with the same
# __version__ but different __schema_version__ may produce
# non-equivalent graphs.
__schema_version__: str = "2.0.0"

# Pipeline version — bump when run_pipeline.py's step ordering or any
# transformation step changes. MLflow logs both __version__ and
# __pipeline_version__ so experiment runs are fully reproducible.
__pipeline_version__: str = "2.0.0-week2"

# Data-source version manifest — programmatically accessible counterpart
# to the `version_note` strings in config.DATA_SOURCES. Keys MUST match
# the keys of DATA_SOURCES; the validator below enforces this.
__data_sources_version__: dict[str, str] = {
    "drkg": "2.0",
    "drugbank": "5.1.12",  # pinned per audit issue 5.2
    "chembl": "35",  # pinned per audit issue 5.2
    "opentargets": "25.03",
    "string": "12.0",
    "uniprot": "2024_03",  # pinned per audit issue 5.2
    "clinicaltrials": "current",
    "stitch": "5.0",
    "sider": "2023-10-25",  # pinned per Phase 0.4 / D3.8
    "geo": "GSE92649",
    "reactome": "current",  # Fixes audit issue 3.4
    "kegg": "current",  # Fixes audit issue 3.4
}

# Semver-ish sanity check (BUG-IDEM-1). A warning is logged rather
# than raising because some development installs may report a non-
# semver string (e.g. editable installs with VCS suffixes).
if not re.match(r"^\d+\.\d+\.\d+", __version__ or ""):
    logging.getLogger(__name__).warning(
        "drugos_graph.__version__ is %r — does not look like semver",
        __version__,
    )

# ─── 3. Logger setup (idempotent — GUARD-REL-3) ───────────────────────────
# A NullHandler is attached exactly once, even under importlib.reload().
# This prevents the "No handlers could be found" warning in library
# consumers that have not configured logging, while not interfering
# with consumers that have.
_logger: logging.Logger = logging.getLogger(__name__)
if not any(isinstance(h, logging.NullHandler) for h in _logger.handlers):
    _logger.addHandler(logging.NullHandler())

# ─── 4. Module tier classification ────────────────────────────────────────
# Tier sets drive `import_tier()` (GAP-REL-2, BUG-INTEROP-1) and the
# `_ALL_MODULES == frozenset(__all__ lower-case names)` invariant check
# (GAP-DATA-1 regression guard).
_CORE_MODULES: frozenset[str] = frozenset({
    "config", "utils", "id_crosswalk", "entity_resolver",
    # New core support modules added by the uniprot_loader v2.0 audit fix:
    "exceptions",   # D6-006 — domain-specific exception hierarchy
    "schemas",      # D1-003 — UniProtRecord / ProteinNode TypedDicts
    # NOTE: _loader_protocol is intentionally NOT here (private, leading _).
})
_DATA_MODULES: frozenset[str] = frozenset({
    "drkg_loader", "drugbank_parser", "kg_builder", "graph_queries",
    "graph_stats", "stitch_loader", "sider_loader", "string_loader",
    "chembl_loader", "opentargets_loader", "uniprot_loader",
    "clinicaltrials_loader", "geo_loader", "run_pipeline",
    # Phase 1 → Phase 2 bridge (single authoritative contract connecting
    # the two halves of the platform). See phase1_bridge.py docstring.
    "phase1_bridge",
})
_ML_MODULES: frozenset[str] = frozenset({
    "pyg_builder", "transe_model", "evaluation",
    "negative_sampling", "training_data",
    "chemberta_encoder", "mlflow_tracker", "gpu_utils",
})
_ALL_MODULES: frozenset[str] = _CORE_MODULES | _DATA_MODULES | _ML_MODULES

# ─── 5. __all__ (complete, validated, tiered) ─────────────────────────────
# Hybrid shape: module names (lower-case) are kept for backward
# compatibility (`from drugos_graph import kg_builder`), AND public API
# object names (CamelCase / UPPER_CASE) are listed so `from drugos_graph
# import *` re-exports them (GAP-DESIGN-1, GAP-CODE-2, GAP-COMP-2).
__all__: list[str] = [
    # ── Metadata ──
    "__version__", "__author__", "__email__", "__license__",
    "__schema_version__", "__pipeline_version__", "__data_sources_version__",
    # ── Submodule facades (lazy) ──
    "config", "drkg_loader", "drugbank_parser", "kg_builder",
    "entity_resolver", "id_crosswalk",          # was missing — BUG-ARCH-1
    "exceptions", "schemas",                    # uniprot_loader v2.0 audit fix
    "pyg_builder", "graph_queries", "graph_stats",
    "utils", "run_pipeline", "stitch_loader", "sider_loader",
    "string_loader", "chembl_loader", "opentargets_loader",
    "uniprot_loader", "clinicaltrials_loader",
    "geo_loader",                                # was missing — BUG-ARCH-2
    "transe_model", "evaluation", "negative_sampling",
    "training_data", "chemberta_encoder",
    "mlflow_tracker", "gpu_utils",
    "phase1_bridge",                            # Phase 1 → Phase 2 connector
    # ── Public API objects (re-exported from submodules) ──
    "Neo4jConfig", "PyGConfig", "TransEConfig",
    "DATA_SOURCES", "CORE_NODE_TYPES", "CORE_EDGE_TYPES",
    "CANONICAL_IDS", "ensure_dirs",
    "IDCrosswalk", "get_default_crosswalk",
    "sanitize_identifier", "DRKG_NODE_TYPE_TO_NEO4J_LABEL",
    "DrugOSGraphBuilder", "PyGBuilder", "TransEModel",
    "EntityResolver", "NegativeSampler",
    "MLflowTracker",
    # ── Package-level entry points ──
    "configure_logging", "configure",
    "import_tier", "module_load_status", "lineage_report",
    "safe_config", "validate", "validate_schema",
]

# ─── 6. Lazy-loading infrastructure (PEP 562) ─────────────────────────────
# _LOADED_MODULES is the single source of truth for diagnostics
# (GUARD-LOG-2). Every lazy import (success, failure, or skipped)
# updates this dict so `module_load_status()` can report it.
_LOADED_MODULES: dict[str, str] = {}

# Lightweight re-exports — eagerly resolved at import time because the
# underlying modules are stdlib-lightweight AND because they are the
# most commonly accessed names. Heavy re-exports (DrugOSGraphBuilder,
# PyGBuilder, TransEModel, NegativeSampler, MLflowTracker) are NOT in
# this dict — they are resolved lazily through __getattr__.
_LIGHT_REEXPORTS: dict[str, tuple[str, str]] = {
    "Neo4jConfig":                    (".config",       "Neo4jConfig"),
    "PyGConfig":                      (".config",       "PyGConfig"),
    "TransEConfig":                   (".config",       "TransEConfig"),
    "DATA_SOURCES":                   (".config",       "DATA_SOURCES"),
    "CORE_NODE_TYPES":                (".config",       "CORE_NODE_TYPES"),
    "CORE_EDGE_TYPES":                (".config",       "CORE_EDGE_TYPES"),
    "CANONICAL_IDS":                  (".config",       "CANONICAL_IDS"),
    "ensure_dirs":                    (".config",       "ensure_dirs"),
    "IDCrosswalk":                    (".id_crosswalk", "IDCrosswalk"),
    "get_default_crosswalk":          (".id_crosswalk", "get_default_crosswalk"),
    "sanitize_identifier":            (".utils",        "sanitize_identifier"),
    "DRKG_NODE_TYPE_TO_NEO4J_LABEL":  (".utils",        "DRKG_NODE_TYPE_TO_NEO4J_LABEL"),
}

# Heavy re-exports — resolved lazily. Maps public name -> (module attr
# path relative to drugos_graph, attribute name). Importing any of
# these triggers the heavy dependency chain of the source module.
_HEAVY_REEXPORTS: dict[str, tuple[str, str]] = {
    "DrugOSGraphBuilder":          (".kg_builder",        "DrugOSGraphBuilder"),
    "PyGBuilder":                  (".pyg_builder",       "PyGBuilder"),
    "TransEModel":                 (".transe_model",      "TransEModel"),
    "EntityResolver":              (".entity_resolver",   "EntityResolver"),
    "NegativeSampler":             (".negative_sampling", "NegativeSampler"),
    "MLflowTracker":               (".mlflow_tracker",    "MLflowTracker"),
    # Fixes audit issue 1.2 — export graph_queries classes from package
    "DrugOSGraphQueries":          (".graph_queries",     "DrugOSGraphQueries"),
    "DrugRepurposingCandidate":    (".graph_queries",     "DrugRepurposingCandidate"),
}

# Names that should be resolvable through __getattr__ (lazy modules +
# heavy reexports + light reexports that somehow weren't populated).
_LAZY_MODULE_NAMES: frozenset[str] = _ALL_MODULES
_LAZY_OBJECT_NAMES: frozenset[str] = (
    frozenset(_HEAVY_REEXPORTS.keys()) | frozenset(_LIGHT_REEXPORTS.keys())
)


def __getattr__(name: str) -> Any:
    """PEP 562 module-level lazy attribute resolver.

    Resolution order:
      1. Names in ``_LIGHT_REEXPORTS`` — eagerly imported in
         ``_populate_light_reexports()`` and cached in ``__dict__``;
         this branch only fires if population failed or was skipped.
      2. Names in ``_HEAVY_REEXPORTS`` — imported on first access.
      3. Names in ``_LAZY_MODULE_NAMES`` (submodule facades) —
         ``importlib.import_module`` fires the heavy dependency chain
         of the target module.
      4. Deprecated names — emit ``DeprecationWarning`` then resolve.
      5. Anything else — raise ``AttributeError`` (NOT ``ImportError``,
         so ``hasattr()`` behaves correctly).
    """
    # Python internals sometimes probe for these — must raise AttributeError.
    if name in ("__path__", "__warningregistry__", "__deprecated_aliases__"):
        raise AttributeError(name)

    # 1. Light reexport fallback (normally already in __dict__).
    if name in _LIGHT_REEXPORTS:
        mod_rel, attr_name = _LIGHT_REEXPORTS[name]
        mod = importlib.import_module(mod_rel, __name__)
        obj = getattr(mod, attr_name)
        globals()[name] = obj  # cache for next access
        return obj

    # 2. Heavy reexport — triggers neo4j/torch/transformers import.
    if name in _HEAVY_REEXPORTS:
        mod_rel, attr_name = _HEAVY_REEXPORTS[name]
        try:
            mod = importlib.import_module(mod_rel, __name__)
        except ImportError as exc:
            _LOADED_MODULES[mod_rel.lstrip(".")] = f"FAILED: {exc}"
            _logger.warning(
                "drugos_graph.%s could not be imported (%s). Install the "
                "corresponding optional dependency if you need this module.",
                mod_rel.lstrip("."), exc,
            )
            raise
        obj = getattr(mod, attr_name)
        globals()[name] = obj  # cache
        _LOADED_MODULES[mod_rel.lstrip(".")] = "ok"
        return obj

    # 3. Lazy submodule facade.
    if name in _LAZY_MODULE_NAMES:
        try:
            mod = importlib.import_module(f"{__name__}.{name}")
        except ImportError as exc:
            _LOADED_MODULES[name] = f"FAILED: {exc}"
            _logger.warning(
                "drugos_graph.%s could not be imported (%s). Install the "
                "corresponding optional dependency if you need this module.",
                name, exc,
            )
            raise
        _LOADED_MODULES[name] = "ok"
        globals()[name] = mod  # cache so __getattr__ isn't called again
        return mod

    # 4. Deprecation shim — emit warning then fall through to AttributeError.
    if name in __deprecated__:
        _emit_deprecation(name)
        new_name = __deprecated__[name]
        if new_name in globals() or new_name in _LAZY_OBJECT_NAMES:
            return getattr(__import__(__name__), new_name)

    # 5. Unknown attribute — MUST be AttributeError for hasattr() correctness.
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Tab-completion support for IPython / Jupyter (GAP-DOC-3)."""
    return sorted(set(list(globals().keys()) + list(__all__)))


# ─── 7. Public API re-exports (lightweight only — eager) ──────────────────
# Eagerly populate the lightweight reexports so `from drugos_graph import
# Neo4jConfig` does not pay the __getattr__ cost. Each import is wrapped
# in try/except so a missing lightweight dep (e.g. pandas for
# id_crosswalk's optional loaders) does not break package import — the
# heavy fallback path through __getattr__ still works.
def _populate_light_reexports() -> None:
    """Populate ``__dict__`` with lightweight re-exported objects.

    Failures are non-fatal — a name that can't be resolved here will
    be retried through ``__getattr__`` on first access.
    """
    for public_name, (mod_rel, attr_name) in _LIGHT_REEXPORTS.items():
        if public_name in globals():
            continue  # already populated (e.g. by a prior call)
        try:
            mod = importlib.import_module(mod_rel, __name__)
            obj = getattr(mod, attr_name)
            globals()[public_name] = obj
            _LOADED_MODULES[mod_rel.lstrip(".")] = "ok"
        except ImportError as exc:
            # Light modules are stdlib-only; an ImportError here is rare
            # but we tolerate it so package import stays resilient.
            _LOADED_MODULES[mod_rel.lstrip(".")] = f"FAILED: {exc}"
            _logger.debug(
                "Light reexport %s failed: %s", public_name, exc,
            )


_populate_light_reexports()

# ─── 8. Deprecation shim (GUARD-DESIGN-3) ─────────────────────────────────
# Map of deprecated public name -> recommended replacement. Currently
# empty; entries may be added in future releases. The dict is exposed
# publicly so downstream test suites can assert no accidental deprecations.
__deprecated__: dict[str, str] = {}


def _emit_deprecation(old_name: str) -> None:
    """Emit a DeprecationWarning for a deprecated public name."""
    new_name = __deprecated__.get(old_name)
    if new_name:
        warnings.warn(
            f"drugos_graph.{old_name} is deprecated and will be removed "
            f"in a future release. Use {new_name} instead.",
            DeprecationWarning,
            stacklevel=3,
        )

# ─── 9. Import-time validators ────────────────────────────────────────────
# _VALIDATORS is populated below. Each validator returns a list of issue
# strings (empty list = pass). `validate()` runs them all and returns a
# report; `validate(strict=True)` raises if any issue is found.
_VALIDATORS: list[Callable[[], list[str]]] = []

# Critical (lightweight) deps that, if missing, indicate a broken
# install. Heavy deps (torch, transformers, mlflow) are intentionally
# NOT checked here — they're optional from the package's perspective.
_CRITICAL_LIGHT_DEPS: tuple[str, ...] = (
    "pandas", "numpy", "networkx",
)


def _validate_critical_dependencies() -> list[str]:
    """Check that lightweight critical deps are importable.

    Heavy deps (torch, transformers, mlflow) are intentionally NOT
    checked here — they're validated lazily on first access of the
    corresponding submodule. Returns a list of issue strings; an
    empty list means all critical deps are present.
    """
    issues: list[str] = []
    for dep in _CRITICAL_LIGHT_DEPS:
        try:
            importlib.import_module(dep)
        except ImportError as exc:
            issues.append(
                f"Critical dependency {dep!r} could not be imported: {exc}. "
                f"Install it via `pip install {dep}`."
            )
    return issues


def _discover_actual_submodules() -> set[str]:
    """Discover the actual submodule names present on disk.

    Used by ``_check_all_complete()`` to detect drift between
    ``__all__`` and the file system.
    """
    import pkgutil
    actual: set[str] = set()
    for _finder, name, _ispkg in pkgutil.iter_modules(__path__):
        if name.startswith("_") or name == "__main__":
            continue
        actual.add(name)
    return actual


def _check_all_complete() -> list[str]:
    """Detect drift between ``__all__`` and the actual files on disk.

    Returns a list of issue strings; an empty list means ``__all__``
    is in sync with the file system. This catches the exact bug that
    produced BUG-ARCH-1 / BUG-ARCH-2 (``id_crosswalk`` and
    ``geo_loader`` were missing from ``__all__``).

    The check compares the *module facade* names declared in
    ``__all__`` (i.e. names also present in ``_LAZY_MODULE_NAMES``)
    against the actual submodules discovered by ``pkgutil``. Function
    and object names in ``__all__`` (e.g. ``configure``,
    ``ensure_dirs``, ``Neo4jConfig``) are intentionally NOT compared
    against disk because they are not files — they are attributes
    re-exported from submodules.
    """
    actual = _discover_actual_submodules()
    # Only compare module facade names — i.e. names that are in BOTH
    # __all__ AND _LAZY_MODULE_NAMES. Function/object reexports like
    # `configure`, `ensure_dirs`, `Neo4jConfig` are not files and must
    # not be flagged here.
    declared = {n for n in __all__ if n in _LAZY_MODULE_NAMES}
    issues: list[str] = []
    missing_from_all = actual - declared
    missing_from_disk = declared - actual
    if missing_from_all:
        issues.append(
            f"Modules on disk but not in __all__: {sorted(missing_from_all)}"
        )
    if missing_from_disk:
        issues.append(
            f"Modules in __all__ but not on disk: {sorted(missing_from_disk)}"
        )
    return issues


def _validate_all() -> list[str]:
    """Validate every name in ``__all__`` resolves without error.

    Returns a list of failure descriptions; an empty list means every
    name is resolvable. Heavy names are skipped if their underlying
    dependency is not installed (those are reported by
    ``_validate_critical_dependencies`` and ``_check_dependency_versions``).
    """
    failures: list[str] = []
    pkg = sys.modules[__name__]
    for name in __all__:
        if name.startswith("__") and name.endswith("__"):
            # Metadata dunder — verify it's a string/dict.
            try:
                val = getattr(pkg, name)
                if not isinstance(val, (str, dict)):
                    failures.append(
                        f"{name}: expected str or dict, got {type(val).__name__}"
                    )
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        try:
            getattr(pkg, name)
        except AttributeError as exc:
            failures.append(f"{name}: AttributeError: {exc}")
        except ImportError as exc:
            # Heavy optional dep missing — non-fatal but report.
            failures.append(f"{name}: ImportError (optional dep missing): {exc}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    return failures


def _validate_scientific_schema() -> list[str]:
    """Validate the scientific schema in ``config.py``.

    Checks:
      - Every edge endpoint in ``CORE_EDGE_TYPES`` is a known node
        type (union of ``CORE_NODE_TYPES`` and ``DRKG_NODE_TYPES``).
      - Every key in ``CANONICAL_IDS`` is a known node type.
      - The ``Compound::treats::Disease`` edge exists (Phase 2 spec).
      - The ``Gene::encodes::Protein`` bridge edge exists (prevents
        disconnected Gene/Protein subgraphs).

    Returns a list of issue strings; an empty list means the schema
    is internally consistent and biologically sound.
    """
    try:
        from .config import (
            CORE_NODE_TYPES, CORE_EDGE_TYPES, CANONICAL_IDS, DRKG_NODE_TYPES,
        )
    except ImportError as exc:
        return [f"Cannot import config for schema validation: {exc}"]

    issues: list[str] = []
    node_set: set[str] = set(CORE_NODE_TYPES) | set(DRKG_NODE_TYPES)

    for src, rel, dst in CORE_EDGE_TYPES:
        if src not in node_set:
            issues.append(
                f"Edge {rel!r}: unknown source node type {src!r}"
            )
        if dst not in node_set:
            issues.append(
                f"Edge {rel!r}: unknown destination node type {dst!r}"
            )

    for ent_type in CANONICAL_IDS:
        if ent_type not in node_set:
            issues.append(
                f"CANONICAL_IDS key {ent_type!r} is not a known node type"
            )

    if ("Compound", "treats", "Disease") not in CORE_EDGE_TYPES:
        issues.append(
            "Core edge ('Compound', 'treats', 'Disease') is missing — "
            "this is the Phase 2 spec contract for the link-prediction "
            "target edge type."
        )

    if ("Gene", "encodes", "Protein") not in CORE_EDGE_TYPES:
        issues.append(
            "Bridge edge ('Gene', 'encodes', 'Protein') is missing — "
            "without it the graph splits into disconnected Gene and "
            "Protein subgraphs."
        )

    return issues


def _validate_data_sources() -> list[str]:
    """Validate the ``DATA_SOURCES`` dict in ``config.py``.

    Checks that every entry has the required keys (``url``,
    ``filename``, ``description``, ``version_note``) and that the
    ``url`` is a valid HTTP/HTTPS/FTP URL.
    """
    try:
        from .config import DATA_SOURCES
    except ImportError as exc:
        return [f"Cannot import config for DATA_SOURCES validation: {exc}"]

    issues: list[str] = []
    required_keys = {"url", "filename", "description", "version_note", "version", "pinned", "sha256"}
    for src_name, src_cfg in DATA_SOURCES.items():
        missing = required_keys - set(src_cfg.keys())
        if missing:
            issues.append(
                f"DATA_SOURCES[{src_name!r}] missing required keys: {sorted(missing)}"
            )
        url = str(src_cfg.get("url", ""))
        if not url.startswith(("http://", "https://", "ftp://")):
            issues.append(
                f"DATA_SOURCES[{src_name!r}].url is not a valid URL: {url!r}"
            )

    # Cross-check that __data_sources_version__ keys match DATA_SOURCES keys.
    src_keys = set(DATA_SOURCES.keys())
    version_keys = set(__data_sources_version__.keys())
    if src_keys != version_keys:
        only_in_sources = src_keys - version_keys
        only_in_version = version_keys - src_keys
        if only_in_sources:
            issues.append(
                f"__data_sources_version__ is missing keys present in "
                f"DATA_SOURCES: {sorted(only_in_sources)}"
            )
        if only_in_version:
            issues.append(
                f"__data_sources_version__ has extra keys not in "
                f"DATA_SOURCES: {sorted(only_in_version)}"
            )

    return issues


def _validate_config_paths() -> list[str]:
    """Validate that the parent directories of all configured paths
    are writable.

    Returns a list of issue strings; an empty list means all paths
    can be created.
    """
    try:
        from .config import (
            DATA_DIR, RAW_DIR, PROCESSED_DIR, KG_DIR,
            EMBEDDINGS_DIR, LOGS_DIR, MODEL_DIR,
        )
    except ImportError as exc:
        return [f"Cannot import config for path validation: {exc}"]

    issues: list[str] = []
    for label, path in [
        ("DATA_DIR", DATA_DIR), ("RAW_DIR", RAW_DIR),
        ("PROCESSED_DIR", PROCESSED_DIR), ("KG_DIR", KG_DIR),
        ("EMBEDDINGS_DIR", EMBEDDINGS_DIR), ("LOGS_DIR", LOGS_DIR),
        ("MODEL_DIR", MODEL_DIR),
    ]:
        parent = path.parent
        if not parent.exists():
            issues.append(
                f"{label}={path}: parent dir {parent} does not exist"
            )
        elif not os.access(str(parent), os.W_OK):
            issues.append(
                f"{label}={path}: parent dir {parent} not writable"
            )
    return issues


# Required (dep, min_version) pairs per module. Used by
# _check_dependency_versions(). Modules whose deps are not installed
# are skipped (they're reported by _validate_critical_dependencies for
# the lightweight set, and by lazy __getattr__ for the heavy set).
_REQUIRED_DEP_VERSIONS: dict[str, tuple[str, tuple[int, ...]]] = {
    "kg_builder":         ("neo4j",           (5, 0, 0)),
    "pyg_builder":        ("torch_geometric", (2, 4, 0)),
    "transe_model":       ("torch",           (2, 0, 0)),
    "negative_sampling":  ("torch",           (2, 0, 0)),
    "chemberta_encoder":  ("transformers",    (4, 30, 0)),
    "mlflow_tracker":     ("mlflow",          (2, 0, 0)),
    "drkg_loader":        ("networkx",        (3, 0, 0)),
    "evaluation":         ("scikit-learn",    (1, 3, 0)),
}


def _parse_version_tuple(ver: str) -> tuple[int, ...]:
    """Parse the leading major.minor.patch digits of a version string.

    Non-numeric components (e.g. ``rc0``, ``+cpu``) are ignored.
    """
    parts: list[int] = []
    for chunk in ver.split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            parts.append(int(digits))
        if len(parts) >= 3:
            break
    return tuple(parts)


def _check_dependency_versions() -> list[str]:
    """Check installed dependency versions against required minimums.

    Returns a list of issue strings; an empty list means all installed
    deps meet the minimum version requirement. Missing deps are NOT
    reported here (they're optional from the package's perspective).
    """
    issues: list[str] = []
    for module, (dep, min_tuple) in _REQUIRED_DEP_VERSIONS.items():
        try:
            installed = _pkg_version(dep)
        except PackageNotFoundError:
            continue  # missing deps are reported elsewhere
        installed_tuple = _parse_version_tuple(installed)
        if installed_tuple and installed_tuple < min_tuple:
            min_str = ".".join(str(x) for x in min_tuple)
            issues.append(
                f"{module} requires {dep}>={min_str}, "
                f"but {installed} is installed"
            )
    return issues


# Patterns that indicate a secret value being logged. Used by
# _scan_for_secret_logging() (only runs under DRUGOS_SELF_TEST=2 to
# avoid import-time cost in production).
_SECRET_LOG_PATTERNS: tuple[str, ...] = (
    r"logger\.\w+\([^)]*\bpassword\b",
    r"logger\.\w+\([^)]*\bapi[_-]?key\b",
    r"logger\.\w+\([^)]*\bsecret\b",
    r"logger\.\w+\([^)]*\btoken\b",
    r"print\([^)]*\bpassword\b",
    r"print\([^)]*\bapi[_-]?key\b",
)


def _scan_for_secret_logging() -> list[str]:
    """Scan every successfully imported module's source for suspicious
    logging of secret values.

    Returns a list of findings (``module: matched source line``). Only
    runs under ``DRUGOS_SELF_TEST=2`` to avoid import-time cost in
    production.
    """
    import inspect
    findings: list[str] = []
    pattern = re.compile("|".join(_SECRET_LOG_PATTERNS), re.IGNORECASE)
    for name in _ALL_MODULES:
        if name not in globals():
            try:
                mod = importlib.import_module(f"{__name__}.{name}")
            except (ImportError, AttributeError, TypeError, OSError) as _imp_exc:
                # v41 ROOT FIX (Task J SEV3): narrowed from bare
                # ``except Exception: continue``. Module-import failures
                # during the security scan are:
                #   - ImportError: module or its deps not installed
                #     (e.g. torch, transformers, neo4j missing in CI).
                #   - AttributeError: module-level symbol resolution
                #     failure (config regression).
                #   - TypeError: bad __init__ signature in a submodule.
                #   - OSError: filesystem error reading the module.
                # Other exceptions (KeyboardInterrupt, SystemExit,
                # MemoryError) should propagate. Logging at DEBUG so the
                # security-scan output isn't polluted, but operators can
                # grep for skipped modules.
                import logging as _logging
                _logging.getLogger(__name__).debug(
                    "Security scan skipped module %s (%s: %s).",
                    name, type(_imp_exc).__name__, _imp_exc,
                )
                continue
        else:
            mod = globals()[name]
        try:
            src = inspect.getsource(mod)
        except (TypeError, OSError):
            continue
        for match in pattern.finditer(src):
            findings.append(f"{name}: {match.group(0)!r}")
    return findings


_VALIDATORS.extend([
    _validate_critical_dependencies,
    _validate_scientific_schema,
    _validate_data_sources,
    _validate_config_paths,
    _check_all_complete,
    _check_dependency_versions,
])


def validate(strict: bool = False) -> dict[str, list[str]]:
    """Run all import-time validators. Returns a report dict.

    Parameters
    ----------
    strict : bool, default False
        If True, raise ``AssertionError`` on any failure.

    Returns
    -------
    dict[str, list[str]]
        Mapping from validator function name to list of issue strings.
        An empty list means that validator passed.
    """
    report: dict[str, list[str]] = {}
    for validator in _VALIDATORS:
        try:
            report[validator.__name__] = list(validator())
        except Exception as exc:  # noqa: BLE001
            report[validator.__name__] = [
                f"VALIDATOR CRASHED: {type(exc).__name__}: {exc}"
            ]
    if strict:
        all_issues = [i for issues in report.values() for i in issues]
        assert not all_issues, f"Validation failures: {all_issues}"
    return report


def validate_schema() -> dict[str, list[str]]:
    """Run only the scientific-schema validator (convenience wrapper).

    Useful for downstream tools that want to check schema integrity
    without running the full validation suite.
    """
    return {"_validate_scientific_schema": list(_validate_scientific_schema())}


# ─── 10. configure_logging() and configure() entry points ─────────────────
def configure_logging(
    level: str | int = "INFO",
    format_str: str | None = None,
    handler: logging.Handler | None = None,
) -> None:
    """Configure logging for the entire drugos_graph package.

    Idempotent: previously-added non-NullHandler handlers are removed
    before the new handler is attached, so multiple calls do not
    accumulate handlers (GUARD-REL-3 regression guard).

    Parameters
    ----------
    level : str or int, default "INFO"
        Logging level — 'DEBUG', 'INFO', 'WARNING', 'ERROR', or
        the numeric equivalent.
    format_str : str, optional
        Format string. Defaults to ``config.LOG_FORMAT``.
    handler : logging.Handler, optional
        Pre-configured handler. Defaults to ``StreamHandler`` with
        the given format.
    """
    try:
        from .config import LOG_FORMAT
    except ImportError:
        LOG_FORMAT = (
            "%(asctime)s | %(name)s | %(levelname)s | %(message)s"
        )
    fmt = format_str or LOG_FORMAT
    if handler is None:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(fmt))
    pkg_logger = logging.getLogger(__name__)
    pkg_logger.setLevel(level)
    # Remove prior non-NullHandler handlers to stay idempotent.
    for h in list(pkg_logger.handlers):
        if not isinstance(h, logging.NullHandler):
            pkg_logger.removeHandler(h)
    pkg_logger.addHandler(handler)
    pkg_logger.info("drugos_graph logging configured at level %s", level)


def configure(
    data_dir: str | os.PathLike[str] | None = None,
    log_level: str | int | None = None,
    neo4j_password: str | None = None,
    neo4j_uri: str | None = None,
    neo4j_user: str | None = None,
    mlflow_tracking_uri: str | None = None,
) -> None:
    """Configure the drugos_graph package at runtime.

    Sets environment variables consumed by ``config.py`` and
    ``mlflow_tracker.py``. Call this BEFORE importing any submodule
    that depends on these settings.

    Parameters
    ----------
    data_dir : str or PathLike, optional
        Override ``DATA_DIR`` (sets ``DRUGOS_DATA_DIR`` env var).
    log_level : str or int, optional
        'DEBUG'/'INFO'/'WARNING'/'ERROR' (sets ``DRUGOS_LOG_LEVEL``).
    neo4j_password : str, optional
        Neo4j password (sets ``DRUGOS_NEO4J_PASSWORD``).
    neo4j_uri : str, optional
        Neo4j URI (sets ``DRUGOS_NEO4J_URI``).
    neo4j_user : str, optional
        Neo4j username (sets ``DRUGOS_NEO4J_USER``).
    mlflow_tracking_uri : str, optional
        MLflow tracking URI (sets ``MLFLOW_TRACKING_URI``).
    """
    if data_dir is not None:
        os.environ["DRUGOS_DATA_DIR"] = str(data_dir)
    if log_level is not None:
        os.environ["DRUGOS_LOG_LEVEL"] = str(log_level)
        configure_logging(level=log_level)
    if neo4j_password is not None:
        os.environ["DRUGOS_NEO4J_PASSWORD"] = neo4j_password
    if neo4j_uri is not None:
        os.environ["DRUGOS_NEO4J_URI"] = neo4j_uri
    if neo4j_user is not None:
        os.environ["DRUGOS_NEO4J_USER"] = neo4j_user
    if mlflow_tracking_uri is not None:
        os.environ["MLFLOW_TRACKING_URI"] = mlflow_tracking_uri
    _logger.info(
        "drugos_graph.configure() applied: data_dir=%s, log_level=%s",
        data_dir, log_level,
    )

# ─── 11. Tiered import + diagnostics ──────────────────────────────────────
def import_tier(tier: str = "CORE") -> dict[str, object]:
    """Import all modules in a tier, skipping ones whose deps are missing.

    Parameters
    ----------
    tier : str, default "CORE"
        One of "CORE", "DATA", "ML", or "ALL".

    Returns
    -------
    dict[str, object]
        Mapping from module name to module object (or ``None`` if the
        module's deps are missing). Missing-dep modules are recorded
        in ``_LOADED_MODULES`` with status ``'SKIPPED: <reason>'``.
    """
    tier_map: dict[str, frozenset[str]] = {
        "CORE": _CORE_MODULES,
        "DATA": _DATA_MODULES,
        "ML": _ML_MODULES,
        "ALL": _ALL_MODULES,
    }
    if tier not in tier_map:
        raise ValueError(
            f"Unknown tier {tier!r}; choose from {sorted(tier_map)}"
        )
    results: dict[str, object] = {}
    pkg = sys.modules[__name__]
    for name in tier_map[tier]:
        if name in globals():
            # Already cached (e.g. by a prior __getattr__ call, or by
            # Python's `from drugos_graph import <name>` machinery).
            # Ensure _LOADED_MODULES reflects this so module_load_status()
            # stays accurate.
            if _LOADED_MODULES.get(name) not in ("ok",):
                _LOADED_MODULES[name] = "ok"
            results[name] = globals()[name]
            continue
        try:
            results[name] = getattr(pkg, name)
        except ImportError as exc:
            _LOADED_MODULES[name] = f"SKIPPED: {exc}"
            results[name] = None
            _logger.warning("Skipped drugos_graph.%s (%s)", name, exc)
    return results


def module_load_status() -> dict[str, str]:
    """Return a copy of the module-load registry (for diagnostics).

    The registry maps module name -> status string. Status values:
      - ``"ok"`` — module imported successfully.
      - ``"FAILED: <reason>"`` — import raised ``ImportError``.
      - ``"SKIPPED: <reason>"`` — import was skipped by ``import_tier``
        because a dependency was missing.

    This call also synchronizes the registry with ``sys.modules`` so
    that submodules loaded via ``from drugos_graph import <name>``
    (which bypasses ``__getattr__``) are accurately reflected.
    """
    # Sync with sys.modules: any drugos_graph.<name> that's in
    # sys.modules but not in _LOADED_MODULES is implicitly 'ok'.
    for full_name in list(sys.modules):
        if not full_name.startswith(f"{__name__}."):
            continue
        short_name = full_name.rsplit(".", 1)[-1]
        if short_name in _ALL_MODULES and short_name not in _LOADED_MODULES:
            _LOADED_MODULES[short_name] = "ok"
    return dict(_LOADED_MODULES)


def lineage_report() -> dict[str, object]:
    """Return a complete provenance report for this package instance.

    The report includes package version, schema version, pipeline
    version, data-source versions, Python version, and the
    module-load registry. Downstream tools (e.g. MLflow tracker)
    can log this dict to record exactly which dataset versions and
    code state produced a given graph.
    """
    return {
        "package_version": __version__,
        "schema_version": __schema_version__,
        "pipeline_version": __pipeline_version__,
        "data_sources_version": dict(__data_sources_version__),
        "python_version": sys.version.split()[0],
        "loaded_modules": module_load_status(),
    }


# Allowlist of non-secret config attributes. Use ``safe_config()`` to
# get a dict of these without risking accidental exposure of
# Neo4jConfig (which reads DRUGOS_NEO4J_PASSWORD from env).
_SAFE_CONFIG_EXPORTS: frozenset[str] = frozenset({
    "DATA_SOURCES", "CORE_NODE_TYPES", "CORE_EDGE_TYPES",
    "DRKG_NODE_TYPES", "CANONICAL_IDS", "ID_MAPPING_PRIORITY",
    "DATA_DIR", "RAW_DIR", "PROCESSED_DIR", "KG_DIR",
    "EMBEDDINGS_DIR", "LOGS_DIR", "MODEL_DIR",
    "MIN_NODES_W2", "MIN_EDGES_W2", "MIN_POSITIVE_PAIRS",
    "MIN_NEGATIVE_PAIRS", "TARGET_TRANSE_AUC",
    "STRING_SCORE_THRESHOLD", "STITCH_SCORE_THRESHOLD",
    "ENTITY_CONFIDENCE_THRESHOLD", "ENTITY_MATCH_RATE",
    "PyGConfig", "TransEConfig", "ensure_dirs",
    "LOG_FORMAT", "LOG_LEVEL",
})


def safe_config() -> dict[str, object]:
    """Return a dict of public, non-secret config values.

    Use this instead of ``from drugos_graph import config`` if you
    only need path/schema constants and want to avoid accidentally
    exposing ``Neo4jConfig`` (which reads ``DRUGOS_NEO4J_PASSWORD``
    from env).
    """
    from . import config as _c
    return {
        k: getattr(_c, k)
        for k in _SAFE_CONFIG_EXPORTS
        if hasattr(_c, k)
    }


# ─── 12. Compatibility matrix & dependency version check ──────────────────
# (Implemented above as _check_dependency_versions.)

# ─── 13. Module-load status registry ──────────────────────────────────────
# (Implemented above as _LOADED_MODULES + module_load_status().)

# ─── 14. __dir__ for tab-completion ───────────────────────────────────────
# (Implemented above as __dir__().)

# ─── Self-test hook (DRUGOS_SELF_TEST env var) ────────────────────────────
# Runs the full validation suite at import time when the env var is
# set. Useful for CI smoke tests and post-install verification.
# DRUGOS_SELF_TEST=1 — run all validators.
# DRUGOS_SELF_TEST=2 — also run the slow secret-logging scan.
if os.environ.get("DRUGOS_SELF_TEST"):
    _logger.info(
        "DRUGOS_SELF_TEST=%s — running full self-validation",
        os.environ["DRUGOS_SELF_TEST"],
    )
    _self_test_report: dict[str, list[str]] = validate(strict=False)
    for _vname, _issues in _self_test_report.items():
        if _issues:
            _logger.warning(
                "Self-test %s: %d issue(s):", _vname, len(_issues)
            )
            for _i in _issues:
                _logger.warning("  - %s", _i)
        else:
            _logger.info("Self-test %s: PASS", _vname)
    if os.environ.get("DRUGOS_SELF_TEST") == "2":
        _secret_findings: list[str] = _scan_for_secret_logging()
        for _f in _secret_findings:
            _logger.error("SECRET LOGGING: %s", _f)
        if not _secret_findings:
            _logger.info("Self-test secret-logging scan: PASS")
