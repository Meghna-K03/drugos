"""OMIM loader — bridges to Phase 1's cleaned OMIM CSV output.

This loader consumes ``phase1/processed_data/omim_gene_disease_associations.csv``
(produced by ``phase1.pipelines.omim_pipeline.OMIMPipeline``) and emits
Phase 2 node/edge records compatible with ``kg_builder``.

Design decision (v5 audit fix):
    Phase 2's ``run_pipeline.py`` previously tried to import a non-existent
    ``omim_loader`` module, falling into an ``except ImportError`` branch
    that silently skipped OMIM ingestion. The proper fix is to bridge
    Phase 1's already-cleaned OMIM output into Phase 2's graph builder.

Public API (matches the contract expected by ``run_pipeline.py:1784-1789``):
    - ``download_omim()`` → triggers Phase 1's pipeline if needed
    - ``parse_omim()`` → returns a pandas DataFrame of OMIM GDA rows
    - ``omim_to_node_records(df)`` → List[Dict] of Disease/Gene nodes
    - ``omim_to_edge_records(df)`` → List[Dict] of (Gene, associated_with, Disease) edges
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_DEFAULT_PHASE1_PROCESSED_DIR: Path = (
    Path(__file__).resolve().parents[2] / "phase1" / "processed_data"
)
DEFAULT_OMIM_CSV: Path = _DEFAULT_PHASE1_PROCESSED_DIR / "omim_gene_disease_associations.csv"


def _safe_gene_id_from_mim(gene_mim: Any, gene_symbol: str) -> Optional[str]:
    """V19 ROOT FIX (RT-9): robustly convert an OMIM ``gene_mim`` value to a
    bare-numeric Gene ID string, falling back to ``SYM:<symbol>`` when the
    value is non-numeric.

    OMIM's ``morbidmap.txt`` emits non-numeric placeholders (``"?"``,
    ``"FGFR3"``, ``"-"``, ``"1A2B"``) for entries where the gene has no
    MIM number assigned. The previous code did ``str(int(float(gene_mim)))``
    without a try/except — a single non-numeric placeholder raised
    ``ValueError`` and aborted the entire OMIM batch (the caller in
    ``run_pipeline.py`` swallows the exception, so all subsequent OMIM
    rows were silently lost).

    Root-level fix: per-row try/except with a deterministic fallback to
    ``SYM:<gene_symbol>`` (mirroring the existing else-branch logic). If
    neither MIM nor symbol is available, returns ``None`` so the caller
    can skip the row entirely.
    """
    if gene_mim is None:
        return f"SYM:{gene_symbol}" if gene_symbol else None
    raw = str(gene_mim).strip()
    if raw in ("", "nan", "None", "null", "?", "-"):
        return f"SYM:{gene_symbol}" if gene_symbol else None
    try:
        return str(int(float(raw)))
    except (TypeError, ValueError):
        logger.warning(
            "omim_loader: non-numeric gene_mim=%r; falling back to SYM:%s",
            raw, gene_symbol,
        )
        return f"SYM:{gene_symbol}" if gene_symbol else None


# v27 ROOT FIX (P2-L-6): mirror phase1_bridge's Gene ID resolution priority.
# The bridge resolves gene IDs in this order:
#   1. ``canonical_gene_id``  (Phase 1's normalized ID, when available)
#   2. ``ncbi_gene_id``       (NCBI Gene Database numeric ID)
#   3. ``gene_mim``           (OMIM's MIM number — last resort because
#                              MIM numbers are NOT NCBI Gene IDs, they
#                              are OMIM's own phenotype/gene numbering)
#   4. ``SYM:<gene_symbol>``  (symbolic fallback for unresolved genes)
# The previous omim_loader used ONLY ``gene_mim`` — causing Gene ID
# fragmentation: the same gene appeared as two disjoint nodes (one keyed
# by NCBI Gene ID via the bridge, another keyed by MIM number via the
# OMIM loader). This function mirrors the bridge's priority so both
# paths emit the same Gene ID for the same gene.
#
# v41 ROOT FIX (SEV2 COMPOUND): verified the priority order is correct —
# canonical_gene_id / ncbi_gene_id (both NCBI Gene IDs, bare numeric)
# are tried BEFORE gene_mim (which gets a ``MIM:`` prefix to avoid
# namespace collision). This matches the audit's instruction: "when
# canonical_gene_id is available, use it preferentially (NCBI Gene ID).
# Only fall back to MIM: prefix when canonical_gene_id is NULL." The
# existing code already does this; this comment documents the
# ID-namespace divergence rationale for future maintainers:
#
# OMIM MIM numbers and NCBI Entrez Gene IDs are DIFFERENT numbering
# systems that can overlap numerically:
#   - MIM:2645  = "Neuroblastoma, multilocus, with pseudoglioma"
#                 (an OMIM phenotype entry)
#   - NCBIGene:2645 = "GABRR2" (gamma-aminobutyric acid receptor R2)
# Without the ``MIM:`` prefix, the same Gene node ID "2645" would be
# shared by two unrelated biological entities → shadow nodes and
# broken edges in the KG. The ``MIM:`` prefix is registered in
# kg_builder.ID_PATTERNS["Gene"] as a valid alternative.
# DisGeNET, by contrast, emits bare NCBI Gene IDs (no prefix) because
# DisGeNET's source data is already NCBI-keyed. Both loaders thus
# produce edges that resolve to the same Gene node when the underlying
# gene is in NCBI, and OMIM's MIM-only entries get their own
# MIM:-prefixed Gene nodes (which Phase 1's entity_resolver later
# tries to merge with NCBI Gene IDs via the UniProt crosswalk).
def _resolve_gene_id_omim(row: pd.Series) -> Optional[str]:
    """Resolve a Gene ID from an OMIM Phase 1 row using bridge priority.

    Priority: canonical_gene_id → ncbi_gene_id → gene_mim → SYM:<symbol>.
    """
    gene_symbol = str(row.get("gene_symbol") or "").strip()
    # 1. canonical_gene_id (Phase 1's normalized gene ID).
    cgid = row.get("canonical_gene_id")
    if cgid is not None and str(cgid).strip() not in ("", "nan", "None", "null"):
        raw = str(cgid).strip()
        # Strip any NCBIGene: prefix that may already be present.
        if raw.startswith("NCBIGene:"):
            raw = raw[len("NCBIGene:"):]
        try:
            return str(int(float(raw)))
        except (TypeError, ValueError):
            pass  # fall through to next priority
    # 2. ncbi_gene_id.
    ncbi = row.get("ncbi_gene_id")
    if ncbi is not None and str(ncbi).strip() not in ("", "nan", "None", "null"):
        raw = str(ncbi).strip()
        if raw.startswith("NCBIGene:"):
            raw = raw[len("NCBIGene:"):]
        try:
            return str(int(float(raw)))
        except (TypeError, ValueError):
            pass  # fall through
    # 3. gene_mim (OMIM's MIM number — last-resort numeric).
    # audit-2025 ROOT FIX (issue 7): prefix MIM numbers with ``MIM:`` so
    # they do NOT collide with NCBI Gene IDs in the bare-numeric Gene ID
    # namespace. OMIM MIM numbers and NCBI Entrez Gene IDs are DIFFERENT
    # numbering systems that can overlap numerically (e.g. MIM:2645 is
    # "Neuroblastoma, multilocus, with pseudoglioma" while NCBIGene:2645
    # is "GABRR2"). Without the prefix, the same Gene node would be
    # shared by two unrelated biological entities → shadow nodes and
    # broken edges in the KG. The ``MIM:`` prefix is registered in
    # kg_builder.ID_PATTERNS["Gene"] as a valid alternative.
    gene_mim = row.get("gene_mim")
    mim_id = _safe_gene_id_from_mim(gene_mim, gene_symbol)
    if mim_id is not None:
        # If the ID is a bare numeric string (from _safe_gene_id_from_mim's
        # int(float(raw)) path), prefix it with MIM: to distinguish from
        # NCBI Gene IDs. SYM:-prefixed IDs pass through unchanged.
        if mim_id and not mim_id.startswith("SYM:"):
            return f"MIM:{mim_id}"
        return mim_id
    # 4. SYM:<symbol>.
    return f"SYM:{gene_symbol}" if gene_symbol else None


# v27 ROOT FIX (P2-L-13): map OMIM ``association_type`` to distinct
# ``rel_type`` (was: collapse ALL to ``associated_with``).
_OMIM_ASSOC_TYPE_TO_REL: Dict[str, str] = {
    "causal": "associated_with",
    "susceptibility": "susceptible_to",
    "therapeutic": "treats",
    "biomarker": "biomarker_for",
    "gene_locus": "associated_with",  # gene_locus = physical mapping only
}


def download_omim(target_path: Optional[Path] = None) -> Path:
    """Run Phase 1's OMIM pipeline if needed, return CSV path.

    v22 ROOT FIX (audit section 7 finding 11 — "Silent stale-CSV fallback"):
    the previous code returned ANY non-empty CSV with only an INFO log,
    regardless of age. A years-stale CSV would be silently used in
    production. Add a freshness check: if the CSV is older than
    ``DRUGOS_OMIM_MAX_AGE_DAYS`` (default 30), warn loudly and re-run
    the pipeline (unless DRUGOS_ALLOW_STALE_CSV=1 is set).
    """
    import time as _time
    import os as _os
    out_path = target_path or DEFAULT_OMIM_CSV
    if out_path.exists() and out_path.stat().st_size > 0:
        max_age_days = float(_os.environ.get("DRUGOS_OMIM_MAX_AGE_DAYS", "30"))
        allow_stale = _os.environ.get("DRUGOS_ALLOW_STALE_CSV", "") == "1"
        try:
            age_days = (_time.time() - out_path.stat().st_mtime) / 86400.0
        except OSError:
            age_days = 0.0
        if age_days > max_age_days and not allow_stale:
            logger.warning(
                "omim_loader: Phase 1 CSV %s is %.1f days old (max=%g). "
                "Re-running OMIMPipeline to refresh. Set "
                "DRUGOS_ALLOW_STALE_CSV=1 to suppress.",
                out_path, age_days, max_age_days,
            )
            # Fall through to the pipeline invocation below.
        else:
            if age_days > max_age_days:
                logger.warning(
                    "omim_loader: using STALE Phase 1 CSV %s "
                    "(%.1f days old, max=%g) — DRUGOS_ALLOW_STALE_CSV=1.",
                    out_path, age_days, max_age_days,
                )
            else:
                logger.info(
                    "omim_loader: using existing Phase 1 CSV %s "
                    "(age=%.1f days, max=%g)",
                    out_path, age_days, max_age_days,
                )
            return out_path
    try:
        from phase1.pipelines.omim_pipeline import OMIMPipeline  # type: ignore
        logger.info("omim_loader: running Phase 1 OMIMPipeline to produce %s", out_path)
        OMIMPipeline().run()
    except Exception as exc:
        logger.warning(
            "omim_loader: Phase 1 OMIMPipeline could not be invoked (%s). "
            "Falling back to whatever CSV is present at %s.", exc, out_path,
        )
    if not out_path.exists():
        raise FileNotFoundError(f"OMIM CSV not found at {out_path}. Run Phase 1 first.")
    return out_path


def parse_omim(filepath: Optional[Path] = None) -> pd.DataFrame:
    """Read Phase 1's cleaned OMIM CSV into a DataFrame."""
    # v28 ROOT FIX (P2-L-9): the type signature says Optional[Path] but
    # downstream callers (e.g. run_pipeline.py:3122) pass plain ``str``
    # paths. Without coercion, ``path.exists()`` raises
    # ``AttributeError: 'str' object has no attribute 'exists'``. Coerce
    # to ``Path`` at the entry point so any path-like input works.
    if filepath is not None and not isinstance(filepath, Path):
        filepath = Path(filepath)
    path = filepath or DEFAULT_OMIM_CSV
    if not path.exists():
        download_omim(path)
    df = pd.read_csv(path)
    required = {"gene_symbol", "disease_id"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"OMIM CSV {path} missing required columns: {missing}. "
            f"Got columns: {list(df.columns)}"
        )
    return df


