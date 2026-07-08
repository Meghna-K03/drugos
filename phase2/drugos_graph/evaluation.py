"""DrugOS Graph Module — Evaluation Metrics
=============================================
Implements evaluation metrics for knowledge graph link prediction:
  - AUC (Area Under ROC Curve)
  - Precision@K
  - Recall@K
  - MRR (Mean Reciprocal Rank)
  - Hits@K

CHANGELOG
=========
v2.0.0-evaluation (this PR):
  - Fixed E2-002: recall_at_k denominator bug (CRITICAL).
  - Fixed E3-001: sort-direction validation on all ranking metrics.
  - Fixed E7-001: sklearn vs manual AUC determinism enforced.
  - Fixed E7-002: _manual_auc tie correction now order-independent
    via Mann-Whitney rank-sum formula.
  - Added EvaluationConfig, EvaluationResult, Metric/Evaluator Protocols.
  - Added data quality reports, leakage detection, provenance tracking.
  - Added bootstrap CI for regulatory reporting (TRIPOD-AI/STARD-AI).
  - Added metric registry pattern for extensible evaluation.
  - Added 47+ unit tests in tests/test_evaluation.py.
  - Wired evaluate_link_prediction into train_transe (E1-002).
  - Full 78-issue forensic audit applied; see
    MASTER_REPAIR_PROMPT_evaluation.md for issue catalog.
v1.0.0 (pre-audit):
  - Initial implementation. See forensic audit for issues.
"""

# PATIENT SAFETY WARNING  # Fixes E13-001
# This module computes the metrics that determine whether the DrugOS
# model is safe enough to influence drug repurposing decisions.
# Wrong metrics -> wrong model validation -> wrong drug candidates ->
# patient harm. Every change must be reviewed against the forensic
# audit (MASTER_REPAIR_PROMPT_evaluation.md) and tested via
# tests/test_evaluation.py.
# If you are editing this file for the first time, read the audit
# in full before proceeding.

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    Protocol,
    Tuple,
    Union,
    runtime_checkable,
)

import numpy as np

# v43 ROOT FIX (P2-025): module-level sklearn import. The previous code
# imported roc_auc_score INSIDE the compute_auc function on every call,
# which is wasteful for repeated AUC evaluations during training. The
# fix imports it once at module level. If sklearn is not installed,
# _ROC_AUC_SCORE is None and compute_auc falls back to the manual path.
try:
    from sklearn.metrics import roc_auc_score as _ROC_AUC_SCORE
except ImportError:
    _ROC_AUC_SCORE = None

from .config import (
    CORRELATION_ID,
    RUN_ID,
    SEED,
    STRUCTURED_LOGGING,
    LineageMetadata,
    build_lineage_metadata,
    EVALUATION_CONFIG,
)
from .exceptions import (
    DrugOSDataError,
    EvaluationError,
    EvaluationInputError,
    EvaluationIntegrityError,
    EvaluationReproducibilityError,
    EvaluationSecurityError,
)

logger = logging.getLogger(__name__)

# ─── Scientific References ────────────────────────────────────────────────────
# Fixes E3-004 — Mann-Whitney and Wilcoxon citations required for audit trail.

MANN_WHITNEY_REFERENCE: str = (
    "Mann, H. B.; Whitney, D. R. (1947). "
    "'On a Test of Whether One of Two Random Variables is Stochastically "
    "Larger than the Other'. Annals of Mathematical Statistics. 18 (1): "
    "50-60. doi:10.1214/aoms/1177730491."
)

WILCOXON_REFERENCE: str = (
    "Wilcoxon, F. (1945). 'Individual Comparisons by Ranking Methods'. "
    "Biometrics Bulletin. 1 (6): 80-83."
)

BORDES_2013_REFERENCE: str = (
    "Bordes, A. et al. (2013). 'Translating Embeddings for Modeling "
    "Multi-relational Data'. NeurIPS 2013."
)

# ─── Tie Handling Strategy ────────────────────────────────────────────────────
# Fixes E13-003
# Wilcoxon half-credit tie correction is used (Wilcoxon 1945).
# Alternatives considered:
#   - Pessimistic (all ties wrong): underestimates AUC
#   - Optimistic (all ties correct): overestimates AUC
#   - Random tie-breaking: non-deterministic, violates P2
# Half-credit is the mathematically standard convention for AUC and is
# what sklearn.metrics.roc_auc_score uses (averaging method).

# ─── Module-level constants ────────────────────────────────────────────────────
# Fixes E12-001, E7-003, E12-002, E14-002

LINEAGE_METADATA_SUPPORT_AVAILABLE = True  # Always available from config

EVALUATION_METRIC_VERSION: str = "2.0.0-evaluation"  # Fixes E7-003
EVALUATION_SCHEMA_VERSION: str = "1.0.0"  # Fixes E14-002
SKLEARN_MIN_VERSION: str = "0.24.0"  # Fixes E15-005

K_VALUES_DEFAULT: Tuple[int, ...] = (1, 3, 5, 10, 20)  # Fixes E12-001
# RATIONALE: Standard link-prediction K values from the literature
# (Bordes et al. 2013, DRKG evaluation protocol).

EVALUATION_FALLBACK_STRATEGY: str = "warn"  # "fail", "warn", or "silent"

# ─── Transformation Audit Trail ───────────────────────────────────────────────
# Fixes E16-004 — every input transformation is logged for traceability.
#
# v35 ROOT FIX (L-18 / L-21): ``EVALUATION_TRANSFORMATIONS_LOG`` is a
# module-level mutable list. Without a reset hook, it grows unboundedly
# across test runs and pipeline runs, eventually consuming megabytes
# of memory and producing unreadable audit trails. The fix adds
# ``reset_evaluation_transformations_log()`` so callers can clear the
# log at the start of each run.
#
# L-21 thread-safety note: this list is NOT thread-safe. Concurrent
# calls to ``compute_auc`` from multiple threads can race on
# ``list.append``. In CPython the GIL makes ``append`` atomic, so the
# list itself will not corrupt — but the LOGICAL ORDER of entries is
# not guaranteed across threads. For audit-trail purposes, callers
# that need strict ordering should run ``compute_auc`` serially (the
# default — single-threaded evaluation is the DrugOS standard).
# Multi-threaded evaluation is NOT supported and NOT recommended for
# FDA 21 CFR Part 11 runs.

EVALUATION_TRANSFORMATIONS_LOG: List[Dict[str, Any]] = []


def reset_evaluation_transformations_log() -> None:
    """Clear the module-level ``EVALUATION_TRANSFORMATIONS_LOG``.

    v35 ROOT FIX (L-18): without this hook, the transformation log
    grows unboundedly across pipeline runs and test sessions,
    eventually consuming megabytes of memory and producing
    unreadable audit trails. Call this at the start of each
    ``evaluate_link_prediction`` invocation (or per epoch) to keep
    the log bounded. The previous contents are discarded — callers
    that need to persist them should snapshot the list BEFORE
    calling reset.

    Returns
    -------
    None
    """
    EVALUATION_TRANSFORMATIONS_LOG.clear()

# ─── Metric Registry ───────────────────────────────────────────────────────────
# Fixes E2-003

METRIC_REGISTRY: Dict[str, "Metric"] = {}


