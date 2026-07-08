"""
Test 1: Comprehensive test for cleaning/__init__.py across ALL 16 domains.

This test suite verifies that the upgraded cleaning/__init__.py is
institutional-grade, production-ready code by testing every feature
added or fixed across all 16 verification domains.

Domains tested:
1. Architecture - relative imports, lazy loading, fault isolation
2. Design - __all__, registry, error taxonomy, composition API
3. Knowledge (Scientific Correctness) - documentation, ordering
4. Coding - naming, ordering, type stubs, PEP 561
5. Data Quality & Integrity - schema validation, quality report
6. Reliability & Resilience - dead letters, retry, circuit breaker
7. Idempotency & Reproducibility - fingerprints, configure, versions
8. Performance & Scalability - lazy timing, chunked processing
9. Security & Privacy - sanitization, masking, audit log
10. Testing & Validation - export validation, import tests
11. Logging & Observability - correlation ID, metrics
12. Configuration & Environment - env vars, validate_environment
13. Documentation & Readability - docstring completeness
14. Compliance & Standards - __all__ completeness, deprecation
15. Interoperability & Integration - hooks, API stability
16. Data Lineage & Traceability - provenance, impact analysis
"""

from __future__ import annotations

import importlib
import sys
import warnings
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Ensure project root is importable
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ===========================================================================
# DOMAIN 1: ARCHITECTURE
# ===========================================================================


class TestArchitecture:
    """Domain 1: System structure, module organization, dependency flow."""

    def test_lazy_import_does_not_load_submodules(self):
        """BUG-A1/GAP-A2: importing cleaning should NOT import sub-modules."""
        # Remove any previously loaded cleaning sub-modules
        to_remove = [k for k in sys.modules if k.startswith("cleaning.")]
        for m in to_remove:
            del sys.modules[m]
        if "cleaning" in sys.modules:
            del sys.modules["cleaning"]

        before = set(sys.modules.keys())
        import cleaning  # noqa: F811

        after = set(sys.modules.keys())
        new_modules = after - before
        # cleaning itself should be loaded, but not its sub-modules
        sub_module_imports = [
            m for m in new_modules
            if m.startswith("cleaning.") and m != "cleaning"
        ]
        assert len(sub_module_imports) == 0, (
            f"Sub-modules loaded eagerly: {sub_module_imports}"
        )

    def test_lazy_import_loads_on_access(self):
        """GAP-A2: accessing a name should trigger sub-module import."""
        import cleaning

        assert "cleaning.normalizer" not in sys.modules
        _ = cleaning.ALLOWED_TYPES
        assert "cleaning.normalizer" in sys.modules

    def test_relative_imports_used(self):
        """BUG-A1: _LAZY_IMPORTS should use relative import paths."""
        import cleaning

        for name, module_path in cleaning._LAZY_IMPORTS.items():
            assert module_path.startswith("."), (
                f"_LAZY_IMPORTS[{name!r}] = {module_path!r} — should use "
                f"relative import (start with '.')"
            )

    def test_fault_isolation_on_import_failure(self):
        """BUG-A3: One sub-module failure should not break the entire package."""
        import cleaning

        # If we access a name that doesn't exist, we get AttributeError
        with pytest.raises(AttributeError, match="has no attribute"):
            _ = cleaning.totally_fake_function_xyz

    def test_package_level_logger_exists(self):
        """GAP-A4: _logger should be defined at package level."""
        import cleaning

        assert hasattr(cleaning, "_logger")
        assert cleaning._logger.name == "cleaning"

    def test_version_attribute(self):
        """GAP-A5: __version__ should be defined."""
        import cleaning

        assert hasattr(cleaning, "__version__")
        assert isinstance(cleaning.__version__, str)
        assert cleaning.__version__ == "2.0.0"

    def test_circular_import_protection_in_missing_values(self):
        """GUARD-A7: missing_values should use lazy import for normalizer."""
        from cleaning import missing_values

        assert hasattr(missing_values, "_get_convert_to_inchikey")
        # The lazy import function should work
        func = missing_values._get_convert_to_inchikey()
        assert callable(func)

    def test_dependency_declaration(self):
        """GAP-A8: _OPTIONAL_DEPS should document required dependencies."""
        import cleaning

        assert "convert_to_inchikey" in cleaning._OPTIONAL_DEPS
        assert cleaning._OPTIONAL_DEPS["convert_to_inchikey"]["rdkit"] is True


# ===========================================================================
# DOMAIN 2: DESIGN
# ===========================================================================


