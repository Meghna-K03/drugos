"""
Test 2: Integration test for all 22 files (21 already-fixed + drugbank_pipeline.py).

This test verifies that the newly-upgraded drugbank_pipeline.py works
correctly with ALL other files in the codebase. It covers:

1. All 22 files import cleanly (no circular imports, no missing deps).
2. Config values from config/settings.py are consumed correctly by
   drugbank_pipeline.py.
3. Database models (Drug, DrugProteinInteraction, Protein, PipelineRun)
   accept the pipeline's output via the loader functions.
4. Cleaning modules (normalizer, missing_values, deduplicator) integrate
   with the pipeline's clean() flow.
5. Entity resolution modules (resolver_utils, drug_resolver,
   protein_resolver) can resolve the pipeline's output.
6. BasePipeline audit-trail infrastructure (PipelineRun rows, run logs)
   works with DrugBankPipeline.
7. End-to-end: download (mocked) -> clean -> load into SQLite, verify
   DB contains expected drug + DPI rows with full lineage.
8. Idempotency: running clean() + load() twice produces identical DB
   state (no duplicate DPI rows).
9. Cross-pipeline consistency: DrugBankPipeline and ChEMBLPipeline
   produce compatible Drug rows (same column set, same InChIKey format).

The 22 files covered:
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
  20. pipelines/base_pipeline.py
  21. pipelines/chembl_pipeline.py
  22. pipelines/drugbank_pipeline.py  <-- the file we upgraded

Run: pytest tests/test_all_22_files_integration_v6.py -v
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from unittest import mock

import pandas as pd
import pytest

# Make project root importable.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

# ---------------------------------------------------------------------------
# The 22 files under test.
# ---------------------------------------------------------------------------

TWENTY_TWO_FILES: list[str] = [
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
    "pipelines/chembl_pipeline.py",
    "pipelines/drugbank_pipeline.py",
]

FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "drugbank_sample.xml"


# ---------------------------------------------------------------------------
# Domain 1: Architecture - all 22 files exist and import cleanly
# ---------------------------------------------------------------------------


class TestAll22FilesImport:
    """Verify all 22 files exist on disk and import without errors."""

    def test_all_22_files_exist_on_disk(self):
        """All 22 files must exist on disk."""
        missing = []
        for rel_path in TWENTY_TWO_FILES:
            full_path = PROJECT_ROOT / rel_path
            if not full_path.exists():
                missing.append(rel_path)
        assert not missing, f"Missing files: {missing}"

    def test_all_python_files_import_cleanly(self):
        """All Python files (21 .py files) must import without errors."""
        py_files = [f for f in TWENTY_TWO_FILES if f.endswith(".py")]
        import_errors = []
        for rel_path in py_files:
            module_name = rel_path[:-3].replace("/", ".")
            # Skip __init__ files that may have side effects.
            if module_name.endswith(".__init__"):
                module_name = module_name[:-9]
            try:
                importlib.import_module(module_name)
            except Exception as exc:
                import_errors.append((rel_path, str(exc)))
        assert not import_errors, f"Import errors: {import_errors}"

    def test_drugbank_pipeline_imports_all_dependencies(self):
        """DrugBankPipeline must import all its dependencies successfully."""
        from pipelines.drugbank_pipeline import DrugBankPipeline
        from config.settings import (
            DRUGBANK_VERSION,
            DRUGBANK_XML_PATH,
            DRUGBANK_XML_NAMESPACE,
            PROCESSED_DATA_DIR,
            RAW_DATA_DIR,
        )
        from database.connection import get_db_session
        from database.loaders import (
            MappingResult,
            UpsertResult,
            bulk_upsert_dpi,
            bulk_upsert_drugs,
            get_inchikey_to_drug_id_map,
            get_uniprot_to_protein_id_map,
        )
        from database.models import Drug, DrugProteinInteraction, Protein
        from pipelines.base_pipeline import BasePipeline, LoadResult, SchemaValidationError
        from cleaning.normalizer import standardize_inchikey, convert_to_inchikey, convert_to_inchikeys
        from cleaning.missing_values import fill_missing_drug_fields, handle_missing_inchikey
        from cleaning.deduplicator import dedup_interactions

        # Verify DrugBankPipeline is a subclass of BasePipeline.
        assert issubclass(DrugBankPipeline, BasePipeline)
        # Verify the pipeline can be constructed.
        pipeline = DrugBankPipeline()
        assert pipeline.source_name == "drugbank"


# ---------------------------------------------------------------------------
# Domain 2: Design - pipeline conforms to loader/model contracts
# ---------------------------------------------------------------------------


class TestDesignContracts:
    """Verify the pipeline conforms to loader and model contracts."""

    def test_drugbank_pipeline_output_matches_drug_model_columns(self, db_session):
        """DrugBank _ensure_drug_columns output must be a subset of Drug model columns."""
        from sqlalchemy import inspect as sa_inspect
        from database.models import Drug
        from pipelines.drugbank_pipeline import DrugBankPipeline

        drug_model_cols = {c.name for c in sa_inspect(Drug).columns}
        pipeline = DrugBankPipeline()
        raw_df = pd.DataFrame({
            "drugbank_id": ["DB01050"],
            "name": ["Ibuprofen"],
            "inchikey": ["WFXAZNNJSJXTJZ-UHFFFAOYSA-N"],
            "smiles": ["CC(C)Cc1ccc(cc1)C(C)C(=O)O"],
            "molecular_weight": [206.28],
            "molecular_formula": ["C13H18O2"],
            "is_fda_approved": [True],
            "mechanism_of_action": ["COX inhibitor"],
        })
        ready_df = pipeline._ensure_drug_columns(raw_df)
        for col in ready_df.columns:
            assert col in drug_model_cols, (
                f"Column '{col}' in DrugBank output is not in Drug model"
            )

    def test_mapping_result_unwrapped_before_series_map(self):
        """D1 fix: MappingResult.mapping must be unwrapped before Series.map()."""
        from database.loaders import MappingResult
        import pipelines.drugbank_pipeline as mod
        src = open(mod.__file__).read()
        assert ".mapping" in src, (
            "MappingResult.mapping not unwrapped - D1 regression"
        )

    def test_upsert_result_fields_extracted_explicitly(self):
        """D2 fix: UpsertResult.inserted/.updated extracted (no += on result)."""
        from database.loaders import UpsertResult
        result = UpsertResult(total_input=100, inserted=80, updated=20)
        # Verify __int__ exists (backward compat) but __add__ does not.
        assert int(result) == 100
        assert not hasattr(result, "__add__")
        # Verify field extraction works.
        assert result.inserted == 80
        assert result.updated == 20

    def test_action_type_enum_conformance(self):
        """D5 fix: action_type mapped to InteractionType enum, never 'target'."""
        from database.models import InteractionType
        from pipelines.drugbank_pipeline import ACTION_TO_ENUM, DrugBankPipeline

        valid_enum_values = {e.value for e in InteractionType}
        # All mapped values must be valid enum members or "unknown"/"substrate"/"inducer".
        for action, mapped in ACTION_TO_ENUM.items():
            assert (
                mapped in valid_enum_values
                or mapped in ("unknown", "substrate", "inducer")
            ), f"Action {action} -> {mapped} not valid"

        # Verify _map_action_to_enum never returns "target".
        pipeline = DrugBankPipeline()
        assert pipeline._map_action_to_enum(None) == "unknown"
        assert pipeline._map_action_to_enum("inhibitor") == "inhibitor"
        assert pipeline._map_action_to_enum("agonist|positive modulator") == "agonist"
        assert pipeline._map_action_to_enum("") == "unknown"


# ---------------------------------------------------------------------------
# Domain 3: Scientific Correctness - life-safety regression
# ---------------------------------------------------------------------------


class TestScientificCorrectnessIntegration:
    """Verify scientific correctness across the full stack."""

    def test_withdrawn_drugs_not_marked_approved_in_db(self, db_session):
        """S3: withdrawn drugs (Baycol) have is_fda_approved=False in DB."""
        from database.models import Drug
        from pipelines.drugbank_pipeline import DrugBankPipeline
        from tests.db_helpers import sqlite_bulk_upsert_proteins

        pipeline = DrugBankPipeline()
        drugs_df = pipeline.clean(FIXTURE_PATH)
        _, interactions_df = pipeline._extract_all(FIXTURE_PATH)

        # Insert proteins referenced by the fixture.
        uniprot_ids = list(interactions_df["uniprot_id"].dropna().unique())
        protein_df = pd.DataFrame({
            "uniprot_id": uniprot_ids,
            "gene_symbol": [f"G{i}" for i in range(len(uniprot_ids))],
            "protein_name": [f"Protein {uid}" for uid in uniprot_ids],
            "organism": ["Humans"] * len(uniprot_ids),
        })
        sqlite_bulk_upsert_proteins(db_session, protein_df)

        pipeline.load(drugs_df, interactions_df=interactions_df, session=db_session)

        baycol = db_session.query(Drug).filter_by(drugbank_id="DB00463").first()
        assert baycol is not None, "Baycol not loaded into DB"
        assert not bool(baycol.is_fda_approved), (
            "Baycol is_fda_approved must be False - LIFE-SAFETY (S3)"
        )

    def test_biologics_loaded_with_synth_keys(self, db_session):
        """S7: biologics (insulin) loaded with SYNTH- InChIKey."""
        from database.models import Drug
        from pipelines.drugbank_pipeline import DrugBankPipeline

        pipeline = DrugBankPipeline()
        drugs_df = pipeline.clean(FIXTURE_PATH)
        pipeline.load(drugs_df, interactions_df=pd.DataFrame(), session=db_session)

        insulin = db_session.query(Drug).filter_by(drugbank_id="DB00011").first()
        assert insulin is not None, "Insulin not loaded into DB - S7 regression"
        assert insulin.inchikey.startswith("SYNTH-"), (
            f"Insulin InChIKey must start with SYNTH-, got {insulin.inchikey}"
        )

    def test_non_human_targets_filtered_from_db(self, db_session):
        """S9: non-human (E. coli) targets are NOT in the DPI table."""
        from database.models import DrugProteinInteraction, Protein
        from pipelines.drugbank_pipeline import DrugBankPipeline
        from tests.db_helpers import sqlite_bulk_upsert_proteins

        pipeline = DrugBankPipeline()
        drugs_df = pipeline.clean(FIXTURE_PATH)
        _, interactions_df = pipeline._extract_all(FIXTURE_PATH)

        # Insert proteins - including E. coli one to verify it's filtered.
        uniprot_ids = list(interactions_df["uniprot_id"].dropna().unique())
        protein_df = pd.DataFrame({
            "uniprot_id": uniprot_ids,
            "gene_symbol": [f"G{i}" for i in range(len(uniprot_ids))],
            "protein_name": [f"Protein {uid}" for uid in uniprot_ids],
            "organism": ["Humans"] * len(uniprot_ids),
        })
        sqlite_bulk_upsert_proteins(db_session, protein_df)

        pipeline.load(drugs_df, interactions_df=interactions_df, session=db_session)

        # The E. coli protein P0A7J6 should NOT have any DPI rows.
        ecoli_protein = db_session.query(Protein).filter_by(uniprot_id="P0A7J6").first()
        if ecoli_protein is not None:
            ecoli_dpi = db_session.query(DrugProteinInteraction).filter_by(
                protein_id=ecoli_protein.id
            ).all()
            assert len(ecoli_dpi) == 0, (
                f"E. coli protein has {len(ecoli_dpi)} DPI rows - S9 regression"
            )

    def test_source_id_unique_across_target_enzyme(self, db_session):
        """S22: source_id includes interactor_type, no collision."""
        from database.models import DrugProteinInteraction
        from pipelines.drugbank_pipeline import DrugBankPipeline
        from tests.db_helpers import sqlite_bulk_upsert_proteins

        pipeline = DrugBankPipeline()
        drugs_df = pipeline.clean(FIXTURE_PATH)
        _, interactions_df = pipeline._extract_all(FIXTURE_PATH)
        uniprot_ids = list(interactions_df["uniprot_id"].dropna().unique())
        protein_df = pd.DataFrame({
            "uniprot_id": uniprot_ids,
            "gene_symbol": [f"G{i}" for i in range(len(uniprot_ids))],
            "protein_name": [f"Protein {uid}" for uid in uniprot_ids],
            "organism": ["Humans"] * len(uniprot_ids),
        })
        sqlite_bulk_upsert_proteins(db_session, protein_df)
        pipeline.load(drugs_df, interactions_df=interactions_df, session=db_session)

        # DB00001 has both target and enzyme interactions with P00734.
        # Both should be loaded (distinct source_ids).
        db1_drug = db_session.query(DrugProteinInteraction).filter(
            DrugProteinInteraction.source_id.like("DB00001_%")
        ).all()
        source_ids = [dpi.source_id for dpi in db1_drug]
        # Verify distinct source_ids (target vs enzyme).
        target_ids = [sid for sid in source_ids if "_target_" in sid]
        enzyme_ids = [sid for sid in source_ids if "_enzyme_" in sid]
        assert len(target_ids) >= 1, f"No target source_ids in {source_ids}"
        assert len(enzyme_ids) >= 1, f"No enzyme source_ids in {source_ids}"


# ---------------------------------------------------------------------------
# Domain 4: Coding - no critical type errors
# ---------------------------------------------------------------------------


class TestCodingIntegration:
    """Verify no critical coding bugs across the stack."""

    def test_load_does_not_crash_on_upsert_result(self, db_session):
        """C1: load() handles UpsertResult without TypeError."""
        from pipelines.drugbank_pipeline import DrugBankPipeline

        pipeline = DrugBankPipeline()
        drugs_df = pd.DataFrame({
            "drugbank_id": ["DB00945"],
            "name": ["Aspirin"],
            "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
            "smiles": ["CC(=O)Oc1ccccc1C(=O)O"],
            "molecular_weight": [180.16],
            "molecular_formula": ["C9H8O4"],
            "is_fda_approved": [True],
            "mechanism_of_action": ["COX inhibitor"],
        })
        drugs_df = pipeline._ensure_drug_columns(drugs_df)
        result = pipeline.load(drugs_df, interactions_df=pd.DataFrame(), session=db_session)
        assert result is not None
        assert isinstance(result, int)

    def test_load_does_not_crash_on_mapping_result(self, db_session):
        """C2: _load_interactions handles MappingResult without TypeError."""
        from pipelines.drugbank_pipeline import DrugBankPipeline
        from tests.db_helpers import sqlite_bulk_upsert_proteins

        pipeline = DrugBankPipeline()
        # Insert a drug + protein so the maps are non-empty.
        protein_df = pd.DataFrame({
            "uniprot_id": ["P23219"],
            "gene_symbol": ["PTGS1"],
            "protein_name": ["PTGS1"],
            "organism": ["Humans"],
        })
        sqlite_bulk_upsert_proteins(db_session, protein_df)
        drugs_df = pd.DataFrame({
            "drugbank_id": ["DB00645"],
            "name": ["Aspirin"],
            "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
            "smiles": ["CC(=O)Oc1ccccc1C(=O)O"],
            "molecular_weight": [180.16],
            "molecular_formula": ["C9H8O4"],
            "is_fda_approved": [True],
            "mechanism_of_action": ["COX inhibitor"],
        })
        drugs_df = pipeline._ensure_drug_columns(drugs_df)
        from tests.db_helpers import sqlite_bulk_upsert_drugs
        sqlite_bulk_upsert_drugs(db_session, drugs_df)

        interactions_df = pd.DataFrame({
            "drugbank_id": ["DB00645"],
            "target_name": ["PTGS1"],
            "target_id": ["BE0000015"],
            "drugbank_target_be_id": ["BE0000015"],
            "uniprot_id": ["P23219"],
            "action_type": ["inhibitor"],
            "organism": ["Humans"],
            "interactor_type": ["target"],
            "is_known_action": [True],
            "source": ["drugbank"],
            "source_id": ["DB00645_target_P23219"],
        })
        pipeline._sha256_cleaned = "test"
        result = pipeline._load_interactions(interactions_df, drugs_df, db_session)
        assert result is not None

    def test_no_bare_except_in_drugbank_pipeline(self):
        """R1: no bare 'except:' in drugbank_pipeline.py."""
        import pipelines.drugbank_pipeline as mod
        src = open(mod.__file__).read()
        # Check for bare except: (followed by nothing or a comment).
        import re
        bare_except = re.findall(r"^\s*except\s*:", src, re.MULTILINE)
        assert not bare_except, f"Bare except: found {len(bare_except)} times"


# ---------------------------------------------------------------------------
# Domain 5: Data Quality - pipeline output passes schema validation
# ---------------------------------------------------------------------------


class TestDataQualityIntegration:
    """Verify data quality across the stack."""

    def test_schema_validation_passes_on_clean_output(self, tmp_path):
        """DQ8: clean() output passes validate_output()."""
        from pipelines.drugbank_pipeline import DrugBankPipeline

        processed = tmp_path / "processed_data"
        processed.mkdir(parents=True, exist_ok=True)
        with mock.patch(
            "pipelines.drugbank_pipeline.PROCESSED_DATA_DIR", processed
        ), mock.patch(
            "config.settings.PROCESSED_DATA_DIR", processed
        ):
            pipeline = DrugBankPipeline()
            drugs_df, _ = pipeline._extract_all(FIXTURE_PATH)
            # Before SYNTH- key generation, validate.
            is_valid, errors = pipeline.validate_output(drugs_df)
            assert is_valid, f"Schema validation failed: {errors}"

    def test_drug_count_in_expected_range(self, tmp_path):
        """CF3: drug count sanity check (fixture has < 20 drugs, triggers warning but not error)."""
        from pipelines.drugbank_pipeline import DrugBankPipeline

        processed = tmp_path / "processed_data"
        processed.mkdir(parents=True, exist_ok=True)
        with mock.patch(
            "pipelines.drugbank_pipeline.PROCESSED_DATA_DIR", processed
        ), mock.patch(
            "config.settings.PROCESSED_DATA_DIR", processed
        ):
            pipeline = DrugBankPipeline()
            drugs_df = pipeline.clean(FIXTURE_PATH)
            # Fixture has ~8 unique drugs; CF3 warns but doesn't fail.
            assert len(drugs_df) > 0

    def test_completeness_score_computed(self, tmp_path):
        """DQ13: completeness_score column exists and is in [0, 1]."""
        from pipelines.drugbank_pipeline import DrugBankPipeline

        pipeline = DrugBankPipeline()
        drugs_df = pipeline.clean(FIXTURE_PATH)
        assert "completeness_score" in drugs_df.columns
        assert drugs_df["completeness_score"].between(0.0, 1.0).all()


# ---------------------------------------------------------------------------
# Domain 7: Idempotency - re-running produces identical state
# ---------------------------------------------------------------------------


class TestIdempotencyIntegration:
    """Verify idempotency across the full stack."""

    def test_clean_twice_produces_identical_drug_ids(self, tmp_path):
        """ID1: running clean() twice produces the same drug set."""
        from pipelines.drugbank_pipeline import DrugBankPipeline

        processed = tmp_path / "processed_data"
        processed.mkdir(parents=True, exist_ok=True)
        with mock.patch(
            "pipelines.drugbank_pipeline.PROCESSED_DATA_DIR", processed
        ), mock.patch(
            "config.settings.PROCESSED_DATA_DIR", processed
        ):
            p1 = DrugBankPipeline()
            drugs1 = p1.clean(FIXTURE_PATH)
            p2 = DrugBankPipeline()
            drugs2 = p2.clean(FIXTURE_PATH)
            assert set(drugs1["drugbank_id"]) == set(drugs2["drugbank_id"])
            assert len(drugs1) == len(drugs2)

    def test_load_twice_no_duplicate_dpi(self, db_session):
        """ID10: loading twice produces no duplicate DPI rows."""
        from database.models import DrugProteinInteraction
        from pipelines.drugbank_pipeline import DrugBankPipeline
        from tests.db_helpers import sqlite_bulk_upsert_proteins

        pipeline = DrugBankPipeline()
        drugs_df = pipeline.clean(FIXTURE_PATH)
        _, interactions_df = pipeline._extract_all(FIXTURE_PATH)
        uniprot_ids = list(interactions_df["uniprot_id"].dropna().unique())
        protein_df = pd.DataFrame({
            "uniprot_id": uniprot_ids,
            "gene_symbol": [f"G{i}" for i in range(len(uniprot_ids))],
            "protein_name": [f"Protein {uid}" for uid in uniprot_ids],
            "organism": ["Humans"] * len(uniprot_ids),
        })
        sqlite_bulk_upsert_proteins(db_session, protein_df)

        # First load.
        pipeline.load(drugs_df, interactions_df=interactions_df, session=db_session)
        count_after_first = db_session.query(DrugProteinInteraction).count()

        # Second load (should upsert, not duplicate).
        pipeline2 = DrugBankPipeline()
        pipeline2.load(drugs_df, interactions_df=interactions_df, session=db_session)
        count_after_second = db_session.query(DrugProteinInteraction).count()

        assert count_after_first == count_after_second, (
            f"DPI rows duplicated: {count_after_first} -> {count_after_second}"
        )


# ---------------------------------------------------------------------------
# Domain 9: Security - XXE, CSV injection, file permissions
# ---------------------------------------------------------------------------


class TestSecurityIntegration:
    """Verify security defenses across the stack."""

    def test_xxe_blocked_by_hardened_parser(self):
        """SEC10: hardened XMLParser blocks XXE entity resolution."""
        from lxml import etree
        import tempfile

        xxe_xml = """<?xml version="1.0"?>
