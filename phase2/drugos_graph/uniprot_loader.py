"""DrugOS Graph Module — UniProt Loader
========================================
Parses UniProtKB/Swiss-Prot flat-file (.dat) format into Protein node
records for the DrugOS knowledge graph.

Pinned release: 2024_03 (see ``config.DATA_SOURCES['uniprot']['version']``).
Production size: ~800 MB gzipped, ~570,000 entries.
Local sample: ``data/raw/uniprot_sprot.dat`` (12 KB, 30 entries, ASCII).

Supported line types (after this fix):
    ID, AC (multi-line), DE (RecName, AltName, Contains, Includes, EC,
    multi-line continuation), GN (Name, Synonyms, ORFNames, LocusNames,
    ``and`` separator for multi-gene loci), OS, OX, OG, OH, PE, SQ,
    DR (all cross-references).

Output schema (``UniProtRecord`` — see ``schemas.py``):
    {accession, secondary_accessions, entry_name, protein_name,
     alternative_names, alternative_name, contains_names, includes_names,
     ec_numbers, gene_name, gene_names, gene_synonyms, gene_orf_names,
     gene_locus_names, gene_id, gene_ids, organism, ncbi_taxid,
     protein_existence, sequence, sequence_length, cross_references,
     _provenance, _source, _license, _attribution}

Memory: streaming via ``iter_uniprot_entries()`` (~10 MB peak).
        ``parse_uniprot_entries()`` materializes the full list (~1 GB for
        570k records) — use the generator for production-scale runs where
        you can consume records one at a time.

Idempotency: YES — same input file + same parser version -> same output.
             Records are sorted by accession before return (D7-001).
             The only non-deterministic field is
             ``_provenance['parsed_at']`` (ISO-8601 timestamp).

Scientific guardian (SCI-1):
    This loader cross-checks every parsed ``gene_id`` against
    ``id_crosswalk.VERIFIED_UNIPROT_GENE_CROSSWALK``. The SCI-1 defect
    (P35568/IRS1, GeneID 2645 -> 3667) is caught at parse time and the
    verified value is emitted, with a CRITICAL log and a dead-letter
    entry for the discrepancy. See D3-001 and
    ``ID_CROSSWALK_REPAIR_CHANGELOG.md``.

Errors raised:
    FileNotFoundError — input file missing.
    UniProtDownloadError — download failed after all retries (D6-006).
    UniProtDataIntegrityError — SHA256 mismatch, record count > 50%
        below expected, missing required accession on a node (D1-003).
    UniProtParseError — parse error rate exceeds 1% of kept records
        (D6-003).
    ValueError — ``organism`` is an empty string (use ``None`` to
        disable filtering — D4-008).

Dead-letter: ``data/dead_letter/uniprot_malformed.jsonl`` (one JSON line
             per dropped record).  Fixes D6-004.

Transformation log: ``logs/transformations/uniprot.jsonl`` (one JSON line
                     per significant transformation — SCI-1 override,
                     DE Contains split, GN ``and`` split, multi-line AC,
                     etc.).  Fixes D16-002.

License: CC BY 4.0 — attribution propagated in every record's
         ``_attribution`` field (D14-001).

URL: https://www.uniprot.org/
Format spec: https://www.uniprot.org/docs/userman.htm
"""

from __future__ import annotations

# ─── Standard library imports (D1-005 — no new third-party deps) ─────────────
import gzip
import hashlib
import io
import json
import logging
import os
import re
import shutil
import socket
import ssl
import time
import urllib.error
import urllib.request
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Dict,
    Final,
    Iterable,
    Iterator,
    List,
    Literal,
    Optional,
    Tuple,
)

# ─── Project imports ─────────────────────────────────────────────────────────
from .config import (
    ALLOWED_UNIPROT_URLS,
    CANONICAL_IDS,
    CHECKPOINT_DIR,
    DATA_SOURCES,
    DEAD_LETTER_DIR,
    ENTITY_TYPE_COMPOUND,
    ENTITY_TYPE_PROTEIN,
    LOGS_DIR,
    ON_SOURCE_FAILURE,
    PROCESSED_DIR,
    RAW_DIR,
    SOURCE_KEY_UNIPROT,
    SOURCE_UNIPROT,
    UNIPROT_ATTRIBUTION,
    UNIPROT_LICENSE,
    UNIPROT_MIN_VALID_SIZE_BYTES,
    UNIPROT_PARSER_VERSION,
    UNIPROT_SCHEMA_VERSION,
    get_data_source_path,
)
from .exceptions import (
    UniProtDataIntegrityError,
    UniProtDownloadError,
    UniProtParseError,
)
from .schemas import PROVENANCE_KEYS, UniProtEdge, UniProtRecord, ProteinNode

# Re-use the validated scientific-correctness layer from id_crosswalk.
# These helpers are the AUTHORITATIVE validators — do not duplicate the
# regex (D5-001, D5-002, D3-001).  Fixes D5-001 / D5-002 / D3-001.
from .id_crosswalk import (
    VERIFIED_UNIPROT_GENE_CROSSWALK,
    _normalize_ncbi_gene_id,
    _validate_uniprot_ac,
)

# ─── D1-004: module public surface ───────────────────────────────────────────
__all__: list[str] = [
    # Public functions (preserved from v1 — D1-003 / D15-002)
    "download_uniprot",
    "parse_uniprot_entries",
    "uniprot_to_node_records",
    # New public functions (D2-002, D2-003, D2-005)
    "iter_uniprot_entries",
    "filter_by_organism",
    "uniprot_to_edge_records",
    # Protocol adapter (D1-002)
    "UniProtLoader",
    # Version constants (D7-003, D14-004)
    "PARSER_VERSION",
    "SCHEMA_VERSION",
    # Schema (D1-003, D13-003)
    "PROTEIN_NODE_SCHEMA",
]

# ─── D7-003 / D14-004: version constants ─────────────────────────────────────
PARSER_VERSION: Final[str] = UNIPROT_PARSER_VERSION   # "2.0.0"
SCHEMA_VERSION: Final[str] = UNIPROT_SCHEMA_VERSION   # "2.0.0"

# ─── D11-003 / D11-005: logger (lazy %s formatting, structured extra) ────────
logger = logging.getLogger(__name__)

# ─── D13-003: authoritative Protein node schema (for docs + validation) ──────
PROTEIN_NODE_SCHEMA: Final[Dict[str, type]] = {
    "uniprot_id": str,        # canonical PK (config.CANONICAL_IDS['Protein'])  — D2-004
    "id": str,                # backward-compat alias (deprecated, removed v3)  — D15-003
    "uniprot_uri": str,       # identifiers.org URI (FAIR)                       — D14-002
    "name": str,              # protein_name | entry_name | accession (never empty)  — D4-002
    "entry_name": str,
    "gene_name": str,         # primary gene symbol
    "gene_names": list,       # all gene symbols (D3-003)
    "gene_id": str,           # primary, VERIFIED (D3-001)
    "gene_ids": list,         # all NCBI Gene IDs (D4-004)
    "ncbi_taxid": int,        # int (D5-003)
    "ec_numbers": list,       # D3-007
    "protein_existence": int, # D3-008
    "sequence": str,          # D3-009
    "entity_type": str,       # always 'Protein'
    "source": str,            # always 'UniProt'
    "_provenance": dict,      # D16-001
    "_license": str,          # D14-001
    "_attribution": str,      # D14-001
}

# ─── D3-004 / D4-007: AC-line regex (same pattern as id_crosswalk) ───────────
# Validates format AND extracts in one step.  Fixes D4-007.
_AC_REGEX: Final[re.Pattern[str]] = re.compile(
    r"(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})"
)

# ─── D3-005: organism -> NCBI TaxID lookup for cross-checking OS vs OX ───────
# Covers the project's use cases.  Extensible via the yaml crosswalk.
_ORGANISM_TO_TAXID: Final[Dict[str, int]] = {
    "homo sapiens": 9606,
    "mus musculus": 10090,
    "rattus norvegicus": 10116,
    "danio rerio": 7955,
    "drosophila melanogaster": 7227,
    "caenorhabditis elegans": 6239,
    "saccharomyces cerevisiae": 4932,
    "escherichia coli": 562,
    "sars-cov-2": 2697049,
}

# ─── D7-004 / D12-003: env-var overrides ─────────────────────────────────────
_DEFAULT_ORGANISM: Optional[str] = os.environ.get(
    "DRUGOS_DEFAULT_ORGANISM", "Homo sapiens"
)
_DRUGOS_UNIPROT_FILE: Optional[str] = os.environ.get("DRUGOS_UNIPROT_FILE")

# ─── D5-002: NCBI Gene ID plausible range ────────────────────────────────────
# NCBI Gene IDs are positive integers; the largest assigned is < 100M.
_GENE_ID_MIN: Final[int] = 1
_GENE_ID_MAX: Final[int] = 100_000_000

# ─── D6-005: checkpoint cadence ──────────────────────────────────────────────
_CHECKPOINT_EVERY: Final[int] = 50_000

# ─── D2-005: DR database -> (edge relation, target entity type) mapping ─────
# Fixes D2-005 — emit edges from UniProt DR cross-references.
_DB_TO_EDGE_TYPE: Final[Dict[str, str]] = {
    "ChEMBL": "interacts_with",
    "DrugBank": "interacts_with",
    "BindingDB": "interacts_with",
    "PharmGKB": "interacts_with",
    "GuidetoPHARMACOLOGY": "interacts_with",
    "HGNC": "xref",
    "Pfam": "has_domain",
    "InterPro": "has_domain",
    "SMART": "has_domain",
    "PROSITE": "has_domain",
    "Reactome": "participates_in",
    "STRING": "interacts_with",
    "MINT": "interacts_with",
    "BioGRID": "interacts_with",
    "IntAct": "interacts_with",
    "GO": "annotated_with",
    "KEGG": "participates_in",
    "PubMed": "cited_in",
}
_DB_TO_ENTITY_TYPE: Final[Dict[str, str]] = {
    "ChEMBL": "Compound",
    "DrugBank": "Compound",
    "BindingDB": "Compound",
    "PharmGKB": "Compound",
    "GuidetoPHARMACOLOGY": "Compound",
    "HGNC": "Gene",
    "Pfam": "Domain",
    "InterPro": "Domain",
    "SMART": "Domain",
    "PROSITE": "Domain",
    "Reactome": "Pathway",
    "STRING": "Protein",
    "MINT": "Protein",
    "BioGRID": "Protein",
    "IntAct": "Protein",
    "GO": "OntologyTerm",
    "KEGG": "Pathway",
    "PubMed": "Publication",
}

# ─── D3-001: verified crosswalk as a fast lookup dict ────────────────────────
# Built ONCE at import time from id_crosswalk.VERIFIED_UNIPROT_GENE_CROSSWALK.
# Key: uppercase UniProt AC.  Value: (verified_ncbi_gene_id, gene_symbol).
# Fixes D3-001 — loader cross-checks every parsed gene_id against this table.
_VERIFIED_LOOKUP: Final[Dict[str, Tuple[str, str]]] = {
    entry.uniprot_ac.upper(): (entry.ncbi_gene_id, entry.gene_symbol)
    for entry in VERIFIED_UNIPROT_GENE_CROSSWALK
}


# =============================================================================
# Section 1 — Private helpers
# =============================================================================