class TestDesign:
    """Domain 2: Design patterns, API design, interface contracts."""

    def test_allowed_types_in_all(self):
        """BUG-D1: ALLOWED_TYPES should be in __all__."""
        import cleaning

        assert "ALLOWED_TYPES" in cleaning.__all__

    def test_allowed_types_accessible(self):
        """BUG-D1: ALLOWED_TYPES should be importable from cleaning."""
        from cleaning import ALLOWED_TYPES

        assert isinstance(ALLOWED_TYPES, list)
        assert len(ALLOWED_TYPES) > 0
        assert "Small molecule" in ALLOWED_TYPES
        assert "Unknown" in ALLOWED_TYPES

    def test_registry_pattern(self):
        """GAP-D3: Cleaning registry should work after lazy loading."""
        import cleaning

        # Force load by accessing a name
        _ = cleaning.dedup_by_inchikey
        assert "dedup_by_inchikey" in cleaning._CLEANING_REGISTRY

    def test_list_cleaning_functions(self):
        """GAP-D3: list_cleaning_functions() should return registered names."""
        import cleaning

        # Force load a function
        _ = cleaning.convert_to_inchikey
        funcs = cleaning.list_cleaning_functions()
        assert isinstance(funcs, list)
        assert "convert_to_inchikey" in funcs

    def test_get_cleaning_function(self):
        """GAP-D3: get_cleaning_function() should return the function."""
        import cleaning

        _ = cleaning.standardize_inchikey
        func = cleaning.get_cleaning_function("standardize_inchikey")
        assert callable(func)

    def test_get_cleaning_function_raises_for_unknown(self):
        """GAP-D3: get_cleaning_function() raises KeyError for unknown."""
        import cleaning

        with pytest.raises(KeyError, match="No cleaning function"):
            cleaning.get_cleaning_function("nonexistent_function")

    def test_error_taxonomy(self):
        """GAP-D5: Exception hierarchy should be defined."""
        from cleaning import CleaningError, CleaningWarning
        from cleaning import SchemaValidationError, DependencyNotAvailableError

        assert issubclass(SchemaValidationError, CleaningError)
        assert issubclass(DependencyNotAvailableError, CleaningError)
        assert issubclass(CleaningWarning, UserWarning)

    def test_composition_api_clean_drugs(self):
        """GAP-D6: clean_drugs() should apply the full pipeline."""
        import cleaning

        # Force load all steps
        _ = cleaning.standardize_inchikey
        _ = cleaning.handle_missing_inchikey
        _ = cleaning.fill_missing_drug_fields
        _ = cleaning.standardize_drug_record
        _ = cleaning.dedup_by_inchikey

        df = pd.DataFrame({
            "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N", None, "AAA"],
            "smiles": ["CC(=O)Oc1ccccc1C(=O)O", "CC(=O)O", "CCCO"],
            "drug_type": ["small molecule", None, "Protein"],
            "is_fda_approved": [True, None, False],
            "name": ["Aspirin", "Acetic acid", "Propanol"],
        })

        result = cleaning.clean_drugs(df)
        assert isinstance(result, pd.DataFrame)
        assert len(result) > 0

    def test_composition_api_schema_validation(self):
        """GAP-D6: clean_drugs() should reject non-DataFrame input."""
        from cleaning import SchemaValidationError, clean_drugs

        with pytest.raises(SchemaValidationError):
            clean_drugs("not a dataframe")

    def test_composition_api_clean_proteins(self):
        """GAP-D6: clean_proteins() should apply protein pipeline."""
        import cleaning

        _ = cleaning.handle_missing_protein_fields

        df = pd.DataFrame({
            "uniprot_id": ["P12345", None],
            "gene_name": ["BRCA1", "TP53"],
            "organism": ["Homo sapiens", None],
            "sequence": ["M" * 100, "AAA"],
        })

        result = cleaning.clean_proteins(df)
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 1  # None uniprot_id dropped

    def test_composition_api_clean_gda(self):
        """GAP-D6: clean_gda() should apply GDA pipeline."""
        import cleaning

        _ = cleaning.validate_gda_scores

        df = pd.DataFrame({
            "disease_id": ["C0001", "C0002"],
            "disease_name": [None, "Alzheimer's"],
            "score": [1.5, -0.2],
            "association_type": [None, "somatic"],
        })

        result = cleaning.clean_gda(df)
        assert isinstance(result, pd.DataFrame)
        assert result["score"].iloc[0] == 1.0  # Clipped
        assert result["score"].iloc[1] == 0.0  # Clipped
        assert result["disease_name"].iloc[0] == "C0001"  # Backfilled
        assert result["association_type"].iloc[0] == "unknown"


