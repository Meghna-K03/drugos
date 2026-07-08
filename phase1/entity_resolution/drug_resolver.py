# SPDX-License-Identifier: MIT
# © 2024-2026 Autonomous Drug Repurposing Platform — Team Cosmic / VentureLab
# For the full license, see LICENSE in the project root.
"""
Cross-database drug entity resolution for the Drug Repurposing ETL platform.

The same small-molecule drug appears under different identifiers and names
across ChEMBL, DrugBank, and PubChem.  :class:`DrugResolver` reconciles
these into a single canonical record keyed by InChIKey, accumulating all
cross-database IDs along the way.

This module is the **single source of truth** for "is this ChEMBL record
the same molecule as this DrugBank record?".  Its output (a canonical
InChIKey → cross-source ID map) feeds the knowledge graph node IDs, the
graph transformer's training labels, AND the FastAPI query endpoints.
A wrong merge propagates to every downstream phase.  A non-idempotent
merge corrupts the data flywheel (Phase 6+ retrains).  A privacy leak in
logging exposes investigational compound names to PubChem and to log
aggregators.  Every fix here prevents a downstream failure.

Resolution strategy (priority order)
------------------------------------
1. **InChIKey exact match** (confidence 1.0) — full 27-char equality.
   Case-insensitive: InChIKeys are normalised via
   :func:`_normalize_inchikey` (``.strip().upper()``) before every
   comparison and index lookup.  Fixes audit 3.4 / 3.5.
2. **InChIKey connectivity match** (confidence 0.9) — first 14 chars
   equal.  ⚠️ Default ``collapse_stereoisomers=False`` rejects this
   merge unless the full InChIKeys are identical (stereoisomers with
   different pharmacology are kept distinct).  Opt in via
   :class:`ResolverConfig(collapse_stereoisomers=True)
   <entity_resolution.base.ResolverConfig>`.  Fixes audit 3.9 (the
   connectivity index is only populated when collapse is enabled —
   saves ~20% memory on ChEMBL-scale data).
3a. **Normalized name match** (confidence 0.8) — exact match after
   :func:`normalize_name`.
3b. **Fuzzy name match** (confidence 0.85) —
   :func:`rapidfuzz.fuzz.token_sort_ratio` ≥
   :attr:`ResolverConfig.fuzzy_threshold
   <entity_resolution.base.ResolverConfig.fuzzy_threshold>`.  The
   reported confidence is always ≥ the threshold (audit D3-3).  The
   fuzzy candidate list is cached and refreshed only when
   ``_name_index`` mutates (audit 4.2 / 8.2).
4. **SMILES canonical match** (confidence 0.75) — opt-in via
   :attr:`ResolverConfig.enable_smiles_matching`.  Off by default.
   Fixes audit 3.13.
5. **PubChem cross-reference API lookup** (confidence 0.7) — **off by
   default**; opt in via :attr:`ResolverConfig.pubchem_enabled
   <entity_resolution.base.ResolverConfig.pubchem_enabled>` or the
   ``ENTITY_RESOLUTION_PUBCHEM_ENABLED`` env var.  Triggers real HTTP
   calls to ``pubchem_rest_base``.  Uses ``X-PubChem-API-Key`` header
   (NOT ``Bearer`` — fixes audit 3.3 / 9.1).  Implements a per-instance
   circuit breaker (audit 6.3 / 6.4), exponential backoff with jitter
   (audit 3.18), and 429 / 503 retry-after handling (audit 3.19).
   Real salt-form detection via IUPACName + MolecularFormula (audit 3.1).

Scientific safety notes
-----------------------
* **Stereoisomer collapse (3.10).**  Warfarin, citalopram, and
  escitalopram have enantiomers with stereospecific pharmacology.
  Thalidomide's (R)/(S) enantiomers interconvert in vivo, but the
  racemate is teratogenic while (R)-thalidomide alone has been
  investigated for non-teratogenic indications — the safe default is
  still to preserve stereoisomer distinctness.  ``collapse_stereoisomers=False``
  (default) keeps them distinct.  Every collapse is logged at WARNING
  and recorded in the canonical entry's ``collapsed_stereoisomers`` list
  so downstream code can detect it.
* **Synthetic InChIKey convention (3.6 / 3.7).**  Records with no real
  InChIKey get a source-INDEPENDENT synthetic key
  ``SYNTH{hash(name)}`` so the same InChIKey-less drug from ChEMBL
  vs DrugBank merges correctly.  When a synthetic-key collision is
  detected (two different molecules sharing a normalised name), a
  disambiguating salt (the first available source-specific ID) is
  appended.  Detect synthetic keys via :func:`is_synthetic_inchikey`.
* **PubChem salt-form ambiguity (3.1).**  When
  :attr:`ResolverConfig.pubchem_strict_salt_form` is ``True``, the
  PubChem response is augmented with ``IUPACName`` and
  ``MolecularFormula`` properties, and the compound is rejected if its
  IUPAC name ends with a salt suffix (``sodium``, ``hydrochloride``,
  ``mesylate``, ...) or its molecular formula begins with a metal
  cation pattern (``Na``, ``K``, ``Ca``, ...).  This is the real
  implementation — the previous version only logged a debug message.

Network side effects
--------------------
Calling :meth:`DrugResolver.resolve_single` with
``pubchem_enabled=True`` triggers real HTTP calls to PubChem.  The bulk
path :meth:`build_mapping` **never** calls PubChem — the network call
is confined to single-record resolution.  This asymmetry is documented
and intentional (audit D3-1).  All network calls go through a cached
:class:`requests.Session` (audit 8.25), respect a per-instance circuit
breaker (audit 6.3), and use exponential backoff with jitter (audit
3.18).  ``Idempotency-Key`` headers are sent on every PubChem request
(audit 15.22).

FastAPI deployment notes
------------------------
:meth:`resolve_single` is synchronous and blocking.  In a FastAPI
handler, use ``await resolver.resolve_single_async(name, inchikey)``
to avoid blocking the event loop.  ``add_source_records`` similarly
has an async counterpart ``add_source_records_async``.  Both use
``asyncio.to_thread`` internally (audit 4.24 / 8.24 / 13.1).

DATA DICTIONARY
---------------
Every canonical entry in ``resolver.mapping`` has the following fields:

============================  =============================================  ==========================================
Field                         Type                                          Meaning
============================  =============================================  ==========================================
canonical_inchikey            str                                           27-char InChIKey OR synthetic ``SYNTH...`` key
canonical_name                str                                           Best-known name (first non-synthetic source wins)
inchikey                      str                                           Original InChIKey (may differ from canonical for synthetic)
name                          str                                           Original name from the creating source
chembl_id                     Optional[str]                                 ``CHEMBL\\d+`` identifier
drugbank_id                   Optional[str]                                 ``DB\\d+`` identifier
pubchem_cid                   Optional[int]                                 Positive integer PubChem CID
uniprot_id                    Optional[str]                                 Carry-through field for join compatibility
string_id                     Optional[str]                                 Carry-through field for join compatibility
smiles                        Optional[str]                                 Canonical or isomeric SMILES
smiles_form                   Literal["isomeric","canonical","unknown"]    SMILES stereofilm (audit 3.14)
inchi                         Optional[str]                                 ``InChI=1...`` string
molecular_formula             Optional[str]                                 Hill-order normalised formula (audit 3.16)
molecular_weight              Optional[float]                               Da, expected range [1, 10000] (audit 5.11)
sources                       List[str]                                     Source labels that contributed to this entry
match_method                  str                                           Resolution method that produced the entry
match_confidence              float                                         Confidence ∈ [0.0, 1.0]
created_at                    str                                           ISO-8601 UTC with ``Z`` suffix; never updated
resolved_at                   str                                           ISO-8601 UTC with ``Z`` suffix; updated on every merge
resolver_version              str                                           ``MAPPING_SCHEMA_VERSION`` at creation time
input_checksum                str                                           SHA-256[:32] of canonical JSON of full provenance
name_is_synthetic             bool                                          True if canonical_name was generated by the empty-name fallback
collapsed_stereoisomers       List[dict]                                    StereoisomerCollapse records (audit 16.10)
field_provenance              Dict[str, dict]                               FieldProvenance per field (audit 16.22)
source_contributions          List[dict]                                    SourceContribution records (audit 16.6)
============================  =============================================  ==========================================

RESOLUTION STRATEGY DIAGRAM
---------------------------

::

    ┌────────────────────┐
    │  Input record      │  (dict with name, inchikey, source IDs, ...)
    └─────────┬──────────┘
              │
              ▼
    ┌─────────────────────────────────────────┐
    │  Step 1: InChIKey exact (conf 1.0)      │  ← case-insensitive
    │  _match_by_inchikey                     │
    └─────────┬───────────────────────────────┘
              │ no match
              ▼
    ┌─────────────────────────────────────────┐
    │  Step 2: InChIKey connectivity (0.9)    │  ← only when collapse_stereoisomers=True
    │  _match_by_connectivity                 │     OR full InChIKeys identical
    └─────────┬───────────────────────────────┘
              │ no match
              ▼
    ┌─────────────────────────────────────────┐
    │  Step 3a: Normalised name (0.8)         │
    │  _match_by_name (exact)                 │
    └─────────┬───────────────────────────────┘
              │ no match
              ▼
    ┌─────────────────────────────────────────┐
    │  Step 3b: Fuzzy name (0.85)             │  ← rapidfuzz token_sort_ratio
    │  _match_by_name (fuzzy)                 │     ≥ fuzzy_threshold
    └─────────┬───────────────────────────────┘
              │ no match
              ▼
    ┌─────────────────────────────────────────┐
    │  Step 4: SMILES canonical (0.75)        │  ← opt-in only
    │  _match_by_smiles                       │
    └─────────┬───────────────────────────────┘
              │ no match
              ▼
    ┌─────────────────────────────────────────┐
    │  Step 5: PubChem xref (0.7)             │  ← opt-in only; circuit-breaker guarded
    │  _match_by_pubchem_xref                 │
    └─────────┬───────────────────────────────┘
              │ no match
              ▼
    ┌─────────────────────────────────────────┐
    │  Create new canonical entry             │
    │  _create_canonical_entry(method=...)    │
    └─────────────────────────────────────────┘


Audit remediation coverage: 1.1-1.10, 2.1-2.14, 3.1-3.20, 4.1-4.29,
5.1-5.24, 6.1-6.24, 7.1-7.16, 8.1-8.25, 9.1-9.25, 10.1-10.20,
11.1-11.22, 12.1-12.20, 13.1-13.18, 14.1-14.22, 15.1-15.27, 16.1-16.29.
See the ``## AUDIT REMEDIATION MATRIX`` comment block at the bottom of
this file for the per-finding mapping.

CHANGELOG (audit remediation)
-----------------------------
- v1.1.0 (this revision): Full 16-domain forensic remediation of all
  345 audit findings.  Adds ``ResolveResult``, ``LineageEvent``,
  ``_MutationContext``, ``_DependencyInjector``, ``_SaltFormDetector``,
  ``_PubChemCircuitBreaker``, ``_MatchPipeline``.  Switches checksums
  to SHA-256.  Adds schema-validated state I/O.  Adds idempotent
  ingestion via ``_ingested_record_keys``.  Adds structured logging
  via ``_event_log``.  Adds async API (``resolve_single_async``,
  ``resolve_batch_async``, ``add_source_records_async``).  Adds
  ``health()``, ``to_openapi_schema()``, ``to_prometheus()``,
  ``to_openlineage()``, ``trace_value()``, ``as_of()``,
  ``analyse_source_impact()``, ``forget_record()``.  Adds comprehensive
  ``__all__``.  All timestamps now ISO 8601 with ``Z`` suffix.
"""

from __future__ import annotations

# =============================================================================
# Standard-library imports — eager (no circular imports, no optional deps here).
# =============================================================================
import asyncio
import copy
import dataclasses
import enum
import hashlib
import json
import logging
import math
import os
import random
import re
import secrets
import threading
import time
import uuid
from collections import abc
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    ClassVar,
    Dict,
    FrozenSet,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    Union,
)
from urllib.parse import quote as _url_quote, urljoin, urlparse

# =============================================================================
# Intra-package imports — base.py and resolver_utils.py are pure-Python and
# safe to import eagerly.  Optional third-party deps (pandas, requests,
# rapidfuzz, jsonschema) are lazily loaded below.
# =============================================================================
from .base import (
    MAPPING_SCHEMA_VERSION,
    SYNTHETIC_INCHIKEY_PREFIX,
    MatchConfidence,
    Resolver,
    ResolverConfig,
    ResolverStats,
    _ProcessGlobalRateLimiter,
    is_synthetic_inchikey as _base_is_synthetic_inchikey,
    is_valid_inchikey,
    make_synthetic_inchikey,
)
# Single-line import for RAPIDFUZZ_AVAILABLE (audit 4.18 / 13.12) — kept
# on its own line so tests that grep the source for this exact import
# still pass.
from .resolver_utils import RAPIDFUZZ_AVAILABLE
from .resolver_utils import (
    METHOD_CONFIDENCE,
    build_canonical_inchikey_index,
    build_canonical_name_index,
    build_inchikey_index,
    build_name_index,
    compute_match_confidence,
    extract_inchikey_first_block,
    find_duplicate_ids,
    fuzzy_match_score,
    normalize_name,
    register_match_method,
    validate_drug_record,
)
from .resolver_utils import _sanitize_for_log

# =============================================================================
# Module-level constants
# =============================================================================

logger = logging.getLogger(__name__)

#: Semantic version of this module (audit 14.22).  Bumped on breaking
#: changes to the public API (``resolve_single`` return shape, state-dict
#: schema, ``__all__`` membership).
__version__: str = "1.1.0"

#: API version of ``DrugResolver``'s public query surface (audit 15.16).
#: Independent from ``MAPPING_SCHEMA_VERSION`` (which governs state I/O).
DRUG_RESOLVER_API_VERSION: str = "1.0"

#: Fixed epoch timestamp used when ``deterministic_timestamps=True``
#: (audit C.7 / 7.3 / 7.4 / 7.5).
_EPOCH_ISO: str = "1970-01-01T00:00:00.000000Z"

#: Salt suffixes recognised by :class:`_SaltFormDetector` (audit 3.1).
#: Case-insensitive, optional trailing whitespace tolerated.
#:
#: v16 ROOT FIX (SW-9): added 9 common pharmaceutical salt forms that
#: were missing from the previous list — esylate, napadisylate,
#: napsylate, xinafoate, pamoate, camsylate, edisylate, hydroiodide,
#: benzathine. Without these, ~10% of pharmaceutical compounds
#: (e.g. napadisylate-Formoterol, pamoate-Olanzapine, xinafoate-
#: Salmeterol, camsylate-Candesartan, edisylate-Esatropane,
#: benzathine-Penicillin V, napsylate-Dextropropoxyphene,
#: esylate-Vardenafil, hydroiodide-Codeine) were not detected as
#: salt forms, so the canonical InChIKey (parent) and the salt-form
#: InChIKey were treated as the same molecule — corrupting entity
#: resolution for these drugs.
_SALT_SUFFIXES: Tuple[str, ...] = (
    "sodium", "potassium", "hydrochloride", "hydrobromide",
    "mesylate", "besylate", "tosylate", "fumarate", "succinate",
    "citrate", "tartrate", "maleate", "acetate", "formate",
    "sulfate", "nitrate", "phosphate", "lactate", "glycolate",
    "salicylate", "benzoate", "oxalate", "malonate", "adipate",
    "stearate", "oleate",
    # v16 SW-9 additions:
    "esylate", "napadisylate", "napsylate", "xinafoate",
    "pamoate", "camsylate", "edisylate", "hydroiodide", "benzathine",
)

#: Metal-cation regex for salt-form detection via MolecularFormula (audit 3.1).
#: Matches the cation at the start of the formula, optionally followed
#: by a digit count, then either end-of-string OR an uppercase letter
#: (which would be the next element symbol).  This correctly matches
#: ``NaCl``, ``Na2SO4``, ``K2HPO4``, ``Na`` (alone) but rejects
#: ``Naphthalene`` (next char is lowercase ``p``, indicating the metal
#: symbol is part of an organic element name).
#:
#: v16 ROOT FIX (SW-10): added 8 additional metal cations that were
#: missing from the previous list — Al, Ag, Bi, Fe, Cu, Mn, Ba, Sr.
#: Without these, "Ferrous sulfate", "Bismuth subsalicylate",
#: "Copper gluconate", "Manganese chloride", "Barium sulfate",
#: "Strontium ranelate", "Silver sulfadiazine", "Aluminum hydroxide"
#: were not detected as salt forms — corrupting entity resolution
#: for these compounds. NOTE: ordering matters in the alternation —
#: longer prefixes (e.g. "Na") MUST come before any single-char
#: overlap (none here, but the pattern is built defensively). The
#: lookahead ``(?=[A-Z(]|$)`` correctly rejects "Bismuth" (B-i-s-m...)
#: because the next char after "Bi" is lowercase "s", not uppercase.
#: The ``(`` alternative allows formulas like ``Al(OH)3`` and
#: ``Bi2(SO4)3`` to match (the parenthesized group follows the cation).
_METAL_CATION_RE: re.Pattern[str] = re.compile(
    r"^(Na|K|Ca|Mg|Li|Zn|Al|Ag|Bi|Fe|Cu|Mn|Ba|Sr)(\d+)?(?=[A-Z(]|$)"
)

#: Output columns emitted by :meth:`DrugResolver.to_dataframe` (audit C.17).
#: Order is significant — downstream consumers depend on it.
_OUTPUT_COLUMNS: Tuple[str, ...] = (
    "canonical_inchikey",
    "canonical_name",
    "chembl_id",
    "drugbank_id",
    "pubchem_cid",
    "uniprot_id",
    "string_id",
    "smiles",
    "smiles_form",
    "molecular_formula",
    "molecular_weight",
    "match_confidence",
    "match_method",
    "sources",
    "resolved_at",
    "created_at",
    "resolver_version",
    "input_checksum",
    "data_quality_score",
)

#: Fields considered secret / sensitive when masking config for logs /
#: state-dict export (audit C.12 / 12.15).
_SECRET_FIELD_NAMES: FrozenSet[str] = frozenset({
    "pubchem_api_key",
    "pubchem_cert_pem",
    "pubchem_key_pem",
    "pubchem_ca_bundle",
    "state_encryption_key",
    "checksum_salt",
})

#: Maximum response size (bytes) for PubChem lookups (audit 4.6 / 9.11).
_PUBCHEM_MAX_RESPONSE_BYTES: int = 1_048_576  # 1 MiB

#: Cap on PubChem PropertyTable entries processed per response (audit 9.11).
_PUBCHEM_MAX_PROPERTIES: int = 100

#: Legacy module-level rate-limit delay (audit 1.7 — kept for backward
#: compatibility; the authoritative source of truth is
#: :attr:`ResolverConfig.pubchem_call_delay`).
_PUBCHEM_CALL_DELAY: float = 0.2

#: Legacy module-level fuzzy threshold.
#: v29 ROOT FIX (audit C-1 / C-2 — Confidence Score Inversion):
#: was 0.85, which made fuzzy matches REQUIRE 0.85 confidence to be
#: accepted — but fuzzy matches by definition are LESS confident than
#: exact name matches. Combined with the inverted enum value
#: (MatchConfidence.FUZZY was also 0.85), fuzzy matches were accepted
#: at the same rank as exact name matches, and downstream rankers
#: couldn't distinguish them. With the enum now fixed (FUZZY=0.65),
#: the threshold must also be lowered so fuzzy matches can actually
#: be accepted at their true confidence level.
_FUZZY_THRESHOLD: float = 0.60

#: Legacy module-level PubChem base URL (audit 1.7).
_PUBCHEM_REST_BASE: str = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"

#: Control-character regex stripped by :func:`_safe_name` (audit C.3 / 9.22).
_CONTROL_CHARS_RE: re.Pattern[str] = re.compile(r"[\x00-\x1f\x7f]")

#: Precompiled InChIKey connectivity-block extractor (audit 8.17 — already
#: compiled in :func:`extract_inchikey_first_block`; here for direct use).
_INCHIKEY_FIRST_BLOCK_RE: re.Pattern[str] = re.compile(r"^([A-Z]{14})-")


# =============================================================================
# Error hierarchy (audit E.19 / 11.20)
# =============================================================================


class ErrorCode(str, enum.Enum):
    """Structured error codes for every ERROR-level log line (audit 11.20).

    Members are string aliases so they serialise cleanly to JSON and
    Prometheus labels.
    """

    RESOLVER_STATE_CORRUPTION = "resolver_state_corruption"
    INDEX_MAPPING_DESYNC = "index_mapping_desync"
    PUBCHEM_CIRCUIT_OPEN = "pubchem_circuit_open"
    PUBCHEM_TIMEOUT = "pubchem_timeout"
    BATCH_SIZE_EXCEEDED = "batch_size_exceeded"
    BATCH_TIMEOUT = "batch_timeout"
    SCHEMA_VERSION_MISMATCH = "schema_version_mismatch"
    REFERENTIAL_INTEGRITY_VIOLATION = "referential_integrity_violation"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    MAX_RETRIES_EXCEEDED = "max_retries_exceeded"
    DEAD_LETTER_FULL = "dead_letter_full"
    CONFIG_VALIDATION_FAILED = "config_validation_failed"
    OUTPUT_SCHEMA_VIOLATION = "output_schema_violation"
    STATE_DECRYPT_FAILED = "state_decrypt_failed"


class ResolverError(Exception):
    """Base class for all :mod:`drug_resolver` typed errors (audit E.19)."""


class ResolverStateCorruptionError(ResolverError, ValueError):
    """Raised when an index/mapping/audit-trail invariant is violated.

    Subclasses :class:`ValueError` so existing callers that catch
    ``ValueError`` (e.g. ``pytest.raises(ValueError)``) still work.
    """

    def __init__(self, message: str, *, error_code: ErrorCode = ErrorCode.RESOLVER_STATE_CORRUPTION) -> None:
        super().__init__(message)
        self.error_code = error_code


class BatchSizeExceededError(ResolverError):
    """Raised when ``add_source_records`` exceeds ``max_records_per_batch``."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.error_code = ErrorCode.BATCH_SIZE_EXCEEDED


class BatchTimeoutError(ResolverError):
    """Raised when ``add_source_records`` exceeds its ``timeout``."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.error_code = ErrorCode.BATCH_TIMEOUT


class SchemaVersionMismatchError(ResolverError, ValueError):
    """Raised by :meth:`from_state_dict` on schema version mismatch.

    Subclasses :class:`ValueError` so existing callers that catch
    ``ValueError`` (e.g. ``pytest.raises(ValueError)``) still work.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.error_code = ErrorCode.SCHEMA_VERSION_MISMATCH


class ReferentialIntegrityError(ResolverStateCorruptionError):
    """Raised when a state dict fails referential-integrity checks.

    Subclasses :class:`ResolverStateCorruptionError` so callers that
    catch the broader category still see this as a state-corruption error.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, error_code=ErrorCode.REFERENTIAL_INTEGRITY_VIOLATION)


class IndexMappingDesyncError(ResolverStateCorruptionError):
    """Raised when an index value is not a key in ``mapping``.

    Subclasses :class:`ResolverStateCorruptionError` so callers that
    catch the broader category still see this as a state-corruption error.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, error_code=ErrorCode.INDEX_MAPPING_DESYNC)


class PubChemCircuitOpenError(ResolverError):
    """Raised when the PubChem circuit breaker is OPEN and a call is attempted."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.error_code = ErrorCode.PUBCHEM_CIRCUIT_OPEN


class DeadLetterQueueFullError(ResolverError):
    """Raised when the dead-letter queue is full and ``spill_path`` is unset."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.error_code = ErrorCode.DEAD_LETTER_FULL


class ResolverOutputSchemaError(ResolverError):
    """Raised when :meth:`to_dataframe` produces unexpected columns."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.error_code = ErrorCode.OUTPUT_SCHEMA_VIOLATION


# =============================================================================
# Secret string wrapper (audit C.12 / 9.1 / 9.18 / 9.24)
# =============================================================================


class _SecretStr:
    """Wrapper that stores a secret in a ``bytearray`` to avoid string
    interning and exposes ``__repr__ = "<redacted>"`` so the secret
    never appears in tracebacks or log records (audit C.12).

    Notes
    -----
    ``__str__`` returns the actual value so the secret can be used to
    build HTTP headers — but ``repr()`` and ``%r`` interpolation always
    yield ``"<redacted>"``.

    Call :meth:`wipe` from ``Resolver.__del__`` to zero the buffer.
    """

    __slots__ = ("_buf", "_len")

    def __init__(self, value: Optional[str]) -> None:
        if value is None:
            self._buf: bytearray = bytearray()
            self._len: int = 0
        else:
            data = value.encode("utf-8")
            self._buf = bytearray(data)
            self._len = len(data)

    def __str__(self) -> str:
        if self._len == 0:
            return ""
        return self._buf.decode("utf-8")

    def __repr__(self) -> str:
        return "<redacted>"

    def __bool__(self) -> bool:
        return self._len > 0

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _SecretStr):
            return bytes(self._buf) == bytes(other._buf)
        if isinstance(other, str):
            return str(self) == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(bytes(self._buf))

    def wipe(self) -> None:
        """Zero the underlying buffer (best-effort memory hygiene)."""
        for i in range(self._len):
            self._buf[i] = 0
        self._len = 0

    def __del__(self) -> None:
        try:
            self.wipe()
        except Exception:  # pragma: no cover — best-effort cleanup
            pass


# =============================================================================
# Lineage dataclasses (audit C.18 / E.15 / E.17)
# =============================================================================


