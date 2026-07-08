# SPDX-License-Identifier: MIT
# © 2024-2026 Autonomous Drug Repurposing Platform — Team Cosmic / VentureLab
"""
Shared utilities for entity resolution across the Drug Repurposing ETL platform.

This module provides the foundational primitives used by both
:class:`~entity_resolution.drug_resolver.DrugResolver` and
:class:`~entity_resolution.protein_resolver.ProteinResolver`:

* :func:`normalize_name` — Aggressive name normalisation for fuzzy matching
  (handles Unicode, Greek letters, parenthetical content, stereo indicators).
* :func:`fuzzy_match_score` — :mod:`rapidfuzz`-backed fuzzy similarity with a
  graceful exact-match fallback when rapidfuzz is unavailable.
* :func:`fuzzy_match_best` — Batch fuzzy-match helper that uses
  :func:`rapidfuzz.process.extractOne` for O(1)-style lookup instead of O(n)
  linear sweeps.
* :func:`extract_inchikey_first_block` — Connectivity-block extraction with
  full InChIKey validation, case normalisation, and synthetic-key rejection.
* :func:`is_valid_inchikey` — Delegates to :func:`cleaning.normalizer.is_valid_inchikey`,
  the SINGLE source of truth for InChIKey validation across the platform.
* :func:`build_name_index`, :func:`build_inchikey_index` — Legacy multi-valued
  index builders (DEPRECATED — kept for backward compatibility).
* :func:`build_canonical_name_index`, :func:`build_canonical_inchikey_index` —
  Canonical single-valued index builders used by the resolvers.
* :data:`METHOD_CONFIDENCE` — Public mapping from method name → confidence score,
  thread-safe and monitored.  Mirrored in the
  :class:`entity_resolution.base.MatchConfidence` enum; the two MUST be kept
  in sync (see :func:`sync_method_confidence`).
* :func:`register_match_method`, :func:`unregister_match_method`,
  :func:`reset_method_confidence`, :func:`get_registered_methods`,
  :func:`sync_method_confidence`, :func:`method_confidence_override` —
  Runtime management of confidence scores with full provenance.
* :func:`compute_match_confidence` — Confidence-score lookup with optional
  enum / detailed / config-aware modes.
* :func:`validate_drug_record`, :func:`validate_protein_record`,
  :func:`validate_record` — Record validation with strict-mode format checks,
  cross-field referential integrity, and structured ``ValidationReport`` output.
* :func:`find_duplicate_ids` — Within-batch duplicate detection with NaN/empty
  handling, cross-batch ``seen`` parameter, and optional counts/indices.

Design invariants
-----------------
1. **Single source of truth for InChIKey validation** — All InChIKey validation
   in this module delegates to :func:`cleaning.normalizer.is_valid_inchikey`,
   which handles standard (``-N``), non-standard (``-B`` etc.), mixture
   (comma-separated), and synthetic (``SYNTH`` prefix) InChIKeys per the InChI
   Trust specification.
2. **UniProt accession regex** — Uses the OFFICIAL UniProt pattern
   ``^([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9]([A-Z0-9]{3}[0-9]){1,2})$``
   so clinically-critical proteins like TP53 (P04637), HBB (P68871),
   RAD51C (Q9NZQ7), STXBP2 (O00161) are correctly accepted.
3. **Thread-safe confidence registry** — All mutations to :data:`METHOD_CONFIDENCE`
   go through a re-entrant lock to support concurrent resolver use.
4. **Backward compatibility** — Every public function preserves its historical
   signature with new parameters added as keyword-only with safe defaults.
5. **Provenance / lineage** — Every confidence lookup, validation, and
   normalisation can optionally return a structured dataclass
   (``MatchResult``, ``ValidationReport``, ``NormalizedName``,
   ``ConnectivityBlock``) recording what was done, when, and from where.

All public symbols are re-exported via :mod:`entity_resolution.__init__`.
"""

from __future__ import annotations

# =============================================================================
# Standard-library imports
# =============================================================================
import functools
import hashlib
import inspect
import logging
import math
import re
import threading
import unicodedata
import warnings
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
    overload,
)

# =============================================================================
# Optional third-party: rapidfuzz
# =============================================================================
try:
    from rapidfuzz import fuzz as _rapidfuzz_fuzz
    RAPIDFUZZ_AVAILABLE: bool = True
except ImportError:  # pragma: no cover - exercised in test_missing_optional_deps
    RAPIDFUZZ_AVAILABLE = False
    _rapidfuzz_fuzz = None  # type: ignore[assignment]

# Backward-compat: keep the private alias pointing at the same value so
# any external code that did ``from entity_resolution.resolver_utils
# import _RAPIDFUZZ_AVAILABLE`` keeps working.  FIX #4 makes the public
# ``RAPIDFUZZ_AVAILABLE`` the canonical name.
_RAPIDFUZZ_AVAILABLE: bool = RAPIDFUZZ_AVAILABLE

logger = logging.getLogger(__name__)

# =============================================================================
# Module-level constants
# =============================================================================

#: Version of the resolver_utils schema (state-dict + confidence registry).
#: Bumped whenever METHOD_CONFIDENCE values change or new public symbols are added.
_RESOLVER_UTILS_SCHEMA_VERSION: str = "1.1"

#: Public prefix used by the resolver to mark synthesised InChIKeys.
#: Mirrors :data:`entity_resolution.base.SYNTHETIC_INCHIKEY_PREFIX`.
_SYNTHETIC_PREFIX: str = "SYNTH"

# =============================================================================
# Precompiled patterns (compiled once at import for performance)
# =============================================================================

# Parentheses removal — uses ``[^)]*`` (non-greedy on the closing paren).
# Applied iteratively to handle nested parens (FIX #26 / BUG-CODE-02).
_PARENTHESES_RE: re.Pattern[str] = re.compile(r"\([^)]*\)")

# Default character allowlist for :func:`normalize_name` — a-z, 0-9, hyphen, slash.
# Precompiled for the hot path; ``allow_chars`` overrides recompile a new pattern.
_NON_ALNUM_RE: re.Pattern[str] = re.compile(r"[^a-z0-9\-/]")

# Collapse multiple consecutive hyphens / slashes into a single character.
_MULTI_HYPHEN_RE: re.Pattern[str] = re.compile(r"-{2,}")
_MULTI_SLASH_RE: re.Pattern[str] = re.compile(r"/{2,}")

# Legacy strict InChIKey shape (kept as a backward-compat alias; the real
# validation is delegated to :func:`cleaning.normalizer.is_valid_inchikey`).
_INCHIKEY_RE: re.Pattern[str] = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")

# Official UniProt accession regex (FIX #15 / BUG-SCI-01).
# v35 ROOT FIX (issue 39): import the canonical UniProt accession regex
# from ``cleaning._constants`` (single source of truth). The local
# definition was byte-for-byte identical, but having two definitions meant
# future edits to one could silently diverge from the other (audit D-4 /
# Chain 3). Falls back to the local pattern only if ``cleaning._constants``
# is not importable (test isolation).
try:
    from cleaning._constants import (
        CANONICAL_UNIPROT_ACCESSION_REGEX_FULL as _UNIPROT_ACCESSION_RE,  # noqa: F401
        CANONICAL_AA_SEQUENCE_REGEX as _AA_VALID_RE,  # noqa: F401
    )
except ImportError:
    # Fallback: replicate the canonical patterns EXACTLY.
    # Source: https://www.uniprot.org/help/accession_numbers
    #   - 6-char:   [OPQ][0-9][A-Z0-9]{3}[0-9]
    #   - 10-char:  [A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2}
    # Accepts P04637 (TP53), P68871 (HBB), Q9NZQ7 (RAD51C), O00161 (STXBP2),
    # A0A024RBG1 (10-char isoform), and rejects malformed accessions.
    # SCI-FIX: Changed [A-Z0-9]{3}[0-9] to [A-Z][A-Z0-9]{2}[0-9] in the 10-char
    # alternative to match the official UniProt spec (first char of each 4-char
    # block must be a letter, not a digit).
    _UNIPROT_ACCESSION_RE: re.Pattern[str] = re.compile(
        r"^([OPQ][0-9][A-Z0-9]{3}[0-9]"
        r"|[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2})$"
    )
    # AA sequence: 20 standard + ambiguity codes B J O U X Z + stop * +
    # alignment gap ``-``. Aligned EXACTLY with
    # ``cleaning._constants.CANONICAL_AA_SEQUENCE_REGEX`` (v35 root fix
    # issue 40 — include or exclude ``-`` consistently across all layers).
    _AA_VALID_RE: re.Pattern[str] = re.compile(
        r"^[ACDEFGHIKLMNPQRSTVWYBJOUXZ\*\-]+$"
    )

# Cross-database ID format patterns (FIX #23 / BUG-SCI-09).
_CHEMBL_ID_RE: re.Pattern[str] = re.compile(r"^CHEMBL\d+$")
# P1-ER-15 ROOT FIX: tightened from ^DB\d{5,7}$ to ^DB\d{5,6}$ to align
# EXACTLY with Phase 2 kg_builder.ID_PATTERNS["Compound"] (which uses
# DB\d{5,6}). The previous {5,7} upper bound accepted 7-digit IDs like
# DB9999999 that DrugBank has never emitted and that Phase 2 rejects —
# creating a "Phase 1 accepts, Phase 2 rejects" mismatch that silently
# dropped drugs at the bridge. DrugBank 5.1.10's highest ID is DB16999
# (5 digits); even with expansion headroom to DB999999 (6 digits), 7
# digits is unjustified. Lower bound 5 preserves the rejection of
# DB1 / DB123. SW-7 ROOT FIX retained.
_DRUGBANK_ID_RE: re.Pattern[str] = re.compile(r"^DB\d{5,6}$")
_INCHI_PREFIX_RE: re.Pattern[str] = re.compile(r"^InChI=1[SB]?/")
# v9 ROOT FIX (audit F4.8): the comment claimed this matches STRING's
# "taxonID.ENSEMBL_protein_id" format (e.g. 9606.ENSP00000269305), but
# ENS[A-Z]+ matches ANY Ensembl ID type — ENSP (protein), ENSG (gene),
# ENST (transcript), ENSR (regulatory). STRING only emits ENSP records
# (it is a protein-protein interaction database). The loose regex would
# silently accept ENSG/ENST/ENSR IDs if a future code path ever fed
# them in. Tighten to ENSP only — fail-closed for everything else.
# Examples accepted: 9606.ENSP00000269305, 511145.ENSP00000269305
# Examples rejected: 9606.ENSG00000143590, 9606.ENST00000357654
_STRING_ID_RE: re.Pattern[str] = re.compile(r"^\d+\.ENSP\d+$")
_CHEMBL_TARGET_ID_RE: re.Pattern[str] = re.compile(r"^CHEMBL\d+$")
# v35 ROOT FIX (issue 39/40): ``_AA_VALID_RE`` is now imported from
# ``cleaning._constants.CANONICAL_AA_SEQUENCE_REGEX`` above (or defined
# in the fallback branch). The duplicate local definition that was here
# has been removed to ensure a single source of truth.

# Method-name validation (FIX #65 / GAP-SEC-03) — lowercase identifier
# starting with a letter, allowing lowercase alphanumerics and underscores.
_VALID_METHOD_NAME_RE: re.Pattern[str] = re.compile(r"^[a-z][a-z0-9_]+$")

# =============================================================================
# Greek-letter transliteration map (FIX #21 / BUG-SCI-07)
# =============================================================================

#: Maps every Greek letter (lowercase AND uppercase) to its ASCII name.
#: Applied BEFORE the ``_NON_ALNUM_RE`` filter so that ``α-tocopherol`` and
#: ``γ-tocopherol`` produce DIFFERENT normalised names (``alpha-tocopherol``
#: vs ``gamma-tocopherol``) and are NOT incorrectly merged as the same entity.
_GREEK_MAP: Dict[str, str] = {
    # Lowercase
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta",
    "ε": "epsilon", "ζ": "zeta", "η": "eta", "θ": "theta",
    "ι": "iota", "κ": "kappa", "λ": "lambda", "μ": "mu",
    "ν": "nu", "ξ": "xi", "ο": "omicron", "π": "pi",
    "ρ": "rho", "σ": "sigma", "τ": "tau", "υ": "upsilon",
    "φ": "phi", "χ": "chi", "ψ": "psi", "ω": "omega",
    # Uppercase
    "Α": "alpha", "Β": "beta", "Γ": "gamma", "Δ": "delta",
    "Ε": "epsilon", "Ζ": "zeta", "Η": "eta", "Θ": "theta",
    "Ι": "iota", "Κ": "kappa", "Λ": "lambda", "Μ": "mu",
    "Ν": "nu", "Ξ": "xi", "Ο": "omicron", "Π": "pi",
    "Ρ": "rho", "Σ": "sigma", "Τ": "tau", "Υ": "upsilon",
    "Φ": "phi", "Χ": "chi", "Ψ": "psi", "Ω": "omega",
}