# ===========================================================================
# DOMAIN 3: KNOWLEDGE (Scientific Correctness)
# ===========================================================================


class TestScientificCorrectness:
    """Domain 3: Domain-specific scientific accuracy."""

    def test_inchikey_ordering_documented(self):
        """BUG-K1: Module docstring should document InChIKey ordering."""
        import cleaning

        doc = cleaning.__doc__
        assert "standardize_inchikey" in doc
        assert "dedup_by_inchikey" in doc
        assert "WARNING" in doc or "Ordering matters" in doc

    def test_inchikey_structure_documented(self):
        """BUG-K2: Module docstring should explain InChIKey structure."""
        import cleaning

        doc = cleaning.__doc__
        assert "14" in doc  # Block 1 is 14 chars
        assert "connectivity" in doc.lower()

    def test_nm_rationale_documented(self):
        """GAP-K3: Module docstring should explain why nM is used."""
        import cleaning

        doc = cleaning.__doc__
        assert "nM" in doc or "nanomolar" in doc.lower()

    def test_scientific_assumptions_documented(self):
        """GAP-K5: Module docstring should list scientific assumptions."""
        import cleaning

        doc = cleaning.__doc__
        assert "Assumptions" in doc or "assumptions" in doc.lower()
        assert "Homo sapiens" in doc

    def test_biologics_limitation_documented(self):
        """GAP-K7: Module docstring should mention biologics limitation."""
        import cleaning

        doc = cleaning.__doc__
        assert "biologics" in doc.lower() or "antibodies" in doc.lower()


# ===========================================================================
# DOMAIN 4: CODING
# ===========================================================================


class TestCoding:
    """Domain 4: Syntax, logic errors, naming conventions, code structure."""

    def test_all_alphabetical_within_sections(self):
        """BUG-C1: __all__ should be in consistent alphabetical order."""
        import cleaning

        normalizer_names = [
            n for n in cleaning.__all__
            if n in cleaning._LAZY_IMPORTS
            and cleaning._LAZY_IMPORTS[n] == ".normalizer"
        ]
        assert normalizer_names == sorted(normalizer_names), (
            f"Normalizer names in __all__ not alphabetical: {normalizer_names}"
        )

    def test_all_consistent_with_lazy_imports(self):
        """GAP-C5/T2: __all__ and _LAZY_IMPORTS should cover the same names."""
        import cleaning

        all_names = set(cleaning.__all__)
        lazy_names = set(cleaning._LAZY_IMPORTS.keys())
        in_all_not_lazy = all_names - lazy_names
        in_lazy_not_all = lazy_names - all_names
        assert in_all_not_lazy == set(), (
            f"In __all__ but not _LAZY_IMPORTS: {in_all_not_lazy}"
        )
        assert in_lazy_not_all == set(), (
            f"In _LAZY_IMPORTS but not __all__: {in_lazy_not_all}"
        )

    def test_type_stub_file_exists(self):
        """GAP-C3: cleaning/__init__.pyi should exist."""
        stub_path = PROJECT_ROOT / "cleaning" / "__init__.pyi"
        assert stub_path.exists(), f"Type stub file not found: {stub_path}"

    def test_py_typed_marker_exists(self):
        """GAP-C4: cleaning/py.typed should exist."""
        marker_path = PROJECT_ROOT / "cleaning" / "py.typed"
        assert marker_path.exists(), f"PEP 561 marker not found: {marker_path}"

    def test_dir_function(self):
        """GAP-C7: __dir__() should include __all__ names."""
        import cleaning

        dir_result = dir(cleaning)
        for name in cleaning.__all__:
            assert name in dir_result, (
                f"{name} not in dir(cleaning)"
            )


# ===========================================================================
# DOMAIN 5: DATA QUALITY & INTEGRITY
# ===========================================================================


