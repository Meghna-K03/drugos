"""Shared constants for the cleaning module (v16 ROOT FIX for CD-7).

Why this file exists
--------------------
Before v16, two modules defined ``_ACTIVITY_VALUE_MAX`` with DIFFERENT
values:

- ``cleaning/normalizer.py`` defined ``_ACTIVITY_VALUE_MAX = 1e6``
  (1 mM) — beyond this, a value was marked ``censored=True`` ("we know
  it's > X") because 1 mM is the upper limit of pharmacological
  relevance.
- ``cleaning/deduplicator.py`` defined ``_ACTIVITY_VALUE_MAX = 1e9``
  (1 M) — beyond this, a value was filtered out as "non-physical"
  (concentrations above 1 M are chemically impossible in aqueous
  biological assays).

A value of e.g. ``5e7 nM`` (50 mM) was therefore ``censored=True``
in normalizer but ``valid`` in deduplicator — a 3-order-of-magnitude
inconsistency that meant the same activity record had different
"valid" status depending on which module inspected it. Downstream
TransE training saw a biased sample.

v16 fix: this module exposes TWO clearly-named constants so the
distinction is explicit and both modules import from here:

- ``ACTIVITY_VALUE_CENSORED_THRESHOLD = 1e6``  (1 mM — above this,
  a value is flagged as "censored: > X" because it exceeds the
  pharmacologically relevant range)
- ``ACTIVITY_VALUE_NON_PHYSICAL_THRESHOLD = 1e9``  (1 M — above this,
  a value is rejected as non-physical / corrupt)

Both modules now reference these shared constants; no module defines
its own ``_ACTIVITY_VALUE_MAX``.

v29 ROOT FIX (Compound Chain 3 — InChIKey Validation Divergence):
The forensic audit found that normalizer.py accepted 28+ char
suffixed InChIKeys (`(?:-[A-Za-z0-9]+)?` block), while
deduplicator.py required strict 27-char (`^[A-Z]{14}-[A-Z]{10}-[A-Z]$`).
A valid InChIKey could pass cleaning, fail dedup, fail DB insert.
An invalid InChIKey could pass cleaning, pass dedup, fail DB insert.
The DB CHECK was even more permissive (LENGTH=27 OR SYNTH%). This
3-way divergence caused silent data loss at every stage.

ROOT FIX: define ONE canonical regex here. All modules import from
this single source of truth. The regex is the IUPAC standard
InChIKey format: 14 uppercase letters, hyphen, 10 uppercase letters,
hyphen, 1 uppercase letter (the version char — usually S or N, but
the spec allows any uppercase letter for forward compatibility).
Protonation / tautomeric suffixes (`-a`, `-b`) are NOT part of the
canonical InChIKey — they're an IUPAC extension and should be
stripped BEFORE validation, not accepted by the regex. This is the
audited, scientifically-correct behavior.
"""

from __future__ import annotations

import math
import re
from typing import Any, Optional

# 1 mM (1e6 nM) — upper bound of pharmacologically relevant range.
# Values above this are flagged as "censored: > X" in normalizer.
ACTIVITY_VALUE_CENSORED_THRESHOLD: float = 1e6

# 1 M (1e9 nM) — non-physical upper bound for aqueous biological assays.
# Values above this are rejected as corrupt in deduplicator.
ACTIVITY_VALUE_NON_PHYSICAL_THRESHOLD: float = 1e9

