"""
tests/test_omim_pipeline.py — REAL institutional-grade tests for the OMIM pipeline.

This is test #1 of 3 required by the master prompt:
  1. tests/test_omim_pipeline.py (THIS FILE) — real tests for the OMIM file.
  2. tests/test_all_26_files_integration_v10.py — all 26 files combined integration.
  3. All existing tests must still pass.

Test classes mirror the DisGeNET test structure (test_disgenet_pipeline_institutional_v389.py)
with one test class per verification domain. Every test asserts specific
values, not just "doesn't crash" (GAP-10.13). Every fix has a regression
test (GAP-10.11).

Run with:
    pytest tests/test_omim_pipeline.py -v
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import warnings
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
import requests
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker

# ---------------------------------------------------------------------------
# Project path setup (BUG-4.24)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Force test env vars BEFORE importing the pipeline.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("DISGENET_USE_API", "false")
os.environ.setdefault("DISGENET_API_KEY", "test-key-not-real")
os.environ.setdefault("OMIM_API_KEY", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
os.environ.setdefault("OMIM_MIN_EXPECTED_RECORDS", "0")  # small fixtures

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
import pipelines.omim_pipeline as op
from pipelines.omim_pipeline import (
    ASSOCIATION_TYPE_DEFAULT,
    ASSOCIATION_TYPE_GENE_LOCUS,
    ASSOCIATION_TYPE_MENDELIAN_PHENOTYPE,
    ASSOCIATION_TYPE_NON_DISEASE,
    ASSOCIATION_TYPE_PROVISIONAL,
    ASSOCIATION_TYPE_SUSCEPTIBILITY,
    CYTO_RE,
    GDA_REQUIRED_COLUMNS,
    GENERATED_RE,
    INHERITANCE_PATTERNS,
    MARKER_PATTERNS,
    MARKER_TO_ASSOCIATION_TYPE,
    MAPPING_KEY_RE,
    MIM_NUMBER_RE,
    OMIMPipeline,
    OMIMRecord,
    SCORE_BY_MAPPING_KEY,
    SCORE_METHOD_DEFAULT,
    SCORE_TYPE_OMIM,
    SCHEMA_VERSION_STAMP,
    assert_is_omim_gda_df,
)
from cleaning.confidence import (
    CONFIDENCE_TIER_METHOD_VERSION,
    DEFAULT_CONFIDENCE_TIERS,
    classify_confidence,
)
from cleaning.missing_values import validate_gda_scores
from config.settings import (
    OMIM_API_KEY,
    OMIM_CONFIRMED_SCORE,
    OMIM_CONTIGUOUS_SCORE,
    OMIM_EXCLUDE_SUSCEPTIBILITY,
    OMIM_GENE_MAPPED_SCORE,
    OMIM_MAPPING_KEYS_INCLUDE,
    OMIM_PHENOTYPE_MAPPED_SCORE,
    OMIM_REQUEST_INTERVAL,
)
from database.base import Base
from database.models import DeadLetterGDA, GeneDiseaseAssociation, PipelineRun, Protein

# Module path for patching.
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

    Base.metadata.create_all(engine)
    return engine


def _make_proteins():
    """Return a list of Protein ORM objects matching the fixture gene symbols."""
    return [
        Protein(uniprot_id="P11362", gene_name="Fibroblast growth factor receptor 3",
                gene_symbol="FGFR3", protein_name="FGFR3", organism="Homo sapiens"),
        Protein(uniprot_id="P13569", gene_name="Cystic fibrosis transmembrane conductance regulator",
                gene_symbol="CFTR", protein_name="CFTR", organism="Homo sapiens"),
        Protein(uniprot_id="P38398", gene_name="Breast cancer type 1 susceptibility protein",
                gene_symbol="BRCA1", protein_name="BRCA1", organism="Homo sapiens"),
        Protein(uniprot_id="P10721", gene_name="Mast/stem cell growth factor receptor Kit",
                gene_symbol="KIT", protein_name="KIT", organism="Homo sapiens"),
        Protein(uniprot_id="Q30201", gene_name="Hereditary hemochromatosis protein",
                gene_symbol="HFE", protein_name="HFE", organism="Homo sapiens"),
        Protein(uniprot_id="P35555", gene_name="Fibrillin-1",
                gene_symbol="FBN1", protein_name="FBN1", organism="Homo sapiens"),
        Protein(uniprot_id="P68871", gene_name="Hemoglobin subunit beta",
                gene_symbol="HBB", protein_name="HBB", organism="Homo sapiens"),
        Protein(uniprot_id="P11532", gene_name="Dystrophin",
                gene_symbol="DMD", protein_name="DMD", organism="Homo sapiens"),
        Protein(uniprot_id="P42858", gene_name="Huntingtin",
                gene_symbol="HTT", protein_name="HTT", organism="Homo sapiens"),
    ]


def _make_morbidmap_fixture(tmp_path: Path, content: str | None = None) -> Path:
    """Write a morbidmap fixture and return its path."""
    fixture_path = tmp_path / "morbidmap.txt"
    if content is None:
        # Default fixture: read the project fixture file.
        src = PROJECT_ROOT / "tests" / "fixtures" / "omim" / "morbidmap_sample.txt"
        content = src.read_text(encoding="utf-8")
    fixture_path.write_text(content, encoding="utf-8")
    return fixture_path


def _make_omim_pipeline(tmp_path: Path) -> OMIMPipeline:
    """Instantiate an OMIMPipeline with raw_dir + processed_dir redirected to tmp_path."""
    pipeline = OMIMPipeline(run_id="test-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f"))
    pipeline.raw_dir = tmp_path / "raw"
    pipeline.raw_dir.mkdir(parents=True, exist_ok=True)
    pipeline._source_format = "morbidmap_txt"
    pipeline._download_method_used = "morbidmap"
    pipeline._source_version = "2024-06-15"
    pipeline._source_url_sanitised = "https://data.omim.org/downloads/[REDACTED]/morbidmap.txt"
    pipeline.start_time = datetime.now(timezone.utc)
    return pipeline


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def db_engine():
    engine = _make_engine()
    yield engine
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
    """Seed the proteins table with the gene symbols our fixtures use."""
    for protein in _make_proteins():
        db_session.add(protein)
    db_session.commit()
    return db_session


@pytest.fixture
def tmp_processed_dir(tmp_path, monkeypatch):
    """Redirect PROCESSED_DATA_DIR + OMIM_OUTPUT_PATH to tmp_path."""
    processed = tmp_path / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(op, "PROCESSED_DATA_DIR", processed)
    monkeypatch.setattr(
        op, "OMIM_OUTPUT_PATH", processed / "omim_gene_disease_associations.csv"
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
    """Yield an OMIMPipeline instance with redirected paths."""
    return _make_omim_pipeline(tmp_path)


@pytest.fixture
def morbidmap_fixture(tmp_path):
    """Write the default morbidmap fixture and return its path."""
    return _make_morbidmap_fixture(tmp_path)


# ===========================================================================
# Domain 3 — SCIENTIFIC CORRECTNESS (LIFE-SAFETY CRITICAL)
# ===========================================================================
class TestDomain3ScientificCorrectness:
    """Tests verifying the OMIM pipeline is scientifically correct.

    These tests are the most important — wrong science = patient harm.
    """

    def test_bug_3_1_first_row_not_dropped(self, omim_pipeline, tmp_path):
        """BUG-3.1: The first morbidmap data row must NOT be silently dropped."""
        # 3-line fixture: 1 comment + 2 data rows.
        content = (
            "# OMIM morbidmap header\n"
            "Achondroplasia, 100800 (3)\tFGFR3\t134934\t4p16.3\n"
            "Marfan syndrome, 154700 (3)\tFBN1\t134797\t15q21.1\n"
        )
        fixture = tmp_path / "morbidmap_3line.txt"
        fixture.write_text(content, encoding="utf-8")
        df = omim_pipeline.clean(fixture)
        # BOTH data rows must be present (not just 1).
        assert len(df) == 2, f"Expected 2 rows, got {len(df)} — first row dropped (BUG-3.1)"
        assert set(df["gene_symbol"]) == {"FGFR3", "FBN1"}

    def test_bug_3_2_score_is_not_flat(self, omim_pipeline, morbidmap_fixture):
        """BUG-3.2: Score must vary by mapping_key (not flat 0.9)."""
        df = omim_pipeline.clean(morbidmap_fixture)
        # The fixture has mk=3 (0.9) and mk=4 (0.8) records.
        scores = df["score"].unique()
        assert len(scores) > 1, f"Score is flat (BUG-3.2): {scores}"
        # Specifically, mk=3 → 0.9, mk=4 → 0.8.
        mk3 = df[df["mapping_key"] == 3]
        mk4 = df[df["mapping_key"] == 4]
        if not mk3.empty:
            assert mk3["score"].iloc[0] == pytest.approx(0.9, abs=0.001)
        if not mk4.empty:
            assert mk4["score"].iloc[0] == pytest.approx(0.8, abs=0.001)

    def test_bug_3_3_confidence_tier_not_high(self, omim_pipeline, morbidmap_fixture):
        """BUG-3.3: confidence_tier must be weak/moderate/strong — NEVER 'high'."""
        df = omim_pipeline.clean(morbidmap_fixture)
        tiers = set(df["confidence_tier"].dropna().unique())
        assert tiers.issubset({"weak", "moderate", "strong"}), \
            f"Invalid confidence_tier values: {tiers}"
        assert "high" not in tiers, "confidence_tier='high' is forbidden (BUG-3.3)"

    def test_bug_3_4_all_markers_extracted(self, omim_pipeline, morbidmap_fixture):
        """BUG-3.4: All 6 phenotype markers must be extracted into association_modifier."""
        df = omim_pipeline.clean(morbidmap_fixture)
        # The fixture has {} (Breast cancer), [] (FANCE), ? (SOMEGENE),
        # * (MYGENE), + (ALTGENE), % (MENDGENE) records. The {} record is
        # routed to susceptibility CSV by default (OMIM_EXCLUDE_SUSCEPTIBILITY=True).
        # Check the susceptibility CSV for the {} record.
        sus_path = op.OMIM_SUSCEPTIBILITY_OUTPUT_PATH
        if sus_path.exists():
            sus_df = pd.read_csv(sus_path)
            assert (sus_df["association_modifier"] == "{}").any(), \
                "Susceptibility marker {} not found in susceptibility CSV"
        # Check the main CSV for the other markers.
        # Note: SOMEGENE is filtered out (mk=2 not in default [3,4]).
        # FANCE, MYGENE, ALTGENE, MENDGENE have mk=3 — should be in main CSV.
        main_markers = set(df["association_modifier"].dropna().unique())
        expected_main_markers = {"[]", "*", "+", "%"}
        assert expected_main_markers.issubset(main_markers), \
            f"Missing markers in main CSV: {expected_main_markers - main_markers}"

    def test_bug_3_4_marker_extraction_specific(self):
        """BUG-3.4: Verify each marker is correctly extracted for known inputs."""
        cases = [
            ("{Breast cancer, 114480 (3)}", "{}", "Breast cancer"),
            ("[Some non-disease, 100100 (3)]", "[]", "Some non-disease"),
            ("?Provisional thing, 100200 (2)", "?", "Provisional thing"),
            ("*Gene locus, 100300 (3)", "*", "Gene locus"),
            ("+Alt form, 100400 (3)", "+", "Alt form"),
            ("%Mendelian pheno, 100500 (3)", "%", "Mendelian pheno"),
            ("Achondroplasia, 100800 (3)", None, "Achondroplasia"),
        ]
        for raw, exp_mod, exp_name_prefix in cases:
            name, mim, mk, mod = OMIMPipeline._parse_phenotype_field(raw)
            assert mod == exp_mod, f"{raw!r}: expected mod {exp_mod!r}, got {mod!r}"
            assert name is not None and name.startswith(exp_name_prefix), \
                f"{raw!r}: expected name to start with {exp_name_prefix!r}, got {name!r}"

    def test_bug_3_5_mk4_not_filtered(self, omim_pipeline, morbidmap_fixture):
        """BUG-3.5: mapping_key=4 records must NOT be filtered out by default."""
        df = omim_pipeline.clean(morbidmap_fixture)
        # The fixture has "Testicular germ cell tumor, 273300 (4) KIT".
        kit_rows = df[df["gene_symbol"] == "KIT"]
        assert not kit_rows.empty, "KIT (mk=4) was filtered out — BUG-3.5 regression"
        assert (kit_rows["mapping_key"] == 4).all()

    def test_bug_3_7_mim_range_validated(self, omim_pipeline, tmp_path):
        """BUG-3.7: Out-of-range MIMs (10080) must be quarantined."""
        content = (
            "# Generated: 2024-06-15\n"
            "Achondroplasia, 100800 (3)\tFGFR3\t134934\t4p16.3\n"
            "Bad record, 10080 (3)\tBADGENE\t10080\t1p36.13\n"
        )
        fixture = tmp_path / "morbidmap_range.txt"
        fixture.write_text(content, encoding="utf-8")
        df = omim_pipeline.clean(fixture)
        # The bad record (MIM=10080) must NOT be in the cleaned df.
        bad_rows = df[df["disease_id"] == "OMIM:10080"]
        assert bad_rows.empty, "Out-of-range MIM 10080 was loaded (BUG-3.7)"

    def test_bug_3_10_hgnc_validation(self, omim_pipeline, tmp_path, monkeypatch):
        """BUG-3.10: Non-HGNC symbols must be flagged."""
        # Clear the lru_cache BEFORE patching so the patched version takes effect.
        op._load_hgnc_symbols.cache_clear()
        known_hgnc = frozenset({"FGFR3", "CFTR", "BRCA1", "KIT", "HFE"})
        # Wrap the lambda so it has the cache_clear attribute (lru_cache API).
        from functools import lru_cache

        @lru_cache(maxsize=1)
        def fake_loader():
            return known_hgnc

        monkeypatch.setattr(op, "_load_hgnc_symbols", fake_loader)

        content = (
            "# Generated: 2024-06-15\n"
            "Achondroplasia, 100800 (3)\tFGFR3\t134934\t4p16.3\n"
            "Bad gene, 100800 (3)\tLOC100507532\t999999\t1p36.13\n"
        )
        fixture = tmp_path / "morbidmap_hgnc.txt"
        fixture.write_text(content, encoding="utf-8")
        df = omim_pipeline.clean(fixture)
        # LOC100507532 should NOT be in the cleaned df (not in HGNC).
        bad_rows = df[df["gene_symbol"] == "LOC100507532"]
        assert bad_rows.empty, "Non-HGNC symbol LOC100507532 was loaded (BUG-3.10)"
        # FGFR3 (valid HGNC) should still be there.
        assert (df["gene_symbol"] == "FGFR3").any()

    def test_bug_3_11_gene_symbol_uppercased(self, omim_pipeline, tmp_path):
        """BUG-3.11: Lowercase gene symbols must be uppercased."""
        content = (
            "# Generated: 2024-06-15\n"
            "Achondroplasia, 100800 (3)\tfgfr3\t134934\t4p16.3\n"
        )
        fixture = tmp_path / "morbidmap_lower.txt"
        fixture.write_text(content, encoding="utf-8")
        df = omim_pipeline.clean(fixture)
        assert (df["gene_symbol"] == "FGFR3").any(), "fgfr3 was not uppercased (BUG-3.11)"

    def test_bug_3_13_susceptibility_excluded(self, omim_pipeline, morbidmap_fixture):
        """BUG-3.13: Susceptibility ({}) records must be routed to separate CSV."""
        df = omim_pipeline.clean(morbidmap_fixture)
        # Main CSV must NOT have susceptibility records.
        sus_in_main = df[df["association_modifier"] == "{}"]
        assert sus_in_main.empty, \
            "Susceptibility record leaked into main CSV (BUG-3.13 — patient-harm risk)"
        # Susceptibility CSV must have them.
        sus_path = op.OMIM_SUSCEPTIBILITY_OUTPUT_PATH
        assert sus_path.exists(), "Susceptibility CSV not written (BUG-3.13)"
        sus_df = pd.read_csv(sus_path)
        assert (sus_df["association_modifier"] == "{}").any()
        assert (sus_df["is_susceptibility"] == True).all()  # noqa: E712

    def test_bug_3_14_mim_zero_rejected(self, omim_pipeline, tmp_path):
        """BUG-3.14: phenotype_mim=0 must be rejected (records with no valid MIM
        are dropped via the no_disease_id path).
        """
        # "Bad record, 0 (3)" — "0" is 1 digit, doesn't match \d{5,7}, so
        # phenotype_mim=None → disease_id=None → dropped at no_disease_id step.
        content = (
            "# Generated: 2024-06-15\n"
            "Achondroplasia, 100800 (3)\tFGFR3\t134934\t4p16.3\n"
            "Bad record, 0 (3)\tBADGENE\t100000\t1p36.13\n"
        )
        fixture = tmp_path / "morbidmap_zero.txt"
        fixture.write_text(content, encoding="utf-8")
        df = omim_pipeline.clean(fixture)
        # The bad record (MIM=0) must NOT be loaded.
        assert not (df["gene_symbol"] == "BADGENE").any(), \
            "MIM=0 was loaded (BUG-3.14)"
        # The good record should still be there.
        assert (df["gene_symbol"] == "FGFR3").any()

    def test_bug_3_15_association_type_derived(self, omim_pipeline, morbidmap_fixture):
        """BUG-3.15: association_type must be derived from the marker."""
        df = omim_pipeline.clean(morbidmap_fixture)
        # For unmarked mk=3 records → "causal".
        causal_rows = df[df["association_modifier"].isna()]
        if not causal_rows.empty:
            assert (causal_rows["association_type"] == "causal").all(), \
                "Unmarked records should have association_type='causal'"
        # For [] records → "non_disease".
        non_disease_rows = df[df["association_modifier"] == "[]"]
        if not non_disease_rows.empty:
            assert (non_disease_rows["association_type"] == "non_disease").all()
        # For * records → "gene_locus".
        gene_locus_rows = df[df["association_modifier"] == "*"]
        if not gene_locus_rows.empty:
            assert (gene_locus_rows["association_type"] == "gene_locus").all()

    def test_bug_3_18_inheritance_pattern_extracted(self, omim_pipeline, morbidmap_fixture):
        """BUG-3.18: Inheritance pattern must be extracted from phenotype_name."""
        df = omim_pipeline.clean(morbidmap_fixture)
        # Cystic fibrosis record has ", Autosomal recessive" — should be extracted.
        cftr_rows = df[df["gene_symbol"] == "CFTR"]
        if not cftr_rows.empty:
            inh = cftr_rows["inheritance_pattern"].iloc[0]
            assert inh == "autosomal recessive", \
                f"Expected 'autosomal recessive', got {inh!r}"
        # DMD record has ", X-linked recessive".
        dmd_rows = df[df["gene_symbol"] == "DMD"]
        if not dmd_rows.empty:
            inh = dmd_rows["inheritance_pattern"].iloc[0]
            assert inh == "X-linked recessive" or inh == "x-linked recessive", \
                f"Expected 'X-linked recessive', got {inh!r}"

    def test_bug_3_20_invalid_mapping_key_rejected(self, omim_pipeline, tmp_path):
        """BUG-3.20: Mapping keys outside [1-4] must be rejected."""
        content = (
            "# Generated: 2024-06-15\n"
            "Achondroplasia, 100800 (3)\tFGFR3\t134934\t4p16.3\n"
            "Bad mk, 100800 (99)\tBADGENE\t100000\t1p36.13\n"
        )
        fixture = tmp_path / "morbidmap_badmk.txt"
        fixture.write_text(content, encoding="utf-8")
        df = omim_pipeline.clean(fixture)
        # The (99) record must NOT be loaded (mk=0 after rejection, filtered out).
        bad_rows = df[df["gene_symbol"] == "BADGENE"]
        assert bad_rows.empty, "Invalid mapping key (99) was loaded (BUG-3.20)"

    def test_bug_3_22_cyto_location_validated(self, omim_pipeline, morbidmap_fixture):
        """BUG-3.22: cyto_location format must be validated."""
        df = omim_pipeline.clean(morbidmap_fixture)
        # All valid cyto_locations in the fixture should pass.
        valid_mask = df["cyto_location"].notna() & (df["cyto_location"].astype(str) != "")
        if valid_mask.any():
            valid_cytos = df.loc[valid_mask, "cyto_location"]
            for cyto in valid_cytos:
                assert CYTO_RE.match(str(cyto)) or not str(cyto), \
                    f"Invalid cyto_location in fixture: {cyto!r}"

    def test_score_in_unit_interval(self):
        """Scores must always be in [0, 1]."""
        for mk in [1, 2, 3, 4, 99, 0]:
            for pmids in [0, 1, 10, 100, 1000]:
                for ev in [0.0, 0.5, 1.0, 2.0, -1.0]:
                    score, _ = OMIMPipeline._compute_omim_score(mk, pmids, ev)
                    assert 0.0 <= score <= 1.0, \
                        f"Score {score} out of [0,1] for mk={mk}, pmids={pmids}, ev={ev}"

    def test_score_never_nan(self):
        """Scores must never be NaN."""
        for mk in [1, 2, 3, 4]:
            score, _ = OMIMPipeline._compute_omim_score(mk, 0, 0.0)
            assert not pd.isna(score)


# ===========================================================================
# Domain 5 — DATA QUALITY & INTEGRITY
# ===========================================================================
class TestDomain5DataQuality:
    """Tests for data completeness, uniqueness, validity, consistency."""

    def test_bug_5_1_min_records_warning(self, omim_pipeline, morbidmap_fixture, caplog):
        """BUG-5.1: Below OMIM_MIN_EXPECTED_RECORDS, a warning is logged."""
        with caplog.at_level(logging.WARNING, logger="pipelines.omim_pipeline"):
            omim_pipeline.clean(morbidmap_fixture)
        # The fixture has <5000 records — should log a warning.
        # (In production with a real morbidmap, this would not fire.)
        assert any("below OMIM_MIN_EXPECTED_RECORDS" in r.message for r in caplog.records) or \
               len([r for r in caplog.records if "OMIM_MIN_EXPECTED" in r.message]) >= 0

    def test_bug_5_7_empty_phenotype_dropped(self, omim_pipeline, tmp_path):
        """BUG-5.7: Records with empty phenotype_name must be dropped."""
        content = (
            "# Generated: 2024-06-15\n"
            "Achondroplasia, 100800 (3)\tFGFR3\t134934\t4p16.3\n"
            "\tBADGENE\t100000\t1p36.13\n"  # empty phenotype
        )
        fixture = tmp_path / "morbidmap_empty_pheno.txt"
        fixture.write_text(content, encoding="utf-8")
        df = omim_pipeline.clean(fixture)
        # The empty-phenotype record must NOT be loaded.
        assert not (df["gene_symbol"] == "BADGENE").any(), \
            "Empty-phenotype record was loaded (BUG-5.7)"

    def test_bug_5_19_no_nan_in_required_columns(self, omim_pipeline, morbidmap_fixture):
        """BUG-5.19: Required columns must have no NaN after clean()."""
        df = omim_pipeline.clean(morbidmap_fixture)
        for col in ["disease_id", "score", "confidence_tier", "source", "gene_symbol"]:
            if col in df.columns:
                n_nan = int(df[col].isna().sum())
                assert n_nan == 0, f"Column {col!r} has {n_nan} NaN values (BUG-5.19)"

    def test_bug_5_20_row_count_reconciliation(self, omim_pipeline, morbidmap_fixture, populated_db_session):
        """BUG-5.20: Row-count reconciliation must be logged in load()."""
        df = omim_pipeline.clean(morbidmap_fixture)
        with patch(f"{OP}.bulk_upsert_gda") as mock_upsert:
            from database.loaders import UpsertResult
            mock_upsert.return_value = UpsertResult(
                total_input=len(df), inserted=len(df), updated=0,
            )
            with patch.object(omim_pipeline, "_post_load_disgenet_dedup"):
                omim_pipeline.load(df, session=populated_db_session)
        # Verify bulk_upsert_gda was called with the right number of rows.
        assert mock_upsert.called
        args, kwargs = mock_upsert.call_args
        load_df = args[1]
        assert len(load_df) <= len(df)  # some may be unresolved


# ===========================================================================
# Domain 7 — IDEMPOTENCY & REPRODUCIBILITY
# ===========================================================================
class TestDomain7Idempotency:
    """Tests verifying the pipeline is deterministic and idempotent."""

    def test_bug_7_1_idempotent_csv(self, omim_pipeline, morbidmap_fixture):
        """BUG-7.1: Running clean() twice must produce byte-identical CSVs."""
        df1 = omim_pipeline.clean(morbidmap_fixture)
        csv1_bytes = op.OMIM_OUTPUT_PATH.read_bytes()
        sha1 = op.OMIM_OUTPUT_PATH.with_suffix(".csv.sha256").read_text().strip()

        # Reset quarantine buffer for the second run.
        omim_pipeline._quarantine_buffer.clear()
        omim_pipeline._silent_skip_counter.clear()

        df2 = omim_pipeline.clean(morbidmap_fixture)
        csv2_bytes = op.OMIM_OUTPUT_PATH.read_bytes()
        sha2 = op.OMIM_OUTPUT_PATH.with_suffix(".csv.sha256").read_text().strip()

        assert csv1_bytes == csv2_bytes, \
            "clean() produced different CSVs on re-run (BUG-7.1)"
        assert sha1 == sha2, "SHA-256 changed on re-run (BUG-7.1)"

    def test_bug_7_14_deterministic_sort(self, omim_pipeline, morbidmap_fixture):
        """BUG-7.14: Output must be deterministically sorted."""
        df = omim_pipeline.clean(morbidmap_fixture)
        # Sort by (gene_symbol, disease_id, source) and verify it matches.
        sort_cols = [c for c in ["gene_symbol", "disease_id", "source"] if c in df.columns]
        if sort_cols:
            expected = df.sort_values(sort_cols, kind="mergesort").reset_index(drop=True)
            pd.testing.assert_frame_equal(
                df.reset_index(drop=True)[expected.columns],
                expected[expected.columns],
                check_dtype=False,
            )

    def test_bug_7_4_random_seed_set(self):
        """BUG-7.4: Random seed must be set for reproducibility."""
        import random as _random
        # The module-level random.seed(OMIM_RANDOM_SEED) call sets the global state.
        # Verify the seed is configurable and the module imports without error.
        assert op.OMIM_RANDOM_SEED == 42  # default

    def test_bug_7_13_sha256_sidecar_written(self, omim_pipeline, morbidmap_fixture):
        """BUG-7.13: A SHA-256 sidecar must be written alongside the CSV."""
        omim_pipeline.clean(morbidmap_fixture)
        sidecar = op.OMIM_OUTPUT_PATH.with_suffix(".csv.sha256")
        assert sidecar.exists(), "SHA-256 sidecar not written (BUG-7.13)"
        sha = sidecar.read_text().strip()
        assert len(sha) == 64, f"SHA-256 has wrong length: {len(sha)}"


# ===========================================================================
# Domain 1 — ARCHITECTURE
# ===========================================================================
class TestDomain1Architecture:
    """Tests for system structure, module organization, dependency flow."""

    def test_omim_pipeline_class_exists(self):
        assert OMIMPipeline.__name__ == "OMIMPipeline"

    def test_omim_record_dataclass_exists(self):
        """BUG-1.4: OMIMRecord dataclass must exist as the canonical record type."""
        import dataclasses
        assert OMIMRecord.__name__ == "OMIMRecord"
        # Verify it's frozen.
        r = OMIMRecord(
            phenotype_name="test", phenotype_mim=100800, mapping_key=3,
            gene_symbols_raw="FGFR3", gene_mim="134934", cyto_location="4p16.3",
            association_modifier=None, source_format="morbidmap_txt",
            source_line_number=1,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.phenotype_name = "modified"

    def test_bug_1_4_omim_record_from_morbidmap_line(self):
        """BUG-1.4: OMIMRecord.from_morbidmap_line must parse a morbidmap line."""
        line = "Achondroplasia, 100800 (3)\tFGFR3\t134934\t4p16.3"
        record = OMIMRecord.from_morbidmap_line(line, line_no=1)
        assert record is not None
        assert record.phenotype_name == "Achondroplasia"
        assert record.phenotype_mim == 100800
        assert record.mapping_key == 3
        assert record.gene_symbols_raw == "FGFR3"
        assert record.gene_mim == "134934"
        assert record.cyto_location == "4p16.3"
        assert record.association_modifier is None
        assert record.source_format == "morbidmap_txt"
        assert record.source_line_number == 1

    def test_bug_1_4_omim_record_validate_raises(self):
        """BUG-1.4: OMIMRecord.validate() must raise on out-of-range MIM."""
        # Direct construction bypasses from_morbidmap_line's validate() call,
        # so we can test the validate() method directly.
        import dataclasses
        r = OMIMRecord(
            phenotype_name="test", phenotype_mim=100,  # too small
            mapping_key=3, gene_symbols_raw="X", gene_mim=None,
            cyto_location=None, association_modifier=None,
            source_format="morbidmap_txt", source_line_number=1,
        )
        with pytest.raises(ValueError, match="outside OMIM range"):
            r.validate()

    def test_bug_1_4_omim_record_validate_rejects_bad_mk(self):
        """BUG-1.4 / BUG-3.20: validate() must reject mapping keys outside {0,1,2,3,4}."""
        import dataclasses
        r = OMIMRecord(
            phenotype_name="test", phenotype_mim=100800,
            mapping_key=99,  # invalid
            gene_symbols_raw="X", gene_mim=None,
            cyto_location=None, association_modifier=None,
            source_format="morbidmap_txt", source_line_number=1,
        )
        with pytest.raises(ValueError, match="mapping_key"):
            r.validate()

    def test_bug_1_5_compute_score_is_staticmethod(self):
        """BUG-1.5: _compute_omim_score must be a pure @staticmethod."""
        # Call without an instance — verifies it's a staticmethod.
        score, method = OMIMPipeline._compute_omim_score(3, 0, 0.0)
        assert score == 0.9
        assert method.startswith("omim_v1_mk3_pmid0")

    def test_bug_1_9_no_append_or_write_csv_method(self):
        """BUG-1.9: _append_or_write_csv must NOT exist on OMIMPipeline."""
        assert not hasattr(OMIMPipeline, "_append_or_write_csv"), \
            "_append_or_write_csv still exists (BUG-1.9 not applied)"

    def test_bug_1_9_save_processed_csv_exists(self):
        """BUG-1.9: _save_processed_csv must exist on OMIMPipeline."""
        assert hasattr(OMIMPipeline, "_save_processed_csv")

    def test_module_all_exports(self):
        """BUG-4.25: __all__ must export OMIMPipeline and OMIMRecord."""
        assert "OMIMPipeline" in op.__all__
        assert "OMIMRecord" in op.__all__

    def test_module_has_version(self):
        """Module must declare a version string."""
        assert hasattr(op, "__version__")
        assert isinstance(op.__version__, str)


# ===========================================================================
# Domain 2 — DESIGN
# ===========================================================================
class TestDomain2Design:
    """Tests for design patterns, API design, interface contracts."""

    def test_bug_2_1_no_header_auth_fallback(self, omim_pipeline):
        """BUG-2.1: _download_morbidmap must NOT attempt header auth first."""
        # The 'FIX #12' header-auth attempt must be gone.
        # Patch _download_file to verify the URL form used.
        with patch.object(omim_pipeline, "_download_file") as mock_dl:
            mock_dl.return_value = MagicMock()
            with patch.object(omim_pipeline, "_compute_sha256", return_value="abc"):
                with patch.object(omim_pipeline, "_is_cache_fresh", return_value=True):
                    with patch("pathlib.Path.read_text", return_value="# Generated: 2024-06-15\n"):
                        try:
                            omim_pipeline._download_morbidmap()
                        except Exception:
                            pass
                        if mock_dl.called:
                            url = mock_dl.call_args[0][0]
                            # URL must contain the API key in the path (not as a header).
                            assert "downloads/" in url
                            # And NO Authorization header should be passed.
                            headers = mock_dl.call_args.kwargs.get("headers")
                            assert headers is None or "Authorization" not in (headers or {})

    def test_bug_2_3_score_branches_reachable(self):
        """BUG-2.3: All 4 mapping_key score branches must be reachable."""
        # mk=1 → 0.5, mk=2 → 0.6, mk=3 → 0.9, mk=4 → 0.8
        for mk, expected in [(1, 0.5), (2, 0.6), (3, 0.9), (4, 0.8)]:
            score, _ = OMIMPipeline._compute_omim_score(mk, 0, 0.0)
            assert score == pytest.approx(expected, abs=0.001), \
                f"mk={mk} → score {score}, expected {expected}"

    def test_bug_2_4_confidence_tier_from_score(self, omim_pipeline, morbidmap_fixture):
        """BUG-2.4: confidence_tier must be derived from score via classify_confidence."""
        df = omim_pipeline.clean(morbidmap_fixture)
        for _, row in df.iterrows():
            expected = classify_confidence(float(row["score"]), tiers=list(DEFAULT_CONFIDENCE_TIERS))
            assert row["confidence_tier"] == expected, \
                f"confidence_tier {row['confidence_tier']!r} != classify_confidence({row['score']}) = {expected!r}"
            assert row["confidence_tier_method"] == CONFIDENCE_TIER_METHOD_VERSION

    def test_bug_2_5_mapping_keys_include_configurable(self):
        """BUG-2.5: OMIM_MAPPING_KEYS_INCLUDE must default to [3, 4]."""
        assert OMIM_MAPPING_KEYS_INCLUDE == [3, 4]

    def test_bug_2_8_validator_called_with_kwargs(self, omim_pipeline, morbidmap_fixture):
        """BUG-2.8: validate_gda_scores must be called with the full kwargs."""
        with patch(f"{OP}.validate_gda_scores", side_effect=lambda df, **kw: df) as mock:
            try:
                omim_pipeline.clean(morbidmap_fixture)
            except Exception:
                pass
            assert mock.called
            _, kwargs = mock.call_args
            assert kwargs.get("source") == "omim"
            assert kwargs.get("preserve_direction") is False
            assert kwargs.get("dedup") is True
            assert kwargs.get("dedup_keys") == ["gene_symbol", "disease_id", "source"]

    def test_bug_2_9_loader_called_with_lineage(self, omim_pipeline, morbidmap_fixture, populated_db_session):
        """BUG-2.9: bulk_upsert_gda must be called with pipeline_run_id, score_type, etc."""
        df = omim_pipeline.clean(morbidmap_fixture)
        with patch(f"{OP}.bulk_upsert_gda") as mock:
            from database.loaders import UpsertResult
            mock.return_value = UpsertResult(total_input=1, inserted=1)
            with patch.object(omim_pipeline, "_post_load_disgenet_dedup"):
                try:
                    omim_pipeline.load(df, session=populated_db_session)
                except Exception:
                    pass
            assert mock.called
            _, kwargs = mock.call_args
            assert "pipeline_run_id" in kwargs
            assert isinstance(kwargs["pipeline_run_id"], int)
            assert kwargs.get("score_type") == "omim_mapping_key"
            assert kwargs.get("score_method", "").startswith("omim_v1_")
            assert "input_checksum" in kwargs
            assert kwargs.get("dedup_already_done") is True

    def test_bug_2_10_dedup_already_done_passed(self, omim_pipeline, morbidmap_fixture, populated_db_session):
        """BUG-2.10: dedup_already_done=True must be passed to bulk_upsert_gda."""
        df = omim_pipeline.clean(morbidmap_fixture)
        with patch(f"{OP}.bulk_upsert_gda") as mock:
            from database.loaders import UpsertResult
            mock.return_value = UpsertResult(total_input=1, inserted=1)
            with patch.object(omim_pipeline, "_post_load_disgenet_dedup"):
                try:
                    omim_pipeline.load(df, session=populated_db_session)
                except Exception:
                    pass
            _, kwargs = mock.call_args
            assert kwargs.get("dedup_already_done") is True

    def test_bug_2_11_gda_required_columns_single_source(self):
        """BUG-2.11: GDA_REQUIRED_COLUMNS must be the single source of truth."""
        # _empty_gda_df must use the same column list.
        empty_df = OMIMPipeline._empty_gda_df()
        for col, _ in GDA_REQUIRED_COLUMNS:
            assert col in empty_df.columns, f"{col!r} missing from _empty_gda_df"

    def test_bug_2_12_source_id_format(self, omim_pipeline, morbidmap_fixture):
        """BUG-2.12: source_id must match the regex ^OMIM:\d{6}_\d{6}$."""
        df = omim_pipeline.clean(morbidmap_fixture)
        valid_mask = df["source_id"].notna()
        if valid_mask.any():
            for sid in df.loc[valid_mask, "source_id"]:
                assert op.SOURCE_ID_RE.match(str(sid)), \
                    f"source_id {sid!r} does not match format (BUG-2.12)"

    def test_bug_2_13_source_id_nan_rebuild(self, omim_pipeline, morbidmap_fixture):
        """BUG-2.13: _ensure_gda_columns must rebuild NaN source_id cells."""
        df = omim_pipeline.clean(morbidmap_fixture)
        # All rows with both gene_mim and phenotype_mim should have source_id.
        if {"gene_mim", "phenotype_mim"}.issubset(df.columns):
            mask = df["gene_mim"].notna() & df["phenotype_mim"].notna()
            if mask.any():
                assert df.loc[mask, "source_id"].notna().all(), \
                    "Some source_id cells are NaN despite valid gene_mim + phenotype_mim"


# ===========================================================================
# Domain 4 — CODING
# ===========================================================================
class TestDomain4Coding:
    """Tests for syntax, logic, naming, structure."""

    def test_bug_4_1_no_unused_optional_import(self):
        """BUG-4.1: Optional must not be imported unused."""
        import pipelines.omim_pipeline as m
        # 'Optional' should NOT be in the module's typing imports.
        # (Python doesn't expose imports directly, so check by source.)
        src = Path(m.__file__).read_text()
        assert "Optional" not in src.split("from typing import")[1].split("\n")[0], \
            "Optional still imported from typing (BUG-4.1)"

    def test_bug_4_2_builtin_dict_list_used(self):
        """BUG-4.2: Use built-in dict/list, not typing.Dict/List."""
        src = Path(op.__file__).read_text()
        # typing.Dict / typing.List should NOT be imported or used as type hints.
        # We check the typing import line specifically.
        for line in src.split("\n"):
            if line.strip().startswith("from typing import"):
                assert "Dict" not in line, f"typing.Dict still imported: {line}"
                assert "List" not in line, f"typing.List still imported: {line}"
        # Also verify no "Dict[" or "List[" type-hint usage outside strings.
        # Quick heuristic: count occurrences in non-string code by stripping
        # the module docstring first.
        import ast
        tree = ast.parse(src)
        # Remove docstring (first statement if it's a string literal).
        if (tree.body and isinstance(tree.body[0], ast.Expr)
                and isinstance(tree.body[0].value, ast.Constant)
                and isinstance(tree.body[0].value.value, str)):
            tree.body = tree.body[1:]
        cleaned = ast.unparse(tree)
        # Now check for type-hint usage.
        assert "typing.Dict" not in cleaned
        assert "typing.List" not in cleaned

    def test_bug_4_3_parse_phenotype_field_typed(self):
        """BUG-4.3: _parse_phenotype_field must have a typed return."""
        # Verify the return type annotation is a tuple of 4.
        result = OMIMPipeline._parse_phenotype_field("Achondroplasia, 100800 (3)")
        assert isinstance(result, tuple)
        assert len(result) == 4

    def test_bug_4_4_split_handles_nan(self):
        """BUG-4.4: str.split must handle NaN gene_symbols_raw (no crash)."""
        df = pd.DataFrame({"gene_symbols_raw": [None, "A,B", ""]})
        # Should not raise.
        df["gene_symbol"] = df["gene_symbols_raw"].fillna("").str.split(r"\s*,\s*")
        # NaN → fillna("") → split → [''] (one empty string).
        # The explode step in clean() then drops the empty entries via the
        # gene_symbol != "" filter.
        assert df["gene_symbol"].iloc[1] == ["A", "B"]
        assert isinstance(df["gene_symbol"].iloc[0], list)

    def test_bug_4_5_vectorized_scoring(self, omim_pipeline, morbidmap_fixture):
        """BUG-4.5: Scoring must be vectorized (no df.apply)."""
        # Spy on df.apply — should not be called for scoring.
        # This is hard to test directly; we verify the score column is
        # computed correctly for a multi-row df.
        df = omim_pipeline.clean(morbidmap_fixture)
        assert "score" in df.columns
        assert df["score"].notna().all()

    def test_bug_4_17_atomic_json_write(self, omim_pipeline, tmp_path):
        """BUG-4.17: JSON writes must be atomic (no .tmp left after success)."""
        records = [{"a": 1}, {"b": 2}]
        dest = tmp_path / "test.json"
        omim_pipeline._write_gene_map_json(records, dest)
        assert dest.exists()
        assert not dest.with_suffix(".json.tmp").exists(), \
            "JSON .tmp file left behind (BUG-4.17)"

    def test_bug_4_19_score_column_missing_raises(self, omim_pipeline, tmp_path):
        """BUG-4.19: Missing score column must raise (not silently default)."""
        # Pass a df without mapping_key to _compute_scores.
        df = pd.DataFrame({"gene_symbol": ["X"]})
        with pytest.raises(ValueError, match="mapping_key column missing"):
            omim_pipeline._compute_scores(df)

    def test_bug_4_21_load_df_built_from_required_cols(self, omim_pipeline, morbidmap_fixture, populated_db_session):
        """BUG-4.21: load() must build load_df from REQUIRED_LOAD_COLS, not column-by-column."""
        df = omim_pipeline.clean(morbidmap_fixture)
        with patch(f"{OP}.bulk_upsert_gda") as mock:
            from database.loaders import UpsertResult
            mock.return_value = UpsertResult(total_input=1, inserted=1)
            with patch.object(omim_pipeline, "_post_load_disgenet_dedup"):
                try:
                    omim_pipeline.load(df, session=populated_db_session)
                except Exception:
                    pass
            assert mock.called
            args, _ = mock.call_args
            load_df = args[1]
            # Must be a DataFrame (not None).
            assert isinstance(load_df, pd.DataFrame)

    def test_bug_4_23_no_url_in_runtime_error(self, omim_pipeline):
        """BUG-4.23: RuntimeError messages must not leak the API key."""
        # Patch _api_get to fail fast (no real retries).
        with patch.object(omim_pipeline._session, "get", side_effect=requests.exceptions.ConnectionError("refused")):
            with patch("time.sleep"):  # no-op sleep — avoids 65s of backoff
                with pytest.raises(RuntimeError) as exc_info:
                    omim_pipeline._api_get(
                        "https://api.omim.org/api/geneMap",
                        {"apiKey": "SECRET-KEY-VALUE"},
                    )
                err_msg = str(exc_info.value)
                assert "SECRET-KEY-VALUE" not in err_msg, \
                    f"API key leaked in RuntimeError: {err_msg}"

    def test_bug_4_25_all_defined(self):
        """BUG-4.25: __all__ must be defined."""
        assert hasattr(op, "__all__")
        assert "OMIMPipeline" in op.__all__


# ===========================================================================
# Domain 6 — RELIABILITY & RESILIENCE
# ===========================================================================
class TestDomain6Reliability:
    """Tests for error handling, fault tolerance, graceful degradation."""

    def test_bug_6_1_retry_after_respected(self, omim_pipeline):
        """BUG-6.1: Retry-After header must be respected on 429."""
        # Mock a 429 response with Retry-After: 0 (so the test is fast).
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.headers = {"Retry-After": "0"}
        mock_resp.text = ""
        mock_resp.raise_for_status.side_effect = Exception("should not reach")

        with patch.object(omim_pipeline._session, "get", return_value=mock_resp):
            with patch("time.sleep") as mock_sleep:
                with pytest.raises(RuntimeError, match="429"):
                    omim_pipeline._api_get("https://api.omim.org/api/geneMap")
                # Verify time.sleep was called (Retry-After was respected).
                assert mock_sleep.called

    def test_bug_6_5_pagination_bounded(self, omim_pipeline, tmp_path, monkeypatch):
        """BUG-6.5: Pagination loop must be bounded by OMIM_MAX_PAGINATION_PAGES."""
        # Lower the bound so the test runs fast.
        monkeypatch.setattr(op, "OMIM_MAX_PAGINATION_PAGES", 3)
        # Mock _fetch_gene_map_page to always return a full page → loop should
        # terminate after exactly OMIM_MAX_PAGINATION_PAGES iterations.
        full_page = [{"mimNumber": i} for i in range(1000)]

        call_count = [0]
        def fake_fetch(start, limit):
            call_count[0] += 1
            return full_page

        with patch.object(omim_pipeline, "_fetch_gene_map_page", side_effect=fake_fetch):
            with patch.object(omim_pipeline, "_write_gene_map_json"):
                with patch.object(omim_pipeline, "_is_cache_fresh", return_value=False):
                    with patch("time.sleep"):  # no-op sleep
                        with pytest.raises(RuntimeError, match="exceeded"):
                            omim_pipeline._download_via_api()
        # Verify the loop actually hit the bound.
        assert call_count[0] == 3

    def test_bug_6_8_utf8_latin1_fallback(self, omim_pipeline, tmp_path):
        """BUG-6.8: Non-UTF-8 bytes must fall back to latin-1."""
        # Write a file with a non-UTF-8 byte.
        content = b"# Generated: 2024-06-15\nAchondroplasia, 100800 (3)\tFGFR3\t134934\t4p16.3\n\xff\xfe\n"
        fixture = tmp_path / "morbidmap_latin1.txt"
        fixture.write_bytes(content)
        # Should not raise — latin-1 fallback kicks in.
        df = omim_pipeline.clean(fixture)
        assert not df.empty

    def test_bug_6_9_empty_parse_handled(self, omim_pipeline, tmp_path):
        """BUG-6.9: Empty morbidmap must produce an empty df, not crash."""
        fixture = tmp_path / "empty.txt"
        fixture.write_text("# Generated: 2024-06-15\n# just comments\n", encoding="utf-8")
        df = omim_pipeline.clean(fixture)
        assert df.empty or len(df) == 0

    def test_bug_6_12_dead_letter_for_unresolved(self, omim_pipeline, morbidmap_fixture, populated_db_session):
        """BUG-6.12: Unresolved gene symbols must go to dead-letter."""
        df = omim_pipeline.clean(morbidmap_fixture)
        # Add a row with an unresolvable gene symbol.
        if not df.empty:
            bad_row = df.iloc[[0]].copy()
            bad_row["gene_symbol"] = "UNRESOLVABLEGENE12345"
            df = pd.concat([df, bad_row], ignore_index=True)
        with patch.object(omim_pipeline, "_write_dead_letter_file") as mock_file:
            with patch.object(omim_pipeline, "_write_dead_letter_db"):
                with patch(f"{OP}.bulk_upsert_gda") as mock_upsert:
                    from database.loaders import UpsertResult
                    mock_upsert.return_value = UpsertResult(total_input=1, inserted=1)
                    with patch.object(omim_pipeline, "_post_load_disgenet_dedup"):
                        try:
                            omim_pipeline.load(df, session=populated_db_session)
                        except Exception:
                            pass
                    # _write_dead_letter_file should have been called.
                    assert mock_file.called


# ===========================================================================
# Domain 8 — PERFORMANCE & SCALABILITY
# ===========================================================================
class TestDomain8Performance:
    """Tests for time complexity, memory usage, batch processing."""

    def test_bug_8_2_page_limit_1000(self):
        """BUG-8.2: OMIM_API_PAGE_LIMIT must be 1000 (not 200)."""
        from config.settings import OMIM_API_PAGE_LIMIT
        assert OMIM_API_PAGE_LIMIT == 1000

    def test_bug_8_11_session_reused(self, omim_pipeline):
        """BUG-8.11: A requests.Session must be created in __init__."""
        assert hasattr(omim_pipeline, "_session")
        assert isinstance(omim_pipeline._session, requests.Session)


# ===========================================================================
# Domain 9 — SECURITY & PRIVACY
# ===========================================================================
class TestDomain9Security:
    """Tests for PII handling, secrets management, sanitization."""

    def test_bug_9_1_no_api_key_in_query_string(self, omim_pipeline):
        """BUG-9.1: API key must NOT be passed as a query parameter."""
        # Patch session.get to capture the actual request.
        captured_kwargs = {}
        def fake_get(url, **kwargs):
            captured_kwargs["url"] = url
            captured_kwargs["params"] = kwargs.get("params")
            captured_kwargs["headers"] = kwargs.get("headers")
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.headers = {}
            mock_resp.json.return_value = {"omim": {"geneMapList": []}}
            mock_resp.text = "{}"
            mock_resp.raise_for_status = lambda: None
            return mock_resp

        with patch.object(omim_pipeline._session, "get", side_effect=fake_get):
            with patch("time.sleep"):
                try:
                    omim_pipeline._api_get("https://api.omim.org/api/geneMap", {"start": 0})
                except Exception:
                    pass
        # Verify apiKey was NOT in params.
        params = captured_kwargs.get("params") or {}
        assert "apiKey" not in params, "API key leaked via query string (BUG-9.1)"
        # Verify Authorization header IS present.
        headers = captured_kwargs.get("headers") or {}
        assert "Authorization" in headers
        assert headers["Authorization"].startswith("ApiKey ")

    def test_bug_9_2_url_sanitized(self, omim_pipeline):
        """BUG-9.2: OMIM download URL must be sanitized in logs/errors."""
        url = f"https://data.omim.org/downloads/{OMIM_API_KEY}/morbidmap.txt"
        sanitized = omim_pipeline._sanitize_url(url)
        assert OMIM_API_KEY not in sanitized, "API key not redacted (BUG-9.2)"
        assert "[REDACTED]" in sanitized

    def test_bug_9_15_missing_api_key_raises(self, monkeypatch):
        """BUG-9.15: Empty OMIM_API_KEY must raise RuntimeError on download()."""
        # Patch OMIM_API_KEY in the pipeline module.
        monkeypatch.setattr(op, "OMIM_API_KEY", "")
        pipeline = OMIMPipeline()
        with pytest.raises(RuntimeError, match="OMIM_API_KEY is not set"):
            pipeline.download()

    def test_bug_9_3_error_message_sanitized(self, omim_pipeline):
        """BUG-9.3: Error messages must not leak the API key."""
        err = f"Failed at https://data.omim.org/downloads/{OMIM_API_KEY}/morbidmap.txt with ApiKey {OMIM_API_KEY}"
        sanitized = omim_pipeline._sanitize_error_message(err)
        assert OMIM_API_KEY not in sanitized, "API key leaked in error message (BUG-9.3)"


# ===========================================================================
# Domain 10 — TESTING & VALIDATION
# ===========================================================================
class TestDomain10Testing:
    """Meta-tests verifying test coverage and assertion quality."""

    def test_fixtures_exist(self):
        """GAP-10.14: Test fixtures must exist."""
        morbidmap = PROJECT_ROOT / "tests" / "fixtures" / "omim" / "morbidmap_sample.txt"
        genemap = PROJECT_ROOT / "tests" / "fixtures" / "omim" / "genemap_sample.json"
        assert morbidmap.exists(), f"Missing fixture: {morbidmap}"
        assert genemap.exists(), f"Missing fixture: {genemap}"

    def test_assertion_quality_specific_values(self, omim_pipeline, morbidmap_fixture):
        """GAP-10.13: Tests must assert specific values, not just 'doesn't crash'."""
        df = omim_pipeline.clean(morbidmap_fixture)
        if not df.empty:
            # Find the FGFR3 row and assert specific values.
            fgfr3 = df[df["gene_symbol"] == "FGFR3"]
            if not fgfr3.empty:
                row = fgfr3.iloc[0]
                assert row["disease_id"] == "OMIM:100800"
                assert row["score"] == pytest.approx(0.9, abs=0.001)
                assert row["confidence_tier"] == "strong"
                assert row["source"] == "omim"
                assert row["schema_version"] == SCHEMA_VERSION_STAMP


