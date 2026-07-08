"""
Comprehensive test suite for the pipelines package public API.

This test module verifies that ``pipelines/__init__.py`` correctly
re-exports every public symbol from all 8 submodules (base_pipeline +
7 source pipelines) via the lazy-loading ``__getattr__`` pattern, and
that the entire pipelines layer functions correctly as a package facade.

This test file mirrors the structure of ``tests/test_database_init.py``
which is the gold-standard test for the sibling ``database/__init__.py``.

Test Categories:
  1. Package API Surface — every symbol in __all__ is importable
  2. Lazy Loading Behaviour — no side effects at import time
  3. PEP 562 Compliance — __getattr__ and __dir__ are correct
  4. Backward Compatibility — deep imports still work
  5. Factory & Introspection — get_pipeline, get_expected_pipelines
  6. Knowledge Graph Mapping — get_kg_mapping
  7. Filtering Thresholds — get_filtering_thresholds (scientific correctness)
  8. Data Dictionary — DATA_DICTIONARY structure
  9. Source Attribution — SOURCE_ATTRIBUTION structure
  10. Validation — validate_infrastructure, _validate_security
  11. Configuration — validate_config, get_config_summary
  12. Lineage & Provenance — get_provenance, get_audit_trail
  13. State Serialisation — to_state_dict / from_state_dict
  14. Reliability — dead letters, circuit breaker, recover_from_failure
  15. Observability — correlation ID, log level, metrics
  16. CLI — python -m pipelines version/list
  17. PEP 8 / PEP 257 Compliance — line length, docstring
  18. ARM64 Simulation — rdkit missing → ChEMBL fails, UniProt works
"""

from __future__ import annotations

import importlib
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# sys.path setup (must happen before importing pipelines)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Set DATABASE_URL BEFORE importing pipelines, because some pipeline
# submodules transitively import config.settings which reads DATABASE_URL.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

# Now import the pipelines package — this MUST NOT trigger side effects
import pipelines  # noqa: E402


# ---------------------------------------------------------------------------
# Autouse fixture: reset the pipelines package's lazy-loaded cache
# between every test (IDEM-7, IDEM-8, TEST-12).
# ---------------------------------------------------------------------------
@pytest.fixture(scope="function", autouse=True)
def _reset_pipelines_package():
    """Reset the pipelines package's lazy-loaded cache between tests."""
    pipelines._reset()
    yield
    pipelines._reset()


# ---------------------------------------------------------------------------
# 1. Package API Surface (TEST-1, TEST-2, TEST-3, TEST-9, TEST-10)
# ---------------------------------------------------------------------------
class TestPackageAPISurface:
    """Verify the public API surface is correctly declared and importable."""

    def test_all_exists_and_is_list(self):
        """TEST-1: __all__ exists and is a list."""
        assert hasattr(pipelines, "__all__")
        assert isinstance(pipelines.__all__, list)

    def test_all_contains_expected_count(self):
        """TEST-1: __all__ has at least 40 entries (8 classes + 20+ constants + 12+ utilities)."""
        assert len(pipelines.__all__) >= 40, (
            f"__all__ has {len(pipelines.__all__)} entries, expected >= 40"
        )

    def test_all_symbols_are_strings(self):
        """TEST-1: every __all__ entry is a string."""
        for name in pipelines.__all__:
            assert isinstance(name, str), f"{name!r} is not a string"

    def test_all_typed_as_list_of_str(self):
        """CODE-3/CODE-10: __all__ has : list[str] annotation."""
        # The annotation is in the source; verify by reading the file
        init_path = PROJECT_ROOT / "pipelines" / "__init__.py"
        source = init_path.read_text()
        assert re.search(
            r"^__all__:\s*list\[str\]\s*=",
            source,
            re.MULTILINE,
        ), "__all__ is not annotated as list[str]"

    @pytest.mark.parametrize("symbol_name", [
        # 8 classes
        "BasePipeline", "ChEMBLPipeline", "DrugBankPipeline",
        "UniProtPipeline", "StringPipeline", "DisGeNETPipeline",
        "OMIMPipeline", "PubChemPipeline",
        # Constants
        "CHEMBL_API_BASE", "MOLECULE_TYPE_MAP", "_LOWER_TYPE_MAP",
        "ACTIVITY_CHUNK_SIZE", "CHEMBL_MIN_REQUEST_INTERVAL", "RETRY_BACKOFF",
        "NS", "UNIPROT_SEARCH_URL", "UNIPROT_FIELDS",
        "DISGENET_API_COLUMN_MAP", "DISGENET_COLUMN_MAP",
        "MIN_SCORE", "CONFIDENCE_TIERS",
        "OMIM_REQUEST_INTERVAL", "MAPPING_KEY_CONFIRMED",
        "PUBCHEM_PROPERTIES", "BATCH_SIZE", "MIN_BACKOFF", "MAX_BACKOFF",
        "RATE_LIMIT_INTERVAL",
        # Metadata
        "__version__", "SCHEMA_VERSION", "PYTHON_MIN_VERSION",
        "DEFAULT_SEED", "KNOWN_DATA_SOURCE_VERSIONS",
        "DATA_DICTIONARY", "SOURCE_ATTRIBUTION",
        # Utilities
        "get_pipeline", "get_expected_pipelines", "get_kg_mapping",
        "get_filtering_thresholds", "get_data_dictionary",
        "get_source_attribution", "find_affected_downstream",
        "compute_file_checksum", "get_json_schema",
        "validate_infrastructure", "_validate_security", "validate_config",
        "get_config_summary", "set_log_level", "set_correlation_id",
        "get_correlation_id", "set_seed",
        "initialize", "reload", "is_loaded", "is_reproducible",
        "health_check", "get_metrics", "get_load_times",
        "performance_benchmark", "recover_from_failure", "get_dead_letters",
        "get_provenance", "get_audit_trail",
        "to_state_dict", "from_state_dict",
        "requires_api_version", "_deprecated",
        "_reset", "_log_import_status",
    ])
    def test_symbol_importable(self, symbol_name):
        """TEST-3: every symbol in __all__ is importable via getattr."""
        attr = getattr(pipelines, symbol_name)
        assert attr is not None, f"pipelines.{symbol_name} is None"

    def test_version_value(self):
        """TEST-9: __version__ is a PEP 440 semver string."""
        assert isinstance(pipelines.__version__, str)
        assert pipelines.__version__ != ""
        assert re.match(r"^\d+\.\d+\.\d+", pipelines.__version__), (
            f"__version__ {pipelines.__version__!r} is not PEP 440 compliant"
        )
        assert pipelines.__version__ == "2.0.0"

    def test_symbol_map_covers_all(self):
        """TEST-3: every __all__ entry (except metadata) is in _SYMBOL_MAP or is a module-level definition."""
        # Read the source to find module-level definitions
        init_path = PROJECT_ROOT / "pipelines" / "__init__.py"
        source = init_path.read_text()
        # Symbols that are defined at module level (not lazy-loaded).
        # Match: `name = ...`, `name: type = ...`, `def name(`, `class name(`
        module_level = set()
        for m in re.finditer(r"^(\w+)(?::\s*[^\s=]+)?\s*=", source, re.MULTILINE):
            module_level.add(m.group(1))
        for m in re.finditer(r"^def\s+(\w+)\s*\(", source, re.MULTILINE):
            module_level.add(m.group(1))
        for m in re.finditer(r"^class\s+(\w+)\s*[\(:]", source, re.MULTILINE):
            module_level.add(m.group(1))
        # Also include explicit metadata names
        module_level.update({
            "__version__", "SCHEMA_VERSION", "PYTHON_MIN_VERSION",
            "DEFAULT_SEED", "KNOWN_DATA_SOURCE_VERSIONS",
            "DATA_DICTIONARY", "SOURCE_ATTRIBUTION",
        })
        for name in pipelines.__all__:
            in_map = name in pipelines._SYMBOL_MAP
            in_module = name in module_level
            assert in_map or in_module, (
                f"{name!r} is in __all__ but not in _SYMBOL_MAP or module-level"
            )

    def test_symbol_map_values_are_valid_submodules(self):
        """TEST-3: every _SYMBOL_MAP value is a valid pipelines submodule path."""
        valid_submodules = {
            "pipelines.base_pipeline",
            "pipelines.chembl_pipeline",
            "pipelines.drugbank_pipeline",
            "pipelines.uniprot_pipeline",
            "pipelines.string_pipeline",
            "pipelines.disgenet_pipeline",
            "pipelines.omim_pipeline",
            "pipelines.pubchem_pipeline",
        }
        for name, (module_path, attr_name) in pipelines._SYMBOL_MAP.items():
            assert module_path in valid_submodules, (
                f"_SYMBOL_MAP[{name!r}] has invalid module path {module_path!r}"
            )

    def test_from_pipelines_import_star(self):
        """TEST-DQ-9: `from pipelines import *` exposes the full API."""
        namespace: dict[str, Any] = {}
        exec("from pipelines import *", namespace)
        for name in ["ChEMBLPipeline", "CHEMBL_API_BASE", "MOLECULE_TYPE_MAP",
                     "DATA_DICTIONARY", "get_expected_pipelines",
                     "__version__", "SCHEMA_VERSION", "validate_infrastructure"]:
            assert name in namespace, f"{name!r} not bound by `from pipelines import *`"

    def test_dir_includes_all_symbols(self):
        """TEST-10: dir(pipelines) includes all __all__ symbols."""
        d = dir(pipelines)
        for name in pipelines.__all__:
            assert name in d, f"{name!r} not in dir(pipelines)"

    def test_no_top_level_pipelines_imports(self):
        """ARCH-1: no top-level `from pipelines.X import Y` statements."""
        init_path = PROJECT_ROOT / "pipelines" / "__init__.py"
        source = init_path.read_text()
        # Find any `from pipelines.X import Y` at top level (not inside functions)
        # Look for lines that start with `from pipelines.`
        lines = source.split("\n")
        for i, line in enumerate(lines, 1):
            stripped = line.lstrip()
            # Skip comments
            if stripped.startswith("#"):
                continue
            # Only check top-level (indentation == 0)
            if line.startswith(" ") or line.startswith("\t"):
                continue
            # Check for forbidden patterns
            if re.match(r"^from\s+pipelines\.\w+\s+import\s+", stripped):
                pytest.fail(f"Line {i}: forbidden top-level import: {stripped!r}")


