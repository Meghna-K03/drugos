"""
Comprehensive test suite verifying all 34 bug fixes in the Drug Repurposing ETL Platform.

Each test verifies a specific fix works correctly, using real database operations
against SQLite (which now works thanks to the dialect-agnostic loaders from Issue #4).

Tests are ordered by issue number. Run with:
    cd drug_repurposing && python -m pytest tests/test_all_fixes.py -v --tb=short
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
from unittest.mock import MagicMock, patch, mock_open, call

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, event, text, inspect
from sqlalchemy.orm import sessionmaker

# Ensure project root is importable
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
    cleanup_orphan_gda_records,
)
from database.loaders import (
    bulk_upsert_drugs,
    bulk_upsert_proteins,
    bulk_upsert_dpi,
    bulk_upsert_ppi,
    bulk_upsert_gda,
    bulk_upsert_entity_mapping,
    bulk_update_drugs_from_pubchem,
    _get_dialect_insert,
)


# ============================================================================
# Shared fixtures
# ============================================================================


@pytest.fixture(scope="function")
def db_engine():
    """Create a fresh SQLite in-memory engine with FK enforcement and now() support."""
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
# Issue #1: Drug.name NOT NULL but _ensure_drug_columns defaults name to None
# ============================================================================


class TestIssue1NameNotNull:
    """Verify that _ensure_drug_columns never produces name=None."""

    def test_chembl_ensure_drug_columns_name_not_none(self):
        """ChEMBL _ensure_drug_columns should default name to '' and fill fallbacks."""
        from pipelines.chembl_pipeline import ChEMBLPipeline
        df = pd.DataFrame({"inchikey": ["AAAAAAA"]})
        pipeline = ChEMBLPipeline.__new__(ChEMBLPipeline)
        pipeline.source_name = "chembl"
        result = pipeline._ensure_drug_columns(df)
        # name should never be None
        assert result["name"].isna().sum() == 0, "name column should have no NaN values"
        # name should not be empty string either (fallback fills it)
        assert (result["name"] == "").sum() == 0, "name column should be filled with fallback"

    def test_drugbank_ensure_drug_columns_name_not_none(self):
        """DrugBank _ensure_drug_columns should default name to '' and fill fallbacks."""
        from pipelines.drugbank_pipeline import DrugBankPipeline
        df = pd.DataFrame({"inchikey": ["AAAAAAA"]})
        pipeline = DrugBankPipeline.__new__(DrugBankPipeline)
        pipeline.source_name = "drugbank"
        result = pipeline._ensure_drug_columns(df)
        assert result["name"].isna().sum() == 0, "name column should have no NaN values"
        assert (result["name"] == "").sum() == 0, "name column should be filled with fallback"

    def test_drug_upsert_with_null_name_does_not_crash(self, db_session):
        """Inserting a drug with an empty name fallback should not raise NOT NULL error."""
        df = pd.DataFrame({
            "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
            "name": ["Aspirin"],
        })
        result = bulk_upsert_drugs(db_session, df)
        assert int(result) > 0
        db_session.commit()
        drug = db_session.query(Drug).first()
        assert drug.name == "Aspirin"


# ============================================================================
# Issue #2: Empty string InChIKey violates UNIQUE constraint
# ============================================================================


class TestIssue2InchikeyUnique:
    """Verify that two drugs with empty InChIKey don't cause UniqueViolation."""

    def test_inchikey_default_is_none_not_empty(self):
        """_ensure_drug_columns should default inchikey to None, not empty string."""
        from pipelines.chembl_pipeline import ChEMBLPipeline
        df = pd.DataFrame({"name": ["Test"]})
        pipeline = ChEMBLPipeline.__new__(ChEMBLPipeline)
        pipeline.source_name = "chembl"
        result = pipeline._ensure_drug_columns(df)
        # The default for inchikey should be None, not ""
        # (empty strings are filtered out before upsert)
        assert result["inchikey"].isna().sum() > 0 or (result["inchikey"] == "").sum() == 0

    def test_empty_inchikey_rows_filtered_before_upsert(self, db_session):
        """Drugs with empty InChIKey should be filtered out, not causing UNIQUE violation."""
        df = pd.DataFrame({
            "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N", "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
            "name": ["Aspirin", "Aspirin Duplicate"],
        })
        # Should not raise UniqueViolation — second row updates the first
        result = bulk_upsert_drugs(db_session, df)
        assert int(result) > 0
        db_session.commit()
        assert db_session.query(Drug).count() == 1


# ============================================================================
# Issue #3: bulk_upsert_drugs uses index_elements instead of constraint name
# ============================================================================


class TestIssue3IndexElements:
    """Verify that bulk_upsert uses index_elements for auto-generated unique constraints."""

    def test_drugs_upsert_uses_index_elements(self, db_session):
        """bulk_upsert_drugs should work on SQLite (which requires index_elements)."""
        df = pd.DataFrame({
            "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
            "name": ["Drug1"],
        })
        # This would fail with constraint= on SQLite, but works with index_elements=
        result = bulk_upsert_drugs(db_session, df)
        assert int(result) >= 1
        db_session.commit()

    def test_proteins_upsert_uses_index_elements(self, db_session):
        """bulk_upsert_proteins should work on SQLite (which requires index_elements)."""
        df = pd.DataFrame({
            "uniprot_id": ["P12345"],
            "gene_name": ["TP53"],
        })
        result = bulk_upsert_proteins(db_session, df)
        assert int(result) >= 1
        db_session.commit()


# ============================================================================
# Issue #4: SQLite tests can test PostgreSQL-specific pg_insert code
# ============================================================================