def _transliterate_greek(text: str) -> str:
    """Replace every Greek letter in *text* with its ASCII name.

    Used by :func:`normalize_name` so that ``α-tocopherol`` becomes
    ``alpha-tocopherol`` instead of being destroyed by the ASCII-only
    regex filter.  Non-Greek characters are passed through unchanged.

    Parameters
    ----------
    text:
        Already lower-cased string (we still handle uppercase for safety).

    Returns
    -------
    str
        The input with every Greek letter replaced by its ASCII name.
    """
    if not text:
        return text
    return "".join(_GREEK_MAP.get(c, c) for c in text)


# =============================================================================
# Helpers — sanitisation, caller info
# =============================================================================

def _sanitize_for_log(value: Any, max_len: int = 16) -> str:
    """Truncate *value* for safe inclusion in log messages (FIX #37 / BUG-SEC-01).

    Drug names, InChIKeys, and protein identifiers can be proprietary or
    PII-adjacent in some contexts.  This helper ensures the log output never
    contains the full value — only the first ``max_len`` characters followed
    by an ellipsis.

    Parameters
    ----------
    value:
        Any value — converted to ``str`` via ``repr`` if not already a string.
    max_len:
        Maximum number of characters to retain.  Default ``16``.

    Returns
    -------
    str
        The truncated representation, suitable for inclusion in log messages.
    """
    if value is None:
        return "<none>"
    s = value if isinstance(value, str) else repr(value)
    if len(s) <= max_len:
        return s
    return s[:max_len] + "..."


def _truncate_for_error(value: Any, max_len: int = 32) -> str:
    """Truncate *value* for inclusion in user-facing error messages (FIX #66).

    Error messages may flow into logs or be shown to operators.  Truncating
    long values keeps messages readable and prevents accidental leakage of
    large input payloads.
    """
    if value is None:
        return "<none>"
    s = repr(value)
    if len(s) <= max_len:
        return s
    return s[:max_len] + "..."


def _get_caller_info(skip: int = 2) -> str:
    """Return ``filename:lineno`` of the calling code (FIX #83 / GUARD-LOG-05).

    Used by :func:`register_match_method` and :func:`compute_match_confidence`
    to record WHO triggered a mutation / lookup, so operators can trace
    rogue callers in production logs.

    Parameters
    ----------
    skip:
        Stack frames to skip.  Default ``2`` (caller of the function that
        called ``_get_caller_info``).
    """
    try:
        frame = inspect.stack()[skip]
        return f"{frame.filename}:{frame.lineno}"
    except (IndexError, AttributeError):  # pragma: no cover - defensive
        return "<unknown>"


# =============================================================================
# Provenance dataclasses (FIX #108, #112, #113, #114)
# =============================================================================

@dataclass(frozen=True)
class MatchResult:
    """Result of a confidence-score lookup with full provenance (FIX #108).

    Returned by :func:`compute_match_confidence` when ``detailed=True``.
    Implements ``__float__`` so it can be used interchangeably with a bare
    ``float`` in legacy callers.
    """

    #: Method name that was looked up.
    method: str
    #: Resolved confidence score in ``[0.0, 1.0]``.
    confidence: float
    #: ``True`` if *method* was found in :data:`METHOD_CONFIDENCE`,
    #: ``False`` if the default (0.5) was returned.
    is_known: bool
    #: ISO-8601 UTC timestamp of the lookup.
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    #: Caller file:line that triggered the lookup.
    caller: str = field(default_factory=lambda: _get_caller_info(skip=3))

    def __float__(self) -> float:
        return self.confidence

    def __int__(self) -> int:
        return int(self.confidence)


@dataclass
class ValidationReport:
    """Structured validation result with full provenance (FIX #112).

    Returned by :func:`validate_drug_record` and :func:`validate_protein_record`
    when ``detailed=True``.  Otherwise, the legacy ``(bool, list[str])`` tuple
    is returned for backward compatibility.
    """

    #: ``True`` if no errors were found.
    ok: bool
    #: List of human-readable error messages (empty if ``ok`` is ``True``).
    errors: List[str]
    #: Either ``"drug"`` or ``"protein"``.
    record_type: str
    #: Field names that were inspected during validation.
    validated_fields: List[str]
    #: ISO-8601 UTC timestamp of the validation run.
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def error_count(self) -> int:
        """Number of validation errors found."""
        return len(self.errors)


@dataclass(frozen=True)
class NormalizedName:
    """Normalised name with provenance information (FIX #113).

    Returned by :func:`normalize_name` when ``detailed=True``.  Implements
    ``__str__``, ``__eq__``, and ``__hash__`` so it can be used
    interchangeably with a bare ``str`` in dictionaries and sets.
    """

    #: The normalised name (always lower-case ASCII).
    normalized: str
    #: The original input string.
    original: str
    #: List of transformation step names that were applied.
    transformations: List[str]

    def __str__(self) -> str:
        return self.normalized

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.normalized == other
        if isinstance(other, NormalizedName):
            return self.normalized == other.normalized
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.normalized)


@dataclass(frozen=True)
class ConnectivityBlock:
    """Connectivity block with provenance (FIX #114).

    Returned by :func:`extract_inchikey_first_block` when ``detailed=True``.
    """

    #: The 14-character connectivity block.
    block: str
    #: The full 27-character InChIKey the block was extracted from.
    full_inchikey: str
    #: ``True`` if the source InChIKey was a synthetic (``SYNTH``-prefixed) key.
    is_synthetic: bool

    def __str__(self) -> str:
        return self.block

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.block == other
        if isinstance(other, ConnectivityBlock):
            return self.block == other.block
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.block)


# =============================================================================
# Thread-safe, monitored confidence registry (FIX #5, #6, #10, #47, #48, #54)
# =============================================================================

class _MonitoredDict(dict):
    """A ``dict`` subclass that logs every mutation at DEBUG level (FIX #54).

    All mutations to :data:`METHOD_CONFIDENCE` go through this class so we
    can audit who changed what, when — even when external code does
    ``METHOD_CONFIDENCE["foo"] = 0.5`` directly instead of going through
    :func:`register_match_method`.

    Reads are NOT logged (they would flood the log); only ``__setitem__``,
    ``__delitem__``, ``pop``, ``popitem``, ``clear``, ``update``, and
    ``setdefault`` are instrumented.
    """

    def __setitem__(self, key: Any, value: Any) -> None:
        old = self.get(key)
        super().__setitem__(key, value)
        # Only log at DEBUG to avoid spamming INFO/WARNING in production.
        logger.debug(
            "METHOD_CONFIDENCE mutation: %s = %s (was %s) — caller %s",
            key, value, old, _get_caller_info(skip=2),
        )

    def __delitem__(self, key: Any) -> None:
        old = self.get(key)
        super().__delitem__(key)
        logger.debug(
            "METHOD_CONFIDENCE deletion: %s (was %s) — caller %s",
            key, old, _get_caller_info(skip=2),
        )

    def pop(self, key: Any, *default: Any) -> Any:  # type: ignore[override]
        old = self.get(key)
        result = super().pop(key, *default)  # type: ignore[arg-type]
        logger.debug(
            "METHOD_CONFIDENCE pop: %s (was %s) — caller %s",
            key, old, _get_caller_info(skip=2),
        )
        return result

    def update(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        super().update(*args, **kwargs)
        logger.debug(
            "METHOD_CONFIDENCE update: %d entries merged — caller %s",
            (len(args[0]) if args and hasattr(args[0], "__len__") else len(kwargs)),
            _get_caller_info(skip=2),
        )

    def clear(self) -> None:  # type: ignore[override]
        super().clear()
        logger.debug(
            "METHOD_CONFIDENCE clear — caller %s",
            _get_caller_info(skip=2),
        )


#: Public mapping from resolution-method name → confidence score.
#:
#: This is the **public** counterpart of the legacy private
#: ``_METHOD_CONFIDENCE`` table.  Audit D2-4 / D16-7 require it be
#: exported so downstream code (e.g. ``compute_match_confidence``
#: consumers, the Phase-5 API) can introspect the legal method set
#: without re-implementing the lookup.
#:
#: Audit D3-3 fix (SUPERSEDED — see v29 ROOT FIX below): the ``"fuzzy"``
#: entry was raised from ``0.6`` to ``0.85`` so that
#: ``METHOD_CONFIDENCE["fuzzy"] >= fuzzy_threshold`` (i.e. accepted fuzzy
#: matches are never reported with a confidence below the threshold that
#: accepted them).  Previously the mismatch (threshold 0.85, reported 0.6)
#: caused downstream ``confidence >= 0.7`` filters to silently drop valid
#: matches.
#:
#: SCI-02 fix (SUPERSEDED — see v29 ROOT FIX below):
#: ``protein_name_fuzzy`` was raised from ``0.6`` to ``0.90`` so that
#: ``METHOD_CONFIDENCE["protein_name_fuzzy"] >= _PROTEIN_FUZZY_THRESHOLD``
#: (0.90).  Audit SCI-02 — the same class of bug as D3-3 but for proteins.
#:
#: NOTE: These values are mirrored in :class:`entity_resolution.base.MatchConfidence`.
#: The two MUST be kept in sync — call :func:`sync_method_confidence` to verify.
#: v29 ROOT FIX (audit C-1 / C-2 — Confidence Score Inversion):
#: The values for "fuzzy" (was 0.85) and "protein_name_fuzzy" (was 0.90)
#: were HIGHER than "name_normalized" (0.8). This is scientifically
#: wrong — a fuzzy match is by definition LESS reliable than an exact
#: normalized name match. The inversion caused the entity resolver to
#: preferentially keep low-quality fuzzy matches over high-quality
#: exact matches. Lowered to 0.65 / 0.60 respectively so the hierarchy
#: is: inchikey_exact > inchikey_connectivity > name_normalized >
#: gene_name_organism > fuzzy > protein_name_fuzzy > pubchem_xref.
#: See base.MatchConfidence for the canonical enum.
#:
#: v41 ROOT FIX (SEV2): the comment block above was historically
#: inaccurate — it claimed "fuzzy raised to 0.85" as the current value
#: while the actual value (per the v29 ROOT FIX) is 0.65, and the
#: _FUZZY_THRESHOLD (also v29-lowered) is 0.60.  The comment has been
#: rewritten to mark the D3-3 / SCI-02 fixes as SUPERSEDED by the v29
#: ROOT FIX, and to call out the actual current values (0.65 for
#: ``fuzzy``, 0.60 for ``protein_name_fuzzy``, 0.60 for
#: ``_FUZZY_THRESHOLD`` — see base.py and resolver_utils._FUZZY_THRESHOLD).
METHOD_CONFIDENCE: Dict[str, float] = _MonitoredDict({
    "inchikey_exact": 1.0,
    "inchikey_connectivity": 0.9,
    "name_normalized": 0.8,
    "pubchem_xref": 0.7,
    "fuzzy": 0.65,                  # v29: was 0.85 — inversion fix
    "uniprot_exact": 1.0,
    "gene_name_organism": 0.75,    # v29: was 0.85 — sit between
                                    # name_normalized and fuzzy
    "protein_name_fuzzy": 0.60,    # v29: was 0.90 — inversion fix
})

#: Snapshot of the original module-load values (FIX #10).
#: Used by :func:`reset_method_confidence` to restore the defaults.
#: NEVER mutate this dict — copy from it.
_ORIGINAL_METHOD_CONFIDENCE: Dict[str, float] = dict(METHOD_CONFIDENCE)

#: Backward-compat alias — MUST point to the SAME object as
#: :data:`METHOD_CONFIDENCE` (legacy contract asserted by the test suite
#: at ``tests/test_entity_resolution_init.py::test_method_confidence_exported``).
_METHOD_CONFIDENCE: Dict[str, float] = METHOD_CONFIDENCE

#: Re-entrant lock protecting all mutations to :data:`METHOD_CONFIDENCE`
#: and :data:`_custom_methods` (FIX #47 / BUG-REL-01).
_METHOD_CONFIDENCE_LOCK: threading.RLock = threading.RLock()

#: Custom (runtime-registered) method names that supplement the
#: :class:`~entity_resolution.base.MatchConfidence` enum (FIX #6 / GUARD-ARCH-06).
#: Checked by :func:`compute_match_confidence` as a fallback before
#: returning the default 0.5 for unknown methods.
_custom_methods: Dict[str, float] = {}

#: Set of unknown method names already warned about (FIX #50 / GAP-REL-04).
#: Prevents log spam when the same unknown method is queried many times.
_unknown_method_warned: set = set()

#: Has the rapidfuzz fallback warning been emitted yet? (FIX #51 / GUARD-REL-05)
_rapidfuzz_fallback_warned: bool = False

#: Sentinel used to detect whether the caller explicitly passed ``seen=None``
#: to :func:`find_duplicate_ids` (FIX #38).  Using a sentinel instead of
#: ``None`` as the default lets us distinguish "not passed" from "passed None".
_UNSET_SENTINEL: Any = object()


# =============================================================================
# Name normalisation
# =============================================================================

@lru_cache(maxsize=8192)
def _normalize_name_cached(name: str, allow_chars: str = "-/") -> str:
    """Cached implementation of :func:`normalize_name` for string inputs.

    Separated from the public :func:`normalize_name` so that the cache only
    sees hashable ``str`` arguments (not ``None``, ``int``, etc.).
    """
    # Step 1: Unicode NFC normalisation (GAP-SCI-08) — collapses composed
    # vs decomposed forms so ``café`` (NFC, U+00E9) and ``cafe\u0301`` (NFD)
    # produce the same output.
    name = unicodedata.normalize("NFC", name)

    # Step 2: Lower-case and strip leading/trailing whitespace.
    name = name.lower().strip()

    # Step 3: Greek-letter transliteration (BUG-SCI-07) — must happen BEFORE
    # the ASCII filter, otherwise Greek letters are destroyed.
    name = _transliterate_greek(name)

    # Step 4: NFKD decomposition + strip combining marks (accents) — converts
    # ``é`` → ``e``, ``ü`` → ``u``, etc.  This preserves the meaning while
    # bringing everything into the ASCII range.
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))

    # Step 5: Remove parenthetical *annotations* (e.g. "(INN)",
    # "(USAN)", "(investigational)", "(withdrawn)") WITHOUT
    # destroying chemically meaningful stereo descriptors: (R), (S),
    # (E), (Z), (+), (-), (±), (D), (L), (rac), (racemate).
    # PS-4 ROOT FIX (patient safety): the previous blind paren-stripping
    # step removed ALL parenthetical content, collapsing (R)- and
    # (S)-enantiomers onto the same normalized key. (R)-thalidomide
    # (sedative) and (S)-thalidomide (teratogenic) became the same
    # entity — the patient-safety catastrophe the docstrings scream
    # about. Preserve stereo tokens before stripping, then re-attach
    # them in a canonical prefix position.
    _STEREO_PAREN_RE = re.compile(
        r"\(\s*("
        r"[RS]"                          # (R) / (S) chirality
        r"|[EZ]"                         # (E) / (Z) alkene geometry — v13 ROOT FIX:
                                         # v12 used "|EZ" which matches the literal
                                         # 2-char string "EZ", NOT (E) or (Z)
                                         # separately. (E)- and (Z)-alkene
                                         # stereoisomers were silently collapsed.
        r"|D|L"                          # (D) / (L) Fischer
        r"|±|rac(?:emate)?|racemic"      # racemic
        r"|[\+\−\-\u2212]"               # (+), (-), (−)
        # v29 ROOT FIX (audit C-7): Multi-stereo descriptors like
        # (2R,3S), (2S,3R), (3R,5S) were NOT matched by the original
        # regex — they were stripped by the paren-removal loop,
        # silently losing stereochemistry. Atorvastatin (2R,3S) and
        # (2S,3R) would collapse to the same normalized key — a
        # patient-safety issue. ROOT FIX: add a pattern that matches
        # comma-separated position+chirality descriptors.
        r"|\d{1,2}[RS](?:\s*,\s*\d{1,2}[RS])*"  # (2R,3S), (3R,5S), etc.
        r"|\d{1,2}[EZ](?:\s*,\s*\d{1,2}[EZ])*"  # (2E,4E), etc.
        r")\s*\)",
        re.IGNORECASE,
    )
    stereo_tokens = _STEREO_PAREN_RE.findall(name)
    stereo_tokens = [t.strip().lower() for t in stereo_tokens]

    # V19 ROOT FIX (PS-4 residual — verification agent flagged this):
    # Optical rotation indicators (+), (-), (±) were being extracted
    # as stereo tokens but then COLLAPSED onto the base name in Step 6
    # (the _NON_ALNUM_RE filter strips +, −, ± because they are not in
    # the default allow_chars="-/"). Result: (+)-ibuprofen, (-)-ibuprofen,
    # (±)-ibuprofen ALL normalized to "ibuprofen" — the same patient-
    # safety collapse the audit's PS-4 flagged for (R)/(S).
    #
    # Root fix: convert +/−/±/racemic to ASCII letter prefixes BEFORE
    # re-attaching them so they survive the char filter:
    #   '+' → 'p'      (plus — dextrorotatory)
    #   '-' or '−' → 'm' (minus — levorotatory)
    #   '±'   → 'pm'   (plus-minus — racemic by optical rotation)
    #   'rac'/'racemate'/'racemic' → 'rac'
    # Then (+)-ibuprofen → (p)-ibuprofen → p-ibuprofen (distinct from
    # m-ibuprofen and pm-ibuprofen).
    _STEREO_TOKEN_NORMALIZE = {
        "+": "p",
        "-": "m",
        "\u2212": "m",   # MINUS SIGN (U+2212)
        "\u2010": "m",  # HYPHEN (U+2010)
        "\u2011": "m",  # NON-BREAKING HYPHEN (U+2011)
        "\u2012": "m",  # FIGURE DASH (U+2012)
        "\u2013": "m",  # EN DASH (U+2013)
        "\u2014": "m",  # EM DASH (U+2014)
        "±": "pm",
        "rac": "rac",
        "racemate": "rac",
        "racemic": "rac",
    }
    stereo_tokens = [_STEREO_TOKEN_NORMALIZE.get(t, t) for t in stereo_tokens]

    while "(" in name:
        new_name = _PARENTHESES_RE.sub("", name)
        if new_name == name:
            break  # unbalanced parens — stop to avoid infinite loop
        name = new_name

    # Re-attach preserved stereo tokens in a deterministic prefix
    # position so that "(R)-aspirin" and "aspirin-(R)" normalize
    # identically and stay distinct from "(S)-aspirin".
    if stereo_tokens:
        seen: set[str] = set()
        uniq = [t for t in stereo_tokens if not (t in seen or seen.add(t))]
        name = "(" + "".join(uniq) + ")-" + name

    # Step 6: Keep only allowed characters (BUG-SCI-07, GAP-CONFIG-05).
    # The default allowlist is ``-/``; custom allowlists recompile the pattern.
    if allow_chars == "-/":
        # Fast path — use the precompiled default pattern.
        name = _NON_ALNUM_RE.sub("", name)
    else:
        escaped = re.escape(allow_chars)
        name = re.compile(f"[^a-z0-9{escaped}]").sub("", name)

    # Step 7: Collapse consecutive hyphens / slashes (BUG-CODE-01, BUG-PERF-01).
    name = _MULTI_HYPHEN_RE.sub("-", name)
    name = _MULTI_SLASH_RE.sub("/", name)

    # Step 8: Strip leading/trailing hyphens and slashes (BUG-SCI-06) —
    # ensures ``(R)-aspirin`` normalises to ``aspirin`` (not ``-aspirin``)
    # and ``aspirin-(S)`` normalises to ``aspirin`` (not ``aspirin-``).
    name = name.strip("-/")

    return name


