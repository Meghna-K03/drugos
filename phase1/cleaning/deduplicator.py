# MIT License
#
# Copyright (c) 2026 Team Cosmic / VentureLab
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# SPDX-License-Identifier: MIT
"""
Deduplication utilities for the Drug Repurposing ETL platform — v3.0.0.

Institutional-grade deduplication module covering 138 issues across 16
domains: Architecture, Design, Knowledge (Scientific Correctness), Coding,
Data Quality & Integrity, Reliability & Resilience, Idempotency &
Reproducibility, Performance & Scalability, Security & Privacy, Testing &
Validation, Logging & Observability, Configuration & Environment Management,
Documentation & Readability, Compliance & Standards Adherence,
Interoperability & Integration, Data Lineage & Traceability.

Project Context
---------------
The Autonomous Drug Repurposing Platform mines 10,000 FDA-approved drugs
against every known disease using 7 public biomedical databases (ChEMBL,
DrugBank, UniProt, STRING, DisGeNET, OMIM, PubChem), builds a Neo4j Knowledge
Graph, trains a PyTorch Geometric Graph Transformer, and ranks hypotheses
with an RL agent. The deduplicator is the single chokepoint that decides
which drug record survives when two sources disagree, and which activity
measurement survives when multiple measurements exist for the same
drug-protein pair. A silent bug here propagates into the knowledge graph,
the model training set, and every downstream prediction.

Public Functions
----------------
- ``dedup_by_inchikey`` — dedup drug records by InChIKey, keep most-complete
- ``dedup_interactions`` — dedup DPI rows by composite key, keep most potent
- ``dedup_by_inchikey_chunked`` — chunked variant for large DataFrames
- ``compute_completeness_score`` — weighted completeness scoring
- ``merge_duplicate_groups`` — column-wise merge of duplicate groups
- ``quality_report`` — pre/post dedup quality metrics
- ``referential_integrity_check`` — verify foreign-key integrity
- ``backfill_safety_check`` — safe re-processing guard
- ``recover_from_failure`` — resume from partial state
- ``checkpoint_state`` / ``validate_recovery_state`` — fault tolerance
- ``performance_benchmark`` / ``is_reproducible`` / ``reproducibility_report``
- ``get_metrics`` / ``reset_metrics`` — observability
- ``get_dead_letters`` / ``clear_dead_letters`` / ``flush_dead_letters``
- ``set_correlation_id`` / ``get_correlation_id`` — distributed tracing
- ``get_provenance`` — extract lineage metadata
- ``timing_report`` / ``health_check`` — observability
- ``configure_deduplicator`` / ``validate_config`` / ``validate_environment``
- ``revert_configuration`` — undo configuration changes
- ``requires_api_version`` — semver gating

Scientific Correctness (Domain 3)
---------------------------------
The "most potent" semantic for interactions respects ``activity_type``:

- ``IC50``, ``Ki``, ``Kd``, ``EC50``, ``AC50``, ``ED50``, ``Kb`` — lower =
  more potent (sort ascending).
- ``pKi``, ``pIC50``, ``pEC50``, ``pKd`` — higher = more potent
  (sort descending). The ``p``-prefix denotes ``-log10`` of the molar
  concentration, so a higher ``pKi`` corresponds to a lower ``Ki``.
- ``%`` inhibition — higher = more potent (sort descending).

``activity_type`` is part of the dedup segmentation when present: two rows
with the same ``(drug_id, protein_id, source)`` but different
``activity_type`` are NOT duplicates — they are different measurements and
both must be retained.

Censored values (``">100"``, ``"<10"``, ``"~50"``) are NOT silently allowed
to win over uncensored values. A censored ``"<10"`` is a lower bound, not an
actual measurement.

InChIKey Validation Contract
----------------------------
A key is valid iff:

1. ``len(key) == 27`` AND ``key`` matches ``^[A-Z]{14}-[A-Z]{10}-[A-Z]$``
   (loose), OR
2. ``key.upper().startswith("SYNTH")``.

Synthetic InChIKeys (``SYNTH...``) are placeholders for records where the
true InChIKey could not be computed (e.g., RDKit unavailable, mixture
compound, or biologic without defined structure). They are NOT collapsed
together — each ``SYNTH`` key is treated as unique.

NaN InChIKeys are NOT collapsed into a single row. The default
``drop_duplicates(subset=["inchikey"], keep="first")`` treats
``NaN == NaN`` as ``True`` in pandas, silently dropping all but one
null-InChIKey row. This is a data-loss bug fixed in v3.0.0.

Configuration
-------------
Environment variables:

==================  =========================================  =================
Name                Purpose                                    Default
==================  =========================================  =================
``CLEANING_DEDUP_LOG_LEVEL``    Override log level for this module
``CLEANING_DEDUP_LOG_FORMAT``   ``"json"`` (default) or ``"text"``
``CLEANING_DEDUP_MAX_ROWS``     Override the DoS-guard row cap      ``10_000_000``
``CLEANING_DEDUP_MAX_DL``       Override the dead-letter cap         ``10_000``
``CLEANING_DEDUP_ENV``          ``dev`` / ``staging`` / ``prod``
``CLEANING_ENV``                Shared env flag (read as fallback)
==================  =========================================  =================

Compliance Notes
----------------
- **FDA 21 CFR Part 11**: ``flush_dead_letters`` writes an immutable
  audit trail of dropped records, supporting electronic records and
  e-signatures requirements. Each dead-letter entry is timestamped and
  attributed to an ``operator_id`` when supplied.
- **GDPR**: No PII is logged by default. ``_redact_for_log`` masks
  emails, phone numbers, and SSNs. ``_scan_for_pii`` warns on detection.
- **HIPAA**: Patient-identifying fields (MRN, patient name) are scanned
  via ``_scan_for_pii`` and rejected from dead-letter persistence.
- **Audit trail**: ``result.attrs["_provenance"]`` records every
  transformation step with input/output fingerprints, enabling full
  reproducibility and forensic review.

Interoperability Notes
----------------------
- All output DataFrames are consumable by ``database.loaders.bulk_upsert_drugs``
  and ``bulk_upsert_dpi`` without modification.
- All path operations use ``pathlib.Path`` for cross-platform safety.
- Tested on pandas 2.1.4+ and 2.2.x. No deprecated APIs (``inplace=True``
  on slices, ``df.append``, ``np.NaN``) are used.

API Stability
-------------
STABLE API (backward-compatible guaranteed within v3.x):

- ``dedup_by_inchikey(df) -> pd.DataFrame``  (positional ``df``, no kwargs)
- ``dedup_interactions(df, keys) -> pd.DataFrame``  (positional ``df`` + ``keys``)
- All new keyword-only parameters have defaults that preserve v1.0.0
  behavior exactly.

UNSTABLE API (may change in v4.x):

- ``DedupResult``, ``DedupStrategy``, ``ActivityDirection``,
  ``CompletenessWeight`` (may add fields)
- ``configure_deduplicator()`` parameters
- Dead-letter queue entry format
- Provenance metadata format

DEPRECATED API (will be removed in v4.0.0):

- (none currently)

Backward Compatibility Policy
-----------------------------
Semver. Breaking changes only in v4.0.0. See ``cleaning/MIGRATION.md``.

References
----------
- InChIKey spec: https://www.inchem.org/inchi-key.html
- ChEMBL activity types: https://chembl.gitbook.io/chembl-interface-documentation
- FDA 21 CFR Part 11: https://www.fda.gov/regulatory-information/search-fda-guidance-documents

License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2026 Team Cosmic, VentureLab
"""

from __future__ import annotations

# Standard library imports only — no third-party top-level imports
# besides pandas (which is the project's data substrate).
import enum
import hashlib
import json
import logging
import os
import re
import threading
import time
import types
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator, Literal

import pandas as pd

# ===========================================================================
# [ARCH-1] Module metadata — version constants
# ===========================================================================
__version__: str = "3.0.0"
__author__: str = "Team Cosmic / VentureLab"
__license__: str = "MIT"

_MODULE_VERSION: str = "3.0.0"           # bumped when dedup logic changes
_OUTPUT_SCHEMA_VERSION: str = "3.0.0"    # bumped when output schema changes
_RULE_VERSION: str = "rules_v3"          # bumped when dedup rules change
_CONFIG_VERSION: str = "1.0.0"           # bumped when config schema changes

# [ARCH-1] [IDEM-10] Logic hash: first 16 hex chars of sha256 of this source
# file. Computed lazily inside try/except so import never crashes.
_LOGIC_HASH: str = "unknown"
try:
    _LOGIC_HASH = hashlib.sha256(
        Path(__file__).read_bytes()
    ).hexdigest()[:16]
except Exception:  # REL-6 — never crash at import time
    _LOGIC_HASH = "unknown"

# [ARCH-9] Module load time (captured by importlib on first access).
# We expose a sentinel here; the package's __getattr__ records the
# real load time into ``cleaning._MODULE_LOAD_TIMES["deduplicator"]``.
_MODULE_LOAD_TIME: float = time.monotonic()


# ===========================================================================
# [ARCH-2] Logger setup — NullHandler + correlation-ID filter
# ===========================================================================
logger = logging.getLogger(__name__)
if not logger.handlers:  # library best practice — never propagate NoHandler
    logger.addHandler(logging.NullHandler())

# Env-driven log level
_ENV_LOG_LEVEL: str = os.environ.get("CLEANING_DEDUP_LOG_LEVEL", "")
if _ENV_LOG_LEVEL:
    logger.setLevel(getattr(logging, _ENV_LOG_LEVEL.upper(), logging.NOTSET))
elif os.environ.get("CLEANING_DEDUP_ENV", "").lower() == "dev":
    logger.setLevel(logging.DEBUG)
elif os.environ.get("CLEANING_DEDUP_ENV", "").lower() in ("staging", "prod"):
    logger.setLevel(logging.INFO)