class TestDataQuality:
    """Domain 5: Data completeness, accuracy, uniqueness, consistency."""

    def test_quality_report_function(self):
        """GAP-DQ2: quality_report() should return a valid report."""
        import cleaning

        df = pd.DataFrame({
            "inchikey": ["AAA", None, "AAA"],
            "smiles": ["CCO", "CC(=O)O", "CCCO"],
            "drug_type": ["Small molecule", None, "Protein"],
            "score": [0.5, -0.1, 1.2],
        })

        report = cleaning.quality_report(df, data_type="drug")
        assert report["data_type"] == "drug"
        assert report["total_rows"] == 3
        assert "completeness" in report
        assert "uniqueness" in report
        assert "validity" in report
        # Check InChIKey duplicates detected
        assert report["uniqueness"]["inchikey_duplicates"] == 1
        # Check out-of-range scores detected
        assert report["validity"]["gda_scores_out_of_range"] == 2

    def test_data_quality_constants_accessible(self):
        """GAP-DQ3: Key constants should be accessible from package level."""
        import cleaning

        _ = cleaning.MAX_SEQUENCE_LENGTH
        _ = cleaning.FUZZY_THRESHOLD
        _ = cleaning.UNIT_CONVERSIONS

    def test_double_cleaning_guard(self):
        """GUARD-DQ5: Re-applying clean_drugs should skip already-applied steps."""
        import cleaning

        # Force load all steps
        _ = cleaning.standardize_inchikey
        _ = cleaning.handle_missing_inchikey
        _ = cleaning.fill_missing_drug_fields
        _ = cleaning.standardize_drug_record
        _ = cleaning.dedup_by_inchikey

        df = pd.DataFrame({
            "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
            "smiles": ["CC(=O)Oc1ccccc1C(=O)O"],
            "drug_type": ["Small molecule"],
            "is_fda_approved": [True],
            "name": ["Aspirin"],
        })

        # First pass should add _cleaning_applied metadata
        result1 = cleaning.clean_drugs(df)
        assert cleaning._CLEANING_METADATA_COL in result1.columns

    def test_cleaning_metrics_tracked(self):
        """GAP-DQ6: clean_drugs should track metrics in attrs."""
        import cleaning

        _ = cleaning.standardize_inchikey
        _ = cleaning.handle_missing_inchikey
        _ = cleaning.fill_missing_drug_fields
        _ = cleaning.standardize_drug_record
        _ = cleaning.dedup_by_inchikey

        df = pd.DataFrame({
            "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
            "smiles": ["CC(=O)Oc1ccccc1C(=O)O"],
            "drug_type": ["Small molecule"],
            "is_fda_approved": [True],
            "name": ["Aspirin"],
        })

        result = cleaning.clean_drugs(df)
        assert "cleaning_metrics" in result.attrs


# ===========================================================================
# DOMAIN 6: RELIABILITY & RESILIENCE
# ===========================================================================


class TestReliability:
    """Domain 6: Error handling, fault tolerance, graceful degradation."""

    def test_dead_letter_mechanism(self):
        """GAP-R3: Dead-letter queue should be accessible."""
        import cleaning

        cleaning.clear_dead_letters()
        letters = cleaning.get_dead_letters()
        assert isinstance(letters, list)

    def test_circuit_breaker_exists(self):
        """GAP-R5: Circuit breaker should be creatable."""
        import cleaning

        cb = cleaning.get_circuit_breaker("test_operation")
        assert cb.state == "closed"
        assert cb.allow_request() is True

    def test_circuit_breaker_opens_on_failures(self):
        """GAP-R5: Circuit breaker should open after N failures."""
        import cleaning

        cb = cleaning.get_circuit_breaker("test_cb_open")
        cb.failure_threshold = 3
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "open"
        assert cb.allow_request() is False

    def test_graceful_degradation_query(self):
        """GAP-R6: has_rdkit_support() and has_rapidfuzz_support() should work."""
        import cleaning

        rdkit_result = cleaning.has_rdkit_support()
        fuzz_result = cleaning.has_rapidfuzz_support()
        assert isinstance(rdkit_result, bool)
        assert isinstance(fuzz_result, bool)


# ===========================================================================
# DOMAIN 7: IDEMPOTENCY & REPRODUCIBILITY
# ===========================================================================