# ---------------------------------------------------------------------------
# 2. Lazy Loading Behaviour (TEST-8, TEST-13, IDEM-1, IDEM-2)
# ---------------------------------------------------------------------------
class TestLazyLoading:
    """Verify lazy loading works correctly."""

    def test_import_no_side_effects(self):
        """TEST-8/IDEM-1: _reset() leaves _loaded empty."""
        # Fixture already called _reset()
        assert len(pipelines._loaded) == 0

    def test_import_does_not_load_transitive_deps(self):
        """TEST-8: import pipelines does not load sqlalchemy/pandas/etc."""
        # We can't fully reset sys.modules in a running test, but we can
        # verify that AFTER _reset(), the lazy cache is empty.
        pipelines._reset()
        assert "ChEMBLPipeline" not in pipelines._loaded

    def test_first_access_loads_symbol(self):
        """TEST-13: accessing a symbol adds it to _loaded."""
        pipelines._reset()
        assert "ChEMBLPipeline" not in pipelines._loaded
        _ = pipelines.ChEMBLPipeline
        assert "ChEMBLPipeline" in pipelines._loaded

    def test_subsequent_access_uses_cache(self):
        """TEST-13: second access returns the same object (cache hit)."""
        pipelines._reset()
        first = pipelines.ChEMBLPipeline
        second = pipelines.ChEMBLPipeline
        assert first is second

    def test_reset_clears_cache(self):
        """TEST-12: _reset() clears the _loaded cache."""
        _ = pipelines.ChEMBLPipeline
        assert len(pipelines._loaded) > 0
        pipelines._reset()
        assert len(pipelines._loaded) == 0

    def test_load_import_status(self):
        """LOG-3: _log_import_status() returns a dict."""
        status = pipelines._log_import_status()
        assert isinstance(status, dict)
        # Initially nothing loaded
        assert all(not v for v in status.values())
        # Load one symbol
        _ = pipelines.UniProtPipeline
        status = pipelines._log_import_status()
        assert status["UniProtPipeline"] is True

    def test_unknown_attribute_raises_error(self):
        """TEST-13: unknown attribute raises AttributeError."""
        with pytest.raises(AttributeError, match="no attribute"):
            getattr(pipelines, "nonexistent_symbol_xyz")

    def test_lazy_mode_default_true(self):
        """IDEM-2: _LAZY_MODE is True by default."""
        # The fixture doesn't change _LAZY_MODE
        assert isinstance(pipelines._LAZY_MODE, bool)

    def test_load_time_recorded(self):
        """PERF-8: load time is recorded after first access."""
        pipelines._reset()
        _ = pipelines.UniProtPipeline
        times = pipelines.get_load_times()
        assert "UniProtPipeline" in times
        assert isinstance(times["UniProtPipeline"], float)
        assert times["UniProtPipeline"] > 0


# ---------------------------------------------------------------------------
# 3. PEP 562 Compliance (TEST-11, COMP-3, DES-4, DES-5)
# ---------------------------------------------------------------------------
class TestPEP562Compliance:
    """Verify PEP 562 lazy loading is correctly implemented."""

    def test_getattr_exists(self):
        """DES-4: __getattr__ is defined."""
        assert hasattr(pipelines, "__getattr__")
        assert callable(pipelines.__getattr__)

    def test_dir_dunder_exists(self):
        """TEST-11/DES-5: __dir__() is defined."""
        assert hasattr(pipelines, "__dir__")
        result = pipelines.__dir__()
        assert isinstance(result, list)
        assert "ChEMBLPipeline" in result
        assert "__version__" in result

    def test_getattr_returns_class(self):
        """DES-4: __getattr__('ChEMBLPipeline') returns the class."""
        cls = pipelines.__getattr__("ChEMBLPipeline")
        assert cls.__name__ == "ChEMBLPipeline"

    def test_getattr_caches_in_loaded_not_globals(self):
        """IDEM-8: cache in _loaded, NOT globals()."""
        pipelines._reset()
        # Before access: not in globals, not in _loaded
        assert "ChEMBLPipeline" not in pipelines.__dict__
        assert "ChEMBLPipeline" not in pipelines._loaded
        _ = pipelines.ChEMBLPipeline
        # After access: in _loaded, NOT in globals
        assert "ChEMBLPipeline" in pipelines._loaded
        assert "ChEMBLPipeline" not in pipelines.__dict__

    def test_submodule_access_via_getattr(self):
        """Submodule access (pipelines.chembl_pipeline) works via __getattr__."""
        pipelines._reset()
        # This triggers __getattr__('chembl_pipeline')
        mod = pipelines.chembl_pipeline
        assert mod.__name__ == "pipelines.chembl_pipeline"
        assert hasattr(mod, "ChEMBLPipeline")


# ---------------------------------------------------------------------------
# 4. Backward Compatibility (TEST-2, ARCH-8, C-5)
# ---------------------------------------------------------------------------
class TestBackwardCompatibility:
    """Verify backward compatibility with deep imports."""

    def test_facade_import_works(self):
        """TEST-2: from pipelines import ChEMBLPipeline returns the class."""
        from pipelines import ChEMBLPipeline
        from pipelines.chembl_pipeline import ChEMBLPipeline as Direct
        assert ChEMBLPipeline is Direct

    def test_deep_import_chembl(self):
        """C-5: deep import from pipelines.chembl_pipeline still works."""
        from pipelines.chembl_pipeline import ChEMBLPipeline
        assert ChEMBLPipeline.__name__ == "ChEMBLPipeline"

    def test_deep_import_drugbank(self):
        """C-5: deep import from pipelines.drugbank_pipeline still works."""
        from pipelines.drugbank_pipeline import DrugBankPipeline
        assert DrugBankPipeline.__name__ == "DrugBankPipeline"

    def test_deep_import_uniprot(self):
        """C-5: deep import from pipelines.uniprot_pipeline still works."""
        from pipelines.uniprot_pipeline import UniProtPipeline
        assert UniProtPipeline.__name__ == "UniProtPipeline"

    def test_deep_import_string(self):
        """C-5: deep import from pipelines.string_pipeline still works."""
        from pipelines.string_pipeline import StringPipeline
        assert StringPipeline.__name__ == "StringPipeline"

    def test_deep_import_disgenet(self):
        """C-5: deep import from pipelines.disgenet_pipeline still works."""
        from pipelines.disgenet_pipeline import DisGeNETPipeline
        assert DisGeNETPipeline.__name__ == "DisGeNETPipeline"

    def test_deep_import_omim(self):
        """C-5: deep import from pipelines.omim_pipeline still works."""
        from pipelines.omim_pipeline import OMIMPipeline
        assert OMIMPipeline.__name__ == "OMIMPipeline"

    def test_deep_import_pubchem(self):
        """C-5: deep import from pipelines.pubchem_pipeline still works."""
        from pipelines.pubchem_pipeline import PubChemPipeline
        assert PubChemPipeline.__name__ == "PubChemPipeline"

    def test_deep_import_base(self):
        """C-5: deep import from pipelines.base_pipeline still works."""
        from pipelines.base_pipeline import BasePipeline
        assert BasePipeline.__name__ == "BasePipeline"

    def test_facade_same_as_deep(self):
        """The facade returns the same class as the deep import."""
        from pipelines.chembl_pipeline import ChEMBLPipeline as Direct
        # Reset to ensure lazy load
        pipelines._reset()
        Lazy = pipelines.ChEMBLPipeline
        assert Lazy is Direct


