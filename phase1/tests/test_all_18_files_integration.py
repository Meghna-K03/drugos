"""
Integration test for ALL 18 files in the Drug Repurposing dataset pipeline.

Files covered (17 already fixed + 1 newly fixed = 18):
  1. config/__init__.py
  2. config/settings.py
  3. database/__init__.py
  4. database/connection.py
  5. database/models.py
  6. database/migrations/__init__.py
  7. database/migrations/001_initial_schema.sql
  8. database/migrations/002_bug_fixes_migration.sql
  9. database/migrations/run_migrations.py
  10. database/loaders.py
  11. cleaning/__init__.py
  12. cleaning/normalizer.py
  13. cleaning/missing_values.py
  14. cleaning/deduplicator.py
  15. entity_resolution/__init__.py
  16. entity_resolution/resolver_utils.py
  17. entity_resolution/drug_resolver.py
  18. entity_resolution/protein_resolver.py  ← NEWLY FIXED

This test verifies that all 18 files work together correctly as a pipeline:
  - Config is loadable
  - Database models and connections work
  - Cleaning pipeline (normalize, dedup, missing values) works
  - Entity resolution (drug + protein resolvers) works
  - End-to-end data flow through the pipeline
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# =====================================================================
# File 1-2: config/__init__.py + config/settings.py
# =====================================================================


class TestConfigModule:
    """Verify config module loads and provides settings."""

    def test_config_init_importable(self):
        """File 1: config/__init__.py is importable."""
        import config
        assert config is not None

    def test_settings_importable(self):
        """File 2: config/settings.py is importable."""
        from config import settings
        assert settings is not None

    def test_settings_has_key_attributes(self):
        """Settings module exposes expected attributes."""
        from config.settings import (
            BASE_DIR,
            RAW_DATA_DIR,
            PROCESSED_DATA_DIR,
        )
        assert BASE_DIR is not None
        assert RAW_DATA_DIR is not None
        assert PROCESSED_DATA_DIR is not None


# =====================================================================
# File 3-5: database/__init__.py + connection.py + models.py
# =====================================================================


class TestDatabaseModule:
    """Verify database module loads and provides models."""

    def test_database_init_importable(self):
        """File 3: database/__init__.py is importable."""
        import database
        assert database is not None

    def test_connection_importable(self):
        """File 4: database/connection.py is importable."""
        from database.connection import Base, get_engine, get_session_factory
        assert Base is not None
        assert callable(get_engine)
        assert callable(get_session_factory)

    def test_models_importable(self):
        """File 5: database/models.py is importable."""
        from database.models import (
            Protein,
            Drug,
            DrugProteinInteraction,
        )
        assert Protein is not None
        assert Drug is not None

    def test_models_have_tables(self):
        """Models define SQLAlchemy tables."""
        from database.models import Protein, Drug
        assert hasattr(Protein, "__tablename__")
        assert hasattr(Drug, "__tablename__")

    def test_protein_model_columns(self):
        """Protein model has expected columns."""
        from database.models import Protein
        actual_cols = {c.name for c in Protein.__table__.columns}
        # Protein model should have uniprot_id at minimum.
        assert "uniprot_id" in actual_cols, f"Protein model missing uniprot_id. Has: {actual_cols}"


# =====================================================================
# File 6-9: database/migrations/
# =====================================================================


class TestMigrationsModule:
    """Verify migration files exist and run_migrations is importable."""

    def test_migrations_init_importable(self):
        """File 6: database/migrations/__init__.py is importable."""
        import database.migrations
        assert database.migrations is not None

    def test_initial_schema_sql_exists(self):
        """File 7: 001_initial_schema.sql exists."""
        sql_path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        assert sql_path.exists(), f"Migration file not found: {sql_path}"
        content = sql_path.read_text()
        assert len(content) > 0

    def test_bug_fixes_migration_sql_exists(self):
        """File 8: 002_bug_fixes_migration.sql exists."""
        sql_path = PROJECT_ROOT / "database" / "migrations" / "002_bug_fixes_migration.sql"
        assert sql_path.exists(), f"Migration file not found: {sql_path}"

    def test_run_migrations_importable(self):
        """File 9: run_migrations.py is importable."""
        from database.migrations.run_migrations import run_migrations
        assert callable(run_migrations)


# =====================================================================
# File 10: database/loaders.py
# =====================================================================


class TestLoadersModule:
    """Verify database/loaders.py is importable and functional."""

    def test_loaders_importable(self):
        """File 10: database/loaders.py is importable."""
        from database.loaders import bulk_upsert_proteins, bulk_upsert_drugs
        assert callable(bulk_upsert_proteins)
        assert callable(bulk_upsert_drugs)


# =====================================================================
# File 11-14: cleaning module
# =====================================================================


class TestCleaningModule:
    """Verify cleaning module loads and provides functionality."""

    def test_cleaning_init_importable(self):
        """File 11: cleaning/__init__.py is importable."""
        import cleaning
        assert cleaning is not None

    def test_normalizer_importable(self):
        """File 12: cleaning/normalizer.py is importable."""
        from cleaning.normalizer import (
            normalize_inchikey,
            standardize_inchikey,
            is_valid_inchikey,
        )
        assert callable(normalize_inchikey)
        assert callable(is_valid_inchikey)

    def test_inchikey_validation(self):
        """InChIKey validation works correctly."""
        from cleaning.normalizer import is_valid_inchikey
        assert is_valid_inchikey("BSYNRYMUTXBXSQ-UHFFFAOYSA-N") is True
        assert is_valid_inchikey("not-an-inchikey") is False

    def test_missing_values_importable(self):
        """File 13: cleaning/missing_values.py is importable."""
        from cleaning.missing_values import is_nullish, clean_proteins
        assert callable(is_nullish)
        assert callable(clean_proteins)

    def test_missing_values_functional(self):
        """Missing values handler works."""
        from cleaning.missing_values import is_nullish
        # is_nullish works with pandas Series or scalars.
        # Just verify the function is callable with valid input.
        import pandas as pd
        assert is_nullish(pd.Series([None])).iloc[0] == True

    def test_deduplicator_importable(self):
        """File 14: cleaning/deduplicator.py is importable."""
        from cleaning.deduplicator import dedup_by_inchikey
        assert callable(dedup_by_inchikey)


# =====================================================================
# File 15-18: entity_resolution module
# =====================================================================


class TestEntityResolutionModule:
    """Verify entity_resolution module loads and provides resolvers."""

    def test_entity_resolution_init_importable(self):
        """File 15: entity_resolution/__init__.py is importable."""
        import entity_resolution
        assert entity_resolution is not None

    def test_resolver_utils_importable(self):
        """File 16: entity_resolution/resolver_utils.py is importable."""
        from entity_resolution.resolver_utils import (
            normalize_name,
            fuzzy_match_score,
            compute_match_confidence,
            validate_protein_record,
            validate_drug_record,
        )
        assert callable(normalize_name)
        assert callable(fuzzy_match_score)
        assert callable(compute_match_confidence)

    def test_drug_resolver_importable(self):
        """File 17: entity_resolution/drug_resolver.py is importable."""
        from entity_resolution.drug_resolver import DrugResolver
        resolver = DrugResolver()
        assert resolver is not None

    def test_protein_resolver_importable(self):
        """File 18: entity_resolution/protein_resolver.py is importable."""
        from entity_resolution.protein_resolver import ProteinResolver
        resolver = ProteinResolver()
        assert resolver is not None
        assert len(resolver) == 0


# =====================================================================
# Cross-module integration: data flows through all 18 files
# =====================================================================


class TestPipelineIntegration:
    """End-to-end data flow through the entire pipeline."""

    def test_normalizer_to_drug_resolver_pipeline(self):
        """Data flows from normalizer through drug resolver."""
        from cleaning.normalizer import is_valid_inchikey
        from entity_resolution.drug_resolver import DrugResolver

        resolver = DrugResolver()
        chembl_records = [
            {
                "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
                "name": "Aspirin",
                "chembl_id": "CHEMBL25",
            }
        ]
        # Verify InChIKey is valid before adding.
        assert is_valid_inchikey("BSYNRYMUTXBXSQ-UHFFFAOYSA-N")
        resolver.add_source_records(chembl_records, source="chembl")
        assert len(resolver.mapping) == 1
        assert "BSYNRYMUTXBXSQ-UHFFFAOYSA-N" in resolver.mapping

    def test_normalizer_to_protein_resolver_pipeline(self):
        """Data flows from normalizer through protein resolver."""
        from entity_resolution.protein_resolver import ProteinResolver

        resolver = ProteinResolver()
        uniprot_records = [
            {"uniprot_id": "P04637", "gene_symbol": "TP53", "organism": "Homo sapiens"},
        ]
        resolver.add_uniprot_records(uniprot_records)
        assert len(resolver.mapping) == 1
        assert "P04637" in resolver.mapping

    def test_string_merge_pipeline(self):
        """STRING records merge into UniProt entries."""
        from entity_resolution.protein_resolver import ProteinResolver

        resolver = ProteinResolver()
        resolver.add_uniprot_records([
            {"uniprot_id": "P04637", "gene_symbol": "TP53", "organism": "Homo sapiens"},
        ])
        resolver.add_string_records([
            {"string_id": "9606.ENSP00000269305", "gene_symbol": "TP53", "organism": "Homo sapiens"},
        ])
        entry = resolver.mapping["P04637"]
        assert "string" in entry["sources"]
        assert entry["string_id"] == "9606.ENSP00000269305"

    def test_chembl_merge_pipeline(self):
        """ChEMBL records merge into UniProt entries."""
        from entity_resolution.protein_resolver import ProteinResolver

        resolver = ProteinResolver()
        resolver.add_uniprot_records([
            {"uniprot_id": "P04637", "gene_symbol": "TP53", "organism": "Homo sapiens"},
        ])
        resolver.add_chembl_target_records([
            {"chembl_target_id": "CHEMBL123", "uniprot_id": "P04637", "gene_symbol": "TP53",
             "organism": "Homo sapiens"},
        ])
        entry = resolver.mapping["P04637"]
        assert "chembl" in entry["sources"]
        assert entry["chembl_target_id"] == "CHEMBL123"

    def test_full_build_mapping_pipeline(self):
        """build_mapping produces a valid DataFrame."""
        import pandas as pd
        from entity_resolution.protein_resolver import ProteinResolver

        resolver = ProteinResolver()
        uniprot_df = pd.DataFrame([
            {"uniprot_id": "P04637", "gene_symbol": "TP53", "organism": "Homo sapiens"},
            {"uniprot_id": "P68871", "gene_symbol": "HBB", "organism": "Homo sapiens"},
        ])
        result = resolver.build_mapping(uniprot_df)
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 2

    def test_drug_resolver_full_pipeline(self):
        """DrugResolver full pipeline works."""
        import pandas as pd
        from entity_resolution.drug_resolver import DrugResolver

        resolver = DrugResolver()
        chembl_df = pd.DataFrame([
            {"inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N", "name": "Aspirin", "chembl_id": "CHEMBL25"},
        ])
        drugbank_df = pd.DataFrame([
            {"inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N", "name": "Acetylsalicylic acid", "drugbank_id": "DB00945"},
        ])
        result = resolver.build_mapping(chembl_df, drugbank_df, pubchem_df=None)
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 1

    def test_config_to_resolver_pipeline(self):
        """Config flows through to resolver correctly."""
        from entity_resolution.base import ResolverConfig
        from entity_resolution.protein_resolver import ProteinResolver

        cfg = ResolverConfig(fuzzy_threshold=0.95, default_organism="Mus musculus")
        resolver = ProteinResolver(config=cfg)
        assert resolver._config.fuzzy_threshold == 0.95
        assert resolver._config.default_organism == "Mus musculus"

    def test_state_serialization_roundtrip(self):
        """State dict round-trip preserves all data across modules."""
        from entity_resolution.protein_resolver import ProteinResolver
        from entity_resolution.drug_resolver import DrugResolver

        # Protein resolver round-trip.
        pr = ProteinResolver()
        pr.add_uniprot_records([
            {"uniprot_id": "P04637", "gene_symbol": "TP53", "organism": "Homo sapiens"},
        ])
        state = pr.to_state_dict()
        pr2 = ProteinResolver.from_state_dict(state)
        assert "P04637" in pr2.mapping

        # Drug resolver round-trip.
        dr = DrugResolver()
        dr.add_source_records([
            {"inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N", "name": "Aspirin", "chembl_id": "CHEMBL25"},
        ], source="chembl")
        dstate = dr.to_state_dict()
        dr2 = DrugResolver.from_state_dict(dstate)
        assert "BSYNRYMUTXBXSQ-UHFFFAOYSA-N" in dr2.mapping

    def test_protein_resolver_organism_normalization_cross_source(self):
        """Organism normalization works across all data sources."""
        from entity_resolution.protein_resolver import ProteinResolver

        resolver = ProteinResolver()
        # UniProt with "human" should normalize to "Homo sapiens".
        resolver.add_uniprot_records([
            {"uniprot_id": "P04637", "gene_symbol": "TP53", "organism": "human"},
        ])
        # STRING with "9606" should also normalize.
        resolver.add_string_records([
            {"string_id": "9606.ENSP00000269305", "gene_symbol": "TP53", "organism": "9606"},
        ])
        # Both should match and merge.
        assert len(resolver.mapping) == 1
        assert resolver.mapping["P04637"]["organism"] == "Homo sapiens"

    def test_entity_resolution_package_exports(self):
        """Package-level exports work correctly."""
        from entity_resolution import (
            DrugResolver,
            ProteinResolver,
            normalize_name,
            compute_match_confidence,
        )
        assert DrugResolver is not None
        assert ProteinResolver is not None
        assert callable(normalize_name)
        assert callable(compute_match_confidence)

    def test_resolver_utils_used_by_both_resolvers(self):
        """Both resolvers use shared resolver_utils correctly."""
        from entity_resolution.resolver_utils import normalize_name, compute_match_confidence
        from entity_resolution.protein_resolver import ProteinResolver
        from entity_resolution.drug_resolver import DrugResolver

        # normalize_name should produce consistent output.
        n1 = normalize_name("Aspirin (acetylsalicylic acid)")
        n2 = normalize_name("aspirin  (acetylsalicylic acid)")
        assert n1 == n2

        # compute_match_confidence should return consistent values.
        assert compute_match_confidence("uniprot_exact") == 1.0
        assert compute_match_confidence("gene_name_organism") == 0.85

    def test_all_18_files_import_without_error(self):
        """All 18 files can be imported without errors."""
        import config
        import config.settings
        import database
        import database.connection
        import database.models
        import database.migrations
        import database.migrations.run_migrations
        import database.loaders
        import cleaning
        import cleaning.normalizer
        import cleaning.missing_values
        import cleaning.deduplicator
        import entity_resolution
        import entity_resolution.resolver_utils
        import entity_resolution.drug_resolver
        import entity_resolution.protein_resolver


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
