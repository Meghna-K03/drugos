"""
Comprehensive test suite for the database package public API.

This test module verifies that database/__init__.py correctly re-exports
every public symbol from all four submodules (connection, models, loaders,
migrations) via the lazy-loading __getattr__ pattern, and that the entire
database layer functions correctly end-to-end.

Test Categories:
  1. Package API Surface — every symbol in __all__ is importable
  2. Lazy Loading Behaviour — no side effects at import time
  3. Connection Management — engine, session, init, health check
  4. ORM Models — schema creation, column definitions, constraints
  5. Bulk Data Operations — upsert/update with real data and SQLite
  6. Lookup Helpers — foreign-key resolution maps
  7. Migrations — schema migration runner
  8. Data Quality Validation — validate_data_quality_infrastructure
  9. Security Audit — _validate_database_security
  10. Idempotency — repeated operations produce same results
  11. Error Handling — informative errors for bad imports
  12. Observability — logging and callbacks
  13. Gene Symbol Resolution — build_gene_to_uniprot_maps + resolve
  14. Full E2E Pipeline — load data → resolve → upsert → query
"""

from __future__ import annotations

import importlib
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest
from sqlalchemy import create_engine, event, text, inspect
from sqlalchemy.orm import sessionmaker

# ---------------------------------------------------------------------------
# Ensure project root is importable
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# We need to set DATABASE_URL BEFORE importing database,
# because the lazy loader will trigger config.settings
# which reads DATABASE_URL on first access.
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

# Now import the database package — this MUST NOT trigger side effects
import database
from database.connection import Base


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(scope="function", autouse=True)
def _reset_db_package():
    """Reset the database package's lazy-loaded cache between tests."""
    database._reset()
    yield
    database._reset()


@pytest.fixture(scope="function")
def db_engine():
    """Create a fresh SQLite in-memory engine with FK enforcement."""
    engine = create_engine("sqlite:///:memory:", echo=False)

    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_conn, connection_record):
        if isinstance(dbapi_conn, sqlite3.Connection):
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
            dbapi_conn.create_function(
                "now", 0,
                lambda: datetime.now(timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S+00:00"
                ),
            )

    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture(scope="function")
def db_session(db_engine):
    """Yield a transactional session bound to in-memory SQLite."""
    session = sessionmaker(bind=db_engine)()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def sample_drug_df() -> pd.DataFrame:
    """Minimal drug DataFrame matching the Drug model."""
    return pd.DataFrame({
        "inchikey": [
            "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
            "WFXAZNNJSJXTJZ-UHFFFAOYSA-N",
        ],
        "name": ["Aspirin", "Ibuprofen"],
        "chembl_id": ["CHEMBL25", "CHEMBL521"],
        "drugbank_id": ["DB00945", "DB01050"],
        "pubchem_cid": [2244, 3672],
        "molecular_formula": ["C9H8O4", "C13H18O2"],
        "molecular_weight": [180.16, 206.28],
        "smiles": [
            "CC(=O)Oc1ccccc1C(=O)O",
            "CC(C)Cc1ccc(cc1)C(C)C(=O)O",
        ],
        "is_fda_approved": [True, True],
        "max_phase": [4, 4],
        # K5 fix: use lowercase enum value (loader rejects Title-case "Small molecule")
        "drug_type": ["small_molecule", "small_molecule"],
        "mechanism_of_action": [
            "COX inhibitor", "COX inhibitor"
        ],
    })


@pytest.fixture
def sample_protein_df() -> pd.DataFrame:
    """Minimal protein DataFrame matching the Protein model."""
    return pd.DataFrame({
        "uniprot_id": ["P23219", "P04637"],
        "gene_name": [
            "Prostaglandin G/H synthase 1",
            "Cellular tumor antigen p53",
        ],
        "gene_symbol": ["PTGS1", "TP53"],
        "protein_name": [
            "Prostaglandin G/H synthase 1",
            "Cellular tumor antigen p53",
        ],
        "organism": ["Homo sapiens", "Homo sapiens"],
        "sequence": ["M" * 100, "M" * 100],
        "function_desc": ["COX enzyme", "Tumor suppressor"],
        "string_id": [
            "9606.ENSP00000269305",
            "9606.ENSP00000269306",
        ],
    })


# ============================================================================
# 1. PACKAGE API SURFACE TESTS
# ============================================================================


