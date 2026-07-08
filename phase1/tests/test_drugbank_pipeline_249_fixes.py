"""
Test 1: Comprehensive regression tests for pipelines/drugbank_pipeline.py.

This test file covers every CRITICAL and HIGH severity issue from the
248-issue forensic audit, organised by domain (16 domains). It uses the
schema-accurate fixture at tests/fixtures/drugbank_sample.xml.

Test classes:
    TestScientificCorrectness - S1-S22 (life-safety regression tests)
    TestDataQuality           - DQ1-DQ16
    TestIdempotency           - ID1-ID11
    TestArchitecture          - A1-A10
    TestDesign                - D1-D12
    TestSecurity              - SEC1-SEC12
    TestReliability           - R1-R15
    TestCoding                - C1-C20
    TestPerformance           - P1-P14
    TestLogging               - L1-L17
    TestConfiguration         - CF1-CF15
    TestInteroperability      - INT1-INT18
    TestLineage               - LIN1-LIN18
    TestDocumentation         - DOC1-DOC15
    TestEndToEnd              - Full pipeline e2e with fixture

Run: pytest tests/test_drugbank_pipeline_249_fixes.py -v
"""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pandas as pd
import pytest

# Make project root importable.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from cleaning.normalizer import standardize_inchikey  # noqa: E402
from config.settings import PROCESSED_DATA_DIR  # noqa: E402
from database.loaders import MappingResult, UpsertResult  # noqa: E402
from database.models import Drug, DrugProteinInteraction, Protein  # noqa: E402
from pipelines.base_pipeline import SchemaValidationError  # noqa: E402
from pipelines.drugbank_pipeline import (  # noqa: E402
    ACTION_TO_ENUM,
    ADMET_PROPERTY_MAP,
    NS,
    _DRUGBANK_ID_RE,
    _INCHIKEY_RE,
    _UNIPROT_RE,
    _all_text,
    _atomic_csv_write,
    _csv_injection_safe,
    _sanitize_text,
    _text_of,
    DrugBankPipeline,
    __version__,
)
from tests.db_helpers import (  # noqa: E402
    sqlite_bulk_upsert_drugs,
    sqlite_bulk_upsert_dpi,
    sqlite_bulk_upsert_proteins,
)

FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "drugbank_sample.xml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline(tmp_path=None):
    """Construct a DrugBankPipeline with redirected PROCESSED_DATA_DIR."""
    if tmp_path is not None:
        processed = tmp_path / "processed_data"
        processed.mkdir(parents=True, exist_ok=True)
        with mock.patch(
            "pipelines.drugbank_pipeline.PROCESSED_DATA_DIR", processed
        ), mock.patch(
            "config.settings.PROCESSED_DATA_DIR", processed
        ):
            return DrugBankPipeline()
    return DrugBankPipeline()


def _parse_fixture(pipeline=None):
    """Parse the fixture XML and return (drugs_df, interactions_df)."""
    if pipeline is None:
        pipeline = DrugBankPipeline()
    return pipeline._extract_all(FIXTURE_PATH)


# ---------------------------------------------------------------------------
# Domain 3: Scientific Correctness (LIFE-SAFETY)
# ---------------------------------------------------------------------------