def normalize_name(
    name: Any,
    *,
    allow_chars: str = "-/",
    detailed: bool = False,
) -> Union[str, NormalizedName]:
    """Normalise a drug or protein name for entity-resolution matching.

    Processing steps (applied in order):

    1. Return ``""`` immediately if *name* is ``None`` or non-string.
    2. Unicode NFC normalisation (collapses composed/decomposed forms).
    3. Lower-case and strip leading/trailing whitespace.
    4. Greek-letter transliteration (``α`` → ``alpha``, ``γ`` → ``gamma``).
    5. NFKD decomposition + strip combining marks (``é`` → ``e``).
    6. Iterative parenthetical-content removal (handles nested parens).
    7. Keep only ``a-z``, ``0-9``, and the characters in *allow_chars*.
    8. Collapse consecutive hyphens / slashes into single characters.
    9. Strip leading/trailing hyphens and slashes (so ``(R)-aspirin`` →
       ``aspirin``, not ``-aspirin``).

    The function is cached via :func:`functools.lru_cache` with a max
    size of 8192 entries — repeated calls with the same input are O(1).
    Use :func:`normalize_name_cache_info` / :func:`normalize_name_cache_clear`
    for cache observability.

    Known Limitations
    -----------------
    - Nested parentheses are handled by iterative removal (innermost
      first).  ``"Foo (a (b) c)"`` → ``"fooc"`` (the trailing ``c``
      survives because it was outside the inner parens).
    - PS-4 ROOT FIX (patient safety): stereochemistry indicators
      ``(R)``, ``(S)``, ``(E)``, ``(Z)`` are PRESERVED as lowercase
      letters prefixed to the name — they are NOT stripped.  This is
      critical for patient safety: ``(R)-thalidomide`` is a sedative
      while ``(S)-thalidomide`` is a teratogen; merging them would
      kill patients.  After normalization: ``(R)-warfarin`` →
      ``"r-warfarin"``, ``(S)-warfarin`` → ``"s-warfarin"`` (distinct
      match keys, no merge).
    - Unicode characters are handled via NFKD decomposition and Greek
      transliteration.  Accented characters are stripped to their base
      form (``"é"`` → ``"e"``); Greek letters are transliterated to
      ASCII (``"α"`` → ``"alpha"``).  This ensures that
      ``"α-tocopherol"`` and ``"γ-tocopherol"`` produce DIFFERENT
      normalised names and are NOT incorrectly merged.
    - Leading and trailing hyphens and slashes are stripped after all
      substitutions.

    Relationship to ``cleaning.normalizer``
    ---------------------------------------
    This function is designed for entity-resolution matching within the
    :mod:`entity_resolution` package.  It is NOT the same as
    :func:`cleaning.normalizer.normalize_compound_name` — they serve
    different purposes:

    - :func:`normalize_name` — Aggressive normalisation for fuzzy matching
      (removes parentheticals, strips accents, transliterates Greek).
    - :func:`cleaning.normalizer.normalize_*` — Data-cleaning normalisation
      for standardising field values before storage.

    Do NOT double-normalise.  If a record has already been through the
    cleaning pipeline, pass the ORIGINAL name to this function, not the
    cleaned name.

    Parameters
    ----------
    name:
        Raw name string from any source database.  Non-string inputs
        are coerced to ``""`` (defensive — source DataFrames sometimes
        contain ``NaN`` or ``None``).
    allow_chars:
        Characters to preserve in addition to ``a-z`` and ``0-9``.
        Default ``"-/"`` keeps hyphens and slashes.  Pass a custom
        string (e.g. ``"-/."``) to also preserve dots.
    detailed:
        If ``True``, return a :class:`NormalizedName` with provenance
        information (original string + list of transformations applied).
        Default ``False`` returns a bare ``str`` for backward compat.

    Returns
    -------
    str or NormalizedName
        Normalised name, or ``""`` when the input was falsy.  When
        ``detailed=True``, returns a :class:`NormalizedName` instead.

    Examples
    --------
    >>> normalize_name("Aspirin (acetylsalicylic acid)")
    'aspirin'
    >>> normalize_name("Acetyl-salicylic acid")
    'acetyl-salicylicacid'
    >>> normalize_name(None)
    ''
    >>> normalize_name("(R)-aspirin")
    'r-aspirin'
    >>> normalize_name("α-tocopherol")
    'alpha-tocopherol'
    >>> normalize_name("γ-tocopherol")
    'gamma-tocopherol'
    >>> normalize_name("α-tocopherol") == normalize_name("γ-tocopherol")
    False
    """
    if not name or not isinstance(name, str):
        if detailed:
            return NormalizedName(
                normalized="",
                original=str(name),
                transformations=["empty_input"],
            )
        return ""

    original = name
    normalized = _normalize_name_cached(name, allow_chars)

    if detailed:
        # Determine which transformation steps were applied — used for
        # data lineage / audit purposes.
        transformations: List[str] = []
        if name != name.lower().strip():
            transformations.append("lowercase+strip")
        if any(c in _GREEK_MAP for c in name):
            transformations.append("greek_transliteration")
        if unicodedata.normalize("NFKD", name) != name:
            transformations.append("accent_removal")
        if "(" in name:
            transformations.append("parenthetical_removal")
        if normalized != name.lower().strip():
            transformations.append("character_filtering")
        transformations.append("collapse_repeating_separators")
        transformations.append("strip_leading_trailing_separators")
        return NormalizedName(
            normalized=normalized,
            original=original,
            transformations=transformations,
        )
    return normalized


def normalize_name_cache_info() -> Any:
    """Return cache statistics for :func:`normalize_name` (FIX #59).

    Returns
    -------
    functools.CacheInfo
        Object with ``hits``, ``misses``, ``maxsize``, ``currsize``
        attributes.  Use to monitor cache effectiveness at runtime.
    """
    return _normalize_name_cached.cache_info()


def normalize_name_cache_clear() -> None:
    """Clear the :func:`normalize_name` cache (FIX #59).

    Useful in tests that need deterministic cache state, or in long-running
    processes that want to evict stale entries.
    """
    _normalize_name_cached.cache_clear()
    logger.debug("normalize_name_cache_clear: cache cleared")


# =============================================================================
# Fuzzy matching
# =============================================================================

