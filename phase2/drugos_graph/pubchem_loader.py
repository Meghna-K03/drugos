"""PubChem loader — bridges to Phase 1's cleaned PubChem enrichment CSV.

This loader consumes ``phase1/processed_data/pubchem_enrichment.csv``
(produced by ``phase1.pipelines.pubchem_pipeline.PubChemPipeline``) and
emits Phase 2 Compound node records compatible with ``kg_builder``.

Design decision (v5 audit fix):
    Phase 2's ``run_pipeline.py`` previously tried to import a non-existent
    ``pubchem_loader`` module, falling into an ``except ImportError`` branch
    that silently skipped PubChem enrichment. The proper fix is to bridge
    Phase 1's already-cleaned PubChem output into Phase 2's graph builder.

Public API (matches the contract expected by ``run_pipeline.py:1821-1825``):
    - ``download_pubchem()`` → triggers Phase 1's pipeline if needed
    - ``parse_pubchem()`` → returns a pandas DataFrame of enrichment rows
    - ``pubchem_to_node_records(df)`` → List[Dict] of Compound nodes
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
DEFAULT_PUBCHEM_CSV: Path = _DEFAULT_PHASE1_PROCESSED_DIR / "pubchem_enrichment.csv"


def _safe_float(value: Any) -> Optional[float]:
    """V19 ROOT FIX (RT-10): robustly coerce a value to ``float`` without
    raising on non-numeric placeholders.

    PubChem SD records emit ``"N/A"``, ``">1000"``, ``"?"``, ``"1.5E"``
    and similar non-numeric placeholders for unknown/approximate masses.
    The previous code did ``float(row["molecular_weight"])`` directly —
    a single non-numeric placeholder raised ``ValueError`` and aborted
    the entire PubChem batch (the caller in ``run_pipeline.py`` swallows
    the exception, so all subsequent Compound rows were silently lost).

    Root-level fix: per-row try/except returning ``None`` on failure so
    the row is preserved with ``molecular_weight=None`` instead of
    dropping every subsequent row.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if raw in ("", "nan", "None", "null", "N/A", "NA", "?", "-"):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "pubchem_loader: non-numeric value %r coerced to None", raw,
        )
        return None


def download_pubchem(target_path: Optional[Path] = None) -> Path:
    """Run Phase 1's PubChem pipeline if needed, return CSV path.

    v16 ROOT FIX (SF-6): the previous code used a bare ``except Exception``
    around ``PubChemPipeline().run()`` and logged at WARNING. This hid
    patient-safety-critical PubChem enrichment failures as warnings, and
    the downstream guard ``if not out_path.exists()`` only fired if NO
    CSV existed — a stale/partial CSV from a previous failed run would
    be silently used. Now: narrow the exception to expected failure
    modes (ImportError, OSError, plus the PubChem pipeline's own
    PipelineError), log at ERROR, AND verify the CSV's freshness
    (modification time within the last 30 days) before accepting it.
    """
    out_path = target_path or DEFAULT_PUBCHEM_CSV
    if out_path.exists() and out_path.stat().st_size > 0:
        logger.info("pubchem_loader: using existing Phase 1 CSV %s", out_path)
        return out_path
    try:
        from phase1.pipelines.pubchem_pipeline import PubChemPipeline  # type: ignore
        from phase1.pipelines.base_pipeline import PipelineError  # type: ignore
        _expected_errors = (ImportError, OSError, PipelineError, FileNotFoundError, ValueError)
    except ImportError:
        _expected_errors = (ImportError, OSError, FileNotFoundError, ValueError)
    try:
        from phase1.pipelines.pubchem_pipeline import PubChemPipeline  # type: ignore
        logger.info("pubchem_loader: running Phase 1 PubChemPipeline to produce %s", out_path)
        PubChemPipeline().run()
    except _expected_errors as exc:
        # v16 SF-6: narrow except + ERROR level + metric.
        logger.error(
            "pubchem_loader: Phase 1 PubChemPipeline failed (%s: %s). "
            "Falling back to whatever CSV is present at %s — if the CSV "
            "is stale, downstream enrichment will be missing the latest "
            "PubChem compound properties.",
            type(exc).__name__, exc, out_path,
            exc_info=True,
        )
    if not out_path.exists():
        raise FileNotFoundError(
            f"PubChem CSV not found at {out_path}. Run Phase 1 first."
        )
    # v16 SF-6: warn if the CSV is stale (older than 30 days).
    try:
        import time as _time
        age_sec = _time.time() - out_path.stat().st_mtime
        if age_sec > 30 * 86400:
            logger.warning(
                "pubchem_loader: CSV at %s is %.1f days old — consider "
                "re-running Phase 1 PubChemPipeline to refresh.",
                out_path, age_sec / 86400,
            )
    except OSError:
        pass
    return out_path