# v41 ROOT FIX (SEV2): the previous comment block (lines 69-126 in v40)
# claimed that ``cleaning/deduplicator.py`` still imported the WRONG
# ``_ACTIVITY_VALUE_MAX`` value (1e6 censored) and silently dropped 5 mM
# values as corrupt.  That was true at v35 audit time but is OUT-OF-DATE
# — the deduplicator was subsequently fixed to import
# ``ACTIVITY_VALUE_NON_PHYSICAL_THRESHOLD`` (1e9) directly and assign it
# to its own module-local ``_ACTIVITY_VALUE_MAX`` (see
# ``cleaning/deduplicator.py:435-440``).  Similarly, ``normalizer.py``
# imports ``ACTIVITY_VALUE_CENSORED_THRESHOLD`` (1e6) and assigns it to
# its own module-local ``_ACTIVITY_VALUE_MAX`` (see
# ``cleaning/normalizer.py:585-586``).  No module imports the
# backward-compat alias ``_ACTIVITY_VALUE_MAX`` or
# ``_ACTIVITY_VALUE_MAX_LEGACY`` from this file (verified via grep on the
# whole codebase) — they were dead.  The dead aliases have been removed
# and the misleading "deduplicator uses wrong threshold" comment has
# been corrected.  The two clearly-named canonical constants below
# remain the SINGLE source of truth that both modules import.


# ============================================================================
# v29 ROOT FIX: canonical InChIKey regex (single source of truth)
# ============================================================================
#
# IUPAC InChIKey specification (https://www.inchi-trust.org/technical-faq/):
#   - 14 uppercase letters (hash of molecular skeleton)
#   - hyphen
#   - 10 uppercase letters (hash of proton layer)
#   - hyphen
#   - 1 uppercase letter (version: 'S' = standard, 'N' = non-standard)
#   - Total: 27 characters, no extensions.
#
# Protonation / tautomer extensions (e.g. "-a", "-N-a") are NOT part of
# the canonical InChIKey. Callers that encounter these extensions should
# STRIP them before validation (see ``strip_inchikey_extension`` below).
#
# This regex is the SINGLE source of truth. normalizer.py, deduplicator.py,
# entity_resolution, and the DB CHECK constraint (migration 009) MUST all
# import and use this regex. Divergence = silent data loss (audit C-5).
CANONICAL_INCHIKEY_REGEX: re.Pattern[str] = re.compile(
    r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$"
)

# Strict standard variant — version char must be 'S' (per IUPAC).
CANONICAL_STANDARD_INCHIKEY_REGEX: re.Pattern[str] = re.compile(
    r"^[A-Z]{14}-[A-Z]{10}-S$"
)

# Non-standard variant — version char must be 'N'.
CANONICAL_NONSTANDARD_INCHIKEY_REGEX: re.Pattern[str] = re.compile(
    r"^[A-Z]{14}-[A-Z]{10}-N$"
)

# Synthetic InChIKey prefix (for biologics / unknown structures).
# Loose match: anything starting with "SYNTH" (case-insensitive).
# Matches the DB CHECK constraint `inchikey LIKE 'SYNTH%'`.
CANONICAL_SYNTHETIC_INCHIKEY_REGEX: re.Pattern[str] = re.compile(
    r"^SYNTH.+$", re.IGNORECASE
)

# Strict SYNTH pattern — SYNTH + 9 hex + hyphen + 10 hex + hyphen + 1 hex.
CANONICAL_SYNTHETIC_INCHIKEY_STRICT_REGEX: re.Pattern[str] = re.compile(
    r"^SYNTH[0-9A-F]{9}-[0-9A-F]{10}-[0-9A-F]$", re.IGNORECASE
)

# Mixture InChIKey — multiple standard keys joined by hyphens.
CANONICAL_MIXTURE_INCHIKEY_REGEX: re.Pattern[str] = re.compile(
    r"^(?:[A-Z]{14}-[A-Z]{10}-[A-Z])(?:-[A-Z]{14}-[A-Z]{10}-[A-Z])*$"
)


