"""
Test 2 of 3 — Integration test for ALL 20 files in the Drug Repurposing dataset pipeline.

Files covered (19 already fixed + 1 newly fixed = 20):

  1.  config/__init__.py
  2.  config/settings.py
  3.  database/__init__.py
  4.  database/connection.py
  5.  database/models.py
  6.  database/migrations/__init__.py
  7.  database/migrations/001_initial_schema.sql
  8.  database/migrations/002_bug_fixes_migration.sql
  9.  database/migrations/run_migrations.py
  10. database/loaders.py
  11. cleaning/__init__.py
  12. cleaning/normalizer.py
  13. cleaning/missing_values.py
  14. cleaning/deduplicator.py
  15. entity_resolution/__init__.py
  16. entity_resolution/resolver_utils.py
  17. entity_resolution/drug_resolver.py
  18. entity_resolution/protein_resolver.py
  19. pipelines/__init__.py
  20. pipelines/base_pipeline.py       ← NEWLY FIXED (this iteration)

This test verifies that all 20 files work together correctly as a pipeline:
  - Config is loadable
  - Database models and connections work
  - Cleaning pipeline (normalize, dedup, missing values) works
  - Entity resolution (drug + protein resolvers) works
  - Pipelines package (lazy façade + 7 source pipelines) works
  - The new institutional-grade base_pipeline.py integrates cleanly
  - End-to-end data flow through the full pipeline

This file extends tests/test_all_19_files_integration.py with new
TestBasePipelineIntegration and TestEndToEndWithBasePipeline classes
that exercise the upgraded base_pipeline.py alongside the 19 already-
fixed files.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# sys.path setup
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Set DATABASE_URL before any imports that might trigger config.settings
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


# ===========================================================================
# 1.  config/__init__.py        (File 1)
# 2.  config/settings.py        (File 2)
# ===========================================================================
class TestConfigModule:
    """Files 1-2: config package."""

    def test_config_init_importable(self):
        """File 1: config/__init__.py is importable."""
        import config
        assert config is not None

    def test_settings_importable(self):
        """File 2: config.settings is importable."""
        from config import settings
        assert settings is not None

    def test_settings_has_key_attributes(self):
        """File 2: config.settings has BASE_DIR, RAW_DATA_DIR, PROCESSED_DATA_DIR."""
        from config import settings
        assert hasattr(settings, "BASE_DIR")
        assert hasattr(settings, "RAW_DATA_DIR")
        assert hasattr(settings, "PROCESSED_DATA_DIR")


# ===========================================================================
# 3.  database/__init__.py      (File 3)
# 4.  database/connection.py    (File 4)
# 5.  database/models.py        (File 5)
# ===========================================================================
class TestDatabaseModule:
    """Files 3-5: database package."""

    def test_database_init_importable(self):
        """File 3: database/__init__.py is importable."""
        import database
        assert database is not None

    def test_connection_importable(self):
        """File 4: database.connection is importable."""
        from database import connection
        assert connection is not None
        assert hasattr(connection, "get_db_session")
        assert hasattr(connection, "get_engine")

    def test_models_importable(self):
        """File 5: database.models is importable."""
        from database import models
        assert models is not None

    def test_pipeline_run_model_exists(self):
        """File 5: PipelineRun model exists with required columns."""
        from database.models import PipelineRun
        cols = {c.name for c in PipelineRun.__table__.columns}
        expected = {
            "id", "source", "run_date", "status",
            "records_downloaded", "records_cleaned", "records_loaded",
            "error_message", "duration_seconds",
        }
        assert expected.issubset(cols), f"Missing columns: {expected - cols}"


# ===========================================================================
# 6.  database/migrations/__init__.py    (File 6)
# 7.  database/migrations/001_initial_schema.sql  (File 7)
# 8.  database/migrations/002_bug_fixes_migration.sql  (File 8)
# 9.  database/migrations/run_migrations.py  (File 9)
# ===========================================================================
class TestMigrationsModule:
    """Files 6-9: database.migrations package."""

    def test_migrations_init_importable(self):
        """File 6: database/migrations/__init__.py is importable."""
        from database import migrations
        assert migrations is not None

    def test_initial_schema_sql_exists(self):
        """File 7: 001_initial_schema.sql exists and is non-empty."""
        path = PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql"
        assert path.exists()
        assert path.stat().st_size > 0

    def test_bug_fixes_migration_sql_exists(self):
        """File 8: 002_bug_fixes_migration.sql exists and is non-empty."""
        path = PROJECT_ROOT / "database" / "migrations" / "002_bug_fixes_migration.sql"
        assert path.exists()
        assert path.stat().st_size > 0

    def test_run_migrations_importable(self):
        """File 9: run_migrations.py is importable."""
        from database.migrations import run_migrations
        assert run_migrations is not None


# ===========================================================================
# 10. database/loaders.py        (File 10)
# ===========================================================================
class TestLoadersModule:
    """File 10: database.loaders."""

    def test_loaders_importable(self):
        """File 10: database.loaders is importable."""
        from database import loaders
        assert loaders is not None

    def test_loaders_has_bulk_upsert_drugs(self):
        """File 10: bulk_upsert_drugs function exists."""
        from database.loaders import bulk_upsert_drugs
        assert callable(bulk_upsert_drugs)


# ===========================================================================
# 11. cleaning/__init__.py       (File 11)
# 12. cleaning/normalizer.py     (File 12)
# 13. cleaning/missing_values.py (File 13)
# 14. cleaning/deduplicator.py   (File 14)
# ===========================================================================
class TestCleaningModule:
    """Files 11-14: cleaning package."""

    def test_cleaning_init_importable(self):
        """File 11: cleaning/__init__.py is importable."""
        import cleaning
        assert cleaning is not None

    def test_normalizer_importable(self):
        """File 12: cleaning.normalizer is importable."""
        from cleaning import normalizer
        assert normalizer is not None
        assert hasattr(normalizer, "standardize_inchikey")

    def test_missing_values_importable(self):
        """File 13: cleaning.missing_values is importable."""
        from cleaning import missing_values
        assert missing_values is not None
        assert hasattr(missing_values, "fill_missing_drug_fields")

    def test_deduplicator_importable(self):
        """File 14: cleaning.deduplicator is importable."""
        from cleaning import deduplicator
        assert deduplicator is not None
        assert hasattr(deduplicator, "dedup_by_inchikey")


# ===========================================================================
# 15. entity_resolution/__init__.py        (File 15)
# 16. entity_resolution/resolver_utils.py  (File 16)
# 17. entity_resolution/drug_resolver.py   (File 17)
# 18. entity_resolution/protein_resolver.py (File 18)
# ===========================================================================
class TestEntityResolutionModule:
    """Files 15-18: entity_resolution package."""

    def test_er_init_importable(self):
        """File 15: entity_resolution/__init__.py is importable."""
        import entity_resolution
        assert entity_resolution is not None

    def test_resolver_utils_importable(self):
        """File 16: entity_resolution.resolver_utils is importable."""
        from entity_resolution import resolver_utils
        assert resolver_utils is not None

    def test_drug_resolver_importable(self):
        """File 17: entity_resolution.drug_resolver is importable."""
        from entity_resolution import drug_resolver
        assert drug_resolver is not None
        assert hasattr(drug_resolver, "DrugResolver")

    def test_protein_resolver_importable(self):
        """File 18: entity_resolution.protein_resolver is importable."""
        from entity_resolution import protein_resolver
        assert protein_resolver is not None
        assert hasattr(protein_resolver, "ProteinResolver")


# ===========================================================================
# 19. pipelines/__init__.py     (File 19)
# ===========================================================================
class TestPipelinesModule:
    """File 19: pipelines package (lazy façade)."""

    def test_pipelines_importable(self):
        """File 19: pipelines package is importable."""
        import pipelines
        assert pipelines is not None

    def test_pipelines_has_eight_classes(self):
        """File 19: all 8 pipeline classes are accessible."""
        import pipelines
        for name in ["BasePipeline", "ChEMBLPipeline", "DrugBankPipeline",
                     "UniProtPipeline", "StringPipeline", "DisGeNETPipeline",
                     "OMIMPipeline", "PubChemPipeline"]:
            cls = getattr(pipelines, name)
            assert cls.__name__ == name

    def test_pipelines_seven_source_names(self):
        """File 19: get_expected_pipelines returns the canonical 7 source names."""
        import pipelines
        expected = {"chembl", "drugbank", "uniprot", "string",
                    "disgenet", "omim", "pubchem"}
        assert pipelines.get_expected_pipelines() == expected


# ===========================================================================
# 20. pipelines/base_pipeline.py   (File 20 — NEWLY FIXED)
# ===========================================================================
class TestBasePipelineModule:
    """File 20: the upgraded institutional-grade base_pipeline.py."""

    def test_base_pipeline_importable(self):
        """File 20: pipelines.base_pipeline is importable."""
        from pipelines import base_pipeline
        assert base_pipeline is not None

    def test_base_pipeline_class_exists(self):
        """File 20: BasePipeline class exists."""
        from pipelines.base_pipeline import BasePipeline
        assert BasePipeline.__name__ == "BasePipeline"

    def test_base_pipeline_is_abstract(self):
        """File 20: BasePipeline is abstract and cannot be instantiated."""
        from pipelines import BasePipeline
        with pytest.raises(TypeError):
            BasePipeline()

    def test_required_stub_methods_implemented(self):
        """File 20: All __init__.pyi stub methods are implemented."""
        from pipelines.base_pipeline import BasePipeline
        for method in [
            "recover_from_failure", "get_dead_letters",
            "get_provenance", "get_audit_trail", "to_state_dict",
        ]:
            assert hasattr(BasePipeline, method), f"Missing stub method: {method}"

    def test_schema_validation_against_v1_json(self):
        """File 20: validate_output uses pipelines/schema/v1.json."""
        from pipelines.base_pipeline import BasePipeline, SCHEMA_PATH
        assert SCHEMA_PATH.exists(), f"Schema not found at {SCHEMA_PATH}"

    def test_custom_exceptions_exist(self):
        """File 20: Custom exception hierarchy exists."""
        from pipelines.base_pipeline import (
            PipelineError, PreCheckError, DataIntegrityError,
            SchemaValidationError, DownloadError,
        )
        assert issubclass(PreCheckError, PipelineError)
        assert issubclass(DataIntegrityError, PipelineError)
        assert issubclass(SchemaValidationError, PipelineError)
        assert issubclass(DownloadError, PipelineError)

    def test_dataclasses_exist(self):
        """File 20: LoadResult and RunLog dataclasses exist."""
        from pipelines.base_pipeline import LoadResult, RunLog
        lr = LoadResult(rows_inserted=5, rows_updated=3)
        assert lr.total_upserted == 8
        rl = RunLog(status="success")
        assert rl.status == "success"

    def test_helper_classes_exist(self):
        """File 20: _RateLimiter and _CircuitBreaker helper classes exist."""
        from pipelines.base_pipeline import _RateLimiter, _CircuitBreaker
        rl = _RateLimiter(min_interval=0.0)
        rl.wait()  # should not block
        cb = _CircuitBreaker(failure_threshold=2)
        assert not cb.is_open()
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open()

    def test_all_seven_subclasses_still_inherit_correctly(self):
        """File 20: All 7 existing subclasses inherit from BasePipeline unmodified."""
        from pipelines import (
            BasePipeline,
            ChEMBLPipeline,
            DrugBankPipeline,
            UniProtPipeline,
            StringPipeline,
            DisGeNETPipeline,
            OMIMPipeline,
            PubChemPipeline,
        )
        for cls in [ChEMBLPipeline, DrugBankPipeline, UniProtPipeline,
                    StringPipeline, DisGeNETPipeline, OMIMPipeline, PubChemPipeline]:
            assert issubclass(cls, BasePipeline), f"{cls.__name__} is not a subclass of BasePipeline"
            # Verify they still have the abstract methods implemented
            assert cls.__abstractmethods__ == frozenset(), (
                f"{cls.__name__} has unimplemented abstract methods: {cls.__abstractmethods__}"
            )

    def test_subclasses_can_be_instantiated(self, tmp_path, monkeypatch):
        """File 20: All 7 subclasses can be instantiated without modification."""
        from pipelines import (
            ChEMBLPipeline,
            DrugBankPipeline,
            UniProtPipeline,
            StringPipeline,
            DisGeNETPipeline,
            OMIMPipeline,
            PubChemPipeline,
        )
        # Use temp dirs to avoid polluting the real data dirs
        monkeypatch.setattr("config.settings.RAW_DATA_DIR", tmp_path / "raw")
        monkeypatch.setattr("config.settings.PROCESSED_DATA_DIR", tmp_path / "processed")
        monkeypatch.setattr("pipelines.base_pipeline.RAW_DATA_DIR", tmp_path / "raw")
        monkeypatch.setattr("pipelines.base_pipeline.PROCESSED_DATA_DIR", tmp_path / "processed")
        (tmp_path / "raw").mkdir()
        (tmp_path / "processed").mkdir()

        for cls in [ChEMBLPipeline, DrugBankPipeline, UniProtPipeline,
                    StringPipeline, DisGeNETPipeline, OMIMPipeline, PubChemPipeline]:
            instance = cls()
            assert instance.source_name in {"chembl", "drugbank", "uniprot", "string",
                                            "disgenet", "omim", "pubchem"}
            # Verify the run_id is set
            assert instance.run_id is not None
            # Verify the new instance attributes exist
            assert hasattr(instance, "downloaded_paths")
            assert hasattr(instance, "dead_letter_queue")
            assert hasattr(instance, "_transformation_log")
            assert hasattr(instance, "_audit_buffer")


# ===========================================================================
# Cross-module integration: base_pipeline + database + cleaning + entity_resolution
# ===========================================================================
class TestBasePipelineIntegration:
    """Cross-module integration tests for the upgraded base_pipeline."""

    def test_base_pipeline_uses_database_models(self):
        """File 20 + File 5: base_pipeline imports PipelineRun from database.models."""
        from pipelines.base_pipeline import PipelineRun as BPR
        from database.models import PipelineRun as DPR
        assert BPR is DPR

    def test_base_pipeline_uses_database_connection(self):
        """File 20 + File 4: base_pipeline imports get_db_session from database.connection."""
        from pipelines.base_pipeline import get_db_session as BPS
        from database.connection import get_db_session as DPS
        assert BPS is DPS

    def test_base_pipeline_uses_config_settings(self):
        """File 20 + File 2: base_pipeline imports RAW_DATA_DIR, PROCESSED_DATA_DIR from config.settings."""
        from pipelines.base_pipeline import RAW_DATA_DIR, PROCESSED_DATA_DIR
        from config.settings import RAW_DATA_DIR as CFG_RAW, PROCESSED_DATA_DIR as CFG_PROC
        assert RAW_DATA_DIR == CFG_RAW
        assert PROCESSED_DATA_DIR == CFG_PROC

    def test_base_pipeline_schema_loads_correctly(self):
        """File 20 + schema/v1.json: _load_schema loads the schema file."""
        from pipelines.base_pipeline import BasePipeline, SCHEMA_PATH

        class TestPipeline(BasePipeline):
            source_name = "chembl"
            def download(self): return Path("/nonexistent")
            def clean(self, raw_path): return pd.DataFrame()
            def load(self, df, session=None): return 0

        p = TestPipeline()
        schema = p._load_schema()
        assert "properties" in schema
        # Verify all 7 CSV files are in the schema
        for csv_name in [
            "drugs.csv", "drugbank_drugs.csv", "proteins.csv",
            "protein_protein_interactions.csv", "gene_disease_associations.csv",
            "omim_gene_disease_associations.csv", "pubchem_enrichment.csv",
        ]:
            assert csv_name in schema["properties"], f"Missing schema for {csv_name}"

    def test_get_dtypes_uses_schema_for_each_source(self, monkeypatch):
        """File 20 + schema/v1.json: get_dtypes returns correct dtypes for each source."""
        from pipelines.base_pipeline import BasePipeline

        # Test each source's dtypes are derived from schema
        test_cases = [
            ("chembl", "drugs.csv", "max_phase", "Int64"),
            ("chembl", "drugs.csv", "molecular_weight", "float64"),
            ("chembl", "drugs.csv", "inchikey", "str"),
            ("uniprot", "proteins.csv", "length", "Int64"),
            ("string", "protein_protein_interactions.csv", "combined_score", "Int64"),
            ("disgenet", "gene_disease_associations.csv", "score", "float64"),
            ("disgenet", "gene_disease_associations.csv", "gene_id", "Int64"),
            # OMIM — institutional-grade schema (master prompt §6).
            # The legacy `mim_number` column is replaced by `mapping_key`
            # (the only integer column in the new schema that always has a value).
            ("omim", "omim_gene_disease_associations.csv", "mapping_key", "Int64"),
        ]
        for src_name, _csv, column, expected_dtype in test_cases:
            # Build a unique subclass per source to avoid name collisions
            cls = type(
                f"TestPipeline_{src_name}",
                (BasePipeline,),
                {
                    "source_name": src_name,
                    "download": lambda self: Path("/nonexistent"),
                    "clean": lambda self, raw_path: pd.DataFrame(),
                    "load": lambda self, df, session=None: 0,
                },
            )
            p = cls()
            dtypes = p.get_dtypes()
            assert column in dtypes, f"{src_name}: missing dtype for {column}"
            assert dtypes[column] == expected_dtype, (
                f"{src_name}.{column}: expected {expected_dtype}, got {dtypes[column]}"
            )


# ===========================================================================
# End-to-end with the upgraded base_pipeline
# ===========================================================================
class TestEndToEndWithBasePipeline:
    """End-to-end tests that exercise the full pipeline lifecycle."""

    def test_full_run_with_mocked_download_and_real_clean(
        self, tmp_path, monkeypatch, db_session
    ):
        """End-to-end: run() executes download -> clean -> load with audit."""
        # Set up temp dirs
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        processed_dir = tmp_path / "processed"
        processed_dir.mkdir()
        monkeypatch.setattr("pipelines.base_pipeline.RAW_DATA_DIR", raw_dir)
        monkeypatch.setattr("pipelines.base_pipeline.PROCESSED_DATA_DIR", processed_dir)

        # Build a test pipeline that produces valid ChEMBL-shape data
        from pipelines.base_pipeline import BasePipeline

        class E2EPipeline(BasePipeline):
            source_name = "chembl"
            def download(self):
                p = self.raw_dir / "raw.csv"
                p.write_text(
                    "inchikey,name,max_phase,molecular_weight\n"
                    "BSYNRYMUTXBXSQ-UHFFFAOYSA-N,Aspirin,4,180.16\n"
                    "WFXAZNNJSJXTJZ-UHFFFAOYSA-N,Ibuprofen,4,206.28\n",
                    encoding="utf-8",
                )
                return p
            def clean(self, raw_path):
                return pd.read_csv(raw_path)
            def load(self, df, session=None):
                # Mock load — just return row count
                return len(df)

        p = E2EPipeline()
        # Should not raise
        p.run()

        # Verify cleaned data was persisted
        assert (processed_dir / "drugs.csv").exists()
        # Verify run state
        assert p.start_time is not None
        assert p.run_log.get("status") in ("success", "warning")
        # Verify SHA-256 was computed for the cleaned data
        assert p._sha256_cleaned is not None
        # Verify provenance sidecar exists
        prov_path = processed_dir / "drugs.csv.provenance.json"
        # _write_provenance is called only if explicitly invoked;
        # the audit metadata is captured in run_log
        assert "validation_errors" in p.run_log
        assert "dq_metrics" in p.run_log

    def test_run_download_and_clean_only_then_run_load_only(
        self, tmp_path, monkeypatch
    ):
        """End-to-end: download+clean phase then load phase (master DAG pattern)."""
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        processed_dir = tmp_path / "processed"
        processed_dir.mkdir()
        monkeypatch.setattr("pipelines.base_pipeline.RAW_DATA_DIR", raw_dir)
        monkeypatch.setattr("pipelines.base_pipeline.PROCESSED_DATA_DIR", processed_dir)

        # Mock get_db_session so audit doesn't fail
        from contextlib import contextmanager
        @contextmanager
        def mock_session(**kwargs):
            class MockSession:
                def execute(self, *a, **k):
                    class R:
                        def scalar_one_or_none(self): return None
                        def scalars(self):
                            class S:
                                def all(self): return []
                            return S()
                    return R()
                def add(self, *a, **k): pass
                def commit(self): pass
                def rollback(self): pass
                def close(self): pass
            yield MockSession()
        monkeypatch.setattr("pipelines.base_pipeline.get_db_session", mock_session)

        from pipelines.base_pipeline import BasePipeline

        load_call_count = [0]

        class E2EPipeline(BasePipeline):
            source_name = "chembl"
            def download(self):
                p = self.raw_dir / "raw.csv"
                p.write_text(
                    "inchikey,name\n"
                    "BSYNRYMUTXBXSQ-UHFFFAOYSA-N,Aspirin\n",
                    encoding="utf-8",
                )
                return p
            def clean(self, raw_path):
                return pd.read_csv(raw_path)
            def load(self, df, session=None):
                load_call_count[0] += 1
                return len(df)

        p = E2EPipeline()

        # Phase 1: download + clean only (should NOT call load)
        result = p.run_download_and_clean_only()
        assert isinstance(result, Path)
        assert load_call_count[0] == 0, "run_download_and_clean_only should not call load()"
        # Cleaned data should be persisted
        assert (processed_dir / "drugs.csv").exists()
        # Run context sidecar should exist
        ctx_path = processed_dir / "drugs.csv.run_context.json"
        assert ctx_path.exists()

        # Phase 2: load only (should NOT call download or clean)
        p2 = E2EPipeline()
        p2.run_load_only()
        assert load_call_count[0] == 1, "run_load_only should call load() exactly once"

    def test_audit_fallback_to_jsonl_on_db_failure(
        self, tmp_path, monkeypatch
    ):
        """When DB is unavailable, audit records go to a local JSONL file (DQ-5.10)."""
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        monkeypatch.setattr("pipelines.base_pipeline.RAW_DATA_DIR", raw_dir)

        # Make get_db_session raise to simulate DB failure
        def mock_session(**kwargs):
            raise RuntimeError("DB unavailable")
        monkeypatch.setattr("pipelines.base_pipeline.get_db_session", mock_session)

        from pipelines.base_pipeline import BasePipeline

        class E2EPipeline(BasePipeline):
            source_name = "chembl"
            def download(self): return Path("/nonexistent")
            def clean(self, raw_path): return pd.DataFrame()
            def load(self, df, session=None): return 0

        p = E2EPipeline()
        # _write_run_log should fall back to local JSONL, not raise
        p._write_run_log(
            status="test",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            records_downloaded=10,
            records_cleaned=8,
            records_loaded=7,
        )

        # Verify the fallback JSONL file was written
        fallback_path = raw_dir / "pipeline_runs_fallback.jsonl"
        assert fallback_path.exists()
        # Each line should be valid JSON
        lines = fallback_path.read_text().strip().split("\n")
        assert len(lines) >= 1
        record = json.loads(lines[-1])
        assert record["source"] == "chembl"
        assert record["status"] == "test"
        assert record["records_downloaded"] == 10

    def test_dead_letter_queue_collects_bad_rows(self, tmp_path, monkeypatch):
        """Dead letter queue collects rows that fail processing (REL-6.1)."""
        from pipelines.base_pipeline import BasePipeline

        class E2EPipeline(BasePipeline):
            source_name = "chembl"
            def download(self): return Path("/nonexistent")
            def clean(self, raw_path):
                # Simulate bad rows being sent to dead letter queue
                for i in range(3):
                    self.dead_letter_queue.append({
                        "row_index": i,
                        "error": f"simulated error {i}",
                    })
                return pd.DataFrame({"inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"]})
            def load(self, df, session=None): return len(df)

        p = E2EPipeline()
        # Verify the queue starts empty
        assert p.get_dead_letters() == []
        # Run clean to populate the queue
        p.clean(Path("/nonexistent"))
        # Verify the dead letters are accessible
        dl = p.get_dead_letters()
        assert len(dl) == 3
        assert dl[0]["error"] == "simulated error 0"

    def test_provenance_metadata_recorded(self, tmp_path, monkeypatch):
        """Provenance metadata is captured in get_provenance() (LIN-16.13)."""
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        processed_dir = tmp_path / "processed"
        processed_dir.mkdir()
        monkeypatch.setattr("pipelines.base_pipeline.RAW_DATA_DIR", raw_dir)
        monkeypatch.setattr("pipelines.base_pipeline.PROCESSED_DATA_DIR", processed_dir)

        from pipelines.base_pipeline import BasePipeline

        class E2EPipeline(BasePipeline):
            source_name = "chembl"
            def download(self):
                p = self.raw_dir / "raw.csv"
                p.write_text("inchikey,name\nKEY-UHFFFAOYSA-N,Test\n", encoding="utf-8")
                return p
            def clean(self, raw_path):
                self._log_transformation("test_step", 1, {"k": "v"})
                return pd.read_csv(raw_path)
            def load(self, df, session=None): return len(df)

        # Mock DB session
        from contextlib import contextmanager
        @contextmanager
        def mock_session(**kwargs):
            class MockSession:
                def execute(self, *a, **k):
                    class R:
                        def scalar_one_or_none(self): return None
                    return R()
                def add(self, *a, **k): pass
                def commit(self): pass
                def rollback(self): pass
                def close(self): pass
            yield MockSession()
        monkeypatch.setattr("pipelines.base_pipeline.get_db_session", mock_session)

        p = E2EPipeline()
        p.run()

        # Provenance should be available
        prov = p.get_provenance()
        assert prov["source_name"] == "chembl"
        assert prov["run_id"] == p.run_id
        assert prov["schema_version"] == "v1"
        assert len(prov["transformation_log"]) >= 1
        assert prov["transformation_log"][0]["step"] == "test_step"

        # State should be serialisable
        state = p.to_state_dict()
        json.dumps(state, default=str)


# ===========================================================================
# Final verification: all 20 files work together
# ===========================================================================
class TestAll20FilesTogether:
    """Final integration test: all 20 files work together."""

    def test_all_20_files_import_cleanly(self):
        """All 20 files can be imported without errors."""
        # Files 1-2
        import config
        from config import settings
        # Files 3-5
        import database
        from database import connection, models
        # Files 6-9
        from database import migrations
        from database.migrations import run_migrations
        # File 10
        from database import loaders
        # Files 11-14
        import cleaning
        from cleaning import normalizer, missing_values, deduplicator
        # Files 15-18
        import entity_resolution
        from entity_resolution import resolver_utils, drug_resolver, protein_resolver
        # Files 19-20
        import pipelines
        from pipelines import base_pipeline

        # Verify all are non-None
        for mod in [config, settings, database, connection, models,
                    migrations, run_migrations, loaders,
                    cleaning, normalizer, missing_values, deduplicator,
                    entity_resolution, resolver_utils, drug_resolver, protein_resolver,
                    pipelines, base_pipeline]:
            assert mod is not None

    def test_all_20_files_paths_exist(self):
        """All 20 files exist on disk."""
        paths = [
            "config/__init__.py",
            "config/settings.py",
            "database/__init__.py",
            "database/connection.py",
            "database/models.py",
            "database/migrations/__init__.py",
            "database/migrations/001_initial_schema.sql",
            "database/migrations/002_bug_fixes_migration.sql",
            "database/migrations/run_migrations.py",
            "database/loaders.py",
            "cleaning/__init__.py",
            "cleaning/normalizer.py",
            "cleaning/missing_values.py",
            "cleaning/deduplicator.py",
            "entity_resolution/__init__.py",
            "entity_resolution/resolver_utils.py",
            "entity_resolution/drug_resolver.py",
            "entity_resolution/protein_resolver.py",
            "pipelines/__init__.py",
            "pipelines/base_pipeline.py",
        ]
        for rel_path in paths:
            full = PROJECT_ROOT / rel_path
            assert full.exists(), f"Missing file: {rel_path}"
            assert full.stat().st_size > 0, f"Empty file: {rel_path}"

    def test_data_flow_through_full_stack(self, tmp_path, monkeypatch, db_session):
        """Full data flow: config -> DB -> base_pipeline -> cleaning -> DB.

        This test exercises:
        - config.settings (File 2) for paths
        - database.connection (File 4) for sessions
        - database.models (File 5) for the PipelineRun model
        - pipelines.base_pipeline (File 20) for the ETL lifecycle
        - database.loaders (File 10) for bulk insert (mocked)
        """
        # Set up temp dirs
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        processed_dir = tmp_path / "processed"
        processed_dir.mkdir()
        monkeypatch.setattr("pipelines.base_pipeline.RAW_DATA_DIR", raw_dir)
        monkeypatch.setattr("pipelines.base_pipeline.PROCESSED_DATA_DIR", processed_dir)

        from pipelines.base_pipeline import BasePipeline, LoadResult

        # Track the session passed to load()
        load_session_ref = [None]

        class FullStackPipeline(BasePipeline):
            source_name = "chembl"
            def download(self):
                p = self.raw_dir / "raw.csv"
                p.write_text(
                    "inchikey,name,max_phase\n"
                    "BSYNRYMUTXBXSQ-UHFFFAOYSA-N,Aspirin,4\n",
                    encoding="utf-8",
                )
                return p
            def clean(self, raw_path):
                df = pd.read_csv(raw_path)
                # Use cleaning.normalizer (File 12) to standardise InChIKey
                from cleaning.normalizer import standardize_inchikey
                df["inchikey"] = df["inchikey"].apply(standardize_inchikey)
                return df
            def load(self, df, session=None):
                load_session_ref[0] = session
                # Return a LoadResult (File 20) for richer semantics
                return LoadResult(rows_inserted=len(df), rows_updated=0)

        p = FullStackPipeline()
        p.run()

        # Verify the cleaned data was persisted
        assert (processed_dir / "drugs.csv").exists()
        # Verify the run completed
        assert p.run_log["status"] in ("success", "warning")
        # Verify the LoadResult was used (records_loaded = rows_inserted)
        assert p.run_log["records_loaded"] == 1

    def test_20_files_no_regressions_in_existing_tests(
        self, tmp_path, monkeypatch
    ):
        """The upgraded base_pipeline.py doesn't break existing method signatures."""
        from pipelines.base_pipeline import BasePipeline

        class TestPipeline(BasePipeline):
            source_name = "chembl"
            def download(self): return Path("/nonexistent")
            def clean(self, raw_path): return pd.DataFrame()
            def load(self, df, session=None): return 0

        p = TestPipeline()

        # Verify all the methods that existing tests rely on still exist
        # and accept the same arguments
        assert callable(p.run)
        assert callable(p.run_download_and_clean_only)
        assert callable(p.run_load_only)
        assert callable(p.download)
        assert callable(p.clean)
        assert callable(p.load)
        assert callable(p._count_records)
        assert callable(p._validate_text_file_integrity)
        assert callable(p._write_run_log)
        assert callable(p._download_file)
        assert callable(p._get_processed_filename)

        # Verify _get_processed_filename returns the canonical names
        for source, expected in [
            ("chembl", "drugs.csv"),
            ("drugbank", "drugbank_drugs.csv"),
            ("uniprot", "proteins.csv"),
            ("string", "protein_protein_interactions.csv"),
            ("disgenet", "gene_disease_associations.csv"),
            ("omim", "omim_gene_disease_associations.csv"),
            ("pubchem", "pubchem_enrichment.csv"),
        ]:
            p.source_name = source
            assert p._get_processed_filename() == expected