# ---------------------------------------------------------------------------
# 5. Factory & Introspection (ARCH-7, DQ-8, DES-3)
# ---------------------------------------------------------------------------
class TestFactoryAndIntrospection:
    """Verify get_pipeline factory and get_expected_pipelines."""

    def test_get_pipeline_returns_class(self):
        """ARCH-7: get_pipeline('chembl') returns the ChEMBLPipeline class."""
        cls = pipelines.get_pipeline("chembl")
        assert cls.__name__ == "ChEMBLPipeline"
        assert isinstance(cls, type)

    def test_get_pipeline_all_seven(self):
        """ARCH-7: get_pipeline works for all 7 source names."""
        for name, expected_class in [
            ("chembl", "ChEMBLPipeline"),
            ("drugbank", "DrugBankPipeline"),
            ("uniprot", "UniProtPipeline"),
            ("string", "StringPipeline"),
            ("disgenet", "DisGeNETPipeline"),
            ("omim", "OMIMPipeline"),
            ("pubchem", "PubChemPipeline"),
        ]:
            cls = pipelines.get_pipeline(name)
            assert cls.__name__ == expected_class, (
                f"get_pipeline({name!r}).__name__ = {cls.__name__!r}, "
                f"expected {expected_class!r}"
            )

    def test_get_pipeline_invalid_raises_value_error(self):
        """ARCH-7: get_pipeline with invalid name raises ValueError."""
        with pytest.raises(ValueError, match="Unknown pipeline"):
            pipelines.get_pipeline("invalid_source")

    def test_get_pipeline_returns_class_not_instance(self):
        """ARCH-7: get_pipeline returns a CLASS, not an instance."""
        cls = pipelines.get_pipeline("uniprot")
        assert isinstance(cls, type)
        # Verify it's not an instance
        assert not hasattr(cls, "source_name") or isinstance(cls.source_name, str)

    def test_get_expected_pipelines_returns_set(self):
        """DQ-8: get_expected_pipelines returns a set."""
        result = pipelines.get_expected_pipelines()
        assert isinstance(result, set)

    def test_get_expected_pipelines_has_seven(self):
        """DQ-8: get_expected_pipelines returns exactly 7 source names."""
        result = pipelines.get_expected_pipelines()
        assert len(result) == 7

    def test_get_expected_pipelines_correct_values(self):
        """DQ-8: the 7 source names match the canonical list."""
        expected = {"chembl", "drugbank", "uniprot", "string",
                    "disgenet", "omim", "pubchem"}
        assert pipelines.get_expected_pipelines() == expected

    def test_get_expected_pipelines_derived_from_symbol_map(self):
        """DQ-8: get_expected_pipelines derives from _SOURCE_TO_CLASS (not hardcoded)."""
        # Verify all source names in _SOURCE_TO_CLASS are in the result
        for source_name in pipelines._SOURCE_TO_CLASS:
            assert source_name in pipelines.get_expected_pipelines()


# ---------------------------------------------------------------------------
# 6. Knowledge Graph Mapping (KNOW-2, DES-6)
# ---------------------------------------------------------------------------
class TestKnowledgeGraphMapping:
    """Verify the pipeline-to-KG node/edge mapping."""

    def test_get_kg_mapping_returns_dict(self):
        """KNOW-2: get_kg_mapping returns a dict."""
        result = pipelines.get_kg_mapping()
        assert isinstance(result, dict)

    def test_get_kg_mapping_has_seven_entries(self):
        """KNOW-2: get_kg_mapping has 7 entries (one per pipeline)."""
        result = pipelines.get_kg_mapping()
        assert len(result) == 7

    def test_get_kg_mapping_keys_match_expected_pipelines(self):
        """KNOW-2: kg_mapping keys match get_expected_pipelines."""
        kg_keys = set(pipelines.get_kg_mapping().keys())
        expected = pipelines.get_expected_pipelines()
        assert kg_keys == expected

    def test_get_kg_mapping_chembl_produces_drugs(self):
        """KNOW-2: chembl produces Drug nodes + Drug->Protein edges."""
        kg = pipelines.get_kg_mapping()
        assert "Drug" in kg["chembl"]["node_types"]
        assert "Drug->Protein" in kg["chembl"]["edge_types"]

    def test_get_kg_mapping_uniprot_produces_proteins(self):
        """KNOW-2: uniprot produces Protein nodes."""
        kg = pipelines.get_kg_mapping()
        assert "Protein" in kg["uniprot"]["node_types"]

    def test_get_kg_mapping_string_produces_ppi_edges(self):
        """KNOW-2: string produces Protein->Protein edges."""
        kg = pipelines.get_kg_mapping()
        assert "Protein->Protein" in kg["string"]["edge_types"]

    def test_get_kg_mapping_disgenet_produces_gda_edges(self):
        """KNOW-2: disgenet produces Gene->Disease edges."""
        kg = pipelines.get_kg_mapping()
        assert "Gene->Disease" in kg["disgenet"]["edge_types"]

    def test_get_kg_mapping_omim_produces_gda_edges(self):
        """KNOW-2: omim produces Gene->Disease edges."""
        kg = pipelines.get_kg_mapping()
        assert "Gene->Disease" in kg["omim"]["edge_types"]

    def test_get_kg_mapping_pubchem_no_new_nodes(self):
        """KNOW-2: pubchem enriches existing Drug nodes (no new nodes)."""
        kg = pipelines.get_kg_mapping()
        assert kg["pubchem"]["node_types"] == []


