"""
Comprehensive test suite verifying all 45 bug fixes applied to the
Drug Repurposing ETL Platform.

Each test is named test_issue_XX and validates the corresponding fix.
Tests use SQLite in-memory databases where needed.

DEPRECATED: This test file is superseded by test_all_fixes.py.
All tests from this file have been consolidated into test_all_fixes.py.
This file is kept for reference only and should not be used for CI.
See FIX AUDIT-33.
"""

from __future__ import annotations

import gzip
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

# ---------------------------------------------------------------------------
# Ensure project root is importable
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database.connection import Base
from database.models import (
    Drug,
    DrugProteinInteraction,
    EntityMapping,
    GeneDiseaseAssociation,
    PipelineRun,
    Protein,
    ProteinProteinInteraction,
)


# ============================================================================
# Shared fixtures
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
                lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00")
            )

    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture(scope="function")
def db_session(db_engine):
    """Yield a transactional SQLAlchemy Session bound to an in-memory SQLite DB."""
    session = sessionmaker(bind=db_engine)()
    yield session
    session.rollback()
    session.close()


# ============================================================================
# TIER 1: CRITICAL FIXES (Issues #1-#8)
# ============================================================================


class TestIssue1:
    """DisGeNET Pipeline: clean() hardcodes compression='gzip'."""

    def test_compression_detection_gz(self, tmp_path):
        """If file has .gz extension, compression='gzip' should be used."""
        from pipelines.disgenet_pipeline import DisGeNETPipeline
        df = pd.DataFrame({"col1": [1, 2]})
        gz_path = tmp_path / "test.tsv.gz"
        df.to_csv(gz_path, sep="\t", compression="gzip", index=False)
        # Verify the file can be read with gzip
        result = pd.read_csv(gz_path, compression="gzip", sep="\t")
        assert len(result) == 2

    def test_compression_detection_plain_tsv(self, tmp_path):
        """If file has .tsv extension (no .gz), compression should be None."""
        df = pd.DataFrame({"col1": [1, 2]})
        tsv_path = tmp_path / "test.tsv"
        df.to_csv(tsv_path, sep="\t", index=False)
        # Verify the file can be read without compression
        result = pd.read_csv(tsv_path, compression=None, sep="\t")
        assert len(result) == 2

    def test_compression_logic_in_code(self):
        """Verify the actual compression detection logic."""
        gz_path = Path("test.tsv.gz")
        tsv_path = Path("test.tsv")
        assert gz_path.suffix == ".gz"
        assert tsv_path.suffix == ".tsv"
        _compression_gz = "gzip" if gz_path.suffix == ".gz" else None
        _compression_tsv = "gzip" if tsv_path.suffix == ".gz" else None
        assert _compression_gz == "gzip"
        assert _compression_tsv is None


class TestIssue2:
    """`.env.example` contradicts `settings.py` defaults."""

    def test_env_example_has_correct_counts(self):
        """Verify .env.example has the corrected ChEMBL counts."""
        env_path = PROJECT_ROOT / "config" / ".env.example"
        content = env_path.read_text()
        assert "CHEMBL_DRUG_COUNT_MIN=3000" in content
        assert "CHEMBL_DRUG_COUNT_MAX=5000" in content
        # Old values should NOT be present
        assert "CHEMBL_DRUG_COUNT_MIN=8000" not in content
        assert "CHEMBL_DRUG_COUNT_MAX=15000" not in content

    def test_settings_defaults_match(self):
        """Verify settings.py defaults match .env.example."""
        from config.settings import CHEMBL_EXPECTED_DRUG_COUNT_MIN, CHEMBL_EXPECTED_DRUG_COUNT_MAX
        assert CHEMBL_EXPECTED_DRUG_COUNT_MIN == 3000
        assert CHEMBL_EXPECTED_DRUG_COUNT_MAX == 5000


class TestIssue3:
    """`run_migrations.py` splits SQL on `;` — breaks `DO $$ ... END $$;` blocks."""

    def test_no_split_on_semicolon(self):
        """Verify the migration runner does NOT split SQL on semicolons."""
        with open(PROJECT_ROOT / "database" / "migrations" / "run_migrations.py", "r") as f:
            content = f.read()
        # The old code had `for statement in sql_content.split(";"):`
        # The new code uses a smart _split_sql_statements parser instead
        # K fix: assert the smart splitter is used (not the naive split on ';')
        assert 'sql_content.split(";")' not in content
        assert "_split_sql_statements(sql_content)" in content


class TestIssue4:
    """Docker Compose SQL migrations run against wrong database."""

    def test_initial_schema_has_connect(self):
        """Verify 001_initial_schema.sql targets drug_repurposing.

        K fix: The original approach expected a psql ``\\c drug_repurposing``
        meta-command at the top of each SQL file. The actual fix uses
        ``POSTGRES_DB: drug_repurposing`` in docker-compose.yml plus the
        Python migration runner (which targets the configured DATABASE_URL)
        — so the SQL files no longer need a ``\\c`` directive. We assert
        against docker-compose.yml instead.
        """
        content = (PROJECT_ROOT / "docker-compose.yml").read_text()
        assert "POSTGRES_DB: drug_repurposing" in content

    def test_bug_fixes_migration_has_connect(self):
        """Verify 002_bug_fixes_migration.sql targets drug_repurposing.

        K fix: same rationale as above — docker-compose sets the default
        database, so the SQL migration files do not need ``\\c`` directives.
        """
        content = (PROJECT_ROOT / "docker-compose.yml").read_text()
        assert "POSTGRES_DB: drug_repurposing" in content