# ============================================================================
# v35 ROOT FIX: canonical HGNC gene symbol regex (single source of truth)
# ============================================================================
#
# HGNC gene symbols are uppercase ASCII letters + digits + hyphens, max 50
# chars (the longest approved HGNC symbol as of 2024 is well under 50, but
# 50 matches the DB column length and gives headroom for future symbols).
# This is the STRICT human form. Non-human species use Title-Case symbols
# (e.g. ``Tp53`` for mouse) — see ``CANONICAL_NON_HUMAN_GENE_SYMBOL_REGEX``.
#
# Pipelines that ingest HUMAN gene data (UniProt human, DisGeNET, OMIM,
# STRING 9606) MUST import this regex and use it at the OUTPUT boundary.
# Pipelines that ingest NON-HUMAN data (UniProt mouse, STRING non-9606)
# MUST import ``CANONICAL_NON_HUMAN_GENE_SYMBOL_REGEX`` instead.
#
# ``database.models.Protein.gene_symbol`` keeps a portable SQLite-compatible
# CHECK constraint (LENGTH 1-50) and delegates the strict format check to
# this regex at the Python layer.
CANONICAL_HGNC_GENE_SYMBOL_REGEX: re.Pattern[str] = re.compile(
    r"^[A-Z][A-Z0-9\-]{0,49}$"
)

# Non-human gene symbols — first letter upper or lower, rest
# upper/lower/digits/hyphens. Matches e.g. ``Tp53`` (mouse),
# ``TP53`` (ALL-CAPS), ``tp53`` (all-lowercase).
# v43 ROOT FIX (P1-024): the previous comment said "Title-Case OR
# ALL-CAPS" but the regex ``^[A-Za-z][A-Za-z0-9\-]{0,49}$`` also
# accepts all-lowercase (e.g. ``tp53`` would match). The comment has
# been corrected to accurately describe the regex's behavior: it
# accepts any case mix (first letter upper or lower, rest any case).
# This is intentional — non-human pipelines sometimes receive
# all-lowercase symbols from upstream sources. Max 50 chars to stay
# consistent with the human form and the DB column.
CANONICAL_NON_HUMAN_GENE_SYMBOL_REGEX: re.Pattern[str] = re.compile(
    r"^[A-Za-z][A-Za-z0-9\-]{0,49}$"
)


# ============================================================================
# v35 ROOT FIX: canonical OMIM disease ID regex (single source of truth)
# ============================================================================
#
# OMIM IDs are 6-digit numbers in current use (e.g. 104300 for Marfan
# syndrome), but historical MIM numbers can be as short as 4 digits (the
# earliest MIM numbers, e.g. 1000-series from the 1960s). 7-digit MIM
# numbers do not exist in current OMIM data but are tolerated for forward
# compatibility (in case OMIM ever expands its ID space).
#
# All pipelines that ingest OMIM IDs (DisGeNET, OMIM, drug-resolver, DB
# loaders) MUST import this regex. ``database.models.Disease.disease_id``
# keeps a portable SQLite-compatible CHECK constraint (LENGTH 4-10) and
# delegates the strict format check to this regex at the Python layer.
CANONICAL_OMIM_DISEASE_ID_REGEX: re.Pattern[str] = re.compile(
    r"^(?:OMIM:)?[0-9]{4,7}$"
)


# ============================================================================
# v35 ROOT FIX: canonical amino-acid sequence regex (single source of truth)
# ============================================================================
#
# Amino-acid sequences consist of the 20 standard residues + the IUPAC
# ambiguity codes (B=Asx, J=Xle, O=Pyl, U=Sec, X=any, Z=Glx) + the stop
# char ``*`` + the alignment gap char ``-`` (for aligned/padded sequences).
# The gap char is included for consistency between the DB CHECK, the
# pipeline validator, and the entity_resolution validator — without it, an
# aligned sequence with gaps would pass the DB CHECK but fail the cleaning
# validator (silent data loss at the cleaning → DB boundary).
CANONICAL_AA_SEQUENCE_REGEX: re.Pattern[str] = re.compile(
    r"^[ACDEFGHIKLMNPQRSTVWYBJOUXZ\*\-]+$"
)