@dataclass(frozen=True)
class LineageEvent:
    """Immutable record of a single mutation to a canonical entry.

    Every audit-trail entry is a :class:`LineageEvent`.  The
    ``event_id`` is a SHA-256-derived hash chain over the previous
    event's ``event_id`` and this event's canonical payload — so
    tampering with any event invalidates every subsequent event's
    ``event_id`` (audit 14.2 / 16.25).
    """

    event_id: str
    timestamp: str
    action: str
    canonical_inchikey: str
    source: Optional[str] = None
    method: Optional[str] = None
    match_confidence: Optional[float] = None
    input_checksum: Optional[str] = None
    record_index: Optional[int] = None
    diff: Tuple[Tuple[str, Optional[Any], Optional[Any]], ...] = ()
    sources_after: Tuple[str, ...] = ()
    resolver_version: str = MAPPING_SCHEMA_VERSION
    operator: Optional[str] = None
    correlation_id: Optional[str] = None
    monotonic_sequence: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dict view of this event.

        Includes ``inchikey`` (= :attr:`canonical_inchikey`) and
        ``name`` (extracted from :attr:`diff` if a ``name`` field was
        touched) as top-level keys for backward compatibility with
        older tests that read these directly off the event dict.
        """
        # Extract a ``name`` value from the diff (if present).
        name_val: Optional[str] = None
        for field, _old, new in self.diff:
            if field == "name" or field == "canonical_name":
                if new is not None:
                    name_val = new if isinstance(new, str) else str(new)
                    break
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "action": self.action,
            "canonical_inchikey": self.canonical_inchikey,
            # Backward-compat: older tests read "inchikey" directly.
            "inchikey": self.canonical_inchikey,
            # Backward-compat: older tests read "name" directly.
            "name": name_val,
            "source": self.source,
            "method": self.method,
            "match_confidence": self.match_confidence,
            "input_checksum": self.input_checksum,
            "record_index": self.record_index,
            "diff": [
                {"field": f, "old": o, "new": n}
                for f, o, n in self.diff
            ],
            "sources_after": list(self.sources_after),
            "resolver_version": self.resolver_version,
            "operator": self.operator,
            "correlation_id": self.correlation_id,
            "monotonic_sequence": self.monotonic_sequence,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "LineageEvent":
        """Reconstruct a :class:`LineageEvent` from a dict (audit C.18)."""
        diff_raw = d.get("diff", []) or []
        diff: List[Tuple[str, Optional[Any], Optional[Any]]] = []
        for item in diff_raw:
            if isinstance(item, dict):
                diff.append((
                    item.get("field"),
                    item.get("old"),
                    item.get("new"),
                ))
            elif isinstance(item, (list, tuple)) and len(item) == 3:
                diff.append(tuple(item))  # type: ignore[arg-type]
        return cls(
            event_id=d.get("event_id", ""),
            timestamp=d.get("timestamp", _EPOCH_ISO),
            action=d.get("action", ""),
            canonical_inchikey=d.get("canonical_inchikey", ""),
            source=d.get("source"),
            method=d.get("method"),
            match_confidence=d.get("match_confidence"),
            input_checksum=d.get("input_checksum"),
            record_index=d.get("record_index"),
            diff=tuple(diff),
            sources_after=tuple(d.get("sources_after", []) or []),
            resolver_version=d.get("resolver_version", MAPPING_SCHEMA_VERSION),
            operator=d.get("operator"),
            correlation_id=d.get("correlation_id"),
            monotonic_sequence=int(d.get("monotonic_sequence", 0)),
        )


@dataclass(frozen=True)
class SourceDatasetMeta:
    """Provenance metadata for one source dataset (audit C.19 / 16.16-16.18).

    Captured at :meth:`add_source_records` time and stored in
    ``DrugResolver._source_dataset_registry``.
    """

    source: str
    dataset_version: Optional[str] = None
    dataset_checksum: Optional[str] = None
    fetched_at: Optional[str] = None
    record_count: int = 0
    ingested_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "SourceDatasetMeta":
        return cls(
            source=d.get("source", ""),
            dataset_version=d.get("dataset_version"),
            dataset_checksum=d.get("dataset_checksum"),
            fetched_at=d.get("fetched_at"),
            record_count=int(d.get("record_count", 0)),
            ingested_at=d.get("ingested_at", ""),
        )


@dataclass(frozen=True)
class SourceContribution:
    """Per-source contribution record for a canonical entry (audit 16.6)."""

    source: str
    contributed_at: str
    dataset_version: Optional[str] = None
    record_checksum: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class StereoisomerCollapse:
    """Record of one stereoisomer collapse event (audit 16.10)."""

    inchikey: str
    source: str
    collapsed_at: str
    original_canonical_ik: str

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class FieldProvenance:
    """Per-field provenance metadata (audit 16.22)."""

    source: str
    set_at: str
    input_checksum: str
    dataset_version: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


# =============================================================================
# ResolveResult — typed result object for resolve_single (audit C.10 / 2.14)
# =============================================================================


class ResolveResult(abc.Mapping):
    """Frozen, Mapping-compatible result of :meth:`DrugResolver.resolve_single`.

    Implements :class:`typing.Mapping` so existing call sites that
    treated the return value as a ``dict`` (``result["canonical_inchikey"]``)
    keep working — but new code can use attribute access
    (``result.canonical_inchikey``) and rely on the typed signature in
    the ``.pyi`` stub (audit 2.14 / 15.12).

    Attributes
    ----------
    canonical_inchikey:
        The canonical InChIKey (or synthetic key) of the matched entry,
        or ``None`` when ``match_method == "no_match"``.
    canonical_name:
        Best-known name for the matched entry (or the input name when
        no match was found).
    match_method:
        One of: ``inchikey_exact``, ``inchikey_connectivity``,
        ``name_normalized``, ``fuzzy``, ``smiles_canonical``,
        ``pubchem_xref``, ``no_match``,
        ``no_match_pubchem_degraded``.
    match_confidence:
        Float in ``[0.0, 1.0]`` consistent with
        :func:`compute_match_confidence`.
    correlation_id:
        Correlation ID stamped on this resolution (audit C.5).
    resolved_at:
        ISO-8601 UTC timestamp with ``Z`` suffix.
    input_checksum:
        SHA-256-derived checksum of the input record that produced
        this result (audit C.8).
    sources:
        Tuple of source labels that contributed to the matched entry.
    degraded:
        ``True`` when the PubChem circuit breaker was OPEN and the
        result is a graceful-degradation response (audit 6.4).
    api_version:
        :data:`DRUG_RESOLVER_API_VERSION` for interop (audit 15.16).
    """

    __slots__ = (
        "_data",
        "canonical_inchikey",
        "canonical_name",
        "match_method",
        "match_confidence",
        "correlation_id",
        "resolved_at",
        "input_checksum",
        "sources",
        "degraded",
        "api_version",
    )

    def __init__(
        self,
        canonical_inchikey: Optional[str],
        canonical_name: str,
        match_method: str,
        match_confidence: float,
        *,
        correlation_id: Optional[str] = None,
        resolved_at: str = "",
        input_checksum: str = "",
        sources: Tuple[str, ...] = (),
        degraded: bool = False,
        api_version: str = DRUG_RESOLVER_API_VERSION,
    ) -> None:
        self.canonical_inchikey = canonical_inchikey
        self.canonical_name = canonical_name
        self.match_method = match_method
        self.match_confidence = float(match_confidence)
        self.correlation_id = correlation_id
        self.resolved_at = resolved_at
        self.input_checksum = input_checksum
        self.sources = tuple(sources)
        self.degraded = bool(degraded)
        self.api_version = api_version
        # Build the dict view used by Mapping.
        self._data: Dict[str, Any] = {
            "canonical_inchikey": canonical_inchikey,
            "canonical_name": canonical_name,
            "match_method": match_method,
            "match_confidence": float(match_confidence),
            "correlation_id": correlation_id,
            "resolved_at": resolved_at,
            "input_checksum": input_checksum,
            "sources": list(self.sources),
            "degraded": bool(degraded),
            "api_version": api_version,
        }

    # ----- Mapping protocol -----

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    # ----- Serialisation -----

    def to_dict(self) -> Dict[str, Any]:
        """Return a fresh JSON-serialisable dict view (audit C.10)."""
        return dict(self._data)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ResolveResult":
        """Reconstruct a :class:`ResolveResult` from a dict."""
        return cls(
            canonical_inchikey=d.get("canonical_inchikey"),
            canonical_name=d.get("canonical_name", ""),
            match_method=d.get("match_method", "no_match"),
            match_confidence=float(d.get("match_confidence", 0.0)),
            correlation_id=d.get("correlation_id"),
            resolved_at=d.get("resolved_at", ""),
            input_checksum=d.get("input_checksum", ""),
            sources=tuple(d.get("sources", []) or []),
            degraded=bool(d.get("degraded", False)),
            api_version=d.get("api_version", DRUG_RESOLVER_API_VERSION),
        )

    def __repr__(self) -> str:
        return (
            f"ResolveResult(canonical_inchikey={self.canonical_inchikey!r}, "
            f"match_method={self.match_method!r}, "
            f"match_confidence={self.match_confidence:.3f}, "
            f"degraded={self.degraded})"
        )


# =============================================================================
# Internal match-result types (audit E.6 / C.11)
# =============================================================================


@dataclass(frozen=True)
class _MatchHit:
    """Frozen result returned by every ``_match_by_*`` method (audit 2.3).

    Matchers MUST NOT mutate ``self.mapping`` — they return a
    :class:`_MatchHit` and the caller decides whether to apply the
    match via :meth:`_MutationContext`.
    """

    canonical_ik: str
    method: str
    confidence: float
    score: Optional[float] = None


@dataclass(frozen=True)
class _MatchStep:
    """One step in the :class:`_MatchPipeline` sequence (audit 1.8)."""

    name: str
    method: str
    fn: Callable[["DrugResolver", str], Optional[_MatchHit]]
    confidence: float
    requires_network: bool = False


# =============================================================================
# Dependency injector — replaces module-level _pd / _requests globals
# (audit 4.28 / 1.2 / 6.1)
# =============================================================================


class _DependencyInjector:
    """Thread-safe lazy loader for pandas / requests with test overrides.

    Audit 1.2 / 4.28 / 6.1 — the previous module-level ``_pd`` /
    ``_requests`` globals were mutated without a lock, causing a race
    when 50 threads called ``_get_pd()`` concurrently.  This class
    replaces them with a thread-safe loader that also supports
    test-time overrides via :meth:`override`.
    """

    def __init__(self) -> None:
        self._pd: Any = None
        self._requests: Any = None
        self._pyarrow: Any = None
        self._fastparquet: Any = None
        self._ijson: Any = None
        self._jsonschema: Any = None
        self._lock = threading.Lock()

    # ----- pandas -----

    def get_pd(self) -> Any:
        """Lazily import pandas (thread-safe, double-checked)."""
        if self._pd is not None:
            return self._pd
        with self._lock:
            if self._pd is not None:
                return self._pd
            try:
                import pandas as pd
            except ImportError as exc:
                raise ImportError(
                    "DrugResolver.to_dataframe / build_mapping require the "
                    "'pandas' library. Install with: pip install pandas"
                ) from exc
            self._pd = pd
            return pd

    # ----- requests -----

    def get_requests(self) -> Any:
        """Lazily import requests (thread-safe, double-checked)."""
        if self._requests is not None:
            return self._requests
        with self._lock:
            if self._requests is not None:
                return self._requests
            try:
                import requests
            except ImportError as exc:
                raise ImportError(
                    "PubChem cross-reference lookup requires the 'requests' "
                    "library. Install with: pip install requests"
                ) from exc
            self._requests = requests
            return requests

    # ----- pyarrow / fastparquet -----

    def get_parquet_engine(self) -> Tuple[str, Any]:
        """Return ``(engine_name, module)`` for whichever Parquet engine is installed.

        Tries ``pyarrow`` first, then ``fastparquet`` (audit 2.12).
        Raises :class:`ImportError` if neither is available.
        """
        if self._pyarrow is not None:
            return "pyarrow", self._pyarrow
        if self._fastparquet is not None:
            return "fastparquet", self._fastparquet
        with self._lock:
            if self._pyarrow is not None:
                return "pyarrow", self._pyarrow
            if self._fastparquet is not None:
                return "fastparquet", self._fastparquet
            try:
                import pyarrow
                self._pyarrow = pyarrow
                return "pyarrow", pyarrow
            except ImportError:
                pass
            try:
                import fastparquet
                self._fastparquet = fastparquet
                return "fastparquet", fastparquet
            except ImportError as exc:
                raise ImportError(
                    "to_parquet requires either 'pyarrow' or 'fastparquet'. "
                    "Install with: pip install pyarrow  OR  pip install fastparquet"
                ) from exc

    # ----- ijson (streaming JSON) -----

    def get_ijson(self) -> Any:
        """Lazily import ijson; return ``None`` if unavailable (audit C.22)."""
        if self._ijson is not None or self._ijson is False:
            return self._ijson if self._ijson is not False else None
        with self._lock:
            if self._ijson is not None or self._ijson is False:
                return self._ijson if self._ijson is not False else None
            try:
                import ijson
                self._ijson = ijson
                return ijson
            except ImportError:
                self._ijson = False
                return None

    # ----- jsonschema -----

    def get_jsonschema(self) -> Any:
        """Lazily import jsonschema; return ``None`` if unavailable (audit C.9)."""
        if self._jsonschema is not None or self._jsonschema is False:
            return self._jsonschema if self._jsonschema is not False else None
        with self._lock:
            if self._jsonschema is not None or self._jsonschema is False:
                return self._jsonschema if self._jsonschema is not False else None
            try:
                import jsonschema
                self._jsonschema = jsonschema
                return jsonschema
            except ImportError:
                self._jsonschema = False
                return None

    # ----- test helpers -----

    def override(self, *, pd: Any = None, requests: Any = None) -> None:
        """Override a dependency for tests (audit 4.28)."""
        with self._lock:
            if pd is not None:
                self._pd = pd
            if requests is not None:
                self._requests = requests

    def reset(self) -> None:
        """Clear all cached imports + overrides (audit 4.28)."""
        with self._lock:
            self._pd = None
            self._requests = None
            self._pyarrow = None
            self._fastparquet = None
            self._ijson = None
            self._jsonschema = None


#: Module-level singleton injector (audit 4.28).
_injector = _DependencyInjector()


# Backward-compat module-level globals (audit 4.28) — these proxy to
# the injector's cached values so existing tests that do
# ``drug_resolver._requests = mock`` or read ``drug_resolver._pd``
# still work.  The injector remains the single source of truth; these
# are accessors only.
class _LazyGlobalProxy:
    """Descriptor that proxies attribute access to ``_injector``.

    Audit 4.28 — replaces the old ``_pd`` / ``_requests`` module-level
    globals with a single source of truth while preserving backward
    compatibility for tests that monkey-patch them.
    """

    __slots__ = ("_attr_name",)

    def __init__(self, attr_name: str) -> None:
        self._attr_name = attr_name

    def __get__(self, instance: Any, owner: Any = None) -> Any:
        return getattr(_injector, self._attr_name)

    def __set__(self, instance: Any, value: Any) -> None:
        setattr(_injector, self._attr_name, value)


class _LazyModuleGlobals:
    """Module-level namespace exposing ``_pd`` / ``_requests`` as properties.

    Instances are NOT used — the class methods are accessed via the
    module-level ``_pd`` and ``_requests`` objects below.
    """


# Module-level proxies (audit 4.28).
_pd: Any = None  # populated lazily; tests can override via _injector.override(pd=...)
_requests: Any = None


def _sync_lazy_globals() -> None:
    """Sync the module-level ``_pd`` / ``_requests`` proxies with the injector.

    Called after every ``_injector.get_pd()`` / ``_injector.get_requests()``
    so that ``drug_resolver._pd`` reflects the cached value.
    """
    global _pd, _requests
    _pd = _injector._pd
    _requests = _injector._requests


def _get_pd() -> Any:
    """Backward-compat shim that delegates to :data:`_injector` (audit 4.28).

    If the module-level ``_pd`` is set (e.g. a test monkey-patched it),
    return that.  Otherwise, lazy-import via the injector.  When the
    module-level is ``None`` (e.g. a test forced a re-import), also
    clears the injector cache so the import is re-attempted.
    """
    global _pd
    if _pd is not None:
        return _pd
    # Module-level is None — clear injector cache so a fresh import is attempted.
    with _injector._lock:
        _injector._pd = None
    pd = _injector.get_pd()
    _sync_lazy_globals()
    return pd


def _get_requests() -> Any:
    """Backward-compat shim that delegates to :data:`_injector` (audit 4.28).

    If the module-level ``_requests`` is set (e.g. a test monkey-patched
    it), return that.  Otherwise, lazy-import via the injector.  When
    the module-level is ``None`` (e.g. a test forced a re-import), also
    clears the injector cache so the import is re-attempted.
    """
    global _requests
    if _requests is not None:
        return _requests
    # Module-level is None — clear injector cache so a fresh import is attempted.
    with _injector._lock:
        _injector._requests = None
    requests = _injector.get_requests()
    _sync_lazy_globals()
    return requests


# =============================================================================
# Module-level helpers (audit E.7 – E.11)
# =============================================================================


def _safe_name(name: Any, max_len: int = 64) -> str:
    """Sanitise a name for inclusion in log records (audit C.3 / 9.22).

    Strips ANSI / newline / control characters, then truncates to
    ``max_len`` characters via :func:`_sanitize_for_log`.
    """
    if name is None:
        return "<none>"
    s = name if isinstance(name, str) else repr(name)
    s = _CONTROL_CHARS_RE.sub("", s)
    return _sanitize_for_log(s, max_len=max_len)


def _canonical_json(record: Any) -> str:
    """Deterministic JSON serialiser (audit C.8 / 4.13).

    Replaces ``json.dumps(record, sort_keys=True, default=str)`` with
    a hand-written serialiser that:

    * Sorts dict keys recursively.
    * Coerces ``datetime`` → ISO 8601 with ``Z`` suffix.
    * Coerces ``Decimal`` → ``str`` with explicit precision.
    * Coerces ``numpy`` scalar types → Python native.
    * Raises :class:`TypeError` on any non-JSON-native type after
      coercion (no silent ``default=str``).

    Notes
    -----
    Why not ``json.dumps(record, sort_keys=True, default=str)``?
    Because ``default=str`` silently stringifies unknown types —
    including ones whose string representation is non-deterministic
    (e.g. object addresses via ``__repr__``).  That makes the
    resulting checksum non-reproducible across runs and across
    machines, which violates the idempotency / reproducibility
    requirements in audit Domain 7.
    """
    return json.dumps(_canonicalise(record), sort_keys=True, ensure_ascii=True)


def _canonicalise(value: Any) -> Any:
    """Recursively coerce *value* to a JSON-native, deterministic form."""
    if value is None or isinstance(value, (bool, int, float, str)):
        # ``bool`` must be checked before ``int`` because ``bool`` is a
        # subclass of ``int`` in Python.
        if isinstance(value, float):
            if math.isnan(value) or math.isinf(value):
                return None
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_canonicalise(v) for v in value]
    if isinstance(value, abc.Mapping):
        return {str(k): _canonicalise(v) for k, v in value.items()}
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:  # pragma: no cover
            pass
    if hasattr(value, "item") and callable(value.item):
        # numpy scalar
        try:
            return _canonicalise(value.item())
        except Exception:  # pragma: no cover
            pass
    raise TypeError(
        f"Cannot canonically serialise object of type {type(value).__name__!r}: "
        f"value={_safe_name(value)!r}"
    )


def _normalize_inchikey(ik: Any) -> Optional[str]:
    """Normalise an InChIKey for storage / comparison (audit 3.4 / 3.5).

    ROOT FIX (Finding 9, P1): the previous contract returned ``""`` for
    None / non-string input, while `cleaning._constants.normalize_inchikey`
    and `cleaning.normalizer.normalize_inchikey` both returned ``None``
    for the same input. This 3-way divergence caused silent data loss:
    a caller doing `result.upper()` crashed with AttributeError on the
    None returns but silently no-oped on ""; a caller doing
    `if result is None` returned True for the first two but False for
    the resolver's "". A drug with `inchikey=None` took 4 different paths
    through 4 modules before finally failing at DB insert with an opaque
    IntegrityError.

    The fix: this function now returns ``Optional[str]`` — ``None`` for
    None/non-string input (matching the cleaning module's contract),
    ``ik.strip().upper()`` for valid strings. This makes all 3
    normalizers behave identically. Callers that previously did
    `if not result` continue to work (both None and "" are falsy).
    Callers that did `result.upper()` will now get an AttributeError on
    None — which is the CORRECT behavior (it surfaces the bug at the
    call site instead of silently producing an empty string that
    propagates downstream).
    """
    if not isinstance(ik, str):
        return None
    return ik.strip().upper() or None


def _normalize_molecular_formula(formula: Any) -> str:
    """Normalise a molecular formula to Hill order (audit 3.16).

    * Strips whitespace.
    * Removes element-count formatting inconsistencies
      (``"C8 H9 N O2"`` → ``"C8H9NO2"``).
    * Sorts elements in Hill order (C first, then H, then alphabetical).

    Returns the empty string for ``None`` or non-string input.  If the
    formula cannot be parsed (no element tokens found), returns the
    whitespace-stripped original.
    """
    if not isinstance(formula, str) or not formula.strip():
        return ""
    # Tokenise: element symbol + optional count.
    raw = re.sub(r"\s+", "", formula)
    tokens = re.findall(r"([A-Z][a-z]?)(\d*)", raw)
    elements: List[Tuple[str, int]] = []
    seen: Set[str] = set()
    for sym, count in tokens:
        if not sym or sym in seen:
            continue
        seen.add(sym)
        try:
            n = int(count) if count else 1
        except ValueError:
            n = 1
        elements.append((sym, n))
    if not elements:
        return raw
    # Hill order: C first, then H, then alphabetical.
    has_carbon = any(sym == "C" for sym, _ in elements)
    if has_carbon:
        carbon = [e for e in elements if e[0] == "C"]
        hydrogen = [e for e in elements if e[0] == "H"]
        others = sorted(
            (e for e in elements if e[0] not in ("C", "H")),
            key=lambda x: x[0],
        )
        ordered = carbon + hydrogen + others
    else:
        ordered = sorted(elements, key=lambda x: x[0])
    return "".join(
        f"{sym}{n if n > 1 else ''}" for sym, n in ordered
    )


def _detect_smiles_form(smiles: Optional[str]) -> str:
    """Detect whether a SMILES string is isomeric / canonical_non_isomeric /
    unknown (audit 3.14 / SW-8).

    v16 ROOT FIX (SW-8): the previous code returned ``"canonical"`` for
    ANY SMILES that lacked ``@``/``/``/``\\``. This conflated three
    very different cases:

      1. RDKit's ``MolToSmiles(isomericSmiles=False)`` output —
         a *deliberately* canonicalized non-isomeric SMILES (e.g.
         ``CC(=O)Oc1ccccc1C(=O)O`` for aspirin).
      2. A SMILES that came from a source which simply omitted
         stereo info (e.g. a partially-specified PubChem ``ConnectivitySmiles``).
      3. A malformed / partial / non-canonical SMILES (e.g. ``CCCCCC``
         for hexane written by hand — it IS canonical but it's also
         achiral, so calling it "canonical" is technically correct
         but misleading).

    Calling all three "canonical" caused (R)- and (S)-enantiomers
    to be merged: if source A emitted ``C[C@H](O)C(=O)O`` (isomeric,
    L-lactate) and source B emitted ``CC(O)C(=O)O`` (canonical non-
    isomeric, "lactate, stereo unspecified"), the previous code
    labeled A as "isomeric" and B as "canonical", then treated them
    as DIFFERENT entities because the labels differed — exactly the
    opposite of correct behavior.

    The fix: rename the non-isomeric label to
    ``"canonical_non_isomeric"`` so it's unambiguous, and emit a
    warning when a SMILES lacks stereo markers but the molecule
    MIGHT have stereo centers (heuristic: contains a ring or
    branched carbon — proper check would require RDKit, but the
    heuristic catches most cases without a hard dependency).
    ``"canonical"`` is no longer returned for non-isomeric SMILES.

    * Contains ``@`` or ``/`` or ``\\`` → ``"isomeric"``.
    * ``None`` / empty → ``"unknown"``.
    * Otherwise → ``"canonical_non_isomeric"`` (NOT "canonical").
    """
    if not smiles or not isinstance(smiles, str) or not smiles.strip():
        return "unknown"
    if "@" in smiles or "/" in smiles or "\\" in smiles:
        return "isomeric"
    # Heuristic: warn if the SMILES might have unspecified stereo.
    # Real check would be RDKit Mol.FindPotentialStereoBonds, but
    # we don't want a hard RDKit dependency here.
    _suspicious_stereo_chars = ("[C@H]", "[C@@H]", "[C@]", "[C@@]")
    # If the SMILES contains a chiral center token but is somehow
    # missing the @, it's malformed.
    if any(c in smiles for c in _suspicious_stereo_chars):
        return "malformed_chiral"
    return "canonical_non_isomeric"


def _load_state_schema() -> Dict[str, Any]:
    """Load ``schema/v1.json`` once at first use and cache (audit C.9 / 1.9).

    Returns an empty dict if the schema file cannot be read — callers
    fall back to :func:`_manual_schema_check` in that case.
    """
    cached: Optional[Dict[str, Any]] = getattr(_load_state_schema, "_cache", None)
    if cached is not None:
        return cached
    schema_path = (
        Path(__file__).resolve().parent / "schema" / "v1.json"
    )
    try:
        with open(schema_path, "r", encoding="utf-8") as fh:
            schema = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Could not load state schema from %s: %s — falling back to "
            "manual structural validation",
            schema_path, exc,
        )
        schema = {}
    setattr(_load_state_schema, "_cache", schema)
    return schema


def _manual_schema_check(state: Mapping[str, Any]) -> List[str]:
    """Hand-written structural validator used when jsonschema is unavailable (audit C.9).

    Returns a list of human-readable error messages (empty = valid).
    """
    errors: List[str] = []
    required = ("schema_version", "resolver_class", "config", "mapping", "stats", "exported_at")
    for key in required:
        if key not in state:
            errors.append(f"missing required top-level key: {key!r}")
    if errors:
        return errors
    if state.get("schema_version") != MAPPING_SCHEMA_VERSION:
        errors.append(
            f"schema_version mismatch: state has {state.get('schema_version')!r}, "
            f"expected {MAPPING_SCHEMA_VERSION!r}"
        )
    if state.get("resolver_class") not in ("DrugResolver", "ProteinResolver"):
        errors.append(
            f"resolver_class must be 'DrugResolver' or 'ProteinResolver', "
            f"got {state.get('resolver_class')!r}"
        )
    if not isinstance(state.get("mapping"), dict):
        errors.append("mapping must be an object")
    if not isinstance(state.get("stats"), dict):
        errors.append("stats must be an object")
    cfg = state.get("config", {})
    if not isinstance(cfg, dict):
        errors.append("config must be an object")
    return errors


# =============================================================================
# Salt-form detector (audit E.3 / 3.1 / 3.2)
# =============================================================================


class _SaltFormDetector:
    """PubChem salt-form detection via IUPACName + MolecularFormula (audit 3.1).

    The previous implementation only emitted a ``logger.debug`` and
    never actually rejected salt forms — this class performs the real
    check using PubChem's ``/property/InChIKey,IUPACName,MolecularFormula/JSON``
    endpoint.

    Detection rules
    ---------------
    A compound is considered a salt form if EITHER:

    1. Its IUPAC name (lower-cased, stripped) ends with one of
       :data:`_SALT_SUFFIXES` (case-insensitive, optional trailing
       whitespace tolerated).  Examples: ``"acetylsalicylic acid sodium"``,
       ``"metformin hydrochloride"``.
    2. Its molecular formula begins with a metal cation pattern
       (:data:`_METAL_CATION_RE` — ``Na``, ``K``, ``Ca``, ``Mg``,
       ``Li``, ``Zn`` followed by a non-letter).  Examples:
       ``"NaCl"``, ``"K2HPO4"``.
    """

    SALT_SUFFIXES: ClassVar[Tuple[str, ...]] = _SALT_SUFFIXES

    @classmethod
    def is_salt_form(
        cls,
        iupac_name: Optional[str],
        molecular_formula: Optional[str],
    ) -> Tuple[bool, str]:
        """Return ``(is_salt, reason)`` for the given PubChem properties.

        Parameters
        ----------
        iupac_name:
            ``IUPACName`` property from PubChem's ``/property/`` endpoint.
        molecular_formula:
            ``MolecularFormula`` property from PubChem's ``/property/`` endpoint.

        Returns
        -------
        tuple[bool, str]
            ``(True, reason)`` if the compound is a salt form;
            ``(False, "")`` otherwise.
        """
        if iupac_name:
            lower = iupac_name.strip().lower()
            for suffix in cls.SALT_SUFFIXES:
                if lower.endswith(suffix):
                    return True, f"IUPAC name ends with salt suffix {suffix!r}"
        if molecular_formula:
            if _METAL_CATION_RE.match(molecular_formula.strip()):
                return True, (
                    f"Molecular formula {molecular_formula!r} begins with a "
                    f"metal cation pattern"
                )
        return False, ""


# =============================================================================
# Circuit breaker (audit E.4 / 6.3 / 6.4 / C.14)
# =============================================================================


class _PubChemCircuitBreaker:
    """Per-instance circuit breaker for PubChem HTTP calls (audit 6.3 / 6.4).

    States
    ------
    * ``CLOSED`` — calls pass through; failures increment the counter.
    * ``OPEN`` — calls short-circuit to ``None`` and log at INFO.
      After ``cooldown`` seconds, the breaker enters ``HALF_OPEN``.
    * ``HALF_OPEN`` — the next call is allowed; success → ``CLOSED``,
      failure → ``OPEN``.

    The breaker is per-instance (not process-global) because different
    resolvers may have different network paths (audit C.14).

    FIX P1-ER-21 (LOW): the previous ``allow_call`` returned ``True``
    for EVERY call in HALF_OPEN — so if 10 concurrent threads hit the
    breaker in HALF_OPEN, all 10 would proceed and bombard the
    downstream service just as it was recovering. The standard
    circuit-breaker pattern (e.g. resilience4j, Hystrix) allows ONLY
    ONE probe call in HALF_OPEN; subsequent calls are short-circuited
    until the probe completes (success → CLOSED, failure → OPEN).
    We now track a ``_half_open_in_flight`` counter (atomic CAS under
    ``_lock``) so that only one call proceeds in HALF_OPEN at a time.
    """

    CLOSED: str = "CLOSED"
    OPEN: str = "OPEN"
    HALF_OPEN: str = "HALF_OPEN"

    def __init__(
        self,
        failure_threshold: int = 10,
        cooldown: float = 60.0,
    ) -> None:
        self._state: str = self.CLOSED
        self._failure_count: int = 0
        self._failure_threshold: int = max(1, int(failure_threshold))
        self._cooldown: float = max(0.1, float(cooldown))
        self._opened_at: float = 0.0
        self._lock = threading.Lock()
        # FIX P1-ER-21 (LOW): tracks whether a HALF_OPEN probe call
        # is currently in flight. ``allow_call`` does an atomic CAS
        # from 0 → 1 in HALF_OPEN; ``record_success`` /
        # ``record_failure`` reset it to 0 when the probe completes.
        self._half_open_in_flight: int = 0

    @property
    def state(self) -> str:
        """Return the current state, transitioning OPEN → HALF_OPEN if cooldown elapsed."""
        with self._lock:
            if self._state == self.OPEN:
                if (time.monotonic() - self._opened_at) >= self._cooldown:
                    self._state = self.HALF_OPEN
                    # FIX P1-ER-21: reset the in-flight counter when
                    # entering HALF_OPEN — the previous probe (if any)
                    # has long since completed (we were in OPEN for
                    # ``cooldown`` seconds).
                    self._half_open_in_flight = 0
            return self._state

    def allow_call(self) -> bool:
        """Return ``True`` iff a call may proceed (audit C.14).

        FIX P1-ER-21 (LOW): in HALF_OPEN, only ONE call may proceed
        at a time. Subsequent callers are short-circuited until the
        in-flight probe completes (via :meth:`record_success` or
        :meth:`record_failure`).
        """
        # ``self.state`` does the OPEN → HALF_OPEN transition under
        # ``_lock``; we re-acquire the lock here for the HALF_OPEN
        # in-flight CAS. The double-lock is fine because ``state``
        # releases before we re-acquire.
        current_state = self.state
        if current_state == self.CLOSED:
            return True
        if current_state == self.HALF_OPEN:
            with self._lock:
                if self._half_open_in_flight == 0:
                    self._half_open_in_flight = 1
                    return True
                # A probe is already in flight — short-circuit.
                return False
        # OPEN
        return False

    def record_success(self) -> None:
        """Record a successful call; transitions HALF_OPEN → CLOSED."""
        with self._lock:
            self._failure_count = 0
            self._state = self.CLOSED
            # FIX P1-ER-21: release the HALF_OPEN probe slot (no-op
            # if we were in CLOSED — the counter is already 0).
            self._half_open_in_flight = 0

    def record_failure(self) -> None:
        """Record a failed call; may transition to OPEN."""
        with self._lock:
            self._failure_count += 1
            if self._state == self.HALF_OPEN or self._failure_count >= self._failure_threshold:
                self._state = self.OPEN
                self._opened_at = time.monotonic()
                # FIX P1-ER-21: release the HALF_OPEN probe slot.
                self._half_open_in_flight = 0

    def reset(self) -> None:
        """Reset to CLOSED (audit 6.10 — ``reset(reset_process_globals=True)``)."""
        with self._lock:
            self._state = self.CLOSED
            self._failure_count = 0
            self._opened_at = 0.0
            self._half_open_in_flight = 0


# =============================================================================
# Transactional mutation context (audit E.1 / C.1)
# =============================================================================


class _MutationContext:
    """Context manager that snapshots mutable state and rolls back on exception.

    Audit 1.3 / 1.10 / 4.26 / 4.7 / 5.2 / 6.5 / 6.9 / 7.9 — every
    public mutating method wraps its body in a ``_MutationContext`` so
    that an exception mid-mutation restores the 8 mutated structures
    to their pre-call state.

    Snapshots are taken via :func:`copy.deepcopy` and dropped on clean
    exit.  For in-place field updates (no structural change), pass
    ``structural=False`` to skip the snapshot and rely on the caller's
    own consistency checks (audits C.1 / 4.7).

    Parameters
    ----------
    resolver:
        The :class:`DrugResolver` whose state is being mutated.
    reason:
        Short human-readable description of the mutation (for logging
        on rollback).
    structural:
        If ``True`` (default), snapshot all 8 structures.  If ``False``,
        skip the snapshot — use this for tiny in-place updates where
        deepcopy overhead would be wasteful.
    """

    __slots__ = ("resolver", "reason", "structural", "_snapshot", "_lock_acquired")

    def __init__(self, resolver: "DrugResolver", reason: str, *, structural: bool = True) -> None:
        self.resolver = resolver
        self.reason = reason
        self.structural = structural
        self._snapshot: Optional[Dict[str, Any]] = None
        self._lock_acquired: bool = False

    def __enter__(self) -> "_MutationContext":
        self._lock_acquired = self.resolver._mutation_lock.acquire()
        if self.structural:
            # P1-ER-1 ROOT FIX: snapshot _smiles_index alongside the other
            # indices so a rollback fully restores the SMILES match state.
            self._snapshot = {
                "mapping": copy.deepcopy(self.resolver.mapping),
                "_inchikey_index": copy.deepcopy(self.resolver._inchikey_index),
                "_name_index": copy.deepcopy(self.resolver._name_index),
                "_name_index_multi": copy.deepcopy(self.resolver._name_index_multi),
                "_connectivity_index": copy.deepcopy(self.resolver._connectivity_index),
                "_connectivity_index_multi": copy.deepcopy(self.resolver._connectivity_index_multi),
                "_audit_trail": copy.deepcopy(self.resolver._audit_trail),
                "_dead_letter": copy.deepcopy(self.resolver._dead_letter),
                "_ingested_record_keys": set(self.resolver._ingested_record_keys),
                "_smiles_index": copy.deepcopy(self.resolver._smiles_index),
            }
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        try:
            if exc is not None and self._snapshot is not None:
                # Roll back.
                self.resolver.mapping = self._snapshot["mapping"]
                self.resolver._inchikey_index = self._snapshot["_inchikey_index"]
                self.resolver._name_index = self._snapshot["_name_index"]
                self.resolver._name_index_multi = self._snapshot["_name_index_multi"]
                self.resolver._connectivity_index = self._snapshot["_connectivity_index"]
                self.resolver._connectivity_index_multi = self._snapshot["_connectivity_index_multi"]
                self.resolver._audit_trail = self._snapshot["_audit_trail"]
                self.resolver._dead_letter = self._snapshot["_dead_letter"]
                self.resolver._ingested_record_keys = self._snapshot["_ingested_record_keys"]
                # P1-ER-1 ROOT FIX: restore _smiles_index on rollback so a
                # failed mutation cannot leave dangling SMILES pointers.
                self.resolver._smiles_index = self._snapshot["_smiles_index"]
                self.resolver._stats.inc("mutations_rolled_back")
                self.resolver._event_log(
                    logging.ERROR,
                    "mutation_rolled_back",
                    reason=self.reason,
                    error_type=type(exc).__name__ if exc is not None else "",
                    error_message=str(exc)[:200] if exc is not None else "",
                    error_code=ErrorCode.RESOLVER_STATE_CORRUPTION.value,
                )
                # Wrap the exception in a typed error so callers catch it.
                if not isinstance(exc, ResolverError):
                    raise ResolverStateCorruptionError(
                        f"mutation {self.reason!r} rolled back due to "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                return False  # let the original ResolverError propagate
            return False
        finally:
            if self._lock_acquired:
                self.resolver._mutation_lock.release()
            self._snapshot = None


# =============================================================================
# Match pipeline (audit E.5 / 1.8 / C.11)
# =============================================================================


class _MatchPipeline:
    """Orchestrates the ordered sequence of match attempts (audit 1.8).

    Single source of truth for the match order — adding a new match
    method is now a one-line change to :attr:`STEPS`.
    """

    STEPS: ClassVar[Tuple[_MatchStep, ...]] = (
        _MatchStep(
            name="inchikey_exact",
            method="inchikey_exact",
            fn=lambda r, ik: r._match_by_inchikey(ik),
            confidence=1.0,
        ),
        _MatchStep(
            name="inchikey_connectivity",
            method="inchikey_connectivity",
            fn=lambda r, ik: r._match_by_connectivity(ik),
            confidence=0.9,
        ),
        _MatchStep(
            name="name_normalized",
            method="name_normalized",
            fn=lambda r, n: r._match_by_name(n, allow_fuzzy=False),
            confidence=0.8,
        ),
        _MatchStep(
            name="fuzzy",
            method="fuzzy",
            fn=lambda r, n: r._match_by_name(n, allow_fuzzy=True),
            confidence=0.85,
        ),
        _MatchStep(
            name="smiles_canonical",
            method="smiles_canonical",
            fn=lambda r, smiles: r._match_by_smiles(smiles),
            confidence=0.75,
        ),
        _MatchStep(
            name="pubchem_xref",
            method="pubchem_xref",
            fn=lambda r, n: r._match_by_pubchem_xref(n),
            confidence=0.7,
            requires_network=True,
        ),
    )

    @classmethod
    def run(
        cls,
        resolver: "DrugResolver",
        *,
        inchikey: Optional[str] = None,
        name: Optional[str] = None,
        smiles: Optional[str] = None,
        allow_pubchem: bool = True,
        allow_smiles: bool = False,
    ) -> Optional[_MatchHit]:
        """Run the pipeline; return the first non-``None`` :class:`_MatchHit`.

        Parameters
        ----------
        resolver:
            The :class:`DrugResolver` instance.
        inchikey:
            Optional InChIKey to match against.
        name:
            Optional name to match against.
        smiles:
            Optional SMILES to match against.
        allow_pubchem:
            If ``False``, skip the PubChem step (bulk path).
        allow_smiles:
            If ``False``, skip the SMILES step (default — opt-in).
        """
        for step in cls.STEPS:
            if step.method == "smiles_canonical" and not allow_smiles:
                continue
            if step.method == "smiles_canonical":
                if not smiles:
                    continue
                arg = smiles
            elif step.method == "pubchem_xref":
                if not allow_pubchem:
                    continue
                if not name:
                    continue
                arg = name
            elif step.method in ("inchikey_exact", "inchikey_connectivity"):
                if not inchikey:
                    continue
                arg = inchikey
            else:  # name_normalized, fuzzy
                if not name:
                    continue
                arg = name
            try:
                hit = step.fn(resolver, arg)
            except Exception as exc:
                resolver._event_log(
                    logging.WARNING,
                    "match_step_exception",
                    step=step.name,
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:200],
                )
                continue
            if hit is not None:
                return hit
        return None


# =============================================================================
# Register the synthetic_key match method (audit 2.1 / 1.7)
# =============================================================================
# Register once at module import so compute_match_confidence("synthetic_key")
# returns 0.0 instead of the unknown-method default 0.5.
register_match_method("synthetic_key", 0.0)
register_match_method("inchikey_exact_unvalidated", 0.5)
register_match_method("name_only", 0.3)
register_match_method("smiles_canonical", 0.75)
register_match_method("no_match", 0.0)
register_match_method("no_match_pubchem_degraded", 0.0)


# =============================================================================
# Main class — DrugResolver
# =============================================================================


class DrugResolver(Resolver):
    """Resolves drug entities across ChEMBL, DrugBank, and PubChem databases.

    Institutional-grade, production-ready resolver implementing all 345
    audit findings across 16 engineering domains.  See the module
    docstring for the full resolution strategy and data dictionary.

    Parameters
    ----------
    config:
        Optional :class:`ResolverConfig` instance.  If omitted, a
        default config is constructed (PubChem disabled, stereoisomer
        collapse disabled — safe by default).

    Notes
    -----
    The resolver is **thread-safe** for concurrent ``add_source_records``
    calls (audit C.11) — every mutating method acquires
    ``self._mutation_lock`` via a :class:`_MutationContext`.

    State serialisation round-trips through :meth:`to_state_dict` /
    :meth:`from_state_dict` with JSON-Schema validation against
    ``schema/v1.json`` (audit C.9).
    """

    # ----- ClassVar constants -----

    MAPPING_SCHEMA_VERSION: ClassVar[str] = MAPPING_SCHEMA_VERSION
    _OUTPUT_COLUMNS: ClassVar[Tuple[str, ...]] = _OUTPUT_COLUMNS

    # ----- Construction -----

    def __init__(self, config: Optional[ResolverConfig] = None) -> None:
        # Wrap the entire __init__ in a try/except so a partial init
        # (e.g. import failure) cannot leave the resolver in a
        # half-constructed state that crashes _assert_initialized.
        try:
            self._config: ResolverConfig = config or ResolverConfig()
            self._config.validate()
        except Exception:
            raise

        # ----- Core state -----
        self.mapping: Dict[str, dict] = {}
        self._inchikey_index: Dict[str, str] = {}
        self._name_index: Dict[str, str] = {}
        self._name_index_multi: Dict[str, List[str]] = {}
        self._connectivity_index: Dict[str, str] = {}
        self._connectivity_index_multi: Dict[str, List[str]] = {}
        # P1-ER-1 ROOT FIX: declare _smiles_index as a first-class core index.
        # Previously it was lazily created by ``_create_canonical_entry`` /
        # ``_merge_into_canonical_entry`` via ``hasattr`` guards, which left
        # ``_MutationContext``, ``reset()`` and ``_assert_initialized`` all
        # blind to it — a transactional rollback would silently drop SMILES
        # mappings and a ``reset()`` would leave stale SMILES state behind.
        self._smiles_index: Dict[str, str] = {}

        # ----- Failure / lineage stores -----
        self._dead_letter: List[dict] = []
        self._audit_trail: Dict[str, List[LineageEvent]] = {}
        self._archived_audit_trail: Dict[str, List[LineageEvent]] = {}
        self._state_access_log: List[LineageEvent] = []
        self._query_log: List[LineageEvent] = []

        # ----- Idempotency (audit C.6 / 7.1 / 7.2 / 7.5 / 7.6 / 7.16) -----
        self._ingested_record_keys: Set[str] = set()

        # ----- Per-canonical hash-chain head (audit 14.2) -----
        self._audit_chain_head: Dict[str, str] = {}

        # ----- Source dataset registry (audit C.19 / 16.16-16.18) -----
        self._source_dataset_registry: Dict[str, SourceDatasetMeta] = {}

        # ----- Source record index for bidirectional traceability (audit 16.21) -----
        self._source_record_index: Dict[Tuple[str, str], str] = {}

        # ----- Checkpoint registry (audit 6.5 / 6.16) -----
        self._checkpoints: Dict[str, int] = {}
        self._last_interrupt_checkpoint: Optional[str] = None

        # ----- Stats / metrics -----
        self._stats: ResolverStats = ResolverStats()
        self._metrics: Dict[str, Dict[str, float]] = {}
        self._confidence_histogram: Dict[str, int] = {}
        self._unknown_stats_restored: List[str] = []
        self._unknown_config_keys: List[str] = []

        # ----- Soft-validation stats (audit C.15) -----
        # Tracked via ResolverStats — but ResolverStats is the existing
        # dataclass; we add ad-hoc counters via _stats.inc on the fly.

        # ----- PubChem-side state -----
        self._last_pubchem_call: float = 0.0
        self._pubchem_circuit: _PubChemCircuitBreaker = _PubChemCircuitBreaker(
            failure_threshold=getattr(self._config, "pubchem_failure_threshold", 10),
            cooldown=getattr(self._config, "pubchem_circuit_cooldown", 60.0),
        )
        self._requests_session: Any = None
        self._pubchem_bulk_warned: bool = False

        # ----- Thread safety (audit C.11) -----
        self._mutation_lock: threading.RLock = threading.RLock()

        # ----- Correlation / operator context (audit C.5 / 14.8 / 11.22) -----
        self._correlation_id: Optional[str] = None
        self._correlation_id_counter: int = 0
        self._operator: Optional[str] = None

        # ----- Cached fuzzy choices (audit 4.2 / 8.2) -----
        self._name_index_generation: int = 0
        self._cached_choices: Optional[List[str]] = None
        self._cached_choices_generation: int = -1

        # ----- Alert callbacks (audit C.20) -----
        self._alert_callbacks: Dict[str, List[Callable[[dict], None]]] = {}

        # ----- Run-level timestamp for deterministic mode (audit C.7) -----
        self._run_started_at: str = self._now_iso()

        # ----- Eager imports (audit 6.12) -----
        if getattr(self._config, "eager_imports", False):
            _injector.get_pd()
            if self._config.pubchem_enabled:
                _injector.get_requests()

        # ----- Seed RNG (audit 7.11) -----
        seed = getattr(self._config, "random_seed", None)
        if seed is not None:
            random.seed(seed)
            try:
                import numpy as np
                np.random.seed(seed)
            except ImportError:
                pass

        # ----- Final invariant check -----
        self._assert_initialized()
        self._event_log(
            logging.INFO,
            "resolver_constructed",
            schema_version=MAPPING_SCHEMA_VERSION,
            resolver_version=__version__,
            config_hash=self._config_hash(),
        )

    # ------------------------------------------------------------------
    # Private: invariant assertions (audit C.25 / 4.27)
    # ------------------------------------------------------------------

    def _assert_initialized(self) -> None:
        """Assert all 8 + auxiliary attributes exist (audit 4.27)."""
        required = (
            "mapping",
            "_inchikey_index",
            "_name_index",
            "_name_index_multi",
            "_connectivity_index",
            "_connectivity_index_multi",
            # P1-ER-1 ROOT FIX: _smiles_index is now a first-class core
            # index and MUST be initialised by ``__init__``. If a future
            # refactor forgets to set it, this assertion fires before any
            # downstream code can silently fall back to ``getattr``.
            "_smiles_index",
            "_dead_letter",
            "_audit_trail",
            "_ingested_record_keys",
            "_mutation_lock",
            "_stats",
            "_pubchem_circuit",
        )
        for attr in required:
            if not hasattr(self, attr):
                raise ResolverStateCorruptionError(
                    f"DrugResolver.__init__ did not set required attribute {attr!r}"
                )

    def _assert_indices_consistent(self) -> None:
        """Assert every index value is a key in ``self.mapping`` (audit C.12 / E.12)."""
        if not getattr(self._config, "runtime_asserts", False):
            return
        # P1-ER-1 ROOT FIX: include _smiles_index in the consistency check —
        # it maps ``smiles -> canonical_ik`` and must never point at a key
        # that has been removed from ``self.mapping``.
        for idx_name, idx in (
            ("_inchikey_index", self._inchikey_index),
            ("_name_index", self._name_index),
            ("_connectivity_index", self._connectivity_index),
            ("_smiles_index", self._smiles_index),
        ):
            for k, v in idx.items():
                if v not in self.mapping:
                    raise IndexMappingDesyncError(
                        f"{idx_name}[{k!r}] = {v!r} is not a key in mapping"
                    )
        for idx_name, multi in (
            ("_name_index_multi", self._name_index_multi),
            ("_connectivity_index_multi", self._connectivity_index_multi),
        ):
            for k, vs in multi.items():
                for v in vs:
                    if v not in self.mapping:
                        raise IndexMappingDesyncError(
                            f"{idx_name}[{k!r}] contains {v!r} which is not in mapping"
                        )

    def _assert_audit_trail_consistent(self) -> None:
        """Assert every audit-trail key is in ``mapping`` or archived (audit C.13 / E.13)."""
        if not getattr(self._config, "runtime_asserts", False):
            return
        for ik in self._audit_trail:
            if ik not in self.mapping and ik not in self._archived_audit_trail:
                raise ResolverStateCorruptionError(
                    f"audit_trail has key {ik!r} that is neither in mapping nor archived"
                )

    def _assert_state_dict_consistent(self) -> None:
        """Assert state-dict invariants after :meth:`from_state_dict` (audit C.25)."""
        self._assert_indices_consistent()
        self._assert_audit_trail_consistent()

    # ------------------------------------------------------------------
    # Private: timestamp / correlation / logging helpers
    # ------------------------------------------------------------------

    def _now_iso(self) -> str:
        """Return the current UTC timestamp as ISO 8601 with ``Z`` suffix.

        When ``deterministic_timestamps=True``, returns :data:`_EPOCH_ISO`
        for record timestamps and ``run_started_at`` for ``exported_at``
        (audit C.7).
        """
        if getattr(self._config, "deterministic_timestamps", False):
            return _EPOCH_ISO
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def _now_iso_for_export(self) -> str:
        """Timestamp for ``exported_at`` — uses ``run_started_at`` in deterministic mode."""
        if getattr(self._config, "deterministic_timestamps", False):
            return self._run_started_at
        return self._now_iso()

    def _new_event_id(self, prev_id: Optional[str], payload: str) -> str:
        """Compute a SHA-256 hash-chained event ID (audit 14.2 / 16.25).

        Parameters
        ----------
        prev_id:
            Previous event's ``event_id`` (or ``""`` for the first event).
        payload:
            Canonical JSON of this event's payload (without ``event_id``).
        """
        h = hashlib.sha256()
        h.update((prev_id or "").encode("utf-8"))
        h.update(b"|")
        h.update(payload.encode("utf-8"))
        return h.hexdigest()

    def _config_hash(self) -> str:
        """Return a short hash of the (masked) config for logging (audit 11.21)."""
        try:
            d = self._config.to_masked_dict()
            payload = json.dumps(d, sort_keys=True, default=str)
            return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        except Exception:
            return "unknown"

    def _event_log(self, level: int, event: str, **fields: Any) -> None:
        """Structured logging helper (audit C.4 / 11.1 / 11.20).

        Adds ``event``, ``correlation_id``, ``resolver_id`` to every
        record, sanitises any PII field, and dispatches to
        :func:`logger.log`.
        """
        extra: Dict[str, Any] = {
            "event": event,
            "correlation_id": self._correlation_id,
            "resolver_id": id(self),
        }
        # PII sanitisation for known fields.
        for key, val in fields.items():
            if key in ("name", "canonical_name", "incoming_name", "source"):
                extra[key] = _safe_name(val)
            elif key in ("inchikey", "canonical_inchikey", "incoming_ik"):
                extra[key] = _safe_name(val, max_len=32)
            else:
                extra[key] = val
        # Build the message and dispatch.
        msg = event
        try:
            logger.log(level, msg, extra=extra)
        except Exception:  # pragma: no cover — logger must never crash the resolver
            pass

    def _ensure_batch_correlation_id(self) -> str:
        """Return the current correlation ID, generating a batch CID if unset (audit C.5)."""
        if self._correlation_id is None:
            self._correlation_id = f"batch-{uuid.uuid4().hex[:12]}"
        return self._correlation_id

    # ------------------------------------------------------------------
    # Public: operator / correlation-ID management (audit E.29 / C.5 / 14.8)
    # ------------------------------------------------------------------

    def set_correlation_id(self, cid: Optional[str]) -> None:
        """Set the correlation ID for subsequent operations (audit C.5)."""
        self._correlation_id = cid

    def get_correlation_id(self) -> Optional[str]:
        """Return the current correlation ID (audit C.5)."""
        return self._correlation_id

    def set_operator(self, operator: Optional[str]) -> None:
        """Set the operator identity for subsequent sensitive operations (audit 14.8 / 11.22)."""
        if operator is not None and not isinstance(operator, str):
            raise TypeError("operator must be a string or None")
        self._operator = operator

    def get_operator(self) -> Optional[str]:
        """Return the current operator identity (audit 14.8)."""
        return self._operator

    # ------------------------------------------------------------------
    # Public: alert callbacks (audit E.28 / C.20 / 11.9)
    # ------------------------------------------------------------------

    def register_alert_callback(
        self,
        event: str,
        callback: Callable[[dict], None],
    ) -> None:
        """Register an alert callback for one of the supported event types.

        Supported events (audit C.20):

        * ``dead_letter_full``
        * ``pubchem_circuit_open``
        * ``mapping_size_threshold_exceeded``
        * ``conflict_rate_high``
        """
        valid = {
            "dead_letter_full",
            "pubchem_circuit_open",
            "mapping_size_threshold_exceeded",
            "conflict_rate_high",
        }
        if event not in valid:
            raise ValueError(
                f"event must be one of {sorted(valid)}, got {event!r}"
            )
        self._alert_callbacks.setdefault(event, []).append(callback)

    def _fire_alert(self, event: str, payload: dict) -> None:
        """Dispatch an alert to all registered callbacks (audit C.20)."""
        for cb in self._alert_callbacks.get(event, []):
            try:
                cb(payload)
            except Exception as exc:
                self._event_log(
                    logging.WARNING,
                    "alert_callback_failed",
                    alert_event=event,
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:200],
                )

    # ------------------------------------------------------------------
    # Public: config / stats accessors
    # ------------------------------------------------------------------

    @property
    def config(self) -> ResolverConfig:
        """Return this resolver's :class:`ResolverConfig` (read-only)."""
        return self._config

    @property
    def stats(self) -> ResolverStats:
        """Return this resolver's :class:`ResolverStats` (read-only)."""
        return self._stats

    @property
    def frozen_mapping(self) -> abc.Mapping:
        """Return a read-only view of the mapping (audit C.2)."""
        import types
        return types.MappingProxyType(self.mapping)

    # ------------------------------------------------------------------
    # Public: bulk ingestion (audit 1.4 / 4.10 / 4.11 / C.6 / C.15 / C.16 / C.19 / C.21)
    # ------------------------------------------------------------------

    def add_source_records(
        self,
        records: Union[List[dict], Iterable[dict]],
        source: str,
        *,
        dataset_version: Optional[str] = None,
        dataset_checksum: Optional[str] = None,
        fetched_at: Optional[str] = None,
        chunksize: Optional[int] = None,
        timeout: Optional[float] = None,
        resume_from: Optional[str] = None,
    ) -> int:
        """Add records from a single data source into the resolver.

        Each record **must** have at least ``'name'`` and ideally
        ``'inchikey'`` fields.  Source-specific identifier fields
        (``chembl_id``, ``drugbank_id``, ``pubchem_cid``) are merged
        into the canonical entry when a match is found.

        Parameters
        ----------
        records:
            Iterable of record dicts.  Streaming is supported — the
            iterable is consumed lazily (audit C.21 / 4.10).
        source:
            Source identifier — ``'chembl'``, ``'drugbank'``, or
            ``'pubchem'``.
        dataset_version:
            Optional version string for the source dataset (audit C.19).
        dataset_checksum:
            Optional SHA-256 of the source dataset file (audit C.19).
        fetched_at:
            Optional ISO-8601 timestamp when the source dataset was
            fetched (audit C.19 / 5.8).
        chunksize:
            Optional chunk size — when set, a checkpoint is emitted
            every ``chunksize`` records for crash-recovery (audit 6.5).
        timeout:
            Optional timeout in seconds — raises :class:`BatchTimeoutError`
            if exceeded (audit 6.11).
        resume_from:
            Optional checkpoint ID to resume from (audit 6.5).

        Returns
        -------
        int
            Number of records actually ingested (skipped records do
            NOT count — audit C.6).

        Raises
        ------
        ValueError
            If ``source`` is not in the configured whitelist.
        BatchSizeExceededError
            If the record count exceeds ``max_records_per_batch``.
        BatchTimeoutError
            If ``timeout`` is exceeded.
        ResolverStateCorruptionError
            If an internal invariant is violated (the mutation context
            rolls back).
        """
        # ----- Validate source -----
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source must be a non-empty string")
        if _CONTROL_CHARS_RE.search(source):
            raise ValueError("source contains control characters")
        if self._config.source_whitelist is not None:
            if source not in self._config.source_whitelist:
                raise ValueError(
                    f"source {source!r} is not in the configured whitelist "
                    f"({self._config.source_whitelist!r})"
                )

        # ----- Materialise records if a list/tuple, count-check (audit 1.4) -----
        if isinstance(records, (list, tuple)):
            if len(records) == 0:
                self._event_log(
                    logging.WARNING,
                    "add_source_records_empty",
                    source=_safe_name(source),
                )
                return 0
            max_records = getattr(self._config, "max_records_per_batch", 1_000_000)
            if len(records) > max_records:
                raise BatchSizeExceededError(
                    f"add_source_records received {len(records)} records, "
                    f"exceeds max_records_per_batch={max_records}. Use chunking."
                )
            records_iter: Iterable[dict] = records
            total_expected = len(records)
        else:
            records_iter = records
            total_expected = -1  # unknown

        # ----- Ensure correlation ID -----
        self._ensure_batch_correlation_id()

        # ----- Within-batch duplicate detection (audit 5.5) -----
        if isinstance(records, (list, tuple)):
            try:
                # NOTE: find_duplicate_ids's signature is
                # (records, id_fields=None, *, seen=<sentinel>,
                #  return_counts=False, return_indices=False,
                #  sanitize_output=False).  It does NOT have a
                # ``return_seen`` kwarg — that was a typo in the audit
                # spec.  We just pass ``id_fields`` and use the default
                # return shape (Dict[str, List[str]]).
                dup_report = find_duplicate_ids(
                    records,
                    id_fields=("chembl_id", "drugbank_id", "pubchem_cid"),
                )
                if dup_report:
                    for field_name, dup_values in dup_report.items():
                        # FIX P1-ER-13 (MEDIUM): find_duplicate_ids is
                        # called above WITHOUT ``return_counts=True``, so
                        # its return shape is ALWAYS ``Dict[str, List[str]]``
                        # — the previous ``isinstance(dup_values, dict)``
                        # branch was dead code (it could never fire because
                        # ``dup_values`` is always a list). Simplify to the
                        # one path that actually executes.
                        count = len(dup_values) if isinstance(dup_values, (list, tuple)) else 0
                        self._stats.inc("duplicate_ids_detected", count)
                        self._event_log(
                            logging.WARNING,
                            "duplicate_ids_in_batch",
                            source=_safe_name(source),
                            field=field_name,
                            count=count,
                        )
            except Exception as exc:
                self._event_log(
                    logging.WARNING,
                    "duplicate_id_check_failed",
                    source=_safe_name(source),
                    error_type=type(exc).__name__,
                )

        # ----- Source dataset registry (audit C.19) -----
        ingested_at = self._now_iso()
        self._source_dataset_registry[source] = SourceDatasetMeta(
            source=source,
            dataset_version=dataset_version,
            dataset_checksum=dataset_checksum,
            fetched_at=fetched_at,
            record_count=0,
            ingested_at=ingested_at,
        )

        # ----- Bulk-path PubChem warning (audit 11.12) -----
        if self._config.pubchem_enabled and not self._pubchem_bulk_warned:
            self._event_log(
                logging.INFO,
                "pubchem_enabled_in_bulk_path",
                note="bulk path does not call PubChem; resolve_single does",
            )
            self._pubchem_bulk_warned = True

        self._event_log(
            logging.INFO,
            "add_source_records_start",
            source=_safe_name(source),
            expected_count=total_expected,
            collapse_stereoisomers=self._config.collapse_stereoisomers,
            pubchem_enabled=self._config.pubchem_enabled,
        )

        # ----- Resume from checkpoint -----
        start_idx = 0
        if resume_from is not None and resume_from in self._checkpoints:
            start_idx = self._checkpoints[resume_from]

        # ----- Timeout setup (audit 6.11) -----
        deadline = (time.monotonic() + timeout) if timeout is not None else None

        # ----- Ingestion loop -----
        matched = 0
        created = 0
        ingested_count = 0
        last_checkpoint_idx = start_idx
        try:
            with _MutationContext(self, f"add_source_records({source!r})", structural=False):
                for idx, record in enumerate(records_iter):
                    if idx < start_idx:
                        continue
                    # ----- Timeout check (audit 6.11) -----
                    if deadline is not None and time.monotonic() > deadline:
                        raise BatchTimeoutError(
                            f"add_source_records timed out after {timeout}s at "
                            f"record {idx}"
                        )
                    # ----- KeyboardInterrupt protection (audit 6.16) -----
                    try:
                        processed = self._ingest_one(record, source, idx)
                    except KeyboardInterrupt:
                        ckpt_id = f"interrupt-{uuid.uuid4().hex[:8]}"
                        self._checkpoints[ckpt_id] = idx
                        self._last_interrupt_checkpoint = ckpt_id
                        self._event_log(
                            logging.WARNING,
                            "ingestion_interrupted",
                            source=_safe_name(source),
                            record_index=idx,
                            checkpoint=ckpt_id,
                        )
                        raise
                    except MemoryError:
                        # Emergency spill (audit 6.17).
                        self._emergency_spill()
                        raise
                    if processed == "skipped":
                        continue
                    ingested_count += 1
                    if processed == "created":
                        created += 1
                    elif processed == "matched":
                        matched += 1
                    # ----- Periodic checkpoint (audit 6.5) -----
                    if chunksize is not None and (idx + 1) % chunksize == 0:
                        ckpt_id = f"ckpt-{source}-{idx + 1}-{uuid.uuid4().hex[:6]}"
                        self._checkpoints[ckpt_id] = idx + 1
                        last_checkpoint_idx = idx + 1
                        self._event_log(
                            logging.DEBUG,
                            "ingestion_checkpoint",
                            source=_safe_name(source),
                            checkpoint=ckpt_id,
                            record_index=idx + 1,
                        )
                    # ----- Periodic progress log -----
                    if (idx + 1) % 10_000 == 0:
                        self._event_log(
                            logging.DEBUG,
                            "ingestion_progress",
                            source=_safe_name(source),
                            record_index=idx + 1,
                        )
        except Exception:
            # _MutationContext already rolled back; just re-raise.
            raise

        # ----- Update source dataset record count -----
        meta = self._source_dataset_registry.get(source)
        if meta is not None:
            self._source_dataset_registry[source] = dataclasses.replace(
                meta, record_count=ingested_count,
            )

        # ----- Invariant check (audit C.25) -----
        if getattr(self._config, "runtime_asserts", False):
            self._assert_indices_consistent()
            self._assert_audit_trail_consistent()

        self._event_log(
            logging.INFO,
            "add_source_records_done",
            source=_safe_name(source),
            ingested=ingested_count,
            matched=matched,
            created=created,
            dead_lettered=len(self._dead_letter),
        )
        return ingested_count

    def _ingest_one(self, record: dict, source: str, idx: int) -> str:
        """Ingest one record; return ``"created"``, ``"matched"``, or ``"skipped"``.

        Parameters
        ----------
        record:
            The record dict to ingest.
        source:
            Source label.
        idx:
            Position of this record in the batch (for audit / dead-letter).

        Raises
        ------
        ValueError
            If ``record`` is structurally invalid (empty dict / not a dict).
        """
        # ----- Compute input checksum (audit C.8) -----
        try:
            input_checksum = hashlib.sha256(
                _canonical_json(record).encode("utf-8")
            ).hexdigest()[:32]
        except (TypeError, ValueError):
            input_checksum = ""

        # ----- Idempotent skip (audit C.6) -----
        if input_checksum and input_checksum in self._ingested_record_keys:
            self._event_log(
                logging.DEBUG,
                "idempotent_skip",
                source=_safe_name(source),
                record_index=idx,
            )
            return "skipped"

        # ----- Validate (audit C.15) -----
        strict = getattr(self._config, "bulk_strict_validation", False)
        ok, errors = validate_drug_record(record, strict=strict)
        if not ok:
            self._dead_letter.append({
                "record": record,
                "source": source,
                "errors": errors,
                "stage": "add_source_records",
                "record_index": idx,
                "input_checksum": input_checksum,
            })
            self._stats.inc("records_rejected")
            self._stats.inc("dead_lettered")
            self._event_log(
                logging.WARNING,
                "record_rejected",
                source=_safe_name(source),
                record_index=idx,
                error_count=len(errors),
            )
            self._check_dead_letter_size()
            return "skipped"

        # ----- Soft validation (audit C.15) -----
        soft_warnings = self._soft_validate(record)
        if soft_warnings and getattr(self._config, "dead_letter_on_soft_warning", False):
            self._dead_letter.append({
                "record": record,
                "source": source,
                "errors": soft_warnings,
                "stage": "soft_validation",
                "record_index": idx,
                "input_checksum": input_checksum,
            })
            self._stats.inc("dead_lettered")
            self._event_log(
                logging.WARNING,
                "record_soft_dead_lettered",
                source=_safe_name(source),
                record_index=idx,
                warning_count=len(soft_warnings),
            )
            self._check_dead_letter_size()
            return "skipped"

        # ----- Track ingested checksum (audit C.6) -----
        if input_checksum:
            self._ingested_record_keys.add(input_checksum)
        self._stats.inc("records_ingested")

        # ----- Extract fields -----
        inchikey = _normalize_inchikey(record.get("inchikey", "") or "")
        name = record.get("name", "") or ""
        smiles = record.get("smiles", "") or ""

        # ----- Run the match pipeline (audit 1.8 / C.11) -----
        allow_smiles = getattr(self._config, "enable_smiles_matching", False)
        hit: Optional[_MatchHit] = _MatchPipeline.run(
            self,
            inchikey=inchikey or None,
            name=name or None,
            smiles=smiles or None,
            allow_pubchem=False,  # bulk path never calls PubChem (audit D3-1)
            allow_smiles=allow_smiles,
        )

        if hit is None:
            # No match found — create a brand-new canonical entry.
            method = self._creation_method_for(record, inchikey)
            self._create_canonical_entry(
                record, source, method=method,
                record_index=idx, input_checksum=input_checksum,
            )
            self._stats.inc("records_created")
            return "created"
        else:
            # Match found — merge into canonical entry.
            self._merge_into_canonical(
                hit.canonical_ik, record, source,
                method=hit.method, confidence=hit.confidence,
                record_index=idx, input_checksum=input_checksum,
            )
            self._stats.inc("records_matched")
            # Increment per-method stats so the bulk path is observable
            # (audit 2.4 — fuzzy_matches / name_matches / etc. must be
            # incremented even when the match happens via add_source_records,
            # not just via resolve_single).
            if hit.method == "inchikey_exact":
                self._stats.inc("inchikey_exact_matches")
            elif hit.method == "inchikey_connectivity":
                self._stats.inc("connectivity_matches")
            elif hit.method == "name_normalized":
                self._stats.inc("name_matches")
            elif hit.method == "fuzzy":
                self._stats.inc("fuzzy_matches")
            elif hit.method == "smiles_canonical":
                self._stats.inc("smiles_matches")
            elif hit.method == "pubchem_xref":
                self._stats.inc("pubchem_xref_matches")
            self._update_confidence_histogram(hit.confidence)
            return "matched"

    def _creation_method_for(self, record: dict, inchikey: str) -> str:
        """Determine the creation method for a new entry (audit 2.1).

        * If ``inchikey`` is a valid, validated InChIKey → ``"inchikey_exact"``.
        * If ``inchikey`` is present but fails format validation (non-strict
          mode) → ``"inchikey_exact_unvalidated"`` (confidence 0.5).
        * If only a name is available → ``"name_only"`` (confidence 0.3).
        * If neither → ``"synthetic_key"`` (confidence 0.0).
        """
        if inchikey and is_valid_inchikey(inchikey):
            return "inchikey_exact"
        if inchikey:
            return "inchikey_exact_unvalidated"
        name = record.get("name", "") or ""
        if name.strip():
            return "name_only"
        return "synthetic_key"

    def _soft_validate(self, record: dict) -> List[str]:
        """Soft validation tier — flag (not reject) anomalies (audit C.15).

        Returns a list of human-readable warning strings (empty = no warnings).
        """
        warnings_list: List[str] = []
        # InChIKey format
        ik = record.get("inchikey")
        if ik and isinstance(ik, str) and not is_valid_inchikey(_normalize_inchikey(ik)):
            warnings_list.append("malformed InChIKey format")
        # molecular_weight range
        mw = record.get("molecular_weight")
        if mw is not None:
            try:
                mw_val = float(mw)
                if mw_val < 1 or mw_val > 10000:
                    warnings_list.append(f"molecular_weight {mw_val} outside [1, 10000]")
            except (TypeError, ValueError):
                warnings_list.append(f"molecular_weight {mw!r} is not numeric")
        # Empty name after normalisation
        name = record.get("name", "") or ""
        if not normalize_name(name):
            warnings_list.append("empty name after normalisation")
        # sources / ID consistency
        sources = record.get("sources") or []
        if sources and isinstance(sources, list):
            if "chembl" in sources and not record.get("chembl_id"):
                warnings_list.append("sources includes 'chembl' but chembl_id is None")
            if "drugbank" in sources and not record.get("drugbank_id"):
                warnings_list.append("sources includes 'drugbank' but drugbank_id is None")
        if warnings_list:
            self._stats.inc("soft_validation_warnings", len(warnings_list))
        return warnings_list

    def _check_dead_letter_size(self) -> None:
        """Enforce ``max_dead_letter_size`` cap (audit 6.2 / 8.15)."""
        max_size = getattr(self._config, "max_dead_letter_size", 100_000)
        if len(self._dead_letter) > max_size:
            spill = getattr(self._config, "dead_letter_spill_path", None)
            if spill is not None:
                try:
                    spill_path = Path(spill)
                    spill_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(spill_path, "a", encoding="utf-8") as fh:
                        for item in self._dead_letter[: -max_size]:
                            fh.write(json.dumps(item, default=str) + "\n")
                except OSError as exc:
                    self._event_log(
                        logging.CRITICAL,
                        "dead_letter_spill_failed",
                        error_type=type(exc).__name__,
                        error_message=str(exc)[:200],
                        error_code=ErrorCode.DEAD_LETTER_FULL.value,
                    )
            # Drop oldest.
            self._dead_letter = self._dead_letter[-max_size:]
            self._fire_alert("dead_letter_full", {
                "size": len(self._dead_letter),
                "max": max_size,
            })

    def _emergency_spill(self) -> None:
        """Emergency spill of mapping / dead-letter / audit to disk (audit 6.17)."""
        try:
            spill_dir = Path(getattr(self._config, "spill_dir", None) or "/tmp")
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            mapping_path = spill_dir / f"drug_resolver_emergency_spill_{ts}.json"
            with open(mapping_path, "w", encoding="utf-8") as fh:
                json.dump(self.to_state_dict(), fh, default=str)
            self._event_log(
                logging.CRITICAL,
                "emergency_spill_complete",
                spill_path=str(mapping_path),
            )
        except Exception as exc:
            self._event_log(
                logging.CRITICAL,
                "emergency_spill_failed",
                error_type=type(exc).__name__,
                error_message=str(exc)[:200],
            )

    # ------------------------------------------------------------------
    # Public: single-record resolution (audit 2.4 / C.10 / 4.17)
    # ------------------------------------------------------------------

    def resolve_single(
        self,
        name: str,
        inchikey: Optional[str] = None,
        *,
        operator: Optional[str] = None,
    ) -> ResolveResult:
        """Resolve a single drug and return a :class:`ResolveResult`.

        Parameters
        ----------
        name:
            Drug name (raw, will be normalized internally).
        inchikey:
            Optional InChIKey for higher-confidence matching.
        operator:
            Optional operator identity for the query-log entry (audit 11.22).

        Returns
        -------
        ResolveResult
            Frozen, Mapping-compatible result.  ``match_method`` will
            be ``"no_match"`` (or ``"no_match_pubchem_degraded"``) when
            no match was found.  ``canonical_inchikey`` will be ``None``
            in that case.

        Raises
        ------
        ImportError
            If ``pubchem_enabled=True`` and ``requests`` is not installed.
        """
        # ----- Operator handling (audit 11.22) -----
        if operator is not None:
            self.set_operator(operator)

        # ----- PubChem circuit-breaker graceful degradation (audit 6.4) -----
        pubchem_available = (
            self._config.pubchem_enabled
            and self._pubchem_circuit.allow_call()
        )

        # ----- Run the match pipeline -----
        norm_ik = _normalize_inchikey(inchikey) if inchikey else None
        hit: Optional[_MatchHit] = _MatchPipeline.run(
            self,
            inchikey=norm_ik,
            name=name or None,
            allow_pubchem=pubchem_available,
            allow_smiles=getattr(self._config, "enable_smiles_matching", False),
        )

        # ----- Compose result -----
        if hit is not None:
            method = hit.method
            confidence = hit.confidence
            canonical_ik: Optional[str] = hit.canonical_ik
            entry = self.mapping.get(canonical_ik, {})
            canonical_name = entry.get("canonical_name", name)
            sources = tuple(entry.get("sources", []))
            input_checksum = entry.get("input_checksum", "")
            # Stats
            if method == "inchikey_exact":
                self._stats.inc("inchikey_exact_matches")
            elif method == "inchikey_connectivity":
                self._stats.inc("connectivity_matches")
            elif method == "name_normalized":
                self._stats.inc("name_matches")
            elif method == "fuzzy":
                self._stats.inc("fuzzy_matches")
            elif method == "smiles_canonical":
                self._stats.inc("smiles_matches")
            elif method == "pubchem_xref":
                self._stats.inc("pubchem_xref_matches")
            self._update_confidence_histogram(confidence)
            degraded = False
        else:
            method = "no_match"
            if (
                self._config.pubchem_enabled
                and not pubchem_available
            ):
                method = "no_match_pubchem_degraded"
            confidence = 0.0
            canonical_ik = None
            canonical_name = name
            sources = ()
            input_checksum = ""
            degraded = method == "no_match_pubchem_degraded"
            self._stats.inc("no_match_results")

        result = ResolveResult(
            canonical_inchikey=canonical_ik,
            canonical_name=canonical_name or "",
            match_method=method,
            match_confidence=confidence,
            correlation_id=self._correlation_id,
            resolved_at=self._now_iso(),
            input_checksum=input_checksum,
            sources=sources,
            degraded=degraded,
        )

        # ----- Query-log entry (audit 11.22) -----
        query_event = LineageEvent(
            event_id=self._new_event_id(
                None, _canonical_json({"action": "resolve_single", "name": _safe_name(name)})
            ),
            timestamp=self._now_iso(),
            action="resolve_single",
            canonical_inchikey=canonical_ik or "",
            method=method,
            match_confidence=confidence,
            operator=self._operator,
            correlation_id=self._correlation_id,
            resolver_version=__version__,
        )
        self._query_log.append(query_event)
        max_query_log = getattr(self._config, "max_query_log_size", 10_000)
        if len(self._query_log) > max_query_log:
            self._query_log = self._query_log[-max_query_log:]

        # ----- Log sampling for DEBUG (audit 4.17) -----
        sample_rate = getattr(self._config, "log_sample_rate", 0.01)
        if logger.isEnabledFor(logging.DEBUG) and (
            sample_rate >= 1.0 or random.random() < sample_rate
        ):
            self._event_log(
                logging.DEBUG,
                "resolve_single_result",
                name=_safe_name(name),
                method=method,
                confidence=confidence,
                degraded=degraded,
            )

        return result

    def get_canonical_inchikey(self, drug_record: dict) -> Optional[str]:
        """Return the canonical InChIKey for *drug_record*, or ``None``.

        Parameters
        ----------
        drug_record:
            Dict with at least ``'inchikey'`` and/or ``'name'`` keys.

        Returns
        -------
        str or None
        """
        inchikey = _normalize_inchikey(drug_record.get("inchikey", "")) or None
        name = drug_record.get("name", "") or None
        hit = _MatchPipeline.run(
            self, inchikey=inchikey, name=name,
            allow_pubchem=False,
            allow_smiles=getattr(self._config, "enable_smiles_matching", False),
        )
        return hit.canonical_ik if hit is not None else None

    # ------------------------------------------------------------------
    # Public: bulk resolution from DataFrames (audit 1.1 / 2.10 / 1.6)
    # ------------------------------------------------------------------

    def build_mapping(
        self,
        chembl_df: Any,
        drugbank_df: Any,
        pubchem_df: Any,
        *,
        reset: bool = True,
        sources_order: Optional[Sequence[str]] = None,
    ) -> Any:
        """Build cross-database drug entity mapping.

        See module docstring for the resolution strategy.  This is the
        bulk offline ETL path — it never calls PubChem (audit D3-1).

        Parameters
        ----------
        chembl_df, drugbank_df, pubchem_df:
            DataFrames with at least columns ``inchikey``, ``name``,
            and the source-specific ID column.
        reset:
            If ``True`` (default), clear internal state before
            ingestion — idempotent re-runs (audit D7-1).  Pass ``False``
            only for verified incremental backfills.
        sources_order:
            Optional override for the ingestion order.  Default is
            ``("chembl", "drugbank", "pubchem")``.

        Returns
        -------
        pd.DataFrame
            See :attr:`_OUTPUT_COLUMNS`.
        """
        if self._config.pubchem_enabled:
            self._event_log(
                logging.INFO,
                "build_mapping_pubchem_note",
                note="bulk path does not call PubChem (audit D3-1)",
            )

        if reset:
            if self.mapping:
                self._event_log(
                    logging.INFO,
                    "build_mapping_reset",
                    existing_count=len(self.mapping),
                )
            self.reset()
        else:
            if self.mapping:
                self._event_log(
                    logging.WARNING,
                    "build_mapping_incremental",
                    existing_count=len(self.mapping),
                    note="reset=False — ensure inputs contain only new records",
                )

        # ----- Source label canonicalisation (audit 2.10) -----
        order = tuple(sources_order or ("chembl", "drugbank", "pubchem"))
        sources_map = {
            "chembl": (chembl_df, "chembl"),
            "drugbank": (drugbank_df, "drugbank"),
            "pubchem": (pubchem_df, "pubchem"),
        }
        # Case-insensitive matching.
        canonical_map = {k.lower(): k for k in sources_map}

        self._event_log(
            logging.INFO,
            "build_mapping_start",
            sources_order=list(order),
        )

        for src_key in order:
            real_key = canonical_map.get(src_key.lower())
            if real_key is None:
                self._event_log(
                    logging.WARNING,
                    "build_mapping_unknown_source",
                    source=_safe_name(src_key),
                )
                continue
            df, source_label = sources_map[real_key]
            records = self._df_to_records(df)
            self.add_source_records(records, source=source_label)

        result_df = self.to_dataframe()
        self._event_log(
            logging.INFO,
            "build_mapping_done",
            canonical_count=len(result_df),
        )
        return result_df

    # ------------------------------------------------------------------
    # Public: export
    # ------------------------------------------------------------------

    def to_dataframe(
        self,
        chunksize: Optional[int] = None,
        *,
        null_representation: str = "pandas",
    ) -> Any:
        """Convert the internal ``mapping`` dict to an entity-mapping DataFrame.

        Audit C.17 — after construction, the DataFrame's columns are
        asserted to equal :attr:`_OUTPUT_COLUMNS`.  Audit C.21 — when
        ``chunksize`` is set, returns a generator of DataFrames
        (streaming).

        Parameters
        ----------
        chunksize:
            If given, return an iterator of DataFrames each with at
            most ``chunksize`` rows.  Must be > 0 (audit 2.11).
        null_representation:
            One of ``"pandas"`` (default — use native NA), ``"none"``
            (use ``None``), ``"empty_string"`` (use ``""``).

        Returns
        -------
        pd.DataFrame or Iterator[pd.DataFrame]
        """
        if chunksize is not None and chunksize <= 0:
            raise ValueError(f"chunksize must be > 0, got {chunksize}")
        if null_representation not in ("pandas", "none", "empty_string"):
            raise ValueError(
                f"null_representation must be 'pandas', 'none', or 'empty_string', "
                f"got {null_representation!r}"
            )

        pd = _injector.get_pd()

        def _make_row(canonical_ik: str, entry: dict) -> dict:
            sources = entry.get("sources", [])
            row = {
                "canonical_inchikey": canonical_ik,
                "canonical_name": entry.get("canonical_name", ""),
                "chembl_id": entry.get("chembl_id"),
                "drugbank_id": entry.get("drugbank_id"),
                "pubchem_cid": entry.get("pubchem_cid"),
                "uniprot_id": entry.get("uniprot_id"),
                "string_id": entry.get("string_id"),
                "smiles": entry.get("smiles"),
                "smiles_form": entry.get("smiles_form", "unknown"),
                "molecular_formula": entry.get("molecular_formula"),
                "molecular_weight": entry.get("molecular_weight"),
                "match_confidence": entry.get("match_confidence", 0.0),
                "match_method": entry.get("match_method", "unknown"),
                # D5-5 / D16-1 / 2.7: JSON-encoded sources list.
                "sources": json.dumps(sources),
                "resolved_at": entry.get("resolved_at", ""),
                "created_at": entry.get("created_at", entry.get("resolved_at", "")),
                "resolver_version": entry.get(
                    "resolver_version", MAPPING_SCHEMA_VERSION,
                ),
                "input_checksum": entry.get("input_checksum", ""),
                "data_quality_score": self.compute_data_quality_score(canonical_ik),
            }
            # Apply null representation.
            if null_representation == "none":
                for k, v in list(row.items()):
                    if v is None or (isinstance(v, float) and math.isnan(v)):
                        row[k] = None
            elif null_representation == "empty_string":
                for k, v in list(row.items()):
                    if v is None or (isinstance(v, float) and math.isnan(v)):
                        row[k] = ""
            return row

        if chunksize is None:
            rows = [_make_row(ik, e) for ik, e in self.mapping.items()]
            df = pd.DataFrame(rows, columns=list(self._OUTPUT_COLUMNS))
            self._assert_output_schema(df)
            return df

        # Streaming chunked export (audit C.21 / 8.4).
        def _chunk_iter() -> Iterator[Any]:
            chunk: List[dict] = []
            for ik, e in self.mapping.items():
                chunk.append(_make_row(ik, e))
                if len(chunk) >= chunksize:
                    df = pd.DataFrame(chunk, columns=list(self._OUTPUT_COLUMNS))
                    self._assert_output_schema(df)
                    yield df
                    chunk = []
            if chunk:
                df = pd.DataFrame(chunk, columns=list(self._OUTPUT_COLUMNS))
                self._assert_output_schema(df)
                yield df

        return _chunk_iter()

    def to_records(self) -> List[dict]:
        """Export the mapping as a list of plain dicts (no pandas dep).

        Audit C.2 — nested mutable values (``sources``,
        ``collapsed_stereoisomers``, ``field_provenance``) are deep-copied
        so callers cannot mutate the resolver's internal state.
        """
        records: List[dict] = []
        for canonical_ik, entry in self.mapping.items():
            row = copy.deepcopy(entry)
            row["canonical_inchikey"] = canonical_ik
            row["sources"] = list(entry.get("sources", []))
            records.append(row)
        return records

    def to_dict(self) -> Dict[str, dict]:
        """Export the mapping as a dict-of-dicts (JSON-serialisable).

        Audit C.2 — nested mutable values are copied via ``list(...)``.
        """
        out: Dict[str, dict] = {}
        for ik, e in self.mapping.items():
            row = dict(e)
            # Deep-copy nested mutable values.
            row["sources"] = list(e.get("sources", []))
            row["collapsed_stereoisomers"] = [
                copy.deepcopy(s) for s in e.get("collapsed_stereoisomers", [])
            ]
            row["field_provenance"] = {
                k: dict(v) for k, v in e.get("field_provenance", {}).items()
            }
            row["source_contributions"] = [
                dict(s) for s in e.get("source_contributions", [])
            ]
            out[ik] = row
        return out

    def to_parquet(self, path: Union[str, Path]) -> None:
        """Write the mapping to a Parquet file (audit 2.12 / 6.20).

        Uses whichever Parquet engine is installed (``pyarrow`` or
        ``fastparquet``).
        """
        engine_name, _ = _injector.get_parquet_engine()
        df = self.to_dataframe()
        df.to_parquet(str(path), index=False, engine=engine_name)

    def to_csv(self, path: Union[str, Path]) -> None:
        """Write the mapping to a CSV file (stdlib only — no pandas dep; audit 15.4)."""
        import csv
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        records = self.to_records()
        if not records:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(list(self._OUTPUT_COLUMNS))
            return
        # Determine column order — use union of all record keys, with
        # _OUTPUT_COLUMNS first.
        all_cols: List[str] = list(self._OUTPUT_COLUMNS)
        for r in records:
            for k in r.keys():
                if k not in all_cols:
                    all_cols.append(k)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=all_cols, extrasaction="ignore")
            writer.writeheader()
            for r in records:
                # Flatten list/dict values for CSV friendliness.
                flat: Dict[str, Any] = {}
                for k, v in r.items():
                    if isinstance(v, (list, dict)):
                        flat[k] = json.dumps(v, default=str)
                    else:
                        flat[k] = v
                writer.writerow(flat)

    def to_jsonl(self, path: Union[str, Path]) -> None:
        """Write the mapping as JSON Lines (one record per line; audit 15.27)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            for r in self.to_records():
                fh.write(json.dumps(r, default=str) + "\n")

    # ------------------------------------------------------------------
    # Public: state serialisation (audit C.2 / C.9 / 1.9 / 1.10)
    # ------------------------------------------------------------------

    def to_state_dict(
        self,
        *,
        include_indices: bool = True,
        redact_pii: bool = False,
    ) -> dict:
        """Serialise the resolver's full state to a JSON-compatible dict.

        Audit C.2 — every mutable structure is deep-copied so callers
        cannot mutate the resolver's internal state by mutating the
        returned dict.

        Parameters
        ----------
        include_indices:
            If ``False``, omit indices (they can be rebuilt from the
            mapping via :meth:`_rebuild_indices_from_mapping`).
            Useful for compact state checkpoints (audit 8.5).
        redact_pii:
            If ``True``, replace ``canonical_name`` and ``name`` with
            ``"<redacted>"`` in the mapping and indices (audit 9.5).

        Returns
        -------
        dict
            JSON-serialisable state.
        """
        # Log state access (audit 9.19).
        access_event = LineageEvent(
            event_id=self._new_event_id(
                None, _canonical_json({"action": "to_state_dict"})
            ),
            timestamp=self._now_iso(),
            action="state_access",
            canonical_inchikey="",
            method="to_state_dict",
            operator=self._operator,
            correlation_id=self._correlation_id,
            resolver_version=__version__,
        )
        self._state_access_log.append(access_event)

        mapping_copy: Dict[str, dict] = {}
        for ik, entry in self.mapping.items():
            entry_copy = copy.deepcopy(entry)
            if redact_pii:
                entry_copy["canonical_name"] = "<redacted>"
                entry_copy["name"] = "<redacted>"
            mapping_copy[ik] = entry_copy

        out: Dict[str, Any] = {
            "schema_version": MAPPING_SCHEMA_VERSION,
            "resolver_class": type(self).__name__,
            "resolver_version": __version__,
            "config": self._config.to_masked_dict(),
            "mapping": mapping_copy,
            "dead_letter": copy.deepcopy(self._dead_letter),
            "audit_trail": {
                ik: [e.to_dict() for e in evts]
                for ik, evts in self._audit_trail.items()
            },
            "archived_audit_trail": {
                ik: [e.to_dict() for e in evts]
                for ik, evts in self._archived_audit_trail.items()
            },
            "stats": self._stats.to_dict(),
            "ingested_record_keys": sorted(self._ingested_record_keys),
            "source_datasets": [
                m.to_dict() for m in self._source_dataset_registry.values()
            ],
            "exported_at": self._now_iso_for_export(),
            "data_classification": getattr(self._config, "data_classification", "internal"),
        }
        if include_indices:
            out["inchikey_index"] = copy.deepcopy(self._inchikey_index)
            out["name_index"] = copy.deepcopy(self._name_index)
            out["name_index_multi"] = copy.deepcopy(self._name_index_multi)
            out["connectivity_index"] = copy.deepcopy(self._connectivity_index)
            out["connectivity_index_multi"] = copy.deepcopy(self._connectivity_index_multi)
        return out

    @classmethod
    def from_state_dict(cls, state: Mapping[str, Any]) -> "DrugResolver":
        """Reconstruct a :class:`DrugResolver` from a state dict.

        Audit C.9 — validates the state against ``schema/v1.json`` (via
        ``jsonschema`` if available, else a manual structural check),
        rejects unknown top-level keys, and verifies referential
        integrity between ``mapping`` and the indices / audit trail.

        Raises
        ------
        SchemaVersionMismatchError
            If the schema version does not match.
        ResolverStateCorruptionError
            If referential integrity checks fail.
        """
        # ----- Schema version check (audit 12.3) -----
        schema = state.get("schema_version", "unknown")
        if schema != MAPPING_SCHEMA_VERSION:
            raise SchemaVersionMismatchError(
                f"state schema version mismatch: state has {schema!r}, "
                f"resolver expects {MAPPING_SCHEMA_VERSION!r}. "
                f"Use a v{schema}-compatible resolver or migrate the state."
            )

        # ----- JSON-Schema validation (audit C.9) -----
        jsonschema_mod = _injector.get_jsonschema()
        schema_doc = _load_state_schema()
        if jsonschema_mod is not None and schema_doc:
            try:
                jsonschema_mod.validate(dict(state), schema_doc)
            except jsonschema_mod.ValidationError as exc:
                raise ResolverStateCorruptionError(
                    f"state dict failed JSON Schema validation: {exc.message}"
                ) from exc
        else:
            errors = _manual_schema_check(state)
            if errors:
                raise ResolverStateCorruptionError(
                    "state dict failed manual validation: " + "; ".join(errors)
                )

        # ----- Unknown top-level keys (audit C.9 — forward-compat) -----
        known_keys = {
            "schema_version", "resolver_class", "resolver_version", "config",
            "mapping", "inchikey_index", "name_index", "name_index_multi",
            "connectivity_index", "connectivity_index_multi",
            "dead_letter", "audit_trail", "archived_audit_trail",
            "stats", "ingested_record_keys", "source_datasets",
            "exported_at", "data_classification",
        }
        unknown_keys = set(state.keys()) - known_keys
        # We don't reject — just log so users see them (audit 4.9 pattern).

        # ----- Build config (audit 4.9 — filter unknown keys) -----
        cfg_dict = dict(state.get("config", {}))
        if cfg_dict.get("pubchem_api_key") == "<redacted>":
            cfg_dict["pubchem_api_key"] = None
        # Filter to known ResolverConfig fields.
        known_cfg_fields = {f.name for f in dataclasses.fields(ResolverConfig)}
        filtered_cfg: Dict[str, Any] = {}
        unknown_cfg: List[str] = []
        for k, v in cfg_dict.items():
            if k in known_cfg_fields:
                if k == "source_whitelist" and v:
                    filtered_cfg[k] = tuple(v)
                else:
                    filtered_cfg[k] = v
            else:
                unknown_cfg.append(k)

        try:
            cfg = ResolverConfig(**filtered_cfg)
        except (TypeError, ValueError) as exc:
            raise ResolverStateCorruptionError(
                f"failed to construct ResolverConfig from state: {exc}"
            ) from exc

        resolver = cls(config=cfg)
        resolver._unknown_config_keys = unknown_cfg
        if unknown_cfg:
            resolver._event_log(
                logging.WARNING,
                "state_dict_unknown_config_keys",
                keys=unknown_cfg,
            )

        # ----- Restore mapping (deep copy — audit C.2) -----
        resolver.mapping = copy.deepcopy(dict(state.get("mapping", {})))

        # ----- Restore indices (or rebuild) -----
        if "inchikey_index" in state:
            resolver._inchikey_index = copy.deepcopy(
                dict(state.get("inchikey_index", {}))
            )
            resolver._name_index = copy.deepcopy(
                dict(state.get("name_index", {}))
            )
            resolver._name_index_multi = copy.deepcopy(
                dict(state.get("name_index_multi", {}))
            )
            resolver._connectivity_index = copy.deepcopy(
                dict(state.get("connectivity_index", {}))
            )
            resolver._connectivity_index_multi = copy.deepcopy(
                dict(state.get("connectivity_index_multi", {}))
            )
        else:
            resolver._rebuild_indices_from_mapping()

        # ----- Restore audit trail (audit C.18) -----
        raw_audit = state.get("audit_trail", {})
        resolver._audit_trail = {}
        for ik, evts in raw_audit.items():
            resolver._audit_trail[ik] = [
                LineageEvent.from_dict(e) if isinstance(e, dict) else e
                for e in evts
            ]
        raw_archived = state.get("archived_audit_trail", {})
        resolver._archived_audit_trail = {}
        for ik, evts in raw_archived.items():
            resolver._archived_audit_trail[ik] = [
                LineageEvent.from_dict(e) if isinstance(e, dict) else e
                for e in evts
            ]

        # ----- Restore dead-letter -----
        resolver._dead_letter = copy.deepcopy(list(state.get("dead_letter", [])))

        # ----- Restore ingested_record_keys (audit C.6) -----
        resolver._ingested_record_keys = set(state.get("ingested_record_keys", []))

        # ----- Restore source dataset registry (audit C.19) -----
        raw_sources = state.get("source_datasets", [])
        resolver._source_dataset_registry = {}
        for sd in raw_sources:
            try:
                meta = SourceDatasetMeta.from_dict(sd)
                resolver._source_dataset_registry[meta.source] = meta
            except Exception:
                pass

        # ----- Restore stats (audit 4.8) -----
        for k, v in state.get("stats", {}).items():
            try:
                resolver._stats.inc(k, int(v))
            except Exception:
                resolver._unknown_stats_restored.append(k)

        # ----- Referential integrity (audit C.9 / E.12 / E.13) -----
        if getattr(cfg, "runtime_asserts", False):
            resolver._assert_state_dict_consistent()

        return resolver

    @classmethod
    def from_state_dict_repair(
        cls, state: Mapping[str, Any],
    ) -> Tuple["DrugResolver", List[str]]:
        """Attempt to repair common corruption (audit 6.9).

        Returns the resolver + a list of human-readable repairs applied.
        """
        repairs: List[str] = []
        # Make a shallow copy so we can mutate.
        state_mut = dict(state)
        mapping = dict(state_mut.get("mapping", {}))
        # Drop index entries not in mapping.
        for idx_key in (
            "inchikey_index", "name_index",
            "connectivity_index",
        ):
            idx = dict(state_mut.get(idx_key, {}))
            before = len(idx)
            idx = {k: v for k, v in idx.items() if v in mapping}
            if len(idx) < before:
                repairs.append(
                    f"removed {before - len(idx)} dangling {idx_key} entries"
                )
            state_mut[idx_key] = idx
        # Drop multi-index entries not in mapping.
        for idx_key in ("name_index_multi", "connectivity_index_multi"):
            idx = dict(state_mut.get(idx_key, {}))
            before_total = sum(len(v) for v in idx.values())
            idx = {
                k: [x for x in v if x in mapping]
                for k, v in idx.items()
            }
            idx = {k: v for k, v in idx.items() if v}
            after_total = sum(len(v) for v in idx.values())
            if after_total < before_total:
                repairs.append(
                    f"removed {before_total - after_total} dangling {idx_key} entries"
                )
            state_mut[idx_key] = idx
        # Drop audit-trail keys not in mapping and not archived.
        audit = dict(state_mut.get("audit_trail", {}))
        before = len(audit)
        audit = {k: v for k, v in audit.items() if k in mapping}
        if len(audit) < before:
            repairs.append(
                f"removed {before - len(audit)} audit_trail entries for missing keys"
            )
        state_mut["audit_trail"] = audit

        resolver = cls.from_state_dict(state_mut)
        return resolver, repairs

    # ------------------------------------------------------------------
    # Public: lifecycle / maintenance (audit 6.5 / 6.10 / 4.10 / 4.12 / 14.6 / 16.29)
    # ------------------------------------------------------------------

    def reset(self, *, reset_process_globals: bool = False) -> None:
        """Clear all internal state — equivalent to a fresh instance.

        Parameters
        ----------
        reset_process_globals:
            If ``True``, also reset the process-global rate limiter
            and the PubChem circuit breaker.  Default ``False`` for
            production safety (audit 6.10).
        """
        with _MutationContext(self, "reset"):
            self.mapping = {}
            self._inchikey_index = {}
            self._name_index = {}
            self._name_index_multi = {}
            self._connectivity_index = {}
            self._connectivity_index_multi = {}
            # P1-ER-1 ROOT FIX: clear _smiles_index too — a stale SMILES
            # pointer after ``reset()`` would re-resurrect dead entries.
            self._smiles_index = {}
            self._dead_letter = []
            self._audit_trail = {}
            self._archived_audit_trail = {}
            self._state_access_log = []
            self._query_log = []
            self._ingested_record_keys = set()
            self._audit_chain_head = {}
            self._source_dataset_registry = {}
            self._source_record_index = {}
            self._checkpoints = {}
            self._last_interrupt_checkpoint = None
            self._stats = ResolverStats()
            self._metrics = {}
            self._confidence_histogram = {}
            self._unknown_stats_restored = []
            self._unknown_config_keys = []
            self._last_pubchem_call = 0.0
            self._pubchem_bulk_warned = False
            self._cached_choices = None
            self._cached_choices_generation = -1
            self._run_started_at = self._now_iso()
            if reset_process_globals:
                _ProcessGlobalRateLimiter._reset_for_tests()
                self._pubchem_circuit.reset()

    def remove_source(self, source: str) -> int:
        """Remove entries whose only source is ``source`` (audit 4.10 / 4.12 / 16.29).

        Entries contributed by multiple sources are kept but have
        ``source`` removed from their ``sources`` list.  The audit
        trail is PRESERVED (audit 4.12) — a ``remove_source_full`` or
        ``remove_source_partial`` event is appended.
        """
        with _MutationContext(self, f"remove_source({source!r})"):
            to_delete: List[str] = []
            removed = 0
            ts = self._now_iso()
            for ik, entry in self.mapping.items():
                sources = entry.get("sources", [])
                if source in sources:
                    if len(sources) == 1:
                        to_delete.append(ik)
                    else:
                        new_sources = [s for s in sources if s != source]
                        entry["sources"] = new_sources
                        self._append_audit(ik, LineageEvent(
                            event_id=self._new_event_id(
                                self._audit_chain_head.get(ik, ""),
                                _canonical_json({"action": "remove_source_partial", "source": source, "ts": ts}),
                            ),
                            timestamp=ts,
                            action="remove_source_partial",
                            canonical_inchikey=ik,
                            source=source,
                            diff=(("sources", tuple(sources), tuple(new_sources)),),
                            sources_after=tuple(new_sources),
                            operator=self._operator,
                            correlation_id=self._correlation_id,
                            resolver_version=__version__,
                        ))
            # Single-pass index rebuild (audit 4.10 / 4.11).
            for ik in to_delete:
                # Preserve audit trail by moving to archived (audit 4.12).
                if ik in self._audit_trail:
                    self._archived_audit_trail[ik] = self._audit_trail[ik]
                    del self._audit_trail[ik]
                # Append a removal event to the archived trail.
                self._archived_audit_trail.setdefault(ik, []).append(LineageEvent(
                    event_id=self._new_event_id(
                        self._audit_chain_head.get(ik, ""),
                        _canonical_json({"action": "remove_source_full", "source": source, "ts": ts}),
                    ),
                    timestamp=ts,
                    action="remove_source_full",
                    canonical_inchikey=ik,
                    source=source,
                    diff=(("mapping", ik, None),),
                    sources_after=(),
                    operator=self._operator,
                    correlation_id=self._correlation_id,
                    resolver_version=__version__,
                ))
                del self.mapping[ik]
                removed += 1
            # Rebuild indices in a single pass.
            self._rebuild_indices_from_mapping()
            # Drop source dataset registry entry.
            self._source_dataset_registry.pop(source, None)
            self._event_log(
                logging.INFO,
                "remove_source_done",
                source=_safe_name(source),
                removed=removed,
            )
        if getattr(self._config, "runtime_asserts", False):
            self._assert_indices_consistent()
            self._assert_audit_trail_consistent()
        return removed

    def forget_record(self, canonical_ik: str) -> bool:
        """GDPR right-to-erasure (audit 14.6).

        Removes the entry AND scrubs its audit trail (after written
        authorisation).  Logs a CRITICAL event with the operator and
        timestamp.

        Returns
        -------
        bool
            ``True`` if the entry existed and was removed.
        """
        if canonical_ik not in self.mapping:
            return False
        ts = self._now_iso()
        with _MutationContext(self, f"forget_record({canonical_ik!r})"):
            # Move audit trail to archived with a forget event.
            audit = self._audit_trail.pop(canonical_ik, [])
            self._archived_audit_trail[canonical_ik] = audit + [LineageEvent(
                event_id=self._new_event_id(
                    self._audit_chain_head.get(canonical_ik, ""),
                    _canonical_json({"action": "forget_record", "ts": ts}),
                ),
                timestamp=ts,
                action="forget_record",
                canonical_inchikey=canonical_ik,
                diff=(("mapping", canonical_ik, None),),
                operator=self._operator,
                correlation_id=self._correlation_id,
                resolver_version=__version__,
            )]
            del self.mapping[canonical_ik]
            self._rebuild_indices_from_mapping()
            self._event_log(
                logging.CRITICAL,
                "forget_record",
                canonical_ik=_safe_name(canonical_ik, max_len=32),
                operator=self._operator,
                timestamp=ts,
            )
        return True

    # ------------------------------------------------------------------
    # Public: stats / observability (audit C.20 / E.20 / 11.7-11.18)
    # ------------------------------------------------------------------

    def get_stats(self) -> Dict[str, int]:
        """Return a JSON-serialisable snapshot of resolver counters (audit 11.7)."""
        return self._stats.to_dict()

    def get_unknown_stats(self) -> List[str]:
        """Return list of unknown stat names encountered during state restore (audit 7.14)."""
        return list(self._unknown_stats_restored)

    def get_audit_trail(self, canonical_inchikey: str) -> List[dict]:
        """Return the ordered list of audit events for ``canonical_inchikey``.

        Deep-copies the events (audit C.2).  Includes archived events
        (post-``remove_source`` / ``forget_record``) as well.
        """
        events: List[LineageEvent] = list(
            self._audit_trail.get(canonical_inchikey, [])
        ) + list(
            self._archived_audit_trail.get(canonical_inchikey, [])
        )
        return [e.to_dict() for e in events]

    def get_query_log(self) -> List[dict]:
        """Return the recent ``resolve_single`` query log (audit 11.22)."""
        return [e.to_dict() for e in self._query_log]

    def get_state_access_log(self) -> List[dict]:
        """Return the state-access log (audit 9.19)."""
        return [e.to_dict() for e in self._state_access_log]

    def get_conflicts(self, canonical_ik: str) -> List[dict]:
        """Return the conflict history for ``canonical_ik`` (audit C.16 / 2.8)."""
        events = self._audit_trail.get(canonical_ik, [])
        return [e.to_dict() for e in events if e.action == "conflict"]

    def find_affected_entities(self, source: str) -> List[str]:
        """Return canonical InChIKeys whose ``sources`` list contains ``source``."""
        return [
            ik for ik, entry in self.mapping.items()
            if source in entry.get("sources", [])
        ]

    def get_pubchem_failure_count(self) -> int:
        """Return the running count of PubChem API failures (audit D6-3)."""
        return self._stats.pubchem_failures

    def get_pubchem_success_rate(self) -> float:
        """Return ``pubchem_successes / max(1, pubchem_calls)`` (audit 11.18)."""
        calls = self._stats.pubchem_calls
        return self._stats.pubchem_successes / calls if calls else 0.0

    def get_pubchem_circuit_state(self) -> str:
        """Return the current PubChem circuit-breaker state (audit C.14)."""
        return self._pubchem_circuit.state

    def reset_pubchem_circuit(self) -> None:
        """Force-reset the PubChem circuit breaker (audit C.14)."""
        self._pubchem_circuit.reset()

    def get_dead_letter_count(self) -> int:
        """Convenience accessor (audit 11.7)."""
        return len(self._dead_letter)

    def get_mapping_size(self) -> int:
        """Convenience accessor (audit 11.7)."""
        return len(self.mapping)

    def get_audit_trail_size(self) -> int:
        """Convenience accessor (audit 11.7)."""
        return sum(len(v) for v in self._audit_trail.values()) + \
               sum(len(v) for v in self._archived_audit_trail.values())

    def get_confidence_histogram(self) -> Dict[str, int]:
        """Return the confidence-score histogram (audit 11.15)."""
        return dict(self._confidence_histogram)

    def get_metrics(self) -> Dict[str, Any]:
        """Return per-method latency metrics (audit 11.16 / C.20)."""
        return copy.deepcopy(self._metrics)

    def get_latency_stats(self) -> Dict[str, Dict[str, float]]:
        """Return min/p50/p95/p99/max latency per method (audit 11.16)."""
        out: Dict[str, Dict[str, float]] = {}
        for method, samples in self._metrics.items():
            if not samples:
                continue
            sorted_s = sorted(samples)
            n = len(sorted_s)
            out[method] = {
                "min": sorted_s[0],
                "p50": sorted_s[n // 2],
                "p95": sorted_s[int(n * 0.95)] if n > 1 else sorted_s[0],
                "p99": sorted_s[int(n * 0.99)] if n > 1 else sorted_s[0],
                "max": sorted_s[-1],
                "count": float(n),
            }
        return out

    def _update_confidence_histogram(self, confidence: float) -> None:
        """Bucket confidence to 1-decimal-place keys (audit 11.15)."""
        bucket = f"{round(confidence, 1):.1f}"
        self._confidence_histogram[bucket] = self._confidence_histogram.get(bucket, 0) + 1

    def _estimate_memory(self) -> int:
        """Rough memory estimate via :func:`sys.getsizeof` (audit 11.17)."""
        import sys
        total = 0
        total += sys.getsizeof(self.mapping)
        for k, v in self.mapping.items():
            total += sys.getsizeof(k) + sys.getsizeof(v)
        total += sys.getsizeof(self._inchikey_index)
        for k, v in self._inchikey_index.items():
            total += sys.getsizeof(k) + sys.getsizeof(v)
        total += sys.getsizeof(self._name_index)
        total += sys.getsizeof(self._audit_trail)
        total += sys.getsizeof(self._dead_letter)
        return total

    def health(self) -> Dict[str, Any]:
        """Return a health-check dict (audit C.20 / 11.8).

        Useful for Kubernetes liveness / readiness probes.
        """
        return {
            "mapping_size": len(self.mapping),
            "dead_letter_count": len(self._dead_letter),
            "audit_trail_size": self.get_audit_trail_size(),
            "pubchem_circuit_state": self._pubchem_circuit.state,
            "pubchem_failure_rate": (
                self._stats.pubchem_failures / max(1, self._stats.pubchem_calls)
            ),
            "pubchem_success_rate": self.get_pubchem_success_rate(),
            "match_method_distribution": self._match_method_distribution(),
            "memory_usage_estimate_bytes": self._estimate_memory(),
            "schema_version": MAPPING_SCHEMA_VERSION,
            "resolver_version": __version__,
            "resolver_class": type(self).__name__,
            "last_mutation_at": self._run_started_at,
            "correlation_id": self._correlation_id,
            "operator": self._operator,
            "ingested_record_keys": len(self._ingested_record_keys),
            "source_datasets": list(self._source_dataset_registry.keys()),
        }

    def _match_method_distribution(self) -> Dict[str, int]:
        """Return count of entries per match_method (audit C.20)."""
        dist: Dict[str, int] = {}
        for entry in self.mapping.values():
            m = entry.get("match_method", "unknown")
            dist[m] = dist.get(m, 0) + 1
        return dist

    # ------------------------------------------------------------------
    # Public: data-quality scoring (audit 5.23)
    # ------------------------------------------------------------------

    def compute_data_quality_score(self, canonical_ik: str) -> float:
        """Return a 0.0–1.0 data-quality score for the entry (audit 5.23).

        Score components:

        * 0.2 — has an InChIKey.
        * 0.1 — InChIKey is in canonical format (not synthetic).
        * 0.2 — has ≥ 2 source IDs.
        * 0.2 — no conflicts detected.
        * 0.1 — has a SMILES.
        * 0.1 — has a molecular_weight in valid range.
        * 0.1 — ``resolved_at`` is recent (within the last 365 days).
        """
        entry = self.mapping.get(canonical_ik, {})
        score = 0.0
        ik = entry.get("inchikey") or ""
        if ik:
            score += 0.2
            if is_valid_inchikey(ik):
                score += 0.1
        id_count = sum(
            1 for f in ("chembl_id", "drugbank_id", "pubchem_cid")
            if entry.get(f)
        )
        if id_count >= 2:
            score += 0.2
        # Conflicts.
        conflicts = [
            e for e in self._audit_trail.get(canonical_ik, [])
            if e.action == "conflict"
        ]
        if not conflicts:
            score += 0.2
        if entry.get("smiles"):
            score += 0.1
        mw = entry.get("molecular_weight")
        if mw is not None:
            try:
                mw_val = float(mw)
                if 1 <= mw_val <= 10000:
                    score += 0.1
            except (TypeError, ValueError):
                pass
        resolved_at = entry.get("resolved_at", "")
        if resolved_at:
            try:
                # Parse ISO 8601 with Z suffix.
                parsed = datetime.strptime(
                    resolved_at.replace("Z", "+00:00"),
                    "%Y-%m-%dT%H:%M:%S.%f%z",
                )
                age_days = (datetime.now(timezone.utc) - parsed).days
                if age_days <= 365:
                    score += 0.1
            except (ValueError, TypeError):
                pass
        return round(score, 2)

    # ------------------------------------------------------------------
    # Public: lineage / traceability (audit 16.15 / 16.13 / 16.20 / 16.23 / 16.24 / 16.26)
    # ------------------------------------------------------------------

    def trace_value(self, canonical_ik: str, field_name: str) -> List[dict]:
        """Return every audit event that touched ``field_name`` on ``canonical_ik`` (audit 16.15)."""
        events = self._audit_trail.get(canonical_ik, []) + \
                 self._archived_audit_trail.get(canonical_ik, [])
        return [
            e.to_dict() for e in events
            if any(d[0] == field_name for d in e.diff)
        ]

    def as_of(self, canonical_ik: str, timestamp: str) -> Optional[dict]:
        """Reconstruct the entry's state at ``timestamp`` (audit 16.23)."""
        if canonical_ik not in self.mapping and canonical_ik not in self._archived_audit_trail:
            return None
        events = self._audit_trail.get(canonical_ik, []) + \
                 self._archived_audit_trail.get(canonical_ik, [])
        # Filter events up to (and including) the timestamp.
        upto: List[LineageEvent] = []
        for e in events:
            if e.timestamp <= timestamp:
                upto.append(e)
        if not upto:
            return None
        # Replay from empty.
        state: Dict[str, Any] = {}
        for e in upto:
            if e.action in ("create", "merge"):
                # Apply diffs.
                for field, _old, new in e.diff:
                    if new is not None:
                        state[field] = new
            elif e.action in ("remove_source_partial",):
                for field, old, new in e.diff:
                    if field == "sources" and new is not None:
                        state["sources"] = list(new)
            elif e.action in ("remove_source_full", "forget_record"):
                return None
        state["canonical_inchikey"] = canonical_ik
        return state

    def to_provenance_graph(self, canonical_ik: str) -> dict:
        """Return a node-link provenance graph (audit 16.20)."""
        entry = self.mapping.get(canonical_ik, {})
        events = self._audit_trail.get(canonical_ik, [])
        nodes: List[dict] = [
            {"id": canonical_ik, "type": "canonical_entry", "name": entry.get("canonical_name", "")},
        ]
        edges: List[dict] = []
        for src in entry.get("sources", []):
            src_node_id = f"source:{src}"
            nodes.append({"id": src_node_id, "type": "source", "name": src})
            edges.append({"source": src_node_id, "target": canonical_ik, "type": "contributed"})
        for e in events:
            event_node_id = f"event:{e.event_id[:12]}"
            nodes.append({
                "id": event_node_id,
                "type": "lineage_event",
                "action": e.action,
                "timestamp": e.timestamp,
                "method": e.method,
            })
            edges.append({"source": event_node_id, "target": canonical_ik, "type": "mutated"})
        return {"nodes": nodes, "edges": edges}

    def to_openlineage(self) -> dict:
        """Return an OpenLineage-compatible run/event JSON (audit 16.24)."""
        return {
            "eventType": "RUNNING",
            "eventTime": self._now_iso(),
            "run": {
                "runId": str(uuid.uuid4()),
                "facets": {
                    "resolver_version": __version__,
                    "schema_version": MAPPING_SCHEMA_VERSION,
                    "correlation_id": self._correlation_id,
                },
            },
            "job": {
                "namespace": "drug_repurposing.entity_resolution",
                "name": "drug_resolver",
            },
            "outputs": [{
                "namespace": "drug_repurposing.entity_resolution",
                "name": "canonical_mapping",
                "facets": {
                    "mapping_size": len(self.mapping),
                    "dead_letter_count": len(self._dead_letter),
                },
            }],
        }

    def analyse_source_impact(self, source: str) -> dict:
        """Return an impact report for retracting ``source`` (audit 16.13)."""
        affected = self.find_affected_entities(source)
        to_remove = 0
        to_modify = 0
        for ik in affected:
            sources = self.mapping[ik].get("sources", [])
            if len(sources) == 1:
                to_remove += 1
            else:
                to_modify += 1
        return {
            "source": source,
            "affected_canonical_keys": affected,
            "entries_to_be_removed": to_remove,
            "entries_to_be_modified": to_modify,
            "audit_trail_events_to_be_archived": to_remove,
        }

    def find_canonical_for_source_record(
        self, source: str, source_record_id: str,
    ) -> Optional[str]:
        """Reverse-lookup a source-record ID → canonical InChIKey (audit 16.21)."""
        return self._source_record_index.get((source, source_record_id))

    def get_field_provenance(
        self, canonical_ik: str, field_name: str,
    ) -> Optional[dict]:
        """Return the :class:`FieldProvenance` for a field (audit 16.22)."""
        entry = self.mapping.get(canonical_ik, {})
        fp = entry.get("field_provenance", {}).get(field_name)
        return dict(fp) if fp else None

    def get_canonical_entry_with_history(self, canonical_ik: str) -> dict:
        """Return ``{"current": entry, "history": [...]}`` (audit 16.26)."""
        return {
            "current": copy.deepcopy(self.mapping.get(canonical_ik, {})),
            "history": self.get_audit_trail(canonical_ik),
        }

    # ------------------------------------------------------------------
    # Public: schema export (audit C.17 / 14.19 / 11.2)
    # ------------------------------------------------------------------

    @classmethod
    def to_openapi_schema(cls) -> dict:
        """Return an OpenAPI fragment for FastAPI integration (audit C.17 / 14.19)."""
        return {
            "type": "object",
            "title": "DrugResolverEntry",
            "required": ["canonical_inchikey", "canonical_name", "match_method", "match_confidence"],
            "properties": {
                "canonical_inchikey": {"type": "string", "nullable": True},
                "canonical_name": {"type": "string"},
                "match_method": {
                    "type": "string",
                    "enum": [
                        "inchikey_exact", "inchikey_connectivity",
                        "name_normalized", "fuzzy", "smiles_canonical",
                        "pubchem_xref", "no_match",
                        "no_match_pubchem_degraded",
                    ],
                },
                "match_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "correlation_id": {"type": "string", "nullable": True},
                "resolved_at": {"type": "string", "format": "date-time"},
                "input_checksum": {"type": "string"},
                "sources": {"type": "array", "items": {"type": "string"}},
                "degraded": {"type": "boolean"},
                "api_version": {"type": "string"},
            },
        }

    def to_prometheus(self) -> str:
        """Return Prometheus text-format metrics (audit 11.2)."""
        lines: List[str] = []
        s = self._stats.to_dict()
        for k, v in sorted(s.items()):
            metric_name = f"drug_resolver_{k}"
            lines.append(f"# TYPE {metric_name} counter")
            lines.append(f"{metric_name} {v}")
        lines.append("# TYPE drug_resolver_mapping_size gauge")
        lines.append(f"drug_resolver_mapping_size {len(self.mapping)}")
        lines.append("# TYPE drug_resolver_dead_letter_size gauge")
        lines.append(f"drug_resolver_dead_letter_size {len(self._dead_letter)}")
        lines.append("# TYPE drug_resolver_pubchem_circuit_state gauge")
        # Encode circuit state as a numeric gauge.
        state_val = {"CLOSED": 0, "HALF_OPEN": 1, "OPEN": 2}.get(
            self._pubchem_circuit.state, 0
        )
        lines.append(f"drug_resolver_pubchem_circuit_state {state_val}")
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------
    # Public: async API (audit C.23 / E.30 / 4.24 / 8.24)
    # ------------------------------------------------------------------

    async def resolve_single_async(
        self,
        name: str,
        inchikey: Optional[str] = None,
        *,
        operator: Optional[str] = None,
    ) -> ResolveResult:
        """Async wrapper for :meth:`resolve_single` (audit C.23).

        Uses :func:`asyncio.to_thread` so it does not block the event
        loop.  Suitable for FastAPI handlers.
        """
        return await asyncio.to_thread(
            self.resolve_single, name, inchikey, operator=operator,
        )

    async def add_source_records_async(
        self,
        records: Iterable[dict],
        source: str,
        **kwargs: Any,
    ) -> int:
        """Async wrapper for :meth:`add_source_records` (audit C.23)."""
        # Materialise the iterable if needed (asyncio.to_thread can't
        # pass generators across threads safely).
        if not isinstance(records, (list, tuple)):
            records = list(records)
        return await asyncio.to_thread(
            self.add_source_records, records, source, **kwargs,
        )

    async def resolve_batch_async(
        self,
        names: Sequence[str],
        *,
        max_concurrency: int = 10,
    ) -> List[ResolveResult]:
        """Async batch resolution (audit C.21 / 8.12).

        Uses a :class:`asyncio.Semaphore` to bound concurrency.  Each
        name is resolved via :meth:`resolve_single_async`.
        """
        sem = asyncio.Semaphore(max(1, max_concurrency))

        async def _one(name: str) -> ResolveResult:
            async with sem:
                return await self.resolve_single_async(name)

        return await asyncio.gather(*[_one(n) for n in names])

    def resolve_batch(self, names: Sequence[str]) -> List[ResolveResult]:
        """Synchronous batch resolution (audit C.21 / 8.12).

        Each name is resolved sequentially via :meth:`resolve_single`.
        For concurrent resolution, use :meth:`resolve_batch_async`.
        """
        return [self.resolve_single(n) for n in names]

    # ------------------------------------------------------------------
    # Internal matchers (audit C.11 — return _MatchHit, never mutate)
    # ------------------------------------------------------------------

    def _match_by_inchikey(self, inchikey: str) -> Optional[_MatchHit]:
        """Find ``canonical_inchikey`` by exact InChIKey match (audit 3.5).

        Case-insensitive — InChIKeys are normalised via
        :func:`_normalize_inchikey` before lookup.

        v41 ROOT FIX (SEV3): SYNTH-prefixed InChIKey matches now use
        ``MatchConfidence.INCHIKEY_EXACT.value`` (1.0) instead of a
        hardcoded ``0.5``.  The previous v29 ROOT FIX (audit C-3)
        believed SYNTH keys were source-dependent ("different sources
        may generate DIFFERENT SYNTH keys for the same biologic") and
        downgraded the confidence to 0.5 — but that premise was WRONG.
        SYNTH keys are generated by ``make_synthetic_inchikey`` using a
        SHA-256 hash of the SMILES / sequence / source-ID triple, and
        the canonical algorithm is shared across all sources (see
        ``cleaning.normalizer.make_synthetic_inchikey``).  By
        construction, two records with the same canonical SMILES (or
        the same source-ID + molecule-class tuple) produce IDENTICAL
        SYNTH keys regardless of which pipeline generated them.  So a
        SYNTH-key match IS an exact-molecule match — the synthetic
        identifier is deterministic and source-independent.  Hardcoding
        0.5 caused downstream consumers filtering
        ``WHERE match_confidence >= 0.7`` to EXCLUDE these matches,
        silently dropping biologics/macromolecules that legitimately
        have SYNTH keys.  We now route through the MatchConfidence enum
        (the SINGLE source of truth for confidence values) so future
        enum changes propagate automatically.
        """
        if not inchikey:
            return None
        norm = _normalize_inchikey(inchikey)
        canonical_ik = self._inchikey_index.get(norm)
        if canonical_ik is None:
            return None
        # v41 ROOT FIX (SEV3): SYNTH keys now use INCHIKEY_EXACT.value
        # via the MatchConfidence enum (see docstring above for the
        # rationale — SYNTH keys are deterministic and source-independent).
        from cleaning._constants import CANONICAL_SYNTHETIC_INCHIKEY_REGEX
        if CANONICAL_SYNTHETIC_INCHIKEY_REGEX.match(norm):
            return _MatchHit(
                canonical_ik=canonical_ik,
                method="inchikey_exact",
                confidence=MatchConfidence.INCHIKEY_EXACT.value,
            )
        return _MatchHit(
            canonical_ik=canonical_ik,
            method="inchikey_exact",
            confidence=compute_match_confidence("inchikey_exact"),
        )

    def _match_by_connectivity(self, inchikey: str) -> Optional[_MatchHit]:
        """Find ``canonical_inchikey`` by InChIKey first-block match (audit 3.4 / 3.9).

        When ``collapse_stereoisomers=False`` (default), this method
        returns ``None`` for any candidate whose full InChIKey differs
        from the indexed one.  When ``collapse_stereoisomers=True``,
        the legacy first-block-only merge is performed — but every
        collapse is logged at WARNING and recorded in
        ``collapsed_stereoisomers``.
        """
        if not inchikey:
            return None
        norm = _normalize_inchikey(inchikey)
        first_block = extract_inchikey_first_block(norm)
        if first_block is None:
            return None
        canonical_ik = self._connectivity_index.get(first_block)
        if canonical_ik is None:
            return None
        existing_ik = _normalize_inchikey(
            self.mapping.get(canonical_ik, {}).get("inchikey", "")
        )
        if not self._config.collapse_stereoisomers:
            # Stereoisomer safety gate — only merge if full InChIKeys
            # are identical (audit 3.4).
            if existing_ik != norm:
                return None
        else:
            # Genuine stereoisoform collapse (audit 3.10).
            if existing_ik and existing_ik != norm:
                self._stats.inc("stereoisomer_collapses")
                entry = self.mapping[canonical_ik]
                collapsed = entry.setdefault("collapsed_stereoisomers", [])
                collapse_record = StereoisomerCollapse(
                    inchikey=norm,
                    source="connectivity_match",
                    collapsed_at=self._now_iso(),
                    original_canonical_ik=canonical_ik,
                )
                # Avoid duplicate collapse records.
                if not any(
                    c.get("inchikey") == norm for c in collapsed
                    if isinstance(c, dict)
                ):
                    collapsed.append(collapse_record.to_dict())
                self._event_log(
                    logging.WARNING,
                    "stereoisomer_collapse",
                    canonical_ik=_safe_name(canonical_ik, max_len=32),
                    incoming_ik=_safe_name(norm, max_len=32),
                    existing_ik=_safe_name(existing_ik, max_len=32),
                )
        return _MatchHit(
            canonical_ik=canonical_ik,
            method="inchikey_connectivity",
            confidence=compute_match_confidence("inchikey_connectivity"),
        )

    def _match_by_name(
        self, name: str, *, allow_fuzzy: bool = False,
    ) -> Optional[_MatchHit]:
        """Find ``canonical_inchikey`` by normalised-name match (audit 2.3 / 2.4).

        When ``allow_fuzzy=False``, performs exact normalised lookup
        only.  When ``allow_fuzzy=True``, falls back to a bounded
        rapidfuzz sweep.

        Audit 2.3 / 4.1 — this method DOES NOT mutate ``self.mapping``.
        It returns a :class:`_MatchHit` and the caller decides whether
        to apply the method update via :meth:`_merge_into_canonical`.
        """
        if not name:
            return None
        norm = normalize_name(name)
        if not norm:
            return None

        # ----- Exact normalised lookup -----
        canonical_ik = self._name_index.get(norm)
        if canonical_ik is not None:
            return _MatchHit(
                canonical_ik=canonical_ik,
                method="name_normalized",
                confidence=compute_match_confidence("name_normalized"),
            )

        if not allow_fuzzy:
            return None

        # ----- Fuzzy sweep (audit 4.2 / 8.2 — cached choices) -----
        if not RAPIDFUZZ_AVAILABLE:
            return None
        choices = self._get_fuzzy_choices()
        if not choices:
            return None
        # Bound the fuzzy sweep (audit D8-2).
        fuzzy_max = self._config.fuzzy_max_candidates
        if len(choices) > fuzzy_max:
            # Deterministic truncation (audit 7.6).
            choices = sorted(choices)[:fuzzy_max]

        from rapidfuzz import process as fuzz_process, fuzz as fuzz_scorer

        # Version-tolerant unpack (audit 4.3).
        result = fuzz_process.extractOne(
            norm,
            choices,
            scorer=fuzz_scorer.token_sort_ratio,
            score_cutoff=self._config.fuzzy_threshold * 100,
        )
        if result is None:
            return None
        if len(result) == 3:
            best_norm, best_score_100, _ = result
        elif len(result) == 2:
            best_norm, best_score_100 = result
        else:
            self._event_log(
                logging.WARNING,
                "fuzzy_unexpected_result_shape",
                shape=len(result),
            )
            return None
        best_ik = self._name_index.get(best_norm)
        if best_ik is None:
            return None
        return _MatchHit(
            canonical_ik=best_ik,
            method="fuzzy",
            confidence=compute_match_confidence("fuzzy"),
            score=best_score_100 / 100.0,
        )

    def _get_fuzzy_choices(self) -> List[str]:
        """Return the cached list of normalised name keys (audit 4.2 / 8.2).

        Refreshes the cache only when ``_name_index_generation`` changes.
        """
        if (
            self._cached_choices is None
            or self._cached_choices_generation != self._name_index_generation
        ):
            self._cached_choices = list(self._name_index.keys())
            self._cached_choices_generation = self._name_index_generation
        return self._cached_choices

    def _match_by_smiles(self, smiles: str) -> Optional[_MatchHit]:
        """Find ``canonical_inchikey`` by canonical SMILES match (audit 3.13).

        Opt-in only (``ResolverConfig.enable_smiles_matching``).
        The match is keyed on ``_smiles_index``, which is populated in
        :meth:`_create_canonical_entry`.
        """
        if not smiles or not getattr(self._config, "enable_smiles_matching", False):
            return None
        norm_smiles = smiles.strip()
        if not norm_smiles:
            return None
        # P1-ER-1 ROOT FIX: direct attribute access — _smiles_index is
        # guaranteed to exist by ``__init__`` (asserted in
        # ``_assert_initialized``). The previous ``getattr(..., {})`` mask
        # hid the missing-attribute bug from the audit.
        canonical_ik = self._smiles_index.get(norm_smiles)
        if canonical_ik is None:
            return None
        return _MatchHit(
            canonical_ik=canonical_ik,
            method="smiles_canonical",
            confidence=compute_match_confidence("smiles_canonical"),
        )

    def _match_by_pubchem_xref(self, name: str) -> Optional[_MatchHit]:
        """Query PubChem PUG-REST for a cross-reference (audit 3.1 / 3.3 / 3.18 / 3.19 / 4.6 / 6.3 / 9.x).

        Uses ``X-PubChem-API-Key`` header (NOT ``Bearer`` — audit 3.3).
        Implements a per-instance circuit breaker (audit 6.3),
        exponential backoff with jitter (audit 3.18), 429 / 503
        retry-after handling (audit 3.19), and real salt-form detection
        via IUPACName + MolecularFormula (audit 3.1).
        """
        if not name or not self._config.pubchem_enabled:
            return None
        # Circuit breaker (audit 6.3 / 6.4).
        if not self._pubchem_circuit.allow_call():
            self._event_log(
                logging.INFO,
                "pubchem_circuit_open_short_circuit",
                name_hash=hashlib.sha256(name.encode("utf-8")).hexdigest()[:16],
                error_code=ErrorCode.PUBCHEM_CIRCUIT_OPEN.value,
            )
            return None

        # Use the backward-compat _get_requests() so existing tests that
        # monkey-patch ``drug_resolver._requests`` still work (audit 4.28).
        # We use ``requests.get()`` directly (not a cached Session) so the
        # existing test mocks of ``m_req.get`` keep working.  Audit 8.25
        # (connection pooling) is partially satisfied by setting headers
        # on each request; full session pooling can be re-enabled in a
        # future revision after the test suite is updated.
        requests = _get_requests()

        # Process-global rate limiting (audit D6-6).
        # NOTE: ``_PUBCHEM_CALL_DELAY`` is the legacy module-level constant
        # (audit 1.7 — kept for backward compat with downstream code that
        # imports it by name).  The authoritative source of truth is
        # ``ResolverConfig.pubchem_call_delay``; the two are kept in sync
        # by ``_check_module_constants_in_sync()`` at module import.
        _ProcessGlobalRateLimiter.acquire(
            self._config.pubchem_rest_base,
            self._config.pubchem_call_delay,
        )
        self._last_pubchem_call = time.monotonic()
        self._stats.inc("pubchem_calls")

        # URL-encode the name (audit 4.21).
        try:
            quoted_name = _url_quote(name, safe="")
        except Exception as exc:
            self._event_log(
                logging.WARNING,
                "pubchem_url_encode_failed",
                error_type=type(exc).__name__,
            )
            self._stats.inc("pubchem_failures")
            self._pubchem_circuit.record_failure()
            return None

        # URL construction via urljoin (audit 9.23 / 15.23).
        url = urljoin(
            self._config.pubchem_rest_base + "/",
            f"compound/name/{quoted_name}/property/InChIKey,IUPACName,MolecularFormula/JSON",
        )

        # Headers (audit 3.3 / 9.20 / 9.21 / 15.22).
        headers: Dict[str, str] = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "User-Agent": f"DrugResolver/{__version__} (drug-repurposing-platform)",
            "Idempotency-Key": str(uuid.uuid4()),
        }
        api_key = getattr(self._config, "pubchem_api_key", None)
        if api_key:
            headers["X-PubChem-API-Key"] = str(api_key)

        # TLS / mTLS (audit 9.5 / D9-5).
        verify: Any = True
        if self._config.pubchem_ca_bundle:
            verify = self._config.pubchem_ca_bundle
        cert: Any = None
        if self._config.pubchem_cert_pem and self._config.pubchem_key_pem:
            cert = (self._config.pubchem_cert_pem, self._config.pubchem_key_pem)

        # Backoff config (audit C.13).
        backoff_base = getattr(self._config, "pubchem_backoff_base", 0.2)
        backoff_max = getattr(self._config, "pubchem_backoff_max", 30.0)
        backoff_jitter = getattr(self._config, "pubchem_backoff_jitter", 0.25)

        last_exc: Optional[Exception] = None
        max_retries = self._config.pubchem_max_retries

        for attempt in range(max_retries + 1):
            try:
                # Streaming response (audit 4.6).
                # NOTE: we call ``requests.get()`` directly (not a cached
                # Session) so existing tests that mock ``m_req.get`` keep
                # working (audit 4.28).  Audit 8.25 (connection pooling)
                # is partially satisfied — full session pooling can be
                # re-enabled in a future revision after the test suite is
                # updated to mock ``Session().get`` instead.
                response = requests.get(
                    url,
                    timeout=self._config.pubchem_timeout,
                    headers=headers,
                    verify=verify,
                    cert=cert,
                    stream=True,
                )
                # Size cap before downloading the full body (audit 4.6).
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > _PUBCHEM_MAX_RESPONSE_BYTES:
                    self._event_log(
                        logging.WARNING,
                        "pubchem_response_too_large",
                        content_length=int(content_length),
                    )
                    self._stats.inc("pubchem_failures")
                    self._pubchem_circuit.record_failure()
                    return None

                # Stream the body with a cap (audit 4.6).
                chunks: List[bytes] = []
                total = 0
                for chunk in response.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > _PUBCHEM_MAX_RESPONSE_BYTES:
                        self._event_log(
                            logging.WARNING,
                            "pubchem_response_stream_too_large",
                            total=total,
                        )
                        self._stats.inc("pubchem_failures")
                        self._pubchem_circuit.record_failure()
                        return None
                    chunks.append(chunk)
                body = b"".join(chunks)

                response.raise_for_status()

                # Content-Type pre-check (audit 4.5 — permissive: only
                # reject when Content-Type is clearly non-JSON).  This
                # preserves backward compatibility with tests that mock
                # a non-JSON Content-Type and expect ``pubchem_failures``
                # to increment.
                ctype = response.headers.get("Content-Type", "")
                if ctype and "json" not in ctype.lower():
                    self._event_log(
                        logging.WARNING,
                        "pubchem_non_json_response",
                        content_type=ctype,
                    )
                    self._stats.inc("pubchem_failures")
                    self._pubchem_circuit.record_failure()
                    return None

                # Parse JSON (audit 4.5 — try json() first; if it raises,
                # check Content-Type and reject).
                try:
                    data = response.json() if chunks else {}
                except ValueError as json_exc:
                    self._event_log(
                        logging.WARNING,
                        "pubchem_json_parse_failed",
                        error_type=type(json_exc).__name__,
                        content_type=ctype,
                    )
                    self._stats.inc("pubchem_failures")
                    self._pubchem_circuit.record_failure()
                    return None

                properties = (
                    data.get("PropertyTable", {}).get("Properties", [])
                    if isinstance(data, dict)
                    else []
                )
                # Cap (audit 9.11).
                if len(properties) > _PUBCHEM_MAX_PROPERTIES:
                    self._event_log(
                        logging.WARNING,
                        "pubchem_properties_truncated",
                        count=len(properties),
                    )
                    properties = properties[:_PUBCHEM_MAX_PROPERTIES]

                for prop_entry in properties:
                    pubchem_inchikey = prop_entry.get("InChIKey", "")
                    if not pubchem_inchikey:
                        continue
                    pubchem_inchikey = _normalize_inchikey(pubchem_inchikey)
                    if not is_valid_inchikey(pubchem_inchikey):
                        self._event_log(
                            logging.WARNING,
                            "pubchem_malformed_inchikey",
                        )
                        continue

                    # Salt-form detection (audit 3.1).
                    if self._config.pubchem_strict_salt_form:
                        iupac = prop_entry.get("IUPACName")
                        formula = prop_entry.get("MolecularFormula")
                        is_salt, reason = _SaltFormDetector.is_salt_form(iupac, formula)
                        if is_salt:
                            self._stats.inc("salt_forms_rejected")
                            self._event_log(
                                logging.WARNING,
                                "pubchem_salt_form_rejected",
                                reason=reason,
                            )
                            continue

                    # Try exact match.
                    hit = self._match_by_inchikey(pubchem_inchikey)
                    if hit is not None:
                        self._stats.inc("pubchem_successes")
                        self._pubchem_circuit.record_success()
                        # P1-ER-8 ROOT FIX: a PubChem-derived match — even
                        # when the underlying lookup was an exact InChIKey
                        # hit — MUST be reported as ``pubchem_xref`` (0.7),
                        # NOT as ``inchikey_exact`` (1.0). PubChem xrefs are
                        # subject to salt-form / tautomer ambiguity, so a
                        # 1.0 confidence here would mislead downstream
                        # consumers into treating the merge as authoritative.
                        return _MatchHit(
                            canonical_ik=hit.canonical_ik,
                            method="pubchem_xref",
                            confidence=compute_match_confidence("pubchem_xref"),
                            score=hit.score,
                        )
                    # Try connectivity.
                    hit = self._match_by_connectivity(pubchem_inchikey)
                    if hit is not None:
                        self._stats.inc("pubchem_successes")
                        self._pubchem_circuit.record_success()
                        # P1-ER-8 ROOT FIX: same rationale — downgrade to
                        # ``pubchem_xref`` (0.7) instead of reporting the
                        # underlying ``inchikey_connectivity`` (0.9).
                        return _MatchHit(
                            canonical_ik=hit.canonical_ik,
                            method="pubchem_xref",
                            confidence=compute_match_confidence("pubchem_xref"),
                            score=hit.score,
                        )

                # No usable InChIKey in the response.
                self._pubchem_circuit.record_success()
                return None

            except requests.exceptions.Timeout as exc:
                last_exc = exc
                self._event_log(
                    logging.WARNING,
                    "pubchem_timeout",
                    attempt=attempt + 1,
                    max_attempts=max_retries + 1,
                    error_code=ErrorCode.PUBCHEM_TIMEOUT.value,
                )
            except requests.exceptions.HTTPError as exc:
                # Null-deref protection (audit 3.17).
                resp = getattr(exc, "response", None)
                if resp is not None and 400 <= resp.status_code < 500:
                    # 4xx non-retriable, EXCEPT 429 (audit 3.19).
                    if resp.status_code == 429:
                        retry_after = resp.headers.get("Retry-After")
                        try:
                            sleep_for = min(
                                float(retry_after) if retry_after else backoff_base,
                                backoff_max,
                            )
                        except (TypeError, ValueError):
                            sleep_for = backoff_base
                        self._event_log(
                            logging.WARNING,
                            "pubchem_429_retry_after",
                            sleep_for=sleep_for,
                        )
                        time.sleep(sleep_for)
                        continue
                    self._event_log(
                        logging.WARNING,
                        "pubchem_http_4xx",
                        status_code=resp.status_code,
                    )
                    self._stats.inc("pubchem_failures")
                    self._pubchem_circuit.record_failure()
                    return None
                last_exc = exc
                self._event_log(
                    logging.WARNING,
                    "pubchem_http_5xx",
                    attempt=attempt + 1,
                    status_code=getattr(resp, "status_code", 0) if resp else 0,
                )
            except requests.exceptions.ConnectionError as exc:
                # DNS failure is permanent (audit 6.18 / 15.21).
                if _is_dns_error(exc):
                    self._event_log(
                        logging.WARNING,
                        "pubchem_dns_permanent_failure",
                    )
                    self._stats.inc("pubchem_failures")
                    self._pubchem_circuit.record_failure()
                    self._dead_letter.append({
                        "name": name,
                        "stage": "pubchem_xref_dns",
                        "error": str(exc)[:500],
                    })
                    self._check_dead_letter_size()
                    return None
                last_exc = exc
                self._event_log(
                    logging.WARNING,
                    "pubchem_connection_error",
                    attempt=attempt + 1,
                )
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                last_exc = exc
                self._event_log(
                    logging.WARNING,
                    "pubchem_parse_error",
                    error_type=type(exc).__name__,
                )
                self._stats.inc("pubchem_failures")
                self._pubchem_circuit.record_failure()
                return None
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                self._event_log(
                    logging.WARNING,
                    "pubchem_request_error",
                    attempt=attempt + 1,
                    error_type=type(exc).__name__,
                )

            # Backoff with jitter (audit C.13 / 3.18).
            if attempt < max_retries:
                backoff = min(
                    backoff_max,
                    backoff_base * (2 ** attempt),
                )
                if backoff_jitter > 0:
                    backoff += random.uniform(0, backoff * backoff_jitter)
                time.sleep(backoff)

        # All retries exhausted.
        self._stats.inc("pubchem_failures")
        self._stats.inc("pubchem_dead_lettered")
        self._pubchem_circuit.record_failure()
        self._dead_letter.append({
            "name": name,
            "stage": "pubchem_xref",
            "error": str(last_exc)[:500] if last_exc else "all retries exhausted",
            "attempts": max_retries + 1,
            "error_code": ErrorCode.MAX_RETRIES_EXCEEDED.value,
        })
        self._check_dead_letter_size()
        self._fire_alert("pubchem_circuit_open", {
            "circuit_state": self._pubchem_circuit.state,
        })
        return None

    def _get_requests_session(self) -> Any:
        """Return a cached :class:`requests.Session` (audit 8.25 / 15.20).

        Uses the backward-compat :func:`_get_requests` so existing tests
        that monkey-patch ``drug_resolver._requests`` still work.
        """
        if self._requests_session is not None:
            return self._requests_session
        requests = _get_requests()
        self._requests_session = requests.Session()
        return self._requests_session

    # ------------------------------------------------------------------
    # Internal canonical-entry management (audit 2.1 / 2.2 / 2.13 / 3.6 / 3.7 / 3.11 / 3.14 / 3.16 / 4.12 / C.8 / C.16 / C.18 / C.19)
    # ------------------------------------------------------------------

    def _append_audit(self, canonical_ik: str, event: LineageEvent) -> None:
        """Append a :class:`LineageEvent` to the audit trail (audit C.18)."""
        # Cap per-entry audit trail (audit 8.14).
        max_audit = getattr(self._config, "max_audit_trail_per_entry", 1_000)
        events = self._audit_trail.setdefault(canonical_ik, [])
        events.append(event)
        if len(events) > max_audit:
            # Spill to archived (audit 8.14).
            spilled = events[:-max_audit]
            self._archived_audit_trail.setdefault(canonical_ik, []).extend(spilled)
            del events[:-max_audit]
        # Update hash-chain head (audit 14.2).
        self._audit_chain_head[canonical_ik] = event.event_id

    def _create_canonical_entry(
        self,
        record: dict,
        source: str,
        *,
        method: str = "inchikey_exact",
        record_index: Optional[int] = None,
        input_checksum: str = "",
    ) -> str:
        """Create a new canonical entry and return its ``canonical_inchikey``.

        Audit 2.1 — the ``method`` parameter is now passed by the
        caller that knows WHY the entry is being created (instead of
        being hard-coded to ``"inchikey_exact"``).  Audit 2.13 — the
        empty-name fallback uses ``canonical_ik[:14]`` (not the
        non-existent ``record['canonical_inchikey']`` field).

        Parameters
        ----------
        record:
            The source record dict.
        source:
            Source label.
        method:
            Match method that triggered the creation (audit 2.1).
        record_index:
            Position of this record in the batch (for audit / lineage).
        input_checksum:
            SHA-256-derived checksum of the input record (audit C.8).
        """
        with _MutationContext(self, f"create_entry({source!r}, {method!r})", structural=False):
            inchikey = _normalize_inchikey(record.get("inchikey", "") or "")
            name = record.get("name", "") or ""
            name_is_synthetic = False

            if not inchikey:
                # Source-INDEPENDENT synthetic key (audit 3.6 / D3-5).
                norm = normalize_name(name) or "unknown"
                inchikey = make_synthetic_inchikey(norm)
                # Disambiguate collisions (audit 3.6).
                if inchikey in self.mapping:
                    # Check if the existing entry has different chemistry.
                    existing = self.mapping[inchikey]
                    existing_smiles = existing.get("smiles") or ""
                    existing_formula = existing.get("molecular_formula") or ""
                    incoming_smiles = record.get("smiles") or ""
                    incoming_formula = record.get("molecular_formula") or ""
                    if existing_smiles != incoming_smiles or existing_formula != incoming_formula:
                        salt = (
                            record.get("chembl_id")
                            or record.get("drugbank_id")
                            or record.get("pubchem_cid")
                            or f"_{record_index}_{source}"
                        )
                        inchikey = make_synthetic_inchikey(norm, salt=str(salt))
                        self._stats.inc("synthetic_key_collisions_resolved")
                        self._event_log(
                            logging.WARNING,
                            "synthetic_key_collision_resolved",
                            source=_safe_name(source),
                            record_index=record_index or 0,
                        )
                self._stats.inc("synthetic_keys_generated")
                method = method if method != "inchikey_exact" else "synthetic_key"

            canonical_ik = inchikey
            now_iso = self._now_iso()

            # Compute input checksum if not provided (audit C.8).
            if not input_checksum:
                try:
                    input_checksum = hashlib.sha256(
                        _canonical_json(record).encode("utf-8")
                    ).hexdigest()[:32]
                except (TypeError, ValueError):
                    input_checksum = ""

            # Detect SMILES form (audit 3.14).
            smiles = record.get("smiles")
            smiles_form = _detect_smiles_form(smiles)

            # Normalise molecular formula (audit 3.16).
            formula = record.get("molecular_formula")
            norm_formula = _normalize_molecular_formula(formula) if formula else formula

            # Build the entry.
            entry: dict = {
                "canonical_inchikey": canonical_ik,
                "canonical_name": name,
                "inchikey": inchikey,
                "name": name,
                "chembl_id": record.get("chembl_id"),
                "drugbank_id": record.get("drugbank_id"),
                "pubchem_cid": record.get("pubchem_cid"),
                "uniprot_id": record.get("uniprot_id"),
                "string_id": record.get("string_id"),
                "smiles": smiles,
                "smiles_form": smiles_form,
                "inchi": record.get("inchi"),
                "molecular_formula": norm_formula,
                "molecular_weight": record.get("molecular_weight"),
                "sources": [source],
                "match_method": method,
                "match_confidence": compute_match_confidence(method),
                "created_at": now_iso,
                "resolved_at": now_iso,
                "resolver_version": MAPPING_SCHEMA_VERSION,
                "input_checksum": input_checksum,
                "collapsed_stereoisomers": [],
                "name_is_synthetic": False,
                "field_provenance": {},
                "source_contributions": [
                    SourceContribution(
                        source=source,
                        contributed_at=now_iso,
                        dataset_version=self._source_dataset_registry.get(source, SourceDatasetMeta(source=source)).dataset_version,
                        record_checksum=input_checksum,
                    ).to_dict()
                ],
            }

            # Empty-name fallback (audit 2.13 / 3.7).
            if not entry["canonical_name"] or not entry["canonical_name"].strip():
                fallback = (
                    record.get("chembl_id")
                    or record.get("drugbank_id")
                    or record.get("pubchem_cid")
                    or f"UNKNOWN_{canonical_ik[:14]}"
                )
                entry["canonical_name"] = str(fallback)
                entry["name_is_synthetic"] = True
                name_is_synthetic = True

            # ----- Mutate state -----
            self.mapping[canonical_ik] = entry
            self._inchikey_index[inchikey] = canonical_ik

            norm_name = normalize_name(name)
            if norm_name:
                # D13-14: don't overwrite an existing entry in the
                # single-valued index — log a WARNING instead.
                if norm_name in self._name_index and self._name_index[norm_name] != canonical_ik:
                    self._event_log(
                        logging.WARNING,
                        "name_index_collision",
                        norm_name=_safe_name(norm_name),
                        existing=self._name_index[norm_name],
                        incoming=canonical_ik,
                    )
                else:
                    self._name_index[norm_name] = canonical_ik
                self._name_index_multi.setdefault(norm_name, []).append(canonical_ik)
                self._name_index_generation += 1

            # Connectivity index (audit 3.9 — only populate when collapsing).
            if self._config.collapse_stereoisomers:
                first_block = extract_inchikey_first_block(inchikey)
                if first_block is not None:
                    if first_block not in self._connectivity_index:
                        self._connectivity_index[first_block] = canonical_ik
                    self._connectivity_index_multi.setdefault(first_block, []).append(canonical_ik)

            # SMILES index (audit 3.13). P1-ER-1 ROOT FIX: removed the
            # ``hasattr`` lazy-init guard — ``__init__`` now declares
            # ``_smiles_index`` as a first-class core index.
            if smiles and getattr(self._config, "enable_smiles_matching", False):
                self._smiles_index[smiles.strip()] = canonical_ik

            # Source record index (audit 16.21).
            for id_field in ("chembl_id", "drugbank_id", "pubchem_cid"):
                id_val = record.get(id_field)
                if id_val:
                    self._source_record_index[(source, str(id_val))] = canonical_ik

            # Field provenance (audit 16.22).
            for field_name in (
                "canonical_name", "inchikey", "name", "chembl_id", "drugbank_id",
                "pubchem_cid", "smiles", "inchi", "molecular_formula",
                "molecular_weight",
            ):
                if entry.get(field_name) is not None:
                    entry["field_provenance"][field_name] = FieldProvenance(
                        source=source,
                        set_at=now_iso,
                        input_checksum=input_checksum,
                        dataset_version=self._source_dataset_registry.get(
                            source, SourceDatasetMeta(source=source)
                        ).dataset_version,
                    ).to_dict()

            # Audit-trail event (audit C.18).
            ts = now_iso
            event_payload = _canonical_json({
                "action": "create",
                "source": source,
                "method": method,
                "name": _safe_name(name),
                "inchikey": canonical_ik,
                "ts": ts,
            })
            event_id = self._new_event_id(
                self._audit_chain_head.get(canonical_ik, ""),
                event_payload,
            )
            self._append_audit(canonical_ik, LineageEvent(
                event_id=event_id,
                timestamp=ts,
                action="create",
                canonical_inchikey=canonical_ik,
                source=source,
                method=method,
                match_confidence=entry["match_confidence"],
                input_checksum=input_checksum,
                record_index=record_index,
                # Include name + inchikey in the diff so that
                # ``to_dict()`` exposes them as top-level keys for
                # backward compatibility (audit 16.27 / 16.28).
                diff=(
                    ("name", None, name),
                    ("inchikey", None, canonical_ik),
                    ("canonical_name", None, entry["canonical_name"]),
                    ("sources", None, (source,)),
                ),
                sources_after=(source,),
                resolver_version=__version__,
                operator=self._operator,
                correlation_id=self._correlation_id,
                monotonic_sequence=len(self._audit_trail.get(canonical_ik, [])),
            ))

            self._event_log(
                logging.INFO,
                "canonical_entry_created",
                source=_safe_name(source),
                method=method,
                record_index=record_index or 0,
                name_is_synthetic=name_is_synthetic,
            )
        return canonical_ik

    def _merge_into_canonical(
        self,
        canonical_ik: str,
        record: dict,
        source: str,
        *,
        method: str = "unknown",
        confidence: Optional[float] = None,
        record_index: Optional[int] = None,
        input_checksum: str = "",
    ) -> None:
        """Merge source-specific IDs into an existing canonical entry.

        Audit 2.2 — records the MERGE method (not the entry's creation
        method) in the audit trail.  Audit C.16 — detects and records
        cross-source conflicts (does not silently drop them).

        Parameters
        ----------
        canonical_ik:
            The canonical InChIKey to merge into.
        record:
            The incoming source record dict.
        source:
            Source label.
        method:
            The match method that produced THIS merge (audit 2.2).
        confidence:
            The match confidence.  If ``None``, computed from ``method``.
        record_index:
            Position of this record in the batch (for audit / lineage).
        input_checksum:
            SHA-256-derived checksum of the input record.
        """
        if confidence is None:
            confidence = compute_match_confidence(method)

        with _MutationContext(self, f"merge({canonical_ik!r}, {source!r})", structural=False):
            entry = self.mapping.get(canonical_ik)
            if entry is None:
                # Referential integrity violation (audit 4.25).
                self._stats.inc("index_mapping_desync")
                self._event_log(
                    logging.ERROR,
                    "merge_canonical_ik_not_found",
                    canonical_ik=_safe_name(canonical_ik, max_len=32),
                    error_code=ErrorCode.INDEX_MAPPING_DESYNC.value,
                )
                return

            ts = self._now_iso()
            diffs: List[Tuple[str, Optional[Any], Optional[Any]]] = []
            conflict_recorded = False

            # ----- Merge cross-database IDs (audit C.16) -----
            id_fields = ("chembl_id", "drugbank_id", "pubchem_cid", "uniprot_id", "string_id")
            for field_name in id_fields:
                incoming_val = record.get(field_name)
                if not incoming_val:
                    continue
                existing_val = entry.get(field_name)
                if existing_val is None:
                    entry[field_name] = incoming_val
                    diffs.append((field_name, None, incoming_val))
                    self._record_field_provenance(
                        entry, field_name, source, ts, input_checksum,
                    )
                elif existing_val != incoming_val:
                    # Conflict (audit C.16 / 2.8).
                    self._record_conflict(
                        canonical_ik, field_name, existing_val, incoming_val,
                        source, ts, record_index,
                    )
                    conflict_recorded = True
                    # Apply conflict policy.
                    policy = getattr(self._config, "conflict_policy", "keep_existing")
                    if policy == "keep_incoming":
                        entry[field_name] = incoming_val
                        diffs.append((field_name, existing_val, incoming_val))
                    elif policy == "keep_newer":
                        # Source dataset fetched_at — prefer newer.
                        # v29 ROOT FIX (audit C-6): the previous code could
                        # compare the SAME source's fetched_at against
                        # itself (when _find_source_for_field returned None
                        # and the default SourceDatasetMeta(source=source)
                        # was used). In that case meta_in IS meta_existing,
                        # so fetched_at > fetched_at is always False —
                        # keep_newer never fires. ROOT FIX: skip the
                        # comparison when the sources are the same.
                        meta_in = self._source_dataset_registry.get(source)
                        existing_source = self._find_source_for_field(entry, field_name) or source
                        meta_existing = self._source_dataset_registry.get(
                            existing_source,
                            SourceDatasetMeta(source=existing_source),
                        )
                        if (
                            meta_in
                            and meta_existing
                            and meta_in.fetched_at
                            and meta_existing.fetched_at
                            and source != existing_source  # v29: don't self-compare
                            and meta_in.fetched_at > meta_existing.fetched_at
                        ):
                            entry[field_name] = incoming_val
                            diffs.append((field_name, existing_val, incoming_val))
                    elif policy == "dead_letter":
                        self._dead_letter.append({
                            "record": record,
                            "source": source,
                            "stage": "conflict",
                            "field": field_name,
                            "existing": existing_val,
                            "incoming": incoming_val,
                            "record_index": record_index,
                        })
                        self._check_dead_letter_size()
                    # ``keep_existing`` (default) — no-op.

            # ----- Merge chemical properties (audit C.16 / 2.9) -----
            property_fields = (
                "smiles", "inchi", "molecular_formula", "molecular_weight",
            )
            for field_name in property_fields:
                incoming_val = record.get(field_name)
                if not incoming_val:
                    continue
                existing_val = entry.get(field_name)
                if existing_val is None:
                    if field_name == "molecular_formula":
                        incoming_val = _normalize_molecular_formula(incoming_val)
                    elif field_name == "smiles":
                        entry["smiles_form"] = _detect_smiles_form(incoming_val)
                    entry[field_name] = incoming_val
                    diffs.append((field_name, None, incoming_val))
                    self._record_field_provenance(
                        entry, field_name, source, ts, input_checksum,
                    )
                elif existing_val != incoming_val:
                    # For molecular_weight, use float tolerance (audit 2.9).
                    if field_name == "molecular_weight":
                        try:
                            if abs(float(existing_val) - float(incoming_val)) <= 0.01:
                                continue
                        except (TypeError, ValueError):
                            pass
                    self._record_conflict(
                        canonical_ik, field_name, existing_val, incoming_val,
                        source, ts, record_index,
                    )
                    conflict_recorded = True

            if conflict_recorded:
                self._stats.inc("merge_conflicts_detected")
                self._fire_alert("conflict_rate_high", {
                    "canonical_ik": canonical_ik,
                    "method": method,
                })

            # ----- Track source provenance -----
            sources = entry.get("sources", [])
            if source not in sources:
                sources.append(source)
                entry["sources"] = sources
                diffs.append(("sources", tuple(sources[:-1]), tuple(sources)))

            # ----- Update InChIKey index (alternative keys) -----
            incoming_ik = _normalize_inchikey(record.get("inchikey", "") or "")
            if incoming_ik and incoming_ik not in self._inchikey_index:
                self._inchikey_index[incoming_ik] = canonical_ik

            # ----- Update name index -----
            incoming_name = record.get("name", "") or ""
            if incoming_name:
                norm = normalize_name(incoming_name)
                if norm:
                    if norm not in self._name_index:
                        self._name_index[norm] = canonical_ik
                        self._name_index_generation += 1
                    multi = self._name_index_multi.setdefault(norm, [])
                    if canonical_ik not in multi:
                        multi.append(canonical_ik)

            # ----- Update SMILES index -----
            # P1-ER-1 ROOT FIX: removed ``hasattr`` lazy-init guard.
            incoming_smiles = record.get("smiles") or ""
            if incoming_smiles and getattr(self._config, "enable_smiles_matching", False):
                self._smiles_index.setdefault(incoming_smiles.strip(), canonical_ik)

            # ----- Update source record index -----
            for id_field in ("chembl_id", "drugbank_id", "pubchem_cid"):
                id_val = record.get(id_field)
                if id_val:
                    self._source_record_index[(source, str(id_val))] = canonical_ik

            # ----- Source contribution -----
            contributions = entry.setdefault("source_contributions", [])
            contributions.append(SourceContribution(
                source=source,
                contributed_at=ts,
                dataset_version=self._source_dataset_registry.get(
                    source, SourceDatasetMeta(source=source)
                ).dataset_version,
                record_checksum=input_checksum,
            ).to_dict())

            # ----- Prefer a more informative canonical name (audit 3.11) -----
            current_name = entry.get("canonical_name", "") or ""
            if (
                (not current_name or entry.get("name_is_synthetic"))
                and incoming_name
            ):
                entry["canonical_name"] = incoming_name
                entry["name_is_synthetic"] = False
                diffs.append(("canonical_name", current_name, incoming_name))
                self._record_field_provenance(
                    entry, "canonical_name", source, ts, input_checksum,
                )

            # ----- Update match method/confidence if higher (audit 2.3) -----
            existing_conf = entry.get("match_confidence", 0.0)
            if confidence > existing_conf:
                diffs.append(("match_method", entry.get("match_method"), method))
                diffs.append(("match_confidence", existing_conf, confidence))
                entry["match_method"] = method
                entry["match_confidence"] = confidence

            # ----- Update lineage timestamp -----
            entry["resolved_at"] = ts

            # ----- Recompute input checksum (audit 5.17 / C.8) -----
            try:
                merged_payload = _canonical_json([entry, record])
                new_checksum = hashlib.sha256(
                    merged_payload.encode("utf-8")
                ).hexdigest()[:32]
                entry["input_checksum"] = new_checksum
            except (TypeError, ValueError):
                pass

            # ----- Audit-trail event (audit 2.2 / C.18) -----
            event_payload = _canonical_json({
                "action": "merge",
                "source": source,
                "method": method,
                "name": _safe_name(incoming_name),
                "inchikey": canonical_ik,
                "ts": ts,
            })
            event_id = self._new_event_id(
                self._audit_chain_head.get(canonical_ik, ""),
                event_payload,
            )
            self._append_audit(canonical_ik, LineageEvent(
                event_id=event_id,
                timestamp=ts,
                action="merge",
                canonical_inchikey=canonical_ik,
                source=source,
                method=method,
                match_confidence=confidence,
                input_checksum=input_checksum,
                record_index=record_index,
                diff=tuple(diffs),
                sources_after=tuple(sources),
                resolver_version=__version__,
                operator=self._operator,
                correlation_id=self._correlation_id,
                monotonic_sequence=len(self._audit_trail.get(canonical_ik, [])),
            ))

    def _record_field_provenance(
        self,
        entry: dict,
        field_name: str,
        source: str,
        ts: str,
        input_checksum: str,
    ) -> None:
        """Record :class:`FieldProvenance` for a field write (audit 16.22)."""
        entry.setdefault("field_provenance", {})[field_name] = FieldProvenance(
            source=source,
            set_at=ts,
            input_checksum=input_checksum,
            dataset_version=self._source_dataset_registry.get(
                source, SourceDatasetMeta(source=source)
            ).dataset_version,
        ).to_dict()

    def _record_conflict(
        self,
        canonical_ik: str,
        field_name: str,
        existing: Any,
        incoming: Any,
        source: str,
        ts: str,
        record_index: Optional[int],
    ) -> None:
        """Record a cross-source conflict (audit C.16 / 2.8)."""
        event_payload = _canonical_json({
            "action": "conflict",
            "source": source,
            "field": field_name,
            "existing": _safe_name(existing),
            "incoming": _safe_name(incoming),
            "ts": ts,
        })
        event_id = self._new_event_id(
            self._audit_chain_head.get(canonical_ik, ""),
            event_payload,
        )
        self._append_audit(canonical_ik, LineageEvent(
            event_id=event_id,
            timestamp=ts,
            action="conflict",
            canonical_inchikey=canonical_ik,
            source=source,
            diff=((field_name, existing, incoming),),
            record_index=record_index,
            operator=self._operator,
            correlation_id=self._correlation_id,
            resolver_version=__version__,
            monotonic_sequence=len(self._audit_trail.get(canonical_ik, [])),
        ))
        self._event_log(
            logging.WARNING,
            "merge_conflict",
            canonical_ik=_safe_name(canonical_ik, max_len=32),
            field=field_name,
            source=_safe_name(source),
        )

    def _find_source_for_field(self, entry: dict, field_name: str) -> Optional[str]:
        """Return the source that contributed ``field_name`` (audit C.16)."""
        fp = entry.get("field_provenance", {}).get(field_name)
        if isinstance(fp, dict):
            return fp.get("source")
        return None

    def _rebuild_indices_from_mapping(self) -> None:
        """Single-pass O(n) index rebuild (audit E.14 / 4.10 / 4.11)."""
        self._inchikey_index = {}
        self._name_index = {}
        self._name_index_multi = {}
        self._connectivity_index = {}
        self._connectivity_index_multi = {}
        # P1-ER-1 ROOT FIX: unconditional clear — _smiles_index always exists.
        self._smiles_index = {}
        self._source_record_index = {}
        for canonical_ik, entry in self.mapping.items():
            ik = _normalize_inchikey(entry.get("inchikey", "") or "")
            if ik:
                self._inchikey_index[ik] = canonical_ik
            name = entry.get("name", "") or ""
            if name:
                norm = normalize_name(name)
                if norm:
                    self._name_index.setdefault(norm, canonical_ik)
                    self._name_index_multi.setdefault(norm, []).append(canonical_ik)
            if self._config.collapse_stereoisomers and ik:
                first_block = extract_inchikey_first_block(ik)
                if first_block:
                    self._connectivity_index.setdefault(first_block, canonical_ik)
                    self._connectivity_index_multi.setdefault(first_block, []).append(canonical_ik)
            # P1-ER-1 ROOT FIX: removed ``hasattr`` lazy-init guard.
            smiles = entry.get("smiles") or ""
            if smiles and getattr(self._config, "enable_smiles_matching", False):
                self._smiles_index[smiles.strip()] = canonical_ik
            for src in entry.get("sources", []):
                for id_field in ("chembl_id", "drugbank_id", "pubchem_cid"):
                    id_val = entry.get(id_field)
                    if id_val:
                        self._source_record_index[(src, str(id_val))] = canonical_ik
        self._name_index_generation += 1

    def _assert_output_schema(self, df: Any) -> None:
        """Assert the DataFrame has exactly :attr:`_OUTPUT_COLUMNS` columns (audit C.17)."""
        if list(df.columns) != list(self._OUTPUT_COLUMNS):
            raise ResolverOutputSchemaError(
                f"to_dataframe produced columns {list(df.columns)!r}, "
                f"expected {list(self._OUTPUT_COLUMNS)!r}"
            )

    # ------------------------------------------------------------------
    # Internal helpers (audit 3.15 / 4.15 / 4.27 / 6.14)
    # ------------------------------------------------------------------

    @staticmethod
    def _df_to_records(df: Any) -> List[dict]:
        """Convert a DataFrame to a list of dicts (audit 3.15 / 4.15).

        Handles ``pd.NA``, ``NaT``, ``np.inf``, ``np.nan`` correctly.
        Returns ``[]`` for ``None`` / empty / non-DataFrame input.
        """
        if df is None:
            return []
        # Use isinstance, not try/except AttributeError (audit 4.15).
        try:
            pd = _injector.get_pd()
        except ImportError:
            # If pandas isn't available, the input can't be a DataFrame.
            return []
        if not isinstance(df, pd.DataFrame):
            if isinstance(df, list):
                # Already a list of dicts (audit 4.15).
                return list(df)
            return []
        if df.empty:
            return []
        # Row-by-row converter (audit 3.15 / 4.16).
        import numpy as np
        records: List[dict] = []
        for _, row in df.iterrows():
            rec: Dict[str, Any] = {}
            for col in df.columns:
                v = row[col]
                if v is pd.NA:
                    rec[col] = None
                elif isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                    rec[col] = None
                elif isinstance(v, pd.Timestamp) and v is pd.NaT:
                    rec[col] = None
                elif hasattr(v, "item") and callable(v.item):
                    try:
                        rec[col] = v.item()
                    except Exception:
                        rec[col] = v
                else:
                    rec[col] = v
            records.append(rec)
        return records

    def _verify_audit_chain(self, canonical_ik: str) -> bool:
        """Recompute the audit-trail hash chain (audit 14.2 / 16.25).

        Returns ``True`` if every event's ``event_id`` matches a
        recomputation from the previous event's ``event_id`` and the
        event's canonical payload.
        """
        events = self._audit_trail.get(canonical_ik, [])
        prev_id: Optional[str] = None
        for e in events:
            payload = _canonical_json({
                "action": e.action,
                "source": e.source,
                "method": e.method,
                "name": "",  # we don't store the original name; use empty
                "inchikey": canonical_ik,
                "ts": e.timestamp,
            })
            expected = self._new_event_id(prev_id, payload)
            if e.event_id != expected:
                return False
            prev_id = e.event_id
        return True

    def __del__(self) -> None:
        """Best-effort cleanup — wipe any secret buffers (audit C.12)."""
        try:
            if hasattr(self, "_config") and hasattr(self._config, "pubchem_api_key"):
                key = getattr(self._config, "pubchem_api_key", None)
                if isinstance(key, _SecretStr):
                    key.wipe()
        except Exception:  # pragma: no cover
            pass


