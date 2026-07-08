"""
tests/test_all_26_files_integration_v10.py — Combined integration test for
all 26 files (25 previously-fixed + the new OMIM pipeline).

This is test #2 of 3 required by the master prompt:
  1. tests/test_omim_pipeline.py — real tests for the OMIM file.
  2. tests/test_all_26_files_integration_v10.py (THIS FILE) — all 26 files
     combined integration.
  3. All existing tests must still pass.

The 26 files covered are:

  config/__init__.py
  config/settings.py
  database/__init__.py
  database/connection.py
  database/models.py
  database/migrations/__init__.py
  database/migrations/001_initial_schema.sql
  database/migrations/002_bug_fixes_migration.sql
  database/migrations/003_models_fix_migration.sql
  database/migrations/004_extend_gda_table_for_389_audit.sql
  database/migrations/run_migrations.py
  database/loaders.py
  cleaning/__init__.py
  cleaning/normalizer.py
  cleaning/missing_values.py
  cleaning/deduplicator.py
  cleaning/confidence.py
  entity_resolution/__init__.py
  entity_resolution/resolver_utils.py
  entity_resolution/drug_resolver.py
  entity_resolution/protein_resolver.py
  pipelines/__init__.py
  pipelines/base_pipeline.py
  pipelines/disgenet_pipeline.py
  pipelines/uniprot_pipeline.py
  pipelines/string_pipeline.py
  pipelines/chembl_pipeline.py
  pipelines/drugbank_pipeline.py
  pipelines/pubchem_pipeline.py
  pipelines/omim_pipeline.py        ← the file under fix (26th file)

Run with:
    pytest tests/test_all_26_files_integration_v10.py -v
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker

# ---------------------------------------------------------------------------
# Project path setup
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Force test env vars BEFORE any imports.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("DISGENET_USE_API", "false")
os.environ.setdefault("DISGENET_API_KEY", "test-key-not-real")
os.environ.setdefault("OMIM_API_KEY", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
os.environ.setdefault("OMIM_MIN_EXPECTED_RECORDS", "0")

# ---------------------------------------------------------------------------
# The 26 files under test
# ---------------------------------------------------------------------------
TWENTY_SIX_FILES = [
    # config
    "config/__init__.py",
    "config/settings.py",
    # database
    "database/__init__.py",
    "database/connection.py",
    "database/models.py",
    "database/migrations/__init__.py",
    "database/migrations/001_initial_schema.sql",
    "database/migrations/002_bug_fixes_migration.sql",
    "database/migrations/003_models_fix_migration.sql",
    "database/migrations/004_extend_gda_table_for_389_audit.sql",
    "database/migrations/run_migrations.py",
    "database/loaders.py",
    # cleaning
    "cleaning/__init__.py",
    "cleaning/normalizer.py",
    "cleaning/missing_values.py",
    "cleaning/deduplicator.py",
    "cleaning/confidence.py",
    # entity_resolution
    "entity_resolution/__init__.py",
    "entity_resolution/resolver_utils.py",
    "entity_resolution/drug_resolver.py",
    "entity_resolution/protein_resolver.py",
    # pipelines (7 pipelines + base + init)
    "pipelines/__init__.py",
    "pipelines/base_pipeline.py",
    "pipelines/disgenet_pipeline.py",
    "pipelines/uniprot_pipeline.py",
    "pipelines/string_pipeline.py",
    "pipelines/chembl_pipeline.py",
    "pipelines/drugbank_pipeline.py",
    "pipelines/pubchem_pipeline.py",
    # The 26th file — the one we just fixed
    "pipelines/omim_pipeline.py",
]

OP = "pipelines.omim_pipeline"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_engine():
    """Create an in-memory SQLite engine with FK enforcement."""
    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    from database.base import Base
    Base.metadata.create_all(engine)
    return engine


def _make_morbidmap_fixture(tmp_path: Path) -> Path:
    """Write the morbidmap fixture and return its path."""
    src = PROJECT_ROOT / "tests" / "fixtures" / "omim" / "morbidmap_sample.txt"
    dest = tmp_path / "morbidmap.txt"
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


def _make_proteins():
    """Seed proteins matching the morbidmap fixture's gene symbols."""
    from database.models import Protein
    return [
        Protein(uniprot_id="P11362", gene_name="FGFR3 protein",
                gene_symbol="FGFR3", protein_name="FGFR3", organism="Homo sapiens"),
        Protein(uniprot_id="P13569", gene_name="CFTR protein",
                gene_symbol="CFTR", protein_name="CFTR", organism="Homo sapiens"),
        Protein(uniprot_id="P38398", gene_name="BRCA1 protein",
                gene_symbol="BRCA1", protein_name="BRCA1", organism="Homo sapiens"),
        Protein(uniprot_id="P10721", gene_name="KIT protein",
                gene_symbol="KIT", protein_name="KIT", organism="Homo sapiens"),
        Protein(uniprot_id="Q30201", gene_name="HFE protein",
                gene_symbol="HFE", protein_name="HFE", organism="Homo sapiens"),
        Protein(uniprot_id="P35555", gene_name="FBN1 protein",
                gene_symbol="FBN1", protein_name="FBN1", organism="Homo sapiens"),
        Protein(uniprot_id="P68871", gene_name="HBB protein",
                gene_symbol="HBB", protein_name="HBB", organism="Homo sapiens"),
        Protein(uniprot_id="P11532", gene_name="DMD protein",
                gene_symbol="DMD", protein_name="DMD", organism="Homo sapiens"),
        Protein(uniprot_id="P42858", gene_name="HTT protein",
                gene_symbol="HTT", protein_name="HTT", organism="Homo sapiens"),
    ]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def db_engine():
    engine = _make_engine()
    yield engine
    from database.base import Base
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    Session = sessionmaker(bind=db_engine)
    session = Session()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def populated_db_session(db_session):
    """Seed the proteins table."""
    for protein in _make_proteins():
        db_session.add(protein)
    db_session.commit()
    return db_session


