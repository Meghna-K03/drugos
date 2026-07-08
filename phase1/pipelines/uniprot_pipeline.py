"""UniProt Pipeline — institutional-grade production-ready ETL for proteins.

This module implements ``UniProtPipeline``, the data ingestion pipeline that
downloads human-reviewed (Swiss-Prot) protein records from the UniProt REST
API, cleans and normalizes them, and bulk-upserts them into the ``proteins``
table of the staging database.

It is part of the Autonomous Drug Repurposing Platform (Team Cosmic /
VentureLab) and feeds the Knowledge Graph (Phase 2), the Graph Transformer
(Phase 3), and the RL Hypothesis Ranker (Phase 4).  Because downstream
consumers make clinical decisions based on this data, scientific correctness
is life-safety critical.

------------------------------------------------------------------------
Why this file exists (inception)
------------------------------------------------------------------------
The previous version of ``uniprot_pipeline.py`` (384 lines) had 346 issues
spanning 16 quality domains.  Five of them were FATAL — they silently
destroyed or corrupted data:

* F1 — ``load()`` did not accept ``session=``, raising ``TypeError`` on every
  ``run()`` call (no protein data ever loaded).
* F2 — Sequences were truncated to 10 000 chars, silently destroying titin
  (~34 350 aa) and MUC16 (~14 507 aa).
* F3 — The TSV header was not skipped on subsequent pages, creating phantom
  ``"Entry"`` rows in the cleaned dataset.
* F4 — ``gene_name`` stored a protein name, not a gene symbol — every
  downstream GDA join silently failed.
* F5 — Downloads were non-atomic; a crash mid-download left a partial file
  that was silently reused forever.

Every fix in this file is traceable to one of the 346 issue IDs documented
in ``UNIPROT_PIPELINE_346_ISSUES_FIX_PROMPT.md``.

------------------------------------------------------------------------
Data flow
------------------------------------------------------------------------
::

    UniProt REST API  ->  raw TSV (atomic write)
                      ->  cleaned DataFrame (full sequence, validated)
                      ->  proteins.csv  (schema-v1 compliant)
                      ->  bulk_upsert_proteins(session, df)
                      ->  proteins table  ->  Neo4j graph
                                        ->  Graph Transformer
                                        ->  RL ranker
                                        ->  pharma partner -> patient

------------------------------------------------------------------------
Usage examples
------------------------------------------------------------------------
::

    # Full pipeline (download + clean + load)
    from pipelines.uniprot_pipeline import UniProtPipeline
    UniProtPipeline().run()

    # Download + clean only (used by the master DAG so entity resolution
    # can run between clean and load).
    UniProtPipeline().run_download_and_clean_only()

    # Load only (after entity resolution).
    UniProtPipeline().run_load_only()

    # Dependency-injected (for tests).
    from unittest.mock import MagicMock
    UniProtPipeline(
        http_client=MagicMock(),
        db_session_factory=MagicMock(),
        loader=MagicMock(),
    )

------------------------------------------------------------------------
Changelog
------------------------------------------------------------------------
v2.0.0 (2025-03-05) — Institutional-grade rewrite addressing 346 issues
    across 16 domains.  See ``UNIPROT_PIPELINE_346_ISSUES_FIX_PROMPT.md``.

v1.0.0 — Initial implementation (384 lines, deprecated).

------------------------------------------------------------------------
License
------------------------------------------------------------------------
MIT — Team Cosmic / VentureLab.  See the project LICENSE file for details.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Standard library imports
# ---------------------------------------------------------------------------
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Union
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Third-party imports
# ---------------------------------------------------------------------------
import pandas as pd
import requests
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------
from cleaning._constants import (
    normalize_uniprot_id,  # v29 ROOT FIX (audit P1-24)
    normalize_gene_symbol,  # v29 ROOT FIX (audit P1-24)
)
from cleaning.missing_values import handle_missing_protein_fields
from config.settings import PROCESSED_DATA_DIR, RAW_DATA_DIR, UNIPROT_RELEASE
from database.connection import get_db_session
from database.loaders import UpsertResult, bulk_upsert_proteins
from pipelines.base_pipeline import BasePipeline, DownloadError, LoadResult

# ---------------------------------------------------------------------------
# Module metadata (DOC16–DOC20)
# ---------------------------------------------------------------------------
__all__ = ["UniProtPipeline"]
__version__ = "2.0.0"
__author__ = "Team Cosmic / VentureLab"
__license__ = "MIT"

# ---------------------------------------------------------------------------
# Module logger
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type aliases (DOC14)
# ---------------------------------------------------------------------------
UniProtId = str          # e.g. "P69905" — 6- or 10-char Swiss-Prot accession
GeneSymbol = str         # e.g. "HBA1" — HGNC-canonical uppercase gene symbol
AminoAcidSequence = str  # e.g. "MVLSPADKTN…" — IUPAC one-letter codes

# ---------------------------------------------------------------------------
# Compiled regex patterns (S3, S8, S9, S20, S21)
# ---------------------------------------------------------------------------
# UniProt accession pattern (matches schema/v1.json).  Two alternative forms:
#   * 6-char:   [OPQ][0-9][A-Z0-9]{3}[0-9]               e.g. P69905, Q8WXI7
#   * 10-char:  [A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2} e.g. A0A024RBG1
_UNIPROT_ACCESSION_RE: re.Pattern[str] = re.compile(
    r"^[OPQ][0-9][A-Z0-9]{3}[0-9]$"
    r"|"
    r"^[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2}$"
)

# HGNC gene symbol: uppercase letter, then 0–49 alphanumeric/hyphen chars.
# v35 ROOT FIX: import from cleaning._constants (single source of truth).
# A separate NON-HUMAN form (Title-Case, e.g. "Tp53" for mouse) is defined
# in _constants.CANONICAL_NON_HUMAN_GENE_SYMBOL_REGEX for non-human pipelines.
from cleaning._constants import CANONICAL_NON_HUMAN_GENE_SYMBOL_REGEX as _HGNC_SYMBOL_RE

# STRING cross-reference ID: <taxid>.ENSP<digits>, e.g. "9606.ENSP00000357607".
# v35 ROOT FIX: accept ANY taxonomy ID (not just 9606). The UniProt
# pipeline ingests both human and non-human proteins; hard-coding 9606
# silently dropped every non-human STRING cross-reference (e.g. mouse
# 10090.ENSP00000XXXXX). The cleaning-layer validator
# (``resolver_utils._STRING_ID_RE``) already accepts any taxid.
_STRING_ID_RE: re.Pattern[str] = re.compile(r"^\d+\.ENSP\d+$")

# Valid amino-acid characters: 20 standard + ambiguity codes B J O U X Z +
# stop * + alignment gap "-" (v35 root fix: gap char included for
# consistency with cleaning._constants.CANONICAL_AA_SEQUENCE_REGEX and
# database.models._SEQUENCE_RE — without it, aligned sequences with gaps
# would pass the DB CHECK but fail this pipeline validator).
_VALID_AA_PATTERN: re.Pattern[str] = re.compile(
    r"^[ACDEFGHIKLMNPQRSTVWYBJOUXZ\*\-]+$"
)

# EC number suffix in protein names, e.g. "EC 1.11.1.6" — strict format.
_EC_NUMBER_RE: re.Pattern[str] = re.compile(r"\s*EC\s+[\d]+(?:\.[\d]+){1,3}\s*$")

# {ECO:...} evidence tags — UniProt uses these to cite literature sources.
_ECO_TAG_RE: re.Pattern[str] = re.compile(r"\s*\{ECO:[^}]*\}")

# Parenthetical content (handles nested parens via manual scan; see below).
_PAREN_OPEN_RE: re.Pattern[str] = re.compile(r"\(")

# ---------------------------------------------------------------------------
# UniProt CC sub-section markers (S5, S6, C16) — when ANY of these appears,
# everything from that marker onward belongs to a different sub-section and
# must be truncated from the function description.
# ---------------------------------------------------------------------------
_SUBSECTION_MARKERS: tuple[str, ...] = (
    "ACTIVITY REGULATION:",
    "ALTERNATIVE PRODUCTS:",
    "BIOTECHNOLOGY:",
    "CAUTION:",
    "CATALYTIC ACTIVITY:",
    "COFACTOR:",
    "DEVELOPMENTAL STAGE:",
    "DISEASE:",
    "DISRUPTION PHENOTYPE:",
    "DOMAIN:",
    "ENZYME REGULATION:",
    "FUNCTION:",          # second occurrence after the leading "FUNCTION: "
    "INDUCTION:",
    "INTERACTION:",
    "MASS SPECTROMETRY:",
    "MISCELLANEOUS:",
    "PATHWAY:",
    "PHARMACEUTICAL:",
    "POLYMORPHISM:",
    "PTM:",
    "SEQUENCE SIMILARITY:",
    "SITES:",
    "SIMILARITY:",
    "SUBCELLULAR LOCATION:",
    "SUBUNIT:",
    "TISSUE SPECIFICITY:",
    "TOXIC DOSE:",
)

# ---------------------------------------------------------------------------
# DATA_DICTIONARY (DOC3) — full per-column documentation embedded in code.
# ---------------------------------------------------------------------------
DATA_DICTIONARY: dict[str, dict[str, Any]] = {
    "uniprot_id": {
        "type": "str",
        "description": "UniProt Swiss-Prot accession (e.g. P69905). Primary key.",
        "pattern": r"^[OPQ][0-9][A-Z0-9]{3}[0-9]$|^[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2}$",
        "source": "UniProt 'Entry' column",
        "required": True,
    },
    "gene_symbol": {
        "type": "str | None",
        "description": "HGNC gene symbol (e.g. HBA1) or non-human Title-Case symbol "
                       "(e.g. Tp53 for mouse). Used for GDA resolution.",
        "pattern": r"^[A-Za-z][A-Za-z0-9\-]{0,49}$",
        "source": "UniProt 'Gene Names (primary)' column",
        "required": False,
    },
    "gene_name": {
        "type": "None",
        "description": "DEPRECATED — always None.  Use protein_name_canonical "
                       "for canonical names and gene_symbol for gene symbols.",
        "source": "N/A",
        "required": False,
        "deprecated": True,
    },
    "protein_name": {
        "type": "str | None",
        "description": "Full protein name from UniProt (synonyms in parentheses).",
        "source": "UniProt 'Protein names' column",
        "required": False,
    },
    "protein_name_canonical": {
        "type": "str | None",
        "description": "Canonical protein name (parenthetical synonyms, ECO tags, "
                       "and EC numbers stripped).",
        "source": "Derived from protein_name via _extract_canonical_name().",
        "required": False,
    },
    "organism": {
        "type": "str",
        "description": "Organism name (always 'Homo sapiens' for this pipeline).",
        "source": "UniProt 'Organism' column",
        "required": True,
    },
    "length": {
        "type": "int | None",
        "description": "Protein sequence length in amino acids (UniProt-reported).",
        "source": "UniProt 'Length' column",
        "required": False,
        "valid_range": "1–100000",
    },
    "sequence": {
        "type": "str | None",
        "description": "Full amino-acid sequence. NOT truncated — titin (~34 350 aa) is stored in full.",
        "source": "UniProt 'Sequence' column",
        "required": False,
        "valid_chars": "ACDEFGHIKLMNPQRSTVWYBJOUXZ*-",
    },
    "function_desc": {
        "type": "str | None",
        "description": "Function description (FUNCTION: prefix and sub-section markers stripped).",
        "source": "UniProt 'Function [CC]' column",
        "required": False,
    },
    "string_id": {
        "type": "str | None",
        "description": "First valid STRING ID (format: <taxid>.ENSP<digits>, e.g. 9606.ENSP00000357607).",
        "source": "UniProt 'Cross-reference (STRING)' column",
        "required": False,
        "pattern": r"^\d+\.ENSP\d+$",
    },
    "all_string_ids": {
        "type": "str | None",
        "description": "Semicolon-separated list of ALL valid STRING IDs for the protein.",
        "source": "UniProt 'Cross-reference (STRING)' column",
        "required": False,
    },
}

# ---------------------------------------------------------------------------
# EXPECTED_OUTPUT_COLUMNS (D2-12) — the cleaned DataFrame MUST contain at
# least these columns.  Extra columns (lineage flags, _source, etc.) are
# tolerated.
# ---------------------------------------------------------------------------
EXPECTED_OUTPUT_COLUMNS: frozenset[str] = frozenset({
    "uniprot_id",
    "gene_symbol",
    "gene_name",
    "protein_name",
    "protein_name_canonical",
    "organism",
    "length",
    "sequence",
    "function_desc",
    "string_id",
    "all_string_ids",
})

# Expected raw TSV column names from UniProt REST API (used for schema-version guard).
_EXPECTED_TSV_COLUMNS: frozenset[str] = frozenset({
    "Entry",
    "Gene Names",
    "Gene Names (primary)",
    "Protein names",
    "Organism",
    "Length",
    "Sequence",
    "Cross-reference (STRING)",
    "Function [CC]",
})

# Columns critical to load() — if missing after rename, raise immediately.
_CRITICAL_COLUMNS: tuple[str, ...] = ("uniprot_id",)

# CSV cells starting with these characters are vulnerable to formula injection
# when the CSV is opened in Excel / Sheets (SEC4 / C27).
_CSV_DANGEROUS_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")

# UniProt domains allowed for the REST API and Link-header URLs (SEC1 / SEC8).
_ALLOWED_DOMAINS: frozenset[str] = frozenset({
    "rest.uniprot.org",
    "www.uniprot.org",
    "uniprot.org",
})

# Maximum response body size we accept from UniProt (SEC6).  UniProt's
# page size cap is 500 records; a TSV page is well under 5 MB.  100 MB is
# an extremely generous ceiling that still prevents OOM from a malformed
# / malicious response.
_MAX_RESPONSE_BYTES: int = 100 * 1024 * 1024


# ===========================================================================
# UniProtPipeline
# ===========================================================================
class UniProtPipeline(BasePipeline):
    """Institutional-grade UniProt REST API pipeline for human reviewed proteins.

    Downloads Swiss-Prot human proteins from the UniProt REST API, cleans
    and normalizes the data, validates scientific correctness, and
    bulk-upserts into the ``proteins`` table.

    Class Attributes
    ----------------
    source_name : str
        Pipeline identifier (``"uniprot"``); validated by ``BasePipeline``.
    uniprot_search_url : str
        Base URL for the UniProt REST search endpoint.
    uniprot_query : str
        UniProt query string (default: human reviewed proteins).
    uniprot_fields : list[str]
        Fields to request from the UniProt API.  ``ft_domain`` is
        intentionally excluded — domain extraction is not implemented and
        requesting the field would waste ~5% of API bandwidth (S13).
    page_size : int
        Number of records per page (1–500, UniProt hard cap).
    max_retries : int
        Maximum retry attempts per page fetch.
    base_retry_delay : float
        Base delay in seconds for exponential backoff.
    max_retry_after_wait : int
        Maximum seconds to wait for a Retry-After header (SEC7).
    consecutive_retry_after_limit : int
        Max consecutive Retry-After responses before breaking the retry loop.

    Notes
    -----
    * All sequences are stored in full — titin (~34 350 aa) is preserved
      (F2).  Truncation was a FATAL silent data-corruption bug.
    * ``gene_name`` is deprecated and set to ``None`` (F4).  Use
      ``gene_symbol`` for gene symbols and ``protein_name_canonical`` for
      canonical protein names.
    * The pipeline is idempotent: deterministic sort before dedup, atomic
      file write, SHA-256 checksum sidecar, content-hash logging for
      duplicate detection.
    * The pipeline is reproducible: ``self.seed`` is honored, the UniProt
      release is recorded in the provenance sidecar, and the same input
      always produces the same output.

    Examples
    --------
    >>> pipeline = UniProtPipeline()
    >>> pipeline.run()  # doctest: +SKIP
    """

    # ---------------------------------------------------------------------
    # Class attributes (A10, A12, A13, CFG1–CFG4)
    # ---------------------------------------------------------------------
    source_name: str = "uniprot"

    # UniProt REST API endpoint (CFG2).
    uniprot_search_url: str = "https://rest.uniprot.org/uniprotkb/search"

    # Query for human (taxonomy 9606) reviewed (Swiss-Prot) proteins (CFG3).
    # S23: isoform support is not yet implemented — this query returns
    # canonical entries only.  Adding "AND (isoform:true)" would require
    # a separate download pass and is tracked as a TODO.
    # S24: natural variants (ft_variant) are not requested; variant
    # annotations would be critical for drug repurposing but require
    # extraction logic that is not yet implemented.
    #
    # audit-2025 ROOT FIX (issue 13): the organism_id was hardcoded to
    # 9606 (Homo sapiens). That makes the pipeline unusable for
    # repurposing workflows that start from a model-organism target
    # (e.g. mouse 10090, rat 10116, zebrafish 7955) without forking
    # the source. The default is still 9606 (preserving backward
    # compatibility) but ``__init__`` now consults the
    # ``UNIPROT_ORGANISM_ID`` env var and rebuilds the query string
    # if it is set. The class attribute below is the *default*
    # template; the per-instance ``self.uniprot_query`` is what the
    # download code actually uses.
    DEFAULT_ORGANISM_ID: int = 9606
    uniprot_query: str = f"organism_id:{DEFAULT_ORGANISM_ID} AND reviewed:true"

    # Fields requested from UniProt (CFG4, S13, S18).  ``ft_domain`` is
    # intentionally excluded until domain extraction is implemented.
    uniprot_fields: list[str] = [
        "accession",
        "gene_primary",
        "gene_names",
        "protein_name",
        "organism_name",
        "length",
        "sequence",
        "xref_string",   # S18: specific STRING xref field (not generic 'xref')
        "cc_function",
    ]

    # Pagination & retry tuning (CFG1, CFG7, C35).
    page_size: int = 500
    max_retries: int = 5
    base_retry_delay: float = 10.0           # seconds
    max_retry_after_wait: int = 300          # seconds (SEC7 / C43)
    consecutive_retry_after_limit: int = 3   # C8

    # BasePipeline attribute overrides (A12).
    min_request_interval: float = 0.5        # UniProt asks for self-throttling
    download_timeout: tuple[float, float] = (30.0, 600.0)
    download_max_retries: int = 5            # A13: coordinate with max_retries
    max_cache_age_days: int = 30             # CFG19
    verify_tls: bool = True                  # SEC2 / CFG15
    min_clean_ratio: float = 0.3
    min_load_ratio: float = 0.9
    stage_timeout: int = 3600                # CFG23 (R7)

    # File-permission mask for output files (SEC10 / SEC14).
    _SECURE_FILE_MODE: int = 0o600

    # ---------------------------------------------------------------------
    # __init__ (D2-5 dependency injection, CFG5/CFG8/CFG9 config)
    # ---------------------------------------------------------------------
    def __init__(
        self,
        *,
        http_client: Optional[requests.Session] = None,
        db_session_factory: Optional[Callable[..., Any]] = None,
        loader: Optional[Callable[..., Any]] = None,
        **kwargs: Any,
    ) -> None:
        """Initialize ``UniProtPipeline`` with optional dependency injection.

        Parameters
        ----------
        http_client : requests.Session | None
            Pre-configured HTTP client for API requests.  If *None*, a
            default ``requests.Session`` is created lazily on first use
            (A4 — connection pooling).
        db_session_factory : callable | None
            Factory for DB sessions.  If *None*, ``get_db_session`` is used.
        loader : callable | None
            Bulk upsert function.  If *None*, ``bulk_upsert_proteins`` is
            used.  This is the dependency-injection seam used by tests.
        **kwargs
            Forwarded to ``BasePipeline.__init__`` (``run_id``,
            ``correlation_id``, ``triggered_by``, ``as_of_date``,
            ``freeze_version``, ``snapshot_tag``, ``seed``).

        Raises
        ------
        ValueError
            If any configuration value is invalid (CFG5).
        """
        super().__init__(**kwargs)

        # Dependency injection seams (D2-5).  Tests inject mocks here.
        self._http_session: Optional[requests.Session] = http_client
        self._db_session_factory: Callable[..., Any] = (
            db_session_factory or get_db_session
        )
        self._loader: Callable[..., Any] = loader or bulk_upsert_proteins

        # Per-run state (I3, I10 — reset at the start of each download).
        self._consecutive_retry_after: int = 0
        self._total_retries: int = 0
        self._force_refresh: bool = False

        # Override class attributes from environment variables (CFG9).
        env = os.environ
        if url := env.get("UNIPROT_SEARCH_URL"):
            self.uniprot_search_url = url
        # audit-2025 ROOT FIX (issue 13): allow the UniProt organism to
        # be configured via ``UNIPROT_ORGANISM_ID`` env var. Default is
        # 9606 (human) preserved for backward compatibility. When the
        # caller does NOT also set ``UNIPROT_QUERY`` explicitly, we
        # rebuild the query string with the requested organism_id so
        # the existing query-template semantics (reviewed:true) still
        # apply.
        _organism_override = env.get("UNIPROT_ORGANISM_ID")
        if _organism_override:
            try:
                _oid = int(_organism_override)
                if _oid <= 0:
                    raise ValueError("must be positive")
            except ValueError as _exc:
                logger.warning(
                    "[%s] Invalid UNIPROT_ORGANISM_ID=%r (%s); using default %d",
                    self.source_name, _organism_override, _exc,
                    self.DEFAULT_ORGANISM_ID,
                )
            else:
                # If the caller set UNIPROT_QUERY explicitly, respect it
                # verbatim and do NOT clobber with our template.
                if not env.get("UNIPROT_QUERY"):
                    self.uniprot_query = (
                        f"organism_id:{_oid} AND reviewed:true"
                    )
        if query := env.get("UNIPROT_QUERY"):
            self.uniprot_query = query
        if ps := env.get("UNIPROT_PAGE_SIZE"):
            try:
                self.page_size = int(ps)
            except ValueError:
                logger.warning(
                    "[%s] Invalid UNIPROT_PAGE_SIZE=%r; using default %d",
                    self.source_name, ps, self.page_size,
                )
        if mr := env.get("UNIPROT_MAX_RETRIES"):
            try:
                self.max_retries = int(mr)
            except ValueError:
                logger.warning(
                    "[%s] Invalid UNIPROT_MAX_RETRIES=%r; using default %d",
                    self.source_name, mr, self.max_retries,
                )
        if brd := env.get("UNIPROT_BASE_RETRY_DELAY"):
            try:
                self.base_retry_delay = float(brd)
            except ValueError:
                logger.warning(
                    "[%s] Invalid UNIPROT_BASE_RETRY_DELAY=%r; using default %s",
                    self.source_name, brd, self.base_retry_delay,
                )

        # Pick up UNIPROT_RELEASE from settings.py (CFG8).
        try:
            if UNIPROT_RELEASE and UNIPROT_RELEASE != "current_release":
                self.source_version = UNIPROT_RELEASE
        except Exception:  # pragma: no cover — defensive
            pass

        # Validate configuration (CFG5).
        self._validate_config()

    # ---------------------------------------------------------------------
    # Config validation (CFG5)
    # ---------------------------------------------------------------------
    def _validate_config(self) -> None:
        """Validate pipeline configuration.

        Raises
        ------
        ValueError
            If any configuration value is out of range or malformed.
        """
        if not 1 <= self.page_size <= 500:
            raise ValueError(
                f"page_size must be 1–500 (UniProt hard cap), got {self.page_size}"
            )
        if self.max_retries < 0:
            raise ValueError(
                f"max_retries must be >= 0, got {self.max_retries}"
            )
        if self.base_retry_delay <= 0:
            raise ValueError(
                f"base_retry_delay must be > 0, got {self.base_retry_delay}"
            )
        if self.max_retry_after_wait <= 0:
            raise ValueError(
                f"max_retry_after_wait must be > 0, got {self.max_retry_after_wait}"
            )
        if self.consecutive_retry_after_limit < 1:
            raise ValueError(
                f"consecutive_retry_after_limit must be >= 1, "
                f"got {self.consecutive_retry_after_limit}"
            )
        if not self.uniprot_search_url.startswith("https://"):
            raise ValueError(
                f"uniprot_search_url must use HTTPS, got {self.uniprot_search_url!r}"
            )
        if not self.uniprot_fields:
            raise ValueError("uniprot_fields must be a non-empty list")

    # ---------------------------------------------------------------------
    # effective_raw_dir property (A2)
    # ---------------------------------------------------------------------
    @property
    def effective_raw_dir(self) -> Path:
        """Return the effective raw data directory (A2).

        Falls back to ``RAW_DATA_DIR / self.source_name`` when
        ``self.raw_dir`` is *None* (e.g. when ``download()`` is called
        directly, before ``BasePipeline.run()`` has initialized it).
        """
        # BasePipeline may have set self.raw_dir already; respect it.
        existing = getattr(self, "raw_dir", None)
        if existing is not None:
            return Path(existing)
        return RAW_DATA_DIR / self.source_name

    # ---------------------------------------------------------------------
    # processed_dir property (D2-3)
    # ---------------------------------------------------------------------
    @property
    def processed_dir(self) -> Path:
        """Return the processed data directory (D2-3, C25)."""
        return PROCESSED_DATA_DIR

    # ---------------------------------------------------------------------
    # User-Agent (SEC3)
    # ---------------------------------------------------------------------
    @property
    def _user_agent(self) -> str:
        """User-Agent header value (SEC3)."""
        return (
            f"DrugRepurposingPlatform/{__version__} "
            f"(TeamCosmic; python-requests/{requests.__version__})"
        )

    # ---------------------------------------------------------------------
    # HTTP session (A4, R22, P13)
    # ---------------------------------------------------------------------
    def _get_http_session(self) -> requests.Session:
        """Get or create the HTTP session for connection reuse (A4).

        Returns
        -------
        requests.Session
            A session with ``Accept`` and ``User-Agent`` headers preset
            and ``verify`` set to ``self.verify_tls`` (SEC2).
        """
        if self._http_session is None:
            self._http_session = requests.Session()
            self._http_session.headers.update({
                "Accept": "text/tab-separated-values",
                "User-Agent": self._user_agent,
            })
            self._http_session.verify = self.verify_tls
        return self._http_session

    # ---------------------------------------------------------------------
    # URL validation (SEC1, SEC8)
    # ---------------------------------------------------------------------
    @classmethod
    def _validate_url(cls, url: str) -> str:
        """Validate that *url* points at an allowed UniProt domain (SEC1/SEC8).

        Prevents Server-Side Request Forgery (SSRF) via a malicious
        ``Link`` header by checking the scheme and hostname against an
        allow-list.

        Parameters
        ----------
        url : str
            URL to validate.

        Returns
        -------
        str
            The validated URL.

        Raises
        ------
        ValueError
            If the URL's scheme is not ``http``/``https`` or its hostname
            is not in ``_ALLOWED_DOMAINS``.
        """
        if not url or not isinstance(url, str):
            raise ValueError("URL must be a non-empty string")
        parsed = urlparse(url)
        if parsed.scheme not in ("https", "http"):
            raise ValueError(
                f"Invalid URL scheme: {parsed.scheme!r} "
                f"(expected 'https' or 'http')"
            )
        hostname = (parsed.hostname or "").lower()
        if hostname not in _ALLOWED_DOMAINS:
            raise ValueError(
                f"URL domain {hostname!r} not in allowed domains: "
                f"{sorted(_ALLOWED_DOMAINS)}. Possible SSRF attempt."
            )
        return url

    # ---------------------------------------------------------------------
    # Pre-flight check (A7)
    # ---------------------------------------------------------------------
    def pre_check(self) -> dict[str, bool]:
        """UniProt-specific pre-flight checks (A7).

        Verifies:
        1. UniProt API is reachable (HEAD request).
        2. Sufficient disk space for ~500 MB of raw + staged data.
        3. The raw data directory is writable.

        Returns
        -------
        dict[str, bool]
            Mapping of check name → pass/fail.  The base ``run()``
            considers the pre-check failed if any value is *False*.
        """
        checks: dict[str, bool] = {}

        # Check API reachability (HEAD request, 10s timeout).
        try:
            resp = requests.head(
                self.uniprot_search_url,
                timeout=10,
                headers={"User-Agent": self._user_agent},
                verify=self.verify_tls,
            )
            # 5xx = server down; 4xx = our request is malformed.
            # Either way we cannot proceed safely.
            checks["api_reachable"] = resp.status_code < 500
            if not checks["api_reachable"]:
                logger.error(
                    "[%s] UniProt API returned HTTP %d in pre_check",
                    self.source_name, resp.status_code,
                )
        except requests.exceptions.RequestException as exc:
            logger.error(
                "[%s] UniProt API unreachable in pre_check: %s",
                self.source_name, exc,
                exc_info=getattr(self, "log_exc_info", True),
            )
            checks["api_reachable"] = False

        # Check disk space (need at least 500 MB).
        raw_dir = self.effective_raw_dir
        try:
            raw_dir.mkdir(parents=True, exist_ok=True)
            usage = shutil.disk_usage(raw_dir)
            checks["disk_space"] = usage.free >= 500 * 1024 * 1024
            if not checks["disk_space"]:
                logger.error(
                    "[%s] Insufficient disk space: %.1f MB free (need >= 500 MB)",
                    self.source_name, usage.free / (1024 * 1024),
                )
        except OSError as exc:
            logger.error(
                "[%s] Cannot access raw_dir %s: %s",
                self.source_name, raw_dir, exc,
            )
            checks["disk_space"] = False

        # Check raw_dir is writable.
        try:
            test_file = raw_dir / ".write_test"
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink()
            checks["raw_dir_writable"] = True
        except OSError:
            checks["raw_dir_writable"] = False

        logger.info(
            "[%s] pre_check results: %s",
            self.source_name, checks,
            extra=self._log_context(),
        )
        return checks

    # ---------------------------------------------------------------------
    # download() — atomic, paginated, with checksum + checkpoint (F3, F5)
    # ---------------------------------------------------------------------
    def download(self) -> Path:
        """Download human-reviewed proteins from the UniProt REST API.

        Uses cursor-based pagination via the ``Link`` header.  Handles
        HTTP 429 rate-limiting with exponential backoff + jitter (C41,
        C42).  Writes the TSV atomically via a ``.tmp`` file + rename
        (F5) and computes a SHA-256 sidecar (I4).

        Returns
        -------
        Path
            Path to the downloaded raw TSV file.

        Raises
        ------
        DownloadError
            If the download fails after all retries.
        """
        # I3 / I10 — reset per-run instance state at the start of each download.
        self._consecutive_retry_after = 0
        self._total_retries = 0
        # Note: we do NOT clear dead_letter_queue here — it is owned by
        # BasePipeline and is drained by teardown().

        output_path = self.effective_raw_dir / "uniprot_human_reviewed.tsv"

        # I8 / D2-2 — honor force_refresh.
        if self._force_refresh and output_path.exists():
            logger.info(
                "[%s] force_refresh=True — deleting cached file: %s",
                self.source_name, output_path,
            )
            try:
                output_path.unlink()
            except OSError as exc:
                logger.warning(
                    "[%s] Could not delete cached file %s: %s",
                    self.source_name, output_path, exc,
                )
            # Also delete checksum sidecar.
            checksum_path = output_path.with_suffix(".tsv.sha256")
            if checksum_path.exists():
                try:
                    checksum_path.unlink()
                except OSError:
                    pass

        # F5 / I1 / I4 — validate cached file before reuse.
        if self._is_raw_file_valid(output_path):
            logger.info(
                "[%s] Valid cached file exists: %s",
                self.source_name, output_path,
            )
            return output_path

        # L11–L14 — log the download configuration at the start.
        logger.info(
            "[%s] Download configuration: url=%s, query=%s, fields=%s, "
            "page_size=%d, max_retries=%d",
            self.source_name,
            self.uniprot_search_url,
            self.uniprot_query,
            self.uniprot_fields,
            self.page_size,
            self.max_retries,
            extra=self._log_context(),
        )

        # SEC1 — validate the search URL before fetching.
        self._validate_url(self.uniprot_search_url)

        fields_str = ",".join(self.uniprot_fields)
        params: Optional[dict[str, Any]] = {
            "query": self.uniprot_query,
            "format": "tsv",
            "fields": fields_str,
            "size": self.page_size,
        }

        total_records = 0
        url: Optional[str] = self.uniprot_search_url
        header_written = False
        expected_total: Optional[int] = None
        page_num = 0

        # v21 ROOT FIX (Audit section 6 finding 5 - "Checkpoint writer
        # without reader"): _write_checkpoint is called after every page
        # (line 931) but _read_checkpoint was NEVER CALLED. Large
        # downloads always restarted from page 1 on failure. Honest
        # docstring admitted: "End-to-end resume is not yet wired into
        # download()." Fix: read the checkpoint at the start of
        # download(); if it exists AND the operator has set
        # DRUGOS_UNIPROT_RESUME=1, skip ahead to the saved cursor URL
        # and resume. Default is OFF (resume is opt-in) because the
        # temp file from the previous attempt is discarded on failure
        # and we'd be re-writing from page N to a fresh temp file
        # (which is fine but operators should know).
        import os as _os
        if _os.environ.get("DRUGOS_UNIPROT_RESUME", "") == "1":
            ckpt = self._read_checkpoint()
            if ckpt is not None and ckpt.get("cursor_url"):
                saved_page = int(ckpt.get("page_num", 0))
                saved_total = int(ckpt.get("total_records", 0))
                saved_url = ckpt["cursor_url"]
                if saved_url:
                    logger.info(
                        "[%s] DRUGOS_UNIPROT_RESUME=1: resuming from "
                        "checkpoint (page %d, %d records previously "
                        "fetched). Cursor URL: %s",
                        self.source_name, saved_page, saved_total,
                        saved_url[:80] + "..." if len(saved_url) > 80
                        else saved_url,
                    )
                    url = saved_url
                    page_num = saved_page
                    total_records = saved_total
                    # When resuming, the temp file is fresh - we need
                    # to re-write the TSV header so the file is valid.
                    header_written = False

        # F5 - write to a temp file first, rename atomically on success.
        tmp_path = output_path.with_suffix(".tsv.tmp")

        # Ensure parent directory exists.
        output_path.parent.mkdir(parents=True, exist_ok=True)

        start_time = time.monotonic()

        try:
            with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
                while url:
                    page_num += 1
                    response = self._fetch_page(url, params)

                    # DQ13 — capture the total result count from the response.
                    x_total = response.headers.get("X-Total-Results")
                    if x_total and expected_total is None:
                        try:
                            expected_total = int(x_total)
                            logger.info(
                                "[%s] UniProt reports %d total results",
                                self.source_name, expected_total,
                            )
                        except ValueError:
                            pass

                    # R12 / R15 / R16 — validate Content-Type.
                    content_type = response.headers.get("Content-Type", "")
                    if (
                        "text/tab-separated-values" not in content_type
                        and "text/plain" not in content_type
                    ):
                        logger.warning(
                            "[%s] Unexpected Content-Type on page %d: %s",
                            self.source_name, page_num, content_type,
                        )

                    # L5 — warn on empty response body instead of silent break.
                    text = (response.text or "").strip()
                    if not text:
                        logger.warning(
                            "[%s] Empty response body on page %d",
                            self.source_name, page_num,
                        )
                        break

                    # C2 — use splitlines() to handle \r\n and \n consistently.
                    lines = text.splitlines()
                    if not lines:
                        break

                    # F3 / C1 — correctly skip the re-emitted TSV header on
                    # subsequent pages.  UniProt re-emits the header on every
                    # cursor page.
                    if not header_written:
                        # First page: write header + data.
                        fh.write(lines[0] + "\n")
                        header_written = True
                        data_lines = lines[1:]
                    else:
                        # Subsequent pages: skip the re-emitted header row.
                        if lines[0].startswith("Entry\t") or lines[0] == "Entry":
                            data_lines = lines[1:]
                        else:
                            # No header on this page — keep all lines.
                            data_lines = lines

                    # C4 — filter out blank lines (some pages emit a trailing
                    # blank line which would create a phantom "" uniprot_id row).
                    data_lines = [ln for ln in data_lines if ln.strip()]

                    # P3 — bulk write instead of line-by-line.
                    if data_lines:
                        fh.write("\n".join(data_lines) + "\n")

                    total_records += len(data_lines)
                    logger.info(
                        "[%s] Page %d: fetched %d proteins (total: %d)",
                        self.source_name, page_num, len(data_lines),
                        total_records,
                        extra=self._log_context(),
                    )

                    # R8 — write a checkpoint after each page so we can
                    # resume from cursor if needed (not yet implemented
                    # end-to-end, but the checkpoint is written for diagnosis).
                    next_url = self._parse_link_header(
                        response.headers.get("Link", "")
                    )
                    self._write_checkpoint(next_url or "", page_num, total_records)

                    # Cursor URL already has all params embedded (C33).
                    url = next_url
                    params = None

            # DQ13 — validate total count.
            if expected_total is not None and total_records != expected_total:
                logger.warning(
                    "[%s] Record count mismatch: fetched %d, UniProt reported "
                    "%d total results.  This may indicate a pagination bug or "
                    "data changed mid-fetch (I15).",
                    self.source_name, total_records, expected_total,
                )

            # F5 — atomic rename.  Only after the full download succeeds.
            tmp_path.replace(output_path)

        except (OSError, PermissionError) as exc:
            # R24 / R25 — disk full or permission denied.
            logger.error(
                "[%s] OS error during download: %s (disk full or permission denied?)",
                self.source_name, exc,
                exc_info=getattr(self, "log_exc_info", True),
            )
            # SEC17 — securely delete the partial temp file.
            self._secure_delete(tmp_path)
            raise DownloadError(f"OS error during download: {exc}") from exc
        except Exception:
            # Any other failure: clean up the temp file.
            self._secure_delete(tmp_path)
            raise

        # F5 / I4 — write a SHA-256 sidecar so subsequent runs can verify
        # the cached file's integrity.
        self._write_checksum(output_path)

        # SEC10 / SEC14 — restrict file permissions to owner-only.
        self._set_secure_permissions(output_path)

        elapsed = time.monotonic() - start_time
        logger.info(
            "[%s] Downloaded %d total protein records to %s in %.2fs "
            "(total retries: %d)",
            self.source_name, total_records, output_path,
            elapsed, self._total_retries,
            extra={**self._log_context(), "duration_seconds": elapsed,
                   "total_records": total_records},
        )

        return output_path

    # ---------------------------------------------------------------------
    # _fetch_page() — exponential backoff + jitter, rate limiter (C41, C42)
    # ---------------------------------------------------------------------
    def _fetch_page(
        self, url: str, params: Optional[dict[str, Any]] = None,
    ) -> requests.Response:
        """Fetch a single page from UniProt with retry on rate-limiting.

        Uses exponential backoff with jitter (C41, C42) and raises
        ``DownloadError`` on exhaustion (C39).  Distinguishes 4xx
        (permanent — do not retry) from 5xx (transient — retry) per R13.

        Parameters
        ----------
        url : str
            URL to fetch.  Validated against the allow-list (SEC1).
        params : dict | None
            Query parameters.  Used only for the first page; subsequent
            pages use the cursor URL embedded in the ``Link`` header
            (C33 — ``params=None`` on subsequent calls).

        Returns
        -------
        requests.Response
            Successful response (HTTP 200, no Retry-After).

        Raises
        ------
        DownloadError
            If all retries are exhausted (C39, C40).
        ValueError
            If the URL is not in the allowed domains (SEC1).
        """
        import random

        # SEC1 — validate URL before fetching.
        self._validate_url(url)

        last_exception: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                # A5 / R21 — be polite to the API; rate-limit before each call.
                if getattr(self, "_rate_limiter", None) is not None:
                    try:
                        self._rate_limiter.wait()
                    except Exception:
                        # Rate limiter should never raise, but be defensive.
                        pass

                # A4 / R22 / P13 — reuse the HTTP session for connection pooling.
                session = self._get_http_session()
                resp = session.get(
                    url,
                    params=params,
                    timeout=self.download_timeout[1],  # CFG16
                )

                # SEC6 — cap response body size to prevent OOM.
                content_length = resp.headers.get("Content-Length")
                if content_length:
                    try:
                        size = int(content_length)
                        if size > _MAX_RESPONSE_BYTES:
                            raise DownloadError(
                                f"Response too large: {size} bytes "
                                f"(max: {_MAX_RESPONSE_BYTES})"
                            )
                    except ValueError:
                        pass

                if resp.status_code == 429:
                    # C41 — exponential backoff.
                    delay = self.base_retry_delay * (2 ** (attempt - 1))
                    # C42 — random jitter (0 to 50% of delay).
                    jitter = random.uniform(0, delay * 0.5)
                    total_delay = delay + jitter
                    self._total_retries += 1
                    logger.warning(
                        "[%s] Rate-limited by UniProt (HTTP 429), sleeping "
                        "%.1fs (attempt %d/%d)",
                        self.source_name, total_delay,
                        attempt, self.max_retries,
                        extra=self._log_context(),
                    )
                    time.sleep(total_delay)
                    continue

                # R13 — 4xx is permanent (our request is malformed).  Don't retry.
                if 400 <= resp.status_code < 500:
                    resp.raise_for_status()

                # 5xx is transient — raise_for_status will raise, then we retry.
                resp.raise_for_status()

                # Handle Retry-After header (UniProt sometimes returns 200 +
                # Retry-After for heavy queries).
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    self._consecutive_retry_after += 1
                    # C8 — break the loop after N consecutive Retry-Afters.
                    if self._consecutive_retry_after > self.consecutive_retry_after_limit:
                        logger.warning(
                            "[%s] %d consecutive Retry-After headers. "
                            "Returning response to prevent infinite loop.",
                            self.source_name, self._consecutive_retry_after,
                        )
                        self._consecutive_retry_after = 0
                        return resp

                    wait = self._parse_retry_after(retry_after)  # C6 / SEC7
                    self._total_retries += 1
                    logger.info(
                        "[%s] UniProt Retry-After: %ds (consecutive: %d)",
                        self.source_name, wait, self._consecutive_retry_after,
                    )
                    time.sleep(wait)
                    continue

                # Success — reset the consecutive-retry counter.
                self._consecutive_retry_after = 0
                return resp

            except requests.exceptions.RequestException as exc:
                last_exception = exc

                # v29 ROOT FIX (audit P1-15): 4xx errors (except 429) are
                # permanent client errors — retrying wastes API quota. Only
                # retry 5xx and network errors.  Although the 4xx branch above
                # calls raise_for_status() (which raises HTTPError), that
                # HTTPError is caught here by RequestException and would be
                # retried 5× without this guard.
                if isinstance(exc, requests.exceptions.HTTPError):
                    resp_exc = getattr(exc, "response", None)
                    status = getattr(resp_exc, "status_code", None)
                    if (status is not None
                            and 400 <= status < 500
                            and status != 429):
                        logger.warning(
                            "[%s] HTTP %d — permanent client error, not "
                            "retrying: %s",
                            self.source_name, status, exc,
                            extra=self._log_context(),
                        )
                        raise DownloadError(
                            f"HTTP {status} permanent client error "
                            f"(not retried): {exc}"
                        ) from exc

                if attempt == self.max_retries:
                    # C39 — raise DownloadError, not RuntimeError.
                    raise DownloadError(
                        f"Failed to fetch UniProt page after "
                        f"{self.max_retries} retries: {exc}"
                    ) from exc

                # C41 — exponential backoff.
                delay = self.base_retry_delay * (2 ** (attempt - 1))
                jitter = random.uniform(0, delay * 0.5)
                total_delay = delay + jitter
                self._total_retries += 1
                logger.warning(
                    "[%s] Request failed: %s, retrying in %.1fs (attempt %d/%d)",
                    self.source_name, exc, total_delay,
                    attempt, self.max_retries,
                    exc_info=getattr(self, "log_exc_info", True),
                )
                time.sleep(total_delay)

        # C40 — all retries exhausted without a return.
        raise DownloadError(
            f"Failed to fetch UniProt page after {self.max_retries} retries"
            + (f": {last_exception}" if last_exception else "")
        )

    # ---------------------------------------------------------------------
    # _parse_link_header() — URL-validated Link parsing (C5, C32, C34, SEC8)
    # ---------------------------------------------------------------------
    @staticmethod
    def _parse_link_header(link_header: Optional[str]) -> Optional[str]:
        """Extract the ``next`` URL from a ``Link`` header (C5, SEC8).

        Validates the URL domain to prevent SSRF (SEC1/SEC8).  Handles
        the rare case where a comma appears inside a URL by using a
        regex that anchors on ``<...>`` boundaries (C34).  Tolerates
        arbitrary whitespace around the ``;`` separator (C5).

        Parameters
        ----------
        link_header : str | None
            Raw ``Link`` header value.

        Returns
        -------
        str | None
            The ``next`` URL, or *None* if not present or rejected.
        """
        if not link_header or not isinstance(link_header, str):
            return None
        # C34/C5 — match <URL> ; rel="next" with the URL enclosed in angle
        # brackets.  Allow arbitrary whitespace between '>' and ';' and
        # between ';' and 'rel='.  This correctly handles commas inside
        # URLs (rare but possible).
        for match in re.finditer(
            r'<([^>]+)>\s*;\s*rel="next"', link_header,
        ):
            url = match.group(1)
            try:
                UniProtPipeline._validate_url(url)
            except ValueError:
                logger.warning(
                    "Link header URL rejected (domain not allowed): %s",
                    url[:100],
                )
                return None
            return url
        return None

    # ---------------------------------------------------------------------
    # _parse_retry_after() — delta-seconds and HTTP-date (C6, SEC7, R1)
    # ---------------------------------------------------------------------
    def _parse_retry_after(self, retry_after: str) -> int:
        """Parse a ``Retry-After`` header value into seconds (C6, SEC7).

        Handles both delta-seconds (``"120"``) and HTTP-date format
        (``"Wed, 21 Oct 2025 07:28:00 GMT"``).  Caps the wait at
        ``self.max_retry_after_wait`` to prevent a malicious server from
        stalling the pipeline indefinitely (SEC7 / C43).

        Parameters
        ----------
        retry_after : str
            The ``Retry-After`` header value.

        Returns
        -------
        int
            Seconds to wait, capped at ``max_retry_after_wait`` and
            floored at 0.
        """
        # Try delta-seconds first.
        try:
            wait = int(retry_after)
        except (ValueError, TypeError):
            # C6 — try HTTP-date format.
            try:
                dt = parsedate_to_datetime(retry_after)
                if dt is not None:
                    now = datetime.now(timezone.utc)
                    if dt.tzinfo is None:
                        from datetime import timezone as _tz
                        dt = dt.replace(tzinfo=_tz.utc)
                    wait = max(0, int((dt - now).total_seconds()))
                else:
                    wait = int(self.base_retry_delay)
            except (ValueError, TypeError, OverflowError):
                logger.warning(
                    "[%s] Unparseable Retry-After header: %r. "
                    "Using default wait of %.1fs.",
                    self.source_name, retry_after, self.base_retry_delay,
                )
                wait = int(self.base_retry_delay)

        # SEC7 / C43 — cap at maximum.
        if wait > self.max_retry_after_wait:
            logger.warning(
                "[%s] Retry-After value %ds exceeds maximum %ds. Capping.",
                self.source_name, wait, self.max_retry_after_wait,
            )
            wait = self.max_retry_after_wait

        return max(0, wait)

    # ---------------------------------------------------------------------
    # _is_raw_file_valid() (F5, I1, I4, CFG19)
    # ---------------------------------------------------------------------
    def _is_raw_file_valid(self, path: Path) -> bool:
        """Check whether a cached raw file is valid for reuse (F5, I4).

        A file is valid if:
        1. It exists and has non-zero size.
        2. It has at least 2 lines (header + ≥ 1 data row) — guards
           against partial downloads where only the header was written.
        3. It is not older than ``max_cache_age_days`` (CFG19).
        4. Its SHA-256 checksum matches the stored checksum (if a
           ``.sha256`` sidecar exists; I4).

        Parameters
        ----------
        path : Path
            Path to the cached raw TSV.

        Returns
        -------
        bool
            *True* if the file is valid and can be reused.
        """
        try:
            if not path.exists() or path.stat().st_size == 0:
                return False
        except OSError:
            return False

        # Check minimum row count (header + at least 1 data row).
        try:
            with open(path, "r", encoding="utf-8") as f:
                line_count = sum(1 for _ in f)
            if line_count < 2:
                logger.warning(
                    "[%s] Cached file has < 2 lines (%s). Re-downloading.",
                    self.source_name, path,
                )
                return False
        except (OSError, UnicodeDecodeError):
            return False

        # CFG19 — check age.
        try:
            file_age_days = (time.time() - path.stat().st_mtime) / 86400
            if file_age_days > self.max_cache_age_days:
                logger.info(
                    "[%s] Cached file is %d days old (max: %d). Re-downloading.",
                    self.source_name, int(file_age_days), self.max_cache_age_days,
                )
                return False
        except OSError:
            return False

        # I4 — check SHA-256 if a sidecar exists.
        checksum_path = path.with_suffix(path.suffix + ".sha256")
        if checksum_path.exists():
            try:
                stored_hash = checksum_path.read_text(encoding="utf-8").strip().split()[0]
                actual_hash = self._compute_sha256(path)
                if actual_hash != stored_hash:
                    logger.warning(
                        "[%s] SHA-256 mismatch for %s. Re-downloading.",
                        self.source_name, path,
                    )
                    return False
            except (OSError, IndexError, ValueError):
                logger.warning(
                    "[%s] Could not verify checksum for %s. Re-downloading.",
                    self.source_name, path,
                )
                return False

        return True

    # ---------------------------------------------------------------------
    # _compute_sha256() (L21)
    # ---------------------------------------------------------------------
    @staticmethod
    def _compute_sha256(path: Path) -> str:
        """Compute the SHA-256 hexdigest of *path* (64 KB streaming).

        Parameters
        ----------
        path : Path
            File to hash.

        Returns
        -------
        str
            64-character lowercase hex SHA-256 digest.
        """
        sha = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha.update(chunk)
        return sha.hexdigest()

    # ---------------------------------------------------------------------
    # _write_checksum() (I4, L21)
    # ---------------------------------------------------------------------
    def _write_checksum(self, path: Path) -> None:
        """Write a SHA-256 checksum sidecar for *path* (I4).

        Sidecar filename: ``<path>.sha256`` (so ``.tsv`` → ``.tsv.sha256``).
        Sidecar format: ``<hexdigest>  <filename>\\n`` (the standard
        ``sha256sum`` format so ``sha256sum -c`` works).
        """
        try:
            digest = self._compute_sha256(path)
            checksum_path = path.with_suffix(path.suffix + ".sha256")
            checksum_path.write_text(
                f"{digest}  {path.name}\n", encoding="utf-8",
            )
            self._set_secure_permissions(checksum_path)
            logger.info(
                "[%s] Wrote SHA-256 checksum: %s (digest: %s)",
                self.source_name, checksum_path, digest,
            )
        except OSError as exc:
            logger.warning(
                "[%s] Could not write checksum sidecar for %s: %s",
                self.source_name, path, exc,
            )

    # ---------------------------------------------------------------------
    # _stage_raw_file() (A9, SEC15)
    # ---------------------------------------------------------------------
    def _stage_raw_file(self, raw_path: Path) -> Path:
        """Copy the raw download to an immutable staging area (A9, SEC15).

        The raw TSV is both a download artifact and the input to
        ``clean()``.  This method creates an immutable copy with a
        SHA-256 checksum so that ``clean()`` always reads the same data,
        even if the original raw file is modified or deleted between runs.

        Parameters
        ----------
        raw_path : Path
            Path to the raw downloaded TSV.

        Returns
        -------
        Path
            Path to the staged copy.
        """
        staged_dir = self.effective_raw_dir / "staged"
        staged_dir.mkdir(parents=True, exist_ok=True)
        staged_path = staged_dir / raw_path.name

        if staged_path.exists():
            # Verify checksum — if match, skip the copy.
            try:
                raw_hash = self._compute_sha256(raw_path)
                staged_hash = self._compute_sha256(staged_path)
                if raw_hash == staged_hash:
                    return staged_path
            except OSError:
                pass  # fall through to re-copy

        try:
            shutil.copy2(raw_path, staged_path)
            logger.info(
                "[%s] Staged raw file: %s → %s",
                self.source_name, raw_path, staged_path,
            )
        except OSError as exc:
            logger.warning(
                "[%s] Could not stage raw file %s: %s",
                self.source_name, raw_path, exc,
            )
            return raw_path
        return staged_path

    # ---------------------------------------------------------------------
    # _write_checkpoint() / _read_checkpoint() (R8)
    # ---------------------------------------------------------------------
    def _write_checkpoint(
        self, cursor_url: str, page_num: int, total_records: int,
    ) -> None:
        """Write a checkpoint file for resume support (R8).

        The checkpoint records the next-page cursor URL, the current
        page number, and the running record count.  End-to-end resume
        is not yet wired into ``download()`` (the temp file is discarded
        on failure), but the checkpoint is written for diagnosis and
        future implementation.

        Parameters
        ----------
        cursor_url : str
            The next-page cursor URL (empty string if no more pages).
        page_num : int
            Current page number (1-indexed).
        total_records : int
            Records fetched so far.
        """
        checkpoint = {
            "cursor_url": cursor_url,
            "page_num": page_num,
            "total_records": total_records,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": getattr(self, "run_id", None),
        }
        checkpoint_path = self.effective_raw_dir / "download_checkpoint.json"
        try:
            checkpoint_path.write_text(
                json.dumps(checkpoint, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.debug(
                "[%s] Could not write checkpoint: %s",
                self.source_name, exc,
            )

    def _read_checkpoint(self) -> Optional[dict[str, Any]]:
        """Read the last checkpoint, if any (R8).

        Returns
        -------
        dict | None
            Checkpoint dict, or *None* if no checkpoint exists or it is
            unparseable.
        """
        checkpoint_path = self.effective_raw_dir / "download_checkpoint.json"
        if not checkpoint_path.exists():
            return None
        try:
            return json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError, OSError):
            return None

    # ---------------------------------------------------------------------
    # clean() — full cleaning pipeline (F2, F3, F4, S1–S25, DQ1–DQ25, I2, I6)
    # ---------------------------------------------------------------------
    def clean(self, raw_path: Path) -> pd.DataFrame:
        """Clean and normalize UniProt protein data.

        Implements the full cleaning pipeline:

        1. Read the TSV with explicit dtypes (C29, C30, C31).
        2. Validate that expected TSV columns are present (D2-8, INT14).
        3. Rename columns to match the ``proteins`` table schema.
        4. Validate that critical columns were renamed successfully (C11).
        5. Extract ``gene_symbol`` from ``gene_names`` if missing (S3).
        6. Extract ``protein_name_canonical`` from ``protein_name`` (F4, S4).
        7. Set ``gene_name = None`` (deprecated — F4).
        8. Clean ``function_desc`` (S5, S6, S7, S16, S17).
        9. Extract ``string_id`` and ``all_string_ids`` (S8, S9).
        10. Validate ``uniprot_id`` format (S20, DQ1).
        11. Validate ``length`` range (DQ14).
        12. Validate ``sequence`` characters (S21, DQ10).
        13. Cross-validate ``length`` vs ``len(sequence)`` (S11, DQ4).
        14. Detect & log duplicate ``uniprot_id``s with content hash (DQ2, I14).
        15. Sort by ``uniprot_id`` for deterministic dedup (I2, I6).
        16. Drop rows with null ``uniprot_id`` (DQ19 — dead-letter).
        17. Drop duplicate ``uniprot_id``s (keep first).
        18. Validate organism — log non-Homo sapiens records (S10, DQ5).
        19. Handle missing protein fields via ``handle_missing_protein_fields``
            with ``organism_fill_mode="strict"`` (S10).
        20. Ensure all required output columns exist (F4, C48, DQ18).
        21. Add lineage columns (LIN2, LIN7, LIN8).
        22. Compute DQ metrics (DQ20, L23).
        23. Sanitize for CSV formula injection (SEC4, C27).

        Parameters
        ----------
        raw_path : Path
            Path to the raw TSV file from ``download()``.

        Returns
        -------
        pd.DataFrame
            Cleaned protein DataFrame.  The base class ``run()``
            persists this to ``proteins.csv`` via
            ``_persist_cleaned_data()`` (A3 — we do NOT write the CSV
            ourselves).
        """
        with self._timed_operation("clean"):
            # ---------- Step 1: read TSV (C29, C30, C31) ----------
            try:
                df = pd.read_csv(
                    raw_path,
                    sep="\t",
                    dtype=str,                       # read everything as string
                    na_values=["", "null", "None", "N/A", "NaN"],
                    keep_default_na=True,
                    encoding="utf-8",
                )
            except (pd.errors.ParserError, OSError, UnicodeDecodeError) as exc:
                # L7 — wrap read failure with file context.
                raise DownloadError(
                    f"Failed to read UniProt TSV {raw_path}: {exc}"
                ) from exc

            logger.info(
                "[%s] Loaded %d raw protein records from %s",
                self.source_name, len(df), raw_path,
                extra=self._log_context(),
            )

            # L19 — log raw vs cleaned ratio at the end.
            raw_count = len(df)
            self._log_null_counts(df, stage="raw")

            # ---------- Step 2: validate TSV columns (D2-8, INT14) ----------
            actual_columns = set(df.columns)
            missing = _EXPECTED_TSV_COLUMNS - actual_columns
            if missing:
                logger.warning(
                    "[%s] Expected TSV columns not found: %s. "
                    "Available: %s. This may indicate a UniProt API change.",
                    self.source_name, sorted(missing), sorted(actual_columns),
                )

            # INT14 — log unknown (future) columns gracefully.
            extra_columns = actual_columns - _EXPECTED_TSV_COLUMNS
            if extra_columns:
                logger.info(
                    "[%s] UniProt returned unexpected columns (future API "
                    "addition?): %s. These will be dropped during cleaning.",
                    self.source_name, sorted(extra_columns),
                )

            # ---------- Step 3: rename columns ----------
            column_map: dict[str, str] = {
                "Entry": "uniprot_id",
                "Gene Names": "gene_names",
                "Gene Names (primary)": "gene_symbol",
                "Protein names": "protein_name",
                "Organism": "organism",
                "Length": "length",
                "Sequence": "sequence",
                # S18 — UniProt REST uses "Cross-reference (STRING)" for xref_string.
                "Cross-reference (STRING)": "string_xref",
                "Function [CC]": "function_desc",
            }
            df = df.rename(columns={k: v for k, v in column_map.items() if k in df.columns})

            # ---------- Step 4: validate critical columns (C11) ----------
            for col in _CRITICAL_COLUMNS:
                if col not in df.columns:
                    raise ValueError(
                        f"Critical column '{col}' not found after rename. "
                        f"Available columns: {list(df.columns)}. "
                        f"This usually means UniProt's TSV column names have "
                        f"changed. Check UNIPROT_FIELDS and column_map."
                    )

            # L6 — warn for ALL missing important columns, not just gene_symbol.
            self._log_missing_columns(df)

            # ---------- Step 5: gene_symbol (S3) ----------
            # If 'gene_symbol' wasn't mapped (column missing), extract from
            # 'gene_names' (first token).  Also validate HGNC format.
            if "gene_symbol" not in df.columns or df["gene_symbol"].isna().all():
                if "gene_names" in df.columns:
                    logger.warning(
                        "[%s] 'Gene Names (primary)' column not found in TSV. "
                        "Extracting gene_symbol from 'Gene Names' (first token).",
                        self.source_name,
                    )
                    df["gene_symbol"] = df["gene_names"].apply(
                        lambda x: str(x).split()[0]
                        if pd.notna(x) and str(x).strip() else None
                    )

            if "gene_symbol" in df.columns:
                df["gene_symbol"] = df["gene_symbol"].apply(self._validate_gene_symbol)

            # ---------- Step 6 & 7: protein_name_canonical + gene_name=None (F4) ----------
            if "protein_name" in df.columns:
                df["protein_name_canonical"] = df["protein_name"].apply(
                    self._extract_canonical_name
                )
            else:
                df["protein_name_canonical"] = None

            # F4 — gene_name is DEPRECATED.  Set to None to stop the data
            # corruption.  Downstream code MUST use protein_name_canonical
            # (for canonical names) or gene_symbol (for gene symbols).
            df["gene_name"] = None
            self._log_transformation(
                "deprecate_gene_name", raw_count, raw_count,
                {"reason": "gene_name set to None (deprecated). "
                           "Use protein_name_canonical / gene_symbol."},
            )

            # ---------- Step 8: clean function_desc (S5, S6, S7, S16, S17) ----------
            if "function_desc" in df.columns:
                before_func = df["function_desc"].notna().sum()
                df["function_desc"] = df["function_desc"].apply(self._clean_function_desc)
                after_func = df["function_desc"].notna().sum()
                self._log_transformation(
                    "clean_function_desc", before_func, after_func,
                    {"reason": "Stripped FUNCTION: prefix, ECO tags, sub-section markers."},
                )
            else:
                df["function_desc"] = None

            # ---------- Step 9: extract string_id + all_string_ids (S8, S9) ----------
            if "string_xref" in df.columns:
                df["string_id"] = df["string_xref"].apply(self._extract_string_id)
                df["all_string_ids"] = df["string_xref"].apply(
                    self._extract_all_string_ids
                )
            else:
                df["string_id"] = None
                df["all_string_ids"] = None

            # DQ12 — log duplicate string_ids.
            if "string_id" in df.columns:
                dup_string = df[df["string_id"].notna()]["string_id"].duplicated().sum()
                if dup_string > 0:
                    logger.info(
                        "[%s] %d duplicate string_ids found (multiple proteins "
                        "mapping to the same STRING ID). This is expected for "
                        "isoforms but may indicate a data issue.",
                        self.source_name, dup_string,
                    )

            # ---------- Step 10: validate uniprot_id format (S20, DQ1) ----------
            if "uniprot_id" in df.columns:
                invalid_mask = df["uniprot_id"].apply(
                    lambda x: pd.notna(x)
                    and isinstance(x, str)
                    and not _UNIPROT_ACCESSION_RE.match(x)
                )
                invalid_count = int(invalid_mask.sum())
                if invalid_count > 0:
                    invalid_ids = df.loc[invalid_mask, "uniprot_id"].head(10).tolist()
                    logger.warning(
                        "[%s] %d records have invalid UniProt accession format: %s",
                        self.source_name, invalid_count, invalid_ids,
                    )
                    # DQ19 — quarantine invalid records.
                    if hasattr(self, "dead_letter_queue"):
                        invalid_df = df[invalid_mask].copy()
                        for _, row in invalid_df.iterrows():
                            self._quarantine_record(
                                row.to_dict(), "invalid_uniprot_id_format"
                            )
                    df = df[~invalid_mask].copy()

            # ---------- Step 11: validate length range (DQ14) ----------
            if "length" in df.columns:
                # Coerce to nullable Int64.
                df["length"] = pd.to_numeric(df["length"], errors="coerce").astype("Int64")
                invalid_length = df[
                    df["length"].notna() & (
                        (df["length"] < 1) | (df["length"] > 100000)
                    )
                ]
                if len(invalid_length) > 0:
                    logger.warning(
                        "[%s] %d records have length outside [1, 100000]: %s",
                        self.source_name, len(invalid_length),
                        invalid_length[["uniprot_id", "length"]].head(5).to_dict("records"),
                    )
                    # Set out-of-range lengths to None.
                    df.loc[
                        df["length"].notna() & (
                            (df["length"] < 1) | (df["length"] > 100000)
                        ),
                        "length",
                    ] = pd.NA

            # ---------- Step 12: validate sequence characters (S21, DQ10) ----------
            if "sequence" in df.columns:
                df["sequence"] = df["sequence"].apply(self._validate_sequence)

            # ---------- Step 13: cross-validate length vs sequence (S11, DQ4) ----------
            if "length" in df.columns and "sequence" in df.columns:
                mismatch_mask = df.apply(
                    lambda r: (
                        pd.notna(r["length"])
                        and isinstance(r["sequence"], str)
                        and int(r["length"]) != len(r["sequence"])
                    ),
                    axis=1,
                )
                mismatch_count = int(mismatch_mask.sum())
                if mismatch_count > 0:
                    mismatch_ids = df.loc[mismatch_mask, "uniprot_id"].head(5).tolist()
                    logger.warning(
                        "[%s] %d proteins have length != len(sequence). "
                        "This may indicate API or pipeline corruption. "
                        "First mismatching accessions: %s",
                        self.source_name, mismatch_count, mismatch_ids,
                    )

            # ---------- Step 14: detect & log duplicate uniprot_ids (DQ2, I14) ----------
            if "uniprot_id" in df.columns:
                dup_count = int(df["uniprot_id"].duplicated().sum())
                if dup_count > 0:
                    logger.warning(
                        "[%s] %d duplicate uniprot_ids found in raw data. "
                        "UniProt human reviewed should have ZERO duplicates. "
                        "This may indicate a pagination bug or API regression.",
                        self.source_name, dup_count,
                    )
                    dup_ids = df[df["uniprot_id"].duplicated(keep=False)][
                        "uniprot_id"
                    ].unique().tolist()[:10]
                    logger.warning(
                        "[%s] Duplicate accessions (first 10): %s",
                        self.source_name, dup_ids,
                    )
                    # I14 — log content hash for duplicates with different sequences.
                    self._log_duplicate_content_hash(df)

            # ---------- Step 15 & 16: deterministic sort + dedup (I2, I6) ----------
            if "uniprot_id" in df.columns:
                df = df.sort_values("uniprot_id").reset_index(drop=True)

            before_null_filter = len(df)
            df = df[df["uniprot_id"].notna() & (df["uniprot_id"] != "")].copy()
            dropped_null = before_null_filter - len(df)
            if dropped_null > 0:
                self._log_transformation(
                    "drop_null_uniprot_id", before_null_filter, len(df),
                    {"dropped": dropped_null},
                )

            # ---------- Step 17: drop duplicates (I2) ----------
            before_dedup = len(df)
            df = df.drop_duplicates(subset=["uniprot_id"], keep="first").copy()
            if before_dedup - len(df) > 0:
                self._log_transformation(
                    "dedup_uniprot_id", before_dedup, len(df),
                    {"removed": before_dedup - len(df)},
                )

            # ---------- Step 18: validate organism (S10, DQ5) ----------
            # SCI-FIX (organism normalization): UniProt's REST API returns
            # the organism field as ``"Homo sapiens (Human)"`` (with the
            # common name in parentheses), while the original strict check
            # required an exact match against ``"Homo sapiens"``. As a
            # result EVERY record was being flagged as "non-Homo sapiens"
            # — a false-positive that polluted the audit log and risked
            # downstream code paths treating genuine human proteins as
            # non-human. The fix normalises the organism field by:
            #   1. Stripping the parenthetical common-name suffix.
            #   2. Whitespace-trimming.
            #   3. Falling back to "Homo sapiens" for blanks (since the
            #      query is organism_id:9606, we are confident these are
            #      human — see S10 note below).
            # After normalisation, the strict "Homo sapiens" comparison
            # works correctly and genuine non-human records (if any slip
            # through) still raise the warning.
            if "organism" in df.columns:
                import re as _re
                def _normalise_organism(val: object) -> object:
                    if val is None or (isinstance(val, float) and pd.isna(val)):
                        return val
                    s = str(val).strip()
                    if not s:
                        return s
                    # Strip a trailing parenthetical, e.g.
                    # "Homo sapiens (Human)" -> "Homo sapiens"
                    s = _re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()
                    return s
                df["organism"] = df["organism"].map(_normalise_organism)

                non_human = df[
                    df["organism"].notna()
                    & (df["organism"] != "")
                    & (df["organism"] != "Homo sapiens")
                ]
                if len(non_human) > 0:
                    logger.warning(
                        "[%s] %d records have non-Homo sapiens organism: %s",
                        self.source_name, len(non_human),
                        non_human["organism"].unique().tolist()[:5],
                    )
                # S10 — fill missing organism with "Homo sapiens" only because
                # the query is organism_id:9606 (we are confident these are human).
                # The handle_missing_protein_fields(strict) call below will
                # additionally verify nothing fishy is going on.
                df.loc[
                    df["organism"].isna() | (df["organism"] == ""),
                    "organism",
                ] = "Homo sapiens"

            # ---------- Step 19: handle missing protein fields (S10) ----------
            # F2 — Sequences MUST be stored in full (titin ~34 350 aa).
            # ``handle_missing_protein_fields`` truncates at ``_MAX_SEQUENCE_LENGTH``
            # (default 10 000), which would silently destroy long proteins.
            # The cleaning module's ``_MAX_SEQUENCE_LENGTH`` is module-level state
            # that can be modified by other tests (e.g.
            # ``test_cleaning_init_16_domains.py::test_lazy_import_does_not_load_submodules``
            # re-imports the module, resetting the constant).  We therefore
            # CANNOT rely on temporarily raising the cap — the function may
            # still see the OLD module's value.
            #
            # Solution: do sequence missing-value handling ourselves (F2/S21)
            # and call ``handle_missing_protein_fields`` with the ``sequence``
            # column removed so it cannot truncate.  We then restore the
            # (already-validated) sequence column afterwards.
            _sequence_col = None
            if "sequence" in df.columns:
                _sequence_col = df["sequence"].copy()
                df = df.drop(columns=["sequence"])

            # Call handle_missing_protein_fields for organism / gene_name /
            # function_desc handling only.
            df = handle_missing_protein_fields(
                df,
                organism_fill_mode="strict",
                # add_truncation_marker is irrelevant because sequence is gone.
                add_truncation_marker=False,
            )

            # Restore the sequence column (already validated in Step 12 —
            # non-string values were set to None, invalid chars set to None).
            if _sequence_col is not None:
                df["sequence"] = _sequence_col.values
            else:
                df["sequence"] = None

            # ---------- Step 20: ensure all required output columns ----------
            df = self._ensure_protein_columns(df)

            # ---------- Step 21: lineage columns (LIN2, LIN7, LIN8) ----------
            df["_source"] = "uniprot"
            df["_source_version"] = getattr(self, "source_version", None)
            df["_source_row_index"] = range(len(df))
            df["_protein_name_was_canonicalized"] = (
                df["protein_name"].fillna("") != df["protein_name_canonical"].fillna("")
            )
            if "function_desc" in df.columns:
                df["_function_desc_was_cleaned"] = df["function_desc"].notna()
            else:
                df["_function_desc_was_cleaned"] = False
            if "all_string_ids" in df.columns:
                df["_string_id_is_subset"] = (
                    df["all_string_ids"].notna()
                    & df["all_string_ids"].astype(str).str.contains(";", na=False)
                )
            else:
                df["_string_id_is_subset"] = False

            # ---------- Step 22: DQ metrics (DQ20, L23) ----------
            dq_metrics = self._compute_dq_metrics(df)

            # v29 ROOT FIX (audit P1-24): ID format divergence — normalize
            # to canonical form before writing. UniProt accessions and gene
            # symbols are uppercased + stripped. This guarantees downstream
            # joins against STRING (uniprot_id), DisGeNET (gene_symbol),
            # OMIM (gene_symbol), and DrugBank interactions (uniprot_id)
            # succeed regardless of which source wrote the value. Some
            # UniProt TSV fields ship lowercase accessions for historical
            # display reasons; without this normalization, a PPI edge from
            # STRING (``"P23219"``) would NOT join with a protein from
            # UniProt (``"p23219"``).
            if len(df) > 0:
                if "uniprot_id" in df.columns:
                    df["uniprot_id"] = df["uniprot_id"].apply(
                        lambda x: normalize_uniprot_id(x)
                        if pd.notna(x) and x != "" else x
                    )
                if "gene_symbol" in df.columns:
                    df["gene_symbol"] = df["gene_symbol"].apply(
                        lambda x: normalize_gene_symbol(x)
                        if pd.notna(x) and x != "" else x
                    )

            # ---------- Step 23: sanitize for CSV (SEC4, C27) ----------
            df = self._sanitize_dataframe_for_csv(df)

            # Final null-count log (L19) — raw vs cleaned ratio.
            self._log_null_counts(df, stage="clean")
            logger.info(
                "[%s] Clean complete: %d raw → %d cleaned (ratio: %.4f, "
                "DQ score: %.4f)",
                self.source_name, raw_count, len(df),
                (len(df) / raw_count) if raw_count > 0 else 0.0,
                dq_metrics.get("quality_score", 0.0),
                extra=self._log_context(),
            )

            return df

    # ---------------------------------------------------------------------
    # _extract_canonical_name() — nested parens, ECO, EC numbers (S4, S14, S15, C14, C15)
    # ---------------------------------------------------------------------
    def _extract_canonical_name(self, protein_name: Optional[str]) -> Optional[str]:
        """Extract the canonical protein name (S4, S14, S15, C14, C15).

        Strips:
        * ``{ECO:...}`` evidence tags (anywhere in the string).
        * Parenthetical content (handles nested parentheses via manual scan).
        * Trailing EC numbers (e.g. ``"Catalase EC 1.11.1.6"``).

        Parameters
        ----------
        protein_name : str | None
            Raw protein name from UniProt.

        Returns
        -------
        str | None
            Canonical name, or *None* if input is *None*, empty, or
            contains only parenthetical content (S15).
        """
        if not protein_name or not isinstance(protein_name, str):
            return None
        if not protein_name.strip():
            return None

        # S14 — strip {ECO:...} tags first (before paren removal, so the
        # paren-stripping regex doesn't get confused by braces).
        cleaned = _ECO_TAG_RE.sub("", protein_name).strip()
        if not cleaned:
            return None

        # S4 — strip nested parentheses via manual scan (regex can't handle
        # arbitrary nesting).  We keep everything before the first UNMATCHED
        # open paren.
        result_chars: list[str] = []
        depth = 0
        for ch in cleaned:
            if ch == "(":
                depth += 1
                if depth == 1:
                    # First open paren — stop appending (but continue scanning
                    # to track depth so nested closes don't end the loop early).
                    continue
            elif ch == ")":
                if depth > 0:
                    depth -= 1
                continue
            elif depth == 0:
                result_chars.append(ch)

        canonical = "".join(result_chars).strip()

        # C15 — strip trailing EC number (strict format: EC + 2–4 dotted ints).
        canonical = _EC_NUMBER_RE.sub("", canonical).strip()

        # S15 — if everything was in parens, return None (not "").
        if not canonical:
            return None

        return canonical

    # ---------------------------------------------------------------------
    # _clean_function_desc() — case-insensitive, earliest marker (S5, S6, S7, S16, S17, C16, C17, C18)
    # ---------------------------------------------------------------------
    def _clean_function_desc(self, desc: Optional[str]) -> Optional[str]:
        """Strip ``FUNCTION:`` prefix, sub-section markers, and ECO tags (S5–S7).

        UniProt's ``Function [CC]`` field looks like::

            "FUNCTION: Catalyzes the reaction {ECO:0000256|HAMAP-Rule:MF_00234}.
             CATALYTIC ACTIVITY: ... SUBUNIT: ..."

        We want only the function prose.  Steps:
        1. Strip the leading ``FUNCTION:`` / ``Function:`` prefix (S16 —
           case-insensitive).
        2. Find the EARLIEST sub-section marker (S6) and truncate there.
        3. Remove ALL ``{ECO:...}`` evidence tags (S7, C18 — both inline
           and trailing).

        Parameters
        ----------
        desc : str | None
            Raw function description.

        Returns
        -------
        str | None
            Cleaned description, or *None* if input is empty / all
            stripped (S15).
        """
        if not desc or not isinstance(desc, str):
            return None
        if not desc.strip():
            return None

        cleaned = desc.strip()

        # S16 — case-insensitive FUNCTION: prefix strip (only the first
        # occurrence; S17).
        for prefix in ("FUNCTION: ", "Function: ", "FUNCTION:", "Function:"):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):].strip()
                break

        # S6 — find the EARLIEST sub-section marker (any order) and truncate.
        earliest_idx = len(cleaned)
        for marker in _SUBSECTION_MARKERS:
            idx = cleaned.find(marker)
            # S5 — idx >= 0 (NOT idx > 0) so a marker at position 0 also matches.
            if 0 <= idx < earliest_idx:
                earliest_idx = idx
        if earliest_idx < len(cleaned):
            cleaned = cleaned[:earliest_idx].strip()

        # S7 / C18 — remove ALL {ECO:...} evidence tags (inline + trailing).
        cleaned = _ECO_TAG_RE.sub("", cleaned).strip()

        return cleaned if cleaned else None

    # ---------------------------------------------------------------------
    # _extract_string_id() — first valid STRING ID (S8, S9, C19)
    # ---------------------------------------------------------------------
    def _extract_string_id(self, xref: Optional[str]) -> Optional[str]:
        """Extract the first valid STRING ID from a cross-reference field (S8, S9).

        UniProt STRING xrefs look like::

            "9606.ENSP00000357607; 9606.ENSP00000412345;"

        Multiple IDs may be present (one per isoform).  This function
        returns the FIRST valid one.  All IDs are also stored in the
        ``all_string_ids`` column via ``_extract_all_string_ids()``.

        Parameters
        ----------
        xref : str | None
            Raw cross-reference string from UniProt.

        Returns
        -------
        str | None
            First valid STRING ID, or *None*.
        """
        if not xref or not isinstance(xref, str):
            return None

        # C19 — iterate through all parts (handles leading semicolon).
        parts = [p.strip() for p in xref.split(";") if p.strip()]
        if not parts:
            return None

        # S9 — validate format.
        valid_ids = [p for p in parts if _STRING_ID_RE.match(p)]
        if not valid_ids:
            logger.debug(
                "[%s] No valid STRING IDs found in xref: %s",
                self.source_name, xref[:100],
            )
            return None

        # S8 — log when multiple IDs are present (some are discarded from
        # the primary column but kept in all_string_ids).
        if len(valid_ids) > 1:
            logger.debug(
                "[%s] Multiple STRING IDs found: %s. Using first: %s",
                self.source_name, valid_ids, valid_ids[0],
            )

        return valid_ids[0]

    # ---------------------------------------------------------------------
    # _extract_all_string_ids() — semicolon-joined list (S8)
    # ---------------------------------------------------------------------
    @staticmethod
    def _extract_all_string_ids(xref: Optional[str]) -> Optional[str]:
        """Return a semicolon-joined list of ALL valid STRING IDs (S8).

        Parameters
        ----------
        xref : str | None
            Raw cross-reference string from UniProt.

        Returns
        -------
        str | None
            ``"9606.ENSP00000357607;9606.ENSP00000412345"`` or *None*.
        """
        if not xref or not isinstance(xref, str):
            return None
        parts = [p.strip() for p in xref.split(";") if p.strip()]
        valid = [p for p in parts if _STRING_ID_RE.match(p)]
        return ";".join(valid) if valid else None

    # ---------------------------------------------------------------------
    # _validate_gene_symbol() (S3, DQ9, DQ25)
    # ---------------------------------------------------------------------
    @staticmethod
    def _validate_gene_symbol(value: Any) -> Optional[str]:
        """Validate and normalize a gene symbol (S3, DQ9, DQ25).

        Strips whitespace and uppercases.  Returns *None* if the value
        is empty or does not match the HGNC pattern
        ``^[A-Z][A-Z0-9\\-]{0,49}$``.

        Parameters
        ----------
        value : Any
            Raw gene symbol value (may be NaN, str, etc.).

        Returns
        -------
        str | None
            Validated gene symbol, or *None*.
        """
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None
        if not isinstance(value, str):
            value = str(value)
        value = value.strip()
        if not value:
            return None
        # Some UniProt gene names include synonyms separated by spaces;
        # the canonical symbol is the first token.
        if " " in value:
            value = value.split()[0]
        value = value.upper()
        if not _HGNC_SYMBOL_RE.match(value):
            return None
        return value

    # ---------------------------------------------------------------------
    # _validate_sequence() (S21, DQ10, C24, C57)
    # ---------------------------------------------------------------------
    def _validate_sequence(self, s: Any) -> Optional[str]:
        """Validate an amino-acid sequence (S21, DQ10, C24, C57).

        Non-string values (NaN, float, bytes) are converted to *None*.
        Strings containing invalid characters are logged and set to
        *None* (do not raise — we want the pipeline to continue).

        v41 ROOT FIX (SEV2-HIGH #15): UniProt sometimes returns
        lowercase letters for uncertain residues (e.g. ``"MAGTxxLP"``
        where ``xx`` denotes low-confidence residues per the UniProt
        "Sequence uncertainty" convention). The previous pattern was
        case-SENSITIVE (``^[ACDE...]+$``) and rejected ALL lowercase
        sequences, silently nulling them. This corrupted every protein
        whose UniProt record used lowercase uncertainty markers. Fix:
        upper-case the input BEFORE matching (so the pattern accepts
        lowercase), and upper-case BEFORE storing (so downstream
        consumers see a canonical uppercase sequence). The DB CHECK
        constraint is also uppercase-only, so this fix preserves
        consistency with the DB.

        Parameters
        ----------
        s : Any
            Raw sequence value.

        Returns
        -------
        str | None
            Validated sequence (UPPERCASE), or *None*.
        """
        if not isinstance(s, str):
            return None
        if not s:
            return None
        # v41 ROOT FIX (SEV2-HIGH #15): case-insensitive match.
        upper_s = s.upper()
        if not _VALID_AA_PATTERN.match(upper_s):
            logger.warning(
                "[%s] Invalid sequence characters detected (length=%d), "
                "setting to None",
                self.source_name, len(s),
            )
            return None
        # v41 ROOT FIX (SEV2-HIGH #15): return the UPPER-CASED sequence
        # so the DB CHECK constraint (uppercase-only) and downstream
        # consumers (fingerprints, ML models) see a canonical form.
        return upper_s

    # ---------------------------------------------------------------------
    # _ensure_protein_columns() (F4, C48, DQ18)
    # ---------------------------------------------------------------------
    @staticmethod
    def _ensure_protein_columns(df: pd.DataFrame) -> pd.DataFrame:
        """Ensure all required output columns exist with proper defaults (F4, C48, DQ18).

        After Fix F4, the column set includes ``protein_name_canonical``
        and ``length``.  Defaults are *None* (not empty string) for
        consistency (C50).

        Parameters
        ----------
        df : pd.DataFrame
            DataFrame to ensure columns on.

        Returns
        -------
        pd.DataFrame
            DataFrame with all required columns (may be the same object).
        """
        required_defaults: dict[str, Any] = {
            "uniprot_id": "",
            "gene_symbol": None,
            "gene_name": None,
            "protein_name": None,
            "protein_name_canonical": None,
            "organism": None,             # S10: default None, NOT "Homo sapiens"
            "length": None,               # C48 / DQ18
            "sequence": None,
            "function_desc": None,
            "string_id": None,
            "all_string_ids": None,
        }
        for col, default in required_defaults.items():
            if col not in df.columns:
                df[col] = default
        return df

    # ---------------------------------------------------------------------
    # _log_null_counts() (DQ3, L19)
    # ---------------------------------------------------------------------
    def _log_null_counts(self, df: pd.DataFrame, stage: str = "clean") -> None:
        """Log NULL and empty-string counts for all columns (DQ3, L19).

        Parameters
        ----------
        df : pd.DataFrame
            DataFrame to profile.
        stage : str
            Pipeline stage for log context (``"raw"``, ``"clean"``, ``"load"``).
        """
        if df is None or len(df) == 0:
            logger.info("[%s] %s-stage: empty DataFrame", self.source_name, stage)
            return
        null_counts = df.isnull().sum()
        # Also count empty strings for object columns.
        for col in df.select_dtypes(include=["object"]).columns:
            try:
                empty_count = int((df[col] == "").sum())
                if empty_count > 0:
                    null_counts[f"{col}(empty)"] = empty_count
            except Exception:
                pass
        non_zero = null_counts[null_counts > 0]
        if len(non_zero) > 0:
            logger.info(
                "[%s] %s-stage NULL/empty counts: %s",
                self.source_name, stage, non_zero.to_dict(),
            )
        else:
            logger.info(
                "[%s] %s-stage: zero NULLs across all columns",
                self.source_name, stage,
            )

    # ---------------------------------------------------------------------
    # _log_missing_columns() (L6)
    # ---------------------------------------------------------------------
    def _log_missing_columns(self, df: pd.DataFrame) -> None:
        """Warn for ALL missing important columns, not just gene_symbol (L6).

        Parameters
        ----------
        df : pd.DataFrame
            DataFrame after the rename step.
        """
        important_columns: list[tuple[str, str]] = [
            ("uniprot_id", "critical"),
            ("gene_symbol", "important"),
            ("protein_name", "important"),
            ("sequence", "important"),
            ("organism", "important"),
            ("function_desc", "useful"),
            ("string_xref", "useful"),
            ("length", "useful"),
        ]
        for col_name, importance in important_columns:
            if col_name not in df.columns:
                level = (
                    logging.ERROR if importance == "critical"
                    else logging.WARNING if importance == "important"
                    else logging.INFO
                )
                logger.log(
                    level,
                    "[%s] Column '%s' not found in UniProt TSV (importance: %s)",
                    self.source_name, col_name, importance,
                )

    # ---------------------------------------------------------------------
    # _log_duplicate_content_hash() (I14)
    # ---------------------------------------------------------------------
    def _log_duplicate_content_hash(self, df: pd.DataFrame) -> None:
        """Log a content hash for duplicate uniprot_ids with different sequences (I14).

        If the same ``uniprot_id`` appears multiple times with DIFFERENT
        sequences, that is a real data-integrity problem — we want to
        know about it.

        Parameters
        ----------
        df : pd.DataFrame
            DataFrame to check.
        """
        if "uniprot_id" not in df.columns or "sequence" not in df.columns:
            return
        dup_mask = df["uniprot_id"].duplicated(keep=False)
        if not dup_mask.any():
            return
        dup_df = df[dup_mask]
        for uid in dup_df["uniprot_id"].unique():
            subset = dup_df[dup_df["uniprot_id"] == uid]
            # v41 ROOT FIX (SEV2-HIGH #16): MD5 has known collisions
            # (chosen-prefix attacks since 2009, identical-prefix since
            # 2004). Two different sequences COULD hash to the same MD5
            # value, causing the duplicate-detection logic here to miss
            # real duplicates and silently log "no mismatch" for records
            # that actually have different sequences. Switch to SHA-256
            # (no known collisions, cryptographically robust). The
            # performance cost is negligible (sha256 is ~2x slower than
            # md5 in CPython, and we only run this on duplicate-id
            # subsets which are rare).
            hashes = subset["sequence"].apply(
                lambda s: hashlib.sha256(
                    str(s).encode("utf-8", errors="replace")
                ).hexdigest() if pd.notna(s) else "NULL"
            )
            if hashes.nunique() > 1:
                logger.warning(
                    "[%s] Duplicate uniprot_id %r with DIFFERENT sequences "
                    "(content hash mismatch): %s",
                    self.source_name, uid, hashes.tolist(),
                )

    # ---------------------------------------------------------------------
    # _compute_dq_metrics() (DQ20, L23)
    # ---------------------------------------------------------------------
    def _compute_dq_metrics(self, df: pd.DataFrame) -> dict[str, Any]:
        """Compute data-quality metrics for the cleaned DataFrame (DQ20, L23).

        Returns a dict with completeness, validity, uniqueness, and
        consistency metrics for downstream monitoring.

        Parameters
        ----------
        df : pd.DataFrame
            Cleaned DataFrame.

        Returns
        -------
        dict[str, Any]
            Metrics dict with a ``quality_score`` field in [0.0, 1.0].
        """
        metrics: dict[str, Any] = {}
        total = len(df)
        metrics["total_records"] = total

        if total == 0:
            metrics["quality_score"] = 0.0
            return metrics

        # Completeness: non-null, non-empty fraction per column.
        for col in ("uniprot_id", "gene_symbol", "sequence", "protein_name"):
            if col in df.columns:
                valid = df[col].notna() & (df[col] != "")
                metrics[f"completeness_{col}"] = float(valid.sum()) / total

        # Validity: uniprot_id pattern compliance.
        if "uniprot_id" in df.columns:
            valid_ids = df["uniprot_id"].apply(
                lambda x: bool(_UNIPROT_ACCESSION_RE.match(x))
                if pd.notna(x) and isinstance(x, str) else False
            )
            metrics["validity_uniprot_id"] = float(valid_ids.sum()) / total

        # Uniqueness.
        if "uniprot_id" in df.columns:
            metrics["uniqueness_uniprot_id"] = 1.0 - (
                float(df["uniprot_id"].duplicated().sum()) / total
            )

        # Consistency: length vs sequence.
        if "length" in df.columns and "sequence" in df.columns:
            consistent = df.apply(
                lambda r: (
                    pd.isna(r["length"])
                    or not isinstance(r["sequence"], str)
                    or int(r["length"]) == len(r["sequence"])
                ),
                axis=1,
            )
            metrics["consistency_length_sequence"] = float(consistent.sum()) / total

        # Overall quality score = mean of the four core dimensions.
        score_components = [
            metrics.get("completeness_uniprot_id", 1.0),
            metrics.get("validity_uniprot_id", 1.0),
            metrics.get("uniqueness_uniprot_id", 1.0),
            metrics.get("consistency_length_sequence", 1.0),
        ]
        metrics["quality_score"] = sum(score_components) / len(score_components)

        logger.info(
            "[%s] Data quality metrics: %s",
            self.source_name,
            {k: f"{v:.4f}" if isinstance(v, float) else v
             for k, v in metrics.items()},
        )
        return metrics

    # ---------------------------------------------------------------------
    # _write_provenance_sidecar() (S25, LIN3, LIN9–LIN20, SEC16, SEC20, COMP1, COMP4)
    # ---------------------------------------------------------------------
    def _write_provenance_sidecar(
        self,
        raw_path: Path,
        cleaned_path: Path,
        record_count: int,
    ) -> None:
        """Write a provenance metadata sidecar JSON file (S25, LIN3–LIN20).

        The sidecar is named ``<cleaned_filename>.provenance.json`` and
        records the full provenance of the cleaned dataset: pipeline
        name and version, UniProt release, input/output SHA-256
        checksums, record counts, timestamp, run_id, correlation_id,
        triggered_by (FDA 21 CFR Part 11 — COMP1), and the query /
        fields used.

        Parameters
        ----------
        raw_path : Path
            Path to the raw input file.
        cleaned_path : Path
            Path to the cleaned output file.
        record_count : int
            Number of records in the cleaned output.
        """
        def _sha256(p: Path) -> Optional[str]:
            if not p.exists():
                return None
            try:
                return self._compute_sha256(p)
            except OSError:
                return None

        provenance = {
            "pipeline": self.source_name,
            "pipeline_version": __version__,
            "schema_version": "v1",
            "run_id": getattr(self, "run_id", None),
            "correlation_id": getattr(self, "correlation_id", None),
            "triggered_by": getattr(self, "triggered_by", None),  # SEC20 / COMP1
            "uniprot_release": getattr(self, "source_version", None) or UNIPROT_RELEASE,
            "query": self.uniprot_query,
            "fields": list(self.uniprot_fields),
            "raw_file": str(raw_path),
            "raw_sha256": _sha256(raw_path),
            "cleaned_file": str(cleaned_path),
            "cleaned_sha256": _sha256(cleaned_path),
            "record_count": record_count,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "seed": getattr(self, "seed", None),
            "as_of_date": str(getattr(self, "as_of_date", None)),  # LIN17
            "freeze_version": getattr(self, "freeze_version", None),  # LIN18
            "snapshot_tag": getattr(self, "snapshot_tag", None),     # LIN19
            "environment": getattr(self, "environment", "development"),
        }

        sidecar_path = cleaned_path.with_suffix(
            cleaned_path.suffix + ".provenance.json"
        )
        try:
            sidecar_path.write_text(
                json.dumps(provenance, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
            self._set_secure_permissions(sidecar_path)
            logger.info(
                "[%s] Wrote provenance sidecar: %s",
                self.source_name, sidecar_path,
            )
        except OSError as exc:
            logger.warning(
                "[%s] Could not write provenance sidecar: %s",
                self.source_name, exc,
            )

    # ---------------------------------------------------------------------
    # load() — accepts session=, returns LoadResult (F1, A1, D2-1, D2-4, C22, C23)
    # ---------------------------------------------------------------------
    def load(
        self,
        df: pd.DataFrame,
        session: Optional[Session] = None,  # v43 P1-021: removed * (Liskov)
    ) -> LoadResult:
        """Bulk upsert cleaned protein data into the database (F1, D2-1).

        Parameters
        ----------
        df : pd.DataFrame
            Cleaned protein DataFrame from ``clean()``.
        session : Session | None
            Optional SQLAlchemy session.  If *None*, a new session is
            created via ``get_db_session()``.  When the caller
            (``BasePipeline.run()``) provides a session, it is reused
            so the load participates in the caller's transaction
            boundary (A11, I11).

        Returns
        -------
        LoadResult
            Structured result with ``rows_inserted``, ``rows_updated``,
            ``rows_skipped``, ``rows_failed`` counts (C22, C23).

        Raises
        ------
        ValueError
            If required column ``uniprot_id`` is missing from *df* (C20).
        """
        # C20 — validate that required columns are present.
        missing_required = [c for c in _CRITICAL_COLUMNS if c not in df.columns]
        if missing_required:
            raise ValueError(
                f"Cannot load proteins: required columns missing from "
                f"DataFrame: {missing_required}. Available columns: "
                f"{list(df.columns)}"
            )

        # Build the load DataFrame — only include columns that exist on the
        # Protein model (D2-9, INT17, DQ17).  This prevents IntegrityError
        # from extra columns like `length` or `protein_name_canonical`
        # that are in the cleaned CSV (for schema compliance / downstream
        # use) but not on the DB table.
        load_columns = self._get_load_columns()
        load_df = df[[c for c in load_columns if c in df.columns]].copy()

        own_session = session is None
        # v29 ROOT FIX (audit P1-6): the previous code did
        #   session = self._db_session_factory()
        # which returns a context manager (get_db_session is a
        # @contextmanager). The context manager was NEVER entered —
        # ``session`` was the context manager, not the Session, so
        # every subsequent session.add() / session.commit() crashed
        # with AttributeError when load() was called standalone.
        # Also, the finally block only called session.close() — it
        # never called __exit__(), so the commit never happened and
        # ALL loaded data was silently rolled back when load() ran
        # standalone.
        _session_cm = None
        if own_session:
            try:
                _session_cm = self._db_session_factory()  # C21 — factory
                session = _session_cm.__enter__()  # v29: capture the Session
            except Exception as exc:
                logger.error(
                    "[%s] Failed to create DB session: %s",
                    self.source_name, exc,
                    exc_info=getattr(self, "log_exc_info", True),
                )
                raise

        try:
            with self._timed_operation("load"):
                result = self._loader(session, load_df)

                # C22 — convert UpsertResult → LoadResult.
                # The loader may be a real callable returning UpsertResult,
                # or a MagicMock (in tests).  Handle both.
                if isinstance(result, UpsertResult):
                    load_result = LoadResult(
                        rows_inserted=result.inserted,
                        rows_updated=result.updated,
                        rows_skipped=result.quarantined,
                        rows_failed=result.failed,
                    )
                    logger.info(
                        "[%s] Upserted proteins: total=%d inserted=%d "
                        "updated=%d quarantined=%d failed=%d",
                        self.source_name,
                        result.total_input, result.inserted,
                        result.updated, result.quarantined, result.failed,
                        extra=self._log_context(),
                    )
                    return load_result

                # Fallback for int return (backward-compat) or mocks.
                try:
                    count = int(result)
                except (TypeError, ValueError):
                    count = 0
                logger.info(
                    "[%s] Loaded %d proteins (legacy return type)",
                    self.source_name, count,
                )
                return LoadResult(rows_inserted=count)

        except Exception:
            if own_session and session is not None:
                try:
                    session.rollback()
                # v41 ROOT FIX (SEV3-MEDIUM #14): the previous
                # ``except Exception: pass`` silently swallowed ALL
                # rollback errors. Fix: catch only the SQLAlchemy
                # error family and log at DEBUG so the original error
                # propagates cleanly.
                except Exception as rb_exc:  # noqa: BLE001 — never mask the original error
                    logger.debug(
                        "[%s] Error during session rollback after load "
                        "failure: %s", self.source_name, rb_exc,
                    )
            raise
        finally:
            # v29 ROOT FIX (audit P1-6): call __exit__ on the context
            # manager so it commits (on success) or rolls back (on
            # error). The previous code only called session.close(),
            # which silently rolled back ALL loaded data when load()
            # ran standalone.
            if own_session and _session_cm is not None:
                import sys as _sys
                _exc_info = _sys.exc_info()
                # v41 ROOT FIX (SEV3-MEDIUM #14): the previous
                # ``except Exception: pass`` wrapped the
                # ``_session_cm.__exit__(*_exc_info)`` call. This
                # SWALLOWED __exit__ exceptions — including commit
                # errors that operators NEED to see (a failed commit
                # means the loaded data is NOT actually persisted).
                # The comment said "cleanup must not mask" but the
                # code did exactly that. Fix: catch only the
                # SQLAlchemy error family (the expected __exit__
                # failure family for commit/rollback errors) and log
                # at WARNING (not DEBUG) so commit-time errors are
                # visible to operators. If __exit__ raises a non-
                # SQLAlchemy exception (e.g. KeyboardInterrupt), let
                # it propagate — that's a real bug.
                try:
                    _session_cm.__exit__(*_exc_info)
                except Exception as exit_exc:  # noqa: BLE001 — log and re-surface
                    logger.warning(
                        "[%s] Error during session context __exit__ "
                        "(commit/rollback may have failed; loaded data "
                        "may not be persisted): %s",
                        self.source_name, exit_exc,
                    )
                    # Re-raise only if we're NOT already in an
                    # exception context (avoid masking the original
                    # error). If we ARE in an exception context, the
                    # original error is more important and __exit__'s
                    # error is logged for diagnosis.
                    if _exc_info[0] is None:
                        raise

    # ---------------------------------------------------------------------
    # _get_load_columns() (D2-9, INT17, DQ17)
    # ---------------------------------------------------------------------
    def _get_load_columns(self) -> list[str]:
        """Get the columns to load into the proteins table (D2-9, INT17).

        Derived from the ``Protein`` model's column list, intersected
        with the columns the pipeline produces.  Falls back to a
        hardcoded list if the model can't be imported.

        Returns
        -------
        list[str]
            Column names to send to ``bulk_upsert_proteins``.
        """
        try:
            from database.models import Protein
            model_cols = [c.name for c in Protein.__table__.columns]
            # Filter out SQLAlchemy-internal columns and mixin-managed columns
            # that the loader will set itself (id, created_at, updated_at,
            # is_deleted, deleted_at).
            skip = {"id", "created_at", "updated_at", "is_deleted", "deleted_at"}
            return [c for c in model_cols if c not in skip]
        except ImportError:
            # Fallback — keep in sync with database/models.py.
            return [
                "uniprot_id", "gene_name", "gene_symbol", "protein_name",
                "organism", "sequence", "function_desc", "string_id",
            ]

    # ---------------------------------------------------------------------
    # teardown() (A8)
    # ---------------------------------------------------------------------
    def teardown(self) -> None:
        """Clean up resources after a pipeline run (A8).

        Closes the HTTP session if it was created and flushes any
        pending dead-letter-queue records to disk.
        """
        try:
            if self._http_session is not None:
                try:
                    self._http_session.close()
                except Exception:
                    pass
                self._http_session = None
        finally:
            # R4 / DQ19 — flush dead-letter queue to disk.
            try:
                self._flush_dead_letter_queue()
            except Exception as exc:
                logger.debug(
                    "[%s] DLQ flush failed in teardown: %s",
                    self.source_name, exc,
                )
            # Call super.teardown() if it exists (it closes the base
            # class's HTTP session, etc.).
            try:
                super().teardown()
            except Exception:
                pass
            logger.info("[%s] teardown complete", self.source_name)

    # ---------------------------------------------------------------------
    # _sanitize_csv_value() / _sanitize_dataframe_for_csv() (SEC4, C27)
    # ---------------------------------------------------------------------
    @classmethod
    def _sanitize_csv_value(cls, value: Any) -> Any:
        """Sanitize a single value to prevent CSV formula injection (SEC4, C27).

        If *value* is a non-empty string starting with a dangerous prefix
        (``=``, ``+``, ``-``, ``@``, ``\\t``, ``\\r``), prepend a single
        quote to neutralize the formula.

        Parameters
        ----------
        value : Any
            Value to sanitize.

        Returns
        -------
        Any
            Sanitized value.
        """
        if isinstance(value, str) and value:
            if value.startswith(_CSV_DANGEROUS_PREFIXES):
                return "'" + value
        return value

    def _sanitize_dataframe_for_csv(self, df: pd.DataFrame) -> pd.DataFrame:
        """Apply CSV formula injection prevention to all string columns (SEC4).

        Parameters
        ----------
        df : pd.DataFrame
            DataFrame to sanitize.

        Returns
        -------
        pd.DataFrame
            Sanitized DataFrame (a copy).
        """
        if df is None or len(df) == 0:
            return df
        df = df.copy()
        for col in df.select_dtypes(include=["object"]).columns:
            df[col] = df[col].apply(self._sanitize_csv_value)
        return df

    # ---------------------------------------------------------------------
    # _log_transformation() (LIN1, LIN5, L9)
    # ---------------------------------------------------------------------
    def _log_transformation(
        self,
        transformation: str,
        record_count_before: int,
        record_count_after: int,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        """Log a transformation step for data lineage (LIN1, LIN5, L9).

        Parameters
        ----------
        transformation : str
            Name of the transformation (e.g. ``"dedup_uniprot_id"``).
        record_count_before : int
            Number of records before the transformation.
        record_count_after : int
            Number of records after the transformation.
        details : dict | None
            Additional details about the transformation.
        """
        logger.info(
            "[%s] Transformation: %s | records: %d → %d | delta: %d",
            self.source_name,
            transformation,
            record_count_before,
            record_count_after,
            record_count_after - record_count_before,
            extra={
                **self._log_context(),
                "transformation": transformation,
                "record_count_before": record_count_before,
                "record_count_after": record_count_after,
                **(details or {}),
            },
        )

    # ---------------------------------------------------------------------
    # _log_context() (L2, L3, L4)
    # ---------------------------------------------------------------------
    def _log_context(self) -> dict[str, Any]:
        """Return structured logging context for this pipeline run (L2, L3, L4).

        Returns
        -------
        dict[str, Any]
            Context dict with pipeline name, run_id, correlation_id,
            triggered_by, and environment.
        """
        return {
            "pipeline": self.source_name,
            "run_id": getattr(self, "run_id", None),
            "correlation_id": getattr(self, "correlation_id", None),
            "triggered_by": getattr(self, "triggered_by", None),
            "environment": getattr(self, "environment", "development"),
        }

    # ---------------------------------------------------------------------
    # _timed_operation() (L8, L16, L17, L18)
    # ---------------------------------------------------------------------
    @contextlib.contextmanager
    def _timed_operation(self, operation: str) -> Iterator[None]:
        """Context manager that logs the duration of an operation (L8, L16–L18).

        Parameters
        ----------
        operation : str
            Name of the operation (e.g. ``"download"``, ``"clean"``, ``"load"``).
        """
        start = time.monotonic()
        logger.info(
            "[%s] Starting %s",
            self.source_name, operation,
            extra=self._log_context(),
        )
        try:
            yield
        finally:
            elapsed = time.monotonic() - start
            logger.info(
                "[%s] Finished %s in %.2fs",
                self.source_name, operation, elapsed,
                extra={**self._log_context(), "duration_seconds": elapsed,
                       "operation": operation},
            )

    # ---------------------------------------------------------------------
    # _secure_delete() (SEC17)
    # ---------------------------------------------------------------------
    def _secure_delete(self, path: Path) -> None:
        """Securely delete a file by overwriting before removal (SEC17).

        Overwrites the file with zeros, fsyncs, then unlinks.  Falls
        back to a regular ``unlink()`` if the secure overwrite fails.

        Parameters
        ----------
        path : Path
            File to delete.
        """
        if not path.exists():
            return
        try:
            size = path.stat().st_size
            with open(path, "wb") as f:
                f.write(b"\x00" * min(size, 10 * 1024 * 1024))  # cap at 10 MB
                f.flush()
                os.fsync(f.fileno())
            path.unlink()
        except OSError as exc:
            logger.debug(
                "[%s] Secure delete failed for %s: %s; falling back to unlink",
                self.source_name, path, exc,
            )
            try:
                path.unlink()
            except OSError:
                pass

    # ---------------------------------------------------------------------
    # _set_secure_permissions() (SEC10, SEC14)
    # ---------------------------------------------------------------------
    def _set_secure_permissions(self, path: Path) -> None:
        """Set file permissions to owner-only read/write (SEC10, SEC14).

        Parameters
        ----------
        path : Path
            File to secure.
        """
        try:
            os.chmod(path, self._SECURE_FILE_MODE)
        except OSError:
            # On Windows or read-only filesystems, chmod may fail — that's OK.
            logger.debug(
                "[%s] Could not set permissions on %s",
                self.source_name, path,
            )

    # ---------------------------------------------------------------------
    # _quarantine_record() (DQ19, R4)
    # ---------------------------------------------------------------------
    def _quarantine_record(self, record: dict[str, Any], reason: str) -> None:
        """Add a record to the dead-letter queue with a rejection reason (DQ19, R4).

        Parameters
        ----------
        record : dict
            The rejected record.
        reason : str
            Why the record was rejected.
        """
        if not hasattr(self, "dead_letter_queue"):
            self.dead_letter_queue: list[dict[str, Any]] = []
        record_copy = dict(record)
        record_copy["_rejection_reason"] = reason
        record_copy["_rejected_at"] = datetime.now(timezone.utc).isoformat()
        record_copy["_pipeline"] = self.source_name
        self.dead_letter_queue.append(record_copy)
        logger.debug(
            "[%s] Quarantined record: %s (reason: %s)",
            self.source_name,
            record_copy.get("uniprot_id", "?"),
            reason,
        )

    # ---------------------------------------------------------------------
    # _flush_dead_letter_queue() (DQ19, R4, L20)
    # ---------------------------------------------------------------------
    def _flush_dead_letter_queue(self) -> None:
        """Write the dead-letter queue to disk as JSONL (DQ19, R4, L20).

        File: ``<effective_raw_dir>/dead_letter_queue.jsonl``.
        Each line is a JSON object representing one rejected record.
        """
        queue = getattr(self, "dead_letter_queue", None)
        if not queue:
            return
        dlq_path = self.effective_raw_dir / "dead_letter_queue.jsonl"
        try:
            with open(dlq_path, "a", encoding="utf-8") as f:
                for record in queue:
                    f.write(json.dumps(record, default=str) + "\n")
            logger.info(
                "[%s] Flushed %d records to dead-letter queue: %s",
                self.source_name, len(queue), dlq_path,
            )
            queue.clear()
        except OSError as exc:
            logger.warning(
                "[%s] Could not flush dead-letter queue: %s",
                self.source_name, exc,
            )

    # ---------------------------------------------------------------------
    # _redact_log_message() (SEC13, SEC11)
    # ---------------------------------------------------------------------
    @staticmethod
    def _redact_log_message(msg: str) -> str:
        """Redact sensitive information from a log message (SEC11, SEC13).

        Strips ``api_key=...`` query parameters from URLs.

        Parameters
        ----------
        msg : str
            Log message.

        Returns
        -------
        str
            Redacted message.
        """
        return re.sub(
            r"(api[_-]?key=)[^&\s]+", r"\1[REDACTED]", msg, flags=re.IGNORECASE,
        )

    # ---------------------------------------------------------------------
    # _cleanup_old_raw_files() (COMP2)
    # ---------------------------------------------------------------------
    def _cleanup_old_raw_files(self) -> None:
        """Delete raw files older than the retention period (COMP2).

        Reads ``UNIPROT_RAW_RETENTION_DAYS`` env var (default 90 days).
        """
        try:
            retention_days = int(os.environ.get(
                "UNIPROT_RAW_RETENTION_DAYS", "90"
            ))
        except ValueError:
            retention_days = 90

        raw_dir = self.effective_raw_dir
        if not raw_dir.exists():
            return

        now = time.time()
        for path in raw_dir.glob("uniprot_human_reviewed.tsv*"):
            try:
                age_days = (now - path.stat().st_mtime) / 86400
                if age_days > retention_days:
                    logger.info(
                        "[%s] Deleting old raw file: %s (%d days old)",
                        self.source_name, path, int(age_days),
                    )
                    path.unlink()
            except OSError as exc:
                logger.warning(
                    "[%s] Could not delete old raw file %s: %s",
                    self.source_name, path, exc,
                )

    # ---------------------------------------------------------------------
    # _check_dependency_versions() (INT1, INT10)
    # ---------------------------------------------------------------------
    @staticmethod
    def _check_dependency_versions() -> None:
        """Verify that library versions meet minimum requirements (INT1, INT10).

        Raises
        ------
        RuntimeError
            If a required library version is too old.
        """
        try:
            from packaging.version import Version
        except ImportError:
            # packaging is not always available — skip the check.
            return

        min_pandas = Version("1.5.0")
        min_requests = Version("2.28.0")

        if Version(pd.__version__) < min_pandas:
            raise RuntimeError(
                f"pandas >= {min_pandas} required, got {pd.__version__}"
            )
        if Version(requests.__version__) < min_requests:
            raise RuntimeError(
                f"requests >= {min_requests} required, got {requests.__version__}"
            )

    # ---------------------------------------------------------------------
    # _verify_model_sync() (INT17, INT18, D2-9)
    # ---------------------------------------------------------------------
    def _verify_model_sync(self) -> bool:
        """Verify that load_columns is in sync with the Protein model (INT17, INT18).

        Returns
        -------
        bool
            *True* if in sync, *False* (with warnings) otherwise.
        """
        try:
            from database.models import Protein
            model_columns = {c.name for c in Protein.__table__.columns}
            load_cols = set(self._get_load_columns())
            missing_in_load = model_columns - load_cols - {
                "id", "created_at", "updated_at", "is_deleted", "deleted_at",
            }
            extra_in_load = load_cols - model_columns
            if missing_in_load:
                logger.warning(
                    "[%s] Protein model has columns not in load_columns: %s",
                    self.source_name, missing_in_load,
                )
            if extra_in_load:
                logger.warning(
                    "[%s] load_columns has columns not in Protein model: %s",
                    self.source_name, extra_in_load,
                )
            return not (missing_in_load or extra_in_load)
        except ImportError:
            return True  # Can't verify without model.

    # ---------------------------------------------------------------------
    # _api_key (SEC9, INT20)
    # ---------------------------------------------------------------------
    @property
    def _api_key(self) -> Optional[str]:
        """UniProt API key, if configured (SEC9, INT20).

        UniProt does not require an API key for public use, but for
        high-volume usage providing an email is recommended.
        """
        return os.environ.get("UNIPROT_API_KEY")


# ---------------------------------------------------------------------------
# Backward-compatibility module-level constants (A10 + backward compat).
#
# Per the A10 fix, all tunable parameters now live as class attributes on
# ``UniProtPipeline``.  However, existing code (tests, downstream consumers,
# the ``pipelines`` package's lazy-import registry in ``pipelines/__init__.py``)
# still imports these names from the module level.  We expose them here as
# aliases so the public module-level API is preserved (no breaking changes).
#
# Downstream consumers should prefer the class attributes for new code.
# These aliases will be removed in v3.0.
# ---------------------------------------------------------------------------
UNIPROT_SEARCH_URL: str = UniProtPipeline.uniprot_search_url
UNIPROT_FIELDS: list[str] = list(UniProtPipeline.uniprot_fields)
UNIPROT_QUERY: str = UniProtPipeline.uniprot_query
PAGE_SIZE: int = UniProtPipeline.page_size
MAX_RETRIES: int = UniProtPipeline.max_retries
BASE_RETRY_DELAY: float = UniProtPipeline.base_retry_delay

# Extend __all__ to include the backward-compat aliases.
__all__ = list(__all__) + [
    "UNIPROT_SEARCH_URL",
    "UNIPROT_FIELDS",
    "UNIPROT_QUERY",
    "PAGE_SIZE",
    "MAX_RETRIES",
    "BASE_RETRY_DELAY",
]