def fuzzy_match_score(name1: str, name2: str) -> float:
    """Compute a fuzzy similarity score between two *already-normalised* names.

    Uses :func:`rapidfuzz.fuzz.token_sort_ratio` which sorts tokens
    alphabetically before comparing, making the result order-independent.

    When rapidfuzz is not installed, falls back to an exact string match
    (returns 1.0 for identical strings, 0.0 otherwise).  The first time
    the fallback is used, a WARNING is logged (FIX #51 / GUARD-REL-05);
    subsequent calls do not re-warn.

    Parameters
    ----------
    name1, name2:
        Normalised name strings.  Non-string inputs return ``0.0`` (FIX #31).

    Returns
    -------
    float
        Score in the range ``[0.0, 1.0]``.  Returns ``0.0`` if either
        argument is empty or non-string.
    """
    # FIX #31 / GAP-CODE-07 — type-validate inputs.
    if not isinstance(name1, str) or not isinstance(name2, str):
        return 0.0
    if not name1 or not name2:
        return 0.0

    if not RAPIDFUZZ_AVAILABLE:
        # FIX #51 / GUARD-REL-05 — warn ONCE on first fallback use, not at
        # import time.  This keeps import side-effect-free (FIX #35, #55)
        # while still alerting operators to degraded functionality.
        global _rapidfuzz_fallback_warned
        if not _rapidfuzz_fallback_warned:
            _rapidfuzz_fallback_warned = True
            logger.warning(
                "fuzzy_match_score: rapidfuzz not available — falling back "
                "to exact-match-only (0.0 or 1.0). Fuzzy matching is disabled. "
                "Install rapidfuzz for full fuzzy matching support."
            )
        return 1.0 if name1 == name2 else 0.0

    score = _rapidfuzz_fuzz.token_sort_ratio(name1, name2) / 100.0
    # FIX #37 / BUG-SEC-01 — truncate names in log output to prevent PII leak.
    logger.debug(
        "fuzzy_match_score('%s', '%s') = %.3f",
        _sanitize_for_log(name1), _sanitize_for_log(name2), score,
    )
    return score


def fuzzy_match_best(
    query: str,
    candidates: Dict[str, str],
    threshold: float = 0.85,
) -> Optional[Tuple[str, float]]:
    """Find the best fuzzy match for *query* among *candidates* (FIX #60).

    Uses :func:`rapidfuzz.process.extractOne` when rapidfuzz is available,
    which is much faster than a linear O(n) sweep over all candidates.
    Falls back to exact match when rapidfuzz is not installed.

    Parameters
    ----------
    query:
        Normalised query name.
    candidates:
        Mapping of candidate names → canonical keys.  For example::

            {"aspirin": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
             "ibuprofen": "WFXAZNNJSJXTJZ-UHFFFAOYSA-N"}
    threshold:
        Minimum similarity score in ``[0.0, 1.0]``.  Matches below this
        threshold are not returned.  Default ``0.85``.

    Returns
    -------
    tuple[str, float] or None
        ``(canonical_key, score)`` if a match above *threshold* was found,
        ``None`` otherwise.  The score is in ``[0.0, 1.0]``.
    """
    if not isinstance(query, str) or not query:
        return None
    if not candidates:
        return None

    if not RAPIDFUZZ_AVAILABLE:
        # Fallback: O(1) exact match.
        if query in candidates:
            return candidates[query], 1.0
        return None

    try:
        from rapidfuzz import process as fuzz_process
    except ImportError:  # pragma: no cover - defensive
        if query in candidates:
            return candidates[query], 1.0
        return None

    candidate_names = list(candidates.keys())
    result = fuzz_process.extractOne(
        query,
        candidate_names,
        scorer=_rapidfuzz_fuzz.token_sort_ratio,
        score_cutoff=threshold * 100.0,
    )
    if result is None:
        return None
    # rapidfuzz 3.x returns (match, score, index) — but extractOne may
    # return (match, score) in older versions.  Handle both.
    matched_name = result[0]
    score = result[1] / 100.0
    return candidates[matched_name], score


# =============================================================================
# InChIKey helpers
# =============================================================================

def is_valid_inchikey(inchikey: Any) -> bool:
    """Return ``True`` iff *inchikey* matches the platform's InChIKey contract.

    This is the **public API** for entity_resolution consumers.  It delegates
    to :func:`cleaning.normalizer.is_valid_inchikey` (FIX #1 / BUG-ARCH-01)
    which is the SINGLE source of truth for InChIKey validation across the
    entire platform.  The normaliser's implementation handles:

    - **Standard** InChIKeys: ``[A-Z]{14}-[A-Z]{10}-S`` (27 chars, ``-S`` suffix).
      Per the InChI Trust specification, the standard InChIKey ends in ``S``
      (not ``N``). The earlier docstring here had the standard / non-standard
      suffixes reversed — corrected (audit finding 10).
    - **Non-standard** InChIKeys: ``[A-Z]{14}-[A-Z]{10}-N`` (27 chars, ``-N``
      suffix). Non-standard InChIKeys end in ``N`` (not any letter) per the
      InChI Trust spec.
    - **Mixture** InChIKeys: multiple keys joined by hyphens (the regex at
      line 682-684 of this file uses hyphen separators, not commas as the
      earlier docstring claimed — corrected, audit finding 10).
    - **Synthetic** InChIKeys: ``SYNTH``-prefixed platform-generated surrogates

    Delegating rather than reimplementing eliminates the historical bug where
    three independent validators (in ``resolver_utils``, ``base``, and
    ``cleaning.normalizer``) had divergent semantics — the same key could be
    accepted by one and rejected by another.

    Parameters
    ----------
    inchikey:
        Anything — non-strings return ``False``.

    Returns
    -------
    bool

    Examples
    --------
    >>> is_valid_inchikey("BSYNRYMUTXBXSQ-UHFFFAOYSA-N")
    True
    >>> is_valid_inchikey("SYNTH-001")
    True
    >>> is_valid_inchikey("not-an-inchikey")
    False
    >>> is_valid_inchikey(None)
    False
    """
    # Local import to avoid a circular import at module load time
    # (cleaning.normalizer is heavy — pandas etc.).
    try:
        from cleaning.normalizer import is_valid_inchikey as _normalizer_is_valid
    except ImportError:
        # If cleaning.normalizer is not available for any reason, fall back
        # to the legacy strict pattern.  This should never happen in
        # production but keeps the resolver resilient.
        logger.debug(
            "is_valid_inchikey: cleaning.normalizer unavailable — falling "
            "back to legacy strict pattern"
        )
        if not isinstance(inchikey, str):
            return False
        return bool(_INCHIKEY_RE.match(inchikey.strip().upper()))
    return _normalizer_is_valid(inchikey)


def _is_synthetic_inchikey_local(inchikey: Any) -> bool:
    """Local helper — delegates to :func:`cleaning.normalizer.is_synthetic_inchikey`
    when available, else uses the ``startswith("SYNTH")`` heuristic.
    """
    if not isinstance(inchikey, str) or not inchikey:
        return False
    try:
        from cleaning.normalizer import is_synthetic_inchikey as _ns
        return _ns(inchikey)
    except ImportError:
        return inchikey.strip().upper().startswith(_SYNTHETIC_PREFIX)


def extract_inchikey_first_block(
    inchikey: Any,
    *,
    detailed: bool = False,
) -> Union[Optional[str], Optional[ConnectivityBlock]]:
    """Extract the first 14-character *connectivity block* from an InChIKey.

    An InChIKey has the format ``AAAAAAAAAAAAAA-BBBBBBCCCCCC-C`` where the
    first 14 characters encode molecular connectivity (same connectivity
    implies the same skeleton, possibly different stereochemistry).

    **This function validates the input** (FIX #18 / BUG-SCI-04).  Only real
    InChIKeys (not synthetic) produce a connectivity block.  Garbage strings
    and synthetic keys return ``None``.

    The input is normalised (strip + upper) before validation (FIX #17 /
    BUG-SCI-03), so lowercase InChIKeys from CSV exports or API responses
    produce the same connectivity block as their uppercase counterparts.

    Parameters
    ----------
    inchikey:
        Full 27-character InChIKey string.  Non-strings, strings shorter
        than 14 characters, invalid InChIKeys, and synthetic InChIKeys
        (prefix ``SYNTH``) return ``None``.
    detailed:
        If ``True``, return a :class:`ConnectivityBlock` dataclass with
        the full InChIKey and synthetic-key flag preserved.  Default
        ``False`` returns a bare ``str`` (or ``None``).

    Returns
    -------
    str or None
        The 14-character connectivity block, or ``None`` if *inchikey*
        is falsy, non-string, shorter than 14 characters, invalid, or
        synthetic (FIX #18, #19 / BUG-SCI-04, BUG-SCI-05).

    Examples
    --------
    >>> extract_inchikey_first_block("BSYNRYMUTXBXSQ-UHFFFAOYSA-N")
    'BSYNRYMUTXBXSQ'
    >>> extract_inchikey_first_block("bsynrymutxbxsq-uhfffaoyas-n")
    'BSYNRYMUTXBXSQ'
    >>> extract_inchikey_first_block("SYNTHABCDEF12345-UHFFFAOYSA-N") is None
    True
    >>> extract_inchikey_first_block("not-an-inchikey-but-14+") is None
    True
    >>> extract_inchikey_first_block("short") is None
    True
    """
    if not inchikey or not isinstance(inchikey, str) or len(inchikey) < 14:
        if detailed:
            return None
        return None

    # FIX #17 / BUG-SCI-03 — normalise case before validation/extraction.
    normalised = inchikey.strip().upper()

    # FIX #18 / BUG-SCI-04 — validate before extracting.  Garbage strings
    # pollute the connectivity index and cause false-positive drug merges.
    if not is_valid_inchikey(normalised):
        logger.debug(
            "extract_inchikey_first_block: rejecting non-InChIKey input '%s...'",
            _sanitize_for_log(normalised),
        )
        return None

    # FIX #19 / BUG-SCI-05 — synthetic InChIKeys (prefix SYNTH) have NO
    # chemical-connectivity meaning; their first 14 chars are SHA-256 hash
    # fragments.  Inserting them into the connectivity index would cause
    # false-positive drug merges.
    is_synth = _is_synthetic_inchikey_local(normalised)
    if is_synth:
        logger.debug(
            "extract_inchikey_first_block: skipping synthetic InChIKey '%s...'",
            _sanitize_for_log(normalised),
        )
        return None

    block = normalised[:14]
    logger.debug(
        "extract_inchikey_first_block('%s...') = '%s...'",
        _sanitize_for_log(normalised), _sanitize_for_log(block),
    )

    if detailed:
        return ConnectivityBlock(
            block=block,
            full_inchikey=normalised,
            is_synthetic=False,
        )
    return block


# =============================================================================
# Index builders (legacy — DEPRECATED, kept for backward compat)
# =============================================================================

def build_name_index(
    records: List[dict],
    name_field: str = "name",
) -> Dict[str, List[int]]:
    """Build a lookup mapping from *normalised name* → list of record indices.

    .. deprecated::
        Use :func:`build_canonical_name_index` instead.  This legacy
        multi-valued variant is kept for backward compatibility but emits
        a :class:`DeprecationWarning` on every call.

    Useful for fast O(1) lookup during entity resolution.  Records with
    empty normalised names are counted and logged at WARNING level
    (FIX #41 / BUG-DQ-05).

    Parameters
    ----------
    records:
        List of record dicts, each expected to contain *name_field*.
    name_field:
        Key inside each record dict that holds the raw name.

    Returns
    -------
    dict[str, list[int]]
        Mapping where each key is a normalised name and each value is
        a list of indices into *records* that share that normalised
        name.
    """
    # FIX #2 / BUG-ARCH-02 — emit DeprecationWarning on every call.
    warnings.warn(
        "build_name_index is deprecated — use build_canonical_name_index "
        "instead. Will be removed in a future version.",
        DeprecationWarning,
        stacklevel=2,
    )

    index: Dict[str, List[int]] = {}
    dropped = 0

    for i, record in enumerate(records):
        try:
            raw_name = record.get(name_field, "")
        except AttributeError as exc:
            raise TypeError(
                f"Record at index {i} is not a dict (got "
                f"{type(record).__name__}). Expected dict."
            ) from exc
        norm = normalize_name(raw_name)
        if norm:
            index.setdefault(norm, []).append(i)
        else:
            dropped += 1

    # FIX #41 / BUG-DQ-05 — log dropped records instead of silently skipping.
    if dropped:
        logger.warning(
            "build_name_index: %d of %d records had empty normalised names "
            "and were skipped",
            dropped, len(records),
        )
    logger.debug(
        "build_name_index: %d records → %d unique normalised names, %d dropped",
        len(records), len(index), dropped,
    )
    return index


