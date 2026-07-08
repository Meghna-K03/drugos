# SPDX-License-Identifier: MIT
# © 2024-2026 Autonomous Drug Repurposing Platform — Team Cosmic / VentureLab
"""
Entity resolution package for the Drug Repurposing ETL platform.

Provides cross-database entity resolution for drugs and proteins,
reconciling identifiers across **five** biomedical databases:

* **ChEMBL** — chemical compounds + bioactivity data (drug records)
* **DrugBank** — FDA-approved drug profiles (drug records)
* **PubChem** — structural / property data (drug records, opt-in
  network lookup)
* **UniProt** — protein sequences / functions (protein records —
  canonical)
* **STRING** — protein-protein interactions (protein records — merged)

DisGeNET and OMIM are **out of scope** for this package — they are
disease databases, not entity-identity databases, and are handled by
the ``cleaning`` and ``database`` packages downstream.

Quick Start
-----------
Import everything from the package top-level; never import from
submodules directly.  The package uses PEP 562 lazy loading, so
``import entity_resolution`` is essentially free (~1 ms) and pandas /
requests / rapidfuzz are only loaded when a resolver is actually
constructed.

>>> from entity_resolution import DrugResolver, ProteinResolver
>>> from entity_resolution import normalize_name, is_valid_inchikey
>>> normalize_name("Aspirin (acetylsalicylic acid)")
'aspirin'
>>> is_valid_inchikey("BSYNRYMUTXBXSQ-UHFFFAOYSA-N")
True
>>> is_valid_inchikey("not-an-inchikey")
False
>>> resolver = DrugResolver()  # safe defaults: PubChem off, stereoisomers preserved
>>> resolver.config.collapse_stereoisomers
False
>>> resolver.config.pubchem_enabled
False

Resolution Strategies
---------------------
**Drugs** are reconciled by InChIKey (the canonical chemical
identifier) with this priority order:

1. ``inchikey_exact`` — full 27-char InChIKey equality (confidence 1.0)
2. ``inchikey_connectivity`` — first 14 chars equal (confidence 0.9).
   **Off by default** — see *Stereoisomer Safety Warning* below.
3a. ``name_normalized`` — exact match after :func:`normalize_name`
   (confidence 0.8)
3b. ``fuzzy`` — :func:`rapidfuzz.fuzz.token_sort_ratio` ≥
   :attr:`ResolverConfig.fuzzy_threshold
   <entity_resolution.base.ResolverConfig.fuzzy_threshold>`
   (confidence 0.85).  The reported confidence is always ≥ the
   threshold (audit D3-3).
4. ``pubchem_xref`` — PubChem REST API name → InChIKey lookup
   (confidence 0.7).  **Off by default** — see *Network Side
   Effects* below.

**Proteins** are reconciled by UniProt accession with this priority
order:

1. ``uniprot_exact`` — UniProt accession equality (confidence 1.0)
2. STRING → UniProt cross-reference (confidence 1.0 when the
   cross-reference was established from a UniProt-supplied STRING ID)
3. ``gene_name_organism`` — ``(gene_symbol.upper(), organism.lower())``
   match (confidence 0.85)
4. ``protein_name_fuzzy`` — protein-name fuzzy match with a stricter
   0.90 threshold to suppress false positives (confidence 0.6)

Bulk vs. Single-Record Mode
---------------------------
The bulk path :meth:`DrugResolver.build_mapping` and
:meth:`ProteinResolver.build_mapping` **never** make network calls —
they ingest pre-fetched DataFrames.  PubChem lookup is confined to
:meth:`DrugResolver.resolve_single`, which is the single-record path
used for ad-hoc lookups.  This asymmetry is intentional and
documented (audit D3-1) — bulk ETL must be deterministic and
reproducible, so it cannot depend on a third-party HTTP service.

Stereoisomer Safety Warning
---------------------------
.. warning::
   The default ``collapse_stereoisomers=False`` is a **patient-safety
   setting**.  Two InChIKeys sharing the same 14-char connectivity
   block represent the same molecular skeleton but may have
   different stereochemistry — and stereochemistry can drastically
   change pharmacology.  Thalidomide enantiomers are the canonical
   example: one is a sedative, the other is a teratogen.  Warfarin,
   citalopram, and many other drugs have similar enantiomer-specific
   safety profiles.

   When ``collapse_stereoisomers=False`` (the default),
   :meth:`DrugResolver._match_by_connectivity
   <entity_resolution.drug_resolver.DrugResolver._match_by_connectivity>`
   refuses to merge two InChIKeys unless their full 27-char forms are
   identical.  If you explicitly opt in via
   ``ResolverConfig(collapse_stereoisomers=True)``, every collapse is
   logged at WARNING and recorded in the canonical entry's
   ``collapsed_stereoisomers`` list so downstream pharmacovigilance
   code can detect it.

Synthetic InChIKey Convention
-----------------------------
Source records occasionally have no real InChIKey (e.g. a drug is in
early development and no structure has been published).  The resolver
generates a **source-independent** synthetic key by hashing the
normalized name with SHA-256 and embedding it in the InChIKey *shape*
(``SYNTH{14 chars}-{10 chars}-{1 char}``).  This means the same
InChIKey-less drug from ChEMBL vs DrugBank gets the **same** synthetic
key and is correctly merged — fixing audit D3-5, where the previous
``sha256(name:source)`` scheme split the two records into different
canonical entries.

Detect synthetic keys with :func:`is_synthetic_inchikey`:

>>> from entity_resolution import is_synthetic_inchikey
>>> is_synthetic_inchikey("SYNTHABCDEFGHI-NOPQRSTUVW-X")
True
>>> is_synthetic_inchikey("BSYNRYMUTXBXSQ-UHFFFAOYSA-N")
False

PubChem Cross-Reference Ambiguity
---------------------------------
PubChem name lookups may resolve to a salt form (e.g. "aspirin" →
"aspirin sodium") rather than the free acid.  Salt forms have
different pharmacology from the free acid.  Set
:attr:`ResolverConfig.pubchem_strict_salt_form
<entity_resolution.base.ResolverConfig.pubchem_strict_salt_form>`
to ``True`` to enable salt-form rejection (currently heuristic; a
full implementation would inspect the PubChem compound record's
component atoms).

Network Side Effects
--------------------
.. warning::
   Calling :meth:`DrugResolver.resolve_single` with
   ``pubchem_enabled=True`` triggers real HTTPS calls to
   ``pubchem_rest_base`` (default: ``https://pubchem.ncbi.nlm.nih.gov``).
   Drug names are sent in the URL path.  This is the fix for audit
   D9-1: the previous default silently leaked drug names to a
   third-party service.  The new default is **off**; opt in via
   ``ResolverConfig(pubchem_enabled=True)`` or the
   ``ENTITY_RESOLUTION_PUBCHEM_ENABLED=1`` env var.

   The bulk path :meth:`DrugResolver.build_mapping` makes zero
   network calls regardless of the ``pubchem_enabled`` flag.

Configuration Rationale
-----------------------
Every magic number that previously lived as a private module-level
constant now has a documented home in
:class:`~entity_resolution.base.ResolverConfig`.  Every field has an
env-var override (prefix ``ENTITY_RESOLUTION_``) so deployments can
re-tune the resolver without editing source.  The default values are
chosen for **safe-by-default** operation:

* ``collapse_stereoisomers=False`` — see Stereoisomer Safety Warning.
* ``pubchem_enabled=False`` — see Network Side Effects.
* ``fuzzy_threshold=0.85`` — empirically chosen to accept common
  typos and case variations while rejecting unrelated drugs that
  happen to share a substring.
* ``fuzzy_max_candidates=10_000`` — bounds the worst-case
  :math:`O(n^2)` fuzzy sweep so a 1M-record dataset doesn't make the
  resolver pathological (audit D8-2).
* ``pubchem_call_delay=0.2`` (5 req/sec) — matches PubChem's
  published rate limit.  When ``pubchem_api_key`` is set, the delay
  drops to 0.1 (10 req/sec) per PubChem's published limits for
  authenticated callers.
* ``default_organism="Homo sapiens"`` — ⚠️ This default assumes
  human-centric research.  Non-human protein studies MUST override
  this via ``ResolverConfig(default_organism=...)`` or the
  ``ENTITY_RESOLUTION_DEFAULT_ORGANISM`` env var.

Confidence scores are **calibrated heuristics, not probabilities**.
A confidence of 0.85 does NOT mean "85 % likely to be the same
entity" — it means "this is the score we assign to a fuzzy match
that scored at least 0.85 on the rapidfuzz token-sort ratio".  The
rationale table:

==============  ===========  =========================================
Method          Confidence  Rationale
==============  ===========  =========================================
inchikey_exact       1.00    Structural identity, no ambiguity.
inchikey_connectivity 0.90    Same skeleton, possibly different
                              stereochemistry (see safety warning).
name_normalized      0.80    Same normalized name across sources.
                              Risk: homonyms (e.g. "aspirin" the brand
                              vs "aspirin" the metabolite).
fuzzy                0.85    Token-sort ratio ≥ threshold.  Bounded
                              false-positive risk because threshold is
                              high; reported confidence was raised from
                              0.6 → 0.85 to fix D3-3.
pubchem_xref         0.70    Third-party network lookup.  Subject to
                              salt-form ambiguity (D3-7) and name-
                              collision ambiguity (D3-7).
uniprot_exact        1.00    UniProt accession is the canonical
                              protein identifier.
gene_name_organism   0.85    Gene symbols are stable within an
                              organism but cross-organism homologs
                              exist (e.g. TP53 in human vs Trp53 in
                              mouse).
protein_name_fuzzy   0.60    Protein names are highly variable; fuzzy
                              matches here are lower confidence than
                              drug-name fuzzy matches.
==============  ===========  =========================================

Logging
-------
The package logger is ``entity_resolution``.  A :class:`logging.NullHandler`
is attached so that importing the package does not produce "No handlers
could be found" warnings in applications that don't configure logging
(audit D11-1).  Use :func:`set_log_level` to control verbosity:

>>> from entity_resolution import set_log_level
>>> set_log_level("DEBUG")  # doctest: +SKIP

For structured JSON logging (audit D11-3), use :func:`set_log_format`:

>>> from entity_resolution import set_log_format
>>> set_log_format("json")  # doctest: +SKIP

Scaling Notes
-------------
* **Cold-start import time**: ``< 5 ms`` because pandas, requests,
  and rapidfuzz are lazily imported (audit D8-1).  Importing the
  package does not trigger any side effects.
* **Fuzzy sweep ceiling**: ``ResolverConfig.fuzzy_max_candidates``
  bounds the worst-case :math:`O(n^2)` fuzzy sweep.  For datasets
  larger than the ceiling, some fuzzy matches may be missed —
  increase the ceiling (and accept slower runtime) if false negatives
  are a concern.
* **PubChem rate limit**: :meth:`resolve_single
  <DrugResolver.resolve_single>` with ``pubchem_enabled=True`` is
  rate-limited **process-globally** via
  :class:`~entity_resolution.base._ProcessGlobalRateLimiter` (audit
  D6-6) so multiple resolver instances in the same Airflow worker
  share one rate budget.  For batch PubChem lookups, use
  :meth:`resolve_batch_pubchem` (when implemented) which uses the
  PubChem batch API (100 names per call).
* **Memory**: :meth:`to_dataframe(chunksize=...)` returns an iterator
  of chunked DataFrames so a 10M-record mapping doesn't need to fit
  in memory all at once (audit D8-4).  :meth:`to_parquet` writes
  directly to disk (requires ``pyarrow``).

Raises
------
ValueError
    By :meth:`ResolverConfig.validate
    <entity_resolution.base.ResolverConfig.validate>` when an
    impossible combination of config values is detected.
ValueError
    By :meth:`DrugResolver.add_source_records
    <entity_resolution.drug_resolver.DrugResolver.add_source_records>`
    and :meth:`ProteinResolver.add_source_records
    <entity_resolution.protein_resolver.ProteinResolver.add_source_records>`
    when ``source`` is not in the configured ``source_whitelist``.
ImportError
    By :meth:`DrugResolver.to_dataframe
    <entity_resolution.drug_resolver.DrugResolver.to_dataframe>` /
    :meth:`ProteinResolver.to_dataframe
    <entity_resolution.protein_resolver.ProteinResolver.to_dataframe>`
    when ``pandas`` is not installed; by
    :meth:`DrugResolver._match_by_pubchem_xref
    <entity_resolution.drug_resolver.DrugResolver._match_by_pubchem_xref>`
    when ``pubchem_enabled=True`` but ``requests`` is not installed.

Version
-------
.. versionadded:: 1.0.0

   This is the first audit-driven release of the ``entity_resolution``
   package.  Every public symbol carries the ``.. versionadded:: 1.0.0``
   directive.  See the project CHANGELOG for the full list of fixes.

See Also
--------
:mod:`entity_resolution.base` — :class:`Resolver` ABC,
:class:`ResolverConfig`, :class:`ResolverStats`,
:class:`MatchConfidence` enum, :class:`_ProcessGlobalRateLimiter`.
:mod:`entity_resolution.drug_resolver` — :class:`DrugResolver` and
:func:`is_synthetic_inchikey`.
:mod:`entity_resolution.protein_resolver` — :class:`ProteinResolver`.
:mod:`entity_resolution.resolver_utils` — :func:`normalize_name`,
:func:`fuzzy_match_score`, :func:`extract_inchikey_first_block`,
:func:`is_valid_inchikey`, :func:`validate_drug_record`,
:func:`validate_protein_record`, :func:`build_canonical_name_index`,
:func:`build_canonical_inchikey_index`, :func:`compute_match_confidence`,
:func:`register_match_method`, :data:`METHOD_CONFIDENCE`,
:class:`MatchConfidence`.
:mod:`cleaning` — pre-resolution cleaning / normalization.
:mod:`database.loaders` — post-resolution bulk loaders.
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

# ---------------------------------------------------------------------------
# Package logger — attach NullHandler BEFORE any submodule import so that
# "No handlers could be found" warnings are impossible (audit D11-1).
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: Package version (PEP 440).  Audit D4-5 / D14-3.
__version__: str = "1.0.0"

#: Schema version of the state-dict / JSON-serialisation format.
MAPPING_SCHEMA_VERSION: str = "1.0"

# ---------------------------------------------------------------------------
# PEP 562 lazy-loading map.
#
# Keys are public names; values are ``(module_name, attribute_name)``
# tuples.  When a user does ``from entity_resolution import X``, Python
# falls back to ``__getattr__("X")`` if ``X`` is not a regular module
# attribute, and we resolve it via ``importlib`` here.  This defers the
# import of pandas / requests / rapidfuzz until a symbol is actually
# accessed (audit D1-2, D6-1, D8-1).
# ---------------------------------------------------------------------------
_SYMBOL_MAP: Dict[str, Tuple[str, str]] = {
    # Classes (D1-3 — Resolver ABC lives in base)
    "Resolver": ("entity_resolution.base", "Resolver"),
    "ResolverConfig": ("entity_resolution.base", "ResolverConfig"),
    "ResolverStats": ("entity_resolution.base", "ResolverStats"),
    "MatchConfidence": ("entity_resolution.base", "MatchConfidence"),
    "DrugResolver": ("entity_resolution.drug_resolver", "DrugResolver"),
    "ProteinResolver": ("entity_resolution.protein_resolver", "ProteinResolver"),

    # New typed result / lineage classes (audit C.10 / C.18 / E.15-17)
    "ResolveResult": ("entity_resolution.drug_resolver", "ResolveResult"),
    "LineageEvent": ("entity_resolution.drug_resolver", "LineageEvent"),
    "SourceDatasetMeta": ("entity_resolution.drug_resolver", "SourceDatasetMeta"),
    "SourceContribution": ("entity_resolution.drug_resolver", "SourceContribution"),
    "StereoisomerCollapse": ("entity_resolution.drug_resolver", "StereoisomerCollapse"),
    "FieldProvenance": ("entity_resolution.drug_resolver", "FieldProvenance"),
    "ErrorCode": ("entity_resolution.drug_resolver", "ErrorCode"),

    # Error hierarchy (audit E.19)
    "ResolverError": ("entity_resolution.drug_resolver", "ResolverError"),
    "ResolverStateCorruptionError": ("entity_resolution.drug_resolver", "ResolverStateCorruptionError"),
    "BatchSizeExceededError": ("entity_resolution.drug_resolver", "BatchSizeExceededError"),
    "BatchTimeoutError": ("entity_resolution.drug_resolver", "BatchTimeoutError"),
    "SchemaVersionMismatchError": ("entity_resolution.drug_resolver", "SchemaVersionMismatchError"),
    "ReferentialIntegrityError": ("entity_resolution.drug_resolver", "ReferentialIntegrityError"),
    "IndexMappingDesyncError": ("entity_resolution.drug_resolver", "IndexMappingDesyncError"),
    "PubChemCircuitOpenError": ("entity_resolution.drug_resolver", "PubChemCircuitOpenError"),
    "DeadLetterQueueFullError": ("entity_resolution.drug_resolver", "DeadLetterQueueFullError"),
    "ResolverOutputSchemaError": ("entity_resolution.drug_resolver", "ResolverOutputSchemaError"),

    # Functions — resolver_utils
    "normalize_name": ("entity_resolution.resolver_utils", "normalize_name"),
    "fuzzy_match_score": ("entity_resolution.resolver_utils", "fuzzy_match_score"),
    "extract_inchikey_first_block": (
        "entity_resolution.resolver_utils", "extract_inchikey_first_block"),
    "build_name_index": ("entity_resolution.resolver_utils", "build_name_index"),
    "build_inchikey_index": (
        "entity_resolution.resolver_utils", "build_inchikey_index"),
    "build_canonical_name_index": (
        "entity_resolution.resolver_utils", "build_canonical_name_index"),
    "build_canonical_inchikey_index": (
        "entity_resolution.resolver_utils", "build_canonical_inchikey_index"),
    "compute_match_confidence": (
        "entity_resolution.resolver_utils", "compute_match_confidence"),
    "register_match_method": (
        "entity_resolution.resolver_utils", "register_match_method"),
    "validate_drug_record": (
        "entity_resolution.resolver_utils", "validate_drug_record"),
    "validate_protein_record": (
        "entity_resolution.resolver_utils", "validate_protein_record"),
    "find_duplicate_ids": (
        "entity_resolution.resolver_utils", "find_duplicate_ids"),

    # Functions — base
    "is_valid_inchikey": ("entity_resolution.base", "is_valid_inchikey"),
    "is_synthetic_inchikey": ("entity_resolution.base", "is_synthetic_inchikey"),
    "make_synthetic_inchikey": (
        "entity_resolution.base", "make_synthetic_inchikey"),

    # Functions — drug_resolver
    "build_mapping": (
        "entity_resolution.drug_resolver", "build_mapping"),

    # Constants
    "METHOD_CONFIDENCE": (
        "entity_resolution.resolver_utils", "METHOD_CONFIDENCE"),
    "MAPPING_SCHEMA_VERSION": ("entity_resolution.base", "MAPPING_SCHEMA_VERSION"),
    "INCHIKEY_PATTERN": ("entity_resolution.base", "INCHIKEY_PATTERN"),
    "SYNTHETIC_INCHIKEY_PREFIX": (
        "entity_resolution.base", "SYNTHETIC_INCHIKEY_PREFIX"),
}

# Submodules that should be accessible via ``entity_resolution.X``
# (audit D1-4 / D15-1).  Listed explicitly so ``__dir__`` reports them.
_SUBMODULES: Tuple[str, ...] = (
    "base",
    "drug_resolver",
    "protein_resolver",
    "resolver_utils",
)


def __getattr__(name: str) -> Any:
    """PEP 562 lazy attribute loader.

    Resolves public names listed in :data:`_SYMBOL_MAP` and submodule
    names listed in :data:`_SUBMODULES` on first access, then caches
    them on this module so subsequent accesses are O(1).

    Parameters
    ----------
    name:
        Attribute name being looked up.

    Raises
    ------
    AttributeError
        If ``name`` is not a known public symbol or submodule.
    """
    # Submodule access: ``entity_resolution.base`` etc.
    if name in _SUBMODULES:
        full = f"entity_resolution.{name}"
        mod = importlib.import_module(full)
        # Cache for subsequent direct access.
        globals()[name] = mod
        return mod

    # Symbol access via _SYMBOL_MAP.
    entry = _SYMBOL_MAP.get(name)
    if entry is None:
        raise AttributeError(
            f"module 'entity_resolution' has no attribute {name!r}"
        )
    module_name, attr_name = entry
    module = importlib.import_module(module_name)
    value = getattr(module, attr_name)
    # Cache for subsequent direct access.
    globals()[name] = value
    return value


def __dir__() -> List[str]:
    """PEP 562 directory listing.

    Returns the explicit list of public names so that
    ``dir(entity_resolution)`` and ``from entity_resolution import *``
    both produce documented names (audit D10-2, D10-3).

    Includes the module-level dunder attributes (``__version__``,
    ``__all__``, etc.) so that ``dir()`` reports them just like a
    regular module would.
    """
    # Start with the regular module globals (dunders, functions
    # defined in this file).
    base = list(globals().keys())
    # Add the lazy-loaded symbols and submodules.
    extra = list(_SYMBOL_MAP.keys()) + list(_SUBMODULES)
    return sorted(set(base + extra))


# ---------------------------------------------------------------------------
# Factory functions (D2-5)
# ---------------------------------------------------------------------------

def make_drug_resolver(
    config: Optional[Any] = None,
    **config_overrides: Any,
) -> Any:
    """Construct a :class:`DrugResolver` with the given configuration.

    Parameters
    ----------
    config:
        Optional pre-built :class:`ResolverConfig` instance.  If
        ``None``, a config is built from env vars + ``config_overrides``.
    **config_overrides:
        Keyword overrides passed to :meth:`ResolverConfig.from_env`.

    Returns
    -------
    DrugResolver
        A fully-wired resolver instance.
    """
    from .base import ResolverConfig
    from .drug_resolver import DrugResolver

    if config is None:
        config = ResolverConfig.from_env(**config_overrides)
    return DrugResolver(config=config)


def make_protein_resolver(
    config: Optional[Any] = None,
    **config_overrides: Any,
) -> Any:
    """Construct a :class:`ProteinResolver` with the given configuration.

    Parameters
    ----------
    config:
        Optional pre-built :class:`ResolverConfig` instance.
    **config_overrides:
        Keyword overrides passed to :meth:`ResolverConfig.from_env`.

    Returns
    -------
    ProteinResolver
    """
    from .base import ResolverConfig
    from .protein_resolver import ProteinResolver

    if config is None:
        config = ResolverConfig.from_env(**config_overrides)
    return ProteinResolver(config=config)


# ---------------------------------------------------------------------------
# Dependency-check helpers (D6-4)
# ---------------------------------------------------------------------------

def check_dependencies() -> Dict[str, bool]:
    """Check whether optional dependencies are importable.

    Returns
    -------
    dict[str, bool]
        Mapping of dependency name → importable.  Keys: ``pandas``,
        ``requests``, ``rapidfuzz``, ``pyarrow``.
    """
    deps: Dict[str, bool] = {}
    for dep in ("pandas", "requests", "rapidfuzz", "pyarrow"):
        try:
            __import__(dep)
            deps[dep] = True
        except ImportError:
            deps[dep] = False
    return deps


def is_available() -> bool:
    """Return ``True`` iff the resolver's **core** dependencies are present.

    Core dependencies are ``pandas`` and ``rapidfuzz`` — without these
    the resolver cannot construct its indices or normalize names.
    ``requests`` is only required for PubChem lookups (opt-in).
    """
    deps = check_dependencies()
    return deps.get("pandas", False) and deps.get("rapidfuzz", False)


# ---------------------------------------------------------------------------
# Logging helpers (D11-5, D11-3, D11-4)
# ---------------------------------------------------------------------------

_VALID_LEVELS: Tuple[str, ...] = (
    "CRITICAL", "FATAL", "ERROR", "WARNING", "WARN", "INFO", "DEBUG", "NOTSET",
)


def set_log_level(level: Union[str, int]) -> None:
    """Set the logging level for the ``entity_resolution`` logger.

    Parameters
    ----------
    level:
        Level name (``"DEBUG"``, ``"INFO"``, etc.) or numeric level.

    Raises
    ------
    ValueError
        If ``level`` is not a recognised logging level.
    """
    if isinstance(level, str):
        level_upper = level.upper()
        if level_upper not in _VALID_LEVELS:
            raise ValueError(
                f"unknown log level {level!r}; expected one of {_VALID_LEVELS}"
            )
        numeric = getattr(logging, level_upper if level_upper != "WARN"
                          else "WARNING")
    else:
        numeric = int(level)
    logger.setLevel(numeric)
    # Also propagate to sub-module loggers so a single call controls
    # the whole package.
    for sub in _SUBMODULES:
        sub_logger = logging.getLogger(f"entity_resolution.{sub}")
        sub_logger.setLevel(numeric)


def set_log_format(fmt: str = "text") -> None:
    """Set the log format for the ``entity_resolution`` logger.

    Parameters
    ----------
    fmt:
        ``"text"`` (default) or ``"json"``.  When ``"json"``, attaches
        a :class:`logging.Formatter` that emits each record as a JSON
        object with ``timestamp``, ``level``, ``logger``, ``message``
        fields (audit D11-3).

    Raises
    ------
    ValueError
        If ``fmt`` is not ``"text"`` or ``"json"``.
    """
    if fmt not in ("text", "json"):
        raise ValueError(f"fmt must be 'text' or 'json', got {fmt!r}")

    # Remove existing handlers (except the NullHandler).
    for h in list(logger.handlers):
        if not isinstance(h, logging.NullHandler):
            logger.removeHandler(h)

    if fmt == "json":
        import json as _json

        class _JsonFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                payload = {
                    "timestamp": self.formatTime(record),
                    "level": record.levelname,
                    "logger": record.name,
                    "message": record.getMessage(),
                }
                if record.exc_info:
                    payload["exc_info"] = self.formatException(record.exc_info)
                return _json.dumps(payload)

        handler = logging.StreamHandler()
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s"
            )
        )
        logger.addHandler(handler)


# ---------------------------------------------------------------------------
# Public API surface (D1-4, D4-4)
# ---------------------------------------------------------------------------

#: Comprehensive ``__all__`` listing every public symbol AND every
#: submodule.  Audit D1-4 / D15-1 require submodules to be listed so
#: ``from entity_resolution import *`` produces documented names.
__all__: List[str] = sorted(set(list(_SYMBOL_MAP.keys()) + list(_SUBMODULES) + [
    "make_drug_resolver",
    "make_protein_resolver",
    "check_dependencies",
    "is_available",
    "set_log_level",
    "set_log_format",
    "__version__",
    "MAPPING_SCHEMA_VERSION",
]))