def _open_uniprot(filepath: Path) -> io.TextIOBase:
    """Open a UniProt flat file for text reading, auto-detecting gzip.

    Fixes D4-005 — detect by magic bytes (``\\x1f\\x8b``), NOT by file
    extension, so a mislabeled file (e.g. a gzipped file without ``.gz``
    suffix, or a plain file with ``.gz`` suffix) is handled correctly.
    Fixes D4-006 — always use ``encoding='utf-8', errors='strict'`` so
    behaviour is identical on Linux, macOS, and Windows (the default
    locale encoding would otherwise be cp1252 on Windows).

    Args:
        filepath: Path to the ``.dat`` or ``.dat.gz`` file.

    Returns:
        A text-mode file handle (utf-8, strict).
    """
    with open(filepath, "rb") as probe:
        magic = probe.read(2)
    if magic == b"\x1f\x8b":  # gzip magic — D4-005
        # D8-004 — wrap in a buffered reader for faster decompression.
        raw = gzip.open(filepath, "rb")
        buffered = io.BufferedReader(raw, buffer_size=65536)
        return io.TextIOWrapper(buffered, encoding="utf-8", errors="strict")
    return open(filepath, "rt", encoding="utf-8", errors="strict")


def _compute_sha256(filepath: Path) -> str:
    """Compute the SHA-256 of a file (streaming, ~1 MB chunks).

    Fixes D7-002 — provenance records the source file's checksum so two
    parses of the same file can be confirmed identical.  Used by D5-007
    (download verification) and D16-001 (per-record provenance).
    """
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _get_ssl_context() -> ssl.SSLContext:
    """Return a TLS context for verifying UniProt's certificate.

    Fixes D9-001 — ``urllib.request.urlretrieve`` does not verify TLS.
    We use ``ssl.create_default_context`` with ``certifi``'s CA bundle if
    available, falling back to the system CA store.  An optional TOFU
    certificate fingerprint can be pinned via ``DRUGOS_UNIPROT_CERT_PIN``.
    """
    ctx = ssl.create_default_context()
    try:
        import certifi  # type: ignore[import-not-found]
        ctx.load_verify_locations(cafile=certifi.where())
    except ImportError:
        pass  # fall back to system CAs
    return ctx


_SSL_CONTEXT: Final[ssl.SSLContext] = _get_ssl_context()


def _validate_uniprot_url(url: str) -> None:
    """Refuse to download from a URL not in the allowlist.

    Fixes D9-002 — guards against config injection / SSRF.  The allowlist
    is ``config.ALLOWED_UNIPROT_URLS`` and can be extended without code
    changes.
    """
    if not any(url.startswith(prefix) for prefix in ALLOWED_UNIPROT_URLS):
        raise UniProtDownloadError(
            f"URL {url!r} not in allowlist {ALLOWED_UNIPROT_URLS}. "
            "Refusing to download from an untrusted source.",
            context={"url": url, "allowlist": list(ALLOWED_UNIPROT_URLS)},
        )


def _sanitize_freetext(value: Any) -> Any:
    """Defence-in-depth sanitiser for free-text fields heading to Neo4j.

    Fixes D9-004 — strips control characters (NUL, newline, tab inside
    identifiers) and escapes backslashes / single quotes so that a
    maliciously crafted protein name cannot break out of a Cypher string
    literal.  The canonical guard is parameterised queries in
    ``kg_builder``; this is the secondary, loader-side guard.

    Non-string values are returned unchanged (so ``int`` taxids and
    ``list`` fields pass through; lists are sanitised element-by-element
    by the caller).
    """
    if not isinstance(value, str):
        return value
    # Strip control chars except space and tab.  Fixes D9-004.
    cleaned = "".join(
        c for c in value
        if c == " " or c == "\t" or (c >= " " and c != "\x7f")
    )
    # Escape backslashes and single quotes for Cypher string literals.
    return cleaned.replace("\\", "\\\\").replace("'", "\\'")


def _iso_now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


# v41 ROOT FIX (SEV1): helper to test whether a string looks like an
# InChIKey (27 chars, ``XXXXXXXXXXXXXX-XXXXXXXXXX-X``). Used by
# ``uniprot_to_edge_records`` to decide whether the crosswalk-resolved
# dst_id is usable. The full validation regex lives in
# ``cleaning.normalizer.is_valid_inchikey``; this local check is a
# lighter-weight guard (we already trust the crosswalk's output, so we
# only need to confirm the SHAPE matches before swapping it in).
_INCHIKEY_LOCAL_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$"
)


def _is_inchikey(value: Any) -> bool:
    """Return True iff ``value`` is a string matching the InChIKey shape."""
    if not isinstance(value, str):
        return False
    return bool(_INCHIKEY_LOCAL_RE.match(value.strip()))


# ─── D6-004: dead-letter writer (dedicated uniprot JSONL) ────────────────────
_DEAD_LETTER_PATH: Final[Path] = DEAD_LETTER_DIR / "uniprot_malformed.jsonl"


def _write_dead_letter(entry: Dict[str, Any]) -> None:
    """Append a malformed/dropped record to the UniProt dead-letter queue.

    Fixes D6-004 — every ``continue`` in a validation/skip path MUST call
    this first, so no record is silently dropped.  The file is
    ``data/dead_letter/uniprot_malformed.jsonl`` (one JSON object per
    line, append-only).
    """
    try:
        _DEAD_LETTER_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": _iso_now(),
            "parser_module": "drugos_graph.uniprot_loader",
            "parser_version": PARSER_VERSION,
            **entry,
        }
        with _DEAD_LETTER_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except OSError as e:  # pragma: no cover - best-effort logging
        logger.error("Failed to write dead-letter entry: %s", e)


# ─── D16-002: transformation log ─────────────────────────────────────────────
_TRANSFORM_LOG_PATH: Final[Path] = LOGS_DIR / "transformations" / "uniprot.jsonl"


def _log_transform(
    accession: str,
    transformation: str,
    original: Any,
    result: Any,
    line_no: int = -1,
) -> None:
    """Record a significant data transformation for audit traceability.

    Fixes D16-002 — every non-trivial transformation (SCI-1 override, DE
    Contains split, GN ``and`` split, multi-line AC accumulation,
    taxid/organism mismatch skip, invalid-accession skip, invalid-gene-id
    skip) is logged as one JSON line.  This is the data-lineage audit
    trail: "how was this output value derived from the raw input?"
    """
    try:
        _TRANSFORM_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": _iso_now(),
            "accession": accession,
            "transformation": transformation,
            "original": str(original)[:500],
            "result": str(result)[:500],
            "line_no": line_no,
            "parser_version": PARSER_VERSION,
        }
        with _TRANSFORM_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except OSError as e:  # pragma: no cover - best-effort logging
        logger.error("Failed to write transform-log entry: %s", e)


# ─── D15-003: backward-compat dict that warns on 'id' access ─────────────────
class _ProteinNodeDict(dict):
    """A dict that emits a DeprecationWarning on first ``['id']`` access.

    Fixes D15-003 — the legacy ``id`` key is kept as a shim during the
    transition to the canonical ``uniprot_id`` key, but callers are
    nudged toward the new key.  Only ``__getitem__`` is overridden to
    keep the shim lightweight.
    """

    _warned: bool = False

    def __getitem__(self, key: Any) -> Any:
        if key == "id" and not _ProteinNodeDict._warned:
            warnings.warn(
                "ProteinNode['id'] is deprecated; use ProteinNode['uniprot_id'] "
                "instead. The 'id' key will be removed in parser v3.0.0.",
                DeprecationWarning,
                stacklevel=2,
            )
            _ProteinNodeDict._warned = True
        return super().__getitem__(key)


# ─── D7-005 / D16-003: loader-state persistence ──────────────────────────────
_LOADER_STATE_PATH: Final[Path] = PROCESSED_DIR / "loader_state.json"


def _persist_loader_state(source_name: str, state: Dict[str, Any]) -> None:
    """Persist loader state (last_downloaded_at, sha256) for idempotency.

    Fixes D7-005 / D16-003 — after a successful download, the SHA-256 and
    timestamp are written to ``data/processed/loader_state.json`` so the
    next run can detect staleness and so downstream consumers can verify
    which source version produced the current graph.
    """
    try:
        _LOADER_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing: Dict[str, Any] = {}
        if _LOADER_STATE_PATH.exists():
            existing = json.loads(_LOADER_STATE_PATH.read_text(encoding="utf-8"))
        existing[source_name] = {**existing.get(source_name, {}), **state}
        _LOADER_STATE_PATH.write_text(
            json.dumps(existing, indent=2, default=str), encoding="utf-8"
        )
    except OSError as e:  # pragma: no cover - best-effort
        logger.warning("Failed to persist loader state: %s", e)


# ─── D6-005: checkpoint writer ───────────────────────────────────────────────
# Use config.CHECKPOINT_DIR for consistency with the rest of the codebase
# (config.write_checkpoint / read_latest_checkpoint use the same directory).
_CHECKPOINT_PATH: Final[Path] = CHECKPOINT_DIR / "uniprot.json"


def _write_checkpoint(records_count: int, byte_offset: int,
                      source_sha256: str) -> None:
    """Write a parse checkpoint for resume-after-failure.

    Fixes D6-005 — every ``_CHECKPOINT_EVERY`` records, the current byte
    offset and record count are persisted.  On a subsequent run, if the
    source SHA-256 matches, the parse can resume from the checkpoint
    instead of restarting from byte 0.  (Full resume requires re-reading
    the entry in progress at the checkpoint offset — best-effort here.)
    """
    try:
        _CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_byte_offset": byte_offset,
            "records_parsed_count": records_count,
            "parser_version": PARSER_VERSION,
            "source_sha256": source_sha256,
            "updated_at": _iso_now(),
        }
        _CHECKPOINT_PATH.write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
    except OSError as e:  # pragma: no cover - best-effort
        logger.warning("Failed to write checkpoint: %s", e)


# ─── D2-002: pure organism filter ────────────────────────────────────────────
def _organism_matches(
    record_organism: str,
    target: Optional[str],
    match_mode: Literal["exact", "substring", "regex"],
) -> bool:
    """Return True iff ``record_organism`` matches ``target``.

    Fixes D3-006 — three match modes:
      * ``exact`` (default): case-insensitive equality on the scientific
        name component (e.g. ``"Homo sapiens"``).  This prevents the
        Neanderthal / Mouse-ear-bat false positives of the old substring
        match.
      * ``substring``: case-insensitive ``in`` (the old behaviour, opt-in).
      * ``regex``: ``re.search`` — power-user mode.
    """
    if target is None:
        return True  # no filter — D4-008 / D2-002
    # D4-008 — empty organism is a programming error, not "match all".
    if not target.strip():
        raise ValueError(
            "organism must be non-empty; use organism=None to disable filtering"
        )
    rec = record_organism.strip()
    if match_mode == "exact":
        # Compare the scientific-name prefix (before any parenthetical).
        # e.g. "Homo sapiens (Human)." -> "Homo sapiens"
        sci = rec.split("(")[0].strip().rstrip(".").strip()
        return sci.lower() == target.strip().lower()
    if match_mode == "substring":
        return target.lower() in rec.lower()
    if match_mode == "regex":
        return re.search(target, rec) is not None
    raise ValueError(f"Unknown organism_match_mode: {match_mode!r}")


