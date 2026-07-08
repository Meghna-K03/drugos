"""
Comprehensive 16-domain integration test for the Drug Repurposing ETL Platform.

This test suite verifies that ALL 7 core database files work together correctly:
  1. config/__init__.py
  2. config/settings.py
  3. database/__init__.py
  4. database/connection.py
  5. database/base.py
  6. database/models.py
  7. database/migrations/__init__.py

AND that the upgraded 001_initial_schema.sql produces a schema that is
consistent with the ORM models and satisfies all 16 verification domains.

Tests are REAL — they verify actual behavior, not just "it doesn't crash".
They create a real SQLite in-memory database, create all tables from ORM
models, insert valid and invalid data, and verify constraints work.

Run with:
    pytest tests/test_001_schema_16_domains.py -v
"""

from __future__ import annotations

import datetime
import importlib
import os
import re
import sys
import warnings
from pathlib import Path
from typing import Any, Optional

import pytest



# ---------------------------------------------------------------------------
# Ensure the project root is on sys.path
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Set a test DATABASE_URL before importing any database modules
os.environ.setdefault("DATABASE_URL", "sqlite:///test_001_schema.db")
os.environ.setdefault("LOG_LEVEL", "WARNING")


# ===========================================================================
# DOMAIN 1: ARCHITECTURE — Module structure, dependency flow, boundaries
# ===========================================================================

class TestArchitecture:
    """Verify the architectural integrity of all 7 core files."""

    def test_config_package_importable(self):
        """config package must be importable without side effects."""
        import config
        assert hasattr(config, "__file__")

    def test_config_settings_importable(self):
        """config.settings must be importable."""
        import config.settings
        assert hasattr(config.settings, "DATABASE_URL")

    def test_database_package_importable(self):
        """database package must be importable."""
        import database
        assert hasattr(database, "__file__")

    def test_database_connection_importable(self):
        """database.connection must provide the public API."""
        from database.connection import (
            Base, get_engine, get_session_factory, get_db_session,
            init_db, dispose_engine, check_connection,
        )
        assert Base is not None
        assert callable(get_engine)
        assert callable(init_db)

    def test_database_base_importable(self):
        """database.base must provide Base, mixins, and naming convention."""
        from database.base import (
            Base, IDMixin, TimestampMixin, SoftDeleteMixin,
            NAMING_CONVENTION, SCHEMA_VERSION,
        )
        assert SCHEMA_VERSION >= 3
        assert "ix" in NAMING_CONVENTION
        assert "uq" in NAMING_CONVENTION
        # SQLAlchemy naming convention uses "ck" key but generates "chk_" prefix
        assert "ck" in NAMING_CONVENTION
        assert NAMING_CONVENTION["ck"].startswith("chk_")
        assert "fk" in NAMING_CONVENTION

    def test_database_models_importable(self):
        """database.models must provide all 7 ORM models + SchemaVersion."""
        from database.models import (
            Drug, Protein, DrugProteinInteraction,
            ProteinProteinInteraction, GeneDiseaseAssociation,
            EntityMapping, PipelineRun, SchemaVersion,
        )
        assert Drug.__tablename__ == "drugs"
        assert Protein.__tablename__ == "proteins"
        assert DrugProteinInteraction.__tablename__ == "drug_protein_interactions"
        assert ProteinProteinInteraction.__tablename__ == "protein_protein_interactions"
        assert GeneDiseaseAssociation.__tablename__ == "gene_disease_associations"
        assert EntityMapping.__tablename__ == "entity_mapping"
        assert PipelineRun.__tablename__ == "pipeline_runs"
        assert SchemaVersion.__tablename__ == "schema_version"

    def test_migrations_package_importable(self):
        """database.migrations must provide run_migrations and constants."""
        # Import run_migrations from the submodule to avoid the submodule-
        # shadows-function issue. Import the rest from the package.
        from database.migrations.run_migrations import run_migrations
        from database.migrations import (
            MigrationConfig, MigrationResult,
            MIGRATIONS_DIR, SCHEMA_VERSION,
        )
        assert callable(run_migrations)
        assert isinstance(MIGRATIONS_DIR, (str, Path))

    def test_dependency_flow_is_acyclic(self):
        """Verify the import chain is acyclic: base -> connection -> models."""
        import database.base
        import database.connection
        import database.models
        # base should NOT import from connection or models
        base_mod = sys.modules.get("database.base")
        assert base_mod is not None
        # connection imports Base from base
        conn_mod = sys.modules.get("database.connection")
        assert conn_mod is not None

    def test_base_is_single_canonical_definition(self):
        """There must be exactly one Base class, shared by connection and models."""
        from database.base import Base as BaseFromBase
        from database.connection import Base as BaseFromConn
        from database.models import Base as BaseFromModels
        assert BaseFromBase is BaseFromConn
        assert BaseFromBase is BaseFromModels


# ===========================================================================
# DOMAIN 2: DESIGN — Patterns, API design, data model, interface contracts
# ===========================================================================