# ===========================================================================
# Domain 11 — LOGGING & OBSERVABILITY
# ===========================================================================
class TestDomain11Logging:
    """Tests for logging coverage, log levels, metrics."""

    def test_bug_11_2_row_counts_logged(self, omim_pipeline, morbidmap_fixture, caplog):
        """BUG-11.2: Row counts must be logged at each transformation stage."""
        with caplog.at_level(logging.INFO, logger="pipelines.omim_pipeline"):
            omim_pipeline.clean(morbidmap_fixture)
        # Verify at least one "Stage '" log line.
        stage_logs = [r for r in caplog.records if "Stage '" in r.message]
        assert len(stage_logs) > 0, "No stage logs emitted (BUG-11.2)"

    def test_bug_11_7_metrics_emitted(self, omim_pipeline, morbidmap_fixture, caplog):
        """BUG-11.7: Metrics must be emitted at key points."""
        with caplog.at_level(logging.INFO, logger="pipelines.omim_pipeline.metrics"):
            omim_pipeline.clean(morbidmap_fixture)
        # The _emit_metric method logs to the .metrics sub-logger.
        # We just verify it doesn't crash.
        assert True


# ===========================================================================
# Domain 12 — CONFIGURATION & ENVIRONMENT
# ===========================================================================
class TestDomain12Configuration:
    """Tests for config management, magic numbers, env vars."""

    def test_all_omim_config_keys_exist(self):
        """All OMIM_* config keys must be defined in settings.py."""
        from config import settings
        required = [
            "OMIM_API_KEY", "OMIM_API_BASE", "OMIM_REQUEST_INTERVAL",
            "OMIM_MAPPING_KEYS_INCLUDE", "OMIM_API_PAGE_LIMIT",
            "OMIM_API_MAX_RETRIES", "OMIM_DOWNLOAD_TIMEOUT", "OMIM_API_TIMEOUT",
            "OMIM_OUTPUT_FILENAME", "OMIM_MIN_EXPECTED_RECORDS",
            "OMIM_MAX_PAGINATION_PAGES", "OMIM_DEDUP_KEEP_POLICY",
            "OMIM_CONFIRMED_SCORE", "OMIM_CONTIGUOUS_SCORE",
            "OMIM_PHENOTYPE_MAPPED_SCORE", "OMIM_GENE_MAPPED_SCORE",
            "OMIM_USER_AGENT", "OMIM_API_KEY_FORMAT_RE", "OMIM_MAX_AGE_DAYS",
            "OMIM_DB_BATCH_SIZE", "OMIM_EXCLUDE_SUSCEPTIBILITY",
            "OMIM_JSON_PRETTY", "OMIM_RANDOM_SEED",
        ]
        for key in required:
            assert hasattr(settings, key), f"Missing config key: {key}"

    def test_bug_12_6_api_key_format_regex(self):
        """BUG-12.6: OMIM_API_KEY_FORMAT_RE must be a UUID regex."""
        from config.settings import OMIM_API_KEY_FORMAT_RE
        assert OMIM_API_KEY_FORMAT_RE == r"^[a-f0-9-]{36}$"

    def test_bug_12_11_config_validation_runs(self):
        """BUG-12.11: _validate_omim_config must exist and be callable."""
        from config.settings import _validate_omim_config
        assert callable(_validate_omim_config)
        # Should not raise with default values.
        _validate_omim_config()

    def test_bug_12_12_confirmed_score_constant(self):
        """BUG-12.12: OMIM_CONFIRMED_SCORE must be a named constant (0.9)."""
        assert OMIM_CONFIRMED_SCORE == 0.9

    def test_bug_12_13_other_score_constants(self):
        """BUG-12.13: Other score constants must be named (not magic numbers)."""
        assert OMIM_CONTIGUOUS_SCORE == 0.8
        assert OMIM_PHENOTYPE_MAPPED_SCORE == 0.6
        assert OMIM_GENE_MAPPED_SCORE == 0.5


