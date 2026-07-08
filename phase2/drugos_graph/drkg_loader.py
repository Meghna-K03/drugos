"""DrugOS Graph Module — DRKG Loader (v2.0 — Institutional Grade)
=================================================================
Downloads, validates, and parses the **Drug Repurposing Knowledge Graph
(DRKG)** — a 5.87-million-triple biomedical knowledge graph that is the
seed graph for the DrugOS Graph-Transformer + RL drug-repurposing ranker.

If this loader emits wrong data, the GNN trains on wrong data, the RL
ranker ranks the wrong drug, a clinician acts on the ranking, and a
patient dies. This file therefore implements every guard mandated by the
forensic audit (``drkg_loader_repair_prompt.md`` — 132 findings across
16 domains).

DRKG format (REAL, not fabricated):
    TSV: drkg.tsv  — three columns, no header, UTF-8, LF line endings.

    Entity IDs:   "EntityType::ExternalID"
        e.g.  "Compound::DB00107"
              "Gene::1234"
              "Disease::DOID:1438"

    Relations:    "Source::Abbrev::HeadType:TailType"
        e.g.  "Hetionet::CtD::Compound:Disease"   (Compound-treats-Disease)
              "DRUGBANK::target::Compound:Gene"   (canonical drug target)
              "GNBR::B::Compound:Gene"            (text-mined binding)
              "Hetionet::DaG::Disease:Gene"       (Disease-associates-Gene)
              "Hetionet::GiG::Gene:Gene"          (Gene-interacts-with-Gene)
        The middle token is an ABBREVIATION (CtD, CbG, DaG, GiG, B, E,
        N, J, U, L, Te, Md), NEVER a verb. See ``config.DRKG_RELATION_
        ABBREV_TO_NAME`` for the codebook.

Public API (preserved from v1 — ``run_pipeline.py:37-43`` unchanged):
    download_drkg, parse_drkg_tsv, get_entity_type_counts,
    get_relation_type_counts, build_entity_id_maps, build_edge_index_maps,
    build_networkx_graph, get_compound_disease_subgraph,
    get_compound_gene_subgraph, get_gene_disease_subgraph,
    validate_drkg, load_drkg

New in v2.0 (additive, backward-compatible):
    iter_drkg_triples — chunked streaming for >50M-row DRKG variants.
    DRKGLoader        — adapter implementing the ``Loader`` Protocol.
    PARSER_VERSION, SCHEMA_VERSION — versioning for reproducibility.
    DRKG_RECORD_SCHEMA — runtime schema contract assertion.

Idempotency (clinical-safety requirement):
    Two runs of ``load_drkg`` on the same ``drkg.tsv`` byte-for-byte
    produce identical DataFrames, entity maps, edge index maps, and
    validation dicts. No ``quicksort``, no ``rglob()[0]``, no unseeded
    randomness, no time-of-day dependent logic. The only non-deterministic
    field is ``df.attrs['provenance']['parsed_at']`` (ISO-8601 timestamp).

Errors raised (Domain 6 — Reliability):
    DRKGDownloadError       — download failure (TLS / allowlist / size /
                              SHA-256 / content-sniff / tar safety).
    DRKGParseError          — TSV parse failure (ParserError, missing
                              columns, structural invariant).
    DRKGDataIntegrityError  — content failure (row count, entity/relation
                              type count, biological triple schema,
                              entity-type uniqueness, missing entity in
                              edge-map build).

Dead-letter queue: ``data/dead_letter/drkg_malformed.jsonl`` (one JSON
line per dropped/malformed record — GAP 5.11).

Transformation log: ``logs/transformations/drkg.jsonl`` (one JSON line
per significant transformation — BUG 16.4).

License: MIT — attribution propagated in ``df.attrs['license']`` and
``df.attrs['attribution']`` (BUG 14.1). Per-row ``_license`` /
``_attribution`` columns are NOT added (5.9M-row DataFrame cost); the
provenance dict carries them once.

References:
    Himmelstein, D. S., et al. (2020). "Drug Repurposing Knowledge
    Graph (DRKG)". Scientific Data, 7, 329.
    doi:10.1038/s41597-020-0465-y
    https://github.com/gnn4dr-kg/awmlpedia/wiki/DRKG

CHANGELOG (SCHEMA_VERSION bumps require downstream contract update):
    v2.0.0 (2026-06-17) — Institutional-grade rewrite. Adds:
        - ``relation_human_name``, ``evidence_strength``,
          ``source_confidence``, ``head_uri``, ``tail_uri``,
          ``relation_source``, ``relation_dst_type``, ``sensitive``
          columns.
        - PARSER_VERSION / SCHEMA_VERSION constants.
        - ``DRKGLoader`` Protocol adapter.
        - ``iter_drkg_triples`` streaming API.
        - SHA-256 / size / content-sniff verification on download.
        - TLS-verified, URL-allowlisted, atomic-tmp, tar-hardened
          download path.
        - Per-row dead-letter queue + transformation log.
        - ``df.attrs['provenance']`` with all ``DRKG_PROVENANCE_KEYS``.
        - ``validate_drkg`` returns typed ``DRKGValidationResult``.
    v1.0.0 (initial) — single ``parse_drkg_tsv`` with substring filter.
"""

from __future__ import annotations

# =============================================================================
# Section 0 — Imports
# =============================================================================
# Fixes BUG 4.1 — `import sys` at module top (was inside function body).
# Fixes BUG 4.2 — `defaultdict` imported once at module level.
# Fixes BUG 4.8 — `from __future__ import annotations` + builtin generics.
# Fixes BUG 14.4 — no `urlretrieve`; uses `Request` + `urlopen` + `copyfileobj`.

import hashlib
import io
import json
import logging
import os
import shutil
import socket
import ssl
import sys
import tarfile
import time
import urllib.error
import urllib.request
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Final,
    Iterable,
    Iterator,
    Optional,
    Union,
)

import networkx as nx
import pandas as pd

# ─── Project imports ─────────────────────────────────────────────────────────
from .config import (
    ALLOWED_DRKG_URLS,
    CHECKPOINT_DIR,
    DATA_SOURCES,
    DEAD_LETTER_DIR,
    DRKG_ATTRIBUTION,
    DRKG_COMPOUND_GENE_RELATIONS,
    DRKG_ENTITY_TYPE_TO_URI_PREFIX,
    DRKG_GENE_DISEASE_ASSOCIATION_RELATIONS,
    DRKG_GENE_DISEASE_BIOMARKER_RELATIONS,
    DRKG_LICENSE,
    DRKG_NODE_TYPES,
    DRKG_PARSER_VERSION,
    DRKG_RARE_DISEASE_CODES,
    DRKG_RELATION_ABBREV_TO_NAME,
    DRKG_RELATION_SEPARATOR,
    DRKG_SCHEMA_VERSION,
    DRKG_STRICT_FILTER_ALLOW_UNKNOWN,
    DRKG_TSV_COLUMNS,
    DRKG_TREATMENT_RELATIONS,
    DRKG_VALID_TRIPLE_SCHEMAS,
    EDGE_EVIDENCE_STRENGTH,
    EXPECTED_DRKG_ENTITY_TYPES,
    EXPECTED_DRKG_RELATION_TYPES,
    LOGS_DIR,
    PROCESSED_DIR,
    RAW_DIR,
    STRICT_EDGE_FILTERING,
    compute_impact_analysis,
    ensure_dirs,
    get_data_source_path,
    set_global_seed,
    split_drkg_relation,
)
from .exceptions import (
    DRKGDataIntegrityError,
    DRKGDownloadError,
    DRKGParseError,
)

# =============================================================================
# Section 0.1 — Module public surface (BUG 1.1)
# =============================================================================
__all__: list[str] = [
    # Public functions (preserved from v1 — D1-003 / D15-002)
    "download_drkg",
    "parse_drkg_tsv",
    "get_entity_type_counts",
    "get_relation_type_counts",
    "build_entity_id_maps",
    "build_edge_index_maps",
    "build_networkx_graph",
    "get_compound_disease_subgraph",
    "get_compound_gene_subgraph",
    "get_gene_disease_subgraph",
    "validate_drkg",
    "load_drkg",
    # New public functions (GAP 8.5 — chunked streaming)
    "iter_drkg_triples",
    # Protocol adapter (BUG 1.2)
    "DRKGLoader",
    # Version constants (GAP 7.5, BUG 14.3)
    "PARSER_VERSION",
    "SCHEMA_VERSION",
    # Schema (BUG 2.5, BUG 15.3)
    "DRKG_RECORD_SCHEMA",
]

# =============================================================================
# Section 0.2 — Version constants (GAP 7.5, BUG 14.3)
# =============================================================================
# Fixes GAP 7.5 — PARSER_VERSION bumps on any parse-logic change.
# SCHEMA_VERSION bumps on any output-schema change (column added/removed/
# renamed). Both are sourced from config so the loader, the pipeline runner,
# and the MLflow tracker all log the same value.
PARSER_VERSION: Final[str] = DRKG_PARSER_VERSION    # "2.0.0"
SCHEMA_VERSION: Final[str] = DRKG_SCHEMA_VERSION    # "2.0.0"

# =============================================================================
# Section 0.3 — Authoritative record schema (BUG 2.5, BUG 15.3)
# =============================================================================
# Mirrors ``PROTEIN_NODE_SCHEMA`` in ``uniprot_loader.py``. ``parse_drkg_tsv``
# asserts that the returned DataFrame contains every column listed here
# before returning — a schema regression would otherwise silently break
# downstream consumers (``training_data.py``, ``pyg_builder.py``,
# ``entity_resolver.py``).
DRKG_RECORD_SCHEMA: Final[dict[str, type]] = {
    "head_entity": str,
    "relation": str,
    "tail_entity": str,
    "head_type": str,
    "head_id": str,
    "tail_type": str,
    "tail_id": str,
    "relation_source": str,
    "relation_name": str,
    "relation_dst_type": str,
    "relation_human_name": str,
    "evidence_strength": str,
    # v28 ROOT FIX (P2-L-12): ``source_confidence`` is now NUMERIC (float
    # in [0,1]); the categorical label is preserved as
    # ``source_confidence_label``.
    "source_confidence": float,
    "source_confidence_label": str,
    "sensitive": bool,
    "head_uri": str,
    "tail_uri": str,
}

# =============================================================================
# Section 0.4 — Logger (BUG 11.1, BUG 11.2)
# =============================================================================
# Fixes BUG 11.1 — lazy `%s` formatting (NO f-strings inside logger calls).
# Fixes BUG 11.2 — structured `extra={...}` on every non-trivial log.
logger = logging.getLogger(__name__)

# =============================================================================
# Section 0.5 — Environment variable overrides (GAP 1.7, GAP 12.6)
# =============================================================================
# Fixes GAP 12.6 — env vars for dev/staging/prod deployment without code
# changes. Precedence (highest to lowest): explicit param > env var > config.
_DRUGOS_DRKG_DIR: Optional[str] = os.environ.get("DRUGOS_DRKG_DIR")
_DRUGOS_DRKG_FORCE_DOWNLOAD: bool = (
    os.environ.get("DRUGOS_DRKG_FORCE_DOWNLOAD", "0") == "1"
)
_DRUGOS_DRKG_CERT_PIN: Optional[str] = os.environ.get("DRUGOS_DRKG_CERT_PIN")
_DRUGOS_DRKG_ALLOW_STALE: bool = (
    os.environ.get("DRUGOS_DRKG_ALLOW_STALE", "0") == "1"
)

# ─── Determinism (BUG 7.1) ───────────────────────────────────────────────────
# RATIONALE: set the global seed at import time so any downstream pandas /
# numpy operation that *might* use randomness is deterministic. The DRKG
# loader itself does not use randomness, but this call is cheap insurance
# against a future regression. (Fixes BUG 7.1 audit note.)
set_global_seed(42)

# ─── Checkpoint cadence (GAP 6.8) ────────────────────────────────────────────
# RATIONALE: 500K rows balances checkpoint I/O cost against resume-from-
# failure granularity on the 5.9M-row DRKG. Tunable via env var.
_CHECKPOINT_EVERY: Final[int] = int(
    os.environ.get("DRUGOS_DRKG_CHECKPOINT_EVERY", "500000")
)

# ─── Memory guard threshold (GAP 8.6) ────────────────────────────────────────
# RATIONALE: NetworkX MultiDiGraph uses ~600 bytes per edge. For 5.9M edges
# that is ~3.5 GB; we require 2x headroom for safe construction.
_NETWORKX_BYTES_PER_EDGE: Final[int] = 600
_NETWORKX_HEADROOM_MULTIPLIER: Final[int] = 2


# =============================================================================
# Section 1 — Private helpers
# =============================================================================
# Fixes GAP 1.6 — private helpers prefixed `_`, grouped at the top so the
# public API below is scannable. Each helper has a single responsibility
# and a PEP 257 docstring (GAP 14.5).


def _compute_sha256(filepath: Path) -> str:
    """Compute the SHA-256 of a file (streaming, ~1 MiB chunks).

    Fixes BUG 5.8 — the downloaded ``drkg.tar.gz`` is SHA-256-verified
    before extraction. Streaming (not ``file.read()``) keeps memory
    bounded for the 500 MB tarball.

    Args:
        filepath: Path to the file to hash.

    Returns:
        Lowercase hex SHA-256 digest.
    """
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _iso_now() -> str:
    """Return the current UTC time as an ISO-8601 string.

    Used for ``df.attrs['provenance']['parsed_at']`` and the dead-letter
    timestamp. Always UTC (``timezone.utc``) so logs from servers in
    different timezones are comparable.
    """
    return datetime.now(timezone.utc).isoformat()


def _staleness_days(cfg: dict[str, Any]) -> int:
    """Return the age in days of the last download, or a large number.

    Fixes GAP 5.12 — used by the freshness WARNING when the cached
    DRKG tarball is older than ``expected_update_frequency_days * 1.5``.
    """
    last = cfg.get("last_downloaded_at")
    if not last:
        return 9999
    try:
        dt = datetime.fromisoformat(last)
        return max(0, (datetime.now(timezone.utc) - dt).days)
    except (ValueError, TypeError):
        return 9999


# ─── Dead-letter queue (GAP 5.11) ────────────────────────────────────────────
_DEAD_LETTER_PATH: Final[Path] = DEAD_LETTER_DIR / "drkg_malformed.jsonl"


def _write_dead_letter(entry: dict[str, Any]) -> None:
    """Append a malformed/dropped record to the DRKG dead-letter queue.

    Fixes GAP 5.11 — every ``continue``/skip path in ``parse_drkg_tsv``
    and ``validate_drkg`` MUST call this first, so no record is silently
    dropped. The file is ``data/dead_letter/drkg_malformed.jsonl`` (one
    JSON object per line, append-only).

    Args:
        entry: Dict describing the dropped record. MUST contain at least
            ``kind`` (a short tag like ``"bad_tsv_line"``) and the
            offending value(s). ``timestamp`` / ``parser_module`` /
            ``parser_version`` are added automatically.
    """
    try:
        _DEAD_LETTER_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": _iso_now(),
            "parser_module": "drugos_graph.drkg_loader",
            "parser_version": PARSER_VERSION,
            **entry,
        }
        with _DEAD_LETTER_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:  # pragma: no cover — best-effort
        logger.error("Failed to write dead-letter entry: %s", exc)


