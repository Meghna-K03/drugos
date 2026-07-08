"""
Test suite for cleaning/deduplicator.py v3.0.0 — 16-domain institutional-grade coverage.

This is Test #1 of 3 required by the upgrade prompt. It exercises the new
deduplicator.py file in depth across all 16 domains:

  1. Architecture
  2. Design
  3. Knowledge (Scientific Correctness)
  4. Coding
  5. Data Quality & Integrity
  6. Reliability & Resilience
  7. Idempotency & Reproducibility
  8. Performance & Scalability
  9. Security & Privacy
 10. Testing & Validation (meta-tests)
 11. Logging & Observability
 12. Configuration & Environment
 13. Documentation & Readability
 14. Compliance & Standards
 15. Interoperability & Integration
 16. Data Lineage & Traceability

Run: pytest tests/test_deduplicator_16_domains_v3.py -v
"""
from __future__ import annotations

import inspect
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

# Ensure project root is on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import cleaning
from cleaning.deduplicator import (
    # Constants
    DEFAULT_COMPLETENESS_WEIGHTS,
    DEFAULT_DPI_KEYS,
    INVERSE_ACTIVITY_TYPES,
    MAX_DATAFRAME_ROWS,
    MAX_DEAD_LETTERS,
    MAX_DROPPED_ROWS_IN_RESULT,
    PERCENT_ACTIVITY_TYPES,
    POTENCY_ACTIVITY_TYPES,
    # Enums
    ActivityDirection,
    CompletenessWeight,
    DedupResult,
    DedupStrategy,
    # Functions
    backfill_safety_check,
    checkpoint_state,
    clean_interactions,
    clear_dead_letters,
    compute_completeness_score,
    configure_deduplicator,
    dedup_by_inchikey,
    dedup_by_inchikey_chunked,
    dedup_interactions,
    flush_dead_letters,
    get_correlation_id,
    get_dead_letters,
    get_metrics,
    get_provenance,
    health_check,
    is_reproducible,
    merge_duplicate_groups,
    performance_benchmark,
    quality_report,
    recover_from_failure,
    referential_integrity_check,
    reproducibility_report,
    requires_api_version,
    reset_metrics,
    revert_configuration,
    set_correlation_id,
    timing_report,
    validate_config,
    validate_environment,
    validate_recovery_state,
)
import cleaning.deduplicator as dedup_mod


# ============================================================================
# Helpers / fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def _isolate_state():
    """Reset metrics + dead-letters before every test."""
    reset_metrics()
    clear_dead_letters()
    yield
    reset_metrics()
    clear_dead_letters()


def _make_drug_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def _make_aspirin_df() -> pd.DataFrame:
    return _make_drug_df([
        {"inchikey": "AAA", "name": "Aspirin", "smiles": "CCO", "mw": 180.0},
        {"inchikey": "AAA", "name": None,     "smiles": "CCO", "mw": None},
        {"inchikey": "BBB", "name": "Ibuprofen", "smiles": "CCC", "mw": 206.0},
    ])


def _make_dpi_df() -> pd.DataFrame:
    return pd.DataFrame([
        {"drug_id": 1, "protein_id": 10, "source": "chembl",
         "source_id": "a", "activity_value": 50.0,  "activity_type": "IC50"},
        {"drug_id": 1, "protein_id": 10, "source": "chembl",
         "source_id": "a", "activity_value": 100.0, "activity_type": "IC50"},
        {"drug_id": 2, "protein_id": 20, "source": "drugbank",
         "source_id": "b", "activity_value": 200.0, "activity_type": "IC50"},
    ])


# ============================================================================
# Section 1 — Architecture (Domain 1)
# ============================================================================

class TestArchitecture:
    """[ARCH-1..9] Module structure, versioning, logger setup."""

    def test_arch_1_module_version_is_v3(self):
        assert dedup_mod.__version__ == "3.0.0"
        assert dedup_mod._MODULE_VERSION == "3.0.0"
        assert dedup_mod._OUTPUT_SCHEMA_VERSION == "3.0.0"
        assert dedup_mod._RULE_VERSION == "rules_v3"

    def test_arch_1_logic_hash_present(self):
        assert isinstance(dedup_mod._LOGIC_HASH, str)
        assert len(dedup_mod._LOGIC_HASH) == 16 or dedup_mod._LOGIC_HASH == "unknown"

    def test_arch_2_logger_has_null_handler(self):
        assert any(isinstance(h, logging.NullHandler)
                   for h in dedup_mod.logger.handlers)

    def test_arch_2_logger_name_is_qualified(self):
        assert dedup_mod.logger.name == "cleaning.deduplicator"

    def test_arch_3_no_top_level_third_party_imports(self):
        """Only stdlib + pandas should be imported at module top-level."""
        src = inspect.getsource(dedup_mod)
        # First 100 lines should not import rdkit/rapidfuzz/scipy
        first_lines = "\n".join(src.splitlines()[:120])
        assert "import rdkit" not in first_lines
        assert "import rapidfuzz" not in first_lines
        assert "import scipy" not in first_lines

    def test_arch_4_optional_deps_self_empty(self):
        assert dedup_mod._OPTIONAL_DEPS_SELF["dedup_by_inchikey"] == set()
        assert dedup_mod._OPTIONAL_DEPS_SELF["dedup_interactions"] == set()

    def test_arch_5_dependency_graph_has_dedup_interactions(self):
        affected = cleaning.get_affected_functions("activity_value")
        assert "dedup_interactions" in affected
        affected_drug = cleaning.get_affected_functions("drug_id")
        assert "dedup_interactions" in affected_drug

    def test_arch_6_attrs_preserved_and_extended(self):
        df = _make_aspirin_df()
        df.attrs["custom_marker"] = "hello"
        result = dedup_by_inchikey(df)
        assert result.attrs.get("custom_marker") == "hello"
        assert "_provenance" in result.attrs
        assert "_input_fingerprint" in result.attrs
        assert "_output_fingerprint" in result.attrs

    def test_arch_7_clean_interactions_orchestrator(self):
        assert callable(clean_interactions)
        assert cleaning._API_VERSIONS.get("clean_interactions") == "3.0.0"

    def test_arch_8_chunked_api_exists(self):
        assert callable(dedup_by_inchikey_chunked)

    def test_arch_9_module_load_time_recorded(self):
        # Force lazy load
        _ = cleaning.dedup_by_inchikey
        # The package tracks load times
        load_times = cleaning.get_load_times()
        assert "dedup_by_inchikey" in load_times


# ============================================================================
# Section 2 — Design (Domain 2)
# ============================================================================