# ============================================================================
# v29 ROOT FIX (audit D-4): canonical UniProt accession regex
# ============================================================================
#
# The DB layer (models.py ``Protein.uniprot_id``) keeps a portable,
# SQLite-compatible LENGTH-based CHECK constraint because SQLite does not
# support regex CHECKs natively. The STRICT canonical regex is enforced at
# the Python layer instead, by ``database.models._validate_uniprot_id``
# (which delegates to ``entity_resolution.resolver_utils._UNIPROT_ACCESSION_RE``
# — same pattern).
#
# Why a separate canonical regex here:
#   * Audit D-4 found that the DB CHECK only enforces ``LENGTH 4-10`` and
#     therefore lets any short alphanumeric string (e.g. a mouse-organism
#     identifier or a stray ``MOUSE1`` token) sneak into the human protein
#     set. The Python validator already rejects these via the strict regex,
#     but the regex was previously only defined in
#     ``entity_resolution.resolver_utils`` — invisible to anyone reading the
#     DB layer. Declaring it here (single source of truth, mirroring the
#     InChIKey pattern above) makes the canonical contract explicit and
#     importable from any module.
#
# Two variants are exposed:
#   * ``CANONICAL_UNIPROT_ACCESSION_REGEX`` — the audit-D-4 spec verbatim:
#     ``^[OPQ][0-9][A-Z0-9]{3}[0-9]([A-Z0-9]{3}[0-9]){1,5}$``. This is the
#     STRICT form. Note: it requires the optional 4-char block at least
#     once, so it matches 10–26-char accessions only. It is intended for
#     caller code that explicitly wants to accept ONLY the long form.
#   * ``CANONICAL_UNIPROT_ACCESSION_REGEX_FULL`` — the operational form
#     that ALSO accepts canonical 6-char accessions (e.g. P69999, Q9Y6K9).
#     This is what ``models._validate_uniprot_id`` and
#     ``resolver_utils._UNIPROT_ACCESSION_RE`` actually apply. Both must
#     stay byte-for-byte in sync — divergence = silent data loss (audit
#     D-4 / Chain 3).
#
# Reference: https://www.uniprot.org/help/accession_numbers
CANONICAL_UNIPROT_ACCESSION_REGEX: re.Pattern[str] = re.compile(
    r"^[OPQ][0-9][A-Z0-9]{3}[0-9]([A-Z0-9]{3}[0-9]){1,5}$"
)

# Operational form — accepts canonical 6-char accessions as well as the
# 10-char newer format. Mirrors
# ``entity_resolution.resolver_utils._UNIPROT_ACCESSION_RE`` EXACTLY.
CANONICAL_UNIPROT_ACCESSION_REGEX_FULL: re.Pattern[str] = re.compile(
    r"^([OPQ][0-9][A-Z0-9]{3}[0-9]"
    r"|[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2})$"
)


def strip_inchikey_extension(inchikey: str) -> str:
    """Strip any non-canonical extension from an InChIKey.

    Per IUPAC, the canonical InChIKey is exactly 27 chars. Real-world
    sources sometimes append extensions like ``-a`` (protonation),
    ``-N-a``, etc. These are NOT part of the canonical key and must
    be stripped before validation / deduplication / DB insert.

    Examples
    --------
    >>> strip_inchikey_extension("BSYNRYMUTXBXSQ-UHFFFAOYSA-N")
    'BSYNRYMUTXBXSQ-UHFFFAOYSA-N'  # already 27-char canonical — unchanged
    >>> strip_inchikey_extension("BSYNRYMUTXBXSQ-UHFFFAOYSA-N-a")
    'BSYNRYMUTXBXSQ-UHFFFAOYSA-N'  # extension stripped
    >>> strip_inchikey_extension("SYNTH-ABCDEF0123-ABCDEF0123-A")
    'SYNTH-ABCDEF0123-ABCDEF0123-A'  # SYNTH keys are not stripped
    """
    if not isinstance(inchikey, str):
        return ""
    s = inchikey.strip().upper()
    if s.startswith("SYNTH"):
        return s  # SYNTH keys have their own format; do not strip
    # If the key is longer than 27 chars and matches the canonical
    # prefix, strip everything after the 27th char.
    if len(s) > 27:
        prefix = s[:27]
        if CANONICAL_INCHIKEY_REGEX.match(prefix):
            return prefix
    return s