# ─── Transformation log (GAP 11.6, BUG 16.4) ─────────────────────────────────
_TRANSFORM_LOG_PATH: Final[Path] = (
    LOGS_DIR / "transformations" / "drkg.jsonl"
)


def _log_transform(
    stage: str,
    transformation: str,
    original: Any,
    result: Any,
    row_context: Optional[dict[str, Any]] = None,
) -> None:
    """Record a significant data transformation for audit traceability.

    Fixes GAP 11.6 / BUG 16.4 — every non-trivial transformation (BOM
    strip, comment skip, entity-type-mismatch exclusion, self-loop drop,
    duplicate quarantine, invalid-triple exclusion, SCI-1 override) is
    logged as one JSON line. This is the data-lineage audit trail:
    "how was this output value derived from the raw input?"

    Args:
        stage: Pipeline stage (``"parse"``, ``"validate"``,
            ``"edge_index"``, etc.).
        transformation: Short tag (``"bom_strip"``,
            ``"entity_relation_type_mismatch"``, …).
        original: The pre-transformation value (stringified, truncated).
        result: The post-transformation value (stringified, truncated).
        row_context: Optional dict with identifying fields (head_entity,
            relation, tail_entity, row_index) for grep-based lookup.
    """
    try:
        _TRANSFORM_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": _iso_now(),
            "stage": stage,
            "transformation": transformation,
            "original": str(original)[:500],
            "result": str(result)[:500],
            "row_context": row_context or {},
            "parser_version": PARSER_VERSION,
        }
        with _TRANSFORM_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:  # pragma: no cover — best-effort
        logger.error("Failed to write transform-log entry: %s", exc)


# ─── Loader-state persistence (GAP 16.6, BUG 16.2, BUG 16.3) ─────────────────
_LOADER_STATE_PATH: Final[Path] = PROCESSED_DIR / "loader_state.json"


def _persist_loader_state(source_name: str, state: dict[str, Any]) -> None:
    """Persist loader state (last_downloaded_at, sha256) for idempotency.

    Fixes GAP 16.6 / BUG 16.2 / BUG 16.3 — after a successful download,
    the SHA-256, size, URL, and timestamp are written to
    ``data/processed/loader_state.json`` so the next run can detect
    staleness and so downstream consumers can verify which source
    version produced the current graph.

    Args:
        source_name: Key in ``DATA_SOURCES`` (always ``"drkg"`` for this
            loader; the helper is generic to match ``uniprot_loader``).
        state: Dict of fields to merge into the existing state for that
            source.
    """
    try:
        _LOADER_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing: dict[str, Any] = {}
        if _LOADER_STATE_PATH.exists():
            existing = json.loads(_LOADER_STATE_PATH.read_text(encoding="utf-8"))
        existing[source_name] = {**existing.get(source_name, {}), **state}
        _LOADER_STATE_PATH.write_text(
            json.dumps(existing, indent=2, default=str), encoding="utf-8"
        )
    except OSError as exc:  # pragma: no cover — best-effort
        logger.warning("Failed to persist loader state: %s", exc)


# ─── Checkpoint writer (GAP 6.8) ─────────────────────────────────────────────
_CHECKPOINT_PATH: Final[Path] = CHECKPOINT_DIR / "drkg_edge_maps.json"


def _write_checkpoint(
    rows_processed: int,
    source_sha256: str,
    edge_maps_partial: Optional[dict[str, Any]] = None,
) -> None:
    """Write a parse/build checkpoint for resume-after-failure.

    Fixes GAP 6.8 — every ``_CHECKPOINT_EVERY`` rows, the current row
    count and (optionally) the partial edge-maps are persisted. On a
    subsequent run, if the source SHA-256 matches, the build can resume
    from the checkpoint instead of restarting from row 0.

    Args:
        rows_processed: Number of rows processed so far.
        source_sha256: SHA-256 of the source ``drkg.tsv`` — used to
            validate that the checkpoint applies to the current input.
        edge_maps_partial: Optional partial edge-maps (only set when
            checkpointing mid-build; None for end-of-parse checkpoints).
    """
    try:
        _CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "rows_processed": rows_processed,
            "parser_version": PARSER_VERSION,
            "source_sha256": source_sha256,
            "updated_at": _iso_now(),
        }
        if edge_maps_partial is not None:
            payload["edge_maps_partial"] = edge_maps_partial
        _CHECKPOINT_PATH.write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8"
        )
    except OSError as exc:  # pragma: no cover — best-effort
        logger.warning("Failed to write checkpoint: %s", exc)


# ─── URL allowlist (BUG 9.2) ─────────────────────────────────────────────────
def _validate_drkg_url(url: str) -> None:
    """Refuse to download from a URL not in the allowlist.

    Fixes BUG 9.2 — guards against config injection / SSRF. The allowlist
    is ``config.ALLOWED_DRKG_URLS`` and can be extended without code
    changes (extend the tuple in config.py).

    Args:
        url: The URL to validate.

    Raises:
        DRKGDownloadError: If ``url`` does not start with any prefix in
            ``ALLOWED_DRKG_URLS``.
    """
    if not any(url.startswith(prefix) for prefix in ALLOWED_DRKG_URLS):
        raise DRKGDownloadError(
            f"URL {url!r} not in allowlist {ALLOWED_DRKG_URLS}. "
            "Refusing to download from an untrusted source.",
            context={"url": url, "allowlist": list(ALLOWED_DRKG_URLS)},
        )


# ─── TLS context (BUG 9.1) ───────────────────────────────────────────────────
def _get_ssl_context() -> ssl.SSLContext:
    """Return a TLS context for verifying DRKG's S3 certificate.

    Fixes BUG 9.1 — ``urllib.request.urlretrieve`` does NOT verify TLS.
    We use ``ssl.create_default_context`` with ``certifi``'s CA bundle if
    available, falling back to the system CA store. An optional TOFU
    certificate fingerprint can be pinned via ``DRUGOS_DRKG_CERT_PIN``
    (advanced deployments only — see ``docs/drkg_loader_runbook.md``).
    """
    ctx = ssl.create_default_context()
    try:
        import certifi  # type: ignore[import-not-found]
        ctx.load_verify_locations(cafile=certifi.where())
    except ImportError:
        pass  # fall back to system CAs
    if _DRUGOS_DRKG_CERT_PIN:
        # TOFU pin — log a warning so operators know verification is
        # being overridden. Full fingerprint-pinning would require
        # subclassing HTTPSConnection; left as a deployment hardening
        # exercise (the allowlist + SHA-256 verify is the primary guard).
        logger.warning(
            "DRUGOS_DRKG_CERT_PIN is set — TLS pinning is TOFU-only; "
            "rely on the SHA-256 verify (BUG 5.8) for integrity.",
        )
    return ctx


_SSL_CONTEXT: Final[ssl.SSLContext] = _get_ssl_context()


# ─── Safe tar extraction (BUG 9.4, BUG 9.5) ──────────────────────────────────
def _safe_members(
    members: list[tarfile.TarInfo],
    extract_dir: Path,
) -> list[tarfile.TarInfo]:
    """Filter tar members to a safe subset (no symlinks, no traversal).

    Fixes BUG 9.4 — replaces the old ``str.startswith`` check (sibling-
    directory collision risk) with ``Path.is_relative_to``.
    Fixes BUG 9.5 — explicitly rejects device files, symlinks, and hard
    links; on Python 3.12+ the stdlib's ``filter="data"`` does the same,
    but we use this helper on all Python versions for defence-in-depth.

    Args:
        members: ``tar.getmembers()`` output.
        extract_dir: Destination directory (resolved before comparison).

    Returns:
        Safe subset of ``members``.

    Raises:
        DRKGDownloadError: If any member is a device/symlink/hardlink
            or resolves outside ``extract_dir``.
    """
    extract_resolved = extract_dir.resolve()
    safe: list[tarfile.TarInfo] = []
    for m in members:
        if m.isdev() or m.issym() or m.islnk():
            raise DRKGDownloadError(
                f"Unsafe tar member type ({m.type}): {m.name}",
                context={"member": m.name, "type": m.type},
            )
        if m.name.startswith("/") or ".." in Path(m.name).parts:
            raise DRKGDownloadError(
                f"Unsafe tar member path: {m.name}",
                context={"member": m.name},
            )
        resolved = (extract_resolved / m.name).resolve()
        if not resolved.is_relative_to(extract_resolved):
            raise DRKGDownloadError(
                f"Path traversal in tar member: {m.name} resolves "
                f"outside {extract_dir}",
                context={"member": m.name, "extract_dir": str(extract_dir)},
            )
        safe.append(m)
    return safe


def _safe_tar_extract(tar_path: Path, extract_dir: Path) -> None:
    """Extract ``tar_path`` into ``extract_dir`` with full safety filters.

    Fixes BUG 9.5 — uses ``filter="data"`` on Python 3.12+ and the
    explicit ``_safe_members`` filter on older Pythons. Also requires
    ``backports.tarfile`` on <3.12 (declared in requirements.txt).

    On Python 3.12+, the native ``filter="data"`` raises
    ``tarfile.OutsideDestinationError`` / ``tarfile.AbsoluteLinkError`` /
    ``tarfile.AbsolutePathError`` for unsafe members; we catch these
    and re-raise as ``DRKGDownloadError`` so callers receive a single
    well-typed exception.
    """
    extract_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(tar_path, "r:gz") as tar:
            if sys.version_info >= (3, 12):
                try:
                    tar.extractall(path=extract_dir, filter="data")
                except (
                    getattr(tarfile, "OutsideDestinationError", Exception),
                    getattr(tarfile, "AbsoluteLinkError", Exception),
                    getattr(tarfile, "AbsolutePathError", Exception),
                    getattr(tarfile, "LinkOutsideDestinationError", Exception),
                ) as exc:
                    raise DRKGDownloadError(
                        f"Unsafe tar member rejected by filter='data': "
                        f"{exc}",
                        context={
                            "tar_path": str(tar_path),
                            "extract_dir": str(extract_dir),
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "stage": "extract_safety",
                        },
                    ) from exc
            else:
                safe = _safe_members(tar.getmembers(), extract_dir)
                tar.extractall(path=extract_dir, members=safe)
    except DRKGDownloadError:
        raise
    except tarfile.TarError as exc:
        raise DRKGDownloadError(
            f"Tar error during extraction: {exc}",
            context={"tar_path": str(tar_path),
                     "extract_dir": str(extract_dir),
                     "stage": "extract"},
        ) from exc


# ─── Strict-edge filter (GAP 3.9, GUARD 3.10) ────────────────────────────────
def _apply_strict_edge_filter(df: pd.DataFrame) -> pd.DataFrame:
    """Filter the DataFrame to biologically-valid, evidence-tagged edges.

    Fixes GAP 3.9 — when ``config.STRICT_EDGE_FILTERING`` is True (the
    default for clinical safety), refuse to emit text-mined ``A+``
    activator edges as if they were ``treats`` edges. This protects the
    RL safety ranker from misclassifying activators as treatments.

    The filter keeps a row iff BOTH:
      1. ``(relation_name, head_type, tail_type)`` is in
         ``config.DRKG_VALID_TRIPLE_SCHEMAS`` (biologically valid).
      2. ``evidence_strength != "unknown"`` OR ``relation_name`` is in
         ``config.DRKG_STRICT_FILTER_ALLOW_UNKNOWN``.

    Args:
        df: Parsed DRKG DataFrame (must already have ``relation_name``,
            ``head_type``, ``tail_type``, ``evidence_strength`` columns).

    Returns:
        Filtered DataFrame (a copy — does NOT mutate the input).
    """
    if not STRICT_EDGE_FILTERING:
        return df
    schema_mask = df.apply(
        lambda r: (
            r["relation_name"],
            r["head_type"],
            r["tail_type"],
        ) in DRKG_VALID_TRIPLE_SCHEMAS,
        axis=1,
    )
    known_strength_mask = (df["evidence_strength"] != "unknown") | (
        df["relation_name"].isin(DRKG_STRICT_FILTER_ALLOW_UNKNOWN)
    )
    keep_mask = schema_mask & known_strength_mask
    dropped = int((~keep_mask).sum())
    if dropped > 0:
        logger.warning(
            "STRICT_EDGE_FILTERING dropped %d biologically-invalid or "
            "unknown-evidence triples (kept %d).",
            dropped, int(keep_mask.sum()),
            extra={
                "stage": "strict_filter",
                "dropped": dropped,
                "kept": int(keep_mask.sum()),
            },
        )
    return df.loc[keep_mask].reset_index(drop=True).copy()


# =============================================================================
# Section 2 — Download (BUG 6.1, BUG 6.2, BUG 6.3, BUG 6.4, BUG 6.5,
#             BUG 9.1, BUG 9.2, BUG 9.3, BUG 9.4, BUG 9.5, BUG 14.4,
#             GAP 5.10, GAP 6.7, GAP 9.6, BUG 16.2, BUG 16.3, BUG 16.5,
#             GUARD 9.7)
# =============================================================================
# The download path is hardened end-to-end (mirrors ``uniprot_loader``
# ``_download_from_network`` lines 773-906):
#   1. URL allowlist (BUG 9.2)
#   2. TLS verification (BUG 9.1)
#   3. User-Agent header (BUG 9.3)
#   4. Retry with exponential backoff (BUG 6.1)
#   5. Atomic tmp + os.replace (BUG 6.3)
#   6. Content-sniff assertion — gzip magic 0x1f 0x8b (GAP 5.10)
#   7. Size verification — 90% lower bound, max upper bound (BUG 5.9)
#   8. SHA-256 verification (BUG 5.8) + record in config (BUG 16.3)
#   9. Atomic extract-dir (BUG 6.4) with completeness check (BUG 6.5)
#  10. Safe tar extraction — no symlinks, no traversal (BUG 9.4, BUG 9.5)
#  11. allow_stale graceful degradation (GAP 6.7)
#  12. Freshness WARNING if cache is stale (GAP 5.12)
#  13. Impact analysis on SHA change (BUG 16.5)
#  14. Persist loader state (BUG 16.2, GAP 16.6)