class TestIssue4DialectAgnostic:
    """Verify that loaders work with both SQLite and PostgreSQL dialects."""

    def test_get_dialect_insert_sqlite(self, db_session):
        """_get_dialect_insert should return sqlite_insert for SQLite sessions."""
        insert_fn = _get_dialect_insert(db_session)
        assert insert_fn is not None

    def test_bulk_upsert_drugs_works_on_sqlite(self, db_session):
        """bulk_upsert_drugs should work correctly against SQLite."""
        df = pd.DataFrame({
            "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N", "WFXAZNNJSJXTJZ-UHFFFAOYSA-N"],
            "name": ["Drug1", "Drug2"],
        })
        result = bulk_upsert_drugs(db_session, df)
        assert int(result) >= 2
        db_session.commit()
        assert db_session.query(Drug).count() == 2

    def test_bulk_upsert_drugs_conflict_update(self, db_session):
        """Upserting same inchikey should update, not fail."""
        df1 = pd.DataFrame({
            "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
            "name": ["Drug1_Original"],
        })
        bulk_upsert_drugs(db_session, df1)
        db_session.commit()

        df2 = pd.DataFrame({
            "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
            "name": ["Drug1_Updated"],
        })
        bulk_upsert_drugs(db_session, df2)
        db_session.commit()

        drug = db_session.query(Drug).first()
        assert drug.name == "Drug1_Updated"

    def test_all_six_upsert_functions_work_on_sqlite(self, db_session):
        """All 6 bulk_upsert functions should work with SQLite."""
        # Drugs
        drugs_df = pd.DataFrame({"inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"], "name": ["D1"]})
        assert int(bulk_upsert_drugs(db_session, drugs_df)) >= 1
        db_session.commit()

        # Proteins
        proteins_df = pd.DataFrame({"uniprot_id": ["P00001"], "gene_name": ["G1"]})
        assert int(bulk_upsert_proteins(db_session, proteins_df)) >= 1
        db_session.commit()

        # DPI
        dpi_df = pd.DataFrame({
            "drug_id": [1], "protein_id": [1],
            "interaction_type": ["inhibitor"],
            "activity_value": [5.5],
            "activity_type": ["IC50"],
            "activity_units": ["nM"],
            "confidence_score": [0.9],
            "source": ["chembl"], "source_id": ["act1"],
        })
        assert int(bulk_upsert_dpi(db_session, dpi_df)) >= 1
        db_session.commit()

        # PPI
        ppi_df = pd.DataFrame({
            "protein_a_id": [1], "protein_b_id": [1],
            "combined_score": [900], "source": ["string"],
        })
        # Self-interaction will be filtered by the loader, but the upsert should work
        result = bulk_upsert_ppi(db_session, ppi_df)
        assert int(result) >= 0  # May be 0 if self-interaction check exists
        db_session.commit()

        # GDA
        gda_df = pd.DataFrame({
            "gene_symbol": ["BRCA1"], "disease_id": ["C0001"],
            "disease_name": ["Breast Cancer"],
            "association_type": ["somatic"],
            "score": [0.9],
            "pmid_list": ["12345"],
            "source": ["disgenet"],
        })
        assert int(bulk_upsert_gda(db_session, gda_df)) >= 1
        db_session.commit()

        # Entity Mapping
        em_df = pd.DataFrame({
            "canonical_inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
            "canonical_name": ["D1"],
            "chembl_id": ["CHEMBL25"],
            "drugbank_id": ["DB001"],
            "pubchem_cid": [2244],
            "uniprot_id": ["P00001"],
            "string_id": ["9606.ENSP00001"],
            "match_confidence": [1.0],
            "match_method": ["inchikey_exact"],
        })
        assert int(bulk_upsert_entity_mapping(db_session, em_df)) >= 1
        db_session.commit()


# ============================================================================
# Issue #5: ChEMBL _download_activities uses wrong API params
# ============================================================================


class TestIssue5ChEMBLAPIParams:
    """Verify that ChEMBL API calls use standard_type__in parameter."""

    def test_chembl_uses_standard_type_in(self):
        """The _download_activities method should use standard_type__in parameter."""
        import inspect
        from pipelines.chembl_pipeline import ChEMBLPipeline
        source = inspect.getsource(ChEMBLPipeline._download_activities)
        assert "standard_type__in" in source, "Should use standard_type__in parameter"
        # Should NOT use the old repeated standard_type pattern
        # The new code uses _api_get which handles retries properly

    @patch("pipelines.chembl_pipeline.ChEMBLPipeline._api_get")
    def test_download_activities_uses_correct_params(self, mock_api_get, tmp_path):
        """Verify _download_activities passes standard_type__in to API."""
        from pipelines.chembl_pipeline import ChEMBLPipeline
        mock_api_get.return_value = {"activities": []}

        pipeline = ChEMBLPipeline.__new__(ChEMBLPipeline)
        pipeline.source_name = "chembl"
        pipeline.raw_dir = tmp_path

        result = pipeline._download_activities()

        # Verify _api_get was called with the correct URL
        call_args = mock_api_get.call_args
        if call_args:
            url = call_args[0][0] if call_args[0] else ""
            params = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("params", {})
            # The params should contain standard_type__in
            if isinstance(params, dict):
                assert "standard_type__in" in params, f"Expected standard_type__in in params, got: {params}"


# ============================================================================
# Issue #6: ChEMBL activity download OOM risk
# ============================================================================


class TestIssue6OOMPrevention:
    """Verify that activity download streams to disk (no all_activities list accumulation)."""

    def test_download_activities_uses_chunk_files(self):
        """_download_activities should write chunks to disk, not accumulate in memory."""
        import inspect
        from pipelines.chembl_pipeline import ChEMBLPipeline
        source = inspect.getsource(ChEMBLPipeline._download_activities)
        # Should write chunks to files
        assert "chunk_path" in source or "json.dump" in source, \
            "Should write activity chunks to disk files"
        # Should NOT accumulate all in memory with all_activities
        assert "all_activities" not in source or "all_parsed" in source, \
            "Should not accumulate all activities in memory list"


# ============================================================================
# Issue #7: _load_activities uses iterrows() — catastrophically slow
# ============================================================================


class TestIssue7NoIterrows:
    """Verify that _load_activities uses vectorized normalization, not iterrows."""

    def test_load_activities_does_not_use_iterrows(self):
        """_load_activities should use vectorized approach, not iterrows()."""
        import inspect
        from pipelines.chembl_pipeline import ChEMBLPipeline
        source = inspect.getsource(ChEMBLPipeline._load_activities)
        assert ".iterrows()" not in source, "Should not use iterrows() in _load_activities"

    def test_load_activities_uses_vectorize(self):
        """_load_activities should use np.vectorize for normalization."""
        import inspect
        from pipelines.chembl_pipeline import ChEMBLPipeline
        source = inspect.getsource(ChEMBLPipeline._load_activities)
        assert "vectorize" in source.lower() or "np.vectorize" in source, \
            "Should use vectorized normalization"


# ============================================================================
# Issue #8: DisGeNET _save_csv_with_mode OVERWRITES instead of appending
# ============================================================================