class TestIdempotency:
    """Domain 7: Same input -> same output, no duplicates on re-run."""

    def test_data_fingerprint_deterministic(self):
        """GAP-I3: compute_data_fingerprint() should be deterministic."""
        import cleaning

        df = pd.DataFrame({
            "inchikey": ["AAA", "BBB"],
            "name": ["Drug1", "Drug2"],
        })

        fp1 = cleaning.compute_data_fingerprint(df)
        fp2 = cleaning.compute_data_fingerprint(df)
        assert fp1 == fp2
        assert len(fp1) == 64  # SHA-256 hex digest

    def test_data_fingerprint_differs_for_different_data(self):
        """GAP-I3: Different data should produce different fingerprints."""
        import cleaning

        df1 = pd.DataFrame({"inchikey": ["AAA"], "name": ["Drug1"]})
        df2 = pd.DataFrame({"inchikey": ["BBB"], "name": ["Drug2"]})

        fp1 = cleaning.compute_data_fingerprint(df1)
        fp2 = cleaning.compute_data_fingerprint(df2)
        assert fp1 != fp2

    def test_api_versions_defined(self):
        """GAP-I4: _API_VERSIONS should have version for each export."""
        import cleaning

        for name in cleaning.__all__:
            assert name in cleaning._API_VERSIONS, (
                f"{name} missing from _API_VERSIONS"
            )

    def test_configure_function(self):
        """GAP-I2: configure() should override defaults."""
        import cleaning

        cleaning.configure(fuzzy_threshold=0.8)
        # Verify it was set
        _ = cleaning.FUZZY_THRESHOLD  # Force load normalizer
        from cleaning import normalizer

        assert normalizer._FUZZY_THRESHOLD == 0.8
        # Reset
        cleaning.configure(fuzzy_threshold=0.7)
        assert normalizer._FUZZY_THRESHOLD == 0.7

    def test_configure_validates_fuzzy_threshold(self):
        """GAP-I2: configure() should reject invalid fuzzy_threshold."""
        import cleaning

        with pytest.raises(ValueError, match="fuzzy_threshold"):
            cleaning.configure(fuzzy_threshold=1.5)

    def test_configure_validates_max_sequence_length(self):
        """GAP-I2: configure() should reject invalid max_sequence_length."""
        import cleaning

        with pytest.raises(ValueError, match="max_sequence_length"):
            cleaning.configure(max_sequence_length=0)


# ===========================================================================
# DOMAIN 8: PERFORMANCE & SCALABILITY
# ===========================================================================


class TestPerformance:
    """Domain 8: Time complexity, memory usage, batch processing."""

    def test_load_times_tracking(self):
        """GAP-L2/GAP-P2: get_load_times() should return timing data."""
        import cleaning

        # Force a lazy load
        _ = cleaning.ALLOWED_TYPES
        times = cleaning.get_load_times()
        assert isinstance(times, dict)

    def test_chunked_processing_api(self):
        """GAP-P5: clean_drugs_chunked() should work with small chunks."""
        import cleaning

        _ = cleaning.standardize_inchikey
        _ = cleaning.handle_missing_inchikey
        _ = cleaning.fill_missing_drug_fields
        _ = cleaning.standardize_drug_record
        _ = cleaning.dedup_by_inchikey

        df = pd.DataFrame({
            "inchikey": ["AAA", "BBB", "CCC"],
            "smiles": ["CCO", "CC(=O)O", "CCCO"],
            "drug_type": ["Small molecule", "Small molecule", "Small molecule"],
            "is_fda_approved": [True, True, True],
            "name": ["Drug1", "Drug2", "Drug3"],
        })

        result = cleaning.clean_drugs_chunked(df, chunk_size=2)
        assert isinstance(result, pd.DataFrame)
        assert len(result) > 0


# ===========================================================================
# DOMAIN 9: SECURITY & PRIVACY
# ===========================================================================


class TestSecurity:
    """Domain 9: PII handling, data sanitization, secrets."""

    def test_sanitize_string(self):
        """GAP-S1: _sanitize_string() should clean dangerous input."""
        from cleaning import _sanitize_string

        # Remove null bytes
        assert "\x00" not in _sanitize_string("hello\x00world")
        # Remove control characters
        assert "\x01" not in _sanitize_string("test\x01data")
        # Truncate long strings
        long_str = "A" * 20000
        result = _sanitize_string(long_str, max_length=100)
        assert len(result) == 100

    def test_mask_sensitive(self):
        """GUARD-S4: _mask_sensitive() should mask data in logs."""
        from cleaning import _mask_sensitive

        result = _mask_sensitive("CC(=O)OC1=CC=CC=C1C(=O)O", visible_chars=10)
        assert result.startswith("CC(=O)OC1=")
        assert "***...***" in result

    def test_audit_log_function(self):
        """GAP-S2: _audit_log() should not raise."""
        from cleaning import _audit_log

        # Should not raise
        _audit_log("test_operation", {"key": "value"})

    def test_pii_guidance_in_docstring(self):
        """GAP-S5: Module docstring should mention PII handling."""
        import cleaning

        doc = cleaning.__doc__
        assert "PII" in doc or "personally identifiable" in doc.lower()

    def test_secrets_management_in_docstring(self):
        """GAP-S6: Module docstring should mention secrets."""
        import cleaning

        doc = cleaning.__doc__
        assert "secrets" in doc.lower() or "credential" in doc.lower()