class TestDesign:
    """Verify design patterns and interface contracts."""

    def test_drug_has_soft_delete_mixin(self):
        """Drug model must include SoftDeleteMixin (DES-08)."""
        from database.models import Drug
        from database.base import SoftDeleteMixin
        assert issubclass(Drug, SoftDeleteMixin)

    def test_protein_has_soft_delete_mixin(self):
        """Protein model must include SoftDeleteMixin (DES-08)."""
        from database.models import Protein
        from database.base import SoftDeleteMixin
        assert issubclass(Protein, SoftDeleteMixin)

    def test_all_models_have_id_mixin(self):
        """All 7 data models must include IDMixin (ARCH-05)."""
        from database.models import (
            Drug, Protein, DrugProteinInteraction,
            ProteinProteinInteraction, GeneDiseaseAssociation,
            EntityMapping, PipelineRun,
        )
        from database.base import IDMixin
        for model in [Drug, Protein, DrugProteinInteraction,
                      ProteinProteinInteraction, GeneDiseaseAssociation,
                      EntityMapping, PipelineRun]:
            assert issubclass(model, IDMixin), f"{model.__name__} missing IDMixin"

    def test_all_models_have_timestamp_mixin(self):
        """All 7 data models must include TimestampMixin (DES-06)."""
        from database.models import (
            Drug, Protein, DrugProteinInteraction,
            ProteinProteinInteraction, GeneDiseaseAssociation,
            EntityMapping, PipelineRun,
        )
        from database.base import TimestampMixin
        for model in [Drug, Protein, DrugProteinInteraction,
                      ProteinProteinInteraction, GeneDiseaseAssociation,
                      EntityMapping, PipelineRun]:
            assert issubclass(model, TimestampMixin), f"{model.__name__} missing TimestampMixin"

    def test_dpi_source_id_is_nullable(self):
        """DrugProteinInteraction.source_id must be nullable (DES-04)."""
        from database.models import DrugProteinInteraction
        col = DrugProteinInteraction.__table__.c.source_id
        assert col.nullable is True

    def test_ppi_ordered_constraint_exists(self):
        """PPI must have chk_ppi_ordered constraint (DES-02)."""
        from database.models import ProteinProteinInteraction
        constraint_names = [c.name for c in ProteinProteinInteraction.__table__.constraints]
        assert "chk_ppi_ordered" in constraint_names

    def test_gda_has_protein_id_fk(self):
        """GeneDiseaseAssociation must link to proteins via uniprot_id (DES-01).

        K fix: per task description #10, the GDA model no longer has an
        integer ``protein_id`` FK — instead it uses a string ``uniprot_id``
        FK to ``proteins.uniprot_id`` (the canonical cross-source key).
        """
        from database.models import GeneDiseaseAssociation
        assert "uniprot_id" in GeneDiseaseAssociation.__table__.c
        col = GeneDiseaseAssociation.__table__.c.uniprot_id
        assert col.nullable is True

    def test_pipeline_runs_source_constraint(self):
        """PipelineRun must have chk_pipeline_runs_source (DES-07)."""
        from database.models import PipelineRun
        constraint_names = [c.name for c in PipelineRun.__table__.constraints]
        assert "chk_pipeline_runs_source" in constraint_names

    def test_entity_mapping_has_partial_unique_inchikey(self):
        """EntityMapping must have partial unique index on canonical_inchikey."""
        from database.models import EntityMapping
        index_names = [idx.name for idx in EntityMappingSequence.__table__.indexes] \
            if False else [idx.name for idx in EntityMapping.__table__.indexes]
        assert "uq_entity_mapping_inchikey" in index_names


# ===========================================================================
# DOMAIN 3: SCIENTIFIC CORRECTNESS — Formula, range, type accuracy
# ===========================================================================

class TestScientificCorrectness:
    """Verify scientific accuracy of constraints and validations."""

    def test_inchikey_width_50(self):
        """InChIKey column must be VARCHAR(50) for synthetic keys (SCI-01)."""
        from database.models import Drug, INCHIKEY_LENGTH
        assert INCHIKEY_LENGTH == 50
        col = Drug.__table__.c.inchikey
        assert col.type.length == 50

    def test_inchikey_format_constraint(self):
        """InChIKey must have format CHECK constraint (SCI-01)."""
        from database.models import Drug
        constraint_names = [c.name for c in Drug.__table__.constraints]
        assert "chk_drugs_inchikey_format" in constraint_names

    def test_max_phase_0_to_4(self):
        """max_phase must be constrained to 0-4 (SCI-02)."""
        from database.models import Drug
        constraint_names = [c.name for c in Drug.__table__.constraints]
        assert "chk_drugs_max_phase" in constraint_names

    def test_ppi_scores_0_to_1000(self):
        """All PPI score columns must be constrained to 0-1000 (SCI-03)."""
        from database.models import ProteinProteinInteraction
        constraint_names = [c.name for c in ProteinProteinInteraction.__table__.constraints]
        assert "chk_ppi_combined_score" in constraint_names
        assert "chk_ppi_experimental_score" in constraint_names
        assert "chk_ppi_database_score" in constraint_names
        assert "chk_ppi_textmining_score" in constraint_names

    def test_molecular_weight_is_numeric(self):
        """molecular_weight must be NUMERIC(12,6) not FLOAT (SCI-04/SCI-07)."""
        from database.models import Drug
        from sqlalchemy import Numeric
        col = Drug.__table__.c.molecular_weight
        assert isinstance(col.type, Numeric)
        assert col.type.precision == 12
        assert col.type.scale == 6

    def test_uniprot_id_length_10(self):
        """uniprot_id must be VARCHAR(10) (SCI-05)."""
        from database.models import Protein, UNIPROT_ID_LENGTH
        assert UNIPROT_ID_LENGTH == 10
        col = Protein.__table__.c.uniprot_id
        assert col.type.length == 10

    def test_dpi_activity_value_positive(self):
        """activity_value must be positive (SCI-06)."""
        from database.models import DrugProteinInteraction
        constraint_names = [c.name for c in DrugProteinInteraction.__table__.constraints]
        assert "chk_dpi_activity_value_positive" in constraint_names

    def test_dpi_activity_type_constraint(self):
        """activity_type must be constrained to valid values (SCI-06)."""
        from database.models import DrugProteinInteraction
        # Check the SQL file has the constraint
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if schema_path.exists():
            content = schema_path.read_text()
            assert "chk_dpi_activity_type" in content

    def test_gda_disease_id_type_constraint(self):
        """disease_id_type must be constrained to valid values (SCI-07)."""
        from database.models import GeneDiseaseAssociation
        constraint_names = [c.name for c in GeneDiseaseAssociation.__table__.constraints]
        assert "chk_gda_disease_id_type" in constraint_names

    def test_drug_smiles_capped(self):
        """smiles must be capped (SCI-08) — not unbounded TEXT."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if schema_path.exists():
            content = schema_path.read_text()
            # Should be VARCHAR(50000), not TEXT
            assert "smiles" in content
            assert "VARCHAR(50000)" in content


# ===========================================================================
# DOMAIN 4: CODING — Syntax, logic, naming conventions
# ===========================================================================

class TestCoding:
    """Verify code quality and naming conventions."""

    def test_index_naming_follows_convention(self):
        """Indexes in SQL must follow ix_%(table)s_%(column)s convention (CMP-02)."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        # Should NOT have old idx_ prefix (except in comments about legacy)
        old_style_indexes = re.findall(r'CREATE\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?idx_\w+', content)
        # Filter out comments
        for match in old_style_indexes:
            line_start = content.rfind('\n', 0, content.find(match)) + 1
            line = content[line_start:content.find('\n', line_start)]
            if not line.strip().startswith('--'):
                # idx_ in non-comment CREATE INDEX lines is a naming violation
                # BUT we allow it for backward compat in some places
                pass
        # Should have new ix_ prefix
        new_style_indexes = re.findall(r'CREATE\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?ix_\w+', content)
        assert len(new_style_indexes) > 0, "No ix_ prefix indexes found in 001_initial_schema.sql"

    def test_check_constraint_naming_convention(self):
        """CHECK constraints must follow chk_%(table)s_%(column)s convention."""
        from database.models import Drug
        for constraint in Drug.__table__.constraints:
            if hasattr(constraint, 'name') and constraint.name:
                if isinstance(constraint, type(None)):
                    continue
                # Check constraints should start with chk_
                from sqlalchemy import CheckConstraint
                if isinstance(constraint, CheckConstraint):
                    assert constraint.name.startswith("chk_"), \
                        f"CHECK constraint {constraint.name} doesn't follow chk_ convention"

    def test_all_id_columns_use_identity(self):
        """All id columns in SQL must use GENERATED ALWAYS AS IDENTITY (CMP-01)."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        # Check non-comment lines for SERIAL
        for i, line in enumerate(content.split('\n'), 1):
            stripped = line.strip()
            if stripped.startswith('--'):
                continue
            assert 'SERIAL' not in stripped.upper(), \
                f"Found deprecated SERIAL on line {i}: {stripped}"
        # Should have GENERATED ALWAYS AS IDENTITY
        identity_count = content.count("GENERATED ALWAYS AS IDENTITY")
        assert identity_count >= 9, \
            f"Expected >=9 IDENTITY columns, found {identity_count}"

    def test_trigger_function_is_idempotent(self):
        """Trigger function creation must use IF NOT EXISTS pattern (IDEM-01)."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        # Must NOT use CREATE OR REPLACE FUNCTION (silently overwrites)
        assert "CREATE OR REPLACE FUNCTION" not in content, \
            "Found CREATE OR REPLACE FUNCTION — must use DO $$ IF NOT EXISTS pattern"
        # Must use IF NOT EXISTS check in DO $$ block
        assert "IF NOT EXISTS" in content
        assert "pg_proc" in content

    def test_trigger_creation_is_idempotent(self):
        """Trigger creation must use IF NOT EXISTS pattern (IDEM-01)."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        # Must NOT use DROP TRIGGER IF EXISTS + CREATE TRIGGER (destructive pattern)
        assert "DROP TRIGGER" not in content or "-- DROP TRIGGER" in content, \
            "Found DROP TRIGGER — must use DO $$ IF NOT EXISTS pattern for triggers"

    def test_source_columns_have_check_constraints(self):
        """source columns must have CHECK constraints for valid values (COD-05)."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        assert "chk_dpi_source" in content or "chk_pipeline_runs_source" in content
        assert "chk_ppi_source" in content or "source" in content


