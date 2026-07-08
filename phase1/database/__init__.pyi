"""
Type stubs for the database package.

These type declarations enable static type checking (mypy, pyright) for
code that imports from the database package via the lazy-loading facade.

[TEST-04] Kept in sync with database.models and database.loaders.
"""

from __future__ import annotations

import datetime
import enum
from typing import Any, Dict, Generator, List, Optional, Tuple

import pandas as pd
from sqlalchemy import Engine
from sqlalchemy.engine import Connection
from sqlalchemy.orm import DeclarativeBase, Session, scoped_session

# ---------------------------------------------------------------------------
# Package metadata
# ---------------------------------------------------------------------------
__version__: str

# ---------------------------------------------------------------------------
# Schema version (ARCH-07)
# ---------------------------------------------------------------------------
SCHEMA_VERSION: int

# ---------------------------------------------------------------------------
# Column length constants (CFG-01)
# ---------------------------------------------------------------------------
INCHIKEY_LENGTH: int
UNIPROT_ID_LENGTH: int
GENE_SYMBOL_LENGTH: int
DRUG_NAME_LENGTH: int
CHEMBL_ID_LENGTH: int
DRUGBANK_ID_LENGTH: int
STRING_ID_LENGTH: int
SOURCE_LENGTH: int
PIPELINE_SOURCE_LENGTH: int
PMID_LIST_LENGTH: int
ERROR_MESSAGE_LENGTH: int
DISEASE_ID_LENGTH: int
DISEASE_ID_TYPE_LENGTH: int

# ---------------------------------------------------------------------------
# Domain enums (DES-05)
# ---------------------------------------------------------------------------

class ClinicalPhase(int, enum.Enum):
    PRECLINICAL = 0
    PHASE_I = 1
    PHASE_II = 2
    PHASE_III = 3
    APPROVED = 4

class DrugType(str, enum.Enum):
    SMALL_MOLECULE = "small_molecule"
    ANTIBODY = "antibody"
    PROTEIN = "protein"
    OLIGONUCLEOTIDE = "oligonucleotide"
    PEPTIDE = "peptide"
    CELL_THERAPY = "cell_therapy"
    GENE_THERAPY = "gene_therapy"
    UNKNOWN = "unknown"