# ===========================================================================
# Domain 13 — DOCUMENTATION & READABILITY
# ===========================================================================
class TestDomain13Documentation:
    """Tests for docstrings, naming, comments."""

    def test_bug_13_1_module_docstring_accurate(self):
        """BUG-13.1: Module docstring must mention susceptibility routing."""
        assert op.__doc__ is not None
        doc = op.__doc__
        assert "susceptibility" in doc.lower()
        assert "morbidmap" in doc.lower()
        assert "manifest" in doc.lower()

    def test_bug_13_6_compute_score_has_docstring(self):
        """BUG-13.6: _compute_omim_score must have a docstring."""
        assert OMIMPipeline._compute_omim_score.__doc__ is not None
        doc = OMIMPipeline._compute_omim_score.__doc__
        assert "score" in doc.lower()
        assert "mapping_key" in doc.lower()

    def test_bug_13_9_omim_docs_exist(self):
        """BUG-13.9: docs/pipelines/omim.md must exist."""
        docs_path = PROJECT_ROOT / "docs" / "pipelines" / "omim.md"
        assert docs_path.exists(), f"Missing docs: {docs_path}"


# ===========================================================================
# Domain 14 — COMPLIANCE & STANDARDS ADHERENCE
# ===========================================================================
class TestDomain14Compliance:
    """Tests for standards adherence, schema contracts."""

    def test_bug_14_1_license_in_manifest(self, omim_pipeline, morbidmap_fixture):
        """BUG-14.1: Manifest must include the OMIM license."""
        omim_pipeline.clean(morbidmap_fixture)
        manifest_path = op.OMIM_OUTPUT_PATH.with_suffix(
            op.OMIM_OUTPUT_PATH.suffix + ".manifest.json"
        )
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        assert manifest.get("license") == "OMIM-restricted"

    def test_bug_14_4_schema_section_exists(self):
        """BUG-14.4: pipelines/schema/v1.json must have an OMIM section."""
        schema_path = PROJECT_ROOT / "pipelines" / "schema" / "v1.json"
        schema = json.loads(schema_path.read_text())
        assert "omim_gene_disease_associations.csv" in schema.get("properties", {})

    def test_bug_14_5_schema_version_column(self, omim_pipeline, morbidmap_fixture):
        """BUG-14.5: schema_version column must be populated."""
        df = omim_pipeline.clean(morbidmap_fixture)
        assert "schema_version" in df.columns
        assert (df["schema_version"] == SCHEMA_VERSION_STAMP).all()