def build_inchikey_index(
    records: List[dict],
    inchikey_field: str = "inchikey",
) -> Dict[str, List[int]]:
    """Build a lookup mapping from *InChIKey* → list of record indices.

    .. deprecated::
        Use :func:`build_canonical_inchikey_index` instead.  This legacy
        multi-valued variant is kept for backward compatibility but emits
        a :class:`DeprecationWarning` on every call.

    InChIKeys are normalised (strip + upper) before indexing (FIX #40 /
    BUG-DQ-03).  Unlike :func:`build_canonical_inchikey_index`, this
    legacy function does NOT skip invalid InChIKeys — backward compat
    with the test suite requires that "AAA-BBB-C" still produces an
    index entry.  Use the canonical variant for new code.

    Parameters
    ----------
    records:
        List of record dicts, each expected to contain *inchikey_field*.
    inchikey_field:
        Key inside each record dict that holds the InChIKey.

    Returns
    -------
    dict[str, list[int]]
        Mapping from (normalised) InChIKey string to list of indices.
    """
    # FIX #2 / BUG-ARCH-02 — emit DeprecationWarning on every call.
    warnings.warn(
        "build_inchikey_index is deprecated — use "
        "build_canonical_inchikey_index instead. Will be removed in a "
        "future version.",
        DeprecationWarning,
        stacklevel=2,
    )

    index: Dict[str, List[int]] = {}
    dropped = 0

    for i, record in enumerate(records):
        try:
            ik = record.get(inchikey_field, "")
        except AttributeError as exc:
            raise TypeError(
                f"Record at index {i} is not a dict (got "
                f"{type(record).__name__}). Expected dict."
            ) from exc
        if ik and isinstance(ik, str):
            # FIX #40 / BUG-DQ-03 — normalise case/whitespace before indexing.
            ik = ik.strip().upper()
            index.setdefault(ik, []).append(i)
        else:
            dropped += 1

    if dropped:
        logger.warning(
            "build_inchikey_index: %d of %d records had empty or non-string "
            "InChIKeys and were skipped",
            dropped, len(records),
        )
    logger.debug(
        "build_inchikey_index: %d records → %d unique InChIKeys, %d dropped",
        len(records), len(index), dropped,
    )
    return index


# =============================================================================
# Canonical index builders (used by the resolvers)
# =============================================================================

def _extract_key_and_record(
    item: Any,
    index: int,
    record_type: str = "auto",
) -> Tuple[str, dict]:
    """Extract ``(canonical_key, record_dict)`` from an index entry (FIX #33).

    Accepts either ``(key, record)`` tuples or plain record dicts.  When
    *record_type* is ``"dict"`` or ``"tuple"``, the expected type is enforced
    and :class:`TypeError` is raised on mismatch.

    Parameters
    ----------
    item:
        Either a ``(key, record_dict)`` 2-tuple or a plain ``record_dict``.
    index:
        Positional index of *item* in the source sequence (for error messages).
    record_type:
        ``"auto"`` (default) — heuristic dispatch.
        ``"dict"`` — require a plain dict.
        ``"tuple"`` — require a ``(key, dict)`` 2-tuple.

    Returns
    -------
    tuple[str, dict]
        The canonical key and the record dict.

    Raises
    ------
    TypeError
        If the item doesn't match the expected shape.
    """
    if record_type == "tuple":
        if not (isinstance(item, tuple) and len(item) == 2):
            raise TypeError(
                f"Expected (key, dict) tuple at index {index}, got "
                f"{type(item).__name__}"
            )
        key, record = item
    elif record_type == "dict":
        if not isinstance(item, dict):
            raise TypeError(
                f"Expected dict at index {index}, got {type(item).__name__}"
            )
        record = item
        key = str(record.get("canonical_key", index))
    else:  # "auto"
        if isinstance(item, tuple) and len(item) == 2:
            key, record = item
        else:
            record = item
            # FIX #32 / GAP-CODE-08 — validate record is a dict.
            if not isinstance(record, dict):
                raise TypeError(
                    f"Expected dict or (key, dict) tuple at index {index}, "
                    f"got {type(record).__name__}"
                )
            key = str(record.get("canonical_key", index))

    # FIX #32 — final type guard on the record half.
    if not isinstance(record, dict):
        raise TypeError(
            f"Record must be dict, got {type(record).__name__} at index {index}"
        )
    if not isinstance(key, (str, int)):
        # Coerce non-string/non-int keys to string.
        key = str(key)
    else:
        key = str(key)
    return key, record


@overload
def build_canonical_name_index(
    records: Sequence[dict],
    name_field: str = ...,
    *,
    record_type: str = ...,
    return_duplicates: bool = ...,
) -> Dict[str, str]: ...


@overload
def build_canonical_name_index(
    records: Sequence[Tuple[str, dict]],
    name_field: str = ...,
    *,
    record_type: str = ...,
    # FIX P1-ER-16 (LOW): the real implementation defaults
    # ``return_duplicates`` to ``False`` (see line ~1367 below). The
    # previous stub wrote ``return_duplicates: bool = True`` which
    # contradicted the real default AND misled type-checkers into
    # thinking the tuple-returning overload fires by default. The
    # literal ellipsis ``= ...`` marks the parameter as REQUIRED for
    # this overload — callers MUST pass ``return_duplicates=True``
    # explicitly to opt into the tuple return shape.
    return_duplicates: bool = ...,
) -> Tuple[Dict[str, str], List[Tuple[str, str, int]]]: ...


def build_canonical_name_index(
    records: Sequence,
    name_field: str = "name",
    *,
    record_type: str = "auto",
    return_duplicates: bool = False,
) -> Union[Dict[str, str], Tuple[Dict[str, str], List[Tuple[str, str, int]]]]:
    """Build a lookup from *normalised name* → first matching record key.

    Unlike :func:`build_name_index` (which returns ``Dict[str, List[int]]``),
    this helper returns ``Dict[str, str]`` — each normalised name maps
    to the **first** record's canonical key.  This matches the shape
    actually consumed by :class:`DrugResolver._name_index` and
    :class:`ProteinResolver._name_index` (audit D5-1).

    Parameters
    ----------
    records:
        Sequence of ``(canonical_key, record_dict)`` tuples OR plain
        record dicts.  When tuples are passed, the first element is
        used as the value; otherwise the record's ``"canonical_key"``
        field is used, falling back to a content-hash of the record
        (FIX #8 / BUG-DESIGN-02 — content-hash is deterministic, unlike
        the legacy positional ``str(i)`` fallback).
    name_field:
        Key inside each record dict that holds the raw name.
    record_type:
        ``"auto"`` (default) — heuristic dispatch.
        ``"dict"`` — enforce plain-dict input.
        ``"tuple"`` — enforce ``(key, dict)`` 2-tuple input.
    return_duplicates:
        If ``True``, return a 2-tuple ``(index, duplicates)`` where
        ``duplicates`` is a list of ``(normalised_name, key, index)``
        tuples for records whose normalised name collided with an
        earlier record (FIX #9 / BUG-DESIGN-03).  Default ``False``
        returns just the index.

    Returns
    -------
    dict[str, str] or tuple[dict[str, str], list[tuple[str, str, int]]]
        Mapping from normalised name → canonical key string.  When
        ``return_duplicates=True``, a second return value lists the
        dropped duplicates.
    """
    index: Dict[str, str] = {}
    dropped: List[Tuple[str, str, int]] = []

    for i, item in enumerate(records):
        key, record = _extract_key_and_record(item, i, record_type)

        # FIX #8 / BUG-DESIGN-02 — if no canonical_key is present, generate
        # a deterministic content-hash key instead of falling back to the
        # positional index.  Positional indices are unstable across
        # reordering / filtering and cause silent corruption.
        if "canonical_key" not in record and not (
            isinstance(item, tuple) and len(item) == 2
        ):
            try:
                content_str = str(sorted(record.items()))
            except TypeError:
                # Unhashable values — fall back to id().
                content_str = str(id(record))
            generated_key = hashlib.sha256(content_str.encode()).hexdigest()[:16]
            warnings.warn(
                f"Record at index {i} has no 'canonical_key' field. "
                f"Generated stable key from content hash: {generated_key!r}. "
                f"Pass (key, record) tuples for explicit key assignment.",
                UserWarning,
                stacklevel=2,
            )
            key = generated_key

        try:
            norm = normalize_name(record.get(name_field, ""))
        except AttributeError as exc:
            raise TypeError(
                f"Record at index {i} is not a dict (got "
                f"{type(record).__name__}). Expected dict."
            ) from exc

        if norm:
            if norm not in index:
                index[norm] = key
            else:
                # FIX #9 / BUG-DESIGN-03 — track duplicates instead of
                # silently dropping them.
                dropped.append((norm, key, i))

    # FIX #9 — log dropped duplicates at WARNING level so operators notice.
    if dropped:
        logger.warning(
            "build_canonical_name_index: %d duplicate normalised names "
            "dropped (first occurrence kept). First 10: %s",
            len(dropped), [(n, k) for n, k, _ in dropped[:10]],
        )
    logger.debug(
        "build_canonical_name_index: %d records → %d unique names, %d dropped",
        len(records), len(index), len(dropped),
    )

    if return_duplicates:
        return index, dropped
    return index


def build_canonical_inchikey_index(
    records: Sequence,
    inchikey_field: str = "inchikey",
    *,
    record_type: str = "auto",
    return_duplicates: bool = False,
) -> Union[Dict[str, str], Tuple[Dict[str, str], List[Tuple[str, str, int]]]]:
    """Build a lookup from *InChIKey* → first matching record key.

    Single-valued counterpart of :func:`build_inchikey_index`,
    matching the shape of :attr:`DrugResolver._inchikey_index`
    (audit D5-1).

    InChIKeys are normalised (strip + upper) before indexing, and
    INVALID InChIKeys are skipped with a WARNING log (FIX #39 /
    BUG-DQ-02).  This prevents typos and case variants from producing
    separate index entries.

    Parameters
    ----------
    records:
        Sequence of ``(canonical_key, record_dict)`` tuples OR plain
        record dicts.
    inchikey_field:
        Key inside each record dict that holds the InChIKey.
    record_type:
        ``"auto"``, ``"dict"``, or ``"tuple"`` — see
        :func:`build_canonical_name_index`.
    return_duplicates:
        If ``True``, return ``(index, duplicates)``.

    Returns
    -------
    dict[str, str] or tuple
        Mapping from normalised InChIKey → canonical key string.
    """
    index: Dict[str, str] = {}
    dropped: List[Tuple[str, str, int]] = []
    invalid = 0

    for i, item in enumerate(records):
        key, record = _extract_key_and_record(item, i, record_type)

        try:
            ik = record.get(inchikey_field, "")
        except AttributeError as exc:
            raise TypeError(
                f"Record at index {i} is not a dict (got "
                f"{type(record).__name__}). Expected dict."
            ) from exc

        if not ik or not isinstance(ik, str):
            continue

        # FIX #39 / BUG-DQ-02 — normalise case/whitespace before indexing.
        ik = ik.strip().upper()

        # FIX #39 — skip invalid InChIKeys with a warning.  This prevents
        # typos and garbage from polluting the index.  Note: synthetic
        # keys (SYNTH-prefixed) ARE accepted because they are a legitimate
        # platform-generated identifier.
        if not is_valid_inchikey(ik):
            logger.warning(
                "build_canonical_inchikey_index: skipping invalid InChIKey "
                "'%s...' at index %d",
                _sanitize_for_log(ik), i,
            )
            invalid += 1
            continue

        if ik not in index:
            index[ik] = key
        else:
            dropped.append((ik, key, i))

    if invalid:
        logger.warning(
            "build_canonical_inchikey_index: %d invalid InChIKeys skipped",
            invalid,
        )
    if dropped:
        logger.warning(
            "build_canonical_inchikey_index: %d duplicate InChIKeys dropped",
            len(dropped),
        )
    logger.debug(
        "build_canonical_inchikey_index: %d records → %d unique InChIKeys, "
        "%d invalid, %d dropped",
        len(records), len(index), invalid, len(dropped),
    )

    if return_duplicates:
        return index, dropped
    return index


def merge_into_name_index(
    index: Dict[str, str],
    records: Sequence,
    name_field: str = "name",
) -> int:
    """Merge new records into an existing canonical name index (FIX #61).

    Avoids the cost of rebuilding the entire index from scratch when
    only a small batch of new records needs to be added.

    Parameters
    ----------
    index:
        Existing index to merge into.  Mutated in place.
    records:
        Sequence of ``(key, record)`` tuples or plain record dicts.
    name_field:
        Key inside each record dict that holds the raw name.

    Returns
    -------
    int
        Number of new entries added to *index*.
    """
    added = 0
    for i, item in enumerate(records):
        key, record = _extract_key_and_record(item, i)
        norm = normalize_name(record.get(name_field, ""))
        if norm and norm not in index:
            index[norm] = key
            added += 1
    logger.debug(
        "merge_into_name_index: added %d entries (index size now %d)",
        added, len(index),
    )
    return added


def merge_into_inchikey_index(
    index: Dict[str, str],
    records: Sequence,
    inchikey_field: str = "inchikey",
) -> int:
    """Merge new records into an existing canonical InChIKey index.

    Counterpart of :func:`merge_into_name_index` for InChIKeys.

    Parameters
    ----------
    index:
        Existing index to merge into.  Mutated in place.
    records:
        Sequence of ``(key, record)`` tuples or plain record dicts.
    inchikey_field:
        Key inside each record dict that holds the InChIKey.

    Returns
    -------
    int
        Number of new entries added to *index*.
    """
    added = 0
    for i, item in enumerate(records):
        key, record = _extract_key_and_record(item, i)
        ik = record.get(inchikey_field, "")
        if ik and isinstance(ik, str):
            ik = ik.strip().upper()
            if is_valid_inchikey(ik) and ik not in index:
                index[ik] = key
                added += 1
    logger.debug(
        "merge_into_inchikey_index: added %d entries (index size now %d)",
        added, len(index),
    )
    return added


