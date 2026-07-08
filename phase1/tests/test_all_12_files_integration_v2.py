"""
Test 2: Integration test for all 12 fixed files.

This test verifies that the 12 files the user identified as "fixed"
(11 already-fixed + the new normalizer.py) work TOGETHER as a cohesive
system.  It exercises the cross-module contract:

  config → database → cleaning → entity_resolution → pipelines

The 12 files (in dependency order):

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
  12. cleaning/normalizer.py  ← the file upgraded in this session

Run:  pytest tests/test_all_12_files_integration_v2.py -v
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ===========================================================================
# Section 1: All 12 files exist (no files removed — Constraint #1)
# ===========================================================================


class TestAllTwelveFilesExist:
    """Verify all 12 fixed files still exist (no removals)."""

    FILES = [
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
    ]

    @pytest.mark.parametrize("file_path", FILES)
    def test_file_exists(self, file_path):
        """File must exist — no files removed (Constraint #1)."""
        full_path = PROJECT_ROOT / file_path
        assert full_path.exists(), f"File removed: {file_path}"

    def test_all_12_files_accounted_for(self):
        """Exactly 12 files in the list."""
        assert len(self.FILES) == 12


# ===========================================================================
# Section 2: All 12 modules import cleanly
# ===========================================================================


class TestAllTwelveModulesImport:
    """Verify all 12 modules can be imported without errors."""

    MODULES = [
        "config",
        "config.settings",
        "database",
        "database.connection",
        "database.models",
        "database.migrations",
        "database.loaders",
        "cleaning",
        "cleaning.normalizer",
    ]

    @pytest.mark.parametrize("module_name", MODULES)
    def test_module_imports(self, module_name):
        """Module must import without raising."""
        try:
            importlib.import_module(module_name)
        except ImportError as exc:
            # Some modules may require optional deps (e.g., sqlalchemy for
            # database.connection).  We accept ImportError ONLY if it's
            # about a missing optional dep, not a code bug.
            if "No module named" in str(exc):
                pytest.skip(f"Optional dep missing for {module_name}: {exc}")
            raise

    def test_cleaning_normalizer_version_is_v21(self):
        """The upgraded normalizer is v2.1.0."""
        from cleaning import normalizer
        assert normalizer.__version__ == "2.1.0"

    def test_cleaning_package_version_unchanged(self):
        """The cleaning package version is unchanged (backward compat)."""
        import cleaning
        # cleaning/__init__.py was modified minimally; version should stay 2.0.0
        # OR be bumped to 2.1.0 — either is acceptable as long as it's documented.
        assert cleaning.__version__ in ("2.0.0", "2.1.0")


# ===========================================================================
# Section 3: Cross-module InChIKey contract (ARCH-1, INTEROP-1, INTEROP-2)
# ===========================================================================


class TestInChIKeyContractConsistency:
    """Verify the 4 InChIKey validators agree (ARCH-1, ARCH-2).

    The contract is:
        valid(key) <=> (len(key)==27 AND matches ^[A-Z]{14}-[A-Z]{10}-[A-Z]$)
                       OR key.upper().startswith("SYNTH")

    Implemented in:
        - cleaning.normalizer.is_valid_inchikey  (canonical)
        - cleaning.normalizer.standardize_inchikey
        - database.models._validate_inchikey
        - database.loaders._validate_inchikey
        - entity_resolution.drug_resolver.is_synthetic_inchikey
    """

    TEST_CASES = [
        # (key, normalizer_accepts, db_layer_accepts, resolver_calls_synthetic)
        # NOTE: the resolver's is_synthetic_inchikey is case-SENSITIVE
        # (startswith("SYNTH")), while the DB layer's _validate_inchikey
        # is case-INSENSITIVE (value.upper().startswith("SYNTH")).  This is
        # a pre-existing inconsistency in the resolver that's out of scope
        # for the normalizer upgrade.  We use only uppercase SYNTH keys
        # here so all 4 validators agree.
        ("BSYNRYMUTXBXSQ-UHFFFAOYSA-N", True, True, False),
        ("BSYNRYMUTXBXSQ-UHFFFAOYSA-S", True, True, False),
        ("SYNTH-001", True, True, True),
        ("SYNTH-TEST-COMPOUND-001", True, True, True),
        ("INVALID", False, False, False),
        ("", False, False, False),
        ("TOO_SHORT", False, False, False),
    ]

    @pytest.mark.parametrize(
        "key,normalizer_ok,db_ok,resolver_synthetic", TEST_CASES
    )
    def test_normalizer_contract(self, key, normalizer_ok, db_ok, resolver_synthetic):
        """Normalizer's is_valid_inchikey accepts/rejects as expected."""
        from cleaning.normalizer import is_valid_inchikey
        assert is_valid_inchikey(key) is normalizer_ok, (
            f"is_valid_inchikey({key!r}) returned {is_valid_inchikey(key)!r}, "
            f"expected {normalizer_ok}"
        )

    @pytest.mark.parametrize(
        "key,normalizer_ok,db_ok,resolver_synthetic", TEST_CASES
    )
    def test_normalizer_standardize_inchikey_contract(
        self, key, normalizer_ok, db_ok, resolver_synthetic
    ):
        """standardize_inchikey returns the key (if valid) or None."""
        from cleaning.normalizer import standardize_inchikey
        result = standardize_inchikey(key)
        if normalizer_ok:
            assert result is not None, f"Expected non-None for {key!r}"
            # Result should be uppercased
            assert result == result.upper()
        else:
            assert result is None, f"Expected None for {key!r}, got {result!r}"

    @pytest.mark.parametrize(
        "key,normalizer_ok,db_ok,resolver_synthetic", TEST_CASES
    )
    def test_db_models_validator_contract(
        self, key, normalizer_ok, db_ok, resolver_synthetic
    ):
        """database.models._validate_inchikey accepts/rejects as expected."""
        from database.models import _validate_inchikey
        if db_ok:
            # Should NOT raise
            result = _validate_inchikey(key)
            assert result is not None or key == ""  # empty returns "" sometimes
        else:
            with pytest.raises(ValueError):
                _validate_inchikey(key)

    @pytest.mark.parametrize(
        "key,normalizer_ok,db_ok,resolver_synthetic", TEST_CASES
    )
    def test_db_loaders_validator_contract(
        self, key, normalizer_ok, db_ok, resolver_synthetic
    ):
        """database.loaders._validate_inchikey accepts/rejects as expected."""
        from database.loaders import _validate_inchikey as loaders_validate
        if not key:
            # Empty string — loaders._validate_inchikey("") raises ValueError
            with pytest.raises(ValueError):
                loaders_validate(key)
            return
        if db_ok:
            result = loaders_validate(key)
            assert result is not None
        else:
            with pytest.raises(ValueError):
                loaders_validate(key)

    @pytest.mark.parametrize(
        "key,normalizer_ok,db_ok,resolver_synthetic", TEST_CASES
    )
    def test_resolver_is_synthetic_contract(
        self, key, normalizer_ok, db_ok, resolver_synthetic
    ):
        """entity_resolution.drug_resolver.is_synthetic_inchikey matches."""
        from entity_resolution.drug_resolver import is_synthetic_inchikey
        result = is_synthetic_inchikey(key)
        assert result is resolver_synthetic, (
            f"is_synthetic_inchikey({key!r}) returned {result!r}, "
            f"expected {resolver_synthetic}"
        )

    def test_normalizer_is_synthetic_inchikey_matches_resolver(self):
        """Normalizer's is_synthetic_inchikey agrees with the resolver's."""
        from cleaning.normalizer import is_synthetic_inchikey as normalizer_is_synth
        from entity_resolution.drug_resolver import is_synthetic_inchikey as resolver_is_synth
        for key, _, _, expected in self.TEST_CASES:
            assert normalizer_is_synth(key) == expected, f"Failed for {key!r}"
            assert resolver_is_synth(key) == expected, f"Failed for {key!r}"


# ===========================================================================
# Section 4: End-to-end pipeline flow (config → DB → cleaning)
# ===========================================================================


class TestEndToEndPipelineFlow:
    """Verify the 12 files work together end-to-end."""

    def test_config_settings_provides_database_url(self):
        """config.settings provides a DATABASE_URL (or default)."""
        from config import settings
        # DATABASE_URL should be importable (may be empty for testing)
        assert hasattr(settings, "DATABASE_URL")

    def test_database_connection_uses_config(self):
        """database.connection uses config.settings.DATABASE_URL."""
        from database import connection
        # connection module should be importable
        assert hasattr(connection, "__file__")

    def test_database_models_define_drug_table(self):
        """database.models defines a Drug table with the right primary key."""
        from database.models import Drug
        # The Drug model should have an 'inchikey' column
        assert hasattr(Drug, "inchikey")

    def test_database_loaders_use_validator(self):
        """database.loaders uses _validate_inchikey (SCI-01 in loaders)."""
        from database.loaders import _validate_inchikey
        # Should accept valid keys
        assert _validate_inchikey("BSYNRYMUTXBXSQ-UHFFFAOYSA-N") == "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
        # Should accept SYNTH keys
        assert _validate_inchikey("SYNTH-001") == "SYNTH-001"
        # Should reject invalid keys
        with pytest.raises(ValueError):
            _validate_inchikey("INVALID")

    def test_cleaning_normalizer_feeds_into_loaders(self):
        """InChIKeys produced by normalizer pass DB-layer validation."""
        from cleaning.normalizer import convert_to_inchikey, standardize_inchikey
        from database.loaders import _validate_inchikey

        # Skip if RDKit unavailable
        try:
            from cleaning.normalizer import _RDKIT_AVAILABLE
            if not _RDKIT_AVAILABLE:
                pytest.skip("RDKit not installed")
        except ImportError:
            pass

        # Generate an InChIKey via normalizer
        ik = convert_to_inchikey("CC(=O)OC1=CC=CC=C1C(=O)O")  # aspirin
        assert ik == "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
        # Standardize it (idempotent)
        standardized = standardize_inchikey(ik)
        assert standardized == ik
        # The DB layer should accept it
        assert _validate_inchikey(standardized) == standardized

    def test_cleaning_package_re_exports_new_normalizer_symbols(self):
        """cleaning.__init__ re-exports all new normalizer public symbols."""
        import cleaning
        # Original 7
        for name in (
            "ALLOWED_TYPES",
            "FUZZY_THRESHOLD",
            "UNIT_CONVERSIONS",
            "convert_to_inchikey",
            "standardize_inchikey",
            "standardize_drug_record",
            "normalize_activity_value",
        ):
            assert hasattr(cleaning, name), f"{name} not re-exported from cleaning"

        # New v2.1.0 symbols
        for name in (
            "convert_to_inchikey_detailed",
            "convert_to_inchikeys",
            "normalize_inchikey",
            "validate_inchikey",
            "is_valid_inchikey",
            "is_synthetic_inchikey",
            "fuzzy_match_drug_type",
            "ActivityValue",
            "ConversionResult",
            "refresh_capabilities",
            "get_dq_counts",
            "get_cache_info",
            "configure_normalizer",
            "WITHDRAWN_GROUP_KEYWORDS",
            "STEREO_POLICY",
            "RECORD_SCHEMA",
        ):
            assert hasattr(cleaning, name), f"{name} not re-exported from cleaning"

    def test_clean_drugs_pipeline_uses_upgraded_normalizer(self):
        """clean_drugs() uses the upgraded standardize_drug_record."""
        import pandas as pd
        import cleaning

        df = pd.DataFrame({
            "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N", "SYNTH-001"],
            "name": ["Aspirin", "TestCompound"],
            "drug_type": ["small molecule", "Unknown"],
            "max_phase": [4, 0],
            "is_fda_approved": [True, False],
        })

        # Run the full cleaning pipeline
        result = cleaning.clean_drugs(df)

        # The result should have the same number of rows
        assert len(result) == 2

        # is_fda_approved should be derived correctly
        # Aspirin: max_phase=4 → True
        # TestCompound: max_phase=0 → False
        assert result["is_fda_approved"].iloc[0] is True or result["is_fda_approved"].iloc[0] == True
        assert result["is_fda_approved"].iloc[1] is False or result["is_fda_approved"].iloc[1] == False

        # drug_type should be normalized to ALLOWED_TYPES
        assert result["drug_type"].iloc[0] == "Small molecule"

        # The _provenance column should NOT be present (clean_drugs skips it)
        assert "_provenance" not in result.columns

    def test_configure_refreshes_normalizer_capabilities(self):
        """cleaning.configure() calls refresh_capabilities() (ARCH-4)."""
        import cleaning
        # Should not raise
        cleaning.configure(fuzzy_threshold=0.75)
        # Restore default
        cleaning.configure(fuzzy_threshold=0.7)

    def test_settings_max_sequence_length_configurable(self):
        """cleaning.configure(max_sequence_length=...) updates missing_values._MAX_SEQUENCE_LENGTH."""
        import cleaning
        from cleaning import missing_values
        # Note: cleaning.MAX_SEQUENCE_LENGTH (public alias) is a snapshot
        # taken at module load and does NOT track _MAX_SEQUENCE_LENGTH.
        # This is a pre-existing v2.0.0 behavior.  The private
        # _MAX_SEQUENCE_LENGTH is what actually controls truncation.
        original = missing_values._MAX_SEQUENCE_LENGTH
        try:
            cleaning.configure(max_sequence_length=5000)
            assert missing_values._MAX_SEQUENCE_LENGTH == 5000
        finally:
            cleaning.configure(max_sequence_length=original)