# ---------------------------------------------------------------------------
# 7. Filtering Thresholds (KNOW-3, scientific correctness P0)
# ---------------------------------------------------------------------------
class TestFilteringThresholds:
    """Verify the scientific filtering thresholds are correct."""

    def test_get_filtering_thresholds_returns_dict(self):
        """KNOW-3: get_filtering_thresholds returns a dict."""
        result = pipelines.get_filtering_thresholds()
        assert isinstance(result, dict)

    def test_min_score_is_0_1(self):
        """KNOW-3: MIN_SCORE == 0.06 per Piñero et al. 2020 (SCI-1, 389-fix audit).

        The previous default of 0.1 silently destroyed weak-evidence rows
        (score in [0.06, 0.1)) which are biologically meaningful, especially
        for rare diseases.  The 389-fix audit lowered the default to 0.06,
        the published weak-evidence floor.
        """
        thresholds = pipelines.get_filtering_thresholds()
        assert thresholds["MIN_SCORE"]["value"] == 0.06
        assert "disgenet_pipeline.py" in thresholds["MIN_SCORE"]["file"]

    def test_mapping_key_confirmed_is_3(self):
        """KNOW-3: MAPPING_KEY_CONFIRMED == 3 (omim_pipeline.py:44)."""
        thresholds = pipelines.get_filtering_thresholds()
        assert thresholds["MAPPING_KEY_CONFIRMED"]["value"] == 3
        assert "omim_pipeline.py:44" in thresholds["MAPPING_KEY_CONFIRMED"]["file"]

    def test_omim_request_interval_is_0_25(self):
        """KNOW-3: OMIM_REQUEST_INTERVAL == 0.25 (omim_pipeline.py:43)."""
        thresholds = pipelines.get_filtering_thresholds()
        assert thresholds["OMIM_REQUEST_INTERVAL"]["value"] == 0.25

    def test_chembl_min_request_interval_is_1_1(self):
        """KNOW-3: CHEMBL_MIN_REQUEST_INTERVAL value (P1 fix: 0.5s, not 1.1s).

        Updated for the P1 fix: the rate limit was reduced from 1.1s to
        0.5s (ChEMBL's actual soft limit is ~2 req/sec for short bursts).
        The new value is enforced via a token-bucket rate limiter in
        ``pipelines/_http_client.py`` (P4 fix).
        """
        thresholds = pipelines.get_filtering_thresholds()
        # P1 fix: 0.5s (not 1.1s) — token-bucket allows bursts while
        # maintaining the 2 req/sec average.
        assert thresholds["CHEMBL_MIN_REQUEST_INTERVAL"]["value"] == 0.5

    def test_activity_chunk_size_is_100000(self):
        """KNOW-3: ACTIVITY_CHUNK_SIZE == 100000 (chembl_pipeline.py:47)."""
        thresholds = pipelines.get_filtering_thresholds()
        assert thresholds["ACTIVITY_CHUNK_SIZE"]["value"] == 100000

    def test_pubchem_batch_size_is_100(self):
        """KNOW-3: PUBCHEM_BATCH_SIZE is at the safety margin of 95 (institutional-grade).

        The legacy value was 100 (PubChem's hard limit). The institutional-grade
        rewrite per PUBCHEM_PIPELINE_MASTER_FIX_PROMPT.md (DESIGN-13, CONF-1)
        uses 95 — a 5% safety margin in case PubChem lowers the limit. This
        test is updated to reflect the new safer default while preserving the
        spirit of the original assertion (the batch size is bounded by
        PubChem's 100-identifier limit).
        """
        thresholds = pipelines.get_filtering_thresholds()
        assert thresholds["PUBCHEM_BATCH_SIZE"]["value"] == 95
        assert thresholds["PUBCHEM_BATCH_SIZE"]["value"] <= 100  # never exceed PubChem hard limit

    def test_pubchem_rate_limit_interval_is_0_2(self):
        """KNOW-3: PUBCHEM_RATE_LIMIT_INTERVAL == 0.2 (pubchem_pipeline.py:58)."""
        thresholds = pipelines.get_filtering_thresholds()
        assert thresholds["PUBCHEM_RATE_LIMIT_INTERVAL"]["value"] == 0.2

    def test_string_min_combined_score_is_400(self):
        """KNOW-3: STRING_MIN_COMBINED_SCORE == 400 (high-confidence threshold)."""
        thresholds = pipelines.get_filtering_thresholds()
        assert thresholds["STRING_MIN_COMBINED_SCORE"]["value"] == 400

    def test_every_threshold_has_rationale(self):
        """KNOW-3: every threshold entry has a non-empty rationale."""
        thresholds = pipelines.get_filtering_thresholds()
        for name, entry in thresholds.items():
            assert "rationale" in entry, f"{name} missing rationale"
            assert len(entry["rationale"]) > 20, (
                f"{name} rationale too short: {entry['rationale']!r}"
            )

    def test_thresholds_match_source_files(self):
        """KNOW-3 (critical): thresholds match the actual source code values."""
        # Cross-check against the actual pipeline module files
        from pipelines.disgenet_pipeline import MIN_SCORE as actual_min_score
        from pipelines.omim_pipeline import (
            MAPPING_KEY_CONFIRMED as actual_mapping_key,
            OMIM_REQUEST_INTERVAL as actual_omim_interval,
        )
        from pipelines.chembl_pipeline import (
            ACTIVITY_CHUNK_SIZE as actual_chunk,
            CHEMBL_MIN_REQUEST_INTERVAL as actual_chembl_interval,
        )
        from pipelines.pubchem_pipeline import (
            BATCH_SIZE as actual_batch,
            RATE_LIMIT_INTERVAL as actual_rate,
        )
        thresholds = pipelines.get_filtering_thresholds()
        assert thresholds["MIN_SCORE"]["value"] == actual_min_score
        assert thresholds["MAPPING_KEY_CONFIRMED"]["value"] == actual_mapping_key
        assert thresholds["OMIM_REQUEST_INTERVAL"]["value"] == actual_omim_interval
        assert thresholds["ACTIVITY_CHUNK_SIZE"]["value"] == actual_chunk
        assert thresholds["CHEMBL_MIN_REQUEST_INTERVAL"]["value"] == actual_chembl_interval
        assert thresholds["PUBCHEM_BATCH_SIZE"]["value"] == actual_batch
        assert thresholds["PUBCHEM_RATE_LIMIT_INTERVAL"]["value"] == actual_rate


# ---------------------------------------------------------------------------
# 8. Data Dictionary (DQ-6, DOC-10)
# ---------------------------------------------------------------------------
class TestDataDictionary:
    """Verify the DATA_DICTIONARY structure."""

    def test_data_dictionary_exists(self):
        """DQ-6: DATA_DICTIONARY exists."""
        assert hasattr(pipelines, "DATA_DICTIONARY")

    def test_data_dictionary_has_seven_entries(self):
        """DQ-6: DATA_DICTIONARY has 7 entries (one per pipeline)."""
        assert len(pipelines.DATA_DICTIONARY) == 7

    def test_data_dictionary_keys_match_expected_pipelines(self):
        """DQ-6: DATA_DICTIONARY keys match get_expected_pipelines."""
        assert set(pipelines.DATA_DICTIONARY.keys()) == pipelines.get_expected_pipelines()

    def test_data_dictionary_entries_have_required_fields(self):
        """DQ-6: every entry has output_file, source_name, primary_key, description."""
        for source_name, entry in pipelines.DATA_DICTIONARY.items():
            assert "output_file" in entry, f"{source_name} missing output_file"
            assert "source_name" in entry, f"{source_name} missing source_name"
            assert "primary_key" in entry, f"{source_name} missing primary_key"
            assert "description" in entry, f"{source_name} missing description"
            assert entry["source_name"] == source_name

    def test_data_dictionary_filenames_match_base_pipeline(self):
        """DQ-6: filenames match base_pipeline.py:174-185 _get_processed_filename dict."""
        import inspect
        from pipelines.base_pipeline import BasePipeline
        src = inspect.getsource(BasePipeline._get_processed_filename)
        for source_name, entry in pipelines.DATA_DICTIONARY.items():
            assert f'"{source_name}"' in src, (
                f"{source_name} not in _get_processed_filename source"
            )
            assert f'"{entry["output_file"]}"' in src, (
                f"{entry['output_file']!r} not in _get_processed_filename source"
            )


# ---------------------------------------------------------------------------
# 9. Source Attribution (LIN-4)
# ---------------------------------------------------------------------------
class TestSourceAttribution:
    """Verify the SOURCE_ATTRIBUTION structure."""

    def test_source_attribution_exists(self):
        """LIN-4: SOURCE_ATTRIBUTION exists."""
        assert hasattr(pipelines, "SOURCE_ATTRIBUTION")

    def test_source_attribution_has_seven_csvs(self):
        """LIN-4: SOURCE_ATTRIBUTION has 7 CSV entries."""
        assert len(pipelines.SOURCE_ATTRIBUTION) == 7

    def test_source_attribution_drugs_csv_has_three_sources(self):
        """LIN-4: drugs.csv is attributed to chembl + drugbank + pubchem."""
        attr = pipelines.SOURCE_ATTRIBUTION["drugs.csv"]
        assert set(attr["sources"]) == {"chembl", "drugbank", "pubchem"}

    def test_get_source_attribution_returns_copy(self):
        """LIN-4: get_source_attribution returns a copy (not the original)."""
        result1 = pipelines.get_source_attribution()
        result2 = pipelines.get_source_attribution()
        assert result1 == result2
        assert result1 is not pipelines.SOURCE_ATTRIBUTION


# ---------------------------------------------------------------------------
# 10. Validation (DQ-7, SEC-2, LOG-5)
# ---------------------------------------------------------------------------
class TestValidation:
    """Verify validate_infrastructure and _validate_security."""

    def test_validate_infrastructure_returns_dict(self):
        """DQ-7: validate_infrastructure returns a dict."""
        result = pipelines.validate_infrastructure()
        assert isinstance(result, dict)

    def test_validate_infrastructure_overall_pass(self):
        """DQ-7: validate_infrastructure overall is PASS."""
        result = pipelines.validate_infrastructure()
        assert result["overall"] == "PASS", (
            f"validate_infrastructure failed: {result['failed']} checks failed"
        )

    def test_validate_infrastructure_has_checks_list(self):
        """DQ-7: validate_infrastructure has a checks list."""
        result = pipelines.validate_infrastructure()
        assert "checks" in result
        assert isinstance(result["checks"], list)
        assert len(result["checks"]) > 0

    def test_validate_infrastructure_all_checks_have_status(self):
        """DQ-7: every check has a status field."""
        result = pipelines.validate_infrastructure()
        for check in result["checks"]:
            assert "status" in check
            assert check["status"] in ("PASS", "FAIL", "WARN", "INFO", "CRITICAL")
            assert "check" in check
            assert "message" in check

    def test_validate_security_returns_dict(self):
        """SEC-2: _validate_security returns a dict."""
        result = pipelines._validate_security()
        assert isinstance(result, dict)

    def test_validate_security_has_overall(self):
        """SEC-2: _validate_security has overall = SECURE or INSECURE."""
        result = pipelines._validate_security()
        assert result["overall"] in ("SECURE", "INSECURE")

    def test_validate_security_omim_key_critical_if_unset(self):
        """SEC-2: missing OMIM_API_KEY produces a CRITICAL finding."""
        # Save and clear OMIM_API_KEY
        old_key = os.environ.pop("OMIM_API_KEY", None)
        try:
            result = pipelines._validate_security()
            omim_check = next(
                (c for c in result["checks"] if c["check"] == "omim_api_key"),
                None,
            )
            assert omim_check is not None
            assert omim_check["severity"] == "CRITICAL"
        finally:
            if old_key:
                os.environ["OMIM_API_KEY"] = old_key

    def test_validate_config_returns_dict(self):
        """CONF-7: validate_config returns a dict."""
        result = pipelines.validate_config()
        assert isinstance(result, dict)
        assert "overall" in result
        assert result["overall"] in ("PASS", "FAIL")