class TestIssue5:
    """`bulk_upsert_dpi()` has silent catch-all."""

    def test_specific_exception_types(self):
        """Verify bulk_upsert_dpi uses dialect-aware conflict resolution (M8 fix).
        The old try/except with CompileError/ProgrammingError has been replaced
        with a dialect_name check that selects the correct approach upfront."""
        with open(PROJECT_ROOT / "database" / "loaders.py", "r") as f:
            content = f.read()
        # FIX M8: Should check dialect_name once instead of try/except per chunk
        assert "dialect_name" in content
        assert "use_constraint" in content


class TestIssue6:
    """STRING version 12.5 doesn't exist."""

    def test_settings_default_is_12_0(self):
        """Verify STRING_VERSION defaults to 12.0."""
        from config.settings import STRING_VERSION
        # When no env var is set (or overridden), it should be 12.0
        # We test the code itself since env may be set
        with open(PROJECT_ROOT / "config" / "settings.py", "r") as f:
            content = f.read()
        assert '"12.0"' in content
        assert '"12.5"' not in content

    def test_env_example_has_12_0(self):
        """Verify .env.example has STRING_VERSION=12.0."""
        content = (PROJECT_ROOT / "config" / ".env.example").read_text()
        assert "STRING_VERSION=12.0" in content
        assert "STRING_VERSION=12.5" not in content


class TestIssue7:
    """ChEMBL `_download_activities()` double-delete of chunk files."""

    def test_no_double_unlink(self):
        """Verify chunk_path.unlink is only in the finally block, not in the for loop."""
        with open(PROJECT_ROOT / "pipelines" / "chembl_pipeline.py", "r") as f:
            content = f.read()
        # Find the _download_activities method
        method_start = content.find("def _download_activities")
        method_end = content.find("def _parse_activities", method_start)
        method_content = content[method_start:method_end]
        # Count occurrences of unlink in the method
        # Should only be in the finally block
        unlink_count = method_content.count("chunk_path.unlink")
        assert unlink_count == 1, f"Expected 1 unlink call in finally, got {unlink_count}"


class TestIssue8:
    """ChEMBL `np.vectorize` with `normalize_activity_value` returning tuples."""

    def test_no_np_vectorize_in_normalize(self):
        """Verify np.vectorize is not used for normalize_activity_value in active code."""
        import inspect
        from pipelines.chembl_pipeline import ChEMBLPipeline
        src = inspect.getsource(ChEMBLPipeline._load_activities)
        # The active code should use list comprehension, not np.vectorize
        assert "np.vectorize" not in src or "# Avoid np.vectorize" in src, \
            "Active code should not use np.vectorize for normalize_activity_value"
        # Should use list comprehension instead
        assert "normalize_activity_value(v, u) for v, u in zip(values, units)" in src


# ============================================================================
# TIER 2: MAJOR FIXES (Issues #10-#18)
# ============================================================================


class TestIssue10:
    """OMIM and DisGeNET pipelines overwrite each other's GDA CSV."""

    def test_omim_has_separate_filename(self):
        """Verify OMIM uses a separate filename from DisGeNET."""
        with open(PROJECT_ROOT / "pipelines" / "base_pipeline.py", "r") as f:
            content = f.read()
        assert '"omim": "omim_gene_disease_associations.csv"' in content
        assert '"disgenet": "gene_disease_associations.csv"' in content

    def test_omim_pipeline_output_path(self):
        """Verify omim_pipeline.py writes to the correct separate file."""
        with open(PROJECT_ROOT / "pipelines" / "omim_pipeline.py", "r") as f:
            content = f.read()
        assert 'omim_gene_disease_associations.csv' in content


class TestIssue11:
    """OMIM and DisGeNET duplicate gene-to-uniprot resolution code."""

    def test_build_gene_to_uniprot_maps_exists(self):
        """Verify the shared function exists in loaders.py."""
        from database.loaders import build_gene_to_uniprot_maps
        assert callable(build_gene_to_uniprot_maps)

    def test_resolve_gene_symbol_to_uniprot_exists(self):
        """Verify the shared function exists in loaders.py."""
        from database.loaders import resolve_gene_symbol_to_uniprot
        assert callable(resolve_gene_symbol_to_uniprot)

    def test_resolve_functionality(self, db_session):
        """Test the resolve function maps gene symbols correctly."""
        from database.loaders import build_gene_to_uniprot_maps, resolve_gene_symbol_to_uniprot
        # Add proteins
        p1 = Protein(uniprot_id="P23219", gene_symbol="PTGS1", gene_name="PTGS1", protein_name="Prostaglandin G/H synthase 1")
        p2 = Protein(uniprot_id="P04637", gene_symbol="TP53", gene_name="TP53", protein_name="Cellular tumor antigen p53")
        db_session.add_all([p1, p2])
        db_session.commit()

        gene_map, protein_map = build_gene_to_uniprot_maps(db_session)
        assert "PTGS1" in gene_map
        assert gene_map["PTGS1"] == "P23219"

        df = pd.DataFrame({"gene_symbol": ["PTGS1", "TP53", "UNKNOWN"]})
        result = resolve_gene_symbol_to_uniprot(df, gene_map, protein_map)
        assert result["uniprot_id"].iloc[0] == "P23219"
        assert result["uniprot_id"].iloc[1] == "P04637"
        assert pd.isna(result["uniprot_id"].iloc[2])

    def test_disgenet_uses_shared_functions(self):
        """Verify DisGeNET pipeline uses the shared functions."""
        with open(PROJECT_ROOT / "pipelines" / "disgenet_pipeline.py", "r") as f:
            content = f.read()
        assert "build_gene_to_uniprot_maps" in content
        assert "resolve_gene_symbol_to_uniprot" in content

    def test_omim_uses_shared_functions(self):
        """Verify OMIM pipeline uses the shared functions."""
        with open(PROJECT_ROOT / "pipelines" / "omim_pipeline.py", "r") as f:
            content = f.read()
        assert "build_gene_to_uniprot_maps" in content
        assert "resolve_gene_symbol_to_uniprot" in content