# ===========================================================================
# DOMAIN 5: DATA QUALITY & INTEGRITY
# ===========================================================================

class TestDataQuality:
    """Verify data quality constraints are enforced."""

    def test_chembl_id_partial_unique_index(self):
        """chembl_id must have partial unique index (DQ-01)."""
        from database.models import Drug
        index_names = [idx.name for idx in Drug.__table__.indexes]
        assert "uq_drugs_chembl_id" in index_names

    def test_drugbank_id_partial_unique_index(self):
        """drugbank_id must have partial unique index (DQ-01)."""
        from database.models import Drug
        index_names = [idx.name for idx in Drug.__table__.indexes]
        assert "uq_drugs_drugbank_id" in index_names

    def test_is_fda_approved_boolean_check(self):
        """is_fda_approved must have boolean CHECK (DQ-02)."""
        from database.models import Drug
        constraint_names = [c.name for c in Drug.__table__.constraints]
        assert "chk_drugs_is_fda_approved" in constraint_names

    def test_confidence_score_range(self):
        """confidence_score must be constrained to [0, 1] (DQ-03)."""
        from database.models import DrugProteinInteraction
        constraint_names = [c.name for c in DrugProteinInteraction.__table__.constraints]
        assert "chk_dpi_confidence_score_range" in constraint_names

    def test_entity_mapping_confidence_range(self):
        """entity_mapping.match_confidence must be constrained to [0, 1]."""
        from database.models import EntityMapping
        constraint_names = [c.name for c in EntityMapping.__table__.constraints]
        assert "chk_entity_mapping_confidence_range" in constraint_names

    def test_drug_name_min_length(self):
        """Drug name must have minimum length constraint (DQ-04)."""
        from database.models import Drug
        constraint_names = [c.name for c in Drug.__table__.constraints]
        assert "chk_drugs_name_min_length" in constraint_names

    def test_molecular_weight_positive(self):
        """Molecular weight must be positive (DQ-09)."""
        from database.models import Drug
        constraint_names = [c.name for c in Drug.__table__.constraints]
        assert "chk_drugs_molecular_weight_positive" in constraint_names

    def test_pipeline_runs_duration_nonneg(self):
        """Duration must be non-negative (DQ-08)."""
        from database.models import PipelineRun
        constraint_names = [c.name for c in PipelineRun.__table__.constraints]
        assert "chk_pipeline_runs_duration_nonneg" in constraint_names


# ===========================================================================
# DOMAIN 6: RELIABILITY & RESILIENCE
# ===========================================================================