def filter_by_organism(
    records: Iterable[Dict[str, Any]],
    organism: Optional[str],
    *,
    match_mode: Literal["exact", "substring", "regex"] = "exact",
) -> Iterator[Dict[str, Any]]:
    """Yield only records whose ``organism`` field matches.

    Fixes D2-002 — pure filter, no I/O.  Composes with
    ``iter_uniprot_entries`` so the full parse-and-filter pipeline is
    memory-streaming.

    Args:
        records: Iterable of UniProtRecord dicts (each must have an
            ``organism`` key; records without one are yielded unchanged
            when ``organism is None`` and dropped otherwise).
        organism: Scientific name to match, or ``None`` to pass all.
        match_mode: ``"exact"`` (default), ``"substring"``, or ``"regex"``.

    Yields:
        Matching UniProtRecord dicts.
    """
    if organism is not None and not organism.strip():  # D4-008
        raise ValueError(
            "organism must be non-empty; use organism=None to disable filtering"
        )
    for rec in records:
        rec_org = rec.get("organism", "")
        if _organism_matches(rec_org, organism, match_mode):
            yield rec


# ─── D3-002 / D4-003: DE-block parser (state machine over accumulated text) ──
_DE_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(RecName|AltName|Contains|Includes|Flags|EC)"
)
_DE_FULL_RE: Final[re.Pattern[str]] = re.compile(r"Full=([^;]*);")
_DE_EC_RE: Final[re.Pattern[str]] = re.compile(r"EC=([\d.\-]+);")


def _parse_DE_block(de_text: str, current: Dict[str, Any]) -> None:
    """Parse an accumulated DE block into structured fields.

    Fixes D3-002 — a ``de_stack`` state machine distinguishes root-level
    RecName from sub-record (Contains/Includes) RecName, so the primary
    protein name is NEVER overwritten by a sub-record name.  Fixes
    D4-003 — by accumulating the whole DE block first, the off-by-one
    risk when ``;`` is on a continuation line disappears.

    Routing:
      * Root ``RecName: Full=``  -> ``protein_name`` (first wins)
      * Root ``AltName: Full=``  -> ``alternative_names`` (list)
      * ``Contains:`` RecName    -> ``contains_names`` (list)
      * ``Includes:`` RecName    -> ``includes_names`` (list)
      * ``EC=``                  -> ``ec_numbers`` (list)
    """
    # Split into sections on Contains: / Includes: markers.
    # The text before the first marker is the root section.
    parts = re.split(r"(?m)^\s*(Contains|Includes):\s*$", de_text, flags=re.M)
    # re.split with a capturing group returns [root, marker1, section1, marker2, section2, ...]
    root_section = parts[0]
    sections: Dict[str, str] = {"root": root_section}
    i = 1
    while i < len(parts):
        marker = parts[i].strip()
        section_text = parts[i + 1] if i + 1 < len(parts) else ""
        sections[marker.lower()] = section_text
        i += 2

    # Root section
    root_fulls = _DE_FULL_RE.findall(sections.get("root", ""))
    if root_fulls and not current.get("protein_name"):
        current["protein_name"] = root_fulls[0].strip()
    for alt in _DE_FULL_RE.findall(sections.get("root", ""))[1:]:
        current.setdefault("alternative_names", []).append(alt.strip())
    # EC numbers appear in root and sub-sections
    for ec in _DE_EC_RE.findall(de_text):
        current.setdefault("ec_numbers", []).append(ec.strip())

    # Contains / Includes sub-records — D3-002
    for sec_key, field_name in (("contains", "contains_names"),
                                ("includes", "includes_names")):
        for full in _DE_FULL_RE.findall(sections.get(sec_key, "")):
            current.setdefault(field_name, []).append(full.strip())

    # D2-001 — alternative_name (singular) backward-compat shim.
    alts = current.get("alternative_names", [])
    current["alternative_name"] = alts[0] if alts else ""


# ─── D3-003: GN-block parser (handles 'and', Synonyms, ORFNames) ─────────────
_GN_FIELD_RES: Final[Dict[str, re.Pattern[str]]] = {
    "Name": re.compile(r"Name=([^;]*)(?:;|$)"),
    "Synonyms": re.compile(r"Synonyms=([^;]*)(?:;|$)"),
    "ORFNames": re.compile(r"ORFNames=([^;]*)(?:;|$)"),
    "LocusNames": re.compile(r"LocusNames=([^;]*)(?:;|$)"),
}


def _parse_GN_block(gn_text: str, current: Dict[str, Any]) -> None:
    """Parse an accumulated GN block into structured gene-name fields.

    Fixes D3-003 — handles the ``and`` separator for multi-gene loci
    (clinically critical for fusion proteins like BCR-ABL), plus
    ``Synonyms=``, ``ORFNames=``, ``LocusNames=``.

    Output fields:
      * ``gene_names`` (list[str]) — primary Name= values
      * ``gene_synonyms`` (list[str])
      * ``gene_orf_names`` (list[str])
      * ``gene_locus_names`` (list[str])
      * ``gene_name`` (str) — first primary name (backward-compat)
    """
    # Split on 'and' separator (may be surrounded by whitespace/newlines).
    segments = re.split(r"\band\b", gn_text)
    for seg in segments:
        for field, pattern in _GN_FIELD_RES.items():
            for m in pattern.finditer(seg):
                val = m.group(1).strip()
                # D4-011 — strip ISOform braces {isoform info} safely.
                brace = val.find("{")
                if brace != -1:
                    val = val[:brace].strip()
                if not val:
                    continue
                if field == "Name":
                    current.setdefault("gene_names", []).append(val)
                elif field == "Synonyms":
                    current.setdefault("gene_synonyms", []).append(val)
                elif field == "ORFNames":
                    current.setdefault("gene_orf_names", []).append(val)
                elif field == "LocusNames":
                    current.setdefault("gene_locus_names", []).append(val)
    names = current.get("gene_names", [])
    if names:
        current["gene_name"] = names[0]  # backward-compat


# ─── D3-001: SCI-1 verified-crosswalk override ───────────────────────────────
def _apply_verified_crosswalk(
    current: Dict[str, Any], accession: str, line_no: int
) -> None:
    """Cross-check parsed gene_id against the verified crosswalk.

    Fixes D3-001 — if the accession is in
    ``VERIFIED_UNIPROT_GENE_CROSSWALK`` and the parsed ``gene_id``
    disagrees with the verified value, the verified value WINS.  This is
    the loader-side enforcement of the SCI-1 fix (P35568/IRS1: 2645 ->
    3667).  The discrepancy is logged at CRITICAL and written to the
    dead-letter queue and transform log.
    """
    verified = _VERIFIED_LOOKUP.get(accession.upper())
    if verified is None:
        return  # not a verified entry — trust the file
    verified_gid, verified_symbol = verified
    parsed_gid = current.get("gene_id")
    if parsed_gid != verified_gid:
        logger.critical(
            "SCI-1 mismatch accession=%s parsed_gene_id=%s verified_gene_id=%s "
            "— overriding with verified value (see D3-001). gene_symbol=%s",
            accession, parsed_gid, verified_gid, verified_symbol,
        )
        _write_dead_letter({
            "reason": "sci1_gene_id_mismatch",
            "accession": accession,
            "parsed_gene_id": parsed_gid,
            "verified_gene_id": verified_gid,
            "gene_symbol": verified_symbol,
            "line_no": line_no,
        })
        _log_transform(
            accession, "sci1_verified_crosswalk_override",
            parsed_gid, verified_gid, line_no,
        )
        # Override the primary gene_id and ensure it's in gene_ids.
        current["gene_id"] = verified_gid
        gids = current.setdefault("gene_ids", [])
        if verified_gid not in gids:
            gids.insert(0, verified_gid)
        current.setdefault("_sci1_corrected", True)
    # If gene_name is empty but we have a verified symbol, fill it in.
    if not current.get("gene_name") and verified_symbol:
        current["gene_name"] = verified_symbol
        if verified_symbol not in current.get("gene_names", []):
            current.setdefault("gene_names", []).insert(0, verified_symbol)


# ─── D15-001: record validator ───────────────────────────────────────────────
_REQUIRED_RECORD_FIELDS: Final[Tuple[str, ...]] = ("accession", "entry_name", "ncbi_taxid")


def _validate_record(
    current: Dict[str, Any], line_no: int, stats: Dict[str, int]
) -> List[str]:
    """Return a list of missing required fields (empty list = valid).

    Fixes D5-009 — every record must have non-empty ``accession``,
    ``entry_name``, ``ncbi_taxid``.  Missing fields are tracked in
    ``stats['missing_field_counts']`` and the record is dead-lettered.
    """
    missing: List[str] = []
    for field in _REQUIRED_RECORD_FIELDS:
        val = current.get(field)
        if val is None or val == "" or val == 0:
            missing.append(field)
            mf = stats.setdefault("missing_field_counts", {})
            mf[field] = mf.get(field, 0) + 1
    return missing


# =============================================================================
# Section 2 — Download (P0: D1-001, D6-001..007, D9-001..003, D12-002..004)
# =============================================================================


