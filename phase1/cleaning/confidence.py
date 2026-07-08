"""Confidence-tier classification for gene–disease association scores.

This module is the SINGLE source of truth for confidence-tier thresholds
across the platform.  It is consumed by the DisGeNET pipeline
(``pipelines/disgenet_pipeline.py``) and may be consumed by the OMIM
pipeline and downstream consumers (Graph Transformer feature loader).

Scientific basis
----------------
The default tiers follow Piñero et al., 2020, *DisGeNET: a comprehensive
platform integrating information on human disease-associated genes and
variants*, Nucleic Acids Research (https://doi.org/10.1093/nar/gkz1021).
Per §2.3 of the publication, the DisGeNET Disease-Specific Genomic Profile
(DSGP) score bands are:

- ``[0.0, 0.06)``   — sub-weak (below the published weak-evidence floor)
- ``[0.06, 0.3)``   — weak evidence
- ``[0.3, 1.0]``    — strong evidence

The previous ``0.7 → "very_high"`` tier is REMOVED — no publication
supports it.  The previous ``0.0 → "low"``, ``0.1 → "medium"``,
``0.3 → "high"`` tiers are REPLACED by the publication-aligned tiers
above.

Design
------
- The function :func:`classify_confidence` uses :func:`bisect.bisect_right`
  on the thresholds for O(log k) classification (DES-3).  This is faster
  than a linear scan and trivially supports arbitrary numbers of tiers.
- Tier thresholds are configurable at runtime via the ``tiers`` parameter.
  The DisGeNET pipeline passes the parsed ``DISGENET_CONFIDENCE_TIERS``
  list from ``config/settings.py``.
- A defensive assertion fires if the score is NaN or negative — these
  should never reach the classifier (validate_gda_scores clips first).
"""

from __future__ import annotations

import bisect
import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default confidence tiers — publication-aligned (Piñero et al. 2020).
# ---------------------------------------------------------------------------
DEFAULT_CONFIDENCE_TIERS: list[tuple[float, str]] = [
    # v41 ROOT FIX (S1 SCIENTIFIC): align tier LABELS with the
    # rationale text above and with Piñero et al. 2020 §2.3.
    # Previously the [0.06, 0.3) band was labelled "moderate" while
    # the publication and the module docstring both call the same
    # band "weak evidence" — a contradiction that could prioritise
    # false-positive drug-disease associations over genuinely
    # moderate evidence from other sources. Now:
    #   [0.0,  0.06) → "sub_weak"  (below DisGeNET's weak-evidence floor)
    #   [0.06, 0.3)  → "weak"      (Piñero et al. 2020 §2.3)
    #   [0.3,  1.0]  → "strong"    (Piñero et al. 2020 §2.3)
    (0.0, "sub_weak"),   # [0.0,  0.06) — below DisGeNET's weak-evidence floor
    (0.06, "weak"),      # [0.06, 0.3)  — weak evidence (Piñero et al. 2020)
    (0.3, "strong"),     # [0.3,  1.0]  — strong evidence (Piñero et al. 2020)
]
"""Default confidence-tier thresholds (Piñero et al. 2020).

A list of ``(threshold, label)`` pairs, sorted ascending by threshold.
The first tier whose ``threshold <= score`` (and which is below the next
tier's threshold) wins.  ``score = 0.0`` always falls in the first tier
(``"sub_weak"``).
"""

# v43 ROOT FIX (P1-033): OMIM-specific confidence tiers.
# The DEFAULT_CONFIDENCE_TIERS above is designed for DisGeNET DSGP scores
# (continuous [0, 1] with known distribution). OMIM scores are derived
# from mapping_key (1-4) via _OMIM_CATEGORICAL_MAP:
#   mk=1 → 0.5  (provisional — "disease-causing mutation" not yet confirmed)
#   mk=2 → 0.6  (phenotype mapped — molecular basis unknown)
#   mk=3 → 0.9  (strongest — "molecular basis known")
#   mk=4 → 0.8  (contiguous gene syndrome — deletion/duplication spanning
#                multiple genes, e.g. DiGeorge, Williams)
#
# ROOT FIX (Findings 5 & 6, P1): the previous labels were
#   "omim_provisional"  (mk=1) — correct
#   "omim_confirmed"    (mk=2) — WRONG: mk=2 is "phenotype mapped",
#                                 molecular basis UNKNOWN. mk=3 is the
#                                 actual "confirmed/molecular basis known"
#                                 tier. The label "confirmed" misled the
#                                 RL ranker into treating mk=2 associations
#                                 as experimentally validated, inflating
#                                 their training weight.
#   "omim_community"    (mk=4) — WRONG: there is no "community" concept
#                                 in OMIM's mapping_key system. mk=4 is
#                                 "contiguous gene syndrome" (deletion/
#                                 duplication spanning multiple genes).
#                                 The label was invented.
#   "omim_molecular"    (mk=3) — correct
#
# The corrected labels match OMIM's actual semantics verbatim:
OMIM_CONFIDENCE_TIERS: list[tuple[float, str]] = [
    # OMIM scores are always >= 0.5 (from the categorical map), so the
    # sub_weak and weak tiers from DisGeNET don't apply. We use 4 tiers
    # that map 1:1 to the mapping_key values:
    (0.0, "omim_provisional"),               # mk=1 → 0.5 — provisional, not yet confirmed
    (0.55, "omim_phenotype_mapped"),         # mk=2 → 0.6 — phenotype mapped, molecular basis UNKNOWN (was "omim_confirmed" — WRONG)
    (0.75, "omim_contiguous_gene_syndrome"), # mk=4 → 0.8 — contiguous gene syndrome, e.g. DiGeorge/Williams (was "omim_community" — invented, WRONG)
    (0.85, "omim_molecular"),                # mk=3 → 0.9 — molecular basis known (strongest, the actual "confirmed" tier)
]
"""OMIM-specific confidence tiers (Findings 5 & 6 root fix).

Unlike DisGeNET's continuous DSGP scores, OMIM scores are derived from
the categorical ``mapping_key`` field (1-4). These tiers preserve the
mapping_key distinction so the RL ranker can differentiate
"provisional" (mk=1) from "molecular basis known" (mk=3).

ROOT FIX (Findings 5 & 6, P1):
  - mk=2 label changed from "omim_confirmed" → "omim_phenotype_mapped"
    because mk=2 is explicitly NOT confirmed (molecular basis unknown).
    mk=3 is the actual "confirmed" tier.
  - mk=4 label changed from "omim_community" → "omim_contiguous_gene_syndrome"
    because mk=4 is contiguous gene syndrome (DiGeorge, Williams), NOT
    a "community" concept. The previous label was invented.
These corrections align with the OMIM pipeline's own docstring
(omim_pipeline.py:2270-2274) and the missing_values.py mk=2 label
"moderate" → now consistent across all three modules.
"""