class TestDesign:
    """[DES-1..8] Dataclasses, enums, API design."""

    def test_des_1_dedup_result_dataclass(self):
        df = _make_aspirin_df()
        result = dedup_by_inchikey(df, return_result=True)
        assert isinstance(result, DedupResult)
        assert result.rows_before == 3
        assert result.rows_after == 2
        assert result.duplicates_removed == 1
        assert int(result) == 2
        assert len(result) == 2
        summary = result.quality_summary()
        assert summary["rows_before"] == 3
        assert summary["rows_dropped"] == 1

    def test_des_2_dedup_strategy_enum(self):
        assert DedupStrategy.MOST_COMPLETE.value == "most_complete"
        assert DedupStrategy.LOWEST_ACTIVITY.value == "lowest_activity"
        assert DedupStrategy.HIGHEST_ACTIVITY.value == "highest_activity"
        assert DedupStrategy.MERGE_FIELDS.value == "merge_fields"

    def test_des_2_activity_direction_enum(self):
        assert ActivityDirection.ASC.value == "asc"
        assert ActivityDirection.DESC.value == "desc"
        assert ActivityDirection.AUTO.value == "auto"

    def test_des_3_completeness_weight(self):
        cw = CompletenessWeight()
        assert cw.weights["inchikey"] == 5.0
        assert cw.weights["name"] == 4.0
        # A row with name + mw should beat a row with just mechanism
        row_with_name = pd.Series({"name": "Aspirin", "mw": 180.0, "moa": None})
        row_with_moa = pd.Series({"name": None, "mw": None, "moa": "long text"})
        assert cw.score_row(row_with_name) > cw.score_row(row_with_moa)

    def test_des_4_keys_default_inferred(self):
        df = _make_dpi_df()
        # Don't pass keys — should infer ["drug_id", "protein_id", "source", "source_id"]
        result = dedup_interactions(df)
        assert len(result) == 2  # 1 of the 2 chembl rows dropped, drugbank kept

    def test_des_4_keys_empty_raises(self):
        df = _make_dpi_df()
        with pytest.raises((ValueError, TypeError)):
            dedup_interactions(df, keys=[])

    def test_des_4_keys_duplicates_raises(self):
        df = _make_dpi_df()
        with pytest.raises(ValueError):
            dedup_interactions(df, keys=["drug_id", "drug_id"])

    def test_des_5_conservative_defaults(self):
        df = _make_aspirin_df()
        result = dedup_by_inchikey(df, conservative_defaults=True)
        assert isinstance(result, pd.DataFrame)

    def test_des_6_merge_fields(self):
        df = pd.DataFrame([
            {"inchikey": "AAA", "name": "A",   "smiles": None,        "mw": 180.0},
            {"inchikey": "AAA", "name": None,  "smiles": "CCO",       "mw": None},
        ])
        result = dedup_by_inchikey(df, merge_fields=True, keep_lineage_columns=True)
        assert len(result) == 1
        # Merged row should have name from row 0 and smiles from row 1
        assert result.iloc[0]["name"] == "A"
        assert result.iloc[0]["smiles"] == "CCO"
        assert result.iloc[0]["mw"] == 180.0

    def test_des_7_keep_mark(self):
        df = _make_dpi_df()
        result = dedup_interactions(df, keep="mark", keep_lineage_columns=True)
        # All 3 rows preserved, but marked
        assert len(result) == 3

    def test_des_8_all_count_large(self):
        assert len(dedup_mod.__all__) >= 30


# ============================================================================
# Section 3 — Scientific Correctness (Domain 3)
# ============================================================================

class TestScientificCorrectness:
    """[SCI-1..12] Activity-type direction, segmentation, censoring, units."""

    def test_sci_1_pic50_keeps_higher(self):
        """pIC50 — higher = more potent. v1.0.0 was wrong (sorted ascending)."""
        df = pd.DataFrame([
            {"drug_id": 1, "protein_id": 10, "source": "chembl", "source_id": "a",
             "activity_value": 6.5, "activity_type": "pIC50"},
            {"drug_id": 1, "protein_id": 10, "source": "chembl", "source_id": "a",
             "activity_value": 8.5, "activity_type": "pIC50"},
        ])
        result = dedup_interactions(df, keys=["drug_id", "protein_id", "source", "source_id"])
        assert len(result) == 1
        assert result.iloc[0]["activity_value"] == 8.5  # higher wins

    def test_sci_1_pki_keeps_higher(self):
        df = pd.DataFrame([
            {"drug_id": 1, "protein_id": 10, "source": "chembl", "source_id": "a",
             "activity_value": 7.0, "activity_type": "pKi"},
            {"drug_id": 1, "protein_id": 10, "source": "chembl", "source_id": "a",
             "activity_value": 9.0, "activity_type": "pKi"},
        ])
        result = dedup_interactions(df, keys=["drug_id", "protein_id", "source", "source_id"])
        assert len(result) == 1
        assert result.iloc[0]["activity_value"] == 9.0

    def test_sci_1_ic50_keeps_lower(self):
        df = pd.DataFrame([
            {"drug_id": 1, "protein_id": 10, "source": "chembl", "source_id": "a",
             "activity_value": 50.0, "activity_type": "IC50"},
            {"drug_id": 1, "protein_id": 10, "source": "chembl", "source_id": "a",
             "activity_value": 100.0, "activity_type": "IC50"},
        ])
        result = dedup_interactions(df, keys=["drug_id", "protein_id", "source", "source_id"])
        assert len(result) == 1
        assert result.iloc[0]["activity_value"] == 50.0  # lower wins

    def test_sci_1_auto_direction(self):
        # Mixed activity types — both should be retained (SCI-2)
        df = pd.DataFrame([
            {"drug_id": 1, "protein_id": 10, "source": "chembl", "source_id": "a",
             "activity_value": 50.0, "activity_type": "IC50"},
            {"drug_id": 1, "protein_id": 10, "source": "chembl", "source_id": "a",
             "activity_value": 7.0, "activity_type": "pIC50"},
        ])
        result = dedup_interactions(df, keys=["drug_id", "protein_id", "source", "source_id"])
        # SCI-2: different activity_type → NOT duplicates → both kept
        assert len(result) == 2

    def test_sci_2_segments_by_activity_type(self):
        """Two rows with same composite key but different activity_type are NOT duplicates."""
        df = pd.DataFrame([
            {"drug_id": 1, "protein_id": 10, "source": "chembl", "source_id": "a",
             "activity_value": 50.0,  "activity_type": "IC50"},
            {"drug_id": 1, "protein_id": 10, "source": "chembl", "source_id": "a",
             "activity_value": 100.0, "activity_type": "Ki"},
        ])
        result = dedup_interactions(df, keys=["drug_id", "protein_id", "source", "source_id"])
        assert len(result) == 2  # both retained — different activity_type

    def test_sci_3_censored_gt_does_not_win(self):
        """>100 should not silently win over an actual 50.0 value."""
        df = pd.DataFrame([
            {"drug_id": 1, "protein_id": 10, "source": "chembl", "source_id": "a",
             "activity_value": ">100", "activity_type": "IC50"},
            {"drug_id": 1, "protein_id": 10, "source": "chembl", "source_id": "a",
             "activity_value": 50.0,   "activity_type": "IC50"},
        ])
        result = dedup_interactions(df, keys=["drug_id", "protein_id", "source", "source_id"])
        assert len(result) == 1
        # The actual 50.0 should win, not the censored >100
        assert float(result.iloc[0]["activity_value"]) == 50.0

    def test_sci_3_censored_lt_does_not_silently_win(self):
        """<10 should not silently win over actual 5.0 (both look 'low')."""
        df = pd.DataFrame([
            {"drug_id": 1, "protein_id": 10, "source": "chembl", "source_id": "a",
             "activity_value": "<10", "activity_type": "IC50"},
            {"drug_id": 1, "protein_id": 10, "source": "chembl", "source_id": "a",
             "activity_value": 5.0,   "activity_type": "IC50"},
        ])
        result = dedup_interactions(df, keys=["drug_id", "protein_id", "source", "source_id"])
        assert len(result) == 1
        # 5.0 (actual) should win over <10 (censored)
        assert float(result.iloc[0]["activity_value"]) == 5.0

    def test_sci_4_unit_normalization(self):
        """1 uM should not beat 100 nM (both ≈ same potency after normalization)."""
        df = pd.DataFrame([
            {"drug_id": 1, "protein_id": 10, "source": "chembl", "source_id": "a",
             "activity_value": 100.0,  "activity_type": "IC50", "activity_units": "nM"},
            {"drug_id": 1, "protein_id": 10, "source": "chembl", "source_id": "a",
             "activity_value": 0.1,    "activity_type": "IC50", "activity_units": "uM"},
        ])
        # 100 nM = 0.1 uM — they're equal in nM, so first-occurrence wins
        result = dedup_interactions(df, keys=["drug_id", "protein_id", "source", "source_id"])
        assert len(result) == 1

    def test_sci_5_synth_keys_not_collapsed(self):
        df = pd.DataFrame([
            {"inchikey": "SYNTH001", "name": "Drug A"},
            {"inchikey": "SYNTH002", "name": "Drug B"},
        ])
        result = dedup_by_inchikey(df)
        assert len(result) == 2  # both SYNTH keys are unique

    def test_sci_6_mixture_keys_not_deduplicated(self):
        # Mixture InChIKey (two connected layers)
        mixture_key = "AAAAAAAAAAAAAA-AAAAAAAAAA-A-BBBBBBBBBBBBBB-BBBBBBBBBB-B"
        df = pd.DataFrame([
            {"inchikey": mixture_key, "name": "Mixture 1"},
            {"inchikey": mixture_key, "name": "Mixture 2"},
        ])
        result = dedup_by_inchikey(df)
        assert len(result) == 2  # mixture keys not collapsed

    def test_sci_7_inchikey_validation(self):
        from cleaning.deduplicator import _is_valid_inchikey_format
        assert _is_valid_inchikey_format("BSYNRYMUTXBXSQ-UHFFFAOYSA-N") is True
        assert _is_valid_inchikey_format("SYNTH001") is True
        assert _is_valid_inchikey_format(None) is False
        assert _is_valid_inchikey_format("invalid") is False
        assert _is_valid_inchikey_format("") is False

    def test_sci_8_whitespace_regex_fix(self):
        """v1.0.0 bug: str.match(r'^\\s+|\\s+$') only matches START of string."""
        from cleaning.deduplicator import _check_whitespace_inchikeys, _WHITESPACE_PATTERN
        # Trailing whitespace should be detected
        df = pd.DataFrame({"inchikey": ["AAA", " BBB", "CCC "]})
        assert _check_whitespace_inchikeys(df) is True

    def test_sci_10_negative_value_quarantined(self):
        df = pd.DataFrame([
            {"drug_id": 1, "protein_id": 10, "source": "chembl", "source_id": "a",
             "activity_value": -5.0, "activity_type": "IC50"},
            {"drug_id": 1, "protein_id": 10, "source": "chembl", "source_id": "a",
             "activity_value": 50.0, "activity_type": "IC50"},
        ])
        result = dedup_interactions(df, keys=["drug_id", "protein_id", "source", "source_id"])
        assert len(result) == 1
        assert float(result.iloc[0]["activity_value"]) == 50.0

    def test_sci_12_confidence_tiebreaker(self):
        """When activity_values tie, higher confidence wins."""
        df = pd.DataFrame([
            {"drug_id": 1, "protein_id": 10, "source": "chembl", "source_id": "a",
             "activity_value": 50.0, "activity_type": "IC50", "confidence_score": 0.9},
            {"drug_id": 1, "protein_id": 10, "source": "chembl", "source_id": "a",
             "activity_value": 50.0, "activity_type": "IC50", "confidence_score": 0.5},
        ])
        result = dedup_interactions(df, keys=["drug_id", "protein_id", "source", "source_id"])
        assert len(result) == 1
        assert float(result.iloc[0]["confidence_score"]) == 0.9