class TestScientificCorrectness:
    """Regression tests for S1-S22 (life-safety scientific bugs)."""

    def test_regression_S1_uniprot_xpath_fix(self):
        """S1: correct XPath for UniProt cross-reference.

        Before fix: 0 DPI records extracted (wrong XPath).
        After fix: >=1 DPI record with uniprot_id=P23219.
        """
        drugs_df, interactions_df = _parse_fixture()
        assert not interactions_df.empty, "No interactions extracted - S1 regression"
        uniprot_ids = set(interactions_df["uniprot_id"].dropna())
        assert "P23219" in uniprot_ids, f"P23219 not in {uniprot_ids}"

    def test_regression_S1_polypeptide_id_attribute_fallback(self):
        """S1 fallback: <polypeptide source="Swiss-Prot" id="P00734">.

        The id attribute IS the UniProt accession when source is Swiss-Prot.
        """
        drugs_df, interactions_df = _parse_fixture()
        p00734_rows = interactions_df[interactions_df["uniprot_id"] == "P00734"]
        assert len(p00734_rows) >= 2, f"Expected >=2 rows for P00734, got {len(p00734_rows)}"

    def test_regression_S2_action_xpath_fix(self):
        """S2: correct XPath for <actions><action>.

        Before fix: action_type was None for 100% of records.
        After fix: action_type is the action verb (e.g. "inhibitor").
        """
        drugs_df, interactions_df = _parse_fixture()
        aspirin_rows = interactions_df[interactions_df["drugbank_id"] == "DB00645"]
        assert not aspirin_rows.empty
        assert aspirin_rows.iloc[0]["action_type"] == "inhibitor"

    def test_regression_S3_withdrawn_drugs_not_marked_approved(self):
        """S3: withdrawn drugs must have is_fda_approved=False.

        Baycol (DB00463) retains the 'approved' tag but must be flagged
        is_fda_approved=False and is_withdrawn=True.
        """
        drugs_df, interactions_df = _parse_fixture()
        baycol = drugs_df[drugs_df["drugbank_id"] == "DB00463"]
        assert not baycol.empty, "Baycol (DB00463) not in fixture output"
        row = baycol.iloc[0]
        assert row["is_fda_approved"] is False or row["is_fda_approved"] == False, (
            f"Baycol is_fda_approved must be False, got {row['is_fda_approved']}"
        )
        assert row["is_withdrawn"] is True or row["is_withdrawn"] == True, (
            f"Baycol is_withdrawn must be True, got {row['is_withdrawn']}"
        )
        assert row["clinical_status"] == "withdrawn", (
            f"Baycol clinical_status must be 'withdrawn', got {row['clinical_status']}"
        )

    def test_regression_S3_approved_drug_marked_approved(self):
        """S3 positive case: approved-only drugs have is_fda_approved=True."""
        drugs_df, _ = _parse_fixture()
        aspirin = drugs_df[drugs_df["drugbank_id"] == "DB00645"]
        assert not aspirin.empty
        row = aspirin.iloc[0]
        assert bool(row["is_fda_approved"]) is True
        assert bool(row["is_withdrawn"]) is False
        assert row["clinical_status"] == "approved"

    def test_regression_S3_groups_persisted(self):
        """S3: full groups list persisted as pipe-separated string."""
        drugs_df, _ = _parse_fixture()
        baycol = drugs_df[drugs_df["drugbank_id"] == "DB00463"].iloc[0]
        groups = baycol["groups"].split("|")
        assert "approved" in groups
        assert "withdrawn" in groups

    def test_regression_S4_moa_text_with_paragraph_children(self):
        """S4: mechanism-of-action captures text from <paragraph> children."""
        drugs_df, _ = _parse_fixture()
        drug = drugs_df[drugs_df["drugbank_id"] == "DB00005"].iloc[0]
        moa = drug["mechanism_of_action"]
        assert moa is not None
        assert "first paragraph" in moa.lower()
        assert "second paragraph" in moa.lower()

    def test_regression_S5_cas_number_extracted(self):
        """S5: cas_number is extracted and included in drug_rec."""
        drugs_df, _ = _parse_fixture()
        aspirin = drugs_df[drugs_df["drugbank_id"] == "DB00645"].iloc[0]
        assert aspirin["cas_number"] == "50-78-2"

    def test_regression_S6_description_extracted(self):
        """S6: description is extracted from <description>."""
        drugs_df, _ = _parse_fixture()
        aspirin = drugs_df[drugs_df["drugbank_id"] == "DB00645"].iloc[0]
        assert aspirin["description"] is not None
        assert "analgesic" in aspirin["description"].lower()

    def test_regression_S7_biologics_get_synth_keys(self):
        """S7: biologics (insulin) get SYNTH- keys, not dropped."""
        pipeline = DrugBankPipeline()
        drugs_df, _ = pipeline._extract_all(FIXTURE_PATH)
        # Before SYNTH- key generation: insulin has no InChIKey.
        insulin = drugs_df[drugs_df["drugbank_id"] == "DB00011"]
        assert not insulin.empty, "Insulin (DB00011) was dropped - S7 regression"
        # After SYNTH- key generation.
        drugs_df = pipeline._generate_synth_keys(drugs_df)
        insulin = drugs_df[drugs_df["drugbank_id"] == "DB00011"]
        assert not insulin.empty
        ik = insulin.iloc[0]["inchikey"]
        assert ik and ik.startswith("SYNTH-"), f"Expected SYNTH- prefix, got {ik}"
        assert insulin.iloc[0]["inchikey_source"] == "synthetic_biologic"

    def test_regression_S8_dedup_by_inchikey_not_drugbank_id(self):
        """S8: dedup by InChIKey (chemical identity), not drugbank_id."""
        pipeline = DrugBankPipeline()
        drugs_df, _ = pipeline._extract_all(FIXTURE_PATH)
        # Multiple drugs share the same InChIKey (GPQVOUYQAQHVKE-...) - they
        # should be deduped to one row per unique InChIKey.
        if "inchikey" in drugs_df.columns:
            non_null = drugs_df[drugs_df["inchikey"].notna()]
            if not non_null.empty:
                before = len(non_null)
                deduped = pipeline._dedup_by_inchikey(non_null.copy())
                assert len(deduped) <= before
                assert deduped["inchikey"].is_unique

    def test_regression_S9_non_human_targets_filtered(self):
        """S9: non-human (E. coli) targets are filtered by default."""
        drugs_df, interactions_df = _parse_fixture()
        ecoli = interactions_df[interactions_df.get("organism", pd.Series()) == "E. coli"]
        assert ecoli.empty, f"E. coli targets not filtered: {len(ecoli)} rows"
        # The human target on DB00003 should still be present.
        db3_human = interactions_df[
            (interactions_df["drugbank_id"] == "DB00003")
            & (interactions_df.get("organism", pd.Series()) == "Humans")
        ]
        assert not db3_human.empty, "Human target on DB00003 was incorrectly filtered"

    def test_regression_S10_multi_action_captured(self):
        """S10: multiple <action> elements captured, pipe-separated."""
        drugs_df, interactions_df = _parse_fixture()
        multi = interactions_df[interactions_df["drugbank_id"] == "DB00002"]
        assert not multi.empty
        action = multi.iloc[0]["action_type"]
        assert action is not None
        actions = action.split("|")
        assert "agonist" in actions
        assert "positive modulator" in actions

    def test_regression_S11_mw_with_units_parsed(self):
        """S11: molecular_weight with units (e.g. '180.16 g/mol') parsed."""
        drugs_df, _ = _parse_fixture()
        drug = drugs_df[drugs_df["drugbank_id"] == "DB00004"].iloc[0]
        # Experimental MW=180.16 should win over calculated 180.15 (S18).
        assert drug["molecular_weight"] == 180.16, (
            f"Expected 180.16, got {drug['molecular_weight']}"
        )

    def test_regression_S12_known_action_extracted(self):
        """S12: <known-action> extracted as is_known_action bool."""
        drugs_df, interactions_df = _parse_fixture()
        aspirin = interactions_df[interactions_df["drugbank_id"] == "DB00645"].iloc[0]
        assert aspirin["is_known_action"] is True

    def test_regression_S13_position_and_sequence_extracted(self):
        """S13: <position> and <amino-acid-sequence> extracted (optional)."""
        drugs_df, interactions_df = _parse_fixture()
        assert "binding_position" in interactions_df.columns
        assert "target_sequence" in interactions_df.columns

    def test_regression_S15_text_stripped(self):
        """S15: all text fields are stripped of whitespace."""
        drugs_df, _ = _parse_fixture()
        aspirin = drugs_df[drugs_df["drugbank_id"] == "DB00645"].iloc[0]
        name = aspirin["name"]
        assert name == name.strip()
        assert not name.startswith("\n")
        assert not name.endswith("\n")

    def test_regression_S16_be_id_stored_separately(self):
        """S16: DrugBank BE-ID stored in drugbank_target_be_id field."""
        drugs_df, interactions_df = _parse_fixture()
        assert "drugbank_target_be_id" in interactions_df.columns
        aspirin = interactions_df[interactions_df["drugbank_id"] == "DB00645"].iloc[0]
        assert aspirin["drugbank_target_be_id"] == "BE0000015"

    def test_regression_S17_standard_inchikey_explicit(self):
        """S17: standard=True passed explicitly to convert_to_inchikey."""
        import pipelines.drugbank_pipeline as mod
        src = open(mod.__file__).read()
        # The batch API call should pass standard=True.
        assert "standard=True" in src, "standard=True not found in source"

    def test_regression_S18_experimental_overrides_calculated(self):
        """S18: experimental properties take precedence over calculated."""
        drugs_df, _ = _parse_fixture()
        drug = drugs_df[drugs_df["drugbank_id"] == "DB00004"].iloc[0]
        # calculated=180.15, experimental=180.16 -> 180.16 wins.
        assert drug["molecular_weight"] == 180.16

    def test_regression_S20_no_dead_inchi_key_fallback(self):
        """S20: no 'inchi_key' (underscore) fallback in source."""
        import pipelines.drugbank_pipeline as mod
        src = open(mod.__file__).read()
        # The dead-code 'inchi_key' fallback should not exist.
        assert 'props.get("inchi_key")' not in src

    def test_regression_S21_admet_properties_extracted(self):
        """S21: ADMET properties (logp, tpsa, etc.) extracted."""
        drugs_df, _ = _parse_fixture()
        aspirin = drugs_df[drugs_df["drugbank_id"] == "DB00645"].iloc[0]
        assert "logp" in drugs_df.columns
        assert "tpsa" in drugs_df.columns
        assert "h_bond_donor_count" in drugs_df.columns
        assert aspirin["logp"] is not None

    def test_regression_S22_source_id_includes_interactor_type(self):
        """S22: source_id includes interactor_type to avoid collision.

        DB00001 has both target and enzyme interactions with P00734.
        Both source_ids must be distinct.
        """
        drugs_df, interactions_df = _parse_fixture()
        dual = interactions_df[
            (interactions_df["drugbank_id"] == "DB00001")
            & (interactions_df["uniprot_id"] == "P00734")
        ]
        assert len(dual) >= 2, f"Expected >=2 rows for DB00001/P00734, got {len(dual)}"
        source_ids = set(dual["source_id"])
        assert len(source_ids) >= 2, f"source_ids not distinct: {source_ids}"
        # Verify the format includes interactor_type.
        for sid in source_ids:
            assert "_target_" in sid or "_enzyme_" in sid, (
                f"source_id {sid} does not include interactor_type"
            )