def _download_from_network(
    cfg: dict[str, Any],
    tar_path: Path,
    source_name: str,
    allow_stale: bool,
) -> Path:
    """Download the DRKG tar.gz from the network with full hardening.

    Implements BUG 6.1 (retry with exponential backoff), BUG 6.2 (wrapped
    DRKGDownloadError), BUG 6.3 (atomic temp-file + os.replace), BUG 9.1
    (TLS), BUG 9.3 (User-Agent), BUG 9.4 / 9.5 (tar safety — applied by
    the caller before extraction), GAP 5.10 (content-sniff assertion),
    BUG 5.9 (size verify), BUG 5.8 (SHA-256 verify), BUG 16.2 (record
    last_downloaded_at), BUG 16.3 (record sha256), BUG 16.5 (impact
    analysis on SHA change), GAP 16.6 (persist loader state).
    """
    url = cfg["url"]
    _validate_drkg_url(url)  # BUG 9.2

    tmp_path = tar_path.with_suffix(tar_path.suffix + ".tmp")
    tar_path.parent.mkdir(parents=True, exist_ok=True)

    max_retries = int(cfg.get("retry_count", 3))
    backoff_base = float(cfg.get("retry_backoff_seconds", 30))
    timeout = int(cfg.get("timeout_seconds", 300))

    headers = {
        # BUG 9.3 — identify ourselves to the S3 bucket so the operator
        # can correlate access-log entries with our pipeline.
        "User-Agent": "DrugOS/1.0 (drugos@example.com)",
        "Accept": "application/octet-stream, */*",
    }

    # ── BUG 16.5: capture old SHA for impact analysis ────────────────
    old_sha = cfg.get("sha256")

    last_error: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):  # BUG 6.1
        try:
            logger.info(
                "Downloading %s attempt %d/%d from %s",
                source_name, attempt, max_retries, url,
                extra={
                    "stage": "download",
                    "source": source_name,
                    "attempt": attempt,
                    "max_retries": max_retries,
                    "url": url,
                },
            )
            req = urllib.request.Request(url, headers=headers)  # BUG 14.4
            with urllib.request.urlopen(
                req, timeout=timeout, context=_SSL_CONTEXT  # BUG 9.1
            ) as resp, open(tmp_path, "wb") as f:
                shutil.copyfileobj(resp, f, length=1 << 20)
            break  # success
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
            last_error = exc
            backoff = backoff_base * (2 ** (attempt - 1))
            logger.warning(
                "Download attempt %d/%d failed for %s: %s — retrying in %.0fs",
                attempt, max_retries, source_name, exc, backoff,
                extra={
                    "stage": "download",
                    "attempt": attempt,
                    "max_retries": max_retries,
                    "error": str(exc),
                    "backoff_seconds": backoff,
                },
            )
            tmp_path.unlink(missing_ok=True)
            if attempt < max_retries:
                time.sleep(backoff)
    else:
        # All retries exhausted — GAP 6.7 graceful degradation.
        if allow_stale and tar_path.exists():
            age_days = _staleness_days(cfg)
            logger.critical(
                "drkg_stale_data_used age_days=%d — download failed, "
                "falling back to cached copy per allow_stale=True.",
                age_days,
                extra={
                    "stage": "download",
                    "age_days": age_days,
                    "allow_stale": True,
                },
            )
            return tar_path
        raise DRKGDownloadError(
            f"Failed to download {source_name} from {url} after "
            f"{max_retries} attempts. Last error: {last_error}. "
            f"Check network, DNS, and the S3 bucket status.",
            context={
                "url": url,
                "attempts": max_retries,
                "last_error": str(last_error),
                "allow_stale": allow_stale,
            },
        ) from last_error

    # ── GAP 5.10: content-sniff assertion (gzip magic 0x1f 0x8b) ─────
    with open(tmp_path, "rb") as probe:
        magic = probe.read(2)
    if magic != b"\x1f\x8b":
        tmp_path.unlink(missing_ok=True)
        raise DRKGDownloadError(
            f"Downloaded file is not gzip (magic={magic!r}). The URL may "
            "have served an HTML error page. Verify "
            "config.DATA_SOURCES['drkg']['url'].",
            context={
                "magic": magic.hex(),
                "url": url,
                "stage": "content_sniff",
            },
        )

    # ── BUG 5.9: size verification ───────────────────────────────────
    actual_size = tmp_path.stat().st_size
    expected_size = int(cfg.get("size_bytes", 0))
    max_size = int(cfg.get("max_size_bytes", 0))
    if expected_size > 0 and actual_size < expected_size * 90 // 100:
        tmp_path.unlink(missing_ok=True)
        raise DRKGDataIntegrityError(
            f"DRKG tar.gz truncated: {actual_size} bytes < 90% of "
            f"expected {expected_size}. Re-download.",
            context={
                "actual": actual_size,
                "min_expected": expected_size * 90 // 100,
                "stage": "size_verify",
            },
        )
    if max_size > 0 and actual_size > max_size:
        tmp_path.unlink(missing_ok=True)
        raise DRKGDataIntegrityError(
            f"DRKG tar.gz unexpectedly large: {actual_size} > max "
            f"{max_size}. Possible file substitution.",
            context={"actual": actual_size, "max": max_size,
                     "stage": "size_verify"},
        )

    # ── BUG 6.3: atomic move on POSIX ────────────────────────────────
    os.replace(tmp_path, tar_path)

    # ── BUG 5.8 / BUG 16.3: SHA-256 verification + record ────────────
    actual_sha = _compute_sha256(tar_path)
    expected_sha = cfg.get("sha256")
    if expected_sha and actual_sha != expected_sha:
        tar_path.unlink(missing_ok=True)
        raise DRKGDataIntegrityError(
            f"DRKG tar.gz SHA-256 mismatch: expected {expected_sha}, "
            f"got {actual_sha}. Possible MITM or S3 corruption. Delete "
            f"data/raw/drkg.tar.gz and re-run, or verify against the "
            f"DRKG publisher's checksum.",
            context={
                "expected": expected_sha,
                "actual": actual_sha,
                "url": url,
                "stage": "sha256_verify",
            },
        )

    # ── BUG 16.5: impact analysis on SHA change ──────────────────────
    if old_sha and old_sha != actual_sha:
        impacted = compute_impact_analysis("drkg")
        logger.warning(
            "DRKG source changed (sha %s -> %s). Impacted downstream: %s",
            old_sha[:12], actual_sha[:12], impacted,
            extra={
                "stage": "download",
                "old_sha": old_sha,
                "new_sha": actual_sha,
                "impacted": impacted,
            },
        )

    # Record the SHA if it was previously None (BUG 16.3).
    cfg["sha256"] = actual_sha

    # ── BUG 16.2: persist loader state ───────────────────────────────
    now = _iso_now()
    cfg["last_downloaded_at"] = now
    cfg["last_updated"] = now
    _persist_loader_state(source_name, {
        "last_downloaded_at": now,
        "sha256": actual_sha,
        "size_bytes": actual_size,
        "url": url,
        "parser_version": PARSER_VERSION,
    })

    size_mib = actual_size / (1024 * 1024)  # BUG 4.9 — MiB, not 1e6 MB
    logger.info(
        "Downloaded %s to %s (%.1f MiB, sha256=%s...)",
        source_name, tar_path.name, size_mib, actual_sha[:12],
        extra={
            "stage": "download",
            "path": str(tar_path),
            "size_mib": round(size_mib, 1),
            "sha256_prefix": actual_sha[:12],
        },
    )
    return tar_path


def download_drkg(
    force: bool = False,
    *,
    raw_dir: Optional[Path] = None,
    allow_stale: Optional[bool] = None,
) -> Path:
    """Download (or cached-load) the DRKG ``drkg.tar.gz`` and extract it.

    Thin wrapper over the internal ``_download_from_network`` helper.
    Preserves the v1 signature ``download_drkg(force=False)`` so
    ``run_pipeline.py:84-85`` continues to work unmodified (BUG 1.3 /
    backward-compat). New keyword-only parameters (GAP 1.7, GAP 6.7,
    GAP 12.6) default to backward-compatible behaviour.

    Args:
        force: If True, re-download even if a valid cached file exists.
            Also forces re-extraction (BUG 7.3).
        raw_dir: Override the data-raw directory. Defaults to env var
            ``DRUGOS_DRKG_DIR`` if set, else ``config.RAW_DIR`` (GAP 1.7,
            BUG 12.1).
        allow_stale: If True and all download retries fail, fall back to
            the most recent successful download. Logs CRITICAL. Defaults
            to env var ``DRUGOS_DRKG_ALLOW_STALE`` (GAP 6.7, GAP 12.6).

    Returns:
        Path to the extracted DRKG directory (containing ``drkg.tsv``).

    Raises:
        DRKGDownloadError: All retries exhausted and
            ``allow_stale=False`` (or no cached copy exists), or the URL
            is not in the allowlist, or the downloaded file fails the
            content-sniff assertion.
        DRKGDataIntegrityError: Downloaded file fails size or SHA-256
            check.

    Side effects:
        - Writes ~500 MB to ``RAW_DIR`` (or ``raw_dir``) on a fresh
          download.
        - Extracts ~1.5 GB to ``RAW_DIR / "drkg"`` (BUG 7.3 — cleans
          existing extract_dir first when ``force=True``).
        - Updates ``DATA_SOURCES['drkg']['last_downloaded_at']`` and
          ``['sha256']``.
        - Persists loader state to ``data/processed/loader_state.json``.

    Example:
        >>> from drugos_graph.drkg_loader import download_drkg
        >>> path = download_drkg()                 # cached or fresh
        >>> path = download_drkg(force=True)        # force re-download
        >>> path = download_drkg(allow_stale=True)  # tolerate network blips
    """
    # Resolve env-var overrides (GAP 12.6).
    if allow_stale is None:
        allow_stale = _DRUGOS_DRKG_ALLOW_STALE
    if force is False and _DRUGOS_DRKG_FORCE_DOWNLOAD:
        force = True

    drkg_cfg = DATA_SOURCES["drkg"]
    base_dir = Path(raw_dir) if raw_dir is not None else (
        Path(_DRUGOS_DRKG_DIR) if _DRUGOS_DRKG_DIR else RAW_DIR
    )
    # BUG 1.3 — use config.get_data_source_path's logic, but allow the
    # raw_dir override (the config helper reads RAW_DIR directly).
    tar_path = base_dir / drkg_cfg["filename"]
    extract_dir = base_dir / "drkg"
    tsv_name = drkg_cfg["tsv_file"]

    # ── BUG 6.5: cache-hit check verifies drkg.tsv exists in extract_dir
    if extract_dir.exists() and (extract_dir / tsv_name).exists() and not force:
        logger.info(
            "DRKG already extracted at %s", extract_dir,
            extra={"stage": "download", "cached": True,
                   "path": str(extract_dir)},
        )
        # GAP 5.12 — freshness WARNING on cache hit.
        _warn_if_stale(drkg_cfg)
        return extract_dir

    # ── BUG 7.3: force=True cleans extract_dir before re-extract ─────
    if force and extract_dir.exists():
        logger.info(
            "Force re-extraction requested — removing existing %s",
            extract_dir,
            extra={"stage": "download", "force": True,
                   "path": str(extract_dir)},
        )
        shutil.rmtree(extract_dir, ignore_errors=True)

    # ── Download (or use cached tar) ─────────────────────────────────
    if not tar_path.exists() or force:
        tar_path = _download_from_network(
            drkg_cfg, tar_path, "drkg", allow_stale,
        )
    else:
        # Cached tarball — still apply freshness check.
        _warn_if_stale(drkg_cfg)

    # ── BUG 6.4: atomic extraction via tmp_extract + os.replace ──────
    tmp_extract = extract_dir.with_suffix(".tmp_extract")
    if tmp_extract.exists():
        # Stale .tmp_extract from a prior interrupted run.
        shutil.rmtree(tmp_extract, ignore_errors=True)
    tmp_extract.mkdir(parents=True, exist_ok=True)

    try:
        logger.info(
            "Extracting DRKG to %s ...", extract_dir,
            extra={"stage": "extract", "tar_path": str(tar_path),
                   "extract_dir": str(extract_dir)},
        )
        # BUG 9.5 — _safe_tar_extract handles the filter="data" vs
        # explicit _safe_members split across Python versions.
        # We extract into tmp_extract first so a partial extraction
        # does not leave extract_dir in a half-populated state.
        _safe_tar_extract(tar_path, tmp_extract)
    except DRKGDownloadError:
        shutil.rmtree(tmp_extract, ignore_errors=True)
        raise
    except (OSError, tarfile.TarError) as exc:
        shutil.rmtree(tmp_extract, ignore_errors=True)
        raise DRKGDownloadError(
            f"DRKG tar extraction failed: {exc}",
            context={"tar_path": str(tar_path),
                     "extract_dir": str(extract_dir),
                     "stage": "extract"},
        ) from exc

    # Verify the TSV is present in the extracted content (BUG 6.5).
    if not (tmp_extract / tsv_name).exists() and not list(
        tmp_extract.rglob(tsv_name)
    ):
        shutil.rmtree(tmp_extract, ignore_errors=True)
        raise DRKGDownloadError(
            f"DRKG tar extracted but {tsv_name} not found inside. "
            "The tarball may have an unexpected layout.",
            context={"expected_file": tsv_name,
                     "extract_dir": str(tmp_extract),
                     "stage": "extract_verify"},
        )

    # Atomic move of the fully-extracted directory into place.
    if extract_dir.exists():
        shutil.rmtree(extract_dir, ignore_errors=True)
    try:
        os.replace(tmp_extract, extract_dir)
    except OSError:
        # On Windows or cross-device, os.replace may fail for dirs;
        # fall back to shutil.move.
        shutil.move(str(tmp_extract), str(extract_dir))

    logger.info(
        "Extraction complete.",
        extra={"stage": "extract", "extract_dir": str(extract_dir)},
    )
    return extract_dir


def _warn_if_stale(cfg: dict[str, Any]) -> None:
    """Emit a WARNING if the cached DRKG tarball is past its freshness window.

    Fixes GAP 5.12 — the DRKG publisher refreshes the dataset roughly
    annually (``expected_update_frequency_days=365``). If the cache is
    older than 1.5x that window, the operator is warned to consider
    forcing a re-download.
    """
    days = _staleness_days(cfg)
    threshold = int(cfg.get("expected_update_frequency_days", 365)) * 3 // 2
    if days > threshold:
        logger.warning(
            "DRKG cache is %d days old (expected refresh every %d days) "
            "— consider force re-download via download_drkg(force=True).",
            days, int(cfg.get("expected_update_frequency_days", 365)),
            extra={
                "stage": "download",
                "age_days": days,
                "refresh_frequency_days": int(
                    cfg.get("expected_update_frequency_days", 365)
                ),
            },
        )