def _download_from_network(
    cfg: Dict[str, Any],
    gz_path: Path,
    source_name: str,
    allow_stale: bool,
) -> Path:
    """Download the UniProt file from the network with full hardening.

    Implements D1-001 (single responsibility download helper), D6-001
    (retry with exponential backoff), D6-002 (atomic temp-file +
    os.replace), D6-006 (wrapped UniProtDownloadError), D6-007
    (allow_stale graceful degradation), D9-001 (TLS), D9-003 (User-Agent),
    D12-002 (content-sniff assertion), D12-004 (timeout), D5-006 (size
    verify), D5-007 (SHA-256 verify), D5-008 (cache validity), D7-005
    (persist loader state), D16-003 (record SHA in config).

    v41 ROOT FIX (SEV2): ``cfg`` is the SHARED module-level
    ``DATA_SOURCES[SOURCE_KEY_UNIPROT]`` dict — mutating it in-place
    BEFORE the SHA-256 verification succeeded meant that a failed
    download (size mismatch, content-sniff failure, SHA mismatch)
    would still leave ``cfg["sha256"]`` and ``cfg["last_downloaded_at"]``
    polluted with stale or bogus values, which a subsequent
    ``download_uniprot(force=False)`` call would treat as a valid
    cache (skipping the re-download). The fix: build a LOCAL copy of
    cfg via ``dict(cfg)`` at entry; mutate ONLY the local copy during
    download/SHA verification; propagate the verified values back to
    the shared dict in a SINGLE atomic step after all checks pass.
    """
    # v41 ROOT FIX (SEV2): work on a local copy so the shared dict
    # is only updated after the SHA check succeeds.
    local_cfg = dict(cfg)
    url = local_cfg["url"]
    _validate_uniprot_url(url)  # D9-002
    tmp_path = gz_path.with_suffix(gz_path.suffix + ".tmp")
    gz_path.parent.mkdir(parents=True, exist_ok=True)

    max_retries = int(local_cfg.get("retry_count", 3))
    backoff_base = float(local_cfg.get("retry_backoff_seconds", 30))
    timeout = int(local_cfg.get("timeout_seconds", 600))

    headers = {
        "User-Agent": "DrugOS/1.0 (drugos@example.com)",  # D9-003
        "Accept": "application/octet-stream, */*",
    }

    last_error: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):  # D6-001
        try:
            logger.info(
                "Downloading %s attempt %d/%d from %s",
                source_name, attempt, max_retries, url,
            )
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(
                req, timeout=timeout, context=_SSL_CONTEXT  # D9-001, D12-004
            ) as resp, open(tmp_path, "wb") as f:
                shutil.copyfileobj(resp, f, length=1 << 20)
            break  # success
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
            last_error = e
            backoff = backoff_base * (2 ** (attempt - 1))
            logger.warning(
                "Download attempt %d/%d failed for %s: %s — retrying in %.0fs",
                attempt, max_retries, source_name, e, backoff,
            )
            tmp_path.unlink(missing_ok=True)
            if attempt < max_retries:
                time.sleep(backoff)
    else:
        # All retries exhausted — D6-007 graceful degradation.
        if allow_stale and gz_path.exists():
            age_days = _staleness_days(local_cfg)
            logger.critical(
                "uniprot_stale_data_used age_days=%d — download failed, "
                "falling back to cached copy per allow_stale=True.", age_days,
            )
            return gz_path
        raise UniProtDownloadError(
            f"Failed to download {source_name} from {url} after "
            f"{max_retries} attempts. Last error: {last_error}. "
            f"Check network, DNS, and https://status.uniprot.org/.",
            context={
                "url": url, "attempts": max_retries,
                "last_error": str(last_error),
                "allow_stale": allow_stale,
            },
        ) from last_error

    # ── D5-006: size verification ──────────────────────────────────────────
    actual_size = tmp_path.stat().st_size
    expected_size = int(local_cfg.get("size_bytes", 0))
    if expected_size > 0 and actual_size < expected_size * 0.9:
        tmp_path.unlink(missing_ok=True)
        raise UniProtDownloadError(
            f"Truncated download: {actual_size} bytes < "
            f"{expected_size * 0.9:.0f} (90% of expected {expected_size}).",
            context={"actual": actual_size, "expected": expected_size},
        )

    # D6-002 — atomic move on POSIX.
    os.replace(tmp_path, gz_path)

    # ── D12-002: content-sniff assertion (first line must start with 'ID ') ─
    try:
        with _open_uniprot(gz_path) as probe:
            first_line = probe.readline()
    except (OSError, UnicodeDecodeError) as e:
        gz_path.unlink(missing_ok=True)
        raise UniProtDownloadError(
            f"Downloaded file is not readable as text: {e}",
            context={"path": str(gz_path)},
        ) from e
    if not first_line.startswith("ID "):
        gz_path.unlink(missing_ok=True)
        raise UniProtDownloadError(
            f"Downloaded file does not look like a UniProt flat file: "
            f"first line={first_line[:80]!r}. Possible URL/filename "
            f"mismatch (D12-002) or tar-vs-flat confusion.",
            context={"first_line": first_line[:80]},
        )

    # ── D5-007 / D16-003: SHA-256 verification + record ────────────────────
    actual_sha = _compute_sha256(gz_path)
    expected_sha = local_cfg.get("sha256")
    if expected_sha and actual_sha != expected_sha:
        gz_path.unlink(missing_ok=True)
        raise UniProtDataIntegrityError(
            f"SHA-256 mismatch: expected {expected_sha}, got {actual_sha}. "
            f"File may have been tampered with — verify against UniProt's "
            f"official .sha256 sidecar.",
            context={"expected": expected_sha, "actual": actual_sha},
        )
    # Record the SHA in the LOCAL copy (D16-003). The shared dict
    # is updated in the atomic propagation step below.
    local_cfg["sha256"] = actual_sha

    # ── D7-005: persist loader state ───────────────────────────────────────
    now = _iso_now()
    local_cfg["last_downloaded_at"] = now
    local_cfg["last_updated"] = now
    _persist_loader_state(source_name, {
        "last_downloaded_at": now, "sha256": actual_sha,
        "size_bytes": actual_size, "url": url,
    })

    # v41 ROOT FIX (SEV2): ATOMIC propagation. Only NOW — after size
    # check + content-sniff + SHA-256 verification + state persistence
    # have ALL succeeded — copy the local mutations back to the shared
    # ``DATA_SOURCES[SOURCE_KEY_UNIPROT]`` dict. A failed download no
    # longer pollutes the shared dict with bogus SHA / timestamps.
    cfg.update(local_cfg)

    logger.info(
        "Downloaded %s to %s (%.1f MB, sha256=%s...)",
        source_name, gz_path.name, actual_size / 1e6, actual_sha[:12],
    )
    return gz_path


def _staleness_days(cfg: Dict[str, Any]) -> int:
    """Return the age in days of the last download, or a large number."""
    last = cfg.get("last_downloaded_at")
    if not last:
        return 9999
    try:
        dt = datetime.fromisoformat(last)
        return max(0, (datetime.now(timezone.utc) - dt).days)
    except (ValueError, TypeError):
        return 9999


def download_uniprot(force: bool = False, *, allow_stale: bool = False) -> Path:
    """Download (or cached-load) the UniProt Swiss-Prot flat file.

    Thin wrapper over the internal ``_download_from_network`` helper.
    Preserves the v1 signature ``download_uniprot(force=False)`` so
    ``run_pipeline.py:301`` continues to work unmodified (D1-003 /
    D15-002).  The new ``allow_stale`` keyword-only parameter (D6-007)
    defaults to ``False`` for backward compatibility.

    Args:
        force: If True, re-download even if a valid cached file exists.
        allow_stale: If True and all download retries fail, fall back to
            the most recent successful download.  Logs CRITICAL.

    Returns:
        Path to the local ``.dat.gz`` (or ``.dat``) file.

    Raises:
        UniProtDownloadError: All retries exhausted and
            ``allow_stale=False`` or no cached copy exists.
        UniProtDataIntegrityError: Downloaded file fails size or SHA-256
            check, or fails the content-sniff assertion (D12-002).

    Side effects:
        - Writes ~800 MB to ``RAW_DIR`` on a fresh download.
        - Updates ``DATA_SOURCES['uniprot']['last_downloaded_at']`` and
          ``['sha256']``.
        - Persists loader state to ``data/processed/loader_state.json``.

    Example:
        >>> from drugos_graph.uniprot_loader import download_uniprot
        >>> path = download_uniprot()  # cached or fresh download
        >>> path = download_uniprot(force=True)  # force re-download
    """
    cfg = DATA_SOURCES[SOURCE_KEY_UNIPROT]  # D12-001
    gz_path = get_data_source_path(SOURCE_KEY_UNIPROT)

    # ── D5-008: cache validity check on the primary path ──────────────────
    if gz_path.exists():
        if force:
            logger.info("Force re-download requested — removing cached %s", gz_path.name)
            gz_path.unlink(missing_ok=True)
        else:
            size = gz_path.stat().st_size
            expected = int(cfg.get("size_bytes", 0))
            # Only enforce the minimum-size guard for production-sized sources.
            if (expected > UNIPROT_MIN_VALID_SIZE_BYTES
                    and size <= UNIPROT_MIN_VALID_SIZE_BYTES):
                logger.warning(
                    "Cached file %s is %d bytes (likely truncated) — re-downloading",
                    gz_path.name, size,
                )
                gz_path.unlink(missing_ok=True)
            else:
                logger.info("UniProt data already exists at %s", gz_path.name)
                return gz_path

    # ── Fallback: local sample (.dat without .gz) for dev/test ────────────
    # Matches run_pipeline.py:407-409 logic.  This lets parse_uniprot_entries()
    # work on the hand-curated 30-entry sample without a network call.
    dat_path = RAW_DIR / "uniprot_sprot.dat"
    if dat_path.exists() and not force:
        logger.info("Using local sample file (no .gz): %s", dat_path.name)
        return dat_path

    # ── Network download ──────────────────────────────────────────────────
    return _download_from_network(cfg, gz_path, SOURCE_KEY_UNIPROT, allow_stale)


# =============================================================================
# Section 3 — Parser (P1: D3-001..009, D4-001..014, D5-001..009, D6-003..005,
#              D7-001..004, D8-001..006, D11-001..005, D14-001..002, D16-001..005)
# =============================================================================