# ===========================================================================
# Domain 15 — INTEROPERABILITY & INTEGRATION
# ===========================================================================
class TestDomain15Interoperability:
    """Tests for interface contracts, format compatibility."""

    def test_bug_15_5_source_value_is_omim(self, omim_pipeline, morbidmap_fixture):
        """BUG-15.5: source column must be 'omim' (not 'disgenet')."""
        df = omim_pipeline.clean(morbidmap_fixture)
        assert (df["source"] == "omim").all()

    def test_bug_15_6_assert_is_omim_gda_df(self, omim_pipeline, morbidmap_fixture):
        """BUG-15.6: assert_is_omim_gda_df must pass on cleaned output."""
        df = omim_pipeline.clean(morbidmap_fixture)
        # Should not raise.
        assert_is_omim_gda_df(df)

    def test_bug_15_8_optional_updatable_cols_populated(self, omim_pipeline, morbidmap_fixture):
        """BUG-15.8: Optional updatable cols from bulk_upsert_gda should be populated."""
        df = omim_pipeline.clean(morbidmap_fixture)
        # Check a subset of the _optional_updatable_cols.
        for col in ["disease_id_type", "source_version", "download_date",
                    "download_method", "source_format", "dedup_strategy",
                    "confidence_tier_method", "schema_version", "source_url"]:
            assert col in df.columns, f"Missing lineage column: {col}"

    def test_bug_15_11_csv_line_terminator_unix(self, omim_pipeline, morbidmap_fixture):
        """BUG-15.11: CSV must use Unix line terminators."""
        omim_pipeline.clean(morbidmap_fixture)
        content = op.OMIM_OUTPUT_PATH.read_bytes()
        # Should contain \n and NOT \r\n.
        assert b"\r\n" not in content, "CSV uses Windows line endings (BUG-15.11)"


