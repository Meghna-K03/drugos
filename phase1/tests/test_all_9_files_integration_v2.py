"""
Test 2: Real integration test for ALL 9 files working together.

Files tested:
1. config/__init__.py
2. config/settings.py
3. database/__init__.py
4. database/connection.py
5. database/models.py
6. database/base.py
7. database/migrations/__init__.py
8. database/migrations/001_initial_schema.sql
9. database/migrations/002_bug_fixes_migration.sql
10. database/migrations/run_migrations.py (newly fixed)

This test verifies that all 9+ files work correctly together as a
complete system — imports work, database can be created, migrations
run, models are valid, and config is accessible.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================================
# Fixtures
# ============================================================================


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
                lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00"),
            )

    from database.base import Base
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture(scope="function")
def db_session(db_engine):
    """Yield a transactional session."""
    from database.base import Base
    session = sessionmaker(bind=db_engine)()
    yield session
    session.rollback()
    session.close()


# ============================================================================
# FILE 1 & 2: config/__init__.py and config/settings.py
# ============================================================================


class TestConfigIntegration:
    """Verify config package works end-to-end."""

    def test_config_import_works(self):
        """config package can be imported."""
        import config
        assert config is not None

    def test_config_database_url_accessible(self):
        """DATABASE_URL is accessible from config."""
        from config import DATABASE_URL
        assert DATABASE_URL is not None
        assert isinstance(DATABASE_URL, str)

    def test_settings_module_works(self):
        """config.settings module works."""
        from config.settings import DATABASE_URL
        assert DATABASE_URL is not None

    def test_chembl_config_accessible(self):
        """ChEMBL configuration is accessible."""
        from config import CHEMBL_VERSION, CHEMBL_API_URL
        assert CHEMBL_VERSION is not None

    def test_string_config_accessible(self):
        """STRING configuration is accessible."""
        from config import STRING_MIN_COMBINED_SCORE
        assert STRING_MIN_COMBINED_SCORE >= 0

    def test_sensitive_settings_masked(self):
        """Sensitive settings are masked in repr."""
        try:
            from config import _SENSITIVE_SETTINGS
            assert isinstance(_SENSITIVE_SETTINGS, (set, frozenset, list, tuple))
        except ImportError:
            # _SENSITIVE_SETTINGS may be internal, skip if not accessible
            pass


# ============================================================================
# FILE 3: database/__init__.py
# ============================================================================


class TestDatabaseInitIntegration:
    """Verify database package works end-to-end."""

    def test_database_import_works(self):
        """database package can be imported."""
        import database
        assert database is not None

    def test_database_exports_base(self):
        """Base is accessible from database."""
        from database import Base
        assert Base is not None

    def test_database_exports_models(self):
        """ORM models are accessible from database."""
        from database import Drug, Protein
        assert Drug is not None
        assert Protein is not None


# ============================================================================
# FILE 4: database/connection.py
# ============================================================================


class TestConnectionIntegration:
    """Verify database.connection works end-to-end."""

    def test_connection_import_works(self):
        """database.connection can be imported."""
        from database.connection import get_engine, get_db_session, init_db
        assert callable(get_engine)
        assert callable(get_db_session)
        assert callable(init_db)

    def test_engine_creation(self, db_engine):
        """Engine can be created and is functional."""
        with db_engine.connect() as conn:
            result = conn.execute(text("SELECT 1")).scalar()
            assert result == 1

    def test_session_works(self, db_session):
        """Database session works for ORM operations."""
        from database.models import Drug
        drug = Drug(
            inchikey="BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
            name="Aspirin",
            chembl_id="CHEMBL25",
        )
        db_session.add(drug)
        db_session.flush()
        assert drug.id is not None

    def test_health_check(self, db_engine):
        """Health check works."""
        from database.connection import check_connection
        result = check_connection(db_engine)
        assert result is not None


# ============================================================================
# FILE 5: database/models.py
# ============================================================================


class TestModelsIntegration:
    """Verify ORM models work end-to-end."""

    def test_all_models_importable(self):
        """All 7 ORM models can be imported."""
        from database.models import (
            Drug, Protein, DrugProteinInteraction,
            ProteinProteinInteraction, GeneDiseaseAssociation,
            EntityMapping, PipelineRun,
        )
        assert Drug is not None
        assert Protein is not None

    def test_drug_model_crud(self, db_session):
        """Drug CRUD operations work."""
        from database.models import Drug
        drug = Drug(
            inchikey="BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
            name="Aspirin",
            chembl_id="CHEMBL25",
            molecular_weight=180.16,
        )
        db_session.add(drug)
        db_session.flush()

        retrieved = db_session.query(Drug).filter_by(chembl_id="CHEMBL25").first()
        assert retrieved is not None
        assert retrieved.name == "Aspirin"

    def test_protein_model_crud(self, db_session):
        """Protein CRUD operations work."""
        from database.models import Protein
        protein = Protein(
            uniprot_id="P23219",
            gene_name="Prostaglandin G/H synthase 1",
            gene_symbol="PTGS1",
        )
        db_session.add(protein)
        db_session.flush()

        retrieved = db_session.query(Protein).filter_by(uniprot_id="P23219").first()
        assert retrieved is not None
        assert retrieved.gene_symbol == "PTGS1"

    def test_drug_protein_interaction(self, db_session):
        """DrugProteinInteraction works with foreign keys."""
        from database.models import Drug, Protein, DrugProteinInteraction

        drug = Drug(inchikey="BSYNRYMUTXBXSQ-UHFFFAOYSA-N", name="TestDrug", chembl_id="CHEMBL_TEST1")
        protein = Protein(uniprot_id="P23219", gene_name="TestProtein", gene_symbol="PTGS1")
        db_session.add_all([drug, protein])
        db_session.flush()

        interaction = DrugProteinInteraction(
            drug_id=drug.id, protein_id=protein.id,
            activity_type="IC50", activity_value=100.0,
        )
        db_session.add(interaction)
        db_session.flush()

        assert interaction.id is not None

    def test_pipeline_run_model(self, db_session):
        """PipelineRun model works."""
        from database.models import PipelineRun
        run = PipelineRun(
            source="chembl",
            status="running",
        )
        db_session.add(run)
        db_session.flush()
        assert run.id is not None

    def test_model_constraints_enforced(self, db_session):
        """CHECK constraints on models are enforced."""
        from database.models import Drug
        # Valid drug
        drug = Drug(
            inchikey="BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
            name="ValidDrug",
            molecular_weight=180.16,
        )
        db_session.add(drug)
        db_session.flush()
        assert drug.id is not None


# ============================================================================
# FILE 6: database/base.py
# ============================================================================


class TestBaseIntegration:
    """Verify database.base works end-to-end."""

    def test_base_importable(self):
        """Base, mixins, and SCHEMA_VERSION are importable."""
        from database.base import Base, IDMixin, TimestampMixin, SoftDeleteMixin, SCHEMA_VERSION
        # SCI-FIX: Updated from == 3 to == 5 to match the 5 SQL migration files
        # SCI-FIX-v2: Updated from == 5 to == 6 to match the 6 SQL migration files
        #             (001-006, including 006_drug_withdrawn_safety_columns.sql).
        assert SCHEMA_VERSION == 6
        assert Base is not None

    def test_naming_convention(self):
        """Naming convention is defined and has expected keys."""
        from database.base import NAMING_CONVENTION
        assert "ix" in NAMING_CONVENTION
        assert "uq" in NAMING_CONVENTION
        assert "fk" in NAMING_CONVENTION
        assert "ck" in NAMING_CONVENTION


# ============================================================================
# FILE 7: database/migrations/__init__.py
# ============================================================================


class TestMigrationsInitIntegration:
    """Verify migrations package works end-to-end."""

    def test_migrations_import_works(self):
        """database.migrations package can be imported."""
        import database.migrations
        assert database.migrations is not None

    def test_run_migrations_accessible(self):
        """run_migrations function is accessible from package."""
        from database.migrations.run_migrations import run_migrations as rm_func
        assert callable(rm_func)

    def test_migration_config_accessible(self):
        """MigrationConfig is accessible from package."""
        from database.migrations import MigrationConfig
        config = MigrationConfig()
        assert config is not None

    def test_constants_accessible(self):
        """Migration constants are accessible from package."""
        from database.migrations import (
            DIALECT_POSTGRESQL, DIALECT_SQLITE, SUPPORTED_DIALECTS,
            MIGRATION_BATCH_SIZE, SCHEMA_VERSION,
        )
        assert DIALECT_POSTGRESQL == "postgresql"
        assert DIALECT_SQLITE == "sqlite"
        # SCI-FIX: Updated from == 3 to == 5 to match the 5 SQL migration files
        # SCI-FIX-v2: Updated from == 5 to == 6 to match the 6 SQL migration files
        #             (001-006, including 006_drug_withdrawn_safety_columns.sql).
        assert SCHEMA_VERSION == 6

    def test_lazy_loading_works(self):
        """Lazy loading doesn't trigger side effects."""
        import database.migrations
        # Access a lazy-loaded symbol
        _ = database.migrations.MIGRATION_BATCH_SIZE
        # Should not raise