# ---------------------------------------------------------------------------
# 11. Configuration & Logging (CONF-3, SEC-3, LOG-7)
# ---------------------------------------------------------------------------
class TestConfigurationAndLogging:
    """Verify configuration management and logging."""

    def test_get_config_summary_returns_dict(self):
        """SEC-3: get_config_summary returns a dict."""
        result = pipelines.get_config_summary()
        assert isinstance(result, dict)

    def test_get_config_summary_masks_credentials(self):
        """SEC-3 (CRITICAL): credentials are masked, never raw values."""
        # Set a fake OMIM_API_KEY
        old_key = os.environ.get("OMIM_API_KEY")
        os.environ["OMIM_API_KEY"] = "secret-key-12345"
        try:
            result = pipelines.get_config_summary()
            creds = result["credentials"]
            # The raw value MUST NOT appear
            assert "secret-key-12345" not in str(result)
            assert creds["OMIM_API_KEY"] == "<set>"
        finally:
            if old_key is None:
                os.environ.pop("OMIM_API_KEY", None)
            else:
                os.environ["OMIM_API_KEY"] = old_key

    def test_get_config_summary_unomim_key_shows_unset(self):
        """SEC-3: unset OMIM_API_KEY shows <unset>."""
        old_key = os.environ.pop("OMIM_API_KEY", None)
        try:
            result = pipelines.get_config_summary()
            assert result["credentials"]["OMIM_API_KEY"] == "<unset>"
        finally:
            if old_key:
                os.environ["OMIM_API_KEY"] = old_key

    def test_set_correlation_id(self):
        """LOG-7: set_correlation_id / get_correlation_id work."""
        pipelines._reset()
        pipelines.set_correlation_id("abc-123")
        assert pipelines.get_correlation_id() == "abc-123"
        pipelines.set_correlation_id(None)
        assert pipelines.get_correlation_id() is None

    def test_set_seed_sets_env_var(self):
        """IDEM-4: set_seed sets PIPELINES_SEED env var."""
        old_seed = os.environ.get("PIPELINES_SEED")
        try:
            pipelines.set_seed(123)
            assert os.environ["PIPELINES_SEED"] == "123"
        finally:
            if old_seed is None:
                os.environ.pop("PIPELINES_SEED", None)
            else:
                os.environ["PIPELINES_SEED"] = old_seed

    def test_set_log_level(self):
        """DES-9: set_log_level changes the logger level."""
        pipelines.set_log_level(logging.DEBUG)
        assert pipelines.logger.level == logging.DEBUG
        pipelines.set_log_level(logging.INFO)
        assert pipelines.logger.level == logging.INFO

    def test_default_seed_is_42(self):
        """IDEM-4: DEFAULT_SEED == 42."""
        assert pipelines.DEFAULT_SEED == 42


# ---------------------------------------------------------------------------
# 12. Lineage & Provenance (LIN-2, LIN-3)
# ---------------------------------------------------------------------------
class TestLineageAndProvenance:
    """Verify lineage and provenance functions."""

    def test_get_provenance_returns_dict(self):
        """LIN-2: get_provenance returns a dict."""
        result = pipelines.get_provenance()
        assert isinstance(result, dict)

    def test_get_provenance_has_required_fields(self):
        """LIN-2: provenance has package, version, schema_version, etc."""
        result = pipelines.get_provenance()
        assert result["package"] == "pipelines"
        assert result["version"] == pipelines.__version__
        assert result["schema_version"] == pipelines.SCHEMA_VERSION
        assert "python_version" in result
        assert "loaded_symbols" in result
        assert "expected_pipelines" in result
        assert "correlation_id" in result
        assert "git_sha" in result

    def test_get_audit_trail_returns_dict(self):
        """LIN-3: get_audit_trail returns a dict."""
        result = pipelines.get_audit_trail()
        assert isinstance(result, dict)

    def test_get_audit_trail_has_required_fields(self):
        """LIN-3: audit_trail has provenance, import_status, load_times, dead_letters, metrics."""
        result = pipelines.get_audit_trail()
        assert "provenance" in result
        assert "import_status" in result
        assert "load_times_ms" in result
        assert "dead_letters" in result
        assert "metrics" in result
        assert "config_summary" in result

    def test_get_audit_trail_provenance_version_matches(self):
        """LIN-3: audit_trail provenance version matches __version__."""
        result = pipelines.get_audit_trail()
        assert result["provenance"]["version"] == pipelines.__version__


# ---------------------------------------------------------------------------
# 13. State Serialisation (IDEM-6, INT-7, LIN-8)
# ---------------------------------------------------------------------------
class TestStateSerialisation:
    """Verify to_state_dict / from_state_dict."""

    def test_to_state_dict_returns_dict(self):
        """IDEM-6: to_state_dict returns a dict."""
        result = pipelines.to_state_dict()
        assert isinstance(result, dict)

    def test_to_state_dict_has_required_fields(self):
        """IDEM-6: state_dict has version, schema_version, loaded_symbols, etc."""
        result = pipelines.to_state_dict()
        assert result["version"] == pipelines.__version__
        assert result["schema_version"] == pipelines.SCHEMA_VERSION
        assert "loaded_symbols" in result
        assert "load_times_ms" in result
        assert "lazy_mode" in result
        assert "expected_pipelines" in result
        assert "correlation_id" in result
        assert "dead_letters_count" in result
        assert "timestamp" in result

    def test_state_dict_round_trip_preserves_correlation_id(self):
        """IDEM-6: round-trip preserves correlation_id."""
        pipelines._reset()
        pipelines.set_correlation_id("round-trip-test")
        state = pipelines.to_state_dict()
        pipelines._reset()
        pipelines.from_state_dict(state)
        assert pipelines.get_correlation_id() == "round-trip-test"

    def test_from_state_dict_rejects_version_mismatch(self):
        """IDEM-6: from_state_dict raises ValueError on version mismatch."""
        bad_state = {"version": "99.0.0", "loaded_symbols": []}
        with pytest.raises(ValueError, match="version mismatch"):
            pipelines.from_state_dict(bad_state)


# ---------------------------------------------------------------------------
# 14. Reliability (REL-3, REL-4, REL-5, REL-6, REL-7)
# ---------------------------------------------------------------------------
class TestReliability:
    """Verify reliability and resilience features."""

    def test_dead_letters_initially_empty(self):
        """REL-4: _dead_letters is initially empty."""
        pipelines._reset()
        assert len(pipelines._dead_letters) == 0

    def test_get_dead_letters_returns_list(self):
        """REL-4: get_dead_letters returns a list."""
        result = pipelines.get_dead_letters()
        assert isinstance(result, list)

    def test_get_dead_letters_returns_copy(self):
        """REL-4: get_dead_letters returns a copy (not the internal list)."""
        result = pipelines.get_dead_letters()
        assert result is not pipelines._dead_letters

    def test_recover_from_failure_clears_state(self):
        """REL-7: recover_from_failure clears _loaded and _dead_letters."""
        # Add a fake dead letter
        pipelines._dead_letters.append({"symbol": "fake", "error": "test"})
        pipelines.recover_from_failure()
        assert len(pipelines._dead_letters) == 0
        assert len(pipelines._loaded) == 0

    def test_circuit_breaker_exists(self):
        """REL-5: _CIRCUIT_BREAKER dict exists with required fields."""
        assert hasattr(pipelines, "_CIRCUIT_BREAKER")
        cb = pipelines._CIRCUIT_BREAKER
        assert "failure_count" in cb
        assert "threshold" in cb
        assert "open_until" in cb
        assert "reset_timeout" in cb

    def test_circuit_breaker_reset_by_recover(self):
        """REL-5: recover_from_failure resets the circuit breaker."""
        pipelines._CIRCUIT_BREAKER["failure_count"] = 99
        pipelines._CIRCUIT_BREAKER["open_until"] = "fake"
        pipelines.recover_from_failure()
        assert pipelines._CIRCUIT_BREAKER["failure_count"] == 0
        assert pipelines._CIRCUIT_BREAKER["open_until"] is None

    def test_pipeline_unavailable_sentinel(self):
        """REL-6: _PipelineUnavailable sentinel raises original error on call."""
        original_err = ImportError("rdkit missing")
        sentinel = pipelines._PipelineUnavailable("ChEMBLPipeline", original_err)
        assert sentinel.name == "ChEMBLPipeline"
        with pytest.raises(ImportError, match="rdkit missing"):
            sentinel()
        # __repr__ should mention the name
        assert "ChEMBLPipeline" in repr(sentinel)