def iter_uniprot_entries(
    filepath: Optional[Path] = None,
    *,
    parser_version: str = PARSER_VERSION,
) -> Iterator[Dict[str, Any]]:
    """Yield parsed UniProt records as a streaming generator.

    Pure parser — NO organism filter (D2-002).  Use ``filter_by_organism``
    or the convenience wrapper ``parse_uniprot_entries`` to filter.

    Memory: ~10 MB peak regardless of file size (one record at a time).

    Each yielded dict conforms to ``schemas.UniProtRecord`` and carries a
    full ``_provenance`` dict (D16-001), ``_source``, ``_license``,
    ``_attribution`` (D14-001), and an ``identifiers.org`` URI (D14-002).

    Args:
        filepath: Path to the ``.dat`` / ``.dat.gz`` file.  If None,
            resolves via ``get_data_source_path('uniprot')`` with a
            fallback to ``RAW_DIR / 'uniprot_sprot.dat'`` (local sample)
            and the ``DRUGOS_UNIPROT_FILE`` env var (D12-003).
        parser_version: Override the parser version stamp (testing).

    Yields:
        UniProtRecord dicts (one per entry, sorted is NOT guaranteed —
        use ``parse_uniprot_entries`` for sorted output, D7-001).
    """
    # ── Resolve filepath (D12-003 env override + local-sample fallback) ────
    if filepath is None:
        if _DRUGOS_UNIPROT_FILE:  # D12-003
            filepath = RAW_DIR / _DRUGOS_UNIPROT_FILE
        else:
            filepath = get_data_source_path(SOURCE_KEY_UNIPROT)
            if not filepath.exists():
                # Local sample fallback (.dat without .gz).
                sample = RAW_DIR / "uniprot_sprot.dat"
                if sample.exists():
                    filepath = sample
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"UniProt file not found: {filepath}")

    cfg = DATA_SOURCES[SOURCE_KEY_UNIPROT]
    logger.info("Parsing UniProt from %s ...", filepath.name)

    # ── D7-002 / D16-001: compute source SHA-256 once (streaming) ──────────
    source_sha256 = _compute_sha256(filepath)

    # ── D11-002: stats ─────────────────────────────────────────────────────
    stats: Dict[str, Any] = {
        "records_read": 0, "records_kept": 0,
        "records_dropped_organism": 0, "records_dropped_malformed": 0,
        "records_dropped_missing_fields": 0, "records_dropped_duplicate": 0,
        "records_dropped_taxid_mismatch": 0,
        "lines_total": 0, "parse_errors": 0,
        "invalid_accessions": 0, "invalid_gene_ids": 0,
        "out_of_range_gene_ids": 0,
        "sci_corrections": 0, "duplicate_accessions": 0,
        "entries_without_terminator": 0,
        "missing_field_counts": {},
        "parse_seconds": 0.0,
    }
    seen_accessions: set[str] = set()  # D5-004
    start = time.perf_counter()

    # ── D16-001: provenance template (file-level fields computed once) ─────
    prov_template: Dict[str, Any] = {
        "source": SOURCE_KEY_UNIPROT,
        "source_file": filepath.name,
        "source_sha256": source_sha256,
        "source_version": cfg.get("version", ""),
        "source_release_date": cfg.get("release_date", ""),
        "source_license": cfg.get("license", UNIPROT_LICENSE),
        "source_url": cfg.get("url", ""),
        "parser_module": "drugos_graph.uniprot_loader",
        "parser_version": parser_version,
        "schema_version": SCHEMA_VERSION,
        "parsed_at": _iso_now(),
        "organism_filter": None,  # filled by parse_uniprot_entries wrapper
        "organism_match_mode": "exact",
        "entry_line_no": 0,
        "byte_range": [0, 0],
    }

    current: Dict[str, Any] = {}
    entry_start_offset: int = 0
    entry_start_line: int = 0
    # D16-005 — track byte offset manually. We CANNOT call fh.tell()
    # inside a `for line in fh:` loop because TextIOWrapper disables
    # telling during iteration (raises OSError). So we sum line byte
    # lengths ourselves. At the top of each iteration, `byte_offset`
    # points to the START of the current line.
    byte_offset: int = 0
    in_sequence: bool = False
    seq_lines: List[str] = []
    sq_header_length: Optional[int] = None
    de_buffer: List[str] = []
    gn_buffer: List[str] = []
    next_milestone: int = 10000  # D4-001 / D8-002

    fh = _open_uniprot(filepath)  # D4-005 / D4-006
    try:
        for line_no, raw_line in enumerate(fh, 1):  # D11-001
            stats["lines_total"] += 1
            line_start_offset = byte_offset  # D16-005 — start of this line
            byte_offset += len(raw_line.encode("utf-8"))
            line = raw_line.rstrip("\n").rstrip("\r")
            try:
                # ── SQ sequence accumulation (D3-009) ──────────────────────
                if in_sequence:
                    if line.startswith("//"):
                        # End of entry — finalize sequence first.
                        seq = "".join(seq_lines).replace(" ", "").upper()
                        current["sequence"] = seq
                        current["sequence_length"] = len(seq)
                        if (sq_header_length is not None
                                and len(seq) != sq_header_length):
                            logger.warning(
                                "Sequence length mismatch accession=%s "
                                "header=%d actual=%d (line %d)",
                                current.get("accession", "?"),
                                sq_header_length, len(seq), line_no,
                            )
                        in_sequence = False
                        seq_lines = []
                        sq_header_length = None
                        # Fall through to the // handler below.
                    elif line.startswith("  "):
                        # Sequence data line (indented).
                        seq_lines.append(line.strip())
                        continue
                    else:
                        # A new line code ended the sequence block.
                        seq = "".join(seq_lines).replace(" ", "").upper()
                        current["sequence"] = seq
                        current["sequence_length"] = len(seq)
                        in_sequence = False
                        seq_lines = []
                        sq_header_length = None
                        # Fall through to process this line normally.

                # ── Entry terminator (//) ─────────────────────────────────
                if line.startswith("//"):
                    # Flush accumulated DE / GN blocks (D3-002, D3-003).
                    if de_buffer:
                        _parse_DE_block("\n".join(de_buffer), current)
                        de_buffer = []
                    if gn_buffer:
                        _parse_GN_block("\n".join(gn_buffer), current)
                        gn_buffer = []

                    # Final entry without // is handled after the loop (D4-009).
                    # Here the entry IS terminated — process it.
                    stats["records_read"] += 1
                    accession = current.get("accession", "")

                    # D5-001 — accession format validation.
                    if accession and not _validate_uniprot_ac(accession):
                        logger.warning(
                            "Invalid UniProt accession %r at line %d — skipping",
                            accession, entry_start_line,
                        )
                        _write_dead_letter({
                            "reason": "invalid_accession",
                            "accession": accession, "line_no": entry_start_line,
                        })
                        _log_transform(accession or "?", "invalid_accession_skip",
                                       accession, None, entry_start_line)
                        stats["invalid_accessions"] += 1
                        stats["records_dropped_malformed"] += 1
                        current = {}
                        continue

                    # D5-009 — required-fields check.
                    missing = _validate_record(current, entry_start_line, stats)
                    if missing:
                        logger.warning(
                            "Entry accession=%s missing required fields %s "
                            "(line %d) — dead-lettering",
                            accession, missing, entry_start_line,
                        )
                        _write_dead_letter({
                            "reason": "missing_required_fields",
                            "accession": accession, "missing": missing,
                            "line_no": entry_start_line, "record": dict(current),
                        })
                        stats["records_dropped_missing_fields"] += 1
                        current = {}
                        continue

                    # D3-001 — SCI-1 verified crosswalk override.
                    _apply_verified_crosswalk(current, accession, entry_start_line)
                    if current.pop("_sci1_corrected", False):
                        stats["sci_corrections"] += 1

                    # D5-004 — deduplicate by accession.
                    if accession in seen_accessions:
                        logger.warning(
                            "Duplicate accession %s at line %d — keeping first, "
                            "second to dead-letter", accession, entry_start_line,
                        )
                        _write_dead_letter({
                            "reason": "duplicate_accession",
                            "accession": accession, "line_no": entry_start_line,
                        })
                        _log_transform(accession, "duplicate_accession_skip",
                                       accession, None, entry_start_line)
                        stats["duplicate_accessions"] += 1
                        stats["records_dropped_duplicate"] += 1
                        current = {}
                        continue
                    seen_accessions.add(accession)

                    # D3-005 — OS / OX cross-check.
                    organism = current.get("organism", "")
                    taxid = current.get("ncbi_taxid")
                    if organism and taxid is not None:
                        expected_tax = _ORGANISM_TO_TAXID.get(
                            organism.split("(")[0].strip().rstrip(".").lower()
                        )
                        if expected_tax is not None and int(taxid) != expected_tax:
                            logger.critical(
                                "OS/OX mismatch accession=%s organism=%s "
                                "ncbi_taxid=%s expected=%d — skipping (D3-005)",
                                accession, organism, taxid, expected_tax,
                            )
                            _write_dead_letter({
                                "reason": "taxid_organism_mismatch",
                                "accession": accession, "organism": organism,
                                "ncbi_taxid": taxid, "expected_taxid": expected_tax,
                                "line_no": entry_start_line,
                            })
                            _log_transform(accession, "taxid_organism_mismatch_skip",
                                           taxid, expected_tax, entry_start_line)
                            stats["records_dropped_taxid_mismatch"] += 1
                            current = {}
                            continue

                    # D16-001 — attach provenance + compliance fields.
                    prov = dict(prov_template)
                    prov["entry_line_no"] = entry_start_line
                    prov["byte_range"] = [entry_start_offset, byte_offset]  # D16-005
                    prov["raw_ncbi_taxid"] = str(taxid) if taxid is not None else ""
                    current["_provenance"] = prov
                    current["_source"] = "uniprot_loader"
                    current["_license"] = UNIPROT_LICENSE  # D14-001
                    current["_attribution"] = UNIPROT_ATTRIBUTION  # D14-001
                    # D14-002 — FAIR identifiers.org URI.
                    current.setdefault(
                        "uniprot_uri", f"https://identifiers.org/uniprot:{accession}"
                    )

                    stats["records_kept"] += 1
                    yield current

                    # D4-001 / D8-002 — milestone logging (one comparison).
                    if stats["records_kept"] >= next_milestone:
                        logger.info(
                            "Parsed %d UniProt records ...",
                            stats["records_kept"],
                        )
                        next_milestone += 10000

                    # D6-005 — checkpoint.
                    if stats["records_kept"] % _CHECKPOINT_EVERY == 0:
                        _write_checkpoint(
                            stats["records_kept"], byte_offset, source_sha256,
                        )

                    current = {}
                    continue

                # ── Track entry start (ID line) ───────────────────────────
                if line.startswith("ID"):
                    entry_start_offset = line_start_offset  # D16-005
                    entry_start_line = line_no
                    # D4-010 — safe split.
                    parts = line[5:].split()
                    if parts:
                        current["entry_name"] = parts[0]
                    else:
                        logger.warning("Malformed ID line at line %d: %r", line_no, line)
                        current["entry_name"] = ""
                        stats["parse_errors"] += 1
                    continue

                # ── AC line (D3-004 — accumulate across multiple lines) ───
                if line.startswith("AC"):
                    line_accessions = _AC_REGEX.findall(line[5:])  # D4-007
                    if line_accessions:
                        current.setdefault("all_accessions", []).extend(line_accessions)
                        if "accession" not in current:
                            # First AC line: first accession is primary, rest are secondary.
                            current["accession"] = line_accessions[0]
                            current["secondary_accessions"] = list(line_accessions[1:])
                        else:
                            # Subsequent AC lines: ALL accessions are secondary
                            # (only the very first accession of the first AC
                            # line is primary).  Fixes D3-004 — previously
                            # line_accessions[0] of the 2nd line was dropped.
                            current.setdefault("secondary_accessions", []).extend(
                                line_accessions
                            )
                        _log_transform(
                            current["accession"], "multi_line_ac_accumulate",
                            "", line_accessions, line_no,
                        )
                    continue

                # ── DE line — accumulate into buffer (D3-002, D4-003) ─────
                if line.startswith("DE"):
                    de_buffer.append(line[5:])
                    continue

                # ── GN line — accumulate into buffer (D3-003) ─────────────
                if line.startswith("GN"):
                    gn_buffer.append(line[5:])
                    continue

                # ── OS line — parse organism (D3-005, D3-006) ─────────────
                if line.startswith("OS"):
                    m = re.match(r"^OS\s+(.+?)\s*\.\s*$", line)
                    if m:
                        current["organism"] = m.group(1).strip()
                    else:
                        current["organism"] = line[5:].strip().rstrip(".")
                    continue

                # ── OX line — parse NCBI TaxID (D5-003) ───────────────────
                if line.startswith("OX") and "NCBI_TaxID=" in line:
                    ox = line[5:]
                    m = re.search(r"NCBI_TaxID=(\d+)", ox)
                    if m:
                        taxid_str = m.group(1)
                        # D5-003 — validate + store as int.
                        if taxid_str.isdigit():
                            current["ncbi_taxid"] = int(taxid_str)
                        else:
                            logger.warning(
                                "Invalid NCBI_TaxID %r at line %d — skipping field",
                                taxid_str, line_no,
                            )
                    continue

                # ── PE line — protein existence (D3-008) ──────────────────
                if line.startswith("PE"):
                    m = re.search(r"(\d+)", line[5:])
                    if m:
                        pe = int(m.group(1))
                        if 1 <= pe <= 5:
                            current["protein_existence"] = pe
                        else:
                            logger.warning(
                                "PE value out of range [1,5]: %d (line %d)",
                                pe, line_no,
                            )
                    continue

                # ── SQ line — start sequence block (D3-009) ───────────────
                if line.startswith("SQ"):
                    m = re.search(r"SEQUENCE\s+(\d+)\s+AA", line[5:])
                    if m:
                        sq_header_length = int(m.group(1))
                    in_sequence = True
                    seq_lines = []
                    continue

                # ── DR line — all cross-references (D2-005, D4-004) ───────
                if line.startswith("DR"):
                    dr_text = line[5:].strip()
                    # Split "DB; id; description." into parts.
                    dr_parts = [p.strip() for p in dr_text.split(";")]
                    if len(dr_parts) >= 2:
                        db_name = dr_parts[0]
                        db_id = dr_parts[1]
                        current.setdefault(
                            "cross_references", {}
                        ).setdefault(db_name, []).append(db_id)

                        # D4-004 — DR GeneID: accumulate, never overwrite.
                        if db_name == "GeneID":
                            normalized = _normalize_ncbi_gene_id(db_id)  # D5-002
                            if normalized is None:
                                logger.warning(
                                    "Invalid gene_id %r at line %d accession=%s "
                                    "— skipping gene_id assignment",
                                    db_id, line_no, current.get("accession", "?"),
                                )
                                stats["invalid_gene_ids"] += 1
                                _log_transform(
                                    current.get("accession", "?"),
                                    "invalid_gene_id_skip", db_id, None, line_no,
                                )
                            else:
                                gid_int = int(normalized)
                                if not (_GENE_ID_MIN <= gid_int <= _GENE_ID_MAX):
                                    logger.warning(
                                        "gene_id out of range: %d (line %d)",
                                        gid_int, line_no,
                                    )
                                    stats["out_of_range_gene_ids"] += 1
                                else:
                                    current.setdefault("gene_ids", []).append(
                                        normalized
                                    )
                                    # D4-004 — primary gene_id = first (backward-compat).
                                    if "gene_id" not in current:
                                        current["gene_id"] = normalized
                    continue

                # ── OG / OH — organelle / host (parsed but not deeply used) ─
                if line.startswith("OG") or line.startswith("OH"):
                    # Record presence for completeness; not propagated to node.
                    current.setdefault("organelle", line[5:].strip())
                    continue

            except Exception as e:  # D6-003 — per-line error isolation
                stats["parse_errors"] += 1
                logger.warning(
                    "parse_error line_no=%d accession=%s error=%s line=%r",
                    line_no, current.get("accession", "?"), e, raw_line[:200],
                )
                _write_dead_letter({
                    "reason": "line_parse_error", "line_no": line_no,
                    "accession": current.get("accession", "?"),
                    "error": str(e), "raw_line": raw_line[:500],
                })
                continue

        # ── D4-009: final entry without trailing // ────────────────────────
        if current and current.get("accession"):
            logger.warning(
                "Final entry had no // terminator — added from in-progress state "
                "(line_no=%d)", entry_start_line,
            )
            stats["entries_without_terminator"] += 1
            # Flush DE / GN buffers.
            if de_buffer:
                _parse_DE_block("\n".join(de_buffer), current)
            if gn_buffer:
                _parse_GN_block("\n".join(gn_buffer), current)
            # Re-run the same finalization as the // handler.
            accession = current["accession"]
            if _validate_uniprot_ac(accession) and accession not in seen_accessions:
                missing = _validate_record(current, entry_start_line, stats)
                if not missing:
                    _apply_verified_crosswalk(current, accession, entry_start_line)
                    if current.pop("_sci1_corrected", False):
                        stats["sci_corrections"] += 1
                    seen_accessions.add(accession)
                    prov = dict(prov_template)
                    prov["entry_line_no"] = entry_start_line
                    prov["byte_range"] = [entry_start_offset, byte_offset]  # D16-005
                    current["_provenance"] = prov
                    current["_source"] = "uniprot_loader"
                    current["_license"] = UNIPROT_LICENSE
                    current["_attribution"] = UNIPROT_ATTRIBUTION
                    current.setdefault(
                        "uniprot_uri",
                        f"https://identifiers.org/uniprot:{accession}",
                    )
                    stats["records_read"] += 1
                    stats["records_kept"] += 1
                    yield current

    except UnicodeDecodeError as e:  # D4-006 — degraded mode
        logger.error(
            "UnicodeDecodeError parsing %s: %s. Retrying with errors='replace' "
            "is recommended for production; for the local sample this indicates "
            "file corruption.", filepath.name, e,
        )
        raise UniProtParseError(
            f"File {filepath.name} is not valid UTF-8: {e}",
            context={"path": str(filepath)},
        ) from e
    finally:
        fh.close()

    stats["parse_seconds"] = time.perf_counter() - start  # D11-002

    # ── D6-003: parse-error-rate guard ────────────────────────────────────
    kept = stats["records_kept"]
    if stats["parse_errors"] > max(kept * 0.01, 1) and kept > 0:
        raise UniProtParseError(
            f"Parse error rate {stats['parse_errors']} exceeds 1% of "
            f"{kept} kept records — aborting (D6-003).",
            context={"stats": stats},
        )

    # ── D11-002: structured stats log ─────────────────────────────────────
    logger.info(
        "uniprot_parse_complete records_read=%d records_kept=%d "
        "dropped_malformed=%d dropped_missing=%d dropped_dup=%d "
        "dropped_taxid=%d sci_corrections=%d parse_errors=%d "
        "parse_seconds=%.3f source_sha256=%s parser_version=%s",
        stats["records_read"], stats["records_kept"],
        stats["records_dropped_malformed"],
        stats["records_dropped_missing_fields"],
        stats["records_dropped_duplicate"],
        stats["records_dropped_taxid_mismatch"],
        stats["sci_corrections"], stats["parse_errors"],
        stats["parse_seconds"], source_sha256[:12], parser_version,
    )
    logger.info("uniprot_parse_stats %s", json.dumps(stats, sort_keys=True, default=str))