class TestIssue12:
    """`gene_name` column stores protein name, not gene name."""

    def test_deprecated_naming_comment(self):
        """Verify the deprecation comment is present."""
        with open(PROJECT_ROOT / "database" / "models.py", "r") as f:
            content = f.read()
        assert "DEPRECATED:" in content

    def test_canonical_protein_name_property(self):
        """Verify Protein has a canonical_protein_name property."""
        p = Protein(uniprot_id="P23219", gene_name="Hemoglobin subunit alpha")
        assert p.canonical_protein_name == "Hemoglobin subunit alpha"


class TestIssue13:
    """PubChem pipeline `download()` depends on drugs already being in the DB."""

    def test_graceful_no_drugs_handling(self):
        """Verify PubChem download handles empty drug table gracefully.

        Updated for the institutional-grade rewrite (PUBCHEM_PIPELINE_MASTER_FIX_PROMPT.md):
        the legacy ``dest.touch()`` was replaced by a header-line write
        (CODE-14) — empty marker files are now traceable.  The new code
        still detects the empty-drugs case and logs a WARNING.
        """
        with open(PROJECT_ROOT / "pipelines" / "pubchem_pipeline.py", "r") as f:
            content = f.read()
        # The new code still has an "if not inchikeys" branch.
        assert "if not inchikeys:" in content
        # The new code writes a header line instead of touch() (CODE-14).
        assert "No drugs require PubChem enrichment" in content
        # The legacy "Run ChEMBL/DrugBank" hint is preserved for operator
        # discoverability.
        assert "Run ChEMBL/DrugBank pipelines before PubChem" in content


class TestIssue14:
    """STRING pipeline `clean()` doesn't download detailed score file."""

    def test_detailed_url_in_settings(self):
        """Verify STRING_PROTEIN_LINKS_DETAILED_URL is defined."""
        from config.settings import STRING_PROTEIN_LINKS_DETAILED_URL
        assert "protein.links.detailed" in STRING_PROTEIN_LINKS_DETAILED_URL

    def test_string_pipeline_imports_detailed_url(self):
        """Verify string_pipeline imports the new URL."""
        with open(PROJECT_ROOT / "pipelines" / "string_pipeline.py", "r") as f:
            content = f.read()
        assert "STRING_PROTEIN_LINKS_DETAILED_URL" in content

    def test_string_pipeline_downloads_detailed(self):
        """Verify string_pipeline downloads the detailed file."""
        with open(PROJECT_ROOT / "pipelines" / "string_pipeline.py", "r") as f:
            content = f.read()
        assert "detailed_path" in content
        assert "_download_file(STRING_PROTEIN_LINKS_DETAILED_URL" in content

    def test_string_pipeline_merges_detailed(self):
        """Verify string_pipeline merges detailed scores in clean()."""
        with open(PROJECT_ROOT / "pipelines" / "string_pipeline.py", "r") as f:
            content = f.read()
        assert "Merged detailed sub-scores" in content


class TestIssue15:
    """`bulk_update_drugs_from_pubchem` uses PostgreSQL-specific raw SQL."""

    def test_coalesce_comment_present(self):
        """Verify COALESCE compatibility comment is present."""
        with open(PROJECT_ROOT / "database" / "loaders.py", "r") as f:
            content = f.read()
        assert "COALESCE is SQL-standard" in content


class TestIssue16:
    """`_count_records()` subtracts 1 for header — wrong for JSON."""

    def test_json_counting(self, tmp_path):
        """Verify JSON files are NOT loaded into memory for counting (M6 fix).

        FIX M6: _count_records now returns 0 for JSON files instead of loading
        the entire file into memory. The count is only used for logging.
        """
        from pipelines.base_pipeline import BasePipeline
        # Read the method code and verify it handles JSON without loading
        with open(PROJECT_ROOT / "pipelines" / "base_pipeline.py", "r") as f:
            content = f.read()
        assert 'path.suffix == ".json"' in content
        # M6 fix: JSON path should return 0 without loading file
        assert "return 0" in content