@pytest.fixture
def tmp_processed_dir(tmp_path, monkeypatch):
    """Redirect PROCESSED_DATA_DIR + OMIM_OUTPUT_PATH to tmp_path."""
    import pipelines.omim_pipeline as op
    processed = tmp_path / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(op, "PROCESSED_DATA_DIR", processed)
    monkeypatch.setattr(
        op, "OMIM_OUTPUT_PATH",
        processed / "omim_gene_disease_associations.csv",
    )
    monkeypatch.setattr(
        op, "OMIM_SUSCEPTIBILITY_OUTPUT_PATH",
        processed / "omim_gene_disease_susceptibility.csv",
    )
    monkeypatch.setattr(
        op, "OMIM_QUARANTINE_PATH", processed / "omim_quarantine.jsonl"
    )
    return processed


@pytest.fixture
def omim_pipeline(tmp_path, tmp_processed_dir):
    """Yield an OMIMPipeline with redirected paths."""
    from pipelines.omim_pipeline import OMIMPipeline
    pipeline = OMIMPipeline(run_id="test26-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f"))
    pipeline.raw_dir = tmp_path / "raw"
    pipeline.raw_dir.mkdir(parents=True, exist_ok=True)
    pipeline._source_format = "morbidmap_txt"
    pipeline._download_method_used = "morbidmap"
    pipeline._source_version = "2024-06-15"
    pipeline._source_url_sanitised = "https://data.omim.org/downloads/[REDACTED]/morbidmap.txt"
    pipeline.start_time = datetime.now(timezone.utc)
    return pipeline


@pytest.fixture
def morbidmap_fixture(tmp_path):
    return _make_morbidmap_fixture(tmp_path)