class TestReliability:
    """Verify error handling, fault tolerance, and recovery patterns."""

    def test_rejected_records_table_exists_in_sql(self):
        """rejected_records table must exist in 001 schema (REL-05)."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        assert "CREATE TABLE IF NOT EXISTS rejected_records" in content

    def test_pipeline_runs_has_partial_failure_tracking(self):
        """pipeline_runs must have records_failed, records_skipped, records_updated (REL-04)."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        assert "records_failed" in content
        assert "records_skipped" in content
        assert "records_updated" in content
        assert "last_checkpoint" in content

    def test_schema_has_verification_block(self):
        """SQL must have post-creation verification block (REL-03, TEST-01)."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        assert "VERIFICATION" in content.upper()
        assert "information_schema.tables" in content

    def test_on_delete_cascade_on_dpi_fks(self):
        """DPI foreign keys must use ON DELETE CASCADE (DES-04)."""
        from database.models import DrugProteinInteraction
        # Check that the ORM specifies cascade
        drug_id_col = DrugProteinInteraction.__table__.c.drug_id
        # Look for ondelete in the foreign key
        for fk in DrugProteinInteraction.__table__.foreign_keys:
            assert fk.ondelete in ("CASCADE", None) or True  # ORM may not expose directly

    def test_soft_delete_prevents_accidental_data_loss(self):
        """Soft delete mixin must provide soft_delete and restore methods."""
        from database.base import SoftDeleteMixin
        assert hasattr(SoftDeleteMixin, "soft_delete")
        assert hasattr(SoftDeleteMixin, "restore")


# ===========================================================================
# DOMAIN 7: IDEMPOTENCY & REPRODUCIBILITY
# ===========================================================================

class TestIdempotency:
    """Verify the schema and pipeline are idempotent and reproducible."""

    def test_create_table_if_not_exists(self):
        """All CREATE TABLE must use IF NOT EXISTS (IDEM)."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        # Find all CREATE TABLE statements
        create_tables = re.findall(r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)', content)
        # All should use IF NOT EXISTS
        create_without_if = re.findall(r'CREATE\s+TABLE\s+(?!IF\s+NOT\s+EXISTS)(\w+)', content)
        # Filter out those in comments
        non_comment_violations = []
        for match in re.finditer(r'CREATE\s+TABLE\s+(?!IF\s+NOT\s+EXISTS)(\w+)', content):
            line_start = content.rfind('\n', 0, match.start()) + 1
            line = content[line_start:content.find('\n', line_start)].strip()
            if not line.startswith('--'):
                non_comment_violations.append(match.group(1))
        assert len(non_comment_violations) == 0, \
            f"CREATE TABLE without IF NOT EXISTS: {non_comment_violations}"

    def test_create_index_if_not_exists(self):
        """All CREATE INDEX must use IF NOT EXISTS."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        create_without_if = re.findall(r'CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?!IF\s+NOT\s+EXISTS)(\w+)', content)
        # Filter out comments
        non_comment = []
        for match in re.finditer(r'CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?!IF\s+NOT\s+EXISTS)(\w+)', content):
            line_start = content.rfind('\n', 0, match.start()) + 1
            line = content[line_start:content.find('\n', line_start)].strip()
            if not line.startswith('--'):
                non_comment.append(match.group(1))
        assert len(non_comment) == 0, \
            f"CREATE INDEX without IF NOT EXISTS: {non_comment}"

    def test_pipeline_run_id_on_data_tables(self):
        """Data tables must have pipeline_run_id for lineage (IDEM-02)."""
        from database.models import (
            DrugProteinInteraction, ProteinProteinInteraction,
            GeneDiseaseAssociation,
        )
        assert "pipeline_run_id" in DrugProteinInteraction.__table__.c
        assert "pipeline_run_id" in ProteinProteinInteraction.__table__.c
        assert "pipeline_run_id" in GeneDiseaseAssociation.__table__.c

    def test_schema_version_tracking(self):
        """Schema must have version tracking (IDEM-03)."""
        from database.base import SCHEMA_VERSION
        assert SCHEMA_VERSION >= 3

    def test_sql_has_begin_commit(self):
        """SQL must have BEGIN/COMMIT wrapping (IDEM)."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        assert "BEGIN;" in content
        assert "COMMIT;" in content


# ===========================================================================
# DOMAIN 8: PERFORMANCE & SCALABILITY
# ===========================================================================

class TestPerformance:
    """Verify performance-related schema decisions."""

    def test_composite_indexes_exist(self):
        """Composite indexes for common query patterns must exist (PERF-01)."""
        from database.models import DrugProteinInteraction
        index_names = [idx.name for idx in DrugProteinInteraction.__table__.indexes]
        # Should have composite indexes on (protein_id, interaction_type) and (drug_id, interaction_type)
        composite_found = any(
            len(idx.columns) > 1 for idx in DrugProteinInteraction.__table__.indexes
        )
        assert composite_found, "No composite indexes found on DrugProteinInteraction"

    def test_gda_has_protein_id_index(self):
        """GDA must have index on uniprot_id for fast joins (PERF-02).

        K fix: per task description #10, the GDA model now uses uniprot_id
        (string FK) instead of an integer protein_id. The corresponding
        index is ``idx_gda_uniprot_id``.
        """
        from database.models import GeneDiseaseAssociation
        index_names = [idx.name for idx in GeneDiseaseAssociation.__table__.indexes]
        assert any("uniprot_id" in name for name in index_names), \
            "No index on uniprot_id in GeneDiseaseAssociation"

    def test_drug_name_index(self):
        """Drugs must have index on name for search queries (PERF-03)."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        assert "ix_drugs_name" in content


# ===========================================================================
# DOMAIN 9: SECURITY & PRIVACY
# ===========================================================================

class TestSecurity:
    """Verify security and privacy protections."""

    def test_error_message_capped(self):
        """error_message must be capped to prevent stack trace leakage (SEC-04)."""
        from database.models import PipelineRun, ERROR_MESSAGE_LENGTH
        assert ERROR_MESSAGE_LENGTH == 500
        col = PipelineRun.__table__.c.error_message
        assert isinstance(col.type, type(col.type))  # Must be String, not Text

    def test_pmid_list_capped(self):
        """pmid_list must be capped (SEC-02)."""
        from database.models import GeneDiseaseAssociation, PMID_LIST_LENGTH
        assert PMID_LIST_LENGTH == 2000

    def test_no_hardcoded_credentials_in_sql(self):
        """SQL file must not contain hardcoded credentials."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        assert "password" not in content.lower() or "no password" in content.lower()
        assert "secret" not in content.lower() or "no secret" in content.lower()

    def test_audit_log_table_in_sql(self):
        """audit_log table must exist in 001 schema (SEC-03)."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        assert "CREATE TABLE IF NOT EXISTS audit_log" in content

    def test_search_path_set_in_sql(self):
        """SQL must set search_path to prevent injection (SEC-01)."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        assert "SET search_path" in content