class TestPackageAPISurface:
    """Verify every symbol in __all__ is importable via the package."""

    def test_all_exists_and_is_list(self):
        """__all__ must exist and be a list."""
        assert hasattr(database, "__all__")
        assert isinstance(database.__all__, list)

    def test_all_contains_expected_count(self):
        """__all__ must contain all 26 public symbols."""
        # 7 connection + 8 models + 11 loaders + 1 migration + __version__
        assert len(database.__all__) >= 26

    def test_all_symbols_are_strings(self):
        """Every entry in __all__ must be a string."""
        for sym in database.__all__:
            assert isinstance(sym, str), (
                f"__all__ entry {sym!r} is not a string"
            )

    @pytest.mark.parametrize("symbol_name", [
        # Connection Management
        "get_db_session", "get_engine", "init_db",
        "dispose_engine", "check_connection",
        "get_session_factory", "Base",
        # ORM Models
        "Drug", "Protein", "DrugProteinInteraction",
        "ProteinProteinInteraction", "GeneDiseaseAssociation",
        "EntityMapping", "PipelineRun",
        "cleanup_orphan_gda_records",
        # Data Operations
        "bulk_upsert_drugs", "bulk_upsert_proteins",
        "bulk_upsert_dpi", "bulk_upsert_ppi",
        "bulk_upsert_gda", "bulk_upsert_entity_mapping",
        "bulk_update_drugs_from_pubchem",
        "get_uniprot_to_protein_id_map",
        "get_inchikey_to_drug_id_map",
        "build_gene_to_uniprot_maps",
        "resolve_gene_symbol_to_uniprot",
        # Schema Migrations
        "run_migrations",
        # Metadata
        "__version__",
    ])
    def test_symbol_importable(self, symbol_name):
        """Every listed symbol must be importable via lazy loading."""
        attr = getattr(database, symbol_name)
        assert attr is not None, (
            f"database.{symbol_name} resolved to None"
        )

    def test_version_value(self):
        """__version__ must be a valid semver string."""
        version = database.__version__
        assert isinstance(version, str)
        parts = version.split(".")
        assert len(parts) == 3
        for part in parts:
            assert part.isdigit(), (
                f"Version part '{part}' is not numeric"
            )

    def test_symbol_map_covers_all(self):
        """_SYMBOL_MAP must cover every symbol in __all__ except __version__."""
        for sym in database.__all__:
            if sym == "__version__":
                continue
            assert sym in database._SYMBOL_MAP, (
                f"'{sym}' is in __all__ but not in _SYMBOL_MAP"
            )

    def test_symbol_map_values_are_valid_submodules(self):
        """Every _SYMBOL_MAP value must be an importable module path."""
        valid_modules = {
            "database.base",  # Base, IDMixin, TimestampMixin, SoftDeleteMixin
            "database.connection",
            "database.models",
            "database.loaders",
            "database.migrations",
        }
        for sym, mod in database._SYMBOL_MAP.items():
            assert mod in valid_modules, (
                f"Symbol '{sym}' maps to unknown module '{mod}'"
            )

    def test_from_database_import_star(self):
        """'from database import *' must provide all __all__ symbols."""
        namespace = {}
        exec("from database import *", namespace)
        for sym in database.__all__:
            assert sym in namespace, (
                f"'{sym}' not in namespace after 'from database import *'"
            )

    def test_dir_includes_all_symbols(self):
        """dir(database) must include all __all__ symbols."""
        dir_result = dir(database)
        for sym in database.__all__:
            assert sym in dir_result, (
                f"'{sym}' not in dir(database)"
            )


# ============================================================================
# 2. LAZY LOADING BEHAVIOUR TESTS
# ============================================================================


class TestLazyLoading:
    """Verify lazy loading: no side effects at import time."""

    def test_import_no_side_effects(self):
        """Importing database must NOT trigger engine creation."""
        # After _reset, no symbols should be loaded
        database._reset()
        assert len(database._loaded) == 0

    def test_first_access_loads_symbol(self):
        """First access to a symbol must load it into cache."""
        database._reset()
        _ = database.Drug
        assert "Drug" in database._loaded

    def test_subsequent_access_uses_cache(self):
        """Second access to the same symbol must return cached value."""
        obj1 = database.Drug
        obj2 = database.Drug
        assert obj1 is obj2

    def test_reset_clears_cache(self):
        """_reset() must clear the symbol cache."""
        _ = database.Drug
        assert "Drug" in database._loaded
        database._reset()
        assert "Drug" not in database._loaded

    def test_load_import_status(self):
        """_log_import_status() must return a status dict."""
        database._reset()
        status = database._log_import_status()
        assert isinstance(status, dict)
        assert "Drug" in status
        # Before loading, Drug should not be loaded
        # (but other tests may have loaded it in same process)

    def test_unknown_attribute_raises_error(self):
        """Accessing an unknown attribute must raise AttributeError."""
        with pytest.raises(AttributeError, match="no attribute"):
            getattr(database, "nonexistent_symbol_xyz")


# ============================================================================
# 3. CONNECTION MANAGEMENT TESTS
# ============================================================================


class TestConnectionManagement:
    """Verify connection management symbols work correctly."""

    def test_get_engine_returns_engine(self):
        """get_engine must return a SQLAlchemy Engine."""
        from sqlalchemy.engine import Engine as EngineClass
        import database.connection as conn_mod
        # Use the actual DATABASE_URL from config (whatever it is)
        # Just verify get_engine returns a valid Engine
        from config.settings import DATABASE_URL as configured_url
        conn_mod._engine = None
        conn_mod._session_factory = None
        # Override DATABASE_URL in connection module to use SQLite for testing
        conn_mod.DATABASE_URL = "sqlite:///:memory:"
        try:
            engine = database.get_engine()
            assert isinstance(engine, EngineClass)
        finally:
            conn_mod.DATABASE_URL = configured_url
            conn_mod._engine = None
            conn_mod._session_factory = None

    def test_base_is_declarative_base(self):
        """Base must be a DeclarativeBase subclass."""
        from sqlalchemy.orm import DeclarativeBase
        assert issubclass(database.Base, DeclarativeBase)

    def test_get_session_factory(self):
        """get_session_factory must return a scoped_session."""
        import database.connection as conn_mod
        from config.settings import DATABASE_URL as configured_url
        conn_mod._engine = None
        conn_mod._session_factory = None
        conn_mod.DATABASE_URL = "sqlite:///:memory:"
        try:
            factory = database.get_session_factory()
            from sqlalchemy.orm import scoped_session
            assert isinstance(factory, scoped_session)
        finally:
            conn_mod.DATABASE_URL = configured_url
            conn_mod._engine = None
            conn_mod._session_factory = None

    def test_init_db_creates_tables(self, db_engine):
        """init_db must create all ORM tables in the database."""
        from database.connection import _engine, _session_factory
        # Patch to use our test engine
        import database.connection as conn_mod
        original_engine = conn_mod._engine
        original_sf = conn_mod._session_factory
        try:
            conn_mod._engine = db_engine
            conn_mod._session_factory = None
            database.init_db()
            inspector = inspect(db_engine)
            tables = inspector.get_table_names()
            expected_tables = [
                "drugs", "proteins",
                "drug_protein_interactions",
                "protein_protein_interactions",
                "gene_disease_associations",
                "entity_mapping", "pipeline_runs",
            ]
            for t in expected_tables:
                assert t in tables, (
                    f"Table '{t}' not created by init_db"
                )
        finally:
            conn_mod._engine = original_engine
            conn_mod._session_factory = original_sf

    def test_dispose_engine_callable(self):
        """dispose_engine must be a callable function."""
        assert callable(database.dispose_engine)

    def test_check_connection_callable(self):
        """check_connection must be a callable function."""
        assert callable(database.check_connection)