def is_canonical_inchikey(inchikey: str) -> bool:
    """Return True iff the input is a valid canonical 27-char InChIKey
    (or a valid SYNTH-prefixed key, or a valid mixture key).

    This is the SINGLE canonical validator. All modules should call this
    instead of defining their own regex.
    """
    if not isinstance(inchikey, str) or not inchikey:
        return False
    s = inchikey.strip()
    if CANONICAL_INCHIKEY_REGEX.match(s):
        return True
    elif CANONICAL_SYNTHETIC_INCHIKEY_REGEX.match(s):
        return True
    elif CANONICAL_MIXTURE_INCHIKEY_REGEX.match(s):
        return True
    return False


# ============================================================================
# v29 ROOT FIX (audit P1-24): canonical ID normalization utilities
# ============================================================================
#
# Each of the 7 ingestion pipelines (chembl, drugbank, uniprot, string,
# disgenet, omim, pubchem) writes a slightly different ID format for the
# SAME biological entity. Cross-source joins (e.g. ChEMBL.drug × DrugBank.drug
# on InChIKey; STRING.PPI × UniProt.protein on uniprot_id; DisGeNET × OMIM
# on gene_symbol) silently fail when one source writes
# ``"BSYNRYMUTXBXSQ-UHFFFAOYSA-N"`` and another writes
# ``"bsynrymutxbxsq-uhfffaoysa-n"``; or when one source writes ``"P23219"``
# and another writes ``"p23219"``.
#
# ROOT FIX: this module exposes one canonical normalizer per ID type. The
# 7 pipelines call them at their OUTPUT boundary (right before persist /
# return) so every CSV shipped downstream has the SAME canonical form,
# regardless of which source produced it.
#
# These functions are deliberately MINIMAL — they enforce case + whitespace
# only. Format validation (regex, length, checksum) remains the job of the
# pipeline-specific validators (which import the CANONICAL_*_REGEX constants
# above). Splitting "shape" from "case" keeps the normalizers idempotent
# and side-effect-free, which is required because some pipelines already
# run their own validators (e.g. ChEMBL's ``_step_standardize_inchikeys``).
# Calling normalize_*() on already-normalized data is a no-op.
#
# The ``normalize_pubchem_cid`` function returns ``Optional[int]`` (not str)
# because PubChem CIDs are integers — storing them as strings would re-introduce
# the leading-zero / case divergence this fix eliminates. ``None`` is returned
# for any value that cannot be coerced to a positive integer.