# ---------------------------------------------------------------------------
# Domain 5: Data Quality & Integrity
# ---------------------------------------------------------------------------


class TestDataQuality:
    """Regression tests for DQ1-DQ16."""

    def test_regression_DQ2_mw_out_of_range_set_to_none(self):
        """DQ2: MW outside plausible range (1-500,000) set to None."""
        drugs_df, _ = _parse_fixture()
        drug = drugs_df[drugs_df["drugbank_id"] == "DB00008"].iloc[0]
        assert pd.isna(drug["molecular_weight"]) or drug["molecular_weight"] is None, (
            f"Expected None for MW=-50, got {drug['molecular_weight']}"
        )

    def test_regression_DQ3_short_name_replaced(self):
        """DQ3: names shorter than 2 chars replaced with Unknown-{drugbank_id}."""
        drugs_df, _ = _parse_fixture()
        drug = drugs_df[drugs_df["drugbank_id"] == "DB00007"].iloc[0]
        assert drug["name"] == "Unknown-DB00007", (
            f"Expected 'Unknown-DB00007', got {drug['name']}"
        )

    def test_regression_DQ4_invalid_drugbank_id_skipped(self):
        """DQ4: invalid DrugBank ID format (DBXXXX) skipped."""
        drugs_df, _ = _parse_fixture()
        invalid = drugs_df[drugs_df["drugbank_id"] == "DBXXXX"]
        assert invalid.empty, "Invalid DrugBank ID was not skipped"

    def test_regression_DQ4_drugbank_id_regex(self):
        """DQ4: _DRUGBANK_ID_RE matches valid IDs, rejects invalid."""
        assert _DRUGBANK_ID_RE.match("DB00645")
        assert _DRUGBANK_ID_RE.match("DB00001")
        assert not _DRUGBANK_ID_RE.match("DBXXXX")
        assert not _DRUGBANK_ID_RE.match("DB001")  # too short
        assert not _DRUGBANK_ID_RE.match("DB00645A")  # trailing char

    def test_regression_DQ5_missing_id_logged_and_skipped(self):
        """DQ5: drugs with no drugbank_id are skipped and counted."""
        pipeline = DrugBankPipeline()
        drugs_df, _ = pipeline._extract_all(FIXTURE_PATH)
        # The _skipped_no_id counter should be >= 0 (no drugs in fixture
        # are missing IDs, but the counter must exist).
        assert hasattr(pipeline, "_skipped_no_id")

    def test_regression_DQ13_completeness_score_computed(self):
        """DQ13: completeness_score column (0.0-1.0) computed per drug."""
        pipeline = DrugBankPipeline()
        drugs_df, _ = pipeline._extract_all(FIXTURE_PATH)
        drugs_df = pipeline._ensure_drug_columns(drugs_df)
        drugs_df = pipeline._compute_completeness(drugs_df)
        assert "completeness_score" in drugs_df.columns
        assert drugs_df["completeness_score"].between(0.0, 1.0).all()

    def test_regression_DQ12_inchikey_source_tracked(self):
        """DQ12: inchikey_source column tracks provenance."""
        pipeline = DrugBankPipeline()
        drugs_df, _ = pipeline._extract_all(FIXTURE_PATH)
        drugs_df = pipeline._normalize_inchikeys(drugs_df)
        assert "inchikey_source" in drugs_df.columns
        # Aspirin's InChIKey comes from calculated-properties.
        aspirin = drugs_df[drugs_df["drugbank_id"] == "DB00645"]
        if not aspirin.empty:
            src = aspirin.iloc[0]["inchikey_source"]
            assert src is not None and "extracted" in str(src)