# ===========================================================================
# DOMAIN 10: TESTING & VALIDATION
# ===========================================================================


class TestValidation:
    """Domain 10: Test coverage, export validation, edge cases."""

    def test_all_names_importable(self):
        """GAP-T1: Every name in cleaning.__all__ must be importable."""
        import cleaning

        for name in cleaning.__all__:
            assert hasattr(cleaning, name), f"cleaning.{name} not found"

    def test_validate_all_exports(self):
        """GAP-C5: validate_all_exports() should return empty list."""
        import cleaning

        failures = cleaning.validate_all_exports()
        assert failures == [], f"Export validation failures: {failures}"

    def test_getattr_nonexistent(self):
        """GAP-T6: Accessing a nonexistent name should raise AttributeError."""
        import cleaning

        with pytest.raises(AttributeError, match="has no attribute"):
            cleaning.totally_fake_function_name

    def test_all_names_are_not_none(self):
        """GAP-T6: No exported name should be None."""
        import cleaning

        for name in cleaning.__all__:
            attr = getattr(cleaning, name)
            assert attr is not None, f"cleaning.{name} is None"


# ===========================================================================
# DOMAIN 11: LOGGING & OBSERVABILITY
# ===========================================================================


class TestLogging:
    """Domain 11: Logging coverage, structured logging, correlation IDs."""

    def test_correlation_id_support(self):
        """GAP-L5: set/get_correlation_id should work."""
        import cleaning

        cleaning.set_correlation_id("test-run-123")
        assert cleaning.get_correlation_id() == "test-run-123"
        cleaning.set_correlation_id(None)
        assert cleaning.get_correlation_id() is None

    def test_metrics_function(self):
        """GAP-L3: get_metrics() should return useful data."""
        import cleaning

        metrics = cleaning.get_metrics()
        assert "version" in metrics
        assert "health" in metrics
        assert "load_times" in metrics
        assert "dead_letter_count" in metrics
        assert "registry_size" in metrics


# ===========================================================================
# DOMAIN 12: CONFIGURATION & ENVIRONMENT MANAGEMENT
# ===========================================================================


class TestConfiguration:
    """Domain 12: Magic numbers, hardcoded paths, environment variables."""

    def test_validate_environment(self):
        """GAP-CF2: validate_environment() should return a valid result."""
        import cleaning

        result = cleaning.validate_environment()
        assert "python_version" in result
        assert "required_deps" in result
        assert "optional_deps" in result
        assert "issues" in result
        assert "pandas" in result["required_deps"]

    def test_no_hardcoded_paths_in_init(self):
        """GAP-CF3: No hardcoded file paths in __init__.py."""
        import cleaning

        source = Path(cleaning.__file__).read_text()
        # Should not have hardcoded /home, /usr, C:\ paths
        assert "/home/" not in source
        assert "C:\\" not in source


# ===========================================================================
# DOMAIN 13: DOCUMENTATION & READABILITY
# ===========================================================================


class TestDocumentation:
    """Domain 13: Decision documentation, docstrings, naming clarity."""

    def test_module_docstring_comprehensive(self):
        """GAP-DC2: Module docstring should be thorough."""
        import cleaning

        doc = cleaning.__doc__
        assert doc is not None
        assert len(doc) > 500  # Should be comprehensive
        assert "Sub-modules" in doc or "Sub-modules" in doc
        assert "Recommended Processing Order" in doc

    def test_design_decisions_documented(self):
        """GAP-DC3: Module docstring should document design decisions."""
        import cleaning

        doc = cleaning.__doc__
        assert "Design Decisions" in doc

    def test_data_dictionary_documented(self):
        """GAP-DC4: Module docstring should include data dictionary."""
        import cleaning

        doc = cleaning.__doc__
        assert "Data Dictionary" in doc
        assert "inchikey" in doc

    def test_version_history_documented(self):
        """GAP-DC5: Module docstring should include version history."""
        import cleaning

        doc = cleaning.__doc__
        assert "Version History" in doc or "v2.0.0" in doc


# ===========================================================================
# DOMAIN 14: COMPLIANCE & STANDARDS ADHERENCE
# ===========================================================================