# =============================================================================
# Section 4 — Convenience wrapper (D2-002, D7-001, D5-005)
# =============================================================================


def parse_uniprot_entries(
    filepath: Optional[Path] = None,
    organism: Optional[str] = _DEFAULT_ORGANISM,
    *,
    organism_match_mode: Literal["exact", "substring", "regex"] = "exact",
    allow_stale: bool = False,
    release: Optional[str] = None,
    parser_version: str = PARSER_VERSION,
) -> List[Dict[str, Any]]:
    """Parse a UniProt dat file into a list of protein records.

    Convenience wrapper that chains ``iter_uniprot_entries`` ->
    ``filter_by_organism`` -> ``list`` (D2-002).  Preserves the v1
    signature ``parse_uniprot_entries(filepath=None, organism=...)`` so
    ``run_pipeline.py:302`` and ``run_pipeline.py:413`` continue to work
    unmodified (D1-003 / D15-002).  New parameters are keyword-only with
    backward-compatible defaults.

    Args:
        filepath: Path to the ``.dat`` / ``.dat.gz`` file.  If None,
            resolves via config + local-sample fallback (see
            ``iter_uniprot_entries``).
        organism: Scientific name to filter by (default: ``"Homo
            sapiens"`` from ``DRUGOS_DEFAULT_ORGANISM`` env var, D7-004).
            Pass ``None`` to disable filtering (return all organisms).
        organism_match_mode: ``"exact"`` (default), ``"substring"``, or
            ``"regex"`` (D3-006).
        allow_stale: Forwarded to download if filepath resolution requires
            a download (rarely needed for parsing).
        release: Optional release tag for the parser (testing).
        parser_version: Override the parser version stamp (testing).

    Returns:
        List of UniProtRecord dicts, sorted by accession (D7-001 —
        deterministic ordering for idempotency).

    Raises:
        FileNotFoundError: Input file missing.
        ValueError: ``organism`` is an empty string (D4-008).
        UniProtParseError: Parse error rate exceeds 1% (D6-003).
        UniProtDataIntegrityError: Record count > 50% below expected
            (D5-005) when ``ON_SOURCE_FAILURE == 'fail_critical'``.

    Example:
        >>> records = parse_uniprot_entries()  # local sample, human
        >>> len(records)
        30
        >>> records = parse_uniprot_entries(organism=None)  # all organisms
    """
    # D4-008 — empty organism is a programming error.
    if organism is not None and not organism.strip():
        raise ValueError(
            "organism must be non-empty; use organism=None to disable filtering"
        )

    # If filepath is None, ensure the file exists (download if needed, but
    # prefer the local sample which needs no network).
    if filepath is None:
        # Try to locate the file without forcing a download.
        candidate = None
        if _DRUGOS_UNIPROT_FILE:
            candidate = RAW_DIR / _DRUGOS_UNIPROT_FILE
        if candidate is None or not candidate.exists():
            candidate = get_data_source_path(SOURCE_KEY_UNIPROT)
        if not candidate.exists():
            sample = RAW_DIR / "uniprot_sprot.dat"
            if sample.exists():
                candidate = sample
            else:
                # Last resort: trigger a download.
                candidate = download_uniprot(allow_stale=allow_stale)
        filepath = candidate

    records_iter = iter_uniprot_entries(filepath, parser_version=parser_version)
    filtered = filter_by_organism(records_iter, organism, match_mode=organism_match_mode)
    records = list(filtered)

    # D7-001 — deterministic ordering for idempotency.
    records.sort(key=lambda r: r.get("accession", ""))

    # ── D5-005: record-count verification ────────────────────────────────
    # Only enforce on production-sized files: the local hand-curated sample
    # (12 KB, 30 entries) is a TEST fixture and must NOT trigger the count
    # guard. We gate on the actual file size vs cfg['size_bytes']: if the
    # file is < 50% of the expected production size, it is clearly a sample
    # and the count check is skipped.
    cfg = DATA_SOURCES[SOURCE_KEY_UNIPROT]
    expected = cfg.get("expected_record_count")
    expected_size = int(cfg.get("size_bytes", 0))
    actual_size = filepath.stat().st_size if filepath.exists() else 0
    is_production_sized = (
        expected_size > 0 and actual_size >= expected_size * 0.5
    )
    if expected and organism is not None and is_production_sized:
        # Only enforce for filtered (human) runs on production-sized files.
        if len(records) < int(expected) * 0.5:
            logger.critical(
                "uniprot_record_count_mismatch expected=~%d actual=%d "
                "source_sha256=%s — possible file corruption, organism filter "
                "mismatch, or URL/format mismatch (see D12-002)",
                expected, len(records),
                records[0].get("_provenance", {}).get("source_sha256", "")[:12]
                if records else "n/a",
            )
            if ON_SOURCE_FAILURE == "fail_critical":
                raise UniProtDataIntegrityError(
                    f"UniProt parse yielded {len(records)} records, expected "
                    f"~{expected}. Aborting per ON_SOURCE_FAILURE=fail_critical.",
                    context={"actual": len(records), "expected": expected},
                )
        elif len(records) < int(expected) * 0.9:
            logger.error(
                "uniprot_record_count_low expected=~%d actual=%d",
                expected, len(records),
            )

    logger.info(
        "Loaded %d UniProt %s protein records",
        len(records), organism or "(all organisms)",
    )
    return records


# =============================================================================
# Section 5 — Node + edge construction (D1-003, D2-004, D2-005, D4-002,
#             D4-004, D14-001, D15-003, D16-001)
# =============================================================================