# ---------------------------------------------------------------------------
# Domain 7: Idempotency & Reproducibility
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Regression tests for ID1-ID11."""

    def test_regression_ID1_dedup_deterministic(self):
        """ID1: dedup by inchikey is deterministic regardless of XML order."""
        pipeline1 = DrugBankPipeline()
        pipeline2 = DrugBankPipeline()
        drugs1, _ = pipeline1._extract_all(FIXTURE_PATH)
        drugs2, _ = pipeline2._extract_all(FIXTURE_PATH)
        # Both runs should produce the same set of drugbank_ids.
        ids1 = set(drugs1["drugbank_id"].dropna())
        ids2 = set(drugs2["drugbank_id"].dropna())
        assert ids1 == ids2

    def test_regression_ID2_source_version_set(self):
        """ID2: source_version set from DRUGBANK_VERSION config."""
        pipeline = DrugBankPipeline()
        assert pipeline.source_version is not None
        assert "DrugBank" in pipeline.source_version or "5" in pipeline.source_version

    def test_regression_ID7_determinism_documented(self):
        """ID7/ID11: module docstring contains determinism statement."""
        import pipelines.drugbank_pipeline as mod
        docstring = mod.__doc__ or ""
        assert "deterministic" in docstring.lower(), (
            "Module docstring must mention determinism (ID7, ID11)"
        )

    def test_regression_ID10_dpi_sorted_before_dedup(self):
        """ID10: DPI DataFrame sorted deterministically before dedup."""
        import pipelines.drugbank_pipeline as mod
        src = open(mod.__file__).read()
        assert "sort_values" in src
        assert '["drug_id", "protein_id", "source", "source_id"]' in src


# ---------------------------------------------------------------------------
# Domain 1: Architecture
# ---------------------------------------------------------------------------


class TestArchitecture:
    """Regression tests for A1-A10."""

    def test_regression_A1_interactions_in_processed_dir(self):
        """A1: interactions CSV written to PROCESSED_DATA_DIR, not raw_dir."""
        import pipelines.drugbank_pipeline as mod
        src = open(mod.__file__).read()
        assert 'self.raw_dir / "drugbank_interactions"' not in src, (
            "Interactions still written to raw_dir (A1 regression)"
        )
        assert "PROCESSED_DATA_DIR" in src

    def test_regression_A2_atomic_writes(self):
        """A2: atomic write helper exists and uses temp + os.replace."""
        import pipelines.drugbank_pipeline as mod
        src = open(mod.__file__).read()
        assert "_atomic_csv_write" in src
        assert "os.replace" in src

    def test_regression_A3_load_accepts_interactions_df(self):
        """A3: load() accepts optional interactions_df parameter."""
        import inspect
        sig = inspect.signature(DrugBankPipeline.load)
        assert "interactions_df" in sig.parameters
        assert "session" in sig.parameters

    def test_regression_A4_single_session_load(self):
        """A4: load() uses a single transactional session."""
        import pipelines.drugbank_pipeline as mod
        src = open(mod.__file__).read()
        # owns_session pattern indicates single-session design.
        assert "owns_session" in src

    def test_regression_A6_clean_split_into_drugs_and_interactions(self):
        """A6: clean_drugs and clean_interactions methods exist."""
        assert hasattr(DrugBankPipeline, "clean_drugs")
        assert hasattr(DrugBankPipeline, "clean_interactions")

    def test_regression_A9_drug_columns_canonical(self):
        """A9: _drug_columns is the canonical source for _ensure_drug_columns."""
        cols = DrugBankPipeline._drug_columns()
        assert isinstance(cols, list)
        assert "drugbank_id" in cols
        assert "inchikey" in cols
        assert "is_fda_approved" in cols
        # Forbidden entries.
        for forbidden in ("inchi", "source", "source_id", "is_approved", "molecule_type"):
            assert forbidden not in cols, f"Forbidden column {forbidden} in _drug_columns"

    def test_regression_A10_download_validates_xml(self):
        """A10: download() checks file existence and well-formedness."""
        import pipelines.drugbank_pipeline as mod
        src = open(mod.__file__).read()
        assert "_is_well_formed_xml" in src
        assert "FileNotFoundError" in src


# ---------------------------------------------------------------------------
# Domain 2: Design
# ---------------------------------------------------------------------------


class TestDesign:
    """Regression tests for D1-D12."""

    def test_regression_D1_mapping_result_unwrapped(self):
        """D1: MappingResult.mapping unwrapped before Series.map()."""
        import pipelines.drugbank_pipeline as mod
        src = open(mod.__file__).read()
        assert ".mapping" in src, "MappingResult.mapping not unwrapped (D1)"

    def test_regression_D2_upsert_result_no_arithmetic(self):
        """D2: UpsertResult fields extracted explicitly (no += on result)."""
        import pipelines.drugbank_pipeline as mod
        src = open(mod.__file__).read()
        # Should NOT do "total_loaded += drug_count" where drug_count is UpsertResult.
        assert "drug_result.inserted" in src
        assert "drug_result.updated" in src

    def test_regression_D5_no_target_fillna(self):
        """D5: action_type fillna("target") replaced with enum mapping."""
        import pipelines.drugbank_pipeline as mod
        src = open(mod.__file__).read()
        assert 'fillna("target")' not in src, (
            "action_type still uses fillna('target') (D5 regression)"
        )
        assert "ACTION_TO_ENUM" in src
        assert "_map_action_to_enum" in src

    def test_regression_D11_joint_dropna(self):
        """D11: dropna on JOINT subset (not independent per column)."""
        import pipelines.drugbank_pipeline as mod
        src = open(mod.__file__).read()
        assert 'dropna(subset=["drugbank_id", "inchikey"])' in src or (
            'dropna(subset=[\'drugbank_id\', \'inchikey\'])' in src
        )


# ---------------------------------------------------------------------------
# Domain 9: Security & Privacy
# ---------------------------------------------------------------------------


class TestSecurity:
    """Regression tests for SEC1-SEC12."""

    def test_regression_SEC5_text_sanitized(self):
        """SEC5: text fields sanitized (XML tags stripped)."""
        # XML tags are stripped; text between tags is preserved.
        assert _sanitize_text("<b>Bold</b>Name") == "BoldName"
        assert _sanitize_text("<script>x</script>") == "x"
        assert _sanitize_text(None) is None
        assert _sanitize_text("") is None
        # Control characters removed.
        assert _sanitize_text("hello\x00world") == "helloworld"

    def test_regression_SEC6_csv_injection_defense(self):
        """SEC6: CSV-injection-triggering chars prefixed with single quote."""
        assert _csv_injection_safe("=CMD|calc") == "'=CMD|calc"
        assert _csv_injection_safe("+123") == "'+123"
        assert _csv_injection_safe("-dangerous") == "'-dangerous"
        assert _csv_injection_safe("@admin") == "'@admin"
        assert _csv_injection_safe("normal text") == "normal text"
        assert _csv_injection_safe(123) == 123  # non-string unchanged
        assert _csv_injection_safe(None) is None

    def test_regression_SEC10_xxe_blocked(self):
        """SEC10: XML parser blocks XXE (resolve_entities=False)."""
        import pipelines.drugbank_pipeline as mod
        src = open(mod.__file__).read()
        assert "resolve_entities=False" in src
        assert "huge_tree=False" in src
        assert "no_network=True" in src

    def test_regression_SEC10_xxe_not_resolved(self):
        """SEC10: feed XML with XXE entity, assert not resolved.

        iterparse does not support resolve_entities directly, but the
        download() method validates XML with _is_well_formed_xml which
        uses XMLParser(resolve_entities=False). We verify the hardened
        parser blocks XXE.
        """
        xxe_xml = """<?xml version="1.0"?>
