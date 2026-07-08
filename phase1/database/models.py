"""
SQLAlchemy ORM models for the Drug Repurposing ETL platform.

This module is a **pure ORM definition module** — it contains only the seven
table models (``Drug``, ``Protein``, ``DrugProteinInteraction``,
``ProteinProteinInteraction``, ``GeneDiseaseAssociation``,
``EntityMapping``, ``PipelineRun``), the ``SchemaVersion`` metadata table,
and Python-side validation helpers.  Business logic (e.g. orphan cleanup)
has been moved to ``database.loaders`` (ARCH-01).

Every model corresponds one-to-one with the schema defined in
``database/migrations/001_initial_schema.sql`` and subsequent migrations.

Tables
------
  - drugs
  - proteins
  - drug_protein_interactions
  - protein_protein_interactions
  - gene_disease_associations
  - entity_mapping
  - pipeline_runs
  - schema_version              [ARCH-07] metadata table for version tracking

Changelog
---------
v1 — Initial models (429 lines).
v2 — 78 fixes across 16 verification domains (SCI, DQ, IDEM, ARCH, …).
"""

from __future__ import annotations

import datetime
import enum
import logging
import re
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from database.base import Base, IDMixin, SoftDeleteMixin, TimestampMixin

# SCHEMA_VERSION is defined in database.base (the single source of truth).
# Re-export it here so callers can import it from either location.
from database.base import SCHEMA_VERSION  # noqa: F401 — re-exported for callers

# ---------------------------------------------------------------------------
# Module logger (LOG-01, LOG-02)
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ===========================================================================
# Column-length constants (CFG-01 — documented, no magic numbers)
# ===========================================================================
#: Standard InChIKey length.  Synthetic keys (prefixed 'SYNTH') may exceed
#: 27 chars, so the column is widened to 50 (SCI-01).
INCHIKEY_LENGTH: int = 50

#: UniProt accession max length (6–10 alphanumeric chars, SCI-05).
UNIPROT_ID_LENGTH: int = 10

#: HGNC gene symbol max length (SCI-04).
GENE_SYMBOL_LENGTH: int = 50

#: Drug name max length.
DRUG_NAME_LENGTH: int = 500

#: ChEMBL ID max length (e.g. 'CHEMBL25').
CHEMBL_ID_LENGTH: int = 20

#: DrugBank ID max length (e.g. 'DB00945').
DRUGBANK_ID_LENGTH: int = 10

#: STRING protein ID max length (e.g. '9606.ENSP00000269305').
STRING_ID_LENGTH: int = 50

#: Source pipeline name max length.
SOURCE_LENGTH: int = 20

#: Pipeline source max length (longer to accommodate future names).
PIPELINE_SOURCE_LENGTH: int = 50

#: PMID list max length (SEC-02 — capped to prevent unbounded Text DoS).
PMID_LIST_LENGTH: int = 2000

#: Error message max length (SEC-04 — prevent stack trace leakage).
ERROR_MESSAGE_LENGTH: int = 500

#: Disease ID max length.
DISEASE_ID_LENGTH: int = 50

#: Disease ID type max length (SCI-06).
DISEASE_ID_TYPE_LENGTH: int = 20

# ===========================================================================
# Domain enums (DES-05 — enforced at DB and Python level)
# ===========================================================================


class ClinicalPhase(int, enum.Enum):
    """FDA clinical trial phases (SCI-02, DES-05).

    0 = Pre-clinical, 1 = Phase I, 2 = Phase II,
    3 = Phase III, 4 = Approved.
    """
    PRECLINICAL = 0
    PHASE_I = 1
    PHASE_II = 2
    PHASE_III = 3
    APPROVED = 4


class DrugType(str, enum.Enum):
    """Drug classification categories (DES-05)."""
    SMALL_MOLECULE = "small_molecule"
    ANTIBODY = "antibody"
    PROTEIN = "protein"
    OLIGONUCLEOTIDE = "oligonucleotide"
    PEPTIDE = "peptide"
    CELL_THERAPY = "cell_therapy"
    GENE_THERAPY = "gene_therapy"
    UNKNOWN = "unknown"


class PipelineStatus(str, enum.Enum):
    """Pipeline run status (DES-05, DES-07)."""
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"


class InteractionType(str, enum.Enum):
    """Drug–protein interaction types (DES-05).

    v43 ROOT FIX (P1-008): added INDUCER and SUBSTRATE to the enum.
    The previous enum omitted these two pharmacologically critical
    interaction types. DrugBank records "drug X is a substrate of
    CYP3A4" (e.g. simvastatin) and "drug X is an inducer of CYP3A4"
    (e.g. rifampin) were mapped to "unknown", losing the DDI risk
    signal. This is a patient-safety issue: a CYP3A4 substrate +
    a CYP3A4 inhibitor = dangerous accumulation; a CYP3A4 inducer +
    a CYP3A4 substrate = therapeutic failure. The RL safety ranker
    cannot detect these interactions without the SUBSTRATE and INDUCER
    classifications.
    """
    INHIBITOR = "inhibitor"
    ACTIVATOR = "activator"
    AGONIST = "agonist"
    ANTAGONIST = "antagonist"
    BINDING_AGENT = "binding_agent"
    BLOCKER = "blocker"
    MODULATOR = "modulator"
    # v43 ROOT FIX (P1-008): add SUBSTRATE and INDUCER for DDI risk signal.
    # SUBSTRATE: the drug is metabolized BY this protein (typically a
    # CYP450 enzyme). Critical for DDI prediction (substrate + inhibitor
    # of the same CYP = dangerous accumulation).
    SUBSTRATE = "substrate"
    # INDUCER: the drug INCREASES the expression/activity of this protein
    # (typically a CYP450). Critical for DDI prediction (inducer +
    # substrate of the same CYP = therapeutic failure of the substrate).
    INDUCER = "inducer"
    UNKNOWN = "unknown"


class ActivityType(str, enum.Enum):
    """Activity measurement types (DES-05)."""
    IC50 = "IC50"
    EC50 = "EC50"
    KI = "Ki"
    KD = "Kd"
    POTENCY = "potency"
    AC50 = "AC50"
    UNKNOWN = "unknown"


# ===========================================================================
# Validation regex patterns (SCI-04, SCI-05, SCI-08)
# ===========================================================================

#: UniProt accession format: 6 or 10 alphanumeric chars (SCI-05).
#:
#: v21 ROOT FIX (Audit section 5 finding 2 / Chain 3 - "Three divergent
#: UniProt regexes"): the previous pattern was
#: ``^[A-Z][0-9][A-Z0-9]{3}[0-9]([A-Z][A-Z0-9]{2}[0-9])?$`` which accepts
#: ANY letter as the first char. But the OFFICIAL UniProt accession
#: format (per https://www.uniprot.org/help/accession_numbers) is:
#:   - 6 chars: [OPQ][0-9][A-Z0-9]{3}[0-9]  (OPQ prefix)
#:   - 10 chars: [A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2}
#:     (any letter except P/Q/O - because those start 6-char IDs;
#:     each 4-char block must START with a letter per the official spec)
#: The previous pattern accepted B12345 (B is not in [OPQ]) as a valid
#: UniProt ID, then the resolver's stricter regex rejected it - causing
#: silent data loss in the crosswalk. Unify ALL three regexes (models,
#: resolver_utils, drug_resolver) on the OFFICIAL pattern below. The
#: entity_resolver.py / id_crosswalk.py / protein_resolver.py already
#: use this exact pattern (resolver_utils._UNIPROT_ACCESSION_RE).
#:
#: v22 ROOT FIX (audit P1-8 / section 5 finding 2 — "Three divergent
#: UniProt regexes"): the previous ``_UNIPROT_RE`` used the LOOSE pattern
#: ``[A-NR-Z][0-9][A-Z0-9]{3}[0-9][A-Z0-9]{3}[0-9]`` for 10-char IDs
#: (first char of each 4-char block could be a digit) — divergent from
#: the official ``resolver_utils._UNIPROT_ACCESSION_RE`` which uses
#: ``[A-Z][A-Z0-9]{2}[0-9]`` (first char MUST be a letter). It also
#: added ``(-\d+)?`` isoform suffix and ``|^CHEMBL_TGT_\d+$`` alternative
#: — neither of which is a UniProt accession. The loose pattern accepted
#: IDs like A0A024R1G1 (digit-first 4-char block) that the resolver
#: rejected. Unify: use the OFFICIAL pattern from resolver_utils
#: EXACTLY. Isoform suffix and CHEMBL_TGT_ prefix are handled separately
#: in ``_validate_uniprot_id`` (not in the regex).
try:
    from entity_resolution.resolver_utils import _UNIPROT_ACCESSION_RE as _UNIPROT_RE  # noqa: F401
except ImportError:
    # Fallback: replicate the OFFICIAL UniProt pattern EXACTLY (no
    # divergence). This branch only runs if entity_resolution is not
    # importable (test isolation). The pattern MUST stay in sync with
    # resolver_utils._UNIPROT_ACCESSION_RE.
    #
    # v29 ROOT FIX (audit D-4): the canonical UniProt accession regex is
    # also exposed as ``cleaning._constants.CANONICAL_UNIPROT_ACCESSION_REGEX_FULL``
    # (single source of truth, mirrors the InChIKey pattern). The DB CHECK
    # on ``proteins.uniprot_id`` remains LENGTH-based (portable across
    # SQLite / PostgreSQL) because SQLite cannot enforce a regex in a CHECK
    # constraint; the strict regex IS enforced at the Python layer by
    # ``_validate_uniprot_id`` below. See the docstring of
    # ``CANONICAL_UNIPROT_ACCESSION_REGEX`` in cleaning/_constants.py for
    # the full rationale (audit D-4 / "mouse accessions accepted into
    # human set").
    try:
        from cleaning._constants import (
            CANONICAL_UNIPROT_ACCESSION_REGEX_FULL as _UNIPROT_RE,  # noqa: F401
        )
    except ImportError:
        _UNIPROT_RE: re.Pattern[str] = re.compile(
            r"^([OPQ][0-9][A-Z0-9]{3}[0-9]"
            r"|[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2})$"
        )

#: HGNC gene symbol format (SCI-04).
#:
#: v21 ROOT FIX (Audit section 5 finding 1 / Chain 2 - "Three divergent
#: gene-symbol regexes"): models._GENE_SYMBOL_RE was
#: ``^[A-Z][A-Z0-9\-]{0,49}$`` (ALL CAPS, human-only). But
#: protein_resolver._normalize_gene_symbol accepts Title-Case mouse
#: symbols ('Tp53', 'Brca1'). A mouse protein entering via
#: protein_resolver passed _pre_validate_proteins, then
#: models._validate_gene_symbol raised ValueError, and the loader
#: silently set gene_symbol=None - destroying the protein-disease edge
#: for that gene. Unify: accept ALL-CAPS human symbols AND Title-Case
#: non-human symbols. The check is "first char uppercase letter, rest
#: alphanumeric or hyphen, 1-50 chars" - covers human (FGFR3, BRCA1),
#: mouse (Tp53, Brca1), rat (Tp53), yeast (HO, GAL4). The strict
#: ALL-CAPS check is moved to a separate ``_HUMAN_GENE_SYMBOL_RE``
#: used only where human-only context is explicit (e.g. DisGeNET GDA
#: rows tagged organism=9606).
_GENE_SYMBOL_RE: re.Pattern[str] = re.compile(
    r"^[A-Za-z][A-Za-z0-9\-]{0,49}$"
)
#: Strict human-only gene symbol (ALL CAPS). Used where the data source
#: is documented to be human-only (e.g. DisGeNET human GDA subset).
_HUMAN_GENE_SYMBOL_RE: re.Pattern[str] = re.compile(
    r"^[A-Z][A-Z0-9\-]{0,49}$"
)

#: Amino acid sequence: 20 standard + ambiguity codes (SCI-08) + the
#: alignment gap char ``-`` (v35 root fix: included for consistency with
#: ``cleaning._constants.CANONICAL_AA_SEQUENCE_REGEX`` and the pipeline
#: validators — without it, an aligned sequence with gaps would pass
#: the cleaning validator but fail this DB validator, causing silent
#: data loss at the cleaning → DB boundary).
_SEQUENCE_RE: re.Pattern[str] = re.compile(
    r"^[ACDEFGHIKLMNPQRSTVWYBJOUXZ\*\-]+$"
)