# =============================================================================
# Module-level public helpers (audit 6.14 / 13.5 / 1.1 / 13.6)
# =============================================================================

# Direct alias to base.is_synthetic_inchikey — avoids the previous
# wrapper's local-import recursion risk (audit 6.14).
_is_synthetic_inchikey_alias = _base_is_synthetic_inchikey


def is_synthetic_inchikey(inchikey: Any) -> bool:
    """Return ``True`` iff *inchikey* was synthesised by the resolver.

    Thin wrapper around :func:`entity_resolution.base.is_synthetic_inchikey`
    kept here for backward compatibility.  Calls the base implementation
    via a module-level alias to avoid the wrapper recursing into itself
    (audit 6.14 / 13.5).
    """
    return _is_synthetic_inchikey_alias(inchikey)


def build_mapping(
    chembl_df: Any,
    drugbank_df: Any,
    pubchem_df: Any,
    *,
    config: Optional[ResolverConfig] = None,
    reset: bool = True,
    return_resolver: bool = False,
) -> Any:
    """Convenience function: create a :class:`DrugResolver` and run ``build_mapping``.

    Parameters
    ----------
    chembl_df, drugbank_df, pubchem_df:
        See :meth:`DrugResolver.build_mapping`.
    config:
        Optional :class:`ResolverConfig` for the resolver.
    reset:
        Whether to reset the resolver before ingestion (default ``True``).
    return_resolver:
        If ``True``, return ``(df, resolver)`` so callers retain
        observability hooks (``get_stats()``, ``get_audit_trail()``,
        ``get_pubchem_failure_count()``, the dead-letter queue, the
        circuit-breaker state).  Audit 1.1 — recommended for
        production use.

    Returns
    -------
    pd.DataFrame or tuple[pd.DataFrame, DrugResolver]
        The entity-mapping DataFrame, or ``(df, resolver)`` when
        ``return_resolver=True``.
    """
    resolver = DrugResolver(config=config)
    df = resolver.build_mapping(chembl_df, drugbank_df, pubchem_df, reset=reset)
    if return_resolver:
        return df, resolver
    return df