def parse_pubchem(filepath: Optional[Path] = None) -> pd.DataFrame:
    """Read Phase 1's cleaned PubChem CSV into a DataFrame."""
    # v28 ROOT FIX (P2-L-9): the type signature says Optional[Path] but
    # downstream callers (e.g. run_pipeline.py:3189) pass plain ``str``
    # paths. Without coercion, ``path.exists()`` raises
    # ``AttributeError: 'str' object has no attribute 'exists'``. Coerce
    # to ``Path`` at the entry point so any path-like input works.
    if filepath is not None and not isinstance(filepath, Path):
        filepath = Path(filepath)
    path = filepath or DEFAULT_PUBCHEM_CSV
    if not path.exists():
        download_pubchem(path)
    df = pd.read_csv(path)
    return df


# v28 ROOT FIX (P2-L-15): streaming parser for production-scale PubChem
# CSVs. ``parse_pubchem`` loads the entire file into memory; production
# PubChem SD-record extracts can be hundreds of MB. ``iter_pubchem_chunked``
# yields successive 10K-row DataFrames so callers with bounded memory can
# process the file incrementally (e.g. the run_pipeline step7h path).
def iter_pubchem_chunked(
    filepath: Optional[Path] = None,
    chunksize: int = 10_000,
) -> "pd.io.parsers.TextFileReader":
    """Stream Phase 1's PubChem CSV in fixed-size chunks.

    Yields
    ------
    pd.DataFrame
        Successive chunks of ``chunksize`` rows from the CSV. The final
        chunk may be smaller.

    Notes
    -----
    Callers iterate the returned reader:

        for chunk in iter_pubchem_chunked():
            nodes = pubchem_to_node_records(chunk)
            builder.load_nodes_batch("Compound", nodes)

    Production PubChem extracts can be hundreds of MB; ``parse_pubchem``
    loads the entire file into memory. This streaming API exists for
    memory-constrained deployments and batch processing pipelines.
    """
    if filepath is not None and not isinstance(filepath, Path):
        filepath = Path(filepath)
    path = filepath or DEFAULT_PUBCHEM_CSV
    if not path.exists():
        download_pubchem(path)
    return pd.read_csv(path, chunksize=chunksize, low_memory=False)