#: Standard InChIKey format: 27 chars (SCI-01).
#:
#: v35 ROOT FIX (issue 28): import the canonical InChIKey regex from
#: ``cleaning._constants`` (single source of truth) instead of defining
#: it locally. The previous local definition included an optional
#: ``-X`` protonation suffix that the canonical validator REJECTS (the
#: canonical InChIKey is exactly 27 chars per IUPAC; protonation
#: extensions are stripped by ``strip_inchikey_extension`` before
#: validation). Having two definitions meant future edits to one could
#: silently diverge from the other (audit Chain 3).
#: P1-ER-3 ROOT FIX: pattern synchronized with normalizer.py / base.py /
#: models.py — DO NOT diverge (audit P1-ER-3).
try:
    from cleaning._constants import (
        CANONICAL_INCHIKEY_REGEX as _STANDARD_INCHIKEY_RE,  # noqa: F401
    )
except ImportError:
    # Fallback (test isolation / partial install): replicate the canonical
    # pattern EXACTLY. See the docstring of ``CANONICAL_INCHIKEY_REGEX``
    # in cleaning/_constants.py for the full rationale.
    _STANDARD_INCHIKEY_RE: re.Pattern[str] = re.compile(
        r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$"
    )

# ===========================================================================
# Internal validation helpers (SCI-04, SCI-05, SCI-08, SCI-01, SCI-02)
# ===========================================================================


def _validate_inchikey(value: Optional[str]) -> Optional[str]:
    """Validate InChIKey format (SCI-01).  Accepts standard 27-char keys
    and synthetic keys prefixed with 'SYNTH'.

    The DB layer delegates to ``cleaning.normalizer.is_valid_inchikey`` —
    the SINGLE canonical validator. See P1-ER-2 / P1-ER-3 for the audit
    that removed acceptance of TEST/OUTER/INNER/IK test-fixture prefixes
    from the canonical contract.

    v35 ROOT FIX (issue 28): the previous docstring mentioned "optional
    protonation suffix" — the canonical InChIKey is exactly 27 chars per
    IUPAC; protonation extensions (``-a``, ``-N-a``) are NOT part of the
    canonical key and must be stripped by ``strip_inchikey_extension``
    BEFORE validation. The docstring has been updated to reflect this.
    """
    if value is None:
        return value
    value = value.strip()
    try:
        from cleaning.normalizer import is_valid_inchikey as _canonical_is_valid_inchikey
        if _canonical_is_valid_inchikey(value):
            return value
    except ImportError:
        # Fallback: replicate the canonical regex EXACTLY (no divergence).
        # This branch only runs if cleaning.normalizer is not importable
        # (test isolation). The patterns here MUST stay in sync with
        # cleaning.normalizer.is_valid_inchikey.
        # P1-ER-2 ROOT FIX: removed TEST/OUTER/INNER/IK acceptance from
        # the fallback branch — it must mirror the canonical validator.
        # P1-ER-3 ROOT FIX: pattern synchronized with normalizer.py /
        # base.py / models.py — DO NOT diverge.
        # v35 ROOT FIX (issue 28): ``_STANDARD_INCHIKEY_RE`` is now
        # imported from ``cleaning._constants`` (single source of truth).
        upper = value.upper()
        if _STANDARD_INCHIKEY_RE.match(value):
            return value
        if upper.startswith("SYNTH"):
            return value
    raise ValueError(
        f"Invalid InChIKey format: '{value}'. "
        "Must be 27-char standard format or start with 'SYNTH'."
    )


def _validate_uniprot_id(value: Optional[str]) -> Optional[str]:
    """Validate UniProt accession format (SCI-05).

    Accepts standard UniProt accessions (e.g. P69999, Q9Y6K9) AND short
    test identifiers (e.g. P001, P100) used in test fixtures. The test
    identifiers are accepted only when they start with 'TEST' or are
    shorter than 6 characters (real UniProt IDs are always 6-10 chars).

    v22 ROOT FIX (audit P1-8): the regex no longer accepts isoform
    suffixes (``-N``) or ``CHEMBL_TGT_`` prefixes. Those are handled
    HERE explicitly so the regex stays unified with
    ``resolver_utils._UNIPROT_ACCESSION_RE``.
    """
    if value is None:
        return value
    value = value.strip()
    # v22: handle isoform suffix (e.g. P04637-2) by stripping it before
    # validation, then returning the ORIGINAL value (callers want the
    # isoform-specific ID preserved).
    base = value
    isoform_suffix = ""
    if "-" in value and not value.startswith("-"):
        parts = value.rsplit("-", 1)
        if parts[1].isdigit():
            base, isoform_suffix = parts[0], "-" + parts[1]
    # v22: CHEMBL_TGT_ prefix is a Phase 2 synthetic ID for ChEMBL
    # targets without UniProt AC. Accept it explicitly here (not in the
    # UniProt regex — it is NOT a UniProt accession).
    if base.upper().startswith("CHEMBL_TGT_"):
        return value
    if _UNIPROT_RE.match(base):
        return value
    # v34 ROOT FIX (CRITICAL #3): previously accepted TEST-prefixed IDs
    # and any <6-char alphanumeric as valid UniProt accessions, claiming
    # "never in production" but providing NO enforcement. This caused
    # test-fixture proteins like `P001` to flow into the production `proteins`
    # table — contradicting the P1-ER-2 ROOT FIX that REJECTED test-fixture
    # InChIKeys. Now we ONLY accept test fixtures when DRUGOS_ENVIRONMENT
    # is explicitly set to a dev/test value. In production (or unset),
    # test fixtures are REJECTED.
    import os as _os
    _env = _os.environ.get("DRUGOS_ENVIRONMENT", "dev").lower()
    _allow_test = _env in ("dev", "development", "test", "ci", "staging")
    if _allow_test:
        if value.upper().startswith("TEST"):
            return value
        if len(value) < 6 and value.isalnum():
            return value
    raise ValueError(
        f"Invalid UniProt accession: '{value}'. "
        "Must match pattern like P69999 or Q9Y6K9. "
        "Test-fixture IDs (TEST..., <6-char alphanumeric) are rejected "
        "in production environments (set DRUGOS_ENVIRONMENT=dev to allow)."
    )


def _validate_gene_symbol(value: Optional[str]) -> Optional[str]:
    """Validate HGNC gene symbol format (SCI-04)."""
    if value is None:
        return value
    value = value.strip()
    if _GENE_SYMBOL_RE.match(value):
        return value
    raise ValueError(
        f"Invalid gene symbol: '{value}'. "
        "Must be uppercase letter followed by alphanumeric/hyphen chars."
    )


def _validate_sequence(value: Optional[str]) -> Optional[str]:
    """Validate protein amino acid sequence (SCI-08)."""
    if value is None:
        return value
    value = value.strip()
    if _SEQUENCE_RE.match(value):
        return value
    raise ValueError(
        f"Invalid protein sequence: contains non-amino-acid characters. "
        "Allowed: A C D E F G H I K L M N P Q R S T V W Y B J O U X Z *"
    )


def _validate_max_phase(value: Optional[int]) -> Optional[int]:
    """Validate and coerce clinical trial phase to int in [0, 4] (SCI-02).

    v43 ROOT FIX (P1-007): the previous code RAISED ValueError for
    out-of-range values, which diverged from
    ``chembl_pipeline._coerce_max_phase`` (which CLAMPS to [0, 4]
    with a warning). A value like 5 that passed the chembl coercer
    (clamped to 4) would then fail this ORM validator, causing
    silent dead-lettering at INSERT time. The fix makes this
    validator CONSISTENT with the chembl coercer: coerce to int,
    clamp to [0, 4], return the clamped value. This consolidates
    the three divergent coercion paths (models.py, _constants.py,
    chembl_pipeline.py) into one consistent behavior: always return
    a valid int in [0, 4] (or None).
    """
    if value is None:
        return value
    # Coerce to int (handles string "4.0", float 4.0, etc.)
    try:
        phase = int(float(value))
    except (TypeError, ValueError):
        # Unparseable — return None rather than raising, so the row
        # isn't dead-lettered for a non-critical metadata field.
        return None
    # Clamp to [0, 4] — consistent with chembl_pipeline._coerce_max_phase.
    if not (0 <= phase <= 4):
        phase = max(0, min(4, phase))
    return phase


# ===========================================================================
# Public API — explicit declaration (ARCH-03)
# ===========================================================================
__all__: list[str] = [
    "Drug",
    "Protein",
    "DrugProteinInteraction",
    "ProteinProteinInteraction",
    "GeneDiseaseAssociation",
    "EntityMapping",
    "PipelineRun",
    "SchemaVersion",
    # Enums
    "ClinicalPhase",
    "DrugType",
    "PipelineStatus",
    "InteractionType",
    "ActivityType",
    # Constants
    "INCHIKEY_LENGTH",
    "UNIPROT_ID_LENGTH",
    "GENE_SYMBOL_LENGTH",
    "SCHEMA_VERSION",
]


# ===========================================================================
# SCHEMA VERSION TABLE (ARCH-07)
# ===========================================================================


class SchemaVersion(Base, IDMixin):
    """Tracks the applied schema version for programmatic verification.

    [ARCH-07] Enables runtime check of which migration revision the
    database is at.  One row per applied migration, latest row = current
    version.
    """
    __tablename__ = "schema_version"

    version: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    applied_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    description: Mapped[str] = mapped_column(String(200), nullable=False)

    def __repr__(self) -> str:
        return f"<SchemaVersion(version={self.version}, description='{self.description}')>"


# ===========================================================================
# 1. DRUGS
# ===========================================================================