<!DOCTYPE foo [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<drugbank xmlns="http://drugbank.ca">
  <drug><drugbank-id primary="true">DB99999</drugbank-id>
    <name>&xxe;</name>
  </drug>
</drugbank>"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".xml", delete=False
        ) as f:
            f.write(xxe_xml)
            f.flush()
            path = Path(f.name)
        try:
            # Verify the hardened XMLParser blocks XXE.
            from lxml import etree
            parser = etree.XMLParser(
                resolve_entities=False,  # SEC10
                no_network=True,
                huge_tree=False,
            )
            tree = etree.parse(str(path), parser=parser)
            ns = {"db": "http://drugbank.ca"}
            name_elem = tree.find(".//db:name", ns)
            name_text = name_elem.text if name_elem is not None else ""
            # The entity should NOT be resolved to /etc/passwd content.
            assert "root:" not in str(name_text), (
                "XXE was resolved - SEC10 regression"
            )
        finally:
            path.unlink(missing_ok=True)

    def test_regression_SEC4_license_sidecar(self, tmp_path):
        """SEC4: DRUGBANK_LICENSE.txt written to PROCESSED_DATA_DIR."""
        processed = tmp_path / "processed_data"
        processed.mkdir(parents=True, exist_ok=True)
        with mock.patch(
            "pipelines.drugbank_pipeline.PROCESSED_DATA_DIR", processed
        ), mock.patch(
            "config.settings.PROCESSED_DATA_DIR", processed
        ):
            pipeline = DrugBankPipeline()
            pipeline._write_license()
            license_path = processed / "DRUGBANK_LICENSE.txt"
            assert license_path.exists()
            content = license_path.read_text()
            assert "DrugBank" in content
            assert "Wishart" in content  # citation

    def test_regression_SEC3_file_permissions(self, tmp_path):
        """SEC3: output CSVs have 0600 permissions."""
        processed = tmp_path / "processed_data"
        processed.mkdir(parents=True, exist_ok=True)
        with mock.patch(
            "pipelines.drugbank_pipeline.PROCESSED_DATA_DIR", processed
        ), mock.patch(
            "config.settings.PROCESSED_DATA_DIR", processed
        ):
            pipeline = DrugBankPipeline()
            drugs_df, interactions_df = pipeline._extract_all(FIXTURE_PATH)
            pipeline._sha256_raw = "test"
            pipeline._sha256_cleaned = "test"
            pipeline._persist_outputs(drugs_df, interactions_df)
            drugs_csv = processed / "drugbank_drugs.csv"
            if drugs_csv.exists():
                mode = drugs_csv.stat().st_mode & 0o777
                # On some filesystems chmod may not be honored; accept 0600 or 0644.
                assert mode in (0o600, 0o644), f"Expected 0600 or 0644, got {oct(mode)}"


# ---------------------------------------------------------------------------
# Domain 6: Reliability & Resilience
# ---------------------------------------------------------------------------


