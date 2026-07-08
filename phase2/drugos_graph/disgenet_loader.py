"""DisGeNET loader — bridges to Phase 1's cleaned DisGeNET CSV output.

This loader consumes ``phase1/processed_data/disgenet_gene_disease_associations.csv``
(produced by ``phase1.pipelines.disgenet_pipeline.DisgenetPipeline``) and
emits Phase 2 node/edge records compatible with ``kg_builder``.

v21 ROOT FIX (Audit section 5 finding / bypass matrix - "DEFAULT filename
is wrong: gene_disease_associations.csv vs Phase 1's actual
disgenet_gene_disease_associations.csv"): the previous default filename
was ``gene_disease_associations.csv`` (without the ``disgenet_`` prefix).
Phase 1's actual output is ``disgenet_gene_disease_associations.csv``.
This caused FileNotFoundError on standalone use and was unreachable from
step7 due to the NameError on phase1_processed_dir (now also fixed).
Fix: use the correct prefixed filename as the default; the parser still
accepts an explicit filepath override for backward compat.

Public API (matches the contract expected by ``run_pipeline.py:1746-1751``):
    - ``download_disgenet()`` -> triggers Phase 1's pipeline if needed
    - ``parse_disgenet()`` -> returns a pandas DataFrame of GDA rows
    - ``disgenet_to_node_records(df)`` -> List[Dict] of Disease/Gene nodes
    - ``disgenet_to_edge_records(df)`` -> List[Dict] of (Gene, associated_with, Disease) edges
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Phase 1 emits this CSV; resolve relative to the unified package layout.
_DEFAULT_PHASE1_PROCESSED_DIR: Path = (
    Path(__file__).resolve().parents[2] / "phase1" / "processed_data"
)
# v21: use the CORRECT prefixed filename that Phase 1 actually emits.
DEFAULT_DISGENET_CSV: Path = _DEFAULT_PHASE1_PROCESSED_DIR / "disgenet_gene_disease_associations.csv"
# Backward-compat alias for callers that still pass the old name.
_LEGACY_DISGENET_CSV: Path = _DEFAULT_PHASE1_PROCESSED_DIR / "gene_disease_associations.csv"

# audit-2025 ROOT FIX (issue 9): module-level constant for association-
# type → rel_type mapping. Previously this dict was rebuilt inside
# ``parse_disgenet`` on every call — a small but unnecessary allocation
# on every invocation. Moving it to module level means it is built
# ONCE at import time and shared across all calls.
_DISGENET_ASSOC_TYPE_TO_REL: Dict[str, str] = {
    "causal": "associated_with",
    "susceptibility": "susceptible_to",
    "therapeutic": "treats",
    "biomarker": "biomarker_for",
}


def _resolve_disgenet_csv(target_path: Optional[Path] = None) -> Path:
    """Resolve the DisGeNET CSV path, checking both v21 and legacy names."""
    if target_path is not None:
        return target_path
    if DEFAULT_DISGENET_CSV.exists():
        return DEFAULT_DISGENET_CSV
    if _LEGACY_DISGENET_CSV.exists():
        logger.warning(
            "disgenet_loader: using legacy filename %s. Phase 1's "
            "canonical output is %s - rename the file to silence "
            "this warning.",
            _LEGACY_DISGENET_CSV, DEFAULT_DISGENET_CSV,
        )
        return _LEGACY_DISGENET_CSV
    # Default: return the canonical name even if it doesn't exist yet
    # (download_disgenet will produce it).
    return DEFAULT_DISGENET_CSV


def download_disgenet(target_path: Optional[Path] = None) -> Path:
    """Run Phase 1's DisGeNET pipeline if needed, return CSV path.

    If Phase 1's cleaned CSV already exists AND is fresh, this is a no-op.
    Otherwise it invokes ``phase1.pipelines.disgenet_pipeline.DisgenetPipeline().run()``
    to download + clean + load.

    v22 ROOT FIX (audit section 7 finding 11 — "Silent stale-CSV fallback"):
    the previous code returned ANY non-empty CSV with only an INFO log,
    regardless of age. A years-stale CSV would be silently used in
    production. Add a freshness check: if the CSV is older than
    ``DRUGOS_DISGENET_MAX_AGE_DAYS`` (default 30), warn loudly and
    re-run the pipeline (unless DRUGOS_ALLOW_STALE_CSV=1 is set).
    """
    import time as _time
    import os as _os
    out_path = _resolve_disgenet_csv(target_path)
    if out_path.exists() and out_path.stat().st_size > 0:
        # v22: freshness check.
        max_age_days = float(_os.environ.get("DRUGOS_DISGENET_MAX_AGE_DAYS", "30"))
        allow_stale = _os.environ.get("DRUGOS_ALLOW_STALE_CSV", "") == "1"
        try:
            age_days = (_time.time() - out_path.stat().st_mtime) / 86400.0
        except OSError:
            age_days = 0.0
        if age_days > max_age_days and not allow_stale:
            logger.warning(
                "disgenet_loader: Phase 1 CSV %s is %.1f days old "
                "(max=%g). Re-running DisgenetPipeline to refresh. "
                "Set DRUGOS_ALLOW_STALE_CSV=1 to suppress.",
                out_path, age_days, max_age_days,
            )
            # Fall through to the pipeline invocation below.
        else:
            if age_days > max_age_days:
                logger.warning(
                    "disgenet_loader: using STALE Phase 1 CSV %s "
                    "(%.1f days old, max=%g) — DRUGOS_ALLOW_STALE_CSV=1.",
                    out_path, age_days, max_age_days,
                )
            else:
                logger.info(
                    "disgenet_loader: using existing Phase 1 CSV %s "
                    "(age=%.1f days, max=%g)",
                    out_path, age_days, max_age_days,
                )
            return out_path
    try:
        # Import lazily so the Phase 2 package doesn't hard-depend on Phase 1
        from phase1.pipelines.disgenet_pipeline import DisgenetPipeline  # type: ignore
        logger.info("disgenet_loader: running Phase 1 DisgenetPipeline to produce %s", out_path)
        DisgenetPipeline().run()
    except Exception as exc:
        logger.warning(
            "disgenet_loader: Phase 1 DisgenetPipeline could not be invoked (%s). "
            "Falling back to whatever CSV is present at %s.", exc, out_path,
        )
    if not out_path.exists():
        raise FileNotFoundError(
            f"DisGeNET CSV not found at {out_path}. Run Phase 1 first."
        )
    return out_path


def parse_disgenet(filepath: Optional[Path] = None) -> pd.DataFrame:
    """Read Phase 1's cleaned DisGeNET CSV into a DataFrame."""
    # v28 ROOT FIX (P2-L-9): the type signature says Optional[Path] but
    # downstream callers (e.g. run_pipeline.py:3047) pass plain ``str``
    # paths. Without coercion, ``path.exists()`` raises
    # ``AttributeError: 'str' object has no attribute 'exists'``. Coerce
    # to ``Path`` at the entry point so any path-like input works.
    if filepath is not None and not isinstance(filepath, Path):
        filepath = Path(filepath)
    path = _resolve_disgenet_csv(filepath)
    if not path.exists():
        download_disgenet(path)
    df = pd.read_csv(path)
    # Normalize column names to the contract the rest of Phase 2 expects.
    # Phase 1 emits: gene_symbol, disease_id, disease_name, source, score, ...
    required = {"gene_symbol", "disease_id"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"DisGeNET CSV {path} missing required columns: {missing}. "
            f"Got columns: {list(df.columns)}"
        )
    return df