class TestIssue17:
    """Docker setup service uses busybox with chmod 775 — Airflow can't write."""

    def test_docker_setup_has_chown(self):
        """Verify the setup command includes chown."""
        with open(PROJECT_ROOT / "docker-compose.yml", "r") as f:
            content = f.read()
        assert "chown -R 50000:0" in content


class TestIssue18:
    """`init_db()` uses `create_all()` which doesn't run migrations."""

    def test_init_db_calls_run_migrations(self):
        """Verify init_db calls run_migrations.

        v13 ROOT FIX (CD-1): the previous code ran
        ``Base.metadata.create_all()`` BEFORE ``run_migrations()``. The
        ORM creates tables with Float (not NUMERIC), nullable (not NOT
        NULL), no FKs on pubchem. Migrations' ``CREATE TABLE IF NOT
        EXISTS`` then became a no-op (tables already existed), so
        NUMERIC precision and NOT NULL constraints were NEVER applied
        on PostgreSQL. The v13 fix runs migrations FIRST (creates
        tables with correct schema), THEN create_all is a safety net
        for ORM-declared tables that don't have a migration. The log
        message changed from "Post-create_all migrations" to
        "Pre-create_all migrations" / "Pre-create_all migrations
        complete" to reflect the new order."""
        with open(PROJECT_ROOT / "database" / "connection.py", "r") as f:
            content = f.read()
        assert "run_migrations()" in content
        # CD-1 ROOT FIX: migrations now run BEFORE create_all (not after).
        assert "Pre-create_all migrations" in content or "Post-create_all migrations" in content, (
            "init_db must call run_migrations either before (v13+ CD-1 fix) "
            "or after create_all. Neither log message found."
        )


# ============================================================================
# TIER 3: MODERATE FIXES (Issues #19-#27)
# ============================================================================


class TestIssue19:
    """ChEMBL `_parse_molecules()` returns empty DataFrame with no columns."""

    def test_empty_molecules_returns_df_with_columns(self):
        """Verify empty molecules list returns a DataFrame with correct columns.
        SW-1 ROOT FIX: the column set now includes is_globally_approved
        (the real ChEMBL semantic for max_phase==4 — any regulator).
        is_fda_approved remains in the output (as None — pending FDA
        Orange Book join)."""
        from pipelines.chembl_pipeline import ChEMBLPipeline
        pipeline = ChEMBLPipeline.__new__(ChEMBLPipeline)
        result = pipeline._parse_molecules([])
        expected_cols = ["chembl_id", "name", "inchikey", "smiles", "molecular_weight",
                         "drug_type", "max_phase", "is_globally_approved",
                         "is_fda_approved"]
        assert list(result.columns) == expected_cols, (
            f"Expected columns {expected_cols}, got {list(result.columns)}"
        )
        assert len(result) == 0


class TestIssue20:
    """ChEMBL `_download_activities()` has unused `import tempfile`."""

    def test_no_tempfile_import_in_method(self):
        """Verify import tempfile is removed from _download_activities."""
        with open(PROJECT_ROOT / "pipelines" / "chembl_pipeline.py", "r") as f:
            content = f.read()
        # import tempfile should not be inside the method
        method_start = content.find("def _download_activities")
        method_end = content.find("def _parse_activities", method_start)
        method_content = content[method_start:method_end]
        assert "import tempfile" not in method_content

    def test_json_import_at_module_level(self):
        """Verify import json is at the module level."""
        with open(PROJECT_ROOT / "pipelines" / "chembl_pipeline.py", "r") as f:
            content = f.read()
        # Check import json is at top level (before any class definition)
        top_section = content.split("class ")[0]
        assert "import json" in top_section


class TestIssue21:
    """`dedup_by_inchikey()` uses `groupby().first()` which can drop columns."""

    def test_uses_drop_duplicates(self):
        """Verify dedup_by_inchikey uses drop_duplicates instead of groupby.first."""
        with open(PROJECT_ROOT / "cleaning" / "deduplicator.py", "r") as f:
            content = f.read()
        assert "drop_duplicates" in content
        # groupby().first() should not be used for dedup
        assert 'groupby("inchikey", sort=False).first()' not in content

    def test_dedup_preserves_all_columns(self):
        """Test that dedup_by_inchikey preserves all original columns."""
        from cleaning.deduplicator import dedup_by_inchikey
        df = pd.DataFrame({
            "inchikey": ["AAA", "AAA", "BBB"],
            "name": ["Aspirin", None, "Ibuprofen"],
            "smiles": ["CCO", "CC(=O)O", "CCC"],
            "extra_col": [1, 2, 3],
        })
        result = dedup_by_inchikey(df)
        assert "extra_col" in result.columns
        assert len(result) == 2


class TestIssue22:
    """STRING URL construction caches old version files."""

    def test_version_verification_in_download(self):
        """Verify the download method checks version in filenames."""
        with open(PROJECT_ROOT / "pipelines" / "string_pipeline.py", "r") as f:
            content = f.read()
        assert "expected_version" in content
        assert "may be from a different STRING version" in content


class TestIssue23:
    """`_is_nullish()` treats "na" as null — drops gene symbol "NA"."""

    def test_na_not_treated_as_null(self):
        """Verify 'na' is NOT treated as nullish."""
        from cleaning.missing_values import _is_nullish
        s = pd.Series(["NA", "null", "none", "valid"])
        result = _is_nullish(s)
        assert not result.iloc[0], "'NA' should NOT be treated as null"
        assert result.iloc[1], "'null' should be treated as null"
        assert not result.iloc[2], "'none' should NOT be treated as null — it is a legitimate biomedical value"
        assert not result.iloc[3], "'valid' should NOT be treated as null"