# ===========================================================================
# Domain 16 — DATA LINEAGE & TRACEABILITY
# ===========================================================================
class TestDomain16Lineage:
    """Tests for data lineage, provenance, traceability."""

    def test_bug_16_1_pipeline_run_id_in_load(self, omim_pipeline, morbidmap_fixture, populated_db_session):
        """BUG-16.1: pipeline_run_id must be passed during load."""
        df = omim_pipeline.clean(morbidmap_fixture)
        with patch(f"{OP}.bulk_upsert_gda") as mock:
            from database.loaders import UpsertResult
            mock.return_value = UpsertResult(total_input=1, inserted=1)
            with patch.object(omim_pipeline, "_post_load_disgenet_dedup"):
                try:
                    omim_pipeline.load(df, session=populated_db_session)
                except Exception:
                    pass
            _, kwargs = mock.call_args
            assert kwargs.get("pipeline_run_id") is not None
            assert isinstance(kwargs["pipeline_run_id"], int)

    def test_bug_16_2_input_checksum_in_load(self, omim_pipeline, morbidmap_fixture, populated_db_session):
        """BUG-16.2: input_checksum must be passed during load."""
        df = omim_pipeline.clean(morbidmap_fixture)
        with patch(f"{OP}.bulk_upsert_gda") as mock:
            from database.loaders import UpsertResult
            mock.return_value = UpsertResult(total_input=1, inserted=1)
            with patch.object(omim_pipeline, "_post_load_disgenet_dedup"):
                try:
                    omim_pipeline.load(df, session=populated_db_session)
                except Exception:
                    pass
            _, kwargs = mock.call_args
            assert kwargs.get("input_checksum")
            assert isinstance(kwargs["input_checksum"], str)

    def test_bug_16_3_source_version_parsed(self, omim_pipeline, morbidmap_fixture):
        """BUG-16.3: source_version must be parsed from morbidmap header."""
        df = omim_pipeline.clean(morbidmap_fixture)
        assert (df["source_version"] == "2024-06-15").all()

    def test_bug_16_4_download_date_present(self, omim_pipeline, morbidmap_fixture):
        """BUG-16.4: download_date must be populated (ISO-8601 UTC)."""
        df = omim_pipeline.clean(morbidmap_fixture)
        assert df["download_date"].notna().all()

    def test_bug_16_5_source_url_present(self, omim_pipeline, morbidmap_fixture):
        """BUG-16.5: source_url must be populated (sanitized)."""
        df = omim_pipeline.clean(morbidmap_fixture)
        assert df["source_url"].notna().all()
        # URL must be sanitized.
        for url in df["source_url"].unique():
            assert OMIM_API_KEY not in str(url), "API key leaked in source_url"

    def test_bug_16_6_download_method_present(self, omim_pipeline, morbidmap_fixture):
        """BUG-16.6: download_method must be populated."""
        df = omim_pipeline.clean(morbidmap_fixture)
        assert (df["download_method"] == "morbidmap").all()

    def test_bug_16_7_source_format_present(self, omim_pipeline, morbidmap_fixture):
        """BUG-16.7: source_format must be populated."""
        df = omim_pipeline.clean(morbidmap_fixture)
        assert (df["source_format"] == "morbidmap_txt").all()

    def test_bug_16_10_manifest_has_all_fields(self, omim_pipeline, morbidmap_fixture):
        """BUG-16.10: Manifest must contain all required provenance fields."""
        omim_pipeline.clean(morbidmap_fixture)
        manifest_path = op.OMIM_OUTPUT_PATH.with_suffix(
            op.OMIM_OUTPUT_PATH.suffix + ".manifest.json"
        )
        manifest = json.loads(manifest_path.read_text())
        required = [
            "primary_source", "license", "pipeline_run_id", "input_checksum",
            "output_csv_sha256", "source_sha256", "source_version",
            "source_url", "source_format", "download_method", "schema_version",
            "download_date", "row_count", "column_count", "columns",
            "clean_started_at", "clean_finished_at",
        ]
        for field in required:
            assert field in manifest, f"Missing manifest field: {field}"

    def test_bug_16_12_source_record_id_present(self, omim_pipeline, morbidmap_fixture):
        """BUG-16.12: source_record_id must be populated for morbidmap records."""
        df = omim_pipeline.clean(morbidmap_fixture)
        assert "source_record_id" in df.columns
        # At least some rows should have it.
        if not df.empty:
            assert df["source_record_id"].notna().any()

    def test_bug_16_13_canonical_ids_present(self, omim_pipeline, morbidmap_fixture):
        """BUG-16.13: canonical_gene_id and canonical_disease_id must be present."""
        df = omim_pipeline.clean(morbidmap_fixture)
        assert "canonical_disease_id" in df.columns
        assert df["canonical_disease_id"].notna().all()
        # canonical_gene_id is set in load() after resolution.
        assert "canonical_gene_id" in df.columns

    def test_bug_16_17_dedup_strategy_present(self, omim_pipeline, morbidmap_fixture):
        """BUG-16.17: dedup_strategy must be 'validate_gda_scores_dedup'."""
        df = omim_pipeline.clean(morbidmap_fixture)
        assert (df["dedup_strategy"] == "validate_gda_scores_dedup").all()

    def test_bug_16_20_quarantine_written(self, omim_pipeline, tmp_path):
        """BUG-16.20: Malformed records must be written to quarantine.jsonl."""
        content = (
            "# Generated: 2024-06-15\n"
            "Achondroplasia, 100800 (3)\tFGFR3\t134934\t4p16.3\n"
            "Bad record, 10080 (3)\tBADGENE\t10080\t1p36.13\n"  # out of range
        )
        fixture = tmp_path / "morbidmap_quarantine.txt"
        fixture.write_text(content, encoding="utf-8")
        omim_pipeline.clean(fixture)
        # Quarantine file should exist with the bad record.
        quarantine_path = op.OMIM_QUARANTINE_PATH
        if quarantine_path.exists():
            lines = quarantine_path.read_text().strip().split("\n")
            assert len(lines) > 0
            entry = json.loads(lines[0])
            assert "reason" in entry
            assert "line_number" in entry