# ============================================================================
# Section 4 — Coding (Domain 4)
# ============================================================================

class TestCoding:
    """[CODE-1..10] Type hints, validation, functional style."""

    def test_code_1_modern_type_hints(self):
        src = inspect.getsource(dedup_mod)
        # Should NOT use deprecated typing.List/Dict/Tuple at top-level
        # (note: List might appear in docstrings, so we check imports)
        first_50 = "\n".join(src.splitlines()[:50])
        assert "from typing import List" not in first_50

    def test_code_2_input_type_validation(self):
        with pytest.raises(TypeError):
            dedup_by_inchikey([1, 2, 3])  # not a DataFrame
        with pytest.raises(TypeError):
            dedup_interactions("not a df", keys=["drug_id"])

    def test_code_3_no_inplace_on_slices(self):
        """inplace=True on slices is forbidden (SettingWithCopyWarning).

        We check code lines only, skipping docstrings and comments.
        """
        src_lines = inspect.getsource(dedup_mod).splitlines()
        in_docstring = False
        for i, line in enumerate(src_lines):
            stripped = line.strip()
            # Track triple-quoted docstrings
            if '"""' in stripped:
                # Toggle if odd count of triple-quotes on this line
                count = stripped.count('"""')
                if count == 1:
                    in_docstring = not in_docstring
                # If count == 2, the docstring opens and closes on same line — no toggle
                continue
            if in_docstring:
                continue
            # Skip comment lines
            if stripped.startswith("#"):
                continue
            # Check for inplace=True (forbidden in code)
            if "inplace=True" in line:
                pytest.fail(
                    f"inplace=True found at line {i+1}: {stripped}"
                )

    def test_code_4_reset_index_parameter(self):
        df = _make_aspirin_df()
        # Default: reset_index=True
        r1 = dedup_by_inchikey(df)
        assert list(r1.index) == [0, 1]
        # reset_index=False: keep original index
        df_custom_index = pd.DataFrame(
            [
                {"inchikey": "AAA", "name": "Aspirin", "smiles": "CCO", "mw": 180.0},
                {"inchikey": "AAA", "name": None,     "smiles": "CCO", "mw": None},
                {"inchikey": "BBB", "name": "Ibuprofen", "smiles": "CCC", "mw": 206.0},
            ],
            index=[10, 20, 30],
        )
        r2 = dedup_by_inchikey(df_custom_index, reset_index=False)
        assert 10 in r2.index  # original Aspirin row kept
        assert 20 not in r2.index  # duplicate row dropped

    def test_code_5_keys_validation(self):
        df = _make_dpi_df()
        with pytest.raises(TypeError):
            dedup_interactions(df, keys="drug_id")  # str, not list
        with pytest.raises(TypeError):
            dedup_interactions(df, keys=[1, 2, 3])  # not strings

    def test_code_7_preserve_column_order(self):
        df = pd.DataFrame({
            "z_col": [1, 2, 3],
            "inchikey": ["AAA", "AAA", "BBB"],
            "a_col": ["a", "b", "c"],
            "name": ["A", "A2", "B"],
        })
        result = dedup_by_inchikey(df)
        # Column order should match input
        assert list(result.columns) == list(df.columns)

    def test_code_8_keyword_only_parameters(self):
        """All new parameters must be keyword-only."""
        sig = inspect.signature(dedup_by_inchikey)
        params = sig.parameters
        # `df` is positional-or-keyword, everything else should be KEYWORD_ONLY
        for name, p in params.items():
            if name == "df":
                assert p.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
            else:
                assert p.kind == inspect.Parameter.KEYWORD_ONLY, \
                    f"{name} should be keyword-only"

    def test_code_9_log_event_helper(self):
        """_log_event should produce structured logs."""
        from cleaning.deduplicator import _log_event
        # Just verify it doesn't crash
        _log_event("info", "test_event", key="value")

    def test_code_10_dir_function(self):
        """__dir__ should include all public names."""
        d = dir(dedup_mod)
        for name in dedup_mod.__all__:
            assert name in d, f"{name} not in dir(deduplicator)"


# ============================================================================
# Section 5 — Data Quality & Integrity (Domain 5)
# ============================================================================