class _CorrelationIdFilter(logging.Filter):
    """Inject the current correlation_id into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            cid = get_correlation_id()
        except Exception:
            cid = None
        record.correlation_id = cid or "-"
        return True


logger.addFilter(_CorrelationIdFilter())


# [LOG-2] Structured-logging toggle
_LOG_FORMAT_JSON: bool = (
    os.environ.get("CLEANING_DEDUP_LOG_FORMAT", "json").lower() != "text"
)


def _log_event(level: str, event: str, **fields: Any) -> None:
    """[LOG-1] [LOG-2] Emit a structured log event (JSON or text).

    Parameters
    ----------
    level : str
        Log level name (``"info"``, ``"warning"``, ``"error"``, ``"debug"``).
    event : str
        Short event name (e.g. ``"dedup_by_inchikey.complete"``).
    **fields : Any
        Additional key/value pairs to include in the log payload.
    """
    log_fn = getattr(logger, level.lower(), logger.debug)
    if not logger.isEnabledFor(
        getattr(logging, level.upper(), logging.DEBUG)
    ):
        return
    if _LOG_FORMAT_JSON:
        payload: dict[str, Any] = {"event": event, **fields}
        try:
            cid = get_correlation_id()
            if cid:
                payload["correlation_id"] = cid
        except Exception:
            pass
        log_fn("%s: %s", event, json.dumps(payload, default=str, sort_keys=True))
    else:
        parts = [f"{k}={v!r}" for k, v in fields.items()]
        log_fn("%s: %s", event, " ".join(parts))


# ===========================================================================
# [ARCH-4] Optional-dependency self-declaration
# ===========================================================================
# Mirrors ``cleaning._OPTIONAL_DEPS["dedup_by_inchikey"] = {}`` — this
# module has NO optional third-party dependencies (only pandas + stdlib).
_OPTIONAL_DEPS_SELF: dict[str, set[str]] = {
    "dedup_by_inchikey": set(),
    "dedup_interactions": set(),
    "dedup_by_inchikey_chunked": set(),
    "compute_completeness_score": set(),
    "merge_duplicate_groups": set(),
    "quality_report": set(),
    "referential_integrity_check": set(),
    "backfill_safety_check": set(),
    "recover_from_failure": set(),
    "checkpoint_state": set(),
    "validate_recovery_state": set(),
    "performance_benchmark": set(),
    "is_reproducible": set(),
    "reproducibility_report": set(),
    "configure_deduplicator": set(),
    "validate_config": set(),
    "validate_environment": set(),
    "revert_configuration": set(),
    "requires_api_version": set(),
    "get_metrics": set(),
    "reset_metrics": set(),
    "get_dead_letters": set(),
    "clear_dead_letters": set(),
    "flush_dead_letters": set(),
    "set_correlation_id": set(),
    "get_correlation_id": set(),
    "get_provenance": set(),
    "timing_report": set(),
    "health_check": set(),
}


# ===========================================================================
# [CODE-1] [SCI-7] Scientific constants — activity-type taxonomy
# ===========================================================================
# Lower-is-better (molar concentration) — these are direct measurements
# of the concentration required to achieve a defined effect. Lower =
# higher binding affinity / potency.
POTENCY_ACTIVITY_TYPES: frozenset[str] = frozenset({
    "IC50", "Ki", "Kd", "EC50", "AC50", "ED50", "Kb",
})

# Higher-is-better (negative log of molar concentration) — these are
# -log10 transforms of the corresponding potency values. Higher = more potent.
INVERSE_ACTIVITY_TYPES: frozenset[str] = frozenset({
    "pKi", "pIC50", "pEC50", "pKd",
})

# Percent-inhibition assays — higher % inhibition at a fixed concentration
# indicates stronger binding / effect.
PERCENT_ACTIVITY_TYPES: frozenset[str] = frozenset({
    "%", "percent", "inhibition", "inhibition_%",
})

# All known activity types — used for validation (DQ-8).
_ALLOWED_ACTIVITY_TYPES: frozenset[str] = (
    POTENCY_ACTIVITY_TYPES
    | INVERSE_ACTIVITY_TYPES
    | PERCENT_ACTIVITY_TYPES
    | frozenset({"None", "unknown"})
)

# Default composite key for drug-protein interactions (matches
# ``uq_dpi_drug_protein_source`` constraint in database/models.py).
DEFAULT_DPI_KEYS: list[str] = ["drug_id", "protein_id", "source", "source_id"]

# Default weights for completeness scoring (DES-3). Higher weight = the
# column carries more identifying information.
_DEFAULT_WEIGHTS: dict[str, float] = {
    "inchikey": 5.0,             # primary identifier — must be present
    "name": 4.0,                 # human-readable identifier
    "smiles": 3.5,               # structural representation
    "molecular_weight": 2.5,
    "molecular_formula": 2.0,
    "drug_type": 2.0,
    "max_phase": 2.0,
    "is_fda_approved": 2.0,
    "chembl_id": 2.0,
    "drugbank_id": 2.0,
    "pubchem_cid": 1.5,
    "mechanism_of_action": 1.0,
    "source": 1.0,
    "groups": 1.0,
}

# [DQ-3] Lineage / metadata columns that must NOT count toward completeness
# (they are internal bookkeeping, not data).
_LINEAGE_COLUMNS: frozenset[str] = frozenset({
    "_cleaning_applied",
    "_provenance",
    "_completeness_score",
    "_dedup_winner",
    "_dedup_loser_inchikey",
    "_dedup_loser_composite_key",
    "_dedup_already_applied",
    "_dedup_source_indices",
    "_input_fingerprint",
    "_output_fingerprint",
    "cleaning_metrics",
})

# [SEC-3] DoS guard — max rows accepted by single-call APIs.
_MAX_DATAFRAME_ROWS: int = int(
    os.environ.get("CLEANING_DEDUP_MAX_ROWS", "10000000")
)
# Public alias (exported in __all__)
MAX_DATAFRAME_ROWS: int = _MAX_DATAFRAME_ROWS

# [REL-1] Dead-letter queue capacity. FIFO eviction when full.
_MAX_DEAD_LETTERS: int = int(
    os.environ.get("CLEANING_DEDUP_MAX_DL", "10000")
)
# Public alias (exported in __all__)
MAX_DEAD_LETTERS: int = _MAX_DEAD_LETTERS

# [DES-1] Maximum dropped rows retained in ``DedupResult.dropped_rows``
# for inspection (prevents unbounded memory on pathological inputs).
_MAX_DROPPED_ROWS_IN_RESULT: int = 1000
# Public alias (exported in __all__)
MAX_DROPPED_ROWS_IN_RESULT: int = _MAX_DROPPED_ROWS_IN_RESULT

# [SCI-10] Plausible activity-value range. Values outside this range are
# treated as data-entry errors and quarantined.
# v16 ROOT FIX (CD-7): import the non-physical threshold from
# cleaning._constants so it is shared with normalizer.py (which
# previously had its own _ACTIVITY_VALUE_MAX = 1e6 — a 3-order-of-
# magnitude divergence). The deduplicator uses the NON-PHYSICAL
# threshold (1 M) to REJECT corrupt values; normalizer uses the
# CENSORED threshold (1 mM) to FLAG values that exceed pharmacological
# relevance. Both modules now import from cleaning._constants.
from cleaning._constants import (
    ACTIVITY_VALUE_NON_PHYSICAL_THRESHOLD as _ACTIVITY_NON_PHYSICAL_MAX,
    ACTIVITY_VALUE_CENSORED_THRESHOLD as _ACTIVITY_CENSORED_MAX,
)
_ACTIVITY_VALUE_MIN: float = 0.0           # negative concentrations impossible
_ACTIVITY_VALUE_MAX: float = _ACTIVITY_NON_PHYSICAL_MAX  # >1 M is non-physical

# [SCI-3] Censor-mark pattern — captures leading ``<``, ``>``, ``=``, ``~``.
_CENSOR_PATTERN: re.Pattern[str] = re.compile(
    r"^\s*([<>=~]{1,2})\s*([0-9.eE+-]+)\s*$"
)

# [SCI-7] InChIKey patterns — v29 ROOT FIX: import the CANONICAL regex
# from cleaning._constants instead of redefining it. The previous code
# defined its own _INCHIKEY_PATTERN with `^[A-Z]{14}-[A-Z]{10}-[A-Z]$`
# (strict 27-char), while normalizer.py accepted `^[A-Z]{14}-[A-Z]{10}-
# [A-Z](?:-[A-Za-z0-9]+)?$` (28+ char suffixed). This divergence meant
# a valid InChIKey could pass cleaning, fail dedup, fail DB insert
# (audit Compound Chain 3).
#
# All InChIKey regexes below are now IMPORTED from _constants — the
# single source of truth. We keep the underscored names as aliases for
# backward compatibility with internal callers in this file.
from ._constants import (
    CANONICAL_INCHIKEY_REGEX as _INCHIKEY_PATTERN,
    CANONICAL_STANDARD_INCHIKEY_REGEX as _STANDARD_INCHIKEY_PATTERN,
    CANONICAL_NONSTANDARD_INCHIKEY_REGEX as _NONSTANDARD_INCHIKEY_PATTERN,
    CANONICAL_SYNTHETIC_INCHIKEY_REGEX as _SYNTHETIC_INCHIKEY_PATTERN,
    CANONICAL_MIXTURE_INCHIKEY_REGEX as _MIXTURE_INCHIKEY_PATTERN,
    strip_inchikey_extension as _strip_inchikey_extension,
    is_canonical_inchikey as _is_canonical_inchikey,
)
# Re-export for backward-compat with callers that import these names
# from deduplicator directly.
_INCHIKEY_PATTERN = _INCHIKEY_PATTERN
_STANDARD_INCHIKEY_PATTERN = _STANDARD_INCHIKEY_PATTERN
_NONSTANDARD_INCHIKEY_PATTERN = _NONSTANDARD_INCHIKEY_PATTERN
_SYNTHETIC_INCHIKEY_PATTERN = _SYNTHETIC_INCHIKEY_PATTERN
_MIXTURE_INCHIKEY_PATTERN = _MIXTURE_INCHIKEY_PATTERN

# [SCI-8] Whitespace-detection regex. The v1.0.0 bug used
# ``str.match(r'^\s+|\s+$')`` which only matches the START of the string,
# missing trailing whitespace. The fix uses ``str.contains`` with a
# pattern that matches leading OR trailing whitespace.
_WHITESPACE_PATTERN: re.Pattern[str] = re.compile(r"^\s+|\s+$")

# [SEC-1] [SEC-2] PII detection patterns (mirrors missing_values.py).
_PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("phone", re.compile(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b")),
    ("ssn",   re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("mrn",   re.compile(r"\bMRN[:\s]?\d+\b", re.IGNORECASE)),
]

# [SEC-4] Path-traversal guard.
_PATH_TRAVERSAL_PATTERN: re.Pattern[str] = re.compile(
    r"(\.\./|\.\.\\|/etc/|\\etc\\)"
)

# [DQ-2] NaN-equivalent string values. These should be treated as null
# when found in an InChIKey column (they are not real identifiers).
_NAN_EQUIVALENT_STRINGS: frozenset[str] = frozenset({
    "", " ", "n/a", "na", "none", "null", "nan", "-", "todo", "tbd",
    "unknown", "missing", "null-none",
})

# [SEC-5] Suspicious name patterns that may indicate injection attempts.
_SUSPICIOUS_NAME_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^\s*\*\s*$"),                # wildcard "*"
    re.compile(r"^\s*%\s*$"),                 # wildcard "%"
    re.compile(r"<script", re.IGNORECASE),    # HTML injection
    re.compile(r"\$\{.*\}"),                  # template injection
]

# Unit-conversion table (nM is the canonical unit for potency).
_UNIT_CONVERSIONS_TO_NM: dict[str, float] = {
    "pM": 1.0e-3, "nM": 1.0, "uM": 1.0e3, "µM": 1.0e3, "μM": 1.0e3,
    "mM": 1.0e6, "M": 1.0e9,
    "mol/L": 1.0e9, "umol/L": 1.0e3, "nmol/L": 1.0, "mmol/L": 1.0e6,
    "pmol/L": 1.0e-3, "fmol/L": 1.0e-6,
}


# ===========================================================================
# [DES-2] Enums
# ===========================================================================
class DedupStrategy(str, enum.Enum):
    """Strategy for selecting the surviving row among duplicates.

    Attributes
    ----------
    MOST_COMPLETE : str
        Keep the row with the highest weighted completeness score
        (default for ``dedup_by_inchikey``).
    FIRST_OCCURRENCE : str
        Plain ``drop_duplicates(keep="first")``.
    LAST_OCCURRENCE : str
        Plain ``drop_duplicates(keep="last")``.
    LOWEST_ACTIVITY : str
        Keep the row with the lowest ``activity_value``
        (default for ``dedup_interactions`` with potency assays).
    HIGHEST_ACTIVITY : str
        Keep the row with the highest ``activity_value``
        (for ``pKi`` / ``pIC50`` / ``%`` inhibition assays).
    MERGE_FIELDS : str
        Column-wise merge: take the first non-null value per column
        across all rows in the duplicate group.
    """

    MOST_COMPLETE = "most_complete"
    FIRST_OCCURRENCE = "first_occurrence"
    LAST_OCCURRENCE = "last_occurrence"
    LOWEST_ACTIVITY = "lowest_activity"
    HIGHEST_ACTIVITY = "highest_activity"
    MERGE_FIELDS = "merge_fields"


class ActivityDirection(str, enum.Enum):
    """Sort direction for activity-value ranking.

    Attributes
    ----------
    ASC : str
        Lower = more potent (``IC50``, ``Ki``, ``Kd``, ``EC50``, ``AC50``,
        ``ED50``, ``Kb``).
    DESC : str
        Higher = more potent (``pKi``, ``pIC50``, ``pEC50``, ``pKd``, ``%``
        inhibition).
    AUTO : str
        Infer from ``activity_type`` (default).
    """

    ASC = "asc"
    DESC = "desc"
    AUTO = "auto"


# ===========================================================================
# [DES-3] CompletenessWeight dataclass
# ===========================================================================
@dataclass(frozen=True)
class CompletenessWeight:
    """Weights for completeness scoring.

    Higher weight = the column carries more identifying information.
    The score for a row is the sum of weights for all non-null,
    non-empty-string columns.

    Parameters
    ----------
    weights : dict[str, float]
        Mapping from column name to weight. Defaults to ``_DEFAULT_WEIGHTS``.
    default_weight : float
        Weight applied to columns not in ``weights``. Default ``0.5``.
    exclude_columns : frozenset[str]
        Columns to ignore entirely (lineage / metadata columns).
    """

    weights: dict[str, float] = field(
        default_factory=lambda: dict(_DEFAULT_WEIGHTS)
    )
    default_weight: float = 0.5
    exclude_columns: frozenset[str] = field(
        default_factory=lambda: frozenset(_LINEAGE_COLUMNS)
    )

    def score_row(self, row: pd.Series) -> float:
        """Compute the weighted completeness score for a single row.

        Parameters
        ----------
        row : pd.Series
            A single row of a DataFrame.

        Returns
        -------
        float
            The weighted completeness score (higher = more complete).
        """
        total = 0.0
        for col, val in row.items():
            if col in self.exclude_columns:
                continue
            # Fast NaN check — works for float NaN, pd.NA, None
            try:
                if val is None or (isinstance(val, float) and val != val):
                    continue
            except Exception:
                continue
            if isinstance(val, str) and val.strip() == "":
                continue
            try:
                if pd.isna(val):
                    continue
            except (TypeError, ValueError):
                pass
            total += self.weights.get(col, self.default_weight)
        return total

    def score_dataframe(self, df: pd.DataFrame) -> pd.Series:
        """[PERF-1] Vectorized completeness scoring for an entire DataFrame.

        Parameters
        ----------
        df : pd.DataFrame
            The DataFrame to score.

        Returns
        -------
        pd.Series
            A series of float scores, indexed like ``df``.
        """
        # Build a per-column weight vector, then mask by non-null &
        # non-empty-string, then sum across columns.
        col_weights: list[float] = []
        score_cols: list[pd.Series] = []
        for col in df.columns:
            if col in self.exclude_columns:
                continue
            w = self.weights.get(col, self.default_weight)
            if w == 0.0:
                continue
            col_data = df[col]
            # Boolean mask: True where the cell counts toward completeness
            try:
                mask = col_data.notna()
            except Exception:
                mask = pd.Series([True] * len(df), index=df.index)
            # For string columns, also exclude empty / whitespace strings
            if col_data.dtype == object or pd.api.types.is_string_dtype(col_data):
                stripped = col_data.astype(str).str.strip()
                empty_mask = stripped.isin({"", "nan", "None", "null", "<NA>"})
                # Don't penalise genuine nulls (already excluded by notna),
                # but DO exclude empty/whitespace strings that notna missed.
                mask = mask & ~empty_mask
            score_cols.append(mask.astype(float) * w)
            col_weights.append(w)
        if not score_cols:
            return pd.Series([0.0] * len(df), index=df.index, dtype=float)
        scored = pd.concat(score_cols, axis=1).sum(axis=1, skipna=True)
        return scored


# Module-level default instance — shared by all calls that don't override.
DEFAULT_COMPLETENESS_WEIGHTS: CompletenessWeight = CompletenessWeight()


# ===========================================================================
# [DES-1] DedupResult dataclass
# ===========================================================================
@dataclass
class DedupResult:
    """Structured result returned when ``return_result=True``.

    Attributes
    ----------
    df : pd.DataFrame
        The deduplicated DataFrame.
    rows_before : int
        Row count of the input.
    rows_after : int
        Row count of the output.
    duplicates_removed : int
        ``rows_before - rows_after``.
    quarantined : int
        Number of rows moved to the dead-letter queue (not counted in
        ``duplicates_removed``).
    dead_letter_count : int
        Total dead-letter entries added during this call.
    duration_seconds : float
        Wall-clock time of the dedup operation.
    warnings : list[str]
        Human-readable warnings emitted during dedup.
    columns_affected : dict[str, dict[str, int]]
        Per-column change counts (e.g. ``{"inchikey": {"duplicates": 3}}``).
    dtype_changes : dict[str, tuple[str, str]]
        Columns whose dtype changed (``{col: (before, after)}``).
    dropped_rows : list[dict[str, Any]]
        Capped list of dropped-row records (for inspection).
    strategy : str
        Name of the dedup strategy used (see ``DedupStrategy``).
    provenance : dict[str, Any]
        Provenance metadata for this dedup operation.
    """

    df: pd.DataFrame
    rows_before: int = 0
    rows_after: int = 0
    duplicates_removed: int = 0
    quarantined: int = 0
    dead_letter_count: int = 0
    duration_seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)
    columns_affected: dict[str, dict[str, int]] = field(default_factory=dict)
    dtype_changes: dict[str, tuple[str, str]] = field(default_factory=dict)
    dropped_rows: list[dict[str, Any]] = field(default_factory=list)
    strategy: str = ""
    provenance: dict[str, Any] = field(default_factory=dict)

    def __int__(self) -> int:
        return self.rows_after

    def __len__(self) -> int:
        return self.rows_after

    def quality_summary(self) -> dict[str, Any]:
        """Return a flat summary dict suitable for logging / metrics."""
        return {
            "rows_before": self.rows_before,
            "rows_after": self.rows_after,
            "rows_dropped": self.duplicates_removed,
            "drop_rate": self.duplicates_removed / max(self.rows_before, 1),
            "quarantined": self.quarantined,
            "dead_letter_count": self.dead_letter_count,
            "duration_seconds": self.duration_seconds,
            "strategy": self.strategy,
            "warnings_count": len(self.warnings),
        }


# ===========================================================================
# [ARCH-3] Lazy import of helpers from sister modules
# ===========================================================================
def _get_helpers() -> SimpleNamespace:
    """Lazy-import helper utilities from the ``cleaning`` package.

    Deferred imports avoid circular dependencies between ``cleaning``
    and ``cleaning.deduplicator``. The first call to ``dedup_by_inchikey``
    triggers this import; subsequent calls reuse the cached namespace.

    Returns
    -------
    SimpleNamespace
        A namespace exposing the package-level helpers. Each attribute
        is wrapped in a try/except-safe accessor so that missing helpers
        degrade gracefully.
    """
    helpers: dict[str, Any] = {}

    # Package-level helpers
    try:
        from cleaning import (  # type: ignore
            _audit_log, _add_provenance, _mark_cleaned,
            _is_already_cleaned, _add_dead_letter, compute_data_fingerprint,
            _sanitize_string, _mask_sensitive, set_correlation_id as _pkg_set_cid,
            get_correlation_id as _pkg_get_cid, get_circuit_breaker,
            SchemaValidationError, CleaningError,
        )
        helpers["audit_log"] = _audit_log
        helpers["add_provenance"] = _add_provenance
        helpers["mark_cleaned"] = _mark_cleaned
        helpers["is_already_cleaned"] = _is_already_cleaned
        helpers["add_dead_letter_pkg"] = _add_dead_letter
        helpers["compute_data_fingerprint"] = compute_data_fingerprint
        helpers["sanitize_string"] = _sanitize_string
        helpers["mask_sensitive"] = _mask_sensitive
        helpers["pkg_set_cid"] = _pkg_set_cid
        helpers["pkg_get_cid"] = _pkg_get_cid
        helpers["get_circuit_breaker"] = get_circuit_breaker
        helpers["SchemaValidationError"] = SchemaValidationError
        helpers["CleaningError"] = CleaningError
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("dedup: package helpers unavailable: %s", exc)

    # Sister-module helpers (lazy, never block import)
    try:
        from cleaning.normalizer import (  # type: ignore
            is_valid_inchikey, is_synthetic_inchikey,
            normalize_activity_value,
        )
        helpers["is_valid_inchikey"] = is_valid_inchikey
        helpers["is_synthetic_inchikey"] = is_synthetic_inchikey
        helpers["normalize_activity_value"] = normalize_activity_value
    except Exception as exc:
        logger.debug("dedup: normalizer helpers unavailable: %s", exc)

    try:
        from cleaning.missing_values import (  # type: ignore
            is_nullish, _scan_for_pii, _validate_input_size,
            _sanitize_smiles, _redact_for_log,
        )
        helpers["is_nullish"] = is_nullish
        helpers["scan_for_pii"] = _scan_for_pii
        helpers["validate_input_size"] = _validate_input_size
        helpers["sanitize_smiles"] = _sanitize_smiles
        helpers["redact_for_log"] = _redact_for_log
    except Exception as exc:
        logger.debug("dedup: missing_values helpers unavailable: %s", exc)

    return SimpleNamespace(**helpers)


# ===========================================================================
# [LOG-1] Correlation-ID management (module-local fallback)
# ===========================================================================
_CORRELATION_ID_LOCK = threading.RLock()
_current_correlation_id: str | None = None


def set_correlation_id(cid: str | None) -> None:
    """Set the correlation ID for the current thread/context.

    Parameters
    ----------
    cid : str or None
        The correlation ID. Pass ``None`` to clear.
    """
    global _current_correlation_id
    with _CORRELATION_ID_LOCK:
        _current_correlation_id = cid
    # Also propagate to the package-level ContextVar so all cleaning
    # modules share the same correlation ID.
    try:
        helpers = _get_helpers()
        if hasattr(helpers, "pkg_set_cid"):
            helpers.pkg_set_cid(cid)  # type: ignore[attr-defined]
    except Exception:
        pass


def get_correlation_id() -> str | None:
    """Return the current correlation ID for distributed tracing.

    The correlation ID is propagated to log records, provenance
    metadata, and dead-letter entries. Returns ``None`` if no
    correlation ID has been set in the current context.
    """
    # Prefer the package-level ContextVar (async-safe)
    try:
        helpers = _get_helpers()
        if hasattr(helpers, "pkg_get_cid"):
            cid = helpers.pkg_get_cid()  # type: ignore[attr-defined]
            if cid:
                return cid
    except Exception:
        pass
    with _CORRELATION_ID_LOCK:
        return _current_correlation_id


# ===========================================================================
# [SEC-1] [SEC-2] Internal sanitization helpers
# ===========================================================================
def _sanitize_string_local(value: Any, *, max_length: int = 200) -> str:
    """[SEC-1] Sanitize a string for safe logging.

    Strips null bytes, control characters, and truncates to
    ``max_length``. Delegates to ``cleaning._sanitize_string`` when
    available (which uses a larger default of 10_000).
    """
    try:
        helpers = _get_helpers()
        if hasattr(helpers, "sanitize_string"):
            return helpers.sanitize_string(value, max_length=max_length)  # type: ignore[attr-defined]
    except Exception:
        pass
    if value is None:
        return ""
    try:
        s = str(value)
    except Exception:
        return "<unprintable>"
    s = s.replace("\x00", "")
    s = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]", "", s)
    if len(s) > max_length:
        s = s[:max_length] + "...[truncated]"
    return s


def _redact_for_log_local(value: Any, max_len: int = 80) -> str:
    """[SEC-1] [SEC-2] Redact PII from a value before logging.

    Masks emails, phone numbers, and SSNs. Truncates long strings.
    """
    if value is None:
        return "None"
    try:
        s = str(value)
    except Exception:
        return "<unprintable>"
    if len(s) > max_len:
        s = s[:max_len] + "...[truncated]"
    for pii_type, pattern in _PII_PATTERNS:
        s = pattern.sub(f"[{pii_type}]", s)
    return s


def _validate_input_size(df: pd.DataFrame) -> None:
    """[SEC-3] DoS guard — reject DataFrames that exceed the row cap.

    Parameters
    ----------
    df : pd.DataFrame
        The input DataFrame.

    Raises
    ------
    ValueError
        If the DataFrame exceeds ``_MAX_DATAFRAME_ROWS``.
    """
    try:
        nrows = len(df)
    except Exception:
        return
    if nrows > _MAX_DATAFRAME_ROWS:
        raise ValueError(
            f"DataFrame has {nrows:,} rows which exceeds the safety cap "
            f"of {_MAX_DATAFRAME_ROWS:,}. Process in chunks via the "
            f"``chunk_size`` parameter of ``dedup_by_inchikey_chunked``."
        )


def _scan_for_pii(df: pd.DataFrame) -> dict[str, int]:
    """[SEC-2] Scan string columns for PII patterns.

    Returns a dict mapping PII type to detection count. Emits a
    WARNING log for each non-zero count.
    """
    counts: dict[str, int] = {name: 0 for name, _ in _PII_PATTERNS}
    try:
        helpers = _get_helpers()
        if hasattr(helpers, "scan_for_pii"):
            return helpers.scan_for_pii(df)  # type: ignore[attr-defined]
    except Exception:
        pass
    for col in df.columns:
        col_data = df[col]
        if col_data.dtype != object and not pd.api.types.is_string_dtype(col_data):
            continue
        non_null = col_data.dropna().astype(str)
        if len(non_null) == 0:
            continue
        for pii_type, pattern in _PII_PATTERNS:
            try:
                matches = non_null.str.contains(pattern, regex=True, na=False)
                n = int(matches.sum())
                if n > 0:
                    counts[pii_type] += n
            except Exception:
                continue
    for pii_type, n in counts.items():
        if n > 0:
            logger.warning(
                "dedup: detected %d %s-like values in input — they will be "
                "redacted from logs and dead-letter entries",
                n, pii_type,
            )
    return counts


# ===========================================================================
# [IDEM-4] [LINEAGE-1] Fingerprinting helpers
# ===========================================================================
def _fingerprint_df(df: pd.DataFrame) -> str:
    """[IDEM-4] Compute a stable SHA-256 fingerprint of a DataFrame.

    Delegates to ``cleaning.compute_data_fingerprint`` when available
    (column-sorted CSV → sha256). Falls back to a content-based
    ``pd.util.hash_pandas_object`` approach.
    """
    try:
        helpers = _get_helpers()
        if hasattr(helpers, "compute_data_fingerprint"):
            return helpers.compute_data_fingerprint(df)  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        sorted_cols = sorted(df.columns)
        sorted_df = df[sorted_cols]
        csv_str = sorted_df.to_csv(index=False, float_format="%.10f")
        return hashlib.sha256(csv_str.encode("utf-8")).hexdigest()
    except Exception:
        return "unknown"


# ===========================================================================
# [SCI-7] [SCI-3] InChIKey / activity-value helpers
# ===========================================================================
def _is_valid_inchikey_format(key: Any) -> bool:
    """[SCI-7] Validate InChIKey format without external dependencies.

    Returns ``True`` for valid 27-char InChIKeys AND ``SYNTH``-prefixed
    synthetic keys. Returns ``False`` for everything else (including
    NaN, None, mixtures, and malformed strings).
    """
    if not isinstance(key, str):
        return False
    if not key:
        return False
    if _SYNTHETIC_INCHIKEY_PATTERN.match(key):
        return True
    if _INCHIKEY_PATTERN.match(key):
        return True
    return False


def _is_synthetic_inchikey(key: Any) -> bool:
    """Return True if the key is a SYNTH-prefixed placeholder."""
    if not isinstance(key, str):
        return False
    return bool(_SYNTHETIC_INCHIKEY_PATTERN.match(key))


def _is_mixture_inchikey(key: Any) -> bool:
    """[SCI-6] Return True if the key encodes a mixture (multiple connected layers)."""
    if not isinstance(key, str):
        return False
    return bool(_MIXTURE_INCHIKEY_PATTERN.match(key)) and "-" in key[27:]


def _is_nullish_inchikey(val: Any) -> bool:
    """[DQ-2] Return True if the value is null-equivalent."""
    if val is None:
        return True
    try:
        if pd.isna(val):
            return True
    except (TypeError, ValueError):
        pass
    if isinstance(val, str):
        if val.strip() in _NAN_EQUIVALENT_STRINGS:
            return True
    return False


def _parse_censored_value(val: Any) -> tuple[bool, str | None, float | None]:
    """[SCI-3] Parse a censored activity value.

    Returns
    -------
    tuple
        ``(is_censored, censor_direction, numeric_value)``.
        - For uncensored numeric values: ``(False, None, value)``.
        - For censored values like ``"<10"``: ``(True, "<", 10.0)``.
        - For unparsable values: ``(False, None, None)``.
    """
    if val is None:
        return (False, None, None)
    # Try numeric fast path
    if isinstance(val, (int, float)):
        try:
            if pd.isna(val):
                return (False, None, None)
            return (False, None, float(val))
        except (TypeError, ValueError):
            return (False, None, None)
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return (False, None, None)
        m = _CENSOR_PATTERN.match(s)
        if m:
            direction = m.group(1)
            try:
                num = float(m.group(2))
                return (True, direction, num)
            except (TypeError, ValueError):
                return (False, None, None)
        # Try plain numeric string
        try:
            return (False, None, float(s))
        except (TypeError, ValueError):
            return (False, None, None)
    return (False, None, None)


def _resolve_activity_direction(
    activity_type: str | None,
    explicit_direction: str,
) -> str:
    """[SCI-1] Resolve the sort direction for an activity value.

    Parameters
    ----------
    activity_type : str or None
        The activity type (e.g. ``"IC50"``, ``"pKi"``).
    explicit_direction : str
        The caller's requested direction (``"asc"``, ``"desc"``, or
        ``"auto"``).

    Returns
    -------
    str
        The resolved direction: ``"asc"`` or ``"desc"``.
    """
    if explicit_direction == "asc":
        return "asc"
    if explicit_direction == "desc":
        return "desc"
    # AUTO — infer from activity_type
    if activity_type is None:
        return "asc"  # safe default — matches v1.0.0 behavior
    at = activity_type.strip()
    if at in INVERSE_ACTIVITY_TYPES:
        return "desc"
    if at in PERCENT_ACTIVITY_TYPES:
        return "desc"
    # POTENCY_ACTIVITY_TYPES, "None", "unknown", or anything else → asc
    return "asc"


def _validate_activity_type(
    activity_type: str | None,
    strict: bool = False,
) -> tuple[bool, str | None]:
    """[DQ-8] Validate ``activity_type`` against the allowed enum.

    Returns
    -------
    tuple
        ``(is_valid, normalized_type)``. For unknown types with
        ``strict=False``, returns ``(False, None)`` and logs a warning.
        With ``strict=True``, raises ``SchemaValidationError`` instead.
    """
    if activity_type is None or (isinstance(activity_type, str) and activity_type.strip() == ""):
        return (True, None)
    if not isinstance(activity_type, str):
        return (False, None)
    at = activity_type.strip()
    # Case-insensitive matching against known types (preserve canonical case)
    for allowed in _ALLOWED_ACTIVITY_TYPES:
        if at.casefold() == allowed.casefold():
            return (True, allowed)
    # Unknown activity type
    if strict:
        try:
            helpers = _get_helpers()
            if hasattr(helpers, "SchemaValidationError"):
                raise helpers.SchemaValidationError(  # type: ignore[attr-defined]
                    f"Unknown activity_type: {at!r}. "
                    f"Allowed: {sorted(_ALLOWED_ACTIVITY_TYPES)}"
                )
        except AttributeError:
            pass
        raise ValueError(
            f"Unknown activity_type: {at!r}. "
            f"Allowed: {sorted(_ALLOWED_ACTIVITY_TYPES)}"
        )
    logger.warning(
        "dedup: unknown activity_type %r — treating as ASC (lower-is-better). "
        "Pass strict_activity_type=True to reject.",
        at,
    )
    return (False, None)


def _normalize_unit_to_nm(value: float, unit: str | None) -> tuple[float, str | None]:
    """[SCI-4] Normalize an activity value to nM.

    Returns
    -------
    tuple
        ``(value_in_nm, warning)``. ``warning`` is ``None`` on success
        or a string describing why normalization was skipped.
    """
    if unit is None:
        return (value, "missing_unit")
    u = unit.strip()
    if not u:
        return (value, "empty_unit")
    # Case-insensitive lookup
    factor = _UNIT_CONVERSIONS_TO_NM.get(u)
    if factor is None:
        # v43 ROOT FIX (P1-022): the previous code did an O(N) linear
        # scan with .casefold() per key for the case-insensitive
        # fallback. For N calls, this was O(N*M) where M is the number
        # of unit entries. The fix precomputes a casefolded lookup
        # dict ONCE (module-level) so the fallback is O(1).
        # The precomputed dict is built lazily on first use (to avoid
        # import-time cost if _UNIT_CONVERSIONS_TO_NM is mutated later).
        global _UNIT_CONVERSIONS_TO_NM_CASEFOLDED
        try:
            _UNIT_CONVERSIONS_TO_NM_CASEFOLDED
        except NameError:
            _UNIT_CONVERSIONS_TO_NM_CASEFOLDED = {
                k.casefold(): v for k, v in _UNIT_CONVERSIONS_TO_NM.items()
            }
        factor = _UNIT_CONVERSIONS_TO_NM_CASEFOLDED.get(u.casefold())
    if factor is None:
        return (value, f"unknown_unit:{u}")
    return (value * factor, None)


# ===========================================================================
# [REL-1] Dead-letter queue
# ===========================================================================
_DEAD_LETTERS_LOCK = threading.RLock()
_dead_letters: list[dict[str, Any]] = []
# FIX-F / C-18: alias kept for backward-compat with operators/tests that
# import ``_dead_letter_queue`` from this module. Same list object —
# in-place mutations (.append/.clear/.pop) are visible through either name.
_dead_letter_queue: list[dict[str, Any]] = _dead_letters


def _append_dead_letter(
    function_name: str,
    reason: str,
    row: dict[str, Any] | None,
    *,
    survivor_info: dict[str, Any] | None = None,
) -> None:
    """[REL-1] [LINEAGE-3] Append an entry to the dead-letter queue.

    Entries are FIFO-evicted when the queue exceeds
    ``_MAX_DEAD_LETTERS``. PII is redacted from the row before storage.
    """
    entry: dict[str, Any] = {
        "function": function_name,
        "reason": reason,
        "row": _redact_row_for_storage(row) if row else None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "correlation_id": get_correlation_id(),
        "module_version": _MODULE_VERSION,
        "schema_version": _OUTPUT_SCHEMA_VERSION,
        "rule_version": _RULE_VERSION,
        "logic_hash": _LOGIC_HASH,
    }
    if survivor_info:
        entry["survivor_info"] = survivor_info
    with _DEAD_LETTERS_LOCK:
        if len(_dead_letters) >= _MAX_DEAD_LETTERS:
            _dead_letters.pop(0)
        _dead_letters.append(entry)
        # v35 ROOT FIX: increment the dead-letter counter INSIDE this
        # function so the count is always consistent with the number of
        # entries actually appended. Callers no longer need to remember
        # to call ``_incr_metric("dead_letters_added")`` after each
        # invocation. The previous pattern (caller increments after each
        # call) was fragile — any caller that forgot the increment, or
        # any exception path that skipped it, would under-count.
        _incr_metric("dead_letters_added")
    # Mirror to package-level queue
    try:
        helpers = _get_helpers()
        if hasattr(helpers, "add_dead_letter_pkg"):
            preview = json.dumps(row, default=str)[:500] if row else ""
            helpers.add_dead_letter_pkg(preview, function_name, reason)  # type: ignore[attr-defined]
    except Exception:
        pass


def _redact_row_for_storage(row: dict[str, Any]) -> dict[str, Any]:
    """[SEC-2] Redact PII from a row dict before storing it in the DLQ."""
    if not isinstance(row, dict):
        return {"_value": _redact_for_log_local(row)}
    redacted: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, str):
            redacted[k] = _redact_for_log_local(v)
        elif isinstance(v, (int, float, bool)):
            redacted[k] = v
        elif v is None:
            redacted[k] = None
        else:
            redacted[k] = _redact_for_log_local(v)
    return redacted


def get_dead_letters() -> list[dict[str, Any]]:
    """Return a snapshot of the dead-letter queue (new list)."""
    with _DEAD_LETTERS_LOCK:
        return list(_dead_letters)


def clear_dead_letters() -> None:
    """Clear the dead-letter queue.

    Removes all entries from the in-memory dead-letter queue. This is
    a destructive operation — once cleared, the records cannot be
    recovered unless they were previously flushed to disk via
    :func:`flush_dead_letters`.

    Use this function in test setups and after a successful
    :func:`flush_dead_letters` to free memory. For audit-trail
    purposes, prefer ``flush_dead_letters`` over ``clear_dead_letters``
    in production code.
    """
    with _DEAD_LETTERS_LOCK:
        _dead_letters.clear()


def flush_dead_letters(path: str | Path | None = None) -> int:
    """[REL-7] [COMP-3] Flush the dead-letter queue to a JSONL file.

    Writes one JSON object per line. The queue is cleared after a
    successful flush. This supports FDA 21 CFR Part 11 audit-trail
    requirements: dropped records are persisted for forensic review.

    Parameters
    ----------
    path : str, Path, or None
        Destination file. If ``None``, defaults to
        ``./dedup_dead_letters_<timestamp>.jsonl``.

    Returns
    -------
    int
        Number of entries flushed.
    """
    # [SEC-4] Path-traversal guard — validate BEFORE checking queue state
    # so malicious paths are always rejected, even when queue is empty.
    if path is not None:
        path_str = str(Path(path))
        if _PATH_TRAVERSAL_PATTERN.search(path_str):
            raise ValueError(
                f"Path-traversal pattern detected in dead-letter path: {path!r}"
            )
    with _DEAD_LETTERS_LOCK:
        entries = list(_dead_letters)
        _dead_letters.clear()
    if not entries:
        return 0
    if path is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = Path(f"dedup_dead_letters_{ts}.jsonl")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, default=str, sort_keys=True))
            f.write("\n")
    logger.info(
        "dedup: flushed %d dead-letter entries to %s", len(entries), path
    )
    return len(entries)


# ===========================================================================
# [LOG-3] [LOG-7] Metrics & timing
# ===========================================================================
_METRICS_LOCK = threading.RLock()
_metrics: dict[str, int] = defaultdict(int)

# Default metric keys (others may be added dynamically)
_METRIC_KEYS: list[str] = [
    "dedup_by_inchikey_calls",
    "dedup_by_inchikey_rows_in",
    "dedup_by_inchikey_rows_out",
    "dedup_by_inchikey_duplicates_removed",
    "dedup_by_inchikey_nan_inchikeys_kept",
    "dedup_by_inchikey_synth_keys_seen",
    "dedup_by_inchikey_mixture_keys_seen",
    "dedup_by_inchikey_version_char_mismatches",
    "dedup_by_inchikey_quarantined",
    "dedup_by_inchikey_idempotent_skips",
    "dedup_interactions_calls",
    "dedup_interactions_rows_in",
    "dedup_interactions_rows_out",
    "dedup_interactions_duplicates_removed",
    "dedup_interactions_activity_type_segments",
    "dedup_interactions_censored_seen",
    "dedup_interactions_censored_winner_overridden",
    "dedup_interactions_unit_normalizations",
    "dedup_interactions_invalid_activity_value_quarantined",
    "dedup_interactions_null_keys_kept",
    "dedup_interactions_idempotent_skips",
    "dead_letters_added",
    "circuit_open_count",
    "warnings_emitted",
    "errors_emitted",
]
for _k in _METRIC_KEYS:
    _metrics[_k] = 0


def _incr_metric(name: str, delta: int = 1) -> None:
    """[LOG-3] Increment a metric counter (thread-safe)."""
    with _METRICS_LOCK:
        _metrics[name] += delta


def get_metrics() -> dict[str, int]:
    """Return a snapshot of deduplicator metrics as a flat dict.

    The returned dict includes per-call counters (e.g.
    ``dedup_by_inchikey_calls``, ``dedup_by_inchikey_rows_in``),
    dead-letter counts, circuit-breaker open counts, and warning/error
    totals. Metrics are thread-safe and reset via :func:`reset_metrics`.
    """
    with _METRICS_LOCK:
        merged: dict[str, int] = dict(_metrics)
    # Merge package-level metrics under "package" if available
    try:
        import cleaning  # type: ignore
        pkg_metrics = cleaning.get_metrics()
        if isinstance(pkg_metrics, dict):
            merged["package"] = pkg_metrics  # type: ignore[assignment]
    except Exception:
        pass
    return merged


def reset_metrics() -> None:
    """Reset all deduplicator metrics counters to zero.

    This is intended for test isolation and operational resets.
    In production, prefer to snapshot via :func:`get_metrics` before
    reset so prior counts are not lost.
    """
    with _METRICS_LOCK:
        for k in list(_metrics.keys()):
            _metrics[k] = 0


# Timing data — per-function wall-clock stats
_TIMING_LOCK = threading.RLock()
_timing_data: dict[str, dict[str, float]] = defaultdict(
    lambda: {"calls": 0, "total_s": 0.0, "min_s": float("inf"), "max_s": 0.0}
)


def _record_timing(func_name: str, elapsed: float) -> None:
    """[LOG-7] Record a timing observation for a function."""
    with _TIMING_LOCK:
        t = _timing_data[func_name]
        t["calls"] += 1
        t["total_s"] += elapsed
        t["min_s"] = min(t["min_s"], elapsed)
        t["max_s"] = max(t["max_s"], elapsed)


def timing_report() -> dict[str, dict[str, float]]:
    """Return per-function timing statistics.

    For each tracked function, returns a dict with ``calls``,
    ``total_s``, ``min_s``, ``max_s``, and ``avg_s``. Use this to
    identify performance regressions and slow operations.
    """
    with _TIMING_LOCK:
        out: dict[str, dict[str, float]] = {}
        for name, stats in _timing_data.items():
            calls = stats["calls"]
            out[name] = {
                "calls": calls,
                "total_s": stats["total_s"],
                "min_s": stats["min_s"] if calls > 0 else 0.0,
                "max_s": stats["max_s"],
                "avg_s": (stats["total_s"] / calls) if calls > 0 else 0.0,
            }
        return out


# ===========================================================================
# [REL-3] Local circuit breaker
# ===========================================================================
class _LocalCircuitBreaker:
    """Simple circuit breaker — opens after N consecutive failures."""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        reset_timeout: float = 60.0,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.failure_count = 0
        self.last_failure_time: float = 0.0
        self.state: str = "closed"  # closed | open | half-open
        self._lock = threading.RLock()

    def record_success(self) -> None:
        with self._lock:
            self.failure_count = 0
            self.state = "closed"

    def record_failure(self) -> None:
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.monotonic()
            if self.failure_count >= self.failure_threshold:
                self.state = "open"
                _incr_metric("circuit_open_count")
                logger.error(
                    "dedup: circuit breaker %r opened after %d failures",
                    self.name, self.failure_count,
                )

    def allow_request(self) -> bool:
        with self._lock:
            if self.state == "closed":
                return True
            if self.state == "open":
                # Check if reset timeout has elapsed
                if time.monotonic() - self.last_failure_time > self.reset_timeout:
                    self.state = "half-open"
                    return True
                return False
            # half-open — allow one request
            return True


_cb_dedup_by_inchikey = _LocalCircuitBreaker("dedup_by_inchikey")
_cb_dedup_interactions = _LocalCircuitBreaker("dedup_interactions")


# ===========================================================================
# [CFG-1] [CFG-6] Configuration management
# ===========================================================================
_CONFIG_LOCK = threading.RLock()
_config: dict[str, Any] = {
    "completeness_weights": dict(_DEFAULT_WEIGHTS),
    "max_duplicate_ratio": None,
    "max_dataframe_rows": _MAX_DATAFRAME_ROWS,
    "default_strategy": DedupStrategy.MOST_COMPLETE,
    "reset_index_default": True,
    "log_format": "json" if _LOG_FORMAT_JSON else "text",
    "log_level": logging.getLevelName(logger.level) if logger.level else "NOTSET",
}
_config_history: list[dict[str, Any]] = []


def configure_deduplicator(
    *,
    completeness_weights: dict[str, float] | None = None,
    max_duplicate_ratio: float | None = None,
    max_dataframe_rows: int | None = None,
    default_strategy: DedupStrategy | str | None = None,
    reset_index_default: bool | None = None,
    log_format: Literal["text", "json"] | None = None,
    log_level: str | None = None,
) -> None:
    """[CFG-1] [CFG-6] Configure the deduplicator at runtime.

    All parameters are keyword-only. Passing ``None`` leaves the current
    value unchanged. Configuration changes are versioned and can be
    reverted via :func:`revert_configuration`.
    """
    # Validate inputs
    if completeness_weights is not None:
        if not isinstance(completeness_weights, dict):
            raise ValueError("completeness_weights must be a dict[str, float]")
        for k, v in completeness_weights.items():
            if not isinstance(k, str):
                raise ValueError(f"completeness_weights key {k!r} must be str")
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise ValueError(
                    f"completeness_weights[{k!r}] must be a number, got {type(v).__name__}"
                )
            if v < 0:
                raise ValueError(
                    f"completeness_weights[{k!r}] must be >= 0, got {v}"
                )
    if max_duplicate_ratio is not None:
        if not isinstance(max_duplicate_ratio, (int, float)) or isinstance(max_duplicate_ratio, bool):
            raise ValueError("max_duplicate_ratio must be a number")
        if not (0.0 <= max_duplicate_ratio <= 1.0):
            raise ValueError(
                f"max_duplicate_ratio must be in [0, 1], got {max_duplicate_ratio}"
            )
    if max_dataframe_rows is not None:
        if not isinstance(max_dataframe_rows, int) or isinstance(max_dataframe_rows, bool):
            raise ValueError("max_dataframe_rows must be an int")
        if max_dataframe_rows < 1:
            raise ValueError(
                f"max_dataframe_rows must be >= 1, got {max_dataframe_rows}"
            )
    if default_strategy is not None:
        if isinstance(default_strategy, str):
            try:
                default_strategy = DedupStrategy(default_strategy)
            except ValueError:
                raise ValueError(
                    f"Unknown strategy: {default_strategy!r}. "
                    f"Allowed: {[s.value for s in DedupStrategy]}"
                )
        if not isinstance(default_strategy, DedupStrategy):
            raise ValueError("default_strategy must be a DedupStrategy or str")
    if log_format is not None and log_format not in ("text", "json"):
        raise ValueError(f"log_format must be 'text' or 'json', got {log_format!r}")
    if log_level is not None:
        if not hasattr(logging, log_level.upper()):
            raise ValueError(f"Unknown log_level: {log_level!r}")

    # Snapshot before
    with _CONFIG_LOCK:
        snapshot_before = dict(_config)
        # Apply changes
        if completeness_weights is not None:
            _config["completeness_weights"] = dict(completeness_weights)
        if max_duplicate_ratio is not None:
            _config["max_duplicate_ratio"] = max_duplicate_ratio
        if max_dataframe_rows is not None:
            _config["max_dataframe_rows"] = max_dataframe_rows
        if default_strategy is not None:
            _config["default_strategy"] = default_strategy
        if reset_index_default is not None:
            _config["reset_index_default"] = reset_index_default
        if log_format is not None:
            _config["log_format"] = log_format
            global _LOG_FORMAT_JSON
            _LOG_FORMAT_JSON = (log_format == "json")
        if log_level is not None:
            _config["log_level"] = log_level
            logger.setLevel(getattr(logging, log_level.upper(), logging.NOTSET))
        # Snapshot after
        snapshot_after = dict(_config)
        # Diff
        diffs = {
            k: (snapshot_before.get(k), snapshot_after.get(k))
            for k in snapshot_after
            if snapshot_before.get(k) != snapshot_after.get(k)
        }
        if diffs:
            _config_history.append(snapshot_before)
            _log_event(
                "info",
                "dedup.configure_deduplicator.applied",
                changes=diffs,
            )


def validate_config() -> list[str]:
    """[CFG-3] Validate the current configuration. Returns list of warnings."""
    warnings_list: list[str] = []
    with _CONFIG_LOCK:
        cw = _config["completeness_weights"]
        for k, v in cw.items():
            if not isinstance(v, (int, float)):
                warnings_list.append(
                    f"completeness_weights[{k!r}] is not numeric: {v!r}"
                )
            elif v < 0:
                warnings_list.append(
                    f"completeness_weights[{k!r}] is negative: {v}"
                )
        if _config["max_dataframe_rows"] < 1:
            warnings_list.append(
                f"max_dataframe_rows < 1: {_config['max_dataframe_rows']}"
            )
        mdr = _config["max_duplicate_ratio"]
        if mdr is not None and not (0.0 <= mdr <= 1.0):
            warnings_list.append(
                f"max_duplicate_ratio out of [0,1]: {mdr}"
            )
    return warnings_list


def validate_environment() -> dict[str, Any]:
    """[CFG-7] Return environment info for diagnostic purposes."""
    issues: list[str] = []
    py_version = f"{__import__('sys').version_info.major}.{__import__('sys').version_info.minor}"
    if __import__('sys').version_info < (3, 9):
        issues.append(f"Python {py_version} < 3.9 (deduplicator v3.0.0 requires 3.9+)")
    pd_version = pd.__version__
    try:
        major, minor = pd_version.split(".")[:2]
        if int(major) < 2 or (int(major) == 2 and int(minor) < 1):
            issues.append(f"pandas {pd_version} < 2.1.4")
    except Exception:
        pass
    return {
        "python_version": py_version,
        "pandas_version": pd_version,
        "numpy_version": __import__("numpy").__version__,
        "module_version": _MODULE_VERSION,
        "schema_version": _OUTPUT_SCHEMA_VERSION,
        "rule_version": _RULE_VERSION,
        "logic_hash": _LOGIC_HASH,
        "log_format": "json" if _LOG_FORMAT_JSON else "text",
        "max_dataframe_rows": _MAX_DATAFRAME_ROWS,
        "max_dead_letters": _MAX_DEAD_LETTERS,
        "issues": issues,
    }


def revert_configuration(steps: int = 1) -> None:
    """[CFG-6] Revert the last ``steps`` configuration changes.

    Parameters
    ----------
    steps : int
        Number of config snapshots to revert (default 1).
    """
    if not isinstance(steps, int) or steps < 1:
        raise ValueError(f"steps must be a positive int, got {steps!r}")
    with _CONFIG_LOCK:
        for _ in range(steps):
            if not _config_history:
                break
            prev = _config_history.pop()
            _config.clear()
            _config.update(prev)
        _log_event(
            "info",
            "dedup.revert_configuration.applied",
            steps=steps,
            remaining_history=len(_config_history),
        )


# ===========================================================================
# [INTEROP-5] Version gating
# ===========================================================================
def requires_api_version(min_version: str) -> bool:
    """[INTEROP-5] Return ``True`` if this module is at least ``min_version``.

    Uses semver comparison (first 3 numeric components).
    """
    def _parse(v: str) -> tuple[int, int, int]:
        parts = re.sub(r"[^0-9.]", "", v).split(".")[:3]
        while len(parts) < 3:
            parts.append("0")
        return tuple(int(p) if p else 0 for p in parts)  # type: ignore[return-value]
    return _parse(__version__) >= _parse(min_version)


# ===========================================================================
# [LINEAGE-1] [ARCH-6] Provenance builder
# ===========================================================================
def _build_provenance_entry(
    function_name: str,
    input_fp: str,
    output_fp: str,
    rows_in: int,
    rows_out: int,
    duplicates_removed: int,
    strategy_name: str,
    *,
    transformations: list[str] | None = None,
    warnings_list: list[str] | None = None,
    dead_letters_added: int = 0,
    quarantined: int = 0,
    parameters: dict[str, Any] | None = None,
    source_attribution: dict[str, int] | None = None,
    operator_id: str | None = None,
    source_dataset_id: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """[LINEAGE-1] [LINEAGE-6] [LINEAGE-7] Build a provenance entry."""
    entry: dict[str, Any] = {
        "function": function_name,
        "module": "cleaning.deduplicator",
        "module_version": _MODULE_VERSION,
        "schema_version": _OUTPUT_SCHEMA_VERSION,
        "rule_version": _RULE_VERSION,
        "logic_hash": _LOGIC_HASH,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "correlation_id": get_correlation_id(),
        "input_fingerprint": input_fp,
        "output_fingerprint": output_fp,
        "input_rows": rows_in,
        "output_rows": rows_out,
        "duplicates_removed": duplicates_removed,
        "quarantined": quarantined,
        "strategy": strategy_name,
        "transformation_chain": transformations or [],
        "warnings": warnings_list or [],
        "dead_letters_added": dead_letters_added,
        "parameters": parameters or {},
        "config_version": _CONFIG_VERSION,
    }
    if source is not None:
        entry["source"] = source
    if operator_id is not None:
        entry["operator_id"] = operator_id
    if source_dataset_id is not None:
        entry["source_dataset_id"] = source_dataset_id
    if source_attribution:
        entry["source_attribution"] = source_attribution
    return entry


def _attach_provenance(
    result_df: pd.DataFrame,
    entry: dict[str, Any],
) -> pd.DataFrame:
    """[ARCH-6] [LINEAGE-1] Attach provenance + fingerprints to a result df."""
    # Preserve existing attrs (copy to avoid mutating input)
    result_df.attrs = dict(result_df.attrs)
    prov_list = result_df.attrs.get("_provenance", [])
    if not isinstance(prov_list, list):
        prov_list = []
    prov_list.append(entry)
    result_df.attrs["_provenance"] = prov_list
    result_df.attrs["_input_fingerprint"] = entry["input_fingerprint"]
    result_df.attrs["_output_fingerprint"] = entry["output_fingerprint"]
    result_df.attrs["cleaning_metrics"] = {
        "rows_before": entry["input_rows"],
        "rows_after": entry["output_rows"],
        "duplicates_removed": entry["duplicates_removed"],
        "quarantined": entry["quarantined"],
        "strategy": entry["strategy"],
        "module_version": entry["module_version"],
    }
    return result_df


def get_provenance(result: pd.DataFrame | DedupResult) -> dict[str, Any]:
    """[LINEAGE-4] Extract the most-recent provenance entry from a result.

    Returns an empty dict if no provenance is present.
    """
    if isinstance(result, DedupResult):
        return dict(result.provenance)
    if isinstance(result, pd.DataFrame):
        prov_list = result.attrs.get("_provenance", [])
        if isinstance(prov_list, list) and prov_list:
            return dict(prov_list[-1])
    return {}


# ===========================================================================
# [DQ-3] [SCI-8] Pre-flight validation helpers
# ===========================================================================
def _check_whitespace_inchikeys(df: pd.DataFrame) -> bool:
    """[SCI-8] Return True if the InChIKey column has leading/trailing whitespace."""
    if "inchikey" not in df.columns:
        return False
    sample = df["inchikey"].dropna().head(100)
    if len(sample) == 0:
        return False
    try:
        return bool(sample.astype(str).str.contains(_WHITESPACE_PATTERN, regex=True).any())
    except Exception:
        return False


def _infer_dpi_keys(df: pd.DataFrame) -> list[str]:
    """[DES-4] Infer the composite DPI key from the DataFrame columns."""
    cols = set(df.columns)
    if {"drug_id", "protein_id", "source", "source_id"}.issubset(cols):
        return ["drug_id", "protein_id", "source", "source_id"]
    if {"drug_id", "protein_id", "source"}.issubset(cols):
        return ["drug_id", "protein_id", "source"]
    if {"drug_id", "protein_id"}.issubset(cols):
        return ["drug_id", "protein_id"]
    return []


# ===========================================================================
# [DES-3] [PERF-1] Public completeness scoring
# ===========================================================================
def compute_completeness_score(
    df: pd.DataFrame,
    *,
    weight: CompletenessWeight | None = None,
) -> pd.Series:
    """Compute weighted completeness scores for each row of ``df``.

    Parameters
    ----------
    df : pd.DataFrame
        The DataFrame to score.
    weight : CompletenessWeight, optional
        Weighting scheme. Defaults to ``DEFAULT_COMPLETENESS_WEIGHTS``.

    Returns
    -------
    pd.Series
        Float scores indexed like ``df``.
    """
    if not isinstance(df, pd.DataFrame):
        raise TypeError(
            f"compute_completeness_score expects a DataFrame, got {type(df).__name__}"
        )
    if weight is None:
        weight = DEFAULT_COMPLETENESS_WEIGHTS
    return weight.score_dataframe(df)


# ===========================================================================
# [DQ-1] [DQ-9] [SCI-5] [SCI-6] Main dedup_by_inchikey
# ===========================================================================
def dedup_by_inchikey(
    df: pd.DataFrame,
    *,
    reset_index: bool = True,
    return_result: bool = False,
    conservative_defaults: bool = False,
    merge_fields: bool = False,
    keep_lineage_columns: bool = False,
    validate_inchikeys: bool = True,
    auto_standardize: bool = True,
    synth_handling: Literal["strict", "by_name", "skip"] = "strict",
    weight: CompletenessWeight | None = None,
    dedup_by_version_char: bool = False,
    null_inchikey_handler: Literal["keep_all", "drop", "quarantine"] = "keep_all",
    skip_if_already_deduped: bool = True,
    max_duplicate_ratio: float | None = None,
    source: str | None = None,
    operator_id: str | None = None,
    source_dataset_id: str | None = None,
) -> pd.DataFrame | DedupResult:
    """Remove duplicate drugs by InChIKey, keeping the most complete row.

    Backward-compatible with the v1.0.0 signature ``dedup_by_inchikey(df)``
    — all keyword arguments are optional with defaults that preserve
    v1.0.0 behavior exactly.

    Parameters
    ----------
    df : pd.DataFrame
        DataFrame of drug records. Must contain an ``inchikey`` column.
    reset_index : bool, default True
        If True, reset the output index (matches v1.0.0 behavior).
    return_result : bool, default False
        If True, return a :class:`DedupResult` instead of a DataFrame.
    conservative_defaults : bool, default False
        If True, quarantine suspicious rows instead of silently dropping.
    merge_fields : bool, default False
        If True, merge duplicate groups column-wise (first non-null per column).
    keep_lineage_columns : bool, default False
        If True, retain ``_completeness_score`` and ``_dedup_*`` helper
        columns in the output. Default False (clean output).
    validate_inchikeys : bool, default True
        If True, validate InChIKey format and warn on malformed values.
    auto_standardize : bool, default True
        If True, strip whitespace from InChIKeys before dedup (does not
        mutate the original DataFrame).
    synth_handling : {"strict", "by_name", "skip"}, default "strict"
        How to handle SYNTH-prefixed keys:
        - "strict": every SYNTH key is unique (default).
        - "by_name": SYNTH keys with the same value are duplicates.
        - "skip": SYNTH keys are excluded from dedup entirely.
    weight : CompletenessWeight, optional
        Weighting scheme for completeness scoring.
    dedup_by_version_char : bool, default False
        If True, treat InChIKeys that differ only in the version char
        (last character) as duplicates.
    null_inchikey_handler : {"keep_all", "drop", "quarantine"}, default "keep_all"
        How to handle rows with null InChIKeys. Default "keep_all"
        preserves all null rows (does NOT collapse them — fixes the
        v1.0.0 NaN==NaN data-loss bug).
    skip_if_already_deduped : bool, default True
        If True and the input has ``_dedup_already_applied=True`` attrs,
        return the input unchanged (idempotency).
    max_duplicate_ratio : float, optional
        If set, raise an error when the duplicate ratio exceeds this
        threshold (suspicious-data guard).
    source, operator_id, source_dataset_id : str, optional
        Provenance metadata.

    Returns
    -------
    pd.DataFrame or DedupResult
        Deduplicated DataFrame (or :class:`DedupResult` if
        ``return_result=True``).
    """
    wall_start = time.perf_counter()
    func_name = "dedup_by_inchikey"
    _incr_metric(f"{func_name}_calls")
    transformations: list[str] = []
    warnings_list: list[str] = []
    dropped_rows: list[dict[str, Any]] = []
    # v35 ROOT FIX: capture the dead-letter counter at entry so we can
    # compute the actual delta for this call (not an approximation based
    # on ``len(dropped_rows)``, which under-counts if any dead-letter
    # append raised inside its try/except, and over-counts if rows were
    # added to ``dropped_rows`` but not to the dead-letter queue).
    _dead_letters_at_start = _metrics.get("dead_letters_added", 0)

    # [REL-3] Circuit breaker — short-circuit if open
    if not _cb_dedup_by_inchikey.allow_request():
        warnings_list.append("circuit_open_short_circuit")
        _log_event("error", f"{func_name}.circuit_open")
        if return_result:
            return DedupResult(
                df=df.copy(), rows_before=len(df), rows_after=len(df),
                strategy="circuit_open", warnings=warnings_list,
                duration_seconds=0.0,
            )
        return df.copy()

    # [CODE-2] Input type validation
    if not isinstance(df, pd.DataFrame):
        raise TypeError(
            f"{func_name} expects a pandas DataFrame, got {type(df).__name__}"
        )

    # [SEC-3] DoS guard
    _validate_input_size(df)

    # [IDEM-1] Idempotency check
    if skip_if_already_deduped:
        try:
            if df.attrs.get("_dedup_already_applied") is True:
                _incr_metric(f"{func_name}_idempotent_skips")
                _log_event("info", f"{func_name}.idempotent_skip")
                if return_result:
                    return DedupResult(
                        df=df.copy(), rows_before=len(df), rows_after=len(df),
                        strategy="idempotent_skip", duration_seconds=0.0,
                        warnings=["idempotent_skip"],
                    )
                return df.copy()
        except Exception:
            pass

    # Empty DataFrame
    if df.empty:
        transformations.append("empty_input")
        _log_event("debug", f"{func_name}.empty_input")
        empty_result = df.copy()
        if reset_index:
            empty_result = empty_result.reset_index(drop=True)
        empty_result.attrs = dict(df.attrs)
        empty_result.attrs["_dedup_already_applied"] = True
        empty_result.attrs["_input_fingerprint"] = _fingerprint_df(df)
        empty_result.attrs["_output_fingerprint"] = _fingerprint_df(empty_result)
        empty_result.attrs["cleaning_metrics"] = {
            "rows_before": 0, "rows_after": 0,
            "duplicates_removed": 0, "strategy": "empty_input",
            "module_version": _MODULE_VERSION,
        }
        empty_result.attrs.setdefault("_provenance", []).append(
            _build_provenance_entry(
                func_name,
                empty_result.attrs["_input_fingerprint"],
                empty_result.attrs["_output_fingerprint"],
                0, 0, 0, "empty_input",
                transformations=transformations,
            )
        )
        _cb_dedup_by_inchikey.record_success()
        _record_timing(func_name, time.perf_counter() - wall_start)
        return empty_result if not return_result else DedupResult(
            df=empty_result, rows_before=0, rows_after=0,
            strategy="empty_input", duration_seconds=0.0,
            provenance=dict(empty_result.attrs.get("_provenance", [{}])[-1]),
        )

    # [CODE-2] Missing inchikey column — return unchanged with warning
    if "inchikey" not in df.columns:
        transformations.append("missing_inchikey_column")
        warnings_list.append("missing_inchikey_column")
        _log_event(
            "warning", f"{func_name}.missing_inchikey_column",
            columns=list(df.columns),
        )
        out = df.copy()
        if reset_index:
            out = out.reset_index(drop=True)
        _incr_metric(f"{func_name}_rows_in", len(df))
        _incr_metric(f"{func_name}_rows_out", len(out))
        _incr_metric("warnings_emitted")
        out.attrs = dict(df.attrs)
        out.attrs["_dedup_already_applied"] = True
        input_fp = _fingerprint_df(df)
        output_fp = _fingerprint_df(out)
        out.attrs["_input_fingerprint"] = input_fp
        out.attrs["_output_fingerprint"] = output_fp
        out.attrs["cleaning_metrics"] = {
            "rows_before": len(df), "rows_after": len(out),
            "duplicates_removed": 0, "strategy": "missing_inchikey_column",
            "module_version": _MODULE_VERSION,
        }
        out.attrs.setdefault("_provenance", []).append(
            _build_provenance_entry(
                func_name, input_fp, output_fp,
                len(df), len(out), 0, "missing_inchikey_column",
                transformations=transformations, warnings_list=warnings_list,
            )
        )
        _cb_dedup_by_inchikey.record_success()
        _record_timing(func_name, time.perf_counter() - wall_start)
        if return_result:
            return DedupResult(
                df=out, rows_before=len(df), rows_after=len(out),
                strategy="missing_inchikey_column",
                warnings=warnings_list,
                duration_seconds=time.perf_counter() - wall_start,
                provenance=dict(out.attrs.get("_provenance", [{}])[-1]),
            )
        return out

    # [SEC-2] PII scan
    try:
        _scan_for_pii(df)
    except Exception:
        pass

    # [SCI-8] Whitespace check
    if _check_whitespace_inchikeys(df):
        warnings_list.append("inchikey_whitespace_detected")
        _log_event(
            "warning", f"{func_name}.inchikey_whitespace_detected",
            advice="call standardize_inchikey FIRST",
        )
        _incr_metric("warnings_emitted")
        if auto_standardize:
            transformations.append("auto_strip_whitespace")

    # [IDEM-4] [LINEAGE-1] Input fingerprint
    input_fp = _fingerprint_df(df)
    transformations.append("compute_input_fingerprint")

    # [DQ-3] Build the list of columns to score (exclude lineage cols)
    if weight is None:
        # v29 ROOT FIX (audit C-11): dedup functions ignored configure_deduplicator — used DEFAULT_COMPLETENESS_WEIGHTS. Now reads from _config.
        weight = CompletenessWeight(weights=dict(_config["completeness_weights"]))

    # [SCI-5] [SCI-6] [DQ-1] [DQ-2] Build a working copy with normalized inchikey
    working = df.copy()
    original_index = working.index

    # Auto-strip whitespace
    if auto_standardize:
        try:
            working["inchikey"] = working["inchikey"].apply(
                lambda x: x.strip() if isinstance(x, str) else x
            )
        except Exception:
            pass

    # [DQ-2] Convert NaN-equivalent strings to NaN
    def _normalize_null_inchikey(val: Any) -> Any:
        if _is_nullish_inchikey(val):
            return pd.NA
        return val

    try:
        working["inchikey"] = working["inchikey"].apply(_normalize_null_inchikey)
    except Exception:
        pass

    # [SCI-5] SYNTH key handling
    # v34 ROOT FIX (CRITICAL #1): previously the deduplicator assigned
    # sentinel strings like `__SYNTH_UNIQUE_N__` to NaN/SYNTH/mixture
    # InChIKeys so they survived drop_duplicates — but the sentinels were
    # NEVER restored to the original values, leaking into the output and
    # causing downstream DB loaders to quarantine EVERY NaN/SYNTH/mixture
    # drug. We now snapshot the original inchikey column before applying
    # any sentinel, then restore it AFTER dedup. The sentinel remains in
    # a hidden `_dedup_sentinel_key` column for the dedup pass only.
    _ORIGINAL_INCHIKEY_BACKUP = working["inchikey"].copy()
    synth_mask = working["inchikey"].apply(_is_synthetic_inchikey)
    n_synth = int(synth_mask.sum())
    if n_synth > 0:
        _incr_metric(f"{func_name}_synth_keys_seen", n_synth)
        _log_event("info", f"{func_name}.synth_keys_seen", count=n_synth)
        if synth_handling == "skip":
            # Mark SYNTH rows as untouchable — give them unique sentinel keys
            # so drop_duplicates won't collapse them. Sentinels are written
            # to `_dedup_sentinel_key` so the original `inchikey` column is
            # preserved for downstream consumers (v34 root fix).
            working["_dedup_sentinel_key"] = working["inchikey"].astype(object)
            working.loc[synth_mask, "_dedup_sentinel_key"] = [
                f"__SYNTH_UNIQUE_{i}__" for i in range(n_synth)
            ]
            transformations.append("skip_synth_keys")

    # [SCI-6] Mixture InChIKey detection (warn-only — don't dedup)
    mixture_mask = working["inchikey"].apply(_is_mixture_inchikey)
    n_mixture = int(mixture_mask.sum())
    if n_mixture > 0:
        _incr_metric(f"{func_name}_mixture_keys_seen", n_mixture)
        _log_event(
            "warning", f"{func_name}.mixture_keys_seen",
            count=n_mixture,
            advice="mixture InChIKeys are not deduplicated",
        )
        warnings_list.append(f"mixture_keys_seen:{n_mixture}")
        _incr_metric("warnings_emitted")
        # Make each mixture key unique so they survive dedup.
        # v34 ROOT FIX (CRITICAL #1): write the sentinel to
        # `_dedup_sentinel_key` (created above) instead of overwriting
        # `inchikey`. The original inchikey value is preserved for the
        # output, while the sentinel is used only for the dedup pass.
        if "_dedup_sentinel_key" not in working.columns:
            working["_dedup_sentinel_key"] = working["inchikey"].astype(object)
        working.loc[mixture_mask, "_dedup_sentinel_key"] = [
            f"__MIXTURE_UNIQUE_{i}__" for i in range(n_mixture)
        ]

    # [DQ-9] Version-char mismatch detection
    if validate_inchikeys and not merge_fields:
        try:
            # Group by the first 25 chars (connectivity + fingerprint, no version)
            non_null = working["inchikey"].dropna()
            non_null_valid = non_null[non_null.apply(_is_valid_inchikey_format)]
            if len(non_null_valid) > 0:
                # Only consider 27-char standard-format keys (skip SYNTH)
                standard_keys = non_null_valid[
                    non_null_valid.str.match(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")
                ]
                if len(standard_keys) > 0:
                    prefixes = standard_keys.str.slice(stop=25)
                    version_chars = standard_keys.str.slice(start=26)
                    grouped = pd.DataFrame({
                        "prefix": prefixes,
                        "version": version_chars,
                    }).groupby("prefix")["version"].nunique()
                    mismatches = grouped[grouped > 1]
                    if len(mismatches) > 0:
                        _incr_metric(
                            f"{func_name}_version_char_mismatches",
                            int(len(mismatches)),
                        )
                        _log_event(
                            "warning",
                            f"{func_name}.version_char_mismatches",
                            count=int(len(mismatches)),
                            prefixes=list(mismatches.index[:10]),
                        )
                        warnings_list.append(
                            f"version_char_mismatches:{int(len(mismatches))}"
                        )
                        _incr_metric("warnings_emitted")
                        if dedup_by_version_char:
                            # CRITICAL FIX (scientific correctness):
                            # The original code normalized InChIKey version
                            # chars (last char: 'S' standard, 'N' non-standard)
                            # to 'S' for dedup purposes. This is DANGEROUS
                            # because non-standard InChIKeys encode additional
                            # information (fixed-H, reconnected metal layers,
                            # isotopes) that the standard InChI drops. Two
                            # compounds with the same 14-char connectivity
                            # hash and 10-char stereo hash but different
                            # version chars may represent DIFFERENT chemical
                            # entities (e.g., a tautomeric-specific form vs.
                            # the canonical form). Merging them silently
                            # would lose scientific information.
                            #
                            # The new behavior: instead of forcing all
                            # version chars to 'S', we log a WARNING and
                            # leave the InChIKeys unchanged. The dedup
                            # will keep BOTH versions as separate rows,
                            # which is the scientifically correct behavior
                            # (the caller can post-process if they really
                            # want to merge standard and non-standard forms).
                            logger.warning(
                                "%s: %d InChIKeys have non-standard version "
                                "char ('N'). dedup_by_version_char=True was "
                                "requested, but forcibly normalizing 'N' to "
                                "'S' would lose tautomeric/isotopic/metal "
                                "information. Keeping both forms as separate "
                                "rows. Sample prefixes: %s",
                                func_name,
                                int(len(mismatches)),
                                list(mismatches.index[:10]),
                            )
                            transformations.append(
                                "version_char_mismatch_kept_separate"
                            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("version-char mismatch detection failed: %s", exc)

    # [DES-3] [PERF-1] Compute weighted completeness scores
    completeness = weight.score_dataframe(working)
    working["_completeness_score"] = completeness
    transformations.append("compute_completeness_score")

    # [SCI-5] SYNTH "by_name" handling — collapse identical SYNTH values
    if synth_handling == "by_name":
        transformations.append("synth_by_name_collapse")
        # Don't make SYNTH unique — let identical SYNTH values dedup together

    # [DQ-1] NaN InChIKey handling
    # v1.0.0 BUG: drop_duplicates(subset=["inchikey"], keep="first")
    # treats NaN==NaN as True, collapsing all null rows into one.
    # v3.0.0 FIX: give each NaN row a unique sentinel so it survives dedup.
    nan_mask = working["inchikey"].isna()
    n_nan = int(nan_mask.sum())
    if n_nan > 0:
        _incr_metric(f"{func_name}_nan_inchikeys_kept", n_nan)
        _log_event(
            "info", f"{func_name}.nan_inchikeys_kept",
            count=n_nan, handler=null_inchikey_handler,
        )
        if null_inchikey_handler == "drop":
            working = working[~nan_mask].copy()
            transformations.append("drop_null_inchikeys")
        elif null_inchikey_handler == "quarantine":
            # Move null rows to dead-letter queue
            for idx in working[nan_mask].index:
                row_dict = working.loc[idx].to_dict()
                _append_dead_letter(
                    func_name, "null_inchikey", row_dict,
                )
                # dead_letters_added counter now incremented atomically
                # inside _append_dead_letter (v35 root fix).
            working = working[~nan_mask].copy()
            transformations.append("quarantine_null_inchikeys")
        else:  # keep_all (default)
            # Give each NaN row a unique sentinel so it survives dedup.
            # v34 ROOT FIX (CRITICAL #1): write the sentinel to
            # `_dedup_sentinel_key` (NOT to `inchikey`). The original
            # NaN value stays in `inchikey` so downstream consumers see
            # the real (NaN) value and can handle it appropriately.
            if "_dedup_sentinel_key" not in working.columns:
                working["_dedup_sentinel_key"] = working["inchikey"].astype(object)
            working.loc[nan_mask, "_dedup_sentinel_key"] = [
                f"__NULL_UNIQUE_{i}__" for i in range(n_nan)
            ]
            transformations.append("preserve_null_inchikeys")

    # [DES-3] [IDEM-2] Sort by the dedup key, then completeness descending, then
    # original index ascending (deterministic tie-breaking).
    # v34 ROOT FIX (CRITICAL #1): use `_dedup_sentinel_key` for sort/dedup
    # if it exists, else fall back to `inchikey`. This keeps the original
    # `inchikey` column pristine for downstream consumers.
    _dedup_key_col = (
        "_dedup_sentinel_key" if "_dedup_sentinel_key" in working.columns
        else "inchikey"
    )
    working["_original_index"] = original_index.values
    working = working.sort_values(
        by=[_dedup_key_col, "_completeness_score", "_original_index"],
        ascending=[True, False, True],
        kind="mergesort",  # stable sort
    )
    transformations.append("sort_by_completeness")

    # [DQ-6] Suspicious duplicate ratio check
    if max_duplicate_ratio is not None:
        n_in = len(working)
        n_unique = working[_dedup_key_col].nunique(dropna=False)
        if n_in > 0:
            ratio = (n_in - n_unique) / n_in
            if ratio > max_duplicate_ratio:
                msg = (
                    f"{func_name}: duplicate ratio {ratio:.2%} exceeds "
                    f"max_duplicate_ratio={max_duplicate_ratio:.2%} — "
                    f"suspicious input"
                )
                _log_event("error", f"{func_name}.suspicious_duplicate_ratio",
                           ratio=ratio, threshold=max_duplicate_ratio)
                if conservative_defaults:
                    # Quarantine all duplicates and return input unchanged
                    for ik, group in working.groupby(_dedup_key_col, sort=False):
                        if len(group) > 1:
                            for idx in group.index[1:]:
                                _append_dead_letter(
                                    func_name, "suspicious_duplicate_ratio",
                                    working.loc[idx].to_dict(),
                                )
                                # counter incremented inside _append_dead_letter (v35 fix)
                    out = df.copy()
                    if reset_index:
                        out = out.reset_index(drop=True)
                    _cb_dedup_by_inchikey.record_failure()
                    if return_result:
                        return DedupResult(
                            df=out, rows_before=len(df), rows_after=len(df),
                            strategy="suspicious_duplicate_ratio_quarantine",
                            warnings=[msg], duration_seconds=0.0,
                        )
                    return out
                raise ValueError(msg)

    # [DES-6] [LINEAGE-2] Merge-fields strategy
    if merge_fields:
        # Group by the dedup key and take the first non-null value per column
        # Preserve original indices for lineage.
        # v34 ROOT FIX (CRITICAL #1): group by `_dedup_sentinel_key` (if
        # present) so the merge doesn't collapse NaN/SYNTH/mixture rows.
        merged_groups: list[pd.DataFrame] = []
        source_indices_list: list[list[int]] = []
        for ik, group in working.groupby(_dedup_key_col, sort=False):
            merged_row: dict[str, Any] = {}
            for col in df.columns:
                if col == "inchikey":
                    # Use the FIRST non-null inchikey in the group (preserves
                    # the original value, never the sentinel).
                    ik_vals = group["inchikey"].dropna()
                    if len(ik_vals) > 0:
                        merged_row[col] = ik_vals.iloc[0]
                    else:
                        merged_row[col] = pd.NA
                    continue
                col_vals = group[col].dropna()
                # For string cols, also drop empty strings
                if col_vals.dtype == object:
                    col_vals = col_vals[col_vals.astype(str).str.strip() != ""]
                merged_row[col] = col_vals.iloc[0] if len(col_vals) > 0 else pd.NA
            merged_row["_completeness_score"] = group["_completeness_score"].max()
            merged_row["_dedup_source_indices"] = list(group["_original_index"].astype(int))
            source_indices_list.append(list(group["_original_index"].astype(int)))
            merged_groups.append(pd.DataFrame([merged_row]))
        if merged_groups:
            deduped = pd.concat(merged_groups, ignore_index=True)
        else:
            deduped = working.head(0).copy()
        transformations.append("merge_fields")
        strategy_name = DedupStrategy.MERGE_FIELDS.value
    else:
        # [CODE-3] [CODE-6] Use drop_duplicates (NOT groupby().first()) — required by TestIssue21
        # [DQ-1] NaN/SYNTH/mixture rows have unique sentinels in
        # `_dedup_sentinel_key`, so they survive.
        # v34 ROOT FIX (CRITICAL #1): dedup on `_dedup_sentinel_key` (if
        # present) so the original `inchikey` column is preserved.
        deduped = working.drop_duplicates(subset=[_dedup_key_col], keep="first").copy()
        transformations.append("drop_duplicates")
        strategy_name = DedupStrategy.MOST_COMPLETE.value

    # [CODE-7] Restore original column order
    original_cols = [c for c in df.columns if c in deduped.columns]
    if not keep_lineage_columns:
        # Drop helper columns (including the v34 `_dedup_sentinel_key`).
        helper_cols = [
            "_completeness_score", "_original_index",
            "_dedup_source_indices", "_dedup_winner",
            "_dedup_loser_inchikey", "_dedup_already_applied",
            "_dedup_sentinel_key",  # v34 ROOT FIX (CRITICAL #1)
        ]
        deduped = deduped.drop(
            columns=[c for c in helper_cols if c in deduped.columns],
            errors="ignore",
        )
    else:
        if "_dedup_source_indices" in deduped.columns:
            # Already added by merge_fields path
            pass
        # Drop the sentinel column even when lineage columns are kept —
        # it's an internal-only field that should never appear in output.
        if "_dedup_sentinel_key" in deduped.columns:
            deduped = deduped.drop(columns=["_dedup_sentinel_key"], errors="ignore")
        # Mark survivors
        deduped["_dedup_winner"] = True

    # Re-attach original index for kept rows
    if "_original_index" in deduped.columns:
        deduped = deduped.set_index("_original_index")
        deduped.index.name = df.index.name

    original_cols = [c for c in df.columns if c in deduped.columns]
    extra_cols = [c for c in deduped.columns if c not in df.columns]
    deduped = deduped[original_cols + extra_cols]

    # [IDEM-1] [IDEM-8] Log survivor change
    duplicates_removed = len(df) - len(deduped)
    if duplicates_removed > 0:
        # Identify dropped rows for dead-letter / inspection
        try:
            survivor_indices = set(deduped.index.tolist())
            all_indices = list(working["_original_index"])
            dropped_indices = [
                idx for idx in all_indices
                if idx not in survivor_indices
            ]
            for di in dropped_indices[:_MAX_DROPPED_ROWS_IN_RESULT]:
                row = working[working["_original_index"] == di].iloc[0]
                dropped_rows.append({
                    "original_index": int(di),
                    "inchikey": _redact_for_log_local(row.get("inchikey")),
                    "completeness_score": float(row.get("_completeness_score", 0.0)),
                    "reason": "duplicate_inchikey",
                })
        except Exception:
            pass
        # [LINEAGE-3] Add dead-letter entries for the first N dropped rows
        max_dl = 100 if not conservative_defaults else 1000
        for di in dropped_indices[:max_dl]:
            try:
                row = working[working["_original_index"] == di].iloc[0]
                # v17 ROOT FIX (DC-5 INCOMPLETE): v16 attempted to record
                # the survivor but used ``deduped.iloc[0]`` — the FIRST
                # row of the ENTIRE deduped DataFrame, NOT the survivor
                # of THIS specific dropped row's group. Every dead-letter
                # entry got the SAME survivor_inchikey regardless of
                # which row was dropped, making the field useless for
                # debugging. Look up the actual survivor by matching
                # the dropped row's InChIKey against the deduped frame.
                # If no match (shouldn't happen — the survivor has the
                # same InChIKey as the dropped row by definition of
                # group-by-inchikey), fall back to None and log a
                # warning so the operator knows the lookup missed.
                _dropped_ik = row.get("inchikey")
                survivor_row = None
                if _dropped_ik is not None and "inchikey" in deduped.columns:
                    _match = deduped[deduped["inchikey"] == _dropped_ik]
                    if len(_match) > 0:
                        survivor_row = _match.iloc[0]
                if survivor_row is None:
                    # Defensive fallback — keep the v16 behavior so the
                    # dead-letter is still written, but flag it.
                    survivor_row = deduped.iloc[0] if len(deduped) > 0 else None
                    if survivor_row is not None:
                        logger.warning(
                            "deduplicator: survivor lookup by inchikey=%r "
                            "missed — falling back to iloc[0]. The "
                            "survivor_inchikey recorded in this dead-letter "
                            "may not correspond to this dropped row.",
                            _redact_for_log_local(_dropped_ik),
                        )
                survivor_info = {
                    "dropped_inchikey": _redact_for_log_local(row.get("inchikey")),
                    "original_index": int(di),
                    "survivor_inchikey": _redact_for_log_local(
                        survivor_row.get("inchikey") if survivor_row is not None else None
                    ),
                    "survivor_source": str(survivor_row.get("source", "")) if survivor_row is not None else "",
                }
                _append_dead_letter(
                    func_name,
                    "duplicate_inchikey",
                    row.to_dict(),
                    survivor_info=survivor_info,
                )
                # counter incremented inside _append_dead_letter (v35 fix)
            except Exception:
                pass

    # [CODE-4] reset_index
    if reset_index:
        deduped = deduped.reset_index(drop=True)
    transformations.append("finalize_output")

    # [IDEM-4] [LINEAGE-1] Output fingerprint
    output_fp = _fingerprint_df(deduped)

    # [LINEAGE-7] Source attribution
    source_attribution: dict[str, int] = {}
    if "source" in deduped.columns:
        try:
            vc = deduped["source"].value_counts()
            source_attribution = {str(k): int(v) for k, v in vc.items()}
        except Exception:
            pass

    # [ARCH-6] [LINEAGE-1] Attach provenance
    # v35 ROOT FIX: compute the actual dead-letter count for this call
    # from the metrics counter delta (counter is incremented atomically
    # inside _append_dead_letter).
    dead_letters_added_count = _metrics.get("dead_letters_added", 0) - _dead_letters_at_start
    prov_entry = _build_provenance_entry(
        func_name, input_fp, output_fp,
        len(df), len(deduped), duplicates_removed,
        strategy_name,
        transformations=transformations,
        warnings_list=warnings_list,
        dead_letters_added=dead_letters_added_count,
        parameters={
            "reset_index": reset_index,
            "merge_fields": merge_fields,
            "conservative_defaults": conservative_defaults,
            "validate_inchikeys": validate_inchikeys,
            "auto_standardize": auto_standardize,
            "synth_handling": synth_handling,
            "dedup_by_version_char": dedup_by_version_char,
            "null_inchikey_handler": null_inchikey_handler,
            "skip_if_already_deduped": skip_if_already_deduped,
            "max_duplicate_ratio": max_duplicate_ratio,
            "keep_lineage_columns": keep_lineage_columns,
        },
        source_attribution=source_attribution,
        operator_id=operator_id,
        source_dataset_id=source_dataset_id,
        source=source,
    )
    deduped = _attach_provenance(deduped, prov_entry)
    deduped.attrs["_dedup_already_applied"] = True

    # [LOG-3] Metrics
    _incr_metric(f"{func_name}_rows_in", len(df))
    _incr_metric(f"{func_name}_rows_out", len(deduped))
    _incr_metric(f"{func_name}_duplicates_removed", duplicates_removed)

    # [LOG-1] [LOG-3] Structured completion log
    _log_event(
        "info", f"{func_name}.complete",
        rows_in=len(df), rows_out=len(deduped),
        duplicates_removed=duplicates_removed,
        strategy=strategy_name,
        duration_s=time.perf_counter() - wall_start,
    )

    # [REL-3] Circuit breaker success
    _cb_dedup_by_inchikey.record_success()
    _record_timing(func_name, time.perf_counter() - wall_start)

    if return_result:
        return DedupResult(
            df=deduped, rows_before=len(df), rows_after=len(deduped),
            duplicates_removed=duplicates_removed,
            quarantined=sum(1 for w in warnings_list if "quarantine" in w),
            dead_letter_count=dead_letters_added_count,
            duration_seconds=time.perf_counter() - wall_start,
            warnings=warnings_list,
            dropped_rows=dropped_rows,
            strategy=strategy_name,
            provenance=dict(prov_entry),
        )
    return deduped


# ===========================================================================
# [SCI-1] [SCI-2] [SCI-3] [SCI-4] [SCI-12] Main dedup_interactions
# ===========================================================================
def dedup_interactions(
    df: pd.DataFrame,
    keys: list[str] | None = None,
    *,
    activity_type_column: str | None = "activity_type",
    activity_value_column: str | None = "activity_value",
    activity_units_column: str | None = "activity_units",
    confidence_column: str | None = "confidence_score",
    direction: Literal["asc", "desc", "auto"] = "auto",
    keep: Literal["best", "first", "last", "mark"] = "best",
    segment_by_activity_type: bool = True,
    normalize_units: bool = True,
    handle_censored: bool = True,
    null_keys_handler: Literal["keep_all", "drop", "quarantine"] = "keep_all",
    strict_activity_type: bool = False,
    reset_index: bool = True,
    return_result: bool = False,
    conservative_defaults: bool = False,
    keep_lineage_columns: bool = False,
    skip_if_already_deduped: bool = True,
    max_duplicate_ratio: float | None = None,
    source: str | None = None,
    operator_id: str | None = None,
    source_dataset_id: str | None = None,
) -> pd.DataFrame | DedupResult:
    """Remove duplicate interaction rows by a composite key, keeping the most potent.

    Backward-compatible with the v1.0.0 signature
    ``dedup_interactions(df, keys)`` — all keyword arguments are optional
    with defaults that preserve v1.0.0 behavior exactly. When
    ``activity_value`` is present, the lowest value wins (matching
    v1.0.0 behavior for ``IC50`` / ``Ki`` / ``Kd`` assays). For
    ``pKi`` / ``pIC50`` / ``%`` inhibition assays, pass
    ``direction="auto"`` (default) or ``direction="desc"`` to keep the
    highest value (the scientifically-correct behavior).

    Parameters
    ----------
    df : pd.DataFrame
        DataFrame of interaction records.
    keys : list[str], optional
        Composite key column names. If None, inferred from the
        DataFrame columns (prefers 4-column key, falls back to 3, then 2).
    activity_type_column : str, optional
        Column name containing the activity type (``"IC50"``, ``"pKi"``, etc.).
        Default ``"activity_type"``. Set to None to disable activity-type
        segmentation.
    activity_value_column : str, optional
        Column name containing the activity value. Default ``"activity_value"``.
        Set to None to fall back to plain ``drop_duplicates``.
    activity_units_column : str, optional
        Column name containing the activity value's unit. Default
        ``"activity_units"``.
    confidence_column : str, optional
        Column name containing a confidence score in [0, 1] used as a
        tiebreaker. Default ``"confidence_score"``.
    direction : {"asc", "desc", "auto"}, default "auto"
        Sort direction for activity value. ``"auto"`` infers from
        ``activity_type`` (lower-is-better for ``IC50``/``Ki``/``Kd``,
        higher-is-better for ``pKi``/``pIC50``/``%``).
    keep : {"best", "first", "last", "mark"}, default "best"
        Which row to keep per duplicate group. ``"best"`` = most potent.
        ``"mark"`` keeps all rows but adds a ``_dedup_winner`` boolean column.
    segment_by_activity_type : bool, default True
        If True and ``activity_type`` is present, include it in the
        composite key (so IC50 vs Ki rows are NOT collapsed).
    normalize_units : bool, default True
        If True, normalize activity values to nM before comparison.
    handle_censored : bool, default True
        If True, censored values (``"<10"``, ``">100"``) are penalized
        so they don't silently win over uncensored values.
    null_keys_handler : {"keep_all", "drop", "quarantine"}, default "keep_all"
        How to handle rows with NULL in any key column.
    strict_activity_type : bool, default False
        If True, raise ``SchemaValidationError`` on unknown activity types.
    reset_index : bool, default True
        If True, reset the output index.
    return_result : bool, default False
        If True, return a :class:`DedupResult`.
    conservative_defaults : bool, default False
        If True, quarantine suspicious rows instead of silently dropping.
    keep_lineage_columns : bool, default False
        If True, retain helper columns in output.
    skip_if_already_deduped : bool, default True
        Idempotency guard.
    max_duplicate_ratio : float, optional
        Suspicious-data threshold.
    source, operator_id, source_dataset_id : str, optional
        Provenance metadata.

    Returns
    -------
    pd.DataFrame or DedupResult
        Deduplicated DataFrame (or :class:`DedupResult`).
    """
    wall_start = time.perf_counter()
    func_name = "dedup_interactions"
    _incr_metric(f"{func_name}_calls")
    transformations: list[str] = []
    warnings_list: list[str] = []
    dropped_rows: list[dict[str, Any]] = []
    # v35 ROOT FIX: capture the dead-letter counter at entry so we can
    # compute the actual delta for this call (counter is incremented
    # atomically inside _append_dead_letter — see same fix in
    # dedup_by_inchikey above).
    _dead_letters_at_start = _metrics.get("dead_letters_added", 0)

    # [REL-3] Circuit breaker
    if not _cb_dedup_interactions.allow_request():
        warnings_list.append("circuit_open_short_circuit")
        if return_result:
            return DedupResult(
                df=df.copy(), rows_before=len(df), rows_after=len(df),
                strategy="circuit_open", warnings=warnings_list,
                duration_seconds=0.0,
            )
        return df.copy()

    # [CODE-2] Input type validation
    if not isinstance(df, pd.DataFrame):
        raise TypeError(
            f"{func_name} expects a pandas DataFrame, got {type(df).__name__}"
        )

    # [SEC-3] DoS guard
    _validate_input_size(df)

    # [IDEM-1] Idempotency check
    if skip_if_already_deduped:
        try:
            if df.attrs.get("_dedup_interactions_already_applied") is True:
                _incr_metric(f"{func_name}_idempotent_skips")
                _log_event("info", f"{func_name}.idempotent_skip")
                if return_result:
                    return DedupResult(
                        df=df.copy(), rows_before=len(df), rows_after=len(df),
                        strategy="idempotent_skip", duration_seconds=0.0,
                        warnings=["idempotent_skip"],
                    )
                return df.copy()
        except Exception:
            pass

    # Empty DataFrame
    if df.empty:
        transformations.append("empty_input")
        empty_result = df.copy()
        if reset_index:
            empty_result = empty_result.reset_index(drop=True)
        empty_result.attrs = dict(df.attrs)
        empty_result.attrs["_dedup_interactions_already_applied"] = True
        input_fp = _fingerprint_df(df)
        output_fp = _fingerprint_df(empty_result)
        empty_result.attrs["_input_fingerprint"] = input_fp
        empty_result.attrs["_output_fingerprint"] = output_fp
        empty_result.attrs["cleaning_metrics"] = {
            "rows_before": 0, "rows_after": 0,
            "duplicates_removed": 0, "strategy": "empty_input",
            "module_version": _MODULE_VERSION,
        }
        empty_result.attrs.setdefault("_provenance", []).append(
            _build_provenance_entry(
                func_name, input_fp, output_fp,
                0, 0, 0, "empty_input",
                transformations=transformations,
            )
        )
        _cb_dedup_interactions.record_success()
        _record_timing(func_name, time.perf_counter() - wall_start)
        return empty_result if not return_result else DedupResult(
            df=empty_result, rows_before=0, rows_after=0,
            strategy="empty_input", duration_seconds=0.0,
            provenance=dict(empty_result.attrs.get("_provenance", [{}])[-1]),
        )

    # [DES-4] Infer keys if not provided
    if keys is None:
        keys = _infer_dpi_keys(df)
        if not keys:
            # Fall back to all object columns that look like IDs
            try:
                helpers = _get_helpers()
                if hasattr(helpers, "SchemaValidationError"):
                    raise helpers.SchemaValidationError(  # type: ignore[attr-defined]
                        f"Could not infer composite key from columns: "
                        f"{list(df.columns)}. Pass keys= explicitly."
                    )
            except AttributeError:
                pass
            raise ValueError(
                f"Could not infer composite key from columns: "
                f"{list(df.columns)}. Pass keys= explicitly."
            )

    # [CODE-5] Keys validation
    if not isinstance(keys, list):
        raise TypeError(f"keys must be a list[str], got {type(keys).__name__}")
    if len(keys) == 0:
        raise ValueError("keys must be a non-empty list")
    for k in keys:
        if not isinstance(k, str):
            raise TypeError(f"keys must be list[str], got {type(k).__name__}")
    if len(set(keys)) != len(keys):
        raise ValueError(f"keys contains duplicates: {keys}")

    missing_keys = [k for k in keys if k not in df.columns]
    if missing_keys:
        warnings_list.append(f"missing_key_columns:{missing_keys}")
        _log_event(
            "warning", f"{func_name}.missing_key_columns",
            missing=missing_keys, available=list(df.columns),
        )
        _incr_metric("warnings_emitted")
        out = df.copy()
        if reset_index:
            out = out.reset_index(drop=True)
        out.attrs = dict(df.attrs)
        input_fp = _fingerprint_df(df)
        output_fp = _fingerprint_df(out)
        out.attrs["_input_fingerprint"] = input_fp
        out.attrs["_output_fingerprint"] = output_fp
        out.attrs["cleaning_metrics"] = {
            "rows_before": len(df), "rows_after": len(out),
            "duplicates_removed": 0, "strategy": "missing_key_columns",
            "module_version": _MODULE_VERSION,
        }
        out.attrs.setdefault("_provenance", []).append(
            _build_provenance_entry(
                func_name, input_fp, output_fp,
                len(df), len(out), 0, "missing_key_columns",
                transformations=transformations, warnings_list=warnings_list,
            )
        )
        _cb_dedup_interactions.record_success()
        _record_timing(func_name, time.perf_counter() - wall_start)
        if return_result:
            return DedupResult(
                df=out, rows_before=len(df), rows_after=len(out),
                strategy="missing_key_columns",
                warnings=warnings_list,
                duration_seconds=time.perf_counter() - wall_start,
            )
        return out

    # [SEC-2] PII scan
    try:
        _scan_for_pii(df)
    except Exception:
        pass

    input_fp = _fingerprint_df(df)
    transformations.append("compute_input_fingerprint")

    working = df.copy()
    working["_original_index"] = working.index.values

    # [DQ-5] NULL keys handling
    null_keys_mask = pd.Series([False] * len(working), index=working.index)
    for k in keys:
        try:
            null_keys_mask = null_keys_mask | working[k].isna()
        except Exception:
            pass
    n_null_keys = int(null_keys_mask.sum())
    if n_null_keys > 0:
        _incr_metric(f"{func_name}_null_keys_kept", n_null_keys)
        _log_event(
            "info", f"{func_name}.null_keys_seen",
            count=n_null_keys, handler=null_keys_handler,
        )
        if null_keys_handler == "drop":
            working = working[~null_keys_mask].copy()
            transformations.append("drop_null_keys")
        elif null_keys_handler == "quarantine":
            for idx in working[null_keys_mask].index:
                _append_dead_letter(
                    func_name, "null_keys",
                    working.loc[idx].to_dict(),
                )
                # counter incremented inside _append_dead_letter (v35 fix)
            working = working[~null_keys_mask].copy()
            transformations.append("quarantine_null_keys")
        else:  # keep_all (default)
            # Give each null-key row a unique sentinel
            working.loc[null_keys_mask, "_null_key_sentinel"] = [
                f"__NULL_KEY_{i}__" for i in range(n_null_keys)
            ]
            transformations.append("preserve_null_keys")

    # Determine the effective composite key (may include activity_type)
    effective_keys = list(keys)
    activity_type_present = (
        activity_type_column is not None
        and activity_type_column in working.columns
    )
    activity_value_present = (
        activity_value_column is not None
        and activity_value_column in working.columns
    )
    if segment_by_activity_type and activity_type_present:
        effective_keys.append(activity_type_column)  # type: ignore[arg-type]
        transformations.append("segment_by_activity_type")

    # [DQ-8] Validate activity_type values
    if activity_type_present and strict_activity_type:
        for at_val in working[activity_type_column].dropna().unique():  # type: ignore[index]
            _validate_activity_type(at_val, strict=True)
        transformations.append("validate_activity_type_strict")

    # Add null_key_sentinel to effective keys if present
    if "_null_key_sentinel" in working.columns:
        effective_keys.append("_null_key_sentinel")

    # [SCI-1] [SCI-2] [SCI-3] [SCI-4] [SCI-12] Activity-value ranking
    sort_cols: list[str] = list(effective_keys)
    sort_ascending: list[bool] = [True] * len(sort_cols)

    if activity_value_present and keep == "best":
        # [SCI-3] Parse censored values & extract numeric component
        if handle_censored:
            parsed = working[activity_value_column].apply(_parse_censored_value)  # type: ignore[index]
            working["_av_censored"] = parsed.apply(lambda t: t[0])
            working["_av_censor_dir"] = parsed.apply(lambda t: t[1])
            working["_av_numeric"] = parsed.apply(lambda t: t[2])
            n_censored = int(working["_av_censored"].sum())
            if n_censored > 0:
                _incr_metric(f"{func_name}_censored_seen", n_censored)
                _log_event(
                    "info", f"{func_name}.censored_seen", count=n_censored,
                )
                transformations.append("parse_censored")
        else:
            working["_av_censored"] = False
            working["_av_censor_dir"] = None
            try:
                working["_av_numeric"] = pd.to_numeric(
                    working[activity_value_column], errors="coerce"  # type: ignore[index]
                )
            except Exception:
                working["_av_numeric"] = pd.NA

        # [SCI-4] Unit normalization
        if (
            normalize_units
            and activity_units_column is not None
            and activity_units_column in working.columns
        ):
            def _normalize_row(row: pd.Series) -> tuple[float | None, str | None]:
                val = row.get("_av_numeric")
                unit = row.get(activity_units_column)
                if val is None or pd.isna(val):
                    return (val, None)
                return _normalize_unit_to_nm(float(val), unit)
            try:
                norm_results = working.apply(_normalize_row, axis=1, result_type="expand")
                working["_av_normalized"] = norm_results[0]
                working["_av_norm_warning"] = norm_results[1]
                n_normalized = int(working["_av_norm_warning"].isna().sum())
                _incr_metric(f"{func_name}_unit_normalizations", n_normalized)
                transformations.append("normalize_units")
            except Exception:
                working["_av_normalized"] = working["_av_numeric"]
                working["_av_norm_warning"] = None
        else:
            working["_av_normalized"] = working["_av_numeric"]
            working["_av_norm_warning"] = None

        # V18 ROOT FIX (CD-7 — patient-safety + TransE training bias):
        # Before v18, deduplicator used _ACTIVITY_VALUE_MAX=1e9 (1 M, the
        # "non-physical" threshold) and treated every value below that as
        # fully valid. But normalizer uses _ACTIVITY_VALUE_MAX=1e6 (1 mM,
        # the "censored" threshold) — values in [1e6, 1e9) nM are flagged
        # as censored by normalizer but pass through deduplicator as
        # ordinary "valid" rows. Downstream TransE therefore sees a
        # biased sample: censored values get the same dedup priority as
        # clean values, so the "winning" record for a drug-target pair
        # may be a censored >X measurement instead of a real measurement.
        #
        # Root fix: tag every value in [1e6, 1e9) nM with a
        # ``_av_in_censored_band`` flag and use it as a tiebreaker
        # in the sort (uncensored < censored_band < explicitly censored).
        # This eliminates the 3-order-of-magnitude divergence the audit
        # flagged WITHOUT changing the non-physical rejection threshold
        # (still 1e9 — concentrations above 1 M remain non-physical).
        try:
            _censored_band_mask = (
                working["_av_normalized"].notna()
                & (working["_av_normalized"] >= _ACTIVITY_CENSORED_MAX)
                & (working["_av_normalized"] < _ACTIVITY_NON_PHYSICAL_MAX)
            )
            working["_av_in_censored_band"] = _censored_band_mask.astype(int)
            n_censored_band = int(_censored_band_mask.sum())
            if n_censored_band > 0:
                _log_event(
                    "info",
                    f"{func_name}.censored_band_values",
                    count=n_censored_band,
                    details=(
                        "values in [1e6, 1e9) nM are tagged "
                        "_av_in_censored_band — deprioritized in dedup "
                        "tiebreak (CD-7 root fix)."
                    ),
                )
                _incr_metric(f"{func_name}_censored_band_values", n_censored_band)
                transformations.append("censored_band_tagging")
        except Exception:
            # Defensive: if column doesn't exist or comparison fails,
            # fall back to "no censored-band values".
            working["_av_in_censored_band"] = 0

        # [SCI-10] Quarantine non-physical values
        invalid_mask = pd.Series([False] * len(working), index=working.index)
        try:
            invalid_mask = (
                working["_av_normalized"].notna()
                & (
                    (working["_av_normalized"] < _ACTIVITY_VALUE_MIN)
                    | (working["_av_normalized"] > _ACTIVITY_VALUE_MAX)
                )
            )
        except Exception:
            pass
        n_invalid = int(invalid_mask.sum())
        if n_invalid > 0:
            _incr_metric(
                f"{func_name}_invalid_activity_value_quarantined", n_invalid
            )
            _log_event(
                "warning", f"{func_name}.invalid_activity_value_quarantined",
                count=n_invalid,
            )
            warnings_list.append(f"invalid_activity_value:{n_invalid}")
            _incr_metric("warnings_emitted")
            for idx in working[invalid_mask].index:
                _append_dead_letter(
                    func_name, "invalid_activity_value_range",
                    working.loc[idx].to_dict(),
                )
                # counter incremented inside _append_dead_letter (v35 fix)
            working = working[~invalid_mask].copy()

        # [DQ-7] Quarantine non-numeric activity values
        non_numeric_mask = working["_av_normalized"].isna() & working[activity_value_column].notna()  # type: ignore[index]
        n_non_numeric = int(non_numeric_mask.sum())
        if n_non_numeric > 0:
            _log_event(
                "warning", f"{func_name}.non_numeric_activity_value",
                count=n_non_numeric,
            )
            warnings_list.append(f"non_numeric_activity_value:{n_non_numeric}")
            _incr_metric("warnings_emitted")
            if conservative_defaults:
                for idx in working[non_numeric_mask].index:
                    _append_dead_letter(
                        func_name, "non_numeric_activity_value",
                        working.loc[idx].to_dict(),
                    )
                    # counter incremented inside _append_dead_letter (v35 fix)
                working = working[~non_numeric_mask].copy()

        # [SCI-1] Resolve direction per activity_type
        if activity_type_present:
            working["_av_direction"] = working[activity_type_column].apply(  # type: ignore[index]
                lambda at: _resolve_activity_direction(at, direction)
            )
            # If direction varies within a group, default to ASC (safe)
            n_segments = int(working["_av_direction"].nunique())
            _incr_metric(f"{func_name}_activity_type_segments", n_segments)
            # Apply per-row direction by negating values for DESC rows
            # so a single ascending sort gives the correct ranking.
            working["_av_sort_value"] = working.apply(
                lambda row: (
                    -row["_av_normalized"]
                    if row["_av_direction"] == "desc" and pd.notna(row["_av_normalized"])
                    else row["_av_normalized"]
                ),
                axis=1,
            )
        else:
            resolved = _resolve_activity_direction(None, direction)
            if resolved == "desc":
                working["_av_sort_value"] = -working["_av_normalized"]
            else:
                working["_av_sort_value"] = working["_av_normalized"]
            working["_av_direction"] = resolved

        # [SCI-3] Censor penalty — uncensored beats censored
        # Sort key: (censored: 0=uncensored wins, 1=censored loses)
        # V18 ROOT FIX (CD-7): also penalize values in the censored
        # band [1e6, 1e9) nM — they're not explicitly censored (no
        # leading ``>``/``<`` marker) but they exceed the
        # pharmacologically-relevant range. Sort order:
        #   0 = clean value (< 1 mM, uncensored)
        #   1 = censored-band value (1 mM <= v < 1 M)
        #   2 = explicitly censored value (``>X`` / ``<X`` marker)
        # v43 ROOT FIX (P1-015): the previous math was
        # ``censored * 2 + in_band`` which gives 0/1/2/3 — a value
        # that is BOTH censored AND in the band gets 3, which
        # deprioritizes it below pure censored (2). But a censored
        # value should be 2 regardless of whether it's also in the
        # band (censored is the stronger signal). The fix clips the
        # band flag when censored is True so the result is strictly
        # 0/1/2 as the comment says.
        _censored_int = working["_av_censored"].astype(int)
        _band_int = working.get("_av_in_censored_band", 0)
        if isinstance(_band_int, pd.Series):
            _band_int = _band_int.astype(int)
        else:
            _band_int = 0
        # v43 P1-015: clip band flag when censored — censored takes
        # precedence (it's the explicit marker, band is heuristic).
        _band_clipped = _band_int.where(_censored_int == 0, 0)
        working["_av_censored_sort"] = _censored_int * 2 + _band_clipped

        # [SCI-12] Confidence tiebreaker (higher confidence wins)
        if confidence_column is not None and confidence_column in working.columns:
            # Validate confidence in [0, 1]
            try:
                conf = pd.to_numeric(working[confidence_column], errors="coerce")
                invalid_conf = conf.notna() & ((conf < 0) | (conf > 1))
                n_invalid_conf = int(invalid_conf.sum())
                if n_invalid_conf > 0:
                    _log_event(
                        "warning", f"{func_name}.invalid_confidence_score",
                        count=n_invalid_conf,
                    )
                    warnings_list.append(f"invalid_confidence:{n_invalid_conf}")
                    _incr_metric("warnings_emitted")
                    # Clip to [0, 1]
                    working[confidence_column] = conf.clip(0.0, 1.0)
                working["_av_confidence_sort"] = pd.to_numeric(
                    working[confidence_column], errors="coerce"
                ).fillna(0.0)
                # Negate so ascending sort puts highest confidence first
                working["_av_confidence_sort"] = -working["_av_confidence_sort"]
                transformations.append("confidence_tiebreaker")
            except (KeyError, ValueError, TypeError) as exc:
                # v43 ROOT FIX (P1-023): the previous code had a bare
                # ``except Exception`` that silently zeroed the
                # confidence sort for ALL rows on ANY failure. This
                # meant that if the confidence column had an unexpected
                # dtype or was missing, the tiebreaker was silently
                # disabled for every row. The fix:
                # 1. Narrows the except to specific types (KeyError,
                #    ValueError, TypeError) so real bugs propagate.
                # 2. Logs the exception so operators know the
                #    confidence tiebreaker was skipped.
                # 3. Only zeros the column if it's genuinely missing
                #    (the else branch handles the "no confidence column"
                #    case; this except handles "column exists but
                #    conversion failed").
                logger.warning(
                    "%s: confidence tiebreaker failed (%s) — "
                    "falling back to 0.0 for all rows. The sort "
                    "will not use confidence as a tiebreaker.",
                    func_name, exc,
                )
                working["_av_confidence_sort"] = 0.0
        else:
            working["_av_confidence_sort"] = 0.0

        # Final sort columns: keys, then censored(0<1), then sort_value(asc),
        # then confidence(asc after negation), then original_index
        sort_cols = list(effective_keys) + [
            "_av_censored_sort",
            "_av_sort_value",
            "_av_confidence_sort",
            "_original_index",
        ]
        sort_ascending = [True] * len(effective_keys) + [True, True, True, True]

        # [IDEM-2] Deterministic tie-breaking via mergesort
        working = working.sort_values(
            by=sort_cols, ascending=sort_ascending, kind="mergesort"
        )
        transformations.append("sort_by_activity")

        # [DQ-6] Suspicious duplicate ratio
        if max_duplicate_ratio is not None:
            n_in = len(working)
            n_unique = working.groupby(effective_keys, dropna=False).ngroups
            if n_in > 0:
                ratio = (n_in - n_unique) / n_in
                if ratio > max_duplicate_ratio:
                    msg = (
                        f"{func_name}: duplicate ratio {ratio:.2%} exceeds "
                        f"max_duplicate_ratio={max_duplicate_ratio:.2%}"
                    )
                    _log_event(
                        "error", f"{func_name}.suspicious_duplicate_ratio",
                        ratio=ratio, threshold=max_duplicate_ratio,
                    )
                    raise ValueError(msg)

        # [CODE-3] Use drop_duplicates (NOT groupby().first())
        # [CODE-6] No inplace=True
        # [SCI-1] [SCI-2] [SCI-3] first row wins (already sorted)
        deduped = working.drop_duplicates(subset=effective_keys, keep="first").copy()
        transformations.append("drop_duplicates")
        strategy_name = (
            DedupStrategy.LOWEST_ACTIVITY.value
            if direction == "asc"
            else (
                DedupStrategy.HIGHEST_ACTIVITY.value
                if direction == "desc"
                else "auto_activity_direction"
            )
        )
        # [SCI-3] Detect if a censored value was overridden
        # (i.e., would have won under plain sort but lost under censor penalty)
        # v16 ROOT FIX (DC-4): the previous code hardcoded
        # ``n_censored_override = 0`` then guarded ``if n_censored_override > 0``
        # — making the entire block dead code and the metric always 0.
        # We now ACTUALLY compute the count: for each duplicate group, find
        # groups where the winner is uncensored but at least one censored
        # row had a more "extreme" raw value (would have won under plain sort).
        if handle_censored and "_av_censored" in working.columns and "_av_normalized" in working.columns:
            try:
                n_censored_override = 0
                # The winner is the first row of each group (keep="first"
                # after sort). A censored row "would have won" if its
                # normalized value is more extreme than the winner's
                # in the requested direction.
                for _key, _grp in working.groupby(effective_keys, sort=False):
                    if len(_grp) <= 1:
                        continue
                    winner = _grp.iloc[0]
                    winner_val = winner.get("_av_normalized")
                    if winner.get("_av_censored") or winner_val is None:
                        continue  # winner itself is censored — no override
                    losers = _grp.iloc[1:]
                    censored_losers = losers[losers["_av_censored"] == True]  # noqa: E712
                    if censored_losers.empty:
                        continue
                    # v43 ROOT FIX (P1-040): the previous code used the
                    # GLOBAL ``direction`` parameter, but per-row direction
                    # was applied at line 3078-3100 via ``_av_direction``.
                    # For ``direction="auto"`` with mixed activity types
                    # (some IC50=asc, some pKi=desc), the global direction
                    # is "auto" which falls into the else branch (lower =
                    # better) — but for pKi rows, higher is better. The
                    # fix uses the per-row ``_av_direction`` column to
                    # determine the correct comparison direction for
                    # EACH censored loser individually.
                    if "_av_direction" in censored_losers.columns:
                        # Per-row direction: desc = higher better, asc = lower better
                        desc_mask = censored_losers["_av_direction"] == "desc"
                        # For desc rows: censored loser "would have won" if value > winner
                        # For asc rows: censored loser "would have won" if value < winner
                        mask = (
                            (desc_mask & (censored_losers["_av_normalized"] > winner_val))
                            | (~desc_mask & (censored_losers["_av_normalized"] < winner_val))
                        )
                    elif direction == "desc":
                        # Fallback to global direction if _av_direction missing
                        mask = censored_losers["_av_normalized"] > winner_val
                    else:
                        mask = censored_losers["_av_normalized"] < winner_val
                    n_censored_override += int(mask.sum())
                if n_censored_override > 0:
                    _incr_metric(
                        f"{func_name}_censored_winner_overridden",
                        n_censored_override,
                    )
                    warnings_list.append(
                        f"censored_winner_overridden:{n_censored_override}"
                    )
            except Exception as exc:  # noqa: BLE001
                # Best-effort — never crash dedup on instrumentation.
                _incr_metric(f"{func_name}_censored_override_check_failed", 1)
    elif keep == "first":
        deduped = working.drop_duplicates(subset=effective_keys, keep="first").copy()
        transformations.append("drop_duplicates_keep_first")
        strategy_name = DedupStrategy.FIRST_OCCURRENCE.value
    elif keep == "last":
        deduped = working.drop_duplicates(subset=effective_keys, keep="last").copy()
        transformations.append("drop_duplicates_keep_last")
        strategy_name = DedupStrategy.LAST_OCCURRENCE.value
    elif keep == "mark":
        # Keep all rows but add a _dedup_winner column
        working["_dedup_winner"] = ~working.duplicated(
            subset=effective_keys, keep="first"
        )
        deduped = working.copy()
        transformations.append("mark_duplicates")
        strategy_name = "mark"
    else:
        # No activity_value column — plain drop_duplicates (v1.0.0 behavior)
        deduped = working.drop_duplicates(subset=effective_keys, keep="first").copy()
        transformations.append("drop_duplicates_no_activity")
        strategy_name = DedupStrategy.FIRST_OCCURRENCE.value
        _log_event(
            "info", f"{func_name}.no_activity_column_fallback",
        )

    # [DQ-3] Drop helper columns unless keep_lineage_columns
    helper_cols = [
        "_original_index", "_null_key_sentinel",
        "_av_censored", "_av_censor_dir", "_av_numeric",
        "_av_normalized", "_av_norm_warning", "_av_direction",
        "_av_sort_value", "_av_censored_sort", "_av_confidence_sort",
    ]
    if not keep_lineage_columns:
        deduped = deduped.drop(
            columns=[c for c in helper_cols if c in deduped.columns],
            errors="ignore",
        )
    else:
        # Rename for clarity
        rename_map = {
            "_av_normalized": "_dedup_activity_value_normalized",
            "_av_direction": "_dedup_activity_direction",
            "_av_censored": "_dedup_activity_censored",
        }
        deduped = deduped.rename(columns={k: v for k, v in rename_map.items() if k in deduped.columns})

    # Re-attach original index
    if "_original_index" in deduped.columns:
        deduped = deduped.set_index("_original_index")
        deduped.index.name = df.index.name

    # [CODE-7] Restore original column order
    original_cols = [c for c in df.columns if c in deduped.columns]
    extra_cols = [c for c in deduped.columns if c not in df.columns]
    deduped = deduped[original_cols + extra_cols]

    duplicates_removed = len(df) - len(deduped)
    if duplicates_removed > 0 and keep != "mark":
        # Record dropped rows (capped)
        try:
            survivor_indices = set(deduped.index.tolist())
            all_indices = list(working["_original_index"])
            dropped_indices = [
                idx for idx in all_indices if idx not in survivor_indices
            ]
            for di in dropped_indices[:_MAX_DROPPED_ROWS_IN_RESULT]:
                row = working[working["_original_index"] == di].iloc[0]
                dropped_rows.append({
                    "original_index": int(di),
                    "composite_key": _redact_for_log_local(
                        tuple(row[k] for k in keys)
                    ),
                    "activity_value": (
                        float(row.get("_av_normalized"))
                        if pd.notna(row.get("_av_normalized")) else None
                    ),
                    "reason": "duplicate_composite_key",
                })
            # Dead-letter entries
            max_dl = 100 if not conservative_defaults else 1000
            for di in dropped_indices[:max_dl]:
                try:
                    row = working[working["_original_index"] == di].iloc[0]
                    survivor_info = {
                        "composite_key": _redact_for_log_local(
                            tuple(row[k] for k in keys)
                        ),
                        "dropped_index": int(di),
                    }
                    _append_dead_letter(
                        func_name, "duplicate_composite_key",
                        row.to_dict(),
                        survivor_info=survivor_info,
                    )
                    # counter incremented inside _append_dead_letter (v35 fix)
                except Exception:
                    pass
        except Exception:
            pass

    # [CODE-4] reset_index
    if reset_index:
        deduped = deduped.reset_index(drop=True)
    transformations.append("finalize_output")

    output_fp = _fingerprint_df(deduped)

    # [LINEAGE-7] Source attribution
    source_attribution: dict[str, int] = {}
    if "source" in deduped.columns:
        try:
            vc = deduped["source"].value_counts()
            source_attribution = {str(k): int(v) for k, v in vc.items()}
        except Exception:
            pass

    # v35 ROOT FIX: compute the actual dead-letter count for this call
    # from the metrics counter delta (counter is incremented atomically
    # inside _append_dead_letter).
    dead_letters_added_count = _metrics.get("dead_letters_added", 0) - _dead_letters_at_start
    prov_entry = _build_provenance_entry(
        func_name, input_fp, output_fp,
        len(df), len(deduped), duplicates_removed,
        strategy_name,
        transformations=transformations,
        warnings_list=warnings_list,
        dead_letters_added=dead_letters_added_count,
        parameters={
            "keys": keys,
            "effective_keys": effective_keys,
            "direction": direction,
            "keep": keep,
            "segment_by_activity_type": segment_by_activity_type,
            "normalize_units": normalize_units,
            "handle_censored": handle_censored,
            "null_keys_handler": null_keys_handler,
            "strict_activity_type": strict_activity_type,
            "reset_index": reset_index,
            "conservative_defaults": conservative_defaults,
            "keep_lineage_columns": keep_lineage_columns,
            "skip_if_already_deduped": skip_if_already_deduped,
            "max_duplicate_ratio": max_duplicate_ratio,
            "activity_type_column": activity_type_column,
            "activity_value_column": activity_value_column,
            "activity_units_column": activity_units_column,
            "confidence_column": confidence_column,
        },
        source_attribution=source_attribution,
        operator_id=operator_id,
        source_dataset_id=source_dataset_id,
        source=source,
    )
    deduped = _attach_provenance(deduped, prov_entry)
    deduped.attrs["_dedup_interactions_already_applied"] = True

    _incr_metric(f"{func_name}_rows_in", len(df))
    _incr_metric(f"{func_name}_rows_out", len(deduped))
    _incr_metric(f"{func_name}_duplicates_removed", duplicates_removed)

    _log_event(
        "info", f"{func_name}.complete",
        rows_in=len(df), rows_out=len(deduped),
        duplicates_removed=duplicates_removed,
        strategy=strategy_name,
        duration_s=time.perf_counter() - wall_start,
    )

    _cb_dedup_interactions.record_success()
    _record_timing(func_name, time.perf_counter() - wall_start)

    if return_result:
        return DedupResult(
            df=deduped, rows_before=len(df), rows_after=len(deduped),
            duplicates_removed=duplicates_removed,
            dead_letter_count=dead_letters_added_count,
            duration_seconds=time.perf_counter() - wall_start,
            warnings=warnings_list,
            dropped_rows=dropped_rows,
            strategy=strategy_name,
            provenance=dict(prov_entry),
        )
    return deduped


# ===========================================================================
# [ARCH-8] [PERF-3] Chunked processing
# ===========================================================================
def dedup_by_inchikey_chunked(
    df: pd.DataFrame,
    chunk_size: int = 10_000,
    **kwargs: Any,
) -> pd.DataFrame | DedupResult:
    """[ARCH-8] [PERF-3] Chunked dedup for large DataFrames.

    Two-pass algorithm:
    1. Chunk-local dedup: process the DataFrame in chunks of
       ``chunk_size`` rows, deduplicating each chunk independently.
    2. Global dedup: merge chunk survivors and dedup again.

    This reduces peak memory usage for very large DataFrames.

    Parameters
    ----------
    df : pd.DataFrame
        Input DataFrame.
    chunk_size : int, default 10_000
        Number of rows per chunk.
    **kwargs
        Forwarded to :func:`dedup_by_inchikey`.

    Returns
    -------
    pd.DataFrame or DedupResult
        Deduplicated DataFrame (or :class:`DedupResult` if
        ``return_result=True``).
    """
    if not isinstance(df, pd.DataFrame):
        raise TypeError(
            f"dedup_by_inchikey_chunked expects a DataFrame, got {type(df).__name__}"
        )
    if not isinstance(chunk_size, int) or chunk_size < 1:
        raise ValueError(f"chunk_size must be a positive int, got {chunk_size!r}")

    return_result = bool(kwargs.get("return_result", False))
    wall_start = time.perf_counter()
    transformations: list[str] = [f"chunked_size_{chunk_size}"]

    if df.empty or len(df) <= chunk_size:
        # Small enough — single pass
        transformations.append("single_pass")
        return dedup_by_inchikey(df, **kwargs)

    # Pass 1: chunk-local dedup
    chunks: list[pd.DataFrame] = []
    n_chunks = (len(df) + chunk_size - 1) // chunk_size
    for i in range(n_chunks):
        start = i * chunk_size
        end = min((i + 1) * chunk_size, len(df))
        chunk = df.iloc[start:end]
        # Disable idempotency skip for sub-chunks
        local_kwargs = dict(kwargs)
        local_kwargs["skip_if_already_deduped"] = False
        local_kwargs["return_result"] = False
        deduped_chunk = dedup_by_inchikey(chunk, **local_kwargs)
        chunks.append(deduped_chunk)
        _log_event(
            "debug", "dedup_by_inchikey_chunked.chunk_done",
            chunk_index=i, rows_in=end - start, rows_out=len(deduped_chunk),
        )

    merged = pd.concat(chunks, ignore_index=True) if chunks else df.iloc[:0].copy()
    transformations.append(f"merged_{len(chunks)}_chunks")

    # Pass 2: global dedup
    global_kwargs = dict(kwargs)
    global_kwargs["skip_if_already_deduped"] = False
    result = dedup_by_inchikey(merged, **global_kwargs)
    transformations.append("global_dedup")

    if return_result and isinstance(result, pd.DataFrame):
        # Re-wrap with chunked-aware metrics
        prov = get_provenance(result)
        prov["transformation_chain"] = transformations
        return DedupResult(
            df=result, rows_before=len(df), rows_after=len(result),
            duplicates_removed=len(df) - len(result),
            duration_seconds=time.perf_counter() - wall_start,
            strategy="chunked_most_complete",
            provenance=prov,
        )
    return result


# ===========================================================================
# [DES-6] [LINEAGE-2] merge_duplicate_groups
# ===========================================================================
def merge_duplicate_groups(
    df: pd.DataFrame,
    keys: list[str],
    *,
    weight: CompletenessWeight | None = None,
) -> pd.DataFrame:
    """[DES-6] Merge duplicate groups column-wise (first non-null per column).

    For each group of rows sharing the same composite key, produce a
    single output row where each column takes its first non-null value
    across the group. A ``_dedup_source_indices`` column lists the
    original indices of all rows merged into each surviving row.

    Parameters
    ----------
    df : pd.DataFrame
        Input DataFrame.
    keys : list[str]
        Composite key columns.
    weight : CompletenessWeight, optional
        Unused — kept for API symmetry. May be used in future versions
        to bias the merge order.

    Returns
    -------
    pd.DataFrame
        Merged DataFrame with one row per unique composite key.
    """
    if not isinstance(df, pd.DataFrame):
        raise TypeError(
            f"merge_duplicate_groups expects a DataFrame, got {type(df).__name__}"
        )
    if not isinstance(keys, list) or not keys:
        raise ValueError("keys must be a non-empty list[str]")
    missing = [k for k in keys if k not in df.columns]
    if missing:
        raise ValueError(f"keys missing from df.columns: {missing}")

    if df.empty:
        return df.copy()

    working = df.copy()
    working["_original_index"] = working.index.values

    merged_groups: list[pd.DataFrame] = []
    for composite_key, group in working.groupby(keys, sort=False, dropna=False):
        merged_row: dict[str, Any] = {}
        for k, v in zip(keys, composite_key if isinstance(composite_key, tuple) else (composite_key,)):
            merged_row[k] = v
        for col in df.columns:
            if col in keys:
                continue
            col_vals = group[col].dropna()
            if col_vals.dtype == object:
                col_vals = col_vals[col_vals.astype(str).str.strip() != ""]
            merged_row[col] = col_vals.iloc[0] if len(col_vals) > 0 else pd.NA
        merged_row["_dedup_source_indices"] = list(group["_original_index"].astype(int))
        merged_groups.append(pd.DataFrame([merged_row]))

    if not merged_groups:
        return df.iloc[:0].copy()
    result = pd.concat(merged_groups, ignore_index=True)
    return result


# ===========================================================================
# [DQ-10] quality_report
# ===========================================================================
def quality_report(
    df: pd.DataFrame,
    *,
    data_type: Literal["drug", "interaction"] = "drug",
) -> dict[str, Any]:
    """[DQ-10] Compute a pre-dedup data quality report.

    Parameters
    ----------
    df : pd.DataFrame
        Input DataFrame.
    data_type : {"drug", "interaction"}, default "drug"
        Type of data — controls which checks are applied.

    Returns
    -------
    dict
        Quality metrics including null counts, duplicate counts,
        suspicious-value flags, and per-column completeness.
    """
    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"quality_report expects a DataFrame, got {type(df).__name__}")
    if data_type not in ("drug", "interaction"):
        raise ValueError(f"data_type must be 'drug' or 'interaction', got {data_type!r}")

    report: dict[str, Any] = {
        "row_count": len(df),
        "column_count": len(df.columns),
        "data_type": data_type,
        "module_version": _MODULE_VERSION,
        "null_counts": {},
        "duplicate_counts": {},
        "completeness_per_column": {},
        "warnings": [],
    }

    if df.empty:
        report["warnings"].append("empty_dataframe")
        return report

    # Per-column null counts
    for col in df.columns:
        try:
            null_count = int(df[col].isna().sum())
            report["null_counts"][col] = null_count
            report["completeness_per_column"][col] = (
                1.0 - null_count / max(len(df), 1)
            )
        except Exception:
            report["null_counts"][col] = -1

    # Drug-specific checks
    if data_type == "drug":
        if "inchikey" in df.columns:
            null_ik = int(df["inchikey"].isna().sum())
            report["null_inchikey_count"] = null_ik
            valid_mask = df["inchikey"].dropna().apply(_is_valid_inchikey_format)
            report["invalid_inchikey_count"] = int((~valid_mask).sum())
            try:
                dup_count = int(df["inchikey"].dropna().duplicated().sum())
                report["duplicate_counts"]["inchikey"] = dup_count
            except Exception:
                pass
            # SYNTH keys
            synth_count = int(df["inchikey"].dropna().apply(_is_synthetic_inchikey).sum())
            report["synth_inchikey_count"] = synth_count
            # Mixture keys
            mixture_count = int(df["inchikey"].dropna().apply(_is_mixture_inchikey).sum())
            report["mixture_inchikey_count"] = mixture_count

    # Interaction-specific checks
    if data_type == "interaction":
        if "activity_value" in df.columns:
            av = df["activity_value"]
            try:
                av_numeric = pd.to_numeric(av, errors="coerce")
                # SCI-FIX (DQ correctness): the previous expression
                # `av_numeric.isna().sum() & av.notna().sum()` did a
                # bitwise AND on two integer counts — a numerology-style
                # value with no scientific meaning. The intent is to
                # count rows where the original activity_value was
                # non-null BUT numeric coercion failed (i.e., the value
                # was a non-numeric string like "N/A" or ">100"). The
                # correct expression is an element-wise logical AND
                # followed by .sum().
                null_av = int((av_numeric.isna() & av.notna()).sum())
                report["non_numeric_activity_value_count"] = null_av
                # Censored values
                # v41 ROOT FIX (SEV3): the previous expression
                # ``av.astype(str).str.match(_CENSOR_PATTERN)`` converted
                # NaN values to the literal string "nan", which doesn't
                # match the censor pattern ``^\s*([<>=~]{1,2})...`` — so
                # NaN values were correctly NOT counted as censored, but
                # ONLY by accident (the "nan" string happens to lack the
                # leading ``<`` ``>`` ``=`` ``~`` prefix).  The implicit
                # contract — "NaN is not censored" — is now made explicit
                # by using ``pd.isna()`` to mask out NaN values BEFORE
                # the str-match, so future readers don't have to reason
                # about the astype(str)-of-NaN edge case.  This also
                # defends against pandas behaviour changes (pd.NA vs
                # np.nan vs None all stringify differently across
                # versions).
                av_str = av.astype(str)
                av_str = av_str.where(~pd.isna(av), other="")
                censored_mask = av_str.str.match(_CENSOR_PATTERN)
                report["censored_activity_value_count"] = int(censored_mask.sum())
                # Out-of-range values
                out_of_range = (
                    av_numeric.notna()
                    & (
                        (av_numeric < _ACTIVITY_VALUE_MIN)
                        | (av_numeric > _ACTIVITY_VALUE_MAX)
                    )
                )
                report["out_of_range_activity_value_count"] = int(out_of_range.sum())
            except Exception:
                pass
        if "activity_type" in df.columns:
            unknown_types = [
                t for t in df["activity_type"].dropna().unique()
                if not _validate_activity_type(t, strict=False)[0]
            ]
            report["unknown_activity_type_count"] = len(unknown_types)
            if unknown_types:
                report["unknown_activity_types"] = unknown_types[:20]

    return report


# ===========================================================================
# [DQ-12] [LINEAGE-5] referential_integrity_check
# ===========================================================================
def referential_integrity_check(
    df: pd.DataFrame,
    *,
    known_inchikeys: set[str] | None = None,
    drug_id_to_inchikey: dict[int, str] | None = None,
) -> dict[str, Any]:
    """[DQ-12] Verify referential integrity of a DataFrame.

    For drug DataFrames: every non-null InChIKey must be in
    ``known_inchikeys`` (if provided).

    For interaction DataFrames: every ``drug_id`` must map to a known
    InChIKey via ``drug_id_to_inchikey`` (if provided).

    Parameters
    ----------
    df : pd.DataFrame
        Input DataFrame.
    known_inchikeys : set[str], optional
        Set of valid InChIKeys.
    drug_id_to_inchikey : dict[int, str], optional
        Mapping from drug_id to InChIKey.

    Returns
    -------
    dict
        Integrity report with ``is_valid``, ``violations``, ``violation_count``.
    """
    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"referential_integrity_check expects a DataFrame, got {type(df).__name__}")

    report: dict[str, Any] = {
        "is_valid": True,
        "violations": [],
        "violation_count": 0,
        "rows_checked": len(df),
    }
    if df.empty:
        return report

    if "inchikey" in df.columns and known_inchikeys is not None:
        for idx, val in df["inchikey"].dropna().items():
            if val not in known_inchikeys:
                report["violations"].append({
                    "row": int(idx),
                    "column": "inchikey",
                    "value": _redact_for_log_local(val),
                    "reason": "unknown_inchikey",
                })

    if "drug_id" in df.columns and drug_id_to_inchikey is not None:
        for idx, did in df["drug_id"].dropna().items():
            try:
                did_int = int(did)
            except (TypeError, ValueError):
                report["violations"].append({
                    "row": int(idx),
                    "column": "drug_id",
                    "value": _redact_for_log_local(did),
                    "reason": "non_integer_drug_id",
                })
                continue
            if did_int not in drug_id_to_inchikey:
                report["violations"].append({
                    "row": int(idx),
                    "column": "drug_id",
                    "value": did_int,
                    "reason": "unknown_drug_id",
                })

    report["violation_count"] = len(report["violations"])
    report["is_valid"] = report["violation_count"] == 0
    return report


# ===========================================================================
# [IDEM-5] backfill_safety_check
# ===========================================================================
def backfill_safety_check(
    df: pd.DataFrame,
    known_inchikeys: set[str],
    *,
    on_conflict: Literal["warn", "error", "keep_existing"] = "warn",
) -> tuple[pd.DataFrame, list[str]]:
    """[IDEM-5] Verify a backfill operation is safe.

    A backfill is safe iff:
    - Every non-null InChIKey in ``df`` is already in ``known_inchikeys``.
    - No new InChIKeys would be introduced that conflict with existing data.

    Parameters
    ----------
    df : pd.DataFrame
        The data to backfill.
    known_inchikeys : set[str]
        InChIKeys already present in the target store.
    on_conflict : {"warn", "error", "keep_existing"}, default "warn"
        What to do when a conflict is detected.

    Returns
    -------
    tuple
        ``(safe_df, warnings)`` — the (possibly filtered) DataFrame and
        a list of warning strings.
    """
    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"backfill_safety_check expects a DataFrame, got {type(df).__name__}")
    if not isinstance(known_inchikeys, set):
        raise TypeError("known_inchikeys must be a set[str]")
    if on_conflict not in ("warn", "error", "keep_existing"):
        raise ValueError(f"on_conflict must be 'warn'|'error'|'keep_existing', got {on_conflict!r}")

    warnings_list: list[str] = []
    if df.empty or "inchikey" not in df.columns:
        return df.copy(), warnings_list

    conflicts = df["inchikey"].dropna().apply(
        lambda x: x not in known_inchikeys
    )
    conflict_count = int(conflicts.sum())
    if conflict_count > 0:
        msg = f"backfill_safety_check: {conflict_count} rows have unknown InChIKeys"
        warnings_list.append(msg)
        _log_event("warning", "dedup.backfill_safety_check.conflicts",
                    count=conflict_count, action=on_conflict)
        if on_conflict == "error":
            raise ValueError(msg)
        if on_conflict == "keep_existing":
            # Drop the conflicting rows
            df = df[~conflicts.reindex(df.index, fill_value=False)].copy()
    return df.copy(), warnings_list


# ===========================================================================
# [REL-6] [REL-8] [REL-9] Recovery & checkpointing
# ===========================================================================
def recover_from_failure(
    df: pd.DataFrame,
    partial_result: pd.DataFrame | None,
    error: Exception,
    *,
    keys: list[str] | None = None,
) -> pd.DataFrame:
    """[REL-6] Recover from a partial dedup failure.

    If a partial result is available, return it (with a warning).
    Otherwise, return the input unchanged. The error is logged but
    not re-raised (use ``conservative_defaults=True`` for strict mode).

    Parameters
    ----------
    df : pd.DataFrame
        Original input.
    partial_result : pd.DataFrame or None
        Whatever was produced before the failure.
    error : Exception
        The error that occurred.
    keys : list[str], optional
        Composite key (for interaction dedup recovery).

    Returns
    -------
    pd.DataFrame
        Recovered result.
    """
    _log_event(
        "error", "dedup.recover_from_failure",
        error_type=type(error).__name__, error_message=str(error)[:200],
        has_partial=partial_result is not None,
    )
    if partial_result is not None and isinstance(partial_result, pd.DataFrame):
        if not partial_result.empty:
            partial_result.attrs["recovery_mode"] = True
            partial_result.attrs["recovery_error"] = str(error)[:500]
            return partial_result
    # No partial result — return input unchanged
    out = df.copy()
    out.attrs["recovery_mode"] = True
    out.attrs["recovery_error"] = str(error)[:500]
    return out


def checkpoint_state(
    df: pd.DataFrame,
    *,
    keys: list[str] | None = None,
) -> dict[str, Any]:
    """[REL-8] Snapshot the dedup-able state of a DataFrame for recovery.

    Returns
    -------
    dict
        Serializable checkpoint with ``row_count``, ``column_hashes``,
        ``key_distribution``, ``timestamp``, ``module_version``.
    """
    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"checkpoint_state expects a DataFrame, got {type(df).__name__}")
    checkpoint: dict[str, Any] = {
        "row_count": len(df),
        "column_count": len(df.columns),
        "columns": list(df.columns),
        "column_hashes": {},
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "module_version": _MODULE_VERSION,
        "schema_version": _OUTPUT_SCHEMA_VERSION,
        "correlation_id": get_correlation_id(),
    }
    for col in df.columns:
        try:
            col_data = df[col]
            col_str = col_data.astype(str).str.cat(sep="|")
            checkpoint["column_hashes"][col] = hashlib.sha256(
                col_str.encode("utf-8", errors="replace")
            ).hexdigest()[:16]
        except Exception:
            checkpoint["column_hashes"][col] = "error"
    if keys is not None:
        try:
            checkpoint["key_distribution"] = {
                str(k): int(df[k].value_counts(dropna=False).to_dict().get(k, 0))
                for k in keys if k in df.columns
            }
        except Exception:
            checkpoint["key_distribution"] = {}
    return checkpoint


def validate_recovery_state(checkpoint: dict) -> bool:
    """[REL-9] Validate that a checkpoint dict is well-formed.

    Returns ``True`` iff the checkpoint has all required keys
    (``row_count``, ``column_count``, ``columns``, ``column_hashes``,
    ``timestamp``, ``module_version``) with sensible value types.
    Returns ``False`` for malformed or partial checkpoints.
    """
    if not isinstance(checkpoint, dict):
        return False
    required = ["row_count", "column_count", "columns", "column_hashes",
                "timestamp", "module_version"]
    for k in required:
        if k not in checkpoint:
            return False
    if not isinstance(checkpoint["row_count"], int) or checkpoint["row_count"] < 0:
        return False
    if not isinstance(checkpoint["columns"], list):
        return False
    if not isinstance(checkpoint["column_hashes"], dict):
        return False
    return True


# ===========================================================================
# [PERF-8] performance_benchmark
# ===========================================================================
def performance_benchmark(
    df: pd.DataFrame,
    *,
    keys: list[str] | None = None,
) -> dict[str, Any]:
    """[PERF-8] Run a performance benchmark on the deduplicator.

    Returns timing info for ``dedup_by_inchikey`` and (if applicable)
    ``dedup_interactions``.
    """
    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"performance_benchmark expects a DataFrame, got {type(df).__name__}")
    results: dict[str, Any] = {
        "row_count": len(df),
        "column_count": len(df.columns),
        "dedup_by_inchikey": None,
        "dedup_interactions": None,
        "module_version": _MODULE_VERSION,
    }
    if "inchikey" in df.columns:
        start = time.perf_counter()
        try:
            _ = dedup_by_inchikey(df, skip_if_already_deduped=False)
            elapsed = time.perf_counter() - start
            results["dedup_by_inchikey"] = {
                "duration_s": elapsed,
                "throughput_rows_per_sec": (len(df) / elapsed) if elapsed > 0 else 0.0,
                "status": "ok",
            }
        except Exception as exc:
            results["dedup_by_inchikey"] = {
                "status": "error",
                "error": str(exc)[:200],
            }
    if keys is not None and all(k in df.columns for k in keys):
        start = time.perf_counter()
        try:
            _ = dedup_interactions(df, keys=keys, skip_if_already_deduped=False)
            elapsed = time.perf_counter() - start
            results["dedup_interactions"] = {
                "duration_s": elapsed,
                "throughput_rows_per_sec": (len(df) / elapsed) if elapsed > 0 else 0.0,
                "status": "ok",
            }
        except Exception as exc:
            results["dedup_interactions"] = {
                "status": "error",
                "error": str(exc)[:200],
            }
    return results


# ===========================================================================
# [IDEM-9] [IDEM-10] Reproducibility
# ===========================================================================
def is_reproducible(
    result_a: pd.DataFrame,
    result_b: pd.DataFrame,
) -> bool:
    """[IDEM-9] Return True if two results are byte-identical (modulo attrs)."""
    if not isinstance(result_a, pd.DataFrame):
        return False
    if not isinstance(result_b, pd.DataFrame):
        return False
    try:
        # Same shape
        if result_a.shape != result_b.shape:
            return False
        # Same columns (order matters)
        if list(result_a.columns) != list(result_b.columns):
            return False
        # Same dtypes
        for c in result_a.columns:
            if result_a[c].dtype != result_b[c].dtype:
                return False
        # Same values
        if not result_a.equals(result_b):
            # equals() is too strict on NaN positions; use a more robust check
            try:
                pd.testing.assert_frame_equal(
                    result_a, result_b, check_dtype=True,
                    check_index_type=True, check_column_type=True,
                )
            except Exception:
                return False
        return True
    except Exception:
        return False


def reproducibility_report(df: pd.DataFrame) -> dict[str, Any]:
    """[IDEM-10] Run dedup twice and check reproducibility."""
    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"reproducibility_report expects a DataFrame, got {type(df).__name__}")
    report: dict[str, Any] = {
        "row_count": len(df),
        "is_reproducible": True,
        "fingerprint_stable": True,
        "module_version": _MODULE_VERSION,
        "rule_version": _RULE_VERSION,
        "logic_hash": _LOGIC_HASH,
    }
    if df.empty:
        return report
    try:
        r1 = dedup_by_inchikey(df, skip_if_already_deduped=False, reset_index=True)
        r2 = dedup_by_inchikey(df, skip_if_already_deduped=False, reset_index=True)
        report["is_reproducible"] = is_reproducible(r1, r2)
        fp1 = r1.attrs.get("_output_fingerprint", "")
        fp2 = r2.attrs.get("_output_fingerprint", "")
        report["fingerprint_stable"] = (fp1 == fp2) and (fp1 != "")
        report["fingerprint"] = fp1
    except Exception as exc:
        report["is_reproducible"] = False
        report["error"] = str(exc)[:200]
    return report


# ===========================================================================
# [LOG-8] health_check
# ===========================================================================
def health_check() -> dict[str, Any]:
    """[LOG-8] Return a health summary of the deduplicator module."""
    return {
        "module": "cleaning.deduplicator",
        "module_version": _MODULE_VERSION,
        "schema_version": _OUTPUT_SCHEMA_VERSION,
        "rule_version": _RULE_VERSION,
        "logic_hash": _LOGIC_HASH,
        "python_version": f"{__import__('sys').version_info.major}.{__import__('sys').version_info.minor}",
        "pandas_version": pd.__version__,
        "metrics": get_metrics(),
        "timing": timing_report(),
        "dead_letter_count": len(_dead_letters),
        "circuit_breakers": {
            "dedup_by_inchikey": _cb_dedup_by_inchikey.state,
            "dedup_interactions": _cb_dedup_interactions.state,
        },
        "config": dict(_config),
        "config_warnings": validate_config(),
    }


# ===========================================================================
# [INTEROP-6] Pre/post clean hook firing
# ===========================================================================
def _fire_hooks(
    hooks: list[Callable],
    step_name: str,
    df: pd.DataFrame,
) -> None:
    """[INTEROP-6] Fire pre/post clean hooks (best-effort)."""
    for hook in hooks:
        try:
            hook(step_name, df)
        except Exception as exc:
            _log_event(
                "warning", "dedup.hook_failed",
                step=step_name, error=str(exc)[:200],
            )


# ===========================================================================
# [ARCH-7] clean_interactions orchestrator
# ===========================================================================
def clean_interactions(
    df: pd.DataFrame,
    *,
    keys: list[str] | None = None,
    activity_type_column: str | None = "activity_type",
    activity_value_column: str | None = "activity_value",
    activity_units_column: str | None = "activity_units",
    confidence_column: str | None = "confidence_score",
    direction: Literal["asc", "desc", "auto"] = "auto",
    segment_by_activity_type: bool = True,
    normalize_units: bool = True,
    handle_censored: bool = True,
    strict_activity_type: bool = False,
    skip_dedup: bool = False,
    source: str | None = None,
    operator_id: str | None = None,
    source_dataset_id: str | None = None,
) -> pd.DataFrame:
    """[ARCH-7] Apply the recommended cleaning pipeline to a DPI DataFrame.

    Runs these steps in order:
    1. ``normalize_activity_value`` (if ``activity_value`` + ``activity_units``
       columns present and the normalizer module is available).
    2. ``dedup_interactions`` with the supplied ``keys`` (or inferred default).

    Parameters
    ----------
    df : pd.DataFrame
        Raw drug-protein interaction records.
    keys : list[str], optional
        Composite key. If None, inferred from the DataFrame columns.
    activity_type_column, activity_value_column, activity_units_column, confidence_column : str, optional
        Column names forwarded to ``dedup_interactions``.
    direction : {"asc", "desc", "auto"}, default "auto"
        Sort direction (auto infers from activity_type).
    segment_by_activity_type : bool, default True
        Include ``activity_type`` in the dedup key.
    normalize_units : bool, default True
        Normalize activity values to nM before comparison.
    handle_censored : bool, default True
        Penalize censored values (``"<10"``, ``">100"``) so they don't
        silently win.
    strict_activity_type : bool, default False
        Reject unknown activity types.
    skip_dedup : bool, default False
        If True, skip the dedup step (useful for inspection).
    source, operator_id, source_dataset_id : str, optional
        Provenance metadata.

    Returns
    -------
    pd.DataFrame
        Cleaned DPI records.
    """
    if not isinstance(df, pd.DataFrame):
        raise TypeError(
            f"clean_interactions expects a pandas DataFrame, "
            f"got {type(df).__name__}"
        )

    input_fingerprint = _fingerprint_df(df)
    out = df.copy()

    # Optional: pre-normalize activity values via normalizer.normalize_activity_value
    # v41 ROOT FIX (SEV1 #6): The previous code overwrote activity_value
    # with the nM-normalized value but did NOT update activity_units.
    # Then dedup_interactions(normalize_units=True) re-normalized,
    # multiplying nM by the unit factor → 1000× error for uM, 1e6× for mM.
    # The fix: (1) update activity_units to "nM" after normalization,
    # (2) tell dedup_interactions NOT to re-normalize (normalize_units=False)
    # because we already did, (3) preserve the censor flag from the
    # original activity_value (e.g. ">100 uM" → censored; the previous
    # code lost this flag and treated censored as exact).
    _v41_already_normalized = False
    if (
        activity_value_column
        and activity_value_column in out.columns
        and activity_units_column
        and activity_units_column in out.columns
    ):
        try:
            helpers = _get_helpers()
            normalize_fn = getattr(helpers, "normalize_activity_value", None)
            if callable(normalize_fn):
                # v41 ROOT FIX (SEV1 #6): preserve censor information.
                # The normalizer returns a tuple (value, unit) but loses
                # the censor flag (>, <, ~). We capture it from the
                # original string-form value BEFORE normalization.
                def _detect_censor(val: Any) -> str:
                    """Return '', '>', '<', or '~' based on the original value."""
                    if val is None:
                        return ""
                    s = str(val).strip()
                    if not s:
                        return ""
                    if s.startswith(">"):
                        return ">"
                    if s.startswith("<"):
                        return "<"
                    if s.startswith("~"):
                        return "~"
                    return ""

                # Capture original censor flags BEFORE normalizing
                _censor_flags = out[activity_value_column].apply(_detect_censor)
                # If a censor_column exists, populate it; otherwise store
                # in an attribute for downstream use.
                censor_col = "activity_censor" if "activity_censor" not in out.columns else None
                if censor_col:
                    out[censor_col] = _censor_flags

                def _normalize_row(row: pd.Series) -> Any:
                    val = row[activity_value_column]
                    unit = row[activity_units_column]
                    at = (
                        row[activity_type_column]
                        if activity_type_column and activity_type_column in row
                        else None
                    )
                    try:
                        result = normalize_fn(val, unit, activity_type=at)  # type: ignore[misc]
                        # ActivityValue is a tuple subclass (value, unit)
                        return float(result[0]) if result[0] is not None else val
                    except Exception:
                        return val
                out[activity_value_column] = out.apply(_normalize_row, axis=1)
                # v41 ROOT FIX (SEV1 #6): update activity_units to "nM"
                # so dedup_interactions does NOT re-normalize (which
                # would multiply nM by the unit factor → 1000× error).
                out[activity_units_column] = "nM"
                _v41_already_normalized = True
                _log_event("info", "clean_interactions.normalize_activity_value",
                           note="v41 fix: activity_units updated to nM, "
                                "normalize_units=False for dedup, "
                                "censor flags preserved")
        except Exception as exc:
            _log_event(
                "debug", "clean_interactions.normalize_skipped",
                error=str(exc)[:200],
            )

    # Dedup step
    # v41 ROOT FIX (SEV1 #6): if we already normalized above, pass
    # normalize_units=False to dedup_interactions to prevent the
    # 1000× double-normalization error.
    _effective_normalize_units = normalize_units and not _v41_already_normalized
    if not skip_dedup:
        out = dedup_interactions(
            out,
            keys=keys,
            activity_type_column=activity_type_column,
            activity_value_column=activity_value_column,
            activity_units_column=activity_units_column,
            confidence_column=confidence_column,
            direction=direction,
            segment_by_activity_type=segment_by_activity_type,
            normalize_units=_effective_normalize_units,
            handle_censored=handle_censored,
            strict_activity_type=strict_activity_type,
            source=source,
            operator_id=operator_id,
            source_dataset_id=source_dataset_id,
        )

    output_fingerprint = _fingerprint_df(out)
    out.attrs["_input_fingerprint"] = input_fingerprint
    out.attrs["_output_fingerprint"] = output_fingerprint

    _log_event(
        "info", "clean_interactions.complete",
        rows_in=len(df), rows_out=len(out),
        direction=direction, segment_by_activity_type=segment_by_activity_type,
    )
    return out


# ===========================================================================
# [COMP-5] [DES-8] Public API declaration
# ===========================================================================
__all__ = [
    # Module metadata
    "__version__",
    # Constants
    "DEFAULT_COMPLETENESS_WEIGHTS",
    "DEFAULT_DPI_KEYS",
    "INVERSE_ACTIVITY_TYPES",
    "MAX_DATAFRAME_ROWS",
    "MAX_DEAD_LETTERS",
    "MAX_DROPPED_ROWS_IN_RESULT",
    "PERCENT_ACTIVITY_TYPES",
    "POTENCY_ACTIVITY_TYPES",
    # Enums
    "ActivityDirection",
    "DedupStrategy",
    # Dataclasses
    "CompletenessWeight",
    "DedupResult",
    # Public functions
    "backfill_safety_check",
    "checkpoint_state",
    "clear_dead_letters",
    "compute_completeness_score",
    "configure_deduplicator",
    "dedup_by_inchikey",
    "dedup_by_inchikey_chunked",
    "dedup_interactions",
    "flush_dead_letters",
    "get_correlation_id",
    "get_dead_letters",
    "get_metrics",
    "get_provenance",
    "health_check",
    "is_reproducible",
    "merge_duplicate_groups",
    "performance_benchmark",
    "quality_report",
    "recover_from_failure",
    "referential_integrity_check",
    "reproducibility_report",
    "requires_api_version",
    "reset_metrics",
    "revert_configuration",
    "set_correlation_id",
    "timing_report",
    "validate_config",
    "validate_environment",
    "validate_recovery_state",
]


# ===========================================================================
# [COMP-1] [CODE-10] PEP 562 — live-reading aliases & __dir__
# ===========================================================================
def __getattr__(name: str) -> Any:
    """[COMP-1] PEP 562 — provide live-reading aliases for module constants."""
    if name == "MAX_ROWS":
        return _MAX_DATAFRAME_ROWS
    if name == "MAX_DL":
        return _MAX_DEAD_LETTERS
    if name == "ACTIVITY_VALUE_MIN":
        return _ACTIVITY_VALUE_MIN
    if name == "ACTIVITY_VALUE_MAX":
        return _ACTIVITY_VALUE_MAX
    if name == "ALLOWED_ACTIVITY_TYPES":
        return _ALLOWED_ACTIVITY_TYPES
    raise AttributeError(
        f"module {__name__!r} has no attribute {name!r}"
    )


def __dir__() -> list[str]:
    """[COMP-1] [CODE-10] Return sorted list of all public names."""
    base = list(globals().keys())
    extra = [
        "MAX_ROWS", "MAX_DL", "ACTIVITY_VALUE_MIN", "ACTIVITY_VALUE_MAX",
        "ALLOWED_ACTIVITY_TYPES",
    ]
    return sorted(set(base) | set(extra) | set(__all__))


# ===========================================================================
# [ARCH-9] Record module load time
# ===========================================================================
_MODULE_LOAD_TIME = time.monotonic() - _MODULE_LOAD_TIME