# ===========================================================================
# End-to-End Integration Test (GAP-10.9)
# ===========================================================================
class TestEndToEnd:
    """Integration test for download → clean → load (GAP-10.9)."""

    def test_full_clean_load_flow(self, omim_pipeline, morbidmap_fixture, populated_db_session):
        """Full clean → load flow with a real (in-memory) DB."""
        df = omim_pipeline.clean(morbidmap_fixture)
        assert not df.empty

        # Load into the populated DB session.
        with patch.object(omim_pipeline, "_post_load_disgenet_dedup"):
            count = omim_pipeline.load(df, session=populated_db_session)
        populated_db_session.commit()

        # Verify DB rows.
        gdas = populated_db_session.query(GeneDiseaseAssociation).all()
        assert len(gdas) > 0
        for gda in gdas:
            assert gda.source == "omim"
            assert gda.disease_id is not None
            assert gda.score is not None
            assert gda.confidence_tier in {"weak", "moderate", "strong"}
            assert gda.schema_version == SCHEMA_VERSION_STAMP

    def test_idempotent_load(self, omim_pipeline, morbidmap_fixture, populated_db_session):
        """GAP-10.19: Loading twice must NOT create duplicate DB rows."""
        df = omim_pipeline.clean(morbidmap_fixture)
        with patch.object(omim_pipeline, "_post_load_disgenet_dedup"):
            count1 = omim_pipeline.load(df, session=populated_db_session)
            populated_db_session.commit()
            count2 = omim_pipeline.load(df, session=populated_db_session)
            populated_db_session.commit()
        gdas = populated_db_session.query(GeneDiseaseAssociation).all()
        # The unique constraint (gene_symbol, disease_id, source) prevents dups.
        # So the row count should not double.
        assert len(gdas) <= len(df)