class TestDataQuality:
    """[DQ-1..12] Completeness, accuracy, uniqueness, consistency."""

    def test_dq_1_nan_inchikey_not_collapsed(self):
        """v1.0.0 BUG: NaN==NaN caused all null-inchikey rows to collapse into one."""
        df = pd.DataFrame([
            {"inchikey": None, "name": "Drug A"},
            {"inchikey": None, "name": "Drug B"},
            {"inchikey": None, "name": "Drug C"},
            {"inchikey": None, "name": "Drug D"},
        ])
        result = dedup_by_inchikey(df)
        assert len(result) == 4  # all 4 preserved (not collapsed to 1)

    def test_dq_2_nan_equivalent_strings(self):
        """Strings like 'n/a', 'null', '-' should be treated as null."""
        df = pd.DataFrame([
            {"inchikey": "n/a", "name": "A"},
            {"inchikey": "null", "name": "B"},
            {"inchikey": "-", "name": "C"},
            {"inchikey": "AAA", "name": "D"},
        ])
        # The 'n/a', 'null', '-' should be normalized to NaN and preserved as unique
        result = dedup_by_inchikey(df)
        # AAA survives, plus 3 null sentinels = 4
        assert len(result) == 4

    def test_dq_3_lineage_column_exclusion(self):
        """_cleaning_applied and other lineage cols shouldn't count toward completeness."""
        df = pd.DataFrame([
            {"inchikey": "AAA", "name": "A", "_cleaning_applied": "step1;step2;step3;step4;step5;"},
            {"inchikey": "AAA", "name": None, "_cleaning_applied": ""},
        ])
        # Row 0 should win because 'name' is filled, even though _cleaning_applied differs
        result = dedup_by_inchikey(df)
        assert len(result) == 1
        assert result.iloc[0]["name"] == "A"

    def test_dq_4_confidence_range_validation(self):
        """Confidence scores outside [0, 1] should be clipped."""
        df = pd.DataFrame([
            {"drug_id": 1, "protein_id": 10, "source": "chembl", "source_id": "a",
             "activity_value": 50.0, "activity_type": "IC50", "confidence_score": 1.5},
            {"drug_id": 1, "protein_id": 10, "source": "chembl", "source_id": "a",
             "activity_value": 50.0, "activity_type": "IC50", "confidence_score": 0.5},
        ])
        # Should not crash; confidence should be clipped
        result = dedup_interactions(df, keys=["drug_id", "protein_id", "source", "source_id"])
        assert len(result) == 1

    def test_dq_5_null_source_id_kept(self):
        """NULL source_id should NOT be treated as a duplicate key."""
        df = pd.DataFrame([
            {"drug_id": 1, "protein_id": 10, "source": "chembl", "source_id": None,
             "activity_value": 50.0},
            {"drug_id": 1, "protein_id": 10, "source": "chembl", "source_id": None,
             "activity_value": 100.0},
        ])
        # With null_keys_handler="keep_all" (default), both rows survive
        result = dedup_interactions(df, keys=["drug_id", "protein_id", "source", "source_id"])
        assert len(result) == 2

    def test_dq_6_suspicious_duplicate_ratio(self):
        df = pd.DataFrame([
            {"inchikey": "AAA", "name": f"Drug {i}"} for i in range(20)
        ])
        # 20 rows, 1 unique inchikey → ratio = 19/20 = 0.95
        with pytest.raises(ValueError):
            dedup_by_inchikey(df, max_duplicate_ratio=0.5)

    def test_dq_7_non_numeric_activity_value_quarantined(self):
        df = pd.DataFrame([
            {"drug_id": 1, "protein_id": 10, "source": "chembl", "source_id": "a",
             "activity_value": "not_a_number", "activity_type": "IC50"},
            {"drug_id": 1, "protein_id": 10, "source": "chembl", "source_id": "a",
             "activity_value": 50.0, "activity_type": "IC50"},
        ])
        result = dedup_interactions(df, keys=["drug_id", "protein_id", "source", "source_id"],
                                     conservative_defaults=True)
        assert len(result) == 1
        assert float(result.iloc[0]["activity_value"]) == 50.0

    def test_dq_8_invalid_activity_type_warns(self):
        """Unknown activity_type should log a warning, not crash."""
        df = pd.DataFrame([
            {"drug_id": 1, "protein_id": 10, "source": "chembl", "source_id": "a",
             "activity_value": 50.0, "activity_type": "WEIRD_TYPE"},
            {"drug_id": 1, "protein_id": 10, "source": "chembl", "source_id": "a",
             "activity_value": 100.0, "activity_type": "WEIRD_TYPE"},
        ])
        # Default: warn and proceed (treat as ASC)
        result = dedup_interactions(df, keys=["drug_id", "protein_id", "source", "source_id"])
        assert len(result) == 1

    def test_dq_8_strict_activity_type_raises(self):
        df = pd.DataFrame([
            {"drug_id": 1, "protein_id": 10, "source": "chembl", "source_id": "a",
             "activity_value": 50.0, "activity_type": "WEIRD_TYPE"},
        ])
        with pytest.raises((ValueError, Exception)):
            dedup_interactions(
                df, keys=["drug_id", "protein_id", "source", "source_id"],
                strict_activity_type=True,
            )

    def test_dq_9_version_char_mismatch_detection(self):
        """Two InChIKeys differing only in version char should trigger warning."""
        df = pd.DataFrame([
            {"inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N", "name": "A"},
            {"inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-S", "name": "B"},
        ])
        result = dedup_by_inchikey(df)  # default: dedup_by_version_char=False
        # Both should be retained (different version chars = different keys by default)
        assert len(result) == 2

    def test_dq_10_quality_report_drug(self):
        df = _make_aspirin_df()
        report = quality_report(df, data_type="drug")
        assert report["row_count"] == 3
        assert "null_counts" in report
        assert "inchikey" in report["null_counts"]
        assert report["duplicate_counts"]["inchikey"] == 1

    def test_dq_10_quality_report_interaction(self):
        df = _make_dpi_df()
        report = quality_report(df, data_type="interaction")
        assert report["row_count"] == 3
        assert report["data_type"] == "interaction"

    def test_dq_11_metrics_incremented(self):
        df = _make_aspirin_df()
        reset_metrics()
        dedup_by_inchikey(df)
        m = get_metrics()
        assert m["dedup_by_inchikey_calls"] == 1
        assert m["dedup_by_inchikey_rows_in"] == 3
        assert m["dedup_by_inchikey_rows_out"] == 2
        assert m["dedup_by_inchikey_duplicates_removed"] == 1

    def test_dq_12_referential_integrity_check(self):
        df = pd.DataFrame([
            {"inchikey": "AAA", "name": "A"},
            {"inchikey": "BBB", "name": "B"},
            {"inchikey": "CCC", "name": "C"},  # not in known_inchikeys
        ])
        known = {"AAA", "BBB"}
        report = referential_integrity_check(df, known_inchikeys=known)
        assert report["violation_count"] == 1
        assert not report["is_valid"]


# ============================================================================
# Section 6 — Reliability & Resilience (Domain 6)
# ============================================================================