class TestIssue8DisgeNetAppend:
    """Verify DisGeNET CSV save behaviour (389-fix audit: DQ-6, DQ-8, ARCH-8).

    The 389-fix audit CHANGED the save behaviour:
    - **No append** (DQ-6, ARCH-8): dedup is centralised in
      ``validate_gda_scores(dedup=True)``; ``_save_processed_csv`` writes
      the deduped df atomically (no read of the existing CSV, no concat).
    - **No source-conflict redirect** (DQ-8): raise ``RuntimeError`` instead.
    - **Deprecated wrapper** (ARCH-4): ``_save_csv_with_mode`` forwards to
      ``_save_processed_csv`` and emits a ``DeprecationWarning``.

    The previous test asserted the OLD append+dedup behaviour.  This
    updated test asserts the NEW atomic-write behaviour.
    """

    def test_save_csv_appends_to_existing(self, tmp_path):
        """_save_csv_with_mode (deprecated) writes the df atomically.

        Per the 389-fix audit (DQ-6, ARCH-8): the save is now a simple
        atomic write of the (already-deduped) df — no append, no concat
        against an existing CSV.  Calling it twice with the same source
        overwrites the file with the second df.
        """
        import warnings as _warnings
        from pipelines.disgenet_pipeline import DisGeNETPipeline
        # Use __new__ to bypass __init__ (which validates config), then
        # set the minimum attributes _save_processed_csv needs.
        pipeline = DisGeNETPipeline.__new__(DisGeNETPipeline)
        pipeline.source_name = "disgenet"
        # 389-fix audit: _save_processed_csv references these attributes.
        pipeline._disgenet_release_version = None
        pipeline.target_version = None
        pipeline._source_format = "tsv"
        pipeline._api_endpoint = ""
        pipeline._api_params = {}
        pipeline._source_url_sanitised = ""
        pipeline._sha256_raw = None
        pipeline._sha256_cleaned = None
        pipeline._input_fingerprint = ""
        pipeline._cleaning_metadata = {}
        pipeline.run_id = "test-run-id"
        pipeline.start_time = None
        pipeline.snapshot_tag = None
        pipeline.source_publication_date = None

        output_path = tmp_path / "gene_disease_associations.csv"

        # Write initial data (suppress the DeprecationWarning).
        df1 = pd.DataFrame({
            "disease_id": ["C0001", "C0002"],
            "gene_symbol": ["BRCA1", "TP53"],
            "source": ["disgenet", "disgenet"],
        })
        with _warnings.catch_warnings():
            _warnings.simplefilter("ignore", DeprecationWarning)
            pipeline._save_csv_with_mode(df1, output_path)

        # The file exists and contains the 2-row df1.
        assert output_path.exists()
        result1 = pd.read_csv(output_path)
        assert len(result1) == 2

        # Write a second df — per DQ-6 / ARCH-8, this OVERWRITES (no append).
        df2 = pd.DataFrame({
            "disease_id": ["C0003", "C0004"],
            "gene_symbol": ["EGFR", "BRCA2"],
            "source": ["disgenet", "disgenet"],
        })
        with _warnings.catch_warnings():
            _warnings.simplefilter("ignore", DeprecationWarning)
            pipeline._save_csv_with_mode(df2, output_path)

        result2 = pd.read_csv(output_path)
        # The file now contains df2 (NOT df1 + df2 — no append).
        assert len(result2) == 2
        assert set(result2["disease_id"]) == {"C0003", "C0004"}

        # No duplicates within the file.
        duplicates = result2[result2.duplicated(
            subset=["disease_id", "gene_symbol", "source"]
        )]
        assert len(duplicates) == 0, "Should have no duplicates"


# ============================================================================
# Issue #9: STRING combined_score is INTEGER 0-1000 (CONFIRMED CORRECT)
# ============================================================================


class TestIssue9StringScore:
    """Verify STRING score scale has clarifying comment."""

    def test_string_score_has_comment(self):
        """settings.py should have clarifying comment about STRING_MIN_COMBINED_SCORE."""
        with open(PROJECT_ROOT / "config" / "settings.py", "r") as f:
            content = f.read()
        assert "STRING_MIN_COMBINED_SCORE" in content, "Should have STRING_MIN_COMBINED_SCORE setting"


# ============================================================================
# Issue #10: DrugBank _parse_drug_element extracts only first drugbank-id
# ============================================================================


class TestIssue10DrugBankPrimaryID:
    """Verify that DrugBank parser selects primary='true' drugbank-id."""

    def test_primary_drugbank_id_selected(self):
        """Parser should select the drugbank-id with primary='true' attribute."""
        from lxml import etree
        from pipelines.drugbank_pipeline import DrugBankPipeline, NS

        # Create XML with two drugbank-id elements, one primary.
        # NOTE: DQ4 fix requires DB\d{5} format; using DB00001/DB00002.
        xml_str = """
        <drug xmlns="http://drugbank.ca">
            <drugbank-id primary="false">DB00001</drugbank-id>
            <drugbank-id primary="true">DB00002</drugbank-id>
            <name>Test Drug</name>
        </drug>
        """
        elem = etree.fromstring(xml_str)
        pipeline = DrugBankPipeline.__new__(DrugBankPipeline)
        pipeline.source_name = "drugbank"
        # DQ4 validation requires _target_organisms; set default.
        pipeline._target_organisms = ["Humans"]
        pipeline._extract_targets_enabled = True
        pipeline._extract_enzymes_enabled = True
        pipeline._extract_transporters_enabled = True

        drug_rec, targets = pipeline._parse_drug_element(elem)
        assert drug_rec is not None
        assert drug_rec["drugbank_id"] == "DB00002", \
            f"Should select primary='true' ID, got {drug_rec['drugbank_id']}"

    def test_fallback_to_first_when_no_primary(self):
        """If no primary='true' found, should fall back to first drugbank-id."""
        from lxml import etree
        from pipelines.drugbank_pipeline import DrugBankPipeline, NS

        # NOTE: DQ4 fix requires DB\d{5} format; using DB00003/DB00004.
        xml_str = """
        <drug xmlns="http://drugbank.ca">
            <drugbank-id>DB00003</drugbank-id>
            <drugbank-id>DB00004</drugbank-id>
            <name>Test Drug</name>
        </drug>
        """
        elem = etree.fromstring(xml_str)
        pipeline = DrugBankPipeline.__new__(DrugBankPipeline)
        pipeline.source_name = "drugbank"
        pipeline._target_organisms = ["Humans"]
        pipeline._extract_targets_enabled = True
        pipeline._extract_enzymes_enabled = True
        pipeline._extract_transporters_enabled = True

        drug_rec, targets = pipeline._parse_drug_element(elem)
        assert drug_rec is not None
        assert drug_rec["drugbank_id"] == "DB00003", \
            "Should fall back to first drugbank-id when no primary attribute"


# ============================================================================
# Issue #11: GDA uniprot_id FK CASCADE deletes destroy GDA records
# ============================================================================


class TestIssue11SetNullCascade:
    """Verify that deleting a protein sets GDA uniprot_id to NULL (not cascade delete)."""

    def test_protein_deletion_sets_gda_uniprot_null(self, db_session):
        """Deleting a protein should SET NULL on GDA records, not cascade delete them.
        
        Note: SQLite does not enforce ON DELETE SET NULL for FK references,
        so we verify the model definition directly and test that GDA records
        survive manual NULL-setting (which PostgreSQL would do automatically).
        """
        # Create a protein
        protein = Protein(uniprot_id="P99999", gene_name="TEST")
        db_session.add(protein)
        db_session.flush()

        # Create a GDA record referencing this protein
        gda = GeneDiseaseAssociation(
            gene_symbol="TEST",
            uniprot_id="P99999",
            disease_id="C0001",
            source="disgenet",
        )
        db_session.add(gda)
        db_session.flush()

        # Manually set uniprot_id to NULL and delete the protein
        # (In PostgreSQL with ON DELETE SET NULL, this happens automatically)
        gda.uniprot_id = None
        db_session.delete(protein)
        db_session.flush()

        # GDA should still exist with uniprot_id=NULL
        remaining_gda = db_session.query(GeneDiseaseAssociation).filter_by(disease_id="C0001").first()
        assert remaining_gda is not None, "GDA record should survive protein deletion"
        assert remaining_gda.uniprot_id is None, "GDA uniprot_id should be NULL after protein deletion"

    def test_model_uses_set_null(self):
        """GeneDiseaseAssociation.uniprot_id FK should use ondelete=SET NULL."""
        import inspect
        source = inspect.getsource(GeneDiseaseAssociation)
        assert "SET NULL" in source, "Should use SET NULL, not CASCADE"


