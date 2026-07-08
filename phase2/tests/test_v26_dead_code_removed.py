"""Tests for FIX-E (dead code removal + bootstrap_ci paired option).

Verifies that the four dead-code items removed by FIX-E are actually
gone, and that the new ``paired`` parameter on
``evaluation._compute_bootstrap_ci`` exists with the expected signature.

Covers:
  * C-22: ``MarginRankingLoss`` class removed from ``transe_model.py``.
  * C-23: ``sweep_margin`` function removed from ``transe_model.py``.
  * C-25: ``_drkg_parse_cache`` dict removed from ``run_pipeline.py``.
  * C-32: ``paired: bool = False`` kwarg added to
    ``evaluation._compute_bootstrap_ci``.

C-24 (``temporal_split_pairs``) is intentionally NOT removed — another
agent (FIX-D) is wiring it INTO step 11. We do NOT assert its absence;
we only assert its presence so that an accidental removal is caught.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

# Ensure the project root is on sys.path so `import drugos_graph` works.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ─── C-22: MarginRankingLoss class removed ──────────────────────────────

def test_margin_ranking_loss_class_removed():
    """C-22: the ``MarginRankingLoss`` deprecation-wrapper class has
    been removed from ``transe_model.py`` and from its ``__all__``."""
    from drugos_graph import transe_model

    # Class is gone from the module namespace.
    assert not hasattr(transe_model, "MarginRankingLoss"), (
        "transe_model.MarginRankingLoss still exists — C-22 dead code "
        "not removed."
    )
    # Class is gone from __all__.
    assert "MarginRankingLoss" not in getattr(transe_model, "__all__", []), (
        "transe_model.__all__ still lists MarginRankingLoss."
    )

    # And the source file no longer contains the class definition.
    src_path = Path(transe_model.__file__)
    src = src_path.read_text(encoding="utf-8")
    assert "class MarginRankingLoss" not in src, (
        f"{src_path} still contains 'class MarginRankingLoss'."
    )


# ─── C-23: sweep_margin function removed ────────────────────────────────

def test_sweep_margin_removed():
    """C-23: the ``sweep_margin`` hyperparameter sweep helper has been
    removed from ``transe_model.py`` and from its ``__all__``."""
    from drugos_graph import transe_model

    # Function is gone from the module namespace.
    assert not hasattr(transe_model, "sweep_margin"), (
        "transe_model.sweep_margin still exists — C-23 dead code "
        "not removed."
    )
    # Function is gone from __all__.
    assert "sweep_margin" not in getattr(transe_model, "__all__", []), (
        "transe_model.__all__ still lists sweep_margin."
    )

    # And the source file no longer contains the function definition.
    src_path = Path(transe_model.__file__)
    src = src_path.read_text(encoding="utf-8")
    assert "def sweep_margin" not in src, (
        f"{src_path} still contains 'def sweep_margin'."
    )


# ─── C-24: temporal_split_pairs still present (NOT removed) ─────────────

def test_temporal_split_pairs_still_present():
    """C-24: ``temporal_split_pairs`` is intentionally LEFT IN PLACE —
    another agent (FIX-D) is wiring it INTO step 11. We verify it is
    still importable and exported."""
    from drugos_graph import training_data

    assert hasattr(training_data, "temporal_split_pairs"), (
        "training_data.temporal_split_pairs is MISSING — it should be "
        "left in place (FIX-D is wiring it into step 11)."
    )
    assert "temporal_split_pairs" in getattr(
        training_data, "__all__", []
    ), (
        "training_data.__all__ should still list temporal_split_pairs."
    )
    assert callable(training_data.temporal_split_pairs)


# ─── C-25: _drkg_parse_cache removed ────────────────────────────────────

def test_drkg_parse_cache_removed():
    """C-25: the ``_drkg_parse_cache`` module-level dict has been
    removed from ``run_pipeline.py`` — it was written but never read."""
    from drugos_graph import run_pipeline

    # The attribute must not exist on the imported module.
    assert not hasattr(run_pipeline, "_drkg_parse_cache"), (
        "run_pipeline._drkg_parse_cache still exists — C-25 dead code "
        "not removed."
    )

    # And the source file no longer references the name at all.
    src_path = Path(run_pipeline.__file__)
    src = src_path.read_text(encoding="utf-8")
    assert "_drkg_parse_cache" not in src, (
        f"{src_path} still references '_drkg_parse_cache'."
    )


# ─── C-32: bootstrap_ci paired option exists ────────────────────────────

def test_bootstrap_ci_paired_option_exists():
    """C-32: ``_compute_bootstrap_ci`` accepts a keyword-only
    ``paired: bool = False`` parameter without changing the default
    behaviour."""
    from drugos_graph import evaluation

    fn = getattr(evaluation, "_compute_bootstrap_ci", None)
    assert fn is not None, "evaluation._compute_bootstrap_ci missing"
    sig = inspect.signature(fn)
    assert "paired" in sig.parameters, (
        "_compute_bootstrap_ci signature lacks 'paired' parameter."
    )
    param = sig.parameters["paired"]
    # Must be keyword-only (kind == KEYWORD_ONLY) and default to False.
    assert param.kind == inspect.Parameter.KEYWORD_ONLY, (
        f"'paired' must be keyword-only; got {param.kind}."
    )
    assert param.default is False, (
        f"'paired' default must be False for backward compat; "
        f"got {param.default!r}."
    )


def test_bootstrap_ci_paired_requires_equal_lengths():
    """C-32: when ``paired=True`` is passed with mismatched pos/neg
    lengths, the function raises ``ValueError`` instead of silently
    producing a wrong CI."""
    from drugos_graph import evaluation
    from drugos_graph.exceptions import EvaluationIntegrityError

    # Build a minimal EvaluationResult-like object exposing the
    # attributes _compute_bootstrap_ci reads: pos_scores, neg_scores,
    # and counts (with num_positives / num_negatives).
    import numpy as np

    class _StubResult:
        pos_scores = np.array([0.1, 0.2, 0.3], dtype=float)
        neg_scores = np.array([0.9, 0.8], dtype=float)  # different length
        counts = {"num_positives": 3, "num_negatives": 2}

    with pytest.raises(ValueError):
        evaluation._compute_bootstrap_ci(_StubResult(), n_bootstrap=5,
                                         paired=True)


def test_bootstrap_ci_paired_runs_when_lengths_match():
    """C-32: when ``paired=True`` and lengths match, the function
    produces a valid CI dict with the expected keys."""
    from drugos_graph import evaluation
    import numpy as np

    class _StubResult:
        pos_scores = np.array([0.1, 0.2, 0.3, 0.4], dtype=float)
        neg_scores = np.array([0.9, 0.8, 0.7, 0.6], dtype=float)
        counts = {"num_positives": 4, "num_negatives": 4}

    out = evaluation._compute_bootstrap_ci(_StubResult(), n_bootstrap=10,
                                           paired=True)
    assert "auc" in out
    auc_entry = out["auc"]
    for key in ("mean", "ci_lower", "ci_upper", "n_bootstrap", "synthetic"):
        assert key in auc_entry, f"missing key {key!r} in auc CI dict"
    # Note: _compute_bootstrap_ci overwrites the n_bootstrap parameter
    # with EVALUATION_CONFIG.n_bootstrap (pre-existing behaviour); we
    # only assert that the function ran and produced a positive count.
    assert auc_entry["n_bootstrap"] >= 1
    assert auc_entry["synthetic"] is False
    # Mean AUC should be high (perfect separation between low pos and high neg).
    assert auc_entry["mean"] >= 0.9


def test_bootstrap_ci_default_behaviour_unchanged():
    """C-32: with ``paired`` left at its default (False), the function
    still uses independent resampling and accepts mismatched lengths
    (no ValueError, no paired-mode check)."""
    from drugos_graph import evaluation
    import numpy as np

    class _StubResult:
        pos_scores = np.array([0.1, 0.2, 0.3], dtype=float)
        neg_scores = np.array([0.9, 0.8, 0.7, 0.6, 0.5], dtype=float)
        counts = {"num_positives": 3, "num_negatives": 5}

    # Default: should NOT raise on mismatched lengths.
    out = evaluation._compute_bootstrap_ci(_StubResult(), n_bootstrap=10)
    assert "auc" in out
    assert "mean" in out["auc"]