# v28 ROOT FIX (P2-L-15): streaming parser for production-scale DisGeNET
# CSVs. ``parse_disgenet`` loads the entire file into memory; production
# DisGeNET extracts can exceed 1M rows. ``iter_disgenet_chunked`` yields
# successive 10K-row DataFrames so callers with bounded memory can
# process the file incrementally.
def iter_disgenet_chunked(
    filepath: Optional[Path] = None,
    chunksize: int = 10_000,
) -> "pd.io.parsers.TextFileReader":
    """Stream Phase 1's DisGeNET CSV in fixed-size chunks.

    Yields
    ------
    pd.DataFrame
        Successive chunks of ``chunksize`` rows from the CSV. The final
        chunk may be smaller.

    Notes
    -----
    The first chunk's columns define the schema; subsequent chunks share
    the same dtype mapping. Callers that need to consume the entire file
    should iterate the returned reader:

        for chunk in iter_disgenet_chunked():
            nodes = disgenet_to_node_records(chunk)
            edges = disgenet_to_edge_records(chunk)
            ...

    Phase 1's DisGeNET CSV is typically small (<50 MB) but this API
    exists for symmetry with pubchem/omim loaders and to support
    memory-constrained deployments.
    """
    if filepath is not None and not isinstance(filepath, Path):
        filepath = Path(filepath)
    path = _resolve_disgenet_csv(filepath)
    if not path.exists():
        download_disgenet(path)
    return pd.read_csv(path, chunksize=chunksize, low_memory=False)