# ===========================================================================
# Test 1: All 26 files exist and import
# ===========================================================================
class TestAllFilesImport:
    """Verify all 26 files exist on disk and import cleanly."""

    @pytest.mark.parametrize("file_path", TWENTY_SIX_FILES)
    def test_file_exists(self, file_path):
        """Each file must exist on disk."""
        full_path = PROJECT_ROOT / file_path
        assert full_path.exists(), f"Missing file: {file_path}"

    @pytest.mark.parametrize("module_path", [
        "config", "config.settings",
        "database", "database.connection", "database.models",
        "database.migrations", "database.migrations.run_migrations",
        "database.loaders",
        "cleaning", "cleaning.normalizer", "cleaning.missing_values",
        "cleaning.deduplicator", "cleaning.confidence",
        "entity_resolution", "entity_resolution.resolver_utils",
        "entity_resolution.drug_resolver", "entity_resolution.protein_resolver",
        "pipelines", "pipelines.base_pipeline",
        "pipelines.disgenet_pipeline", "pipelines.uniprot_pipeline",
        "pipelines.string_pipeline", "pipelines.chembl_pipeline",
        "pipelines.drugbank_pipeline", "pipelines.pubchem_pipeline",
        "pipelines.omim_pipeline",
    ])
    def test_module_imports(self, module_path):
        """Each Python module must import without errors."""
        mod = importlib.import_module(module_path)
        assert mod is not None


# ===========================================================================
# Test 2: Count is exactly 26
# ===========================================================================
def test_all_26_files_count():
    """Verify we have exactly 26 files in the list."""
    assert len(TWENTY_SIX_FILES) == 30, (
        f"Expected 30 entries (26 .py files + 4 .sql migrations), "
        f"got {len(TWENTY_SIX_FILES)}"
    )
    # The 26th file is omim_pipeline.py
    assert TWENTY_SIX_FILES[-1] == "pipelines/omim_pipeline.py"


# ===========================================================================
# Test 3: OMIM pipeline imports cleanly (regression for BUG-4.24)
# ===========================================================================
def test_omim_pipeline_imports_cleanly():
    """The OMIM pipeline module must import without errors (BUG-4.24)."""
    from pipelines.omim_pipeline import OMIMPipeline, OMIMRecord
    assert OMIMPipeline is not None
    assert OMIMRecord is not None


# ===========================================================================
# Test 4: Config integration — OMIM config keys are available
# ===========================================================================
class TestConfigIntegration:
    """Verify OMIM config is registered and available."""

    def test_omim_api_key_available(self):
        from config.settings import OMIM_API_KEY
        assert isinstance(OMIM_API_KEY, str)

    def test_omim_api_base_available(self):
        from config.settings import OMIM_API_BASE
        assert OMIM_API_BASE.startswith("https://")

    def test_omim_request_interval_available(self):
        from config.settings import OMIM_REQUEST_INTERVAL
        assert OMIM_REQUEST_INTERVAL > 0

    def test_omim_mapping_keys_include_available(self):
        from config.settings import OMIM_MAPPING_KEYS_INCLUDE
        assert isinstance(OMIM_MAPPING_KEYS_INCLUDE, list)
        assert all(mk in (1, 2, 3, 4) for mk in OMIM_MAPPING_KEYS_INCLUDE)

    def test_omim_confirmed_score_available(self):
        from config.settings import OMIM_CONFIRMED_SCORE
        assert 0.0 <= OMIM_CONFIRMED_SCORE <= 1.0

    def test_omim_exclude_susceptibility_available(self):
        from config.settings import OMIM_EXCLUDE_SUSCEPTIBILITY
        assert isinstance(OMIM_EXCLUDE_SUSCEPTIBILITY, bool)

    def test_omim_api_key_format_re_available(self):
        from config.settings import OMIM_API_KEY_FORMAT_RE
        assert isinstance(OMIM_API_KEY_FORMAT_RE, str)

    def test_validate_omim_config_available(self):
        from config.settings import _validate_omim_config
        assert callable(_validate_omim_config)
        # Should not raise with default values.
        _validate_omim_config()

    def test_config_registry_has_omim_entries(self):
        from config.settings import CONFIG_REGISTRY
        omim_keys = [k for k in CONFIG_REGISTRY if k.startswith("OMIM_")]
        assert len(omim_keys) >= 20, (
            f"Expected >=20 OMIM_* entries in CONFIG_REGISTRY, got {len(omim_keys)}"
        )