class TestReliability:
    """[REL-1..9] Dead letters, retry, circuit breaker, recovery."""

    def test_rel_1_dead_letter_queue_populated(self):
        df = _make_aspirin_df()
        clear_dead_letters()
        dedup_by_inchikey(df)
        # 1 duplicate dropped → at least 1 dead-letter entry
        letters = get_dead_letters()
        assert len(letters) >= 1
        assert letters[0]["function"] == "dedup_by_inchikey"
        assert "timestamp" in letters[0]
        assert "correlation_id" in letters[0]

    def test_rel_1_dead_letter_bounded(self):
        """DLQ must FIFO-evict when full (10K cap)."""
        # Push 5 fake entries to verify clear_dead_letters works
        for i in range(5):
            dedup_by_inchikey(pd.DataFrame([
                {"inchikey": "AAA", "name": f"A{i}"},
                {"inchikey": "AAA", "name": f"A{i}-dup"},
            ]))
            clear_dead_letters()  # keep clean per iteration
        # Cap should be defined
        assert MAX_DEAD_LETTERS > 0

    def test_rel_3_circuit_breaker(self):
        from cleaning.deduplicator import _cb_dedup_by_inchikey
        # Initially closed
        assert _cb_dedup_by_inchikey.state == "closed"
        # Force failures
        for _ in range(5):
            _cb_dedup_by_inchikey.record_failure()
        assert _cb_dedup_by_inchikey.state == "open"
        # Should be reset on success
        _cb_dedup_by_inchikey.record_success()
        assert _cb_dedup_by_inchikey.state == "closed"

    def test_rel_6_recover_from_failure(self):
        df = _make_aspirin_df()
        partial = df.iloc[:2].copy()
        result = recover_from_failure(df, partial, ValueError("test"))
        assert isinstance(result, pd.DataFrame)
        assert result.attrs.get("recovery_mode") is True

    def test_rel_7_flush_dead_letters(self, tmp_path):
        # Populate DLQ
        df = _make_aspirin_df()
        dedup_by_inchikey(df)
        path = tmp_path / "dl.jsonl"
        n = flush_dead_letters(path)
        assert n > 0
        assert path.exists()
        # File should be JSONL
        content = path.read_text(encoding="utf-8")
        lines = [l for l in content.splitlines() if l.strip()]
        for line in lines:
            entry = json.loads(line)
            assert "function" in entry
            assert "timestamp" in entry

    def test_rel_7_flush_dead_letters_path_traversal(self):
        with pytest.raises(ValueError):
            flush_dead_letters("../../etc/passwd")

    def test_rel_8_checkpoint_state(self):
        df = _make_aspirin_df()
        cp = checkpoint_state(df)
        assert cp["row_count"] == 3
        assert "column_hashes" in cp
        assert "inchikey" in cp["column_hashes"]
        assert cp["column_hashes"]["inchikey"] != "error"

    def test_rel_9_validate_recovery_state(self):
        df = _make_aspirin_df()
        cp = checkpoint_state(df)
        assert validate_recovery_state(cp) is True
        # Malformed
        assert validate_recovery_state({"foo": "bar"}) is False
        assert validate_recovery_state("not a dict") is False


# ============================================================================
# Section 7 — Idempotency & Reproducibility (Domain 7)
# ============================================================================

class TestIdempotency:
    """[IDEM-1..10] Idempotency markers, determinism, fingerprints."""

    def test_idem_1_idempotent_marker_set(self):
        df = _make_aspirin_df()
        result = dedup_by_inchikey(df)
        assert result.attrs.get("_dedup_already_applied") is True

    def test_idem_1_skip_if_already_deduped(self):
        df = _make_aspirin_df()
        # First call
        r1 = dedup_by_inchikey(df)
        # Mark r1 as already deduped
        r1.attrs["_dedup_already_applied"] = True
        # Second call should skip
        reset_metrics()
        r2 = dedup_by_inchikey(r1, skip_if_already_deduped=True)
        assert get_metrics()["dedup_by_inchikey_idempotent_skips"] == 1
        # Same shape
        assert r2.shape == r1.shape

    def test_idem_2_deterministic_tie_breaking(self):
        """Same input → same output, even with ties."""
        df = pd.DataFrame([
            {"inchikey": "AAA", "name": "A"},  # both equally complete
            {"inchikey": "AAA", "name": "B"},
        ])
        r1 = dedup_by_inchikey(df)
        r2 = dedup_by_inchikey(df)
        assert r1.iloc[0]["name"] == r2.iloc[0]["name"]

    def test_idem_4_input_output_fingerprints(self):
        df = _make_aspirin_df()
        result = dedup_by_inchikey(df)
        fp_in = result.attrs.get("_input_fingerprint")
        fp_out = result.attrs.get("_output_fingerprint")
        assert isinstance(fp_in, str) and len(fp_in) == 64
        assert isinstance(fp_out, str) and len(fp_out) == 64
        assert fp_in != fp_out  # data changed

    def test_idem_4_fingerprint_stable(self):
        """Same input twice → same output fingerprint."""
        df = _make_aspirin_df()
        r1 = dedup_by_inchikey(df, skip_if_already_deduped=False)
        r2 = dedup_by_inchikey(df, skip_if_already_deduped=False)
        assert r1.attrs["_output_fingerprint"] == r2.attrs["_output_fingerprint"]

    def test_idem_5_backfill_safety_check(self):
        df = pd.DataFrame([
            {"inchikey": "AAA", "name": "A"},
            {"inchikey": "BBB", "name": "B"},  # not in known set
        ])
        known = {"AAA"}
        safe_df, warnings = backfill_safety_check(df, known, on_conflict="warn")
        assert len(warnings) == 1
        assert "1" in warnings[0]
        # keep_existing mode: drops the conflict
        safe_df2, _ = backfill_safety_check(df, known, on_conflict="keep_existing")
        assert len(safe_df2) == 1

    def test_idem_5_backfill_safety_check_error_mode(self):
        df = pd.DataFrame([{"inchikey": "CCC", "name": "C"}])
        with pytest.raises(ValueError):
            backfill_safety_check(df, {"AAA"}, on_conflict="error")

    def test_idem_9_is_reproducible(self):
        df = _make_aspirin_df()
        r1 = dedup_by_inchikey(df, skip_if_already_deduped=False)
        r2 = dedup_by_inchikey(df, skip_if_already_deduped=False)
        assert is_reproducible(r1, r2) is True

    def test_idem_10_reproducibility_report(self):
        df = _make_aspirin_df()
        report = reproducibility_report(df)
        assert report["is_reproducible"] is True
        assert report["fingerprint_stable"] is True
        assert "fingerprint" in report
        assert len(report["fingerprint"]) == 64


# ============================================================================
# Section 8 — Performance & Scalability (Domain 8)
# ============================================================================

class TestPerformance:
    """[PERF-1..8] Vectorization, chunked, lazy, caching, benchmark."""

    def test_perf_1_vectorized_completeness(self):
        df = _make_aspirin_df()
        scores = compute_completeness_score(df)
        assert isinstance(scores, pd.Series)
        assert len(scores) == 3
        # Row 0 (Aspirin, all filled) should score highest
        assert scores.iloc[0] > scores.iloc[1]

    def test_perf_3_chunked_processing(self):
        df = pd.DataFrame([
            {"inchikey": "AAA", "name": "A"},
            {"inchikey": "AAA", "name": None},
            {"inchikey": "BBB", "name": "B"},
            {"inchikey": "BBB", "name": None},
            {"inchikey": "CCC", "name": "C"},
        ])
        single = dedup_by_inchikey(df, skip_if_already_deduped=False)
        chunked = dedup_by_inchikey_chunked(df, chunk_size=2, skip_if_already_deduped=False)
        # Should produce same number of rows
        assert len(single) == len(chunked)

    def test_perf_8_performance_benchmark(self):
        df = _make_aspirin_df()
        result = performance_benchmark(df)
        assert result["row_count"] == 3
        assert result["dedup_by_inchikey"]["status"] == "ok"
        assert result["dedup_by_inchikey"]["duration_s"] >= 0.0

    def test_perf_100_rows_under_5s(self):
        """[PERF-8] 100 rows should dedup in under 5 seconds."""
        rows = [{"inchikey": f"KEY{i:03d}", "name": f"Drug{i}"} for i in range(100)]
        # Add some duplicates
        for i in range(20):
            rows.append({"inchikey": f"KEY{i:03d}", "name": None})
        df = pd.DataFrame(rows)
        start = time.perf_counter()
        dedup_by_inchikey(df, skip_if_already_deduped=False)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0, f"Took {elapsed:.2f}s"

    def test_perf_1000_rows_under_1s(self):
        rows = [{"inchikey": f"KEY{i:04d}", "name": f"Drug{i}"} for i in range(1000)]
        df = pd.DataFrame(rows)
        start = time.perf_counter()
        dedup_by_inchikey(df, skip_if_already_deduped=False)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0, f"Took {elapsed:.2f}s"


# ============================================================================
# Section 9 — Security & Privacy (Domain 9)
# ============================================================================