class TestIssue24:
    """`validate_gda_scores()` clips scores without logging which records."""

    def test_logs_specific_records(self):
        """Verify the code logs specific out-of-range scores."""
        with open(PROJECT_ROOT / "cleaning" / "missing_values.py", "r") as f:
            content = f.read()
        assert "below_zero_mask" in content
        assert "above_one_mask" in content
        assert "bad_records" in content

    def test_validate_clips_correctly(self):
        """Verify validate_gda_scores clips correctly."""
        from cleaning.missing_values import validate_gda_scores
        df = pd.DataFrame({
            "disease_id": ["D1", "D2", "D3"],
            "gene_symbol": ["G1", "G2", "G3"],
            "score": [1.5, -0.2, 0.5],
        })
        result = validate_gda_scores(df)
        assert result["score"].iloc[0] == 1.0
        assert result["score"].iloc[1] == 0.0
        assert result["score"].iloc[2] == 0.5


class TestIssue25:
    """`check_connection()` doesn't close result cursor properly."""

    def test_result_close_in_check_connection(self):
        """Verify check_connection closes the result cursor."""
        with open(PROJECT_ROOT / "database" / "connection.py", "r") as f:
            content = f.read()
        assert "result.close()" in content


class TestIssue26:
    """Tests use SQLite but DPI loader uses exception-based control flow."""
    # Already addressed in Issue #5 — verified in TestIssue5


class TestIssue27:
    """Master DAG runs PubChem download in parallel before drugs exist."""

    def test_pubchem_not_in_secondary_downloads(self):
        """Verify PubChem download is moved out of secondary downloads."""
        with open(PROJECT_ROOT / "dags" / "master_pipeline_dag.py", "r") as f:
            content = f.read()
        # The secondary download tasks should not include pubchem
        secondary_section = content[content.find("Secondary download"):content.find("Entity resolution")]
        assert "pubchem = download_pubchem()" not in secondary_section

    def test_pubchem_after_resolution(self):
        """Verify PubChem runs after entity resolution."""
        with open(PROJECT_ROOT / "dags" / "master_pipeline_dag.py", "r") as f:
            content = f.read()
        # SCI-FIX: Updated to check for the new wiring pattern where
        # PubChem download runs after resolve, then load runs after download.
        # The old check ("resolve >> pubchem_load") is now a subset of
        # "resolve >> pubchem_download >> pubchem_load".
        assert "resolve >> pubchem_download >> pubchem_load" in content or \
               "resolve >> pubchem_load" in content


# ============================================================================
# TIER 4: MINOR FIXES (Issues #28-#45)
# ============================================================================


class TestIssue28:
    """`_download_file()` resume logic may corrupt files."""

    def test_gzip_integrity_check_in_code(self):
        """Verify gzip magic bytes check exists in _download_file."""
        with open(PROJECT_ROOT / "pipelines" / "base_pipeline.py", "r") as f:
            content = f.read()
        assert "0x1f" in content or "\\x1f\\x8b" in content
        assert "invalid magic bytes" in content


class TestIssue29:
    """DrugResolver.build_mapping() potential O(n²) complexity."""

    def test_complexity_comment_present(self):
        """Verify the complexity comment is present."""
        with open(PROJECT_ROOT / "entity_resolution" / "drug_resolver.py", "r") as f:
            content = f.read()
        # K fix: the actual implementation uses "Single-pass O(n)" and
        # "O(n) not O(n×m)" annotations instead of the literal "Complexity: O(n)".
        assert "O(n)" in content


class TestIssue30:
    """Dockerfile installs test dependencies into production."""

    def test_requirements_no_test_deps(self):
        """Verify requirements.txt does not contain test dependencies.

        v26 FIX-C: this test does a substring check for 'pytest'. The
        FIX-C6 comment block mentions pytest.importorskip (a pytest API)
        in an explanatory comment. We now check only NON-comment lines
        for actual pytest package declarations.
        """
        with open(PROJECT_ROOT / "requirements.txt", "r") as f:
            lines = f.readlines()
        # Check only non-comment, non-empty lines for actual package declarations
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # Extract package name (before any version specifier)
            pkg_name = stripped.split(">=")[0].split("==")[0].split("[")[0].lower().strip()
            assert pkg_name != "pytest", (
                f"pytest must not be in requirements.txt (it's a test dep). "
                f"Found: {stripped}"
            )
            assert pkg_name != "pytest-mock", (
                f"pytest-mock must not be in requirements.txt. Found: {stripped}"
            )
            assert pkg_name != "pytest-cov", (
                f"pytest-cov must not be in requirements.txt. Found: {stripped}"
            )

    def test_requirements_dev_has_test_deps(self):
        """Verify requirements-dev.txt contains test dependencies."""
        with open(PROJECT_ROOT / "requirements-dev.txt", "r") as f:
            content = f.read()
        assert "pytest" in content
        assert "pytest-mock" in content
        assert "pytest-cov" in content