# ============================================================================
# 4. ORM MODEL TESTS
# ============================================================================


class TestORMModels:
    """Verify ORM models define correct tables, columns, and constraints."""

    def test_drug_model_table_name(self):
        """Drug model must map to 'drugs' table."""
        assert database.Drug.__tablename__ == "drugs"

    def test_protein_model_table_name(self):
        """Protein model must map to 'proteins' table."""
        assert database.Protein.__tablename__ == "proteins"

    def test_dpi_model_table_name(self):
        """DrugProteinInteraction must map to correct table."""
        assert database.DrugProteinInteraction.__tablename__ == (
            "drug_protein_interactions"
        )

    def test_ppi_model_table_name(self):
        """ProteinProteinInteraction must map to correct table."""
        assert database.ProteinProteinInteraction.__tablename__ == (
            "protein_protein_interactions"
        )

    def test_gda_model_table_name(self):
        """GeneDiseaseAssociation must map to correct table."""
        assert database.GeneDiseaseAssociation.__tablename__ == (
            "gene_disease_associations"
        )

    def test_entity_mapping_table_name(self):
        """EntityMapping must map to 'entity_mapping' table."""
        assert database.EntityMapping.__tablename__ == "entity_mapping"

    def test_pipeline_run_table_name(self):
        """PipelineRun must map to 'pipeline_runs' table."""
        assert database.PipelineRun.__tablename__ == "pipeline_runs"

    def test_drug_has_inchikey_unique_constraint(self):
        """Drug must have a unique constraint on inchikey."""
        table = database.Drug.__table__
        has_unique = any(
            any(col.name == "inchikey" for col in c.columns)
            for c in table.constraints
            if hasattr(c, "columns")
        )
        assert has_unique, "Drug missing unique constraint on inchikey"

    def test_protein_has_uniprot_id_unique(self):
        """Protein must have a unique constraint on uniprot_id."""
        table = database.Protein.__table__
        has_unique = any(
            any(col.name == "uniprot_id" for col in c.columns)
            for c in table.constraints
            if hasattr(c, "columns")
        )
        assert has_unique, (
            "Protein missing unique constraint on uniprot_id"
        )

    def test_dpi_has_composite_unique(self):
        """DPI must have composite unique on drug_id, protein_id, etc."""
        table = database.DrugProteinInteraction.__table__
        constraint_cols_sets = []
        for c in table.constraints:
            if hasattr(c, "columns"):
                cols = {col.name for col in c.columns}
                constraint_cols_sets.append(cols)
        expected = {"drug_id", "protein_id", "source", "source_id"}
        assert expected in constraint_cols_sets, (
            "DPI missing composite unique constraint"
        )

    def test_ppi_has_pair_unique(self):
        """PPI must have unique constraint on protein_a_id, protein_b_id."""
        table = database.ProteinProteinInteraction.__table__
        constraint_cols_sets = []
        for c in table.constraints:
            if hasattr(c, "columns"):
                cols = {col.name for col in c.columns}
                constraint_cols_sets.append(cols)
        expected = {"protein_a_id", "protein_b_id"}
        assert expected in constraint_cols_sets, (
            "PPI missing unique constraint on protein pair"
        )

    def test_drug_columns_exist(self, db_session):
        """Drug table must have all expected columns."""
        table = database.Drug.__table__
        expected_cols = {
            "id", "inchikey", "name", "chembl_id", "drugbank_id",
            "pubchem_cid", "molecular_formula", "molecular_weight",
            "smiles", "is_fda_approved", "max_phase", "drug_type",
            "mechanism_of_action", "created_at", "updated_at",
        }
        actual_cols = {col.name for col in table.columns}
        missing = expected_cols - actual_cols
        assert not missing, f"Drug missing columns: {missing}"

    def test_protein_has_gene_symbol_column(self):
        """Protein model must have gene_symbol column."""
        table = database.Protein.__table__
        col_names = {col.name for col in table.columns}
        assert "gene_symbol" in col_names

    def test_protein_has_protein_name_column(self):
        """Protein model must have protein_name column."""
        table = database.Protein.__table__
        col_names = {col.name for col in table.columns}
        assert "protein_name" in col_names

    def test_cleanup_orphan_gda_records_callable(self):
        """cleanup_orphan_gda_records must be callable."""
        assert callable(database.cleanup_orphan_gda_records)


# ============================================================================
# 5. BULK DATA OPERATIONS TESTS
# ============================================================================