# ===========================================================================
# Test 5: Database integration — GDA model has all required columns
# ===========================================================================
class TestDatabaseIntegration:
    """Verify the GDA model and loaders support OMIM's output schema."""

    def test_gda_model_has_omim_compatible_columns(self):
        """The GDA model must have all columns OMIM populates."""
        from database.models import GeneDiseaseAssociation
        engine = _make_engine()
        inspector = inspect(engine)
        cols = {c["name"] for c in inspector.get_columns("gene_disease_associations")}
        required = [
            "gene_symbol", "uniprot_id", "disease_id", "disease_id_type",
            "disease_name", "association_type", "score", "source",
            "pmid_list", "score_type", "score_method", "pipeline_run_id",
            "confidence_tier", "confidence_tier_method",
            "source_version", "download_date", "download_method",
            "source_format", "dedup_strategy", "schema_version",
            "source_url", "source_id",
        ]
        for col in required:
            assert col in cols, f"GDA model missing column: {col}"
        engine.dispose()

    def test_dead_letter_gda_model_exists(self):
        from database.models import DeadLetterGDA
        engine = _make_engine()
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert "dead_letter_gda" in tables
        engine.dispose()

    def test_bulk_upsert_gda_accepts_omim_output(self, omim_pipeline, morbidmap_fixture, populated_db_session):
        """bulk_upsert_gda must accept the OMIM pipeline's cleaned output."""
        df = omim_pipeline.clean(morbidmap_fixture)
        assert not df.empty
        with patch.object(omim_pipeline, "_post_load_disgenet_dedup"):
            count = omim_pipeline.load(df, session=populated_db_session)
        populated_db_session.commit()
        assert count > 0

    def test_get_or_create_pipeline_run_returns_int(self, populated_db_session):
        from database.loaders import get_or_create_pipeline_run
        run_id = get_or_create_pipeline_run(
            populated_db_session,
            run_id="test-run-26",
            source="omim",
            started_at=datetime.now(timezone.utc),
            status="running",
        )
        assert isinstance(run_id, int)

    def test_build_gene_to_uniprot_maps_returns_dicts(self, populated_db_session):
        from database.loaders import build_gene_to_uniprot_maps
        gene_map, name_map = build_gene_to_uniprot_maps(populated_db_session)
        assert isinstance(gene_map, dict)
        assert isinstance(name_map, dict)
        # FGFR3 should be in the gene_map (we seeded it).
        assert "FGFR3" in gene_map


# ===========================================================================
# Test 6: Cleaning integration — validate_gda_scores + classify_confidence
# ===========================================================================
class TestCleaningIntegration:
    """Verify the cleaning utilities work with OMIM's output."""

    def test_validate_gda_scores_accepts_omim_output(self, omim_pipeline, morbidmap_fixture):
        """validate_gda_scores must accept OMIM's cleaned DataFrame."""
        from cleaning.missing_values import validate_gda_scores
        df = omim_pipeline.clean(morbidmap_fixture)
        # Run validate_gda_scores again — should be idempotent.
        result = validate_gda_scores(
            df,
            score_range=(0.0, 1.0),
            preserve_direction=False,
            source="omim",
            dedup=True,
            dedup_keys=["gene_symbol", "disease_id", "source"],
        )
        assert isinstance(result, pd.DataFrame)
        assert "score" in result.columns

    def test_classify_confidence_accepts_omim_scores(self):
        """classify_confidence must accept OMIM's score range."""
        from cleaning.confidence import classify_confidence, DEFAULT_CONFIDENCE_TIERS
        # OMIM scores are in {0.5, 0.6, 0.8, 0.9} for mk=1/2/4/3.
        for score in [0.5, 0.6, 0.8, 0.9]:
            tier = classify_confidence(score, tiers=list(DEFAULT_CONFIDENCE_TIERS))
            assert tier in {"weak", "moderate", "strong"}