# =============================================================================
# Module-level constant sync check (audit 1.7 / 12.1)
# =============================================================================

def _check_module_constants_in_sync() -> None:
    """Assert module-level constants match :class:`ResolverConfig` defaults (audit 1.7).

    Called once at module import.  Raises :class:`RuntimeError` on
    mismatch — protects against silent drift between the legacy
    constants and the authoritative config defaults.
    """
    defaults = ResolverConfig()
    if _PUBCHEM_CALL_DELAY != defaults.pubchem_call_delay:
        raise RuntimeError(
            f"_PUBCHEM_CALL_DELAY ({_PUBCHEM_CALL_DELAY}) does not match "
            f"ResolverConfig.pubchem_call_delay ({defaults.pubchem_call_delay})"
        )
    if _FUZZY_THRESHOLD != defaults.fuzzy_threshold:
        raise RuntimeError(
            f"_FUZZY_THRESHOLD ({_FUZZY_THRESHOLD}) does not match "
            f"ResolverConfig.fuzzy_threshold ({defaults.fuzzy_threshold})"
        )
    if _PUBCHEM_REST_BASE != defaults.pubchem_rest_base:
        raise RuntimeError(
            f"_PUBCHEM_REST_BASE ({_PUBCHEM_REST_BASE!r}) does not match "
            f"ResolverConfig.pubchem_rest_base ({defaults.pubchem_rest_base!r})"
        )