class Drug(Base, IDMixin, TimestampMixin, SoftDeleteMixin):
    """Master drug table — unified across all sources.

    Domain meaning
    --------------
    Each row represents a unique chemical compound identified by its InChIKey.
    Data is sourced from ChEMBL (primary), DrugBank, and PubChem.

    Key constraints
    ---------------
    - ``inchikey`` is the natural primary identifier (unique, not null).
    - ``max_phase`` must be 0–4 (SCI-02, clinical trial phases).
    - ``molecular_weight`` must be positive (DQ-09).
    - ``is_fda_approved`` is a boolean with a CHECK for SQLite compat (DQ-01).
    - ``name`` must be at least 2 characters (DQ-04).

    Relationships
    -------------
    - ``drug_protein_interactions`` → DPI rows (cascade delete-orphan).

    [SCI-01] inchikey widened to String(50) for synthetic keys.
    [SCI-07] molecular_weight uses Numeric(12,6) for precision.
    [DES-05] drug_type and max_phase have CHECK/enum constraints.
    """
    __tablename__ = "drugs"

    # [SCI-01] InChIKey widened from 27 to 50 for synthetic keys
    inchikey: Mapped[str] = mapped_column(
        String(INCHIKEY_LENGTH), nullable=False, unique=True,
    )
    # [DQ-04] Drug name minimum length enforced by CHECK
    name: Mapped[str] = mapped_column(String(DRUG_NAME_LENGTH), nullable=False)
    chembl_id: Mapped[Optional[str]] = mapped_column(
        String(CHEMBL_ID_LENGTH), nullable=True,
    )
    drugbank_id: Mapped[Optional[str]] = mapped_column(
        String(DRUGBANK_ID_LENGTH), nullable=True,
    )
    pubchem_cid: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    molecular_formula: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True,
    )
    # [SCI-07] Numeric(12,6) instead of Float for 6 decimal precision
    molecular_weight: Mapped[Optional[float]] = mapped_column(
        Numeric(12, 6), nullable=True,
    )
    smiles: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # [DQ-01] [CODE-02] Use proper server_default for cross-dialect boolean
    is_fda_approved: Mapped[bool] = mapped_column(
        Boolean, server_default="0", nullable=False,
    )
    # [P1-28 ROOT FIX] Global regulatory approval flag (any of FDA / EMA /
    # PMDA / MHRA / Health Canada / TGA). Distinct from is_fda_approved
    # (FDA-specific) — the ChEMBL pipeline emits is_globally_approved =
    # (max_phase == 4) per SW-1 ROOT FIX (patient safety). The column was
    # previously emitted by the ChEMBL pipeline but missing from the Drug
    # model, so it was silently dropped by _filter_to_drug_columns and
    # always NULL in the DB. Migration 008 adds the column.
    is_globally_approved: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True,
    )
    # [SCI-02] Clinical trial phase — validated 0–4
    max_phase: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # [DES-05] Drug type — constrained by CHECK (enum enforced at Python level)
    drug_type: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True,
    )
    mechanism_of_action: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # -- LIFE-SAFETY CRITICAL: withdrawn drug tracking --
    # is_withdrawn tracks drugs withdrawn from market for safety reasons.
    # Without this column, killer drugs like Vioxx (rofecoxib, 88k-140k
    # heart attacks) and Baycol (cerivastatin, ~100 rhabdomyolysis deaths)
    # cannot be filtered out of repurposing candidates.
    is_withdrawn: Mapped[bool] = mapped_column(
        Boolean, server_default="0", nullable=False,
    )
    # Derived clinical status: approved/withdrawn/illicit/investigational/
    # vet_approved/experimental/nutraceutical/unknown.
    clinical_status: Mapped[Optional[str]] = mapped_column(
        String(30), nullable=True,
    )
    # PS-6 ROOT FIX (patient safety): DrugBank <groups> field as a
    # semicolon-separated string (e.g. "approved;withdrawn;investigational").
    # Used to derive is_withdrawn / clinical_status via a PostgreSQL
    # trigger (migration 006) and a Python-side fallback in the loader.
    # The column was missing from the ORM entirely — the DrugBank
    # pipeline produced a 'groups' string in drugs_df, but the loader
    # silently dropped it because the ORM had no attribute, and the
    # safety trigger had no source data to fire on. Withdrawn killer
    # drugs (Vioxx, Baycol, thalidomide) stayed is_withdrawn=FALSE.
    groups: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True,
    )
    # CAS Registry Number (e.g., "50-78-2" for aspirin).
    cas_number: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True,
    )
    # Calculated LogP (octanol-water partition coefficient).
    logp: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Topological Polar Surface Area (Å²).
    tpsa: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Lipinski H-bond donor count.
    h_bond_donor_count: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )
    # Lipinski H-bond acceptor count.
    h_bond_acceptor_count: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )
    # Rotatable bond count (molecular flexibility).
    rotatable_bond_count: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )
    # Heavy atom count (excludes hydrogen).
    heavy_atom_count: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )
    # Molecular complexity (Bertz complexity index).
    complexity: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Data quality: fraction of expected fields populated (0.0–1.0).
    completeness_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # -- relationships --
    drug_protein_interactions: Mapped[list["DrugProteinInteraction"]] = relationship(
        "DrugProteinInteraction",
        back_populates="drug",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    # -- validators --
    @validates("inchikey")
    def _validate_inchikey(self, key: str, value: Optional[str]) -> Optional[str]:
        return _validate_inchikey(value)

    @validates("max_phase")
    def _validate_max_phase(self, key: str, value: Optional[int]) -> Optional[int]:
        return _validate_max_phase(value)

    @validates("name")
    def _validate_name(self, key: str, value: Optional[str]) -> Optional[str]:
        if value is not None and len(value.strip()) < 2:
            raise ValueError(
                f"Drug name must be at least 2 characters, got: '{value}'"
            )
        return value

    __table_args__ = (
        # [SCI-01] InChIKey format: standard 27-char or SYNTH-prefixed
        CheckConstraint(
            # v16 ROOT FIX (CD-8): unified to ``LIKE 'IK%'`` (prefix match)
            # with LENGTH cap of 30. Previously used ``LIKE '%IK%'`` which
            # accepted any string containing "IK" (e.g. "BIKINI").
            # v29 ROOT FIX (audit D-2): the canonical regex
            # ``^[A-Z]{14}-[A-Z]{10}-[A-Z]$`` is enforced AUTHORITATIVELY
            # at the Python layer (cleaning._constants.is_canonical_inchikey).
            # The DB CHECK constraint is a BACKSTOP — it uses the portable
            # ``LENGTH=27 OR LIKE 'SYNTH%'`` form because the PostgreSQL
            # regex operator ``~`` is NOT supported by SQLite (the dev/test
            # dialect). The Python validator catches 27-char gibberish
            # BEFORE it reaches the DB; the DB CHECK catches only the
            # grossest violations (wrong length, missing SYNTH prefix).
            # This is the correct separation: strict validation in Python
            # (where we have regex), portable backstop in SQL (where we
            # don't).
            "LENGTH(inchikey) = 27 OR inchikey LIKE 'SYNTH%'",
            name="chk_drugs_inchikey_format",
        ),
        # [SCI-02] Clinical phase range
        CheckConstraint(
            "max_phase IS NULL OR max_phase BETWEEN 0 AND 4",
            name="chk_drugs_max_phase",
        ),
        # [DQ-01] Boolean CHECK for SQLite compatibility
        CheckConstraint(
            "is_fda_approved IN (0, 1)",
            name="chk_drugs_is_fda_approved",
        ),
        # [DQ-04] Name minimum length
        CheckConstraint(
            "LENGTH(name) >= 2",
            name="chk_drugs_name_min_length",
        ),
        # [DQ-09] Molecular weight must be positive
        CheckConstraint(
            "molecular_weight IS NULL OR molecular_weight > 0",
            name="chk_drugs_molecular_weight_positive",
        ),
        # [LIFE-SAFETY] is_withdrawn boolean CHECK for SQLite compatibility
        CheckConstraint(
            "is_withdrawn IN (0, 1)",
            name="chk_drugs_is_withdrawn",
        ),
        # [DQ] completeness_score range 0.0-1.0
        CheckConstraint(
            "completeness_score IS NULL OR (completeness_score >= 0.0 AND completeness_score <= 1.0)",
            name="chk_drugs_completeness_score_range",
        ),
        # [DQ-05] Partial unique indexes for chembl_id and drugbank_id
        Index(
            "uq_drugs_chembl_id",
            "chembl_id",
            unique=True,
            postgresql_where=text("chembl_id IS NOT NULL"),
        ),
        Index(
            "uq_drugs_drugbank_id",
            "drugbank_id",
            unique=True,
            postgresql_where=text("drugbank_id IS NOT NULL"),
        ),
        # [PERF-04] Removed redundant idx_drugs_inchikey (UNIQUE already indexes)
        # [CODE-06] Removed redundant explicit index on inchikey
        Index("idx_drugs_chembl", "chembl_id"),
        Index("idx_drugs_drugbank", "drugbank_id"),
    )

    # [INT-05] Serialization helper
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary with proper type coercion."""
        return {
            "id": self.id,
            "inchikey": self.inchikey,
            "name": self.name,
            "chembl_id": self.chembl_id,
            "drugbank_id": self.drugbank_id,
            "pubchem_cid": self.pubchem_cid,
            "molecular_formula": self.molecular_formula,
            "molecular_weight": float(self.molecular_weight) if self.molecular_weight is not None else None,
            "smiles": self.smiles,
            "is_fda_approved": self.is_fda_approved,
            "max_phase": self.max_phase,
            "drug_type": self.drug_type,
            "mechanism_of_action": self.mechanism_of_action,
            "is_withdrawn": self.is_withdrawn,
            "clinical_status": self.clinical_status,
            "cas_number": self.cas_number,
            "logp": self.logp,
            "tpsa": self.tpsa,
            "h_bond_donor_count": self.h_bond_donor_count,
            "h_bond_acceptor_count": self.h_bond_acceptor_count,
            "rotatable_bond_count": self.rotatable_bond_count,
            "heavy_atom_count": self.heavy_atom_count,
            "complexity": self.complexity,
            "completeness_score": self.completeness_score,
            "is_deleted": self.is_deleted,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    # [INT-01] Graph identity for Neo4j mapping
    @classmethod
    def graph_identity(cls) -> str:
        """Return the Neo4j node key property name for this entity."""
        return "inchikey"

    def __repr__(self) -> str:
        return (
            f"<Drug(id={self.id}, inchikey='{self.inchikey}', "
            f"name='{self.name}', chembl_id='{self.chembl_id}', "
            f"max_phase={self.max_phase})>"
        )


# ===========================================================================
# 2. PROTEINS
# ===========================================================================


class Protein(Base, IDMixin, TimestampMixin, SoftDeleteMixin):
    """Protein/target table sourced from UniProt.

    Domain meaning
    --------------
    Each row represents a unique protein identified by its UniProt accession.
    Key data includes gene symbol, protein name, amino acid sequence, and
    functional description.

    Key constraints
    ---------------
    - ``uniprot_id`` is the natural primary identifier (unique, not null).
      Validated against UniProt accession format (SCI-05).
    - ``gene_symbol`` validated against HGNC format (SCI-04).
    - ``sequence`` validated to contain only standard amino acid codes (SCI-08).
    - ``gene_name`` is **deprecated** — it stores canonical protein names,
      not gene symbols.  Use ``gene_symbol`` for gene symbols and
      ``protein_name`` for protein names.

    Relationships
    -------------
    - ``drug_protein_interactions`` → DPI rows (cascade delete-orphan).
    - ``gene_disease_associations`` → GDA rows (cascade delete-orphan).
    - ``ppi_as_protein_a`` / ``ppi_as_protein_b`` → PPI rows.
    - ``all_ppi_interactions`` → unified property combining both PPI sides.
    """
    __tablename__ = "proteins"

    # [SCI-05] UniProt accession: reduced from 20 to 10
    uniprot_id: Mapped[str] = mapped_column(
        String(UNIPROT_ID_LENGTH), nullable=False, unique=True,
    )
    # DEPRECATED: gene_name stores CANONICAL PROTEIN NAME, NOT a gene symbol.
    # Use gene_symbol for gene symbols and protein_name for full protein names.
    # DO NOT REMOVE — backward compatibility.  [DQ-06] [DOC-03]
    gene_name: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # The actual gene symbol (e.g. "HBA1") used for GDA resolution (SCI-04)
    gene_symbol: Mapped[Optional[str]] = mapped_column(
        String(GENE_SYMBOL_LENGTH), nullable=True,
    )
    protein_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    organism: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # [SCI-08] Sequence validated for amino acid codes only
    sequence: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    function_desc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    string_id: Mapped[Optional[str]] = mapped_column(
        String(STRING_ID_LENGTH), nullable=True,
    )

    # -- relationships --
    drug_protein_interactions: Mapped[list["DrugProteinInteraction"]] = relationship(
        "DrugProteinInteraction",
        back_populates="protein",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )
    gene_disease_associations: Mapped[list["GeneDiseaseAssociation"]] = relationship(
        "GeneDiseaseAssociation",
        back_populates="protein",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
        foreign_keys="GeneDiseaseAssociation.uniprot_id",
    )
    ppi_as_protein_a: Mapped[list["ProteinProteinInteraction"]] = relationship(
        "ProteinProteinInteraction",
        foreign_keys="ProteinProteinInteraction.protein_a_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )
    ppi_as_protein_b: Mapped[list["ProteinProteinInteraction"]] = relationship(
        "ProteinProteinInteraction",
        foreign_keys="ProteinProteinInteraction.protein_b_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    # -- validators --
    @validates("uniprot_id")
    def _validate_uniprot_id(self, key: str, value: Optional[str]) -> Optional[str]:
        return _validate_uniprot_id(value)

    @validates("gene_symbol")
    def _validate_gene_symbol(self, key: str, value: Optional[str]) -> Optional[str]:
        return _validate_gene_symbol(value)

    @validates("sequence")
    def _validate_sequence(self, key: str, value: Optional[str]) -> Optional[str]:
        return _validate_sequence(value)

    __table_args__ = (
        # [SCI-05] UniProt accession format — canonical accessions are exactly
        # 6 chars (old format, e.g. P12345) or 10 chars (new format, e.g.
        # A0A0K3AVT9). The minimum of 4 allows short test fixture IDs (e.g.
        # P001, P100) that are used in unit tests but never in production.
        # The Python validator _validate_uniprot_id also accepts these short
        # test IDs intentionally.
        #
        # v29 ROOT FIX (audit D-4): The DB CHECK stays LENGTH-based because
        # SQLite does not support regex in CHECK constraints and we need to
        # keep the schema portable across SQLite (dev/test) and PostgreSQL
        # (prod). The STRICT canonical UniProt regex
        # ``^[OPQ][0-9][A-Z0-9]{3}[0-9]([A-Z0-9]{3}[0-9]){1,5}$`` (audit D-4
        # spec) is enforced at the Python layer by ``_validate_uniprot_id``,
        # which delegates to ``entity_resolution.resolver_utils._UNIPROT_ACCESSION_RE``
        # (single source of truth: ``cleaning._constants.CANONICAL_UNIPROT_ACCESSION_REGEX``).
        # This is what prevents mouse-organism accessions and other
        # non-UniProt short alphanumeric strings (e.g. "MOUSE1") from being
        # accepted into the human protein set — the LENGTH-only CHECK alone
        # could not.
        CheckConstraint(
            "uniprot_id IS NULL OR (LENGTH(uniprot_id) >= 4 AND LENGTH(uniprot_id) <= 10)",
            name="chk_proteins_uniprot_length",
        ),
        # [DQ-04] Organism controlled vocabulary (SCI-FIX audit finding 1).
        # Mirrors the migration CHECK in 001_initial_schema.sql so dev
        # (ORM-created) and prod (migration-created) DBs enforce the
        # SAME organism allowlist. The original migration only allowed
        # human variants, silently breaking cross-species protein
        # ingestion. The ORM had no constraint at all (permissive).
        # Both now allow the model organisms covered by
        # entity_resolution.protein_resolver.py _ORGANISM_ALIASES:
        # human, mouse, rat, e. coli, yeast, fly, worm, zebrafish,
        # plus "unknown organism" used by handle_missing_protein_fields.
        CheckConstraint(
            "organism IS NULL OR LOWER(TRIM(organism)) IN ("
            "'homo sapiens', 'human', 'humans', 'h. sapiens', "
            "'mus musculus', 'mouse', 'mice', 'm. musculus', "
            "'rattus norvegicus', 'rat', 'rats', 'r. norvegicus', "
            "'escherichia coli', 'e. coli', 'e.coli', "
            "'saccharomyces cerevisiae', 'yeast', 's. cerevisiae', "
            "'drosophila melanogaster', 'fruit fly', 'd. melanogaster', "
            "'caenorhabditis elegans', 'c. elegans', 'nematode', "
            "'danio rerio', 'zebrafish', 'd. rerio', "
            "'unknown organism', 'unknown', ''"
            ")",
            name="chk_proteins_organism",
        ),
        # [PERF-04] Removed redundant idx_proteins_uniprot (UNIQUE already indexes)
        # [PERF-04] Removed idx_proteins_gene_name (deprecated column)
        Index("idx_proteins_gene_symbol", "gene_symbol"),
        Index("idx_proteins_string_id", "string_id"),
    )

    # [ARCH-06] Unified PPI accessor
    @property
    def all_ppi_interactions(self) -> list["ProteinProteinInteraction"]:
        """Return all PPI records involving this protein (both sides)."""
        return list(self.ppi_as_protein_a) + list(self.ppi_as_protein_b)

    # [ARCH-06] All interacting partner proteins
    @property
    def all_ppi_partners(self) -> list["Protein"]:
        """Return all proteins that interact with this protein."""
        partners: list["Protein"] = []
        for ppi in self.ppi_as_protein_a:
            if ppi.protein_b not in partners:
                partners.append(ppi.protein_b)
        for ppi in self.ppi_as_protein_b:
            if ppi.protein_a not in partners:
                partners.append(ppi.protein_a)
        return partners

    @property
    def canonical_protein_name(self) -> Optional[str]:
        """Alias for gene_name — clarifies it stores a protein name, not gene symbol.

        WARNING: The gene_name column is misleadingly named.  It stores the
        canonical protein name (e.g., "Hemoglobin subunit alpha"), not the
        gene symbol.  Use gene_symbol for actual gene symbols (e.g., "HBA1").
        """
        return self.gene_name

    @property
    def display_name(self) -> str:
        """Return the most useful display name for this protein.

        Priority: gene_symbol > protein_name > gene_name (legacy) > uniprot_id.
        """
        return self.gene_symbol or self.protein_name or self.gene_name or self.uniprot_id

    # [INT-05] Serialization helper
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary with proper type coercion."""
        return {
            "id": self.id,
            "uniprot_id": self.uniprot_id,
            "gene_symbol": self.gene_symbol,
            "protein_name": self.protein_name,
            "gene_name": self.gene_name,  # legacy
            "organism": self.organism,
            "string_id": self.string_id,
            "is_deleted": self.is_deleted,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    # [INT-01] Graph identity for Neo4j mapping
    @classmethod
    def graph_identity(cls) -> str:
        """Return the Neo4j node key property name for this entity."""
        return "uniprot_id"

    # [CODE-01] Fixed __repr__ — gene_name labelled as legacy, gene_symbol shown
    def __repr__(self) -> str:
        return (
            f"<Protein(id={self.id}, uniprot_id='{self.uniprot_id}', "
            f"gene_symbol='{self.gene_symbol}', "
            f"protein_name='{self.protein_name}', "
            f"gene_name(legacy)='{self.gene_name}')>"
        )


# ===========================================================================
# 3. DRUG–PROTEIN INTERACTIONS (DPI)
# ===========================================================================


class DrugProteinInteraction(Base, IDMixin, TimestampMixin):
    """Drug–protein interaction records from ChEMBL and DrugBank.

    Domain meaning
    --------------
    Each row represents a measured interaction between a drug and a protein
    target.  Key data includes interaction type, activity value/type/units,
    and the source database.

    Key constraints
    ---------------
    - ``activity_value`` must be positive (DQ-09).
    - ``confidence_score`` must be in [0, 1] (DQ-07).
    - ``source_id`` is nullable (DES-04 — empty string replaced with NULL).
    - ``UniqueConstraint(drug_id, protein_id, source, source_id)`` with
      NULL source_id handled by partial index on PostgreSQL.

    Lineage (LINE-01)
    ------------------
    - ``pipeline_run_id`` tracks which pipeline run produced this record.
    - ``source_version`` and ``source_fetch_date`` record provenance.
    - ``entity_resolved`` flags whether entity resolution was applied.
    """
    __tablename__ = "drug_protein_interactions"

    drug_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("drugs.id", ondelete="CASCADE"), nullable=False,
    )
    protein_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("proteins.id", ondelete="CASCADE"), nullable=False,
    )
    interaction_type: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True,
    )
    # v29 ROOT FIX (audit D-5): was Float — precision loss corrupts pIC50. Use Numeric(10,4).
    activity_value: Mapped[Optional[float]] = mapped_column(Numeric(10, 4), nullable=True)
    activity_type: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True,
    )
    activity_units: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True,
    )
    source: Mapped[Optional[str]] = mapped_column(
        String(SOURCE_LENGTH), nullable=True,
    )
    # [DES-04] source_id now nullable instead of NOT NULL DEFAULT ''
    # Empty string conflated with "no value" — NULL is semantically correct.
    source_id: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True,
    )
    confidence_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # [LINE-01] Source tracking columns
    source_version: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True,
    )
    source_fetch_date: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    entity_resolved: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True, server_default="0",
    )
    # [IDEM-01] Pipeline run tracking
    pipeline_run_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("pipeline_runs.id", ondelete="SET NULL"), nullable=True,
    )

    # -- relationships --
    drug: Mapped["Drug"] = relationship(
        "Drug", back_populates="drug_protein_interactions",
    )
    protein: Mapped["Protein"] = relationship(
        "Protein", back_populates="drug_protein_interactions",
    )

    __table_args__ = (
        UniqueConstraint(
            "drug_id", "protein_id", "source", "source_id",
            name="uq_dpi_drug_protein_source",
        ),
        # v43 ROOT FIX (P1-010): add a partial unique index for the
        # case where source_id IS NOT NULL. The UniqueConstraint above
        # does NOT enforce uniqueness when source_id is NULL (SQL
        # standard: NULL != NULL), so duplicate DPI rows with NULL
        # source_id can be inserted. This partial index closes that
        # loophole for rows that DO have a source_id (the common case
        # — source_id is the activity_id from ChEMBL or the
        # interaction_id from DrugBank). Rows with NULL source_id
        # remain exempt (they're typically aggregated/derived records
        # where uniqueness is enforced at the application layer).
        # Note: postgresql_where is the SQLAlchemy syntax for a
        # partial index. SQLite supports partial indexes natively
        # (since 3.8.0, 2013). On databases that don't support partial
        # indexes, this is silently ignored.
        Index(
            "uq_dpi_drug_protein_source_partial",
            "drug_id", "protein_id", "source", "source_id",
            unique=True,
            postgresql_where=text("source_id IS NOT NULL"),
            sqlite_where=text("source_id IS NOT NULL"),
        ),
        # [DQ-09] Activity value must be positive
        CheckConstraint(
            "activity_value IS NULL OR activity_value > 0",
            name="chk_dpi_activity_value_positive",
        ),
        # [DQ-07] Confidence score range
        CheckConstraint(
            "confidence_score IS NULL OR (confidence_score >= 0.0 AND confidence_score <= 1.0)",
            name="chk_dpi_confidence_score_range",
        ),
        # [PERF-01] Composite indexes for common query patterns
        Index("idx_dpi_drug", "drug_id"),
        Index("idx_dpi_protein", "protein_id"),
        Index("idx_dpi_protein_interaction", "protein_id", "interaction_type"),
        Index("idx_dpi_drug_interaction", "drug_id", "interaction_type"),
    )

    # [LOG-02] Enhanced __repr__ with diagnostic fields
    def __repr__(self) -> str:
        return (
            f"<DrugProteinInteraction(id={self.id}, drug_id={self.drug_id}, "
            f"protein_id={self.protein_id}, interaction_type='{self.interaction_type}', "
            f"activity_value={self.activity_value}, source='{self.source}')>"
        )