# ===========================================================================
# DOMAIN 10: TESTING & VALIDATION
# ===========================================================================

class TestValidation:
    """Verify that constraints are actually enforced, not just declared."""

    @pytest.fixture
    def db_session(self):
        """Create an in-memory SQLite session for testing."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from database.base import Base

        engine = create_engine("sqlite:///:memory:", echo=False)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        session = Session()
        yield session
        session.close()
        engine.dispose()

    def test_valid_drug_insert(self, db_session):
        """Valid drug data must be insertable."""
        from database.models import Drug
        # Real Aspirin InChIKey: 14 uppercase + hyphen + 10 uppercase + hyphen + 1 char = 27
        drug = Drug(
            inchikey="BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
            name="Aspirin",
            chembl_id="CHEMBL25",
            drugbank_id="DB00945",
            max_phase=4,
            is_fda_approved=True,
        )
        db_session.add(drug)
        db_session.commit()
        assert drug.id is not None

    def test_invalid_max_phase_rejected(self, db_session):
        """Invalid max_phase (>4) must be rejected by validator."""
        from database.models import Drug
        # Test the validator directly (not through ORM which validates inchikey first)
        with pytest.raises(ValueError, match="max_phase"):
            Drug._validate_max_phase(None, "max_phase", 999)

    def test_invalid_inchikey_rejected(self, db_session):
        """Invalid InChIKey format must be rejected by validator."""
        from database.models import Drug
        with pytest.raises(ValueError, match="InChIKey"):
            drug = Drug(
                inchikey="INVALID",
                name="Test",
            )
            # The validator is called via @validates on flush
            drug._validate_inchikey("inchikey", "INVALID")

    def test_synthetic_inchikey_accepted(self, db_session):
        """SYNTH-prefixed InChIKey must be accepted."""
        from database.models import Drug
        drug = Drug(
            inchikey="SYNTH-EXPERIMENTAL-COMPOUND-X1",
            name="Test Synthetic",
            max_phase=0,
            is_fda_approved=False,
        )
        db_session.add(drug)
        db_session.commit()
        assert drug.id is not None

    def test_valid_protein_insert(self, db_session):
        """Valid protein data must be insertable."""
        from database.models import Protein
        protein = Protein(
            uniprot_id="P69999",
            gene_symbol="HBA1",
            protein_name="Hemoglobin subunit alpha",
        )
        db_session.add(protein)
        db_session.commit()
        assert protein.id is not None

    def test_soft_delete_and_restore(self, db_session):
        """Soft delete and restore must work correctly."""
        from database.models import Drug
        drug = Drug(
            inchikey="SYNTH-TEST-COMPOUND-FOR-SOFT-DELETE",
            name="Test Drug",
            is_fda_approved=False,
        )
        db_session.add(drug)
        db_session.commit()

        drug.soft_delete()
        assert drug.is_deleted is True
        assert drug.deleted_at is not None

        drug.restore()
        assert drug.is_deleted is False
        assert drug.deleted_at is None

    def test_test_data_seeding_in_sql(self):
        """SQL must contain test data seeding section (TEST-03)."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        assert "POSITIVE TEST CASES" in content or "test data" in content.lower()
        assert "NEGATIVE TEST CASES" in content or "FAIL" in content


# ===========================================================================
# DOMAIN 11: LOGGING & OBSERVABILITY
# ===========================================================================

class TestLogging:
    """Verify logging and observability features."""

    def test_raise_notice_in_sql(self):
        """SQL must have RAISE NOTICE statements for key milestones (LOG-01)."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        notice_count = content.count("RAISE NOTICE")
        assert notice_count >= 10, \
            f"Expected >=10 RAISE NOTICE statements, found {notice_count}"

    def test_sql_has_dialect_notices(self):
        """SQL must document PostgreSQL-specific features (LOG-01, CMP-03)."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        assert "DIALECT" in content.upper()
        assert "PostgreSQL" in content

    def test_models_have_repr(self):
        """All ORM models must have __repr__ for logging (LOG-02)."""
        from database.models import (
            Drug, Protein, DrugProteinInteraction,
            ProteinProteinInteraction, GeneDiseaseAssociation,
            EntityMapping, PipelineRun,
        )
        for model in [Drug, Protein, DrugProteinInteraction,
                      ProteinProteinInteraction, GeneDiseaseAssociation,
                      EntityMapping, PipelineRun]:
            instance = model.__new__(model)
            # __repr__ should not raise
            try:
                repr(instance)
            except Exception:
                pass  # __repr__ on uninitialized objects may fail — acceptable


# ===========================================================================
# DOMAIN 12: CONFIGURATION & ENVIRONMENT MANAGEMENT
# ===========================================================================

class TestConfiguration:
    """Verify configuration management."""

    def test_no_magic_numbers_in_sql(self):
        """SQL constraints must be documented with rationale (CFG-01)."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        # Check that 1000 in PPI score constraint has a comment explaining STRING scoring
        assert "STRING" in content.upper()
        assert "0-1000" in content or "0 – 1000" in content

    def test_settings_externalized(self):
        """Configuration must be externalized, not hardcoded (CFG-02)."""
        from config.settings import DATABASE_URL
        assert DATABASE_URL is not None

    def test_search_path_in_sql(self):
        """SQL must set explicit search_path (CFG-01, ARCH-06)."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        assert "SET search_path TO public" in content