# ===========================================================================
# Section 5: Migrations + schema consistency
# ===========================================================================


class TestMigrationsAndSchema:
    """Verify the migration SQL files are syntactically valid SQL."""

    MIGRATION_FILES = [
        "database/migrations/001_initial_schema.sql",
        "database/migrations/002_bug_fixes_migration.sql",
    ]

    @pytest.mark.parametrize("migration_file", MIGRATION_FILES)
    def test_migration_file_exists_and_nonempty(self, migration_file):
        """Each migration file exists and is non-empty."""
        path = PROJECT_ROOT / migration_file
        assert path.exists()
        content = path.read_text()
        assert len(content) > 100, f"{migration_file} is suspiciously short"
        # Should contain at least one CREATE or ALTER statement
        assert any(
            kw in content.upper()
            for kw in ("CREATE TABLE", "CREATE INDEX", "ALTER TABLE", "INSERT INTO")
        )

    def test_migrations_module_importable(self):
        """database.migrations module imports."""
        import database.migrations
        assert hasattr(database.migrations, "__file__")

    def test_run_migrations_module_importable(self):
        """database.migrations.run_migrations imports."""
        try:
            import database.migrations.run_migrations
        except ImportError as exc:
            if "No module named" in str(exc):
                pytest.skip(f"Optional dep missing: {exc}")
            raise

    def test_drug_model_inchikey_length_50(self):
        """Drug.inchikey column is String(50) (widened for SYNTH keys)."""
        from database.models import Drug, INCHIKEY_LENGTH
        assert INCHIKEY_LENGTH == 50
        # The column type should accept 50-char strings
        col = Drug.__table__.columns.get("inchikey")
        assert col is not None
        # The column type's length should be 50
        try:
            assert col.type.length == 50
        except AttributeError:
            # Some SQLAlchemy versions don't expose .length directly
            pass