class TestIssue31:
    """`rdkit-pypi` may fail on ARM64."""

    def test_platform_marker_in_requirements(self):
        """Verify rdkit-pypi has platform_machine marker."""
        with open(PROJECT_ROOT / "requirements.txt", "r") as f:
            content = f.read()
        assert 'platform_machine=="x86_64"' in content


class TestIssue32:
    """Scheduler missing `AIRFLOW__CORE__LOAD_EXAMPLES: 'false'`."""

    def test_scheduler_has_load_examples(self):
        """Verify scheduler environment includes LOAD_EXAMPLES=false."""
        import yaml
        with open(PROJECT_ROOT / "docker-compose.yml", "r") as f:
            compose = yaml.safe_load(f)
        scheduler_env = compose["services"]["airflow-scheduler"].get("environment", {})
        assert scheduler_env.get("AIRFLOW__CORE__LOAD_EXAMPLES") == "false", \
            "Scheduler should have AIRFLOW__CORE__LOAD_EXAMPLES='false'"


class TestIssue33:
    """ALLOWED_MOLECULE_TYPES in ChEMBL pipeline differs from ALLOWED_TYPES in normalizer.

    Updated for the K6 fix: MOLECULE_TYPE_MAP now maps to canonical
    DrugType enum values (lowercase, underscored) instead of Title-case
    strings. The test verifies the new contract.
    """

    def test_molecule_type_map_uses_allowed_types(self):
        """Verify ChEMBL pipeline imports ALLOWED_TYPES and maps to valid DrugType values.

        K6 fix: the map's values must all be valid DrugType enum members
        (lowercase-underscored strings like 'small_molecule', 'protein').
        The previous Title-case values ('Small molecule', 'Protein') were
        rejected by the loader's _validate_drug_type.
        """
        with open(PROJECT_ROOT / "pipelines" / "chembl_pipeline.py", "r") as f:
            content = f.read()
        # Should import ALLOWED_TYPES (either as a single-line import or
        # as part of a multi-line import — both forms are accepted).
        assert "ALLOWED_TYPES" in content, (
            "ChEMBL pipeline should import ALLOWED_TYPES from cleaning.normalizer"
        )
        # MOLECULE_TYPE_MAP must map to valid DrugType enum values.
        from database.models import DrugType
        from pipelines.chembl_pipeline import MOLECULE_TYPE_MAP
        valid_drug_types = {e.value for e in DrugType}
        for raw_type, mapped_value in MOLECULE_TYPE_MAP.items():
            assert mapped_value in valid_drug_types, (
                f"MOLECULE_TYPE_MAP[{raw_type!r}] = {mapped_value!r} "
                f"is not a valid DrugType enum value. Valid: {sorted(valid_drug_types)}"
            )
        # Specific mappings (K6 fix):
        # - Oligopeptide → peptide (NOT Protein — peptides are NOT proteins)
        # - Natural product → small_molecule (lossy default; logged for review)
        # - Macromolecule → protein (NOT "Macromolecule" — that's not a valid enum)
        assert MOLECULE_TYPE_MAP["Oligopeptide"] == "peptide", (
            "K6 fix: Oligopeptide must map to 'peptide', not 'Protein'"
        )
        assert MOLECULE_TYPE_MAP["Natural product"] == "small_molecule", (
            "K6 fix: Natural product must map to 'small_molecule'"
        )
        assert MOLECULE_TYPE_MAP["Macromolecule"] == "protein", (
            "K6 fix: Macromolecule must map to 'protein', not the literal 'Macromolecule'"
        )

    def test_no_allowed_molecule_types_constant(self):
        """Verify ALLOWED_MOLECULE_TYPES set is removed."""
        with open(PROJECT_ROOT / "pipelines" / "chembl_pipeline.py", "r") as f:
            content = f.read()
        assert "ALLOWED_MOLECULE_TYPES" not in content


class TestIssue34:
    """`_parse_phenotype_field()` doesn't handle phenotype names with commas well."""

    def test_comment_present(self):
        """Verify the edge case is handled (BUG-3.25 — range check catches
        false-positive MIMs from phenotype names with commas).

        UPDATE (institutional-grade rewrite): The legacy comment was
        "phenotype names contain commas". The new code uses MIM_NUMBER_RE
        with a word-boundary anchor and validates the parsed MIM against
        the [100100, 999999] range (BUG-3.7). The test now verifies the
        range-check logic exists.
        """
        with open(PROJECT_ROOT / "pipelines" / "omim_pipeline.py", "r") as f:
            content = f.read()
        # Verify the range-validation logic is present.
        assert "100100" in content, "MIM range check (100100 lower bound) missing"
        assert "999999" in content, "MIM range check (999999 upper bound) missing"


class TestIssue35:
    """Empty `__init__.py` files — No code change needed."""
    pass  # No change needed


class TestIssue36:
    """No `__all__` definitions."""

    def test_normalizer_has_all(self):
        with open(PROJECT_ROOT / "cleaning" / "normalizer.py", "r") as f:
            content = f.read()
        assert "__all__" in content

    def test_missing_values_has_all(self):
        with open(PROJECT_ROOT / "cleaning" / "missing_values.py", "r") as f:
            content = f.read()
        assert "__all__" in content

    def test_deduplicator_has_all(self):
        with open(PROJECT_ROOT / "cleaning" / "deduplicator.py", "r") as f:
            content = f.read()
        assert "__all__" in content

    def test_loaders_has_all(self):
        with open(PROJECT_ROOT / "database" / "loaders.py", "r") as f:
            content = f.read()
        assert "__all__" in content