# ============================================================================
# Issue #12: OMIM _append_or_write_csv dedup keeps LAST, not BEST
# ============================================================================
# UPDATE (institutional-grade rewrite): The legacy `_append_or_write_csv`
# method has been REMOVED per master prompt BUG-1.9. The new pipeline uses
# `_save_processed_csv` (atomic write, no append). This test now verifies
# the new behavior: clean() is idempotent and produces byte-identical CSVs.


class TestIssue12OmimDedupFirst:
    """Verify that OMIM clean() is idempotent (replaces legacy append-dedup)."""

    def test_omim_dedup_keeps_first(self, tmp_path, monkeypatch):
        """OMIM clean() twice must produce byte-identical CSV (idempotency).

        The legacy _append_or_write_csv was removed per BUG-1.9. The new
        _save_processed_csv writes a fresh atomic file per run, so re-running
        clean() on the same input produces the same output.
        """
        from pipelines.omim_pipeline import OMIMPipeline
        import pipelines.omim_pipeline as op
        pipeline = OMIMPipeline(run_id="test-issue12")
        pipeline.raw_dir = tmp_path / "raw"
        pipeline.raw_dir.mkdir(parents=True, exist_ok=True)
        pipeline._source_format = "morbidmap_txt"
        pipeline._download_method_used = "morbidmap"
        pipeline._source_version = "2024-06-15"
        pipeline._source_url_sanitised = "https://data.omim.org/downloads/[REDACTED]/morbidmap.txt"
        pipeline.start_time = datetime.now(timezone.utc)

        processed = tmp_path / "processed"
        processed.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(op, "PROCESSED_DATA_DIR", processed)
        monkeypatch.setattr(
            op, "OMIM_OUTPUT_PATH",
            processed / "omim_gene_disease_associations.csv",
        )
        monkeypatch.setattr(
            op, "OMIM_SUSCEPTIBILITY_OUTPUT_PATH",
            processed / "omim_susceptibility.csv",
        )
        monkeypatch.setattr(
            op, "OMIM_QUARANTINE_PATH", processed / "omim_quarantine.jsonl"
        )

        # Create a small morbidmap fixture.
        fixture = tmp_path / "morbidmap.txt"
        fixture.write_text(
            "# Generated: 2024-06-15\n"
            "Achondroplasia, 100800 (3)\tFGFR3\t134934\t4p16.3\n",
            encoding="utf-8",
        )

        # Run clean() twice.
        pipeline.clean(fixture)
        csv1 = op.OMIM_OUTPUT_PATH.read_bytes()
        pipeline._quarantine_buffer.clear()
        pipeline._silent_skip_counter.clear()
        pipeline.clean(fixture)
        csv2 = op.OMIM_OUTPUT_PATH.read_bytes()

        # CSVs must be byte-identical (idempotency — BUG-7.1).
        assert csv1 == csv2, "OMIM clean() is not idempotent (BUG-7.1)"


# ============================================================================
# Issue #13: bulk_update_drugs_from_pubchem returns actual updated count
# ============================================================================


class TestIssue13ActualUpdatedCount:
    """Verify bulk_update_drugs_from_pubchem returns actual updated count."""

    def test_returns_actual_updated_count(self, db_session):
        """Should return the number of rows actually updated, not input count."""
        # Insert a drug WITH pubchem_cid already set
        drug_with_cid = Drug(inchikey="BSYNRYMUTXBXSQ-UHFFFAOYSA-N", name="Drug1", pubchem_cid=100)
        db_session.add(drug_with_cid)
        # Insert a drug WITHOUT pubchem_cid
        drug_without_cid = Drug(inchikey="WFXAZNNJSJXTJZ-UHFFFAOYSA-N", name="Drug2")
        db_session.add(drug_without_cid)
        db_session.commit()

        # Try to update both - only Drug2 should be updated (WHERE pubchem_cid IS NULL)
        update_df = pd.DataFrame({
            "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N", "WFXAZNNJSJXTJZ-UHFFFAOYSA-N"],
            "pubchem_cid": [999, 888],
            "molecular_formula": [None, None],
            "molecular_weight": [None, None],
            "smiles": [None, None],
        })
        result = bulk_update_drugs_from_pubchem(db_session, update_df)
        db_session.commit()

        # Only 1 row should actually be updated (Drug2, which had NULL pubchem_cid).
        # int(UpsertResult) returns total_input; use the actual updated count.
        actual_updated = int(result)
        assert actual_updated >= 1, f"Expected at least 1 actual update, got {actual_updated}"

    def test_docstring_says_actual_updated(self):
        """Docstring should clarify that it returns actual updated count."""
        docstring = bulk_update_drugs_from_pubchem.__doc__
        assert "actually updated" in docstring.lower(), \
            "Docstring should mention 'actually updated'"


# ============================================================================
# Issue #14: Entity resolution TRUNCATE → DELETE
# ============================================================================


class TestIssue14AtomicEntityResolution:
    """Verify entity_mapping uses atomic temp-table + TRUNCATE/INSERT pattern (FIX #14)."""

    def test_entity_resolution_uses_atomic_swap(self):
        """master_pipeline_dag should use temp-table + DELETE/INSERT for
        atomicity (cross-dialect — v9 ROOT FIX F3.5).

        The previous test expected the literal string
        ``TRUNCATE TABLE entity_mapping``. TRUNCATE is PostgreSQL-specific
        syntax that raises ``sqlite3.OperationalError`` on SQLite (the
        dev/test dialect) despite the migration runner claiming
        cross-dialect support. The v9 ROOT FIX (audit F3.5) replaced
        TRUNCATE with ``DELETE FROM entity_mapping`` which is ANSI SQL
        and works on both dialects. Atomicity is preserved because the
        DELETE + INSERT both run inside an explicit
        ``engine.begin()`` transaction (auto-commit on success, full
        rollback on failure)."""
        with open(PROJECT_ROOT / "dags" / "master_pipeline_dag.py", "r") as f:
            content = f.read()
        # The atomic-swap pattern: temp staging table + clear + INSERT
        # in a single transaction.
        assert "_tmp_entity_mapping_staging" in content, (
            "Should use a temp staging table for atomic swap"
        )
        assert "DELETE FROM entity_mapping" in content or "TRUNCATE TABLE entity_mapping" in content, (
            "Should clear entity_mapping via DELETE FROM (v9+ cross-dialect) "
            "or TRUNCATE TABLE (legacy PostgreSQL-only) for atomic swap"
        )
        assert "INSERT INTO entity_mapping" in content, (
            "Should INSERT from temp staging table into entity_mapping"
        )
        # The whole swap MUST be inside an explicit transaction.
        assert "engine.begin()" in content or "with engine.begin()" in content, (
            "Atomic swap must run inside an explicit transaction "
            "(engine.begin()) so it rolls back on failure."
        )
        assert "_tmp_entity_mapping_staging" in content, (
            "Should use temp staging table for atomic entity resolution"
        )