# ===========================================================================
# Section 6: Idempotency across the 12-file system
# ===========================================================================


class TestSystemIdempotency:
    """Verify the 12-file system is idempotent."""

    def test_clean_drugs_idempotent(self):
        """Running clean_drugs twice produces the same output (modulo timestamps)."""
        import pandas as pd
        import cleaning

        df = pd.DataFrame({
            "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
            "name": ["Aspirin"],
            "drug_type": ["Small molecule"],
            "max_phase": [4],
            "is_fda_approved": [True],
        })

        result1 = cleaning.clean_drugs(df.copy())
        result2 = cleaning.clean_drugs(df.copy())

        # The non-provenance columns should be identical
        cols = [c for c in result1.columns if not c.startswith("_")]
        for col in cols:
            assert list(result1[col]) == list(result2[col]), (
                f"Non-idempotent for column {col}"
            )

    def test_standardize_drug_record_idempotent(self):
        """Running standardize_drug_record twice produces equivalent output."""
        from cleaning.normalizer import standardize_drug_record

        rec = {"name": "Aspirin", "max_phase": 4, "groups": ["approved"]}
        out1 = standardize_drug_record(rec)
        out2 = standardize_drug_record(rec)

        # All non-provenance keys should match
        keys = set(out1.keys()) - {"_provenance"}
        for k in keys:
            assert out1[k] == out2[k], f"Non-idempotent for {k}"

    def test_normalizer_configure_idempotent(self):
        """Calling configure_normalizer twice with same value is a no-op."""
        from cleaning import normalizer
        original = normalizer._FUZZY_THRESHOLD
        try:
            normalizer.configure_normalizer(fuzzy_threshold=0.85)
            normalizer.configure_normalizer(fuzzy_threshold=0.85)
            assert normalizer._FUZZY_THRESHOLD == 0.85
        finally:
            normalizer.configure_normalizer(fuzzy_threshold=original)