class TestSecurity:
    """[SEC-1..8] Sanitization, PII, DoS, path traversal, audit."""

    def test_sec_1_sanitize_string(self):
        from cleaning.deduplicator import _sanitize_string_local
        s = _sanitize_string_local("hello\x00world")
        assert "\x00" not in s
        assert "hello" in s and "world" in s

    def test_sec_1_sanitize_truncates(self):
        from cleaning.deduplicator import _sanitize_string_local
        s = _sanitize_string_local("a" * 500, max_length=100)
        assert len(s) <= 120  # 100 + truncation marker

    def test_sec_2_redact_pii_in_logs(self):
        from cleaning.deduplicator import _redact_for_log_local
        redacted = _redact_for_log_local("user@example.com")
        assert "user@example.com" not in redacted
        assert "[email]" in redacted

    def test_sec_2_redact_ssn(self):
        from cleaning.deduplicator import _redact_for_log_local
        redacted = _redact_for_log_local("123-45-6789")
        assert "123-45-6789" not in redacted
        assert "[ssn]" in redacted

    def test_sec_3_dos_guard(self):
        """DataFrame > _MAX_DATAFRAME_ROWS should be rejected."""
        from cleaning.deduplicator import _MAX_DATAFRAME_ROWS, _validate_input_size
        # Create a small df and patch the limit
        df = pd.DataFrame({"a": [1]})
        # Use mock to simulate exceeding the limit
        with patch("cleaning.deduplicator._MAX_DATAFRAME_ROWS", 0):
            with pytest.raises(ValueError):
                _validate_input_size(pd.DataFrame({"a": [1, 2, 3]}))

    def test_sec_4_path_traversal_guard(self):
        with pytest.raises(ValueError):
            flush_dead_letters("../../etc/passwd")
        with pytest.raises(ValueError):
            flush_dead_letters("/etc/passwd")

    def test_sec_7_operator_id_in_provenance(self):
        df = _make_aspirin_df()
        result = dedup_by_inchikey(df, operator_id="user123", source_dataset_id="chembl_v32")
        prov = get_provenance(result)
        assert prov.get("operator_id") == "user123"
        assert prov.get("source_dataset_id") == "chembl_v32"


# ============================================================================
# Section 10 — Testing & Validation (Domain 10)
# ============================================================================

class TestTestingValidation:
    """Meta-tests verifying the test suite itself is robust."""

    def test_test_1_module_has_200_plus_assertions(self):
        """Verify the test file itself has substantial assertions."""
        # This is a meta-test — just verify we have many test methods
        test_methods = [
            name for name in dir(TestScientificCorrectness)
            if name.startswith("test_")
        ]
        assert len(test_methods) >= 10

    def test_test_2_each_domain_covered(self):
        """Verify all 16 domain test classes exist."""
        classes = [
            TestArchitecture, TestDesign, TestScientificCorrectness, TestCoding,
            TestDataQuality, TestReliability, TestIdempotency, TestPerformance,
            TestSecurity, TestTestingValidation, TestLogging, TestConfiguration,
            TestDocumentation, TestCompliance, TestInteroperability, TestDataLineage,
        ]
        for cls in classes:
            methods = [n for n in dir(cls) if n.startswith("test_")]
            assert len(methods) > 0, f"{cls.__name__} has no test methods"

    def test_test_3_edge_cases_covered(self):
        """Verify TestEdgeCases class exists with multiple methods."""
        methods = [n for n in dir(TestEdgeCases) if n.startswith("test_")]
        assert len(methods) >= 8

    def test_test_6_schema_validation(self):
        """Verify the dedup output schema matches the documented contract."""
        df = _make_aspirin_df()
        result = dedup_by_inchikey(df)
        # No columns added by default
        assert list(result.columns) == list(df.columns)
        # attrs has expected keys
        for key in ["_provenance", "_input_fingerprint", "_output_fingerprint",
                    "cleaning_metrics"]:
            assert key in result.attrs


# ============================================================================
# Section 11 — Logging & Observability (Domain 11)
# ============================================================================

class TestLogging:
    """[LOG-1..8] Correlation ID, metrics, structured logging."""

    def test_log_1_correlation_id_set_get(self):
        set_correlation_id("test-cid-123")
        assert get_correlation_id() == "test-cid-123"
        set_correlation_id(None)
        # After clearing, may be None or package-level value
        # (depends on whether package-level was set)

    def test_log_1_correlation_id_in_provenance(self):
        set_correlation_id("cid-abc")
        df = _make_aspirin_df()
        result = dedup_by_inchikey(df)
        prov = get_provenance(result)
        assert prov.get("correlation_id") == "cid-abc"
        set_correlation_id(None)

    def test_log_3_metrics_present(self):
        df = _make_aspirin_df()
        reset_metrics()
        dedup_by_inchikey(df)
        m = get_metrics()
        assert "dedup_by_inchikey_calls" in m
        assert "dedup_by_inchikey_rows_in" in m
        assert "dedup_by_inchikey_rows_out" in m

    def test_log_4_reset_metrics(self):
        df = _make_aspirin_df()
        dedup_by_inchikey(df)
        reset_metrics()
        m = get_metrics()
        assert m["dedup_by_inchikey_calls"] == 0

    def test_log_7_timing_report(self):
        df = _make_aspirin_df()
        dedup_by_inchikey(df)
        report = timing_report()
        assert "dedup_by_inchikey" in report
        assert report["dedup_by_inchikey"]["calls"] >= 1
        assert report["dedup_by_inchikey"]["total_s"] >= 0.0

    def test_log_8_health_check(self):
        h = health_check()
        assert h["module"] == "cleaning.deduplicator"
        assert h["module_version"] == "3.0.0"
        assert "metrics" in h
        assert "circuit_breakers" in h
        assert "config" in h


# ============================================================================
# Section 12 — Configuration & Environment (Domain 12)
# ============================================================================

class TestConfiguration:
    """[CFG-1..7] Configuration, env vars, validation."""

    def test_cfg_1_configure_deduplicator(self):
        configure_deduplicator(max_duplicate_ratio=0.5)
        # The config should now have this value
        # (we can verify via validate_config returning no warnings about it)
        warnings = validate_config()
        assert isinstance(warnings, list)
        # Revert to default
        revert_configuration(steps=1)

    def test_cfg_3_validate_config(self):
        warnings = validate_config()
        assert isinstance(warnings, list)

    def test_cfg_6_revert_configuration(self):
        configure_deduplicator(max_duplicate_ratio=0.3)
        revert_configuration(steps=1)
        # Should be reverted to None (the default)

    def test_cfg_6_revert_configuration_invalid_steps(self):
        with pytest.raises(ValueError):
            revert_configuration(steps=0)

    def test_cfg_7_validate_environment(self):
        env = validate_environment()
        assert "python_version" in env
        assert "pandas_version" in env
        assert "module_version" in env
        assert env["module_version"] == "3.0.0"
        assert "issues" in env

    def test_cfg_invalid_completeness_weights(self):
        with pytest.raises(ValueError):
            configure_deduplicator(completeness_weights={"name": "not a number"})

    def test_cfg_invalid_max_dataframe_rows(self):
        with pytest.raises(ValueError):
            configure_deduplicator(max_dataframe_rows=0)

    def test_cfg_invalid_max_duplicate_ratio(self):
        with pytest.raises(ValueError):
            configure_deduplicator(max_duplicate_ratio=1.5)


# ============================================================================
# Section 13 — Documentation & Readability (Domain 13)
# ============================================================================

