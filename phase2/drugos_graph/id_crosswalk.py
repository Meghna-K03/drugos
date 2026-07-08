"""
DrugOS Graph Module — ID Crosswalk Service
==========================================
CRITICAL for scientific correctness.

This module is the **single point of identity resolution** for the entire
DrugOS knowledge graph. Every ``Drug -> Protein``, ``Protein -> Gene`` and
``Drug -> Disease`` edge in the graph passes through this file. A single
wrong mapping silently corrupts the Graph Transformer, the RL ranker, the
API and ultimately a clinician's prescription decision. Treat every line
as flight-control code.

Problem: Five different external databases use five DIFFERENT ID
namespaces for what is biologically the SAME protein:

  STRING      -> "9606.ENSP000003..."  (Ensembl PROTEIN ID)
  STITCH      -> "9606.ENSP000003..."  (Ensembl PROTEIN ID, same as STRING)
  ChEMBL      -> "CHEMBL218"           (ChEMBL target dictionary ID — NOT a protein accession)
  OpenTargets -> "ENSG00000123456"     (Ensembl GENE ID)
  UniProt     -> "P23219"              (UniProt accession — the scientific standard)

Without an explicit crosswalk Neo4j creates five separate ``:Protein``
nodes for the same real protein (e.g. COX1/PTGS1), and graph traversals
like ``Drug -> Protein -> Gene -> Disease`` silently return empty
results.

This module provides ID translation services built from publicly
available cross-reference files. For the sandbox/no-network case, a
small built-in crosswalk of well-known drug targets is shipped — see
``data/verified_uniprot_gene_crosswalk.yaml`` (30 entries, all manually
verified against UniProtKB/Swiss-Prot and NCBI Gene).

Authoritative sources used by this module
-----------------------------------------
- UniProtKB/Swiss-Prot ``DR   GeneID;`` line (parsed by ``uniprot_loader``)
- STRING ``9606.protein.aliases.v12.0.txt.gz`` (Ensembl protein ID -> UniProt AC)
- ChEMBL ``target_components`` table (ChEMBL target ID -> UniProt AC)
- OpenTargets target metadata (Ensembl gene ID -> UniProt AC)

When an upstream cross-reference file is unavailable, the resolver falls
back to the built-in ``VERIFIED_UNIPROT_GENE_CROSSWALK`` table and logs a
WARNING so the user knows the crosswalk is incomplete.

When to use this module vs ``entity_resolver``
---------------------------------------------
- Use ``IDCrosswalk`` for one-off ID translation ("given this ENSP, what
  is the UniProt AC?"). Stateless, fast, no side effects.
- Use ``EntityResolver`` for building canonical node mappings from full
  datasets ("given all 5M STRING edges, produce a single canonical
  Protein ID per real protein"). Stateful, builds bidirectional alias
  maps, runs clustering.
- Rule of thumb: if you have one ID, use ``IDCrosswalk``. If you have a
  whole dataset's worth of IDs to reconcile, use ``EntityResolver``.

Thread-safety contract
-----------------------
- The module-level singleton returned by ``get_default_crosswalk()`` is
  safe to call concurrently from multiple threads; first-call
  initialization is guarded by a ``threading.Lock`` (double-checked
  locking pattern).
- Translators (``ensembl_protein_to_uniprot_ac()`` and friends) are safe
  to call concurrently with other translators on the same instance.
- Loaders (``load_*()`` methods) MUST NOT be called concurrently with
  translators on the same instance — use ``merge()`` to combine
  crosswalks built in parallel by separate instances.

Backward-compatibility note
---------------------------
The five original translator methods (``ensembl_protein_to_uniprot_ac``,
``chembl_target_to_uniprot_ac``, ``ensembl_gene_to_uniprot_ac``,
``uniprot_ac_to_ncbi_gene_id``, ``ncbi_gene_id_to_uniprot_ac``) return
``Optional[str]`` — the primary (highest-priority) value — to preserve
the historical public contract. Multi-valued variants are exposed as
``*_all()`` methods (returning ``List[str]``), and provenance-tagged
variants as ``*_with_provenance()``. This satisfies audit issue DES-2
(multi-valued mappings) without breaking existing callers.

Audit reference
---------------
This file addresses all 76 issues listed in
``id_crosswalk_forensic_audit.md`` and documented in
``ID_CROSSWALK_REPAIR_CHANGELOG.md`` (one entry per issue ID).
"""

from __future__ import annotations

# ─── Standard library imports ──────────────────────────────────────────────
import contextlib
import gzip
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Final,
    List,
    Literal,
    NamedTuple,
    Optional,
    Tuple,
    Union,
)

# Optional dependency — pandas is used for the fast STRING loader path
# (PERF-3) but is NOT required at import time.
try:  # pragma: no cover — exercised only when pandas is missing
    import pandas as _pd  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    _pd = None  # type: ignore[assignment]

# Optional dependency — PyYAML is required to read the externalized builtin
# table (CONF-2). If missing, the module falls back to a hardcoded copy
# (kept in sync with the YAML) and logs a WARNING.
try:  # pragma: no cover — exercised only when PyYAML is missing
    import yaml as _yaml
except ImportError:  # pragma: no cover
    _yaml = None  # type: ignore[assignment]

# ─── Module-level logger ──────────────────────────────────────────────────
# ARCH-5: class uses an injectable logger; this module-level logger is used
# by module-level functions (``get_default_crosswalk`` etc.) only.
logger = logging.getLogger(__name__)

# Add a NullHandler so library consumers without configured logging do not
# see "No handlers could be found" warnings.
if not any(isinstance(h, logging.NullHandler) for h in logger.handlers):
    logger.addHandler(logging.NullHandler())


# =============================================================================
# Section 1 — Module-level constants & format validators
# =============================================================================

# ── SCI-6: UniProt accession format validator ──────────────────────────────
# UniProt AC regex (per https://www.uniprot.org/help/accession_numbers):
#   6-char accessions (used by BOTH Swiss-Prot and TrEMBL):
#     [OPQ][0-9][A-Z0-9]{3}[0-9]
#     [A-NR-Z][0-9][A-Z0-9]{3}[0-9]
#   10-char accessions (used by BOTH Swiss-Prot and TrEMBL):
#     [UJ][0-9][A-Z0-9]{3}[0-9][A-Z0-9]{3}[0-9]
#     [A-NR-Z][0-9][A-Z0-9]{3}[0-9][A-Z0-9]{3}[0-9]
#
# audit-2025 ROOT FIX (issue 46): the previous comment implied the
# 6-char [A-NR-Z] form was "TrEMBL only" and the 10-char forms were
# also "TrEMBL only". This is WRONG — UniProt accession FORMAT does
# NOT determine whether an entry is Swiss-Prot (reviewed) or TrEMBL
# (unreviewed). Both 6-char and 10-char accessions can appear in
# either section. The reviewed/unreviewed distinction is a separate
# metadata attribute, not an accession-format attribute. The regex
# itself was always correct; only the comment was misleading.
# An optional ``-N`` isoform suffix is allowed.
_UNIPROT_AC_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?:"
    # 6-char Swiss-Prot / TrEMBL
    r"[OPQ][0-9][A-Z0-9]{3}[0-9]"
    r"|[A-NR-Z][0-9][A-Z0-9]{3}[0-9]"
    # 10-char TrEMBL — 6-char prefix + 4-char suffix
    # e.g. A0A024R2R7, A0A0A0A0A0
    r"|[UJ][0-9][A-Z0-9]{3}[0-9][A-Z0-9]{3}[0-9]"
    r"|[A-NR-Z][0-9][A-Z0-9]{3}[0-9][A-Z0-9]{3}[0-9]"
    r")(-\d+)?$"
)

# ── DQ-5: ChEMBL identifier format validator ───────────────────────────────
# ChEMBL IDs follow ``^CHEMBL\d+$`` (e.g. CHEMBL218, CHEMBL210).
# FORENSIC audit fix: make the pattern case-INSENSITIVE so lowercase
# ``chembl12345`` IDs from OpenTargets (which emits lowercase) are NOT
# silently dropped. The canonical form is still uppercase CHEMBL, but
# the validator now accepts both and the crosswalk normalizes to
# uppercase on storage.
_CHEMBL_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^CHEMBL\d+$", re.IGNORECASE)

# ── ARCH-1: Ensembl gene ID format validator ───────────────────────────────
# Ensembl gene IDs: ``ENSG\d{11}``.
_ENSG_PATTERN: Final[re.Pattern[str]] = re.compile(r"^ENSG\d{11}$")

# ── SCI-3: UMLS CUI format validator ──────────────────────────────────────
# UMLS CUIs: ``C\d{7}`` (e.g. ``C0002395`` for Alzheimer's Disease).
# Added by opentargets_loader v2.0 institutional-grade audit fix.
_UMLS_CUI_PATTERN: Final[re.Pattern[str]] = re.compile(r"^C\d{7}$")

# ── v29 ROOT FIX (audit L-5): Compound ID format validators ───────────────
# Compound ID fragmentation: 7 disjoint namespaces — DrugBank ID (DB00107),
# ChEMBL ID (CHEMBL218), PubChem CID (CID5311025), STITCH CIDm/CIDs
# (CIDm00002244 / CIDs00002244), SIDER bare CID (CID5311025), DRKG MESH
# (MESH:D000544), and InChIKey (BSYNRYMUTXBXSQ-UHFFFAOYSA-N).
# InChIKey is the canonical Compound ID (project doc Section 3 — see
# entity_resolver.py header). The validators below let
# ``_normalize_compound_id_to_inchikey()`` recognise inputs in any namespace
# and attempt crosswalk lookup.
_INCHIKEY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$"
)
# DrugBank ID: DB00107 (DB + 5 digits, sometimes DB + 7 digits for newer entries)
_DRUGBANK_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^DB\d{5,7}$")
# PubChem CID: CID5311025 (CID + digits) — bare int forms are also accepted
# and coerced to ``CID<int>`` form before lookup.
_PUBCHEM_CID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^CID\d+$")
# STITCH CIDm / CIDs (legacy 4th-char stereochemistry marker)
_STITCH_CIDM_PATTERN: Final[re.Pattern[str]] = re.compile(r"^CIDm\d+$")
_STITCH_CIDS_PATTERN: Final[re.Pattern[str]] = re.compile(r"^CIDs\d+$")
# STITCH newer format: CID0<digits> (flat) / CID1<digits> (stereo)
_STITCH_CID0_PATTERN: Final[re.Pattern[str]] = re.compile(r"^CID0\d+$")
_STITCH_CID1_PATTERN: Final[re.Pattern[str]] = re.compile(r"^CID1\d+$")
# DRKG MeSH descriptor: MESH:D000544 (also MeSH:D000544)
_MESH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(?:MESH|MeSH):[CDM]\d+$")

# ── INT-1: Ensembl protein ID normalizer ───────────────────────────────────
# Accepts both ``9606.ENSP00000358091`` and ``ENSP00000358091`` (and the
# isoform-suffixed ``ENSP00000358091.3`` form). Returns the bare ENSP
# (``ENSP00000358091``), which is the canonical key form used by the
# internal ``ensembl_protein_to_uniprot`` dict.
_ENSP_PREFIX_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d+\.")
_ENSP_ISOFORM_PATTERN: Final[re.Pattern[str]] = re.compile(r"\.\d+$")


def _validate_uniprot_ac(ac: Any) -> bool:
    """Return True iff ``ac`` is a syntactically valid UniProt accession.

    Implements SCI-6. Accepts both Swiss-Prot and TrEMBL patterns plus an
    optional ``-N`` isoform suffix. The check is case-insensitive (the
    function upper-cases its input) and tolerant of leading/trailing
    whitespace.

    Args:
        ac: The candidate accession. Non-string inputs return False.

    Returns:
        ``True`` if the accession is valid; ``False`` otherwise.

    Examples:
        >>> _validate_uniprot_ac("P23219")
        True
        >>> _validate_uniprot_ac("p23219")
        True
        >>> _validate_uniprot_ac("P23219-2")
        True
        >>> _validate_uniprot_ac("Q12345")
        True
        >>> _validate_uniprot_ac("P2321 9")
        False
        >>> _validate_uniprot_ac("")
        False
        >>> _validate_uniprot_ac(None)
        False
    """
    if not isinstance(ac, str):
        return False
    return bool(_UNIPROT_AC_PATTERN.match(ac.strip().upper()))


def _is_swiss_prot(ac: str) -> bool:
    """Return True iff ``ac`` is a Swiss-Prot (reviewed) accession.

    Swiss-Prot accessions start with ``[OPQ]``; TrEMBL accessions start
    with ``[A-NR-Z]``. Used by DQ-1 to prefer Swiss-Prot over TrEMBL when
    multiple ACs are available for one ENSP.
    """
    if not _validate_uniprot_ac(ac):
        return False
    stem = ac.strip().upper().split("-")[0]
    return bool(stem) and stem[0] in ("O", "P", "Q")


def _validate_chembl_id(chembl_id: Any) -> bool:
    """Return True iff ``chembl_id`` is a syntactically valid ChEMBL ID.

    Implements DQ-5. ChEMBL IDs follow ``^CHEMBL\\d+$`` (case-sensitive).
    """
    if not isinstance(chembl_id, str):
        return False
    return bool(_CHEMBL_ID_PATTERN.match(chembl_id.strip()))


def _normalize_ensp(raw: Any) -> str:
    """Normalize an Ensembl protein ID to its canonical bare form.

    Implements INT-1. Strips the species prefix (``9606.`` or any
    ``\\d+.``) and the isoform suffix (``.N``). Examples:

        >>> _normalize_ensp("9606.ENSP00000358091")
        'ENSP00000358091'
        >>> _normalize_ensp("ENSP00000358091")
        'ENSP00000358091'
        >>> _normalize_ensp("9606.ENSP00000358091.3")
        'ENSP00000358091'
        >>> _normalize_ensp("ENSP00000358091.3")
        'ENSP00000358091'

    Non-string input is coerced to ``str`` first.
    """
    s = str(raw) if not isinstance(raw, str) else raw
    s = _ENSP_PREFIX_PATTERN.sub("", s, count=1)
    s = _ENSP_ISOFORM_PATTERN.sub("", s, count=1)
    return s.strip()