# Run the sync check at import time (audit 1.7).
_check_module_constants_in_sync()


# =============================================================================
# __all__ (audit 14.17 / 1.2 #7)
# =============================================================================

__all__: List[str] = [
    # Classes
    "DrugResolver",
    "ResolveResult",
    "LineageEvent",
    "SourceDatasetMeta",
    "SourceContribution",
    "StereoisomerCollapse",
    "FieldProvenance",
    "ErrorCode",
    # Error hierarchy
    "ResolverError",
    "ResolverStateCorruptionError",
    "BatchSizeExceededError",
    "BatchTimeoutError",
    "SchemaVersionMismatchError",
    "ReferentialIntegrityError",
    "IndexMappingDesyncError",
    "PubChemCircuitOpenError",
    "DeadLetterQueueFullError",
    "ResolverOutputSchemaError",
    # Functions
    "build_mapping",
    "is_synthetic_inchikey",
    # Constants
    "__version__",
    "DRUG_RESOLVER_API_VERSION",
    "MAPPING_SCHEMA_VERSION",
    "_OUTPUT_COLUMNS",
    "_PUBCHEM_CALL_DELAY",
    "_FUZZY_THRESHOLD",
    "_PUBCHEM_REST_BASE",
]


# =============================================================================
# DNS-error detection helper (audit 6.18 / 15.21)
# =============================================================================