def disgenet_to_node_records(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """Emit Disease and Gene node records from DisGeNET GDA rows."""
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
                "_source": "disgenet",
            })
        gene_symbol = str(row.get("gene_symbol") or "").strip()
        # NCBI Gene ID column. BUG-B-002 root fix: kg_builder.ID_PATTERNS
        # rejects 'NCBIGene:2645'. Strip the prefix and use the bare
        # numeric NCBI gene ID. The previous code emitted 'NCBIGene:2645'
        # which fell through to the gene_symbol fallback on every row.
        # Also BUG-A-002 (mentioned in audit): the column may be named
        # ``gene_id`` in some Phase 1 versions — accept both names.
        ncbi_gene_id = (
            row.get("ncbi_gene_id")
            if row.get("ncbi_gene_id") is not None
            else row.get("gene_id")
        )
        if ncbi_gene_id is not None and str(ncbi_gene_id).strip() not in ("", "nan"):
            # Strip any NCBIGene: prefix that may already be present.
            raw = str(ncbi_gene_id).strip()
            if raw.startswith("NCBIGene:"):
                raw = raw[len("NCBIGene:"):]
            try:
                gene_id = str(int(float(raw)))
            except (TypeError, ValueError):
                gene_id = f"SYM:{gene_symbol}" if gene_symbol else None
                if gene_id is None:
                    continue
        elif gene_symbol:
            gene_id = f"SYM:{gene_symbol}"
        else:
            continue
        if gene_id not in seen_gene:
            seen_gene.add(gene_id)
            nodes.append({
                "id": gene_id,
                "label": "Gene",
                "name": gene_symbol or gene_id,
                "gene_symbol": gene_symbol,  # BUG-D-009: preserve for canonicalization
                "_source": "disgenet",
            })
    return nodes