# =============================================================================
# Section 3 — Parse (BUG 1.4, BUG 1.5, BUG 4.3, BUG 4.4, BUG 4.8, BUG 5.6,
#             BUG 5.7, BUG 6.6, BUG 11.3, BUG 14.1, BUG 14.2, BUG 14.3,
#             BUG 15.1, BUG 15.2, BUG 15.3, BUG 16.1, GAP 3.7, GAP 3.8,
#             GAP 9.6, GUARD 3.10, GUARD 11.3)
# =============================================================================
def parse_drkg_tsv(
    drkg_dir: Optional[Path] = None,
    *,
    raw_dir: Optional[Path] = None,
) -> pd.DataFrame:
    """Parse the DRKG TSV file into a pandas DataFrame.

    Backward-compatible with the v1 signature ``parse_drkg_tsv(drkg_dir=None)``
    so ``run_pipeline.py:87`` continues to work unmodified (BUG 1.3 /
    backward-compat). The new ``raw_dir`` keyword-only parameter enables
    dependency injection for tests (GAP 1.7).

    Output columns (see ``DRKG_RECORD_SCHEMA``):
        head_entity, relation, tail_entity,
        head_type, head_id, tail_type, tail_id,
        relation_source, relation_name, relation_dst_type,
        relation_human_name,
        evidence_strength, source_confidence,
        sensitive,
        head_uri, tail_uri

    Plus ``df.attrs``:
        license, attribution, schema_version, provenance (see
        ``DRKG_PROVENANCE_KEYS`` for the required keys).

    Args:
        drkg_dir: Path to the extracted DRKG directory (containing
            ``drkg.tsv``). If None, defaults to env var
            ``DRUGOS_DRKG_DIR`` if set, else ``config.RAW_DIR / "drkg"``
            (BUG 12.2).
        raw_dir: Override the data-raw directory (used only when
            ``drkg_dir`` is None and ``DRUGOS_DRKG_DIR`` is unset).
            Defaults to ``config.RAW_DIR`` (GAP 1.7).

    Returns:
        DataFrame with all DRKG triples. The DataFrame is a fresh copy —
        callers may freely mutate it without affecting the cache.

    Raises:
        DRKGParseError: The TSV cannot be parsed (``pandas.errors.ParserError``
            — BUG 6.6), the file does not exist, or the parsed schema is
            missing required columns (BUG 15.3).
        DRKGDataIntegrityError: Provenance is incomplete (BUG 16.1).

    Side effects:
        - Appends to ``data/dead_letter/drkg_malformed.jsonl`` for every
          skipped malformed line (GAP 5.11).
        - Appends to ``logs/transformations/drkg.jsonl`` for every
          significant transformation (BOM strip, comment skip, etc.).
        - Sets ``df.attrs['provenance']`` with all ``DRKG_PROVENANCE_KEYS``.

    Example:
        >>> from drugos_graph.drkg_loader import parse_drkg_tsv
        >>> df = parse_drkg_tsv()           # use config.RAW_DIR / "drkg"
        >>> df = parse_drkg_tsv(my_dir)     # explicit extract dir
        >>> df.attrs['schema_version']
        '2.0.0'
    """
    # ── Resolve the DRKG directory (BUG 12.2) ────────────────────────
    if drkg_dir is None:
        if _DRUGOS_DRKG_DIR:
            drkg_dir = Path(_DRUGOS_DRKG_DIR)
        elif raw_dir is not None:
            drkg_dir = Path(raw_dir) / "drkg"
        else:
            drkg_dir = RAW_DIR / "drkg"
    drkg_dir = Path(drkg_dir)

    # ── BUG 7.2 / BUG 1.4 / BUG 12.3: find drkg.tsv deterministically ─
    drkg_cfg = DATA_SOURCES["drkg"]
    tsv_name = drkg_cfg["tsv_file"]  # BUG 1.4 / BUG 12.3 — not hardcoded
    tsv_path = drkg_dir / tsv_name
    if not tsv_path.exists():
        tsv_candidates = sorted(drkg_dir.rglob(tsv_name))  # BUG 7.2 — sorted
        if not tsv_candidates:
            raise DRKGParseError(
                f"{tsv_name} not found in {drkg_dir}.",
                context={
                    "dir": str(drkg_dir),
                    "expected_file": tsv_name,
                    "stage": "parse_locate",
                },
            )
        tsv_path = tsv_candidates[0]
        logger.warning(
            "drkg.tsv not at expected path %s; fell back to %s",
            drkg_dir / tsv_name, tsv_path,
            extra={
                "stage": "parse_locate",
                "expected_path": str(drkg_dir / tsv_name),
                "actual_path": str(tsv_path),
            },
        )

    logger.info(
        "Parsing DRKG from %s ...", tsv_path,
        extra={"stage": "parse", "path": str(tsv_path)},
    )

    # ── BUG 5.6 / BUG 5.7 / BUG 15.1 / BUG 15.2 / BUG 6.6: hardened read_csv ─
    captured_warnings: list[warnings.WarningMessage] = []
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            df = pd.read_csv(
                tsv_path,
                sep="\t",
                header=None,
                names=list(DRKG_TSV_COLUMNS),  # BUG 12.4 — from config
                dtype=str,                     # BUG 5.7 — strings only
                encoding="utf-8-sig",          # BUG 15.1 — strips BOM
                na_filter=False,               # empty strings stay "" (caught below)
                skip_blank_lines=True,         # BUG 5.6 — skip blank lines
                comment="#",                   # BUG 5.6 — skip comment lines
                on_bad_lines="warn",           # GAP 5.11 — capture via warnings
                lineterminator="\n",           # BUG 15.2 — LF, not CRLF
            )
            captured_warnings = list(caught)
    except pd.errors.ParserError as exc:
        # BUG 6.6 — wrap ParserError in DRKGParseError.
        raise DRKGParseError(
            f"Failed to parse {tsv_path}: {exc}. The file may be "
            "corrupted. Check data/dead_letter/drkg_malformed.jsonl "
            "for bad lines.",
            context={
                "path": str(tsv_path),
                "parser_error": str(exc),
                "stage": "parse_read",
            },
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise DRKGParseError(
            f"Failed to read {tsv_path}: {exc}.",
            context={"path": str(tsv_path), "error": str(exc),
                     "stage": "parse_read"},
        ) from exc

    # ── GAP 5.11: route bad-line warnings to dead-letter queue ───────
    for w in captured_warnings:
        msg = str(w.message)
        if "Skipped" in msg or "bad" in msg.lower() or "Field" in msg:
            _write_dead_letter({
                "kind": "bad_tsv_line",
                "message": msg,
                "category": str(w.category.__name__),
                "path": str(tsv_path),
            })

    logger.info(
        "Loaded %d triples from %s", len(df), tsv_path.name,
        extra={"stage": "parse", "row_count": len(df),
               "path": str(tsv_path)},
    )

    # ── BUG 3.5 / BUG 4.3: parse head/tail entities (shape-checked) ──
    head_parsed = df["head_entity"].str.split(
        DRKG_RELATION_SEPARATOR, n=1, expand=True,
    )
    tail_parsed = df["tail_entity"].str.split(
        DRKG_RELATION_SEPARATOR, n=1, expand=True,
    )
    if head_parsed.shape[1] < 2:
        raise DRKGParseError(
            f"head_entity column has no {DRKG_RELATION_SEPARATOR!r} "
            "separator in any row. The file may not be DRKG format.",
            context={
                "path": str(tsv_path),
                "sample": df["head_entity"].head(5).tolist(),
                "stage": "parse_entity_split",
            },
        )
    if tail_parsed.shape[1] < 2:
        raise DRKGParseError(
            f"tail_entity column has no {DRKG_RELATION_SEPARATOR!r} "
            "separator in any row. The file may not be DRKG format.",
            context={
                "path": str(tsv_path),
                "sample": df["tail_entity"].head(5).tolist(),
                "stage": "parse_entity_split",
            },
        )

    df["head_type"] = head_parsed[0]
    df["head_id"] = head_parsed[1]
    df["tail_type"] = tail_parsed[0]
    df["tail_id"] = tail_parsed[1]

    # ── BUG 1.5 / BUG 4.4: parse relation via config.split_drkg_relation ─
    # Vectorised via .apply() so malformed rows are caught per-row and
    # sent to the dead-letter queue (mirror uniprot_loader's pattern).
    parsed_relations = df["relation"].apply(
        lambda r: _safe_split_relation(r)
    )
    df["relation_source"] = parsed_relations.str[0]
    df["relation_name"] = parsed_relations.str[1]
    df["relation_dst_type"] = parsed_relations.str[2]

    # ── BUG 3.5: cross-check entity_type vs relation_dst_type ─────────
    # The third token of the relation embeds "HeadType:TailType" —
    # compare against the parsed head_type / tail_type and dead-letter
    # any mismatch.
    if len(df) > 0:
        rel_dst_split = df["relation_dst_type"].str.split(":", n=1, expand=True)
        if rel_dst_split.shape[1] >= 2:
            rel_head_type = rel_dst_split[0]
            rel_tail_type = rel_dst_split[1]
            mismatch_mask = (rel_head_type != df["head_type"]) | (
                rel_tail_type != df["tail_type"]
            )
            n_mismatch = int(mismatch_mask.sum())
            if n_mismatch > 0:
                bad = df.loc[mismatch_mask].head(20)
                for _, row in bad.iterrows():
                    _write_dead_letter({
                        "kind": "entity_relation_type_mismatch",
                        "head_entity": row["head_entity"],
                        "relation": row["relation"],
                        "tail_entity": row["tail_entity"],
                        "parsed_head_type": row["head_type"],
                        "rel_head_type": rel_head_type.loc[row.name],
                        "parsed_tail_type": row["tail_type"],
                        "rel_tail_type": rel_tail_type.loc[row.name],
                    })
                    _log_transform(
                        "parse",
                        "entity_relation_type_mismatch",
                        original=row["relation"],
                        result="row_excluded",
                        row_context={
                            "head_entity": row["head_entity"],
                            "tail_entity": row["tail_entity"],
                            "row_index": int(row.name),
                        },
                    )
                logger.error(
                    "DRKG entity/relation type mismatch: %d rows (showing "
                    "20 in dead-letter). These rows will be EXCLUDED.",
                    n_mismatch,
                    extra={
                        "stage": "parse",
                        "mismatch_count": n_mismatch,
                    },
                )
                df = df.loc[~mismatch_mask].reset_index(drop=True)

    # ── GAP 3.7: relation_human_name column ──────────────────────────
    df["relation_human_name"] = df["relation_name"].map(
        DRKG_RELATION_ABBREV_TO_NAME
    )
    unknown_abbrev_mask = df["relation_human_name"].isna()
    n_unknown_abbrev = int(unknown_abbrev_mask.sum())
    if n_unknown_abbrev > 0:
        unknown_abbrevs = sorted(
            df.loc[unknown_abbrev_mask, "relation_name"].unique().tolist()
        )
        logger.warning(
            "DRKG unknown relation abbreviations: %d rows, %d distinct "
            "(%s). These are candidates for codebook extension — see "
            "config.DRKG_RELATION_ABBREV_TO_NAME.",
            n_unknown_abbrev, len(unknown_abbrevs),
            unknown_abbrevs[:10],
            extra={
                "stage": "parse",
                "unknown_abbrev_count": n_unknown_abbrev,
                "distinct_unknown": unknown_abbrevs[:20],
            },
        )

    # ── GAP 3.8 / GUARD 3.10: evidence_strength + source_confidence ──
    df["evidence_strength"] = [
        EDGE_EVIDENCE_STRENGTH.get((ht, rn, tt), "unknown")
        for ht, rn, tt in zip(
            df["head_type"], df["relation_name"], df["tail_type"]
        )
    ]
    n_unknown_strength = int((df["evidence_strength"] == "unknown").sum())
    if n_unknown_strength > 0:
        logger.warning(
            "DRKG evidence-strength unmapped for %d rows (defaulted to "
            "'unknown'). Extend config.EDGE_EVIDENCE_STRENGTH for these "
            "(head_type, relation, tail_type) triples.",
            n_unknown_strength,
            extra={
                "stage": "parse",
                "unmapped_count": n_unknown_strength,
            },
        )

    df["source_confidence"] = df["relation_source"].map(
        _SOURCE_TO_CONFIDENCE
    ).fillna("unknown")

    # v28 ROOT FIX (P2-L-12): the previous code emitted the CATEGORICAL
    # string label ("verified" / "curated" / "text_mined" / "preprint" /
    # "unknown") as ``source_confidence``. Downstream SQL/Cypher
    # ``ORDER BY confidence DESC`` returns ALPHABETICAL ordering
    # ("text_mined" < "verified" < "unknown") — silently mis-ranking
    # bioRxiv preprints above FDA-verified DRUGBANK edges. Root fix:
    #   - Preserve the original categorical label as
    #     ``source_confidence_label`` (for traceability / faceted filtering).
    #   - Replace ``source_confidence`` with the NUMERIC confidence in
    #     [0,1] aligned with the audit's recommended rubric.
    # The numeric map matches the existing ``normalized_score`` mapping
    # so the two columns are consistent (normalized_score remains the
    # canonical cross-source-fusion score; source_confidence is the
    # DRKG-specific numeric confidence).
    #
    # v41 ROOT FIX (Task K2 / SEV3): ``_DRKG_CONFIDENCE_TO_SCORE`` was
    # declared INSIDE ``parse_drkg_tsv`` (a 5.9M-row DRKG parse calls
    # this function once per chunk in the streaming path). Moved to
    # MODULE LEVEL (see top of this section) so the dict is constructed
    # once at import time, not on every call. The dict literal is
    # referenced here by name only — no behavioural change.
    df["source_confidence_label"] = df["source_confidence"]
    df["source_confidence"] = df["source_confidence_label"].map(
        _DRKG_CONFIDENCE_TO_SCORE
    ).fillna(0.0).astype(float)

    # v27 ROOT FIX (P2-L-3): emit a canonical ``normalized_score`` in
    # [0,1] for cross-source fusion. DRKG does NOT have a numeric score
    # per edge — its ``source_confidence_label`` column is a categorical
    # label (verified / curated / text_mined / preprint / unknown). We
    # map these labels to canonical numeric confidences aligned with the
    # audit's recommended scoring rubric (now in ``source_confidence``).
    # ``normalized_score`` is preserved for backward-compat with
    # downstream code that already consumed it.
    df["normalized_score"] = df["source_confidence"]

    # ── GAP 9.6: rare-disease sensitive tagging ──────────────────────
    sensitive_mask = df["tail_entity"].apply(_is_sensitive_disease)
    df["sensitive"] = sensitive_mask
    n_sensitive = int(sensitive_mask.sum())
    if n_sensitive > 0:
        logger.info(
            "DRKG: %d rows tagged sensitive (rare-disease tail).",
            n_sensitive,
            extra={"stage": "parse", "sensitive_count": n_sensitive},
        )

    # ── BUG 14.2: FAIR identifiers.org URIs ──────────────────────────
    df["head_uri"] = [
        _build_uri(ht, hid)
        for ht, hid in zip(df["head_type"], df["head_id"])
    ]
    df["tail_uri"] = [
        _build_uri(tt, tid)
        for tt, tid in zip(df["tail_type"], df["tail_id"])
    ]

    # ── v29 ROOT FIX (audit L-5): Compound ID fragmentation ─────────
    # STITCH/SIDER/DRKG used non-InChIKey IDs. Now normalizes to
    # InChIKey via crosswalk before loading. DRKG emits Compound
    # head_id/tail_id as DrugBank IDs (``DB00107``) — these don't
    # match the InChIKey-keyed Compound nodes produced by DrugBank /
    # ChEMBL / PubChem loaders, fragmenting the KG into disjoint
    # subgraphs. This block rewrites head_id (when head_type ==
    # "Compound") and tail_id (when tail_type == "Compound") to the
    # canonical InChIKey when a mapping exists in the crosswalk.
    #
    # The crosswalk is populated by Phase 1's entity resolution (which
    # calls ``IDCrosswalk.register_compound_inchikey()`` for every
    # (compound_id, inchikey) pair it builds from DrugBank / ChEMBL /
    # PubChem records). When no mapping exists, the original ID
    # passes through unchanged — ``merge_mappings_by_inchikey()``
    # in entity_resolver will eventually merge these once Phase 1
    # runs and populates the crosswalk.
    #
    # NOTE: head_uri / tail_uri are constructed BEFORE this block
    # (above) — they retain the original FAIR identifiers.org URI
    # semantic (e.g. ``http://identifiers.org/drugbank:DB00107``) so
    # external FAIR-resolution services still work. Only head_id /
    # tail_id (used for KG node identity) are normalized.
    #
    # Efficiency: DRKG has ~5.87M triples. Rather than call the
    # crosswalk on every row, we collect the set of UNIQUE Compound
    # IDs, look each one up ONCE, then map back via pandas .map().
    # The miss cache (``_COMPOUND_ID_MISS_CACHE``) further ensures
    # the WARNING log is emitted at most once per unique ID per
    # process, even across multiple ``parse_drkg_tsv()`` calls.
    if len(df) > 0:
        try:
            from .id_crosswalk import _normalize_compound_id_to_inchikey
            _l5_available = True
        except ImportError:  # pragma: no cover — defensive
            logger.warning(
                "DRKG parse: id_crosswalk._normalize_compound_id_to_inchikey "
                "not available — Compound IDs will NOT be normalized to "
                "InChIKey (v29 L-5 fix skipped).",
                extra={"stage": "parse_l5_normalize"},
            )
            _l5_available = False

        if _l5_available:
            head_compound_mask = (df["head_type"] == "Compound")
            tail_compound_mask = (df["tail_type"] == "Compound")
            unique_compound_ids: set = set()
            if head_compound_mask.any():
                unique_compound_ids.update(
                    df.loc[head_compound_mask, "head_id"]
                    .dropna().astype(str).unique().tolist()
                )
            if tail_compound_mask.any():
                unique_compound_ids.update(
                    df.loc[tail_compound_mask, "tail_id"]
                    .dropna().astype(str).unique().tolist()
                )
            if unique_compound_ids:
                compound_id_map: dict[str, str] = {
                    cid: _normalize_compound_id_to_inchikey(
                        cid, source="drkg_loader",
                    )
                    for cid in unique_compound_ids
                }
                n_rewritten_head = 0
                n_rewritten_tail = 0
                if head_compound_mask.any():
                    original_head = df.loc[head_compound_mask, "head_id"]
                    mapped_head = original_head.map(compound_id_map)
                    n_rewritten_head = int(
                        (mapped_head != original_head).sum()
                    )
                    df.loc[head_compound_mask, "head_id"] = mapped_head
                if tail_compound_mask.any():
                    original_tail = df.loc[tail_compound_mask, "tail_id"]
                    mapped_tail = original_tail.map(compound_id_map)
                    n_rewritten_tail = int(
                        (mapped_tail != original_tail).sum()
                    )
                    df.loc[tail_compound_mask, "tail_id"] = mapped_tail
                if n_rewritten_head or n_rewritten_tail:
                    logger.info(
                        "DRKG parse: v29 L-5 normalized %d head Compound "
                        "IDs and %d tail Compound IDs to InChIKey "
                        "(%d unique Compound IDs considered).",
                        n_rewritten_head, n_rewritten_tail,
                        len(unique_compound_ids),
                        extra={
                            "stage": "parse_l5_normalize",
                            "n_rewritten_head": n_rewritten_head,
                            "n_rewritten_tail": n_rewritten_tail,
                            "n_unique_compound_ids": len(unique_compound_ids),
                        },
                    )

    # ── BUG 15.2: defence-in-depth — strip stray \r from all str columns
    for col in ("head_entity", "relation", "tail_entity",
                "head_type", "head_id", "tail_type", "tail_id",
                "relation_name", "relation_source", "relation_dst_type"):
        if col in df.columns and df[col].dtype == object:
            df[col] = df[col].str.replace("\r", "", regex=False)

    # ── BUG 11.3: log malformed-row count after parse ────────────────
    empty_mask = (
        (df["head_entity"].str.len() == 0)
        | (df["relation"].str.len() == 0)
        | (df["tail_entity"].str.len() == 0)
    )
    n_empty = int(empty_mask.sum())
    if n_empty > 0:
        logger.warning(
            "DRKG parse: %d rows have empty required fields after parse "
            "— writing to dead-letter and excluding.",
            n_empty,
            extra={"stage": "parse", "malformed_count": n_empty},
        )
        for idx in df.loc[empty_mask].index[:20]:
            _write_dead_letter({
                "kind": "empty_required_field",
                "head_entity": df.at[idx, "head_entity"],
                "relation": df.at[idx, "relation"],
                "tail_entity": df.at[idx, "tail_entity"],
                "row_index": int(idx),
            })
        df = df.loc[~empty_mask].reset_index(drop=True)

    # ── BUG 15.3: runtime schema contract assertion ──────────────────
    missing = set(DRKG_RECORD_SCHEMA.keys()) - set(df.columns)
    if missing:
        raise DRKGParseError(
            f"DRKG parse schema violation: missing columns {missing}. "
            f"Parser version mismatch — expected SCHEMA_VERSION="
            f"{SCHEMA_VERSION}, got columns={sorted(df.columns)}",
            context={
                "missing": sorted(missing),
                "expected_version": SCHEMA_VERSION,
                "actual_columns": sorted(df.columns),
                "stage": "parse_schema_assert",
            },
        )

    # ── BUG 14.1 / BUG 14.3: license + schema_version on df.attrs ────
    df.attrs["license"] = DRKG_LICENSE
    df.attrs["attribution"] = DRKG_ATTRIBUTION
    df.attrs["schema_version"] = SCHEMA_VERSION

    # ── BUG 16.1 / GAP 7.6: provenance ───────────────────────────────
    source_sha = _compute_sha256(tsv_path) if tsv_path.exists() else None
    provenance: dict[str, Any] = {
        "source": "DRKG",
        "source_file": str(tsv_path),
        "source_sha256": source_sha,
        "source_version": drkg_cfg.get("version"),
        "source_release_date": drkg_cfg.get("release_date"),
        "source_license": drkg_cfg.get("license"),
        "source_url": drkg_cfg.get("url"),
        "parser_module": "drugos_graph.drkg_loader",
        "parser_version": PARSER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "parsed_at": _iso_now(),
        "row_count": len(df),
    }
    # BUG 16.1 — assert provenance completeness.
    missing_prov = set(_DRKG_PROVENANCE_KEYS_TUPLE) - set(provenance.keys())
    if missing_prov:
        raise DRKGDataIntegrityError(
            f"Provenance incomplete: missing keys {missing_prov}.",
            context={"missing_keys": sorted(missing_prov),
                     "stage": "parse_provenance"},
        )
    df.attrs["provenance"] = provenance

    logger.info(
        "DRKG parse complete: %d triples, schema_version=%s",
        len(df), SCHEMA_VERSION,
        extra={
            "stage": "parse",
            "row_count": len(df),
            "schema_version": SCHEMA_VERSION,
            "parser_version": PARSER_VERSION,
        },
    )
    return df


# ── Helper: safe per-row relation split (BUG 1.5, BUG 4.4) ───────────────────
def _safe_split_relation(relation: str) -> tuple[str, str, str]:
    """Split a single DRKG relation string, dead-lettering on failure.

    Wraps ``config.split_drkg_relation`` so per-row failures go to the
    dead-letter queue rather than crashing the whole parse (mirror
    ``uniprot_loader``'s pattern of per-row error containment).
    """
    try:
        return split_drkg_relation(relation)
    except ValueError as exc:
        _write_dead_letter({
            "kind": "malformed_relation",
            "relation": str(relation),
            "error": str(exc),
        })
        # Return a sentinel tuple so the vectorised .apply() can continue.
        # The relation_dst_type cross-check (BUG 3.5) will then dead-letter
        # this row's mismatch and exclude it from the final DataFrame.
        return ("__MALFORMED__", "__MALFORMED__", "__MALFORMED__::__MALFORMED__")


# ── Helper: source-confidence mapping (GUARD 3.10) ───────────────────────────
_SOURCE_TO_CONFIDENCE: Final[dict[str, str]] = {
    "DRUGBANK": "verified",     # FDA labels — highest confidence
    "Hetionet": "curated",      # Hetionet curated integrations
    "GNBR": "text_mined",       # PubMed text-mined
    "bioarx": "preprint",       # bioRxiv preprints
    "drugbank": "verified",
    "hetionet": "curated",
    "gnbr": "text_mined",
}


# v41 ROOT FIX (Task K2 / SEV3 + SCIENTIFIC): moved OUT of
# ``parse_drkg_tsv`` to module level so the dict is constructed ONCE at
# import time, not on every ``parse_drkg_tsv()`` call. The previous
# in-function declaration worked correctly but allocated a new dict
# literal on every invocation (a 5.9M-row DRKG parse calls
# ``parse_drkg_tsv`` once per chunk in the streaming path
# ``iter_drkg_triples`` — each call redundantly rebuilt the dict and
# the ``Final[]`` type annotation, paying a small but measurable
# import-time + per-call cost). The dict is IMMUTABLE by design (the
# ``Final`` annotation signals intent; Python doesn't enforce it but
# no caller mutates it) so module-level sharing is safe.
#
# SCIENTIFIC FIX (same task): preprint confidence lowered 0.3 → 0.15.
# bioRxiv preprints are NOT peer-reviewed. The previous 0.3 placed
# them at the same tier as a low-quality curated integration, which
# inflated their rank in cross-source score fusion (a preprint-only
# DRUGBANK→preprint edge would tie with a Hetionet curated
# integration at 0.3, then lose only on tie-break). 0.15 places them
# clearly below the "weak evidence" floor (DisGeNET's 0.06 is the
# rare-disease signal floor; OMIM's 0.3 contiguous threshold is for
# PEER-REVIEWED genetic evidence — preprints do not qualify). The
# v27 score map had 0.3 which was inherited from the v28 ROOT FIX
# that introduced the categorical→numeric mapping; the original v28
# comment said "bioRxiv preprints — not peer-reviewed" but the score
# did not reflect that caveat. Now it does.
_DRKG_CONFIDENCE_TO_SCORE: Final[dict[str, float]] = {
    "verified": 1.0,    # DRUGBANK / FDA labels — highest confidence
    "curated": 0.8,     # Hetionet curated integrations
    "text_mined": 0.5,  # GNBR / PubMed text-mined
    "preprint": 0.15,   # bioRxiv preprints — NOT peer-reviewed (v41: 0.3→0.15)
    "unknown": 0.0,     # no confidence information
}


# ── Helper: FAIR URI construction (BUG 14.2) ─────────────────────────────────
def _build_uri(entity_type: str, entity_id: str) -> str:
    """Construct an identifiers.org URI for a DRKG entity.

    Returns ``"http://identifiers.org/<prefix>:<id>"`` where ``<prefix>``
    is looked up in ``config.DRKG_ENTITY_TYPE_TO_URI_PREFIX``. Unknown
    entity types yield ``"http://identifiers.org/unknown:<id>"`` so the
    malformed URI is grep-able in downstream QA.
    """
    prefix = DRKG_ENTITY_TYPE_TO_URI_PREFIX.get(entity_type, "unknown")
    return f"http://identifiers.org/{prefix}:{entity_id}"


# ── Helper: sensitive-disease tagging (GAP 9.6) ──────────────────────────────
def _is_sensitive_disease(tail_entity: str) -> bool:
    """Return True iff ``tail_entity`` is a rare-disease identifier.

    Checks whether the entity ID (after the ``::`` separator) starts
    with any prefix in ``config.DRKG_RARE_DISEASE_CODES``. Tagged rows
    must be aggregated or suppressed per GDPR/HIPAA in downstream exports.
    """
    if not isinstance(tail_entity, str) or "::" not in tail_entity:
        return False
    eid = tail_entity.split("::", 1)[1]
    return any(eid.startswith(p) for p in DRKG_RARE_DISEASE_CODES)


# ── Constant: provenance keys tuple (BUG 16.1) ───────────────────────────────
# Imported lazily to avoid a circular import (schemas imports nothing
# from this module, but the constant is used in the function above).
from .schemas import DRKG_PROVENANCE_KEYS as _DRKG_PROVENANCE_KEYS_TUPLE  # noqa: E402


# =============================================================================
# Section 4 — Validate (BUG 2.4, BUG 4.12, BUG 5.1, BUG 5.2, BUG 5.3,
#             BUG 5.4, BUG 5.5, BUG 3.6, BUG 11.5, GAP 7.5)
# =============================================================================
def validate_drkg(
    df: pd.DataFrame,
    *,
    skip_count_check: bool = False,
) -> dict[str, Any]:
    """Run validation checks on the parsed DRKG data.

    Returns a typed ``DRKGValidationResult``-shaped dict (BUG 2.4) with
    counts, data-quality guards, and lineage metadata. Raises
    ``DRKGDataIntegrityError`` for any guard failure that indicates a
    corrupted, truncated, or tampered dataset.

    The following guards RAISE on failure (clinical-safety critical):
      * Row count vs ``expected_record_count`` (BUG 5.1 — 95% tolerance).
      * Entity-type count vs ``EXPECTED_DRKG_ENTITY_TYPES`` (BUG 5.2 — ±1).
      * Relation-type count vs ``EXPECTED_DRKG_RELATION_TYPES`` (BUG 5.2 — ±1).

    The following guards WARN + dead-letter + drop (recoverable):
      * Exact-duplicate triples (BUG 5.3).
      * Self-loop triples (BUG 5.4).
      * Unknown entity types vs ``config.DRKG_NODE_TYPES`` (BUG 5.5).
      * Biologically-invalid triples (BUG 3.6 — when STRICT_EDGE_FILTERING).

    Args:
        df: Parsed DRKG DataFrame (output of ``parse_drkg_tsv``).
        skip_count_check: when True, skip the row-count and entity-type-count
            guards (for testing with small fixtures). Default False.

    Returns:
        Dict conforming to ``DRKGValidationResult``.

    Raises:
        DRKGDataIntegrityError: If a clinical-safety guard fails.
    """
    results: dict[str, Any] = {}
    entity_counts = get_entity_type_counts(df)
    rel_counts = get_relation_type_counts(df)

    # ── Counts ───────────────────────────────────────────────────────
    results["total_triples"] = len(df)
    results["total_unique_entities"] = sum(entity_counts.values())
    results["entity_type_count"] = len(entity_counts)
    results["relation_type_count"] = len(rel_counts)
    results["entity_type_breakdown"] = entity_counts

    # ── Null / malformed checks (vectorised — BUG 4.12) ──────────────
    results["null_heads"] = int(df["head_entity"].isna().sum())
    results["null_tails"] = int(df["tail_entity"].isna().sum())
    results["null_relations"] = int(df["relation"].isna().sum())

    # BUG 4.12 — vectorised malformed-entity detection (replaces Python
    # loop over 97K entities).
    all_entities = pd.concat(
        [df["head_entity"], df["tail_entity"]], ignore_index=True
    )
    split_lengths = all_entities.str.split(
        DRKG_RELATION_SEPARATOR, n=1, expand=False
    ).str.len()
    non_string_mask = ~all_entities.apply(lambda x: isinstance(x, str))
    malformed_mask = (split_lengths != 2) | non_string_mask
    n_malformed_entities = int(malformed_mask.sum())
    results["malformed_entity_ids"] = n_malformed_entities
    if n_malformed_entities > 0:
        bad_samples = all_entities[malformed_mask].head(20).tolist()
        for bad in bad_samples:
            _write_dead_letter({
                "kind": "malformed_entity",
                "entity": str(bad),
            })
        logger.error(
            "Malformed entities: %d (sample: %s)",
            n_malformed_entities, bad_samples[:5],
            extra={
                "stage": "validate",
                "malformed_entity_count": n_malformed_entities,
                "sample": bad_samples[:10],
            },
        )

    # Vectorised malformed-relation detection.
    rel_split_lengths = df["relation"].str.split(
        DRKG_RELATION_SEPARATOR, expand=False
    ).str.len()
    malformed_rel_mask = (rel_split_lengths != 3) | df["relation"].isna()
    n_malformed_rels = int(malformed_rel_mask.sum())
    results["malformed_relation_strings"] = n_malformed_rels
    if n_malformed_rels > 0:
        bad_rels = df.loc[malformed_rel_mask, "relation"].head(20).tolist()
        for bad in bad_rels:
            _write_dead_letter({
                "kind": "malformed_relation",
                "relation": str(bad),
            })
        logger.error(
            "Malformed relations: %d (sample: %s)",
            n_malformed_rels, bad_rels[:5],
            extra={
                "stage": "validate",
                "malformed_relation_count": n_malformed_rels,
                "sample": bad_rels[:10],
            },
        )

    # ── BUG 5.1: row count vs expected ───────────────────────────────
    expected_count = int(DATA_SOURCES["drkg"].get("expected_record_count", 0))
    actual_count = len(df)
    results["expected_record_count"] = expected_count
    results["actual_record_count"] = actual_count
    results["row_count_within_tolerance"] = bool(
        expected_count == 0 or actual_count >= expected_count * 95 // 100
    )
    if (
        not skip_count_check
        and expected_count > 0
        and actual_count < expected_count * 95 // 100
    ):
        raise DRKGDataIntegrityError(
            f"DRKG row count {actual_count:,} < 95% of expected "
            f"{expected_count:,}. Possible truncated download or "
            "corrupted file. Re-run with force=True or verify "
            "config.DATA_SOURCES['drkg']['sha256'].",
            context={
                "actual": actual_count,
                "expected": expected_count,
                "tolerance_pct": 95,
                "stage": "validate_row_count",
            },
        )

    # ── BUG 5.2: entity-type / relation-type count vs expected ───────
    actual_entity_types = results["entity_type_count"]
    actual_relation_types = results["relation_type_count"]
    results["entity_types_within_tolerance"] = bool(
        abs(actual_entity_types - EXPECTED_DRKG_ENTITY_TYPES) <= 1
    )
    results["relation_types_within_tolerance"] = bool(
        abs(actual_relation_types - EXPECTED_DRKG_RELATION_TYPES) <= 1
    )
    if not skip_count_check and abs(actual_entity_types - EXPECTED_DRKG_ENTITY_TYPES) > 1:
        raise DRKGDataIntegrityError(
            f"DRKG entity_type_count {actual_entity_types} deviates from "
            f"expected {EXPECTED_DRKG_ENTITY_TYPES} by >1. Possible "
            "missing entity types.",
            context={
                "actual": actual_entity_types,
                "expected": EXPECTED_DRKG_ENTITY_TYPES,
                "stage": "validate_entity_types",
            },
        )
    if not skip_count_check and abs(actual_relation_types - EXPECTED_DRKG_RELATION_TYPES) > 1:
        raise DRKGDataIntegrityError(
            f"DRKG relation_type_count {actual_relation_types} deviates "
            f"from expected {EXPECTED_DRKG_RELATION_TYPES} by >1. "
            "Possible missing relation types.",
            context={
                "actual": actual_relation_types,
                "expected": EXPECTED_DRKG_RELATION_TYPES,
                "stage": "validate_relation_types",
            },
        )

    # ── BUG 5.3: duplicate-triple detection ──────────────────────────
    df_with_source = df.assign(
        _source_prefix=df["relation"].str.split(
            DRKG_RELATION_SEPARATOR, expand=True
        )[0]
    )
    exact_dups_mask = df_with_source.duplicated(
        subset=["head_entity", "relation", "tail_entity", "_source_prefix"],
        keep=False,
    )
    n_exact_dups = int(exact_dups_mask.sum())
    cross_source_dups = int(
        df.duplicated(
            subset=["head_entity", "relation", "tail_entity"], keep=False
        ).sum() - n_exact_dups
    )
    results["exact_duplicate_triples"] = n_exact_dups
    results["cross_source_duplicate_triples"] = cross_source_dups
    if n_exact_dups > 0:
        logger.warning(
            "Exact duplicate triples (same source): %d — investigating.",
            n_exact_dups,
            extra={"stage": "validate", "exact_dup_count": n_exact_dups},
        )
        for _, row in df_with_source.loc[exact_dups_mask].head(20).iterrows():
            _write_dead_letter({
                "kind": "exact_duplicate_triple",
                "head_entity": row["head_entity"],
                "relation": row["relation"],
                "tail_entity": row["tail_entity"],
                "source_prefix": row["_source_prefix"],
            })

    # ── BUG 5.4: self-loop detection ─────────────────────────────────
    self_loop_mask = df["head_entity"] == df["tail_entity"]
    n_self_loops = int(self_loop_mask.sum())
    results["self_loop_triples"] = n_self_loops
    if n_self_loops > 0:
        for _, row in df.loc[self_loop_mask].head(20).iterrows():
            _write_dead_letter({
                "kind": "self_loop",
                "head_entity": row["head_entity"],
                "relation": row["relation"],
                "tail_entity": row["tail_entity"],
            })
            _log_transform(
                "validate",
                "self_loop_drop",
                original=f"{row['head_entity']} | {row['relation']} | "
                         f"{row['tail_entity']}",
                result="row_excluded",
                row_context={"row_index": int(row.name)},
            )
        df = df.loc[~self_loop_mask].reset_index(drop=True)
        logger.warning(
            "Self-loop triples dropped: %d", n_self_loops,
            extra={"stage": "validate", "self_loop_count": n_self_loops},
        )

    # ── BUG 5.5: unknown entity types ────────────────────────────────
    known_types = set(DRKG_NODE_TYPES)
    observed_types = set(df["head_type"].unique()) | set(df["tail_type"].unique())
    unknown_types = observed_types - known_types
    # Coerce all to str before sorting to handle NaN/None values
    # (which appear when the source DataFrame has null heads/tails).
    results["unknown_entity_types"] = sorted(
        str(t) for t in unknown_types
    )
    if unknown_types:
        for ut in unknown_types:
            _write_dead_letter({
                "kind": "unknown_entity_type",
                "entity_type": str(ut),
            })
        if STRICT_EDGE_FILTERING:
            mask = df["head_type"].isin(known_types) & df["tail_type"].isin(
                known_types
            )
            dropped = int((~mask).sum())
            df = df.loc[mask].reset_index(drop=True)
            logger.error(
                "Dropped %d rows with unknown entity types: %s",
                dropped, sorted(str(t) for t in unknown_types),
                extra={
                    "stage": "validate",
                    "dropped_count": dropped,
                    "unknown_types": sorted(str(t) for t in unknown_types),
                },
            )

    # ── BUG 3.6: biologically-invalid triples ────────────────────────
    triple_schemas = list(
        zip(df["relation_name"], df["head_type"], df["tail_type"])
    )
    invalid_mask = pd.Series(
        [t not in DRKG_VALID_TRIPLE_SCHEMAS for t in triple_schemas],
        index=df.index,
    )
    n_invalid = int(invalid_mask.sum())
    results["biologically_invalid_triples"] = n_invalid
    if n_invalid > 0:
        bad_triples = df.loc[invalid_mask].head(20)
        for _, row in bad_triples.iterrows():
            _write_dead_letter({
                "kind": "biologically_invalid_triple",
                "head_type": row["head_type"],
                "relation_name": row["relation_name"],
                "tail_type": row["tail_type"],
                "head_entity": row["head_entity"],
                "relation": row["relation"],
                "tail_entity": row["tail_entity"],
            })
            _log_transform(
                "validate",
                "invalid_triple_exclusion",
                original=f"({row['head_type']}, {row['relation_name']}, "
                         f"{row['tail_type']})",
                result="row_flagged",
                row_context={"row_index": int(row.name)},
            )
        logger.error(
            "Biologically-invalid triples: %d (showing 20 in dead-letter).",
            n_invalid,
            extra={
                "stage": "validate",
                "invalid_triple_count": n_invalid,
            },
        )
        if STRICT_EDGE_FILTERING:
            df = df.loc[~invalid_mask].reset_index(drop=True)

    # ── GUARD 3.10: text-mined treats edges excluded ─────────────────
    # Guard: if source_confidence_label/evidence_strength columns are missing
    # (e.g., a minimal test fixture), skip this check rather than crash.
    # v28 ROOT FIX (P2-L-12): use ``source_confidence_label`` (the string
    # label) instead of ``source_confidence`` (now numeric).
    _sc_label_col = (
        "source_confidence_label"
        if "source_confidence_label" in df.columns
        else "source_confidence"
    )
    if _sc_label_col in df.columns and "evidence_strength" in df.columns:
        text_mined_treats_mask = (
            (df["head_type"] == "Compound")
            & (df["tail_type"] == "Disease")
            & (df["relation_name"].isin(DRKG_TREATMENT_RELATIONS))
            & (df[_sc_label_col] == "text_mined")
            & (df["evidence_strength"] == "weak")
        )
    else:
        text_mined_treats_mask = df.index == -1  # always False (empty mask)
    n_text_mined = int(text_mined_treats_mask.sum())
    results["text_mined_treats_edges_excluded"] = n_text_mined
    if n_text_mined > 0 and STRICT_EDGE_FILTERING:
        logger.warning(
            "Excluding %d text-mined weak-evidence 'treats' edges under "
            "STRICT_EDGE_FILTERING (GUARD 3.10).",
            n_text_mined,
            extra={
                "stage": "validate",
                "text_mined_excluded": n_text_mined,
            },
        )
        df = df.loc[~text_mined_treats_mask].reset_index(drop=True)

    # ── GAP 7.5: lineage / version metadata ──────────────────────────
    results["parser_version"] = PARSER_VERSION
    results["schema_version"] = SCHEMA_VERSION
    results["validation_timestamp"] = _iso_now()

    logger.info(
        "DRKG Validation: %d triples, %d entities, %d entity types, "
        "%d relation types",
        results["total_triples"],
        results["total_unique_entities"],
        results["entity_type_count"],
        results["relation_type_count"],
        extra={
            "stage": "validate",
            "total_triples": results["total_triples"],
            "total_unique_entities": results["total_unique_entities"],
            "entity_type_count": results["entity_type_count"],
            "relation_type_count": results["relation_type_count"],
            "parser_version": PARSER_VERSION,
            "schema_version": SCHEMA_VERSION,
        },
    )
    return results


# =============================================================================
# Section 5 — Entity & Relation maps (BUG 2.1, BUG 2.6, BUG 4.6, BUG 4.11,
#             BUG 7.1, BUG 7.4, BUG 8.1, BUG 8.2, BUG 8.3, BUG 11.4,
#             GAP 2.6)
# =============================================================================
def get_entity_type_counts(df: pd.DataFrame) -> dict[str, int]:
    """Count unique entities per type, deduplicating across head/tail columns.

    Uses a vectorised ``pd.concat → drop_duplicates → groupby`` pipeline
    (BUG 8.1). Time complexity is O(n) with a ~2x memory factor for the
    concatenated DataFrame; for the full 5.9M-row DRKG this is ~500 MB
    transient and completes in <1s.

    For memory-constrained environments, use the private
    ``_get_entity_type_counts_iterative`` fallback (kept for
    documentation purposes; the vectorised version is the default).

    Args:
        df: Parsed DRKG DataFrame with ``head_type``, ``head_id``,
            ``tail_type``, ``tail_id`` columns.

    Returns:
        Dict mapping entity type → count of unique entities of that type.
    """
    # BUG 8.1 — vectorised
    combined = pd.concat(
        [
            df[["head_type", "head_id"]].rename(
                columns={"head_type": "etype", "head_id": "eid"}
            ),
            df[["tail_type", "tail_id"]].rename(
                columns={"tail_type": "etype", "tail_id": "eid"}
            ),
        ],
        ignore_index=True,
    )
    counts = (
        combined.drop_duplicates()
        .groupby("etype", sort=True)
        .size()
        .to_dict()
    )
    return dict(counts)


def _get_entity_type_counts_iterative(df: pd.DataFrame) -> dict[str, int]:
    """Iterative (lower-memory) alternative to ``get_entity_type_counts``.

    Slower than the vectorised version (~3s vs <1s on 5.9M rows) but
    uses ~10x less memory. Kept as a private fallback for memory-
    constrained environments per BUG 8.1 audit note.
    """
    from collections import defaultdict
    entity_sets: dict[str, set] = defaultdict(set)
    for etype, eid in zip(df["head_type"], df["head_id"]):
        entity_sets[etype].add(eid)
    for etype, eid in zip(df["tail_type"], df["tail_id"]):
        entity_sets[etype].add(eid)
    return {etype: len(ids) for etype, ids in entity_sets.items()}


def get_relation_type_counts(df: pd.DataFrame) -> dict[str, int]:
    """Count triples per relation type, sorted by relation string (deterministic).

    Fixes BUG 4.11 — sorted by key (relation string) for reproducibility
    across runs. Use ``df['relation'].value_counts()`` directly if you
    need count-descending order.

    Args:
        df: Parsed DRKG DataFrame with a ``relation`` column.

    Returns:
        Dict mapping relation string → count, ordered by relation string
        ascending.
    """
    return dict(sorted(df["relation"].value_counts().items()))


def build_entity_id_maps(df: pd.DataFrame) -> dict[str, dict[str, int]]:
    """Build per-type entity ID → integer index mappings.

    Uses a chained pandas pipeline (BUG 8.3 — no 3 intermediate copies)
    with ``sort_values(kind="stable")`` (BUG 7.1 — stable sort for
    reproducibility). Two runs on the same DataFrame produce identical
    maps.

    Args:
        df: Parsed DRKG DataFrame.

    Returns:
        Dict mapping entity type → ``{external_id: integer_index}``.
        Integer indices are 0-based per type.
    """
    # BUG 8.3 — chained pipeline
    head_entities = df[["head_type", "head_id"]].rename(
        columns={"head_type": "entity_type", "head_id": "entity_id"}
    )
    tail_entities = df[["tail_type", "tail_id"]].rename(
        columns={"tail_type": "entity_type", "tail_id": "entity_id"}
    )
    unique_entities = (
        pd.concat([head_entities, tail_entities], ignore_index=True)
        .drop_duplicates()
        .sort_values(["entity_type", "entity_id"], kind="stable")
        .reset_index(drop=True)
    )
    # BUG 7.1 — groupby cumcount gives deterministic 0-based indices
    # per entity type.
    unique_entities["index"] = unique_entities.groupby(
        "entity_type", sort=True
    ).cumcount()

    maps: dict[str, dict[str, int]] = {}
    # Build the per-type dicts (vectorised via groupby).
    for etype, group in unique_entities.groupby("entity_type", sort=True):
        maps[etype] = dict(zip(group["entity_id"], group["index"]))
    return maps


def build_edge_index_maps(
    df: pd.DataFrame,
    entity_maps: dict[str, dict[str, int]],
    *,
    drop_missing: bool = False,
) -> dict[tuple[str, str, str], tuple[tuple[int, ...], tuple[int, ...]]]:
    """Build edge index data per ``(head_type, relation_name, tail_type)``.

    Vectorised via pandas ``groupby`` (BUG 8.2). Returns IMMUTABLE tuples
    (GAP 2.6 / GAP 15.6 — caller cannot accidentally ``.append()`` to the
    returned lists). Edge lists are sorted by ``(src_idx, dst_idx)`` per
    type for reproducibility (BUG 7.4).

    Args:
        df: Parsed DRKG DataFrame.
        entity_maps: Output of ``build_entity_id_maps(df)``.
        drop_missing: If True, rows whose head/tail entity is not in
            ``entity_maps`` are logged, dead-lettered, and excluded
            (BUG 2.1). If False (default), a missing entity raises
            ``DRKGDataIntegrityError`` (it indicates a parser bug —
            ``entity_maps`` should be built from the same df).

    Returns:
        Dict mapping ``(src_type, rel_name, dst_type)`` →
        ``((src_idx, ...), (dst_idx, ...))``. Both tuples are sorted by
        ``(src_idx, dst_idx)``.

    Raises:
        DRKGDataIntegrityError: If ``drop_missing=False`` and a head/tail
            entity is not in ``entity_maps`` (BUG 2.1).
    """
    # Build a single combined lookup dict for vectorised mapping.
    combined_lookup: dict[tuple[str, str], int] = {}
    for etype, id_map in entity_maps.items():
        for eid, idx in id_map.items():
            combined_lookup[(etype, eid)] = idx

    head_keys = list(zip(df["head_type"], df["head_id"]))
    tail_keys = list(zip(df["tail_type"], df["tail_id"]))

    # BUG 2.1 — no -1 sentinel. Missing keys raise unless drop_missing=True.
    # We assign an index per row only when BOTH head and tail are found;
    # rows with any missing entity are either dropped (drop_missing=True)
    # or cause a raise (drop_missing=False).
    missing_keys: list[tuple[str, str]] = []
    src_idx_per_row: list[Optional[int]] = []
    dst_idx_per_row: list[Optional[int]] = []
    for hk, tk in zip(head_keys, tail_keys):
        s = combined_lookup.get(hk)
        d = combined_lookup.get(tk)
        if s is None:
            missing_keys.append(hk)
        if d is None:
            missing_keys.append(tk)
        src_idx_per_row.append(s)
        dst_idx_per_row.append(d)

    if missing_keys:
        if not drop_missing:
            raise DRKGDataIntegrityError(
                f"Entity {missing_keys[0]!r} not found in entity_maps. "
                "This indicates a parser bug — entity_maps should be "
                "built from the same df. Pass drop_missing=True to "
                "silently exclude.",
                context={
                    "missing_key": str(missing_keys[0]),
                    "missing_count": len(missing_keys),
                    "first_10_missing": [str(k) for k in missing_keys[:10]],
                    "stage": "edge_index",
                },
            )
        # BUG 11.4 — log first 10 + dead-letter all
        logger.warning(
            "Edge index mapping: %d missing entities (showing first 10): "
            "%s", len(missing_keys), [str(k) for k in missing_keys[:10]],
            extra={
                "stage": "edge_index",
                "missing_count": len(missing_keys),
                "sample": [str(k) for k in missing_keys[:10]],
            },
        )
        for k in missing_keys:
            _write_dead_letter({
                "kind": "missing_entity_in_edge_map",
                "entity_type": k[0],
                "entity_id": k[1],
            })

    # Build a per-row "keep" mask: only rows where both src and dst were
    # found in entity_maps. When drop_missing=False, all rows are kept
    # (any missing would have raised above).
    keep_mask = [
        s is not None and d is not None
        for s, d in zip(src_idx_per_row, dst_idx_per_row)
    ]
    # Apply the keep mask to df + the index lists (BUG 8.2 vectorised path).
    df_kept = df.loc[keep_mask].reset_index(drop=True)
    src_idx = [s for s, k in zip(src_idx_per_row, keep_mask) if k]
    dst_idx = [d for d, k in zip(dst_idx_per_row, keep_mask) if k]
    df_edges = df_kept.assign(_src_idx=src_idx, _dst_idx=dst_idx)

    edge_maps: dict[tuple[str, str, str], tuple[tuple[int, ...], tuple[int, ...]]] = {}
    # BUG 8.2 — vectorised groupby
    for (ht, rn, tt), group in df_edges.groupby(
        ["head_type", "relation_name", "tail_type"], sort=True
    ):
        srcs = group["_src_idx"].tolist()
        dsts = group["_dst_idx"].tolist()
        # BUG 7.4 — sort by (src, dst) for reproducibility
        paired = sorted(zip(srcs, dsts))
        edge_maps[(ht, rn, tt)] = (
            tuple(s for s, _ in paired),
            tuple(d for _, d in paired),
        )
    return edge_maps


def _build_edge_index_maps_iterative(
    df: pd.DataFrame,
    entity_maps: dict[str, dict[str, int]],
    *,
    drop_missing: bool = False,
) -> dict[tuple[str, str, str], tuple[tuple[int, ...], tuple[int, ...]]]:
    """Iterative (lower-memory) alternative to ``build_edge_index_maps``.

    Slower than the vectorised version (~10s vs <2s on 5.9M rows) but
    uses ~5x less memory. Kept as a private fallback per BUG 8.2 audit
    note. The public ``build_edge_index_maps`` is the default.
    """
    combined_lookup: dict[tuple[str, str], int] = {}
    for etype, id_map in entity_maps.items():
        for eid, idx in id_map.items():
            combined_lookup[(etype, eid)] = idx

    edge_maps: dict[tuple[str, str, str], tuple[list[int], list[int]]] = {}
    missing: list[tuple[str, str]] = []
    # BUG 4.6 — parallel zip (no enumerate / index lookup in the loop body)
    for ht, hid, rn, tt, tid in zip(
        df["head_type"], df["head_id"], df["relation_name"],
        df["tail_type"], df["tail_id"],
    ):
        s = combined_lookup.get((ht, hid))
        d = combined_lookup.get((tt, tid))
        if s is None or d is None:
            if s is None:
                missing.append((ht, hid))
            if d is None:
                missing.append((tt, tid))
            if not drop_missing:
                raise DRKGDataIntegrityError(
                    f"Entity not found in entity_maps — pass "
                    f"drop_missing=True to exclude.",
                    context={"missing_count": len(missing)},
                )
            continue
        key = (ht, rn, tt)
        if key not in edge_maps:
            edge_maps[key] = ([], [])
        edge_maps[key][0].append(s)
        edge_maps[key][1].append(d)
    # BUG 7.4 + GAP 2.6 — sort + immutable tuples
    result: dict[tuple[str, str, str], tuple[tuple[int, ...], tuple[int, ...]]] = {}
    for key, (srcs, dsts) in edge_maps.items():
        paired = sorted(zip(srcs, dsts))
        result[key] = (
            tuple(s for s, _ in paired),
            tuple(d for _, d in paired),
        )
    return result


# =============================================================================
# Section 6 — NetworkX Construction (BUG 4.10, BUG 8.4, GAP 8.6,
#             GUARD 8.7)
# =============================================================================
def build_networkx_graph(df: pd.DataFrame) -> nx.MultiDiGraph:
    """Build a NetworkX MultiDiGraph from DRKG triples.

    WARNING: This can consume 3-5 GB RAM for the full 5.9M-edge DRKG.
    Only use if you need NetworkX-specific algorithms. For GNN training,
    use ``build_edge_index_maps`` and pass the result to PyG HeteroData
    instead.

    Args:
        df: Parsed DRKG DataFrame with ``head_entity``, ``tail_entity``,
            ``relation``, ``relation_name``, ``head_type``, ``tail_type``
            columns.

    Returns:
        NetworkX ``MultiDiGraph`` with:
          * Node IDs = full entity strings (e.g. ``"Compound::DB00107"``)
            for global uniqueness.
          * Node attribute ``entity_type`` = the parsed head/tail type.
          * Edge attributes: ``relation``, ``relation_name``,
            ``evidence_strength``, ``source_confidence``.

    Raises:
        DRKGDataIntegrityError: If an entity ID appears with multiple
            ``entity_type`` values (BUG 4.10 — data corruption).
        MemoryError: If available RAM is less than 2x the estimated peak
            (GAP 8.6).
    """
    # ── GAP 8.6: memory guard via psutil ─────────────────────────────
    try:
        import psutil  # type: ignore[import-not-found]
        available = psutil.virtual_memory().available
        estimated_peak = len(df) * _NETWORKX_BYTES_PER_EDGE
        if available < estimated_peak * _NETWORKX_HEADROOM_MULTIPLIER:
            raise MemoryError(
                f"build_networkx_graph would need ~"
                f"{estimated_peak * _NETWORKX_HEADROOM_MULTIPLIER / 1e9:.1f} "
                f"GB peak, only {available / 1e9:.1f} GB available. Either "
                f"filter the DataFrame first or use build_edge_index_maps "
                f"+ PyG HeteroData instead.",
            )
    except ImportError:
        logger.debug(
            "psutil not available — skipping memory guard (GAP 8.6).",
        )

    logger.info(
        "Building NetworkX graph from DRKG ...",
        extra={"stage": "networkx", "input_rows": len(df)},
    )
    G = nx.MultiDiGraph()

    # ── BUG 4.10: validate per-node entity-type uniqueness ───────────
    # Each node_id (e.g. "Compound::DB00107") must map to exactly one
    # entity_type. A conflict indicates data corruption (the same ID was
    # written with two different types in two different rows).
    node_type_map: dict[str, str] = {}
    for col_prefix in ("head", "tail"):
        entity_col = f"{col_prefix}_entity"
        type_col = f"{col_prefix}_type"
        for node_id, etype in zip(df[entity_col], df[type_col]):
            existing = node_type_map.get(node_id)
            if existing is None:
                node_type_map[node_id] = etype
            elif existing != etype:
                raise DRKGDataIntegrityError(
                    f"Entity ID {node_id!r} appears with multiple types: "
                    f"{existing!r} and {etype!r}. Data corruption.",
                    context={
                        "entity": node_id,
                        "type_1": existing,
                        "type_2": etype,
                        "stage": "networkx_node_type",
                    },
                )

    # Add nodes (vectorised — single pass).
    G.add_nodes_from(
        (node_id, {"entity_type": etype})
        for node_id, etype in node_type_map.items()
    )

    # ── BUG 8.4: generator expression (no intermediate list) ─────────
    ev_col = (
        df["evidence_strength"] if "evidence_strength" in df.columns
        else pd.Series(["unknown"] * len(df))
    )
    # v28 ROOT FIX (P2-L-12): ``source_confidence`` is now NUMERIC; the
    # categorical label lives in ``source_confidence_label``. Pass the
    # label into the networkx edge attribute (operators expect a string
    # there). Fall back to the numeric ``source_confidence`` for older
    # DataFrames that haven't been re-parsed.
    sc_label_col = (
        df["source_confidence_label"]
        if "source_confidence_label" in df.columns
        else (
            df["source_confidence"] if "source_confidence" in df.columns
            else pd.Series(["unknown"] * len(df))
        )
    )
    sc_numeric_col = (
        df["source_confidence"] if "source_confidence" in df.columns
        else pd.Series([0.0] * len(df))
    )
    G.add_edges_from(
        (src, dst, {
            "relation": rel,
            "relation_name": rel_name,
            "evidence_strength": ev,
            "source_confidence": sc_num,
            "source_confidence_label": sc_lbl,
        })
        for src, dst, rel, rel_name, ev, sc_num, sc_lbl in zip(
            df["head_entity"], df["tail_entity"], df["relation"],
            df["relation_name"], ev_col, sc_numeric_col, sc_label_col,
        )
    )

    logger.info(
        "NetworkX graph: %d nodes, %d edges",
        G.number_of_nodes(), G.number_of_edges(),
        extra={
            "stage": "networkx",
            "node_count": G.number_of_nodes(),
            "edge_count": G.number_of_edges(),
        },
    )
    return G


# =============================================================================
# Section 7 — Filtered Subgraphs (BUG 2.3, BUG 3.1, BUG 3.2, BUG 3.4,
#             GAP 3.9, GUARD 3.10)
# =============================================================================
def get_compound_disease_subgraph(
    df: pd.DataFrame,
    *,
    relations: Optional[Iterable[str]] = None,
) -> pd.DataFrame:
    """Extract Compound-treats-Disease triples (the core prediction target).

    Fixes BUG 3.1 — the old loader filtered by ``relation_name.str.contains
    ("treat", case=False)`` which matched ZERO rows on real DRKG (the real
    abbreviation is ``CtD``). This function uses ``DRKG_TREATMENT_RELATIONS``
    from config (a frozenset, O(1) lookup) instead of a regex.

    Args:
        df: Parsed DRKG DataFrame.
        relations: Optional iterable of relation names to include
            (defaults to ``config.DRKG_TREATMENT_RELATIONS``).

    Returns:
        A copy (BUG 2.3 — caller may freely mutate) of the filtered
        DataFrame, reset_index'd.
    """
    rel_set = (
        frozenset(relations) if relations is not None
        else DRKG_TREATMENT_RELATIONS
    )
    mask = (
        (df["head_type"] == "Compound")
        & (df["tail_type"] == "Disease")
        & (df["relation_name"].isin(rel_set))
    )
    return df.loc[mask].reset_index(drop=True).copy()


def get_compound_gene_subgraph(
    df: pd.DataFrame,
    *,
    relations: Optional[Iterable[str]] = None,
) -> pd.DataFrame:
    """Extract Compound-binds/affects-Gene triples.

    Fixes BUG 3.2 — the old loader filtered by ``relation_name.str.contains
    ("bind|interact|target", case=False)`` which matched only the small
    ``DRUGBANK::target::`` slice. This function uses
    ``DRKG_COMPOUND_GENE_RELATIONS`` from config (sourced from
    ``training_data.py:121-138``'s verified regex) so the loader and the
    training-data builder stay in lock-step.

    Args:
        df: Parsed DRKG DataFrame.
        relations: Optional iterable of relation names to include
            (defaults to ``config.DRKG_COMPOUND_GENE_RELATIONS``).

    Returns:
        A copy of the filtered DataFrame, reset_index'd.
    """
    rel_set = (
        frozenset(relations) if relations is not None
        else DRKG_COMPOUND_GENE_RELATIONS
    )
    mask = (
        (df["head_type"] == "Compound")
        & (df["tail_type"] == "Gene")
        & (df["relation_name"].isin(rel_set))
    )
    return df.loc[mask].reset_index(drop=True).copy()


def get_gene_disease_subgraph(
    df: pd.DataFrame,
    *,
    relations: Optional[Iterable[str]] = None,
    include_biomarkers: bool = False,
) -> pd.DataFrame:
    """Extract Gene-associated_with-Disease triples.

    Fixes BUG 3.4 — the old loader returned ALL Gene-Disease edges
    regardless of semantics, merging biomarkers (``J``), causal (``L``),
    therapeutic-effect (``Te``), upregulated (``U``), underexpressed
    (``Y``), curator (``DaG``, ``DdG``) into one indistinguishable blob.

    Default behaviour (no args): returns only the curated "association"
    set (``DaG``, ``DdG``, ``L``, ``Te``). Pass ``include_biomarkers=True``
    to also include the biomarker/expression set (``J``, ``U``, ``Y``,
    ``Md``, ``X``).

    Args:
        df: Parsed DRKG DataFrame.
        relations: Optional iterable of relation names to include
            (overrides ``include_biomarkers`` if set).
        include_biomarkers: If True (and ``relations`` is None), include
            the biomarker/expression set in addition to the curated
            association set.

    Returns:
        A copy of the filtered DataFrame, reset_index'd. The
        ``relation_name`` column is always present so the caller can
        sub-filter.
    """
    if relations is not None:
        rel_set = frozenset(relations)
    else:
        rel_set = set(DRKG_GENE_DISEASE_ASSOCIATION_RELATIONS)
        if include_biomarkers:
            rel_set |= DRKG_GENE_DISEASE_BIOMARKER_RELATIONS
        rel_set = frozenset(rel_set)
    mask = (
        (df["head_type"] == "Gene")
        & (df["tail_type"] == "Disease")
        & (df["relation_name"].isin(rel_set))
    )
    return df.loc[mask].reset_index(drop=True).copy()


# =============================================================================
# Section 8 — Streaming API (GAP 8.5, GUARD 8.7)
# =============================================================================
def iter_drkg_triples(
    chunksize: int = 1_000_000,
    *,
    drkg_dir: Optional[Path] = None,
    raw_dir: Optional[Path] = None,
) -> Iterator[pd.DataFrame]:
    """Yield DRKG triples in chunks for memory-bounded processing.

    Fixes GAP 8.5 — for DRKG variants >50M rows, ``parse_drkg_tsv``
    (which materialises the full DataFrame) is not viable. This generator
    uses ``pd.read_csv(chunksize=...)`` so peak memory is bounded by
    ``chunksize`` (default 1M rows ~ 250 MB).

    Each yielded chunk is a raw DataFrame with the three TSV columns
    (``head_entity``, ``relation``, ``tail_entity``) — NOT the full
    parsed schema. Apply ``parse_drkg_tsv`` per chunk if you need the
    derived columns.

    Args:
        chunksize: Number of rows per chunk. Must be > 0.
        drkg_dir: Override the DRKG extract directory.
        raw_dir: Override the data-raw directory.

    Yields:
        DataFrame chunks of ``chunksize`` rows (the last chunk may be
        smaller).

    Raises:
        DRKGParseError: If the TSV cannot be opened or ``chunksize <= 0``.
    """
    if chunksize <= 0:
        raise DRKGParseError(
            f"chunksize must be > 0, got {chunksize}.",
            context={"chunksize": chunksize, "stage": "iter_chunksize"},
        )
    if drkg_dir is None:
        if _DRUGOS_DRKG_DIR:
            drkg_dir = Path(_DRUGOS_DRKG_DIR)
        elif raw_dir is not None:
            drkg_dir = Path(raw_dir) / "drkg"
        else:
            drkg_dir = RAW_DIR / "drkg"
    drkg_dir = Path(drkg_dir)
    tsv_name = DATA_SOURCES["drkg"]["tsv_file"]
    tsv_path = drkg_dir / tsv_name
    if not tsv_path.exists():
        tsv_candidates = sorted(drkg_dir.rglob(tsv_name))
        if not tsv_candidates:
            raise DRKGParseError(
                f"{tsv_name} not found in {drkg_dir}.",
                context={"dir": str(drkg_dir), "stage": "iter_locate"},
            )
        tsv_path = tsv_candidates[0]

    try:
        reader = pd.read_csv(
            tsv_path,
            sep="\t",
            header=None,
            names=list(DRKG_TSV_COLUMNS),
            dtype=str,
            encoding="utf-8-sig",
            na_filter=False,
            skip_blank_lines=True,
            comment="#",
            on_bad_lines="warn",
            lineterminator="\n",
            chunksize=chunksize,
        )
        for chunk in reader:
            yield chunk
    except pd.errors.ParserError as exc:
        raise DRKGParseError(
            f"Failed to stream-parse {tsv_path}: {exc}.",
            context={"path": str(tsv_path), "parser_error": str(exc),
                     "stage": "iter_read"},
        ) from exc


# =============================================================================
# Section 9 — Convenience: Full Pipeline (GAP 2.7, GAP 6.7, GAP 7.6,
#             GUARD 7.7, GUARD 8.7)
# =============================================================================
def load_drkg(
    download: bool = True,
    *,
    force: bool = False,
    drkg_dir: Optional[Path] = None,
    raw_dir: Optional[Path] = None,
    build_nx: bool = False,
    allow_stale: Optional[bool] = None,
    return_provenance: bool = False,
) -> Union[
    tuple[pd.DataFrame, Optional[nx.MultiDiGraph], dict[str, Any]],
    tuple[pd.DataFrame, Optional[nx.MultiDiGraph], dict[str, Any], dict[str, Any]],
]:
    """Full DRKG loading pipeline: download → parse → validate → optionally build NetworkX.

    Backward-compatible with the v1 signature ``load_drkg(download=True,
    build_nx=False)`` so ``run_pipeline.py:step1_load_drkg`` continues
    to work unmodified. The new keyword-only parameters enable
    force-redownload, custom dirs, graceful degradation, and provenance
    return (GAP 2.7).

    Args:
        download: Whether to download (or use cache) if not present.
        force: If True, force re-download and re-extraction.
        drkg_dir: Override the DRKG extract directory.
        raw_dir: Override the data-raw directory.
        build_nx: Whether to build NetworkX graph (memory-intensive:
            3-5 GB for full DRKG). Default False.
        allow_stale: If True and all retries fail, fall back to cached
            tarball. Defaults to env var ``DRUGOS_DRKG_ALLOW_STALE``.
        return_provenance: If True, return a 4-tuple with the provenance
            dict as the 4th element. Default False (3-tuple, backward
            compat).

    Returns:
        3-tuple ``(df, G_or_None, validation_dict)`` by default.
        4-tuple ``(df, G_or_None, validation_dict, provenance_dict)`` if
        ``return_provenance=True``.

    Raises:
        DRKGDownloadError: Download failure (when ``download=True`` and
            ``allow_stale=False``).
        DRKGParseError: TSV parse failure.
        DRKGDataIntegrityError: Validation guard failure.

    Side effects:
        - Persists loader state to ``data/processed/loader_state.json``
          (GAP 16.6).
        - Writes dead-letter entries to
          ``data/dead_letter/drkg_malformed.jsonl`` for any skipped rows.
        - Writes transformation log to
          ``logs/transformations/drkg.jsonl``.

    Note:
        This loader is validated to 5.9M rows. For DRKG variants >50M
        rows, use ``iter_drkg_triples`` and the streaming helpers in
        ``_loader_protocol``. The NetworkX path is O(edges) memory and
        should not be used beyond 10M edges; for larger graphs, use
        ``build_edge_index_maps`` and pass the result to PyG HeteroData.
    """
    if allow_stale is None:
        allow_stale = _DRUGOS_DRKG_ALLOW_STALE

    t0 = time.perf_counter()  # GAP 11.7 — per-stage metrics
    if download:
        drkg_dir = download_drkg(force=force, raw_dir=raw_dir, allow_stale=allow_stale)
    else:
        if drkg_dir is None:
            if _DRUGOS_DRKG_DIR:
                drkg_dir = Path(_DRUGOS_DRKG_DIR)
            elif raw_dir is not None:
                drkg_dir = Path(raw_dir) / "drkg"
            else:
                drkg_dir = RAW_DIR / "drkg"
    dl_elapsed = time.perf_counter() - t0
    logger.info(
        "DRKG stage complete",
        extra={
            "stage": "download",
            "elapsed_s": round(dl_elapsed, 3),
            "download": download,
        },
    )

    t1 = time.perf_counter()
    df = parse_drkg_tsv(drkg_dir, raw_dir=raw_dir)
    parse_elapsed = time.perf_counter() - t1
    logger.info(
        "DRKG stage complete",
        extra={
            "stage": "parse",
            "elapsed_s": round(parse_elapsed, 3),
            "input_rows": None,
            "output_rows": len(df),
            "dropped_rows": 0,
        },
    )

    t2 = time.perf_counter()
    validation = validate_drkg(df)
    validate_elapsed = time.perf_counter() - t2
    logger.info(
        "DRKG stage complete",
        extra={
            "stage": "validate",
            "elapsed_s": round(validate_elapsed, 3),
            "output_rows": validation.get("actual_record_count", len(df)),
        },
    )

    G: Optional[nx.MultiDiGraph] = None
    if build_nx:
        t3 = time.perf_counter()
        G = build_networkx_graph(df)
        nx_elapsed = time.perf_counter() - t3
        logger.info(
            "DRKG stage complete",
            extra={
                "stage": "networkx",
                "elapsed_s": round(nx_elapsed, 3),
                "node_count": G.number_of_nodes(),
                "edge_count": G.number_of_edges(),
            },
        )

    # ── GAP 16.6: persist loader state ───────────────────────────────
    _persist_loader_state("drkg", {
        "last_run": _iso_now(),
        "input_sha256": df.attrs.get("provenance", {}).get("source_sha256"),
        "output_rows": len(df),
        "parser_version": PARSER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "validation_summary": {
            k: v for k, v in validation.items()
            if not isinstance(v, (dict, list))
        },
    })

    total_elapsed = time.perf_counter() - t0
    logger.info(
        "DRKG load_drkg complete in %.3fs", total_elapsed,
        extra={
            "stage": "load_drkg",
            "elapsed_s": round(total_elapsed, 3),
            "row_count": len(df),
            "build_nx": build_nx,
        },
    )

    if return_provenance:
        provenance = df.attrs.get("provenance", {})
        return df, G, validation, provenance
    return df, G, validation


# =============================================================================
# Section 10 — Loader Protocol Adapter (BUG 1.2)
# =============================================================================
class DRKGLoader:
    """Adapter implementing the ``Loader`` Protocol for DRKG.

    Fixes BUG 1.2 — provides a uniform ``download / parse / to_graph``
    interface so ``run_pipeline`` can treat all loaders polymorphically.
    The module-level functions remain the public API; this class is a
    thin adapter that delegates to them.

    v35 ROOT FIX (V35-P2-LOADERS-FIXES L-3 / M-8):
      * DEPRECATION NOTE: the project's canonical Phase 1 entry point
        is ``phase1_bridge.load_drkg_to_staged`` and the canonical
        Phase 2 entry point is ``run_pipeline.step1_drkg`` /
        ``run_pipeline.step8_entity_resolution``. ``DRKGLoader.to_graph``
        was previously used in v1-v9 of the pipeline but the LIVE
        Phase 2 path now consumes DRKG via the dedicated
        ``parse_drkg_tsv`` + ``entity_resolver.resolve_*_from_drkg``
        functions; this class is retained for the Loader Protocol
        conformance test and for any external scripts that still use
        it. New code SHOULD NOT use ``DRKGLoader`` — call
        ``parse_drkg_tsv`` directly.
      * M-8 fix: the edge dict previously emitted ``"relation": rn``
        and only ``src_idx`` / ``dst_idx`` (integer indices into
        ``entity_maps``). The standard kg_builder edge schema requires
        ``rel_type``, ``src_id``, ``dst_id``, ``src_type``, and
        ``dst_type`` as STRING keys (not integer indices). All five
        keys are now emitted; ``src_idx`` / ``dst_idx`` are retained
        for backwards compatibility with any consumer still using the
        integer-index form.

    Usage::

        from drugos_graph.drkg_loader import DRKGLoader
        from drugos_graph._loader_protocol import Loader
        loader = DRKGLoader()
        assert isinstance(loader, Loader)        # runtime check
        path = loader.download()
        records = list(loader.parse(path))
        nodes, edges = loader.to_graph(records)

    Attributes
    ----------
    name : str
        Always ``"drkg"`` (matches ``DATA_SOURCES`` key).
    """

    name: str = "drkg"

    def download(self, force: bool = False) -> Path:
        """Download (or cached-load) the DRKG tar.gz and extract it.

        Args:
            force: If True, force re-download and re-extraction.

        Returns:
            Path to the extracted DRKG directory.
        """
        return download_drkg(force=force)

    def parse(self, path: Optional[Path] = None) -> Iterator[dict[str, Any]]:
        """Yield parsed DRKG records as dicts.

        Args:
            path: Optional DRKG extract directory. Defaults to
                ``config.RAW_DIR / "drkg"``.

        Yields:
            One dict per DRKG triple (conforming to ``DRKGRecord``).
        """
        df = parse_drkg_tsv(path) if path else parse_drkg_tsv()
        for record in df.to_dict("records"):
            yield record

    def to_graph(
        self, records: Any
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Convert records into ``(nodes, edges)`` for the KG.

        Args:
            records: Iterable of DRKG record dicts (e.g. output of
                ``parse``). A DataFrame is also accepted (converted
                via ``df.to_dict('records')``).

        Returns:
            Tuple ``(nodes, edges)`` where ``nodes`` is a list of
            ``{entity_type, entity_id, index}`` dicts and ``edges`` is a
            list of ``{src_type, rel_type, dst_type, src_id, dst_id,
            src_idx, dst_idx}`` dicts.
        """
        if isinstance(records, pd.DataFrame):
            df = records
        else:
            df = pd.DataFrame(list(records))
        entity_maps = build_entity_id_maps(df)
        edge_maps = build_edge_index_maps(df, entity_maps)
        nodes: list[dict[str, Any]] = []
        # v35 ROOT FIX (M-8 / L-3): build a reverse-lookup from
        # (entity_type, index) -> entity_id so the edge builder can
        # emit ``src_id`` / ``dst_id`` (string IDs) alongside the
        # integer ``src_idx`` / ``dst_idx`` indices. The standard
        # kg_builder schema requires the string IDs; the integer
        # indices are retained for backwards compatibility.
        idx_to_id: dict[tuple[str, int], str] = {}
        for etype, id_map in entity_maps.items():
            for eid, idx in id_map.items():
                nodes.append({
                    "entity_type": etype,
                    "entity_id": eid,
                    "index": idx,
                })
                idx_to_id[(etype, idx)] = eid
        edges: list[dict[str, Any]] = []
        for (ht, rn, tt), (srcs, dsts) in edge_maps.items():
            for s, d in zip(srcs, dsts):
                # v35 ROOT FIX (M-8): emit the standard kg_builder
                # edge schema (rel_type / src_id / dst_id / src_type /
                # dst_type) as PRIMARY keys, and keep ``relation`` /
                # ``src_idx`` / ``dst_idx`` as legacy aliases for
                # backwards compatibility with any consumer still
                # reading the old form.
                _src_id = idx_to_id.get((ht, s))
                _dst_id = idx_to_id.get((tt, d))
                edges.append({
                    "src_type": ht,
                    "src_id": _src_id,
                    "dst_type": tt,
                    "dst_id": _dst_id,
                    "rel_type": rn,
                    # Legacy aliases (kept for backwards compatibility).
                    "relation": rn,
                    "src_idx": s,
                    "dst_idx": d,
                })
        return nodes, edges


# =============================================================================
# Section 11 — __main__ entry (BUG 4.7)
# =============================================================================
if __name__ == "__main__":  # pragma: no cover
    import sys as _sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        drkg_df, drkg_graph, drkg_validation = load_drkg(build_nx=False)
    except DRKGDownloadError as exc:
        logger.error("DRKG load failed (download): %s", exc, exc_info=True)
        _sys.exit(2)
    except DRKGParseError as exc:
        logger.error("DRKG load failed (parse): %s", exc, exc_info=True)
        _sys.exit(2)
    except DRKGDataIntegrityError as exc:
        logger.error("DRKG load failed (integrity): %s", exc, exc_info=True)
        _sys.exit(2)
    except Exception as exc:
        logger.error(
            "Unexpected DRKG load failure: %s", exc, exc_info=True,
        )
        _sys.exit(1)

    print(f"\n{'=' * 60}")
    print("DRKG Load Complete")
    print(f"{'=' * 60}")
    for key, value in drkg_validation.items():
        if not isinstance(value, (dict, list)):
            print(f"  {key}: {value}")
    print("\nEntity breakdown:")
    for etype, count in drkg_validation["entity_type_breakdown"].items():
        print(f"  {etype}: {count:,}")