def normalize_inchikey(s: str) -> str:
    """Normalize an InChIKey to canonical form.

    - Uppercase (InChIKey hash chars are uppercase by IUPAC spec).
    - Strip leading/trailing whitespace.
    - Non-string inputs are coerced via ``str()``.

    Returns ``None`` for ``None`` input so callers can decide how to handle
    missing values (the canonical regex will reject ``None`` as invalid).
    This is aligned with ``normalizer.py``'s ``normalize_inchikey`` which
    also returns ``None`` for ``None`` input (v35 root fix — the previous
    ``""`` sentinel created a divergence between the two normalizers that
    caused ``None`` values to be silently written as empty strings into
    the cleaned CSV).

    .. warning::
        **INTENTIONAL DIVERGENCE (audit-2025 issue 14)**: this function
        returns ``None`` for ``None`` input, while ``normalize_uniprot_id``
        and ``normalize_drugbank_id`` in this same module return ``""``.
        The two contracts serve different downstream consumers:

        * InChIKeys are validated against a strict IUPAC regex
          (``CANONICAL_INCHIKEY_REGEX``) immediately after normalization.
          Returning ``""`` would let an empty string sneak past regex
          checks that use ``if not key: return False`` shortcuts and
          would also be written to the cleaned CSV as the empty string,
          silently corrupting ``None`` values.
        * UniProt accessions and DrugBank IDs flow through different
          validators that explicitly treat ``""`` as "missing" via
          truthiness checks, so the empty-string sentinel is safe
          there.

        Do NOT change one without changing the other — if a future
        refactor unifies the contract, ALL three normalizers AND
        ``normalizer.py::normalize_inchikey`` must be updated together
        and ALL downstream consumers (regex validators, CSV writers,
        ORM ``nullable=True`` columns) re-audited.

    Parameters
    ----------
    s : str
        Raw InChIKey (e.g. ``"bsynrymutxbxsq-uhfffaoysa-n"``).

    Returns
    -------
    str or None
        Canonical InChIKey (e.g. ``"BSYNRYMUTXBXSQ-UHFFFAOYSA-N"``), or
        ``None`` if the input was ``None``.

    Examples
    --------
    >>> normalize_inchikey("bsynrymutxbxsq-uhfffaoysa-n")
    'BSYNRYMUTXBXSQ-UHFFFAOYSA-N'
    >>> normalize_inchikey("  BSYNRYMUTXBXSQ-UHFFFAOYSA-N  ")
    'BSYNRYMUTXBXSQ-UHFFFAOYSA-N'
    >>> normalize_inchikey(None) is None
    True
    """
    if s is None:
        return None
    if not isinstance(s, str):
        s = str(s)
    return s.strip().upper()


def normalize_uniprot_id(s: str) -> str:
    """Normalize a UniProt accession to canonical form.

    - Uppercase (UniProt accessions are uppercase by spec).
    - Strip leading/trailing whitespace.
    - Non-string inputs are coerced via ``str()``.

    Returns ``""`` for ``None`` input.

    Parameters
    ----------
    s : str
        Raw UniProt accession (e.g. ``"p23219"``).

    Returns
    -------
    str
        Canonical UniProt accession (e.g. ``"P23219"``).

    Examples
    --------
    >>> normalize_uniprot_id("p23219")
    'P23219'
    >>> normalize_uniprot_id("  Q9Y6K9  ")
    'Q9Y6K9'
    >>> normalize_uniprot_id(None)
    ''
    """
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    return s.strip().upper()


def normalize_drugbank_id(s: str) -> str:
    """Normalize a DrugBank ID to canonical form.

    - Uppercase (DrugBank IDs are ``DB`` + digits by spec).
    - Strip leading/trailing whitespace.
    - Non-string inputs are coerced via ``str()``.

    Returns ``""`` for ``None`` input.

    Parameters
    ----------
    s : str
        Raw DrugBank ID (e.g. ``"db00001"``).

    Returns
    -------
    str
        Canonical DrugBank ID (e.g. ``"DB00001"``).

    Examples
    --------
    >>> normalize_drugbank_id("db00001")
    'DB00001'
    >>> normalize_drugbank_id("  DB00001  ")
    'DB00001'
    >>> normalize_drugbank_id(None)
    ''
    """
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    return s.strip().upper()


def normalize_chembl_id(s: str) -> str:
    """Normalize a ChEMBL ID to canonical form.

    - Uppercase (ChEMBL IDs are ``CHEMBL`` + digits by spec).
    - Strip leading/trailing whitespace.
    - Non-string inputs are coerced via ``str()``.

    Returns ``""`` for ``None`` input.

    Parameters
    ----------
    s : str
        Raw ChEMBL ID (e.g. ``"chembl123"``).

    Returns
    -------
    str
        Canonical ChEMBL ID (e.g. ``"CHEMBL123"``).

    Examples
    --------
    >>> normalize_chembl_id("chembl123")
    'CHEMBL123'
    >>> normalize_chembl_id("  CHEMBL123  ")
    'CHEMBL123'
    >>> normalize_chembl_id(None)
    ''
    """
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    return s.strip().upper()