def disgenet_to_edge_records(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """Emit (Gene, associated_with, Disease) edge records.

    v27 ROOT FIXES (P2-L-3 + P2-L-13):
      - **P2-L-3 (score scale)**: DisGeNET ``score`` / ``gda_score`` are
        already on a 0-1 scale (per DisGeNET docs at
        https://www.disgenet.org/documentation). Emit BOTH the raw
        source-specific score (``disgenet_score`` — preserved for
        traceability) AND a canonical ``normalized_score`` in [0,1] for
        downstream model training / cross-source fusion.
      - **P2-L-13 (association_type collapse)**: the previous code
        collapsed ALL ``association_type`` values to
        ``rel_type="associated_with"``. Per the audit, distinct
        biological associations should remain distinct. New mapping:
          causal       -> ``associated_with``
          susceptibility -> ``susceptible_to``
          therapeutic  -> ``treats``
          biomarker    -> ``biomarker_for``
        The raw ``association_type`` is always preserved in ``props``.

    v28 ROOT FIX (P2-L-16): the previous code applied NO score threshold.
    DisGeNET GDA scores span 0-1; a 0.01-score association (text-mined
    noise from a single PubMed abstract) carried the SAME edge weight as
    a 0.95-score association (validated causal variant). Now drop edges
    with ``score < config.DISGENET_MIN_SCORE`` (default 0.3). The
    dropped-row count is logged at WARNING so operators can audit the
    loss. Rows with missing/unparseable scores are KEPT (they may carry
    high-quality curated evidence whose score was lost during ETL).
    """
    # v27 ROOT FIX (P2-L-13): distinct rel_type per association_type.
    # audit-2025 ROOT FIX (issue 9): moved to module-level constant
    # ``_DISGENET_ASSOC_TYPE_TO_REL`` (defined above) so it is built
    # ONCE at import time, not rebuilt on every call to parse_disgenet.
    # Use the module-level constant here.
    _ASSOC_TYPE_TO_REL = _DISGENET_ASSOC_TYPE_TO_REL

    # v28 ROOT FIX (P2-L-16): import DisGeNET min score threshold.
    try:
        from .config import DISGENET_MIN_SCORE as _DISGENET_MIN_SCORE
    except ImportError:
        _DISGENET_MIN_SCORE = 0.3
    _dropped_below_threshold = 0
    _total_seen = 0

    edges: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        _total_seen += 1
        disease_id = str(row.get("disease_id") or "").strip()
        gene_symbol = str(row.get("gene_symbol") or "").strip()
        # BUG-B-002 root fix: same canonicalization as the node emitter.
        # Strip NCBIGene: prefix and use bare numeric ID.
        ncbi_gene_id = (
            row.get("ncbi_gene_id")
            if row.get("ncbi_gene_id") is not None
            else row.get("gene_id")
        )
        if ncbi_gene_id is not None and str(ncbi_gene_id).strip() not in ("", "nan"):
            raw = str(ncbi_gene_id).strip()
            if raw.startswith("NCBIGene:"):
                raw = raw[len("NCBIGene:"):]
            try:
                gene_id = str(int(float(raw)))
            except (TypeError, ValueError):
                gene_id = f"SYM:{gene_symbol}" if gene_symbol else None
                if gene_id is None:
                    continue
        elif gene_symbol:
            gene_id = f"SYM:{gene_symbol}"
        else:
            continue
        if not disease_id or not gene_id:
            continue
        # DisGeNET score: prefer ``gda_score`` (Phase 1's normalized name),
        # fall back to ``score``.
        score = row.get("gda_score")
        if score is None or str(score) == "nan":
            score = row.get("score")
        try:
            score_f = float(score) if score is not None and str(score) != "nan" else None
        except (TypeError, ValueError):
            score_f = None
        # v28 ROOT FIX (P2-L-16): apply min-score threshold. Rows with
        # missing scores are KEPT (curated evidence whose score was lost
        # during ETL may still be high-quality).
        if score_f is not None and score_f < _DISGENET_MIN_SCORE:
            _dropped_below_threshold += 1
            continue
        # v27 ROOT FIX (P2-L-3): DisGeNET scores already on 0-1 scale;
        # passthrough to ``normalized_score`` for cross-source fusion.
        if score_f is not None:
            normalized_score = min(max(score_f, 0.0), 1.0)
        else:
            normalized_score = None
        # v27 ROOT FIX (P2-L-13): map association_type to distinct rel_type.
        raw_assoc_type = str(row.get("association_type") or "").strip().lower()
        if raw_assoc_type == "nan":
            raw_assoc_type = ""
        rel_type = _ASSOC_TYPE_TO_REL.get(raw_assoc_type, "associated_with")
        edges.append({
            "src_id": gene_id,
            "dst_id": disease_id,
            "src_type": "Gene",
            "dst_type": "Disease",
            "rel_type": rel_type,
            "props": {
                "score": score_f,
                # v27 ROOT FIX (P2-L-3): raw source-specific score, preserved
                # under a descriptive name for traceability / debugging.
                "disgenet_score": score_f,
                # Canonical normalized score in [0,1] for cross-source fusion.
                "normalized_score": normalized_score,
                # v41 ROOT FIX (SEV3): default to "disgenet_curated"
                # (the most common sub-source — Phase 1 always emits
                # prefixed sources via _prefix_disgenet_source()).
                # The bare "disgenet" default previously emitted
                # inconsistent source labels: rows WITH a source column
                # got "disgenet_curated" / "disgenet_beasfree" /
                # "disgenet_all" etc., while rows WITHOUT (the common
                # case for curated-only CSVs) got the bare "disgenet".
                # kg_builder's per-source provenance audit counted these
                # as two different sources, splitting the edge count
                # in the report.
                "source": str(row.get("source") or "disgenet_curated"),
                "evidence": "gene_disease_association",
                # v27 ROOT FIX (P2-L-13): ALWAYS preserve raw association_type.
                "association_type": raw_assoc_type or None,
            },
            "_source": "disgenet",
        })
    # v28 ROOT FIX (P2-L-16): log dropped rows so operators can audit.
    if _dropped_below_threshold > 0:
        logger.warning(
            "disgenet_to_edge_records: dropped %d of %d rows with score < %.3f "
            "(config.DISGENET_MIN_SCORE). Set DRUGOS_DISGENET_MIN_SCORE=0 to "
            "disable the threshold (not recommended in production).",
            _dropped_below_threshold, _total_seen, _DISGENET_MIN_SCORE,
        )
    return edges


__all__ = [
    "download_disgenet",
    "parse_disgenet",
    "iter_disgenet_chunked",
    "disgenet_to_node_records",
    "disgenet_to_edge_records",
    "DEFAULT_DISGENET_CSV",
]