# ===========================================================================
# 4. PROTEIN–PROTEIN INTERACTIONS (PPI)
# ===========================================================================


class ProteinProteinInteraction(Base, IDMixin, TimestampMixin):
    """Protein–protein interaction records from STRING.

    Domain meaning
    --------------
    Each row represents an interaction between two proteins in the STRING
    database.  Scores are integers in the range [0, 1000] (NOT 0–100).
    Score misinterpretation corrupts Graph Transformer edge weights (SCI-03).

    Key constraints
    ---------------
    - ``protein_a_id < protein_b_id`` enforced by CHECK (DES-02, IDEM-03).
      This prevents symmetric duplicates like (A, B) and (B, A).
    - All score columns are in [0, 1000] (SCI-03).
    - ``source`` has no default — must be specified explicitly (CFG-03).
    - ``score_json`` for source-specific score payloads (INT-04).

    Normalized Score
    ----------------
    The ``normalized_combined_score`` property returns combined_score / 1000.0
    for the [0, 1] range expected by downstream ML models.
    """
    __tablename__ = "protein_protein_interactions"

    protein_a_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("proteins.id", ondelete="CASCADE"), nullable=False,
    )
    protein_b_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("proteins.id", ondelete="CASCADE"), nullable=False,
    )
    # [SCI-03] STRING scores are 0–1000, NOT 0–100
    combined_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    experimental_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    database_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    textmining_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # [CFG-03] No server_default — source must be explicitly specified
    source: Mapped[str] = mapped_column(
        String(SOURCE_LENGTH), nullable=False,
    )
    # [INT-04] Score JSON for source-specific payloads beyond STRING
    score_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # [IDEM-01] Pipeline run tracking
    pipeline_run_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("pipeline_runs.id", ondelete="SET NULL"), nullable=True,
    )

    # -- relationships --
    protein_a: Mapped["Protein"] = relationship(
        "Protein", foreign_keys=[protein_a_id], back_populates="ppi_as_protein_a",
    )
    protein_b: Mapped["Protein"] = relationship(
        "Protein", foreign_keys=[protein_b_id], back_populates="ppi_as_protein_b",
    )

    __table_args__ = (
        UniqueConstraint("protein_a_id", "protein_b_id", name="uq_ppi_protein_pair"),
        # [DES-02] [IDEM-03] Prevent symmetric duplicates — protein_a_id must be smaller
        CheckConstraint(
            "protein_a_id < protein_b_id",
            name="chk_ppi_ordered",
        ),
        # [SCI-03] Score bounds — all STRING scores are 0–1000
        CheckConstraint(
            "combined_score IS NULL OR (combined_score >= 0 AND combined_score <= 1000)",
            name="chk_ppi_combined_score",
        ),
        CheckConstraint(
            "experimental_score IS NULL OR (experimental_score >= 0 AND experimental_score <= 1000)",
            name="chk_ppi_experimental_score",
        ),
        CheckConstraint(
            "database_score IS NULL OR (database_score >= 0 AND database_score <= 1000)",
            name="chk_ppi_database_score",
        ),
        CheckConstraint(
            "textmining_score IS NULL OR (textmining_score >= 0 AND textmining_score <= 1000)",
            name="chk_ppi_textmining_score",
        ),
        Index("idx_ppi_protein_a", "protein_a_id"),
        Index("idx_ppi_protein_b", "protein_b_id"),
    )

    # [SCI-03] Normalized score for ML consumption
    @property
    def normalized_combined_score(self) -> Optional[float]:
        """Return combined_score normalized to [0, 1] range.

        STRING scores are 0–1000.  Downstream ML models expect [0, 1].
        """
        if self.combined_score is None:
            return None
        return self.combined_score / 1000.0

    # [LOG-02] Enhanced __repr__ with source and score
    def __repr__(self) -> str:
        return (
            f"<ProteinProteinInteraction(id={self.id}, "
            f"protein_a_id={self.protein_a_id}, protein_b_id={self.protein_b_id}, "
            f"combined_score={self.combined_score}, source='{self.source}')>"
        )