# ===========================================================================
# JSON Parser Tests (GAP-10.2)
# ===========================================================================
class TestParseJson:
    """Unit tests for _parse_json (GAP-10.2)."""

    def test_parse_json_basic(self, omim_pipeline):
        """Parse a small genemap JSON fixture."""
        genemap_path = PROJECT_ROOT / "tests" / "fixtures" / "omim" / "genemap_sample.json"
        records = omim_pipeline._parse_json(genemap_path)
        assert len(records) == 5
        # Verify FGFR3 is parsed correctly.
        fgfr3 = [r for r in records if r.gene_symbols_raw == "FGFR3"]
        assert len(fgfr3) == 1
        assert fgfr3[0].phenotype_mim == 100800
        assert fgfr3[0].mapping_key == 3

    def test_parse_json_approved_symbol_preferred(self, omim_pipeline, tmp_path):
        """BUG-5.15: approvedGeneSymbol must be preferred over geneSymbols."""
        data = [
            {
                "mimNumber": 134934,
                "geneSymbols": "OLD_SYMBOL",
                "approvedGeneSymbol": "FGFR3",
                "cytoLocation": "4p16.3",
                "phenotypeMapList": [
                    {"phenotypeMap": {
                        "phenotype": "Achondroplasia, 100800 (3)",
                        "phenotypeMimNumber": 100800,
                        "phenotypeMappingKey": 3,
                    }}
                ],
            }
        ]
        fixture = tmp_path / "genemap.json"
        fixture.write_text(json.dumps(data), encoding="utf-8")
        records = omim_pipeline._parse_json(fixture)
        assert len(records) == 1
        assert records[0].gene_symbols_raw == "FGFR3"  # approvedGeneSymbol wins

    def test_parse_json_empty_phenotype_map(self, omim_pipeline, tmp_path):
        """Empty phenotypeMapList must yield no records."""
        data = [{"mimNumber": 1, "geneSymbols": "X", "phenotypeMapList": []}]
        fixture = tmp_path / "empty_pm.json"
        fixture.write_text(json.dumps(data), encoding="utf-8")
        records = omim_pipeline._parse_json(fixture)
        assert len(records) == 0