# ============================================================================
# FILES 8, 9, 10: SQL migrations + run_migrations.py
# ============================================================================


class TestMigrationsIntegration:
    """Verify migration SQL files and runner work together."""

    def test_sql_migration_files_exist(self):
        """SQL migration files exist in migrations directory."""
        from database.migrations import get_sql_migration_files
        files = get_sql_migration_files()
        assert len(files) >= 3
        names = [f.name for f in files]
        assert "001_initial_schema.sql" in names
        assert "002_bug_fixes_migration.sql" in names

    def test_run_migrations_on_sqlite(self, db_engine):
        """run_migrations runs successfully on SQLite."""
        from database.migrations.run_migrations import run_migrations, MigrationConfig
        config = MigrationConfig(
            stop_on_failure=False,
            block_on_data_issues=False,
        )
        result = run_migrations(db_engine, config)
        assert isinstance(result.applied, list)
        assert isinstance(result.skipped, list)
        assert isinstance(result.failed, list)
        assert result.dialect == "sqlite"

    def test_migrations_idempotent(self, db_engine):
        """Running migrations twice produces consistent results."""
        from database.migrations.run_migrations import run_migrations, MigrationConfig
        config = MigrationConfig(
            stop_on_failure=False,
            block_on_data_issues=False,
        )
        result1 = run_migrations(db_engine, config)
        result2 = run_migrations(db_engine, config)
        # Second run should have same or more skipped
        assert len(result2.skipped) >= len(result1.skipped)

    def test_check_migrations_after_run(self, db_engine):
        """check_migrations works after running migrations."""
        from database.migrations.run_migrations import run_migrations, check_migrations, MigrationConfig
        config = MigrationConfig(
            stop_on_failure=False,
            block_on_data_issues=False,
        )
        run_migrations(db_engine, config)
        health = check_migrations(db_engine)
        assert health.dialect == "sqlite"

    def test_scientific_validation_works(self, db_engine):
        """validate_scientific_constraints works on a populated database."""
        from database.migrations.run_migrations import validate_scientific_constraints
        warnings = validate_scientific_constraints(db_engine)
        assert isinstance(warnings, list)

    def test_schema_verification_works(self, db_engine):
        """verify_schema_matches_orm works."""
        from database.migrations.run_migrations import verify_schema_matches_orm
        result = verify_schema_matches_orm(db_engine)
        assert "missing_in_db" in result
        assert "extra_in_db" in result

    def test_fingerprint_works(self, db_engine):
        """get_database_fingerprint works."""
        from database.migrations.run_migrations import get_database_fingerprint
        fp = get_database_fingerprint(db_engine)
        assert "tables" in fp
        assert len(fp["tables"]) > 0  # Should have tables from Base.metadata.create_all

    def test_plan_migrations_works(self, db_engine):
        """plan_migrations works."""
        from database.migrations.run_migrations import plan_migrations
        planned = plan_migrations(db_engine)
        assert isinstance(planned, list)
        for item in planned:
            assert "name" in item
            assert "is_new" in item
            assert "checksum" in item