class PipelineStatus(str, enum.Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"

class InteractionType(str, enum.Enum):
    INHIBITOR = "inhibitor"
    ACTIVATOR = "activator"
    AGONIST = "agonist"
    ANTAGONIST = "antagonist"
    BINDING_AGENT = "binding_agent"
    BLOCKER = "blocker"
    MODULATOR = "modulator"
    UNKNOWN = "unknown"

class ActivityType(str, enum.Enum):
    IC50 = "IC50"
    EC50 = "EC50"
    KI = "Ki"
    KD = "Kd"
    POTENCY = "potency"
    AC50 = "AC50"
    UNKNOWN = "unknown"

# ---------------------------------------------------------------------------
# Connection Management (database.connection / database.base)
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    """Declarative base class shared by all ORM models."""
    ...

def get_engine() -> Engine:
    """Return the global SQLAlchemy Engine, creating it on first call."""
    ...

def get_session_factory() -> scoped_session:
    """Return the thread-safe scoped session factory."""
    ...

def get_db_session() -> Generator[Session, None, None]:
    """Yield a database session with automatic commit/rollback/close."""
    ...

def init_db() -> None:
    """Create all tables and run pending migrations."""
    ...

def dispose_engine() -> None:
    """Dispose of the global engine and session factory."""
    ...

def check_connection() -> bool:
    """Verify the database is reachable."""
    ...

# ---------------------------------------------------------------------------
# ORM Models (database.models)
# ---------------------------------------------------------------------------

class SchemaVersion:
    __tablename__: str = "schema_version"
    id: int
    version: int
    applied_at: datetime.datetime
    description: str

class Drug:
    __tablename__: str = "drugs"
    id: int
    inchikey: str
    name: str
    chembl_id: Optional[str]
    drugbank_id: Optional[str]
    pubchem_cid: Optional[int]
    molecular_formula: Optional[str]
    molecular_weight: Optional[float]  # Numeric(12,6) [SCI-07]
    smiles: Optional[str]
    is_fda_approved: bool
    max_phase: Optional[int]  # 0-4 [SCI-02]
    drug_type: Optional[str]
    mechanism_of_action: Optional[str]
    is_deleted: bool  # [DES-08]
    deleted_at: Optional[datetime.datetime]  # [DES-08]
    created_at: datetime.datetime
    updated_at: datetime.datetime
    drug_protein_interactions: List["DrugProteinInteraction"]

    def soft_delete(self) -> None: ...
    def restore(self) -> None: ...
    def to_dict(self) -> dict[str, Any]: ...
    @classmethod
    def graph_identity(cls) -> str: ...

class Protein:
    __tablename__: str = "proteins"
    id: int
    uniprot_id: str  # String(10) [SCI-05]
    gene_name: Optional[str]  # DEPRECATED [DQ-06]
    gene_symbol: Optional[str]  # [SCI-04]
    protein_name: Optional[str]
    organism: Optional[str]
    sequence: Optional[str]  # [SCI-08]
    function_desc: Optional[str]
    string_id: Optional[str]
    is_deleted: bool  # [DES-08]
    deleted_at: Optional[datetime.datetime]  # [DES-08]
    created_at: datetime.datetime
    updated_at: datetime.datetime  # [DES-06]
    drug_protein_interactions: List["DrugProteinInteraction"]
    gene_disease_associations: List["GeneDiseaseAssociation"]
    ppi_as_protein_a: List["ProteinProteinInteraction"]
    ppi_as_protein_b: List["ProteinProteinInteraction"]

    @property
    def all_ppi_interactions(self) -> List["ProteinProteinInteraction"]: ...
    @property
    def all_ppi_partners(self) -> List["Protein"]: ...
    @property
    def canonical_protein_name(self) -> Optional[str]: ...
    @property
    def display_name(self) -> str: ...
    def soft_delete(self) -> None: ...
    def restore(self) -> None: ...
    def to_dict(self) -> dict[str, Any]: ...
    @classmethod
    def graph_identity(cls) -> str: ...

class DrugProteinInteraction:
    __tablename__: str = "drug_protein_interactions"
    id: int
    drug_id: int
    protein_id: int
    interaction_type: Optional[str]
    activity_value: Optional[float]
    activity_type: Optional[str]
    activity_units: Optional[str]
    source: Optional[str]
    source_id: Optional[str]  # [DES-04] now nullable
    confidence_score: Optional[float]
    source_version: Optional[str]  # [LINE-01]
    source_fetch_date: Optional[datetime.datetime]  # [LINE-01]
    entity_resolved: Optional[bool]  # [LINE-01]
    pipeline_run_id: Optional[int]  # [IDEM-01]
    created_at: datetime.datetime
    updated_at: datetime.datetime  # [DES-06]
    drug: Drug
    protein: Protein

class ProteinProteinInteraction:
    __tablename__: str = "protein_protein_interactions"
    id: int
    protein_a_id: int  # must be < protein_b_id [DES-02]
    protein_b_id: int
    combined_score: Optional[int]  # 0-1000 [SCI-03]
    experimental_score: Optional[int]  # 0-1000 [SCI-03]
    database_score: Optional[int]  # 0-1000 [SCI-03]
    textmining_score: Optional[int]  # 0-1000 [SCI-03]
    source: str  # [CFG-03] no default, must be specified
    score_json: Optional[str]  # [INT-04]
    pipeline_run_id: Optional[int]  # [IDEM-01]
    created_at: datetime.datetime
    updated_at: datetime.datetime  # [DES-06]
    protein_a: Protein
    protein_b: Protein

    @property
    def normalized_combined_score(self) -> Optional[float]: ...

class GeneDiseaseAssociation:
    __tablename__: str = "gene_disease_associations"
    id: int
    gene_symbol: Optional[str]  # [SCI-04]
    uniprot_id: Optional[str]
    protein_id: Optional[int]  # [DES-01] integer FK for fast joins
    disease_id: Optional[str]
    disease_id_type: Optional[str]  # [SCI-06]
    disease_name: Optional[str]
    association_type: Optional[str]
    score: Optional[float]
    source: Optional[str]
    pmid_list: Optional[str]  # String(2000) [SEC-02]
    score_type: Optional[str]  # [LINE-03]
    score_method: Optional[str]  # [LINE-03]
    pipeline_run_id: Optional[int]  # [IDEM-01]
    created_at: datetime.datetime
    updated_at: datetime.datetime  # [DES-06]
    protein: Protein

class EntityMapping:
    __tablename__: str = "entity_mapping"
    id: int
    canonical_inchikey: Optional[str]  # String(50) [SCI-01]
    canonical_name: Optional[str]
    chembl_id: Optional[str]
    drugbank_id: Optional[str]
    pubchem_cid: Optional[int]
    uniprot_id: Optional[str]
    string_id: Optional[str]
    match_confidence: Optional[float]  # 0.0-1.0 [DQ-07]
    match_method: Optional[str]
    match_history: Optional[str]  # [LINE-04]
    created_at: datetime.datetime
    updated_at: datetime.datetime  # [DES-06]

class PipelineRun:
    __tablename__: str = "pipeline_runs"
    id: int
    source: str  # constrained to 7 pipeline names [DES-07]
    run_date: datetime.datetime
    status: Optional[str]  # [DES-05]
    records_downloaded: Optional[int]
    records_cleaned: Optional[int]
    records_loaded: Optional[int]
    error_message: Optional[str]  # String(500) [SEC-04]
    duration_seconds: Optional[int]  # non-negative [DQ-08]
    created_at: datetime.datetime
    updated_at: datetime.datetime  # [DES-06]

def cleanup_orphan_gda_records(
    session: Session,
    auto_commit: bool = False,
) -> int:
    """Delete GDA records with uniprot_id=NULL older than 24 hours.

    .. deprecated:: Import from database.loaders instead.
    """
    ...

# ---------------------------------------------------------------------------
# Data Operations (database.loaders)
# ---------------------------------------------------------------------------

def bulk_upsert_drugs(
    session: Session,
    df: pd.DataFrame,
    batch_size: int = 1000,
) -> int:
    """Bulk upsert drugs with ON CONFLICT (inchikey) DO UPDATE."""
    ...

def bulk_upsert_proteins(
    session: Session,
    df: pd.DataFrame,
    batch_size: int = 1000,
) -> int:
    """Bulk upsert proteins with ON CONFLICT (uniprot_id) DO UPDATE."""
    ...

def bulk_upsert_dpi(
    session: Session,
    df: pd.DataFrame,
    batch_size: int = 1000,
) -> int:
    """Bulk upsert drug-protein interactions."""
    ...

def bulk_upsert_ppi(
    session: Session,
    df: pd.DataFrame,
    batch_size: int = 1000,
) -> int:
    """Bulk upsert protein-protein interactions."""
    ...

def bulk_upsert_gda(
    session: Session,
    df: pd.DataFrame,
    batch_size: int = 1000,
) -> int:
    """Bulk upsert gene-disease associations."""
    ...

def bulk_upsert_entity_mapping(
    session: Session,
    df: pd.DataFrame,
    batch_size: int = 1000,
) -> int:
    """Bulk upsert entity mapping rows."""
    ...

def bulk_update_drugs_from_pubchem(
    session: Session,
    df: pd.DataFrame,
    batch_size: int = 1000,
) -> int:
    """Update drugs with PubChem data where pubchem_cid is NULL."""
    ...

def get_uniprot_to_protein_id_map(
    session: Session,
) -> Dict[str, int]:
    """Return uniprot_id -> protein.id mapping."""
    ...

def get_inchikey_to_drug_id_map(
    session: Session,
) -> Dict[str, int]:
    """Return inchikey -> drug.id mapping."""
    ...

def build_gene_to_uniprot_maps(
    session: Session,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Build gene_symbol -> uniprot_id and protein_name -> uniprot_id maps."""
    ...

def resolve_gene_symbol_to_uniprot(
    df: pd.DataFrame,
    gene_to_uniprot: Dict[str, str],
    protein_name_to_uniprot: Dict[str, str],
) -> pd.DataFrame:
    """Resolve gene_symbol -> uniprot_id using pre-built maps."""
    ...

# ---------------------------------------------------------------------------
# Schema Migrations (database.migrations)
# ---------------------------------------------------------------------------

def run_migrations(
    engine: Optional[Engine] = ...,
    config: Any = ...,  # MigrationConfig
) -> Any:  # MigrationResult
    """Run cross-dialect schema migrations with dependency injection."""
    ...

def check_migrations(engine: Optional[Engine] = ...) -> Any:
    """Verify all migrations are applied and schema version matches."""
    ...

def get_migration_status(engine: Optional[Engine] = ...) -> Any:
    """Return detailed migration status including history."""
    ...

# ---------------------------------------------------------------------------
# Package-level utilities
# ---------------------------------------------------------------------------

def validate_data_quality_infrastructure(
    session: Session,
) -> Dict[str, Any]:
    """Validate data quality infrastructure is properly configured."""
    ...

def _validate_database_security() -> Dict[str, Any]:
    """Audit DATABASE_URL for insecure patterns."""
    ...

def _reset() -> None:
    """Clear the lazy-loaded symbol cache for testing."""
    ...

def _log_import_status() -> Dict[str, bool]:
    """Log which symbols have been loaded and which haven't."""
    ...