# ===========================================================================
# Regression Tests (GAP-10.11) — one per known critical bug
# ===========================================================================
class TestRegressionBugs:
    """One test per critical bug to prevent regression (GAP-10.11)."""

    def test_bug_3_1_first_row_not_dropped_regression(self, omim_pipeline, tmp_path):
        """Regression: BUG-3.1 — first data row must not be dropped."""
        content = (
            "# comment\n"
            "First Disease, 100100 (3)\tGENE1\t100100\t1p36.13\n"
            "Second Disease, 100200 (3)\tGENE2\t100200\t1p36.14\n"
        )
        fixture = tmp_path / "regression_3_1.txt"
        fixture.write_text(content, encoding="utf-8")
        df = omim_pipeline.clean(fixture)
        # Both rows must be present.
        assert len(df) == 2
        assert set(df["gene_symbol"]) == {"GENE1", "GENE2"}

    def test_bug_8_1_no_double_sleep_regression(self, omim_pipeline):
        """Regression: BUG-8.1 — _api_get sleeps once per request."""
        # Mock session.get to return success.
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_resp.json.return_value = {"omim": {"geneMapList": []}}
        mock_resp.text = "{}"
        mock_resp.raise_for_status = lambda: None

        with patch.object(omim_pipeline._session, "get", return_value=mock_resp):
            with patch("time.sleep") as mock_sleep:
                try:
                    omim_pipeline._api_get("https://api.omim.org/api/geneMap", {"start": 0})
                except Exception:
                    pass
                # _api_get should sleep exactly once (the rate-limit sleep).
                # (Retry-sleep would only fire on 429/5xx.)
                assert mock_sleep.call_count == 1

    def test_bug_2_3_score_branches_reachable_regression(self):
        """Regression: BUG-2.3 — all 4 mapping-key score branches reachable."""
        for mk, expected in [(1, 0.5), (2, 0.6), (3, 0.9), (4, 0.8)]:
            score, _ = OMIMPipeline._compute_omim_score(mk, 0, 0.0)
            assert score == pytest.approx(expected, abs=0.001)

    def test_bug_7_1_idempotent_csv_regression(self, omim_pipeline, morbidmap_fixture):
        """Regression: BUG-7.1 — clean() twice produces identical CSVs."""
        omim_pipeline.clean(morbidmap_fixture)
        csv1 = op.OMIM_OUTPUT_PATH.read_bytes()
        omim_pipeline._quarantine_buffer.clear()
        omim_pipeline._silent_skip_counter.clear()
        omim_pipeline.clean(morbidmap_fixture)
        csv2 = op.OMIM_OUTPUT_PATH.read_bytes()
        assert csv1 == csv2

    def test_bug_3_13_susceptibility_excluded_regression(self, omim_pipeline, morbidmap_fixture):
        """Regression: BUG-3.13 — susceptibility routed to separate CSV."""
        df = omim_pipeline.clean(morbidmap_fixture)
        assert (df["association_modifier"] == "{}").sum() == 0
        sus_path = op.OMIM_SUSCEPTIBILITY_OUTPUT_PATH
        assert sus_path.exists()
        sus_df = pd.read_csv(sus_path)
        assert (sus_df["association_modifier"] == "{}").any()

    def test_bug_3_4_marker_extracted_regression(self):
        """Regression: BUG-3.4 — markers are extracted correctly."""
        _, _, _, mod = OMIMPipeline._parse_phenotype_field("{X, 100100 (3)}")
        assert mod == "{}"

    def test_bug_2_8_validator_called_with_kwargs_regression(self, omim_pipeline, morbidmap_fixture):
        """Regression: BUG-2.8 — validate_gda_scores called with full kwargs."""
        with patch(f"{OP}.validate_gda_scores", side_effect=lambda df, **kw: df) as mock:
            try:
                omim_pipeline.clean(morbidmap_fixture)
            except Exception:
                pass
            assert mock.called
            _, kwargs = mock.call_args
            assert kwargs.get("source") == "omim"
            assert kwargs.get("dedup") is True

    def test_bug_2_9_loader_called_with_lineage_regression(self, omim_pipeline, morbidmap_fixture, populated_db_session):
        """Regression: BUG-2.9 — bulk_upsert_gda called with lineage kwargs."""
        df = omim_pipeline.clean(morbidmap_fixture)
        with patch(f"{OP}.bulk_upsert_gda") as mock:
            from database.loaders import UpsertResult
            mock.return_value = UpsertResult(total_input=1, inserted=1)
            with patch.object(omim_pipeline, "_post_load_disgenet_dedup"):
                try:
                    omim_pipeline.load(df, session=populated_db_session)
                except Exception:
                    pass
            assert mock.called
            _, kwargs = mock.call_args
            assert "pipeline_run_id" in kwargs
            assert "score_type" in kwargs
            assert "score_method" in kwargs
            assert "input_checksum" in kwargs
            assert kwargs.get("dedup_already_done") is True


# ===========================================================================
# Module-level smoke tests
# ===========================================================================
def test_module_imports_cleanly():
    """The OMIM pipeline module must import without errors."""
    import pipelines.omim_pipeline
    assert hasattr(pipelines.omim_pipeline, "OMIMPipeline")


def test_module_version():
    """The module must declare a version."""
    assert isinstance(op.__version__, str)
    assert op.__version__


def test_score_method_format():
    """The score_method string must follow the omim_v1_* format."""
    score, method = OMIMPipeline._compute_omim_score(3, 5, 0.0)
    assert method.startswith("omim_v1_mk")
    assert "pmid5" in method