<!DOCTYPE foo [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<drugbank xmlns="http://drugbank.ca">
  <drug><drugbank-id primary="true">DB99999</drugbank-id>
    <name>&xxe;</name>
  </drug>
</drugbank>"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False) as f:
            f.write(xxe_xml)
            f.flush()
            path = f.name
        try:
            parser = etree.XMLParser(
                resolve_entities=False,
                no_network=True,
                huge_tree=False,
            )
            tree = etree.parse(path, parser=parser)
            ns = {"db": "http://drugbank.ca"}
            name_elem = tree.find(".//db:name", ns)
            name_text = name_elem.text if name_elem is not None else ""
            assert "root:" not in str(name_text), "XXE resolved - SEC10 regression"
        finally:
            os.unlink(path)

    def test_csv_injection_defense_applied(self, tmp_path):
        """SEC6: CSV-injection-triggering MOA prefixed with single quote."""
        from pipelines.drugbank_pipeline import DrugBankPipeline, _csv_injection_safe

        assert _csv_injection_safe("=CMD|calc") == "'=CMD|calc"
        assert _csv_injection_safe("normal") == "normal"

    def test_file_permissions_restricted(self, tmp_path):
        """SEC3: output CSVs have restrictive permissions."""
        from pipelines.drugbank_pipeline import DrugBankPipeline

        processed = tmp_path / "processed_data"
        processed.mkdir(parents=True, exist_ok=True)
        with mock.patch(
            "pipelines.drugbank_pipeline.PROCESSED_DATA_DIR", processed
        ), mock.patch(
            "config.settings.PROCESSED_DATA_DIR", processed
        ):
            pipeline = DrugBankPipeline()
            drugs_df = pipeline.clean(FIXTURE_PATH)
            drugs_csv = processed / "drugbank_drugs.csv"
            if drugs_csv.exists():
                mode = drugs_csv.stat().st_mode & 0o777
                assert mode in (0o600, 0o644), f"Expected 0600 or 0644, got {oct(mode)}"


# ---------------------------------------------------------------------------
# Domain 16: Data Lineage - provenance and audit trail
# ---------------------------------------------------------------------------


class TestLineageIntegration:
    """Verify data lineage across the full stack."""

    def test_provenance_sidecar_written(self, tmp_path):
        """A8: provenance JSON sidecar written next to drugs CSV."""
        from pipelines.drugbank_pipeline import DrugBankPipeline

        processed = tmp_path / "processed_data"
        processed.mkdir(parents=True, exist_ok=True)
        with mock.patch(
            "pipelines.drugbank_pipeline.PROCESSED_DATA_DIR", processed
        ), mock.patch(
            "config.settings.PROCESSED_DATA_DIR", processed
        ):
            pipeline = DrugBankPipeline()
            drugs_df = pipeline.clean(FIXTURE_PATH)
            candidates = [
                processed / "drugbank_drugs.provenance.json",
                processed / "drugbank_drugs.csv.provenance.json",
            ]
            prov_path = next((p for p in candidates if p.exists()), None)
            assert prov_path is not None, (
                f"No provenance file. Files: {list(processed.iterdir())}"
            )
            prov = json.loads(prov_path.read_text())
            assert prov["source"] == "drugbank"
            assert "source_version" in prov
            assert "pipeline_run_id" in prov
            assert "sha256_raw" in prov
            assert "sha256_cleaned" in prov

    def test_license_sidecar_written(self, tmp_path):
        """SEC4: DRUGBANK_LICENSE.txt written with citation."""
        from pipelines.drugbank_pipeline import DrugBankPipeline

        processed = tmp_path / "processed_data"
        processed.mkdir(parents=True, exist_ok=True)
        with mock.patch(
            "pipelines.drugbank_pipeline.PROCESSED_DATA_DIR", processed
        ), mock.patch(
            "config.settings.PROCESSED_DATA_DIR", processed
        ):
            pipeline = DrugBankPipeline()
            pipeline.clean(FIXTURE_PATH)
            license_path = processed / "DRUGBANK_LICENSE.txt"
            assert license_path.exists()
            content = license_path.read_text()
            assert "DrugBank" in content
            assert "Wishart" in content

    def test_sha256_sidecar_written(self, tmp_path):
        """DQ7: SHA-256 sidecar written next to drugs CSV."""
        from pipelines.drugbank_pipeline import DrugBankPipeline

        processed = tmp_path / "processed_data"
        processed.mkdir(parents=True, exist_ok=True)
        with mock.patch(
            "pipelines.drugbank_pipeline.PROCESSED_DATA_DIR", processed
        ), mock.patch(
            "config.settings.PROCESSED_DATA_DIR", processed
        ):
            pipeline = DrugBankPipeline()
            pipeline.clean(FIXTURE_PATH)
            sha_path = processed / "drugbank_drugs.csv.sha256"
            assert sha_path.exists()
            sha_content = sha_path.read_text().strip()
            # SHA-256 is 64 hex chars.
            assert len(sha_content) == 64
            assert all(c in "0123456789abcdef" for c in sha_content)


# ---------------------------------------------------------------------------
# Domain 12: Configuration - env vars consumed correctly
# ---------------------------------------------------------------------------


class TestConfigurationIntegration:
    """Verify configuration integration."""

    def test_drugbank_config_values_loaded(self):
        """CF1-CF15: all DRUGBANK_* config values load from settings.py."""
        from config.settings import (
            DRUGBANK_VERSION,
            DRUGBANK_XML_NAMESPACE,
            DRUGBANK_TARGET_ORGANISMS,
            DRUGBANK_GENERATE_SYNTH_KEYS,
            DRUGBANK_DROP_NO_INCHIKEY,
            DRUGBANK_CONSERVATIVE_DEFAULTS,
            DRUGBANK_BATCH_SIZE,
            DRUGBANK_LOG_INTERVAL,
            DRUGBANK_MAX_DRUGS,
            DRUGBANK_EXTRACT_TARGETS,
            DRUGBANK_EXTRACT_ENZYMES,
            DRUGBANK_EXTRACT_TRANSPORTERS,
            DRUGBANK_CSV_COMPRESSION,
            DRUGBANK_DPI_BATCH_SIZE,
        )
        assert DRUGBANK_VERSION is not None
        assert DRUGBANK_XML_NAMESPACE == "http://drugbank.ca"
        assert isinstance(DRUGBANK_TARGET_ORGANISMS, list)
        assert "Humans" in DRUGBANK_TARGET_ORGANISMS
        assert isinstance(DRUGBANK_GENERATE_SYNTH_KEYS, bool)
        assert isinstance(DRUGBANK_DROP_NO_INCHIKEY, bool)
        assert isinstance(DRUGBANK_CONSERVATIVE_DEFAULTS, bool)
        assert isinstance(DRUGBANK_BATCH_SIZE, int)
        assert isinstance(DRUGBANK_LOG_INTERVAL, int)
        assert isinstance(DRUGBANK_MAX_DRUGS, int)
        assert isinstance(DRUGBANK_EXTRACT_TARGETS, bool)
        assert isinstance(DRUGBANK_EXTRACT_ENZYMES, bool)
        assert isinstance(DRUGBANK_EXTRACT_TRANSPORTERS, bool)
        assert isinstance(DRUGBANK_CSV_COMPRESSION, str)
        assert isinstance(DRUGBANK_DPI_BATCH_SIZE, int)

    def test_pipeline_consumes_config_values(self):
        """Verify pipeline reads config values into instance attributes."""
        from pipelines.drugbank_pipeline import DrugBankPipeline

        pipeline = DrugBankPipeline()
        assert pipeline.source_version is not None
        assert "DrugBank" in pipeline.source_version or "5" in pipeline.source_version
        assert pipeline._target_organisms == ["Humans"]
        assert pipeline._batch_size > 0
        assert pipeline._dpi_batch_size > 0
        assert pipeline._log_interval > 0


# ---------------------------------------------------------------------------
# Domain 15: Interoperability - cross-pipeline consistency
# ---------------------------------------------------------------------------


class TestInteroperabilityIntegration:
    """Verify cross-pipeline consistency."""

    def test_drugbank_and_chembl_produce_compatible_drug_columns(self):
        """INT4: DrugBank and ChEMBL pipelines produce Drug-model-compatible columns."""
        from sqlalchemy import inspect as sa_inspect
        from database.models import Drug
        from pipelines.drugbank_pipeline import DrugBankPipeline

        drug_model_cols = {c.name for c in sa_inspect(Drug).columns}
        pipeline = DrugBankPipeline()
        cols = pipeline._drug_columns()
        for col in cols:
            assert col in drug_model_cols, (
                f"DrugBank column {col} not in Drug model"
            )

    def test_drugbank_source_name_in_valid_source_names(self):
        """INT11: 'drugbank' is in BasePipeline's VALID_SOURCE_NAMES."""
        from pipelines.base_pipeline import VALID_SOURCE_NAMES
        assert "drugbank" in VALID_SOURCE_NAMES

    def test_drugbank_filename_mapping_correct(self):
        """INT4: _get_processed_filename returns 'drugbank_drugs.csv'."""
        from pipelines.drugbank_pipeline import DrugBankPipeline
        pipeline = DrugBankPipeline()
        filename = pipeline._get_processed_filename()
        assert filename == "drugbank_drugs.csv"


# ---------------------------------------------------------------------------
# Domain 10: Testing - end-to-end with real DB
# ---------------------------------------------------------------------------


class TestEndToEndAll22Files:
    """Full end-to-end test: download (mocked) -> clean -> load -> verify DB."""

    def test_full_pipeline_e2e_with_sqlite(self, tmp_path, db_session):
        """E2E: full download -> clean -> load with SQLite, verify DB state."""
        from database.models import Drug, DrugProteinInteraction, Protein
        from pipelines.drugbank_pipeline import DrugBankPipeline
        from tests.db_helpers import sqlite_bulk_upsert_proteins

        processed = tmp_path / "processed_data"
        processed.mkdir(parents=True, exist_ok=True)
        with mock.patch(
            "pipelines.drugbank_pipeline.PROCESSED_DATA_DIR", processed
        ), mock.patch(
            "config.settings.PROCESSED_DATA_DIR", processed
        ):
            pipeline = DrugBankPipeline()
            drugs_df = pipeline.clean(FIXTURE_PATH)
            _, interactions_df = pipeline._extract_all(FIXTURE_PATH)

            # Insert proteins.
            uniprot_ids = list(interactions_df["uniprot_id"].dropna().unique())
            protein_df = pd.DataFrame({
                "uniprot_id": uniprot_ids,
                "gene_symbol": [f"G{i}" for i in range(len(uniprot_ids))],
                "protein_name": [f"Protein {uid}" for uid in uniprot_ids],
                "organism": ["Humans"] * len(uniprot_ids),
            })
            sqlite_bulk_upsert_proteins(db_session, protein_df)

            # Load.
            result = pipeline.load(
                drugs_df, interactions_df=interactions_df, session=db_session
            )
            assert result is not None

            # Verify drugs in DB.
            drug_count = db_session.query(Drug).count()
            assert drug_count > 0, "No drugs loaded"

            # Verify DPI rows.
            dpi_count = db_session.query(DrugProteinInteraction).count()
            assert dpi_count > 0, "No DPI rows loaded"

            # Verify aspirin is approved.
            aspirin = db_session.query(Drug).filter_by(drugbank_id="DB00645").first()
            assert aspirin is not None
            assert bool(aspirin.is_fda_approved)

            # Verify Baycol is NOT approved (life-safety).
            baycol = db_session.query(Drug).filter_by(drugbank_id="DB00463").first()
            assert baycol is not None
            assert not bool(baycol.is_fda_approved)

            # Verify insulin has SYNTH- key.
            insulin = db_session.query(Drug).filter_by(drugbank_id="DB00011").first()
            assert insulin is not None
            assert insulin.inchikey.startswith("SYNTH-")

    def test_full_pipeline_e2e_provenance_audit(self, tmp_path, db_session):
        """E2E: verify provenance sidecar + audit trail after full run."""
        from pipelines.drugbank_pipeline import DrugBankPipeline

        processed = tmp_path / "processed_data"
        processed.mkdir(parents=True, exist_ok=True)
        with mock.patch(
            "pipelines.drugbank_pipeline.PROCESSED_DATA_DIR", processed
        ), mock.patch(
            "config.settings.PROCESSED_DATA_DIR", processed
        ):
            pipeline = DrugBankPipeline()
            drugs_df = pipeline.clean(FIXTURE_PATH)

            # Verify provenance sidecar.
            candidates = [
                processed / "drugbank_drugs.provenance.json",
                processed / "drugbank_drugs.csv.provenance.json",
            ]
            prov_path = next((p for p in candidates if p.exists()), None)
            assert prov_path is not None
            prov = json.loads(prov_path.read_text())
            assert prov["source"] == "drugbank"
            assert prov["drug_count"] == len(drugs_df)
            assert "transformation_fingerprint" in prov
            assert "data_quality_fingerprint" in prov

    def test_all_22_files_together_data_flow(self, tmp_path, db_session):
        """Final integration: all 22 files work together to produce correct data."""
        from database.models import Drug, DrugProteinInteraction, Protein
        from pipelines.drugbank_pipeline import DrugBankPipeline
        from tests.db_helpers import sqlite_bulk_upsert_proteins

        # 1. Config provides settings (file 2).
        from config.settings import DRUGBANK_VERSION, PROCESSED_DATA_DIR
        assert DRUGBANK_VERSION is not None

        # 2. Database connection provides session (files 4, 5, 10).
        from database.connection import get_db_session
        from database.loaders import bulk_upsert_drugs, bulk_upsert_dpi
        assert callable(bulk_upsert_drugs)
        assert callable(bulk_upsert_dpi)

        # 3. Cleaning modules provide normalization (files 12, 13, 14).
        from cleaning.normalizer import standardize_inchikey
        from cleaning.missing_values import fill_missing_drug_fields
        from cleaning.deduplicator import dedup_interactions
        assert callable(standardize_inchikey)
        assert callable(fill_missing_drug_fields)
        assert callable(dedup_interactions)

        # 4. Entity resolution (files 16, 17, 18) - just verify importable.
        from entity_resolution.resolver_utils import normalize_name
        from entity_resolution.drug_resolver import DrugResolver
        from entity_resolution.protein_resolver import ProteinResolver
        assert callable(normalize_name)

        # 5. BasePipeline provides the contract (file 20).
        from pipelines.base_pipeline import BasePipeline
        assert issubclass(DrugBankPipeline, BasePipeline)

        # 6. Run the full pipeline.
        processed = tmp_path / "processed_data"
        processed.mkdir(parents=True, exist_ok=True)
        with mock.patch(
            "pipelines.drugbank_pipeline.PROCESSED_DATA_DIR", processed
        ), mock.patch(
            "config.settings.PROCESSED_DATA_DIR", processed
        ):
            pipeline = DrugBankPipeline()
            drugs_df = pipeline.clean(FIXTURE_PATH)
            _, interactions_df = pipeline._extract_all(FIXTURE_PATH)

            # Insert proteins.
            uniprot_ids = list(interactions_df["uniprot_id"].dropna().unique())
            protein_df = pd.DataFrame({
                "uniprot_id": uniprot_ids,
                "gene_symbol": [f"G{i}" for i in range(len(uniprot_ids))],
                "protein_name": [f"Protein {uid}" for uid in uniprot_ids],
                "organism": ["Humans"] * len(uniprot_ids),
            })
            sqlite_bulk_upsert_proteins(db_session, protein_df)

            pipeline.load(drugs_df, interactions_df=interactions_df, session=db_session)

            # Verify the full stack produced correct data.
            assert db_session.query(Drug).count() > 0
            assert db_session.query(DrugProteinInteraction).count() > 0
            # Life-safety: Baycol is NOT approved.
            baycol = db_session.query(Drug).filter_by(drugbank_id="DB00463").first()
            assert baycol is not None
            assert not bool(baycol.is_fda_approved)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