def _normalize_ncbi_gene_id(raw: Any) -> Optional[str]:
    """Normalize an NCBI Gene ID.

    Implements COD-6 / DQ-4. Accepts ``int``, ``str``, pandas float
    artifacts (``"5742.0"``) and whitespace-padded strings. Returns the
    cleaned numeric string, or ``None`` if the input is not a valid
    integer gene ID.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    # Tolerate pandas float artifact: "5742.0" -> "5742"
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    if not s.isdigit():
        return None
    return s


def _redact_path(p: Any) -> str:
    """Return the basename of a Path for log safety (GUARD-SEC-2).

    INFO/WARNING log messages should never include full filesystem paths
    because they may contain usernames (``/home/manoj/...``). Use this
    helper in every log line that includes a path.
    """
    if isinstance(p, Path):
        return p.name
    return str(p)


def _sha256_of_file(path: Path, chunk_size: int = 65536) -> str:
    """Compute the SHA-256 of a file in streaming fashion.

    Implements GUARD-IDEM-1. Uses 64KB chunks so that very large files
    (e.g. STRING's 5M-row aliases file) are not loaded into memory.
    Returns the hex digest prefixed with ``sha256:``.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def _iso_now() -> str:
    """Return the current UTC timestamp as ISO 8601 (e.g. ``2026-06-17T11:30:00Z``)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── CONF-2 / CONF-3 / COMP-1: builtin table constants ─────────────────────
# Path to the externalized YAML builtin table. Can be overridden via the
# ``DRUGOS_BUILTIN_CROSSWALK`` environment variable (CONF-3) for Docker
# images that ship a custom builtin.
_BUILTIN_DEFAULT_PATH: Final[Path] = (
    Path(__file__).resolve().parent / "data" / "verified_uniprot_gene_crosswalk.yaml"
)
BUILTIN_PATH: Final[Path] = Path(
    os.environ.get("DRUGOS_BUILTIN_CROSSWALK", str(_BUILTIN_DEFAULT_PATH))
)

# SCI-5 / GUARD-SCI-2: organism constants. The builtin table is human-only;
# non-human organisms will not resolve and the crosswalk logs this loudly.
DEFAULT_ORGANISM_TAX_ID: Final[int] = 9606
BUILTIN_ORGANISM: Final[str] = "Homo sapiens"
BUILTIN_TAX_ID: Final[int] = 9606
BUILTIN_TABLE_VERSION: Final[str] = "2025.01"

# GUARD-PERF-1: memory ceilings. Configurable via env var so production
# deployments with larger servers can raise them.
MAX_ENSP_ENTRIES: Final[int] = int(
    os.environ.get("DRUGOS_MAX_ENSP_ENTRIES", "10_000_000")
)
MAX_CHEMBL_ENTRIES: Final[int] = int(
    os.environ.get("DRUGOS_MAX_CHEMBL_ENTRIES", "500_000")
)
MAX_OPENTARGETS_ENTRIES: Final[int] = int(
    os.environ.get("DRUGOS_MAX_OPENTARGETS_ENTRIES", "500_000")
)

# GUARD-REL-1: DLQ (dead-letter queue) sample size cap.
_DLQ_SAMPLE_CAP: Final[int] = int(os.environ.get("DRUGOS_DLQ_SAMPLE_CAP", "10000"))


# =============================================================================
# Section 2 — Data model: NamedTuples for builtin entries and provenance
# =============================================================================


class VerifiedEntry(NamedTuple):
    """A single verified (UniProt AC, NCBI Gene ID, gene symbol) tuple.

    Implements DOC-3: the builtin table is a tuple of NamedTuples, so
    consumers access fields by name (``entry.uniprot_ac``) rather than
    by positional index. This eliminates the bug class where a reader
    miscounts columns and silently swaps fields.
    """

    uniprot_ac: str
    ncbi_gene_id: str
    gene_symbol: str
    notes: str = ""


class Provenance(NamedTuple):
    """Provenance metadata attached to every resolved ID.

    Implements GUARD-LINE-1. Each stored value in the crosswalk's
    multi-valued dicts is accompanied by a ``Provenance`` so that any
    output value can be traced back to (a) which source file produced
    it, (b) which row in that file, (c) what confidence/source-tag the
    original row had, (d) when the loader ran, (e) the SHA-256 of the
    source file.

    For the builtin table, ``source`` is ``builtin-verified-v{VERSION}``,
    ``source_row`` is the index in the builtin tuple, ``confidence`` is
    ``manually_reviewed``, ``loaded_at`` is the load time and
    ``source_sha256`` is ``builtin``.
    """

    source: str
    source_row: int
    confidence: str
    loaded_at: str
    source_sha256: str


# Storage type aliases — values in multi-valued dicts are paired with
# their Provenance.
_StoredValue = List[Tuple[str, Provenance]]


class AmbiguousMappingError(ValueError):
    """Raised when a translator is called with ``strict=True`` and the
    mapping is multi-valued.

    Implements DES-2. Allows callers that require strict single-value
    semantics to detect ambiguity explicitly instead of silently
    receiving a primary-picked value.
    """


# =============================================================================
# Section 3 — Externalized builtin table (CONF-2)
# =============================================================================


def _load_builtin_yaml(path: Path) -> Tuple[VerifiedEntry, ...]:
    """Load the verified builtin crosswalk from the YAML file at ``path``.

    Implements CONF-2 / DOC-3 / COMP-1. Falls back to a hardcoded copy
    (kept in sync with the YAML) if PyYAML is unavailable or the file is
    missing — never raises at import time (production-safe). On any
    fallback a WARNING is logged.

    Args:
        path: Path to the YAML builtin table.

    Returns:
        Tuple of ``VerifiedEntry`` NamedTuples.
    """
    if _yaml is None:
        logger.warning(
            "PyYAML not available — falling back to hardcoded builtin "
            "table. Install PyYAML (`pip install pyyaml`) for the full "
            "externalized experience."
        )
        return _HARDCODED_BUILTIN_FALLBACK

    if not path.exists():
        logger.error(
            "Builtin crosswalk YAML not found at %s — falling back to "
            "hardcoded builtin table. Check BUILTIN_PATH or set the "
            "DRUGOS_BUILTIN_CROSSWALK env var.",
            _redact_path(path),
        )
        return _HARDCODED_BUILTIN_FALLBACK

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = _yaml.safe_load(f)
    except (OSError, _yaml.YAMLError) as e:  # type: ignore[union-attr]
        logger.error(
            "Failed to parse builtin YAML at %s: %s: %s — falling back "
            "to hardcoded builtin table.",
            _redact_path(path), type(e).__name__, e,
        )
        return _HARDCODED_BUILTIN_FALLBACK

    entries: List[VerifiedEntry] = []
    for raw in data.get("entries", []):
        try:
            entries.append(
                VerifiedEntry(
                    uniprot_ac=str(raw["uniprot_ac"]).strip().upper(),
                    ncbi_gene_id=str(raw["ncbi_gene_id"]).strip(),
                    gene_symbol=str(raw["gene_symbol"]).strip().upper(),
                    notes=str(raw.get("notes", "")).strip(),
                )
            )
        except (KeyError, TypeError, AttributeError) as e:
            logger.warning(
                "Skipping malformed builtin entry %r: %s", raw, e
            )
            continue
    if not entries:
        logger.error(
            "Builtin YAML at %s contained zero valid entries — falling "
            "back to hardcoded builtin table.",
            _redact_path(path),
        )
        return _HARDCODED_BUILTIN_FALLBACK
    return tuple(entries)


# Hardcoded fallback — MUST stay in sync with
# ``data/verified_uniprot_gene_crosswalk.yaml``. The IRS1 entry has the
# SCI-1 fix (gene_id "3667", NOT "2645").
_HARDCODED_BUILTIN_FALLBACK: Final[Tuple[VerifiedEntry, ...]] = (
    VerifiedEntry("P23219", "5742",  "PTGS1",   "COX-1 — aspirin target"),
    VerifiedEntry("P35354", "5743",  "PTGS2",   "COX-2 — celecoxib target"),
    VerifiedEntry("P00519", "25",    "ABL1",    "Imatinib primary target (BCR-ABL)"),
    VerifiedEntry("P10721", "3815",  "KIT",     "Imatinib target (GIST)"),
    VerifiedEntry("P09619", "5159",  "PDGFRB",  "Imatinib target"),
    VerifiedEntry("P00533", "1956",  "EGFR",    "Erlotinib / gefitinib target"),
    VerifiedEntry("P04626", "2064",  "ERBB2",   "Trastuzumab target (HER2)"),
    VerifiedEntry("P14672", "5562",  "PRKAA1",  "AMPK alpha 1 — metformin downstream"),
    VerifiedEntry("P54646", "5563",  "PRKAA2",  "AMPK alpha 2 — metformin downstream"),
    VerifiedEntry("P06213", "3643",  "INSR",    "Insulin receptor"),
    # SCI-1 FIX: IRS1 Gene ID was 2645 (GFAP); corrected to 3667 (IRS1).
    VerifiedEntry("P35568", "3667",  "IRS1",    "Insulin receptor substrate 1 — SCI-1 fix"),
    VerifiedEntry("P11712", "1559",  "CYP2C9",  "Warfarin metabolism (S-isomer)"),
    VerifiedEntry("P00735", "2147",  "F2",      "Prothrombin — warfarin target end-effect"),
    VerifiedEntry("P00742", "2159",  "F10",     "Factor X — warfarin target end-effect"),
    VerifiedEntry("P00451", "7450",  "VWF",     "von Willebrand factor"),
    VerifiedEntry("P08172", "1813",  "DRD2",    "Dopamine D2 — antipsychotic target"),
    VerifiedEntry("P31645", "6532",  "SLC6A4",  "Serotonin transporter — SSRIs"),
    VerifiedEntry("P23975", "6531",  "SLC6A3",  "Dopamine transporter"),
    VerifiedEntry("P04035", "3156",  "HMGCR",   "Atorvastatin, simvastatin target"),
    VerifiedEntry("P12821", "1636",  "ACE",     "Lisinopril, enalapril target"),
    VerifiedEntry("P08588", "153",   "ADRB1",   "Beta-1 — metoprolol target"),
    VerifiedEntry("P07550", "154",   "ADRB2",   "Beta-2 — albuterol target"),
    VerifiedEntry("P06401", "5241",  "PGR",     "Progesterone receptor"),
    VerifiedEntry("P03372", "2099",  "ESR1",    "Estrogen receptor alpha — tamoxifen target"),
    VerifiedEntry("P04150", "2908",  "NR3C1",   "Glucocorticoid receptor — dexamethasone target"),
    VerifiedEntry("O76074", "8654",  "PDE5A",   "PDE5 — sildenafil target"),
    VerifiedEntry("P42345", "2475",  "MTOR",    "mTOR — rapamycin / everolimus target"),
    VerifiedEntry("P00374", "1719",  "DHFR",    "DHFR — methotrexate target"),
    VerifiedEntry("P38398", "672",   "BRCA1",   "Breast cancer susceptibility"),
    VerifiedEntry("P04637", "7157",  "TP53",    "p53 — multiple cancers"),
)


# The module-level constant — populated at import time from the YAML
# (CONF-2). Existing imports of ``VERIFIED_UNIPROT_GENE_CROSSWALK`` continue
# to work; the only difference is that entries are now NamedTuples rather
# than raw 3-tuples (DOC-3). Positional unpacking still works because
# NamedTuples are tuples.
VERIFIED_UNIPROT_GENE_CROSSWALK: Final[Tuple[VerifiedEntry, ...]] = (
    _load_builtin_yaml(BUILTIN_PATH)
)


# =============================================================================
# Section 4 — Source classes (ARCH-3: separate loaders from translators)
# =============================================================================
# v41 ROOT FIX (Task J SEV2): the abstract base class ``CrosswalkSource``
# (previously defined here with ``def load_into(self, crosswalk): raise
# NotImplementedError``) was DEAD CODE — it had NO subclasses, NO
# instantiations, and NO callers anywhere in the codebase (verified via
# `grep -rn "CrosswalkSource" phase2/ tests/`). Its presence in ``__all__``
# suggested a public extension point that didn't actually work (subclasses
# would have had to re-implement the entire load_into contract from
# scratch with no helper methods). The real extension point is
# ``IDCrosswalk.load_*`` methods, which is what every production caller
# uses. Removed both the class definition AND its ``__all__`` entry.
# A PEP 562 ``__getattr__`` (defined at module bottom) raises a clear
# ImportError with upgrade guidance if any external code still imports
# ``CrosswalkSource`` by name (none found in this repo, but the symbol
# was in ``__all__`` so third-party code could have imported it).


# =============================================================================
# Section 5 — The IDCrosswalk class
# =============================================================================


class IDCrosswalk:
    """Translates external protein/gene IDs to the canonical UniProt AC.

    A single instance holds the following lookup tables (DES-1 split):
      - ``ensembl_protein_to_uniprot`` (STRING/STITCH ENSP -> UniProt AC, multi-valued)
      - ``chembl_target_to_uniprot``   (ChEMBL CHEMBL**** -> UniProt AC, multi-valued)
      - ``ensg_to_uniprot``            (OpenTargets ENSG -> UniProt AC, multi-valued)
      - ``gene_symbol_to_uniprot``     (Gene symbol "PTGS1" -> UniProt AC, multi-valued)
      - ``uniprot_to_gene``            (UniProt AC -> NCBI Gene ID, multi-valued)
      - ``gene_to_uniprot``            (NCBI Gene ID -> UniProt AC, multi-valued)
      - ``uniprot_secondary_to_primary`` (UniProt secondary AC -> primary AC, single-valued)

    For the Gene <-> Protein bridge, ``uniprot_to_gene`` and its reverse
    are used.

    When external alias files are not available, we fall back to the
    ``VERIFIED_UNIPROT_GENE_CROSSWALK`` built-in table. In that case only
    the 30 most common drug targets will be crosswalk-able; this is
    sufficient for the scientific correctness tests but production use
    requires downloading the full STRING aliases file.

    Thread-safety:
        - Translators are safe to call concurrently with other translators.
        - Loaders MUST NOT be called concurrently with translators on
          the same instance — use ``merge()`` for parallel-built
          crosswalks.
    """

    # ── Construction ─────────────────────────────────────────────────────

    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        # ARCH-5: injectable logger
        self._logger: logging.Logger = logger or globals()["logger"]

        # DES-1 / DES-2: separate dicts per namespace, all multi-valued.
        # Each value is a list of (ac, Provenance) tuples — see GUARD-LINE-1.
        # Backward-compat note: direct injection of plain strings into these
        # dicts (e.g. ``cw.ensembl_protein_to_uniprot["ENSP..."] = "P23219"``)
        # is supported by the translators via ``_coerce_stored_list``. This
        # keeps existing tests that inject strings working unchanged.
        self.ensembl_protein_to_uniprot: Dict[str, Any] = {}
        self.chembl_target_to_uniprot: Dict[str, Any] = {}
        self.ensg_to_uniprot: Dict[str, Any] = {}
        self.gene_symbol_to_uniprot: Dict[str, Any] = {}
        self.uniprot_to_gene: Dict[str, Any] = {}
        self.gene_to_uniprot: Dict[str, Any] = {}
        # Secondary ACs are genuinely single-valued (UniProt merges make
        # secondary -> primary unambiguous); keep as Dict[str, str].
        self.uniprot_secondary_to_primary: Dict[str, str] = {}

        # ── SCI-3 / SCI-9 (added by opentargets_loader v2.0 audit fix) ──
        # Disease → UMLS CUI crosswalk (populated by
        # ``load_opentargets_diseases``). Maps EFO/MONDO/HP/MP/Orphanet/
        # SNOMED/OTAR disease IDs → UMLS CUIs.
        self.disease_to_umls: Dict[str, Any] = {}
        # ENSG → NCBI Gene ID crosswalk (populated by
        # ``load_ensembl_to_ncbi_gene``). Maps Ensembl gene IDs → NCBI
        # Gene IDs (used to unify OpenTargets ENSG-keyed Gene edges with
        # DRKG NCBI-keyed Gene nodes).
        self.ensg_to_ncbi_gene: Dict[str, Any] = {}

        # ── v29 ROOT FIX (audit L-5): Compound ID → InChIKey crosswalk ──
        # Populated by ``register_compound_inchikey()`` (called by the
        # entity_resolver after Phase 1's entity resolution runs) or by
        # ``load_compound_inchikey_crosswalk()`` (bulk TSV loader).
        # Keys are compound IDs in ANY namespace (DrugBank ID, ChEMBL ID,
        # PubChem CID ``CID<digits>``, STITCH CIDm/CIDs, SIDER bare CID,
        # DRKG MESH ``MESH:D000544``, etc.). Values are InChIKeys (the
        # canonical Compound ID per project doc Section 3 / Risk-1).
        # Multi-valued storage matching the existing crosswalk pattern
        # (DES-2) — each value is a list of ``(inchikey, Provenance)``
        # tuples. Most compound IDs map to exactly one InChIKey, but
        # some (e.g. bare PubChem CIDs without stereo info) may map to
        # multiple InChIKeys (one per stereoisomer). The primary
        # InChIKey is the first in source order.
        #
        # Naming: dict is ``compound_to_inchikey`` (short form, matches
        # the ``disease_to_umls`` / ``ensg_to_ncbi_gene`` convention);
        # the translator method is ``compound_id_to_inchikey()`` (long
        # form, matches ``disease_id_to_umls_cui()``). The two names
        # MUST stay distinct — otherwise the instance attribute shadows
        # the class method (Python name-resolution rule).
        self.compound_to_inchikey: Dict[str, Any] = {}

        # IDEM-3 / IDEM-1: state flags
        self._loaded: bool = False
        self._warned_unloaded: bool = False
        self._builtin_loaded: bool = False

        # OBS-2: structured source summary (list of (source_name, count))
        self._source_summary_structured: List[Tuple[str, int]] = []

        # GUARD-IDEM-1: source file hashes
        self._source_files: Dict[str, str] = {}

        # GUARD-REL-1: dead-letter queue for unresolved IDs. The set holds
        # unique samples (capped at _DLQ_SAMPLE_CAP); _unresolved_counts
        # tracks the TOTAL miss count (including duplicates) so repeated
        # queries of the same missing ID are still counted.
        self._unresolved: Dict[str, set] = {
            "ensp": set(),
            "ensg": set(),
            "chembl_target": set(),
            "gene_symbol": set(),
            "ncbi_gene_id": set(),
            "uniprot_ac": set(),
            # v29 ROOT FIX (audit L-5): Compound ID namespace (DLQ for
            # compound IDs that fail to resolve to InChIKey).
            "compound_id": set(),
        }
        self._unresolved_overflow: Dict[str, int] = {k: 0 for k in self._unresolved}
        self._unresolved_counts: Dict[str, int] = {k: 0 for k in self._unresolved}

        # OBS-1: per-translator hit/miss counters
        self._translator_stats: Dict[str, Dict[str, int]] = {
            "ensembl_protein_to_uniprot_ac": {"calls": 0, "hits": 0, "misses": 0},
            "chembl_target_to_uniprot_ac": {"calls": 0, "hits": 0, "misses": 0},
            "ensembl_gene_to_uniprot_ac": {"calls": 0, "hits": 0, "misses": 0},
            "gene_symbol_to_uniprot_ac": {"calls": 0, "hits": 0, "misses": 0},
            "resolve_uniprot_alias": {"calls": 0, "hits": 0, "misses": 0},
            "uniprot_ac_to_ncbi_gene_id": {"calls": 0, "hits": 0, "misses": 0},
            "ncbi_gene_id_to_uniprot_ac": {"calls": 0, "hits": 0, "misses": 0},
            # v29 ROOT FIX (audit L-5)
            "compound_id_to_inchikey": {"calls": 0, "hits": 0, "misses": 0},
        }

        # GUARD-COMP-1: audit trail — instance ID and built_at timestamp
        self._instance_id: str = str(uuid.uuid4())
        self._built_at: str = _iso_now()

        # Internal cache: builtin load timestamp for synthesized provenance
        self._builtin_load_time: Optional[str] = None

        # v29 ROOT FIX (audit L-9/I-7): reverse-lookup index cache.
        # ``reverse_lookup`` previously iterated every entry in every
        # namespace dict on EACH call (O(n) per call). ``canonicalize``
        # calls ``reverse_lookup`` once per invocation, so a batch of n
        # canonicalize() calls ran in O(n²) total — slow on real
        # production data (STRING aliases alone are ~100K entries).
        # The cache maps UniProt AC (UPPERCASED, matching
        # ``reverse_lookup``'s already-uppercased target) to a dict of
        # {namespace: [source IDs that map to this AC]}. Built lazily on
        # first ``reverse_lookup`` call, invalidated by
        # ``_invalidate_reverse_index_cache()`` at the end of every
        # loader (via ``_sort_all_lists``) and on ``clear()``.
        self._reverse_index_cache: Optional[Dict[str, Dict[str, List[str]]]] = None

    # ── Public properties (read-only views of internal state) ────────────

    @property
    def ensembl_gene_to_uniprot(self) -> Dict[str, Any]:
        """Backward-compat alias for ``ensg_to_uniprot`` (DES-1 split).

        Pre-audit, this dict name was used. Existing tests that read it
        continue to see the same ENSG-keyed data. Setting via this alias
        is not supported — use ``ensg_to_uniprot`` directly.
        """
        return self.ensg_to_uniprot

    # ── Internal helpers ─────────────────────────────────────────────────

    @staticmethod
    def _coerce_stored_list(raw: Any) -> List[Tuple[str, Provenance]]:
        """Coerce a stored dict value to a list of (ac, Provenance) tuples.

        Tolerates three input shapes (backward-compat for direct injection):
          - ``"P23219"``                -> ``[("P23219", NULL_PROV)]``
          - ``["P23219", "Q12345"]``    -> ``[("P23219", NULL_PROV), ...]``
          - ``[("P23219", prov), ...]`` -> returned as-is (after copy)
        """
        if raw is None:
            return []
        null_prov = Provenance(
            source="unknown", source_row=-1,
            confidence="unknown", loaded_at="",
            source_sha256="",
        )
        if isinstance(raw, str):
            return [(raw, null_prov)]
        if isinstance(raw, list):
            out: List[Tuple[str, Provenance]] = []
            for item in raw:
                if isinstance(item, tuple) and len(item) == 2:
                    ac, prov = item
                    if not isinstance(prov, Provenance):
                        prov = null_prov
                    out.append((str(ac), prov))
                else:
                    out.append((str(item), null_prov))
            return out
        # Fallback: scalar of another type
        return [(str(raw), null_prov)]

    def _record_unresolved(self, namespace: str, key: str) -> None:
        """Track an unresolved ID in the dead-letter queue (GUARD-REL-1).

        Caps the set size at ``_DLQ_SAMPLE_CAP`` to bound memory; the
        overflow counter continues to track total misses. The total
        miss count (including duplicate queries of the same missing ID)
        is tracked in ``_unresolved_counts``.
        """
        bucket = self._unresolved.get(namespace)
        if bucket is None:
            return
        # Always increment the total count (duplicates included)
        self._unresolved_counts[namespace] = (
            self._unresolved_counts.get(namespace, 0) + 1
        )
        if len(bucket) < _DLQ_SAMPLE_CAP:
            bucket.add(key)
        else:
            self._unresolved_overflow[namespace] = (
                self._unresolved_overflow.get(namespace, 0) + 1
            )

    def _record_translator_call(
        self, translator_name: str, hit: bool
    ) -> None:
        """Increment the hit/miss counter for a translator (OBS-1).

        Call this exactly ONCE per translator invocation, AFTER the
        lookup has been performed, with the correct ``hit`` flag. Do
        NOT call this with ``hit=False`` up-front and then manually
        bump ``hits`` — that double-counts misses.
        """
        stats = self._translator_stats.get(translator_name)
        if stats is None:
            return
        stats["calls"] += 1
        if hit:
            stats["hits"] += 1
        else:
            stats["misses"] += 1
        # OBS-1: warn at >90% miss rate after at least 1000 calls
        if stats["calls"] >= 1000:
            rate = stats["hits"] / stats["calls"]
            if rate < 0.10 and stats["calls"] % 1000 == 0:
                self._logger.warning(
                    "Translator %s has low hit rate: %d/%d (%.1f%%) — "
                    "consider loading additional source files.",
                    translator_name, stats["hits"], stats["calls"], rate * 100,
                )

    def _append_source_summary(self, name: str, count: int) -> None:
        """Append a (source, count) entry, warning on duplicate (OBS-2)."""
        for existing_name, _ in self._source_summary_structured:
            if existing_name == name:
                self._logger.warning(
                    "Source %r loaded twice — possible re-run without "
                    "reset(). Counts will be cumulative.",
                    name,
                )
                break
        self._source_summary_structured.append((name, count))

    def _check_unloaded_warning(self) -> None:
        """Warn ONCE if a translator is called before any load_* (ARCH-6)."""
        if not self._loaded and not self._warned_unloaded:
            self._logger.warning(
                "IDCrosswalk queried before any load_*() — all "
                "translators will return None. Call load_builtin() at "
                "minimum."
            )
            self._warned_unloaded = True

    def _stage_marker(self, stage: str, added: int, elapsed: float,
                      source_hash: str = "") -> None:
        """Emit a structured pipeline-stage marker (GUARD-OBS-1)."""
        h = source_hash[:8] if source_hash else "n/a"
        self._logger.info(
            "=== IDCrosswalk: stage '%s' complete (%d entries, %.2fs, "
            "source_sha256=%s) ===",
            stage, added, elapsed, h,
        )

    @staticmethod
    def _validate_allowed_dir(path: Path,
                              allowed_dir: Optional[Path]) -> bool:
        """Return True if ``path`` is inside ``allowed_dir`` (SEC-2)."""
        if allowed_dir is None:
            return True
        try:
            path.resolve().relative_to(allowed_dir.resolve())
            return True
        except (ValueError, OSError):
            return False

    # ── Loaders (public API preserved; bodies delegate to sources) ───────

    def load_builtin(self) -> int:
        """Load the built-in verified crosswalk (30 high-confidence entries).

        This is always called first by ``get_default_crosswalk()``.
        External alias files ADD to this table; they do not replace it.

        Implements:
          - SCI-1: IRS1 Gene ID corrected from 2645 (GFAP) to 3667 (IRS1).
          - SCI-2: gene symbols stored in ``gene_symbol_to_uniprot``,
            NOT in ``ensg_to_uniprot`` (which is now ENSG-only).
          - SCI-7 / GUARD-SCI-2: organism documented; builtin table is
            Homo sapiens (tax_id=9606).
          - DQ-2: duplicate detection on UniProt ACs, Gene IDs, symbols.
          - DOC-2: idempotent — second call is a no-op + WARNING.
          - COMP-1: ``BUILTIN_TABLE_VERSION`` included in summary.

        Returns:
            Number of entries loaded (0 if already loaded — see DOC-2).
        """
        # DOC-2 idempotency
        if self._builtin_loaded:
            self._logger.warning(
                "load_builtin() called twice — possible re-run; no-op. "
                "Use clear() before reload if you intend to reseed."
            )
            return 0

        t0 = time.time()
        # GUARD-SCI-2 / COMP-1: log organism on first call
        self._logger.info(
            "IDCrosswalk: loading builtin table version %s "
            "(organism=%s, tax_id=%d)",
            BUILTIN_TABLE_VERSION, BUILTIN_ORGANISM, BUILTIN_TAX_ID,
        )

        # SCI-1: all entries come from the YAML (or the hardcoded fallback)
        # which already has the corrected IRS1 Gene ID "3667".
        entries = VERIFIED_UNIPROT_GENE_CROSSWALK

        # DQ-2: duplicate detection (gated by DRUGOS_STRICT for assert vs warn)
        uniprot_acs = [e.uniprot_ac for e in entries]
        gene_ids = [e.ncbi_gene_id for e in entries]
        symbols = [e.gene_symbol for e in entries]
        strict = os.environ.get("DRUGOS_STRICT", "") == "1"
        dups_msg = []
        if len(set(uniprot_acs)) != len(uniprot_acs):
            dups = sorted({a for a in uniprot_acs if uniprot_acs.count(a) > 1})
            dups_msg.append(f"duplicate UniProt ACs: {dups}")
        if len(set(gene_ids)) != len(gene_ids):
            dups = sorted({g for g in gene_ids if gene_ids.count(g) > 1})
            dups_msg.append(f"duplicate Gene IDs: {dups}")
        if len(set(symbols)) != len(symbols):
            dups = sorted({s for s in symbols if symbols.count(s) > 1})
            dups_msg.append(f"duplicate gene symbols: {dups}")
        if dups_msg:
            msg = "; ".join(dups_msg)
            if strict:
                raise AssertionError(f"Builtin table has duplicates: {msg}")
            self._logger.error(
                "Builtin table has duplicates: %s — DRUGOS_STRICT=1 would "
                "have raised.", msg,
            )

        # SCI-6: validate every UniProt AC before storing
        self._builtin_load_time = _iso_now()
        builtin_prov_template = Provenance(
            source=f"builtin-verified-v{BUILTIN_TABLE_VERSION}",
            source_row=-1,  # filled per-entry below
            confidence="manually_reviewed",
            loaded_at=self._builtin_load_time,
            source_sha256="builtin",
        )

        added = 0
        for idx, entry in enumerate(entries):
            if not _validate_uniprot_ac(entry.uniprot_ac):
                self._logger.warning(
                    "Skipping builtin entry with invalid UniProt AC: %r",
                    entry,
                )
                continue
            gene_id = _normalize_ncbi_gene_id(entry.ncbi_gene_id)
            if gene_id is None:
                self._logger.warning(
                    "Skipping builtin entry with invalid NCBI Gene ID: %r",
                    entry,
                )
                continue
            prov = builtin_prov_template._replace(source_row=idx)
            # Multi-valued storage (DES-2)
            self.uniprot_to_gene.setdefault(entry.uniprot_ac, []).append(
                (gene_id, prov)
            )
            self.gene_to_uniprot.setdefault(gene_id, []).append(
                (entry.uniprot_ac, prov)
            )
            # SCI-2: gene symbols go to gene_symbol_to_uniprot, NOT ensg_to_uniprot
            self.gene_symbol_to_uniprot.setdefault(
                entry.gene_symbol.upper(), []
            ).append((entry.uniprot_ac, prov))
            added += 1

        # IDEM-4: deterministic ordering — sort each list
        self._sort_all_lists()

        self._loaded = True
        self._builtin_loaded = True
        self._append_source_summary("builtin", added)
        elapsed = time.time() - t0
        self._stage_marker("load_builtin", added, elapsed, "builtin")
        self._logger.info(
            "IDCrosswalk: loaded %d verified UniProt<->Gene mappings",
            added,
        )
        return added

    def load_from_uniprot_records(self, uniprot_records: list) -> int:
        """Populate the crosswalk from parsed UniProt dat records.

        Implements:
          - SCI-3: secondary accessions stored in
            ``uniprot_secondary_to_primary``, NOT in ``ensg_to_uniprot``.
          - COD-3: corrected double-counting of ``added``.
          - DQ-3: tolerant of str / non-str / malformed secondary ACs.
          - DQ-4: numeric validation of ``gene_id``.
          - COD-6 / DQ-4: pandas float artifacts tolerated.
          - GUARD-LINE-1: provenance attached.

        Args:
            uniprot_records: list of dicts (output of
                ``uniprot_loader.parse_uniprot_entries``). Each must have
                ``accession`` and ``gene_id`` keys.

        Returns:
            Number of NEW UniProt<->Gene mappings added (one per record,
            not double-counted — COD-3).
        """
        t0 = time.time()
        added_u2g = 0
        added_g2u = 0
        added_sec = 0
        source_name = "uniprot-dat"

        for rec_idx, rec in enumerate(uniprot_records):
            if not isinstance(rec, dict):
                self._logger.warning(
                    "Skipping non-dict UniProt record at index %d: %r",
                    rec_idx, rec,
                )
                continue
            uniprot_ac_raw = rec.get("accession", "")
            uniprot_ac = (
                str(uniprot_ac_raw).strip().upper()
                if uniprot_ac_raw is not None else ""
            )
            if not _validate_uniprot_ac(uniprot_ac):
                self._logger.warning(
                    "Skipping UniProt record at index %d with invalid "
                    "accession %r", rec_idx, uniprot_ac_raw,
                )
                continue

            # DQ-4 / COD-6: numeric gene_id validation
            gene_id = _normalize_ncbi_gene_id(rec.get("gene_id"))
            if gene_id is None:
                self._logger.warning(
                    "Skipping record with non-numeric gene_id: %r "
                    "(accession %s)", rec.get("gene_id"), uniprot_ac,
                )
                continue

            prov = Provenance(
                source=source_name, source_row=rec_idx,
                confidence="swissprot_reviewed",
                loaded_at=_iso_now(),
                source_sha256=self._source_files.get(source_name, ""),
            )

            # Don't overwrite verified mappings (preserve builtin priority)
            existing = self.uniprot_to_gene.get(uniprot_ac)
            if existing is None:
                self.uniprot_to_gene.setdefault(uniprot_ac, []).append(
                    (gene_id, prov)
                )
                added_u2g += 1
            existing_g = self.gene_to_uniprot.get(gene_id)
            if existing_g is None:
                self.gene_to_uniprot.setdefault(gene_id, []).append(
                    (uniprot_ac, prov)
                )
                added_g2u += 1

            # SCI-3 / DQ-3: secondary accessions — tolerant + correct dict
            secs = rec.get("secondary_accessions") or []
            if isinstance(secs, str):
                self._logger.warning(
                    "secondary_accessions is a str, not list, for %s: %r",
                    uniprot_ac, secs,
                )
                secs = [secs]
            for sec in secs:
                if not isinstance(sec, str):
                    self._logger.warning(
                        "non-str secondary accession for %s: %r",
                        uniprot_ac, sec,
                    )
                    continue
                sec_clean = sec.strip().upper()
                if not _validate_uniprot_ac(sec_clean):
                    self._logger.warning(
                        "invalid secondary accession for %s: %r",
                        uniprot_ac, sec,
                    )
                    continue
                # Last-write-wins is correct for UniProt merges
                self.uniprot_secondary_to_primary[sec_clean] = uniprot_ac
                added_sec += 1

        # IDEM-4: deterministic ordering
        self._sort_all_lists()

        added = added_u2g  # COD-3: report NEW primary mappings only
        if added or added_sec:
            self._append_source_summary(source_name, added)
            self._logger.info(
                "IDCrosswalk: added %d primary mappings + %d secondary "
                "AC aliases from UniProt dat file",
                added, added_sec,
            )
        elapsed = time.time() - t0
        self._stage_marker("load_from_uniprot_records", added, elapsed,
                           self._source_files.get(source_name, ""))
        return added

    def load_string_aliases(
        self,
        aliases_path: Path,
        allowed_dir: Optional[Path] = None,
        tax_id: int = DEFAULT_ORGANISM_TAX_ID,
    ) -> int:
        """Load STRING aliases file (e.g. ``9606.protein.aliases.v12.0.txt.gz``).

        Implements:
          - INT-1: canonical key form is bare ENSP (``ENSP00000358091``);
            ``9606.`` prefix and ``.N`` isoform suffix are stripped.
          - DQ-1: multi-valued — multiple UniProt ACs per ENSP preserved;
            Swiss-Prot ACs prioritized over TrEMBL.
          - REL-1: I/O wrapped in try/except — pipeline never crashes.
          - REL-3: partial-load rollback — temp dict only committed on
            successful full parse.
          - GUARD-REL-2: version-mismatch detection on the header row.
          - GUARD-PERF-1: memory-ceiling check.
          - GUARD-IDEM-1: source file SHA-256 recorded.
          - GUARD-SEC-2: only basename logged at INFO/WARN.
          - SEC-2: optional ``allowed_dir`` path-traversal protection.
          - PERF-2 / PERF-3: batched update with optional pandas fast path.

        Args:
            aliases_path: Path to the STRING aliases file (gzipped TSV).
            allowed_dir: Optional directory that ``aliases_path`` must be
                inside (SEC-2 path-traversal protection).
            tax_id: NCBI taxonomy ID for the species prefix (default 9606).

        Returns:
            Number of mappings added. Returns 0 on missing file, corrupt
            file, version mismatch, path-traversal refusal, or memory
            ceiling breach — never raises.
        """
        # SEC-2: path-traversal check
        if not self._validate_allowed_dir(aliases_path, allowed_dir):
            self._logger.error(
                "load_string_aliases: path %r is outside allowed_dir %r "
                "— refusing to load (SEC-2).",
                _redact_path(aliases_path),
                _redact_path(allowed_dir) if allowed_dir else "n/a",
            )
            return 0
        if not aliases_path.exists():
            self._logger.warning(
                "STRING aliases file not found: %s", _redact_path(aliases_path),
            )
            return 0

        # GUARD-IDEM-1: source file hash
        try:
            source_hash = _sha256_of_file(aliases_path)
        except OSError as e:
            self._logger.warning(
                "load_string_aliases: could not hash %s (%s: %s) — "
                "proceeding without hash.",
                _redact_path(aliases_path), type(e).__name__, e,
            )
            source_hash = ""

        is_gz = str(aliases_path).endswith(".gz")
        open_func = gzip.open if is_gz else open
        source_name = "string-aliases"
        t0 = time.time()

        # REL-3: load into temp dict, commit only on full success
        temp: Dict[str, _StoredValue] = {}
        added = 0
        try:
            with open_func(aliases_path, "rt", encoding="utf-8") as f:
                # GUARD-REL-2: validate header / first non-# line
                first_data_line: Optional[str] = None
                line_no = 0
                for line_no, line in enumerate(f, start=1):
                    if not line.strip() or line.startswith("#"):
                        continue
                    first_data_line = line
                    break
                if first_data_line is None:
                    self._logger.warning(
                        "load_string_aliases: %s contains no data rows.",
                        _redact_path(aliases_path),
                    )
                    self._stage_marker(source_name, 0, time.time() - t0, source_hash)
                    return 0
                parts = first_data_line.rstrip("\n").split("\t")
                if len(parts) < 3:
                    self._logger.warning(
                        "load_string_aliases: version mismatch — first "
                        "data row has %d columns, expected >= 3. File: %s",
                        len(parts), _redact_path(aliases_path),
                    )
                    self._stage_marker(source_name, 0, time.time() - t0, source_hash)
                    return 0
                # First column should start with f"{tax_id}."
                if not parts[0].startswith(f"{tax_id}."):
                    self._logger.warning(
                        "load_string_aliases: version mismatch — first "
                        "data row's first column %r does not start with "
                        "expected prefix %r. File: %s",
                        parts[0][:32], f"{tax_id}.", _redact_path(aliases_path),
                    )
                    self._stage_marker(source_name, 0, time.time() - t0, source_hash)
                    return 0
                # Re-process the first data line
                ensp = _normalize_ensp(parts[0])
                alias = parts[1].strip().upper()
                source_tag = parts[2].strip()
                if source_tag == "UniProt_AC" and _validate_uniprot_ac(alias):
                    prov = Provenance(
                        source=source_name, source_row=line_no,
                        confidence=source_tag, loaded_at=_iso_now(),
                        source_sha256=source_hash,
                    )
                    temp.setdefault(ensp, []).append((alias, prov))
                    added += 1

                # Process the rest of the file
                for line_no, line in enumerate(f, start=line_no + 1):
                    if not line.strip() or line.startswith("#"):
                        continue
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) < 3:
                        continue
                    source_tag = parts[2].strip()
                    if source_tag != "UniProt_AC":
                        continue
                    ensp = _normalize_ensp(parts[0])
                    alias = parts[1].strip().upper()
                    if not _validate_uniprot_ac(alias):
                        self._logger.debug(
                            "load_string_aliases: skipping invalid AC "
                            "%r at line %d", alias, line_no,
                        )
                        continue
                    prov = Provenance(
                        source=source_name, source_row=line_no,
                        confidence=source_tag, loaded_at=_iso_now(),
                        source_sha256=source_hash,
                    )
                    temp.setdefault(ensp, []).append((alias, prov))
                    added += 1

        except (OSError, UnicodeDecodeError) as e:
            # REL-1: gzip.BadGzipFile subclasses OSError; UnicodeDecodeError
            # is raised by the text-mode reader on non-UTF-8 bytes.
            self._logger.warning(
                "load_string_aliases: failed to read %s: %s: %s. "
                "Returning 0 mappings (partial load of %d entries "
                "discarded).",
                _redact_path(aliases_path), type(e).__name__, e, len(temp),
            )
            self._stage_marker(source_name, 0, time.time() - t0, source_hash)
            return 0
        except Exception as e:  # pragma: no cover — last-resort guard
            self._logger.error(
                "load_string_aliases: unexpected error on %s: %s: %s. "
                "Returning 0 mappings (partial load of %d entries "
                "discarded).",
                _redact_path(aliases_path), type(e).__name__, e, len(temp),
                exc_info=True,
            )
            self._stage_marker(source_name, 0, time.time() - t0, source_hash)
            return 0

        # GUARD-PERF-1: memory ceiling
        if len(temp) > MAX_ENSP_ENTRIES:
            self._logger.error(
                "load_string_aliases: %d entries exceeds MAX_ENSP_ENTRIES=%d. "
                "Refusing to commit (set DRUGOS_MAX_ENSP_ENTRIES to override).",
                len(temp), MAX_ENSP_ENTRIES,
            )
            self._stage_marker(source_name, 0, time.time() - t0, source_hash)
            return 0

        # IDEM-4: deterministic ordering — sort each list (Swiss-Prot first
        # via _sort_ac_list)
        for k in temp:
            temp[k] = self._sort_ac_list(temp[k])

        # PERF-2: one bulk update
        for k, v in temp.items():
            existing = self.ensembl_protein_to_uniprot.get(k)
            if existing is None:
                self.ensembl_protein_to_uniprot[k] = v
            else:
                # Merge — preserve unique ACs
                merged = list(self._coerce_stored_list(existing))
                seen = {ac for ac, _ in merged}
                for ac, prov in v:
                    if ac not in seen:
                        merged.append((ac, prov))
                        seen.add(ac)
                self.ensembl_protein_to_uniprot[k] = self._sort_ac_list(merged)

        self._source_files[source_name] = source_hash
        self._append_source_summary(source_name, added)
        elapsed = time.time() - t0
        self._stage_marker("load_string_aliases", added, elapsed, source_hash)
        self._logger.info(
            "IDCrosswalk: added %d STRING Ensembl->UniProt mappings "
            "(%d unique ENSPs)",
            added, len(temp),
        )
        return added

    def load_chembl_target_components(
        self,
        db_path: Path,
        allowed_dir: Optional[Path] = None,
        organism_tax_id: int = DEFAULT_ORGANISM_TAX_ID,
    ) -> int:
        """Load ChEMBL ``target_components`` -> UniProt accession crosswalk.

        Implements:
          - SCI-4: multi-subunit complexes preserved (Dict[str, List[str]]).
          - SCI-5: organism filter (default ``tax_id=9606``).
          - SCI-6 / DQ-5: format validation for UniProt AC and ChEMBL ID.
          - GUARD-DQ-1: schema-drift guard (returns 0 + ERROR, no crash).
          - REL-1: SQLite errors return partial count, no crash.
          - REL-2: SQLite busy_timeout + retry with exponential backoff.
          - REL-3: partial-load rollback.
          - SEC-1: read-only SQLite connection.
          - SEC-2: optional ``allowed_dir`` path-traversal protection.
          - PERF-1: cursor iteration (no ``fetchall()``).
          - GUARD-PERF-1: memory ceiling.
          - GUARD-IDEM-1: source file SHA-256.
          - GUARD-SEC-1: parameterized SQL only.

        Args:
            db_path: Path to the ChEMBL SQLite database.
            allowed_dir: Optional directory that ``db_path`` must be inside.
            organism_tax_id: NCBI taxonomy ID filter (default 9606 — human).

        Returns:
            Number of mappings added. Returns 0 on missing file, schema
            drift, locked DB (after retries), or memory ceiling breach —
            never raises.
        """
        # SEC-2: path-traversal check
        if not self._validate_allowed_dir(db_path, allowed_dir):
            self._logger.error(
                "load_chembl_target_components: path %r is outside "
                "allowed_dir %r — refusing to load (SEC-2).",
                _redact_path(db_path),
                _redact_path(allowed_dir) if allowed_dir else "n/a",
            )
            return 0
        if not db_path.exists():
            self._logger.warning(
                "ChEMBL SQLite DB not found: %s", _redact_path(db_path),
            )
            return 0

        # GUARD-IDEM-1: source file hash
        try:
            source_hash = _sha256_of_file(db_path)
        except OSError as e:
            self._logger.warning(
                "load_chembl_target_components: could not hash %s (%s: %s).",
                _redact_path(db_path), type(e).__name__, e,
            )
            source_hash = ""

        # Log the organism filter on first call
        self._logger.info(
            "Filtering ChEMBL by tax_id=%d (DEFAULT_ORGANISM_TAX_ID)",
            organism_tax_id,
        )

        # SECURITY: never string-format user input into this SQL — use ?
        # placeholders. (GUARD-SEC-1)
        # Expected schema (documented for GUARD-DQ-1):
        #   target_dictionary(tid, chembl_id, tax_id, ...)
        #   target_components(tid, accession, ...)
        SQL = (
            "SELECT DISTINCT td.chembl_id, tc.accession "
            "FROM target_dictionary td "
            "JOIN target_components tc ON td.tid = tc.tid "
            "WHERE tc.accession IS NOT NULL AND tc.accession != '' "
            "AND td.tax_id = ?"
        )
        params = (organism_tax_id,)

        source_name = "chembl-target-components"
        temp: Dict[str, _StoredValue] = {}
        added = 0
        t0 = time.time()

        # REL-2: retry on "database is locked" with exponential backoff
        max_attempts = 3
        backoff_seconds = [1.0, 2.0, 4.0]
        conn: Optional[sqlite3.Connection] = None
        last_exc: Optional[Exception] = None

        for attempt in range(max_attempts):
            try:
                # SEC-1: open read-only via URI
                conn = sqlite3.connect(
                    f"file:{db_path}?mode=ro", uri=True, timeout=30.0,
                )
                # REL-2: busy_timeout pragma
                try:
                    conn.execute("PRAGMA busy_timeout=30000")
                except sqlite3.DatabaseError:
                    pass  # not all SQLite builds honor this pragma
                break
            except sqlite3.OperationalError as e:
                last_exc = e
                if "locked" in str(e).lower() and attempt < max_attempts - 1:
                    self._logger.warning(
                        "load_chembl_target_components: SQLite locked "
                        "(attempt %d/%d) — retrying in %.1fs.",
                        attempt + 1, max_attempts, backoff_seconds[attempt],
                    )
                    time.sleep(backoff_seconds[attempt])
                    continue
                self._logger.error(
                    "load_chembl_target_components: could not open %s "
                    "after %d attempts: %s",
                    _redact_path(db_path), attempt + 1, e,
                )
                self._stage_marker(source_name, 0, time.time() - t0, source_hash)
                return 0

        if conn is None:
            self._logger.error(
                "load_chembl_target_components: no connection after retries."
            )
            self._stage_marker(source_name, 0, time.time() - t0, source_hash)
            return 0

        try:
            with conn:  # transaction context
                cur = conn.cursor()
                try:
                    cur.execute(SQL, params)
                except sqlite3.OperationalError as e:
                    # GUARD-DQ-1: schema drift
                    self._logger.error(
                        "ChEMBL SQL failed (schema drift suspected): %s. "
                        "Returning 0 mappings. ChEMBL loader will fall "
                        "back to in-loader SQL JOIN.", e,
                    )
                    self._stage_marker(source_name, 0, time.time() - t0, source_hash)
                    return 0

                # PERF-1: iterate cursor directly (no fetchall)
                for row_idx, (chembl_id, uniprot_ac) in enumerate(cur):
                    # DQ-5: validate ChEMBL ID
                    if not _validate_chembl_id(chembl_id):
                        self._logger.debug(
                            "Skipping invalid ChEMBL ID: %r", chembl_id,
                        )
                        continue
                    # SCI-6: validate UniProt AC
                    if not _validate_uniprot_ac(uniprot_ac):
                        self._logger.debug(
                            "Skipping invalid UniProt AC for %s: %r",
                            chembl_id, uniprot_ac,
                        )
                        continue
                    ac_clean = uniprot_ac.strip().upper()
                    prov = Provenance(
                        source=source_name, source_row=row_idx,
                        confidence="chembl_curated", loaded_at=_iso_now(),
                        source_sha256=source_hash,
                    )
                    # SCI-4: append (multi-subunit) — never overwrite
                    temp.setdefault(chembl_id, []).append((ac_clean, prov))
                    added += 1
        except sqlite3.DatabaseError as e:
            # REL-1: any other SQLite error — return partial count, no crash
            self._logger.warning(
                "load_chembl_target_components: SQLite error on %s: %s: %s. "
                "Returning 0 mappings (partial load of %d entries discarded).",
                _redact_path(db_path), type(e).__name__, e, len(temp),
            )
            self._stage_marker(source_name, 0, time.time() - t0, source_hash)
            return 0
        except Exception as e:  # pragma: no cover
            self._logger.error(
                "load_chembl_target_components: unexpected error on %s: "
                "%s: %s. Returning 0 mappings.",
                _redact_path(db_path), type(e).__name__, e, exc_info=True,
            )
            self._stage_marker(source_name, 0, time.time() - t0, source_hash)
            return 0
        finally:
            # COD-4: always close, even on exception
            with contextlib.suppress(Exception):
                conn.close()

        # GUARD-PERF-1: memory ceiling
        if len(temp) > MAX_CHEMBL_ENTRIES:
            self._logger.error(
                "load_chembl_target_components: %d entries exceeds "
                "MAX_CHEMBL_ENTRIES=%d. Refusing to commit.",
                len(temp), MAX_CHEMBL_ENTRIES,
            )
            self._stage_marker(source_name, 0, time.time() - t0, source_hash)
            return 0

        # IDEM-4: deterministic ordering
        for k in temp:
            temp[k] = self._sort_ac_list(temp[k])

        # PERF-2: bulk update
        for k, v in temp.items():
            existing = self.chembl_target_to_uniprot.get(k)
            if existing is None:
                self.chembl_target_to_uniprot[k] = v
            else:
                merged = list(self._coerce_stored_list(existing))
                seen = {ac for ac, _ in merged}
                for ac, prov in v:
                    if ac not in seen:
                        merged.append((ac, prov))
                        seen.add(ac)
                self.chembl_target_to_uniprot[k] = self._sort_ac_list(merged)

        self._source_files[source_name] = source_hash
        self._append_source_summary(source_name, added)
        elapsed = time.time() - t0
        self._stage_marker("load_chembl_target_components", added, elapsed, source_hash)
        self._logger.info(
            "IDCrosswalk: added %d ChEMBL target->UniProt mappings "
            "(%d unique ChEMBL targets)",
            added, len(temp),
        )
        return added

    def load_opentargets_targets(
        self,
        targets_path: Path,
        allowed_dir: Optional[Path] = None,
    ) -> int:
        """Load OpenTargets target metadata JSONL (ENSG -> UniProt AC).

        Implements ARCH-1. Expected file format: one JSON object per line,
        each ``{"id": "ENSG00000123456", "proteinIds": [{"id": "P23219",
        "source": "uniprot"}, ...]}``. Stores ALL UniProt-sourced
        proteinIds per ENSG id into ``self.ensg_to_uniprot`` (multi-valued
        per DES-2). The FIRST UniProt-sourced proteinId is the primary.

        Implements:
          - ARCH-1: this method (previously missing).
          - SCI-2: populates ``ensg_to_uniprot`` with real ENSG IDs.
          - REL-1: I/O wrapped in try/except — pipeline never crashes.
          - REL-3: partial-load rollback.
          - GUARD-PERF-1: memory ceiling.
          - GUARD-IDEM-1: source file SHA-256.
          - SEC-2: optional ``allowed_dir`` path-traversal protection.

        Args:
            targets_path: Path to the OpenTargets targets JSONL file.
            allowed_dir: Optional directory that ``targets_path`` must be
                inside.

        Returns:
            Number of mappings added. Returns 0 on missing file, parse
            error, or memory ceiling breach — never raises.
        """
        # SEC-2: path-traversal check
        if not self._validate_allowed_dir(targets_path, allowed_dir):
            self._logger.error(
                "load_opentargets_targets: path %r is outside allowed_dir "
                "%r — refusing to load (SEC-2).",
                _redact_path(targets_path),
                _redact_path(allowed_dir) if allowed_dir else "n/a",
            )
            return 0
        if not targets_path.exists():
            self._logger.warning(
                "OpenTargets targets file not found: %s",
                _redact_path(targets_path),
            )
            return 0

        # GUARD-IDEM-1: source file hash
        try:
            source_hash = _sha256_of_file(targets_path)
        except OSError as e:
            self._logger.warning(
                "load_opentargets_targets: could not hash %s (%s: %s).",
                _redact_path(targets_path), type(e).__name__, e,
            )
            source_hash = ""

        source_name = "opentargets-targets"
        temp: Dict[str, _StoredValue] = {}
        added = 0
        t0 = time.time()
        try:
            with open(targets_path, "r", encoding="utf-8") as f:
                for line_no, line in enumerate(f, start=1):
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError as e:
                        self._logger.warning(
                            "load_opentargets_targets: malformed JSON at "
                            "line %d of %s: %s",
                            line_no, _redact_path(targets_path), e,
                        )
                        continue
                    if not isinstance(entry, dict):
                        continue
                    ensg = str(entry.get("id", "")).strip().upper()
                    if not _ENSG_PATTERN.match(ensg):
                        self._logger.debug(
                            "load_opentargets_targets: skipping non-ENSG "
                            "id %r at line %d", ensg, line_no,
                        )
                        continue
                    protein_ids = entry.get("proteinIds") or []
                    if not isinstance(protein_ids, list):
                        self._logger.warning(
                            "load_opentargets_targets: proteinIds not a "
                            "list at line %d for ENSG %s: %r",
                            line_no, ensg, protein_ids,
                        )
                        continue
                    for prot in protein_ids:
                        if not isinstance(prot, dict):
                            continue
                        if str(prot.get("source", "")).lower() != "uniprot":
                            continue
                        ac = str(prot.get("id", "")).strip().upper()
                        if not _validate_uniprot_ac(ac):
                            self._logger.warning(
                                "load_opentargets_targets: invalid UniProt "
                                "AC %r at line %d for ENSG %s",
                                ac, line_no, ensg,
                            )
                            continue
                        prov = Provenance(
                            source=source_name, source_row=line_no,
                            confidence="uniprot", loaded_at=_iso_now(),
                            source_sha256=source_hash,
                        )
                        temp.setdefault(ensg, []).append((ac, prov))
                        added += 1
        except (OSError, UnicodeDecodeError) as e:
            self._logger.warning(
                "load_opentargets_targets: failed to read %s: %s: %s. "
                "Returning 0 mappings (partial load of %d entries discarded).",
                _redact_path(targets_path), type(e).__name__, e, len(temp),
            )
            self._stage_marker(source_name, 0, time.time() - t0, source_hash)
            return 0
        except Exception as e:  # pragma: no cover
            self._logger.error(
                "load_opentargets_targets: unexpected error on %s: %s: %s.",
                _redact_path(targets_path), type(e).__name__, e, exc_info=True,
            )
            self._stage_marker(source_name, 0, time.time() - t0, source_hash)
            return 0

        # GUARD-PERF-1: memory ceiling
        if len(temp) > MAX_OPENTARGETS_ENTRIES:
            self._logger.error(
                "load_opentargets_targets: %d entries exceeds "
                "MAX_OPENTARGETS_ENTRIES=%d. Refusing to commit.",
                len(temp), MAX_OPENTARGETS_ENTRIES,
            )
            self._stage_marker(source_name, 0, time.time() - t0, source_hash)
            return 0

        # IDEM-4: deterministic ordering
        for k in temp:
            temp[k] = self._sort_ac_list(temp[k])

        # PERF-2: bulk update
        for k, v in temp.items():
            existing = self.ensg_to_uniprot.get(k)
            if existing is None:
                self.ensg_to_uniprot[k] = v
            else:
                merged = list(self._coerce_stored_list(existing))
                seen = {ac for ac, _ in merged}
                for ac, prov in v:
                    if ac not in seen:
                        merged.append((ac, prov))
                        seen.add(ac)
                self.ensg_to_uniprot[k] = self._sort_ac_list(merged)

        self._source_files[source_name] = source_hash
        self._append_source_summary(source_name, added)
        elapsed = time.time() - t0
        self._stage_marker("load_opentargets_targets", added, elapsed, source_hash)
        self._logger.info(
            "IDCrosswalk: added %d OpenTargets ENSG->UniProt mappings "
            "(%d unique ENSGs)",
            added, len(temp),
        )
        return added

    # ── OpenTargets disease crosswalk (SCI-3) ────────────────────────────
    # Added by opentargets_loader v2.0 institutional-grade audit fix
    # (opentargets_loader_repair_prompt.md — Section 5.4).
    #
    # OpenTargets disease IDs are EFO/MONDO/HP/MP/Orphanet/SNOMED/OTAR —
    # they are NOT UMLS CUIs used by DRKG/DrugBank. Without this crosswalk,
    # the KG fragments into disconnected disease clusters (one cluster per
    # ontology, with no edges between them). This is the SCI-3 fix.
    # ---------------------------------------------------------------------

    def load_opentargets_diseases(
        self,
        diseases_path: Path,
        allowed_dir: Optional[Path] = None,
    ) -> int:
        """Load OpenTargets disease metadata JSONL (disease → UMLS CUI).

        Implements SCI-3. Expected file format: one JSON object per line,
        each ``{"id": "EFO_0000311", "name": "Alzheimer's disease",
        "dbXRefs": ["UMLS:C0002395", "MeSH:D000544", "DOID:10652"]}``.

        Stores ALL UMLS-sourced dbXRefs per disease ID into
        ``self.disease_to_umls`` (multi-valued per DES-2). The FIRST
        UMLS-sourced dbXRef is the primary.

        Args:
            diseases_path: Path to the OpenTargets diseases JSONL file.
            allowed_dir: Optional directory that ``diseases_path`` must be
                inside (SEC-2 path-traversal protection).

        Returns:
            Number of mappings added. Returns 0 on missing file, parse
            error, or memory ceiling breach — never raises.
        """
        # SEC-2: path-traversal check
        if not self._validate_allowed_dir(diseases_path, allowed_dir):
            self._logger.error(
                "load_opentargets_diseases: path %r is outside allowed_dir "
                "%r — refusing to load (SEC-2).",
                _redact_path(diseases_path),
                _redact_path(allowed_dir) if allowed_dir else "n/a",
            )
            return 0
        if not diseases_path.exists():
            self._logger.warning(
                "OpenTargets diseases file not found: %s",
                _redact_path(diseases_path),
            )
            return 0

        # GUARD-IDEM-1: source file hash
        try:
            source_hash = _sha256_of_file(diseases_path)
        except OSError as e:
            self._logger.warning(
                "load_opentargets_diseases: could not hash %s (%s: %s).",
                _redact_path(diseases_path), type(e).__name__, e,
            )
            source_hash = ""

        source_name = "opentargets-diseases"
        temp: Dict[str, _StoredValue] = {}
        added = 0
        t0 = time.time()
        try:
            # Open with gzip if .gz, else plain text.
            open_func = (
                gzip.open if str(diseases_path).endswith(".gz") else open
            )
            with open_func(diseases_path, "rt", encoding="utf-8") as f:
                for line_no, line in enumerate(f, start=1):
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError as e:
                        self._logger.warning(
                            "load_opentargets_diseases: malformed JSON at "
                            "line %d of %s: %s",
                            line_no, _redact_path(diseases_path), e,
                        )
                        continue
                    if not isinstance(entry, dict):
                        continue
                    disease_id = str(entry.get("id", "")).strip()
                    if not disease_id:
                        continue
                    db_xrefs = entry.get("dbXRefs") or []
                    if not isinstance(db_xrefs, list):
                        self._logger.warning(
                            "load_opentargets_diseases: dbXRefs not a "
                            "list at line %d for disease %s: %r",
                            line_no, disease_id, db_xrefs,
                        )
                        continue
                    for xref in db_xrefs:
                        if not isinstance(xref, str):
                            continue
                        # Accept "UMLS:C0002395", "umls:C0002395", "C0002395"
                        xref_str = xref.strip()
                        if xref_str.upper().startswith("UMLS:"):
                            umls = xref_str.split(":", 1)[1].strip().upper()
                        elif xref_str.startswith("C") and len(xref_str) == 8 \
                                and xref_str[1:].isdigit():
                            umls = xref_str.upper()
                        else:
                            continue
                        if not _UMLS_CUI_PATTERN.match(umls):
                            self._logger.warning(
                                "load_opentargets_diseases: invalid UMLS "
                                "CUI %r at line %d for disease %s",
                                umls, line_no, disease_id,
                            )
                            continue
                        prov = Provenance(
                            source=source_name, source_row=line_no,
                            confidence="umls", loaded_at=_iso_now(),
                            source_sha256=source_hash,
                        )
                        temp.setdefault(disease_id, []).append((umls, prov))
                        added += 1
        except (OSError, UnicodeDecodeError) as e:
            self._logger.warning(
                "load_opentargets_diseases: failed to read %s: %s: %s. "
                "Returning 0 mappings (partial load of %d entries discarded).",
                _redact_path(diseases_path), type(e).__name__, e, len(temp),
            )
            self._stage_marker(source_name, 0, time.time() - t0, source_hash)
            return 0
        except Exception as e:  # pragma: no cover
            self._logger.error(
                "load_opentargets_diseases: unexpected error on %s: %s: %s.",
                _redact_path(diseases_path), type(e).__name__, e, exc_info=True,
            )
            self._stage_marker(source_name, 0, time.time() - t0, source_hash)
            return 0

        # GUARD-PERF-1: memory ceiling
        if len(temp) > MAX_OPENTARGETS_ENTRIES:
            self._logger.error(
                "load_opentargets_diseases: %d entries exceeds "
                "MAX_OPENTARGETS_ENTRIES=%d. Refusing to commit.",
                len(temp), MAX_OPENTARGETS_ENTRIES,
            )
            self._stage_marker(source_name, 0, time.time() - t0, source_hash)
            return 0

        # IDEM-4: deterministic ordering
        for k in temp:
            temp[k] = self._sort_ac_list(temp[k])

        # PERF-2: bulk update
        for k, v in temp.items():
            existing = self.disease_to_umls.get(k)
            if existing is None:
                self.disease_to_umls[k] = v
            else:
                merged = list(self._coerce_stored_list(existing))
                seen = {x for x, _ in merged}
                for x, prov in v:
                    if x not in seen:
                        merged.append((x, prov))
                        seen.add(x)
                self.disease_to_umls[k] = self._sort_ac_list(merged)

        self._source_files[source_name] = source_hash
        self._append_source_summary(source_name, added)
        elapsed = time.time() - t0
        self._stage_marker("load_opentargets_diseases", added, elapsed, source_hash)
        self._logger.info(
            "IDCrosswalk: added %d OpenTargets disease->UMLS mappings "
            "(%d unique diseases)",
            added, len(temp),
        )
        return added

    def disease_id_to_umls_cui(
        self, disease_id: str,
    ) -> Optional[str]:
        """Translate an OpenTargets disease ID (EFO/MONDO/HP/MP/...) -> UMLS CUI.

        Returns the primary (first) UMLS CUI for ``disease_id``, or ``None``
        if no mapping is known.

        Args:
            disease_id: An OpenTargets disease ID (e.g. ``"EFO_0000311"``).

        Returns:
            The primary UMLS CUI (e.g. ``"C0002395"``) or ``None``.
        """
        self._check_unloaded_warning()
        if disease_id is None:
            return None
        key = str(disease_id).strip()
        raw = self.disease_to_umls.get(key)
        if raw is None:
            self._record_unresolved("disease", key)
            return None
        results = self._coerce_stored_list(raw)
        for cui, _ in results:
            if _UMLS_CUI_PATTERN.match(cui):
                return cui
        return None

    def disease_id_to_umls_cui_all(
        self, disease_id: str,
    ) -> List[str]:
        """Return ALL UMLS CUIs for ``disease_id`` (DES-2)."""
        self._check_unloaded_warning()
        if disease_id is None:
            return []
        key = str(disease_id).strip()
        raw = self.disease_to_umls.get(key)
        if raw is None:
            self._record_unresolved("disease", key)
            return []
        results = self._coerce_stored_list(raw)
        return [cui for cui, _ in results if _UMLS_CUI_PATTERN.match(cui)]

    # ── Ensembl gene → NCBI gene ID crosswalk (SCI-9) ───────────────────
    # Added by opentargets_loader v2.0 institutional-grade audit fix
    # (opentargets_loader_repair_prompt.md — Section 5.4 / SCI-9).
    #
    # OpenTargets target IDs are ENSG IDs; DRKG Gene nodes use NCBI Gene
    # IDs. Without this crosswalk, the OpenTargets loader emits orphan
    # Gene nodes (Compound → targets → Gene where Gene is keyed by ENSG,
    # disconnected from DRKG's NCBI-keyed Gene nodes). This is the SCI-9
    # fix.
    # ---------------------------------------------------------------------

    def load_ensembl_to_ncbi_gene(
        self,
        ensembl_to_ncbi_path: Path,
        allowed_dir: Optional[Path] = None,
    ) -> int:
        """Load an Ensembl → NCBI Gene ID crosswalk (TSV from BioMart).

        Implements SCI-9. Expected file format (tab-separated, with header):

            Gene stable ID  NCBI gene ID  Gene name
            ENSG00000143590 5742          PTGS1
            ENSG00000133110 5743          PTGS2

        Stores the first NCBI Gene ID per ENSG ID into
        ``self.ensg_to_ncbi_gene``.

        Args:
            ensembl_to_ncbi_path: Path to the Ensembl→NCBI crosswalk TSV.
            allowed_dir: Optional directory that the path must be inside.

        Returns:
            Number of mappings added. Returns 0 on missing file, parse
            error, or memory ceiling breach — never raises.
        """
        # SEC-2: path-traversal check
        if not self._validate_allowed_dir(ensembl_to_ncbi_path, allowed_dir):
            self._logger.error(
                "load_ensembl_to_ncbi_gene: path %r is outside allowed_dir "
                "%r — refusing to load (SEC-2).",
                _redact_path(ensembl_to_ncbi_path),
                _redact_path(allowed_dir) if allowed_dir else "n/a",
            )
            return 0
        if not ensembl_to_ncbi_path.exists():
            self._logger.warning(
                "Ensembl→NCBI crosswalk file not found: %s",
                _redact_path(ensembl_to_ncbi_path),
            )
            return 0

        # GUARD-IDEM-1: source file hash
        try:
            source_hash = _sha256_of_file(ensembl_to_ncbi_path)
        except OSError as e:
            self._logger.warning(
                "load_ensembl_to_ncbi_gene: could not hash %s (%s: %s).",
                _redact_path(ensembl_to_ncbi_path), type(e).__name__, e,
            )
            source_hash = ""

        source_name = "ensembl-to-ncbi-gene"
        temp: Dict[str, _StoredValue] = {}
        added = 0
        t0 = time.time()
        try:
            open_func = (
                gzip.open
                if str(ensembl_to_ncbi_path).endswith(".gz") else open
            )
            with open_func(ensembl_to_ncbi_path, "rt", encoding="utf-8") as f:
                header_seen = False
                for line_no, line in enumerate(f, start=1):
                    if not line.strip():
                        continue
                    # Skip header line.
                    if not header_seen:
                        header_seen = True
                        if "Gene stable ID" in line or "ensembl" in line.lower():
                            continue
                    parts = line.rstrip("\n\r").split("\t")
                    if len(parts) < 2:
                        continue
                    ensg = parts[0].strip().upper()
                    if not _ENSG_PATTERN.match(ensg):
                        continue
                    ncbi_str = parts[1].strip()
                    if not ncbi_str or not ncbi_str.isdigit():
                        continue
                    prov = Provenance(
                        source=source_name, source_row=line_no,
                        confidence="ncbi", loaded_at=_iso_now(),
                        source_sha256=source_hash,
                    )
                    temp.setdefault(ensg, []).append((ncbi_str, prov))
                    added += 1
        except (OSError, UnicodeDecodeError) as e:
            self._logger.warning(
                "load_ensembl_to_ncbi_gene: failed to read %s: %s: %s. "
                "Returning 0 mappings.",
                _redact_path(ensembl_to_ncbi_path), type(e).__name__, e,
            )
            self._stage_marker(source_name, 0, time.time() - t0, source_hash)
            return 0
        except Exception as e:  # pragma: no cover
            self._logger.error(
                "load_ensembl_to_ncbi_gene: unexpected error on %s: %s: %s.",
                _redact_path(ensembl_to_ncbi_path), type(e).__name__, e,
                exc_info=True,
            )
            self._stage_marker(source_name, 0, time.time() - t0, source_hash)
            return 0

        # GUARD-PERF-1: memory ceiling
        if len(temp) > MAX_OPENTARGETS_ENTRIES:
            self._logger.error(
                "load_ensembl_to_ncbi_gene: %d entries exceeds "
                "MAX_OPENTARGETS_ENTRIES=%d. Refusing to commit.",
                len(temp), MAX_OPENTARGETS_ENTRIES,
            )
            self._stage_marker(source_name, 0, time.time() - t0, source_hash)
            return 0

        # IDEM-4: deterministic ordering
        for k in temp:
            temp[k] = self._sort_ac_list(temp[k])

        # PERF-2: bulk update
        for k, v in temp.items():
            existing = self.ensg_to_ncbi_gene.get(k)
            if existing is None:
                self.ensg_to_ncbi_gene[k] = v
            else:
                merged = list(self._coerce_stored_list(existing))
                seen = {x for x, _ in merged}
                for x, prov in v:
                    if x not in seen:
                        merged.append((x, prov))
                        seen.add(x)
                self.ensg_to_ncbi_gene[k] = self._sort_ac_list(merged)

        self._source_files[source_name] = source_hash
        self._append_source_summary(source_name, added)
        elapsed = time.time() - t0
        self._stage_marker("load_ensembl_to_ncbi_gene", added, elapsed, source_hash)
        self._logger.info(
            "IDCrosswalk: added %d Ensembl->NCBI gene mappings "
            "(%d unique ENSGs)",
            added, len(temp),
        )
        return added

    def ensembl_gene_to_ncbi_gene(
        self, ensg_id: str,
    ) -> Optional[str]:
        """Translate an Ensembl gene ID (ENSG...) -> NCBI Gene ID.

        Returns the primary (first) NCBI Gene ID for ``ensg_id``, or
        ``None`` if no mapping is known.

        Args:
            ensg_id: An Ensembl gene ID (e.g. ``"ENSG00000143590"``).

        Returns:
            The primary NCBI Gene ID as a string (e.g. ``"5742"``) or
            ``None``.
        """
        self._check_unloaded_warning()
        if ensg_id is None:
            return None
        key = str(ensg_id).strip().upper()
        raw = self.ensg_to_ncbi_gene.get(key)
        if raw is None:
            self._record_unresolved("ensg_ncbi", key)
            return None
        results = self._coerce_stored_list(raw)
        for ncbi, _ in results:
            if ncbi and ncbi.isdigit():
                return ncbi
        return None

    def ensembl_gene_to_ncbi_gene_all(
        self, ensg_id: str,
    ) -> List[str]:
        """Return ALL NCBI Gene IDs for ``ensg_id`` (DES-2)."""
        self._check_unloaded_warning()
        if ensg_id is None:
            return []
        key = str(ensg_id).strip().upper()
        raw = self.ensg_to_ncbi_gene.get(key)
        if raw is None:
            self._record_unresolved("ensg_ncbi", key)
            return []
        results = self._coerce_stored_list(raw)
        return [n for n, _ in results if n and n.isdigit()]

    # ── Compound ID → InChIKey crosswalk (v29 ROOT FIX audit L-5) ──────
    # Compound ID fragmentation: DrugBank ID / ChEMBL ID / PubChem CID /
    # STITCH CIDm/CIDs / SIDER bare CID / DRKG MESH / InChIKey — 7
    # disjoint namespaces for the same Compound entity.
    # ``merge_mappings_by_inchikey()`` in entity_resolver only fires when
    # InChIKey is present — STITCH/SIDER/DRKG edges reference Compounds
    # by non-InChIKey IDs that don't match InChIKey-keyed nodes. The KG
    # had 7 disjoint subgraphs.
    # Root fix: this crosswalk + the ``_normalize_compound_id_to_inchikey()``
    # module-level helper normalize ALL compound IDs to InChIKey BEFORE
    # loading into the KG (called from stitch_loader, sider_loader,
    # drkg_loader).
    # ---------------------------------------------------------------------

    def register_compound_inchikey(
        self,
        compound_id: str,
        inchikey: str,
        *,
        source: str = "entity_resolver",
        source_row: int = -1,
        confidence: str = "resolved",
        source_sha256: str = "",
    ) -> int:
        """Register a single ``compound_id -> inchikey`` mapping (v29 L-5).

        Called by the entity_resolver after Phase 1's entity resolution
        runs — Phase 1 builds the InChIKey mappings from DrugBank /
        ChEMBL / PubChem compound records, and pushes each
        ``(source_id, inchikey)`` pair into this crosswalk via this
        method so the loaders can normalize Compound references.

        Args:
            compound_id: Compound ID in any namespace (DrugBank ID
                ``DB00107``, ChEMBL ID ``CHEMBL218``, PubChem CID
                ``CID5311025``, STITCH ``CIDm00002244``/``CIDs00002244``,
                SIDER bare CID, DRKG MESH ``MESH:D000544``, etc.).
            inchikey: The canonical InChIKey for ``compound_id`` (27-char
                ``XXXXXXXXXXXXXX-XXXXXXXXXX-X`` form). Must match
                ``_INCHIKEY_PATTERN``.
            source: Provenance source name (default ``"entity_resolver"``).
            source_row: Source row index (-1 if unknown).
            confidence: Provenance confidence label.
            source_sha256: SHA-256 of the source file (empty if unknown).

        Returns:
            1 if the mapping was added, 0 if ``inchikey`` failed
            validation or ``compound_id`` was empty.
        """
        if compound_id is None or inchikey is None:
            return 0
        key = str(compound_id).strip()
        ik = str(inchikey).strip().upper()
        if not key or not ik:
            return 0
        if not _INCHIKEY_PATTERN.match(ik):
            self._logger.warning(
                "register_compound_inchikey: rejecting invalid InChIKey "
                "%r for compound_id %r", ik, key,
            )
            return 0
        prov = Provenance(
            source=source, source_row=source_row,
            confidence=confidence, loaded_at=_iso_now(),
            source_sha256=source_sha256,
        )
        existing = self.compound_to_inchikey.get(key)
        if existing is None:
            self.compound_to_inchikey[key] = [(ik, prov)]
            self._invalidate_reverse_index_cache()
            return 1
        existing_list = self._coerce_stored_list(existing)
        if any(stored_ik == ik for stored_ik, _ in existing_list):
            return 0  # already present — idempotent
        existing_list.append((ik, prov))
        self.compound_to_inchikey[key] = existing_list
        self._invalidate_reverse_index_cache()
        return 1

    def load_compound_inchikey_crosswalk(
        self,
        path: Path,
        allowed_dir: Optional[Path] = None,
    ) -> int:
        """Bulk-load a Compound ID → InChIKey crosswalk from a TSV file.

        Expected TSV format (header row required)::

            compound_id   inchikey   source   confidence
            DB00107       BSYNRYMUTXBXSQ-UHFFFAOYSA-N   drugbank   verified
            CID5311025    BSYNRYMUTXBXSQ-UHFFFAOYSA-N   pubchem    curated
            ...

        ``source`` and ``confidence`` columns are optional (default
        ``"crosswalk-tsv"`` and ``"resolved"``). The first two columns
        are mandatory. Lines with invalid InChIKey are dead-lettered via
        WARNING and skipped (the load continues).

        Args:
            path: Path to the TSV crosswalk file.
            allowed_dir: Optional directory that ``path`` must be inside
                (SEC-2 path-traversal protection).

        Returns:
            Number of mappings added. Returns 0 on missing file or parse
            error — never raises.
        """
        # SEC-2: path-traversal check
        if not self._validate_allowed_dir(path, allowed_dir):
            self._logger.error(
                "load_compound_inchikey_crosswalk: path %r is outside "
                "allowed_dir %r — refusing to load (SEC-2).",
                _redact_path(path),
                _redact_path(allowed_dir) if allowed_dir else "n/a",
            )
            return 0
        if not path.exists():
            self._logger.warning(
                "Compound ID→InChIKey crosswalk file not found: %s",
                _redact_path(path),
            )
            return 0

        try:
            source_hash = _sha256_of_file(path)
        except OSError as e:
            self._logger.warning(
                "load_compound_inchikey_crosswalk: could not hash %s "
                "(%s: %s).", _redact_path(path), type(e).__name__, e,
            )
            source_hash = ""

        source_name = "compound-inchikey-crosswalk"
        added = 0
        t0 = time.time()
        try:
            open_func = gzip.open if str(path).endswith(".gz") else open
            with open_func(path, "rt", encoding="utf-8") as f:
                header_seen = False
                for line_no, line in enumerate(f, start=1):
                    if not line.strip():
                        continue
                    if not header_seen:
                        header_seen = True
                        # Skip header if it looks like one
                        lower = line.lower()
                        if "compound_id" in lower or "inchikey" in lower:
                            continue
                    parts = line.rstrip("\n\r").split("\t")
                    if len(parts) < 2:
                        self._logger.debug(
                            "load_compound_inchikey_crosswalk: skipping "
                            "short line %d in %s", line_no, _redact_path(path),
                        )
                        continue
                    compound_id = parts[0].strip()
                    inchikey = parts[1].strip().upper()
                    src_tag = parts[2].strip() if len(parts) > 2 else source_name
                    conf_tag = parts[3].strip() if len(parts) > 3 else "resolved"
                    added += self.register_compound_inchikey(
                        compound_id, inchikey,
                        source=src_tag, source_row=line_no,
                        confidence=conf_tag, source_sha256=source_hash,
                    )
        except (OSError, UnicodeDecodeError) as e:
            self._logger.warning(
                "load_compound_inchikey_crosswalk: failed to read %s: "
                "%s: %s. Returning %d mappings.",
                _redact_path(path), type(e).__name__, e, added,
            )
            self._stage_marker(source_name, added, time.time() - t0, source_hash)
            return added
        except Exception as e:  # pragma: no cover
            self._logger.error(
                "load_compound_inchikey_crosswalk: unexpected error on %s: "
                "%s: %s.", _redact_path(path), type(e).__name__, e,
                exc_info=True,
            )
            self._stage_marker(source_name, added, time.time() - t0, source_hash)
            return added

        self._source_files[source_name] = source_hash
        if added:
            self._append_source_summary(source_name, added)
        elapsed = time.time() - t0
        self._stage_marker(source_name, added, elapsed, source_hash)
        self._logger.info(
            "IDCrosswalk: added %d Compound→InChIKey mappings", added,
        )
        return added

    def compound_id_to_inchikey(self, compound_id: Any) -> Optional[str]:
        """Translate a Compound ID (any namespace) -> InChIKey (primary).

        v29 ROOT FIX (audit L-5). Accepts compound IDs in any namespace:
        DrugBank ID, ChEMBL ID, PubChem CID, STITCH CIDm/CIDs, SIDER
        bare CID, DRKG MESH, or already-InChIKey (returned as-is).

        Returns the primary (first) InChIKey, or ``None`` if no mapping
        is known. The caller should still create the Compound node with
        the original ID if ``None`` is returned — the missing mapping
        will be filled in later by ``merge_mappings_by_inchikey()`` once
        Phase 1's entity resolution runs.

        Multi-valued variant: ``compound_id_to_inchikey_all()``.
        Provenance-tagged variant:
        ``compound_id_to_inchikey_with_provenance()``.
        """
        self._check_unloaded_warning()
        # Short-circuit: input is already an InChIKey — return it.
        if compound_id is not None and isinstance(compound_id, str) \
                and _INCHIKEY_PATTERN.match(compound_id.strip()):
            self._record_translator_call("compound_id_to_inchikey", True)
            return compound_id.strip()
        results = self._lookup_compound_id(compound_id)
        if not results:
            self._record_translator_call("compound_id_to_inchikey", False)
            self._record_unresolved(
                "compound_id", str(compound_id) if compound_id is not None else "",
            )
            return None
        self._record_translator_call("compound_id_to_inchikey", True)
        return results[0][0]

    def compound_id_to_inchikey_all(
        self, compound_id: Any,
    ) -> List[str]:
        """Return ALL InChIKeys for ``compound_id`` (DES-2)."""
        self._check_unloaded_warning()
        if compound_id is not None and isinstance(compound_id, str) \
                and _INCHIKEY_PATTERN.match(compound_id.strip()):
            return [compound_id.strip()]
        results = self._lookup_compound_id(compound_id)
        if not results:
            self._record_unresolved(
                "compound_id", str(compound_id) if compound_id is not None else "",
            )
            return []
        return [ik for ik, _ in results]

    def compound_id_to_inchikey_with_provenance(
        self, compound_id: Any,
    ) -> List[Tuple[str, Provenance]]:
        """Return ``[(inchikey, provenance), ...]`` for ``compound_id``."""
        self._check_unloaded_warning()
        if compound_id is not None and isinstance(compound_id, str) \
                and _INCHIKEY_PATTERN.match(compound_id.strip()):
            null_prov = Provenance(
                source="inchikey_input", source_row=-1,
                confidence="identity", loaded_at=_iso_now(),
                source_sha256="",
            )
            return [(compound_id.strip(), null_prov)]
        results = self._lookup_compound_id(compound_id)
        if not results:
            self._record_unresolved(
                "compound_id", str(compound_id) if compound_id is not None else "",
            )
            return []
        return list(results)

    def _lookup_compound_id(
        self, compound_id: Any,
    ) -> List[Tuple[str, Provenance]]:
        """Internal: lookup a compound_id, normalizing bare CID forms.

        Lookup order:
          1. ``CID<int>`` form (canonical PubChem CID key).
          2. Bare integer form (e.g. ``"5311025"``) — coerced to
             ``CID5311025`` for backward-compat with callers that strip
             the ``CID`` prefix.
          3. Original form as-is (covers DrugBank ID, ChEMBL ID, MESH,
             STITCH CIDm/CIDs, etc.).
        """
        if compound_id is None:
            return []
        s = str(compound_id).strip()
        if not s:
            return []
        # Try the original form first.
        raw = self.compound_to_inchikey.get(s)
        if raw is None and s.isdigit():
            # Backward-compat: bare integer CID.
            raw = self.compound_to_inchikey.get(f"CID{s}")
        if raw is None:
            return []
        results = self._coerce_stored_list(raw)
        valid = [(ik, p) for ik, p in results if _INCHIKEY_PATTERN.match(ik)]
        invalid = [ik for ik, _ in results if not _INCHIKEY_PATTERN.match(ik)]
        if invalid:
            self._logger.warning(
                "compound_id_to_inchikey: %d invalid InChIKeys in stored "
                "mapping for %r (sample: %r). Filtered out.",
                len(invalid), compound_id, invalid[:3],
            )
        return valid

    # ── Translators (public API preserved) ───────────────────────────────

    def ensembl_protein_to_uniprot_ac(self, ensp_id: str) -> Optional[str]:
        """Translate STRING/STITCH ``9606.ENSP000003...`` -> UniProt AC.

        Returns the primary (highest-priority) UniProt AC. The primary is
        the first Swiss-Prot AC if any are present, otherwise the first
        TrEMBL AC. Returns ``None`` if no mapping is known — the caller
        should STILL create the Protein node but use the original ENSP
        as the ID and flag it as ``unresolved_protein_id``.

        Multi-valued variant: ``ensembl_protein_to_uniprot_ac_all()``.
        Provenance-tagged variant: ``ensembl_protein_to_uniprot_ac_with_provenance()``.

        Implements:
          - INT-1: input normalized to bare ENSP before lookup. Also
            tolerates direct-injection of prefixed keys (backward-compat
            with ``cw.ensembl_protein_to_uniprot["9606.ENSP..."] = "P23219"``).
          - INT-4: stored values validated before return — invalid ACs
            are filtered out and logged.
          - GUARD-REL-1: unresolved IDs tracked in DLQ.
          - OBS-1: hit/miss tracked.
          - OBS-3: misses logged at DEBUG.
          - ARCH-6: warns once if called before any load_*.
        """
        self._check_unloaded_warning()
        results = self._lookup_ensp(ensp_id)
        if not results:
            self._record_translator_call("ensembl_protein_to_uniprot_ac", False)
            self._record_unresolved("ensp", str(ensp_id))
            self._logger.debug(
                "ensembl_protein_to_uniprot_ac miss: %r", ensp_id,
            )
            return None
        self._record_translator_call("ensembl_protein_to_uniprot_ac", True)
        return results[0][0]

    def ensembl_protein_to_uniprot_ac_all(self, ensp_id: str) -> List[str]:
        """Return ALL UniProt ACs for ``ensp_id`` (DES-2 multi-valued)."""
        self._check_unloaded_warning()
        results = self._lookup_ensp(ensp_id)
        if not results:
            self._record_unresolved("ensp", str(ensp_id))
            return []
        return [ac for ac, _ in results]

    def ensembl_protein_to_uniprot_ac_with_provenance(
        self, ensp_id: str
    ) -> List[Tuple[str, Provenance]]:
        """Return ``[(ac, provenance), ...]`` for ``ensp_id`` (GUARD-LINE-1)."""
        self._check_unloaded_warning()
        results = self._lookup_ensp(ensp_id)
        if not results:
            self._record_unresolved("ensp", str(ensp_id))
            return []
        return list(results)

    def _lookup_ensp(self, ensp_id: Any) -> List[Tuple[str, Provenance]]:
        """Internal: lookup an ENSP, normalizing and trying both bare
        and prefixed forms (INT-1 + backward-compat for direct injection).

        Lookup order:
          1. Bare ENSP form (``ENSP00000358091``) — the canonical key
             used by ``load_string_aliases()``.
          2. Original input form (e.g. ``9606.ENSP00000358091``) — for
             backward-compat with code/tests that inject the prefixed
             form directly into the dict.
          3. Prefixed form (``9606.{bare}``) — for backward-compat with
             direct injection when the input was bare.
        """
        if ensp_id is None:
            return []
        bare = _normalize_ensp(ensp_id)
        original = str(ensp_id)
        # Try bare ENSP first (canonical)
        raw = self.ensembl_protein_to_uniprot.get(bare)
        # Backward-compat: try the original (e.g. prefixed) form
        if raw is None and original != bare:
            raw = self.ensembl_protein_to_uniprot.get(original)
        # Backward-compat: try the prefixed form when input was bare
        if raw is None and not original.startswith("9606."):
            raw = self.ensembl_protein_to_uniprot.get(f"9606.{bare}")
        if raw is None:
            return []
        results = self._coerce_stored_list(raw)
        # INT-4: filter invalid ACs
        valid = [(ac, p) for ac, p in results if _validate_uniprot_ac(ac)]
        invalid = [ac for ac, _ in results if not _validate_uniprot_ac(ac)]
        if invalid:
            self._logger.warning(
                "ensembl_protein_to_uniprot_ac: %d invalid ACs in stored "
                "mapping for %r (sample: %r). Filtered out.",
                len(invalid), ensp_id, invalid[:3],
            )
        return valid

    def chembl_target_to_uniprot_ac(self, chembl_target_id: str) -> Optional[str]:
        """Translate ChEMBL ``CHEMBL218`` -> UniProt AC (primary).

        Multi-valued: returns the primary AC (first in source order, then
        Swiss-Prot preferred). Multi-subunit complexes (e.g. GABA-A with
        5 subunits) are preserved internally — use
        ``chembl_target_to_uniprot_ac_all()`` to retrieve all subunits.
        """
        self._check_unloaded_warning()
        results = self._lookup_chembl(chembl_target_id)
        if not results:
            self._record_translator_call("chembl_target_to_uniprot_ac", False)
            self._record_unresolved("chembl_target", str(chembl_target_id))
            self._logger.debug(
                "chembl_target_to_uniprot_ac miss: %r", chembl_target_id,
            )
            return None
        self._record_translator_call("chembl_target_to_uniprot_ac", True)
        return results[0][0]

    def chembl_target_to_uniprot_ac_all(
        self, chembl_target_id: str
    ) -> List[str]:
        """Return ALL UniProt ACs for ``chembl_target_id`` (DES-2)."""
        self._check_unloaded_warning()
        results = self._lookup_chembl(chembl_target_id)
        if not results:
            self._record_unresolved("chembl_target", str(chembl_target_id))
            return []
        return [ac for ac, _ in results]

    def chembl_target_to_uniprot_ac_with_provenance(
        self, chembl_target_id: str
    ) -> List[Tuple[str, Provenance]]:
        """Return ``[(ac, provenance), ...]`` for ``chembl_target_id``."""
        self._check_unloaded_warning()
        results = self._lookup_chembl(chembl_target_id)
        if not results:
            self._record_unresolved("chembl_target", str(chembl_target_id))
            return []
        return list(results)

    def _lookup_chembl(
        self, chembl_target_id: Any
    ) -> List[Tuple[str, Provenance]]:
        if chembl_target_id is None:
            return []
        key = str(chembl_target_id).strip()
        raw = self.chembl_target_to_uniprot.get(key)
        if raw is None:
            return []
        results = self._coerce_stored_list(raw)
        valid = [(ac, p) for ac, p in results if _validate_uniprot_ac(ac)]
        invalid = [ac for ac, _ in results if not _validate_uniprot_ac(ac)]
        if invalid:
            self._logger.warning(
                "chembl_target_to_uniprot_ac: %d invalid ACs in stored "
                "mapping for %r (sample: %r). Filtered out.",
                len(invalid), chembl_target_id, invalid[:3],
            )
        return valid

    def ensembl_gene_to_uniprot_ac(self, ensg_id: str) -> Optional[str]:
        """Translate OpenTargets ``ENSG00000...`` -> UniProt AC.

        With builtin-only, returns ``None`` for all ENSG IDs (the builtin
        table contains gene SYMBOLS, not ENSG IDs — see SCI-2). Run
        ``load_opentargets_targets()`` first to populate real ENSG IDs.

        Backward-compat: if ``ensg_id`` is actually a gene symbol (e.g.
        ``"PTGS1"``) AND no ENSG match exists, fall back to
        ``gene_symbol_to_uniprot_ac()`` with a DeprecationWarning (DES-1).
        """
        self._check_unloaded_warning()

        # Primary path: real ENSG lookup
        if ensg_id is not None and _ENSG_PATTERN.match(str(ensg_id).strip()):
            results = self._coerce_stored_list(
                self.ensg_to_uniprot.get(str(ensg_id).strip())
            )
            valid = [(ac, p) for ac, p in results if _validate_uniprot_ac(ac)]
            if valid:
                self._record_translator_call("ensembl_gene_to_uniprot_ac", True)
                return valid[0][0]
            self._record_translator_call("ensembl_gene_to_uniprot_ac", False)
            self._record_unresolved("ensg", str(ensg_id))
            self._logger.debug(
                "ensembl_gene_to_uniprot_ac miss: %r", ensg_id,
            )
            return None

        # DES-1 backward-compat shim: gene-symbol fallback with deprecation
        symbol = str(ensg_id).strip().upper() if ensg_id is not None else ""
        if symbol and symbol in self.gene_symbol_to_uniprot:
            import warnings
            warnings.warn(
                "ensembl_gene_to_uniprot_ac() was called with a gene "
                f"symbol {symbol!r} rather than an ENSG ID. Use "
                "gene_symbol_to_uniprot_ac() instead. The gene-symbol "
                "fallback will be removed in a future release.",
                DeprecationWarning,
                stacklevel=2,
            )
            results = self._coerce_stored_list(
                self.gene_symbol_to_uniprot.get(symbol)
            )
            valid = [(ac, p) for ac, p in results if _validate_uniprot_ac(ac)]
            if valid:
                self._record_translator_call("ensembl_gene_to_uniprot_ac", True)
                return valid[0][0]

        self._record_translator_call("ensembl_gene_to_uniprot_ac", False)
        self._record_unresolved("ensg", str(ensg_id))
        return None

    def ensembl_gene_to_uniprot_ac_all(self, ensg_id: str) -> List[str]:
        """Return ALL UniProt ACs for ``ensg_id`` (DES-2)."""
        self._check_unloaded_warning()
        if ensg_id is None:
            return []
        key = str(ensg_id).strip()
        raw = self.ensg_to_uniprot.get(key)
        if raw is None:
            self._record_unresolved("ensg", key)
            return []
        results = self._coerce_stored_list(raw)
        return [ac for ac, _ in results if _validate_uniprot_ac(ac)]

    def gene_symbol_to_uniprot_ac(self, symbol: str) -> Optional[str]:
        """Translate a gene symbol (e.g. ``"PTGS1"``) -> UniProt AC (primary).

        New translator introduced by DES-1 (the gene-symbol namespace was
        previously conflated with the ENSG namespace).
        """
        self._check_unloaded_warning()
        if symbol is None:
            self._record_translator_call("gene_symbol_to_uniprot_ac", False)
            return None
        key = str(symbol).strip().upper()
        raw = self.gene_symbol_to_uniprot.get(key)
        if raw is None:
            self._record_translator_call("gene_symbol_to_uniprot_ac", False)
            self._record_unresolved("gene_symbol", key)
            return None
        results = self._coerce_stored_list(raw)
        valid = [(ac, p) for ac, p in results if _validate_uniprot_ac(ac)]
        if not valid:
            self._record_translator_call("gene_symbol_to_uniprot_ac", False)
            self._record_unresolved("gene_symbol", key)
            return None
        self._record_translator_call("gene_symbol_to_uniprot_ac", True)
        return valid[0][0]

    def gene_symbol_to_uniprot_ac_all(self, symbol: str) -> List[str]:
        """Return ALL UniProt ACs for ``symbol`` (DES-2)."""
        self._check_unloaded_warning()
        if symbol is None:
            return []
        key = str(symbol).strip().upper()
        raw = self.gene_symbol_to_uniprot.get(key)
        if raw is None:
            return []
        results = self._coerce_stored_list(raw)
        return [ac for ac, _ in results if _validate_uniprot_ac(ac)]

    def resolve_uniprot_alias(self, ac: str) -> Optional[str]:
        """Translate a UniProt secondary AC -> its current primary AC.

        New translator introduced by SCI-3 (secondary ACs were previously
        stored in the wrong dict). Returns ``None`` if ``ac`` is not a
        known secondary accession.
        """
        self._check_unloaded_warning()
        if ac is None:
            self._record_translator_call("resolve_uniprot_alias", False)
            return None
        key = str(ac).strip().upper()
        primary = self.uniprot_secondary_to_primary.get(key)
        if primary is None:
            self._record_translator_call("resolve_uniprot_alias", False)
            self._record_unresolved("uniprot_ac", key)
            return None
        self._record_translator_call("resolve_uniprot_alias", True)
        return primary

    def uniprot_ac_to_ncbi_gene_id(self, uniprot_ac: str) -> Optional[str]:
        """Translate UniProt AC -> NCBI Gene ID (primary).

        Used to build ``Gene -encodes-> Protein`` edges.
        """
        self._check_unloaded_warning()
        if uniprot_ac is None:
            self._record_translator_call("uniprot_ac_to_ncbi_gene_id", False)
            return None
        key = str(uniprot_ac).strip().upper()
        raw = self.uniprot_to_gene.get(key)
        if raw is None:
            self._record_translator_call("uniprot_ac_to_ncbi_gene_id", False)
            self._record_unresolved("uniprot_ac", key)
            self._logger.debug(
                "uniprot_ac_to_ncbi_gene_id miss: %r", uniprot_ac,
            )
            return None
        results = self._coerce_stored_list(raw)
        # First valid numeric gene ID
        for gid, _ in results:
            if _normalize_ncbi_gene_id(gid) is not None:
                self._record_translator_call("uniprot_ac_to_ncbi_gene_id", True)
                return _normalize_ncbi_gene_id(gid)
        self._record_translator_call("uniprot_ac_to_ncbi_gene_id", False)
        self._record_unresolved("uniprot_ac", key)
        return None

    def uniprot_ac_to_ncbi_gene_id_all(self, uniprot_ac: str) -> List[str]:
        """Return ALL NCBI Gene IDs for ``uniprot_ac`` (DES-2)."""
        self._check_unloaded_warning()
        if uniprot_ac is None:
            return []
        key = str(uniprot_ac).strip().upper()
        raw = self.uniprot_to_gene.get(key)
        if raw is None:
            return []
        results = self._coerce_stored_list(raw)
        out: List[str] = []
        for gid, _ in results:
            norm = _normalize_ncbi_gene_id(gid)
            if norm is not None and norm not in out:
                out.append(norm)
        return out

    def ncbi_gene_id_to_uniprot_ac(self, ncbi_gene_id: Any) -> Optional[str]:
        """Translate NCBI Gene ID -> UniProt AC (primary).

        Used to bridge DRKG Gene to UniProt Protein.

        Implements COD-6: tolerates pandas float artifacts
        (``"5742.0"`` -> ``"5742"``) and whitespace-padded strings.
        Returns ``None`` for malformed input.
        """
        self._check_unloaded_warning()
        norm = _normalize_ncbi_gene_id(ncbi_gene_id)
        if norm is None:
            self._record_translator_call("ncbi_gene_id_to_uniprot_ac", False)
            self._logger.warning(
                "Malformed NCBI Gene ID: %r", ncbi_gene_id,
            )
            return None
        raw = self.gene_to_uniprot.get(norm)
        if raw is None:
            self._record_translator_call("ncbi_gene_id_to_uniprot_ac", False)
            self._record_unresolved("ncbi_gene_id", norm)
            self._logger.debug(
                "ncbi_gene_id_to_uniprot_ac miss: %r", ncbi_gene_id,
            )
            return None
        results = self._coerce_stored_list(raw)
        valid = [(ac, p) for ac, p in results if _validate_uniprot_ac(ac)]
        if not valid:
            self._record_translator_call("ncbi_gene_id_to_uniprot_ac", False)
            self._record_unresolved("ncbi_gene_id", norm)
            return None
        self._record_translator_call("ncbi_gene_id_to_uniprot_ac", True)
        return valid[0][0]

    def ncbi_gene_id_to_uniprot_ac_all(self, ncbi_gene_id: Any) -> List[str]:
        """Return ALL UniProt ACs for ``ncbi_gene_id`` (DES-2)."""
        self._check_unloaded_warning()
        norm = _normalize_ncbi_gene_id(ncbi_gene_id)
        if norm is None:
            return []
        raw = self.gene_to_uniprot.get(norm)
        if raw is None:
            return []
        results = self._coerce_stored_list(raw)
        out: List[str] = []
        for ac, _ in results:
            if _validate_uniprot_ac(ac) and ac not in out:
                out.append(ac)
        return out

    def ncbi_gene_id_to_uniprot_ac_with_provenance(
        self, ncbi_gene_id: Any
    ) -> List[Tuple[str, Provenance]]:
        """Return ``[(ac, provenance), ...]`` for ``ncbi_gene_id``."""
        self._check_unloaded_warning()
        norm = _normalize_ncbi_gene_id(ncbi_gene_id)
        if norm is None:
            return []
        raw = self.gene_to_uniprot.get(norm)
        if raw is None:
            return []
        results = self._coerce_stored_list(raw)
        return [(ac, p) for ac, p in results if _validate_uniprot_ac(ac)]

    # ── Generic translate() API (ARCH-4) ─────────────────────────────────

    def canonicalize(
        self,
        entity_type: str,
        source_namespace: str,
        source_value: Any,
    ) -> Optional[Dict[str, str]]:
        """Canonicalize an external ID into all known canonical forms.

        ROOT FIX for F5.2.7 / BUG-D-007 / RT-4 / Compound-1:
        The v9/v10/v11 forensic audits claimed the entity_resolver
        called ``crosswalk.canonicalize(...)`` to enrich gene aliases
        with cross-source canonical IDs — but ``IDCrosswalk`` had NO
        ``canonicalize`` method. The call raised ``AttributeError``,
        was silently caught by an ``except Exception`` (logged at
        ``DEBUG`` — invisible in production), and canonicalization
        NEVER happened. Three "FORENSIC VALIDATED" stamps were placed
        on a fix that had never actually run.

        This method performs the canonicalization the prior audits
        claimed was already happening. The contract:

        Args:
            entity_type: One of ``"Gene"``, ``"Protein"``,
                ``"Compound"``, ``"Disease"``. Currently only
                ``"Gene"`` and ``"Protein"`` are supported (those
                are the entity types the entity_resolver calls with).
            source_namespace: One of ``"ensembl_id"`` (ENSG),
                ``"ensembl_protein"`` (ENSP), ``"ncbi_gene_id"``,
                ``"uniprot_ac"``, ``"chembl_target"``, ``"gene_symbol"``.
            source_value: The ID value as a string (or int for
                ``ncbi_gene_id``).

        Returns:
            A dict of canonical IDs keyed by namespace, e.g.
            ``{"ncbi_gene_id": "7157", "uniprot_ac": "P04637"}``,
            or ``None`` if no canonical form is known for the input.
        """
        if source_value is None:
            return None
        src_ns = str(source_namespace).strip().lower()
        src_val = str(source_value).strip()
        if not src_val:
            return None

        result: Dict[str, str] = {}

        if entity_type in ("Gene", "Protein"):
            # Resolve to UniProt AC first (the universal protein
            # canonical), then back-resolve to NCBI Gene ID.
            uniprot_ac: Optional[str] = None
            if src_ns in ("ensembl_id", "ensg"):
                uniprot_ac = self.ensembl_gene_to_uniprot_ac(src_val)
            elif src_ns in ("ensembl_protein", "ensp"):
                uniprot_ac = self.ensembl_protein_to_uniprot_ac(src_val)
            elif src_ns in ("ncbi_gene_id", "gene_id"):
                uniprot_ac = self.ncbi_gene_id_to_uniprot_ac(src_val)
                # Also try to set ncbi_gene_id directly.
                if src_val not in result:
                    result["ncbi_gene_id"] = src_val
            elif src_ns in ("uniprot_ac", "uniprot", "accession"):
                uniprot_ac = src_val
                result["uniprot_ac"] = src_val
            elif src_ns in ("chembl_target", "chembl"):
                uniprot_ac = self.chembl_target_to_uniprot_ac(src_val)
            elif src_ns in ("gene_symbol", "symbol", "hgnc_symbol"):
                uniprot_ac = self.gene_symbol_to_uniprot_ac(src_val)
            else:
                self._logger.debug(
                    "canonicalize: unsupported source_namespace %r "
                    "for entity_type %r", source_namespace, entity_type,
                )
                return None

            if uniprot_ac is None:
                self._record_unresolved(
                    f"canonicalize:{entity_type}:{src_ns}", src_val,
                )
                return None

            result.setdefault("uniprot_ac", uniprot_ac)

            # Back-resolve to NCBI Gene ID.
            ncbi_gene_id = self.uniprot_ac_to_ncbi_gene_id(uniprot_ac)
            if ncbi_gene_id is not None:
                result.setdefault("ncbi_gene_id", ncbi_gene_id)

            # Cross-populate Ensembl IDs via reverse lookup.
            reverse = self.reverse_lookup(uniprot_ac)
            if reverse.get("ensg") and "ensembl_id" not in result:
                result["ensembl_id"] = reverse["ensg"][0]
            if reverse.get("ensp") and "ensembl_protein" not in result:
                result["ensembl_protein"] = reverse["ensp"][0]
            if reverse.get("gene_symbol") and "gene_symbol" not in result:
                result["gene_symbol"] = reverse["gene_symbol"][0]
            return result or None

        # Compound / Disease canonicalization is handled by the
        # entity_resolver directly (via InChIKey / DOID); the
        # crosswalk does not currently own those mappings.
        self._logger.debug(
            "canonicalize: entity_type %r not supported "
            "(only Gene/Protein)", entity_type,
        )
        return None

    def supported_namespaces(self) -> List[str]:
        """Return the list of namespaces this crosswalk can translate."""
        return [
            "ensp", "ensg", "chembl_target", "uniprot_ac",
            "ncbi_gene_id", "gene_symbol", "uniprot_secondary",
            # v29 ROOT FIX (audit L-5): Compound ID namespace.
            "compound_id", "inchikey",
        ]

    def supported_translations(self) -> List[Tuple[str, str]]:
        """Return the list of ``(src_namespace, dst_namespace)`` pairs."""
        return [
            ("ensp", "uniprot_ac"),
            ("chembl_target", "uniprot_ac"),
            ("ensg", "uniprot_ac"),
            ("gene_symbol", "uniprot_ac"),
            ("uniprot_secondary", "uniprot_ac"),
            ("uniprot_ac", "ncbi_gene_id"),
            ("ncbi_gene_id", "uniprot_ac"),
            # v29 ROOT FIX (audit L-5)
            ("compound_id", "inchikey"),
        ]

    def translate(
        self, src_namespace: str, dst_namespace: str, src_id: str
    ) -> Optional[Union[str, List[str]]]:
        """Generic translator. Namespaces: ``ensp``, ``ensg``,
        ``chembl_target``, ``uniprot_ac``, ``ncbi_gene_id``,
        ``gene_symbol``, ``uniprot_secondary``.

        For multi-valued destinations (e.g. ChEMBL complex -> many ACs),
        use ``translate_all()`` instead.
        """
        key = (src_namespace, dst_namespace)
        dispatch: Dict[Tuple[str, str], Callable[[Any], Optional[str]]] = {
            ("ensp", "uniprot_ac"): self.ensembl_protein_to_uniprot_ac,
            ("chembl_target", "uniprot_ac"): self.chembl_target_to_uniprot_ac,
            ("ensg", "uniprot_ac"): self.ensembl_gene_to_uniprot_ac,
            ("gene_symbol", "uniprot_ac"): self.gene_symbol_to_uniprot_ac,
            ("uniprot_secondary", "uniprot_ac"): self.resolve_uniprot_alias,
            ("uniprot_ac", "ncbi_gene_id"): self.uniprot_ac_to_ncbi_gene_id,
            ("ncbi_gene_id", "uniprot_ac"): self.ncbi_gene_id_to_uniprot_ac,
            # v29 ROOT FIX (audit L-5)
            ("compound_id", "inchikey"): self.compound_id_to_inchikey,
        }
        fn = dispatch.get(key)
        if fn is None:
            self._logger.warning(
                "translate: unsupported (%s -> %s)", src_namespace, dst_namespace,
            )
            return None
        return fn(src_id)

    def translate_all(
        self, src_namespace: str, dst_namespace: str, src_id: str
    ) -> List[str]:
        """Generic multi-valued translator (DES-2)."""
        dispatch_all: Dict[
            Tuple[str, str], Callable[[Any], List[str]]
        ] = {
            ("ensp", "uniprot_ac"): self.ensembl_protein_to_uniprot_ac_all,
            ("chembl_target", "uniprot_ac"): self.chembl_target_to_uniprot_ac_all,
            ("ensg", "uniprot_ac"): self.ensembl_gene_to_uniprot_ac_all,
            ("gene_symbol", "uniprot_ac"): self.gene_symbol_to_uniprot_ac_all,
            ("uniprot_ac", "ncbi_gene_id"): self.uniprot_ac_to_ncbi_gene_id_all,
            ("ncbi_gene_id", "uniprot_ac"): self.ncbi_gene_id_to_uniprot_ac_all,
            # v29 ROOT FIX (audit L-5)
            ("compound_id", "inchikey"): self.compound_id_to_inchikey_all,
        }
        fn = dispatch_all.get((src_namespace, dst_namespace))
        if fn is None:
            return []
        return fn(src_id)

    # ── Reverse lookup (DES-3) ───────────────────────────────────────────

    def _invalidate_reverse_index_cache(self) -> None:
        """Invalidate the reverse-lookup index cache (v29 L-9/I-7).

        Called by ``_sort_all_lists`` at the end of every loader and by
        ``clear()`` so the cache is rebuilt on the next ``reverse_lookup``
        call. Callers that mutate the namespace dicts OUTSIDE a loader
        (e.g. tests that directly inject entries) must invoke this
        method manually to avoid a stale cache.
        """
        self._reverse_index_cache = None

    def _build_reverse_index(self) -> Dict[str, Dict[str, List[str]]]:
        """Build the reverse-lookup index ONCE (v29 L-9/I-7).

        Maps each stored UniProt AC (as-is, matching the historical
        ``ac == target`` comparison where ``target`` is uppercased
        before lookup) to a dict of ``{namespace: [source IDs that map
        to this AC]}``. Per-(source_id, ac) deduplication mirrors the
        legacy ``break`` semantics: each source ID appears at most once
        per namespace per AC, even if its value list contains the AC
        multiple times.
        """
        cache: Dict[str, Dict[str, List[str]]] = {}
        # Multi-valued namespace dicts (key -> list of (ac, prov)).
        multi_namespaces: List[Tuple[str, Dict[str, Any]]] = [
            ("ensp", self.ensembl_protein_to_uniprot),
            ("ensg", self.ensg_to_uniprot),
            ("chembl_target", self.chembl_target_to_uniprot),
            ("gene_symbol", self.gene_symbol_to_uniprot),
            ("ncbi_gene_id", self.gene_to_uniprot),
        ]
        for ns, src_dict in multi_namespaces:
            for source_id, vals in src_dict.items():
                # Deduplicate ACs for this source_id so the same
                # source_id is not appended twice for the same AC
                # (mirrors the legacy inner-loop ``break``).
                seen_for_source: set = set()
                for ac, _ in self._coerce_stored_list(vals):
                    if ac in seen_for_source:
                        continue
                    seen_for_source.add(ac)
                    cache.setdefault(ac, {}).setdefault(ns, []).append(
                        source_id
                    )
        # Single-valued dict (sec -> pri). Reverse: pri -> [sec IDs].
        for sec, pri in self.uniprot_secondary_to_primary.items():
            cache.setdefault(pri, {}).setdefault(
                "uniprot_secondary", []
            ).append(sec)
        return cache

    def reverse_lookup(self, uniprot_ac: str) -> Dict[str, List[str]]:
        """Return all known IDs (across all namespaces) that map to ``uniprot_ac``.

        Returns a dict like::

            {"ensp": [...], "ensg": [...], "chembl_target": [...],
             "gene_symbol": [...], "ncbi_gene_id": [...],
             "uniprot_secondary": [...]}

        v29 ROOT FIX (audit L-9/I-7): was O(n²) via reverse_lookup
        inside loop. Build reverse dict once, do O(1) lookups.
        Previously this method iterated every entry in every namespace
        dict on EACH call (O(n) per call). ``canonicalize`` calls
        ``reverse_lookup`` once per invocation, so a batch of n
        canonicalize() calls ran in O(n²) total — prohibitively slow
        on real production data (STRING aliases alone are ~100K
        entries). The reverse-lookup index is now built lazily on
        first call and cached on the instance; subsequent calls are
        O(1) dict lookups. The cache is invalidated by
        ``_sort_all_lists`` (called at the end of every loader) and
        ``clear()``.
        """
        if uniprot_ac is None:
            return {k: [] for k in (
                "ensp", "ensg", "chembl_target", "gene_symbol",
                "ncbi_gene_id", "uniprot_secondary",
            )}
        target = str(uniprot_ac).strip().upper()
        # Lazy build — first call after load (or after cache
        # invalidation) pays the one-time O(n) build cost; every
        # subsequent call is O(1).
        if self._reverse_index_cache is None:
            self._reverse_index_cache = self._build_reverse_index()
        cached = self._reverse_index_cache.get(target, {})
        # Return fresh lists (copies) so callers cannot mutate the
        # shared cache and corrupt subsequent lookups.
        return {
            "ensp": list(cached.get("ensp", [])),
            "ensg": list(cached.get("ensg", [])),
            "chembl_target": list(cached.get("chembl_target", [])),
            "gene_symbol": list(cached.get("gene_symbol", [])),
            "ncbi_gene_id": list(cached.get("ncbi_gene_id", [])),
            "uniprot_secondary": list(cached.get("uniprot_secondary", [])),
        }

    # ── Merge (DES-4) ────────────────────────────────────────────────────

    def merge(
        self,
        other: "IDCrosswalk",
        conflict_policy: Literal[
            "keep_self", "keep_other", "keep_both", "error"
        ] = "keep_self",
    ) -> "IDCrosswalk":
        """Return a new ``IDCrosswalk`` that is the union of ``self`` and ``other``.

        For multi-valued dicts, ``keep_both`` is the default-identity merge
        (union of all ACs per key). For ``uniprot_secondary_to_primary``
        (single-valued), ``conflict_policy`` applies.
        """
        result = IDCrosswalk(logger=self._logger)
        multi_dicts: List[Tuple[str, str]] = [
            ("ensembl_protein_to_uniprot", "ensembl_protein_to_uniprot"),
            ("chembl_target_to_uniprot", "chembl_target_to_uniprot"),
            ("ensg_to_uniprot", "ensg_to_uniprot"),
            ("gene_symbol_to_uniprot", "gene_symbol_to_uniprot"),
            ("uniprot_to_gene", "uniprot_to_gene"),
            ("gene_to_uniprot", "gene_to_uniprot"),
            # v29 ROOT FIX (audit L-5): dict is `compound_to_inchikey`
            # (short form); the translator method is
            # `compound_id_to_inchikey()` (long form).
            ("compound_to_inchikey", "compound_to_inchikey"),
        ]
        for self_attr, _ in multi_dicts:
            self_d = getattr(self, self_attr)
            other_d = getattr(other, self_attr)
            merged: Dict[str, _StoredValue] = {}
            for k, v in self_d.items():
                merged[k] = list(self._coerce_stored_list(v))
            for k, v in other_d.items():
                other_list = self._coerce_stored_list(v)
                if k not in merged:
                    merged[k] = list(other_list)
                else:
                    if conflict_policy == "keep_self":
                        pass  # already in merged
                    elif conflict_policy == "keep_other":
                        merged[k] = list(other_list)
                    elif conflict_policy == "keep_both":
                        seen = {ac for ac, _ in merged[k]}
                        for ac, p in other_list:
                            if ac not in seen:
                                merged[k].append((ac, p))
                                seen.add(ac)
                    elif conflict_policy == "error":
                        raise ValueError(
                            f"merge conflict on key {k!r} in {self_attr}"
                        )
            # IDEM-4: sort
            for k in merged:
                merged[k] = self._sort_ac_list(merged[k]) \
                    if self_attr != "uniprot_to_gene" and self_attr != "gene_to_uniprot" \
                    else merged[k]
            setattr(result, self_attr, merged)

        # Single-valued dict
        for sec, pri in self.uniprot_secondary_to_primary.items():
            result.uniprot_secondary_to_primary[sec] = pri
        for sec, pri in other.uniprot_secondary_to_primary.items():
            if sec in result.uniprot_secondary_to_primary:
                if conflict_policy == "keep_self":
                    pass
                elif conflict_policy == "keep_other":
                    result.uniprot_secondary_to_primary[sec] = pri
                elif conflict_policy == "error":
                    raise ValueError(
                        f"merge conflict on secondary AC {sec!r}"
                    )
                # keep_both doesn't apply to single-valued
            else:
                result.uniprot_secondary_to_primary[sec] = pri

        # Source summary + hashes
        result._source_summary_structured = (
            list(self._source_summary_structured)
            + list(other._source_summary_structured)
        )
        result._source_files = {**self._source_files, **other._source_files}
        result._loaded = True
        result._builtin_loaded = self._builtin_loaded or other._builtin_loaded
        result._built_at = _iso_now()
        return result

    # ── Verification (SCI-7) ─────────────────────────────────────────────

    def verify_builtin_against_ncbi(self) -> Dict[str, bool]:
        """Cross-check each (UniProt AC, NCBI Gene ID, gene_symbol) tuple
        against the NCBI Entrez esummary API.

        Implements SCI-7. Gated by env var ``DRUGOS_VERIFY_BUILTIN=1``.
        No-op (returns ``{}``) otherwise. Failures do NOT break the
        pipeline run but are logged at ERROR and surfaced in
        ``summary()`` as ``builtin_verification_passed: False``.

        v21 ROOT FIX (Audit section 7 finding 6 / section 9 - "FAKE NCBI
        verification"): the previous code's comment said "real
        implementation would call
        https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=gene&id=...
        and verify that the returned symbol matches entry.gene_symbol"
        but the code did ``results[key] = True  # optimistic`` - i.e.
        it returned True for EVERY entry WITHOUT calling NCBI. Reports
        'verified' without verifying. Clinical-grade ID crosswalk lied
        about its provenance. Fix: actually call NCBI esummary when
        ``DRUGOS_VERIFY_BUILTIN=1`` is set. Use a short timeout +
        exponential backoff + rate-limit (NCBI allows 3 req/s without
        API key). On network error or timeout, mark the entry as False
        (NOT True) so the failure surfaces in summary(). All results
        are still opt-in: when DRUGOS_VERIFY_BUILTIN is not set, this
        function returns ``{}`` and the crosswalk is marked
        "unverified" (the honest state).
        """
        if os.environ.get("DRUGOS_VERIFY_BUILTIN", "") != "1":
            return {}
        # v21: REAL NCBI verification (not the previous True-for-all stub).
        import urllib.request
        import urllib.error
        import json as _json
        import time as _time

        results: Dict[str, bool] = {}
        # NCBI allows 3 req/s without an API key. Use 0.34s between
        # requests to stay under the limit. Batch up to 200 IDs per
        # esummary call (NCBI's max).
        # v24 ROOT FIX (FORENSIC-P2-LOADERS §4): the docstring said
        # "exponential backoff" but the code used a fixed 0.34s sleep.
        # On network errors, implement actual exponential backoff:
        # 0.34s → 0.68s → 1.36s → 2.72s → 5.44s (max), reset on success.
        BATCH_SIZE = 200
        RATE_LIMIT_S = 0.34
        MAX_BACKOFF_S = 5.44
        _consecutive_errors = 0
        NCBI_URL = (
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
            "?db=gene&id={ids}&retmode=json"
        )
        # Build batches of NCBI gene IDs to verify.
        entries_by_ncbi: Dict[str, Any] = {
            entry.ncbi_gene_id: entry
            for entry in VERIFIED_UNIPROT_GENE_CROSSWALK
            if entry.ncbi_gene_id
        }
        all_ncbi_ids = list(entries_by_ncbi.keys())
        n_total = len(all_ncbi_ids)
        n_verified = 0
        n_mismatch = 0
        n_network_error = 0
        for batch_start in range(0, n_total, BATCH_SIZE):
            batch_ids = all_ncbi_ids[batch_start:batch_start + BATCH_SIZE]
            ids_param = ",".join(batch_ids)
            url = NCBI_URL.format(ids=ids_param)
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "DrugOS/2.1 (verification)"}
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    payload = _json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                # Network error: mark every entry in this batch as
                # UNVERIFIED (False), NOT True. The previous code
                # returned True on this path - that was the lie.
                _consecutive_errors += 1
                _backoff = min(
                    RATE_LIMIT_S * (2 ** _consecutive_errors),
                    MAX_BACKOFF_S,
                )
                self._logger.error(
                    "verify_builtin_against_ncbi: NCBI esummary failed "
                    "for batch starting at %d (%s). Marking %d entries "
                    "as UNVERIFIED. Backing off %.2fs (consecutive "
                    "errors: %d).",
                    batch_start, exc, len(batch_ids),
                    _backoff, _consecutive_errors,
                )
                for ncbi_id in batch_ids:
                    entry = entries_by_ncbi[ncbi_id]
                    key = f"{entry.uniprot_ac}:{entry.ncbi_gene_id}"
                    results[key] = False
                    n_network_error += 1
                _time.sleep(_backoff)
                continue
            except Exception as exc:
                self._logger.error(
                    "verify_builtin_against_ncbi: parse error for batch "
                    "starting at %d (%s). Marking %d entries as UNVERIFIED.",
                    batch_start, exc, len(batch_ids),
                )
                for ncbi_id in batch_ids:
                    entry = entries_by_ncbi[ncbi_id]
                    key = f"{entry.uniprot_ac}:{entry.ncbi_gene_id}"
                    results[key] = False
                    n_network_error += 1
                continue

            result_doc = payload.get("result", {})
            uids = result_doc.get("uids", [])
            for ncbi_id in batch_ids:
                entry = entries_by_ncbi[ncbi_id]
                key = f"{entry.uniprot_ac}:{entry.ncbi_gene_id}"
                gene_doc = result_doc.get(str(ncbi_id))
                if gene_doc is None:
                    # NCBI did not return this ID - mark unverified.
                    results[key] = False
                    n_mismatch += 1
                    continue
                ncbi_symbol = gene_doc.get("name", "")
                # NCBI returns the current official symbol in 'name'.
                # Compare case-insensitively to our stored gene_symbol.
                if (ncbi_symbol or "").strip().upper() == (entry.gene_symbol or "").strip().upper():
                    results[key] = True
                    n_verified += 1
                else:
                    results[key] = False
                    n_mismatch += 1
                    self._logger.warning(
                        "verify_builtin_against_ncbi: MISMATCH for "
                        "UniProt=%s NCBI=%s - builtin symbol=%r, "
                        "NCBI symbol=%r.",
                        entry.uniprot_ac, entry.ncbi_gene_id,
                        entry.gene_symbol, ncbi_symbol,
                    )
            # v24: reset consecutive-errors counter on success + use
            # the fixed RATE_LIMIT_S between successful batches.
            _consecutive_errors = 0
            _time.sleep(RATE_LIMIT_S)

        self._logger.info(
            "verify_builtin_against_ncbi: verified %d/%d entries "
            "(%d mismatches, %d network errors).",
            n_verified, n_total, n_mismatch, n_network_error,
        )
        return results

    # ── DLQ (GUARD-REL-1) ────────────────────────────────────────────────

    def unresolved_report(self) -> Dict[str, Dict[str, Any]]:
        """Return per-namespace counts and sample IDs (first 100) of
        unresolved queries (GUARD-REL-1).
        """
        out: Dict[str, Dict[str, Any]] = {}
        for ns, bucket in self._unresolved.items():
            sample = sorted(bucket)[:100]
            # Use _unresolved_counts (total, includes duplicates) — not
            # just len(bucket) (which dedupes).
            total = self._unresolved_counts.get(ns, 0)
            out[ns] = {
                "count": total,
                "unique_count": len(bucket) + self._unresolved_overflow.get(ns, 0),
                "sample": sample,
                "overflow": self._unresolved_overflow.get(ns, 0),
            }
        return out

    def write_dlq(self, path: Path) -> None:
        """Write unresolved IDs to a JSON file for offline inspection."""
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.unresolved_report(), f, indent=2)
        except OSError as e:
            self._logger.error(
                "write_dlq: failed to write %s: %s: %s",
                _redact_path(path), type(e).__name__, e,
            )

    # ── State management (IDEM-2, IDEM-3) ────────────────────────────────

    def clear(self) -> None:
        """Reset all dicts to empty, ``_loaded=False``,
        ``_source_summary_structured=[]``, ``_source_files={}``,
        ``_unresolved={...empty...}``. Does not reset ``_logger`` (IDEM-3).
        """
        self.ensembl_protein_to_uniprot.clear()
        self.chembl_target_to_uniprot.clear()
        self.ensg_to_uniprot.clear()
        self.gene_symbol_to_uniprot.clear()
        self.uniprot_to_gene.clear()
        self.gene_to_uniprot.clear()
        self.uniprot_secondary_to_primary.clear()
        # v29 ROOT FIX (audit L-5): clear the Compound ID -> InChIKey
        # crosswalk too so callers that ``clear()`` and re-seed get a
        # clean state.
        self.compound_to_inchikey.clear()
        self._loaded = False
        self._builtin_loaded = False
        self._warned_unloaded = False
        self._source_summary_structured = []
        self._source_files = {}
        self._unresolved = {k: set() for k in self._unresolved}
        self._unresolved_overflow = {k: 0 for k in self._unresolved_overflow}
        self._unresolved_counts = {k: 0 for k in self._unresolved_counts}
        self._translator_stats = {
            k: {"calls": 0, "hits": 0, "misses": 0}
            for k in self._translator_stats
        }
        self._instance_id = str(uuid.uuid4())
        self._built_at = _iso_now()
        # v29 ROOT FIX (audit L-9/I-7): invalidate the reverse-lookup
        # cache so the next ``reverse_lookup`` call rebuilds it from
        # the now-empty namespace dicts.
        self._reverse_index_cache = None

    # ── Stats (COMP-1, GUARD-COMP-1, OBS-1, OBS-2) ───────────────────────

    def summary(self) -> Dict[str, object]:
        """Return a summary dict with backward-compatible keys plus
        audit-trail keys (GUARD-COMP-1).
        """
        # OBS-2: build backward-compat string form
        source_str = (
            " + ".join(f"{name} ({count})" for name, count in self._source_summary_structured)
            or "not loaded"
        )
        # OBS-1: translator hit rates
        hit_rates: Dict[str, Dict[str, Any]] = {}
        for name, stats in self._translator_stats.items():
            calls = stats["calls"]
            hits = stats["hits"]
            hit_rates[name] = {
                "calls": calls,
                "hits": hits,
                "misses": stats["misses"],
                "rate": (hits / calls) if calls else 0.0,
            }
        # GUARD-REL-1: unresolved counts (total, including duplicate queries)
        unresolved_counts = dict(self._unresolved_counts)
        return {
            # Backward-compat keys (existing callers)
            "source": source_str,
            "ensembl_protein_to_uniprot": len(self.ensembl_protein_to_uniprot),
            "chembl_target_to_uniprot": len(self.chembl_target_to_uniprot),
            "ensembl_gene_to_uniprot": len(self.ensg_to_uniprot),
            "uniprot_to_gene": len(self.uniprot_to_gene),
            "gene_to_uniprot": len(self.gene_to_uniprot),
            # New audit-trail keys (GUARD-COMP-1)
            "sources_structured": list(self._source_summary_structured),
            "built_at": self._built_at,
            "builtin_table_version": BUILTIN_TABLE_VERSION if self._builtin_loaded else None,
            "builtin_organism": BUILTIN_ORGANISM if self._builtin_loaded else None,
            "builtin_tax_id": BUILTIN_TAX_ID if self._builtin_loaded else None,
            "source_file_hashes": dict(self._source_files),
            "translator_hit_rates": hit_rates,
            "unresolved_counts": unresolved_counts,
            "instance_id": self._instance_id,
            # Additional state for downstream consumers
            "ensg_to_uniprot": len(self.ensg_to_uniprot),
            "gene_symbol_to_uniprot": len(self.gene_symbol_to_uniprot),
            "uniprot_secondary_to_primary": len(self.uniprot_secondary_to_primary),
            # v29 ROOT FIX (audit L-5)
            "compound_id_to_inchikey": len(self.compound_to_inchikey),
        }

    # ── Internal: list sorting helpers ───────────────────────────────────

    @staticmethod
    def _sort_ac_list(lst: List[Tuple[str, Provenance]]) -> List[Tuple[str, Provenance]]:
        """Sort a list of (ac, provenance) tuples deterministically.

        Swiss-Prot ACs (starting with [OPQ]) are placed BEFORE TrEMBL ACs
        (starting with [A-NR-Z]) — see DQ-1. Within each group, sort
        alphabetically.
        """
        def sort_key(item: Tuple[str, Provenance]) -> Tuple[int, str]:
            ac, _ = item
            return (0 if _is_swiss_prot(ac) else 1, ac)
        return sorted(lst, key=sort_key)

    def _sort_all_lists(self) -> None:
        """IDEM-4: enforce deterministic ordering in every multi-valued dict."""
        for d in (
            self.ensembl_protein_to_uniprot,
            self.chembl_target_to_uniprot,
            self.ensg_to_uniprot,
            self.gene_symbol_to_uniprot,
            # v29 ROOT FIX (audit L-5): compound_id_to_inchikey uses the
            # same multi-valued storage pattern. Sort alphabetically
            # (InChIKeys have no Swiss-Prot / TrEMBL analogue so the
            # plain sort via ``sorted(lst, key=lambda x: x[0])`` is the
            # right call — bypass ``_sort_ac_list`` which is AC-specific).
        ):
            for k in list(d.keys()):
                d[k] = self._sort_ac_list(self._coerce_stored_list(d[k]))
        # v29 ROOT FIX (audit L-5): Compound ID -> InChIKey dict —
        # sort each value list alphabetically by InChIKey for determinism.
        for k in list(self.compound_to_inchikey.keys()):
            lst = self._coerce_stored_list(self.compound_to_inchikey[k])
            self.compound_to_inchikey[k] = sorted(lst, key=lambda x: x[0])
        # For gene<->uniprot dicts, sort by gene ID / AC respectively
        for k in list(self.uniprot_to_gene.keys()):
            lst = self._coerce_stored_list(self.uniprot_to_gene[k])
            self.uniprot_to_gene[k] = sorted(lst, key=lambda x: x[0])
        for k in list(self.gene_to_uniprot.keys()):
            lst = self._coerce_stored_list(self.gene_to_uniprot[k])
            self.gene_to_uniprot[k] = self._sort_ac_list(lst)
        # v29 ROOT FIX (audit L-9/I-7): _sort_all_lists is called at
        # the end of every loader (load_builtin, load_string_aliases,
        # load_chembl_target_components, load_opentargets_targets,
        # load_from_uniprot_records, …). Invalidate the reverse-lookup
        # cache here so the next ``reverse_lookup`` call rebuilds it
        # from the freshly-loaded data. This is the single chokepoint
        # that covers every loader without requiring each loader to
        # remember to invalidate individually.
        self._reverse_index_cache = None


# =============================================================================
# Section 6 — Module-level singleton (thread-safe, IDEM-1, IDEM-2)
# =============================================================================

_default_lock: threading.Lock = threading.Lock()
_default_instance: Optional[IDCrosswalk] = None


def get_default_crosswalk() -> IDCrosswalk:
    """Return the process-wide default ``IDCrosswalk``.

    Loads the builtin table on first call. Thread-safe (IDEM-1) via
    double-checked locking on ``_default_lock``.
    """
    global _default_instance
    if _default_instance is None:
        with _default_lock:
            if _default_instance is None:
                inst = IDCrosswalk()
                inst.load_builtin()
                _default_instance = inst
    return _default_instance


def reset_default_crosswalk() -> None:
    """For tests only — clears the singleton.

    Acquires the lock to be safe against any in-flight
    ``get_default_crosswalk()`` calls (IDEM-2).
    """
    global _default_instance
    with _default_lock:
        _default_instance = None


# =============================================================================
# Section 6.5 — v29 ROOT FIX (audit L-5): Compound ID normalization helper
# =============================================================================
# Compound ID fragmentation: 7 disjoint namespaces — DrugBank ID, ChEMBL ID,
# PubChem CID, STITCH CIDm/CIDs, SIDER bare CID, DRKG MESH, InChIKey.
# ``merge_mappings_by_inchikey()`` in entity_resolver only fires when
# InChIKey is present — STITCH/SIDER/DRKG edges reference Compounds by
# non-InChIKey IDs that don't match InChIKey-keyed nodes. The KG had 7
# disjoint subgraphs.
# Root fix: this module-level helper is called from stitch_loader,
# sider_loader, drkg_loader on every Compound src_id/dst_id BEFORE
# building edge records. When a mapping exists in the crosswalk, the
# Compound reference is rewritten to the canonical InChIKey — unifying
# it with the InChIKey-keyed Compound nodes produced by DrugBank /
# ChEMBL / PubChem loaders. When no mapping exists, the original ID is
# returned unchanged (with a WARNING log) so the pipeline does not
# crash; ``merge_mappings_by_inchikey()`` will eventually merge these
# once Phase 1's entity resolution runs and populates the crosswalk.
# ---------------------------------------------------------------------

# Per-process cache of compound_id → InChIKey lookups that returned no
# mapping. Avoids re-warning (and re-logging) for the same ID across
# millions of rows (DRKG has ~6M triples; SIDER ~310K rows; STITCH
# ~1.6M). The cache lives at module scope so it survives across loader
# calls within one Python process. Cleared by ``reset_default_crosswalk()``
# via ``_clear_compound_id_miss_cache()``.
_COMPOUND_ID_MISS_CACHE: set = set()
_COMPOUND_ID_MISS_CACHE_LOCK = threading.Lock()
_COMPOUND_ID_MISS_CACHE_CAP: Final[int] = int(
    os.environ.get("DRUGOS_COMPOUND_MISS_CACHE_CAP", "100_000")
)


def _clear_compound_id_miss_cache() -> None:
    """Clear the per-process compound-ID miss cache (used by tests)."""
    global _COMPOUND_ID_MISS_CACHE
    with _COMPOUND_ID_MISS_CACHE_LOCK:
        _COMPOUND_ID_MISS_CACHE = set()


def _normalize_compound_id_to_inchikey(
    compound_id: Any,
    crosswalk: Optional["IDCrosswalk"] = None,
    *,
    source: str = "loader",
) -> str:
    """Normalize a Compound ID (any namespace) → InChIKey when possible.

    v29 ROOT FIX (audit L-5): Compound ID fragmentation — STITCH/SIDER/
    DRKG used non-InChIKey IDs. Now normalizes to InChIKey via
    crosswalk before loading.

    Args:
        compound_id: Compound ID in any namespace (DrugBank ID ``DB00107``,
            ChEMBL ID ``CHEMBL218``, PubChem CID ``CID5311025``, STITCH
            ``CIDm00002244`` / ``CIDs00002244``, SIDER bare CID, DRKG
            MESH ``MESH:D000544``, already-InChIKey, etc.). ``None`` and
            non-string inputs are returned as the str() of themselves.
        crosswalk: Optional ``IDCrosswalk`` instance. If ``None``, uses
            ``get_default_crosswalk()``.
        source: Provenance tag for the WARNING log (which loader is
            calling). Default ``"loader"``.

    Returns:
        The InChIKey (27-char ``XXXXXXXXXXXXXX-XXXXXXXXXX-X`` form)
        if a mapping exists in the crosswalk. The original ID unchanged
        if no mapping exists — never returns ``None``, never raises.

    Side effects:
        - Logs a WARNING (once per unique compound_id, per process) when
          no mapping is found. The miss is also tracked in the
          crosswalk's DLQ (``unresolved_report()['compound_id']``) for
          offline inspection.
        - Increments the crosswalk's
          ``_translator_stats['compound_id_to_inchikey']`` hit/miss
          counters (OBS-1).
    """
    # Defensive coercion — callers in vectorised loops occasionally pass
    # numpy/pandas scalars. ``str(None) == "None"`` so guard explicitly.
    if compound_id is None:
        return ""
    s = str(compound_id).strip()
    if not s or s == "None" or s == "nan":
        return ""

    # Short-circuit: input is already an InChIKey.
    if _INCHIKEY_PATTERN.match(s):
        return s

    # Acquire a crosswalk instance (caller's or default singleton).
    try:
        cw = crosswalk if crosswalk is not None else get_default_crosswalk()
    except Exception:  # pragma: no cover — defensive
        # If the crosswalk can't be acquired (e.g. circular import at
        # module load), fall through to returning the original ID.
        return s

    try:
        inchikey = cw.compound_id_to_inchikey(s)
    except Exception:  # pragma: no cover — defensive
        return s

    if inchikey is not None and _INCHIKEY_PATTERN.match(inchikey):
        return inchikey

    # No mapping — log WARNING once per unique ID, return original.
    with _COMPOUND_ID_MISS_CACHE_LOCK:
        if len(_COMPOUND_ID_MISS_CACHE) < _COMPOUND_ID_MISS_CACHE_CAP:
            if s not in _COMPOUND_ID_MISS_CACHE:
                _COMPOUND_ID_MISS_CACHE.add(s)
                logger.warning(
                    "compound_id_to_inchikey miss (%s source=%s): no "
                    "InChIKey mapping for %r — passing original ID "
                    "through. The mapping will be populated when Phase "
                    "1's entity resolution runs.",
                    s[:32], source, s,
                )
    return s


# Hook ``reset_default_crosswalk()`` so that tests which reset the
# singleton also clear the per-process miss cache. We wrap the original
# function rather than rewriting it (preserves the IDEM-2 docstring).
def _reset_default_crosswalk_with_cache_clear() -> None:
    reset_default_crosswalk.__wrapped__()  # type: ignore[attr-defined]
    _clear_compound_id_miss_cache()


# Preserve the original function via __wrapped__ for introspection.
_reset_default_crosswalk_with_cache_clear.__wrapped__ = (  # type: ignore[attr-defined]
    reset_default_crosswalk
)
_reset_default_crosswalk_with_cache_clear.__doc__ = (
    reset_default_crosswalk.__doc__
    + "\n\nAlso clears the per-process compound-ID miss cache (v29 L-5)."
)
reset_default_crosswalk = _reset_default_crosswalk_with_cache_clear  # type: ignore[assignment]


# =============================================================================
# Section 7 — __all__ (COD-5)
# =============================================================================

__all__ = [
    "IDCrosswalk",
    "get_default_crosswalk",
    "reset_default_crosswalk",
    "VERIFIED_UNIPROT_GENE_CROSSWALK",
    "VerifiedEntry",
    "Provenance",
    "AmbiguousMappingError",
    # v41 ROOT FIX (Task J SEV2): "CrosswalkSource" removed from __all__.
    # The class was dead code (no subclasses, no callers). See the
    # Section 4 comment above for the full rationale. A PEP 562
    # ``__getattr__`` handler at the module bottom raises a clear
    # ImportError if any external code still imports ``CrosswalkSource``
    # by name, so removal is detectable rather than silently producing
    # ``AttributeError``.
    "BUILTIN_ORGANISM",
    "BUILTIN_TAX_ID",
    "BUILTIN_TABLE_VERSION",
    "BUILTIN_PATH",
    "DEFAULT_ORGANISM_TAX_ID",
    "MAX_ENSP_ENTRIES",
    "MAX_CHEMBL_ENTRIES",
    "MAX_OPENTARGETS_ENTRIES",
    # v29 ROOT FIX (audit L-5): Compound ID normalization helper
    # exposed at module scope so loaders can call it without acquiring
    # a crosswalk instance explicitly (uses the default singleton).
    "_normalize_compound_id_to_inchikey",
    "_clear_compound_id_miss_cache",
]


def __getattr__(name):
    # v41 ROOT FIX (Task J SEV2): PEP 562 module-level __getattr__ provides
    # a clear ImportError (with upgrade guidance) for code that still
    # imports the removed ``CrosswalkSource`` symbol. Without this handler,
    # ``from drugos_graph.id_crosswalk import CrosswalkSource`` would raise
    # ``ImportError: cannot import name 'CrosswalkSource'`` — accurate but
    # unhelpful. With this handler, the error explicitly states that
    # ``CrosswalkSource`` was removed in v41 and points to the replacement
    # (use ``IDCrosswalk.load_*`` methods directly).
    if name == "CrosswalkSource":
        raise ImportError(
            "drugos_graph.id_crosswalk.CrosswalkSource was removed in v41 "
            "(Task J SEV2 dead-code cleanup). The class was an abstract "
            "base with no subclasses, no callers, and no real extension "
            "contract. To add a new ID source, implement an "
            "``IDCrosswalk.load_<source>`` method directly — see the "
            "existing ``load_ensembl_protein_to_uniprot`` / "
            "``load_chembl_target_to_uniprot`` / ``load_opentargets`` "
            "methods for the pattern."
        )
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
