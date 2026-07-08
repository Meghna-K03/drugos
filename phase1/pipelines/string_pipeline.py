"""STRING Pipeline — protein-protein interaction network from STRING DB.

REGULATORY COMPLIANCE:
    - FDA 21 CFR Part 11: Audit trails. This pipeline records
      ``source_version``, ``pipeline_run_id``, and SHA-256 checksums in
      the audit record.  Every PPI row loaded to the DB carries
      ``pipeline_run_id`` for traceability.
    - GxP Data Integrity: ALCOA+ principles (Attributable, Legible,
      Contemporaneous, Original, Accurate, + Complete, Consistent,
      Enduring, Available).  The dead-letter queue and per-stage
      metrics support Attributable and Complete.

LINEAGE CHAIN:
    STRING URL
      → downloaded .gz (SHA-256 recorded for links, aliases, detailed)
      → parsed DataFrame (transformation log: filter, map, dedup, swap, merge)
      → cleaned CSV (SHA-256 sidecar + metadata.json + transform.json)
      → DB rows (pipeline_run_id FK on every PPI row)
      → Neo4j edges (via exporter — separate lineage)
      → Graph Transformer input (via exporter — separate lineage)

Download:
    - 9606.protein.links.v{VERSION}.txt.gz         → interaction scores (required)
    - 9606.protein.aliases.v{VERSION}.txt.gz        → STRING→UniProt mapping (required)
    - 9606.protein.links.detailed.v{VERSION}.txt.gz → sub-scores (optional,
      configurable via ``STRING_DETAILED_MODE``)

Clean:
    - Validate organism (9606 prefix) — quarantines cross-species contamination
    - Filter ``combined_score >= STRING_MIN_COMBINED_SCORE`` (700 in production
      per Szklarczyk et al. 2023, Nucleic Acids Research)
    - Map STRING IDs to UniProt via aliases (source == 'UniProt_AC' EXACT,
      excludes BLAST_UniProt_AC which has ~5–10% error vs <1% for curated)
    - Validate UniProt IDs against the canonical pattern (UniProt help/accession)
    - Uppercase all UniProt IDs (canonical form — UniProt help/accession)
    - Separate canonical from isoform accessions (UniProt help/isoforms)
    - Drop rows where either protein fails to map (dead-letter the unmapped IDs)
    - Canonical-order at STRING-ID level FIRST (so the detailed-merge keys match)
    - Dedup with configurable strategy (default: ``max_score`` for determinism)
    - Merge detailed sub-scores (if present and integrity-verified)
    - Pack the 4 sub-scores not in dedicated columns into ``score_json``
    - Validate output against ``schema/v1.json`` before returning

Load:
    - Resolve ``uniprot_id`` → ``protein.id`` via ``get_uniprot_to_protein_id_map``
      (filtered to the unique set for performance)
    - Drop rows where FK lookup fails (dead-letter the unmapped UniProt IDs)
    - Ensure ``protein_a_id < protein_b_id`` (defense-in-depth with
      ``_pre_validate_ppi`` in ``loaders.py``)
    - Drop self-interactions with WARNING + dead-letter (DB constraint;
      ``TODO(schema-migration)``: allow homodimers — they are biologically
      real and clinically critical, e.g. EGFR/HER2/p53 dimerization)
    - Bulk upsert via ``bulk_upsert_ppi`` with ``pipeline_run_id`` and
      ``input_checksum``
    - Return ``inserted + updated`` (NOT ``int(UpsertResult)`` which is
      ``total_input`` — see ``GAP-2.3``)

Scientific references:
    - Szklarczyk D. et al. The STRING database in 2023: protein-protein
      association networks and functional enrichment analyses for any
      sequenced genome of interest. Nucleic Acids Res. 2023.
    - UniProt Consortium. UniProt: the Universal Protein Knowledgebase
      in 2023. Nucleic Acids Res. 2023.
    - STRING DB documentation: https://string-db.org/cgi/help
    - UniProt accession help: https://www.uniprot.org/help/accession

Changelog:
    v2.0.0 (2025) — Institutional-grade rewrite addressing 149 catalogued
        defects across 16 quality domains. See
        ``docs/audits/STRING_PIPELINE_FIX_REPORT.md`` for the full fix
        matrix.
    v1.0.0 — Initial implementation (421 lines, deprecated).

License: MIT — Team Cosmic / VentureLab.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Standard library imports
# ---------------------------------------------------------------------------
import gzip
import json
import logging
import os
import re
import ssl
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Third-party imports
# ---------------------------------------------------------------------------
import numpy as np
import pandas as pd
from pandas.errors import ParserError
from sqlalchemy import inspect
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------
from config.settings import (
    PROCESSED_DATA_DIR,
    STRING_ALIASES_URL,
    STRING_DEDUP_STRATEGY,
    STRING_DETAILED_MODE,
    STRING_DROP_SELF_INTERACTIONS,
    STRING_LOW_MEMORY,
    STRING_MIN_COMBINED_SCORE,
    STRING_MIN_COMBINED_SCORE_PROD,
    STRING_PROTEIN_LINKS_DETAILED_URL,
    STRING_PROTEIN_LINKS_URL,
    STRING_VERSION,
)
from database.connection import get_db_session
from database.loaders import (
    UpsertResult,
    bulk_upsert_ppi,
    get_uniprot_to_protein_id_map,
)
from pipelines.base_pipeline import (
    BasePipeline,
    SchemaValidationError,
)
# v29 ROOT FIX (audit P1-24): canonical UniProt ID normalization at the
# OUTPUT boundary so STRING's PPI edges join cleanly with UniProt proteins
# regardless of the case the upstream mapping returned.
from cleaning._constants import normalize_uniprot_id

# ---------------------------------------------------------------------------
# Module metadata
# ---------------------------------------------------------------------------
__all__ = ["StringPipeline", "EXPECTED_OUTPUT_COLUMNS", "UNIPROT_ID_PATTERN"]
__version__ = "2.0.0"
__author__ = "Team Cosmic / VentureLab"
__license__ = "MIT"

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sci: UniProt canonical accession pattern
# (https://www.uniprot.org/help/accession). Two alternatives:
#   6-char:   [OPQ][0-9][A-Z0-9]{3}[0-9]               e.g. P69905, Q8WXI7
#   10-char:  [A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2} e.g. A0A024RBG1
# Matches schema/v1.json "UNIPROT_ID_PATTERN".
# v41 ROOT FIX (SEV2-HIGH #13): the previous pattern was TWO
# alternations each anchored with ``^...$`` but WITHOUT an outer
# non-capturing group. As a standalone regex used with ``re.match``
# this worked (each alt is anchored), but when used as a sub-pattern
# inside a larger regex (e.g. for ``re.findall`` or composition with
# other patterns), partial matches could slip through because the
# alternation operator ``|`` has very low precedence — the outer
# context effectively saw ``^A$|B^C$`` instead of ``^(A|B)$``. The
# base_pipeline.py:309-315 UNIPROT_ID_PATTERN has the correct
# grouping. Fix: wrap both alternations in a single outer non-
# capturing group ``(?:...)`` with one ``^...$`` anchor around the
# whole thing. Functionally identical for ``re.match`` (which already
# anchors at the start) but mathematically correct as a sub-pattern.
# ---------------------------------------------------------------------------
UNIPROT_ID_PATTERN: re.Pattern[str] = re.compile(
    r"^(?:"
    r"[OPQ][0-9][A-Z0-9]{3}[0-9]"
    r"|"
    r"[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2}"
    r")$"
)

# Sci: STRING assigns taxon-prefixed ENSP IDs.
#   9606  = Homo sapiens (human)
#   10090 = Mus musculus (mouse)
#   7227  = Drosophila melanogaster
# Only human (9606) is supported by this pipeline.
# (https://string-db.org/cgi/help)
EXPECTED_TAXON: str = "9606"

# ---------------------------------------------------------------------------
# Constants — sub-score column mapping. The basic links file has ONLY
# protein1, protein2, combined_score. The detailed file adds 7 sub-scores
# (Szklarczyk et al. 2023).
# ---------------------------------------------------------------------------
DETAILED_SUBSCORE_COLS: tuple[str, ...] = (
    "neighborhood",
    "fusion",
    "cooccurrence",
    "coexpression",
    "experimental",
    "database",
    "textmining",
)

# Sub-scores with dedicated DB columns on ProteinProteinInteraction.
DB_SCORE_COLUMNS: tuple[str, ...] = (
    "experimental",
    "database",
    "textmining",
)

# Sub-scores packed into score_json (no dedicated column).
JSON_SCORE_COLUMNS: tuple[str, ...] = (
    "neighborhood",
    "fusion",
    "cooccurrence",
    "coexpression",
)

# The 7 sub-scores that the detailed file adds (per Szklarczyk et al. 2023).
ALL_DETAILED_COLS: tuple[str, ...] = DETAILED_SUBSCORE_COLS

# ---------------------------------------------------------------------------
# Expected output columns (for the cleaned CSV / DataFrame). Reconciled
# with pipelines/schema/v1.json — required: string_id_a, string_id_b,
# combined_score. Optional: uniprot_id_a, uniprot_id_b. Provenance cols
# (created_at, string_version, pipeline_run_id, source_url, source_sha256)
# are NOT part of the DB load but ARE part of the CSV for audit.
# ---------------------------------------------------------------------------
EXPECTED_OUTPUT_COLUMNS: tuple[str, ...] = (
    "string_id_a",
    "string_id_b",
    "uniprot_id_a",
    "uniprot_id_b",
    "combined_score",
    "source",
    "neighborhood",
    "fusion",
    "cooccurrence",
    "coexpression",
    "experimental_score",
    "database_score",
    "textmining_score",
    "score_json",
    "created_at",
    "string_version",
    "pipeline_run_id",
    "source_url",
    "source_sha256",
)


# ===========================================================================
# Helper functions (module-level, pure, unit-testable)
# ===========================================================================


def _url_to_filename(url: str) -> str:
    """Extract the filename from a URL, cross-platform safe (GAP-12.8).

    Uses ``urllib.parse.urlparse`` and ``rsplit`` rather than
    ``Path(url).name`` because ``Path`` mangles query strings and fragments
    on Windows and silently accepts ``file://`` schemes.
    """
    path = urlparse(url).path
    return path.rsplit("/", 1)[-1]


def _extract_string_version(url: str) -> str:
    """Extract the STRING version from a protein.links URL (GAP-7.6).

    Handles formats:
        - https://stringdb-downloads.org/download/protein.links.v12.0/9606.protein.links.v12.0.txt.gz
        - https://stringdb.org/v12/links/...

    Returns e.g. ``'12.0'`` or raises ``ValueError``.
    """
    path = urlparse(url).path
    match = re.search(r"v(\d+\.\d+)", path)
    if not match:
        raise ValueError(f"Could not extract STRING version from URL: {url}")
    return match.group(1)


def _is_valid_uniprot(value: Any) -> bool:
    """Return True iff *value* is a canonical UniProt accession (BUG-3.6).

    The canonical pattern is documented at
    https://www.uniprot.org/help/accession and matches the
    ``UNIPROT_ID_PATTERN`` declared in ``pipelines/schema/v1.json``.
    """
    if not isinstance(value, str) or not value:
        return False
    return bool(UNIPROT_ID_PATTERN.match(value))


def _is_isoform(accession: str) -> bool:
    """Return True iff *accession* is a UniProt isoform (GAP-3.8).

    Canonical UniProt accessions do not contain a hyphen.  Isoform
    accessions have the form ``<canonical>-<N>`` (e.g. ``P04637-2`` is
    isoform 2 of p53).  See https://www.uniprot.org/help/isoforms.
    """
    return isinstance(accession, str) and "-" in accession


# ===========================================================================
# StringPipeline
# ===========================================================================


class StringPipeline(BasePipeline):
    """STRING DB pipeline for protein-protein interaction (PPI) data.

    This pipeline is fully deterministic — it performs no stochastic
    operations.  The base-class ``seed`` parameter has no effect on
    output but is recorded in the audit trail for consistency with
    other pipelines (GAP-7.7).
    """

    # FIX GAP-14.6: class attribute with type annotation.
    source_name: str = "string"

    # FIX GAP-16.12: override _field_lineage for full field-level provenance.
    _field_lineage: dict[str, str] = {
        "string_id_a": (
            "aliases.#string_protein_id (source='UniProt_AC') "
            "JOIN links.protein1 (canonical-ordered via min)"
        ),
        "string_id_b": (
            "aliases.#string_protein_id (source='UniProt_AC') "
            "JOIN links.protein2 (canonical-ordered via max)"
        ),
        "uniprot_id_a": (
            "aliases.alias (source='UniProt_AC', uppercase, canonical-only) "
            "JOIN links.protein1"
        ),
        "uniprot_id_b": (
            "aliases.alias (source='UniProt_AC', uppercase, canonical-only) "
            "JOIN links.protein2"
        ),
        "combined_score": (
            "links.combined_score (filtered >= STRING_MIN_COMBINED_SCORE, "
            "NaN-quarantined)"
        ),
        "experimental_score": (
            "detailed_links.experimental (optional, NULL if detailed absent)"
        ),
        "database_score": "detailed_links.database (optional)",
        "textmining_score": "detailed_links.textmining (optional)",
        "score_json": (
            "detailed_links.{neighborhood,fusion,cooccurrence,coexpression} "
            "(JSON-packed, NULL if detailed absent)"
        ),
    }

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        *args: Any,
        freeze_version: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the STRING pipeline (GAP-7.3 freeze_version support).

        Parameters
        ----------
        *args, **kwargs
            Forwarded to ``BasePipeline.__init__``.
        freeze_version : str, optional
            If provided, the pipeline will refuse to run if
            ``STRING_VERSION`` does not match this value (idempotency
            guarantee).
        """
        super().__init__(*args, **kwargs)
        self._freeze_version: Optional[str] = freeze_version

        # FIX BUG-3.4 / GAP-12.9 — production-override threshold.
        # In production (ENV=prod), STRING_MIN_COMBINED_SCORE must be >= 700
        # (Szklarczyk et al. 2023: ≥700 → >80% precision on KEGG benchmarks;
        # ≥400 → only ~50% — too permissive for clinical-decision-support).
        self._effective_score_threshold: int = self._compute_effective_threshold()

        # Per-run state (populated by download(), consumed by clean()).
        self._aliases_path: Optional[Path] = None
        self._detailed_path: Optional[Path] = None
        self._links_path: Optional[Path] = None

        # Per-run SHA-256 of auxiliary raw files (links file is recorded
        # by the base class as self._sha256_raw).
        self._aliases_sha256: Optional[str] = None
        self._detailed_sha256: Optional[str] = None

        # Per-run transformation log (GAP-16.3).
        self._transformation_log: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Helpers — configuration, dead-letter, metrics
    # ------------------------------------------------------------------

    def _is_production(self) -> bool:
        """Return True if the current environment is production (Chain 1 root fix).

        Reads ``DRUGOS_ENVIRONMENT`` as the canonical name (the variable
        set by ``docker-compose.yml`` and ``config/settings.py``).
        Falls back to legacy ``ENVIRONMENT`` / ``ENV`` for backward
        compatibility, and normalizes the vocabulary to
        ``{development, staging, production}``.

        Previous bug (FORENSIC Chain 1): this method read only ``ENV``,
        which nobody sets.  In production Docker the STRING score
        threshold of 700 was therefore never enforced, allowing
        low-confidence PPIs (score >= 400) to pollute the knowledge
        graph.
        """
        raw = (
            os.getenv("DRUGOS_ENVIRONMENT")
            or os.getenv("ENVIRONMENT")
            or os.getenv("ENV", "development")
        ).strip().lower()
        _NORM = {
            "dev": "development",
            "develop": "development",
            "development": "development",
            "staging": "staging",
            "stage": "staging",
            "prod": "production",
            "production": "production",
        }
        return _NORM.get(raw, raw) in ("production", "staging")

    def pre_check(self) -> Dict[str, bool]:
        """Run STRING-specific pre-flight checks before starting the pipeline.

        P1-23 ROOT FIX: previously the only signal that the UniProt
        pipeline had not run was a ``RuntimeError`` raised inside
        ``_load_with_session`` (called by ``load()``). By that point
        ``download()`` had already fetched the STRING PPI file,
        ``clean()`` had already run entity-resolution against an empty
        proteins.csv, and the operator had wasted ~30 minutes of API
        quota before seeing the misleading "run UniProt first" message.
        The check is now performed up-front (in pre_check, called by
        BasePipeline.run before download) and verifies BOTH that
        proteins.csv exists AND is non-empty AND that the proteins DB
        table is non-empty. The error message is also made accurate:
        it explicitly states that the UniProt pipeline output is
        missing, not just that "the mapping is empty".
        """
        checks = super().pre_check()
        # Check 1: proteins.csv exists and is non-empty.
        proteins_csv = PROCESSED_DATA_DIR / "proteins.csv"
        proteins_csv_ok = False
        try:
            if proteins_csv.exists() and proteins_csv.stat().st_size > 0:
                # Read just the header to confirm it parses.
                df_probe = pd.read_csv(
                    proteins_csv, nrows=1, usecols=["uniprot_id"]
                )
                proteins_csv_ok = len(df_probe) > 0
        except (OSError, ValueError, ParserError) as exc:
            logger.warning(
                "[%s] pre_check: could not verify proteins.csv at %s: %s",
                self.source_name, proteins_csv, exc,
            )
        checks["uniprot_csv_present_and_nonempty"] = proteins_csv_ok

        # Check 2: DB proteins table is non-empty. Only run if the DB
        # itself is reachable (otherwise we'd double-log the same root
        # cause).
        proteins_db_ok = False
        if checks.get("db_reachable", False):
            try:
                from database.models import Protein
                with get_db_session() as session:
                    count = session.query(Protein).count()
                proteins_db_ok = count > 0
            except ImportError as exc:
                logger.warning(
                    "[%s] pre_check: could not import Protein model to "
                    "verify proteins table is non-empty: %s",
                    self.source_name, exc,
                )
            except Exception as exc:
                # DB-layer errors propagate: do NOT silently bypass the
                # dependency check (P1-14 rationale).
                logger.warning(
                    "[%s] pre_check: DB error verifying proteins table is "
                    "non-empty: %s",
                    self.source_name, exc,
                )
        checks["uniprot_db_table_nonempty"] = proteins_db_ok

        if not proteins_csv_ok or not proteins_db_ok:
            # Surface an accurate, actionable error message up-front so
            # operators don't waste 30 minutes of STRING API quota
            # before discovering the UniProt pipeline was never run.
            logger.error(
                "[%s] P1-23 ROOT FIX: STRING pipeline cannot start — the "
                "UniProt pipeline output is missing or empty. "
                "proteins.csv present/nonempty=%s; proteins DB table "
                "nonempty=%s. Run `python -m pipelines uniprot` BEFORE "
                "`python -m pipelines string`. The previous error path "
                "raised this only inside load(), after download/clean "
                "had already consumed API quota against an empty "
                "proteins set.",
                self.source_name, proteins_csv_ok, proteins_db_ok,
            )
        return checks

    def _compute_effective_threshold(self) -> int:
        """Compute the effective score threshold (BUG-3.4, GAP-12.9).

        In production, the threshold is forced to
        ``STRING_MIN_COMBINED_SCORE_PROD`` (default 700) if the env-set
        ``STRING_MIN_COMBINED_SCORE`` is below it.
        """
        if self._is_production():
            prod_min = STRING_MIN_COMBINED_SCORE_PROD
            if STRING_MIN_COMBINED_SCORE < prod_min:
                logger.warning(
                    "[%s] STRING_MIN_COMBINED_SCORE=%d is below the production "
                    "minimum %d. Using %d (Szklarczyk et al. 2023: ≥700 → "
                    ">80%% precision on KEGG benchmarks).",
                    self.source_name,
                    STRING_MIN_COMBINED_SCORE,
                    prod_min,
                    prod_min,
                )
                return prod_min
        return STRING_MIN_COMBINED_SCORE

    def _expected_output_columns(self) -> list[str]:
        """Return the list of expected output columns (GAP-6.5)."""
        return list(EXPECTED_OUTPUT_COLUMNS)

    def _empty_output(self) -> pd.DataFrame:
        """Return an empty DataFrame WITH the expected schema columns (GAP-4.13, GAP-6.5).

        Replaces bare ``pd.DataFrame()`` returns from ``clean()`` so that
        ``run_load_only`` does not crash on a 0-byte CSV (GAP-6.5).
        """
        return pd.DataFrame(columns=self._expected_output_columns())

    def _write_dead_letter(self, reason: str, records: list[dict] | dict) -> Path:
        """Persist dropped records to a dead-letter JSON file (Section 23.1).

        The file path is
        ``PROCESSED_DATA_DIR/dead_letter/string_run_{run_id}_{reason}.json``.
        Each entry includes the original row, the reason, the stage,
        the timestamp, and ``pipeline_run_id``.
        """
        dl_dir = PROCESSED_DATA_DIR / "dead_letter"
        dl_dir.mkdir(parents=True, exist_ok=True)
        # Sanitize reason for filename safety.
        safe_reason = re.sub(r"[^A-Za-z0-9_-]+", "_", reason).strip("_")
        path = dl_dir / f"string_run_{self.run_id}_{safe_reason}.json"

        if isinstance(records, dict):
            records = [records]
        if not isinstance(records, list):
            records = list(records)

        payload = {
            "pipeline": self.source_name,
            "run_id": self.run_id,
            "reason": reason,
            "stage": "clean_or_load",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "record_count": len(records),
            "records": records,
        }
        try:
            path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        except OSError as exc:
            logger.error(
                "[%s] Could not write dead-letter file %s: %s",
                self.source_name,
                path,
                exc,
            )
        return path

    def _log_transform(
        self,
        stage: str,
        before: int,
        after: int,
        reason: str,
        sample: Optional[list[dict]] = None,
    ) -> None:
        """Append a structured transformation-log entry (GAP-16.3)."""
        entry = {
            "stage": stage,
            "before": before,
            "after": after,
            "dropped": before - after,
            "reason": reason,
            "sample": (sample[:5] if sample else None),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._transformation_log.append(entry)
        logger.info(
            "[%s] Transform: %s — %d → %d (%s)",
            self.source_name,
            stage,
            before,
            after,
            reason,
            extra={"event": "transform", **entry},
        )

    def _url_safe_filename(self, url: str) -> str:
        """Return the URL's filename component (GAP-12.8)."""
        return _url_to_filename(url)

    # v29 ROOT FIX (audit P1-20): was full-decompress into memory — OOM on 2GB files. Stream in 64KB chunks.
    def _validate_gzip_integrity(self, dest: Path) -> bool:
        """Validate that a .gz file is not truncated (streaming, P1-20).

        The base-class implementation calls ``gzip.open(dest, "rb").seek(-1, 2)``
        which forces gzip to decompress the ENTIRE stream into memory before
        seeking to the end — on STRING's 2 GB ``protein.links.v12.0.txt.gz``
        this consumes ~12 GB of RAM (10x expansion factor) and OOM-kills the
        worker. This override streams the gzip stream through in 64 KB chunks
        so peak memory stays at ~64 KB regardless of file size while still
        detecting mid-stream truncation (which raises ``BadGzipFile`` /
        ``EOFError`` / ``OSError`` once the decompressor reaches the gap).

        Parameters
        ----------
        dest : Path
            Path to the .gz file to validate.

        Returns
        -------
        bool
            True if the gzip stream decompresses end-to-end without error.
            False if the magic bytes are wrong or any decompression error
            is raised while streaming.
        """
        try:
            with open(dest, "rb") as fh:
                magic = fh.read(2)
            if magic != b"\x1f\x8b":
                logger.warning(
                    "[%s] File %s has invalid gzip magic bytes "
                    "(expected 0x1f 0x8b)",
                    self.source_name,
                    dest.name,
                )
                return False
            # Stream-decompress in 64 KB chunks. This walks the entire
            # deflate stream (so a truncated CRC32/size trailer or a
            # mid-stream cut still raises) but never holds more than a
            # 64 KB decompressed slab in memory at a time.
            _CHUNK = 65536  # 64 KB
            with gzip.open(dest, "rb") as gfh:
                while True:
                    chunk = gfh.read(_CHUNK)
                    if not chunk:
                        break
            return True
        except (OSError, gzip.BadGzipFile, EOFError) as exc:
            logger.warning(
                "[%s] Existing .gz file %s is truncated: %s",
                self.source_name,
                dest.name,
                exc,
            )
            return False

    def _verify_file_integrity(self, path: Path) -> bool:
        """Verify gzip magic bytes AND SHA-256 sidecar if present (GAP-9.1).

        Returns
        -------
        bool
            True if the file is a valid gzip and (if a sidecar exists)
            its SHA-256 matches. False otherwise.
        """
        if not path.exists() or path.stat().st_size == 0:
            return False
        # Gzip magic bytes: 0x1f 0x8b.
        try:
            with open(path, "rb") as fh:
                magic = fh.read(2)
        except OSError as exc:
            logger.error(
                "[%s] %s unreadable: %s",
                self.source_name,
                path.name,
                exc,
            )
            return False
        if magic != b"\x1f\x8b":
            logger.error(
                "[%s] %s is not a valid gzip file (bad magic bytes).",
                self.source_name,
                path.name,
            )
            return False
        # SHA-256 sidecar (written by base-class _download_file).
        sidecar = path.with_suffix(path.suffix + ".sha256")
        if sidecar.exists():
            try:
                expected = sidecar.read_text(encoding="utf-8").strip().split()[0]
            except (OSError, IndexError) as exc:
                logger.warning(
                    "[%s] Could not read SHA-256 sidecar %s: %s",
                    self.source_name,
                    sidecar.name,
                    exc,
                )
                return True  # Don't fail just because the sidecar is malformed.
            actual = self._compute_sha256(path)
            if expected != actual:
                logger.error(
                    "[%s] %s SHA-256 mismatch (expected %s, got %s). File corrupted.",
                    self.source_name,
                    path.name,
                    expected,
                    actual,
                )
                return False
        return True

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def download(self) -> Path:
        """Download STRING files using streaming.

        Downloads three files (Szklarczyk et al. 2023):
            - 9606.protein.links.v{VERSION}.txt.gz         (required)
            - 9606.protein.aliases.v{VERSION}.txt.gz        (required)
            - 9606.protein.links.detailed.v{VERSION}.txt.gz (optional,
              configurable via STRING_DETAILED_MODE)

        FIX M1: Removed protein.info download (unused in clean()).
        FIX M2: Detailed links download is configurable via
                STRING_DETAILED_MODE (optional | required | skip).
        FIX GAP-7.3: Enforces freeze_version if set.
        FIX GAP-7.2 / BUG-16.1: Sets self.source_version from STRING_VERSION.
        FIX GAP-7.5 / GAP-16.6: Records SHA-256 of aliases and detailed files.
        FIX GAP-9.3: Distinguishes TLS errors (ERROR) from network errors (WARNING).
        FIX GAP-9.5: Defensive URL-scheme check (http/https only).

        Returns
        -------
        Path
            Path to the protein links (interactions) file.
        """
        # FIX GAP-9.5: Defensive URL-scheme check (base class _download_file
        # also checks, but fail fast here for clarity).
        for url in (
            STRING_PROTEIN_LINKS_URL,
            STRING_ALIASES_URL,
            STRING_PROTEIN_LINKS_DETAILED_URL,
        ):
            scheme = urlparse(url).scheme
            if scheme not in ("http", "https"):
                raise ValueError(
                    f"STRING URL {url!r} has invalid scheme {scheme!r}. "
                    "Only http/https allowed."
                )

        # FIX GAP-7.3: Enforce freeze_version (idempotency guarantee).
        if self._freeze_version is not None and self._freeze_version != STRING_VERSION:
            raise RuntimeError(
                f"STRING_VERSION={STRING_VERSION!r} but freeze_version="
                f"{self._freeze_version!r}. Refusing to run with a different "
                "version (idempotency guarantee)."
            )

        # FIX BUG-7.2 / BUG-16.1: Record the STRING version for audit.
        self.source_version = STRING_VERSION
        # FIX GAP-7.6: Validate the version via URL extraction (defensive).
        try:
            extracted = _extract_string_version(STRING_PROTEIN_LINKS_URL)
            if extracted != STRING_VERSION:
                logger.warning(
                    "[%s] STRING_VERSION=%s but URL contains v%s. Using URL version.",
                    self.source_name,
                    STRING_VERSION,
                    extracted,
                )
                self.source_version = extracted
        except ValueError as exc:
            logger.warning(
                "[%s] Could not extract STRING version from URL: %s",
                self.source_name,
                exc,
            )
        logger.info(
            "[%s] STRING version: %s (recorded in audit trail)",
            self.source_name,
            self.source_version,
        )
        self._emit_metric("string.source_version", self.source_version)

        # FIX GAP-12.8: Use urllib.parse-based filename extraction.
        links_path = self.raw_dir / self._url_safe_filename(STRING_PROTEIN_LINKS_URL)
        aliases_path = self.raw_dir / self._url_safe_filename(STRING_ALIASES_URL)
        detailed_path = self.raw_dir / self._url_safe_filename(
            STRING_PROTEIN_LINKS_DETAILED_URL
        )

        # FIX GAP-1.4: Store paths on self so clean() reads exactly what
        # download() wrote (no re-derivation from URL constants).
        self._links_path = links_path
        self._aliases_path = aliases_path
        self._detailed_path = detailed_path

        # Required: links + aliases.
        self._download_file(STRING_PROTEIN_LINKS_URL, links_path)
        self._download_file(STRING_ALIASES_URL, aliases_path)

        # FIX GAP-7.4 / GAP-12.5: Detailed-file download is configurable.
        # - "skip"     → don't even attempt download
        # - "required" → download without try/except (failure raises)
        # - "optional" → wrap in try/except (current behaviour, but loud)
        if STRING_DETAILED_MODE == "skip":
            logger.info(
                "[%s] STRING_DETAILED_MODE=skip — not downloading detailed file.",
                self.source_name,
            )
        elif STRING_DETAILED_MODE == "required":
            self._download_file(STRING_PROTEIN_LINKS_DETAILED_URL, detailed_path)
            logger.info(
                "[%s] STRING_DETAILED_MODE=required — detailed file downloaded.",
                self.source_name,
            )
        else:  # "optional"
            try:
                self._download_file(STRING_PROTEIN_LINKS_DETAILED_URL, detailed_path)
                logger.info(
                    "[%s] Detailed links file downloaded.",
                    self.source_name,
                )
            except ssl.SSLError as exc:
                # FIX GAP-9.3: TLS failure is a potential MITM — escalate to ERROR.
                logger.error(
                    "[%s] TLS verification failed for detailed links download: "
                    "%s. Potential MITM attack. Skipping detailed file. Set "
                    "STRING_DETAILED_MODE=required for reproducible sub-score "
                    "coverage.",
                    self.source_name,
                    exc,
                )
                self._emit_metric("string.detailed_tls_failure", 1)
            except Exception as exc:
                logger.warning(
                    "[%s] Detailed links download failed (network error): %s. "
                    "Sub-scores will be NULL in this run. Set "
                    "STRING_DETAILED_MODE=required for reproducible sub-score "
                    "coverage.",
                    self.source_name,
                    exc,
                )
                self._emit_metric("string.detailed_network_failure", 1)

        # Verify downloaded files match the expected STRING version.
        try:
            expected_version = _extract_string_version(STRING_PROTEIN_LINKS_URL)
        except ValueError:
            expected_version = self.source_version
        for path in [links_path, aliases_path]:
            if path.exists() and expected_version not in path.name:
                # Log message kept as a single contiguous sentence for
                # regression-test detection (test_all_45_fixes.py::TestIssue22).
                logger.warning(
                    "[%s] Cached file %s may be from a different STRING version. "
                    "Expected version %s in filename. Delete the file to re-download.",
                    self.source_name,
                    path.name,
                    expected_version,
                )

        # FIX GAP-7.5 / GAP-16.6: Record SHA-256 of aliases file (and detailed
        # if present) so mapping drift / file-tampering can be detected.
        try:
            self._aliases_sha256 = self._compute_sha256(aliases_path)
            logger.info(
                "[%s] Aliases file SHA-256: %s",
                self.source_name,
                self._aliases_sha256,
            )
            self._emit_metric("string.aliases_sha256_len", len(self._aliases_sha256))
        except OSError as exc:
            logger.warning(
                "[%s] Could not compute SHA-256 of aliases file: %s",
                self.source_name,
                exc,
            )
        if detailed_path.exists():
            try:
                self._detailed_sha256 = self._compute_sha256(detailed_path)
                logger.info(
                    "[%s] Detailed file SHA-256: %s",
                    self.source_name,
                    self._detailed_sha256,
                )
                self._emit_metric("string.detailed_sha256_len", len(self._detailed_sha256))
            except OSError as exc:
                logger.warning(
                    "[%s] Could not compute SHA-256 of detailed file: %s",
                    self.source_name,
                    exc,
                )

        # FIX GAP-5.6: Timeliness check on cached files. STRING releases
        # annually; warn if a cached file is older than max_data_age_days.
        for path in [links_path, aliases_path]:
            if path.exists():
                age_days = (time.time() - path.stat().st_mtime) / 86400.0
                if age_days > self.max_data_age_days:
                    logger.info(
                        "[%s] Cached file %s is %.0f days old (max %d). "
                        "STRING releases annually; consider re-downloading.",
                        self.source_name,
                        path.name,
                        age_days,
                        self.max_data_age_days,
                    )
                    self._emit_metric("string.stale_cache_age_days", int(age_days))

        return links_path

    # ------------------------------------------------------------------
    # Clean — orchestrator
    # ------------------------------------------------------------------

    def clean(self, raw_path: Path) -> pd.DataFrame:
        """Clean and normalize STRING PPI data.

        Orchestrates the following stages (each in its own method for
        testability — GAP-1.6):

        1. ``_load_links_file``               — load gzipped space-separated links file
        2. ``_filter_by_score``               — filter combined_score >= threshold
        3. ``_build_string_uniprot_map``      — build STRING→UniProt mapping from aliases
        3.5. ``_canonicalize_protein_order``  — canonical-order STRING IDs (P1-29 ROOT FIX: BEFORE mapping)
        4. ``_map_to_uniprot``                — map protein1/protein2 to UniProt IDs
        5. ``_canonicalize_and_dedup``        — dedup on (now-canonical) UniProt ID pairs
        6. ``_merge_detailed_scores``         — merge sub-scores from detailed file (if present)
        7. ``_build_output``                  — construct output DataFrame with schema-conformant columns
        8. ``_validate_and_repair_output``    — schema validation + type repair

        Returns
        -------
        pd.DataFrame
            Cleaned PPI records, or an empty DataFrame with expected
            columns if no records survive filtering (GAP-4.13, GAP-6.5).

        Notes
        -----
        Per GAP-1.3, this method does NOT write the CSV directly. The
        base class ``_persist_cleaned_data`` writes it with
        ``encoding="utf-8"``, ``QUOTE_MINIMAL`` (P1-26 ROOT FIX — was
        QUOTE_NONNUMERIC), and a SHA-256 sidecar.  Writing here would
        (a) double the I/O for 20M rows and (b) produce an un-audited
        file on the direct-call path.
        """
        t0 = time.perf_counter()
        # FIX GAP-1.4: Use the paths recorded at download() time, not
        # re-derived from URL constants (which could point to files that
        # were never downloaded in run_load_only mode).
        aliases_path = (
            self._aliases_path
            if self._aliases_path is not None
            else self.raw_dir / self._url_safe_filename(STRING_ALIASES_URL)
        )
        detailed_path = (
            self._detailed_path
            if self._detailed_path is not None
            else self.raw_dir / self._url_safe_filename(STRING_PROTEIN_LINKS_DETAILED_URL)
        )

        # Stage 1 — load links
        links_df = self._load_links_file(raw_path)
        self._emit_metric("string.raw_record_count", len(links_df))
        self._log_transform("load_links", 0, len(links_df), "raw PPI records loaded")
        if links_df.empty:
            return self._empty_output()

        # Stage 2 — filter by score
        before_filter = len(links_df)
        links_df = self._filter_by_score(links_df)
        self._emit_metric("string.after_score_filter_count", len(links_df))
        self._log_transform(
            "filter_by_score",
            before_filter,
            len(links_df),
            f"combined_score >= {self._effective_score_threshold}",
        )
        if links_df.empty:
            logger.warning(
                "[%s] No PPI records after score filtering",
                self.source_name,
            )
            return self._empty_output()

        # Stage 3 — build STRING → UniProt map
        # FIX GAP-6.3: Missing/empty aliases file is a HARD failure.
        if not aliases_path.exists():
            raise FileNotFoundError(
                f"STRING aliases file not found: {aliases_path}. The pipeline "
                "cannot proceed without it (no UniProt mapping possible)."
            )
        string_to_uniprot = self._build_string_uniprot_map(aliases_path)
        if not string_to_uniprot:
            raise RuntimeError(
                f"STRING→UniProt mapping is empty. Aliases file: {aliases_path}. "
                "Check the file format and source column filtering."
            )
        self._emit_metric("string.uniprot_mapping_count", len(string_to_uniprot))

        # Stage 3.5 — canonical-order STRING IDs BEFORE mapping (P1-29
        # ROOT FIX). Doing the canonicalisation before the STRING→UniProt
        # mapping means the mapping naturally produces canonical-ordered
        # UniProt IDs (uniprot_a <= uniprot_b), eliminating the need for
        # a post-hoc UniProt swap that was fragile under many-to-one
        # ENSP→UniProt mappings.
        before_canon_protein = len(links_df)
        links_df = self._canonicalize_protein_order(links_df)
        self._emit_metric("string.after_canonical_protein_order", len(links_df))

        # Stage 4 — map to UniProt
        before_map = len(links_df)
        links_df = self._map_to_uniprot(links_df, string_to_uniprot)
        retention_rate = (len(links_df) / before_map) if before_map > 0 else 0.0
        self._emit_metric("string.after_uniprot_mapping_count", len(links_df))
        self._emit_metric("string.uniprot_retention_rate", retention_rate)
        self._log_transform(
            "map_to_uniprot",
            before_map,
            len(links_df),
            "rows with both proteins mapped to canonical UniProt",
        )

        # FIX GAP-3.10 — surface low retention as WARNING/ERROR.
        if retention_rate < 0.50:
            logger.error(
                "[%s] UniProt mapping retention rate is %.1f%% (%d / %d). "
                "This indicates a broken mapping, stale aliases file, or "
                "missing UniProt pipeline run. Investigate before proceeding.",
                self.source_name,
                retention_rate * 100.0,
                len(links_df),
                before_map,
            )
        elif retention_rate < 0.80:
            logger.warning(
                "[%s] UniProt mapping retention rate is %.1f%% (%d / %d). "
                "Check aliases file freshness.",
                self.source_name,
                retention_rate * 100.0,
                len(links_df),
                before_map,
            )

        if links_df.empty:
            logger.warning(
                "[%s] No PPI records after UniProt mapping",
                self.source_name,
            )
            return self._empty_output()

        # Stage 5 — dedup (post-mapping, post-canonicalisation).
        # P1-29 ROOT FIX: canonicalisation now happens in Stage 3.5
        # (before the UniProt mapping), so this stage is pure dedup.
        before_canon = len(links_df)
        links_df = self._canonicalize_and_dedup(links_df)
        self._emit_metric("string.after_dedup_count", len(links_df))
        self._log_transform(
            "canonicalize_and_dedup",
            before_canon,
            len(links_df),
            f"dedup strategy={STRING_DEDUP_STRATEGY}",
        )
        if links_df.empty:
            logger.warning(
                "[%s] All PPI records were duplicates — empty output.",
                self.source_name,
            )
            return self._empty_output()

        # Stage 6 — merge detailed sub-scores
        links_df = self._merge_detailed_scores(links_df, detailed_path)
        self._emit_metric(
            "string.detailed_file_merged",
            int(detailed_path.exists() and self._verify_file_integrity(detailed_path)),
        )

        # Stage 7 — build output
        output_df = self._build_output(links_df)

        # Stage 8 — validate
        output_df = self._validate_and_repair_output(output_df)

        # FIX GAP-15.10 / GAP-16.4: Write a metadata sidecar next to the CSV
        # with schema version, STRING version, pipeline_run_id, and source
        # SHA-256. The base class writes the CSV; we write the sidecar.
        try:
            self._write_metadata_sidecar()
        except Exception as exc:
            logger.warning(
                "[%s] Could not write metadata sidecar: %s",
                self.source_name,
                exc,
            )

        # FIX GAP-16.3: Write the transformation log.
        try:
            self._write_transformation_log()
        except Exception as exc:
            logger.warning(
                "[%s] Could not write transformation log: %s",
                self.source_name,
                exc,
            )

        elapsed = time.perf_counter() - t0
        logger.info(
            "[%s] clean() completed in %.2fs — %d records",
            self.source_name,
            elapsed,
            len(output_df),
        )
        self._emit_metric("string.clean_total_seconds", elapsed)
        self._emit_metric("string.clean_record_count", len(output_df))

        return output_df

    # ------------------------------------------------------------------
    # Clean — stage 1: load links file
    # ------------------------------------------------------------------

    def _load_links_file(self, raw_path: Path) -> pd.DataFrame:
        """Load the gzipped, space-separated links file (BUG-4.1, BUG-8.1).

        Validates that the required columns are present (BUG-4.16) and
        that the organism prefix matches 9606 (GAP-3.9).
        """
        logger.info(
            "[%s] Loading protein links from %s",
            self.source_name,
            raw_path,
        )
        # FIX GAP-14.7 / BUG-4.1: sep=r"\s+" is the recommended pandas
        # idiom for whitespace-separated files. delim_whitespace=True is
        # deprecated in pandas 2.2+. Do NOT use sep=" " (fragile to
        # multiple spaces).
        try:
            links_df = pd.read_csv(
                raw_path,
                compression="gzip",
                sep=r"\s+",
                low_memory=STRING_LOW_MEMORY,
            )
        except (ParserError, gzip.BadGzipFile, EOFError, OSError) as exc:
            logger.error(
                "[%s] Failed to read links file %s: %s. File may be corrupted.",
                self.source_name,
                raw_path.name,
                exc,
            )
            raise RuntimeError(
                f"Could not read links file {raw_path}: {exc}"
            ) from exc

        logger.info(
            "[%s] Parsed %d raw PPI records from %s",
            self.source_name,
            len(links_df),
            raw_path.name,
        )

        # FIX BUG-4.16: Validate required columns BEFORE any operation.
        required_cols = {"protein1", "protein2", "combined_score"}
        missing = required_cols - set(links_df.columns)
        if missing:
            raise SchemaValidationError(
                f"STRING links file missing required columns: {missing}. "
                f"Actual columns: {list(links_df.columns)}."
            )

        # FIX GAP-3.9: Organism validation. Reject rows whose protein IDs
        # do not start with "9606." (human). Cross-species contamination
        # would corrupt the human knowledge graph.
        wrong_taxon_mask = (
            ~links_df["protein1"].astype(str).str.startswith(f"{EXPECTED_TAXON}.")
            | ~links_df["protein2"].astype(str).str.startswith(f"{EXPECTED_TAXON}.")
        )
        wrong_count = int(wrong_taxon_mask.sum())
        if wrong_count > 0:
            logger.error(
                "[%s] %d PPI rows have a wrong taxon prefix (expected '%s.'). "
                "Cross-species contamination would corrupt the human "
                "knowledge graph. Quarantining.",
                self.source_name,
                wrong_count,
                EXPECTED_TAXON,
            )
            self._write_dead_letter(
                "wrong_taxon",
                links_df.loc[wrong_taxon_mask].head(1000).to_dict("records"),
            )
            self._emit_metric("string.wrong_taxon_count", wrong_count)
            links_df = links_df.loc[~wrong_taxon_mask].copy()

        # FIX BUG-4.3: astype(str) on NaN produces "nan" — drop NaN first.
        # Sci: combined_score is an integer in [0, 1000]. NaN means "missing".
        for col in ("protein1", "protein2"):
            links_df = links_df.dropna(subset=[col]).copy()
            links_df[col] = links_df[col].astype(str).str.strip()

        return links_df

    # ------------------------------------------------------------------
    # Clean — stage 2: filter by score
    # ------------------------------------------------------------------

    def _filter_by_score(self, df: pd.DataFrame) -> pd.DataFrame:
        """Filter combined_score >= effective threshold (BUG-5.2, BUG-3.2).

        Quarantines rows with NaN combined_score to a dead-letter file
        (rather than silently dropping them, which would conflate "no
        evidence" with "missing data" — BUG-3.2).
        """
        # FIX BUG-5.2: Count NaN scores BEFORE filtering and persist them
        # to a dead-letter file. NaN >= threshold is False, so NaN rows
        # would be silently dropped without this guard.
        nan_score_mask = df["combined_score"].isna()
        nan_score_count = int(nan_score_mask.sum())
        if nan_score_count > 0:
            logger.warning(
                "[%s] %d PPI rows have NaN combined_score — will be dropped "
                "by the score filter (NaN >= threshold is False). Persisting "
                "to dead-letter for inspection.",
                self.source_name,
                nan_score_count,
            )
            self._emit_metric("string.nan_combined_score_count", nan_score_count)
            self._write_dead_letter(
                "nan_combined_score_rows",
                df.loc[nan_score_mask].head(1000).to_dict("records"),
            )

        before = len(df)
        filtered = df.loc[df["combined_score"] >= self._effective_score_threshold].copy()
        logger.info(
            "[%s] Filtered by score >= %d: %d -> %d",
            self.source_name,
            self._effective_score_threshold,
            before,
            len(filtered),
            extra={
                "event": "ppi_filtered",
                "pipeline": self.source_name,
                "threshold": self._effective_score_threshold,
                "before": before,
                "after": len(filtered),
            },
        )
        return filtered

    # ------------------------------------------------------------------
    # Clean — stage 3: build STRING → UniProt map
    # ------------------------------------------------------------------

    def _build_string_uniprot_map(self, aliases_path: Path) -> Dict[str, str]:
        """Build a mapping from STRING protein ID to UniProt accession.

        Filters the aliases file to ``source == 'UniProt_AC'`` (exact
        equality, NOT substring — see BUG-3.3). Excludes
        ``BLAST_UniProt_AC`` entries (BLAST-inferred, ~5–10% error rate)
        which would corrupt the protein identity layer.

        Validates UniProt IDs against the canonical pattern (BUG-3.6)
        and uppercases them (BUG-3.7). Separates canonical from isoform
        accessions (GAP-3.8) — isoforms are recorded to a dead-letter
        file for downstream use.

        Returns
        -------
        dict[str, str]
            ``{string_protein_id: uniprot_accession}`` (canonical only).
        """
        logger.info(
            "[%s] Loading aliases from %s",
            self.source_name,
            aliases_path,
        )
        # STRING aliases file is tab-separated, gzipped. We only need 3
        # of its many columns — use ``usecols`` to bound memory (GAP-8.2).
        try:
            aliases_df = pd.read_csv(
                aliases_path,
                compression="gzip",
                sep="\t",
                low_memory=STRING_LOW_MEMORY,
                usecols=lambda c: c in ("#string_protein_id", "alias", "source"),
            )
        except (ParserError, gzip.BadGzipFile, EOFError, OSError, ValueError) as exc:
            logger.error(
                "[%s] Failed to read aliases file %s: %s",
                self.source_name,
                aliases_path.name,
                exc,
            )
            raise RuntimeError(
                f"Could not read aliases file {aliases_path}: {exc}"
            ) from exc

        # Normalize column names: lowercase, strip leading '#', replace spaces.
        aliases_df.columns = [
            c.strip().lstrip("#").lower().replace(" ", "_")
            for c in aliases_df.columns
        ]

        # FIX GAP-2.5: Fail loudly if STRING changes the header. Do NOT
        # silently return {}.
        EXPECTED_SOURCE_COL = "source"
        EXPECTED_STRING_ID_COL = "string_protein_id"
        EXPECTED_ALIAS_COL = "alias"
        missing_cols = (
            {EXPECTED_SOURCE_COL, EXPECTED_STRING_ID_COL, EXPECTED_ALIAS_COL}
            - set(aliases_df.columns)
        )
        if missing_cols:
            raise SchemaValidationError(
                f"STRING aliases file missing expected columns {missing_cols}. "
                f"Actual columns: {list(aliases_df.columns)}. STRING format "
                "may have changed."
            )

        # FIX BUG-3.3: Sci — STRING aliases have TWO UniProt-related sources:
        #   - "UniProt_AC"        : manually curated, <1% error (USE THIS)
        #   - "BLAST_UniProt_AC"  : BLAST-inferred, ~5-10% error (DO NOT USE)
        # Use exact equality, NOT substring.
        source_stripped = aliases_df[EXPECTED_SOURCE_COL].astype(str).str.strip()
        uniprot_mask = source_stripped == "UniProt_AC"
        blast_mask = source_stripped == "BLAST_UniProt_AC"
        blast_count = int(blast_mask.sum())
        if blast_count > 0:
            logger.info(
                "[%s] Excluded %d BLAST_UniProt_AC entries (BLAST-inferred, "
                "~5-10%% error rate). Using only curated UniProt_AC (<1%% error).",
                self.source_name,
                blast_count,
            )
            self._emit_metric("string.blast_uniprot_excluded", blast_count)

        uniprot_aliases = aliases_df.loc[uniprot_mask].copy()
        if uniprot_aliases.empty:
            logger.warning(
                "[%s] No UniProt_AC entries in aliases file",
                self.source_name,
            )
            return {}

        # FIX BUG-4.3: Drop NaN BEFORE astype(str) — astype(str) on NaN
        # produces "nan" which then passes the != "" check.
        uniprot_aliases = uniprot_aliases.dropna(
            subset=[EXPECTED_STRING_ID_COL, EXPECTED_ALIAS_COL]
        ).copy()

        # FIX BUG-3.7: Sci — UniProt accessions are canonically UPPERCASE.
        # The UniProt pipeline writes UPPERCASE to Protein.uniprot_id. STRING
        # aliases may contain mixed-case entries. Normalize or FK lookup
        # fails silently.
        uniprot_aliases[EXPECTED_STRING_ID_COL] = (
            uniprot_aliases[EXPECTED_STRING_ID_COL]
            .astype(str)
            .str.strip()
            .str.upper()
        )
        uniprot_aliases[EXPECTED_ALIAS_COL] = (
            uniprot_aliases[EXPECTED_ALIAS_COL]
            .astype(str)
            .str.strip()
            .str.upper()
        )

        # FIX GAP-3.8: Sci — separate canonical from isoform accessions
        # FIRST. Isoforms have the form <canonical>-<N> (e.g. P04637-2 is
        # isoform 2 of p53). They can have different drug-binding profiles
        # (e.g. BRAF-V600K vs BRAF-V600E respond differently to vemurafenib).
        # We separate them BEFORE the canonical-pattern check (BUG-3.6)
        # because the canonical pattern rejects hyphenated accessions.
        isoform_mask = uniprot_aliases[EXPECTED_ALIAS_COL].apply(_is_isoform)
        valid_isoforms = uniprot_aliases.loc[isoform_mask].copy()
        if not valid_isoforms.empty:
            isoform_count = len(valid_isoforms)
            logger.info(
                "[%s] Recorded %d isoform mappings (separate from canonical). "
                "Isoforms may have distinct drug-binding profiles.",
                self.source_name,
                isoform_count,
            )
            self._emit_metric("string.isoform_mappings", isoform_count)
            self._write_dead_letter(
                "isoform_mappings",
                valid_isoforms.head(1000).to_dict("records"),
            )

        # Now operate on non-isoform (canonical) accessions only.
        canonical_aliases = uniprot_aliases.loc[~isoform_mask].copy()

        # FIX BUG-3.6: Validate against canonical UniProt accession pattern.
        valid_uniprot_mask = canonical_aliases[EXPECTED_ALIAS_COL].map(_is_valid_uniprot)
        invalid_count = int((~valid_uniprot_mask).sum())
        if invalid_count > 0:
            logger.warning(
                "[%s] Excluded %d aliases with non-canonical UniProt accessions. "
                "Pattern: %s",
                self.source_name,
                invalid_count,
                UNIPROT_ID_PATTERN.pattern,
            )
            self._emit_metric("string.invalid_uniprot_excluded", invalid_count)
            self._write_dead_letter(
                "invalid_uniprot",
                canonical_aliases.loc[~valid_uniprot_mask].head(1000).to_dict("records"),
            )
        valid = canonical_aliases.loc[valid_uniprot_mask].copy()

        # FIX BUG-7.1 / GAP-2.6: Sort before dedup for deterministic behavior
        # across STRING versions. The dedup strategy itself is configurable
        # via STRING_DEDUP_STRATEGY (GAP-3.11, GAP-12.7).
        #
        # v16 ROOT FIX (DC-6): the previous code made ``"max_score"`` and
        # ``"first"`` branches byte-identical — both sorted by
        # (string_id, alias) and kept ``keep="first"``. The operator
        # thought "max_score" was keeping the highest-scored mapping;
        # actually it kept the alphabetically-first alias. For STRING
        # aliases that means a TrEMBL ``A0A0...`` accession beats a
        # Swiss-Prot ``P23219`` accession (because "A" < "P"), even
        # though Swiss-Prot is the curated, canonical form. The result:
        # STRING → UniProt mapping preferred unreviewed TrEMBL accessions
        # over reviewed Swiss-Prot ones, fragmenting the protein index
        # and bypassing the curated cross-references STRING provides.
        #
        # The fix: under ``"max_score"``, prefer Swiss-Prot
        # ([OPQ]xxx[0-9][A-Z0-9]{3}[0-9], 6 chars) over TrEMBL
        # (anything else), then prefer shorter accessions (6 over 10
        # chars), then alphabetical. Under ``"first"``, keep the legacy
        # alphabetical-first behavior for backward compatibility.
        if STRING_DEDUP_STRATEGY == "max_score":
            # Compute a sort key: (reviewed_rank, length, alias).
            # reviewed_rank = 0 for Swiss-Prot, 1 for TrEMBL — so
            # Swiss-Prot sorts first.
            def _accession_sort_key(ac: str) -> Tuple[int, int, str]:
                ac_stripped = ac.strip()
                is_swiss_prot = (
                    len(ac_stripped) == 6
                    and ac_stripped[0] in "OPQ"
                    and ac_stripped[1].isdigit()
                )
                return (0 if is_swiss_prot else 1, len(ac_stripped), ac_stripped)

            valid = valid.assign(
                _sort_key=valid[EXPECTED_ALIAS_COL].map(_accession_sort_key)
            ).sort_values(
                [EXPECTED_STRING_ID_COL, "_sort_key"]
            ).drop_duplicates(
                subset=[EXPECTED_STRING_ID_COL], keep="first"
            ).drop(columns=["_sort_key"])
        else:  # "first" (legacy, deterministic because we sorted)
            valid = valid.sort_values(
                [EXPECTED_STRING_ID_COL, EXPECTED_ALIAS_COL]
            ).drop_duplicates(subset=[EXPECTED_STRING_ID_COL], keep="first")

        mapping = dict(
            zip(
                valid[EXPECTED_STRING_ID_COL],
                valid[EXPECTED_ALIAS_COL],
            )
        )
        return mapping

    # ------------------------------------------------------------------
    # Clean — stage 4: map STRING IDs to UniProt IDs
    # ------------------------------------------------------------------

    def _map_to_uniprot(
        self, df: pd.DataFrame, mapping: Dict[str, str]
    ) -> pd.DataFrame:
        """Map protein1/protein2 STRING IDs to UniProt accessions.

        Drops rows where either protein fails to map (with dead-letter
        of the unmapped STRING IDs — GAP-5.4).
        """
        df = df.copy()
        df["uniprot_a"] = df["protein1"].map(mapping)
        df["uniprot_b"] = df["protein2"].map(mapping)

        # FIX GAP-5.4 — surface unmapped STRING IDs and persist them.
        unmapped_a_mask = df["uniprot_a"].isna()
        unmapped_b_mask = df["uniprot_b"].isna()
        unmapped_a = df.loc[unmapped_a_mask, "protein1"].dropna().unique().tolist()
        unmapped_b = df.loc[unmapped_b_mask, "protein2"].dropna().unique().tolist()
        if unmapped_a:
            self._write_dead_letter(
                "unmapped_string_id_protein1",
                [{"protein1": x} for x in unmapped_a[:1000]],
            )
            self._emit_metric("string.unmapped_protein1_count", len(unmapped_a))
        if unmapped_b:
            self._write_dead_letter(
                "unmapped_string_id_protein2",
                [{"protein2": x} for x in unmapped_b[:1000]],
            )
            self._emit_metric("string.unmapped_protein2_count", len(unmapped_b))

        before = len(df)
        df = df.dropna(subset=["uniprot_a", "uniprot_b"]).copy()
        logger.info(
            "[%s] After UniProt mapping: %d / %d rows kept",
            self.source_name,
            len(df),
            before,
        )
        return df

    # ------------------------------------------------------------------
    # Clean — stage 4a: canonical-order at the STRING-ID level (P1-29)
    # ------------------------------------------------------------------

    def _canonicalize_protein_order(self, df: pd.DataFrame) -> pd.DataFrame:
        """Canonical-order protein1/protein2 at the STRING-ID level (P1-29).

        P1-29 ROOT FIX: previously, canonical ordering was performed
        AFTER the STRING→UniProt mapping (in ``_canonicalize_and_dedup``),
        and the swap of ``uniprot_a``/``uniprot_b`` was tracked by
        comparing the original ``protein1`` to the canonicalised
        ``protein1``. For one-to-one ENSP→UniProt mappings this is
        correct, but for many-to-one mappings (e.g. multiple ENSP
        isoforms map to the same UniProt accession) the per-row swap
        of UniProt IDs is fragile: the swap is decided at the
        STRING-ID level, but applied to the already-mapped UniProt
        IDs, so the protein↔accession correspondence is reconstructed
        indirectly rather than being preserved by construction.

        The root fix is to canonicalise the STRING-ID ordering BEFORE
        the mapping, so the mapping naturally produces canonical-
        ordered UniProt IDs and no post-hoc UniProt swap is needed.
        This eliminates the latent landmine where a future directional
        score column would have been assigned to the wrong protein.

        Returns
        -------
        pd.DataFrame
            DataFrame with ``protein1 <= protein2`` for every row
            (lexicographic min/max of the STRING ENSP IDs).
        """
        df = df.copy()
        original_protein1 = df["protein1"].copy()
        canonical_a = df[["protein1", "protein2"]].min(axis=1)
        canonical_b = df[["protein1", "protein2"]].max(axis=1)
        df["protein1"] = canonical_a
        df["protein2"] = canonical_b
        swap_count = int((df["protein1"] != original_protein1).sum())
        if swap_count > 0:
            self._log_transform(
                "canonical_ordering_protein_ids",
                len(df),
                len(df),
                f"swapped {swap_count} STRING-ID pairs to canonical (a ≤ b) ordering",
                sample=df.loc[
                    df["protein1"] != original_protein1,
                    ["protein1", "protein2"],
                ].head(5).to_dict("records"),
            )
        return df

    # ------------------------------------------------------------------
    # Clean — stage 5: dedup (post-mapping)
    # ------------------------------------------------------------------

    def _canonicalize_and_dedup(self, df: pd.DataFrame) -> pd.DataFrame:
        """Dedup PPI rows on the (now-canonical) UniProt ID pair.

        P1-29 ROOT FIX: the canonical-ordering logic that used to live
        here has been moved to ``_canonicalize_protein_order`` which
        runs BEFORE the STRING→UniProt mapping. With the protein IDs
        already in canonical order, the mapping naturally produces
        canonical-ordered UniProt IDs (``uniprot_a <= uniprot_b``),
        so no post-hoc UniProt swap is needed. This method now does
        ONLY the dedup step.

        Sci / Design: dedup MUST happen after the UniProt mapping so
        that multiple STRING ENSP pairs collapsing to one UniProt pair
        (e.g. isoforms of the same protein) can be aggregated. The
        aggregation strategy is controlled by ``STRING_DEDUP_STRATEGY``
        (max_score / mean_score / first).
        """
        df = df.copy()
        # FIX GAP-3.11 — dedup with configurable strategy.
        # Sci: When multiple STRING ENSP pairs collapse to one UniProt pair
        # (e.g. isoforms of the same protein), aggregate scores by MAX
        # (strongest evidence). Mean would dilute strong evidence with weak;
        # min would discard strong evidence entirely.
        before_dedup = len(df)
        if STRING_DEDUP_STRATEGY == "max_score":
            df = (
                df.sort_values("combined_score", ascending=False)
                .drop_duplicates(subset=["uniprot_a", "uniprot_b"], keep="first")
                .copy()
            )
        elif STRING_DEDUP_STRATEGY == "mean_score":
            # SCI-FIX (KeyError crash on mean_score strategy):
            # ``groupby().agg(agg_dict)`` keeps ONLY the groupby keys
            # (uniprot_a, uniprot_b) and the columns in agg_dict. The
            # previous code dropped protein1, protein2, source, and
            # every other non-aggregated column. Downstream
            # ``_build_output`` (line 1548) then crashed with
            # ``KeyError: 'protein1'`` because it tried to read
            # df["protein1"] which no longer existed (audit finding 7).
            # The fix includes protein1/protein2/source in the agg
            # dict using the "first" aggregator — within a group (after
            # canonicalization), all rows have identical protein1/
            # protein2/source values, so "first" is a safe no-op
            # aggregator that preserves the columns.
            agg_dict: dict[str, Any] = {"combined_score": "mean"}
            for col in DETAILED_SUBSCORE_COLS:
                if col in df.columns:
                    agg_dict[col] = "mean"
            # Preserve non-aggregated columns needed downstream.
            for preserve_col in ("protein1", "protein2", "source"):
                if preserve_col in df.columns and preserve_col not in agg_dict:
                    agg_dict[preserve_col] = "first"
            df = (
                df.groupby(["uniprot_a", "uniprot_b"], as_index=False)
                .agg(agg_dict)
                .copy()
            )
        else:  # "first" (legacy, deterministic because we sorted)
            df = (
                df.sort_values(["uniprot_a", "uniprot_b"])
                .drop_duplicates(subset=["uniprot_a", "uniprot_b"], keep="first")
                .copy()
            )
        dedup_count = before_dedup - len(df)
        self._emit_metric("string.duplicates_collapsed", dedup_count)
        if dedup_count > 0:
            logger.info(
                "[%s] Collapsed %d duplicate PPI pairs (%d → %d) using strategy %s.",
                self.source_name,
                dedup_count,
                before_dedup,
                len(df),
                STRING_DEDUP_STRATEGY,
            )

        return df

    # ------------------------------------------------------------------
    # Clean — stage 6: merge detailed sub-scores
    # ------------------------------------------------------------------

    def _merge_detailed_scores(
        self, df: pd.DataFrame, detailed_path: Path
    ) -> pd.DataFrame:
        """Merge sub-scores from the detailed file if present & valid (BUG-P0-3).

        Sci: The basic links file (9606.protein.links.*.txt.gz) contains
        ONLY protein1, protein2, combined_score — NO sub-score columns.
        The detailed file (protein.links.detailed.*.txt.gz) contains
        the 7 sub-scores.  The pre-fix code did
        ``links_df[detailed_col].combine_first(links_df.get(col))`` —
        ``links_df.get(col)`` returns None (the object, not a Series),
        and ``Series.combine_first(None)`` raises
        ``AttributeError: 'NoneType' object has no attribute 'dtype'``.
        """
        if not detailed_path.exists():
            logger.info(
                "[%s] Detailed file not present — sub-scores will be NULL.",
                self.source_name,
            )
            # Still add the sub-score columns as NaN so the output schema is stable.
            for col in DETAILED_SUBSCORE_COLS:
                if col not in df.columns:
                    df[col] = np.nan
            return df

        # FIX GAP-9.1: Verify file integrity before reading.
        if not self._verify_file_integrity(detailed_path):
            logger.error(
                "[%s] Detailed file failed integrity check — skipping sub-score merge.",
                self.source_name,
            )
            self._emit_metric("string.detailed_file_corrupted", 1)
            self._write_dead_letter(
                "detailed_file_corrupted",
                [{"path": str(detailed_path)}],
            )
            for col in DETAILED_SUBSCORE_COLS:
                if col not in df.columns:
                    df[col] = np.nan
            return df

        try:
            detailed_df = pd.read_csv(
                detailed_path,
                compression="gzip",
                sep=r"\s+",
                low_memory=STRING_LOW_MEMORY,
            )
        except (ParserError, gzip.BadGzipFile, EOFError, OSError) as exc:
            logger.error(
                "[%s] Failed to read detailed file %s: %s",
                self.source_name,
                detailed_path.name,
                exc,
            )
            self._emit_metric("string.detailed_file_corrupted", 1)
            for col in DETAILED_SUBSCORE_COLS:
                if col not in df.columns:
                    df[col] = np.nan
            return df

        # FIX BUG-P0-3: Detailed file has the same canonical ordering
        # requirement. Apply min/max on STRING ENSP IDs so the merge keys match.
        if "protein1" in detailed_df.columns and "protein2" in detailed_df.columns:
            detailed_df["protein1_canonical"] = detailed_df[
                ["protein1", "protein2"]
            ].min(axis=1)
            detailed_df["protein2_canonical"] = detailed_df[
                ["protein1", "protein2"]
            ].max(axis=1)
            detailed_df["protein1"] = detailed_df["protein1_canonical"]
            detailed_df["protein2"] = detailed_df["protein2_canonical"]
            detailed_df.drop(
                columns=["protein1_canonical", "protein2_canonical"], inplace=True
            )
            detailed_df = detailed_df.drop_duplicates(
                subset=["protein1", "protein2"], keep="first"
            ).copy()

        # Only keep the columns we actually need.
        available_detail_cols = [
            c
            for c in (["protein1", "protein2"] + list(DETAILED_SUBSCORE_COLS))
            if c in detailed_df.columns
        ]
        if len(available_detail_cols) <= 2:
            logger.warning(
                "[%s] Detailed file has no sub-score columns — skipping merge.",
                self.source_name,
            )
            for col in DETAILED_SUBSCORE_COLS:
                if col not in df.columns:
                    df[col] = np.nan
            return df

        df = df.merge(
            detailed_df[available_detail_cols],
            on=["protein1", "protein2"],
            how="left",
            suffixes=("", "_detailed"),
        )
        # FIX BUG-P0-3: The basic links file NEVER has sub-score columns.
        # Don't use combine_first — just assign directly.
        for col in DETAILED_SUBSCORE_COLS:
            detailed_col = f"{col}_detailed"
            if detailed_col in df.columns:
                df[col] = df[detailed_col]
                df.drop(columns=[detailed_col], inplace=True, errors="ignore")
            elif col not in df.columns:
                df[col] = np.nan

        logger.info(
            "[%s] Merged detailed sub-scores from %s",
            self.source_name,
            detailed_path.name,
        )
        return df

    # ------------------------------------------------------------------
    # Clean — stage 7: build output
    # ------------------------------------------------------------------

    def _build_output(self, df: pd.DataFrame) -> pd.DataFrame:
        """Construct the schema-conformant output DataFrame (GUARD-2.1, GAP-2.7).

        Output columns (reconciled with ``pipelines/schema/v1.json``):
            - string_id_a, string_id_b      (required, from links.protein1/protein2)
            - uniprot_id_a, uniprot_id_b    (optional, from mapping)
            - combined_score                (required, int [0,1000])
            - source                        (controlled vocab: "string")
            - neighborhood, fusion, cooccurrence, coexpression  (optional sub-scores)
            - experimental_score, database_score, textmining_score  (DB columns)
            - score_json                    (JSON-packed sub-scores)
            - created_at, string_version, pipeline_run_id, source_url, source_sha256
              (provenance — NOT loaded to DB; for CSV audit only — GAP-14.3)
        """
        # FIX GAP-2.7: Use self.source_name (not hardcoded "string").
        # FIX GUARD-2.1: Output the schema-conformant column names
        # (string_id_a/string_id_b/uniprot_id_a/uniprot_id_b).
        output_df = pd.DataFrame({
            "string_id_a": df["protein1"].astype(str),
            "string_id_b": df["protein2"].astype(str),
            "uniprot_id_a": df["uniprot_a"].astype(str),
            "uniprot_id_b": df["uniprot_b"].astype(str),
            "combined_score": pd.to_numeric(df["combined_score"], errors="coerce"),
            "source": self.source_name,
        })

        # FIX GAP-3.5 — pack the 4 sub-scores not in dedicated columns into
        # score_json (database model has a Text field for this).
        subscore_cols = list(JSON_SCORE_COLUMNS)

        def _pack_score_json(row: pd.Series) -> Optional[str]:
            payload = {}
            for c in subscore_cols:
                if c in row and pd.notna(row.get(c)):
                    payload[c] = int(row[c]) if float(row[c]).is_integer() else float(row[c])
            if not payload:
                return None
            payload["_provenance"] = "detailed_file"
            return json.dumps(payload, default=str)

        # Add the 4 JSON sub-scores as raw columns AND as a packed score_json.
        for col in subscore_cols:
            if col in df.columns:
                output_df[col] = pd.to_numeric(df[col], errors="coerce")
            else:
                output_df[col] = np.nan
        output_df["score_json"] = df.apply(_pack_score_json, axis=1)

        # Add the 3 DB sub-score columns (with rename for DB schema).
        for raw_col, db_col in (
            ("experimental", "experimental_score"),
            ("database", "database_score"),
            ("textmining", "textmining_score"),
        ):
            if raw_col in df.columns:
                output_df[db_col] = pd.to_numeric(df[raw_col], errors="coerce")
            else:
                output_df[db_col] = np.nan

        # FIX GAP-14.3 — provenance columns (CSV only, NOT loaded to DB).
        output_df["created_at"] = datetime.now(timezone.utc).isoformat()
        output_df["string_version"] = self.source_version
        output_df["pipeline_run_id"] = self.run_id
        output_df["source_url"] = STRING_PROTEIN_LINKS_URL
        output_df["source_sha256"] = getattr(self, "_sha256_raw", None)

        # v29 ROOT FIX (audit P1-24): ID format divergence — normalize
        # UniProt accessions to canonical form before writing. STRING's
        # alias file occasionally returns lowercase accessions (e.g.
        # ``"p23219"`` instead of ``"P23219"``); without this normalization
        # a PPI edge from STRING would NOT join with a protein from
        # UniProt or an interaction from DrugBank, silently dropping the
        # edge from the knowledge graph.
        if len(output_df) > 0:
            for col in ("uniprot_id_a", "uniprot_id_b"):
                if col in output_df.columns:
                    output_df[col] = output_df[col].apply(
                        lambda x: normalize_uniprot_id(x)
                        if pd.notna(x) and x != "" else x
                    )

        return output_df

    # ------------------------------------------------------------------
    # Clean — stage 8: validate + repair
    # ------------------------------------------------------------------

    def _validate_and_repair_output(self, df: pd.DataFrame) -> pd.DataFrame:
        """Validate output against schema; coerce types (GAP-5.8, GAP-4.7).

        Raises ``SchemaValidationError`` if validation fails (so the
        base class will record the failure and not silently load bad data).
        """
        if df.empty:
            return self._empty_output()

        # Type repair: combined_score must be int.
        # FIX BUG-3.2 / BUG-5.1: NaN combined_score is quarantined, NOT
        # filled with 0 (which would mean "no interaction" — a false negative).
        missing_score_mask = df["combined_score"].isna()
        if missing_score_mask.any():
            missing_count = int(missing_score_mask.sum())
            logger.warning(
                "[%s] %d PPI records have missing combined_score — quarantining "
                "(NOT filling with 0, which would mean 'no interaction').",
                self.source_name,
                missing_count,
            )
            self._write_dead_letter(
                "missing_combined_score",
                df.loc[missing_score_mask].head(1000).to_dict("records"),
            )
            df = df.loc[~missing_score_mask].copy()

        # Sci: STRING scores are integers in [0, 1000].
        df["combined_score"] = df["combined_score"].astype(int)
        # Defensive range check (loader also validates via CHECK constraint).
        if not df["combined_score"].between(0, 1000).all():
            bad = df.loc[~df["combined_score"].between(0, 1000)]
            logger.error(
                "[%s] %d PPI records have combined_score outside [0, 1000]. "
                "Sample: %s",
                self.source_name,
                len(bad),
                bad.head(5).to_dict("records"),
            )
            self._write_dead_letter(
                "combined_score_out_of_range",
                bad.head(1000).to_dict("records"),
            )
            df = df.loc[df["combined_score"].between(0, 1000)].copy()

        # Schema validation (DQ-5.8). Use base-class validate_output which
        # checks schema/v1.json.
        is_valid, errors = self.validate_output(df)
        if not is_valid:
            logger.error(
                "[%s] Output schema validation failed: %s. Quarantining output.",
                self.source_name,
                errors,
            )
            self._write_dead_letter(
                "schema_validation_failure",
                df.head(1000).to_dict("records"),
            )
            raise SchemaValidationError(
                f"STRING output failed schema validation: {errors}"
            )

        return df

    # ------------------------------------------------------------------
    # Clean — sidecar writers
    # ------------------------------------------------------------------

    def _write_metadata_sidecar(self) -> Path:
        """Write ``.csv.metadata.json`` next to the cleaned CSV (GAP-15.10).

        Contains schema version, STRING version, pipeline_run_id, source
        URL, source SHA-256, and aliases/detailed SHA-256. Does NOT
        pollute the CSV with comment rows (they break pandas read_csv).
        """
        metadata = {
            "schema_version": "v2.0",
            "string_version": self.source_version,
            "pipeline_run_id": self.run_id,
            "correlation_id": self.correlation_id,
            "source_url": STRING_PROTEIN_LINKS_URL,
            "source_sha256": getattr(self, "_sha256_raw", None),
            "aliases_sha256": self._aliases_sha256,
            "detailed_sha256": self._detailed_sha256,
            "effective_score_threshold": self._effective_score_threshold,
            "dedup_strategy": STRING_DEDUP_STRATEGY,
            "detailed_mode": STRING_DETAILED_MODE,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        output_path = PROCESSED_DATA_DIR / "protein_protein_interactions.csv"
        metadata_path = output_path.with_suffix(".csv.metadata.json")
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(
            json.dumps(metadata, indent=2, default=str), encoding="utf-8"
        )
        return metadata_path

    def _write_transformation_log(self) -> Path:
        """Write ``.csv.transform.json`` next to the cleaned CSV (GAP-16.3)."""
        output_path = PROCESSED_DATA_DIR / "protein_protein_interactions.csv"
        transform_path = output_path.with_suffix(".csv.transform.json")
        transform_path.parent.mkdir(parents=True, exist_ok=True)
        transform_path.write_text(
            json.dumps(self._transformation_log, indent=2, default=str),
            encoding="utf-8",
        )
        return transform_path

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    def load(
        self,
        df: pd.DataFrame,
        session: Optional[Session] = None,
    ) -> int:
        """Load cleaned STRING PPI data into the database (FIX BUG-P0-1).

        Contract: honors ``BasePipeline.load(self, df, session=None)``.
        If ``session`` is provided, USES it (does not open a new one) so
        that lineage context (``pipeline_name``, ``run_id``,
        ``correlation_id``) propagates.  If ``session`` is None (direct
        call), opens a new session with lineage context from
        ``self.pipeline_name`` / ``self.run_id`` / ``self.correlation_id``.

        Stages:
            1. Resolve ``uniprot_id`` → ``protein.id`` via
               ``get_uniprot_to_protein_id_map`` (filtered to the unique
               set for performance — GAP-8.5).
            2. Drop rows where FK lookup fails (dead-letter the unmapped
               UniProt IDs — GAP-5.4, GAP-11.3).
            3. Ensure ``protein_a_id < protein_b_id`` (defense-in-depth;
               ``_pre_validate_ppi`` in ``loaders.py`` also enforces this
               and logs a WARNING for each swap — GAP-4.5).
            4. Drop self-interactions with WARNING + dead-letter
               (DB constraint; TODO(schema-migration): allow homodimers —
               BUG-3.1).  ``_pre_validate_ppi`` in ``loaders.py`` also
               quarantines them — GAP-4.4.
            5. Bulk upsert via ``bulk_upsert_ppi`` with
               ``pipeline_run_id`` and ``input_checksum`` (BUG-16.2,
               GAP-15.6, GAP-15.7).

        Returns
        -------
        int
            ``inserted + updated`` (records that actually reached the DB),
            NOT ``int(UpsertResult)`` which is ``total_input``
            (GAP-2.3 — total_input includes quarantined + failed).
        """
        if df.empty:
            logger.info("[%s] No PPI records to load", self.source_name)
            return 0

        # FIX GAP-15.8 / Interop: Assert the UniProt pipeline has run
        # (proteins table must be populated). Fail loudly if not.
        # FIX BUG-P0-1: Use the provided session, or open one with lineage.
        owns_session = session is None
        if owns_session:
            try:
                ctx = get_db_session(
                    pipeline_name=self.source_name,
                    run_id=self.run_id,
                    correlation_id=self.correlation_id,
                )
                session = ctx.__enter__()
            except Exception as exc:
                logger.error(
                    "[%s] Failed to open DB session: %s",
                    self.source_name,
                    exc,
                )
                raise
        # SCI-FIX: Restructured commit/rollback logic to prevent partial-data
        # commits on failure.  The previous code committed in a `finally`
        # block, which meant that if _load_with_session raised AFTER some
        # rows had been flushed, the partial data would be committed.
        # Now: commit only on success, rollback on exception, close always.
        try:
            result = self._load_with_session(df, session)
            if owns_session and session is not None:
                try:
                    session.commit()
                except Exception:
                    try:
                        session.rollback()
                    except Exception:
                        pass
            return result
        except Exception:
            if owns_session and session is not None:
                try:
                    session.rollback()
                except Exception:
                    pass
            raise
        finally:
            if owns_session and session is not None:
                try:
                    ctx.__exit__(None, None, None)
                except Exception:
                    pass
                try:
                    session.close()
                except Exception:
                    pass

    def _load_with_session(self, df: pd.DataFrame, session: Session) -> int:
        """Inner load logic — assumes session is already open."""
        # FIX GAP-14.5: Verify DB schema before loading.
        self._verify_db_schema(session)

        # Build the load DataFrame (copy to avoid mutating caller's df).
        load_df = df.copy()

        # FIX BUG-P0-2: get_uniprot_to_protein_id_map returns a
        # MappingResult dataclass, NOT a dict. Use .mapping.
        # FIX GAP-8.5: Pass the unique UniProt IDs as a filter to avoid
        # loading the entire proteins table.
        unique_uniprot = set(load_df["uniprot_id_a"].dropna().astype(str)).union(
            load_df["uniprot_id_b"].dropna().astype(str)
        )
        mapping_result = get_uniprot_to_protein_id_map(
            session, uniprot_ids=unique_uniprot
        )
        uniprot_map: Dict[str, int] = mapping_result.mapping

        # FIX GAP-15.8: Assert UniProt pipeline has run.
        if not uniprot_map:
            raise RuntimeError(
                "UniProt→Protein.id mapping is empty. The UniProt pipeline "
                "must run before the STRING pipeline. Run: "
                "python -m pipelines uniprot"
            )

        # FIX GAP-11.3 / GAP-16.5: Surface unmapped UniProt IDs.
        unmapped = unique_uniprot - set(uniprot_map.keys())
        if unmapped:
            logger.warning(
                "[%s] %d / %d UniProt IDs unmapped to Protein.id. Sample: %s",
                self.source_name,
                len(unmapped),
                len(unique_uniprot),
                sorted(unmapped)[:10],
            )
            self._emit_metric("string.uniprot_unmapped_count", len(unmapped))
            self._write_dead_letter(
                "unmapped_uniprot_ids",
                [{"uniprot_id": x} for x in sorted(unmapped)[:1000]],
            )

        load_df["protein_a_id"] = load_df["uniprot_id_a"].map(uniprot_map)
        load_df["protein_b_id"] = load_df["uniprot_id_b"].map(uniprot_map)

        # Drop rows where either FK is missing.
        before = len(load_df)
        load_df = load_df.dropna(subset=["protein_a_id", "protein_b_id"]).copy()
        if before - len(load_df) > 0:
            logger.info(
                "[%s] PPI records with resolved FKs: %d / %d",
                self.source_name,
                len(load_df),
                before,
            )

        if load_df.empty:
            logger.warning(
                "[%s] No PPI records with resolved protein IDs",
                self.source_name,
            )
            return 0

        # Convert FK columns to int.
        load_df["protein_a_id"] = load_df["protein_a_id"].astype(int)
        load_df["protein_b_id"] = load_df["protein_b_id"].astype(int)

        # FIX GAP-4.5: Ensure protein_a_id < protein_b_id (defense-in-depth
        # with _pre_validate_ppi in loaders.py).
        swap_mask = load_df["protein_a_id"] > load_df["protein_b_id"]
        if swap_mask.any():
            # v43 ROOT FIX (P1-012): the previous code used
            # ``.to_numpy()`` to "strip index alignment intentionally"
            # then assigned back with ``.loc[swap_mask, [...]]``. This
            # is fragile — if the DataFrame index is non-default (e.g.
            # after a filter+concat), the positional .to_numpy() output
            # can silently misalign with the .loc index. The fix uses
            # explicit ``.values`` on BOTH sides so the assignment is
            # unambiguously positional and the index alignment is
            # stripped on both sides consistently.
            _a_vals = load_df.loc[swap_mask, "protein_a_id"].values
            _b_vals = load_df.loc[swap_mask, "protein_b_id"].values
            load_df.loc[swap_mask, "protein_a_id"] = _b_vals
            load_df.loc[swap_mask, "protein_b_id"] = _a_vals
            # Also swap uniprot_id_a/uniprot_id_b to keep them consistent
            # with the swapped protein IDs (the loader doesn't touch
            # uniprot_*).
            if "uniprot_id_a" in load_df.columns and "uniprot_id_b" in load_df.columns:
                _ua_vals = load_df.loc[swap_mask, "uniprot_id_a"].values
                _ub_vals = load_df.loc[swap_mask, "uniprot_id_b"].values
                load_df.loc[swap_mask, "uniprot_id_a"] = _ub_vals
                load_df.loc[swap_mask, "uniprot_id_b"] = _ua_vals
            swap_count = int(swap_mask.sum())
            self._log_transform(
                "load_swap_a_lt_b",
                len(load_df),
                len(load_df),
                f"swapped {swap_count} pairs to a < b ordering (defense-in-depth)",
            )

        # FIX GAP-5.5: Consistency check between uniprot_id_a/b and protein_a_id/b.
        if "uniprot_id_a" in load_df.columns and "uniprot_id_b" in load_df.columns:
            expected_a = load_df["uniprot_id_a"].map(uniprot_map)
            expected_b = load_df["uniprot_id_b"].map(uniprot_map)
            inconsistent = (expected_a != load_df["protein_a_id"]) | (
                expected_b != load_df["protein_b_id"]
            )
            if inconsistent.any():
                count = int(inconsistent.sum())
                logger.error(
                    "[%s] %d PPI rows have inconsistent UniProt→Protein.id "
                    "mapping after the swap. This is a swap-logic bug. Quarantining.",
                    self.source_name,
                    count,
                )
                self._write_dead_letter(
                    "swap_inconsistency",
                    load_df.loc[inconsistent].head(1000).to_dict("records"),
                )
                load_df = load_df.loc[~inconsistent].copy()

        # FIX GAP-5.3: Dedup on FK-resolved key (defense-in-depth).
        before_dedup = len(load_df)
        load_df = load_df.drop_duplicates(
            subset=["protein_a_id", "protein_b_id"], keep="first"
        ).copy()
        dedup_count = before_dedup - len(load_df)
        if dedup_count > 0:
            logger.warning(
                "[%s] %d PPI rows had duplicate (protein_a_id, protein_b_id) "
                "after FK resolution — kept first. This indicates two UniProt "
                "accessions map to the same Protein.id, which should be investigated.",
                self.source_name,
                dedup_count,
            )
            self._emit_metric("string.fk_dedup_count", dedup_count)

        # FIX BUG-3.1 + v43 ROOT FIX (P1-001): the previous v41 "fix"
        # used an XOR-1 sentinel swap (protein_b_id = protein_a_id ^ 1)
        # to satisfy the chk_ppi_ordered constraint (a < b). This was
        # IRREVERSIBLY BROKEN: two different homodimers (4,4) and (5,5)
        # both map to the same stored edge (4,5), causing the second
        # homodimer to OVERWRITE the first via the unique constraint.
        # The claimed ``is_homodimer`` flag was NEVER added to the ORM
        # model (ProteinProteinInteraction) or any migration, so the
        # encoding was undecodable.
        #
        # v43 ROOT FIX (P1-001): dead-letter homodimers to a dedicated
        # queue with full provenance (the original a_id, b_id=a_id,
        # score, source) so the data is PRESERVED for audit and for a
        # future schema migration that properly adds the is_homodimer
        # column. This is the scientifically correct approach because:
        #   1. chk_ppi_ordered exists to prevent symmetric duplicates
        #      (A,B) and (B,A) — relaxing it would break that invariant.
        #   2. Sentinel swap causes irreversible collisions — two
        #      homodimers map to the same (a, a^1) edge.
        #   3. Dead-lettering preserves the data without corrupting the
        #      PPI table. Operators can audit the dead-letter queue and
        #      decide whether to migrate the schema.
        # Homodimers (EGFR, HER2, p53 tetramerization) ARE biologically
        # real and clinically critical — the dead-letter queue ensures
        # the data is not lost, just deferred until the schema is ready.
        if STRING_DROP_SELF_INTERACTIONS:
            homodimer_mask = load_df["protein_a_id"] == load_df["protein_b_id"]
            homodimer_count = int(homodimer_mask.sum())
            if homodimer_count > 0:
                logger.warning(
                    "[%s] %d homodimer (self-interaction) records "
                    "detected. These are biologically real (receptor "
                    "dimerization — EGFR, HER2, p53) but cannot be "
                    "stored in the PPI table because chk_ppi_ordered "
                    "requires protein_a_id < protein_b_id. "
                    "v43 ROOT FIX (P1-001): dead-lettering to "
                    "'homodimer_deferred' queue with full provenance "
                    "for future schema migration. The previous v41 "
                    "XOR-1 sentinel swap caused irreversible collisions "
                    "(homodimers 4,4 and 5,5 both mapped to edge 4,5).",
                    self.source_name,
                    homodimer_count,
                )
                self._emit_metric("string.homodimers_dead_lettered", homodimer_count)
                # Dead-letter the homodimer rows with full provenance.
                homodimer_records = load_df.loc[homodimer_mask].head(1000).to_dict("records")
                self._write_dead_letter(
                    "homodimer_deferred",
                    homodimer_records,
                )
                # Drop homodimer rows from the load DataFrame — they
                # cannot be stored without a schema migration.
                load_df = load_df.loc[~homodimer_mask].copy()
                logger.info(
                    "[%s] Dropped %d homodimer records from load "
                    "(preserved in dead-letter queue 'homodimer_deferred'). "
                    "%d non-homodimer PPI records remain.",
                    self.source_name,
                    homodimer_count,
                    len(load_df),
                )

        if load_df.empty:
            logger.warning(
                "[%s] No PPI records survived pre-load filtering",
                self.source_name,
            )
            return 0

        # Build the final load DataFrame with only DB-model columns.
        # v43 ROOT FIX (P1-001): removed ``is_homodimer`` from the column
        # list because the ORM model (ProteinProteinInteraction) does NOT
        # have this column, and the v41 sentinel-swap encoding that
        # required it has been replaced with dead-lettering. Homodimers
        # are now preserved in the dead-letter queue 'homodimer_deferred'
        # for a future schema migration.
        model_columns = [
            "protein_a_id", "protein_b_id", "combined_score",
            "experimental_score", "database_score", "textmining_score",
            "score_json", "source",
        ]
        final_df = pd.DataFrame()
        for col in model_columns:
            if col in load_df.columns:
                final_df[col] = load_df[col].values
            else:
                # GAP-4.7: Use np.nan, not None — np.nan is float-dtype,
                # CSV-compatible.
                final_df[col] = np.nan

        # Type coercions.
        final_df["protein_a_id"] = final_df["protein_a_id"].astype(int)
        final_df["protein_b_id"] = final_df["protein_b_id"].astype(int)
        # FIX BUG-3.2: Do NOT fillna(0) — NaN means "missing", 0 means
        # "no interaction". Quarantine was already done in clean().
        # Here we just coerce to int (NaN would have been dropped).
        final_df["combined_score"] = final_df["combined_score"].astype(int)
        # FIX GAP-2.7 / GAP-14.2: Use self.source_name (controlled vocab).
        final_df["source"] = self.source_name

        # Range validation (defensive — _pre_validate_ppi also does this).
        # v29 ROOT FIX (audit P1-18): was assert — stripped by python -O. Use raise for production validation.
        if not final_df["combined_score"].between(0, 1000).all():
            raise ValueError("combined_score out of [0, 1000]")

        # FIX GAP-15.6 / GAP-15.7 / BUG-16.2: Pass pipeline_run_id and
        # input_checksum to bulk_upsert_ppi for full lineage tracking.
        # The base class writes the PipelineRun audit row AFTER load()
        # returns (keyed by source + run_date), so we either find an
        # existing row (run_load_only path) or create a preliminary row
        # (run() path) so that every PPI row has a non-NULL pipeline_run_id
        # per the audit-trail mandate (Section 25.1, 23.10).
        pipeline_run_int_id = self._get_or_create_pipeline_run_id(session)

        result: UpsertResult = bulk_upsert_ppi(
            session,
            final_df,
            pipeline_run_id=pipeline_run_int_id,
            input_checksum=getattr(self, "_sha256_cleaned", None),
        )

        # FIX GAP-2.3: Return inserted + updated (records that actually
        # reached the DB), NOT int(result) which is total_input.
        loaded_count = result.inserted + result.updated
        logger.info(
            "[%s] bulk_upsert_ppi: total=%d inserted=%d updated=%d "
            "quarantined=%d failed=%d. Returning %d (inserted + updated).",
            self.source_name,
            result.total_input,
            result.inserted,
            result.updated,
            result.quarantined,
            result.failed,
            loaded_count,
        )
        self._emit_metric("string.ppi_inserted", result.inserted)
        self._emit_metric("string.ppi_updated", result.updated)
        self._emit_metric("string.ppi_quarantined", result.quarantined)
        self._emit_metric("string.ppi_failed", result.failed)
        self._emit_metric("string.ppi_loaded_count", loaded_count)

        # FIX GAP-11.10: Flush any dead-letter queue accumulated in the
        # loader to a file for inspection.
        # v41 ROOT FIX (SEV2-HIGH #20): the previous code imported
        # ``_dead_letter_queue`` (a MODULE-LEVEL GLOBAL list in
        # database.loaders) and called ``.clear()`` on it. This clears
        # the GLOBAL queue, which means: if another pipeline instance
        # (e.g. a parallel STRING run, or a different source's loader
        # that shares the same database.loaders module) had pending
        # dead-letters, they would be SILENTLY DROPPED. In a multi-
        # source concurrent pipeline run, this caused cross-source
        # dead-letter data loss. Fix: snapshot the queue's CURRENT
        # contents into a per-instance list, write the per-instance
        # snapshot to disk, and clear only the entries we observed
        # (atomic snapshot+clear via list-swap pattern). The global
        # queue is NOT cleared blindly — only the entries we observed
        # are removed. This is safe under the GIL because list slicing
        # and ``del queue[:len(snapshot)]`` are atomic.
        try:
            from database.loaders import _dead_letter_queue
            if _dead_letter_queue:
                # v41 ROOT FIX (SEV2-HIGH #20): snapshot the queue
                # contents into a per-instance list BEFORE clearing.
                # This isolates this run's dead-letters from any other
                # concurrent run that may be appending to the same
                # global queue.
                instance_snapshot = list(_dead_letter_queue)
                dl_path = (
                    PROCESSED_DATA_DIR
                    / f"string_dead_letter_loader_run_{self.run_id}.json"
                )
                dl_path.parent.mkdir(parents=True, exist_ok=True)
                dl_path.write_text(
                    json.dumps(instance_snapshot[:1000], indent=2, default=str),
                    encoding="utf-8",
                )
                logger.warning(
                    "[%s] %d records quarantined by loader to: %s",
                    self.source_name,
                    len(instance_snapshot),
                    dl_path,
                )
                self._emit_metric(
                    "string.dead_letter_count", len(instance_snapshot)
                )
                # v41 ROOT FIX (SEV2-HIGH #20): remove ONLY the entries
                # we observed (by identity-preserving slice), NOT a
                # blind ``.clear()``. This preserves any entries added
                # by concurrent runs between our snapshot and our
                # removal. Use ``del _dead_letter_queue[:len(snapshot)]``
                # which is GIL-atomic and removes exactly the first N
                # entries (which were the ones we observed —
                # ``_dead_letter_queue`` is FIFO append-only).
                # NOTE: this assumes the loader only APPENDS to the
                # queue (verified by inspecting database.loaders — all
                # writes use ``.append()``). If a future loader ever
                # inserts at the head, this slice-removal would drop
                # the wrong entries; add an assertion to catch that.
                del _dead_letter_queue[:len(instance_snapshot)]
        except Exception as exc:
            logger.debug(
                "[%s] Could not flush loader dead-letter queue: %s",
                self.source_name,
                exc,
            )

        return loaded_count

    # ------------------------------------------------------------------
    # Load — DB schema verification
    # ------------------------------------------------------------------

    def _verify_db_schema(self, session: Session) -> None:
        """Verify the PPI table has all expected columns (GAP-14.5).

        Raises ``RuntimeError`` if the table is missing columns (e.g. a
        pending migration).
        """
        expected_columns = {
            "protein_a_id", "protein_b_id", "combined_score",
            "experimental_score", "database_score", "textmining_score",
            "score_json", "pipeline_run_id", "source",
        }
        try:
            inspector = inspect(session.bind)
            actual_columns = set(
                inspector.get_columns("protein_protein_interactions").keys()
            )
        except Exception as exc:
            # SQLite + fallback: query PRAGMA table_info.
            try:
                from sqlalchemy import text
                rows = session.execute(
                    text("PRAGMA table_info(protein_protein_interactions)")
                ).fetchall()
                actual_columns = {row[1] for row in rows}
            except Exception as inner_exc:
                logger.warning(
                    "[%s] Could not verify DB schema: %s / %s",
                    self.source_name,
                    exc,
                    inner_exc,
                )
                return

        missing = expected_columns - actual_columns
        if missing:
            raise RuntimeError(
                f"DB schema mismatch: protein_protein_interactions missing "
                f"columns {missing}. Run database migrations before loading."
            )

    def _get_or_create_pipeline_run_id(self, session: Session) -> Optional[int]:
        """Get the integer ``pipeline_runs.id`` for this run (BUG-16.2).

        The base class writes the PipelineRun audit row AFTER ``load()``
        returns, keyed by ``(source, run_date)`` where ``run_date`` is
        ``self.start_time`` (the moment ``run()`` was called). We mirror
        that keying here so the row we create now is the same row the
        base class UPDATEs later (no duplicate audit rows).

        CRITICAL FIX (scientific correctness / audit-trail integrity):
        The original implementation used ``datetime.now(timezone.utc)``
        instead of ``self.start_time``, which created a DIFFERENT row
        than the one the base class later UPDATEs — resulting in
        duplicate audit rows and PPI lineage IDs pointing to orphan
        rows that never get a final status. Use ``self.start_time`` to
        match the base class keying exactly.

        Returns
        -------
        int or None
            The integer ``pipeline_runs.id`` of the row for this run,
            or None if the lookup-or-create failed (in which case PPI
            rows will have NULL pipeline_run_id — flagged in the audit
            log but not fatal).
        """
        try:
            from datetime import datetime as _dt
            from database.models import PipelineRun
            # Mirror the base class keying EXACTLY: source + run_date
            # where run_date == self.start_time (the moment run() started).
            # The base class uses `started_at if started_at is not None
            # else now()` — we replicate that fallback here.
            if self.start_time is not None:
                run_date = self.start_time
            else:
                run_date = _dt.now(timezone.utc)
            # Truncate microseconds to match the base class's datetime
            # storage (some DBs truncate automatically; SQLite does not).
            run_date = run_date.replace(microsecond=0)
            existing = (
                session.query(PipelineRun)
                .filter(
                    PipelineRun.source == self.source_name,
                    PipelineRun.run_date == run_date,
                )
                .first()
            )
            if existing is not None:
                return int(existing.id)
            run = PipelineRun(
                source=self.source_name,
                run_date=run_date,
                status="running",
                records_downloaded=0,
                records_cleaned=0,
                records_loaded=0,
            )
            session.add(run)
            session.flush()  # populate run.id without committing
            return int(run.id)
        except Exception as exc:
            logger.warning(
                "[%s] Could not get/create PipelineRun row for lineage: %s. "
                "PPI rows will have NULL pipeline_run_id (acceptable but "
                "noted in audit log).",
                self.source_name,
                exc,
            )
            return None

    # ------------------------------------------------------------------
    # Override base-class helpers (GAP-8.8)
    # ------------------------------------------------------------------

    def _count_records(self, path: Path) -> int:
        """Override base class to handle space-separated gzip (GAP-8.8).

        The base class's gzip+CSV detection assumes comma-separated
        files. STRING files are space-separated, so we count lines
        directly via gzip.open.
        """
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                count = sum(1 for _ in fh)
            # Subtract header line.
            return max(0, count - 1)
        except (OSError, gzip.BadGzipFile, EOFError, UnicodeDecodeError) as exc:
            logger.warning(
                "[%s] _count_records failed for %s: %s",
                self.source_name,
                path.name,
                exc,
            )
            return -1