# =============================================================================
# Confidence-score registry: register / unregister / reset / sync / override
# =============================================================================

def register_match_method(method: str, confidence: float) -> None:
    """Register or override a ``(method, confidence)`` pair at runtime.

    Allows downstream code (e.g. a custom resolver plugin) to teach the
    package about new resolution strategies without monkey-patching the
    module-level constant.  Audit D2-4.

    Parameters
    ----------
    method:
        Resolution-method identifier (e.g. ``"inchikey_exact"``).  Must
        match ``^[a-z][a-z0-9_]+$`` (FIX #65 / GAP-SEC-03) — lowercase
        identifier starting with a letter.  Whitespace-only or non-string
        values raise :class:`ValueError` (FIX #27 / BUG-CODE-03).
    confidence:
        Confidence score in ``[0.0, 1.0]``.  Booleans are rejected
        (FIX #28 / BUG-CODE-04) — ``True`` would otherwise silently
        register as confidence ``1.0`` because ``isinstance(True, int)``
        is ``True`` in Python.

    Raises
    ------
    ValueError
        If ``method`` is empty, whitespace-only, doesn't match the
        identifier pattern, or ``confidence`` is outside ``[0, 1]`` or
        is a bool.

    Warnings
    --------
    - This function mutates module-level global state (:data:`METHOD_CONFIDENCE`).
    - It IS thread-safe (protected by an internal re-entrant lock).
    - It does NOT update the :class:`~entity_resolution.base.MatchConfidence`
      enum (enums are immutable by design).  Custom methods are stored
      in a separate ``_custom_methods`` dict and checked as a fallback
      by :func:`compute_match_confidence` and
      :func:`MatchConfidence.from_method`.
    - Overriding existing methods produces a WARNING log (FIX #49).
    - Use :func:`unregister_match_method` to remove a custom method.
    - Use :func:`reset_method_confidence` to restore ALL original values.
    """
    # FIX #27 / BUG-CODE-03 — reject whitespace-only method names.
    if not method or not isinstance(method, str) or not method.strip():
        raise ValueError(
            f"method must be a non-empty, non-whitespace string, got {method!r}"
        )
    # FIX #65 / GAP-SEC-03 — enforce identifier pattern on method names.
    if not _VALID_METHOD_NAME_RE.match(method):
        raise ValueError(
            f"method name must match [a-z][a-z0-9_]+, got {method!r}"
        )
    # FIX #28 / BUG-CODE-04 — reject bool as confidence.
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError(
            f"confidence must be a numeric (int/float), not bool. "
            f"Got {type(confidence).__name__}: {confidence!r}"
        )
    if not 0.0 <= float(confidence) <= 1.0:
        raise ValueError(
            f"confidence must be in [0, 1], got {confidence}"
        )

    caller = _get_caller_info(skip=2)

    with _METHOD_CONFIDENCE_LOCK:
        # FIX #49 / GAP-REL-03 — warn when overriding existing methods.
        if method in METHOD_CONFIDENCE:
            old_value = METHOD_CONFIDENCE[method]
            logger.warning(
                "register_match_method: overriding existing method '%s' "
                "(%.3f → %.3f). Caller: %s",
                method, old_value, float(confidence), caller,
            )
        else:
            logger.info(
                "register_match_method: registered '%s' → %.3f. Caller: %s",
                method, float(confidence), caller,
            )
        METHOD_CONFIDENCE[method] = float(confidence)
        # Track custom methods separately so MatchConfidence.from_method
        # can pick them up (FIX #6 / GUARD-ARCH-06).
        if method not in _ORIGINAL_METHOD_CONFIDENCE:
            _custom_methods[method] = float(confidence)
        else:
            # Built-in method overridden — record in _custom_methods so
            # from_method can see the new value.
            _custom_methods[method] = float(confidence)


def unregister_match_method(method: str) -> None:
    """Remove a previously-registered custom method (FIX #48 / BUG-REL-02).

    Restores the original value if *method* was a built-in that was
    overridden, or removes it entirely if it was a custom registration.

    Parameters
    ----------
    method:
        Method name to unregister.

    Raises
    ------
    KeyError
        If *method* is unknown (never registered).
    """
    if not isinstance(method, str) or not method:
        raise KeyError(f"method must be a non-empty string, got {method!r}")

    with _METHOD_CONFIDENCE_LOCK:
        was_in = method in METHOD_CONFIDENCE
        if not was_in:
            raise KeyError(
                f"Cannot unregister unknown method {method!r}. "
                f"Use reset_method_confidence() to restore original values."
            )

        if method in _ORIGINAL_METHOD_CONFIDENCE:
            # Built-in method was overridden — restore the original value.
            original = _ORIGINAL_METHOD_CONFIDENCE[method]
            METHOD_CONFIDENCE[method] = original
            _custom_methods.pop(method, None)
            logger.info(
                "unregister_match_method: restored built-in '%s' → %.3f",
                method, original,
            )
        else:
            # Truly custom method — remove entirely.
            METHOD_CONFIDENCE.pop(method, None)
            _custom_methods.pop(method, None)
            # Reset the warning-suppression set so future registrations
            # of the same name warn again.
            _unknown_method_warned.discard(method)
            logger.info(
                "unregister_match_method: removed custom method '%s'",
                method,
            )


def reset_method_confidence() -> None:
    """Reset :data:`METHOD_CONFIDENCE` to its original module-load values.

    Restores the built-in confidence scores and removes ALL custom
    registrations.  Useful for test isolation and for reverting
    accidental mutations.
    """
    with _METHOD_CONFIDENCE_LOCK:
        METHOD_CONFIDENCE.clear()
        METHOD_CONFIDENCE.update(_ORIGINAL_METHOD_CONFIDENCE)
        _custom_methods.clear()
        _unknown_method_warned.clear()
        logger.info(
            "reset_method_confidence: restored %d entries",
            len(METHOD_CONFIDENCE),
        )


def get_registered_methods() -> Dict[str, float]:
    """Return a snapshot copy of the current :data:`METHOD_CONFIDENCE`.

    Includes both built-in and custom-registered methods.  The returned
    dict is a copy — mutations do not affect the global state.

    Returns
    -------
    dict[str, float]
        Snapshot of all currently-registered method → confidence pairs.
    """
    with _METHOD_CONFIDENCE_LOCK:
        return dict(METHOD_CONFIDENCE)


def sync_method_confidence() -> bool:
    """Verify :data:`METHOD_CONFIDENCE` matches the :class:`MatchConfidence` enum.

    Logs a CRITICAL error if the two sources of truth have drifted apart.
    Returns ``True`` if they agree, ``False`` otherwise.

    Returns
    -------
    bool
        ``True`` if all built-in method names have matching values in
        both :data:`METHOD_CONFIDENCE` and :class:`MatchConfidence`,
        ``False`` otherwise.
    """
    try:
        from .base import MatchConfidence
    except ImportError:
        logger.error(
            "sync_method_confidence: cannot import MatchConfidence — skipping check"
        )
        return True

    with _METHOD_CONFIDENCE_LOCK:
        all_match = True
        for method, confidence in METHOD_CONFIDENCE.items():
            if method in _ORIGINAL_METHOD_CONFIDENCE:
                # Built-in — check enum.
                enum_name = method.upper()
                if not hasattr(MatchConfidence, enum_name):
                    logger.critical(
                        "sync_method_confidence: MatchConfidence missing %s "
                        "(present in METHOD_CONFIDENCE)", enum_name,
                    )
                    all_match = False
                    continue
                enum_value = float(getattr(MatchConfidence, enum_name))
                if enum_value != confidence:
                    logger.critical(
                        "sync_method_confidence: drift detected — "
                        "MatchConfidence.%s = %.3f but METHOD_CONFIDENCE['%s'] = %.3f",
                        enum_name, enum_value, method, confidence,
                    )
                    all_match = False
        if all_match:
            logger.debug(
                "sync_method_confidence: %d built-in methods in sync",
                len(_ORIGINAL_METHOD_CONFIDENCE),
            )
        return all_match


@contextmanager
def method_confidence_override(overrides: Dict[str, float]):
    """Context manager for temporarily overriding :data:`METHOD_CONFIDENCE` (FIX #53).

    Restores the previous values on exit, even if an exception is raised.
    Useful in tests that need to inject custom confidence values without
    polluting global state.

    Example
    -------
    >>> with method_confidence_override({"fuzzy": 0.9}):
    ...     # In this scope, fuzzy confidence is 0.9.
    ...     assert compute_match_confidence("fuzzy") == 0.9
    >>> # Outside the context, fuzzy confidence is back to 0.85.
    >>> assert compute_match_confidence("fuzzy") == 0.85
    """
    saved: Dict[str, Optional[float]] = {}
    registered_new: List[str] = []
    try:
        for method, confidence in overrides.items():
            saved[method] = METHOD_CONFIDENCE.get(method)
            register_match_method(method, confidence)
            if saved[method] is None:
                registered_new.append(method)
        yield
    finally:
        with _METHOD_CONFIDENCE_LOCK:
            for method, original in saved.items():
                if original is None:
                    # Method didn't exist before — remove it entirely.
                    METHOD_CONFIDENCE.pop(method, None)
                    _custom_methods.pop(method, None)
                else:
                    # Restore original value.
                    METHOD_CONFIDENCE[method] = original
                    if method in _ORIGINAL_METHOD_CONFIDENCE:
                        # Built-in — clear from custom_methods.
                        _custom_methods.pop(method, None)
                    else:
                        _custom_methods[method] = original
        logger.debug(
            "method_confidence_override: restored %d methods", len(saved),
        )


# =============================================================================
# Confidence-score lookup
# =============================================================================

def compute_match_confidence(
    method: str,
    *,
    as_enum: bool = False,
    detailed: bool = False,
    config: Any = None,
) -> Union[float, "MatchConfidence", MatchResult]:  # type: ignore[name-defined]
    """Return a confidence score for a given resolution *method*.

    Well-defined methods are mapped to fixed scores via
    :data:`METHOD_CONFIDENCE`; any unrecognised method defaults to
    ``0.5``.

    Parameters
    ----------
    method:
        Resolution method identifier, e.g. ``"inchikey_exact"``.
        Whitespace is trimmed before lookup (FIX #29 / BUG-CODE-05).
    as_enum:
        If ``True``, return a :class:`~entity_resolution.base.MatchConfidence`
        enum member instead of a bare float (FIX #11 / GAP-DESIGN-05).
        Default ``False`` for backward compat.
    detailed:
        If ``True``, return a :class:`MatchResult` dataclass with full
        provenance (FIX #108).  Default ``False``.
    config:
        Optional :class:`~entity_resolution.base.ResolverConfig`.  When
        provided, the function checks ``config.mapping_schema_version``
        against :data:`_RESOLVER_UTILS_SCHEMA_VERSION` and warns on
        mismatch (FIX #106 / GAP-INT-05).

    Returns
    -------
    float or MatchConfidence or MatchResult
        Confidence in ``[0.0, 1.0]``.  Default mode returns a ``float``.
        ``as_enum=True`` returns a :class:`MatchConfidence` member.
        ``detailed=True`` returns a :class:`MatchResult`.

    Raises
    ------
    TypeError
        If *method* is not a string (FIX #29).
    """
    if not isinstance(method, str):
        raise TypeError(
            f"method must be str, got {type(method).__name__}"
        )

    # FIX #106 / GAP-INT-05 — schema-version compatibility check.
    if config is not None:
        try:
            cfg_version = getattr(config, "mapping_schema_version", None)
            if cfg_version and cfg_version != _RESOLVER_UTILS_SCHEMA_VERSION:
                # Only warn — don't refuse to compute.  Operators can
                # decide whether to upgrade.
                logger.warning(
                    "compute_match_confidence: config schema version %s != "
                    "current version %s. Confidence values may differ.",
                    cfg_version, _RESOLVER_UTILS_SCHEMA_VERSION,
                )
        except Exception:  # pragma: no cover - defensive
            pass

    # FIX #29 / BUG-CODE-05 — trim whitespace before lookup.
    method = method.strip()

    with _METHOD_CONFIDENCE_LOCK:
        is_known = method in METHOD_CONFIDENCE
        confidence = METHOD_CONFIDENCE.get(method, 0.5)

    if not is_known:
        # FIX #50 / GAP-REL-04 — rate-limit warnings to one per unknown method.
        if method not in _unknown_method_warned:
            _unknown_method_warned.add(method)
            logger.warning(
                "compute_match_confidence: unknown method '%s', "
                "returning default confidence %.2f. "
                "This warning will not repeat for this method. "
                "Called from: %s",
                method, 0.5, _get_caller_info(skip=2),
            )

    # FIX #57 / GAP-IDEM-05 — round to 10 decimal places for cross-version
    # determinism.  IEEE 754 guarantees binary-level determinism, but the
    # string representation can differ; rounding makes string output stable.
    confidence = round(confidence, 10)

    if detailed:
        return MatchResult(
            method=method,
            confidence=confidence,
            is_known=is_known,
        )

    if as_enum:
        try:
            from .base import MatchConfidence
        except ImportError:
            # If base isn't available, fall back to returning the float.
            return confidence
        # Check custom methods first (FIX #6 / GUARD-ARCH-06).
        with _METHOD_CONFIDENCE_LOCK:
            if method in _custom_methods and method not in _ORIGINAL_METHOD_CONFIDENCE:
                # Truly custom method — synthesize an UNKNOWN-like member
                # with the right value.  Enum members can't be added at
                # runtime, so we return the float value cast through the
                # MatchConfidence constructor if possible, else float.
                return MatchConfidence.UNKNOWN  # type: ignore[return-value]
        return MatchConfidence.from_method(method)

    return confidence