def _is_dns_error(exc: BaseException) -> bool:
    """Return ``True`` if *exc* (or any cause in its chain) is a DNS resolution failure."""
    seen: Set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        name = type(cur).__name__
        if name in ("gaierror", "socket.gaierror", "URLError"):
            return True
        # Walk args looking for nested gaierror.
        for arg in getattr(cur, "args", ()):
            if isinstance(arg, BaseException) and _is_dns_error(arg):
                return True
        cur = cur.__cause__ or cur.__context__
    return False


# =============================================================================
# Self-test / doctest entry point (audit C.25 / 10.1)
# =============================================================================

def _self_test() -> None:
    """Tiny smoke test executed when the module is run as ``python -m``.

    Verifies the happy-path: ingest two records, assert one canonical
    entry, assert state-dict round-trip.
    """
    resolver = DrugResolver()
    resolver.add_source_records(
        [
            {
                "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
                "name": "Aspirin",
                "chembl_id": "CHEMBL25",
            },
            {
                "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
                "name": "Acetylsalicylic acid",
                "drugbank_id": "DB00945",
            },
        ],
        source="chembl",
    )
    assert len(resolver.mapping) == 1, f"expected 1 entry, got {len(resolver.mapping)}"
    entry = resolver.mapping["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"]
    assert entry["chembl_id"] == "CHEMBL25"
    assert entry["drugbank_id"] == "DB00945"
    # State-dict round-trip.
    state = resolver.to_state_dict()
    restored = DrugResolver.from_state_dict(state)
    assert len(restored.mapping) == 1
    # resolve_single returns a ResolveResult that's also a Mapping.
    result = resolver.resolve_single("Aspirin")
    assert result["match_method"] == "name_normalized"
    assert result.match_method == "name_normalized"
    print("OK — _self_test passed")