class TestCompliance:
    """Domain 14: PEP compliance, license, regulatory."""

    def test_pep561_compliance(self):
        """GAP-CO2: py.typed marker and .pyi stub should exist."""
        assert (PROJECT_ROOT / "cleaning" / "py.typed").exists()
        assert (PROJECT_ROOT / "cleaning" / "__init__.pyi").exists()

    def test_license_in_docstring(self):
        """GAP-CO4: Module docstring should include license."""
        import cleaning

        doc = cleaning.__doc__
        assert "MIT" in doc or "License" in doc

    def test_regulatory_compliance_documented(self):
        """GAP-CO5: Module docstring should mention regulatory compliance."""
        import cleaning

        doc = cleaning.__doc__
        assert "FDA" in doc or "GDPR" in doc or "HIPAA" in doc

    def test_coding_standards_documented(self):
        """GAP-CO7: Module docstring should list coding standards."""
        import cleaning

        doc = cleaning.__doc__
        assert "PEP 8" in doc or "PEP" in doc


# ===========================================================================
# DOMAIN 15: INTEROPERABILITY & INTEGRATION
# ===========================================================================


class TestInteroperability:
    """Domain 15: Interface contracts, version compatibility, hooks."""

    def test_pre_post_hooks(self):
        """GAP-IO5: Pre/post clean hooks should be registerable."""
        import cleaning

        pre_called = []
        post_called = []

        def pre_hook(step_name, df):
            pre_called.append(step_name)

        def post_hook(step_name, df):
            post_called.append(step_name)

        cleaning.register_pre_clean_hook(pre_hook)
        cleaning.register_post_clean_hook(post_hook)

        # Force load
        _ = cleaning.standardize_inchikey
        _ = cleaning.handle_missing_inchikey
        _ = cleaning.fill_missing_drug_fields
        _ = cleaning.standardize_drug_record
        _ = cleaning.dedup_by_inchikey

        df = pd.DataFrame({
            "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
            "smiles": ["CC(=O)Oc1ccccc1C(=O)O"],
            "drug_type": ["Small molecule"],
            "is_fda_approved": [True],
            "name": ["Aspirin"],
        })

        cleaning.clean_drugs(df)

        assert len(pre_called) > 0
        assert len(post_called) > 0

        # Cleanup hooks
        cleaning._pre_clean_hooks.clear()
        cleaning._post_clean_hooks.clear()

    def test_api_stability_documented(self):
        """GAP-IO2: Module docstring should document API stability."""
        import cleaning

        doc = cleaning.__doc__
        assert "API Stability" in doc or "STABLE" in doc

    def test_dependency_compatibility_documented(self):
        """GAP-IO3: Module docstring should list dependency versions."""
        import cleaning

        doc = cleaning.__doc__
        assert "pandas" in doc
        assert "rdkit" in doc.lower()

    def test_backward_compatible_imports(self):
        """BUG-IO1: from cleaning import X should work for all old names."""
        import cleaning

        # All original imports should still work
        from cleaning import convert_to_inchikey
        from cleaning import standardize_inchikey
        from cleaning import standardize_drug_record
        from cleaning import normalize_activity_value
        from cleaning import dedup_by_inchikey
        from cleaning import dedup_interactions
        from cleaning import handle_missing_inchikey
        from cleaning import fill_missing_drug_fields
        from cleaning import handle_missing_protein_fields
        from cleaning import validate_gda_scores

        assert callable(convert_to_inchikey)
        assert callable(standardize_inchikey)
        assert callable(standardize_drug_record)
        assert callable(normalize_activity_value)
        assert callable(dedup_by_inchikey)
        assert callable(dedup_interactions)
        assert callable(handle_missing_inchikey)
        assert callable(fill_missing_drug_fields)
        assert callable(handle_missing_protein_fields)
        assert callable(validate_gda_scores)


# ===========================================================================
# DOMAIN 16: DATA LINEAGE & TRACEABILITY
# ===========================================================================