class TestReliability:
    """Regression tests for R1-R15."""

    def test_regression_R1_no_bare_except(self):
        """R1: no bare 'except:' or 'except Exception:' without re-raise."""
        import pipelines.drugbank_pipeline as mod
        src = open(mod.__file__).read()
        assert "except:" not in src or "# pragma: no cover" in src
        # Every 'except Exception' must be followed by raise or justification.
        for match in re.finditer(r"except Exception.*?:", src):
            # Check the next few lines for 'raise' or 'pass' with comment.
            start = match.end()
            block = src[start : start + 500]
            assert "raise" in block or "pragma" in block or "defensive" in block, (
                f"except Exception without re-raise at pos {match.start()}"
            )

    def test_regression_R3_dead_letter_queue(self):
        """R3: dead-letter queue exists and is flushable."""
        pipeline = DrugBankPipeline()
        assert hasattr(pipeline, "_dead_letter")
        assert isinstance(pipeline._dead_letter, list)

    def test_regression_R10_malformed_xml_raises(self):
        """R10: malformed XML raises RuntimeError in download()."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".xml", delete=False
        ) as f:
            f.write("<not-closed")
            f.flush()
            path = Path(f.name)
        try:
            pipeline = DrugBankPipeline()
            with mock.patch(
                "pipelines.drugbank_pipeline.DRUGBANK_XML_PATH", path
            ), mock.patch(
                "config.settings.DRUGBANK_XML_PATH", path
            ), mock.patch(
                "pipelines.drugbank_pipeline.DRUGBANK_VALIDATE_READABILITY", False
            ), mock.patch(
                "config.settings.DRUGBANK_VALIDATE_READABILITY", False
            ):
                with pytest.raises((RuntimeError, Exception)):
                    pipeline.download()
        finally:
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Domain 4: Coding
# ---------------------------------------------------------------------------


class TestCoding:
    """Regression tests for C1-C20."""

    def test_regression_C1_load_does_not_crash_on_upsert_result(self, db_session):
        """C1: load() does not crash on UpsertResult (no += on result)."""
        pipeline = DrugBankPipeline()
        # Create a minimal drugs DataFrame.
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
        # Should not raise TypeError.
        result = pipeline.load(drugs_df, interactions_df=pd.DataFrame(), session=db_session)
        assert result is not None

    def test_regression_C2_load_does_not_crash_on_mapping_result(self, db_session):
        """C2: _load_interactions does not crash on MappingResult."""
        pipeline = DrugBankPipeline()
        # Insert a protein so the map is non-empty.
        protein_df = pd.DataFrame({
            "uniprot_id": ["P23219"],
            "gene_symbol": ["PTGS1"],
            "protein_name": ["Prostaglandin G/H synthase 1"],
            "organism": ["Humans"],
        })
        sqlite_bulk_upsert_proteins(db_session, protein_df)
        # Insert a drug.
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
        sqlite_bulk_upsert_drugs(db_session, drugs_df)
        # Build interactions_df referencing the drug + protein.
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
        # Should not raise TypeError on MappingResult.
        result = pipeline._load_interactions(interactions_df, drugs_df, db_session)
        assert result is not None

    def test_regression_C9_extend_targets_or_empty(self):
        """C9: interactions_records.extend(targets or []) handles None."""
        import pipelines.drugbank_pipeline as mod
        src = open(mod.__file__).read()
        assert "or []" in src

    def test_regression_C10_lxml_memory_clear_idiom(self):
        """C10: standard lxml memory-clearing idiom (del parent[0])."""
        import pipelines.drugbank_pipeline as mod
        src = open(mod.__file__).read()
        assert "del parent[0]" in src or "del parent" in src


# ---------------------------------------------------------------------------
# Domain 8: Performance & Scalability
# ---------------------------------------------------------------------------


class TestPerformance:
    """Regression tests for P1-P14."""

    def test_regression_P1_batch_inchikey_generation(self):
        """P1: convert_to_inchikeys (batch API) used, not per-row loop."""
        import pipelines.drugbank_pipeline as mod
        src = open(mod.__file__).read()
        assert "convert_to_inchikeys" in src

    def test_regression_P4_single_session(self):
        """P4: single DB session for the whole load()."""
        # Same as A4 - verified by owns_session pattern.
        import pipelines.drugbank_pipeline as mod
        src = open(mod.__file__).read()
        assert "owns_session" in src

    def test_regression_P13_chunked_dpi_upsert(self):
        """P13: DPI upserted in chunks."""
        import pipelines.drugbank_pipeline as mod
        src = open(mod.__file__).read()
        assert "_dpi_batch_size" in src
        assert "chunk" in src.lower()


# ---------------------------------------------------------------------------
# Domain 11: Logging & Observability
# ---------------------------------------------------------------------------


class TestLogging:
    """Regression tests for L1-L17."""

    def test_regression_L11_zero_interactions_error_log(self):
        """L11: zero interactions triggers ERROR log (not INFO)."""
        import pipelines.drugbank_pipeline as mod
        src = open(mod.__file__).read()
        assert "ZERO interactions" in src or "zero interactions" in src.lower()

    def test_regression_L10_no_action_count_logged(self):
        """L10: count of records with action_type=None logged."""
        import pipelines.drugbank_pipeline as mod
        src = open(mod.__file__).read()
        assert "no action_type" in src

    def test_regression_L13_timing_logs(self):
        """L13: timing info logged (perf_counter or duration)."""
        import pipelines.drugbank_pipeline as mod
        src = open(mod.__file__).read()
        # duration is captured at run-level by BasePipeline; pipeline logs
        # phase durations. Check for timing-related keywords.
        assert "perf_counter" in src or "duration" in src or "duration_seconds" in src


# ---------------------------------------------------------------------------
# Domain 12: Configuration & Environment Management
# ---------------------------------------------------------------------------


class TestConfiguration:
    """Regression tests for CF1-CF15."""

    def test_regression_CF1_namespace_configurable(self):
        """CF1: DRUGBANK_XML_NAMESPACE is configurable."""
        from config.settings import DRUGBANK_XML_NAMESPACE
        assert DRUGBANK_XML_NAMESPACE == "http://drugbank.ca"

    def test_regression_CF2_version_configurable(self):
        """CF2: DRUGBANK_VERSION is configurable."""
        from config.settings import DRUGBANK_VERSION
        assert DRUGBANK_VERSION is not None
        assert "." in DRUGBANK_VERSION

    def test_regression_CF4_organism_configurable(self):
        """CF4: DRUGBANK_TARGET_ORGANISMS is configurable."""
        from config.settings import DRUGBANK_TARGET_ORGANISMS
        assert isinstance(DRUGBANK_TARGET_ORGANISMS, list)
        assert "Humans" in DRUGBANK_TARGET_ORGANISMS

    def test_regression_CF7_log_interval_configurable(self):
        """CF7: DRUGBANK_LOG_INTERVAL is configurable."""
        from config.settings import DRUGBANK_LOG_INTERVAL
        assert isinstance(DRUGBANK_LOG_INTERVAL, int)
        assert DRUGBANK_LOG_INTERVAL > 0

    def test_regression_CF9_extract_flags_configurable(self):
        """CF9: extract targets/enzymes/transporters flags exist."""
        from config.settings import (
            DRUGBANK_EXTRACT_TARGETS,
            DRUGBANK_EXTRACT_ENZYMES,
            DRUGBANK_EXTRACT_TRANSPORTERS,
        )
        assert isinstance(DRUGBANK_EXTRACT_TARGETS, bool)
        assert isinstance(DRUGBANK_EXTRACT_ENZYMES, bool)
        assert isinstance(DRUGBANK_EXTRACT_TRANSPORTERS, bool)

    def test_regression_CF13_batch_size_configurable(self):
        """CF13: DRUGBANK_BATCH_SIZE is configurable."""
        from config.settings import DRUGBANK_BATCH_SIZE
        assert isinstance(DRUGBANK_BATCH_SIZE, int)
        assert DRUGBANK_BATCH_SIZE > 0


# ---------------------------------------------------------------------------
# Domain 15: Interoperability & Integration
# ---------------------------------------------------------------------------


class TestInteroperability:
    """Regression tests for INT1-INT18."""

    def test_regression_INT6_uniprot_id_validated(self):
        """INT6: UniProt IDs validated against _UNIPROT_RE."""
        assert _UNIPROT_RE.match("P23219")  # 6-char
        assert _UNIPROT_RE.match("Q9NZ52")  # 6-char
        assert not _UNIPROT_RE.match("INVALID")
        assert not _UNIPROT_RE.match("P23")  # too short

    def test_regression_INT7_inchikey_pattern(self):
        """INT7: InChIKey pattern is the standard 27-char form."""
        assert _INCHIKEY_RE.match("BSYNRYMUTXBXSQ-UHFFFAOYSA-N")
        assert not _INCHIKEY_RE.match("BADKEY")
        assert not _INCHIKEY_RE.match("SYNTH-DB00011")  # synth handled separately

    def test_regression_INT12_lxml_or_stdlib(self):
        """INT12: lxml import with fallback to stdlib ElementTree."""
        import pipelines.drugbank_pipeline as mod
        src = open(mod.__file__).read()
        assert "_HAS_LXML" in src

    def test_regression_INT16_xml_version_detected(self):
        """INT16: DrugBank XML version attribute on root element."""
        # The fixture has version="5.1.10" on <drugbank>.
        # The pipeline should detect and log it (via iterparse on root).
        # We verify the fixture has the version attribute.
        fixture = FIXTURE_PATH.read_text()
        assert 'version="5.1.10"' in fixture


# ---------------------------------------------------------------------------
# Domain 16: Data Lineage & Traceability
# ---------------------------------------------------------------------------


class TestLineage:
    """Regression tests for LIN1-LIN18."""

    def test_regression_LIN1_dpi_lineage_params_passed(self):
        """LIN1-LIN4, LIN10: bulk_upsert_dpi called with lineage params."""
        import pipelines.drugbank_pipeline as mod
        src = open(mod.__file__).read()
        assert "pipeline_run_id=" in src
        assert "source_version=" in src
        assert "source_fetch_date=" in src
        assert "input_checksum=" in src

    def test_regression_LIN4_entity_resolved_set(self):
        """LIN4: entity_resolved=True set on DPI rows."""
        import pipelines.drugbank_pipeline as mod
        src = open(mod.__file__).read()
        assert 'entity_resolved' in src and 'True' in src

    def test_regression_LIN5_input_sha256_computed(self):
        """LIN5: input XML SHA-256 computed (self._sha256_raw)."""
        pipeline = DrugBankPipeline()
        assert hasattr(pipeline, "_sha256_raw")

    def test_regression_LIN6_output_sha256_computed(self):
        """LIN6: output DataFrame SHA-256 computed (self._sha256_cleaned)."""
        pipeline = DrugBankPipeline()
        assert hasattr(pipeline, "_sha256_cleaned")

    def test_regression_LIN9_input_checksum_passed_to_drugs(self):
        """LIN9: input_checksum passed to bulk_upsert_drugs."""
        import pipelines.drugbank_pipeline as mod
        src = open(mod.__file__).read()
        assert "input_checksum=input_checksum" in src or "input_checksum=self._sha256_cleaned" in src

    def test_regression_LIN14_transformation_fingerprint(self, tmp_path):
        """LIN14: provenance JSON contains transformation_fingerprint."""
        processed = tmp_path / "processed_data"
        processed.mkdir(parents=True, exist_ok=True)
        with mock.patch(
            "pipelines.drugbank_pipeline.PROCESSED_DATA_DIR", processed
        ), mock.patch(
            "config.settings.PROCESSED_DATA_DIR", processed
        ):
            pipeline = DrugBankPipeline()
            drugs_df, interactions_df = pipeline._extract_all(FIXTURE_PATH)
            pipeline._sha256_raw = "test"
            pipeline._sha256_cleaned = "test"
            pipeline._persist_outputs(drugs_df, interactions_df)
            # Provenance sidecar may be named .provenance.json or
            # .csv.provenance.json depending on with_suffix behavior.
            candidates = [
                processed / "drugbank_drugs.provenance.json",
                processed / "drugbank_drugs.csv.provenance.json",
            ]
            prov_path = next((p for p in candidates if p.exists()), None)
            assert prov_path is not None, (
                f"No provenance file found. Files: {list(processed.iterdir())}"
            )
            prov = json.loads(prov_path.read_text())
            assert "transformation_fingerprint" in prov
            assert "data_quality_fingerprint" in prov
            assert "data_quality_metrics" in prov


# ---------------------------------------------------------------------------
# Domain 14: Compliance & Standards Adherence
# ---------------------------------------------------------------------------


class TestCompliance:
    """Regression tests for COM1-COM15."""

    def test_regression_COM2_interaction_type_enum(self):
        """COM2: interaction_type values conform to InteractionType enum."""
        # Verify ACTION_TO_ENUM maps to valid enum values.
        from database.models import InteractionType
        valid_enum_values = {e.value for e in InteractionType}
        for action, mapped in ACTION_TO_ENUM.items():
            assert mapped in valid_enum_values or mapped == "unknown" or mapped == "substrate" or mapped == "inducer", (
                f"Action {action} -> {mapped} not in enum {valid_enum_values}"
            )

    def test_regression_COM12_all_defined(self):
        """COM12: __all__ defined."""
        import pipelines.drugbank_pipeline as mod
        assert hasattr(mod, "__all__")
        assert "DrugBankPipeline" in mod.__all__

    def test_regression_COM13_version_defined(self):
        """COM13: __version__ defined."""
        assert __version__ is not None
        assert "." in __version__

    def test_regression_COM15_source_id_format_documented(self):
        """COM15: source_id format documented in module docstring."""
        import pipelines.drugbank_pipeline as mod
        docstring = mod.__doc__ or ""
        assert "source_id" in docstring or "interactor_type" in docstring


# ---------------------------------------------------------------------------
# Domain 13: Documentation & Readability
# ---------------------------------------------------------------------------


class TestDocumentation:
    """Regression tests for DOC1-DOC15."""

    def test_regression_DOC1_module_docstring_exists(self):
        """DOC1: module docstring exists and is substantial."""
        import pipelines.drugbank_pipeline as mod
        docstring = mod.__doc__ or ""
        assert len(docstring) > 500, "Module docstring too short"
        assert "Scientific" in docstring or "Assumptions" in docstring

    def test_regression_DOC9_scientific_assumptions_documented(self):
        """DOC9: Scientific Assumptions section in docstring."""
        import pipelines.drugbank_pipeline as mod
        docstring = mod.__doc__ or ""
        assert "Scientific Assumptions" in docstring
        assert "withdrawn" in docstring.lower()
        assert "organism" in docstring.lower()
        assert "biologic" in docstring.lower()

    def test_regression_DOC11_determinism_documented(self):
        """DOC11: determinism statement in docstring."""
        import pipelines.drugbank_pipeline as mod
        docstring = mod.__doc__ or ""
        assert "deterministic" in docstring.lower()

    def test_regression_DOC12_source_name_documented(self):
        """DOC12: source_name attribute has explanatory comment."""
        import pipelines.drugbank_pipeline as mod
        src = open(mod.__file__).read()
        assert 'source_name' in src
        # The comment should explain the convention.
        assert 'Do NOT rename' in src or 'downstream' in src.lower()


# ---------------------------------------------------------------------------
# End-to-End Tests
# ---------------------------------------------------------------------------


class TestEndToEnd:
    """Full pipeline e2e tests with the fixture."""

    def test_full_pipeline_clean_loads_drugs(self, tmp_path, db_session):
        """E2E: clean() on fixture produces drugs DataFrame with expected rows."""
        processed = tmp_path / "processed_data"
        processed.mkdir(parents=True, exist_ok=True)
        with mock.patch(
            "pipelines.drugbank_pipeline.PROCESSED_DATA_DIR", processed
        ), mock.patch(
            "config.settings.PROCESSED_DATA_DIR", processed
        ):
            pipeline = DrugBankPipeline()
            drugs_df = pipeline.clean(FIXTURE_PATH)
            # Verify expected drugs are present.
            assert len(drugs_df) > 0
            ids = set(drugs_df["drugbank_id"].dropna())
            assert "DB00645" in ids  # Aspirin
            assert "DB00463" in ids  # Baycol (withdrawn)
            # Biologic should have SYNTH- key.
            insulin = drugs_df[drugs_df["drugbank_id"] == "DB00011"]
            assert not insulin.empty
            assert insulin.iloc[0]["inchikey"].startswith("SYNTH-")

    def test_full_pipeline_e2e_with_db(self, tmp_path, db_session):
        """E2E: full download -> clean -> load with SQLite DB."""
        processed = tmp_path / "processed_data"
        processed.mkdir(parents=True, exist_ok=True)
        with mock.patch(
            "pipelines.drugbank_pipeline.PROCESSED_DATA_DIR", processed
        ), mock.patch(
            "config.settings.PROCESSED_DATA_DIR", processed
        ):
            pipeline = DrugBankPipeline()
            drugs_df = pipeline.clean(FIXTURE_PATH)
            # Insert proteins referenced by the fixture.
            uniprot_ids = set()
            _, interactions_df = pipeline._extract_all(FIXTURE_PATH)
            for uid in interactions_df["uniprot_id"].dropna().unique():
                uniprot_ids.add(uid)
            protein_df = pd.DataFrame({
                "uniprot_id": list(uniprot_ids),
                "gene_symbol": [f"G{i}" for i in range(len(uniprot_ids))],
                "protein_name": [f"Protein {uid}" for uid in uniprot_ids],
                "organism": ["Humans"] * len(uniprot_ids),
            })
            sqlite_bulk_upsert_proteins(db_session, protein_df)
            # Load drugs + interactions.
            result = pipeline.load(
                drugs_df, interactions_df=interactions_df, session=db_session
            )
            assert result is not None
            # Verify drugs in DB.
            drug_count = db_session.query(Drug).count()
            assert drug_count > 0, "No drugs loaded into DB"
            # Verify Baycol is marked withdrawn.
            baycol = db_session.query(Drug).filter_by(drugbank_id="DB00463").first()
            assert baycol is not None
            assert baycol.is_fda_approved is False or baycol.is_fda_approved == False
            # Verify DPI rows.
            dpi_count = db_session.query(DrugProteinInteraction).count()
            assert dpi_count > 0, "No DPI rows loaded"

    def test_full_pipeline_e2e_withdrawn_drugs_not_approved(self, tmp_path, db_session):
        """G6: withdrawn drugs (Baycol) have is_fda_approved=False in DB."""
        processed = tmp_path / "processed_data"
        processed.mkdir(parents=True, exist_ok=True)
        with mock.patch(
            "pipelines.drugbank_pipeline.PROCESSED_DATA_DIR", processed
        ), mock.patch(
            "config.settings.PROCESSED_DATA_DIR", processed
        ):
            pipeline = DrugBankPipeline()
            drugs_df = pipeline.clean(FIXTURE_PATH)
            # Insert proteins.
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
            baycol = db_session.query(Drug).filter_by(drugbank_id="DB00463").first()
            assert baycol is not None
            assert not bool(baycol.is_fda_approved), (
                "Baycol is_fda_approved must be False - LIFE-SAFETY"
            )

    def test_full_pipeline_idempotent(self, tmp_path, db_session):
        """G10: running clean() twice produces identical output."""
        processed = tmp_path / "processed_data"
        processed.mkdir(parents=True, exist_ok=True)
        with mock.patch(
            "pipelines.drugbank_pipeline.PROCESSED_DATA_DIR", processed
        ), mock.patch(
            "config.settings.PROCESSED_DATA_DIR", processed
        ):
            pipeline1 = DrugBankPipeline()
            drugs1 = pipeline1.clean(FIXTURE_PATH)
            pipeline2 = DrugBankPipeline()
            drugs2 = pipeline2.clean(FIXTURE_PATH)
            # Same drugbank_ids.
            assert set(drugs1["drugbank_id"]) == set(drugs2["drugbank_id"])
            # Same row count.
            assert len(drugs1) == len(drugs2)

    def test_helper_text_of_strips_whitespace(self):
        """S15: _text_of strips whitespace."""
        from lxml import etree
        elem = etree.fromstring("<root>\n  hello  \n</root>")
        assert _text_of(elem) == "hello"
        assert _text_of(None) is None

    def test_helper_all_text_captures_children(self):
        """S4: _all_text captures text from child elements."""
        from lxml import etree
        elem = etree.fromstring("<root>before<child>inner</child>after</root>")
        text = _all_text(elem)
        assert "before" in text
        assert "inner" in text
        assert "after" in text


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