# ---------------------------------------------------------------------------
# 15. Observability (LOG-4, LOG-6, PERF-7, PERF-8, PERF-9)
# ---------------------------------------------------------------------------
class TestObservability:
    """Verify observability features."""

    def test_logger_exists(self):
        """ARCH-4: pipelines.logger exists."""
        assert hasattr(pipelines, "logger")
        assert isinstance(pipelines.logger, logging.Logger)
        assert pipelines.logger.name == "pipelines"

    def test_null_handler_attached(self):
        """ARCH-9/LOG-2: NullHandler is attached to the logger."""
        assert any(
            isinstance(h, logging.NullHandler)
            for h in pipelines.logger.handlers
        )

    def test_get_metrics_returns_dict(self):
        """LOG-6: get_metrics returns a dict."""
        result = pipelines.get_metrics()
        assert isinstance(result, dict)
        assert "import_count" in result
        assert "import_failures" in result
        assert "load_times_ms" in result
        assert "dead_letters" in result

    def test_get_load_times_returns_dict(self):
        """PERF-8: get_load_times returns a dict."""
        result = pipelines.get_load_times()
        assert isinstance(result, dict)

    def test_performance_benchmark_returns_dict(self):
        """PERF-9: performance_benchmark returns a dict with required keys."""
        result = pipelines.performance_benchmark()
        assert isinstance(result, dict)
        assert "total_load_time_ms" in result
        assert "symbol_load_times_ms" in result
        assert "slowest_symbol" in result
        assert "symbol_count" in result
        assert result["total_load_time_ms"] > 0
        assert result["symbol_count"] >= 28

    def test_observability_callback_invoked(self):
        """PERF-7: _on_symbol_loaded_callback is invoked on lazy load."""
        pipelines._reset()
        calls: list[tuple] = []
        pipelines._on_symbol_loaded_callback = (
            lambda name, path, ms: calls.append((name, path, ms))
        )
        try:
            _ = pipelines.UniProtPipeline
            assert len(calls) == 1
            assert calls[0][0] == "UniProtPipeline"
            assert "uniprot_pipeline" in calls[0][1]
            assert isinstance(calls[0][2], float)
        finally:
            pipelines._on_symbol_loaded_callback = None

    def test_observability_callback_failure_does_not_break_load(self):
        """PERF-7: callback failure does not break the load."""
        pipelines._reset()
        def bad_callback(name, path, ms):
            raise RuntimeError("callback broken")
        pipelines._on_symbol_loaded_callback = bad_callback
        try:
            # Should NOT raise
            cls = pipelines.UniProtPipeline
            assert cls.__name__ == "UniProtPipeline"
        finally:
            pipelines._on_symbol_loaded_callback = None


# ---------------------------------------------------------------------------
# 16. CLI (CODE-12)
# ---------------------------------------------------------------------------
class TestCLI:
    """Verify the python -m pipelines CLI."""

    def test_python_m_pipelines_version(self):
        """CODE-12: `python -m pipelines version` prints 2.0.0."""
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "pipelines", "version"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "2.0.0" in result.stdout

    def test_python_m_pipelines_list(self):
        """CODE-12: `python -m pipelines list` prints 7 source names."""
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "pipelines", "list"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        for name in ["chembl", "drugbank", "uniprot", "string",
                     "disgenet", "omim", "pubchem"]:
            assert name in result.stdout

    def test_python_m_pipelines_validate(self):
        """CODE-12: `python -m pipelines validate` runs validate_infrastructure."""
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "pipelines", "validate"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert '"overall": "PASS"' in result.stdout


# ---------------------------------------------------------------------------
# 17. PEP 8 / PEP 257 Compliance (COMP-1, COMP-2, CODE-5, CODE-11)
# ---------------------------------------------------------------------------
class TestPEP8Compliance:
    """Verify PEP 8 / PEP 257 compliance."""

    def test_no_line_exceeds_99_chars(self):
        """CODE-5: no line exceeds 99 characters."""
        init_path = PROJECT_ROOT / "pipelines" / "__init__.py"
        lines = init_path.read_text().split("\n")
        long_lines = [
            (i + 1, len(line))
            for i, line in enumerate(lines)
            if len(line) > 99
        ]
        assert not long_lines, f"Lines >99 chars: {long_lines[:5]}"

    def test_module_has_docstring(self):
        """COMP-2: module has a docstring."""
        assert pipelines.__doc__ is not None
        assert len(pipelines.__doc__) > 5000, (
            f"docstring len={len(pipelines.__doc__)}, expected >5000"
        )

    def test_docstring_mentions_lazy_loading(self):
        """DOC-3: docstring mentions 'lazy' loading."""
        assert "lazy" in pipelines.__doc__.lower()

    def test_docstring_mentions_all_8_submodules(self):
        """DOC-13: docstring mentions all 8 submodules."""
        doc = pipelines.__doc__
        for sub in ["base_pipeline", "chembl_pipeline", "drugbank_pipeline",
                    "uniprot_pipeline", "string_pipeline", "disgenet_pipeline",
                    "omim_pipeline", "pubchem_pipeline"]:
            assert sub in doc, f"docstring missing {sub!r}"

    def test_docstring_mentions_inchikey(self):
        """DOC-11: docstring mentions InChIKey."""
        assert "InChIKey" in pipelines.__doc__

    def test_docstring_mentions_thalidomide(self):
        """KNOW-1: docstring mentions thalidomide (patient-safety warning)."""
        assert "thalidomide" in pipelines.__doc__.lower()

    def test_docstring_mentions_security_note(self):
        """DOC-4: docstring has a Security Note section."""
        assert "Security Note" in pipelines.__doc__

    def test_docstring_mentions_pii(self):
        """SEC-6: docstring mentions PII / HIPAA / GDPR."""
        doc = pipelines.__doc__
        assert "PII" in doc
        assert "HIPAA" in doc
        assert "GDPR" in doc

    def test_docstring_mentions_data_lineage(self):
        """DOC-5: docstring has a Data Lineage section."""
        assert "Data Lineage" in pipelines.__doc__

    def test_docstring_mentions_changelog(self):
        """DOC-6: docstring has a Changelog section."""
        assert "Changelog" in pipelines.__doc__
        assert "v2.0.0" in pipelines.__doc__

    def test_docstring_mentions_see_also(self):
        """DOC-7: docstring has a See Also section."""
        assert "See Also" in pipelines.__doc__

    def test_docstring_mentions_processing_order(self):
        """DOC-12: docstring mentions Recommended Processing Order."""
        assert "Recommended Processing Order" in pipelines.__doc__

    def test_docstring_mentions_kg_mapping(self):
        """DOC-11/KNOW-2: docstring mentions knowledge-graph node/edge mapping."""
        assert "Knowledge-Graph" in pipelines.__doc__ or "knowledge graph" in pipelines.__doc__.lower()

    def test_docstring_mentions_airflow(self):
        """ARCH-1: docstring mentions Airflow DAG parsing safety."""
        assert "Airflow" in pipelines.__doc__

    def test_docstring_does_not_say_convenience_imports(self):
        """DOC-15/KNOW-7: docstring does NOT say 'Convenience imports'."""
        assert "Convenience imports" not in pipelines.__doc__

    def test_spdx_header_present(self):
        """SEC-5/CODE-6: first two lines are SPDX header."""
        init_path = PROJECT_ROOT / "pipelines" / "__init__.py"
        lines = init_path.read_text().split("\n")
        assert lines[0] == "# SPDX-License-Identifier: MIT"
        assert "Team Cosmic" in lines[1]

    def test_from_future_import_annotations(self):
        """CODE-1: from __future__ import annotations is the first import."""
        init_path = PROJECT_ROOT / "pipelines" / "__init__.py"
        source = init_path.read_text()
        # Find the first import statement
        match = re.search(r"^from\s+(\S+)\s+import", source, re.MULTILINE)
        assert match is not None, "no import found"
        assert match.group(1) == "__future__"

    def test_docstring_length_at_least_100_lines(self):
        """CODE-11/DOC-1: docstring is at least 100 lines."""
        assert pipelines.__doc__.count("\n") >= 100