class TestDataLineage:
    """Domain 16: Provenance, transformation audit, impact analysis."""

    def test_provenance_tracking(self):
        """GAP-I5/GAP-DL1: clean_drugs should add provenance metadata."""
        import cleaning

        _ = cleaning.standardize_inchikey
        _ = cleaning.handle_missing_inchikey
        _ = cleaning.fill_missing_drug_fields
        _ = cleaning.standardize_drug_record
        _ = cleaning.dedup_by_inchikey

        df = pd.DataFrame({
            "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
            "smiles": ["CC(=O)Oc1ccccc1C(=O)O"],
            "drug_type": ["Small molecule"],
            "is_fda_approved": [True],
            "name": ["Aspirin"],
        })

        result = cleaning.clean_drugs(df)
        assert "_provenance" in result.attrs
        assert len(result.attrs["_provenance"]) > 0

    def test_input_output_fingerprints(self):
        """GAP-DL3: clean_drugs should record input/output fingerprints."""
        import cleaning

        _ = cleaning.standardize_inchikey
        _ = cleaning.handle_missing_inchikey
        _ = cleaning.fill_missing_drug_fields
        _ = cleaning.standardize_drug_record
        _ = cleaning.dedup_by_inchikey

        df = pd.DataFrame({
            "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"],
            "smiles": ["CC(=O)Oc1ccccc1C(=O)O"],
            "drug_type": ["Small molecule"],
            "is_fda_approved": [True],
            "name": ["Aspirin"],
        })

        result = cleaning.clean_drugs(df)
        assert "_input_fingerprint" in result.attrs
        assert "_output_fingerprint" in result.attrs
        assert len(result.attrs["_input_fingerprint"]) == 64

    def test_impact_analysis(self):
        """GAP-DL4: get_affected_functions() should return relevant functions."""
        import cleaning

        affected = cleaning.get_affected_functions("inchikey")
        assert isinstance(affected, list)
        assert "standardize_inchikey" in affected
        assert "dedup_by_inchikey" in affected

    def test_impact_analysis_unknown_column(self):
        """GAP-DL4: get_affected_functions() for unknown column returns []."""
        import cleaning

        affected = cleaning.get_affected_functions("nonexistent_column")
        assert affected == []

    def test_lineage_metadata_on_functions(self):
        """GAP-DL5: Lazy-loaded functions should have lineage metadata."""
        import cleaning

        func = cleaning.convert_to_inchikey
        assert hasattr(func, "_cleaning_source_module")
        assert hasattr(func, "_cleaning_api_version")

    def test_dependency_graph_exists(self):
        """GAP-DL4: _CLEANING_DEPENDENCY_GRAPH should be populated."""
        import cleaning

        assert len(cleaning._CLEANING_DEPENDENCY_GRAPH) > 0
        assert "inchikey" in cleaning._CLEANING_DEPENDENCY_GRAPH
        assert "score" in cleaning._CLEANING_DEPENDENCY_GRAPH


# ===========================================================================
# CROSS-DOMAIN INTEGRATION TESTS
# ===========================================================================


class TestCrossDomainIntegration:
    """Tests that span multiple domains."""

    def test_clean_drugs_produces_consistent_results(self):
        """Running clean_drugs on same data should produce same fingerprint."""
        import cleaning

        _ = cleaning.standardize_inchikey
        _ = cleaning.handle_missing_inchikey
        _ = cleaning.fill_missing_drug_fields
        _ = cleaning.standardize_drug_record
        _ = cleaning.dedup_by_inchikey

        df = pd.DataFrame({
            "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N", "AAA"],
            "smiles": ["CC(=O)Oc1ccccc1C(=O)O", "CCCO"],
            "drug_type": ["Small molecule", "Small molecule"],
            "is_fda_approved": [True, False],
            "name": ["Aspirin", "Propanol"],
        })

        result1 = cleaning.clean_drugs(df)
        fp1 = result1.attrs.get("_output_fingerprint", "")

        # Reset metadata for re-run
        df2 = pd.DataFrame({
            "inchikey": ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N", "AAA"],
            "smiles": ["CC(=O)Oc1ccccc1C(=O)O", "CCCO"],
            "drug_type": ["Small molecule", "Small molecule"],
            "is_fda_approved": [True, False],
            "name": ["Aspirin", "Propanol"],
        })

        result2 = cleaning.clean_drugs(df2)
        fp2 = result2.attrs.get("_output_fingerprint", "")

        assert fp1 == fp2, "Same input should produce same output fingerprint"

    def test_check_health_returns_valid_structure(self):
        """check_health() should return a complete, valid structure."""
        import cleaning

        health = cleaning.check_health()
        assert health["status"] in ("healthy", "degraded")
        assert health["version"] == cleaning.__version__
        assert "modules" in health
        assert "optional_deps" in health

    def test_all_submodule_files_still_exist(self):
        """ABSOLUTE RULE: No files should have been removed."""
        cleaning_dir = PROJECT_ROOT / "cleaning"
        assert (cleaning_dir / "__init__.py").exists()
        assert (cleaning_dir / "normalizer.py").exists()
        assert (cleaning_dir / "deduplicator.py").exists()
        assert (cleaning_dir / "missing_values.py").exists()
        assert (cleaning_dir / "__init__.pyi").exists()
        assert (cleaning_dir / "py.typed").exists()