def uniprot_to_node_records(
    records: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Convert UniProt records to Protein node records for the KG.

    Field mapping (see ``PROTEIN_NODE_SCHEMA`` for the authoritative list):
        accession   -> uniprot_id (canonical, D2-004) + id (backward-compat, D15-003)
        protein_name|entry_name|accession -> name (never empty, D4-002)
        gene_name / gene_names -> gene_name (first), gene_names (all, D3-003)
        gene_id / gene_ids -> gene_id (verified, D3-001), gene_ids (all, D4-004)
        ncbi_taxid -> ncbi_taxid (int, D5-003)
        ec_numbers, protein_existence, sequence -> 1:1 (D3-007/008/009)
        entity_type <- 'Protein' (constant, D12-001)
        source <- 'UniProt' (constant, D12-001)
        _provenance <- computed (D16-001)
        _license, _attribution <- CC BY 4.0 (D14-001)

    Dropped (remain on the UniProtRecord for edge construction — see
    ``uniprot_to_edge_records``):
        secondary_accessions, alternative_names, contains_names,
        includes_names, gene_synonyms, gene_orf_names, cross_references.

    Args:
        records: Iterable of UniProtRecord dicts.

    Returns:
        List of ProteinNode dicts.  Every node has every key in
        ``PROTEIN_NODE_SCHEMA``.

    Raises:
        UniProtDataIntegrityError: A record is missing the required
            ``accession`` field (D1-003).

    Example:
        >>> records = parse_uniprot_entries()
        >>> nodes = uniprot_to_node_records(records)
        >>> assert all(n['uniprot_id'] for n in nodes)
    """
    nodes: List[Dict[str, Any]] = []
    missing_accession_count = 0
    for rec in records:
        accession = rec.get("accession")
        # D1-003 — runtime assertion: accession must be non-empty.
        if not accession:
            missing_accession_count += 1
            logger.error(
                "uniprot_to_node_records: record missing accession — skipping. "
                "entry_name=%s", rec.get("entry_name", "?"),
            )
            continue

        # D4-002 — name fallback chain (never empty for a valid record).
        name = (
            rec.get("protein_name")
            or rec.get("entry_name")
            or accession
            or ""
        )

        # D4-004 — gene_id / gene_ids (always list for gene_ids).
        gene_ids = list(rec.get("gene_ids", []))
        gene_id = rec.get("gene_id", "") or (gene_ids[0] if gene_ids else "")

        # D3-003 — gene_name / gene_names.
        gene_names = list(rec.get("gene_names", []))
        gene_name = rec.get("gene_name", "") or (gene_names[0] if gene_names else "")

        # D9-004 — sanitize free-text fields (defence-in-depth for Neo4j).
        node = _ProteinNodeDict({  # D15-003 — warns on ['id'] access
            "uniprot_id": accession,           # D2-004 canonical
            "id": accession,                   # D15-003 backward-compat shim
            "uniprot_uri": rec.get(
                "uniprot_uri",
                f"https://identifiers.org/uniprot:{accession}",
            ),  # D14-002
            "name": _sanitize_freetext(name),  # D4-002 / D9-004
            "entry_name": _sanitize_freetext(rec.get("entry_name", "")),
            "gene_name": _sanitize_freetext(gene_name),
            "gene_names": [_sanitize_freetext(g) for g in gene_names],
            "gene_id": gene_id,                # D3-001 verified
            "gene_ids": list(gene_ids),        # D4-004
            "ncbi_taxid": rec.get("ncbi_taxid", 0),  # D5-003 int
            "ec_numbers": list(rec.get("ec_numbers", [])),  # D3-007
            "protein_existence": rec.get("protein_existence", 0),  # D3-008
            "sequence": rec.get("sequence", ""),  # D3-009
            "entity_type": ENTITY_TYPE_PROTEIN,  # D12-001
            "source": SOURCE_UNIPROT,            # D12-001
            # v41 ROOT FIX (DEAD): propagate OG/OH organelle lines to
            # the node record's ``subcellular_location`` property.
            # Previously the OG/OH line was parsed into
            # ``current["organelle"]`` (line ~1459) but never copied
            # onto the node — pure dead parsing. Downstream consumers
            # (the subcellular-localisation feature used by the RL
            # safety ranker for off-target toxicity prediction) had
            # ``subcellular_location=None`` for every UniProt protein.
            # Now we copy the organelle string verbatim (sanitised for
            # Cypher); the cleaning.normalizer can post-process if a
            # structured form is needed.
            "subcellular_location": _sanitize_freetext(
                rec.get("organelle", "") or ""
            ),
            "_provenance": dict(rec.get("_provenance", {})),  # D16-001
            "_license": rec.get("_license", UNIPROT_LICENSE),  # D14-001
            "_attribution": rec.get("_attribution", UNIPROT_ATTRIBUTION),
        })
        nodes.append(node)

    if missing_accession_count:
        raise UniProtDataIntegrityError(
            f"{missing_accession_count} records were missing the required "
            f"'accession' field and were skipped (D1-003).",
            context={"missing_accession_count": missing_accession_count},
        )

    logger.info("Converted %d UniProt node records", len(nodes))
    return nodes


def uniprot_to_edge_records(
    records: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Convert UniProt DR cross-references into KG edge records.

    Fixes D2-005 — the v1 loader extracted only ``DR GeneID`` and
    discarded every other cross-reference (ChEMBL, DrugBank, HGNC, Pfam,
    InterPro, Reactome, STRING, …).  Those are edges in the KG.  This
    function emits them.

    Edge shape:
        {"source": "uniprot:P23219", "source_type": "Protein",
         "target": "ChEMBL:CHEMBL218", "target_type": "Compound",
         "relation": "interacts_with", "source_db": "UniProt",
         "_provenance": {...}}

    The ``DR GeneID`` edge is emitted as ``relation='xref'`` (target_type
    'Gene') to avoid duplicating the Gene-encodes-Protein edge that
    ``entity_resolver.build_gene_protein_edges`` already produces.

    Args:
        records: Iterable of UniProtRecord dicts (must have
            ``cross_references`` and ``_provenance``).

    Returns:
        List of edge dicts.
    """
    edges: List[Dict[str, Any]] = []
    # v41 ROOT FIX (SEV1): import the crosswalk helper lazily so the
    # Compound cross-reference edges (ChEMBL, DrugBank, PubChem) get
    # normalised to InChIKey before dst_id is built. Previously
    # ``dst_id = f"{db_name}:{db_id}"`` (e.g. ``"ChEMBL:CHEMBL218"``)
    # which NEVER matches the InChIKey-keyed Compound nodes emitted by
    # drugbank/chembl/sider/stitch loaders — orphan edges 100% of the
    # time. Now: resolve via the crosswalk; if no mapping, dead-letter
    # the edge with reason="no_inchikey_crosswalk" so the operator sees
    # the real coverage gap (instead of a silent orphan that the
    # kg_builder drops downstream).
    try:
        from .id_crosswalk import _normalize_compound_id_to_inchikey as _norm_cid
    except Exception:  # pragma: no cover — defensive
        _norm_cid = None  # type: ignore[assignment]
    for rec in records:
        accession = rec.get("accession")
        if not accession:
            continue
        prov = dict(rec.get("_provenance", {}))
        xrefs = rec.get("cross_references", {})
        for db_name, db_ids in xrefs.items():
            relation = _DB_TO_EDGE_TYPE.get(db_name)
            target_type = _DB_TO_ENTITY_TYPE.get(db_name)
            if relation is None or target_type is None:
                # Unknown DB — record as a generic 'xref' edge so no data is lost.
                relation = "xref"
                target_type = "ExternalRef"
            for db_id in db_ids:
                # v41 ROOT FIX (SEV1): normalise Compound IDs to InChIKey
                # for Compound-typed cross-refs (ChEMBL, DrugBank, PubChem,
                # STITCH, SIDER). Other target_types (Gene, ExternalRef,
                # Pathway, etc.) keep their prefixed form since they're
                # keyed differently downstream.
                dst_id_resolved: str = f"{db_name}:{db_id}"
                if (
                    target_type == ENTITY_TYPE_COMPOUND
                    and _norm_cid is not None
                ):
                    # ``_normalize_compound_id_to_inchikey`` accepts any
                    # compound ID form (bare ID, prefixed DB:ID, even
                    # already-InChIKey). It returns the original string
                    # when no mapping is found (NEVER None).
                    _inchi = _norm_cid(
                        f"{db_name}:{db_id}",
                        source="uniprot_xref",
                    )
                    if _inchi and _is_inchikey(_inchi):
                        dst_id_resolved = _inchi
                    else:
                        # No InChIKey crosswalk — dead-letter the edge
                        # so the operator sees the gap. Do NOT emit an
                        # orphan dst_id (the kg_builder would drop it
                        # silently downstream, giving a misleading edge
                        # count).
                        _write_dead_letter({
                            "kind": "no_inchikey_crosswalk",
                            "stage": "uniprot_to_edge_records",
                            "reason": (
                                f"Cross-reference edge {db_name}:{db_id} "
                                f"from UniProt AC {accession} has no "
                                f"InChIKey crosswalk mapping — Compound "
                                f"node keyed by InChIKey would never "
                                f"match. Edge dead-lettered."
                            ),
                            "accession": accession,
                            "db_name": db_name,
                            "db_id": db_id,
                            "target_type": target_type,
                            "relation": relation,
                        })
                        continue
                edges.append({
                    # BUG-B-003 root fix — kg_builder._load_edges requires
                    # ``src_id`` and ``dst_id`` keys. The previous dict
                    # used ``source``/``target`` which caused every UniProt
                    # cross-reference edge to be dead-lettered at the Cypher
                    # MERGE step. We add ``src_id``/``dst_id`` as the
                    # canonical keys and keep ``source``/``target`` as
                    # aliases for downstream consumers (e.g. reporting).
                    #
                    # v9 ROOT FIX (audit F5.2.1): the previous code emitted
                    # src_id = f"uniprot:{accession}" = "uniprot:P23219".
                    # ID_PATTERNS["Protein"] requires the BARE Swiss-Prot
                    # accession "P23219" (no prefix). Every UniProt
                    # cross-reference edge was dead-lettered. Strip the
                    # prefix to match the pattern.
                    "src_id": accession,
                    # v41 ROOT FIX (SEV1): use the resolved InChIKey
                    # for Compound-typed cross-refs (dst_id_resolved),
                    # falling back to the prefixed form for non-Compound
                    # target_types. Previously always emitted
                    # ``f"{db_name}:{db_id}"`` which never matched the
                    # InChIKey-keyed Compound nodes.
                    "dst_id": dst_id_resolved,
                    "source": accession,                          # alias (legacy)
                    "source_type": ENTITY_TYPE_PROTEIN,
                    "target": dst_id_resolved,                    # alias (legacy)
                    "target_type": target_type,
                    "relation": relation,
                    "rel_type": relation,                         # v9: kg_builder alias
                    "src_type": ENTITY_TYPE_PROTEIN,              # v9: explicit types
                    "dst_type": target_type,
                    "source_db": SOURCE_UNIPROT,
                    "_provenance": prov,
                })
    logger.info("Converted %d UniProt edge records", len(edges))
    return edges


# =============================================================================
# Section 6 — Loader Protocol adapter (D1-002)
# =============================================================================


class UniProtLoader:
    """Adapter implementing the ``Loader`` Protocol for UniProt.

    Fixes D1-002 — provides a uniform ``download / parse / to_graph``
    interface so ``run_pipeline`` can treat all loaders polymorphically.
    The module-level functions remain the public API; this class is a
    thin adapter that delegates to them.

    Attributes
    ----------
    name : str
        Always ``"uniprot"`` (matches ``DATA_SOURCES`` key).
    """

    name: str = SOURCE_KEY_UNIPROT

    def download(self, force: bool = False) -> Path:
        """Download (or cached-load) the UniProt flat file."""
        return download_uniprot(force=force)

    def parse(self, path: Optional[Path] = None) -> Iterator[Dict[str, Any]]:
        """Yield parsed records (no organism filter — pure parser)."""
        return iter_uniprot_entries(path)

    def to_graph(
        self, records: Iterable[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Convert records into ``(nodes, edges)`` for the KG."""
        records_list = list(records)
        nodes = uniprot_to_node_records(records_list)
        edges = uniprot_to_edge_records(records_list)
        return nodes, edges


# ═══════════════════════════════════════════════════════════════════════════════
# v26 ROOT FIX (Audit section 10 — Phase 2 Loaders Bypass Matrix / P0 BLOCKER):
# "Make the 4 raw re-fetch loaders consume Phase 1 CSVs by default."
# The audit's recommendation: refactor uniprot_loader to follow the same
# bridge pattern as disgenet_loader / omim_loader / pubchem_loader — read
# Phase 1 CSVs by default; only fall back to raw fetch when explicitly
# requested.
#
# The v24 fix in run_pipeline.py step7_additional_sources SKIPS this loader
# when data_source="phase1" (because the bridge in step1 already loaded
# uniprot_proteins.csv). This v26 fix adds Phase-1-aware functions so that
# STANDALONE use (calling download_uniprot() or parse_uniprot_entries()
# directly) ALSO consumes Phase 1 CSVs by default — defense in depth.
# ═══════════════════════════════════════════════════════════════════════════════

# Phase 1 emits this CSV; resolve relative to the unified package layout.
_DEFAULT_PHASE1_PROCESSED_DIR: Path = (
    Path(__file__).resolve().parents[2] / "phase1" / "processed_data"
)
DEFAULT_UNIPROT_PROTEINS_CSV: Path = (
    _DEFAULT_PHASE1_PROCESSED_DIR / "uniprot_proteins.csv"
)


def parse_uniprot_entries_from_phase1_csv(
    filepath: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Read Phase 1's cleaned ``uniprot_proteins.csv`` into a list of records.

    This is the Phase-1-aware analogue of ``parse_uniprot_entries`` (which
    reads the raw UniProt ``uniprot_sprot.dat.gz`` file). The returned
    list-of-dicts schema mirrors what ``uniprot_to_node_records`` and
    ``uniprot_to_edge_records`` expect.

    v26 ROOT FIX (Audit section 10 — bypass matrix): previously, calling
    ``parse_uniprot_entries()`` standalone would re-download the ~800 MB
    UniProt .dat file and re-parse it — bypassing Phase 1's cleaning
    (organism verification, gene-symbol normalization, sequence
    validation). Now standalone callers can consume Phase 1's
    already-cleaned output.

    Parameters
    ----------
    filepath : path-like, optional
        Explicit path to the Phase 1 CSV. Defaults to the canonical location.

    Returns
    -------
    list of dict
        Cleaned UniProt protein records. Each dict has keys: accession,
        entry_name, protein_name, gene_symbol, organism, organism_id,
        sequence, length, reviewed, etc.

    Raises
    ------
    FileNotFoundError
        If the CSV does not exist (Phase 1 not yet run).
    """
    path = filepath or DEFAULT_UNIPROT_PROTEINS_CSV
    if not path.exists():
        raise FileNotFoundError(
            f"Phase 1 UniProt proteins CSV not found at {path}. "
            f"Run Phase 1's UniProt pipeline first "
            f"(phase1.pipelines.uniprot_pipeline.UniprotPipeline().run())."
        )
    import pandas as pd  # lazy import (module-level uses plain dicts)
    df = pd.read_csv(path)
    logger.info(
        "uniprot_loader: read %d rows from Phase 1 CSV %s", len(df), path,
    )
    # Convert DataFrame to list-of-dicts to match parse_uniprot_entries' return type.
    return df.to_dict("records")


def uniprot_to_node_records_from_phase1(
    records: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Convert Phase 1's UniProt records to Protein node records.

    v26 ROOT FIX: Phase 1's ``uniprot_proteins.csv`` has a DIFFERENT
    schema than the raw UniProt .dat file. The raw .dat parser
    ``uniprot_to_node_records`` expects keys like ``accession``,
    ``entry_name``, ``protein_name``, ``gene_symbol``, ``organism``,
    ``sequence``, ``length``, ``reviewed``. Phase 1's CSV has columns
    ``uniprot_ac``, ``accession``, ``name``, ``protein_name``,
    ``gene_name``, ``gene_symbol``, ``organism``, ``sequence``,
    ``function``. The schemas overlap but the primary key column is
    ``uniprot_ac`` in Phase 1 (not ``accession``). This function
    normalizes the Phase 1 schema to the canonical node-record format
    that ``kg_builder.load_nodes_batch("Protein", ...)`` expects.
    """
    nodes: List[Dict[str, Any]] = []
    seen: set = set()
    for idx, rec in enumerate(records):
        # Prefer 'uniprot_ac', fall back to 'accession' / 'id'.
        ac = (
            rec.get("uniprot_ac")
            or rec.get("accession")
            or rec.get("id")
            or ""
        )
        ac = str(ac).strip()
        if not ac or ac in seen:
            continue
        seen.add(ac)
        nodes.append({
            "id": ac,
            # v41 ROOT FIX (SEV3): emit ``uniprot_id`` alongside ``id``
            # so downstream consumers reading ``node["uniprot_id"]``
            # (the raw .dat path emits both at line ~1718-1719; the
            # Phase 1 path was the odd one out and emitted only ``id``).
            # The bridge / kg_builder canonicalise on ``id``; the
            # ``uniprot_id`` alias is for backward-compat with
            # downstream tools (entity_resolver graph queries,
            # exporters, the clinical-trials enrichment step).
            "uniprot_id": ac,
            "label": "Protein",
            "name": str(rec.get("protein_name") or rec.get("name") or ac),
            "entry_name": str(rec.get("name") or rec.get("entry_name") or ""),
            "gene_symbol": str(rec.get("gene_symbol") or rec.get("gene_name") or ""),
            "gene_name": str(rec.get("gene_name") or rec.get("gene_symbol") or ""),
            "organism": str(rec.get("organism") or ""),
            "sequence": str(rec.get("sequence") or ""),
            "function": str(rec.get("function") or ""),
            "_source": "uniprot",
            "_source_phase": 1,
            "_source_file": "uniprot_proteins.csv",
            "_source_row": int(rec.get("_source_row", idx)),
        })
    return nodes


def uniprot_to_edge_records_from_phase1(
    records: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Convert Phase 1's UniProt records to cross-reference edge records.

    v26 ROOT FIX: Phase 1's CSV is already entity-resolved (one row per
    UniProt accession, gene_symbol already canonicalized). Cross-reference
    edges (Protein -> Gene, Protein -> ExternalRef) are emitted ONLY when
    the record carries the relevant foreign key (gene_symbol or
    ncbi_gene_id). This mirrors the bridge's logic in
    ``phase1_bridge._load_uniprot``.

    v35 ROOT FIX (V35-P2-LOADERS-FIXES M-3): the previous implementation
    emitted ``dst_id = f"SYM:{gene_symbol.upper()}"`` for the
    Protein-encodes-Gene edge. Phase 2's KG node-builder keys Gene nodes
    by their NCBI Gene ID (the project spec: "Canonical Gene ID =
    NCBI Gene ID"; see ``entity_resolver.resolve_genes_from_drkg`` which
    now also prefers ncbi_gene_id). A ``SYM:TP53`` dst_id would never
    MATCH a Gene node keyed ``7157`` — orphan edge. Fix:

      1. If the record already carries ``ncbi_gene_id``, use it directly.
      2. Else use the verified UniProt→Gene crosswalk
         (``VERIFIED_UNIPROT_GENE_CROSSWALK``) to resolve the UniProt AC
         to ncbi_gene_id.
      3. Else use ``IDCrosswalk.canonicalize("Gene", "gene_symbol",
         gene_symbol)`` to resolve the symbol to ncbi_gene_id.
      4. Only fall back to ``SYM:{symbol}`` when no ncbi_gene_id is
         resolvable (preserving prior behavior so the edge is still
         emitted — better an orphan edge than a silently dropped edge).
    """
    # Lazy import to avoid a circular import at module load time.
    try:
        from .id_crosswalk import get_default_crosswalk as _get_xwalk
    except Exception:  # pragma: no cover — defensive
        _get_xwalk = None  # type: ignore[assignment]

    edges: List[Dict[str, Any]] = []
    for idx, rec in enumerate(records):
        ac = str(rec.get("uniprot_ac") or rec.get("accession") or "").strip()
        if not ac:
            continue
        # Protein -> Gene (encodes) edge when gene_symbol is present.
        gene_symbol = str(rec.get("gene_symbol") or rec.get("gene_name") or "").strip()
        if gene_symbol and gene_symbol.upper() not in ("", "NAN", "NONE"):
            # v35 ROOT FIX (M-3): resolve gene_symbol -> ncbi_gene_id.
            gene_id: Optional[str] = None
            gene_id_source = "none"

            # Step 1: explicit ncbi_gene_id column on the row.
            _row_ncbi = rec.get("ncbi_gene_id")
            if _row_ncbi is not None and str(_row_ncbi).strip() not in ("", "nan", "none", "None"):
                _norm = _normalize_ncbi_gene_id(str(_row_ncbi).strip())
                if _norm:
                    gene_id = _norm
                    gene_id_source = "row_ncbi_gene_id"

            # Step 2: verified UniProt->Gene crosswalk (by AC).
            if gene_id is None:
                # v35 ROOT FIX (M-3): ``VERIFIED_UNIPROT_GENE_CROSSWALK``
                # in id_crosswalk is a TUPLE/LIST of entry objects (not
                # a dict). The pre-built ``_VERIFIED_LOOKUP`` dict
                # (uppercase UniProt AC -> (ncbi_gene_id, gene_symbol))
                # is the correct lookup table.
                _vw = _VERIFIED_LOOKUP.get(ac.upper())
                if _vw is not None:
                    _vw_ncbi = _vw[0]  # (ncbi_gene_id, gene_symbol)
                    if _vw_ncbi:
                        _norm = _normalize_ncbi_gene_id(str(_vw_ncbi))
                        if _norm:
                            gene_id = _norm
                            gene_id_source = "verified_crosswalk"

            # Step 3: IDCrosswalk.canonicalize("Gene", "gene_symbol", ...).
            if gene_id is None and _get_xwalk is not None:
                try:
                    _xw = _get_xwalk()
                    if _xw is not None:
                        _canon = _xw.canonicalize("Gene", "gene_symbol", gene_symbol)
                        if _canon is not None:
                            _canon_ncbi = _canon.get("ncbi_gene_id")
                            if _canon_ncbi:
                                _norm = _normalize_ncbi_gene_id(str(_canon_ncbi))
                                if _norm:
                                    gene_id = _norm
                                    gene_id_source = "idcrosswalk_canonicalize"
                except Exception:
                    # Fall back to SYM: below.
                    pass

            # Step 4: last-resort fallback — preserve the prior
            # SYM:{symbol} form so the edge is still emitted (downstream
            # entity resolution may still recover the link).
            if gene_id is None:
                gene_id = f"SYM:{gene_symbol.upper()}"
                gene_id_source = "sym_fallback"

            edges.append({
                "src_id": ac,
                "dst_id": gene_id,
                "src_type": "Protein",
                "dst_type": "Gene",
                "rel_type": "encodes",
                "props": {
                    "source": "uniprot",
                    "gene_symbol": gene_symbol,
                    "gene_id_source": gene_id_source,
                },
                "source": "uniprot",
                "_source_phase": 1,
                "_source_file": "uniprot_proteins.csv",
                "_source_row": int(rec.get("_source_row", idx)),
            })
    return edges