# The tier-method version string recorded in the GDA model's
# ``confidence_tier_method`` column (LIN-15, IDEM-17).  Bump this when
# the default thresholds change so downstream consumers can detect a
# definition change.
CONFIDENCE_TIER_METHOD_VERSION: str = "pinero_2020_v1"


def classify_confidence(
    score: Optional[float],
    tiers: Optional[list[tuple[float, str]]] = None,
) -> str:
    """Classify a DisGeNET DSGP score into a confidence tier.

    Uses :func:`bisect.bisect_right` on the sorted thresholds for
    O(log k) classification (DES-3, PERF-11).

    Parameters
    ----------
    score : float or None
        The DisGeNET DSGP score, expected to be in ``[0, 1]``.  NaN and
        negative scores MUST NOT reach this function —
        :func:`cleaning.missing_values.validate_gda_scores` is responsible
        for clipping before classification (SCI-12, SCI-13).  A defensive
        assertion fires if these invariants are violated.
    tiers : list of (threshold, label), optional
        Custom tier list (sorted ascending by threshold).  Defaults to
        :data:`DEFAULT_CONFIDENCE_TIERS`.

    Returns
    -------
    str
        The tier label (e.g. ``"sub_weak"``, ``"weak"``, ``"strong"``).

    Raises
    ------
    ValueError
        If ``score`` is None, NaN, negative, or greater than 1.0
        (defensive check — should never fire if the caller respects the
        SCI-12 / SCI-13 contract).

    Notes
    -----
    CRITICAL FIX (patient safety): the original implementation used
    ``assert`` statements, which are SILENTLY DISABLED when Python is
    invoked with ``-O`` (optimized mode). For a biomedical platform
    where bad scores propagate to drug-repurposing predictions, that
    is unacceptable — a NaN score would silently classify as "weak"
    instead of raising. We replace the asserts with explicit
    ``ValueError`` raises that fire regardless of optimization level.
    """
    # Defensive invariant (SCI-12): the caller (validate_gda_scores) is
    # responsible for clipping and coercing before this function is
    # called.  If we ever see a None, NaN, or negative score here, the
    # contract has been violated — fail LOUDLY with a real exception
    # (not assert, which is disabled by `python -O`).
    if score is None:
        raise ValueError(
            "classify_confidence invariant violated: score is None "
            "(validate_gda_scores should have coerced NaN -> 0.0 first)"
        )
    if pd.isna(score):
        raise ValueError(
            f"classify_confidence invariant violated: score is NaN ({score!r})"
        )
    if score < 0.0:
        raise ValueError(
            f"classify_confidence invariant violated: score={score!r} < 0 "
            f"(validate_gda_scores should have clipped to [0, 1] first)"
        )
    if score > 1.0:
        # v35 ROOT FIX: enforce the upper bound of the DisGeNET DSGP score
        # range. The previous code only checked ``score < 0.0`` and let
        # ``score > 1.0`` silently classify as the top tier ("strong"),
        # masking a bug in the upstream score-computation (which should
        # have clipped to [0, 1]). A score > 1.0 is never legitimate for
        # a normalized DSGP score, so fail LOUDLY instead of silently
        # producing an over-confident tier.
        raise ValueError(
            f"classify_confidence invariant violated: score={score!r} > 1 "
            f"(validate_gda_scores should have clipped to [0, 1] first)"
        )

    if tiers is None:
        tiers = DEFAULT_CONFIDENCE_TIERS
    # Defensive: ensure the tiers are sorted (the caller is expected to
    # sort, but we cannot trust that).
    sorted_tiers = sorted(tiers, key=lambda t: t[0])
    thresholds = [t[0] for t in sorted_tiers]
    labels = [t[1] for t in sorted_tiers]
    # bisect_right returns the insertion point to the right of any
    # existing entries equal to score.  Subtracting 1 gives the index of
    # the tier whose threshold <= score.
    idx = bisect.bisect_right(thresholds, score) - 1
    if idx < 0:
        # score < the lowest threshold — fall back to the lowest tier.
        # This should not happen in practice (the lowest threshold is 0.0
        # and we asserted score >= 0.0 above), but defensive programming.
        idx = 0
    return labels[idx]


__all__ = [
    "DEFAULT_CONFIDENCE_TIERS",
    "OMIM_CONFIDENCE_TIERS",  # v43 P1-033
    "CONFIDENCE_TIER_METHOD_VERSION",
    "classify_confidence",
]