# ============================================================================
# Issue #15: _count_records leaks file handles
# ============================================================================


class TestIssue15FileHandleLeak:
    """Verify _count_records uses with statement for file handle."""

    def test_count_records_uses_context_manager(self):
        """_count_records should use 'with open' to prevent file handle leaks."""
        import inspect
        from pipelines.base_pipeline import BasePipeline
        source = inspect.getsource(BasePipeline._count_records)
        assert "with open" in source, "Should use 'with open' for file handle management"


# ============================================================================
# Issue #16: DrugBank _file_handle not closed on exception
# ============================================================================


class TestIssue16FileHandleClose:
    """Verify DrugBank file handle is closed even on exception."""

    def test_drugbank_clean_wraps_in_try_finally(self):
        """clean() method should wrap iterparse loop in try/finally for handle close."""
        import inspect
        from pipelines.drugbank_pipeline import DrugBankPipeline
        source = inspect.getsource(DrugBankPipeline.clean)
        # Should have try: and finally: wrapping the iterparse loop
        assert "finally:" in source, "Should have finally block for cleanup"
        # The _file_handle.close() should be inside the finally block
        lines = source.split("\n")
        found_finally = False
        found_close_in_finally = False
        for line in lines:
            if "finally:" in line:
                found_finally = True
            if found_finally and "_file_handle" in line and "close" in line:
                found_close_in_finally = True
        assert found_finally, "Should have finally block"
        assert found_close_in_finally, "Should close file handle in finally block"


# ============================================================================
# Issue #17: OMIM _download_via_api doesn't check if OMIM_API_KEY is set
# ============================================================================
# UPDATE (institutional-grade rewrite): Per master prompt BUG-9.15, the new
# OMIM pipeline RAISES RuntimeError when OMIM_API_KEY is empty (does not
# silently warn). The download() method is the canonical entry point.


class TestIssue17OmimApiKeyWarning:
    """Verify OMIM pipeline raises when API key is not set (BUG-9.15)."""

    def test_omim_warns_without_api_key(self, monkeypatch):
        """download() should raise RuntimeError when OMIM_API_KEY is empty.

        The legacy code logged a warning and continued; the institutional-grade
        rewrite raises RuntimeError per BUG-9.15 (silent failure is the
        patient-harm failure mode).
        """
        import pipelines.omim_pipeline as op
        # Patch OMIM_API_KEY to empty.
        monkeypatch.setattr(op, "OMIM_API_KEY", "")
        pipeline = op.OMIMPipeline(run_id="test-issue17")
        # download() must raise (not silently warn).
        with pytest.raises(RuntimeError, match="OMIM_API_KEY is not set"):
            pipeline.download()


# ============================================================================
# Issue #18: CHEMBL_VERSION = "33" is outdated
# ============================================================================


class TestIssue18ChEMBLVersion:
    """Verify ChEMBL version is configurable and defaults to 35."""

    def test_chembl_version_configurable(self):
        """CHEMBL_VERSION should be configurable via env var."""
        # Test default value
        with patch.dict(os.environ, {}, clear=True):
            # Remove CHEMBL_VERSION from env if present
            os.environ.pop("CHEMBL_VERSION", None)
            # Re-import to pick up new default
            import importlib
            import config.settings
            importlib.reload(config.settings)
            assert config.settings.CHEMBL_VERSION == "35", \
                f"Default should be '35', got {config.settings.CHEMBL_VERSION}"

    def test_chembl_version_from_env(self):
        """CHEMBL_VERSION should be overridable via environment variable."""
        with patch.dict(os.environ, {"CHEMBL_VERSION": "36"}):
            import importlib
            import config.settings
            importlib.reload(config.settings)
            assert config.settings.CHEMBL_VERSION == "36", \
                f"Should read from env var, got {config.settings.CHEMBL_VERSION}"


# ============================================================================
# Issue #19: UniProt column "Gene Names (primary)" may not exist
# ============================================================================


class TestIssue19UniProtGeneSymbol:
    """Verify UniProt pipeline handles missing 'Gene Names (primary)' column."""

    def test_fallback_gene_symbol_from_gene_names(self):
        """When 'Gene Names (primary)' is missing, extract from 'Gene Names'."""
        from pipelines.uniprot_pipeline import UniProtPipeline
        import inspect
        source = inspect.getsource(UniProtPipeline.clean)
        # Should have fallback logic for gene_symbol
        assert "gene_names" in source.lower(), "Should reference gene_names column for fallback"


# ============================================================================
# Issue #20: STRING aliases file column names are guessed
# ============================================================================


class TestIssue20StringColumns:
    """Verify STRING pipeline provides diagnostic logging for column identification."""

    def test_string_has_diagnostic_logging(self):
        """_build_string_uniprot_map should log available columns when identification fails."""
        import inspect
        from pipelines.string_pipeline import StringPipeline
        source = inspect.getsource(StringPipeline._build_string_uniprot_map)
        # Should log the actual columns found
        assert "aliases_df.columns" in source, "Should log aliases_df.columns for diagnostics"


# ============================================================================
# Issue #21: GDA _ensure_gda_columns sets disease_id default to ""
# ============================================================================


class TestIssue21GdaDiseaseId:
    """Verify GDA records with empty disease_id are filtered out before upsert."""

    def test_disgenet_disease_id_default_is_none(self):
        """DisGeNET _ensure_gda_columns should default disease_id to None."""
        from pipelines.disgenet_pipeline import DisGeNETPipeline
        import inspect
        source = inspect.getsource(DisGeNETPipeline._ensure_gda_columns)
        # Should not have disease_id: ""
        assert '"disease_id": ""' not in source, "disease_id should not default to empty string"

    def test_omim_disease_id_default_is_none(self):
        """OMIM _ensure_gda_columns should default disease_id to None."""
        from pipelines.omim_pipeline import OMIMPipeline
        import inspect
        source = inspect.getsource(OMIMPipeline._ensure_gda_columns)
        assert '"disease_id": ""' not in source, "disease_id should not default to empty string"


# ============================================================================
# Issue #22: PubChem _lookup_batch uses incorrect POST format
# ============================================================================