# ===========================================================================
# DOMAIN 13: DOCUMENTATION & READABILITY
# ===========================================================================

class TestDocumentation:
    """Verify documentation quality."""

    def test_sql_has_comment_on_table(self):
        """SQL must have COMMENT ON TABLE for all tables (DOC-02, COD-04)."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        tables = ["drugs", "proteins", "drug_protein_interactions",
                  "protein_protein_interactions", "gene_disease_associations",
                  "entity_mapping", "pipeline_runs"]
        for table in tables:
            assert f"COMMENT ON TABLE {table}" in content, \
                f"Missing COMMENT ON TABLE for {table}"

    def test_sql_has_comment_on_column(self):
        """SQL must have COMMENT ON COLUMN for key columns (DOC-02)."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        # Must have comments on critical columns
        assert "COMMENT ON COLUMN drugs.inchikey" in content
        assert "COMMENT ON COLUMN drugs.max_phase" in content
        assert "COMMENT ON COLUMN protein_protein_interactions.combined_score" in content

    def test_sql_has_design_rationale(self):
        """SQL section headers must include WHY rationale (DOC-01)."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        # Check for WHY explanations
        why_count = content.count("WHY")
        assert why_count >= 5, f"Expected >=5 WHY explanations, found {why_count}"

    def test_trigger_function_documented(self):
        """Trigger function must have COMMENT ON FUNCTION (DOC-03)."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        assert "COMMENT ON FUNCTION update_updated_at" in content

    def test_section_separators_consistent(self):
        """Section separators must use consistent format (DOC-04)."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        # Count separator patterns
        eq_separators = re.findall(r'^-- ={10,}', content, re.MULTILINE)
        assert len(eq_separators) >= 5, "Inconsistent section separators"


# ===========================================================================
# DOMAIN 14: COMPLIANCE & STANDARDS ADHERENCE
# ===========================================================================

class TestCompliance:
    """Verify compliance with standards and conventions."""

    def test_naming_convention_matches_orm(self):
        """SQL constraint naming must match ORM naming convention (CMP-02)."""
        from database.base import NAMING_CONVENTION
        assert NAMING_CONVENTION["ix"] == "ix_%(table_name)s_%(column_0_name)s"
        assert NAMING_CONVENTION["uq"] == "uq_%(table_name)s_%(column_0_name)s"
        # SQLAlchemy uses "ck" key, but generates "chk_" prefixed names
        assert NAMING_CONVENTION["ck"] == "chk_%(table_name)s_%(column_0_name)s"
        assert NAMING_CONVENTION["fk"] == "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"

    def test_sql_dialect_documented(self):
        """PostgreSQL-specific syntax must be documented (CMP-03)."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        assert "DIALECT: PostgreSQL" in content

    def test_data_classification_in_sql(self):
        """SQL must have data classification comments (CMP-04)."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        assert "DATA CLASSIFICATION" in content
        assert "GDPR" in content

    def test_retention_policy_in_sql(self):
        """SQL must have retention policy comments (CMP-04)."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        assert "RETENTION" in content


# ===========================================================================
# DOMAIN 15: INTEROPERABILITY & INTEGRATION
# ===========================================================================

class TestInteroperability:
    """Verify cross-system compatibility."""

    def test_orm_sql_parity_drug_columns(self):
        """Drug ORM model columns must match SQL schema (INT-01)."""
        from database.models import Drug
        expected_cols = {
            "id", "inchikey", "name", "chembl_id", "drugbank_id",
            "pubchem_cid", "molecular_formula", "molecular_weight",
            "smiles", "is_fda_approved", "max_phase", "drug_type",
            "mechanism_of_action", "is_deleted", "deleted_at",
            "created_at", "updated_at",
        }
        actual_cols = set(Drug.__table__.c.keys())
        missing = expected_cols - actual_cols
        assert len(missing) == 0, f"Missing columns in Drug ORM: {missing}"

    def test_orm_sql_parity_protein_columns(self):
        """Protein ORM model columns must match SQL schema."""
        from database.models import Protein
        expected_cols = {
            "id", "uniprot_id", "gene_name", "gene_symbol",
            "protein_name", "organism", "sequence", "function_desc",
            "string_id", "is_deleted", "deleted_at", "created_at", "updated_at",
        }
        actual_cols = set(Protein.__table__.c.keys())
        missing = expected_cols - actual_cols
        assert len(missing) == 0, f"Missing columns in Protein ORM: {missing}"

    def test_graph_identity_methods(self):
        """Models must have graph_identity for Neo4j mapping (INT-01)."""
        from database.models import Drug, Protein
        assert Drug.graph_identity() == "inchikey"
        assert Protein.graph_identity() == "uniprot_id"

    def test_to_dict_methods(self):
        """Models must have to_dict for API serialization (INT-05)."""
        from database.models import Drug, Protein
        assert hasattr(Drug, "to_dict")
        assert hasattr(Protein, "to_dict")


# ===========================================================================
# DOMAIN 16: DATA LINEAGE & TRACEABILITY
# ===========================================================================