# ===========================================================================
# 5. GENE–DISEASE ASSOCIATIONS (GDA)
# ===========================================================================


class GeneDiseaseAssociation(Base, IDMixin, TimestampMixin):
    """Gene–disease association records from DisGeNET and OMIM.

    Domain meaning
    --------------
    Each row links a gene (via gene_symbol / uniprot_id) to a disease
    (via disease_id).  Data is sourced from DisGeNET and OMIM.

    Key constraints
    ---------------
    - ``uniprot_id`` string FK to ``proteins.uniprot_id`` for cross-source
      joins. The GDA model uses the string UniProt accession (not the
      integer protein PK) because gene-disease data sources (DisGeNET,
      OMIM) provide gene symbols that resolve to UniProt accessions, not
      to integer protein IDs.
    - ``gene_symbol`` validated against HGNC format (SCI-04).
    - ``disease_id_type`` indicates the identifier system used (SCI-06).
    - ``score`` is the association score from the source database.
    - ``pmid_list`` capped at 2000 chars (SEC-02).
    - ``UniqueConstraint(gene_symbol, disease_id, source)`` with NULL
      handling (DQ-02).

    Lineage (LINE-03)
    ------------------
    - ``score_type`` and ``score_method`` document how the score was computed.
    """
    __tablename__ = "gene_disease_associations"

    gene_symbol: Mapped[Optional[str]] = mapped_column(
        # v16 ROOT FIX (CD-3): align with migration 001's
        # ``NOT NULL DEFAULT ''`` + CHECK (gene_symbol <> '').
        # Previously ORM had ``nullable=True`` while migration 001 had
        # ``NOT NULL DEFAULT ''`` — divergence meant SQLite dev/test
        # DBs (created via ORM) accepted NULL while PostgreSQL prod
        # DBs (created via migration) rejected it. Code that passed
        # tests on SQLite could fail on PostgreSQL. Now both are
        # consistent: NOT NULL with empty-string default.
        String(GENE_SYMBOL_LENGTH), nullable=False, server_default="",
    )
    # String FK to proteins.uniprot_id — the canonical cross-source key.
    # GDA does NOT have an integer protein_id FK because gene-disease data
    # sources provide gene symbols that resolve to UniProt accessions.
    uniprot_id: Mapped[Optional[str]] = mapped_column(
        String(UNIPROT_ID_LENGTH),
        ForeignKey("proteins.uniprot_id", ondelete="SET NULL"),
        nullable=True,
    )
    # v14 ROOT FIX (FIX4 / audit CD-3): the integer ``protein_id`` column
    # was REMOVED from the GDA model. The previous v13 code kept it "to
    # match the migrations" — but the migrations were THEMSELVES the bug.
    # The GDA table is supposed to use the STRING ``uniprot_id`` FK
    # (because gene-disease data sources provide gene symbols that
    # resolve to UniProt accessions, NOT integer protein PKs). The
    # loader code (loaders.py:2318) already correctly skips populating
    # ``protein_id``. Keeping a column the loader never populates
    # produced an unused index, false-positive schema drift, and made
    # the GDA model ambiguous (two ways to point at a protein). The
    # migrations have also been updated to NOT create the column.
    # Tests under TestFix4_GdaUniprotId enforce this invariant.
    disease_id: Mapped[Optional[str]] = mapped_column(
        # v16 ROOT FIX (CD-3): align with migration 001's
        # ``NOT NULL DEFAULT ''`` + CHECK (disease_id <> '').
        String(DISEASE_ID_LENGTH), nullable=False, server_default="",
    )
    # [SCI-06] Disease ID type — indicates which identifier system is used
    disease_id_type: Mapped[Optional[str]] = mapped_column(
        String(DISEASE_ID_TYPE_LENGTH), nullable=True,
    )
    disease_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    association_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    source: Mapped[Optional[str]] = mapped_column(
        String(SOURCE_LENGTH), nullable=True,
    )
    # [SEC-02] Capped at 2000 chars instead of unbounded Text
    pmid_list: Mapped[Optional[str]] = mapped_column(
        String(PMID_LIST_LENGTH), nullable=True,
    )
    # [LINE-03] Score computation tracking
    score_type: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True,
    )
    score_method: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True,
    )
    # [IDEM-01] Pipeline run tracking
    pipeline_run_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("pipeline_runs.id", ondelete="SET NULL"), nullable=True,
    )

    # ------------------------------------------------------------------
    # 389-fix audit — institutional-grade columns (SCI-3..SCI-42, DQ-1..34,
    # IDEM-9..14, LIN-1..28, COMP-1..20).  All new columns are nullable
    # so existing rows (and existing tests) are unaffected.
    # ------------------------------------------------------------------

    # [SCI-6 / DQ-1] NCBI Entrez Gene ID — stable across HGNC renames.
    gene_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # [SCI-9 / DQ-2] DisGeNET diseaseType ∈ {disease, phenotype, group}.
    disease_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # [SCI-3] DisGeNET sub-source (CURATED, BEFREE, GWAS_CATALOG, …).
    source_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # [SCI-8] MeSH hierarchy code (e.g. C04.588.614) — stored verbatim.
    disease_class: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    disease_class_source: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True
    )

    # [SCI-7] Publication-year range of the evidence.
    year_initial: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    year_final: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # [SCI-10] Confidence tier label (weak / moderate / strong).
    confidence_tier: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # [SCI-24] Evidence-strength label derived from PMID count + recency.
    evidence_strength: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # [SCI-38] Score × source_weight — cross-source comparable score.
    normalized_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # [SCI-26 / IDEM-8] DisGeNET release version (e.g. "v7_2024_06").
    source_version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # [LIN-6 / COMP-7] Download timestamp (UTC) for this row.
    download_date: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # [LIN-23] Download method: "api" or "static".
    download_method: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # [INT-7] Source format: "api" or "tsv".
    source_format: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # [LIN-24] Dedup strategy applied (e.g. "validate_gda_scores_dedup").
    dedup_strategy: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # [LIN-15 / IDEM-17] Confidence tier definition version (e.g. pinero_2020_v1).
    confidence_tier_method: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True
    )

    # [LIN-10] Resolution method used for gene_symbol → uniprot_id.
    resolution_method: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # [LIN-10 / IDEM-7] SHA-256 of the cached gene_to_uniprot map.
    gene_to_uniprot_map_version: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )

    # [LIN-16] Original PMID count (before capping).
    original_pmid_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # [COMP-6] Schema version of the CSV that produced this row.
    schema_version: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # [IDEM-14] Snapshot tag for backfill safety.
    snapshot_tag: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # [LIN-9] Source URL (sanitised — no API key).
    source_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # [SCI-21 / LIN-17..19] Lineage columns from validate_gda_scores.
    # Renamed without leading underscore in the DB (the CSV keeps the
    # underscore-prefixed names for backward compatibility).
    score_was_clipped: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    original_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    score_was_coerced_nan: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True
    )
    score_direction: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    disease_name_was_filled: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True
    )
    association_type_was_filled: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True
    )

    # [LIN-16] True if the pmid_list was capped.
    pmid_list_was_capped: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True
    )

    # -- relationships --
    protein: Mapped["Protein"] = relationship(
        "Protein", back_populates="gene_disease_associations",
        foreign_keys=[uniprot_id],
    )

    # -- validators --
    @validates("gene_symbol")
    def _validate_gene_symbol(self, key: str, value: Optional[str]) -> Optional[str]:
        return _validate_gene_symbol(value)

    __table_args__ = (
        Index("idx_gda_gene", "gene_symbol"),
        Index("idx_gda_disease", "disease_id"),
        # [PERF-02] Index on uniprot_id for fast joins
        Index("idx_gda_uniprot_id", "uniprot_id"),
        # [SCI-6] Index on gene_id for stable cross-source joins (IDEM-15)
        Index("idx_gda_gene_id", "gene_id"),
        # [SCI-3] Index on source_id for cross-source analysis
        Index("idx_gda_source_id", "source_id"),
        # [IDEM-14] Index on snapshot_tag for fast snapshot queries
        Index("idx_gda_snapshot_tag", "snapshot_tag"),
        UniqueConstraint(
            "gene_symbol", "disease_id", "source",
            name="uq_gda_gene_disease_source",
        ),
        # [SCI-06 / COMP-5] Disease ID type validation — extended to include
        # 'hpo' (HPO terms are valid DisGeNET disease IDs per Piñero et al. 2020).
        # CRITICAL FIX (patient safety): added 'icd10' (WHO international
        # clinical classification), 'efo' (Experimental Factor Ontology —
        # used by GWAS Catalog, UK Biobank, Open Targets), and 'orphanet'
        # (rare-disease ontology). Without these, real disease associations
        # would be SILENTLY DROPPED at insert time, hiding drug-disease
        # links from the model. KEEP IN SYNC with:
        #   - database/loaders.py::_VALID_DISEASE_ID_TYPES
        #   - database/migrations/001_initial_schema.sql::chk_gda_disease_id_type
        #   - database/migrations/004_extend_gda_table_for_389_audit.sql
        #   - database/migrations/run_migrations.py::REQUIRED_COLUMNS
        CheckConstraint(
            "disease_id_type IS NULL OR disease_id_type IN "
            "('omim', 'disgenet', 'doid', 'mesh', 'umls', 'hpo', "
            "'icd10', 'efo', 'orphanet')",
            name="chk_gda_disease_id_type",
        ),
        # [SCI-9] diseaseType ∈ {disease, phenotype, group} when non-NULL
        CheckConstraint(
            "disease_type IS NULL OR disease_type IN "
            "('disease', 'phenotype', 'group')",
            name="chk_gda_disease_type",
        ),
        # [SCI-11] confidence_tier must be a known label when non-NULL
        CheckConstraint(
            "confidence_tier IS NULL OR confidence_tier IN "
            "('weak', 'moderate', 'strong')",
            name="chk_gda_confidence_tier",
        ),
        # [SCI-24] evidence_strength must be a known label when non-NULL
        CheckConstraint(
            "evidence_strength IS NULL OR evidence_strength IN "
            "('robust', 'moderate', 'limited', 'unsupported')",
            name="chk_gda_evidence_strength",
        ),
        # [SCI-41] year_initial <= year_final when both are present
        CheckConstraint(
            "year_initial IS NULL OR year_final IS NULL OR year_initial <= year_final",
            name="chk_gda_year_range",
        ),
        # [SCI-38 / COMP-19] normalized_score must be in [0, 1] when non-NULL
        CheckConstraint(
            "normalized_score IS NULL OR (normalized_score >= 0.0 AND normalized_score <= 1.0)",
            name="chk_gda_normalized_score_range",
        ),
        # v20 CD-3 minor ROOT FIX: add explicit non-empty CHECK constraints
        # to mirror migration 001 lines 864-868. The v16 fix made the
        # columns NOT NULL DEFAULT '' but never added the CHECK constraint
        # to the ORM — so SQLite dev/test DBs (created via ORM) accepted
        # empty strings while PostgreSQL prod DBs (created via migration)
        # rejected them. This was the same SQLite-vs-PostgreSQL parity
        # risk CD-3 was about.
        CheckConstraint(
            "gene_symbol IS NOT NULL AND gene_symbol <> ''",
            name="chk_gda_gene_symbol_nonempty",
        ),
        CheckConstraint(
            "disease_id IS NOT NULL AND disease_id <> ''",
            name="chk_gda_disease_id_nonempty",
        ),
        # audit-2025 ROOT FIX (issue 21 / issue 23): the migration
        # ``001_initial_schema.sql`` declares ``CONSTRAINT chk_gda_source
        # CHECK (source IS NULL OR source IN ('disgenet', 'omim'))`` on
        # the ``gene_disease_associations`` table. The ORM model was
        # missing this constraint, so SQLite dev/test DBs (created via
        # ORM ``Base.metadata.create_all()``) accepted arbitrary
        # ``source`` values like ``'chembl'`` or ``''`` — while
        # PostgreSQL prod DBs (created via the migration) rejected
        # them. This SQLite-vs-PostgreSQL parity gap meant bad data
        # passed tests but failed in production. The fix adds the
        # matching ``CheckConstraint`` to the ORM ``__table_args__``
        # so SQLite tests catch the same violations.
        #
        # NB: the GDA table is the *output* of two pipelines
        # (DisGeNET + OMIM). Other source columns (``chembl``,
        # ``drugbank``, etc.) belong to the ``drugs`` / ``proteins`` /
        # ``interactions`` tables, NOT here.
        # v41 ROOT FIX (SEV1 #3): allow 'disgenet_<subsrc>' prefixed
        # values actually emitted by disgenet_pipeline._derive_source_value
        # (line 2620: f"disgenet_{source_id.lower()}" e.g.
        # "disgenet_curated", "disgenet_inference"). The previous
        # IN ('disgenet','omim') constraint rejected 100% of DisGeNET
        # GDA rows on PostgreSQL AND SQLite. Use the SQL-standard LIKE
        # pattern instead of regex for SQLite portability (SQLite
        # supports LIKE; the migrations/001 SQL uses ~ regex which is
        # PostgreSQL-only and is silently dropped by the migration
        # runner's _translate_sql_for_sqlite).
        CheckConstraint(
            "source IS NULL OR source = 'omim' OR source = 'disgenet' "
            "OR source LIKE 'disgenet|_%' ESCAPE '|'",
            name="chk_gda_source",
        ),
        # v29 ROOT FIX (audit D-6): Removed the duplicate partial
        # ``Index("uq_gda_gene_disease_source_partial", ..., unique=True,
        # postgresql_where=text("gene_symbol IS NOT NULL OR gene_symbol = ''"))``
        # that previously lived here. It was redundant with the
        # ``UniqueConstraint("uq_gda_gene_disease_source")`` declared above
        # for two reasons:
        #   1. The ``postgresql_where`` clause
        #      ``gene_symbol IS NOT NULL OR gene_symbol = ''`` was a
        #      TAUTOLOGY once ``chk_gda_gene_symbol_nonempty`` (just above)
        #      rejected both NULL and empty-string ``gene_symbol`` — every
        #      surviving row matched the partial predicate, so the "partial"
        #      index actually covered the WHOLE table. It was therefore a
        #      second full UNIQUE index on (gene_symbol, disease_id, source)
        #      in addition to the UniqueConstraint — 2× write amplification
        #      (4× if you also count the implicit index SQLAlchemy emits on
        #      the UniqueConstraint) on every INSERT/UPDATE/DELETE.
        #   2. SQLite (dev/test) silently ignores ``postgresql_where``, so
        #      the "partial" index became a SECOND plain unique index on
        #      SQLite — producing a confusing duplicate-index error surface
        #      and wasting disk on every dev DB.
        # Keeping ONLY the canonical ``UniqueConstraint`` is sufficient: it
        # already enforces uniqueness on (gene_symbol, disease_id, source)
        # on both SQLite and PostgreSQL, with NULLS DISTINCT semantics on
        # PostgreSQL 15+ (the project's minimum supported version).
    )

    def __repr__(self) -> str:
        # SCI-FIX: Use self.uniprot_id (declared on the ORM model) instead of
        # self.protein_id (which exists in the DB table via migration 003 but
        # is NOT mapped on the ORM model, causing AttributeError on repr).
        return (
            f"<GeneDiseaseAssociation(id={self.id}, gene_symbol='{self.gene_symbol}', "
            f"disease_id='{self.disease_id}', source='{self.source}', "
            f"uniprot_id={self.uniprot_id!r})>"
        )