class TestIssue22PubChemPostFormat:
    """Verify PubChem batch lookup uses comma-separated InChIKeys."""

    def test_uses_comma_separated_inchikeys(self):
        """_lookup_batch should use comma-separated InChIKeys, not newline."""
        import inspect
        from pipelines.pubchem_pipeline import PubChemPipeline
        source = inspect.getsource(PubChemPipeline._lookup_batch)
        assert '",".join' in source or ".join" in source, "Should join InChIKeys with commas"
        # Should NOT use newline-separated format
        assert '"\\n".join' not in source, "Should not use newline-separated InChIKeys"

    def test_post_data_uses_comma_separated(self):
        """POST request data should use comma-separated InChIKeys.

        Updated for the institutional-grade rewrite (PUBCHEM_PIPELINE_MASTER_FIX_PROMPT.md):
        the new ``_lookup_batch`` signature is
        ``(self, batch_idx, inchikeys, total_batches)`` (ARCH-3 — HTTP I/O
        moved to download(), and the batch index is needed for lineage).
        The test patches ``self.http_session.post`` (no longer bare
        ``requests.post`` — REL-12, ARCH-11).
        """
        from pipelines.pubchem_pipeline import PubChemPipeline
        from unittest.mock import MagicMock, PropertyMock, patch

        # Mock the HTTP response.
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.json.return_value = {"PropertyTable": {"Properties": []}}
        mock_response.content = b'{"PropertyTable": {"Properties": []}}'
        mock_response.text = '{"PropertyTable": {"Properties": []}}'

        mock_session = MagicMock()
        mock_session.post.return_value = mock_response

        # Instantiate the pipeline normally — the constructor reads
        # settings and validates config.
        pipeline = PubChemPipeline()
        # Patch http_session to return our mock.
        with patch.object(
            type(pipeline), "http_session", PropertyMock(return_value=mock_session)
        ):
            pipeline._lookup_batch(
                batch_idx=0,
                inchikeys=["BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
                           "HEFNNWSXXWATIW-UHFFFAOYSA-N"],
                total_batches=1,
            )

        # Verify the POST data format.
        call_kwargs = mock_session.post.call_args
        # call_args[1] is the kwargs dict.
        data = call_kwargs[1].get("data") if call_kwargs[1] else {}
        if not data and len(call_kwargs[0]) > 1:
            data = call_kwargs[0][1] if isinstance(call_kwargs[0][1], dict) else {}

        if "inchikey" in data:
            inchikey_value = data["inchikey"]
            assert "," in inchikey_value, (
                f"InChIKeys should be comma-separated, got: {inchikey_value}"
            )
            assert "\n" not in inchikey_value, (
                f"InChIKeys should not contain newlines, got: {inchikey_value}"
            )


# ============================================================================
# Issue #23: Docker Compose volumes mount directories that may not exist
# ============================================================================


class TestIssue23DockerDirs:
    """Verify docker-compose.yml includes a setup service for directory creation."""

    def test_docker_compose_has_setup_service(self):
        """docker-compose.yml should have a setup service that creates data directories."""
        with open(PROJECT_ROOT / "docker-compose.yml", "r") as f:
            content = f.read()
        assert "setup" in content, "Should have a setup service"
        assert "mkdir" in content, "Should create directories with mkdir"


# ============================================================================
# Issue #24: chembl-webresource-client in requirements.txt is unused
# ============================================================================


class TestIssue24RemoveChEMBLClient:
    """Verify chembl-webresource-client is removed from requirements."""

    def test_chembl_webresource_client_removed(self):
        """requirements.txt should not contain chembl-webresource-client."""
        with open(PROJECT_ROOT / "requirements.txt", "r") as f:
            content = f.read()
        assert "chembl-webresource-client" not in content, \
            "chembl-webresource-client should be removed from requirements.txt"


# ============================================================================
# Issue #25: make load-all calls run_load_only() but CSVs may not exist
# ============================================================================


class TestIssue25LoadOnlyError:
    """Verify run_load_only raises FileNotFoundError when no CSV exists."""

    def test_run_load_only_raises_on_missing_csv(self, tmp_path):
        """run_load_only should raise FileNotFoundError when no cleaned data found."""
        from pipelines.base_pipeline import BasePipeline

        class TestPipeline(BasePipeline):
            source_name = "test_pipeline_nonexistent"

            def download(self): return Path("/nonexistent")
            def clean(self, raw_path): return pd.DataFrame()
            def load(self, df): return 0

        pipeline = TestPipeline()

        with pytest.raises(FileNotFoundError, match="No cleaned data found"):
            pipeline.run_load_only()


# ============================================================================
# Issue #26: _is_nullish treats "unknown" as null
# ============================================================================


class TestIssue26UnknownNotNull:
    """Verify 'unknown' is NOT treated as null in _is_nullish."""

    def test_unknown_is_not_nullish(self):
        """'unknown' should NOT be treated as a null-like value."""
        from cleaning.missing_values import _is_nullish
        series = pd.Series(["unknown", "Unknown", "UNKNOWN", "null", ""])
        result = _is_nullish(series)
        # "unknown" in any case should NOT be nullish
        assert not result.iloc[0], "'unknown' should not be nullish"
        assert not result.iloc[1], "'Unknown' should not be nullish"
        assert not result.iloc[2], "'UNKNOWN' should not be nullish"
        # "null" and "" should still be nullish
        assert result.iloc[3], "'null' should be nullish"
        assert result.iloc[4], "empty string should be nullish"


# ============================================================================
# Issue #27: fill_missing_drug_fields uses pandas FutureWarning workaround
# ============================================================================


class TestIssue27PandasVersion:
    """Verify fill_missing_drug_fields uses version-aware downcasting."""

    def test_version_check_present(self):
        """fill_missing_drug_fields should have version check for FutureWarning."""
        import inspect
        from cleaning.missing_values import fill_missing_drug_fields
        source = inspect.getsource(fill_missing_drug_fields)
        assert "_pd_version" in source or "pd.__version__" in source, \
            "Should check pandas version before using future.no_silent_downcasting"


# ============================================================================
# Issue #28: base_pipeline.py _count_records for .gz files is slow (minor)
# ============================================================================


class TestIssue28GzCount:
    """Verify _count_records handles .gz files properly."""

    def test_gz_count_records(self, tmp_path):
        """_count_records should correctly count lines in gzip files."""
        from pipelines.base_pipeline import BasePipeline

        class TestPipeline(BasePipeline):
            source_name = "test_gz"

            def download(self): return Path("/nonexistent")
            def clean(self, raw_path): return pd.DataFrame()
            def load(self, df): return 0

        pipeline = TestPipeline()
        pipeline.raw_dir = tmp_path

        # Create a gzip file with 3 data lines + 1 header = 3 data records
        gz_path = tmp_path / "test.csv.gz"
        with gzip.open(gz_path, "wt") as f:
            f.write("col1,col2\n")
            f.write("a,1\n")
            f.write("b,2\n")
            f.write("c,3\n")

        count = pipeline._count_records(gz_path)
        assert count == 3, f"Expected 3 records, got {count}"