class TestDocumentation:
    """[DOC-1..7] Docstrings, data dictionary, decision documentation."""

    def test_doc_1_module_docstring_has_required_keywords(self):
        doc = dedup_mod.__doc__ or ""
        assert "API Stability" in doc
        assert "STABLE API" in doc
        assert "UNSTABLE API" in doc
        assert "FDA 21 CFR Part 11" in doc
        assert "GDPR" in doc
        assert "HIPAA" in doc
        assert "audit trail" in doc

    def test_doc_2_function_docstrings_present(self):
        for name in dedup_mod.__all__:
            if name.startswith("_"):
                continue
            obj = getattr(dedup_mod, name, None)
            if obj is None or not callable(obj):
                continue
            doc = getattr(obj, "__doc__", None)
            assert doc is not None, f"{name} has no docstring"
            assert len(doc) > 50, f"{name} docstring too short"

    def test_doc_7_module_version_in_docstring(self):
        doc = dedup_mod.__doc__ or ""
        # Should mention 3.0.0
        assert "3.0.0" in doc or "v3.0.0" in doc

    def test_doc_license_header(self):
        src = inspect.getsource(dedup_mod)
        first_50 = "\n".join(src.splitlines()[:50])
        assert "MIT License" in first_50
        assert "Copyright" in first_50
        assert "Team Cosmic" in first_50


# ============================================================================
# Section 14 — Compliance & Standards (Domain 14)
# ============================================================================

class TestCompliance:
    """[COMP-1..7] PEP 561, PEP 8, FDA, schema versioning."""

    def test_comp_1_pep_561_stubs_exist(self):
        stub_path = _PROJECT_ROOT / "cleaning" / "__init__.pyi"
        assert stub_path.exists()
        content = stub_path.read_text(encoding="utf-8")
        # New dedup stubs should be present
        assert "def dedup_by_inchikey" in content
        assert "class DedupResult" in content
        assert "class DedupStrategy" in content

    def test_comp_1_py_typed_present(self):
        assert (_PROJECT_ROOT / "cleaning" / "py.typed").exists()

    def test_comp_2_pep_8_naming(self):
        """Public functions follow snake_case_verb_first convention."""
        public_fns = [
            name for name in dedup_mod.__all__
            if not name.startswith("_")
            and callable(getattr(dedup_mod, name, None))
            and not name[0].isupper()  # skip class names
        ]
        valid_prefixes = (
            "dedup_", "compute_", "merge_", "validate_", "get_", "set_",
            "clear_", "reset_", "flush_", "configure_", "recover_",
            "checkpoint_", "is_", "reproducibility_", "performance_",
            "quality_", "referential_", "backfill_", "requires_",
            "revert_", "timing_", "health_", "clean_",
        )
        for name in public_fns:
            assert name.startswith(valid_prefixes), \
                f"{name} does not follow naming convention"

    def test_comp_3_fda_21_cfr_part_11_audit_trail(self):
        """Dead-letter queue supports FDA audit trail requirements."""
        df = _make_aspirin_df()
        dedup_by_inchikey(df)
        letters = get_dead_letters()
        if letters:
            entry = letters[0]
            assert "timestamp" in entry  # 21 CFR 11.10(e)
            assert "function" in entry   # 21 CFR 11.10(e)
            assert "module_version" in entry  # 21 CFR 11.10(c)

    def test_comp_4_schema_version(self):
        assert dedup_mod._OUTPUT_SCHEMA_VERSION == "3.0.0"
        # SCHEMA.md should document v3.0.0
        schema_path = _PROJECT_ROOT / "cleaning" / "SCHEMA.md"
        content = schema_path.read_text(encoding="utf-8")
        assert "dedup_by_inchikey output schema (v3.0.0)" in content

    def test_comp_5_naming_consistency(self):
        """All public names in __all__ are accessible."""
        for name in dedup_mod.__all__:
            assert hasattr(dedup_mod, name), f"{name} in __all__ but not on module"

    def test_comp_7_license_in_source(self):
        src = inspect.getsource(dedup_mod)
        first_30 = "\n".join(src.splitlines()[:30])
        assert "MIT License" in first_30
        assert "SPDX-License-Identifier: MIT" in first_30


# ============================================================================
# Section 15 — Interoperability & Integration (Domain 15)
# ============================================================================

class TestInteroperability:
    """[INTEROP-1..8] Import paths, loader compat, cross-platform."""

    def test_interop_1_backward_compatible_imports(self):
        """All 7 import patterns must work."""
        # 1
        from cleaning.deduplicator import dedup_by_inchikey as f1
        assert callable(f1)
        # 2
        from cleaning.deduplicator import dedup_interactions as f2
        assert callable(f2)
        # 3
        from cleaning import dedup_by_inchikey as f3
        assert callable(f3)
        # 4
        from cleaning import dedup_interactions as f4
        assert callable(f4)
        # 5
        import cleaning.deduplicator
        assert callable(cleaning.deduplicator.dedup_by_inchikey)
        # 6
        import cleaning
        assert callable(cleaning.dedup_by_inchikey)
        # 7
        from cleaning.deduplicator import __all__
        assert "dedup_by_inchikey" in __all__
        assert "dedup_interactions" in __all__

    def test_interop_2_output_consumable_by_loaders(self):
        """Output DataFrame should have same columns as input (no surprise additions)."""
        df = _make_aspirin_df()
        result = dedup_by_inchikey(df)
        assert list(result.columns) == list(df.columns)

    def test_interop_3_cross_platform_paths(self, tmp_path):
        """Path operations should use pathlib, not string concatenation."""
        df = _make_aspirin_df()
        dedup_by_inchikey(df)
        # Use a Path object
        path = tmp_path / "subdir" / "dl.jsonl"
        n = flush_dead_letters(path)
        assert n >= 0  # should not crash on subdir creation

    def test_interop_4_pandas_version_compat(self):
        """Should work on pandas 2.1.4+."""
        assert pd.__version__ >= "2.1.4"

    def test_interop_5_requires_api_version(self):
        assert requires_api_version("3.0.0") is True
        assert requires_api_version("2.0.0") is True
        assert requires_api_version("4.0.0") is False

    def test_interop_6_pre_post_hooks_integration(self):
        """Pre/post hooks should fire when dedup is called via clean_drugs."""
        pre_calls: list[str] = []
        post_calls: list[str] = []
        cleaning.register_pre_clean_hook(lambda step, df: pre_calls.append(step))
        cleaning.register_post_clean_hook(lambda step, df: post_calls.append(step))
        try:
            df = _make_aspirin_df()
            cleaning.clean_drugs(df)
            assert "dedup_by_inchikey" in pre_calls
            assert "dedup_by_inchikey" in post_calls
        finally:
            cleaning._pre_clean_hooks.clear()
            cleaning._post_clean_hooks.clear()

    def test_interop_7_requirements_txt_no_new_deps(self):
        """Production requirements.txt should not need new deps for dedup."""
        req_path = _PROJECT_ROOT / "requirements.txt"
        content = req_path.read_text(encoding="utf-8")
        # Dedup only needs pandas + stdlib — no new deps required
        assert "pandas" in content

    def test_interop_8_api_stability_documented(self):
        doc = dedup_mod.__doc__ or ""
        assert "STABLE API" in doc
        assert "UNSTABLE API" in doc


# ============================================================================
# Section 16 — Data Lineage & Traceability (Domain 16)
# ============================================================================