# v28 ROOT FIX (P2-L-15): streaming parser for production-scale OMIM
# CSVs. Mirrors ``disgenet_loader.iter_disgenet_chunked``. OMIM's
# morbidmap is typically small, but this API exists for symmetry and
# memory-constrained deployments.
def iter_omim_chunked(
    filepath: Optional[Path] = None,
    chunksize: int = 10_000,
) -> "pd.io.parsers.TextFileReader":
    """Stream Phase 1's OMIM CSV in fixed-size chunks.

    Yields
    ------
    pd.DataFrame
        Successive chunks of ``chunksize`` rows from the CSV.

    Notes
    -----
    Callers iterate the returned reader:

        for chunk in iter_omim_chunked():
            nodes = omim_to_node_records(chunk)
            edges = omim_to_edge_records(chunk)
            ...
    """
    if filepath is not None and not isinstance(filepath, Path):
        filepath = Path(filepath)
    path = filepath or DEFAULT_OMIM_CSV
    if not path.exists():
        download_omim(path)
    return pd.read_csv(path, chunksize=chunksize, low_memory=False)


def omim_to_node_records(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """Emit Disease and Gene node records from OMIM GDA rows.

    v27 ROOT FIX (P2-L-6): use ``_resolve_gene_id_omim`` to resolve the
    Gene ID with the SAME priority as ``phase1_bridge`` (canonical_gene_id
    → ncbi_gene_id → gene_mim → SYM:<symbol>). The previous code used
    ONLY ``gene_mim``, causing the same gene to appear as two disjoint
    nodes (one keyed by NCBI Gene ID via the bridge, another keyed by
    MIM number via the OMIM loader).
    """
    nodes: List[Dict[str, Any]] = []
    seen_disease: set[str] = set()
    seen_gene: set[str] = set()
    for _, row in df.iterrows():
        disease_id = str(row.get("disease_id") or "").strip()
        if disease_id and disease_id not in seen_disease:
            seen_disease.add(disease_id)
            nodes.append({
                "id": disease_id,
                "label": "Disease",
                "name": str(row.get("disease_name") or disease_id),
                "mim_id": str(row.get("phenotype_mim") or ""),
                "_source": "omim",
            })
        gene_symbol = str(row.get("gene_symbol") or "").strip()
        # Filter OMIM's ALTGENE/MENDGENE/MYGENE placeholders (audit §C.4).
        if gene_symbol.upper() in {"ALTGENE", "MENDGENE", "MYGENE", ""}:
            continue
        # v27 ROOT FIX (P2-L-6): use bridge-compatible priority.
        gene_id = _resolve_gene_id_omim(row)
        if gene_id is None:
            continue
        if gene_id not in seen_gene:
            seen_gene.add(gene_id)
            nodes.append({
                "id": gene_id,
                "label": "Gene",
                "name": gene_symbol or gene_id,
                "mim_id": str(row.get("gene_mim") or ""),
                "uniprot_id": str(row.get("uniprot_id") or ""),
                "gene_symbol": gene_symbol,  # BUG-D-009: preserve for canonicalization
                "_source": "omim",
            })
    return nodes


def omim_to_edge_records(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """Emit (Gene, <rel_type>, Disease) edge records.

    v27 ROOT FIXES (P2-L-3 + P2-L-6 + P2-L-13):
      - **P2-L-3 (score scale)**: OMIM ``score`` is already on a 0-1 scale
        (per Phase 1's score_method=omim_mapping_key). Emit BOTH the raw
        source-specific score (``omim_score`` — preserved for traceability)
        AND a canonical ``normalized_score`` in [0,1] for downstream
        cross-source fusion.
      - **P2-L-6 (gene ID resolution)**: use ``_resolve_gene_id_omim``
        (bridge-compatible priority: canonical_gene_id → ncbi_gene_id →
        gene_mim → SYM:<symbol>) instead of ``_safe_gene_id_from_mim``
        alone.
      - **P2-L-13 (association_type collapse)**: map ``association_type``
        to distinct ``rel_type`` (was: collapse ALL to
        ``associated_with``). The raw ``association_type`` is preserved
        in ``props``.

    v28 ROOT FIX (P2-L-16): the previous code applied NO score threshold.
    OMIM mapping_key scores (1=confirmed, 2=likely, 3=provisional) map to
    evidence_strength values; a 0.05-score mapping (provisional evidence)
    carried the SAME edge weight as a 1.0-score confirmed mapping. Now
    drop edges with ``score < config.OMIM_MIN_SCORE`` (default 0.5). The
    dropped-row count is logged at WARNING. Rows with missing/unparseable
    scores are KEPT (they may carry high-quality curated evidence whose
    score was lost during ETL).
    """
    # v28 ROOT FIX (P2-L-16): import OMIM min score threshold.
    try:
        from .config import OMIM_MIN_SCORE as _OMIM_MIN_SCORE
    except ImportError:
        _OMIM_MIN_SCORE = 0.5
    _dropped_below_threshold = 0
    _total_seen = 0

    edges: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        _total_seen += 1
        disease_id = str(row.get("disease_id") or "").strip()
        gene_symbol = str(row.get("gene_symbol") or "").strip()
        if gene_symbol.upper() in {"ALTGENE", "MENDGENE", "MYGENE", ""}:
            continue
        # v27 ROOT FIX (P2-L-6): bridge-compatible Gene ID resolution.
        gene_id = _resolve_gene_id_omim(row) or ""
        if not disease_id or not gene_id:
            continue
        # OMIM score: prefer Phase 1's ``evidence_strength`` (normalized),
        # fall back to ``score``, fall back to ``normalized_score``.
        score = row.get("evidence_strength")
        if score is None or str(score) == "nan":
            score = row.get("normalized_score")
        if score is None or str(score) == "nan":
            score = row.get("score")
        try:
            score_f = float(score) if score is not None and str(score) != "nan" else None
        except (TypeError, ValueError):
            score_f = None
        # v28 ROOT FIX (P2-L-16): apply min-score threshold. Rows with
        # missing scores are KEPT.
        if score_f is not None and score_f < _OMIM_MIN_SCORE:
            _dropped_below_threshold += 1
            continue
        # v27 ROOT FIX (P2-L-3): OMIM scores already 0-1; passthrough.
        if score_f is not None:
            normalized_score = min(max(score_f, 0.0), 1.0)
        else:
            normalized_score = None
        # v27 ROOT FIX (P2-L-13): distinct rel_type per association_type.
        raw_assoc_type = str(row.get("association_type") or "").strip().lower()
        if raw_assoc_type == "nan":
            raw_assoc_type = ""
        rel_type = _OMIM_ASSOC_TYPE_TO_REL.get(raw_assoc_type, "associated_with")
        edges.append({
            "src_id": gene_id,
            "dst_id": disease_id,
            "src_type": "Gene",
            "dst_type": "Disease",
            "rel_type": rel_type,
            "props": {
                "score": score_f,
                # v27 ROOT FIX (P2-L-3): raw source-specific score.
                "omim_score": score_f,
                # Canonical normalized score in [0,1] for cross-source fusion.
                "normalized_score": normalized_score,
                "source": "omim",
                "evidence": raw_assoc_type or "genetic_association",
                # v27 ROOT FIX (P2-L-13): ALWAYS preserve raw association_type.
                "association_type": raw_assoc_type or None,
                "mapping_key": str(row.get("mapping_key") or ""),
            },
            "_source": "omim",
        })
    # v28 ROOT FIX (P2-L-16): log dropped rows so operators can audit.
    if _dropped_below_threshold > 0:
        logger.warning(
            "omim_to_edge_records: dropped %d of %d rows with score < %.3f "
            "(config.OMIM_MIN_SCORE). Set DRUGOS_OMIM_MIN_SCORE=0 to "
            "disable the threshold (not recommended in production).",
            _dropped_below_threshold, _total_seen, _OMIM_MIN_SCORE,
        )
    return edges


__all__ = [
    "download_omim",
    "parse_omim",
    "iter_omim_chunked",
    "omim_to_node_records",
    "omim_to_edge_records",
    "DEFAULT_OMIM_CSV",
]