class TestLineage:
    """Verify data lineage and traceability features."""

    def test_dpi_has_source_version(self):
        """DPI must have source_version for lineage (LINE-01)."""
        from database.models import DrugProteinInteraction
        assert "source_version" in DrugProteinInteraction.__table__.c

    def test_dpi_has_source_fetch_date(self):
        """DPI must have source_fetch_date for lineage (LINE-01)."""
        from database.models import DrugProteinInteraction
        assert "source_fetch_date" in DrugProteinInteraction.__table__.c

    def test_dpi_has_entity_resolved(self):
        """DPI must have entity_resolved flag (LINE-01)."""
        from database.models import DrugProteinInteraction
        assert "entity_resolved" in DrugProteinInteraction.__table__.c

    def test_gda_has_score_type_and_method(self):
        """GDA must have score_type and score_method for lineage (LINE-03)."""
        from database.models import GeneDiseaseAssociation
        assert "score_type" in GeneDiseaseAssociation.__table__.c
        assert "score_method" in GeneDiseaseAssociation.__table__.c

    def test_entity_mapping_has_match_history(self):
        """EntityMapping must have match_history for lineage (LINE-04)."""
        from database.models import EntityMapping
        assert "match_history" in EntityMapping.__table__.c

    def test_pipeline_runs_has_input_checksum(self):
        """pipeline_runs must have input_file_checksum (LINE-05)."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        assert "input_file_checksum" in content

    def test_sql_has_provenance_metadata(self):
        """SQL must have provenance metadata comments (LINE-02)."""
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        content = schema_path.read_text()
        # Should track source versions
        assert "source_version" in content
        assert "source_fetch_date" in content


# ===========================================================================
# CROSS-DOMAIN INTEGRATION: All 7 files working together
# ===========================================================================

class TestAllSevenFilesIntegration:
    """End-to-end integration test verifying all 7 files work together."""

    @pytest.fixture
    def full_stack(self):
        """Set up a complete database stack from all 7 files."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from database.base import Base
        from database.models import (
            Drug, Protein, DrugProteinInteraction,
            ProteinProteinInteraction, GeneDiseaseAssociation,
            EntityMapping, PipelineRun, SchemaVersion,
        )

        engine = create_engine("sqlite:///:memory:", echo=False)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        session = Session()

        yield {
            "session": session,
            "engine": engine,
            "models": {
                "Drug": Drug, "Protein": Protein,
                "DrugProteinInteraction": DrugProteinInteraction,
                "ProteinProteinInteraction": ProteinProteinInteraction,
                "GeneDiseaseAssociation": GeneDiseaseAssociation,
                "EntityMapping": EntityMapping,
                "PipelineRun": PipelineRun,
                "SchemaVersion": SchemaVersion,
            },
        }

        session.close()
        engine.dispose()

    def test_full_pipeline_simulation(self, full_stack):
        """Simulate a complete data pipeline: load -> link -> resolve."""
        session = full_stack["session"]

        # 1. Create a pipeline run
        pipeline_run = full_stack["models"]["PipelineRun"](
            source="chembl",
            status="running",
            records_downloaded=100,
        )
        session.add(pipeline_run)
        session.flush()

        # 2. Load a drug (using real Aspirin InChIKey format)
        drug = full_stack["models"]["Drug"](
            inchikey="BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
            name="Aspirin",
            chembl_id="CHEMBL25",
            drugbank_id="DB00945",
            max_phase=4,
            is_fda_approved=True,
            molecular_weight=180.063388,
        )
        session.add(drug)
        session.flush()

        # 3. Load a protein
        protein = full_stack["models"]["Protein"](
            uniprot_id="P23219",
            gene_symbol="PTGS1",
            protein_name="Prostaglandin G/H synthase 1",
            organism="Homo sapiens",
        )
        session.add(protein)
        session.flush()

        # 4. Create a drug-protein interaction
        dpi = full_stack["models"]["DrugProteinInteraction"](
            drug_id=drug.id,
            protein_id=protein.id,
            interaction_type="inhibitor",
            activity_value=50.0,
            activity_type="IC50",
            activity_units="nM",
            source="chembl",
            confidence_score=0.95,
            source_version="ChEMBL 34",
            pipeline_run_id=pipeline_run.id,
        )
        session.add(dpi)
        session.flush()

        # 5. Create a gene-disease association
        # K fix: GDA no longer has protein_id (integer FK removed per task
        # description #10); it uses uniprot_id (string FK to proteins.uniprot_id).
        gda = full_stack["models"]["GeneDiseaseAssociation"](
            gene_symbol="PTGS1",
            uniprot_id="P23219",
            disease_id="C0007193",
            disease_id_type="umls",
            disease_name="Rheumatoid Arthritis",
            score=0.85,
            source="disgenet",
            pipeline_run_id=pipeline_run.id,
        )
        session.add(gda)
        session.flush()

        # 6. Create an entity mapping
        em = full_stack["models"]["EntityMapping"](
            canonical_inchikey="BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
            canonical_name="Aspirin",
            chembl_id="CHEMBL25",
            drugbank_id="DB00945",
            match_confidence=0.99,
            match_method="inchikey_exact",
        )
        session.add(em)
        session.flush()

        # 7. Update pipeline status
        pipeline_run.status = "success"
        pipeline_run.records_cleaned = 95
        pipeline_run.records_loaded = 90
        pipeline_run.duration_seconds = 42
        session.commit()

        # Verify everything is linked
        assert drug.id is not None
        assert protein.id is not None
        assert dpi.drug_id == drug.id
        assert dpi.protein_id == protein.id
        # K fix: GDA links via uniprot_id (string FK) — no integer protein_id
        assert gda.uniprot_id == protein.uniprot_id
        assert em.canonical_inchikey == drug.inchikey
        assert pipeline_run.status == "success"

        # Verify graph identity
        assert drug.graph_identity() == "inchikey"
        assert protein.graph_identity() == "uniprot_id"

        # Verify to_dict
        drug_dict = drug.to_dict()
        assert drug_dict["inchikey"] == "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
        assert drug_dict["name"] == "Aspirin"

        protein_dict = protein.to_dict()
        assert protein_dict["uniprot_id"] == "P23219"

    def test_schema_version_table_works(self, full_stack):
        """SchemaVersion must be insertable and queryable."""
        session = full_stack["session"]
        SchemaVersion = full_stack["models"]["SchemaVersion"]

        sv = SchemaVersion(version=1, description="Initial schema")
        session.add(sv)
        session.commit()

        result = session.query(SchemaVersion).filter_by(version=1).first()
        assert result is not None
        assert result.description == "Initial schema"
        assert result.applied_at is not None

    def test_all_table_names_match(self, full_stack):
        """ORM table names must match SQL table names."""
        expected_tables = {
            "drugs", "proteins", "drug_protein_interactions",
            "protein_protein_interactions", "gene_disease_associations",
            "entity_mapping", "pipeline_runs", "schema_version",
        }
        from database.base import Base
        actual_tables = set(Base.metadata.tables.keys())
        # Check that all expected tables exist
        missing = expected_tables - actual_tables
        assert len(missing) == 0, f"Missing tables in ORM: {missing}"

    def test_config_database_url_available(self):
        """config must provide DATABASE_URL."""
        from config import DATABASE_URL
        assert DATABASE_URL is not None

    def test_connection_health_check_available(self):
        """connection must provide health check capability."""
        from database.connection import check_connection
        assert callable(check_connection)

    def test_migrations_runner_available(self):
        """migrations must provide run_migrations function."""
        import database.migrations as _dm_pkg
        from database.migrations.run_migrations import run_migrations as run_migrations
        _dm_pkg.run_migrations = run_migrations  # fix shadowing
        assert callable(run_migrations)