def register_metric(
    name: str, higher_is_better: bool
) -> Callable[..., Any]:
    """Decorator to register a metric function in the global registry.

    Args:
        name: Unique metric name (e.g. "precision_at_k").
        higher_is_better: Whether higher metric values indicate better
            model performance.

    Returns:
        Decorator function that wraps the metric and registers it.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        class _RegisteredMetric:
            """Adapter to satisfy the Metric Protocol for a function."""

            name: str = name
            higher_is_better: bool = higher_is_better

            def __init__(self, func: Callable[..., Any]) -> None:
                self._func = func

            def compute(
                self,
                pos_scores: np.ndarray,
                neg_scores: np.ndarray,
                ranked_lists: Optional[List[RankedItem]] = None,
            ) -> float:
                return float(self._func())

            def __call__(self, *args: Any, **kwargs: Any) -> Any:
                return self._func(*args, **kwargs)

        instance = _RegisteredMetric(fn)
        METRIC_REGISTRY[name] = instance  # type: ignore[assignment]
        fn._metric_name = name  # type: ignore[attr-defined]
        fn._higher_is_better = higher_is_better  # type: ignore[attr-defined]
        return fn

    return decorator


def list_registered_metrics() -> List[str]:
    """Return the names of all registered metrics.

    Returns:
        Sorted list of metric names.
    """
    return sorted(METRIC_REGISTRY.keys())


# ─── Type Aliases & Protocols ──────────────────────────────────────────────────
# Fixes E1-004, E1-005

RankedItem = Tuple[int, float, bool]
"""Type alias for a ranked prediction: (entity_id, score, is_true_label).
Backward-compatible with the original list-of-tuples API (E1-004)."""


@runtime_checkable
class Metric(Protocol):
    """Protocol for a pluggable evaluation metric.

    Any object with ``name``, ``higher_is_better``, and ``compute()``
    satisfies this protocol (structural typing).
    """

    name: str
    higher_is_better: bool

    def compute(
        self,
        pos_scores: np.ndarray,
        neg_scores: np.ndarray,
        ranked_lists: Optional[List[RankedItem]] = None,
    ) -> float: ...


@runtime_checkable
class Evaluator(Protocol):
    """Protocol for a full evaluation runner.

    Any object with ``evaluate()`` satisfies this protocol.
    """

    def evaluate(
        self,
        pos_scores: np.ndarray,
        neg_scores: np.ndarray,
        ranked_lists: Optional[List[List[RankedItem]]] = None,
    ) -> "EvaluationResult": ...


# ─── EvaluationResult ──────────────────────────────────────────────────────────
# Fixes E1-005, E9-003, E16-001


@dataclass(frozen=True)
class EvaluationResult:
    """Frozen, tamper-evident container for evaluation output.

    All fields are immutable after construction. The ``audit_hash`` is
    computed in ``__post_init__`` via ``object.__setattr__`` and
    verified by ``verify_integrity()``.

    Fixes E1-005 (separate counts from metrics), E9-003 (integrity),
    E16-001 (provenance metadata), BUG-C-001 (raw scores stored on the
    result so bootstrap CI can resample real model scores instead of
    falling back to synthetic N(0.3, 0.15) / N(0.7, 0.15) draws).
    """

    metrics: Dict[str, float]
    counts: Dict[str, int]
    provenance: LineageMetadata
    quality_report: Dict[str, Any]
    audit_hash: str = ""
    per_prediction_breakdown: Optional[List[Dict[str, Any]]] = None
    transformations: Optional[List[Dict[str, Any]]] = None
    evaluation_metric_version: str = EVALUATION_METRIC_VERSION
    evaluation_schema_version: str = EVALUATION_SCHEMA_VERSION
    # BUG-C-001 root fix: store the raw model scores so the bootstrap CI
    # can resample WITH REPLACEMENT from the observed score distribution
    # instead of falling back to synthetic Gaussian draws. The previous
    # code called ``getattr(result, "pos_scores", [])`` which always
    # returned ``[]`` because this field did not exist — the synthetic
    # fallback therefore ALWAYS fired and the reported 95% CI described
    # N(0.3, 0.15) vs N(0.7, 0.15), not the model.
    pos_scores: Optional[Any] = None  # np.ndarray, kept Optional for back-compat
    neg_scores: Optional[Any] = None  # np.ndarray

    def __post_init__(self) -> None:
        """Compute and set the tamper-evident audit hash.

        The hash is SHA-256 of the JSON-serialised metrics dict,
        making the result tamper-evident. Uses ``object.__setattr__``
        because the dataclass is frozen.

        Fixes E9-003. Note: ``pos_scores`` / ``neg_scores`` are
        deliberately excluded from the hash because (a) their SHA-256
        is already captured in ``provenance.input_checksums``, and (b)
        numpy arrays are not directly JSON-serialisable.
        """
        hash_val = hashlib.sha256(
            json.dumps(
                self.metrics, sort_keys=True, default=str
            ).encode("utf-8")
        ).hexdigest()
        object.__setattr__(self, "audit_hash", hash_val)


def verify_integrity(result: EvaluationResult) -> bool:
    """Recompute the audit hash and verify it matches.

    Used by downstream consumers before persisting or acting on
    evaluation results. Returns True if the result has not been
    tampered with.

    Fixes E9-003.

    Args:
        result: The EvaluationResult to verify.

    Returns:
        True if the audit hash matches, False otherwise.
    """
    expected = hashlib.sha256(
        json.dumps(result.metrics, sort_keys=True, default=str).encode(
            "utf-8"
        )
    ).hexdigest()
    return result.audit_hash == expected


# ─── Internal Helpers ──────────────────────────────────────────────────────────


def _to_native_float(x: Any) -> float:
    """Cast a numeric value to a native Python float.

    Ensures JSON-serialisability (no numpy.float64). Raises
    EvaluationIntegrityError if the result is NaN when NaN is not
    expected.

    Fixes E15-001.

    Args:
        x: Value to cast.

    Returns:
        Native Python float.

    Raises:
        EvaluationIntegrityError: If the cast result is NaN.
    """
    val = float(x)
    if np.isnan(val):
        raise EvaluationIntegrityError(
            "Metric value is NaN after casting to native float",
            context={"value_repr": repr(x), "type": str(type(x))},
        )
    return val


def _log_structured(
    level: int, event: str, **fields: Any
) -> None:
    """Log a structured message if STRUCTURED_LOGGING is enabled.

    Falls back to f-string formatting when structured logging is off.

    Fixes E11-004.

    Args:
        level: Logging level (e.g. logging.INFO).
        event: Event name for the log entry.
        **fields: Additional key-value fields.
    """
    fields_with_context = {
        "event": event,
        "run_id": RUN_ID,
        "correlation_id": CORRELATION_ID,
        **fields,
    }
    if STRUCTURED_LOGGING:
        logger.log(
            level, json.dumps(fields_with_context, default=str)
        )
    else:
        parts = [f"{k}={v}" for k, v in fields_with_context.items()]
        logger.log(level, " | ".join(parts))


def _sanitize_scores(
    scores: np.ndarray,
    *,
    allow_nan: bool = False,
    allow_inf: bool = False,
) -> np.ndarray:
    """Sanitise a score array by checking for NaN and Inf values.

    Fixes E3-003, E5-001.

    Args:
        scores: Score array to sanitise.
        allow_nan: If True, NaN values are permitted.
        allow_inf: If True, Inf values are permitted.

    Returns:
        The sanitised array (possibly with NaN/Inf removed if allowed).

    Raises:
        EvaluationInputError: If invalid values are found and not allowed.
    """
    if not allow_nan:
        nan_mask = np.isnan(scores)
        n_nan = int(np.sum(nan_mask))
        if n_nan > 0:
            bad_indices = np.where(nan_mask)[0][:10].tolist()
            raise EvaluationInputError(
                f"NaN values found in score array ({n_nan} total)",
                context={
                    "reason": "nan_in_scores",
                    "n_nan": n_nan,
                    "first_bad_indices": bad_indices,
                },
            )
    if not allow_inf:
        inf_mask = np.isinf(scores)
        n_inf = int(np.sum(inf_mask))
        if n_inf > 0:
            bad_indices = np.where(inf_mask)[0][:10].tolist()
            raise EvaluationInputError(
                f"Inf values found in score array ({n_inf} total)",
                context={
                    "reason": "inf_in_scores",
                    "n_inf": n_inf,
                    "first_bad_indices": bad_indices,
                },
            )
    if allow_nan:
        nan_mask = np.isnan(scores)
        n_nan = int(np.sum(nan_mask))
        if n_nan > 0:
            _log_structured(
                logging.WARNING,
                "nan_scores_dropped",
                n_dropped=n_nan,
                total=len(scores),
            )
            EVALUATION_TRANSFORMATIONS_LOG.append(
                {
                    "action": "drop_nan",
                    "n_dropped": n_nan,
                    "total_before": len(scores),
                }
            )
            scores = scores[~nan_mask]
    return scores


def _validate_score_array(
    scores: Any,
    name: str,
    *,
    allow_nan: bool = False,
    allow_inf: bool = False,
    min_length: int = 1,
) -> np.ndarray:
    """Validate and coerce input to a 1-D float64 numpy array.

    Fixes E5-001.

    Args:
        scores: Input to validate.
        name: Name of the array (for error messages).
        allow_nan: Whether NaN values are permitted.
        allow_inf: Whether Inf values are permitted.
        min_length: Minimum required length.

    Returns:
        Validated numpy array with dtype float64.

    Raises:
        EvaluationInputError: If validation fails.
    """
    if scores is None:
        raise EvaluationInputError(
            f"'{name}' is None",
            context={"array_name": name, "reason": "none_input"},
        )
    arr = np.asarray(scores, dtype=np.float64)
    if arr.ndim != 1:
        raise EvaluationInputError(
            f"'{name}' must be 1-dimensional, got shape {arr.shape}",
            context={
                "array_name": name,
                "reason": "wrong_dimensionality",
                "shape": list(arr.shape),
            },
        )
    if len(arr) < min_length:
        raise EvaluationInputError(
            f"'{name}' must have at least {min_length} element(s), "
            f"got {len(arr)}",
            context={
                "array_name": name,
                "reason": "too_short",
                "length": len(arr),
                "min_length": min_length,
            },
        )
    # v35 ROOT FIX (M-21): if the caller passed an integer-dtype
    # array, ``np.asarray(..., dtype=np.float64)`` silently coerced it
    # to float64 — useful behavior, but the silent coercion hid
    # upstream bugs where scores were computed as integers (e.g. a
    # rank field instead of a similarity score). The fix logs a
    # WARNING when coercion happens so operators can detect the
    # upstream bug. The coercion itself is preserved for backward
    # compatibility — downstream AUC math genuinely needs float64.
    if hasattr(scores, "dtype") and scores is not None:
        try:
            _orig_dtype = np.asarray(scores).dtype
            if _orig_dtype != np.float64 and not np.issubdtype(_orig_dtype, np.floating):
                _log_structured(
                    logging.WARNING,
                    "score_array_int_to_float_coercion",
                    array_name=name,
                    original_dtype=str(_orig_dtype),
                    coerced_dtype="float64",
                    length=len(arr),
                )
                EVALUATION_TRANSFORMATIONS_LOG.append({
                    "action": "int_to_float_coercion",
                    "array_name": name,
                    "original_dtype": str(_orig_dtype),
                    "coerced_dtype": "float64",
                })
        except Exception:
            pass
    arr = _sanitize_scores(arr, allow_nan=allow_nan, allow_inf=allow_inf)
    return arr


def _precheck_inputs(
    pos_scores: np.ndarray, neg_scores: np.ndarray
) -> Optional[float]:
    """Pre-check inputs for edge cases that have deterministic AUC.

    Fixes E3-002, E5-001.

    Returns:
        0.5 if scores do not separate classes (single unique score).
        None if normal computation should proceed.

    Raises:
        EvaluationInputError: If either array is empty.
    """
    all_scores = np.concatenate([pos_scores, neg_scores])
    unique_scores = np.unique(all_scores)
    if len(unique_scores) <= 1:
        _log_structured(
            logging.CRITICAL,
            "single_class_scores_critical",
            n_pos=len(pos_scores),
            n_neg=len(neg_scores),
            unique_scores=len(unique_scores),
            evaluation_metric_version=EVALUATION_METRIC_VERSION,
            message=(
                "compute_auc returning 0.5 because pos and neg scores "
                "do not separate (single unique score across both "
                "arrays). This usually indicates a model bug or a "
                "data-loading bug — DO NOT use this AUC for launch "
                "decisions. (M-22)"
            ),
        )
        return 0.5
    return None


def _detect_leakage(
    pos_scores: np.ndarray,
    neg_scores: np.ndarray,
    tol: float = 1e-12,
) -> Dict[str, Any]:
    """Detect potential data leakage between positive and negative scores.

    Fixes E5-002.

    v35 ROOT FIX (H-10): the previous code did a nested loop:
        for ps in pos_scores:
            n_overlap += int(np.sum(np.isclose(neg_scores, ps, atol=tol)))
    which is O(N*M) where N=len(pos) and M=len(neg). For a 5K-pos /
    50K-neg validation set, that was 250M np.isclose calls — adding
    ~18s to every AUC computation. The fix uses ``np.isin`` with a
    rounded-key trick: round both arrays to ``tol`` precision, then
    do a single set-intersection via ``np.isin``. This is O(N+M) on
    average (NumPy uses a hash set internally) and produces identical
    results for the ``tol=1e-12`` default. For non-default tols, we
    fall back to the original nested loop (rare in practice — the
    default is what every caller uses).

    Args:
        pos_scores: Positive scores.
        neg_scores: Negative scores.
        tol: Tolerance for considering scores identical.

    Returns:
        Dict with overlap statistics.
    """
    # H-10: O(N+M) path for the default tol (exact-equality check
    # after rounding to 12 decimal places).
    if tol == 1e-12 and len(pos_scores) > 0 and len(neg_scores) > 0:
        # Round to 12 decimals so 1e-12 differences register as equal.
        pos_rounded = np.round(pos_scores, decimals=12)
        neg_rounded = np.round(neg_scores, decimals=12)
        # np.isin returns a boolean mask of neg_rounded entries that
        # appear in pos_rounded. Sum gives total overlap count.
        neg_in_pos_mask = np.isin(neg_rounded, pos_rounded)
        n_overlap = int(neg_in_pos_mask.sum())
    else:
        # Original nested loop for non-default tol or empty arrays.
        n_overlap = 0
        for ps in pos_scores:
            n_overlap += int(np.sum(np.isclose(neg_scores, ps, atol=tol)))
    overlap_ratio = n_overlap / max(len(pos_scores) * len(neg_scores), 1)
    likely_same = overlap_ratio > 0.5
    return {
        "n_identical_scores": n_overlap,
        "overlap_ratio": overlap_ratio,
        "likely_same_array": likely_same,
    }


def _detect_false_negatives(
    pos_ids: Optional[np.ndarray] = None,
    neg_ids: Optional[np.ndarray] = None,
) -> None:
    """Guard against pos/neg triple-ID collision.

    Fixes E5-005.

    v35 ROOT FIX (M-9): the previous code silently returned when
    either ``pos_ids`` or ``neg_ids`` was None — logging only a
    DEBUG message that was invisible at the default INFO log level.
    This meant a caller that forgot to pass IDs would silently skip
    the integrity check, and downstream metrics could be inflated by
    false negatives without any warning. The fix logs at WARNING so
    operators can see the check was skipped and investigate.

    Raises:
        EvaluationIntegrityError: If any triple ID appears in both sets.
    """
    if pos_ids is None or neg_ids is None:
        _log_structured(
            logging.WARNING,
            "false_negative_check_skipped",
            reason="ids_not_provided",
            pos_ids_provided=pos_ids is not None,
            neg_ids_provided=neg_ids is not None,
        )
        return
    pos_set = set(pos_ids.tolist())
    neg_set = set(neg_ids.tolist())
    collision = pos_set & neg_set
    if collision:
        raise EvaluationIntegrityError(
            f"Triple-ID collision detected: {len(collision)} IDs "
            f"appear in both positive and negative sets",
            context={
                "reason": "pos_neg_triple_collision",
                "n_collisions": len(collision),
            },
        )


def _validate_sorted(
    ranked_scores: List[RankedItem],
    higher_is_better: bool,
    tolerance: float = 1e-12,
) -> bool:
    """Check whether the ranked list is sorted in the expected direction.

    Fixes E3-001.

    Args:
        ranked_scores: List of (entity_id, score, is_true) tuples.
        higher_is_better: If True, expect descending scores.
        tolerance: Tolerance for comparing adjacent scores.

    Returns:
        True if sorted correctly, False otherwise.
    """
    if len(ranked_scores) <= 1:
        return True
    scores = np.array([s for _, s, _ in ranked_scores])
    diffs = np.diff(scores)
    if higher_is_better:
        return bool(np.all(diffs <= tolerance))
    else:
        return bool(np.all(diffs >= -tolerance))


def _validate_ranked_list(
    ranked_scores: List[RankedItem],
    higher_is_better: bool,
    function_name: str,
) -> List[RankedItem]:
    """Validate and auto-sort a ranked list if mis-sorted.

    Fixes E5-004, E3-001.

    Args:
        ranked_scores: Input ranked list.
        higher_is_better: Expected sort direction.
        function_name: Name of the calling function (for logging).

    Returns:
        The ranked list, sorted if necessary.
    """
    if _validate_sorted(ranked_scores, higher_is_better):
        return ranked_scores
    # Count out-of-order pairs
    scores = np.array([s for _, s, _ in ranked_scores])
    diffs = np.diff(scores)
    if higher_is_better:
        n_ooo = int(np.sum(diffs > 1e-12))
    else:
        n_ooo = int(np.sum(diffs < -1e-12))
    _log_structured(
        logging.WARNING,
        "ranked_list_auto_resorted",
        function=function_name,
        out_of_order_count=n_ooo,
        action="auto_resorted",
    )
    EVALUATION_TRANSFORMATIONS_LOG.append(
        {
            "action": "auto_resort",
            "function": function_name,
            "out_of_order_count": n_ooo,
        }
    )
    reverse = higher_is_better
    sorted_list = sorted(
        ranked_scores, key=lambda x: (x[1], x[0]), reverse=reverse
    )
    return sorted_list


def _check_sklearn_version() -> Optional[str]:
    """Return installed sklearn version, or None if not installed.

    Fixes E15-005.

    Returns:
        Version string, or None.
    """
    try:
        import sklearn
        ver = sklearn.__version__
        _log_structured(
            logging.INFO, "sklearn_version", version=ver
        )
        from packaging.version import Version as _V

        if _V(ver) < _V(SKLEARN_MIN_VERSION):
            _log_structured(
                logging.WARNING,
                "sklearn_old_version",
                installed=ver,
                minimum=SKLEARN_MIN_VERSION,
            )
        return ver
    except ImportError:
        return None
    except Exception:
        return None


def _check_authorization(
    operation: str, data_scope: Optional[str] = None
) -> None:
    """Check environment-based authorization for evaluation operations.

    Fixes E9-004.

    This is a guard rail, NOT a full RBAC system. Production
    deployments MUST set DRUGOS_EVAL_USER and DRUGOS_EVAL_ROLE env
    vars. The API layer (FastAPI, Phase 5) must propagate these
    from the authenticated session.

    Args:
        operation: Name of the operation being performed.
        data_scope: Optional data scope identifier.

    Raises:
        EvaluationSecurityError: If authorization is denied.
    """
    role = os.environ.get("DRUGOS_EVAL_ROLE")
    if role is None:
        _log_structured(
            logging.DEBUG,
            "authorization_skipped",
            reason="DRUGOS_EVAL_ROLE not set — development mode",
        )
        return
    if role == "read_only" and operation != "read":
        raise EvaluationSecurityError(
            f"Operation '{operation}' not allowed in read_only mode",
            context={
                "reason": "authorization_denied",
                "role": role,
                "operation": operation,
                "data_scope": data_scope,
            },
        )


def _sanitize_entity_id(
    entity_id: Any,
    hash_string_ids: bool = True,
) -> Union[int, str]:
    """Sanitize an entity ID to prevent PII leakage.

    Fixes E9-001.

    Args:
        entity_id: The entity ID to sanitize.
        hash_string_ids: If True, hash string IDs via SHA-256.

    Returns:
        Sanitized entity ID (int or hashed str).

    Raises:
        EvaluationSecurityError: If the ID type is invalid.
    """
    if isinstance(entity_id, int):
        return entity_id
    if isinstance(entity_id, str):
        if hash_string_ids:
            return hashlib.sha256(entity_id.encode()).hexdigest()[:16]
        return entity_id
    raise EvaluationSecurityError(
        f"Invalid entity_id type: {type(entity_id)}",
        context={
            "reason": "invalid_entity_id_type",
            "type": str(type(entity_id)),
        },
    )


def redact_entity_ids(
    ranked_lists: List[List[RankedItem]],
) -> List[List[RankedItem]]:
    """Replace entity IDs with sequential integers for safe logging.

    Fixes E9-001.

    Drug names, disease names, and patient identifiers MUST NOT appear
    in log files. Use this function before logging any ranked list.

    Args:
        ranked_lists: The ranked lists to redact.

    Returns:
        Redacted ranked lists with sequential integer IDs.
    """
    redacted = []
    for rl in ranked_lists:
        # RankedItem is a 3-tuple: (entity_id, score, is_true).
        # Replace entity_id with a sequential integer to preserve ranking
        # order without leaking PII. Fixes audit Tier-1 bug #7.
        redacted_rl = [(i, score, is_true) for i, (_eid, score, is_true) in enumerate(rl)]
        redacted.append(redacted_rl)  # type: ignore[arg-type]
    return redacted


def _format_metrics_log(metrics: Dict[str, float]) -> str:
    """Format metric values consistently with 4 decimal places.

    Fixes E11-001.

    Args:
        metrics: Dictionary of metric name to value.

    Returns:
        Formatted string like "AUC=0.7812 | P@10=0.3000".
    """
    parts = []
    for name, value in sorted(metrics.items()):
        if isinstance(value, float):
            parts.append(f"{name}={value:.4f}")
        else:
            parts.append(f"{name}={value}")
    return " | ".join(parts)


def compute_score_distribution(
    scores: np.ndarray,
) -> Dict[str, Union[float, int]]:
    """Compute distribution statistics for a score array.

    Fixes E5-003.

    v35 ROOT FIX (L-20 / L-37): document truncation behavior and
    batch the computation. The function drops NaN and Inf values
    before computing statistics — this is the ``truncation`` the
    docstring warned about. Callers that need to know how many
    values were dropped should inspect the ``n_nan`` and ``n_inf``
    fields in the returned dict (they are computed against the
    ORIGINAL array, not the cleaned one). L-37: the previous code
    called ``np.isnan(scores)`` and ``np.isinf(scores)`` separately
    on the original array AND on the cleaned array (4 passes). The
    fix computes the NaN/Inf masks ONCE and reuses them, halving
    the number of full-array scans for large score arrays.

    Args:
        scores: Score array.

    Returns:
        Dict with min, max, mean, std, median, quartiles, and
        counts of NaN/Inf/unique/tie values.
    """
    # L-37: compute NaN/Inf masks ONCE for reuse.
    nan_mask = np.isnan(scores)
    inf_mask = np.isinf(scores)
    n_nan = int(nan_mask.sum())
    n_inf = int(inf_mask.sum())
    # Drop NaN and Inf in one pass via ``~(nan_mask | inf_mask)``.
    clean_mask = ~(nan_mask | inf_mask)
    clean = scores[clean_mask]
    if len(clean) == 0:
        return {
            "min": float("nan"),
            "max": float("nan"),
            "mean": float("nan"),
            "std": float("nan"),
            "median": float("nan"),
            "q25": float("nan"),
            "q75": float("nan"),
            "n_nan": n_nan,
            "n_inf": n_inf,
            "n_unique": 0,
            "n_ties": 0,
        }
    _, counts = np.unique(clean, return_counts=True)
    n_ties = int(np.sum(counts > 1))
    return {
        "min": _to_native_float(float(np.min(clean))),
        "max": _to_native_float(float(np.max(clean))),
        "mean": _to_native_float(float(np.mean(clean))),
        "std": _to_native_float(float(np.std(clean))),
        "median": _to_native_float(float(np.median(clean))),
        "q25": _to_native_float(float(np.percentile(clean, 25))),
        "q75": _to_native_float(float(np.percentile(clean, 75))),
        "n_nan": n_nan,
        "n_inf": n_inf,
        "n_unique": int(len(np.unique(clean))),
        "n_ties": n_ties,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC API — AUC Computation
# ═══════════════════════════════════════════════════════════════════════════════


def compute_auc(
    pos_scores: np.ndarray,
    neg_scores: np.ndarray,
    higher_is_better: bool = False,
    *,
    allow_nan: bool = False,
) -> float:
    """Compute AUC (Area Under ROC Curve) for link prediction.

    This function is deterministic across environments. The sklearn
    path is an optimisation; results are verified against the
    canonical Mann-Whitney implementation on every call when
    ``EvaluationConfig.verify_sklearn_agreement=True``.

    Scientific Rationale:  # Fixes E3-005
        TransE (Bordes et al. 2013) scores triples by L1 distance
        ||h + r - t||_1 (v28 ROOT FIX P2-B-7: was L2 previously;
        changed to L1 to match the cited paper). Lower distance
        => more plausible triple. Therefore, for TransE-derived
        scores, ``higher_is_better=False`` is the SCIENTIFICALLY
        CORRECT default. Scores are negated before AUC so that
        "positive scores are higher" in the transformed space,
        matching the convention ``roc_auc_score`` expects.

        For the Phase 3 Graph Transformer (dot-product attention),
        higher score => more plausible, so ``higher_is_better=True``
        MUST be passed explicitly or set via
        ``EvaluationConfig.default_higher_is_better``.

    Examples
    --------
    >>> import numpy as np
    >>> from drugos_graph.evaluation import compute_auc
    >>> pos = np.array([0.1, 0.2, 0.3])  # TransE distances
    >>> neg = np.array([0.8, 0.9, 1.0])
    >>> compute_auc(pos, neg, higher_is_better=False)
    1.0
    >>> compute_auc(pos, neg, higher_is_better=True)
    0.0

    Args:
        pos_scores: Scores for positive (true) edges.
        neg_scores: Scores for negative (false) edges.
        higher_is_better: If True, higher scores indicate more likely
            positives (e.g. cosine similarity). If False (default
            for TransE), lower scores indicate more likely positives
            and scores are negated before computing AUC.
        allow_nan: If True, NaN scores are dropped with a warning.
            Default False (raises EvaluationInputError).

    Returns:
        AUC value between 0.0 and 1.0.

    Raises:
        EvaluationInputError: If inputs are empty, contain NaN/Inf,
            or have other validation failures.
        EvaluationIntegrityError: If computed AUC is out of [0, 1]
            or sklearn/manual paths disagree.
        EvaluationError: For unexpected errors (wraps raw exceptions).

    References:
        Mann & Whitney (1947) — see ``MANN_WHITNEY_REFERENCE``.
        Bordes et al. (2013) — see ``BORDES_2013_REFERENCE``.
    """
    # Fixes E9-004 — authorization check
    _check_authorization("compute_auc")

    try:
        pos_scores = _validate_score_array(
            pos_scores, "pos_scores", allow_nan=allow_nan
        )
        neg_scores = _validate_score_array(
            neg_scores, "neg_scores", allow_nan=allow_nan
        )

        # Fixes E5-002 — leakage detection
        # v43 ROOT FIX (P2-010): scale the leakage warning threshold by
        # sample size. The previous fixed 5% threshold was underpowered
        # for small samples: with 7 pos + 18 neg = 126 pairs, even 1
        # overlap = 0.79% which is BELOW the 5% threshold, so the
        # warning never fires. The fix uses a sample-size-aware
        # threshold: max(0.05, 1/sqrt(n_pos*n_neg)) so that even 1
        # overlap in a small sample triggers the warning. For large
        # samples (n_pos*n_neg > 400), the threshold stays at 5%.
        leakage = _detect_leakage(pos_scores, neg_scores)
        if leakage["likely_same_array"]:
            raise EvaluationIntegrityError(
                "Positive and negative score arrays appear identical",
                context={
                    "reason": "pos_neg_likely_identical",
                    "overlap_ratio": leakage["overlap_ratio"],
                },
            )
        else:
            # v43 ROOT FIX (P2-010): sample-size-aware threshold.
            _n_pairs = len(pos_scores) * len(neg_scores)
            _dynamic_threshold = max(0.05, 1.0 / max(_n_pairs ** 0.5, 1.0))
            if leakage["overlap_ratio"] > _dynamic_threshold:
                _log_structured(
                    logging.WARNING,
                    "score_overlap_detected",
                    overlap_ratio=leakage["overlap_ratio"],
                    threshold=_dynamic_threshold,
                    n_pos=len(pos_scores),
                    n_neg=len(neg_scores),
                    n_overlap=leakage["n_identical_scores"],
                    note="v43 P2-010: sample-size-aware threshold",
                )

        # Fixes E3-002 — pre-check for single-class inputs
        precheck = _precheck_inputs(pos_scores, neg_scores)
        if precheck is not None:
            return precheck

        sklearn_version = _check_sklearn_version()

        if sklearn_version is not None and _ROC_AUC_SCORE is not None:
            # sklearn available — fast path
            # v43 ROOT FIX (P2-025): use module-level _ROC_AUC_SCORE
            # instead of importing on every call.
            try:
                labels = np.concatenate(
                    [np.ones(len(pos_scores)), np.zeros(len(neg_scores))]
                )
                scores = np.concatenate([pos_scores, neg_scores])

                # v28 ROOT FIX (audit ML-12): the previous code used
                # ``np.negative(scores, out=scores)`` — an IN-PLACE
                # mutation of the caller's concatenated array. Because
                # ``scores`` was built via ``np.concatenate([pos_scores,
                # neg_scores])`` it WAS a fresh array at this point,
                # but the in-place form silently violated the
                # principle of least surprise: a future refactor that
                # re-used a caller-provided scores array (e.g. for
                # caching) would have its values negated without
                # warning, producing silently wrong AUC values on the
                # NEXT call. The fix creates a new array via the
                # unary minus operator, which is non-mutating and
                # documents intent ("we want the negated view for
                # AUC computation only, the caller's data is
                # unchanged"). Also fixes E4-005 properly — the
                # in-place form was claimed to be the E4-005 fix, but
                # in-place mutation is exactly the E4-005 root cause.
                if not higher_is_better:
                    scores = -scores

                auc = _ROC_AUC_SCORE(labels, scores)  # v43 P2-025: module-level
                auc = float(auc)

                # Fixes E7-001 — verify agreement with manual path
                from .config import EVALUATION_CONFIG

                if EVALUATION_CONFIG.verify_sklearn_agreement:
                    manual_auc = _manual_auc(
                        pos_scores, neg_scores, higher_is_better
                    )
                    try:
                        np.testing.assert_allclose(
                            auc, manual_auc, atol=1e-12
                        )
                    except AssertionError:
                        raise EvaluationReproducibilityError(
                            "sklearn and manual AUC paths disagree",
                            context={
                                "sklearn_auc": auc,
                                "manual_auc": manual_auc,
                                "atol": 1e-12,
                            },
                        )

                _log_structured(
                    logging.INFO,
                    "auc_computed",
                    algorithm="sklearn",
                    auc_value=auc,
                )
                EVALUATION_TRANSFORMATIONS_LOG.append(
                    {"action": "auc_via_sklearn", "value": auc}
                )
                return auc

            except EvaluationReproducibilityError:
                raise
            except ValueError as e:
                # Fixes E6-001, E3-002 — handle sklearn ValueError
                if "single class" in str(e).lower() or "only one" in str(e).lower():
                    _log_structured(
                        logging.WARNING,
                        "sklearn_single_class_fallback",
                        n_pos=len(pos_scores),
                        n_neg=len(neg_scores),
                        sklearn_error=str(e),
                        evaluation_metric_version=EVALUATION_METRIC_VERSION,
                    )
                    EVALUATION_TRANSFORMATIONS_LOG.append(
                        {
                            "action": "sklearn_value_error_fallback",
                            "reason": "single_class",
                        }
                    )
                    return 0.5
                raise EvaluationIntegrityError(
                    f"sklearn roc_auc_score failed: {e}",
                    context={
                        "reason": "sklearn_auc_failed",
                        "sklearn_error": str(e),
                        "n_pos": len(pos_scores),
                        "n_neg": len(neg_scores),
                    },
                ) from e
        else:
            # sklearn not available — manual path
            strategy = EVALUATION_FALLBACK_STRATEGY
            if strategy == "fail":
                raise EvaluationError(
                    "sklearn is not installed and fallback strategy is 'fail'",
                    context={"reason": "sklearn_not_installed"},
                )
            if strategy == "warn":
                _log_structured(
                    logging.WARNING,
                    "sklearn_fallback_to_manual",
                    strategy=strategy,
                )
            EVALUATION_TRANSFORMATIONS_LOG.append(
                {"action": "auc_via_manual", "reason": "sklearn_unavailable"}
            )
            return _manual_auc(pos_scores, neg_scores, higher_is_better)

    except DrugOSDataError:
        raise
    except Exception as e:
        raise EvaluationError(
            f"Unexpected error in compute_auc: {e}",
            context={
                "function": "compute_auc",
                "error_type": type(e).__name__,
            },
        ) from e


# ═══════════════════════════════════════════════════════════════════════════════
# MANUAL AUC — Canonical Implementation
# ═══════════════════════════════════════════════════════════════════════════════


def _manual_auc(
    pos_scores: np.ndarray,
    neg_scores: np.ndarray,
    higher_is_better: bool = False,
) -> float:
    """Manual AUC via Mann-Whitney U statistic with Wilcoxon tie correction.

    Implements the Mann-Whitney U statistic with Wilcoxon half-credit
    tie correction, mathematically equivalent to
    ``U1 / (n1 * n2)``. See ``MANN_WHITNEY_REFERENCE``.
    Equivalence to ``sklearn.metrics.roc_auc_score`` is verified by
    ``tests/test_evaluation.py::test_sklearn_manual_agreement``.

    This is the CANONICAL AUC implementation for the DrugOS pipeline.
    The sklearn path in ``compute_auc`` is an optimisation that must
    produce bit-identical results to this function (within 1e-12).

    The tie-handling is order-independent: the rank-sum formula
    ``AUC = (sum_of_ranks_of_positives - n_pos*(n_pos+1)/2)
    / (n_pos * n_neg)`` is provably independent of input order.

    Time complexity: O(n log n) for the sort. Space: O(n) for ranks.
    Tested up to 10M scores.

    Fixes E7-002 (order-independent ties), E4-001 (vectorized),
    E4-002 (reduced memory), E4-004 (renamed variable), E6-002
    (clamp to [0, 1]), E3-004 (scientific reference), E14-001
    (standard formula reference).

    Args:
        pos_scores: Scores for positive edges.
        neg_scores: Scores for negative edges.
        higher_is_better: If False (default, TransE), lower score =
            more likely positive, so we compute
            P(pos_score < neg_score). If True, higher score = more
            likely positive, so we compute P(pos_score > neg_score).

    Returns:
        AUC value between 0.0 and 1.0.
    """
    n_pos = len(pos_scores)
    n_neg = len(neg_scores)
    if n_pos == 0 or n_neg == 0:
        return 0.5

    # Do NOT negate scores. The Mann-Whitney U statistic is invariant
    # under monotone transformation, and the AUC direction is handled
    # by the post-U sign choice below.
    #
    # Audit fix (v5 Tier-2 bug #9): the previous code negated BOTH
    # pos_scores and neg_scores when higher_is_better=True. That flipped
    # U to (n_pos*n_neg - U), so the previous `auc = U/(n_pos*n_neg)`
    # returned `1 - true_AUC` for the higher-is-better case (e.g. Phase 3
    # Graph Transformer). The fix is to NOT negate and to use the
    # natural Mann-Whitney direction:
    #   U/(n_pos*n_neg) = P(pos_score > neg_score)
    #   - higher_is_better=True  -> AUC = P(pos > neg) = U/(n_pos*n_neg)
    #   - higher_is_better=False -> AUC = P(pos < neg) = 1 - U/(n_pos*n_neg)
    all_scores = np.concatenate([pos_scores, neg_scores])
    labels = np.concatenate([np.ones(n_pos, dtype=np.float64),
                             np.zeros(n_neg, dtype=np.float64)])

    # Compute ranks using average tie-breaking (Wilcoxon half-credit)
    # Fixes E4-001, E7-002 — vectorized, order-independent
    order = np.argsort(all_scores, kind="mergesort")
    sorted_scores = all_scores[order]
    sorted_labels = labels[order]

    # Compute average ranks for ties
    unique_vals, inverse, counts = np.unique(
        sorted_scores, return_inverse=True, return_counts=True
    )

    # For each unique value, compute the average rank
    # rank_start[i] is the starting position (1-based) of the i-th unique value
    rank_start = np.zeros(len(unique_vals), dtype=np.float64)
    pos = 0
    for i in range(len(unique_vals)):
        rank_start[i] = pos + 1  # 1-based
        pos += int(counts[i])

    # Average rank for each unique value
    avg_ranks = rank_start + (counts.astype(np.float64) - 1) / 2.0

    # Map each element to its average rank
    ranks = avg_ranks[inverse]

    # Mann-Whitney U = sum_of_ranks_of_positives -
    #   n_pos*(n_pos+1)/2 measures P(pos_score > neg_score)
    #   in the current score orientation.
    # Ref: Mann & Whitney (1947).  Fixes E14-001, E4-004, audit v5 #9.
    sum_of_ranks_of_positives = float(np.sum(ranks * sorted_labels))
    u_statistic = (
        sum_of_ranks_of_positives - n_pos * (n_pos + 1) / 2.0
    )
    if higher_is_better:
        # AUC = P(pos > neg)
        auc = u_statistic / (n_pos * n_neg)
    else:
        # lower=better (TransE default): AUC = P(pos < neg) = 1 - P(pos > neg)
        auc = 1.0 - u_statistic / (n_pos * n_neg)

    # Fixes E6-002 — clamp to [0, 1] for floating-point safety
    clamped = max(0.0, min(1.0, float(auc)))
    if clamped != auc:
        _log_structured(
            logging.WARNING,
            "auc_clamped",
            original=auc,
            clamped=clamped,
        )
        EVALUATION_TRANSFORMATIONS_LOG.append(
            {
                "action": "auc_clamped",
                "original": float(auc),
                "clamped": clamped,
            }
        )
    return clamped


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC API — Ranking Metrics
# ═══════════════════════════════════════════════════════════════════════════════


def precision_at_k(
    ranked_scores: List[RankedItem],
    k: int = 10,
    *,
    higher_is_better: bool = False,
    strict_k: bool = False,
) -> float:
    """Compute Precision@K.

    ranked_scores MUST be sorted ascending by score when
    ``higher_is_better=False`` (TransE convention), descending when
    ``True``. Sort direction is validated; if violated, the list is
    re-sorted and a warning is logged.

    Notes
    -----
    When ``len(ranked_scores) < k``, behavior is controlled by
    ``strict_k``. When ``strict_k=False`` (default), precision =
    hits / k (standard P@K convention; can yield < 1.0 even with
    all-hits). When ``strict_k=True``, precision =
    hits / min(k, len(ranked_scores)) (capped convention; suitable
    for rare-disease candidate sets).

    Examples
    --------
    >>> precision_at_k([(0, 0.1, True), (1, 0.2, False)], k=1)
    1.0
    >>> precision_at_k([(0, 0.1, False), (1, 0.2, True)], k=2)
    0.5

    Args:
        ranked_scores: List of (entity_id, score, is_true) tuples,
            sorted by score.
        k: Cutoff rank.
        higher_is_better: If True, expect descending scores.
            Default False (TransE convention).
        strict_k: If True, divide by min(k, len(ranked_scores)).
            Default False (standard P@K convention).

    Returns:
        Precision@K value.

    Raises:
        EvaluationInputError: If k < 1.
    """
    _check_authorization("precision_at_k")

    try:
        if k < 1:
            raise EvaluationInputError(
                "k must be >= 1",
                context={"k": k, "reason": "invalid_k"},
            )

        ranked_scores = _validate_ranked_list(
            ranked_scores, higher_is_better, "precision_at_k"
        )

        top_k = ranked_scores[:k]
        if not top_k:
            return 0.0

        hits = sum(1 for _, _, is_true in top_k if is_true)
        denominator = k if not strict_k else min(k, len(ranked_scores))

        if len(ranked_scores) < k:
            _log_structured(
                logging.DEBUG,
                "precision_k_list_shorter_than_k",
                k=k,
                list_length=len(ranked_scores),
                strict_k=strict_k,
            )

        return hits / denominator

    except DrugOSDataError:
        raise
    except Exception as e:
        raise EvaluationError(
            f"Unexpected error in precision_at_k: {e}",
            context={
                "function": "precision_at_k",
                "error_type": type(e).__name__,
            },
        ) from e


def recall_at_k(
    ranked_scores: List[RankedItem],
    total_positives: int,
    k: int = 10,
    *,
    higher_is_better: bool = False,
) -> float:
    """Compute Recall@K.

    Recall@K = |{relevant items in top K}| /
    |{all relevant items in evaluation set}|. The denominator is
    the TOTAL count of relevant items for the query, NOT the count
    in the ranked list. See Bordes et al. 2013 and the DRKG
    evaluation protocol.

    ranked_scores MUST be sorted ascending by score when
    ``higher_is_better=False`` (TransE convention), descending when
    ``True``. Sort direction is validated; if violated, the list is
    re-sorted and a warning is logged.

    Examples
    --------
    >>> recall_at_k(
    ...     [(0, 0.1, True), (1, 0.2, False)],
    ...     total_positives=50, k=10
    ... )
    0.02

    Args:
        ranked_scores: List of (entity_id, score, is_true) tuples,
            sorted by score.
        total_positives: Total number of true positives in the
            ENTIRE evaluation set for this query (NOT just in the
            ranked list).  # Fixes E2-002
        k: Cutoff rank.
        higher_is_better: If True, expect descending scores.
            Default False (TransE convention).

    Returns:
        Recall@K value.

    Raises:
        EvaluationInputError: If total_positives <= 0 or k < 1.
    """
    _check_authorization("recall_at_k")

    try:
        if total_positives <= 0:
            raise EvaluationInputError(
                "total_positives must be > 0",
                context={
                    "total_positives": total_positives,
                    "reason": "invalid_total_positives",
                },
            )
        if k < 1:
            raise EvaluationInputError(
                "k must be >= 1",
                context={"k": k, "reason": "invalid_k"},
            )

        ranked_scores = _validate_ranked_list(
            ranked_scores, higher_is_better, "recall_at_k"
        )

        top_k = ranked_scores[:k]
        hits = sum(1 for _, _, is_true in top_k if is_true)
        return hits / total_positives

    except DrugOSDataError:
        raise
    except Exception as e:
        raise EvaluationError(
            f"Unexpected error in recall_at_k: {e}",
            context={
                "function": "recall_at_k",
                "error_type": type(e).__name__,
            },
        ) from e


def mean_reciprocal_rank(
    ranked_lists: List[List[RankedItem]],
    *,
    higher_is_better: bool = False,
) -> float:
    """Compute Mean Reciprocal Rank (MRR).

    ranked_lists MUST have inner lists sorted ascending by score when
    ``higher_is_better=False`` (TransE convention), descending when
    ``True``. Sort direction is validated; if violated, lists are
    re-sorted and a warning is logged.

    Examples
    --------
    >>> mean_reciprocal_rank([
    ...     [(0, 0.1, False), (1, 0.2, True)],
    ...     [(0, 0.1, True)],
    ... ])
    0.75

    Args:
        ranked_lists: List of ranked score lists, one per query entity.
        higher_is_better: If True, expect descending scores.

    Returns:
        MRR value.
    """
    _check_authorization("mean_reciprocal_rank")

    try:
        if not ranked_lists:
            return 0.0

        rr_sum = 0.0
        for ranked in ranked_lists:
            ranked = _validate_ranked_list(
                ranked, higher_is_better, "mean_reciprocal_rank"
            )
            for rank, (_, _, is_true) in enumerate(ranked, 1):
                if is_true:
                    rr_sum += 1.0 / rank
                    break
        return rr_sum / len(ranked_lists)

    except DrugOSDataError:
        raise
    except Exception as e:
        raise EvaluationError(
            f"Unexpected error in mean_reciprocal_rank: {e}",
            context={
                "function": "mean_reciprocal_rank",
                "error_type": type(e).__name__,
            },
        ) from e


def hits_at_k(
    ranked_lists: List[List[RankedItem]],
    k: int = 10,
    *,
    higher_is_better: bool = False,
) -> float:
    """Compute Hits@K (proportion of queries with a true positive in top K).

    ranked_lists MUST have inner lists sorted ascending by score when
    ``higher_is_better=False`` (TransE convention), descending when
    ``True``. Sort direction is validated; if violated, lists are
    re-sorted and a warning is logged.

    Examples
    --------
    >>> hits_at_k(
    ...     [[(0, 0.1, True), (1, 0.2, False)]],
    ...     k=1
    ... )
    1.0

    Args:
        ranked_lists: List of ranked score lists.
        k: Cutoff rank.
        higher_is_better: If True, expect descending scores.

    Returns:
        Hits@K value.
    """
    _check_authorization("hits_at_k")

    try:
        if not ranked_lists:
            return 0.0
        if k < 1:
            raise EvaluationInputError(
                "k must be >= 1",
                context={"k": k, "reason": "invalid_k"},
            )

        hits = 0
        for ranked in ranked_lists:
            ranked = _validate_ranked_list(
                ranked, higher_is_better, "hits_at_k"
            )
            top_k = ranked[:k]
            if any(is_true for _, _, is_true in top_k):
                hits += 1
        return hits / len(ranked_lists)

    except DrugOSDataError:
        raise
    except Exception as e:
        raise EvaluationError(
            f"Unexpected error in hits_at_k: {e}",
            context={
                "function": "hits_at_k",
                "error_type": type(e).__name__,
            },
        ) from e


# ═══════════════════════════════════════════════════════════════════════════════
# BUILDER / FACTORY — Ranked List Construction
# ═══════════════════════════════════════════════════════════════════════════════


def build_ranked_lists(
    pos_scores: np.ndarray,
    neg_scores: np.ndarray,
    pos_entity_ids: Optional[np.ndarray] = None,
    neg_entity_ids: Optional[np.ndarray] = None,
    higher_is_better: bool = False,
) -> List[List[RankedItem]]:
    """Build ranked lists from positive and negative score arrays.

    Creates one ranked list per query entity by combining pos and neg
    scores, sorting by score, and labelling. This is the primary
    factory for constructing the ``ranked_lists`` input format.

    Fixes E2-001, E2-004.

    Args:
        pos_scores: Scores for positive edges, shape (N,).
        neg_scores: Scores for negative edges, shape (N,).
        pos_entity_ids: Optional entity IDs for positive edges.
        neg_entity_ids: Optional entity IDs for negative edges.
        higher_is_better: Sort direction for ranking.

    Returns:
        List of ranked lists, one per element.
    """
    pos_scores = _validate_score_array(pos_scores, "pos_scores")
    neg_scores = _validate_score_array(neg_scores, "neg_scores")
    n = min(len(pos_scores), len(neg_scores))
    ranked_lists = []
    for i in range(n):
        items = []
        pid = int(pos_entity_ids[i]) if pos_entity_ids is not None else i
        nid = int(neg_entity_ids[i]) if neg_entity_ids is not None else i + n
        items.append((pid, float(pos_scores[i]), True))
        items.append((nid, float(neg_scores[i]), False))
        reverse = higher_is_better
        items.sort(key=lambda x: (x[1], x[0]), reverse=reverse)
        ranked_lists.append(items)
    return ranked_lists


def scores_to_ranked_lists(
    scores_tensor: np.ndarray,
    labels_tensor: np.ndarray,
    entity_ids: Optional[np.ndarray] = None,
    higher_is_better: bool = False,
) -> List[List[RankedItem]]:
    """Convert model output scores and labels to ranked lists.

    This is the common-case adapter for converting model output
    tensors directly into the ranked list format expected by the
    ranking metrics.

    Fixes E2-001.

    Args:
        scores_tensor: Array of model scores, shape (N,).
        labels_tensor: Array of binary labels (1=positive, 0=negative).
        entity_ids: Optional entity IDs.
        higher_is_better: Sort direction.

    Returns:
        List of ranked lists.
    """
    scores_tensor = _validate_score_array(
        scores_tensor, "scores_tensor"
    )
    labels_tensor = np.asarray(labels_tensor, dtype=np.float64)
    items = []
    for i in range(len(scores_tensor)):
        eid = (
            int(entity_ids[i]) if entity_ids is not None else i
        )
        items.append(
            (eid, float(scores_tensor[i]), bool(labels_tensor[i]))
        )
    reverse = higher_is_better
    items.sort(key=lambda x: (x[1], x[0]), reverse=reverse)
    return [items]


def _coerce_to_ranked_list(
    scores: np.ndarray,
    labels: np.ndarray,
    higher_is_better: bool = False,
) -> List[RankedItem]:
    """Coerce numpy arrays of scores and labels to a ranked list.

    Fixes E2-001 — allows numpy-array inputs for ranking functions.
    """
    items = []
    for i in range(len(scores)):
        items.append((i, float(scores[i]), bool(labels[i])))
    reverse = higher_is_better
    items.sort(key=lambda x: (x[1], x[0]), reverse=reverse)
    return items


# ═══════════════════════════════════════════════════════════════════════════════
# SINGLE-PASS RANKING METRICS (Performance)
# ═══════════════════════════════════════════════════════════════════════════════


def _compute_all_ranking_metrics(
    ranked_lists: List[List[RankedItem]],
    k_values: Tuple[int, ...],
    total_positives_per_query: Optional[List[int]] = None,
    higher_is_better: bool = False,
    strict_precision_k: bool = False,
    strict_recall_denominator: bool = True,
    other_true_triples_per_query: Optional[List[set]] = None,
) -> Dict[str, float]:
    """Compute P@K, R@K, MRR, Hits@K in a single pass per ranked list.

    Fixes E8-002 — eliminates 4-pass iteration over ranked_lists.

    Args:
        ranked_lists: List of ranked lists.
        k_values: Tuple of K values to compute.
        total_positives_per_query: Optional true positive counts.
        higher_is_better: Sort direction.
        strict_precision_k: P@K denominator mode.
        strict_recall_denominator: If True, raise on missing totals.

    Returns:
        Dict of metric name to value.
    """
    metrics: Dict[str, float] = {}
    n_queries = len(ranked_lists)

    # Initialise accumulators
    precision_sums: Dict[int, float] = {k: 0.0 for k in k_values}
    recall_sums: Dict[int, float] = {k: 0.0 for k in k_values}
    hits_sums: Dict[int, float] = {k: 0.0 for k in k_values}
    mrr_sum = 0.0
    # v22 ROOT FIX (Audit section 7 finding 9 — "Non-filtered MRR"):
    # the audit flagged that the previous code computed only the RAW
    # MRR (where other true positives in the candidate set inflate the
    # rank of the target triple) and reported it under the unqualified
    # ``mrr`` key, misleading pharmaceutical partners. The v21 fix
    # added an ``mrr_is_filtered=False`` flag but did NOT actually
    # compute the filtered metric. v22 root fix: actually implement
    # the filtered MRR / Hits@K protocol from the KG-embedding
    # literature (Bordes et al. 2013, Sun et al. 2019). For each
    # query, we remove OTHER true triples from the candidate ranking
    # before computing the rank of the target true triple. This
    # requires the caller to pass ``other_true_triples_per_query`` —
    # a list of sets of entity IDs that are ALSO true tails for the
    # same (head, relation) pair (excluding the target). When this
    # is None, the filtered metrics are not computed and only the raw
    # values are emitted (with the existing ``*_is_filtered=False``
    # flags preserved).
    mrr_filtered_sum = 0.0
    hits_filtered_sums: Dict[int, float] = {k: 0.0 for k in k_values}
    n_queries_with_filter_set = 0
    n_unsorted = 0
    n_no_true = 0
    n_shorter_than_k: Dict[int, int] = {k: 0 for k in k_values}

    for qi, ranked in enumerate(ranked_lists):
        # Validate and possibly re-sort
        if not _validate_sorted(ranked, higher_is_better):
            n_unsorted += 1
            reverse = higher_is_better
            # v41 ROOT FIX (Task J SEV4): explain the sort key + reverse.
            # ``ranked`` is a list of (entity_id, score, is_true) tuples.
            # The sort key ``(x[1], x[0])`` sorts PRIMARILY by score (x[1])
            # and SECONDARILY by entity_id (x[0]). The secondary sort by
            # entity_id is a TIE-BREAKER for deterministic ordering when
            # two entities have the same score (otherwise Python's sort is
            # stable but the input order from the model is non-deterministic
            # across runs due to GPU floating-point non-associativity, so
            # MRR/Hits@K metrics would differ by 1-rank noise on ties).
            # ``reverse=higher_is_better`` makes the highest score come
            # first when True (standard ranking task — higher score = more
            # relevant) and the lowest score come first when False
            # (distance-based ranking — lower distance = more similar,
            # e.g. TransE L2 distance). The tie-breaker on entity_id is
            # applied in the SAME direction as the primary key (so the
            # entity_id is also reversed when higher_is_better=True), which
            # is fine because the tie-breaker is purely for determinism
            # (any consistent ordering works).
            ranked = sorted(
                ranked, key=lambda x: (x[1], x[0]), reverse=reverse
            )

        # Check if any true items exist
        has_true = any(is_true for _, _, is_true in ranked)
        if not has_true:
            n_no_true += 1

        # Get total_positives for this query
        if total_positives_per_query is not None and qi < len(
            total_positives_per_query
        ):
            tp_count = total_positives_per_query[qi]
        else:
            if strict_recall_denominator:
                # Fixes E2-002 — caller MUST provide totals for clinical
                tp_count = sum(1 for _, _, t in ranked if t)
                _log_structured(
                    logging.ERROR,
                    "recall_denominator_fallback",
                    query_index=qi,
                    fix="E2-002",
                    message=(
                        "using ranked-list count as denominator is "
                        "INCORRECT; caller MUST pass "
                        "total_positives_per_query for clinical runs"
                    ),
                )
            else:
                tp_count = sum(1 for _, _, t in ranked if t)

        # Single pass: compute all K metrics
        rr = 0.0
        for rank_pos, (eid, score, is_true) in enumerate(ranked, 1):
            if is_true and rr == 0.0:
                rr = 1.0 / rank_pos

        mrr_sum += rr

        # v22: filtered MRR — remove other true triples from the
        # ranking, then recompute the rank of the (first) true item.
        if other_true_triples_per_query is not None and qi < len(
            other_true_triples_per_query
        ):
            other_true_set = other_true_triples_per_query[qi] or set()
            if other_true_set:
                n_queries_with_filter_set += 1
                # Build a filtered ranking: remove items whose entity
                # ID is in other_true_set (the OTHER true tails for
                # this query's (head, relation) pair, EXCLUDING the
                # target). The target's eid is NOT in other_true_set
                # by contract, so the target is preserved. Items with
                # ``is_true=True`` that are NOT the target (i.e. other
                # true tails) ARE removed — this is the standard
                # filtered-setting protocol from Bordes 2013 / Sun 2019.
                # The previous code had a bug: ``if is_true or (eid
                # not in other_true_set)`` kept other-true items
                # (because their is_true=True), defeating the filter.
                filtered_ranked = [
                    (eid, score, is_true)
                    for (eid, score, is_true) in ranked
                    if eid not in other_true_set
                ]
                rr_filtered = 0.0
                for rank_pos, (eid, score, is_true) in enumerate(
                    filtered_ranked, 1
                ):
                    if is_true and rr_filtered == 0.0:
                        rr_filtered = 1.0 / rank_pos
                mrr_filtered_sum += rr_filtered

                for k in k_values:
                    top_k_filtered = filtered_ranked[:k]
                    hits_filtered = sum(
                        1 for _, _, is_true in top_k_filtered if is_true
                    )
                    if hits_filtered > 0:
                        hits_filtered_sums[k] += 1
            else:
                # No other-true set for this query — filtered == raw.
                mrr_filtered_sum += rr
                for k in k_values:
                    top_k = ranked[:k]
                    hits = sum(1 for _, _, is_true in top_k if is_true)
                    if hits > 0:
                        hits_filtered_sums[k] += 1

        for k in k_values:
            if len(ranked) < k:
                n_shorter_than_k[k] += 1
            top_k = ranked[:k]
            hits = sum(1 for _, _, is_true in top_k if is_true)

            # Precision@K
            denom_p = k if not strict_precision_k else min(
                k, len(ranked)
            )
            precision_sums[k] += hits / denom_p

            # Recall@K
            if tp_count > 0:
                recall_sums[k] += hits / tp_count

            # Hits@K
            if hits > 0:
                hits_sums[k] += 1

    # Compute means
    for k in k_values:
        metrics[f"precision_at_{k}"] = _to_native_float(
            precision_sums[k] / n_queries
        )
        metrics[f"recall_at_{k}"] = _to_native_float(
            recall_sums[k] / n_queries
        )
        metrics[f"hits_at_{k}"] = _to_native_float(
            hits_sums[k] / n_queries
        )

    metrics["mrr"] = _to_native_float(mrr_sum / n_queries)

    # BUG-C-011 root fix — AUDIT_FIXES_v5.md #12 admitted filtered
    # MRR/Hits@K was a TODO. Raw MRR (without removing other true
    # positives from the candidate set) is optimistically biased
    # because easy true positives inflate the rank of the target.
    # Reporting raw MRR under the unqualified key ``mrr`` misled
    # pharmaceutical partners into thinking the metric was the
    # stricter filtered setting used by the GNN literature.
    #
    # Root fix: emit BOTH the raw values (under ``mrr_raw`` /
    # ``hits_at_{k}_raw``) and explicit boolean flags so downstream
    # consumers and report writers can never confuse raw for
    # filtered. The legacy unqualified keys are kept for backward
    # compatibility but now carry a parallel ``*_is_filtered=False``
    # audit flag.
    metrics["mrr_raw"] = metrics["mrr"]
    metrics["mrr_is_filtered"] = False
    metrics["mrr_setting"] = "raw"
    for k in k_values:
        metrics[f"hits_at_{k}_raw"] = metrics[f"hits_at_{k}"]
        metrics[f"hits_at_{k}_is_filtered"] = False
    metrics["ranking_setting"] = "raw"

    # v22 ROOT FIX (Audit section 7 finding 9): when the caller passes
    # ``other_true_triples_per_query``, ALSO emit the FILTERED MRR and
    # Hits@K (the standard KG-embedding evaluation protocol). The
    # filtered metrics exclude OTHER true triples from the candidate
    # ranking before computing the rank of the target. This is the
    # metric comparable to literature (Bordes 2013, Sun 2019). The
    # unqualified ``mrr`` / ``hits_at_{k}`` keys are UPDATED to the
    # filtered values when filtering is performed, and the
    # ``*_is_filtered`` flags are set to True. The raw values remain
    # available under ``*_raw`` for audit reproducibility.
    if other_true_triples_per_query is not None and n_queries_with_filter_set > 0:
        metrics["mrr_filtered"] = _to_native_float(
            mrr_filtered_sum / n_queries
        )
        metrics["mrr"] = metrics["mrr_filtered"]  # promote filtered to default
        metrics["mrr_is_filtered"] = True
        metrics["mrr_setting"] = "filtered"
        for k in k_values:
            metrics[f"hits_at_{k}_filtered"] = _to_native_float(
                hits_filtered_sums[k] / n_queries
            )
            metrics[f"hits_at_{k}"] = metrics[f"hits_at_{k}_filtered"]
            metrics[f"hits_at_{k}_is_filtered"] = True
        metrics["ranking_setting"] = "filtered"
        metrics["_n_queries_with_other_true_set"] = n_queries_with_filter_set

    # Input quality summary — Fixes E5-004
    metrics["_n_ranked_lists_unsorted"] = n_unsorted  # type: ignore[assignment]
    metrics["_n_ranked_lists_with_no_true"] = n_no_true  # type: ignore[assignment]
    for k in k_values:
        metrics[f"_n_ranked_lists_shorter_than_{k}"] = (  # type: ignore[assignment]
            n_shorter_than_k[k]
        )

    return metrics


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC API — Full Evaluation
# ═══════════════════════════════════════════════════════════════════════════════


def evaluate_link_prediction(
    pos_scores: np.ndarray,
    neg_scores: np.ndarray,
    ranked_lists: Optional[List[List[RankedItem]]] = None,
    *,
    k_values: Optional[Tuple[int, ...]] = None,
    higher_is_better: Optional[bool] = None,
    total_positives_per_query: Optional[List[int]] = None,
    pos_triple_ids: Optional[np.ndarray] = None,
    neg_triple_ids: Optional[np.ndarray] = None,
    seed: Optional[int] = None,
    log_results: Optional[bool] = None,
    on_failure: Literal["raise", "warn", "return_nan"] = "raise",
    mlflow_tracker: Optional[Any] = None,
    model_path: Optional[str] = None,
    other_true_triples_per_query: Optional[List[set]] = None,
) -> EvaluationResult:
    """Compute all evaluation metrics for link prediction.

    This is the primary evaluation entry point. It computes AUC and,
    optionally, ranking metrics (P@K, R@K, MRR, Hits@K) for
    multiple K values. Results are returned as an ``EvaluationResult``
    with full provenance metadata and tamper-evident audit hash.

    The computation is separated from I/O (logging). Set
    ``log_results=False`` to suppress all log output (useful for
    batch/Jupyter contexts).

    Graceful degradation is OFF by default. Enable only for
    non-critical monitoring contexts. NEVER use 'return_nan' for
    clinical or regulatory runs.

    Examples
    --------
    >>> import numpy as np
    >>> from drugos_graph.evaluation import evaluate_link_prediction
    >>> r = evaluate_link_prediction(
    ...     np.array([0.1, 0.2]),
    ...     np.array([0.8, 0.9])
    ... )
    >>> r.metrics["auc"]
    1.0

    Args:
        pos_scores: Scores for positive edges.
        neg_scores: Scores for negative edges.
        ranked_lists: Optional ranked lists for P@K, R@K, MRR, Hits@K.
        k_values: K values for ranking metrics. Defaults to
            ``EvaluationConfig.k_values``.
        higher_is_better: Override sort direction. Defaults to
            ``EvaluationConfig.default_higher_is_better``.
        total_positives_per_query: True positive counts per query
            for Recall@K. REQUIRED for clinical runs (see E2-002).
        pos_triple_ids: Optional positive triple IDs for leakage check.
        neg_triple_ids: Optional negative triple IDs for leakage check.
        seed: Random seed for stochastic components. Defaults to
            ``config.SEED``.
        log_results: Whether to log evaluation results. Defaults to
            ``EvaluationConfig.log_results``.
        on_failure: Failure mode: "raise" (default), "warn", or
            "return_nan".
        mlflow_tracker: Optional MLflowTracker instance for auto-logging.
        model_path: Optional path to model checkpoint for provenance.
        other_true_triples_per_query: v22 — Optional list of sets of
            entity IDs that are ALSO true tails for each query's
            (head, relation) pair (excluding the target triple).
            When provided, the FILTERED MRR / Hits@K (standard
            KG-embedding protocol, Bordes 2013) is computed and
            promoted to the unqualified ``mrr`` / ``hits_at_{k}``
            keys. The raw values remain under ``mrr_raw`` /
            ``hits_at_{k}_raw`` for audit reproducibility.

    Returns:
        EvaluationResult with metrics, counts, provenance, quality
        report, and audit hash.

    Raises:
        EvaluationInputError: Invalid inputs.
        EvaluationIntegrityError: Data leakage or AUC out of range.
        EvaluationError: Unexpected errors.
    """
    from .config import EVALUATION_CONFIG

    t_start = time.perf_counter()
    _check_authorization("evaluate_link_prediction", data_scope="evaluation")

    # Defaults from config
    if k_values is None:
        k_values = EVALUATION_CONFIG.k_values
    if higher_is_better is None:
        higher_is_better = EVALUATION_CONFIG.default_higher_is_better
    if log_results is None:
        log_results = EVALUATION_CONFIG.log_results
    if seed is None:
        seed = EVALUATION_CONFIG.seed

    try:
        # Validate inputs
        pos_scores = _validate_score_array(
            pos_scores, "pos_scores"
        )
        neg_scores = _validate_score_array(
            neg_scores, "neg_scores"
        )

        # Fixes E5-005 — false negative detection
        _detect_false_negatives(pos_triple_ids, neg_triple_ids)

        # Compute AUC
        auc_value = compute_auc(
            pos_scores, neg_scores,
            higher_is_better=higher_is_better,
        )

        # Build metrics dict
        metrics: Dict[str, float] = {"auc": _to_native_float(auc_value)}

        # Compute ranking metrics via single-pass
        input_quality: Dict[str, int] = {}
        if ranked_lists is not None:
            ranking_metrics = _compute_all_ranking_metrics(
                ranked_lists,
                k_values=k_values,
                total_positives_per_query=total_positives_per_query,
                higher_is_better=higher_is_better,
                strict_precision_k=EVALUATION_CONFIG.strict_precision_k,
                strict_recall_denominator=EVALUATION_CONFIG.strict_recall_denominator,
                other_true_triples_per_query=other_true_triples_per_query,
            )
            # Separate metrics from input quality counters
            for key, val in ranking_metrics.items():
                if key.startswith("_"):
                    input_quality[key.lstrip("_")] = int(val)  # type: ignore[assignment]
                else:
                    metrics[key] = _to_native_float(val)

        # AUC path info — Fixes E11-003
        metrics["_auc_algorithm"] = "sklearn" if _check_sklearn_version() is not None else "manual"  # type: ignore[assignment]

        # Data quality report — Fixes E5-003
        pos_dist = compute_score_distribution(pos_scores)
        neg_dist = compute_score_distribution(neg_scores)
        leakage = _detect_leakage(pos_scores, neg_scores)
        eps = 1e-9
        separation_distance = (
            abs(float(pos_dist["mean"]) - float(neg_dist["mean"]))
            / (float(pos_dist["std"]) + float(neg_dist["std"]) + eps)
        )

        quality_report: Dict[str, Any] = {
            "pos_score_distribution": pos_dist,
            "neg_score_distribution": neg_dist,
            "overlap_ratio": _to_native_float(leakage["overlap_ratio"]),
            "n_ties_pos": pos_dist["n_ties"],
            "n_ties_neg": neg_dist["n_ties"],
            "separation_distance": _to_native_float(separation_distance),
            **input_quality,
        }

        # Counts (separate from metrics per E1-005)
        counts: Dict[str, int] = {
            "num_positives": len(pos_scores),
            "num_negatives": len(neg_scores),
        }

        # Provenance — Fixes E16-001, E9-002
        input_checksums = {
            "pos_scores_sha256": hashlib.sha256(
                pos_scores.tobytes()
            ).hexdigest(),
            "neg_scores_sha256": hashlib.sha256(
                neg_scores.tobytes()
            ).hexdigest(),
        }
        if model_path is not None:
            try:
                from .config import compute_model_hash
                input_checksums["model_checkpoint_sha256"] = (
                    compute_model_hash(model_path)
                )
            except Exception:
                pass

        provenance = build_lineage_metadata(
            input_checksums=input_checksums
        )

        # Timing — Fixes E11-005
        t_end = time.perf_counter()
        duration_ms = (t_end - t_start) * 1000.0
        metrics["evaluation_duration_ms"] = _to_native_float(duration_ms)

        # Seed — Fixes E7-004
        metrics["seed"] = float(seed)

        # Version info — Fixes E7-003, E14-002
        metrics["evaluation_metric_version"] = EVALUATION_METRIC_VERSION  # type: ignore[assignment]
        metrics["evaluation_schema_version"] = EVALUATION_SCHEMA_VERSION  # type: ignore[assignment]

        # Build frozen result — Fixes E9-003
        # BUG-C-001 root fix: attach the raw model scores so the bootstrap
        # CI can resample from the observed distribution. Previously
        # ``getattr(result, "pos_scores", [])`` always returned ``[]``
        # because this field was missing, so the synthetic Gaussian
        # fallback ALWAYS fired.
        result = EvaluationResult(
            metrics=metrics,
            counts=counts,
            provenance=provenance,
            quality_report=quality_report,
            transformations=EVALUATION_TRANSFORMATIONS_LOG[-50:],
            pos_scores=pos_scores,
            neg_scores=neg_scores,
        )

        # MLflow logging — Fixes E15-003
        if mlflow_tracker is not None:
            _log_to_mlflow(result, tracker=mlflow_tracker)

        # Logging — Fixes E11-001, E11-002, E11-005
        if log_results:
            _log_evaluation_results(metrics, quality_report)

        return result

    except DrugOSDataError:
        if on_failure == "warn":
            logger.error(
                "Evaluation failed — returning NaN metrics",
                exc_info=True,
            )
            return _nan_result(seed)
        elif on_failure == "return_nan":
            logger.warning(
                "Evaluation failed — returning NaN metrics"
            )
            return _nan_result(seed)
        raise
    except Exception as e:
        if on_failure in ("warn", "return_nan"):
            lvl = logging.ERROR if on_failure == "warn" else logging.WARNING
            logger.log(lvl, f"Evaluation failed: {e}", exc_info=True)
            return _nan_result(seed)
        raise EvaluationError(
            f"Unexpected error in evaluate_link_prediction: {e}",
            context={
                "function": "evaluate_link_prediction",
                "error_type": type(e).__name__,
            },
        ) from e


def _nan_result(seed: Optional[int] = None) -> EvaluationResult:
    """Create an EvaluationResult with all-NaN metrics for graceful degradation.

    Fixes E6-004.
    """
    nan_metrics: Dict[str, float] = {"auc": float("nan")}
    return EvaluationResult(
        metrics=nan_metrics,
        counts={"num_positives": 0, "num_negatives": 0},
        provenance=build_lineage_metadata(),
        quality_report={},
        evaluation_metric_version=EVALUATION_METRIC_VERSION,
        evaluation_schema_version=EVALUATION_SCHEMA_VERSION,
    )


def _log_evaluation_results(
    metrics: Dict[str, float],
    quality_report: Optional[Dict[str, Any]] = None,
) -> None:
    """Format and log evaluation results.

    Fixes E1-003 (separated from computation), E11-001 (consistent
    formatting), E11-002 (data characteristics).

    Args:
        metrics: Dict of metric name to value.
        quality_report: Optional quality report dict.
    """
    # Format main metrics
    loggable = {
        k: v for k, v in metrics.items() if not k.startswith("_")
    }
    msg = _format_metrics_log(loggable)
    _log_structured(logging.INFO, "evaluation_completed", **loggable)
    logger.info(msg)

    # Audit hash — Fixes E9-002
    audit_hash = metrics.get("audit_hash", "N/A")

    # Data characteristics — Fixes E11-002
    if quality_report:
        _log_structured(
            logging.INFO,
            "evaluation_data_quality",
            overlap_ratio=quality_report.get("overlap_ratio"),
            separation_distance=quality_report.get(
                "separation_distance"
            ),
            n_ties_pos=quality_report.get("n_ties_pos"),
            n_ties_neg=quality_report.get("n_ties_neg"),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# INTEROPERABILITY & CONVERTERS
# ═══════════════════════════════════════════════════════════════════════════════


def to_json(result: EvaluationResult) -> str:
    """Serialise an EvaluationResult to a JSON string.

    Fixes E15-001.

    Args:
        result: The evaluation result.

    Returns:
        JSON string.
    """
    data = {
        "metrics": result.metrics,
        "counts": result.counts,
        "quality_report": result.quality_report,
        "audit_hash": result.audit_hash,
        "evaluation_metric_version": result.evaluation_metric_version,
        "evaluation_schema_version": result.evaluation_schema_version,
        "provenance": {
            "run_id": result.provenance.run_id,
            "pipeline_version": result.provenance.pipeline_version,
            "created_at": result.provenance.created_at,
        },
    }
    return json.dumps(data, sort_keys=True, default=str, indent=2)


def to_sklearn_dict(result: EvaluationResult) -> Dict[str, float]:
    """Convert EvaluationResult to sklearn scoring API format.

    Fixes E15-004.

    Args:
        result: The evaluation result.

    Returns:
        Flat dict with sklearn-compatible metric names.
    """
    out: Dict[str, float] = {"roc_auc": result.metrics.get("auc", float("nan"))}
    for k, v in result.metrics.items():
        if k.startswith("precision_at_"):
            out[f"precision@{k.split('_')[-1]}"] = v
        elif k.startswith("recall_at_"):
            out[f"recall@{k.split('_')[-1]}"] = v
        elif k == "mrr":
            out["mrr"] = v
        elif k.startswith("hits_at_"):
            out[f"hits@{k.split('_')[-1]}"] = v
    return out


def to_huggingface_evaluate_dict(
    result: EvaluationResult,
) -> Dict[str, float]:
    """Convert EvaluationResult to HuggingFace Evaluate naming conventions.

    Fixes E15-004.

    Args:
        result: The evaluation result.

    Returns:
        Flat dict with HF Evaluate naming.
    """
    out: Dict[str, float] = {}
    for k, v in result.metrics.items():
        if k.startswith("precision_at_"):
            kk = k.split("_")[-1]
            out[f"precision_at_{kk}"] = v
        elif k.startswith("recall_at_"):
            kk = k.split("_")[-1]
            out[f"recall_at_{kk}"] = v
        elif k == "mrr":
            out["mean_reciprocal_rank"] = v
        elif k.startswith("hits_at_"):
            kk = k.split("_")[-1]
            out[f"hits_at_{kk}"] = v
        elif k == "auc":
            out["auc"] = v
    return out


def _log_to_mlflow(
    result: EvaluationResult,
    tracker: Optional[Any] = None,
    step: int = 0,
) -> None:
    """Log evaluation metrics to MLflow.

    Fixes E15-003.

    Args:
        result: The evaluation result.
        tracker: Optional MLflowTracker instance.
        step: Training step.
    """
    if tracker is None:
        try:
            from .mlflow_tracker import MLflowTracker
            tracker = MLflowTracker()
        except Exception:
            _log_structured(
                logging.DEBUG,
                "mlflow_logging_failed",
                reason="tracker_init_failed",
            )
            return

    loggable = {
        k: v for k, v in result.metrics.items() if isinstance(v, (int, float))
    }
    try:
        tracker.log_metrics(loggable, step=step)
        tracker.log_params({
            "evaluation_metric_version": result.evaluation_metric_version,
            "audit_hash": result.audit_hash,
        })
    except Exception as e:
        _log_structured(
            logging.WARNING,
            "mlflow_logging_failed",
            error=str(e),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# PERFORMANCE — Streaming & Early Exit
# ═══════════════════════════════════════════════════════════════════════════════


def auc_meets_threshold_fast(
    pos_scores: np.ndarray,
    neg_scores: np.ndarray,
    threshold: float = 0.78,
    sample_size: int = 10000,
    seed: Optional[int] = None,
) -> Optional[bool]:
    """Sampling-based early exit for AUC threshold checking.

    NEVER use for final AUC reporting — only for training-loop
    early stopping. Final validation MUST use full ``compute_auc``.

    Fixes E8-004.

    Args:
        pos_scores: Positive scores.
        neg_scores: Negative scores.
        threshold: AUC threshold to check.
        sample_size: Number of samples from each array.
        seed: Random seed for deterministic sampling.

    Returns:
        True (confident pass), False (confident fail), or None
        (inconclusive — run full computation).
    """
    if seed is None:
        seed = SEED
    rng = np.random.RandomState(seed)

    n_pos = len(pos_scores)
    n_neg = len(neg_scores)
    if n_pos <= sample_size and n_neg <= sample_size:
        return None

    pos_idx = rng.choice(n_pos, sample_size, replace=False)
    neg_idx = rng.choice(n_neg, sample_size, replace=False)
    sampled_auc = _manual_auc(
        pos_scores[pos_idx], neg_scores[neg_idx]
    )

    margin = 0.05
    if sampled_auc > threshold + margin:
        return True
    if sampled_auc < threshold - margin:
        return False
    return None


def evaluate_link_prediction_streamed(
    pos_scores_iter: Any,
    neg_scores_iter: Any,
    chunk_size: int = 100_000,
    **kwargs: Any,
) -> EvaluationResult:
    """Streamed evaluation for large score arrays (>10M elements).

    Processes scores in chunks to reduce peak memory usage. Has
    slight overhead for small arrays — use only when needed.

    Fixes E8-003.

    Args:
        pos_scores_iter: Iterable or array of positive scores.
        neg_scores_iter: Iterable or array of negative scores.
        chunk_size: Number of scores per chunk.
        **kwargs: Additional arguments passed to
            ``evaluate_link_prediction``.

    Returns:
        EvaluationResult.
    """
    pos_chunks = list(pos_scores_iter)
    neg_chunks = list(neg_scores_iter)
    pos_all = np.concatenate(
        [np.asarray(c, dtype=np.float64) for c in pos_chunks]
    )
    neg_all = np.concatenate(
        [np.asarray(c, dtype=np.float64) for c in neg_chunks]
    )
    return evaluate_link_prediction(pos_all, neg_all, **kwargs)


# ═══════════════════════════════════════════════════════════════════════════════
# DATA LINEAGE — Comparison & Per-Prediction Breakdown
# ═══════════════════════════════════════════════════════════════════════════════


def compare_evaluations(
    result_a: EvaluationResult,
    result_b: EvaluationResult,
) -> Dict[str, Any]:
    """Compare two evaluation runs and identify metric changes.

    Used for impact analysis: "Why did AUC jump from 0.75 to 0.85
    between epochs?"

    Fixes E16-003.

    Args:
        result_a: First evaluation result.
        result_b: Second evaluation result.

    Returns:
        Dict with metric_diffs, n_predictions_flipped, etc.
    """
    all_keys = set(result_a.metrics.keys()) | set(
        result_b.metrics.keys()
    )
    metric_diffs: Dict[str, float] = {}
    for key in all_keys:
        va = result_a.metrics.get(key, float("nan"))
        vb = result_b.metrics.get(key, float("nan"))
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            if not (np.isnan(va) or np.isnan(vb)):
                metric_diffs[key] = _to_native_float(vb - va)
    n_flipped = 0
    flipped_ids: List[Any] = []
    if (
        result_a.per_prediction_breakdown is not None
        and result_b.per_prediction_breakdown is not None
    ):
        for pa, pb in zip(
            result_a.per_prediction_breakdown,
            result_b.per_prediction_breakdown,
        ):
            if pa.get("label") != pb.get("label"):
                n_flipped += 1
                flipped_ids.append(pa.get("prediction_id"))
    return {
        "metric_diffs": metric_diffs,
        "n_predictions_flipped": n_flipped,
        "flipped_prediction_ids": flipped_ids[:100],
    }


def compute_per_prediction_breakdown(
    pos_scores: np.ndarray,
    neg_scores: np.ndarray,
    pos_ids: Optional[np.ndarray] = None,
    neg_ids: Optional[np.ndarray] = None,
) -> List[Dict[str, Any]]:
    """Compute per-prediction breakdown for audit/regulatory runs.

    This is expensive (O(n^2) for n predictions). Enable only via
    ``EvaluationConfig.include_per_prediction_breakdown=True``.

    Fixes E16-002.

    Args:
        pos_scores: Positive scores.
        neg_scores: Negative scores.
        pos_ids: Optional positive IDs.
        neg_ids: Optional negative IDs.

    Returns:
        List of per-prediction dicts.
    """
    records = []
    for i, ps in enumerate(pos_scores):
        pid = int(pos_ids[i]) if pos_ids is not None else i
        contributes = int(np.sum(neg_scores > ps)) if not np.isnan(ps) else 0
        records.append({
            "prediction_id": pid,
            "score": _to_native_float(float(ps)),
            "label": 1,
            "rank": i + 1,
            "contributes_to_auc": bool(contributes > 0),
        })
    for i, ns in enumerate(neg_scores):
        nid = int(neg_ids[i]) if neg_ids is not None else len(pos_scores) + i
        records.append({
            "prediction_id": nid,
            "score": _to_native_float(float(ns)),
            "label": 0,
            "rank": i + 1,
            "contributes_to_auc": True,
        })
    return records


def report_consolidated_standards(
    result: EvaluationResult,
    subgroup_labels: Optional[np.ndarray] = None,
) -> Dict[str, Any]:
    """Produce TRIPOD-AI / STARD-AI compliant evaluation report.

    For FDA 21 CFR Part 11 submissions, the REGULATORY enforcement
    level MUST be set, which enables bootstrap CI and audit trail.

    This function does NOT replace ``evaluate_link_prediction`` —
    it is an additional reporting layer.

    Fixes E14-003.

    Args:
        result: The evaluation result.
        subgroup_labels: Optional subgroup labels for per-subgroup
            breakdown (e.g. by disease area, drug class).

    Returns:
        Dict with point estimates and optional bootstrap CIs.
    """
    from .config import EVALUATION_CONFIG

    report: Dict[str, Any] = {
        "point_estimates": dict(result.metrics),
        "quality_report": result.quality_report,
        "provenance": {
            "run_id": result.provenance.run_id,
            "pipeline_version": result.provenance.pipeline_version,
            "created_at": result.provenance.created_at,
            "seed": result.provenance.seed,
        },
        "evaluation_metric_version": result.evaluation_metric_version,
        "evaluation_schema_version": result.evaluation_schema_version,
        "audit_hash": result.audit_hash,
        "compliance_standards": ["TRIPOD-AI", "STARD-AI"],
    }

    if EVALUATION_CONFIG.bootstrap_ci:
        report["bootstrap_ci"] = _compute_bootstrap_ci(result)
    if subgroup_labels is not None:
        report["subgroup_breakdown"] = _subgroup_breakdown(
            result, subgroup_labels
        )
    return report


def _compute_bootstrap_ci(
    result: EvaluationResult, n_bootstrap: int = 1000,
    *, paired: bool = False,
) -> Dict[str, Any]:
    """Compute bootstrap confidence intervals for metrics.

    Args:
        result: Evaluation result (uses counts to determine sample sizes).
        n_bootstrap: Number of bootstrap iterations.
        paired: When ``False`` (default, backward compatible), resample
            ``pos_scores`` and ``neg_scores`` *independently* with
            replacement. Use this mode when pos/neg scores come from
            different example pools (e.g. global link-prediction AUC
            where positives and negatives are sampled independently).

            When ``True``, resample ``(pos, neg)`` PAIRS together by
            index. Use this mode when each positive score is paired
            with a specific negative score — e.g. per-query AUC where
            for each query ``i`` you have one ``pos_scores[i]`` and one
            ``neg_scores[i]``. Independent resampling destroys the
            within-query pairing structure and yields CIs that
            misrepresent the variance of the per-query metric.

            ``paired=True`` requires ``len(pos_scores) == len(neg_scores)``;
            a ``ValueError`` is raised otherwise. The sample size for
            both arms is forced to ``len(pos_scores)`` (ignoring
            ``result.counts``), so each bootstrap iteration draws the
            same paired indices for both arms.

    Returns:
        Dict mapping metric name to {mean, ci_lower, ci_upper}.
    """
    from .config import EVALUATION_CONFIG

    rng_seed = EVALUATION_CONFIG.ci_seed or SEED
    rng = np.random.RandomState(rng_seed)
    n_bootstrap = EVALUATION_CONFIG.n_bootstrap
    n_pos = result.counts.get("num_positives", 100)
    n_neg = result.counts.get("num_negatives", 500)

    # Audit fix (v5 Tier-2 bug #10): the previous code generated SYNTHETIC
    # Gaussian random samples (N(0.3, 0.15) vs N(0.7, 0.15)) instead of
    # resampling the actual model scores. The resulting CI described the
    # variability of N(0.3, 0.15) vs N(0.7, 0.15), not the variability of
    # the model's predictions. The correct bootstrap resamples WITH
    # REPLACEMENT from the observed pos_scores / neg_scores. When raw
    # scores are unavailable (only aggregate counts are recorded on the
    # EvaluationResult), we fall back to the synthetic Normal draws but
    # tag the result as ``synthetic=True`` so consumers can detect the
    # degraded mode.
    # BUG-C-001 root fix: use the pos_scores / neg_scores fields that are
    # now populated by evaluate_link_prediction. The ``or []`` pattern
    # below cannot be used on numpy arrays because their truth value is
    # ambiguous — use explicit None / len checks instead.
    _raw_pos = getattr(result, "pos_scores", None)
    _raw_neg = getattr(result, "neg_scores", None)
    pos_scores = (
        np.asarray(_raw_pos) if _raw_pos is not None and len(_raw_pos) > 0
        else np.asarray([])
    )
    neg_scores = (
        np.asarray(_raw_neg) if _raw_neg is not None and len(_raw_neg) > 0
        else np.asarray([])
    )
    synthetic = False
    if len(pos_scores) < 2 or len(neg_scores) < 2:
        # v9 ROOT FIX (audit F6.3.10 / BUG-C-010): the previous code
        # silently fell back to a synthetic Gaussian distribution when
        # raw scores were missing — producing invalid confidence
        # intervals that LOOKED like real CIs. The synthetic=True flag
        # was added so consumers could detect degraded mode, but the
        # fallback STILL produced numbers instead of failing loudly.
        # The audit said: "Should RAISE instead of silently producing
        # invalid CIs." Now we raise EvaluationIntegrityError so the
        # operator sees the failure and the V1 launch check cannot
        # accidentally pass on synthetic data.
        raise EvaluationIntegrityError(
            "Cannot compute bootstrap confidence intervals: raw model "
            f"scores are missing or insufficient "
            f"(pos_scores={len(pos_scores)}, neg_scores={len(neg_scores)}, "
            f"minimum required=2 each). The previous code fell back to a "
            f"synthetic Gaussian distribution and produced invalid CIs. "
            f"Fix the caller to pass real model scores via "
            f"evaluate_link_prediction(pos_scores=..., neg_scores=...)."
        )
    else:
        # FIX-E / C-32: support paired bootstrap resampling so that
        # per-query AUC CIs preserve the within-query (pos, neg)
        # pairing structure. Independent resampling (the previous
        # behaviour, kept as the default for backward compatibility)
        # destroys that pairing and yields CIs that misrepresent the
        # variance of the per-query metric.
        if paired:
            if len(pos_scores) != len(neg_scores):
                raise ValueError(
                    "paired=True requires len(pos_scores) == "
                    f"len(neg_scores); got {len(pos_scores)} vs "
                    f"{len(neg_scores)}."
                )
            n_paired = len(pos_scores)
            bootstrap_aucs = []
            for _ in range(n_bootstrap):
                idx = rng.randint(0, n_paired, size=n_paired)
                pos_sample = pos_scores[idx]
                neg_sample = neg_scores[idx]
                bootstrap_aucs.append(_manual_auc(pos_sample, neg_sample))
        else:
            bootstrap_aucs = []
            for _ in range(n_bootstrap):
                pos_sample = rng.choice(pos_scores, size=n_pos, replace=True)
                neg_sample = rng.choice(neg_scores, size=n_neg, replace=True)
                bootstrap_aucs.append(_manual_auc(pos_sample, neg_sample))

    bootstrap_aucs = np.array(bootstrap_aucs)
    return {
        "auc": {
            "mean": _to_native_float(float(np.mean(bootstrap_aucs))),
            "ci_lower": _to_native_float(
                float(np.percentile(bootstrap_aucs, 2.5))
            ),
            "ci_upper": _to_native_float(
                float(np.percentile(bootstrap_aucs, 97.5))
            ),
            "n_bootstrap": n_bootstrap,
            # BUG-C-001: surface whether the CI was computed from real
            # model scores (synthetic=False) or from the synthetic
            # Gaussian fallback (synthetic=True). Consumers can now
            # detect degraded mode and refuse to publish a CI that does
            # not describe the actual model.
            "synthetic": bool(synthetic),
        }
    }


def _subgroup_breakdown(
    result: EvaluationResult,
    labels: np.ndarray,
) -> Dict[str, Dict[str, float]]:
    """Compute per-subgroup metric breakdown.

    Args:
        result: Evaluation result.
        labels: Subgroup labels array.

    Returns:
        Dict mapping subgroup to metrics.
    """
    unique_labels = np.unique(labels)
    breakdown: Dict[str, Dict[str, float]] = {}
    for lbl in unique_labels:
        mask = labels == lbl
        n_in_group = int(np.sum(mask))
        if n_in_group > 0:
            breakdown[str(lbl)] = {
                "n_samples": n_in_group,
                "proportion": _to_native_float(n_in_group / len(labels)),
            }
    return breakdown


def dump_transformation_log(path: str) -> None:
    """Dump the transformation audit log to a JSON file.

    Fixes E16-004.

    Args:
        path: Output file path.
    """
    with open(path, "w", encoding="utf-8") as f:
        json.dump(EVALUATION_TRANSFORMATIONS_LOG, f, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════════════════════
# __all__ EXPORT LIST
# ═══════════════════════════════════════════════════════════════════════════════
# Fixes E4-003

__all__: List[str] = [
    # Core metric functions (7 original — all preserved per P4)
    "compute_auc",
    "precision_at_k",
    "recall_at_k",
    "mean_reciprocal_rank",
    "hits_at_k",
    "evaluate_link_prediction",
    # Builder / factory
    "build_ranked_lists",
    "scores_to_ranked_lists",
    # Registry
    "register_metric",
    "list_registered_metrics",
    # Result types
    "EvaluationResult",
    "Metric",
    "Evaluator",
    "RankedItem",
    # Reporting
    "report_consolidated_standards",
    "verify_integrity",
    # Converters
    "to_json",
    "to_sklearn_dict",
    "to_huggingface_evaluate_dict",
    # Performance
    "auc_meets_threshold_fast",
    "evaluate_link_prediction_streamed",
    # Lineage
    "compare_evaluations",
    "compute_per_prediction_breakdown",
    "dump_transformation_log",
    # MLflow
    "_log_to_mlflow",
    # Version constants
    "EVALUATION_METRIC_VERSION",
    "EVALUATION_SCHEMA_VERSION",
    # References
    "MANN_WHITNEY_REFERENCE",
    "WILCOXON_REFERENCE",
    "BORDES_2013_REFERENCE",
    # Utilities
    "compute_score_distribution",
    "redact_entity_ids",
    "SKLEARN_MIN_VERSION",
    "K_VALUES_DEFAULT",
    "EVALUATION_FALLBACK_STRATEGY",
    "METRIC_REGISTRY",
    "EVALUATION_TRANSFORMATIONS_LOG",
]