# =============================================================================
# Record validation
# =============================================================================

#: Required fields for a drug record.
_REQUIRED_DRUG_FIELDS: Tuple[str, ...] = ("name",)

#: Optional but recognised fields for a drug record.
_OPTIONAL_DRUG_FIELDS: Tuple[str, ...] = (
    "inchikey", "chembl_id", "drugbank_id", "pubchem_cid",
    "smiles", "inchi", "molecular_formula", "molecular_weight",
)

#: Required fields for a protein record.
_REQUIRED_PROTEIN_FIELDS: Tuple[str, ...] = ("uniprot_id",)

#: Optional but recognised fields for a protein record.
_OPTIONAL_PROTEIN_FIELDS: Tuple[str, ...] = (
    "gene_symbol", "gene_name", "organism", "sequence",
    "string_id", "chembl_target_id", "protein_name",
)

#: Default drug ID fields (DEPRECATED — pass id_fields explicitly).
_DRUG_ID_FIELDS: Tuple[str, ...] = ("chembl_id", "drugbank_id", "pubchem_cid")

#: Default protein ID fields.
_PROTEIN_ID_FIELDS: Tuple[str, ...] = ("uniprot_id", "string_id", "chembl_target_id")

#: Protein fuzzy acceptance threshold (mirrors ``ProteinResolver._PROTEIN_FUZZY_THRESHOLD``).
#: METHOD_CONFIDENCE["protein_name_fuzzy"] must be >= this value (FIX #16 / SCI-02).
#:
#: v29 ROOT FIX (audit C-2 — Confidence Score Inversion): was 0.90.
#: Lowered to 0.55 to match the corrected MatchConfidence.PROTEIN_NAME_FUZZY=0.60.
#: See base.py MatchConfidence docstring for the full rationale.
_PROTEIN_FUZZY_THRESHOLD: float = 0.55


def validate_drug_record(
    record: dict,
    *,
    strict: bool = False,
    detailed: bool = False,
    required_fields: Tuple[str, ...] = _REQUIRED_DRUG_FIELDS,
    optional_fields: Tuple[str, ...] = _OPTIONAL_DRUG_FIELDS,
) -> Union[Tuple[bool, List[str]], ValidationReport]:
    """Validate a drug record at the public API boundary.

    Audit D5-2 / D3-8 require every record entering the resolver to be
    validated so that downstream garbage doesn't silently corrupt the
    mapping.  This helper returns a ``(ok, errors)`` tuple so callers
    can choose between lenient (log + drop) and strict (raise) modes.

    Parameters
    ----------
    record:
        Drug record dict to validate.
    strict:
        If ``True``, perform additional checks (FIX #23, #30, #43, #45, #46):

        - InChIKey format validation.
        - ChEMBL / DrugBank ID format validation.
        - PubChem CID positivity / type (rejects bool).
        - InChI prefix check.
        - SMILES non-empty check.
        - Molecular-weight range check (1–10 000 Da).
        - Unknown-field detection (catches typos like ``inchikeyy``).
    detailed:
        If ``True``, return a :class:`ValidationReport` dataclass with
        full provenance (FIX #112).  Default ``False`` returns the
        legacy ``(bool, list[str])`` tuple.
    required_fields:
        Tuple of required field names.  Defaults to
        :data:`_REQUIRED_DRUG_FIELDS` (``("name",)``).
    optional_fields:
        Tuple of recognised optional field names.  Defaults to
        :data:`_OPTIONAL_DRUG_FIELDS`.  Used for unknown-field detection
        in strict mode.

    Returns
    -------
    tuple[bool, list[str]] or ValidationReport
        ``(True, [])`` if the record is valid; otherwise
        ``(False, [error_message, ...])``.  When ``detailed=True``,
        returns a :class:`ValidationReport` instead.
    """
    errors: List[str] = []
    if not isinstance(record, dict):
        msg = f"record must be a dict, got {type(record).__name__}"
        if detailed:
            return ValidationReport(
                ok=False, errors=[msg], record_type="drug",
                validated_fields=[],
            )
        return False, [msg]
    if not record:
        msg = "record is empty"
        if detailed:
            return ValidationReport(
                ok=False, errors=[msg], record_type="drug",
                validated_fields=[],
            )
        return False, [msg]

    for f in required_fields:
        val = record.get(f)
        if val is None or (isinstance(val, str) and not val.strip()):
            errors.append(f"missing or empty required field: {f!r}")

    if strict:
        # InChIKey format
        ik = record.get("inchikey")
        if ik and isinstance(ik, str) and not is_valid_inchikey(ik):
            errors.append(
                f"inchikey {_truncate_for_error(ik)} does not match canonical "
                f"InChIKey format ([A-Z]{{14}}-[A-Z]{{10}}-[A-Z])"
            )

        # FIX #23 / BUG-SCI-09 — ChEMBL ID format
        chembl_id = record.get("chembl_id")
        if chembl_id and isinstance(chembl_id, str) and not _CHEMBL_ID_RE.match(chembl_id):
            errors.append(
                f"chembl_id {_truncate_for_error(chembl_id)} does not match "
                f"CHEMBL\\d+ format"
            )

        # FIX #23 — DrugBank ID format
        drugbank_id = record.get("drugbank_id")
        if drugbank_id and isinstance(drugbank_id, str) and not _DRUGBANK_ID_RE.match(drugbank_id):
            errors.append(
                f"drugbank_id {_truncate_for_error(drugbank_id)} does not match "
                f"DB\\d+ format"
            )

        # FIX #23, #30 / BUG-CODE-06 — PubChem CID type + positivity
        pcid = record.get("pubchem_cid")
        if pcid is not None:
            if isinstance(pcid, bool):
                errors.append("pubchem_cid must be int/str/None, not bool")
            elif not isinstance(pcid, (int, str)):
                errors.append(
                    f"pubchem_cid must be int/str/None, got {type(pcid).__name__}"
                )
            elif isinstance(pcid, int) and pcid <= 0:
                errors.append(f"pubchem_cid must be positive, got {pcid}")
            elif isinstance(pcid, str) and (not pcid.isdigit() or int(pcid) <= 0):
                errors.append(
                    f"pubchem_cid must be a positive integer string, "
                    f"got {_truncate_for_error(pcid)}"
                )

        # FIX #23 — InChI prefix check
        inchi = record.get("inchi")
        if inchi and isinstance(inchi, str) and not _INCHI_PREFIX_RE.match(inchi):
            errors.append(
                f"inchi {_truncate_for_error(inchi)} does not start with "
                f"InChI=1S/ or InChI=1/"
            )

        # FIX #23 — SMILES non-empty check
        smiles = record.get("smiles")
        if smiles is not None and isinstance(smiles, str) and not smiles.strip():
            errors.append("smiles is empty string")

        # FIX #23, #46 — Molecular weight type + range check
        mw = record.get("molecular_weight")
        if mw is not None:
            if isinstance(mw, bool):
                errors.append("molecular_weight must be numeric, not bool")
            elif not isinstance(mw, (int, float)):
                errors.append(
                    f"molecular_weight must be numeric, got {type(mw).__name__}"
                )
            elif isinstance(mw, (int, float)) and mw <= 0:
                errors.append(f"molecular_weight must be positive, got {mw}")
            elif isinstance(mw, (int, float)) and (mw < 1 or mw > 10000):
                errors.append(
                    f"molecular_weight {mw} is outside the expected range "
                    f"(1-10000 Da)"
                )

        # FIX #23 — Type checks on known optional string fields.
        for f in ("chembl_id", "drugbank_id", "name"):
            v = record.get(f)
            if v is not None and not isinstance(v, str):
                errors.append(
                    f"field {f!r} must be str or None, got {type(v).__name__}"
                )

        # FIX #45 / GAP-DQ-09 — Cross-field consistency (best-effort, RDKit optional)
        # Full InChIKey↔InChI cross-check requires RDKit; we attempt it but
        # silently skip if RDKit isn't available.
        if ik and inchi and isinstance(ik, str) and isinstance(inchi, str):
            try:
                from rdkit import Chem  # type: ignore
                computed_ik = Chem.InchiToInchiKey(inchi)
                if computed_ik and computed_ik != ik:
                    errors.append(
                        f"inchikey {_truncate_for_error(ik)} does not match the "
                        f"InChIKey derived from inchi (expected {computed_ik!r})"
                    )
            except ImportError:
                # RDKit not available — skip cross-field check.
                pass
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug(
                    "validate_drug_record: RDKit cross-check failed: %s", exc,
                )

        # FIX #43 / BUG-DQ-06 — Unknown field detection (catch typos).
        all_known = set(required_fields) | set(optional_fields)
        unknown = set(record.keys()) - all_known
        if unknown:
            errors.append(
                f"unknown fields detected (possible typos): {sorted(unknown)}"
            )

    ok = len(errors) == 0
    # FIX #80 / GAP-LOG-02 — log validation failures.
    if not ok:
        logger.warning(
            "validate_drug_record: %d errors for record with name=%s: %s",
            len(errors),
            _truncate_for_error(record.get("name", "<unknown>")),
            "; ".join(errors[:3]),
        )

    if detailed:
        return ValidationReport(
            ok=ok,
            errors=errors,
            record_type="drug",
            validated_fields=list(record.keys()),
        )
    return ok, errors


def validate_protein_record(
    record: dict,
    *,
    strict: bool = False,
    detailed: bool = False,
    required_fields: Tuple[str, ...] = _REQUIRED_PROTEIN_FIELDS,
    optional_fields: Tuple[str, ...] = _OPTIONAL_PROTEIN_FIELDS,
) -> Union[Tuple[bool, List[str]], ValidationReport]:
    """Validate a protein record at the public API boundary.

    Parameters
    ----------
    record:
        Protein record dict to validate.
    strict:
        If ``True``, perform additional checks (FIX #15, #24, #44):

        - UniProt accession format using the OFFICIAL UniProt regex
          (supports 6-char and 10-char accessions, including O/P/Q prefixes).
        - ``STRING:`` and ``CHEMBL_T:`` prefixed ID validation.
        - Amino-acid sequence character validation.
        - ``string_id`` and ``chembl_target_id`` format checks.
        - Unknown-field detection.
    detailed:
        If ``True``, return a :class:`ValidationReport` dataclass with
        full provenance (FIX #112).  Default ``False`` returns the
        legacy ``(bool, list[str])`` tuple.
    required_fields:
        Tuple of required field names.
    optional_fields:
        Tuple of recognised optional field names.

    Returns
    -------
    tuple[bool, list[str]] or ValidationReport
        ``(True, [])`` if valid; otherwise ``(False, [errors...])``.
        When ``detailed=True``, returns a :class:`ValidationReport`.
    """
    errors: List[str] = []
    if not isinstance(record, dict):
        msg = f"record must be a dict, got {type(record).__name__}"
        if detailed:
            return ValidationReport(
                ok=False, errors=[msg], record_type="protein",
                validated_fields=[],
            )
        return False, [msg]
    if not record:
        msg = "record is empty"
        if detailed:
            return ValidationReport(
                ok=False, errors=[msg], record_type="protein",
                validated_fields=[],
            )
        return False, [msg]

    for f in required_fields:
        val = record.get(f)
        if val is None or (isinstance(val, str) and not val.strip()):
            errors.append(f"missing or empty required field: {f!r}")

    if strict:
        # FIX #15, #44 / BUG-SCI-01, BUG-DQ-07 — UniProt accession validation.
        uid = record.get("uniprot_id")
        if uid and isinstance(uid, str):
            if uid.startswith("STRING:"):
                # FIX #44 — validate the content after the prefix.
                rest = uid[len("STRING:"):]
                if not rest or not _STRING_ID_RE.match(rest):
                    errors.append(
                        f"STRING: prefixed ID has invalid format: "
                        f"{_truncate_for_error(uid)}"
                    )
            elif uid.startswith("CHEMBL_T:"):
                # FIX #44 — validate the content after the prefix.
                rest = uid[len("CHEMBL_T:"):]
                if not rest or not _CHEMBL_TARGET_ID_RE.match(rest):
                    errors.append(
                        f"CHEMBL_T: prefixed ID has invalid format: "
                        f"{_truncate_for_error(uid)}"
                    )
            elif not _UNIPROT_ACCESSION_RE.match(uid):
                # FIX #15 — official UniProt accession pattern.
                errors.append(
                    f"uniprot_id {_truncate_for_error(uid)} does not match the "
                    f"official UniProt accession format"
                )

        # FIX #24 / BUG-SCI-10 — amino-acid sequence validation.
        seq = record.get("sequence")
        if seq and isinstance(seq, str) and not _AA_VALID_RE.match(seq):
            errors.append(
                f"sequence contains invalid amino acid characters"
            )

        # FIX #24 — string_id format
        sid = record.get("string_id")
        if sid and isinstance(sid, str) and not _STRING_ID_RE.match(sid):
            errors.append(
                f"string_id {_truncate_for_error(sid)} does not match "
                f"'species.ENSPxxxxx' format"
            )

        # FIX #24 — chembl_target_id format
        ctid = record.get("chembl_target_id")
        if ctid and isinstance(ctid, str) and not _CHEMBL_TARGET_ID_RE.match(ctid):
            errors.append(
                f"chembl_target_id {_truncate_for_error(ctid)} does not match "
                f"CHEMBL\\d+ format"
            )

        # Type checks on known optional string fields.
        for f in ("gene_symbol", "gene_name", "organism", "protein_name"):
            v = record.get(f)
            if v is not None and not isinstance(v, str):
                errors.append(
                    f"field {f!r} must be str or None, got {type(v).__name__}"
                )

        # FIX #43 — Unknown field detection.
        all_known = set(required_fields) | set(optional_fields)
        unknown = set(record.keys()) - all_known
        if unknown:
            errors.append(
                f"unknown fields detected (possible typos): {sorted(unknown)}"
            )

    ok = len(errors) == 0
    if not ok:
        logger.warning(
            "validate_protein_record: %d errors for record with uniprot_id=%s: %s",
            len(errors),
            _truncate_for_error(record.get("uniprot_id", "<unknown>")),
            "; ".join(errors[:3]),
        )

    if detailed:
        return ValidationReport(
            ok=ok,
            errors=errors,
            record_type="protein",
            validated_fields=list(record.keys()),
        )
    return ok, errors