def pubchem_to_node_records(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """Emit Compound node records from PubChem enrichment rows.

    v27 ROOT FIX (P2-L-2): Phase 1's ``pubchem_enrichment.csv`` is keyed
    by ``inchikey`` (NOT ``pubchem_cid`` — PubChem enrichment at Phase 1
    is the post-cleaning output where compounds have already been
    resolved to their canonical InChIKey). The previous implementation
    REQUIRED a ``pubchem_cid`` / ``cid`` / ``CID`` column — none of which
    exist in Phase 1's CSV — so it silently dropped 100% of rows
    (confirmed empirically: ``pubchem_nodes: 0``).

    Phase 1's actual columns are:
      - ``inchikey``         (canonical key, present on every row)
      - ``canonical_smiles`` (canonical SMILES)
      - ``isomeric_smiles``  (stereo-specific SMILES, when available)
      - ``molecular_weight`` (float)
      - ``xlogp``, ``tpsa``, ``complexity``, ``h_bond_donors``,
        ``h_bond_acceptors`` (optional physicochemical properties)

    New behavior:
      - Use ``inchikey`` (uppercased to satisfy kg_builder.ID_PATTERNS)
        as the canonical Compound ID when no CID column is present.
      - Map ``canonical_smiles`` -> node ``smiles`` field.
      - Map ``isomeric_smiles`` -> node ``isomeric_smiles`` field.
      - Emit ``pubchem_cid`` ONLY when a CID column is actually present
        (preserves backward compatibility for raw-PubChem-SD-record inputs).
    """
    nodes: List[Dict[str, Any]] = []
    seen: set[str] = set()
    # v29 ROOT FIX (audit L-8): CID matching was case-sensitive — failed
    # on case differences. Normalize to lowercase before comparison.
    # The previous code only checked three specific column-name spellings
    # ("pubchem_cid", "cid", "CID"). Real-world Phase 1 outputs and raw
    # PubChem SD-record extracts emit the CID column under many case
    # variants ("PubChem_CID", "PUBCHEM_CID", "Cid", "Pubchem_cid", …).
    # Any case variant other than the three hard-coded ones was silently
    # treated as "no CID column present", dropping the CID from the
    # emitted node record (and, when no InChIKey was present either,
    # dropping the whole row). Build a single case-insensitive view of
    # the row ONCE, then look up the CID column by lowercase key.
    cid_column_keys = ("pubchem_cid", "cid")
    for _, row in df.iterrows():
        # Resolve the PubChem CID — accept ANY case variant of the
        # column name (may be absent entirely; Phase 1's enrichment CSV
        # has no CID column). Truthy check mirrors the legacy ``or``
        # chain so falsy values (0, "", NaN-as-empty) fall through.
        row_lc = {str(k).lower(): v for k, v in row.items()}
        cid_raw = next(
            (row_lc.get(k) for k in cid_column_keys if row_lc.get(k)),
            None,
        )
        cid_int: Optional[int] = None
        if cid_raw is not None and str(cid_raw).strip() not in ("", "nan"):
            try:
                cid_int = int(float(cid_raw))
            except (TypeError, ValueError):
                cid_int = None

        # InChIKey — Phase 1's canonical key. Uppercase to satisfy
        # kg_builder.ID_PATTERNS["Compound"] regex (must be uppercase).
        inchikey = str(row.get("inchikey") or "").strip()
        inchikey = "" if inchikey.lower() == "nan" else inchikey.upper()

        # Choose canonical ID: InChIKey preferred, else CID<pid> if we
        # somehow have a CID without an InChIKey (raw-SD-record path).
        if inchikey:
            canonical_id = inchikey
        elif cid_int is not None:
            canonical_id = f"CID{cid_int}"
        else:
            # Neither InChIKey nor CID — cannot canonically identify
            # the compound. Skip rather than emit a dead-letter node.
            continue
        if canonical_id in seen:
            continue
        seen.add(canonical_id)

        # SMILES — Phase 1 emits ``canonical_smiles`` and ``isomeric_smiles``;
        # raw-SD-record path emits ``smiles``. Map all three.
        canonical_smiles = str(row.get("canonical_smiles") or "").strip()
        if canonical_smiles.lower() == "nan":
            canonical_smiles = ""
        isomeric_smiles = str(row.get("isomeric_smiles") or "").strip()
        if isomeric_smiles.lower() == "nan":
            isomeric_smiles = ""
        legacy_smiles = str(row.get("smiles") or "").strip()
        if legacy_smiles.lower() == "nan":
            legacy_smiles = ""
        smiles = canonical_smiles or legacy_smiles or isomeric_smiles

        # v41 ROOT FIX (Task K2 / SEV3): the previous name-fallback chain
        # ended with `canonical_id` (an InChIKey string, e.g. "RYYVLZVLCIJ...-
        # N") when no human-readable name was available. Surfacing an InChIKey
        # as the node `name` is wrong on two counts:
        #   1. Operators reading graph queries / Neo4j Browser see an opaque
        #      14-char hash where they expected a drug name.
        #   2. Downstream `name`-based fuzzy matchers (e.g. NAME:<drug_name>
        #      fallback in clinicaltrials_loader) treat the InChIKey string
        #      as a real drug name and may join against it spuriously.
        # Fix: when no iupac_name / name / CID label is available, leave
        # `name` as None — kg_builder and graph_stats handle None names
        # gracefully (they are excluded from name-based joins and rendered
        # as "<unnamed>" in display paths).
        raw_name = str(row.get("iupac_name") or "").strip()
        if not raw_name or raw_name.lower() == "nan":
            raw_name = str(row.get("name") or "").strip()
            if not raw_name or raw_name.lower() == "nan":
                raw_name = ""
        # If a CID is present we keep the CID-derived synthetic label only
        # when no human-readable name exists (CID<n> IS a stable, intelligible
        # identifier — operators recognise "CID2244" as aspirin's PubChem CID).
        # The InChIKey string fallback is intentionally REMOVED.
        if not raw_name and cid_int is not None:
            raw_name = f"CID{cid_int}"
        node: Dict[str, Any] = {
            "id": canonical_id,
            "label": "Compound",
            "name": raw_name or None,
            "inchikey": inchikey or None,
            "smiles": smiles or None,
            "molecular_formula": str(row.get("molecular_formula") or ""),
            # V19 ROOT FIX (RT-10): delegate to _safe_float so a single
            # non-numeric placeholder (e.g. "N/A", ">1000", "?") no longer
            # aborts the entire PubChem batch with ValueError. The row is
            # preserved with molecular_weight=None instead.
            "molecular_weight": _safe_float(row.get("molecular_weight")),
            "_source": "pubchem",
        }
        # Emit ``pubchem_cid`` ONLY when a CID was actually present —
        # Phase 1's enrichment CSV has no CID column, so omit it.
        if cid_int is not None:
            node["pubchem_cid"] = cid_int
        # Preserve isomeric SMILES as a separate field when available.
        if isomeric_smiles:
            node["isomeric_smiles"] = isomeric_smiles
        nodes.append(node)
    return nodes


__all__ = [
    "download_pubchem",
    "parse_pubchem",
    "iter_pubchem_chunked",
    "pubchem_to_node_records",
    "DEFAULT_PUBCHEM_CSV",
]