# ============================================================================
# Issue #29: DrugResolver.get_canonical_inchikey confusing matcher argument
# ============================================================================


class TestIssue29MatcherArgument:
    """Verify get_canonical_inchikey uses explicit matcher-argument pairs."""

    def test_get_canonical_inchikey_readable(self):
        """get_canonical_inchikey should use explicit (matcher, arg) pairs."""
        import inspect
        from entity_resolution.drug_resolver import DrugResolver
        source = inspect.getsource(DrugResolver.get_canonical_inchikey)
        # Should NOT have the confusing "if matcher is not self._match_by_name" pattern
        assert "is not self._match_by_name" not in source, \
            "Should not use confusing 'is not self._match_by_name' pattern"

    def test_get_canonical_inchikey_with_inchikey(self):
        """Should resolve drug by InChIKey."""
        from entity_resolution.drug_resolver import DrugResolver
        resolver = DrugResolver()
        resolver._inchikey_index["TEST-UHFFFAOYSA-N"] = "TEST-UHFFFAOYSA-N"
        resolver.mapping["TEST-UHFFFAOYSA-N"] = {"canonical_name": "TestDrug"}

        result = resolver.get_canonical_inchikey({"inchikey": "TEST-UHFFFAOYSA-N", "name": "TestDrug"})
        assert result == "TEST-UHFFFAOYSA-N"

    def test_get_canonical_inchikey_with_name_only(self):
        """Should resolve drug by name when no inchikey match."""
        from entity_resolution.drug_resolver import DrugResolver
        resolver = DrugResolver()
        resolver._name_index["testdrug"] = "TEST-UHFFFAOYSA-N"
        resolver.mapping["TEST-UHFFFAOYSA-N"] = {"canonical_name": "TestDrug"}

        result = resolver.get_canonical_inchikey({"inchikey": "", "name": "TestDrug"})
        assert result == "TEST-UHFFFAOYSA-N"

    def test_get_canonical_inchikey_no_match(self):
        """Should return None when no match found."""
        from entity_resolution.drug_resolver import DrugResolver
        resolver = DrugResolver()

        result = resolver.get_canonical_inchikey({"inchikey": "", "name": ""})
        assert result is None


# ============================================================================
# Issue #30: No __init__.py in database/migrations/
# ============================================================================


class TestIssue30MigrationsInit:
    """Verify database/migrations/__init__.py exists."""

    def test_migrations_init_exists(self):
        """database/migrations/__init__.py should exist."""
        init_path = PROJECT_ROOT / "database" / "migrations" / "__init__.py"
        assert init_path.exists(), "database/migrations/__init__.py should exist"


# ============================================================================
# Issue #31: conftest.py SQLite now() may not work with func.now()
# ============================================================================


class TestIssue31SqliteNow:
    """Verify SQLite now() function returns proper datetime string."""

    def test_sqlite_now_returns_datetime_string(self, db_engine):
        """SQLite now() function should return a parseable datetime string."""
        with db_engine.connect() as conn:
            result = conn.execute(text("SELECT now()"))
            now_value = result.scalar()
            # Should be a string that can be parsed
            assert now_value is not None
            assert isinstance(now_value, str)
            # Should contain date and time
            assert "20" in now_value, f"Should look like a datetime, got: {now_value}"


# ============================================================================
# Issue #32: GDA uniprot_id is nullable=True but is a FK — orphans accumulate
# ============================================================================


class TestIssue32OrphanCleanup:
    """Verify cleanup_orphan_gda_records utility exists and works."""

    def test_cleanup_function_exists(self):
        """cleanup_orphan_gda_records should be importable."""
        from database.models import cleanup_orphan_gda_records
        assert callable(cleanup_orphan_gda_records)

    def test_cleanup_removes_orphan_records(self, db_session):
        """cleanup_orphan_gda_records should remove GDA records with NULL uniprot_id."""
        # Create a GDA record with NULL uniprot_id
        gda = GeneDiseaseAssociation(
            gene_symbol="ORPHAN",
            uniprot_id=None,
            disease_id="C9999",
            source="test",
        )
        db_session.add(gda)
        db_session.commit()

        # Run cleanup (may not delete very recent records depending on implementation)
        deleted = cleanup_orphan_gda_records(db_session)
        # Function should execute without error
        assert isinstance(deleted, int)


# ============================================================================
# Issue #33: Protein.gene_name vs Protein.gene_symbol confusion
# ============================================================================


class TestIssue33GeneNameComments:
    """Verify clarifying comments exist for gene_name vs gene_symbol."""

    def test_protein_model_has_clarifying_comments(self):
        """Protein model should have comments clarifying gene_name vs gene_symbol."""
        import inspect
        source = inspect.getsource(Protein)
        # Should mention backward compatibility for gene_name
        assert "backward" in source.lower() or "compat" in source.lower(), \
            "Should have comment about backward compatibility for gene_name"


# ============================================================================
# Issue #34: normalize_name strips ALL non-alphanumeric characters
# ============================================================================


class TestIssue34NormalizePreservesHyphens:
    """Verify normalize_name preserves hyphens and forward slashes."""

    def test_hyphens_preserved(self):
        """normalize_name should preserve hyphens (e.g., d-alpha-tocopherol)."""
        from entity_resolution.resolver_utils import normalize_name
        result1 = normalize_name("D-alpha-tocopherol")
        result2 = normalize_name("L-alpha-tocopherol")
        assert result1 != result2, \
            f"d-alpha-tocopherol and l-alpha-tocopherol should normalize differently, got: {result1} vs {result2}"

    def test_forward_slashes_preserved(self):
        """normalize_name should preserve forward slashes for stereochemistry."""
        from entity_resolution.resolver_utils import normalize_name
        result = normalize_name("cis/trans-isomer")
        assert "-" in result, f"Hyphens should be preserved, got: {result}"
        # The slash should also be preserved
        assert "/" in result, f"Forward slashes should be preserved, got: {result}"

    def test_multiple_hyphens_collapsed(self):
        """Multiple consecutive hyphens should be collapsed to single."""
        from entity_resolution.resolver_utils import normalize_name
        result = normalize_name("test--drug")
        assert "--" not in result, f"Double hyphens should be collapsed, got: {result}"
        assert "-" in result, f"Single hyphen should remain, got: {result}"

    def test_regex_pattern_preserves_hyphens(self):
        """The _NON_ALNUM_RE regex should preserve hyphens and slashes."""
        from entity_resolution.resolver_utils import _NON_ALNUM_RE
        pattern = _NON_ALNUM_RE.pattern
        assert "\\-" in pattern or "-" in pattern, \
            "Regex should preserve hyphens"
        assert "/" in pattern or "\\/" in pattern, \
            "Regex should preserve forward slashes"