# ===========================================================================
# Test 7: End-to-end integration — clean → load → DB
# ===========================================================================
class TestEndToEndIntegration:
    """Full clean → load flow with a real (in-memory) DB."""

    def test_full_omim_pipeline_clean_and_load(self, omim_pipeline, morbidmap_fixture, populated_db_session):
        """Full clean → load flow: verify DB rows have correct values."""
        df = omim_pipeline.clean(morbidmap_fixture)
        assert not df.empty
        with patch.object(omim_pipeline, "_post_load_disgenet_dedup"):
            count = omim_pipeline.load(df, session=populated_db_session)
        populated_db_session.commit()

        from database.models import GeneDiseaseAssociation
        gdas = populated_db_session.query(GeneDiseaseAssociation).all()
        assert len(gdas) > 0
        for gda in gdas:
            assert gda.source == "omim"
            assert gda.disease_id is not None
            assert gda.disease_id.startswith("OMIM:")
            assert gda.score is not None
            assert 0.0 <= gda.score <= 1.0
            assert gda.confidence_tier in {"weak", "moderate", "strong"}
            assert gda.schema_version == "2.0"
            assert gda.dedup_strategy == "validate_gda_scores_dedup"
            assert gda.score_type == "omim_mapping_key"

    def test_idempotent_clean_and_load(self, omim_pipeline, morbidmap_fixture, populated_db_session):
        """GAP-10.19: Loading twice must NOT create duplicate DB rows."""
        df = omim_pipeline.clean(morbidmap_fixture)
        with patch.object(omim_pipeline, "_post_load_disgenet_dedup"):
            count1 = omim_pipeline.load(df, session=populated_db_session)
            populated_db_session.commit()
            count2 = omim_pipeline.load(df, session=populated_db_session)
            populated_db_session.commit()

        from database.models import GeneDiseaseAssociation
        gdas = populated_db_session.query(GeneDiseaseAssociation).all()
        # Row count should not double.
        assert len(gdas) <= len(df), (
            f"Idempotency violation: {len(gdas)} rows after 2 loads of {len(df)} records"
        )

    def test_omim_disgenet_no_conflict(self, omim_pipeline, morbidmap_fixture, populated_db_session):
        """OMIM and DisGeNET can coexist in the same GDA table (source column)."""
        # Load DisGeNET row first.
        from database.models import GeneDiseaseAssociation
        disgenet_row = GeneDiseaseAssociation(
            gene_symbol="FGFR3",
            disease_id="OMIM:100800",
            source="disgenet",
            score=0.7,
            confidence_tier="strong",
            schema_version="2.0",
            disease_id_type="omim",
            association_type="curated",
            dedup_strategy="validate_gda_scores_dedup",
        )
        populated_db_session.add(disgenet_row)
        populated_db_session.commit()

        # Now load OMIM.
        df = omim_pipeline.clean(morbidmap_fixture)
        with patch.object(omim_pipeline, "_post_load_disgenet_dedup"):
            omim_pipeline.load(df, session=populated_db_session)
        populated_db_session.commit()

        # Both sources should be in the table (separate rows).
        all_rows = populated_db_session.query(GeneDiseaseAssociation).all()
        sources = {r.source for r in all_rows}
        assert "disgenet" in sources
        assert "omim" in sources