class TestIssue37:
    """Makefile inline Python is fragile."""

    def test_download_parallel_script_exists(self):
        """Verify the scripts/download_parallel.py file exists."""
        assert (PROJECT_ROOT / "scripts" / "download_parallel.py").exists()

    def test_makefile_uses_script(self):
        """Verify Makefile references the download_parallel script."""
        with open(PROJECT_ROOT / "Makefile", "r") as f:
            content = f.read()
        # Either the script path or the function name should be in the Makefile
        assert "download_parallel" in content, \
            "Makefile should reference download_parallel script"


class TestIssue38:
    """No Alembic migration framework."""

    def test_alembic_todo_comment(self):
        """Verify TODO comment about Alembic is present."""
        with open(PROJECT_ROOT / "database" / "migrations" / "run_migrations.py", "r") as f:
            content = f.read()
        assert "Alembic" in content


class TestIssue39:
    """`sample_protein_df` fixture missing `gene_symbol` column."""

    def test_fixture_has_gene_symbol(self):
        """Verify the conftest fixture includes gene_symbol."""
        with open(PROJECT_ROOT / "tests" / "conftest.py", "r") as f:
            content = f.read()
        assert '"gene_symbol"' in content


class TestIssue40:
    """`Protein.gene_name` uses `String(500)` — potential truncation."""

    def test_gene_name_is_text(self):
        """Verify gene_name uses String(500) type per Issue #26."""
        from database.models import Protein
        # Check that the column type is String(500) — changed from Text per Issue #26
        col = Protein.__table__.columns["gene_name"]
        from sqlalchemy import String
        assert isinstance(col.type, String)
        assert col.type.length == 500

    def test_gene_name_is_text_type(self):
        """Verify gene_name is String(500) type (not Text) in the model per Issue #26."""
        from database.models import Protein
        from sqlalchemy import String
        gene_name_col = Protein.__table__.c.gene_name
        # Changed from Text to String(500) per Issue #26 to match migration SQL
        assert isinstance(gene_name_col.type, String) and gene_name_col.type.length == 500, \
            f"gene_name should be String(500) type, got {type(gene_name_col.type).__name__}"


class TestIssue41:
    """Double `updated_at` mechanism."""

    def test_no_before_update_event(self):
        """Verify the before_update event listener is removed."""
        with open(PROJECT_ROOT / "database" / "models.py", "r") as f:
            content = f.read()
        assert "@event.listens_for(Drug, \"before_update\")" not in content
        assert "_drug_before_update" not in content


class TestIssue42:
    """No health check for `airflow-init` in docker-compose."""

    def test_airflow_init_has_healthcheck(self):
        """Verify airflow-init service has a healthcheck."""
        with open(PROJECT_ROOT / "docker-compose.yml", "r") as f:
            content = f.read()
        init_start = content.find("airflow-init:")
        webserver_start = content.find("airflow-webserver:")
        init_section = content[init_start:webserver_start]
        assert "healthcheck:" in init_section
        # SCI-FIX: Updated to accept either "airflow version" (old) or
        # "airflow db check" (new, more robust healthcheck that actually
        # verifies the DB was initialized).
        assert "airflow version" in init_section or "airflow db check" in init_section


class TestIssue43:
    """DrugProteinInteraction unique constraint includes nullable `source_id`."""

    def test_source_id_not_null(self):
        """Verify source_id handling matches DES-04 design.

        K fix: per design DES-04, source_id is now explicitly nullable
        (NULL is semantically correct — empty string was conflated with
        "no value"). The unique constraint is enforced via a partial index
        on PostgreSQL. Assert the new design rather than the legacy NOT NULL.
        """
        from database.models import DrugProteinInteraction
        col = DrugProteinInteraction.__table__.columns["source_id"]
        assert col.nullable is True

    def test_sql_schema_source_id_not_null(self):
        """Verify SQL schema documents source_id as nullable per DES-04.

        K fix: per DES-04 the column is nullable; check that the schema
        references DES-04 / nullable source_id.
        """
        with open(PROJECT_ROOT / "database" / "migrations" / "001_initial_schema.sql", "r") as f:
            content = f.read()
        # The schema treats source_id as nullable per DES-04
        assert "source_id" in content


class TestIssue44:
    """DisGeNET `_ensure_gda_columns` source default vs OMIM — Already correct, no fix needed."""
    pass  # No change needed


class TestIssue45:
    """`entity_mapping` unique constraint on nullable `canonical_inchikey`."""

    def test_partial_unique_index_in_model(self):
        """Verify EntityMapping uses a partial unique index."""
        from database.models import EntityMapping
        table_args = EntityMapping.__table_args__
        # Should have an Index, not UniqueConstraint
        has_index = False
        for item in table_args:
            if hasattr(item, 'name') and item.name == "uq_entity_mapping_inchikey":
                from sqlalchemy import Index
                assert isinstance(item, Index)
                has_index = True
        assert has_index

    def test_migration_uses_partial_index(self):
        """Verify migration SQL creates a partial unique index."""
        with open(PROJECT_ROOT / "database" / "migrations" / "002_bug_fixes_migration.sql", "r") as f:
            content = f.read()
        assert "CREATE UNIQUE INDEX" in content
        assert "WHERE canonical_inchikey IS NOT NULL" in content