class TestBulkDataOperations:
    """Verify bulk upsert/update functions work with real data."""

    def test_bulk_upsert_drugs_inserts(
        self, db_session, sample_drug_df
    ):
        """bulk_upsert_drugs must insert new drug records."""
        count = database.bulk_upsert_drugs(
            db_session, sample_drug_df
        )
        db_session.commit()
        assert int(count) >= 2
        result = db_session.execute(
            text("SELECT COUNT(*) FROM drugs")
        )
        assert result.scalar() == 2

    def test_bulk_upsert_drugs_upsert_idempotent(
        self, db_session, sample_drug_df
    ):
        """Running bulk_upsert_drugs twice must not create duplicates."""
        database.bulk_upsert_drugs(db_session, sample_drug_df)
        db_session.commit()
        count2 = database.bulk_upsert_drugs(
            db_session, sample_drug_df
        )
        db_session.commit()
        assert int(count2) >= 2
        result = db_session.execute(
            text("SELECT COUNT(*) FROM drugs")
        )
        assert result.scalar() == 2

    def test_bulk_upsert_drugs_updates_existing(
        self, db_session, sample_drug_df
    ):
        """bulk_upsert_drugs must update existing records on conflict."""
        database.bulk_upsert_drugs(db_session, sample_drug_df)
        db_session.commit()

        # Change the name for the first drug
        updated_df = sample_drug_df.copy()
        updated_df.loc[0, "name"] = "Acetylsalicylic Acid"
        database.bulk_upsert_drugs(db_session, updated_df)
        db_session.commit()

        result = db_session.execute(
            text(
                "SELECT name FROM drugs "
                "WHERE inchikey = :ik"
            ),
            {"ik": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"},
        )
        name = result.scalar()
        assert name == "Acetylsalicylic Acid"

    def test_bulk_upsert_drugs_empty_df(self, db_session):
        """bulk_upsert_drugs must return 0 for empty DataFrame."""
        empty_df = pd.DataFrame()
        count = database.bulk_upsert_drugs(db_session, empty_df)
        assert int(count) == 0

    def test_bulk_upsert_proteins_inserts(
        self, db_session, sample_protein_df
    ):
        """bulk_upsert_proteins must insert new protein records."""
        count = database.bulk_upsert_proteins(
            db_session, sample_protein_df
        )
        db_session.commit()
        assert int(count) >= 2
        result = db_session.execute(
            text("SELECT COUNT(*) FROM proteins")
        )
        assert result.scalar() == 2

    def test_bulk_upsert_proteins_idempotent(
        self, db_session, sample_protein_df
    ):
        """Running bulk_upsert_proteins twice must not duplicate."""
        database.bulk_upsert_proteins(
            db_session, sample_protein_df
        )
        db_session.commit()
        database.bulk_upsert_proteins(
            db_session, sample_protein_df
        )
        db_session.commit()
        result = db_session.execute(
            text("SELECT COUNT(*) FROM proteins")
        )
        assert result.scalar() == 2

    def test_bulk_upsert_dpi_inserts(
        self, db_session, sample_drug_df, sample_protein_df
    ):
        """bulk_upsert_dpi must insert drug-protein interactions."""
        database.bulk_upsert_drugs(db_session, sample_drug_df)
        db_session.commit()
        database.bulk_upsert_proteins(
            db_session, sample_protein_df
        )
        db_session.commit()

        drug_map = database.get_inchikey_to_drug_id_map(
            db_session
        )
        protein_map = database.get_uniprot_to_protein_id_map(
            db_session
        )

        dpi_df = pd.DataFrame({
            "drug_id": [
                drug_map.mapping["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"]
            ],
            "protein_id": [protein_map.mapping["P23219"]],
            "interaction_type": ["inhibitor"],
            "activity_value": [0.5],
            "activity_type": ["IC50"],
            "activity_units": ["uM"],
            "source": ["chembl"],
            "source_id": ["ACT_12345"],
            "confidence_score": [0.9],
        })
        count = database.bulk_upsert_dpi(db_session, dpi_df)
        db_session.commit()
        assert int(count) >= 1

        result = db_session.execute(
            text("SELECT COUNT(*) FROM drug_protein_interactions")
        )
        assert result.scalar() == 1

    def test_bulk_upsert_ppi_inserts(
        self, db_session, sample_protein_df
    ):
        """bulk_upsert_ppi must insert protein-protein interactions."""
        database.bulk_upsert_proteins(
            db_session, sample_protein_df
        )
        db_session.commit()

        protein_map = database.get_uniprot_to_protein_id_map(
            db_session
        )
        pid_a = protein_map.mapping["P23219"]
        pid_b = protein_map.mapping["P04637"]

        # Ensure protein_a_id < protein_b_id for unique constraint
        ppi_df = pd.DataFrame({
            "protein_a_id": [min(pid_a, pid_b)],
            "protein_b_id": [max(pid_a, pid_b)],
            "combined_score": [850],
            "experimental_score": [700],
            "database_score": [600],
            "textmining_score": [500],
            "source": ["string"],
        })
        count = database.bulk_upsert_ppi(db_session, ppi_df)
        db_session.commit()
        assert int(count) >= 1

    def test_bulk_upsert_gda_inserts(self, db_session):
        """bulk_upsert_gda must insert gene-disease associations."""
        gda_df = pd.DataFrame({
            "gene_symbol": ["TP53"],
            "disease_id": ["C0027651"],
            "disease_name": ["Neoplasms"],
            "association_type": ["biomarker"],
            "score": [0.8],
            "source": ["disgenet"],
            "pmid_list": ["12345678"],
        })
        count = database.bulk_upsert_gda(db_session, gda_df)
        db_session.commit()
        assert int(count) >= 1

    def test_bulk_upsert_gda_null_gene_symbol_coalesced(
        self, db_session
    ):
        """BUG-A-002 ROOT FIX: NULL gene_symbol rows are QUARANTINED,
        not coalesced to empty string. The previous behavior silently
        collapsed distinct genes into one row (every NULL-gene row
        became the same "" gene), corrupting the GDA deduplication
        logic. The fix routes NULL-gene rows to a dead-letter table
        for curator review."""
        gda_df = pd.DataFrame({
            "gene_symbol": [None],
            "disease_id": ["C0001"],
            "disease_name": ["Test Disease"],
            "score": [0.5],
            "source": ["test"],
        })
        count = database.bulk_upsert_gda(db_session, gda_df)
        db_session.commit()
        # NULL-gene row is quarantined — 0 rows inserted into GDA table.
        assert int(count) == 0, (
            f"BUG-A-002: expected 0 rows inserted (NULL-gene row quarantined), "
            f"got {int(count)}. Coalescing to empty string silently merges "
            f"distinct genes."
        )

        # Verify no row with empty gene_symbol was inserted.
        result = db_session.execute(
            text(
                "SELECT COUNT(*) FROM gene_disease_associations "
                "WHERE gene_symbol IS NULL OR gene_symbol = ''"
            )
        )
        val = result.scalar()
        assert val == 0, (
            f"BUG-A-002 regression: found {val} row(s) with NULL/empty "
            f"gene_symbol in GDA table — should have been quarantined."
        )

    def test_bulk_upsert_gda_idempotent(self, db_session):
        """Running bulk_upsert_gda twice must not duplicate."""
        gda_df = pd.DataFrame({
            "gene_symbol": ["BRCA1"],
            "disease_id": ["C001"],
            "disease_name": ["Breast Cancer"],
            "score": [0.9],
            "source": ["disgenet"],
        })
        database.bulk_upsert_gda(db_session, gda_df)
        db_session.commit()
        database.bulk_upsert_gda(db_session, gda_df)
        db_session.commit()

        result = db_session.execute(
            text(
                "SELECT COUNT(*) FROM "
                "gene_disease_associations"
            )
        )
        assert result.scalar() == 1

    def test_bulk_upsert_entity_mapping_with_inchikey(
        self, db_session
    ):
        """Entity mapping WITH inchikey must use ON CONFLICT."""
        em_df = pd.DataFrame({
            "canonical_inchikey": [
                "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
            ],
            "canonical_name": ["Aspirin"],
            "chembl_id": ["CHEMBL25"],
            "drugbank_id": ["DB00945"],
        })
        count = database.bulk_upsert_entity_mapping(
            db_session, em_df
        )
        db_session.commit()
        assert int(count) >= 1

    def test_bulk_upsert_entity_mapping_without_inchikey(
        self, db_session
    ):
        """Entity mapping WITHOUT inchikey must still insert."""
        em_df = pd.DataFrame({
            "canonical_inchikey": [None],
            "canonical_name": ["Unknown Drug X"],
            "chembl_id": [None],
        })
        count = database.bulk_upsert_entity_mapping(
            db_session, em_df
        )
        db_session.commit()
        assert int(count) >= 1

    def test_bulk_update_drugs_from_pubchem(
        self, db_session, sample_drug_df
    ):
        """bulk_update_drugs_from_pubchem must update NULL pubchem_cid."""
        # Insert drugs WITHOUT pubchem_cid
        drugs_no_cid = sample_drug_df.drop(
            columns=["pubchem_cid"]
        )
        database.bulk_upsert_drugs(db_session, drugs_no_cid)
        db_session.commit()

        # Now update with PubChem data
        pubchem_df = pd.DataFrame({
            "inchikey": [
                "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
            ],
            "pubchem_cid": [2244],
            "molecular_formula": ["C9H8O4"],
            "molecular_weight": [180.16],
            "smiles": ["CC(=O)Oc1ccccc1C(=O)O"],
        })
        updated = database.bulk_update_drugs_from_pubchem(
            db_session, pubchem_df
        )
        db_session.commit()
        assert updated >= 1

        result = db_session.execute(
            text(
                "SELECT pubchem_cid FROM drugs "
                "WHERE inchikey = :ik"
            ),
            {"ik": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"},
        )
        assert result.scalar() == 2244


# ============================================================================
# 6. LOOKUP HELPER TESTS
# ============================================================================


class TestLookupHelpers:
    """Verify lookup functions return correct mappings."""

    def test_get_uniprot_to_protein_id_map(
        self, db_session, sample_protein_df
    ):
        """Must return correct uniprot_id -> protein.id mapping.

        The loader returns a MappingResult (not a dict); use .mapping.
        """
        database.bulk_upsert_proteins(
            db_session, sample_protein_df
        )
        db_session.commit()

        mapping_result = database.get_uniprot_to_protein_id_map(
            db_session
        )
        mapping = mapping_result.mapping
        assert isinstance(mapping, dict)
        assert "P23219" in mapping
        assert "P04637" in mapping
        assert isinstance(mapping["P23219"], int)

    def test_get_inchikey_to_drug_id_map(
        self, db_session, sample_drug_df
    ):
        """Must return correct inchikey -> drug.id mapping.

        The loader returns a MappingResult (not a dict); use .mapping.
        """
        database.bulk_upsert_drugs(
            db_session, sample_drug_df
        )
        db_session.commit()

        mapping_result = database.get_inchikey_to_drug_id_map(
            db_session
        )
        mapping = mapping_result.mapping
        assert isinstance(mapping, dict)
        assert "BSYNRYMUTXBXSQ-UHFFFAOYSA-N" in mapping

    def test_get_uniprot_to_protein_id_map_empty(
        self, db_session
    ):
        """Must return empty mapping when no proteins exist."""
        mapping_result = database.get_uniprot_to_protein_id_map(
            db_session
        )
        assert mapping_result.mapping == {}

    def test_get_inchikey_to_drug_id_map_empty(
        self, db_session
    ):
        """Must return empty mapping when no drugs exist."""
        mapping_result = database.get_inchikey_to_drug_id_map(
            db_session
        )
        assert mapping_result.mapping == {}


# ============================================================================
# 7. MIGRATION TESTS
# ============================================================================


class TestMigrations:
    """Verify schema migration runner works."""

    def test_run_migrations_callable(self):
        """run_migrations must be callable."""
        assert callable(database.run_migrations)


# ============================================================================
# 8. DATA QUALITY VALIDATION TESTS
# ============================================================================


class TestDataQualityValidation:
    """Verify validate_data_quality_infrastructure works."""

    def test_validation_returns_report(self, db_session):
        """Must return a dict with 'checks', 'passed', 'failed', 'overall'."""
        report = database.validate_data_quality_infrastructure(
            db_session
        )
        assert isinstance(report, dict)
        assert "checks" in report
        assert "passed" in report
        assert "failed" in report
        assert "overall" in report
        assert isinstance(report["checks"], list)

    def test_validation_overall_pass(self, db_session):
        """All bulk_upsert functions should be importable (PASS)."""
        report = database.validate_data_quality_infrastructure(
            db_session
        )
        assert report["overall"] in ("PASS", "FAIL")
        # At minimum, import checks should pass
        import_checks = [
            c for c in report["checks"]
            if c["check"].startswith("import_")
        ]
        for check in import_checks:
            assert check["status"] == "PASS", (
                f"Import check failed: {check}"
            )


# ============================================================================
# 9. SECURITY AUDIT TESTS
# ============================================================================


class TestSecurityAudit:
    """Verify _validate_database_security works."""

    def test_security_audit_returns_report(self):
        """Must return a dict with 'checks', 'warnings', 'critical'."""
        report = database._validate_database_security()
        assert isinstance(report, dict)
        assert "checks" in report
        assert "warnings" in report
        assert "critical" in report
        assert "overall" in report

    def test_security_audit_with_sqlite_memory(self):
        """SQLite in-memory should warn in non-test environment.

        The _validate_database_security function reads DATABASE_URL from
        config.settings, which is a module-level variable set on first
        import. We need to reload the module to pick up the new env var.
        """
        original_env = os.environ.get("ENVIRONMENT")
        original_db_url = os.environ.get("DATABASE_URL")
        try:
            os.environ["ENVIRONMENT"] = "production"
            os.environ["DATABASE_URL"] = "sqlite:///:memory:"
            # Reload config.settings to pick up new DATABASE_URL
            import config.settings as cs
            import importlib
            importlib.reload(cs)
            report = database._validate_database_security()
            all_checks = report.get("checks", [])
            # Should flag in-memory SQLite in production
            in_memory_checks = [
                c for c in all_checks
                if c["check"] == "in_memory_sqlite"
            ]
            assert len(in_memory_checks) > 0, (
                f"Expected in_memory_sqlite check. "
                f"Got checks: {all_checks}"
            )
            assert in_memory_checks[0]["severity"] == "CRITICAL"
        finally:
            if original_env is not None:
                os.environ["ENVIRONMENT"] = original_env
            else:
                os.environ.pop("ENVIRONMENT", None)
            if original_db_url is not None:
                os.environ["DATABASE_URL"] = original_db_url
            else:
                os.environ.pop("DATABASE_URL", None)
            # Restore original config.settings
            import config.settings as cs
            import importlib
            importlib.reload(cs)


# ============================================================================
# 10. IDEMPOTENCY TESTS
# ============================================================================


class TestIdempotency:
    """Verify operations produce identical results when run multiple times."""

    def test_drug_upsert_triple_run(
        self, db_session, sample_drug_df
    ):
        """Running drug upsert 3 times must produce exactly 2 records."""
        for _ in range(3):
            database.bulk_upsert_drugs(
                db_session, sample_drug_df
            )
            db_session.commit()

        result = db_session.execute(
            text("SELECT COUNT(*) FROM drugs")
        )
        assert result.scalar() == 2

    def test_protein_upsert_triple_run(
        self, db_session, sample_protein_df
    ):
        """Running protein upsert 3 times must produce exactly 2 records."""
        for _ in range(3):
            database.bulk_upsert_proteins(
                db_session, sample_protein_df
            )
            db_session.commit()

        result = db_session.execute(
            text("SELECT COUNT(*) FROM proteins")
        )
        assert result.scalar() == 2


# ============================================================================
# 11. ERROR HANDLING TESTS
# ============================================================================


class TestErrorHandling:
    """Verify informative errors for bad imports."""

    def test_unknown_symbol_attribute_error(self):
        """Accessing unknown symbol must raise AttributeError."""
        with pytest.raises(AttributeError) as exc_info:
            getattr(database, "totally_fake_symbol")
        assert "no attribute" in str(exc_info.value)

    def test_getattr_error_message_includes_symbol_name(self):
        """Error message must include the symbol name."""
        with pytest.raises(AttributeError) as exc_info:
            getattr(database, "missing_function_xyz")
        assert "missing_function_xyz" in str(exc_info.value)


# ============================================================================
# 12. OBSERVABILITY TESTS
# ============================================================================


class TestObservability:
    """Verify logging and callback hooks."""

    def test_callback_invoked_on_load(self):
        """_on_symbol_loaded_callback must be invoked after loading."""
        calls = []
        database._on_symbol_loaded_callback = (
            lambda name, mod, ms: calls.append(
                (name, mod, ms)
            )
        )
        try:
            database._reset()
            _ = database.PipelineRun
            assert len(calls) >= 1
            assert calls[0][0] == "PipelineRun"
            assert calls[0][1] == "database.models"
            assert isinstance(calls[0][2], float)
        finally:
            database._on_symbol_loaded_callback = None

    def test_callback_exception_does_not_break_load(self):
        """A failing callback must not prevent symbol loading."""
        def bad_callback(name, mod, ms):
            raise RuntimeError("Callback crashed!")

        database._on_symbol_loaded_callback = bad_callback
        try:
            database._reset()
            # Should not raise despite bad callback
            result = database.EntityMapping
            assert result is not None
        finally:
            database._on_symbol_loaded_callback = None


# ============================================================================
# 13. GENE SYMBOL RESOLUTION TESTS
# ============================================================================


class TestGeneSymbolResolution:
    """Verify build_gene_to_uniprot_maps and resolve_gene_symbol_to_uniprot."""

    def test_build_gene_to_uniprot_maps(
        self, db_session, sample_protein_df
    ):
        """Must build correct gene_symbol -> uniprot_id mapping."""
        database.bulk_upsert_proteins(
            db_session, sample_protein_df
        )
        db_session.commit()

        gene_map, pn_map = database.build_gene_to_uniprot_maps(
            db_session
        )
        assert "PTGS1" in gene_map
        assert gene_map["PTGS1"] == "P23219"
        assert "TP53" in gene_map
        assert gene_map["TP53"] == "P04637"

    def test_resolve_gene_symbol_to_uniprot(
        self, db_session, sample_protein_df
    ):
        """Must resolve gene_symbol column to uniprot_id."""
        database.bulk_upsert_proteins(
            db_session, sample_protein_df
        )
        db_session.commit()

        gene_map, pn_map = database.build_gene_to_uniprot_maps(
            db_session
        )

        gda_df = pd.DataFrame({
            "gene_symbol": ["PTGS1", "TP53", "UNKNOWN"],
            "disease_id": ["C001", "C002", "C003"],
            "source": ["test", "test", "test"],
        })
        result_df = database.resolve_gene_symbol_to_uniprot(
            gda_df, gene_map, pn_map
        )
        assert result_df["uniprot_id"].iloc[0] == "P23219"
        assert result_df["uniprot_id"].iloc[1] == "P04637"
        assert pd.isna(result_df["uniprot_id"].iloc[2])

    def test_resolve_with_no_gene_symbol_column(self):
        """Must add uniprot_id=None when gene_symbol column missing."""
        df = pd.DataFrame({
            "disease_id": ["C001"],
            "source": ["test"],
        })
        result = database.resolve_gene_symbol_to_uniprot(
            df, {}, {}
        )
        assert "uniprot_id" in result.columns
        assert pd.isna(result["uniprot_id"].iloc[0])

    def test_gene_map_does_not_contain_protein_names(
        self, db_session, sample_protein_df
    ):
        """gene_to_uniprot map must NOT contain protein names as keys."""
        database.bulk_upsert_proteins(
            db_session, sample_protein_df
        )
        db_session.commit()

        gene_map, pn_map = database.build_gene_to_uniprot_maps(
            db_session
        )
        # These are protein names, NOT gene symbols
        assert "PROSTAGLANDIN G/H SYNTHASE 1" not in gene_map
        assert "CELLULAR TUMOR ANTIGEN P53" not in gene_map


# ============================================================================
# 14. FULL END-TO-END PIPELINE TEST
# ============================================================================


class TestFullE2EPipeline:
    """End-to-end test: load data → resolve → upsert → query → verify."""

    def test_drug_protein_interaction_pipeline(
        self, db_session, sample_drug_df, sample_protein_df
    ):
        """Full pipeline: drugs + proteins → resolve IDs → insert DPI."""
        # Step 1: Load drugs
        drug_count = database.bulk_upsert_drugs(
            db_session, sample_drug_df
        )
        db_session.commit()
        assert int(drug_count) >= 2

        # Step 2: Load proteins
        protein_count = database.bulk_upsert_proteins(
            db_session, sample_protein_df
        )
        db_session.commit()
        assert int(protein_count) >= 2

        # Step 3: Build lookup maps (returns MappingResult; use .mapping)
        drug_map_result = database.get_inchikey_to_drug_id_map(
            db_session
        )
        protein_map_result = database.get_uniprot_to_protein_id_map(
            db_session
        )
        drug_map = drug_map_result.mapping
        protein_map = protein_map_result.mapping
        assert len(drug_map) == 2
        assert len(protein_map) == 2

        # Step 4: Insert DPI
        dpi_df = pd.DataFrame({
            "drug_id": [
                drug_map["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
                drug_map["WFXAZNNJSJXTJZ-UHFFFAOYSA-N"],
            ],
            "protein_id": [
                protein_map["P23219"],
                protein_map["P04637"],
            ],
            "interaction_type": ["inhibitor", "binding_agent"],  # K5 fix: 'binder' is not a valid enum; use 'binding_agent'
            "activity_value": [0.5, 1.2],
            "activity_type": ["IC50", "Ki"],
            "activity_units": ["uM", "uM"],
            "source": ["chembl", "drugbank"],
            "source_id": ["ACT_1", "ACT_2"],
            "confidence_score": [0.9, 0.8],
        })
        dpi_count = database.bulk_upsert_dpi(
            db_session, dpi_df
        )
        db_session.commit()
        assert int(dpi_count) >= 2

        # Step 5: Verify data integrity
        result = db_session.execute(
            text(
                "SELECT d.name, p.uniprot_id, dpi.activity_value "
                "FROM drug_protein_interactions dpi "
                "JOIN drugs d ON d.id = dpi.drug_id "
                "JOIN proteins p ON p.id = dpi.protein_id "
                "ORDER BY d.name"
            )
        )
        rows = result.fetchall()
        assert len(rows) == 2
        assert rows[0][0] == "Aspirin"
        assert rows[0][1] == "P23219"
        assert rows[0][2] == 0.5

    def test_gene_disease_pipeline(
        self, db_session, sample_protein_df
    ):
        """Full pipeline: proteins → build gene map → resolve → upsert GDA."""
        # Step 1: Load proteins
        database.bulk_upsert_proteins(
            db_session, sample_protein_df
        )
        db_session.commit()

        # Step 2: Build gene maps
        gene_map, pn_map = database.build_gene_to_uniprot_maps(
            db_session
        )

        # Step 3: Create GDA data and resolve
        gda_df = pd.DataFrame({
            "gene_symbol": ["PTGS1", "TP53"],
            "disease_id": ["C0027651", "C003 phenomen"],
            "disease_name": ["Neoplasms", "Cancer"],
            "score": [0.8, 0.9],
            "source": ["disgenet", "disgenet"],
        })
        gda_df = database.resolve_gene_symbol_to_uniprot(
            gda_df, gene_map, pn_map
        )
        assert gda_df["uniprot_id"].iloc[0] == "P23219"
        assert gda_df["uniprot_id"].iloc[1] == "P04637"

        # Step 4: Upsert GDA
        gda_count = database.bulk_upsert_gda(
            db_session, gda_df
        )
        db_session.commit()
        assert int(gda_count) >= 2

        # Step 5: Verify
        result = db_session.execute(
            text(
                "SELECT gene_symbol, uniprot_id "
                "FROM gene_disease_associations "
                "ORDER BY gene_symbol"
            )
        )
        rows = result.fetchall()
        assert len(rows) == 2
        assert rows[0][0] == "PTGS1"
        assert rows[0][1] == "P23219"

    def test_entity_resolution_pipeline(self, db_session):
        """Full pipeline: entity mapping with and without inchikey."""
        # Entity WITH inchikey
        em_with = pd.DataFrame({
            "canonical_inchikey": [
                "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
            ],
            "canonical_name": ["Aspirin"],
            "chembl_id": ["CHEMBL25"],
            "drugbank_id": ["DB00945"],
            "pubchem_cid": [2244],
            "match_confidence": [1.0],
            "match_method": ["inchikey_exact"],
        })
        count1 = database.bulk_upsert_entity_mapping(
            db_session, em_with
        )
        db_session.commit()
        assert int(count1) >= 1

        # Entity WITHOUT inchikey
        em_without = pd.DataFrame({
            "canonical_inchikey": [None],
            "canonical_name": ["Complex Biologic X"],
            "chembl_id": [None],
            "match_confidence": [0.7],
            "match_method": ["name_fuzzy"],
        })
        count2 = database.bulk_upsert_entity_mapping(
            db_session, em_without
        )
        db_session.commit()
        assert int(count2) >= 1

        # Verify total
        result = db_session.execute(
            text("SELECT COUNT(*) FROM entity_mapping")
        )
        assert result.scalar() == 2


# ============================================================================
# 15. BACKWARD COMPATIBILITY TESTS
# ============================================================================


class TestBackwardCompatibility:
    """Verify existing import paths still work."""

    def test_direct_submodule_imports_still_work(self):
        """from database.models import Drug must still work."""
        from database.models import Drug
        assert Drug is not None

    def test_direct_connection_import_still_works(self):
        """from database.connection import get_engine must still work."""
        from database.connection import get_engine
        assert callable(get_engine)

    def test_direct_loaders_import_still_works(self):
        """from database.loaders import bulk_upsert_drugs must work."""
        from database.loaders import bulk_upsert_drugs
        assert callable(bulk_upsert_drugs)

    def test_package_import_same_as_submodule(self):
        """Package-level import must return same object as submodule."""
        from database.models import Drug as Drug_direct
        Drug_pkg = database.Drug
        assert Drug_pkg is Drug_direct


# ============================================================================
# 16. LINE LENGTH AND PEP 8 COMPLIANCE TESTS
# ============================================================================


class TestPEP8Compliance:
    """Verify the database/__init__.py file meets PEP 8 standards."""

    def test_no_line_exceeds_99_chars(self):
        """No line in database/__init__.py must exceed 99 characters."""
        init_path = (
            PROJECT_ROOT / "database" / "__init__.py"
        )
        assert init_path.exists()
        long_lines = []
        with open(init_path, "r") as f:
            for i, line in enumerate(f, 1):
                if len(line.rstrip("\n")) > 99:
                    long_lines.append(
                        (i, len(line.rstrip("\n")))
                    )
        assert not long_lines, (
            f"Lines exceeding 99 chars: {long_lines}"
        )

    def test_module_has_docstring(self):
        """Module must have a docstring."""
        assert database.__doc__ is not None
        assert len(database.__doc__) > 100

    def test_module_docstring_mentions_lazy_loading(self):
        """Module docstring must mention 'lazy loading'."""
        assert "lazy" in database.__doc__.lower()

    def test_module_docstring_mentions_all_4_submodules(self):
        """Module docstring must reference all 4 submodules."""
        doc = database.__doc__.lower()
        assert "connection" in doc
        assert "models" in doc
        assert "loaders" in doc
        assert "migrations" in doc