def normalize_pubchem_cid(s: Any) -> Optional[int]:
    """Normalize a PubChem CID to canonical form: an integer with no leading zeros.

    Accepts ``int``, ``float`` (must be integer-valued), numeric ``str``
    (leading zeros stripped), and pandas/numpy nullable numeric types.
    Returns ``None`` for ``None``, NaN/NA, non-numeric strings, booleans
    (a CID is never a boolean), and non-integer floats.

    Parameters
    ----------
    s : Any
        Raw PubChem CID (e.g. ``"2244"``, ``2244``, ``2244.0``,
        ``"0002244"``).

    Returns
    -------
    int or None
        Canonical PubChem CID as a plain Python ``int``, or ``None`` if
        the input cannot be coerced.

    Examples
    --------
    >>> normalize_pubchem_cid("2244")
    2244
    >>> normalize_pubchem_cid("0002244")
    2244
    >>> normalize_pubchem_cid(2244)
    2244
    >>> normalize_pubchem_cid(2244.0)
    2244
    >>> normalize_pubchem_cid(2244.5) is None
    True
    >>> normalize_pubchem_cid(None) is None
    True
    >>> normalize_pubchem_cid(True) is None
    True
    """
    if s is None:
        return None
    # Booleans are a subclass of int — exclude them explicitly because a
    # CID is never a boolean (and ``int(True) == 1`` would otherwise sneak
    # a fake CID into the output).
    if isinstance(s, bool):
        return None
    if isinstance(s, str):
        s = s.strip()
        if not s:
            return None
        # Strip leading zeros (but always keep at least one digit so "0"
        # round-trips). ``int("0002244") == 2244`` would already do this,
        # but we do it explicitly so the behavior is obvious from the code.
        s = s.lstrip("0") or "0"
        try:
            cid = int(s)
        except ValueError:
            return None
        # ROOT FIX (Finding 16, P2): PubChem CIDs start at 1 (CID 1 =
        # formaldehyde). CID 0 is NOT a valid PubChem identifier. The
        # previous code accepted 0 as valid, which silently slipped past
        # normalization and reached the DB (no CHECK constraint on
        # pubchem_cid > 0). Downstream PubChem enrichment lookups would
        # 404 on CID 0.
        if cid == 0:
            return None
        return cid
    if isinstance(s, int):
        # ROOT FIX (Finding 16, P2): reject int 0 too.
        if s == 0:
            return None
        return int(s)
    if isinstance(s, float):
        if math.isnan(s):
            return None
        if s != int(s):
            return None
        # ROOT FIX (Finding 16, P2): reject float 0.0 too.
        if s == 0.0:
            return None
        return int(s)
    # Fall through for numpy / pandas nullable numeric types. ``float(s)``
    # raises TypeError for pandas.NA (its __float__ is not defined), which
    # we want — that returns None below.
    try:
        f = float(s)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    if f != int(f):
        return None
    # ROOT FIX (Finding 16, P2): reject 0.0 here too.
    if f == 0.0:
        return None
    return int(f)


def normalize_gene_symbol(s: str) -> str:
    """Normalize an HGNC gene symbol to canonical form.

    - Uppercase (HGNC gene symbols are uppercase by convention).
    - Strip leading/trailing whitespace.
    - Non-string inputs are coerced via ``str()``.

    Returns ``""`` for ``None`` input.

    Parameters
    ----------
    s : str
        Raw gene symbol (e.g. ``"tp53"``).

    Returns
    -------
    str
        Canonical gene symbol (e.g. ``"TP53"``).

    Examples
    --------
    >>> normalize_gene_symbol("tp53")
    'TP53'
    >>> normalize_gene_symbol("  BRCA1  ")
    'BRCA1'
    >>> normalize_gene_symbol(None)
    ''
    """
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    return s.strip().upper()