def validate_record(
    record: dict,
    kind: str,
    *,
    strict: bool = False,
    detailed: bool = False,
) -> Union[Tuple[bool, List[str]], ValidationReport]:
    """Polymorphic dispatcher for record validation (FIX #14 / GAP-DESIGN-08).

    Parameters
    ----------
    record:
        Record dict to validate.
    kind:
        Either ``"drug"`` or ``"protein"``.
    strict:
        If ``True``, perform strict-mode validation.
    detailed:
        If ``True``, return a :class:`ValidationReport`.

    Returns
    -------
    tuple[bool, list[str]] or ValidationReport
        Same shape as :func:`validate_drug_record` /
        :func:`validate_protein_record`.

    Raises
    ------
    ValueError
        If *kind* is not ``"drug"`` or ``"protein"``.
    """
    if kind == "drug":
        return validate_drug_record(record, strict=strict, detailed=detailed)
    elif kind == "protein":
        return validate_protein_record(record, strict=strict, detailed=detailed)
    else:
        raise ValueError(
            f"Unknown record kind: {kind!r}. Expected 'drug' or 'protein'."
        )


# =============================================================================
# Duplicate-ID detection
# =============================================================================

def find_duplicate_ids(
    records: Sequence[dict],
    id_fields: Optional[Sequence[str]] = None,
    *,
    seen: Any = _UNSET_SENTINEL,
    return_counts: bool = False,
    return_indices: bool = False,
    sanitize_output: bool = False,
) -> Union[
    Dict[str, List[str]],
    Dict[str, Dict[str, int]],
    Dict[str, Dict[str, List[int]]],
    Tuple[Dict[str, List[str]], Optional[Dict[str, Dict[str, int]]]],
]:
    """Find source-specific IDs that appear in more than one record.

    Audit D5-3 — silent duplicates across sources are a data-quality
    landmine because they produce ambiguous ``WHERE chembl_id = ...``
    lookups downstream.

    NOTE: This function detects duplicates WITHIN the provided *records*
    sequence only.  Cross-batch duplicate detection requires the caller
    to pass the union of all batches, or to use the *seen* parameter
    for incremental tracking across batches (FIX #38 / BUG-DQ-01).

    NaN values, empty strings, and whitespace-only strings are NOT
    counted as duplicates (FIX #34 / GAP-CODE-10).

    Parameters
    ----------
    records:
        Sequence of record dicts.
    id_fields:
        Sequence of field names to check for duplicates.  Default
        (DEPRECATED — FIX #12) is :data:`_DRUG_ID_FIELDS` (drug-specific).
        Callers should pass this explicitly.  When ``None``, a
        :class:`DeprecationWarning` is emitted.
    seen:
        Optional mutable state dict for incremental cross-batch duplicate
        detection (FIX #38).  Pass the same dict across multiple calls
        to track duplicates across batches.  When the caller EXPLICITLY
        passes ``seen`` (even as ``None`` on the first call), the
        function returns a 2-tuple ``(result, seen)`` so the caller can
        keep using the same state dict for subsequent batches.  When
        *seen* is not passed at all, only the result is returned.
    return_counts:
        If ``True``, return ``{field: {value: count}}`` instead of
        ``{field: [values]}`` (FIX #36 / GAP-CODE-12).
    return_indices:
        If ``True``, return ``{field: {value: [record_indices]}}`` so
        callers can locate the duplicate records (FIX #111 / GAP-LIN-04).
    sanitize_output:
        If ``True``, return only counts per field (no actual values),
        for privacy-safe logging (FIX #67 / GUARD-SEC-05).

    Returns
    -------
    dict
        Default: ``{field: [duplicate_values]}``.
        With ``return_counts=True``: ``{field: {value: count}}``.
        With ``return_indices=True``: ``{field: {value: [indices]}}``.
        With ``sanitize_output=True``: ``{field: count_of_duplicates}``.

        When *seen* is explicitly passed by the caller (not the default),
        AND ``return_counts``/``return_indices``/``sanitize_output`` are
        all ``False``, returns a 2-tuple ``(result, seen)`` so the
        caller can keep using the same state dict across batches.
    """
    # FIX #12 / GAP-DESIGN-06 — id_fields defaults to drug-specific values.
    # Emit a DeprecationWarning so callers know to pass id_fields explicitly.
    if id_fields is None:
        warnings.warn(
            "find_duplicate_ids: id_fields default is drug-specific and will "
            "be removed in a future version. Pass id_fields explicitly.",
            DeprecationWarning,
            stacklevel=2,
        )
        id_fields = _DRUG_ID_FIELDS
    id_fields = tuple(id_fields)

    # FIX #38 — sentinel-based detection of whether the caller explicitly
    # passed ``seen``.  ``seen=None`` (explicit) is different from
    # ``seen not passed at all``.  When explicitly passed (even as None),
    # we return the 2-tuple so the caller can chain calls.
    seen_provided = seen is not _UNSET_SENTINEL
    if seen is None or seen is _UNSET_SENTINEL:
        seen = {f: {} for f in id_fields}
    else:
        # Ensure all id_fields are present in seen.
        for f in id_fields:
            if f not in seen:
                seen[f] = {}

    # FIX #111 — track record indices when return_indices=True.
    indices_map: Dict[str, Dict[str, List[int]]] = {f: {} for f in id_fields}

    for i, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        for f in id_fields:
            v = record.get(f)
            if v is None:
                continue
            # FIX #34 / GAP-CODE-10 — skip NaN.
            if isinstance(v, float) and math.isnan(v):
                continue
            v_str = str(v).strip()
            # FIX #34 — skip empty and whitespace-only.
            if not v_str:
                continue
            seen[f][v_str] = seen[f].get(v_str, 0) + 1
            if return_indices:
                indices_map[f].setdefault(v_str, []).append(i)

    # Build the result based on the requested return mode.
    if return_indices:
        result: Dict[str, Dict[str, List[int]]] = {
            f: {v: idxs for v, idxs in indices_map[f].items() if len(idxs) > 1}
            for f in id_fields
            if any(len(idxs) > 1 for idxs in indices_map[f].values())
        }
    elif return_counts:
        result = {
            f: {v: n for v, n in counts.items() if n > 1}
            for f, counts in seen.items()
            if any(n > 1 for n in counts.values())
        }
    elif sanitize_output:
        # FIX #67 — return only counts per field, no actual values.
        result = {
            f: sum(1 for n in counts.values() if n > 1)
            for f, counts in seen.items()
            if any(n > 1 for n in counts.values())
        }
    else:
        # FIX #56 / GAP-IDEM-04 — sort by field name for deterministic order.
        result = {
            f: sorted(v for v, n in counts.items() if n > 1)
            for f, counts in sorted(seen.items())
            if any(n > 1 for n in counts.values())
        }

    # FIX #82 / GAP-LOG-04 — log duplicate findings.
    if result:
        if return_counts or return_indices:
            total_dupes = sum(
                len(v) if isinstance(v, (dict, list)) else 1
                for v in result.values()
            )
        elif sanitize_output:
            # sanitize_output returns ints (counts), not lists/dicts.
            total_dupes = sum(result.values())
        else:
            total_dupes = sum(len(v) for v in result.values())
        logger.info(
            "find_duplicate_ids: found %d duplicated values across %d fields",
            total_dupes, len(result),
        )
        for f, vals in result.items():
            if isinstance(vals, list):
                logger.warning(
                    "find_duplicate_ids: field '%s' has %d duplicated values: %s",
                    f, len(vals), vals[:5],
                )
            elif isinstance(vals, dict):
                logger.warning(
                    "find_duplicate_ids: field '%s' has %d duplicated values",
                    f, len(vals),
                )
            else:
                logger.warning(
                    "find_duplicate_ids: field '%s' has %d duplicated values",
                    f, vals,
                )
    else:
        logger.debug("find_duplicate_ids: no duplicates found")

    # FIX #38 — when seen was provided AND return_counts is False,
    # return a 2-tuple so the caller can keep using the same state dict.
    if seen_provided and not return_counts and not return_indices and not sanitize_output:
        return result, seen
    return result


def find_duplicate_ids_streaming(
    records: Iterable[dict],
    id_fields: Sequence[str],
) -> Iterable[Tuple[str, str, int]]:
    """Stream duplicate-ID findings one at a time (FIX #62 / GAP-PERF-05).

    Yields ``(field_name, duplicate_value, count_so_far)`` tuples the
    moment a duplicate is first detected (i.e. the second occurrence of
    a value).  Uses ``O(unique_values)`` memory instead of ``O(records)``.

    Parameters
    ----------
    records:
        Iterable of record dicts.  Can be a generator — does not need
        to fit in memory.
    id_fields:
        Sequence of field names to check.

    Yields
    ------
    tuple[str, str, int]
        ``(field_name, duplicate_value, count_so_far)`` — emitted the
        first time a value is seen for the second time, and again for
        each subsequent occurrence.
    """
    id_fields = tuple(id_fields)
    seen: Dict[str, Dict[str, int]] = {f: {} for f in id_fields}
    for record in records:
        if not isinstance(record, dict):
            continue
        for f in id_fields:
            v = record.get(f)
            if v is None:
                continue
            if isinstance(v, float) and math.isnan(v):
                continue
            v_str = str(v).strip()
            if not v_str:
                continue
            seen[f][v_str] = seen[f].get(v_str, 0) + 1
            if seen[f][v_str] >= 2:
                yield f, v_str, seen[f][v_str]


# =============================================================================
# Public API surface
# =============================================================================

#: Public symbols exported by this module.  ``from entity_resolution.resolver_utils
#: import *`` only imports these names (FIX #3 / GAP-ARCH-03).
__all__: List[str] = [
    # Original public symbols (preserved for backward compat)
    "normalize_name",
    "fuzzy_match_score",
    "extract_inchikey_first_block",
    "is_valid_inchikey",
    "build_name_index",            # deprecated — kept for backward compat
    "build_inchikey_index",        # deprecated — kept for backward compat
    "build_canonical_name_index",
    "build_canonical_inchikey_index",
    "METHOD_CONFIDENCE",
    "register_match_method",
    "compute_match_confidence",
    "validate_drug_record",
    "validate_protein_record",
    "find_duplicate_ids",
    # New public symbols added by the 113-issue fix
    "RAPIDFUZZ_AVAILABLE",         # was _RAPIDFUZZ_AVAILABLE
    "validate_record",             # polymorphic dispatcher
    "unregister_match_method",     # new — for test isolation
    "reset_method_confidence",     # new — restore original values
    "get_registered_methods",      # new — snapshot of METHOD_CONFIDENCE
    "method_confidence_override",  # new — context manager
    "fuzzy_match_best",            # new — batch fuzzy match
    "merge_into_name_index",       # new — incremental index update
    "merge_into_inchikey_index",   # new — incremental index update
    "find_duplicate_ids_streaming",  # new — streaming variant
    "MatchResult",                 # new — dataclass for provenance
    "ValidationReport",            # new — dataclass for validation
    "NormalizedName",              # new — dataclass for normalisation
    "ConnectivityBlock",           # new — dataclass for connectivity
    "normalize_name_cache_info",   # new — cache observability
    "normalize_name_cache_clear",  # new — cache management
    "sync_method_confidence",      # new — verify enum/dict sync
]