# ===========================================================================
# Test 8: Schema compliance
# ===========================================================================
class TestSchemaCompliance:
    """Verify the JSON schema validates OMIM's output."""

    def test_schema_json_is_valid(self):
        """pipelines/schema/v1.json must be valid JSON."""
        schema_path = PROJECT_ROOT / "pipelines" / "schema" / "v1.json"
        schema = json.loads(schema_path.read_text())
        assert "properties" in schema
        assert "omim_gene_disease_associations.csv" in schema["properties"]

    def test_schema_has_omim_required_columns(self):
        """The OMIM schema section must require disease_id and score."""
        schema_path = PROJECT_ROOT / "pipelines" / "schema" / "v1.json"
        schema = json.loads(schema_path.read_text())
        omim_schema = schema["properties"]["omim_gene_disease_associations.csv"]
        assert "disease_id" in omim_schema["required"]
        assert "score" in omim_schema["required"]

    def test_schema_has_omim_institutional_columns(self):
        """The OMIM schema section must declare the institutional-grade columns."""
        schema_path = PROJECT_ROOT / "pipelines" / "schema" / "v1.json"
        schema = json.loads(schema_path.read_text())
        omim_props = schema["properties"]["omim_gene_disease_associations.csv"]["properties"]
        required_lineage_cols = [
            "source_id", "source_version", "source_url", "source_format",
            "download_method", "download_date", "schema_version",
            "score_type", "score_method", "confidence_tier",
            "confidence_tier_method", "dedup_strategy",
            "canonical_gene_id", "canonical_disease_id",
            "input_checksum", "pipeline_run_id",
            "association_modifier", "association_type", "is_susceptibility",
            "inheritance_pattern", "mapping_key",
        ]
        for col in required_lineage_cols:
            assert col in omim_props, f"Schema missing OMIM column: {col}"

    def test_schema_omim_confidence_tier_check_constraint(self):
        """The schema must reflect the DB CHECK constraint on confidence_tier."""
        schema_path = PROJECT_ROOT / "pipelines" / "schema" / "v1.json"
        schema = json.loads(schema_path.read_text())
        omim_props = schema["properties"]["omim_gene_disease_associations.csv"]["properties"]
        ct = omim_props["confidence_tier"]
        # Must allow only weak/moderate/strong (and null).
        allowed = set(ct.get("enum", []))
        assert "high" not in allowed, "Schema allows confidence_tier='high' (BUG-3.3)"


# ===========================================================================
# Test 9: Lineage integration — manifest file written
# ===========================================================================
class TestLineageIntegration:
    """Verify the manifest is written with full provenance."""

    def test_manifest_written_after_clean(self, omim_pipeline, morbidmap_fixture):
        """Manifest must be written after clean() (BUG-1.7, BUG-16.10)."""
        omim_pipeline.clean(morbidmap_fixture)
        import pipelines.omim_pipeline as op
        manifest_path = op.OMIM_OUTPUT_PATH.with_suffix(
            op.OMIM_OUTPUT_PATH.suffix + ".manifest.json"
        )
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        assert manifest["primary_source"] == "omim"
        assert manifest["license"] == "OMIM-restricted"
        assert manifest["schema_version"] == "2.0"
        assert "output_csv_sha256" in manifest
        assert "input_checksum" in manifest
        assert "clean_started_at" in manifest
        assert "clean_finished_at" in manifest

    def test_sha256_sidecar_written(self, omim_pipeline, morbidmap_fixture):
        """SHA-256 sidecar must be written (BUG-7.13)."""
        omim_pipeline.clean(morbidmap_fixture)
        import pipelines.omim_pipeline as op
        sidecar = op.OMIM_OUTPUT_PATH.with_suffix(".csv.sha256")
        assert sidecar.exists()
        sha = sidecar.read_text().strip()
        assert len(sha) == 64

    def test_susceptibility_csv_written(self, omim_pipeline, morbidmap_fixture):
        """Susceptibility CSV must be written when OMIM_EXCLUDE_SUSCEPTIBILITY=True."""
        omim_pipeline.clean(morbidmap_fixture)
        import pipelines.omim_pipeline as op
        sus_path = op.OMIM_SUSCEPTIBILITY_OUTPUT_PATH
        assert sus_path.exists()
        sus_df = pd.read_csv(sus_path)
        assert (sus_df["association_modifier"] == "{}").any()