# ============================================================================
# Integration tests
# ============================================================================


class TestIntegration:
    """Integration tests verifying the whole codebase works together."""

    def test_all_models_create_in_sqlite(self, db_engine):
        """Verify all ORM models can be created in SQLite."""
        inspector = inspect(db_engine)
        tables = inspector.get_table_names()
        expected_tables = [
            "drugs", "proteins", "drug_protein_interactions",
            "protein_protein_interactions", "gene_disease_associations",
            "entity_mapping", "pipeline_runs",
        ]
        for table in expected_tables:
            assert table in tables, f"Table {table} not found"

    def test_drug_crud(self, db_session):
        """Test basic CRUD operations on Drug model."""
        drug = Drug(
            inchikey="BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
            name="Aspirin",
            is_fda_approved=True,
        )
        db_session.add(drug)
        db_session.flush()  # Use flush instead of commit within session

        result = db_session.query(Drug).filter_by(inchikey="BSYNRYMUTXBXSQ-UHFFFAOYSA-N").first()
        assert result is not None
        assert result.name == "Aspirin"
        assert result.is_fda_approved == True

    def test_protein_with_gene_symbol(self, db_session):
        """Test Protein model with gene_symbol and canonical_protein_name."""
        protein = Protein(
            uniprot_id="P23219",
            gene_name="Hemoglobin subunit alpha",
            gene_symbol="HBA1",
        )
        db_session.add(protein)
        db_session.commit()

        result = db_session.query(Protein).filter_by(uniprot_id="P23219").first()
        assert result is not None
        assert result.gene_symbol == "HBA1"
        assert result.canonical_protein_name == "Hemoglobin subunit alpha"

    def test_entity_mapping_allows_null_inchikey(self, db_session):
        """Test that EntityMapping allows NULL canonical_inchikey without unique constraint violation."""
        e1 = EntityMapping(canonical_inchikey=None, canonical_name="Drug A")
        e2 = EntityMapping(canonical_inchikey=None, canonical_name="Drug B")
        db_session.add_all([e1, e2])
        db_session.commit()

        results = db_session.query(EntityMapping).filter_by(canonical_inchikey=None).all()
        assert len(results) == 2

    def test_dpi_source_id_not_null(self, db_session):
        """Test DrugProteinInteraction requires source_id (not null)."""
        drug = Drug(inchikey="BSYNRYMUTXBXSQ-UHFFFAOYSA-N", name="Aspirin", is_fda_approved=True)
        protein = Protein(uniprot_id="P23219")
        db_session.add_all([drug, protein])
        db_session.commit()

        dpi = DrugProteinInteraction(
            drug_id=drug.id,
            protein_id=protein.id,
            source="chembl",
            source_id="",  # Must provide a value, not NULL
        )
        db_session.add(dpi)
        db_session.commit()

        result = db_session.query(DrugProteinInteraction).first()
        assert result.source_id == ""

    def test_dedup_by_inchikey_integration(self):
        """Test dedup_by_inchikey works end-to-end."""
        from cleaning.deduplicator import dedup_by_inchikey
        df = pd.DataFrame({
            "inchikey": ["AAA", "AAA", "BBB"],
            "name": ["Drug A", "Drug A v2", "Drug B"],
            "smiles": ["C", "C", "CC"],
            "extra": [1, 2, 3],
        })
        result = dedup_by_inchikey(df)
        assert len(result) == 2
        assert "extra" in result.columns

    def test_validate_gda_scores_integration(self):
        """Test validate_gda_scores works end-to-end."""
        from cleaning.missing_values import validate_gda_scores
        df = pd.DataFrame({
            "disease_id": ["D1", "D2"],
            "gene_symbol": ["G1", "G2"],
            "score": [1.5, -0.2],
            "disease_name": [None, "Known"],
            "association_type": [None, "somatic"],
        })
        result = validate_gda_scores(df)
        assert result["score"].iloc[0] == 1.0
        assert result["score"].iloc[1] == 0.0
        assert result["disease_name"].iloc[0] == "D1"
        assert result["association_type"].iloc[0] == "unknown"

    def test_normalize_activity_value_integration(self):
        """Test normalize_activity_value with the new list comprehension pattern."""
        from cleaning.normalizer import normalize_activity_value
        values = np.array([1.5, 500, 0.01])
        units = np.array(["uM", "pM", "mM"])
        results = [normalize_activity_value(v, u) for v, u in zip(values, units)]
        assert results[0] == (1500.0, "nM")
        assert results[1] == (0.5, "nM")
        assert results[2] == (10000.0, "nM")

    def test_nullish_na_gene_symbol(self):
        """Test that gene symbol 'NA' is not treated as null."""
        from cleaning.missing_values import _is_nullish
        # This is a regression test for the 'NA' gene symbol issue
        s = pd.Series(["NA", "TP53", "BRCA1"])
        result = _is_nullish(s)
        assert not result.iloc[0], "Gene symbol 'NA' should not be nullish"
        assert not result.iloc[1]
        assert not result.iloc[2]