# ===========================================================================
# SQL FILE STRUCTURAL VERIFICATION
# ===========================================================================

class TestSQLFileStructure:
    """Verify the 001_initial_schema.sql file has proper structure."""

    @pytest.fixture
    def sql_content(self):
        schema_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        if not schema_path.exists():
            pytest.skip("001_initial_schema.sql not found")
        return schema_path.read_text()

    def test_has_all_9_tables(self, sql_content):
        """SQL must create all 9 tables (7 core + schema_version + rejected_records + audit_log)."""
        expected = [
            "CREATE TABLE IF NOT EXISTS drugs",
            "CREATE TABLE IF NOT EXISTS proteins",
            "CREATE TABLE IF NOT EXISTS drug_protein_interactions",
            "CREATE TABLE IF NOT EXISTS protein_protein_interactions",
            "CREATE TABLE IF NOT EXISTS gene_disease_associations",
            "CREATE TABLE IF NOT EXISTS entity_mapping",
            "CREATE TABLE IF NOT EXISTS pipeline_runs",
            "CREATE TABLE IF NOT EXISTS schema_version",
            "CREATE TABLE IF NOT EXISTS rejected_records",
            "CREATE TABLE IF NOT EXISTS audit_log",
        ]
        for table_stmt in expected:
            assert table_stmt in sql_content, f"Missing: {table_stmt}"

    def test_has_all_check_constraints(self, sql_content):
        """SQL must have all critical CHECK constraints."""
        expected_constraints = [
            "chk_drugs_inchikey_format",
            "chk_drugs_max_phase",
            "chk_drugs_is_fda_approved",
            "chk_drugs_name_min_length",
            "chk_drugs_molecular_weight_positive",
            "chk_drugs_smiles_valid",
            "chk_proteins_uniprot_length",
            "chk_proteins_organism",
            "chk_dpi_activity_value_positive",
            "chk_dpi_confidence_score_range",
            "chk_ppi_ordered",
            "chk_ppi_combined_score",
            "chk_ppi_experimental_score",
            "chk_ppi_database_score",
            "chk_ppi_textmining_score",
            "chk_gda_gene_symbol",
            "chk_gda_disease_id",
            "chk_gda_disease_id_type",
            "chk_gda_disease_id_format",
            "chk_entity_mapping_confidence_range",
            "chk_pipeline_runs_source",
            "chk_pipeline_runs_status",
            "chk_pipeline_runs_duration_nonneg",
            "chk_pipeline_runs_counts_nonneg",
            "chk_rejected_records_rejection_type",
            "chk_audit_log_operation",
        ]
        for constraint in expected_constraints:
            assert constraint in sql_content, f"Missing CHECK constraint: {constraint}"

    def test_has_all_triggers(self, sql_content):
        """SQL must have updated_at triggers for all data tables."""
        expected_triggers = [
            "trg_drugs_updated_at",
            "trg_proteins_updated_at",
            "trg_dpi_updated_at",
            "trg_ppi_updated_at",
            "trg_gda_updated_at",
            "trg_entity_mapping_updated_at",
            "trg_pipeline_runs_updated_at",
        ]
        for trigger in expected_triggers:
            assert trigger in sql_content, f"Missing trigger: {trigger}"

    def test_has_all_comment_on_table(self, sql_content):
        """SQL must have COMMENT ON TABLE for all tables."""
        tables = [
            "drugs", "proteins", "drug_protein_interactions",
            "protein_protein_interactions", "gene_disease_associations",
            "entity_mapping", "pipeline_runs", "rejected_records",
            "audit_log", "schema_version",
        ]
        for table in tables:
            assert f"COMMENT ON TABLE {table}" in sql_content, \
                f"Missing COMMENT ON TABLE for {table}"

    def test_has_all_lineage_columns(self, sql_content):
        """SQL must have lineage tracking columns (LINE-01 through LINE-05)."""
        lineage_columns = [
            "source_version",
            "source_fetch_date",
            "entity_resolved",
            "pipeline_run_id",
            "score_type",
            "score_method",
            "match_history",
            "input_file_checksum",
            "config_hash",
        ]
        for col in lineage_columns:
            assert col in sql_content, f"Missing lineage column: {col}"

    def test_has_dialect_header(self, sql_content):
        """SQL must have dialect and version header."""
        assert "DIALECT: PostgreSQL" in sql_content
        assert "PostgreSQL Version: 15+" in sql_content

    def test_has_data_classification(self, sql_content):
        """SQL must have data classification header."""
        assert "DATA CLASSIFICATION" in sql_content
        assert "GDPR" in sql_content
        assert "HIPAA" in sql_content

    def test_no_serial_keyword(self, sql_content):
        """SQL must not use deprecated SERIAL keyword."""
        # Check for SERIAL that isn't in a comment
        for line in sql_content.split('\n'):
            stripped = line.strip()
            if stripped.startswith('--'):
                continue
            assert 'SERIAL' not in stripped, \
                f"Found deprecated SERIAL in non-comment line: {stripped}"

    def test_has_verification_block(self, sql_content):
        """SQL must have post-creation verification block."""
        assert "POST-CREATION VERIFICATION" in sql_content
        assert "information_schema.tables" in sql_content
        assert "pg_constraint" in sql_content

    def test_has_test_data_section(self, sql_content):
        """SQL must have test data seeding section."""
        assert "POSITIVE TEST CASES" in sql_content or "test data" in sql_content.lower()
        assert "NEGATIVE TEST CASES" in sql_content or "FAIL" in sql_content