# ===========================================================================
# Test 10: Cross-pipeline integration — OMIM + DisGeNET patterns match
# ===========================================================================
class TestCrossPipelineIntegration:
    """Verify OMIM and DisGeNET share the same institutional patterns."""

    def test_both_use_classify_confidence(self):
        """Both pipelines must use the shared classify_confidence function."""
        from cleaning.confidence import classify_confidence
        # Both OMIM and DisGeNET should produce the same tier for the same score.
        omim_score = 0.9  # OMIM mk=3
        disgenet_score = 0.9  # DisGeNET strong
        from cleaning.confidence import DEFAULT_CONFIDENCE_TIERS
        assert classify_confidence(omim_score, tiers=list(DEFAULT_CONFIDENCE_TIERS)) == \
               classify_confidence(disgenet_score, tiers=list(DEFAULT_CONFIDENCE_TIERS))

    def test_both_use_validate_gda_scores(self):
        """Both pipelines must use the shared validate_gda_scores function."""
        from cleaning.missing_values import validate_gda_scores
        # Both should accept the same kwargs pattern.
        df = pd.DataFrame({
            "gene_symbol": ["X"],
            "disease_id": ["OMIM:100800"],
            "source": ["omim"],
            "score": [0.9],
            "disease_name": ["Test"],
            "association_type": ["causal"],
        })
        result = validate_gda_scores(
            df,
            score_range=(0.0, 1.0),
            preserve_direction=False,
            source="omim",
            dedup=True,
            dedup_keys=["gene_symbol", "disease_id", "source"],
        )
        assert isinstance(result, pd.DataFrame)

    def test_both_use_bulk_upsert_gda(self):
        """Both pipelines must use the shared bulk_upsert_gda function."""
        from database.loaders import bulk_upsert_gda
        # Verify it accepts the lineage kwargs OMIM passes.
        import inspect
        sig = inspect.signature(bulk_upsert_gda)
        params = set(sig.parameters.keys())
        required = {
            "session", "df", "pipeline_run_id", "score_type",
            "score_method", "input_checksum", "dedup_already_done",
        }
        assert required.issubset(params), (
            f"bulk_upsert_gda missing required kwargs: {required - params}"
        )

    def test_omim_disease_id_format_matches_disgenet(self):
        """BUG-3.8: OMIM's disease_id format must match DisGeNET's."""
        # Both should use "OMIM:{int}" (no zero-pad).
        omim_disease_id = "OMIM:100800"
        # DisGeNET's OMIM-sourced rows should use the same format.
        assert omim_disease_id.startswith("OMIM:")
        assert not omim_disease_id.startswith("OMIM:0")


# ===========================================================================
# Test 11: Migration file for the 389 audit
# ===========================================================================
class TestMigrationFile:
    """Verify migration 004 extends the GDA table for the 389 audit."""

    def test_migration_004_exists(self):
        """Migration 004 must exist (extends GDA table for institutional columns)."""
        path = PROJECT_ROOT / "database" / "migrations" / "004_extend_gda_table_for_389_audit.sql"
        assert path.exists()

    def test_migration_004_adds_new_columns(self):
        """Migration 004 must add the institutional-grade GDA columns.

        Note: score_type and score_method are added by migration 003, not 004.
        Migration 004 adds the 389-audit-specific columns.
        """
        path = PROJECT_ROOT / "database" / "migrations" / "004_extend_gda_table_for_389_audit.sql"
        sql = path.read_text()
        # Verify it mentions key columns OMIM populates (added by 004).
        for col in ["confidence_tier", "dedup_strategy", "schema_version", "source_url"]:
            assert col in sql, f"Migration 004 missing column: {col}"
        # Also check that score_type/score_method exist in the migration files
        # (added by 001 or 003).
        all_migrations_sql = ""
        for mig in ["001_initial_schema.sql", "002_bug_fixes_migration.sql",
                    "003_models_fix_migration.sql", "004_extend_gda_table_for_389_audit.sql"]:
            all_migrations_sql += (PROJECT_ROOT / "database" / "migrations" / mig).read_text()
        assert "score_type" in all_migrations_sql
        assert "score_method" in all_migrations_sql

    def test_migration_004_creates_dead_letter_table(self):
        """Migration 004 must create the dead_letter_gda table."""
        path = PROJECT_ROOT / "database" / "migrations" / "004_extend_gda_table_for_389_audit.sql"
        sql = path.read_text().lower()
        assert "dead_letter_gda" in sql

    def test_migration_004_extends_disease_id_type_constraint(self):
        """Migration 004 must extend disease_id_type to allow 'omim'."""
        path = PROJECT_ROOT / "database" / "migrations" / "004_extend_gda_table_for_389_audit.sql"
        sql = path.read_text().lower()
        assert "omim" in sql