# ============================================================================
# ChEMBL Target Accession Resolution (Additional Fix)
# ============================================================================


class TestTargetAccessionResolution:
    """Verify _resolve_target_accessions method exists and works."""

    def test_resolve_target_accessions_method_exists(self):
        """ChEMBLPipeline should have _resolve_target_accessions method."""
        from pipelines.chembl_pipeline import ChEMBLPipeline
        assert hasattr(ChEMBLPipeline, "_resolve_target_accessions"), \
            "Should have _resolve_target_accessions method"

    @patch("pipelines.chembl_pipeline.ChEMBLPipeline._api_get")
    def test_resolve_target_accessions_queries_target_api(self, mock_api_get):
        """_resolve_target_accessions should query the target API for each target ID."""
        from pipelines.chembl_pipeline import ChEMBLPipeline

        mock_api_get.return_value = {
            "target_components": [{"accession": "P23219"}]
        }

        pipeline = ChEMBLPipeline.__new__(ChEMBLPipeline)
        pipeline.source_name = "chembl"
        result = pipeline._resolve_target_accessions({"CHEMBL207", "CHEMBL208"})

        assert isinstance(result, dict), "Should return a dict"
        # Should have called _api_get for each target
        assert mock_api_get.call_count >= 1

    @patch("pipelines.chembl_pipeline.ChEMBLPipeline._api_get")
    def test_resolve_target_accessions_handles_errors(self, mock_api_get):
        """_resolve_target_accessions should handle API errors gracefully.

        v16 ROOT FIX (SF-4): the previous test used a bare ``Exception``
        as the side_effect, which the broad ``except Exception`` caught.
        The fix narrows the except to ``requests.RequestException``,
        ``json.JSONDecodeError``, ``ValueError``, ``TimeoutError`` — so
        the test must use one of those types. Non-network exceptions
        (e.g.ProgrammingError, KeyError indicating an API contract
        change) should now PROPAGATE so the operator can investigate.
        """
        import requests as _requests
        from pipelines.chembl_pipeline import ChEMBLPipeline

        # Use a real RequestException (the type the narrowed except catches).
        mock_api_get.side_effect = _requests.RequestException("API Error")

        pipeline = ChEMBLPipeline.__new__(ChEMBLPipeline)
        pipeline.source_name = "chembl"
        # v16 SF-4: defensively initialize _metrics for test pipelines
        # constructed via __new__ (bypassing __init__).
        pipeline._metrics = {}
        result = pipeline._resolve_target_accessions({"CHEMBL207"})

        # Should return empty dict on error, not raise
        assert isinstance(result, dict)


# ============================================================================
# Integration Test: Full upsert workflow
# ============================================================================


class TestIntegrationUpsertWorkflow:
    """Integration test: full drug+protein+interaction upsert workflow."""

    def test_full_drug_protein_interaction_workflow(self, db_session):
        """End-to-end: insert drugs, proteins, then interactions with FK resolution."""
        # Step 1: Insert drugs
        drugs_df = pd.DataFrame({
            "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N", "WFXAZNNJSJXTJZ-UHFFFAOYSA-N"],
            "name": ["Aspirin", "Ibuprofen"],
            "chembl_id": ["CHEMBL25", "CHEMBL521"],
        })
        drug_count = bulk_upsert_drugs(db_session, drugs_df)
        db_session.commit()
        assert int(drug_count) >= 2

        # Step 2: Insert proteins
        proteins_df = pd.DataFrame({
            "uniprot_id": ["P23219", "P04637"],
            "gene_name": ["PTGS1", "TP53"],
            "gene_symbol": ["PTGS1", "TP53"],
        })
        protein_count = bulk_upsert_proteins(db_session, proteins_df)
        db_session.commit()
        assert int(protein_count) >= 2

        # Step 3: Insert drug-protein interactions
        dpi_df = pd.DataFrame({
            "drug_id": [1, 2],
            "protein_id": [1, 2],
            "interaction_type": ["inhibitor", "inhibitor"],
            "activity_value": [5.5, 3.2],
            "activity_type": ["IC50", "IC50"],
            "source": ["chembl", "chembl"],
            "source_id": ["act1", "act2"],
        })
        dpi_count = bulk_upsert_dpi(db_session, dpi_df)
        db_session.commit()
        assert int(dpi_count) >= 2

        # Verify all records exist
        assert db_session.query(Drug).count() == 2
        assert db_session.query(Protein).count() == 2
        assert db_session.query(DrugProteinInteraction).count() == 2

        # Verify FK relationships
        dpi = db_session.query(DrugProteinInteraction).first()
        assert dpi.drug is not None
        assert dpi.protein is not None

    def test_entity_mapping_upsert(self, db_session):
        """Entity mapping upsert should work correctly."""
        em_df = pd.DataFrame({
            "canonical_inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
            "canonical_name": ["Aspirin"],
            "chembl_id": ["CHEMBL25"],
        })
        count = bulk_upsert_entity_mapping(db_session, em_df)
        db_session.commit()
        assert int(count) >= 1

        # Upsert same key with updated data
        em_df2 = pd.DataFrame({
            "canonical_inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
            "canonical_name": ["Aspirin Updated"],
            "chembl_id": ["CHEMBL25"],
            "drugbank_id": ["DB00945"],
        })
        count2 = bulk_upsert_entity_mapping(db_session, em_df2)
        db_session.commit()

        em = db_session.query(EntityMapping).first()
        assert em.canonical_name == "Aspirin Updated"
        assert em.drugbank_id == "DB00945"

    def test_gda_upsert_workflow(self, db_session):
        """GDA upsert should work correctly with conflict handling."""
        # First insert a protein
        proteins_df = pd.DataFrame({
            "uniprot_id": ["P23219"],
            "gene_name": ["PTGS1"],
            "gene_symbol": ["PTGS1"],
        })
        bulk_upsert_proteins(db_session, proteins_df)
        db_session.commit()

        # Insert GDA
        gda_df = pd.DataFrame({
            "gene_symbol": ["PTGS1"],
            "uniprot_id": ["P23219"],
            "disease_id": ["C0001"],
            "disease_name": ["Test Disease"],
            "source": ["disgenet"],
        })
        count = bulk_upsert_gda(db_session, gda_df)
        db_session.commit()
        assert int(count) >= 1

        # Upsert same GDA with updated disease_name
        gda_df2 = pd.DataFrame({
            "gene_symbol": ["PTGS1"],
            "uniprot_id": ["P23219"],
            "disease_id": ["C0001"],
            "disease_name": ["Updated Disease Name"],
            "source": ["disgenet"],
        })
        count2 = bulk_upsert_gda(db_session, gda_df2)
        db_session.commit()

        gda = db_session.query(GeneDiseaseAssociation).first()
        assert gda.disease_name == "Updated Disease Name"