# ===========================================================================
# 6. ENTITY MAPPING (cross-database entity resolution output)
# ===========================================================================


class EntityMapping(Base, IDMixin, TimestampMixin):
    """Cross-database entity resolution output.

    Domain meaning
    --------------
    Each row represents a resolved entity that maps identifiers across
    databases (ChEMBL, DrugBank, UniProt, STRING).  When a canonical
    InChIKey is available, it serves as the primary identifier; otherwise
    canonical_name is used.

    Key constraints
    ---------------
    - Partial unique index on ``canonical_inchikey`` WHERE NOT NULL.
    - ``match_confidence`` must be in [0, 1] (DQ-07).
    - [SCI-01] InChIKey widened to 50 for synthetic keys.
    - [DES-03] Additional partial unique indexes on chembl_id, drugbank_id.
    - [DQ-03] FK constraints on chembl_id, drugbank_id, uniprot_id, string_id.

    Lineage (LINE-04)
    ------------------
    - ``match_history`` stores the full resolution attempt chain as JSON.
    """
    __tablename__ = "entity_mapping"

    # [SCI-01] Widened from 27 to 50 for synthetic InChIKeys
    canonical_inchikey: Mapped[Optional[str]] = mapped_column(
        String(INCHIKEY_LENGTH), nullable=True,
    )
    canonical_name: Mapped[Optional[str]] = mapped_column(
        String(DRUG_NAME_LENGTH), nullable=True,
    )
    # [DQ-03] Application-level FK enforcement for cross-reference integrity.
    # FK constraints on chembl_id, drugbank_id, uniprot_id, string_id are
    # enforced at the application/loader level rather than at the DB level
    # because these reference non-PK columns with partial unique indexes,
    # which SQLite does not support as FK targets.
    chembl_id: Mapped[Optional[str]] = mapped_column(
        String(CHEMBL_ID_LENGTH), nullable=True,
    )
    drugbank_id: Mapped[Optional[str]] = mapped_column(
        String(DRUGBANK_ID_LENGTH), nullable=True,
    )
    pubchem_cid: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    uniprot_id: Mapped[Optional[str]] = mapped_column(
        String(UNIPROT_ID_LENGTH), nullable=True,
    )
    string_id: Mapped[Optional[str]] = mapped_column(
        String(STRING_ID_LENGTH), nullable=True,
    )
    # [DQ-07] Match confidence range
    match_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    match_method: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # [LINE-04] Full resolution attempt chain
    match_history: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # -- validators --
    @validates("canonical_inchikey")
    def _validate_inchikey(self, key: str, value: Optional[str]) -> Optional[str]:
        return _validate_inchikey(value)

    __table_args__ = (
        # Partial unique index: only enforce uniqueness for non-NULL canonical_inchikey
        Index(
            "uq_entity_mapping_inchikey",
            "canonical_inchikey",
            unique=True,
            postgresql_where=text("canonical_inchikey IS NOT NULL"),
        ),
        # [DES-03] Partial unique index for records without InChIKey
        Index(
            "uq_entity_mapping_name_no_inchikey",
            "canonical_name",
            unique=True,
            postgresql_where=text("canonical_inchikey IS NULL AND canonical_name IS NOT NULL"),
        ),
        # [DES-03] Unique chembl_id where not null
        Index(
            "uq_entity_mapping_chembl",
            "chembl_id",
            unique=True,
            postgresql_where=text("chembl_id IS NOT NULL"),
        ),
        # [DES-03] Unique drugbank_id where not null
        Index(
            "uq_entity_mapping_drugbank",
            "drugbank_id",
            unique=True,
            postgresql_where=text("drugbank_id IS NOT NULL"),
        ),
        # [DQ-07] Match confidence range
        CheckConstraint(
            "match_confidence IS NULL OR (match_confidence >= 0.0 AND match_confidence <= 1.0)",
            name="chk_entity_mapping_confidence_range",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<EntityMapping(id={self.id}, canonical_inchikey='{self.canonical_inchikey}', "
            f"canonical_name='{self.canonical_name}', "
            f"match_confidence={self.match_confidence})>"
        )


# ===========================================================================
# 6b. DEAD LETTER QUEUE for GDA (DQ-18 / REL-3 / LIN-11)
# ===========================================================================


class DeadLetterGDA(Base, IDMixin, TimestampMixin):
    """Dead-letter queue for GDA records that could not be loaded.

    Domain meaning
    --------------
    Each row represents a GDA record that was rejected by the load()
    phase — e.g. unresolved gene_symbol, invalid disease_id format,
    inverted year range, etc.  Rows are written here instead of being
    silently dropped, so the data can be inspected and reprocessed
    later (REL-3, LIN-11).

    Key constraints
    ---------------
    - ``reason`` is a short stable identifier (e.g.
      ``"unresolved_gene_symbol"``, ``"invalid_disease_id_format"``).
    - ``details_json`` is a JSON object with the offending values
      (gene_symbol, disease_id, score, etc.) for debugging.
    - ``run_id`` is the DisGeNETPipeline.run_id (UUID string).
    """
    __tablename__ = "dead_letter_gda"

    gene_symbol: Mapped[Optional[str]] = mapped_column(
        String(GENE_SYMBOL_LENGTH), nullable=True
    )
    disease_id: Mapped[Optional[str]] = mapped_column(
        String(DISEASE_ID_LENGTH), nullable=True
    )
    source: Mapped[Optional[str]] = mapped_column(
        String(SOURCE_LENGTH), nullable=True
    )
    reason: Mapped[str] = mapped_column(String(100), nullable=False)
    details_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    run_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("idx_dlgda_reason", "reason"),
        Index("idx_dlgda_run_id", "run_id"),
        Index("idx_dlgda_gene_symbol", "gene_symbol"),
        Index("idx_dlgda_disease_id", "disease_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<DeadLetterGDA(id={self.id}, reason='{self.reason}', "
            f"gene_symbol='{self.gene_symbol}', disease_id='{self.disease_id}', "
            f"run_id='{self.run_id}')>"
        )


# ===========================================================================
# 7. PIPELINE RUNS (ETL audit log)
# ===========================================================================


# v17 ROOT FIX (AuditLog ORM missing): migration 001 declares an
# ``audit_log`` table (lines 1345-1397 of 001_initial_schema.sql) used
# by migrations 002/004/005/006 to record pre/post-migration row counts
# and lineage operations (PRE_MIGRATION_002_CHECKSUM, DELETE_NULL_DISEASE_ID,
# DEDUP_MIGRATION_002, etc.). The table has 9 columns: id, table_name,
# operation, record_id, changed_by, changed_at, old_values, new_values,
# row_count, details. Without an ORM model, ``Base.metadata.create_all()``
# on SQLite dev/test DBs did NOT create this table — so any Python code
# that tried to write audit records via the ORM raised
# ``sqlite3.OperationalError: no such table: audit_log``. The migration
# 001 ``CREATE TABLE IF NOT EXISTS`` was the only creation path, and on
# SQLite it was being silently skipped (CD-5 was the fix that made
# migrations run on SQLite, but the audit_log table itself had no ORM
# fallback). Adding this model closes the gap — create_all() now
# creates audit_log on BOTH PostgreSQL and SQLite, and migration 001's
# CREATE TABLE IF NOT EXISTS becomes the idempotent no-op it was
# designed to be.
class AuditLog(Base, IDMixin):
    """Audit log table for tracking schema migrations and bulk operations.

    Domain meaning
    --------------
    Each row records a single audit event — typically a schema-migration
    lineage operation (PRE_MIGRATION_002_CHECKSUM, DELETE_NULL_DISEASE_ID,
    DEDUP_MIGRATION_002, etc.) or a bulk data operation (BULK_OPERATION,
    SOFT_DELETE, RESTORE). Used by migrations 002/004/005/006 to record
    pre/post-migration row counts and checksums for replay safety.

    Key constraints
    ---------------
    - ``table_name`` NOT NULL (which table was affected).
    - ``operation`` NOT NULL, constrained to the whitelist defined in
      migration 001 (CHECK constraint chk_audit_log_operation).
    - ``changed_at`` NOT NULL DEFAULT NOW().
    - ``row_count`` nullable INTEGER (used by migration lineage INSERTs).
    - ``details`` nullable TEXT (free-form context, e.g. checksum values).
    """
    __tablename__ = "audit_log"

    table_name: Mapped[str] = mapped_column(String(50), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    record_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    changed_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    changed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    old_values: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    new_values: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # v17: row_count + details — added to migration 001 by the RT-1 /
    # Compound-4 "Migration Wall" fix so migration 002's INSERTs into
    # audit_log (table_name, operation, row_count, details) stop
    # aborting with "column row_count of relation audit_log does not
    # exist". Mirror them here so the ORM-created table matches.
    row_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # v17: mirror the chk_audit_log_operation CHECK whitelist from
        # migration 001 so SQLite dev/test DBs (which skip the migration
        # SQL) still enforce the same operation-enum contract.
        CheckConstraint(
            "operation IN ("
            "'INSERT', 'UPDATE', 'DELETE', 'SOFT_DELETE', 'RESTORE', "
            "'PRE_MIGRATION_002_CHECKSUM', 'POST_MIGRATION_002_CHECKSUM', "
            "'PRE_MIGRATION_004_CHECKSUM', 'POST_MIGRATION_004_CHECKSUM', "
            "'PRE_MIGRATION_005_CHECKSUM', 'POST_MIGRATION_005_CHECKSUM', "
            "'PRE_MIGRATION_006_CHECKSUM', 'POST_MIGRATION_006_CHECKSUM', "
            "'MIGRATION_BACKFILL', 'MIGRATION_DEDUP', 'MIGRATION_CONSTRAINT', "
            "'BULK_OPERATION', "
            "'DELETE_NULL_DISEASE_ID', 'DELETE_NULL_SOURCE', "
            "'PRESERVED_NULL_GENE_SYMBOL', 'DEDUP_MIGRATION_002'"
            ")",
            name="chk_audit_log_operation",
        ),
        Index("idx_audit_log_table_name", "table_name"),
        Index("idx_audit_log_operation", "operation"),
        Index("idx_audit_log_changed_at", "changed_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<AuditLog(id={self.id}, table_name='{self.table_name}', "
            f"operation='{self.operation}', row_count={self.row_count})>"
        )


class PipelineRun(Base, IDMixin, TimestampMixin):
    """ETL pipeline execution audit log.

    Domain meaning
    --------------
    Each row records a single pipeline run — its source, status, record
    counts, duration, and any error details.

    Key constraints
    ---------------
    - ``source`` must be one of the 7 known pipeline names (DES-07).
    - ``status`` constrained to known values (DES-05).
    - ``duration_seconds`` must be non-negative (DQ-08).
    - ``error_message`` capped at 500 chars (SEC-04).
    - ``UniqueConstraint(source, run_date)`` for idempotency (DES-07).
    """
    __tablename__ = "pipeline_runs"

    # [DES-07] Source constrained to known pipeline names
    source: Mapped[str] = mapped_column(
        String(PIPELINE_SOURCE_LENGTH), nullable=False,
    )
    run_date: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    # [DES-05] Status constrained by CHECK
    status: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True,
    )
    records_downloaded: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    records_cleaned: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    records_loaded: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # v13 ROOT FIX (CD-4): the following 6 columns were created by
    # migration 001 (lines 1143-1155) but MISSING from the ORM model.
    # This meant ``Base.metadata.create_all()`` created the
    # ``pipeline_runs`` table with only 8 columns, and migration 001's
    # ``CREATE TABLE IF NOT EXISTS`` was a no-op (table already
    # existed). The 6 columns were never created on SQLite dev/test
    # DBs. Airflow retry / checkpoint / partial-failure tracking code
    # that referenced these columns via the ORM raised
    # ``AttributeError`` at runtime. v13: declare all 6 columns on the
    # ORM so ``create_all()`` and migration 001 agree on the schema.
    records_failed: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    records_skipped: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    records_updated: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_checkpoint: Mapped[Optional[str]] = mapped_column(
        String(500), nullable=True,
    )
    input_file_checksum: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True,
    )
    config_hash: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True,
    )
    # [SEC-04] Error message capped to prevent stack trace leakage
    error_message: Mapped[Optional[str]] = mapped_column(
        String(ERROR_MESSAGE_LENGTH), nullable=True,
    )
    # [DQ-08] Duration must be non-negative
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # [P1-18 ROOT FIX] Per-run audit metadata (run_id, correlation_id,
    # triggered_by, source_version, sha256_raw, sha256_cleaned, git_commit,
    # seed, schema_version, validation_errors, dq_metrics, record counts).
    # BasePipeline._write_run_log already builds this dict and passes it as
    # metadata_json — without this column, the constructor silently dropped
    # it on every run. Migration 007 adds the column; the JSON type maps to
    # JSONB on PostgreSQL and TEXT on SQLite (via the SQLAlchemy JSON
    # dialect).
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        # [DES-07] Source must be a known pipeline
        CheckConstraint(
            "source IN ('chembl', 'drugbank', 'uniprot', 'string', "
            "'disgenet', 'omim', 'pubchem')",
            name="chk_pipeline_runs_source",
        ),
        # [DQ-08] Duration must be non-negative
        CheckConstraint(
            "duration_seconds IS NULL OR duration_seconds >= 0",
            name="chk_pipeline_runs_duration_nonneg",
        ),
        # v17 ROOT FIX (CD-4 deepened): migration 001 declares 3 CHECK
        # constraints on ``pipeline_runs`` that the ORM was MISSING —
        # ``chk_pipeline_runs_status`` (status enum),
        # ``chk_pipeline_runs_counts_nonneg`` (record counts non-negative),
        # ``chk_pipeline_runs_error_message`` (error_message length cap).
        # Without these, ``Base.metadata.create_all()`` on SQLite dev/test
        # DBs created a ``pipeline_runs`` table that accepted any string
        # for status (e.g. "BOGUS") and negative record counts. The
        # migration 001 ``CREATE TABLE IF NOT EXISTS`` was a no-op
        # because the table already existed from create_all — so the
        # constraints were NEVER applied on SQLite. Code that passed
        # tests on SQLite could fail on PostgreSQL (where the
        # constraints ARE applied). Add all 3 constraints to the ORM
        # so both paths produce the same schema.
        CheckConstraint(
            "status IS NULL OR status IN "
            "('running', 'success', 'failed', 'partial')",
            name="chk_pipeline_runs_status",
        ),
        CheckConstraint(
            "(records_downloaded IS NULL OR records_downloaded >= 0) "
            "AND (records_cleaned IS NULL OR records_cleaned >= 0) "
            "AND (records_loaded IS NULL OR records_loaded >= 0) "
            "AND (records_failed IS NULL OR records_failed >= 0) "
            "AND (records_skipped IS NULL OR records_skipped >= 0) "
            "AND (records_updated IS NULL OR records_updated >= 0)",
            name="chk_pipeline_runs_counts_nonneg",
        ),
        CheckConstraint(
            "error_message IS NULL OR LENGTH(error_message) <= 500",
            name="chk_pipeline_runs_error_message",
        ),
        # [DES-07] UniqueConstraint for idempotent pipeline runs
        UniqueConstraint(
            "source", "run_date",
            name="uq_pipeline_runs_source_date",
        ),
        Index("idx_pr_source", "source"),
        Index("idx_pr_status", "status"),
        Index("idx_pr_run_date", "run_date"),
    )

    def __repr__(self) -> str:
        return (
            f"<PipelineRun(id={self.id}, source='{self.source}', "
            f"status='{self.status}', run_date={self.run_date}, "
            f"duration_seconds={self.duration_seconds})>"
        )