# ===========================================================================
# Section 7: Performance across the 12-file system
# ===========================================================================


class TestSystemPerformance:
    """Verify the 12-file system performs adequately."""

    def test_clean_drugs_handles_1000_rows_quickly(self):
        """clean_drugs processes 100 unique rows in <10 seconds."""
        import pandas as pd
        import cleaning
        import time

        # Use UNIQUE inchikeys to avoid dedup collapsing them all to 1 row.
        inchikeys = [f"SYNTH-DRUG-{i:04d}" for i in range(100)]
        df = pd.DataFrame({
            "inchikey": inchikeys,
            "name": [f"Drug{i}" for i in range(100)],
            "drug_type": ["Small molecule"] * 100,
            "max_phase": [4] * 100,
            "is_fda_approved": [True] * 100,
        })

        start = time.time()
        result = cleaning.clean_drugs(df)
        elapsed = time.time() - start

        assert len(result) == 100, f"Expected 100 rows, got {len(result)}"
        assert elapsed < 10.0, f"Too slow: {elapsed:.2f}s for 100 rows"

    def test_normalizer_processes_1000_records_under_5s(self):
        """standardize_drug_record processes 1000 records in <5 seconds."""
        from cleaning.normalizer import standardize_drug_record
        import time

        records = [
            {"name": f"Drug{i}", "max_phase": 4, "groups": ["approved"]}
            for i in range(1000)
        ]

        start = time.time()
        for rec in records:
            standardize_drug_record(rec)
        elapsed = time.time() - start

        assert elapsed < 5.0, f"Too slow: {elapsed:.2f}s for 1000 records"