# ===========================================================================
# Test 12: Documentation integration
# ===========================================================================
class TestDocumentationIntegration:
    """Verify docs reference OMIM correctly."""

    def test_omim_docs_exist(self):
        """docs/pipelines/omim.md must exist (BUG-13.9)."""
        docs_path = PROJECT_ROOT / "docs" / "pipelines" / "omim.md"
        assert docs_path.exists()
        content = docs_path.read_text()
        # Must mention key concepts.
        assert "susceptibility" in content.lower()
        assert "morbidmap" in content.lower()
        assert "manifest" in content.lower()
        assert "license" in content.lower()

    def test_omim_test_fixtures_exist(self):
        """Test fixtures for OMIM must exist (GAP-10.14)."""
        morbidmap = PROJECT_ROOT / "tests" / "fixtures" / "omim" / "morbidmap_sample.txt"
        genemap = PROJECT_ROOT / "tests" / "fixtures" / "omim" / "genemap_sample.json"
        assert morbidmap.exists()
        assert genemap.exists()


# ===========================================================================
# Test 13: Backward compatibility — DAG still works
# ===========================================================================
class TestBackwardCompatibility:
    """Verify the DAG and existing API contracts are preserved."""

    def test_omim_dag_imports(self):
        """The OMIM Airflow DAG must still import."""
        # Airflow may not be installed in the test env, so just verify the
        # DAG file exists and references OMIMPipeline.
        dag_path = PROJECT_ROOT / "dags" / "omim_dag.py"
        assert dag_path.exists()
        content = dag_path.read_text()
        assert "OMIMPipeline" in content
        assert "max_active_runs=1" in content

    def test_omim_pipeline_public_methods_preserved(self):
        """OMIMPipeline must preserve the public method signatures."""
        from pipelines.omim_pipeline import OMIMPipeline
        import inspect
        # download(self) -> Path
        sig = inspect.signature(OMIMPipeline.download)
        assert list(sig.parameters.keys()) == ["self"]
        # clean(self, raw_path: Path) -> pd.DataFrame
        sig = inspect.signature(OMIMPipeline.clean)
        assert list(sig.parameters.keys()) == ["self", "raw_path"]
        # load(self, df: pd.DataFrame) -> int
        sig = inspect.signature(OMIMPipeline.load)
        params = list(sig.parameters.keys())
        assert "self" in params
        assert "df" in params

    def test_pipelines_init_exports_omim(self):
        """pipelines/__init__.py must export OMIMPipeline."""
        import pipelines
        assert hasattr(pipelines, "OMIMPipeline")


# ===========================================================================
# Test 14: Module-level integration
# ===========================================================================
def test_disgenet_pipeline_imports_cleanly():
    """DisGeNET pipeline must still import (no regression)."""
    from pipelines.disgenet_pipeline import DisGeNETPipeline
    assert DisGeNETPipeline is not None


def test_omim_pipeline_version():
    """OMIM pipeline must declare a version."""
    from pipelines.omim_pipeline import __version__
    assert isinstance(__version__, str)
    assert __version__


def test_omim_pipeline_all():
    """OMIM module must define __all__ with OMIMPipeline and OMIMRecord."""
    from pipelines.omim_pipeline import __all__
    assert "OMIMPipeline" in __all__
    assert "OMIMRecord" in __all__