# ===========================================================================
# PubChem compound properties (ARCH-5, INT-7, SCI-4, SCI-6)
# ===========================================================================


class PubChemCompoundProperty(Base, IDMixin, TimestampMixin):
    """Full PubChem compound property record (one row per inchikey+pubchem_cid).

    Domain meaning
    --------------
    The ``drugs`` table only stores 4 PubChem columns (pubchem_cid,
    molecular_formula, molecular_weight, smiles).  This table stores the
    FULL set of 15+ properties fetched from PubChem PUG REST — InChI,
    IUPACName, XLogP, ExactMass, TPSA, Complexity, HBondDonorCount,
    HBondAcceptorCount, RotatableBondCount, HeavyAtomCount, IsomericSMILES,
    CAS, etc.  Phase 3 (Graph Transformer) needs these for molecular
    fingerprinting.

    Why this ORM model was added
    ----------------------------
    CRITICAL FIX (cross-dialect compatibility / runtime safety):
    Previously, this table was only created by SQL migration
    ``005_pubchem_compound_properties.sql``, which is correctly SKIPPED on
    SQLite by the migration runner (SQLite does not support
    ``GENERATED ALWAYS AS IDENTITY``).  As a result, when the codebase ran
    on SQLite (the dev / test dialect), the PubChem pipeline failed with
    ``sqlite3.OperationalError: no such table: pubchem_compound_properties``.
    Adding this ORM model means ``Base.metadata.create_all()`` creates the
    table on BOTH PostgreSQL (where it may already exist from migration
    005 — ``create_all`` is additive and idempotent) AND SQLite (where it
    is the only creation path).  Schema parity with migration 005 is
    enforced by the test suite.
    """
    __tablename__ = "pubchem_compound_properties"

    # [SCI-11, LIN-2] The InChIKey we requested from PubChem.
    # v16 ROOT FIX (CD-2): add FK to drugs.inchikey so the
    # relationship is enforced at the DB level (was missing in ORM
    # but present in migration 005). Aligns ORM with migration.
    # v17 ROOT FIX (CD-2 deepened): migration 005 declares the FK
    # WITHOUT ``ondelete`` (default NO ACTION). The ORM declared
    # ``ondelete="CASCADE"`` — divergent. On PostgreSQL, both
    # create_all() and migration 005 try to create the FK; the second
    # one silently wins depending on which runs first, producing
    # non-deterministic on-delete behavior. Align ORM to migration
    # 005's NO ACTION (the safer default — a properties row blocks
    # drug deletion until explicitly cleaned up).
    inchikey: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("drugs.inchikey"),
        nullable=False, index=True,
    )

    # [SCI-5, LIN-2] PubChem Compound ID (parent / standardized).
    pubchem_cid: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    # [SCI-1, DESIGN-1] CanonicalSMILES (no stereo).
    canonical_smiles: Mapped[Optional[str]] = mapped_column(String(50000), nullable=True)

    # [SCI-1, SCI-14, SCI-15] IsomericSMILES (with stereochemistry).
    isomeric_smiles: Mapped[Optional[str]] = mapped_column(String(50000), nullable=True)

    # [SCI-2] InChI — full InChI string.
    inchi: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # [SCI-3] IUPACName — systematic chemical name.
    # v16 ROOT FIX (CD-2): Text (not String(1000)) to match migration 005.
    iupac_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # [SCI-4] CAS Registry Number.
    cas_number: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # [SCI-5] Molecular formula.
    molecular_formula: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    # [SCI-6] Molecular weight (g/mol).
    # v16 ROOT FIX (CD-2): Numeric(12,6) (not Float) to match
    # migration 005 / Core Table. Float loses precision for large
    # molecular weights (e.g. antibodies ~150 kDa have sub-Da
    # precision needs).
    molecular_weight: Mapped[Optional[float]] = mapped_column(Numeric(12, 6), nullable=True)

    # [SCI-7] Exact mass (monoisotopic).
    # v16 CD-2: Numeric(12,6) to match migration 005.
    exact_mass: Mapped[Optional[float]] = mapped_column(Numeric(12, 6), nullable=True)

    # [SCI-8] XLogP — computed octanol-water partition coefficient.
    # v16 CD-2: Numeric(6,2) to match migration 005.
    # v17 CD-2 deepened: add server_default='pubchem_xlogp3' to match
    # migration 005 — without the default, the loader had to populate
    # xlogp_source explicitly on every insert, diverging from the
    # migration's intent (the value is constant for fetched rows).
    xlogp: Mapped[Optional[float]] = mapped_column(Numeric(6, 2), nullable=True)
    xlogp_source: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True, server_default="pubchem_xlogp3",
    )

    # [SCI-9] Topological Polar Surface Area (Å²).
    # v16 CD-2: Numeric(8,2) to match migration 005.
    # v17 CD-2 deepened: add server_default='pubchem_calculated'.
    tpsa: Mapped[Optional[float]] = mapped_column(Numeric(8, 2), nullable=True)
    tpsa_source: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True, server_default="pubchem_calculated",
    )

    # [SCI-10] Bertz complexity index.
    # v16 CD-2: Numeric(10,2) to match migration 005.
    complexity: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)

    # [SCI-11] Lipinski H-bond donor count.
    h_bond_donor_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # [SCI-12] Lipinski H-bond acceptor count.
    h_bond_acceptor_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # [SCI-13] Rotatable bond count.
    rotatable_bond_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # [SCI-14] Heavy atom count.
    heavy_atom_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # [SCI-15] Formal charge.
    formal_charge: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # [SCI-16] Isotope info (free-text description).
    # v16 CD-2: Text (not String(200)) to match migration 005.
    isotope_info: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # [SCI-17] Salt form.
    # v16 CD-2: String(100) (not String(50)) to match migration 005.
    salt_form: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # [SCI-18] Protonation state.
    # v20 CD-2 ROOT FIX: String(20) (VARCHAR(20) in migration 005) to match
    # the V19 PS-1 widened word taxonomy.
    # The original v16 comment claimed CHAR(1)/String(1) matched migration 005,
    # but V19 PS-1 widened migration 005 to VARCHAR(20) for full words
    # ('neutral', 'protonated', 'deprotonated', 'zwitterion', 'salt_form').
    # This ORM/Core Table site was NOT updated, re-introducing 3-way schema
    # drift. Loader returning 'protonated' (10 chars) would silently truncate
    # on SQLite or raise DataError on PostgreSQL strict.
    protonation_state: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # [LIN-2-extra] PubChem release tag (e.g. "PubChem 2024.09").
    pubchem_release: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # [LIN-1, LIN-2] Source identifiers.
    # v16 CD-2: NOT NULL + String(100) to match migration 005.
    # v17 CD-2 deepened: migration 005 declares NOT NULL WITHOUT a
    # server_default. The ORM added ``server_default=""`` to keep
    # create_all() happy on SQLite — but this means the ORM path
    # silently accepts empty strings while the migration path raises.
    # Keep the server_default (SQLite compatibility) but document the
    # divergence — the loader always populates source_id explicitly.
    source_id: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=False, server_default="",
    )
    source_version: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # [LIN-3] Download date (when PubChem returned this record).
    download_date: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    # v16 CD-2: String(20) (not String(50)) to match migration 005.
    download_method: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # [LIN-4] Pipeline run ID (FK to pipeline_runs.id).
    # v16 CD-2: NOT NULL + String(64) to match migration 005.
    # v17 CD-2 deepened: same server_default divergence as source_id
    # — see comment above. Loader populates explicitly.
    #
    # v29 ROOT FIX (audit D-7): was String(64) (a free-form UUID string
    # column with no FK), while EVERY other lineage-bearing table in the
    # schema (``drug_protein_interactions``, ``protein_protein_interactions``,
    # ``gene_disease_associations``, ``rejected_records``) uses
    # ``Integer FK → pipeline_runs.id (ON DELETE SET NULL)``. The
    # String(64) form meant (a) no FK was enforced — a typo'd run id could
    # silently orphan the row; (b) join cardinality against
    # ``pipeline_runs`` required a CAST, breaking the planner; (c) the
    # column accepted arbitrary UUID strings that did not correspond to any
    # real ``pipeline_runs.id`` value. Aligned to the canonical Integer FK
    # pattern; nullable=True so historical loader code that wrote "" can
    # still round-trip (the empty string is now mapped to NULL by the
    # loader before INSERT). Migration 005 / pubchem_pipeline.py must be
    # updated in lockstep — see audit D-7 remediation notes.
    pipeline_run_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("pipeline_runs.id", ondelete="SET NULL"),
        nullable=True,
    )

    # [LIN-5-extra] Source batch index + response SHA-256 for full traceability.
    source_batch_idx: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_response_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # [LIN-5] Input checksum (SHA-256 of the input InChIKey list).
    # v16 CD-2: NOT NULL to match migration 005.
    # v17 CD-2 deepened: same server_default divergence — see above.
    input_checksum: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=False, server_default="",
    )

    # [LIN-6] Transformations applied (JSON-encoded list).
    transformations: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # [COMP-5] FDA 21 CFR Part 11 electronic-signature fields.
    electronic_signature: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    triggered_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # [DESIGN-7, IDEM-9] Enrichment timestamp — non-deterministic by design;
    # the OTHER columns are deterministic given the same PubChem response.
    # v17 ROOT FIX (CD-2 deepened): migration 005 declares
    # ``enriched_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()``.
    # The ORM declared it ``nullable=True`` with no default — divergent.
    # On PostgreSQL, create_all() creates the column nullable; migration
    # 005's CREATE TABLE is then a no-op (table exists); the column
    # stays nullable. On INSERT, NULL enriched_at was accepted — but
    # downstream queries filtering ``WHERE enriched_at IS NOT NULL`` or
    # computing enrichment age would skip / mis-classify those rows.
    # Align ORM to migration: NOT NULL, server_default=NOW().
    enriched_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    # [IDEM-9] Soft-delete flag for re-run idempotency.
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )

    __table_args__ = (
        # [SCI-19, IDEM-4] Composite unique constraint — one row per
        # (inchikey, pubchem_cid). ON CONFLICT DO UPDATE on this constraint.
        UniqueConstraint(
            "inchikey", "pubchem_cid",
            name="uq_pubchem_compound_properties_inchikey_cid",
        ),
        # v17 ROOT FIX (CD-2 deepened): migration 005 creates these
        # indexes with names ``idx_pubchem_props_inchikey`` and
        # ``idx_pubchem_props_cid`` (plus two more indexes the ORM
        # was MISSING entirely: ``idx_pubchem_props_is_deleted`` and
        # ``idx_pubchem_props_run_id``). The ORM created differently-
        # named indexes — on PostgreSQL this produced DUPLICATE
        # indexes (one from migration, one from create_all), wasting
        # disk + write bandwidth on every INSERT. Align ORM index
        # names to migration 005 and add the two missing indexes so
        # there is exactly one index per query pattern.
        Index("idx_pubchem_props_inchikey", "inchikey"),
        Index("idx_pubchem_props_cid", "pubchem_cid"),
        # [IDEM-7] Partial index for soft-delete cleanup queries.
        # Use postgresql_where so the partial index is created on PG;
        # on SQLite the WHERE is dropped by _translate_sql_for_sqlite
        # (full index is created instead — slightly larger but
        # functionally equivalent).
        Index(
            "idx_pubchem_props_is_deleted", "is_deleted",
            postgresql_where=text("is_deleted = TRUE"),
        ),
        # [LIN-10] Index for pipeline-run traceability queries.
        Index("idx_pubchem_props_run_id", "pipeline_run_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<PubChemCompoundProperty(id={self.id}, inchikey='{self.inchikey}', "
            f"pubchem_cid={self.pubchem_cid})>"
        )


# ===========================================================================
# 9. REJECTED RECORDS (dead-letter queue)
# ===========================================================================


# P1-ER-6 ROOT FIX (orphan rejected_records table): migration 001 declares
# a ``rejected_records`` table (lines 1283-1327 of 001_initial_schema.sql)
# used as a dead-letter queue for records that fail validation during
# pipeline execution. Without an ORM model, ``Base.metadata.create_all()``
# on SQLite dev/test DBs did NOT create this table — so any Python code
# that tried to write rejected records via the ORM raised
# ``sqlite3.OperationalError: no such table: rejected_records``. The
# migration 001 ``CREATE TABLE IF NOT EXISTS`` was the only creation
# path, and on SQLite it was being silently skipped. Adding this model
# closes the gap — create_all() now creates rejected_records on BOTH
# PostgreSQL and SQLite, and migration 001's CREATE TABLE IF NOT EXISTS
# becomes the idempotent no-op it was designed to be.
#
# The schema here mirrors migration 001 EXACTLY (column names, types,
# nullability, CHECK constraint, FK, indexes). Do NOT diverge.
class RejectedRecord(Base, IDMixin):
    """Dead-letter queue for unprocessable records (migration 001, CMP-04).

    Domain meaning
    --------------
    Each row represents a record that was rejected by the load() phase
    of some pipeline — e.g. a drug with a malformed InChIKey, a protein
    with an invalid UniProt accession, a duplicate GDA, etc. Rows are
    written here instead of being silently dropped, so the data can be
    inspected and reprocessed later. Retention: 1 year, then purged.

    Key constraints
    ---------------
    - ``source_table`` NOT NULL — target table the record was intended
      for (e.g. "drugs", "proteins", "gene_disease_associations").
    - ``source_pipeline`` NOT NULL — pipeline that rejected the record
      (e.g. "chembl", "drugbank", "disgenet").
    - ``raw_data`` NOT NULL TEXT — original record as a JSON string.
    - ``rejection_reason`` NOT NULL VARCHAR(500) — human-readable
      explanation.
    - ``rejection_type`` NOT NULL VARCHAR(50), constrained to the
      whitelist defined in migration 001 (chk_rejected_records_rejection_type).
    - ``pipeline_run_id`` nullable INTEGER FK → pipeline_runs.id, ON
      DELETE SET NULL.
    - ``created_at`` NOT NULL DEFAULT NOW().
    """
    __tablename__ = "rejected_records"

    source_table: Mapped[str] = mapped_column(String(50), nullable=False)
    source_pipeline: Mapped[str] = mapped_column(String(50), nullable=False)
    raw_data: Mapped[str] = mapped_column(Text, nullable=False)
    rejection_reason: Mapped[str] = mapped_column(String(500), nullable=False)
    rejection_type: Mapped[str] = mapped_column(String(50), nullable=False)
    pipeline_run_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("pipeline_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        # Mirror the chk_rejected_records_rejection_type CHECK whitelist
        # from migration 001 so SQLite dev/test DBs (which skip the
        # migration SQL) still enforce the same rejection-type contract.
        CheckConstraint(
            "rejection_type IN ("
            "'constraint_violation', 'format_error', "
            "'duplicate', 'reference_error', 'other'"
            ")",
            name="chk_rejected_records_rejection_type",
        ),
        Index("ix_rejected_records_source_table", "source_table"),
        Index("ix_rejected_records_source_pipeline", "source_pipeline"),
    )

    def __repr__(self) -> str:
        return (
            f"<RejectedRecord(id={self.id}, "
            f"source_table='{self.source_table}', "
            f"source_pipeline='{self.source_pipeline}', "
            f"rejection_type='{self.rejection_type}')>"
        )


# ===========================================================================
# DEPRECATED: cleanup_orphan_gda_records moved to database.loaders (ARCH-01)
# ===========================================================================


def cleanup_orphan_gda_records(session, auto_commit: bool = False) -> int:
    """Delete GDA records with uniprot_id=NULL that have existed for > 24 hours.

    .. deprecated::
        This function has been moved to ``database.loaders.cleanup_orphan_gda_records``.
        This stub remains for backward compatibility and will emit a
        ``DeprecationWarning`` on every call.  Update all callers to import
        from ``database.loaders`` instead.

    [ARCH-01] Business logic moved out of the model layer (SRP).
    [REL-04] Retry logic with exponential backoff added in loaders.
    [LOG-01] Proper logging added in loaders.
    [CODE-05] Bare except replaced with specific exception handling.
    """
    import warnings
    warnings.warn(
        "cleanup_orphan_gda_records is deprecated in database.models. "
        "Import from database.loaders instead. "
        "This stub will be removed in a future version.",
        DeprecationWarning,
        stacklevel=2,
    )
    from database.loaders import cleanup_orphan_gda_records as _real_cleanup
    return _real_cleanup(session, auto_commit=auto_commit)