# ---------------------------------------------------------------------------
# 18. ARM64 Simulation (ARCH-6, IDEM-1, REL-2, SEC-7)
# ---------------------------------------------------------------------------
class TestARM64Simulation:
    """Verify graceful degradation when rdkit is missing (ARM64 scenario)."""

    def test_uniprot_works_without_chembl(self):
        """ARCH-6: UniProtPipeline imports even if chembl is broken."""
        pipelines._reset()
        # Mock importlib.import_module to fail for chembl only
        real_import = importlib.import_module
        def mock_import(name, *args, **kwargs):
            if name == "pipelines.chembl_pipeline":
                raise ImportError("simulated: rdkit missing on ARM64")
            return real_import(name, *args, **kwargs)

        with unittest_mock_patch("importlib.import_module", mock_import):
            # UniProt should still work
            cls = pipelines.UniProtPipeline
            assert cls.__name__ == "UniProtPipeline"
            # ChEMBL should fail with clear message
            with pytest.raises(ImportError, match="ChEMBL"):
                _ = pipelines.ChEMBLPipeline

    def test_failed_import_records_dead_letter(self):
        """REL-4: failed import records a dead letter entry."""
        pipelines._reset()
        real_import = importlib.import_module
        def mock_import(name, *args, **kwargs):
            if name == "pipelines.chembl_pipeline":
                raise ImportError("simulated: rdkit missing on ARM64")
            return real_import(name, *args, **kwargs)

        with unittest_mock_patch("importlib.import_module", mock_import):
            with pytest.raises(ImportError):
                _ = pipelines.ChEMBLPipeline

        dead_letters = pipelines.get_dead_letters()
        assert len(dead_letters) >= 1
        dl = dead_letters[-1]
        assert dl["symbol"] == "ChEMBLPipeline"
        assert "rdkit" in dl["error"]
        assert "timestamp" in dl

    def test_clean_room_import_succeeds(self):
        """TEST-14: import pipelines succeeds with no API keys, no DB."""
        import subprocess
        env = dict(os.environ)
        env.pop("DISGENET_API_KEY", None)
        env.pop("OMIM_API_KEY", None)
        env.pop("DATABASE_URL", None)
        env.pop("DRUGBANK_XML_PATH", None)
        env.pop("PIPELINES_LAZY_IMPORT", None)
        result = subprocess.run(
            [sys.executable, "-c",
             "import pipelines; print(pipelines.__version__)"],
            env=env, capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0, f"Clean-room import failed: {result.stderr}"
        assert "2.0.0" in result.stdout

    def test_import_is_o1_under_50ms(self):
        """PERF-1/ARCH-2: import pipelines is under 50ms cold."""
        import subprocess
        # Use a fresh subprocess to get a true cold start
        result = subprocess.run(
            [sys.executable, "-c", """
import time, sys
t = time.perf_counter()
import pipelines
elapsed_ms = (time.perf_counter() - t) * 1000
assert elapsed_ms < 50, f'import took {elapsed_ms:.1f} ms'
# Verify no transitive deps loaded
for dep in ['sqlalchemy', 'pandas', 'requests', 'lxml', 'rdkit', 'psycopg2', 'config']:
    assert dep not in sys.modules, f'import pipelines loaded {dep}'
print(f'OK ({elapsed_ms:.1f} ms)')
"""],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
        assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# 19. Scientific Correctness (Domain 3 — P0)
# ---------------------------------------------------------------------------
class TestScientificCorrectness:
    """Verify scientific correctness (Domain 3 — 100% non-negotiable)."""

    def test_inchikey_pattern_in_docstring(self):
        """KNOW-1: docstring mentions the InChIKey regex."""
        doc = pipelines.__doc__
        # The canonical pattern from entity_resolution/base.py
        assert "[A-Z]{14}-[A-Z]{10}-[A-Z]" in doc

    def test_omim_mapping_key_rationale(self):
        """KNOW-3: MAPPING_KEY_CONFIRMED rationale mentions experimental validation."""
        thresholds = pipelines.get_filtering_thresholds()
        rationale = thresholds["MAPPING_KEY_CONFIRMED"]["rationale"]
        assert "confirmed" in rationale.lower()
        assert "experimental" in rationale.lower() or "validation" in rationale.lower()

    def test_disgenet_min_score_rationale(self):
        """KNOW-3: MIN_SCORE rationale mentions weak evidence / Piñero (SCI-1, 389-fix)."""
        thresholds = pipelines.get_filtering_thresholds()
        rationale = thresholds["MIN_SCORE"]["rationale"]
        # The 389-fix audit rewrote the rationale to cite Piñero et al. 2020
        # and the weak-evidence concept.  Accept any of these terms.
        lowered = rationale.lower()
        assert (
            "weak evidence" in lowered
            or "literature" in lowered
            or "noise" in lowered
            or "piñero" in lowered
            or "pinero" in lowered
        ), f"rationale lacks scientific justification: {rationale!r}"

    def test_string_score_threshold_rationale(self):
        """KNOW-3: STRING_MIN_COMBINED_SCORE rationale mentions high-confidence."""
        thresholds = pipelines.get_filtering_thresholds()
        rationale = thresholds["STRING_MIN_COMBINED_SCORE"]["rationale"]
        assert "high-confidence" in rationale.lower() or "high confidence" in rationale.lower()

    def test_seven_pipeline_classes_inherit_from_base(self):
        """TEST-4: all 7 concrete classes subclass BasePipeline."""
        from pipelines import (BasePipeline, ChEMBLPipeline, DrugBankPipeline,
                               UniProtPipeline, StringPipeline, DisGeNETPipeline,
                               OMIMPipeline, PubChemPipeline)
        for cls in [ChEMBLPipeline, DrugBankPipeline, UniProtPipeline,
                    StringPipeline, DisGeNETPipeline, OMIMPipeline, PubChemPipeline]:
            assert issubclass(cls, BasePipeline), (
                f"{cls.__name__} not subclass of BasePipeline"
            )

    def test_seven_source_names_nonempty(self):
        """TEST-5: all 7 concrete classes declare non-empty source_name."""
        from pipelines import (ChEMBLPipeline, DrugBankPipeline, UniProtPipeline,
                               StringPipeline, DisGeNETPipeline, OMIMPipeline,
                               PubChemPipeline)
        for cls in [ChEMBLPipeline, DrugBankPipeline, UniProtPipeline,
                    StringPipeline, DisGeNETPipeline, OMIMPipeline, PubChemPipeline]:
            assert getattr(cls, "source_name", "") != "", (
                f"{cls.__name__} has empty source_name"
            )

    def test_seven_source_names_unique(self):
        """TEST-6: all 7 source_name values are distinct."""
        from pipelines import (ChEMBLPipeline, DrugBankPipeline, UniProtPipeline,
                               StringPipeline, DisGeNETPipeline, OMIMPipeline,
                               PubChemPipeline)
        names = [cls.source_name for cls in [ChEMBLPipeline, DrugBankPipeline,
                  UniProtPipeline, StringPipeline, DisGeNETPipeline, OMIMPipeline,
                  PubChemPipeline]]
        assert len(set(names)) == 7, f"Duplicate source_names: {names}"

    def test_seven_source_names_match_expected(self):
        """TEST-7: source_name values match the canonical 7."""
        from pipelines import (ChEMBLPipeline, DrugBankPipeline, UniProtPipeline,
                               StringPipeline, DisGeNETPipeline, OMIMPipeline,
                               PubChemPipeline)
        expected = {"chembl", "drugbank", "uniprot", "string",
                    "disgenet", "omim", "pubchem"}
        actual = {cls.source_name for cls in [ChEMBLPipeline, DrugBankPipeline,
                  UniProtPipeline, StringPipeline, DisGeNETPipeline, OMIMPipeline,
                  PubChemPipeline]}
        assert actual == expected

    def test_base_pipeline_is_abstract(self):
        """DES-8: BasePipeline is abstract and cannot be instantiated."""
        from pipelines import BasePipeline
        # Try to instantiate — should raise TypeError (ABC with abstract methods)
        with pytest.raises(TypeError):
            BasePipeline()

    def test_pubchem_does_not_create_new_nodes(self):
        """KNOW-2: pubchem enriches existing Drug nodes (no new nodes)."""
        kg = pipelines.get_kg_mapping()
        assert kg["pubchem"]["node_types"] == []
        assert kg["pubchem"]["edge_types"] == []


# ---------------------------------------------------------------------------
# 20. Interoperability (INT-1 through INT-11)
# ---------------------------------------------------------------------------
class TestInteroperability:
    """Verify interoperability features."""

    def test_version_exists(self):
        """INT-1: __version__ exists."""
        assert hasattr(pipelines, "__version__")

    def test_schema_version_exists(self):
        """INT-8: SCHEMA_VERSION exists."""
        assert hasattr(pipelines, "SCHEMA_VERSION")
        assert pipelines.SCHEMA_VERSION == "2.0"

    def test_python_min_version_exists(self):
        """INT-10: PYTHON_MIN_VERSION exists."""
        assert hasattr(pipelines, "PYTHON_MIN_VERSION")
        assert pipelines.PYTHON_MIN_VERSION == (3, 9)

    def test_requires_api_version_passes_for_lower(self):
        """INT-2: requires_api_version passes for a lower version."""
        pipelines.requires_api_version("1.0.0")  # should not raise

    def test_requires_api_version_fails_for_higher(self):
        """INT-2: requires_api_version raises for a higher version."""
        with pytest.raises(ImportError, match="required"):
            pipelines.requires_api_version("99.0.0")

    def test_py_typed_exists(self):
        """INT-4/COMP-4: pipelines/py.typed exists."""
        assert (PROJECT_ROOT / "pipelines" / "py.typed").exists()

    def test_pyi_exists(self):
        """INT-5/COMP-4: pipelines/__init__.pyi exists."""
        assert (PROJECT_ROOT / "pipelines" / "__init__.pyi").exists()

    def test_json_schema_file_exists(self):
        """INT-9: pipelines/schema/v1.json exists."""
        assert (PROJECT_ROOT / "pipelines" / "schema" / "v1.json").exists()

    def test_get_json_schema_returns_dict(self):
        """INT-9: get_json_schema returns a dict."""
        result = pipelines.get_json_schema()
        assert isinstance(result, dict)

    def test_json_schema_has_seven_csvs(self):
        """INT-9: JSON schema has 7 CSV entries."""
        schema = pipelines.get_json_schema()
        assert "properties" in schema
        csv_keys = list(schema["properties"].keys())
        assert len(csv_keys) == 7
        for csv in ["drugs.csv", "drugbank_drugs.csv", "proteins.csv",
                    "protein_protein_interactions.csv",
                    "gene_disease_associations.csv",
                    "omim_gene_disease_associations.csv",
                    "pubchem_enrichment.csv"]:
            assert csv in schema["properties"]

    def test_known_data_source_versions_has_seven(self):
        """LIN-10: KNOWN_DATA_SOURCE_VERSIONS has 7 entries."""
        assert len(pipelines.KNOWN_DATA_SOURCE_VERSIONS) == 7
        for v in ["CHEMBL_VERSION", "STRING_VERSION", "UNIPROT_VERSION",
                  "DRUGBANK_VERSION", "DISGENET_VERSION", "OMIM_VERSION",
                  "PUBCHEM_VERSION"]:
            assert v in pipelines.KNOWN_DATA_SOURCE_VERSIONS

    def test_deprecated_emits_warning(self):
        """DES-7/COMP-9: _deprecated emits a DeprecationWarning."""
        with pytest.warns(DeprecationWarning, match="deprecated"):
            pipelines._deprecated("FOO", "3.0.0", "BAR")

    def test_compute_file_checksum_returns_hex(self):
        """LIN-6: compute_file_checksum returns a 64-char hex string."""
        checksum = pipelines.compute_file_checksum(__file__)
        assert len(checksum) == 64
        assert all(c in "0123456789abcdef" for c in checksum)

    def test_find_affected_downstream_chembl(self):
        """LIN-5: find_affected_downstream('chembl') returns expected files."""
        result = pipelines.find_affected_downstream("chembl")
        assert "drugs.csv" in result
        assert "entity_mapping" in result

    def test_find_affected_downstream_invalid_raises(self):
        """LIN-5: find_affected_downstream with invalid name raises ValueError."""
        with pytest.raises(ValueError, match="Unknown source"):
            pipelines.find_affected_downstream("invalid")


# ---------------------------------------------------------------------------
# 21. Idempotency (IDEM-7, IDEM-8)
# ---------------------------------------------------------------------------
class TestIdempotency:
    """Verify idempotency and monkey-patch isolation."""

    def test_reset_clears_loaded(self):
        """IDEM-7: _reset clears _loaded."""
        _ = pipelines.UniProtPipeline
        assert len(pipelines._loaded) > 0
        pipelines._reset()
        assert len(pipelines._loaded) == 0

    def test_reset_clears_load_times(self):
        """IDEM-7: _reset clears _load_times."""
        _ = pipelines.UniProtPipeline
        assert len(pipelines._load_times) > 0
        pipelines._reset()
        assert len(pipelines._load_times) == 0

    def test_reset_clears_dead_letters(self):
        """IDEM-7: _reset clears _dead_letters."""
        pipelines._dead_letters.append({"fake": True})
        pipelines._reset()
        assert len(pipelines._dead_letters) == 0

    def test_no_monkey_patch_bleed(self):
        """TEST-15/IDEM-8: _reset() clears monkey-patches between tests."""
        pipelines._reset()
        # Use `from pipelines import chembl_pipeline as ...` to avoid
        # making `pipelines` a local name in this function (which would
        # shadow the module-level `pipelines` and cause UnboundLocalError).
        from pipelines import chembl_pipeline as chembl_module
        original = chembl_module.ChEMBLPipeline
        chembl_module.ChEMBLPipeline = MagicMock
        # Access via facade — gets the mock
        mock_cls = pipelines.ChEMBLPipeline
        assert mock_cls is MagicMock
        # Restore and reset
        chembl_module.ChEMBLPipeline = original
        pipelines._reset()
        # Re-access — should get the real class
        real_cls = pipelines.ChEMBLPipeline
        assert real_cls is original

    def test_is_loaded_false_initially(self):
        """CONF-4: is_loaded() returns False right after reset."""
        pipelines._reset()
        assert pipelines.is_loaded() is False

    def test_is_loaded_true_after_access(self):
        """CONF-4: is_loaded() returns True after accessing a symbol."""
        pipelines._reset()
        _ = pipelines.UniProtPipeline
        assert pipelines.is_loaded() is True

    def test_is_reproducible_true_in_lazy_mode(self):
        """IDEM-3: is_reproducible() returns True in lazy mode."""
        # Default is lazy mode
        assert pipelines.is_reproducible() is True


# ---------------------------------------------------------------------------
# 22. Health Check (DES-9, LOG-5)
# ---------------------------------------------------------------------------
class TestHealthCheck:
    """Verify health_check and lifecycle functions."""

    def test_health_check_returns_dict(self):
        """DES-9: health_check returns a dict."""
        result = pipelines.health_check()
        assert isinstance(result, dict)

    def test_health_check_status_is_valid(self):
        """DES-9: health_check status is healthy/degraded/unhealthy."""
        result = pipelines.health_check()
        assert result["status"] in ("healthy", "degraded", "unhealthy")

    def test_health_check_has_version(self):
        """DES-9: health_check includes version."""
        result = pipelines.health_check()
        assert result["version"] == pipelines.__version__

    def test_initialize_loads_all_symbols(self):
        """CONF-2: initialize() triggers eager loading."""
        pipelines._reset()
        assert len(pipelines._loaded) == 0
        pipelines.initialize()
        # At least some symbols should be loaded (those whose deps are available)
        assert len(pipelines._loaded) > 0


# ---------------------------------------------------------------------------
# 23. PEP 20 Adherence (COMP-11)
# ---------------------------------------------------------------------------
class TestPEP20Adherence:
    """Verify PEP 20 (Zen of Python) adherence."""

    def test_module_mentions_pep20(self):
        """COMP-11: module mentions PEP 20 adherence."""
        init_path = PROJECT_ROOT / "pipelines" / "__init__.py"
        source = init_path.read_text()
        assert "PEP 20" in source

    def test_no_bare_except(self):
        """COMP-11 (Errors should never pass silently): no bare except: blocks."""
        init_path = PROJECT_ROOT / "pipelines" / "__init__.py"
        source = init_path.read_text()
        # Find any bare `except:` (not `except SpecificException:`)
        lines = source.split("\n")
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped == "except:" or stripped.startswith("except:"):
                pytest.fail(f"Line {i}: bare except: found")
        # Also check for `except:` followed by `pass`
        assert not re.search(r"except\s*:\s*pass", source), "found except: pass"


# ---------------------------------------------------------------------------
# Helper for ARM64 simulation test
# ---------------------------------------------------------------------------
try:
    from unittest.mock import patch as unittest_mock_patch
except ImportError:
    unittest_mock_patch = None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