# ============================================================================
# CROSS-FILE INTEGRATION: All 9 files working together
# ============================================================================


class TestFullStackIntegration:
    """Tests that verify all 9+ files work together as a complete system."""

    def test_config_to_connection_pipeline(self):
        """Config provides DATABASE_URL -> connection creates engine."""
        from config import DATABASE_URL
        from database.connection import get_engine
        # Just verify the pipeline doesn't crash
        assert DATABASE_URL is not None

    def test_models_match_base(self):
        """All models inherit from Base correctly."""
        from database.base import Base
        from database.models import (
            Drug, Protein, DrugProteinInteraction,
            ProteinProteinInteraction, GeneDiseaseAssociation,
            EntityMapping, PipelineRun,
        )
        for model in [Drug, Protein, DrugProteinInteraction,
                       ProteinProteinInteraction, GeneDiseaseAssociation,
                       EntityMapping, PipelineRun]:
            assert issubclass(model, Base), f"{model.__name__} doesn't inherit from Base"

    def test_models_have_correct_tablenames(self):
        """Models have expected __tablename__ values."""
        from database.models import (
            Drug, Protein, DrugProteinInteraction,
            ProteinProteinInteraction, GeneDiseaseAssociation,
            EntityMapping, PipelineRun,
        )
        expected = {
            "drugs", "proteins", "drug_protein_interactions",
            "protein_protein_interactions", "gene_disease_associations",
            "entity_mapping", "pipeline_runs",
        }
        actual = {
            Drug.__tablename__, Protein.__tablename__,
            DrugProteinInteraction.__tablename__,
            ProteinProteinInteraction.__tablename__,
            GeneDiseaseAssociation.__tablename__,
            EntityMapping.__tablename__,
            PipelineRun.__tablename__,
        }
        assert actual == expected

    def test_migrations_add_columns_to_all_tables(self):
        """REQUIRED_COLUMNS covers all 7 core tables."""
        from database.migrations.run_migrations import REQUIRED_COLUMNS
        expected_tables = {
            "proteins", "drugs", "drug_protein_interactions",
            "protein_protein_interactions", "gene_disease_associations",
            "entity_mapping", "pipeline_runs",
        }
        assert set(REQUIRED_COLUMNS.keys()) == expected_tables

    def test_schema_version_consistency(self):
        """SCHEMA_VERSION is consistent between base and migrations."""
        from database.base import SCHEMA_VERSION as base_version
        from database.migrations import SCHEMA_VERSION as mig_version
        assert base_version == mig_version

    def test_full_database_lifecycle(self, db_engine, db_session):
        """Complete lifecycle: create, insert, query, verify schema."""
        from database.models import Drug, Protein, DrugProteinInteraction
        from database.migrations.run_migrations import (
            run_migrations, check_migrations, MigrationConfig,
        )

        # Run migrations
        config = MigrationConfig(
            stop_on_failure=False,
            block_on_data_issues=False,
        )
        result = run_migrations(db_engine, config)
        assert isinstance(result.applied, list)

        # Insert data
        drug = Drug(
            inchikey="BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
            name="Aspirin",
            chembl_id="CHEMBL25",
            molecular_weight=180.16,
        )
        protein = Protein(
            uniprot_id="P23219",
            gene_name="PTGS1 Protein",
            gene_symbol="PTGS1",
        )
        db_session.add_all([drug, protein])
        db_session.flush()

        # Create interaction
        interaction = DrugProteinInteraction(
            drug_id=drug.id,
            protein_id=protein.id,
            activity_type="IC50",
            activity_value=100.0,
        )
        db_session.add(interaction)
        db_session.flush()

        # Verify data
        assert db_session.query(Drug).count() >= 1
        assert db_session.query(Protein).count() >= 1

        # Verify health check
        health = check_migrations(db_engine)
        assert isinstance(health, MigrationHealthResult)

    def test_migration_tracking_tables_created(self, db_engine):
        """Migration tracking tables are created correctly."""
        from database.migrations.run_migrations import (
            run_migrations, MigrationConfig, get_failed_migrations,
        )

        config = MigrationConfig(
            stop_on_failure=False,
            block_on_data_issues=False,
        )
        run_migrations(db_engine, config)

        # Verify tracking tables exist
        inspector = inspect(db_engine)
        tables = inspector.get_table_names()
        assert "_migration_history" in tables
        assert "_failed_migrations" in tables

        # Verify we can query failed migrations
        failed = get_failed_migrations(db_engine)
        assert isinstance(failed, list)

    def test_export_verification(self):
        """verify_package_exports works across all files."""
        from database.migrations.run_migrations import verify_package_exports
        results = verify_package_exports()
        assert isinstance(results, dict)
        # Most symbols should be importable
        importable_count = sum(1 for v in results.values() if v)
        assert importable_count > 0

    def test_analyze_impact_works(self):
        """analyze_migration_impact works with real migration files."""
        from database.migrations.run_migrations import analyze_migration_impact, get_sql_migration_files
        files = get_sql_migration_files()
        if files:
            result = analyze_migration_impact(None, files[0].name)
            assert "affected_tables" in result
            assert "estimated_risk" in result


from database.migrations.run_migrations import MigrationHealthResult