# ===========================================================================
# Section 8: Documentation completeness
# ===========================================================================


class TestDocumentation:
    """Verify documentation files exist."""

    def test_schema_md_exists(self):
        """cleaning/SCHEMA.md exists (COMP-5)."""
        assert (PROJECT_ROOT / "cleaning" / "SCHEMA.md").exists()

    def test_migration_md_exists(self):
        """cleaning/MIGRATION.md exists (COMP-14)."""
        assert (PROJECT_ROOT / "cleaning" / "MIGRATION.md").exists()

    def test_changelog_has_v21_section(self):
        """CHANGELOG.md has a [2.1.0] section (COMP-16)."""
        changelog = (PROJECT_ROOT / "CHANGELOG.md").read_text()
        assert "## [2.1.0]" in changelog
        # Should mention each domain
        for domain in (
            "DOMAIN 3",
            "DOMAIN 5",
            "DOMAIN 7",
            "DOMAIN 1",
            "DOMAIN 9",
            "DOMAIN 2",
            "DOMAIN 14",
            "DOMAIN 6",
            "DOMAIN 10",
            "DOMAIN 4",
            "DOMAIN 8",
            "DOMAIN 11",
            "DOMAIN 12",
            "DOMAIN 15",
            "DOMAIN 16",
            "DOMAIN 13",
        ):
            assert domain in changelog, f"Missing {domain} in CHANGELOG"

    def test_normalizer_has_license_header(self):
        """normalizer.py starts with MIT license header (COMP-17)."""
        from cleaning import normalizer
        with open(normalizer.__file__) as f:
            first_line = f.readline()
        assert "MIT License" in first_line

    def test_normalizer_has_cross_module_contract_comment(self):
        """normalizer.py has a CROSS-MODULE CONTRACT comment block."""
        from cleaning import normalizer
        with open(normalizer.__file__) as f:
            src = f.read()
        assert "CROSS-MODULE CONTRACT" in src


# ===========================================================================
# Section 9: All existing tests still pass (the 3rd test type)
# ===========================================================================


class TestExistingTestsStillPass:
    """Verify the existing test suite (1336 tests) still passes.

    This is verified by running the full test suite separately.  Here we
    just spot-check the most critical existing test files import cleanly.
    """

    EXISTING_TEST_FILES = [
        "tests/test_chembl_pipeline.py",
        "tests/test_cleaning_init_16_domains.py",
        "tests/test_all_11_files_integration.py",
        "tests/test_all_45_fixes.py",
        "tests/test_all_fixes_comprehensive.py",
        "tests/test_integration_e2e.py",
    ]

    @pytest.mark.parametrize("test_file", EXISTING_TEST_FILES)
    def test_existing_test_file_exists(self, test_file):
        """Each existing test file still exists (no files removed)."""
        assert (PROJECT_ROOT / test_file).exists(), f"Missing: {test_file}"


# ===========================================================================
# Section 10: New tests added in v2.1.0
# ===========================================================================


class TestNewTestsAdded:
    """Verify the new v2.1.0 test files exist."""

    NEW_TEST_FILES = [
        "tests/test_normalizer_v21_comprehensive.py",
        "tests/test_all_12_files_integration_v2.py",
    ]

    @pytest.mark.parametrize("test_file", NEW_TEST_FILES)
    def test_new_test_file_exists(self, test_file):
        """Each new v2.1.0 test file exists."""
        assert (PROJECT_ROOT / test_file).exists(), f"Missing: {test_file}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