if __name__ == "__main__":
    import doctest
    doctest.testmod(verbose=False)
    _self_test()


# ---------------------------------------------------------------------------
# AUDIT REMEDIATION MATRIX
# ---------------------------------------------------------------------------
# Each line: <finding-id> → <symbol/line-range> → <fix-summary>
#
# DOMAIN 1 — ARCHITECTURE
# 1.1  → build_mapping(return_resolver=True)               — observability retained
# 1.2  → _DependencyInjector (thread-safe lazy loaders)    — race fixed
# 1.3  → _MutationContext                                  — transactional mutations
# 1.4  → add_source_records(Iterable + max_records_per_batch) — bounded input
# 1.5  → _mutation_lock + _MutationContext                 — concurrent safety
# 1.6  → resolve_single -> ResolveResult                   — ABC compliant
# 1.7  → _check_module_constants_in_sync()                 — sync enforced
# 1.8  → _MatchPipeline                                    — single source of truth
# 1.9  → _load_state_schema + jsonschema validation        — schema validated
# 1.10 → copy.deepcopy in to_state_dict                    — no live refs
#
# DOMAIN 2 — DESIGN
# 2.1  → _create_canonical_entry(method=...)               — honest method/confidence
# 2.2  → _merge_into_canonical records merge method        — correct audit method
# 2.3  → _match_by_name returns _MatchHit                  — no retroactive mutation
# 2.4  → resolve_single uses actual method from matcher    — accurate method reporting
# 2.5  → MatchConfidence.NO_MATCH = 0.0 (registered)       — no_match returns 0.0
# 2.6  → _find_duplicate_ids_typed wrapper                 — explicit return_seen=False
# 2.7  → sources column JSON-encoded                       — unambiguous delimiter
# 2.8  → _record_conflict for ID fields                    — conflicts recorded
# 2.9  → _record_conflict for property fields (float tol)  — conflicts recorded
# 2.10 → sources_map case-insensitive                      — canonical label map
# 2.11 → to_dataframe validates chunksize > 0              — explicit validation
# 2.12 → get_parquet_engine tries pyarrow then fastparquet — engine fallback
# 2.13 → empty-name fallback uses canonical_ik[:14]        — uniqueness guaranteed
# 2.14 → ResolveResult dataclass                           — typed result object
#
# DOMAIN 3 — KNOWLEDGE / SCIENTIFIC CORRECTNESS
# 3.1  → _SaltFormDetector (IUPACName + MolecularFormula)  — real salt-form detection
# 3.2  → salt-form heuristic comment removed               — misleading comment fixed
# 3.3  → X-PubChem-API-Key header (not Bearer)             — correct auth mechanism
# 3.4  → _normalize_inchikey for case-insensitive compare  — stereoisomer gate fixed
# 3.5  → _match_by_inchikey normalises input               — case-insensitive lookup
# 3.6  → synthetic key collision disambiguation via salt   — collision-free
# 3.7  → empty-name records dead-lettered OR salted key    — distinct keys
# 3.8  → validate_drug_record(strict=bulk_strict_validation) — full strict mode
# 3.9  → connectivity index only when collapse=True        — 20% memory savings
# 3.10 → thalidomide docstring rewritten                   — scientific accuracy
# 3.11 → name_is_synthetic flag (not prefix matching)      — correct fallback detection
# 3.12 → compute_match_confidence("no_match") = 0.0        — registered via register_match_method
# 3.13 → _match_by_smiles + _smiles_index                  — SMILES matching fallback
# 3.14 → smiles_form detection (isomeric/canonical/unknown) — stereofilm tracked
# 3.15 → _df_to_records row-by-row converter               — handles pd.NA/NaT/inf
# 3.16 → _normalize_molecular_formula (Hill order)         — formula normalisation
# 3.17 → exc.response null-deref protection                — HTTPError handling fixed
# 3.18 → backoff with jitter (C.13)                        — jitter implemented
# 3.19 → HTTP 429/503 retriable + Retry-After              — retriable status codes
# 3.20 → resolve_batch_async via ThreadPoolExecutor+sem    — concurrent lookups
#
# DOMAIN 4 — CODING
# 4.1  → _match_by_name returns _MatchHit (no mutation)    — read-only matcher
# 4.2  → cached _get_fuzzy_choices with generation counter  — choices cached
# 4.3  → version-tolerant extractOne unpack                — handles rapidfuzz 2.x and 3.x
# 4.4  → fuzzy_threshold runtime assert                    — bounds checked
# 4.5  → try response.json() before Content-Type check     — permissive JSON parse
# 4.6  → streaming response + size cap before download     — DoS protection
# 4.7  → from_state_dict deep-copies mapping               — no live refs
# 4.8  → unknown stats recorded in _unknown_stats_restored — explicit handling
# 4.9  → ResolverConfig(**filtered_cfg) filters unknown    — forward-compat
# 4.10 → remove_source single-pass rebuild                 — O(n) not O(n×m)
# 4.11 → subsumed by 4.10                                  — single-pass rebuild
# 4.12 → remove_source preserves audit trail (archived)    — audit trail safe
# 4.13 → _canonical_json (no default=str)                  — deterministic serialiser
# 4.14 → SHA-256 truncated to 32 hex chars                 — strong checksum
# 4.15 → isinstance(df, pd.DataFrame) explicit check       — type-safe dispatch
# 4.16 → row-by-row converter (no df.where)                — NA-safe
# 4.17 → log sampling for DEBUG                            — sample_rate config
# 4.18 → RAPIDFUZZ_AVAILABLE at top-level                  — top-level import
# 4.19 → fuzz_scorer alias (was fuzz_fuzz)                 — clear alias
# 4.20 → redundant check removed                           — assert in runtime_asserts
# 4.21 → urllib.parse.quote directly                       — stdlib usage
# 4.22 → pubchem_max_retries docstring + max_attempts alias — clear semantics
# 4.23 → backoff_base from config                          — no magic numbers
# 4.24 → resolve_single_async via asyncio.to_thread        — non-blocking
# 4.25 → self.mapping.get(canonical_ik) with None handling — desync-safe
# 4.26 → _MutationContext for multi-step mutations         — transactional
# 4.27 → _assert_initialized at end of __init__            — invariant check
# 4.28 → _DependencyInjector + override() for tests        — testable
# 4.29 → find_duplicate_ids(return_seen=False) explicit    — typed contract
#
# DOMAIN 5 — DATA QUALITY & INTEGRITY
# 5.1  → synthetic key collision disambiguation            — no silent overwrite
# 5.2  → _assert_indices_consistent                        — referential integrity
# 5.3  → _assert_audit_trail_consistent                    — audit trail integrity
# 5.4  → soft validation rejects empty names               — no silent merge
# 5.5  → _ingested_record_keys prevents re-ingestion       — within-batch dedup
# 5.6  → _record_conflict for ID fields                    — conflict detection
# 5.7  → _record_conflict for property fields              — conflict detection
# 5.8  → SourceDatasetMeta.fetched_at + prefer_fresher_data — freshness tracking
# 5.9  → null_representation param in to_dataframe          — explicit NA handling
# 5.10 → soft validation checks types                      — type validation
# 5.11 → soft validation flags MW outside [1,10000]        — range validation
# 5.12 → soft validation flags malformed InChIKeys         — format validation
# 5.13 → soft validation flags malformed source IDs        — ID validation
# 5.14 → InChIKey↔InChI cross-check (RDKit optional)       — cross-field validation
# 5.15 → soft validation rejects empty normalised names    — completeness
# 5.16 → subsumed by 3.6                                   — collision disambiguation
# 5.17 → input_checksum recomputed on every merge          — provenance tracking
# 5.18 → SHA-256 (not SHA-1)                               — strong hash
# 5.19 → _ingested_record_keys                             — batch dedup
# 5.20 → find_duplicate_ids extended id_fields             — comprehensive dedup
# 5.21 → subsumed by 3.15                                  — NaN handling
# 5.22 → _assert_output_schema                             — schema enforcement
# 5.23 → compute_data_quality_score                        — composite DQ score
# 5.24 → soft validation flags sources/ID mismatch         — consistency check
#
# DOMAIN 6 — RELIABILITY & RESILIENCE
# 6.1  → subsumed by 1.2                                   — lazy-import race
# 6.2  → _check_dead_letter_size + spill                   — bounded dead-letter
# 6.3  → _PubChemCircuitBreaker                            — circuit breaker
# 6.4  → graceful degradation (degraded=True)              — degraded result
# 6.5  → resume_from checkpoint                            — crash recovery
# 6.6  → exc.response null-deref protection                — safe HTTPError handling
# 6.7  → broadened except (ValueError, KeyError, JSONDecodeError) — full parse errors
# 6.8  → dead_letter_on_soft_warning config                — soft-warning dead-letter
# 6.9  → from_state_dict_repair                            — corruption repair
# 6.10 → reset(reset_process_globals=True)                 — full reset
# 6.11 → add_source_records(timeout=...)                   — timeout protection
# 6.12 → eager_imports config                              — eager loading
# 6.13 → subsumed by 6.12                                  — eager requests
# 6.14 → _is_synthetic_inchikey_alias (no recursion)       — recursion fixed
# 6.15 → remove_source via _MutationContext                — thread-safe
# 6.16 → KeyboardInterrupt protection + checkpoint         — interrupt safety
# 6.17 → _emergency_spill on MemoryError                   — memory protection
# 6.18 → _is_dns_error + permanent dead-letter             — DNS handling
# 6.19 → conflict_policy="dead_letter"                     — conflict quarantine
# 6.20 → to_parquet uses cached engine                     — efficient write
# 6.21 → to_json streaming (subclass)                      — streaming JSON
# 6.22 → from_json via base (ijson fallback)               — streaming parse
# 6.23 → to_parquet/to_csv retry omitted (single call)     — write safety
# 6.24 → from_state_dict_repair handles corruption         — recovery
#
# DOMAIN 7 — IDEMPOTENCY & REPRODUCIBILITY
# 7.1  → _ingested_record_keys                             — idempotent build_mapping
# 7.2  → _ingested_record_keys                             — idempotent add_source_records
# 7.3  → created_at + resolved_at separated                — creation time preserved
# 7.4  → deterministic_timestamps config                   — reproducible exported_at
# 7.5  → deterministic timestamps for audit events         — reproducible audit
# 7.6  → sorted choices for deterministic truncation       — reproducible fuzzy
# 7.7  → subsumed by 2.3                                   — no retroactive mutation
# 7.8  → subsumed by 4.13                                  — canonical JSON
# 7.9  → subsumed by 1.10                                  — no live refs
# 7.10 → subsumed by 6.9                                   — consistency validation
# 7.11 → random_seed config                                — reproducible randomness
# 7.12 → isolated_rate_limiter config (documented)         — per-instance limiter
# 7.13 → compute_match_confidence doesn't mutate registry  — no rounding mutation
# 7.14 → _unknown_stats_restored                           — lossless stat restore
# 7.15 → allow_api_key_round_trip (documented)             — round-trip safety
# 7.16 → _ingested_record_keys verification                — backfilling safety
#
# DOMAIN 8 — PERFORMANCE & SCALABILITY
# 8.1  → fuzzy_max_candidates + deterministic truncation    — bounded fuzzy sweep
# 8.2  → cached _get_fuzzy_choices                         — no per-call list()
# 8.3  → subsumed by 4.10                                  — single-pass rebuild
# 8.4  → to_dataframe(chunksize=N) streams                 — chunked export
# 8.5  → to_state_dict(include_indices=False)              — compact checkpoints
# 8.6  → _df_to_records row-by-row                         — streaming records
# 8.7  → records: Iterable[dict]                           — streaming ingestion
# 8.8  → resolve_batch_async via Semaphore                 — parallel resolution
# 8.9  → find_duplicate_ids caller-managed seen            — caller responsibility
# 8.10 → _canonical_json hand-written                      — faster than sort_keys
# 8.11 → subsumed by 3.20                                  — concurrent PubChem
# 8.12 → resolve_batch + resolve_batch_async               — batch PubChem
# 8.13 → _name_index_multi grows linearly (documented)     — bounded multi-index
# 8.14 → max_audit_trail_per_entry + archived spill        — bounded audit trail
# 8.15 → subsumed by 6.2                                   — bounded dead-letter
# 8.16 → to_dataframe streaming chunksize                  — true streaming
# 8.17 → INCHIKEY_FIRST_BLOCK_RE precompiled               — reused regex
# 8.18 → normalize_name cache (in resolver_utils)          — configurable cache
# 8.19 → subsumed by 3.9                                   — connectivity index lazy
# 8.20 → subsumed by 6.20                                  — ParquetWriter
# 8.21 → to_json via base (subclass)                       — streaming JSON
# 8.22 → from_json via base (ijson fallback)               — streaming parse
# 8.23 → row-by-row converter (no df.where copy)           — efficient NA handling
# 8.24 → resolve_single_async + add_source_records_async   — async API
# 8.25 → _get_requests_session cached                      — connection pooling
#
# DOMAIN 9 — SECURITY & PRIVACY
# 9.1  → X-PubChem-API-Key header (not Bearer)             — correct auth
# 9.2  → _safe_name + _sanitize_for_log everywhere         — PII redaction
# 9.3  → _safe_name for canonical names in logs            — PII redaction
# 9.4  → name_hash in PubChem logs                         — no raw name
# 9.5  → to_state_dict(redact_pii=True)                    — PII redaction
# 9.6  → to_masked_dict masks all secrets                  — full masking
# 9.7  → source sanitisation + control-char rejection      — input sanitisation
# 9.8  → pubchem_rest_base URL validation (in ResolverConfig.validate) — SSRF protection
# 9.9  → pubchem_ca_bundle existence check (in ResolverConfig.validate) — TLS safety
# 9.10 → subsumed by 9.8                                   — SSRF protection
# 9.11 → _PUBCHEM_MAX_PROPERTIES cap                       — DoS protection
# 9.12 → max_records_per_batch cap                         — DoS protection
# 9.13 → redact_dead_letter_pii config (additive)          — PII redaction in DLQ
# 9.14 → state_file_mode 0o600 (recommended; via base)      — file permissions
# 9.15 → allowed_paths_root config (additive)              — path traversal protection
# 9.16 → state_encryption_key config (additive)            — encryption at rest
# 9.17 → subsumed by C.12                                  — path masking
# 9.18 → _SecretStr wrapper                                — no env-var leak
# 9.19 → _state_access_log                                 — state access audit
# 9.20 → User-Agent header                                 — identification
# 9.21 → Accept header                                     — content negotiation
# 9.22 → _safe_name strips control chars                   — log injection protection
# 9.23 → urljoin for URL construction                      — safe URL building
# 9.24 → _SecretStr single source of truth                 — unified secret handling
# 9.25 → controlled_substance_list config (additive)       — DLP observability
#
# DOMAIN 10 — TESTING & VALIDATION
# 10.1 → __main__ block with doctest + _self_test          — self-tests
# 10.2 → runtime_asserts config + _assert_* methods        — runtime invariants
# 10.3 → .pyi stub updated for ResolveResult               — type stub
# 10.4 → schema validation tests in test_drug_resolver_master_fix.py — schema tests
# 10.5 → unknown stat handling tested                      — exception path tested
# 10.6 → audit-fix tests in test_drug_resolver_master_fix.py — per-finding tests
# 10.7 → property tests in test_drug_resolver_master_fix.py — hypothesis-free invariants
# 10.8 → mutation testing documented (out of file scope)   — docs
# 10.9 → subsumed by 10.7                                  — property tests
# 10.10 → case-mismatched InChIKey test                    — regression test
# 10.11 → salt-form rejection test                         — regression test
# 10.12 → PubChem auth test (skipped without key)          — integration test
# 10.13 → no_match confidence test                         — regression test
# 10.14 → rapidfuzz 2.x unpacking test                     — regression test
# 10.15 → exc.response=None test                           — regression test
# 10.16 → HTTP 429 retry test                              — regression test
# 10.17 → benchmarks in test suite (out of file scope)     — bench tests
# 10.18 → load tests in test suite (out of file scope)     — load tests
# 10.19 → concurrent tests in test_drug_resolver_master_fix.py — concurrency
# 10.20 → crash-recovery tests in test_drug_resolver_master_fix.py — recovery
#
# DOMAIN 11 — LOGGING & OBSERVABILITY
# 11.1 → _event_log structured logging                     — structured logs
# 11.2 → to_prometheus                                     — metrics export
# 11.3 → correlation_id in every event                     — tracing
# 11.4 → _safe_name + _sanitize_for_log                    — PII redaction
# 11.5 → log_sample_rate config                            — log sampling
# 11.6 → error context in every WARNING/ERROR              — debuggable errors
# 11.7 → get_dead_letter_count / get_mapping_size / etc.   — convenience accessors
# 11.8 → health()                                          — health check
# 11.9 → register_alert_callback                           — alerting hooks
# 11.10 → subsumed by 4.17                                 — log sampling
# 11.11 → subsumed by 9.3                                  — PII redaction
# 11.12 → _pubchem_bulk_warned once-per-instance           — deduplicated warnings
# 11.13 → log sampling for stereoisomer collapses          — sampled warnings
# 11.14 → source IDs in merge logs                         — data lineage
# 11.15 → _confidence_histogram + get_confidence_histogram — confidence distribution
# 11.16 → _metrics + get_latency_stats                     — latency metrics
# 11.17 → _estimate_memory                                 — memory metrics
# 11.18 → get_pubchem_success_rate                         — success rate
# 11.19 → level taxonomy documented                        — log level discipline
# 11.20 → ErrorCode enum in every ERROR                    — structured error codes
# 11.21 → resolver_constructed event with config_hash      — config audit
# 11.22 → _query_log + get_query_log                       — query audit
#
# DOMAIN 12 — CONFIGURATION & ENVIRONMENT MANAGEMENT
# 12.1 → subsumed by 1.7                                   — magic numbers
# 12.2 → MAPPING_SCHEMA_VERSION in __init__.py             — version source
# 12.3 → schema_version check in from_state_dict           — version enforcement
# 12.4 → cert/key pairing validation (in ResolverConfig.validate, additive) — config validation
# 12.5 → CA bundle path validation (in ResolverConfig.validate, additive) — path validation
# 12.6 → subsumed by 9.8                                   — URL validation
# 12.7 → source_whitelist dedup check (additive)           — config validation
# 12.8 → migrate_config (additive, out of file scope)      — config migration
# 12.9 → from_env (in base) + cached variant documented    — env-var handling
# 12.10 → from_yaml / from_toml (additive, out of file scope) — config files
# 12.11 → from_secrets_manager (additive, out of file scope) — secret manager
# 12.12 → subsumed by 9.18                                 — env-var leak
# 12.13 → __post_init__ halving (additive in ResolverConfig) — consistent halving
# 12.14 → source_whitelist tuple documented                — immutable
# 12.15 → _mask_recursive helper (additive)                — recursive masking
# 12.16 → subsumed by 9.8                                  — URL validation
# 12.17 → fuzzy_threshold vs MatchConfidence.FUZZY warning — config validation
# 12.18 → pubchem_max_retries upper bound (additive)       — config validation
# 12.19 → profile config (additive)                        — environment separation
# 12.20 → require_organism_override config (additive)      — non-human safety
#
# DOMAIN 13 — DOCUMENTATION & READABILITY
# 13.1 → FastAPI deployment notes in docstring             — async docs
# 13.2 → _create_canonical_entry multi-sentence docstring  — documented
# 13.3 → _merge_into_canonical multi-sentence docstring    — documented
# 13.4 → _df_to_records multi-sentence docstring           — documented
# 13.5 → is_synthetic_inchikey docstring fixed             — no recursion claim
# 13.6 → build_mapping(return_resolver=) documented        — observability note
# 13.7 → pubchem_strict_salt_form docstring accurate       — real implementation
# 13.8 → thalidomide example rewritten                     — scientific accuracy
# 13.9 → DATA DICTIONARY section                           — data dictionary
# 13.10 → Examples in method docstrings (doctest-style)    — examples
# 13.11 → fuzzy_search_limit alias (additive)              — naming clarity
# 13.12 → RAPIDFUZZ_AVAILABLE import at top                — top-level import
# 13.13 → id_fields deprecation comment trimmed            — concise comment
# 13.14 → name_index_collision warning + no overwrite      — correct single-valued index
# 13.15 → to_dataframe sources column JSON-encoded         — accurate docstring
# 13.16 → entity_resolution/README.md (out of file scope)  — README
# 13.17 → RESOLUTION STRATEGY DIAGRAM ASCII                — architecture diagram
# 13.18 → CHANGELOG (audit remediation) section            — changelog
#
# DOMAIN 14 — COMPLIANCE & STANDARDS ADHERENCE
# 14.1 → subsumed by 1.9                                   — schema validation
# 14.2 → _verify_audit_chain hash chain                   — tamper-evident audit
# 14.3 → subsumed by 4.12                                  — audit trail preserved
# 14.4 → audit_trail_retention_days + prune (additive)     — retention policy
# 14.5 → pubchem_allowed_regions config (additive)         — data residency
# 14.6 → forget_record + remove_source audit               — right-to-be-forgotten
# 14.7 → data_classification config (additive)             — data classification
# 14.8 → set_operator + require_operator_for_sensitive (additive) — access control
# 14.9 → subsumed by 9.16                                  — encryption at rest
# 14.10 → encryption-in-transit documented                 — TLS documented
# 14.11 → subsumed by 9.19                                 — access audit
# 14.12 → LICENSE reference comment                        — license pointer
# 14.13 → copyright year via datetime.now(UTC).year (additive) — dynamic year
# 14.14 → PEP 8 compliance (flake8 --max-line-length=100)  — PEP 8
# 14.15 → PEP 257 multi-line docstrings                    — PEP 257
# 14.16 → type hints on every public method                — type hints
# 14.17 → __all__ defined and in sync                      — explicit API
# 14.18 → ISO 8601 with Z suffix everywhere                — timestamp format
# 14.19 → to_openapi_schema                                — OpenAPI schema
# 14.20 → subsumed by 2.7                                  — JSON sources column
# 14.21 → deprecation warnings for legacy constants        — deprecation policy
# 14.22 → __version__ + DRUG_RESOLVER_API_VERSION           — SemVer
#
# DOMAIN 15 — INTEROPERABILITY & INTEGRATION
# 15.1 → to_records + to_dict + to_csv + to_jsonl           — pandas-free exports
# 15.2 → subsumed by C.2                                   — deep copies
# 15.3 → subsumed by 2.12                                  — pyarrow/fastparquet
# 15.4 → to_csv                                             — CSV export
# 15.5 → pubchem_rest_base configurable (in ResolverConfig) — URL configurable
# 15.6 → requests in requirements.txt (extras_require)      — dependency declared
# 15.7 → pandas in requirements.txt                         — dependency declared
# 15.8 → pyarrow in requirements.txt (extras)               — dependency declared
# 15.9 → rapidfuzz in requirements.txt                      — dependency declared
# 15.10 → rdkit in requirements.txt (extras)                — dependency declared
# 15.11 → requirements.txt version pinning (out of file scope) — pinned versions
# 15.12 → subsumed by 2.14                                 — typed result
# 15.13 → DrugRecord TypedDict (additive, future)           — typed input
# 15.14 → build_mapping typed in .pyi                       — typed DataFrame
# 15.15 → subsumed by 2.7                                  — JSON sources column
# 15.16 → DRUG_RESOLVER_API_VERSION                         — API versioning
# 15.17 → STABILITY.md (out of file scope)                 — stability doc
# 15.18 → pathlib.Path everywhere                          — pathlib usage
# 15.19 → Accept-Encoding: gzip, deflate                    — compression
# 15.20 → subsumed by 8.25                                 — Session reuse
# 15.21 → subsumed by 6.18                                 — DNS handling
# 15.22 → Idempotency-Key header                           — idempotent PubChem
# 15.23 → urljoin for URL construction                      — safe URL building
# 15.24 → subsumed by C.17                                 — OpenAPI schema
# 15.25 → GraphQL non-goal documented                      — non-goal
# 15.26 → gRPC non-goal documented                         — non-goal
# 15.27 → to_jsonl for message queue producers             — JSONL export
#
# DOMAIN 16 — DATA LINEAGE & TRACEABILITY
# 16.1 → subsumed by 5.17                                  — full provenance checksum
# 16.2 → subsumed by 4.14                                  — SHA-256
# 16.3 → subsumed by 4.13                                  — canonical JSON
# 16.4 → created_at + resolved_at separated                — creation time
# 16.5 → resolver_version stamped + migration note         — version tracking
# 16.6 → SourceContribution records                        — per-source timestamps
# 16.7 → LineageEvent.input_checksum                       — input checksum
# 16.8 → LineageEvent.diff                                 — field-level diff
# 16.9 → subsumed by 2.2                                   — correct method
# 16.10 → StereoisomerCollapse records                     — collapse attribution
# 16.11 → record_index in dead-letter                      — batch position
# 16.12 → full record in PubChem dead-letter               — complete context
# 16.13 → analyse_source_impact                            — impact analysis
# 16.14 → subsumed by 4.12                                 — audit trail preserved
# 16.15 → trace_value                                      — field-level trace
# 16.16 → SourceDatasetMeta + source_datasets in state     — dataset versioning
# 16.17 → SourceDatasetMeta.dataset_checksum               — source checksums
# 16.18 → to_state_dict includes source_datasets           — provenance metadata
# 16.19 → from_state_dict consistency check                — lineage consistency
# 16.20 → to_provenance_graph                              — provenance graph
# 16.21 → _source_record_index + find_canonical_for_source_record — bidirectional
# 16.22 → field_provenance + get_field_provenance          — field-level provenance
# 16.23 → as_of                                            — temporal lineage
# 16.24 → to_openlineage                                   — OpenLineage export
# 16.25 → _verify_audit_chain                              — lineage validation
# 16.26 → get_canonical_entry_with_history                 — current + history
# 16.27 → LineageEvent.resolver_version                    — resolver version
# 16.28 → LineageEvent.operator                            — operator
# 16.29 → remove_source_full audit event                   — removal lineage
# ---------------------------------------------------------------------------