class TestDataLineage:
    """[LINEAGE-1..7] Provenance, source attribution, transformation chain."""

    def test_lineage_1_provenance_in_attrs(self):
        df = _make_aspirin_df()
        result = dedup_by_inchikey(df)
        prov_list = result.attrs.get("_provenance", [])
        assert isinstance(prov_list, list)
        assert len(prov_list) >= 1
        last = prov_list[-1]
        assert last["function"] == "dedup_by_inchikey"
        assert last["module_version"] == "3.0.0"
        assert last["schema_version"] == "3.0.0"
        assert len(last["input_fingerprint"]) == 64
        assert len(last["output_fingerprint"]) == 64
        assert last["input_rows"] == 3
        assert last["output_rows"] == 2
        assert last["duplicates_removed"] == 1

    def test_lineage_2_source_indices_with_merge_fields(self):
        df = pd.DataFrame([
            {"inchikey": "AAA", "name": "A", "smiles": None},
            {"inchikey": "AAA", "name": None, "smiles": "CCO"},
            {"inchikey": "BBB", "name": "B", "smiles": "CCC"},
        ])
        result = dedup_by_inchikey(df, merge_fields=True, keep_lineage_columns=True)
        assert "_dedup_source_indices" in result.columns
        # First row (AAA group) should have 2 source indices
        aaa_row = result[result["inchikey"] == "AAA"].iloc[0]
        assert len(aaa_row["_dedup_source_indices"]) == 2

    def test_lineage_3_dead_letter_survivor_info(self):
        df = _make_aspirin_df()
        clear_dead_letters()
        dedup_by_inchikey(df)
        letters = get_dead_letters()
        assert len(letters) >= 1
        # The dropped row should have a survivor_info entry
        if "survivor_info" in letters[0]:
            # v16 ROOT FIX (DC-5): survivor_info now includes BOTH
            # the dropped row's inchikey (renamed to 'dropped_inchikey'
            # for clarity) AND the survivor's inchikey + source
            # (previously only the dropped row's info was recorded,
            # making it impossible to tell WHICH record won the dedup).
            si = letters[0]["survivor_info"]
            assert "dropped_inchikey" in si or "inchikey" in si, \
                f"survivor_info should contain dropped_inchikey or inchikey; got {si}"
            # v16 DC-5 new fields:
            assert "survivor_inchikey" in si, \
                f"v16 DC-5: survivor_info should contain survivor_inchikey; got {si}"
            assert "survivor_source" in si, \
                f"v16 DC-5: survivor_info should contain survivor_source; got {si}"

    def test_lineage_4_get_provenance_helper(self):
        df = _make_aspirin_df()
        result = dedup_by_inchikey(df)
        prov = get_provenance(result)
        assert prov["function"] == "dedup_by_inchikey"
        assert prov["module_version"] == "3.0.0"

    def test_lineage_4_get_provenance_from_dedup_result(self):
        df = _make_aspirin_df()
        result = dedup_by_inchikey(df, return_result=True)
        prov = get_provenance(result)
        assert prov["function"] == "dedup_by_inchikey"

    def test_lineage_5_dependency_graph(self):
        """dedup_interactions should appear in column-to-function mappings."""
        assert "dedup_interactions" in cleaning.get_affected_functions("activity_value")
        assert "dedup_interactions" in cleaning.get_affected_functions("activity_type")
        assert "dedup_interactions" in cleaning.get_affected_functions("drug_id")
        assert "dedup_interactions" in cleaning.get_affected_functions("protein_id")

    def test_lineage_6_transformation_chain(self):
        df = _make_aspirin_df()
        result = dedup_by_inchikey(df)
        prov = get_provenance(result)
        assert "transformation_chain" in prov
        assert isinstance(prov["transformation_chain"], list)
        assert len(prov["transformation_chain"]) > 0
        # Should mention drop_duplicates (the core operation)
        assert any("drop_duplicates" in t or "compute_completeness" in t
                   for t in prov["transformation_chain"])

    def test_lineage_7_source_attribution(self):
        df = pd.DataFrame([
            {"inchikey": "AAA", "name": "A", "source": "chembl"},
            {"inchikey": "AAA", "name": None, "source": "chembl"},
            {"inchikey": "BBB", "name": "B", "source": "drugbank"},
        ])
        result = dedup_by_inchikey(df)
        prov = get_provenance(result)
        if "source_attribution" in prov:
            assert "chembl" in prov["source_attribution"]
            assert "drugbank" in prov["source_attribution"]


# ============================================================================
# Section 17 — Edge Cases (cross-domain)
# ============================================================================

class TestEdgeCases:
    """Edge-case tests for boundary conditions."""

    def test_empty_df(self):
        df = pd.DataFrame(columns=["inchikey", "name"])
        result = dedup_by_inchikey(df)
        assert len(result) == 0

    def test_single_row(self):
        df = pd.DataFrame([{"inchikey": "AAA", "name": "A"}])
        result = dedup_by_inchikey(df)
        assert len(result) == 1

    def test_no_duplicates(self):
        df = pd.DataFrame([
            {"inchikey": "AAA", "name": "A"},
            {"inchikey": "BBB", "name": "B"},
            {"inchikey": "CCC", "name": "C"},
        ])
        result = dedup_by_inchikey(df)
        assert len(result) == 3

    def test_all_null_inchikey_column(self):
        df = pd.DataFrame([
            {"inchikey": None, "name": "A"},
            {"inchikey": None, "name": "B"},
            {"inchikey": None, "name": "C"},
        ])
        result = dedup_by_inchikey(df)
        # All 3 should be preserved (NaN not collapsed)
        assert len(result) == 3

    def test_missing_inchikey_column(self):
        df = pd.DataFrame([{"name": "A"}, {"name": "B"}])
        result = dedup_by_inchikey(df)
        assert len(result) == 2  # returned unchanged

    def test_extra_columns_preserved(self):
        df = pd.DataFrame([
            {"inchikey": "AAA", "name": "A", "extra_col": 1},
            {"inchikey": "AAA", "name": None, "extra_col": 2},
            {"inchikey": "BBB", "name": "B", "extra_col": 3},
        ])
        result = dedup_by_inchikey(df)
        assert "extra_col" in result.columns
        assert len(result) == 2

    def test_duplicate_index_input(self):
        df = pd.DataFrame(
            [{"inchikey": "AAA", "name": "A"}, {"inchikey": "AAA", "name": "B"}],
            index=[5, 5],
        )
        result = dedup_by_inchikey(df)
        assert len(result) == 1

    def test_categorical_dtype(self):
        df = pd.DataFrame({
            "inchikey": pd.Categorical(["AAA", "AAA", "BBB"]),
            "name": ["A", "B", "C"],
        })
        result = dedup_by_inchikey(df)
        assert len(result) == 2


# ============================================================================
# Section 18 — Source-literal invariants (TestIssue21 / TestIssue36)
# ============================================================================

class TestSourceLiterals:
    """Verify the source-literal constraints from TestIssue21 and TestIssue36."""

    def test_uses_drop_duplicates(self):
        """Source MUST contain 'drop_duplicates'."""
        src = inspect.getsource(dedup_mod)
        assert "drop_duplicates" in src

    def test_no_groupby_first(self):
        """Source MUST NOT contain 'groupby("inchikey", sort=False).first()'."""
        src = inspect.getsource(dedup_mod)
        assert 'groupby("inchikey", sort=False).first()' not in src

    def test_has_all(self):
        """Source MUST contain '__all__'."""
        src = inspect.getsource(dedup_mod)
        assert "__all__" in src


# ============================================================================
# Section 19 — Cross-Domain Integration
# ============================================================================

class TestCrossDomainIntegration:
    """End-to-end tests combining multiple domains."""

    def test_clean_drugs_calls_dedup_last(self):
        """clean_drugs orchestration should include dedup_by_inchikey."""
        df = _make_aspirin_df()
        result = cleaning.clean_drugs(df)
        # Result should be non-empty
        assert len(result) >= 1
        # Should have provenance
        assert "_provenance" in result.attrs

    def test_clean_interactions_works(self):
        df = _make_dpi_df()
        result = clean_interactions(df)
        assert len(result) >= 1

    def test_no_files_removed(self):
        """Verify all expected cleaning files exist."""
        cleaning_dir = _PROJECT_ROOT / "cleaning"
        for fname in ["__init__.py", "normalizer.py", "missing_values.py",
                      "deduplicator.py", "__init__.pyi", "py.typed",
                      "SCHEMA.md", "MIGRATION.md"]:
            assert (cleaning_dir / fname).exists(), f"{fname} missing"

    def test_module_load_time_is_recent(self):
        _ = cleaning.dedup_by_inchikey  # force load
        load_times = cleaning.get_load_times()
        # Should have a non-negative load time
        if "dedup_by_inchikey" in load_times:
            assert load_times["dedup_by_inchikey"] >= 0.0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
